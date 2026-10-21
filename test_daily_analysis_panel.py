import copy
from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import daily_analysis_data as data
import daily_analysis_panel as panel
import prediction_journal as journal
import scenario_workspace_panel as workspace
from test_scenario_tables import independent, positions


NOW = datetime(2026, 10, 21, 8, tzinfo=timezone.utc)


def entry(identifier='a', day='2026-10-21', status='pending', size=3, support=75):
    fixtures = [dict(match_id=str(100+i), league='39' if i % 2 == 0 else '61', provider='api-football',
                     home=f'Club {i}', away=f'Visiteur {i}', home_id=str(1000+i), away_id=str(2000+i),
                     timestamp=f'{day}T12:{i:02}:00', season=2026) for i in range(size)]
    method = independent((positions()*((size+1)//2))[:size])
    return dict(id=identifier, source='API-Football', group_kind='mixed', league='mixed', size=size,
                created_at='2026-10-20T23:30:00+00:00', fixtures=fixtures, methods={'clubs': method},
                metadata=dict(daily_policy='daily-v1', selection=dict(data_support_index=support, reason='Profils conservés.'),
                              competitions={'39': {'competition':'Premier League'},'61':{'competition':'Ligue 1'}}),
                status=status, actual=None, observed=[], evaluations={}, result_history=[], issues=[])


class DailyDataTests(unittest.TestCase):
    def test_saved_support_orders_across_competitions_sizes_without_using_odds_probabilities_or_results(self):
        low=entry('low', support=55, status='settled')
        high=entry('high', size=8, support=90)
        original=copy.deepcopy([low,high])
        rows=data.project([low,high])
        self.assertEqual([row['id'] for row in rows],['high','low'])
        self.assertEqual(rows[1]['status_label'],'Réalisé')
        self.assertEqual(rows[0]['status_label'],'En attente')
        self.assertEqual([low,high],original)
        self.assertEqual(data.competition_name(rows[0],'61'),'Ligue 1')

    def test_legacy_forecast_is_included_only_by_saved_selection_link(self):
        legacy=entry('legacy');legacy.pop('group_kind');legacy['league']='39'
        legacy['metadata'].pop('daily_policy');legacy['metadata'].pop('selection')
        self.assertEqual(data.project([legacy]),[])
        rows=data.project([legacy],[dict(entry_id='legacy', selection={'data_support_index':65},
                                       competitions={'39':{'competition':'Premier League'}})])
        self.assertEqual(rows[0]['id'],'legacy')
        self.assertEqual(rows[0]['support'],65)
        self.assertNotIn('daily_policy',legacy['metadata'])

    def test_local_day_archives_and_future_guard_keep_whole_groups(self):
        past=entry('past',day='2026-10-20');future=entry('future',day='2026-10-22')
        mixed=entry('mixed');mixed['fixtures'][-1]['timestamp']='2026-10-21T23:30:00Z'
        rows=data.project([past,future,mixed])
        self.assertEqual([row['id'] for row in data.on_day(rows,date(2026,10,20),today=date(2026,10,21))],['past'])
        self.assertNotIn('mixed',[row['id'] for row in rows])
        with self.assertRaises(ValueError):data.on_day(rows,date(2026,10,22),today=date(2026,10,21))


class DailyPanelTests(unittest.TestCase):
    def setUp(self):
        self.entries=[]
        self.patch(workspace,'_snapshot',return_value=dict(competitions={},fixtures=[],day_count=0,usable_count=0,updated_at=None))
        self.patch(panel,'_now',return_value=NOW)
        self.patch(journal,'list_entries',side_effect=lambda *args:self.entries)
        self.patch(panel.tracking,'reconcile_local',return_value={})
        self.patch(panel.worker,'load_selections',return_value=[])
        self.patch(panel.worker,'load_status',return_value={})
        self.prepare=self.patch(panel.worker,'run_now',return_value=dict(day='2026-10-21',created=2,remaining=3,status='completed'))
        self.refresh=self.patch(panel.tracking,'refresh_entry',return_value={})
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No automatic API calls'))

    def patch(self,target,name,**kwargs):
        p=patch.object(target,name,**kwargs);result=p.start();self.addCleanup(p.stop);return result

    def open(self):
        at=AppTest.from_file('app.py',default_timeout=20).run()
        at.switch_page('ui_pages/daily.py').run()
        self.assertFalse(at.exception)
        return at

    def test_all_groups_visible_and_detail_has_every_competition_and_multiple_scenarios(self):
        self.entries=[entry('three'),entry('eight',size=8,support=90)]
        before=copy.deepcopy(self.entries)
        at=self.open()
        self.assertEqual(len([button for button in at.button if button.label=='Voir l’analyse']),2)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()
        at.button(key='daily_open_three').click().run()
        self.assertFalse(at.exception)
        table=next(frame.value for frame in at.dataframe if 'Compétition' in frame.value.columns)
        self.assertEqual(len(table),3)
        self.assertEqual(set(table['Compétition']),{'Premier League','Ligue 1'})
        html='\n'.join(node.proto.body for node in at.get('html'))
        self.assertIn('Cote cumulée',html)
        self.assertGreaterEqual(html.count('<th scope="row">'),3)
        self.assertEqual(self.entries,before)
        at.button(key='daily_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len([button for button in at.button if button.label=='Voir l’analyse']),2)

    def test_history_is_read_only_and_realized_means_evaluated_not_a_success_claim(self):
        self.entries=[entry('past',day='2026-10-20',status='settled'),entry('today'),entry('future',day='2026-10-22')]
        at=self.open()
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_today'])
        at.segmented_control(key='daily_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='daily_history_date').value,date(2026,10,20))
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_past'])
        self.assertFalse(any(button.key=='daily_prepare' for button in at.button))
        self.assertEqual(next(metric.value for metric in at.metric if metric.label=='Réalisées'),'1')
        self.assertTrue(any('La réussite des scénarios est indiquée séparément' in item.value for item in at.caption))
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_prepare_and_refresh_require_explicit_clicks(self):
        self.entries=[entry()]
        at=self.open()
        at.button(key='daily_prepare').click().run()
        self.assertFalse(at.exception)
        self.prepare.assert_called_once()
        at.button(key='daily_open_a').click().run()
        at.button(key='daily_refresh').click().run()
        self.assertFalse(at.exception)
        self.refresh.assert_called_once_with(self.entries[0])
        self.network.assert_not_called()

    def test_state_filter_and_day_switch_clear_previous_detail_without_future_entries(self):
        self.entries=[entry('waiting'),entry('done',status='settled'),entry('past',day='2026-10-20')]
        at=self.open()
        at.selectbox(key='daily_state').set_value('settled').run()
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_done'])
        at.button(key='daily_open_done').click().run()
        at.segmented_control(key='daily_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertNotIn('daily_selected',at.session_state)
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_past'])

    def test_exact_verdict_is_visible_in_list_and_detail_with_saved_method_rank(self):
        hit=entry('hit',status='settled')
        hit['actual']=hit['methods']['clubs']['ranking'][1]['sequence'][:]
        hit['evaluations']={'clubs':{'top1':True,'top3':False,'probability':.1}}
        miss=entry('miss',status='settled');miss['actual']=['Égal']*3
        waiting=entry('waiting');waiting['actual']=hit['actual'][:]
        self.entries=[hit,miss,waiting]
        original=copy.deepcopy(self.entries)
        at=self.open()
        text='\n'.join(item.value for item in at.markdown)
        self.assertIn('Scénario réalisé',text)
        self.assertIn('Aucun scénario proposé réalisé',text)
        self.assertIn('Scénarios en attente',text)
        self.assertTrue(any(item.value=='Clubs · Choix 2' for item in at.caption))
        at.selectbox(key='daily_verdict').set_value('hit').run()
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_hit'])
        at.button(key='daily_open_hit').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any(item.value=='Clubs · Choix 2' for item in at.caption))
        html='\n'.join(node.proto.body for node in at.get('html'))
        self.assertTrue(any(item.value=='**Scénario réalisé :**' for item in at.markdown))
        self.assertIn('<tr class="pl-scenario-realized">',html)
        evaluation=next(frame.value for frame in at.dataframe if 'Premier choix exact' in frame.value.columns)
        self.assertFalse(bool(evaluation.iloc[0]['Premier choix exact']))
        self.assertTrue(bool(evaluation.iloc[0]['Scénario exact dans les 3 choix']))
        at.button(key='daily_back').click().run()
        self.assertEqual(at.selectbox(key='daily_verdict').value,'hit')
        self.assertEqual(self.entries,original)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_past_verdicts_available_and_pending_never_inherits_stale_success(self):
        past=entry('past',day='2026-10-20',status='settled')
        past['actual']=past['methods']['clubs']['ranking'][0]['sequence'][:]
        waiting=entry('waiting');waiting['actual']=past['actual'][:]
        waiting['evaluations']={'clubs':{'top1':True,'top3':True}}
        self.entries=[past,waiting]
        at=self.open()
        at.selectbox(key='daily_verdict').set_value('hit').run()
        self.assertFalse(any(button.label=='Voir l’analyse' for button in at.button))
        at.segmented_control(key='daily_view').set_value('history').run()
        self.assertEqual(at.selectbox(key='daily_verdict').value,'all')
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_past'])
        self.assertTrue(any('Scénario réalisé' in item.value for item in at.markdown))
        self.network.assert_not_called()

    def test_detail_cannot_show_stale_success_evaluations_and_damaged_choices_stay_unavailable(self):
        waiting=entry('waiting')
        waiting['actual']=waiting['methods']['clubs']['ranking'][0]['sequence'][:]
        waiting['evaluations']={'clubs':{'top1':True,'top3':True,'probability':.9}}
        damaged=entry('damaged',status='settled');damaged['actual']=['2nd']*3
        damaged['methods']['clubs']['ranking']=None
        self.entries=[waiting,damaged]
        at=self.open()
        self.assertTrue(any('Vérification des scénarios indisponible' in item.value for item in at.markdown))
        at.button(key='daily_open_waiting').click().run()
        self.assertFalse(at.exception)
        self.assertFalse(any('Premier choix exact' in frame.value.columns for frame in at.dataframe))
        html='\n'.join(node.proto.body for node in at.get('html'))
        self.assertNotIn('<tr class="pl-scenario-realized">',html)
        at.button(key='daily_back').click().run()
        at.button(key='daily_open_damaged').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Comparaison indisponible' in item.value for item in at.warning))
        self.network.assert_not_called()


if __name__=='__main__':
    unittest.main()
