"""Prospective selection evidence: synthetic data, isolated SQLite, no API."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import api_football as api
import argumented_selection_engine as engine
import argumented_selection_store as store
import high_probability_engine as probabilities
from test_high_probability_engine import NOW, fixture, history


def report(targets, now=NOW):
    historical = history(equal=True)
    return engine.build(probabilities.scan(historical + targets, now,
        day=datetime.fromisoformat(targets[0]['fixture']['date']).date()),
        [api.normalize_fixture(raw) for raw in historical], now)


class ArgumentedSelectionStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'selection'
        self.raw = [fixture(100001), fixture(100002)]
        self.report = report(self.raw)
        self.assertEqual(len(self.report['rows']), 2)

    def capture(self, value=None, *, refresh=True, clock=None, check_time=None):
        if refresh is True:
            refresh = lambda **kwargs: deepcopy(self.raw)
        return store.capture(self.report if value is None else value, self.root,
                             clock=clock or (lambda: NOW), refresh=refresh, check_time=check_time)

    def entries(self, now=NOW):
        return store.entries(self.root, now)

    def reconcile(self, raw, now=NOW + timedelta(hours=10)):
        with patch.object(api, 'load_fixtures', return_value=raw) as read:
            result = store.reconcile(self.root, now)
        return result, read

    def test_empty_readonly_views_and_reconciliation_create_nothing(self):
        with patch.object(api, 'load_fixtures', side_effect=AssertionError('No raw read')):
            self.assertEqual(store.signature(self.root), ())
            self.assertEqual(self.entries(), [])
            summary = store.summary(self.root, NOW)
            self.assertEqual(summary['counts']['matches'], 0)
            self.assertEqual(summary['evaluated'], 0)
            self.assertTrue(all(row['accuracy'] is None for row in summary['by_method'].values()))
            self.assertEqual(store.reconcile(self.root, NOW)['changed'], 0)
        self.assertFalse(self.root.exists())

    def test_capture_freezes_whole_proof_probabilities_and_own_code_hashes(self):
        original = deepcopy(self.report)
        self.report['model_code_sha256'] = {'caller': 'untrusted'}
        self.report['rows'][0]['caller_note'] = {'proof': 'preserve all engine row fields'}
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API client')):
            self.assertEqual(self.capture()['created'], 2)
        rows = self.entries()
        self.assertEqual(len(rows), 2)
        by_id = {row['fixture']['match_id']: row for row in rows}
        for row in self.report['rows']:
            saved = by_id[row['fixture']['match_id']]
            self.assertEqual(saved['engine_row'], row)
            self.assertEqual(saved['engine_row_sha256'], store._digest(row))
            self.assertEqual(saved['created_at'], NOW.isoformat())
            self.assertEqual(saved['selection_version'], engine.VERSION)
            self.assertEqual(saved['criteria'], engine.CRITERIA)
            self.assertEqual(saved['status'], 'pending')
            self.assertIsNone(saved['correct'])
            self.assertEqual(saved['recent_home']['matches'], 10)
            self.assertEqual(saved['recent_away']['matches'], 10)
            self.assertEqual(saved['confirmation']['status'], 'NS')
            self.assertEqual(saved['confirmation']['raw_sha256'], store._digest(saved['confirmation']['raw']))
            self.assertEqual(saved['model_code_sha256'], store.implementation.code_hashes())
            for name in store.SELECTION_SOURCES:
                expected = sha256((Path(store.__file__).parent / name).read_bytes()).hexdigest()
                self.assertEqual(saved['selection_code_sha256'][name], expected)
        self.assertEqual(self.report['rows'][1], original['rows'][1])

    def test_first_capture_survives_recalculation_and_a_new_calendar_day(self):
        self.capture()
        first = self.entries()
        changed = deepcopy(self.report)
        row = changed['rows'][0]
        row['probabilities'] = {'1st': .05, 'Égal': .90, '2nd': .05}
        row['selection_probability'] = .9
        row['delta_points'] = (.9 - row['baseline_probability']) * 100
        unused = Mock(side_effect=AssertionError('Existing matches require no refresh'))
        result = self.capture(changed, refresh=unused, clock=lambda: NOW + timedelta(minutes=1))
        self.assertEqual((result['created'], result['existing']), (0, 2))
        tomorrow = deepcopy(changed)
        tomorrow['day'] = '2026-10-23'
        tomorrow['before'] = '2026-10-23'
        tomorrow['generated_at'] = (NOW + timedelta(days=1)).isoformat()
        for row in tomorrow['rows']:
            row['fixture']['timestamp'] = '2026-10-23T15:00:00'
        result = self.capture(tomorrow, refresh=unused, clock=lambda: NOW + timedelta(days=1))
        self.assertEqual((result['created'], result['existing']), (0, 2))
        self.assertEqual(self.entries(), first)
        unused.assert_not_called()

    def test_fresh_confirmation_is_required_and_identity_changes_exclude(self):
        result = self.capture(refresh=None)
        self.assertEqual((result['created'], result['unavailable']), (0, 2))
        self.assertFalse(self.root.exists())
        for alter in (
            lambda raw: raw['fixture']['status'].update(short='PST'),
            lambda raw: raw['fixture'].update(date='2026-10-22T16:00:00+00:00'),
            lambda raw: raw['teams']['home'].update(id=999),
            lambda raw: raw['teams']['away'].update(id=998),
            lambda raw: raw['league'].update(id=999),
            lambda raw: raw['league'].update(season=2025),
            lambda raw: raw.update(provider='other'),
            lambda raw: raw.update(_patternlab_import={}),
        ):
            changed = deepcopy(self.raw)
            for raw in changed:
                alter(raw)
            with self.subTest(alter=alter):
                result = self.capture(refresh=lambda **kwargs: changed)
                self.assertEqual((result['created'], result['unavailable']), (0, 2))
                self.assertFalse(self.root.exists())

    def test_conflicting_input_or_fresh_duplicates_never_choose_a_favourable_version(self):
        changed = deepcopy(self.report)
        changed['rows'].append(deepcopy(changed['rows'][0]))
        changed['rows'][-1]['probabilities'] = {'1st': .1, 'Égal': .8, '2nd': .1}
        self.assertEqual(self.capture(changed)['created'], 1)
        self.assertEqual(self.entries()[0]['fixture']['match_id'], '100002')
        altered = deepcopy(self.raw[0])
        altered['fixture']['status']['short'] = 'PST'
        result = self.capture(refresh=lambda **kwargs: [self.raw[0], altered])
        self.assertEqual((result['created'], result['unavailable']), (0, 1))

    def test_invalid_selection_evidence_and_target_results_cannot_be_captured(self):
        for alter in (
            lambda row: row['recent_home'].update(matching=4),
            lambda row: row['recent_away']['rows'][0].update(away_id='999'),
            lambda row: row.update(criteria={}),
            lambda row: row.update(choice='1st'),
            lambda row: row['fixture'].update(score={'halftime': {'home': 99}}),
            lambda row: row.update(before='2026-10-23'),
            lambda row: row['fixture'].update(raw_status='FT'),
        ):
            changed = deepcopy(self.report)
            for row in changed['rows']:
                alter(row)
            with self.subTest(alter=alter):
                self.assertEqual(self.capture(changed)['created'], 0)
                self.assertFalse(self.root.exists())

    def test_every_qualifying_match_is_captured_with_twenty_item_provider_batches(self):
        self.raw = [fixture(200000 + index) for index in range(47)]
        self.report = report(self.raw)
        batches = []
        def refresh(**kwargs):
            ids = kwargs['ids'].split('-')
            batches.append(ids)
            self.assertEqual(kwargs['timezone'], 'UTC')
            return [deepcopy(raw) for raw in self.raw if str(raw['fixture']['id']) in ids]
        result = self.capture(refresh=refresh)
        self.assertEqual(result['created'], 47)
        self.assertEqual(list(map(len, batches)), [20, 20, 7])
        self.assertEqual(len(self.entries()), 47)

    def test_provider_and_scheduler_errors_propagate_without_hidden_capture(self):
        from automation_worker import _Pause
        for exception in (api.ApiFootballError('quota'), _Pause('deadline')):
            with self.subTest(exception=exception):
                with self.assertRaises(type(exception)) as raised:
                    self.capture(refresh=Mock(side_effect=exception))
                self.assertIs(raised.exception, exception)
                self.assertFalse(self.root.exists())
        for envelope in ({'response': [], 'errors': {'requests': 'quota'}},
                         {'response': self.raw, 'paging': {'current': 1, 'total': 2}},
                         [None], [{'fixture': None}]):
            with self.subTest(envelope=envelope), self.assertRaises(api.ApiFootballError):
                self.capture(refresh=lambda **kwargs: envelope)
        calls = []
        def checkpoint():
            calls.append(1)
            if len(calls) == 3:
                raise _Pause('deadline at commit')
        with self.assertRaises(_Pause):
            self.capture(check_time=checkpoint)
        self.assertEqual(self.entries(), [])

    def test_confirmation_and_commit_clocks_cannot_cross_kickoff(self):
        one = deepcopy(self.report)
        one['rows'] = one['rows'][:1]
        late = NOW + timedelta(hours=7)
        times = iter([NOW, NOW, NOW, late])
        with self.assertRaises(store.SelectionStoreError):
            self.capture(one, clock=lambda: next(times))
        self.assertEqual(self.entries(), [])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'expired'
            times = iter([NOW, late])
            result = store.capture(one, root, clock=lambda: next(times),
                                   refresh=lambda **kwargs: self.raw)
            self.assertEqual(result['created'], 0)
            self.assertFalse(root.exists())
        changed = deepcopy(one)
        changed['generated_at'] = (NOW + timedelta(seconds=1)).isoformat()
        with self.assertRaises(store.SelectionStoreError):
            self.capture(changed)

    def test_production_root_ignores_an_injected_historical_clock(self):
        class WallClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW + timedelta(days=1)
        with patch.object(api, 'DATA_DIR', self.root), patch.object(store, 'datetime', WallClock), \
                patch.object(store, '_connect', side_effect=AssertionError('No production write')):
            with self.assertRaises(store.SelectionStoreError):
                self.capture(clock=lambda: NOW)

    def test_frozen_procedure_code_requires_a_new_version_for_new_matches(self):
        self.capture()
        first = self.entries()
        self.raw = [fixture(100003)]
        self.report = report(self.raw)
        provenance = store._implementation()
        provenance['selection_code_sha256']['argumented_selection_engine.py'] = 'a' * 64
        provenance['implementation_sha256']['argumented_selection_engine.py'] = 'a' * 64
        with patch.object(store, '_implementation', return_value=provenance):
            with self.assertRaises(store.SelectionStoreError):
                self.capture()
        self.assertEqual(self.entries(), first)

    def test_legacy_procedure_remains_readable_and_new_procedure_attests_shared_contract(self):
        provenance = store._implementation()
        self.assertIn('halftime_domain.py',provenance['model_code_sha256'])
        old_model = {name:value for name,value in provenance['model_code_sha256'].items()
                     if name in store.implementation.LEGACY_SOURCE_FILES}
        old = dict(selection_version='argumented-selection-v1',criteria={'version':'argumented-selection-v1'},
                   model_code_sha256=old_model,selection_code_sha256=provenance['selection_code_sha256'],
                   implementation_sha256=dict(old_model,**provenance['selection_code_sha256']))
        before = deepcopy(old)
        self.assertEqual(store._procedure(old),before)
        self.assertEqual(old,before)
        with patch.object(store.implementation,'code_hashes',return_value=old_model):
            with self.assertRaises(store.SelectionStoreError):
                store._implementation()

    def test_tables_reject_updates_deletes_and_payload_digest_detects_tampering(self):
        self.capture()
        self.reconcile([fixture(100001, status='FT'), fixture(100002, status='FT')])
        with sqlite3.connect(store.database_path(self.root)) as con:
            for table in ('procedures', 'selections', 'results'):
                for sql in (f'UPDATE {table} SET payload=payload', f'DELETE FROM {table}'):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                        con.execute(sql)
            con.execute('DROP TRIGGER selection_no_update')
            con.execute("UPDATE selections SET payload=replace(payload,'preserve all','forged all')")
            # The above replacement is a no-op without the optional caller field;
            # replace a guaranteed field and retain its original digest.
            con.execute("UPDATE selections SET payload=replace(payload,'API-Football','forged source')")
        with self.assertRaises(store.SelectionStoreError):
            self.entries()

    def test_result_settlement_uses_regulation_scores_and_separates_correctness(self):
        self.capture()
        first = fixture(100001, status='AET', ht=(0, 0), ft=(1, 0))
        first['score']['extratime'] = {'home': 9, 'away': 9}
        second = fixture(100002, status='PEN', ht=(1, 0), ft=(1, 1))
        result, _ = self.reconcile([first, second])
        self.assertEqual(result['settled'], 2)
        rows = {row['fixture']['match_id']: row for row in self.entries(NOW + timedelta(hours=10))}
        self.assertEqual((rows['100001']['actual'], rows['100001']['ft']), ('2nd', '1-0'))
        self.assertFalse(rows['100001']['correct'])
        self.assertEqual(rows['100001']['status'], 'settled')
        self.assertEqual(rows['100002']['actual'], 'Égal')
        self.assertTrue(rows['100002']['correct'])
        summary = store.summary(self.root, NOW + timedelta(hours=10))
        self.assertEqual(summary['evaluated'], 2)
        self.assertTrue(all(metric['matches'] == 2 and metric['correct'] == 1
                            for metric in summary['by_method'].values()))
        self.assertGreater(summary['by_method']['clubs']['logloss'], 0)
        self.assertGreater(summary['by_method']['baseline']['brier'], 0)
        self.assertNotIn('logloss', summary['by_method']['selection'])

    def test_missing_postponed_void_changed_identity_and_stale_ns_are_explicit(self):
        self.capture()
        missing = fixture(100001, status='FT')
        missing['score']['halftime'] = {'home': None, 'away': None}
        result, _ = self.reconcile([missing, fixture(100002, status='PST')])
        self.assertEqual(result['unresolved'], 2)
        result, _ = self.reconcile([fixture(100001, status='CANC'), fixture(100002)],
                                   NOW + timedelta(hours=15))
        self.assertEqual((result['void'], result['unresolved']), (1, 1))
        for alter in (lambda raw: raw['league'].update(season=2025),
                      lambda raw: raw['league'].update(id=999),
                      lambda raw: raw['teams']['home'].update(id=999)):
            raw = fixture(100001, status='FT')
            alter(raw)
            result, _ = self.reconcile([raw, fixture(100002, status='PST')],
                                       NOW + timedelta(days=1))
            self.assertEqual(result['unresolved'], 2)
        summary = store.summary(self.root, NOW + timedelta(days=1))
        self.assertEqual(summary['evaluated'], 0)
        self.assertTrue(all(row['correct'] is None for row in summary['rows']))

    def test_changed_kickoff_requires_final_result_and_compatible_prematch_time(self):
        self.capture()
        changed = fixture(100001, stamp='2026-10-22T16:00:00+00:00')
        self.reconcile([changed, self.raw[1]])
        row = next(row for row in self.entries(NOW + timedelta(hours=10))
                   if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['status'], 'unresolved')
        changed['fixture']['status']['short'] = 'FT'
        self.reconcile([changed, self.raw[1]], NOW + timedelta(hours=11))
        row = next(row for row in self.entries(NOW + timedelta(hours=11))
                   if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['status'], 'settled')
        self.assertEqual(row['schedule_resolution']['effective_timestamp'], '2026-10-22T16:00:00')
        changed['fixture']['date'] = '2026-10-22T08:00:00+00:00'
        self.reconcile([changed, self.raw[1]], NOW + timedelta(hours=12))
        row = next(row for row in self.entries(NOW + timedelta(hours=12))
                   if row['fixture']['match_id'] == '100001')
        self.assertEqual(row['status'], 'unresolved')

    def test_result_corrections_append_and_readonly_asof_views_preserve_database(self):
        self.capture()
        raw = [fixture(100001, status='FT', ht=(0, 0), ft=(1, 0)),
               fixture(100002, status='FT', ht=(1, 0), ft=(1, 1))]
        self.reconcile(raw)
        self.assertEqual(self.reconcile(raw)[0]['changed'], 0)
        raw[0]['score']['halftime'] = {'home': 1, 'away': 0}
        self.assertEqual(self.reconcile(raw, NOW + timedelta(days=1))[0]['changed'], 1)
        rows = self.entries(NOW + timedelta(days=1))
        corrected = next(row for row in rows if row['fixture']['match_id'] == '100001')
        self.assertEqual([event['actual'] for event in corrected['result_history']], ['2nd', '1st'])
        path = store.database_path(self.root)
        before = path.read_bytes()
        token = store.signature(self.root)
        with patch.object(api, 'load_fixtures', side_effect=AssertionError('Read view cannot load results')), \
                patch.object(store, '_implementation', side_effect=AssertionError('No re-attestation')):
            early = self.entries(NOW + timedelta(hours=10))
            corrected = next(row for row in early if row['fixture']['match_id'] == '100001')
            self.assertEqual(corrected['actual'], '2nd')
            self.assertEqual(len(corrected['result_history']), 1)
            self.assertEqual(self.entries(NOW - timedelta(seconds=1)), [])
            store.summary(self.root, NOW + timedelta(days=1))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.signature(self.root), token)

    def test_reconciliation_reads_only_tracked_fixture_ids_and_never_calls_provider(self):
        self.capture()
        with patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
            _, read = self.reconcile(self.raw)
        read.assert_called_once_with(self.root.resolve() / 'fixtures.json', ids=['100001', '100002'])

    def test_summary_first_match_denominator_does_not_weight_extra_versions(self):
        self.capture()
        first = self.entries()
        duplicate = deepcopy(first[0])
        duplicate.update(id='later-version', selection_version='future-version')
        duplicate['created_at'] = (NOW + timedelta(seconds=1)).isoformat()
        for row in first + [duplicate]:
            row.update(status='settled', actual='Égal')
        with patch.object(store, 'entries', return_value=first + [duplicate]):
            summary = store.summary(self.root, NOW + timedelta(hours=10))
        self.assertEqual(summary['captures'], 3)
        self.assertEqual(summary['counts']['matches'], 2)
        self.assertEqual(summary['evaluated'], 2)
        self.assertTrue(all(metric['matches'] == 2 for metric in summary['by_method'].values()))
        self.assertEqual(summary['by_day'][0]['evaluated'], 2)

    def test_zero_probability_actual_is_explicit_and_summary_remains_valid_json(self):
        self.capture()
        rows = self.entries()
        rows[0].update(status='settled', actual='1st',
                       probabilities={'1st': 0, 'Égal': 1, '2nd': 0})
        with patch.object(store, 'entries', return_value=rows):
            summary = store.summary(self.root, NOW + timedelta(hours=10))
        clubs = summary['by_method']['clubs']
        self.assertEqual(clubs['matches'], 1)
        self.assertEqual(clubs['zero_probability_actual'], 1)
        self.assertIsNone(clubs['logloss'])
        self.assertFalse(clubs['logloss_defined'])
        self.assertEqual(clubs['brier'], 2)
        json.dumps(summary, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
