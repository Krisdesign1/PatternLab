"""Chronological seasonal selection, common cohorts, and no production writes."""
import copy
from datetime import date, timedelta
import importlib.util
import json
from math import log
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

    def test_explicit_cutoff_preserves_the_frozen_population_and_partial_season_after_clock_changes(self):
        rows = sample()
        rows.append(dict(rows[-1], match_id='999991', timestamp='2026-10-19T12:00:00'))
        with patch.object(evaluation, '_today', return_value='2026-10-18'):
            initial = evaluation.plan(rows, '39')
        with patch.object(evaluation, '_today', return_value='2027-08-01'):
            resumed = evaluation.plan(rows, '39', before='2026-10-18')
            later = evaluation.plan(rows, '39')
        self.assertEqual(initial, resumed)
        self.assertTrue(resumed['folds'][-1]['partial'])
        self.assertFalse(later['folds'][-1]['partial'])
        self.assertNotEqual(initial['fingerprint'], later['fingerprint'])

    def test_invalid_and_future_explicit_cutoffs_are_refused_before_training(self):
        with patch.object(evaluation, '_today', return_value='2026-10-18'), \
             patch.object(evaluation, '_fit', side_effect=AssertionError('No training')):
            for cutoff in ('2026-10-19', '2026-02-30', '2026-1-01', 'bad', True, 20261018):
                for operation in (evaluation.plan, evaluation.run):
                    with self.subTest(cutoff=cutoff, operation=operation.__name__), self.assertRaises(evaluation.SeasonalError):
                        operation(sample(), '39', before=cutoff)


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

    def test_native_procedure_shares_joint_selection_and_global_anchors(self):
        # Uneven seasons distinguish native global blocks from per-season
        # anchors. The same shared selector must choose alpha/params once on4.
        rows=sample(49)
        with patch.object(evaluation,'_fit',side_effect=fake_fit), \
             patch.object(evaluation,'_predict',side_effect=fake_predict), \
             patch.object(ml,'CANDIDATES',({'depth':2,'iterations':8},{'depth':3,'iterations':10})), \
             patch.object(ml,'select_validation',wraps=ml.select_validation) as selector:
            report=evaluation.run(rows,'39',sizes=(2,4,6),procedure='native')
        self.assertEqual(selector.call_count,3)
        self.assertEqual(report['procedure'],'native')
        for fold in report['folds']:
            outputs=list(fold['sizes'].values())
            self.assertEqual(len({json.dumps(o['selection']['params'],sort_keys=True) for o in outputs}),1)
            self.assertEqual(len({o['selection']['alpha'] for o in outputs}),1)
            self.assertEqual({o['selection']['window'] for o in outputs},{'all'})
            split=evaluation._fold_rows(rows,tuple(fold['split']['train']['seasons']),
                fold['split']['validation']['seasons'][0],fold['test_season'])
            scope=sorted(sum(split.values(),[]),key=lambda r:(r['timestamp'],int(r['match_id'])))
            expected=ml._contained(ml._blocks(scope,4),[dict(match=r) for r in split['test']])
            self.assertEqual([v['match_ids'] for v in fold['sizes']['4']['targets']['test']],
                             [[str(r['match_id']) for r in b['matches']] for b in expected])

    def test_native_test_labels_cannot_change_joint_parameters_or_repetition_strength(self):
        with patch.object(ml,'CANDIDATES',({'depth':2,'iterations':8},)):
            original=self.run_fake(sizes=(2,4),procedure='native')
            rows=sample()
            for row in rows:
                if row['season']==2026: row.update(ht='0-0',ft='0-0',value='Égal')
            changed=self.run_fake(rows,sizes=(2,4),procedure='native')
        for size in ('2','4'):
            first=original['folds'][-1]['sizes'][size];second=changed['folds'][-1]['sizes'][size]
            self.assertEqual(first['selection'],second['selection'])
            self.assertEqual(first['validation'],second['validation'])
            self.assertNotEqual(first['test'],second['test'])

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
                diagnostics = output['diagnostics']
                self.assertEqual(diagnostics['version'], evaluation.DIAGNOSTICS_VERSION)
                for method in ('clubs', 'catboost_plain'):
                    self.assertEqual(diagnostics['calibration']['test'][method]['matches'], expected*int(size))
                    self.assertEqual(set(diagnostics['calibration']['test'][method]['reliability']), set(ml.VALUES))
                for method in evaluation.METHODS:
                    coverage = diagnostics['group_coverage']['test'][method]
                    self.assertEqual(coverage['groups'], expected)
                    self.assertAlmostEqual(coverage['observed_top1_rate'], output['test'][method]['top1_rate'])
                    self.assertLessEqual(coverage['mean_top1_probability'], coverage['mean_top3_probability'])
                    self.assertLessEqual(coverage['mean_top3_probability'], 1+1e-12)
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
        self.assertEqual(a['diagnostics']['calibration']['validation'], b['diagnostics']['calibration']['validation'])
        self.assertNotEqual(a['diagnostics']['calibration']['test'], b['diagnostics']['calibration']['test'])

    def test_run_explicit_cutoff_remains_frozen_when_resumed_in_a_later_year(self):
        with patch.object(evaluation, '_today', return_value='2027-08-01'):
            report = self.run_fake(sizes=(4,), before='2026-10-18')
        self.assertEqual(report['cutoff'], '2026-10-18')
        self.assertTrue(report['folds'][-1]['partial'])

    def test_calibration_sizes_preserve_distinct_target_populations(self):
        report = self.run_fake(sizes=(2, 20))
        for fold in report['folds']:
            for method in ('clubs', 'catboost_plain'):
                small = fold['sizes']['2']['diagnostics']['calibration']['test'][method]
                large = fold['sizes']['20']['diagnostics']['calibration']['test'][method]
                self.assertEqual(small['matches'], 48)
                self.assertEqual(large['matches'], 40)
        self.assertFalse(report['protocol']['test_selection'])
        self.assertIn('No learned calibration', report['protocol']['diagnostics'])

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

    def test_legacy_reports_without_probability_diagnostics_remain_readable(self):
        report = self.run_fake(sizes=(4,))
        report['protocol'].pop('diagnostics')
        for fold in report['folds']:
            for output in fold['sizes'].values():
                output.pop('diagnostics')
                for period in ('validation', 'test'):
                    for metrics in output[period].values():
                        metrics.pop('mean_top1_probability')
                        metrics.pop('mean_top3_probability')
        with tempfile.TemporaryDirectory() as td, patch.object(evaluation, 'REPORT_DIR', Path(td)):
            evaluation.save_report(report)
            self.assertEqual(evaluation.load_report('39'), report)


