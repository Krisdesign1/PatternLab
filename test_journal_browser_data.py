"""Daily frozen groups, prudent states and deterministic read-only browsing."""
from contextlib import ExitStack
import copy
from datetime import date, datetime, timedelta, timezone
from math import nan
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import journal_browser_data as browser
import prediction_journal as journal
import prospective_models as models


NOW=datetime(2026,10,20,10,tzinfo=timezone.utc)


def entry(identifier='entry-full-identity', *, size=4, league='39', status='pending',
          selected='statistical', day='2026-10-20', created='2026-10-19T12:30:00Z',observed=0):
    fixtures=[dict(match_id=str(1000+i),league=league,provider='api-football',
                   home=f'Étoile {i}',away=f'Léopards {i}',home_id=str(100+i),away_id=str(200+i),
                   timestamp=f'{day}T12:{i:02}:00',season=2026) for i in range(size)]
    methods={name:dict(version='frozen-v1',generation='old-generation',
                       ranking=[dict(sequence=[label]*size,probability=probability)])
             for name,label,probability in [('frequency','Égal',.2),('clubs','1st',.8),
                                            ('combined','2nd',.125),('catboost','1st',.3),
                                            ('context_neighbors','Égal',.1)]}
    return dict(id=identifier,source='API-Football',league=league,size=size,status=status,
                created_at=created,fixtures=fixtures,methods=methods,
                metadata=dict(selected_engine=selected,competition='Première division',country='Côte d’Ivoire'),
                observed=[dict(match_id=fixtures[i]['match_id'],ht='0-0',ft='1-0',value='2nd') for i in range(observed)],
                evaluations={name:dict(top1=True,top3=True,loss=.1) for name in methods},actual=['2nd']*size)


