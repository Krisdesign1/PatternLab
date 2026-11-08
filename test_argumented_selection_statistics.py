"""Synthetic checks for the fixed exploratory replay statistics; no data I/O."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json
from math import log
import random
import unittest
from unittest.mock import patch

import argumented_selection_statistics as stats


def distribution(choice='1st', confidence=.6):
    return {value: confidence if value == choice else (1-confidence)/2 for value in stats.VALUES}


def record(mid=1, day='2024-01-01', *, choice='1st', baseline_choice=None,
           confidence=.6, baseline_confidence=.55, matching=7, age=30,
           actual='1st', league='61', kickoff_hour=12):
    prediction = datetime.combine(date.fromisoformat(day), time.min, timezone.utc)
    baseline_choice = baseline_choice or choice
    kickoff = prediction+timedelta(hours=kickoff_hour)
    counts = {value: matching if value == choice else (10-matching)//2 for value in stats.VALUES}
    # Assign the possible remainder using canonical order, preserving ten.
    for value in stats.VALUES:
        if sum(counts.values()) < 10 and value != choice:
            counts[value] += 1
    first = prediction-timedelta(days=age)
    last = prediction-timedelta(days=1)
    recent = dict(matches=10, matching=matching, counts=counts,
        first_at=first.isoformat(), last_at=last.isoformat(),
        ids=[str(900000000+int(mid)*20+index) for index in range(10)])
    agreement = choice == baseline_choice
    full = agreement and matching >= 5 and matching == max(counts.values())
    return dict(match_id=str(mid), day=kickoff.astimezone(stats.ZONE).date().isoformat(),
        timestamp=kickoff.isoformat(), league=league, source='API-Football',
        provider='api-football', country='Synthetic country', competition='Synthetic league',
        fixture=dict(match_id=str(mid), league=league, home_id='101', away_id='102',
            timestamp=kickoff.isoformat(), provider='api-football'),
        prediction_at=prediction.isoformat(), cutoff_at=prediction.isoformat(),
        history_before=prediction.date().isoformat(),
        probabilities=distribution(choice, confidence),
        baseline=distribution(baseline_choice, baseline_confidence),
        choice=choice, baseline_choice=baseline_choice,
        home_profile=dict(matches=40), away_profile=dict(matches=35),
        training=100, history_days=30, eligible=True, personalized=True,
        recent_home=deepcopy(recent), recent_away=deepcopy(recent),
        synthesized_NS=True, no_future_target_history=True,
        flags=dict(full_rule=full, agreement_only=agreement,
            min7=full and matching >= 7, recent365=full and age <= 365), actual=actual)


def fixture_pool():
    return [
        record(30, confidence=.7, baseline_confidence=.55, matching=7),
        record(9, confidence=.8, baseline_choice='2nd', baseline_confidence=.9),
        record(2, confidence=.7, baseline_confidence=.6, matching=5, age=366, actual='2nd'),
        record(100, confidence=.4, baseline_confidence=.95, matching=4),
        record(101, '2024-01-02', baseline_choice='2nd', confidence=.9),
    ]


class ArgumentedSelectionStatisticsTests(unittest.TestCase):
    def test_controls_have_exact_daily_counts_and_rank_by_own_argmax_with_numeric_ties(self):
        report = stats.summarize(fixture_pool())
        full = report['policies']['argumented']
        first, zero = full['daily']
        self.assertEqual(first['match_ids']['selected'], ['2', '30'])
        self.assertEqual(first['match_ids']['top_clubs'], ['9', '2'])
        self.assertEqual(first['match_ids']['top_baseline'], ['100', '9'])
        for policy in report['policies'].values():
            for day in policy['daily']:
                self.assertEqual({day[name]['N'] for name in stats.COHORTS}, {day['selected']['N']})
            self.assertTrue(all(item['equal_counts_every_day'] for item in policy['comparisons'].values()))
        self.assertEqual([zero[name]['N'] for name in stats.COHORTS], [0, 0, 0])
        self.assertEqual(full['zero_selection_days'], 1)
        self.assertEqual(report['policies']['agreement_only']['daily'][0]['selected']['N'], 3)
        self.assertEqual(report['policies']['strict_recent7']['daily'][0]['selected']['N'], 1)
        self.assertEqual(report['policies']['recent365']['daily'][0]['selected']['N'], 1)

    def test_no_global_top_n_or_artificial_cap(self):
        rows = [record(index+1) for index in range(175)]
        rows.append(record(1001, '2024-01-02', confidence=.99))
        result = stats.summarize(rows)['policies']['argumented']
        self.assertEqual(result['overall']['selected']['N'], 176)
        self.assertEqual([item['selected']['N'] for item in result['daily']], [175, 1])
        self.assertEqual([item['top_clubs']['N'] for item in result['daily']], [175, 1])

    def test_multiclass_scores_keep_frozen_distributions_and_paired_baseline_accuracy(self):
        rows = [record(1, confidence=.7, baseline_confidence=.5),
                record(2, confidence=.8, baseline_confidence=.6, actual='2nd')]
        result = stats.summarize(rows)['policies']['argumented']['overall']
        selected, baseline = result['selected'], result['baseline_on_selected']
        self.assertEqual((selected['N'], selected['evaluated_N'], selected['correct']), (2, 2, 1))
        self.assertEqual(selected['accuracy'], .5)
        self.assertEqual(selected['accuracy'], baseline['accuracy'])
        self.assertNotEqual(selected['logloss'], baseline['logloss'])
        self.assertAlmostEqual(selected['logloss'], -(log(.7)+log(.1))/2)
        expected = ((.7-1)**2+.15**2+.15**2 + .8**2+.1**2+(.1-1)**2)/2
        self.assertAlmostEqual(selected['brier'], expected)
        self.assertAlmostEqual(selected['mean_confidence'], .75)
        self.assertEqual(selected['coverage'], 1)
        self.assertEqual(selected['scoring_distribution'], 'frozen_clubs')
        self.assertEqual(baseline['scoring_distribution'], 'frozen_league_baseline')
        self.assertEqual([item['threshold'] for item in selected['confidence_thresholds']], [.5, .6, .7])
        self.assertEqual(sum(item['N'] for item in selected['confidence_bins']), 2)

    def test_actuals_affect_scores_only_and_unavailable_results_are_not_failures(self):
        rows = fixture_pool()
        first = stats.summarize(rows)
        changed = deepcopy(rows)
        changed[0]['actual'] = None
        changed[2]['actual'] = '1st'
        second = stats.summarize(changed)
        for name in stats.POLICIES:
            self.assertEqual([item['match_ids'] for item in first['policies'][name]['daily']],
                             [item['match_ids'] for item in second['policies'][name]['daily']])
        full = second['policies']['argumented']
        metrics = full['overall']['selected']
        self.assertEqual((metrics['N'], metrics['evaluated_N'], metrics['missing_outcomes']), (2, 1, 1))
        self.assertEqual(metrics['accuracy'], 1)
        comparison = full['comparisons']['top_clubs']
        self.assertIsNone(comparison['daily'][0]['delta_correct'])
        self.assertEqual(comparison['paired_evaluated_N'], 0)
        self.assertIsNone(comparison['delta_accuracy'])

    def test_overlap_and_exact_paired_day_success_delta(self):
        result = stats.summarize(fixture_pool())['policies']['argumented']
        clubs = result['comparisons']['top_clubs']
        day = clubs['daily'][0]
        self.assertEqual((day['N'], day['control_N'], day['overlap_N']), (2, 2, 1))
        self.assertEqual(day['overlap_ids'], ['2'])
        self.assertEqual(day['selected_only_ids'], ['30'])
        self.assertEqual(day['control_only_ids'], ['9'])
        self.assertEqual((day['selected_correct'], day['control_correct'], day['delta_correct']), (1, 1, 0))
        self.assertEqual(clubs['delta_accuracy'], 0)
        baseline = result['comparisons']['top_baseline']['daily'][0]
        self.assertEqual((baseline['selected_correct'], baseline['control_correct'], baseline['delta_correct']), (1, 1, 0))

    def test_complete_utc_prediction_week_bootstrap_is_fixed_and_boundary_explicit(self):
        start = date(2024, 1, 3)  # Wednesday: this boundary week is excluded.
        rows = []
        for offset in range(75):  # Ends Sunday, ten complete weeks after the boundary.
            day = (start+timedelta(days=offset)).isoformat()
            rows.append(record(2*offset+1, day, confidence=.6, actual='1st'))
            actual = '2nd' if offset//7 % 2 else '1st'
            rows.append(record(2*offset+2, day, choice='2nd', baseline_choice='1st',
                confidence=.9, baseline_confidence=.8, actual=actual))
        first = stats.summarize(rows)['policies']['argumented']['comparisons']['top_clubs']['bootstrap']
        random.Random(44).shuffle(rows)
        second = stats.summarize(rows)['policies']['argumented']['comparisons']['top_clubs']['bootstrap']
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'descriptive_interval')
        self.assertEqual(first['resamples'], 1000)
        self.assertEqual(first['seed'], 20261030)
        self.assertEqual(first['weeks'], 10)
        self.assertEqual(first['boundary_weeks_excluded'], ['2024-01-01'])
        self.assertEqual(first['evaluated_N'], 70)
        self.assertLessEqual(first['lower'], first['upper'])
        self.assertEqual(first['week_basis'], 'UTC_calendar_week_of_shared_virtual_prediction_date')

    def test_incomplete_outcome_excludes_entire_week_and_small_samples_get_no_interval(self):
        rows = []
        for offset in range(70):
            day = (date(2024, 1, 1)+timedelta(days=offset)).isoformat()
            rows.append(record(offset+1, day))
        rows[10]['actual'] = None
        interval = stats.summarize(rows)['policies']['argumented']['comparisons']['top_clubs']['bootstrap']
        self.assertEqual(interval['outcome_incomplete_weeks_excluded'], ['2024-01-08'])
        self.assertEqual(interval['weeks'], 9)
        small = stats.summarize(rows[:5])['policies']['argumented']['comparisons']['top_clubs']['bootstrap']
        self.assertEqual(small['status'], 'insufficient_complete_weeks')
        self.assertIsNone(small['lower'])
        self.assertIsNone(small['upper'])

    def test_shared_cutoff_keeps_midnight_local_day_pair_in_one_utc_week(self):
        a = record(1, '2024-01-07', kickoff_hour=23.5)
        b = record(2, '2024-01-07', kickoff_hour=25)
        self.assertEqual(a['day'], '2024-01-08')
        self.assertEqual(a['day'], b['day'])
        pair = stats.summarize([a, b])['policies']['argumented']['comparisons']['top_clubs']['daily'][0]
        self.assertEqual(pair['utc_weeks'], ['2024-01-01'])
        self.assertEqual(pair['N'], 2)

    def test_zero_probability_is_explicitly_floored_only_for_logloss(self):
        row = record(actual='2nd')
        row['probabilities'] = {'1st': 1., 'Égal': 0., '2nd': 0.}
        report = stats.summarize([row])
        selected = report['policies']['argumented']['overall']['selected']
        self.assertAlmostEqual(selected['logloss'], -log(stats.LOG_EPSILON))
        self.assertEqual(selected['brier'], 2)
        self.assertEqual(row['probabilities']['2nd'], 0.)
        self.assertEqual(report['protocol']['zero_probability_log_floor'], stats.LOG_EPSILON)

    def test_empty_data_is_serializable_explicit_exploratory_and_has_no_test_claim(self):
        result = stats.summarize([])
        self.assertTrue(result['exploratory'])
        self.assertFalse(result['holdout_test'])
        self.assertFalse(result['automatic_promotion'])
        self.assertFalse(result['point_in_time_provider_availability_verified'])
        self.assertEqual(result['common_pool']['quality_support']['N'], 0)
        for policy in result['policies'].values():
            self.assertIsNone(policy['overall']['selected']['accuracy'])
            self.assertIsNone(policy['overall']['selected']['coverage'])
        json.dumps(result, allow_nan=False)

    def test_duplicate_and_conflicting_identities_are_rejected_globally(self):
        first = record()
        for other in (deepcopy(first), record(1, '2024-01-02'), record(1, league='39')):
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, 'Duplicate'):
                stats.summarize([first, other])

    def test_invalid_probabilities_flags_support_or_causal_proofs_are_rejected(self):
        mutations = [
            lambda row: row['probabilities'].update({'1st': float('nan')}),
            lambda row: row['baseline'].update({'Égal': -.1}),
            lambda row: row['probabilities'].update({'1st': .9}),
            lambda row: row.update(choice='2nd'),
            lambda row: row['flags'].update(full_rule=1),
            lambda row: row['flags'].update(full_rule=False),
            lambda row: row.update(day='2024-01-02'),
            lambda row: row.update(prediction_at='2024-01-02T00:00:00+00:00'),
            lambda row: row.update(history_before='2024-01-02'),
            lambda row: row['recent_home'].update(last_at='2024-01-01T00:00:00+00:00'),
            lambda row: row['recent_away']['ids'].__setitem__(0, row['match_id']),
            lambda row: row['recent_away']['ids'].__setitem__(0, row['recent_away']['ids'][1]),
            lambda row: row['home_profile'].update(matches=9),
            lambda row: row.update(training=29),
            lambda row: row.update(history_days=2),
            lambda row: row.update(eligible=False),
            lambda row: row.update(personalized=False),
            lambda row: row.update(no_future_target_history=False),
            lambda row: row.update(source='Other'),
            lambda row: row['fixture'].update(ft='1-0'),
            lambda row: row.update(actual='unknown'),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                row = record()
                mutate(row)
                with self.assertRaises(ValueError):
                    stats.summarize([row])

    def test_recent365_inclusive_calendar_boundary_and_argmax_tie_order(self):
        rows = [record(1, age=365), record(2, age=366)]
        result = stats.summarize(rows)['policies']['recent365']
        self.assertEqual(result['daily'][0]['match_ids']['selected'], ['1'])
        tie = record()
        tie['probabilities'] = {'1st': .45, 'Égal': .1, '2nd': .45}
        tie['baseline'] = {'1st': .5, 'Égal': 0., '2nd': .5}
        self.assertEqual(stats.summarize([tie])['policies']['argumented']['overall']['selected']['N'], 1)

    def test_deterministic_order_no_input_mutation_or_file_io_and_compact_daily_rows(self):
        rows = fixture_pool()
        before = deepcopy(rows)
        first = stats.summarize(rows)
        random.Random(7).shuffle(rows)
        with patch('builtins.open', side_effect=AssertionError('file I/O')):
            second = stats.summarize(rows)
        self.assertEqual(first, second)
        self.assertEqual(sorted(rows, key=lambda row: int(row['match_id'])),
                         sorted(before, key=lambda row: int(row['match_id'])))
        full = first['policies']['argumented']
        self.assertNotIn('confidence_bins', full['daily'][0]['selected'])
        self.assertNotIn('confidence_bins', full['by_league'][0]['selected'])
        self.assertIn('confidence_bins', full['by_year']['2024']['selected'])
        json.dumps(first, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
