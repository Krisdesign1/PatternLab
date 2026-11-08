"""Temporary-SQLite integration checks; no production data, provider or model."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import fcntl
import json
import os
import sqlite3
import unittest

import context_archive_sync as sync
import context_observation_store as store


SOURCE_UUID = 'a' * 32


def raw_fixture(mid=99, *, context=True):
    raw = dict(fixture=dict(id=mid, date='2026-11-07T18:00:00Z', status=dict(short='NS')),
               league=dict(id=39), teams=dict(home=dict(id=1), away=dict(id=2)))
    if context:
        raw['injuries'] = [dict(fixture=dict(id=mid), team=dict(id=1),
            player=dict(id=123, type='Missing Fixture', reason='Knee Injury'))]
    return raw


def record(raw, *, at='2026-11-07T17:00:00Z', previous=None, baseline=None, **extra):
    value = dict(version=sync.RECORD_VERSION, match_id=str(raw['fixture']['id']),
        observed_at_utc=at, time_kind='local_import_recorded', previous_observation=previous,
        received_raw=deepcopy(raw), received_raw_sha256=sync._digest(raw),
        selected_raw_sha256=sync._digest(raw), selected_quality=None, decision='received', reason=None)
    if baseline is not None:
        value.update(baseline_raw=deepcopy(baseline), baseline_raw_sha256=sync._digest(baseline),
                     baseline_time_kind='recovered_at_first_tracked_import')
    value.update(extra)
    value['id'] = sync._digest(value)
    return value


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.destination = self.root/'fixtures.sqlite3', self.root/'context.sqlite3'
        with sqlite3.connect(self.source) as connection:
            connection.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            connection.executemany('INSERT INTO metadata VALUES(?,?)', [('store_uuid', SOURCE_UUID), ('revision', '1')])
            connection.execute('''CREATE TABLE fixture_import_revisions(sequence INTEGER PRIMARY KEY,
                match_id TEXT,observed_at_utc TEXT,payload TEXT,digest TEXT)''')

    def add(self, value, *, indexed_mid=None, indexed_at=None, indexed_digest=None):
        with sqlite3.connect(self.source) as connection:
            connection.execute('''INSERT INTO fixture_import_revisions
                (match_id,observed_at_utc,payload,digest) VALUES(?,?,?,?)''',
                (indexed_mid or value['match_id'], indexed_at or value['observed_at_utc'],
                 sync._canonical(value), indexed_digest or value['id']))
            connection.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def latest(self, kind='injuries', before='2026-11-07T17:59:00Z', **kwargs):
        return store.latest(self.destination, fixture_id='99', kind=kind, before=before, **kwargs)

    def test_batches_resume_replay_and_readonly_source_hash(self):
        previous = None
        for minute in range(4):
            value = record(raw_fixture(), at=f'2026-11-07T17:0{minute}:00Z', previous=previous)
            self.add(value); previous = value['id']
        before = sha256(self.source.read_bytes()).hexdigest()
        first = sync.sync_archive(self.source, self.destination, batch_size=2, max_batches=1)
        self.assertFalse(first['complete']); self.assertEqual(first['processed_records'], 2)
        second = sync.sync_archive(self.source, self.destination, batch_size=2)
        self.assertTrue(second['complete']); self.assertEqual(second['total_processed_records'], 4)
        replay = sync.sync_archive(self.source, self.destination)
        self.assertEqual(replay['processed_records'], 0)
        self.assertEqual(replay['total_receipts'], 4)
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), before)

    def test_interrupted_callback_runs_only_after_committed_checkpoint(self):
        previous = None
        for minute in range(3):
            value = record(raw_fixture(), at=f'2026-11-07T17:0{minute}:00Z', previous=previous)
            self.add(value); previous = value['id']
        observed = []
        def interrupt(summary):
            observed.append(summary['checkpoint']['sequence'])
            raise RuntimeError('simulated scheduling interruption')
        with self.assertRaises(RuntimeError):
            sync.sync_archive(self.source, self.destination, batch_size=1, progress=interrupt)
        self.assertEqual(observed, [1])
        resumed = sync.sync_archive(self.source, self.destination)
        self.assertEqual(resumed['processed_records'], 2)
        self.assertEqual(resumed['total_receipts'], 3)

    def test_destination_failure_rolls_back_whole_batch_and_checkpoint(self):
        first = record(raw_fixture()); self.add(first)
        self.add(record(raw_fixture(), at='2026-11-07T17:01:00Z', previous=first['id']))
        original = store.append_observation
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(kwargs['sequence'])
            if kwargs['sequence'] == 2: raise RuntimeError('simulated write failure')
            return original(*args, **kwargs)
        with patch.object(store, 'append_observation', side_effect=fail_second):
            with self.assertRaises(RuntimeError): sync.sync_archive(self.source, self.destination)
        connection = store.connect(self.destination, readonly=True)
        try:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM context_receipts').fetchone()[0], 0)
            self.assertIsNone(store.get_checkpoint(connection, SOURCE_UUID))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sync_fixture_chains').fetchone()[0], 0)
        finally: connection.close()
        self.assertEqual(sync.sync_archive(self.source, self.destination)['total_receipts'], 2)

    def test_source_uuid_replacement_and_journal_rollback_refused(self):
        self.add(record(raw_fixture())); sync.sync_archive(self.source, self.destination)
        with sqlite3.connect(self.source) as c:
            c.execute("UPDATE metadata SET value=? WHERE key='store_uuid'", ('b'*32,))
        with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)
        with sqlite3.connect(self.source) as c:
            c.execute("UPDATE metadata SET value=? WHERE key='store_uuid'", (SOURCE_UUID,))
            c.execute('DELETE FROM fixture_import_revisions')
        with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)

    def test_checkpoint_index_tamper_and_earlier_deletion_refused(self):
        first = record(raw_fixture()); self.add(first)
        self.add(record(raw_fixture(), at='2026-11-07T17:01:00Z', previous=first['id']))
        sync.sync_archive(self.source, self.destination)
        with sqlite3.connect(self.source) as c:
            c.execute("UPDATE fixture_import_revisions SET observed_at_utc='2026-11-07T17:02:00Z' WHERE sequence=2")
        with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)
        with sqlite3.connect(self.source) as c:
            c.execute("UPDATE fixture_import_revisions SET observed_at_utc='2026-11-07T17:01:00Z' WHERE sequence=2")
            c.execute('DELETE FROM fixture_import_revisions WHERE sequence=1')
        with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)

    def test_checksum_invalid_context_is_preserved_as_invalidation(self):
        value = record(raw_fixture()); value['received_raw']['injuries'] = []
        self.add(value)
        result = sync.sync_archive(self.source, self.destination)
        self.assertIn('record_checksum_invalid', result['error_counts'])
        self.assertIn('received_checksum_invalid', result['error_counts'])
        self.assertEqual(result['total_observations'], 3)
        self.assertEqual(self.latest()['state'], 'invalid')

    def test_bad_chain_and_time_regression_are_not_dropped(self):
        first = record(raw_fixture()); self.add(first)
        self.add(record(raw_fixture(context=False), at='2026-11-07T16:59:00Z', previous='f'*64))
        result = sync.sync_archive(self.source, self.destination)
        self.assertIn('chain_predecessor_mismatch', result['error_counts'])
        self.assertIn('chain_time_regression', result['error_counts'])
        self.assertEqual(result['total_receipts'], 2)
        self.assertEqual(self.latest()['state'], 'invalid')

    def test_sparse_payload_preserves_rich_empty_and_null_mask(self):
        first = record(raw_fixture()); self.add(first)
        sparse = record(raw_fixture(context=False), at='2026-11-07T17:01:00Z', previous=first['id']); self.add(sparse)
        sync.sync_archive(self.source, self.destination)
        self.assertEqual(self.latest()['state'], 'present')
        empty = raw_fixture(); empty['injuries'] = []
        third = record(empty, at='2026-11-07T17:02:00Z', previous=sparse['id']); self.add(third)
        sync.sync_archive(self.source, self.destination)
        self.assertEqual(self.latest()['state'], 'empty')
        null = raw_fixture(); null['injuries'] = None
        self.add(record(null, at='2026-11-07T17:03:00Z', previous=third['id']))
        sync.sync_archive(self.source, self.destination)
        self.assertEqual(self.latest()['state'], 'null')

    def test_recovered_baseline_has_first_receipt_time_and_incoming_precedence(self):
        baseline = raw_fixture()
        first = record(raw_fixture(context=False), baseline=baseline); self.add(first)
        result = sync.sync_archive(self.source, self.destination)
        self.assertEqual(result['total_receipts'], 2)
        self.assertEqual(self.latest()['state'], 'unknown')
        self.assertEqual(self.latest(include_recovered=True)['state'], 'present')
        self.assertEqual(self.latest(before='2026-11-07T16:59:00Z', include_recovered=True)['state'], 'unknown')
        connection = store.connect(self.destination, readonly=True)
        try:
            times = [tuple(r) for r in connection.execute('SELECT DISTINCT observed_at,provenance FROM context_receipts')]
            self.assertEqual({r[0] for r in times}, {'2026-11-07T17:00:00.000000+00:00'})
        finally: connection.close()

    def test_quarantine_and_rejected_incoming_invalidate_even_without_blocks(self):
        first = record(raw_fixture()); self.add(first)
        second = record(raw_fixture(context=False), at='2026-11-07T17:01:00Z', previous=first['id'],
            selected_quality=dict(quarantined=True), selected_raw_sha256='f'*64)
        self.add(second)
        result = sync.sync_archive(self.source, self.destination)
        self.assertIn('quarantined_observation', result['error_counts'])
        self.assertEqual(self.latest()['state'], 'invalid')

    def test_unknown_receipt_time_never_uses_current_time(self):
        self.add(record(raw_fixture(), at='2026-11-07T17:00:00'))
        result = sync.sync_archive(self.source, self.destination)
        self.assertIn('observation_time_unknown_or_naive', result['error_counts'])
        self.assertEqual(self.latest()['reason'], 'unknown_observation_time_for_fixture')
        connection = store.connect(self.destination, readonly=True)
        try: self.assertIsNone(connection.execute('SELECT observed_at FROM context_receipts').fetchone()[0])
        finally: connection.close()

    def test_hardlink_and_symlink_destinations_refused_without_source_write(self):
        self.add(record(raw_fixture()))
        original = sha256(self.source.read_bytes()).hexdigest()
        os.link(self.source, self.destination)
        with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)
        self.destination.unlink()
        self.destination.symlink_to(self.source)
        with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)
        self.assertEqual(sha256(self.source.read_bytes()).hexdigest(), original)

    def test_envelope_decision_and_quality_fail_closed_and_later_correction(self):
        for extra in (dict(decision='invented'), dict(decision='quarantined'),
                      dict(selected_quality=[]), dict(selected_quality=dict(quarantined=False))):
            with self.subTest(extra=extra):
                self.destination.unlink(missing_ok=True)
                with sqlite3.connect(self.source) as c: c.execute('DELETE FROM fixture_import_revisions')
                first = record(raw_fixture()); self.add(first)
                bad = record(raw_fixture(context=False), at='2026-11-07T17:01:00Z',
                             previous=first['id'], **extra); self.add(bad)
                sync.sync_archive(self.source, self.destination)
                self.assertEqual(self.latest()['state'], 'invalid')
                self.add(record(raw_fixture(), at='2026-11-07T17:02:00Z', previous=bad['id']))
                sync.sync_archive(self.source, self.destination)
                self.assertEqual(self.latest()['state'], 'present')

    def test_regressed_and_discordant_times_are_retained_undated(self):
        first = record(raw_fixture()); self.add(first)
        self.add(record(raw_fixture(context=False), at='2026-11-07T16:59:00Z', previous=first['id']))
        sync.sync_archive(self.source, self.destination)
        self.assertEqual(self.latest()['reason'], 'unknown_observation_time_for_fixture')
        with sqlite3.connect(self.destination) as c:
            row = c.execute('SELECT supplied_observed_at FROM sync_receipt_audit WHERE sequence=2').fetchone()
            self.assertEqual(json.loads(row[0]), '2026-11-07T16:59:00Z')
        # A distinct destination proves the index/payload disagreement alone
        # also invalidates an older value at every as-of cutoff.
        other = self.root/'discordant.sqlite3'
        with sqlite3.connect(self.source) as c: c.execute('DELETE FROM fixture_import_revisions WHERE sequence=2')
        self.add(record(raw_fixture(context=False), at='2026-11-07T17:01:00Z', previous=first['id']),
                 indexed_at='2026-11-08T17:01:00Z')
        sync.sync_archive(self.source, other)
        self.assertEqual(store.latest(other, fixture_id=99, kind='injuries',
            before='2026-11-07T17:59:00Z')['reason'], 'unknown_observation_time_for_fixture')

    def test_initial_highwater_not_extended_by_callback_and_lock_conflict(self):
        first = record(raw_fixture()); self.add(first)
        def append_after_commit(_):
            self.add(record(raw_fixture(), at='2026-11-07T17:01:00Z', previous=first['id']))
        result = sync.sync_archive(self.source, self.destination, batch_size=1, progress=append_after_commit)
        self.assertEqual(result['initial_source_highwater'], 1)
        self.assertEqual(result['processed_records'], 1)
        self.assertEqual(sync.sync_archive(self.source, self.destination)['processed_records'], 1)
        with Path(str(self.destination)+'.sync.lock').open('a') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(sync.SyncError): sync.sync_archive(self.source, self.destination)

    def test_progress_counts_match_committed_sql_without_rescanning_each_batch(self):
        previous = None
        for minute in range(4):
            value = record(raw_fixture(), at=f'2026-11-07T17:0{minute}:00Z', previous=previous)
            self.add(value); previous = value['id']
        statements=[]; original=store.connect; updates=[]
        def traced(*args,**kwargs):
            connection=original(*args,**kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        def progress(summary):
            with sqlite3.connect(self.destination) as c:
                count=c.execute('SELECT COUNT(*) FROM context_receipts').fetchone()[0]
            self.assertEqual(summary['total_receipts'],count)
            updates.append(count)
        with patch.object(store,'connect',side_effect=traced):
            sync.sync_archive(self.source,self.destination,batch_size=1,progress=progress)
        self.assertEqual(updates,[1,2,3,4])
        scans=[s for s in statements if 'SELECT reason,COUNT(*) FROM sync_receipt_audit' in s]
        self.assertEqual(len(scans),2)  # initial state and final independent recount


if __name__ == '__main__':
    unittest.main()
