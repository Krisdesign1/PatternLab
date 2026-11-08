from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import offline_environment as env
import local_recovery as recovery


class OfflineEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.bundle=self.root/'bundle';self.bundle.mkdir()
        source=self.bundle/'site-packages/example.py';source.parent.mkdir();source.write_text('VALUE=42\n')
        self.manifest=dict(version=env.VERSION,state='complete',runtime=env.runtime(),
            distributions=[dict(name='example',version='1')],files=[dict(path='example.py',bytes=source.stat().st_size,
                sha256=recovery.fingerprint(source))])
        self.index=self.bundle/'manifest.json';self.index.write_text(json.dumps(self.manifest))
        self.sha=recovery.fingerprint(self.index)

    def test_bad_hash_refuses_before_creating_environment(self):
        with patch.object(env.venv,'EnvBuilder') as builder:
            with self.assertRaises(ValueError):env.rebuild(self.bundle,self.root/'new','0'*64)
        builder.assert_not_called();self.assertFalse((self.root/'new').exists())

    def test_changed_package_refuses_before_creating_environment(self):
        (self.bundle/'site-packages/example.py').write_text('CHANGED')
        with patch.object(env.venv,'EnvBuilder') as builder:
            with self.assertRaises(ValueError):env.rebuild(self.bundle,self.root/'new',self.sha)
        builder.assert_not_called()

    def test_runtime_difference_requires_reprovisioning(self):
        self.manifest['runtime']['python']='different'
        with self.assertRaises(ValueError):env._items(self.manifest)

    def test_invalid_paths_types_duplicate_and_sizes_rejected(self):
        for changes in (dict(path='../escape'),dict(path='/tmp/escape'),dict(path='a//b'),
                        dict(path=['unhashable']),dict(path='a\\b'),dict(bytes=True),dict(sha256='bad')):
            with self.subTest(changes=changes):
                value=deepcopy(self.manifest);value['files'][0].update(changes)
                with self.assertRaises(ValueError):env._items(value)
        self.manifest['files']*=2
        with self.assertRaises(ValueError):env._items(self.manifest)

    def test_bundle_symlink_rejected(self):
        source=self.bundle/'site-packages/example.py';source.unlink();source.symlink_to(self.index)
        with self.assertRaises(ValueError):env.rebuild(self.bundle,self.root/'new',self.sha)

    def test_existing_destination_preserved(self):
        dest=self.root/'existing';dest.mkdir();(dest/'keep').write_text('unchanged')
        with self.assertRaises(ValueError):env.rebuild(self.bundle,dest,self.sha)
        self.assertEqual((dest/'keep').read_text(),'unchanged')

    def test_rehydration_uses_new_venv_without_pip_or_network(self):
        dest=self.root/'new'
        site=dest/'lib'/f'python{env.sys.version_info.major}.{env.sys.version_info.minor}'/'site-packages'
        with patch.object(env.venv,'EnvBuilder') as builder:
            builder.return_value.create.side_effect=lambda _:site.mkdir(parents=True)
            result=env.rebuild(self.bundle,dest,self.sha)
        builder.assert_called_once_with(with_pip=False,symlinks=False)
        self.assertEqual((site/'example.py').read_text(),'VALUE=42\n')
        self.assertEqual(result['state'],'rehydrated');self.assertFalse(result['wheel_installation_proven'])
        self.assertFalse(result['network_used'])

    def test_executable_pth_refused_and_incomplete_retained(self):
        source=self.root/'installed';source.mkdir();(source/'evil.pth').write_text('import anything\n')
        dest=self.root/'capture'
        with patch.object(env.sysconfig,'get_path',return_value=str(source)),patch.object(env,'packages',return_value=[]):
            with self.assertRaises(ValueError):env.capture(dest)
        self.assertEqual(json.loads((dest/'manifest.json').read_text())['state'],'incomplete')


if __name__=='__main__':unittest.main()
