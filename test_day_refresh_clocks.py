"""Independent civil/monotonic-clock scenarios for cooperative data refresh.

The clocks intentionally advance separately. No provider, credential, model or
production database is consulted; only completed-operation boundaries are mocked.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import api_football as api
import automation_worker as worker


NOW = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


class DayRefreshClockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.civil = NOW
        self.monotonic = 100.0
        self.events = []
        self.report = dict(status='completed', started_at=NOW.isoformat(), history_imports=0,
                           requests=0, issues=[])
        self.config = dict(worker.DEFAULT_CONFIG, history_enabled=False)
        self.gate = SimpleNamespace(state={}, save=Mock(), check_time=Mock(), call=Mock())
        self.pulse = SimpleNamespace(progress=Mock())
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(worker.time, 'monotonic', side_effect=lambda:self.monotonic))
        self.patches.enter_context(patch.object(worker.cloud_storage, 'configured', return_value=False))
        self.observations = self.patches.enter_context(patch.object(worker.health, 'record_refresh', return_value=True))
        self.patches.enter_context(patch.object(api, 'load_key', side_effect=AssertionError('No credentials')))
        self.patches.enter_context(patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No network')))
        self.calendar = self.patches.enter_context(patch.object(worker, '_calendar',
                                      side_effect=lambda *a,**k:self.events.append('calendar')))
        self.results = self.patches.enter_context(patch.object(worker, '_refresh_results',
                                      side_effect=lambda *a,**k:self.events.append('results')))
        self.reconcile = self.patches.enter_context(patch.object(worker, '_reconcile',
                                      side_effect=lambda *a,**k:self.events.append('reconcile')))
        self.forecast = self.patches.enter_context(patch.object(worker, '_forecast',
                                      side_effect=lambda *a,**k:self.events.append('forecasts')))
        self.patches.enter_context(patch.object(worker, '_write'))
        self.refresh = worker._DayRefresh(self.root, self.config, self.gate, self.report,
                                         self.clock, NOW.date(), None, self.pulse)
        self.gate.results_checkpoint = self.refresh.check_results

    def clock(self):
        return self.civil

    def advance(self, *, civil=0, monotonic=0):
        self.civil += timedelta(seconds=civil)
        self.monotonic += monotonic

    def test_suspension_advancing_only_civil_time_refreshes_calendar_before_backlog(self):
        self.advance(civil=3*3600)
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])
        self.calendar.assert_called_once_with(self.root, self.gate, self.clock, NOW.date(), archive=False)
        self.assertEqual(self.results.call_args.kwargs['max_batches'], 1)
        self.assertEqual(self.gate.state['day_service']['calendar_at'], self.clock().isoformat())
        self.assertEqual(self.gate.state['day_service']['results_at'], self.clock().isoformat())
        self.reconcile.assert_called_once_with(self.root, self.report, self.clock())
        self.forecast.assert_not_called()
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])
        self.gate.call.assert_not_called()

    def test_civil_deadline_also_reaches_history_cooperative_checkpoint(self):
        self.advance(civil=2*3600)
        self.refresh()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile', 'forecasts'])
        self.assertEqual(self.report['day_refreshes'], 1)
        self.refresh()
        self.assertEqual(self.forecast.call_count, 1)
        self.assertEqual(self.results.call_args.kwargs['max_batches'], 1)

    def test_result_batch_callback_uses_civil_age_without_forecast_recursion(self):
        self.advance(civil=2*3600)
        self.refresh.during_results()
        self.assertEqual(self.events, ['calendar', 'reconcile'])
        self.results.assert_not_called()
        self.forecast.assert_not_called()
        self.refresh.check_results()
        self.results.assert_not_called()

    def test_equality_at_civil_deadline_is_due_but_just_before_is_not(self):
        self.advance(civil=worker.DAY_REFRESH_SECONDS-.001)
        self.refresh.check_results()
        self.assertEqual(self.events, [])
        self.advance(civil=.001)
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])

    def test_normal_monotonic_expiry_still_works_when_civil_clock_is_stationary(self):
        self.advance(monotonic=worker.DAY_REFRESH_SECONDS)
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])
        self.refresh.check_results()
        self.assertEqual(self.results.call_count, 1)

    def test_backward_civil_adjustment_cannot_postpone_monotonic_expiry(self):
        self.advance(civil=-30*60, monotonic=worker.DAY_REFRESH_SECONDS)
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])
        self.assertEqual(self.gate.state['day_service']['calendar_at'], self.clock().isoformat())
        self.refresh.check_results()
        self.assertEqual(self.results.call_count, 1)

    def test_slow_pass_rearms_both_clocks_from_completion_and_leaves_forecast_a_turn(self):
        self.advance(civil=3*3600)
        def results(*args, **kwargs):
            self.events.append('results')
            self.advance(civil=2*3600, monotonic=5)
            self.refresh.check_results()  # The active pass must not recurse.
        def reconcile(*args, **kwargs):
            self.events.append('reconcile')
            self.advance(civil=2*3600, monotonic=5)
        self.results.side_effect = results
        self.reconcile.side_effect = reconcile
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])
        self.assertFalse(self.refresh.servicing_results)
        self.assertEqual(self.refresh.due, self.monotonic+worker.DAY_REFRESH_SECONDS)
        self.assertEqual(self.refresh.results_due, self.monotonic+worker.DAY_REFRESH_SECONDS)
        self.assertEqual(self.refresh.due_wall, self.clock()+timedelta(seconds=worker.DAY_REFRESH_SECONDS))
        self.assertEqual(self.refresh.results_due_wall, self.clock()+timedelta(seconds=worker.DAY_REFRESH_SECONDS))
        for _ in range(3):
            worker._forecast_checkpoint(self.gate)
        self.assertEqual(self.results.call_count, 1)
        self.advance(civil=worker.DAY_REFRESH_SECONDS-1)
        self.refresh.check_results()
        self.assertEqual(self.results.call_count, 1)
        self.results.side_effect = lambda *a,**k:self.events.append('results')
        self.reconcile.side_effect = lambda *a,**k:self.events.append('reconcile')
        self.advance(civil=1)
        self.refresh.check_results()
        self.assertEqual(self.results.call_count, 2)
        self.forecast.assert_not_called()

    def test_provider_pause_cannot_mark_calendar_success_or_consume_its_deadline(self):
        self.advance(civil=3*3600)
        deadlines = (self.refresh.due, self.refresh.due_wall,
                     self.refresh.results_due, self.refresh.results_due_wall)
        self.calendar.side_effect = worker._Pause('Provider cooldown')
        with self.assertRaises(worker._Pause):
            self.refresh.check_results()
        self.assertNotIn('calendar_at', self.gate.state.get('day_service',{}))
        self.assertEqual((self.refresh.due, self.refresh.due_wall,
                          self.refresh.results_due, self.refresh.results_due_wall), deadlines)
        self.assertFalse(self.refresh.servicing_results)
        self.results.assert_not_called()
        self.reconcile.assert_not_called()
        self.calendar.side_effect = lambda *a,**k:self.events.append('calendar')
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])

    def test_long_civil_jump_to_next_day_stops_before_an_old_day_request(self):
        self.advance(civil=24*3600)
        with self.assertRaises(worker._Pause):
            self.refresh.check_results()
        self.calendar.assert_not_called(); self.results.assert_not_called(); self.reconcile.assert_not_called()
        self.assertFalse(self.refresh.servicing_results)
        self.assertFalse(self.gate.state.get('day_service'))

    def test_result_callback_stops_after_rollover_before_marking_or_collecting_old_day(self):
        self.advance(civil=24*3600)
        with self.assertRaises(worker._Pause):
            self.refresh.during_results()
        self.calendar.assert_not_called(); self.results.assert_not_called(); self.reconcile.assert_not_called()
        self.observations.assert_not_called()
        self.assertFalse(self.gate.state.get('day_service'))

    def test_equivalent_aware_timezone_representation_preserves_absolute_deadline(self):
        self.civil = (NOW+timedelta(seconds=worker.DAY_REFRESH_SECONDS)).astimezone(timezone(timedelta(hours=1)))
        self.refresh.check_results()
        self.assertEqual(self.events, ['calendar', 'results', 'reconcile'])
        self.refresh.check_results()
        self.assertEqual(self.results.call_count, 1)

    def test_calendar_refresh_success_does_not_hide_a_failed_result_pass(self):
        self.advance(civil=3*3600)
        self.results.side_effect = worker._Pause('Provider result cooldown')
        with self.assertRaises(worker._Pause):
            self.refresh.check_results()
        self.assertIn('calendar_at', self.gate.state['day_service'])
        self.assertNotIn('results_at', self.gate.state['day_service'])
        self.assertFalse(self.refresh.servicing_results)
        self.reconcile.assert_not_called()


if __name__ == '__main__':
    unittest.main()
