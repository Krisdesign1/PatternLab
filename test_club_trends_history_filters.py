"""Historical sample minimums filter each role before paging and complete CSV exports."""
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
APPLY = 'Appliquer le minimum d’historique'


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class HistoryCountLogicTests(unittest.TestCase):
    def test_inclusive_independent_minimums_use_and_not_total_or_recent_counts(self):
        rows = [
            {'home_matches': 10, 'away_matches': 10, 'matches': 20, 'recent_matches': 5},
            {'home_matches': 9, 'away_matches': 20, 'matches': 29, 'recent_matches': 20},
            {'home_matches': 20, 'away_matches': 9, 'matches': 29, 'recent_matches': 20},
            {'home_matches': 11, 'away_matches': 12, 'matches': 23, 'recent_matches': 5},
        ]
        self.assertEqual(panel._filter_history_counts(rows, (10, 10)), [rows[0], rows[3]])
        self.assertEqual(panel._filter_history_counts(rows, (0, 10)), [rows[0], rows[1], rows[3]])
        self.assertEqual(panel._filter_history_counts(rows, (10, 0)), [rows[0], rows[2], rows[3]])

    def test_zero_disables_a_role_and_missing_only_fails_an_active_minimum(self):
        rows = [{}, {'home_matches': None}, {'home_matches': 0}, {'home_matches': 1},
                {'home_matches': float('nan')}, {'home_matches': float('inf')}]
        self.assertEqual(panel._filter_history_counts(rows, (0, 0)), rows)
        self.assertEqual(panel._filter_history_counts(rows, (1, 0)), [rows[3]])
        self.assertEqual(panel._filter_history_counts(rows, (0, 1)), [])
        self.assertEqual(panel._history_filter_metadata((10, 7)),
                         {'Minimum historique domicile': 10, 'Minimum historique extérieur': 7})

    def test_fixtures_use_each_teams_actual_role_and_before_profiles(self):
        good = {'home_profile': {'home_matches': 10, 'away_matches': 0},
                'away_profile': {'home_matches': 0, 'away_matches': 10}}
        swapped = {'home_profile': {'home_matches': 0, 'away_matches': 20},
                   'away_profile': {'home_matches': 20, 'away_matches': 0}}
        after_only = {'home_profile': {'home_matches': 9}, 'away_profile': {'away_matches': 9},
                      'home_after_profile': {'home_matches': 10},
                      'away_after_profile': {'away_matches': 10}}
        missing_visitor = {'home_profile': {'home_matches': 10}, 'away_profile': None}
        rows = [good, swapped, after_only, missing_visitor, {}]
        self.assertEqual(panel._filter_history_counts(rows, (10, 10), fixtures=True), [good])
        self.assertEqual(panel._filter_history_counts(rows, (10, 0), fixtures=True),
                         [good, missing_visitor])
        self.assertEqual(panel._filter_history_counts(rows, (0, 0), fixtures=True), rows)


