"""Local, retrospective season folds; never publish a deployment model.

Tree parameters are selected by validation log loss on four-match groups.
Recurrence strength/window are then selected on validation separately per size.
Test labels never select parameters. All members of a group share its first
UTC-day information cutoff, including groups spanning several match days.
"""

import cloud_storage
from collections import defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
import json
from math import fsum, isclose, isfinite, log
import os
from pathlib import Path
import tempfile

import api_football as api
import catboost_scenarios as ml
from coupon_sizes import validate_size
from league_model import VALUES
from league_patterns import repeated_scenarios
from match_predictor import VERSION as CLUB_VERSION, estimate
from club_identity import fixture_identity
import scenario_features as features
from scenario_predictor import best_sequences, fit_sequences, probability


VERSION = 'seasonal-validation-v1'
SOURCE = 'API-Football'
METHODS = ('frequency', 'clubs', 'combined', 'catboost_plain', 'catboost_repeats')
DEFAULT_SIZES = (2, 4, 6, 8)
CANDIDATES = ml.CANDIDATES
ALPHAS = (0.0, .25, .5, 1.0)
WINDOWS = ('all', 'last_season')
FOLDS = (((2022,), 2023, 2024), ((2022, 2023), 2024, 2025), ((2022, 2023, 2024), 2025, 2026))
MIN_SEASON_MATCHES = 30
DIAGNOSTICS_VERSION = 'seasonal-probability-diagnostics-v1'
CALIBRATION_BINS = 10
REPORT_DIR = Path(__file__).resolve().parent / 'data' / 'api_football'


class SeasonalError(ValueError):
    """Invalid data or an unavailable chronological seasonal experiment."""


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _league(league):
    try:
        return api._numeric_id(league, 'de compétition')
    except ValueError as error:
        raise SeasonalError(str(error)) from None


def _history(rows, league, source, before=None):
    if source != SOURCE:
        raise SeasonalError('Cette évaluation accepte uniquement API-Football.')
    league = _league(league)
    today = _today()
    cutoff = today if before is None else before
    try:
        if (type(cutoff) is not str or datetime.strptime(cutoff, '%Y-%m-%d').date().isoformat() != cutoff
                or cutoff > today):
            raise ValueError()
    except (ValueError, TypeError):
        raise SeasonalError('La date limite d’évaluation doit être un jour UTC actuel ou antérieur au format AAAA-MM-JJ.') from None
    try:
        history = ml._rows(list(rows), league, source, cutoff)
    except (ValueError, TypeError, KeyError) as error:
        raise SeasonalError(str(error)) from None
    for row in history:
        season = row.get('season')
        if type(season) is not int or not 1900 <= season <= 2100:
            raise SeasonalError('Chaque résultat doit conserver sa saison API-Football réelle.')
    return history, league, cutoff


def _partition(rows, seasons):
    dates = [row['timestamp'][:10] for row in rows]
    return dict(seasons=list(seasons), matches=len(rows), start=min(dates, default=None), end=max(dates, default=None))


def _partial_season(season, cutoff):
    # A season beginning in the previous calendar year may still be in play
    # through June. Do not silently promote it to complete on 1 January.
    current = datetime.fromisoformat(cutoff)
    earliest_open_season = current.year - int(current.month <= 6)
    return season >= earliest_open_season


def _implementation():
    try:
        catboost_version = package_version('catboost')
    except PackageNotFoundError:
        catboost_version = None
    return dict(feature_version=features.FEATURE_VERSION, catboost_engine=ml.VERSION,
                clubs_engine=CLUB_VERSION, frequency_engine='league-frequency-v1',
                combined_engine='contextual-scenarios-club-ids-v2', catboost_version=catboost_version,
                loss_function='MultiClass', random_seed=20261018, thread_count=2,
                has_time=True, allow_writing_files=False)


