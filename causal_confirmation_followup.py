"""Read-only weekly coverage and a preregistered, fixed-endpoint comparison.

This eight-week follow-up is a separate protocol, not an extension of the
one-day confirmation. Its optional percentile interval is approximate and
descriptive. Eight blocks is an operational condition, not a power or coverage
guarantee; it does not change the older journal's twenty-block policy.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta, time
from hashlib import sha256
import json
from math import fsum
import random

import causal_confirmation_diagnostics as diagnostics


VERSION = 'causal-followup-v1'
CRITERIA = dict(
    version=VERSION, weeks=8, min_blocks=8, bootstrap_samples=4000,
    seed=20261102, confidence=.95, decision_hour_utc=8,
    primary='candidate_minus_clubs_paired_logloss',
    secondary=['paired_brier', 'favorite_accuracy', 'frequency_reference', 'class_calibration'],
    block='original_frozen_kickoff_complete_utc_week',
    weighting='ratio_of_summed_paired_deltas_to_summed_fixture_counts',
    interval='fixed_endpoint_percentile_week_cluster_bootstrap_approximate_descriptive',
    schedule_policy='strict_original_schedule_changes_remain_unresolved_no_recapture',
    population='admissible_captured_evaluable_fixtures_only_not_all_scheduled_fixtures',
    maturity='all_seven_censuses_anchored_with_postcommit_closure_ack_no_admissible_pending_or_unresolved_results',
    endpoint='sealed_endpoint_manifest_of_result_revisions_and_postcommit_closure_ack_at_or_before_fixed_cutoff',
    automatic_promotion=False,
    limitations=[
        'Eight weekly blocks is a new operational rule, not the older journal twenty-block rule and not a power guarantee.',
        'Percentile intervals with eight blocks can have poor coverage; independent comparable weeks are not established.',
        'Dependence can persist between weeks through clubs, competitions and shared information.',
        'The estimate is conditional on captured and evaluable matches; missing captures and missing scores can bias it.',
        'No interim interval, significance claim, durable-advantage declaration or automatic promotion is produced.',
        'The final cutoff cannot be moved after observing results; later result corrections do not enter this endpoint.',
        'A missing post-commit closure acknowledgment cannot be repaired later or qualify a week retroactively.',
        'The final interval requires the immutable endpoint selection; an unsealed read remains descriptive.',
    ],
)


def _instant(value, name):
    return diagnostics._instant(value, name)


def make_plan(start, end):
    """Describe exactly eight complete UTC weeks, before their first decision."""
    first, last = _instant(start, 'start'), _instant(end, 'end')
    if (first.time() != time() or last.time() != time() or first.weekday() != 0
            or last-first != timedelta(weeks=CRITERIA['weeks'])):
        raise ValueError('Le suivi exige huit semaines UTC complètes, à partir d’un lundi à minuit.')
    return dict(deepcopy(CRITERIA), confirmation_start=first.isoformat(),
                confirmation_end=last.isoformat(),
                result_cutoff_utc=(last+timedelta(days=7, hours=8)).isoformat())


def _same_type_and_value(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same_type_and_value(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_type_and_value(a, b) for a, b in zip(actual, expected))
    return actual == expected


def validate_plan(plan, start, end):
    expected = make_plan(start, end)
    if not _same_type_and_value(plan, expected):
        raise ValueError('Les critères du suivi de huit semaines ont changé ou sont invalides.')
    return deepcopy(plan)


def _days(protocol, start, end, rows):
    values = protocol.get('days')
    if not isinstance(values, list):
        raise ValueError('Le suivi exige les recensements quotidiens conservés.')
    indexed = {}
    for raw in values:
        if not isinstance(raw, dict) or not isinstance(raw.get('day'), str):
            raise ValueError('Un recensement quotidien est invalide.')
        day = _instant(raw['day']+'T00:00:00+00:00', 'census day')
        if day.date().isoformat() != raw['day'] or not start <= day < end or raw['day'] in indexed:
            raise ValueError('Un recensement est dupliqué ou hors de la période.')
        if raw.get('state') not in ('anchored', 'awaiting', 'missed', 'source_unavailable'):
            raise ValueError('L’état du recensement est invalide.')
        targets, captures = raw.get('targets'), raw.get('captures')
        if type(captures) is not int or captures < 0:
            raise ValueError('L’effectif des captures doit être un entier positif ou nul.')
        if targets is not None and (type(targets) is not int or targets < 0):
            raise ValueError('L’effectif des cibles recensées est invalide.')
        if raw['state'] == 'anchored' and (targets is None or captures > targets):
            raise ValueError('Les captures ne correspondent pas au recensement conservé.')
        if raw['state'] != 'anchored' and (captures or targets is not None):
            raise ValueError('Un jour non recensé ne peut annoncer des captures ou un effectif de cibles.')
        indexed[raw['day']] = deepcopy(raw)
    observed = defaultdict(int)
    for row in rows:
        observed[row['utc_day']] += 1
    for day, count in observed.items():
        if day not in indexed or indexed[day]['captures'] != count or indexed[day]['state'] != 'anchored':
            raise ValueError('Les captures par journée contredisent les prévisions conservées.')
    for day, raw in indexed.items():
        if raw['captures'] != observed[day]:
            raise ValueError('Des captures quotidiennes ne figurent pas dans le relevé des prévisions.')
    return indexed


def _closures(protocol, endpoint):
    attempts = protocol.get('attempts')
    if not isinstance(attempts, list):
        raise ValueError('Le suivi exige les événements de fermeture conservés.')
    original, acknowledgments = {}, {}
    for event in attempts:
        if not isinstance(event, dict):
            raise ValueError('Un événement de suivi est invalide.')
        kind = event.get('kind')
        if kind == 'window_closed_ack':
            closure_id = diagnostics._identifier(event.get('closure_id'), 'closure_id')
            if closure_id in acknowledgments:
                raise ValueError('Plusieurs accusés de fermeture désignent le même événement.')
            acknowledgments[closure_id] = event
            continue
        if kind != 'window_closed':
            continue
        closure_id = diagnostics._identifier(event.get('id'), 'closure id')
        if closure_id in original:
            raise ValueError('Un événement de fermeture est dupliqué.')
        stamp = _instant(event.get('observed_at_utc'), 'closure time')
        day = _instant(event.get('day', '')+'T00:00:00+00:00', 'closure day')
        if stamp < day+timedelta(hours=8, minutes=30):
            raise ValueError('La fermeture enregistrée précède la fin de la fenêtre de capture.')
        original[closure_id] = (event, stamp, day)
    result = {}
    for closure_id, (event, stamp, day) in original.items():
        ack = acknowledgments.get(closure_id)
        if ack is None:
            # Absence of a real acknowledgment is not repaired by reading the
            # ledger later; this day's closure remains timing-unqualified.
            continue
        if ack.get('day') != event['day']:
            raise ValueError('L’accusé de fermeture désigne une autre journée.')
        acknowledged = _instant(ack.get('observed_at_utc'), 'closure acknowledgment time')
        if acknowledged < stamp or acknowledged < day+timedelta(hours=8, minutes=30):
            raise ValueError('L’accusé de fermeture précède l’événement conservé ou la fin de la fenêtre.')
        if ack.get('time_quality') != 'local_closure_commit_ack' or acknowledged > endpoint:
            continue
        key = day.date().isoformat()
        if key in result:
            raise ValueError('Plusieurs fermetures attestées désignent la même journée.')
        result[key] = dict(closure_id=closure_id, observed_at_utc=stamp.isoformat(),
                           acknowledged_at_utc=acknowledged.isoformat(),
                           time_quality='local_closure_commit_ack')
    return result


def _quantile(values, probability):
    position = (len(values)-1)*probability
    lower = int(position)
    fraction = position-lower
    return values[lower] + fraction*(values[min(lower+1, len(values)-1)]-values[lower])


def _interval(blocks, plan):
    means = [delta/count for count, delta in blocks]
    if max(means)-min(means) <= 1e-14:
        return None
    generator = random.Random(plan['seed'])
    draws = []
    for _ in range(plan['bootstrap_samples']):
        sample = generator.choices(blocks, k=len(blocks))
        draws.append(fsum(delta for _, delta in sample)/sum(count for count, _ in sample))
    draws.sort()
    if draws[-1]-draws[0] <= 1e-14:
        return None
    tail = (1-plan['confidence'])/2
    return dict(lower=_quantile(draws, tail), upper=_quantile(draws, 1-tail),
                confidence=plan['confidence'], kind=plan['interval'])


def build(protocol):
    """Calculate one protocol without joining histories or changing evidence."""
    if not isinstance(protocol, dict):
        raise ValueError('Un seul protocole de suivi est requis.')
    identity = diagnostics._identifier(protocol.get('id'), 'protocol.id')
    compatible = protocol.get('implementation_compatible')
    if compatible is not None and type(compatible) is not bool:
        raise ValueError('La compatibilité de l’implémentation doit être vraie, fausse ou inconnue.')
    if protocol.get('followup') is None:
        return dict(version=VERSION, protocol_id=identity, registered=False, weeks=[],
                    implementation_compatible=compatible,
                    inference=dict(available=False, status='not_registered', confidence_interval=None,
                                   reason='Ce protocole ne possède pas de suivi de huit semaines préenregistré.'),
                    automatic_promotion=False, predictive_improvement_demonstrated=False)
    start, end = diagnostics._window(protocol)
    plan = validate_plan(protocol['followup'], start.isoformat(), end.isoformat())
    registered = _instant(protocol.get('registered_at'), 'registered_at')
    current = _instant(protocol.get('generated_at'), 'generated_at')
    if not registered < start+timedelta(hours=plan['decision_hour_utc']) or current < registered:
        raise ValueError('Le suivi doit être inscrit avant sa première décision et lu après son inscription.')
    cutoff = _instant(plan['result_cutoff_utc'], 'result_cutoff_utc')
    reported_cutoff = protocol.get('result_cutoff_utc')
    if reported_cutoff is not None and _instant(reported_cutoff, 'reported cutoff') != cutoff:
        raise ValueError('La coupure des résultats diffère de celle enregistrée.')
    if current >= cutoff and reported_cutoff is None:
        raise ValueError('Le bilan final exige une lecture des résultats à la coupure fixe enregistrée.')
    endpoint = protocol.get('endpoint')
    if endpoint is not None:
        if not isinstance(endpoint, dict):
            raise ValueError('Le manifeste du bilan final est invalide.')
        diagnostics._identifier(endpoint.get('id'), 'endpoint.id')
        prepared = _instant(endpoint.get('prepared_at'), 'endpoint.prepared_at')
        if (not cutoff <= prepared <= current
                or _instant(endpoint.get('result_cutoff_utc'), 'endpoint.result_cutoff_utc') != cutoff):
            raise ValueError('Le manifeste final ne correspond pas à la coupure enregistrée ou à son heure réelle.')
    rows = diagnostics._rows(protocol, start, end)
    days = _days(protocol, start, end, rows)
    closures = _closures(protocol, min(current, cutoff))
    indexed = defaultdict(list)
    for row in rows:
        indexed[row['utc_day']].append(row)
    weeks, blocks = [], []
    for index in range(plan['weeks']):
        first = start+timedelta(weeks=index)
        last = first+timedelta(weeks=1)
        expected = [(first+timedelta(days=offset)).date().isoformat() for offset in range(7)]
        selected = [row for day in expected for row in indexed[day]]
        paired = [row for row in selected if row['exclusion_reason'] is None]
        anchored = [day for day in expected if days.get(day, {}).get('state') == 'anchored']
        closed = [day for day in expected if day in closures]
        known_targets = sum(days[day]['targets'] for day in anchored)
        known_captures = sum(days[day]['captures'] for day in anchored)
        reasons = []
        if current < last:
            reasons.append('open_week')
        if len(anchored) != 7:
            reasons.append('unknown_census')
        if len(closed) != 7:
            reasons.append('capture_window_not_closed')
        if any(row['admissible'] and row['exclusion_reason'] in ('pending', 'unresolved') for row in selected):
            reasons.append('unresolved_results')
        if not paired:
            reasons.append('no_evaluable_results')
        eligible = not reasons
        if eligible:
            blocks.append((len(paired), fsum(row['deltas']['clubs']['logloss'] for row in paired)))
        weeks.append(dict(week_start=first.isoformat(), week_end=last.isoformat(), eligible=eligible,
                          reasons=reasons, anchored_days=len(anchored), closed_days=len(closed),
                          unknown_census_days=7-len(anchored), targets_known=known_targets,
                          captures_known=known_captures, missing_captures_known=known_targets-known_captures,
                          capture_coverage=known_captures/known_targets if known_targets else None,
                          **diagnostics._group_summary(selected)))
    paired = [row for row in rows if row['exclusion_reason'] is None]
    estimate = fsum(row['deltas']['clubs']['logloss'] for row in paired)/len(paired) if paired else None
    interval = None
    if current < cutoff:
        status = 'awaiting_fixed_endpoint'
        reason = 'Les écarts restent descriptifs ; aucun intervalle intermédiaire n’est calculé.'
    elif compatible is not True:
        status = 'incompatible_implementation' if compatible is False else 'unverified_implementation'
        reason = 'La compatibilité du code et de son environnement inscrit n’est pas vérifiée ; aucun intervalle final n’est calculé.'
    elif endpoint is None:
        status = 'awaiting_endpoint_freeze'
        reason = 'La sélection finale des résultats et accusés n’est pas encore figée ; la lecture reste descriptive.'
    elif len(blocks) < plan['min_blocks']:
        status = 'insufficient_complete_weeks'
        reason = 'Les huit semaines admissibles ne sont pas disponibles à la coupure fixe ; aucun intervalle n’est calculé.'
    else:
        interval = _interval(blocks, plan)
        status = 'approximate_descriptive_interval' if interval else 'degenerate_blocks'
        reason = ('Intervalle approximatif sur les seuls matchs capturés et évaluables ; sa couverture n’est pas garantie.'
                  if interval else 'Les écarts des blocs sont constants ; aucun intervalle nul n’est présenté comme une certitude.')
    projection = dict(protocol_id=identity, plan=plan, result_cutoff_utc=reported_cutoff,
                      endpoint=endpoint,
                      implementation_compatible=compatible,
                      rows=[{k:v for k,v in row.items() if k not in ('scores', 'deltas')} for row in rows],
                      days=days, closures=closures)
    source_hash = sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return dict(version=VERSION, protocol_id=identity, registered=True, plan=plan,
                endpoint=deepcopy(endpoint),
                implementation_compatible=compatible,
                generated_at=current.isoformat(), result_cutoff_utc=reported_cutoff,
                source_sha256=source_hash, counts=diagnostics._counts(rows), weeks=weeks,
                coverage=dict(registered_days=56, anchored_days=sum(w['anchored_days'] for w in weeks),
                    closed_days=sum(w['closed_days'] for w in weeks),
                    unknown_census_days=sum(w['unknown_census_days'] for w in weeks),
                    targets_known=sum(w['targets_known'] for w in weeks),
                    captures_known=sum(w['captures_known'] for w in weeks),
                    missing_captures_known=sum(w['missing_captures_known'] for w in weeks),
                    eligible_weeks=len(blocks), registered_weeks=plan['weeks']),
                primary=dict(metric='logloss', method='candidate', reference='clubs',
                             convention='candidate minus Clubs; negative means lower observed loss',
                             estimate=estimate, count=len(paired)),
                metrics=diagnostics._metrics(paired), paired_deltas=diagnostics._deltas(paired),
                inference=dict(available=interval is not None, status=status, reason=reason,
                    confidence_interval=interval, blocks=len(blocks), min_blocks=plan['min_blocks'],
                    fixed_result_cutoff_utc=cutoff.isoformat(), significance_tests=None),
                limitations=deepcopy(plan['limitations']),
                automatic_promotion=False, predictive_improvement_demonstrated=False)
