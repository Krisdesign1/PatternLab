import copy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import api_football as api
import high_probability_engine as engine
from league_model import VALUES
import match_predictor as predictor


NOW = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)


def fixture(mid, *, league=61, stamp='2026-10-22T15:00:00+00:00', status='NS',
            home='Home', away='Away', ht=(0, 0), ft=(0, 0)):
    return dict(fixture=dict(id=mid, date=stamp, status=dict(short=status)),
                league=dict(id=league, name=f'League {league}', country='Country', season=2026),
                teams=dict(home=dict(id=league*10+1, name=home), away=dict(id=league*10+2, name=away)),
                score=dict(halftime=dict(zip(('home', 'away'), ht)),
                           fulltime=dict(zip(('home', 'away'), ft))),
                goals=dict(home=9, away=9))


def history(*, league=61, count=36, start=1, equal=False, days=6):
    scores = [((2, 0), (2, 0)), ((1, 0), (1, 1)), ((0, 0), (2, 0))]
    output = []
    for i in range(count):
        ht, ft = ((0, 0), (0, 0)) if equal else scores[i % len(scores)]
        stamp = (datetime(2026, 10, 1, 12, tzinfo=timezone.utc)+timedelta(days=i % days)).isoformat()
        output.append(fixture(start+i, league=league, status='FT', stamp=stamp, ht=ht, ft=ft))
    return output


def ranked_row(mid, probabilities, *, rank=1, stamp='2026-10-22T15:00:00+00:00'):
    choice = max(VALUES, key=probabilities.get)
    return dict(rank=rank, choice=choice, max_probability=probabilities[choice],
                probabilities=probabilities,
                fixture=dict(match_id=str(mid), timestamp=stamp))


