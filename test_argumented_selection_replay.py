"""Causal replay checks against the unchanged production probability engines."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import api_football as api
import argumented_selection_engine as argumented
import argumented_selection_replay as replay
import high_probability_engine as hp


def raw(mid, stamp, value='2nd', *, league=61, home_id=611, away_id=612, status='FT'):
    ht, ft = {'1st': ((2, 0), (2, 0)), 'Égal': ((1, 0), (1, 1)),
              '2nd': ((0, 0), (2, 0))}[value]
    return dict(fixture=dict(id=mid, date=stamp, status=dict(short=status)),
        league=dict(id=league, name='Synthetic', country='Country', season=2024),
        teams=dict(home=dict(id=home_id, name='Home'), away=dict(id=away_id, name='Away')),
        score=dict(halftime=dict(zip(('home', 'away'), ht)),
                   fulltime=dict(zip(('home', 'away'), ft))))


def warmup(value='2nd', *, count=36, start='2023-11-01', **kwargs):
    first = datetime.fromisoformat(start+'T12:00:00+00:00')
    return [api.normalize_fixture(raw(i+1, (first+timedelta(days=i)).isoformat(), value, **kwargs))
            for i in range(count)]


def result(mid=1001, stamp='2024-01-02T12:00:00+00:00', value='2nd', **kwargs):
    return api.normalize_fixture(raw(mid, stamp, value, **kwargs))


def probabilities_only(records):
    return [{key: value for key, value in record.items() if key != 'actual'} for record in records]


class ArgumentedSelectionReplayTests(unittest.TestCase):
    def test_predictions_and_rule_match_actual_scan_and_build_for_three_labels(self):
        for outcome in replay.VALUES:
            with self.subTest(outcome=outcome):
                history = warmup(outcome)
                targets = [result(value=outcome), result(1002, '2024-01-02T15:00:00+00:00', outcome)]
                actual = replay.replay(history+targets, start='2024-01-01', end='2024-01-03')
                now = datetime(2024, 1, 2, tzinfo=timezone.utc)
                fixtures = [raw(int(row['match_id']), row['timestamp']+'+00:00', status='NS')
                            for row in targets]
                report = hp.scan(fixtures, now, day=date(2024, 1, 2), history=history+targets)
                selected = argumented.build(report, history+targets, now)
                by_id = {row['fixture']['match_id']: row for row in report['rows']}
                self.assertEqual(len(actual['records']), 2)
                self.assertEqual({row['match_id'] for row in actual['records'] if row['flags']['full_rule']},
                                 {row['fixture']['match_id'] for row in selected['rows']})
                for record in actual['records']:
                    expected = by_id[record['match_id']]
                    for key in ('probabilities', 'baseline', 'home_profile', 'away_profile',
                                'training', 'history_days', 'expected_goals', 'last_training_day', 'choice'):
                        self.assertEqual(record[key], expected[key], key)
                    self.assertEqual(record['choice'], outcome)
                    proof = next(row for row in selected['rows']
                                 if row['fixture']['match_id'] == record['match_id'])
                    for role in ('home', 'away'):
                        for key in ('ids', 'counts', 'matching', 'first_at', 'last_at'):
                            self.assertEqual(record['recent_'+role][key], proof['recent_'+role][key])

    def test_all_day_predictions_are_fixed_before_any_target_score_is_read(self):
        rows = warmup()+[result(), result(1002, '2024-01-02T15:00:00+00:00')]
        original = replay._outcome
        with patch.object(hp, '_estimate', wraps=hp._estimate) as estimate:
            def observe(items, diagnostics):
                self.assertEqual(estimate.call_count, 2)
                return original(items, diagnostics)
            with patch.object(replay, '_outcome', side_effect=observe):
                first = replay.replay(rows, start='2024-01-02', end='2024-01-03')
        changed = deepcopy(rows)
        for row in changed[-2:]:
            row.update(ht='2-0', ft='2-0', value='1st')
        second = replay.replay(changed, start='2024-01-02', end='2024-01-03')
        self.assertEqual(probabilities_only(first['records']), probabilities_only(second['records']))
        self.assertNotEqual([row['actual'] for row in first['records']],
                            [row['actual'] for row in second['records']])
        for record in first['records']:
            self.assertFalse(argumented._SCORE_FIELDS.intersection(record['fixture']))

    def test_future_same_day_and_later_conflicting_id_cannot_change_earlier_predictions(self):
        rows = warmup()+[result()]
        first = replay.replay(rows, start='2024-01-02', end='2024-01-05')
        later = deepcopy(rows[0])
        later.update(timestamp='2024-01-03T12:00:00', day='2024-01-03', ht=None, ft=None, value=None)
        injected = [result(2001, '2024-01-02T18:00:00+00:00', '1st'), later,
                    result(2002, '2025-01-01T12:00:00+00:00', '1st')]
        second = replay.replay(rows+injected, start='2024-01-02', end='2024-01-05')
        self.assertEqual(first['records'][0], second['records'][0])
        self.assertEqual(first['records'][0]['training'], 36)

    def test_prestart_warmup_and_incremental_next_day_are_used(self):
        rows = warmup()+[result(), result(1002, '2024-01-03T12:00:00+00:00')]
        actual = replay.replay(rows, start='2024-01-02', end='2024-01-04')
        self.assertEqual([row['training'] for row in actual['records']], [36, 37])
        self.assertEqual(actual['diagnostics']['warmup_count_before_start'], 36)
        self.assertEqual(actual['records'][1]['recent_home']['ids'][-1], '1001')
        self.assertEqual(actual['records'][1]['last_training_day'], '2024-01-02')

    def test_late_utc_boundary_uses_shared_earliest_cutoff_and_excludes_whole_day(self):
        rows = warmup()+[result(900, '2024-01-01T12:00:00+00:00', '1st'),
                        result(1001, '2024-01-01T23:30:00+00:00', '1st'),
                        result(1002, '2024-01-02T15:00:00+00:00', '2nd')]
        actual = replay.replay(rows, start='2024-01-02', end='2024-01-03')
        self.assertEqual(len(actual['records']), 2)
        for record in actual['records']:
            self.assertEqual(record['day'], '2024-01-02')
            self.assertEqual(record['cutoff_exclusive'], '2024-01-01')
            self.assertEqual(record['training'], 36)
            self.assertNotIn('900', record['recent_home']['ids'])
            self.assertNotIn('1001', record['recent_home']['ids'])
        now = datetime(2024, 1, 1, 23, tzinfo=timezone.utc)
        report = hp.scan([raw(1001, '2024-01-01T23:30:00+00:00', status='NS'),
                          raw(1002, '2024-01-02T15:00:00+00:00', status='NS')],
                         now, day=date(2024, 1, 2), history=rows)
        self.assertEqual(report['before'], actual['records'][0]['before'])
        for expected in report['rows']:
            record = next(row for row in actual['records'] if row['match_id'] == expected['fixture']['match_id'])
            self.assertEqual(record['probabilities'], expected['probabilities'])

    def test_zero_pool_empty_input_and_progress(self):
        observed = []
        empty = replay.replay([], start='2024-01-01', end='2024-01-03')
        self.assertEqual(empty['records'], [])
        self.assertEqual(empty['day_counts'], [])
        actual = replay.replay([result()], start='2024-01-01', end='2024-01-03', progress=observed.append)
        self.assertEqual(actual['records'], [])
        self.assertEqual(actual['day_counts'][0]['common_pool_count'], 0)
        self.assertEqual(observed[-1]['completed'], 1)

    def test_exact_role_id_and_league_common_pool_and_old_samples_variant(self):
        rows = warmup(start='2020-01-01')+[result()]
        actual = replay.replay(rows, start='2024-01-02', end='2024-01-03')['records'][0]
        self.assertTrue(actual['flags']['full_rule'])
        self.assertTrue(actual['flags']['min7'])
        self.assertFalse(actual['flags']['recent365'])
        for change in (dict(home_id='999'), dict(league='39', competition_code='39')):
            changed = deepcopy(rows)
            for row in changed[:28]:
                row.update(change)
            self.assertEqual(replay.replay(changed, start='2024-01-02', end='2024-01-03')['records'], [])

    def test_duplicates_foreign_sources_invalid_history_and_target_scores_are_conservative(self):
        rows = warmup()+[result()]
        duplicated = rows+[deepcopy(rows[0]), deepcopy(rows[-1])]
        original = deepcopy(duplicated)
        actual = replay.replay(duplicated, start='2024-01-02', end='2024-01-03')
        self.assertEqual(duplicated, original)
        self.assertEqual(len(actual['records']), 1)
        self.assertEqual(actual['records'][0]['training'], 36)
        self.assertEqual(actual['diagnostics']['history_duplicate_count'], 1)
        foreign = deepcopy(rows[0]); foreign.update(provider='forebet', value='1st')
        self.assertEqual(actual['records'], replay.replay(rows+[foreign], start='2024-01-02', end='2024-01-03')['records'])
        invalid = deepcopy(rows); invalid[-1].update(ht='9-0', ft='0-0', value='1st')
        checked = replay.replay(invalid, start='2024-01-02', end='2024-01-03')['records'][0]
        self.assertIsNone(checked['actual'])
        self.assertEqual(checked['flags'], actual['records'][0]['flags'])
        conflict = deepcopy(rows[0]); conflict.update(ht='2-0', ft='2-0', value='1st')
        checked = replay.replay(rows+[conflict], start='2024-01-02', end='2024-01-03')
        self.assertEqual(checked['records'][0]['training'], 35)

    def test_thresholds_and_date_contract_are_frozen(self):
        self.assertEqual(replay.PROTOCOL['strict_matching'], 7)
        self.assertEqual(replay.PROTOCOL['recent_days'], 365)
        self.assertFalse(replay.PROTOCOL['tuning'])
        for kwargs in (dict(start='2024-01-02', end='2024-01-02'),
                       dict(start='20240101'), dict(end='not-a-date')):
            with self.assertRaises(ValueError):
                replay.replay([], **kwargs)
        with self.assertRaises(ValueError):
            replay.replay(None)

    def test_a_shifted_duplicate_target_id_is_not_allowed_to_use_its_past_result(self):
        rows = warmup()+[result(1)]
        actual = replay.replay(rows, start='2024-01-02', end='2024-01-03')
        self.assertEqual(actual['records'], [])
        self.assertEqual(actual['diagnostics']['issue_counts']['target_identity_already_in_past'], 1)

    def test_calendar_365_variant_includes_the_boundary_and_freezes_strict_seven(self):
        history = warmup(start='2023-01-01')
        # Last ten home/away samples start on 2023-01-27: exactly 365 days
        # before the first target cutoff and 366 before the second.
        first = result(1001, '2024-01-27T12:00:00+00:00')
        second = result(1002, '2024-01-28T12:00:00+00:00')
        actual = replay.replay(history+[first, second], start='2024-01-27', end='2024-01-29')
        self.assertTrue(actual['records'][0]['flags']['recent365'])
        # Adding the first target evicts the oldest sample, so shift its IDs
        # to leave the original role sample collection intact for next day.
        first['home_id'], first['away_id'] = '8001', '8002'
        actual = replay.replay(history+[first, second], start='2024-01-27', end='2024-01-29')
        self.assertFalse(actual['records'][-1]['flags']['recent365'])
        self.assertEqual(actual['records'][-1]['recent_home']['matching'], 10)
        self.assertTrue(actual['records'][-1]['flags']['min7'])

    def test_exact_midnight_virtual_cutoff_and_exclusive_local_end(self):
        rows = warmup()+[result(1001, '2024-01-02T00:00:00+00:00'),
                        result(1002, '2024-01-02T23:30:00+00:00'),
                        result(1003, '2024-01-03T00:00:00+00:00')]
        actual = replay.replay(rows, start='2024-01-02', end='2024-01-03')
        self.assertEqual([row['match_id'] for row in actual['records']], ['1001'])
        record = actual['records'][0]
        self.assertEqual(record['prediction_at'], record['timestamp']+'+00:00')
        self.assertEqual(record['cutoff_exclusive'], '2024-01-02')
        self.assertEqual(actual['diagnostics']['outside_end_count'], 1)

    def test_fixed_six_seven_and_agreement_only_variants_keep_the_model_argmax(self):
        for matching in (0, 6, 7):
            with self.subTest(matching=matching):
                rows = warmup()
                for row in rows[-10:][:10-matching]:
                    row.update(ht='2-0', ft='2-0', value='1st')
                actual = replay.replay(rows+[result()], start='2024-01-02', end='2024-01-03')['records'][0]
                self.assertEqual(actual['choice'], '2nd')
                self.assertTrue(actual['flags']['agreement_only'])
                self.assertEqual(actual['recent_home']['matching'], matching)
                self.assertEqual(actual['flags']['full_rule'], matching >= 5)
                self.assertEqual(actual['flags']['min7'], matching >= 7)

    def test_replay_uses_incremental_estimates_without_full_training_rescans(self):
        rows = warmup()+[result(1000+i, f'2024-01-{2+i:02d}T12:00:00+00:00') for i in range(10)]
        with patch.object(hp, '_aggregate', side_effect=AssertionError('full rescan')):
            with patch.object(hp.predictor, 'training_rows', side_effect=AssertionError('full rescan')):
                actual = replay.replay(rows, start='2024-01-02', end='2024-01-13')
        self.assertEqual(len(actual['records']), 10)
        self.assertEqual([row['training'] for row in actual['records']], list(range(36, 46)))


if __name__ == '__main__':
    unittest.main()
