"""Verify causal features and chronological boundaries without ML dependencies."""
import copy
from datetime import date, timedelta
from math import isfinite
import unittest

from league_model import VALUES, outcome
from scenario_features import (
    CATEGORICAL_COLUMNS, FEATURE_COLUMNS, FeatureError, LABELS, UNKNOWN_ID,
    build_examples, chronological_split, feature_for_fixture,
)


def match(mid, day=0, *, ht=(1, 0), ft=(2, 1), league='39', provider='api-football',
          home='Home', away='Away', home_id='1', away_id='2', timestamp=None, season=2025):
    stamp = timestamp or (date(2026, 1, 1) + timedelta(days=day)).isoformat() + 'T12:00:00'
    return dict(match_id=str(mid), league=league, provider=provider, home=home, away=away,
                home_id=home_id, away_id=away_id, timestamp=stamp,
                season=season, ht='-'.join(map(str, ht)), ft='-'.join(map(str, ft)), value=outcome(ht, ft))


def ids(partitions):
    return {name: [example['match']['match_id'] for example in items] for name, items in partitions.items()}


class CausalFeatureTests(unittest.TestCase):
    def test_schema_is_stable_numeric_finite_and_first_match_has_no_history(self):
        row = match(1)
        example = build_examples([row], '39')[0]
        features = example['features']
        self.assertEqual(LABELS, VALUES)
        self.assertEqual(tuple(features), FEATURE_COLUMNS)
        self.assertEqual(len(FEATURE_COLUMNS), len(set(FEATURE_COLUMNS)))
        self.assertIs(example['match'], row)
        self.assertEqual(example['label'], '2nd')
        self.assertEqual(features['league_count'], 0)
        self.assertEqual(features['home_role_last5_count'], 0)
        self.assertEqual(features['home_overall_days_rest'], -1)
        self.assertEqual(features['home_role_last5_ht_for'], 0)
        self.assertEqual(features['league_rate_2nd'], 0)
        for key, value in features.items():
            if key in CATEGORICAL_COLUMNS:
                self.assertIsInstance(value, str)
                self.assertTrue(value)
            else:
                self.assertIsInstance(value, (int, float))
                self.assertTrue(isfinite(value))
        self.assertTrue({'value', 'label', 'ht', 'ft', 'match_id', 'timestamp'}.isdisjoint(features))
        self.assertEqual(feature_for_fixture([row], row), features)

    def test_all_matches_of_a_day_are_emitted_before_any_results_are_used(self):
        rows = [match(1, timestamp='2026-01-01T10:00:00'),
                match(2, timestamp='2026-01-01T21:00:00'), match(3, day=1)]
        examples = build_examples(rows, '39')
        self.assertEqual([e['features']['league_count'] for e in examples], [0, 0, 2])
        changed = copy.deepcopy(rows)
        changed[0].update(ht='5-0', ft='5-0', value='1st')
        altered = build_examples(changed, '39')
        self.assertEqual(examples[1]['features'], altered[1]['features'])
        self.assertNotEqual(examples[2]['features'], altered[2]['features'])
        self.assertEqual(examples[2]['features']['home_role_count'], 2)

    def test_target_same_day_and_future_corrupt_scores_cannot_affect_fixture_features(self):
        rows = [match(1, day=0), match(2, day=1), match(3, day=2), match(4, day=3)]
        fixture = dict(match(99, day=2), timestamp='2026-01-03T23:00:00')
        expected = feature_for_fixture(rows, fixture)
        changed = copy.deepcopy(rows)
        for row in changed:
            if row['timestamp'][:10] >= '2026-01-03':
                row.update(ht='unreadable target score', ft=None, value='unreadable future label', season=2099)
        target = dict(fixture, ht=None, ft='must not be read', value='must not be read')
        self.assertEqual(feature_for_fixture(changed, target), expected)
        self.assertEqual(expected['league_count'], 2)
        earlier = feature_for_fixture(rows, fixture, before='2026-01-02')
        self.assertEqual(earlier['league_count'], 1)
        self.assertEqual(feature_for_fixture(rows, fixture, before='2026-02-01'), expected)

    def test_utc_days_and_numeric_identifier_order_are_used(self):
        rows = [match(11, timestamp='2026-01-01T23:30:00Z'),
                match(2, timestamp='2026-01-02T00:30:00+02:00'),
                match(10, timestamp='2026-01-01T23:30:00Z'),
                match(12, timestamp='2026-01-02T00:30:00Z')]
        examples = build_examples(rows[::-1], '39')
        self.assertEqual([e['match']['match_id'] for e in examples], ['2', '10', '11', '12'])
        self.assertEqual([e['features']['league_count'] for e in examples], [0, 0, 0, 3])
        self.assertEqual(examples[0]['features']['hour_utc'], 22)
        self.assertEqual(examples[3]['features']['hour_utc'], 0)

    def test_role_specific_goals_are_computed_from_ht_and_second_half_differences(self):
        rows = [match(1, ht=(1, 0), ft=(1, 2)),
                match(2, day=1, home='Other', away='Home', home_id='3', away_id='1',
                      ht=(0, 2), ft=(1, 3))]
        features = feature_for_fixture(rows, match(99, day=3))
        self.assertEqual(features['home_overall_count'], 2)
        self.assertEqual(features['home_role_count'], 1)
        self.assertEqual(features['home_overall_last5_ht_for'], 1.5)
        self.assertEqual(features['home_overall_last5_sh_for'], .5)
        self.assertEqual(features['home_overall_last5_sh_against'], 1.5)
        self.assertEqual(features['home_role_last5_ht_for'], 1)
        self.assertEqual(features['home_role_last5_sh_for'], 0)
        self.assertEqual(features['home_role_last5_sh_against'], 2)
        self.assertEqual(features['home_overall_days_rest'], 2)
        self.assertEqual(features['home_role_days_rest'], 3)

    def test_recent_five_and_ten_windows_have_real_support_and_label_rates(self):
        rows = [match(i, day=i-1, ht=(i, 0), ft=(2*i+1, 0)) for i in range(1, 13)]
        features = feature_for_fixture(rows, match(99, day=13))
        self.assertEqual(features['home_role_count'], 12)
        self.assertEqual(features['home_role_last5_count'], 5)
        self.assertEqual(features['home_role_last10_count'], 10)
        self.assertEqual(features['home_role_last5_ht_for'], 10)
        self.assertEqual(features['home_role_last10_ht_for'], 7.5)
        self.assertEqual(features['home_role_last5_sh_for'], 11)
        self.assertEqual(features['away_role_last10_sh_against'], 8.5)
        self.assertEqual(features['home_role_last10_rate_2nd'], 1)
        self.assertEqual(features['league_last10_rate_2nd'], 1)
        self.assertEqual(features['home_overall_days_rest'], 2)

    def test_explicit_scope_excludes_other_leagues_and_sources_before_inspecting_scores(self):
        rows = [match(1), match(2, day=1)]
        other_league = dict(match(1, league='61'), ht=None, ft=None, value='invalid')
        other_provider = dict(match(1, provider='football-data.org'), ht=None, ft=None, value='invalid')
        expected = build_examples(rows, '39', provider='api-football')
        self.assertEqual(build_examples(rows + [other_league, other_provider], '39', provider='API-Football'), expected)
        self.assertEqual(feature_for_fixture(rows + [other_league, other_provider], match(99, day=3)),
                         feature_for_fixture(rows, match(99, day=3)))
        with self.assertRaises(FeatureError):
            build_examples(rows + [match(3, provider='football-data.org')], '39')
        self.assertEqual(build_examples(rows, 'not-imported'), [])

    def test_duplicate_ids_are_deduplicated_or_rejected_without_mutating_input(self):
        rows = [match(1), match(2, day=1)]
        before = copy.deepcopy(rows)
        self.assertEqual(build_examples(rows + [copy.deepcopy(rows[0])], '39'), build_examples(rows, '39'))
        self.assertEqual(rows, before)
        with self.assertRaises(FeatureError):
            build_examples(rows + [match(1, ht=(3, 0), ft=(3, 0))], '39')

    def test_team_ids_survive_renames_and_missing_ids_resolve_only_unambiguous_past_names(self):
        rows = [match(1, home='Old name')]
        renamed = feature_for_fixture(rows, match(99, day=2, home='New name'))
        self.assertEqual(renamed['home_id'], '1')
        self.assertEqual(renamed['home_name'], 'New name')
        self.assertEqual(renamed['home_role_count'], 1)
        unresolved = match(99, day=2, home='Old name', home_id=None)
        resolved = feature_for_fixture(rows, unresolved)
        self.assertEqual(resolved['home_id'], '1')
        self.assertEqual(resolved['home_role_count'], 1)
        conflicting_name = match(2, day=1, home='Old name', home_id='3')
        ambiguous = feature_for_fixture(rows + [conflicting_name], unresolved)
        self.assertEqual(ambiguous['home_id'], UNKNOWN_ID)
        self.assertEqual(ambiguous['home_role_count'], 0)
        future_same_name = match(3, day=3, home='Old name', home_id='4')
        self.assertEqual(feature_for_fixture(rows + [future_same_name], unresolved), resolved)

    def test_missing_season_uses_only_prior_metadata_and_cold_start_remains_finite(self):
        rows = [match(1, season=2025), match(2, day=1, season=2026), match(3, day=4, season=2099)]
        target = match(99, day=3, season=None, home_id=None, away_id=None)
        features = feature_for_fixture(rows, target)
        self.assertEqual(features['season'], 2026)
        self.assertEqual(features['season_known'], 0)
        self.assertEqual(features['home_id'], '1')
        cold = feature_for_fixture([], target)
        self.assertEqual(cold['season'], 2026)
        self.assertEqual(cold['home_id'], UNKNOWN_ID)
        self.assertEqual(cold['league_count'], 0)
        self.assertEqual(tuple(cold), FEATURE_COLUMNS)

    def test_invalid_scores_source_dates_and_metadata_are_rejected(self):
        invalid = [dict(match(1), value='1st'), dict(match(1), ht=None),
                   dict(match(1), ht='4-0', ft='1-0'), dict(match(1), provider=None),
                   dict(match(1), timestamp='not-a-date'), dict(match(1), match_id='abc'),
                   dict(match(1), season=True), dict(match(1), away_id='1')]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(FeatureError):
                build_examples([row], '39')
        with self.assertRaises(FeatureError):
            feature_for_fixture([], dict(match(99), provider=None))
        with self.assertRaises(FeatureError):
            feature_for_fixture([], match(99), before='not-a-day')


