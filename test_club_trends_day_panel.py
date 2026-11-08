"""Today's club-trend view uses a real bounded SQLite calendar, without API calls."""
from datetime import date
import csv
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
from test_competition_trends import raw


DAY = date(2026, 11, 6)


def day_fixture(mid, stamp, status='NS', *, league=39, home=99, away=98):
    row = raw(mid, '2nd', stamp=stamp, status=status, league=league)
    row['teams'] = {'home': {'id': home, 'name': 'Club témoin' if home == 99 else f'Club {home}'},
                    'away': {'id': away, 'name': 'Adversaire connu' if away == 98 else f'Club {away}'}}
    if status in ('NS', 'PST', 'CANC'):
        row['score'] = {key: {'home': None, 'away': None} for key in ('halftime', 'fulltime')}
        row['goals'] = {'home': None, 'away': None}
        row['fixture']['status']['elapsed'] = None
    elif status == '2H':
        row['fixture']['status']['elapsed'] = 61
        row['score']['fulltime'] = {'home': None, 'away': None}
        row['goals'] = {'home': 1, 'away': 0}
    return row


class ClubTrendsDayPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        history = []
        for mid, outcome in ((1, '1st'), (2, 'Égal'), (3, '2nd')):
            row = raw(mid, outcome)
            row['teams']['home'] = {'id': 99, 'name': 'Club témoin'}
            row['teams']['away'] = {'id': 98, 'name': 'Adversaire connu'}
            history.append(row)
        self.calendar = [
            day_fixture(1001, '2026-11-05T23:00:00Z'),
            day_fixture(1002, '2026-11-06T20:00:00Z', '2H', away=777),
            day_fixture(1003, '2026-11-06T21:00:00Z', 'FT'),
            day_fixture(1004, '2026-11-06T22:59:59Z', 'PST', league=61, home=555, away=556),
            day_fixture(1005, '2026-11-06T23:00:00Z', home=444, away=445),
            day_fixture(1006, '2026-11-05T22:59:59Z', 'FT', home=333, away=334),
        ]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': history + self.calendar,
                                        'season_imports': []}))
        fixture_store.migrate_json(self.path)
        for name in ('_catalogue', '_summary', '_details', '_day_calendar'):
            cached = getattr(panel, name, None)
            if cached is not None:
                cached.clear()
                self.addCleanup(cached.clear)
        self._patch(api, 'STORE_PATH', self.path)
        self.build = self._patch(trends, 'build', wraps=trends.build)
        self.network = self._patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API from trend controls'))
        self.full_raw = self._patch(api, 'load_fixtures', side_effect=AssertionError('No full raw load'))
        self.migrate = self._patch(fixture_store, 'migrate_json', side_effect=AssertionError('No screen migration'))
        self.at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        self.at.session_state['analysis_selected_day'] = DAY
        self.at.session_state['analysis_follow_today'] = False
        self.at.run()
        self.assertFalse(self.at.exception)
        self.at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        self.assertFalse(self.at.exception)

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def table(self):
        return next(frame.value for frame in self.at.dataframe
                    if 'Match' in frame.value.columns and 'Statut' in frame.value.columns)

    def test_full_selected_local_day_keeps_future_live_finished_and_postponed_matches(self):
        table = self.table()
        self.assertEqual(len(table), 4)
        self.assertEqual(list(table['Heure (Kinshasa)']), ['00:00', '21:00', '22:00', '23:59'])
        self.assertIn('À venir', set(table['Statut']))
        self.assertIn('Seconde mi-temps', set(table['Statut']))
        self.assertIn('Terminé', set(table['Statut']))
        self.assertIn('Reporté', set(table['Statut']))
        live = table[table['Statut'] == 'Seconde mi-temps'].iloc[0]
        final = table[table['Statut'] == 'Terminé'].iloc[0]
        self.assertEqual(live['Minute'], '61′')
        self.assertEqual(list(final['Verdict final']), ['2nd'])
        self.assertNotEqual(list(live['Verdict final']), ['2nd'])
        self.assertTrue(any('06/11/2026' in item.value for item in self.at.caption))
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_clubs_without_historical_profile_keep_their_match_and_missing_rates(self):
        table = self.table()
        unknown = table[table['Extérieur'] == 'Club 777'].iloc[0]
        history_columns = [name for name in table.columns if 'extérieur' in name.casefold()
                           and ('historique' in name.casefold() or '(%)' in name)]
        self.assertTrue(history_columns)
        percentage_columns = [name for name in history_columns if '(%)' in name]
        self.assertEqual(len(percentage_columns), 3)
        self.assertTrue(all(__import__('pandas').isna(unknown[name]) for name in percentage_columns))
        known = table[table['Extérieur'] == 'Adversaire connu'].iloc[0]
        self.assertAlmostEqual(sum(float(known[name]) for name in percentage_columns), 100, places=6)
        self.assertEqual(len(table), 4)

    def test_filters_use_the_calendar_and_do_not_recompute_historical_profiles(self):
        initial_calls = self.build.call_count
        self.at.text_input(key='club_trends_day_search').set_value('777').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.table()), 1)
        self.at.text_input(key='club_trends_day_search').set_value('').run()
        self.at.button_group(key='club_trends_day_status').set_value('À venir').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.table()), 1)
        self.at.button_group(key='club_trends_day_status').set_value('Tous').run()
        self.at.selectbox(key='club_trends_day_country').set_value('France').run()
        self.assertEqual(len(self.table()), 1)
        self.assertEqual(self.table().iloc[0]['Statut'], 'Reporté')
        self.at.selectbox(key='club_trends_day_competition').set_value('61').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.build.call_count, initial_calls)
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_reloading_local_calendar_reads_new_imports_without_api_or_history_rebuild(self):
        initial_calls = self.build.call_count
        fresh = day_fixture(1002, '2026-11-06T20:00:00Z', 'FT', away=777)
        fixture_store.upsert(fixture_store.database_path(self.path), [fresh])
        self.at.button(key='club_trends_day_reload').click().run()
        self.assertFalse(self.at.exception)
        table = self.table()
        self.assertEqual(sum(table['Statut'] == 'Terminé'), 2)
        self.assertEqual(self.build.call_count, initial_calls)
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_unconfirmed_competition_id_does_not_break_the_day_selector(self):
        incomplete = day_fixture(1007, '2026-11-06T19:00:00Z', home=800, away=801)
        incomplete['league']['id'] = '٣٩'
        fixture_store.upsert(fixture_store.database_path(self.path), [incomplete])
        self.at.button(key='club_trends_day_reload').click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Club 800' in value for value in self.table()['Match']))
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_pagination_keeps_every_match_in_the_export_with_precise_dates(self):
        extra = [day_fixture(1100+i, f'2026-11-06T19:{i:02}:00Z', home=500+i, away=600+i)
                 for i in range(11)]
        fixture_store.upsert(fixture_store.database_path(self.path), extra)
        self.at.button(key='club_trends_day_reload').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.table()), 15)
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self.at.selectbox(key='club_trends_day_limit').set_value(10).run()
            self.assertFalse(self.at.exception)
            self.assertEqual(len(self.table()), 10)
            self.at.selectbox(key='club_trends_day_page').set_value(2).run()
            self.assertFalse(self.at.exception)
            self.assertEqual(len(self.table()), 5)
            arguments = download.call_args.kwargs
        exported = list(csv.DictReader(StringIO(arguments['data'].decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(len(exported), 15)
        self.assertEqual({row['Date'] for row in exported}, {'06/11/2026'})
        self.assertEqual({row['Journée des matchs (Kinshasa)'] for row in exported}, {DAY.isoformat()})
        self.assertEqual(arguments['file_name'], 'matchs_tendances_clubs_2026-11-06.csv')
        self.assertTrue({'Domicile · 1st (%)', 'Extérieur · 2nd (%)', 'Verdict final'} <= exported[0].keys())
        self.at.text_input(key='club_trends_day_search').set_value('Club 777').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 1)
        self.assertEqual(len(self.table()), 1)
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_switching_global_day_updates_calendar_and_empty_day_has_clear_message(self):
        self.at.session_state['analysis_selected_day'] = date(2026, 11, 7)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.table()), 1)
        self.assertEqual(self.table().iloc[0]['Domicile'], 'Club 444')
        self.at.session_state['analysis_selected_day'] = date(2026, 11, 8)
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucun match' in item.value for item in self.at.info))
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
