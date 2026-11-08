from copy import deepcopy
from itertools import product
from math import fsum
import unittest
from unittest.mock import patch

from league_model import VALUES
import prospective_models as models
from scenario_predictor import fit_sequences
import group_probability_diagnostics as diagnostics
from test_saved_model_comparison import entry


class FrozenGroupDiagnosticsTests(unittest.TestCase):
    def test_adaptive_selection_keeps_original_three_reference_mass(self):
        from adaptive_scenario_selection import select, VERSION
        base=[{'1st':.5,'Égal':.3,'2nd':.2}, {'1st':.45,'Égal':.35,'2nd':.2}]
        original=models._method('test-v1',models._distribution('independent',2,distributions=base),{})
        expected=diagnostics.inspect(original)
        for threshold in (.2, .15, .09):
            method=deepcopy(original)
            method.update(reference_ranking=deepcopy(original['ranking']), ranking=select(original,threshold),
                          proposal_policy=dict(version=VERSION,threshold=threshold))
            result=diagnostics.inspect(method)
            self.assertAlmostEqual(result['top3_probability'],expected['top3_probability'])
            self.assertEqual(result['proposed_count'],len(method['ranking']))
            self.assertAlmostEqual(result['proposed_probability'],sum(row['probability'] for row in method['ranking']))
            self.assertEqual(result['top1_match_probabilities'],expected['top1_match_probabilities'])

    def test_extended_explicit_choices_do_not_expand_historical_top_three_mass(self):
        from coupon_sizes import top_independent
        base=[{'1st':.5,'Égal':.3,'2nd':.2}, {'1st':.45,'Égal':.35,'2nd':.2}]
        method=models._method('test-v1',models._distribution('independent',2,distributions=base),{})
        original=diagnostics.inspect(method)
        method['ranking']=top_independent(base,6)
        result=diagnostics.inspect(method)
        self.assertEqual(result['proposed_count'],6)
        self.assertAlmostEqual(result['top3_probability'], original['top3_probability'])
        self.assertAlmostEqual(result['proposed_probability'],sum(row['probability'] for row in method['ranking']))
        self.assertGreater(result['proposed_probability'],result['top3_probability'])

    def test_exact_scenario_mass_is_distinct_from_each_match_probability(self):
        method=entry()['methods']['clubs']; before=deepcopy(method)
        result=diagnostics.inspect(method)
        self.assertEqual(result['size'],2)
        self.assertEqual(result['possible_scenarios'],9)
        self.assertAlmostEqual(result['top1_probability'],.5*.6)
        self.assertEqual(result['top1_sequence'],['2nd','1st'])
        self.assertEqual([row['probability'] for row in result['top1_match_probabilities']],[.5,.6])
        self.assertAlmostEqual(result['top3_probability'],fsum(row['probability'] for row in method['ranking']))
        self.assertAlmostEqual(result['top3_probability']+result['remaining_probability'],1)
        self.assertIn('Indépendance',result['assumptions'][0])
        self.assertIn('tailles différentes',result['size_warning'])
        self.assertEqual(method,before)

    def test_corrected_frequency_and_context_mass_matches_the_saved_joint(self):
        fit=fit_sequences([('1st','2nd'),('2nd','Égal'),('2nd','2nd')],2)
        base=[dict(zip(VALUES,(.3,.2,.5))),dict(zip(VALUES,(.1,.6,.3)))]
        _,_,normalization=models._normalization(fit,base,models.PRIOR_WEIGHT,.5)
        distributions=[models._distribution('frequency',2,fit=fit),
            models._distribution('corrected',2,fit=fit,distributions=base,alpha=.5,normalization=normalization),
            dict(schema=models.DISTRIBUTION_VERSION,type='context_mixture',size=2,baseline=base,
                 eta=.25,local_mass=[dict(sequence=['1st','2nd'],probability=1)])]
        for distribution in distributions:
            method=models._method('test-v1',distribution,{})
            result=diagnostics.inspect(method)
            all_probabilities=sorted((models.probability(distribution,sequence)
                                     for sequence in product(VALUES,repeat=2)),reverse=True)
            self.assertAlmostEqual(result['top1_probability'],all_probabilities[0])
            self.assertAlmostEqual(result['top3_probability'],fsum(all_probabilities[:3]))
            self.assertFalse(any(text=='Indépendance supposée entre les résultats des matchs.'
                                 for text in result['assumptions']))

    def test_mixed_components_explain_block_independence_and_exact_top3(self):
        distributions=[dict(zip(VALUES,(.2,.3,.5))),dict(zip(VALUES,(.6,.3,.1)))]
        components=[]
        for index,probabilities in enumerate(distributions):
            method=models._method('component-v1',models._distribution('independent',1,
                distributions=[probabilities]),{},_component=True)
            components.append(dict(indices=[index],league=str(index+1),method=method))
        distribution=dict(schema=models.DISTRIBUTION_VERSION,type='block_product',size=2,components=components)
        method=models._method('mixed-v1',distribution,{})
        result=diagnostics.inspect(method)
        self.assertAlmostEqual(result['top1_probability'],.3)
        self.assertIn('blocs de compétitions',result['assumptions'][0])
        self.assertAlmostEqual(result['top3_probability'],sum(sorted(
            [a*b for a in distributions[0].values() for b in distributions[1].values()],reverse=True)[:3]))

    def test_twenty_matches_need_no_joint_enumeration_or_model_inference(self):
        base=[dict(zip(VALUES,(.2,.3,.5))) for _ in range(20)]
        method=models._method('test-v1',models._distribution('independent',20,distributions=base),{})
        with patch.object(models,'probability',side_effect=AssertionError('No exhaustive enumeration')), \
                patch('api_football.ApiFootballClient',side_effect=AssertionError('No API')), \
                patch('catboost_scenarios.forecast',side_effect=AssertionError('No model inference')):
            result=diagnostics.inspect(method)
        self.assertEqual(result['possible_scenarios'],3486784401)
        self.assertAlmostEqual(result['top1_probability'],.5**20)
        self.assertAlmostEqual(result['remaining_probability'],1-result['top3_probability'])
        self.assertEqual(result['size'],20)

    def test_tampered_ranking_is_not_described_as_exact_and_valid_methods_remain_visible(self):
        methods=entry()['methods']; methods['clubs']['ranking'][0]['probability']=.99
        report=diagnostics.inspect_methods(methods)
        self.assertNotIn('clubs',report['methods'])
        self.assertIn('clubs',report['unavailable'])
        self.assertEqual(set(report['methods']),{'frequency','catboost'})
        with self.assertRaises(ValueError):
            diagnostics.inspect({'ranking':[]})


if __name__=='__main__':
    unittest.main()
