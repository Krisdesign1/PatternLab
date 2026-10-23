"""Real PostgreSQL fencing tests opt in with an isolated local test DSN."""
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

from cloud_postgres import BackendBusy, BackendError, LeaseLost, PostgresBackend, validate_dsn


class DsnTests(unittest.TestCase):
    def test_tls_required_and_errors_never_echo_credentials(self):
        for dsn in ('postgresql://user:private@remote/db',
                    'postgresql://user:private@remote/db?sslmode=disable',
                    'not a URL private', None):
            with self.subTest(dsn=dsn), self.assertRaises(BackendError) as error:
                validate_dsn(dsn)
            self.assertNotIn('private', str(error.exception))
        self.assertIn('sslmode=require', validate_dsn('postgresql://u:p@remote/db?sslmode=require'))

    def test_unencrypted_exception_is_explicitly_local_only(self):
        validate_dsn('postgresql://u@127.0.0.1/test', allow_localhost=True)
        with self.assertRaises(BackendError):
            validate_dsn('postgresql://u@remote/test', allow_localhost=True)


@unittest.skipUnless(os.environ.get('PATTERNLAB_TEST_POSTGRES_DSN'), 'Isolated PostgreSQL test instance not configured')
class RealPostgresTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ['PATTERNLAB_TEST_POSTGRES_DSN']
        self.backend = PostgresBackend(self.dsn, 'test-' + uuid.uuid4().hex, allow_localhost=True)
        self.backend.initialize()
        self.addCleanup(self.cleanup_workspace)

    def cleanup_workspace(self):
        with self.backend._connection() as con:
            con.execute('DELETE FROM patternlab.workspace_blobs WHERE workspace=%s', (self.backend.workspace,))
            con.execute('DELETE FROM patternlab.workspace_heads WHERE workspace=%s', (self.backend.workspace,))

    def blob(self, value):
        raw = value.encode()
        content = zlib.compress(raw)
        entry = dict(sha256=sha256(raw).hexdigest(), size=len(raw), compressed_size=len(content))
        return entry, content

    def manifest(self, value):
        entry, content = self.blob(value)
        return dict(version=1, files={'api_football/state.json': entry}), entry, content

    def test_exclusive_acquisition_and_fencing_after_forced_expiration(self):
        first, _, _ = self.backend.acquire('first')
        with self.assertRaises(BackendBusy):
            self.backend.acquire('second')
        with self.backend._connection() as con:
            con.execute("UPDATE patternlab.workspace_heads SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s",
                        (self.backend.workspace,))
        second, _, _ = self.backend.acquire('second')
        self.assertGreater(second.fence, first.fence)
        for action in (lambda: self.backend.assert_lease(first), lambda: self.backend.renew(first),
                       lambda: self.backend.publish(first, 0, dict(version=1, files={}))):
            with self.assertRaises(LeaseLost):
                action()
        self.backend.release(first)
        self.backend.assert_lease(second)

    def test_missing_blob_and_stale_revision_never_publish(self):
        lease, revision, _ = self.backend.acquire('writer')
        manifest, entry, content = self.manifest('first')
        with self.assertRaises(BackendError):
            self.backend.publish(lease, revision, manifest)
        self.assertEqual(self.backend.status()['revision'], 0)
        self.backend.upload(lease, entry['sha256'], content, entry['size'])
        self.assertEqual(self.backend.publish(lease, 0, manifest), 1)
        with self.assertRaises(LeaseLost):
            self.backend.publish(lease, 0, manifest)
        self.assertEqual(self.backend.status()['revision'], 1)

    def test_current_and_previous_retained_older_blobs_collected(self):
        lease, _, _ = self.backend.acquire('writer')
        entries = []
        for revision in range(3):
            manifest, entry, content = self.manifest(str(revision))
            entries.append(entry)
            self.backend.upload(lease, entry['sha256'], content, entry['size'])
            self.backend.publish(lease, revision, manifest)
        with self.assertRaises(BackendError):
            self.backend.read_blob(entries[0]['sha256'])
        for entry in entries[1:]:
            self.assertEqual(self.backend.read_blob(entry['sha256'])[1], entry['size'])
        self.assertEqual(self.backend.status()['revision'], 3)

    def test_full_workspace_seed_nested_commit_and_fresh_machine_restore(self):
        import cloud_storage as storage
        options = dict(PATTERNLAB_DATABASE_URL=self.dsn, PATTERNLAB_WORKSPACE=self.backend.workspace,
                       PATTERNLAB_CLOUD_ADOPT_LOCAL=None, PATTERNLAB_CLOUD_REQUIRED=True,
                       PATTERNLAB_CLOUD_ALLOW_INSECURE_LOCALHOST_TEST='true')
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as destination:
            root = Path(source)
            path = root/'api_football/predictions.sqlite3'
            path.parent.mkdir()
            with sqlite3.connect(path) as con:
                con.executescript("CREATE TABLE predictions(id TEXT PRIMARY KEY,created_at TEXT);"
                                  "INSERT INTO predictions VALUES('original','2026-10-22T08:00:00Z');")
            (root/'models').mkdir()
            (root/'models/generation.cbm').write_bytes(b'example-test-model')
            with patch.object(storage, '_options', return_value=options), \
                    patch.object(storage, '_backend_factory', return_value=self.backend), \
                    patch.object(storage, '_data_root', root):
                self.assertEqual(storage.seed()['revision'], 1)
                @storage.persisted
                def save():
                    (root/'api_football/automation_state.json').write_text('{"requests_today":1}')
                with storage.transaction():
                    save()
                    self.assertEqual(self.backend.status()['revision'], 2)
                with patch.object(storage, '_data_root', Path(destination)):
                    with storage.transaction():
                        self.assertEqual((Path(destination)/'api_football/automation_state.json').read_text(),
                                         '{"requests_today":1}')
                        with sqlite3.connect(Path(destination)/'api_football/predictions.sqlite3') as con:
                            self.assertEqual(con.execute('SELECT * FROM predictions').fetchall(),
                                             [('original','2026-10-22T08:00:00Z')])
            storage._file_cache.clear()


if __name__ == '__main__':
    unittest.main()