class ChronologicalSplitTests(unittest.TestCase):
    def examples(self, counts=(4,) * 50):
        rows = []
        for day, count in enumerate(counts):
            first = len(rows)
            rows.extend(match(first + i + 1, day=day) for i in range(count))
        return build_examples(rows, '39')

    def test_disjoint_whole_days_and_minimum_counts_with_unsorted_input(self):
        examples = self.examples()
        before = copy.deepcopy(examples)
        partitions = chronological_split(examples[::-1])
        self.assertEqual({key: len(value) for key, value in partitions.items()},
                         {'train': 120, 'validation': 40, 'test': 40})
        days = {key: {e['match']['timestamp'][:10] for e in value} for key, value in partitions.items()}
        self.assertLess(max(days['train']), min(days['validation']))
        self.assertLess(max(days['validation']), min(days['test']))
        self.assertTrue(days['train'].isdisjoint(days['validation']))
        self.assertTrue(days['validation'].isdisjoint(days['test']))
        combined = [mid for group in ids(partitions).values() for mid in group]
        self.assertEqual(len(combined), len(set(combined)))
        self.assertEqual(len(combined), len(examples))
        self.assertEqual(examples, before)

    def test_boundaries_do_not_use_labels_or_feature_values(self):
        examples = self.examples()
        partitions = chronological_split(examples)
        changed = copy.deepcopy(examples)
        for example in changed:
            example['label'] = '1st'
            example['features']['league_count'] = -999
        self.assertEqual(ids(chronological_split(changed)), ids(partitions))

    def test_sensible_minimums_can_override_requested_fraction_without_splitting_days(self):
        partitions = chronological_split(self.examples((100, 30, 30)))
        self.assertEqual([len(partitions[key]) for key in ('train', 'validation', 'test')], [100, 30, 30])
        for counts in ((100,), (200,), (100, 29, 31)):
            with self.subTest(counts=counts), self.assertRaises(FeatureError):
                chronological_split(self.examples(counts))

    def test_invalid_proportions_duplicate_matches_and_mixed_sources_are_rejected(self):
        examples = self.examples()
        for fractions in ((True, .2), (0, .2), (.9, .2), (.6, float('nan')), ('0.6', .2)):
            with self.subTest(fractions=fractions), self.assertRaises(FeatureError):
                chronological_split(examples, *fractions)
        with self.assertRaises(FeatureError):
            chronological_split(examples + [examples[0]])
        changed = copy.deepcopy(examples)
        changed[-1]['match']['provider'] = 'football-data.org'
        with self.assertRaises(FeatureError):
            chronological_split(changed)


if __name__ == '__main__':
    unittest.main()
