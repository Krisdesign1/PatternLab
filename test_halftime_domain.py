from copy import deepcopy
from itertools import product
import unittest

from halftime_domain import (CLASS_ORDER, ContractError, outcome, parse_score,
                             regulation_result, validate_probabilities)


class HalftimeContractTests(unittest.TestCase):
    def test_exhaustive_valid_scores_follow_the_declared_target(self):
        for home1, away1, home2, away2 in product(range(4), repeat=4):
            ht, ft = (home1,away1), (home1+home2,away1+away2)
            first, second = home1+away1, home2+away2
            expected = '1st' if first > second else '2nd' if second > first else 'Égal'
            result = regulation_result(ht,ft,status='FT')
            self.assertEqual((result.first_goals,result.second_goals,result.label), (first,second,expected))

    def test_zero_is_a_real_score_and_not_missing(self):
        self.assertEqual(outcome('0-0', '0-0'), 'Égal')
        for value in (None, '', {}, {'home':0,'away':None}, [], [0], [0,0,0],
                      [True,0], [0,False], [-1,0], [0.0,0], ['0',0], '(0-0', '0-0)'):
            with self.subTest(value=value), self.assertRaises(ContractError):
                outcome(value, '0-0')

    def test_cumulative_scores_are_checked_per_team_not_just_total(self):
        with self.assertRaisesRegex(ContractError, 'HT supérieur'):
            outcome((2,0), (1,3))

    def test_legacy_strings_api_mappings_and_pairs_have_identical_results(self):
        for value in ('  ( 1 - 0 ) ', {'home':1,'away':0}, (1,0), [1,0]):
            original = deepcopy(value)
            self.assertEqual(parse_score(value), (1,0))
            self.assertEqual(outcome(value,'2-2'), '2nd')
            self.assertEqual(value,original)

    def test_nonfinal_and_administrative_statuses_never_become_outcomes(self):
        for status in ('NS','TBD','1H','HT','2H','PST','CANC','SUSP','INT','ABD','AWD','WO','unknown', [], True):
            with self.subTest(status=status), self.assertRaises(ContractError) as caught:
                regulation_result('0-0','0-0',status=status)
            self.assertEqual(caught.exception.code,'MATCH_NOT_FINISHED')
        for status in ('FT','AET','PEN'):
            self.assertEqual(regulation_result('1-0','1-1',status=status).label,'Égal')

    def test_all_three_classes_are_explicit_and_probabilities_are_not_repaired(self):
        good = {'2nd':.48,'1st':.28,'Égal':.24}
        self.assertEqual(tuple(validate_probabilities(good)), CLASS_ORDER)
        self.assertEqual(good, {'2nd':.48,'1st':.28,'Égal':.24})
        invalid = [None, {}, {'1st':.5,'2nd':.5}, {**good,'unknown':0},
                   {'1st':28,'Égal':24,'2nd':48}, {'1st':.2,'Égal':.2,'2nd':.2}]
        for number in (float('nan'),float('inf'),-0.1,1.1,True,'0.28',None):
            invalid.append({**good,'1st':number})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_probabilities(value)


if __name__ == '__main__':
    unittest.main()
