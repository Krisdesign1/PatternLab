"""Pure chronological daily selection study; never reads or publishes artifacts.

All fitted weights, recurrence counts and neighbor catalogs use TRAIN only.
Causal club summaries and lagged target features may incorporate earlier played
UTC days. Daily identities are retrospective reconstructions, not saved slates.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
from math import fsum, log, prod

import api_football as api
import api_match_workspace as workspace
import catboost_scenarios as ml
import context_neighbors as neighbors
import contextual_scenarios as combined
from coupon_sizes import top_independent
import daily_analysis_engine as engine
from daily_analysis_evaluation import HistoryIndex, ZONE, construct, _order, _available
from league_model import VALUES
from league_patterns import repeated_scenarios
import scenario_features as features
from scenario_predictor import MIN_TRAIN, best_sequences, fit_sequences, probability

VERSION = 'daily-selection-methods-replay-v1'
METHODS = ('frequency', 'clubs', 'combined', 'catboost', 'context_neighbors')
PROTOCOL = dict(
    train_start='2022-01-01', train_end='2025-01-01',
    calibration_start='2025-01-01', calibration_end='2025-07-01',
    selection_start='2025-07-01', selection_end='2026-01-01',
    test_start='2026-01-01', sizes=list(range(2, 21)), min_train_examples=100,
    catboost_parameters=dict(ml.CANDIDATES[0]),
    context_parameters=dict(k=20, half_life_days=None, mix=.25),
    recurrence_min_groups=MIN_TRAIN, recurrence_prior_weight=10.,
    combined_exponent=.5, catboost_recurrence_exponent=0.,
    mono_grouping='chronological-disjoint-local-day-per-league-and-size',
    mixed_grouping=engine.VERSION, qualification='TRAIN count only; no later coverage qualification',
    calendar='final identities reconstructed; missing outcomes retain their original group',
    chronology='local phases; fit excludes whole boundary UTC day; all features strictly before first UTC day',
    status='exploratory retrospective study; historical data previously inspected; not a new blind test')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _date(value):
    return value if type(value) is date else date.fromisoformat(value)


def _configuration(protocol, today):
    result = deepcopy(PROTOCOL)
    if protocol is not None:
        if not isinstance(protocol, dict) or set(protocol)-set(PROTOCOL)-{'end', 'training_utc_before'}:
            raise ValueError('Protocole de sélection inconnu.')
        result.update(deepcopy(protocol))
    # Parameters are fixed families, not an arbitrary path to target-label tuning.
    if (result['sizes'] != list(range(2, 21)) or result['min_train_examples'] != 100
            or result['catboost_parameters'] != dict(ml.CANDIDATES[0])
            or result['context_parameters'] != dict(k=20, half_life_days=None, mix=.25)
            or result['recurrence_min_groups'] != MIN_TRAIN
            or result['recurrence_prior_weight'] != 10. or result['combined_exponent'] != .5
            or result['catboost_recurrence_exponent'] != 0.):
        raise ValueError('Les familles et hyperparamètres de ce rejeu sont figés.')
    fields = ('train_start', 'train_end', 'calibration_start', 'calibration_end',
              'selection_start', 'selection_end', 'test_start')
    for field in fields: result[field] = _date(result[field]).isoformat()
    if not (result['train_start'] < result['train_end'] == result['calibration_start']
            < result['calibration_end'] == result['selection_start']
            < result['selection_end'] == result['test_start'] < today.isoformat()):
        raise ValueError('Les quatre périodes doivent être chronologiques, contiguës et antérieures à aujourd’hui.')
    end = _date(result.get('end', today))
    if not _date(result['test_start']) < end <= today:
        raise ValueError('La fin exclusive du test ne peut dépasser aujourd’hui.')
    result['end'] = end.isoformat()
    result['training_utc_before'] = datetime.combine(_date(result['train_end']), time.min, tzinfo=ZONE).astimezone(timezone.utc).date().isoformat()
    return result


def _phase(day, protocol):
    value = day.isoformat() if type(day) is date else day
    for name in ('calibration', 'selection', 'test'):
        end = protocol['end'] if name == 'test' else protocol[name+'_end']
        if protocol[name+'_start'] <= value < end: return name
    return None


def _normalize(raw, today, protocol):
    unique = {}
    for item in raw:
        if not isinstance(item, dict) or item.get('provider') not in (None, 'api-football', 'API-Football'):
            raise ValueError('Seules les données API-Football sont acceptées.')
        try: identity = api._numeric_id(item['fixture']['id'], 'du match')
        except (KeyError, TypeError, ValueError): continue
        if identity in unique and unique[identity] != item:
            raise ValueError('Identifiant de match contradictoire dans le rejeu.')
        unique[identity] = item
    history, outcomes, calendars = [], {}, defaultdict(list)
    invalid, missing, years = 0, 0, Counter()
    for identity, item in sorted(unique.items(), key=lambda pair: int(pair[0])):
        try:
            fixture = workspace.fixture_from_raw(item)
            stamp = workspace._stamp(item)
            day = stamp.astimezone(ZONE).date()
        except (KeyError, TypeError, ValueError, AttributeError):
            invalid += 1
            continue
        if day >= today: continue
        if _phase(day, protocol) and stamp > datetime.combine(day, time.min, tzinfo=ZONE).astimezone(timezone.utc):
            calendars[day].append(fixture)
        if api._status_short(item) in api.FINISHED_STATUSES:
            try:
                row = api.normalize_fixture(item)
                history.append(row); outcomes[identity] = row; years[str(day.year)] += 1
            except (ValueError, TypeError, KeyError): missing += 1
    history.sort(key=_order)
    inventory = dict(raw_fixtures=len(unique), valid_past_results=len(history), invalid_identities=invalid,
                     missing_finished_scores=missing, calendar_days=len(calendars),
                     calendar_matches=sum(map(len, calendars.values())), outcomes_by_year=dict(sorted(years.items())))
    return history, outcomes, calendars, inventory


class _ContextBuilder:
    """Causal incremental features and Clubs summaries shared across group sizes."""
    def __init__(self, history, league):
        _, self.prepared = features._scoped_rows(history, league, 'api-football')
        self.state, self.pointer, self.before = features._History(), 0, None
        self.clubs = HistoryIndex(history)
        self.cache = {}

    def advance(self, before):
        if self.before is not None and before < self.before:
            raise ValueError('Les contextes doivent avancer dans le temps.')
        while self.pointer < len(self.prepared) and self.prepared[self.pointer]['stamp'].date().isoformat() < before:
            self.state.update(self.prepared[self.pointer]); self.pointer += 1
        self.clubs.advance(before); self.before = before

    def context(self, group):
        before = min(row['timestamp'][:10] for row in group)
        self.advance(before)
        values, distributions = [], []
        for row in group:
            key = (row['match_id'], before)
            if key not in self.cache:
                item = features._metadata(row, row['league'], 'api-football', require_id=True)
                self.cache[key] = features._features(self.state, item)
            values.append(self.cache[key])
            distributions.append(self.clubs.estimate(row)['probabilities'])
        return dict(block=dict(matches=group), before=before, features=values, clubs=distributions)


def _train_groups(rows, size):
    output = []
    by_season = defaultdict(list)
    for row in rows: by_season[row['season']].append(row)
    for season in sorted(by_season):
        members = by_season[season]
        output.extend(repeated_scenarios(members, size)[members[0]['league']]['blocks'])
    return sorted(output, key=lambda block: _order(block['matches'][0]))


def _fit_league(history, train, league, protocol):
    """Fit every learnable object solely on the predetermined training rows."""
    train_ids = {row['match_id'] for row in train}
    examples = [item for item in features.build_examples(history, league, 'api-football')
                if item['match']['match_id'] in train_ids]
    model, error = None, None
    if len(examples) >= protocol['min_train_examples'] and {item['label'] for item in examples} == set(VALUES):
        try:
            model = ml._fit(examples, protocol['catboost_parameters'], list(features.FEATURE_COLUMNS), list(features.CATEGORICAL_COLUMNS))
        except ml.ModelError as exc: error = str(exc)
    else: error = 'TRAIN doit contenir au moins 100 exemples et les trois classes.'
    blocks = {size: _train_groups(train, size) for size in protocol['sizes']}
    fits = {size: fit_sequences([block['sequence'] for block in groups], size)
            for size, groups in blocks.items() if len(groups) >= MIN_TRAIN}
    builder = _ContextBuilder(history, league)
    pending = sorted(((min(row['timestamp'][:10] for row in block['matches']), size, i, block)
                      for size, groups in blocks.items() for i, block in enumerate(groups)), key=lambda item: item[:3])
    contexts = defaultdict(list)
    for _, size, _, block in pending:
        context = builder.context(block['matches']); context['block'] = block
        contexts[size].append(context)
    catalogs = {size: neighbors.fit_catalog(groups) for size, groups in contexts.items() if groups}
    detail = dict(league=league, training_matches=len(train), training_examples=len(examples),
                  training_classes=sorted({item['label'] for item in examples}),
                  training_max_utc_day=max((row['timestamp'][:10] for row in train), default=None),
                  training_ids_fingerprint=sha256(_json(sorted(train_ids, key=int)).encode()).hexdigest(),
                  catboost_available=model is not None, catboost_error=error,
                  recurrence_groups={str(size): len(groups) for size, groups in blocks.items()},
                  context_catalog_groups={str(size): len(catalogs.get(size, ())) for size in protocol['sizes']},
                  context_neighbor_supported=0, context_fallback_to_clubs=0)
    return dict(model=model, fits=fits, catalogs=catalogs, detail=detail)


def _independent(distributions):
    ranking = top_independent(distributions, 3)
    return ranking, lambda sequence: prod(p[value] for p, value in zip(distributions, sequence))


def _metric(ranking, probability_for, distributions, actual):
    order = [tuple(row['sequence']) for row in ranking]
    complete = actual is not None
    return dict(top1_probability=float(ranking[0]['probability']),
                top3_probability=min(1., fsum(row['probability'] for row in ranking)),
                min_match_confidence=min(max(row.values()) for row in distributions) if distributions else None,
                top1=bool(actual == order[0]) if complete else None,
                top3=bool(actual in order) if complete else None,
                loss_per_match=-log(max(probability_for(actual), 1e-300))/len(actual) if complete else None)


def _record(group, day, phase, cohort, outcomes, methods):
    ids = [row['match_id'] for row in group]
    complete = all(mid in outcomes for mid in ids)
    return dict(id=sha256(_json([VERSION, cohort, day, ids]).encode()).hexdigest(),
                day=day, phase=phase, cohort=cohort, size=len(group), match_ids=ids,
                leagues=sorted({row['league'] for row in group}, key=int),
                methods=methods, complete=complete)


def _actual(group, outcomes):
    return tuple(outcomes[row['match_id']]['value'] for row in group) if all(row['match_id'] in outcomes for row in group) else None


def _mono_methods(group, context, fitted, actual, cb, protocol):
    distributions = context['clubs']; methods = {}
    ranking, function = _independent(distributions)
    methods['clubs'] = _metric(ranking, function, distributions, actual)
    fit = fitted['fits'].get(len(group))
    if fit is not None:
        ranking = [dict(sequence=sequence, probability=probability(fit, sequence)) for sequence in best_sequences(fit, 3)]
        methods['frequency'] = _metric(ranking, lambda sequence: probability(fit, sequence), fit[2], actual)
        model = combined._combined_model(fit, distributions)
        methods['combined'] = _metric(combined._ranking(model, 3),
            lambda sequence: combined._sequence_details(model, sequence)['probability'], distributions, actual)
    if cb is not None:
        ranking, function = _independent(cb)
        methods['catboost'] = _metric(ranking, function, cb, actual)
    catalog = fitted['catalogs'].get(len(group))
    if catalog:
        value = neighbors.distribution(catalog, context, protocol['context_parameters'])
        methods['context_neighbors'] = _metric(value['ranking'], lambda sequence: neighbors.probability_for(value, sequence), distributions, actual)
        support = value['support']
        methods['context_neighbors']['neighbor_supported'] = support['eligible']
        methods['context_neighbors']['neighbors'] = support['neighbors']
        methods['context_neighbors']['effective_neighbors'] = support['ess']
        fitted['detail']['context_neighbor_supported' if support['eligible'] else 'context_fallback_to_clubs'] += 1
    return methods


def replay(raw, *, today, protocol=None, progress=None, before_test=None):
    """Reconstruct daily groups and emit frozen-family predictions, without I/O."""
    today = _date(today)
    protocol = _configuration(protocol, today)
    history, outcomes, calendars, inventory = _normalize(raw, today, protocol)
    by_league, train = defaultdict(list), defaultdict(list)
    for row in history:
        by_league[row['league']].append(row)
        local_day = features._stamp(row['timestamp']).astimezone(ZONE).date().isoformat()
        if protocol['train_start'] <= local_day < protocol['train_end'] and row['timestamp'][:10] < protocol['training_utc_before']:
            train[row['league']].append(row)
    qualified = sorted((league for league, rows in train.items() if len(rows) >= protocol['min_train_examples']), key=int)
    inventory['qualified_leagues_train_only'] = qualified
    inventory['train_matches_by_league'] = {league: len(train[league]) for league in sorted(train, key=int)}
    support_index = HistoryIndex(history)
    mono_groups, mixed_groups, phase_counts = defaultdict(list), [], Counter()
    eligible_matches, ineligible_matches = 0, 0
    for number, day in enumerate(sorted(calendars), 1):
        slate = sorted(calendars[day], key=_order)
        before = min(row['timestamp'][:10] for row in slate)
        support_index.advance(before)
        supports = {row['match_id']: engine._support(row, support_index.leagues) for row in slate}
        eligible = [row for row in slate if supports[row['match_id']]['eligible']]
        eligible_matches += len(eligible); ineligible_matches += len(slate)-len(eligible)
        phase = _phase(day, protocol); phase_counts[phase] += len(slate)
        leagues = defaultdict(list)
        for row in eligible:
            if row['league'] in qualified: leagues[row['league']].append(row)
        for league in sorted(leagues, key=int):
            members = leagues[league]
            for size in protocol['sizes']:
                for start in range(0, len(members)-size+1, size):
                    mono_groups[league].append((day.isoformat(), phase, members[start:start+size]))
        for group in construct(eligible, supports):
            if len({row['league'] for row in group}) > 1:
                mixed_groups.append((day.isoformat(), phase, group))
        if progress and number % 100 == 0:
            progress(dict(stage='calendars', completed=number, total=len(calendars)))
    inventory.update(eligible_calendar_matches=eligible_matches, ineligible_calendar_matches=ineligible_matches,
                     phase_calendar_matches=dict(phase_counts), mono_groups=sum(map(len, mono_groups.values())), mixed_groups=len(mixed_groups))
    records, details, test_tasks = [], [], []
    for number, league in enumerate(qualified, 1):
        if progress: progress(dict(stage='training', completed=number-1, total=len(qualified), league=league))
        fitted = _fit_league(by_league[league], train[league], league, protocol)
        pending = sorted(mono_groups[league], key=lambda item: (min(row['timestamp'][:10] for row in item[2]), item[0], len(item[2]), tuple(int(row['match_id']) for row in item[2])))
        builder = _ContextBuilder(by_league[league], league)
        contexts = [builder.context(group) for _, _, group in pending]
        unique = {}
        for context in contexts:
            for row, value in zip(context['block']['matches'], context['features']):
                unique[(row['match_id'], context['before'])] = value
        predictions = {}
        if fitted['model'] is not None and unique:
            probabilities = ml._predict(fitted['model'], list(unique.values()), list(features.FEATURE_COLUMNS), list(features.CATEGORICAL_COLUMNS))
            predictions = dict(zip(unique, probabilities))
        for (day, phase, group), context in zip(pending, contexts):
            cb = [predictions[(row['match_id'], context['before'])] for row in group] if predictions else None
            if phase == 'test':
                test_tasks.append(('mono', day, group, context, fitted, cb))
                continue
            methods = _mono_methods(group, context, fitted, _actual(group, outcomes), cb, protocol)
            records.append(_record(group, day, phase, 'mono', outcomes, methods))
        fitted['detail']['phase_groups'] = dict(Counter(row[1] for row in pending))
        details.append(fitted['detail'])
        if progress: progress(dict(stage='training', completed=number, total=len(qualified), league=league))
    # The mixed cohort deliberately has only the two coherent independent models.
    mixed_index = HistoryIndex(history)
    for day, phase, group in sorted(mixed_groups, key=lambda item: (min(row['timestamp'][:10] for row in item[2]), item[0], len(item[2]), tuple(int(row['match_id']) for row in item[2]))):
        if phase == 'test':
            test_tasks.append(('mixed', day, group, None, None, None))
            continue
        mixed_index.advance(min(row['timestamp'][:10] for row in group))
        estimates = [mixed_index.estimate(row) for row in group]
        actual = _actual(group, outcomes); methods = {}
        for name, key in (('clubs', 'probabilities'), ('league_baseline', 'baseline')):
            if name == 'clubs' and not _available(group, mixed_index): continue
            distributions = [item[key] for item in estimates]
            ranking, function = _independent(distributions)
            methods[name] = _metric(ranking, function, distributions, actual)
        records.append(_record(group, day, phase, 'mixed', outcomes, methods))
    # Only pre-test records are passed out; selection is committed before any
    # test correctness, top-k membership or loss is evaluated. The callback's
    # return value cannot change any frozen model or grouping parameters.
    if before_test is not None:
        before_test(deepcopy(sorted(records, key=lambda row: (row['day'], row['cohort'], row['size'], row['id']))))
    for cohort, day, group, context, fitted, cb in sorted(test_tasks, key=lambda item: (min(row['timestamp'][:10] for row in item[2]), item[0], item[1], len(item[2]))):
        actual = _actual(group, outcomes)
        if cohort == 'mono':
            methods = _mono_methods(group, context, fitted, actual, cb, protocol)
        else:
            mixed_index.advance(min(row['timestamp'][:10] for row in group))
            estimates = [mixed_index.estimate(row) for row in group]
            methods = {}
            for name, key in (('clubs', 'probabilities'), ('league_baseline', 'baseline')):
                if name == 'clubs' and not _available(group, mixed_index): continue
                distributions = [item[key] for item in estimates]
                ranking, function = _independent(distributions)
                methods[name] = _metric(ranking, function, distributions, actual)
        records.append(_record(group, day, 'test', cohort, outcomes, methods))
    records.sort(key=lambda row: (row['day'], row['cohort'], row['size'], tuple(map(int, row['match_ids']))))
    inventory['incomplete_groups'] = sum(not row['complete'] for row in records)
    inventory['method_availability'] = dict(Counter(name for row in records for name in row['methods']))
    if progress: progress(dict(stage='complete', records=len(records)))
    return dict(version=VERSION, protocol=protocol, inventory=inventory, records=records, method_details=details,
                warnings=[
                    'Calendriers historiques reconstruits après coup : des affiches absentes à la date réelle ne peuvent être retrouvées.',
                    'Périodes et données déjà explorées : le test 2026 est un contrôle rétrospectif, pas une preuve prospective indépendante.',
                    'Les tailles et groupes se recouvrent ; aucune précision globale ni hypothèse d’indépendance entre groupes.',
                    'Probabilités jointes indépendantes pour Clubs, CatBoost et la référence de ligue ; corrélations intermatchs non estimées.',
                    'Frequency/Combined et catalogues de contextes figés sur TRAIN ; profils Clubs et caractéristiques autorisés à suivre les jours UTC antérieurs.',
                    'Contextes avec trop peu de voisins retombent explicitement sur Clubs ; cet accord n’est pas une confirmation indépendante.',
                    'Clubs mixte absent si une composante de plusieurs matchs manque des 20 groupes historiques exigés par la production.',
                    'Le seuil par match de Frequency utilise ses distributions historiques par position, pas les marginales corrigées du modèle joint.',
                    'Les groupes incomplets sont conservés avec résultats null ; aucune recomposition après connaissance des scores.'])
