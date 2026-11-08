"""Immutable confirmation storage and post-commit timing, without API calls."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import causal_confirmation_store as store
from causal_feature_workspace import digest
import prematch_feature_snapshot as snapshots
from test_causal_partitions import synthetic_bundle


def seal(value):
    return dict(value, id=digest(value))


def payload(protocol_id, snapshot):
    return seal(dict(protocol_id=protocol_id, fixture=deepcopy(snapshot['fixture']),
                     snapshot=deepcopy(snapshot), methods={'candidate': {'1st': .3, 'Égal': .2, '2nd': .5}}))


class Clock(datetime):
    values = []

    @classmethod
    def now(cls, tz=None):
        value = cls.values.pop(0)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class ConfirmationStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'data'
        configured = patch.object(store.cloud_storage, 'configured', return_value=False)
        configured.start(); self.addCleanup(configured.stop)
        network = patch.object(store.api.ApiFootballClient, 'get', side_effect=AssertionError('No API'))
        self.network = network.start(); self.addCleanup(network.stop)
        self.bundle = synthetic_bundle()
        self.snapshot = next(iter(self.bundle['snapshots'].values()))
        self.protocol = seal(dict(version='test-protocol', registered_at='2026-01-01T00:00:00Z'))

    def enroll(self):
        return store.put_protocol(self.protocol, root=self.root)

    def before(self):
        return datetime.fromisoformat(self.snapshot['fixture']['timestamp']).replace(tzinfo=timezone.utc).replace(hour=15)

    def save(self, value=None):
        self.enroll()
        return store.put_forecast(value or payload(self.protocol['id'], self.snapshot), root=self.root,
                                  clock=lambda: self.before())

    def db(self):
        return sqlite3.connect(store.database_path(self.root))

    def test_missing_readers_never_create_a_directory_or_database(self):
        self.assertEqual(store.protocols(self.root), [])
        self.assertIsNone(store.anchor(self.protocol['id'], '2026-01-10', root=self.root))
        self.assertEqual(store.attempts(self.protocol['id'], root=self.root), [])
        self.assertEqual(store.forecasts(self.root), [])
        self.assertEqual(store.heads(self.root), [])
        self.assertEqual(store.results('a'*64, self.root), [])
        self.assertFalse(self.root.exists()); self.network.assert_not_called()

    def test_protocol_anchor_and_attempt_are_exact_idempotent_and_immutable(self):
        self.assertEqual(self.enroll(), self.protocol); self.assertEqual(self.enroll(), self.protocol)
        self.assertEqual(store.protocols(self.root), [self.protocol])
        anchor = seal(dict(day='2026-01-10', targets=['900001']))
        self.assertEqual(store.anchor(self.protocol['id'], '2026-01-10', anchor, self.root), anchor)
        self.assertEqual(store.anchor(self.protocol['id'], '2026-01-10', root=self.root), anchor)
        with self.assertRaises(ValueError):
            store.anchor(self.protocol['id'], '2026-01-10', seal(dict(day='2026-01-10', targets=[])), self.root)
        attempt = seal(dict(kind='capture_missed', observed_at_utc='2026-01-10T18:00:00Z'))
        self.assertEqual(store.attempts(self.protocol['id'], attempt, self.root), [attempt])
        self.assertEqual(store.attempts(self.protocol['id'], attempt, self.root), [attempt])
        self.assertEqual(store.attempts(self.protocol['id'], root=self.root), [attempt])
        with self.db() as con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute('UPDATE protocols SET payload=?', ('{}',))
            with self.assertRaises(sqlite3.IntegrityError): con.execute('DELETE FROM anchors')

    def test_forecast_roundtrip_and_first_forecast_per_fixture_is_preserved(self):
        first = self.save()
        self.assertFalse(first['existing']); self.assertTrue(first['ack']['before_kickoff'])
        self.assertFalse(first['ack']['admissible'])
        self.assertEqual(first['ack']['time_quality'], 'synthetic_unattested')
        self.assertEqual(store.forecasts(self.root), [dict(payload=first['payload'], ack=first['ack'])])
        changed = deepcopy(first['payload']); changed.pop('id'); changed['methods']['candidate']['1st'] = .4
        changed['methods']['candidate']['2nd'] = .4
        second = store.put_forecast(seal(changed), root=self.root, clock=lambda: self.before())
        self.assertTrue(second['existing']); self.assertEqual(second['payload'], first['payload'])
        self.assertEqual(second['ack'], first['ack'])
        self.network.assert_not_called()

    def test_real_wall_clock_ack_can_be_admissible_and_late_ack_stays_saved(self):
        self.enroll()
        start = self.before(); before_ko = start.replace(hour=15, minute=59, second=59)
        after_ko = start.replace(hour=16, minute=0, second=1)
        Clock.values = [start, before_ko, after_ko]
        with patch.object(store, 'datetime', Clock):
            saved = store.put_forecast(payload(self.protocol['id'], self.snapshot), root=self.root)
        self.assertEqual(saved['ack']['time_quality'], 'local_commit_ack')
        self.assertFalse(saved['ack']['before_kickoff']); self.assertFalse(saved['ack']['admissible'])
        self.assertEqual(saved['ack']['reason'], 'commit_ack_after_kickoff')
        self.assertEqual(len(store.forecasts(self.root)), 1)
        # A different fixture with an actual clock receipt before kickoff is admitted.
        second = list(self.bundle['snapshots'].values())[1]
        start = datetime.fromisoformat(second['fixture']['timestamp']).replace(tzinfo=timezone.utc, hour=15)
        Clock.values = [start]*3
        with patch.object(store, 'datetime', Clock):
            saved = store.put_forecast(payload(self.protocol['id'], second), root=self.root)
        self.assertTrue(saved['ack']['admissible'])

    def test_clock_after_lock_or_inside_transaction_refuses_and_rolls_back(self):
        self.enroll()
        kickoff = self.before().replace(hour=16)
        for values in ([kickoff], [self.before(), kickoff]):
            with self.subTest(values=values):
                Clock.values = list(values)
                with patch.object(store, 'datetime', Clock), self.assertRaises(ValueError):
                    store.put_forecast(payload(self.protocol['id'], self.snapshot), root=self.root)
                self.assertEqual(store.forecasts(self.root), [])
                with self.db() as con:
                    self.assertEqual(con.execute('SELECT count(*) FROM contexts').fetchone()[0], 0)

    def test_missing_ack_after_committed_forecast_is_never_recovered_or_invented(self):
        self.enroll()
        with patch.object(store, '_write_ack', side_effect=OSError('Synthetic receipt failure')):
            with self.assertRaises(OSError):
                store.put_forecast(payload(self.protocol['id'], self.snapshot), root=self.root, clock=lambda: self.before())
        stored = store.forecasts(self.root)
        self.assertEqual(len(stored), 1); self.assertIsNone(stored[0]['ack'])
        never = lambda: (_ for _ in ()).throw(AssertionError('Existing capture does not get a new clock receipt'))
        saved = store.put_forecast(payload(self.protocol['id'], self.snapshot), root=self.root, clock=never)
        self.assertTrue(saved['existing']); self.assertIsNone(saved['ack'])

    def test_production_root_rejects_clock_injection_before_any_write(self):
        value = payload(self.protocol['id'], self.snapshot)
        with patch.object(store.api, 'DATA_DIR', self.root):
            with self.assertRaises(ValueError): store.put_forecast(value, root=self.root, clock=lambda: self.before())
        self.assertFalse(self.root.exists())

    def test_history_documents_and_context_are_deduplicated_and_fully_reconstructed(self):
        self.enroll()
        history = self.bundle['outcomes']['examples'][0]
        targets = list(self.bundle['snapshots'].values())[3:5]
        for target in targets:
            rebuilt = snapshots.build([history['outcome_row']], target['fixture'], decision_at=target['decision_at'],
                source_refs=[history['outcome_source']], target_ref=target['target_ref'], target_raw=target['target_observation'])
            stamp = datetime.fromisoformat(rebuilt['fixture']['timestamp']).replace(tzinfo=timezone.utc, hour=15)
            store.put_forecast(payload(self.protocol['id'], rebuilt), root=self.root, clock=lambda: stamp)
        with self.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM forecasts').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM contexts').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM sources').fetchone()[0], 2)
            compact = json.loads(con.execute('SELECT payload FROM forecasts LIMIT 1').fetchone()[0])
            self.assertNotIn('history_rows', compact['snapshot']); self.assertNotIn('source_refs', compact['snapshot'])
        values = store.forecasts(self.root)
        self.assertTrue(all(len(row['payload']['snapshot']['history_rows']) == 1 for row in values))

    def test_public_history_storage_uses_the_same_context_as_forecast_codec(self):
        self.enroll()
        history = self.bundle['outcomes']['examples'][0]; target = list(self.bundle['snapshots'].values())[3]
        rows, refs = [history['outcome_row']], [history['outcome_source']]
        context_id = store.put_history(rows, refs, root=self.root)
        self.assertEqual(store.get_history(context_id, root=self.root), dict(history_rows=rows, source_refs=refs))
        self.assertEqual(store.put_history(rows, refs, root=self.root), context_id)
        rebuilt = snapshots.build(rows, target['fixture'], decision_at=target['decision_at'],
            source_refs=refs, target_ref=target['target_ref'], target_raw=target['target_observation'])
        stamp = datetime.fromisoformat(rebuilt['fixture']['timestamp']).replace(tzinfo=timezone.utc, hour=15)
        store.put_forecast(payload(self.protocol['id'], rebuilt), root=self.root, clock=lambda: stamp)
        with self.db() as con:
            self.assertEqual(con.execute('SELECT context_id FROM forecasts').fetchone()[0], context_id)
            self.assertEqual(con.execute('SELECT count(*) FROM contexts').fetchone()[0], 1)
        changed = deepcopy(refs); changed[0]['normalized_sha256'] = 'a'*64
        with self.assertRaises(ValueError): store.put_history(rows, changed, root=self.root)
        with self.assertRaises(ValueError): store.get_history('b'*64, root=self.root)

    def test_corrupt_or_missing_context_is_rejected_by_reader(self):
        self.save()
        with self.db() as con:
            con.execute('DROP TRIGGER confirmation_contexts_no_update')
            con.execute('UPDATE contexts SET payload=?', ('{}',))
        with self.assertRaises(ValueError): store.forecasts(self.root)

    def test_missing_source_document_is_rejected(self):
        self.enroll()
        history = self.bundle['outcomes']['examples'][0]; target = list(self.bundle['snapshots'].values())[3]
        rebuilt = snapshots.build([history['outcome_row']], target['fixture'], decision_at=target['decision_at'],
            source_refs=[history['outcome_source']], target_ref=target['target_ref'], target_raw=target['target_observation'])
        stamp = datetime.fromisoformat(rebuilt['fixture']['timestamp']).replace(tzinfo=timezone.utc, hour=15)
        store.put_forecast(payload(self.protocol['id'], rebuilt), root=self.root, clock=lambda: stamp)
        with self.db() as con:
            con.execute('DROP TRIGGER confirmation_sources_no_delete'); con.execute('DELETE FROM sources')
        with self.assertRaises(ValueError): store.forecasts(self.root)

    def test_results_append_revisions_without_replacing_forecasts(self):
        saved = self.save(); identity = saved['payload']['id']
        pending = seal(dict(checked_at='2026-01-10T18:00:00Z', status='pending'))
        settled = seal(dict(checked_at='2026-01-10T20:00:00Z', status='settled', actual='2nd'))
        first = store.put_result(identity, pending, self.root)
        self.assertEqual(first['revision'], 1)
        self.assertTrue(store.put_result(identity, pending, self.root)['existing'])
        second = store.put_result(identity, settled, self.root)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(store.results(identity, self.root),
                         [dict(revision=1, payload=pending, ack=first['ack']),
                          dict(revision=2, payload=settled, ack=second['ack'])])
        self.assertEqual(store.forecasts(self.root)[0]['payload'], saved['payload'])
        with self.db() as con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute('DELETE FROM results')

    def test_result_ack_is_after_commit_and_can_cross_fixed_endpoint(self):
        saved = self.save(); identity = saved['payload']['id']
        value = seal(dict(checked_at='2027-01-04T07:59:58+00:00', status='settled', actual='2nd'))
        transaction = datetime.fromisoformat('2027-01-04T07:59:59+00:00')
        after = datetime.fromisoformat('2027-01-04T08:00:01+00:00')
        cutoff = datetime.fromisoformat('2027-01-04T08:00:00+00:00')
        original = store._write_result_ack
        def checked_write(con, ack):
            # A separate reader can already see the result before its ACK.
            with self.db() as other:
                self.assertEqual(other.execute('SELECT count(*) FROM results WHERE forecast_id=?',
                                               (identity,)).fetchone()[0], 1)
            original(con, ack)
        Clock.values = [transaction, after]
        with patch.object(store, 'datetime', Clock), patch.object(store, '_write_result_ack', side_effect=checked_write):
            result = store.put_result(identity, value, self.root)
        self.assertEqual(result['ack']['time_quality'], 'local_result_commit_ack')
        self.assertLess(datetime.fromisoformat(value['checked_at']), cutoff)
        self.assertGreater(datetime.fromisoformat(result['ack']['observed_at_utc']), cutoff)
        self.assertEqual(store.results(identity, self.root)[0]['ack'], result['ack'])

    def test_result_clock_injection_is_synthetic_and_never_production_proof(self):
        saved = self.save(); identity = saved['payload']['id']
        current = datetime.fromisoformat('2027-01-04T08:00:00+00:00')
        value = seal(dict(checked_at=current.isoformat(), status='pending'))
        result = store.put_result(identity, value, self.root, clock=lambda: current)
        self.assertEqual(result['ack']['time_quality'], 'synthetic_unattested')
        self.assertEqual(result['ack']['observed_at_utc'], current.isoformat())

    def test_result_production_root_rejects_clock_before_creating_database(self):
        value = seal(dict(status='pending'))
        with patch.object(store.api, 'DATA_DIR', self.root), self.assertRaises(ValueError):
            store.put_result('a'*64, value, self.root, clock=lambda: self.before())
        self.assertFalse(self.root.exists())

    def test_missing_result_ack_is_never_repaired_on_identical_replay(self):
        saved = self.save(); identity = saved['payload']['id']
        value = seal(dict(checked_at='2026-01-10T20:00:00Z', status='pending'))
        with patch.object(store, '_write_result_ack', side_effect=OSError('Synthetic result receipt interruption')):
            with self.assertRaises(OSError):
                store.put_result(identity, value, self.root)
        self.assertEqual(store.results(identity, self.root), [dict(revision=1, payload=value, ack=None)])
        never = lambda: (_ for _ in ()).throw(AssertionError('Never recreate missing result timing'))
        replay = store.put_result(identity, value, self.root, clock=never)
        self.assertTrue(replay['existing']); self.assertIsNone(replay['ack'])
        self.assertIsNone(store.results(identity, self.root)[0]['ack'])

    def test_legacy_results_without_receipt_table_remain_readable_and_unattested(self):
        saved = self.save(); identity = saved['payload']['id']
        value = seal(dict(checked_at='2026-01-10T20:00:00Z', status='pending'))
        store.put_result(identity, value, self.root)
        with self.db() as con:
            con.execute('DROP TABLE result_receipts')
        self.assertEqual(store.results(identity, self.root), [dict(revision=1, payload=value, ack=None)])
        # Reopening for a write may migrate the schema, but never attests old rows.
        replay = store.put_result(identity, value, self.root)
        self.assertTrue(replay['existing']); self.assertIsNone(replay['ack'])

    def test_future_result_check_is_rejected_before_insert(self):
        saved = self.save(); identity = saved['payload']['id']
        value = seal(dict(checked_at='2027-01-04T08:01:00+00:00', status='pending'))
        current = datetime.fromisoformat('2027-01-04T08:00:00+00:00')
        with self.assertRaises(ValueError):
            store.put_result(identity, value, self.root, clock=lambda: current)
        self.assertEqual(store.results(identity, self.root), [])

    def test_result_receipts_are_immutable_and_bound_to_exact_revision(self):
        saved = self.save(); identity = saved['payload']['id']
        value = seal(dict(checked_at='2026-01-10T20:00:00Z', status='pending'))
        result = store.put_result(identity, value, self.root)
        with self.db() as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('UPDATE result_receipts SET payload=?', ('{}',))
            with self.assertRaises(sqlite3.IntegrityError): con.execute('DELETE FROM result_receipts')
            con.execute('DROP TRIGGER confirmation_result_receipts_no_update')
            changed = deepcopy(result['ack']); changed.pop('id'); changed['result_id'] = 'b'*64
            changed = seal(changed)
            con.execute('UPDATE result_receipts SET payload=?,digest=?', (store._json(changed), digest(changed)))
        with self.assertRaises(ValueError): store.results(identity, self.root)

    def test_backwards_result_clock_never_produces_timing_receipt(self):
        saved = self.save(); identity = saved['payload']['id']
        value = seal(dict(checked_at='2026-01-10T18:00:00Z', status='pending'))
        values = iter([datetime.fromisoformat('2026-01-10T20:00:00+00:00'),
                       datetime.fromisoformat('2026-01-10T19:00:00+00:00')])
        with self.assertRaises(ValueError):
            store.put_result(identity, value, self.root, clock=lambda: next(values))
        self.assertEqual(store.results(identity, self.root), [dict(revision=1, payload=value, ack=None)])

    def endpoint_example(self):
        cutoff = '2027-01-04T08:00:00+00:00'
        protocol = seal(dict(version='test-followup', registered_at='2026-10-31T20:00:00Z',
                             followup=dict(result_cutoff_utc=cutoff)))
        store.put_protocol(protocol, root=self.root)
        value = seal(dict(version='causal-confirmation-endpoint-v1', protocol_id=protocol['id'],
                          result_cutoff_utc=cutoff, prepared_at='2027-01-04T08:00:10+00:00',
                          selection={'a'*64:dict(revision=1, result_id='b'*64),
                                     'c'*64:dict(revision=None, result_id=None)},
                          days=[dict(day='2026-11-02', state='anchored', targets=2, captures=2)],
                          attempt_ids=['d'*64]))
        current = datetime.fromisoformat('2027-01-04T08:00:11+00:00')
        return protocol, value, current

    def test_final_endpoint_roundtrip_is_immutable_and_exact_idempotent(self):
        protocol, value, current = self.endpoint_example()
        self.assertIsNone(store.endpoint(protocol['id'], root=self.root))
        self.assertEqual(store.endpoint(protocol['id'], value, self.root, clock=lambda: current), value)
        self.assertEqual(store.endpoint(protocol['id'], root=self.root), value)
        never = lambda: (_ for _ in ()).throw(AssertionError('Do not recapture final endpoint time'))
        self.assertEqual(store.endpoint(protocol['id'], value, self.root, clock=never), value)
        changed = deepcopy(value); changed.pop('id'); changed['selection']['a'*64]['revision'] = 2
        changed = seal(changed)
        with self.assertRaises(ValueError):
            store.endpoint(protocol['id'], changed, self.root, clock=lambda: current)
        self.assertEqual(store.endpoint(protocol['id'], root=self.root), value)
        with self.db() as con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute('DELETE FROM endpoints')
            with self.assertRaises(sqlite3.IntegrityError): con.execute('UPDATE endpoints SET payload=?', ('{}',))

    def test_final_endpoint_reader_never_creates_database_or_old_table(self):
        self.assertIsNone(store.endpoint('a'*64, root=self.root))
        self.assertFalse(self.root.exists())
        protocol, value, current = self.endpoint_example()
        with self.db() as con:
            con.execute('DROP TABLE endpoints')
        self.assertIsNone(store.endpoint(protocol['id'], root=self.root))
        with self.db() as con:
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='endpoints'").fetchone())

    def test_final_endpoint_requires_known_followup_and_registered_cutoff(self):
        protocol, value, current = self.endpoint_example()
        changed = deepcopy(value); changed.pop('id'); changed['result_cutoff_utc'] = '2027-01-03T08:00:00Z'
        with self.assertRaises(ValueError):
            store.endpoint(protocol['id'], seal(changed), self.root, clock=lambda: current)
        changed['protocol_id'] = 'e'*64
        with self.assertRaises(ValueError):
            store.endpoint('e'*64, seal(changed), self.root, clock=lambda: current)
        self.enroll()
        changed['protocol_id'] = self.protocol['id']
        with self.assertRaises(ValueError):
            store.endpoint(self.protocol['id'], seal(changed), self.root, clock=lambda: current)

    def test_final_endpoint_cannot_be_prepared_or_written_before_cutoff(self):
        protocol, value, current = self.endpoint_example()
        for prepared, now in [('2027-01-04T07:59:59Z', current),
                              (value['prepared_at'], datetime.fromisoformat('2027-01-04T07:59:59+00:00')),
                              ('2027-01-04T08:00:12Z', current)]:
            changed = deepcopy(value); changed.pop('id'); changed['prepared_at'] = prepared
            with self.subTest(prepared=prepared, now=now), self.assertRaises(ValueError):
                store.endpoint(protocol['id'], seal(changed), self.root, clock=lambda: now)
        self.assertIsNone(store.endpoint(protocol['id'], root=self.root))

    def test_final_endpoint_rejects_injected_production_clock_before_write(self):
        protocol, value, current = self.endpoint_example()
        other = self.root/'production'
        with patch.object(store.api, 'DATA_DIR', other), self.assertRaises(ValueError):
            store.endpoint(protocol['id'], value, other, clock=lambda: current)
        self.assertFalse(other.exists())

    def test_final_endpoint_selection_rejects_invalid_revision_or_result_identity(self):
        protocol, value, current = self.endpoint_example()
        for selected in [dict(revision=True, result_id='b'*64), dict(revision=0, result_id='b'*64),
                         dict(revision=None, result_id='b'*64), dict(revision=1, result_id=None),
                         dict(revision=1, result_id='b'*64, extra='unexpected')]:
            changed = deepcopy(value); changed.pop('id'); changed['selection']['a'*64] = selected
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                store.endpoint(protocol['id'], seal(changed), self.root, clock=lambda: current)
        self.assertIsNone(store.endpoint(protocol['id'], root=self.root))

    def test_concurrent_final_endpoint_sealers_keep_one_selection(self):
        protocol, first, current = self.endpoint_example()
        second = deepcopy(first); second.pop('id'); second['selection']['a'*64] = dict(revision=2, result_id='f'*64)
        second = seal(second)
        def save(value):
            try:
                return store.endpoint(protocol['id'], value, self.root, clock=lambda: current)
            except store.StoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, [first, second]))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertIn(store.endpoint(protocol['id'], root=self.root), [first, second])

    def test_corrupted_final_endpoint_is_never_silently_rebuilt(self):
        protocol, value, current = self.endpoint_example()
        store.endpoint(protocol['id'], value, self.root, clock=lambda: current)
        with self.db() as con:
            con.execute('DROP TRIGGER confirmation_endpoints_no_update')
            con.execute('UPDATE endpoints SET payload=?', ('{}',))
        with self.assertRaises(ValueError): store.endpoint(protocol['id'], root=self.root)
        with self.assertRaises(ValueError):
            store.endpoint(protocol['id'], value, self.root, clock=lambda: current)

    def test_modified_seals_or_fixture_do_not_enter_store(self):
        self.enroll()
        value = payload(self.protocol['id'], self.snapshot)
        value['fixture']['timestamp'] = '2026-01-10T20:00:00'
        with self.assertRaises(ValueError): store.put_forecast(value, root=self.root, clock=lambda: self.before())
        value.pop('id'); value = seal(value)
        with self.assertRaises(ValueError): store.put_forecast(value, root=self.root, clock=lambda: self.before())
        self.assertEqual(store.forecasts(self.root), [])

    def test_concurrent_first_writers_keep_one_original_forecast(self):
        self.enroll()
        first = payload(self.protocol['id'], self.snapshot)
        second = deepcopy(first); second.pop('id'); second['methods'] = {'candidate': {'1st': .4, 'Égal': .2, '2nd': .4}}
        second = seal(second)
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda value: store.put_forecast(value, root=self.root, clock=lambda: self.before()), [first, second]))
        self.assertEqual(sum(not value['existing'] for value in values), 1)
        records = store.forecasts(self.root)
        self.assertEqual(len(records), 1)
        self.assertTrue(all(value['payload'] == records[0]['payload'] for value in values))

    def test_cloud_guard_and_cross_bound_result_are_rejected(self):
        with patch.object(store.cloud_storage, 'configured', return_value=True), self.assertRaises(ValueError):
            self.enroll()
        self.assertFalse(self.root.exists())
        saved = self.save()
        result = seal(dict(forecast_id='a'*64, status='pending'))
        with self.assertRaises(ValueError): store.put_result(saved['payload']['id'], result, root=self.root)

    def test_heads_never_rebuild_or_read_history_documents(self):
        saved = self.save()
        with patch.object(store.snapshots, 'validate', side_effect=AssertionError('No feature reconstruction')), \
                patch.object(store, '_document', side_effect=AssertionError('No shared history read')):
            heads = store.heads(self.root, protocol_id=self.protocol['id'])
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]['payload']['id'], saved['payload']['id'])
        self.assertEqual(heads[0]['ack'], saved['ack'])
        self.assertNotIn('history_rows', heads[0]['payload']['snapshot'])
        self.assertNotIn('source_refs', heads[0]['payload']['snapshot'])
        self.assertEqual(store.get_history(heads[0]['context_id'], self.root), dict(history_rows=[], source_refs=[]))

    def test_heads_detect_modified_compact_payload_without_history_rebuild(self):
        self.save()
        with self.db() as con:
            con.execute('DROP TRIGGER confirmation_forecasts_no_update')
            con.execute('UPDATE forecasts SET payload=?', ('{}',))
        with self.assertRaises(ValueError): store.heads(self.root)


if __name__ == '__main__':
    unittest.main()
