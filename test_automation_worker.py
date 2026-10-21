import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as worker
import daily_analysis_worker
import catboost_scenarios as ml
import prediction_journal as journal
import prospective_models as models
from test_catboost_scenarios import sample, fixtures
from test_prediction_journal import raw_fixtures, AFTER
from test_api_match_workspace import payload, raw as raw_match


BEFORE = datetime(2025, 3, 20, 9, tzinfo=timezone.utc)


class Client:
    def __init__(self, matches):
        self.matches = copy.deepcopy(matches)
        self.calls = []
        self.last_quota = {}
        self.fail = None

    def fixtures(self, **params):
        self.calls.append(params)
        if self.fail:
            raise self.fail
        if 'ids' in params:
            ids = set(params['ids'].split('-'))
            return payload([r for r in self.matches if str(r['fixture']['id']) in ids])
        if 'date' in params:
            from zoneinfo import ZoneInfo
            return payload([r for r in self.matches if
                            worker.workspace._stamp(r).astimezone(ZoneInfo(worker.TIMEZONE)).date().isoformat() == params['date']])
        return payload([])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = dict(worker.DEFAULT_CONFIG, sizes=[2, 4], history_requests_per_cycle=0)
        # This suite exercises the original per-league queue in isolation.
        # The real daily hook, its variable sizes and shared budgets are covered
        # separately by test_daily_analysis_worker.DailyIntegrationTests.
        daily = patch.object(daily_analysis_worker, 'run', return_value=dict(
            status='completed', candidates=0, created=0, remaining=0))
        daily.start()
        self.addCleanup(daily.stop)
        self.history = []
        for r in sample():
            item = raw_fixtures([r], status='FT')[0]
            item['score'] = {p:dict(zip(('home', 'away'), map(int, r[k].split('-'))))
                             for p, k in (('halftime', 'ht'), ('fulltime', 'ft'))}
            self.history.append(item)
        self.future = raw_fixtures()
        api.save_fixtures(self.history + self.future, self.root/'fixtures.json')
        for p in (patch.object(ml, 'load_report', return_value=None),
                  patch.object(models, '_context_neighbors', side_effect=models.SnapshotError('Configuration absente.'))):
            p.start()
            self.addCleanup(p.stop)

    def run_cycle(self, client=None, now=BEFORE, **kwargs):
        return worker.run_cycle(root=self.root, config=self.config, client=client or Client(self.future), now=now, **kwargs)

    def test_cycle_freezes_all_sizes_and_second_cycle_is_idempotent(self):
        client = Client(self.future)
        first = self.run_cycle(client)
        self.assertEqual(first['status'], 'completed', first)
        self.assertEqual(first['created'], 3)
        self.assertEqual(first['requests'], 4)
        entries = journal.list_entries(self.root/'predictions.sqlite3')
        self.assertEqual(len(entries), 3)
        self.assertEqual({e['size'] for e in entries}, {2, 4})
        for entry in entries:
            self.assertEqual(entry['metadata']['automation_policy'], worker.VERSION)
            self.assertEqual(set(entry['methods']), {'frequency', 'clubs', 'combined'})
            self.assertIn('catboost', entry['metadata']['unavailable'])
        second = self.run_cycle(client)
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['already_recorded'], 3)
        self.assertEqual(second['requests'], 1)
        self.assertEqual(len(journal.list_entries(self.root/'predictions.sqlite3')), 3)

    def test_today_only_calendar_and_cached_tomorrow_are_excluded_without_erasing_them(self):
        tomorrow=[raw_match(10000+i,stamp='2025-03-21T12:00:00Z',league=777,season=2025) for i in range(2)]
        api.save_fixtures(tomorrow,self.root/'fixtures.json')
        past_key='2025-03-18/39/2'
        tomorrow_key='2025-03-21/777/2'
        existing={past_key:[['8000','8001']],tomorrow_key:[['10000','10001']]}
        worker._write(self.root/'automation_state.json',dict(version=worker.VERSION,plans=existing))
        client=Client(self.future+tomorrow)
        report=self.run_cycle(client)
        self.assertEqual(report['status'],'completed',report)
        self.assertEqual([call['date'] for call in client.calls if 'date' in call],['2025-03-20'])
        self.assertEqual(report['created'],3)
        self.assertEqual({job['day'] for job in report['jobs']},{'2025-03-20'})
        coverage=worker.coverage(root=self.root,config=self.config,now=BEFORE)
        self.assertEqual([row['league'] for row in coverage],['39'])
        self.assertEqual(coverage[0]['fixtures'],4)
        stored={str(row['fixture']['id']):row for row in api.load_fixtures(self.root/'fixtures.json')}
        self.assertEqual([stored[str(row['fixture']['id'])] for row in tomorrow],tomorrow)
        plans=json.loads((self.root/'automation_state.json').read_text())['plans']
        for key,blocks in existing.items():self.assertEqual(plans[key],blocks)
        self.assertEqual(json.loads((self.root/'automation_state.json').read_text())['version'],worker.VERSION)

    def test_kinshasa_day_begins_before_utc_midnight_and_excludes_next_local_day(self):
        self.config['sizes']=[2]
        early=[raw_match(10010,stamp='2025-03-19T23:05:00Z',league=39,season=2025),
               raw_match(10011,stamp='2025-03-20T00:05:00Z',league=39,season=2025)]
        tomorrow=[raw_match(10012+i,stamp='2025-03-20T23:05:00Z',league=777,season=2025) for i in range(2)]
        # Replace only the isolated test store to avoid unrelated noon groups.
        self.root=self.root/'boundary'
        api.save_fixtures(self.history+early+tomorrow,self.root/'fixtures.json')
        now=datetime(2025,3,19,23,2,tzinfo=timezone.utc)
        client=Client(early+tomorrow)
        report=self.run_cycle(client,now=now)
        self.assertEqual(report['created'],1,report)
        self.assertEqual([call['date'] for call in client.calls if 'date' in call],['2025-03-20'])
        entries=journal.list_entries(self.root/'predictions.sqlite3')
        self.assertEqual([row['match_id'] for row in entries[0]['fixtures']],['10010','10011'])
        coverage=worker.coverage(root=self.root,config=self.config,now=now)
        self.assertEqual([row['league'] for row in coverage],['39'])
        self.assertEqual(coverage[0]['fixtures'],2)
        self.assertEqual(coverage[0]['per_size'][0]['before'],'2025-03-19')
        self.assertIsNone(worker._fixtures(dict(day='2025-03-21',league='777',ids=['10012','10013']),
                                          {str(row['fixture']['id']):row for row in tomorrow},now))

    def test_local_midnight_during_calendar_request_pauses_before_planning_or_prediction(self):
        current=[datetime(2025,3,20,22,58,tzinfo=timezone.utc)]
        client=Client(self.future)
        original=client.fixtures
        def response(**params):
            result=original(**params)
            if 'date' in params:current[0]=datetime(2025,3,20,23,tzinfo=timezone.utc)
            return result
        client.fixtures=response
        with patch.object(worker,'_time',side_effect=lambda now=None:current[0]), \
             patch.object(models,'build_methods',side_effect=AssertionError('No forecast after local day rollover')):
            report=worker.run_cycle(root=self.root,config=self.config,client=client)
        self.assertEqual(report['status'],'paused',report)
        self.assertEqual(report['created'],0)
        self.assertEqual(report['groups_considered'],0)
        self.assertEqual([call['date'] for call in client.calls if 'date' in call],['2025-03-20'])
        self.assertFalse((self.root/'predictions.sqlite3').exists())

    def test_local_midnight_during_compute_or_preflight_cannot_register_any_group(self):
        self.config['sizes']=[2]
        late=copy.deepcopy(self.future[:2])
        for i,row in enumerate(late):row['fixture']['date']=f'2025-03-20T22:59:{10+i:02d}Z'
        for stage in ('compute','preflight'):
            with self.subTest(stage=stage):
                root=self.root/stage
                api.save_fixtures(self.history+late,root/'fixtures.json')
                current=[datetime(2025,3,20,22,58,tzinfo=timezone.utc)]
                client=Client(late)
                build=models.build_methods
                request=client.fixtures
                def compute(*args,**kwargs):
                    value=build(*args,**kwargs)
                    if stage=='compute':current[0]=datetime(2025,3,20,23,tzinfo=timezone.utc)
                    return value
                def response(**params):
                    value=request(**params)
                    if stage=='preflight' and 'ids' in params:current[0]=datetime(2025,3,20,23,tzinfo=timezone.utc)
                    return value
                client.fixtures=response
                with patch.object(worker,'_time',side_effect=lambda now=None:current[0]), \
                     patch.object(models,'build_methods',side_effect=compute), \
                     patch.object(journal,'record',side_effect=AssertionError('No record after local midnight')):
                    report=worker.run_cycle(root=root,config=self.config,client=client)
                self.assertEqual(report['status'],'paused',report)
                self.assertEqual(report['created'],0)
                self.assertEqual(len(client.calls),1 if stage=='compute' else 2)
                self.assertFalse((root/'predictions.sqlite3').exists())

    def test_dry_run_and_disabled_never_call_api_or_write(self):
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        client = Client(self.future)
        self.assertEqual(self.run_cycle(client, dry_run=True)['status'], 'dry_run')
        self.config['enabled'] = False
        self.assertEqual(self.run_cycle(client)['status'], 'disabled')
        self.assertEqual(client.calls, [])
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()}, before)

    def test_no_hardcoded_league_filter_and_insufficient_is_explicit(self):
        extra = [raw_match(9900+i, stamp='2025-03-20T16:00:00Z', league=777, season=2025) for i in range(2)]
        extra[1]['teams']['home'] = dict(id=11, name='Another home')
        api.save_fixtures(extra, self.root/'fixtures.json')
        client = Client(self.future+extra)
        report = self.run_cycle(client)
        self.assertEqual(report['coverage_competitions'], 2)
        self.assertTrue(any(j['league'] == '777' and j['state'] == 'insufficient' for j in report['jobs']))
        rows = worker.coverage(root=self.root, config=self.config, now=BEFORE)
        self.assertEqual({r['league'] for r in rows}, {'39', '777'})
        self.assertEqual(next(r['history_matches'] for r in rows if r['league']=='777'), 0)

    def test_started_matches_do_not_shift_group_anchors(self):
        self.run_cycle()
        state1 = json.loads((self.root/'automation_state.json').read_text())['plans']
        # First match now live; pair 9001-9002 must never replace 9000-9001.
        current = datetime(2025, 3, 20, 12, 30, tzinfo=timezone.utc)
        client = Client(self.future)
        client.matches[0]['fixture']['status']['short'] = '1H'
        report = self.run_cycle(client, now=current)
        state2 = json.loads((self.root/'automation_state.json').read_text())['plans']
        self.assertEqual(state1, state2)
        self.assertEqual(report['created'], 0)
        entries = journal.list_entries(self.root/'predictions.sqlite3')
        self.assertNotIn({'9001','9002'}, [{r['match_id'] for r in e['fixtures']} for e in entries])

    def test_new_calendar_fixture_does_not_reanchor_locked_groups(self):
        self.run_cycle()
        extra = raw_match(8999, stamp='2025-03-20T11:00:00Z', season=2025)
        self.run_cycle(Client(self.future+[extra]))
        plans = json.loads((self.root/'automation_state.json').read_text())['plans']
        self.assertEqual(plans['2025-03-20/39/2'], [['9000','9001'], ['9002','9003']])

    def test_fresh_status_recheck_rejects_a_match_started_during_computation(self):
        client = Client(self.future)
        original = client.fixtures
        def response(**params):
            if 'ids' in params:
                client.matches[0]['fixture']['status']['short'] = '1H'
            return original(**params)
        client.fixtures = response
        report = self.run_cycle(client)
        self.assertEqual(report['created'], 1)  # Only pair 9002/9003 is still prospective.
        entries = journal.list_entries(self.root/'predictions.sqlite3')
        self.assertEqual([r['match_id'] for r in entries[0]['fixtures']], ['9002','9003'])

    def test_clock_advances_past_kickoff_during_calculation(self):
        current = [BEFORE]
        original = models.build_methods
        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            current[0] = AFTER
            return result
        with patch.object(worker, '_time', side_effect=lambda now=None: current[0]), \
             patch.object(journal, '_now', side_effect=lambda now=None: current[0]), \
             patch.object(models, 'build_methods', side_effect=build):
            report = worker.run_cycle(root=self.root, config=self.config, client=Client(self.future))
        self.assertEqual(report['created'], 0)
        self.assertFalse((self.root/'predictions.sqlite3').exists())

    def test_budget_reservation_persists_even_on_failed_request(self):
        self.config['daily_request_budget'] = 1
        client = Client(self.future)
        client.fail = api.ApiFootballError('Connexion impossible.')
        report = self.run_cycle(client)
        self.assertEqual(report['requests'], 1)
        state = json.loads((self.root/'automation_state.json').read_text())
        self.assertEqual(state['requests_today'], 1)
        again = self.run_cycle(client)
        self.assertEqual(again['status'], 'paused')
        self.assertEqual(again['requests'], 0)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.root/'predictions.sqlite3').exists())

    def test_daily_budget_resets_on_next_utc_day(self):
        self.config['daily_request_budget'] = 1
        client = Client([])
        self.run_cycle(client)
        result = self.run_cycle(client, now=BEFORE+timedelta(days=1))
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['requests_today'], 1)

    def test_quota_reserve_and_rolling_minute_are_enforced(self):
        client = Client(self.future)
        client.last_quota = {'requests_remaining_day': 5}
        report = self.run_cycle(client)
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(report['requests'], 1)
        self.assertEqual(report['created'], 0)
        # A persisted full rolling-minute window refuses even the calendar call.
        state = json.loads((self.root/'automation_state.json').read_text())
        state.update(minute_calls=[BEFORE.timestamp()]*10, quota={})
        worker._write(self.root/'automation_state.json', state)
        self.assertEqual(self.run_cycle()['requests'], 0)

    def test_provider_minute_quota_waits_without_an_extra_request(self):
        client = Client(self.future)
        client.last_quota = {'requests_available_minute':0}
        first = self.run_cycle(client)
        self.assertEqual(first['requests'], 1)
        self.assertEqual(first['status'], 'paused')
        second = self.run_cycle(client)
        self.assertEqual(second['requests'], 0)
        self.assertEqual(len(client.calls), 1)

    def test_budget_pause_does_not_postpone_an_unattempted_history_import(self):
        self.config.update(history_requests_per_cycle=2, cycle_request_budget=1, sizes=[20])
        report = self.run_cycle()
        self.assertEqual(report['status'], 'paused')
        state = json.loads((self.root/'automation_state.json').read_text())
        self.assertEqual(state['history_attempts'], {})
        self.config['cycle_request_budget'] = 5
        report = self.run_cycle(now=BEFORE+timedelta(minutes=15))
        self.assertEqual(report['history_imports'], 2)

    def test_previous_start_year_season_is_refreshed_in_january(self):
        marker_raw = copy.deepcopy(self.future)
        for item in marker_raw:
            item['league']['season'] = 2024
        api.save_fixtures(marker_raw, self.root/'fixtures.json', season_imports=[dict(
            competition_code='39', season=2024, match_ids=[str(r['fixture']['id']) for r in marker_raw])])
        store = json.loads((self.root/'fixtures.json').read_text())
        store['season_imports'][0]['imported_at'] = '2024-12-01T00:00:00Z'
        worker._write(self.root/'fixtures.json', store)
        self.config.update(history_requests_per_cycle=1, history_seasons=[2024], sizes=[20])
        client = Client([])
        report = self.run_cycle(client, now=datetime(2025,1,2,tzinfo=timezone.utc))
        self.assertEqual(report['history_imports'], 1)
        self.assertIn(dict(league='39',season=2024,timezone='UTC'), client.calls)

    def test_missing_key_is_saved_as_a_cycle_error_without_api_use(self):
        with patch.object(api, 'load_key', return_value=''):
            report = worker.run_cycle(root=self.root, config=self.config, now=BEFORE)
        self.assertEqual(report['status'], 'error')
        self.assertEqual(report['requests'], 0)
        self.assertEqual(worker.load_status(root=self.root)['status'], 'error')

    def test_safe_api_error_explains_failure_without_exposing_secret(self):
        client = Client(self.future)
        client.fail = api.ApiFootballError('Clé refusée : isolated-test-secret')
        with patch.object(api, 'load_key', return_value='isolated-test-secret'):
            report = self.run_cycle(client)
        self.assertEqual(report['status'], 'error')
        self.assertIn('Clé refusée', report['issues'][0])
        self.assertNotIn('isolated-test-secret', json.dumps(report))
        self.assertIn('[clé masquée]', report['issues'][0])

    def test_current_season_refresh_and_missing_import_share_bounded_slots(self):
        current_raw = copy.deepcopy(self.future)
        for item in current_raw:
            item['league']['season'] = 2025
        api.save_fixtures(current_raw, self.root/'fixtures.json', season_imports=[dict(
            competition_code='39', season=2025, match_ids=[str(r['fixture']['id']) for r in current_raw])])
        store = json.loads((self.root/'fixtures.json').read_text())
        store['season_imports'][0]['imported_at'] = '2025-03-01T00:00:00Z'
        worker._write(self.root/'fixtures.json', store)
        self.config.update(history_requests_per_cycle=2, history_seasons=[2024,2025], sizes=[20])
        client = Client(self.future)
        report = self.run_cycle(client)
        years = [r['season'] for r in client.calls if 'league' in r]
        self.assertEqual(years, [2025,2024])
        self.assertEqual(report['history_imports'], 2)

    def test_upcoming_history_takes_priority_without_starving_background_imports(self):
        self.config.update(history_seasons=[2024],history_requests_per_cycle=2,sizes=[20])
        # League 777 plays tomorrow; league 39's existing future is removed
        # from the calendar response while its historic results stay stored.
        future=[raw_match(9999,stamp='2025-03-21T15:00:00Z',league=777,season=2025)]
        for item in self.future:item['fixture']['status']['short']='CANC'
        api.save_fixtures(self.future+future,self.root/'fixtures.json')
        client=Client(future)
        report=self.run_cycle(client)
        self.assertEqual([p['league'] for p in client.calls if 'league' in p],['777','39'])
        self.assertEqual([p['priority'] for p in report['history_jobs']],['upcoming','background'])

    def test_coverage_distinguishes_ready_sizes_from_calendar_presence(self):
        self.root = self.root/'coverage'
        api.save_fixtures(self.history[:41]+self.future, self.root/'fixtures.json')
        rows = worker.coverage(root=self.root, config=self.config, now=BEFORE)
        league = rows[0]
        self.assertEqual(league['readiness'], 'partial')
        self.assertEqual((league['eligible_groups'], league['ready_groups'], league['pending_groups']), (3,2,1))
        self.assertEqual(league['ready_sizes'], [2])
        two, four = league['per_size']
        self.assertEqual((two['history_groups'],two['missing_matches'],two['eligible']), (20,0,True))
        self.assertEqual((four['history_groups'],four['missing_groups'],four['missing_matches']), (10,10,39))
        self.assertEqual(league['missing_matches'],39)

    def test_coverage_uses_worker_history_and_target_utc_day(self):
        self.root = self.root/'coverage'
        self.config['sizes']=[4]
        current_day = copy.deepcopy(self.history[79])
        current_day['fixture']['date'] = '2025-03-20T08:00:00Z'
        api.save_fixtures(self.history[:79]+[current_day]+self.future, self.root/'fixtures.json')
        worker._write(self.root/'automation_state.json',dict(plans={
            '2025-03-20/39/4':[[str(item['fixture']['id']) for item in self.future]]}))
        row=worker.coverage(root=self.root, config=self.config, now=BEFORE)[0]
        self.assertEqual((row['readiness'],row['history_matches'],row['missing_matches']),('insufficient',79,1))
        self.assertEqual(row['per_size'][0]['before'],'2025-03-20')
        # Local 20 March starts during 19 March UTC; the target's first UTC
        # day must be used, even though the calendar date remains the 20th.
        early = copy.deepcopy(self.future)
        for i,item in enumerate(early):
            item['fixture']['date']=f'2025-03-19T23:{10+i:02d}:00Z'
        api.save_fixtures(early,self.root/'fixtures.json')
        row=worker.coverage(root=self.root, config=self.config, now=datetime(2025,3,19,23,tzinfo=timezone.utc))[0]
        self.assertEqual(row['per_size'][0]['before'],'2025-03-19')
        self.assertFalse(row['per_size'][0]['eligible'])

    def test_coverage_without_future_blocks_is_separate_from_missing_history(self):
        self.root = self.root/'coverage'
        cancelled=copy.deepcopy(self.future)
        for item in cancelled:item['fixture']['status']['short']='CANC'
        api.save_fixtures(self.history[:12]+cancelled,self.root/'fixtures.json')
        row=worker.coverage(root=self.root, config=self.config, now=BEFORE)[0]
        self.assertEqual(row['readiness'],'no_upcoming')
        self.assertEqual((row['eligible_groups'],row['ready_groups'],row['pending_groups']),(0,0,0))
        self.assertIsNone(row['missing_matches'])
        self.assertFalse(row['per_size'][0]['eligible'])

    def test_invalid_scores_are_counted_and_other_leagues_remain_available(self):
        invalid = raw_match(99999, status='FT', league=777, season=2024,
                            stamp='2024-10-18T12:00:00Z')
        invalid['score']['halftime']['home'] = None
        current=raw_match(99998,status='CANC',league=777,season=2025,stamp='2025-03-20T12:00:00Z')
        api.save_fixtures([invalid,current], self.root/'fixtures.json')
        coverage = worker.coverage(root=self.root, config=self.config, now=BEFORE)
        extra = next(r for r in coverage if r['league']=='777')
        self.assertEqual((extra['issues'],extra['history_matches']), (1,0))
        self.assertEqual(self.run_cycle()['created'], 3)

    def test_lock_prevents_overlapping_worker_mutations(self):
        handle = (self.root/'automation.lock').open('a')
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = self.run_cycle()
        self.assertEqual(report['status'], 'busy')
        self.assertFalse((self.root/'automation_state.json').exists())

    def test_results_settle_without_recalculating_saved_forecasts(self):
        self.run_cycle()
        before = {e['id']:e['methods'] for e in journal.list_entries(self.root/'predictions.sqlite3')}
        client = Client(raw_fixtures(status='FT'))
        with patch.object(models, 'build_methods', side_effect=AssertionError('Do not recompute finished forecasts')):
            report = self.run_cycle(client, now=AFTER)
        self.assertEqual(report['settled'], 3)
        entries = journal.list_entries(self.root/'predictions.sqlite3')
        self.assertTrue(all(e['status']=='settled' for e in entries))
        self.assertEqual({e['id']:e['methods'] for e in entries}, before)

    def test_history_queue_includes_every_league_and_advances_after_denial(self):
        self.config.update(history_requests_per_cycle=2, history_seasons=[2024], sizes=[20])
        extra = raw_match(9900, stamp='2025-03-20T16:00:00Z', league=777, season=2025)
        api.save_fixtures([extra], self.root/'fixtures.json')
        client = Client(self.future+[extra])
        original = client.fixtures
        def response(**params):
            if params.get('league') == '39':
                client.calls.append(params)
                raise api.ApiFootballError('Accès API-Football refusé.')
            return original(**params)
        client.fixtures = response
        report = self.run_cycle(client)
        self.assertEqual(report['history_imports'], 1)
        self.assertTrue(any(p.get('league')=='777' for p in client.calls))
        markers = api.load_season_imports(self.root/'fixtures.json')
        self.assertEqual([(m['competition_code'],m['season']) for m in markers], [('777',2024)])
        # Unavailable first league is backed off; completed empty second response isn't refetched.
        client.calls.clear()
        self.run_cycle(client, now=BEFORE+timedelta(minutes=15))
        self.assertFalse(any('league' in p for p in client.calls))

    def test_incomplete_history_response_never_marks_season_imported(self):
        self.config.update(history_requests_per_cycle=1, sizes=[20])
        client = Client(self.future)
        original = client.fixtures
        def response(**params):
            if 'league' in params:
                return payload([], pages=2)
            return original(**params)
        client.fixtures = response
        report = self.run_cycle(client)
        self.assertEqual(report['history_imports'], 0)
        self.assertEqual(api.load_season_imports(self.root/'fixtures.json'), [])

    def test_configuration_validation_and_corrupt_state_fail_closed(self):
        for invalid in ({'sizes':[1]}, {'sizes':[]}, {'source':'other'}, {'enabled':'yes'},
                        {'daily_request_budget':True}, {'history_requests_per_cycle':3}):
            with self.assertRaises(worker.AutomationError):
                worker.save_config(invalid, root=self.root)
        (self.root/'automation_state.json').write_text('not json')
        with self.assertRaises(worker.AutomationError):
            self.run_cycle()


if __name__ == '__main__':
    unittest.main()
