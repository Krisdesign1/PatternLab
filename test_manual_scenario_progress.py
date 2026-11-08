"""Partial scenario diagnostics never alter complete-group settlement.

Only isolated values and the archive's pure evaluator are used: no database,
provider, training or production workspace is accessed.
"""
from copy import deepcopy
import unittest

import manual_analysis_store as archive
import manual_scenario_progress as progress
from test_manual_journal_rows import entry as archived_entry, raw, NOW


def saved(size=5, *, sequences=None):
    value = archived_entry(size=size)
    if sequences is None:
        return value
    value['forecast']['ranking'] = [dict(sequence=list(sequence), probability=probability)
        for sequence, probability in zip(sequences, (.3, .2, .1))]
    # Reconstruct a valid immutable identity rather than accepting a snapshot
    # whose saved content and semantic digest disagree.
    snapshot = archive._snapshot(value, NOW)
    return dict(snapshot, id=archive._digest(archive._semantic(snapshot)), created_at=NOW.isoformat())


def observed(value, results, *, status='pending', actual=None):
    rows = [dict(match_id=fixture['match_id'], status='settled' if label else 'pending', actual=label)
            for fixture, label in zip(value['fixtures'], results)]
    return dict(status=status, actual=actual, top1=None, top3=None, matches=rows,
                scenario_verdicts=[dict(item, rank=index, matches_all=None)
                    for index, item in enumerate(value['forecast']['ranking'], 1)])


