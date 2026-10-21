import copy
from itertools import product
import json
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from coupon_sizes import top_independent
import prospective_models as prospective
import scenario_tables as tables


def independent(positions):
    return {'version': 'saved-test-v1',
            'ranking': top_independent(positions, 3),
            'distribution': {'schema': prospective.DISTRIBUTION_VERSION, 'type': 'independent',
                             'size': len(positions), 'distributions': positions}}


def positions(first=.2, second=.6):
    return [{'1st': first, 'Égal': 1-first-second, '2nd': second} for _ in range(2)]


class ScenarioTableTests(unittest.TestCase):
    def test_reference_odds_are_separate_from_saved_probabilities(self):
        methods = {'clubs': {'ranking': [{'sequence': ['1st', '2nd', 'Égal'], 'probability': .1234}]}}
        original = copy.deepcopy(methods)
        matrix = tables.build_matrix(methods)
        html = tables.matrix_html(matrix)
        self.assertIn('Cote cumulée', html)
        self.assertIn('>19,53<', html)
        self.assertIn('>12,34 %<', html)
        self.assertIn('Référence fixe', html)
        self.assertEqual(methods, original)
        self.assertNotIn('odds', matrix['rows'][0])

    def test_shared_choices_are_single_rows_and_cross_probabilities_use_saved_distribution(self):
        methods = {'combined': independent(positions(.5, .3)), 'clubs': independent(positions())}
        original = copy.deepcopy(methods)
        with patch.object(prospective, 'build_methods', side_effect=AssertionError('No current model')):
            matrix = tables.build_matrix(methods, actual=['2nd', '2nd'])
        self.assertEqual(methods, original)
        self.assertEqual([method['key'] for method in matrix['methods']], ['clubs', 'combined'])
        sequences = [row['sequence'] for row in matrix['rows']]
        self.assertEqual(len(sequences), len(set(sequences)))
        row = next(row for row in matrix['rows'] if row['sequence'] == ('2nd', '2nd'))
        self.assertAlmostEqual(row['cells']['clubs']['probability'], .36)
        self.assertAlmostEqual(row['cells']['combined']['probability'], .09)
        self.assertEqual(row['cells']['combined']['status'], 'outside_top3')
        self.assertIsNone(row['cells']['combined']['rank'])
        self.assertTrue(row['actual'])

    def test_ranking_only_has_missing_values_rather_than_invented_zero_probability(self):
        methods = {'frequency': {'ranking': [{'sequence': ['1st', '1st'], 'probability': .2}]},
                   'clubs': {'ranking': [{'sequence': ['2nd', '2nd'], 'probability': .3}]}}
        matrix = tables.build_matrix(methods)
        row = next(row for row in matrix['rows'] if row['sequence'] == ('1st', '1st'))
        self.assertIsNone(row['cells']['clubs']['probability'])
        html = tables.matrix_html(matrix)
        self.assertIn('Non classé dans les 3', html)
        self.assertNotIn('>0,00 %<', html)
        self.assertNotIn('Non réalisé', html)

    def test_rows_are_deterministic_independently_of_method_mapping_order(self):
        methods = {'combined': independent(positions(.5, .3)), 'frequency': independent(positions())}
        first = tables.build_matrix(methods)
        second = tables.build_matrix(dict(reversed(list(methods.items()))))
        self.assertEqual(first, second)
        self.assertEqual(first['rows'][0]['sequence'], ('2nd', '2nd'))

    def test_unseen_actual_outcome_is_explicit_without_adding_a_fake_top_choice(self):
        method = independent(positions())
        matrix = tables.build_matrix({'clubs': method}, actual=['Égal', 'Égal'])
        self.assertFalse(matrix['actual_in_choices'])
        self.assertEqual(len(matrix['rows']), 3)
        self.assertTrue(all(row['actual'] is False for row in matrix['rows']))

    def test_damaged_full_distribution_cannot_fall_back_to_ranking_only(self):
        payload = independent(positions())
        payload['distribution']['distributions'][0]['2nd'] = .9
        with self.assertRaises(ValueError):
            tables.build_matrix({'clubs': payload})
        payload = independent(positions())
        payload['ranking'][0]['probability'] = .99
        with self.assertRaises(ValueError):
            tables.build_matrix({'clubs': payload})

    def test_invalid_probabilities_sizes_duplicate_and_mixed_ordered_groups_are_rejected(self):
        for value in [None, float('nan'), float('inf'), True, -.1, 1.1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tables.build_matrix({'clubs': {'ranking': [{'sequence': ['1st', '2nd'], 'probability': value}]}})
        for sequence in [['1st'], ['1st'] * 21, ['1st', '<script>']]:
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                tables.build_matrix({'clubs': {'ranking': [{'sequence': sequence, 'probability': .1}]}})
        duplicate = {'ranking': [{'sequence': ['1st', '2nd'], 'probability': .1}] * 2}
        with self.assertRaises(ValueError):
            tables.build_matrix({'clubs': duplicate})
        with self.assertRaises(ValueError):
            tables.build_matrix({'clubs': independent(positions())}, actual=['1st'] * 3)
        with self.assertRaises(ValueError):
            tables.build_matrix({str(number): independent(positions()) for number in range(6)})

    def test_twenty_match_sequences_stay_bounded_and_tiny_probabilities_are_not_rounded_to_zero(self):
        matrix = tables.build_matrix({'clubs': independent(positions() * 10)})
        self.assertEqual(len(matrix['rows']), 3)
        html = tables.matrix_html(matrix)
        self.assertIn('&lt; 0,01 %', html)
        self.assertNotIn('0,00 %', html)

    def test_html_escapes_dynamic_names_titles_and_unknown_labels(self):
        hostile = '<img src=x onerror="alert(1)">'
        matrix = tables.build_matrix({hostile: {'ranking': [{'sequence': ['1st', '2nd'], 'probability': .12}]}})
        html = tables.matrix_html(matrix, title=hostile)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)
        self.assertIn('scope="row"', html)
        self.assertIn('scope="col"', html)
        self.assertIn('<caption', html)
        self.assertNotIn('<img', tables.sequence_html(['1st', hostile]))
        for css in ('first', 'second', 'equal'):
            self.assertIn(f'pl-scenario-token-{css}', tables.sequence_html(['1st', '2nd', 'Égal']))

    def test_empty_methods_render_without_failure_or_false_results(self):
        matrix = tables.build_matrix({})
        self.assertEqual(matrix['rows'], [])
        self.assertIsNone(matrix['actual_in_choices'])


