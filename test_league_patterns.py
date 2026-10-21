import unittest
from league_patterns import repeated_scenarios


def fixture(values, league='A', offset=0):
    return [dict(match_id=str(offset + i + 1), value=v, league=league,
                 timestamp=f'2026-09-{18 + i // 8:02d}T12:{i % 8:02d}:00')
            for i, v in enumerate(values)]


class PatternTests(unittest.TestCase):
    def test_counts_actual_ordered_blocks_not_individual_frequencies(self):
        seq = ['1st', 'Égal', '2nd', '2nd']
        rows = fixture(seq + seq + seq[::-1] + ['Égal'])
        result = repeated_scenarios(rows[::-1])['A']
        self.assertEqual(len(result['blocks']), 3)
        self.assertEqual(result['ranking'][0]['sequence'], tuple(seq))
        self.assertEqual(result['ranking'][0]['count'], 2)
        self.assertEqual(result['ranking'][0]['blocks'], [1, 2])
        self.assertEqual(len(result['pending']), 1)
        ids = [r['match_id'] for b in result['blocks'] for r in b['matches']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(result['ranking']), 2)

    def test_leagues_never_mix_and_incomplete_group_excluded(self):
        rows = fixture(['1st'] * 5) + fixture(['2nd'] * 3, 'B', 100)
        results = repeated_scenarios(rows)
        self.assertEqual(len(results['A']['blocks']), 1)
        self.assertEqual(len(results['A']['pending']), 1)
        self.assertEqual(results['B']['ranking'], [])
        self.assertEqual(len(results['B']['pending']), 3)

    def test_duplicate_match_not_counted_twice_conflict_rejected(self):
        rows = fixture(['1st'] * 4)
        self.assertEqual(len(repeated_scenarios(rows + rows)['A']['blocks']), 1)
        with self.assertRaises(ValueError):
            repeated_scenarios(rows + [{**rows[0], 'value': '2nd'}])

    def test_simultaneous_matches_use_numeric_id_and_ties_share_rank(self):
        rows = fixture(['1st'] * 4 + ['2nd'] * 4, offset=8)
        for r in rows:
            r['timestamp'] = '2026-09-19T12:00:00'
        result = repeated_scenarios(rows[::-1])['A']
        self.assertTrue(result['simultaneous'])
        self.assertEqual([r['match_id'] for r in result['blocks'][0]['matches']], ['9','10','11','12'])
        self.assertEqual([r['rank'] for r in result['ranking']], [1, 1])
        self.assertEqual([r['count'] for r in result['ranking']], [1, 1])

    def test_empty_data(self):
        self.assertEqual(repeated_scenarios([]), {})


class CompletionTests(unittest.TestCase):
    def test_exact_prefix_counts_only_complete_groups_and_correct_denominator(self):
        from league_patterns import scenario_completions
        result = repeated_scenarios(fixture(
            ['2nd', 'Égal', '1st', '2nd'] * 2 +
            ['2nd', 'Égal', 'Égal', '1st'] + ['1st'] * 4 + ['2nd', 'Égal']))['A']
        found = scenario_completions(result, ['2nd', 'Égal'])
        self.assertEqual((found['support'], found['total']), (3, 4))
        self.assertEqual(found['ranking'][0]['suffix'], ('1st', '2nd'))
        self.assertEqual(found['ranking'][0]['count'], 2)
        self.assertEqual(found['ranking'][0]['blocks'], [1, 2])
        self.assertEqual(sum(r['count'] for r in found['ranking']), 3)

    def test_one_three_results_ties_and_missing_prefix(self):
        from league_patterns import scenario_completions
        result = repeated_scenarios(fixture(['1st', 'Égal', '2nd', '1st', '1st', 'Égal', '2nd', '2nd']))['A']
        for prefix in (['1st'], ['1st', 'Égal', '2nd']):
            found = scenario_completions(result, prefix)
            self.assertEqual(found['support'], 2)
            self.assertEqual([r['rank'] for r in found['ranking']], [1, 1])
            self.assertTrue(all(len(r['suffix']) == 4-len(prefix) for r in found['ranking']))
        self.assertEqual(scenario_completions(result, ['Égal'])['ranking'], [])

    def test_invalid_prefix_rejected(self):
        from league_patterns import scenario_completions
        for prefix in ([], ['1st'] * 4, ['invalid']):
            with self.assertRaises(ValueError):
                scenario_completions({'blocks': []}, prefix)
