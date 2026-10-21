"""Exploratory seasonal tests of recurrence among comparable match contexts.

Validation alone chooses neighbor parameters and CatBoost settings. Test
catalogues contain only training and validation groups. No production CatBoost
model or prediction journal is written by this module.
"""
from datetime import datetime, timezone
import hashlib
import json
from math import fsum, isfinite, log
import os
from pathlib import Path
import re
import tempfile

import context_neighbors as neighbors
from coupon_sizes import validate_size
import seasonal_validation as seasonal


VERSION = 'contextual-validation-v1'
SOURCE = 'API-Football'
METHODS = ('frequency', 'clubs', 'combined', 'catboost_plain', 'context_neighbors')
DEFAULT_SIZES = (2, 4, 6, 8)
CANDIDATES = seasonal.CANDIDATES
PARAMETERS = tuple(dict(k=k, half_life_days=half_life, mix=mix)
                   for mix in (0.0, .1, .25, .5)
                   for k in (20, 50) for half_life in (None, 365))
REPORT_DIR = Path(__file__).resolve().parent / 'data' / 'api_football'
_CONFIGURATION_FIELDS = ('version', 'source', 'league', 'context_version', 'feature_version',
                         'parameters', 'selection_through', 'selection_test_season', 'selection_fingerprint')


class ContextualError(ValueError):
    """The contextual experiment or its saved configuration is unavailable."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def plan(rows, league, source=SOURCE):
    """Reuse the local API-only seasonal plan without training or writing data."""
    return seasonal.plan(rows, league, source)


def _rank_sequences(ranking):
    if not isinstance(ranking, list) or len(ranking) != 3:
        raise ContextualError('Le classement contextuel doit contenir trois scénarios.')
    return [tuple(row['sequence']) for row in ranking]


def _neighbor_metrics(catalog, groups, params):
    scores, supports = [], []
    for group in groups:
        frozen = neighbors.distribution(catalog, group, params)
        actual = tuple(group['block']['sequence'])
        p = neighbors.probability_for(frozen, actual)
        if not isfinite(p) or not 0 < p <= 1 + 1e-12:
            raise ContextualError('La distribution contextuelle contient une probabilité invalide.')
        ranked = _rank_sequences(neighbors.rank(frozen, 3))
        scores.append(dict(top1=actual == ranked[0], top3=actual in ranked,
                           loss=-log(max(p, 1e-300))))
        support = frozen['support']
        supports.append(dict(neighbors=int(support['neighbors']), ess=float(support['ess']),
                             eligible=bool(support['eligible']), eta=float(frozen['eta'])))
    n = len(scores)
    metric = dict(tested=n, top1=sum(row['top1'] for row in scores), top3=sum(row['top3'] for row in scores),
                  top1_rate=sum(row['top1'] for row in scores)/n if n else None,
                  top3_rate=sum(row['top3'] for row in scores)/n if n else None,
                  loss=fsum(row['loss'] for row in scores)/n if n else None)
    summary = dict(groups=n, eligible=sum(row['eligible'] for row in supports),
                   active_groups=sum(row['eta'] > 0 for row in supports),
                   corrected=sum(row['eta'] > 0 for row in supports),
                   mean_neighbors=fsum(row['neighbors'] for row in supports)/n if n else None,
                   mean_ess=fsum(row['ess'] for row in supports)/n if n else None,
                   min_ess=min((row['ess'] for row in supports), default=None),
                   max_ess=max((row['ess'] for row in supports), default=None),
                   mean_eta=fsum(row['eta'] for row in supports)/n if n else None)
    return metric, summary


def _aggregate(folds, sizes):
    result = {}
    for size in sizes:
        result[str(size)] = {}
        for name in METHODS:
            scores = [fold['sizes'][str(size)]['test'][name] for fold in folds]
            n = sum(row['tested'] for row in scores)
            top1, top3 = (sum(row[key] for row in scores) for key in ('top1', 'top3'))
            result[str(size)][name] = dict(tested=n, top1=top1, top3=top3,
                top1_rate=top1/n if n else None, top3_rate=top3/n if n else None,
                loss=fsum(row['loss']*row['tested'] for row in scores if row['tested'])/n if n else None)
    return result


def _configuration(folds, history, league, fingerprint):
    """Use only the latest fold's validation selection, never its test score."""
    final = max(folds, key=lambda fold: fold['split']['validation']['end'])
    configuration = dict(version=VERSION, source=SOURCE, league=league,
        context_version=getattr(neighbors, 'VERSION', 'context-neighbors-v1'),
        feature_version=seasonal.features.FEATURE_VERSION,
        parameters={size: dict(output['selection']['params']) for size, output in final['sizes'].items()},
        selection_through=final['split']['validation']['end'],
        selection_test_season=final['test_season'])
    selection_rows = [row for row in history if row['timestamp'][:10] <= configuration['selection_through']]
    configuration['selection_fingerprint'] = seasonal.ml._fingerprint(selection_rows)
    generation = hashlib.sha256(_json(configuration).encode()).hexdigest()
    return dict(configuration, generation=generation, rows=len(history),
                trained_through=history[-1]['timestamp'][:10], fingerprint=fingerprint,
                policy='Configuration chosen on the latest validation season only; inference builds a past-only rolling catalogue.',
                test_used_for_selection=False)


