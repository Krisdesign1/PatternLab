from test_table_view_support import html_nodes
import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
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
    def test_preparation_status_uses_latest_passage_for_selected_day(self):
        day = date(2026, 10, 21)
        status = dict(day=day.isoformat(), created=12, finished_at='2026-10-21T09:00:00Z')
        manual = dict(day=day.isoformat(), created=0, finished_at='2026-10-21T08:00:00Z')
        self.assertEqual(panel._latest_preparation(day, status, manual), status)
        manual['finished_at'] = '2026-10-21T10:00:00Z'
        self.assertEqual(panel._latest_preparation(day, status, manual), manual)
        self.assertEqual(panel._latest_preparation(date(2026,10,22), status, manual), {})

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

    def test_local_day_archives_and_future_views_keep_whole_groups(self):
        past=entry('past',day='2026-10-20');future=entry('future',day='2026-10-22')
        mixed=entry('mixed');mixed['fixtures'][-1]['timestamp']='2026-10-21T23:30:00Z'
        rows=data.project([past,future,mixed])
        self.assertEqual([row['id'] for row in data.on_day(rows,date(2026,10,20),today=date(2026,10,21))],['past'])
        self.assertNotIn('mixed',[row['id'] for row in rows])
        self.assertEqual([row['id'] for row in data.on_day(rows,date(2026,10,22),today=date(2026,10,21))],['future'])
        with self.assertRaises(ValueError):data.on_day(rows,'2026-10-22',today=date(2026,10,21))


