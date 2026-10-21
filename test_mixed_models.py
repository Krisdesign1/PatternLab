"""Exact mixed-block products, singleton semantics and one causal cutoff."""
import copy
from itertools import product
from math import fsum
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
from league_model import VALUES
import prospective_models as models
from test_catboost_scenarios import sample, fixtures


def mixed_fixtures(size=4):
    result=[]
    base=fixtures()
    for index in range(size):
        result.append(dict(base[index%4],match_id=str(9000+index),league='39' if index%2==0 else '61',
                           timestamp=f'2025-03-20T12:{index:02d}:00'))
    return result


def mixed_history(count=240):
    return sample(count)+[dict(row,match_id=str(int(row['match_id'])+10000)) for row in sample(count,league='61')]


class MixedModelsTests(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.object(ml,'load_report',return_value=None),
                        patch.object(models,'_context_neighbors',side_effect=models.SnapshotError('Configuration absente.'))):
            patcher.start();self.addCleanup(patcher.stop)

    def test_actual_blocks_normalize_and_rank_exactly_with_interleaved_leagues(self):
        chosen=mixed_fixtures()
        original=copy.deepcopy(chosen)
        frozen=models.build_mixed_methods(mixed_history(),chosen)
        self.assertEqual(set(frozen['methods']),{'frequency','clubs','combined'})
        self.assertEqual(frozen['leagues'],['39','61'])
        for payload in frozen['methods'].values():
            self.assertEqual(payload['league'],'mixed')
            self.assertTrue(payload['assumes_block_independence'])
            self.assertEqual(payload['correction_scope'],'within_each_league_only')
            components=payload['distribution']['components']
            self.assertEqual([part['indices'] for part in components],[[0,2],[1,3]])
            values={seq:models.probability_for_sequence(payload,seq) for seq in product(VALUES,repeat=4)}
            self.assertAlmostEqual(fsum(values.values()),1)
            expected=sorted(values,key=lambda seq:(-values[seq],seq))[:3]
            self.assertEqual([tuple(row['sequence']) for row in payload['ranking']],expected)
            for sequence in values:
                chance=1
                for part in components:
                    chance*=models.probability_for_sequence(part['method'],[sequence[i] for i in part['indices']])
                self.assertAlmostEqual(values[sequence],chance)
            models.validate_method(payload,4)
        self.assertEqual(chosen,original)

    def test_singletons_only_supply_explicit_clubs_and_external_size_one_stays_invalid(self):
        frozen=models.build_mixed_methods(mixed_history(40),mixed_fixtures(2))
        self.assertEqual(set(frozen['methods']),{'clubs'})
        self.assertEqual(set(frozen['unavailable']),set(models.METHODS)-{'clubs'})
        for reason in frozen['unavailable'].values():self.assertIn('Une seule affiche',reason)
        payload=frozen['methods']['clubs']
        self.assertEqual(payload['distribution']['components'][0]['method']['distribution']['type'],'independent')
        self.assertAlmostEqual(fsum(models.probability_for_sequence(payload,seq) for seq in product(VALUES,repeat=2)),1)
        leaf=payload['distribution']['components'][0]['method']
        with self.assertRaises(ValueError):models.validate_method(leaf,1)
        with self.assertRaises(ValueError):models.probability_for_sequence(leaf,['1st'])
        insufficient=models.build_mixed_methods(mixed_history(20),mixed_fixtures(2))
        self.assertEqual(insufficient['methods'],{})
        self.assertIn('30 résultats',insufficient['unavailable']['clubs'])

    def test_twenty_singleton_blocks_have_bounded_exact_top_three_and_stable_ties(self):
        chosen=mixed_fixtures(20)
        for index,row in enumerate(chosen):row['league']=str(100+index)
        def component(rows,fixtures,league,before):
            positions=[dict(zip(VALUES,(1/3,1/3,1/3)))]
            payload=models._method('clubs-single',models._distribution('independent',1,distributions=positions),
                dict(source=models.SOURCE,league=league,training_before=before),_component=True)
            return dict(methods={'clubs':payload},unavailable={})
        with patch.object(models,'_mixed_component',side_effect=component):
            frozen=models.build_mixed_methods([],chosen)
        payload=frozen['methods']['clubs']
        self.assertEqual([row['sequence'] for row in payload['ranking']],
                         [['1st']*20,['1st']*19+['2nd'],['1st']*19+['Égal']])
        self.assertAlmostEqual(payload['ranking'][0]['probability'],(1/3)**20)
        self.assertEqual(len(payload['distribution']['components']),20)

    def test_every_component_uses_the_whole_group_first_utc_day_and_no_future_scores(self):
        chosen=mixed_fixtures()
        chosen[0]['timestamp']='2025-03-19T23:10:00'
        rows=mixed_history()
        future=[dict(rows[0],match_id='20001',timestamp='2025-03-19T12:00:00',league='39',ht=None,ft=None,value='bad'),
                dict(rows[0],match_id='20002',timestamp='2025-03-19T12:00:00',league='61',ht=None,ft=None,value='bad')]
        original=models.build_mixed_methods(rows,chosen)
        changed=models.build_mixed_methods(rows+future,chosen)
        self.assertEqual(original,changed)
        for payload in original['methods'].values():
            self.assertEqual(payload['training_before'],'2025-03-19')
            for component in payload['distribution']['components']:
                self.assertEqual(component['method']['training_before'],'2025-03-19')

    def test_partition_and_saved_ranking_corruption_are_rejected_without_current_models(self):
        payload=models.build_mixed_methods(mixed_history(),mixed_fixtures())['methods']['combined']
        for issue in ('duplicate','missing','bool','nested','rank'):
            value=copy.deepcopy(payload)
            if issue=='duplicate':value['distribution']['components'][1]['indices']=[0,3]
            elif issue=='missing':value['distribution']['components'].pop()
            elif issue=='bool':value['distribution']['components'][0]['indices']=[False,2]
            elif issue=='nested':value['distribution']['components'][0]['method']=copy.deepcopy(payload)
            else:value['ranking'][0]['probability']*=.5
            with self.subTest(issue=issue),self.assertRaises(ValueError):models.validate_method(value,4)
        expected=models.probability_for_sequence(payload,['Égal']*4)
        with patch.object(models,'_mixed_component',side_effect=AssertionError('No inference on frozen JSON')):
            self.assertEqual(models.probability_for_sequence(payload,['Égal']*4),expected)

    def test_explicit_single_league_mixed_builder_does_not_change_default_strict_api(self):
        chosen=fixtures()
        value=models.build_mixed_methods(sample(),chosen)
        self.assertEqual(value['league'],'mixed')
        self.assertEqual(value['leagues'],['39'])
        self.assertEqual(len(value['methods']['clubs']['distribution']['components']),1)
        with self.assertRaises(ValueError):models.build_methods(mixed_history(),mixed_fixtures())
        with self.assertRaises(ValueError):models.build_mixed_methods(mixed_history(),mixed_fixtures(),'other')
        with self.assertRaises(ValueError):models.build_mixed_methods(mixed_history(),list(reversed(mixed_fixtures())))


if __name__=='__main__':unittest.main()
