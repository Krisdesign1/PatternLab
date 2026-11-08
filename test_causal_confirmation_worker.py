"""Scheduler callback routing; no provider or candidate fitting is executed."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import api_football as api
import causal_confirmation_worker as worker


class CausalConfirmationWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = SimpleNamespace(
            protocols=Mock(), capture_due=Mock(return_value=dict(status='completed', created=1)),
            reconcile=Mock(return_value=dict(status='available', settled=0)),
            refresh_entries=Mock(return_value=[]), report=Mock(return_value=dict(status='waiting')))
        patches = [patch.dict('sys.modules', {'causal_confirmation': self.core}),
                   patch.object(worker.cloud_storage, 'configured', return_value=False),
                   patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API from adapter')),
                   patch.object(api, 'load_key', side_effect=AssertionError('No key from adapter'))]
        self.mocks = [item.start() for item in patches]
        for item in patches: self.addCleanup(item.stop)

    def test_capture_passes_the_existing_gate_and_clock_callbacks_unchanged(self):
        clock = Mock(return_value=datetime(2026, 10, 31, 8, tzinfo=timezone.utc))
        refresh, check_time = Mock(return_value=[]), Mock()
        result = worker.capture_cycle(self.root, clock=clock, refresh=refresh, check_time=check_time)
        self.core.capture_due.assert_called_once_with(root=self.root.resolve(), clock=clock,
                                                       refresh=refresh, check_time=check_time)
        self.assertEqual(result, dict(status='completed', created=1))
        self.core.protocols.assert_not_called(); self.core.reconcile.assert_not_called()
        self.mocks[2].assert_not_called(); self.mocks[3].assert_not_called()

    def test_no_activation_or_training_is_inferred_by_the_adapter(self):
        self.core.capture_due.return_value = dict(status='idle', protocols=0, created=0)
        result = worker.capture_cycle(self.root)
        self.assertEqual(result['created'], 0)
        self.core.capture_due.assert_called_once_with(root=self.root.resolve(), clock=None,
                                                       refresh=None, check_time=None)
        self.core.protocols.assert_not_called()

    def test_cloud_mode_disables_mutations_and_exposes_no_result_queue(self):
        self.mocks[1].return_value = True
        self.assertEqual(worker.capture_cycle(self.root)['status'], 'disabled')
        self.assertEqual(worker.reconcile(self.root)['status'], 'disabled')
        self.assertEqual(worker.status(self.root)['status'], 'disabled')
        self.assertEqual(worker.refresh_entries(self.root), [])
        for name in ('capture_due', 'reconcile', 'report', 'refresh_entries'):
            getattr(self.core, name).assert_not_called()

    def test_scheduler_pause_propagates_without_becoming_an_empty_success(self):
        from automation_worker import _Pause
        self.core.capture_due.side_effect = _Pause('Provider pause')
        with self.assertRaises(_Pause): worker.capture_cycle(self.root)
        self.core.capture_due.side_effect = ValueError('Invalid saved protocol')
        with self.assertRaises(ValueError): worker.capture_cycle(self.root)

    def test_result_reconciliation_has_no_capture_window_or_provider_dependency(self):
        current = datetime(2026, 11, 5, 18, tzinfo=timezone.utc)
        result = worker.reconcile(self.root, now=current)
        self.assertEqual(result['status'], 'available')
        self.core.reconcile.assert_called_once_with(root=self.root.resolve(), now=current)
        self.core.capture_due.assert_not_called(); self.mocks[2].assert_not_called()

    def test_production_reconciliation_rejects_a_caller_supplied_clock(self):
        with patch.object(api, 'DATA_DIR', self.root):
            with self.assertRaises(ValueError):
                worker.reconcile(now=datetime(2026, 10, 31, 8, tzinfo=timezone.utc))
            worker.reconcile()
        self.core.reconcile.assert_called_once_with(root=self.root.resolve(), now=None)

    def test_status_and_refresh_queue_are_read_only_without_capture(self):
        expected = [dict(status='pending', fixtures=[dict(match_id='123')])]
        self.core.refresh_entries.return_value = expected
        self.assertEqual(worker.refresh_entries(self.root), expected)
        self.assertEqual(worker.status(self.root), dict(status='waiting'))
        self.core.report.assert_called_once_with(root=self.root.resolve())
        self.core.refresh_entries.assert_called_once_with(root=self.root.resolve())
        self.core.capture_due.assert_not_called(); self.core.reconcile.assert_not_called()
        self.mocks[2].assert_not_called(); self.mocks[3].assert_not_called()

    def test_automation_hook_keeps_provider_quota_reservation_and_cooperative_checkpoints(self):
        import automation_worker as automation
        current = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
        client = SimpleNamespace(fixtures=Mock(return_value=dict(response=[], results=0)), last_quota={})
        report = dict(requests=0)
        gate = automation._Gate(self.root, {}, automation.DEFAULT_CONFIG, client, lambda: current, report)
        checkpoint = Mock(); gate.results_checkpoint = checkpoint
        def capture(root, *, clock, refresh, check_time):
            self.assertEqual(root, self.root)
            self.assertEqual(clock(), current)
            check_time()
            self.assertEqual(refresh(ids='123', timezone='UTC'), [])
            # The real gate reserves and saves the call before network access.
            self.assertEqual(json.loads((self.root/'automation_state.json').read_text())['requests_today'], 1)
            return dict(status='completed', created=0)
        with patch.object(worker, 'capture_cycle', side_effect=capture):
            automation._track_causal_confirmation(self.root, gate, report, lambda: current)
        client.fixtures.assert_called_once_with(ids='123', timezone='UTC')
        self.assertEqual(report['requests'], 1)
        self.assertEqual(checkpoint.call_count, 2)
        self.assertEqual(report['causal_confirmation']['status'], 'completed')
        self.mocks[2].assert_not_called(); self.mocks[3].assert_not_called()

    def test_automation_hook_propagates_pause_and_reports_other_failures_separately(self):
        import automation_worker as automation
        report = dict(unrelated='preserved')
        with patch.object(worker, 'capture_cycle', side_effect=automation._Pause('Quota pause')):
            with self.assertRaises(automation._Pause):
                automation._track_causal_confirmation(self.root, Mock(), report, Mock())
        self.assertNotIn('causal_confirmation', report)
        with patch.object(worker, 'capture_cycle', side_effect=ValueError('Protocol error')):
            automation._track_causal_confirmation(self.root, Mock(), report, Mock())
        self.assertEqual(report['causal_confirmation']['status'], 'unavailable')
        self.assertEqual(report['causal_confirmation']['created'], 0)
        self.assertEqual(report['unrelated'], 'preserved')

    def test_inference_hook_runs_with_background_model_training_disabled(self):
        import automation_worker as automation
        import daily_analysis_worker
        current = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
        clock = lambda: current
        gate = SimpleNamespace(state={}, call=Mock(), save=Mock(), check_time=Mock())
        config = dict(automation.DEFAULT_CONFIG, models_enabled=False)
        report = dict(issues=[])
        events = []
        with patch.object(daily_analysis_worker, 'run', return_value=dict(status='completed')), \
                patch.object(automation, '_archive_probabilities', side_effect=lambda *args: events.append('archive')), \
                patch.object(automation, '_track_causal_confirmation', side_effect=lambda *args: events.append('causal')) as capture, \
                patch.object(automation, '_track_argumented_selection'), \
                patch.object(automation, '_track_candidates'), \
                patch.object(automation, '_prepare_daily_learning') as training, \
                patch.object(automation, '_track_daily_learning') as daily_tracking, \
                patch.object(automation.api, 'load_fixtures', return_value=[]), \
                patch.object(automation.journal, 'list_entries', return_value=[]):
            automation._forecast(self.root, config, gate, report, clock, automation._local_day(current), None)
        capture.assert_called_once_with(self.root, gate, report, clock)
        self.assertEqual(events, ['archive', 'causal'])
        training.assert_not_called(); daily_tracking.assert_not_called()

    def test_confirmation_pending_ids_enter_the_existing_result_refresh_queue(self):
        import automation_worker as automation
        from test_api_football import fixture
        current = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
        raw = fixture(123)
        self.core.refresh_entries.return_value = [dict(status='pending', fixtures=[
            dict(match_id='123', timestamp='2025-08-12T19:00:00')])]
        gate = SimpleNamespace(state={}, call=Mock(return_value=[raw]), save=Mock())
        report = {}
        automation._refresh_results(self.root, gate, lambda: current, report, max_batches=1)
        gate.call.assert_called_once_with(ids='123', timezone='UTC')
        self.assertEqual(report['result_refresh_sources']['causal_confirmation'], 'available')
        self.assertEqual(report['result_refresh']['received'], 1)
        self.assertEqual(report['result_refresh']['remaining'], 0)


if __name__ == '__main__':
    unittest.main()
