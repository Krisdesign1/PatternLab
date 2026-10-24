from copy import deepcopy
import unittest

import history_support as support


def row(home=8, away=8, training=100, days=10):
    return dict(probabilities={'1st':.2,'Égal':.3,'2nd':.5}, training=training, history_days=days,
                home_profile={'matches':home}, away_profile={'matches':away})


class HistorySupportTests(unittest.TestCase):
    def test_exact_boundary_is_only_an_input_filter_and_does_not_change_probabilities(self):
        rows = [row(),row(home=3),row(training=99),row(days=9)]
        before = deepcopy(rows)
        self.assertEqual(support.select(rows), [rows[0]])
        self.assertEqual(support.select(rows,support.MINIMUM),rows)
        self.assertEqual(rows,before)

    def test_unknown_and_invalid_counts_are_never_described_as_qualified(self):
        for value in (None, True, -1, '10', 9.5):
            estimate = row(home=value)
            state = support.assess(estimate)
            self.assertFalse(state['eligible'])
            self.assertIn('team_matches',state['missing'])

    def test_counts_are_role_specific_and_probabilities_do_not_override_them(self):
        estimate = row(home=100,away=3)
        estimate['probabilities']={'1st':.99,'Égal':.005,'2nd':.005}
        self.assertFalse(support.assess(estimate)['eligible'])
        self.assertEqual(support.assess(estimate)['gaps'],{'team_matches':5})

    def test_custom_criteria_must_respect_model_minimum(self):
        for criteria in ({},dict(support.PRUDENT,team_matches=2),dict(support.PRUDENT,history_days=True)):
            with self.assertRaises(ValueError):support.assess(row(),criteria)


if __name__ == '__main__':unittest.main()