class HistoryPanelFixture(unittest.TestCase):
    comparison = False

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        history = []

        def add_pair(home, away, home_count=3, away_count=2):
            for index in range(home_count + away_count):
                mid = len(history) + 1
                stamp = datetime(2026, 10, 1, 12, tzinfo=timezone.utc) + timedelta(minutes=mid)
                row = raw(mid, '2nd', stamp=stamp.isoformat())
                first, second = (home, away) if index < home_count else (away, home)
                row['teams'] = {'home': {'id': first, 'name': f'Club {first}'},
                                'away': {'id': second, 'name': f'Club {second}'}}
                history.append(row)

        add_pair(99, 98)
        for index in range(11):
            add_pair(200 + index * 2, 201 + index * 2)
        add_pair(399, 398, home_count=4)
        other = raw(len(history) + 1, '2nd', stamp='2026-10-02T12:00:00Z', league=61)
        other['teams'] = {'home': {'id': 99, 'name': 'Club 99'},
                          'away': {'id': 98, 'name': 'Club 98'}}
        history.append(other)
        calendar = [day_fixture(1001, '2026-11-06T15:00:00Z', 'FT' if self.comparison else 'NS'),
                    day_fixture(1002, '2026-11-06T16:00:00Z', home=98, away=99),
                    day_fixture(1003, '2026-11-06T17:00:00Z', away=777),
                    day_fixture(1004, '2026-11-06T18:00:00Z', home=778),
                    day_fixture(1005, '2026-11-06T19:00:00Z', home=399, away=398)]
        calendar += [day_fixture(1010 + index, f'2026-11-06T20:{index:02}:00Z') for index in range(11)]
        self.path.write_text(json.dumps({'version': 1, 'fixtures': history + calendar,
                                        'season_imports': []}))
        fixture_store.migrate_json(self.path)
        self.db = fixture_store.database_path(self.path)
        self.digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        for name in ('_catalogue', '_summary', '_details', '_day_calendar', '_period_calendar'):
            cached = getattr(panel, name)
            cached.clear()
            self.addCleanup(cached.clear)
        self.mock(api, 'STORE_PATH', self.path)
        self.mock(panel, 'datetime', Clock)
        self.build = self.mock(trends, 'build', wraps=trends.build)
        self.compare_build = self.mock(trends, 'build_comparison', wraps=trends.build_comparison)
        self.network = self.mock(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.raw_load = self.mock(api, 'load_fixtures', side_effect=AssertionError('No full JSON load'))
        self.migration = self.mock(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))
        self.at = AppTest.from_string('from club_trends_panel import render\nrender()', default_timeout=20)
        self.at.session_state['analysis_selected_day'] = DAY
        self.at.session_state['analysis_follow_today'] = False
        self.at.session_state['club_trends_compare'] = self.comparison
        self.at.run()
        self.assertFalse(self.at.exception)

    def mock(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def table(self, day=False):
        column = 'ID du match API' if day else 'ID club API'
        return next(frame.value for frame in self.at.dataframe if column in frame.value.columns)

    def apply_minima(self, home, away):
        self.at.number_input(key='club_trends_min_home').set_value(home)
        self.at.number_input(key='club_trends_min_away').set_value(away)
        next(button for button in self.at.button if button.label == APPLY).click().run()
        self.assertFalse(self.at.exception)

    def day_view(self):
        self.at.button_group(key='club_trends_view').set_value('Matchs de la journée').run()
        self.assertFalse(self.at.exception)

    def export(self):
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as download:
            self.at.run()
            self.assertFalse(self.at.exception)
        data = download.call_args.kwargs['data']
        content = data() if callable(data) else data
        return list(csv.DictReader(StringIO(content.decode('utf-8-sig')), delimiter=';'))

    def assert_read_only_without_rebuilding(self):
        self.network.assert_not_called()
        self.raw_load.assert_not_called()
        self.migration.assert_not_called()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.digest)
        self.assertEqual(self.build.call_count, 0 if self.comparison else 1)
        self.assertEqual(self.compare_build.call_count, 1 if self.comparison else 0)


