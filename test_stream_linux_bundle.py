from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

from scripts import stream_linux_bundle as bundle


class StreamLinuxBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.stage = Path(temporary.name).resolve() / 'stage'
        self.stage.mkdir()
        self.contents = {'app.py': b'VALUE = 42\n',
                         'deployment/linux/run.sh': b'#!/bin/sh\nexit 0\n'}
        for name, content in self.contents.items():
            path = self.stage / 'files' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.stage / 'files/deployment/linux/run.sh').chmod(0o750)
        self.base = dict(version='local-recovery-v1', state='complete', files=[
            self.item('app.py', kind='file')])
        self.extra = dict(version='patternlab-linux-local-staging-v1', state='complete',
                          files=[self.item('deployment/linux/run.sh')],
                          bytes=sum(map(len, self.contents.values())))
        self.verification = dict(version='local-recovery-v1', state='verified',
                                 files_verified=1, databases_restored=[])
        self.publish()

    def item(self, name, **extra):
        content = self.contents[name]
        return dict(path=name, bytes=len(content), sha256=sha256(content).hexdigest(), **extra)

    def publish(self):
        raw = json.dumps(self.base).encode()
        (self.stage / 'manifest.json').write_bytes(raw)
        self.extra['recovery_manifest_sha256'] = sha256(raw).hexdigest()
        (self.stage / 'linux-bundle-manifest.json').write_text(json.dumps(self.extra))
        (self.stage / 'verification.json').write_text(json.dumps(self.verification))

    def assert_preflight_failure(self):
        output = io.BytesIO()
        with self.assertRaises((ValueError, OSError)):
            bundle.stream_bundle(self.stage, output)
        self.assertEqual(output.getvalue(), b'')

    def test_archive_contains_exact_union_and_root_manifests_without_extra_files(self):
        for name in ('app.py-wal', 'app.py-shm', 'unlisted-secret.txt'):
            (self.stage / 'files' / name).write_bytes(b'UNLISTED SYNTHETIC CONTENT')
        output, diagnostics = io.BytesIO(), io.StringIO()
        plan = bundle.stream_bundle(self.stage, output, diagnostics=diagnostics)
        self.assertEqual(plan.inventory_files, 2)
        self.assertEqual(output.getvalue()[:3], b'\x1f\x8b\x08')
        self.assertEqual(output.getvalue()[8], 4)  # gzip fast compression flag.
        expected = set(bundle.ROOT_MANIFESTS) | {'files/' + name for name in self.contents}
        with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode='r:gz') as archive:
            self.assertEqual(set(archive.getnames()), expected)
            self.assertEqual(len(archive.getmembers()), len(expected))
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))
            for name, content in self.contents.items():
                self.assertEqual(archive.extractfile('files/' + name).read(), content)
            self.assertEqual(archive.getmember('files/deployment/linux/run.sh').mode, 0o750)
        statuses = [json.loads(line) for line in diagnostics.getvalue().splitlines()]
        self.assertEqual([item['state'] for item in statuses], ['preflight_verified', 'stream_complete'])
        self.assertNotIn('app.py', diagnostics.getvalue())

    def test_unsafe_inventory_paths_emit_nothing(self):
        original = self.base['files'][0]['path']
        for name in ('../escape', '/absolute', 'a//b', './a', 'a/../b', '.',
                     'a\\b', 'a\x00b', 'a\nb', 'C:/escape'):
            with self.subTest(path=repr(name)):
                self.base['files'][0]['path'] = name
                self.publish()
                self.assert_preflight_failure()
        self.base['files'][0]['path'] = original

    def test_duplicates_within_or_across_inventories_emit_nothing(self):
        original = list(self.base['files'])
        self.base['files'].append(self.base['files'][0].copy())
        self.verification['files_verified'] = 2
        self.publish()
        self.assert_preflight_failure()
        self.base['files'] = original
        self.verification['files_verified'] = 1
        self.extra['files'] = [self.item('app.py')]
        self.publish()
        self.assert_preflight_failure()

    def test_tamper_missing_file_or_manifest_binding_mismatch_emits_nothing(self):
        path = self.stage / 'files/app.py'
        path.write_bytes(b'VALUE = 99\n')  # Same size, different SHA-256.
        self.assert_preflight_failure()
        path.unlink()
        self.assert_preflight_failure()
        path.write_bytes(self.contents['app.py'])
        with (self.stage / 'manifest.json').open('ab') as handle:
            handle.write(b'\n')
        self.assert_preflight_failure()

    def test_symlink_file_parent_stage_or_root_manifest_is_rejected(self):
        source = self.stage / 'files/app.py'
        saved = source.with_name('saved.py')
        source.rename(saved)
        source.symlink_to(saved)
        self.assert_preflight_failure()
        source.unlink()
        saved.rename(source)
        parent = self.stage / 'files/deployment'
        saved_parent = parent.with_name('saved-deployment')
        parent.rename(saved_parent)
        parent.symlink_to(saved_parent, target_is_directory=True)
        self.assert_preflight_failure()
        parent.unlink()
        saved_parent.rename(parent)
        alias = self.stage.with_name('alias')
        alias.symlink_to(self.stage, target_is_directory=True)
        with self.assertRaises(OSError):
            bundle.preflight(alias)
        manifest = self.stage / 'verification.json'
        saved_manifest = manifest.with_name('saved-verification.json')
        manifest.rename(saved_manifest)
        manifest.symlink_to(saved_manifest)
        self.assert_preflight_failure()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO requires POSIX')
    def test_special_file_is_rejected_without_blocking(self):
        path = self.stage / 'files/app.py'
        path.unlink()
        os.mkfifo(path)
        self.assert_preflight_failure()

    def test_changed_metadata_after_preflight_prevents_first_archive_byte(self):
        plan = bundle.preflight(self.stage)
        path = self.stage / 'files/app.py'
        path.chmod(0o600 if path.stat().st_mode & 0o777 != 0o600 else 0o640)
        output = io.BytesIO()
        with self.assertRaises(ValueError):
            bundle.write_archive(plan, output)
        self.assertEqual(output.getvalue(), b'')

    def test_change_after_archive_starts_aborts_stream(self):
        plan = bundle.preflight(self.stage)
        path = self.stage / 'files/app.py'

        class MutatingOutput(io.BytesIO):
            changed = False

            def write(self, content):
                if not self.changed:
                    self.changed = True
                    path.write_bytes(b'VALUE = 99\n')
                return super().write(content)

        output = MutatingOutput()
        with self.assertRaises(ValueError):
            bundle.write_archive(plan, output)
        self.assertGreater(len(output.getvalue()), 0)

    def test_invalid_counts_types_receipt_or_repeated_json_keys_emit_nothing(self):
        self.extra['bytes'] += 1
        self.publish()
        self.assert_preflight_failure()
        self.extra['bytes'] -= 1
        self.base['files'][0]['bytes'] = True
        self.publish()
        self.assert_preflight_failure()
        self.base['files'][0] = self.item('app.py', kind='unknown')
        self.publish()
        self.assert_preflight_failure()
        self.base['files'][0] = self.item('app.py', kind='file')
        self.verification['state'] = 'incomplete'
        self.publish()
        self.assert_preflight_failure()
        self.verification['state'] = 'verified'
        self.publish()
        (self.stage / 'verification.json').write_text('{"state":"verified","state":"verified"}')
        self.assert_preflight_failure()

    def test_cli_check_only_and_preflight_failure_keep_stdout_empty(self):
        script = Path(bundle.__file__).resolve()
        command = [sys.executable, str(script), '--stage', str(self.stage)]
        result = subprocess.run([*command, '--check-only'], capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(json.loads(result.stderr)['state'], 'preflight_verified')
        (self.stage / 'files/app.py').write_bytes(b'SYNTHETIC PRIVATE CONTENT')
        result = subprocess.run(command, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(json.loads(result.stderr)['state'], 'error')
        self.assertNotIn(b'SYNTHETIC', result.stderr)
        self.assertNotIn(str(self.stage).encode(), result.stderr)


if __name__ == '__main__':
    unittest.main()
