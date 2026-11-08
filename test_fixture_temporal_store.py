"""Historical readers must not silently substitute today's corrected scores."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import fixture_store as store
from fixture_temporal_store import history_as_of
from local_recovery import fingerprint
from test_api_football import fixture


class TemporalHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.network = patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API')).start()
        self.addCleanup(patch.stopall)

    def path(self,mode):
        path = self.root/mode/'fixtures.json'
        api.save_fixtures([],path)
        if mode=='sqlite':
            store.migrate_json(path)
        return path

    def at(self,stamp):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                return datetime.fromisoformat(stamp).astimezone(tz)
        return Clock

    def save(self,path,value,stamp):
        with patch.object(api,'datetime',self.at(stamp)),patch.object(store,'datetime',self.at(stamp)):
            api.save_fixtures([value],path)

    def test_old_scores_not_replaced_by_later_correction_json_and_sqlite(self):
        for mode in ('json','sqlite'):
            with self.subTest(mode=mode):
                path = self.path(mode)
                self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
                first,audit = api.load_matches(path,as_of='2026-10-26T00:00:00Z')
                changed = fixture();changed['score']['halftime']={'home':0,'away':0}
                self.save(path,changed,'2026-10-26T10:00:00+00:00')
                historical,after = api.load_matches(path,as_of='2026-10-26T00:00:00Z')
                self.assertEqual(first,historical)
                self.assertEqual(audit['source_fingerprint'],after['source_fingerprint'])
                current,_ = api.load_matches(path)
                latest,_ = api.load_matches(path,as_of='2026-10-27T00:00:00Z')
                self.assertNotEqual(first[0]['ht'],current[0]['ht'])
                self.assertEqual(latest,current)
                self.assertFalse(after['prospective_confirmation'])
                self.assertFalse(after['database_visibility_at_cutoff_proven'])

    def test_exact_cutoff_missing_tracking_and_naive_time_have_no_fallback(self):
        for mode in ('json','sqlite'):
            path = self.path(mode)
            self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
            rows,audit=api.load_matches(path,as_of='2026-10-25T10:00:00Z')
            self.assertEqual(rows,[])
            self.assertEqual(audit['temporal_exclusions'][0]['reason'],'not_observed_before_cutoff')
            with self.assertRaises(ValueError):
                api.load_matches(path,as_of='2026-10-25T10:00:01')
        legacy=self.root/'legacy.json'
        legacy.write_text(json.dumps(dict(version=1,fixtures=[fixture()],season_imports=[])))
        rows,audit=api.load_matches(legacy,as_of='2026-10-27T00:00:00Z',ids=['123'])
        self.assertEqual(rows,[])
        self.assertEqual(audit['untracked_inventory_count'],1)
        self.assertEqual(audit['temporal_exclusions'],[dict(match_id='123',reason='untracked_availability')])

    def test_selected_quarantine_and_later_review_do_not_repair_an_earlier_cutoff(self):
        for mode in ('json','sqlite'):
            path=self.path(mode)
            self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
            bad=fixture();bad['score']['halftime']={}
            self.save(path,bad,'2026-10-26T10:00:00+00:00')
            rows,audit=api.load_matches(path,as_of='2026-10-27T00:00:00Z')
            self.assertEqual(rows,[])
            self.assertIn('Quarantaine',audit['issues'][0]['reason'])
            self.save(path,fixture(),'2026-10-28T10:00:00+00:00')
            self.assertEqual(api.load_matches(path,as_of='2026-10-27T00:00:00Z')[0],[])
            self.assertEqual(len(api.load_matches(path,as_of='2026-10-29T00:00:00Z')[0]),1)

    def test_date_filters_apply_to_reconstructed_schedule_not_current_projection(self):
        path=self.path('sqlite')
        self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
        later=fixture();later['fixture']['date']='2026-09-12T21:00:00+02:00'
        self.save(path,later,'2026-10-26T10:00:00+00:00')
        rows,_=api.load_matches(path,as_of='2026-10-26T00:00:00Z',end='2025-09-01')
        self.assertEqual(len(rows),1)
        self.assertEqual(api.load_matches(path,end='2025-09-01')[0],[])

    def test_final_future_fixture_is_not_a_past_training_result(self):
        path=self.path('sqlite')
        value=fixture();value['fixture']['date']='2026-10-28T12:00:00Z'
        self.save(path,value,'2026-10-25T10:00:00+00:00')
        rows,audit=api.load_matches(path,as_of='2026-10-27T00:00:00Z')
        self.assertEqual(rows,[])
        self.assertTrue(audit['issues'])

    def test_final_imported_before_kickoff_stays_ineligible_even_at_a_later_read(self):
        path=self.path('sqlite')
        value=fixture();value['fixture']['date']='2026-10-25T12:00:00Z'
        self.save(path,value,'2026-10-25T10:00:00+00:00')
        rows,audit=api.load_matches(path,as_of='2026-10-26T00:00:00Z')
        self.assertEqual(rows,[])
        self.assertIn('avant son coup d’envoi',audit['issues'][0]['reason'])

    def test_offset_bounds_and_subsecond_boundaries_compare_real_instants(self):
        path=self.path('sqlite')
        value=fixture();value['fixture']['date']='2026-10-25T00:30:00Z'
        self.save(path,value,'2026-10-25T04:00:00+00:00')
        for bound in ('2026-10-25T01:00:00+02:00',datetime.fromisoformat('2026-10-25T01:00:00+02:00')):
            rows,_=api.load_matches(path,as_of='2026-10-26T00:00:00Z',start=bound)
            self.assertEqual(len(rows),1)
        self.assertEqual(len(api.load_matches(path,as_of='2026-10-26T00:00:00Z',start='2026-10-25T00:30:00Z')[0]),1)
        self.assertEqual(api.load_matches(path,as_of='2026-10-26T00:00:00Z',end='2026-10-25T00:30:00Z')[0],[])
        self.assertEqual(api.load_matches(path,as_of='2026-10-26T00:00:00Z',start='2026-10-25T00:30:00.000001Z')[0],[])
        with self.assertRaises(ValueError):
            api.load_matches(path,as_of='2026-10-26T00:00:00Z',start='2026-10-25T00:30:00')

    def test_non_object_event_does_not_crash_healthy_fixture_read(self):
        path=self.path('sqlite')
        self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
        self.save(path,fixture(456),'2026-10-25T10:00:00+00:00')
        with sqlite3.connect(store.database_path(path)) as connection:
            connection.execute('DROP TRIGGER fixture_import_no_update')
            connection.execute("UPDATE fixture_import_revisions SET payload='[1]' WHERE match_id='123'")
        rows,audit=api.load_matches(path,as_of='2026-10-26T00:00:00Z')
        self.assertEqual([row['match_id'] for row in rows],['456'])
        self.assertTrue(audit['temporal_exclusions'])

    def test_sql_index_tampering_is_localized_and_reading_writes_nothing(self):
        path=self.path('sqlite')
        self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
        self.save(path,fixture(456),'2026-10-25T10:00:00+00:00')
        database=store.database_path(path)
        with sqlite3.connect(database) as connection:
            connection.execute('DROP TRIGGER fixture_import_no_update')
            connection.execute("UPDATE fixture_import_revisions SET digest=? WHERE match_id='123'",('f'*64,))
        before=fingerprint(database)
        rows,audit=api.load_matches(path,as_of='2026-10-26T00:00:00Z')
        self.assertEqual([row['match_id'] for row in rows],['456'])
        self.assertEqual(audit['temporal_exclusions'][0]['reason'],'indexed_digest_mismatch')
        self.assertEqual(fingerprint(database),before)

    def test_missing_store_and_empty_id_filter_create_nothing(self):
        path=self.root/'absent/fixtures.json'
        self.assertEqual(api.load_matches(path,as_of='2026-10-27T00:00:00Z')[0],[])
        self.assertFalse(path.parent.exists())
        path=self.path('sqlite')
        self.save(path,fixture(),'2026-10-25T10:00:00+00:00')
        self.assertEqual(api.load_matches(path,as_of='2026-10-27T00:00:00Z',ids=[])[0],[])

    def test_new_sqlite_import_timestamp_is_taken_after_writer_lock(self):
        path=self.path('sqlite')
        original=store._connection
        times=['2026-10-25T10:00:00+00:00']
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):
                return datetime.fromisoformat(times[0]).astimezone(tz)
        @contextmanager
        def delayed(*args,**kwargs):
            with original(*args,**kwargs) as connection:
                if kwargs.get('write'):
                    times[0]='2026-10-25T10:05:00+00:00'
                yield connection
        with patch.object(store,'datetime',Clock),patch.object(store,'_connection',delayed):
            api.save_fixtures([fixture()],path)
        record=api.load_import_observations(path)[0]
        self.assertEqual(record['observed_at_utc'],'2026-10-25T10:05:00.000000+00:00')
        self.network.assert_not_called()


if __name__=='__main__':
    unittest.main()