class HighProbabilityEngineTests(unittest.TestCase):
    def test_ranks_probability_across_competitions_without_support_or_count_cap(self):
        raw = history(league=1, count=30, equal=True)
        raw += history(league=2, count=180, start=1000)
        raw += [fixture(100000+i, league=1 if i % 2 else 2) for i in range(160)]
        result = engine.scan(raw, NOW)
        self.assertEqual(len(result['rows']), 160)
        self.assertEqual(result['diagnostics']['day_count'], 160)
        self.assertEqual(result['diagnostics']['upcoming_count'], 160)
        self.assertTrue(all(row['fixture']['league'] == '1' for row in result['rows'][:80]))
        self.assertEqual(result['rows'][0]['training'], 30)
        self.assertEqual(result['rows'][-1]['training'], 180)
        self.assertEqual([row['rank'] for row in result['rows']], list(range(1, 161)))
        self.assertEqual(result['rows'][0]['choice'], 'Égal')

    def test_probabilities_and_profiles_equal_existing_clubs_estimate(self):
        raw = history()
        # Same team's distinct venue contributions and the regulation AET score
        # must be identical to the established model's behavior.
        raw += [fixture(500, status='AET', stamp='2026-10-10T16:00:00+00:00',
                        home='Other', away='Away', ht=(1, 0), ft=(1, 2)),
                fixture(501, status='PEN', stamp='2026-10-11T16:00:00+00:00',
                        home='Home', away='Other', ht=(0, 2), ft=(3, 3))]
        target = fixture(100001)
        result = engine.scan(raw+[target], NOW)
        actual = result['rows'][0]
        expected = predictor.estimate([api.normalize_fixture(row) for row in raw],
                                      '61', 'Home', 'Away', result['before'])
        for key, value in expected.items():
            self.assertEqual(actual[key], value, key)
        self.assertEqual(actual['history_days'], 8)
        self.assertEqual(actual['choice'], max(VALUES, key=expected['probabilities'].get))
        self.assertAlmostEqual(sum(actual['probabilities'].values()), 1)

    def test_no_target_scores_same_utc_day_or_future_history_used(self):
        target = fixture(100001)
        raw = history()+[target]
        original = engine.scan(raw, NOW)
        target['score'] = {'halftime': {'home': 100, 'away': 100},
                           'fulltime': {'home': 0, 'away': 0}}
        target['goals'] = None
        injected = [fixture(10000+i, status='FT', stamp=stamp, ht=(10, 0), ft=(10, 0))
                    for i, stamp in enumerate(('2026-10-22T01:00:00+00:00',
                                               '2026-10-22T17:00:00+00:00',
                                               '2026-10-23T12:00:00+00:00'))]
        after = engine.scan(raw+injected, NOW)
        self.assertEqual(after['rows'], original['rows'])
        self.assertEqual(after['history_fingerprint'], original['history_fingerprint'])
        self.assertNotIn('score', after['rows'][0]['fixture'])
        self.assertNotIn('value', after['rows'][0]['fixture'])

    def test_kinshasa_day_and_whole_earliest_utc_day_cutoff(self):
        now = datetime.fromisoformat('2026-10-22T00:01:00+01:00')
        targets = [fixture(100001, stamp='2026-10-21T23:05:00+00:00'),
                   fixture(100002, stamp='2026-10-22T22:59:00+00:00'),
                   fixture(100003, stamp='2026-10-22T23:00:00+00:00')]
        leakage = fixture(90001, status='FT', stamp='2026-10-21T12:00:00+00:00', ht=(10, 0), ft=(10, 0))
        result = engine.scan(history()+targets+[leakage], now)
        self.assertEqual(result['day'], '2026-10-22')
        self.assertEqual(result['before'], '2026-10-21')
        self.assertEqual({row['fixture']['match_id'] for row in result['rows']}, {'100001', '100002'})
        self.assertEqual(result['rows'][0]['training'], 36)

    def test_status_confirmed_participants_and_expired_kickoff(self):
        raw = history()+[fixture(100001), fixture(100002, status='TBD'),
                         fixture(100003, status='1H'), fixture(100004, status='HT'),
                         fixture(100005, status='PST'), fixture(100006, status='FT'),
                         fixture(100007, stamp=NOW.isoformat()),
                         fixture(100008, home='TBD'), fixture(100009, away='Home')]
        result = engine.scan(raw, NOW)
        self.assertEqual([row['fixture']['match_id'] for row in result['rows']], ['100001'])
        self.assertEqual(result['diagnostics']['day_count'], 9)
        self.assertEqual(result['diagnostics']['upcoming_count'], 1)
        self.assertEqual(result['diagnostics']['excluded_counts'],
                         {'not_upcoming': 5, 'kickoff_elapsed': 1, 'unconfirmed_fixture': 2})

    def test_exact_duplicates_deduplicate_conflicting_ids_never_cherry_pick(self):
        target, conflict = fixture(100001), fixture(100002)
        revised = copy.deepcopy(conflict)
        revised['fixture']['status']['short'] = '1H'
        original_history = history()
        historical_conflict = copy.deepcopy(original_history[0])
        historical_conflict['score']['fulltime'] = {'home': 9, 'away': 9}
        raw = original_history+[historical_conflict, target, copy.deepcopy(target), conflict, revised]
        first, second = engine.scan(raw, NOW), engine.scan(list(reversed(raw)), NOW)
        self.assertEqual(first['rows'], second['rows'])
        self.assertEqual(len(first['rows']), 1)
        self.assertEqual(first['rows'][0]['training'], 35)
        self.assertEqual(first['diagnostics']['duplicate_count'], 1)
        self.assertEqual(first['diagnostics']['excluded_counts']['conflicting_duplicate'], 2)

    def test_insufficient_league_days_and_role_profiles_are_explicitly_excluded(self):
        raw = history(league=1, count=29)
        raw += history(league=2, count=30, days=2, start=1000)
        raw += history(league=3, count=36, start=2000)
        raw += [fixture(100001, league=1), fixture(100002, league=2),
                fixture(100003, league=3, home='New home club'),
                fixture(100004, league=3, away='New away club'),
                fixture(100005, league=3, home='Away', away='Home')]
        result = engine.scan(raw, NOW)
        self.assertFalse(result['rows'])
        self.assertEqual(result['diagnostics']['excluded_counts'],
                         {'insufficient_league_history': 1, 'insufficient_history_days': 1,
                          'insufficient_club_history': 3})
        self.assertTrue(all(item['detail'] and item['fixture'] for item in result['excluded']))

    def test_invalid_history_does_not_turn_missing_scores_into_zero(self):
        raw = history()
        raw[0]['score']['halftime']['home'] = None
        raw[1]['score']['fulltime'] = {'home': 0, 'away': 0}
        raw[2]['fixture']['date'] = '2026-10-02T12:00:00'
        result = engine.scan(raw+[fixture(100001)], NOW)
        self.assertEqual(result['rows'][0]['training'], 33)
        self.assertEqual(result['diagnostics']['invalid_history_count'], 2)
        self.assertEqual(result['diagnostics']['excluded_counts']['invalid_fixture_date'], 1)

    def test_filter_uses_unrounded_probability_and_validates_arguments(self):
        report = dict(rows=[
            ranked_row(1, {'1st': .2, 'Égal': .1, '2nd': .7}, rank=1),
            ranked_row(2, {'1st': .50000001, 'Égal': .2, '2nd': .29999999}, rank=2),
            ranked_row(3, {'1st': .2, 'Égal': .5, '2nd': .3}, rank=3),
            ranked_row(4, {'1st': .3, 'Égal': .20000001, '2nd': .49999999}, rank=4)])
        result = engine.filter_rows(report)
        self.assertEqual([row['rank'] for row in result], [1, 2, 3])
        self.assertEqual([row['search_probability'] for row in result], [.7, .50000001, .5])
        self.assertTrue(all(row['search_outcome'] is None for row in result))
        self.assertEqual([row['fixture']['match_id'] for row in engine.filter_rows(report, .5, '1st')], ['2'])
        self.assertFalse(engine.filter_rows(report, 1))
        for value in (True, False, -.1, 1.1, '0.5', None, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.filter_rows(report, value)
        for outcome in ('egal', 'all', True):
            with self.assertRaises(ValueError):
                engine.filter_rows(report, outcome=outcome)

    def test_first_half_search_includes_second_half_favorites(self):
        report = dict(rows=[ranked_row(1, {'1st': .45, 'Égal': .05, '2nd': .50})])
        result = engine.filter_rows(report, .45, '1st')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['search_outcome'], '1st')
        self.assertEqual(result[0]['search_probability'], .45)
        self.assertEqual(result[0]['choice'], '2nd')
        self.assertEqual(result[0]['max_probability'], .50)
        self.assertFalse(engine.filter_rows(report, .46, '1st'))
        self.assertEqual(len(engine.filter_rows(report, .50)), 1)

    def test_equal_search_reorders_all_matches_and_uses_category_threshold(self):
        report = dict(rows=[
            ranked_row(1, {'1st': .10, 'Égal': .20, '2nd': .70}, rank=1),
            ranked_row(2, {'1st': .40, 'Égal': .35, '2nd': .25}, rank=2),
            ranked_row(3, {'1st': .34, 'Égal': .33, '2nd': .33}, rank=3)])
        result = engine.filter_rows(report, 0, 'Égal')
        self.assertEqual([row['fixture']['match_id'] for row in result], ['2', '3', '1'])
        self.assertEqual([row['rank'] for row in result], [1, 2, 3])
        self.assertEqual([row['search_probability'] for row in result], [.35, .33, .20])
        self.assertTrue(all(row['search_outcome'] == 'Égal' for row in result))
        above = engine.filter_rows(report, .34, 'Égal')
        self.assertEqual([row['fixture']['match_id'] for row in above], ['2'])
        self.assertFalse(engine.filter_rows(report, .50, 'Égal'))

    def test_category_ties_use_kickoff_then_numeric_match_id(self):
        report = dict(rows=[
            ranked_row(10, {'1st': .4, 'Égal': .3, '2nd': .3}, rank=1),
            ranked_row(2, {'1st': .4, 'Égal': .1, '2nd': .5}, rank=2),
            ranked_row(20, {'1st': .4, 'Égal': .2, '2nd': .4}, rank=3,
                       stamp='2026-10-22T14:00:00+00:00')])
        result = engine.filter_rows(report, .4, '1st')
        self.assertEqual([row['fixture']['match_id'] for row in result], ['20', '2', '10'])
        self.assertEqual([row['rank'] for row in result], [1, 2, 3])

    def test_independent_rankings_do_not_mutate_report_or_recalculate_model(self):
        report = engine.scan(history()+[fixture(100001)], NOW)
        original = copy.deepcopy(report)
        with patch.object(predictor, 'estimate', side_effect=AssertionError('No refitting')):
            for outcome in (*VALUES, None):
                result = engine.filter_rows(report, 0, outcome)
                self.assertIsNot(result[0], report['rows'][0])
                for key in ('choice', 'max_probability', 'probabilities', 'home_profile',
                            'away_profile', 'history_fingerprint'):
                    self.assertEqual(result[0][key], report['rows'][0][key], key)
                self.assertEqual(report, original)
        self.assertNotIn('search_outcome', report['rows'][0])
        self.assertNotIn('search_probability', report['rows'][0])

    def test_training_aggregates_once_per_league_instead_of_once_per_fixture(self):
        raw = history()+[fixture(100000+i) for i in range(40)]
        with patch.object(predictor, 'goals', wraps=predictor.goals) as goals:
            report = engine.scan(raw, NOW)
        self.assertEqual(len(report['rows']), 40)
        self.assertEqual(goals.call_count, 36)

    def test_foreign_provider_bad_input_naive_now_and_empty_report(self):
        bad = fixture(100001)
        bad['provider'] = 'football-data'
        report = engine.scan([bad, None, {'fixture': {'id': True}}], NOW)
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['before'], '2026-10-22')
        self.assertEqual(report['diagnostics']['excluded_counts'],
                         {'foreign_provider': 1, 'invalid_fixture': 1, 'invalid_fixture_id': 1})
        self.assertEqual(engine.filter_rows(report), [])
        with self.assertRaises(ValueError):
            engine.scan([], datetime(2026, 10, 22))
        with self.assertRaises(ValueError):
            engine.scan({})
        with self.assertRaises(ValueError):
            engine.filter_rows({})


if __name__ == '__main__':
    unittest.main()
