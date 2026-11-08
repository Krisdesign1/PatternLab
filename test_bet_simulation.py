"""Daily paper-betting accounting, from saved forecasts only."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

import bet_simulation as simulation
from coupon_sizes import top_independent
import prediction_journal as journal
import probability_scenario_store as intent_store


NOW = datetime(2026,10,30,22,tzinfo=timezone.utc)


def entry(identifier='a', day='2026-10-28', status='settled', size=2):
    fixtures=[dict(match_id=str(100+i),league='39',provider='api-football',home=f'Home{i}',away=f'Away{i}',
        home_id=str(1000+i),away_id=str(2000+i),timestamp=f'{day}T12:{i:02}:00',season=2026)
        for i in range(size)]
    positions=[{'1st':.3,'Égal':.1,'2nd':.6} for _ in range(size)]
    method=dict(version='test-v1',source='API-Football',league='39',training_before='2026-10-27',
        ranking=top_independent(positions,3),distribution=dict(schema='prospective-distribution-v1',
            type='independent',size=size,distributions=positions))
    return dict(id=identifier,source='API-Football',size=size,fixtures=fixtures,
        created_at=f'{day}T08:00:00+00:00',checked_at='2026-10-30T12:00:00+00:00',
        methods={'clubs':method},status=status,actual=['2nd']*size,metadata={},observed=[])


def intent(saved, sequence, created='2026-10-28T09:00:00+00:00'):
    saved['id']=journal._group_key(saved['fixtures'])
    probability=1.0
    for label in sequence:
        probability*=dict(zip(('1st','Égal','2nd'),(.3,.1,.6)))[label]
    return dict(id='custom-one',entry_id=saved['id'],created_at=created,
        forecast_digest=intent_store.forecast_digest(saved),
        selection=dict(requested_scenario=dict(sequence=sequence,probability=probability)))


class SimulationTests(unittest.TestCase):
    def build(self, entries, **kwargs):
        return simulation.build(entries, now=NOW, **kwargs)

    def test_all_three_scenarios_cost_a_stake_even_when_only_one_wins(self):
        saved=entry(); before=deepcopy(saved)
        result=self.build([saved],stake=10)
        self.assertEqual(result['summary'],dict(bets=3,won=1,lost=2,pending=0,unresolved=0,refunded=0,
            stake_total=30.0,stake_settled=30.0,stake_open=0.0,stake_refunded=0.0,stake_resolved=30.0,
            returned=44.1,pnl=14.1,roi=47.0))
        winning=next(row for row in result['tickets'] if row['status']=='won')
        self.assertEqual((winning['odds'],winning['return_amount'],winning['pnl']),(4.41,44.1,34.1))
        self.assertEqual(saved,before)

    def test_all_methods_union_is_bet_once_and_probabilities_are_not_combined(self):
        saved=entry()
        saved['methods']={name:deepcopy(saved['methods']['clubs']) for name in simulation.METHOD_LABELS}
        saved['methods']['frequency']['ranking'][0]['probability']=.35
        result=self.build([saved])
        self.assertEqual(result['summary']['bets'],3)
        self.assertEqual(result['duplicates_removed'],12)
        ticket=next(row for row in result['tickets'] if row['status']=='won')
        self.assertEqual(len(ticket['methods']),5)
        self.assertIn('Répétitions · Choix 1 : 35,0000 %',ticket['probability_by_method'])
        self.assertIn('Clubs · Choix 1 : 36,0000 %',ticket['probability_by_method'])
        self.assertEqual(ticket['stake'],1)

    def test_distinct_method_proposals_are_all_retained_without_a_top_three_global_cap(self):
        saved=entry()
        saved['methods']['frequency']=dict(ranking=[
            dict(sequence=['Égal','Égal'],probability=.2),dict(sequence=['1st','1st'],probability=.1),
            dict(sequence=['Égal','1st'],probability=.05)])
        result=self.build([saved])
        self.assertEqual(result['summary']['bets'],6)
        self.assertEqual(result['summary']['lost'],5)

    def test_fourth_explicit_proposal_is_retained_and_can_be_the_only_winner(self):
        saved=entry()
        saved['actual']=['Égal','Égal']
        saved['methods']['clubs']['ranking'].append(dict(sequence=['Égal','Égal'],probability=.01))
        before=deepcopy(saved)
        result=self.build([saved])
        self.assertEqual((result['summary']['bets'],result['summary']['won'],result['summary']['lost']),(4,1,3))
        winning=next(row for row in result['tickets'] if row['status']=='won')
        self.assertEqual(winning['sequence'],['Égal','Égal'])
        self.assertEqual(winning['methods'],['Clubs · Choix 4'])
        self.assertEqual(result['exclusions'],{})
        self.assertEqual(saved,before)

    def test_invalid_tail_rejects_the_entire_method_instead_of_ignoring_it(self):
        for tail in (None, dict(sequence=['unknown','Égal'],probability=.01),
                     dict(sequence=['Égal','Égal'],probability=float('nan')),
                     dict(sequence=['2nd','2nd'],probability=.01)):
            with self.subTest(tail=tail):
                saved=entry()
                saved['methods']['clubs']['ranking'].append(tail)
                result=self.build([saved])
                self.assertEqual(result['summary']['bets'],0)
                self.assertEqual(result['exclusions'],dict(invalid_rankings=1,no_valid_proposals=1))

    def test_long_rankings_are_deduplicated_across_methods_without_reducing_choices(self):
        saved=entry()
        saved['methods']['clubs']['ranking'].append(dict(sequence=['Égal','Égal'],probability=.01))
        saved['methods']['frequency']=deepcopy(saved['methods']['clubs'])
        result=self.build([saved])
        self.assertEqual(result['summary']['bets'],4)
        self.assertEqual(result['duplicates_removed'],4)
        fourth=next(row for row in result['tickets'] if row['sequence']==['Égal','Égal'])
        self.assertEqual(fourth['methods'],['Clubs · Choix 4','Répétitions · Choix 4'])

    def test_same_economic_sequence_is_deduplicated_even_if_fixture_order_changes(self):
        first=entry('first');first['actual']=['1st','2nd']
        second=deepcopy(first);second['id']='second';second['fixtures'].reverse();second['actual'].reverse()
        for proposal in second['methods']['clubs']['ranking']:
            proposal['sequence']=list(reversed(proposal['sequence']))
        result=self.build([first,second])
        self.assertEqual((result['summary']['bets'],result['summary']['won']),(3,1))
        self.assertEqual(result['duplicates_removed'],3)
        self.assertTrue(all(len(row['entry_ids'])==2 for row in result['tickets']))

    def test_overlapping_groups_are_distinct_bets(self):
        first=entry('first'); second=entry('second')
        second['fixtures'][1]['match_id']='999'
        result=self.build([first,second])
        self.assertEqual(result['summary']['bets'],6)
        self.assertEqual(result['duplicates_removed'],0)

    def test_valid_custom_choice_outside_top_three_adds_one_ticket(self):
        saved=entry(); chosen=intent(saved,['Égal','Égal'])
        result=self.build([saved],intents=[chosen])
        self.assertEqual(result['summary']['bets'],4)
        custom=next(row for row in result['tickets'] if row['sequence']==['Égal','Égal'])
        self.assertEqual(custom['methods'],['Clubs · Scénario choisi'])
        self.assertEqual(custom['odds'],9)
        self.assertEqual(custom['captured_at'],chosen['created_at'])
        self.assertEqual(custom['status'],'lost')

    def test_metadata_and_linked_custom_choice_in_top_three_do_not_duplicate_stakes(self):
        saved=entry()
        saved['metadata']['selection']=dict(requested_scenario=dict(sequence=['2nd','2nd'],probability=.36))
        chosen=intent(saved,['2nd','2nd'])
        result=self.build([saved],intents=[chosen])
        self.assertEqual(result['summary']['bets'],3)
        self.assertEqual(result['duplicates_removed'],2)

    def test_corrupt_late_early_and_future_intents_are_excluded(self):
        saved=entry(); valid=intent(saved,['Égal','Égal'])
        cases=[dict(valid,forecast_digest='bad'),dict(valid,created_at='2026-10-28T12:00:00Z'),
               dict(valid,created_at='2026-10-28T07:59:59Z'),dict(valid,created_at='2026-11-01T09:00:00Z'),
               dict(valid,entry_id='missing')]
        result=self.build([saved],intents=cases)
        self.assertEqual(result['summary']['bets'],3)
        self.assertEqual(result['exclusions']['invalid_intent'],5)

    def test_partial_pending_and_unresolved_have_no_return_or_loss(self):
        for state in ('pending','unresolved'):
            with self.subTest(state=state):
                saved=entry(status=state)
                saved['actual']=['2nd','2nd']
                result=self.build([saved])
                self.assertEqual(result['summary'][state],3)
                self.assertEqual(result['summary']['pnl'],0)
                self.assertEqual(result['summary']['stake_open'],3)
                self.assertIsNone(result['summary']['roi'])
                self.assertTrue(all(row['return_amount'] is None and row['pnl'] is None for row in result['tickets']))

    def test_void_group_refunds_all_stakes_and_is_excluded_from_roi_denominator(self):
        saved=entry(status='void')
        result=self.build([saved],stake=2.5)
        self.assertEqual(result['summary']['refunded'],3)
        self.assertEqual(result['summary']['stake_refunded'],7.5)
        self.assertEqual(result['summary']['returned'],7.5)
        self.assertEqual(result['summary']['pnl'],0)
        self.assertEqual(result['summary']['stake_settled'],0)
        self.assertIsNone(result['summary']['roi'])

    def test_missing_actual_damaged_rankings_and_future_results_stay_unresolved(self):
        for mutate in (lambda r:r.update(actual=None),lambda r:r.update(checked_at='2026-11-01T12:00:00Z'),
                       lambda r:r.update(checked_at='2026-10-28T11:59:59Z'),
                       lambda r:r['methods'].update(broken={'ranking':None})):
            saved=entry();mutate(saved)
            result=self.build([saved])
            self.assertEqual(result['summary']['unresolved'],3)
            self.assertEqual(result['summary']['lost'],0)
        saved=entry(day='2026-10-31')
        result=simulation.build([saved],now=datetime(2026,10,31,10,tzinfo=timezone.utc))
        self.assertEqual(result['summary']['unresolved'],3)

    def test_valid_effective_kickoff_correction_is_used_without_rewriting_original_day_or_order(self):
        from fixture_schedule_resolution import resolve
        saved=entry()
        saved['checked_at']='2026-10-28T11:00:00Z'
        saved['observed']=[]
        for index,fixture in enumerate(saved['fixtures']):
            actual=dict(fixture,timestamp=f'2026-10-28T10:{index:02}:00')
            audit=resolve(saved,fixture,actual,status='FT',checked_at=saved['checked_at'])
            saved['observed'].append(dict(match_id=fixture['match_id'],schedule_resolution=audit,
                                          ht='0-0',ft='0-1',value='2nd'))
        before=deepcopy(saved)
        result=simulation.build([saved],now=datetime(2026,10,28,11,30,tzinfo=timezone.utc))
        self.assertEqual(result['summary']['won'],1)
        self.assertEqual(result['summary']['lost'],2)
        self.assertEqual(result['days'][0]['day'],'2026-10-28')
        self.assertEqual(saved,before)
        saved['observed'][0]['schedule_resolution']['home_id']='bad'
        result=simulation.build([saved],now=datetime(2026,10,28,11,30,tzinfo=timezone.utc))
        self.assertEqual(result['summary']['unresolved'],3)

    def test_conflicting_results_of_duplicate_economic_ticket_fail_closed(self):
        a=entry('a'); b=entry('b');b['actual']=['1st','2nd']
        result=self.build([a,b])
        self.assertEqual(result['summary']['bets'],3)
        self.assertEqual(result['summary']['unresolved'],3)
        self.assertEqual(result['summary']['lost'],0)
        self.assertEqual(result['summary']['pnl'],0)

    def test_only_eligible_api_forecasts_and_single_local_day_groups_participate(self):
        valid=entry('valid')
        late=entry('late');late['created_at']=late['fixtures'][0]['timestamp']
        future=entry('future');future['created_at']='2026-11-01T08:00:00Z'
        wrong_source=entry('source');wrong_source['source']='football-data.org'
        spanning=entry('spanning');spanning['fixtures'][1]['timestamp']='2026-10-29T12:00:00'
        result=self.build([valid,late,future,wrong_source,spanning])
        self.assertEqual(result['summary']['bets'],3)
        self.assertEqual(result['exclusions'],dict(after_kickoff=1,future_capture=1,invalid_source=1,mixed_days=1))

    def test_day_is_kinshasa_not_creation_day_or_utc_day(self):
        saved=entry(day='2026-10-28')
        saved['fixtures'][0]['timestamp']='2026-10-28T23:10:00'
        saved['fixtures'][1]['timestamp']='2026-10-29T00:10:00Z'
        result=self.build([saved])
        self.assertEqual(result['days'][0]['day'],'2026-10-29')
        self.assertTrue(all(row['day']=='2026-10-29' for row in result['tickets']))

    def test_daily_totals_equal_overall_totals_and_days_are_latest_first(self):
        first=entry('day1');second=entry('day2',day='2026-10-29',status='pending')
        for fixture in second['fixtures']:
            fixture['match_id']=str(int(fixture['match_id'])+500)
        result=self.build([first,second],stake=.1)
        self.assertEqual([r['day'] for r in result['days']],['2026-10-29','2026-10-28'])
        for field in ('bets','won','lost','pending','unresolved','refunded','stake_total','stake_settled','stake_open',
                      'stake_refunded','stake_resolved','returned','pnl'):
            self.assertAlmostEqual(sum(r[field] for r in result['days']),result['summary'][field])

    def test_round_once_after_full_scenario_product_and_include_all_twenty_legs(self):
        result=self.build([entry(size=3)],stake=.1)
        won=next(r for r in result['tickets'] if r['status']=='won')
        self.assertEqual(won['return_amount'],.93)
        result=self.build([entry(size=20)],stake=1)
        won=next(r for r in result['tickets'] if r['status']=='won')
        self.assertEqual(won['return_amount'],float((Decimal('2.1')**20).quantize(Decimal('.01'))))
        self.assertEqual(len(won['sequence']),20)
        self.assertNotEqual(won['odds'],float(Decimal('2.1')**6))

    def test_invalid_stake_and_naive_clock_raise_clear_errors_and_empty_summary_is_zero(self):
        for stake in (True,0,-1,float('nan'),float('inf'),'oops',.001):
            with self.subTest(stake=stake),self.assertRaises(ValueError):
                self.build([],stake=stake)
        with self.assertRaises(ValueError):simulation.build([],now=datetime(2026,10,28))
        result=self.build([])
        self.assertEqual(result['tickets'],[]);self.assertEqual(result['days'],[])
        self.assertEqual(result['summary']['pnl'],0);self.assertIsNone(result['summary']['roi'])

    def test_input_is_unchanged_and_current_models_network_and_recording_are_never_used(self):
        saved=entry(); before=deepcopy(saved)
        with (patch('prospective_models.build_methods',side_effect=AssertionError('No fitting')),
              patch('prediction_journal.record',side_effect=AssertionError('No storage')),
              patch('api_football.ApiFootballClient.get',side_effect=AssertionError('No API'))):
            self.build([saved])
        self.assertEqual(saved,before)


if __name__=='__main__':
    unittest.main()
