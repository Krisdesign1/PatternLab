"""UI integration with an isolated real fixture database and no provider calls."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import competition_trends as trends
import competition_trends_panel as panel
import fixture_store
from test_competition_trends import raw


class CompetitionTrendsPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fixtures.json'
        rows = [raw(mid, ('1st', 'Égal', '2nd')[mid % 3]) for mid in range(1, 26)]
        rows += [raw(26, '1st', league=61, season=2025), raw(27, '1st', league=61)]
        invalid = raw(28, league=61)
        invalid['score']['halftime']['home'] = None
        rows.append(invalid)
        self.path.write_text(json.dumps({'version': 1, 'fixtures': rows, 'season_imports': []}))
        fixture_store.migrate_json(self.path)
        panel._catalogue.clear(); panel._summary.clear(); panel._details.clear()
        self.addCleanup(panel._catalogue.clear)
        self.addCleanup(panel._summary.clear)
        self.addCleanup(panel._details.clear)
        self._patch(api, 'STORE_PATH', self.path)
        self.build = self._patch(trends, 'build', wraps=trends.build)
        self.detail = self._patch(trends, 'details', wraps=trends.details)
        self.network = self._patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.full_raw = self._patch(api, 'load_fixtures', side_effect=AssertionError('No full raw load'))
        self.migrate = self._patch(fixture_store, 'migrate_json', side_effect=AssertionError('No migration'))
        self.at = AppTest.from_string('from competition_trends_panel import render\nrender()', default_timeout=15)
        self.at.session_state['analysis_selected_day'] = date(2026, 11, 6)
        self.at.session_state['analysis_follow_today'] = False
        self.at.run()
        self.assertFalse(self.at.exception)

    def _patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start(); self.addCleanup(patcher.stop)
        return value

    def table(self, key='trends_summary'):
        column = {'trends_summary': 'Tendance dominante', 'trends_comparison': 'Écart (points)',
                  'trends_matches': 'Compté dans la tendance'}[key]
        return next(frame.value for frame in self.at.dataframe if column in frame.value.columns)

    def apply(self):
        button = next(item for item in self.at.button if item.label == 'Afficher et actualiser')
        button.click().run()
        self.assertFalse(self.at.exception)

    def test_default_table_shows_three_classes_counts_colors_and_exclusions(self):
        table = self.table()
        self.assertEqual(len(table), 2)
        self.assertEqual(list(table['Matchs exploitables']), [25, 2])
        self.assertEqual(list(table['Résultats exclus']), [0, 1])
        for _, row in table.iterrows():
            self.assertAlmostEqual(sum(row[f'{value} (%)'] for value in ('1st', 'Égal', '2nd')), 100)
        self.assertEqual(list(table.iloc[0]['Tendance dominante']), ['Égal'])
        self.assertEqual(list(table.iloc[1]['Tendance dominante']), ['1st'])
        self.assertFalse(any(frame.key == 'trends_matches' for frame in self.at.dataframe))
        self.detail.assert_not_called()
        self.network.assert_not_called(); self.full_raw.assert_not_called(); self.migrate.assert_not_called()

    def test_country_search_sort_and_minimum_reuse_summary_without_database_rebuild(self):
        self.at.selectbox(key='trends_sort').set_value('1st').run()
        self.assertEqual(self.table().iloc[0]['Compétition'], 'Ligue 1')
        self.at.selectbox(key='trends_country').set_value('England').run()
        self.assertEqual(len(self.table()), 1)
        self.at.text_input(key='trends_search').set_value('premier').run()
        self.assertEqual(len(self.table()), 1)
        self.at.number_input(key='trends_minimum').set_value(30).run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any('Aucune compétition' in info.value for info in self.at.info))
        self.assertEqual(self.build.call_count, 1)

    def test_recent_window_form_is_applied_and_uses_disjoint_previous_effectif(self):
        self.at.selectbox(key='trends_recent').set_value(10)
        self.apply()
        table = self.table('trends_comparison')
        self.assertEqual(set(table['Matchs récents']), {10})
        self.assertEqual(set(table['Matchs précédents']), {15})
        self.assertAlmostEqual(table.iloc[0]['Écart (points)'], 30 - 100 / 3)
        self.assertEqual(self.build.call_count, 2)

    def test_seasons_and_date_range_filter_the_actual_sqlite_results(self):
        self.at.multiselect(key='trends_seasons').set_value([2025])
        self.apply()
        table = self.table()
        self.assertEqual(len(table), 1)
        self.assertEqual(int(table.iloc[0]['Matchs exploitables']), 1)
        self.at.date_input(key='trends_start').set_value(date(2026, 10, 27))
        self.apply()
        self.assertTrue(any('Aucune rencontre' in info.value for info in self.at.info))

    def test_match_evidence_is_on_demand_paginated_and_keeps_final_outcomes(self):
        self.at.toggle(key='trends_show_matches').set_value(True).run()
        self.assertFalse(self.at.exception)
        table = self.table('trends_matches')
        self.assertEqual(len(table), 25)
        self.assertTrue(table['Compté dans la tendance'].all())
        self.assertTrue(all(list(value) for value in table['Verdict final']))
        self.assertTrue(all('13:00' in value for value in table['Coup d’envoi (Kinshasa)']))
        self.at.selectbox(key='trends_match_limit').set_value(10).run()
        self.at.selectbox(key='trends_match_page').set_value(3).run()
        self.assertEqual(len(self.table('trends_matches')), 5)
        self.at.selectbox(key='trends_competition').set_value('61').run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.selectbox(key='trends_match_page').value, 1)
        evidence = self.table('trends_matches')
        self.assertEqual(len(evidence), 3)
        self.assertEqual(sum(evidence['Compté dans la tendance']), 2)
        self.assertIn('Score HT/FT absent', ' '.join(evidence['Motif']))

    def test_invalid_period_and_missing_database_are_actionable_without_exception(self):
        self.at.date_input(key='trends_start').set_value(date(2026, 12, 1))
        self.apply()
        self.assertTrue(any('doit précéder' in value.value for value in self.at.warning))
        panel._catalogue.clear()
        with patch.object(trends, 'catalogue', side_effect=trends.TrendsError('Base indisponible')):
            at = AppTest.from_string('from competition_trends_panel import render\nrender()').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Base indisponible' in value.value for value in at.warning))


if __name__ == '__main__':
    unittest.main()
