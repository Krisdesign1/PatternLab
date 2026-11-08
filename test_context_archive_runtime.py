"""Offline runtime/installer checks using temporary SQLite and mocked services.

No application source, production database, service, provider or credential is
changed. Installer writes are redirected to this test's temporary tree.
"""
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import context_observation_store as store
from deployment.linux import install_context_archive as installer
from deployment.linux import run_context_archive as runtime
import local_recovery as recovery
from scripts import sync_context_archive as cli


ROOT = Path(__file__).resolve().parent
ENOUGH_DISK = SimpleNamespace(free=2 * 1024**3)


def raw_fixture():
    return dict(fixture=dict(id=99, date='2026-11-07T18:00:00Z', status=dict(short='NS')),
        league=dict(id=39), teams=dict(home=dict(id=1), away=dict(id=2)),
        injuries=[dict(fixture=dict(id=99), team=dict(id=1),
            player=dict(id=101, type='Missing Fixture', reason='Knee Injury'))])


def source_database(path):
    raw = raw_fixture()
    record = dict(version='fixture-import-quality-v1', match_id='99',
        observed_at_utc='2026-11-07T17:00:00Z', time_kind='local_import_recorded',
        previous_observation=None, received_raw=raw, received_raw_sha256=store.digest(raw),
        selected_raw_sha256=store.digest(raw), selected_quality=None, decision='received', reason=None)
    record['id'] = store.digest(record)
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        connection.executemany('INSERT INTO metadata VALUES(?,?)', [('store_uuid', 'a'*32), ('revision', '1')])
        connection.execute('''CREATE TABLE fixture_import_revisions(sequence INTEGER PRIMARY KEY,
            match_id TEXT,observed_at_utc TEXT,payload TEXT,digest TEXT)''')
        connection.execute('INSERT INTO fixture_import_revisions VALUES(1,?,?,?,?)',
            ('99', record['observed_at_utc'], store.canonical(record), record['id']))


class ManifestGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in runtime.SOURCES:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# isolated synthetic runtime file\n');path.chmod(0o600)
        self.manifest = dict(version=runtime.VERSION, tests_passed=True,
            source_sha256={name: runtime.digest(self.root / name) for name in runtime.SOURCES})
        self.write_manifest()

    def write_manifest(self):
        (self.root/runtime.MANIFEST).write_text(json.dumps(self.manifest))

    def test_valid_manifest_and_exact_hash_version_test_and_source_set_gates(self):
        self.assertEqual(runtime.verify(self.root, require_root_owned=False), self.manifest)
        original = json.loads(json.dumps(self.manifest))
        for key, value in [('version', 'other'), ('tests_passed', False), ('tests_passed', 1),
                           ('source_sha256', {})]:
            self.manifest = dict(original, **{key: value});self.write_manifest()
            with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=False)
        self.manifest = original;self.write_manifest()
        (self.root/runtime.SOURCES[0]).write_text('changed')
        with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=False)

    def test_symlink_nonregular_and_unsafe_ownership_or_permissions_are_rejected(self):
        source = self.root/runtime.SOURCES[0]
        original = source.read_bytes();source.unlink()
        target = self.root/'outside.py';target.write_bytes(original);source.symlink_to(target)
        with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=False)
        source.unlink();source.mkdir()
        with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=False)
        source.rmdir();source.write_bytes(original)
        source.chmod(0o666)
        with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=True)
        source.chmod(0o600)
        manifest = self.root/runtime.MANIFEST
        manifest.unlink();manifest.symlink_to(target)
        with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=False)

    def test_failed_runtime_verification_never_calls_sync_and_sanitizes_errors(self):
        with patch.object(runtime, 'verify', side_effect=ValueError('private payload must not leak')), \
                patch.object(cli, 'run') as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runtime.main(), 1)
        run.assert_not_called()
        response = json.loads(output.getvalue())
        self.assertEqual(response['error_type'], 'ValueError')
        self.assertNotIn('private payload', output.getvalue())

    def test_symlinked_parent_cannot_redirect_an_otherwise_qualified_source(self):
        parent = self.root/'scripts'
        target = self.root/'redirected-scripts'
        parent.rename(target);parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):runtime.verify(self.root, require_root_owned=False)


class CliRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.destination = self.root/'fixtures.sqlite3', self.root/'context.sqlite3'
        source_database(self.source)

    def test_real_cli_sync_status_and_idempotent_replay_leave_source_unchanged(self):
        before = self.source.read_bytes()
        with patch.object(cli.shutil, 'disk_usage', return_value=ENOUGH_DISK):
            first = cli.run(self.source, self.destination, batch_size=1)
            again = cli.run(self.source, self.destination, batch_size=1)
        self.assertTrue(first['complete'])
        self.assertEqual(first['processed_records'], 1)
        self.assertEqual(again['processed_records'], 0)
        status = json.loads(self.destination.with_suffix('.status.json').read_text())
        self.assertEqual(status['version'], cli.STATUS_VERSION)
        self.assertEqual(status['phase'], 'caught_up_to_starting_watermark')
        self.assertEqual(status['api_calls'], 0)
        self.assertFalse(status['model_training'])
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(self.destination.with_suffix('.status.json').stat().st_mode & 0o777, 0o600)

    def test_error_status_is_sanitized_and_can_be_resumed(self):
        with patch.object(cli.shutil, 'disk_usage', return_value=ENOUGH_DISK):
            with patch.object(cli, 'sync_archive', side_effect=RuntimeError('private provider raw')):
                with self.assertRaises(RuntimeError):cli.run(self.source, self.destination)
            path = self.destination.with_suffix('.status.json')
            status = json.loads(path.read_text())
            self.assertEqual(status['phase'], 'error')
            self.assertTrue(status['resumable'])
            self.assertNotIn('private provider raw', path.read_text())
            self.assertTrue(cli.run(self.source, self.destination)['complete'])

    def test_cli_main_reports_refusal_as_nonzero_without_raw_exception_text(self):
        arguments = ['sync_context_archive', '--source', str(self.source), '--destination', str(self.destination)]
        with patch.object(cli.sys, 'argv', arguments), \
                patch.object(cli, 'run', side_effect=ValueError('private error')), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(), 1)
        self.assertEqual(json.loads(output.getvalue())['state'], 'error')
        self.assertNotIn('private error', output.getvalue())

    def test_destination_symlink_hardlink_and_source_identity_refused_before_sync(self):
        for mode in ('same', 'symlink', 'hardlink'):
            destination = self.source if mode == 'same' else self.root / (mode + '.sqlite3')
            if mode == 'symlink':destination.symlink_to(self.source)
            if mode == 'hardlink':os.link(self.source, destination)
            before = self.source.read_bytes()
            with patch.object(cli, 'sync_archive') as sync:
                with self.assertRaises(ValueError):cli.run(self.source, destination)
            sync.assert_not_called()
            self.assertEqual(before, self.source.read_bytes())

    def test_status_symlink_unrelated_file_and_low_disk_refused_before_sync(self):
        status = self.destination.with_suffix('.status.json')
        unrelated = self.root/'unrelated.json';unrelated.write_text('{"private":"unchanged"}')
        status.symlink_to(unrelated)
        with patch.object(cli.shutil, 'disk_usage', return_value=ENOUGH_DISK), patch.object(cli, 'sync_archive') as sync:
            with self.assertRaises(ValueError):cli.run(self.source, self.destination)
            sync.assert_not_called()
        self.assertEqual(unrelated.read_text(), '{"private":"unchanged"}')
        status.unlink();status.write_text('{"version":"different"}')
        with patch.object(cli.shutil, 'disk_usage', return_value=ENOUGH_DISK), patch.object(cli, 'sync_archive') as sync:
            with self.assertRaises(ValueError):cli.run(self.source, self.destination)
            sync.assert_not_called()
        status.unlink()
        with patch.object(cli.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024)), \
                patch.object(cli, 'sync_archive') as sync:
            with self.assertRaises(OSError):cli.run(self.source, self.destination)
            sync.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_initial_low_disk_does_not_leave_a_stale_success_status(self):
        with patch.object(cli.shutil, 'disk_usage', return_value=ENOUGH_DISK):
            cli.run(self.source, self.destination)
        with patch.object(cli.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024)), \
                patch.object(cli, 'sync_archive') as sync:
            with self.assertRaises(OSError):cli.run(self.source, self.destination)
            sync.assert_not_called()
        status=json.loads(self.destination.with_suffix('.status.json').read_text())
        self.assertIn(status['phase'], ('error', 'paused'))
        self.assertTrue(status['resumable'])


class InstallerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.application = self.root/'application';self.application.mkdir()
        self.units = self.root/'systemd';self.units.mkdir()
        self.receipts = self.root/'receipts';self.receipts.mkdir()

    def bundle(self, *, extra=False, qualified=True, wrong_payload=False):
        payload = {name:(ROOT/name).read_bytes() for name in installer.FILES}
        manifest = dict(version='context-archive-execution-v1', tests_passed=qualified,
            sha256={name:sha256(value).hexdigest() for name,value in payload.items()},
            qualification=dict(tests='isolated unit tests'))
        if wrong_payload:payload[installer.PYTHON_FILES[0]] += b'\n# tampered\n'
        payload['manifest.json'] = json.dumps(manifest).encode()
        if extra:payload['unexpected.txt'] = b'unexpected'
        path=self.root/'bundle.tar'
        with tarfile.open(path, 'w') as archive:
            for name,data in payload.items():
                member=tarfile.TarInfo(name);member.size=len(data)
                archive.addfile(member,io.BytesIO(data))
        return path, installer.digest(path)

    def mapped_path(self, *args):
        if args and args[0] == '/etc/systemd/system':return self.units
        if args and args[0] == '/var/lib/patternlab-cutover':return self.receipts
        return Path(*args)

    def test_installer_root_digest_payload_and_qualification_guards_precede_services(self):
        bundle, expected=self.bundle()
        with patch.object(installer.os,'geteuid',return_value=1000), \
                patch.object(installer,'command') as command:
            with self.assertRaises(PermissionError):installer.main()
            command.assert_not_called()
        for options in ({'extra':True},{'qualified':False},{'wrong_payload':True}):
            bundle,expected=self.bundle(**options)
            with patch.object(installer.os,'geteuid',return_value=0), \
                    patch.object(installer,'ROOT',self.application), \
                    patch.object(installer.sys,'argv',['install',str(bundle),expected]), \
                    patch.object(installer,'preserved') as preserved, \
                    patch.object(installer,'command') as command:
                with self.assertRaises(ValueError):installer.main()
                preserved.assert_not_called();command.assert_not_called()
        bundle,expected=self.bundle()
        with patch.object(installer.os,'geteuid',return_value=0), \
                patch.object(installer.sys,'argv',['install',str(bundle),'0'*64]), \
                patch.object(installer,'command') as command:
            with self.assertRaisesRegex(ValueError,'digest'):installer.main()
            command.assert_not_called()

    def test_additive_installer_refuses_existing_application_file(self):
        bundle,expected=self.bundle()
        existing=self.application/installer.FILES[0];existing.write_text('preserved')
        with patch.object(installer.os,'geteuid',return_value=0), patch.object(installer,'ROOT',self.application), \
                patch.object(installer.sys,'argv',['install',str(bundle),expected]), \
                patch.object(installer,'preserved') as preserved, patch.object(installer,'command') as command:
            with self.assertRaises(ValueError):installer.main()
            preserved.assert_not_called();command.assert_not_called()
        self.assertEqual(existing.read_text(),'preserved')

    def test_qualified_install_is_redirected_to_temp_and_never_enables_timer_or_restarts_main(self):
        bundle,expected=self.bundle()
        with patch.object(installer.os,'geteuid',return_value=0), patch.object(installer,'ROOT',self.application), \
                patch.object(installer,'Path',side_effect=self.mapped_path), \
                patch.object(installer.sys,'argv',['install',str(bundle),expected]), \
                patch.object(installer,'_safe_parent') as safe_parent, \
                patch.object(installer,'preserved',return_value={'protected':'unchanged'}), \
                patch.object(installer,'command',return_value='stable') as command, \
                patch.object(installer.subprocess,'run') as subprocess_run, redirect_stdout(io.StringIO()) as output:
            installer.main()
        subprocess_run.assert_not_called()
        self.assertGreaterEqual(safe_parent.call_count,len(installer.FILES)+len(installer.UNITS)+1)
        calls=[call.args for call in command.call_args_list]
        self.assertIn(('/usr/bin/systemctl','start','--no-block','patternlab-context-archive.service'),calls)
        self.assertFalse(any('enable' in call or 'restart' in call for call in calls))
        self.assertTrue(all((self.application/name).is_file() for name in installer.PYTHON_FILES))
        self.assertTrue(all((self.units/name).is_file() for name in installer.UNITS))
        response=json.loads(output.getvalue())
        self.assertFalse(response['timer_enabled'])
        self.assertFalse(response['main_services_restarted'])
        self.assertTrue(all(response['main_services_same_start'].values()))
        manifest=json.loads((self.application/installer.MANIFEST).read_text())
        self.assertEqual(set(manifest['source_sha256']),set(runtime.SOURCES))

    def test_installer_rejects_symlinked_parent_and_outside_root_without_writes(self):
        target=self.root/'redirected';target.mkdir()
        parent=self.application/'scripts';parent.symlink_to(target,target_is_directory=True)
        before=set(target.iterdir())
        with self.assertRaisesRegex(ValueError,'directory is unsafe'):
            installer._safe_parent(self.application,parent/'sync_context_archive.py')
        with self.assertRaisesRegex(ValueError,'outside'):
            installer._safe_parent(self.application,self.root/'outside.py')
        self.assertEqual(set(target.iterdir()),before)


