"""Manual multi-competition Clubs groups keep causal, independent contexts."""
from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from math import fsum, prod
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
from club_identity import fixture_identity
from league_model import VALUES
import manual_mixed_analysis as mixed
from match_predictor import estimate
import prediction_journal as journal
import prospective_models as models
from test_mixed_models import mixed_fixtures, mixed_history


NOW = datetime(2025, 3, 19, 10, tzinfo=timezone.utc)


class ManualMixedAnalysisTests(unittest.TestCase):
    def setUp(self):
        for task in (patch.object(ml, 'load_report', return_value=None),
                     patch.object(models, '_context_neighbors',
                                  side_effect=models.SnapshotError('Configuration absente.'))):
            task.start(); self.addCleanup(task.stop)

    def build(self, rows=None, fixtures=None, **kwargs):
        return mixed.build(rows if rows is not None else mixed_history(),
                           fixtures if fixtures is not None else mixed_fixtures(),
                           decision_at=kwargs.pop('decision_at', NOW), **kwargs)

    def test_singleton_competitions_use_their_exact_club_probabilities(self):
        rows, fixtures = mixed_history(40), mixed_fixtures(2)
        value = self.build(rows, fixtures)
        self.assertTrue(value['forecast']['eligible'])
        self.assertEqual(value['engine'], 'mixed_clubs')
        self.assertEqual(value['selected_method'], 'clubs')
        self.assertEqual(set(value['comparison_methods']), {'clubs'})
        method = value['comparison_methods']['clubs']
        probabilities = [estimate(rows, fixture['league'], fixture['home'], fixture['away'],
                                  value['history_cutoff_exclusive'], **fixture_identity(fixture))['probabilities']
                         for fixture in fixtures]
        for sequence in product(VALUES, repeat=2):
            self.assertAlmostEqual(models.probability_for_sequence(method, sequence),
                prod(p[label] for p, label in zip(probabilities, sequence)))
        self.assertAlmostEqual(fsum(models.probability_for_sequence(method, sequence)
                                   for sequence in product(VALUES, repeat=2)), 1)
        journal._mixed_provenance(method, value['fixtures'], method['training_before'], ['39', '61'])

    def test_clubs_does_not_require_twenty_repetition_groups(self):
        fixtures = mixed_fixtures(3)
        self.assertNotIn('clubs', models.build_mixed_methods(mixed_history(30), fixtures,
                                                           decision_at=NOW)['methods'])
        value = self.build(mixed_history(30), fixtures)
        self.assertTrue(value['forecast']['eligible'])
        self.assertIn('clubs', value['comparison_methods'])
        self.assertIn('combined', value['unavailable'])
        self.assertEqual([row['training_matches'] for row in value['competition_support']], [30, 30])

    def test_all_existing_comparison_methods_are_retained_without_replacing_their_ranks(self):
        rows, fixtures = mixed_history(), mixed_fixtures()
        before = models.build_mixed_methods(rows, fixtures, decision_at=NOW)
        value = self.build(rows, fixtures)
        self.assertEqual(set(value['comparison_methods']), set(before['methods']))
        for name, method in before['methods'].items():
            self.assertEqual(value['comparison_methods'][name]['ranking'], method['ranking'])
        for row in value['forecast']['ranking']:
            self.assertAlmostEqual(row['probability'], models.probability_for_sequence(
                value['comparison_methods']['clubs'], row['sequence']))
        self.assertLess(sum(row['probability'] for row in value['forecast']['ranking']), 1)

    def test_each_competition_stays_separate_and_source_inputs_are_unchanged(self):
        rows, fixtures = mixed_history(40), mixed_fixtures(2)
        original = deepcopy((rows, fixtures))
        value = self.build(rows, fixtures)
        # Ensure only the second competition differs, including its match IDs.
        changed = [row for row in rows if row['league'] == '39'] + [
            row for row in mixed_history(80) if row['league'] == '61']
        other = self.build(changed, fixtures)
        first_leaf = lambda item: item['comparison_methods']['clubs']['distribution']['components'][0]['method']
        self.assertEqual(first_leaf(value), first_leaf(other))
        self.assertEqual((rows, fixtures), original)
        value['comparison_methods']['clubs']['ranking'][0]['sequence'][0] = 'edited'
        self.assertNotEqual(value['forecast']['ranking'][0]['sequence'][0], 'edited')

    def test_one_competition_is_supported_in_the_same_selection_path(self):
        fixtures = mixed_fixtures(3)
        for fixture in fixtures: fixture['league'] = '39'
        value = self.build(mixed_history(40), fixtures)
        self.assertTrue(value['forecast']['eligible'])
        self.assertEqual(value['forecast']['leagues'], ['39'])
        method = value['comparison_methods']['clubs']
        self.assertEqual(len(method['distribution']['components']), 1)
        journal._mixed_provenance(method, value['fixtures'], method['training_before'], ['39'])

    def test_common_cutoff_excludes_future_scores_even_across_utc_midnight(self):
        current = datetime(2025, 3, 18, 22, tzinfo=timezone.utc)
        fixtures = mixed_fixtures(2)
        fixtures[0]['timestamp'] = '2025-03-18T23:20:00'
        fixtures[1]['timestamp'] = '2025-03-19T00:20:00'
        rows = mixed_history(40)
        future = [dict(rows[0], match_id='20001', timestamp='2025-03-18T12:00:00',
                       ht=None, ft=None, value='not a result')]
        value = self.build(rows, fixtures, decision_at=current)
        other = self.build(rows + future, fixtures, decision_at=current)
        self.assertEqual(value, other)
        self.assertEqual(value['day'], '2025-03-19')
        self.assertEqual(value['history_cutoff_exclusive'], '2025-03-18')
        self.assertTrue(all(item['before'] == '2025-03-18' for item in value['competition_support']))

    def test_insufficient_league_is_explicit_without_substitution(self):
        value = self.build(mixed_history(20), mixed_fixtures(2))
        self.assertFalse(value['forecast']['eligible'])
        self.assertEqual(value['forecast']['ranking'], [])
        self.assertNotIn('clubs', value['comparison_methods'])
        self.assertIn('30 résultats', value['forecast']['diagnostic'])
        self.assertEqual(value['selected_method'], 'clubs')

    def test_invalid_identity_order_day_source_or_decision_is_rejected(self):
        for issue in ('duplicate', 'missing_club_id', 'order', 'day', 'source', 'naive', 'past'):
            fixtures = mixed_fixtures(2)
            kwargs = {}
            if issue == 'duplicate': fixtures[1] = deepcopy(fixtures[0])
            elif issue == 'missing_club_id': fixtures[0].pop('home_id')
            elif issue == 'order': fixtures.reverse()
            elif issue == 'day': fixtures[1]['timestamp'] = '2025-03-21T12:01:00'
            elif issue == 'source': kwargs['source'] = 'another-provider'
            elif issue == 'naive': kwargs['decision_at'] = NOW.replace(tzinfo=None)
            elif issue == 'past': kwargs['decision_at'] = datetime(2025, 3, 20, 13, tzinfo=timezone.utc)
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                self.build(mixed_history(40), fixtures, **kwargs)

    def test_twenty_matches_keep_all_positions_and_precise_product_mass(self):
        fixtures = mixed_fixtures(20)
        value = self.build(mixed_history(40), fixtures)
        self.assertTrue(value['forecast']['eligible'])
        self.assertEqual(len(value['forecast']['club_estimates']), 20)
        components = value['comparison_methods']['clubs']['distribution']['components']
        self.assertEqual([row['indices'] for row in components], [list(range(0, 20, 2)), list(range(1, 20, 2))])
        for row in value['forecast']['ranking']:
            self.assertEqual(len(row['sequence']), 20)
            expected = prod(item['probabilities'][label] for item, label in
                            zip(value['forecast']['club_estimates'], row['sequence']))
            self.assertAlmostEqual(row['probability'], expected, places=15)


if __name__ == '__main__': unittest.main()
