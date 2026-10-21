from decimal import Decimal
import unittest

from scenario_odds import DEFAULT_ODDS, combined_odds, format_odds, scenario_odds_text


class ScenarioOddsTests(unittest.TestCase):
    def test_user_reference_values_and_mixed_product(self):
        self.assertEqual(DEFAULT_ODDS, {'1st': 3.10, '2nd': 2.10, 'Égal': 3.00})
        self.assertAlmostEqual(combined_odds(['1st', '2nd', 'Égal']), 19.53)
        self.assertEqual(scenario_odds_text(['1st', '2nd', 'Égal']), '19,53')

    def test_final_rounding_does_not_round_intermediate_products(self):
        self.assertEqual(scenario_odds_text(['2nd'] * 3), '9,26')
        self.assertEqual(scenario_odds_text(['2nd'] * 5), '40,84')

    def test_full_twenty_match_product_is_not_the_six_match_preview(self):
        sequence = ['2nd'] * 20
        expected = format_odds(Decimal('2.1') ** 20)
        self.assertEqual(scenario_odds_text(sequence), expected)
        self.assertNotEqual(expected, scenario_odds_text(sequence[:6]))
        self.assertEqual(sequence, ['2nd'] * 20)

    def test_existing_explicit_odds_argument_and_numeric_return_are_preserved(self):
        self.assertIsInstance(combined_odds(['1st'], {'1st': 2.5}), float)
        self.assertEqual(combined_odds(['1st', '1st'], {'1st': 2.5}), 6.25)
        self.assertEqual(DEFAULT_ODDS['1st'], 3.10)

    def test_empty_invalid_and_overflowing_products_are_rejected(self):
        with self.assertRaises(ValueError):
            combined_odds([])
        for value in [1, 0, -3, float('nan'), float('inf'), 'invalide', True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                combined_odds(['1st'], {'1st': value})
        with self.assertRaises(ValueError):
            combined_odds(['1st', '1st'], {'1st': 1e308})


if __name__ == '__main__':
    unittest.main()
