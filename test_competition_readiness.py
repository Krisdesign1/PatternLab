"""Pure synthetic checks against the real statistical forecast eligibility."""
import copy
from contextlib import ExitStack
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import api_football as api
import catboost_scenarios as ml
import competition_readiness as readiness
import contextual_scenarios as contextual
import match_predictor
from league_patterns import repeated_scenarios


BEFORE = '2025-08-01'


def history(count, days=3, league='39', provider='api-football'):
    scores = (('2-0', '2-0', '1st'), ('1-0', '1-1', 'Égal'), ('0-0', '1-0', '2nd'))
    rows = []
    for index in range(count):
        ht, ft, value = scores[index % len(scores)]
        stamp = datetime(2025, 1, 1, 12) + timedelta(days=index % days, seconds=index // days)
        rows.append(dict(match_id=str(index + 1), league=league, provider=provider,
                         timestamp=stamp.isoformat(), season=2024,
                         home=f'Historical home {index % 8}', away=f'Historical away {index % 7}',
                         home_id=str(100 + index % 8), away_id=str(200 + index % 7),
                         ht=ht, ft=ft, value=value))
    return rows


def fixtures(size, day=BEFORE):
    # New participants deliberately exercise the allowed league-baseline path.
    return [dict(match_id=str(9000 + index), league='39', provider='api-football',
                 home=f'New home {index}', away=f'New away {index}',
                 home_id=str(300 + index), away_id=str(400 + index), season=2025,
                 timestamp=f'{day}T12:{index:02}:00') for index in range(size)]


def result_for(rows, size, before=BEFORE):
    selected = ml._rows(rows, '39', 'API-Football', before)
    return repeated_scenarios(selected, size).get('39',
        dict(size=size, matches=[], blocks=[], ranking=[], pending=[], simultaneous=False))


class CompetitionReadinessTests(unittest.TestCase):
    def test_thresholds_match_real_prepare_for_every_size_two_through_twenty(self):
        for size in range(2, 21):
            for count in (0, 20 * size - 1, 20 * size, 21 * size - 1):
                with self.subTest(size=size, count=count):
                    rows = history(count)
                    assessment = readiness.assess_history(rows, '39', size, BEFORE)
                    prepared, model = contextual._prepare(result_for(rows, size), fixtures(size))
                    self.assertEqual(assessment['eligible'], prepared['eligible'])
                    self.assertEqual(assessment['history_groups'], prepared['training_groups'])
                    self.assertEqual(assessment['history_matches'], prepared['training_matches'])
                    self.assertEqual(assessment['history_groups'], count // size)
                    self.assertEqual(assessment['missing_groups'], max(0, 20 - count // size))
                    self.assertEqual(assessment['min_matches'], 20 * size)
                    self.assertEqual(assessment['min_groups'], 20)
                    self.assertEqual(assessment['min_days'], 3)
                    self.assertEqual(assessment['missing_matches'], max(0, 20 * size - count))
                    self.assertEqual(model is not None, assessment['eligible'])
                    self.assertTrue(assessment['reason'])

    def test_same_day_eightieth_result_cannot_complete_twentieth_group(self):
        rows = history(79)
        rows.append(dict(rows[0], match_id='1000', timestamp=f'{BEFORE}T00:00:00'))
        assessment = readiness.assess_history(rows, '39', 4, BEFORE)
        self.assertEqual((assessment['history_matches'], assessment['history_groups']), (79, 19))
        self.assertEqual((assessment['missing_matches'], assessment['missing_groups']), (1, 1))
        self.assertFalse(assessment['eligible'])
        # Also check the full-history anchor followed by _prepare's block cutoff.
        full = repeated_scenarios(ml._rows(rows, '39', 'API-Football'), 4)['39']
        prepared, _ = contextual._prepare(full, fixtures(4))
        self.assertEqual(prepared['training_groups'], 19)
        self.assertEqual(assessment['eligible'], prepared['eligible'])

    def test_enough_groups_on_two_days_still_require_another_day_and_match(self):
        rows = history(80, days=2)
        assessment = readiness.assess_history(rows, '39', 4, BEFORE)
        self.assertEqual((assessment['history_groups'], assessment['history_days']), (20, 2))
        self.assertEqual((assessment['missing_groups'], assessment['missing_days'], assessment['missing_matches']), (0, 1, 1))
        self.assertFalse(assessment['eligible'])
        self.assertIn('2/3 jours distincts', assessment['reason'])
        self.assertFalse(contextual.forecast(result_for(rows, 4), fixtures(4))['eligible'])
        rows.append(dict(rows[0], match_id='1000', timestamp='2025-01-03T00:00:00'))
        self.assertTrue(readiness.assess_history(rows, '39', 4, BEFORE)['eligible'])

    def test_new_clubs_do_not_block_statistical_readiness_or_forecast(self):
        rows = history(80)
        assessment = readiness.assess_history(rows, '39', 4, BEFORE)
        forecast = contextual.forecast(result_for(rows, 4), fixtures(4))
        self.assertTrue(assessment['eligible'])
        self.assertTrue(forecast['eligible'])
        self.assertEqual(forecast['personalized_count'], 0)
        self.assertEqual(len(forecast['ranking']), 3)

    def test_offset_timestamps_are_cut_off_by_utc_day(self):
        rows = history(79)
        included = dict(rows[0], match_id='1000', timestamp='2025-08-01T00:30:00+02:00')
        excluded = dict(rows[1], match_id='1001', timestamp='2025-07-31T23:30:00-02:00')
        without_previous_utc_day = readiness.assess_history(rows + [excluded], '39', 4, BEFORE)
        self.assertEqual(without_previous_utc_day['history_matches'], 79)
        assessment = readiness.assess_history(rows + [included, excluded], '39', 4, BEFORE)
        self.assertEqual(assessment['history_matches'], 80)
        self.assertTrue(assessment['eligible'])
        prepared, _ = contextual._prepare(result_for(rows + [included, excluded], 4), fixtures(4))
        self.assertEqual(assessment['history_matches'], prepared['training_matches'])
        self.assertEqual(assessment['history_groups'], prepared['training_groups'])

    def test_other_sources_and_leagues_are_filtered_before_validation(self):
        rows = history(80)
        invalid = dict(rows[0], timestamp='bad timestamp', value='invalid', ht=None, ft=None)
        contaminants = [dict(invalid, provider='football-data.org'),
                        dict(invalid, provider='forebet'), dict(invalid, provider=None),
                        dict(invalid, league='61')]
        expected = readiness.assess_history(rows, '39', 4, BEFORE)
        self.assertEqual(readiness.assess_history(rows + contaminants, 39, 4, BEFORE), expected)

    def test_future_bad_scores_and_conflicting_future_id_are_not_used(self):
        rows = history(80)
        future = dict(rows[0], timestamp=f'{BEFORE}T00:00:00', ht=None, ft=None, value='invalid')
        expected = readiness.assess_history(rows, '39', 4, BEFORE)
        self.assertEqual(readiness.assess_history(rows + [future], '39', 4, BEFORE), expected)
        future['timestamp'] = '2026-01-01T00:00:00Z'
        self.assertEqual(readiness.assess_history(rows + [future], '39', 4, BEFORE), expected)

    def test_identical_duplicate_id_is_counted_once(self):
        rows = history(79)
        assessment = readiness.assess_history(rows + [copy.deepcopy(rows[0])] * 4, '39', 4, BEFORE)
        self.assertEqual((assessment['history_matches'], assessment['history_groups']), (79, 19))
        self.assertFalse(assessment['eligible'])

    def test_conflicting_duplicate_and_observed_score_mismatch_fail(self):
        rows = history(80)
        conflict = dict(rows[0], ht='0-0', ft='1-0', value='2nd')
        with self.assertRaisesRegex(ValueError, 'contradictoire'):
            readiness.assess_history(rows + [conflict], '39', 4, BEFORE)
        mismatch = dict(rows[0], match_id='1000', value='2nd')
        with self.assertRaises(ValueError):
            readiness.assess_history(rows + [mismatch], '39', 4, BEFORE)

    def test_assess_sizes_reuses_one_normalized_population_and_preserves_order(self):
        rows = history(400)
        original = copy.deepcopy(rows)
        normalize = ml._rows
        with patch.object(readiness.ml, '_rows', wraps=normalize) as calls:
            assessments = readiness.assess_sizes(reversed(rows), '39', [20, 2, 4, 20, 2], BEFORE)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual([item['size'] for item in assessments], [20, 2, 4])
        self.assertEqual([item['history_groups'] for item in assessments], [20, 200, 100])
        self.assertTrue(all(item['eligible'] for item in assessments))
        self.assertEqual(rows, original)

    def test_assessment_never_predicts_trains_loads_model_or_accesses_api(self):
        blocked = (
            (contextual, '_prepare'), (contextual, 'forecast'), (match_predictor, 'estimate'),
            (ml, 'forecast'), (ml, 'train'), (ml, 'load_report'), (ml, '_classifier'),
            (api, 'load_key'), (api.ApiFootballClient, 'fixtures'),
        )
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(target, name, side_effect=AssertionError(name)))
                     for target, name in blocked]
            assessments = readiness.assess_sizes(history(400), '39', range(2, 21), BEFORE)
        self.assertEqual(len(assessments), 19)
        self.assertTrue(all(item['eligible'] for item in assessments))
        for mocked in mocks:
            mocked.assert_not_called()

    def test_insufficient_or_empty_history_has_nonnegative_requirements(self):
        for count, days in ((0, 3), (1, 1), (30, 3), (40, 1)):
            with self.subTest(count=count, days=days):
                assessment = readiness.assess_history(history(count, days), '39', 2, BEFORE)
                self.assertFalse(assessment['eligible'])
                self.assertGreaterEqual(assessment['missing_groups'], 0)
                self.assertGreaterEqual(assessment['missing_days'], 0)
                self.assertGreater(assessment['missing_matches'], 0)
                self.assertEqual(assessment['before'], BEFORE)

    def test_invalid_sizes_and_cutoff_fail_explicitly(self):
        for size in (1, 21, True, 2.0, '2'):
            with self.subTest(size=size), self.assertRaises(ValueError):
                readiness.assess_history(history(80), '39', size, BEFORE)
        for before in ('2025-02-30', '2025-08-01T00:00:00', ''):
            with self.subTest(before=before), self.assertRaises(ValueError):
                readiness.assess_history(history(80), '39', 4, before)


if __name__ == '__main__':
    unittest.main()
