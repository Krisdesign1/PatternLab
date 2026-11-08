"""The journal performance release may replace only reviewed source files."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock


spec = importlib.util.spec_from_file_location('journal_install_test',
    Path(__file__).parent / 'deployment/linux/install_journal_performance.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class JournalPerformanceInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'app'; self.root.mkdir()
        self.backups = Path(self.temporary.name) / 'backups'
        for key, value in [('ROOT', self.root), ('BACKUP_ROOT', self.backups)]:
            task = patch.object(install.base, key, value); task.start(); self.addCleanup(task.stop)
        self.payload = {name: b'# reviewed source\n' for name in install.SOURCES}
        (self.root/'prediction_journal.py').write_bytes(b'# old source\n')

    def bundle(self, payload=None):
        payload = self.payload if payload is None else payload
        manifest = dict(sha256={name: hashlib.sha256(data).hexdigest() for name,data in payload.items()},
            previous_sha256={name: install.base.digest(self.root/name) if (self.root/name).exists() else None
                             for name in payload})
        path = Path(self.temporary.name) / 'catalogue.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in {**payload, 'manifest.json':json.dumps(manifest).encode()}.items():
                info = tarfile.TarInfo(name); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path, install.base.digest(path)

    def test_source_scope_and_drift_are_checked(self):
        path, digest = self.bundle()
        self.assertEqual(install.load_bundle(path, digest)[1], self.payload)
        (self.root/'prediction_journal.py').write_bytes(b'# independent modification\n')
        with self.assertRaisesRegex(RuntimeError, 'reviewed baseline'):
            install.load_bundle(path, digest)

    def test_data_secrets_and_paths_cannot_enter_payload(self):
        for name in ('../escape.py', 'data/predictions.sqlite3', '.streamlit/secrets.toml', 'catboost_scenarios.py'):
            with self.subTest(name=name):
                path, digest = self.bundle({**self.payload, name:b'forbidden\n'})
                with self.assertRaisesRegex(RuntimeError, 'source scope'):
                    install.load_bundle(path, digest)

    def test_failed_health_restores_sources_not_database_and_resumes_services(self):
        path, digest = self.bundle()
        patches = [patch.object(install.views.os, 'geteuid', return_value=0),
            patch.object(install.base, 'frozen', return_value={}),
            patch.object(install.views, 'protected_sources', return_value={}),
            patch.object(install.views, 'readonly_archives', return_value={'manual_analyses.sqlite3':{'original':1}}),
            patch.object(install.base, 'require_legacy_unchanged'),
            patch.object(install.base, 'application_owner', return_value=(0,0)),
            patch.object(install.base, 'atomic_write', side_effect=lambda p,data,**kw: p.write_bytes(data)),
            patch.object(install.base, 'healthy', return_value=False),
            patch.object(install.base, 'command'),
            patch.object(install.views.subprocess, 'run', return_value=Mock(returncode=0))]
        started = [item.start() for item in patches]
        try:
            with self.assertRaisesRegex(RuntimeError, 'health'):
                install.deploy(path, digest)
            self.assertEqual((self.root/'prediction_journal.py').read_bytes(), b'# old source\n')
            self.assertFalse((self.root/'journal_page_data.py').exists())
            self.assertFalse(list(self.backups.rglob('*.sqlite3')))
            commands = started[-2].call_args_list
            self.assertEqual(sum(item.args == ('/usr/bin/systemctl','start','patternlab-proxy.service')
                                 for item in commands), 2)
        finally:
            for item in reversed(patches): item.stop()


if __name__ == '__main__': unittest.main()
