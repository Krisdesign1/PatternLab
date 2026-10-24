"""An explicit local choice isolates the Mac from all old remote settings."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

import cloud_storage as storage


class LocalStorageModeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / '.streamlit').mkdir()
        self.file = self.root / '.streamlit/secrets.toml'
        project = patch.object(storage, '_PROJECT', self.root)
        project.start(); self.addCleanup(project.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        remote = patch.object(storage, '_backend_factory',
            side_effect=AssertionError('Local mode must never construct a remote backend'))
        self.remote = remote.start(); self.addCleanup(remote.stop)

    def settings(self, mode='local'):
        self.file.write_text('PATTERNLAB_DATABASE_URL = "private-test-connection"\n'
            'PATTERNLAB_CLOUD_REQUIRED = true\nPATTERNLAB_STORAGE_MODE = '+mode+'\n')

    def test_local_transactions_and_checkpoints_ignore_stale_cloud_credentials(self):
        self.settings('"local"')
        self.assertFalse(storage.configured())
        destination = self.root / 'local-result.txt'
        @storage.persisted
        def write_local():
            destination.write_text('kept on the Mac')
            storage.assert_lease()
        with storage.transaction():
            write_local()
            self.assertIsNone(storage.checkpoint())
        self.assertEqual(destination.read_text(), 'kept on the Mac')
        self.remote.assert_not_called()

    def test_local_mode_blocks_direct_remote_entrypoints(self):
        self.settings('"local"')
        for operation in (storage._backend, storage.snapshot_identity, storage.committed_revision, storage.seed):
            with self.subTest(operation=operation.__name__), self.assertRaises(storage.CloudStorageError):
                operation()
        with self.assertRaises(storage.CloudStorageError):
            with storage.committed_snapshot():
                self.fail('No remote read in local mode')
        self.remote.assert_not_called()

    def test_private_local_choice_still_wins_over_old_database_environment(self):
        self.settings('"local"')
        os.environ.update(PATTERNLAB_DATABASE_URL='stale-private-connection', PATTERNLAB_CLOUD_REQUIRED='true')
        self.assertFalse(storage.configured())
        self.remote.assert_not_called()

    def test_explicit_environment_mode_has_standard_precedence(self):
        self.settings('"shared"')
        os.environ['PATTERNLAB_STORAGE_MODE'] = 'local'
        self.assertFalse(storage.configured())
        os.environ['PATTERNLAB_STORAGE_MODE'] = 'shared'
        self.assertTrue(storage.configured())

    def test_shared_mode_missing_credentials_never_falls_back_to_local(self):
        self.file.write_text('PATTERNLAB_STORAGE_MODE = "shared"\n')
        self.assertTrue(storage.configured())

    def test_auto_preserves_existing_required_mode(self):
        self.settings('"auto"')
        self.assertTrue(storage.configured())
        self.file.write_text('PATTERNLAB_CLOUD_REQUIRED = true\n')
        self.assertTrue(storage.configured())
        self.file.write_text('')
        self.assertFalse(storage.configured())

    def test_invalid_mode_fails_before_any_connection(self):
        for mode in ('"locla"', '""', 'true', '12'):
            with self.subTest(mode=mode):
                self.settings(mode)
                with self.assertRaises(storage.CloudStorageError):
                    storage.configured()
                with self.assertRaises(storage.CloudStorageError):
                    storage._backend()
        self.remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