def run(rows, league, source=SOURCE, sizes=DEFAULT_SIZES, progress=None):
    """Return a five-method exploratory comparison on common seasonal cohorts.

    CatBoost settings use validation four-match log loss. Neighbors use their
    own validation loss for each requested size. The catalogue is frozen to
    TRAIN for validation and TRAIN+VALIDATION for test. Earlier observed test
    days update causal context features, never the catalogue or selected rules.
    """
    sizes = tuple(sizes)
    if not sizes or len(set(sizes)) != len(sizes):
        raise ContextualError('Choisissez des tailles de groupe distinctes.')
    for size in sizes:
        validate_size(size)
    history, league, cutoff = seasonal._history(rows, league, source)
    planned = seasonal._plan(history, league, cutoff)
    if not planned['suitable']:
        raise ContextualError(' '.join(planned['diagnostics']) or 'Historique par saison insuffisant.')
    requested = tuple(sorted(set(sizes) | {4}))
    folds, skipped = [], []
    total = len(planned['folds'])

    def announce(index, fraction, message):
        if progress:
            progress((index+fraction)/total, message)

    for index, planned_fold in enumerate(planned['folds']):
        test_year = planned_fold['test_season']
        train_years = tuple(planned_fold['train']['seasons'])
        validation_year = planned_fold['validation']['seasons'][0]
        split = seasonal._fold_rows(history, train_years, validation_year, test_year)
        scoped = sorted(sum(split.values(), []), key=lambda row: (row['timestamp'], int(row['match_id'])))
        train_val = sorted(split['train']+split['validation'], key=lambda row: (row['timestamp'], int(row['match_id'])))
        blocks = {part: {size: seasonal._groups(history, selected, size) for size in requested}
                  for part, selected in split.items()}
        if any(not blocks[part][size] for part in blocks for size in requested):
            skipped.append(f'Test {test_year} exclu : groupes complets insuffisants dans une période.')
            continue
        announce(index, .01, f'Saison {test_year} : construction des contextes historiques causaux…')
        contexts = {part: seasonal._contexts(scoped, by_size) for part, by_size in blocks.items()}
        validation_fit4 = seasonal._repeat_fit(history, split['train'], 4, 'all')
        selected_model, selected_params, selected_loss = None, None, None
        parameter_trials = []
        for candidate_index, candidate in enumerate(CANDIDATES):
            announce(index, .08+.18*candidate_index/max(1, len(CANDIDATES)),
                     f'Saison {test_year} : validation des paramètres CatBoost {candidate_index+1}/{len(CANDIDATES)}…')
            model = seasonal._fit(split['train'], dict(candidate))
            predictions = seasonal._predict(model, {4: contexts['validation'][4]})
            score = seasonal._metrics(contexts['validation'][4], validation_fit4, 'catboost_plain', predictions)
            parameter_trials.append(dict(params=dict(candidate), loss=score['loss'], tested=score['tested']))
            if selected_loss is None or score['loss'] < selected_loss-1e-12:
                selected_model, selected_params, selected_loss = model, dict(candidate), score['loss']
        if selected_model is None:
            raise ContextualError('Aucun réglage CatBoost disponible.')
        validation_predictions = seasonal._predict(selected_model, contexts['validation'])
        choices, val_metrics, val_support, catalogs = {}, {}, {}, {}
        for size_index, size in enumerate(sizes):
            announce(index, .30+.30*size_index/max(1, len(sizes)),
                     f'Saison {test_year} : validation des voisins sur des groupes de {size} matchs…')
            catalogue = neighbors.fit_catalog(contexts['train'][size])
            best, trials = None, []
            for params in PARAMETERS:
                score, support = _neighbor_metrics(catalogue, contexts['validation'][size], params)
                trials.append(dict(params=dict(params), loss=score['loss'], tested=score['tested'], support=support))
                if best is None or score['loss'] < best['validation_loss']-1e-12:
                    best = dict(params=dict(params), validation_loss=score['loss'])
            choices[size] = dict(best, criterion='validation whole-group logloss', trials=trials,
                catboost_params=dict(selected_params), catboost_parameter_size=4,
                catboost_validation_loss=selected_loss, catboost_trials=parameter_trials,
                tie_break='First candidate; mix zero first, then k and half-life grid order; tolerance 1e-12.')
            fit = seasonal._repeat_fit(history, split['train'], size, 'all')
            metrics = {name: seasonal._metrics(contexts['validation'][size], fit, name, validation_predictions)
                       for name in METHODS if name != 'context_neighbors'}
            metrics['context_neighbors'], val_support[size] = _neighbor_metrics(
                catalogue, contexts['validation'][size], best['params'])
            val_metrics[size] = metrics
            # Keep the test catalogue fixed; no test group is ever appended.
            catalogs[size] = neighbors.fit_catalog(contexts['train'][size]+contexts['validation'][size])
        announce(index, .65, f'Saison {test_year} : réajustement CatBoost sur entraînement et validation…')
        frozen = seasonal._fit(train_val, selected_params)
        test_predictions = seasonal._predict(frozen, contexts['test'])
        fold_sizes = {}
        for size in sizes:
            fit = seasonal._repeat_fit(history, train_val, size, 'all')
            metrics = {name: seasonal._metrics(contexts['test'][size], fit, name, test_predictions)
                       for name in METHODS if name != 'context_neighbors'}
            metrics['context_neighbors'], test_support = _neighbor_metrics(
                catalogs[size], contexts['test'][size], choices[size]['params'])
            n = len(contexts['test'][size])
            if {score['tested'] for score in metrics.values()} != {n}:
                raise ContextualError('Les méthodes doivent être comparées sur les mêmes groupes.')
            fold_sizes[str(size)] = dict(selection=choices[size], validation=val_metrics[size], test=metrics,
                targets=dict(validation=seasonal._targets(contexts['validation'][size]),
                             test=seasonal._targets(contexts['test'][size])),
                support_summary=dict(validation=val_support[size], test=test_support),
                small_test_sample=n < 30,
                group_audit={part: dict(groups=len(blocks[part][size]),
                    matches_outside_complete_groups=len(selected)-size*len(blocks[part][size]))
                    for part, selected in split.items()},
                catalogs=dict(validation_groups=len(contexts['train'][size]),
                              test_groups=len(contexts['train'][size])+len(contexts['validation'][size])))
        folds.append(dict(test_season=test_year, partial=planned_fold['partial'],
                          split={part: planned_fold[part] for part in split}, sizes=fold_sizes,
                          excluded_boundary_matches=planned_fold['excluded_boundary_matches']))
        announce(index, 1, f'Saison {test_year} : comparaison contextuelle terminée.')
    if not folds:
        raise ContextualError(' '.join(skipped) or 'Aucune évaluation contextuelle possible.')
    return dict(version=VERSION, source=SOURCE, league=league, exploratory=True,
        generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), cutoff=cutoff,
        fingerprint=planned['fingerprint'], seasons=planned['seasons'], sizes=list(sizes), folds=folds,
        implementation=dict(seasonal._implementation(), context_version=getattr(neighbors, 'VERSION', 'context-neighbors-v1')),
        aggregate=_aggregate(folds, sizes),
        aggregate_complete=_aggregate([fold for fold in folds if not fold['partial']], sizes),
        aggregate_partial=_aggregate([fold for fold in folds if fold['partial']], sizes),
        deployment=_configuration(folds, history, league, planned['fingerprint']),
        warnings=planned['diagnostics']+skipped+[
            'Comparaison exploratoire sur un historique déjà inspecté ; il ne s’agit pas d’un nouveau test indépendant.',
            'Les résultats des tailles sont séparés. De petits effectifs et des clubs communs limitent les conclusions.',
            'Le catalogue de test est figé avant le test ; les résultats du test ne choisissent ni la distance ni les réglages.',
            'Les matchs simultanés sont ordonnés par identifiant. Les groupes ne représentent pas toutes les sélections manuelles possibles.'],
        protocol=dict(parameter_selection='CatBoost parameters selected only on validation four-match group logloss.',
            neighbor_selection='k, half-life and mix selected only on validation, separately for each group size.',
            parameters=[dict(params) for params in PARAMETERS], candidates=[dict(params) for params in CANDIDATES],
            features='All group members use only results strictly before the first UTC day of the complete group.',
            catalogue='Causal TRAIN descriptors for validation; causal TRAIN+VALIDATION descriptors frozen for test. No test descriptor/outcome is added to the catalogue.',
            correction='P=(1-eta)*Q_clubs+eta*L_local; eta=mix*ESS/(ESS+20), enabled only with at least 12 neighbors and ESS>=8.',
            cohort='Five methods share the same ordered group targets within each fold and size; no pooling across sizes.',
            deployment='Only a configuration from the latest validation fold is saved; test metrics never select or promote it.',
            production='No CatBoost artifact publication, provider writes, network request, or journal mutation.',
            test_selection=False))


