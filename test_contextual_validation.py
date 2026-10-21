"""Selection isolation, shared seasonal cohorts, and explicit report persistence."""
import copy
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

import api_football
import catboost_scenarios as ml
import contextual_validation as evaluation
import seasonal_validation as seasonal
from test_seasonal_validation import fake_fit, fake_predict, sample
from scripts import evaluate_context_neighbors as cli


class ContextualValidationTests(unittest.TestCase):
    def run_fake(self, rows=None, **kwargs):
        with patch.object(seasonal, '_fit', side_effect=fake_fit), \
             patch.object(seasonal, '_predict', side_effect=fake_predict), \
             patch.object(evaluation, 'CANDIDATES', ({'depth': 2, 'iterations': 8}, {'depth': 3, 'iterations': 10})):
            return evaluation.run(sample() if rows is None else rows, '39', **kwargs)

    def test_common_cohorts_all_sizes_and_weighted_aggregates(self):
        progress = []
        report = self.run_fake(progress=lambda fraction, text: progress.append(fraction))
        self.assertTrue(report['exploratory'])
        self.assertEqual(report['sizes'], [2, 4, 6, 8])
        self.assertEqual([fold['test_season'] for fold in report['folds']], [2024, 2025, 2026])
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 1)
        self.assertFalse(report['protocol']['test_selection'])
        for fold in report['folds']:
            for size, output in fold['sizes'].items():
                targets = output['targets']['test']
                self.assertEqual(set(output['test']), set(evaluation.METHODS))
                self.assertEqual({metric['tested'] for metric in output['test'].values()}, {len(targets)})
                self.assertEqual({metric['tested'] for metric in output['validation'].values()},
                                 {len(output['targets']['validation'])})
                self.assertTrue(all(len(target['match_ids']) == int(size) for target in targets))
                self.assertEqual(output['selection']['catboost_parameter_size'], 4)
                self.assertIn(output['selection']['params'], evaluation.PARAMETERS)
                self.assertEqual(len(output['selection']['trials']), 16)
                self.assertIn('mean_eta', output['support_summary']['test'])
        for size in report['sizes']:
            key = str(size)
            for method in evaluation.METHODS:
                metrics = report['aggregate'][key][method]
                parts = [fold['sizes'][key]['test'][method] for fold in report['folds']]
                self.assertEqual(metrics['tested'], sum(part['tested'] for part in parts))
                self.assertAlmostEqual(metrics['loss'], sum(part['loss']*part['tested'] for part in parts)/metrics['tested'])
                self.assertEqual(report['aggregate_partial'][key][method]['tested'], parts[-1]['tested'])
                self.assertEqual(report['aggregate_complete'][key][method]['tested'], sum(part['tested'] for part in parts[:-1]))
        json.dumps(report, allow_nan=False)

    def test_catalogues_contain_only_training_then_training_plus_validation_groups(self):
        actual = evaluation.neighbors.fit_catalog
        calls = []
        def capture(contexts):
            calls.append(copy.deepcopy(contexts))
            return actual(contexts)
        with patch.object(evaluation.neighbors, 'fit_catalog', side_effect=capture):
            self.run_fake(sizes=(4,))
        self.assertEqual(len(calls), 6)
        for index, test_season in enumerate((2024, 2025, 2026)):
            train = calls[index*2]
            train_val = calls[index*2+1]
            self.assertEqual({context['block']['season'] for context in train}, set(range(2022, test_season-1)))
            self.assertEqual({context['block']['season'] for context in train_val}, set(range(2022, test_season)))
            self.assertTrue(all(context['block']['season'] < test_season for context in train_val))
            self.assertLess(max(context['before'] for context in train_val), f'{test_season}-08-01')

    def test_final_test_labels_cannot_choose_neighbor_configuration_or_catboost_settings(self):
        initial = self.run_fake(sizes=(2, 4))
        changed_rows = sample()
        for row in changed_rows:
            if row['season'] == 2026:
                row.update(ht='0-0', ft='0-0', value='Égal')
        changed = self.run_fake(changed_rows, sizes=(2, 4))
        first, second = initial['folds'][-1], changed['folds'][-1]
        for size in ('2', '4'):
            self.assertEqual(first['sizes'][size]['selection'], second['sizes'][size]['selection'])
            self.assertEqual(first['sizes'][size]['validation'], second['sizes'][size]['validation'])
            self.assertNotEqual(first['sizes'][size]['test'], second['sizes'][size]['test'])
        self.assertEqual(initial['deployment']['parameters'], changed['deployment']['parameters'])
        self.assertEqual(initial['deployment']['generation'], changed['deployment']['generation'])
        self.assertEqual(initial['deployment']['selection_fingerprint'], changed['deployment']['selection_fingerprint'])
        self.assertNotEqual(initial['fingerprint'], changed['fingerprint'])

    def test_exact_validation_ties_keep_zero_mix_then_the_stable_grid_order(self):
        actual = evaluation._neighbor_metrics
        def tied(catalogue, groups, params):
            metrics, support = actual(catalogue, groups, params)
            metrics['loss'] = 1.0
            return metrics, support
        with patch.object(evaluation, '_neighbor_metrics', side_effect=tied):
            report = self.run_fake(sizes=(4,))
        expected = {'k': 20, 'half_life_days': None, 'mix': 0.0}
        self.assertTrue(all(fold['sizes']['4']['selection']['params'] == expected for fold in report['folds']))
        self.assertEqual(report['deployment']['parameters'], {'4': expected})

    def test_deployment_uses_latest_validation_only_and_protects_its_input_fingerprint(self):
        rows = sample()
        report = self.run_fake(rows, sizes=(2, 4))
        configuration = report['deployment']
        final = report['folds'][-1]
        self.assertEqual(configuration['selection_through'], final['split']['validation']['end'])
        self.assertEqual(configuration['selection_test_season'], 2026)
        self.assertEqual(configuration['parameters'],
                         {size: output['selection']['params'] for size, output in final['sizes'].items()})
        self.assertFalse(configuration['test_used_for_selection'])
        history, _, _ = seasonal._history(rows, '39', 'API-Football')
        selection_rows = [row for row in history if row['timestamp'][:10] <= configuration['selection_through']]
        self.assertEqual(configuration['selection_fingerprint'], ml._fingerprint(selection_rows))
        self.assertEqual(len(configuration['generation']), 64)
        self.assertEqual(set(configuration['parameters']), {'2', '4'})

    def test_source_and_future_results_are_excluded_before_validation_and_run_has_no_side_effects(self):
        rows = sample()
        before = copy.deepcopy(rows)
        other = [dict(rows[0], league='61', ht=None, value='invalid'),
                 dict(rows[0], provider='other', ht=None, value='invalid'),
                 dict(rows[0], match_id='99999', timestamp='2099-08-01T12:00:00', ht=None, value='invalid')]
        with patch.object(ml, '_publish', side_effect=AssertionError('No CatBoost publication')), \
             patch.object(api_football, 'save_fixtures', side_effect=AssertionError('No provider mutation')), \
             patch.object(api_football, 'ApiFootballClient', side_effect=AssertionError('No network')), \
             patch.object(evaluation, 'save_report', side_effect=AssertionError('Saving must be explicit')):
            report = self.run_fake(rows+other, sizes=(4,))
        self.assertEqual(rows, before)
        self.assertEqual(report['source'], 'API-Football')
        self.assertEqual(report['fingerprint'], evaluation.plan(rows, '39')['fingerprint'])
        with self.assertRaises(ValueError):
            evaluation.run(rows, '39', source='football-data.org')
        for sizes in ((), (True,), (1,), (21,), (4, 4)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                self.run_fake(sizes=sizes)

    def test_atomic_report_roundtrip_and_failed_save_preserve_previous_configuration(self):
        report = self.run_fake(sizes=(4,))
        with tempfile.TemporaryDirectory() as directory, patch.object(evaluation, 'REPORT_DIR', Path(directory)):
            self.assertIsNone(evaluation.load_report('39'))
            path = evaluation.save_report(report)
            self.assertEqual(evaluation.load_report('39'), report)
            previous = path.read_bytes()
            with patch.object(evaluation.os, 'replace', side_effect=OSError('Simulated write failure')):
                with self.assertRaises(OSError):
                    evaluation.save_report(dict(report, generated_at='later attempt'))
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            path.write_text('{invalid')
            with self.assertRaises(evaluation.ContextualError):
                evaluation.load_report('39')


@unittest.skipUnless(importlib.util.find_spec('catboost'), 'CatBoost runtime unavailable')
class NativeContextualValidationTests(unittest.TestCase):
    def test_real_catboost_fit_and_context_distributions_without_publishing_artifacts(self):
        with patch.object(evaluation, 'CANDIDATES', ({'depth': 2, 'iterations': 5, 'l2_leaf_reg': 5},)), \
             patch.object(seasonal, 'FOLDS', (((2022,), 2023, 2024),)), \
             patch.object(ml, '_publish', side_effect=AssertionError('No production model publication')):
            report = evaluation.run(sample(), '39', sizes=(2, 4))
        self.assertEqual(len(report['folds']), 1)
        self.assertEqual(report['folds'][0]['sizes']['4']['test']['context_neighbors']['tested'], 12)
        self.assertTrue(report['exploratory'])
        json.dumps(report, allow_nan=False)


class ContextualEvaluationCommandTests(unittest.TestCase):
    def report(self):
        return dict(aggregate_complete={}, aggregate_partial={}, deployment={})

    def fixtures(self):
        return [dict(fixture={'id': 1}, league={'id': 39}),
                dict(fixture={'id': 2}, league={'id': 61}),
                dict(fixture={'id': 3}, league={'id': 140})]

    def test_other_league_score_issues_do_not_block_the_selected_league_or_mutate_raw_data(self):
        raw = self.fixtures()
        before = copy.deepcopy(raw)
        rows = sample()
        with patch.object(sys, 'argv', ['evaluate_context_neighbors.py', '39', '--sizes', '2', '4']), \
             patch.object(cli.campaign, 'load_manifest', return_value=None), \
             patch.object(api_football, 'load_matches', return_value=(rows, {'issues': [{'match_id': '3', 'reason': 'HT missing'}]})), \
             patch.object(api_football, 'load_fixtures', return_value=raw), \
             patch.object(api_football, 'save_fixtures', side_effect=AssertionError('No provider write')), \
             patch.object(evaluation, 'run', return_value=self.report()) as run, \
             patch.object(evaluation, 'save_report', return_value=Path('/tmp/contextual-test.json')) as save, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        run.assert_called_once()
        self.assertEqual(run.call_args.args, (rows, '39'))
        self.assertEqual(run.call_args.kwargs['sizes'], (2, 4))
        save.assert_called_once()
        self.assertIn('autres compétitions', output.getvalue())
        self.assertEqual(raw, before)

    def test_selected_league_score_issue_blocks_before_any_training_or_report_write(self):
        with patch.object(sys, 'argv', ['evaluate_context_neighbors.py', '39']), \
             patch.object(cli.campaign, 'load_manifest', return_value=None), \
             patch.object(api_football, 'load_matches', return_value=([], {'issues': [{'match_id': '1', 'reason': 'HT missing'}]})), \
             patch.object(api_football, 'load_fixtures', return_value=self.fixtures()), \
             patch.object(evaluation, 'run') as run, patch.object(evaluation, 'save_report') as save, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
