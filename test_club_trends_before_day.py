"""Historical tests cannot count the tested day or later outcomes in a profile."""
import csv
from datetime import date, datetime, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import club_trends as trends
import club_trends_panel as panel
import fixture_store
from test_club_trends_period import match

DAY = date(2026, 11, 6)
START, END = date(2026, 10, 1), date(2026, 11, 8)
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


class ClubBeforeDayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        history = [match(i, value) for i, value in enumerate(('1st', '2nd', 'Égal', '1st', '2nd', 'Égal'), 1)]
        for row in history[1::2]:
            row['teams']['home'], row['teams']['away'] = row['teams']['away'], row['teams']['home']
        previous = match(7, '1st', stamp='2026-11-05T22:59:59Z')
        midnight = match(8, '2nd', stamp='2026-11-05T23:00:00Z')
        daytime = match(9, '2nd', stamp='2026-11-06T12:00:00Z')
        daytime['teams']['home'], daytime['teams']['away'] = daytime['teams']['away'], daytime['teams']['home']
        later = match(10, '2nd', stamp='2026-11-06T23:00:00Z')
        new_clubs = match(11, 'Égal', stamp='2026-11-06T15:00:00Z')
        new_clubs['teams'] = {'home': {'id': 777, 'name': 'New home'}, 'away': {'id': 778, 'name': 'New away'}}
        self.path.write_text(json.dumps({'version': 1, 'fixtures': history + [previous, midnight, daytime, later, new_clubs],
                                        'season_imports': []}))
        fixture_store.migrate_json(self.path)
        self.db = fixture_store.database_path(self.path)
        for name in ('_catalogue', '_summary', '_details', '_day_calendar', '_period_calendar'):
            cached = getattr(panel, name)
            cached.clear()
            self.addCleanup(cached.clear)
        self.mock(api, 'STORE_PATH', self.path)
        self.network = self.mock(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.mock(api, 'load_fixtures', side_effect=AssertionError('No full JSON'))
        self.mock(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))

    def mock(self, obj, name, *args, **kwargs):
        p = patch.object(obj, name, *args, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def report(self, **kwargs):
        return trends.build(START, END, now=NOW, path=self.path, recent=2, **kwargs)

    def profile(self, report):
        return next(row for row in report['rows'] if row['profile_id'] == '99:39')

    def app(self):
        at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        at.session_state['analysis_selected_day'] = DAY
        at.session_state['analysis_follow_today'] = False
        at.run()
        self.assertFalse(at.exception)
        return at

    def table(self, at, column):
        return next(frame.value for frame in at.dataframe if column in frame.value.columns)

    def download(self, at):
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            at.run()
            self.assertFalse(at.exception)
        args = download.call_args.kwargs
        return list(csv.DictReader(StringIO(args['data']().decode('utf-8-sig')), delimiter=';'))

    def test_midnight_cutoff_backfills_each_role_and_recalculates_all_denominators(self):
        normal = self.profile(self.report())
        report = self.report(before_day=DAY)
        club = self.profile(report)
        self.assertEqual(report['before_day'], DAY.isoformat())
        self.assertEqual((normal['matches'], club['matches']), (10, 7))
        self.assertEqual(club['counts'], {'1st': 3, 'Égal': 2, '2nd': 2})
        self.assertEqual(club['recent_results'], ['Égal', '1st'])
        self.assertEqual(club['recent_home_ids'], ['5', '7'])
        self.assertEqual(club['recent_away_ids'], ['4', '6'])
        self.assertEqual(club['recent_home_results'], ['2nd', '1st'])
        self.assertEqual(club['recent_away_results'], ['1st', 'Égal'])
        self.assertEqual((club['recent_matches'], club['previous_matches']), (2, 5))
        self.assertAlmostEqual(club['percentages']['1st'], 300 / 7)
        self.assertEqual(club['last_day'], '2026-11-05')
        self.assertFalse(any(row['team_id'] in ('777', '778') for row in report['rows']))

    def test_details_and_role_filtered_profiles_share_the_identical_cutoff(self):
        report = self.report(before_day=DAY, role='home')
        club = self.profile(report)
        detail = trends.details(99, 39, START, END, path=self.path, now=NOW, before_day=DAY, role='home')
        self.assertEqual(detail['before_day'], DAY.isoformat())
        self.assertEqual(detail['total'], club['observed'])
        self.assertEqual([row['match_id'] for row in detail['rows']], ['7', '5', '3', '1'])
        self.assertEqual(club['recent_home_ids'], ['5', '7'])
        self.assertEqual(club['recent_away_ids'], ['4', '6'])

    def test_empty_cutoff_now_and_season_bounds_are_not_relaxed(self):
        self.assertEqual(self.report(before_day=START)['rows'], [])
        self.assertEqual(self.report(before_day=DAY, seasons=[2025])['rows'], [])
        early_now = datetime(2026, 10, 2, 0, tzinfo=timezone.utc)
        report = trends.build(START, END, path=self.path, now=early_now, before_day=DAY)
        self.assertEqual(self.profile(report)['matches'], 1)
        for bad in ('2026-11-06', NOW, True):
            with self.subTest(bad=bad), self.assertRaises(trends.TrendsError):
                self.report(before_day=bad)

    def test_all_clubs_toggle_updates_counts_evidence_exports_and_can_be_disabled(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        at = self.app()
        at.text_input(key='club_trends_search').set_value('Club A').run()
        normal = self.table(at, 'Tendance dominante').iloc[0]['Matchs exploitables']
        at.toggle(key='club_trends_before_day').set_value(True).run()
        self.assertFalse(at.exception)
        filtered = self.table(at, 'Tendance dominante').iloc[0]
        self.assertEqual((normal, filtered['Matchs exploitables']), (9, 7))
        self.assertEqual(filtered['Dernier résultat'], '2026-11-05')
        at.toggle(key='club_trends_show_matches').set_value(True).run()
        detail = self.table(at, 'Compté dans la tendance')
        self.assertEqual(len(detail), 7)
        self.assertTrue(all(datetime.strptime(value, '%d/%m/%Y %H:%M').date() < DAY
                            for value in detail['Coup d’envoi (Kinshasa)']))
        exported = self.download(at)
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]['Historique strictement avant (Kinshasa)'], '2026-11-06')
        self.assertEqual(exported[0]['Coupure historique exclusive UTC'], '2026-11-05T23:00:00+00:00')
        self.assertEqual(exported[0]['Fin incluse (Kinshasa)'], '2026-11-05')
        self.assertEqual(exported[0]['Fin historique demandée incluse'], '2026-11-06')
        self.assertNotIn('8', exported[0]['ID matchs récents domicile'].split(' → '))
        at.toggle(key='club_trends_before_day').set_value(False).run()
        self.assertEqual(self.table(at, 'Tendance dominante').iloc[0]['Matchs exploitables'], normal)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.network.assert_not_called()

    def test_calendar_keeps_tested_scores_and_missing_history_with_cutoff_exports(self):
        at = self.app()
        at.toggle(key='club_trends_before_day').set_value(True).run()
        at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        self.assertTrue(at.toggle(key='club_trends_before_day').value)
        table = self.table(at, 'Match')
        self.assertEqual(set(table['ID du match API']), {'8', '9', '11'})
        known = table[table['ID du match API'] == '8'].iloc[0]
        self.assertEqual(known['Matchs historiques domicile'], 7)
        self.assertEqual(known['Verdict final'], ['2nd'])
        self.assertEqual(known['À 90 minutes'], '1–1')
        self.assertNotIn('8', known['ID matchs récents domicile'].split(' → '))
        new = table[table['ID du match API'] == '11'].iloc[0]
        self.assertEqual(new['Récents domicile (ancien → récent)'], '')
        exported = self.download(at)
        self.assertEqual(len(exported), 3)
        self.assertEqual({row['Historique strictement avant (Kinshasa)'] for row in exported}, {'2026-11-06'})
        self.assertEqual({row['Fin historique incluse'] for row in exported}, {'2026-11-05'})

    def test_period_first_day_drives_shared_cutoff_before_query_and_filtering_reuses_cache(self):
        at = self.app()
        at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        at.toggle(key='club_trends_before_day').set_value(True).run()
        at.date_input(key='club_trends_period_start').set_value(date(2026, 11, 5))
        at.date_input(key='club_trends_period_end').set_value(date(2026, 11, 7))
        next(b for b in at.button if b.label == 'Afficher la période des matchs').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(at.toggle(key='club_trends_before_day').value)
        table = self.table(at, 'Match')
        self.assertEqual(set(table['ID du match API']), {'7', '8', '9', '10', '11'})
        self.assertEqual(set(table[table['ID du match API'] != '11']['Matchs historiques domicile']), {6})
        exported = self.download(at)
        self.assertEqual({row['Historique strictement avant (Kinshasa)'] for row in exported}, {'2026-11-05'})
        self.assertEqual({row['Fin historique incluse'] for row in exported}, {'2026-11-04'})
        self.assertTrue(any('même coupure' in item.value for item in at.caption))
        build = self.mock(trends, 'build', wraps=trends.build)
        at.text_input(key='club_trends_day_search').set_value('Club A').run()
        at.selectbox(key='club_trends_day_limit').set_value(10).run()
        self.assertFalse(at.exception)
        build.assert_not_called()
        self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
