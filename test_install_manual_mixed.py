"""Exercise the scoped installer without services or deployment access."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'manual_mixed_install_test',
    Path(__file__).parent / 'deployment/linux/install_manual_mixed.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class ManualMixedInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'app'
        self.root.mkdir()
        self.backups = Path(self.temp.name) / 'backups'
        for name, value in (('ROOT', self.root), ('BACKUP_ROOT', self.backups),
                            ('FILES', install.FILES)):
            self.patch(install.base, name, value)
        self.old = {name: ('# previous ' + name + '\n').encode()
                    for name in install.FILES if name not in install.NEW_FILES}
        self.payload = {name: ('# reviewed ' + name + '\n').encode()
                        for name in install.FILES}
        for name, data in self.old.items():
            path = self.root / name
            path.write_bytes(data)
            path.chmod(0o640)
        self.sentinel = self.root / 'untouched_worker.py'
        self.sentinel.write_bytes(b'# independent live worker\n')
        for name in (install.base.ENABLED, install.base.ARTIFACT):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'unchanged policy\n')
        self.database = self.root / 'data/api_football/predictions.sqlite3'
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE predictions (id TEXT, created_at TEXT, digest TEXT, payload TEXT)')
            db.execute('INSERT INTO predictions VALUES (?, ?, ?, ?)',
                       ('frozen', 'before', 'digest', '{"forecast":"original"}'))
        self.patch(install.base, 'ARCHIVES', {'predictions.sqlite3': 'predictions'})

    def patch(self, target, name, *args, **kwargs):
        item = patch.object(target, name, *args, **kwargs)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def bundle(self, *, change_manifest=None, members=None, payload=None):
        payload = self.payload if payload is None else payload
        manifest = dict(
            sha256={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
            previous_sha256={name: install.base.digest(self.root / name)
                             if (self.root / name).exists() else None for name in payload})
        if change_manifest:
            change_manifest(manifest)
        entries = list(payload.items()) + [('manifest.json', json.dumps(manifest).encode())]
        if members:
            entries = members(entries)
        path = Path(self.temp.name) / 'bundle.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in entries:
                item = tarfile.TarInfo(name)
                if data is None:
                    item.type = tarfile.SYMTYPE
                    item.linkname = 'manual_analysis_panel.py'
                    archive.addfile(item)
                else:
                    item.size = len(data)
                    archive.addfile(item, io.BytesIO(data))
        return path, install.base.digest(path)

    def sandbox_deploy(self, *, healthy=True, proxy=True):
        self.patch(install.os, 'geteuid', return_value=0)
        self.patch(install.base, 'frozen', return_value={'candidate': 'unchanged'})
        self.patch(install.support, 'protected_sources', side_effect=lambda names: {
            str(self.sentinel): install.base.digest(self.sentinel)})
        self.patch(install.base, 'application_owner', return_value=(123, 456))
        def write(path, data, **metadata):
            path.write_bytes(data)
            path.chmod(metadata.get('mode', 0o644))
        self.writes = self.patch(install.base, 'atomic_write', side_effect=write)
        self.commands = self.patch(install.base, 'command')
        self.health = self.patch(install.base, 'healthy', side_effect=healthy
                                 if callable(healthy) else lambda: healthy)
        self.patch(install, 'proxy_active', return_value=proxy)
        self.patch(install.shutil, 'disk_usage', return_value=Mock(free=10**9))

    def append_live_record(self):
        with sqlite3.connect(self.database) as db:
            db.execute('INSERT INTO predictions VALUES (?, ?, ?, ?)',
                       ('worker-append', 'during-deployment', 'new', '{"forecast":"new"}'))

    def assert_source_rollback(self):
        for name, data in self.old.items():
            self.assertEqual((self.root / name).read_bytes(), data)
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o640)
        for name in install.NEW_FILES:
            self.assertFalse((self.root / name).exists())

    def test_exact_bundle_allows_only_expected_missing_new_files(self):
        path, digest = self.bundle()
        manifest, payload = install.load_bundle(path, digest)
        self.assertEqual(payload, self.payload)
        self.assertEqual({name for name, value in manifest['previous_sha256'].items()
                          if value is None}, install.NEW_FILES)
        self.assert_source_rollback()  # Reading a verified tar never extracts it.
        (self.root / 'manual_analysis_panel.py').unlink()
        path, digest = self.bundle()
        with self.assertRaisesRegex(RuntimeError, 'Only new mixed-manual files'):
            install.load_bundle(path, digest)

    def test_manifest_scope_hashes_and_payload_syntax_are_strict(self):
        mutations = (
            lambda m: m['sha256'].pop('table_views.py'),
            lambda m: m['previous_sha256'].update({'unreviewed.py': '0' * 64}),
            lambda m: m['sha256'].update({'table_views.py': 'not-a-hash'}),
            lambda m: m['sha256'].update({'table_views.py': '0' * 64}),
            lambda m: m['previous_sha256'].update({'table_views.py': 12}),
        )
        for change in mutations:
            with self.subTest(change=change):
                path, digest = self.bundle(change_manifest=change)
                with self.assertRaises(RuntimeError):
                    install.load_bundle(path, digest)
        path, digest = self.bundle()
        with self.assertRaisesRegex(RuntimeError, 'Bundle hash mismatch'):
            install.load_bundle(path, '0' * 64)
        path, digest = self.bundle(payload={**self.payload, 'table_views.py': b'if :\n'})
        with self.assertRaises(SyntaxError):
            install.load_bundle(path, digest)

    def test_archive_rejects_duplicates_extra_paths_and_nonregular_members(self):
        variants = (
            lambda rows: rows + [('manual_analysis_panel.py', b'# duplicate\n')],
            lambda rows: rows + [('../escape.py', b'# outside\n')],
            lambda rows: rows + [('data/api_football/predictions.sqlite3', b'no')],
            lambda rows: [(name, None if name == 'table_views.py' else data)
                          for name, data in rows],
        )
        for members in variants:
            with self.subTest(members=members):
                path, digest = self.bundle(members=members)
                with self.assertRaisesRegex(RuntimeError, 'bundle scope'):
                    install.load_bundle(path, digest)
        self.assertFalse((Path(self.temp.name) / 'escape.py').exists())

    def test_drift_and_symlink_targets_are_rejected_before_writes(self):
        path, digest = self.bundle()
        target = self.root / 'table_views.py'
        target.write_bytes(b'# concurrent source release\n')
        with self.assertRaisesRegex(RuntimeError, 'reviewed baseline'):
            install.load_bundle(path, digest)
        target.unlink()
        target.symlink_to(self.sentinel)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            install.load_bundle(path, digest)
        target.unlink()
        target.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'non-file'):
            install.load_bundle(path, digest)

    def test_success_preserves_archives_policy_metadata_and_service_scope(self):
        path, digest = self.bundle()
        policies = install.policy_assets()
        def healthy():
            self.append_live_record()
            return True
        self.sandbox_deploy(healthy=healthy)
        receipt = install.deploy(path, digest)
        for name, data in self.payload.items():
            self.assertEqual((self.root / name).read_bytes(), data)
            self.assertEqual((self.root / name).stat().st_mode & 0o777,
                             0o644 if name in install.NEW_FILES else 0o640)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM predictions').fetchone()[0], 2)
        self.assertEqual(install.policy_assets(), policies)
        self.assertEqual(receipt['records_preserved'], {'predictions.sqlite3': 1})
        self.assertEqual(receipt['rollback_scope'], 'source_only')
        self.assertEqual(receipt['database_writes'], 0)
        self.assertFalse(receipt['worker_restarted'])
        self.assertTrue(receipt['timers_unchanged'])
        self.assertEqual({call.args for call in self.commands.call_args_list}, {
            ('/usr/bin/systemctl', 'stop', 'patternlab-web.service'),
            ('/usr/bin/systemctl', 'start', 'patternlab-web.service'),
            ('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')})
        backup = Path(receipt['backup_directory'])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((backup / 'receipt.json').read_text()), receipt)
        self.assertEqual({call.args[0].name for call in self.writes.call_args_list}, set(install.FILES))

    def test_failed_health_restores_sources_only_keeps_live_worker_appends(self):
        path, digest = self.bundle()
        policies = install.policy_assets()
        def unhealthy():
            self.append_live_record()
            return False
        self.sandbox_deploy(healthy=unhealthy)
        with self.assertRaisesRegex(RuntimeError, 'Web health failed'):
            install.deploy(path, digest)
        self.assert_source_rollback()
        with sqlite3.connect(self.database) as db:
            self.assertEqual([row[0] for row in db.execute('SELECT id FROM predictions ORDER BY id')],
                             ['frozen', 'worker-append'])
        self.assertEqual(install.policy_assets(), policies)
        receipt = json.loads(next(self.backups.glob('*/receipt.json')).read_text())
        self.assertEqual(receipt['state'], 'manual_mixed_rolled_back')
        self.assertEqual(receipt['rollback_scope'], 'source_only')
        self.assertEqual(receipt['database_writes'], 0)
        self.assertEqual(sum(call.args == ('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')
                             for call in self.commands.call_args_list), 2)

    def test_preservation_drift_triggers_rollback_without_overwriting_external_change(self):
        path, digest = self.bundle()
        policy = self.root / install.base.ARTIFACT
        def changed_policy():
            policy.write_bytes(b'policy changed externally\n')
            return True
        self.sandbox_deploy(healthy=changed_policy, proxy=False)
        with self.assertRaisesRegex(RuntimeError, 'policy or calibration changed'):
            install.deploy(path, digest)
        self.assert_source_rollback()
        self.assertEqual(policy.read_bytes(), b'policy changed externally\n')
        self.assertTrue(all('worker' not in ' '.join(call.args)
                            and 'timer' not in ' '.join(call.args)
                            and 'proxy' not in ' '.join(call.args)
                            for call in self.commands.call_args_list))

    def test_permission_and_disk_preflight_fail_without_service_or_source_changes(self):
        path, digest = self.bundle()
        self.sandbox_deploy()
        uid = self.patch(install.os, 'geteuid', return_value=1000)
        with self.assertRaisesRegex(RuntimeError, 'Root privileges'):
            install.deploy(path, digest)
        uid.return_value = 0
        self.patch(install.shutil, 'disk_usage', return_value=Mock(free=0))
        with self.assertRaisesRegex(RuntimeError, 'Insufficient space'):
            install.deploy(path, digest)
        self.assert_source_rollback()
        self.commands.assert_not_called()
        self.writes.assert_not_called()
        self.assertFalse(self.backups.exists())


if __name__ == '__main__':
    unittest.main()
