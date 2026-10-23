"""Offline bulk restore bounds, deduplication, and all-or-nothing validation."""
from hashlib import sha256
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import zlib

import cloud_storage as storage


class BulkBackend:
    workspace = 'bulk-restore-test'

    def __init__(self, blobs):
        self.blobs, self.calls = blobs, []
        self.transform = None

    def read_blobs(self, digests):
        self.calls.append(list(digests))
        result = {digest: self.blobs[digest] for digest in digests}
        return self.transform(result, len(self.calls)) if self.transform else result

    def read_blob(self, digest):
        raise AssertionError('Bulk backend must use read_blobs')


class SingleBackend:
    workspace = BulkBackend.workspace

    def __init__(self, blobs):
        self.blobs, self.calls = blobs, []

    def read_blob(self, digest):
        self.calls.append(digest)
        return self.blobs[digest]


class Session:
    revision = 7

    def __init__(self, root, manifest, backend):
        self.root, self.manifest, self.backend = root, manifest, backend
        self.checks, self.fail_check = 0, None

    def check(self):
        self.checks += 1
        if self.checks == self.fail_check:
            raise storage.CloudStorageError('Lease lost')


class BatchRestoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.entries, self.blobs, self.expected = {}, {}, {}
        storage._file_cache.clear()
        self.addCleanup(storage._file_cache.clear)

    def add(self, name, raw):
        digest = sha256(raw).hexdigest()
        compressed = zlib.compress(raw, 6)
        self.entries[name] = dict(sha256=digest, size=len(raw), compressed_size=len(compressed))
        self.blobs[digest] = compressed, len(raw)
        self.expected[name] = raw
        return digest

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def session(self, backend_type=BulkBackend):
        backend = backend_type(self.blobs)
        session = Session(self.root, dict(version=1, files=self.entries), backend)
        return session

    def restore(self, session):
        marker = dict(version=1, workspace=session.backend.workspace, database=session.backend.workspace)
        storage._restore(session, marker)

    def assert_restored(self):
        for name, raw in self.expected.items():
            self.assertEqual((self.root / name).read_bytes(), raw)

    def test_cold_restore_limits_each_batch_to_32_unique_objects(self):
        for index in range(70):
            self.add(f'api_football/{index}.json', f'fixture {index}'.encode())
        session = self.session()
        self.restore(session)
        self.assertEqual([len(call) for call in session.backend.calls], [32, 32, 6])
        self.assert_restored()

    def test_real_compressed_size_caps_each_batch_at_four_mib(self):
        generator = random.Random(7301)
        for index in range(3):
            self.add(f'models/{index}.bin', generator.randbytes(1_400_000))
        session = self.session()
        self.restore(session)
        self.assertEqual([len(call) for call in session.backend.calls], [2, 1])
        for call in session.backend.calls:
            self.assertLessEqual(sum(len(self.blobs[digest][0]) for digest in call), 4 * 1024 * 1024)
        self.assert_restored()

    def test_one_oversized_object_gets_its_own_batch(self):
        generator = random.Random(7302)
        self.add('models/large.bin', generator.randbytes(4 * 1024 * 1024 + 1))
        self.add('api_football/small.json', b'small')
        session = self.session()
        self.restore(session)
        self.assertEqual([len(call) for call in session.backend.calls], [1, 1])
        self.assert_restored()

    def test_duplicate_files_are_downloaded_once_and_restored_to_each_path(self):
        digest = self.add('api_football/a.json', b'identical remote content')
        self.add('api_football/b.json', b'identical remote content')
        self.add('models/c.bin', b'identical remote content')
        session = self.session()
        self.restore(session)
        self.assertEqual(session.backend.calls, [[digest]])
        self.assert_restored()

    def test_single_object_fallback_also_deduplicates(self):
        digest = self.add('api_football/a.json', b'same')
        self.add('api_football/b.json', b'same')
        session = self.session(SingleBackend)
        self.restore(session)
        self.assertEqual(session.backend.calls, [digest])
        self.assert_restored()

    def test_unchanged_local_files_are_excluded_before_bulk_read(self):
        self.add('api_football/same.json', b'cached')
        changed = self.add('api_football/changed.json', b'remote')
        self.write('api_football/same.json', b'cached')
        self.write('api_football/changed.json', b'old')
        session = self.session()
        self.restore(session)
        self.assertEqual(session.backend.calls, [[changed]])
        self.restore(session)
        self.assertEqual(session.backend.calls, [[changed]])
        self.assert_restored()

    def failure_fixture(self):
        self.add('api_football/first.json', b'remote first')
        self.add('api_football/later.json', b'remote later')
        self.write('api_football/first.json', b'original first')
        self.write('api_football/later.json', b'original later')
        self.write('api_football/local-extra.json', b'original extra')
        return self.session()

    def assert_originals_untouched(self):
        self.assertEqual((self.root / 'api_football/first.json').read_bytes(), b'original first')
        self.assertEqual((self.root / 'api_football/later.json').read_bytes(), b'original later')
        self.assertEqual((self.root / 'api_football/local-extra.json').read_bytes(), b'original extra')
        self.assertFalse((self.root / '.cloud-state.json').exists())

    def test_corrupt_later_batch_does_not_replace_or_delete_any_original(self):
        session = self.failure_fixture()
        def corrupt_later(contents, batch):
            if batch == 2:
                digest = next(iter(contents))
                contents[digest] = b'corrupt', contents[digest][1]
            return contents
        session.backend.transform = corrupt_later
        with patch.object(storage, 'RESTORE_BATCH_FILES', 1):
            with self.assertRaises(storage.CloudStorageError):
                self.restore(session)
        self.assertEqual(len(session.backend.calls), 2)
        self.assert_originals_untouched()

    def test_missing_later_batch_does_not_replace_or_delete_any_original(self):
        session = self.failure_fixture()
        session.backend.transform = lambda contents, batch: {} if batch == 2 else contents
        with patch.object(storage, 'RESTORE_BATCH_FILES', 1):
            with self.assertRaisesRegex(storage.CloudStorageError, 'incomplets'):
                self.restore(session)
        self.assertEqual(len(session.backend.calls), 2)
        self.assert_originals_untouched()

    def test_unexpected_object_in_bulk_response_fails_closed(self):
        session = self.failure_fixture()
        session.backend.transform = lambda contents, _: dict(contents, unexpected=(b'extra', 5))
        with self.assertRaisesRegex(storage.CloudStorageError, 'inattendus'):
            self.restore(session)
        self.assert_originals_untouched()

    def test_lease_lost_between_batches_stops_reads_and_preserves_originals(self):
        session = self.failure_fixture()
        session.fail_check = 3  # Before the second batch, after validating the first.
        with patch.object(storage, 'RESTORE_BATCH_FILES', 1):
            with self.assertRaisesRegex(storage.CloudStorageError, 'Lease lost'):
                self.restore(session)
        self.assertEqual(len(session.backend.calls), 1)
        self.assert_originals_untouched()

    def test_lease_lost_while_downloading_stops_before_mutation(self):
        session = self.failure_fixture()
        session.fail_check = 2  # Immediately after the bulk read returns.
        with self.assertRaisesRegex(storage.CloudStorageError, 'Lease lost'):
            self.restore(session)
        self.assert_originals_untouched()

    def test_inconsistent_duplicate_metadata_rejected_before_download(self):
        self.add('api_football/a.json', b'same')
        self.add('api_football/b.json', b'same')
        self.entries['api_football/b.json']['size'] += 1
        session = self.session()
        with self.assertRaisesRegex(storage.CloudStorageError, 'incohérentes'):
            self.restore(session)
        self.assertEqual(session.backend.calls, [])
        self.assertFalse((self.root / 'api_football/a.json').exists())

    def test_duplicate_sqlite_destination_still_requires_integrity_check(self):
        self.add('api_football/plain.json', b'not a sqlite database')
        self.add('api_football/journal.sqlite3', b'not a sqlite database')
        session = self.session()
        with self.assertRaisesRegex(storage.CloudStorageError, 'journal distant'):
            self.restore(session)
        self.assertFalse((self.root / 'api_football/plain.json').exists())
        self.assertFalse((self.root / 'api_football/journal.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