class DailyPanelTests(unittest.TestCase):
    def test_only_opened_day_loads_explanations_and_export_is_deferred_complete(self):
        self.entries = [entry('today-small', size=2), entry('today-large', size=20),
                        entry('past', day='2026-10-20'), entry('future', day='2026-10-22')]
        original = copy.deepcopy(self.entries)
        explanation = dict(label='Résultat attendu', summary='Contrôle conservé', matches=[], blocking=[])
        calls = []
        def annotate(entries, **kwargs):
            entries = list(entries)
            calls.append([value['id'] for value in entries])
            return [dict(value, result_explanation=copy.deepcopy(explanation)) for value in entries]
        self.patch(panel.result_explanations, 'annotate_entries', side_effect=annotate)
        with patch.object(panel.st, 'download_button', wraps=panel.st.download_button) as downloads:
            at = self.open()
            self.assertEqual(len(calls), 1)
            self.assertEqual(set(calls[0]), {'today-small', 'today-large'})
            at.selectbox(key='daily_group_size').set_value(2).run()
            self.assertTrue(all(set(values) == {'today-small', 'today-large'} for values in calls))
            exported = next(call for call in reversed(downloads.call_args_list)
                            if call.kwargs.get('key') == 'daily_export')
            self.assertTrue(callable(exported.args[1]))
            self.assertEqual(exported.kwargs['on_click'], 'ignore')
            content = json.loads(exported.args[1]())
            self.assertEqual({value['id'] for value in content}, {'today-small', 'today-large'})
            for value in content:
                source = next(item for item in original if item['id'] == value['id'])
                self.assertEqual(value, json.loads(json.dumps(dict(source, result_explanation=explanation))))
            at.segmented_control(key='daily_view').set_value('history').run()
            self.assertEqual(calls[-1], ['past'])
        self.assertEqual(self.entries, original)
        self.network.assert_not_called()

    def setUp(self):
        admin = patch('admin_access.can_admin', return_value=True)
        admin.start(); self.addCleanup(admin.stop)
        self.entries=[]
        self.intents=[]
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fixture_path = Path(folder.name) / 'fixtures.json'
        self.patch(api, 'STORE_PATH', new=self.fixture_path)
        self.patch(workspace,'_snapshot',return_value=dict(competitions={},fixtures=[],day_count=0,usable_count=0,updated_at=None))
        self.patch(panel,'_now',return_value=NOW)
        local_date = patch('analysis_date.local_today', return_value=NOW.astimezone(panel.performance.LOCAL_ZONE).date())
        local_date.start(); self.addCleanup(local_date.stop)
        self.patch(journal,'list_entries',side_effect=lambda *args:self.entries)
        self.patch(panel.tracking,'reconcile_local',return_value={})
        self.patch(panel.worker,'load_selections',return_value=[])
        self.patch(panel.worker,'load_status',return_value={})
        self.patch(panel.intent_store,'list_intents',side_effect=lambda:self.intents)
        self.prepare=self.patch(panel.worker,'run_now',return_value=dict(day='2026-10-21',created=2,remaining=3,status='completed'))
        self.refresh=self.patch(panel.tracking,'refresh_entry',return_value={})
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No automatic API calls'))

    def patch(self,target,name,**kwargs):
        p=patch.object(target,name,**kwargs);result=p.start();self.addCleanup(p.stop);return result

    def open(self, day=None):
        at=AppTest.from_file('app.py',default_timeout=20)
        if day is not None:
            at.session_state['api_only_data_version'] = '2026-10-18-v1'
            at.session_state['analysis_selected_day'] = day
            at.session_state['analysis_follow_today'] = False
        at.run()
        at.switch_page('ui_pages/daily.py').run()
        self.assertFalse(at.exception)
        return at

    def test_future_selected_day_shows_saved_mixed_groups_details_and_explicit_preparation(self):
        tomorrow = date(2026,10,22)
        self.entries=[entry('today'),entry('tomorrow',day=tomorrow.isoformat())]
        before=copy.deepcopy(self.entries)
        at=self.open(day=tomorrow)
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_tomorrow'])
        self.prepare.assert_not_called(); self.network.assert_not_called()
        at.button(key='daily_prepare').click().run()
        self.assertFalse(at.exception)
        self.prepare.assert_called_once_with(day=tomorrow)
        at.button(key='daily_open_tomorrow').click().run()
        self.assertFalse(at.exception)
        table=next(frame.value for frame in at.dataframe if 'Compétition' in frame.value.columns)
        self.assertEqual(set(table['Compétition']),{'Premier League','Ligue 1'})
        self.assertEqual(self.entries,before)
        self.network.assert_not_called()

    def test_past_selected_day_is_consultable_but_cannot_prepare_retrospective_predictions(self):
        self.entries=[entry('past',day='2026-10-20')]
        at=self.open(day=date(2026,10,20))
        self.assertTrue(at.button(key='daily_prepare').disabled)
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_past'])
        self.prepare.assert_not_called(); self.network.assert_not_called()

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
        html='\n'.join(node.proto.body for node in html_nodes(at))
        self.assertIn('Cote cumulée',html)
        self.assertGreaterEqual(html.count('<th scope="row">'),3)
        self.assertEqual(self.entries,before)
        at.button(key='daily_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(len([button for button in at.button if button.label=='Voir l’analyse']),2)

    def test_exact_group_size_filter_covers_two_to_twenty_without_limiting_scenarios_or_pages(self):
        self.entries=[entry(f'size_{size}',size=size,support=90-size) for size in range(2,21)]
        original=copy.deepcopy(self.entries)
        at=self.open()
        self.assertEqual(len([button for button in at.button if button.label=='Voir l’analyse']),19)
        for size in (2,3,4,20):
            at.selectbox(key='daily_group_size').set_value(size).run()
            self.assertFalse(at.exception)
            self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],[f'daily_open_size_{size}'])
        at.button(key='daily_open_size_20').click().run()
        self.assertFalse(at.exception)
        frame=next(row.value for row in at.dataframe if 'Compétition' in row.value.columns)
        self.assertEqual(len(frame),20)
        at.button(key='daily_back').click().run()
        self.assertEqual(at.selectbox(key='daily_group_size').value,20)
        at.selectbox(key='daily_group_size').set_value(None).run()
        self.assertEqual(len([button for button in at.button if button.label=='Voir l’analyse']),19)
        self.assertEqual(self.entries,original)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_absent_group_size_stays_selected_with_empty_list_and_resets_pagination(self):
        self.entries=[entry(f'two_{i}',size=2,support=90-i) for i in range(21)]+[entry('three',size=3)]
        at=self.open()
        at.selectbox(key='daily_list_page_size').set_value(10).run()
        at.selectbox(key='daily_page_index').set_value(2).run()
        at.selectbox(key='daily_group_size').set_value(3).run()
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_three'])
        self.assertEqual(at.session_state['daily_preferences']['daily_page_index'],0)
        at.selectbox(key='daily_group_size').set_value(20).run()
        self.assertEqual(at.selectbox(key='daily_group_size').value,20)
        self.assertFalse(any(button.label=='Voir l’analyse' for button in at.button))
        self.assertTrue(any('Aucune analyse ne correspond' in row.value for row in at.info))
        at.selectbox(key='daily_group_size').set_value(None).run()
        self.assertEqual(at.selectbox(key='daily_page_index').value,0)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_blocking_cause_is_visible_in_list_and_match_table_without_changing_saved_forecast(self):
        self.entries = [entry('missing-halftime', status='unresolved', day='2026-10-20')]
        self.entries[0]['created_at'] = '2026-10-19T23:30:00+00:00'
        original = copy.deepcopy(self.entries)
        detail = dict(match_id='100', label='Score à la pause manquant',
            reason='Le score final est présent, mais le score à la pause manque.',
            state='missing_halftime', raw_status='FT')
        explanation = dict(label='Score à la pause manquant',
            summary='Club 0 — Visiteur 0 : score à la pause manquant.', matches=[detail], blocking=[detail])
        self.patch(panel.result_explanations, 'annotate_entries',
            side_effect=lambda entries, **kwargs: [dict(value, result_explanation=copy.deepcopy(explanation))
                                                  for value in entries])
        at = self.open()
        at.segmented_control(key='daily_view').set_value('history').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Club 0 — Visiteur 0' in node.value and 'score à la pause manquant' in node.value
                            for node in at.caption))
        self.assertEqual(at.selectbox(key='daily_state').value, None)
        at.button(key='daily_open_missing-halftime').click().run()
        self.assertFalse(at.exception)
        table = next(frame.value for frame in at.dataframe if 'Motif du suivi' in frame.value.columns)
        self.assertEqual(table['État du résultat'].iloc[0], 'Score à la pause manquant')
        self.assertEqual(table['Motif du suivi'].iloc[0], detail['reason'])
        self.assertEqual(self.entries, original)
        self.refresh.assert_not_called()
        self.network.assert_not_called()

    def test_eighty_five_analyses_have_two_pages_by_default_and_all_remain_accessible(self):
        self.entries=[entry(f'analysis_{i:03}',support=90-i) for i in range(85)]
        original=copy.deepcopy(self.entries)
        at=self.open()
        visible=lambda:[button.key for button in at.button if button.label=='Voir l’analyse']
        self.assertEqual(at.selectbox(key='daily_list_page_size').value,50)
        first=visible()
        self.assertEqual(len(first),50)
        self.assertTrue(any('1–50 sur 85 analyses' in item.value for item in at.caption))
        at.selectbox(key='daily_page_index').set_value(1).run()
        self.assertFalse(at.exception)
        last=visible()
        self.assertEqual(len(last),35)
        self.assertEqual(len(set(first+last)),85)
        at.selectbox(key='daily_list_page_size').set_value(10).run()
        self.assertEqual(at.selectbox(key='daily_page_index').value,0)
        at.selectbox(key='daily_page_index').set_value(8).run()
        self.assertFalse(at.exception)
        self.assertEqual(len(visible()),5)
        self.assertTrue(any('81–85 sur 85 analyses' in item.value for item in at.caption))
        self.assertEqual(self.entries,original)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_page_size_and_page_survive_detail_and_filter_shrink_returns_to_first_page(self):
        self.entries=[entry(f'analysis_{i:03}',support=90-i) for i in range(25)]
        at=self.open()
        at.selectbox(key='daily_list_page_size').set_value(10).run()
        at.selectbox(key='daily_page_index').set_value(2).run()
        chosen=next(button.key for button in at.button if button.label=='Voir l’analyse')
        at.button(key=chosen).click().run()
        at.button(key='daily_back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='daily_list_page_size').value,10)
        self.assertEqual(at.selectbox(key='daily_page_index').value,2)
        self.entries=self.entries[:12]
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='daily_page_index').value,0)
        self.assertEqual(len([button for button in at.button if button.label=='Voir l’analyse']),10)
        at.text_input(key='daily_search').set_value('club introuvable').run()
        self.assertFalse(at.exception)
        self.assertFalse(any(button.label=='Voir l’analyse' for button in at.button))
        self.assertEqual(at.selectbox(key='daily_list_page_size').value,10)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_invalid_pagination_state_is_reset_without_string_integer_error(self):
        self.entries=[entry(f'analysis_{i:03}',support=90-i) for i in range(21)]
        for bad in ('1 / 9',True,999):
            # Seed before creating the daily widgets: AppTest itself cannot
            # serialize an invalid value in an already rendered selectbox.
            at=AppTest.from_file('app.py',default_timeout=20).run()
            saved=dict(daily_context=('2026-10-21','today','2026-10-21'),
                daily_list_context=('2026-10-21',None,'all','',10),
                daily_list_page_size=10,daily_page_index=bad)
            for key,value in saved.items():
                at.session_state[key]=value
            at.switch_page('ui_pages/daily.py').run()
            self.assertFalse(at.exception)
            self.assertEqual(at.selectbox(key='daily_page_index').value,0)
        at=AppTest.from_file('app.py',default_timeout=20).run()
        at.session_state['daily_list_page_size']='10'
        at.switch_page('ui_pages/daily.py').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='daily_list_page_size').value,50)
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_probability_search_group_keeps_its_saved_percentages_in_history(self):
        saved = entry('probabilities')
        selection = dict(policy='high-probability-v1', reason='Classement par probabilité.', before='2026-10-21',
                         search={'minimum': .5}, match_probabilities=[
                             dict(match_id=fixture['match_id'], probabilities={'1st': .6, 'Égal': .1, '2nd': .3})
                             for fixture in saved['fixtures']])
        saved['metadata'].update(daily_policy='high-probability-v1', selection=selection)
        self.entries = [saved]
        at = self.open()
        at.button(key='daily_open_probabilities').click().run()
        self.assertFalse(at.exception)
        probabilities = next(frame.value for frame in at.dataframe if '1st' in frame.value.columns)
        self.assertEqual(probabilities['1st'].tolist(), [60.0]*3)
        self.assertFalse(any(metric.label == 'Appui des données à la sélection' for metric in at.metric))
        self.assertTrue(any('pourcentages ci-dessous sont ceux conservés avant match' in item.value for item in at.caption))
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_requested_scenario_has_its_own_verdict_even_when_outside_the_top_three(self):
        saved = entry('searched', status='settled')
        saved['methods']['clubs'] = independent([{'1st': .2, 'Égal': .1, '2nd': .7}]*3)
        saved['actual'] = ['1st']*3
        saved['metadata'].update(daily_policy='high-probability-v2', selection=dict(
            policy='high-probability-v2', searched_outcome='1st', search={'outcome': '1st', 'minimum': 0},
            requested_scenario={'sequence': ['1st']*3, 'probability': .2**3}))
        self.entries = [saved]
        at = self.open()
        self.assertTrue(any('Scénario recherché réalisé' in node.value for node in at.markdown))
        self.assertTrue(any('Aucun scénario proposé réalisé' in item.value for item in at.markdown))
        at.button(key='daily_open_searched').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(next(metric.value for metric in at.metric
                              if metric.label == 'Probabilité du scénario recherché avant match'), '0,80 %')
        self.prepare.assert_not_called();self.refresh.assert_not_called();self.network.assert_not_called()

    def test_two_custom_scenarios_share_one_forecast_and_have_their_own_detail_and_verdict(self):
        saved = entry('choices')
        for fixture in saved['fixtures']:
            fixture['league'] = '39'
        saved['id'] = journal._group_key(saved['fixtures'])
        saved['methods']['clubs'].update(source='API-Football', league='39', training_before='2026-10-21')
        with tempfile.TemporaryDirectory() as folder:
            for sequence in (['1st', 'Égal', '2nd'], ['2nd', '1st', 'Égal']):
                self.intents.append(panel.intent_store.record(saved, {'requested_scenario': {'sequence': sequence}},
                    path=Path(folder)/'scenarios.sqlite3', clock=lambda: NOW))
        self.entries = [saved]
        at = self.open()
        self.assertEqual(len([button for button in at.button if button.label == 'Voir l’analyse']), 2)
        at.button(key='daily_open_intent:'+self.intents[1]['id']).click().run()
        self.assertFalse(at.exception)
        table = next(frame.value for frame in at.dataframe if 'Résultat choisi' in frame.value.columns)
        self.assertEqual([list(value) for value in table['Résultat choisi']], [['2nd'], ['1st'], ['Égal']])
        self.assertTrue(table['Tendance du championnat (%)'].isna().all())
        saved['status'] = 'settled'
        saved['actual'] = ['2nd', '1st', 'Égal']
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Scénario recherché réalisé' in node.value for node in at.markdown))
        self.assertEqual(len(self.entries), 1)
        self.network.assert_not_called()

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
        html='\n'.join(node.proto.body for node in html_nodes(at))
        self.assertIn('aria-label="'+' → '.join(hit['actual'])+'"',html)
        self.assertIn('aria-label="'+' → '.join(miss['actual'])+'"',html)
        self.assertEqual(html.count('class="pl-scenario-sequence"'),2)
        self.assertTrue(any(item.value=='**Scénario réalisé :**' for item in at.markdown))
        self.assertTrue(any(item.value=='**Scénario observé :**' for item in at.markdown))
        from scenario_odds import scenario_odds_text
        for saved, label in ((hit,'réalisé'),(miss,'observé')):
            self.assertTrue(any(item.value == f'Cote cumulée indicative du scénario {label} : '
                                f'**{scenario_odds_text(saved["actual"])}** · Référence fixe manuelle'
                                for item in at.caption))
        at.selectbox(key='daily_verdict').set_value('hit').run()
        self.assertEqual([button.key for button in at.button if button.label=='Voir l’analyse'],['daily_open_hit'])
        html='\n'.join(node.proto.body for node in html_nodes(at))
        self.assertEqual(html.count('class="pl-scenario-sequence"'),1)
        at.button(key='daily_open_hit').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any(item.value=='Clubs · Choix 2' for item in at.caption))
        html='\n'.join(node.proto.body for node in html_nodes(at))
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
        html='\n'.join(node.proto.body for node in html_nodes(at))
        self.assertNotIn('<tr class="pl-scenario-realized">',html)
        at.button(key='daily_back').click().run()
        at.button(key='daily_open_damaged').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Comparaison indisponible' in item.value for item in at.warning))
        self.network.assert_not_called()


if __name__=='__main__':
    unittest.main()