class JournalBrowserDataTests(unittest.TestCase):
    def test_primary_choice_uses_saved_selected_engine_not_highest_probability_or_results(self):
        value=entry(selected='catboost')
        original=copy.deepcopy(value)
        row=browser.build_rows([value],now=NOW)[0]
        self.assertEqual(row['primary_method'],'catboost')
        self.assertEqual(row['primary_method_name'],'CatBoost')
        self.assertEqual(row['primary_sequence'],['1st']*4)
        self.assertEqual(row['primary_probability'],.3)
        self.assertEqual(row['primary_generation'],'old-generation')
        self.assertNotIn('top1',row);self.assertNotIn('top3',row);self.assertNotIn('accuracy',row)
        self.assertEqual(value,original)
        row['primary_sequence'][0]='Égal';row['fixtures'][0]['home']='Other'
        self.assertEqual(value,original)

    def test_all_sizes_and_competitions_remain_visible_and_origin_is_explicit(self):
        entries=[entry(str(size),size=size,league=str(100+size),selected='automation' if size%2 else 'statistical')
                 for size in range(2,21)]
        rows=browser.build_rows(entries,now=NOW)
        self.assertEqual(len(rows),19)
        self.assertEqual({row['size'] for row in rows},set(range(2,21)))
        self.assertEqual(len({row['league'] for row in rows}),19)
        self.assertEqual({row['origin'] for row in rows},{'manual','automatic'})
        self.assertEqual(browser.filter_rows(rows),rows)
        two=browser.filter_rows(rows,size=2,league='102',origin='manual')
        self.assertEqual([row['id'] for row in two],['2'])
        self.assertEqual(two[0]['expected_matches'],2)
        self.assertEqual(two[0]['match_names'][0],'Étoile 0 — Léopards 0')

    def test_day_filter_uses_every_kickoff_in_kinshasa_not_creation_date(self):
        values=[]
        for identifier,stamps in [
            ('today',['2026-10-19T23:00:00Z','2026-10-20T22:59:59Z']),
            ('tomorrow',['2026-10-20T23:00:00Z','2026-10-21T12:00:00Z']),
            ('yesterday',['2026-10-19T22:00:00Z','2026-10-19T22:59:59Z']),
            ('mixed',['2026-10-20T12:00:00Z','2026-10-20T23:30:00Z']),
            ('offset',['2026-10-20T00:00:00+01:00','2026-10-20T23:00:00+01:00'])]:
            value=entry(identifier,size=2,created='2026-10-18T09:00:00Z')
            for fixture,stamp in zip(value['fixtures'],stamps):fixture['timestamp']=stamp
            values.append(value)
        original=copy.deepcopy(values)
        rows=browser.build_rows(values,now=NOW)
        self.assertEqual([row['id'] for row in rows],['offset','today'])
        self.assertEqual(rows[1]['kickoff_range'],'00:00–23:59')
        self.assertEqual(rows[1]['created_local'],'18/10/2026 à 10:00')
        self.assertEqual(values,original)
        # At 23 UTC, the Kinshasa date has advanced while the UTC date has not.
        next_rows=browser.build_rows(values,now=datetime(2026,10,20,23,1,tzinfo=timezone.utc))
        self.assertEqual([row['id'] for row in next_rows],['tomorrow'])

    def test_pending_clock_never_proves_live_or_success_and_partial_counts_are_saved_only(self):
        future=entry('future')
        awaiting=entry('awaiting')
        awaiting['fixtures'][0]['timestamp']='2026-10-20T09:00:00'
        partial=entry('partial',observed=1)
        partial['observed'] += [copy.deepcopy(partial['observed'][0]),dict(match_id='outside')]
        states={row['id']:row for row in browser.build_rows([future,awaiting,partial],now=NOW)}
        self.assertEqual((states['future']['state'],states['future']['status_label']),('pending','En attente'))
        self.assertEqual((states['awaiting']['state'],states['awaiting']['status_label']),('awaiting_results','Résultats attendus'))
        self.assertEqual((states['partial']['state'],states['partial']['status_label']),('in_progress','Résultats partiels'))
        self.assertEqual((states['partial']['progress'],states['partial']['total']),(1,4))
        for row in states.values():
            self.assertEqual(row['journal_status'],'pending')
            self.assertNotIn('réussi',row['status_label'].casefold())
            self.assertNotIn('live',row['status_label'].casefold())
        self.assertEqual([row['id'] for row in browser.filter_rows(states.values(),state='awaiting_results')],['awaiting'])

    def test_explicit_archive_day_preserves_saved_forecast_and_uses_actual_consultation_clock(self):
        archived=entry('past-pending',day='2026-10-19',created='2026-10-18T08:00:00Z')
        partial=entry('past-partial',day='2026-10-19',observed=1)
        current=entry('today')
        original=copy.deepcopy([archived,partial,current])
        rows=browser.build_rows([archived,partial,current],now=NOW,day=date(2026,10,19))
        indexed={row['id']:row for row in rows}
        self.assertEqual(set(indexed),{'past-pending','past-partial'})
        self.assertEqual(indexed['past-pending']['state'],'awaiting_results')
        self.assertEqual(indexed['past-partial']['state'],'in_progress')
        self.assertEqual(indexed['past-pending']['primary_probability'],.125)
        self.assertEqual(indexed['past-pending']['primary_sequence'],['2nd']*4)
        self.assertEqual({row['day'] for row in rows},{'2026-10-19'})
        self.assertEqual([archived,partial,current],original)
        self.assertEqual([row['id'] for row in browser.build_rows([archived,current],now=NOW)],['today'])

    def test_archive_day_is_the_whole_group_local_day_even_across_utc_midnight(self):
        archived=entry('local-nineteenth',size=2)
        archived['fixtures'][0]['timestamp']='2026-10-18T23:00:00Z'
        archived['fixtures'][1]['timestamp']='2026-10-19T22:59:59Z'
        mixed=copy.deepcopy(archived);mixed['id']='mixed'
        mixed['fixtures'][1]['timestamp']='2026-10-19T23:00:00Z'
        rows=browser.build_rows([archived,mixed],now=NOW,day=date(2026,10,19))
        self.assertEqual([row['id'] for row in rows],['local-nineteenth'])
        self.assertEqual(rows[0]['kickoff_range'],'00:00–23:59')
        self.assertEqual(rows[0]['status_label'],'Résultats attendus')

    def test_archive_selector_rejects_future_and_invalid_local_dates(self):
        value=entry()
        for day in (date(2026,10,21),datetime(2026,10,19), '2026-10-19',False):
            with self.subTest(day=day),self.assertRaises(ValueError):
                browser.build_rows([value],now=NOW,day=day)
        # 23:00 UTC already belongs to tomorrow in Kinshasa.
        next_now=datetime(2026,10,20,23,1,tzinfo=timezone.utc)
        next_day=entry('next-local-day',day='2026-10-21')
        self.assertEqual([row['id'] for row in browser.build_rows(
            [value,next_day],now=next_now,day=date(2026,10,21))],['next-local-day'])

    def test_archive_consultation_does_not_fetch_rebuild_or_record_predictions(self):
        value=entry('archived',day='2026-10-19',selected='context_neighbors')
        original=copy.deepcopy(value)
        with ExitStack() as stack:
            for target,name in ((api,'load_key'),(api,'load_fixtures'),(api.ApiFootballClient,'get'),
                                (ml,'load_report'),(ml,'forecast'),(models,'build_methods'),
                                (journal,'list_entries'),(journal,'record')):
                stack.enter_context(patch.object(target,name,side_effect=AssertionError(name)))
            stack.enter_context(patch('builtins.open',side_effect=AssertionError('No file I/O')))
            rows=browser.build_rows([value],now=NOW,day=date(2026,10,19))
            filtered=browser.filter_rows(rows,state='awaiting_results',query='etoile')
        self.assertEqual(filtered[0]['primary_sequence'],['Égal']*4)
        self.assertEqual(filtered[0]['primary_probability'],.1)
        self.assertEqual(value,original)

    def test_saved_terminal_states_are_not_inferred_from_probability_or_elapsed_time(self):
        values=[entry(state,status=state,observed=4 if state=='settled' else 0) for state in ('settled','void','unresolved')]
        labels={row['journal_status']:row['status_label'] for row in browser.build_rows(values,now=NOW)}
        self.assertEqual(labels,{'settled':'Évalué','void':'Annulé','unresolved':'À vérifier'})

    def test_verdict_checks_all_saved_methods_and_filters_without_changing_primary(self):
        hit=entry('hit',status='settled',selected='catboost')
        miss=entry('miss',status='settled')
        miss['actual']=['1st','2nd','Égal','2nd']
        waiting=entry('waiting',observed=1)
        original=copy.deepcopy([hit,miss,waiting])
        rows=browser.build_rows([hit,miss,waiting],now=NOW)
        self.assertEqual([row['id'] for row in browser.filter_rows(rows,verdict='hit')],['hit'])
        self.assertEqual([row['id'] for row in browser.filter_rows(rows,verdict='miss')],['miss'])
        self.assertEqual([row['id'] for row in browser.filter_rows(rows,verdict='pending')],['waiting'])
        row=next(row for row in rows if row['id']=='hit')
        self.assertEqual(row['primary_method'],'catboost')
        self.assertEqual(row['primary_sequence'],['1st']*4)
        self.assertEqual([(item['method'],item['rank']) for item in row['scenario_outcome']['matches']],[('combined',1)])
        self.assertEqual([hit,miss,waiting],original)
        with self.assertRaises(ValueError):browser.filter_rows(rows,verdict='made-up-success')

    def test_absent_selected_method_uses_deterministic_named_fallback_without_max_probability(self):
        value=entry(selected='catboost');value['methods'].pop('catboost')
        row=browser.build_rows([value],now=NOW)[0]
        self.assertEqual((row['primary_method'],row['primary_probability']),('combined',.125))
        value['methods'].pop('combined')
        row=browser.build_rows([value],now=NOW)[0]
        self.assertEqual((row['primary_method'],row['primary_probability']),('frequency',.2))
        value['methods']['frequency'].pop('ranking')
        row=browser.build_rows([value],now=NOW)[0]
        self.assertEqual(row['primary_method_name'],'Répétitions seules')
        self.assertIsNone(row['primary_probability']);self.assertIsNone(row['primary_sequence'])
        value['methods']={}
        row=browser.build_rows([value],now=NOW)[0]
        self.assertIsNone(row['primary_method']);self.assertIsNone(row['primary_probability'])

    def test_sort_created_desc_then_full_id_and_accent_casefold_search(self):
        values=[entry('b',created='2026-10-19T12:00:00Z'),entry('a',created='2026-10-19T12:00:00Z'),
                entry('new',created='2026-10-20T08:00:00-01:00')]
        rows=browser.build_rows(reversed(values),now=NOW)
        original=copy.deepcopy(rows)
        self.assertEqual([row['id'] for row in rows],['new','a','b'])
        for query in ('ETOILE','leopards','premiere','cote','DIVISION'):
            self.assertEqual(browser.filter_rows(rows,query=query),rows)
        self.assertEqual([row['id'] for row in browser.filter_rows(rows,query='new')],['new'])
        self.assertEqual(browser.filter_rows(rows,query='other'),[])
        self.assertEqual(rows,original)
        self.assertEqual(browser.select(rows,origin=None,status='pending'),rows)

    def test_foreign_source_duplicate_groups_and_damaged_frozen_fields_fail_explicitly(self):
        for change in (
            lambda value:value.update(source='football-data.org'),
            lambda value:value['fixtures'][0].update(provider='forebet'),
            lambda value:value['fixtures'][0].update(league='61'),
            lambda value:value.update(size=3),
            lambda value:value['methods']['combined']['ranking'][0].update(probability=nan),
            lambda value:value['methods']['combined']['ranking'][0].update(probability=True),
            lambda value:value['methods']['combined']['ranking'][0].update(sequence=['2nd']*3),
            lambda value:value.update(status='success'),
        ):
            value=entry();change(value)
            with self.subTest(value=value),self.assertRaises(ValueError):browser.build_rows([value],now=NOW)
        with self.assertRaises(ValueError):browser.build_rows([entry(),entry()],now=NOW)
        with self.assertRaises(ValueError):browser.build_rows([entry()],now=NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):browser.filter_rows([],state='invalid')

    def test_pure_module_never_reads_data_api_or_current_models_and_ignores_current_inputs(self):
        value=entry(selected='context_neighbors')
        original=copy.deepcopy(value)
        with ExitStack() as stack:
            for target,name in ((api,'load_key'),(api,'load_fixtures'),(api.ApiFootballClient,'get'),
                                (ml,'load_report'),(ml,'forecast'),(models,'build_methods'),
                                (journal,'list_entries'),(journal,'record')):
                stack.enter_context(patch.object(target,name,side_effect=AssertionError(name)))
            stack.enter_context(patch('builtins.open',side_effect=AssertionError('No file I/O')))
            rows=browser.build_rows([value],now=NOW)
            filtered=browser.filter_rows(rows,query='etoile')
        self.assertEqual(filtered[0]['primary_sequence'],['Égal']*4)
        self.assertEqual(filtered[0]['primary_probability'],.1)
        self.assertEqual(value,original)


if __name__=='__main__':unittest.main()