def _fold_rows(history, train_seasons, validation_season, test_season):
    """Remove late rows crossing a later season's first UTC day.

    A whole day belongs to the later period: no training or validation example
    on that boundary day can cross into the next partition.
    """
    train = [r for r in history if r['season'] in train_seasons]
    validation = [r for r in history if r['season'] == validation_season]
    test = [r for r in history if r['season'] == test_season]
    validation_start = min((r['timestamp'][:10] for r in validation), default=None)
    test_start = min((r['timestamp'][:10] for r in test), default=None)
    if validation_start:
        train = [r for r in train if r['timestamp'][:10] < validation_start]
    if test_start:
        train = [r for r in train if r['timestamp'][:10] < test_start]
        validation = [r for r in validation if r['timestamp'][:10] < test_start]
    return dict(train=train, validation=validation, test=test)


def _plan(history, league, cutoff):
    by_season = defaultdict(list)
    for row in history:
        by_season[row['season']].append(row)
    seasons = [dict(season=year, **{k: v for k, v in _partition(rows, (year,)).items() if k != 'seasons'})
               for year, rows in sorted(by_season.items())]
    folds, diagnostics = [], []
    for train_years, val_year, test_year in FOLDS:
        rows = _fold_rows(history, train_years, val_year, test_year)
        needed = (*train_years, val_year, test_year)
        missing = [year for year in needed if len(by_season[year]) < MIN_SEASON_MATCHES]
        if missing or any(len(rows[name]) < MIN_SEASON_MATCHES for name in rows):
            diagnostics.append(f'Test {test_year} indisponible : au moins {MIN_SEASON_MATCHES} résultats par saison et période sont nécessaires.')
            continue
        split = dict(train=_partition(rows['train'], train_years),
                     validation=_partition(rows['validation'], (val_year,)), test=_partition(rows['test'], (test_year,)))
        if not (split['train']['end'] < split['validation']['start'] <= split['validation']['end'] < split['test']['start']):
            diagnostics.append(f'Test {test_year} indisponible : les jours des saisons se chevauchent.')
            continue
        original_count = sum(len(by_season[year]) for year in needed)
        folds.append(dict(test_season=test_year, partial=_partial_season(test_year, cutoff),
                          **split, excluded_boundary_matches=original_count-sum(len(r) for r in rows.values())))
    return dict(version=VERSION, source=SOURCE, league=league, cutoff=cutoff,
                fingerprint=ml._fingerprint(history), seasons=seasons, folds=folds,
                suitable=bool(folds), diagnostics=diagnostics)


def plan(rows, league, source=SOURCE, *, before=None):
    """Inspect local seasons without training, network, journal, or file writes."""
    history, league, cutoff = _history(rows, league, source, before)
    return _plan(history, league, cutoff)


def _groups(history, selected, size):
    """Anchor groups once per season, then exclude partition-crossing blocks."""
    ids = {str(row['match_id']) for row in selected}
    seasons = sorted({row['season'] for row in selected})
    output = []
    for season in seasons:
        rows = [row for row in history if row['season'] == season]
        if not rows:
            continue
        blocks = repeated_scenarios(rows, size)[rows[0]['league']]['blocks']
        for block in blocks:
            if all(str(row['match_id']) in ids for row in block['matches']):
                output.append(dict(block, season=season))
    return sorted(output, key=lambda b: (b['matches'][0]['timestamp'], int(b['matches'][0]['match_id'])))


