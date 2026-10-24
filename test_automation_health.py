from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import automation_health as health
import cloud_storage


NOW = datetime(2026, 10, 24, 8, tzinfo=timezone.utc)


class AutomationHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def record(self, start=NOW, *, end=None, origin='service', status='completed', issues=(), duration=2):
        report = dict(started_at=start.isoformat(), status=status, requests=2,
                      created=1, issues=list(issues), key='never save this secret')
        with patch.object(health.time, 'perf_counter', side_effect=[100, 100+duration]):
            with health.monitor(self.root, report, clock=lambda: end or start+timedelta(seconds=duration),
                                interval_minutes=15, origin=origin):
                pass
        return report

    def test_read_is_empty_without_creating_files_and_file_is_excluded_from_cloud(self):
        self.assertEqual(health.load(self.root, now=NOW)['rows'], [])
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(cloud_storage.CloudStorageError):
            cloud_storage._safe_path('api_football/' + health.FILENAME)

    def test_completed_refresh_observations_append_only_controlled_counts(self):
        for offset in (0,61):
            self.assertTrue(health.record_refresh(self.root,phase='calendar',when=NOW+timedelta(seconds=offset),
                cycle_started_at=NOW,day=NOW.date().isoformat(),counters=dict(requests=2+offset,secret='hidden',created=True)))
        with sqlite3.connect(health.path(self.root)) as connection:
            rows = connection.execute('SELECT observed_at,phase,counters FROM refresh_events ORDER BY observed_at').fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual([r[1] for r in rows],['calendar','calendar'])
        self.assertNotIn('hidden',str(rows))
        self.assertNotIn('created',str(rows))
        self.assertFalse(health.record_refresh(self.root,phase='uncontrolled',when=NOW,
            cycle_started_at=NOW,day=NOW.date().isoformat(),counters={}))
        with patch.object(health,'_connection',side_effect=OSError('disk full')):
            self.assertFalse(health.record_refresh(self.root,phase='calendar',when=NOW,
                cycle_started_at=NOW,day=NOW.date().isoformat(),counters={}))

    def test_attempt_retains_duration_counters_origin_and_only_controlled_reasons(self):
        report = self.record(status='paused', issues=['Quota API par minute épuisé secret abc123'])
        self.assertEqual(report['duration_seconds'], 2)
        view = health.load(self.root, now=NOW+timedelta(minutes=1))
        row = view['rows'][0]
        self.assertEqual((row['status'], row['origin'], row['duration_seconds']), ('paused', 'service', 2))
        self.assertEqual(row['reasons'], ['provider_quota'])
        self.assertEqual(row['counters'], {'requests': 2, 'created': 1})
        self.assertNotIn('secret', str(view))

    def test_running_without_finished_confirmation_is_not_counted_completed(self):
        report = dict(started_at=NOW.isoformat(), status='completed', issues=[])
        with health.monitor(self.root, report, clock=lambda: NOW, interval_minutes=15):
            view = health.load(self.root, now=NOW)
            self.assertIsNone(view['rows'][0]['finished_at'])
            counts = health.summarize(view, now=NOW)['counts']
            self.assertEqual((counts['running'], counts['completed']), (1, 0))
        self.assertEqual(health.load(self.root, now=NOW)['rows'][0]['status'], 'completed')

    def test_unexpected_exception_and_interrupt_are_retained_without_exception_text(self):
        for exception, expected in ((RuntimeError('private token'), 'error'),
                                    (KeyboardInterrupt('private token'), 'interrupted')):
            report = dict(started_at=NOW.isoformat(), status='completed', issues=[])
            with self.assertRaises(type(exception)):
                with health.monitor(self.root, report, clock=lambda: NOW, interval_minutes=15):
                    raise exception
            view = health.load(self.root, now=NOW)
            self.assertTrue(any(row['status'] == expected for row in view['rows']))
            self.assertNotIn('private token', str(view))

    def test_diagnostics_failure_is_visible_and_does_not_hide_original_error(self):
        report = dict(started_at=NOW.isoformat(), status='completed', issues=[])
        with patch.object(health, '_connection', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'original error'):
                with health.monitor(self.root, report, clock=lambda: NOW, interval_minutes=15):
                    raise RuntimeError('original error')
        self.assertEqual(report['monitoring_warning'], health.MONITORING_WARNING)

    def test_disabled_monitor_never_creates_a_database(self):
        report = {}
        with health.monitor(self.root, report, clock=lambda: NOW, interval_minutes=15, enabled=False):
            report['status'] = 'completed'
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertNotIn('duration_seconds', report)

    def test_gap_checks_service_attempts_only_and_handles_long_cycle(self):
        self.record()
        self.record(NOW+timedelta(minutes=15), origin='manual')
        self.record(NOW+timedelta(minutes=60), duration=2)
        view = health.load(self.root, now=NOW+timedelta(minutes=80))
        summary = health.summarize(view, now=NOW+timedelta(minutes=80), schedule_expected=False)
        self.assertEqual(summary['service_observations'], 2)
        self.assertEqual(len(summary['gaps']), 1)
        self.assertEqual(summary['gaps'][0]['minutes'], 45)
        self.assertFalse(summary['gaps'][0]['ongoing'])
        # A service is only expected now when the inspected scheduler is loaded
        # and cycles are enabled. Historical gaps remain descriptive either way.
        self.assertEqual(len(health.summarize(view, now=NOW+timedelta(minutes=90), schedule_expected=True)['gaps']), 2)
        with tempfile.TemporaryDirectory() as folder:
            self.root = Path(folder)
            self.record(end=NOW+timedelta(hours=1), duration=3600)
            self.record(NOW+timedelta(minutes=65))
            view = health.load(self.root, now=NOW+timedelta(minutes=70))
            self.assertEqual(health.summarize(view, now=NOW+timedelta(minutes=70))['gaps'], [])

    def test_no_service_history_never_invents_uptime_or_a_missing_pre_history(self):
        self.record(origin='manual')
        view = health.load(self.root, now=NOW+timedelta(days=2))
        summary = health.summarize(view, now=NOW+timedelta(days=2), schedule_expected=True)
        self.assertEqual(summary['gaps'], [])
        self.assertEqual(summary['service_observations'], 0)
        self.assertNotIn('uptime', summary)

    def test_window_is_bounded_and_reports_truncation_without_deleting_history(self):
        for minute in range(4):
            self.record(NOW+timedelta(minutes=minute))
        view = health.load(self.root, now=NOW+timedelta(hours=1), limit=2)
        self.assertTrue(view['truncated'])
        self.assertEqual((view['total'], len(view['rows'])), (4, 2))
        self.assertEqual(view['first_started_at'], NOW.isoformat())

    def test_live_signal_distinguishes_presence_from_progress_and_never_creates_a_gap(self):
        report = dict(started_at=NOW.isoformat(),status='completed',requests=0,issues=[])
        current = NOW
        with health.monitor(self.root,report,clock=lambda:current,interval_minutes=15,origin='service') as pulse:
            pulse.progress('history')
            current = NOW+timedelta(hours=2)
            pulse.beat()
            view = health.load(self.root,now=current)
            self.assertEqual(view['rows'][0]['signal_state'],'recent')
            self.assertEqual(view['rows'][0]['phase'],'history')
            self.assertEqual(view['rows'][0]['last_progress_at'],NOW.isoformat())
            self.assertEqual(view['rows'][0]['elapsed_seconds'],7200)
            self.assertEqual(health.summarize(view,now=current,schedule_expected=True)['gaps'],[])
            report['requests'] = 2
            pulse.beat()
            self.assertEqual(health.load(self.root,now=current)['rows'][0]['last_progress_at'],current.isoformat())
            stale = health.load(self.root,now=current+timedelta(minutes=3))
            self.assertEqual(stale['rows'][0]['signal_state'],'stale')
            self.assertEqual(stale['rows'][0]['status'],'running')
        row = health.load(self.root,now=current)['rows'][0]
        self.assertEqual(row['status'],'completed')
        self.assertEqual(row['counters'],{'requests':2})

    def test_legacy_journal_remains_readable_without_migration_on_read(self):
        target = health.path(self.root)
        with sqlite3.connect(target) as con:
            con.execute('CREATE TABLE cycles (id,started_at,finished_at,duration_seconds,status,origin,interval_minutes,reasons,counters)')
            con.execute('PRAGMA user_version=1')
            con.execute('INSERT INTO cycles VALUES (?,?,?,?,?,?,?,?,?)',
                        ('old',NOW.isoformat(),None,None,'running','service',15,'[]','{}'))
        before = target.read_bytes()
        view = health.load(self.root,now=NOW+timedelta(hours=2))
        self.assertEqual(view['rows'][0]['signal_state'],'unavailable')
        self.assertEqual(view['rows'][0]['phase'],None)
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(health.summarize(view,now=NOW+timedelta(hours=2),schedule_expected=True)['gaps'],[])

    def test_confirmed_maintenance_stop_only_changes_explicit_unfinished_service_attempt(self):
        report = dict(started_at=NOW.isoformat(),status='completed',issues=[])
        with health.monitor(self.root,report,clock=lambda:NOW,interval_minutes=15,origin='service'):
            identity = health.load(self.root,now=NOW)['rows'][0]['id']
            self.assertEqual(health.confirm_service_interruption(self.root,[identity],confirmed_at=NOW),1)
            row = health.load(self.root,now=NOW)['rows'][0]
            self.assertEqual(row['status'],'interrupted')
            self.assertEqual(row['reasons'],['interrupted','maintenance'])
            self.assertIsNone(row['duration_seconds'])
        # A late monitor finalization cannot undo an explicitly confirmed stop.
        self.assertEqual(health.load(self.root,now=NOW)['rows'][0]['status'],'interrupted')
        self.assertEqual(health.confirm_service_interruption(self.root,[identity],confirmed_at=NOW),0)

    def test_corrupted_ledger_is_reported_without_schema_writes(self):
        health.path(self.root).write_bytes(b'invalid sqlite data')
        before = health.path(self.root).read_bytes()
        with self.assertRaises(health.HealthError):
            health.load(self.root, now=NOW)
        self.assertEqual(health.path(self.root).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
