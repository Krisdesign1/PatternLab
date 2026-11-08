"""Independent read-only tests for the lightweight collection monitor."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from halftime_statistics_store import Store


NOW=datetime(2026,10,26,22,tzinfo=timezone.utc)


def task(mid,*,year=2024,league='39'):
    return dict(match_id=str(mid),league=league,season=year,
                kickoff_utc=f'{year}-09-01T12:00:00',home_id='10',away_id='20')


class CollectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.path=self.root/'data/api_football/halftime_statistics.sqlite3'
        self.store=None

    def tearDown(self):
        if self.store is not None:self.store.close()
        self.temporary.cleanup()

    def create(self,rows):
        self.store=Store(self.path);self.store.enqueue(rows)

    def read(self,now=NOW):
        from halftime_collection_status import load_status
        return load_status(root=self.root,now=now)

    def classify(self,mid,availability,*,state=None):
        self.store.db.execute('UPDATE tasks SET availability=?,state=? WHERE match_id=?',
            (availability,state or availability,str(mid)))

    def activity(self,mid,*,finished_at,started_at=None):
        self.store.db.execute('''INSERT INTO attempts(match_id,source,started_at,finished_at,classification)
            VALUES(?,?,?,?,?)''',(str(mid),'network',started_at if started_at is not None else finished_at-.4,
                                 finished_at,'ready'))

    def test_missing_database_is_not_created(self):
        result=self.read()
        self.assertFalse(result['exists'])
        self.assertEqual(result['status'],'not_started')
        self.assertFalse(self.path.parent.exists())

    def test_completed_compact_view_keeps_counts_without_scanning_attempts_or_research(self):
        import halftime_collection_status as status
        self.create([task(100),task(101)])
        self.classify(100,'ready');self.classify(101,'partial')
        original=status.sqlite3.connect
        queries=[]
        def traced(*args,**kwargs):
            connection=original(*args,**kwargs)
            connection.set_trace_callback(queries.append)
            return connection
        with patch.object(status.sqlite3,'connect',side_effect=traced), \
                patch.object(status,'_comparison',side_effect=AssertionError('research scan')):
            result=status.load_status(root=self.root,now=NOW,compact=True)
        self.assertEqual((result['status'],result['total'],result['resolved'],result['ready'],result['partial']),
                         ('complete',2,2,1,1))
        self.assertFalse(any('attempts' in query.lower() for query in queries))
        self.assertIsNone(result['eta_seconds'])

    def test_active_compact_view_preserves_activity_pause_and_eta(self):
        import halftime_collection_status as status
        self.create([task(100),task(101)])
        self.classify(100,'ready')
        self.activity(100,finished_at=NOW.timestamp()-2)
        self.store.set_meta('provider_pause_until',NOW.timestamp()+100)
        full=self.read()
        compact=status.load_status(root=self.root,now=NOW,compact=True)
        self.assertEqual(compact,full)

    def test_no_raw_payload_or_api_client_is_needed(self):
        self.create([task(100)])
        self.activity(100,finished_at=NOW.timestamp()-2)
        changes=self.store.db.total_changes
        result=self.read()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['remaining'],1)
        self.assertEqual(self.store.db.total_changes,changes)
        self.assertFalse(result.get('eta_seconds'))

    def test_partial_and_empty_count_as_resolved_but_not_ready(self):
        self.create([task(i) for i in range(100,105)])
        for mid,kind in ((100,'ready'),(101,'partial'),(102,'empty'),(103,'invalid')):
            self.classify(mid,kind)
        result=self.read()
        self.assertEqual((result['total'],result['resolved'],result['remaining']),(5,4,1))
        self.assertEqual(result['ready'],1)
        self.assertEqual(result['partial'],1)
        self.assertEqual(result['empty'],1)
        self.assertEqual(result['invalid'],1)

    def test_retry_empty_is_resolved_but_network_failure_is_not(self):
        self.create([task(100),task(101)])
        self.classify(100,'empty',state='retry')
        self.classify(101,'transport_error',state='retry')
        result=self.read()
        self.assertEqual(result['resolved'],1)
        self.assertEqual(result['remaining'],1)
        self.assertEqual(result['retry_errors'],1)

    def test_collection_scope_excludes_other_leagues_and_later_dates(self):
        future=task(103);future['kickoff_utc']='2026-10-26T01:00:00'
        self.create([task(100),task(101,league='667'),task(102,year=2023),future])
        result=self.read()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['per_year'][0]['year'],'2024')

    def test_provider_pause_suppresses_finish_estimate(self):
        self.create([task(i) for i in range(100,106)])
        for i in range(100,104):
            self.classify(i,'ready');self.activity(i,finished_at=NOW.timestamp()-5+(i-100))
        self.store.set_meta('provider_pause_until',NOW.timestamp()+3600)
        result=self.read()
        self.assertEqual(result['status'],'paused')
        self.assertIsNone(result['eta_seconds'])
        self.assertIsNone(result['estimated_finish_utc'])

    def test_stale_activity_does_not_claim_active_or_show_eta(self):
        self.create([task(i) for i in range(100,106)])
        for i in range(100,104):
            self.classify(i,'ready');self.activity(i,finished_at=NOW.timestamp()-3600+(i-100))
        result=self.read()
        self.assertEqual(result['status'],'inactive')
        self.assertIsNone(result['eta_seconds'])

    def test_progress_rate_reflects_actual_inter_call_gaps(self):
        self.create([task(i) for i in range(100,240)])
        for n,i in enumerate(range(100,200)):
            self.classify(i,'ready');self.activity(i,finished_at=NOW.timestamp()-100+n)
        result=self.read()
        self.assertEqual(result['status'],'collecting')
        self.assertGreater(result['rate_per_hour'],3000)
        self.assertLess(result['rate_per_hour'],4000)
        self.assertGreater(result['eta_seconds'],30)
        self.assertLess(result['eta_seconds'],50)

    def test_shared_daily_allowance_insufficient_suppresses_eta(self):
        self.create([task(i) for i in range(100,240)])
        for n,i in enumerate(range(100,200)):
            self.classify(i,'ready');self.activity(i,finished_at=NOW.timestamp()-100+n)
        self.store.set_meta('last_provider_headers',{'x-ratelimit-requests-remaining':'10'})
        result=self.read()
        self.assertTrue(result['eta_limited_by_quota'])
        self.assertIsNone(result['eta_seconds'])
        self.assertIsNone(result['estimated_finish_utc'])

    def test_duplicate_retries_do_not_inflate_throughput(self):
        self.create([task(100),task(101)])
        self.classify(100,'ready')
        for n in range(100):self.activity(100,finished_at=NOW.timestamp()-100+n)
        result=self.read()
        self.assertIsNone(result['rate_per_hour'])
        self.assertIsNone(result['eta_seconds'])

    def test_complete_collection_does_not_claim_model_trained(self):
        self.create([task(100),task(101)])
        self.classify(100,'ready');self.classify(101,'empty')
        result=self.read()
        self.assertEqual(result['remaining'],0)
        self.assertEqual(result['status'],'complete')
        self.assertNotEqual(result['comparison']['stage'],'results_available')

    def test_complete_live_collection_with_old_snapshot_requests_new_snapshot(self):
        self.create([task(100)]);self.classify(100,'ready')
        path=self.root/'data/research/halftime_enrichment/v2';path.mkdir(parents=True)
        (path/'readiness.json').write_text(json.dumps({'ready':False,'examples':{'fit':100,'validation':100,'historical':100}}))
        (path/'plan.json').write_text(json.dumps({'created_at_utc':'2026-10-26T20:00:00+00:00'}))
        result=self.read()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['comparison']['stage'],'snapshot_waiting')
        self.assertEqual(result['comparison']['prepared_examples'],300)

    def test_inflight_request_counts_as_recent_activity_not_completion(self):
        self.create([task(100)])
        self.store.reserve('100',source='network',now=NOW-timedelta(seconds=5))
        result=self.read()
        self.assertEqual(result['status'],'collecting')
        self.assertEqual(result['remaining'],1)

    def test_malformed_research_summaries_do_not_break_collection_status(self):
        self.create([task(100)]);self.classify(100,'ready')
        path=self.root/'data/research/halftime_enrichment/bad';path.mkdir(parents=True)
        for examples in (None,[],1,'bad'):
            with self.subTest(examples=examples):
                (path/'readiness.json').write_text(json.dumps({'ready':True,'examples':examples}))
                result=self.read()
                self.assertEqual(result['status'],'complete')
                self.assertEqual(result['comparison']['stage'],'not_prepared')
        (path/'readiness.json').write_text(json.dumps({'ready':True,'examples':{'fit':100}}))
        (path/'plan.json').write_text('[]')
        result=self.read()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['comparison']['stage'],'collection_ready')
        self.assertIsNone(result['comparison']['snapshot_created_utc'])

    def test_corrupt_database_returns_safe_unavailable_status(self):
        self.path.parent.mkdir(parents=True);self.path.write_text('not a database private-details')
        result=self.read()
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('private-details',str(result))

    def test_reads_committed_snapshot_while_collector_has_short_write_transaction(self):
        self.create([task(100)])
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            self.classify(100,'ready')
            result=self.read()
            self.assertEqual(result['remaining'],1)
            self.assertNotEqual(result['status'],'unavailable')
        finally:self.store.db.rollback()


if __name__=='__main__':unittest.main()
