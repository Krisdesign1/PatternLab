from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from math import fsum, isclose
import unittest
from unittest.mock import patch

import prospective_models as models
import scenario_group_contract as contract


CREATED = '2026-10-31T10:00:00+00:00'
DECISION = '2026-10-31T09:00:00+00:00'
BEFORE = '2026-10-31'


def fixtures(size=2, *, mixed=False):
    first = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    return [dict(match_id=str(1000+index), league='61' if mixed and index % 2 else '39',
                 provider='api-football', home_id=str(10+2*index), away_id=str(11+2*index),
                 home='Home '+str(index), away='Away '+str(index), season=2026,
                 timestamp=(first+timedelta(minutes=index)).replace(tzinfo=None).isoformat())
            for index in range(size)]


def independent(chosen, *, component=False, before=BEFORE, decision=DECISION):
    context = models._context(chosen, before, decision)
    return models._method('clubs-test-v1',
        models._distribution('independent', len(chosen),
                             distributions=[{'1st':.2, 'Égal':.3, '2nd':.5} for _ in chosen]),
        dict(source='API-Football', league=chosen[0]['league'], **context), _component=component)


def entry(size=2, *, mixed=False):
    chosen = fixtures(size, mixed=mixed)
    if not mixed:
        method = independent(chosen)
    else:
        leagues = sorted({row['league'] for row in chosen})
        components = []
        for league in leagues:
            indices = [index for index,row in enumerate(chosen) if row['league'] == league]
            selected = [chosen[index] for index in indices]
            components.append(dict(indices=indices, league=league,
                                   method=independent(selected, component=len(selected)==1)))
        method = models._method('mixed-test-v1',
            dict(schema=models.DISTRIBUTION_VERSION, type='block_product', size=size, components=components),
            dict(source='API-Football', provider='api-football', league='mixed',
                 assumes_block_independence=True, **models._context(chosen, BEFORE, DECISION)))
    return dict(id='saved-group', source='API-Football', size=size, created_at=CREATED,
                fixtures=chosen, methods={'clubs':method})


