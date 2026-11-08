"""Failure-oriented tests for the standalone local sidecar collector."""
from datetime import datetime, timedelta, timezone
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from halftime_statistics_store import Store, StoreError
from halftime_statistics_collector import collect_once, seed_audit, discover, plan_local
from halftime_statistics_parser import parse_statistics
from halftime_statistics_client import ApiClient

NOW=datetime(2026,10,26,12,tzinfo=timezone.utc)


def task(mid='100'):
    return dict(match_id=mid,league='39',season=2024,kickoff_utc='2024-09-01T15:00:00',home_id='10',away_id='20')


def stats():
    return dict(errors=[],response=[dict(team=dict(id=tid),statistics=[dict(type='Total Shots',value=8),dict(type='Shots on Goal',value=4)],
        statistics_1h=[dict(type='Total Shots',value=3),dict(type='Shots on Goal',value=1)],
        statistics_2h=[dict(type='Total Shots',value=5),dict(type='Shots on Goal',value=3)]) for tid in (20,10)])


def envelope(payload=None,mid='100',headers=None):
    return dict(endpoint='fixtures/statistics',parameters=dict(fixture=int(mid),half='true'),
        observed_at_utc=NOW.isoformat(),transport='received',http_status=200,headers=headers or {},payload=payload if payload is not None else stats())


def raw_fixture(mid=100,when='2024-09-01T15:00:00+00:00'):
    return dict(fixture=dict(id=mid,date=when,status=dict(short='FT')),league=dict(id=39,season=2024),
                teams=dict(home=dict(id=10),away=dict(id=20)),score=dict(halftime=dict(home=0,away=0),fulltime=dict(home=1,away=2)))