class BackupAndUnitContractTests(unittest.TestCase):
    def test_context_archive_is_selected_and_online_sqlite_backup_restores_committed_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data/api_football';data.mkdir(parents=True)
            archive=data/'context_observations.sqlite3';store.initialize(archive)
            connection=store.connect(archive)
            try:
                with connection:
                    store.append_observation(connection,source_uuid='a'*32,sequence=1,
                        record_id='receipt',observed_at='2026-11-07T17:00:00Z',raw=raw_fixture())
                (data/'context_observations.sqlite3.sync.lock').write_text('')
                (data/'context_observations.status.json').write_text('{"version":"context-archive-status-v1"}')
                selected=recovery.source_files(root)
                self.assertIn(Path('data/api_football/context_observations.sqlite3'),selected)
                self.assertNotIn(Path('data/api_football/context_observations.sqlite3.sync.lock'),selected)
                self.assertFalse(any(str(p).endswith(('-wal','-shm')) for p in selected))
                restored=root/'restored.sqlite3';recovery._backup_database(archive,restored)
                self.assertEqual(recovery._database_summary(archive),recovery._database_summary(restored))
                self.assertEqual(recovery._database_summary(restored)['tables']['context_receipts'],1)
            finally:connection.close()

    def test_service_is_offline_bounded_and_independent_of_serving_units(self):
        service=(ROOT/'deployment/linux/patternlab-context-archive.service').read_text()
        timer=(ROOT/'deployment/linux/patternlab-context-archive.timer').read_text()
        for line in ('PrivateNetwork=yes','NoNewPrivileges=yes','ProtectSystem=strict',
                     'CPUQuota=50%','MemoryMax=512M','TimeoutStartSec=30min',
                     'RestrictAddressFamilies=AF_UNIX','Restart=no'):
            self.assertIn(line,service)
        self.assertNotIn('patternlab-worker.service',service)
        self.assertNotIn('patternlab-web.service',service)
        self.assertIn('OnUnitInactiveSec=5min',timer)
        self.assertIn('Unit=patternlab-context-archive.service',timer)


if __name__ == '__main__':
    unittest.main()
