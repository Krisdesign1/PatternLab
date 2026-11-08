"""Source-only rollout rejects drift and restores sources on failure."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock


spec = importlib.util.spec_from_file_location('views_install',
    Path(__file__).parent / 'deployment/linux/install_table_views.py')
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


class TableViewsInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'app'; self.root.mkdir()
        self.backups = Path(self.temp.name) / 'backups'
        for name, value in [('ROOT', self.root), ('BACKUP_ROOT', self.backups)]:
            task = patch.object(install.base, name, value); task.start(); self.addCleanup(task.stop)
        self.payload = {name: b'# source\n' for name in (
            'app.py', 'table_views.py', 'table_view_state.py', 'table_view_editor.js', 'table_view_editor.css')}
        (self.root / 'app.py').write_bytes(b'# original\n')

    def bundle(self, payload=None):
        payload = payload or self.payload
        manifest = dict(sha256={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()},
            previous_sha256={name: install.base.digest(self.root/name) if (self.root/name).exists() else None
                             for name in payload})
        path = Path(self.temp.name) / 'bundle.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in {**payload, 'manifest.json': json.dumps(manifest).encode()}.items():
                info = tarfile.TarInfo(name); info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path, install.base.digest(path)

    def test_verified_bundle_and_changed_live_source(self):
        path, digest = self.bundle()
        self.assertEqual(install.load_bundle(path, digest)[1], self.payload)
        (self.root/'app.py').write_bytes(b'# another deployment\n')
        with self.assertRaisesRegex(RuntimeError, 'reviewed baseline'):
            install.load_bundle(path, digest)

    def test_nested_or_non_source_payload_is_rejected(self):
        for name in ('../escape.py', 'data/forecast.py', 'secrets.toml'):
            with self.subTest(name=name):
                path, digest = self.bundle({**self.payload, name: b'x=1\n'})
                with self.assertRaisesRegex(RuntimeError, 'presentation files'):
                    install.load_bundle(path, digest)

    def test_failed_health_restores_old_sources_and_resumes_proxy(self):
        path, digest = self.bundle()
        patches = [patch.object(install.os, 'geteuid', return_value=0),
            patch.object(install.base, 'frozen', return_value={}),
            patch.object(install, 'protected_sources', return_value={}),
            patch.object(install, 'readonly_archives', return_value={}),
            patch.object(install.base, 'require_legacy_unchanged'),
            patch.object(install.base, 'application_owner', return_value=(0, 0)),
            patch.object(install.base, 'atomic_write', side_effect=lambda p,data,**kw: p.write_bytes(data)),
            patch.object(install.base, 'healthy', return_value=False),
            patch.object(install.base, 'command'),
            patch.object(install.subprocess, 'run', return_value=Mock(returncode=0))]
        started = [task.start() for task in patches]
        try:
            with self.assertRaisesRegex(RuntimeError, 'health'):
                install.deploy(path, digest)
            self.assertEqual((self.root/'app.py').read_bytes(), b'# original\n')
            self.assertFalse((self.root/'table_views.py').exists())
            commands = started[-2].call_args_list
            self.assertEqual(sum(item.args == ('/usr/bin/systemctl','start','patternlab-proxy.service')
                                 for item in commands), 2)
        finally:
            for task in reversed(patches): task.stop()


if __name__ == '__main__': unittest.main()