class GroupContractTests(unittest.TestCase):
    def test_all_sizes_keep_absolute_mass_without_outcome_enumeration(self):
        for size in range(2,21):
            value = contract.inspect(entry(size))
            with self.subTest(size=size):
                self.assertEqual(value['status'], 'valid', value['issues'])
                self.assertEqual(value['common_cutoff'], BEFORE)
                self.assertEqual(value['decision_at'], DECISION)
                method = value['methods'][0]
                self.assertAlmostEqual(method['top1_probability'], .5**size)
                self.assertAlmostEqual(method['top3_probability'], .5**size+2*.3*.5**(size-1))
                self.assertAlmostEqual(method['top3_probability']+method['remaining_probability'], 1.)
                self.assertGreater(method['remaining_probability'], 0.)

    def test_mixed_components_include_singletons_and_have_one_common_decision(self):
        for size in (2,3,4,20):
            value = contract.inspect(entry(size,mixed=True))
            with self.subTest(size=size):
                self.assertEqual(value['status'], 'valid', value['issues'])
                self.assertIn('Indépendance supposée entre les blocs de compétitions.', value['methods'][0]['assumptions'])

    def test_legacy_missing_binding_is_unverified_never_silently_repaired(self):
        original = entry()
        method = original['methods']['clubs']
        for key in ('ordered_match_ids','fixture_fingerprint','decision_at'):
            method.pop(key)
        before = deepcopy(original)
        value = contract.inspect(original)
        self.assertEqual(value['status'],'unverified')
        self.assertEqual(original,before)
        self.assertEqual({row['code'] for row in value['issues']},
                         {'decision_unverified','binding_unverified','fingerprint_unverified'})
        self.assertEqual(len(value['methods']),1)

    def test_old_context_fingerprint_without_ordered_binding_stays_unknown(self):
        original = entry()
        original['methods']['clubs'].pop('ordered_match_ids')
        original['methods']['clubs']['fixture_fingerprint']='old-context-engine-convention'
        value = contract.inspect(original)
        self.assertEqual(value['status'],'unverified')
        self.assertIn('fingerprint_scope_unverified',{row['code'] for row in value['issues']})

    def test_contradictory_saved_bindings_are_invalid(self):
        for field,value in [('ordered_match_ids',['1001','1000']),
                            ('fixture_fingerprint','f'*64)]:
            original=entry(); original['methods']['clubs'][field]=value
            with self.subTest(field=field):
                self.assertEqual(contract.inspect(original)['status'],'invalid')

    def test_duplicate_fixtures_wrong_order_wrong_size_and_scores_are_invalid(self):
        for problem in ('duplicate','order','size','boolid','score','source'):
            original=entry()
            if problem=='duplicate':original['fixtures'][1]['match_id']='1000'
            elif problem=='order':original['fixtures'].reverse()
            elif problem=='size':original['size']=3
            elif problem=='boolid':original['fixtures'][0]['match_id']=True
            elif problem=='score':original['fixtures'][0]['ht']='0-0'
            else:original['fixtures'][0]['provider']='other'
            with self.subTest(problem=problem):
                self.assertEqual(contract.inspect(original)['status'],'invalid')

    def test_future_cutoff_is_invalid_even_if_match_is_tomorrow(self):
        original=entry()
        original['methods']['clubs']['training_before']='2026-11-01'
        value=contract.inspect(original)
        self.assertEqual(value['status'],'invalid')
        self.assertIn('cutoff_after_decision_or_match',{row['code'] for row in value['issues']})

    def test_methods_require_one_cutoff_and_one_decision(self):
        for problem in ('cutoff','decision'):
            original=entry(); original['methods']['frequency']=deepcopy(original['methods']['clubs'])
            original['methods']['frequency']['training_before' if problem=='cutoff' else 'decision_at']=(
                '2026-10-30' if problem=='cutoff' else '2026-10-31T09:01:00+00:00')
            value=contract.inspect(original)
            with self.subTest(problem=problem):
                self.assertEqual(value['status'],'invalid')
                self.assertIn('common_cutoff_mismatch' if problem=='cutoff' else 'common_decision_mismatch',
                              {row['code'] for row in value['issues']})

    def test_missing_component_decision_remains_unverified_not_parent_mismatch(self):
        original=entry(4,mixed=True)
        original['methods']['clubs']['distribution']['components'][0]['method'].pop('decision_at')
        value=contract.inspect(original)
        self.assertEqual(value['status'],'unverified')
        self.assertIn('decision_unverified',{row['code'] for row in value['issues']})
        self.assertNotIn('component_decision_mismatch',{row['code'] for row in value['issues']})

    def test_component_wrong_cutoff_and_decision_are_invalid(self):
        for problem in ('cutoff','decision','positions'):
            original=entry(4,mixed=True)
            block=original['methods']['clubs']['distribution']['components'][0]
            if problem=='cutoff':block['method']['training_before']='2026-10-30'
            elif problem=='decision':block['method']['decision_at']='2026-10-31T09:01:00+00:00'
            else:block['method']['ordered_match_ids'].reverse()
            value=contract.inspect(original)
            with self.subTest(problem=problem):
                self.assertEqual(value['status'],'invalid')
                if problem=='decision':
                    self.assertIn('component_decision_mismatch',{row['code'] for row in value['issues']})

    def test_late_future_or_naive_decision_is_invalid(self):
        for decision in ('2026-10-31T10:01:00Z','2026-11-01T12:00:00Z','2026-10-31T09:00:00'):
            original=entry(); original['methods']['clubs']['decision_at']=decision
            with self.subTest(decision=decision):
                self.assertEqual(contract.inspect(original)['status'],'invalid')

    def test_saved_model_training_and_selection_periods_cannot_cross_cutoff(self):
        for field, value in [('trained_through',BEFORE),('artifact_cutoff','2026-11-01'),
                             ('selection_through',BEFORE)]:
            original=entry();original['methods']['clubs'][field]=value
            with self.subTest(field=field):
                self.assertEqual(contract.inspect(original)['status'],'invalid')
        original=entry();original['methods']['clubs'].update(trained_through='2026-10-30',
            artifact_cutoff=BEFORE,selection_through='2026-10-30')
        self.assertEqual(contract.inspect(original)['status'],'valid')

    def test_missing_created_time_is_unverified_and_known_late_time_invalid(self):
        original=entry();original.pop('created_at')
        self.assertEqual(contract.inspect(original)['status'],'unverified')
        original['created_at']='2026-11-01T12:01:00+00:00'
        self.assertEqual(contract.inspect(original)['status'],'invalid')

    def test_bad_top_three_or_distribution_is_never_renormalized(self):
        for problem in ('mass','rank','duplicate','nan'):
            original=entry();method=original['methods']['clubs']
            if problem=='mass':method['distribution']['distributions'][0]['2nd']=.9
            elif problem=='rank':method['ranking'][0]['probability']=.99
            elif problem=='duplicate':method['ranking'][1]=deepcopy(method['ranking'][0])
            else:method['ranking'][0]['probability']=float('nan')
            value=contract.inspect(original)
            with self.subTest(problem=problem):
                self.assertEqual(value['status'],'invalid')
                self.assertFalse(value['methods'])

    def test_result_updates_do_not_change_contract_input_fingerprint(self):
        original=entry(); before=contract.inspect(original)
        original.update(status='settled',actual=['2nd','2nd'],result_history=[{'anything':'new'}])
        after=contract.inspect(original)
        self.assertEqual(before,after)

    def test_no_io_api_model_fit_or_reinference_occurs(self):
        import api_football as api
        import causal_candidate
        import catboost_scenarios as ml
        original=entry()
        with patch.object(api.ApiFootballClient,'get',side_effect=AssertionError('No API')), \
                patch.object(ml,'forecast',side_effect=AssertionError('No model inference')), \
                patch.object(causal_candidate,'fit_candidate',side_effect=AssertionError('No fit')):
            self.assertEqual(contract.inspect(original)['status'],'valid')

    def test_record_guard_rejects_contradictions_and_returns_unknown_legacy(self):
        original=entry()
        value=contract.require_recordable(original['fixtures'],original['methods'],original['created_at'])
        self.assertEqual(value['status'],'valid')
        original['methods']['clubs']['ordered_match_ids'].reverse()
        with self.assertRaises(ValueError):
            contract.require_recordable(original['fixtures'],original['methods'],original['created_at'])
        original=entry();original['methods']['clubs'].pop('ordered_match_ids')
        value=contract.require_recordable(original['fixtures'],original['methods'],original['created_at'])
        self.assertEqual(value['status'],'unverified')

    def test_malformed_input_returns_visible_invalid_without_crashing(self):
        for original in (None,[],{},dict(size=True),dict(size=2,fixtures='bad',methods=[])):
            with self.subTest(original=original):
                self.assertEqual(contract.inspect(original)['status'],'invalid')

    def test_invalid_nonfinite_identity_and_size_still_export_as_finite_json(self):
        original=entry()
        original.update(id=float('nan'),size=float('nan'))
        value=contract.inspect(original)
        self.assertEqual(value['status'],'invalid')
        self.assertIsNone(value['entry_id']);self.assertIsNone(value['size'])
        self.assertIsNone(value['input_sha256'])
        json.dumps(value,allow_nan=False)


if __name__=='__main__':
    unittest.main()
