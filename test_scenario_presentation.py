"""Display adapters retain source probabilities and causal group boundaries."""
import copy
from math import prod
import unittest
from unittest.mock import patch

import contextual_scenarios as contextual
import prospective_models as models
import scenario_presentation as presentation
import scenario_tables as tables
from league_patterns import repeated_scenarios
from test_catboost_scenarios import sample,fixtures


def payload():
    rows=sample()
    chosen=fixtures()
    result=repeated_scenarios(rows,4)['39']
    forecast=contextual.forecast(result,chosen)
    return dict(forecast=forecast,fixtures=chosen,result=result,source='API-Football',engine='statistical')


class PresentationTests(unittest.TestCase):
    def test_three_methods_keep_exact_combined_ranking_and_input_unchanged(self):
        value=payload()
        before=copy.deepcopy(value)
        with patch('catboost_scenarios.train',side_effect=AssertionError('No fit')), \
             patch('catboost_scenarios.forecast',side_effect=AssertionError('No additional inference')):
            methods=presentation.forecast_methods(value)
        self.assertEqual(set(methods),{'frequency','clubs','combined'})
        for saved,supplied in zip(methods['combined']['ranking'],value['forecast']['ranking']):
            self.assertEqual(saved['sequence'],list(supplied['sequence']))
            self.assertAlmostEqual(saved['probability'],supplied['probability'])
        matrix=tables.build_matrix(methods)
        for row in matrix['rows']:
            expected=prod(item['probabilities'][label] for item,label in zip(value['forecast']['club_estimates'],row['sequence']))
            self.assertAlmostEqual(row['cells']['clubs']['probability'],expected)
        self.assertEqual(value,before)

    def test_blocks_after_first_target_day_are_excluded_from_comparison(self):
        value=payload()
        before=presentation.forecast_methods(value)
        value['result']['blocks'].append(dict(sequence=('1st',)*4,
            matches=[dict(timestamp='2025-03-20T20:00:00')]*4))
        self.assertEqual(presentation.forecast_methods(value),before)

    def test_learned_and_context_estimates_are_not_mislabeled_as_club_predictions(self):
        value=payload()
        for engine,key in (('catboost','catboost'),('context_neighbors','context_neighbors')):
            value['engine']=engine
            methods=presentation.forecast_methods(value)
            self.assertEqual(set(methods),{key})
            self.assertNotIn('distribution',methods[key])
            self.assertEqual(methods[key]['ranking'][0]['probability'],value['forecast']['ranking'][0]['probability'])

    def test_changed_or_truncated_ranking_is_refused_without_mutating_it(self):
        for change in ('truncated','mass','history'):
            value=payload()
            if change=='truncated':value['forecast']['ranking'].pop()
            elif change=='mass':value['forecast']['ranking'][0]['probability'] /= 2
            else:value['forecast']['training_groups']+=1
            before=copy.deepcopy(value)
            with self.assertRaises(ValueError):presentation.forecast_methods(value)
            self.assertEqual(value,before)


if __name__=='__main__':unittest.main()
