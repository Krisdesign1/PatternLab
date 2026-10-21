from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import api_football as api
import api_match_workspace as workspace


DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def raw(match_id=1, stamp="2026-10-18T12:00:00Z", status="NS", league=39, season=2026):
    return {"fixture": {"id": match_id, "date": stamp, "status": {"short": status}},
            "league": {"id": league, "season": season, "country": "England", "name": "Premier League"},
            "teams": {"home": {"id": 10, "name": "Home"}, "away": {"id": 20, "name": "Away"}},
            "score": {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 2, "away": 1}}}


def payload(matches, pages=1):
    return {"errors": [], "results": len(matches), "paging": {"current": 1, "total": pages}, "response": matches}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixtures.json"
        path_patch = patch.object(api, "STORE_PATH", self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def test_kinshasa_day_is_strictly_filtered_at_both_utc_boundaries(self):
        fixtures = [raw(1, "2026-10-17T22:59:59Z"), raw(2, "2026-10-17T23:00:00Z"),
                    raw(3, "2026-10-18T23:59:59+01:00"), raw(4, "2026-10-18T23:00:00Z")]
        api.save_fixtures(fixtures, self.path)
        self.assertEqual([row["fixture"]["id"] for row in workspace.local_day_matches(DAY)], [2, 3])

    def test_confirmed_future_matches_and_canonical_statuses(self):
        fixtures = [raw(i, status=status) for i, status in enumerate(
            ("NS", "TBD", "FT", "AET", "PEN", "HT", "1H", "PST", "CANC"), 1)]
        fixtures.append(raw(10, "2026-10-18T09:00:00Z"))
        unknown = raw(11)
        unknown["teams"]["home"] = {"id": None, "name": "TBD"}
        fixtures.append(unknown)
        rows = {row["match_id"]: row for row in workspace.fixtures_in_day(fixtures, DAY, NOW)}
        self.assertEqual([row["match_id"] for row in workspace.upcoming_in_day(fixtures, DAY, NOW)], ["1"])
        self.assertEqual(rows["1"]["status"], "TIMED")
        self.assertEqual(rows["1"]["provider"], "api-football")
        self.assertFalse(rows["2"]["selectable"])
        self.assertEqual(rows["2"]["status"], "SCHEDULED")
        self.assertTrue(all(rows[str(i)]["is_historical"] for i in (3, 4, 5)))
        self.assertEqual((rows["6"]["status"], rows["7"]["status"]), ("PAUSED", "IN_PLAY"))
        self.assertFalse(rows["8"]["selectable"])
        self.assertFalse(rows["10"]["selectable"])
        self.assertNotIn("11", rows)
        self.assertNotIn("score", rows["1"])
        self.assertNotIn("value", rows["1"])

    def test_local_snapshot_is_offline_and_competitions_use_ids_not_names(self):
        api.save_fixtures([raw(1), raw(2, status="FT"), raw(3, status="FT", league=40),
                           raw(4, "2025-09-01T12:00:00Z", status="FT", season=2025)], self.path)
        with patch.object(api, "load_key", side_effect=AssertionError("must stay offline")), \
             patch.object(api, "ApiFootballClient", side_effect=AssertionError("must stay offline")):
            snapshot = workspace.local_snapshot(DAY, now=NOW)
        self.assertEqual((snapshot["stored_count"], snapshot["usable_count"], snapshot["day_count"]), (4, 3, 3))
        self.assertEqual(snapshot["source_label"], "API-Football")
        self.assertEqual(snapshot["history_counts"], {"39": 2, "40": 1})
        self.assertEqual(set(snapshot["competitions"]), {"39", "40"})
        self.assertEqual(snapshot["competitions"]["39"]["code"], "39")
        self.assertEqual(snapshot["competitions"]["39"]["current_season"], 2026)
        self.assertEqual(snapshot["fixtures"][0]["provider"], "api-football")
        self.assertIsNone(snapshot["calendar_updated_at"])

    def test_missing_or_invalid_calendar_sidecar_does_not_claim_freshness_or_break_data(self):
        api.save_fixtures([raw()], self.path)
        sidecar = self.path.with_name("fixtures.calendar.json")
        cases = ("not JSON", "[]", '{"version":99,"source":"API-Football","calendar_updates":[]}',
                 '{"version":1,"source":"other","calendar_updates":[]}',
                 json.dumps(dict(version=1, source="API-Football", calendar_updates=[
                     dict(day=DAY.isoformat(), timezone="Africa/Kinshasa", updated_at="2026-10-18T12:00:00"),
                     dict(day=DAY.isoformat(), timezone="Invalid/Zone", updated_at="2026-10-18T12:00:00Z"),
                     None])))
        for content in cases:
            with self.subTest(content=content):
                sidecar.write_text(content)
                snapshot = workspace.local_snapshot(DAY, now=NOW)
                self.assertEqual(snapshot["day_count"], 1)
                self.assertIsNone(snapshot["calendar_updated_at"])

    def test_global_store_update_does_not_pretend_that_any_day_was_refreshed(self):
        api.save_fixtures([raw(status="FT")], self.path)
        snapshot = workspace.local_snapshot(DAY, now=NOW)
        self.assertIsNotNone(snapshot["updated_at"])
        self.assertIsNone(snapshot["calendar_updated_at"])
        api.save_fixtures([raw(2, "2025-09-01T12:00:00Z", "FT", season=2025)], self.path,
            season_imports=[dict(competition_code="39", season=2025, match_ids=[2])])
        self.assertIsNone(workspace.local_snapshot(DAY, now=NOW)["calendar_updated_at"])

    def test_naive_dates_or_clock_and_unknown_clubs_are_rejected(self):
        with self.assertRaises(ValueError):
            workspace.fixture_from_raw(raw(stamp="2026-10-18T12:00:00"))
        with self.assertRaises(ValueError):
            workspace.fixtures_in_day([raw()], DAY, datetime(2026, 10, 18, 10))
        invalid = raw()
        invalid["teams"]["home"]["name"] = "TBD"
        with self.assertRaises(ValueError):
            workspace.fixture_from_raw(invalid)


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "fixtures.json"
        self.key = "workspace-api-fake-token"
        self.client = MagicMock(last_quota={})
        for name, replacement in (("STORE_PATH", self.path), ("load_key", lambda: self.key),
                                  ("ApiFootballClient", lambda key: self.client)):
            patcher = patch.object(api, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_refresh_uses_one_local_date_request_and_preserves_old_seasons(self):
        api.save_fixtures([raw(99, "2025-09-01T12:00:00Z", "FT", season=2025)], self.path)
        batch = [raw(1, "2026-10-17T22:59:59Z"), raw(2, "2026-10-17T23:00:00Z"),
                 raw(3, "2026-10-18T22:59:59Z"), raw(4, "2026-10-18T23:00:00Z")]
        self.client.fixtures.return_value = payload(batch)
        result = workspace.refresh_day(DAY)
        self.client.fixtures.assert_called_once_with(date="2026-10-18", timezone="Africa/Kinshasa")
        self.assertEqual([row["fixture"]["id"] for row in result["matches"]], [2, 3])
        self.assertEqual(result["stored_count"], 5)
        self.assertEqual(result["received_count"], 4)
        self.assertEqual(result["quota"], {})
        self.assertEqual(workspace.missing_history("39", [2026]), [2026])
        self.assertNotIn(self.key, json.dumps(result))
        self.assertIsNotNone(result["calendar_updated_at"])

    def test_refresh_timestamps_are_scoped_to_exact_day_and_timezone(self):
        self.client.fixtures.return_value = payload([raw()])
        with patch.object(workspace, "_now", return_value=NOW):
            first = workspace.refresh_day(DAY)
        self.assertEqual(first["calendar_updated_at"], NOW.isoformat(timespec="seconds"))
        self.assertIsNone(workspace.local_snapshot(date(2026, 10, 19), now=NOW)["calendar_updated_at"])
        self.assertIsNone(workspace.local_snapshot(DAY, timezone_name="UTC", now=NOW)["calendar_updated_at"])
        later = datetime(2026, 10, 19, 11, tzinfo=timezone.utc)
        self.client.fixtures.return_value = payload([])
        with patch.object(workspace, "_now", return_value=later):
            workspace.refresh_day(date(2026, 10, 19))
        self.assertEqual(workspace.local_snapshot(DAY, now=NOW)["calendar_updated_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(workspace.local_snapshot(date(2026, 10, 19), now=later)["calendar_updated_at"], later.isoformat(timespec="seconds"))
        api.save_fixtures([raw(99, "2025-09-01T12:00:00Z", "FT", season=2025)], self.path)
        self.assertEqual(workspace.local_snapshot(DAY, now=NOW)["calendar_updated_at"], NOW.isoformat(timespec="seconds"))

    def test_refresh_errors_never_advance_the_previous_day_freshness(self):
        self.client.fixtures.return_value = payload([raw()])
        with patch.object(workspace, "_now", return_value=NOW):
            workspace.refresh_day(DAY)
        sidecar = self.path.with_name("fixtures.calendar.json")
        original = sidecar.read_bytes()
        self.client.fixtures.side_effect = api.ApiFootballError("quota " + self.key)
        with self.assertRaises(api.ApiFootballError):
            workspace.refresh_day(DAY)
        self.assertEqual(sidecar.read_bytes(), original)
        self.client.fixtures.side_effect = None
        self.client.fixtures.return_value = payload([raw()])
        real_replace = workspace.os.replace
        def fail_metadata(source, target):
            if Path(target) == sidecar:
                raise OSError("metadata write failed " + self.key)
            return real_replace(source, target)
        with patch.object(workspace.os, "replace", side_effect=fail_metadata):
            with self.assertRaises(api.ApiFootballError) as error:
                workspace.refresh_day(DAY)
        self.assertNotIn(self.key, str(error.exception))
        self.assertEqual(sidecar.read_bytes(), original)
        self.assertEqual(list(sidecar.parent.glob(".fixtures.calendar.json.*.tmp")), [])

    def test_paginated_invalid_duplicate_or_refused_refresh_cannot_write(self):
        api.save_fixtures([raw(99, status="FT")], self.path)
        original = self.path.read_bytes()
        for response in (payload([raw()], pages=2), {"response": "bad"}, payload([raw(), raw()]),
                         {"errors": {"access": "refused " + self.key}, "response": []},
                         dict(payload([raw()]), results=2), dict(payload([raw()]), paging=None)):
            self.client.fixtures.return_value = response
            with self.assertRaises(api.ApiFootballError):
                workspace.refresh_day(DAY)
            self.assertEqual(self.path.read_bytes(), original)
        self.client.fixtures.side_effect = api.ApiFootballError("Quota error " + self.key)
        with self.assertRaises(api.ApiFootballError) as error:
            workspace.refresh_day(DAY)
        self.assertNotIn(self.key, str(error.exception))
        self.assertEqual(self.path.read_bytes(), original)

    def test_history_collects_two_complete_responses_then_one_atomic_save(self):
        invalid = raw(2, status="FT")
        invalid["score"]["halftime"]["home"] = None
        self.client.fixtures.side_effect = [payload([raw(1, status="FT", season=2025)]), payload([invalid, raw(3)])]
        with patch.object(api, "save_fixtures", wraps=api.save_fixtures) as save:
            result = workspace.ensure_history("39", [2025, 2026])
        self.assertEqual([call.kwargs for call in self.client.fixtures.call_args_list],
                         [{"league": "39", "season": 2025, "timezone": "UTC"},
                          {"league": "39", "season": 2026, "timezone": "UTC"}])
        save.assert_called_once()
        self.assertEqual((result["stored_count"], result["history_count"]), (3, 1))
        self.assertEqual(result["reports"][1]["issues"][0]["match_id"], "2")
        self.assertEqual(workspace.missing_history("39", [2025, 2026]), [])
        self.assertEqual(len(api.load_season_imports(self.path)), 2)

    def test_second_history_request_failure_or_wrong_league_commits_nothing(self):
        api.save_fixtures([raw(99, status="FT")], self.path)
        original = self.path.read_bytes()
        for second in (api.ApiFootballError("429 " + self.key), payload([raw(2, league=40)]),
                       payload([raw(2, season=2024)]), payload([raw(2)], pages=2)):
            self.client.fixtures.side_effect = [payload([raw(1, season=2025)]), second]
            with self.assertRaises(api.ApiFootballError) as error:
                workspace.ensure_history("39", [2025, 2026])
            self.assertNotIn(self.key, str(error.exception))
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(workspace.missing_history("39", [2025, 2026]), [2025, 2026])

    def test_full_future_or_empty_season_is_not_reloaded_on_every_prediction(self):
        for batch in ([raw(status="NS")], []):
            self.path.unlink(missing_ok=True)
            self.client.fixtures.reset_mock()
            self.client.fixtures.side_effect = None
            self.client.fixtures.return_value = payload(batch)
            for _ in range(3):
                missing = workspace.missing_history("39", [2026])
                if missing:
                    workspace.ensure_history("39", missing)
            self.client.fixtures.assert_called_once_with(league="39", season=2026, timezone="UTC")
            self.assertEqual(api.load_matches(self.path)[0], [])

    def test_refresh_never_turns_a_finished_fixture_back_into_a_future_match(self):
        api.save_fixtures([raw(status="FT")], self.path,
            season_imports=[dict(competition_code="39", season=2026, match_ids=[1])])
        self.client.fixtures.return_value = payload([raw(status="NS")])
        workspace.refresh_day(DAY)
        snapshot = workspace.local_snapshot(DAY, now=NOW)
        self.assertEqual(snapshot["upcoming"], [])
        self.assertEqual(snapshot["usable_count"], 1)
        self.assertEqual(workspace.missing_history("39", [2026]), [])

    def test_season_limit_and_numeric_league_are_validated_before_credentials(self):
        with patch.object(api, "load_key", side_effect=AssertionError("validate first")):
            for code, years in (("PL", [2026]), ("39", []), ("39", [2024, 2025, 2026]), ("39", [True])):
                with self.assertRaises(ValueError):
                    workspace.ensure_history(code, years)
        self.client.fixtures.assert_not_called()


if __name__ == "__main__":
    unittest.main()
