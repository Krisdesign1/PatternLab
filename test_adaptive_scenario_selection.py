"""Exact adaptive sets against exhaustive small joints and large lazy products."""
from copy import deepcopy
from itertools import product
from math import nextafter
import unittest
from unittest.mock import patch

import adaptive_scenario_selection as selection
import prospective_models as frozen
from scenario_predictor import fit_sequences


VALUES = ('1st', 'Égal', '2nd')


def positions(size):
    return [{'1st': .59 - .03 * index, 'Égal': .26,
             '2nd': 1 - (.59 - .03 * index) - .26} for index in range(size)]


def method(kind, size):
    base = positions(size)
    histories = [tuple(VALUES[(position + offset) % 3] for position in range(size))
                 for offset in (0, 1, 2, 0, 0, 2, 1)]
    fit = fit_sequences(histories, size)
    if kind == 'independent':
        distribution = frozen._distribution(kind, size, distributions=base)
    elif kind == 'frequency':
        distribution = frozen._distribution(kind, size, fit=fit)
    elif kind == 'corrected':
        normalization = frozen._normalization(fit, base, frozen.PRIOR_WEIGHT, .5)[2]
        distribution = frozen._distribution(kind, size, fit=fit, distributions=base,
                                            alpha=.5, normalization=normalization)
    elif kind == 'context_mixture':
        local = sorted(set(histories))
        distribution = dict(schema=frozen.DISTRIBUTION_VERSION, type=kind, size=size,
                            baseline=base, eta=.25,
                            local_mass=[dict(sequence=list(row), probability=1/len(local)) for row in local])
    else:
        raise ValueError(kind)
    return frozen._method('test-source-v1', distribution, {})


def block_method(size=5):
    components = []
    if size == 20:
        for index in range(size):
            distribution = frozen._distribution('independent', 1,
                distributions=[{'1st': .99, 'Égal': .006, '2nd': .004}])
            child = frozen._method('test-component', distribution, {}, _component=True)
            components.append(dict(indices=[index], method=child))
    else:
        # Interleaving positions catches accidental block-order sequence joins.
        components = [dict(indices=[0, 2], method=method('corrected', 2)),
                      dict(indices=[1, 3, 4], method=method('context_mixture', 3))]
    distribution = dict(schema=frozen.DISTRIBUTION_VERSION, type='block_product',
                        size=size, components=components)
    return frozen._method('test-mixed-v1', distribution, {})


def brute(payload, threshold):
    model = frozen._read_distribution(payload['distribution'])
    rows = [dict(sequence=list(sequence), probability=frozen._probability(model, sequence))
            for sequence in product(VALUES, repeat=model['size'])]
    return sorted((row for row in rows if row['probability'] >= threshold),
                  key=lambda row: (-row['probability'], tuple(row['sequence'])))


class AdaptiveScenarioSelectionTests(unittest.TestCase):
    def test_every_distribution_matches_exhaustive_small_joints_and_boundaries(self):
        for size in range(2, 6):
            for kind in ('independent', 'frequency', 'corrected', 'context_mixture'):
                payload = method(kind, size)
                all_rows = brute(payload, 1e-10)
                thresholds = (.2, .05, all_rows[0]['probability'],
                              all_rows[len(all_rows)//2]['probability'],
                              all_rows[-1]['probability'] / 2,
                              nextafter(all_rows[0]['probability'], 1.0))
                for threshold in thresholds:
                    with self.subTest(size=size, kind=kind, threshold=threshold):
                        self.assertEqual(selection.select(payload, threshold), brute(payload, threshold))

    def test_interleaved_block_product_matches_exhaustive_five_match_joint(self):
        payload = block_method()
        all_rows = brute(payload, 1e-10)
        for threshold in (.2, .01, .001, all_rows[30]['probability'], all_rows[-1]['probability']/2):
            with self.subTest(threshold=threshold):
                self.assertEqual(selection.select(payload, threshold), brute(payload, threshold))

    def test_uniform_boundary_keeps_all_ties_instead_of_three(self):
        distribution = frozen._distribution('independent', 2,
                            distributions=[dict.fromkeys(VALUES, 1/3)] * 2)
        payload = frozen._method('uniform', distribution, {})
        result = selection.select(payload, 1/9)
        self.assertEqual(len(result), 9)
        self.assertEqual(result, brute(payload, 1/9))
        self.assertEqual(selection.select(payload, nextafter(1/9, 1)), [])

    def test_sparse_exception_can_win_when_its_independent_remainder_is_below_threshold(self):
        for kind in ('frequency', 'corrected', 'context_mixture'):
            payload = method(kind, 5)
            expected = brute(payload, .025)
            self.assertTrue(expected)
            self.assertEqual(selection.select(payload, .025), expected)

    def test_twenty_match_component_product_is_lazy_and_does_not_enumerate_joint(self):
        payload = block_method(20)
        checks = []
        def budget():
            checks.append(1)
            if len(checks) > 100:
                raise AssertionError('An exact high-threshold product must be small')
        with patch('itertools.product', side_effect=AssertionError('No exhaustive product')):
            rows = selection.select(payload, .8, check_time=budget)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sequence'], ['1st'] * 20)
        self.assertEqual(rows[0]['probability'], payload['ranking'][0]['probability'])

    def test_complete_output_is_independent_and_input_snapshot_remains_unchanged(self):
        payload = method('corrected', 4)
        before = deepcopy(payload)
        output = selection.select(payload, .001)
        self.assertGreater(len(output), 3)
        self.assertEqual(payload, before)
        output[0]['sequence'][0] = 'changed'
        self.assertEqual(payload, before)
        self.assertEqual(list(selection.iter_selected(payload, .001)), brute(payload, .001))

    def test_resource_interruption_never_returns_a_truncated_list(self):
        payload = method('independent', 5)
        calls = 0
        def timeout():
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise TimeoutError('Budget exhausted')
        with self.assertRaisesRegex(TimeoutError, 'Budget exhausted'):
            selection.select(payload, 1e-10, check_time=timeout)
        self.assertEqual(calls, 3)

    def test_invalid_threshold_callback_and_damaged_source_are_rejected(self):
        payload = method('independent', 2)
        for threshold in (True, False, None, '0.1', 0, -.1, 1.1, float('nan'), float('inf'), 10**1000):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                selection.select(payload, threshold)
        with self.assertRaises(ValueError):
            selection.select(payload, .1, check_time=5)
        for damaged in (None, {}, dict(payload, ranking=payload['ranking'][:2]),
                        dict(payload, ranking=payload['ranking'] + payload['ranking'][:1])):
            with self.subTest(damaged=damaged), self.assertRaises(ValueError):
                selection.select(damaged, .1)
        damaged = deepcopy(payload)
        damaged['ranking'][0]['probability'] += .01
        with self.assertRaises(ValueError):
            selection.select(damaged, .1)


if __name__ == '__main__':
    unittest.main()
