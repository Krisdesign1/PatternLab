"""The local setup command preserves credentials and never exposes passwords."""
import contextlib
import getpass
import io
from pathlib import Path
import stat
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit
import warnings

from scripts import configure_cloud_storage as configure


class ConfigureCloudStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / '.streamlit' / 'secrets.toml'
        self.options = dict(host='aws-0-eu-west-1.pooler.supabase.com', user='postgres.exampleproject',
                            project_root=self.root)

    def existing(self, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(value)

    def configure(self, password='private-password', **options):
        configure.configure_storage(**{**self.options, **options}, password_prompt=lambda _: password)

    def test_preserves_existing_api_secret_bytes_and_root_scope(self):
        original = b'# API private config\r\n[api_football]\r\nkey = "test-only-api-key"\r\n'
        self.existing(original)
        self.configure()
        result = self.path.read_bytes()
        self.assertTrue(result.endswith(original))
        parsed = tomllib.loads(result.decode())
        self.assertTrue(parsed['PATTERNLAB_CLOUD_REQUIRED'])
        self.assertEqual(parsed['PATTERNLAB_WORKSPACE'], 'patternlab-main')
        self.assertEqual(parsed['api_football'], {'key': 'test-only-api-key'})
        self.assertNotIn('PATTERNLAB_DATABASE_URL', parsed['api_football'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_special_password_is_url_encoded_and_tls_is_required(self):
        password = 'a:@/?#%[] space+é\\"\n'
        self.configure(password)
        parsed = tomllib.loads(self.path.read_text())
        dsn = urlsplit(parsed['PATTERNLAB_DATABASE_URL'])
        self.assertEqual(unquote(dsn.password), password)
        self.assertEqual(dsn.hostname, self.options['host'])
        self.assertEqual(dsn.port, 5432)
        self.assertEqual(dsn.query, 'sslmode=require')

    def test_refuses_any_existing_root_cloud_key_without_prompt_or_change(self):
        for key in configure.CLOUD_KEYS:
            with self.subTest(key=key):
                original = f'{key} = ""\n[api_football]\nkey="private"\n'.encode()
                self.existing(original)
                prompt = Mock()
                with self.assertRaises(configure.ConfigurationError):
                    configure.configure_storage(**self.options, password_prompt=prompt)
                self.assertEqual(self.path.read_bytes(), original)
                prompt.assert_not_called()

    def test_malformed_toml_and_invalid_utf8_are_never_modified(self):
        for original in (b'[broken\n', b'key="\xff"'):
            with self.subTest(original=original):
                self.existing(original)
                prompt = Mock()
                with self.assertRaises(configure.ConfigurationError):
                    configure.configure_storage(**self.options, password_prompt=prompt)
                self.assertEqual(self.path.read_bytes(), original)
                prompt.assert_not_called()

    def test_invalid_hosts_and_fields_never_prompt(self):
        for changes in ({'host': 'evil.example'}, {'host': 'aws-0-eu-west-1.pooler.supabase.com.evil.example'},
                        {'host': 'https://aws-0-eu-west-1.pooler.supabase.com'}, {'host': 'x@aws-0-eu-west-1.pooler.supabase.com'},
                        {'user': ''}, {'port': 0}, {'workspace': '../escape'}, {'database': 'postgres/other'}):
            with self.subTest(changes=changes):
                prompt = Mock()
                with self.assertRaises(configure.ConfigurationError):
                    configure.configure_storage(**{**self.options, **changes}, password_prompt=prompt)
                prompt.assert_not_called()
                self.assertFalse(self.path.exists())

    def test_password_cancellation_and_getpass_echo_fallback_leave_no_file(self):
        def fallback(_):
            warnings.warn('Password input may be echoed.', getpass.GetPassWarning)
            return 'private'
        for prompt in (Mock(side_effect=EOFError), Mock(side_effect=KeyboardInterrupt), fallback):
            with self.subTest(prompt=prompt):
                with self.assertRaises(configure.ConfigurationError):
                    configure.configure_storage(**self.options, password_prompt=prompt)
                self.assertFalse(self.path.exists())

    def test_changed_secrets_during_prompt_are_not_replaced(self):
        self.existing(b'[api_football]\nkey="original"\n')
        replacement = b'[api_football]\nkey="concurrent"\n'
        def prompt(_):
            self.path.write_bytes(replacement)
            return 'private'
        with self.assertRaises(configure.ConfigurationError):
            configure.configure_storage(**self.options, password_prompt=prompt)
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_write_failure_keeps_original_and_cleans_temporary_files(self):
        original = b'[api_football]\nkey="original"\n'
        self.existing(original)
        with patch.object(configure.os, 'replace', side_effect=OSError('private-password')):
            with self.assertRaises(configure.ConfigurationError) as raised:
                self.configure()
        self.assertNotIn('private-password', str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_symlink_target_is_never_modified(self):
        external = self.root / 'external.toml'
        original = b'[api_football]\nkey="original"\n'
        external.write_bytes(original)
        self.path.parent.mkdir()
        self.path.symlink_to(external)
        with self.assertRaises(configure.ConfigurationError):
            self.configure()
        self.assertEqual(external.read_bytes(), original)

    def test_main_success_and_error_never_print_password_or_dsn(self):
        args = ['--host', self.options['host'], '--user', self.options['user']]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(configure.main(args, project_root=self.root,
                                           password_prompt=lambda _: 'never-print-this'), 0)
            self.assertEqual(configure.main(args, project_root=self.root,
                                           password_prompt=lambda _: 'never-print-this'), 1)
        self.assertNotIn('never-print-this', output.getvalue())
        self.assertNotIn('postgresql://', output.getvalue())


if __name__ == '__main__':
    unittest.main()
