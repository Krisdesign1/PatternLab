import copy
import itertools
import unittest

from league_model import VALUES
from league_patterns import repeated_scenarios
from scenario_recurrence import recurrence_diagnostics


def result_for(sequences, pending=()):
    size = len(sequences[0])
    values = [value for sequence in sequences for value in sequence] + list(pending)
    rows = [dict(match_id=str(i + 1), league='A', value=value,
                 timestamp=f'2026-09-19T12:{i // 60:02d}:{i % 60:02d}')
            for i, value in enumerate(values)]
    return repeated_scenarios(rows, size)['A']


class RecurrenceTests(unittest.TestCase):
    def test_constant_history_has_no_excess_even_at_maximum_size(self):
        for size in (2, 4, 20):
            with self.subTest(size=size):
                result = result_for([('2nd',) * size] * 7)
                self.assertEqual(recurrence_diagnostics(result), [{
                    'sequence': ('2nd',) * size,
                    'observed': 7, 'groups': 7, 'expected': 7.0,
                    'ratio': 1.0, 'excess': 0.0,
                    'blocks': list(range(1, 8)),
                }])

    def test_balanced_independent_positions_have_expected_observed_counts(self):
        result = result_for(list(itertools.product(VALUES, repeat=2)))
        diagnostics = recurrence_diagnostics(result)
        self.assertEqual(len(diagnostics), 9)
        for row in diagnostics:
            self.assertEqual(row['observed'], 1)
            self.assertEqual(row['groups'], 9)
            self.assertAlmostEqual(row['expected'], 1)
            self.assertAlmostEqual(row['ratio'], 1)
            self.assertAlmostEqual(row['excess'], 0)

    def test_reference_uses_separate_position_frequencies(self):
        result = result_for([
            ('1st', '2nd'), ('1st', '2nd'),
            ('1st', 'Égal'), ('Égal', 'Égal'),
        ])
        rows = {r['sequence']: r for r in recurrence_diagnostics(result)}
        self.assertEqual(set(rows), {('1st', '2nd'), ('1st', 'Égal'), ('Égal', 'Égal')})
        common = rows[('1st', '2nd')]
        self.assertEqual(common['observed'], 2)
        self.assertEqual(common['expected'], 1.5)
        self.assertAlmostEqual(common['ratio'], 4 / 3)
        self.assertEqual(common['excess'], 0.5)
        rare = rows[('Égal', 'Égal')]
        self.assertEqual(rare['expected'], 0.5)
        self.assertEqual(rare['ratio'], 2)

    def test_keeps_ranking_counts_blocks_and_input_order_unchanged(self):
        result = result_for([
            ('2nd', '1st', 'Égal'),
            ('Égal', '2nd', '1st'),
            ('2nd', '1st', 'Égal'),
        ], pending=('2nd', '2nd'))
        before = copy.deepcopy(result)
        diagnostics = recurrence_diagnostics(result)
        self.assertEqual([r['sequence'] for r in diagnostics],
                         [r['sequence'] for r in result['ranking']])
        self.assertEqual([r['observed'] for r in diagnostics],
                         [r['count'] for r in result['ranking']])
        self.assertEqual([r['blocks'] for r in diagnostics],
                         [r['blocks'] for r in result['ranking']])
        self.assertEqual([r['groups'] for r in diagnostics], [3, 3])
        self.assertEqual(result, before)

    def test_empty_complete_history(self):
        self.assertEqual(recurrence_diagnostics({'size': 2, 'blocks': [], 'ranking': []}), [])

    def test_rejects_sizes_outside_two_to_twenty(self):
        for size in (1, 21, True, 4.0):
            with self.subTest(size=size), self.assertRaises(ValueError):
                recurrence_diagnostics({'size': size, 'blocks': [], 'ranking': []})

    def test_rejects_malformed_complete_groups(self):
        for sequence in (('1st',), ('1st', 'unknown')):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                recurrence_diagnostics({'size': 2,
                                        'blocks': [{'sequence': sequence, 'number': 1}],
                                        'ranking': [{'sequence': sequence, 'count': 1}]})
