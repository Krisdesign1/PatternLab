"""Chronological probability-threshold policy; never tunes on its test period.

F0.5 balances set coverage against the number of mutually exclusive proposals,
favouring per-proposal precision. It is a decision rule, not a model refit or a
promise of predictive advantage. Entire match days and disjoint match IDs are
used; the historical Top3 definition is unchanged.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from math import nextafter
import re
from time import monotonic
from zoneinfo import ZoneInfo

import prospective_models as models
from adaptive_scenario_selection import iter_selected

VERSION = 'scenario-threshold-f05-v1'
MIN_TUNING = 30
MIN_TEST = 20
MIN_DAYS = 3
LOCAL_ZONE = ZoneInfo('Africa/Kinshasa')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return sha256(_json(value).encode()).hexdigest()


def stamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Une heure de disponibilité avec fuseau est nécessaire.')
    return parsed.astimezone(timezone.utc)


def _match_id(value):
    """Canonical API identifiers, without coercing bools or fractional IDs."""
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
        return value
    raise ValueError('Identifiant API-Football invalide.')


def _kickoff(value):
    # Existing frozen fixtures intentionally use naive UTC timestamps.
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _algorithm_signature(method):
    """Identify algorithms/configurations, not fixture or competition hashes.

    Mixed generation hashes the competition-keyed component_versions mapping;
    treating it as a fitted-model ID fragments comparable groups almost one by
    one. Its actual leaf model/configuration identities remain in this recursive
    signature, including block sizes. League codes and component positions do
    not identify a different algorithm.
    """
    distribution = method.get('distribution') or {}
    signature = dict(version=method.get('version'),
        distribution_type=distribution.get('type'),
        model_sha256=method.get('model_sha256'),
        feature_version=method.get('feature_version'),
        descriptor_version=method.get('descriptor_version'))
    if distribution.get('type') == 'block_product':
        blocks = [dict(size=len(block.get('indices') or []),
                       algorithm=_algorithm_signature(block.get('method') or {}))
                  for block in distribution.get('components') or []]
        signature['components'] = sorted(blocks, key=_json)
    else:
        # For non-mixed methods, generation identifies a learned artifact or a
        # selected context configuration, and must not be pooled across versions.
        signature.update(generation=method.get('generation'),parameters=method.get('parameters'),
                         alpha=method.get('alpha'))
    return signature


def context_key(name, method, size):
    return digest(dict(method=name, size=size, algorithm=_algorithm_signature(method)))


def _observation(entry, before):
    events = entry.get('result_history') or []
    if not events and entry.get('checked_at'):
        events = [entry]
    eligible = []
    last_kickoff = max(_kickoff(row['timestamp']) for row in entry['fixtures'])
    for order, event in enumerate(events):
        try:
            checked = stamp(event['checked_at'])
            if last_kickoff < checked < before:
                eligible.append((checked, order, event))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    if not eligible:
        return None
    checked, _, event = max(eligible, key=lambda pair: (pair[0], pair[1]))
    actual = event.get('actual')
    if (event.get('status') != 'settled' or not isinstance(actual, list) or len(actual) != entry['size']
            or any(value not in models.VALUES for value in actual)):
        return None
    return actual, checked.isoformat()


def _metric(rows, threshold=None):
    hits = proposals = abstentions = universal = 0
    sizes = []
    for row in rows:
        if threshold is None:
            count, hit = 3, row['top3']
        else:
            count = bisect_right(row['negative_probabilities'], -threshold)
            if count == row['universe']:
                count = 0
                universal += 1
            hit = bool(count and row['true_probability'] >= threshold)
        hits += bool(hit); proposals += count; abstentions += count == 0; sizes.append(count)
    n = len(rows)
    return dict(groups=n, hits=hits, proposals=proposals, coverage=hits/n if n else 0,
        precision=hits/proposals if proposals else 0,
        f05=1.25*hits/(.25*n+proposals) if .25*n+proposals else 0,
        mean_proposals=proposals/n if n else 0, maximum_proposals=max(sizes, default=0),
        abstentions=abstentions, uninformative_groups=universal)


def _prepare(rows, threshold, deadline, check_time):
    output, byte_count = [], 0
    def check():
        if check_time is not None:
            check_time()
        if monotonic() >= deadline:
            raise TimeoutError('Calibration trop volumineuse : aucun seuil de ce contexte publié.')
    for row in rows:
        check()
        chances = []
        for item in iter_selected(row['method'], threshold, check_time=check):
            chances.append(-item['probability']); byte_count += 32
            if byte_count > 64*1024*1024:
                raise MemoryError('Calibration trop volumineuse : aucun seuil de ce contexte publié.')
        actual = row['actual']
        output.append(dict(negative_probabilities=chances,
            true_probability=models.probability_for_sequence(row['method'], actual),
            top3=any(item['sequence'] == actual for item in row['method']['ranking']),
            universe=3**row['size']))
    return output


def fit(entries, *, as_of, check_time=None):
    """Learn a cutoff on earlier days, then test it unchanged on later days.

    A forecast is never picked because it won: overlap removal precedes result
    availability filtering. Tuning labels must already be known before the very
    first test forecast, and all evaluation labels must precede ``as_of``.
    """
    as_of = stamp(as_of)
    grouped, excluded = defaultdict(list), Counter()
    for entry in entries:
        try:
            if entry.get('source') != 'API-Football':
                raise ValueError('source')
            size = models.validate_size(entry['size'])
            if not isinstance(entry.get('id'),str) or not entry['id']:
                raise ValueError('forecast_id')
            fixtures = entry['fixtures']
            if len(fixtures) != size:
                raise ValueError('fixtures')
            created = stamp(entry['created_at'])
            kickoffs = [_kickoff(row['timestamp']) for row in fixtures]
            ids = tuple(_match_id(row['match_id']) for row in fixtures)
            days = {v.astimezone(LOCAL_ZONE).date().isoformat() for v in kickoffs}
            if (len(set(ids)) != size or len(days) != 1 or created >= min(kickoffs)
                    or max(kickoffs) >= as_of or created >= as_of):
                raise ValueError('time_or_identity')
            for name, method in entry['methods'].items():
                if name not in models.METHODS or not isinstance(method,dict) or not isinstance(method.get('distribution'), dict):
                    continue
                key = context_key(name, method, size)
                grouped[key].append(dict(id=entry['id'],day=next(iter(days)),ids=ids,created=created,
                    entry=entry,method=method,name=name,size=size))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            excluded['invalid_forecast'] += 1
    contexts = {}
    for key, candidates in sorted(grouped.items()):
        if check_time is not None: check_time()
        # Global within context, including rescheduled appearances: no shared
        # match contributes twice, or to both tuning and test phases.
        used, rows = set(), []
        for row in sorted(candidates,key=lambda r:(r['day'],r['created'],r['id'])):
            if used.intersection(row['ids']):
                excluded['overlapping_groups'] += 1
                continue
            used.update(row['ids']); rows.append(row)
        days = sorted({row['day'] for row in rows})
        first = rows[0]
        result = dict(method=first['name'],version=first['method']['version'],size=first['size'],
            status='unavailable',reason='Historique chronologique insuffisant pour sélectionner un seuil.',
            total_disjoint_groups=len(rows),total_days=len(days))
        contexts[key] = result
        if len(days) < 2*MIN_DAYS:
            continue
        split = max(MIN_DAYS, min(len(days)-MIN_DAYS,2*len(days)//3))
        test_day = days[split]
        test_rows = [row for row in rows if row['day'] >= test_day]
        boundary = min(row['created'] for row in test_rows)
        tuning, test = [], []
        for row in rows:
            before = boundary if row['day'] < test_day else as_of
            observation = _observation(row['entry'], before)
            if observation is None:
                excluded['result_unavailable_at_phase_boundary'] += 1
                continue
            (tuning if row['day'] < test_day else test).append(dict(row,
                actual=observation[0],label_available_at=observation[1]))
        result.update(tuning_groups=len(tuning),test_groups=len(test),
            tuning_days=len({r['day'] for r in tuning}),test_days=len({r['day'] for r in test}),
            tuning_labels_before=boundary.isoformat(),test_first_day=test_day)
        if (len(tuning)<MIN_TUNING or len(test)<MIN_TEST
                or result['tuning_days']<MIN_DAYS or result['test_days']<MIN_DAYS):
            continue
        try:
            thresholds=sorted({models.probability_for_sequence(row['method'],row['actual']) for row in tuning})
            if not thresholds or any(not isfinite(v) or not 0<v<=1 for v in thresholds):
                raise ValueError('Scores de calibration invalides.')
            prepared=_prepare(tuning,min(thresholds),monotonic()+30,check_time)
            # This is a declared finite candidate search, not a claim of a
            # global mathematical optimum over every possible threshold. A
            # universal set is an abstention, so include its activation boundary
            # whenever its complete probability list was already enumerated.
            candidates = set(thresholds)
            for row in prepared:
                if len(row['negative_probabilities']) == row['universe']:
                    minimum = -row['negative_probabilities'][-1]
                    boundary = nextafter(minimum, 1.0)
                    if boundary <= 1:
                        candidates.add(boundary)
            scored=[(_metric(prepared,tau),tau) for tau in sorted(candidates)]
            metric,tau=max(scored,key=lambda pair:(pair[0]['f05'],pair[0]['precision'],-pair[0]['mean_proposals'],pair[1]))
            tested=_prepare(test,tau,monotonic()+30,check_time)
            measured,reference=_metric(tested,tau),_metric(tested)
            passes=(measured['hits']>0 and measured['f05']>=reference['f05']
                    and measured['precision']>=reference['precision'])
            result.update(status='qualified' if passes else 'not_qualified',threshold=tau,
                reason=('Seuil choisi sur le passé et évalué sur des journées ultérieures, sans baisse du F0,5 ni de la précision par choix face au Top3.' if passes else
                        'Le seuil testé ne satisfait pas les critères historiques face à la référence Top3.'),
                tuning=metric,test=measured,reference_top3=reference,
                candidate_thresholds=len(candidates),
                tuning_ids_digest=digest(sorted(row['id'] for row in tuning)),
                test_ids_digest=digest(sorted(row['id'] for row in test)),
                quality_claim='Comparaison historique descriptive ; avantage prédictif futur non garanti.')
        except (ValueError, TypeError, KeyError, OverflowError, TimeoutError, MemoryError) as error:
            result.update(status='unavailable',reason=str(error))
    artifact=dict(schema=VERSION,created_at=as_of.isoformat(),contexts=contexts,exclusions=dict(excluded),
        protocol=dict(objective='best_tuning_f05_among_declared_candidates',
            candidates='realized_tuning_probabilities_and_enumerated_universal_activation_boundaries',
            beta=.5,min_tuning_groups=MIN_TUNING,
            min_test_groups=MIN_TEST,min_days_per_phase=MIN_DAYS,split='whole_days_2_3_then_1_3',
            overlap='disjoint_match_ids_before_result_filtering',universal_set='abstain',
            calendar_timezone='Africa/Kinshasa',
            pooling='same_algorithm_artifact_configuration_and_block_sizes_across_competitions',
            promotion='test_f05_and_precision_at_least_reference_top3',
            labels='tuning_known_before_first_test_forecast;test_known_before_artifact'))
    artifact['sha256']=digest(artifact)
    return artifact
