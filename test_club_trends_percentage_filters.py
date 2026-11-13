"""Role-specific percentage search must agree with tables and complete exports."""
import csv
from datetime import date
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


class PercentageLogicTests(unittest.TestCase):
    def test_all_six_values_are_independent_and_filters_use_and(self):
        target = {'home_percentages': {'1st': 10, 'Égal': 20, '2nd': 70},
                  'away_percentages': {'1st': 40, 'Égal': 50, '2nd': 10}}
        criteria = tuple((side, result, value, value) for side in ('home', 'away')
                         for result, value in target[side + '_percentages'].items())
        self.assertEqual(panel._filter_percentages([target], criteria), [target])
        for index in range(6):
            wrong = list(criteria)
            side, result, lower, upper = wrong[index]
            wrong[index] = side, result, lower + 1, upper + 1
            self.assertEqual(panel._filter_percentages([target], wrong), [])

    def test_one_decimal_exact_and_inclusive_bounds_distinguish_zero_from_missing(self):
        rows = [{'home_percentages': {'2nd': value}} for value in (200/3, 66.64, 0, None)]
        self.assertEqual(panel._filter_percentages(rows, [('home', '2nd', 66.7, 66.7)]), rows[:1])
        self.assertEqual(panel._filter_percentages(rows, [('home', '2nd', 66.6, 66.7)]), rows[:2])
        self.assertEqual(panel._filter_percentages(rows, [('home', '2nd', 0, 0)]), rows[2:3])
        self.assertEqual(panel._filter_percentages(rows, ()), rows)
        fixtures = [{'home_profile': rows[0], 'away_profile': None}]
        self.assertEqual(panel._filter_percentages(fixtures, [('home', '2nd', 66.7, 100)], fixtures=True), fixtures)
        self.assertEqual(panel._filter_percentages(fixtures, [('away', '2nd', 0, 100)], fixtures=True), [])


class PercentagePanelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'fixtures.json'
        history = []
        for mid, result in enumerate(('1st', '2nd', '2nd', '1st', 'Égal'), 1):
            row = raw(mid, result)
            home, away = (99, 98) if mid < 4 else (98, 99)
            row['teams'] = {'home': {'id': home, 'name': f'Club {home}'},
                            'away': {'id': away, 'name': f'Club {away}'}}
            history.append(row)
        other = raw(6, '1st', league=61)
        other['teams'] = {'home': {'id': 99, 'name': 'Club 99'}, 'away': {'id': 98, 'name': 'Club 98'}}
        calendar = [day_fixture(1001, '2026-11-06T16:00:00Z'),
                    day_fixture(1002, '2026-11-06T17:00:00Z', home=98, away=99),
                    day_fixture(1003, '2026-11-06T18:00:00Z', away=777),
                    day_fixture(1004, '2026-11-06T19:00:00Z', home=778)]
        calendar += [day_fixture(1010+i, f'2026-11-06T20:{i:02}:00Z') for i in range(11)]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': history + [other] + calendar,
                                        'season_imports': []}))
        fixture_store.migrate_json(self.path)
        self.db = fixture_store.database_path(self.path)
        self.digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for name in ('_catalogue', '_summary', '_details', '_day_calendar', '_period_calendar'):
            cached = getattr(panel, name)
            cached.clear()
            self.addCleanup(cached.clear)
        self.mock(api, 'STORE_PATH', self.path)
        self.build = self.mock(trends, 'build', wraps=trends.build)
        self.network = self.mock(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.mock(api, 'load_fixtures', side_effect=AssertionError('No full JSON'))
        self.mock(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))
        self.at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        self.at.session_state['analysis_selected_day'] = DAY
        self.at.session_state['analysis_follow_today'] = False
        self.at.run()
        self.assertFalse(self.at.exception)

    def mock(self, obj, name, *args, **kwargs):
        p = patch.object(obj, name, *args, **kwargs)
        mocked = p.start()
        self.addCleanup(p.stop)
        return mocked

    def table(self, day=False):
        column = 'ID du match API' if day else 'Tendance dominante'
        return next(frame.value for frame in self.at.dataframe if column in frame.value.columns)

    def download(self):
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self.at.run()
            self.assertFalse(self.at.exception)
        content = download.call_args.kwargs['data']()
        return list(csv.DictReader(StringIO(content.decode('utf-8-sig')), delimiter=';'))

    def assert_read_only(self):
        self.network.assert_not_called()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.digest)

    def test_all_clubs_exact_filters_export_and_role_setting_remain_consistent(self):
        target = self.table()
        target = target[(target['ID club API'] == '99') & (target['ID compétition API'] == '39')].iloc[0]
        self.assertEqual((target['Matchs exploitables'], target['Matchs historiques domicile'],
                          target['Matchs historiques extérieur']), (5, 3, 2))
        self.assertAlmostEqual(target['Domicile · 2nd (%)'], 200/3)
        self.at.multiselect(key='club_trends_pct_selected').set_value(['home:2nd', 'away:Égal']).run()
        self.at.number_input(key='club_trends_pct_home_2nd_value').set_value(66.7).run()
        self.assertFalse(self.at.exception)
        target = self.table()
        self.assertEqual(list(target['ID club API']), ['99'])
        self.assertEqual(list(target['ID compétition API']), ['39'])
        exported = self.download()
        self.assertEqual(len(exported), 1)
        self.assertEqual(float(exported[0]['Extérieur · Égal (%)']), 50)
        self.assertIn('Domicile · 2nd : 66.7 % ET Extérieur · Égal : 50.0 %',
                      exported[0]['Filtres de pourcentages par rôle'])
        self.assertEqual(self.build.call_count, 1)
        self.at.selectbox(key='club_trends_role').set_value('away')
        next(b for b in self.at.button if b.label == 'Afficher et actualiser').click().run()
        self.assertFalse(self.at.exception)
        target = self.table().iloc[0]
        self.assertEqual(target['Matchs exploitables'], 2)
        self.assertEqual(target['Matchs historiques domicile'], 3)
        self.assertAlmostEqual(target['Domicile · 2nd (%)'], 200/3)
        self.assert_read_only()

    def test_day_filters_apply_to_each_actual_role_before_pagination_and_csv(self):
        self.at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        self.assertEqual(len(self.table(True)), 15)
        self.at.selectbox(key='club_trends_day_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_day_page').set_value(2).run()
        self.at.multiselect(key='club_trends_pct_selected').set_value(['home:2nd', 'away:2nd']).run()
        for side in ('home', 'away'):
            self.at.selectbox(key=f'club_trends_pct_{side}_2nd_mode').set_value('minimum').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 1)
        self.assertEqual(len(self.table(True)), 10)
        exported = self.download()
        self.assertEqual(len(exported), 12)
        self.assertTrue(all(float(row['Domicile · 2nd (%)']) > 66 for row in exported))
        self.assertTrue(all(float(row['Extérieur · 2nd (%)']) > 66 for row in exported))
        self.assertTrue(all(row['Date'] == '06/11/2026' for row in exported))
        self.assertEqual({row['ID du match API'] for row in exported}, {'1001'} | {str(1010+i) for i in range(11)})
        self.at.selectbox(key='club_trends_day_page').set_value(2).run()
        self.at.selectbox(key='club_trends_role').set_value('away')
        next(b for b in self.at.button if b.label == 'Afficher et actualiser').click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 1)
        self.assertEqual(len(self.download()), 12)
        self.at.multiselect(key='club_trends_pct_selected').set_value(['home:2nd']).run()
        self.assertEqual(len(self.download()), 13)  # An unfiltered visitor may have no history.
        self.at.multiselect(key='club_trends_pct_selected').set_value([]).run()
        self.assertEqual(len(self.download()), 15)
        self.assertEqual(self.build.call_count, 2)
        self.assert_read_only()

    def test_interval_validation_and_filters_survive_navigation_without_rebuilding(self):
        self.at.multiselect(key='club_trends_pct_selected').set_value(['home:2nd']).run()
        self.at.selectbox(key='club_trends_pct_home_2nd_mode').set_value('range').run()
        self.at.number_input(key='club_trends_pct_home_2nd_value').set_value(60.0).run()
        self.at.number_input(key='club_trends_pct_home_2nd_upper').set_value(70.0).run()
        self.assertEqual(list(self.table()['ID club API']), ['99'])
        self.at.number_input(key='club_trends_pct_home_2nd_upper').set_value(59.0).run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('minimum' in item.value and 'maximum' in item.value for item in self.at.warning))
        self.at.number_input(key='club_trends_pct_home_2nd_upper').set_value(70.0).run()
        self.at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        self.assertEqual(self.at.multiselect(key='club_trends_pct_selected').value, ['home:2nd'])
        self.assertEqual(self.at.number_input(key='club_trends_pct_home_2nd_value').value, 60.0)
        self.assertEqual(len(self.download()), 13)
        self.at.button_group(key='club_trends_view').set_value('Tous les clubs').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(list(self.table()['ID club API']), ['99'])
        self.assertEqual(self.build.call_count, 1)
        self.assert_read_only()


if __name__ == '__main__':
    unittest.main()
