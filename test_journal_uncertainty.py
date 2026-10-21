"""Prospective boundaries, connected dependencies and paired uncertainty."""
from datetime import datetime, timedelta, timezone
import copy
import json
import unittest
from unittest.mock import patch

import journal_protocol as protocol_store
import journal_uncertainty as uncertainty


START = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
NOW = datetime(2027, 1, 4, tzinfo=timezone.utc)


def protocol():
    value = dict(copy.deepcopy(protocol_store.CRITERIA), registered_at='2026-01-01T00:00:00+00:00')
    value['fingerprint'] = protocol_store._fingerprint(value)
    return value


def entry(index=0, *, week=None, loss_gain=1., hit_gain=1, status='settled', generation='generation-one'):
    stamp = START+timedelta(weeks=index if week is None else week)
    return dict(id=f'entry-{index:04}', league='39', size=2, status=status,
        created_at=(stamp-timedelta(days=1)).isoformat(),
        fixtures=[dict(match_id=str(10000+index*2+i), timestamp=(stamp+timedelta(hours=i)).replace(tzinfo=None).isoformat()) for i in range(2)],
        methods={name:dict(version='version-one', generation=generation) for name in ('clubs','combined')},
        evaluations={
            'clubs':dict(loss=4., top3=hit_gain < 0),
            'combined':dict(loss=4.-loss_gain, top3=hit_gain > 0),
        } if status == 'settled' else {})


def evaluate(entries, methods=('clubs','combined'), **kwargs):
    return uncertainty.evaluate(entries, methods, protocol(), now=kwargs.pop('now',NOW), **kwargs)


