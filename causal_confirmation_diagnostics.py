"""Pure descriptive diagnostics for one frozen confirmation report protocol.

``build`` reads only the supplied report dictionary. It neither reconstructs
features nor loads, fits, changes or selects a model. Matches are paired across
all three methods; their count is never presented as independent replication.
"""
from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from hashlib import sha256
import json
from math import fsum

from causal_candidate import metrics as candidate_metrics
from halftime_domain import CLASS_ORDER, validate_probabilities


VERSION = 'causal-confirmation-diagnostics-v1'
METHODS = ('candidate', 'clubs', 'frequency')
REFERENCES = ('clubs', 'frequency')
SCORES = ('logloss', 'brier', 'accuracy')
STATUSES = ('settled', 'pending', 'unresolved', 'void', 'late_capture',
            'missing_ack', 'unattested_capture')
EXCLUSIONS = ('pending', 'unresolved', 'void', 'late_capture', 'missing_ack',
              'unattested_capture', 'inadmissible_capture')
TIMING_QUALITIES = ('local_commit_ack', 'synthetic_unattested', 'missing_commit_ack')


def _identifier(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f'{name} must be an identifier.')
    value = str(value)
    if not value or value != value.strip():
        raise ValueError(f'{name} must be a nonempty identifier.')
    return value


def _instant(value, name, *, allow_naive=False):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError(f'{name} must be an ISO timestamp.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            if not allow_naive:
                raise ValueError()
            # Report fixture timestamps use the application's explicit UTC
            # convention, including when serialized without an offset.
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f'{name} must be an ISO timestamp with a valid UTC conversion.') from None


def _window(protocol):
    start = _instant(protocol.get('confirmation_start'), 'confirmation_start')
    end = _instant(protocol.get('confirmation_end'), 'confirmation_end')
    if start >= end or start.time() != time() or end.time() != time():
        raise ValueError('The registered confirmation window must contain complete UTC days.')
    return start, end


def _exclusion(row):
    # Explicit bad acknowledgments remain excluded even if a caller supplies
    # a sporting result and an inconsistent admissible=True flag.
    if row['status'] in ('late_capture', 'missing_ack', 'unattested_capture'):
        return row['status']
    if row['timing_quality'] == 'missing_commit_ack':
        return 'missing_ack'
    if row['timing_quality'] == 'synthetic_unattested':
        return 'unattested_capture'
    if not row['admissible']:
        return 'inadmissible_capture'
    return None if row['status'] == 'settled' else row['status']


def _rows(protocol, start, end):
    source = protocol.get('rows')
    if not isinstance(source, list):
        raise ValueError('rows must be a list for one protocol.')
    output, forecasts, matches = [], set(), set()
    for raw in source:
        if not isinstance(raw, dict) or not isinstance(raw.get('fixture'), dict):
            raise ValueError('Each forecast must contain fixture metadata.')
        fixture = raw['fixture']
        forecast = _identifier(raw.get('forecast_id'), 'forecast_id')
        match = _identifier(fixture.get('match_id'), 'match_id')
        if forecast in forecasts or match in matches:
            raise ValueError('Duplicate forecast_id or match_id within one protocol.')
        forecasts.add(forecast); matches.add(match)
        league = _identifier(fixture.get('league'), 'league')
        kickoff = _instant(fixture.get('timestamp'), 'fixture.timestamp', allow_naive=True)
        if not start <= kickoff < end:
            raise ValueError('A fixture falls outside the registered UTC confirmation window.')
        if type(raw.get('admissible')) is not bool or raw.get('status') not in STATUSES:
            raise ValueError('Each forecast needs a boolean admissible flag and a known status.')
        if raw.get('timing_quality') not in TIMING_QUALITIES:
            raise ValueError('Each forecast needs a known capture timing quality.')
        actual = raw.get('actual')
        if actual is not None and actual not in CLASS_ORDER:
            raise ValueError('The sporting outcome must be one of the three classes or None.')
        if raw['status'] == 'settled' and actual is None:
            raise ValueError('A settled result must have a sporting outcome.')
        if raw['status'] in ('pending', 'unresolved', 'void') and actual is not None:
            raise ValueError('An unevaluated sporting result must not contain a class.')
        methods = raw.get('methods')
        if not isinstance(methods, dict) or set(methods) != set(METHODS):
            raise ValueError('All and only the three paired methods are required.')
        distributions = {method: {label: float(value) for label, value in
                         validate_probabilities(methods[method]).items()} for method in METHODS}
        row = dict(forecast_id=forecast, match_id=match, league=league,
                   kickoff_utc=kickoff.isoformat(), utc_day=kickoff.date().isoformat(),
                   utc_hour=kickoff.replace(minute=0, second=0, microsecond=0).isoformat(),
                   status=raw['status'], admissible=raw['admissible'],
                   timing_quality=raw['timing_quality'], actual=actual, methods=distributions)
        row['exclusion_reason'] = _exclusion(row)
        if row['exclusion_reason'] is None:
            row['scores'] = {method: candidate_metrics([distributions[method]], [actual])
                             for method in METHODS}
            row['deltas'] = {reference: {score: row['scores']['candidate'][score]
                             - row['scores'][reference][score] for score in SCORES}
                             for reference in REFERENCES}
        output.append(row)
    return sorted(output, key=lambda row: (row['kickoff_utc'], row['match_id'], row['forecast_id']))


