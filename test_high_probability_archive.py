"""Whole rankings are frozen per match, independently of group registration."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import high_probability_archive as archive
import high_probability_engine as engine
from test_high_probability_engine import fixture, history, NOW


def report():
    return engine.scan(history()+[fixture(100001), fixture(100002)], NOW)


class HighProbabilityArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)/'archive'
        self.report = report()

    def capture(self, value=None, clock=None):
        return archive.capture(self.report if value is None else value, root=self.root,
                               clock=(lambda: NOW) if clock is None else clock)

    def entries(self):
        return archive.list_entries(self.root)

    def reconcile(self, raw, now=NOW+timedelta(hours=10)):
        return archive.reconcile(raw, root=self.root, now=now)

    def test_tomorrow_archive_keeps_real_capture_time_and_validates_target_day_and_cutoff(self):
        tomorrow = (NOW+timedelta(days=1)).date()
        value = engine.scan(history()+[fixture(100001, stamp='2026-10-23T15:00:00+00:00')],
                            NOW, day=tomorrow)
        self.assertEqual(self.capture(value)['created'], 1)
        saved = self.entries()[0]
        self.assertEqual(saved['created_at'], NOW.isoformat())
        self.assertEqual(saved['day'], '2026-10-23')
        self.assertEqual(saved['status'], 'pending')
        self.assertEqual(saved['before'], '2026-10-22')
        invalid = deepcopy(value)
        invalid['rows'][0]['before'] = '2026-10-23'
        self.assertEqual(self.capture(invalid)['created'], 0)
        invalid = deepcopy(value)
        invalid['rows'][0]['fixture']['timestamp'] = '2026-10-24T15:00:00+00:00'
        self.assertEqual(self.capture(invalid)['created'], 0)
        # Local midnight changes the real day, not the identity of the chosen date.
        midnight = NOW.replace(hour=23, minute=1)
        self.assertEqual(self.capture(value, clock=lambda: midnight)['existing'], 1)
        late = NOW+timedelta(days=1, hours=7)
        self.assertEqual(self.capture(value, clock=lambda: late)['created'], 0)

    def test_reading_missing_archive_does_not_create_directory(self):
        self.assertEqual(self.entries(), [])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.reconcile([])['entries'], 0)
        self.assertFalse(self.root.exists())

    def test_every_row_and_all_three_outcomes_are_frozen_without_group_or_api(self):
        original = deepcopy(self.report)
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')), \
                patch('prospective_models.build_mixed_methods', side_effect=AssertionError('No group')):
            result = self.capture()
        self.assertEqual(result, dict(created=2, existing=0, skipped=0))
        rows = self.entries()
        self.assertEqual(len(rows), 2)
        index = {row['fixture']['match_id']: row for row in rows}
        for row in self.report['rows']:
            saved = index[row['fixture']['match_id']]
            self.assertEqual(saved['probabilities'], row['probabilities'])
            self.assertEqual(set(saved['probabilities']), {'1st', 'Égal', '2nd'})
            self.assertEqual(saved['status'], 'pending')
            self.assertEqual(saved['fixture']['provider'], 'api-football')
            self.assertNotIn('score', saved['fixture'])
            self.assertNotIn('goals', saved['fixture'])
            self.assertIsNone(saved['actual'])
        self.assertEqual(self.report, original)

    def test_identical_captures_are_idempotent_and_distinct_percentages_append(self):
        self.capture()
        first = self.entries()
        result = self.capture(clock=lambda: NOW+timedelta(minutes=1))
        self.assertEqual(result, dict(created=0, existing=2, skipped=0))
        self.assertEqual(self.entries(), first)
        changed = deepcopy(self.report)
        changed['rows'][0]['probabilities'] = {'1st': .6, 'Égal': .2, '2nd': .2}
        result = self.capture(changed, clock=lambda: NOW+timedelta(minutes=2))
        self.assertEqual(result, dict(created=1, existing=1, skipped=0))
        all_rows = self.entries()
        self.assertEqual(len(all_rows), 3)
        latest = archive.latest(all_rows)
        self.assertEqual(len(latest), 2)
        mid = changed['rows'][0]['fixture']['match_id']
        self.assertEqual(next(row for row in latest if row['fixture']['match_id'] == mid)['probabilities'],
                         changed['rows'][0]['probabilities'])
        self.assertTrue(all(row in all_rows for row in first))

    def test_return_to_an_older_probability_appends_a_new_last_snapshot(self):
        value = deepcopy(self.report)
        value['rows'] = value['rows'][:1]
        self.capture(value)
        first = self.entries()[0]
        self.assertNotIn('previous_snapshot', first)
        changed = deepcopy(value)
        changed['rows'][0]['probabilities'] = {'1st': .6, 'Égal': .2, '2nd': .2}
        self.assertEqual(self.capture(changed, clock=lambda: NOW+timedelta(hours=1))['created'], 1)
        middle = self.entries()[-1]
        self.assertEqual(middle['previous_snapshot'], first['id'])
        self.assertEqual(self.capture(value, clock=lambda: NOW+timedelta(hours=2))['created'], 1)
        rows = self.entries()
        self.assertEqual(len(rows), 3)
        last = archive.latest(rows)[0]
        self.assertEqual(last['created_at'], (NOW+timedelta(hours=2)).isoformat())
        self.assertEqual(last['probabilities'], first['probabilities'])
        self.assertEqual(last['previous_snapshot'], middle['id'])
        self.assertNotEqual(last['id'], first['id'])
        self.assertEqual(self.capture(value, clock=lambda: NOW+timedelta(hours=2, minutes=30)),
                         dict(created=0, existing=1, skipped=0))
        self.assertEqual(self.entries(), rows)

    def test_changed_snapshot_cannot_be_recorded_with_a_nonadvancing_clock(self):
        self.capture()
        changed = deepcopy(self.report)
        changed['rows'][0]['probabilities'] = {'1st': .6, 'Égal': .2, '2nd': .2}
        with self.assertRaises(archive.ArchiveError):
            self.capture(changed)
        self.assertEqual(len(self.entries()), 2)

    def test_canonical_archive_ignores_scores_in_scan_fixture(self):
        self.report['rows'][0]['fixture'].update(score={'home': 99}, goals={'away': 99})
        self.capture()
        self.assertTrue(all('score' not in row['fixture'] and 'goals' not in row['fixture'] for row in self.entries()))

    def test_forecasts_and_result_revisions_reject_update_and_delete(self):
        self.capture()
        self.reconcile([fixture(100001, status='FT'), fixture(100002, status='FT')])
        with sqlite3.connect(archive.database_path(self.root)) as con:
            for sql in ('UPDATE forecasts SET day=day', 'DELETE FROM forecasts',
                        'UPDATE results SET revision=revision', 'DELETE FROM results'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    con.execute(sql)

    def test_expired_or_invalid_rows_are_not_archived(self):
        value = deepcopy(self.report)
        value['rows'][0]['fixture']['timestamp'] = '2026-10-22T08:00:00Z'
        value['rows'][1]['fixture']['raw_status'] = 'FT'
        self.assertEqual(self.capture(value), dict(created=0, existing=0, skipped=2))
        self.assertFalse(self.root.exists())
        for modify in (
            lambda row: row['probabilities'].update({'1st': float('nan')}),
            lambda row: row['fixture'].update(home_id=True),
            lambda row: row.update(before='2026-10-23'),
            lambda row: row.update(personalized=False),
        ):
            value = deepcopy(self.report)
            value['rows'] = value['rows'][:1]
            modify(value['rows'][0])
            with self.subTest(modify=modify):
                self.assertEqual(self.capture(value)['created'], 0)
        self.assertFalse(self.root.exists())

    def test_insert_and_commit_clocks_guard_against_kickoff_crossing(self):
        value = deepcopy(self.report)
        value['rows'] = value['rows'][:1]
        late = NOW+timedelta(hours=7)
        for times, raises in (([NOW, late, late], False), ([NOW, NOW, late], True)):
            with self.subTest(times=times), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                clock = iter(times)
                if raises:
                    with self.assertRaises(archive.ArchiveError):
                        archive.capture(value, root, clock=lambda: next(clock))
                else:
                    self.assertEqual(archive.capture(value, root, clock=lambda: next(clock))['created'], 0)
                self.assertEqual(archive.list_entries(root), [])

    def test_real_data_root_cannot_be_backdated(self):
        with patch.object(archive, '_connect', side_effect=AssertionError('No production write')):
            with self.assertRaises(archive.ArchiveError):
                archive.capture(self.report, api.DATA_DIR, clock=lambda: NOW)
        value = deepcopy(self.report)
        value['source'] = 'Other provider'
        with self.assertRaises(archive.ArchiveError):
            self.capture(value)
        self.assertFalse(self.root.exists())

    def test_regulation_time_result_is_independent_for_each_match(self):
        self.capture()
        raw = [fixture(100001, status='AET', ht=(1, 0), ft=(1, 0)), fixture(100002)]
        result = self.reconcile(raw)
        self.assertEqual((result['settled'], result['pending']), (1, 1))
        rows = {row['fixture']['match_id']: row for row in self.entries()}
        self.assertEqual(rows['100001']['actual'], '1st')
        self.assertEqual(rows['100001']['ft'], '1-0')
        self.assertIsNone(rows['100002']['actual'])
        self.assertEqual(rows['100001']['probabilities'], self.report['rows'][0]['probabilities'])

    def test_score_corrections_append_but_unchanged_result_does_not(self):
        self.capture()
        raw = [fixture(100001, status='FT', ht=(1, 0), ft=(1, 0)), fixture(100002)]
        self.reconcile(raw)
        first = self.entries()[0]
        self.assertEqual(self.reconcile(raw)['changed'], 0)
        raw[0]['score']['halftime'] = dict(home=0, away=0)
        self.assertEqual(self.reconcile(raw, now=NOW+timedelta(days=1))['changed'], 1)
        row = next(row for row in self.entries() if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['actual'], '2nd')
        self.assertEqual(len(row['result_history']), 2)
        self.assertEqual(row['result_history'][0]['actual'], '1st')
        self.assertEqual(row['probabilities'], first['probabilities'])
        self.reconcile([fixture(100001), fixture(100002)], now=NOW+timedelta(days=2))
        row = next(row for row in self.entries() if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['actual'], '2nd')
        self.assertEqual(len(row['result_history']), 2)

    def test_identity_changes_missing_scores_and_postponements_are_not_losses(self):
        self.capture()
        for raw, state in (([], 'unresolved'), ([fixture(100001, status='PST')], 'unresolved'),
                           ([fixture(100001, status='CANC')], 'void'),
                           ([fixture(100001, status='FT', home_id=999)], 'unresolved'),
                           ([fixture(100001, status='FT', ht=(None, None))], 'unresolved')):
            with self.subTest(state=state):
                self.reconcile(raw)
                row = next(row for row in self.entries() if row['fixture']['match_id'] == '100001')
                self.assertEqual(row['status'], state)
                self.assertIsNone(row['actual'])

    def test_final_reschedule_requires_forecast_before_both_times(self):
        self.capture()
        raw = [fixture(100001, status='FT', stamp='2026-10-22T16:00:00Z')]
        self.reconcile(raw)
        row = next(row for row in self.entries() if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['status'], 'settled')
        self.assertEqual(row['schedule_resolution']['effective_timestamp'], '2026-10-22T16:00:00')
        raw[0]['fixture']['date'] = '2026-10-22T08:00:00Z'
        self.reconcile(raw)
        row = next(row for row in self.entries() if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['status'], 'unresolved')
        self.assertIsNone(row['actual'])

    def test_damaged_forecast_and_result_payloads_fail_closed(self):
        self.capture()
        path = archive.database_path(self.root)
        with sqlite3.connect(path) as con:
            con.execute('DROP TRIGGER forecasts_no_update')
            con.execute("UPDATE forecasts SET payload='{}'")
        with self.assertRaises(archive.ArchiveError):
            self.entries()

    def test_conflicting_duplicate_fixture_is_excluded_as_a_whole(self):
        value = deepcopy(self.report)
        first = deepcopy(value['rows'][0])
        conflict = deepcopy(first)
        conflict['probabilities'] = {'1st': .6, 'Égal': .2, '2nd': .2}
        value['rows'] = [first, conflict]
        self.assertEqual(self.capture(value), dict(created=0, existing=0, skipped=2))
        self.assertFalse(self.root.exists())
        value['rows'] = [first, deepcopy(first)]
        self.assertEqual(self.capture(value), dict(created=1, existing=0, skipped=1))
        self.assertEqual(len(self.entries()), 1)

    def test_result_timestamp_before_kickoff_is_rejected_even_with_valid_payload_digest(self):
        self.capture()
        self.reconcile([fixture(100001, status='FT'), fixture(100002)])
        with sqlite3.connect(archive.database_path(self.root)) as con:
            con.execute('DROP TRIGGER results_no_update')
            con.execute("UPDATE results SET checked_at=? WHERE forecast_id=?",
                        (NOW.isoformat(), next(row['id'] for row in self.entries()
                                              if row['fixture']['match_id'] == '100001')))
        with self.assertRaises(archive.ArchiveError):
            self.entries()

    def test_older_result_observation_cannot_reverse_a_newer_correction(self):
        self.capture()
        raw = [fixture(100001, status='FT', ht=(1, 0), ft=(1, 0)), fixture(100002)]
        self.reconcile(raw, now=NOW+timedelta(days=2))
        raw[0]['score']['halftime'] = dict(home=0, away=0)
        self.assertEqual(self.reconcile(raw, now=NOW+timedelta(days=1))['changed'], 0)
        row = next(row for row in self.entries() if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['actual'], '1st')


if __name__ == '__main__':
    unittest.main()
