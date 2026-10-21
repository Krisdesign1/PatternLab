"""Day boundaries, causal history and stable native competition actions."""
from contextlib import ExitStack
import copy
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import api_football as api
import catboost_scenarios as ml
import competition_browser as browser
import competition_overview as data
import competition_readiness as readiness
from test_competition_readiness import history


DAY = date(2025,8,1)
NOW = datetime(2025,8,1,10,tzinfo=timezone.utc)


def snapshot(codes=('39','61','777'), counts=(3,2,1)):
    result = dict(day=DAY.isoformat(), timezone='Africa/Kinshasa', source_label='API-Football',
                  competitions={}, matches=[], fixtures=[])
    for index,(code,count) in enumerate(zip(codes,counts)):
        result['competitions'][code] = dict(name=('Premier League' if code=='39' else 'Ligue 1' if code=='61' else f'Division {code}'),
                                            country='England' if code=='39' else 'France' if code=='61' else 'Congo')
        for offset in range(count):
            mid = str(100000+index*100+offset)
            stamp = f'{DAY.isoformat()}T{12+offset:02}:00:00'
            result['matches'].append(dict(league=dict(id=int(code)),fixture=dict(id=int(mid),date=stamp+'Z')))
            result['fixtures'].append(dict(match_id=mid,league=code,timestamp=stamp,provider='api-football',
                home='Home',away='Away',home_id='1',away_id='2',is_upcoming=True,is_historical=False))
    return result


