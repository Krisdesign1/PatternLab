import copy
import math
import unittest

import trajectory_research_features as features


def fixture(mid, day, home='10', away='20', ht='1-0', ft='2-0', league='39'):
    first, total = sum(map(int, ht.split('-'))), sum(map(int, ft.split('-')))
    return dict(match_id=str(mid), timestamp=f'2024-01-{day:02d}T12:00:00',
                league=league, home_id=home, away_id=away, season=2023,
                provider='api-football', ht=ht, ft=ft,
                value='1st' if 2*first > total else 'Égal' if 2*first == total else '2nd')


class TrajectoryFeaturesTests(unittest.TestCase):
    def test_same_day_results_never_enter_features(self):
        rows = [fixture(1, 1), fixture(2, 2), fixture(3, 2, home='10', away='30')]
        first = features.build_examples(rows)
        changed = copy.deepcopy(rows)
        changed[1].update(ht='4-0', ft='4-0', value='1st')
        second = features.build_examples(changed)
        self.assertEqual(first[2]['context'], second[2]['context'])
        for key in ('summary', 'bag', 'ordered', 'history_ids'):
            self.assertEqual(first[2][key], second[2][key])
        self.assertEqual(first[2]['history_ids']['home_overall'], ['1'])

    def test_future_result_changes_cannot_change_past_features(self):
        rows = [fixture(i, i) for i in range(1, 5)]
        original = features.build_examples(rows)
        rows[-1].update(ht='5-0', ft='5-0', value='1st')
        changed = features.build_examples(rows)
        for a, b in zip(original, changed):
            for key in ('context', 'summary', 'bag', 'ordered', 'history_ids'):
                self.assertEqual(a[key], b[key])

    def test_bag_preserves_records_but_not_order(self):
        # All totals split evenly, keeping each prequential reference at .5.
        rows = [fixture(1, 1, ht='1-0', ft='1-1'), fixture(2, 2, ht='0-2', ft='2-2'),
                fixture(3, 3, ht='2-1', ft='4-2'), fixture(4, 4)]
        a = features.build_examples(rows)[-1]
        swapped = copy.deepcopy(rows)
        swapped[0]['timestamp'], swapped[1]['timestamp'] = swapped[1]['timestamp'], swapped[0]['timestamp']
        b = features.build_examples(swapped)[-1]
        self.assertEqual(a['context'], b['context'])
        self.assertEqual(a['summary'], b['summary'])
        self.assertEqual(a['bag'], b['bag'])
        self.assertNotEqual(a['ordered'], b['ordered'])
        for prefix in features.PROFILES:
            def vectors(view):
                return sorted(tuple(view[f'{prefix}_slot{s}_{c}'] for c in features.CHANNELS)
                              for s in range(1, features.WINDOW+1)
                              if view[f'{prefix}_slot{s}_present'])
            self.assertEqual(vectors(a['bag']), vectors(a['ordered']))

    def test_club_ids_are_scoped_to_competition(self):
        rows = [fixture(1, 1, league='39'), fixture(2, 2, league='61')]
        last = features.build_examples(rows)[-1]
        self.assertEqual(last['context']['home_overall_count'], 0)
        self.assertEqual(last['context']['league_count'], 0)
        self.assertEqual(last['history_ids']['home_overall'], [])

    def test_roles_are_separate_while_overall_contains_both(self):
        rows = [fixture(1, 1), fixture(2, 2, home='20', away='10'), fixture(3, 3)]
        last = features.build_examples(rows)[-1]
        self.assertEqual(last['history_ids']['home_overall'], ['1', '2'])
        self.assertEqual(last['history_ids']['home_role'], ['1'])

    def test_window_and_masks_do_not_invent_precedents(self):
        result = features.build_examples([fixture(i, i) for i in range(1, 14)])[-1]
        self.assertEqual(result['history_ids']['home_overall'], list(map(str, range(3, 13))))
        self.assertEqual(result['context']['home_overall_count'], 12)
        early = features.build_examples([fixture(1, 1), fixture(2, 2)])[1]
        self.assertEqual(early['ordered']['home_overall_slot1_present'], 1.)
        self.assertEqual(early['ordered']['home_overall_slot2_present'], 0.)

    def test_zero_goals_and_schema_finite(self):
        examples = features.build_examples([fixture(1, 1, ht='0-0', ft='0-0'), fixture(2, 2)])
        e = examples[-1]
        self.assertEqual(e['ordered']['home_overall_slot1_equal_residual'], 0.)
        for mode in ('summary', 'bag', 'ordered'):
            numeric, categorical = features.columns(mode)
            values = dict(e['context'], **e['summary'])
            if mode != 'summary':
                values.update(e[mode])
            self.assertEqual(len(numeric), len(set(numeric)))
            self.assertTrue(all(math.isfinite(float(values[k])) for k in numeric))
            self.assertTrue(all(isinstance(values[k], str) for k in categorical))

    def test_ambiguous_or_invalid_rows_fail_closed(self):
        original = fixture(1, 1)
        self.assertEqual(len(features.build_examples([original, original.copy()])), 1)
        conflict = dict(original, ht='2-0', ft='2-0', value='1st')
        for rows in ([original, conflict], [dict(original, provider='other')],
                     [dict(original, home_id='20')], [dict(original, ht='3-0')],
                     [dict(original, value='1st')], [dict(original, home_id=None)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                features.build_examples(rows)


if __name__ == '__main__':
    unittest.main()