def _class_counts(rows):
    return {label: sum(row['actual'] == label for row in rows) for label in CLASS_ORDER}


def _counts(rows):
    statuses = Counter(row['status'] for row in rows)
    exclusions = Counter(row['exclusion_reason'] for row in rows if row['exclusion_reason'] is not None)
    evaluated = sum(row['exclusion_reason'] is None for row in rows)
    return dict(forecasts=len(rows), reported_admissible=sum(row['admissible'] for row in rows),
                evaluated=evaluated, excluded=len(rows)-evaluated,
                statuses={status: statuses[status] for status in STATUSES},
                exclusion_reasons={reason: exclusions[reason] for reason in EXCLUSIONS})


def _metrics(rows):
    if not rows:
        return {}
    labels = [row['actual'] for row in rows]
    return {method: candidate_metrics([row['methods'][method] for row in rows], labels)
            for method in METHODS}


def _deltas(rows):
    return {reference: dict(count=len(rows), **{score: fsum(row['deltas'][reference][score]
                            for row in rows)/len(rows) if rows else None for score in SCORES})
            for reference in REFERENCES}


def _group_summary(rows):
    paired = [row for row in rows if row['exclusion_reason'] is None]
    return dict(counts=_counts(rows), actual_classes=_class_counts(paired),
                metrics=_metrics(paired), paired_deltas=_deltas(paired))


def _bins(probabilities, outcomes):
    groups = [[] for _ in range(10)]
    for probability, actual in zip(probabilities, outcomes):
        groups[min(int(probability*10), 9)].append((probability, actual))
    output = []
    for index, values in enumerate(groups):
        predicted = fsum(value[0] for value in values)/len(values) if values else None
        observed = fsum(value[1] for value in values)/len(values) if values else None
        output.append(dict(lower=index/10, upper=(index+1)/10, upper_inclusive=index == 9,
                           count=len(values), mean_probability=predicted, observed_frequency=observed,
                           gap=observed-predicted if values else None))
    return output


def _calibration(rows):
    result = {}
    for method in METHODS:
        distributions = [row['methods'][method] for row in rows]
        favorites = [max(CLASS_ORDER, key=lambda label: probability[label]) for probability in distributions]
        result[method] = dict(classes={label: _bins([probability[label] for probability in distributions],
                                 [int(row['actual'] == label) for row in rows]) for label in CLASS_ORDER},
                             favorite_confidence=_bins([probability[favorite] for probability, favorite
                                                          in zip(distributions, favorites)],
                                 [int(row['actual'] == favorite) for row, favorite in zip(rows, favorites)]))
    return result


