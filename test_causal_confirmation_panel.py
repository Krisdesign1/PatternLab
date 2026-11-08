from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

import causal_confirmation_panel as panel


def report(*, rows=False, metrics=False):
    p = {'1st': .25, 'Égal': .25, '2nd': .5}
    protocol = dict(id='a'*64, candidate_id='b'*64, registered_at='2026-10-30T23:00:00Z',
        confirmation_start='2026-10-31T00:00:00Z', confirmation_end='2026-11-01T00:00:00Z',
        decision_hour_utc=8, capture_grace_minutes=30, implementation_compatible=True,
        phase='awaiting_decision', targets_known=0, forecasts=0, admissible=0, settled=0,
        pending=0, unresolved=0, void=0, late=0, missing_ack=0,
        metrics={}, rows=[], days=[dict(day='2026-10-31', targets=None, captures=0, state='awaiting')])
    if rows:
        protocol['rows'] = [dict(fixture=dict(match_id='1', league='61', home='Home', away='Away',
            timestamp='2026-10-31T16:00:00', country='France', competition='Ligue 1'),
            methods={name: dict(p) for name in ('candidate', 'clubs', 'frequency')},
            captured_at='2026-10-31T08:02:00Z', status='settled', ht='1-0', ft='2-2', actual='2nd',
            timing_quality='local_commit_ack_before_kickoff', issues=[])]
        protocol.update(phase='finished', forecasts=1, admissible=1, settled=1, targets_known=1)
        protocol['days'] = [dict(day='2026-10-31', targets=1, captures=1, state='anchored')]
    if metrics:
        protocol['metrics'] = {name:dict(count=1, logloss=.69, brier=.375, accuracy=1.)
                               for name in ('candidate', 'clubs', 'frequency')}
    return dict(version='test', generated_at='2026-10-31T00:00:00Z', protocols=[protocol])


def table(at, column):
    # AppTest does not expose dataframe keys in this installed Streamlit version.
    return next(row.value for row in at.dataframe if column in row.value.columns)


class ConfirmationPanelTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(panel, '_load_report', return_value=report())
        self.read = self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.at = AppTest.from_string('from causal_confirmation_panel import render\nrender()', default_timeout=10).run()

    def submit(self):
        return self.at.button(key='causal_confirmation_read').click().run()

    def test_opening_and_reruns_never_read_until_explicit_request(self):
        self.read.assert_not_called(); self.at.run(); self.read.assert_not_called()
        self.submit()
        self.assertFalse(self.at.exception); self.read.assert_called_once_with()
        self.at.run(); self.assertEqual(self.read.call_count, 1)
        self.assertEqual(table(self.at, 'Cibles recensées').iloc[0]['Cibles recensées'], 'Non recensé')
        self.assertFalse(any('Mêmes matchs évalués' in df.value.columns for df in self.at.dataframe))
        self.assertTrue(any('Aucun résultat admissible' in row.value for row in self.at.info))

    def test_failed_new_read_clears_previous_report(self):
        self.submit(); self.assertTrue(self.at.dataframe)
        self.read.side_effect = ValueError('Preuve altérée')
        self.submit()
        self.assertFalse(self.at.exception); self.assertTrue(self.at.error)
        self.assertFalse(self.at.dataframe)

    def test_saved_forecasts_include_all_probabilities_scores_and_temporal_receipts(self):
        self.read.return_value = report(rows=True, metrics=True)
        self.submit(); self.assertFalse(self.at.exception)
        frame = table(self.at, 'Match')
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]['1st (%)'], 25.)
        self.assertEqual(frame.iloc[0]['2nd (%)'], 50.)
        self.assertEqual(frame.iloc[0]['Clubs · Égal (%)'], 25.)
        self.assertEqual(frame.iloc[0]['Score à la pause'], '1-0')
        self.assertEqual(frame.iloc[0]['Résultat'], '2nd')
        self.assertEqual(frame.iloc[0]['Statut'], 'Évalué')
        self.assertEqual(frame.iloc[0]['Coup d’envoi (Kinshasa)'], '31/10/2026 17:00:00')
        self.assertEqual(len(table(self.at, 'Mêmes matchs évalués')), 3)

    def test_late_or_missing_ack_forecasts_are_visible_without_success_claim(self):
        value = report(rows=True)
        row = value['protocols'][0]['rows'][0]
        row.update(status='missing_ack', captured_at=None, actual=None, ht=None, ft=None,
                   issues=['Accusé absent après interruption'])
        value['protocols'][0].update(admissible=0, settled=0, missing_ack=1)
        self.read.return_value = value
        self.submit(); self.assertFalse(self.at.exception)
        frame = table(self.at, 'Match')
        self.assertEqual(frame.iloc[0]['Statut'], 'Accusé d’enregistrement absent')
        self.assertEqual(frame.iloc[0]['Résultat'], '—')
        self.assertIn('Accusé absent', frame.iloc[0]['Motif'])

    def test_missed_window_and_source_unavailable_are_not_zero_eligible_matches(self):
        for phase, state in [('window_missed', 'missed'), ('source_unavailable', 'source_unavailable')]:
            value = report(); value['protocols'][0]['phase'] = phase
            value['protocols'][0]['days'][0].update(state=state, targets=0)
            self.read.return_value = value
            self.submit(); self.assertFalse(self.at.exception)
            self.assertEqual(table(self.at, 'Cibles recensées').iloc[0]['Cibles recensées'], 'Non recensé')

    def test_empty_protocol_list_does_not_register_automatically(self):
        self.read.return_value = dict(generated_at='2026-10-31T00:00:00Z', protocols=[])
        self.submit(); self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun protocole' in row.value for row in self.at.info))
        self.assertFalse(self.at.dataframe)

    def test_weekly_view_uses_only_loaded_protocol_and_keeps_compatibility_warning(self):
        value = report(); value['protocols'][0]['implementation_compatible'] = False
        self.read.return_value = value
        self.submit()
        with patch('causal_confirmation_followup_panel.render') as weekly:
            self.at.segmented_control(key='causal_confirmation_view').set_value('weekly').run()
        self.assertFalse(self.at.exception)
        weekly.assert_called_once_with(value['protocols'][0])
        self.assertTrue(self.at.warning)
        self.read.assert_called_once_with()

    def test_confirmation_route_returns_before_history_or_model_loading(self):
        import causal_partition_panel as cohorts
        with patch.object(cohorts.artifacts, 'load_bundle', side_effect=AssertionError('No cohort load')):
            at = AppTest.from_string('from causal_partition_panel import render\nrender()', default_timeout=10).run()
            at.segmented_control(key='causal_cohort_tool').set_value('confirmation').run()
        self.assertFalse(at.exception); self.assertTrue(at.button(key='causal_confirmation_read'))
        self.read.assert_not_called()


class ConfirmationCliTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parent/'scripts/track_causal_confirmation.py'
        spec = importlib.util.spec_from_file_location('confirmation_cli_under_test', path)
        self.cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.cli)
        self.core = SimpleNamespace(register=Mock(return_value={'id':'a'*64}),
                                    register_followup=Mock(return_value={'id':'b'*64}),
                                    report=Mock(return_value=report()),
                                    reconcile=Mock(return_value={'changed':0}))

    def call(self, args):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['track_causal_confirmation.py', *args]), \
                patch.dict(sys.modules, {'causal_confirmation': self.core}), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.cli.main()
        return output.getvalue()

    def test_register_is_explicit_and_does_not_capture(self):
        output = self.call(['register', '--candidate', 'research/candidate'])
        self.core.register.assert_called_once_with(Path('research/candidate'))
        self.core.report.assert_not_called(); self.core.reconcile.assert_not_called()
        self.assertIn('aaaa', output)

    def test_status_is_read_only_and_reconcile_is_separate(self):
        self.call(['status']); self.core.report.assert_called_once_with()
        self.core.register.assert_not_called(); self.core.reconcile.assert_not_called()
        self.call(['reconcile']); self.core.reconcile.assert_called_once_with()
        self.core.register.assert_not_called()

    def test_future_followup_enrollment_is_separate_and_explicit(self):
        self.call(['register-followup', '--candidate', 'research/candidate', '--start',
                   '2026-11-02T00:00:00Z', '--end', '2026-12-28T00:00:00Z'])
        self.core.register_followup.assert_called_once_with(Path('research/candidate'),
            start='2026-11-02T00:00:00Z', end='2026-12-28T00:00:00Z')
        self.core.register.assert_not_called(); self.core.report.assert_not_called()

    def test_weekly_read_never_registers_and_rejects_unknown_protocol(self):
        output = self.call(['weekly'])
        self.assertIn('not_registered', output)
        self.core.register_followup.assert_not_called()
        with self.assertRaises(SystemExit):
            self.call(['weekly', '--protocol', 'unknown'])

    def test_no_standalone_capture_client_is_exposed(self):
        with self.assertRaises(SystemExit) as error:
            self.call(['capture'])
        self.assertEqual(error.exception.code, 2)
        self.core.register.assert_not_called(); self.core.report.assert_not_called()


if __name__ == '__main__':
    unittest.main()
