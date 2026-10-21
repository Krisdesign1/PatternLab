"""Exercise the journal list without APIs, predictions, or journal writes."""
import copy
from datetime import date
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football
import prediction_journal
import prospective_models
import workspace_sources


DAY = date(2026, 10, 20)


def row(number, *, league='39', size=2, origin='manual', status='pending'):
    names = [f'Club{number} Home {index} — Club{number} Away {index}' for index in range(size)]
    observed = size if status == 'settled' else 1 if status == 'in_progress' else 0
    return dict(id=f'full-shared-prefix-{number:064d}', entry_id=f'full-shared-prefix-{number:064d}',
                league=league, competition='Premier League' if league == '39' else 'Ligue 1',
                country='England' if league == '39' else 'France', size=size, origin=origin, status=status,
                state=status, journal_status='pending', status_label={
                    'pending':'En attente', 'in_progress':'Résultats partiels', 'settled':'Évalué',
                    'awaiting_results':'Résultats attendus'}[status],
                created_at=f'2026-10-20T08:{number % 60:02d}:00+00:00',
                created_local=f'20/10 09:{number % 60:02d}', kickoff_range='14:00–18:00',
                fixtures=[dict(match_id=str(number*100+index), home=f'Club{number} Home {index}',
                               away=f'Club{number} Away {index}', timestamp='2026-10-20T13:00:00Z')
                          for index in range(size)],
                clubs=[f'Club{number} Home {index}' for index in range(size)], match_names=names,
                observed_matches=observed, expected_matches=size, progress=observed, total=size,
                primary_method='combined', primary_method_name='Clubs + répétitions',
                primary_sequence=(['1st','2nd','Égal'] * ((size+2)//3))[:size], primary_probability=.1234)


def source(rows):
    return ('import json\nfrom datetime import date\nimport streamlit as st\nimport journal_browser\n'
            f'st.session_state.setdefault("rows",json.loads({json.dumps(json.dumps(rows))}))\n'
            'st.session_state.setdefault("day","2026-10-20")\n'
            'if "requested_page" in st.session_state:\n'
            '    st.session_state["list_page"]=st.session_state.pop("requested_page")\n'
            'st.session_state["opened"]=journal_browser.render(st.session_state["rows"],'
            'date.fromisoformat(st.session_state["day"]),key="list")\n')


class JournalBrowserTests(unittest.TestCase):
    def setUp(self):
        self.forbidden = []
        for target, name in ((api_football.ApiFootballClient,'get'), (prediction_journal,'record'),
                             (prospective_models,'build_methods'), (workspace_sources,'refresh_day')):
            patcher = patch.object(target,name,side_effect=AssertionError('The list is read-only'))
            self.forbidden.append(patcher.start())
            self.addCleanup(patcher.stop)

    def open(self, rows):
        at = AppTest.from_string(source(rows),default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def entries(self, at):
        return [button for button in at.button if button.key.startswith('list_open_')]

    def assert_readonly(self):
        for call in self.forbidden:
            call.assert_not_called()

    def test_all_competitions_sizes_and_origins_are_visible_without_auto_selection(self):
        rows = [row(1), row(2,league='61',size=4,origin='automatic',status='in_progress'),
                row(3,size=6,status='settled')]
        at = self.open(rows)
        self.assertEqual(len(self.entries(at)),3)
        self.assertIsNone(at.selectbox(key='list_league').value)
        self.assertIsNone(at.selectbox(key='list_size').value)
        self.assertEqual(at.selectbox(key='list_origin').value,'all')
        self.assertEqual(at.selectbox(key='list_state').value,'all')
        self.assertIsNone(at.session_state['opened'])
        self.assertEqual(at.session_state['rows'],rows)
        self.assertTrue(any('Clubs + répétitions' in value.value for value in at.caption))
        self.assertTrue(any('12,34 %' in value.value for value in at.markdown))
        self.assert_readonly()

    def test_optional_filters_and_search_combine_without_hiding_other_sizes_by_default(self):
        rows = [row(1),row(2,league='61',size=4,origin='automatic',status='in_progress'),
                row(3,size=4,origin='automatic',status='settled')]
        at = self.open(rows)
        at.selectbox(key='list_origin').select('automatic').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.entries(at)),2)
        at.selectbox(key='list_size').select(4).run()
        at.text_input(key='list_query').set_value('France').run()
        self.assertFalse(at.exception)
        self.assertEqual([button.key for button in self.entries(at)],['list_open_'+rows[1]['entry_id']])
        at.selectbox(key='list_state').select('settled').run()
        self.assertFalse(at.exception)
        self.assertFalse(self.entries(at))
        self.assertTrue(any('Aucune analyse ne correspond' in value.value for value in at.info))
        at.selectbox(key='list_state').select('all').run()
        at.text_input(key='list_query').set_value('Club2').run()
        self.assertEqual(len(self.entries(at)),1)
        self.assert_readonly()

    def test_scenario_verdict_is_visible_before_opening_and_can_filter_history(self):
        hit=row(1,status='settled');miss=row(2,status='settled');waiting=row(3)
        hit['scenario_outcome']=dict(state='hit',label='Scénario réalisé',actual=['2nd','1st'],
                                     matches=[dict(method='clubs',rank=3,sequence=['2nd','1st'])])
        miss['scenario_outcome']=dict(state='miss',label='Aucun scénario proposé réalisé',actual=['Égal']*2,matches=[])
        waiting['scenario_outcome']=dict(state='pending',label='Scénarios en attente',actual=None,matches=[])
        rows=[hit,miss,waiting]
        original=copy.deepcopy(rows)
        at=self.open(rows)
        text='\n'.join(item.value for item in at.markdown)
        for label in ('Scénario réalisé','Aucun scénario proposé réalisé','Scénarios en attente'):
            self.assertIn(label,text)
        self.assertTrue(any(item.value=='Clubs · Choix 3' for item in at.caption))
        at.selectbox(key='list_verdict').set_value('hit').run()
        self.assertEqual([button.key for button in self.entries(at)],['list_open_'+hit['id']])
        self.assertIsNone(at.session_state['opened'])
        self.assertEqual(at.session_state['rows'],original)
        self.assert_readonly()

    def test_open_button_returns_complete_stable_id_after_reordering(self):
        rows = [row(1),row(2),row(3)]
        at = self.open(rows)
        at.session_state['rows'] = list(reversed(rows))
        at.run()
        at.button(key='list_open_'+rows[1]['entry_id']).click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['opened'],rows[1]['entry_id'])
        at.run()
        self.assertIsNone(at.session_state['opened'])
        self.assertEqual(at.session_state['rows'],list(reversed(rows)))
        self.assert_readonly()

    def test_pagination_sizes_and_resets_cover_every_group(self):
        rows = [row(number) for number in range(1,24)]
        at = self.open(rows)
        self.assertEqual(len(self.entries(at)),10)
        at.session_state['requested_page'] = 2
        at.run()
        self.assertFalse(at.exception)
        second_ids = {button.key for button in self.entries(at)}
        self.assertEqual(len(second_ids),10)
        at.session_state['requested_page'] = 3
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(len(self.entries(at)),3)
        at.selectbox(key='list_page_size').select(20).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['list_page'],1)
        self.assertEqual(len(self.entries(at)),20)
        at.session_state['requested_page'] = 2
        at.run()
        self.assertEqual(len(self.entries(at)),3)
        at.text_input(key='list_query').set_value('Club1').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['list_page'],1)
        at.selectbox(key='list_page_size').select(50).run()
        self.assertEqual(len(self.entries(at)),11)
        at.text_input(key='list_query').set_value('').run()
        self.assertEqual(len(self.entries(at)),23)
        self.assert_readonly()

    def test_new_day_resets_page_and_invalid_filter_options_gracefully(self):
        at = self.open([row(number) for number in range(1,24)])
        at.session_state['requested_page'] = 2
        at.run()
        at.session_state['day'] = '2026-10-21'
        at.session_state['rows'] = [row(1,league='61',size=8)]
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['list_page'],1)
        self.assertEqual(len(self.entries(at)),1)
        self.assert_readonly()

    def test_large_scenario_is_an_explicit_preview_with_complete_scenario_probability(self):
        at = self.open([row(1,size=20)])
        from decimal import Decimal
        sequence = row(1,size=20)['primary_sequence']
        product = Decimal(1)
        for value in sequence:
            product *= {'1st': Decimal('3.10'), '2nd': Decimal('2.10'), 'Égal': Decimal('3.00')}[value]
        expected = f'{product:,.2f}'.replace(',', '\u202f').replace('.', ',')
        self.assertTrue(any(f'Cote cumulée indicative : {expected}' in value.value for value in at.caption))
        self.assertTrue(any('Début du scénario · 20 résultats' in value.value for value in at.caption))
        self.assertTrue(any('+18 affiches' in value.value for value in at.caption))
        html = '\n'.join(node.proto.body for node in at.get('html'))
        self.assertIn('pl-scenario-token-first',html)
        self.assertIn('pl-scenario-token-second',html)
        self.assertIn('pl-scenario-token-equal',html)
        self.assertEqual(html.count('class="pl-scenario-token pl-scenario-token-'),6)
        self.assert_readonly()

    def test_unavailable_primary_and_empty_list_are_explicit(self):
        unavailable = row(1)
        unavailable.update(primary_sequence=None,primary_probability=None)
        at = self.open([unavailable])
        self.assertFalse(at.exception)
        self.assertTrue(any('Premier scénario indisponible' in value.value for value in at.caption))
        self.assertEqual(len(self.entries(at)),1)
        empty = self.open([])
        self.assertTrue(any('Aucune analyse enregistrée' in value.value for value in empty.info))
        self.assertFalse(self.entries(empty))
        self.assertIsNone(empty.session_state['opened'])
        self.assert_readonly()

    def test_provider_text_cannot_insert_external_markdown_images_or_html(self):
        hostile = row(1)
        hostile.update(competition='![x](https://evil.invalid/image)', country='[name](https://evil.invalid)',
                       match_names=['<img src=x onerror=alert(1)>'])
        at = self.open([hostile])
        self.assertFalse(at.exception)
        title = next(value.value for value in at.markdown if 'evil' in value.value)
        self.assertIn(r'\!\[x\]\(https://evil\.invalid/image\)',title)
        html = '\n'.join(node.proto.body for node in at.get('html'))
        self.assertNotIn('evil.invalid',html)
        self.assertNotIn('<img',html)
        self.assert_readonly()

    def test_filters_survive_a_hidden_detail_view_and_return(self):
        rows = [row(number) for number in range(1,24)]
        code = ('import json\nfrom datetime import date\nimport streamlit as st\nimport journal_browser\n'
                f'rows=json.loads({json.dumps(json.dumps(rows))})\n'
                'if "requested_page" in st.session_state:\n'
                '    st.session_state["list_page"]=st.session_state.pop("requested_page")\n'
                'if st.session_state.get("detail"):\n'
                '    if st.button("Retour",key="back"):\n'
                '        st.session_state.pop("detail",None)\n'
                '        st.rerun()\n'
                'else:\n'
                '    selected=journal_browser.render(rows,date(2026,10,20),key="list")\n'
                '    if selected is not None:\n'
                '        st.session_state["detail"]=selected\n'
                '        st.rerun()\n')
        at = AppTest.from_string(code,default_timeout=15).run()
        at.text_input(key='list_query').set_value('Club1').run()
        at.session_state['requested_page'] = 2
        at.run()
        # AppTest does not serialize pagination's frontend value between runs.
        at.session_state['requested_page'] = 2
        at.button(key=self.entries(at)[0].key).click().run()
        self.assertFalse(at.exception)
        self.assertFalse(at.text_input)
        at.button(key='back').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.text_input(key='list_query').value,'Club1')
        self.assertEqual(at.session_state['list_page'],2)
        self.assertEqual(len(self.entries(at)),1)
        self.assert_readonly()


if __name__ == '__main__':
    unittest.main()
