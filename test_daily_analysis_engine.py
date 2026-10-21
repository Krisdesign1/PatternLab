"""Daily planning is synthetic, deterministic, causal, and never requests data."""
import copy
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import math
import unittest
from unittest.mock import patch

import api_football as api
import api_match_workspace as workspace
import daily_analysis_engine as engine
import match_predictor
import prospective_models


NOW = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)


def raw_fixture(mid, stamp, league=39, status='NS'):
    return dict(fixture=dict(id=mid, date=stamp, status=dict(short=status)),
        league=dict(id=league, name=f'League {league}', country=f'Country {league}', season=2026),
        teams=dict(home=dict(id=league * 10 + 1, name=f'Home {league}'),
                   away=dict(id=league * 10 + 2, name=f'Away {league}')),
        goals=dict(home=1, away=1),
        score=dict(halftime=dict(home=1, away=0), fulltime=dict(home=1, away=1)))


def history(count=60, league=39, days=6, first_id=None):
    first_id = first_id if first_id is not None else league * 10000
    return [raw_fixture(first_id + i,
        (datetime(2026, 10, 10, 12, tzinfo=timezone.utc) + timedelta(days=i % days, seconds=i // days)).isoformat(),
        league, 'FT') for i in range(count)]


def future(count=4, leagues=(39,), now=NOW):
    return [raw_fixture(9000 + i, (now + timedelta(hours=1, minutes=2*i)).isoformat(),
                        leagues[i % len(leagues)]) for i in range(count)]


def candidate_sets(report):
    return {frozenset(row['match_id'] for row in group['fixtures']) for group in report['candidates']}


class DailyAnalysisEngineTests(unittest.TestCase):
    def test_mono_and_mixed_groups_remain_available_with_competition_context(self):
        report = engine.plan(history(60, 39) + history(60, 61) + future(4, (39, 61)), now=NOW)
        self.assertGreater(len(report['candidates']), 1)
        self.assertEqual(report['diagnostics']['eligible_matches'], 4)
        self.assertTrue(any(row['mixed'] for row in report['candidates']))
        mono = engine.plan(history() + future(), now=NOW)
        self.assertTrue(mono['candidates'])
        self.assertTrue(all(not row['mixed'] for row in mono['candidates']))
        for row in report['candidates']:
            leagues = {fixture['league'] for fixture in row['fixtures']}
            self.assertEqual({c['league'] for c in row['competitions']}, leagues)
            self.assertEqual(row['mixed'], len(leagues) > 1)
            self.assertEqual(row['fixtures'], sorted(row['fixtures'], key=lambda f: (f['timestamp'], int(f['match_id']))))
            self.assertEqual(row['size'], len(row['fixtures']))
            self.assertTrue(all(f['provider'] == 'api-football' for f in row['fixtures']))

    def test_all_sizes_two_to_twenty_without_a_top_n_cap_or_combinatorial_growth(self):
        count = 200
        report = engine.plan(history() + future(count), now=NOW)
        groups = report['candidates']
        self.assertEqual({row['size'] for row in groups}, set(range(2, 21)))
        self.assertGreater(len(groups), 100)
        self.assertLessEqual(len(groups), sum(count // size for size in range(2, 21)) + math.ceil(count / 20) + 6 + 1)
        self.assertLessEqual(sum(row['size'] for row in groups), 20 * count + 2)
        self.assertFalse(report['diagnostics']['exhaustive'])
        self.assertEqual(report['diagnostics']['candidate_count'], len(groups))

    def test_main_partitions_are_disjoint_per_size_and_every_match_has_a_group(self):
        pool = future(23)
        report = engine.plan(history() + pool, now=NOW)
        self.assertEqual(len(candidate_sets(report)), len(report['candidates']))
        for size in range(2, 21):
            groups = [row for row in report['candidates'] if size in row['selection']['nominal_sizes']]
            ids = [f['match_id'] for group in groups for f in group['fixtures']]
            self.assertEqual(len(ids), len(set(ids)), f'Partitions for size {size} overlap')
        covered = {f['match_id'] for group in report['candidates'] for f in group['fixtures']}
        self.assertEqual(covered, {str(f['fixture']['id']) for f in pool})

    def test_index_is_a_per_match_mean_and_does_not_reward_shorter_length(self):
        uniform = engine.plan(history() + future(20), now=NOW)
        scores = [row['selection']['data_support_index'] for row in uniform['candidates']]
        self.assertAlmostEqual(min(scores), max(scores))
        self.assertTrue(all(0 <= score <= 100 for score in scores))
        for row in uniform['candidates']:
            selection = row['selection']
            self.assertEqual(selection['data_support_scale'], 100)
            self.assertFalse(selection['temporal_evidence']['available'])
            self.assertIn('précision', selection['reason'])
            self.assertIn('indice', selection['reason'].casefold())
        strong = engine.plan(history(120, 39) + future(2), now=NOW)['candidates'][0]['selection']['data_support_index']
        weak = engine.plan(history(30, 61) + future(2, (61,)), now=NOW)['candidates'][0]['selection']['data_support_index']
        mixed = engine.plan(history(120, 39) + history(30, 61) + future(4, (39, 61)), now=NOW)
        full = next(row for row in mixed['candidates'] if row['size'] == 4)
        self.assertGreater(strong, weak)
        self.assertAlmostEqual(full['selection']['data_support_index'], (strong + weak) / 2)

    def test_role_and_league_thresholds_are_explicit(self):
        for count, days in ((29, 6), (60, 2)):
            with self.subTest(count=count, days=days):
                report = engine.plan(history(count, days=days) + future(2), now=NOW)
                self.assertEqual(report['candidates'], [])
                self.assertEqual(report['diagnostics']['exclusions']['insufficient_profiles'], 2)
                self.assertTrue(report['diagnostics']['excluded'][0]['reason'])
        rows = history(60)
        for row in rows[2:]:
            row['teams']['home']['name'] = 'Another home'
            row['teams']['home']['id'] = 8888
        report = engine.plan(rows + future(2), now=NOW)
        self.assertFalse(report['candidates'])
        self.assertEqual(report['diagnostics']['excluded'][0]['support']['home_role_matches'], 2)
        rows[2]['teams']['home'].update(name='Home 39', id=391)
        self.assertTrue(engine.plan(rows + future(2), now=NOW)['candidates'])

    def test_admission_matches_real_club_profile_eligibility_without_computing_predictions(self):
        raw = history(60) + future(4)
        report = engine.plan(raw, now=NOW)
        selected = [api.normalize_fixture(item) for item in raw if item['fixture']['status']['short'] == 'FT']
        fixture = report['candidates'][0]['fixtures'][0]
        train = match_predictor.training_rows(selected, fixture['league'], report['before'])
        home = match_predictor.profile(train, fixture['home'], 'home')
        away = match_predictor.profile(train, fixture['away'], 'away')
        support = report['candidates'][0]['selection']['support']
        self.assertEqual(support['history_matches_min'], len(train))
        self.assertEqual(support['history_days_min'], len({row['timestamp'][:10] for row in train}))
        self.assertEqual(support['club_role_matches_min'], min(home['matches'], away['matches']))

    def test_only_future_ns_confirmed_participants_of_today_are_admitted(self):
        base = future(2)
        extras = []
        for index, status in enumerate(('HT', '2H', 'FT', 'PST', 'TBD', 'CANC')):
            extras.append(raw_fixture(9100 + index, (NOW + timedelta(hours=2)).isoformat(), status=status))
        extras.append(raw_fixture(9200, NOW.isoformat()))
        extras.append(raw_fixture(9201, (NOW - timedelta(minutes=1)).isoformat()))
        extras.append(raw_fixture(9202, (NOW + timedelta(days=1)).isoformat()))
        unknown = raw_fixture(9203, (NOW + timedelta(hours=3)).isoformat())
        unknown['teams']['home']['name'] = 'TBD'
        extras.append(unknown)
        report = engine.plan(history() + base + extras, now=NOW)
        self.assertEqual(report['diagnostics']['eligible_matches'], 2)
        self.assertEqual(candidate_sets(report), {frozenset(('9000', '9001'))})
        self.assertEqual(report['diagnostics']['exclusions']['kickoff_passed'], 2)
        self.assertEqual(report['diagnostics']['exclusions']['unconfirmed_teams'], 1)

    def test_kinshasa_today_and_utc_history_cutoff_are_distinct(self):
        now = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc)
        games = [raw_fixture(9000, '2026-10-20T23:50:00Z'),
                 raw_fixture(9001, '2026-10-21T00:10:00Z'),
                 raw_fixture(9002, '2026-10-20T22:59:00Z'),
                 raw_fixture(9003, '2026-10-21T23:01:00Z')]
        report = engine.plan(history() + games, now=now)
        self.assertEqual(report['day'], '2026-10-21')
        self.assertEqual(report['before'], '2026-10-20')
        self.assertEqual(candidate_sets(report), {frozenset(('9000', '9001'))})

    def test_same_day_or_future_outcomes_never_increase_support(self):
        raw = history() + future(2)
        expected = engine.plan(raw, now=NOW)['candidates']
        invalid = raw_fixture(1000000, '2026-10-21T08:00:00Z', status='FT')
        invalid['score'] = None
        later = raw_fixture(1000001, '2026-10-22T01:00:00Z', status='FT')
        later['score'] = None
        actual = engine.plan(raw + [invalid, later], now=NOW)
        self.assertEqual(actual['candidates'], expected)
        self.assertEqual(actual['diagnostics']['history_issues'], [])

    def test_invalid_past_scores_are_diagnostic_and_do_not_block_other_leagues(self):
        invalid = history(1, 61)[0]
        invalid['score']['halftime'] = None
        report = engine.plan(history() + future(2) + [invalid], now=NOW)
        self.assertTrue(report['candidates'])
        self.assertEqual(report['diagnostics']['history_issues'], [dict(league='61', matches=1)])

    def test_fixture_results_odds_and_agreement_do_not_rank_candidates(self):
        raw = history() + future(8)
        expected = engine.plan(raw, now=NOW)['candidates']
        changed = copy.deepcopy(raw)
        for item in changed:
            item['odds'] = 999
            item['confidence'] = 1
            item['models_agree'] = True
            item['score'] = dict(halftime=dict(home=0, away=0), fulltime=dict(home=7, away=0))
        self.assertEqual(engine.plan(changed, now=NOW)['candidates'], expected)

    def test_existing_candidates_ids_and_legacy_fixture_sets_are_not_recreated(self):
        raw = history() + future(8)
        original = engine.plan(raw, now=NOW)
        first = original['candidates'][0]
        for existing in ([first], [first['id']], [dict(id='legacy-namespace', fixtures=first['fixtures'])]):
            with self.subTest(existing=existing):
                report = engine.plan(raw, now=NOW, existing=existing)
                self.assertEqual(report['diagnostics']['existing_skipped'], 1)
                self.assertEqual(len(report['candidates']), len(original['candidates']) - 1)
                self.assertNotIn(first['id'], {row['id'] for row in report['candidates']})

    def test_plan_is_stable_under_input_order_json_roundtrip_and_does_not_mutate_input(self):
        raw = history() + history(60, 61) + future(25, (39, 61))
        original = copy.deepcopy(raw)
        report = engine.plan(raw, now=NOW)
        self.assertEqual(engine.plan(reversed(raw), now=NOW), report)
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)
        self.assertEqual(raw, original)
        scores = [row['selection']['data_support_index'] for row in report['candidates']]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_api_key_model_prediction_training_or_store_access(self):
        blocked = ((api, 'load_key'), (api, 'load_fixtures'), (api, 'load_matches'), (api, 'save_fixtures'),
                   (api.ApiFootballClient, 'fixtures'), (match_predictor, 'estimate'),
                   (prospective_models, 'build_methods'))
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(module, name, side_effect=AssertionError(name)))
                     for module, name in blocked]
            self.assertTrue(engine.plan(history() + future(4), now=NOW)['candidates'])
        for mocked in mocks:
            mocked.assert_not_called()

    def test_identical_duplicates_are_counted_once_but_conflicting_ids_fail(self):
        raw = history() + future(4)
        expected = engine.plan(raw, now=NOW)['candidates']
        self.assertEqual(engine.plan(raw + [copy.deepcopy(raw[0])], now=NOW)['candidates'], expected)
        contradictory = copy.deepcopy(raw[-1])
        contradictory['league']['id'] = 61
        with self.assertRaises(engine.DailyAnalysisError):
            engine.plan(raw + [contradictory], now=NOW)

    def test_other_provider_is_not_silently_accepted_and_bad_clock_fails(self):
        raw = history() + future(2)
        for item in raw:
            item['provider'] = 'forebet'
        report = engine.plan(raw, now=NOW)
        self.assertFalse(report['candidates'])
        self.assertEqual(report['diagnostics']['invalid_raw'], len(raw))
        with self.assertRaises(engine.DailyAnalysisError):
            engine.plan([], now=datetime(2026, 10, 21))


if __name__ == '__main__':
    unittest.main()
