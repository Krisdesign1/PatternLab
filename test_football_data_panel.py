"""UI checks for explicit football-data.org requests and complete local imports."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

import football_data as api
import football_data_panel as panel


def raw_match(match_id, status="FINISHED"):
    return {
        "id": match_id, "utcDate": "2026-09-01T19:00:00Z", "status": status,
        "area": {"name": "England"},
        "competition": {"id": 2021, "code": "PL", "name": "Premier League"},
        "season": {"startDate": "2026-08-15"},
        "homeTeam": {"id": 1, "name": "Home"}, "awayTeam": {"id": 2, "name": "Away"},
        "score": {"duration": "REGULAR", "halfTime": {"home": 1, "away": 0},
                  "fullTime": {"home": 2, "away": 1}},
    }


class FootballDataPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store_path = self.root / "matches.json"
        self.check_path = self.root / "access_check.json"
        self.key = "test-football-data-key-never-real"
        self.client = MagicMock()
        self.client.last_quota = {"requests_available_minute": 9, "reset_seconds": 52}
        self.client.competitions.return_value = {"competitions": [
            {"id": 2021, "code": "PL", "name": "Premier League", "area": {"name": "England"},
             "plan": "TIER_ONE", "currentSeason": {"startDate": "2026-08-15"}},
            {"id": 2015, "code": "FL1", "name": "Ligue 1", "area": {"name": "France"},
             "plan": "TIER_ONE", "currentSeason": {"startDate": "2026-08-15"}},
            {"id": 2152, "code": "CLI", "name": "Copa Libertadores", "area": {"name": "South America"},
             "plan": "TIER_FOUR", "currentSeason": {"startDate": "2026-02-01"}},
        ]}
        self.batch = [raw_match(101), raw_match(102, status="TIMED")]
        self.client.matches.return_value = {"matches": self.batch}
        self.load = self._patch("load_fixtures", side_effect=lambda: api.load_fixtures(self.store_path))
        self.save = self._patch("save_fixtures", side_effect=lambda rows, **kwargs:
                               api.save_fixtures(rows, self.store_path, **kwargs))
        self.load_key = self._patch("load_key", return_value=self.key)
        self._patch("FootballDataClient", return_value=self.client)
        self._patch("DATA_DIR", self.root)
        self._patch("STORE_PATH", self.store_path)
        self._patch("CHECK_PATH", self.check_path)
        self.at = AppTest.from_string(
            "from football_data_panel import render_football_data\nrender_football_data()",
            default_timeout=10,
        ).run()
        self.assertFalse(self.at.exception)

    def _patch(self, name, *args, **kwargs):
        patcher = patch.object(panel, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _check(self):
        self.at.button(key="fd_check").click().run()
        self.assertFalse(self.at.exception)

    def _load_batch(self):
        self._check()
        self.at.button(key="fd_fetch_season").click().run()
        self.assertFalse(self.at.exception)

    def _assert_no_network(self):
        self.client.competitions.assert_not_called()
        self.client.matches.assert_not_called()

    def test_render_and_rerun_do_not_make_network_calls(self):
        self._assert_no_network()
        self.at.run()
        self.assertFalse(self.at.exception)
        self._assert_no_network()
        self.save.assert_not_called()

    def test_access_refusal_is_recorded_without_exposing_key(self):
        self.client.competitions.side_effect = api.FootballDataError("HTTP 403 : accès refusé " + self.key)
        self._check()
        warnings = " ".join(item.value for item in self.at.warning)
        self.assertIn("403", warnings)
        self.assertNotIn(self.key, warnings)
        self.assertNotIn(self.key, self.check_path.read_text())
        self.assertFalse(json.loads(self.check_path.read_text())["ok"])
        self.client.competitions.assert_called_once_with()
        self.client.matches.assert_not_called()
        self.at.run()
        self.assertFalse(self.at.exception)
        self.client.competitions.assert_called_once()

    def test_rate_limit_does_not_leave_a_stale_batch_or_expose_key(self):
        self._load_batch()
        self.assertTrue(any(button.key == "fd_import" for button in self.at.button))
        self.client.last_quota = {"requests_available_minute": 0, "reset_seconds": 42}
        self.client.matches.side_effect = api.FootballDataError("HTTP 429 : limite atteinte " + self.key)
        self.at.button(key="fd_fetch_season").click().run()
        self.assertFalse(self.at.exception)
        errors = " ".join(item.value for item in self.at.error)
        self.assertIn("429", errors)
        self.assertNotIn(self.key, errors)
        self.assertTrue(any("42" in item.value for item in self.at.caption))
        self.assertFalse(any(button.key == "fd_import" for button in self.at.button))
        self.save.assert_not_called()

    def test_check_load_and_import_preserve_results_and_calendar(self):
        self._check()
        self.client.competitions.assert_called_once_with()
        self.client.matches.assert_not_called()
        self.assertEqual(self.at.selectbox(key="fd_season_selected").value, 2026)
        self.at.run()
        self.client.competitions.assert_called_once()
        self.at.button(key="fd_fetch_season").click().run()
        self.assertFalse(self.at.exception)
        self.client.matches.assert_called_once_with("PL", season=2026)
        self.save.assert_not_called()
        self.assertTrue(any("2 matchs reçus" in item.value and "1 résultats HT/FT utilisables" in item.value
                            for item in self.at.markdown))
        self.at.button(key="fd_import").click().run()
        self.assertFalse(self.at.exception)
        self.save.assert_called_once_with(self.batch, season_imports=[
            {"competition_code": "PL", "season": 2026, "match_ids": [101, 102]}
        ])
        manifest = json.loads(self.store_path.read_text())["season_imports"]
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["competition_code"], "PL")
        self.assertEqual(manifest[0]["season"], 2026)
        self.assertEqual({row["status"] for row in api.load_fixtures(self.store_path)}, {"FINISHED", "TIMED"})
        self.assertEqual(len(api.load_matches(self.store_path)[0]), 1)
        self.assertTrue(any("2 matchs football-data.org conservés au total" in item.value for item in self.at.success))
        self.at.run()
        self.assertFalse(self.at.exception)
        self.client.competitions.assert_called_once()
        self.client.matches.assert_called_once()
        self.save.assert_called_once()

    def test_changing_season_or_competition_hides_previous_batch(self):
        self._load_batch()
        self.at.text_input(key="fd_season_override").set_value("2025").run()
        self.assertFalse(self.at.exception)
        self.assertFalse(any(button.key == "fd_import" for button in self.at.button))
        self.at.text_input(key="fd_season_override").set_value("").run()
        self.assertTrue(any(button.key == "fd_import" for button in self.at.button))
        self.at.selectbox(key="fd_competition_selected").set_value("FL1").run()
        self.assertFalse(self.at.exception)
        self.assertFalse(any(button.key == "fd_import" for button in self.at.button))
        self.client.matches.assert_called_once_with("PL", season=2026)
        self.save.assert_not_called()

    def test_preview_is_limited_but_import_keeps_all_matches(self):
        self.batch = [raw_match(match_id) for match_id in range(1, 66)]
        self.client.matches.return_value = {"matches": self.batch}
        self._load_batch()
        self.assertEqual(len(self.at.dataframe[0].value), 50)
        self.at.button(key="fd_import").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(api.load_fixtures(self.store_path)), 65)

    def test_catalogue_hides_other_plans_by_default(self):
        self._check()
        options = self.at.selectbox(key="fd_competition_selected").options
        self.assertEqual(len(options), 2)
        self.assertFalse(any("CLI" in option for option in options))
        self.at.checkbox(key="fd_show_other_plans").check().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("CLI" in option for option in self.at.selectbox(key="fd_competition_selected").options))
        self.client.competitions.assert_called_once()

    def test_missing_key_keeps_local_backup_available_without_network(self):
        api.save_fixtures(self.batch, self.store_path)
        self.load_key.return_value = ""
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("Configure la clé" in item.value for item in self.at.info))
        self.assertTrue(any(item.key == "fd_backup" for item in self.at.get("download_button")))
        self._assert_no_network()

    def test_missing_scores_and_overtime_without_regular_time_are_excluded(self):
        no_ht = raw_match(101)
        no_ht["score"]["halfTime"] = {"home": None, "away": None}
        overtime = copy.deepcopy(raw_match(102))
        overtime["score"]["duration"] = "EXTRA_TIME"
        self.client.matches.return_value = {"matches": [no_ht, overtime, raw_match(103, "SCHEDULED")]}
        self._load_batch()
        self.assertTrue(any("0 résultats HT/FT utilisables" in item.value for item in self.at.markdown))
        self.assertTrue(any("2 match(s) terminé(s) exclus" in item.value for item in self.at.warning))
        self.at.button(key="fd_import").click().run()
        self.assertFalse(self.at.exception)
        rows, audit = api.load_matches(self.store_path)
        self.assertEqual(rows, [])
        self.assertEqual(audit["expected"], 2)
        self.assertEqual(len(audit["issues"]), 2)


if __name__ == "__main__":
    unittest.main()
