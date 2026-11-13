"""Before/after columns expose independent role histories without rewriting them."""
from copy import deepcopy
import unittest

from club_recent_sequences import comparison_role_columns, role_columns


def profile(home=(), away=(), *, home_percentages=None, away_percentages=None):
    values = {'team_id': '99', 'league': '39'}
    for side, results, percentages in (('home', home, home_percentages),
                                        ('away', away, away_percentages)):
        values[side + '_matches'] = len(results)
        values[side + '_percentages'] = percentages or {
            result: 100 * results.count(result) / len(results) if results else None
            for result in ('1st', 'Égal', '2nd')}
        values['recent_' + side + '_results'] = list(results)
        values['recent_' + side + '_dates'] = [f'2026-11-0{i+1}T13:00:00+01:00' for i in range(len(results))]
        values['recent_' + side + '_ids'] = [str(i+1) for i in range(len(results))]
    return values


class ClubComparisonColumnsTests(unittest.TestCase):
    def test_missing_history_is_not_a_measured_zero(self):
        after = profile(home=('2nd',))
        columns = comparison_role_columns(None, after, 'home')
        self.assertIsNone(columns['Matchs historiques domicile avant'])
        self.assertIsNone(columns['Domicile · 1st avant (%)'])
        self.assertIsNone(columns['Domicile · 2nd écart (points)'])
        self.assertEqual(columns['Matchs historiques domicile après'], 1)
        self.assertEqual(columns['Domicile · 1st après (%)'], 0)
        self.assertEqual(columns['Domicile · 2nd après (%)'], 100)
        self.assertEqual(columns['Tendance domicile avant'], [])
        self.assertEqual(columns['Tendance domicile après'], ['2nd'])
        empty = comparison_role_columns(profile(), None, 'away')
        self.assertEqual(empty['Matchs historiques extérieur avant'], 0)
        self.assertIsNone(empty['Extérieur · Égal avant (%)'])
        self.assertIsNone(empty['Matchs historiques extérieur après'])
        self.assertEqual(empty['Effectif récent extérieur avant'], 0)
        self.assertEqual(empty['Récents extérieur avant (ancien → récent)'], '')
        self.assertEqual(empty['Tendance extérieur avant'], [])
        self.assertEqual(empty['Tendance extérieur après'], [])

    def test_leaders_use_exact_full_role_frequencies_and_keep_all_ties(self):
        before = profile(home=('2nd',), away=('1st', '1st', 'Égal'))
        after = profile(home=('1st', '2nd'), away=('1st', '1st', 'Égal', 'Égal'))
        before['recent_home_results'] = ['1st']
        self.assertEqual(comparison_role_columns(before, after, 'home')['Tendance domicile avant'], ['2nd'])
        self.assertEqual(comparison_role_columns(before, after, 'home')['Tendance domicile après'], ['1st', '2nd'])
        self.assertEqual(comparison_role_columns(before, after, 'away')['Tendance extérieur avant'], ['1st'])
        self.assertEqual(comparison_role_columns(before, after, 'away')['Tendance extérieur après'], ['1st', 'Égal'])
        near_tie = profile(home=('2nd',), home_percentages={'1st': 33.349, 'Égal': 33.300, '2nd': 33.351})
        self.assertEqual(comparison_role_columns(near_tie, near_tie, 'home')['Tendance domicile avant'], ['2nd'])
        equal = profile(away=('1st', 'Égal', '2nd'))
        self.assertEqual(comparison_role_columns(equal, equal, 'away')['Tendance extérieur après'], ['1st', 'Égal', '2nd'])
        zeros = profile(home=('1st',), home_percentages={'1st': 0, 'Égal': 0, '2nd': 0})
        self.assertEqual(comparison_role_columns(zeros, zeros, 'home')['Tendance domicile avant'], [])

    def test_each_role_keeps_its_own_sequence_dates_ids_and_denominator(self):
        before = profile(home=('1st', '2nd'), away=('Égal', 'Égal', '1st'))
        after = profile(home=('1st', '2nd', '2nd'), away=('Égal', 'Égal', '1st', '2nd'))
        original = deepcopy((before, after))
        home = comparison_role_columns(before, after, 'home')
        away = comparison_role_columns(before, after, 'away')
        self.assertEqual(home['Matchs historiques domicile avant'], 2)
        self.assertEqual(home['Matchs historiques domicile après'], 3)
        self.assertEqual(away['Matchs historiques extérieur avant'], 3)
        self.assertEqual(away['Matchs historiques extérieur après'], 4)
        self.assertEqual(home['Récents domicile avant (ancien → récent)'], '1st → 2nd')
        self.assertEqual(home['Récents domicile après (ancien → récent)'], '1st → 2nd → 2nd')
        self.assertEqual(away['Récents extérieur avant (ancien → récent)'], 'Égal → Égal → 1st')
        self.assertEqual(away['Dates des récents extérieur après (Kinshasa)'],
                         '01/11/2026 13:00 → 02/11/2026 13:00 → 03/11/2026 13:00 → 04/11/2026 13:00')
        self.assertEqual(home['ID matchs récents domicile avant'], '1 → 2')
        self.assertEqual(home['ID matchs récents domicile après'], '1 → 2 → 3')
        self.assertEqual(home['Effectif récent domicile après'], 3)
        self.assertEqual(away['Effectif récent extérieur après'], 4)
        self.assertEqual((before, after), original)

    def test_delta_matches_displayed_values_and_does_not_round_percentages(self):
        before = profile(home=('2nd',), home_percentages={'1st': 33.349, 'Égal': 16.702, '2nd': 49.949})
        after = profile(home=('2nd',), home_percentages={'1st': 33.351, 'Égal': 16.698, '2nd': 49.951})
        columns = comparison_role_columns(before, after, 'home')
        self.assertEqual(columns['Domicile · 1st avant (%)'], 33.349)
        self.assertEqual(columns['Domicile · 1st après (%)'], 33.351)
        self.assertEqual(columns['Domicile · 1st écart (points)'], 0.1)
        self.assertEqual(columns['Domicile · Égal écart (points)'], 0)
        self.assertEqual(columns['Domicile · 2nd écart (points)'], 0.1)
        names = list(columns)
        start = names.index('Domicile · 2nd avant (%)')
        self.assertEqual(names[start:start + 3], ['Domicile · 2nd avant (%)',
                         'Domicile · 2nd après (%)', 'Domicile · 2nd écart (points)'])

    def test_recent_windows_remain_separate_from_total_history_and_backfill(self):
        before = profile(home=('1st', 'Égal', '2nd'), away=('1st', '1st'))
        after = deepcopy(before)
        after['home_matches'] = 6
        after['home_percentages'] = {'1st': 100/6, 'Égal': 100/6, '2nd': 400/6}
        after['recent_home_results'] = ['2nd', '2nd', '2nd']
        after['recent_home_dates'] = ['2026-11-04T13:00:00+01:00', '2026-11-05T13:00:00+01:00', '2026-11-06T13:00:00+01:00']
        after['recent_home_ids'] = ['4', '5', '6']
        columns = comparison_role_columns(before, after, 'home')
        self.assertEqual(columns['Matchs historiques domicile après'], 6)
        self.assertEqual(columns['Effectif récent domicile après'], 3)
        self.assertEqual(columns['Récents domicile avant (ancien → récent)'], '1st → Égal → 2nd')
        self.assertEqual(columns['Récents domicile après (ancien → récent)'], '2nd → 2nd → 2nd')
        self.assertEqual(columns['ID matchs récents domicile après'], '4 → 5 → 6')
        self.assertEqual(columns['Dates des récents domicile après (Kinshasa)'],
                         '04/11/2026 13:00 → 05/11/2026 13:00 → 06/11/2026 13:00')
        self.assertAlmostEqual(columns['Domicile · 2nd après (%)'], 400/6)
        self.assertEqual(columns['Domicile · 2nd écart (points)'], 33.4)

    def test_hiding_after_keeps_only_before_and_preserves_existing_role_columns(self):
        before = profile(home=('1st', '2nd'), away=('Égal',))
        after = profile(home=('2nd',), away=('2nd',))
        original = role_columns(before, before)
        full = comparison_role_columns(before, after, 'home')
        hidden = comparison_role_columns(before, after, 'home', show_after=False)
        self.assertTrue(hidden)
        self.assertTrue(all('avant' in key for key in hidden))
        self.assertEqual(hidden, {key: value for key, value in full.items() if 'avant' in key})
        self.assertEqual(role_columns(before, before), original)
        with self.assertRaises(ValueError):
            comparison_role_columns(before, after, 'all')


if __name__ == '__main__':
    unittest.main()
