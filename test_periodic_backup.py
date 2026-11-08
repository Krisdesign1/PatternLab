"""Exercise real tiny SQLite/archive recovery; never touch runtime services."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from deployment.linux import backup
import operations_status


class PeriodicBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'project'
        (self.root / 'data').mkdir(parents=True)
        (self.root / 'data/state.json').write_text('{"counter":1}')
        (self.root / '.streamlit').mkdir()
        (self.root / '.streamlit/secrets.toml').write_text('NEVER INCLUDED')
        self.database = self.root / 'data/journal.sqlite3'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE forecasts (id INTEGER PRIMARY KEY, p REAL)')
            db.execute('INSERT INTO forecasts VALUES (1, .55)')
            db.commit()
        self.destination = self.base / 'backups'
        self.destination.mkdir()
        self.report = self.base / 'operations/backup-status.json'

    def run_backup(self, **kwargs):
        return backup.run(self.root, self.destination, self.report, idle=lambda: True, reserve=0, **kwargs)

    def test_real_archive_restores_committed_wal_and_excludes_secrets(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('INSERT INTO forecasts VALUES (2, .45)')
            db.commit()
            db.execute('INSERT INTO forecasts VALUES (3, .3)')
            result = self.run_backup()
            self.assertEqual(result['state'], 'verified')
            self.assertEqual(result['last_backup']['databases_restored'], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM forecasts').fetchone()[0], 3)
        archive = self.destination / result['last_backup']['point_name']
        with tarfile.open(archive) as source:
            names = source.getnames()
            self.assertNotIn('files/.streamlit/secrets.toml', names)
            restored = self.base / 'restored.sqlite3'
            restored.write_bytes(source.extractfile('files/data/journal.sqlite3').read())
        with closing(sqlite3.connect(restored)) as db:
            self.assertEqual(db.execute('SELECT id FROM forecasts ORDER BY id').fetchall(), [(1,), (2,)])
        self.assertFalse(list(self.destination.glob('.daily-work-*')))
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o640)

    def test_low_disk_never_deletes_first_or_existing_backup(self):
        first = self.destination / 'first-aws-20261105'
        first.mkdir()
        (first / 'marker').write_text('preserve')
        with patch.object(backup.shutil, 'disk_usage', return_value=Mock(free=1)), \
                patch.object(backup.recovery, 'create_recovery_point') as create:
            result = self.run_backup()
        self.assertEqual(result['state'], 'deferred')
        self.assertEqual(result['last_attempt']['reason_code'], 'insufficient_space')
        create.assert_not_called()
        self.assertEqual((first / 'marker').read_text(), 'preserve')

    def test_busy_worker_defers_without_stopping_or_starting_services(self):
        with patch.object(backup.recovery, 'create_recovery_point') as create:
            result = backup.run(self.root, self.destination, self.report, idle=lambda: False)
        self.assertEqual(result['state'], 'deferred')
        self.assertEqual(result['last_attempt']['reason_code'], 'worker_busy')
        create.assert_not_called()

    def test_corrupt_archive_never_published_or_retained(self):
        original = backup.verify_archive

        def corrupt(archive, manifest, scratch, **kwargs):
            with archive.open('ab') as stream:
                stream.write(b'bad trailing bytes')
            archive.write_bytes(b'not gzip')
            return original(archive, manifest, scratch, **kwargs)

        with patch.object(backup, 'verify_archive', side_effect=corrupt), \
                patch.object(backup, 'retain_verified') as retention:
            result = self.run_backup()
        self.assertEqual(result['state'], 'error')
        self.assertEqual(list(self.destination.glob('daily-*.tar.gz')), [])
        retention.assert_not_called()

    def test_invalid_gzip_trailer_is_rejected_even_after_tar_end_marker(self):
        original = backup.verify_archive

        def corrupt_trailer(archive, manifest, scratch, **kwargs):
            raw = bytearray(archive.read_bytes())
            raw[-5] ^= 1  # gzip CRC, after the tar end-of-archive blocks
            archive.write_bytes(raw)
            return original(archive, manifest, scratch, **kwargs)

        with patch.object(backup, 'verify_archive', side_effect=corrupt_trailer):
            result = self.run_backup()
        self.assertEqual(result['state'], 'error')
        self.assertFalse(list(self.destination.glob('daily-*.tar.gz')))

    def test_initial_operator_verified_backup_survives_deferred_attempt(self):
        self.report.parent.mkdir()
        current = datetime.now(timezone.utc).isoformat()
        original = dict(created_at=current, verified_at=current, files_verified=5397,
                        databases_restored=14, payload_bytes=3229719534,
                        point_name='first-aws-20261105')
        self.report.write_text(json.dumps(dict(version=backup.VERSION, last_backup=original)))
        result = backup.run(self.root, self.destination, self.report, idle=lambda: False, force=True)
        self.assertEqual(result['state'], 'deferred')
        self.assertEqual(result['last_backup'], original)

    def test_retention_only_after_new_verified_point_preserves_first_and_two_good(self):
        first = self.destination / 'first-aws-20261105'
        first.mkdir()
        (first / 'marker').write_text('immutable first point')
        for _index in range(3):
            result = self.run_backup(force=True)
            self.assertEqual(result['state'], 'verified')
        self.assertEqual(len(list(self.destination.glob('daily-*.tar.gz'))), 2)
        self.assertEqual(len(list(self.destination.glob('daily-*.verified.json'))), 2)
        self.assertEqual((first / 'marker').read_text(), 'immutable first point')

    def test_same_day_checks_do_not_make_another_archive(self):
        result = self.run_backup()
        with patch.object(backup.recovery, 'create_recovery_point') as create:
            again = self.run_backup()
        create.assert_not_called()
        self.assertEqual(result['last_backup'], again['last_backup'])
        self.assertEqual(len(list(self.destination.glob('daily-*.tar.gz'))), 1)

    def test_produced_backup_receipt_is_accepted_by_the_read_only_ui(self):
        result = self.run_backup()
        self.assertEqual(result['state'], 'verified')
        view = operations_status.backup_status(self.report, platform='darwin',
                                              expected_owner=backup.os.geteuid())
        self.assertTrue(view['available'])
        self.assertEqual(view['last_backup'], result['last_backup'])

    def test_symlink_backup_root_and_status_parent_are_refused(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(PermissionError):
            backup.run(self.root, alias, self.report)
        self.report.parent.mkdir()
        alias_report = self.base / 'alias-report'
        alias_report.symlink_to(self.report.parent, target_is_directory=True)
        with self.assertRaises(PermissionError):
            backup.run(self.root, self.destination, alias_report / 'backup-status.json')

    def test_worker_probe_requires_quiescent_main_and_control_pids(self):
        runner = Mock(return_value=Mock(returncode=0, stdout='ActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n'))
        self.assertTrue(backup.worker_idle(runner=runner))
        runner.return_value.stdout = 'ActiveState=inactive\nMainPID=42\nControlPID=0\n'
        self.assertFalse(backup.worker_idle(runner=runner))
        runner.return_value.returncode = 1
        self.assertFalse(backup.worker_idle(runner=runner))

    def test_capacity_includes_wal_qa_and_reserve(self):
        wal = Path(str(self.database) + '-wal')
        wal.write_bytes(b'X' * 32768)
        plan = backup.capacity_plan(self.root, self.destination, reserve=12345)
        selected = backup.recovery.source_files(self.root)
        source = sum((self.root / path).stat().st_size for path in selected) + 32768
        self.assertEqual(plan['source_bytes'], source)
        self.assertGreaterEqual(plan['required_free_bytes'], plan['largest_file_bytes'] + 12345)
        expected_overhead = max(1024 * 1024, source // 1000 + len(selected) * 4096)
        self.assertEqual(plan['required_free_bytes'], plan['largest_file_bytes'] + expected_overhead + 12345)

    def test_failed_attempt_preserves_last_verified_and_private_error_is_not_printed(self):
        first = self.run_backup()
        with patch.object(backup, '_stream_snapshot', side_effect=ValueError('PRIVATE API KEY')):
            result = self.run_backup(force=True)
        self.assertEqual(result['state'], 'error')
        self.assertEqual(result['last_backup'], first['last_backup'])
        self.assertNotIn('PRIVATE', self.report.read_text())
        self.assertEqual(len(list(self.destination.glob('daily-*.tar.gz'))), 1)

    def test_reserve_exhausted_during_compression_preserves_previous_archive(self):
        first = self.run_backup()
        original = backup._ReservedWriter.write

        def exhausted(writer, block):
            raise backup.CapacityError('insufficient_space')

        with patch.object(backup._ReservedWriter, 'write', exhausted):
            value = self.run_backup(force=True)
        self.assertEqual(value['state'], 'deferred')
        self.assertEqual(value['last_attempt']['reason_code'], 'insufficient_space')
        self.assertEqual(value['last_backup'], first['last_backup'])
        self.assertEqual(len(list(self.destination.glob('daily-*.tar.gz'))), 1)
        self.assertFalse(list(self.destination.glob('.daily-work-*')))

    def test_service_permits_sqlite_reader_sidecars_but_hides_credentials(self):
        unit = Path('deployment/linux/patternlab-backup.service').read_text()
        self.assertIn('ProtectSystem=strict', unit)
        self.assertIn('InaccessiblePaths=/opt/patternlab/.streamlit/secrets.toml /etc/patternlab', unit)
        self.assertIn('PrivateNetwork=yes', unit)
        self.assertIn('ReadWritePaths=/var/lib/patternlab-backups /var/lib/patternlab-ops /opt/patternlab/data', unit)

    def test_sqlite_restore_verification_cannot_consume_the_disk_reserve(self):
        value = self.run_backup()
        archive = self.destination / value['last_backup']['point_name']
        with tarfile.open(archive) as stream:
            manifest = json.load(stream.extractfile('manifest.json'))
        scratch = self.base / 'limited-restore'
        scratch.mkdir()
        with patch.object(backup.shutil, 'disk_usage', return_value=Mock(free=0)):
            with self.assertRaises(backup.CapacityError):
                backup.verify_archive(archive, manifest, scratch, reserve=1024)
        self.assertFalse(list(scratch.iterdir()))
        self.assertTrue(archive.is_file())


if __name__ == '__main__':
    unittest.main()
