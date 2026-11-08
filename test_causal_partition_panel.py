"""The partition inspector performs work only on an explicit request."""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

import causal_partition_panel as panel


def prepared():
    boundaries = ['2026-07-01', '2026-08-01', '2026-09-01', '2026-10-01', '2026-11-01']
    plan = dict(id='a' * 64, windows=panel.windows_from_boundaries(boundaries))
    phases = {name: dict(requested=2, included=2, usable_labels=0,
                        classes=None if name == 'confirmation' else {'1st': 0, 'Égal': 0, '2nd': 0})
              for name in panel.PHASES}
    partition = dict(id='b' * 64, cohort_id='c' * 64, phases=phases,
                     ready=False, blockers=['train: pas assez de labels admissibles'])
    return plan, partition


class CausalPartitionPanelTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.partition = prepared()
        self.patches = [patch.object(panel.artifacts, 'load_bundle', return_value={'test': 'bundle'}),
                        patch.object(panel.partitions, 'make_plan', return_value=self.plan),
                        patch.object(panel.partitions, 'prepare', return_value=self.partition)]
        self.load, self.make, self.prepare = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.at = AppTest.from_string('from causal_partition_panel import render\nrender()',
                                     default_timeout=10).run()

    def submit(self):
        self.at.text_input(key='causal_partition_directory').set_value('test/bundle')
        for index, value in enumerate(['2026-07-01', '2026-08-01', '2026-09-01', '2026-10-01', '2026-11-01']):
            self.at.text_input(key=f'causal_partition_boundary_{index}').set_value(value)
        return self.at.button(key='causal_partition_prepare').click().run()

    def test_opening_and_rerunning_never_read_or_prepare_a_cohort(self):
        self.assertFalse(self.at.exception)
        self.load.assert_not_called(); self.prepare.assert_not_called()
        self.at.run()
        self.load.assert_not_called(); self.prepare.assert_not_called()
        self.submit()
        self.assertFalse(self.at.exception)
        self.load.assert_called_once(); self.prepare.assert_called_once()
        self.assertEqual(len(self.at.dataframe[0].value), 4)
        self.assertEqual(self.at.dataframe[0].value.iloc[3]['Labels utilisables'], 'Réservés')
        self.assertEqual(self.at.dataframe[0].value.iloc[3]['Classes observées'], 'Non utilisées')
        self.at.run()
        self.assertEqual(self.load.call_count, 1)
        self.assertEqual(self.prepare.call_count, 1)

    def test_invalid_new_request_does_not_keep_a_previous_result(self):
        self.submit()
        self.assertTrue(self.at.dataframe)
        self.prepare.side_effect = ValueError('Bornes non croissantes')
        self.submit()
        self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)

    def test_ready_result_does_not_claim_a_predictive_gain(self):
        changed = deepcopy(self.partition)
        changed['ready'] = True; changed['blockers'] = []
        self.prepare.return_value = changed
        self.submit()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('ne démontre pas' in row.value for row in self.at.success))


class CausalPartitionCliTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent / 'scripts' / 'prepare_causal_partitions.py'
        spec = importlib.util.spec_from_file_location('partition_cli_under_test', path)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.plan, self.partition = prepared()

    def run_cli(self, arguments):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['prepare_causal_partitions.py', *arguments]), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.cli.main()
        return output.getvalue()

    def test_plan_prepares_then_reloads_without_any_fit(self):
        bundle = {'test': 'bundle'}
        candidate = SimpleNamespace(fit_candidate=Mock(side_effect=AssertionError('No model fit')))
        with patch.object(panel.artifacts, 'load_bundle', return_value=bundle), \
                patch.object(panel.partitions, 'make_plan', return_value=self.plan) as make, \
                patch.object(panel.partitions, 'prepare', return_value=self.partition), \
                patch.object(panel.partitions, 'write_partition_bundle') as write, \
                patch.object(panel.partitions, 'load_partition_bundle', return_value={'partition': self.partition}), \
                patch.dict(sys.modules, {'causal_candidate': candidate}):
            result = self.run_cli(['plan', '--bundle', 'input', '--boundaries',
                                   '2026-07-01', '2026-08-01', '2026-09-01', '2026-10-01', '2026-11-01',
                                   '--output', str(self.root / 'new')])
        make.assert_called_once_with(self.plan['windows'])
        write.assert_called_once_with(self.root / 'new', bundle, self.plan, self.partition)
        candidate.fit_candidate.assert_not_called()
        self.assertIn('"state": "verified"', result)
        self.assertIn('"ready": false', result)

    def test_verify_only_reads_the_partition_package(self):
        with patch.object(panel.partitions, 'load_partition_bundle', return_value={'partition': self.partition}) as load, \
                patch.object(panel.artifacts, 'load_bundle', side_effect=AssertionError('No source access')), \
                patch.object(panel.partitions, 'prepare', side_effect=AssertionError('No new cohort')):
            result = self.run_cli(['verify', str(self.root / 'saved')])
        load.assert_called_once_with(self.root / 'saved')
        self.assertIn('"state": "verified"', result)

    def test_existing_training_output_refuses_before_loading_or_fitting(self):
        with patch.object(panel.partitions, 'load_partition_bundle') as load, self.assertRaises(SystemExit) as error:
            self.run_cli(['train', '--partition', 'saved', '--output', str(self.root)])
        self.assertEqual(error.exception.code, 2)
        load.assert_not_called()

    def test_candidate_rejection_never_writes_an_artifact(self):
        candidate = SimpleNamespace(fit_candidate=Mock(side_effect=ValueError('Données insuffisantes')),
                                    write_candidate=Mock(), load_candidate=Mock())
        with patch.object(panel.partitions, 'load_partition_bundle',
                          return_value={'partition': self.partition, 'bundle': {}}), \
                patch.object(panel.partitions, 'development_inputs', return_value={'ready': False}), \
                patch.dict(sys.modules, {'causal_candidate': candidate}), self.assertRaises(SystemExit) as error:
            self.run_cli(['train', '--partition', 'saved', '--output', str(self.root / 'candidate')])
        self.assertEqual(error.exception.code, 2)
        candidate.write_candidate.assert_not_called()
        candidate.load_candidate.assert_not_called()


LOCAL_BUNDLE = Path(__file__).parent / 'data/research/causal_features/lot03-20261030-verifie'


@unittest.skipUnless(LOCAL_BUNDLE.is_dir(), 'Preserved local technical bundle is unavailable')
class CausalPartitionRealBundleTests(unittest.TestCase):
    def submit(self, boundaries):
        at = AppTest.from_string('from causal_partition_panel import render\nrender()', default_timeout=30).run()
        at.text_input(key='causal_partition_directory').set_value(str(LOCAL_BUNDLE))
        for index, value in enumerate(boundaries):
            at.text_input(key=f'causal_partition_boundary_{index}').set_value(value)
        with patch('causal_candidate.fit_candidate', side_effect=AssertionError('No model fit')) as fit:
            at.button(key='causal_partition_prepare').click().run()
            fit.assert_not_called()
        self.assertFalse(at.exception)
        self.assertFalse(at.error)
        self.assertEqual(len(at.dataframe[0].value), 4)
        return at

    def test_real_two_targets_do_not_become_an_adequate_development_sample(self):
        at = self.submit(['2026-10-28', '2026-10-30', '2026-10-31', '2026-11-01', '2026-11-02'])
        periods = at.dataframe[0].value
        self.assertEqual(periods.iloc[0]['Cibles retenues'], 2)
        self.assertTrue(at.warning)
        self.assertGreater(len(at.dataframe[1].value), 0)

    def test_real_confirmation_rows_never_display_their_known_labels(self):
        at = self.submit(['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30'])
        confirmation = at.dataframe[0].value.iloc[3]
        self.assertEqual(confirmation['Cibles retenues'], 2)
        self.assertEqual(confirmation['Labels utilisables'], 'Réservés')
        self.assertEqual(confirmation['Classes observées'], 'Non utilisées')
        result = at.session_state['causal_partition_result']['partition']
        self.assertFalse(result['confirmation_labels_used'])
        self.assertTrue(all(item['label_status'] == 'withheld'
                            for item in result['phases']['confirmation']['entries']))


if __name__ == '__main__':
    unittest.main()
