import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import saved_model_comparison_panel as panel
import saved_model_comparison as comparison
import test_saved_model_comparison as fixtures


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.SavedComparisonTests('test_all_methods_use_identical_matches_and_expected_losses')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)

    def test_three_methods_and_category_render_without_api_or_forecasting(self):
        result = comparison.build([fixtures.entry()],self.fixture.protocol)
        with patch.object(panel.journal_protocol,'load_protocol',return_value=self.fixture.protocol), \
                patch.object(panel,'_load',return_value=result), \
                patch('api_football.ApiFootballClient',side_effect=AssertionError('No API')), \
                patch('catboost_scenarios.forecast',side_effect=AssertionError('No inference')):
            app=AppTest.from_string('from saved_model_comparison_panel import _render_strict\n_render_strict()').run()
            self.assertFalse(app.exception)
            table=next(node.value for node in app.dataframe if 'Matchs identiques' in node.value.columns)
            self.assertEqual(list(table['Matchs identiques']),[2,2,2])
            app.segmented_control(key='saved_model_comparison_outcome').set_value('Égal').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe),2)
            self.assertFalse(app.button)

    def test_no_protocol_never_reads_or_writes_journal(self):
        with patch.object(panel.journal_protocol,'load_protocol',return_value=None), \
                patch.object(panel,'_load') as load:
            app=AppTest.from_string('from saved_model_comparison_panel import render\nrender()').run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Aucun protocole' in node.value for node in app.info))
        load.assert_not_called()

    def test_default_pairs_show_missing_evidence_and_calibration_without_model_calls(self):
        value=fixtures.entry(); value['methods'].pop('catboost')
        result=comparison.build_pairs([value],self.fixture.protocol)
        with patch.object(panel.journal_protocol,'load_protocol',return_value=self.fixture.protocol), \
                patch.object(panel,'_load_pairs',return_value=result), \
                patch('api_football.ApiFootballClient',side_effect=AssertionError('No API')), \
                patch('catboost_scenarios.forecast',side_effect=AssertionError('No inference')):
            app=AppTest.from_string('from saved_model_comparison_panel import render\nrender()').run()
            self.assertFalse(app.exception)
            self.assertEqual(app.segmented_control(key='saved_model_comparison_view').value,'pairs')
            table=next(node.value for node in app.dataframe if 'Matchs identiques' in node.value.columns)
            self.assertEqual(list(table['Matchs identiques']),[2,2])
            self.assertTrue(app.warning)
            self.assertFalse(any('Probabilité annoncée' in node.value.columns for node in app.dataframe))
            app.toggle(key='saved_pair_show_calibration').set_value(True).run()
            app.segmented_control(key='saved_pair_outcome').set_value('Égal').run()
            self.assertFalse(app.exception)
            calibration=next(node.value for node in app.dataframe if 'Probabilité annoncée' in node.value.columns)
            self.assertEqual(len(calibration),20)
            self.assertEqual(sum(calibration['Matchs évalués']),4)
            self.assertFalse(app.button)

    def test_default_cohort_uses_evidence_count_never_model_performance(self):
        from copy import deepcopy
        result=comparison.build_pairs([fixtures.entry()],self.fixture.protocol)
        original=next(row for row in result['cohorts'] if row['challenger']=='clubs')
        small=deepcopy(original); small.update(league='1',evaluated=2,dates=1)
        largest=deepcopy(original); largest.update(league='99',evaluated=30,dates=4)
        # Deliberately worse displayed scores must not push the largest cohort down.
        largest['scores']['clubs']['logloss']=9.0
        same_size=deepcopy(original); same_size.update(league='2',evaluated=30,dates=2)
        result['cohorts']=[small,same_size,largest]
        with patch.object(panel.journal_protocol,'load_protocol',return_value=self.fixture.protocol), \
                patch.object(panel,'_load_pairs',return_value=result):
            app=AppTest.from_string('from saved_model_comparison_panel import render\nrender()').run()
        self.assertFalse(app.exception)
        self.assertIn('Compétition 99',app.selectbox(key='saved_pair_cohort').options[0])
        self.assertIn('Compétition 2',app.selectbox(key='saved_pair_cohort').options[1])
        self.assertEqual(next(node.value for node in app.metric if node.label=='Matchs identiques évalués'),'30')
        self.assertIn('Comprendre ce bilan et ses exclusions',str(app.main))


if __name__=='__main__':
    unittest.main()
