import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import catboost_scenarios as ml
import journal_panel as panel
import prediction_journal as journal
import prospective_models as models
import scenario_workspace_panel as workspace
from test_catboost_scenarios import fixtures, sample
from test_prediction_journal import BEFORE, raw_fixtures
from test_prospective_models import context_method


class JournalPanelTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.patch(journal,'DB_PATH',self.root/'journal.sqlite3')
        self.patch(workspace,'_snapshot',return_value=dict(competitions={},fixtures=[],day_count=0,usable_count=0,updated_at=None))
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No automatic API calls'))
        self.raw=[]
        self.patch(api,'load_fixtures',side_effect=lambda *args:self.raw)
        self.day=self.patch(panel,'_today',return_value=date(2025,3,20))
        self.patch(panel,'_now',side_effect=lambda: datetime.combine(self.day.return_value,datetime.min.time(),
                                                                  tzinfo=timezone.utc)+timedelta(hours=8))

    def patch(self,target,name,*args,**kwargs):
        p=patch.object(target,name,*args,**kwargs);v=p.start();self.addCleanup(p.stop);return v

    def open(self,request=None):
        at=AppTest.from_file('app.py',default_timeout=20).run()
        at.switch_page('ui_pages/journal.py')
        if request is not None:at.query_params['analysis']=request
        at.run()
        self.assertFalse(at.exception)
        return at

    def buttons(self,at):
        return [button for button in at.button if button.label=='Voir l’analyse']

    def view(self,at,identity=None,compare=False):
        button=at.button(key=f'journal_browser_open_{identity}') if identity else self.buttons(at)[0]
        button.click().run()
        self.assertFalse(at.exception)
        if compare:
            identity=at.session_state['journal_selected_id']
            at.toggle(key=f'journal_compare_{identity}').set_value(True).run()
            self.assertFalse(at.exception)
        return at

    def seed(self,catboost=False,offset=0,context=False,automatic=False,finished=True,created_at=BEFORE,
             day=None,size=4,league='39',competition='League',generation='test-generation'):
        chosen=fixtures()[:size]
        for row in chosen:
            row['match_id']=str(int(row['match_id'])+offset)
            row['league']=league
        if day is not None:
            for row in chosen:row['timestamp']=day.isoformat()+row['timestamp'][10:]
        with patch.object(ml,'load_report',return_value=None), patch.object(models,'_context_neighbors',
                side_effect=models.SnapshotError('Configuration par contexte absente.')):
            methods=models.build_methods(sample(league=league),chosen)['methods']
        if catboost:
            methods['catboost']=dict(copy.deepcopy(methods['clubs']),version='catboost-test',generation=generation)
        if context:methods['context_neighbors']=context_method()
        entry=journal.record(chosen,methods,now=created_at,raw_fixtures=raw_fixtures(chosen),
                       metadata={'competition':competition,'selected_engine':'automation' if automatic else 'statistical'})
        self.raw+=raw_fixtures(chosen,status='FT' if finished else 'NS')
        return entry

    def test_empty_page_never_backfills_historical_results_or_creates_storage(self):
        self.raw=raw_fixtures(status='FT')
        at=self.open()
        self.assertTrue(any('Aucune prévision avant match' in r.value for r in at.info))
        self.assertFalse(journal.DB_PATH.exists())
        self.network.assert_not_called()

    def test_all_sizes_and_competitions_visible_then_exact_three_match_detail(self):
        manual=self.seed(size=3)
        automatic=self.seed(offset=100,size=2,automatic=True,finished=False)
        other=self.seed(offset=200,league='61',competition='Other league')
        before={entry['id']:entry['methods'] for entry in journal.list_entries()}
        at=self.open()
        self.assertEqual({button.key for button in self.buttons(at)},
                         {f'journal_browser_open_{entry["id"]}' for entry in (manual,automatic,other)})
        self.assertFalse(any('Coup d’envoi (Kinshasa)' in frame.value.columns for frame in at.dataframe))
        at=self.view(at,manual['id'])
        self.assertEqual(at.session_state['journal_selected_id'],manual['id'])
        matches=next(frame.value for frame in at.dataframe if 'Coup d’envoi (Kinshasa)' in frame.value.columns)
        self.assertEqual(len(matches),3)
        self.assertEqual(list(matches['Domicile']),[row['home'] for row in manual['fixtures']])
        self.assertFalse(any('Groupes communs' in frame.value.columns for frame in at.dataframe))
        self.assertEqual({entry['id']:entry['methods'] for entry in journal.list_entries()},before)
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.buttons(at)),3)
        self.network.assert_not_called()

    def test_local_results_evaluated_and_comparison_uses_exact_common_groups(self):
        self.seed(catboost=True)
        self.seed(offset=100)
        at=self.view(self.open(),compare=True)
        table=next(d.value for d in at.dataframe if 'Groupes communs' in d.value.columns)
        self.assertEqual(set(table['Groupes communs']),{2})
        at.multiselect(key='journal_methods_39_4').set_value(['frequency','clubs','combined','catboost']).run()
        self.assertFalse(at.exception)
        table=next(d.value for d in at.dataframe if 'Groupes communs' in d.value.columns)
        self.assertEqual(len(table),4)
        self.assertEqual(set(table['Groupes communs']),{1})
        self.assertTrue(any('Scénario réalisé' in r.value for r in at.markdown))
        self.network.assert_not_called()

    def test_result_refresh_is_explicit_and_errors_keep_saved_forecast(self):
        entry=self.seed()
        refresh=self.patch(panel.tracking,'refresh_entry',side_effect=api.ApiFootballError('Quota atteint'))
        at=self.view(self.open(),entry['id'])
        refresh.assert_not_called()
        before=journal.list_entries()[0]['methods']
        at.button(key='journal_refresh').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Quota atteint' in r.value for r in at.error))
        self.assertEqual(journal.list_entries()[0]['methods'],before)
        self.assertTrue(at.get('download_button'))

    def test_context_method_label_and_comparison_preserve_legacy_cohort(self):
        self.seed(context=True)
        self.seed(offset=100)
        at=self.view(self.open(),compare=True)
        selector=at.multiselect(key='journal_methods_39_4')
        self.assertIn('Répétitions par contexte',selector.options)
        selector.set_value(['clubs','context_neighbors']).run()
        self.assertFalse(at.exception)
        table=next(d.value for d in at.dataframe if 'Groupes communs' in d.value.columns)
        self.assertEqual(set(table['Méthode']),{'Clubs seuls','Répétitions par contexte'})
        self.assertEqual(set(table['Groupes communs']),{1})
        self.assertEqual(len(journal.list_entries()),2)
        self.network.assert_not_called()

    def test_pending_groups_show_waiting_counts_without_zero_percent_results(self):
        self.seed(finished=False)
        self.seed(offset=100,automatic=True,finished=False)
        at=self.open()
        metrics={row.label:row.value for row in at.metric}
        self.assertEqual(metrics,{'Groupes évalués':'0','En attente des résultats':'2','Annulés du suivi':'0','À vérifier':'0'})
        at=self.view(at,compare=True)
        self.assertTrue(any('Aucun taux de réussite' in row.value for row in at.info))
        self.assertFalse(any('Groupes communs' in frame.value.columns for frame in at.dataframe))
        self.assertTrue(any('0/4 matchs terminés' in row.value for row in at.caption))
        self.network.assert_not_called()

    def test_origin_filter_limits_list_but_not_statistical_cohort(self):
        self.seed()
        automatic=self.seed(offset=100,automatic=True,finished=False)
        at=self.open()
        at.selectbox(key='journal_browser_origin').set_value('automatic').run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{automatic["id"]}'])
        at=self.view(at,automatic['id'],compare=True)
        self.assertTrue(any('Origine : Regroupement automatique' in row.value for row in at.caption))
        table=next(frame.value for frame in at.dataframe if 'Groupes communs' in frame.value.columns)
        self.assertEqual(set(table['Groupes communs']),{1})
        at.selectbox(key='journal_comparison_origin_39_4').set_value('automatic').run()
        self.assertFalse(at.exception)
        self.assertFalse(any('Groupes communs' in frame.value.columns for frame in at.dataframe))
        self.assertTrue(any('Aucun taux de réussite' in row.value for row in at.info))
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='journal_browser_origin').value,'automatic')
        self.assertEqual(len(self.buttons(at)),1)
        self.network.assert_not_called()

    def test_empty_filter_preserves_storage_and_export(self):
        self.seed()
        before=journal.list_entries()[0]['methods']
        at=self.open()
        at.selectbox(key='journal_browser_origin').set_value('automatic').run()
        self.assertFalse(at.exception)
        self.assertFalse(self.buttons(at))
        self.assertTrue(at.info)
        self.assertTrue(at.get('download_button'))
        self.assertEqual(journal.list_entries()[0]['methods'],before)
        self.network.assert_not_called()

    def test_four_statuses_and_partial_match_progress_are_distinct(self):
        self.seed()
        pending=self.seed(offset=100,finished=False)
        self.seed(offset=200)
        self.seed(offset=300)
        self.raw[4]['fixture']['status']['short']='FT'
        self.raw[8]['fixture']['status']['short']='CANC'
        self.raw[12]['score']['halftime']['home']=None
        at=self.open()
        metrics={row.label:row.value for row in at.metric}
        self.assertEqual(metrics,{'Groupes évalués':'1','En attente des résultats':'1','Annulés du suivi':'1','À vérifier':'1'})
        at=self.view(at,pending['id'],compare=True)
        table=next(frame.value for frame in at.dataframe if 'Groupes communs' in frame.value.columns)
        self.assertEqual(set(table['Groupes communs']),{1})
        self.assertTrue(any('1/4 matchs terminés' in row.value for row in at.caption))
        self.network.assert_not_called()

    def test_native_cumulative_chart_appears_only_for_multiple_prediction_days(self):
        self.seed(created_at=BEFORE-timedelta(days=1))
        self.seed(offset=100)
        at=self.view(self.open(),compare=True)
        self.assertTrue(any('Évolution sur les mêmes prévisions' == row.label for row in at.expander))
        trend=next(frame.value for frame in at.dataframe if 'Groupes communs cumulés' in frame.value.columns)
        self.assertEqual(set(trend['Groupes communs cumulés']),{1,2})
        self.assertTrue(at.get('vega_lite_chart'))
        self.network.assert_not_called()

    def test_day_filter_uses_match_date_and_keeps_future_and_history_in_storage(self):
        today=self.seed(created_at=BEFORE-timedelta(days=2))
        self.seed(offset=100,day=date(2025,3,21),finished=False)
        self.seed(offset=200,day=date(2025,3,19),created_at=BEFORE-timedelta(days=2))
        before={entry['id']:entry['methods'] for entry in journal.list_entries()}
        exported=self.patch(panel,'_export',wraps=panel._export)
        at=self.open()
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{today["id"]}'])
        self.assertTrue(any('1 prévision(s) pour les matchs d’aujourd’hui' in row.value for row in at.caption))
        self.assertEqual({entry['id']:entry['methods'] for entry in journal.list_entries()},before)
        self.assertEqual({entry['id'] for entry in exported.call_args.args[0]},set(before))
        self.assertTrue(at.get('download_button'))
        self.network.assert_not_called()

    def test_only_other_days_shows_empty_daily_view_and_preserves_export(self):
        self.seed(offset=100,day=date(2025,3,21),finished=False)
        before=journal.list_entries()[0]['methods']
        at=self.open()
        self.assertTrue(any('Aucune prévision enregistrée pour les matchs d’aujourd’hui' in row.value for row in at.info))
        self.assertFalse(at.selectbox)
        self.assertFalse(at.metric)
        self.assertFalse(at.dataframe)
        self.assertTrue(at.get('download_button'))
        self.assertEqual(journal.list_entries()[0]['methods'],before)
        self.network.assert_not_called()

    def test_midnight_closes_old_detail_and_lists_new_day_without_auto_selection(self):
        old=self.seed()
        new=self.seed(offset=100,day=date(2025,3,21),finished=False)
        at=self.view(self.open(),old['id'])
        self.day.return_value=date(2025,3,21)
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{new["id"]}'])
        self.assertFalse(any('Coup d’envoi (Kinshasa)' in frame.value.columns for frame in at.dataframe))
        self.assertTrue(any('Matchs du jour : 21/03/2025' in row.value for row in at.caption))
        self.network.assert_not_called()

    def test_midnight_during_local_reconciliation_renders_the_new_day(self):
        self.seed()
        new=self.seed(offset=100,day=date(2025,3,21),finished=False)
        original=panel.tracking.reconcile_local
        def cross_midnight():
            result=original()
            self.day.return_value=date(2025,3,21)
            return result
        self.patch(panel.tracking,'reconcile_local',side_effect=cross_midnight)
        at=self.open()
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{new["id"]}'])
        self.assertTrue(any('Matchs du jour : 21/03/2025' in row.value for row in at.caption))
        self.network.assert_not_called()

    def test_direct_link_opens_exact_group_even_with_an_old_browser_filter(self):
        target=self.seed(size=3)
        self.seed(offset=100,automatic=True,size=2)
        at=self.open()
        at.selectbox(key='journal_browser_size').set_value(2).run()
        self.assertEqual(len(self.buttons(at)),1)
        at.query_params['analysis']=target['id']
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['journal_selected_id'],target['id'])
        self.assertEqual(len(next(frame.value for frame in at.dataframe if 'Coup d’envoi (Kinshasa)' in frame.value.columns)),3)
        self.assertNotIn('analysis',at.query_params)
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='journal_browser_size').value,2)
        self.assertEqual(len(self.buttons(at)),1)
        self.network.assert_not_called()

    def test_unknown_or_other_day_link_returns_to_list_without_recomputing(self):
        current=self.seed()
        future=self.seed(offset=100,day=date(2025,3,21),finished=False)
        for identity in (future['id'],'f'*64):
            at=self.open(request=identity)
            self.assertTrue(any('Cette analyse n’est pas disponible' in row.value for row in at.info))
            self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{current["id"]}'])
            self.assertFalse(any('Coup d’envoi (Kinshasa)' in frame.value.columns for frame in at.dataframe))
        self.network.assert_not_called()

    def test_comparison_defaults_to_opened_generation_and_never_pools_sizes_or_leagues(self):
        target=self.seed(catboost=True,generation='generation-z')
        self.seed(offset=100,catboost=True,generation='generation-a')
        self.seed(offset=200,size=3)
        self.seed(offset=300,league='61')
        at=self.view(self.open(),target['id'],compare=True)
        at.multiselect(key='journal_methods_39_4').set_value(['frequency','clubs','combined','catboost']).run()
        self.assertFalse(at.exception)
        table=next(frame.value for frame in at.dataframe if 'Groupes communs' in frame.value.columns)
        self.assertEqual(set(table['Groupes communs']),{1})
        self.assertIn('generation-z',set(table['Version']))
        self.network.assert_not_called()

    def test_archived_evaluations_are_accessible_even_when_today_has_no_predictions(self):
        archived=self.seed(day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        before=journal.list_entries()[0]['methods']
        build=self.patch(models,'build_methods',side_effect=AssertionError('No recomputed prediction'))
        forecast=self.patch(ml,'forecast',side_effect=AssertionError('No current model inference'))
        refresh=self.patch(panel.tracking,'refresh_entry',side_effect=AssertionError('No automatic refresh'))
        at=self.open()
        self.assertEqual(at.segmented_control(key='journal_view').value,'today')
        self.assertFalse(self.buttons(at))
        at.segmented_control(key='journal_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='journal_history_date').value,date(2025,3,19))
        self.assertEqual([button.key for button in self.buttons(at)],
                         [f'journal_browser_open_{archived["id"]}'])
        self.assertEqual({row.label:row.value for row in at.metric}['Groupes évalués'],'1')
        self.assertTrue(at.get('download_button'))
        self.assertEqual(journal.list_entries()[0]['methods'],before)
        build.assert_not_called();forecast.assert_not_called();refresh.assert_not_called()
        self.network.assert_not_called()

    def test_history_date_options_are_past_days_descending_and_today_stays_the_default(self):
        current=self.seed()
        yesterday=self.seed(offset=100,day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        earlier=self.seed(offset=200,day=date(2025,3,18),created_at=BEFORE-timedelta(days=2))
        self.seed(offset=300,day=date(2025,3,21),finished=False)
        at=self.open()
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{current["id"]}'])
        at.segmented_control(key='journal_view').set_value('history').run()
        self.assertFalse(at.exception)
        dates=at.selectbox(key='journal_history_date')
        self.assertEqual(len(dates.options),2)
        self.assertIn('19/03/2025',dates.options[0]);self.assertIn('18/03/2025',dates.options[1])
        self.assertEqual(dates.value,date(2025,3,19))
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{yesterday["id"]}'])
        dates.set_value(date(2025,3,18)).run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{earlier["id"]}'])
        at.segmented_control(key='journal_view').set_value('today').run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{current["id"]}'])
        self.assertEqual(len(journal.list_entries()),4)
        self.network.assert_not_called()

    def test_archive_exact_three_match_detail_back_and_origin_filter_preserve_frozen_forecasts(self):
        target=self.seed(size=3,day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        self.seed(offset=100,size=2,automatic=True,day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        before={entry['id']:copy.deepcopy(entry['methods']) for entry in journal.list_entries()}
        at=self.open()
        at.segmented_control(key='journal_view').set_value('history').run()
        self.assertFalse(at.exception)
        at.selectbox(key='journal_browser_origin').set_value('manual').run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{target["id"]}'])
        at=self.view(at,target['id'])
        matches=next(frame.value for frame in at.dataframe if 'Coup d’envoi (Kinshasa)' in frame.value.columns)
        self.assertEqual(len(matches),3)
        self.assertEqual(list(matches['Domicile']),[fixture['home'] for fixture in target['fixtures']])
        self.assertTrue(all('19/03/2025' in stamp for stamp in matches['Coup d’envoi (Kinshasa)']))
        at.button(key='journal_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.segmented_control(key='journal_view').value,'history')
        self.assertEqual(at.selectbox(key='journal_history_date').value,date(2025,3,19))
        self.assertEqual(at.selectbox(key='journal_browser_origin').value,'manual')
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{target["id"]}'])
        self.assertEqual({entry['id']:entry['methods'] for entry in journal.list_entries()},before)
        self.network.assert_not_called()

    def test_archive_comparison_is_scoped_to_selected_day_competition_and_size(self):
        target=self.seed(size=3,day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        self.seed(offset=100,size=3,automatic=True,day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        self.seed(offset=200,size=2,day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        self.seed(offset=300,size=3,league='61',day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        self.seed(offset=400,size=3,day=date(2025,3,18),created_at=BEFORE-timedelta(days=2))
        self.seed(offset=500,size=3)
        at=self.open()
        at.segmented_control(key='journal_view').set_value('history').run()
        self.assertFalse(at.exception)
        at.selectbox(key='journal_browser_origin').set_value('manual').run()
        at=self.view(at,target['id'],compare=True)
        table=next(frame.value for frame in at.dataframe if 'Groupes communs' in frame.value.columns)
        self.assertEqual(set(table['Groupes communs']),{2})
        at.selectbox(key='journal_comparison_origin_39_3').set_value('automatic').run()
        self.assertFalse(at.exception)
        table=next(frame.value for frame in at.dataframe if 'Groupes communs' in frame.value.columns)
        self.assertEqual(set(table['Groupes communs']),{1})
        self.network.assert_not_called()

    def test_archive_result_refresh_remains_explicit_and_error_preserves_frozen_forecast(self):
        target=self.seed(day=date(2025,3,19),created_at=BEFORE-timedelta(days=1),finished=False)
        refresh=self.patch(panel.tracking,'refresh_entry',side_effect=api.ApiFootballError('Quota archive atteint'))
        before=journal.list_entries()[0]['methods']
        at=self.open()
        at.segmented_control(key='journal_view').set_value('history').run()
        at=self.view(at,target['id'])
        refresh.assert_not_called()
        self.assertTrue(any('0/4 matchs terminés' in caption.value for caption in at.caption))
        at.button(key='journal_refresh').click().run()
        self.assertFalse(at.exception)
        refresh.assert_called_once()
        self.assertTrue(any('Quota archive atteint' in item.value for item in at.error))
        self.assertEqual(journal.list_entries()[0]['methods'],before)
        self.network.assert_not_called()

    def test_archive_date_change_closes_detail_and_midnight_keeps_past_day_selected(self):
        target=self.seed(day=date(2025,3,19),created_at=BEFORE-timedelta(days=1))
        earlier=self.seed(offset=100,day=date(2025,3,18),created_at=BEFORE-timedelta(days=2))
        self.seed(offset=200)
        at=self.open()
        at.segmented_control(key='journal_view').set_value('history').run()
        at=self.view(at,target['id'])
        at.selectbox(key='journal_history_date').set_value(date(2025,3,18)).run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{earlier["id"]}'])
        self.assertFalse(any('Coup d’envoi (Kinshasa)' in frame.value.columns for frame in at.dataframe))
        self.day.return_value=date(2025,3,21)
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.segmented_control(key='journal_view').value,'history')
        self.assertEqual(at.selectbox(key='journal_history_date').value,date(2025,3,18))
        self.assertEqual([button.key for button in self.buttons(at)],[f'journal_browser_open_{earlier["id"]}'])
        self.network.assert_not_called()


if __name__=='__main__':unittest.main()
