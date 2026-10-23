"""Real PostgreSQL fencing tests opt in with an isolated local test DSN."""
from contextlib import nullcontext
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
import zlib

from cloud_postgres import BackendBusy, BackendError, Lease, LeaseLost, PostgresBackend, validate_dsn


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


class PublishBulkTests(unittest.TestCase):
    def setUp(self):
        self.backend = PostgresBackend('postgresql://u:p@remote/db?sslmode=require', 'bulk-test')
        self.lease = Lease('writer', 1)
        self.first = dict(sha256='a' * 64, size=10, compressed_size=8)
        self.second = dict(sha256='b' * 64, size=20, compressed_size=12)
        self.manifest = dict(version=1, files={'api_football/one.json': self.first,
            'api_football/same.json': dict(self.first), 'api_football/two.json': self.second})

    def connection(self, rows):
        con = Mock()
        con.execute.side_effect = [Mock(fetchone=Mock(return_value=(0, {}))),
            Mock(fetchall=Mock(return_value=rows)), Mock(fetchone=Mock(return_value=(1,))), Mock()]
        return con

    def test_many_files_and_duplicate_digests_use_one_bulk_metadata_query(self):
        con = self.connection([('a' * 64, 10, 8), ('b' * 64, 20, 12)])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
            self.assertEqual(self.backend.publish(self.lease, 0, self.manifest), 1)
        queries = con.execute.call_args_list
        reads = [call for call in queries if call.args[0].lstrip().startswith('SELECT')
                 and 'FROM patternlab.workspace_blobs' in call.args[0]]
        self.assertEqual(len(reads), 1)
        self.assertIn('digest=ANY(%s)', reads[0].args[0])
        self.assertEqual(reads[0].args[1], ('bulk-test', ['a' * 64, 'b' * 64]))
        self.assertEqual(len(queries), 4)
        self.assertIn('lease_until > clock_timestamp()', queries[2].args[0])
        self.assertIn('revision=%s', queries[2].args[0])

    def test_missing_or_incorrect_blob_metadata_rejects_before_head_update(self):
        for rows in ([('a' * 64, 10, 8)], [('a' * 64, 11, 8), ('b' * 64, 20, 12)],
                     [('a' * 64, 10, 8), ('b' * 64, 20, 13)]):
            with self.subTest(rows=rows):
                con = self.connection(rows)
                with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
                    with self.assertRaises(BackendError):
                        self.backend.publish(self.lease, 0, self.manifest)
                self.assertEqual(con.execute.call_count, 2)

    def test_conflicting_metadata_on_duplicate_digest_is_not_hidden(self):
        self.manifest['files']['api_football/same.json']['size'] = 999
        con = self.connection([('a' * 64, 10, 8), ('b' * 64, 20, 12)])
        with patch.object(self.backend, '_connection', return_value=nullcontext(con)):
            with self.assertRaises(BackendError):
                self.backend.publish(self.lease, 0, self.manifest)
        self.assertEqual(con.execute.call_count, 2)


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

    def test_bulk_validation_multiple_files_with_duplicate_and_missing_digest(self):
        lease, _, _ = self.backend.acquire('writer')
        first, first_content = self.blob('one')
        second, second_content = self.blob('two')
        manifest = dict(version=1, files={'api_football/one.json': first,
            'api_football/copy.json': dict(first), 'api_football/two.json': second})
        self.backend.upload(lease, first['sha256'], first_content, first['size'])
        with self.assertRaises(BackendError):
            self.backend.publish(lease, 0, manifest)
        self.assertEqual(self.backend.status()['revision'], 0)
        self.backend.upload(lease, second['sha256'], second_content, second['size'])
        manifest['files']['api_football/copy.json']['compressed_size'] += 1
        with self.assertRaises(BackendError):
            self.backend.publish(lease, 0, manifest)
        self.assertEqual(self.backend.status()['revision'], 0)
        manifest['files']['api_football/copy.json'] = dict(first)
        self.assertEqual(self.backend.publish(lease, 0, manifest), 1)

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
