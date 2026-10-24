"""A local switch preserves histories and credentials without network access."""
import contextlib
import io
from pathlib import Path
import socket
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import configure_local_storage as configure


class ConfigureLocalStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '.streamlit' / 'secrets.toml'
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('No network in local configuration'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def existing(self, content):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(content)

    def configure(self, **kwargs):
        configure.configure_storage(project_root=self.root, **kwargs)
        return tomllib.loads(self.path.read_text())

    def test_existing_credentials_and_unrelated_bytes_are_preserved(self):
        original = (b'# private settings\r\nPATTERNLAB_DATABASE_URL = "postgresql://private-test"\r\n'
                    b'PATTERNLAB_CLOUD_REQUIRED = true\r\n[api_football]\r\nkey = "private-api-test"\r\n')
        self.existing(original)
        parsed = self.configure()
        self.assertTrue(self.path.read_bytes().endswith(original))
        self.assertEqual(parsed['PATTERNLAB_STORAGE_MODE'], 'local')
        self.assertEqual(parsed['api_football']['key'], 'private-api-test')
        self.assertTrue(parsed['PATTERNLAB_CLOUD_REQUIRED'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_owner_updates_only_existing_owner_and_mode(self):
        self.existing(b'PATTERNLAB_STORAGE_MODE = "shared" # old\n[access]\nprovider="google"\nlocal_owner = false\n[api_football]\nkey="private-test"\n')
        parsed = self.configure(owner=True)
        self.assertEqual(parsed['PATTERNLAB_STORAGE_MODE'], 'local')
        self.assertEqual(parsed['access'], {'provider': 'google', 'local_owner': True})
        self.assertIn(b'provider="google"\n', self.path.read_bytes())
        self.assertIn(b'[api_football]\nkey="private-test"\n', self.path.read_bytes())

    def test_empty_missing_or_existing_owner_configs_are_idempotent(self):
        for original in (b'', b'[access]\nprovider="google"', b'access.provider="google"\n', b'access.local_owner=false\n'):
            with self.subTest(original=original):
                self.existing(original)
                self.configure(owner=True)
                first = self.path.read_bytes()
                self.configure(owner=True)
                self.assertEqual(self.path.read_bytes(), first)
                self.assertTrue(tomllib.loads(first.decode())['access']['local_owner'])

    def test_quoted_keys_and_multiline_private_values_are_not_confused(self):
        original = (b'"PATTERNLAB_STORAGE_MODE" = """shared"""\n'
                    b'other = """\n[access]\nlocal_owner = false\nPATTERNLAB_STORAGE_MODE = \'shared\'\n"""\n'
                    b'["access"] # owner settings\nprovider="google"\n[api_football]\nkey="private"\n')
        self.existing(original)
        before = tomllib.loads(original.decode())
        after = self.configure(owner=True)
        self.assertEqual(after['other'], before['other'])
        self.assertEqual(after['api_football'], before['api_football'])
        self.assertTrue(after['access']['local_owner'])
        self.assertEqual(after['PATTERNLAB_STORAGE_MODE'], 'local')

    def test_owner_is_explicit_and_does_not_replace_other_access_permissions(self):
        self.existing(b'[access]\nlocal_owner=false\nadmin_emails=["owner@example.test"]\n')
        parsed = self.configure()
        self.assertFalse(parsed['access']['local_owner'])
        self.assertEqual(parsed['access']['admin_emails'], ['owner@example.test'])

    def test_compact_owner_table_refuses_without_rewriting_private_values(self):
        original = b'access={provider="google",local_owner=false}\n'
        self.existing(original)
        with self.assertRaises(configure.ConfigurationError):
            self.configure(owner=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.configure()['PATTERNLAB_STORAGE_MODE'], 'local')

    def test_invalid_documents_and_symlinks_are_refused(self):
        for original in (b'[broken\n', b'key="\xff"'):
            self.existing(original)
            with self.assertRaises(configure.ConfigurationError):
                self.configure()
            self.assertEqual(self.path.read_bytes(), original)
        target = self.root / 'external.toml'
        target.write_bytes(b'key="private"\n')
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(configure.ConfigurationError):
            self.configure()
        self.assertEqual(target.read_bytes(), b'key="private"\n')

    def test_concurrent_change_before_replace_keeps_new_document(self):
        original, concurrent = b'key="original"\n', b'key="concurrent"\n'
        self.existing(original)
        real_temporary = configure.tempfile.NamedTemporaryFile
        def changed(*args, **kwargs):
            self.path.write_bytes(concurrent)
            return real_temporary(*args, **kwargs)
        with patch.object(configure.tempfile, 'NamedTemporaryFile', side_effect=changed):
            with self.assertRaises(configure.ConfigurationError):
                self.configure()
        self.assertEqual(self.path.read_bytes(), concurrent)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_atomic_write_failure_and_cli_never_print_private_values(self):
        original = b'PATTERNLAB_DATABASE_URL="postgresql://never-print-this"\n'
        self.existing(original)
        output = io.StringIO()
        with patch.object(configure.os, 'replace', side_effect=OSError('never-print-this')), contextlib.redirect_stdout(output):
            self.assertEqual(configure.main([], project_root=self.root), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn('never-print-this', output.getvalue())
        self.assertNotIn('postgresql://', output.getvalue())
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_only_configuration_changes_not_data_models_or_journal(self):
        self.root.joinpath('data').mkdir()
        data = self.root / 'data' / 'fixture-test.json'
        data.write_bytes(b'preserved history')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(configure.main(['--owner'], project_root=self.root), 0)
        self.assertEqual(data.read_bytes(), b'preserved history')
        self.assertIn('aucun transfert', output.getvalue())


if __name__ == '__main__':
    unittest.main()
