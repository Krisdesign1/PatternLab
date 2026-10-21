"""Explicit mixed-day registration is immutable and settles from JSON only."""
import copy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
import prediction_journal as journal
import prospective_models as models
from test_catboost_scenarios import sample,fixtures
from test_prediction_journal import raw_fixtures
from test_mixed_models import mixed_fixtures,mixed_history


NOW=datetime(2025,3,20,9,tzinfo=timezone.utc)
AFTER=datetime(2025,3,20,18,tzinfo=timezone.utc)


class MixedJournalTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'predictions.sqlite3'
        for patcher in (patch.object(ml,'load_report',return_value=None),
                        patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('Configuration absente.'))):
            patcher.start();self.addCleanup(patcher.stop)
        self.chosen=mixed_fixtures()
        self.methods=models.build_mixed_methods(mixed_history(),self.chosen)['methods']

    def record(self,**kwargs):
        options=dict(now=NOW,path=self.path,raw_fixtures=raw_fixtures(self.chosen))
        options.update(kwargs)
        return journal.record_mixed(self.chosen,self.methods,**options)

    def test_explicit_mixed_payload_and_metadata_locked_once_without_changing_single_default(self):
        metadata=dict(daily_policy='daily-analyses-v1',selection={'method':'fixed-first'},
                      competitions={'39':{'name':'PL','country':'England'},'61':{'name':'Ligue1','country':'France'}})
        first=self.record(metadata=metadata)
        self.assertTrue(first['created'])
        self.assertEqual(first['league'],'mixed')
        self.assertEqual(first['leagues'],['39','61'])
        self.assertEqual(first['group_kind'],'mixed')
        self.assertEqual(first['version'],journal.MIXED_VERSION)
        self.assertEqual(first['metadata'],metadata)
        with sqlite3.connect(self.path) as connection:
            original=connection.execute('SELECT payload,digest FROM predictions').fetchone()
        changed=copy.deepcopy(self.methods);changed['clubs']['generation']='later'
        again=journal.record_mixed(self.chosen,changed,now=AFTER,path=self.path)
        self.assertFalse(again['created']);self.assertEqual(again['methods'],first['methods'])
        self.assertEqual(journal.find_mixed(self.chosen,self.path)['id'],first['id'])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT payload,digest FROM predictions').fetchone(),original)
        with self.assertRaises(journal.JournalError):journal.record(self.chosen,self.methods,path=self.path,now=NOW)
        with self.assertRaises(journal.JournalError):journal.find(self.chosen,self.path)

    def test_settlement_corrections_use_frozen_block_product_without_network_or_inference(self):
        first=self.record()
        raw=raw_fixtures(self.chosen,status='FT')
        with patch.object(models,'build_mixed_methods',side_effect=AssertionError('No future inference')):
            report=journal.reconcile(raw,now=AFTER,path=self.path)
        self.assertEqual(report['settled'],1)
        evaluated=journal.list_entries(self.path)[0]
        self.assertEqual(evaluated['actual'],['Égal']*4)
        self.assertEqual(evaluated['methods'],first['methods'])
        for name,payload in self.methods.items():
            self.assertAlmostEqual(evaluated['evaluations'][name]['probability'],models.probability_for_sequence(payload,['Égal']*4))
        raw[0]['score']['halftime']={'home':0,'away':0}
        journal.reconcile(raw,now=AFTER,path=self.path)
        corrected=journal.list_entries(self.path)[0]
        self.assertEqual(corrected['actual'][0],'2nd')
        self.assertEqual(corrected['methods'],first['methods'])
        self.assertEqual(len(corrected['result_history']),2)

    def test_raw_status_ids_league_clubs_and_timestamp_are_checked_before_write(self):
        for issue in ('live','league','home','away','timestamp','missing'):
            raw=raw_fixtures(self.chosen)
            if issue=='live':raw[0]['fixture']['status']['short']='1H'
            elif issue=='league':raw[0]['league']['id']=140
            elif issue=='home':raw[0]['teams']['home']['id']=99999
            elif issue=='away':raw[0]['teams']['away']['id']=99999
            elif issue=='timestamp':raw[0]['fixture']['date']='2025-03-20T18:00:00Z'
            else:raw.pop()
            with self.subTest(issue=issue),self.assertRaises(journal.JournalError):self.record(raw_fixtures=raw)
            self.assertFalse(self.path.exists())

    def test_common_cutoff_and_each_fixture_provenance_cannot_be_forged(self):
        for issue in ('cutoff','indices','fixture','leagues'):
            changed=copy.deepcopy(self.methods)
            payload=changed['clubs']
            if issue=='cutoff':payload['distribution']['components'][1]['method']['training_before']='2025-03-21'
            elif issue=='indices':payload['distribution']['components'][0]['league']='61'
            elif issue=='fixture':payload['fixtures'][0]['home_id']='999'
            else:payload['leagues']=['39','61','140']
            with self.subTest(issue=issue),self.assertRaises(ValueError):
                journal.record_mixed(self.chosen,changed,now=NOW,path=self.path,raw_fixtures=raw_fixtures(self.chosen))
            self.assertFalse(self.path.exists())

    def test_only_whole_local_today_groups_and_future_kickoffs_can_be_registered(self):
        for now in (NOW-timedelta(days=1),NOW+timedelta(days=1),AFTER):
            with self.subTest(now=now),self.assertRaises(journal.JournalError):self.record(now=now)
            self.assertFalse(self.path.exists())
        chosen=copy.deepcopy(self.chosen)
        chosen[-1]['timestamp']='2025-03-20T23:00:00'
        methods=models.build_mixed_methods(mixed_history(),chosen)['methods']
        with self.assertRaises(journal.JournalError):
            journal.record_mixed(chosen,methods,now=NOW,path=self.path,raw_fixtures=raw_fixtures(chosen))
        self.assertFalse(self.path.exists())

    def test_live_clock_checked_at_insert_and_commit_rolls_back_when_it_crosses_kickoff(self):
        for stamps in ((NOW,AFTER),(NOW,NOW,AFTER)):
            clock=iter(stamps)
            with self.subTest(stamps=stamps),self.assertRaises(journal.JournalError):
                self.record(now=None,clock=lambda:next(clock))
            self.assertEqual(journal.list_entries(self.path),[])
        with self.assertRaises(journal.JournalError):self.record(clock=lambda:NOW)

    def test_mono_daily_group_reuses_exact_legacy_id_digest_and_forecast(self):
        chosen=fixtures()
        old_methods=models.build_methods(sample(),chosen)['methods']
        first=journal.record(chosen,old_methods,now=NOW,path=self.path,raw_fixtures=raw_fixtures(chosen))
        self.assertEqual(first['version'],journal.VERSION)
        self.assertNotIn('group_kind',first)
        old_id=journal._group_key(journal._fixtures(chosen))
        with sqlite3.connect(self.path) as connection:
            original=connection.execute('SELECT payload,digest FROM predictions').fetchone()
        new_methods=models.build_mixed_methods(sample(),chosen)['methods']
        again=journal.record_mixed(chosen,new_methods,now=NOW,path=self.path,raw_fixtures=raw_fixtures(chosen))
        self.assertEqual(again['id'],old_id);self.assertFalse(again['created'])
        self.assertEqual(again['methods'],first['methods'])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT payload,digest FROM predictions').fetchone(),original)
        self.assertEqual(len(journal.list_entries(self.path)),1)


if __name__=='__main__':unittest.main()
