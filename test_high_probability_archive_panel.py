"""Probability history exposes every saved field directly in its table."""
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import high_probability_archive_panel as panel
import high_probability_panel as ranking
import high_probability_workspace as workspace
import high_probability_archive as archive
import api_football as api
import result_explanations as reasons
from test_high_probability_archive_view import archived
from test_high_probability_history import LATER


class RankingArchivePanelTests(unittest.TestCase):
    def setUp(self):
        self.records = [archived(0, status='settled'), archived(1)]
        self.patch(ranking, '_now', return_value=LATER)
        self.patch(panel, '_load', side_effect=lambda *args: deepcopy(self.records))
        self.patch(panel, '_load_groups', return_value=([], []))
        self.explain = self.patch(panel, 'annotate_entries', side_effect=lambda entries, **kwargs:
            [dict(entry, result_explanation=reasons.describe_entry(entry, now=LATER)) for entry in entries])
        self.scan = self.patch(workspace, 'load_scan', side_effect=AssertionError('History must not scan'))
        self.capture = self.patch(archive, 'capture', side_effect=AssertionError('No forecast on history open'))
        self.reconcile = self.patch(archive, 'reconcile', side_effect=AssertionError('No implicit result writes'))
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API on history read'))
        self.local = self.patch(api, 'load_fixtures', side_effect=AssertionError('No production fixture read'))

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def open(self, day=None):
        at = AppTest.from_string('from high_probability_panel import render_high_probabilities\nrender_high_probabilities()', default_timeout=20)
        at.session_state['hp_view'] = 'history'
        if day is not None:
            at.session_state['analysis_selected_day'] = day
            at.session_state['analysis_follow_today'] = False
        at.run()
        self.assertFalse(at.exception)
        return at

    def table(self, at):
        return next(item.value for item in at.dataframe if 'Probabilité (%)' in item.value.columns)

    def count(self, at):
        return sum(len(item.value) for item in at.dataframe if 'Probabilité (%)' in item.value.columns)

    def content(self, at):
        return '\n'.join(item.value for kind in (at.markdown, at.caption, at.info, at.warning) for item in kind)

    def readonly(self):
        for item in (self.scan, self.capture, self.reconcile, self.network, self.local):
            item.assert_not_called()

    def test_complete_table_has_probabilities_scores_and_no_detail_controls(self):
        at = self.open()
        self.assertEqual(self.count(at), 2)
        self.assertEqual(len(at.dataframe), 1)
        self.assertEqual(len(self.table(at).columns), 25)
        self.assertNotIn('Détails', self.table(at).columns)
        for column in ('1st (%)', 'Égal (%)', '2nd (%)', 'Score à la pause', 'Score à 90 minutes',
                       'Résultat réel', 'Favori du modèle', 'Statut', 'Motif', 'Calcul avant match',
                       'Version du modèle', 'Provenance', 'Calculs conservés', 'Corrections des résultats'):
            self.assertIn(column, self.table(at).columns)
        self.assertFalse(at.metric and any(metric.label == 'Estimation' for metric in at.metric))
        self.assertEqual(self.table(at)['Probabilité (%)'].tolist(), [60, 20])
        self.assertEqual(self.table(at)['1st (%)'].tolist(), [60, 20])
        self.assertEqual(self.table(at)['2nd (%)'].tolist(), [30, 50])
        self.assertEqual(self.table(at)['Score à la pause'].tolist(), ['1-0', 'Non reçu'])
        self.assertNotIn('hp_archive_detail', at.session_state)
        self.assertFalse(any(button.label == 'Enregistrer avant match' for button in at.button))
        self.assertFalse(any('Détails' in button.label or 'Retour' in button.label for button in at.button))
        self.assertFalse(at.expander)
        self.readonly()

    def test_three_categories_reuse_saved_values_and_sort_without_recalculating(self):
        at = self.open()
        at.segmented_control(key='hp_archive_outcome').set_value('2nd').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table(at)['Probabilité (%)'].tolist(), [50, 30])
        at.segmented_control(key='hp_archive_outcome').set_value('Égal').run()
        self.assertEqual(self.table(at)['Probabilité (%)'].tolist(), [30, 10])
        self.readonly()

    def test_filters_keep_all_three_probabilities_and_scores_in_the_same_table(self):
        at = self.open()
        at.selectbox(key='hp_archive_state').set_value('settled').run()
        data = self.table(at).iloc[0]
        self.assertEqual(data[['1st (%)', 'Égal (%)', '2nd (%)']].tolist(), [60, 10, 30])
        self.assertEqual(data['Score à la pause'], '1-0')
        self.assertIn('22/10/2026', data['Calcul avant match'])
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='hp_archive_state').value, 'settled')
        self.assertEqual(self.count(at), 1)
        self.readonly()

    def test_trend_and_signed_point_gap_are_visible_in_table_without_mutation(self):
        self.records[0]['baseline'] = {'1st': .45, 'Égal': .2, '2nd': .35}
        before = deepcopy(self.records)
        at = self.open()
        self.assertEqual(self.table(at)['Tendance (%)'].iloc[0], 45)
        self.assertAlmostEqual(self.table(at)['Écart (points)'].iloc[0], 15)
        self.assertIn('fréquence historique lissée', self.content(at))
        self.assertEqual(self.records, before)
        self.readonly()

    def test_missing_baseline_is_not_rebuilt_from_current_data(self):
        at = self.open()
        self.assertTrue(self.table(at)['Tendance (%)'].isna().all())
        self.assertTrue(self.table(at)['Écart (points)'].isna().all())
        self.assertEqual(self.table(at)['1st (%)'].tolist(), [60, 20])
        self.readonly()

    def test_filters_do_not_replace_latest_probability_to_find_a_higher_one(self):
        at = self.open()
        at.number_input(key='hp_archive_minimum').set_value(70).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun match ne correspond' in node.value for node in at.info))
        at.number_input(key='hp_archive_minimum').set_value(0).run()
        at.selectbox(key='hp_archive_state').set_value('settled').run()
        self.assertEqual(self.count(at), 1)
        self.readonly()

    def test_empty_history_explains_archiving_without_creating_groups(self):
        self.records = []
        at = self.open()
        self.assertIn('Aucune probabilité avant match', self.content(at))
        self.assertIn('jamais enregistrés', self.content(at))
        self.readonly()

    def test_shared_future_date_and_manual_archive_choice_are_preserved(self):
        value = archived(0)
        value['fixture']['timestamp'] = '2026-10-26T14:00:00Z'
        value['day'] = '2026-10-26'
        value['id'] = 'tomorrow'
        self.records.append(value)
        at = self.open(date(2026, 10, 26))
        self.assertEqual(at.selectbox(key='hp_archive_day').value, '2026-10-26')
        self.assertEqual(self.count(at), 1)
        self.assertEqual(list(self.table(at)['Verdict'].iloc[0]), ['En attente'])
        at.selectbox(key='hp_archive_day').set_value('2026-10-22').run()
        at.segmented_control(key='hp_archive_outcome').set_value('2nd').run()
        self.assertEqual(at.selectbox(key='hp_archive_day').value, '2026-10-22')
        self.assertEqual(self.count(at), 2)
        self.readonly()

    def test_missing_shared_day_stays_empty(self):
        at = self.open(date(2026, 10, 26))
        self.assertEqual(at.selectbox(key='hp_archive_day').value, '2026-10-26')
        self.assertIn('Aucun match ne correspond', self.content(at))
        self.assertEqual(self.count(at), 0)
        self.readonly()

    def test_verdict_filter_matches_display_for_past_pending_games(self):
        at = self.open()
        at.selectbox(key='hp_archive_verdict').set_value('unresolved').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.count(at), 1)
        self.assertEqual(list(self.table(at)['Verdict'].iloc[0]), ['Non évalué'])
        at.selectbox(key='hp_archive_verdict').set_value('pending').run()
        self.assertEqual(self.count(at), 0)
        self.assertIn('Aucun match ne correspond', self.content(at))
        self.readonly()

    def test_old_detail_session_never_hides_the_table_even_after_an_empty_filter(self):
        at = self.open()
        at.session_state['hp_archive_detail'] = 'archive-0'
        at.session_state['hp_archive_detail_context'] = ('old',)
        at.run()
        self.assertEqual(self.count(at), 2)
        self.assertNotIn('hp_archive_detail', at.session_state)
        at.number_input(key='hp_archive_minimum').set_value(70).run()
        self.assertNotIn('hp_archive_detail', at.session_state)
        at.number_input(key='hp_archive_minimum').set_value(0).run()
        self.assertEqual(self.count(at), 2)
        self.assertFalse(any(button.key == 'hp_archive_back' for button in at.button))
        self.readonly()

    def test_specific_missing_halftime_reason_is_visible_in_the_table_without_inventing_result(self):
        self.records = [archived(0, status='unresolved')]
        def explain(entries, **kwargs):
            return [dict(entry, result_explanation={
                'label': 'Score à la pause manquant', 'summary': 'Score à la pause absent du relevé API.',
                'matches': [], 'blocking': []}) for entry in entries]
        self.explain.side_effect = explain
        at = self.open()
        data = self.table(at).iloc[0]
        self.assertEqual(data['Statut'], 'Score à la pause manquant')
        self.assertEqual(data['Motif'], 'Score à la pause absent du relevé API.')
        self.assertEqual(list(data['Verdict']), ['Non évalué'])
        self.assertEqual(list(data['Résultat réel']), [])
        self.assertIn('ce n’est pas une défaite', self.content(at))
        self.readonly()

    def test_rescheduled_match_explains_new_date_directly_in_the_table(self):
        self.records = [archived(0, status='unresolved')]
        fixture = self.records[0]['fixture']
        mid = str(fixture['match_id'])
        raw = {'fixture': {'id': int(mid), 'date': '2026-10-28T17:30:00Z', 'status': {'short': 'NS'}},
               'league': {'id': int(fixture['league'])},
               'teams': {'home': {'id': int(fixture['home_id'])}, 'away': {'id': int(fixture['away_id'])}}}
        self.explain.side_effect = lambda entries, **kwargs: [dict(entry, result_explanation=
            reasons.describe_entry(entry, raw_by_id={mid: raw}, now=LATER)) for entry in entries]
        at = self.open()
        data = self.table(at).iloc[0]
        self.assertIn('Reprogrammé au 28/10 à 18:30', data['Statut'])
        self.assertIn('28/10', data['Motif'])
        self.assertEqual(list(data['Verdict']), ['Non évalué'])
        self.readonly()

    def test_pagination_and_filters_keep_all_fields_and_do_not_reorder_saved_matches(self):
        self.records = [dict(archived(0), id=f'id-{index}',
            fixture=dict(archived(0)['fixture'], match_id=str(1000+index), home=f'Home {index}')) for index in range(28)]
        at = self.open()
        at.selectbox(key='hp_archive_page_size').set_value(10).run()
        at.selectbox(key='hp_archive_page').set_value(1).run()
        before = self.table(at).copy(deep=True)
        self.assertEqual(before['Match'].iloc[0], 'Home 10 — Away 0')
        at.run()
        self.assertEqual(at.selectbox(key='hp_archive_page_size').value, 10)
        self.assertEqual(at.selectbox(key='hp_archive_page').value, 1)
        self.assertEqual(self.count(at), 10)
        self.assertEqual(self.table(at)['Match'].tolist(), before['Match'].tolist())
        self.assertIn('Calculs conservés', self.table(at).columns)
        at.text_input(key='hp_archive_search').set_value('Home 10').run()
        self.assertEqual(at.selectbox(key='hp_archive_page').value, 0)
        self.assertEqual(self.count(at), 1)
        self.assertEqual(self.table(at)['Match'].iloc[0], 'Home 10 — Away 0')
        self.assertEqual(at.selectbox(key='hp_archive_page_size').value, 10)
        self.readonly()


if __name__ == '__main__':
    unittest.main()
