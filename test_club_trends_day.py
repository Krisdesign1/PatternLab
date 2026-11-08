from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import club_trends_day as day_trends
import fixture_store
from test_competition_trends import raw

DAY=date(2026,11,7)
NOW=datetime(2026,11,7,10,tzinfo=timezone.utc)


class ClubDayTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'fixtures.json'

    def store(self,rows):
        self.path.write_text(json.dumps(dict(version=1,fixtures=rows,season_imports=[])))
        fixture_store.migrate_json(self.path)
        return fixture_store.database_path(self.path)

    def load(self):
        return day_trends.load(DAY,path=self.path,now=NOW)

    def test_full_local_day_keeps_future_kickoffs_and_excludes_both_neighbor_days(self):
        self.store([raw(1,stamp='2026-11-06T22:59:59Z'),raw(2,stamp='2026-11-06T23:00:00Z'),
                    raw(3,stamp='2026-11-07T22:59:59Z',status='NS'),raw(4,stamp='2026-11-07T23:00:00Z')])
        report=self.load()
        self.assertEqual([row['match_id'] for row in report['rows']],['2','3'])
        self.assertEqual(report['rows'][0]['kickoff_local'],'2026-11-07T00:00:00+01:00')
        self.assertEqual(report['rows'][1]['final_verdict'],'En attente')
        self.assertEqual(report['totals']['upcoming'],1)
        self.assertTrue(all(row['local_day']=='2026-11-07' for row in report['rows']))

    def test_states_scores_and_regulation_result_follow_calendar_contract(self):
        rows=[raw(1,'1st',stamp='2026-11-07T08:00:00Z'),
              raw(2,'Égal',stamp='2026-11-07T08:30:00Z'),
              raw(3,'2nd',stamp='2026-11-07T09:00:00Z'),
              raw(4,stamp='2026-11-07T10:00:00Z',status='2H'),
              raw(5,stamp='2026-11-07T11:00:00Z',status='PST'),
              raw(6,stamp='2026-11-07T12:00:00Z',status='CANC')]
        rows[3]['fixture']['status'].update(elapsed=63,extra=1)
        self.store(rows)
        found=self.load()['rows']
        self.assertEqual([r['final_verdict'] for r in found],['1st','Égal','2nd','En cours','Non évaluable','Non évaluable'])
        self.assertEqual(found[3]['minute'],'63+1′')
        self.assertEqual(found[3]['regulation_score'],'—')
        self.assertEqual([r['status_label'] for r in found[-2:]],['Reporté','Annulé'])

    def test_missing_halftime_quarantine_and_overtime_never_manufacture_final_label(self):
        missing=raw(1,stamp='2026-11-07T08:00:00Z');missing['score']['halftime']['home']=None
        held=raw(2,stamp='2026-11-07T08:30:00Z');held['_patternlab_import']={'quarantined':True,'reason':'identity_changed'}
        aet=raw(3,'1st',stamp='2026-11-07T09:00:00Z',status='AET');aet['goals']={'home':5,'away':4}
        self.store([missing,held,aet]);found=self.load()['rows']
        self.assertEqual(found[0]['final_verdict'],'Non évaluable')
        self.assertEqual(found[1]['final_verdict'],'À vérifier')
        self.assertEqual(found[2]['final_verdict'],'1st')
        self.assertEqual(found[2]['score'],'5–4')
        self.assertEqual(found[2]['regulation_score'],'2–1')

    def test_ids_are_strict_and_unknown_team_remains_visible(self):
        unknown=raw(1,stamp='2026-11-07T08:00:00Z',status='NS');unknown['teams']['home']={'id':None,'name':'TBD'}
        unicode=raw(2,stamp='2026-11-07T09:00:00Z');unicode['teams']['home']['id']='٩٩'
        self.store([unknown,unicode]);found=self.load()['rows']
        self.assertEqual(len(found),2)
        self.assertIsNone(found[0]['home_id']);self.assertFalse(found[0]['teams_confirmed'])
        self.assertIsNone(found[1]['home_id']);self.assertEqual(found[1]['final_verdict'],'À vérifier')
        enriched=day_trends.enrich(found,[dict(team_id='99',league='39',matches=10,club='Same')])
        self.assertIsNone(enriched[1]['home_profile'])

    def test_day_query_uses_compact_rows_no_raw_loader_no_stats_no_provider_or_migration(self):
        item=raw(1,stamp='2026-11-07T08:00:00Z');item['events']=[{'detail':'X'*100000}]
        self.store([item])
        original=json.loads
        def loads(value,*args,**kwargs):
            self.assertNotIn('X'*1000,value)
            return original(value,*args,**kwargs)
        with patch.object(api,'load_fixtures',side_effect=AssertionError('No full raw loader')), \
             patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API')), \
             patch.object(fixture_store,'migrate_json',side_effect=AssertionError('No migration')), \
             patch.object(fixture_store,'_statistics',side_effect=AssertionError('No whole-store stats')), \
             patch.object(day_trends.json,'loads',side_effect=loads):
            self.assertEqual(self.load()['totals']['matches'],1)

    def test_refresh_marker_and_revision_share_store_snapshot(self):
        db=self.store([raw(1,stamp='2026-11-07T08:00:00Z')])
        fixture_store.mark_calendar_refresh(db,DAY.isoformat(),'Africa/Kinshasa','2026-11-07T09:55:00Z')
        found=self.load()
        self.assertEqual(found['calendar_updated_at'],'2026-11-07T09:55:00+00:00')
        self.assertEqual(found['revision'],fixture_store.signature(db)[1])
        self.assertEqual(found['store_uuid'],fixture_store.signature(db)[2])

    def test_join_never_uses_names_or_other_competitions_and_keeps_no_history_rows(self):
        fixtures=[dict(match_id='1',league='39',home_id='99',away_id='100',home='renamed',away='Same'),
                  dict(match_id='2',league='61',home_id='99',away_id='101',home='renamed',away='Same'),
                  dict(match_id='3',league='61',home_id='102',away_id=None,home='Same',away='unknown')]
        profiles=[dict(team_id='99',league='39',matches=10,club='Old name'),
                  dict(team_id='100',league='39',matches=0,club='Same'),
                  dict(team_id='103',league='61',matches=9,club='Same')]
        original=deepcopy((fixtures,profiles));joined=day_trends.enrich(fixtures,profiles)
        self.assertEqual(len(joined),3);self.assertTrue(joined[0]['home_has_history'])
        self.assertFalse(joined[0]['away_has_history']);self.assertIsNone(joined[1]['home_profile'])
        self.assertIsNone(joined[2]['home_profile']);self.assertIsNone(joined[2]['away_profile'])
        joined[0]['home_profile']['matches']=200
        self.assertEqual((fixtures,profiles),original)
        with self.assertRaises(day_trends.TrendsError):day_trends.enrich(fixtures,profiles+[profiles[0]])

    def test_invalid_dates_naive_clock_and_absent_structured_store_are_explicit(self):
        for day in ('2026-11-07',datetime(2026,11,7)):
            with self.assertRaises(day_trends.TrendsError):day_trends.load(day,path=self.path,now=NOW)
        with self.assertRaises(day_trends.TrendsError):day_trends.load(DAY,path=self.path,now=datetime(2026,11,7))
        with self.assertRaises(day_trends.TrendsError):self.load()


if __name__=='__main__':unittest.main()
