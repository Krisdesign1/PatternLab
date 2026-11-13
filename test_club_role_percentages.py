"""Independent home/away denominators share the validated historical boundary."""
from contextlib import contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import club_trends as trends
import fixture_store
from test_club_trends import club_match
from test_competition_trends import NOW, START, END


class ClubRolePercentagesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'

    def store(self, rows):
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        return fixture_store.database_path(self.path)

    def build(self, **kwargs):
        return trends.build(START, END, now=NOW, path=self.path, **kwargs)

    def profile(self, report, league='39'):
        return next(row for row in report['rows'] if row['team_id'] == '100' and row['league'] == league)

    def test_full_role_counts_are_independent_of_main_role_and_recent_window(self):
        self.store([club_match(1, '1st'), club_match(2, 'Égal'), club_match(3, '2nd'),
                    club_match(4, 'Égal', away=True), club_match(5, '2nd', away=True)])
        expected = {
            'home_counts': {'1st': 1, 'Égal': 1, '2nd': 1}, 'home_matches': 3,
            'away_counts': {'1st': 0, 'Égal': 1, '2nd': 1}, 'away_matches': 2,
        }
        for role in ('all', 'home', 'away'):
            with self.subTest(role=role):
                club = self.profile(self.build(role=role, recent=1))
                for field, value in expected.items():
                    self.assertEqual(club[field], value)
                self.assertAlmostEqual(club['home_percentages']['1st'], 100 / 3)
                self.assertEqual(club['away_percentages'], {'1st': 0, 'Égal': 50, '2nd': 50})
                self.assertEqual(club['recent_home_results'], ['2nd'])
                self.assertEqual(club['recent_away_results'], ['2nd'])
                self.assertEqual(club['matches'], {'all': 5, 'home': 3, 'away': 2}[role])

    def test_zero_observed_frequency_is_distinct_from_missing_role_history(self):
        self.store([club_match(1, '2nd'), club_match(2, 'Égal', status='NS')])
        club = self.profile(self.build())
        self.assertEqual(club['home_percentages'], {'1st': 0, 'Égal': 0, '2nd': 100})
        self.assertEqual(club['away_counts'], {'1st': 0, 'Égal': 0, '2nd': 0})
        self.assertEqual(club['away_matches'], 0)
        self.assertEqual(club['away_percentages'], {'1st': None, 'Égal': None, '2nd': None})

    def test_opposite_main_role_preserves_profile_without_mixing_its_denominator(self):
        self.store([club_match(1, '1st'), club_match(2, '2nd', away=True, team=101)])
        for team, available, main, value in (('100', 'home', 'away', '1st'),
                                              ('101', 'away', 'home', '2nd')):
            with self.subTest(team=team, main_role=main):
                report = self.build(role=main, recent=1)
                club = next(row for row in report['rows'] if row['team_id'] == team and row['league'] == '39')
                self.assertEqual((club['matches'], club['observed'], club['excluded'], club['not_finished']), (0, 0, 0, 0))
                self.assertEqual(club['counts'], {'1st': 0, 'Égal': 0, '2nd': 0})
                self.assertEqual(club['percentages'], {'1st': None, 'Égal': None, '2nd': None})
                self.assertEqual(club['recent_results'], [])
                self.assertEqual(club['seasons'], [])
                self.assertIsNone(club['first_day'])
                self.assertIsNone(club['last_day'])
                self.assertEqual(club[available + '_matches'], 1)
                self.assertEqual(club[available + '_percentages'][value], 100)
                self.assertEqual(club['recent_' + available + '_results'], [value])
                self.assertEqual(club[main + '_matches'], 0)
                self.assertEqual(club['recent_' + main + '_results'], [])

    def test_invalid_main_role_metadata_does_not_rename_a_valid_other_role_profile(self):
        invalid = club_match(2, '1st', away=True, name='Nom incorrect')
        invalid['score']['halftime']['home'] = None
        self.store([club_match(1, '2nd', name='Nom confirmé'), invalid])
        club = self.profile(self.build(role='away'))
        self.assertEqual(club['club'], 'Nom confirmé')
        self.assertEqual((club['matches'], club['excluded']), (0, 1))
        self.assertEqual(club['home_matches'], 1)

    def test_excluded_missing_and_nonfinal_results_do_not_enter_either_role(self):
        missing = club_match(3, '1st', away=True)
        missing['score']['halftime']['home'] = None
        quarantined = club_match(4, 'Égal')
        quarantined['_patternlab_import'] = {'quarantined': True, 'reason': 'Identité à vérifier'}
        db = self.store([club_match(1, '2nd'), club_match(2, 'Égal', away=True),
                         missing, quarantined, club_match(5, '1st', away=True, status='NS'),
                         club_match(6, '1st')])
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE fixtures SET issue='Import invalidé' WHERE match_id='6'")
        club = self.profile(self.build())
        self.assertEqual(club['home_counts'], {'1st': 0, 'Égal': 0, '2nd': 1})
        self.assertEqual(club['away_counts'], {'1st': 0, 'Égal': 1, '2nd': 0})
        self.assertEqual(club['home_matches'] + club['away_matches'], club['matches'])

    def test_competitions_seasons_and_exact_kinshasa_cutoff_apply_to_both_roles(self):
        self.store([
            club_match(1, '1st', stamp='2026-11-05T22:59:59Z'),
            club_match(2, 'Égal', away=True, stamp='2026-11-05T23:00:00Z'),
            club_match(3, '2nd', away=True, stamp='2026-11-04T12:00:00Z'),
            club_match(4, 'Égal', season=2025),
            club_match(5, 'Égal', league=61),
        ])
        report = self.build(seasons=[2026], before_day=date(2026, 11, 6), role='home')
        club = self.profile(report)
        self.assertEqual(club['home_counts'], {'1st': 1, 'Égal': 0, '2nd': 0})
        self.assertEqual(club['away_counts'], {'1st': 0, 'Égal': 0, '2nd': 1})
        self.assertEqual(club['home_percentages']['1st'], 100)
        self.assertEqual(club['away_percentages']['2nd'], 100)
        self.assertEqual(self.profile(report, '61')['home_percentages']['Égal'], 100)

    def test_one_compact_read_only_snapshot_without_an_additional_role_query(self):
        db = self.store([club_match(1, '1st'), club_match(2, '2nd', away=True)])
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (db, self.path)}
        statements = []
        original = fixture_store._connection

        @contextmanager
        def tracked(*args, **kwargs):
            with original(*args, **kwargs) as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(fixture_store, '_connection', tracked):
            club = self.profile(self.build(role='away', recent=1))
        queries = [sql for sql in statements if ' FROM fixtures WHERE ' in sql]
        self.assertEqual(len(queries), 1)
        self.assertTrue(queries[0].startswith('SELECT match_id, kickoff_utc'))
        self.assertEqual((club['home_matches'], club['away_matches']), (1, 1))
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})


if __name__ == '__main__':
    unittest.main()