class UncertaintyTests(unittest.TestCase):
    def pair(self, entries, **kwargs):
        return evaluate(entries, **kwargs)['comparisons'][0]

    def test_empty_cohort_and_baseline_contract_never_invent_rates(self):
        result = evaluate([])
        self.assertEqual(result['prospective_count'],0)
        pair = result['comparisons'][0]
        self.assertEqual((pair['groups'],pair['blocks'],pair['status']),(0,0,'insufficient'))
        self.assertEqual(pair['delta_loss'],dict(estimate=None,lower=None,upper=None))
        self.assertEqual(evaluate([],methods=('clubs',))['comparisons'],[])
        for methods in (('combined',),('clubs','unknown'),('clubs','clubs')):
            with self.assertRaises(ValueError):evaluate([],methods=methods)

    def test_protocol_date_boundary_and_timezone_conversion_are_exact(self):
        rows=[entry(0),entry(1)]
        rows[0]['created_at']='2026-01-01T00:59:59+01:00'
        rows[1]['created_at']='2026-01-01T01:00:00+01:00'
        result=evaluate(rows)
        self.assertEqual((result['legacy_count'],result['prospective_count']),(1,1))
        self.assertEqual(result['comparisons'][0]['group_ids'],[rows[1]['id']])

    def test_many_groups_in_one_week_count_as_one_block(self):
        rows=[entry(index,week=0) for index in range(100)]
        result=self.pair(rows)
        self.assertEqual((result['groups'],result['blocks'],result['status']),(100,1,'insufficient'))
        self.assertIsNone(result['delta_loss']['lower'])

    def test_shared_fixture_links_weeks_and_connected_chain_is_transitive(self):
        rows=[entry(0),entry(1),entry(2),entry(3)]
        rows[1]['fixtures'][0]['match_id']=rows[0]['fixtures'][0]['match_id']
        rows[2]['fixtures'][1]['match_id']=rows[1]['fixtures'][1]['match_id']
        result=self.pair(rows)
        self.assertEqual(result['groups'],4)
        self.assertEqual(result['blocks'],2)

    def test_multiperiod_group_links_every_spanned_week_not_only_endpoints(self):
        rows=[entry(0),entry(1),entry(2)]
        rows[0]['fixtures'][1]['timestamp']=(START+timedelta(weeks=2,hours=1)).isoformat()
        self.assertEqual(self.pair(rows)['blocks'],1)

    def test_open_week_removes_whole_connected_component_at_utc_boundary(self):
        rows=[entry(0),entry(1)]
        rows[0]['fixtures'][1]['timestamp']=(START+timedelta(weeks=1,hours=1)).isoformat()
        during=START+timedelta(weeks=1,hours=6)
        result=self.pair(rows,now=during)
        self.assertEqual((result['groups'],result['blocks']),(0,0))
        self.assertEqual(result['excluded']['open_week'],2)
        # Sunday in Kinshasa-offset time is still UTC Sunday: not closed yet.
        sunday=datetime.fromisoformat('2026-01-19T00:30:00+01:00')
        self.assertEqual(self.pair(rows,now=sunday)['groups'],0)
        monday=datetime.fromisoformat('2026-01-19T00:00:00+00:00')
        self.assertEqual(self.pair(rows,now=monday)['groups'],2)

    def test_pending_bridge_blocks_settled_neighbors_until_all_results_arrive(self):
        rows=[entry(0),entry(1,status='pending'),entry(2)]
        rows[1]['fixtures'][0]['match_id']=rows[0]['fixtures'][0]['match_id']
        rows[1]['fixtures'][1]['match_id']=rows[2]['fixtures'][1]['match_id']
        result=self.pair(rows)
        self.assertEqual(result['excluded']['pending'],1)
        self.assertEqual(result['excluded']['incomplete_block'],2)
        self.assertEqual(result['groups'],0)
        rows[1]['status']='settled'
        rows[1]['evaluations']=copy.deepcopy(rows[0]['evaluations'])
        result=self.pair(rows)
        self.assertEqual((result['groups'],result['blocks']),(3,1))

    def test_unresolved_blocks_component_but_void_group_is_never_a_failure(self):
        settled=entry(0)
        unresolved=entry(1,week=0,status='unresolved')
        result=self.pair([settled,unresolved])
        self.assertEqual(result['groups'],0)
        self.assertEqual(result['excluded']['invalid'],1)
        self.assertEqual(result['excluded']['incomplete_block'],1)
        unresolved['status']='void'
        result=self.pair([settled,unresolved])
        self.assertEqual(result['groups'],1)
        self.assertEqual(result['excluded']['void'],1)

    def test_point_estimate_weights_groups_not_uniform_block_means(self):
        result=self.pair([entry(0,week=0,loss_gain=1),entry(1,week=0,loss_gain=1),entry(2,week=1,loss_gain=4)])
        self.assertEqual((result['groups'],result['blocks']),(3,2))
        self.assertAlmostEqual(result['delta_loss']['estimate'],2.)

    def test_paired_bootstrap_obeys_analytic_relation_and_is_order_independent(self):
        rows=[entry(i,loss_gain=1+2*(i%2),hit_gain=i%2) for i in range(20)]
        before=copy.deepcopy(rows)
        pair=self.pair(rows)
        self.assertEqual(pair['status'],'favorable')
        self.assertEqual((pair['groups'],pair['blocks']),(20,20))
        self.assertAlmostEqual(pair['delta_loss']['estimate'],2.)
        self.assertAlmostEqual(pair['delta_top3']['estimate'],.5)
        for key in ('lower','upper'):
            self.assertAlmostEqual(pair['delta_loss'][key],1+2*pair['delta_top3'][key])
        self.assertGreaterEqual(pair['delta_top3']['lower'],.25)
        self.assertLessEqual(pair['delta_top3']['upper'],.75)
        self.assertEqual(self.pair(list(reversed(rows))),pair)
        self.assertEqual(rows,before)

    def test_negative_and_mixed_gains_are_distinguished_from_favorable(self):
        negative=[entry(i,loss_gain=-1-(i%2),hit_gain=-(i%2)) for i in range(20)]
        self.assertEqual(self.pair(negative)['status'],'unfavorable')
        mixed=[entry(i,loss_gain=(-1 if i%2 else 1),hit_gain=(-1 if i%2 else 1)) for i in range(20)]
        self.assertEqual(self.pair(mixed)['status'],'inconclusive')

    def test_constant_or_degenerate_indicator_never_becomes_false_certainty(self):
        for rows in ([entry(i,loss_gain=1,hit_gain=1) for i in range(20)],
                     [entry(i,loss_gain=1+i%2,hit_gain=0) for i in range(20)]):
            pair=self.pair(rows)
            self.assertEqual(pair['status'],'inconclusive')
            self.assertIsNone(pair['delta_top3']['lower'])
            self.assertIsNotNone(pair['delta_loss']['estimate'])
        few=[entry(i,loss_gain=1+i%2,hit_gain=i%2) for i in range(19)]
        with patch.object(uncertainty,'_bootstrap',side_effect=AssertionError('Not enough blocks')):
            self.assertEqual(self.pair(few)['status'],'insufficient')

    def test_missing_methods_are_visible_and_do_not_impersonate_another_model(self):
        rows=[entry(0),entry(1)]
        del rows[1]['methods']['combined']
        pair=self.pair(rows)
        self.assertEqual(pair['excluded']['missing_methods'],1)
        self.assertEqual(pair['groups'],1)

    def test_invalid_scores_are_not_treated_as_success_or_failure(self):
        for value in (float('nan'),float('inf'),-1,True,'4',None):
            row=entry(1)
            row['evaluations']['combined']['loss']=value
            pair=self.pair([entry(0),row])
            self.assertEqual(pair['excluded']['invalid'],1)
            self.assertEqual(pair['groups'],1)
        row=entry(0);row['evaluations']['combined']['top3']=1
        self.assertEqual(self.pair([row])['excluded']['invalid'],1)

    def test_bad_fixture_metadata_or_nonprospective_registration_is_excluded(self):
        rows=[entry(i) for i in range(5)]
        rows[0]['fixtures'][0]['timestamp']='broken'
        rows[1]['fixtures'][0]['match_id']=True
        rows[2]['fixtures'][0]['match_id']=rows[2]['fixtures'][1]['match_id']
        rows[3]['created_at']=rows[3]['fixtures'][0]['timestamp']+'+00:00'
        rows[4]['created_at']='2028-01-01T00:00:00+00:00'
        result=self.pair(rows)
        self.assertEqual(result['groups'],0)
        self.assertEqual(result['excluded']['invalid'],5)

    def test_unknown_dependencies_fail_closed_even_with_twenty_other_blocks(self):
        rows=[entry(i,loss_gain=1+i%2,hit_gain=i%2) for i in range(20)]
        broken=entry(21);broken['fixtures'][0]['timestamp']='invalid'
        with patch.object(uncertainty,'_bootstrap',side_effect=AssertionError('Unknown dependencies')):
            pair=self.pair(rows+[broken])
        self.assertEqual((pair['groups'],pair['blocks']),(20,20))
        self.assertEqual(pair['status'],'insufficient')
        self.assertIsNone(pair['delta_loss']['lower'])
        self.assertEqual(pair['excluded']['invalid'],1)

    def test_mixed_sizes_competitions_versions_and_duplicate_groups_are_refused(self):
        for key,value in (('size',4),('league','61'),('id','entry-0000')):
            rows=[entry(0),entry(1)];rows[1][key]=value
            with self.assertRaises(ValueError):evaluate(rows)
        rows=[entry(0),entry(1,generation='different')]
        with self.assertRaisesRegex(ValueError,'versions'):evaluate(rows)
        with self.assertRaises(ValueError):evaluate([entry(0)],now=NOW.replace(tzinfo=None))
        changed=protocol();changed['min_blocks']=1
        with self.assertRaises(ValueError):uncertainty.evaluate([],('clubs','combined'),changed,now=NOW)

    def test_all_outputs_are_json_serializable_and_evaluation_never_reads_protocol_storage(self):
        with patch.object(protocol_store,'load_protocol',side_effect=AssertionError('No I/O')):
            result=evaluate([entry(0),entry(1,status='pending')])
        json.dumps(result,allow_nan=False)


if __name__ == '__main__':
    unittest.main()
