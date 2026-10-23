"""The probability search reads locally and writes only on explicit action."""
from copy import deepcopy
from datetime import datetime
from math import prod
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import catboost_scenarios as ml
from coupon_sizes import top_independent
import high_probability_panel as panel
import high_probability_workspace as workspace
import prediction_journal as journal
import prospective_models as models


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=panel.LOCAL_ZONE)


def row(number, probabilities, *, league='39', country='England', competition='Premier League'):
    fixture = dict(match_id=str(number), league=league, country=country, competition=competition,
                   home=f'Home {number}', away=f'Away {number}', timestamp=f'2026-10-22T{14 + number % 5:02d}:00:00',
                   status='TIMED', selectable=True, is_upcoming=True, is_historical=False)
    return dict(fixture=fixture, probabilities=probabilities, max_probability=max(probabilities.values()),
                choice=max(panel.VALUES, key=probabilities.get), baseline={'1st': .3, 'Égal': .3, '2nd': .4},
                training=100 + number, history_days=30, home_profile={'matches': 5}, away_profile={'matches': 6},
                before='2026-10-22', personalized=True)


def report():
    rows = [row(1, {'1st': .2, 'Égal': .1, '2nd': .7}),
            row(2, {'1st': .65, 'Égal': .2, '2nd': .15}, league='61', country='France', competition='Ligue 1'),
            row(3, {'1st': .3, 'Égal': .25, '2nd': .45}),
            row(4, {'1st': .1, 'Égal': .6, '2nd': .3}, league='140', country='Spain', competition='La Liga')]
    return dict(source='API-Football', version='test-clubs-v1', day='2026-10-22', before='2026-10-22',
                generated_at='2026-10-22T10:30:00+00:00', rows=rows,
                excluded=[dict(match_id='5', reason='insufficient_history', detail='Historique insuffisant')],
                diagnostics=dict(day_count=5, upcoming_count=5, eligible_count=4))


def prepared(report, ids, *, outcome='1st', minimum=0, choices=None):
    index = {str(row['fixture']['match_id']): row for row in report['rows']}
    rows = sorted((index[str(identifier)] for identifier in ids), key=lambda row: row['fixture']['timestamp'])
    distributions = [row['probabilities'] for row in rows]
    method = dict(version='test-saved-v1', ranking=top_independent(distributions, 3),
                  distribution=dict(schema=models.DISTRIBUTION_VERSION, type='independent',
                                    size=len(rows), distributions=distributions))
    selected = [choices[str(row['fixture']['match_id'])] if choices is not None else outcome for row in rows]
    requested = dict(sequence=selected, probability=prod(row['probabilities'][value] for row, value in zip(rows, selected)))
    snapshots = [dict(match_id=str(row['fixture']['match_id']), probabilities=row['probabilities'],
                      baseline=row.get('baseline'), choice=row['choice'], max_probability=row['max_probability'],
                      search_outcome=value, search_probability=row['probabilities'][value])
                 for row, value in zip(rows, selected)]
    return dict(fixtures=[row['fixture'] for row in rows], methods={'clubs': method}, requested_scenario=requested,
                metadata=dict(selection=dict(search=dict(outcome='mixed' if choices is not None else outcome,
                                                        minimum=minimum),
                                             requested_scenario=requested, match_probabilities=snapshots)))


