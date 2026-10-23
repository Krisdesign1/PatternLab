"""The selection-study page reads frozen evidence without evaluating models."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import catboost_scenarios as ml
import daily_selection_statistics as statistics
import daily_selection_study as study
import prediction_journal as journal
import prospective_models as models
import scenario_workspace_panel as workspace
from test_daily_selection_statistics import observations


def report():
    records = observations(five=True) + observations(cohort='mixed', size=3)
    result = statistics.analyze(records)
    return dict(run_id='a' * 64, created_at='2026-10-22T08:00:00+00:00',
                protocol=dict(periods={
                    'train': dict(start='2022-01-01', end_exclusive='2025-01-01'),
                    'calibration': dict(start='2025-01-01', end_exclusive='2025-07-01'),
                    'selection': dict(start='2025-07-01', end_exclusive='2026-01-01'),
                    'test': dict(start='2026-01-01', end_exclusive='2026-10-22')},
                    exploratory=True, prior_history_explored=True, production_changed=False),
                inventory=dict(valid_past_results=39700, known_leagues=444, modeled_leagues=13),
                statistics=result, records=records, method_details=[{'catboost_available': True}],
                warnings=['Historique déjà examiné.'])


class DailySelectionStudyPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = report()

    def setUp(self):
        self.value = deepcopy(self.fixture)
        self.patch(workspace, '_snapshot', return_value=dict(competitions={}, fixtures=[], day_count=0,
                                                           usable_count=0, updated_at=None))
        self.load = self.patch(study, 'load_latest', side_effect=lambda: self.value)
        self.run = self.patch(study, 'run_study', side_effect=AssertionError('No calculation'))
        self.fit = self.patch(ml, '_fit', side_effect=AssertionError('No training'))
        self.select = self.patch(statistics, 'fit_rules', side_effect=AssertionError('No rule selection'))
        self.evaluate = self.patch(statistics, 'evaluate_rules', side_effect=AssertionError('No test evaluation'))
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.reconcile = self.patch(journal, 'reconcile', side_effect=AssertionError('No journal write'))
        self.forecast = self.patch(models, 'build_mixed_methods', side_effect=AssertionError('No forecasts'))

    def patch(self, target, name, **kwargs):
        obj = patch.object(target, name, **kwargs)
        value = obj.start()
        self.addCleanup(obj.stop)
        return value

    def open(self):
        at = AppTest.from_file('app.py', default_timeout=20).run()
        at.switch_page('ui_pages/selection_study.py').run()
        self.assertFalse(at.exception)
        return at

    def readonly(self):
        for mock in (self.run, self.fit, self.select, self.evaluate, self.network, self.reconcile, self.forecast):
            mock.assert_not_called()

    def table(self, at, column):
        return next(node.value for node in at.dataframe if column in node.value.columns)

    def test_frozen_rule_and_raw_calibrated_confidence_display_on_common_cohort(self):
        before = deepcopy(self.value)
        at = self.open()
        self.assertEqual(next(node.value for node in at.metric if node.label == 'Compétitions dans les groupes'), '2')
        self.assertEqual(next(node.value for node in at.metric if node.label == 'CatBoost ajustés sur apprentissage'), '1')
        self.assertEqual(self.table(at, 'Au, exclu')['Du'].tolist(),
                         ['2022-01-01', '2025-01-01', '2025-07-01', '2026-01-01'])
        overview = self.table(at, 'Groupes communs au test')
        self.assertEqual(overview['Matchs par groupe'].tolist(), [2])
        self.assertEqual(overview['Famille'].tolist(), ['Cinq méthodes'])
        evidence = self.table(at, 'Confiance calibrée du Top 3 (%)')
        self.assertEqual(evidence['Population'].iloc[0], 'Règle sélectionnée')
        self.assertGreater(evidence['Groupes évalués'].iloc[0], 0)
        self.assertIn('Probabilité brute des 3 (%)', evidence.columns)
        self.assertTrue(any('déjà été examinées' in node.value for node in at.info))
        self.assertEqual(self.value, before)
        self.readonly()

    def test_mixed_population_and_reference_remain_separate(self):
        at = self.open()
        at.segmented_control(key='selection_study_cohort').set_value('mixed').run()
        self.assertFalse(at.exception)
        self.assertEqual(self.table(at, 'Groupes communs au test')['Matchs par groupe'].tolist(), [3])
        self.assertEqual(self.table(at, 'Groupes communs au test')['Famille'].tolist(), ['Clubs et référence'])
        methods = [node.value for node in at.dataframe if 'Population' in node.value.columns and len(node.value) == 2]
        self.assertTrue(any(set(frame['Population']) == {'Clubs', 'Tendance de chaque compétition'} for frame in methods))
        self.assertTrue(any('comparaison distincte' in node.value for node in at.caption))
        self.readonly()

    def test_insufficient_calibration_abstains_without_fake_test_rates(self):
        row = next(row for row in self.value['statistics']['by_size'] if row['cohort'] == 'mono' and row['size'] == 2)
        row.update(status='insufficient_calibration', rule=None, selection=None, test=None, calibration={})
        row['family'] = dict(name=None, methods=[], chosen_from='calibration', attempts=[])
        self.value['statistics']['by_size'] = [row]
        at = self.open()
        overview = self.table(at, 'Groupes communs au test')
        self.assertEqual(overview['Décision'].tolist(), ['Abstention'])
        self.assertTrue(overview['Exact dans les 3 (%)'].isna().all())
        self.assertFalse(any('Population' in node.value.columns for node in at.dataframe))
        self.assertTrue(any('Abstention' in node.value for node in at.warning))
        self.assertTrue(any('100 groupes' in node.value for node in at.caption))
        self.readonly()

    def test_no_completed_test_keeps_rates_missing_instead_of_counting_failures(self):
        row = next(row for row in self.value['statistics']['by_size'] if row['cohort'] == 'mono' and row['size'] == 2)
        row['status'] = 'awaiting_test'
        for label in ('selected', 'unfiltered', 'baseline_on_selected'):
            block = row['test'][label]
            block['sample'] = dict(groups=0, days=0, unique_matches=0, zero_selection_days=20)
            block['metrics'] = dict(evaluated_groups=0, top1_rate=None, top3_rate=None,
                                    raw_top3_event={}, calibrated_top3_event={})
        row['test']['selected']['coverage'] = None
        row['test']['common'] = dict(groups=0, complete_groups=0)
        self.value['statistics']['by_size'] = [row]
        at = self.open()
        table = self.table(at, 'Confiance calibrée du Top 3 (%)')
        self.assertTrue(table['Exact dans les 3 (%)'].isna().all())
        self.assertTrue(any('taux de réussite restent indisponibles' in node.value for node in at.info))
        self.readonly()

    def test_test_table_uses_evaluated_fixture_counts_and_retains_pre_result_coverage(self):
        row = next(row for row in self.value['statistics']['by_size'] if row['cohort'] == 'mono' and row['size'] == 2)
        test = row['test']
        selected = test['selected']
        selected['sample'] = dict(groups=343, days=90, unique_matches=686, zero_selection_days=20)
        selected['evaluated_sample'] = dict(groups=338, days=88, unique_matches=676, zero_selection_days=22)
        selected['missing_outcomes'] = 5
        selected['metrics']['evaluated_groups'] = 338
        test['baseline_on_selected']['sample'] = dict(groups=338, days=88, unique_matches=676)
        test['baseline_on_selected']['metrics']['evaluated_groups'] = 338
        self.value['statistics']['by_size'] = [row]
        at = self.open()
        table = self.table(at, 'Confiance calibrée du Top 3 (%)')
        self.assertEqual(table['Groupes évalués'].iloc[0], 338)
        self.assertEqual(table['Matchs distincts'].iloc[0], 676)
        self.assertEqual(table['Journées'].iloc[0], 88)
        self.assertEqual(table['Matchs distincts'].iloc[2], 676)
        self.assertEqual(table['Journées'].iloc[2], 88)
        self.assertTrue(any('343' in item.value and 'Résultats incomplets parmi eux : 5' in item.value for item in at.caption))
        methods = next(node.value for node in at.dataframe
                       if 'Population' in node.value.columns and len(node.value) == 5)
        self.assertNotIn('Matchs distincts', methods.columns)
        self.assertNotIn('Journées', methods.columns)
        self.readonly()

    def test_absent_or_invalid_report_never_launches_the_study(self):
        self.value = None
        at = self.open()
        self.assertFalse(at.dataframe)
        self.assertTrue(any('Aucun comparatif' in node.value for node in at.info))
        self.assertEqual(at.code[0].value, 'python scripts/evaluate_selection_policy.py')
        self.load.side_effect = study.StudyError('Rapport figé invalide')
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Rapport figé invalide' in node.value for node in at.error))
        self.readonly()


if __name__ == '__main__':
    unittest.main()
