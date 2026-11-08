"""Compare complete lazy rankings with exhaustive frozen-model probabilities."""
from copy import deepcopy
from itertools import product
from math import fsum
import json
import unittest
from unittest.mock import patch

import frozen_scenario_catalog as catalog
import prospective_models as models
from scenario_predictor import fit_sequences, PRIOR_WEIGHT


POSITIONS = [{'1st': .2, 'Égal': .3, '2nd': .5},
             {'1st': .45, 'Égal': .35, '2nd': .2},
             {'1st': .32, 'Égal': .22, '2nd': .46}]


def independent(size, positions=None):
    positions = positions or [deepcopy(POSITIONS[index % 3]) for index in range(size)]
    return models._method('catalogue-test-v1', models._distribution(
        'independent', size, distributions=positions), {}, _component=size == 1)


def candidates():
    fit = fit_sequences([('1st', 'Égal', '2nd')]*5 + [('2nd', '1st', 'Égal')]*2, 3)
    yield independent(3)
    yield models._method('frequency-test', models._distribution('frequency', 3, fit=fit), {})
    normalization = models._normalization(fit, POSITIONS, PRIOR_WEIGHT, .6)[2]
    yield models._method('corrected-test', models._distribution('corrected', 3, fit=fit,
        distributions=POSITIONS, alpha=.6, normalization=normalization), {})
    yield models._method('context-test', dict(schema=models.DISTRIBUTION_VERSION,
        type='context_mixture', size=3, baseline=deepcopy(POSITIONS), eta=.35,
        local_mass=[dict(sequence=['1st','Égal','2nd'], probability=.2),
                    dict(sequence=['Égal','2nd','1st'], probability=.8)]), {})
    # Competition components need not occupy adjacent positions.
    yield models._method('mixed-test', dict(schema=models.DISTRIBUTION_VERSION,
        type='block_product', size=3, components=[
            dict(indices=[0, 2], method=independent(2)),
            dict(indices=[1], method=independent(1, [POSITIONS[1]]))]), {})


