import copy
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import daily_evaluation_panel as panel
import daily_evaluation_store as store
import daily_analysis_worker as worker
import prediction_journal as journal
import prospective_models as models
import scenario_workspace_panel as workspace
from test_daily_analysis_panel import entry


def report():
    return dict(protocol=dict(start='2022-01-01',end_exclusive='2026-10-21'),
                inventory=dict(calendar_days=100,valid_past_results=2000,eligible_matches=300),
                paired_summary=dict(by_size=[dict(size=3,groups=50,days=20,weeks=6,
                    support_top1_rate=.08,reference_top1_rate=.1,support_top3_rate=.2,reference_top3_rate=.22,
                    delta_top1_pp=-2,delta_top3_pp=-2,interval_top3_pp={'lower':-5,'upper':1},
                    nll_identity=True)],protocol={},warnings=[]),
                paired_records=[dict(day='2026-01-01',size=3,groups=50,support_top3=10,reference_top3=11)],
                production_by_size=[dict(size=3,groups=60,evaluated=50,unavailable=5,missing_outcomes=5,
                    clubs_top3_rate=.2,baseline_top3_rate=.18,clubs_loss_per_match=1.05,baseline_loss_per_match=1.07)],
                support_bins=[dict(lower=65,upper=80,matches=300,missing_outcomes=5,clubs_loss=1.05,baseline_loss=1.07)],
                controlled_attrition={'day_sizes_missing_outcomes':5},warnings=[])


class DailyEvaluationPanelTests(unittest.TestCase):
    def setUp(self):
        self.value=report();self.entries=[]
        self.patch(workspace,'_snapshot',return_value=dict(competitions={},fixtures=[],day_count=0,usable_count=0,updated_at=None))
        self.load=self.patch(store,'load_latest',side_effect=lambda:self.value)
        self.read=self.patch(journal,'list_entries',side_effect=lambda:self.entries)
        self.patch(worker,'load_selections',return_value=[])
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No API'))
        self.reconcile=self.patch(journal,'reconcile',side_effect=AssertionError('No reconciliation'))
        self.build=self.patch(models,'build_mixed_methods',side_effect=AssertionError('No forecasting'))
        self.run=self.patch(store,'run_audit',side_effect=AssertionError('Read only'))

    def patch(self,target,name,**kwargs):
        obj=patch.object(target,name,**kwargs);value=obj.start();self.addCleanup(obj.stop);return value

    def open(self):
        at=AppTest.from_file('app.py',default_timeout=15).run()
        at.switch_page('ui_pages/daily_evaluation.py').run()
        self.assertFalse(at.exception)
        return at

    def assert_readonly(self):
        self.network.assert_not_called();self.reconcile.assert_not_called();self.build.assert_not_called();self.run.assert_not_called()

    def test_historical_comparison_and_distinct_baselines_render_without_calculation(self):
        before=copy.deepcopy(self.value)
        at=self.open()
        comparison=next(node.value for node in at.dataframe if 'Groupes par politique' in node.value.columns)
        self.assertEqual(list(comparison['Matchs par groupe']),[3])
        self.assertEqual(comparison['Exact dans les 3 · Appui (%)'].iloc[0],20)
        self.assertEqual(comparison['Exact dans les 3 · Chronologique (%)'].iloc[0],22)
        self.assertEqual(comparison['Intervalle exploratoire bas'].iloc[0],-5)
        self.assertTrue(any('même erreur probabiliste' in node.value for node in at.success))
        self.assertEqual(next(node.value for node in at.dataframe if 'Groupes construits' in node.value.columns)['Résultats manquants'].iloc[0],5)
        self.assertEqual(next(node.value for node in at.dataframe if 'Indice d’appui' in node.value.columns)['Matchs évaluables'].iloc[0],300)
        self.assertTrue(any('exploratoire' in node.value for node in at.info))
        self.assertEqual(self.value,before);self.assert_readonly()
        self.read.assert_not_called()

    def test_missing_report_still_allows_real_followup_without_replay_or_backfill(self):
        self.value=None;self.entries=[entry('waiting')]
        at=self.open()
        self.assertTrue(any('Aucun rapport' in node.value for node in at.info))
        at.segmented_control(key='daily_audit_view').set_value('prospective').run()
        self.assertFalse(at.exception)
        self.assertEqual(next(node.value for node in at.metric if node.label=='En attente'),'1')
        table=next(node.value for node in at.dataframe if 'Génération' in node.value.columns)
        self.assertTrue(table['Exact dans les 3 (%)'].isna().all())
        self.assert_readonly()

    def test_real_results_remain_separate_from_replay_and_pending_not_counted_as_failure(self):
        settled=entry('done',status='settled')
        settled['evaluations']={'clubs':dict(top1=False,top3=True,loss=2.4,probability=.09)}
        self.entries=[settled,entry('waiting')]
        before=copy.deepcopy(self.entries)
        at=self.open();at.segmented_control(key='daily_audit_view').set_value('prospective').run()
        self.assertFalse(at.exception)
        self.assertFalse(any('Groupes par politique' in node.value.columns for node in at.dataframe))
        table=next(node.value for node in at.dataframe if 'Génération' in node.value.columns)
        self.assertEqual(table['Groupes évalués'].iloc[0],1)
        self.assertEqual(table['En attente'].iloc[0],1)
        self.assertEqual(table['Exact dans les 3 (%)'].iloc[0],100)
        self.assertEqual(self.entries,before);self.assert_readonly()

    def test_corrupted_report_is_reported_without_invented_metrics(self):
        self.load.side_effect=ValueError('Rapport figé invalide')
        at=self.open()
        self.assertTrue(any('Rapport figé invalide' in node.value for node in at.error))
        self.assertFalse(at.dataframe);self.assert_readonly()


if __name__=='__main__':unittest.main()