def _report_path(league):
    return REPORT_DIR / f'contextual_validation_{seasonal._league(league)}.json'


def _validate_report(report, league=None):
    if (not isinstance(report, dict) or report.get('version') != VERSION or report.get('source') != SOURCE
            or report.get('exploratory') is not True or not isinstance(report.get('folds'), list) or not report['folds']
            or league is not None and str(report.get('league')) != seasonal._league(league)):
        raise ContextualError('Rapport de validation contextuelle invalide ou incompatible.')
    seasonal._league(report['league'])
    config = report.get('deployment')
    if (not isinstance(config, dict) or config.get('source') != SOURCE or config.get('league') != report['league']
            or not isinstance(config.get('parameters'), dict) or not isinstance(config.get('generation'), str)):
        raise ContextualError('Configuration contextuelle absente ou incompatible.')
    try:
        if set(config['parameters']) != {str(size) for size in report['sizes']}:
            raise ValueError('Configuration size mismatch')
        for size, params in config['parameters'].items():
            validate_size(int(size))
            if params not in PARAMETERS:
                raise ValueError('Unknown configuration')
        datetime.strptime(config['selection_through'], '%Y-%m-%d')
        if not re.fullmatch(r'[0-9a-f]{64}', config['selection_fingerprint']):
            raise ValueError('Missing selection fingerprint')
        encoded = _json({key: config[key] for key in _CONFIGURATION_FIELDS})
        if config['generation'] != hashlib.sha256(encoded.encode()).hexdigest():
            raise ValueError('Invalid configuration generation')
    except (KeyError, TypeError, ValueError):
        raise ContextualError('La configuration contextuelle ou son empreinte est invalide.') from None
    return report


def load_report(league):
    path = _report_path(league)
    if not path.exists():
        return None
    try:
        return _validate_report(json.loads(path.read_text(encoding='utf-8')), league)
    except (OSError, ValueError, TypeError):
        raise ContextualError('Le rapport contextuel local est illisible ou invalide.') from None


def save_report(report):
    """Explicitly save the retrospective JSON/configuration atomically."""
    _validate_report(report)
    import model_campaign as campaign
    try:
        with campaign.manual_publication(report['league'], root=REPORT_DIR):
            return _save_report_unlocked(report)
    except campaign.CampaignError as error:
        raise ContextualError(str(error)) from None


def _save_report_unlocked(report):
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
