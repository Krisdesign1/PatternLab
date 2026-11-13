"""Before/after histories share one snapshot and strict local-day boundaries."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import club_trends as trends
import fixture_store
from test_club_trends import club_match

START, END = date(2026, 10, 1), date(2026, 11, 9)
DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


class ClubComparisonDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'fixtures.json'

    def store(self, rows):
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        return fixture_store.database_path(self.path)

    def comparison(self, **kwargs):
        return trends.build_comparison(START, END, comparison_start=DAY, now=NOW, path=self.path, **kwargs)

    def club(self, report, team='100', league='39'):
        return next(row for row in report['rows'] if row['team_id'] == team and row['league'] == league)

    def test_exact_midnights_and_recent_windows_change_only_by_selected_day(self):
        self.store([
            club_match(1, '1st'), club_match(2, 'Égal', away=True),
            club_match(3, '1st', stamp='2026-11-05T22:59:59Z'),
            club_match(4, '2nd', stamp='2026-11-05T23:00:00Z'),
            club_match(5, '2nd', away=True, stamp='2026-11-06T22:59:59Z'),
            club_match(6, 'Égal', stamp='2026-11-06T23:00:00Z'),
        ])
        report = self.comparison(recent=2)
        before, after = (self.club(report[key]) for key in ('before', 'after'))
        self.assertEqual((before['matches'], after['matches']), (3, 5))
        self.assertEqual(before['counts'], {'1st': 2, 'Égal': 1, '2nd': 0})
        self.assertEqual(after['counts'], {'1st': 2, 'Égal': 1, '2nd': 2})
        self.assertEqual(before['recent_results'], ['Égal', '1st'])
        self.assertEqual(after['recent_results'], ['2nd', '2nd'])
        self.assertEqual(before['recent_home_ids'], ['1', '3'])
        self.assertEqual(after['recent_home_ids'], ['3', '4'])
        self.assertEqual(before['recent_away_ids'], ['2'])
        self.assertEqual(after['recent_away_ids'], ['2', '5'])
        self.assertEqual((report['before_cutoff_utc'], report['after_cutoff_utc']),
                         ('2026-11-05T23:00:00', '2026-11-06T23:00:00'))
        for key, cutoff in (('before', DAY), ('after', date(2026, 11, 7))):
            reference = trends.build(START, END, recent=2, now=NOW, path=self.path, before_day=cutoff)
            self.assertEqual(report[key], reference)

    def test_multi_day_uses_shared_start_and_end_and_never_later_outcomes(self):
        self.store([
            club_match(1, '1st', stamp='2026-11-05T22:59:59Z'),
            club_match(2, 'Égal', stamp='2026-11-05T23:00:00Z'),
            club_match(3, '2nd', stamp='2026-11-07T12:00:00Z'),
            club_match(4, '2nd', stamp='2026-11-07T22:59:59Z'),
            club_match(5, '1st', stamp='2026-11-07T23:00:00Z'),
        ])
        report = self.comparison(comparison_end=date(2026, 11, 7))
        self.assertEqual((self.club(report['before'])['matches'], self.club(report['after'])['matches']), (1, 4))
        self.assertEqual(report['comparison_start'], '2026-11-06')
        self.assertEqual(report['comparison_end'], '2026-11-07')
        self.assertEqual(report['after_cutoff_utc'], '2026-11-07T23:00:00')

    def test_history_dates_seasons_main_role_and_opposite_roles_are_respected(self):
        self.store([
            club_match(1, '1st', stamp='2026-10-01T12:00:00Z'),
            club_match(2, 'Égal', stamp='2026-11-04T12:00:00Z', season=2025),
            club_match(3, '1st', stamp='2026-11-05T12:00:00Z'),
            club_match(4, '2nd', away=True, stamp='2026-11-06T12:00:00Z'),
            club_match(5, '2nd', stamp='2026-11-07T12:00:00Z'),
            club_match(6, 'Égal', league=61, stamp='2026-11-06T12:00:00Z'),
        ])
        report = trends.build_comparison(date(2026, 11, 1), DAY, comparison_start=DAY,
            comparison_end=date(2026, 11, 8), seasons=[2026], role='home', now=NOW, path=self.path)
        before, after = (self.club(report[key]) for key in ('before', 'after'))
        self.assertEqual((before['matches'], after['matches']), (1, 1))
        self.assertEqual((before['away_matches'], after['away_matches']), (0, 1))
        self.assertEqual(after['away_percentages']['2nd'], 100)
        self.assertEqual(after['home_percentages']['1st'], 100)
        self.assertEqual(self.club(report['after'], league='61')['home_percentages']['Égal'], 100)

    def test_missing_invalid_and_nonfinal_do_not_become_valid_after_results(self):
        missing = club_match(3, '1st', stamp='2026-11-06T12:00:00Z')
        missing['score']['halftime']['home'] = None
        quarantine = club_match(4, 'Égal', stamp='2026-11-06T12:00:00Z')
        quarantine['_patternlab_import'] = {'quarantined': True, 'reason': 'Identité à vérifier'}
        self.store([club_match(1, '1st'), club_match(2, '2nd', stamp='2026-11-06T11:00:00Z'),
                    missing, quarantine, club_match(5, '1st', status='NS', stamp='2026-11-06T13:00:00Z')])
        report = self.comparison()
        before, after = (self.club(report[key]) for key in ('before', 'after'))
        self.assertEqual((before['matches'], before['excluded'], before['not_finished']), (1, 0, 0))
        self.assertEqual((after['matches'], after['excluded'], after['not_finished']), (2, 2, 1))
        self.assertEqual(after['counts'], {'1st': 1, 'Égal': 0, '2nd': 1})
        self.assertEqual(report['after']['totals']['excluded'], 2)

    def test_now_bound_never_uses_future_even_if_database_contains_final_results(self):
        self.store([club_match(1, '1st', stamp='2026-11-06T10:00:00Z'),
                    club_match(2, '2nd', stamp='2026-11-06T13:00:00Z')])
        report = trends.build_comparison(START, END, comparison_start=DAY, path=self.path,
                                         now=datetime(2026, 11, 6, 12, tzinfo=timezone.utc))
        self.assertEqual(report['before']['rows'], [])
        self.assertEqual(self.club(report['after'])['matches'], 1)
        self.assertEqual(report['before']['as_of'], report['after']['as_of'])

    def test_one_read_only_snapshot_and_query_without_provider_or_full_raw_calls(self):
        db = self.store([club_match(1, '1st'), club_match(2, '2nd', stamp='2026-11-06T13:00:00Z')])
        old = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (db, self.path)}
        statements, connection_statements = [], []
        original = fixture_store._connection
        @contextmanager
        def tracked(*args, **kwargs):
            self.assertFalse(kwargs.get('write', False))
            self.assertFalse(kwargs.get('create', False))
            with original(*args, **kwargs) as connection:
                local_statements = []
                connection_statements.append(local_statements)
                def trace(sql):
                    local_statements.append(sql)
                    statements.append(sql)
                connection.set_trace_callback(trace)
                yield connection
        with patch.object(fixture_store, '_connection', tracked), \
             patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
             patch.object(api, 'load_fixtures', side_effect=AssertionError('No full JSON')):
            report = self.comparison()
        queries = [sql for sql in statements if ' FROM fixtures WHERE ' in sql]
        self.assertEqual(len(queries), 1)
        # Store activation has its own metadata-only validation connection;
        # both before/after aggregations must share the single fixture snapshot.
        fixture_snapshots = [items for items in connection_statements
                             if any(' FROM fixtures WHERE ' in sql for sql in items)]
        self.assertEqual(len(fixture_snapshots), 1)
        self.assertTrue(any(' FROM metadata' in sql for sql in fixture_snapshots[0]))
        self.assertTrue(queries[0].startswith('SELECT match_id, kickoff_utc'))
        self.assertNotIn('SELECT *', queries[0])
        self.assertEqual(report['revision'], report['before']['revision'])
        self.assertEqual(report['revision'], report['after']['revision'])
        self.assertEqual(report['store_uuid'], report['before']['store_uuid'])
        self.assertEqual(report['store_uuid'], report['after']['store_uuid'])
        report['after']['rows'][0]['counts']['1st'] = 999
        self.assertNotIn(999, report['before']['rows'][0]['counts'].values())
        self.assertEqual(old, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in old})

    def test_invalid_dates_roles_windows_and_reverse_comparison_raise_safe_errors(self):
        self.store([])
        for bad in ('2026-11-06', NOW, True):
            with self.subTest(date=bad), self.assertRaises(trends.TrendsError):
                trends.build_comparison(START, END, comparison_start=bad, now=NOW, path=self.path)
            with self.subTest(end=bad), self.assertRaises(trends.TrendsError):
                self.comparison(comparison_end=bad)
        for kwargs in ({'comparison_end':date(2026, 11, 5)}, {'role':'both'}, {'recent':0},
                       {'recent':True}, {'seasons':[True]}, {'comparison_end':date.max}):
            with self.subTest(kwargs=kwargs), self.assertRaises(trends.TrendsError):
                self.comparison(**kwargs)
        with self.assertRaises(trends.TrendsError):
            trends.build_comparison(END, START, comparison_start=DAY, now=NOW, path=self.path)


if __name__ == '__main__':
    unittest.main()
