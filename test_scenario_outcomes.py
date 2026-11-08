import copy
from pathlib import Path
import subprocess
import sys
import unittest

import scenario_outcomes as outcomes


def method(sequences=None):
    sequences = sequences or [['2nd', '2nd'], ['1st', '2nd'], ['2nd', '1st']]
    return {'ranking': [{'sequence': sequence, 'probability': .3 - rank * .05}
                        for rank, sequence in enumerate(sequences)]}


def entry():
    return dict(size=2, status='settled', actual=['2nd', '2nd'],
                methods={'clubs': method()}, evaluations={'clubs': {'top1': False, 'top3': False}})


class ScenarioOutcomeTests(unittest.TestCase):
    def test_checks_every_method_and_reports_each_matching_saved_rank(self):
        value = entry()
        value['metadata'] = {'selected_engine': 'combined'}
        value['methods'] = {
            'combined': method([['1st', '1st'], ['Égal', 'Égal'], ['1st', 'Égal']]),
            'context_neighbors': method([['1st', '1st'], ['Égal', 'Égal'], ['2nd', '2nd']]),
            'clubs': method([['1st', '1st'], ['2nd', '2nd'], ['1st', 'Égal']]),
        }
        result = outcomes.summarize(value)
        self.assertEqual(result['state'], 'hit')
        self.assertEqual([(row['method'], row['rank']) for row in result['matches']],
                         [('clubs', 2), ('context_neighbors', 3)])
        self.assertEqual(result['actual'], ['2nd', '2nd'])

    def test_match_is_complete_and_ordered_not_merely_same_tokens(self):
        value = entry()
        value['actual'] = ['Égal', '1st']
        value['methods'] = {'clubs': method([['1st', 'Égal'], ['1st', '1st'], ['2nd', '2nd']])}
        result = outcomes.summarize(value)
        self.assertEqual(result['state'], 'miss')
        self.assertEqual(result['matches'], [])
        value['actual'] = ['1st', 'Égal']
        self.assertEqual(outcomes.summarize(value)['matches'][0]['rank'], 1)

    def test_nonfinal_states_ignore_stale_results_and_evaluations(self):
        for state in ('pending', 'void', 'unresolved'):
            with self.subTest(state=state):
                value = entry()
                value['status'] = state
                value['evaluations'] = {'clubs': {'top1': True, 'top3': True}}
                result = outcomes.summarize(value)
                self.assertEqual(result['state'], state)
                self.assertIsNone(result['actual'])
                self.assertEqual(result['matches'], [])

    def test_damaged_settled_data_never_becomes_a_false_miss_or_success(self):
        cases = [dict(size=True), dict(size=1), dict(size=21), dict(actual=None),
                 dict(actual=['2nd']), dict(actual=['2nd', 'unknown']), dict(methods={}),
                 dict(methods=None), dict(methods={'clubs': {'ranking': []}}),
                 dict(methods={'clubs': method(), 'damaged': None}),
                 dict(methods={'clubs': method(), '': method()}), dict(status='unexpected')]
        for patch in cases:
            with self.subTest(patch=patch):
                value = entry()
                value.update(patch)
                result = outcomes.summarize(value)
                self.assertEqual(result['state'], 'unavailable')
                self.assertEqual(result['matches'], [])
                self.assertIsNone(result['actual'])
        for probability in [None, True, float('nan'), float('inf'), -1, 2]:
            with self.subTest(probability=probability):
                value = entry()
                value['methods']['clubs']['ranking'][1]['probability'] = probability
                self.assertEqual(outcomes.summarize(value)['state'], 'unavailable')
        value = entry()
        value['methods']['clubs']['ranking'][1]['sequence'] = ['2nd', '2nd']
        self.assertEqual(outcomes.summarize(value)['state'], 'unavailable')

    def test_abbreviated_and_longer_rankings_keep_all_explicitly_saved_proposals(self):
        for count in (1, 2, 3):
            value = entry()
            value['methods']['clubs']['ranking'] = value['methods']['clubs']['ranking'][:count]
            self.assertEqual(outcomes.summarize(value)['state'], 'hit')
        value = entry()
        value['actual'] = ['Égal', 'Égal']
        value['methods']['clubs']['ranking'].append({'sequence': ['Égal', 'Égal'], 'probability': .01})
        result = outcomes.summarize(value)
        self.assertEqual(result['state'], 'hit')
        self.assertEqual(result['matches'], [dict(method='clubs', rank=4, sequence=['Égal', 'Égal'])])

    def test_six_distinct_proposals_from_multiple_methods_are_all_checked(self):
        value = entry()
        value['methods']['frequency'] = method([['Égal', '1st'], ['1st', 'Égal'], ['Égal', 'Égal']])
        value['actual'] = ['Égal', 'Égal']
        result = outcomes.summarize(value)
        self.assertEqual(result['state'], 'hit')
        self.assertEqual(result['matches'], [dict(method='frequency', rank=3, sequence=['Égal', 'Égal'])])

    def test_invalid_tail_invalidates_verdict_even_when_an_earlier_choice_matches(self):
        for tail in (None, {'sequence': ['invalid', 'Égal'], 'probability': .01},
                     {'sequence': ['Égal', 'Égal'], 'probability': float('nan')},
                     {'sequence': ['2nd', '2nd'], 'probability': .01}):
            with self.subTest(tail=tail):
                value = entry()
                value['methods']['clubs']['ranking'].append(tail)
                result = outcomes.summarize(value)
                self.assertEqual(result['state'], 'unavailable')
                self.assertEqual(result['matches'], [])
                self.assertIsNone(result['actual'])

    def test_saved_evaluations_and_distributions_cannot_override_exact_choices(self):
        value = entry()
        value['evaluations'] = {'clubs': {'top1': False, 'top3': False}}
        value['methods']['clubs']['distribution'] = {'unsupported': 'never read'}
        self.assertEqual(outcomes.summarize(value)['state'], 'hit')
        value['actual'] = ['Égal', 'Égal']
        value['evaluations']['clubs'] = {'top1': True, 'top3': True}
        self.assertEqual(outcomes.summarize(value)['state'], 'miss')

    def test_input_is_unchanged_and_returned_lists_do_not_alias_it(self):
        value = entry()
        original = copy.deepcopy(value)
        result = outcomes.summarize(value)
        self.assertEqual(value, original)
        result['actual'][0] = '1st'
        result['matches'][0]['sequence'][0] = 'Égal'
        self.assertEqual(value, original)

    def test_all_sizes_supported_and_mapping_order_does_not_change_result(self):
        for size in (2, 3, 20):
            sequences = [['2nd'] * size, ['1st'] * size, ['Égal'] * size]
            value = dict(size=size, status='settled', actual=['Égal'] * size,
                         methods={'new_method': method(sequences), 'clubs': method(sequences)})
            original = outcomes.summarize(value)
            value['methods'] = dict(reversed(list(value['methods'].items())))
            self.assertEqual(outcomes.summarize(value), original)
            self.assertEqual([row['rank'] for row in original['matches']], [3, 3])

    def test_module_imports_no_streamlit_provider_model_or_journal(self):
        source = ("import sys; import scenario_outcomes; "
                  "assert not ({'streamlit','api_football','prospective_models','prediction_journal'} & set(sys.modules))")
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, text=True,
                                cwd=Path(__file__).parent, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