class HistoryPanelTests(HistoryPanelFixture):
    def test_all_clubs_applies_explicitly_resets_page_and_exports_all_selected_profiles(self):
        self.assertEqual(self.at.number_input(key='club_trends_min_home').value, 0)
        self.assertEqual(self.at.number_input(key='club_trends_min_away').value, 0)
        self.at.selectbox(key='club_trends_summary_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_summary_page').set_value(2).run()
        before_ids = list(self.table()['ID club API'])
        self.at.number_input(key='club_trends_min_home').set_value(3).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(list(self.table()['ID club API']), before_ids)
        self.assertEqual(self.at.selectbox(key='club_trends_summary_page').value, 2)
        self.assertEqual(self.at.session_state['club_trends_support_applied'], (0, 0))
        self.apply_minima(3, 2)
        self.assertEqual(self.at.selectbox(key='club_trends_summary_page').value, 1)
        self.assertEqual(len(self.table()), 10)
        exported = self.export()
        self.assertEqual(len(exported), 13)
        self.assertTrue(all(int(row['Matchs historiques domicile']) >= 3 for row in exported))
        self.assertTrue(all(int(row['Matchs historiques extérieur']) >= 2 for row in exported))
        self.assertEqual({row['Minimum historique domicile'] for row in exported}, {'3'})
        self.assertEqual({row['Minimum historique extérieur'] for row in exported}, {'2'})
        self.assertFalse(any(row['ID compétition API'] == '61' for row in exported))
        self.assert_read_only_without_rebuilding()

    def test_day_uses_actual_roles_shares_applied_values_and_preserves_unknown_when_disabled(self):
        self.day_view()
        self.assertEqual(len(self.table(True)), 16)
        self.at.selectbox(key='club_trends_day_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_day_page').set_value(2).run()
        self.apply_minima(3, 3)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 1)
        self.assertEqual(len(self.table(True)), 10)
        exported = self.export()
        self.assertEqual({row['ID du match API'] for row in exported},
                         {'1001', '1005'} | {str(1010 + index) for index in range(11)})
        self.assertEqual(len(exported), 13)
        self.assertTrue(all(int(row['Matchs historiques domicile']) >= 3 and
                            int(row['Matchs historiques extérieur']) >= 3 for row in exported))
        self.at.button_group(key='club_trends_view').set_value('Tous les clubs').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.number_input(key='club_trends_min_home').value, 3)
        self.assertEqual(self.at.number_input(key='club_trends_min_away').value, 3)
        self.assertTrue(any('Aucun club' in item.value for item in self.at.info))
        self.day_view()
        self.assertEqual(self.at.session_state['club_trends_support_applied'], (3, 3))
        self.apply_minima(3, 0)
        self.assertIn('1003', {row['ID du match API'] for row in self.export()})
        self.apply_minima(0, 0)
        self.assertEqual(len(self.export()), 16)
        self.assert_read_only_without_rebuilding()

    def test_history_minimum_combines_with_percentage_filter_and_empty_selection_can_be_reset(self):
        self.apply_minima(3, 2)
        self.at.multiselect(key='club_trends_pct_selected').set_value(['home:2nd']).run()
        self.at.number_input(key='club_trends_pct_home_2nd_value').set_value(100.0).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.export()), 13)
        self.apply_minima(10, 10)
        self.assertTrue(any('Aucun club' in item.value for item in self.at.info))
        self.apply_minima(3, 2)
        self.assertEqual(len(self.export()), 13)
        self.assertEqual(self.at.multiselect(key='club_trends_pct_selected').value, ['home:2nd'])
        self.assert_read_only_without_rebuilding()


class HistoryComparisonPanelTests(HistoryPanelFixture):
    comparison = True

    def test_before_counts_control_both_views_even_when_after_crosses_threshold(self):
        main = self.table()
        main = main[(main['ID club API'] == '99') & (main['ID compétition API'] == '39')].iloc[0]
        self.assertEqual(main['Matchs historiques domicile avant'], 3)
        self.assertEqual(main['Matchs historiques domicile après'], 4)
        self.apply_minima(4, 2)
        self.assertEqual(list(self.table()['ID club API']), ['399'])
        self.at.toggle(key='club_trends_compare_show_after').set_value(False).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(list(self.table()['ID club API']), ['399'])
        self.assertNotIn('Matchs historiques domicile après', self.table().columns)
        exported = self.export()
        self.assertEqual([row['ID club API'] for row in exported], ['399'])
        self.assertIn('Matchs historiques domicile après', exported[0])
        self.assertEqual(exported[0]['Minimum historique domicile'], '4')
        self.day_view()
        self.apply_minima(4, 4)
        self.assertEqual(list(self.table(True)['ID du match API']), ['1005'])
        self.at.toggle(key='club_trends_compare_show_after').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(list(self.table(True)['ID du match API']), ['1005'])
        self.assertEqual([row['ID du match API'] for row in self.export()], ['1005'])
        self.assert_read_only_without_rebuilding()

    def test_after_visibility_keeps_filtered_page_and_complete_export(self):
        self.day_view()
        self.apply_minima(3, 3)
        self.at.selectbox(key='club_trends_day_limit').set_value(10).run()
        self.at.selectbox(key='club_trends_day_page').set_value(2).run()
        ids = list(self.table(True)['ID du match API'])
        self.assertEqual(len(ids), 3)
        self.at.toggle(key='club_trends_compare_show_after').set_value(False).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 2)
        self.assertEqual(list(self.table(True)['ID du match API']), ids)
        exported = self.export()
        self.assertEqual(len(exported), 13)
        self.assertEqual({row['Minimum historique domicile'] for row in exported}, {'3'})
        self.assertEqual({row['Minimum historique extérieur'] for row in exported}, {'3'})
        self.assertEqual({row['Filtres et sélection appliqués à'] for row in exported}, {'Avant uniquement'})
        self.at.toggle(key='club_trends_compare_show_after').set_value(True).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='club_trends_day_page').value, 2)
        self.assertEqual(list(self.table(True)['ID du match API']), ids)
        self.assert_read_only_without_rebuilding()


if __name__ == '__main__':
    unittest.main()
