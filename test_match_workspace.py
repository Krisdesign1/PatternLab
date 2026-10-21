from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import football_data
import match_workspace as workspace


DAY = date(2026, 10, 18)
NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def raw(match_id=1, stamp="2026-10-18T12:00:00Z", status="TIMED", league=2021, code="PL", season=2026):
    return dict(id=match_id, utcDate=stamp, status=status,
                competition={"id": league, "code": code, "name": "Premier League"},
                area={"name": "England"}, season={"startDate": f"{season}-08-01"},
                homeTeam={"id": 10, "name": "Home"}, awayTeam={"id": 20, "name": "Away"},
                score={"duration": "REGULAR", "halfTime": {"home": 1, "away": 0},
                       "fullTime": {"home": 2, "away": 1}})


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "matches.json"
        self.path_patch = patch.object(football_data, "STORE_PATH", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def save(self, matches):
        return football_data.save_fixtures(matches, self.path)


class WorkspaceTests(WorkspaceCase):
    def test_a_few_finished_matches_do_not_establish_complete_season_coverage(self):
        self.save([raw(i, status="FINISHED") for i in range(1, 4)])
        with patch.object(football_data, "load_key", side_effect=AssertionError("lookup must stay local")):
            self.assertEqual(workspace.missing_history("PL", [2025, 2026]), [2025, 2026])

    def test_missing_history_checks_marker_records_and_exact_competition_code(self):
        batch = [raw(), raw(2, league=999, code="PL2")]
        football_data.save_fixtures(batch, self.path,
            season_imports=[dict(competition_code="PL", season=2026, league=2021, match_ids=[1])])
        self.assertEqual(workspace.missing_history("pl", [2026]), [])
        self.assertEqual(workspace.missing_history("PL2", [2026]), [2026])
        payload = json.loads(self.path.read_text())
        payload["season_imports"][0]["match_ids"] = ["999999"]
        self.path.write_text(json.dumps(payload))
        self.assertEqual(workspace.missing_history("PL", [2026]), [2026])

    def test_kinshasa_local_day_has_exact_utc_boundaries(self):
        matches = [raw(1, "2026-10-17T22:59:59Z"), raw(2, "2026-10-17T23:00:00Z"),
                   raw(3, "2026-10-18T22:59:59Z"), raw(4, "2026-10-18T23:00:00Z")]
        self.save(matches)
        selected = workspace.local_day_matches(DAY)
        self.assertEqual([item["id"] for item in selected], [2, 3])

    def test_timezone_conversion_handles_offsets_and_new_york_dst(self):
        self.save([raw(1, "2026-10-18T01:00:00+02:00")])
        self.assertEqual(len(workspace.local_day_matches(DAY)), 1)
        self.assertEqual(workspace.local_day_matches(date(2026, 10, 17)), [])
        # New York switches to DST on 8 March 2026: the local day lasts 23 hours.
        self.save([raw(2, "2026-03-08T04:59:59Z"), raw(3, "2026-03-08T05:00:00Z"),
                   raw(4, "2026-03-09T03:59:59Z"), raw(5, "2026-03-09T04:00:00Z")])
        self.assertEqual([item["id"] for item in workspace.local_day_matches(date(2026, 3, 8), "America/New_York")], [3, 4])

    def test_fixture_is_score_free_and_rejects_unknown_clubs(self):
        fixture = workspace.fixture_from_raw(raw())
        self.assertEqual((fixture["match_id"], fixture["league"], fixture["timestamp"]),
                         ("1", "2021", "2026-10-18T12:00:00"))
        self.assertEqual(fixture["provider"], "football-data.org")
        self.assertNotIn("value", fixture)
        self.assertNotIn("score", fixture)
        invalid = raw()
        invalid["homeTeam"] = {"id": None, "name": "Winner quarter-final"}
        with self.assertRaises(ValueError):
            workspace.fixture_from_raw(invalid)
        invalid["homeTeam"] = {"id": 10, "name": "TBD"}
        with self.assertRaises(ValueError):
            workspace.fixture_from_raw(invalid)

    def test_upcoming_excludes_elapsed_live_cancelled_and_unconfirmed_matches(self):
        matches = [raw(1), raw(2, status="SCHEDULED"), raw(3, "2026-10-18T09:00:00Z"),
                   raw(4, status="IN_PLAY"), raw(5, status="POSTPONED"), raw(6, status="CANCELLED"),
                   raw(7, status="FINISHED"), raw(8, "2026-10-18T10:00:00Z")]
        unknown = raw(9)
        unknown["awayTeam"] = {"id": None, "name": None}
        matches.append(unknown)
        self.assertEqual([item["match_id"] for item in workspace.upcoming_in_day(matches, DAY, NOW)], ["1", "2"])
        all_rows = {row["match_id"]: row for row in workspace.fixtures_in_day(matches, DAY, NOW)}
        self.assertTrue(all_rows["7"]["selectable"])
        self.assertTrue(all_rows["7"]["is_historical"])
        self.assertFalse(all_rows["4"]["selectable"])
        self.assertFalse(all_rows["3"]["selectable"])
        self.assertNotIn("9", all_rows)

    def test_snapshot_is_local_only_and_keeps_similar_competition_names_separate(self):
        matches = [raw(1), raw(2, status="FINISHED"),
                   raw(3, status="FINISHED", league=999, code="PL2"),
                   raw(4, "2025-09-01T12:00:00Z", status="FINISHED", season=2025)]
        self.save(matches)
        with patch.object(football_data, "load_key", side_effect=AssertionError("read must not use credentials")), \
             patch.object(football_data, "FootballDataClient", side_effect=AssertionError("read must not use network")):
            snapshot = workspace.local_snapshot(DAY, now=NOW)
        self.assertEqual(snapshot["stored_count"], 4)
        self.assertEqual(snapshot["usable_count"], 3)
        self.assertEqual(snapshot["day_usable_count"], 2)
        self.assertEqual(snapshot["history_counts"], {"2021": 2, "999": 1})
        self.assertEqual(set(snapshot["competitions"]), {"2021", "999"})
        self.assertEqual(snapshot["competitions"]["2021"]["history_matches"], 2)
        self.assertEqual(snapshot["competitions"]["2021"]["matches"], 2)
        self.assertEqual(snapshot["competitions"]["999"]["matches"], 1)
        self.assertEqual(snapshot["source_label"], "football-data.org")
        self.assertIsNotNone(snapshot["updated_at"])

    def test_finished_results_with_missing_halftime_do_not_count_as_usable(self):
        invalid = raw(status="FINISHED")
        invalid["score"]["halfTime"] = {"home": None, "away": None}
        self.save([invalid])
        snapshot = workspace.local_snapshot(DAY, now=NOW)
        self.assertEqual(snapshot["stored_count"], 1)
        self.assertEqual(snapshot["usable_count"], 0)
        self.assertEqual(snapshot["competitions"]["2021"]["history_matches"], 0)

    def test_invalid_dates_and_naive_clock_are_not_silently_interpreted(self):
        with self.assertRaises(ValueError):
            workspace.fixture_from_raw(raw(stamp="2026-10-18T12:00:00"))
        with self.assertRaises(ValueError):
            workspace.upcoming_in_day([raw()], DAY, datetime(2026, 10, 18, 10))
        with self.assertRaises(ValueError):
            workspace.local_day_matches("2026-10-18")
        with self.assertRaises(ValueError):
            workspace.local_day_matches(DAY, "Not/AZone")


class SynchronizationTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.key = "workspace-test-token-never-real"
        self.client = MagicMock()
        self.client.last_quota = {"requests_available_minute": 8, "reset_seconds": 45,
                                  "private_header": self.key}
        self.key_patch = patch.object(football_data, "load_key", return_value=self.key)
        self.client_patch = patch.object(football_data, "FootballDataClient", return_value=self.client)
        self.key_patch.start()
        self.client_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.addCleanup(self.client_patch.stop)

    def test_refresh_filters_the_visible_day_but_keeps_all_received_and_old_seasons(self):
        self.save([raw(99, "2025-09-01T12:00:00Z", status="FINISHED", season=2025)])
        received = [raw(1, "2026-10-17T22:00:00Z"), raw(2, "2026-10-17T23:00:00Z"),
                    raw(3, "2026-10-18T22:59:59Z"), raw(4, "2026-10-18T23:00:00Z")]
        self.client.get.return_value = {"matches": received}
        result = workspace.refresh_day(DAY)
        self.client.get.assert_called_once_with("matches", dateFrom="2026-10-17", dateTo="2026-10-19")
        self.assertEqual([item["id"] for item in result["matches"]], [2, 3])
        self.assertEqual(result["received_count"], 4)
        self.assertEqual(result["stored_count"], 5)
        self.assertEqual({item["id"] for item in football_data.load_fixtures(self.path)}, {1, 2, 3, 4, 99})
        self.assertNotIn(self.key, json.dumps(result))
        self.assertEqual(result["quota"], {"requests_available_minute": 8, "reset_seconds": 45})

    def test_refresh_quota_or_network_error_and_invalid_response_never_mutate_storage(self):
        self.save([raw(99)])
        original = self.path.read_bytes()
        for message in ("429 quota " + self.key, "Network error " + self.key):
            self.client.get.side_effect = football_data.FootballDataError(message)
            with self.assertRaises(football_data.FootballDataError) as raised:
                workspace.refresh_day(DAY)
            self.assertNotIn(self.key, str(raised.exception))
            self.assertEqual(self.path.read_bytes(), original)
        self.client.get.side_effect = None
        self.client.get.return_value = {"matches": "not a list"}
        with self.assertRaises(football_data.FootballDataError):
            workspace.refresh_day(DAY)
        self.assertEqual(self.path.read_bytes(), original)

    def test_history_import_is_two_calls_one_upsert_and_reports_invalid_scores(self):
        self.save([raw(99, "2024-09-01T12:00:00Z", status="FINISHED", season=2024)])
        first = raw(1, "2025-09-01T12:00:00Z", status="FINISHED", season=2025)
        invalid = raw(2, status="FINISHED")
        invalid["score"]["halfTime"] = {"home": None, "away": None}
        self.client.matches.side_effect = [{"matches": [first]}, {"matches": [invalid, raw(3)]}]
        with patch.object(football_data, "save_fixtures", wraps=football_data.save_fixtures) as save:
            result = workspace.ensure_history("PL", [2025, 2026])
        self.assertEqual(self.client.matches.call_count, 2)
        self.assertEqual([call.args for call in self.client.matches.call_args_list], [("PL",), ("PL",)])
        self.assertEqual([call.kwargs for call in self.client.matches.call_args_list], [{"season": 2025}, {"season": 2026}])
        save.assert_called_once()
        self.assertEqual((result["received_count"], result["stored_count"], result["usable_count"], result["history_count"]), (3, 4, 1, 2))
        self.assertEqual(result["reports"][1]["issues"][0]["match_id"], "2")
        self.assertNotIn(self.key, json.dumps(result))
        self.assertEqual(workspace.missing_history("PL", [2025, 2026]), [])
        self.assertEqual(len(football_data.load_season_imports(self.path)), 2)

    def test_full_future_season_with_zero_finished_matches_is_not_fetched_repeatedly(self):
        self.client.matches.return_value = {"matches": [raw(status="TIMED")]}
        for _ in range(3):
            missing = workspace.missing_history("PL", [2026])
            if missing:
                workspace.ensure_history("PL", missing)
        self.client.matches.assert_called_once_with("PL", season=2026)
        self.assertEqual(football_data.load_matches(self.path)[0], [])
        self.assertEqual(workspace.missing_history("PL", [2026]), [])

    def test_empty_full_season_response_is_a_valid_import_and_survives_daily_refresh(self):
        self.client.matches.return_value = {"matches": []}
        workspace.ensure_history("PL", [2026])
        self.assertEqual(workspace.missing_history("PL", [2026]), [])
        self.client.get.return_value = {"matches": [raw(2, status="TIMED")]}
        workspace.refresh_day(DAY)
        self.assertEqual(workspace.missing_history("PL", [2026]), [])

    def test_daily_refresh_never_marks_a_partial_season_complete(self):
        self.client.get.return_value = {"matches": [raw(1, status="FINISHED"), raw(2, status="FINISHED")]}
        workspace.refresh_day(DAY)
        self.assertEqual(workspace.missing_history("PL", [2026]), [2026])
        self.assertEqual(football_data.load_season_imports(self.path), [])

    def test_second_history_request_failure_commits_nothing(self):
        self.save([raw(99)])
        original = self.path.read_bytes()
        self.client.matches.side_effect = [
            {"matches": [raw(1, "2025-09-01T12:00:00Z", status="FINISHED", season=2025)]},
            football_data.FootballDataError("429 quota " + self.key),
        ]
        with self.assertRaises(football_data.FootballDataError) as raised:
            workspace.ensure_history("PL", [2025, 2026])
        self.assertNotIn(self.key, str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(workspace.missing_history("PL", [2025, 2026]), [2025, 2026])

    def test_similar_names_wrong_competition_id_or_wrong_season_are_refused(self):
        self.save([raw(99)])
        original = self.path.read_bytes()
        for second in (raw(2, league=999), raw(2, code="PL2"), raw(2, season=2024)):
            self.client.matches.side_effect = [
                {"matches": [raw(1, season=2025)]}, {"matches": [second]},
            ]
            with self.assertRaises(football_data.FootballDataError):
                workspace.ensure_history("PL", [2025, 2026])
            self.assertEqual(self.path.read_bytes(), original)

    def test_at_most_two_distinct_seasons_are_accepted_before_using_credentials(self):
        with patch.object(football_data, "load_key", side_effect=AssertionError("must validate first")):
            for seasons in ([], [2024, 2025, 2026], [True], ["2026"]):
                with self.subTest(seasons=seasons), self.assertRaises(ValueError):
                    workspace.ensure_history("PL", seasons)
        self.client.matches.assert_not_called()


if __name__ == "__main__":
    unittest.main()