class FrozenScenarioCatalogueTests(unittest.TestCase):
    def test_each_distribution_matches_exhaustive_order_mass_and_pagination(self):
        for method in candidates():
            with self.subTest(kind=method['distribution']['type']):
                original = deepcopy(method)
                expected = [dict(sequence=list(sequence), probability=models.probability_for_sequence(method, sequence))
                            for sequence in product(models.VALUES, repeat=3)]
                expected.sort(key=lambda row: (-row['probability'], tuple(row['sequence'])))
                seen = []
                for offset in range(0, 27, 7):
                    result = catalog.page(method, offset, 7)
                    self.assertEqual(result['total'], 27)
                    self.assertEqual(result['has_next'], offset + 7 < 27)
                    seen.extend(result['ranking'])
                self.assertEqual([r['sequence'] for r in seen], [r['sequence'] for r in expected])
                for saved, actual in zip(seen, expected):
                    self.assertAlmostEqual(saved['probability'], actual['probability'], places=14)
                self.assertAlmostEqual(fsum(row['probability'] for row in seen), 1)
                self.assertEqual(len({tuple(row['sequence']) for row in seen}), 27)
                self.assertEqual(method, original)

    def test_equal_probability_ties_are_deterministic_across_pages(self):
        positions = [dict.fromkeys(models.VALUES, 1/3) for _ in range(4)]
        method = independent(4, positions)
        ranking = catalog.page(method, 0, 100)['ranking']
        expected = sorted(product(models.VALUES, repeat=4))
        self.assertEqual([tuple(row['sequence']) for row in ranking], expected)
        self.assertEqual(catalog.page(method, 33, 11)['ranking'], ranking[33:44])

    def test_ten_match_page_has_fifty_and_lookup_handles_every_exact_sequence(self):
        method = independent(10)
        result = catalog.page(method)
        self.assertEqual(len(result['ranking']), 50)
        self.assertEqual(result['total'], 59049)
        for row in result['ranking'][3:]:
            actual = catalog.lookup(method, row['sequence'])
            self.assertIsNone(actual['rank'])
            self.assertEqual(actual['rank_status'], 'not_computed')
            self.assertEqual(actual['probability'], row['probability'])
        actual = catalog.lookup(method, method['ranking'][0]['sequence'])
        self.assertEqual(actual['rank'], 1)

    def test_twenty_matches_are_compact_and_first_page_is_not_cartesian_enumeration(self):
        method = independent(20)
        with patch.object(catalog, 'product', side_effect=AssertionError('No Cartesian materialization')):
            result = catalog.page(method)
            found = catalog.lookup(method, ['Égal']*20)
        self.assertEqual(result['total'], 3486784401)
        self.assertEqual(len(result['ranking']), 50)
        self.assertGreater(found['probability'], 0)
        self.assertLess(len(json.dumps(method)), 10000)

    def test_last_ten_match_page_retains_49_small_positive_probabilities(self):
        method = independent(10)
        result = catalog.page(method, offset=59000, page_size=50, time_budget_seconds=15)
        self.assertEqual(len(result['ranking']), 49)
        self.assertFalse(result['has_next'])
        self.assertGreater(result['ranking'][-1]['probability'], 0)

    def test_empty_tail_page_is_explicit_total_and_no_next(self):
        result = catalog.page(independent(2), 9, 50)
        self.assertEqual(result['ranking'], [])
        self.assertEqual(result['total'], 9)
        self.assertFalse(result['has_next'])

    def test_deadline_never_returns_a_truncated_page(self):
        calls = []
        def stop():
            calls.append(1)
            if len(calls) > 2:
                raise TimeoutError('budget')
        with self.assertRaises(TimeoutError):
            catalog.page(independent(10), 50, 50, check_time=stop)
        self.assertGreater(len(calls), 2)
        with self.assertRaises(catalog.CatalogBudgetExceeded):
            catalog.page(independent(20), time_budget_seconds=1e-15)

    def test_bad_page_arguments_and_lookup_lengths_are_rejected(self):
        method = independent(2)
        for offset, length in [(-1, 2), (10, 2), (False, 2), (0, 0), (0, 1001), (0, '50')]:
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                catalog.page(method, offset, length)
        for sequence in [['1st'], ['2nd']*3, ['bad','2nd']]:
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                catalog.lookup(method, sequence)

    def test_descriptor_detects_tampering_but_fixture_binding_is_separate(self):
        method = independent(3)
        original = deepcopy(method['scenario_catalog'])
        method['distribution']['context'] = {'fixtures':['11','22','33']}
        self.assertEqual(catalog.validate_descriptor(method, original), original)
        changed = deepcopy(method)
        changed['distribution']['distributions'][0] = POSITIONS[1]
        with self.assertRaises(ValueError):
            catalog.page(changed)
        changed = deepcopy(method)
        changed['ranking'][0]['probability'] = .9
        with self.assertRaises(ValueError):
            catalog.lookup(changed, ['1st']*3)

    def test_old_snapshot_is_read_without_addition_or_retraining(self):
        method = independent(3)
        method.pop('scenario_catalog')
        before = deepcopy(method)
        with patch.object(models, 'build_methods', side_effect=AssertionError('No training')):
            self.assertEqual(len(catalog.page(method)['ranking']), 27)
        self.assertEqual(method, before)

    def test_underflowed_outcomes_are_present_last_instead_of_being_dropped(self):
        method = independent(2, [{'1st':1e-200, 'Égal':.4, '2nd':.6}]*2)
        ranking = catalog.page(method)['ranking']
        self.assertEqual(len(ranking), 9)
        self.assertEqual(ranking[-1], dict(sequence=['1st','1st'], probability=0))
        self.assertEqual(catalog.lookup(method, ['1st','1st'])['probability'], 0)

    def test_invalid_math_is_rejected_even_with_rehashed_descriptor(self):
        method = independent(3)
        method['distribution']['distributions'][0]['2nd'] = .8
        method['scenario_catalog'] = catalog.descriptor(method)
        with self.assertRaises(ValueError):
            catalog.page(method)


if __name__ == '__main__':
    unittest.main()
