"""Real SQLite club profiles: temporal bounds, identity and evidence coherence."""
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import club_trends as trends
import fixture_store
from test_competition_trends import raw, NOW, START, END


def club_match(mid, value='2nd', *, team=100, away=False, name='Club A', **kwargs):
    item = raw(mid, value, **kwargs)
    item['teams']['away' if away else 'home'] = {'id': team, 'name': name}
    return item


class ClubTrendsTests(unittest.TestCase):
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

    def profile(self, report, team='100', league='39'):
        return next(row for row in report['rows'] if row['team_id'] == team and row['league'] == league)

    def test_counts_recent_disjoint_samples_and_unique_match_totals(self):
        self.store([club_match(i, value) for i, value in enumerate(('1st', 'Égal', '2nd', '2nd', '2nd'), 1)])
        report = self.build(recent=2)
        club = self.profile(report)
        self.assertEqual(club['counts'], {'1st': 1, 'Égal': 1, '2nd': 3})
        self.assertEqual(club['percentages'], {'1st': 20, 'Égal': 20, '2nd': 60})
        self.assertEqual(club['recent_results'], ['2nd', '2nd'])
        self.assertEqual(club['previous_matches'], 3)
        self.assertAlmostEqual(club['delta_points']['2nd'], 100 - 100/3)
        self.assertEqual(report['totals']['matches'], 5)
        self.assertEqual(sum(row['matches'] for row in report['rows']), 10)
        self.assertEqual(report['totals']['clubs'], 6)

    def test_roles_change_denominator_recent_window_and_goals_perspective(self):
        self.store([club_match(1, '1st'), club_match(2, '2nd', away=True), club_match(3, 'Égal')])
        home, away = self.profile(self.build(role='home')), self.profile(self.build(role='away'))
        self.assertEqual(home['counts'], {'1st': 1, 'Égal': 1, '2nd': 0})
        self.assertEqual(away['counts'], {'1st': 0, 'Égal': 0, '2nd': 1})
        self.assertEqual(home['leaders'], ['1st', 'Égal'])
        self.assertEqual(home['goals_per_match']['goals_for_h1'], 1.5)
        self.assertEqual(away['goals_per_match']['goals_against_h2'], 1)
        self.assertTrue(all(value is None for value in home['delta_points'].values()))

    def test_id_renaming_homonyms_and_competitions_remain_distinct(self):
        self.store([club_match(1, name='Ancien nom'), club_match(2, name='Nouveau nom'),
                    club_match(3, team=101, name='Nouveau nom'), club_match(4, league=61)])
        report = self.build()
        self.assertEqual(self.profile(report)['matches'], 2)
        self.assertEqual(self.profile(report)['club'], 'Nouveau nom')
        self.assertEqual(self.profile(report, team='101')['matches'], 1)
        self.assertEqual(self.profile(report, league='61')['matches'], 1)
        self.assertNotEqual(self.profile(report)['profile_id'], self.profile(report, league='61')['profile_id'])

    def test_missing_scores_quarantine_and_nonfinal_are_not_zero_labels(self):
        missing = club_match(2)
        missing['score']['halftime']['home'] = None
        quarantine = club_match(3)
        quarantine['_patternlab_import'] = {'quarantined': True, 'reason': 'Identité à vérifier'}
        self.store([club_match(1), missing, quarantine, club_match(4, status='NS')])
        report = self.build()
        club = self.profile(report)
        self.assertEqual((club['matches'], club['excluded'], club['not_finished']), (1, 2, 1))
        self.assertEqual(club['percentages']['2nd'], 100)
        self.assertEqual(report['totals']['excluded'], 2)

    def test_projection_issue_and_changed_label_cannot_enter_counts(self):
        db = self.store([club_match(1), club_match(2), club_match(3)])
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE fixtures SET issue='Import invalidé' WHERE match_id='1'")
            payload = json.loads(connection.execute("SELECT normalized_json FROM fixtures WHERE match_id='2'").fetchone()[0])
            payload['value'] = '1st'
            connection.execute("UPDATE fixtures SET normalized_json=? WHERE match_id='2'", (json.dumps(payload),))
        club = self.profile(self.build())
        self.assertEqual((club['matches'], club['excluded']), (1, 2))

    def test_invalid_club_ids_are_unassigned_and_never_merged_by_name(self):
        bad = club_match(2)
        bad['teams']['home']['id'] = None
        same = club_match(3)
        same['teams']['away']['id'] = 100
        self.store([club_match(1), bad, same])
        report = self.build()
        self.assertEqual(report['unassigned_matches'], 2)
        self.assertEqual(self.profile(report)['matches'], 1)
        self.assertEqual(report['totals']['excluded'], 2)

    def test_excluded_recent_metadata_does_not_rename_a_valid_profile(self):
        bad = club_match(2, name='Libellé incorrect')
        bad['score']['halftime']['home'] = None
        self.store([club_match(1, name='Nom confirmé'), bad])
        self.assertEqual(self.profile(self.build())['club'], 'Nom confirmé')

    def test_date_inclusive_kinshasa_season_and_future_bounds(self):
        self.store([club_match(1, stamp='2026-10-01T22:59:59Z'),
                    club_match(2, stamp='2026-10-01T23:00:00Z'),
                    club_match(3, stamp='2026-10-02T23:00:00Z'),
                    club_match(4, season=2025),
                    club_match(5, stamp='2026-11-07T13:00:00Z')])
        report = trends.build(date(2026, 10, 2), date(2026, 10, 2), now=NOW, path=self.path)
        self.assertEqual(self.profile(report)['matches'], 1)
        self.assertEqual(self.profile(self.build(seasons=[2025]))['matches'], 1)
        self.assertEqual(self.profile(self.build())['matches'], 4)
        self.assertEqual(self.build(seasons=[])['rows'], [])

    def test_zero_zero_extra_time_and_penalties_use_regulation_scores_only(self):
        zero = club_match(1)
        zero['score']['halftime'] = {'home': 0, 'away': 0}
        zero['score']['fulltime'] = {'home': 0, 'away': 0}
        zero['goals'] = {'home': 0, 'away': 0}
        aet, pen = club_match(2, 'Égal', status='AET'), club_match(3, '1st', status='PEN')
        for match in (aet, pen):
            match['score']['extratime'] = {'home': 4, 'away': 3}
            match['score']['penalty'] = {'home': 6, 'away': 5}
            match['goals'] = {'home': 4, 'away': 3}
        self.store([zero, aet, pen])
        self.assertEqual(self.profile(self.build())['counts'], {'1st': 1, 'Égal': 2, '2nd': 0})

    def test_details_match_profile_filters_and_paginate_without_full_raw_or_api(self):
        self.store([club_match(1), club_match(2, away=True), club_match(3), club_match(4, league=61)])
        with patch.object(api, 'load_fixtures', side_effect=AssertionError('full raw')), \
             patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('API')), \
             patch.object(fixture_store, 'migrate_json', side_effect=AssertionError('migration')):
            report = self.build(role='home')
            detail = trends.details(100, 39, START, END, now=NOW, path=self.path, role='home', limit=1)
            self.assertEqual(detail['total'], self.profile(report)['observed'])
            self.assertEqual(detail['rows'][0]['match_id'], '3')
            second = trends.details('100', '39', START, END, now=NOW, path=self.path, role='home', limit=1, offset=1)
            self.assertEqual(second['rows'][0]['match_id'], '1')
            self.assertEqual(second['rows'][0]['goals_for_h2'], 1)

    def test_queries_do_not_modify_store_or_legacy_json(self):
        db = self.store([club_match(1)])
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (db, self.path)}
        self.build()
        trends.details(100, 39, START, END, now=NOW, path=self.path)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_invalid_controls_and_missing_database_fail_safely(self):
        self.store([club_match(1)])
        for kwargs in ({'recent': 0}, {'recent': True}, {'role': 'neutral'}, {'seasons': ['2026']}):
            with self.assertRaises(trends.TrendsError):
                self.build(**kwargs)
        with self.assertRaises(trends.TrendsError):
            trends.details('bad', 39, START, END, path=self.path, now=NOW)
        with self.assertRaises(trends.TrendsError):
            trends.build(START, END, path=self.path.parent / 'missing.json', now=NOW)


if __name__ == '__main__':
    unittest.main()
