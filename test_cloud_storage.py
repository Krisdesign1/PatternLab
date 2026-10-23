"""Offline recovery, acknowledgement and unsafe-input checks for cloud state."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

import cloud_storage as storage
from cloud_postgres import BackendBusy, BackendError, Lease, LeaseLost


class MemoryBackend:
    workspace = 'isolated-test'

    def __init__(self):
        self.revision, self.manifest = 0, {}
        self.previous, self.blobs = {}, {}
        self.owner, self.fence = None, 0
        self.uploads = self.publications = 0
        self.fail_publish = False

    def status(self):
        return dict(revision=self.revision, manifest=deepcopy(self.manifest), updated_at='2026-10-23T10:00:00+00:00')

    def acquire(self, owner):
        if self.owner is not None:
            raise BackendBusy('Busy')
        self.owner, self.fence = owner, self.fence + 1
        return Lease(owner, self.fence), self.revision, deepcopy(self.manifest)

    def assert_lease(self, lease):
        if (lease.owner, lease.fence) != (self.owner, self.fence):
            raise LeaseLost('Lease lost')

    renew = assert_lease

    def release(self, lease):
        if (lease.owner, lease.fence) == (self.owner, self.fence):
            self.owner = None

    def read_blob(self, digest):
        return self.blobs[digest]

    def upload(self, lease, digest, content, raw_size):
        self.assert_lease(lease)
        self.uploads += 1
        self.blobs.setdefault(digest, (content, raw_size))

    def publish(self, lease, expected_revision, manifest):
        self.assert_lease(lease)
        if self.fail_publish or expected_revision != self.revision:
            raise BackendError('Publication failed')
        self.publications += 1
        self.previous, self.manifest = self.manifest, deepcopy(manifest)
        self.revision += 1
        return self.revision


class CloudStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'data'
        self.backend = MemoryBackend()
        self.options = dict(PATTERNLAB_DATABASE_URL='configured-only-for-fake-backend',
                            PATTERNLAB_WORKSPACE='isolated-test', PATTERNLAB_CLOUD_ADOPT_LOCAL=None,
                            PATTERNLAB_CLOUD_ALLOW_INSECURE_LOCALHOST_TEST=None,
                            PATTERNLAB_CLOUD_REQUIRED=None)
        for target, name, value in ((storage, '_data_root', self.root),
                                     (storage, '_options', lambda: self.options),
                                     (storage, '_backend_factory', lambda *a, **kw: self.backend)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        storage._file_cache.clear()
        self.addCleanup(storage._file_cache.clear)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def seed(self):
        self.write('api_football/fixtures.json', '{"fixtures":[]}')
        return storage.seed()

    def test_local_mode_does_not_create_data_or_connect(self):
        self.options['PATTERNLAB_DATABASE_URL'] = None
        with patch.object(storage, '_backend_factory', side_effect=AssertionError('No network')):
            with storage.transaction():
                storage.assert_lease()
                storage.checkpoint()
            self.assertFalse(self.root.exists())

    def test_required_mode_never_falls_back_when_secret_missing(self):
        self.options.update(PATTERNLAB_DATABASE_URL=None, PATTERNLAB_CLOUD_REQUIRED='true')
        self.assertTrue(storage.configured())
        # The real constructor must validate a missing URL before connecting.
        from cloud_postgres import PostgresBackend
        with patch.object(storage, '_backend_factory', PostgresBackend):
            with self.assertRaises(storage.CloudStorageError):
                with storage.transaction():
                    self.fail('A write must not run')

    def test_empty_remote_requires_explicit_seed(self):
        with self.assertRaisesRegex(storage.CloudStorageError, 'vide'):
            with storage.transaction():
                self.fail('No implicit initialization')
        self.assertEqual(self.backend.revision, 0)

    def test_seed_preserves_models_and_excludes_secrets_locks(self):
        self.write('models/api-football/39/generation.cbm', 'model')
        self.write('api_football/automation.lock', 'lock')
        self.write('api_football/.temporary', 'private')
        self.write('unrelated/secrets.toml', 'private')
        result = self.seed()
        self.assertTrue(result['initialized'])
        self.assertEqual(set(self.backend.manifest['files']), {'models/api-football/39/generation.cbm',
                                                             'api_football/fixtures.json'})
        self.assertEqual(result['revision'], 1)

    def test_seed_never_replaces_existing_remote(self):
        self.seed()
        first = deepcopy(self.backend.manifest)
        self.write('api_football/fixtures.json', 'new')
        with self.assertRaisesRegex(storage.CloudStorageError, 'déjà'):
            storage.seed()
        self.assertEqual(self.backend.manifest, first)

    def test_new_machine_restores_committed_state(self):
        self.seed()
        with tempfile.TemporaryDirectory() as fresh:
            with patch.object(storage, '_data_root', Path(fresh)):
                with storage.transaction():
                    self.assertEqual((Path(fresh) / 'api_football/fixtures.json').read_text(), '{"fixtures":[]}')
        self.assertEqual(self.backend.revision, 1)

    def test_refusal_to_adopt_local_data_cannot_be_bypassed_by_retry(self):
        self.seed()
        with tempfile.TemporaryDirectory() as fresh:
            root = Path(fresh)
            (root / 'api_football').mkdir()
            original = root / 'api_football/fixtures.json'
            original.write_text('precious local data')
            with patch.object(storage, '_data_root', root):
                for _ in range(2):
                    with self.assertRaisesRegex(storage.CloudStorageError, 'locales'):
                        with storage.transaction():
                            self.fail('Adoption not approved')
                self.assertEqual(original.read_text(), 'precious local data')
                self.assertFalse((root / '.cloud-state.json').exists())
                self.options['PATTERNLAB_CLOUD_ADOPT_LOCAL'] = 'true'
                with storage.transaction():
                    self.assertEqual(original.read_text(), '{"fixtures":[]}')
                backups = list((root / '.cloud-backups').rglob('fixtures.json'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(), 'precious local data')

    def test_persisted_acknowledges_after_remote_commit_even_nested(self):
        self.seed()
        @storage.persisted
        def save():
            self.write('api_football/new.json', 'immutable forecast')
            return 'saved'
        with storage.transaction():
            self.assertEqual(save(), 'saved')
            self.assertIn('api_football/new.json', self.backend.manifest['files'])
        self.assertEqual(self.backend.revision, 2)

    def test_failed_remote_commit_never_returns_success_and_recovers(self):
        self.seed()
        @storage.persisted
        def save():
            self.write('api_football/new.json', 'unconfirmed')
            return 'saved'
        self.backend.fail_publish = True
        with self.assertRaises(storage.CloudStorageError):
            save()
        self.assertNotIn('api_football/new.json', self.backend.manifest['files'])
        self.backend.fail_publish = False
        with storage.transaction():
            self.assertFalse((self.root / 'api_football/new.json').exists())

    def test_caught_partial_failure_cannot_be_published_by_outer_context(self):
        self.seed()
        @storage.persisted
        def broken():
            self.write('api_football/partial.json', 'partial')
            raise ValueError('Business failure')
        with self.assertRaises(storage.CloudStorageError):
            with storage.transaction():
                try:
                    broken()
                except ValueError:
                    pass
        self.assertNotIn('api_football/partial.json', self.backend.manifest['files'])
        with storage.transaction():
            self.assertFalse((self.root / 'api_football/partial.json').exists())

    def test_expected_error_without_mutation_does_not_poison_outer_report(self):
        self.seed()
        @storage.persisted
        def validate():
            raise ValueError('Insufficient history')
        with storage.transaction():
            try:
                validate()
            except ValueError:
                pass
            self.write('api_football/status.json', 'insufficient')
        self.assertIn('api_football/status.json', self.backend.manifest['files'])

    def test_changed_database_requires_new_explicit_adoption(self):
        self.seed()
        self.backend.identity = 'different-database'
        with self.assertRaisesRegex(storage.CloudStorageError, 'locales'):
            with storage.transaction():
                self.fail('Database switch needs explicit adoption')

    def test_nested_committed_reservation_survives_later_crash(self):
        self.seed()
        @storage.persisted
        def reserve():
            self.write('api_football/automation_state.json', '{"requests_today":1}')
        with self.assertRaises(RuntimeError):
            with storage.transaction():
                reserve()
                self.write('api_football/unconfirmed.json', 'unfinished')
                raise RuntimeError('Process stopped')
        with storage.transaction():
            self.assertEqual((self.root / 'api_football/automation_state.json').read_text(), '{"requests_today":1}')
            self.assertFalse((self.root / 'api_football/unconfirmed.json').exists())

    def test_read_only_transactions_do_not_reupload_or_publish(self):
        self.seed()
        counters = self.backend.uploads, self.backend.publications
        for _ in range(2):
            with storage.transaction():
                pass
        self.assertEqual((self.backend.uploads, self.backend.publications), counters)

    def test_only_changed_file_uploaded(self):
        self.seed()
        first_uploads = self.backend.uploads
        with storage.transaction():
            self.write('api_football/new.json', 'new')
        self.assertEqual(self.backend.uploads, first_uploads + 1)

    def test_corrupted_blob_rejected_before_existing_file_mutated(self):
        self.seed()
        entry = self.backend.manifest['files']['api_football/fixtures.json']
        self.backend.blobs[entry['sha256']] = (b'corrupt', entry['size'])
        # A new machine must validate before adopting its unrelated local file.
        self.options['PATTERNLAB_CLOUD_ADOPT_LOCAL'] = 'true'
        with tempfile.TemporaryDirectory() as fresh:
            root = Path(fresh)
            (root / 'api_football').mkdir()
            path = root / 'api_football/fixtures.json'
            path.write_text('keep original')
            with patch.object(storage, '_data_root', root):
                with self.assertRaises(storage.CloudStorageError):
                    with storage.transaction():
                        pass
            self.assertEqual(path.read_text(), 'keep original')

    def test_sqlite_backup_restores_records_and_immutable_trigger(self):
        path = self.root / 'api_football/predictions.sqlite3'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as con:
            con.executescript("CREATE TABLE predictions(id TEXT PRIMARY KEY,created_at TEXT);"
                              "INSERT INTO predictions VALUES('abc','2026-10-22T10:00:00Z');"
                              "CREATE TRIGGER immutable BEFORE UPDATE ON predictions BEGIN SELECT RAISE(ABORT,'immutable'); END;")
        storage.seed()
        with tempfile.TemporaryDirectory() as fresh:
            with patch.object(storage, '_data_root', Path(fresh)):
                with storage.transaction():
                    with sqlite3.connect(Path(fresh) / 'api_football/predictions.sqlite3') as restored:
                        self.assertEqual(restored.execute('SELECT * FROM predictions').fetchall(),
                                         [('abc', '2026-10-22T10:00:00Z')])
                        with self.assertRaises(sqlite3.IntegrityError):
                            restored.execute("UPDATE predictions SET created_at='changed'")

    def test_expired_owner_cannot_publish_or_spend_an_api_call(self):
        self.seed()
        with self.assertRaises(storage.CloudStorageError):
            with storage.transaction():
                self.backend.owner = 'new-owner'
                self.backend.fence += 1
                storage.assert_lease()
        self.assertEqual(self.backend.revision, 1)

    def test_concurrent_local_thread_fails_quickly_instead_of_blocking_ui(self):
        self.seed()
        results = []
        def other():
            try:
                with storage.transaction():
                    results.append('entered')
            except storage.CloudStorageBusy:
                results.append('busy')
        with storage.transaction():
            worker = threading.Thread(target=other)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, ['busy'])

    def test_remote_busy_does_not_run_local_operation(self):
        self.seed()
        self.backend.owner = 'another-process'
        with self.assertRaises(storage.CloudStorageBusy):
            with storage.transaction():
                self.fail('Must wait')

    def test_rejects_unsafe_paths_and_decompression_bomb(self):
        for name in ('../secrets.toml', '/absolute', 'api_football/../escape',
                     'api_football/.secret', 'models/file.lock', 'workspace_settings.json/nested',
                     'api_football\\escape', 'code.py'):
            with self.subTest(name=name), self.assertRaises(storage.CloudStorageError):
                storage._safe_path(name)
        raw = b'x' * 100000
        encoded = zlib.compress(raw)
        with self.assertRaises(storage.CloudStorageError):
            storage._decode(encoded, dict(compressed_size=len(encoded), size=1, sha256=sha256(raw).hexdigest()))


if __name__ == '__main__':
    unittest.main()