class SeasonalProbabilityDiagnosticsTests(unittest.TestCase):
    def contexts(self):
        distributions = [{'1st': 1., 'Égal': 0., '2nd': 0.}, {'1st': .2, 'Égal': .3, '2nd': .5}]
        group = dict(before='2024-08-01', feature_keys=[('1', '2024-08-01'), ('2', '2024-08-01')],
            clubs=distributions, block=dict(matches=[dict(match_id='1', value='1st'), dict(match_id='2', value='Égal')],
                sequence=['1st', 'Égal']))
        return group, dict(zip(group['feature_keys'], distributions))

    def test_match_metrics_use_multiclass_definition_and_probability_one_final_bin(self):
        group, predictions = self.contexts()
        clubs = evaluation._calibration([group], predictions, 'clubs')
        self.assertEqual(clubs['matches'], 2)
        self.assertAlmostEqual(clubs['logloss'], -log(.3)/2)
        self.assertAlmostEqual(clubs['brier'], .39)
        self.assertEqual(clubs, evaluation._calibration([group], predictions, 'catboost_plain'))
        final = clubs['reliability']['1st'][-1]
        self.assertEqual((final['count'], final['mean_probability'], final['observed_rate']), (1, 1, 1))
        for label in ml.VALUES:
            bins = clubs['reliability'][label]
            self.assertEqual(sum(row['count'] for row in bins), 2)
            self.assertEqual(len(bins), 10)
            self.assertTrue(all(row['mean_probability'] is None and row['observed_rate'] is None
                                for row in bins if row['count'] == 0))

    def test_repeated_fixture_cutoffs_are_deduplicated_and_conflicts_refused(self):
        group, predictions = self.contexts()
        self.assertEqual(evaluation._calibration([group, copy.deepcopy(group)], predictions, 'clubs')['matches'], 2)
        later = copy.deepcopy(group)
        later['before'] = '2024-08-02'
        self.assertEqual(evaluation._calibration([group, later], predictions, 'clubs')['matches'], 4)
        conflicting = copy.deepcopy(group)
        conflicting['block']['matches'][0]['value'] = '2nd'
        with self.assertRaises(evaluation.SeasonalError):
            evaluation._calibration([group, conflicting], predictions, 'clubs')

    def test_group_coverage_is_full_distribution_mass_without_display_renormalization(self):
        group, predictions = self.contexts()
        group['clubs'] = [{'1st': .6, 'Égal': .25, '2nd': .15}, {'1st': .2, 'Égal': .3, '2nd': .5}]
        predictions = dict(zip(group['feature_keys'], group['clubs']))
        group['block']['sequence'] = ['1st', '2nd']
        fitted = evaluation.fit_sequences([('1st', 'Égal'), ('2nd', '2nd')], 2)
        metrics = evaluation._metrics([group], fitted, 'catboost_plain', predictions)
        probabilities = sorted((a*b for a in group['clubs'][0].values() for b in group['clubs'][1].values()), reverse=True)
        self.assertAlmostEqual(metrics['mean_top1_probability'], probabilities[0])
        self.assertAlmostEqual(metrics['mean_top3_probability'], sum(probabilities[:3]))
        self.assertLess(metrics['mean_top3_probability'], 1)
        self.assertEqual(metrics['top1_rate'], 1)


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