def _contexts(history, blocks_by_size):
    """Build lagged state once in time order; reuse fixtures across group sizes."""
    league = history[0]['league']
    _, prepared = features._scoped_rows(history, league, 'api-football')
    by_id = {item['match_id']: item for item in prepared}
    pending = []
    output = {size: [] for size in blocks_by_size}
    for size, blocks in blocks_by_size.items():
        for block in blocks:
            before = min(row['timestamp'][:10] for row in block['matches'])
            pending.append((before, size, block))
    history_state, pointer = features._History(), 0
    feature_cache, club_cache = {}, {}
    for before, size, block in sorted(pending, key=lambda r: (r[0], r[1], r[2]['number'])):
        while pointer < len(prepared) and prepared[pointer]['stamp'].date().isoformat() < before:
            history_state.update(prepared[pointer])
            pointer += 1
        values, keys, clubs = [], [], []
        for row in block['matches']:
            key = (str(row['match_id']), before)
            if key not in feature_cache:
                feature_cache[key] = features._features(history_state, by_id[str(row['match_id'])])
            club_key = (row.get('provider'), str(league), row.get('home_id'), row.get('away_id'),
                        row['home'], row['away'], before)
            if club_key not in club_cache:
                club_cache[club_key] = estimate(history, league, row['home'], row['away'], before,
                                               **fixture_identity(row))['probabilities']
            keys.append(key)
            values.append(feature_cache[key])
            clubs.append(club_cache[club_key])
        output[size].append(dict(block=block, before=before, features=values, feature_keys=keys, clubs=clubs))
    return output


def _predict(model, contexts):
    unique = {}
    for groups in contexts.values():
        for group in groups:
            unique.update(zip(group['feature_keys'], group['features']))
    if not unique:
        return {}
    values = ml._predict(model, list(unique.values()), list(features.FEATURE_COLUMNS), list(features.CATEGORICAL_COLUMNS))
    return dict(zip(unique, values))


def _fit(rows, params):
    examples = features.build_examples(rows, rows[0]['league'], 'api-football')
    return ml._fit(examples, params, list(features.FEATURE_COLUMNS), list(features.CATEGORICAL_COLUMNS))


def _repeat_fit(history, training, size, window):
    if window == 'last_season':
        last = max(row['season'] for row in training)
        training = [row for row in training if row['season'] == last]
    return fit_sequences([block['sequence'] for block in _groups(history, training, size)], size)


def _metrics(groups, fit, name, predictions=None, alpha=0):
    scores = []
    for group in groups:
        actual = tuple(group['block']['sequence'])
        if name == 'frequency':
            p = probability(fit, actual)
            ranking = best_sequences(fit, 3)
            ranked_probabilities = [probability(fit, sequence) for sequence in ranking]
        else:
            distributions = (group['clubs'] if name in ('clubs', 'combined') else
                             [predictions[key] for key in group['feature_keys']])
            exponent = .5 if name == 'combined' else alpha if name == 'catboost_repeats' else 0
            model = ml._distribution(fit, distributions, exponent)
            p = ml._details(model, actual)['probability']
            ranked = ml._rank(model, 3)
            ranking = [row['sequence'] for row in ranked]
            ranked_probabilities = [row['probability'] for row in ranked]
        scores.append(dict(top1=actual == ranking[0], top3=actual in ranking, loss=-log(max(p, 1e-300)),
            top1_probability=ranked_probabilities[0], top3_probability=fsum(ranked_probabilities)))
    n = len(scores)
    return dict(tested=n, top1=sum(r['top1'] for r in scores), top3=sum(r['top3'] for r in scores),
                top1_rate=sum(r['top1'] for r in scores)/n if n else None,
                top3_rate=sum(r['top3'] for r in scores)/n if n else None,
                loss=fsum(r['loss'] for r in scores)/n if n else None,
                mean_top1_probability=fsum(r['top1_probability'] for r in scores)/n if n else None,
                mean_top3_probability=fsum(r['top3_probability'] for r in scores)/n if n else None)


