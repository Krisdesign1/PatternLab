"""Chronological seasonal selection, common cohorts, and no production writes."""
import copy
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football
import catboost_scenarios as ml
import scenario_features as features
import seasonal_validation as evaluation


def sample(count=48):
    scores = (('2-0', '2-0', '1st'), ('1-0', '1-1', 'Égal'), ('0-0', '1-0', '2nd'))
    rows = []
    for year in range(2022, 2027):
        for index in range(count):
            ht, ft, value = scores[index % 3]
            day = date(year, 8, 1) + timedelta(days=index//4)
            rows.append(dict(match_id=str(year*1000+index), league='39', provider='api-football',
                timestamp=f'{day.isoformat()}T{12+index%4:02}:00:00', season=year,
                home='Home'+str(index%4), away='Away'+str(index%5),
                home_id=str(100+index%4), away_id=str(200+index%5), ht=ht, ft=ft, value=value))
    return rows


def fake_fit(rows, params):
    counts = {label: sum(r['value'] == label for r in rows)+1 for label in ml.VALUES}
    # A deterministic learned prior plus a parameter-dependent tilt is enough
    # to test selection isolation without repeatedly fitting a native model.
    counts['2nd'] += params['depth']
    total = sum(counts.values())
    return {label: n/total for label, n in counts.items()}


def fake_predict(model, contexts):
    return {key: dict(model) for groups in contexts.values() for group in groups for key in group['feature_keys']}


class SeasonalPlanTests(unittest.TestCase):
    def test_actual_seasons_and_whole_utc_days_define_the_three_fold_plan(self):
        planned = evaluation.plan(sample(), '39')
        self.assertTrue(planned['suitable'])
        self.assertEqual([r['season'] for r in planned['seasons']], list(range(2022, 2027)))
        self.assertEqual([r['test_season'] for r in planned['folds']], [2024, 2025, 2026])
        for fold in planned['folds']:
            self.assertLess(fold['train']['end'], fold['validation']['start'])
            self.assertLess(fold['validation']['end'], fold['test']['start'])
        self.assertEqual([r['partial'] for r in planned['folds']], [False, False, True])

    def test_source_and_future_rows_are_filtered_before_scores_are_read(self):
        rows = sample()
        future = dict(rows[0], match_id='9999999', timestamp='2099-08-01T12:00:00Z', ht=None, ft=None, value='invalid')
        other = [dict(rows[0], league='61', value='invalid'), dict(rows[0], provider='other', value='invalid')]
        self.assertEqual(evaluation.plan(rows, '39'), evaluation.plan(rows+[future]+other, '39'))
        for source in ('football-data.org', 'Forebet', 'api-football'):
            with self.assertRaises(evaluation.SeasonalError):
                evaluation.plan(rows, '39', source)
        with self.assertRaises(evaluation.SeasonalError): evaluation.plan(rows, 'PL')
        with self.assertRaises(evaluation.SeasonalError): evaluation.plan([dict(rows[0], season=None)], '39')

    def test_late_previous_season_rows_are_excluded_without_sharing_a_boundary_day(self):
        rows = sample()
        rows.append(dict(rows[0], match_id='999900', timestamp='2023-08-01T10:00:00'))
        fold = evaluation.plan(rows, '39')['folds'][0]
        self.assertEqual(fold['excluded_boundary_matches'], 1)
        self.assertEqual(fold['train']['matches'], 48)
        split = evaluation._fold_rows(rows, (2022,), 2023, 2024)
        self.assertTrue(all(row['timestamp'][:10] < '2023-08-01' for row in split['train']))
        grouped = evaluation._groups(rows, split['train'], 4)
        self.assertTrue(all('999900' not in [r['match_id'] for r in group['matches']] for group in grouped))

    def test_group_anchors_are_per_season_and_simultaneous_kickoffs_use_numeric_ids(self):
        rows = sample(49)
        groups = evaluation._groups(rows, rows, 4)
        self.assertEqual(len(groups), 5*12)
        self.assertTrue(all(len({r['season'] for r in block['matches']}) == 1 for block in groups))
        self.assertEqual(groups[12]['matches'][0]['match_id'], '2023000')
        equal_time = [dict(r, timestamp='2022-08-01T12:00:00') for r in rows[:8]]
        self.assertEqual(evaluation._groups(equal_time[::-1], equal_time, 4)[0]['sequence'],
                         evaluation._groups(equal_time, equal_time, 4)[0]['sequence'])

    def test_insufficient_history_returns_diagnostics_without_training(self):
        with patch.object(evaluation, '_fit', side_effect=AssertionError('No automatic fit')):
            planned = evaluation.plan(sample(12), '39')
        self.assertFalse(planned['suitable'])
        self.assertTrue(planned['diagnostics'])

    def test_january_does_not_mark_the_previous_autumn_season_complete(self):
        with patch.object(evaluation, '_today', return_value='2027-01-18'):
            planned = evaluation.plan(sample(), '39')
        self.assertTrue(planned['folds'][-1]['partial'])


class SeasonalCausalTests(unittest.TestCase):
    def test_incremental_group_features_match_reference_and_never_use_earlier_group_members(self):
        rows = sample()
        selected = [r for r in rows if r['season'] == 2023]
        blocks = {size: evaluation._groups(rows, selected, size) for size in (2, 4, 6, 8)}
        contexts = evaluation._contexts(rows, blocks)
        cross_day = next(group for group in contexts[6]
                         if len({r['timestamp'][:10] for r in group['block']['matches']}) > 1)
        for row, actual in zip(cross_day['block']['matches'], cross_day['features']):
            expected = features.feature_for_fixture(rows, row, before=cross_day['before'])
            self.assertEqual(actual, expected)
        changed = copy.deepcopy(rows)
        for row in changed:
            if row['timestamp'][:10] >= cross_day['before']:
                row.update(ht='7-0', ft='7-0', value='1st')
        altered = evaluation._contexts(changed, {6: [cross_day['block']]})[6][0]
        self.assertEqual(altered['features'], cross_day['features'])
        self.assertEqual(altered['clubs'], cross_day['clubs'])

    def test_repetition_windows_use_only_their_training_seasons(self):
        rows = sample()
        train = [r for r in rows if r['season'] in (2022, 2023)]
        all_fit = evaluation._repeat_fit(rows, train, 4, 'all')
        last_fit = evaluation._repeat_fit(rows, train, 4, 'last_season')
        self.assertEqual(all_fit[1], 24)
        self.assertEqual(last_fit[1], 12)
        changed = [dict(r, value='1st') if r['season'] >= 2024 else r for r in rows]
        self.assertEqual(evaluation._repeat_fit(changed, train, 4, 'all'), all_fit)


class SeasonalRunTests(unittest.TestCase):
    def run_fake(self, rows=None, **kwargs):
        with patch.object(evaluation, '_fit', side_effect=fake_fit), \
             patch.object(evaluation, '_predict', side_effect=fake_predict), \
             patch.object(evaluation, 'CANDIDATES', ({'depth': 2, 'iterations': 8}, {'depth': 3, 'iterations': 10})):
            return evaluation.run(rows or sample(), '39', **kwargs)

    def test_all_methods_share_target_cohorts_and_sizes_are_never_pooled(self):
        progress = []
        report = self.run_fake(progress=lambda fraction, text: progress.append(fraction))
        self.assertEqual(report['sizes'], [2, 4, 6, 8])
        self.assertFalse(report['protocol']['test_selection'])
        self.assertEqual(progress[-1], 1)
        self.assertEqual(progress, sorted(progress))
        for fold in report['folds']:
            for size, output in fold['sizes'].items():
                expected = len(output['targets']['test'])
                self.assertEqual(set(output['test']), set(evaluation.METHODS))
                self.assertEqual({r['tested'] for r in output['test'].values()}, {expected})
                self.assertTrue(all(len(r['match_ids']) == int(size) for r in output['targets']['test']))
                self.assertEqual(output['selection']['parameter_size'], 4)
                self.assertIn(output['selection']['alpha'], evaluation.ALPHAS)
                self.assertIn(output['selection']['window'], evaluation.WINDOWS)
        for size in report['sizes']:
            key = str(size)
            for name in evaluation.METHODS:
                metrics = report['aggregate'][key][name]
                values = [f['sizes'][key]['test'][name] for f in report['folds']]
                self.assertEqual(metrics['tested'], sum(r['tested'] for r in values))
                self.assertAlmostEqual(metrics['loss'], sum(r['loss']*r['tested'] for r in values)/metrics['tested'])
                self.assertEqual(report['aggregate_partial'][key][name]['tested'], values[-1]['tested'])

    def test_exact_validation_ties_favor_alpha_zero_and_all_training_seasons(self):
        original = evaluation._metrics
        def tied(groups, fit, name, predictions=None, alpha=0):
            output = original(groups, fit, name, predictions, alpha)
            if name == 'catboost_repeats':
                output['loss'] = 1.0
            return output
        with patch.object(evaluation, '_metrics', side_effect=tied):
            report = self.run_fake(sizes=(4,))
        self.assertTrue(all(f['sizes']['4']['selection']['alpha'] == 0 for f in report['folds']))
        self.assertTrue(all(f['sizes']['4']['selection']['window'] == 'all' for f in report['folds']))

    def test_final_test_labels_cannot_select_configuration_alpha_or_window(self):
        original = self.run_fake(sizes=(4,))
        rows = sample()
        for row in rows:
            if row['season'] == 2026:
                row.update(ht='0-0', ft='0-0', value='Égal')
        changed = self.run_fake(rows, sizes=(4,))
        a, b = original['folds'][-1]['sizes']['4'], changed['folds'][-1]['sizes']['4']
        self.assertEqual(a['selection'], b['selection'])
        self.assertEqual(a['validation'], b['validation'])
        self.assertNotEqual(a['test'], b['test'])

    def test_run_never_mutates_or_publishes_data_models_reports_or_journal(self):
        rows = sample()
        before = copy.deepcopy(rows)
        with patch.object(ml, '_publish', side_effect=AssertionError('No model publication')), \
             patch.object(api_football, 'save_fixtures', side_effect=AssertionError('No provider mutation')), \
             patch.object(api_football, 'ApiFootballClient', side_effect=AssertionError('No network')), \
             patch.object(evaluation, 'save_report', side_effect=AssertionError('Saving must be explicit')):
            report = self.run_fake(rows, sizes=(2, 20))
        self.assertEqual(rows, before)
        self.assertEqual(report['sizes'], [2, 20])
        self.assertTrue(all('20' in fold['sizes'] for fold in report['folds']))
        for invalid in ((), (True,), (1,), (21,), (4, 4)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.run_fake(sizes=invalid)

    def test_explicit_atomic_report_roundtrip_and_failure_preserve_previous_report(self):
        report = self.run_fake(sizes=(4,))
        with tempfile.TemporaryDirectory() as td, patch.object(evaluation, 'REPORT_DIR', Path(td)):
            self.assertIsNone(evaluation.load_report('39'))
            path = evaluation.save_report(report)
            self.assertEqual(evaluation.load_report('39'), report)
            original = path.read_bytes()
            changed = dict(report, generated_at='a later attempt')
            with patch.object(evaluation.os, 'replace', side_effect=OSError('Simulated write failure')):
                with self.assertRaises(OSError): evaluation.save_report(changed)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(td).glob('*.tmp')), [])
            path.write_text('{invalid')
            with self.assertRaises(evaluation.SeasonalError): evaluation.load_report('39')


@unittest.skipUnless(importlib.util.find_spec('catboost'), 'CatBoost runtime unavailable')
class NativeSeasonalTests(unittest.TestCase):
    def test_actual_catboost_fit_batch_prediction_and_no_native_artifact_publication(self):
        with patch.object(evaluation, 'CANDIDATES', ({'depth': 2, 'iterations': 5, 'l2_leaf_reg': 5},)), \
             patch.object(ml, '_publish', side_effect=AssertionError('Never publish a seasonal model')):
            report = evaluation.run(sample(), '39', sizes=(4,))
        self.assertEqual(len(report['folds']), 3)
        self.assertTrue(all(f['sizes']['4']['test']['catboost_plain']['tested'] == 12 for f in report['folds']))
        json.dumps(report, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
