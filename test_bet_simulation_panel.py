"""Only explicit user choices become persistent FC paper bets."""
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import bet_simulation as simulation
import bet_simulation_panel as panel
import manual_bet_store as portfolio
from test_scenario_tables import independent, positions

NOW=datetime(2026,10,28,10,tzinfo=timezone.utc)


def entry(identifier='settled',day='2026-10-27',status='settled',size=2):
    methods={'clubs':independent((positions() * 10)[:size])}
    return dict(id=identifier,source='API-Football',league='39',size=size,
        created_at='2026-10-26T08:00:00+00:00',
        fixtures=[dict(match_id=str(1000+index),league='39',home_id=str(10+index),away_id=str(20+index),
            provider='api-football',home=f'Home {index}',away=f'Away {index}',
            timestamp=day+f'T12:{index:02}:00') for index in range(size)],
        methods=methods,metadata={},status=status,
        checked_at='2026-10-27T18:00:00+00:00' if status=='settled' else None,
        actual=methods['clubs']['ranking'][1]['sequence'][:] if status=='settled' else None,
        observed=[],evaluations={},issues=[])


def script():
    from bet_simulation_panel import render_simulation
    render_simulation()


class BetSimulationPanelTests(unittest.TestCase):
    def setUp(self):
        self.entries=[entry(),entry('waiting',day='2026-10-28',status='pending')]
        for index,fixture in enumerate(self.entries[1]['fixtures']):
            fixture['match_id']=str(2000+index)
        self.original=deepcopy(self.entries)
        self.patches={}
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'manual.sqlite3'
        for target,kwargs in (
            ('manual_bet_store.DEFAULT_PATH',dict(new=self.path)),
            ('bet_simulation_panel._now',dict(return_value=NOW)),
            ('analysis_date.local_today',dict(return_value=date(2026,10,28))),
            ('admin_access.can_owner',dict(return_value=True)),
            ('admin_access.can_admin',dict(return_value=False)),
            ('bet_simulation_panel.journal.DB_PATH',dict(new=Path(folder.name)/'journal.sqlite3')),
            ('bet_simulation_panel.journal.list_entries',dict(side_effect=lambda *args:self.entries)),
            ('bet_simulation_panel.intents.list_intents',dict(return_value=[])),
            ('api_football.ApiFootballClient.get',dict(side_effect=AssertionError('No API'))),
            ('prediction_journal.record',dict(side_effect=AssertionError('No forecast write'))),
            ('prospective_models.build_methods',dict(side_effect=AssertionError('No inference')))):
            p=patch(target,**kwargs);self.patches[target]=p.start();self.addCleanup(p.stop)
        panel._load.clear();self.addCleanup(panel._load.clear)
        panel._catalog.clear();self.addCleanup(panel._catalog.clear)

    def open(self,day=date(2026,10,27)):
        at=AppTest.from_function(script,default_timeout=15)
        at.session_state['analysis_selected_day']=day
        at.session_state['analysis_follow_today']=False
        at.run();self.assertFalse(at.exception)
        return at

    def catalog(self,day='2026-10-27'):
        return [row for row in simulation.build(self.entries,now=NOW)['tickets'] if row['day']==day]

    def frame(self,at,marker):
        return next(node.value for node in at.dataframe if marker in node.value.columns)

    def submit(self,at,label):
        next(button for button in at.button if button.label==label).click().run()
        self.assertFalse(at.exception)

    def add(self,at,identity,stake):
        at.segmented_control(key='bet_manual_view').set_value('add').run()
        at.selectbox(key='bet_pick_scenario').set_value(identity).run()
        at.number_input(key='bet_manual_add_stake').set_value(stake)
        self.submit(at,'Ajouter à mes paris')

    def metric(self,at,label):
        return next(node.value for node in at.metric if node.label==label)

    def test_initial_portfolio_stays_empty_despite_all_saved_proposals(self):
        at=self.open()
        self.assertEqual(self.metric(at,'Paris choisis · journée'),'0')
        self.assertEqual(self.metric(at,'Mises choisies (FC)'),'0,00 FC')
        self.assertFalse(at.dataframe)
        self.assertFalse(self.path.exists())
        at.segmented_control(key='bet_manual_view').set_value('add').run()
        self.assertFalse(at.exception)
        available=self.frame(at,'N°')
        self.assertEqual(len(available),3)
        self.assertNotIn('Mise (FC)',available.columns)
        self.assertIsNone(at.selectbox(key='bet_pick_scenario').value)
        self.assertTrue(next(b.disabled for b in at.button if b.label=='Ajouter à mes paris'))
        self.assertFalse(self.path.exists())
        at.run();self.assertFalse(self.path.exists())

    def test_catalog_size_filter_keeps_exact_groups_and_empty_sizes_available(self):
        self.entries=[entry(f'group-{size}',size=size) for size in (2,3,4,20)]
        before=deepcopy(self.entries)
        at=self.open()
        self.assertEqual(len(at.selectbox(key='bet_personal_group_size').options),20)
        at.segmented_control(key='bet_manual_view').set_value('add').run()
        self.assertEqual(at.selectbox(key='bet_available_group_size').options,
                         ['Toutes les tailles',*[f'{size} matchs' for size in range(2,21)]])
        for size in (2,3,4,20):
            with self.subTest(size=size):
                at.selectbox(key='bet_available_group_size').set_value(size).run()
                self.assertFalse(at.exception)
                table=self.frame(at,'N°')
                self.assertEqual(set(table['Matchs']),{size})
                self.assertTrue(all(len(sequence)==size for sequence in table['Scénario']))
        at.selectbox(key='bet_available_group_size').set_value(5).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.dataframe)
        self.assertTrue(any('cette taille de groupe' in node.value for node in at.info))
        self.assertEqual(len(at.selectbox(key='bet_available_group_size').options),20)
        self.assertFalse(self.path.exists())
        at.selectbox(key='bet_available_group_size').set_value(None).run()
        self.assertEqual(set(self.frame(at,'N°')['Matchs']),{2,3,4,20})
        self.assertEqual(self.entries,before)
        for target in ('api_football.ApiFootballClient.get','prediction_journal.record','prospective_models.build_methods'):
            self.patches[target].assert_not_called()

    def test_portfolio_size_filter_scopes_table_and_management_without_changing_finances(self):
        self.entries=[entry(f'group-{size}',size=size) for size in (2,3,4,20)]
        choices={row['size']:row for row in self.catalog()}
        for size,row in choices.items():
            portfolio.add(row,size*100,now=NOW)
        saved=portfolio.list_bets()
        at=self.open()
        metrics={node.label:node.value for node in at.metric}
        for size in (2,3,4,20):
            with self.subTest(size=size):
                at.selectbox(key='bet_personal_group_size').set_value(size).run()
                self.assertFalse(at.exception)
                self.assertEqual(set(self.frame(at,'Matchs dans l’ordre')['Matchs']),{size})
                self.assertEqual(len(at.selectbox(key='bet_manage_id').options),1)
                self.assertIsNone(at.selectbox(key='bet_manage_id').value)
                self.assertEqual({node.label:node.value for node in at.metric},metrics)
        at.selectbox(key='bet_manage_id').set_value(choices[20]['id']).run()
        at.selectbox(key='bet_personal_group_size').set_value(5).run()
        self.assertFalse(at.exception)
        self.assertFalse(any(node.key=='bet_tickets' for node in at.dataframe))
        self.assertFalse(any(node.key=='bet_manage_id' for node in at.selectbox))
        self.assertTrue(any(node.proto.label=='Exporter mes paris de cette journée'
                            for node in at.get('download_button')))
        self.assertTrue(any('toutes tailles comprises' in node.value for node in at.caption))
        self.assertEqual(portfolio.list_bets(),saved)

    def test_catalog_size_change_resets_single_and_bulk_choices(self):
        self.entries=[entry(f'group-{size}',size=size) for size in (2,3)]
        choices={row['size']:row for row in self.catalog()}
        at=self.open();at.segmented_control(key='bet_manual_view').set_value('add').run()
        at.selectbox(key='bet_available_group_size').set_value(2).run()
        at.selectbox(key='bet_pick_scenario').set_value(choices[2]['id']).run()
        at.toggle(key='bet_add_all').set_value(True).run()
        at.selectbox(key='bet_available_group_size').set_value(3).run()
        self.assertFalse(at.exception)
        self.assertFalse(at.toggle(key='bet_add_all').value)
        self.assertIsNone(at.selectbox(key='bet_pick_scenario').value)
        self.assertTrue(next(b.disabled for b in at.button if b.label=='Ajouter à mes paris'))
        self.assertFalse(self.path.exists())

    def test_size_change_resets_catalog_and_portfolio_pagination(self):
        self.entries=[entry(f'group-{size}-{index}',size=size)
                      for size in (2,3) for index in range(20)]
        for index,value in enumerate(self.entries):
            for position,fixture in enumerate(value['fixtures']):
                fixture['match_id']=str(5000+10*index+position)
        at=self.open();at.segmented_control(key='bet_manual_view').set_value('add').run()
        at.selectbox(key='bet_available_page').set_value(1).run()
        at.selectbox(key='bet_available_group_size').set_value(3).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='bet_available_page').value,0)
        self.assertEqual(set(self.frame(at,'N°')['Matchs']),{3})
        self.assertFalse(self.path.exists())
        portfolio.add_many(self.catalog(),300,now=NOW)
        at.segmented_control(key='bet_manual_view').set_value('portfolio').run()
        at.selectbox(key='bet_personal_page').set_value(1).run()
        saved=portfolio.list_bets()
        at.selectbox(key='bet_personal_group_size').set_value(3).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox(key='bet_personal_page').value,0)
        self.assertEqual(set(self.frame(at,'Matchs dans l’ordre')['Matchs']),{3})
        self.assertEqual(portfolio.list_bets(),saved)

    def test_select_all_adds_only_size_matching_available_scenarios_across_pages(self):
        self.entries=[entry(f'group-{index}',size=3) for index in range(20)]
        for index,value in enumerate(self.entries):
            for position,fixture in enumerate(value['fixtures']):
                fixture['match_id']=str(5000+10*index+position)
        self.entries.extend(entry(f'other-size-{size}',size=size) for size in (2,4,20))
        self.entries.append(entry('tomorrow',day='2026-10-29',status='pending',size=3))
        existing=next(row for row in self.catalog() if row['size']==3)
        portfolio.add(existing,100,now=NOW)
        at=self.open();at.segmented_control(key='bet_manual_view').set_value('add').run()
        at.selectbox(key='bet_available_group_size').set_value(3).run()
        self.assertEqual(len(self.frame(at,'N°')),50)
        at.selectbox(key='bet_available_page').set_value(1).run()
        at.toggle(key='bet_add_all').set_value(True).run()
        self.assertIn('taille : 3 matchs',at.toggle(key='bet_add_all').label)
        at.text_input(key='bet_available_query').set_value('no such club').run()
        self.assertTrue(any('59 scénarios sélectionnés' in node.value for node in at.caption))
        self.assertEqual(len(portfolio.list_bets()),1)
        self.submit(at,'Ajouter les 59 scénarios à mes paris')
        saved=portfolio.list_bets()
        self.assertEqual(len(saved),60)
        self.assertEqual({row['ticket']['size'] for row in saved},{3})
        self.assertEqual({row['ticket']['day'] for row in saved},{'2026-10-27'})
        self.assertEqual(next(row['stake'] for row in saved if row['id']==existing['id']),100)
        self.assertTrue(all(row['stake']==300 for row in saved if row['id']!=existing['id']))

    def test_add_is_explicit_and_only_chosen_scenario_has_a_stake(self):
        at=self.open()
        chosen=self.catalog()[0]
        self.add(at,chosen['id'],300)
        self.assertEqual(len(portfolio.list_bets()),1)
        at.segmented_control(key='bet_manual_view').set_value('portfolio').run()
        self.assertEqual(self.metric(at,'Paris choisis · journée'),'1')
        self.assertEqual(self.metric(at,'Mises choisies (FC)'),'300,00 FC')
        table=self.frame(at,'Matchs dans l’ordre')
        self.assertEqual(len(table),1)
        self.assertEqual(float(table.iloc[0]['Mise (FC)']),300)
        self.assertIn('Rétrospectif',table.iloc[0]['Type de simulation'])
        self.assertEqual(self.entries,self.original)
        for target in ('api_football.ApiFootballClient.get','prediction_journal.record','prospective_models.build_methods'):
            self.patches[target].assert_not_called()

    def test_choices_and_individual_stakes_survive_new_browser_session(self):
        first,second=self.catalog()[:2]
        at=self.open();self.add(at,first['id'],100);self.add(at,second['id'],500)
        another=self.open()
        self.assertEqual(self.metric(another,'Paris choisis · journée'),'2')
        self.assertEqual(self.metric(another,'Mises choisies (FC)'),'600,00 FC')
        table=self.frame(another,'Matchs dans l’ordre')
        self.assertEqual(set(table['Mise (FC)']),{100,500})
        self.assertEqual(len(portfolio.list_bets()),2)

    def test_update_and_remove_apply_only_to_selected_manual_bet(self):
        first,second=self.catalog()[:2]
        portfolio.add(first,100,now=NOW);portfolio.add(second,500,now=NOW)
        at=self.open()
        at.selectbox(key='bet_manage_id').set_value(first['id']).run()
        at.number_input(key='bet_edit_stake_'+first['id']).set_value(200)
        self.submit(at,'Enregistrer la mise')
        self.assertEqual({r['id']:r['stake'] for r in portfolio.list_bets()},
                         {first['id']:200,second['id']:500})
        self.submit(at,'Retirer ce pari de ma simulation')
        self.assertEqual([r['id'] for r in portfolio.list_bets()],[second['id']])
        self.assertEqual(self.metric(at,'Paris choisis · journée'),'1')
        self.assertEqual(self.metric(at,'Mises choisies (FC)'),'500,00 FC')

    def test_pending_chosen_bets_have_blank_returns_and_day_selection_scopes_metrics(self):
        chosen=self.catalog('2026-10-28')[0]
        portfolio.add(chosen,300,now=NOW)
        at=self.open(date(2026,10,28))
        table=self.frame(at,'Matchs dans l’ordre')
        self.assertTrue(table['Retour réglé (FC)'].isna().all())
        self.assertTrue(table['Bénéfice / perte (FC)'].isna().all())
        self.assertEqual(table.iloc[0]['Type de simulation'],'Choisi avant match')
        self.assertEqual(self.metric(at,'Mises sans verdict (FC)'),'300,00 FC')
        at.session_state['analysis_selected_day']=date(2026,10,27)
        at.run();self.assertFalse(at.exception)
        self.assertEqual(self.metric(at,'Paris choisis · journée'),'0')
        self.assertEqual(self.metric(at,'Mises choisies (FC)'),'0,00 FC')
        history=self.frame(at,'Journée')
        self.assertEqual(len(history),1)
        self.assertEqual(int(history.iloc[0]['Paris simulés']),1)

    def test_filters_do_not_erase_chosen_bets_or_hide_export(self):
        chosen=self.catalog()[0];portfolio.add(chosen,300,now=NOW)
        at=self.open();at.text_input(key='bet_query').set_value('no such club').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Aucun de tes paris' in n.value for n in at.info))
        self.assertTrue(any(n.proto.label=='Exporter mes paris de cette journée' for n in at.get('download_button')))
        self.assertEqual(len(portfolio.list_bets()),1)
        self.assertEqual(self.metric(at,'Mises choisies (FC)'),'300,00 FC')

    def test_readonly_visitor_can_browse_but_not_save_a_choice(self):
        self.patches['admin_access.can_owner'].return_value=False
        at=self.open();at.segmented_control(key='bet_manual_view').set_value('add').run()
        at.selectbox(key='bet_pick_scenario').set_value(self.catalog()[0]['id']).run()
        self.assertTrue(next(b.disabled for b in at.button if b.label=='Ajouter à mes paris'))
        at.toggle(key='bet_add_all').set_value(True).run()
        self.assertFalse(at.exception)
        self.assertTrue(next(b.disabled for b in at.button if b.label=='Ajouter les 3 scénarios à mes paris'))
        self.assertFalse(self.path.exists())

    def test_owner_can_manage_paper_bets_while_shared_administration_is_locked(self):
        self.assertFalse(self.patches['admin_access.can_admin']())
        first=self.catalog()[0]
        at=self.open()
        self.add(at,first['id'],300)
        at.segmented_control(key='bet_manual_view').set_value('portfolio').run()
        at.selectbox(key='bet_manage_id').set_value(first['id']).run()
        at.number_input(key='bet_edit_stake_'+first['id']).set_value(500)
        self.submit(at,'Enregistrer la mise')
        self.assertEqual(portfolio.list_bets()[0]['stake'],500)
        self.submit(at,'Retirer ce pari de ma simulation')
        self.assertEqual(portfolio.list_bets(),[])
        self.assertEqual(self.entries,self.original)
        self.patches['prediction_journal.record'].assert_not_called()
        self.patches['api_football.ApiFootballClient.get'].assert_not_called()

    def test_owner_revoked_after_display_cannot_add_even_with_a_submitted_event(self):
        at=self.open()
        at.segmented_control(key='bet_manual_view').set_value('add').run()
        at.selectbox(key='bet_pick_scenario').set_value(self.catalog()[0]['id']).run()
        with patch.object(panel.bet_access,'render_notice'), \
                patch.object(panel.bet_access,'can_edit',side_effect=[True,False]):
            self.submit(at,'Ajouter à mes paris')
        self.assertTrue(at.warning)
        self.assertFalse(self.path.exists())

    def test_visitor_cannot_change_or_delete_a_previously_saved_bet(self):
        chosen=self.catalog()[0]
        portfolio.add(chosen,300,now=NOW)
        self.patches['admin_access.can_owner'].return_value=False
        at=self.open()
        at.selectbox(key='bet_manage_id').set_value(chosen['id']).run()
        for label in ('Enregistrer la mise','Retirer ce pari de ma simulation'):
            self.assertTrue(next(b.disabled for b in at.button if b.label==label))
        self.assertEqual(portfolio.list_bets()[0]['stake'],300)

    def test_missing_custom_choices_does_not_create_any_manual_bets(self):
        with patch.object(panel.intents,'list_intents',side_effect=ValueError('invalid')):
            at=self.open()
        self.assertTrue(any('personnalisés' in n.value for n in at.warning))
        self.assertEqual(self.metric(at,'Paris choisis · journée'),'0')
        self.assertFalse(self.path.exists())

    def test_select_all_includes_other_pages_and_ignores_display_search_without_repricing_owned_bets(self):
        base=entry()
        self.entries=[]
        for index in range(20):
            value=deepcopy(base);value['id']=f'group-{index}'
            for position,fixture in enumerate(value['fixtures']):
                fixture['match_id']=str(5000+10*index+position)
            self.entries.append(value)
        # A different day's proposals are never part of this bulk choice.
        self.entries.append(entry('tomorrow',day='2026-10-29',status='pending'))
        existing=self.catalog()[0]
        portfolio.add(existing,100,now=NOW)
        at=self.open();at.segmented_control(key='bet_manual_view').set_value('add').run()
        self.assertEqual(len(self.frame(at,'N°')),50)
        at.toggle(key='bet_add_all').set_value(True).run()
        self.assertFalse(at.exception)
        self.assertEqual(len(portfolio.list_bets()),1)
        at.text_input(key='bet_available_query').set_value('no such club').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('59 scénarios sélectionnés' in node.value for node in at.caption))
        self.submit(at,'Ajouter les 59 scénarios à mes paris')
        saved=portfolio.list_bets()
        self.assertEqual(len(saved),60)
        self.assertEqual(next(row['stake'] for row in saved if row['id']==existing['id']),100)
        self.assertTrue(all(row['stake']==300 for row in saved if row['id']!=existing['id']))
        self.assertEqual({row['ticket']['day'] for row in saved},{'2026-10-27'})
        at.segmented_control(key='bet_manual_view').set_value('portfolio').run()
        self.assertEqual(self.metric(at,'Paris choisis · journée'),'60')
        self.assertEqual(self.metric(at,'Mises choisies (FC)'),'17\u202f800,00 FC')

    def test_select_all_is_explicit_resets_on_day_change_and_shows_fc_total(self):
        at=self.open();at.segmented_control(key='bet_manual_view').set_value('add').run()
        self.assertFalse(at.toggle(key='bet_add_all').value)
        at.toggle(key='bet_add_all').set_value(True).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('3 scénario(s) × 300,00 FC = 900,00 FC' in node.value for node in at.markdown))
        self.assertFalse(self.path.exists())
        at.session_state['analysis_selected_day']=date(2026,10,28)
        at.run();self.assertFalse(at.exception)
        self.assertFalse(at.toggle(key='bet_add_all').value)
        self.assertIsNone(at.selectbox(key='bet_pick_scenario').value)
        self.assertFalse(self.path.exists())


if __name__=='__main__':
    unittest.main()
