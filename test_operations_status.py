"""Operating evidence tests never access real services, credentials or data."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import operations_status as ops


NOW = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)


def response(**changes):
    values = dict(LoadState='loaded', ActiveState='active', SubState='waiting', UnitFileState='enabled',
                  LastTriggerUSec='Thu 2026-11-05 06:40:00 UTC',
                  NextElapseUSecRealtime='Thu 2026-11-05 07:15:00 UTC',
                  TimersMonotonic='{ OnUnitInactiveUSec=15min ; next_elapse=1h }')
    values.update(changes)
    return SimpleNamespace(returncode=0, stdout='\n'.join(f'{key}={value}' for key, value in values.items()))


def receipt(**changes):
    value = dict(version='backup-status-v1', observed_at=NOW.isoformat(), state='verified',
                 last_backup=dict(created_at=(NOW-timedelta(hours=1)).isoformat(), verified_at=NOW.isoformat(),
                                  files_verified=5397, databases_restored=14, payload_bytes=3229719534,
                                  point_name='first-aws-20261105'),
                 schedule=dict(interval_hours=24, retention_count=2), capacity=dict(free_bytes=5378859008),
                 last_attempt=dict(started_at=(NOW-timedelta(minutes=3)).isoformat(), finished_at=NOW.isoformat(),
                                   state='verified', reason_code='none'))
    value.update(changes)
    return value


class SchedulerStatusTests(unittest.TestCase):
    def runner(self, timer=None, worker=None):
        return Mock(side_effect=[timer or response(), worker or response(
            ActiveState='inactive', SubState='dead', UnitFileState='static', Result='success',
            ExecMainStartTimestamp='Thu 2026-11-05 06:40:00 UTC',
            ExecMainExitTimestamp='Thu 2026-11-05 06:55:00 UTC')])

    def test_timer_between_cycles_is_active_without_process_and_commands_are_bounded(self):
        runner = self.runner()
        status = ops.scheduler_status(platform='linux', runner=runner, now=NOW)
        self.assertTrue(status['available'])
        self.assertTrue(status['enabled'])
        self.assertTrue(status['running'])
        self.assertFalse(status['process_running'])
        self.assertEqual(status['interval_seconds'], 900)
        self.assertEqual(status['worker']['finished_at'], '2026-11-05T06:55:00+00:00')
        self.assertEqual(status['timer']['next_elapse_at'], '2026-11-05T07:15:00+00:00')
        self.assertEqual([call.args[0][2] for call in runner.call_args_list],
                         ['patternlab-worker.timer', 'patternlab-worker.service'])
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 2)
            self.assertFalse(call.kwargs.get('shell', False))
            self.assertEqual(call.kwargs['env']['TZ'], 'UTC')

    def test_oneshot_activating_means_process_running_and_next_trigger_not_guessed(self):
        runner = self.runner(timer=response(NextElapseUSecRealtime='n/a', NextElapseUSecMonotonic='0'),
                             worker=response(ActiveState='activating', SubState='start', Result='success'))
        status = ops.scheduler_status(platform='linux', runner=runner, now=NOW)
        self.assertTrue(status['process_running'])
        self.assertIsNone(status['timer']['next_elapse_at'])

    def test_real_systemd_multiple_timer_triggers_are_preserved(self):
        timer = response(NextElapseUSecRealtime='', NextElapseUSecMonotonic='infinity')
        timer.stdout += '\nTimersMonotonic={ OnActiveUSec=1min ; next_elapse=1d 9h }'
        value = ops.scheduler_status(platform='linux', runner=self.runner(timer=timer), now=NOW)
        self.assertTrue(value['available'])
        self.assertEqual(value['interval_seconds'], 900)
        self.assertIsNone(value['timer']['next_elapse_at'])

    def test_next_attempt_uses_the_earliest_calendar_or_monotonic_deadline(self):
        timer = response(NextElapseUSecRealtime='Fri 2026-11-06 02:00:00 UTC',
                         NextElapseUSecMonotonic='3700000000')
        with patch.object(ops, 'monotonic', return_value=100):
            value = ops.scheduler_status(platform='linux', runner=self.runner(timer=timer), now=NOW)
        self.assertEqual(value['timer']['next_elapse_at'], (NOW+timedelta(hours=1)).isoformat())

    def test_monotonic_target_is_converted_using_live_clock(self):
        runner = self.runner(timer=response(NextElapseUSecRealtime='n/a', NextElapseUSecMonotonic='200000000'))
        with patch.object(ops, 'monotonic', return_value=100):
            status = ops.scheduler_status(platform='linux', runner=runner, now=NOW)
        self.assertEqual(status['timer']['next_elapse_at'], (NOW+timedelta(seconds=100)).isoformat())

    def test_disabled_failed_and_absent_are_not_reported_enabled(self):
        runner = self.runner(timer=response(ActiveState='inactive', UnitFileState='disabled'),
                             worker=response(ActiveState='failed', SubState='failed', Result='exit-code'))
        status = ops.scheduler_status(platform='linux', runner=runner, now=NOW)
        self.assertFalse(status['enabled'])
        self.assertFalse(status['running'])
        self.assertFalse(status['process_running'])
        self.assertEqual(status['worker']['result'], 'exit-code')
        absent = ops.scheduler_status(platform='linux', runner=self.runner(timer=response(LoadState='not-found')), now=NOW)
        self.assertFalse(absent['installed'])

    def test_unknown_enablement_is_not_converted_to_disabled(self):
        status = ops.scheduler_status(platform='linux', runner=self.runner(timer=response(UnitFileState='')), now=NOW)
        self.assertIsNone(status['enabled'])

    def test_unsupported_platform_never_launches_process(self):
        runner = Mock()
        result = ops.scheduler_status(platform='darwin', runner=runner, now=NOW)
        self.assertFalse(result['supported'])
        self.assertFalse(result['available'])
        runner.assert_not_called()

    def test_failure_and_malformed_output_cannot_leak_errors_or_claim_activity(self):
        runners = [Mock(side_effect=OSError('PRIVATE_KEY=should-not-display')),
                   Mock(side_effect=subprocess.TimeoutExpired('secret', 2)),
                   Mock(return_value=SimpleNamespace(returncode=1, stdout='SECRET')),
                   Mock(return_value=SimpleNamespace(returncode=0, stdout='x'*40000)),
                   Mock(return_value=response(LoadState='secret'))]
        for runner in runners:
            with self.subTest(runner=runner):
                value = ops.scheduler_status(platform='linux', runner=runner, now=NOW)
                self.assertFalse(value['available'])
                self.assertFalse(value['running'])
                self.assertNotIn('secret', json.dumps(value).casefold())


class BackupStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / 'backup-status.json'
        self.root.chmod(0o700)

    def write(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def load(self, **arguments):
        return ops.backup_status(self.path, now=NOW, platform='darwin', **arguments)

    def test_receipt_is_only_past_verification_and_does_not_claim_live_schedule(self):
        self.write(receipt())
        value = self.load()
        self.assertTrue(value['available'])
        self.assertEqual(value['last_backup']['databases_restored'], 14)
        self.assertFalse(value['observation_stale'])
        self.assertFalse(value['backup_overdue'])
        self.assertFalse(value['scheduler']['available'])
        self.assertNotIn('enabled', value['schedule'])
        self.assertEqual(self.path.read_text(), json.dumps(receipt()))

    def test_receipt_and_verification_ages_are_separate(self):
        self.write(receipt(observed_at=(NOW-timedelta(hours=3)).isoformat()))
        value = self.load()
        self.assertTrue(value['observation_stale'])
        self.assertFalse(value['backup_overdue'])
        data = receipt()
        data['last_backup'].update(created_at=(NOW-timedelta(days=3)).isoformat(),
                                   verified_at=(NOW-timedelta(days=2)).isoformat())
        self.write(data)
        value = self.load()
        self.assertFalse(value['observation_stale'])
        self.assertTrue(value['backup_overdue'])

    def test_hourly_receipt_remains_current_and_duplicate_keys_are_rejected(self):
        self.write(receipt(observed_at=(NOW-timedelta(minutes=70)).isoformat()))
        self.assertFalse(self.load()['observation_stale'])
        content = json.dumps(receipt()).replace('"state": "verified"', '"state": "verified", "state": "error"', 1)
        self.path.write_text(content)
        self.assertEqual(self.load()['state'], 'invalid')

    def test_deferred_attempt_preserves_verified_backup(self):
        data = receipt(state='deferred')
        data['last_attempt'].update(state='deferred', reason_code='worker_busy')
        self.write(data)
        value = self.load()
        self.assertTrue(value['available'])
        self.assertEqual(value['state'], 'deferred')
        self.assertEqual(value['last_backup']['files_verified'], 5397)

    def test_future_invalid_and_untrusted_values_refused_without_exposing_content(self):
        cases = [receipt(observed_at=(NOW+timedelta(days=1)).isoformat()),
                 receipt(version='wrong'), receipt(last_backup=None), receipt(password='SECRET'),
                 receipt(observed_at='2026-11-05T07:00:00'), receipt(state='secret')]
        for field, replacement in [('files_verified', True), ('point_name', '../private'),
                                   ('payload_bytes', -1), ('verified_at', 'yesterday')]:
            item = receipt()
            item['last_backup'][field] = replacement
            cases.append(item)
        for item in cases:
            with self.subTest(item=item):
                self.write(item)
                value = self.load()
                self.assertFalse(value['available'])
                self.assertEqual(value['state'], 'invalid')
                self.assertNotIn('SECRET', json.dumps(value))
                self.assertIsNone(value['last_backup'])

    def test_missing_oversized_symlink_hardlink_permissions_and_owner_refused(self):
        self.assertFalse(self.load()['available'])
        self.write(receipt())
        self.path.chmod(0o666)
        self.assertEqual(self.load()['state'], 'invalid')
        self.path.chmod(0o600)
        self.assertEqual(self.load(expected_owner=os.getuid()+1)['state'], 'invalid')
        target = self.root / 'safe.json'
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(self.load()['state'], 'invalid')
        self.path.unlink()
        os.link(target, self.path)
        self.assertEqual(self.load()['state'], 'invalid')
        self.path.unlink()
        self.path.write_bytes(b'x'*(ops.MAX_RECEIPT_BYTES+1))
        self.path.chmod(0o600)
        self.assertEqual(self.load()['state'], 'invalid')

    def test_backup_timer_observed_independently_even_when_receipt_missing(self):
        runner = Mock(side_effect=[response(ActiveState='inactive', UnitFileState='disabled'),
                                  response(ActiveState='inactive', SubState='dead')])
        value = ops.backup_status(self.path, now=NOW, platform='linux', runner=runner)
        self.assertFalse(value['available'])
        self.assertFalse(value['scheduler']['enabled'])
        self.assertEqual([call.args[0][2] for call in runner.call_args_list],
                         ['patternlab-backup.timer', 'patternlab-backup.service'])


if __name__ == '__main__':
    unittest.main()
