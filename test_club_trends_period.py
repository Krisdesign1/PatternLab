"""Date-exact multi-day exports and independently traceable home/away recents."""
import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import club_trends as trends
import club_trends_day as days
import club_trends_panel as panel
from club_recent_sequences import role_columns
import fixture_store
from test_competition_trends import raw

START, END = date(2026, 10, 1), date(2026, 10, 31)
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def match(mid, value='2nd', **kwargs):
    row = raw(mid, value, **kwargs)
    row['teams'] = {'home': {'id': 99, 'name': 'Club A'}, 'away': {'id': 98, 'name': 'Club B'}}
    return row


class ClubTrendsPeriodTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        history = [match(i, value) for i, value in enumerate(('1st', '2nd', 'Égal', '1st', '2nd', 'Égal'), 1)]
        for row in history[1::2]:
            row['teams']['home'], row['teams']['away'] = row['teams']['away'], row['teams']['home']
        bad = match(7)
        bad['score']['halftime']['home'] = None
        foreign = match(8, '2nd', league=61)
        self.calendar = [match(101, stamp='2026-11-05T23:00:00Z', status='NS'),
                         match(102, '1st', stamp='2026-11-06T22:59:59Z'),
                         match(103, stamp='2026-11-06T23:00:00Z', status='NS'),
                         match(104, stamp='2026-11-07T23:00:00Z', status='CANC'),
                         match(105, stamp='2026-11-08T23:00:00Z', status='NS')]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': history + [bad, foreign] + self.calendar,
                                        'season_imports': []}))
        fixture_store.migrate_json(self.path)
        self.db = fixture_store.database_path(self.path)
        for name in ('_catalogue', '_summary', '_details', '_day_calendar', '_period_calendar'):
            cached = getattr(panel, name)
            cached.clear()
            self.addCleanup(cached.clear)
        self.patches = []
        self.mock(api, 'STORE_PATH', self.path)
        self.network = self.mock(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.mock(api, 'load_fixtures', side_effect=AssertionError('No full JSON'))
        self.mock(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))

    def mock(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def build(self, **kwargs):
        return trends.build(START, END, recent=2, now=NOW, path=self.path, **kwargs)

    def test_each_role_has_its_own_recent_window_dates_and_ids(self):
        report = self.build()
        club = next(row for row in report['rows'] if row['profile_id'] == '99:39')
        self.assertEqual(club['recent_results'], ['2nd', 'Égal'])
        self.assertEqual(club['recent_home_results'], ['Égal', '2nd'])
        self.assertEqual(club['recent_away_results'], ['1st', 'Égal'])
        self.assertEqual(club['recent_home_ids'], ['3', '5'])
        self.assertEqual(club['recent_away_ids'], ['4', '6'])
        self.assertEqual(club['matches'], 6)
        self.assertEqual(club['excluded'], 1)
        columns = role_columns(club, club)
        self.assertEqual(columns['Récents domicile (ancien → récent)'], 'Égal → 2nd')
        self.assertEqual(columns['Récents extérieur (ancien → récent)'], '1st → Égal')
        self.assertEqual(columns['Dates des récents domicile (Kinshasa)'], '03/10/2026 13:00 → 05/10/2026 13:00')
        self.assertFalse(any('Combinaison' in key for key in columns))

    def test_role_filter_does_not_blend_or_remove_the_other_role_sequence(self):
        home = next(row for row in self.build(role='home')['rows'] if row['profile_id'] == '99:39')
        away = next(row for row in self.build(role='away')['rows'] if row['profile_id'] == '99:39')
        self.assertEqual((home['matches'], away['matches']), (3, 3))
        self.assertEqual(home['recent_home_results'], away['recent_home_results'])
        self.assertEqual(home['recent_away_results'], away['recent_away_results'])
        foreign = next(row for row in self.build()['rows'] if row['profile_id'] == '99:61')
        self.assertEqual(foreign['recent_home_ids'], ['8'])
        self.assertEqual(foreign['recent_away_ids'], [])
        self.assertEqual(role_columns(None, None)['Récents domicile (ancien → récent)'], '')

    def test_period_query_includes_local_midnights_and_preserves_every_status(self):
        before = self.db.read_bytes()
        report = days.load_period(date(2026, 11, 6), date(2026, 11, 8), now=NOW, path=self.path)
        self.assertEqual([row['match_id'] for row in report['rows']], ['101', '102', '103', '104'])
        self.assertEqual([row['local_day'] for row in report['rows']], ['2026-11-06']*2 + ['2026-11-07', '2026-11-08'])
        self.assertEqual(report['totals']['other'], 1)
        self.assertEqual(len(report['days']), 3)
        empty = days.load_period(date(2026, 11, 10), date(2026, 11, 11), path=self.path)
        self.assertEqual([day['matches'] for day in empty['days']], [0, 0])
        self.assertEqual(self.db.read_bytes(), before)
        with self.assertRaises(trends.TrendsError):
            days.load_period(date(2026, 11, 8), date(2026, 11, 6), path=self.path)
        self.network.assert_not_called()

    def app(self, view):
        at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        at.session_state['analysis_selected_day'] = date(2026, 11, 6)
        at.session_state['analysis_follow_today'] = False
        at.run()
        if view != 'Tous les clubs':
            at.button_group(key='club_trends_view').set_value(view).run()
        self.assertFalse(at.exception)
        return at

    def test_multi_day_ui_and_export_preserve_filters_dates_all_pages_and_sequences(self):
        extra = [match(200+i, stamp=f'2026-11-07T18:{i:02}:00Z', status='NS') for i in range(11)]
        fixture_store.upsert(self.db, extra)
        at = self.app('Matchs de la journée')
        calls = self.mock(trends, 'build', wraps=trends.build)
        at.date_input(key='club_trends_period_end').set_value(date(2026, 11, 8))
        next(button for button in at.button if button.label == 'Afficher la période des matchs').click().run()
        self.assertFalse(at.exception)
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            at.selectbox(key='club_trends_day_limit').set_value(10).run()
            at.selectbox(key='club_trends_day_page').set_value(2).run()
            self.assertFalse(at.exception)
            arguments = download.call_args.kwargs
        self.assertTrue(callable(arguments['data']))
        rows = list(csv.DictReader(StringIO(arguments['data']().decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 15)
        self.assertEqual(len({row['ID du match API'] for row in rows}), 15)
        self.assertEqual({row['Journée des matchs (Kinshasa)'] for row in rows}, {'2026-11-06', '2026-11-07', '2026-11-08'})
        self.assertEqual({row['Première journée exportée'] for row in rows}, {'2026-11-06'})
        self.assertEqual(arguments['file_name'], 'matchs_tendances_clubs_2026-11-06_au_2026-11-08.csv')
        self.assertTrue(all(row['Récents domicile (ancien → récent)'] for row in rows))
        self.assertTrue(all(row['Récents extérieur (ancien → récent)'] for row in rows))
        at.button_group(key='club_trends_day_status').set_value('Autres').run()
        table = next(frame.value for frame in at.dataframe if 'Match' in frame.value.columns)
        self.assertEqual(list(table['Statut']), ['Annulé'])
        self.assertEqual(at.selectbox(key='club_trends_day_page').value, 1)
        self.assertEqual(at.date_input(key='club_trends_period_end').value, date(2026, 11, 8))
        calls.assert_not_called()
        self.network.assert_not_called()

    def test_all_clubs_csv_keeps_both_roles_for_the_chosen_history_period(self):
        at = self.app('Tous les clubs')
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            at.text_input(key='club_trends_search').set_value('Club A').run()
            self.assertFalse(at.exception)
            arguments = download.call_args.kwargs
        rows = list(csv.DictReader(StringIO(arguments['data']().decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('Récents extérieur (ancien → récent)' in row for row in rows))
        main = next(row for row in rows if row['ID compétition API'] == '39')
        self.assertTrue(main['Récents domicile (ancien → récent)'])
        self.assertTrue(main['Récents extérieur (ancien → récent)'])
        self.assertEqual(main['Fin incluse (Kinshasa)'], '2026-11-06')


if __name__ == '__main__':
    unittest.main()
