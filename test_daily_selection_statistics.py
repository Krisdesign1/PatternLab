"""Chronological rule selection preserves its untouched test and abstentions."""
from copy import deepcopy
from datetime import date, timedelta
import json
import random
import unittest
from unittest.mock import patch

import daily_selection_statistics as stats


def observations(*, count=240, cohort='mono', size=2, five=False):
    rows = []
    for phase_index, phase in enumerate(stats.PHASES):
        for index in range(count):
            high = index % 20 < 10
            day = (date(2022+phase_index, 1, 1)+timedelta(days=index//8)).isoformat()
            group_id = f'{cohort}-{size}-{phase}-{index}'
            hit = index % 10 < (8 if high else 4)
            baseline_hit = index % 10 < 6
            methods = {}
            names = (stats.MONO_METHODS if five else stats.MONO_METHODS[:3]) if cohort == 'mono' else stats.MIXED_METHODS
            for name in names:
                is_clubs = name in ('clubs', 'catboost', 'context_neighbors')
                q, success = ((.8 if high else .4), hit) if is_clubs else (.6, baseline_hit)
                methods[name] = dict(top1_probability=q/2, top3_probability=q,
                    min_match_confidence=.7 if high else .4, top1=success and index % 3 == 0,
                    top3=success, loss_per_match=.9 if high else 1.1)
            rows.append(dict(id=group_id, day=day, phase=phase, cohort=cohort, size=size,
                match_ids=[f'{group_id}-{position}' for position in range(size)],
                leagues=['39'] if cohort == 'mono' else ['39', '61'], complete=True, methods=methods))
    return rows


def selected(result, cohort='mono', size=2):
    return next(row for row in result['by_size'] if row['cohort'] == cohort and row['size'] == size)


class DailySelectionStatisticsTests(unittest.TestCase):
    def test_fit_freezes_before_test_and_test_mutation_cannot_change_rule_or_calibration(self):
        rows = observations()
        prior = [row for row in rows if row['phase'] != 'test']
        frozen = stats.fit_rules(prior, {'cutoff': '2024-01-01'})
        original_frozen = deepcopy(frozen)
        self.assertIsNone(selected(frozen)['test'])
        self.assertEqual(selected(frozen)['family']['name'], 'statistical_three')
        self.assertIsNotNone(selected(frozen)['rule'])
        first = stats.evaluate_rules(rows, frozen)
        changed = deepcopy(rows)
        for row in changed:
            if row['phase'] == 'test':
                for payload in row['methods'].values():
                    payload.update(top1=False, top3=False, top1_probability=.01, top3_probability=.02, min_match_confidence=.01)
        second = stats.evaluate_rules(changed, frozen)
        for key in ('family', 'calibration', 'rule', 'selection'):
            self.assertEqual(selected(first)[key], selected(second)[key])
        self.assertNotEqual(selected(first)['test'], selected(second)['test'])
        self.assertEqual(frozen, original_frozen)
        with self.assertRaisesRegex(ValueError, 'refuse'):
            stats.fit_rules(rows)
        # The application can score only final records after writing rules.json.
        test_only = stats.evaluate_rules([row for row in rows if row['phase'] == 'test'], frozen)
        self.assertEqual(selected(test_only)['test'], selected(first)['test'])

    def test_frozen_evaluation_never_calls_fit_or_reselects_family(self):
        rows = observations()
        frozen = stats.fit_rules([row for row in rows if row['phase'] != 'test'])
        with patch.object(stats, '_fit', side_effect=AssertionError('refit')), patch.object(stats, '_family_attempt', side_effect=AssertionError('reselect')):
            final = stats.evaluate_rules(rows, frozen)
        self.assertEqual(selected(final)['status'], 'evaluated')
        changed = deepcopy(rows)
        changed[0]['methods']['clubs']['top3_probability'] = .9
        with self.assertRaisesRegex(ValueError, 'changé'):
            stats.evaluate_rules(changed, frozen)

    def test_family_is_decided_on_calibration_and_common_groups_are_identical(self):
        rows = observations(five=True)
        for row in rows:
            index = int(row['id'].rsplit('-', 1)[1])
            if row['phase'] == 'calibration' and index >= 80:
                row['methods'].pop('catboost')
            if row['phase'] == 'test' and index < 10:
                row['methods'].pop('combined')
        result = selected(stats.analyze(rows))
        self.assertEqual(result['family']['name'], 'statistical_three')
        self.assertFalse(result['family']['attempts'][0]['eligible'])
        self.assertEqual(result['availability']['test']['methods']['catboost']['groups'], 240)
        self.assertEqual(result['availability']['test']['common_groups'], 230)
        self.assertEqual(result['test']['common']['excluded_missing_method'], 10)
        self.assertEqual(set(result['test']['common']['methods']), {'frequency', 'clubs', 'combined'})
        self.assertEqual({item['evaluated_groups'] for item in result['test']['common']['methods'].values()}, {230})
        self.assertEqual(result['test']['selected']['metrics']['evaluated_groups'], result['test']['baseline_on_selected']['metrics']['evaluated_groups'])

    def test_five_methods_and_mixed_family_are_never_silently_substituted(self):
        result = stats.analyze(observations(five=True)+observations(cohort='mixed', size=3))
        self.assertEqual(selected(result)['family']['name'], 'all_five')
        self.assertEqual(selected(result)['family']['methods'], list(stats.MONO_METHODS))
        mixed = selected(result, 'mixed', 3)
        self.assertEqual(mixed['family']['name'], 'mixed_two')
        self.assertEqual(mixed['test']['baseline_on_selected']['metrics']['method'], 'league_baseline')
        self.assertEqual(len(mixed['test']['by_league']), 2)
        self.assertNotIn('overall', result)
        self.assertNotIn('top3_rate', result)

    def test_insufficient_samples_or_missing_outcomes_are_explicit_abstention(self):
        small = selected(stats.analyze(observations(count=80)))
        self.assertEqual(small['status'], 'insufficient_calibration')
        self.assertIsNone(small['rule'])
        self.assertIsNone(small['test'])
        rows = observations()
        for row in rows:
            if row['phase'] == 'selection':
                row['complete'] = False
                for payload in row['methods'].values():
                    payload.update(top1=None, top3=None, loss_per_match=None)
        result = selected(stats.analyze(rows))
        self.assertEqual(result['status'], 'insufficient_selection')
        self.assertEqual(result['selection']['common']['groups'], 240)
        self.assertEqual(result['selection']['common']['complete_groups'], 0)
        self.assertIsNone(result['rule'])
        candidate = result['selection']['candidates'][0]
        self.assertIsNone(candidate['top3_rate'])
        self.assertIsNone(candidate['top3_interval'])

    def test_calibrated_event_is_separate_from_raw_masses_and_raw_per_match_threshold(self):
        rows = observations()
        for row in rows:
            # Deliberate shared miscalibration, fixed using calibration only.
            row['methods']['clubs']['top3_probability'] = .95 if int(row['id'].rsplit('-', 1)[1]) % 20 < 10 else .7
        before = deepcopy(rows)
        result = selected(stats.analyze(rows))
        fit = result['calibration']['clubs']['fit']
        self.assertFalse(fit['alters_joint_distribution'])
        metrics = result['calibration']['clubs']['descriptive_metrics']
        self.assertGreater(metrics['raw_top3_event']['absolute_gap'], .20)
        self.assertLess(metrics['calibrated_top3_event']['absolute_gap'], .01)
        self.assertNotAlmostEqual(stats.calibrated_probability(.95, fit), .95)
        candidate = next(item for item in result['selection']['candidates']
            if item['method'] == 'clubs' and item['scope'] == 'min_match_base' and item['threshold'] == .5)
        self.assertEqual(candidate['selected_groups'], 120)
        self.assertEqual(candidate['coverage'], .5)
        self.assertEqual(rows, before)

    def test_coverage_denominator_counts_common_identities_before_missing_results(self):
        rows = observations()
        for row in rows:
            index = int(row['id'].rsplit('-', 1)[1])
            if row['phase'] == 'selection' and index % 20 >= 10:
                row['complete'] = False
                for payload in row['methods'].values():
                    payload.update(top1=None, top3=None, loss_per_match=None)
        result = selected(stats.analyze(rows))
        self.assertEqual(result['selection']['common']['groups'], 240)
        self.assertEqual(result['selection']['common']['complete_groups'], 120)
        candidate = next(item for item in result['selection']['candidates']
            if item['method'] == 'clubs' and item['scope'] == 'min_match_base' and item['threshold'] == .5)
        self.assertEqual((candidate['selected_groups'], candidate['evaluated_groups'], candidate['coverage']), (120, 120, .5))
        unfiltered = next(item for item in result['selection']['candidates']
            if item['method'] == 'clubs' and item['scope'] == 'calibrated_top3' and item['threshold'] == 0)
        self.assertEqual((unfiltered['selected_groups'], unfiltered['evaluated_groups'], unfiltered['coverage']), (240, 120, 1))

    def test_missing_test_results_are_never_zero_failures_and_no_selection_days_are_counted(self):
        rows = observations()
        for row in rows:
            if row['phase'] == 'test':
                row['complete'] = False
                for payload in row['methods'].values():
                    payload.update(top1=None, top3=None, loss_per_match=None)
        result = selected(stats.analyze(rows))
        self.assertEqual(result['status'], 'awaiting_test')
        self.assertIsNone(result['test']['selected']['metrics']['top3_rate'])
        self.assertGreater(result['test']['selected']['sample']['groups'], 0)
        self.assertEqual(result['test']['selected']['evaluated_sample']['groups'], 0)
        # A low-confidence test day remains in the coverage and abstention count.
        for row in rows:
            if row['phase'] == 'test' and row['day'] == '2024-01-01':
                for payload in row['methods'].values():
                    payload['top3_probability'] = .001
                    payload['top1_probability'] = .0001
                    payload['min_match_confidence'] = .001
        updated = selected(stats.analyze(rows))
        self.assertGreaterEqual(updated['test']['selected']['sample']['zero_selection_days'], 1)

    def test_validation_temporal_separation_duplicate_identity_and_probability_bounds(self):
        base = observations(count=1)
        cases = []
        same_day = deepcopy(base)
        same_day[1]['day'] = same_day[0]['day']
        cases.append(same_day)
        cases.append(base+[deepcopy(base[0])])
        same_match = deepcopy(base)
        same_match[1]['match_ids'][0] = same_match[0]['match_ids'][0]
        cases.append(same_match)
        bad = deepcopy(base)
        bad[0]['methods']['clubs']['top3_probability'] = float('nan')
        cases.append(bad)
        bad = deepcopy(base)
        bad[0]['methods']['clubs'].update(top1=True, top3=False)
        cases.append(bad)
        bad = deepcopy(base)
        bad[0]['methods']['clubs'].update(top1_probability=.9, top3_probability=.1)
        cases.append(bad)
        reversed_periods = deepcopy(base)
        reversed_periods[0]['day'] = '2025-01-01'
        cases.append(reversed_periods)
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                stats.analyze(rows)

    def test_deterministic_order_serializable_and_no_api_model_or_file_io(self):
        rows = observations()
        before = deepcopy(rows)
        first = stats.analyze(rows)
        random.Random(10).shuffle(rows)
        with patch('builtins.open', side_effect=AssertionError('file access')):
            second = stats.analyze(rows)
        self.assertEqual(first, second)
        self.assertEqual(sorted(rows, key=lambda row: row['id']), sorted(before, key=lambda row: row['id']))
        json.dumps(first, allow_nan=False)
        self.assertEqual(len(first['by_size']), 38)


if __name__ == '__main__':
    unittest.main()
