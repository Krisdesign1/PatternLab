"""Lossless opt-in migration, indexed reads and transactional correction guards."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import api_football as api
import api_match_workspace as calendar
import fixture_store as store
from test_api_football import fixture


def changed(raw, *, stamp=None, league=None, status=None):
    raw = deepcopy(raw)
    if stamp is not None:
        raw['fixture']['date'] = stamp
    if league is not None:
        raw['league']['id'] = league
    if status is not None:
        raw['fixture']['status']['short'] = status
    return raw


class FixtureStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root/'fixtures.json'
        self.database = store.database_path(self.path)

    def seed(self, raw=None, **kwargs):
        api.save_fixtures([fixture()] if raw is None else raw, self.path, **kwargs)
        return self.path.read_bytes()

    def migrate(self, raw=None, **kwargs):
        before = self.seed(raw, **kwargs)
        report = store.migrate_json(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(api.active_store(self.path), self.database)
        return report

    def test_missing_json_is_not_silently_activated_as_empty_database(self):
        self.assertIsNone(api.active_store(self.path))
        self.assertFalse(self.database.exists())
        with self.assertRaisesRegex(store.FixtureStoreError, 'absent'):
            store.migrate_json(self.path)
        self.assertFalse(self.database.exists())

    def test_migration_is_lossless_and_idempotent_with_source_fingerprint(self):
        raw = [fixture(), fixture(456, status='NS', season=2024)]
        raw[0]['provider_extension'] = dict(unusual='équipe', value=None)
        self.seed(raw)
        before = self.path.read_bytes()
        original_matches = api.load_matches(self.path)
        report = store.migrate_json(self.path)
        self.assertEqual(report['source_sha256'], sha256(before).hexdigest())
        self.assertEqual(report['stored_count'], 2)
        self.assertEqual(report['usable_count'], 1)
        self.assertEqual(api.load_fixtures(self.path), raw)
        self.assertEqual(api.load_matches(self.path), original_matches)
        self.assertEqual(api.export_store(self.path)['fixtures'], raw)
        signature = api.data_signature(self.path)
        with patch.object(api, '_load_store', side_effect=AssertionError('No second JSON parse')):
            again = store.migrate_json(self.path)
        self.assertTrue(again['already_active'])
        self.assertEqual(signature, api.data_signature(self.path))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob('.fixtures-migration-*')), [])

    def test_normalized_rows_and_issues_preserve_regulation_scores_and_order(self):
        invalid = fixture(500)
        invalid['score']['halftime'] = None
        invalid_status = fixture(700)
        invalid_status['fixture']['status'] = None
        invalid_date = changed(fixture(800), stamp='2025-08-12T12:00:00')
        penalty = changed(fixture(600), status='PEN')
        raw = [invalid, penalty, fixture(200), invalid_status, invalid_date, fixture(100, status='NS')]
        self.seed(raw)
        expected = api.load_matches(self.path)
        store.migrate_json(self.path)
        self.assertEqual(api.load_matches(self.path), expected)
        rows, audit = api.load_matches(self.path)
        self.assertEqual([row['match_id'] for row in rows], ['200', '600'])
        self.assertEqual(audit['expected'], 5)
        self.assertEqual([row['match_id'] for row in audit['issues']], ['500', '700', '800'])
        self.assertEqual(rows[-1]['ft'], '2-1')

    def test_explicit_foreign_provider_is_preserved_raw_but_never_becomes_training_data(self):
        foreign = dict(fixture(456), provider='foreign-provider')
        accepted = [fixture(1), dict(fixture(2), provider=None),
                    dict(fixture(3), provider='api-football'), dict(fixture(4), provider='API-Football')]
        self.seed(accepted + [foreign])
        expected = api.load_matches(self.path)
        self.assertEqual([row['match_id'] for row in expected[0]], ['1', '2', '3', '4'])
        self.assertEqual(expected[1]['issues'][0]['match_id'], '456')
        with self.assertRaisesRegex(ValueError, 'API-Football'):
            api.normalize_fixture(foreign)
        store.migrate_json(self.path)
        self.assertEqual(api.load_matches(self.path), expected)
        self.assertEqual(api.load_fixtures(self.path)[-1], foreign)
        self.assertEqual(store.statistics(self.database)['usable_count'], 4)

    def test_foreign_history_does_not_gain_probability_eligibility_after_migration(self):
        import high_probability_workspace as workspace
        from test_high_probability_engine import history, fixture as target, NOW
        workspace._cached_scan.cache_clear()
        self.addCleanup(workspace._cached_scan.cache_clear)
        raw = [dict(row, provider='foreign-provider') for row in history()]
        self.seed(raw + [target(100001)])
        before = workspace.load_scan(self.root, NOW)
        store.migrate_json(self.path)
        after = workspace.load_scan(self.root, NOW)
        self.assertEqual(before['rows'], after['rows'])
        self.assertEqual(before['rows'], [])
        self.assertEqual(before['history_fingerprint'], after['history_fingerprint'])
        self.assertEqual(before['diagnostics']['history_count'], after['diagnostics']['history_count'])

    def test_corrupt_existing_database_fails_closed_instead_of_reusing_stale_json(self):
        self.seed()
        self.database.write_bytes(b'not a SQLite database')
        with self.assertRaises(api.ApiFootballError):
            api.load_fixtures(self.path)
        with self.assertRaises(store.FixtureStoreError):
            store.migrate_json(self.path)
        self.assertEqual(self.database.read_bytes(), b'not a SQLite database')

    def test_invalid_json_duplicate_ids_or_nan_never_publish_a_database(self):
        payloads = [dict(version=1, fixtures=[fixture(), fixture()]),
                    dict(version=1, fixtures=[dict(fixture(), extension=float('nan'))]),
                    dict(version=3, fixtures=[])]
        for payload in payloads:
            with self.subTest(payload=payload.get('version')):
                self.path.write_text(json.dumps(payload))
                before = self.path.read_bytes()
                with self.assertRaises((api.ApiFootballError, ValueError)):
                    store.migrate_json(self.path)
                self.assertFalse(self.database.exists())
                self.assertEqual(self.path.read_bytes(), before)

    def test_failure_before_publication_keeps_json_authority_and_removes_partial_files(self):
        before = self.seed()
        with patch.object(store.os, 'replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                store.migrate_json(self.path)
        self.assertFalse(self.database.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(api.load_fixtures(self.path), [fixture()])
        self.assertEqual(list(self.root.glob('.fixtures-migration-*')), [])

    def test_selective_raw_and_normalized_reads_equal_json_reference(self):
        raw = [changed(fixture(1), stamp='2026-10-23T22:59:59Z'),
               changed(fixture(2), stamp='2026-10-23T23:00:00Z'),
               changed(fixture(3), stamp='2026-10-24T15:00:00+02:00', league=39),
               changed(fixture(4, status='NS'), stamp='2026-10-24T23:00:00Z'),
               changed(fixture(5), stamp='invalid')]
        self.seed(raw)
        start, end = calendar._bounds(date(2026, 10, 24), 'Africa/Kinshasa')
        filters = [{}, dict(start=start, end=end), dict(leagues=['39']),
                   dict(ids=['1', '3']), dict(statuses=['NS']), dict(leagues=[]),
                   dict(start='2026-10-24', end='2026-10-25', statuses=['FT']),
                   dict(ids=["1') OR 1=1 --"])]
        expected = [(api.load_fixtures(self.path, **query), api.load_matches(self.path, **query))
                    for query in filters]
        store.migrate_json(self.path)
        with patch.object(api, '_load_store', side_effect=AssertionError('No JSON read')):
            for query, (fixtures_expected, matches_expected) in zip(filters, expected):
                with self.subTest(query=query):
                    self.assertEqual(api.load_fixtures(self.path, **query), fixtures_expected)
                    self.assertEqual(api.load_matches(self.path, **query), matches_expected)
        self.assertEqual([row['fixture']['id'] for row in api.load_fixtures(self.path, start=start, end=end)], [2, 3])

    def test_calendar_selects_only_day_payloads_using_an_index(self):
        self.migrate([changed(fixture(i), stamp=f'2026-10-{20+i:02d}T12:00:00Z') for i in range(1, 6)])
        query, values = store._filters(start='2026-10-24', end='2026-10-25')
        with sqlite3.connect(self.database) as connection:
            plan = connection.execute('EXPLAIN QUERY PLAN SELECT raw_json FROM fixtures WHERE '
                                      + ' AND '.join(query), values).fetchall()
        self.assertTrue(any('fixture_kickoff' in row[-1] for row in plan), plan)
        with patch.object(api, 'normalize_fixture', side_effect=AssertionError('No renormalization')):
            snapshot = store.calendar_snapshot(self.database, start='2026-10-24', end='2026-10-25')
        self.assertEqual([row['fixture']['id'] for row in snapshot['matches']], [4])
        self.assertEqual([row['match_id'] for row in snapshot['usable']], ['4'])
        self.assertEqual(snapshot['stats']['stored_count'], 5)

    def test_reads_never_reparse_raw_history_or_mutate_json_or_signature(self):
        self.migrate()
        signature = api.data_signature(self.path)
        original = self.path.read_bytes()
        with patch.object(api, '_load_store', side_effect=AssertionError('No JSON parse')), \
                patch.object(api, 'normalize_fixture', side_effect=AssertionError('No normalization')), \
                patch.object(api.ApiFootballClient, 'get', side_effect=AssertionError('No API')):
            for _ in range(3):
                self.assertEqual(len(api.load_matches(self.path)[0]), 1)
                self.assertEqual(store.statistics(self.database)['history_counts'], {'61': 1})
        self.assertEqual(api.data_signature(self.path), signature)
        self.assertEqual(self.path.read_bytes(), original)

    def test_all_updates_go_to_database_after_activation_even_if_json_is_absent(self):
        original = self.seed()
        store.migrate_json(self.path)
        self.path.unlink()
        self.assertEqual(api.save_fixtures([fixture(456)], self.path), 2)
        self.assertFalse(self.path.exists())
        self.assertEqual([raw['fixture']['id'] for raw in api.load_fixtures(self.path)], [123, 456])
        self.path.write_bytes(original)
        self.assertEqual(len(api.export_store(self.path)['fixtures']), 2)

    def test_finished_guard_and_final_corrections_match_json_reference(self):
        for final_status in ('FT', 'AET', 'PEN'):
            final = fixture(status=final_status)
            # Include same-ID duplicates in a batch: intermediate finals count.
            batches = [[final, fixture(status='NS')], [fixture(status='2H')],
                       [dict(final, fixture=dict(final['fixture'], referee='Corrected'))]]
            json_path = self.root/(final_status+'.json')
            sql_path = self.root/(final_status+'_sql.json')
            api.save_fixtures([], json_path)
            api.save_fixtures([], sql_path)
            store.migrate_json(sql_path)
            sql_original = sql_path.read_bytes()
            for batch in batches:
                api.save_fixtures(batch, json_path)
                api.save_fixtures(batch, sql_path)
                self.assertEqual(api.load_fixtures(sql_path), api.load_fixtures(json_path))
                self.assertEqual(api.load_matches(sql_path), api.load_matches(json_path))
            self.assertEqual(sql_path.read_bytes(), sql_original)

    def test_invalid_batch_rolls_back_all_data_and_revision(self):
        self.migrate()
        before, signature = api.export_store(self.path), api.data_signature(self.path)
        for batch in ([fixture(456), {'fixture': {'id': False}}],
                      [fixture(456), dict(fixture(789), extension=float('nan'))]):
            with self.subTest(batch=batch[-1]):
                with self.assertRaises(api.ApiFootballError):
                    api.save_fixtures(batch, self.path)
                self.assertEqual(api.export_store(self.path), before)
                self.assertEqual(api.data_signature(self.path), signature)

    def test_sql_failure_after_first_upsert_rolls_back_all_rows(self):
        self.migrate()
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TRIGGER reject_789 BEFORE INSERT ON fixtures WHEN NEW.match_id='789' "
                               "BEGIN SELECT RAISE(ABORT,'test rejected'); END")
        before, signature = api.export_store(self.path), api.data_signature(self.path)
        with self.assertRaises(api.ApiFootballError):
            api.save_fixtures([fixture(456), fixture(789)], self.path)
        self.assertEqual(api.export_store(self.path), before)
        self.assertEqual(api.data_signature(self.path), signature)

    def test_complete_season_manifests_migrate_and_daily_results_do_not_infer_completeness(self):
        raw = [fixture(), fixture(456, status='NS')]
        marker = dict(competition_code='61', season=2025, match_ids=[123, 456])
        self.seed(raw, season_imports=[marker])
        expected = api.load_season_imports(self.path)
        store.migrate_json(self.path)
        self.assertEqual(api.load_season_imports(self.path), expected)
        api.save_fixtures([fixture(789, season=2024)], self.path)
        self.assertEqual(api.load_season_imports(self.path), expected)
        self.assertEqual(api.export_store(self.path)['season_imports'], expected)

    def test_invalid_manifest_rolls_back_new_rows_and_keeps_revision(self):
        self.migrate()
        before, signature = api.export_store(self.path), api.data_signature(self.path)
        marker = dict(competition_code='61', season=2025, match_ids=[456])
        with self.assertRaises(api.ApiFootballError):
            api.save_fixtures([fixture(456), fixture(789)], self.path, season_imports=[marker])
        self.assertEqual(api.export_store(self.path), before)
        self.assertEqual(api.data_signature(self.path), signature)

    def test_manifest_reassignment_is_invalidated_and_mismatch_retained_final_aborts(self):
        self.migrate([fixture()], season_imports=[dict(competition_code='61', season=2025, match_ids=[123])])
        api.save_fixtures([changed(fixture(), league=39)], self.path)
        self.assertEqual(api.load_season_imports(self.path), [])
        # The retained final is league 39. A stale league 61 NS import cannot
        # certify that it actually imported a complete league 61 season.
        before, signature = api.export_store(self.path), api.data_signature(self.path)
        with self.assertRaises(api.ApiFootballError):
            api.save_fixtures([fixture(status='NS')], self.path, season_imports=[
                dict(competition_code='61', season=2025, match_ids=[123])])
        self.assertEqual(api.export_store(self.path), before)
        self.assertEqual(api.data_signature(self.path), signature)

    def test_calendar_refresh_migrates_and_older_times_cannot_move_freshness_backwards(self):
        self.seed()
        day = date(2026, 10, 24)
        calendar._mark_calendar_refresh(self.path, day, 'Africa/Kinshasa')
        expected = calendar._calendar_updates(self.path)
        sidecar = calendar._calendar_path(self.path).read_bytes()
        store.migrate_json(self.path)
        self.assertEqual(store.calendar_updates(self.database), expected)
        store.mark_calendar_refresh(self.database, day.isoformat(), 'Africa/Kinshasa', '2027-01-01T12:00:00Z')
        signature = api.data_signature(self.path)
        store.mark_calendar_refresh(self.database, day.isoformat(), 'Africa/Kinshasa', '2026-01-01T00:00:00Z')
        self.assertEqual(store.calendar_updates(self.database)[(day.isoformat(), 'Africa/Kinshasa')],
                         '2027-01-01T12:00:00+00:00')
        self.assertNotEqual(signature, api.data_signature(self.path))
        self.assertEqual(calendar._calendar_path(self.path).read_bytes(), sidecar)
        before = api.data_signature(self.path)
        with self.assertRaises(store.FixtureStoreError):
            store.mark_calendar_refresh(self.database, day.isoformat(), 'Africa/Kinshasa', 'invalid')
        self.assertEqual(before, api.data_signature(self.path))

    def test_wal_reader_keeps_original_committed_view_and_writer_is_not_blocked(self):
        self.migrate()
        original_signature = api.data_signature(self.path)
        with store._connection(self.database) as reader:
            original_meta = store._meta(reader)
            self.assertEqual(reader.execute('SELECT count(*) FROM fixtures').fetchone()[0], 1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(api.save_fixtures, [fixture(456)], self.path)
                self.assertEqual(future.result(timeout=5), 2)
            self.assertEqual(reader.execute('SELECT count(*) FROM fixtures').fetchone()[0], 1)
            self.assertEqual(store._meta(reader)['revision'], original_meta['revision'])
            self.assertNotEqual(api.data_signature(self.path), original_signature)
        self.assertEqual(len(api.load_fixtures(self.path)), 2)

    def test_more_than_sqlite_variable_limit_ids_are_bounded_deduplicated_and_ordered(self):
        invalid = fixture(700)
        invalid['score']['halftime'] = None
        self.migrate([fixture(123), invalid, fixture(1)])
        # The shipped macOS SQLite build allows 32766 SQL parameters. The
        # caller may supply an iterator of any size, including repeated IDs.
        identities = list(range(40000, 0, -1)) + [123, 700, 1]
        selected = api.load_fixtures(self.path, ids=iter(identities))
        self.assertEqual([row['fixture']['id'] for row in selected], [123, 700, 1])
        rows, audit = api.load_matches(self.path, ids=iter(identities))
        self.assertEqual([row['match_id'] for row in rows], ['1', '123'])
        self.assertEqual(audit, dict(expected=3, matched=2,
                         issues=[dict(match_id='700', reason='Score HT/FT absent ou invalide')]))

    def test_id_chunks_keep_one_read_snapshot_when_another_writer_commits(self):
        self.migrate([fixture(1), fixture(1000)])
        original = store._filters
        calls = []

        def publish(**filters):
            calls.append(filters)
            if len(calls) == 2:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(api.save_fixtures, [fixture(501)], self.path).result(timeout=5)
            return original(**filters)

        with patch.object(store, '_filters', side_effect=publish):
            selected = api.load_fixtures(self.path, ids=list(range(1, 501)) + [1000, 501, 1])
        self.assertEqual([row['fixture']['id'] for row in selected], [1, 1000])
        self.assertEqual([len(call['ids']) for call in calls[:2]], [500, 3])
        self.assertEqual(len(api.load_fixtures(self.path)), 3)

    def test_parallel_writers_keep_all_rows_and_increment_revision_once_per_batch(self):
        self.migrate([])
        revision = store.statistics(self.database)['revision']
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda identity: api.save_fixtures([fixture(identity)], self.path), range(1, 25)))
        self.assertEqual({raw['fixture']['id'] for raw in api.load_fixtures(self.path)}, set(range(1, 25)))
        self.assertEqual(store.statistics(self.database)['revision'], revision + 24)

    def test_calendar_snapshot_counts_and_metadata_share_one_transaction_during_publication(self):
        self.migrate([changed(fixture(123), stamp='2026-10-24T12:00:00Z')])
        original = store._statistics
        observed = threading.Event()
        published = threading.Event()

        def pause(connection, meta):
            observed.set()
            self.assertTrue(published.wait(5))
            return original(connection, meta)

        def writer():
            self.assertTrue(observed.wait(5))
            api.save_fixtures([changed(fixture(456), stamp='2026-10-24T13:00:00Z')], self.path)
            published.set()

        with patch.object(store, '_statistics', side_effect=pause), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            snapshot = store.calendar_snapshot(self.database, start='2026-10-24', end='2026-10-25')
            future.result(timeout=5)
        self.assertEqual(len(snapshot['matches']), 1)
        self.assertEqual(snapshot['stats']['stored_count'], 1)
        self.assertEqual(snapshot['stats']['usable_count'], 1)
        self.assertEqual(store.statistics(self.database)['stored_count'], 2)

    def test_migration_waiting_writer_rechecks_authority_after_lock_release(self):
        before = self.seed()
        entered, release = threading.Event(), threading.Event()
        original = store._schema

        def pause(connection):
            original(connection)
            entered.set()
            self.assertTrue(release.wait(5))

        with patch.object(store, '_schema', side_effect=pause), ThreadPoolExecutor(max_workers=2) as pool:
            migration = pool.submit(store.migrate_json, self.path)
            self.assertTrue(entered.wait(5))
            writer = pool.submit(api.save_fixtures, [fixture(456)], self.path)
            release.set()
            migration.result(timeout=5)
            writer.result(timeout=5)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([raw['fixture']['id'] for raw in api.load_fixtures(self.path)], [123, 456])

    def test_calendar_refresh_waiting_for_migration_marks_sqlite_authority(self):
        self.seed()
        entered, release = threading.Event(), threading.Event()
        original = store._schema

        def pause(connection):
            original(connection)
            entered.set()
            self.assertTrue(release.wait(5))

        with patch.object(store, '_schema', side_effect=pause), ThreadPoolExecutor(max_workers=2) as pool:
            migration = pool.submit(store.migrate_json, self.path)
            self.assertTrue(entered.wait(5))
            refresh = pool.submit(calendar._mark_calendar_refresh, self.path,
                                  date(2026, 10, 24), 'Africa/Kinshasa')
            # The migration already captured the sidecar. Refresh must wait
            # instead of writing to it after that capture.
            with self.assertRaises(TimeoutError):
                refresh.result(timeout=.05)
            release.set()
            migration.result(timeout=5)
            refresh.result(timeout=5)
        self.assertIn(('2026-10-24', 'Africa/Kinshasa'), store.calendar_updates(self.database))
        self.assertFalse(calendar._calendar_path(self.path).exists())


if __name__ == '__main__':
    unittest.main()
