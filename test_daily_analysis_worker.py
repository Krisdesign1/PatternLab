"""Isolated coordinator/worker checks: no real API, model or data writes."""
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as automation
import daily_analysis_worker as worker
import prediction_journal as journal

from test_catboost_scenarios import fixtures
from test_prediction_journal import raw_fixtures


NOW = datetime(2025, 3, 20, 9, tzinfo=timezone.utc)


def candidate(identifier='one', offset=0):
    selected = fixtures()[:2]
    selected[1]['league'] = '61'
    for row in selected:
        row['match_id'] = str(int(row['match_id'])+offset)
    return dict(id=identifier, fixtures=selected,
                selection=dict(policy='daily-plan-test', strategy='support', data_support_index=80),
                competitions=[dict(league='39',competition='English',country='England'),
                              dict(league='61',competition='French',country='France')])


class DailyWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.current = NOW
        self.candidates = [candidate()]
        self.raw = raw_fixtures(self.candidates[0]['fixtures'])
        api.save_fixtures(self.raw, self.root/'fixtures.json')
        planner = patch.dict(sys.modules, {'daily_analysis_engine':SimpleNamespace(plan=self._plan)})
        planner.start()
        self.addCleanup(planner.stop)
        self.find = self._patch(worker.journal, 'find_mixed', return_value=None)
        self.build = self._patch(worker.models, 'build_mixed_methods', return_value=dict(
            methods={'clubs':{'version':'synthetic'}}, unavailable={'combined':'Singleton component.'}))
        self.record = self._patch(worker.journal, 'record_mixed', side_effect=self._record)
        self.fetch_calls = []
        self._patch(api, 'load_key', side_effect=AssertionError('No credential reads'))
        self._patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No network'))

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _plan(self, raw, now=None, existing=()):
        prior = {row['id'] for row in existing}
        return dict(version='test-plan-v1', day=now.astimezone(worker.ZONE).date().isoformat(),
                    candidates=[copy.deepcopy(row) for row in self.candidates if row['id'] not in prior], diagnostics=[])

    def _record(self, selected, methods, **kwargs):
        # The real backend owns the transaction/time checks; assert clock is
        # passed as a callable, rather than a prematurely frozen now value.
        self.assertNotIn('now', kwargs)
        self.assertEqual(kwargs['clock'](), self.current)
        return dict(id='saved-'+selected[0]['match_id'], created=True, methods=methods)

    def fetch(self, **params):
        self.fetch_calls.append(params)
        wanted = set(params['ids'].split('-'))
        return copy.deepcopy([row for row in self.raw if str(row['fixture']['id']) in wanted])

    def run_worker(self, **kwargs):
        return worker.run(self.root, lambda:self.current, self.fetch, **kwargs)

    def test_anchor_precedes_calculation_preflight_follows_and_metadata_is_immutable(self):
        def build(history, selected):
            state = json.loads((self.root/worker.STATE_NAME).read_text())
            self.assertEqual(state['days']['2025-03-20']['one']['fixtures'], selected)
            self.assertEqual(self.fetch_calls, [])
            return {'methods':{'clubs':{'version':'synthetic'}},'unavailable':{}}
        self.build.side_effect = build
        before = copy.deepcopy(self.candidates)
        result = self.run_worker()
        self.assertEqual(result['created'], 1)
        self.assertEqual(self.fetch_calls, [{'ids':'9000-9001','timezone':'UTC'}])
        self.assertEqual(self.record.call_args.kwargs['metadata']['daily_policy'], worker.VERSION)
        self.assertEqual(self.candidates, before)
        mapping = worker.load_selections(self.root)
        self.assertEqual(mapping[0]['entry_id'], 'saved-9000')
        self.assertEqual(mapping[0]['selection']['data_support_index'], 80)
        self.assertEqual(set(mapping[0]['competitions']), {'39','61'})

    def test_existing_legacy_forecast_is_linked_without_model_call_or_new_metadata(self):
        old = dict(id='legacy', group_kind='single', methods={'clubs':{'version':'old'}})
        self.find.return_value = old
        result = self.run_worker()
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['already_recorded'], 1)
        self.build.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(self.fetch_calls, [])
        self.assertEqual(worker.load_selections(self.root)[0]['entry_id'], 'legacy')
        self.assertNotIn('metadata', old)

    def test_repeat_after_record_never_recalculates_or_replaces_anchor(self):
        self.run_worker()
        self.build.reset_mock()
        self.record.reset_mock()
        self.candidates[0]['selection']['data_support_index'] = 1
        second = self.run_worker()
        self.assertEqual(second['created'], 0)
        self.build.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(worker.load_selections(self.root)[0]['selection']['data_support_index'], 80)

    def test_started_matches_and_late_calendar_arrivals_never_reanchor_frozen_plan(self):
        worker.run(self.root,lambda:self.current)  # freeze without preflight
        original=json.loads((self.root/worker.STATE_NAME).read_text())['days']['2025-03-20']
        self.current=NOW+timedelta(hours=4)
        later=candidate('late',100)
        for row in later['fixtures']:row['timestamp']='2025-03-20T18:00:00'
        self.candidates=[later]
        self.raw+=raw_fixtures(later['fixtures'])
        api.save_fixtures(self.raw,self.root/'fixtures.json')
        result=self.run_worker()
        self.assertEqual(result['candidates'],1)
        self.assertEqual(result['jobs'][0]['candidate_id'],'one')
        self.assertEqual(result['jobs'][0]['fixtures'],original['one']['fixtures'])
        self.assertEqual(result['jobs'][0]['state'],'expired')
        self.record.assert_not_called()

    def test_without_preflight_callback_no_model_credentials_or_journal_write(self):
        result = worker.run(self.root, lambda:self.current)
        self.assertEqual(result['status'], 'awaiting_preflight')
        self.assertEqual(result['remaining'], 1)
        self.build.assert_not_called()
        self.record.assert_not_called()
        self.assertFalse((self.root/'predictions.sqlite3').exists())

    def test_empty_plan_does_not_fetch_or_build(self):
        self.candidates = []
        result = self.run_worker()
        self.assertEqual((result['candidates'],result['created'],result['remaining']), (0,0,0))
        self.assertEqual(self.fetch_calls, [])
        self.build.assert_not_called()

    def test_started_or_non_ns_cached_group_cannot_be_backfilled(self):
        for mode in ('started','live'):
            with self.subTest(mode=mode):
                if mode=='started': self.current = NOW+timedelta(hours=4)
                else:
                    self.current=NOW
                    self.raw[0]['fixture']['status']['short']='1H'
                    api.save_fixtures(self.raw,self.root/'fixtures.json')
                result = self.run_worker()
                self.assertEqual(result['created'],0)
                self.assertEqual(result['jobs'][0]['state'],'expired')
        self.build.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(self.fetch_calls, [])

    def test_fresh_live_incomplete_and_changed_club_responses_never_write(self):
        for mode in ('live','missing','duplicate','club'):
            with self.subTest(mode=mode):
                self.root = self.root/mode
                api.save_fixtures(self.raw,self.root/'fixtures.json')
                fresh = copy.deepcopy(self.raw)
                if mode=='live':fresh[0]['fixture']['status']['short']='1H'
                elif mode=='missing':fresh.pop()
                elif mode=='duplicate':fresh=[fresh[0],fresh[0]]
                else:fresh[0]['teams']['home']['id']=999999
                result=worker.run(self.root,lambda:self.current,lambda **kwargs:fresh)
                self.assertEqual(result['created'],0)
        self.record.assert_not_called()

    def test_clock_crosses_kickoff_or_local_midnight_during_build_no_preflight_or_record(self):
        for stage in ('kickoff','midnight'):
            with self.subTest(stage=stage):
                self.root = self.root/stage
                api.save_fixtures(self.raw,self.root/'fixtures.json')
                self.current=NOW
                def build(*args):
                    self.current = NOW+timedelta(hours=4 if stage=='kickoff' else 14)
                    return {'methods':{'clubs':{}},'unavailable':{}}
                self.build.side_effect=build
                result=self.run_worker()
                self.assertEqual(result['created'],0)
                self.assertEqual(result['jobs'][0]['state'],'expired')
        self.assertEqual(self.fetch_calls,[])
        self.record.assert_not_called()

    def test_clock_crosses_kickoff_during_preflight_no_record(self):
        def response(**kwargs):
            self.current=NOW+timedelta(hours=4)
            return copy.deepcopy(self.raw)
        result=worker.run(self.root,lambda:self.current,response)
        self.assertEqual(result['created'],0)
        self.assertEqual(result['jobs'][0]['state'],'changed')
        self.record.assert_not_called()

    def test_provider_pause_preserves_plan_and_resumes_without_reanchoring(self):
        class Pause(RuntimeError): pass
        with self.assertRaises(Pause):
            worker.run(self.root,lambda:self.current,lambda **kwargs:(_ for _ in ()).throw(Pause('provider quota')))
        self.assertEqual(worker.load_status(self.root)['status'],'paused')
        state=json.loads((self.root/worker.STATE_NAME).read_text())
        original=copy.deepcopy(state['days']['2025-03-20']['one']['fixtures'])
        result=self.run_worker()
        self.assertEqual(result['created'],1)
        self.assertEqual(result['jobs'][0]['fixtures'],original)

    def test_default_attempts_every_candidate_in_frozen_queue(self):
        self.candidates=[candidate(str(i),i*10) for i in range(7)]
        self.raw=[row for selected in self.candidates for row in raw_fixtures(selected['fixtures'])]
        api.save_fixtures(self.raw,self.root/'fixtures.json')
        result=self.run_worker()
        self.assertEqual((result['attempted'],result['created'],result['remaining']),(7,7,0))
        self.assertEqual(len(self.fetch_calls),7)
        self.assertEqual(len(worker.load_selections(self.root)),7)

    def test_time_pause_before_calculation_retains_frozen_queue_for_resume(self):
        class Pause(RuntimeError): pass
        checks=[]
        def check_time():
            checks.append(True)
            if len(checks)==3:
                raise Pause('Host deadline')
        with self.assertRaises(Pause):
            self.run_worker(check_time=check_time)
        self.assertEqual(len(checks),3)  # planner, candidate, expensive model build
        self.build.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(self.fetch_calls,[])
        paused=worker.load_status(self.root)
        self.assertEqual(paused['status'],'paused')
        anchor=copy.deepcopy(paused['jobs'][0]['fixtures'])
        self.candidates=[candidate('replacement',100)]
        resumed=self.run_worker()
        self.assertEqual(resumed['created'],1)
        self.assertEqual(resumed['jobs'][0]['fixtures'],anchor)
        self.assertEqual(resumed['jobs'][0]['candidate_id'],'one')

    def test_time_callback_runs_before_each_candidate_and_model_build(self):
        self.candidates=[candidate(str(i),i*10) for i in range(2)]
        self.raw=[row for selected in self.candidates for row in raw_fixtures(selected['fixtures'])]
        api.save_fixtures(self.raw,self.root/'fixtures.json')
        events=[]
        def build(history, selected):
            events.append('build')
            return {'methods':{'clubs':{'version':'synthetic'}},'unavailable':{}}
        self.build.side_effect=build
        self.run_worker(check_time=lambda:events.append('check'))
        self.assertEqual(events,['check','check','check','build','check','check','build'])

    def test_explicit_batch_accepts_any_positive_integer_and_rejects_invalid_inputs(self):
        for value in (0,-1,True,1.5,'2'):
            with self.subTest(max_jobs=value), self.assertRaises(worker.DailyAnalysisError):
                self.run_worker(max_jobs=value)
        with self.assertRaises(worker.DailyAnalysisError):
            self.run_worker(check_time=False)
        self.assertEqual(self.run_worker(max_jobs=101)['created'],1)

    def test_transient_record_failure_remains_retryable_with_original_anchor(self):
        self.record.side_effect=journal.JournalError('Temporary isolated storage failure')
        first=self.run_worker()
        self.assertEqual(first['jobs'][0]['state'],'awaiting_record')
        self.assertEqual(first['remaining'],1)
        self.record.side_effect=self._record
        second=self.run_worker()
        self.assertEqual(second['created'],1)
        self.assertEqual(second['jobs'][0]['fixtures'],first['jobs'][0]['fixtures'])

    def test_max_jobs_resumes_every_candidate_and_insufficient_group_cannot_starve(self):
        self.candidates=[candidate(str(i),i*10) for i in range(5)]
        self.raw=[row for selected in self.candidates for row in raw_fixtures(selected['fixtures'])]
        api.save_fixtures(self.raw,self.root/'fixtures.json')
        seen=[]
        def build(rows,selected):
            seen.append(selected[0]['match_id'])
            return {'methods':{},'unavailable':{'clubs':'insufficient'}}
        self.build.side_effect=build
        first=self.run_worker(max_jobs=2)
        self.assertEqual(first['attempted'],2)
        self.current+=timedelta(minutes=1)
        self.run_worker(max_jobs=2)
        self.current+=timedelta(minutes=1)
        self.run_worker(max_jobs=2)
        self.assertEqual(len(set(seen)),5)
        self.assertEqual(self.fetch_calls,[])

    def test_daily_lock_and_corrupt_state_fail_before_compute(self):
        with (self.root/'daily_analysis.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(worker.DailyAnalysisError,'déjà'):
                self.run_worker()
        (self.root/worker.STATE_NAME).write_text('{broken')
        with self.assertRaisesRegex(worker.DailyAnalysisError,'invalide'):
            self.run_worker()
        self.build.assert_not_called()
        self.record.assert_not_called()

    def test_run_now_reuses_provider_gate_and_never_calls_training_or_history(self):
        self._patch(automation,'_time',side_effect=lambda now=None:self.current)
        # Use the real gate and its durable request counters; the client
        # and every input file are isolated synthetic data.
        api.load_key.side_effect=None
        api.load_key.return_value='synthetic-test-key'
        client=SimpleNamespace(last_quota={}, fixtures=lambda **kwargs:dict(errors=[],results=len(self.raw),response=copy.deepcopy(self.raw)))
        self._patch(api,'ApiFootballClient',return_value=client)
        history=self._patch(automation,'_history',side_effect=AssertionError('No history import'))
        result=worker.run_now(self.root,max_jobs=1)
        self.assertEqual(result['created'],1)
        self.assertEqual(result['requests'],2)
        state=json.loads((self.root/'automation_state.json').read_text())
        self.assertEqual(state['requests_today'],2)
        self.assertEqual(len(state['minute_calls']),2)
        history.assert_not_called()


class DailyIntegrationTests(unittest.TestCase):
    def test_real_engine_hook_retains_mono_ids_and_probabilities_and_records_variable_sizes(self):
        from test_catboost_scenarios import sample
        from test_automation_worker import Client
        import catboost_scenarios as ml
        import prospective_models as models
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            history=[]
            for row in sample():
                item=raw_fixtures([row],status='FT')[0]
                item['score']={period:dict(zip(('home','away'),map(int,row[field].split('-'))))
                              for period,field in (('halftime','ht'),('fulltime','ft'))}
                history.append(item)
            future=raw_fixtures()
            api.save_fixtures(history+future,root/'fixtures.json')
            config=dict(automation.DEFAULT_CONFIG,sizes=[2,4],history_enabled=False)
            with patch.object(ml,'load_report',return_value=None), patch.object(models,'_context_neighbors',
                    side_effect=models.SnapshotError('Isolated missing configuration.')):
                client=Client(future)
                first=automation.run_cycle(root=root,config=config,client=client,now=NOW)
                self.assertEqual(first['status'],'completed',first)
                daily=first['daily_analyses']
                self.assertEqual(daily['created'],4)
                self.assertEqual(first['created'],0)
                self.assertEqual(first['already_recorded'],3)
                self.assertEqual(first['requests'],5)  # one calendar + four daily preflights
                saved=journal.list_entries(root/'predictions.sqlite3')
                self.assertEqual({entry['size'] for entry in saved},{2,3,4})
                self.assertEqual(len(saved),4)
                original_sets=[fixtures()[:2],fixtures()[2:],fixtures()]
                byid={entry['id']:entry for entry in saved}
                for selected in original_sets:
                    identifier=journal._group_key(journal._fixtures(selected))
                    self.assertIn(identifier,byid)
                    entry=byid[identifier]
                    baseline=models.build_methods(sample(),selected)['methods']
                    self.assertEqual(set(entry['methods']),set(baseline))
                    for name in baseline:
                        self.assertEqual(entry['methods'][name]['ranking'],baseline[name]['ranking'])
                        component=entry['methods'][name]['distribution']['components'][0]['method']
                        self.assertEqual(component['version'],baseline[name]['version'])
                links=worker.load_selections(root)
                self.assertEqual({link['entry_id'] for link in links},set(byid))
                frozen={entry['id']:entry['methods'] for entry in saved}
                second=automation.run_cycle(root=root,config=config,client=client,now=NOW)
                self.assertEqual(second['requests'],1)
                self.assertEqual(second['daily_analyses']['created'],0)
                self.assertEqual({entry['id']:entry['methods'] for entry in journal.list_entries(root/'predictions.sqlite3')},frozen)
                for item in future:item['fixture']['status']['short']='FT'
                settled=automation.run_cycle(root=root,config=config,client=Client(future),
                                              now=NOW+timedelta(hours=8))
                self.assertEqual(settled['settled'],4)
                self.assertEqual({entry['id']:entry['methods'] for entry in journal.list_entries(root/'predictions.sqlite3')},frozen)

    def test_actual_provider_quota_pause_preserves_immutable_plan(self):
        from test_catboost_scenarios import sample
        import catboost_scenarios as ml
        import prospective_models as models
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            all_history=[]
            chosen=candidate()['fixtures']
            for league in ('39','61'):
                for row in sample(count=80,league=league):
                    row=dict(row,match_id=str(int(row['match_id'])+(10000 if league=='61' else 0)))
                    item=raw_fixtures([row],status='FT')[0]
                    item['score']={period:dict(zip(('home','away'),map(int,row[field].split('-'))))
                                  for period,field in (('halftime','ht'),('fulltime','ft'))}
                    all_history.append(item)
            future=raw_fixtures(chosen)
            api.save_fixtures(all_history+future,root/'fixtures.json')
            config=dict(automation.DEFAULT_CONFIG)
            automation.save_config(config,root)
            current=NOW
            calls=[]
            def calendar(**kwargs):
                calls.append(kwargs)
                client.last_quota={'requests_remaining_day':0}
                return dict(errors=[],results=len(future),response=future)
            client=SimpleNamespace(last_quota={},fixtures=calendar)
            with patch.object(automation,'_time',side_effect=lambda now=None:current), \
                 patch.object(api,'load_key',return_value='synthetic'), \
                 patch.object(api,'ApiFootballClient',return_value=client), \
                 patch.object(ml,'load_report',return_value=None), \
                 patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('Missing test config.')):
                with self.assertRaises(automation._Pause):worker.run_now(root)
                original=json.loads((root/worker.STATE_NAME).read_text())['days']['2025-03-20']
                self.assertTrue(original)
                with self.assertRaises(automation._Pause):worker.run_now(root)
                self.assertEqual(json.loads((root/worker.STATE_NAME).read_text())['days']['2025-03-20'],original)
            self.assertFalse((root/'predictions.sqlite3').exists())
            self.assertEqual(worker.load_status(root)['status'],'paused')
            self.assertTrue(json.loads((root/worker.STATE_NAME).read_text())['days']['2025-03-20'])
            self.assertEqual(json.loads((root/'automation_state.json').read_text())['requests_today'],1)
            self.assertEqual(len(calls),1)


if __name__=='__main__':
    unittest.main()