class CompetitionTable(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.active=False;self.body=False;self.cell=None;self.row=[];self.rows=[];self.headers=[]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag=='table' and 'pl-competition-table' in dict(attrs).get('class','').split():self.active=True
        if not self.active:return
        if tag=='tbody':self.body=True
        if tag=='tr':self.row=[]
        if tag in ('th','td'):self.cell=[]

    def handle_data(self, data):
        if self.active and self.cell is not None:self.cell.append(data)

    def handle_endtag(self, tag):
        if not self.active:return
        if tag in ('th','td') and self.cell is not None:
            self.row.append(' '.join(self.cell));self.cell=None
        if tag=='tr':
            if self.body:self.rows.append(self.row)
            else:self.headers=self.row
        if tag=='tbody':self.body=False
        if tag=='table':self.active=False


def rendered_table(at):
    markup=next(item.proto.body for item in at.get('html') if 'pl-competition-table' in item.proto.body)
    return CompetitionTable(markup)


class OverviewTests(unittest.TestCase):
    def test_counts_are_causal_and_history_is_distinct_from_available_fixture_size(self):
        state=snapshot()
        rows=history(80)+history(40,league='61')
        original=copy.deepcopy(rows)
        values=data.build(state,rows,DAY,size=4,now=NOW)
        self.assertEqual([r['league'] for r in values],['39','61','777'])
        first=values[0]
        self.assertEqual((first['matches'],first['upcoming'],first['history_matches'],first['history_groups']),(3,3,80,20))
        self.assertTrue(first['history_ready'])
        self.assertFalse(first['fixture_ready'])
        self.assertEqual(first['fixture_state'],'Pas assez d’affiches (3/4)')
        self.assertEqual(values[1]['missing_matches'],40)
        self.assertEqual(values[2]['history_matches'],0)
        self.assertEqual(rows,original)
        self.assertEqual([r['league'] for r in data.select(values,sort='history')],['39','61','777'])
        self.assertEqual([r['league'] for r in data.select(values,sort='name')],['777','61','39'])
        self.assertEqual([r['league'] for r in data.select(values,country='Congo',query='777')],['777'])
        self.assertEqual(data.select(values,country='Congo',query='Premier'),[])

    def test_kinshasa_first_fixture_uses_previous_utc_day_and_future_rows_cannot_train(self):
        state=snapshot(('39',),(2,))
        state['matches'][0]['fixture']['date']='2025-08-01T00:15:00+01:00'
        state['fixtures'][0]['timestamp']='2025-07-31T23:15:00'
        included=dict(history(1)[0],match_id='9001',timestamp='2025-07-31T00:30:00+02:00')
        excluded=dict(history(1)[0],match_id='9002',timestamp='2025-07-30T23:30:00-02:00')
        same_today=dict(history(1)[0],match_id='9003',timestamp='2025-08-01T00:00:00',ht=None,ft=None,value='invalid')
        result=data.build(state,history(79)+[included,excluded,same_today],DAY,now=NOW)[0]
        self.assertEqual(result['before'],'2025-07-31')
        self.assertEqual(result['history_matches'],80)
        self.assertTrue(result['history_ready'])
        future=copy.deepcopy(state)
        future['day']='2025-08-02'
        for raw in future['matches']:raw['fixture']['date']='2025-08-02T12:00:00Z'
        for row in future['fixtures']:row['timestamp']='2025-08-02T12:00:00'
        future_result=data.build(future,history(79)+[same_today],date(2025,8,2),now=NOW)[0]
        self.assertEqual(future_result['before'],'2025-08-01')
        self.assertEqual(future_result['history_matches'],79)

    def test_day_filter_excludes_neighbors_duplicates_and_wrong_snapshot_day(self):
        state=snapshot(('39',),(2,))
        state['matches'] += [copy.deepcopy(state['matches'][0])]
        wrong=copy.deepcopy(state['matches'][0]);wrong['fixture'].update(id=99,date='2025-08-01T23:30:00Z')
        state['matches'].append(wrong)
        state['fixtures'] += [dict(state['fixtures'][0],match_id='99',timestamp='2025-08-01T23:30:00')]
        result=data.build(state,[],DAY,now=NOW)[0]
        self.assertEqual(result['matches'],2)
        self.assertEqual(result['upcoming'],2)
        self.assertEqual(data.build(state,[],DAY+timedelta(days=1),now=NOW),[])

    def test_past_date_counts_finished_available_for_simulation_not_future_predictions(self):
        state=snapshot(('39',),(2,))
        for fixture in state['fixtures']:fixture.update(is_upcoming=False,is_historical=True)
        result=data.build(state,history(40),DAY,size=2,now=NOW+timedelta(days=1))[0]
        self.assertEqual(result['upcoming'],0)
        self.assertEqual(result['available'],2)
        self.assertEqual(result['fixture_state'],'Simulation possible (2/2)')
        self.assertTrue(result['history_ready'])

    def test_other_sources_and_leagues_do_not_corrupt_history_and_invalid_league_stays_visible(self):
        state=snapshot()
        bad=dict(history(1)[0],provider='other',ht=None,ft=None,value='invalid')
        wrong=dict(bad,provider='api-football',league='999')
        result=data.build(state,history(80)+[bad,wrong],DAY,now=NOW)
        self.assertEqual(result[0]['history_matches'],80)
        invalid=dict(history(1)[0],value='invalid')
        result=data.build(state,history(80)+[invalid],DAY,now=NOW)
        self.assertEqual(len(result),3)
        self.assertEqual(result[0]['preparation'],'À vérifier')
        self.assertIsNone(result[0]['history_matches'])

    def test_each_competition_receives_only_its_own_history_and_no_prediction_or_io(self):
        state=snapshot()
        rows=history(80)+history(90,league='61')+history(20,league='777')
        assess=readiness.assess_history
        populations=[]
        def check(values,league,*args):
            populations.append((league,len(values)))
            self.assertTrue(all(str(row['league'])==league for row in values))
            return assess(values,league,*args)
        with ExitStack() as stack:
            for target,name in ((api,'load_key'),(api,'load_fixtures'),(api.ApiFootballClient,'get'),
                                (ml,'train'),(ml,'forecast'),(ml,'load_report')):
                stack.enter_context(patch.object(target,name,side_effect=AssertionError(name)))
            stack.enter_context(patch.object(readiness,'assess_history',side_effect=check))
            data.build(state,rows,DAY,now=NOW)
        self.assertEqual(populations,[('39',80),('61',90),('777',20)])


class FixedTime(datetime):
    @classmethod
    def now(cls,tz=None):return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.snapshot=snapshot(tuple(str(100+i) for i in range(23)),tuple(1 for _ in range(23)))
        self.rows=[]
        self.patch(browser,'datetime',FixedTime)
        self.network=self.patch(api.ApiFootballClient,'get',side_effect=AssertionError('No API on browser render'))
        self.assess=self.patch(data.readiness,'assess_history',wraps=data.readiness.assess_history)
        browser._overview.clear();self.addCleanup(browser._overview.clear)

    def patch(self,target,name,*args,**kwargs):
        patcher=patch.object(target,name,*args,**kwargs)
        value=patcher.start();self.addCleanup(patcher.stop);return value

    def open(self):
        source=(f"from datetime import date\nimport streamlit as st\nimport competition_browser as browser\n"
                f"snapshot={self.snapshot!r}\nrows={self.rows!r}\n"
                f"chosen=browser.render(snapshot,rows,date(2025,8,1))\nst.write('chosen='+str(chosen))")
        at=AppTest.from_string(source,default_timeout=15).run()
        self.assertFalse(at.exception)
        return at

    def table(self,at):return rendered_table(at)

    def test_default_page_size_and_independent_preparation_size_are_read_only_cached(self):
        at=self.open()
        self.assertEqual(len(self.table(at).rows),10)
        self.assertEqual(at.selectbox(key='competition_browser_size').value,4)
        self.assertEqual(at.selectbox(key='competition_browser_page').value,1)
        self.assertTrue(any(item.value=='chosen=None' for item in at.markdown))
        self.assertEqual(self.assess.call_count,23)
        at.selectbox(key='competition_browser_sort').set_value('name').run()
        self.assertEqual(self.assess.call_count,23)
        at.selectbox(key='competition_browser_page_size').set_value(20).run()
        self.assertEqual(len(self.table(at).rows),20)
        self.assertEqual(self.assess.call_count,23)
        self.network.assert_not_called()
        self.assertEqual(list(self.root.iterdir()),[])

    def test_pagination_filters_reset_page_and_analysis_uses_explicit_visible_id(self):
        at=self.open()
        at.selectbox(key='competition_browser_page').set_value(3).run()
        self.assertEqual(len(self.table(at).rows),3)
        selected=at.selectbox(key='competition_browser_selection').value
        self.assertTrue(any(f'Division {selected} ' in row[0] for row in self.table(at).rows))
        at.text_input(key='competition_browser_query').set_value('Division 11').run()
        self.assertEqual(at.selectbox(key='competition_browser_page').value,1)
        self.assertEqual({row[0] for row in self.table(at).rows},{f'Division {i} Congo' for i in range(110,120)})
        at.selectbox(key='competition_browser_selection').set_value('117').run()
        self.assertTrue(any(item.value=='chosen=None' for item in at.markdown))
        at.button(key='competition_browser_analyze').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any(item.value=='chosen=117' for item in at.markdown))
        at.text_input(key='competition_browser_query').set_value('Division 102').run()
        self.assertEqual([row[0] for row in self.table(at).rows],['Division 102 Congo'])
        self.assertEqual(at.selectbox(key='competition_browser_selection').value,'102')
        at.button(key='competition_browser_analyze').click().run()
        self.assertTrue(any(item.value=='chosen=102' for item in at.markdown))
        self.network.assert_not_called()

    def test_size_change_recomputes_preparation_and_empty_search_is_explicit(self):
        at=self.open()
        at.selectbox(key='competition_browser_size').set_value(2).run()
        self.assertIn('Préparation Groupe de 2 matchs',self.table(at).headers)
        self.assertEqual(self.assess.call_count,46)
        at.text_input(key='competition_browser_query').set_value('not found').run()
        self.assertFalse(at.exception)
        self.assertFalse(any('pl-competition-table' in item.proto.body for item in at.get('html')))
        self.assertTrue(any('Aucune compétition ne correspond' in item.value for item in at.info))
        self.assertFalse(at.button)
        self.network.assert_not_called()

    def test_sorted_options_are_ids_not_stored_positions(self):
        state=snapshot()
        rows=history(80)+history(120,league='61')
        self.assertEqual(browser.sorted_options(state,rows,DAY,now=NOW),['39','61','777'])
        self.assertEqual(browser.sorted_options(state,rows,DAY,sort='history',now=NOW),['61','39','777'])