class ScenarioMatrixUiTests(unittest.TestCase):
    def test_renders_matrix_and_colors_without_training_or_data_loading(self):
        methods = {'clubs': independent(positions()), 'combined': independent(positions(.5, .3))}
        source = ('import json\nimport scenario_tables as tables\n'
                  f'methods=json.loads({json.dumps(json.dumps(methods))})\n'
                  'tables.render_legend()\n'
                  'tables.render_sequence(["1st", "2nd", "Égal"], label="Scénario réalisé")\n'
                  'tables.render_matrix(methods, actual=["2nd", "2nd"], key="proof")\n')
        with patch.object(prospective, 'build_methods', side_effect=AssertionError('No training')):
            at = AppTest.from_string(source, default_timeout=15).run()
        self.assertFalse(at.exception)
        html = '\n'.join(node.proto.body for node in at.get('html'))
        self.assertIn('pl-scenario-token-first', html)
        self.assertIn('Réalisé', html)
        self.assertIn('Hors des 3 choix', html)
        self.assertEqual(len(at.get('html')), 3)

    def test_pagination_covers_union_and_resets_on_changed_payload(self):
        sequences = list(product(('1st', 'Égal', '2nd'), repeat=4))
        methods = {name: {'ranking': [{'sequence': sequence, 'probability': .1 - index * .01}
                                     for index, sequence in enumerate(sequences[offset:offset+3])]}
                   for name, offset in zip(tables.METHOD_ORDER, range(0, 15, 3))}
        source = ('import json\nimport streamlit as st\nimport scenario_tables as tables\n'
                  f'st.session_state.setdefault("payload",json.loads({json.dumps(json.dumps(methods))}))\n'
                  'tables.render_matrix(st.session_state["payload"], key="pages")\n')
        at = AppTest.from_string(source, default_timeout=15).run()
        self.assertFalse(at.exception)
        self.assertIn('10 scénario(s)', at.caption[0].value)
        at.selectbox[0].select(1).run()
        self.assertFalse(at.exception)
        self.assertIn('5 scénario(s)', at.caption[0].value)
        changed = copy.deepcopy(at.session_state['payload'])
        changed['frequency']['ranking'][0]['probability'] = .12
        at.session_state['payload'] = changed
        at.run()
        self.assertFalse(at.exception)
        self.assertEqual(at.selectbox[0].value, 0)
        self.assertIn('10 scénario(s)', at.caption[0].value)
        at.button_group[0].set_value('Premiers choix').run()
        self.assertFalse(at.exception)
        self.assertFalse(at.selectbox)
        self.assertIn('5 scénario(s)', at.caption[0].value)


if __name__ == '__main__':
    unittest.main()