class FakeClient:
    def __init__(self,*responses):
        self.responses=list(responses); self.calls=[]
    def get(self,endpoint,**params):
        self.calls.append((endpoint,params))
        value=self.responses.pop(0)
        if isinstance(value,BaseException):
            raise value
        return value


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.store=Store(self.root/'half.sqlite3')
        self.store.enqueue([task()])
    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_ready_atomic_measurements_and_second_run_no_call(self):
        client=FakeClient(envelope())
        result=collect_once(self.store,client,25,now=NOW)
        self.assertEqual(result['ready'],1)
        self.assertEqual(self.store.status()['measurements'],12)
        self.assertEqual(self.store.status()['states'],{'ready':1})
        collect_once(self.store,client,25,now=NOW)
        self.assertEqual(len(client.calls),1)

    def test_crash_reservation_recovered_and_completed_after_restart(self):
        client=FakeClient(RuntimeError('simulated process failure'))
        with self.assertRaises(RuntimeError):
            collect_once(self.store,client,now=NOW)
        self.assertEqual(self.store.db.execute('SELECT finished_at FROM attempts').fetchone()[0],None)
        result=collect_once(self.store,FakeClient(envelope()),now=NOW+timedelta(minutes=1))
        self.assertEqual(result['recovered_interruptions'],1)
        self.assertEqual(result['ready'],1)
        self.assertEqual(self.store.db.execute("SELECT classification FROM attempts WHERE id=1").fetchone()[0],'interrupted')

    def test_old_empty_is_recorded_and_never_hot_retried(self):
        client=FakeClient(envelope(dict(errors=[],response=[])))
        collect_once(self.store,client,now=NOW)
        collect_once(self.store,client,now=NOW+timedelta(days=1))
        self.assertEqual(len(client.calls),1)
        self.assertEqual(self.store.status()['states'],{'empty':1})

    def test_recent_empty_retried_next_day(self):
        row=task();row['kickoff_utc']='2026-10-25T12:00:00'
        self.store.enqueue([row])
        client=FakeClient(envelope(dict(errors=[],response=[])),envelope())
        collect_once(self.store,client,now=NOW)
        self.assertEqual(self.store.status()['availability'],{'empty':1})
        collect_once(self.store,client,now=NOW+timedelta(hours=23))
        self.assertEqual(len(client.calls),1)
        collect_once(self.store,client,now=NOW+timedelta(days=1,seconds=1))
        self.assertEqual(self.store.status()['states'],{'ready':1})

    def test_quota_http200_not_empty_and_pause_survives_reopen(self):
        self.store.enqueue([task('101')])
        response=envelope(dict(errors={'requests':'quota exhausted'},response=[]),headers={'x-ratelimit-requests-remaining':'0'})
        client=FakeClient(response)
        collect_once(self.store,client,now=NOW)
        self.assertEqual(len(client.calls),1)
        self.assertEqual(self.store.status()['availability'],{'api_error':1,'unobserved':1})
        path=self.store.path;self.store.close();self.store=Store(path)
        collect_once(self.store,client,now=NOW+timedelta(hours=1))
        self.assertEqual(len(client.calls),1)

    def test_network_error_is_retry_then_success(self):
        failed=envelope();failed.pop('payload');failed.update(transport='network_error');failed.pop('http_status')
        client=FakeClient(failed,envelope())
        collect_once(self.store,client,now=NOW)
        self.assertEqual(self.store.status()['states'],{'retry':1})
        collect_once(self.store,client,now=NOW+timedelta(seconds=61))
        self.assertEqual(self.store.status()['states'],{'ready':1})

    def test_refused_credentials_pause_all_tasks(self):
        self.store.enqueue([task('101')])
        response=envelope();response.update(transport='http_error',http_status=403);response.pop('payload')
        client=FakeClient(response)
        collect_once(self.store,client,now=NOW)
        collect_once(self.store,client,now=NOW+timedelta(minutes=10))
        self.assertEqual(len(client.calls),1)
        self.assertGreater(self.store.status()['provider_pause_until'],NOW.timestamp()+3500)

    def test_team_identity_change_hides_old_projection(self):
        collect_once(self.store,FakeClient(envelope()),now=NOW)
        self.assertTrue(self.store.before('10','2025-01-01T00:00:00'))
        row=task();row['home_id']='30';self.store.enqueue([row])
        self.assertEqual(self.store.before('10','2025-01-01T00:00:00'),[])
        self.assertEqual(self.store.status()['states'],{'pending':1})
        self.assertEqual(self.store.status()['measurements'],12) # Historical evidence retained.

    def test_information_time_cutoff_blocks_retroactive_use(self):
        collect_once(self.store,FakeClient(envelope()),now=NOW)
        self.assertEqual(self.store.before('10','2024-09-01T15:00:00'),[])
        self.assertEqual(self.store.before('10','2025-01-01T00:00:00',observed_before=NOW-timedelta(days=1)),[])
        self.assertTrue(self.store.before('10','2025-01-01T00:00:00',observed_before=NOW+timedelta(seconds=1)))

    def test_transaction_failure_leaves_no_partial_values(self):
        # Deliberate duplicate measurement causes a mid-transaction constraint failure.
        observation=self.store.reserve('100',source='network',now=NOW)
        parsed=parse_statistics(stats(),('10','20'))
        bad=task();bad['away_id']='10'
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(observation,bad,envelope(),parsed,state='ready',availability='ready',now=NOW)
        self.assertEqual(self.store.status()['measurements'],0)
        self.assertIsNone(self.store.db.execute('SELECT finished_at FROM attempts').fetchone()[0])

    def test_collector_lock_prevents_second_instance(self):
        with self.store.lock():
            with self.assertRaises(StoreError):
                with self.store.lock():
                    pass

    def test_seed_idempotent_and_does_not_adopt_stale_quota(self):
        directory=self.root/'audit';directory.mkdir()
        row=dict(match_id='100',league_id='39',season=2024,kickoff_utc=task()['kickoff_utc'],expected_team_ids=[10,20])
        (directory/'sample_manifest.json').write_text(json.dumps(dict(samples=[row])))
        response=envelope(headers={'x-ratelimit-requests-remaining':'0'})
        (directory/'fixtures_statistics-test.json').write_text(json.dumps(response))
        seed_audit(self.store,directory);seed_audit(self.store,directory)
        self.assertEqual(self.store.status()['attempts'],1)
        self.assertEqual(self.store.status()['provider_pause_until'],0)

    def test_invalid_response_teams_quarantined(self):
        value=stats();value['response'][0]['team']['id']=30
        collect_once(self.store,FakeClient(envelope(value)),now=NOW)
        self.assertEqual(self.store.status()['states'],{'invalid':1})
        self.assertEqual(self.store.status()['measurements'],0)

    def test_shot_inconsistency_removes_metric_not_zero_imputation(self):
        value=stats();value['response'][0]['statistics_1h'][0]['value']=4
        parsed=parse_statistics(value,('10','20'))
        self.assertFalse(parsed['halves_core_shots_available'])
        self.assertNotIn('Total Shots',parsed['periods']['first']['pairs'])
        self.assertTrue(parsed['consistency_issues'])

    def test_plan_reads_source_only_and_deduplicates(self):
        source=self.root/'source.sqlite3';connection=sqlite3.connect(source)
        connection.execute('CREATE TABLE fixtures(raw_json TEXT,status TEXT,normalized_json TEXT,season INT)')
        connection.execute('INSERT INTO fixtures VALUES(?,?,?,?)',(json.dumps(raw_fixture()),'FT','{}',2024))
        connection.execute('INSERT INTO fixtures VALUES(?,?,?,?)',(json.dumps(raw_fixture(101,'2026-10-26T11:00:00+00:00')),'FT','{}',2024))
        connection.commit();connection.close()
        result=plan_local(self.store,source,before=NOW-timedelta(hours=6))
        self.assertEqual(result['eligible'],1);self.assertEqual(result['created'],0)

    def test_readonly_missing_status_creates_nothing(self):
        path=self.root/'missing'/'db.sqlite3'
        with Store(path,readonly=True) as store:
            self.assertFalse(store.status()['exists'])
        self.assertFalse(path.parent.exists())

    def test_discover_validates_and_reuses_saved_manifests(self):
        coverage=dict(transport='received',headers={},http_status=200,payload=dict(errors=[],response=[dict(league=dict(id=39),seasons=[dict(year=2024,coverage=dict(fixtures=dict(statistics_fixtures=True)))])]))
        fixtures=dict(transport='received',headers={},http_status=200,payload=dict(errors=[],paging=dict(total=1),response=[raw_fixture(),raw_fixture(102,'2026-10-27T12:00:00+00:00')]))
        client=FakeClient(coverage,fixtures)
        first=discover(self.store,client,('39',),(2024,),now=NOW)
        second=discover(self.store,client,('39',),(2024,),now=NOW)
        self.assertEqual(first['eligible'],1);self.assertEqual(second['cached_seasons'],1)
        self.assertEqual(len(client.calls),2)