class HighProbabilityPanelTests(unittest.TestCase):
    def setUp(self):
        self.value = report()
        self.clock = self.patch(panel, '_now', return_value=NOW)
        self.load = self.patch(workspace, 'load_scan', side_effect=lambda: deepcopy(self.value))
        self.refresh = self.patch(workspace, 'refresh_scan', side_effect=lambda: deepcopy(self.value))
        self.prepare = self.patch(workspace, 'prepare_group', side_effect=prepared)
        self.record = self.patch(workspace, 'record_group', side_effect=lambda result: dict(
            id='saved-analysis', created=True, group_kind='mixed', methods=result['methods'],
            fixtures=result['fixtures'], metadata=result['metadata']))
        self.api = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No hidden API'))
        self.train = self.patch(ml, '_fit', side_effect=AssertionError('No training'))
        self.write = self.patch(journal, 'record', side_effect=AssertionError('No hidden journal write'))
        self.write_mixed = self.patch(journal, 'record_mixed', side_effect=AssertionError('No hidden journal write'))

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open(self):
        at = AppTest.from_string('from high_probability_panel import render_high_probabilities\n'
                                 'render_high_probabilities()', default_timeout=20).run()
        self.assertFalse(at.exception)
        return at

    def table(self, at):
        return next(node.value for node in at.dataframe if 'Probabilité recherchée (%)' in node.value.columns)

    def readonly(self):
        for mock in (self.api, self.train, self.write, self.write_mixed):
            mock.assert_not_called()

    def test_first_ranking_shows_all_matches_and_probabilities_without_support_sorting(self):
        original = deepcopy(self.value)
        at = self.open()
        self.assertEqual(at.number_input(key='hp_minimum_1st').value, 0)
        self.assertEqual(at.segmented_control(key='hp_outcome').value, '1st')
        table = self.table(at)
        self.assertEqual(table['Match'].tolist(), ['Home 2 — Away 2', 'Home 3 — Away 3', 'Home 1 — Away 1', 'Home 4 — Away 4'])
        self.assertEqual(table['Probabilité recherchée (%)'].tolist(), [65.0, 30.0, 20.0, 10.0])
        self.assertEqual(table['Résultat recherché'].tolist(), [['1st']] * 4)
        self.assertEqual(table['Résultat le plus probable'].tolist(), [['1st'], ['2nd'], ['2nd'], ['Égal']])
        self.assertTrue(set(panel.VALUES).issubset(table.columns))
        self.assertEqual(len(set(table['Compétition'])), 3)
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Sous le seuil'), '0')
        for category, expected in [('1st', '65.00 %'), ('Égal', '60.00 %'), ('2nd', '70.00 %')]:
            self.assertEqual(next(node.value for node in at.metric if node.label == f'Meilleur {category}'), expected)
        self.assertEqual(self.value, original)
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        self.refresh.assert_not_called()
        self.readonly()

    def test_changing_page_size_from_last_page_keeps_valid_match_ranges(self):
        self.value['rows'] = [row(number, {'1st': .2, 'Égal': .25, '2nd': .55})
                              for number in range(1, 64)]
        at = self.open()
        at.selectbox(key='hp_page').select(6).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table(at)['Rang'].tolist(), [61, 62, 63])
        for size in (50, 25, 10):
            with self.subTest(size=size):
                at.selectbox(key='hp_page_size').select(size).run()
                self.assertFalse(at.exception)
                self.assertEqual(at.selectbox(key='hp_page').value, 0)
                self.assertEqual(self.table(at)['Rang'].tolist(), list(range(1, size + 1)))
        at.selectbox(key='hp_page').select(6).run()
        self.value['rows'] = self.value['rows'][:13]
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='hp_page').value, 0)
        at.selectbox(key='hp_page_size').select(50).run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table(at)['Rang'].tolist(), list(range(1, 14)))
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        self.refresh.assert_not_called()
        self.readonly()

    def test_stale_page_labels_and_invalid_saved_pages_recover_without_crashing(self):
        at = AppTest.from_string(
            "import streamlit as st\n"
            "from high_probability_panel import render_high_probabilities\n"
            "if 'restore_page' in st.session_state:\n"
            "    st.session_state['hp_page'] = st.session_state.pop('restore_page')\n"
            "render_high_probabilities()", default_timeout=20).run()
        self.assertFalse(at.exception)
        for value in ('1 / 10', '0', None, True, -1, 999):
            with self.subTest(saved_page=value):
                at.session_state['restore_page'] = value
                at.run()
                self.assertFalse(at.exception)
                self.assertEqual(at.selectbox(key='hp_page').value, 0)
                self.assertEqual(len(self.table(at)), 4)
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        self.refresh.assert_not_called()
        self.readonly()

    def test_invalid_saved_page_sizes_recover_before_pagination_arithmetic(self):
        for value in ('50', None, True, 0, 100):
            with self.subTest(saved_size=value):
                at = AppTest.from_string(
                    'from high_probability_panel import render_high_probabilities\n'
                    'render_high_probabilities()', default_timeout=20)
                at.session_state['hp_page_size'] = value
                at.run()
                self.assertFalse(at.exception)
                self.assertEqual(at.selectbox(key='hp_page_size').value, 10)
                self.assertEqual(len(self.table(at)), 4)
        self.readonly()

    def test_threshold_outcome_and_search_never_add_under_threshold_fixtures(self):
        at = self.open()
        at.number_input(key='hp_minimum_1st').set_value(90).run()
        self.assertFalse(at.exception)
        self.assertFalse(any('Maximum (%)' in node.value.columns for node in at.dataframe))
        self.assertTrue(any('Le seuil reste inchangé' in node.value for node in at.info))
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Meilleur 2nd'), '70.00 %')
        at.number_input(key='hp_minimum_1st').set_value(40).run()
        self.assertEqual(self.table(at)['Match'].tolist(), ['Home 2 — Away 2'])
        at.text_input(key='hp_search').set_value('Spain').run()
        self.assertFalse(any('Maximum (%)' in node.value.columns for node in at.dataframe))
        at.segmented_control(key='hp_outcome').set_value('Égal').run()
        self.assertEqual(self.table(at)['Match'].tolist(), ['Home 4 — Away 4'])
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        self.refresh.assert_not_called()
        self.readonly()

    def test_top_group_can_mix_competitions_and_is_not_automatically_recorded(self):
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        self.assertFalse(at.exception)
        self.prepare.assert_called_once()
        self.assertEqual(self.prepare.call_args.args[1], ['2', '3'])
        self.assertEqual(self.prepare.call_args.kwargs, dict(outcome='1st', minimum=0))
        self.assertEqual({fixture['league'] for fixture in at.session_state['hp_prepared']['fixtures']}, {'39', '61'})
        self.assertTrue(any(node.value == 'Les trois scénarios les mieux classés · Clubs' for node in at.subheader))
        self.assertTrue(any('probabilité d’un scénario complet est plus faible' in node.value for node in at.caption))
        self.record.assert_not_called()
        self.refresh.assert_not_called()
        self.readonly()

    def test_each_outcome_has_its_own_ranking_and_remembers_its_threshold(self):
        at = self.open()
        at.number_input(key='hp_minimum_1st').set_value(20).run()
        at.segmented_control(key='hp_outcome').set_value('Égal').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.number_input(key='hp_minimum_equal').value, 0)
        self.assertEqual(self.table(at)['Match'].tolist(),
                         ['Home 4 — Away 4', 'Home 3 — Away 3', 'Home 2 — Away 2', 'Home 1 — Away 1'])
        at.number_input(key='hp_minimum_equal').set_value(25).run()
        at.segmented_control(key='hp_outcome').set_value('2nd').run()
        self.assertEqual(at.number_input(key='hp_minimum_2nd').value, 0)
        self.assertEqual(self.table(at)['Match'].tolist(),
                         ['Home 1 — Away 1', 'Home 3 — Away 3', 'Home 4 — Away 4', 'Home 2 — Away 2'])
        at.number_input(key='hp_minimum_2nd').set_value(50).run()
        at.segmented_control(key='hp_outcome').set_value('1st').run()
        self.assertEqual(at.number_input(key='hp_minimum_1st').value, 20)
        self.assertEqual(len(self.table(at)), 3)
        at.segmented_control(key='hp_outcome').set_value('Égal').run()
        self.assertEqual(at.number_input(key='hp_minimum_equal').value, 25)
        self.assertEqual(len(self.table(at)), 2)
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        self.readonly()

    def test_requested_scenario_is_shown_and_saved_even_outside_the_top_three(self):
        at = self.open()
        at.multiselect(key='hp_manual').set_value(['2', '4']).run()
        at.button(key='hp_analyze_manual').click().run()
        self.assertFalse(at.exception)
        value = at.session_state['hp_prepared']
        self.assertEqual(value['requested_scenario']['sequence'], ['1st', '1st'])
        self.assertNotIn(['1st', '1st'], [item['sequence'] for item in value['methods']['clubs']['ranking']])
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Probabilité du scénario recherché'), '6.50 %')
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        saved_requested = self.record.call_args.args[0]['metadata']['selection']['requested_scenario']
        self.assertEqual(saved_requested['sequence'], ['1st', '1st'])
        self.assertAlmostEqual(saved_requested['probability'], .065)
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Probabilité du scénario recherché'), '6.50 %')
        self.readonly()

    def test_manual_group_and_explicit_save_preserve_the_search_criteria(self):
        at = self.open()
        at.multiselect(key='hp_manual').set_value(['2', '4']).run()
        at.button(key='hp_analyze_manual').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.prepare.call_args.args[1], ['2', '4'])
        self.record.assert_not_called()
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        self.record.assert_called_once()
        criteria = self.record.call_args.args[0]['metadata']['selection']['search']
        self.assertEqual(criteria, dict(minimum=0, outcome='1st', query=''))
        self.assertEqual(at.session_state['hp_saved']['id'], 'saved-analysis')
        self.assertTrue(any('Analyse enregistrée avant match' in node.value for node in at.success))
        self.assertFalse(any(button.key == 'hp_record' for button in at.button))
        self.readonly()

    def test_refresh_is_explicit_and_invalidates_prepared_groups(self):
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        self.assertIn('hp_prepared', at.session_state)
        at.button(key='hp_refresh').click().run()
        self.assertFalse(at.exception)
        self.refresh.assert_called_once()
        self.assertNotIn('hp_prepared', at.session_state)
        self.record.assert_not_called()
        self.readonly()

    def test_existing_forecast_is_shown_instead_of_the_newly_prepared_probabilities(self):
        old = prepared(self.value, ['1', '2'])
        old.pop('requested_scenario')
        old['metadata'] = {}
        distributions = [{'1st': .1, 'Égal': .8, '2nd': .1} for _ in range(2)]
        old['methods']['clubs']['distribution']['distributions'] = distributions
        old['methods']['clubs']['ranking'] = top_independent(distributions, 3)
        self.record.side_effect = lambda result: dict(id='older-analysis', created=False, group_kind='mixed', **old)
        render = self.patch(panel.scenario_tables, 'render_matrix', wraps=panel.scenario_tables.render_matrix)
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(render.call_args.args[0], old['methods'])
        self.assertTrue(any('première prévision est conservée' in node.value for node in at.info))
        # The main ranking remains current; a second, newly computed table is
        # not presented as if it belonged to the older immutable forecast.
        self.assertEqual(sum('Maximum (%)' in node.value.columns for node in at.dataframe), 1)
        self.assertFalse(any(node.label == 'Probabilité du scénario recherché' for node in at.metric))
        self.readonly()

    def test_saved_mixed_group_opens_its_existing_daily_history(self):
        switch = self.patch(panel.st, 'switch_page', return_value=None)
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        at.button(key='hp_record').click().run()
        at.button(key='hp_open_journal').click().run()
        self.assertFalse(at.exception)
        switch.assert_called_once_with('ui_pages/daily.py')
        self.assertEqual(at.session_state['daily_selected'], 'saved-analysis')
        self.assertEqual(at.session_state['daily_view'], 'today')
        self.assertEqual(at.session_state['daily_context'], ('2026-10-22', 'today', '2026-10-22'))
        self.readonly()

    def test_filters_group_changes_kickoff_and_midnight_expire_the_preparation(self):
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        at.number_input(key='hp_minimum_1st').set_value(10).run()
        self.assertNotIn('hp_prepared', at.session_state)
        at.button(key='hp_analyze_top').click().run()
        at.number_input(key='hp_top_count').set_value(3).run()
        self.assertNotIn('hp_prepared', at.session_state)
        at.button(key='hp_analyze_top').click().run()
        self.clock.return_value = datetime(2026, 10, 22, 23, 0, tzinfo=panel.LOCAL_ZONE)
        at.run()
        self.assertFalse(at.exception)
        self.assertNotIn('hp_prepared', at.session_state)
        self.clock.return_value = datetime(2026, 10, 23, 0, 1, tzinfo=panel.LOCAL_ZONE)
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('La journée a changé' in node.value for node in at.info))
        self.record.assert_not_called()
        self.refresh.assert_not_called()
        self.readonly()

    def test_unchanged_probabilities_keep_the_preparation_across_the_next_local_calculation(self):
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        self.assertIn('hp_prepared', at.session_state)
        self.value['generated_at'] = '2026-10-22T11:01:00+00:00'
        at.run()
        self.assertFalse(at.exception)
        self.assertIn('hp_prepared', at.session_state)
        self.value['rows'][0]['probabilities'] = {'1st': .1, 'Égal': .1, '2nd': .8}
        self.value['rows'][0]['max_probability'] = .8
        at.run()
        self.assertNotIn('hp_prepared', at.session_state)
        self.record.assert_not_called()
        self.readonly()

    def test_absent_calendar_is_empty_without_triggering_an_api_request(self):
        self.load.side_effect = lambda: None
        at = self.open()
        self.assertTrue(any('Aucun calendrier local' in node.value for node in at.info))
        self.assertFalse(at.dataframe)
        self.refresh.assert_not_called()
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        self.readonly()

    def test_previous_all_results_session_opens_the_first_independent_ranking(self):
        at = AppTest.from_string('from high_probability_panel import render_high_probabilities\n'
                                 'render_high_probabilities()', default_timeout=20)
        at.session_state['hp_outcome'] = None
        at.session_state['hp_minimum'] = 50
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.segmented_control(key='hp_outcome').value, '1st')
        self.assertEqual(at.number_input(key='hp_minimum_1st').value, 0)
        self.assertEqual(len(self.table(at)), 4)
        self.readonly()

    def test_ranking_compares_the_requested_result_with_its_league_baseline(self):
        self.value['rows'][2].pop('baseline')
        at = self.open()
        table = self.table(at).set_index('Match')
        self.assertAlmostEqual(table.loc['Home 2 — Away 2', 'Tendance du championnat (%)'], 30)
        self.assertAlmostEqual(table.loc['Home 2 — Away 2', 'Écart à la tendance (points)'], 35)
        self.assertTrue(table.loc['Home 3 — Away 3', ['Tendance du championnat (%)',
                                                   'Écart à la tendance (points)']].isna().all())
        at.segmented_control(key='hp_outcome').set_value('2nd').run()
        table = self.table(at).set_index('Match')
        self.assertAlmostEqual(table.loc['Home 2 — Away 2', 'Tendance du championnat (%)'], 40)
        self.assertAlmostEqual(table.loc['Home 2 — Away 2', 'Écart à la tendance (points)'], -25)
        self.readonly()

    def test_mixed_builder_uses_all_matches_even_when_the_ranking_is_empty(self):
        at = self.open()
        at.number_input(key='hp_minimum_1st').set_value(100).run()
        at.text_input(key='hp_search').set_value('Absent club').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.multiselect(key='hp_mix_widget_ids').options), 4)
        self.assertTrue(at.button(key='hp_mix_prepare').disabled)
        at.multiselect(key='hp_mix_widget_ids').set_value(['4', '2', '1']).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.button(key='hp_mix_prepare').disabled)
        at.segmented_control(key='hp_mix_choice_1').set_value('1st').run()
        at.segmented_control(key='hp_mix_choice_2').set_value('Égal').run()
        at.segmented_control(key='hp_mix_choice_4').set_value('2nd').run()
        self.prepare.assert_not_called()
        self.record.assert_not_called()
        at.button(key='hp_mix_prepare').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.prepare.call_args.args[1], ['1', '2', '4'])
        self.assertEqual(self.prepare.call_args.kwargs, dict(choices={'1': '1st', '2': 'Égal', '4': '2nd'}))
        value = at.session_state['hp_prepared']
        self.assertEqual(value['requested_scenario']['sequence'], ['1st', 'Égal', '2nd'])
        self.assertNotIn(['1st', 'Égal', '2nd'], [item['sequence'] for item in value['methods']['clubs']['ranking']])
        self.assertAlmostEqual(value['requested_scenario']['probability'], .012)
        selected = next(node.value for node in at.dataframe if len(node.value) == 3)
        self.assertEqual(selected['Résultat recherché'].tolist(), [['1st'], ['Égal'], ['2nd']])
        self.assertEqual(selected['Probabilité recherchée (%)'].tolist(), [20, 20, 30])
        self.assertEqual(selected['Tendance du championnat (%)'].tolist(), [30, 30, 40])
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Probabilité du scénario recherché'), '1.20 %')
        self.record.assert_not_called()
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(self.record.call_args.args[0]['metadata']['selection']['requested_scenario']['sequence'],
                         ['1st', 'Égal', '2nd'])
        self.readonly()

    def test_mixed_basket_and_prepared_analysis_survive_unrelated_ranking_filters(self):
        at = self.open()
        at.multiselect(key='hp_mix_widget_ids').set_value(['1', '2']).run()
        at.segmented_control(key='hp_mix_choice_1').set_value('Égal').run()
        at.button(key='hp_mix_prepare').click().run()
        at.segmented_control(key='hp_outcome').set_value('2nd').run()
        at.number_input(key='hp_minimum_2nd').set_value(100).run()
        at.text_input(key='hp_search').set_value('no match').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.multiselect(key='hp_mix_widget_ids').value, ['1', '2'])
        self.assertEqual(at.segmented_control(key='hp_mix_choice_1').value, 'Égal')
        self.assertEqual(at.session_state['hp_prepared']['requested_scenario']['sequence'], ['Égal', '1st'])
        self.prepare.assert_called_once()
        at.segmented_control(key='hp_mix_choice_2').set_value('2nd').run()
        self.assertFalse(at.exception)
        self.assertNotIn('hp_prepared', at.session_state)
        self.assertFalse(any(button.key == 'hp_record' for button in at.button))
        self.record.assert_not_called()
        self.readonly()

    def test_mixed_basket_removes_started_fixtures_and_expires_old_preparation(self):
        at = self.open()
        at.multiselect(key='hp_mix_widget_ids').set_value(['1', '2', '4']).run()
        at.button(key='hp_mix_prepare').click().run()
        self.clock.return_value = datetime(2026, 10, 22, 16, 30, tzinfo=panel.LOCAL_ZONE)
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['hp_mix_ids'], ['2', '4'])
        self.assertNotIn('hp_prepared', at.session_state)
        self.assertEqual(at.multiselect(key='hp_mix_widget_ids').value, ['2', '4'])
        self.clock.return_value = datetime(2026, 10, 23, 0, 1, tzinfo=panel.LOCAL_ZONE)
        at.run()
        self.assertFalse(at.exception)
        self.assertNotIn('hp_mix_ids', at.session_state)
        self.record.assert_not_called()
        self.readonly()

    def test_mixed_save_preserves_frozen_selected_probabilities(self):
        at = self.open()
        at.multiselect(key='hp_mix_widget_ids').set_value(['1', '2']).run()
        at.segmented_control(key='hp_mix_choice_1').set_value('Égal').run()
        at.button(key='hp_mix_prepare').click().run()
        original_record = self.record.side_effect
        self.record.side_effect = lambda value: dict(original_record(value), metadata=dict(selection=dict(
            value['metadata']['selection'], match_probabilities=[dict(snapshot,
                probabilities={'1st': .1, 'Égal': .7, '2nd': .2},
                baseline={'1st': .2, 'Égal': .5, '2nd': .3})
                for snapshot in value['metadata']['selection']['match_probabilities']])))
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        tables = [node.value for node in at.dataframe if 'Probabilité recherchée (%)' in node.value.columns]
        self.assertEqual(tables[-1]['Probabilité recherchée (%)'].tolist(), [70, 10])
        self.assertEqual(tables[-1]['Tendance du championnat (%)'].tolist(), [50, 20])
        self.readonly()

    def test_detail_keeps_competition_names_when_journal_fixtures_only_have_ids(self):
        prepare_original = self.prepare.side_effect
        def without_labels(*args, **kwargs):
            result = prepare_original(*args, **kwargs)
            result['fixtures'] = [{key: value for key, value in fixture.items()
                                   if key not in ('country', 'competition')} for fixture in result['fixtures']]
            return result
        self.prepare.side_effect = without_labels
        at = self.open()
        at.button(key='hp_analyze_top').click().run()
        self.assertFalse(at.exception)
        selected = [node.value for node in at.dataframe if 'Probabilité recherchée (%)' in node.value.columns][-1]
        self.assertEqual(selected['Compétition'].tolist(), ['France · Ligue 1', 'England · Premier League'])
        record_original = self.record.side_effect
        self.record.side_effect = lambda value: dict(record_original(value), metadata=dict(value['metadata'],
            competitions={'61': dict(country='France enregistrée', competition='Ligue enregistrée')}))
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        selected = [node.value for node in at.dataframe if 'Probabilité recherchée (%)' in node.value.columns][-1]
        self.assertEqual(selected['Compétition'].tolist(), ['France enregistrée · Ligue enregistrée',
                                                          'England · Premier League'])
        self.readonly()

    def test_new_mixed_choice_on_an_existing_forecast_uses_intent_snapshots_and_history(self):
        old = prepared(self.value, ['1', '2'])
        distributions = [{'1st': .1, 'Égal': .8, '2nd': .1} for _ in range(2)]
        old['methods']['clubs']['distribution']['distributions'] = distributions
        old['methods']['clubs']['ranking'] = top_independent(distributions, 3)
        intent_selection = dict(search=dict(outcome='mixed', minimum=0),
            requested_scenario=dict(sequence=['Égal', '2nd'], probability=.08),
            match_probabilities=[dict(match_id=identity, probabilities=distribution,
                baseline={'1st': .2, 'Égal': .5, '2nd': .3}, search_outcome=choice,
                search_probability=distribution[choice], choice='Égal', max_probability=.8)
                for identity, choice, distribution in zip(['1', '2'], ['Égal', '2nd'], distributions)])
        self.record.side_effect = lambda value: dict(old, id='older-analysis', created=True,
            forecast_created=False, group_kind='mixed', scenario_intent=dict(
                id='new-intent', entry_id='older-analysis', selection=intent_selection))
        switch = self.patch(panel.st, 'switch_page', return_value=None)
        render = self.patch(panel.scenario_tables, 'render_matrix', wraps=panel.scenario_tables.render_matrix)
        at = self.open()
        at.multiselect(key='hp_mix_widget_ids').set_value(['1', '2']).run()
        at.segmented_control(key='hp_mix_choice_1').set_value('Égal').run()
        at.segmented_control(key='hp_mix_choice_2').set_value('2nd').run()
        at.button(key='hp_mix_prepare').click().run()
        at.button(key='hp_record').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(render.call_args.args[0], old['methods'])
        selected = [node.value for node in at.dataframe if 'Probabilité recherchée (%)' in node.value.columns][-1]
        self.assertEqual(selected['Résultat recherché'].tolist(), [['Égal'], ['2nd']])
        self.assertEqual(selected['Probabilité recherchée (%)'].tolist(), [80, 10])
        self.assertEqual(selected['Tendance du championnat (%)'].tolist(), [50, 30])
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Probabilité du scénario recherché'), '8.00 %')
        self.assertTrue(any('prévision figée' in node.value for node in at.success))
        at.button(key='hp_open_journal').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['daily_selected'], 'intent:new-intent')
        switch.assert_called_once_with('ui_pages/daily.py')
        self.readonly()


if __name__ == '__main__':
    unittest.main()
