"""Real SQLite checks for durable versions, strict time and atomic resumption."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from context_observation_store import (append_observation, connect, get_checkpoint,
                                       initialize, latest, latest_observation, update_checkpoint,
                                       canonical, digest)


def fixture(*, events=True, lineups=False):
    raw = dict(fixture=dict(id=99, date='2026-11-07T18:00:00Z', status=dict(short='FT')),
        league=dict(id=39), teams=dict(home=dict(id=1), away=dict(id=2)),
        score=dict(halftime=dict(home=1, away=0), fulltime=dict(home=1, away=0)))
    if events:
        raw['events'] = [dict(time=dict(elapsed=10, extra=None), team=dict(id=1),
            player=dict(id=101), assist=dict(id=None), type='Goal', detail='Normal Goal', comments=None)]
    if lineups:
        raw['lineups'] = [dict(team=dict(id=team), coach=dict(id=team + 50),
            startXI=[dict(player=dict(id=100 * team + i)) for i in range(11)], substitutes=[])
            for team in (1, 2)]
    return raw


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'context.sqlite3'
        initialize(self.path)
        self.connection = connect(self.path)

    def tearDown(self):
        self.connection.close()
        self.folder.cleanup()

    def append(self, sequence, raw, at=None, **kwargs):
        return append_observation(self.connection, source_uuid='source-A', sequence=sequence,
            record_id='record-' + str(sequence), observed_at=at or '2026-11-07T17:00:00Z',
            raw=raw, **kwargs)

    def query(self, before='2026-11-07T18:00:00Z', kind='events', **kwargs):
        return latest_observation(self.connection, fixture_id='99', kind=kind, before=before, **kwargs)

    def test_exact_replay_is_idempotent_and_changed_key_is_rejected(self):
        with self.connection:
            first = self.append(1, fixture())
            replay = self.append(1, fixture())
        self.assertTrue(first['inserted'])
        self.assertFalse(replay['inserted'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM context_observations').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'collision'):
            self.append(1, fixture(events=False))

    def test_omission_preserves_component_but_empty_and_null_mask_it(self):
        with self.connection:
            self.append(1, fixture(), at='2026-11-07T17:00:00Z')
            omission = self.append(2, fixture(events=False), at='2026-11-07T17:10:00Z')
        self.assertEqual(omission['observations_inserted'], 0)
        self.assertEqual(self.query()['state'], 'present')
        empty = fixture();empty['events'] = []
        with self.connection:
            self.append(3, empty, at='2026-11-07T17:20:00Z')
        self.assertEqual(self.query()['state'], 'empty')
        self.assertFalse(self.query()['usable'])
        null = fixture();null['events'] = None
        with self.connection:
            self.append(4, null, at='2026-11-07T17:30:00Z')
        self.assertEqual(self.query()['state'], 'null')
        self.assertEqual(self.query(before='2026-11-07T17:15:00Z')['state'], 'present')

    def test_strict_time_and_future_invalidations_do_not_rewrite_past(self):
        with self.connection:
            self.append(1, fixture(), at='2026-11-07T17:00:00Z')
            self.append(2, fixture(events=False), at='2026-11-07T17:40:00Z', validation_error='bad_chain')
        self.assertEqual(self.query(before='2026-11-07T17:00:00Z')['state'], 'unknown')
        self.assertEqual(self.query(before='2026-11-07T17:30:00Z')['state'], 'present')
        self.assertEqual(self.query()['state'], 'invalid')

    def test_quarantine_without_any_block_invalidates_every_kind(self):
        with self.connection:
            self.append(1, fixture(lineups=True), at='2026-11-07T17:00:00Z')
            bad = fixture(events=False);bad['_patternlab_import'] = dict(quarantined=True)
            result = self.append(2, bad, at='2026-11-07T17:10:00Z')
        self.assertEqual(result['observations_inserted'], 3)
        for kind in ('events', 'lineups', 'injuries'):
            self.assertEqual(self.query(kind=kind)['state'], 'invalid')

    def test_unknown_time_is_retained_and_fails_closed_without_fake_time(self):
        with self.connection:
            self.append(1, fixture())
            append_observation(self.connection, source_uuid='source-A', sequence=2, record_id='bad',
                               observed_at='2026-11-07T17:10:00', raw=fixture(events=False))
        self.assertIsNone(self.connection.execute('SELECT observed_at FROM context_receipts WHERE sequence=2').fetchone()[0])
        self.assertEqual(self.query(before='2026-11-07T16:00:00Z')['reason'], 'unknown_observation_time_for_fixture')

    def test_recovered_baseline_is_distinct_and_never_backdated(self):
        with self.connection:
            self.append(1, fixture(), provenance='recovered_at_first_tracked_import', receipt_variant='recovered_baseline')
            self.append(1, fixture(events=False))
        self.assertEqual(self.query()['state'], 'unknown')
        recovered = self.query(include_recovered=True)
        self.assertEqual(recovered['state'], 'present')
        self.assertFalse(recovered['usable'])
        self.assertEqual(self.query(before='2026-11-07T17:00:00Z', include_recovered=True)['state'], 'unknown')
        with self.connection:
            raw = fixture();raw['events'] = []
            self.append(2, raw, provenance='recovered_at_first_tracked_import', receipt_variant='recovered_baseline')
            self.append(2, fixture())
        self.assertEqual(self.query(include_recovered=True)['state'], 'present')

    def test_append_and_checkpoint_rollback_and_resume_together(self):
        with self.assertRaises(RuntimeError):
            with self.connection:
                self.append(1, fixture())
                update_checkpoint(self.connection, 'source-A', 1, 'envelope-1')
                raise RuntimeError('simulate interruption')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM context_receipts').fetchone()[0], 0)
        self.assertIsNone(get_checkpoint(self.connection, 'source-A'))
        with self.connection:
            self.append(1, fixture())
            update_checkpoint(self.connection, 'source-A', 1, 'envelope-1')
        self.assertEqual(get_checkpoint(self.connection, 'source-A')['sequence'], 1)
        with self.assertRaises(ValueError):
            update_checkpoint(self.connection, 'source-A', 0, 'earlier')
        with self.assertRaises(ValueError):
            update_checkpoint(self.connection, 'source-A', 1, 'different')

    def test_append_only_triggers_and_readonly_no_create(self):
        with self.connection:
            self.append(1, fixture())
        for table in ('context_receipts', 'context_observations'):
            for statement in ('DELETE FROM ', 'UPDATE '):
                sql = statement + table + (' SET fixture_id=fixture_id' if statement.startswith('UPDATE') else '')
                with self.assertRaises(sqlite3.IntegrityError):
                    self.connection.execute(sql)
        read = connect(self.path, readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                read.execute("INSERT INTO context_checkpoints VALUES('x',1,'x')")
        finally:
            read.close()
        missing = Path(self.folder.name) / 'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):
            latest(missing, fixture_id=99, kind='events', before='2026-11-07T18:00:00Z')
        self.assertFalse(missing.exists())
        other = Path(self.folder.name) / 'source.sqlite3'
        source = sqlite3.connect(other)
        source.execute('CREATE TABLE fixtures(id INTEGER)');source.commit();source.close()
        before = other.read_bytes()
        with self.assertRaisesRegex(ValueError, 'non-context'):
            initialize(other)
        self.assertEqual(other.read_bytes(), before)

    def test_reader_rejects_integrity_corruption_even_if_trigger_was_removed(self):
        with self.connection:
            self.append(1, fixture())
        # Simulate external corruption, not an archive API mutation.
        self.connection.execute('DROP TRIGGER context_observations_no_update')
        with self.connection:
            self.connection.execute("UPDATE context_observations SET quality_sha256='corrupt'")
        self.assertEqual(self.query()['reason'], 'immutable_observation_integrity_mismatch')
        self.assertFalse(self.query()['usable'])

    def test_reader_binds_observation_temporal_and_identity_indexes_to_receipt(self):
        with self.connection:
            self.append(1, fixture(), at='2026-11-07T19:00:00Z')
        self.assertEqual(self.query()['state'], 'unknown')
        self.connection.execute('DROP TRIGGER context_observations_no_update')
        with self.connection:
            self.connection.execute("UPDATE context_observations SET observed_at='2026-11-07T17:00:00.000000+00:00'")
        self.assertEqual(self.query()['reason'], 'immutable_observation_integrity_mismatch')
        self.assertFalse(self.query()['usable'])
        # Other order/provenance indexes must not bypass the same check.
        original=dict(self.connection.execute('SELECT * FROM context_observations').fetchone())
        original['observed_at']='2026-11-07T19:00:00.000000+00:00'
        for column, value in (('source_uuid', 'other-source'), ('sequence', 77),
                ('receipt_variant', 'recovered_baseline'), ('provenance', 'recovered_at_first_tracked_import'),
                ('metadata_json', '{}')):
            with self.subTest(column=column):
                with self.connection:
                    self.connection.execute('''UPDATE context_observations SET
                        source_uuid=?,sequence=?,receipt_variant=?,observed_at=?,provenance=?,metadata_json=?''',
                        tuple(original[key] for key in ('source_uuid','sequence','receipt_variant','observed_at','provenance','metadata_json')))
                    self.connection.execute('UPDATE context_observations SET '+column+'=?', (value,))
                self.assertEqual(self.query(before='2026-11-07T20:00:00Z',include_recovered=True)['reason'], 'immutable_observation_integrity_mismatch')

    def test_receipt_hash_rejects_simultaneously_altered_observation_and_receipt_times(self):
        with self.connection:
            self.append(1, fixture(), at='2026-11-07T19:00:00Z')
        for table in ('context_observations', 'context_receipts'):
            self.connection.execute('DROP TRIGGER '+table+'_no_update')
        with self.connection:
            for table in ('context_observations', 'context_receipts'):
                self.connection.execute("UPDATE "+table+" SET observed_at='2026-11-07T17:00:00.000000+00:00'")
        self.assertEqual(self.query()['reason'], 'immutable_observation_integrity_mismatch')
        # Changing both indexes and the canonical body without its hash also fails.
        body=json.loads(self.connection.execute('SELECT canonical_receipt_json FROM context_receipts').fetchone()[0])
        body['observed_at']=body['supplied_observed_at']='2026-11-07T17:00:00.000000+00:00'
        with self.connection:
            self.connection.execute('UPDATE context_receipts SET canonical_receipt_json=?', (canonical(body),))
        self.assertFalse(self.query()['usable'])

    def test_new_receipts_normalize_legitimate_timezone_and_replay_equivalent_instants(self):
        local=datetime(2026,11,7,19,tzinfo=timezone(timedelta(hours=2)))
        with self.connection:
            self.append(1, fixture(), at=local)
            replay=self.append(1, fixture(), at='2026-11-07T17:00:00Z')
        self.assertFalse(replay['inserted'])
        self.assertTrue(self.query()['usable'])
        body=json.loads(self.connection.execute('SELECT canonical_receipt_json FROM context_receipts').fetchone()[0])
        self.assertEqual(body['observed_at'],'2026-11-07T17:00:00.000000+00:00')
        self.assertEqual(body['supplied_observed_at'],body['observed_at'])
        self.assertFalse(self.query()['model_training_ready'])

    def make_legacy_receipt(self, supplied_time):
        """Construct the exact old, unhashed-body storage contract in isolation."""
        receipt=dict(self.connection.execute('SELECT * FROM context_receipts').fetchone())
        body=json.loads(receipt['canonical_receipt_json'])
        body['supplied_observed_at']=supplied_time
        old_hash=digest(body)
        for table in ('context_observations','context_receipts'):
            self.connection.execute('DROP TRIGGER IF EXISTS '+table+'_no_update')
        with self.connection:
            self.connection.execute('UPDATE context_receipts SET canonical_receipt_json=NULL,receipt_sha256=?',(old_hash,))
            for row in self.connection.execute('SELECT * FROM context_observations').fetchall():
                payload=dict(receipt_sha256=old_hash,fixture_id=row['fixture_id'],kind=row['kind'],
                    state=row['state'],availability=row['availability'],payload=json.loads(row['payload_json']),
                    metadata=json.loads(row['metadata_json']),quality=json.loads(row['quality_json']))
                self.connection.execute('UPDATE context_observations SET observation_sha256=? WHERE id=?',
                                        (digest(payload),row['id']))

    def test_legacy_utc_receipts_read_and_replay_without_rewriting_old_rows(self):
        with self.connection:self.append(1,fixture())
        for supplied in ('2026-11-07T17:00:00+00:00','2026-11-07T17:00:00.000000+00:00',
                         '2026-11-07T17:00:00Z','2026-11-07T17:00:00.000000Z'):
            with self.subTest(supplied=supplied):
                # Restore its new canonical body only inside this synthetic setup.
                row=self.connection.execute('SELECT * FROM context_receipts').fetchone()
                if row['canonical_receipt_json'] is None:
                    body=dict(source_uuid=row['source_uuid'],sequence=row['sequence'],receipt_variant=row['receipt_variant'],
                        record_id=row['record_id'],fixture_id=row['fixture_id'],observed_at=row['observed_at'],
                        supplied_observed_at=row['observed_at'],provenance=row['provenance'],raw_sha256=row['raw_sha256'],
                        metadata=json.loads(row['metadata_json']),validation_error=row['validation_error'])
                    with self.connection:self.connection.execute('UPDATE context_receipts SET canonical_receipt_json=?',(canonical(body),))
                self.make_legacy_receipt(supplied)
                before=[tuple(r) for r in self.connection.execute('SELECT * FROM context_receipts')]
                self.assertTrue(self.query()['usable'])
                with self.connection:replay=self.append(1,fixture(),at='2026-11-07T17:00:00Z')
                self.assertFalse(replay['inserted'])
                self.assertEqual(before,[tuple(r) for r in self.connection.execute('SELECT * FROM context_receipts')])
                with self.assertRaises(ValueError):self.append(1,fixture(events=False))

    def test_legacy_schema_read_only_and_additive_initialization_do_not_backfill(self):
        with self.connection:self.append(1,fixture())
        self.make_legacy_receipt('2026-11-07T17:00:00+00:00')
        with self.connection:self.connection.execute('ALTER TABLE context_receipts DROP COLUMN canonical_receipt_json')
        self.assertTrue(latest(self.path,fixture_id=99,kind='events',before='2026-11-07T18:00:00Z')['usable'])
        old_hash=self.connection.execute('SELECT receipt_sha256 FROM context_receipts').fetchone()[0]
        initialize(self.path)
        row=self.connection.execute('SELECT receipt_sha256,canonical_receipt_json FROM context_receipts').fetchone()
        self.assertEqual(row['receipt_sha256'],old_hash)
        self.assertIsNone(row['canonical_receipt_json'])
        self.assertTrue(self.query()['usable'])

    def test_legacy_unknown_time_format_is_not_invented_by_reader(self):
        with self.connection:self.append(1,fixture())
        self.make_legacy_receipt('2026-11-07T19:00:00+02:00')
        self.assertFalse(self.query()['usable'])
        self.assertEqual(self.query()['reason'],'immutable_observation_integrity_mismatch')
        # The original exact supplied value can still prove an idempotent replay.
        with self.connection:replay=self.append(1,fixture(),at='2026-11-07T19:00:00+02:00')
        self.assertFalse(replay['inserted'])

    def test_quality_and_identity_are_immutable_and_caller_scope_is_explicit(self):
        raw = fixture();before = deepcopy(raw)
        with self.connection:
            self.append(1, raw)
        result = self.query()
        self.assertEqual(raw, before)
        self.assertTrue(result['usable'])
        self.assertFalse(result['model_training_ready'])
        self.assertIn('code_sha256', result['observation']['quality']['validator'])
        self.assertEqual(self.query(expected_identity={'league_id': '999'})['reason'], 'caller_identity_scope_mismatch')
        bad = fixture();bad['fixture']['id'] = 100
        with self.connection:
            self.append(2, bad, fixture_id=99, at='2026-11-07T17:10:00Z')
        self.assertEqual(self.query()['state'], 'invalid')
        with self.assertRaises(ValueError):
            self.query(before='2026-11-07T17:00:00')


if __name__ == '__main__':
    unittest.main()
