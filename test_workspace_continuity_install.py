"""Source-only workspace rollout; real temporary SQLite archives, no services."""
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

spec = importlib.util.spec_from_file_location('workspace_installer',
    Path(__file__).parent/'deployment/linux/install_workspace_continuity.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
base = installer.base


def sha(data):
    return hashlib.sha256(data).hexdigest()


class WorkspaceContinuityInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory/'app'
        self.root.mkdir()
        root = patch.object(base, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.name = installer.FILES[0]
        self.old = b'OLD_WORKSPACE = True\n'
        self.new = b'NEW_WORKSPACE = True\n'
        (self.root/self.name).write_bytes(self.old)
        for name in (*installer.PRESERVED_SOURCES, base.ENABLED, base.ARTIFACT):
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'preserved')
        for filename, table in base.ARCHIVES.items():
            with sqlite3.connect(self.root/'data/api_football'/filename) as connection:
                connection.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,created_at TEXT,digest TEXT,payload TEXT)')
                connection.execute(f'INSERT INTO {table} VALUES(?,?,?,?)', ('old','before','digest','{}'))
        self.manifest = dict(sha256={self.name: sha(self.new)}, previous_sha256={self.name: sha(self.old)})
        self.bundle = self.directory/'bundle.tar.gz'
        self.backups = self.directory/'backups'

    def bundle_hash(self, *, manifest=None, extra=None, link=False):
        with tarfile.open(self.bundle, 'w:gz') as archive:
            for name, data in {self.name: self.new,
                    'manifest.json': json.dumps(manifest or self.manifest).encode(), **(extra or {})}.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                if name == self.name and link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = '/outside'
                archive.addfile(member, BytesIO(data))
        return base.digest(self.bundle)

    def runtime(self, *, healthy=True, proxy=True):
        self.calls = []
        stack = ExitStack()
        stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))
        stack.enter_context(patch.object(installer, 'proxy_active', return_value=proxy))
        stack.enter_context(patch.object(installer, 'resource_headroom'))
        stack.enter_context(patch.object(base.os, 'chown'))
        stack.enter_context(patch.object(base, 'command', side_effect=lambda *args: self.calls.append(args)))
        self.frozen = stack.enter_context(patch.object(base, 'frozen', return_value={'candidate_unchanged': True}))
        stack.enter_context(patch.object(base, 'protected_snapshot', return_value={'auth-worker-timers': 'same'}))
        stack.enter_context(patch.object(base, 'healthy', side_effect=healthy if callable(healthy) else None,
                                         return_value=healthy if not callable(healthy) else True))
        return stack

    def deploy(self, expected):
        return installer.deploy(self.bundle, expected, backup_root=self.backups)

    def test_only_reviewed_workspace_with_matching_manifest_can_be_loaded(self):
        expected = self.bundle_hash()
        self.assertEqual(installer.FILES, ('scenario_workspace_panel.py',))
        self.assertEqual(installer.load_bundle(self.bundle, expected),
                         (self.manifest, {self.name: self.new}))
        for extra in ({'../outside.py': b'pass'}, {base.ENABLED: b'changed'}):
            with self.subTest(extra=extra), self.assertRaisesRegex(RuntimeError, 'Unexpected bundle scope'):
                installer.load_bundle(self.bundle, self.bundle_hash(extra=extra))
        with self.assertRaisesRegex(RuntimeError, 'Unexpected bundle scope'):
            installer.load_bundle(self.bundle, self.bundle_hash(link=True))
        missing = deepcopy(self.manifest)
        missing['previous_sha256'] = {}
        with self.assertRaisesRegex(RuntimeError, 'Incomplete reviewed manifest'):
            installer.load_bundle(self.bundle, self.bundle_hash(manifest=missing))
        with self.assertRaisesRegex(RuntimeError, 'Bundle hash mismatch'):
            installer.load_bundle(self.bundle, '0'*64)

    def test_unreviewed_live_source_blocks_before_service_shutdown(self):
        expected = self.bundle_hash()
        (self.root/self.name).write_bytes(b'OTHER_WORKSPACE = True\n')
        with self.runtime(), self.assertRaisesRegex(RuntimeError, 'Unreviewed live source'):
            self.deploy(expected)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.backups.exists())

    def test_success_backs_up_only_source_and_resumes_proxy_without_database_writes(self):
        expected = self.bundle_hash()
        databases = {name: (self.root/'data/api_football'/name).read_bytes() for name in base.ARCHIVES}
        real_connect = sqlite3.connect
        connections = []
        def read_only(*args, **kwargs):
            connections.append((args, kwargs))
            self.assertTrue(args[0].endswith('?mode=ro'))
            self.assertTrue(kwargs.get('uri'))
            return real_connect(*args, **kwargs)
        with self.runtime(), patch.object(installer.sqlite3, 'connect', side_effect=read_only):
            receipt = self.deploy(expected)
        self.assertGreaterEqual(self.frozen.call_count, 3)
        self.assertEqual((self.root/self.name).read_bytes(), self.new)
        backup = Path(receipt['backup_directory'])
        self.assertEqual((backup/self.name).read_bytes(), self.old)
        self.assertFalse(list(backup.glob('*.sqlite3')))
        self.assertEqual(receipt['original_records_preserved'], {name: 1 for name in base.ARCHIVES})
        self.assertEqual(receipt['database_writes'], 0)
        self.assertFalse(receipt['worker_restarted'])
        self.assertTrue(receipt['public_proxy_resumed'])
        self.assertGreaterEqual(len(connections), 6)
        self.assertEqual(self.calls, [('/usr/bin/systemctl','stop','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-proxy.service')])
        self.assertTrue(all((self.root/'data/api_football'/name).read_bytes() == data
                            for name, data in databases.items()))

    def test_proxy_not_previously_running_is_not_started(self):
        expected = self.bundle_hash()
        with self.runtime(proxy=False):
            receipt = self.deploy(expected)
        self.assertFalse(receipt['public_proxy_resumed'])
        self.assertEqual(self.calls, [('/usr/bin/systemctl','stop','patternlab-web.service'),
                                     ('/usr/bin/systemctl','start','patternlab-web.service')])

    def test_failed_health_restores_source_and_proxy_while_preserving_worker_appends(self):
        def unhealthy():
            for filename, table in base.ARCHIVES.items():
                with sqlite3.connect(self.root/'data/api_football'/filename) as connection:
                    connection.execute(f'INSERT INTO {table} VALUES(?,?,?,?)', ('worker','later','digest','{}'))
            return False
        expected = self.bundle_hash()
        with self.runtime(healthy=unhealthy), self.assertRaisesRegex(RuntimeError, 'Web health failed'):
            self.deploy(expected)
        self.assertEqual((self.root/self.name).read_bytes(), self.old)
        for filename, table in base.ARCHIVES.items():
            with sqlite3.connect(self.root/'data/api_football'/filename) as connection:
                self.assertEqual(connection.execute(f'SELECT id FROM {table} ORDER BY id').fetchall(), [('old',),('worker',)])
        self.assertEqual(self.calls, [('/usr/bin/systemctl','stop','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-proxy.service'),
            ('/usr/bin/systemctl','stop','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-proxy.service')])

    def test_changed_protected_runtime_blocks_new_web_and_restores_source(self):
        expected = self.bundle_hash()
        with self.runtime(), patch.object(base, 'protected_snapshot', side_effect=[{'auth': 'old'}, {'auth': 'changed'}]), \
                self.assertRaisesRegex(RuntimeError, 'Authentication, worker, timers, club trends or context sources changed'):
            self.deploy(expected)
        self.assertEqual((self.root/self.name).read_bytes(), self.old)
        self.assertEqual(self.calls, [('/usr/bin/systemctl','stop','patternlab-web.service'),
            ('/usr/bin/systemctl','stop','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-web.service'),
            ('/usr/bin/systemctl','start','patternlab-proxy.service')])

    def test_changed_archive_source_or_policy_is_detected(self):
        expected = self.bundle_hash()
        original_write = base.atomic_write
        def change_policy(*args, **kwargs):
            original_write(*args, **kwargs)
            (self.root/base.ENABLED).write_bytes(b'unreviewed-policy')
        with self.runtime(), patch.object(base, 'atomic_write', side_effect=change_policy), \
                self.assertRaisesRegex(RuntimeError, 'Proposal/archive or policy source changed'):
            self.deploy(expected)
        self.assertEqual((self.root/self.name).read_bytes(), self.old)


if __name__ == '__main__':
    unittest.main()
