"""Scoped no-gate rollout tests against temporary SQLite and files only."""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('computed_installer', Path(__file__).parents[1]/'install_computed_proposals.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
base = installer.base


def sha(value):
    return hashlib.sha256(value).hexdigest()


class ComputedDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)/'app'
        self.root.mkdir()
        change_root = patch.object(base, 'ROOT', self.root)
        change_root.start()
        self.addCleanup(change_root.stop)
        self.old, self.payload = {}, {}
        for index, name in enumerate(installer.SOURCES):
            self.payload[name] = f'NEW_SOURCE = {index}\n'.encode()
            if index > 1:
                (self.root/name).write_bytes(f'OLD_SOURCE = {index}\n'.encode())
                self.old[name] = (self.root/name).read_bytes()
        marker = self.root/installer.ENABLED
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b'adaptive-scenario-proposals-v1\n')
        self.old[installer.ENABLED] = marker.read_bytes()
        self.payload[installer.ENABLED] = installer.MARKER
        self.manifest = dict(sha256={name:sha(data) for name,data in self.payload.items()},
            previous_sha256={name:sha(self.old[name]) if name in self.old else None for name in installer.FILES})
        self.calibration = self.root/base.ARTIFACT
        self.calibration.write_bytes(b'{"unchanged_historical_evidence": true}')
        for filename, table in base.ARCHIVES.items():
            with sqlite3.connect(self.root/'data/api_football'/filename) as connection:
                connection.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,created_at TEXT,digest TEXT,payload TEXT)')
                connection.execute(f'INSERT INTO {table} VALUES(?,?,?,?)',('old','before','hash','{}'))
        self.bundle = Path(self.directory.name)/'bundle.tar.gz'

    def bundle_hash(self, *, manifest=None, payload=None, extra=None):
        with tarfile.open(self.bundle,'w:gz') as archive:
            for name, content in {**(payload or self.payload),
                    'manifest.json':json.dumps(manifest or self.manifest).encode(), **(extra or {})}.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info,BytesIO(content))
        return base.digest(self.bundle)

    def runtime(self, *, validation=None, healthy=True):
        self.calls = []
        def command(*args):
            self.calls.append(args)
            return ''
        stack = ExitStack()
        stack.enter_context(patch.object(installer.os,'geteuid',return_value=0))
        stack.enter_context(patch.object(installer,'proxy_active',return_value=False))
        stack.enter_context(patch.object(base.os,'chown'))
        stack.enter_context(patch.object(base,'application_owner',return_value=(os.getuid(),os.getgid())))
        stack.enter_context(patch.object(base,'command',side_effect=command))
        self.frozen_check = stack.enter_context(patch.object(base,'frozen',return_value={'models':'preserved'}))
        stack.enter_context(patch.object(base,'protected_snapshot',return_value={'auth-club-worker-timers':'preserved'}))
        stack.enter_context(patch.object(base,'resource_headroom'))
        stack.enter_context(patch.object(base,'healthy',side_effect=healthy if callable(healthy) else None,
                                         return_value=healthy if not callable(healthy) else True))
        stack.enter_context(patch.object(installer,'validate_installed_policy',side_effect=validation or
            (lambda:dict(policy='all-computed-proposals-v1',qualification_blocks_proposals=False))))
        return stack

    def assert_rollback(self):
        for name in installer.FILES:
            if name in self.old:
                self.assertEqual((self.root/name).read_bytes(),self.old[name])
            else:
                self.assertFalse((self.root/name).exists())

    def test_bundle_exact_scope_hashes_and_current_sources(self):
        expected = self.bundle_hash()
        self.assertEqual(installer.load_bundle(self.bundle,expected),(self.manifest,self.payload))
        self.assertEqual(len(installer.SOURCES),7)
        self.assertNotIn(base.ARTIFACT,installer.FILES)
        for extra in ({'../outside.py':b'pass'}, {base.ARTIFACT:b'{}'}):
            with self.subTest(extra=extra), self.assertRaisesRegex(RuntimeError,'Unexpected bundle scope'):
                installer.load_bundle(self.bundle,self.bundle_hash(extra=extra))
        missing = deepcopy(self.manifest)
        missing['previous_sha256'].pop(installer.SOURCES[0])
        with self.assertRaisesRegex(RuntimeError,'Incomplete reviewed manifest'):
            installer.load_bundle(self.bundle,self.bundle_hash(manifest=missing))
        with self.assertRaisesRegex(RuntimeError,'Bundle hash mismatch'):
            installer.load_bundle(self.bundle,'0'*64)
        name = installer.SOURCES[2]
        (self.root/name).write_bytes(b'UNREVIEWED = True\n')
        with self.assertRaisesRegex(RuntimeError,'Unreviewed live source'):
            installer.load_bundle(self.bundle,self.bundle_hash())

    def test_success_has_no_calibration_requirement_and_stops_then_starts_only_web(self):
        self.calibration.unlink()
        expected = self.bundle_hash()
        with self.runtime():
            receipt = installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'backups')
            self.assertGreaterEqual(self.frozen_check.call_count,2)
        self.assertFalse(self.calibration.exists())
        self.assertTrue(receipt['historical_calibration_unchanged'])
        self.assertEqual(receipt['original_records_preserved'],{'predictions.sqlite3':1,'manual_analyses.sqlite3':1})
        self.assertEqual(self.calls,[('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service')])
        self.assertTrue(all((self.root/name).read_bytes()==content for name,content in self.payload.items()))
        for path in receipt['archive_backups'].values():
            self.assertEqual(Path(path).stat().st_mode&0o777,0o600)

    def test_isolated_installed_check_really_allows_sizes_two_through_twenty_without_calibration(self):
        def command(*args):
            return subprocess.run([sys.executable,'-B','-c',args[-1]],check=True,capture_output=True,text=True).stdout
        with patch.object(base,'command',side_effect=command):
            result = installer.validate_installed_policy()
        self.assertEqual(result['sizes_verified'],list(range(2,21)))
        self.assertFalse(result['qualification_blocks_proposals'])

    def test_running_https_proxy_resumes_after_web_start(self):
        expected = self.bundle_hash()
        with self.runtime(), patch.object(installer,'proxy_active',return_value=True):
            receipt = installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'backups')
        self.assertTrue(receipt['public_proxy_resumed'])
        self.assertEqual(self.calls,[('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-proxy.service')])

    def test_health_failure_rolls_back_files_only_and_keeps_worker_appends(self):
        def unhealthy():
            for filename, table in base.ARCHIVES.items():
                with sqlite3.connect(self.root/'data/api_football'/filename) as connection:
                    connection.execute(f'INSERT INTO {table} VALUES(?,?,?,?)',('worker-new','later','hash','{}'))
            return False
        expected = self.bundle_hash()
        with self.runtime(healthy=unhealthy), self.assertRaisesRegex(RuntimeError,'Web health failed'):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'backups')
        self.assert_rollback()
        for filename, table in base.ARCHIVES.items():
            with sqlite3.connect(self.root/'data/api_football'/filename) as connection:
                self.assertEqual(connection.execute(f'SELECT id FROM {table} ORDER BY id').fetchall(),
                                 [('old',),('worker-new',)])
        self.assertEqual(self.calls,[('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service'),
                                     ('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service')])

    def test_calibration_change_is_detected_and_source_files_are_rolled_back(self):
        def changed():
            self.calibration.write_bytes(b'{"changed":true}')
            return {'policy':'all-computed-proposals-v1'}
        expected = self.bundle_hash()
        with self.runtime(validation=changed), self.assertRaisesRegex(RuntimeError,'Historical calibration changed'):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'backups')
        self.assert_rollback()
        self.assertEqual(self.calls,[('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service')])

    def test_auth_club_worker_timer_protection_is_checked_before_web_start(self):
        expected = self.bundle_hash()
        with self.runtime(), patch.object(base,'protected_snapshot',side_effect=[{'auth':'old'},{'auth':'changed'}]), \
                self.assertRaisesRegex(RuntimeError,'Authentication, worker, timers, club trends or context sources changed'):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'backups')
        self.assert_rollback()
        self.assertEqual(self.calls,[('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service')])

    def test_web_is_stopped_before_any_source_replacement_and_started_after_validation(self):
        events = []
        original_write = base.atomic_write
        def command(*args):
            events.append(args[1])
            return ''
        def write(*args,**kwargs):
            self.assertEqual(events[0],'stop')
            self.assertNotIn('start',events)
            events.append('write')
            return original_write(*args,**kwargs)
        def validate():
            self.assertEqual(events,['stop']+['write']*len(installer.FILES))
            events.append('validate')
            return {'policy':'all-computed-proposals-v1'}
        expected = self.bundle_hash()
        with self.runtime(validation=validate), patch.object(base,'command',side_effect=command), \
                patch.object(base,'atomic_write',side_effect=write):
            installer.deploy(self.bundle,expected,backup_root=Path(self.directory.name)/'backups')
        self.assertEqual(events,['stop']+['write']*len(installer.FILES)+['validate','start'])


if __name__ == '__main__':
    unittest.main()
