"""Adversarial integration checks of import quality and durable receipts.

Every store lives in a temporary directory; network and credential access are
forbidden. The public save/load adapters are used for JSON and migrated SQLite.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import api_football as api
import cloud_storage
import fixture_import_policy as policy
import fixture_store as store
from test_api_football import fixture


class ImportAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for obj, name, kwargs in (
            (cloud_storage, 'configured', dict(return_value=False)),
            (api, 'load_key', dict(side_effect=AssertionError('No credentials in import tests'))),
            (api.ApiFootballClient, 'get', dict(side_effect=AssertionError('No network in import tests'))),
        ):
            patcher = patch.object(obj, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def path(self, mode, suffix=''):
        result = self.root / (mode + suffix) / 'fixtures.json'
        api.save_fixtures([], result)
        if mode == 'sqlite':
            store.migrate_json(result)
        return result

    def saved(self, path):
        return api.load_fixtures(path)[0]

    def observations(self, path, mid=123):
        return api.load_import_observations(path, ids=[str(mid)])

    def assert_chain(self, records):
        prior = {}
        for record in records:
            self.assertEqual(record['previous_observation'], prior.get(record['match_id']))
            self.assertEqual(policy.validate_record(record), record)
            self.assertIsNotNone(datetime.fromisoformat(record['observed_at_utc']).tzinfo)
            self.assertEqual(record['time_kind'], 'local_import_recorded')
            prior[record['match_id']] = record['id']

    def test_final_to_administrative_preserves_receipt_and_removes_training_row(self):
        for mode in ('json', 'sqlite'):
            for code in ('CANC', 'ABD', 'AWD', 'WO', 'PST', 'SUSP', 'INT'):
                with self.subTest(storage=mode, status=code):
                    path = self.path(mode, code)
                    api.save_fixtures([fixture()], path)
                    received = fixture(status=code)
                    api.save_fixtures([received], path)
                    self.assertEqual(api._status_short(self.saved(path)), code)
                    self.assertEqual(api.load_matches(path)[0], [])
                    records = self.observations(path)
                    self.assertEqual(records[-1]['decision'], 'status_correction')
                    self.assertEqual(records[-1]['received_raw'], received)
                    self.assert_chain(records)

    def test_live_regression_never_replaces_final_or_invents_a_quarantine(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                api.save_fixtures([fixture()], path)
                final = deepcopy(self.saved(path))
                for code in ('NS', '1H', 'HT', '2H', 'LIVE'):
                    api.save_fixtures([fixture(status=code)], path)
                    self.assertEqual(self.saved(path), final)
                    self.assertEqual(len(api.load_matches(path)[0]), 1)
                    self.assertEqual(self.observations(path)[-1]['decision'], 'retained_final')

    def test_incomplete_final_keeps_original_scores_but_blocks_normalization(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                previous = fixture(); received = deepcopy(previous)
                received['score']['halftime'] = dict(home=None, away=None)
                api.save_fixtures([previous], path)
                api.save_fixtures([received], path)
                selected = self.saved(path)
                self.assertEqual(selected['score'], previous['score'])
                self.assertEqual(policy.provider_raw(selected), previous)
                self.assertEqual(policy.quarantine(selected)['reason'], 'incomplete_final')
                with self.assertRaisesRegex(ValueError, 'Quarantaine'):
                    api.normalize_fixture(selected)
                rows, audit = api.load_matches(path)
                self.assertEqual(rows, [])
                self.assertIn('Quarantaine', audit['issues'][0]['reason'])
                self.assertEqual(self.observations(path)[-1]['received_raw'], received)

    def test_later_complete_consistent_final_clears_quality_regression(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                complete = fixture(); bad = deepcopy(complete)
                bad['score']['halftime'] = dict(home=None, away=None)
                api.save_fixtures([complete, bad], path)
                self.assertEqual(len(api.load_matches(path)[0]), 0)
                api.save_fixtures([complete], path)
                self.assertNotIn(policy.QUALITY_KEY, self.saved(path))
                self.assertEqual(len(api.load_matches(path)[0]), 1)
                self.assertEqual(len(self.observations(path)), 3)
                self.assert_chain(self.observations(path))

    def test_changed_identity_keeps_prior_and_excludes_training(self):
        for mode in ('json', 'sqlite'):
            for identity_field in ('league', 'home', 'away'):
                with self.subTest(storage=mode, field=identity_field):
                    path = self.path(mode) if identity_field == 'league' else self.root / f'{mode}-{identity_field}' / 'fixtures.json'
                    if identity_field != 'league':
                        api.save_fixtures([], path)
                        if mode == 'sqlite': store.migrate_json(path)
                    original = fixture(); changed = deepcopy(original)
                    if identity_field == 'league': changed['league']['id'] = 999
                    else: changed['teams'][identity_field]['id'] = 999
                    api.save_fixtures([original, changed], path)
                    self.assertEqual(policy.provider_raw(self.saved(path)), original)
                    self.assertEqual(policy.quarantine(self.saved(path))['reason'], 'identity_changed')
                    self.assertEqual(api.load_matches(path)[0], [])

    def test_final_after_cancellation_requires_review_instead_of_restoring_training(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                api.save_fixtures([fixture(), fixture(status='CANC'), fixture()], path)
                selected = self.saved(path)
                self.assertEqual(api._status_short(selected), 'CANC')
                self.assertEqual(policy.quarantine(selected)['reason'], 'final_after_administrative')
                self.assertEqual(api.load_matches(path)[0], [])
                self.assertEqual(self.observations(path)[-1]['received_raw'], fixture())

    def test_identical_receipts_are_deduplicated_even_when_quarantined(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                complete = fixture(); bad = deepcopy(complete); bad['score']['halftime'] = {}
                api.save_fixtures([complete], path)
                api.save_fixtures([complete], path)
                self.assertEqual(len(self.observations(path)), 1)
                api.save_fixtures([bad], path)
                original_selected = deepcopy(self.saved(path))
                for _ in range(3): api.save_fixtures([bad], path)
                self.assertEqual(len(self.observations(path)), 2)
                self.assertEqual(self.saved(path), original_selected)

    def test_alternating_receipts_keep_every_transition(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                a = fixture(); b = deepcopy(a); b['score']['halftime'] = dict(home=0, away=0)
                for value in (a, b, a, b): api.save_fixtures([value], path)
                records = self.observations(path)
                self.assertEqual(len(records), 4)
                self.assertEqual([r['received_raw'] for r in records], [a, b, a, b])
                self.assert_chain(records)

    def test_legacy_baseline_is_recovered_now_without_fake_old_receipt(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.root / mode / 'fixtures.json'; path.parent.mkdir()
                original = fixture()
                path.write_text(json.dumps(dict(version=1, fixtures=[original], season_imports=[])))
                if mode == 'sqlite': store.migrate_json(path)
                changed = deepcopy(original); changed['score']['halftime'] = {}
                api.save_fixtures([changed], path)
                records = self.observations(path)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['baseline_raw'], original)
                self.assertEqual(records[0]['baseline_time_kind'], 'recovered_at_first_tracked_import')
                self.assertIsNone(records[0]['previous_observation'])

    def test_migration_and_export_preserve_revisions_and_quality_marker(self):
        path = self.path('json')
        good = fixture(); bad = deepcopy(good); bad['score']['halftime'] = {}
        api.save_fixtures([good, bad], path)
        prior = api.export_store(path)
        store.migrate_json(path)
        exported = api.export_store(path)
        self.assertEqual(exported['fixtures'], prior['fixtures'])
        self.assertEqual(exported['import_revisions'], prior['import_revisions'])
        self.assertEqual(self.observations(path), prior['import_revisions'])
        restored = self.root / 'restored' / 'fixtures.json'; restored.parent.mkdir()
        restored.write_text(json.dumps(exported))
        self.assertEqual(api.load_matches(restored)[0], [])
        self.assertEqual(api.load_import_observations(restored), prior['import_revisions'])
        store.migrate_json(restored)
        self.assertEqual(api.load_import_observations(restored), prior['import_revisions'])

    def test_sql_observations_are_append_only(self):
        path = self.path('sqlite'); api.save_fixtures([fixture()], path)
        database = store.database_path(path)
        with sqlite3.connect(database) as con:
            for query in ('UPDATE fixture_import_revisions SET observed_at_utc="changed"',
                          'DELETE FROM fixture_import_revisions'):
                with self.subTest(query=query):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                        con.execute(query)
        self.assertEqual(len(self.observations(path)), 1)

    def test_invalid_batch_rolls_back_raw_quality_and_observations(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); api.save_fixtures([fixture()], path)
                before = api.export_store(path)
                bad = deepcopy(fixture()); bad['score']['halftime'] = {}
                for invalid in ({'fixture': {'id': False}}, dict(fixture(456), extra=float('nan'))):
                    with self.assertRaises(api.ApiFootballError):
                        api.save_fixtures([bad, invalid], path)
                    self.assertEqual(api.export_store(path), before)

    def test_sql_failure_after_receipt_insert_rolls_back_receipts_and_fixture_changes(self):
        path = self.path('sqlite'); api.save_fixtures([fixture()], path)
        before = api.export_store(path)
        with sqlite3.connect(store.database_path(path)) as con:
            con.execute("CREATE TRIGGER reject_456 BEFORE INSERT ON fixtures WHEN NEW.match_id='456' "
                        "BEGIN SELECT RAISE(ABORT,'isolated constraint'); END")
        changed = fixture(); changed['score']['halftime'] = {}
        with self.assertRaises(api.ApiFootballError):
            api.save_fixtures([changed, fixture(456)], path)
        self.assertEqual(api.export_store(path), before)
        self.assertEqual(len(api.load_matches(path)[0]), 1)

    def test_annotated_final_cannot_settle_a_group_using_preserved_scores(self):
        import prediction_journal as journal
        import api_match_workspace as calendar
        from datetime import timezone
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); good = fixture(); missing = deepcopy(good)
                missing['score']['halftime'] = {}
                frozen = calendar.fixture_from_raw(good)
                api.save_fixtures([good, missing], path)
                saved = dict(fixtures=[frozen], created_at='2025-08-12T10:00:00Z', methods={}, size=1)
                result = journal._settle(saved, {'123': self.saved(path)},
                                        datetime(2025,8,13,12,tzinfo=timezone.utc))
                self.assertEqual(result['status'], 'unresolved')
                self.assertEqual(result['observed'], [])
                self.assertIn('Quarantaine', result['issues'][0])
                self.assertEqual(result['evaluations'], {})

    def test_live_intermediate_cannot_bypass_administrative_reversal_review(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                api.save_fixtures([fixture(), fixture(status='CANC'), fixture(status='NS'), fixture()], path)
                selected = self.saved(path)
                self.assertEqual(policy.status(selected), 'CANC')
                self.assertTrue(policy.quarantine(selected))
                self.assertEqual(api.load_matches(path)[0], [])
                records = self.observations(path)
                self.assertEqual([policy.status(r['received_raw']) for r in records], ['FT', 'CANC', 'NS', 'FT'])

    def test_review_prior_stays_effective_for_identical_reply_but_not_new_payload(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                good = fixture(); missing = deepcopy(good); missing['score']['halftime'] = {}
                api.save_fixtures([good, missing], path)
                latest = self.observations(path)[-1]
                api.review_import(123, path=path, choice='prior', expected_observation=latest['id'], reason='Score vérifié')
                count = len(self.observations(path))
                api.save_fixtures([missing, missing], path)
                self.assertIsNone(policy.quarantine(self.saved(path)))
                self.assertEqual(len(api.load_matches(path)[0]), 1)
                self.assertEqual(len(self.observations(path)), count)
                different = deepcopy(missing); different['score']['fulltime']['home'] += 1
                api.save_fixtures([different], path)
                self.assertTrue(policy.quarantine(self.saved(path)))
                self.assertEqual(len(api.load_matches(path)[0]), 0)

    def test_annotated_export_is_not_fresh_provider_confirmation(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); good = fixture(); bad = deepcopy(good); bad['score']['halftime'] = {}
                api.save_fixtures([good, bad], path)
                before = api.export_store(path)
                with self.assertRaises(api.ApiFootballError):
                    api.save_fixtures([self.saved(path)], path)
                self.assertEqual(api.export_store(path), before)

    def test_review_prior_restores_valid_scores_and_preserves_received_raw(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); original = fixture(); missing = deepcopy(original)
                missing['score']['halftime'] = {}
                api.save_fixtures([original, missing], path)
                latest = self.observations(path)[-1]
                event = api.review_import(123, path=path, choice='prior', expected_observation=latest['id'],
                                          reason='Prior score verified in isolated test')
                self.assertEqual(self.saved(path), original)
                self.assertEqual(len(api.load_matches(path)[0]), 1)
                self.assertEqual(event['review']['choice'], 'prior')
                self.assertEqual(event['decision'], 'reviewed')
                self.assertEqual(event['previous_observation'], latest['id'])
                self.assertEqual(event['received_raw'], missing)
                self.assertEqual(event['selected_raw_sha256'], policy.digest(original))
                self.assert_chain(self.observations(path))

    def test_review_cannot_accept_received_missing_scores(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); original = fixture(); missing = deepcopy(original)
                missing['score']['halftime'] = {}
                api.save_fixtures([original, missing], path)
                before = api.export_store(path); latest = self.observations(path)[-1]
                with self.assertRaises(ValueError):
                    api.review_import(123, path=path, choice='received', expected_observation=latest['id'],
                                      reason='Cannot manufacture the missing halftime')
                self.assertEqual(api.export_store(path), before)

    def test_review_compare_and_swap_rejects_an_observation_that_has_changed(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); original = fixture(); missing = deepcopy(original)
                missing['score']['halftime'] = {}
                api.save_fixtures([original, missing], path)
                stale = self.observations(path)[-1]['id']
                second_missing = deepcopy(missing); second_missing['fixture']['referee'] = 'New observation'
                api.save_fixtures([second_missing], path)
                before = api.export_store(path)
                with self.assertRaises(ValueError):
                    api.review_import(123, path=path, choice='prior', expected_observation=stale,
                                      reason='Do not apply a stale review')
                self.assertEqual(api.export_store(path), before)

    def test_review_requires_quarantine_and_an_explicit_acceptable_reason(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); good = fixture(); bad = deepcopy(good); bad['score']['halftime'] = {}
                api.save_fixtures([good, bad], path)
                latest = self.observations(path)[-1]
                before = api.export_store(path)
                for choice, reason in [('unknown', 'Meaningful reason'), ('prior', '  '), ('prior', 'x' * 501)]:
                    with self.subTest(choice=choice, reason_length=len(reason)):
                        with self.assertRaises(ValueError):
                            api.review_import(123, path=path, choice=choice, expected_observation=latest['id'], reason=reason)
                        self.assertEqual(api.export_store(path), before)
                api.save_fixtures([good], path)
                after_confirmation = api.export_store(path)
                with self.assertRaises(ValueError):
                    api.review_import(123, path=path, choice='prior', expected_observation=latest['id'],
                                      reason='No longer a quarantined observation')
                self.assertEqual(api.export_store(path), after_confirmation)

    def test_review_can_accept_a_complete_received_identity_change(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode); previous = fixture(); received = deepcopy(previous)
                received['teams']['home']['id'] = 77
                received['teams']['home']['name'] = 'Verified new club'
                api.save_fixtures([previous, received], path)
                latest = self.observations(path)[-1]
                event = api.review_import(123, path=path, choice='received', expected_observation=latest['id'],
                                          reason='Identity manually verified in test')
                self.assertEqual(self.saved(path), received)
                self.assertEqual(api.load_matches(path)[0][0]['home_id'], '77')
                self.assertEqual(event['selected_raw_sha256'], policy.digest(received))
                self.assert_chain(self.observations(path))

    def test_review_can_accept_complete_final_after_an_administrative_decision(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                api.save_fixtures([fixture(status='CANC'), fixture()], path)
                latest = self.observations(path)[-1]
                api.review_import(123, path=path, choice='received', expected_observation=latest['id'],
                                  reason='Final result independently verified in test')
                self.assertEqual(api._status_short(self.saved(path)), 'FT')
                self.assertEqual(len(api.load_matches(path)[0]), 1)
                self.assert_chain(self.observations(path))

    def test_review_received_league_change_invalidates_old_season_manifest(self):
        for mode in ('json', 'sqlite'):
            with self.subTest(storage=mode):
                path = self.path(mode)
                marker = dict(competition_code='61', season=2025, match_ids=['123'])
                api.save_fixtures([fixture()], path, season_imports=[marker])
                self.assertEqual(len(api.load_season_imports(path)), 1)
                received = fixture(); received['league']['id'] = 99
                api.save_fixtures([received], path)
                latest = self.observations(path)[-1]
                api.review_import(123, path=path, choice='received', expected_observation=latest['id'],
                                  reason='Competition identity independently verified')
                self.assertEqual(api.load_season_imports(path), [])
                self.assertEqual(api.load_matches(path)[0][0]['league'], '99')


if __name__ == '__main__':
    unittest.main()
