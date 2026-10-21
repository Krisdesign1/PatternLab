"""User workflows for joint scenario rankings and recurrence explanations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import contextual_scenario_panel as panel
import football_data
import match_sources
from test_contextual_scenarios import sample
from test_football_data import fixture


class ContextualPanelTests(unittest.TestCase):
    def make_app(self, days=30, size=2):
        return AppTest.from_string(
            'from contextual_scenario_panel import render_contextual_scenarios\n'
            'from test_contextual_scenarios import sample\n'
            f"render_contextual_scenarios(sample(days={days}, size={size}), 'A', 'API-Football')",
            default_timeout=15).run()

    def submit(self, app):
        next(button for button in app.button if button.label == 'Classer les scénarios de ce groupe').click().run()
        self.assertFalse(app.exception)

    def test_manual_forecast_and_evaluation_persist_without_network(self):
        with patch.object(panel, 'prepare_calendar', return_value=[]), \
             patch.object(football_data.FootballDataClient, 'get', side_effect=AssertionError('No network')) as network:
            app = self.make_app()
            self.assertFalse(app.exception)
            self.assertFalse(app.selectbox)
            app.checkbox(key='show_contextual_scenarios').check().run()
            self.submit(app)
            self.assertTrue(any('Choisis les deux clubs' in item.value for item in app.warning))
            for i in range(2):
                suffix = '2' if i else ''
                app.selectbox(key=f'contextual_home_API-Football_A_2_{i}').set_value(f'Early H{suffix}')
                app.selectbox(key=f'contextual_away_API-Football_A_2_{i}').set_value(f'Early A{suffix}')
            self.submit(app)
            table = next(item.value for item in app.dataframe if 'Estimation combinée (%)' in item.value.columns)
            self.assertEqual(len(table), 3)
            self.assertEqual(table.iloc[0]['Scénario'], '1st → 1st')
            self.assertLess(table['Estimation combinée (%)'].sum(), 100)
            app.button(key='contextual_evaluate_API-Football_A_2').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('Estimation combinée (%)' in item.value.columns for item in app.dataframe))
            comparison = next(item.value for item in app.dataframe if 'Méthode' in item.value.columns)
            self.assertEqual(set(comparison['Méthode']), {'Répétitions seules', 'Clubs seuls', 'Clubs + répétitions'})
            self.assertEqual(len(set(comparison['Groupes testés'])), 1)
            app.run()
            self.assertTrue(any('Méthode' in item.value.columns for item in app.dataframe))
            network.assert_not_called()

    def test_calendar_selection_uses_chronological_order(self):
        calendar = [dict(home='Late H', away='Late A', league='A', match_id='202', provider='sample',
                         timestamp='2099-01-02T12:00:00'),
                    dict(home='Early H', away='Early A', league='A', match_id='201', provider='sample',
                         timestamp='2099-01-01T12:00:00')]
        with patch.object(panel, 'prepare_calendar', return_value=calendar):
            app = self.make_app()
            app.checkbox(key='show_contextual_scenarios').check().run()
            app.multiselect(key='contextual_calendar_API-Football_A_2').set_value(['202', '201'])
            self.submit(app)
            self.assertFalse(app.exception)
            order = app.dataframe[0].value
            self.assertEqual(list(order['Domicile']), ['Early H', 'Late H'])
            self.assertTrue(any('Scénarios adaptés' in item.value for item in app.markdown))

    def test_insufficient_history_and_duplicate_affiches_are_explained(self):
        with patch.object(panel, 'prepare_calendar', return_value=[]):
            app = self.make_app(days=5)
            app.checkbox(key='show_contextual_scenarios').check().run()
            for i in range(2):
                app.selectbox(key=f'contextual_home_API-Football_A_2_{i}').set_value('Early H')
                app.selectbox(key=f'contextual_away_API-Football_A_2_{i}').set_value('Early A')
            self.submit(app)
            self.assertTrue(any('une seule fois' in item.value for item in app.warning))
            app.selectbox(key='contextual_home_API-Football_A_2_1').set_value('Late H')
            app.selectbox(key='contextual_away_API-Football_A_2_1').set_value('Late A')
            self.submit(app)
            self.assertTrue(any('Historique insuffisant' in item.value for item in app.info))


class LocalCalendarTests(unittest.TestCase):
    def test_only_confirmed_scheduled_clubs_in_selected_competition(self):
        import api_football
        from test_api_match_workspace import raw
        rows=[raw(1), raw(2,status='FT'), raw(3,status='PST'), raw(4,league=61)]
        unknown=raw(5)
        unknown['teams']['home']['id']=None
        rows.append(unknown)
        with patch.object(api_football,'load_fixtures',return_value=rows):
            calendar=match_sources.scheduled_fixtures('API-Football','39')
        self.assertEqual([r['match_id'] for r in calendar],['1'])
        self.assertEqual(calendar[0]['provider'],'api-football')

    def test_recurrence_columns_do_not_treat_a_constant_history_as_excess(self):
        from league_pattern_panel import ranking_table
        from test_scenario_recurrence import result_for
        table = ranking_table(result_for([('2nd', '2nd')] * 25))
        self.assertEqual(table[0]['Apparitions'], 25)
        self.assertEqual(table[0]['Attendu (positions indépendantes)'], 25)
        self.assertEqual(table[0]['Rapport observé / attendu'], 1)


if __name__ == '__main__':
    unittest.main()
