from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import automation_health as health
import scheduler_operations as operations


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def identity(number):
    return f'{number:032x}'


class SchedulerOperationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = health.path(self.root)

    def database(self, *, refresh=True):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA user_version=1')
            con.execute('''CREATE TABLE cycles(id TEXT PRIMARY KEY,started_at TEXT,finished_at TEXT,
                duration_seconds REAL,status TEXT,origin TEXT,interval_minutes INTEGER,reasons TEXT,
                counters TEXT,last_seen_at TEXT,last_progress_at TEXT,phase TEXT)''')
            if refresh:
                con.execute('''CREATE TABLE refresh_events(id TEXT PRIMARY KEY,observed_at TEXT,
                    cycle_started_at TEXT,day TEXT,phase TEXT,counters TEXT)''')

    def cycle(self, number=1, *, start=NOW, end=None, duration=60, status='completed',
              seen=None, progress=None, phase='finished'):
        if not self.path.exists():
            self.database()
        end = end if end is not None else None if status == 'running' else start + timedelta(seconds=duration or 0)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('INSERT INTO cycles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (
                identity(number), start.isoformat(), end.isoformat() if end else None, duration,
                status, 'service', 15, '[]', '{"secret":"DO NOT EXPORT"}',
                seen.isoformat() if seen else None, progress.isoformat() if progress else None, phase))

    def event(self, number, when, phase, *, start=NOW, day=None):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute('INSERT INTO refresh_events VALUES(?,?,?,?,?,?)', (
                identity(number), when.isoformat(), start.isoformat(),
                day or when.astimezone(operations.ZONE).date().isoformat(), phase,
                '{"provider":"PRIVATE RESPONSE","secret":"DO NOT EXPORT"}'))

    def test_missing_database_is_not_created(self):
        report = operations.load(self.root, now=NOW)
        self.assertEqual(report['state'], 'missing')
        self.assertEqual(report['cycles'], [])
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_normal_finished_cycle_and_recent_operations_have_separate_timings(self):
        self.cycle(end=NOW + timedelta(minutes=10), duration=600)
        self.event(1, NOW + timedelta(minutes=1), 'calendar')
        self.event(2, NOW + timedelta(minutes=2), 'results')
        report = operations.load(self.root, now=NOW + timedelta(minutes=12))
        self.assertEqual(report['state'], 'available')
        row = report['cycles'][0]
        self.assertEqual((row['wall_duration_seconds'], row['measured_duration_seconds'],
                          row['wall_minus_measured_seconds']), (600, 600, 0))
        self.assertEqual(row['signal_state'], 'unavailable')
        self.assertEqual(report['latest_operations']['calendar']['state'], 'recent')
        self.assertEqual(report['latest_operations']['calendar']['age_seconds'], 660)
        self.assertEqual(report['latest_operations']['forecasts']['state'], 'missing')
        self.assertEqual(report['resumptions'], [])
        self.assertEqual(report['issues'], [])
        self.assertNotIn('DO NOT EXPORT', json.dumps(report))
        self.assertNotIn('PRIVATE RESPONSE', json.dumps(report))

    def test_wall_monotonic_divergence_is_descriptive_and_does_not_invent_cause(self):
        self.cycle(end=NOW + timedelta(hours=18), duration=1482)
        report = operations.load(self.root, now=NOW + timedelta(hours=19))
        row = report['cycles'][0]
        self.assertEqual(row['wall_duration_seconds'], 64800)
        self.assertEqual(row['wall_minus_measured_seconds'], 63318)
        self.assertEqual(row['issues'], ['wall_measured_divergence'])
        self.assertNotIn('sleep', report)
        self.assertNotIn('uptime', report)
        self.assertNotIn('cause', row)

    def test_same_cycle_resumed_results_show_calendar_age_before_resumption(self):
        self.cycle(end=NOW + timedelta(hours=18), duration=1482)
        self.event(1, NOW + timedelta(minutes=4), 'calendar')
        self.event(2, NOW + timedelta(hours=17, minutes=45), 'results')
        self.event(3, NOW + timedelta(hours=17, minutes=46), 'reconcile')
        report = operations.load(self.root, now=NOW + timedelta(hours=19))
        self.assertEqual(len(report['resumptions']), 1)
        row = report['resumptions'][0]
        self.assertEqual(row['cycle_id'], identity(1))
        self.assertEqual(row['gap_seconds'], 63660)
        self.assertEqual(row['resumed_phase'], 'results')
        self.assertEqual(row['calendar_age_seconds'], 63660)
        self.assertEqual(row['calendar_state'], 'stale')
        self.assertEqual(report['latest_operations']['calendar']['state'], 'stale')

    def test_resuming_calendar_does_not_hide_previous_calendar_age(self):
        self.cycle(end=NOW + timedelta(hours=2), duration=100)
        self.event(1, NOW, 'calendar')
        self.event(2, NOW + timedelta(hours=1), 'calendar')
        report = operations.load(self.root, now=NOW + timedelta(hours=2))
        self.assertEqual(report['resumptions'][0]['calendar_at'], NOW.isoformat())
        self.assertEqual(report['resumptions'][0]['calendar_age_seconds'], 3600)
        self.assertEqual(report['latest_operations']['calendar']['observed_at'], (NOW + timedelta(hours=1)).isoformat())

    def test_cross_cycle_gap_is_not_presented_as_same_process_resumption(self):
        self.cycle()
        self.cycle(2, start=NOW + timedelta(hours=2))
        self.event(1, NOW, 'calendar')
        self.event(2, NOW + timedelta(hours=2), 'results', start=NOW + timedelta(hours=2))
        report = operations.load(self.root, now=NOW + timedelta(hours=3))
        self.assertEqual(report['resumptions'], [])

    def test_recent_running_heartbeat_is_independent_from_stale_data(self):
        self.cycle(status='running', duration=None, seen=NOW + timedelta(hours=5),
                   progress=NOW, phase='history')
        self.event(1, NOW, 'calendar')
        report = operations.load(self.root, now=NOW + timedelta(hours=5, seconds=30))
        self.assertEqual(report['cycles'][0]['signal_state'], 'recent')
        self.assertEqual(report['latest_operations']['calendar']['state'], 'stale')
        self.assertIsNone(report['cycles'][0]['measured_duration_seconds'])

    def test_future_signal_is_never_recent(self):
        self.cycle(status='running', duration=None, seen=NOW + timedelta(hours=2), phase='history')
        report = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertEqual(report['cycles'][0]['signal_state'], 'invalid_clock')
        self.assertIn('future_signal', report['issues'])

    def test_future_and_inverted_timelines_are_explicit(self):
        self.cycle(start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=1), duration=600,
                   seen=NOW + timedelta(hours=3), progress=NOW + timedelta(hours=4))
        report = operations.load(self.root, now=NOW)
        self.assertEqual(report['state'], 'available')
        self.assertIsNone(report['cycles'][0]['wall_duration_seconds'])
        for code in ('future_cycle_start', 'future_cycle_end', 'end_before_start',
                     'future_signal', 'future_progress', 'progress_after_signal',
                     'signal_after_finish', 'progress_after_finish'):
            self.assertIn(code, report['issues'])

    def test_future_event_does_not_become_a_recent_operation_or_resumption(self):
        self.cycle(status='running', duration=None, phase='results')
        self.event(1, NOW, 'calendar')
        self.event(2, NOW + timedelta(hours=1), 'results')
        report = operations.load(self.root, now=NOW + timedelta(minutes=15))
        self.assertEqual(report['latest_operations']['results']['state'], 'invalid_clock')
        self.assertIn('future_event', report['issues'])
        self.assertEqual(report['resumptions'], [])

    def test_impossible_event_cycle_and_day_are_detected(self):
        self.cycle(end=NOW + timedelta(minutes=1))
        self.event(1, NOW - timedelta(minutes=1), 'calendar', day='2026-10-31')
        self.event(2, NOW + timedelta(minutes=2), 'results')
        self.event(3, NOW, 'forecasts', start=NOW - timedelta(days=2))
        report = operations.load(self.root, now=NOW + timedelta(minutes=3))
        for code in ('event_before_cycle', 'event_day_mismatch', 'event_after_cycle_end', 'cycle_identity_unavailable'):
            self.assertIn(code, report['issues'])
        self.assertEqual(report['resumptions'], [])

    def test_measured_duration_larger_than_wall_and_negative_are_not_silently_clamped(self):
        self.cycle(end=NOW + timedelta(seconds=10), duration=200)
        self.cycle(2, end=NOW + timedelta(seconds=10), duration=-1)
        report = operations.load(self.root, now=NOW + timedelta(minutes=1))
        self.assertIn('measured_exceeds_wall', report['issues'])
        self.assertIn('invalid_measured_duration', report['issues'])
        self.assertTrue(any(row['wall_minus_measured_seconds'] == -190 for row in report['cycles']))
        json.dumps(report, allow_nan=False)

    def test_exact_threshold_is_documented_as_monitoring_reference(self):
        self.cycle(end=NOW + timedelta(hours=2), duration=3600)
        self.event(1, NOW, 'calendar')
        self.event(2, NOW + timedelta(minutes=20), 'results')
        report = operations.load(self.root, now=NOW + timedelta(minutes=20), interval_minutes=10)
        self.assertEqual(report['stale_threshold_seconds'], 1200)
        self.assertEqual(len(report['resumptions']), 1)
        self.assertEqual(report['resumptions'][0]['calendar_state'], 'recent')
        self.assertEqual(report['latest_operations']['calendar']['state'], 'recent')

    def test_source_fingerprint_does_not_change_only_with_asof_and_derived_ages(self):
        self.cycle()
        self.event(1, NOW, 'calendar')
        one = operations.load(self.root, now=NOW + timedelta(hours=1))
        two = operations.load(self.root, now=NOW + timedelta(hours=2))
        self.assertEqual(one['source_sha256'], two['source_sha256'])
        self.assertNotEqual(one['as_of'], two['as_of'])
        self.assertNotEqual(one['latest_operations']['calendar']['age_seconds'], two['latest_operations']['calendar']['age_seconds'])
        self.event(2, NOW + timedelta(seconds=1), 'results')
        self.assertNotEqual(one['source_sha256'], operations.load(self.root, now=NOW + timedelta(hours=2))['source_sha256'])

    def test_source_fingerprint_includes_implementation(self):
        self.cycle()
        one = operations.load(self.root, now=NOW + timedelta(hours=1))
        with patch.object(operations, '_implementation', return_value='changed'):
            two = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertNotEqual(one['source_sha256'], two['source_sha256'])

    def test_invalid_measurement_changes_are_fingerprinted_without_exporting_freeform_text(self):
        self.cycle(duration=-1)
        one = operations.load(self.root, now=NOW + timedelta(hours=1))
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE cycles SET duration_seconds='PRIVATE MALFORMED MEASUREMENT'")
        two = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertEqual(two['state'], 'available')
        self.assertNotEqual(one['source_sha256'], two['source_sha256'])
        self.assertNotIn('PRIVATE MALFORMED MEASUREMENT', json.dumps(two))

    def test_read_only_no_api_and_one_sqlite_snapshot(self):
        self.cycle()
        self.event(1, NOW, 'calendar')
        import api_football
        before = self.path.read_bytes()
        connect = operations.sqlite3.connect
        calls = []

        def readonly(*args, **kwargs):
            self.assertIn('?mode=ro', args[0])
            self.assertTrue(kwargs['uri'])
            con = connect(*args, **kwargs)
            con.set_trace_callback(calls.append)
            return con

        with patch.object(operations.sqlite3, 'connect', side_effect=readonly), \
             patch.object(api_football.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
            report = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertEqual(report['state'], 'available')
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(sum(command == 'BEGIN' for command in calls), 1)
        self.assertTrue(any('query_only=ON' in command for command in calls))
        self.assertFalse(any(command.startswith(('INSERT', 'UPDATE', 'CREATE', 'DELETE')) for command in calls))

    def test_missing_old_refresh_table_is_reported_without_read_migration(self):
        self.database(refresh=False)
        self.cycle()
        before = self.path.read_bytes()
        report = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertEqual(report['state'], 'available')
        self.assertFalse(report['refresh_events_available'])
        self.assertEqual(before, self.path.read_bytes())

    def test_concurrent_writer_does_not_mix_cycle_and_event_snapshots(self):
        self.cycle()
        self.event(1, NOW, 'calendar')
        connect = operations.sqlite3.connect
        inserted = []

        class ConnectionProxy:
            def __init__(proxy, connection):
                proxy.connection = connection

            def execute(proxy, sql, parameters=()):
                if sql.startswith('SELECT id,observed_at') and not inserted:
                    # The reader has already selected cycles in its transaction.
                    # Commit a coherent new cycle/event pair concurrently.
                    with closing(connect(self.path)) as writer, writer:
                        second = NOW + timedelta(minutes=1)
                        writer.execute('INSERT INTO cycles VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (
                            identity(2), second.isoformat(), (second + timedelta(seconds=1)).isoformat(), 1,
                            'completed', 'service', 15, '[]', '{}', None, None, 'finished'))
                        writer.execute('INSERT INTO refresh_events VALUES(?,?,?,?,?,?)', (
                            identity(2), second.isoformat(), second.isoformat(), '2026-11-01', 'results', '{}'))
                    inserted.append(True)
                return proxy.connection.execute(sql, parameters)

            def close(proxy):
                proxy.connection.close()

        def reader(*args, **kwargs):
            return ConnectionProxy(connect(*args, **kwargs))

        with patch.object(operations.sqlite3, 'connect', side_effect=reader):
            report = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertEqual(inserted, [True])
        self.assertEqual(report['total_cycles'], 1)
        self.assertEqual(report['observed_events'], 1)
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['latest_operations']['results']['state'], 'missing')
        subsequent = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertEqual(subsequent['total_cycles'], 2)
        self.assertEqual(subsequent['observed_events'], 2)

    def test_malformed_database_and_unknown_values_fail_closed_without_exception_text(self):
        self.path.write_bytes(b'PRIVATE SECRET is not sqlite')
        report = operations.load(self.root, now=NOW)
        self.assertEqual(report['state'], 'unavailable_or_invalid')
        self.assertEqual(report['issues'], ['store_unavailable_or_invalid'])
        self.assertNotIn('PRIVATE SECRET', json.dumps(report))
        self.path.unlink()
        self.cycle()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("UPDATE cycles SET reasons='[\"PRIVATE SECRET\"]'")
        report = operations.load(self.root, now=NOW)
        self.assertEqual(report['state'], 'unavailable_or_invalid')
        self.assertEqual(report['cycles'], [])
        self.assertNotIn('PRIVATE SECRET', json.dumps(report))

    def test_truncation_is_explicit_not_a_data_deletion_or_hidden_cap(self):
        self.cycle(end=NOW + timedelta(minutes=3), duration=180)
        for offset in range(3):
            self.event(offset + 1, NOW + timedelta(minutes=offset), 'calendar')
        with patch.object(operations, 'EVENT_LIMIT', 2):
            report = operations.load(self.root, now=NOW + timedelta(hours=1))
        self.assertTrue(report['truncated'])
        self.assertEqual(report['observed_events'], 2)
        with closing(sqlite3.connect(self.path)) as con, con:
            self.assertEqual(con.execute('SELECT count(*) FROM refresh_events').fetchone()[0], 3)

    def test_parameters_are_strict_and_naive_time_is_refused(self):
        for value in (True, 0, 366, 1.5):
            with self.subTest(days=value), self.assertRaises(operations.OperationError):
                operations.load(self.root, days=value, now=NOW)
        for value in (True, 0, 1441, 1.5):
            with self.subTest(interval=value), self.assertRaises(operations.OperationError):
                operations.load(self.root, interval_minutes=value, now=NOW)
        with self.assertRaises(operations.OperationError):
            operations.load(self.root, now=NOW.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
