"""Differential checks against the frozen worker's canonical fixture reads."""
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import automation_worker as original
import fixture_store
from test_api_football import fixture
import worker_fixture_io as io


NOW = datetime(2026,11,6,12,tzinfo=timezone.utc)


def raw(mid, *, stamp='2026-11-05T20:00:00Z', league=61, status='FT'):
    result = fixture(mid, status=status, season=2026)
    result['fixture']['date'] = stamp
    result['league']['id'] = league
    return result


class WorkerFixtureIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'fixtures.json'

    def seed(self, values, *, sqlite=True):
        api.save_fixtures(values, self.path)
        if sqlite:
            fixture_store.migrate_json(self.path)

    def test_streamed_sqlite_and_legacy_json_have_identical_filters_and_order(self):
        values = [raw(30),raw(10,league=39),raw(20,status='NS'),
                  raw(40,stamp='2026-11-06T00:30:00+01:00')]
        self.seed(values,sqlite=False)
        filters = ({}, {'leagues':['61']}, {'ids':['40','10','40']},
                   {'statuses':['FT']}, {'start':'2026-11-05T23:00:00','end':'2026-11-06'})
        expected = [(selected,api.load_fixtures(self.path,**selected)) for selected in filters]
        for selected,wanted in expected:
            self.assertEqual(list(io.iter_fixtures(self.path,**selected)),wanted)
        fixture_store.migrate_json(self.path)
        for selected,_ in expected:
            self.assertEqual(list(io.iter_fixtures(self.path,**selected)),api.load_fixtures(self.path,**selected))

    def test_inventory_metadata_errors_cutoffs_and_normalized_order_match_original(self):
        values = [raw(1,league=61),raw(2,league=39),raw(3,stamp='2026-11-06T00:30:00+01:00'),
                  raw(4,stamp='2026-11-06T00:00:00Z'),raw(5,status='NS')]
        values[-1]['league'].update(name='Latest league name',country='Latest country')
        invalid = raw(6);invalid['score']['halftime']=None
        values.append(invalid)
        self.seed(values)
        expected = original._inventory(api.load_fixtures(self.path),NOW)
        actual = io.inventory(self.path,NOW)
        self.assertEqual(actual,expected)
        self.assertEqual(actual[0]['61']['competition'],'Ligue 1')
        self.assertEqual([row['match_id'] for row in actual[1]['61']],['1','3'])
        self.assertEqual(actual[2]['61'],1)
        scoped = io.inventory(self.path,NOW,keep_leagues={'39'})
        self.assertEqual(scoped[0],expected[0]);self.assertEqual(scoped[2],expected[2])
        self.assertEqual(dict(scoped[1]),{'39':expected[1]['39']})
        metadata_only = io.inventory(self.path,NOW,include_history=False)
        self.assertEqual(metadata_only[0],expected[0]);self.assertEqual(metadata_only[2],expected[2])
        self.assertEqual(dict(metadata_only[1]),{})

    def test_sqlite_stream_does_not_materialize_raw_fixture_list(self):
        self.seed([raw(mid) for mid in range(1,501)])
        with patch.object(api,'load_fixtures',side_effect=AssertionError('Full raw population')), \
                patch.object(io.json,'loads',wraps=io.json.loads) as decode:
            stream = io.iter_fixtures(self.path)
            first = next(stream)
            self.assertEqual(first['fixture']['id'],1)
            self.assertEqual(decode.call_count,1)
            stream.close()

    def test_quarantined_identity_and_incomplete_today_keep_global_issue_counts(self):
        values=[raw(1),raw(2,stamp='2026-11-06T10:00:00Z')]
        self.seed(values)
        changed=raw(1);changed['teams']['home']['id']=999
        incomplete=raw(2,stamp='2026-11-06T10:00:00Z');incomplete['score']['halftime']=None
        api.save_fixtures([changed,incomplete],self.path)
        canonical=api.load_fixtures(self.path)
        self.assertTrue(all(value.get('_patternlab_import',{}).get('quarantined') for value in canonical))
        expected=original._inventory(canonical,NOW)
        self.assertEqual(io.inventory(self.path,NOW),expected)
        scoped=io.inventory(self.path,NOW,keep_leagues=())
        self.assertEqual(scoped[0],expected[0]);self.assertEqual(scoped[2],expected[2])
        self.assertEqual(expected[2]['61'],2)
        self.assertFalse(scoped[1])

    def test_reader_uses_one_mvcc_snapshot_and_closes_it(self):
        self.seed([raw(1),raw(2)])
        stream = io.iter_fixtures(self.path)
        first = next(stream)
        updated = raw(2);updated['league']['name']='New metadata'
        api.save_fixtures([updated],self.path)
        remainder = list(stream)
        self.assertEqual(first['fixture']['id'],1)
        self.assertEqual(remainder[0]['league']['name'],'Ligue 1')
        self.assertEqual(next(io.iter_fixtures(self.path,ids=['2']))['league']['name'],'New metadata')

    def test_more_than_one_sql_id_chunk_keeps_global_ordinal_order_and_deduplicates(self):
        self.seed([raw(mid) for mid in range(1,701)])
        identities=[str(mid) for mid in range(700,0,-1)]+['1','700']
        expected=api.load_fixtures(self.path,ids=identities)
        self.assertEqual(list(io.iter_fixtures(self.path,ids=identities)),expected)

    def test_day_scope_keeps_rescheduled_plan_ids_and_original_plan(self):
        day = date(2026,11,6)
        values = [raw(1,stamp='2026-11-06T13:00:00Z',status='NS'),
                  raw(2,stamp='2026-11-07T13:00:00Z',status='NS'),
                  raw(3,stamp='2026-11-05T23:30:00Z',status='NS'),
                  raw(4,stamp='2026-11-06T14:00:00Z',status='NS'),raw(5)]
        self.seed(values)
        plans = {day.isoformat()+'/61/2':[['1','2']]}
        original_plans = deepcopy(plans)
        expected = original._plan(api.load_fixtures(self.path),[day.isoformat()],[2],original_plans)
        reduced = io.forecast_fixtures(self.path,day,[2],plans)
        actual = original._plan(reduced,[day.isoformat()],[2],plans)
        self.assertEqual(actual[0],expected[0])
        self.assertEqual(plans,original_plans)
        needed = {mid for group in expected[0] for mid in group['ids']}
        self.assertEqual({mid:actual[1].get(mid) for mid in needed},
                         {mid:expected[1].get(mid) for mid in needed})
        self.assertEqual({row['fixture']['id'] for row in reduced},{1,2,3,4})

    def test_broken_database_fails_closed_with_the_api_error_type(self):
        self.seed([raw(1)])
        with patch.object(fixture_store,'_meta',side_effect=fixture_store.FixtureStoreError('Broken store')):
            with self.assertRaises(api.ApiFootballError):
                list(io.iter_fixtures(self.path))

    def test_forecast_calendar_and_history_share_one_snapshot_and_skip_invalid_league(self):
        day=NOW.date()
        values=[raw(1),raw(2,stamp='2026-11-06T15:00:00Z',status='NS')]
        self.seed(values)
        expected=original._inventory(api.load_fixtures(self.path),NOW)
        stream=io._iter_connection
        changed=[]
        def during(connection,filters):
            yield from stream(connection,filters)
            if 'start' in filters:
                update=raw(1);update['league']['name']='Changed after calendar read'
                api.save_fixtures([update],self.path)
                changed.append(True)
        with patch.object(io,'_iter_connection',side_effect=during):
            raw_day, day_info, info, rows, issues=io.forecast_inventory(self.path,day,[2],{},NOW)
        self.assertEqual(changed,[True])
        self.assertEqual((info,rows,issues),expected)
        self.assertEqual([item['fixture']['id'] for item in raw_day],[2])
        self.assertEqual(set(day_info),{'61'})
        self.assertEqual(api.load_fixtures(self.path,ids=['1'])[0]['league']['name'],
                         'Changed after calendar read')


if __name__ == '__main__':
    unittest.main()