def build(protocol):
    """Return JSON-safe descriptive diagnostics for ONE ``report()`` protocol.

    The report's stored metrics are ignored. All scores use the same admissible,
    settled matches and the existing candidate metric conventions: multiclass
    Brier sum, the registered log floor, and CLASS_ORDER for favorite ties.
    Probability bins are fixed [0,.1), ... [.9,1], with observed-minus-predicted
    gaps. No uncertainty estimate or model-selection decision is made here.
    """
    if not isinstance(protocol, dict):
        raise ValueError('A single confirmation protocol dictionary is required.')
    identity = _identifier(protocol.get('id'), 'protocol.id')
    compatible = protocol.get('implementation_compatible')
    if compatible is not None and type(compatible) is not bool:
        raise ValueError('The implementation compatibility must be boolean or unknown.')
    start, end = _window(protocol)
    rows = _rows(protocol, start, end)
    paired = [row for row in rows if row['exclusion_reason'] is None]
    by_day, by_league = defaultdict(list), defaultdict(list)
    for row in rows:
        by_day[row['utc_day']].append(row)
        by_league[row['league']].append(row)
    paired_days = sorted({row['utc_day'] for row in paired})
    paired_leagues = sorted({row['league'] for row in paired})
    paired_times = [datetime.fromisoformat(row['kickoff_utc']) for row in paired]
    registered_days = (end-start).days
    projection = dict(protocol_id=identity, confirmation_start=start.isoformat(), confirmation_end=end.isoformat(),
        rows=[{key: value for key, value in row.items() if key not in ('scores', 'deltas')} for row in rows])
    source_sha256 = sha256(json.dumps(projection, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    return dict(version=VERSION, protocol_id=identity,
                source_sha256=source_sha256,
                implementation_compatible=compatible,
                confirmation_start=start.isoformat(), confirmation_end=end.isoformat(),
                counts=_counts(rows),
                sample=dict(registered_utc_days=registered_days,
                    registered_one_day_pilot=registered_days == 1,
                    observed_utc_days=len(by_day), evaluated_utc_days=len(paired_days),
                    evaluated_utc_day_values=paired_days, observed_leagues=len(by_league),
                    evaluated_leagues=len(paired_leagues), evaluated_league_values=paired_leagues,
                    evaluated_kickoff_utc_hours=len({row['utc_hour'] for row in paired}),
                    first_evaluated_kickoff_utc=min(paired_times).isoformat() if paired else None,
                    last_evaluated_kickoff_utc=max(paired_times).isoformat() if paired else None,
                    evaluated_kickoff_span_hours=(max(paired_times)-min(paired_times)).total_seconds()/3600
                        if paired else None,
                    actual_classes=_class_counts(paired),
                    limitation='Matches, leagues and kickoff hours are counts, not established independent replications.'),
                metrics=_metrics(paired), paired_deltas=_deltas(paired),
                paired_delta_convention='candidate minus reference; lower logloss/Brier and higher accuracy are better',
                paired_rows=[{key: row[key] for key in ('forecast_id', 'match_id', 'league', 'utc_day',
                              'kickoff_utc', 'actual', 'scores', 'deltas')} for row in paired],
                by_utc_day=[dict(day=day, **_group_summary(by_day[day])) for day in sorted(by_day)],
                by_league=[dict(league=league, **_group_summary(by_league[league])) for league in sorted(by_league)],
                calibration=_calibration(paired),
                calibration_bin_convention='Ten fixed bins [0,.1), ... [.9,1]; gap = observed frequency minus mean probability.',
                support=dict(status='unknown', evaluated=len(paired),
                    reason='Report rows do not expose frozen historical support; no history is joined or inferred from probabilities.'),
                inference=dict(kind='statistical', available=False, status='uncertainty_not_estimated',
                    reason='Descriptive pilot: dependence between matches and leagues is not quantified. '
                           'A registered one-day pilot cannot estimate between-day uncertainty. '
                           'No confidence interval or significance test is computed.',
                    confidence_intervals=None, significance_tests=None),
                automatic_promotion=False, predictive_improvement_demonstrated=False)
