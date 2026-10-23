"""Pure, chronological selection-rule study, without changing daily production.

Only calibration data choose a comparable method family and fit an event
calibrator. Only selection data choose a method and threshold. Test data merely
describe that frozen choice. The calibrated event is ``one of the top three
scenarios occurred``; it is never a new probability distribution over scenarios.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
from math import exp, fsum, isfinite, log, sqrt


VERSION = 'daily-selection-statistics-v1'
PHASES = ('calibration', 'selection', 'test')
MONO_METHODS = ('frequency', 'clubs', 'combined', 'catboost', 'context_neighbors')
MIXED_METHODS = ('clubs', 'league_baseline')
SCOPES = ('calibrated_top3', 'min_match_base')
THRESHOLDS = (0., .4, .45, .5, .55, .6, .65, .7, .75, .8, .9)
MIN_GROUPS = 100
MIN_DAYS = 20
MIN_HITS = 10
MIN_MISSES = 10
MIN_COVERAGE = .10
CALIBRATION_TOLERANCE = .10
RIDGE = 1e-3
EPSILON = 1e-9
PROTOCOL = dict(
    phases=list(PHASES), family_chosen_on='calibration', calibrator_fitted_on='calibration',
    rule_chosen_on='selection', final_evaluation_on='test', minimum_groups=MIN_GROUPS,
    minimum_local_days=MIN_DAYS, minimum_calibration_hits=MIN_HITS, minimum_calibration_misses=MIN_MISSES,
    minimum_selection_coverage=MIN_COVERAGE, maximum_selection_calibration_gap=CALIBRATION_TOLERANCE,
    scopes=list(SCOPES), thresholds=list(THRESHOLDS), ridge=RIDGE,
    objective='maximum_descriptive_wilson_95_lower_bound_for_top3_rate',
    tie_break='lower_threshold_then_canonical_method_then_canonical_scope',
    calibration_event='at_least_one_of_the_three_proposed_scenarios_is_exactly_realized',
    baseline_population='same_selected_complete_groups', sizes_are_separate=True, production_changed=False)


def _number(value, label, *, probability=False, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label} doit être un nombre fini.')
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f'{label} doit être un nombre fini.') from None
    if not isfinite(value) or value < 0 or (probability and value > 1):
        raise ValueError(f'{label} est hors limites.')
    return value


def _records(values):
    records, ids, day_phases, fixture_days = [], set(), {}, {}
    for raw in values:
        try:
            group_id, day, phase, cohort, size = (raw[key] for key in ('id', 'day', 'phase', 'cohort', 'size'))
            if not isinstance(group_id, str) or not group_id or group_id in ids:
                raise ValueError('Les identifiants de groupes doivent être distincts et non vides.')
            if date.fromisoformat(day).isoformat() != day or phase not in PHASES or cohort not in ('mono', 'mixed'):
                raise ValueError('Date, phase ou cohorte invalide.')
            if type(size) is not int or not 2 <= size <= 20:
                raise ValueError('La taille du groupe doit être comprise entre 2 et 20.')
            if type(raw['complete']) is not bool:
                raise ValueError('Le statut complet doit être booléen.')
            match_ids = raw['match_ids']
            leagues = raw['leagues']
            if (not isinstance(match_ids, (list, tuple)) or len(match_ids) != size
                    or any(not isinstance(item, str) or not item for item in match_ids)
                    or len(set(match_ids)) != size):
                raise ValueError('Les identifiants de matchs doivent être distincts dans chaque groupe.')
            if (not isinstance(leagues, (list, tuple)) or not leagues
                    or any(not isinstance(item, str) or not item for item in leagues)):
                raise ValueError('Les compétitions doivent être renseignées.')
            if day in day_phases and day_phases[day] != phase:
                raise ValueError('Une journée ne peut pas appartenir à plusieurs périodes.')
            day_phases[day] = phase
            for fixture_id in match_ids:
                if fixture_id in fixture_days and fixture_days[fixture_id] != day:
                    raise ValueError('Un même match ne peut pas changer de journée ou de période.')
                fixture_days[fixture_id] = day
            methods = {}
            allowed = MONO_METHODS if cohort == 'mono' else MIXED_METHODS
            if not isinstance(raw['methods'], dict):
                raise ValueError('Les prévisions doivent être un objet par méthode.')
            for method, payload in raw['methods'].items():
                if method not in allowed:
                    raise ValueError(f'Méthode inconnue dans cette cohorte : {method}.')
                q1 = _number(payload['top1_probability'], 'Probabilité du premier scénario', probability=True)
                q3 = _number(payload['top3_probability'], 'Masse des trois scénarios', probability=True)
                confidence = _number(payload.get('min_match_confidence'), 'Confiance par match', probability=True, nullable=True)
                if q1 > q3 + 1e-12:
                    raise ValueError('La masse Top 3 doit être au moins celle du premier scénario.')
                hit1, hit3 = payload.get('top1'), payload.get('top3')
                if any(value is not None and type(value) is not bool for value in (hit1, hit3)):
                    raise ValueError('Les réalisations doivent être des booléens ou absentes.')
                if hit1 is True and hit3 is not True:
                    raise ValueError('La réalisation du premier scénario implique celle du Top 3.')
                loss = _number(payload.get('loss_per_match'), 'Perte par match', nullable=True)
                methods[method] = dict(top1_probability=q1, top3_probability=q3,
                    min_match_confidence=confidence, top1=hit1, top3=hit3, loss_per_match=loss)
            records.append(dict(id=group_id, day=day, phase=phase, cohort=cohort, size=size,
                match_ids=tuple(match_ids), leagues=tuple(sorted(set(leagues))), complete=raw['complete'], methods=methods))
            ids.add(group_id)
        except (KeyError, TypeError, OverflowError):
            raise ValueError('Un relevé de sélection est incomplet ou invalide.') from None
    ranges = [(phase, sorted(day for day, value in day_phases.items() if value == phase)) for phase in PHASES]
    previous_end = None
    for _, days in ranges:
        if days:
            if previous_end is not None and days[0] <= previous_end:
                raise ValueError('Les périodes doivent être disjointes et chronologiques : calibration, sélection, test.')
            previous_end = days[-1]
    return sorted(records, key=lambda row: (row['day'], row['cohort'], row['size'], row['id']))


def _sigmoid(z):
    if z >= 0:
        return 1/(1+exp(-z))
    value = exp(z)
    return value/(1+value)


def _logit(q):
    value = min(1-1e-6, max(1e-6, q))
    return log(value)-log(1-value)


def calibrated_probability(q, fit):
    """Probability of the Top 3 success EVENT, not a scenario probability."""
    return _sigmoid(fit['intercept']+fit['slope']*_logit(q))


def _fit(rows, method):
    observations = [(_logit(row['methods'][method]['top3_probability']), float(row['methods'][method]['top3'])) for row in rows]
    intercept, slope = 0., 1.

    def objective(a, b):
        # Stable softplus avoids overflow, even with separated observations.
        return fsum(max(a+b*x, 0)+log(1+exp(-abs(a+b*x)))-y*(a+b*x) for x, y in observations) + .5*RIDGE*(a*a+b*b)

    for iteration in range(100):
        probabilities = [(_sigmoid(intercept+slope*x), x, y) for x, y in observations]
        ga = fsum(p-y for p, _, y in probabilities)+RIDGE*intercept
        gb = fsum((p-y)*x for p, x, y in probabilities)+RIDGE*slope
        haa = fsum(p*(1-p) for p, _, _ in probabilities)+RIDGE
        hab = fsum(p*(1-p)*x for p, x, _ in probabilities)
        hbb = fsum(p*(1-p)*x*x for p, x, _ in probabilities)+RIDGE
        determinant = haa*hbb-hab*hab
        if determinant <= 0:
            break
        da, db = (hbb*ga-hab*gb)/determinant, (haa*gb-hab*ga)/determinant
        scale, before = 1., objective(intercept, slope)
        while scale > 1e-10 and objective(intercept-scale*da, slope-scale*db) > before+1e-10:
            scale *= .5
        intercept, slope = intercept-scale*da, slope-scale*db
        if max(abs(scale*da), abs(scale*db)) < 1e-9:
            break
    return dict(kind='binary_logistic_platt_top3_event', intercept=intercept, slope=slope,
        fitted_on='calibration', ridge=RIDGE, groups=len(rows), days=len({row['day'] for row in rows}),
        iterations=iteration+1, alters_joint_distribution=False)


def _wilson(hits, count):
    if not count:
        return None
    z = 1.959963984540054
    rate, denominator = hits/count, 1+z*z/count
    center = (rate+z*z/(2*count))/denominator
    half = z*sqrt(rate*(1-rate)/count+z*z/(4*count*count))/denominator
    return dict(lower=max(0., center-half), upper=min(1., center+half), kind='wilson_descriptive')


def _sample(rows, all_days=()):
    days = sorted({row['day'] for row in rows})
    return dict(groups=len(rows), days=len(days), unique_matches=len({item for row in rows for item in row['match_ids']}),
        first_day=days[0] if days else None, last_day=days[-1] if days else None,
        zero_selection_days=len(set(all_days)-set(days)))


def _probability_metrics(probabilities, hits):
    if not probabilities:
        return dict(mean_probability=None, observed_rate=None, signed_gap=None, absolute_gap=None,
            brier=None, event_logloss=None, ece_10_bins=None, reliability_bins=[])
    count = len(probabilities)
    mean, observed = fsum(probabilities)/count, sum(hits)/count
    bins = []
    for index in range(10):
        selected = [(q, y) for q, y in zip(probabilities, hits) if min(9, int(q*10)) == index]
        if selected:
            predicted, actual = fsum(q for q, _ in selected)/len(selected), sum(y for _, y in selected)/len(selected)
            bins.append(dict(lower=index/10, upper=(index+1)/10, groups=len(selected), mean_probability=predicted, observed_rate=actual))
    return dict(mean_probability=mean, observed_rate=observed, signed_gap=mean-observed, absolute_gap=abs(mean-observed),
        brier=fsum((q-y)**2 for q, y in zip(probabilities, hits))/count,
        event_logloss=-fsum(y*log(max(EPSILON, q))+(1-y)*log(max(EPSILON, 1-q)) for q, y in zip(probabilities, hits))/count,
        ece_10_bins=fsum(item['groups']*abs(item['mean_probability']-item['observed_rate']) for item in bins)/count,
        reliability_bins=bins)


def _metrics(rows, method, fit=None):
    observations = [row['methods'][method] for row in rows if row['complete'] and row['methods'][method]['top3'] is not None]
    count, hits3 = len(observations), sum(item['top3'] for item in observations)
    top1 = [item['top1'] for item in observations if item['top1'] is not None]
    losses = [item['loss_per_match'] for item in observations if item['loss_per_match'] is not None]
    raw = _probability_metrics([item['top3_probability'] for item in observations], [int(item['top3']) for item in observations])
    calibrated = (_probability_metrics([calibrated_probability(item['top3_probability'], fit) for item in observations],
        [int(item['top3']) for item in observations]) if fit else None)
    return dict(method=method, evaluated_groups=count, top1_evaluated_groups=len(top1), top1_hits=sum(top1),
        top1_rate=sum(top1)/len(top1) if top1 else None, top3_hits=hits3,
        top3_rate=hits3/count if count else None, top3_interval=_wilson(hits3, count),
        loss_evaluated_groups=len(losses), loss_per_match=fsum(losses)/len(losses) if losses else None,
        raw_top3_event=raw, calibrated_top3_event=calibrated)


def _common(rows, methods):
    common = [row for row in rows if all(method in row['methods'] for method in methods)]
    complete = [row for row in common if row['complete'] and all(row['methods'][method]['top3'] is not None for method in methods)]
    return common, complete


def _family_attempt(rows, name, methods):
    common, complete = _common(rows, methods)
    days = len({row['day'] for row in complete})
    per_method = {method: dict(hits=sum(row['methods'][method]['top3'] for row in complete),
        misses=sum(not row['methods'][method]['top3'] for row in complete)) for method in methods}
    reasons = []
    if len(complete) < MIN_GROUPS:
        reasons.append('fewer_than_100_complete_common_groups')
    if days < MIN_DAYS:
        reasons.append('fewer_than_20_local_days')
    if any(item['hits'] < MIN_HITS or item['misses'] < MIN_MISSES for item in per_method.values()):
        reasons.append('fewer_than_10_hits_or_misses_per_method')
    return dict(name=name, methods=list(methods), eligible=not reasons, common_groups=len(common),
        complete_groups=len(complete), days=days, per_method=per_method, reasons=reasons)


def _availability(phases, all_methods, family):
    result = {}
    for phase, rows in phases.items():
        item = dict(_sample(rows), complete_groups=sum(row['complete'] for row in rows), methods={})
        for method in all_methods:
            present = [row for row in rows if method in row['methods']]
            item['methods'][method] = dict(_sample(present), complete_groups=sum(row['complete'] and row['methods'][method]['top3'] is not None for row in present))
        if family:
            common, complete = _common(rows, family)
            item.update(common_groups=len(common), common_complete_groups=len(complete),
                excluded_missing_method=len(rows)-len(common), excluded_missing_outcome=len(common)-len(complete))
        result[phase] = item
    return result


def _meets(row, method, scope, threshold, fit):
    payload = row['methods'][method]
    value = (calibrated_probability(payload['top3_probability'], fit) if scope == 'calibrated_top3'
        else payload['min_match_confidence'])
    return value is not None and value+1e-12 >= threshold


def _candidate(common, complete, method, scope, threshold, fit):
    selected = [row for row in common if _meets(row, method, scope, threshold, fit)]
    evaluated = [row for row in complete if _meets(row, method, scope, threshold, fit)]
    metrics = _metrics(evaluated, method, fit)
    coverage = len(selected)/len(common) if common else None
    days = len({row['day'] for row in evaluated})
    reasons = []
    if len(evaluated) < MIN_GROUPS:
        reasons.append('fewer_than_100_complete_groups')
    if days < MIN_DAYS:
        reasons.append('fewer_than_20_local_days')
    if coverage is None or coverage+1e-12 < MIN_COVERAGE:
        reasons.append('coverage_below_10_percent')
    gap = metrics['calibrated_top3_event']['absolute_gap']
    if gap is None or gap > CALIBRATION_TOLERANCE+1e-12:
        reasons.append('calibration_gap_above_10_percentage_points')
    return dict(method=method, scope=scope, threshold=threshold, selected_groups=len(selected), evaluated_groups=len(evaluated),
        days=days, coverage=coverage, top3_rate=metrics['top3_rate'], top3_interval=metrics['top3_interval'],
        calibrated_top3_gap=gap, eligible=not reasons, reasons=reasons)


def _evaluation(rows, methods, rule, fits, baseline):
    common, complete = _common(rows, methods)
    method = rule['method']
    days = {row['day'] for row in common}
    selected = [row for row in common if _meets(row, method, rule['scope'], rule['threshold'], fits[method])]
    selected_complete = [row for row in complete if _meets(row, method, rule['scope'], rule['threshold'], fits[method])]
    selected_block = dict(sample=_sample(selected, days), evaluated_sample=_sample(selected_complete, days),
        coverage=len(selected)/len(common) if common else None, missing_outcomes=len(selected)-len(selected_complete),
        metrics=_metrics(selected_complete, method, fits[method]))
    league_rows = []
    for league in sorted({league for row in selected for league in row['leagues']}):
        selected_league = [row for row in selected if league in row['leagues']]
        complete_league = [row for row in selected_complete if league in row['leagues']]
        league_rows.append(dict(league=league, sample=_sample(selected_league), evaluated_groups=len(complete_league),
            metrics=_metrics(complete_league, method, fits[method]),
            baseline_metrics=_metrics(complete_league, baseline, fits[baseline])))
    return dict(common=dict(_sample(common), complete_groups=len(complete), excluded_missing_method=len(rows)-len(common),
            excluded_missing_outcome=len(common)-len(complete),
            methods={name: _metrics(complete, name, fits[name]) for name in methods}), selected=selected_block,
        unfiltered=dict(sample=_sample(common), evaluated_sample=_sample(complete), metrics=_metrics(complete, method, fits[method]),
            population='all_complete_common_groups_before_threshold'),
        baseline_on_selected=dict(sample=_sample(selected_complete), metrics=_metrics(selected_complete, baseline, fits[baseline]),
            population='exactly_the_same_selected_complete_groups'),
        by_league=league_rows,
        by_league_note='Un groupe mixte figure pour chacune de ses compétitions ; ces lignes ne sont pas additives.')


def _fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def fit_rules(records, protocol=None):
    """Fit on calibration/selection ONLY, suitable for an immutable rules.json.

    Test records are rejected, rather than silently discarded. This boundary
    lets the caller persist all choices before constructing test predictions.
    """
    rows = _records(records)
    if any(row['phase'] == 'test' for row in rows):
        raise ValueError('fit_rules refuse les données de test ; geler les règles avant de les lire.')
    result = dict(version=VERSION, protocol=dict(deepcopy(PROTOCOL), input_protocol=deepcopy(protocol or {})),
        fitted_input_sha256=_fingerprint(rows), by_size=[],
        warnings=[
            'Étude exploratoire de 19 tailles et plusieurs règles : aucun ajustement pour comparaisons multiples ni promotion automatique en production.',
            'Les groupes partagent des matchs et des journées. Les intervalles de Wilson sont descriptifs et supposent une indépendance qui peut être violée.',
            'La calibration porte sur la réalisation du Top 3 ; elle ne modifie ni les scénarios, ni leurs masses brutes, ni la distribution jointe.',
            'Les résultats absents ne sont pas des échecs. La couverture compte les groupes sélectionnés avant exclusion des résultats absents.',
            'Le seuil par match porte sur le minimum des meilleures probabilités brutes individuelles ; il ne constitue pas un seuil du scénario complet.',
            'Le test final est descriptif pour la règle figée. Le relire pour modifier la règle demanderait une nouvelle période de test.',
        ])
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['cohort'], row['size'])].append(row)
    for cohort in ('mono', 'mixed'):
        all_methods = MONO_METHODS if cohort == 'mono' else MIXED_METHODS
        families = [('all_five', MONO_METHODS), ('statistical_three', MONO_METHODS[:3])] if cohort == 'mono' else [('mixed_two', MIXED_METHODS)]
        for size in range(2, 21):
            group = grouped[(cohort, size)]
            phases = {phase: [row for row in group if row['phase'] == phase] for phase in PHASES}
            attempts = [_family_attempt(phases['calibration'], name, methods) for name, methods in families]
            chosen = next((item for item in attempts if item['eligible']), None)
            methods = tuple(chosen['methods']) if chosen else ()
            entry = dict(cohort=cohort, size=size, status='insufficient_calibration',
                family=dict(name=chosen['name'] if chosen else None, methods=list(methods), chosen_from='calibration', attempts=attempts),
                availability=_availability(phases, all_methods, methods), calibration={}, selection=None, rule=None, test=None)
            result['by_size'].append(entry)
            if not chosen:
                continue
            _, complete_calibration = _common(phases['calibration'], methods)
            fits = {method: _fit(complete_calibration, method) for method in methods}
            entry['calibration'] = {method: dict(fit=fits[method], sample=_sample(complete_calibration),
                descriptive_metrics=_metrics(complete_calibration, method, fits[method])) for method in methods}
            common, complete = _common(phases['selection'], methods)
            candidates = [_candidate(common, complete, method, scope, threshold, fits[method])
                for method in methods for scope in SCOPES for threshold in THRESHOLDS]
            eligible = [item for item in candidates if item['eligible']]
            entry['selection'] = dict(common=dict(_sample(common), complete_groups=len(complete),
                excluded_missing_method=len(phases['selection'])-len(common), excluded_missing_outcome=len(common)-len(complete)),
                candidates=candidates, eligible_candidates=len(eligible))
            if not eligible:
                entry['status'] = 'insufficient_selection'
                continue
            best = max(eligible, key=lambda item: (item['top3_interval']['lower'], -item['threshold'],
                -methods.index(item['method']), -SCOPES.index(item['scope'])))
            rule = dict(method=best['method'], scope=best['scope'], threshold=best['threshold'],
                calibrator=dict(fits[best['method']]), chosen_on='selection', family=chosen['name'],
                selection_basis=dict(best), no_filtering_evidence=best['threshold'] == 0,
                selection_is_exploratory=True, production_changed=False)
            entry['rule'] = rule
            baseline = 'frequency' if cohort == 'mono' else 'league_baseline'
            entry['selection']['evaluation'] = _evaluation(phases['selection'], methods, rule, fits, baseline)
            entry['status'] = 'awaiting_test'
    return result


def evaluate_rules(records, frozen, protocol=None):
    """Apply already frozen families, calibrators and rules without fitting.

    If calibration/selection records are supplied, verify they are exactly the
    records used for freezing. Test-only records may also be supplied. No test
    outcome or probability can change a saved rule or calibrator.
    """
    rows = _records(records)
    if not isinstance(frozen, dict) or frozen.get('version') != VERSION:
        raise ValueError('Règles gelées absentes ou version incompatible.')
    frozen_protocol = frozen.get('protocol', {})
    if {key: value for key, value in frozen_protocol.items() if key != 'input_protocol'} != PROTOCOL:
        raise ValueError('Le protocole statistique gelé est incompatible.')
    if protocol is not None and protocol != frozen_protocol.get('input_protocol'):
        raise ValueError('Le protocole fourni diffère du protocole gelé.')
    pretest = [row for row in rows if row['phase'] != 'test']
    if pretest and _fingerprint(pretest) != frozen.get('fitted_input_sha256'):
        raise ValueError('Les données de calibration/sélection ont changé depuis le gel des règles.')
    result = deepcopy(frozen)
    result['frozen_rules_sha256'] = _fingerprint(frozen)
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['cohort'], row['size'])].append(row)
    for entry in result['by_size']:
        cohort, size = entry['cohort'], entry['size']
        group = grouped[(cohort, size)]
        phases = {phase: [row for row in group if row['phase'] == phase] for phase in PHASES}
        methods = tuple(entry['family']['methods'])
        all_methods = MONO_METHODS if cohort == 'mono' else MIXED_METHODS
        availability = _availability(phases, all_methods, methods)
        if pretest:
            entry['availability'] = availability
        else:
            entry['availability']['test'] = availability['test']
        if entry['rule'] is None:
            continue
        fits = {method: entry['calibration'][method]['fit'] for method in methods}
        if entry['rule']['calibrator'] != fits[entry['rule']['method']]:
            raise ValueError('Le calibrateur de la règle diffère du calibrateur gelé.')
        baseline = 'frequency' if cohort == 'mono' else 'league_baseline'
        entry['test'] = _evaluation(phases['test'], methods, entry['rule'], fits, baseline)
        entry['status'] = 'evaluated' if entry['test']['selected']['metrics']['evaluated_groups'] else 'awaiting_test'
    return result


def analyze(records, protocol=None):
    """Convenience pure study; persist fit_rules separately for a real audit.

    Rates are fractions in [0, 1]; absent rates are None, never zero. No overall
    accuracy combines sizes. Original inputs and frozen rules are not mutated.
    """
    values = list(records)
    # Validate all periods together before fitting; this checks temporal overlap.
    normalized = _records(values)
    frozen = fit_rules([row for row in normalized if row['phase'] != 'test'], protocol)
    return evaluate_rules(normalized, frozen, protocol)