def _calibration(groups, predictions, name):
    """Describe raw match marginals, with fixed bins and no fitted transform.

    A fixture can occur in repeated contexts. Count its (fixture ID, information
    cutoff) only once within this fold, period and size, never across sizes.
    Different group cutoffs are different forecasts and remain distinguishable.
    These are raw Clubs/CatBoost probabilities, not marginals of the separately
    recurrence-corrected joint distribution.
    """
    if name not in ('clubs', 'catboost_plain'):
        raise SeasonalError('Le diagnostic par match exige Clubs ou CatBoost sans correction.')
    unique = {}
    for group in groups:
        values = (group['clubs'] if name == 'clubs' else
                  [predictions[key] for key in group['feature_keys']])
        if len(values) != len(group['block']['matches']):
            raise SeasonalError('Les probabilités du diagnostic ne correspondent pas aux matchs.')
        for row, distribution in zip(group['block']['matches'], values):
            key = (str(row['match_id']), group['before'])
            actual = row['value']
            if (actual not in VALUES or set(distribution) != set(VALUES)
                    or any(type(p) not in (int, float) or not isfinite(p) or not 0 <= p <= 1
                           for p in distribution.values())
                    or not isclose(fsum(distribution.values()), 1, rel_tol=1e-9, abs_tol=1e-12)):
                raise SeasonalError('Une probabilité du diagnostic est invalide.')
            value = (actual, distribution)
            if key in unique and unique[key] != value:
                raise SeasonalError('Un même match et sa date limite ont des prévisions contradictoires.')
            unique[key] = value
    records = list(unique.values())
    n = len(records)
    reliability = {}
    for label in VALUES:
        buckets = [[] for _ in range(CALIBRATION_BINS)]
        for actual, distribution in records:
            p = distribution[label]
            buckets[min(CALIBRATION_BINS-1, int(p*CALIBRATION_BINS))].append((p, actual == label))
        reliability[label] = [dict(lower=index/CALIBRATION_BINS, upper=(index+1)/CALIBRATION_BINS,
            count=len(bucket), mean_probability=fsum(p for p, _ in bucket)/len(bucket) if bucket else None,
            observed_rate=sum(observed for _, observed in bucket)/len(bucket) if bucket else None)
            for index, bucket in enumerate(buckets)]
    return dict(matches=n, logloss=fsum(-log(max(distribution[actual], 1e-300))
                                      for actual, distribution in records)/n if n else None,
        brier=fsum(fsum((distribution[label]-int(actual == label))**2 for label in VALUES)
                   for actual, distribution in records)/n if n else None,
        brier_definition='Mean sum of squared errors across the three classes; no division by three.',
        probability_floor=1e-300, reliability=reliability,
        bin_policy='Ten fixed equal-width bins [lower, upper), with probability one in the final bin.')


def _diagnostics(validation_groups, test_groups, validation_predictions, test_predictions,
                 validation_metrics, test_metrics):
    output = dict(version=DIAGNOSTICS_VERSION, calibration={}, group_coverage={})
    for period, groups, predictions, metrics in (
            ('validation', validation_groups, validation_predictions, validation_metrics),
            ('test', test_groups, test_predictions, test_metrics)):
        output['calibration'][period] = {name: _calibration(groups, predictions, name)
                                        for name in ('clubs', 'catboost_plain')}
        output['group_coverage'][period] = {name: dict(groups=score['tested'],
            mean_top1_probability=score['mean_top1_probability'], observed_top1_rate=score['top1_rate'],
            mean_top3_probability=score['mean_top3_probability'], observed_top3_rate=score['top3_rate'])
            for name, score in metrics.items()}
    return output


def _targets(groups):
    return [dict(season=g['block']['season'], day=g['before'],
                 match_ids=[str(r['match_id']) for r in g['block']['matches']],
                 actual=list(g['block']['sequence'])) for g in groups]


def _aggregate(folds, sizes):
    output = {}
    for size in sizes:
        output[str(size)] = {}
        for name in METHODS:
            values = [fold['sizes'][str(size)]['test'][name] for fold in folds]
            n = sum(row['tested'] for row in values)
            top1, top3 = (sum(row[key] for row in values) for key in ('top1', 'top3'))
            output[str(size)][name] = dict(tested=n, top1=top1, top3=top3,
                top1_rate=top1/n if n else None, top3_rate=top3/n if n else None,
                loss=fsum(row['loss']*row['tested'] for row in values if row['tested'])/n if n else None)
    return output


