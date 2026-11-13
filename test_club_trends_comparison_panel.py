"""Before/after filtering keeps the same pre-day population and complete exports."""
import csv
from datetime import date, datetime, timedelta, timezone
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
from test_competition_trends import raw
from test_club_trends_day_panel import day_fixture

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


class Clock(datetime):
    tick = 0

    @classmethod
    def now(cls, tz=None):
        cls.tick += 1
        stamp = NOW + timedelta(microseconds=cls.tick)
        return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)


def history(mid, outcome, home, away):
    row = raw(mid, outcome)
    row['teams'] = {'home': {'id': home, 'name': f'Club {home}'},
                    'away': {'id': away, 'name': f'Club {away}'}}
    return row


class ClubComparisonPanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'fixtures.json'
        # Club 99 starts at 2/3 second-half results, then drops to 2/4.
        old = [history(i, value, 99, 98) for i, value in enumerate(('1st', '2nd', '2nd'), 1)]
        # Club 77 starts at 1/3, then reaches 4/6 after today's three results.
        old += [history(i, value, 77, 78) for i, value in enumerate(('1st', '1st', '2nd'), 4)]
        final = day_fixture(1001, '2026-11-06T15:00:00Z', 'FT')
        final['score'] = {'halftime': {'home': 2, 'away': 0}, 'fulltime': {'home': 2, 'away': 1}}
        final['goals'] = {'home': 2, 'away': 1}
        calendar = [final]
        calendar += [day_fixture(1010+i, f'2026-11-06T16:{i:02}:00Z') for i in range(11)]
        calendar += [day_fixture(1100+i, f'2026-11-06T19:{i:02}:00Z', 'FT', home=77, away=78)
                     for i in range(3)]
        calendar += [day_fixture(1200, '2026-11-06T20:00:00Z', 'FT', home=777, away=778)]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': old + calendar, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        self.db = fixture_store.database_path(self.path)
        self.digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for name in ('_catalogue', '_summary', '_details', '_day_calendar', '_period_calendar'):
            cached = getattr(panel, name)
            cached.clear()
            self.addCleanup(cached.clear)
        self.mock(api, 'STORE_PATH', self.path)
        Clock.tick = 0
        self.mock(panel, 'datetime', Clock)
        self.build = self.mock(trends, 'build_comparison', wraps=trends.build_comparison)
        self.network = self.mock(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.mock(api, 'load_fixtures', side_effect=AssertionError('No raw JSON screen load'))
        self.mock(fixture_store, 'migrate_json', side_effect=AssertionError('No screen migration'))
        self.at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        self.at.session_state['analysis_selected_day'] = DAY
        self.at.session_state['analysis_follow_today'] = False
        self.at.session_state['club_trends_compare'] = True
        self.at.run()
        self.assertFalse(self.at.exception)

    def mock(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def day_view(self):
        self.at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        self.assertFalse(self.at.exception)

    def table(self, day=True):
        key = 'ID du match API' if day else 'ID club API'
        return next(frame.value for frame in self.at.dataframe if key in frame.value.columns)

    def filter_before_second(self):
        self.at.multiselect(key='club_trends_pct_selected').set_value(['home:2nd']).run()
        self.at.number_input(key='club_trends_pct_home_2nd_value').set_value(66.7).run()
        self.assertFalse(self.at.exception)

    def export(self):
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self.at.run()
            self.assertFalse(self.at.exception)
        args = download.call_args.kwargs
        content = args['data']() if callable(args['data']) else args['data']
        return args, list(csv.DictReader(StringIO(content.decode('utf-8-sig')), delimiter=';'))

    def assert_read_only(self):
        self.network.assert_not_called()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.digest)

    def test_before_threshold_keeps_losing_members_and_rejects_after_only_entrants(self):
        self.day_view()
        self.filter_before_second()
        rows = self.table()
        self.assertEqual(set(rows['ID du match API']), {'1001'} | {str(1010+i) for i in range(11)})
        self.assertTrue(all(abs(value - 200/3) < 1e-8 for value in rows['Domicile · 2nd avant (%)']))
        self.assertEqual(set(rows['Domicile · 2nd après (%)']), {50})
        self.assertEqual(set(rows['Domicile · 2nd écart (points)']), {-16.7})
        self.assertEqual(rows[rows['ID du match API'] == '1001'].iloc[0]['Verdict final'], ['1st'])
        for _, row in rows.iterrows():
            self.assertEqual(list(row['Tendance domicile avant']), ['2nd'])
            self.assertEqual(list(row['Tendance domicile après']), ['1st', '2nd'])
            self.assertEqual(list(row['Tendance extérieur avant']), ['2nd'])
            self.assertEqual(list(row['Tendance extérieur après']), ['1st', '2nd'])
        self.assertEqual(list(rows.columns[:8]), ['Match', 'Date', 'Heure (Kinshasa)', 'Verdict final',
            'Tendance domicile avant', 'Tendance domicile après', 'Tendance extérieur avant', 'Tendance extérieur après'])
        self.assertTrue(any('Après provisoire' in item.value for item in self.at.caption))
        self.assertEqual(self.build.call_count, 1)
        self.assert_read_only()

    def test_after_visibility_preserves_page_ids_and_exports_every_selected_match(self):
        self.day_view()
        self.filter_before_second()
        self.at.selectbox(key='club_trends_day_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_day_page').set_value(2).run()
        ids = list(self.table()['ID du match API'])
        self.assertEqual(len(ids), 2)
        self.at.toggle(key='club_trends_compare_show_after').set_value(False).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 2)
        self.assertEqual(list(self.table()['ID du match API']), ids)
        self.assertFalse(any('après' in key or 'écart (points)' in key for key in self.table().columns))
        args, exported = self.export()
        self.assertEqual(len(exported), 12)
        self.assertTrue(args['file_name'].startswith('avant_apres_'))
        self.assertEqual({row['Filtres et sélection appliqués à'] for row in exported}, {'Avant uniquement'})
        self.assertEqual({row['Mode historique'] for row in exported}, {'Comparaison avant / après'})
        self.assertEqual({row['Première journée comparée'] for row in exported}, {DAY.isoformat()})
        self.assertEqual({row['Dernière journée comparée incluse'] for row in exported}, {DAY.isoformat()})
        utc = lambda value: datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        self.assertEqual({utc(row['Coupure Avant exclusive UTC']) for row in exported},
                         {datetime(2026, 11, 5, 23, tzinfo=timezone.utc)})
        self.assertEqual({utc(row['Coupure Après exclusive UTC']) for row in exported},
                         {datetime(2026, 11, 6, 23, tzinfo=timezone.utc)})
        self.assertEqual({float(row['Domicile · 2nd après (%)']) for row in exported}, {50})
        self.assertEqual({row['Tendance domicile avant'] for row in exported}, {'2nd'})
        self.assertEqual({row['Tendance domicile après'] for row in exported}, {'1st / 2nd'})
        self.assertEqual({row['Tendance extérieur avant'] for row in exported}, {'2nd'})
        self.assertEqual({row['Tendance extérieur après'] for row in exported}, {'1st / 2nd'})
        self.at.toggle(key='club_trends_compare_show_after').set_value(True).run()
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 2)
        self.assertEqual(list(self.table()['ID du match API']), ids)
        self.assertIn('Domicile · 2nd après (%)', self.table().columns)
        self.assertEqual(self.build.call_count, 1)
        self.assert_read_only()

    def test_new_club_keeps_missing_before_values_and_is_visible_without_percentage_filter(self):
        self.day_view()
        rows = self.table()
        self.assertEqual(len(rows), 16)
        new = rows[rows['ID du match API'] == '1200'].iloc[0]
        self.assertTrue(__import__('pandas').isna(new['Matchs historiques domicile avant']))
        self.assertTrue(__import__('pandas').isna(new['Domicile · 2nd avant (%)']))
        self.assertTrue(__import__('pandas').isna(new['Domicile · 2nd écart (points)']))
        self.assertEqual(new['Domicile · 2nd après (%)'], 100)
        self.assertEqual(new['Matchs historiques domicile après'], 1)
        self.assertEqual(list(new['Tendance domicile avant']), [])
        self.assertEqual(list(new['Tendance domicile après']), ['2nd'])
        entrant = rows[rows['ID du match API'] == '1100'].iloc[0]
        self.assertAlmostEqual(entrant['Domicile · 2nd avant (%)'], 100/3)
        self.assertAlmostEqual(entrant['Domicile · 2nd après (%)'], 200/3)
        self.assertEqual(list(entrant['Tendance domicile avant']), ['1st'])
        self.assertEqual(list(entrant['Tendance domicile après']), ['2nd'])
        self.filter_before_second()
        self.assertFalse({'1100', '1101', '1102', '1200'} & set(self.table()['ID du match API']))
        self.assert_read_only()

    def test_all_clubs_and_its_export_also_filter_before_and_keep_after_when_hidden(self):
        self.filter_before_second()
        rows = self.table(day=False)
        self.assertEqual(list(rows['ID club API']), ['99'])
        self.assertAlmostEqual(rows.iloc[0]['Domicile · 2nd avant (%)'], 200/3)
        self.assertEqual(rows.iloc[0]['Domicile · 2nd après (%)'], 50)
        self.assertEqual(list(rows.iloc[0]['Tendance domicile avant']), ['2nd'])
        self.assertEqual(list(rows.iloc[0]['Tendance domicile après']), ['1st', '2nd'])
        self.assertEqual(list(rows.iloc[0]['Tendance extérieur avant']), [])
        self.assertEqual(list(rows.iloc[0]['Tendance extérieur après']), [])
        self.assertNotIn('Verdict final', rows.columns)
        self.assertEqual(list(rows.columns[:7]), ['Club', 'Pays', 'Compétition',
            'Tendance domicile avant', 'Tendance domicile après', 'Tendance extérieur avant', 'Tendance extérieur après'])
        self.at.toggle(key='club_trends_compare_show_after').set_value(False).run()
        self.assertEqual(list(self.table(False)['ID club API']), ['99'])
        self.assertNotIn('Domicile · 2nd après (%)', self.table(False).columns)
        _, exported = self.export()
        self.assertEqual(len(exported), 1)
        self.assertEqual(float(exported[0]['Domicile · 2nd après (%)']), 50)
        self.assertEqual(exported[0]['Filtres et sélection appliqués à'], 'Avant uniquement')
        self.assertEqual(exported[0]['Récents domicile avant (ancien → récent)'], '1st → 2nd → 2nd')
        self.assertEqual(exported[0]['Récents domicile après (ancien → récent)'], '1st → 2nd → 2nd → 1st')
        self.assertEqual(exported[0]['Tendance domicile avant'], '2nd')
        self.assertEqual(exported[0]['Tendance domicile après'], '1st / 2nd')
        self.assertEqual(self.build.call_count, 1)
        self.assert_read_only()

    def test_snapshot_changes_only_after_explicit_refresh_or_form_apply(self):
        self.day_view()
        self.filter_before_second()
        fresh = day_fixture(1010, '2026-11-06T16:00:00Z', 'FT')
        fixture_store.upsert(self.db, [fresh])
        # Importing another result cannot change the open comparison on a display rerun.
        self.at.toggle(key='club_trends_compare_show_after').set_value(False).run()
        self.at.toggle(key='club_trends_compare_show_after').set_value(True).run()
        self.assertEqual(set(self.table()['Domicile · 2nd après (%)']), {50})
        self.assertEqual(self.build.call_count, 1)
        self.at.button(key='club_trends_comparison_refresh').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.build.call_count, 2)
        self.assertEqual(set(self.table()['Domicile · 2nd après (%)']), {60})
        self.assertEqual(set(self.table()['ID du match API']), {'1001'} | {str(1010+i) for i in range(11)})
        self.at.selectbox(key='club_trends_recent').set_value(5)
        next(b for b in self.at.button if b.label == 'Afficher et actualiser').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.build.call_count, 3)
        self.at.button(key='club_trends_day_reload').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.build.call_count, 4)
        self.assertEqual(set(self.table()['Domicile · 2nd après (%)']), {60})
        self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