class CompetitionHtmlTests(unittest.TestCase):
    def test_rich_cells_have_country_second_line_history_groups_and_separate_business_badges(self):
        items=data.build(snapshot(),history(80)+history(40,league='61'),DAY,now=NOW)
        markup=browser.table_html(items,size=4,theme='light')
        table=CompetitionTable(markup)
        self.assertEqual(table.headers,['Compétition','Matchs du jour','À venir','Historique exploitable',
                                        'Préparation Groupe de 4 matchs','Affiches disponibles'])
        self.assertEqual(len(table.rows),3)
        self.assertEqual(table.rows[0][0],'Premier League England')
        self.assertEqual(table.rows[0][3],'80 20 groupes complets')
        self.assertEqual(table.rows[0][4],'Prêt Pour 4 matchs')
        self.assertEqual(table.rows[0][5],'Pas assez d’affiches 3/4 matchs à venir')
        self.assertEqual(table.rows[1][4],'À compléter Encore 40 résultat(s)')
        self.assertNotIn('ID compétition',markup)
        self.assertIn('class="pl-competition-name"',markup)
        self.assertIn('class="pl-competition-meta">England',markup)
        self.assertNotIn('<script',markup)

    def test_all_provider_strings_are_escaped_and_css_remains_scoped_in_both_themes(self):
        item=data.build(snapshot(('39',),(1,)),history(80),DAY,now=NOW)[0]
        item.update(competition='<img src=x onerror=alert(1)>',country='<script>alert(2)</script>',
                    preparation_reason='"><svg onload=alert(3)>',history_ready=False,missing_matches=None,
                    history_matches=None,history_groups=None)
        for theme in ('light','dark'):
            with self.subTest(theme=theme):
                markup=browser.table_html([item],theme=theme)
                self.assertIn('&lt;img',markup)
                self.assertIn('&lt;script&gt;alert(2)&lt;/script&gt;',markup)
                self.assertIn('&quot;&gt;&lt;svg',markup)
                self.assertNotIn('<img',markup);self.assertNotIn('<script',markup);self.assertNotIn('<svg',markup)
                table=CompetitionTable(markup)
                self.assertEqual(table.rows[0][3],'— Historique à vérifier')
                self.assertEqual(table.rows[0][4],'À vérifier Données à vérifier')
                for line in markup.splitlines():
                    if '{' in line and not line.lstrip().startswith('<'):
                        self.assertTrue(line.startswith('.pl-scenario-root') or line.startswith('.pl-competition-root'))
        self.assertNotEqual(browser.table_html([item],theme='light'),browser.table_html([item],theme='dark'))


if __name__=='__main__':unittest.main()
