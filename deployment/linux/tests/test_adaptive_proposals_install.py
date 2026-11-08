"""Deployment checks use isolated files/SQLite, never AWS or system services."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('adaptive_installer', Path(__file__).parents[1]/'install_adaptive_proposals.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class AdaptiveDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)/'app'
        self.root.mkdir()
        self.patch_root = patch.object(installer, 'ROOT', self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.old = {}
        self.payload = {}
        for index, name in enumerate(installer.SOURCES):
            self.payload[name] = f'NEW_SOURCE = {index}\n'.encode()
            # Exercise rollback of both updated and newly introduced modules.
            if index >= 3:
                target = self.root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f'OLD_SOURCE = {index}\n'.encode())
                self.old[name] = target.read_bytes()
        self.payload[installer.ARTIFACT] = b'{"contexts":{}}'
        self.payload[installer.ENABLED] = installer.MARKER
        self.manifest = dict(sha256={name:sha(data) for name,data in self.payload.items()},
            previous_sha256={name:sha(self.old[name]) if name in self.old else None for name in installer.FILES})
        for filename, table in installer.ARCHIVES.items():
            target = self.root/'data/api_football'/filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(target) as con:
                con.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY, created_at TEXT, digest TEXT, payload TEXT)')
                con.execute(f'INSERT INTO {table} VALUES(?,?,?,?)', ('old', 'before', 'digest', '{}'))
        self.bundle = Path(self.directory.name)/'bundle.tar.gz'

    def bundle_bytes(self, *, payload=None, manifest=None, extra=None):
        payload = self.payload if payload is None else payload
        manifest = self.manifest if manifest is None else manifest
        with tarfile.open(self.bundle, 'w:gz') as stream:
            for name, data in {**payload, 'manifest.json':json.dumps(manifest).encode(), **(extra or {})}.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                stream.addfile(info, BytesIO(data))
        return installer.digest(self.bundle)

    def runtime(self, validation=None, healthy=True):
        self.calls = []
        def command(*args):
            self.calls.append(args)
            return 'start-time' if 'show' in args else ''
        stack = ExitStack()
        stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))
        stack.enter_context(patch.object(installer.os, 'chown'))
        stack.enter_context(patch.object(installer, 'application_owner', return_value=(os.getuid(),os.getgid())))
        stack.enter_context(patch.object(installer, 'command', side_effect=command))
        stack.enter_context(patch.object(installer, 'frozen', return_value={'protected':True}))
        stack.enter_context(patch.object(installer, 'protected_snapshot', return_value={'guarded':'old'}))
        stack.enter_context(patch.object(installer, 'resource_headroom'))
        stack.enter_context(patch.object(installer, 'healthy', side_effect=healthy if callable(healthy) else None,
                                       return_value=healthy if not callable(healthy) else True))
        stack.enter_context(patch.object(installer, 'validate_installed_artifact',
            side_effect=validation or (lambda:dict(sha256='calibration',qualified_contexts=1))))
        return stack

    def assert_rolled_back(self):
        for name in installer.FILES:
            target = self.root/name
            if name in self.old:
                self.assertEqual(target.read_bytes(),self.old[name],name)
            else:
                self.assertFalse(target.exists(),name)
        self.assertFalse(any(self.root.rglob('*.adaptive-new-*')))

    def test_exact_reviewed_bundle_only_and_complete_previous_hashes_required(self):
        expected = self.bundle_bytes()
        manifest, payload = installer.load_bundle(self.bundle,expected)
        self.assertEqual(manifest,self.manifest)
        self.assertEqual(payload,self.payload)
        with self.assertRaisesRegex(RuntimeError,'Bundle hash'):
            installer.load_bundle(self.bundle,'0'*64)
        expected = self.bundle_bytes(extra={'../outside.py':b'print("outside")'})
        with self.assertRaisesRegex(RuntimeError,'Unexpected bundle'):
            installer.load_bundle(self.bundle,expected)
        missing = deepcopy(self.manifest)
        missing['previous_sha256'].pop(installer.ARTIFACT)
        expected = self.bundle_bytes(manifest=missing)
        with self.assertRaisesRegex(RuntimeError,'previous-source'):
            installer.load_bundle(self.bundle,expected)
        self.assert_rolled_back()

    def test_unreviewed_live_source_and_invalid_python_fail_before_any_write(self):
        name = installer.SOURCES[3]
        (self.root/name).write_bytes(b'UNREVIEWED = True\n')
        expected = self.bundle_bytes()
        with self.assertRaisesRegex(RuntimeError,'Unreviewed existing'):
            installer.load_bundle(self.bundle,expected)
        (self.root/name).write_bytes(self.old[name])
        changed = deepcopy(self.payload)
        changed[installer.SOURCES[0]] = b'if syntax error!'
        manifest = deepcopy(self.manifest)
        manifest['sha256'][installer.SOURCES[0]] = sha(changed[installer.SOURCES[0]])
        expected = self.bundle_bytes(payload=changed,manifest=manifest)
        with self.assertRaises(SyntaxError):
            installer.load_bundle(self.bundle,expected)
        self.assert_rolled_back()

    def test_both_immutable_archives_are_backed_up_and_worker_appends_are_allowed(self):
        backup = Path(self.directory.name)/'backup'
        backup.mkdir()
        previous = installer.archive_snapshot(backup)
        self.assertEqual({key:len(value) for key,value in previous.items()},
                         {'predictions.sqlite3':1,'manual_analyses.sqlite3':1})
        for filename, table in installer.ARCHIVES.items():
            with sqlite3.connect(self.root/'data/api_football'/filename) as con:
                con.execute(f'INSERT INTO {table} VALUES(?,?,?,?)',('new','later','hash','{"new":1}'))
            self.assertEqual((backup/filename).stat().st_mode&0o777,0o600)
        installer.require_legacy_unchanged(previous)
        for filename, table in installer.ARCHIVES.items():
            for column in ('created_at','digest','payload'):
                target = self.root/'data/api_football'/filename
                with sqlite3.connect(target) as con:
                    original = con.execute(f'SELECT {column} FROM {table} WHERE id="old"').fetchone()[0]
                    con.execute(f'UPDATE {table} SET {column}=? WHERE id="old"',('changed',))
                with self.subTest(filename=filename,column=column), self.assertRaisesRegex(RuntimeError,'Original archived'):
                    installer.require_legacy_unchanged(previous)
                with sqlite3.connect(target) as con:
                    con.execute(f'UPDATE {table} SET {column}=? WHERE id="old"',(original,))

    def test_activation_follows_installed_validation_and_first_web_health(self):
        sequence = []
        def validate():
            self.assertFalse((self.root/installer.ENABLED).exists())
            self.assertEqual((self.root/installer.ARTIFACT).read_bytes(),self.payload[installer.ARTIFACT])
            self.assertTrue(all((self.root/name).read_bytes()==self.payload[name] for name in installer.SOURCES))
            sequence.append('validate')
            return dict(sha256='qualified-policy',qualified_contexts=2)
        def healthy():
            sequence.append('enabled-health' if (self.root/installer.ENABLED).exists() else 'inactive-health')
            return True
        expected = self.bundle_bytes()
        with self.runtime(validate,healthy):
            receipt = installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'cutover')
        self.assertEqual(sequence,['validate','inactive-health','enabled-health'])
        self.assertTrue(receipt['selection_enabled'])
        self.assertEqual(receipt['calibration']['qualified_contexts'],2)
        self.assertEqual(receipt['original_records_preserved'],{'predictions.sqlite3':1,'manual_analyses.sqlite3':1})
        self.assertTrue(all((self.root/name).read_bytes()==data for name,data in self.payload.items()))
        self.assertEqual([args for args in self.calls if 'restart' in args],
                         [('/usr/bin/systemctl','restart','patternlab-web.service')])
        self.assertFalse(any('patternlab-worker.service' in args and 'restart' in args for args in self.calls))

    def test_health_failure_restores_all_files_and_activation_without_overwriting_worker_appends(self):
        def unhealthy():
            for filename, table in installer.ARCHIVES.items():
                with sqlite3.connect(self.root/'data/api_football'/filename) as con:
                    con.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?,?,?)',('new','later','hash','{"new":1}'))
            return False
        expected = self.bundle_bytes()
        with self.runtime(healthy=unhealthy), self.assertRaisesRegex(RuntimeError,'Web health failed'):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'cutover')
        self.assert_rolled_back()
        for filename, table in installer.ARCHIVES.items():
            with sqlite3.connect(self.root/'data/api_football'/filename) as con:
                self.assertEqual(con.execute(f'SELECT id FROM {table} ORDER BY id').fetchall(),[('new',),('old',)])
        self.assertEqual([args[-1] for args in self.calls if 'restart' in args],
                         ['patternlab-web.service','patternlab-web.service'])

    def test_invalid_installed_calibration_restores_existing_activation_last(self):
        marker = self.root/installer.ENABLED
        marker.parent.mkdir(parents=True,exist_ok=True)
        marker.write_bytes(installer.MARKER)
        self.old[installer.ENABLED] = installer.MARKER
        self.manifest['previous_sha256'][installer.ENABLED] = sha(installer.MARKER)
        def invalid():
            self.assertFalse(marker.exists())
            raise ValueError('No qualified policy')
        expected = self.bundle_bytes()
        with self.runtime(invalid), self.assertRaisesRegex(ValueError,'No qualified policy'):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'cutover')
        self.assert_rolled_back()
        self.assertEqual([args for args in self.calls if 'restart' in args],
                         [('/usr/bin/systemctl','restart','patternlab-web.service')])

    def test_source_changed_during_rollout_is_detected_and_rolled_back(self):
        def changed():
            (self.root/installer.SOURCES[0]).write_bytes(b'UNREVIEWED = True\n')
            return dict(sha256='calibration',qualified_contexts=1)
        expected = self.bundle_bytes()
        with self.runtime(changed), self.assertRaisesRegex(RuntimeError,'Activated source hash mismatch'):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'cutover')
        self.assert_rolled_back()


if __name__ == '__main__':
    unittest.main()
