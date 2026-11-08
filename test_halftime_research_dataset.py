"""Source isolation, frozen-cohort and snapshot integrity tests."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from halftime_research_dataset import extract,write_snapshot,load_snapshot
from halftime_statistics_store import Store,encoded


def raw(mid=100):
    return dict(fixture=dict(id=mid,date='2024-09-01T12:00:00+00:00',status=dict(short='FT')),
        league=dict(id=39,season=2024),teams=dict(home=dict(id=10,name='H'),away=dict(id=20,name='A')),
        score=dict(halftime=dict(home=1,away=0),fulltime=dict(home=2,away=2)))


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'fixtures.sqlite3';db=sqlite3.connect(self.source)
        db.execute('CREATE TABLE fixtures(normalized_json TEXT,kickoff_utc TEXT,league TEXT)')
        db.commit();db.close()
        self.side=self.root/'half.sqlite3';self.store=Store(self.side)
        self.cutoff=datetime(2026,10,26,tzinfo=timezone.utc)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def manifest(self,fixture):
        envelope=dict(endpoint='fixtures',parameters=dict(league=39,season=2024,status='FT'),transport='received',http_status=200,
                      payload=dict(errors=[],response=[fixture]))
        self.store.db.execute('INSERT OR REPLACE INTO discoveries VALUES(?,?,?,?)',('39',2024,1,encoded(envelope)))
    def test_target_without_own_statistics_stays_in_universe(self):
        self.manifest(raw())
        result=extract(self.source,self.side,cutoff=self.cutoff,leagues=('39',))
        self.assertEqual(len(result['universe']),1)
        self.assertEqual(result['rows'][0]['value'],'2nd')
        self.assertEqual(result['statistics'],{})
        self.assertEqual(result['coverage']['2024']['missing_requests'],1)
    def test_future_fixture_excluded_by_actual_kickoff(self):
        value=raw();value['fixture']['date']='2026-10-27T12:00:00+00:00'
        self.manifest(value)
        result=extract(self.source,self.side,cutoff=self.cutoff,leagues=('39',))
        self.assertEqual(result['universe'],[])
    def test_pre2024_fixture_is_warmup_only_despite_season_label(self):
        value=raw();value['fixture']['date']='2023-12-20T12:00:00+00:00'
        self.manifest(value)
        result=extract(self.source,self.side,cutoff=self.cutoff,leagues=('39',))
        self.assertEqual(result['universe'],[])
        self.assertEqual(len(result['rows']),1)
    def test_wrong_endpoint_or_http_failure_is_not_discovery(self):
        self.manifest(raw())
        for key,value in (('endpoint','leagues'),('http_status',403)):
            payload=json.loads(self.store.db.execute('SELECT payload_json FROM discoveries').fetchone()[0])
            original=payload[key];payload[key]=value
            self.store.db.execute('UPDATE discoveries SET payload_json=?',(encoded(payload),))
            with self.assertRaises(ValueError):extract(self.source,self.side,cutoff=self.cutoff,leagues=('39',))
            payload[key]=original;self.store.db.execute('UPDATE discoveries SET payload_json=?',(encoded(payload),))
    def test_wrong_discovery_league_rejected(self):
        value=raw();value['league']['id']=40
        self.manifest(value)
        with self.assertRaises(ValueError):extract(self.source,self.side,cutoff=self.cutoff,leagues=('39','40'))
    def test_conflicting_local_scores_rejected(self):
        self.manifest(raw())
        row=dict(match_id='100',league='39',season=2024,timestamp='2024-09-01T12:00:00',
            home_id='10',away_id='20',home='H',away='A',ht='1-0',ft='5-0',value='2nd',provider='api-football')
        db=sqlite3.connect(self.source);db.execute('INSERT INTO fixtures VALUES(?,?,?)',(json.dumps(row),row['timestamp'],'39'));db.commit();db.close()
        with self.assertRaises(ValueError):extract(self.source,self.side,cutoff=self.cutoff,leagues=('39',))
    def test_read_only_source_and_immutable_snapshot(self):
        self.manifest(raw())
        before=self.source.read_bytes()
        result=extract(self.source,self.side,cutoff=self.cutoff,leagues=('39',))
        self.assertEqual(self.source.read_bytes(),before)
        directory=self.root/'snapshot';write_snapshot(directory,result)
        self.assertEqual(load_snapshot(directory)['universe'],result['universe'])
        with self.assertRaises(FileExistsError):write_snapshot(directory,result)
        with (directory/'rows.json.gz').open('ab') as handle:handle.write(b'corruption')
        with self.assertRaises(ValueError):load_snapshot(directory)


if __name__=='__main__':unittest.main()
