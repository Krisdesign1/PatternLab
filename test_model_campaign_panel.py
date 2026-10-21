"""Campaign UI reads all local leagues and runs at most one explicit job."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import model_campaign_panel as panel
import model_training_panel as training
import scenario_workspace_panel as workspace


class CampaignPanelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.rows = [dict(league='39', provider='api-football', competition='Premier League', country='England')]
        self.raw = [dict(league=dict(id=777, name='Petite division'))]
        self.jobs = [self.job('39','Premier League','ready'),
                     self.job('61','Ligue 1','frozen', fingerprint='updated', frozen_fingerprint='first'),
                     self.job('777','Petite division','insufficient',history_matches=0)]
        self.report = dict(league='39', status='frozen', frozen_at='2026-10-18T12:00:00Z',cutoff='2026-10-18',
                           components={'catboost':dict(status='available',origin='adopted_existing_exploratory',generation='first')},
                           comparisons=dict(status='partial'))
        self.patch(api,'DATA_DIR',self.root)
        self.patch(api,'STORE_PATH',self.root/'fixtures.json')
        self.patch(api,'load_fixtures',return_value=self.raw)
        self.network = self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No API on campaign page'))
        self.patch(workspace,'_snapshot',return_value=dict(competitions={},fixtures=[],day_count=0,usable_count=0,updated_at=None))
        self.patch(training.sources,'selected_source',return_value='API-Football')
        self.patch(training,'_rows',return_value=(self.rows,{}))
        self.planner = self.patch(panel.campaign,'plan',side_effect=lambda *a,**k:copy.deepcopy(self.jobs))
        self.loader = self.patch(panel.campaign,'load_manifest',return_value=None)
        self.runner = self.patch(panel.campaign,'run_one',return_value=self.report)
        panel._plan.clear();self.addCleanup(panel._plan.clear)

    def job(self, league, competition, status, **extra):
        value = dict(league=league,competition=competition,status=status,history_matches=240,fingerprint='first',
                    frozen_fingerprint=None,upcoming_at=None,chronological_ready=status!='insufficient',
                    contextual_ready=False,reason='Saisons manquantes.' if status=='insufficient' else '')
        return dict(value, **extra)

    def patch(self,target,name,*args,**kwargs):
        patcher=patch.object(target,name,*args,**kwargs)
        result=patcher.start();self.addCleanup(patcher.stop);return result

    def open(self):
        at=AppTest.from_file('app.py',default_timeout=20).run()
        at.switch_page('ui_pages/models.py').run()
        self.assertFalse(at.exception)
        return at

    def table(self,at):
        return next(item.value for item in at.dataframe if 'Validation CatBoost possible' in item.value.columns)

    def test_default_campaign_includes_raw_only_and_does_not_train_or_fetch(self):
        at=self.open()
        self.assertEqual(at.segmented_control(key='model_validation_view').value,'campaign')
        self.assertEqual(set(self.table(at)['ID compétition']),{'39','61','777'})
        self.assertTrue(self.planner.call_args.kwargs['raw'] is self.raw)
        self.runner.assert_not_called();self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertTrue(any('test historique indépendant' in item.value for item in at.info))
        self.assertTrue(at.get('page_link'))

    def test_button_runs_exactly_one_competition_and_rerun_never_repeats(self):
        at=self.open()
        at.button(key='campaign_run').click().run()
        self.assertFalse(at.exception)
        self.runner.assert_called_once_with(self.rows,'39',sizes=(2,4,6,8))
        components=next(item.value for item in at.dataframe if 'Origine' in item.value.columns)
        self.assertIn('Artefact exploratoire existant conservé',list(components['Origine']))
        self.assertTrue(at.get('download_button'))
        at.run()
        self.assertEqual(self.runner.call_count,1)
        self.network.assert_not_called()

    def test_frozen_and_insufficient_are_not_launched_even_with_changed_history(self):
        at=self.open()
        at.selectbox(key='campaign_state').set_value('changed').run()
        self.assertFalse(at.exception)
        self.assertEqual(list(self.table(at)['ID compétition']),['61'])
        self.assertTrue(at.button(key='campaign_run').disabled)
        self.assertTrue(any('paramètres conservés' in item.value for item in at.warning))
        at.selectbox(key='campaign_state').set_value('insufficient').run()
        self.assertTrue(at.button(key='campaign_run').disabled)
        self.assertTrue(any('Saisons manquantes' in item.value for item in at.info))
        self.runner.assert_not_called();self.network.assert_not_called()

    def test_error_retains_plan_and_scheduler_read_is_local(self):
        (self.root/'model_campaign_status.json').write_text(json.dumps(dict(status='paused',issues=['Limite quotidienne atteinte'])))
        self.runner.side_effect=panel.campaign.CampaignError('Campagne verrouillée')
        at=self.open()
        self.assertTrue(any('Limite quotidienne' in item.value for item in at.warning))
        at.button(key='campaign_run').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Campagne verrouillée' in item.value for item in at.error))
        self.assertEqual(len(self.table(at)),3)
        self.network.assert_not_called()

    def test_new_results_and_corrected_initial_history_are_distinguished_without_retuning(self):
        self.jobs[1].update(history_state='new_results', new_results=True, changed_history=False,
                            context_frozen_unavailable=True)
        at=self.open()
        at.selectbox(key='campaign_state').set_value('frozen').run()
        self.assertFalse(at.exception)
        self.assertEqual(list(self.table(at)['Historique depuis le gel']),['Nouveaux résultats après le gel'])
        self.assertTrue(any('De nouveaux résultats sont arrivés' in item.value for item in at.info))
        self.assertTrue(any('Cette indisponibilité reste figée' in item.value for item in at.info))
        self.jobs[1].update(history_state='changed_history',new_results=False,changed_history=True)
        panel._plan.clear()
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(list(self.table(at)['Historique depuis le gel']),['Historique initial corrigé ou complété'])
        self.assertTrue(at.button(key='campaign_run').disabled)
        self.runner.assert_not_called();self.network.assert_not_called()


if __name__=='__main__':unittest.main()
