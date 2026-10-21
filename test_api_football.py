import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import api_football as api


def fixture(match_id=123, status="FT", season=2025):
    return {
        "fixture": {"id": match_id, "date": "2025-08-12T21:00:00+02:00", "status": {"short": status}},
        "league": {"id": 61, "name": "Ligue 1", "country": "France", "season": season},
        "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
        "goals": {"home": 9, "away": 9},
        "score": {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 2, "away": 1},
                  "extratime": {"home": 3, "away": 2}, "penalty": {"home": 5, "away": 4}},
    }


class FixtureTests(unittest.TestCase):
    def test_halftime_fulltime_and_utc_conversion(self):
        row = api.normalize_fixture(fixture())
        self.assertEqual((row["ht"], row["ft"], row["value"]), ("1-0", "2-1", "2nd"))
        self.assertEqual(row["timestamp"], "2025-08-12T19:00:00")
        self.assertEqual(row["day"], "2025-08-12")
        self.assertEqual((row["match_id"], row["league"], row["provider"]), ("123", "61", "api-football"))

    def test_aet_and_penalties_use_regulation_score(self):
        for status in ("AET", "PEN"):
            with self.subTest(status=status):
                raw = fixture(status=status)
                raw["score"]["fulltime"] = {"home": 1, "away": 1}
                row = api.normalize_fixture(raw)
                self.assertEqual((row["ft"], row["value"]), ("1-1", "Égal"))

    def test_missing_scores_never_become_zero(self):
        for period, value in (("halftime", None), ("fulltime", None), ("halftime", True),
                              ("fulltime", -1), ("fulltime", "2")):
            with self.subTest(period=period, value=value):
                raw = fixture()
                raw["score"][period]["home"] = value
                with self.assertRaises(ValueError):
                    api.normalize_fixture(raw)

    def test_inconsistent_and_unfinished_results_are_rejected(self):
        raw = fixture()
        raw["score"]["fulltime"]["home"] = 0
        with self.assertRaisesRegex(ValueError, "HT supérieur"):
            api.normalize_fixture(raw)
        with self.assertRaisesRegex(ValueError, "non terminé"):
            api.normalize_fixture(fixture(status="NS"))

    def test_naive_dates_and_colliding_teams_are_rejected(self):
        raw = fixture()
        raw["fixture"]["date"] = "2025-08-12T21:00:00"
        with self.assertRaisesRegex(ValueError, "Fuseau"):
            api.normalize_fixture(raw)
        raw = fixture()
        raw["teams"]["away"]["id"] = raw["teams"]["home"]["id"]
        with self.assertRaises(ValueError):
            api.normalize_fixture(raw)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "fixtures.json"

    def test_upsert_updates_status_and_preserves_other_seasons(self):
        first, other_season = fixture(status="NS"), fixture(456, season=2024)
        self.assertEqual(api.load_fixtures(self.path), [])
        self.assertEqual(api.save_fixtures([first, other_season], self.path), 2)
        changed = fixture()
        self.assertEqual(api.save_fixtures([changed], self.path), 2)
        self.assertEqual(api.save_fixtures([changed], self.path), 2)
        saved = api.load_fixtures(self.path)
        self.assertEqual(saved, [changed, other_season])
        payload = json.loads(self.path.read_text())
        self.assertEqual(payload["version"], 1)
        self.assertIn("updated_at", payload)

    def test_final_results_cannot_be_replaced_by_calendar_or_live_states(self):
        for final_status in ("FT", "AET", "PEN"):
            for incoming_status in ("NS", "TBD", "1H", "HT", "2H", "PST", "CANC"):
                with self.subTest(final=final_status, incoming=incoming_status):
                    final = fixture(status=final_status)
                    api.save_fixtures([final], self.path)
                    api.save_fixtures([fixture(status=incoming_status)], self.path)
                    self.assertEqual(api.load_fixtures(self.path), [final])

    def test_final_corrections_and_nonfinal_to_final_still_update_the_record(self):
        api.save_fixtures([fixture(status="NS"), fixture(456, season=2024)], self.path)
        final = fixture()
        api.save_fixtures([final], self.path)
        corrected = fixture()
        corrected["score"]["fulltime"] = {"home": 3, "away": 1}
        corrected["fixture"]["referee"] = "Confirmed referee"
        api.save_fixtures([corrected], self.path)
        self.assertEqual(api.load_fixtures(self.path), [corrected, fixture(456, season=2024)])

    def test_full_season_manifest_and_daily_update_keep_coverage_separate(self):
        batch = [fixture(), fixture(456, status="NS")]
        marker = dict(competition_code="61", season=2025, match_ids=[123, 456])
        api.save_fixtures(batch, self.path, season_imports=[marker])
        markers = api.load_season_imports(self.path)
        self.assertEqual((markers[0]["league"], markers[0]["received_count"]), ("61", 2))
        self.assertTrue(markers[0]["complete"])
        api.save_fixtures([fixture(789, status="NS", season=2026)], self.path)
        self.assertEqual(api.load_season_imports(self.path), markers)
        self.assertEqual(len(api.load_fixtures(self.path)), 3)

    def test_invalid_or_partial_new_season_manifest_is_rejected_atomically(self):
        api.save_fixtures([fixture(999, season=2024)], self.path)
        original = self.path.read_bytes()
        batch = [fixture(), fixture(456)]
        for marker in (
            dict(competition_code="61", season=2025, match_ids=[123]),
            dict(competition_code="39", season=2025, match_ids=[123, 456]),
            dict(competition_code="61", season=2025, league="39", match_ids=[123, 456]),
            dict(competition_code="61", season=2026, match_ids=[123, 456]),
            dict(competition_code="61", season=2025, match_ids=[123, 123]),
            dict(competition_code="61", season=2025, complete=False, match_ids=[123, 456]),
        ):
            with self.subTest(marker=marker), self.assertRaises(api.ApiFootballError):
                api.save_fixtures(batch, self.path, season_imports=[marker])
            self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_manifest_never_establishes_coverage_and_empty_import_is_valid(self):
        api.save_fixtures([fixture()], self.path)
        self.assertEqual(api.load_season_imports(self.path), [])
        marker = dict(competition_code="61", season=2025, match_ids=[123])
        api.save_fixtures([fixture()], self.path, season_imports=[marker])
        valid = json.loads(self.path.read_text())
        for change in ({"complete": False}, {"received_count": 0}, {"match_ids": ["999"]},
                       {"season": 2024}, {"league": "39"}, {"imported_at": "2026-10-18T12:00:00"}):
            payload = json.loads(json.dumps(valid))
            payload["season_imports"][0].update(change)
            self.path.write_text(json.dumps(payload))
            self.assertEqual(api.load_season_imports(self.path), [])
        empty = dict(competition_code="61", season=2026, match_ids=[])
        api.save_fixtures([], self.path, season_imports=[empty])
        self.assertEqual(api.load_season_imports(self.path)[0]["received_count"], 0)

    def test_failed_write_cannot_commit_a_season_manifest_without_its_matches(self):
        api.save_fixtures([fixture(999, season=2024)], self.path)
        original = self.path.read_bytes()
        marker = dict(competition_code="61", season=2025, match_ids=[123])
        with patch.object(api.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(api.ApiFootballError):
                api.save_fixtures([fixture()], self.path, season_imports=[marker])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(api.load_season_imports(self.path), [])

    def test_malformed_status_short_is_audited_instead_of_crashing(self):
        invalid = fixture(456)
        invalid["fixture"]["status"]["short"] = []
        api.save_fixtures([fixture(), invalid], self.path)
        rows, audit = api.load_matches(self.path)
        self.assertEqual([row["match_id"] for row in rows], ["123"])
        self.assertEqual(len(audit["issues"]), 1)

    def test_completed_matches_audit_missing_scores_but_ignore_schedule(self):
        invalid = fixture(456)
        invalid["score"]["halftime"]["home"] = None
        api.save_fixtures([fixture(), invalid, fixture(789, status="NS")], self.path)
        rows, audit = api.load_matches(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual((audit["expected"], audit["matched"]), (2, 1))
        self.assertEqual(audit["issues"][0]["match_id"], "456")

    def test_missing_status_is_reported_without_crashing(self):
        raw = fixture()
        raw["fixture"]["status"] = None
        api.save_fixtures([raw], self.path)
        rows, audit = api.load_matches(self.path)
        self.assertEqual(rows, [])
        self.assertEqual((audit["expected"], audit["matched"], len(audit["issues"])), (1, 0, 1))

    def test_failed_save_keeps_previous_store_intact(self):
        api.save_fixtures([fixture()], self.path)
        original = self.path.read_bytes()
        with patch.object(api.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(api.ApiFootballError):
                api.save_fixtures([fixture(456)], self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_invalid_local_store_is_reported_without_partial_loading(self):
        self.path.parent.mkdir()
        self.path.write_text('{"version":1,"fixtures":[{}]}')
        with self.assertRaises(api.ApiFootballError):
            api.load_fixtures(self.path)

    def test_corrupt_store_is_never_overwritten_by_an_import(self):
        self.path.parent.mkdir()
        self.path.write_text("corrupt JSON")
        original = self.path.read_bytes()
        with self.assertRaises(api.ApiFootballError):
            api.save_fixtures([fixture()], self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_incoming_ids_and_duplicate_saved_ids_are_refused(self):
        api.save_fixtures([fixture()], self.path)
        original = self.path.read_bytes()
        with self.assertRaises(api.ApiFootballError):
            api.save_fixtures([fixture(match_id=False)], self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text(json.dumps({"version": 1, "fixtures": [fixture(), fixture()]}))
        with self.assertRaises(api.ApiFootballError):
            api.load_fixtures(self.path)

    def test_signature_changes_after_a_save(self):
        before = api.data_signature(self.path)
        api.save_fixtures([fixture()], self.path)
        self.assertNotEqual(before, api.data_signature(self.path))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.key = "unit-test-key-never-real"
        self.client = api.ApiFootballClient(self.key)

    def test_key_is_a_header_only_and_ssl_timeout_are_used(self):
        response = io.BytesIO(json.dumps({"errors": [], "response": []}).encode())
        context = object()
        with patch.object(api, "_ssl_context", return_value=context), patch.object(api, "urlopen", return_value=response) as opened:
            result = self.client.fixtures(league=61, season=2025, unused=None)
        request = opened.call_args.args[0]
        self.assertNotIn(self.key, request.full_url)
        self.assertEqual(request.get_header("X-apisports-key"), self.key)
        self.assertIn("league=61", request.full_url)
        self.assertNotIn("unused", request.full_url)
        self.assertEqual(opened.call_args.kwargs, {"timeout": 25, "context": context})
        self.assertEqual(result["response"], [])

    def test_http_quota_failure_is_safe_and_not_retried(self):
        error = HTTPError("https://example.test", 429, self.key, {}, None)
        with patch.object(api, "urlopen", side_effect=error) as opened:
            with self.assertRaises(api.ApiFootballError) as raised:
                self.client.status()
        self.assertIn("Quota", str(raised.exception))
        self.assertNotIn(self.key, str(raised.exception))
        opened.assert_called_once()

    def test_provider_errors_are_sanitized_including_with_http_200(self):
        response = io.BytesIO(json.dumps({"errors": {"requests": "Quota for " + self.key}, "response": []}).encode())
        with patch.object(api, "urlopen", return_value=response):
            with self.assertRaises(api.ApiFootballError) as raised:
                self.client.leagues()
        self.assertNotIn(self.key, str(raised.exception))
        self.assertIn("[clé masquée]", str(raised.exception))

    def test_suspended_account_with_empty_response_is_reported(self):
        response = io.BytesIO(json.dumps({"errors": {"access": "Your account is suspended, check on https://dashboard.api-football.com."}, "response": []}).encode())
        with patch.object(api, "urlopen", return_value=response) as opened:
            with self.assertRaisesRegex(api.ApiFootballError, "account is suspended"):
                self.client.status()
        opened.assert_called_once()

    def test_network_errors_do_not_expose_their_details_or_key(self):
        with patch.object(api, "urlopen", side_effect=URLError(self.key)):
            with self.assertRaises(api.ApiFootballError) as raised:
                self.client.status()
        self.assertNotIn(self.key, str(raised.exception))

    def test_invalid_json_and_external_endpoints_are_rejected(self):
        with patch.object(api, "urlopen", return_value=io.BytesIO(b"not json")):
            with self.assertRaises(api.ApiFootballError):
                self.client.status()
        with patch.object(api, "urlopen") as opened:
            with self.assertRaises(api.ApiFootballError):
                self.client.get("https://untrusted.example/steal")
        opened.assert_not_called()

    def test_environment_key_takes_precedence_without_reading_secrets(self):
        with patch.dict(api.os.environ, {"API_FOOTBALL_KEY": " test-key "}), patch.object(api.Path, "exists", side_effect=AssertionError("must not read secrets")):
            self.assertEqual(api.load_key(), "test-key")

    def test_local_toml_key_and_invalid_toml(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(api.os.environ, {}, clear=True):
            path = Path(tmp) / "secrets.toml"
            with patch.object(api, "SECRETS_PATH", path):
                path.write_text('API_FOOTBALL_KEY = "local-test-key"\n')
                self.assertEqual(api.load_key(), "local-test-key")
                path.write_text('API_FOOTBALL_KEY = invalid')
                with self.assertRaises(api.ApiFootballError) as raised:
                    api.load_key()
                self.assertNotIn("invalid", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
