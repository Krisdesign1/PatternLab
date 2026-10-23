"""Leakage, missing-result and exact-distribution checks for selection replay."""
import copy
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from math import fsum, log, prod
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import contextual_scenarios as combined
import daily_selection_replay as study
from league_model import VALUES
from scenario_predictor import fit_sequences, probability
from test_daily_analysis_engine import raw_fixture


def fixture(mid, stamp, league=39, label=0):
    row = raw_fixture(mid, stamp, league, 'FT')
    row['league']['season'] = int(stamp[:4])
    ht, ft = ((1, 1), (0, 0), (0, 1))[label % 3]
    row['score']['halftime'] = {'home': ht, 'away': 0}
    row['score']['fulltime'] = {'home': ft, 'away': 0}
    return row


def sample():
    rows = []
    for league in (39, 61):
        for i in range(120):
            stamp = (datetime(2024, 1, 1, 12, tzinfo=timezone.utc)+timedelta(days=i//4, minutes=i%4)).isoformat()
            rows.append(fixture(league*10000+i, stamp, league, i))
        for day in ('2025-03-03', '2025-09-03', '2026-01-02'):
            for i in range(4):
                mid = league*100000+int(day.replace('-', ''))*10+i
                rows.append(fixture(mid, day+f'T{12+i:02}:00:00+00:00', league, i))
    return rows


def fit_fake(examples, params, columns, categorical):
    return {'rows': copy.deepcopy(examples), 'params': dict(params)}


def predict_fake(model, values, columns, categorical):
    return [dict(zip(VALUES, (.25, .25, .5))) for _ in values]


class SelectionReplayTests(unittest.TestCase):
    def run_study(self, rows=None, **kwargs):
        with patch.object(ml, '_fit', side_effect=fit_fake), patch.object(ml, '_predict', side_effect=predict_fake):
            return study.replay(sample() if rows is None else rows, today=date(2026, 1, 4), **kwargs)

    def test_five_methods_share_daily_mono_groups_and_mixed_has_distinct_reference(self):
        result = self.run_study()
        mono = [row for row in result['records'] if row['cohort'] == 'mono']
        mixed = [row for row in result['records'] if row['cohort'] == 'mixed']
        self.assertTrue(mono); self.assertTrue(mixed)
        for row in mono:
            self.assertEqual(set(row['methods']), set(study.METHODS))
            self.assertEqual(len(row['leagues']), 1)
        for row in mixed:
            self.assertEqual(set(row['methods']), {'clubs', 'league_baseline'})
            self.assertGreater(len(row['leagues']), 1)
        self.assertEqual({row['phase'] for row in mono}, {'calibration', 'selection', 'test'})
        self.assertTrue(all(row['methods']['context_neighbors']['neighbors'] > 0 for row in mono))
        self.assertEqual(sum(item['context_neighbor_supported'] + item['context_fallback_to_clubs']
                             for item in result['method_details']), len(mono))

    def test_input_order_is_irrelevant_and_input_remains_unchanged(self):
        raw = sample(); original = copy.deepcopy(raw)
        result = self.run_study(raw)
        self.assertEqual(raw, original)
        self.assertEqual(result, self.run_study(reversed(raw)))

    def test_future_test_labels_cannot_change_train_fits_pretest_predictions_or_grouping(self):
        raw = sample(); fitted = []
        def record_fit(examples, *args):
            fitted.append(copy.deepcopy(examples)); return fit_fake(examples, *args)
        with patch.object(ml, '_fit', side_effect=record_fit), patch.object(ml, '_predict', side_effect=predict_fake):
            original = study.replay(raw, today='2026-01-04')
        first_fits = copy.deepcopy(fitted); fitted.clear()
        changed = copy.deepcopy(raw)
        for row in changed:
            if row['fixture']['date'][:4] == '2026':
                row['score']['halftime'] = {'home': 0, 'away': 0}
                row['score']['fulltime'] = {'home': 0, 'away': 0}
        with patch.object(ml, '_fit', side_effect=record_fit), patch.object(ml, '_predict', side_effect=predict_fake):
            second = study.replay(changed, today='2026-01-04')
        self.assertEqual(first_fits, fitted)
        self.assertEqual([row for row in original['records'] if row['phase'] != 'test'],
                         [row for row in second['records'] if row['phase'] != 'test'])
        self.assertEqual([(row['id'], row['match_ids']) for row in original['records']],
                         [(row['id'], row['match_ids']) for row in second['records']])
        self.assertTrue(all(item['match']['timestamp'][:10] < '2024-12-31' for fit in fitted for item in fit))

    def test_before_test_is_once_and_precedes_all_test_correctness_and_loss(self):
        seen, calls = [], []
        original = study._actual
        def actual(group, outcomes):
            if group[0]['timestamp'][:4] == '2026': self.assertEqual(calls, ['frozen'])
            return original(group, outcomes)
        def freeze(records):
            self.assertTrue(records)
            self.assertTrue(all(row['phase'] in {'calibration', 'selection'} for row in records))
            seen.extend(copy.deepcopy(records)); calls.append('frozen')
            records.clear()  # A callback cannot mutate replay's original records.
        with patch.object(study, '_actual', side_effect=actual):
            result = self.run_study(before_test=freeze)
        self.assertEqual(calls, ['frozen'])
        self.assertEqual(len(seen), sum(row['phase'] != 'test' for row in result['records']))
        no_test = [row for row in sample() if row['fixture']['date'][:4] != '2026']
        calls.clear(); self.run_study(no_test, before_test=lambda rows: calls.append('frozen'))
        self.assertEqual(calls, ['frozen'])

    def test_missing_outcome_keeps_original_group_and_predictions_with_null_scores(self):
        raw = sample(); result = self.run_study(raw)
        # This is a test fixture; changing its score cannot affect its own features.
        target = next(row for row in raw if row['fixture']['date'].startswith('2026'))
        target_id = str(target['fixture']['id'])
        target['score']['halftime'] = {'home': None, 'away': None}
        changed = self.run_study(raw)
        self.assertEqual([row['id'] for row in result['records']], [row['id'] for row in changed['records']])
        first = {row['id']: row for row in result['records']}
        for row in changed['records']:
            if target_id not in row['match_ids']: continue
            self.assertFalse(row['complete'])
            for name, metric in row['methods'].items():
                self.assertIsNone(metric['top1']); self.assertIsNone(metric['top3']); self.assertIsNone(metric['loss_per_match'])
                self.assertEqual(metric['top1_probability'], first[row['id']]['methods'][name]['top1_probability'])

    def test_sparse_training_does_not_invent_a_trained_method_or_qualify_on_test(self):
        raw = sample()
        # A third league has many test results, but no TRAIN at all.
        for i in range(120): raw.append(fixture(99000000+i, '2026-01-02T18:00:00+00:00', 99, i))
        # TRAIN for league 61 remains enough to qualify, but only 15 blocks of 8.
        result = self.run_study(raw)
        self.assertNotIn('99', result['inventory']['qualified_leagues_train_only'])
        self.assertTrue(all('99' not in row['leagues'] for row in result['records'] if row['cohort'] == 'mono'))
        with patch.object(ml, '_fit', side_effect=ml.ModelError('Unavailable')):
            fitted = study._fit_league([api.normalize_fixture(row) for row in raw if row['league']['id'] == 39 and row['fixture']['date'][:4] == '2024'],
                                      [api.normalize_fixture(row) for row in raw if row['league']['id'] == 39 and row['fixture']['date'][:4] == '2024'], '39', study.PROTOCOL)
        self.assertNotIn(8, fitted['fits']); self.assertFalse(fitted['detail']['catboost_available'])

    def test_mixed_clubs_obeys_actual_component_block_availability(self):
        raw = sample()
        counts = {39: 0, 61: 0}
        reduced = []
        for row in raw:
            league = row['league']['id']
            if row['fixture']['date'].startswith('2024'):
                counts[league] += 1
                if counts[league] > 30: continue
            reduced.append(row)
        result = self.run_study(reduced)
        groups = [row for row in result['records'] if row['cohort'] == 'mixed' and row['size'] == 4
                  and row['phase'] == 'calibration']
        self.assertTrue(groups)
        self.assertTrue(all(set(row['methods']) == {'league_baseline'} for row in groups))

    def test_same_utc_day_results_do_not_enter_target_features_or_clubs_predictions(self):
        raw = sample()
        baseline = self.run_study(raw)
        # Additional completed identities are on the target UTC day, so they do
        # alter the slate but cannot contribute to any same-day club profile.
        extra = fixture(98765432, '2026-01-02T00:01:00+00:00', 39, 0)
        history = [api.normalize_fixture(row) for row in raw+[extra] if row['league']['id'] == 39]
        group = [study.workspace.fixture_from_raw(row) for row in raw
                 if row['league']['id'] == 39 and row['fixture']['date'].startswith('2026')][:2]
        builder = study._ContextBuilder(history, '39')
        context = builder.context(group)
        self.assertEqual(context['features'][0]['league_count'], 128)
        self.assertEqual(builder.clubs.leagues['39']['matches'], 128)
        with patch.object(ml, '_fit', side_effect=fit_fake), patch.object(ml, '_predict', side_effect=predict_fake):
            fitted = study._fit_league(history, [row for row in history if row['timestamp'].startswith('2024')], '39', study.PROTOCOL)
        methods = study._mono_methods(group, context, fitted, study._actual(group, {row['match_id']: row for row in history}),
                                     predict_fake(None, group, None, None), study.PROTOCOL)
        expected = next(row for row in baseline['records'] if row['cohort'] == 'mono' and row['match_ids'] == [item['match_id'] for item in group])
        self.assertEqual(methods, expected['methods'])

    def test_whole_boundary_utc_day_is_purged_from_fixed_training(self):
        raw = sample()
        raw.append(fixture(9990001, '2024-12-31T00:10:00+00:00', 39, 0))
        raw.append(fixture(9990002, '2024-12-31T22:10:00+00:00', 39, 1))
        result = self.run_study(raw)
        detail = next(row for row in result['method_details'] if row['league'] == '39')
        self.assertEqual(result['protocol']['training_utc_before'], '2024-12-31')
        self.assertEqual(detail['training_matches'], 120)
        self.assertLess(detail['training_max_utc_day'], '2024-12-31')

    def test_exact_joint_top3_and_log_loss_match_existing_probability_functions(self):
        distributions = [dict(zip(VALUES, (.2, .3, .5))), dict(zip(VALUES, (.4, .35, .25)))]
        actual = ('2nd', '1st')
        ranked, function = study._independent(distributions)
        result = study._metric(ranked, function, distributions, actual)
        self.assertAlmostEqual(result['top1_probability'], .2)
        self.assertAlmostEqual(result['loss_per_match'], -log(.2)/2)
        self.assertAlmostEqual(result['top3_probability'], fsum(row['probability'] for row in ranked))
        fit = fit_sequences([('2nd', '2nd')]*25+[('1st', 'Égal')]*5, 2)
        model = combined._combined_model(fit, distributions)
        ranking = combined._ranking(model, 3)
        metric = study._metric(ranking, lambda seq: combined._sequence_details(model, seq)['probability'], distributions, actual)
        self.assertAlmostEqual(metric['loss_per_match'], -log(combined._sequence_details(model, actual)['probability'])/2)
        self.assertAlmostEqual(metric['top3_probability'], sum(row['probability'] for row in ranking))

    def test_no_api_artifact_or_journal_access(self):
        with ExitStack() as stack:
            for obj, name in [(api, 'load_key'), (api, 'load_fixtures'), (api, 'load_matches'),
                              (api.ApiFootballClient, 'get'), (ml, 'load_report')]:
                if hasattr(obj, name): stack.enter_context(patch.object(obj, name, side_effect=AssertionError(name)))
            stack.enter_context(patch('builtins.open', side_effect=AssertionError('No file I/O')))
            result = self.run_study()
        self.assertTrue(result['records'])

    def test_invalid_sources_conflicting_ids_and_future_periods_are_rejected(self):
        raw = sample(); raw[0]['provider'] = 'other'
        with self.assertRaises(ValueError): self.run_study(raw)
        raw = sample(); raw.append(copy.deepcopy(raw[0])); raw[-1]['teams']['home']['name'] = 'Changed'
        with self.assertRaises(ValueError): self.run_study(raw)
        with self.assertRaises(ValueError): self.run_study(protocol={'end': '2026-01-05'})
        with self.assertRaises(ValueError): self.run_study(protocol={'catboost_parameters': {'depth': 9}})


if __name__ == '__main__': unittest.main()
