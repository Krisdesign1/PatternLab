import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import football_data as football


def fixture(match_id=123, status="FINISHED", duration="REGULAR", season=2025):
    return {
        "id": match_id, "status": status, "utcDate": "2025-08-12T21:00:00+02:00",
        "area": {"name": "France"},
        "competition": {"id": 2015, "code": "FL1", "name": "Ligue 1"},
        "season": {"startDate": f"{season}-08-01", "endDate": f"{season+1}-05-31"},
        "homeTeam": {"id": 1, "name": "Home"}, "awayTeam": {"id": 2, "name": "Away"},
        "score": {"duration": duration, "halfTime": {"home": 1, "away": 0},
                  "fullTime": {"home": 2, "away": 1}},
    }


def response(payload, headers=None):
    stream = io.BytesIO(json.dumps(payload).encode())
    stream.headers = headers or {}
    return stream


class FixtureTests(unittest.TestCase):
    def test_normalization_converts_to_utc_and_preserves_provider_identity(self):
        row = football.normalize_fixture(fixture())
        self.assertEqual((row["ht"], row["ft"], row["value"]), ("1-0", "2-1", "2nd"))
        self.assertEqual(row["timestamp"], "2025-08-12T19:00:00")
        self.assertEqual((row["match_id"], row["league"], row["competition_code"]), ("123", "2015", "FL1"))
        self.assertEqual((row["home_id"], row["away_id"], row["season"], row["provider"]), ("1", "2", 2025, "football-data.org"))

    def test_overtime_and_shootouts_use_regular_time_instead_of_full_time(self):
        for duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
            with self.subTest(duration=duration):
                raw = fixture(duration=duration)
                raw["score"]["fullTime"] = {"home": 8, "away": 7}
                raw["score"]["regularTime"] = {"home": 1, "away": 1}
                raw["score"]["extraTime"] = {"home": 3, "away": 3}
                raw["score"]["penalties"] = {"home": 5, "away": 4}
                row = football.normalize_fixture(raw)
                self.assertEqual((row["ft"], row["value"]), ("1-1", "Égal"))

    def test_overtime_without_regular_time_is_excluded(self):
        for duration in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                football.normalize_fixture(fixture(duration=duration))

    def test_null_and_invalid_scores_never_become_zero(self):
        for field, value in (("halfTime", None), ("fullTime", None), ("halfTime", True),
                             ("fullTime", -1), ("fullTime", "2")):
            with self.subTest(field=field, value=value):
                raw = fixture()
                raw["score"][field]["home"] = value
                with self.assertRaises(ValueError):
                    football.normalize_fixture(raw)

    def test_missing_duration_and_inconsistent_periods_are_rejected(self):
        raw = fixture(duration=None)
        with self.assertRaisesRegex(ValueError, "Durée"):
            football.normalize_fixture(raw)
        raw = fixture()
        raw["score"]["fullTime"]["home"] = 0
        with self.assertRaisesRegex(ValueError, "HT supérieur"):
            football.normalize_fixture(raw)

    def test_calendar_unknown_timezone_and_colliding_teams_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non terminé"):
            football.normalize_fixture(fixture(status="TIMED"))
        raw = fixture()
        raw["utcDate"] = "2025-08-12T21:00:00"
        with self.assertRaisesRegex(ValueError, "Fuseau"):
            football.normalize_fixture(raw)
        raw = fixture()
        raw["awayTeam"]["id"] = 1
        with self.assertRaises(ValueError):
            football.normalize_fixture(raw)

    def test_utc_day_boundary_is_used_for_training_day(self):
        raw = fixture()
        raw["utcDate"] = "2025-08-12T00:30:00+02:00"
        row = football.normalize_fixture(raw)
        self.assertEqual((row["timestamp"], row["day"]), ("2025-08-11T22:30:00", "2025-08-11"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "matches.json"

    def test_upsert_is_idempotent_updates_status_and_keeps_other_seasons(self):
        self.assertEqual(football.load_fixtures(self.path), [])
        other = fixture(456, season=2024)
        self.assertEqual(football.save_fixtures([fixture(status="TIMED"), other], self.path), 2)
        self.assertEqual(football.save_fixtures([fixture()], self.path), 2)
        self.assertEqual(football.save_fixtures([fixture()], self.path), 2)
        self.assertEqual(football.load_fixtures(self.path), [fixture(), other])
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["version"], 1)
        self.assertIn("updated_at", payload)

    def test_full_season_manifest_is_validated_and_preserved_on_daily_upserts(self):
        batch = [fixture(), fixture(456, status="TIMED")]
        marker = dict(competition_code="FL1", season=2025, match_ids=["123", "456"])
        football.save_fixtures(batch, self.path, season_imports=[marker])
        imports = football.load_season_imports(self.path)
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["league"], "2015")
        self.assertEqual(imports[0]["received_count"], 2)
        self.assertTrue(imports[0]["complete"])
        football.save_fixtures([fixture(789, season=2026, status="TIMED")], self.path)
        self.assertEqual(football.load_season_imports(self.path), imports)
        football.save_fixtures([fixture(789, season=2026)], self.path,
                              season_imports=[dict(competition_code="FL1", season=2026,
                                                   league=2015, match_ids=[789])])
        self.assertEqual({row["season"] for row in football.load_season_imports(self.path)}, {2025, 2026})
        self.assertEqual(len(football.load_fixtures(self.path)), 3)

    def test_valid_empty_season_import_is_recorded_without_inventing_matches(self):
        marker = dict(competition_code="FL1", season=2027, match_ids=[])
        football.save_fixtures([], self.path, season_imports=[marker])
        imports = football.load_season_imports(self.path)
        self.assertEqual(football.load_fixtures(self.path), [])
        self.assertEqual((imports[0]["season"], imports[0]["received_count"]), (2027, 0))
        self.assertIsNone(imports[0]["league"])

    def test_partial_or_mismatched_new_manifests_never_modify_existing_store(self):
        football.save_fixtures([fixture(999, season=2024)], self.path)
        original = self.path.read_bytes()
        batch = [fixture(), fixture(456)]
        markers = [
            dict(competition_code="FL1", season=2025, complete=False, match_ids=[123, 456]),
            dict(competition_code="FL1", season=2025, match_ids=[123]),
            dict(competition_code="PL", season=2025, match_ids=[123, 456]),
            dict(competition_code="FL1", season=2026, match_ids=[123, 456]),
            dict(competition_code="FL1", season=2025, league=999, match_ids=[123, 456]),
            dict(competition_code="FL1", season=2025, match_ids=[123, 123]),
            dict(competition_code="FL1", season=2025, match_ids=[123, 999999]),
        ]
        for marker in markers:
            with self.subTest(marker=marker), self.assertRaises(football.FootballDataError):
                football.save_fixtures(batch, self.path, season_imports=[marker])
            self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_stores_and_invalid_markers_do_not_claim_season_coverage(self):
        football.save_fixtures([fixture()], self.path)
        self.assertEqual(football.load_season_imports(self.path), [])
        marker = dict(competition_code="FL1", season=2025, match_ids=[123])
        football.save_fixtures([fixture()], self.path, season_imports=[marker])
        valid = json.loads(self.path.read_text())
        for replacement in ({"complete": False}, {"received_count": 0}, {"league": "999"},
                            {"match_ids": ["999999"]}, {"match_ids": ["123", "123"]},
                            {"season": 2024}, {"imported_at": "invalid"},
                            {"imported_at": "2026-10-18T12:00:00"}):
            with self.subTest(replacement=replacement):
                payload = json.loads(json.dumps(valid))
                payload["season_imports"][0].update(replacement)
                self.path.write_text(json.dumps(payload))
                self.assertEqual(football.load_season_imports(self.path), [])
                self.assertEqual(football.load_fixtures(self.path), [fixture()])

    def test_failed_atomic_write_cannot_commit_matches_or_their_season_marker(self):
        football.save_fixtures([fixture(999, season=2024)], self.path)
        original = self.path.read_bytes()
        marker = dict(competition_code="FL1", season=2025, match_ids=[123])
        with patch.object(football.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(football.FootballDataError):
                football.save_fixtures([fixture()], self.path, season_imports=[marker])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(football.load_season_imports(self.path), [])

    def test_stale_provider_updates_are_ignored_even_when_imported_last(self):
        newer = fixture(status="TIMED")
        newer["lastUpdated"] = "2026-10-17T22:25:14Z"
        newer["utcDate"] = "2026-10-20T19:00:00Z"
        stale = fixture(status="SCHEDULED")
        stale["lastUpdated"] = "2026-10-11T01:32:05Z"
        football.save_fixtures([newer], self.path)
        football.save_fixtures([stale], self.path)
        self.assertEqual(football.load_fixtures(self.path), [newer])

    def test_finished_results_never_roll_back_to_calendar_or_live_states(self):
        finished = fixture()
        finished["lastUpdated"] = "2026-10-17T22:25:14Z"
        for status in ("TIMED", "SCHEDULED", "IN_PLAY", "PAUSED", "POSTPONED", "CANCELLED"):
            for updated in (None, "2026-10-18T12:00:00Z", "invalid", "2026-10-18T12:00:00"):
                with self.subTest(status=status, updated=updated):
                    football.save_fixtures([finished], self.path)
                    incoming = fixture(status=status)
                    incoming["score"]["halfTime"] = {"home": None, "away": None}
                    incoming["score"]["fullTime"] = {"home": None, "away": None}
                    if updated is not None:
                        incoming["lastUpdated"] = updated
                    football.save_fixtures([incoming], self.path)
                    self.assertEqual(football.load_fixtures(self.path), [finished])

    def test_newer_final_corrections_and_nonfinal_to_final_updates_still_apply(self):
        scheduled = fixture(status="TIMED")
        scheduled["lastUpdated"] = "2026-10-11T01:32:05Z"
        finished = fixture()
        finished["lastUpdated"] = "2026-10-17T22:25:14Z"
        football.save_fixtures([scheduled], self.path)
        football.save_fixtures([finished], self.path)
        self.assertEqual(football.load_fixtures(self.path), [finished])
        corrected = fixture()
        corrected["lastUpdated"] = "2026-10-18T12:00:00Z"
        corrected["score"]["fullTime"] = {"home": 3, "away": 1}
        corrected["referees"] = [{"id": 1, "name": "Confirmed referee"}]
        football.save_fixtures([corrected], self.path)
        self.assertEqual(football.load_fixtures(self.path), [corrected])
        football.save_fixtures([finished], self.path)
        self.assertEqual(football.load_fixtures(self.path), [corrected])

    def test_freshness_compares_instants_not_iso_string_order(self):
        previous = fixture(status="TIMED")
        previous["lastUpdated"] = "2026-10-18T12:00:00+02:00"
        newer = fixture(status="TIMED")
        newer["lastUpdated"] = "2026-10-18T10:30:00Z"
        newer["utcDate"] = "2026-10-20T18:00:00Z"
        football.save_fixtures([previous], self.path)
        football.save_fixtures([newer], self.path)
        self.assertEqual(football.load_fixtures(self.path), [newer])

    def test_uncomparable_timestamps_do_not_block_final_score_corrections(self):
        for old_stamp, new_stamp in ((None, None), ("bad", "2026-10-18T12:00:00Z"),
                                     ("2026-10-18T12:00:00", "2026-10-17T12:00:00Z")):
            with self.subTest(old=old_stamp, new=new_stamp):
                before, after = fixture(), fixture()
                if old_stamp is not None:
                    before["lastUpdated"] = old_stamp
                if new_stamp is not None:
                    after["lastUpdated"] = new_stamp
                after["score"]["fullTime"] = {"home": 3, "away": 1}
                self.path.unlink(missing_ok=True)
                football.save_fixtures([before], self.path)
                football.save_fixtures([after], self.path)
                self.assertEqual(football.load_fixtures(self.path), [after])

    def test_stale_or_nonfinal_updates_leave_other_competitions_and_seasons_intact(self):
        finished = fixture()
        finished["lastUpdated"] = "2026-10-17T22:25:14Z"
        other = fixture(456, season=2024)
        other["competition"] = {"id": 2021, "code": "PL", "name": "Premier League"}
        football.save_fixtures([finished, other], self.path)
        stale = fixture(status="TIMED")
        stale["lastUpdated"] = "2026-10-11T01:32:05Z"
        football.save_fixtures([stale, fixture(789)], self.path)
        self.assertEqual(football.load_fixtures(self.path), [finished, other, fixture(789)])

    def test_history_service_cannot_erase_an_existing_final_result(self):
        from match_workspace import ensure_history
        final = fixture()
        final["lastUpdated"] = "2026-10-17T22:25:14Z"
        incoming = fixture(status="TIMED")
        incoming["lastUpdated"] = "2026-10-18T12:00:00Z"
        football.save_fixtures([final], self.path)
        client = MagicMock(last_quota={})
        client.matches.return_value = {"matches": [incoming]}
        with patch.object(football, "load_key", return_value="fake-token"), \
             patch.object(football, "FootballDataClient", return_value=client):
            report = ensure_history("FL1", [2025], path=self.path)
        self.assertEqual(football.load_fixtures(self.path), [final])
        self.assertEqual(report["history_count"], 1)

    def test_invalid_second_history_http_payload_does_not_partially_write(self):
        from match_workspace import ensure_history
        football.save_fixtures([fixture(999, season=2024)], self.path)
        original = self.path.read_bytes()
        client = MagicMock(last_quota={})
        client.matches.side_effect = [{"matches": [fixture()]}, {"matches": "invalid HTTP payload"}]
        with patch.object(football, "load_key", return_value="fake-token"), \
             patch.object(football, "FootballDataClient", return_value=client):
            with self.assertRaises(football.FootballDataError):
                ensure_history("FL1", [2025, 2026], path=self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_audit_only_counts_finished_matches_and_reports_missing_halftime(self):
        invalid = fixture(456)
        invalid["score"]["halfTime"]["home"] = None
        football.save_fixtures([fixture(), invalid, fixture(789, status="SCHEDULED")], self.path)
        rows, audit = football.load_matches(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual((audit["expected"], audit["matched"]), (2, 1))
        self.assertEqual(audit["issues"][0]["match_id"], "456")

    def test_write_failure_keeps_previous_file_and_removes_temporary_file(self):
        football.save_fixtures([fixture()], self.path)
        original = self.path.read_bytes()
        with patch.object(football.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(football.FootballDataError):
                football.save_fixtures([fixture(456)], self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_malformed_status_is_audited_without_losing_valid_results(self):
        invalid = fixture(456)
        invalid['status'] = []
        football.save_fixtures([fixture(), invalid], self.path)
        rows, audit = football.load_matches(self.path)
        self.assertEqual([row['match_id'] for row in rows], ['123'])
        self.assertEqual((audit['expected'], audit['matched']), (2, 1))
        self.assertIn('Statut', audit['issues'][0]['reason'])

    def test_corruption_refuses_import_without_overwriting_original_file(self):
        self.path.parent.mkdir()
        self.path.write_text("corrupt JSON")
        original = self.path.read_bytes()
        with self.assertRaises(football.FootballDataError):
            football.save_fixtures([fixture()], self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_duplicate_stored_ids_and_invalid_incoming_ids_are_rejected(self):
        football.save_fixtures([fixture()], self.path)
        original = self.path.read_bytes()
        with self.assertRaises(football.FootballDataError):
            football.save_fixtures([fixture(match_id=False)], self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text(json.dumps({"version": 1, "matches": [fixture(), fixture()]}))
        with self.assertRaises(football.FootballDataError):
            football.load_fixtures(self.path)

    def test_signature_changes_after_save(self):
        before = football.data_signature(self.path)
        football.save_fixtures([fixture()], self.path)
        self.assertNotEqual(before, football.data_signature(self.path))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.key = "unit-test-token-never-real"
        self.client = football.FootballDataClient(self.key)

    def test_header_only_token_verified_https_timeout_and_quota_counters(self):
        stream = response({"matches": []}, {"X-RequestsAvailable": "9", "X-RequestCounter-Reset": "44",
                                             "X-Authenticated-Client": "private-account-123"})
        context = object()
        with patch.object(football, "_ssl_context", return_value=context), patch.object(football, "urlopen", return_value=stream) as opened:
            payload = self.client.matches("FL1", season=2025, unused=None)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("X-auth-token"), self.key)
        self.assertNotIn(self.key, request.full_url)
        self.assertEqual(request.full_url, football.BASE_URL + "/competitions/FL1/matches?season=2025")
        self.assertEqual(opened.call_args.kwargs, {"timeout": 25, "context": context})
        self.assertEqual(payload, {"matches": []})
        self.assertEqual(self.client.last_quota, {"requests_available_minute": 9, "reset_seconds": 44})
        self.assertNotIn("private-account", str(self.client.last_quota))

    def test_forbidden_and_quota_http_errors_are_safe_and_never_retried(self):
        for status in (403, 429):
            with self.subTest(status=status):
                error = HTTPError("https://example.test", status, self.key,
                                  {"X-RequestsAvailable": "0", "X-RequestCounter-Reset": "30"}, None)
                with patch.object(football, "urlopen", side_effect=error) as opened:
                    with self.assertRaises(football.FootballDataError) as raised:
                        self.client.competitions()
                self.assertNotIn(self.key, str(raised.exception))
                self.assertEqual(self.client.last_quota, {"requests_available_minute": 0, "reset_seconds": 30})
                opened.assert_called_once()

    def test_provider_message_expunges_token_even_with_http_200(self):
        stream = response({"errorCode": 403, "message": "Access denied for " + self.key})
        with patch.object(football, "urlopen", return_value=stream):
            with self.assertRaises(football.FootballDataError) as raised:
                self.client.competitions()
        self.assertNotIn(self.key, str(raised.exception))
        self.assertIn("[clé masquée]", str(raised.exception))

    def test_network_error_does_not_expose_token_or_stale_quota(self):
        self.client.last_quota = {"requests_available_minute": 9}
        with patch.object(football, "urlopen", side_effect=URLError(self.key)):
            with self.assertRaises(football.FootballDataError) as raised:
                self.client.competitions()
        self.assertNotIn(self.key, str(raised.exception))
        self.assertEqual(self.client.last_quota, {})

    def test_invalid_and_private_header_values_are_not_saved_as_quota(self):
        stream = response({"competitions": []}, {"X-RequestsAvailableMinute": "account123", "X-RequestCounter-Reset": "-1",
                                                  "X-Authenticated-Client": self.key})
        with patch.object(football, "urlopen", return_value=stream):
            self.client.competitions()
        self.assertEqual(self.client.last_quota, {})

    def test_invalid_json_external_endpoints_and_competition_paths_are_refused(self):
        stream = io.BytesIO(b"not JSON")
        stream.headers = {}
        with patch.object(football, "urlopen", return_value=stream):
            with self.assertRaises(football.FootballDataError):
                self.client.competitions()
        with patch.object(football, "urlopen") as opened:
            with self.assertRaises(football.FootballDataError):
                self.client.get("https://untrusted.example/steal")
            with self.assertRaises(football.FootballDataError):
                self.client.matches("../../secret")
        opened.assert_not_called()

    def test_environment_key_takes_precedence_without_reading_secrets(self):
        with patch.dict(football.os.environ, {"FOOTBALL_DATA_KEY": " test-token "}), \
             patch.object(football.Path, "exists", side_effect=AssertionError("must not read secrets")):
            self.assertEqual(football.load_key(), "test-token")

    def test_local_toml_section_key_and_invalid_toml_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(football.os.environ, {}, clear=True):
            path = Path(tmp) / "secrets.toml"
            with patch.object(football, "SECRETS_PATH", path):
                path.write_text('[football_data]\nkey = "local-test-token"\n')
                self.assertEqual(football.load_key(), "local-test-token")
                path.write_text('[football_data]\nkey = invalid')
                with self.assertRaises(football.FootballDataError) as raised:
                    football.load_key()
                self.assertNotIn("invalid", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
