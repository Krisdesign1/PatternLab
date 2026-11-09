"""Manual coverage and actual Clubs analysis use the same past-only rows."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from club_identity import fixture_identity
import manual_match_coverage as coverage
import manual_mixed_analysis as mixed
import match_predictor as predictor
import prospective_models as models
from test_manual_mixed_analysis import NOW, mixed_fixtures, mixed_history, ml


class ManualMatchCoverageTests(unittest.TestCase):
    def setUp(self):
        for operation in (patch.object(ml, 'load_report', return_value=None),
                          patch.object(models, '_context_neighbors',
                              side_effect=models.SnapshotError('Configuration absente.'))):
            operation.start()
            self.addCleanup(operation.stop)

    def check(self, rows=None, fixtures=None, *, now=NOW, group=()):
        return coverage.coverage(rows if rows is not None else mixed_history(40),
            fixtures if fixtures is not None else mixed_fixtures(), decision_at=now, group_fixtures=group)

    def test_counts_eligibility_and_personalization_match_actual_estimates_and_build(self):
        rows, fixtures = mixed_history(40), mixed_fixtures()
        original = deepcopy((rows, fixtures))
        actual = mixed.build(rows, fixtures, decision_at=NOW)
        result = self.check(rows, fixtures, group=fixtures)
        self.assertEqual(all(item['eligible'] for item in result.values()), actual['forecast']['eligible'])
        for fixture in fixtures:
            item = result[fixture['match_id']]
            estimate = predictor.estimate(rows, fixture['league'], fixture['home'], fixture['away'],
                actual['history_cutoff_exclusive'], **fixture_identity(fixture))
            self.assertEqual(item['eligible'], estimate['eligible'])
            self.assertEqual(item['training_matches'], estimate['training'])
            self.assertEqual(item['personalized'], estimate['personalized'])
            self.assertEqual(item['home_matches'], estimate['home_profile']['matches'])
            self.assertEqual(item['away_matches'], estimate['away_profile']['matches'])
            support = next(row for row in actual['competition_support'] if row['league'] == fixture['league'])
            self.assertEqual((item['training_matches'], item['training_days'], item['before']),
                (support['training_matches'], support['training_days'], support['before']))
        self.assertEqual((rows, fixtures), original)

    def test_thirty_results_and_three_distinct_utc_days_are_both_required(self):
        fixtures = mixed_fixtures(2)
        for count, dates, expected in ((29, 3, False), (30, 2, False), (30, 3, True)):
            rows = mixed_history(count)
            for index, row in enumerate(rows):
                row['timestamp'] = f'2025-01-{1 + index % dates:02d}T12:00:00'
            with self.subTest(count=count, dates=dates):
                result = self.check(rows, fixtures)
                self.assertTrue(all(item['eligible'] == expected for item in result.values()))
                self.assertTrue(all(item['training_matches'] == count for item in result.values()))
                actual = mixed.build(rows, fixtures, decision_at=NOW)
                self.assertEqual(actual['forecast']['eligible'], expected)

    def test_fewer_than_three_club_precedents_keeps_eligible_competition_baseline(self):
        fixtures = mixed_fixtures(2)
        fixtures[0].update(home='New Club', home_id='900000')
        item = self.check(fixtures=fixtures)[fixtures[0]['match_id']]
        self.assertTrue(item['eligible'])
        self.assertFalse(item['personalized'])
        self.assertEqual(item['home_matches'], 0)
        self.assertTrue(mixed.build(mixed_history(40), fixtures, decision_at=NOW)['forecast']['eligible'])

    def test_one_deficient_league_does_not_borrow_the_other_competitions_rows(self):
        rows = [row for row in mixed_history(40) if row['league'] == '39'] + [
            row for row in mixed_history(20) if row['league'] == '61']
        fixtures = mixed_fixtures(2)
        result = self.check(rows, fixtures, group=fixtures)
        self.assertTrue(result[fixtures[0]['match_id']]['eligible'])
        self.assertFalse(result[fixtures[1]['match_id']]['eligible'])
        self.assertEqual(result[fixtures[1]['match_id']]['training_matches'], 20)
        self.assertFalse(mixed.build(rows, fixtures, decision_at=NOW)['forecast']['eligible'])

    def test_common_cutoff_matches_build_across_utc_midnight_and_ignores_target_day_scores(self):
        current = datetime(2025, 3, 18, 22, tzinfo=timezone.utc)
        fixtures = mixed_fixtures(2)
        fixtures[0]['timestamp'] = '2025-03-18T23:20:00'
        fixtures[1]['timestamp'] = '2025-03-19T00:20:00'
        rows = mixed_history(40)
        future = [dict(rows[0], match_id='20001', timestamp='2025-03-18T12:00:00',
                       ht=None, ft=None, value='bad')]
        result = self.check(rows + future, fixtures, now=current, group=fixtures)
        actual = mixed.build(rows + future, fixtures, decision_at=current)
        self.assertTrue(all(item['before'] == actual['history_cutoff_exclusive'] == '2025-03-18'
                            for item in result.values()))
        self.assertEqual(result, self.check(rows, fixtures, now=current, group=fixtures))

    def test_selected_group_can_move_candidates_cutoff_earlier_than_its_own_utc_day(self):
        fixtures = mixed_fixtures(2)
        rows = mixed_history(30)
        # Exactly one admissible competition-39 result becomes target-day data
        # after adding an earlier UTC fixture to the group.
        rows[0]['timestamp'] = '2025-03-18T10:00:00'
        group = [dict(fixtures[1], timestamp='2025-03-18T23:30:00')]
        candidate = [dict(fixtures[0], timestamp='2025-03-19T00:30:00')]
        current = datetime(2025, 3, 18, 22, tzinfo=timezone.utc)
        without = self.check(rows, candidate, now=datetime(2025, 3, 19, tzinfo=timezone.utc))
        with_group = self.check(rows, candidate, now=current, group=group)
        self.assertEqual(without[candidate[0]['match_id']]['before'], '2025-03-19')
        self.assertEqual(without[candidate[0]['match_id']]['training_matches'], 30)
        self.assertEqual(with_group[candidate[0]['match_id']]['before'], '2025-03-18')
        self.assertEqual(with_group[candidate[0]['match_id']]['training_matches'], 29)
        self.assertFalse(with_group[candidate[0]['match_id']]['eligible'])

    def test_timezone_is_normalized_before_counting_days_and_naive_clock_rejected(self):
        fixtures = mixed_fixtures(2)
        result = self.check(fixtures=fixtures, now=NOW.astimezone(timezone(timedelta(hours=12))))
        self.assertEqual(result, self.check(fixtures=fixtures))
        with self.assertRaisesRegex(ValueError, 'fuseau'):
            self.check(now=NOW.replace(tzinfo=None))
        rows = mixed_history(30)
        for index, row in enumerate(rows):
            row['timestamp'] = ('2025-01-02T00:30:00+02:00' if index % 3 == 0
                                else f'2025-01-{2 + index % 2:02d}T12:00:00+00:00')
        result = self.check(rows, fixtures)
        self.assertTrue(all(item['training_days'] == 3 for item in result.values()))

    def test_exact_duplicate_history_does_not_inflate_counts_and_conflicts_fail_closed(self):
        rows, fixtures = mixed_history(29), mixed_fixtures(2)
        duplicate = deepcopy(rows[0])
        result = self.check(rows + [duplicate], fixtures)
        self.assertEqual(result[fixtures[0]['match_id']]['training_matches'], 29)
        self.assertFalse(result[fixtures[0]['match_id']]['eligible'])
        duplicate.update(ht='0-0', ft='0-1', value='2nd')
        result = self.check(rows + [duplicate], fixtures)
        self.assertTrue(result[fixtures[0]['match_id']]['invalid'])
        self.assertIsNone(result[fixtures[0]['match_id']]['training_matches'])
        self.assertFalse(result[fixtures[0]['match_id']]['eligible'])
        self.assertFalse(result[fixtures[1]['match_id']]['invalid'])

    def test_bad_historical_score_or_club_identity_never_becomes_eligible_partial_history(self):
        for changes in ({'ht': None}, {'value': 'wrong'}, {'home_id': True}, {'home_id': -1}):
            rows, fixtures = mixed_history(40), mixed_fixtures(2)
            rows[0].update(changes)
            with self.subTest(changes=changes):
                result = self.check(rows, fixtures)
                item = result[fixtures[0]['match_id']]
                self.assertTrue(item['invalid'])
                self.assertFalse(item['eligible'])
                self.assertIn('Historique local invalide', item['reason'])
                self.assertFalse(result[fixtures[1]['match_id']]['invalid'])

    def test_unassignable_history_produces_explicit_diagnostic_instead_of_silent_drop(self):
        for bad in (None, {'provider': 'api-football', 'match_id': '1'}):
            with self.subTest(bad=bad):
                result = self.check(mixed_history(40) + [bad])
                self.assertTrue(all(item['invalid'] and not item['eligible'] for item in result.values()))

    def test_unassignable_future_or_target_day_row_does_not_poison_past_coverage(self):
        rows = mixed_history(40)
        for stamp in ('2025-03-19T10:00:00', '2025-03-20T10:00:00', '2025-03-19T00:30:00+01:00'):
            bad = dict(provider='api-football', match_id='unknown', timestamp=stamp, ht=None, ft=None)
            with self.subTest(stamp=stamp):
                if stamp.endswith('+01:00'):
                    result = self.check(rows + [bad])
                    self.assertTrue(all(item['invalid'] for item in result.values()))
                else:
                    self.assertEqual(self.check(rows + [bad]), self.check(rows))

    def test_other_provider_is_not_used_to_satisfy_local_api_coverage(self):
        rows = mixed_history(29)
        extra = [dict(rows[0], match_id='999999', provider='other', ht=None)]
        result = self.check(rows + extra)
        self.assertTrue(all(item['training_matches'] == 29 and not item['eligible'] for item in result.values()))

    def test_bad_candidate_remains_explicit_and_missing_or_duplicate_ids_raise(self):
        fixtures = mixed_fixtures(2)
        fixtures[0].pop('home_id')
        result = self.check(fixtures=fixtures)
        self.assertEqual(set(result), {row['match_id'] for row in fixtures})
        self.assertTrue(result[fixtures[0]['match_id']]['invalid'])
        self.assertTrue(result[fixtures[1]['match_id']]['eligible'])
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.check(fixtures=[fixtures[1], deepcopy(fixtures[1])])
        fixtures[0].pop('match_id')
        with self.assertRaisesRegex(ValueError, 'identifiant'):
            self.check(fixtures=fixtures)

    def test_group_invalid_or_started_never_reports_usable_candidate(self):
        fixtures = mixed_fixtures(2)
        for group in ([dict(fixtures[1], away_id=None)], [dict(fixtures[1], timestamp=NOW.isoformat())]):
            with self.subTest(group=group):
                result = self.check(fixtures=[fixtures[0]], group=group)
                self.assertFalse(result[fixtures[0]['match_id']]['eligible'])
                self.assertTrue(result[fixtures[0]['match_id']]['invalid'])

    def test_each_league_cutoff_history_is_validated_once_without_model_inference_or_io(self):
        rows, fixtures = mixed_history(40), mixed_fixtures(20)
        with patch.object(coverage.ml, '_rows', wraps=coverage.ml._rows) as select, \
             patch.object(coverage.predictor, 'training_rows', wraps=coverage.predictor.training_rows) as training, \
             patch.object(coverage.predictor, 'estimate', side_effect=AssertionError('No inference')), \
             patch.object(coverage.ml, 'load_report', side_effect=AssertionError('No saved model read')):
            result = self.check(rows, fixtures, group=fixtures)
        self.assertEqual(len(result), 20)
        self.assertTrue(all(item['eligible'] for item in result.values()))
        self.assertEqual(select.call_count, 2)
        self.assertEqual(training.call_count, 2)
        for call in select.call_args_list:
            scoped, league = call.args[:2]
            self.assertEqual(len(scoped), 40)
            self.assertTrue(all(str(row['league']) == league for row in scoped))


if __name__ == '__main__':
    unittest.main()
