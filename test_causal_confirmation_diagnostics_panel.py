from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

import causal_confirmation_panel as ledger
import causal_confirmation_diagnostics_panel as panel
from test_causal_confirmation_panel import report, table


def sample_report():
    value = report(rows=True)
    row = value['protocols'][0]['rows'][0]
    row.update(forecast_id='1'*64, admissible=True, timing_quality='local_commit_ack')
    row['methods']['candidate'] = {'1st': .2, 'Égal': .2, '2nd': .6}
    other = deepcopy(row)
    other.update(forecast_id='2'*64, status='pending', actual=None, ht=None, ft=None)
    other['fixture']['match_id'] = '2'
    value['protocols'][0]['rows'].append(other)
    return value


class DiagnosticsPanelTests(unittest.TestCase):
    def setUp(self):
        self.source = sample_report()
        self.read_patch = patch.object(ledger, '_load_report', return_value=self.source)
        self.read = self.read_patch.start(); self.addCleanup(self.read_patch.stop)
        self.at = AppTest.from_string('from causal_confirmation_panel import render\nrender()', default_timeout=10).run()

    def open_diagnostics(self):
        self.at.button(key='causal_confirmation_read').click().run()
        self.at.segmented_control(key='causal_confirmation_view').set_value('diagnostics').run()
        return self.at

    def test_diagnostics_load_only_on_selected_view_and_never_reread_on_filters(self):
        with patch.object(panel, 'build', wraps=panel.build) as build:
            self.assertFalse(self.at.exception); self.read.assert_not_called(); build.assert_not_called()
            self.at.button(key='causal_confirmation_read').click().run()
            build.assert_not_called()
            self.at.segmented_control(key='causal_confirmation_view').set_value('diagnostics').run()
            self.assertFalse(self.at.exception); build.assert_called_once_with(self.source['protocols'][0])
            self.at.selectbox(key='confirmation_diagnostics_method').set_value('clubs').run()
            self.at.segmented_control(key='confirmation_diagnostics_label').set_value('1st').run()
            self.read.assert_called_once_with()
            self.assertFalse(self.at.exception)

    def test_display_uses_matched_scores_and_excludes_pending(self):
        self.open_diagnostics()
        self.assertFalse(self.at.exception)
        metrics = table(self.at, 'Erreur logarithmique ↓')
        self.assertEqual(set(metrics['Méthode']), set(panel.METHOD_NAMES.values()))
        self.assertTrue(all(metrics['Mêmes matchs évalués'] == 1))
        self.assertTrue(all(metrics['Choix corrects (%)'] == 100))
        self.assertEqual(table(self.at, 'Motif').iloc[0]['Motif'], 'En attente')
        deltas = table(self.at, 'Écart logarithmique ↓')
        self.assertTrue(all(deltas['Écart logarithmique ↓'] < 0))

    def test_calibration_shows_empty_bins_as_missing_not_zero_accuracy(self):
        self.open_diagnostics()
        frame = table(self.at, 'Pourcentages annoncés')
        self.assertEqual(len(frame), 10)
        self.assertEqual(frame['Matchs évalués'].sum(), 1)
        selected = frame[frame['Matchs évalués'] == 1].iloc[0]
        self.assertEqual(selected['Probabilité moyenne (%)'], 60.)
        self.assertEqual(selected['Fréquence observée (%)'], 100.)
        self.assertEqual(selected['Observé − annoncé (points)'], 40.)
        self.assertTrue(frame[frame['Matchs évalués'] == 0]['Fréquence observée (%)'].isna().all())
        self.at.segmented_control(key='confirmation_diagnostics_label').set_value('favorite').run()
        self.assertFalse(self.at.exception)

    def test_empty_realistic_protocol_does_not_show_false_metrics(self):
        self.read.return_value = report()
        self.open_diagnostics()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun résultat admissible' in row.value for row in self.at.info))
        self.assertFalse(any('Erreur logarithmique ↓' in frame.value.columns for frame in self.at.dataframe))
        self.assertFalse(any(widget.key == 'confirmation_diagnostics_method' for widget in self.at.selectbox))

    def test_no_api_fit_or_history_rebuild_on_display(self):
        import api_football as api
        import causal_candidate
        import causal_confirmation as core
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
                patch.object(causal_candidate, 'fit_candidate', side_effect=AssertionError('No fit')), \
                patch.object(core.store, 'get_history', side_effect=AssertionError('No history')):
            self.open_diagnostics()
            self.assertFalse(self.at.exception)

    def test_bad_data_produces_explicit_error_without_reusing_old_metrics(self):
        self.open_diagnostics()
        self.source['protocols'][0]['rows'][0]['methods']['candidate']['2nd'] = float('nan')
        self.at.button(key='causal_confirmation_read').click().run()
        self.assertFalse(self.at.exception); self.assertTrue(self.at.error)
        self.assertFalse(any('Erreur logarithmique ↓' in frame.value.columns for frame in self.at.dataframe))

    def test_single_day_and_unknown_support_are_stated_without_promotion(self):
        self.open_diagnostics()
        self.assertTrue(any('une seule journée' in row.value for row in self.at.info))
        self.assertTrue(any('restent non renseignés' in row.value for row in self.at.caption))
        self.assertTrue(any('Aucun intervalle de confiance' in row.value for row in self.at.caption))
        self.assertFalse(self.at.success)

    def test_incompatible_protocol_warning_remains_visible_on_diagnostics(self):
        self.source['protocols'][0]['implementation_compatible'] = False
        self.open_diagnostics()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('incompatible' in row.value for row in self.at.warning))
        self.assertIs(panel.build(self.source['protocols'][0])['implementation_compatible'], False)


class DiagnosticsCliTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent/'scripts/track_causal_confirmation.py'
        spec = importlib.util.spec_from_file_location('confirmation_diagnostics_cli_under_test', path)
        self.cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.cli)
        self.core = SimpleNamespace(register=Mock(), reconcile=Mock(), report=Mock(return_value=sample_report()))

    def call(self, args):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['track_causal_confirmation.py', *args]), \
                patch.dict(sys.modules, {'causal_confirmation': self.core}), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.cli.main()
        return output.getvalue()

    def test_command_reports_separate_protocols_without_mutations(self):
        import json
        value = json.loads(self.call(['diagnostics']))
        self.assertEqual(len(value['protocols']), 1)
        self.assertEqual(value['protocols'][0]['counts']['evaluated'], 1)
        self.core.register.assert_not_called(); self.core.reconcile.assert_not_called()

    def test_protocol_selection_does_not_pool_other_protocols(self):
        import json
        other = deepcopy(self.core.report.return_value['protocols'][0]); other['id'] = 'c'*64
        self.core.report.return_value['protocols'].append(other)
        value = json.loads(self.call(['diagnostics', '--protocol', 'c'*64]))
        self.assertEqual([row['protocol_id'] for row in value['protocols']], ['c'*64])

    def test_unknown_protocol_fails_instead_of_showing_another_sample(self):
        with self.assertRaises(SystemExit) as error:
            self.call(['diagnostics', '--protocol', 'missing'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