class ManualScenarioProgressTests(unittest.TestCase):
    def test_screenshot_five_fixtures_four_results_all_three_already_contradicted(self):
        sequences = [['2nd']*5,
                     ['2nd', '1st', '2nd', '2nd', '2nd'],
                     ['2nd', '2nd', '1st', '2nd', '2nd']]
        value = saved(sequences=sequences)
        raw_rows = [raw(value['fixtures'][0], ht=(2,1), ft=(3,2)),
                    raw(value['fixtures'][1], ht=(1,1), ft=(3,3)),
                    raw(value['fixtures'][2], ht=(1,0), ft=(1,0)),
                    raw(value['fixtures'][3], ht=(0,0), ft=(0,3))]
        evaluation = archive.evaluate(value, raw_rows, now=NOW.replace(hour=16))
        before = deepcopy((value, evaluation))
        result = progress.project(value, evaluation)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['actual'], ['1st', '2nd', '1st', '2nd', None])
        self.assertEqual((result['known_count'], result['total']), (4,5))
        self.assertEqual(result['progress_label'], '4/5 résultats confirmés')
        self.assertEqual(result['group_label'], 'Groupe incomplet')
        self.assertEqual(result['contradicted_count'], 3)
        self.assertEqual([row['state'] for row in result['rows']], ['contradicted']*3)
        self.assertTrue(all(1 in row['contradicted_positions'] for row in result['rows']))
        self.assertIn('Tous les scénarios', result['summary'])
        self.assertIsNone(evaluation['top1']); self.assertIsNone(evaluation['top3'])
        self.assertTrue(all(row['matches_all'] is None for row in evaluation['scenario_verdicts']))
        self.assertEqual((value,evaluation), before)

    def test_partial_compatible_scenario_stays_possible_without_hit_or_settlement(self):
        value = saved(size=3)
        evaluation = observed(value, ['1st', None, None])
        result = progress.project(value, evaluation)
        self.assertEqual([row['state'] for row in result['rows']],
                         ['possible', 'contradicted', 'contradicted'])
        self.assertEqual(result['rows'][0]['contradicted_positions'], [])
        self.assertIn('2 résultat(s)', result['rows'][0]['reason'])
        self.assertNotIn('hit', {row['state'] for row in result['rows']})
        self.assertNotIn('miss', {row['state'] for row in result['rows']})
        self.assertIsNone(evaluation['top1']); self.assertIsNone(evaluation['top3'])
        self.assertNotIn('matches_all', result['rows'][0])

    def test_no_confirmed_results_ignore_stale_complete_actuals(self):
        value = saved(size=3)
        evaluation = observed(value, [None]*3, actual=['1st']*3)
        result = progress.project(value,evaluation)
        self.assertEqual(result['actual'], [None]*3)
        self.assertEqual(result['known_count'], 0)
        self.assertEqual({row['state'] for row in result['rows']}, {'pending'})
        self.assertEqual(result['contradicted_count'],0)

    def test_void_group_keeps_void_diagnostic_even_with_a_known_contradiction(self):
        value = saved(size=2)
        evaluation = observed(value, ['1st', None], status='void')
        evaluation['matches'][1]['status'] = 'void'
        result = progress.project(value,evaluation)
        self.assertEqual(result['known_count'],1)
        self.assertEqual(result['group_label'],'Groupe annulé du suivi')
        self.assertEqual({row['state'] for row in result['rows']},{'void'})
        self.assertFalse(any(row['state'] in ('hit','miss') for row in result['rows']))

    def test_non_evaluable_preserves_confirmed_diagnostics_without_counting_invalid_match(self):
        value=saved(size=2)
        evaluation=observed(value,['1st','2nd'],status='non_evaluable',actual=['1st','2nd'])
        evaluation['matches'][1]['status']='non_evaluable'
        result=progress.project(value,evaluation)
        self.assertEqual(result['actual'],['1st',None])
        self.assertEqual(result['known_count'],1)
        self.assertEqual([row['state'] for row in result['rows']],
                         ['non_evaluable','contradicted','contradicted'])
        self.assertEqual(result['group_label'],'Groupe à vérifier')
        self.assertIsNone(evaluation['top1']);self.assertIsNone(evaluation['top3'])

    def test_unavailable_catalogue_ignores_all_stale_per_match_and_complete_outcomes(self):
        value=saved(size=2)
        for status in ('unavailable','invalid-status'):
            with self.subTest(status=status):
                evaluation=observed(value,['1st','2nd'],status=status,actual=['1st','2nd'])
                result=progress.project(value,evaluation)
                self.assertEqual(result['status'],'unavailable')
                self.assertEqual(result['actual'],[None,None])
                self.assertEqual(result['known_count'],0)
                self.assertEqual({row['state'] for row in result['rows']},{'unavailable'})

    def test_complete_verified_hit_and_miss_keep_saved_rank_order_and_probability(self):
        value=saved(size=3)
        expected=[('hit',['1st']*3,['hit','miss','miss']),
                  ('miss',['1st','2nd','Égal'],['miss']*3)]
        for name,actual,states in expected:
            with self.subTest(name=name):
                evaluation=observed(value,actual,status='settled',actual=actual)
                result=progress.project(value,evaluation)
                self.assertEqual(result['known_count'],3)
                self.assertEqual([row['state'] for row in result['rows']],states)
                self.assertEqual([row['rank'] for row in result['rows']],[1,2,3])
                self.assertEqual([row['probability'] for row in result['rows']],
                                 [row['probability'] for row in value['forecast']['ranking']])
                self.assertEqual(result['group_label'],'Groupe évalué')

    def test_legacy_complete_verified_actual_can_supply_missing_per_match_detail(self):
        value=saved(size=2)
        result=progress.project(value,dict(status='settled',actual=['2nd','2nd']))
        self.assertEqual([row['state'] for row in result['rows']],['miss','miss','hit'])
        self.assertEqual(result['known_count'],2)

    def test_settled_flag_cannot_override_explicit_incomplete_or_duplicated_detail(self):
        value=saved(size=2)
        cases=[]
        empty=observed(value,[],status='settled',actual=['1st','1st'])
        cases.append(empty)
        incomplete=observed(value,['1st',None],status='settled',actual=['1st','1st'])
        cases.append(incomplete)
        duplicate=observed(value,['1st','1st'],status='settled',actual=['1st','1st'])
        duplicate['matches'].append(deepcopy(duplicate['matches'][0]))
        cases.append(duplicate)
        for evaluation in cases:
            with self.subTest(evaluation=evaluation):
                result=progress.project(value,evaluation)
                self.assertEqual(result['status'],'unavailable')
                self.assertEqual(result['actual'],[None,None])
                self.assertEqual(result['known_count'],0)
                self.assertEqual({row['state'] for row in result['rows']},{'unavailable'})

    def test_explicit_confirmed_details_take_precedence_over_stale_complete_actual(self):
        value=saved(size=2)
        evaluation=observed(value,['2nd','2nd'],status='settled',actual=['1st','1st'])
        result=progress.project(value,evaluation)
        self.assertEqual(result['actual'],['2nd','2nd'])
        self.assertEqual([row['state'] for row in result['rows']],['miss','miss','hit'])

    def test_later_correction_can_remove_contradiction_without_mutating_saved_calculation(self):
        value=saved(size=3)
        evaluation=observed(value,['1st',None,None])
        original=deepcopy(value)
        contradicted=progress.project(value,evaluation)
        corrected=deepcopy(evaluation)
        corrected['matches'][0]['actual']='2nd'
        second=progress.project(value,corrected)
        self.assertEqual(contradicted['rows'][2]['state'],'contradicted')
        self.assertEqual(second['rows'][2]['state'],'possible')
        self.assertEqual(second['rows'][2]['contradicted_positions'],[])
        second['rows'][2]['sequence'][0]='Égal'
        self.assertEqual(value,original)
        self.assertEqual(evaluation['matches'][0]['actual'],'1st')

    def test_all_supported_group_sizes_keep_positions_and_never_truncate(self):
        for size in range(2,21):
            with self.subTest(size=size):
                value=saved(size=size)
                results=['1st']*(size-1)+[None]
                result=progress.project(value,observed(value,results))
                self.assertEqual((result['known_count'],result['total']),(size-1,size))
                self.assertEqual(len(result['actual']),size)
                self.assertEqual([len(row['sequence']) for row in result['rows']],[size]*3)
                self.assertEqual(result['rows'][2]['contradicted_positions'],list(range(1,size)))

    def test_duplicate_or_unconfirmed_per_match_observations_never_supply_known_labels(self):
        value=saved(size=3)
        evaluation=observed(value,['1st','2nd','Égal'],actual=['1st','2nd','Égal'])
        evaluation['matches'].append(deepcopy(evaluation['matches'][0]))
        evaluation['matches'][1]['status']='pending'
        evaluation['matches'][2]['status']='non_evaluable'
        result=progress.project(value,evaluation)
        self.assertEqual(result['actual'],[None,None,None])
        self.assertEqual(result['known_count'],0)
        self.assertEqual({row['state'] for row in result['rows']},{'pending'})

    def test_invalid_saved_sequence_is_rejected_instead_of_partial_zip_success(self):
        value=saved(size=3)
        for sequence in (['1st'],['1st','unknown','2nd']):
            with self.subTest(sequence=sequence):
                invalid=deepcopy(value)
                invalid['forecast']['ranking'][0]['sequence']=sequence
                with self.assertRaises(ValueError):progress.project(invalid,observed(value,['1st',None,None]))


if __name__=='__main__':unittest.main()