class ClientTests(unittest.TestCase):
    def test_error_envelope_redaction_and_no_redirect_endpoint(self):
        class Response:
            status=200;headers={'X-Ratelimit-Remaining':'12','Authorization':'sensitive'}
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return b'{"errors":{"message":"secret123"},"response":[]}'
        class Opener:
            def open(self,request,timeout):
                self.url=request.full_url;return Response()
        opener=Opener();client=ApiClient(key='secret123',opener=opener)
        result=client.get('fixtures/statistics',fixture=100,half='true')
        self.assertNotIn('secret123',json.dumps(result));self.assertNotIn('authorization',result['headers'])
        self.assertTrue(opener.url.startswith('https://v3.football.api-sports.io/'))
        with self.assertRaises(ValueError):client.get('https://evil.invalid')

    def test_http429_network_and_no_content_distinct(self):
        class Opener:
            def __init__(self,error):self.error=error
            def open(self,*a,**k):raise self.error
        response=ApiClient(key='secret',opener=Opener(HTTPError('url',429,'reason',{'Retry-After':'65'},None))).get('status')
        self.assertEqual(response['http_status'],429);self.assertEqual(response['headers']['retry-after'],'65')
        response=ApiClient(key='secret',opener=Opener(URLError('private details'))).get('status')
        self.assertEqual(response['transport'],'network_error');self.assertNotIn('private details',str(response))


if __name__=='__main__':
    unittest.main()
