"""Readers use isolated committed files while writers retain their lease."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import unittest
from unittest.mock import patch
import zlib

import cloud_storage as storage
import cloud_ui
import test_cloud_storage as fixtures


class CommittedSnapshotTests(unittest.TestCase):
    setUp = fixtures.CloudStorageTests.setUp
    write = fixtures.CloudStorageTests.write
    seed = fixtures.CloudStorageTests.seed

    def test_head_probe_does_not_download_files_or_acquire_a_lease(self):
        self.seed()
        @contextmanager
        def head_only():
            def forbidden(digests):
                raise AssertionError('Head probe must not download files')
            yield dict(self.backend.status(),read_blobs=forbidden)
        with patch.object(self.backend,'acquire',side_effect=AssertionError('No writer lease')), \
                patch.object(self.backend,'committed_view',head_only):
            self.assertEqual(storage.committed_revision(),1)
            self.assertEqual(storage.committed_revision(),1)
        self.assertEqual(self.backend.revision,1)

    def test_busy_writer_does_not_block_or_publish_reader_and_local_dirty_files_are_ignored(self):
        self.seed()
        self.backend.owner = 'active-writer'
        self.write('api_football/fixtures.json','{"unpublished":true}')
        before = deepcopy((self.backend.revision,self.backend.uploads,self.backend.publications))
        with patch.object(self.backend,'acquire',side_effect=AssertionError('Reader must not acquire a lease')):
            with storage.committed_snapshot() as snapshot:
                self.assertEqual((snapshot.root/'api_football/fixtures.json').read_text(),'{"fixtures":[]}')
                self.assertNotEqual(snapshot.root,self.root)
                self.assertEqual(snapshot.revision,1)
                self.assertEqual(self.backend.owner,'active-writer')
                temporary = snapshot.root
        self.assertFalse(temporary.exists())
        self.assertEqual((self.backend.revision,self.backend.uploads,self.backend.publications),before)
        self.assertEqual((self.root/'api_football/fixtures.json').read_text(),'{"unpublished":true}')

    def test_every_mutation_boundary_refuses_before_consumer_body_or_api(self):
        self.seed()
        called=[]
        @storage.persisted
        def mutate():
            called.append(True)
        with storage.committed_snapshot():
            for operation in (mutate, storage.assert_lease, storage.checkpoint, storage.seed):
                with self.subTest(operation=operation.__name__),self.assertRaises(storage.CloudStorageReadOnly):
                    operation()
            with self.assertRaises(storage.CloudStorageReadOnly):
                with storage.transaction():
                    self.fail('No writable nested transaction')
        self.assertEqual(called,[])
        with storage.transaction():
            mutate()
        self.assertEqual(called,[True])

    def test_reader_keeps_original_blobs_when_a_later_head_collects_them(self):
        self.seed()
        original = self.backend.committed_view
        @contextmanager
        def concurrent_publication():
            with original() as view:
                self.backend.blobs.clear()
                self.backend.manifest=dict(version=1,files={})
                self.backend.revision+=2
                yield view
        with patch.object(self.backend,'committed_view',concurrent_publication):
            with storage.committed_snapshot() as snapshot:
                self.assertEqual(snapshot.revision,1)
                self.assertEqual((snapshot.root/'api_football/fixtures.json').read_text(),'{"fixtures":[]}')

    def test_corruption_is_rejected_before_exposing_any_file(self):
        self.seed()
        digest=next(iter(self.backend.blobs))
        encoded,size=self.backend.blobs[digest]
        self.backend.blobs[digest]=zlib.compress(b'{"wrong":true}'),size
        with self.assertRaises(storage.CloudStorageError):
            with storage.committed_snapshot():
                self.fail('Corrupted snapshot must not be exposed')

    def test_sqlite_integrity_is_checked_and_file_filter_excludes_other_objects(self):
        self.write('api_football/fixtures.json','{"fixtures":[]}')
        self.write('models/unused.cbm','not a model read by this preview')
        self.seed()
        with storage.committed_snapshot(files=('api_football/fixtures.json',)) as snapshot:
            self.assertFalse((snapshot.root/'models/unused.cbm').exists())
        self.write('api_football/predictions.sqlite3','not a valid SQLite file')
        # Install a manifest with a correct digest for malformed SQLite bytes.
        raw=b'not a valid SQLite file'; encoded=zlib.compress(raw);digest=sha256(raw).hexdigest()
        self.backend.manifest['files']['api_football/predictions.sqlite3']=dict(
            sha256=digest,size=len(raw),compressed_size=len(encoded))
        self.backend.blobs[digest]=(encoded,len(raw))
        with self.assertRaises(storage.CloudStorageError):
            with storage.committed_snapshot():
                self.fail('Malformed SQLite must not be exposed')

    def test_preview_loads_only_explicit_snapshot_paths_without_mutating_ledger(self):
        self.write('api_football/fixtures.json','{"version":1,"fixtures":[]}')
        storage.seed()
        path=self.root/'api_football/predictions.sqlite3'
        payload=json.dumps(dict(size=2,fixtures=[],methods={}))
        with storage.transaction():
            with sqlite3.connect(path) as con:
                con.executescript('CREATE TABLE predictions(id TEXT,created_at TEXT,payload TEXT,digest TEXT);'
                                  'CREATE TABLE results(prediction_id TEXT,revision INTEGER,checked_at TEXT,payload TEXT);')
                con.execute('INSERT INTO predictions VALUES(?,?,?,?)',
                            ('saved','2026-10-23T10:00:00Z',payload,sha256(payload.encode()).hexdigest()))
        original=path.read_bytes()
        with storage.committed_snapshot() as snapshot:
            with patch('api_football.ApiFootballClient',side_effect=AssertionError('No API client')):
                preview=cloud_ui._read_preview(snapshot)
            self.assertEqual([entry['id'] for entry in preview['entries']],['saved'])
            self.assertEqual(preview['calendar'],[])
            self.assertEqual(preview['probabilities']['rows'],[])
        self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':
    unittest.main()
