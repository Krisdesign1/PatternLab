"""Pure descriptive statistics for the fixed, reconstructed historical replay.

``summarize(records)`` never fits, tunes, fetches, persists, or promotes a rule.
Every control is selected from the SAME daily common pool, with exactly as
many matches as the corresponding policy. Outcomes enter scoring only, after
identities, probabilities, rule flags, and daily selections have been fixed.

The source archive is its current corrected state. A virtual UTC-midnight
prediction and synthesized NS fixture are not evidence of a real past capture
or of the provider's actual information availability at that time.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from math import fsum, isclose, isfinite, log
from numbers import Real
import random
from statistics import median
from zoneinfo import ZoneInfo


VERSION = 'argumented-selection-statistics-v1'
VALUES = ('1st', 'Égal', '2nd')
ZONE = ZoneInfo('Africa/Kinshasa')
POLICIES = {
    'argumented': 'full_rule',
    'agreement_only': 'agreement_only',
    'strict_recent7': 'min7',
    'recent365': 'recent365',
}
COHORTS = ('selected', 'top_clubs', 'top_baseline')
LOG_EPSILON = 1e-15
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261030
MIN_BOOTSTRAP_WEEKS = 8
CONFIDENCE_THRESHOLDS = (.5, .6, .7)
_SCORES = frozenset(('score', 'scores', 'ht', 'ft', 'value', 'goals', 'outcome', 'result',
                     'halftime', 'fulltime', 'home_score', 'away_score'))


def _identifier(value, name):
    if (isinstance(value, bool) or not isinstance(value, (str, int))
            or not str(value).isascii() or not str(value).isdecimal() or int(value) <= 0):
        raise ValueError(f'{name}: positive numeric API identity required.')
    return str(int(value))


def _day(value, name):
    if not isinstance(value, str):
        raise ValueError(f'{name}: ISO calendar date required.')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{name}: ISO calendar date required.') from None
    if parsed.isoformat() != value:
        raise ValueError(f'{name}: ISO calendar date required.')
    return parsed


def _stamp(value, name, *, naive_utc=False):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError(f'{name}: timestamp required.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None and naive_utc:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f'{name}: valid timezone-aware timestamp required.') from None


def _distribution(value, name):
    if (not isinstance(value, dict) or set(value) != set(VALUES)
            or any(isinstance(p, bool) or not isinstance(p, Real) or not isfinite(p)
                   or p < 0 or p > 1 for p in value.values())
            or not isclose(fsum(value.values()), 1., rel_tol=0, abs_tol=1e-9)):
        raise ValueError(f'{name}: three finite probabilities summing to one required.')
    return {key: float(value[key]) for key in VALUES}


def _count(value, name, *, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{name}: integer count outside permitted bounds.')
    return value


def _recent(raw, role, choice, kickoff_id, cutoff):
    item = raw.get('recent_' + role)
    if not isinstance(item, dict):
        raise ValueError(f'recent_{role}: ten-match support required.')
    if _count(item.get('matches'), 'recent matches') != 10:
        raise ValueError('Every common-pool record requires ten available precedents per role.')
    counts = item.get('counts')
    if not isinstance(counts, dict) or set(counts) != set(VALUES):
        raise ValueError('Recent class counts must contain exactly the three classes.')
    counts = {key: _count(counts[key], 'recent class count', maximum=10) for key in VALUES}
    if sum(counts.values()) != 10:
        raise ValueError('Recent class counts must sum to ten.')
    matching = _count(item.get('matching'), 'recent matching', maximum=10)
    if matching != counts[choice]:
        raise ValueError('Recent matching count contradicts the frozen model choice.')
    first = _stamp(item.get('first_at'), 'recent first_at', naive_utc=True)
    last = _stamp(item.get('last_at'), 'recent last_at', naive_utc=True)
    if first > last or last >= cutoff:
        raise ValueError('Future or current-cutoff history cannot enter selection.')
    ids = item.get('ids')
    if not isinstance(ids, (list, tuple)) or len(ids) != 10:
        raise ValueError('Ten recent match identities required.')
    ids = tuple(_identifier(mid, 'recent match_id') for mid in ids)
    if len(set(ids)) != 10 or kickoff_id in ids:
        raise ValueError('Recent history contains duplicate identities or the target result.')
    return dict(matches=10, counts=counts, matching=matching,
                first=first, last=last, span_days=(last.date()-first.date()).days)


def _records(records):
    checked, identities = [], set()
    try:
        iterator = iter(records)
    except TypeError:
        raise ValueError('Replay records must be an iterable of mappings.') from None
    for raw in iterator:
        if not isinstance(raw, dict):
            raise ValueError('Each replay record must be a mapping.')
        fixture = raw.get('fixture', {})
        if not isinstance(fixture, dict) or _SCORES.intersection(fixture):
            raise ValueError('The reconstructed prediction fixture must be score-free.')
        source = raw.get('source', fixture.get('source'))
        provider = raw.get('provider', fixture.get('provider'))
        if source not in (None, 'API-Football', 'api-football', 'https://www.api-football.com/'):
            raise ValueError('Replay source must be API-Football.')
        if provider not in (None, 'api-football') or (source is None and provider is None):
            raise ValueError('Explicit API-Football provenance required.')
        if fixture.get('provider') not in (None, 'api-football'):
            raise ValueError('Contradictory fixture provider.')
        mid = _identifier(raw.get('match_id'), 'match_id')
        if mid in identities:
            raise ValueError('Duplicate or conflicting replay match identity; double counting is forbidden.')
        identities.add(mid)
        league = _identifier(raw.get('league'), 'league')
        kickoff = _stamp(raw.get('timestamp'), 'timestamp', naive_utc=True)
        if (_identifier(fixture.get('match_id'), 'fixture match_id') != mid
                or _identifier(fixture.get('league'), 'fixture league') != league
                or _stamp(fixture.get('timestamp'), 'fixture timestamp', naive_utc=True) != kickoff):
            raise ValueError('Fixture identity, competition or kickoff contradicts the replay record.')
        home_id, away_id = (_identifier(fixture.get(role + '_id'), role + '_id') for role in ('home', 'away'))
        if home_id == away_id:
            raise ValueError('The two exact API club identities must differ.')
        day = _day(raw.get('day'), 'day')
        if day != kickoff.astimezone(ZONE).date():
            raise ValueError('Declared day contradicts the Kinshasa kickoff day.')
        prediction = _stamp(raw.get('prediction_at', raw.get('virtual_prediction_at')), 'prediction_at')
        before = _day(raw.get('history_before', raw.get('cutoff_exclusive', raw.get('before'))), 'history_before')
        cutoff = datetime.combine(before, time.min, timezone.utc)
        for key in ('cutoff_time', 'cutoff_at'):
            if raw.get(key) is not None and _stamp(raw[key], key) != cutoff:
                raise ValueError('The historical cutoff timestamp contradicts its UTC date.')
        if not cutoff <= prediction <= kickoff:
            raise ValueError('Selection must precede kickoff and cannot use future history.')
        if prediction != cutoff:
            raise ValueError('The reconstructed virtual prediction must equal its UTC-midnight cutoff.')
        if (raw.get('virtual_prediction_at') is not None
                and _stamp(raw['virtual_prediction_at'], 'virtual_prediction_at') != prediction):
            raise ValueError('The two virtual prediction timestamps contradict each other.')
        if before > kickoff.date():
            raise ValueError('The UTC historical cutoff exceeds the target UTC day.')
        for key in ('no_future_target_history', 'synthesized_NS'):
            if raw.get(key) is not True:
                raise ValueError(f'{key}: explicit true replay assertion required.')
        probabilities = _distribution(raw.get('probabilities'), 'probabilities')
        baseline = _distribution(raw.get('baseline'), 'baseline')
        choice = max(VALUES, key=probabilities.get)
        baseline_choice = max(VALUES, key=baseline.get)
        if raw.get('choice') != choice or raw.get('baseline_choice') != baseline_choice:
            raise ValueError('Frozen choices must be the true argmax, with the fixed class tie order.')
        if raw.get('eligible') is not True or raw.get('personalized') is not True:
            raise ValueError('Common-pool forecasts must be eligible and personalized.')
        training = _count(raw.get('training'), 'training', minimum=30)
        _count(raw.get('history_days'), 'history_days', minimum=3, maximum=training)
        profiles = {}
        for role in ('home', 'away'):
            profile = raw.get(role + '_profile')
            if not isinstance(profile, dict):
                raise ValueError('Both complete role profiles are required.')
            profiles[role] = _count(profile.get('matches'), role + ' profile matches', minimum=10)
            if profiles[role] > training:
                raise ValueError('Role profile count exceeds the full training sample.')
        recent = {role: _recent(raw, role, choice, mid, cutoff) for role in ('home', 'away')}
        flags = raw.get('flags')
        if (not isinstance(flags, dict) or not set(POLICIES.values()).issubset(flags)
                or any(type(value) is not bool for value in flags.values())):
            raise ValueError('All required rule flags must be actual booleans.')
        agreement = choice == baseline_choice
        full_rule = agreement and all(item['matching'] >= 5
            and item['matching'] == max(item['counts'].values()) for item in recent.values())
        expected = dict(agreement_only=agreement, full_rule=full_rule,
            min7=full_rule and all(item['matching'] >= 7 for item in recent.values()),
            recent365=full_rule and all((prediction.date()-item['first'].date()).days <= 365
                                       for item in recent.values()))
        if any(flags[key] != value for key, value in expected.items()):
            raise ValueError('Rule flags contradict the fixed pre-outcome evidence.')
        actual = raw.get('actual')
        if actual is not None and (not isinstance(actual, str) or actual not in VALUES):
            raise ValueError('actual must be a valid class or None for an unavailable outcome.')
        checked.append(dict(match_id=mid, league=league,
            country=raw.get('country', fixture.get('country', '')),
            competition=raw.get('competition', fixture.get('competition', '')),
            day=day.isoformat(), timestamp=kickoff, prediction_at=prediction,
            utc_day=kickoff.date(), year=str(day.year), probabilities=probabilities,
            baseline=baseline, choice=choice, baseline_choice=baseline_choice,
            actual=actual, flags=expected, profiles=profiles, recent=recent))
    shared_predictions = {}
    for row in checked:
        day, prediction = row['day'], row['prediction_at']
        if day in shared_predictions and shared_predictions[day] != prediction:
            raise ValueError('Every Kinshasa day must share one virtual prediction cutoff.')
        shared_predictions[day] = prediction
    return sorted(checked, key=lambda row: (row['day'], row['timestamp'], int(row['match_id'])))


def _range(values):
    return dict(min=min(values), median=median(values), max=max(values)) if values else dict(min=None, median=None, max=None)


def _support(rows):
    return dict(N=len(rows), evaluated_N=sum(row['actual'] is not None for row in rows),
        missing_outcomes=sum(row['actual'] is None for row in rows),
        days=len({row['day'] for row in rows}), leagues=len({row['league'] for row in rows}),
        role_profiles={role: _range([row['profiles'][role] for row in rows]) for role in ('home', 'away')},
        recent_matching={role: _range([row['recent'][role]['matching'] for row in rows]) for role in ('home', 'away')},
        recent_span_days={role: _range([row['recent'][role]['span_days'] for row in rows]) for role in ('home', 'away')},
        minimum_role_support_verified=all(min(row['profiles'].values()) >= 10 for row in rows),
        provenance='current_corrected_API_Football_archive',
        availability_at_virtual_prediction_verified=False)


def _basic_metrics(rows, method, pool_N, *, quality=True):
    distribution = 'baseline' if method == 'baseline' else 'probabilities'
    choice_key = 'baseline_choice' if method == 'baseline' else 'choice'
    evaluated = [row for row in rows if row['actual'] is not None]
    n, count = len(rows), len(evaluated)
    correct = sum(row[choice_key] == row['actual'] for row in evaluated)
    result = dict(N=n, selected_N=n, evaluated_N=count, missing_outcomes=n-count, correct=correct,
        accuracy=correct/count if count else None,
        logloss=-fsum(log(max(LOG_EPSILON, row[distribution][row['actual']])) for row in evaluated)/count if count else None,
        brier=fsum(fsum((row[distribution][value]-float(row['actual'] == value))**2 for value in VALUES)
                   for row in evaluated)/count if count else None,
        mean_confidence=fsum(row[distribution][row[choice_key]] for row in rows)/n if n else None,
        coverage=n/pool_N if pool_N else None, pool_N=pool_N,
        scoring_distribution='frozen_league_baseline' if method == 'baseline' else 'frozen_clubs')
    if quality:
        result['quality_support'] = _support(rows)
    return result


def _metrics(rows, method, pool_N):
    result = _basic_metrics(rows, method, pool_N)
    distribution = 'baseline' if method == 'baseline' else 'probabilities'
    choice_key = 'baseline_choice' if method == 'baseline' else 'choice'
    bins = []
    for index in range(10):
        members = [row for row in rows if min(9, int(row[distribution][row[choice_key]]*10)) == index]
        metrics = _basic_metrics(members, method, pool_N, quality=False)
        evaluated = [row for row in members if row['actual'] is not None]
        evaluated_confidence = (fsum(row[distribution][row[choice_key]] for row in evaluated)/len(evaluated)
                                if evaluated else None)
        bins.append(dict(lower=index/10, upper=(index+1)/10, upper_inclusive=index == 9,
            N=metrics['N'], evaluated_N=metrics['evaluated_N'], correct=metrics['correct'],
            accuracy=metrics['accuracy'], mean_confidence=metrics['mean_confidence'],
            evaluated_mean_confidence=evaluated_confidence,
            calibration_gap=(evaluated_confidence-metrics['accuracy']) if evaluated else None))
    result['confidence_bins'] = bins
    result['confidence_thresholds'] = [dict(threshold=threshold, **_basic_metrics(
        [row for row in rows if row[distribution][row[choice_key]] >= threshold], method, pool_N, quality=False))
        for threshold in CONFIDENCE_THRESHOLDS]
    result['ece_10_bins'] = (fsum(item['evaluated_N']*abs(item['calibration_gap']) for item in bins
        if item['evaluated_N'])/result['evaluated_N'] if result['evaluated_N'] else None)
    return result


def _week(stamp):
    return (stamp.date()-timedelta(days=stamp.weekday())).isoformat()


def _quantile(values, probability):
    ordered = sorted(values)
    index = (len(ordered)-1)*probability
    lower = int(index)
    upper = min(lower+1, len(ordered)-1)
    return ordered[lower]+(ordered[upper]-ordered[lower])*(index-lower)


def _bootstrap(pairs, observations):
    """Resample whole UTC weeks; retain both sides of every daily pairing."""
    base = dict(method='paired_complete_UTC_calendar_week_block_percentile',
        resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED,
        minimum_weeks=MIN_BOOTSTRAP_WEEKS, level=.95,
        week_basis='UTC_calendar_week_of_shared_virtual_prediction_date',
        lower=None, upper=None, delta_accuracy=None, weeks=0, evaluated_N=0,
        boundary_weeks_excluded=[], outcome_incomplete_weeks_excluded=[],
        complete_weeks=[], status='insufficient_complete_weeks',
        interpretation='exploratory_cluster_interval_not_a_causal_or_independent_team_test')
    if not observations:
        return base
    first = min(row['prediction_at'].date() for row in observations)
    last = max(row['prediction_at'].date() for row in observations)
    represented = sorted({_week(row['prediction_at']) for row in observations})
    complete = []
    for key in represented:
        monday = date.fromisoformat(key)
        if monday < first or monday+timedelta(days=6) > last:
            base['boundary_weeks_excluded'].append(key)
        else:
            complete.append(key)
    # Both sides of a Kinshasa-day pair share one virtual UTC prediction date;
    # this assigns the complete pair to one UTC calendar week without splitting
    # a Sunday-night/Monday-local slate across different resampling blocks.
    blocks = defaultdict(lambda: [0, 0])
    incomplete = set()
    for item in pairs:
        if not item['N']:
            continue
        weeks = item['utc_weeks']
        if len(weeks) != 1:
            raise ValueError('A paired day must share one virtual prediction week.')
        key = weeks[0]
        if key not in complete:
            continue
        if item['delta_correct'] is None:
            incomplete.add(key)
            continue
        blocks[key][0] += item['delta_correct']
        blocks[key][1] += item['N']
    base['outcome_incomplete_weeks_excluded'] = sorted(incomplete)
    usable = [(key, blocks[key]) for key in complete if key in blocks and key not in incomplete and blocks[key][1]]
    base['complete_weeks'] = [key for key, _ in usable]
    base['weeks'] = len(usable)
    base['evaluated_N'] = sum(block[1] for _, block in usable)
    if base['evaluated_N']:
        base['delta_accuracy'] = sum(block[0] for _, block in usable)/base['evaluated_N']
    if len(usable) < MIN_BOOTSTRAP_WEEKS:
        return base
    generator = random.Random(BOOTSTRAP_SEED)
    deltas = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        drawn = [usable[generator.randrange(len(usable))][1] for _ in usable]
        deltas.append(sum(block[0] for block in drawn)/sum(block[1] for block in drawn))
    base.update(status='descriptive_interval', lower=_quantile(deltas, .025), upper=_quantile(deltas, .975))
    return base


def _comparison(daily, control, pool):
    pairs = []
    for day, cohorts in daily.items():
        selected, other = cohorts['selected'], cohorts[control]
        ids_a = {row['match_id'] for row in selected}
        ids_b = {row['match_id'] for row in other}
        complete = all(row['actual'] is not None for row in selected+other)
        baseline = control == 'top_baseline'
        selected_correct = sum(row['choice'] == row['actual'] for row in selected) if complete else None
        other_correct = sum(row['baseline_choice' if baseline else 'choice'] == row['actual'] for row in other) if complete else None
        delta = selected_correct-other_correct if complete else None
        pairs.append(dict(day=day, N=len(selected), control_N=len(other), equal_counts=len(selected) == len(other),
            evaluated=complete, selected_correct=selected_correct, control_correct=other_correct,
            delta_correct=delta, delta_accuracy=delta/len(selected) if complete and selected else None,
            overlap_N=len(ids_a & ids_b), selected_only_N=len(ids_a-ids_b), control_only_N=len(ids_b-ids_a),
            overlap_ids=sorted(ids_a & ids_b, key=int), selected_only_ids=sorted(ids_a-ids_b, key=int),
            control_only_ids=sorted(ids_b-ids_a, key=int),
            utc_weeks=sorted({_week(row['prediction_at']) for row in selected+other})))
    complete = [item for item in pairs if item['evaluated']]
    count = sum(item['N'] for item in complete)
    delta = sum(item['delta_correct'] for item in complete)
    return dict(control=control, daily=pairs,
        N=sum(item['N'] for item in pairs), control_N=sum(item['control_N'] for item in pairs),
        equal_counts_every_day=all(item['equal_counts'] for item in pairs),
        paired_complete_days=sum(item['N'] > 0 for item in complete),
        paired_evaluated_N=count, unevaluated_days=sum(not item['evaluated'] for item in pairs),
        delta_correct=delta, delta_accuracy=delta/count if count else None,
        overlap_N=sum(item['overlap_N'] for item in pairs),
        selected_only_N=sum(item['selected_only_N'] for item in pairs),
        control_only_N=sum(item['control_only_N'] for item in pairs),
        bootstrap=_bootstrap(pairs, pool), inference='descriptive_only_no_winner_or_promotion')


def _scope(cohorts, pool, *, detailed=True):
    metrics = _metrics if detailed else lambda rows, method, n: _basic_metrics(rows, method, n, quality=False)
    return dict(common_pool_N=len(pool), quality_support=_support(pool),
        **{name: metrics(cohorts[name], 'baseline' if name == 'top_baseline' else 'clubs', len(pool))
           for name in COHORTS},
        baseline_on_selected=metrics(cohorts['selected'], 'baseline', len(pool)))


def summarize(records):
    """Describe four predeclared policies and count-matched daily controls.

    Records contain only common-pool fixtures (both full role profiles >=10),
    frozen Clubs/baseline distributions, ten-match support, fixed boolean rule
    flags, and an optional actual class. Invalid records or duplicate fixture
    identities raise ``ValueError``; nothing is silently repaired or dropped.
    Missing outcomes are explicit and cannot become failed predictions.
    """
    pool = _records(records)
    days = sorted({row['day'] for row in pool})
    by_day = {day: [row for row in pool if row['day'] == day] for day in days}
    result = dict(version=VERSION, exploratory=True, automatic_decision=False,
        automatic_promotion=False, holdout_test=False, synthesized_NS=True,
        timezone=ZONE.key, source='API-Football',
        interpretation='offline_conditional_reconstruction_from_current_corrected_archive',
        point_in_time_provider_availability_verified=False,
        protocol=dict(policies=dict(POLICIES), recent_matches=10, minimum_full_role_profile=10,
            full_recent_minimum=5, strict_recent_minimum=7, recent365_calendar_days=365,
            agreement_only_removes_all_recent_matching_and_modal_requirements=True,
            control_pool='identical_daily_common_pool_for_every_policy_and_control',
            control_size='exact_daily_policy_selected_count_without_global_top_N_or_cap',
            subgroup_counts='year_and_league_describe_inherited_daily_selections_not_new_subgroup_rankings',
            ranking='descending_own_argmax_probability_then_UTC_kickoff_then_numeric_match_id',
            argmax_tie_order=list(VALUES), brier='multiclass_sum_squared_error_range_0_to_2',
            logloss='multiclass_negative_log_probability_of_actual',
            zero_probability_log_floor=LOG_EPSILON, probabilities_changed_for_scoring=False,
            confidence_bins=[index/10 for index in range(11)],
            confidence_thresholds=list(CONFIDENCE_THRESHOLDS),
            bootstrap_resamples=BOOTSTRAP_RESAMPLES, bootstrap_seed=BOOTSTRAP_SEED,
            minimum_bootstrap_complete_UTC_weeks=MIN_BOOTSTRAP_WEEKS),
        common_pool=dict(quality_support=_support(pool),
            first_day=days[0] if days else None, last_day=days[-1] if days else None),
        limitations=[
            'Exploratory fixed-rule replay; no untouched holdout or automatic promotion.',
            'Virtual prediction times and synthesized NS fixtures are not real past captures.',
            'Current corrected archive does not establish historical provider availability.',
            'Weekly blocks preserve paired days; repeated teams can remain dependent across weeks.',
            'Intervals across four variants and two controls are unadjusted descriptive intervals, not evidence of a winner.',
            'Boundary UTC weeks of shared virtual prediction dates are excluded from intervals.',
            'Weeks are complete relative to the observed virtual prediction span; missing source coverage is not proven complete.',
            'A baseline on the same agreement-selected matches has identical accuracy by construction; its probability scores can differ.',
        ], policies={})
    for policy, flag in POLICIES.items():
        daily = {}
        for day, candidates in by_day.items():
            selected = [row for row in candidates if row['flags'][flag]]
            n = len(selected)
            clubs = sorted(candidates, key=lambda row: (-row['probabilities'][row['choice']], row['timestamp'], int(row['match_id'])))[:n]
            baseline = sorted(candidates, key=lambda row: (-row['baseline'][row['baseline_choice']], row['timestamp'], int(row['match_id'])))[:n]
            daily[day] = dict(selected=selected, top_clubs=clubs, top_baseline=baseline)
        cohorts = {name: [row for item in daily.values() for row in item[name]] for name in COHORTS}
        overall = _scope(cohorts, pool)
        by_year = {}
        for year in sorted({row['year'] for row in pool}):
            year_pool = [row for row in pool if row['year'] == year]
            by_year[year] = _scope({name: [row for row in rows if row['year'] == year]
                                   for name, rows in cohorts.items()}, year_pool)
        by_league = []
        for league in sorted({row['league'] for row in pool}, key=int):
            league_pool = [row for row in pool if row['league'] == league]
            by_league.append(dict(league=league,
                countries=sorted({str(row['country']) for row in league_pool}),
                competitions=sorted({str(row['competition']) for row in league_pool}),
                **_scope({name: [row for row in rows if row['league'] == league]
                          for name, rows in cohorts.items()}, league_pool, detailed=False)))
        daily_report = [dict(day=day, match_ids={name: [row['match_id'] for row in rows]
            for name, rows in daily[day].items()}, **_scope(daily[day], by_day[day], detailed=False)) for day in days]
        result['policies'][policy] = dict(flag=flag, overall=overall, by_year=by_year,
            by_league=by_league, daily=daily_report,
            zero_selection_days=sum(not item['selected'] for item in daily.values()),
            baseline_on_selected_accuracy_identical_by_construction=True,
            comparisons={name: _comparison(daily, name, pool) for name in ('top_clubs', 'top_baseline')})
    return result
