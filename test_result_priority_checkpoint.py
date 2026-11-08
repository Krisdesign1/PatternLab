"""Result collection must get CPU turns before and between forecast jobs.

All clocks, provider responses and model calculations are synthetic. These
checks never read production data or require an API key.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import api_football as api
import automation_worker as worker
import daily_analysis_engine as daily_engine
import daily_analysis_worker as daily
from test_daily_analysis_worker import candidate
from test_prediction_journal import raw_fixtures
from test_result_refresh_resilience import Gate as ResultGate


NOW = datetime(2025, 3, 20, 9, tzinfo=timezone.utc)


class ResultPriorityCheckpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.elapsed = 0
        self.config = dict(worker.DEFAULT_CONFIG, history_enabled=False)
        self.report = dict(status='completed', started_at=NOW.isoformat(), history_imports=0,
            requests=0, created=0, already_recorded=0, groups_considered=0, issues=[], jobs=[])
        self.gate = SimpleNamespace(state={}, save=Mock(), check_time=Mock(), call=Mock(return_value=[]))
        self.pulse = SimpleNamespace(progress=Mock())
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(worker.cloud_storage, 'configured', return_value=False))
        self.patches.enter_context(patch.object(worker.health, 'record_refresh', return_value=True))
        self.patches.enter_context(patch.object(api, 'load_key', side_effect=AssertionError('No credentials')))
        self.patches.enter_context(patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No network')))
        self.patches.enter_context(patch.object(worker.time, 'monotonic', side_effect=lambda:self.elapsed))

    def clock(self):
        return NOW + timedelta(seconds=self.elapsed)

    def refresh(self):
        return worker._DayRefresh(self.root, self.config, self.gate, self.report,
            self.clock, NOW.date(), None, self.pulse)

    def backlog(self, count, *, status='pending', first_id=12000):
        base = raw_fixtures(candidate()['fixtures'], status='FT')[0]
        raw, fixtures = [], []
        for index in range(count):
            row = deepcopy(base)
            row['fixture'].update(id=first_id+index, date='2025-03-20T08:00:00Z')
            raw.append(row)
            fixtures.append(dict(match_id=str(first_id+index), timestamp=row['fixture']['date']))
        return raw, [dict(status=status, fixtures=fixtures)]

    def test_result_quantum_resumes_all_ids_from_durable_checks_without_duplicates(self):
        raw, entries = self.backlog(65)
        state, calls, remaining = {}, [], []
        report = {}
        with patch.object(worker.journal,'list_entries',return_value=entries):
            for expected_due in (65,45,25,5):
                gate = ResultGate(self.root,raw,state=state,omitted={'12000'})
                worker._refresh_results(self.root,gate,self.clock,report,max_batches=1)
                calls.extend(gate.calls)
                self.assertEqual(len(gate.calls),1)
                self.assertEqual(report['result_refresh']['due'],expected_due)
                remaining.append(report['result_refresh']['remaining'])
                state = json.loads((self.root/'state.json').read_text())
            empty = ResultGate(self.root,raw,state=state)
            worker._refresh_results(self.root,empty,self.clock,report,max_batches=1)
        attempted = [mid for call in calls for mid in call['ids'].split('-')]
        self.assertEqual(len(attempted),65)
        self.assertEqual(len(set(attempted)),65)
        self.assertEqual(set(attempted),{str(12000+i) for i in range(65)})
        self.assertEqual(remaining,[45,25,5,0])
        self.assertEqual(report['result_refresh'],dict(received=64,missing=1,batches=4,
            due=0,remaining=0,status='partial'))
        self.assertEqual(state['result_missing']['12000']['attempts'],1)
        self.assertFalse(empty.calls)

    def test_nonsettled_shared_ids_precede_corrections_then_all_corrections_resume(self):
        corrections, settled = self.backlog(25,status='settled')
        pending, pending_entries = self.backlog(3,first_id=22000)
        entries = settled + pending_entries + [dict(status='pending',fixtures=[settled[0]['fixtures'][-1]])]
        checks = {str(12000+i):(NOW-timedelta(hours=7)).timestamp() for i in range(25)}
        checks.update({str(22000+i):(NOW-timedelta(hours=i+1)).timestamp() for i in range(3)})
        gate = ResultGate(self.root,corrections+pending,state=dict(result_checks=checks))
        report = {}
        with patch.object(worker.journal,'list_entries',return_value=entries):
            worker._refresh_results(self.root,gate,self.clock,report,max_batches=1)
            first = gate.calls[0]['ids'].split('-')
            self.assertEqual(first[:4],['12024','22002','22001','22000'])
            self.assertEqual(report['result_refresh']['remaining'],8)
            self.assertEqual(report['result_refresh']['status'],'pending')
            worker._refresh_results(self.root,gate,self.clock,report,max_batches=1)
        attempted = [mid for call in gate.calls for mid in call['ids'].split('-')]
        self.assertEqual(len(attempted),28)
        self.assertEqual(len(set(attempted)),28)
        self.assertEqual(report['result_refresh']['remaining'],0)
        self.assertEqual(report['result_refresh']['status'],'completed')

    def test_invalid_result_quantum_fails_before_loading_or_network(self):
        with patch.object(worker.journal,'list_entries') as entries:
            for invalid in (0,-1,True,1.5,'1'):
                with self.subTest(max_batches=invalid), self.assertRaises(worker.AutomationError):
                    worker._refresh_results(self.root,self.gate,self.clock,{},max_batches=invalid)
        entries.assert_not_called()
        self.gate.call.assert_not_called()

    def test_startup_result_backlog_yields_to_a_real_daily_forecast_after_one_batch(self):
        from test_automation_worker import Client
        raw, entries = self.backlog(85)
        entries[0]['id'] = 'backlog'
        group = candidate()
        future = raw_fixtures(group['fixtures'])
        api.save_fixtures(raw+future,self.root/'fixtures.json')
        client = Client(raw+future)
        def reconcile(*args):
            self.elapsed += 75
        def record(fixtures,methods,**kwargs):
            result_calls = [call for call in client.calls if 'ids' in call and
                            len(call['ids'].split('-'))==20]
            self.assertEqual(len(result_calls),1)
            self.assertEqual(self.report['result_refresh']['remaining'],65)
            return dict(id='daily-created',created=True,methods=methods)
        with ExitStack() as patches:
            for name in ('_archive_probabilities','_track_argumented_selection',
                         '_prepare_daily_learning','_track_daily_learning','_track_candidates'):
                patches.enter_context(patch.object(worker,name))
            patches.enter_context(patch.object(worker,'_reconcile',side_effect=reconcile))
            patches.enter_context(patch.object(worker,'_history',side_effect=worker._Pause('End synthetic turn')))
            patches.enter_context(patch.object(worker,'_inventory',return_value=({}, {}, {})))
            patches.enter_context(patch.object(worker,'_plan',return_value=([], {})))
            patches.enter_context(patch.object(daily_engine,'plan',return_value=dict(version='synthetic',
                day=NOW.date().isoformat(),candidates=[group],diagnostics=[])))
            patches.enter_context(patch.object(daily.journal,'list_entries',return_value=entries))
            patches.enter_context(patch.object(daily.journal,'find_mixed',return_value=None))
            patches.enter_context(patch.object(daily.journal,'record_mixed',side_effect=record))
            patches.enter_context(patch.object(daily.models,'build_mixed_methods',return_value=dict(
                methods={'clubs':{'version':'synthetic'}},unavailable={})))
            result = worker._execute_cycle(self.root,self.config,client,self.clock,
                self.report,None,None,self.pulse)
        self.assertEqual(result['status'],'paused',result)
        self.assertEqual(result['daily_analysis_priority']['created'],1)
        self.assertEqual(result['result_refresh']['status'],'pending')
        self.assertEqual(result['result_refresh']['remaining'],65)

    def test_checkpoint_checks_host_deadline_before_cooperative_results(self):
        events = []
        self.gate.check_time.side_effect = lambda:events.append('deadline')
        self.gate.results_checkpoint = lambda:events.append('results')
        worker._forecast_checkpoint(self.gate)
        self.assertEqual(events, ['deadline','results'])
        self.gate.check_time.side_effect = worker._Pause('Host deadline')
        with self.assertRaises(worker._Pause):
            worker._forecast_checkpoint(self.gate)
        self.assertEqual(events, ['deadline','results'])

    def test_startup_results_and_reconciliation_precede_candidate_or_forecast_work(self):
        events = []
        state = dict(version=worker.VERSION, next_phase='forecasts')
        def event(name, result=None):
            def called(*args, **kwargs):
                events.append(name)
                return result
            return called
        with patch.object(worker,'_read',return_value=state), \
                patch.object(worker,'_write'), \
                patch.object(worker,'_calendar',side_effect=event('calendar',{})), \
                patch.object(worker,'_refresh_results',side_effect=event('results')), \
                patch.object(worker,'_reconcile',side_effect=event('reconcile')), \
                patch.object(daily,'run',side_effect=event('daily-priority',dict(created=1))) as priority, \
                patch.object(worker,'_track_candidates',side_effect=event('candidates')), \
                patch.object(worker,'_track_argumented_selection',side_effect=event('argumented')), \
                patch.object(worker,'_prepare_daily_learning',side_effect=event('training')), \
                patch.object(worker,'_track_daily_learning',side_effect=event('learning')), \
                patch.object(worker,'_forecast',side_effect=event('forecasts')), \
                patch.object(worker,'_history',side_effect=event('history')), \
                patch.object(worker,'_inventory',return_value=({}, {}, {})), \
                patch.object(api,'load_fixtures',return_value=[]):
            result = worker._execute_cycle(self.root,self.config,object(),self.clock,
                self.report,None,None,self.pulse)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(events[:5], ['calendar','results','reconcile','daily-priority','forecasts'])
        self.assertEqual(priority.call_args.kwargs['max_jobs'],1)
        self.assertEqual(result['daily_analysis_priority']['created'],1)
        self.assertLess(events.index('reconcile'),events.index('forecasts'))
        self.assertNotIn('candidates',events)  # The full forecast phase owns this capture.
        self.assertTrue({'argumented','training','learning'}.isdisjoint(events))

    def test_daily_queue_precedes_secondary_inference_and_training(self):
        events = []
        with ExitStack() as patches:
            patches.enter_context(patch.object(daily,'run',side_effect=lambda *a,**k:
                events.append('daily') or dict(created=1)))
            for name in ('_archive_probabilities','_track_argumented_selection',
                         '_prepare_daily_learning','_track_daily_learning','_track_candidates'):
                patches.enter_context(patch.object(worker,name,
                    side_effect=lambda *a,_name=name,**k:events.append(_name)))
            patches.enter_context(patch.object(api,'load_fixtures',return_value=[]))
            patches.enter_context(patch.object(worker,'_inventory',return_value=({}, {}, {})))
            patches.enter_context(patch.object(worker,'_plan',return_value=([], {})))
            patches.enter_context(patch.object(worker.journal,'list_entries',return_value=[]))
            worker._forecast(self.root,self.config,self.gate,self.report,self.clock,NOW.date(),None)
        self.assertEqual(events,['daily','_archive_probabilities','_track_argumented_selection',
            '_prepare_daily_learning','_track_daily_learning','_track_candidates'])
        self.assertEqual(self.report['daily_analyses']['created'],1)

    def test_slow_startup_results_do_not_repeat_before_daily_progress(self):
        state = dict(version=worker.VERSION, next_phase='forecasts')
        def slow(*args, **kwargs):
            self.elapsed += 75
        def forecast(root, config, gate, *args):
            worker._forecast_checkpoint(gate)
            self.report['daily_progress'] = True
        with patch.object(worker,'_read',return_value=state), \
                patch.object(worker,'_write'), \
                patch.object(worker,'_calendar',return_value={}), \
                patch.object(worker,'_refresh_results',side_effect=slow) as results, \
                patch.object(worker,'_reconcile',side_effect=slow), \
                patch.object(worker,'_forecast',side_effect=forecast), \
                patch.object(worker,'_history',side_effect=worker._Pause('End synthetic turn')), \
                patch.object(worker,'_inventory',return_value=({}, {}, {})), \
                patch.object(api,'load_fixtures',return_value=[]):
            result = worker._execute_cycle(self.root,self.config,object(),self.clock,
                self.report,None,None,self.pulse)
        self.assertEqual(result['status'],'paused',result)
        self.assertTrue(self.report['daily_progress'])
        self.assertEqual(results.call_count,1)

    def test_due_results_checkpoint_is_nonreentrant_and_uses_current_clock(self):
        refresh = self.refresh()
        with patch.object(worker,'_refresh_results') as results, \
                patch.object(worker,'_reconcile') as reconcile, \
                patch.object(worker,'_forecast') as forecast:
            refresh.check_results()
            results.assert_not_called()
            self.elapsed = 61
            results.side_effect = lambda *args,**kwargs:refresh.check_results()
            refresh.check_results()
            self.assertEqual(results.call_count,1)
            reconcile.assert_called_once_with(self.root,self.report,self.clock())
            self.assertEqual(self.gate.state['day_service']['results_at'],self.clock().isoformat())
            self.assertEqual(self.gate.state['day_service']['reconciled_at'],self.clock().isoformat())
            self.assertEqual(self.report['result_priority_passes'],1)
            forecast.assert_not_called()
            self.assertEqual(self.pulse.progress.call_args.args,('forecasts',))
            refresh.check_results()
            self.assertEqual(results.call_count,1)

    def test_results_checkpoint_refreshes_today_before_the_global_id_backlog(self):
        refresh = self.refresh()
        self.elapsed = 61
        raw = raw_fixtures(candidate()['fixtures'], status='FT')
        for item in raw:
            item['fixture']['date'] = '2025-03-20T08:00:00Z'
        self.gate.call.return_value = raw
        events = []
        def results(*args, **kwargs):
            events.append('results')
            # A large global ID queue must not keep today's calendar at NS.
            saved = api.load_fixtures(self.root/'fixtures.json')
            self.assertEqual({api._status_short(row) for row in saved}, {'FT'})
        def reconcile(root, report, current):
            events.append('reconcile')
            self.assertEqual(current, self.clock())
            report.update(settled=2, pending=0)
        with patch.object(worker,'_refresh_results',side_effect=results), \
                patch.object(worker,'_reconcile',side_effect=reconcile), \
                patch.object(worker,'_forecast') as forecast, \
                patch.object(worker,'_archive_probabilities') as archive, \
                patch.object(worker,'_track_candidates') as candidates, \
                patch.object(worker,'_prepare_daily_learning') as training:
            refresh.check_results()
        self.gate.call.assert_called_once_with(date=NOW.date().isoformat(),timezone=worker.TIMEZONE)
        self.assertEqual(events, ['results','reconcile'])
        self.assertEqual(self.gate.state['day_service']['calendar_at'],self.clock().isoformat())
        saved_status = json.loads((self.root/'automation_status.json').read_text())
        self.assertEqual(saved_status['status'],'running')
        self.assertEqual(saved_status['settled'],2)
        self.assertEqual(saved_status['pending'],0)
        self.assertEqual(saved_status['last_updated_at'],self.clock().isoformat())
        self.assertEqual(saved_status['day_service'],self.gate.state['day_service'])
        for operation in (forecast,archive,candidates,training):
            operation.assert_not_called()

    def test_results_checkpoint_does_not_refresh_calendar_before_its_interval(self):
        refresh = self.refresh()
        refresh.due = 120
        refresh.due_wall = NOW+timedelta(seconds=120)
        self.elapsed = 61
        with patch.object(worker,'_calendar') as calendar, \
                patch.object(worker,'_refresh_results') as results, \
                patch.object(worker,'_reconcile'):
            refresh.check_results()
        calendar.assert_not_called()
        self.gate.call.assert_not_called()
        results.assert_called_once()

    def test_results_checkpoint_stops_when_the_local_day_has_changed(self):
        refresh = self.refresh()
        self.elapsed = 24*60*60
        with patch.object(worker,'_calendar') as calendar, \
                patch.object(worker,'_refresh_results') as results, \
                patch.object(worker,'_reconcile') as reconcile:
            with self.assertRaises(worker._Pause):
                refresh.check_results()
        calendar.assert_not_called()
        results.assert_not_called()
        reconcile.assert_not_called()
        self.gate.call.assert_not_called()
        self.assertFalse(refresh.servicing_results)

    def test_slow_checkpoint_defers_calendar_until_after_a_full_interval(self):
        refresh = self.refresh()
        self.elapsed = 61
        def slow(*args, **kwargs):
            self.elapsed += 75
        with patch.object(worker,'_calendar',side_effect=slow) as calendar, \
                patch.object(worker,'_refresh_results') as results, \
                patch.object(worker,'_reconcile',side_effect=slow):
            refresh.check_results()
            self.assertEqual(self.elapsed,211)
            self.assertEqual(refresh.due,self.elapsed+worker.DAY_REFRESH_SECONDS)
            self.assertEqual(refresh.results_due,self.elapsed+worker.DAY_REFRESH_SECONDS)
            refresh.check_results()
            self.assertEqual(calendar.call_count,1)
            self.assertEqual(results.call_count,1)

    def test_slow_results_pass_leaves_a_full_interval_for_daily_progress(self):
        refresh = self.refresh()
        self.gate.results_checkpoint = refresh.check_results
        self.elapsed = 61
        def slow_results(*args, **kwargs):
            self.elapsed += 75
            refresh.check_results()  # An internal checkpoint must not recurse.
        def slow_reconcile(*args):
            self.elapsed += 75
        with patch.object(worker,'_refresh_results',side_effect=slow_results) as results, \
                patch.object(worker,'_reconcile',side_effect=slow_reconcile), \
                patch.object(worker,'_track_argumented_selection',side_effect=AssertionError('No captures')), \
                patch.object(worker,'_prepare_daily_learning',side_effect=AssertionError('No training')), \
                patch.object(worker,'_track_daily_learning',side_effect=AssertionError('No captures')):
            worker._forecast_checkpoint(self.gate)
            self.assertEqual(self.elapsed,211)
            self.assertEqual(refresh.results_due,211+worker.DAY_REFRESH_SECONDS)
            # The planner/build/preflight checkpoints can all advance after a
            # result pass slower than its interval, without repeating that pass.
            for _ in range(3):
                worker._forecast_checkpoint(self.gate)
                self.elapsed += 1
            self.assertEqual(results.call_count,1)
            self.elapsed = refresh.results_due
            worker._forecast_checkpoint(self.gate)
            self.assertEqual(results.call_count,2)
            self.assertEqual(self.report['result_priority_passes'],2)

    def test_result_batch_callback_can_refresh_calendar_but_never_run_forecasts(self):
        refresh = self.refresh()
        self.elapsed = 70
        with patch.object(worker,'_calendar',return_value={}) as calendar, \
                patch.object(worker,'_reconcile') as reconcile, \
                patch.object(worker,'_refresh_results') as results, \
                patch.object(worker,'_forecast') as forecast, \
                patch.object(worker,'_track_candidates') as candidates:
            refresh.during_results()
            calendar.assert_called_once_with(self.root,self.gate,self.clock,NOW.date(),archive=False)
            reconcile.assert_called_once_with(self.root,self.report,self.clock())
            results.assert_not_called()
            forecast.assert_not_called()
            candidates.assert_not_called()
            self.assertEqual(self.pulse.progress.call_args.args,('results',))

    def test_slow_result_batch_callback_defers_next_calendar_and_result_pass(self):
        refresh = self.refresh()
        self.elapsed = 61
        def slow(*args, **kwargs):
            self.elapsed += 75
        with patch.object(worker,'_calendar',side_effect=slow) as calendar, \
                patch.object(worker,'_reconcile',side_effect=slow), \
                patch.object(worker,'_refresh_results') as results:
            refresh.during_results()
            self.assertEqual(self.elapsed,211)
            self.assertEqual(refresh.due,211+worker.DAY_REFRESH_SECONDS)
            self.assertEqual(refresh.results_due,211+worker.DAY_REFRESH_SECONDS)
            refresh.check_results()
            results.assert_not_called()
            refresh.during_results()
            self.assertEqual(calendar.call_count,1)

    def test_slow_day_refresh_finishes_result_cooldown_before_entering_forecasts(self):
        refresh = self.refresh()
        self.elapsed = 61
        def slow(*args, **kwargs):
            self.elapsed += 75
        def forecast(*args):
            refresh.check_results()
        with patch.object(worker,'_calendar',side_effect=slow), \
                patch.object(worker,'_refresh_results',side_effect=slow) as results, \
                patch.object(worker,'_reconcile',side_effect=slow), \
                patch.object(worker,'_forecast',side_effect=forecast) as forecasts, \
                patch.object(worker,'_write'):
            refresh()
        self.assertEqual(results.call_count,1)
        forecasts.assert_called_once()
        self.assertEqual(refresh.results_due,self.elapsed+worker.DAY_REFRESH_SECONDS)

    def test_calendar_without_archiving_never_computes_probability_scan_or_forecasts(self):
        import high_probability_workspace as probabilities
        import high_probability_engine as probability_engine
        raw = raw_fixtures(candidate()['fixtures'])
        self.gate.call.return_value = raw
        with patch.object(worker,'_archive_probabilities') as archive, \
                patch.object(probabilities,'archive_scan',side_effect=AssertionError('No probability archive')), \
                patch.object(probability_engine,'scan',side_effect=AssertionError('No probability inference')), \
                patch.object(worker,'_forecast') as forecast:
            result = worker._calendar(self.root,self.gate,self.clock,NOW.date(),archive=False)
        self.gate.call.assert_called_once_with(date=NOW.date().isoformat(),timezone=worker.TIMEZONE)
        archive.assert_not_called()
        forecast.assert_not_called()
        self.assertEqual(result,dict(status='deferred'))
        self.assertEqual(len(api.load_fixtures(self.root/'fixtures.json')),len(raw))

    def test_provider_pause_during_checkpoint_propagates_without_leaving_reentrancy_lock(self):
        refresh = self.refresh()
        self.elapsed = 61
        with patch.object(worker,'_refresh_results',side_effect=worker._Pause('Provider cooldown')), \
                patch.object(worker,'_reconcile') as reconcile:
            with self.assertRaises(worker._Pause):
                refresh.check_results()
            self.assertFalse(refresh.servicing_results)
            self.assertEqual(refresh.results_due,worker.DAY_REFRESH_SECONDS)
            reconcile.assert_not_called()
            self.assertEqual(self.pulse.progress.call_args.args,('forecasts',))

    def test_daily_queue_yields_after_each_slow_model_before_its_provider_preflight(self):
        groups = [candidate(f'group-{index}',offset=index*10) for index in range(4)]
        raw = [row for group in groups for row in raw_fixtures(group['fixtures'])]
        api.save_fixtures(raw,self.root/'fixtures.json')
        refresh = self.refresh()
        self.gate.results_checkpoint = refresh.check_results
        events = []
        def build(*args, decision_at):
            self.assertEqual(decision_at,NOW)
            events.append('model')
            self.elapsed += 61
            return dict(methods={'clubs':{'version':'synthetic'}},unavailable={})
        def fetch(**params):
            events.append('preflight')
            ids = set(params['ids'].split('-'))
            return deepcopy([row for row in raw if str(row['fixture']['id']) in ids])
        def record(fixtures,methods,**kwargs):
            events.append('record')
            self.assertEqual(kwargs['clock'](),self.clock())
            self.assertEqual(kwargs['metadata']['calendar_checked_at'],self.clock().isoformat())
            self.assertGreater(self.clock(),NOW)
            return dict(id=fixtures[0]['match_id'],created=True,methods=methods)
        with patch.object(daily_engine,'plan',return_value=dict(version='synthetic',
                    day=NOW.date().isoformat(),candidates=groups,diagnostics=[])), \
                patch.object(daily.models,'build_mixed_methods',side_effect=build), \
                patch.object(daily.journal,'find_mixed',return_value=None), \
                patch.object(daily.journal,'record_mixed',side_effect=record), \
                patch.object(worker,'_refresh_results',side_effect=lambda *a,**k:events.append('results')), \
                patch.object(worker,'_reconcile',side_effect=lambda *a:events.append('reconcile')):
            result = daily.run(self.root,self.clock,fetch,
                check_time=lambda:worker._forecast_checkpoint(self.gate))
        self.assertEqual(result['created'],len(groups))
        self.assertEqual(events, ['model','results','reconcile','preflight','record']*len(groups))
        self.assertEqual(self.report['result_priority_passes'],len(groups))


if __name__ == '__main__':
    unittest.main()