@cloud_storage.persisted
def run(rows, league, source=SOURCE, sizes=DEFAULT_SIZES, progress=None, *, before=None, procedure="legacy"):
    """Evaluate fixed seasonal folds in memory, with validation-only selection.

    Returns JSON-compatible results. Saving is explicit through ``save_report``;
    deployment artifacts, provider data, and prediction journals are untouched.
    """
    if procedure not in ('legacy', 'native'):
        raise SeasonalError('Procédure de validation inconnue.')
    sizes = tuple(sizes)
    if not sizes or len(set(sizes)) != len(sizes):
        raise SeasonalError('Choisissez des tailles de groupe distinctes.')
    for size in sizes:
        validate_size(size)
    history, league, cutoff = _history(rows, league, source, before)
    planned = _plan(history, league, cutoff)
    if not planned['suitable']:
        raise SeasonalError(' '.join(planned['diagnostics']) or 'Historique par saison insuffisant.')
    requested_sizes = tuple(sorted(set(sizes) | {4}))
    report_folds, skipped = [], []
    total = len(planned['folds'])
    def announce(fold_index, fraction, message):
        if progress:
            progress((fold_index+fraction)/total, message)
    for fold_index, planned_fold in enumerate(planned['folds']):
        test_season = planned_fold['test_season']
        train_years = tuple(planned_fold['train']['seasons'])
        validation_year = planned_fold['validation']['seasons'][0]
        split = _fold_rows(history, train_years, validation_year, test_season)
        scoped_history = sorted(sum(split.values(), []), key=lambda r: (r['timestamp'], int(r['match_id'])))
        train_val = sorted(split['train']+split['validation'], key=lambda r: (r['timestamp'], int(r['match_id'])))
        def selected_groups(selected, size):
            if procedure == 'native':
                return [dict(block, season=block['matches'][0]['season'])
                        for block in ml._contained(ml._blocks(scoped_history, size),
                                                  [dict(match=row) for row in selected])]
            return _groups(history, selected, size)
        def repeat_fit(selected, size, window='all'):
            if procedure == 'native':
                return fit_sequences([block['sequence'] for block in selected_groups(selected, size)], size)
            return _repeat_fit(history, selected, size, window)
        blocks = {part: {size: selected_groups(split[part], size) for size in requested_sizes}
                  for part in ('validation', 'test')}
        if any(not blocks[part][size] for part in blocks for size in requested_sizes):
            skipped.append(f'Test {test_season} exclu : pas de groupe complet pour chaque taille demandée et la validation de taille 4.')
            continue
        announce(fold_index, .02, f'Saison {test_season} : préparation des groupes et de leurs antécédents…')
        validation_contexts = _contexts(scoped_history, blocks['validation'])
        test_contexts = _contexts(scoped_history, blocks['test'])
        selection_fit = repeat_fit(split['train'], 4)
        if procedure == 'native':
            def evaluate_native(model, alpha):
                predictions = _predict(model, {4: validation_contexts[4]})
                metric = _metrics(validation_contexts[4], selection_fit, 'catboost_repeats', predictions, alpha)
                return {'catboost': metric}, _targets(validation_contexts[4])
            native_choice, parameter_trials, selected_model, _, _ = ml.select_validation(
                lambda candidate: _fit(split['train'], candidate), evaluate_native)
            params, best_loss = native_choice['params'], native_choice['validation_loss']
        else:
            parameter_trials, selected_model, params, best_loss = [], None, None, None
            for index, candidate in enumerate(CANDIDATES):
                announce(fold_index, .12+.25*index/max(1, len(CANDIDATES)),
                         f'Saison {test_season} : validation des paramètres CatBoost {index+1}/{len(CANDIDATES)}…')
                model = _fit(split['train'], dict(candidate))
                predictions = _predict(model, {4: validation_contexts[4]})
                metric = _metrics(validation_contexts[4], selection_fit, 'catboost_plain', predictions)
                parameter_trials.append(dict(params=dict(candidate), loss=metric['loss'], tested=metric['tested']))
                if best_loss is None or metric['loss'] < best_loss-1e-12:
                    selected_model, params, best_loss = model, dict(candidate), metric['loss']
            if selected_model is None:
                raise SeasonalError('Aucun réglage CatBoost disponible.')
        validation_predictions = _predict(selected_model, validation_contexts)
        choices, validation_metrics = {}, {}
        for size in sizes:
            if procedure == 'native':
                selected = dict(alpha=native_choice['alpha'], window='all', validation_loss=best_loss)
                trials = parameter_trials
            else:
                selected, trials = None, []
                for alpha in ALPHAS:
                    for window in WINDOWS:
                        fit = repeat_fit(split['train'], size, window)
                        metric = _metrics(validation_contexts[size], fit, 'catboost_repeats', validation_predictions, alpha)
                        trials.append(dict(alpha=alpha, window=window, loss=metric['loss'], tested=metric['tested']))
                        if selected is None or metric['loss'] < selected['validation_loss']-1e-12:
                            selected = dict(alpha=alpha, window=window, validation_loss=metric['loss'])
            choices[size] = dict(selected, params=params, criterion='validation whole-group logloss',
                                 parameter_size=4, parameter_validation_loss=best_loss,
                                 parameter_trials=parameter_trials, repetition_trials=trials,
                                 tie_break=('First candidate/alpha in native order; strict loss improvement.' if procedure == 'native' else
                                            'First candidate; alpha zero first, all history first; tolerance 1e-12.'),
                                 procedure=procedure)
            all_fit = repeat_fit(split['train'], size)
            chosen_fit = repeat_fit(split['train'], size, selected['window'])
            validation_metrics[size] = {name: _metrics(validation_contexts[size],
                chosen_fit if name == 'catboost_repeats' else all_fit, name,
                validation_predictions, selected['alpha']) for name in METHODS}
        announce(fold_index, .65, f'Saison {test_season} : réajustement sur entraînement et validation…')
        frozen = _fit(train_val, params)
        test_predictions = _predict(frozen, test_contexts)
        fold_sizes = {}
        for size in sizes:
            selection = choices[size]
            all_fit = repeat_fit(train_val, size)
            chosen_fit = repeat_fit(train_val, size, selection['window'])
            metrics = {name: _metrics(test_contexts[size], chosen_fit if name == 'catboost_repeats' else all_fit,
                                      name, test_predictions, selection['alpha']) for name in METHODS}
            fold_sizes[str(size)] = dict(selection=selection, validation=validation_metrics[size], test=metrics,
                targets=dict(validation=_targets(validation_contexts[size]), test=_targets(test_contexts[size])),
                diagnostics=_diagnostics(validation_contexts[size], test_contexts[size],
                    validation_predictions, test_predictions, validation_metrics[size], metrics),
                small_test_sample=metrics['catboost_plain']['tested'] < 30,
                group_audit={name: dict(groups=len(selected_groups(selected, size)),
                    matches_outside_complete_groups=len(selected)-size*len(selected_groups(selected, size)))
                    for name, selected in split.items()},
                training_groups=dict(validation_all=repeat_fit(split['train'], size)[1],
                                     validation_selected=repeat_fit(split['train'], size, selection['window'])[1],
                                     test_all=all_fit[1], test_selected=chosen_fit[1]))
        report_folds.append(dict(test_season=test_season, partial=planned_fold['partial'],
            split={name: planned_fold[name] for name in split}, sizes=fold_sizes,
            excluded_boundary_matches=planned_fold['excluded_boundary_matches']))
        announce(fold_index, 1, f'Saison {test_season} : test terminé, sans modifier les modèles de production.')
    if not report_folds:
        raise SeasonalError(' '.join(skipped) or 'Aucune évaluation par saison possible.')
    return dict(version=VERSION, source=SOURCE, league=league, cutoff=cutoff,
        generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), fingerprint=planned['fingerprint'],
        sizes=list(sizes), folds=report_folds, seasons=planned['seasons'], procedure=procedure,
        feature_version=features.FEATURE_VERSION, implementation=_implementation(),
        aggregate=_aggregate(report_folds, sizes),
        aggregate_complete=_aggregate([f for f in report_folds if not f['partial']], sizes),
        aggregate_partial=_aggregate([f for f in report_folds if f['partial']], sizes),
        warnings=planned['diagnostics']+skipped+[
            'Évaluation rétrospective sur un historique déjà disponible ; aucune prévision prospective n’est créée.',
            'Les tailles sont évaluées séparément. Les groupes partagent des clubs et des antécédents ; petits effectifs à interpréter avec prudence.',
            'Les diagnostics de calibration sont descriptifs, sans transformation apprise ni intervalle de confiance ; '
            'les matchs partageant des clubs ou des journées ne sont pas indépendants. Brier et logloss ne mesurent pas la calibration seule.',
            'La saison courante est partielle. Les saisons antérieures ne sont pas une garantie de couverture intégrale du fournisseur.'],
        protocol=dict(parameter_selection=('Native shared joint params/alpha selection by four-match validation logloss.' if procedure == 'native' else 'CatBoost parameters: validation logloss of chronological four-match groups only.'),
            repetition_selection=('Native four-match alpha reused for every size; all training groups only.' if procedure == 'native' else 'Alpha and all/last-season window selected independently per size on validation only.'),
            candidates=[dict(p) for p in (ml.CANDIDATES if procedure == 'native' else CANDIDATES)],
            alphas=list(ml.ALPHAS if procedure == 'native' else ALPHAS), windows=['all'] if procedure == 'native' else list(WINDOWS),
            features='All members use results strictly before the first UTC day of the whole group. Earlier test days update only causal lagged features and clubs baselines.',
            grouping=('Native chronological non-overlapping blocks anchored at the first training fixture, across seasons; partition-crossing blocks excluded.' if procedure == 'native' else 'Fixed non-overlapping blocks, anchored separately at each season start; timestamp then numeric fixture ID. Groups crossing partition boundaries excluded.'),
            boundaries='Whole UTC days. Late earlier-season rows on or after the later partition first day are excluded.',
            fitting='Train for validation; selected fixed parameters refit on train+validation for test; frozen recurrence counts from the corresponding training window.',
            cohort='The five methods share exactly the same group IDs within each fold and size; no cross-size logloss pooling.',
            coverage='Only valid finished API-Football HT/fulltime regulation results. Ligue 1 includes playoffs under the same competition ID.',
            partial='Seasons starting in the current year, or the previous year before July, are conservatively partial; no guarantee of full provider coverage for older seasons.',
            diagnostics='Raw Clubs/CatBoost match marginals deduplicated by fixture ID and whole-group UTC cutoff within each fold, period and size. '
                        'Fixed ten-bin classwise reliability; mean multiclass Brier sum and match logloss. Group top1/top3 predicted mass versus observed coverage. '
                        'No learned calibration, selection, significance claim or pooling across sizes.',
            production='No deployment publication, journal writes, provider network calls, or model promotion.', test_selection=False))


def _report_path(league):
    return REPORT_DIR / f'seasonal_validation_{_league(league)}.json'


def _validate_report(report, league=None):
    if (not isinstance(report, dict) or report.get('version') != VERSION or report.get('source') != SOURCE
            or league is not None and str(report.get('league')) != _league(league)
            or not isinstance(report.get('folds'), list) or not report['folds']):
        raise SeasonalError('Rapport de validation par saison invalide ou incompatible.')
    _league(report.get('league'))
    return report


def load_report(league):
    path = _report_path(league)
    if not path.exists():
        return None
    try:
        return _validate_report(json.loads(path.read_text(encoding='utf-8')), league)
    except (OSError, ValueError, TypeError):
        raise SeasonalError('Le rapport de validation par saison est illisible ou invalide.') from None


@cloud_storage.persisted
def save_report(report):
    """Atomically persist only the explicit retrospective JSON report."""
    _validate_report(report)
    path, temporary = _report_path(report['league']), None
    encoded = _json(report)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path
