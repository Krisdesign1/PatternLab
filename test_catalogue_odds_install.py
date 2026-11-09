"""Catalogue odds rollout replaces one reviewed source, never live archives."""
from contextlib import ExitStack
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


spec = importlib.util.spec_from_file_location('catalogue_odds_install_test',
    Path(__file__).parent / 'deployment/linux/install_catalogue_odds.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class CatalogueOddsInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'app'
        self.root.mkdir()
        self.backups = Path(self.temporary.name) / 'backups'
        for key, value in (('ROOT', self.root), ('BACKUP_ROOT', self.backups)):
            task = patch.object(install.base, key, value)
            task.start()
            self.addCleanup(task.stop)
        self.payload = {'scenario_catalog_panel.py': b'# reviewed source\n'}
        (self.root / 'scenario_catalog_panel.py').write_bytes(b'# old source\n')
        self.archive = self.root / 'data/api_football/predictions.sqlite3'
        self.archive.parent.mkdir(parents=True)
        with sqlite3.connect(self.archive) as db:
            db.execute('CREATE TABLE predictions (id TEXT, created_at TEXT, digest TEXT, payload TEXT)')
            db.execute('INSERT INTO predictions VALUES (?, ?, ?, ?)',
                       ('original', '2026-11-09T08:00:00Z', 'unchanged', '{"scenario":["1st","2nd"]}'))
        self.archive_digest = install.base.digest(self.archive)

    def bundle(self, payload=None, manifest_edit=None):
        payload = self.payload if payload is None else payload
        manifest = dict(
            sha256={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
            previous_sha256={name: install.base.digest(self.root / name)
                             if (self.root / name).exists() else None for name in payload})
        if manifest_edit:
            manifest_edit(manifest)
        path = Path(self.temporary.name) / 'catalogue-odds.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in {**payload, 'manifest.json': json.dumps(manifest).encode()}.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path, install.base.digest(path)

    def test_exact_reviewed_source_and_baseline_are_required(self):
        path, digest = self.bundle()
        manifest, payload = install.load_bundle(path, digest)
        self.assertEqual(install.SOURCES, ('scenario_catalog_panel.py',))
        self.assertEqual(payload, self.payload)
        self.assertEqual(manifest['previous_sha256']['scenario_catalog_panel.py'],
                         install.base.digest(self.root / 'scenario_catalog_panel.py'))
        (self.root / 'scenario_catalog_panel.py').write_bytes(b'# independent modification\n')
        with self.assertRaisesRegex(RuntimeError, 'reviewed baseline'):
            install.load_bundle(path, digest)

    def test_data_secrets_other_sources_and_paths_cannot_enter_payload(self):
        for name in ('../escape.py', 'data/predictions.sqlite3', '.streamlit/secrets.toml',
                     'scenario_catalog.py', 'app.py'):
            with self.subTest(name=name):
                path, digest = self.bundle({**self.payload, name: b'# forbidden\n'})
                with self.assertRaisesRegex(RuntimeError, 'source scope'):
                    install.load_bundle(path, digest)
        path, digest = self.bundle({})
        with self.assertRaisesRegex(RuntimeError, 'source scope'):
            install.load_bundle(path, digest)

    def test_bundle_payload_and_manifest_hashes_are_checked(self):
        path, digest = self.bundle()
        with self.assertRaisesRegex(RuntimeError, 'Bundle hash mismatch'):
            install.load_bundle(path, '0' * 64)
        path, digest = self.bundle(manifest_edit=lambda item: item['previous_sha256'].clear())
        with self.assertRaisesRegex(RuntimeError, 'Incomplete reviewed'):
            install.load_bundle(path, digest)
        path, digest = self.bundle(manifest_edit=lambda item:
            item['sha256'].update({'scenario_catalog_panel.py': '0' * 64}))
        with self.assertRaisesRegex(RuntimeError, 'Payload hash mismatch'):
            install.load_bundle(path, digest)

    def test_target_symlink_is_rejected(self):
        path, digest = self.bundle()
        target = self.root / 'scenario_catalog_panel.py'
        target.unlink()
        target.symlink_to(self.archive)
        with self.assertRaisesRegex(RuntimeError, 'target symlink'):
            install.load_bundle(path, digest)

    def test_invalid_python_is_rejected_before_installation(self):
        path, digest = self.bundle({'scenario_catalog_panel.py': b'def broken(\n'})
        with self.assertRaises(SyntaxError):
            install.load_bundle(path, digest)
        self.assertEqual((self.root / 'scenario_catalog_panel.py').read_bytes(), b'# old source\n')

    def rollout(self, healthy):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target, name, kwargs in (
            (install.views.os, 'geteuid', {'return_value': 0}),
            (install.base, 'frozen', {'return_value': {}}),
            (install.views, 'protected_sources', {'return_value': {}}),
            (install.base, 'application_owner', {'return_value': (0, 0)}),
            (install.base, 'atomic_write', {'side_effect': lambda path, data, **kw: path.write_bytes(data)}),
            (install.base, 'healthy', {'return_value': healthy}),
            (install.views.subprocess, 'run', {'return_value': Mock(returncode=0)}),
        ):
            stack.enter_context(patch.object(target, name, **kwargs))
        return stack.enter_context(patch.object(install.base, 'command'))

    def test_failed_health_restores_source_not_database_and_resumes_proxy(self):
        path, digest = self.bundle()
        commands = self.rollout(healthy=False)
        original_loader = install.views.load_bundle
        with self.assertRaisesRegex(RuntimeError, 'Web health failed'):
            install.deploy(path, digest)
        self.assertIs(install.views.load_bundle, original_loader)
        self.assertEqual((self.root / 'scenario_catalog_panel.py').read_bytes(), b'# old source\n')
        self.assertEqual(install.base.digest(self.archive), self.archive_digest)
        self.assertFalse(list(self.backups.rglob('*.sqlite3')))
        self.assertEqual(sum(item.args == ('/usr/bin/systemctl', 'start', 'patternlab-proxy.service')
                             for item in commands.call_args_list), 2)

    def test_success_receipt_preserves_archives_and_only_restarts_web_and_proxy(self):
        path, digest = self.bundle()
        commands = self.rollout(healthy=True)
        receipt = install.deploy(path, digest)
        self.assertEqual(receipt['state'], 'catalogue_odds_deployed')
        self.assertEqual(receipt['storage'], 'source_only_no_archive_rewrite')
        self.assertTrue(Path(receipt['backup_directory']).name.startswith('catalogue-odds-'))
        self.assertEqual(receipt['database_writes'], 0)
        self.assertEqual(receipt['records_preserved']['predictions.sqlite3'], 1)
        self.assertTrue(receipt['public_proxy_resumed'])
        self.assertTrue(receipt['models_unchanged'])
        self.assertTrue(receipt['timers_unchanged'])
        self.assertTrue(receipt['authentication_unchanged'])
        self.assertFalse(receipt['worker_restarted'])
        self.assertFalse(receipt['network_rules_changed'])
        self.assertEqual(install.base.digest(self.archive), self.archive_digest)
        self.assertFalse(list(self.backups.rglob('*.sqlite3')))
        self.assertEqual([item.args for item in commands.call_args_list], [
            ('/usr/bin/systemctl', 'stop', 'patternlab-web.service'),
            ('/usr/bin/systemctl', 'start', 'patternlab-web.service'),
            ('/usr/bin/systemctl', 'start', 'patternlab-proxy.service'),
        ])
        self.assertEqual((self.root / 'scenario_catalog_panel.py').read_bytes(),
                         self.payload['scenario_catalog_panel.py'])


if __name__ == '__main__':
    unittest.main()
