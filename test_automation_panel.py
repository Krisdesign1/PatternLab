"""Native page controls never start a worker or a request on render."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import automation_panel as panel
import scenario_workspace_panel as workspace


class AutomationPanelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = copy.deepcopy(panel.worker.DEFAULT_CONFIG)
        self.config.update(enabled=True, sizes=[2,4,6,8], interval_minutes=15,
                           history_seasons=[2022,2023,2024,2025,2026], history_enabled=True)
        self.patch(api, 'STORE_PATH', self.root/'fixtures.json')
        self.patch(workspace, '_snapshot', return_value=dict(competitions={}, fixtures=[], day_count=0,
                                                            usable_count=0, updated_at=None))
        self.network = self.patch(api.ApiFootballClient, 'get', side_effect=AssertionError('No automatic API calls'))
        self.patch(panel.worker, 'load_config', side_effect=lambda:copy.deepcopy(self.config))
        self.saver = self.patch(panel.worker, 'save_config', side_effect=self.save)
        self.patch(panel.worker, 'load_status', return_value={})
        self.service = self.patch(panel.service, 'service_status', return_value=dict(installed=False, running=False,
            label='org.patternlab.automation', interval_seconds=900, plist_path='/local/automation.plist'))
        self.coverage = self.patch(panel.worker, 'coverage', return_value=[
            dict(league='39', competition='Premier League', country='England', history_matches=800,
                 issues=0, fixtures=10, eligible_groups=3, ready_groups=3, pending_groups=0,
                 readiness='ready', readiness_label='Prête pour Clubs + répétitions',
                 ready_sizes=[2,4,6,8], missing_matches=0, reason='',
                 per_size=[dict(size=size, history_matches=800, history_groups=800//size, min_groups=20,
                                missing_groups=0, missing_matches=0, eligible=True, reason='') for size in [2,4,6,8]]),
            dict(league='61', competition='Ligue 1', country='France', history_matches=120,
                 issues=1, fixtures=8, eligible_groups=3, ready_groups=2, pending_groups=1,
                 readiness='partial', readiness_label='Prête pour certaines tailles', ready_sizes=[2,4,6],
                 missing_matches=40, reason='Compléter les groupes de 8 matchs.',
                 per_size=[dict(size=size, history_matches=120, history_groups=120//size, min_groups=20,
                                missing_groups=max(0,20-120//size), missing_matches=max(0,20*size-120),
                                eligible=size<8, reason='40 matchs supplémentaires pour une taille de 8.' if size==8 else '')
                           for size in [2,4,6,8]]),
            dict(league='777', competition='Petite division', country='Congo', history_matches=12,
                 issues=0, fixtures=2, eligible_groups=1, ready_groups=0, pending_groups=1,
                 readiness='insufficient', readiness_label='Historique statistique à compléter',
                 ready_sizes=[], missing_matches=28, reason='Historique statistique insuffisant.',
                 per_size=[dict(size=2, history_matches=12, history_groups=6, min_groups=20,
                                missing_groups=14, missing_matches=28, eligible=False, reason='28 matchs supplémentaires.')]),
            dict(league='888', competition='Autre division', country='Congo', history_matches=1000,
                 issues=0, fixtures=20, eligible_groups=0, ready_groups=0, pending_groups=0,
                 readiness='no_upcoming', readiness_label='Sans groupe à venir',
                 ready_sizes=[], missing_matches=0, per_size=[], reason='Aucun groupe futur complet.')])
        self.runner = self.patch(panel.worker, 'run_cycle', return_value=dict(status='completed', requests=3,
            created=2, already_recorded=1, history_imports=1, issues=[],
            jobs=[dict(league='777', competition='Petite division', size=2,
                       day=datetime.now(panel.ZoneInfo('Africa/Kinshasa')).date().isoformat(),
                       state='pending', reason='Historique insuffisant', methods=['frequency','clubs'],
                       unavailable={'catboost':'Aucun modèle disponible.',
                                    'context_neighbors':'Réglages par contexte absents.'})]))
        panel._coverage.clear()
        self.addCleanup(panel._coverage.clear)

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def save(self, candidate):
        self.config = copy.deepcopy(candidate)
        return copy.deepcopy(candidate)

    def open(self):
        at = AppTest.from_file('app.py', default_timeout=20).run()
        at.switch_page('ui_pages/automation.py').run()
        self.assertFalse(at.exception)
        return at

    def table(self, at):
        return next(item.value for item in at.dataframe if 'Résultats utilisables' in item.value.columns)

    def test_open_and_filters_are_local_and_include_every_competition(self):
        at = self.open()
        self.assertEqual(set(self.table(at)['ID compétition']), {'39','61','777','888'})
        self.assertEqual(set(self.table(at)['Compétition']), {'Premier League','Ligue 1','Petite division','Autre division'})
        self.assertTrue(any('n’est pas installé' in item.value for item in at.info))
        at.selectbox(key='automation_country').set_value('Congo').run()
        self.assertFalse(at.exception)
        self.assertEqual(list(self.table(at)['ID compétition']), ['777','888'])
        self.assertEqual(self.coverage.call_count, 1)
        self.runner.assert_not_called()
        self.saver.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(any(item.value=='Les compétitions du jour' for item in at.subheader))
        self.assertTrue(any('uniquement les matchs du jour à Kinshasa' in item.value for item in at.caption))

    def test_configuration_changes_only_save_on_submit_and_do_not_run(self):
        at = self.open()
        self.assertFalse(any('budget' in item.label.casefold() for item in at.number_input))
        self.assertTrue(any('Aucun plafond de requêtes' in item.value for item in at.caption))
        at.toggle(key='automation_history_enabled').set_value(False).run()
        self.saver.assert_not_called()
        at.multiselect(key='automation_sizes').set_value([3,5]).run()
        at.button(key='automation_save').click().run()
        self.assertFalse(at.exception)
        self.saver.assert_called_once()
        saved = self.saver.call_args.args[0]
        self.assertNotIn('daily_request_budget', saved)
        self.assertNotIn('cycle_request_budget', saved)
        self.assertEqual(saved['sizes'], [3,5])
        self.assertFalse(saved['history_enabled'])
        self.assertTrue(any('Réglages enregistrés' in item.value for item in at.success))
        self.runner.assert_not_called()
        self.network.assert_not_called()

    def test_manual_cycle_runs_once_and_rerun_does_not_repeat_it(self):
        at = self.open()
        self.runner.assert_not_called()
        at.button(key='automation_run').click().run()
        self.assertFalse(at.exception)
        self.runner.assert_called_once_with(config=self.config)
        jobs = next(item.value for item in at.dataframe if 'Méthodes figées' in item.value.columns)
        self.assertEqual(list(jobs['Compétition']), ['Petite division'])
        self.assertEqual(list(jobs['État']), ['En attente'])
        self.assertEqual(list(jobs['Méthodes figées']), ['Répétitions seules, Clubs seuls'])
        self.assertIn('CatBoost : Aucun modèle disponible.', jobs.iloc[0]['Méthodes indisponibles'])
        self.assertIn('Répétitions par contexte : Réglages par contexte absents.', jobs.iloc[0]['Méthodes indisponibles'])
        self.assertTrue(any(item.value.startswith('État : Terminé') for item in at.caption))
        self.assertTrue(any(item.label == 'Nouveaux groupes verrouillés' and item.value == '2' for item in at.metric))
        at.run()
        self.assertEqual(self.runner.call_count, 1)
        self.network.assert_not_called()

    def test_disabled_config_and_cycle_errors_remain_reviewable(self):
        self.config['enabled'] = False
        at = self.open()
        self.assertTrue(at.button(key='automation_run').disabled)
        self.runner.assert_not_called()
        at.toggle(key='automation_enabled').set_value(True).run()
        at.button(key='automation_save').click().run()
        self.runner.side_effect = panel.worker.AutomationError('Budget API atteint')
        at.button(key='automation_run').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Budget API atteint' in item.value for item in at.error))
        self.assertEqual(len(self.table(at)), 4)
        self.network.assert_not_called()

    def test_readiness_counts_filters_and_exact_size_specific_gaps(self):
        at = self.open()
        self.assertEqual(at.segmented_control(key='automation_readiness').value, 'all')
        for label in ('Prêtes pour Clubs + répétitions', 'Prêtes pour certaines tailles',
                      'Historique statistique à compléter', 'Sans groupe à venir'):
            self.assertTrue(any(item.label == label and item.value == '1' for item in at.metric))
        at.segmented_control(key='automation_readiness').set_value('partial').run()
        self.assertFalse(at.exception)
        self.assertEqual(list(self.table(at)['ID compétition']), ['61'])
        self.assertEqual(self.table(at).iloc[0]['Tailles prêtes'], '2, 4, 6')
        detail = next(item.value for item in at.dataframe if 'Groupes historiques' in item.value.columns)
        eight = detail.loc[detail['Matchs par groupe'] == 8].iloc[0]
        self.assertEqual(eight['Résultats antérieurs'], 120)
        self.assertEqual(eight['Groupes historiques'], 15)
        self.assertEqual(eight['Groupes manquants'], 5)
        self.assertEqual(eight['Matchs supplémentaires requis'], 40)
        self.assertFalse(eight['Clubs + répétitions calculable'])
        four = detail.loc[detail['Matchs par groupe'] == 4].iloc[0]
        self.assertTrue(four['Clubs + répétitions calculable'])
        self.assertEqual(four['Matchs supplémentaires requis'], 0)
        self.assertTrue(any('Ces seuils concernent le moteur Clubs + répétitions' in item.value for item in at.caption))
        self.assertTrue(at.get('page_link'))
        at.segmented_control(key='automation_readiness').set_value('insufficient').run()
        self.assertEqual(list(self.table(at)['ID compétition']), ['777'])
        self.assertEqual(int(self.table(at).iloc[0]['Matchs manquants au minimum']), 28)
        self.assertEqual(self.coverage.call_count, 1)
        self.runner.assert_not_called()
        self.network.assert_not_called()

    def test_readiness_filter_with_no_result_is_explicit_without_network(self):
        at = self.open()
        at.selectbox(key='automation_country').set_value('Congo').run()
        at.segmented_control(key='automation_readiness').set_value('ready').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucune compétition ne correspond' in item.value for item in at.info))
        self.assertEqual(self.coverage.call_count, 1)
        self.runner.assert_not_called()
        self.network.assert_not_called()

    def test_local_midnight_invalidates_coverage_cache_without_api_or_store_change(self):
        current=[datetime(2025,3,20,22,59,tzinfo=timezone.utc)]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)
        self.patch(panel,'datetime',Clock)
        at=self.open()
        self.assertEqual(self.coverage.call_count,1)
        at.run()
        self.assertEqual(self.coverage.call_count,1)
        current[0]+=timedelta(minutes=2)  # Same UTC day, next day in Kinshasa.
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(self.coverage.call_count,2)
        self.runner.assert_not_called();self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_last_report_hides_other_days_jobs_without_changing_saved_report(self):
        current=datetime(2025,3,20,23,5,tzinfo=timezone.utc)  # Already 21 March in Kinshasa.
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return current.astimezone(tz) if tz else current.replace(tzinfo=None)
        self.patch(panel,'datetime',Clock)
        report=copy.deepcopy(self.runner.return_value)
        base=report['jobs'][0]
        report['jobs']=[dict(base,day='2025-03-20',competition='Yesterday'),
                        dict(base,day='2025-03-21',competition='Today'),
                        dict(base,day='2025-03-22',competition='Tomorrow')]
        original=copy.deepcopy(report)
        self.patch(panel.worker,'load_status',return_value=report)
        at=self.open()
        table=next(item.value for item in at.dataframe if 'Méthodes figées' in item.value.columns)
        self.assertEqual(list(table['Compétition']),['Today'])
        self.assertTrue(any('2 groupe(s) d’autres jours' in item.value for item in at.caption))
        self.assertEqual(report,original)
        report['jobs']=[dict(base,day='2025-03-22',competition='Tomorrow')]
        at.run()
        self.assertFalse(at.exception)
        self.assertFalse(any('Méthodes figées' in item.value.columns for item in at.dataframe))
        self.assertTrue(any('Aucun groupe du jour dans ce dernier bilan' in item.value for item in at.info))
        self.runner.assert_not_called();self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
