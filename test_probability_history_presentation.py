"""Every saved probability and result stays accessible in the history table."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import high_probability_archive as archive
import high_probability_engine as engine
import prediction_journal as journal
import probability_history_presentation as presentation


NOW = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)


def saved_match(identity='archive-1', *, status='settled', actual='1st'):
    return dict(id=identity, source='API-Football', source_kind='ranking_archive',
        fixture=dict(match_id='100', league='39', provider='api-football',
            home='Home', away='Away', timestamp='2026-10-27T14:00:00+00:00'),
        day='2026-10-27', competition='England · Premier League',
        created_at='2026-10-27T09:00:00+00:00', before='2026-10-27',
        probabilities={'1st': .6, 'Égal': .1, '2nd': .3}, favorite='1st',
        baseline={'1st': .3, 'Égal': .3, '2nd': .4}, status=status,
        actual=actual if status == 'settled' else None,
        ht='1-0' if status == 'settled' else None,
        ft='1-0' if status == 'settled' else None,
        model_version='clubs-saved-test', training=123, history_days=20,
        result_history=[], result_explanation={})


class ProbabilityHistoryTrendTests(unittest.TestCase):
    def test_positive_negative_and_zero_gaps_use_the_saved_baseline(self):
        row = saved_match()
        before = deepcopy(row)
        for outcome, expected in (('1st', (30, 30)), ('Égal', (30, -20)), ('2nd', (40, -10))):
            with self.subTest(outcome=outcome):
                baseline, gap = presentation.trend_values(row, outcome)
                self.assertAlmostEqual(baseline, expected[0])
                self.assertAlmostEqual(gap, expected[1])
        self.assertEqual(row, before)
        row['baseline'] = deepcopy(row['probabilities'])
        self.assertEqual(presentation.trend_values(row, '1st'), (60, 0))
        row['baseline'] = {'1st': 0, 'Égal': .6, '2nd': .4}
        self.assertEqual(presentation.trend_values(row, '1st'), (0, 60))

    def test_absent_or_invalid_baselines_remain_unavailable(self):
        row = saved_match()
        cases = [None, {}, [], {'1st': .3},
            {'1st': .3, 'Égal': .3, '2nd': .3},
            {'1st': math.nan, 'Égal': .3, '2nd': .7},
            {'1st': math.inf, 'Égal': 0, '2nd': 0},
            {'1st': True, 'Égal': 0, '2nd': 0},
            {'1st': -.1, 'Égal': .4, '2nd': .7},
            {'1st': '0.3', 'Égal': .3, '2nd': .4},
            {'1st': .3, 'Égal': .3, '2nd': .4, 'other': 0}]
        for baseline in cases:
            with self.subTest(baseline=baseline):
                row['baseline'] = baseline
                self.assertEqual(presentation.trend_values(row, '1st'), (None, None))
        row.pop('baseline')
        self.assertEqual(presentation.trend_values(row, '2nd'), (None, None))

    def test_verdict_distinguishes_a_past_pending_match_from_a_loss(self):
        row = saved_match(status='pending')
        self.assertEqual(presentation.verdict(row, '1st', NOW), ('Non évalué', 'orange'))
        row['fixture']['timestamp'] = '2026-10-29T14:00:00+00:00'
        self.assertEqual(presentation.verdict(row, '1st', NOW), ('En attente', 'blue'))
        row = saved_match()
        self.assertEqual(presentation.verdict(row, '1st', NOW), ('Réalisé', 'green'))
        self.assertEqual(presentation.verdict(row, '2nd', NOW), ('Non réalisé', 'red'))
        row['status'] = 'void'
        self.assertEqual(presentation.verdict(row, '1st', NOW), ('Annulé du suivi', 'gray'))

class ProbabilityHistoryPresentationTests(unittest.TestCase):
    def setUp(self):
        self.read_guards = []
        for target, name in ((api.ApiFootballClient, 'get'), (api, 'load_fixtures'),
                             (engine, 'scan'), (archive, 'capture'), (archive, 'reconcile'),
                             (journal, 'list_entries')):
            guard = patch.object(target, name, side_effect=AssertionError('Presentation must remain read-only'))
            self.read_guards.append(guard.start())
            self.addCleanup(guard.stop)

    def tearDown(self):
        for guard in self.read_guards:
            guard.assert_not_called()

    def open(self, rows, *, outcome='1st'):
        script = '''
import streamlit as st
from probability_history_presentation import render_match_table
render_match_table(st.session_state['test_rows'], st.session_state['test_outcome'], st.session_state['test_now'])
'''
        at = AppTest.from_string(script, default_timeout=10)
        at.session_state['test_rows'] = deepcopy(rows)
        at.session_state['test_outcome'] = outcome
        at.session_state['test_now'] = NOW
        at.run()
        self.assertFalse(at.exception)
        return at

    def test_table_shows_all_fields_without_actions_or_hidden_details(self):
        row = saved_match()
        at = self.open([row])
        table = at.dataframe[0].value
        self.assertEqual(list(table.columns), ['Match', 'Compétition', 'Coup d’envoi (Kinshasa)', 'Choix',
            'Probabilité (%)', 'Tendance (%)', 'Écart (points)', '1st (%)', 'Égal (%)', '2nd (%)',
            'Favori du modèle', 'Score à la pause', 'Score à 90 minutes', 'Résultat réel', 'Verdict',
            'Statut', 'Motif', 'Calcul avant match', 'Résultats historiques', 'Journées historiques',
            'Coupure historique UTC', 'Version du modèle', 'Provenance', 'Calculs conservés',
            'Corrections des résultats'])
        self.assertEqual(table['Match'].tolist(), ['Home — Away'])
        self.assertIn('Premier League', table['Compétition'].iloc[0])
        self.assertEqual(table['Coup d’envoi (Kinshasa)'].tolist(), ['27/10/2026 15:00'])
        self.assertEqual(table['Probabilité (%)'].tolist(), [60])
        self.assertEqual(table['Tendance (%)'].tolist(), [30])
        self.assertEqual(table['Écart (points)'].tolist(), [30])
        self.assertEqual(list(table['Verdict'].iloc[0]), ['Réalisé'])
        self.assertEqual(table[['1st (%)', 'Égal (%)', '2nd (%)']].iloc[0].tolist(), [60, 10, 30])
        self.assertEqual(table['Score à la pause'].tolist(), ['1-0'])
        self.assertEqual(table['Score à 90 minutes'].tolist(), ['1-0'])
        self.assertEqual(list(table['Résultat réel'].iloc[0]), ['1st'])
        self.assertEqual(table['Calcul avant match'].tolist(), ['27/10/2026 10:00'])
        self.assertEqual(table['Résultats historiques'].tolist(), [123])
        self.assertEqual(table['Journées historiques'].tolist(), [20])
        self.assertEqual(table['Provenance'].tolist(), ['Classement archivé'])
        self.assertEqual(len(at.dataframe), 1)
        self.assertFalse(at.metric)
        self.assertFalse(at.button)
        self.assertFalse(at.expander)
        config = json.loads(at.dataframe[0].proto.columns)
        self.assertFalse(any(value is None for value in config.values()))
        self.assertFalse(any(value.get('type_config', {}).get('type') == 'button' for value in config.values()))
        self.assertEqual(at.session_state['test_rows'], [row])

    def test_previous_calculations_and_corrections_remain_in_the_table(self):
        row = saved_match()
        old = deepcopy(row)
        old.update(created_at='2026-10-27T08:00:00Z', model_version='clubs-older',
                   probabilities={'1st': .2, 'Égal': .3, '2nd': .5})
        row['snapshots'] = [deepcopy(row), old]
        row['result_history'] = [{'revision': 1, 'actual': '2nd'}, {'revision': 2, 'actual': '1st'}]
        original = deepcopy(row)
        at = self.open([row])
        data = at.dataframe[0].value.iloc[0]
        for text in ('27/10/2026 10:00', '27/10/2026 09:00', '1st : 60.00 %', '1st : 20.00 %',
                     '2nd : 50.00 %', 'clubs-older', 'clubs-saved-test'):
            self.assertIn(text, data['Calculs conservés'])
        self.assertEqual(json.loads(data['Corrections des résultats']), row['result_history'])
        self.assertEqual(at.session_state['test_rows'], [original])
        self.assertFalse(at.button)
        self.assertFalse(at.expander)

    def test_outcome_colors_and_negative_zero_gaps_are_consistent(self):
        for outcome, gap in (('1st', 30), ('Égal', -20), ('2nd', -10)):
            with self.subTest(outcome=outcome):
                at = self.open([saved_match()], outcome=outcome)
                table = at.dataframe[0].value
                self.assertEqual(list(table['Choix'].iloc[0]), [outcome])
                self.assertAlmostEqual(table['Écart (points)'].iloc[0], gap)
                config = json.loads(at.dataframe[0].proto.columns)
                options = config['Choix']['type_config']['options']
                self.assertEqual([option['value'] for option in options], ['1st', 'Égal', '2nd'])
                colors = [option['color'] for option in options]
                self.assertEqual(len(colors), 3)
                self.assertEqual(len(set(colors)), 3)
                for label in ('Favori du modèle', 'Résultat réel'):
                    self.assertEqual(config[label]['type_config']['options'], options)
        row = saved_match()
        row['baseline'] = deepcopy(row['probabilities'])
        at = self.open([row])
        self.assertEqual(at.dataframe[0].value['Écart (points)'].iloc[0], 0)

    def test_missing_baseline_is_not_rebuilt_and_past_pending_is_not_a_loss(self):
        row = saved_match(status='pending')
        row.pop('baseline')
        at = self.open([row])
        table = at.dataframe[0].value
        self.assertTrue(table['Tendance (%)'].isna().all())
        self.assertTrue(table['Écart (points)'].isna().all())
        self.assertEqual(list(table['Verdict'].iloc[0]), ['Non évalué'])
        self.assertEqual(table['Score à la pause'].tolist(), ['Non reçu'])
        self.assertEqual(table['Score à 90 minutes'].tolist(), ['Non reçu'])
        self.assertEqual(list(table['Résultat réel'].iloc[0]), [])
        self.assertEqual(table[['1st (%)', 'Égal (%)', '2nd (%)']].iloc[0].tolist(), [60, 10, 30])
        self.assertFalse(at.button)

    def test_long_explanation_and_special_names_are_preserved_directly_in_the_table(self):
        row = saved_match(status='unresolved')
        row['fixture']['home'] = '[Home](https://example.test) **club** <script>alert(1)</script>'
        row['fixture']['away'] = 'Away_[U19]'
        reason = 'Changement [horaire] : '+('information à contrôler ; '*60)+'fin du motif.'
        row['result_explanation'] = dict(label='À vérifier', summary='Résumé', matches=[
            dict(match_id='100', label='Horaire à vérifier', reason=reason)])
        at = self.open([row])
        self.assertIn('[Home](https://example.test)', at.dataframe[0].value['Match'].iloc[0])
        table = at.dataframe[0].value
        self.assertEqual(table['Motif'].tolist(), [reason])
        self.assertEqual(table['Statut'].tolist(), ['Horaire à vérifier'])
        self.assertIn('Away_[U19]', table['Match'].iloc[0])
        self.assertFalse(any(node.proto.allow_html for node in at.markdown))
        self.assertEqual(at.session_state['test_rows'], [row])


if __name__ == '__main__':
    unittest.main()
