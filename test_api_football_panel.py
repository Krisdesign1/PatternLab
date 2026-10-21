"""UI regression checks for explicit API calls and local fixture imports."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

import api_football as api
import api_football_panel as panel


def raw_fixture(match_id, status="FT"):
    return {
        "fixture": {"id": match_id, "date": "2025-09-01T19:00:00+00:00", "status": {"short": status}},
        "league": {"id": 61, "name": "Ligue 1", "country": "France", "season": 2025},
        "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
        "score": {"halftime": {"home": 1, "away": 0}, "fulltime": {"home": 2, "away": 1}},
    }


class ApiFootballPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store_path = self.root / "fixtures.json"
        self.check_path = self.root / "access_check.json"
        self.key = "test-panel-key-never-real"
        self.client = MagicMock()
        self.client.leagues.return_value = {
            "errors": [], "response": [
                {"league": {"id": 61, "name": "Ligue 1"}, "country": {"name": "France"},
                 "seasons": [{"year": 2024}, {"year": 2025}]}],
        }
        self.batch = [raw_fixture(101), raw_fixture(102, status="NS")]
        self.client.fixtures.return_value = {"errors": [], "response": self.batch, "paging": {"total": 1}}
        self.load = self._patch("load_fixtures", side_effect=lambda: api.load_fixtures(self.store_path))
        self.save = self._patch("save_fixtures", side_effect=lambda rows, **kwargs:
                               api.save_fixtures(rows, self.store_path, **kwargs))
        self._patch("load_key", return_value=self.key)
        self._patch("ApiFootballClient", return_value=self.client)
        self._patch("DATA_DIR", self.root)
        self._patch("STORE_PATH", self.store_path)
        self._patch("CHECK_PATH", self.check_path)
        self.at = AppTest.from_string(
            "from api_football_panel import render_api_football\nrender_api_football()",
            default_timeout=10,
        ).run()
        self.assertFalse(self.at.exception)

    def _patch(self, name, *args, **kwargs):
        patcher = patch.object(panel, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _click_label(self, label):
        next(button for button in self.at.button if button.label == label).click().run()
        self.assertFalse(self.at.exception)

    def _search(self):
        self.at.text_input(key="api_league_query").set_value("Ligue 1")
        self._click_label("Rechercher les compétitions")

    def _load_batch(self):
        self._search()
        self.at.button(key="api_fetch_season").click().run()
        self.assertFalse(self.at.exception)

    def _assert_no_network(self):
        self.client.status.assert_not_called()
        self.client.leagues.assert_not_called()
        self.client.fixtures.assert_not_called()

    def test_render_and_rerun_do_not_make_network_calls(self):
        self._assert_no_network()
        self.at.run()
        self.assertFalse(self.at.exception)
        self._assert_no_network()
        self.save.assert_not_called()

    def test_suspension_is_a_warning_without_exposing_key_or_crashing(self):
        self.client.status.side_effect = api.ApiFootballError(
            "Your account is suspended, check https://dashboard.api-football.com. " + self.key)
        self.at.button(key="api_check").click().run()
        self.assertFalse(self.at.exception)
        warnings = " ".join(item.value for item in self.at.warning)
        self.assertIn("suspended", warnings)
        self.assertNotIn(self.key, warnings)
        self.assertTrue(any("dashboard.api-football.com" in item.value for item in self.at.markdown))
        self.client.status.assert_called_once_with()
        self.client.leagues.assert_not_called()
        self.client.fixtures.assert_not_called()
        self.assertTrue(self.check_path.exists())
        self.assertNotIn(self.key, self.check_path.read_text())
        self.at.run()
        self.assertFalse(self.at.exception)
        self.client.status.assert_called_once_with()

    def test_search_load_and_import_preserve_finished_and_scheduled_fixtures(self):
        self._search()
        self.client.leagues.assert_called_once_with(search="Ligue 1", country=None)
        self.client.fixtures.assert_not_called()
        self.assertEqual(self.at.selectbox(key="api_season_selected").value, 2025)
        self.assertEqual(self.at.selectbox(key="api_season_selected").options, ["2025", "2024"])
        self.at.run()
        self.client.leagues.assert_called_once()
        self.at.button(key="api_fetch_season").click().run()
        self.assertFalse(self.at.exception)
        self.client.fixtures.assert_called_once_with(league="61", season=2025, timezone="UTC")
        self.save.assert_not_called()
        self.assertTrue(any("2 matchs reçus" in item.value and "1 résultats HT/FT utilisables" in item.value
                            for item in self.at.markdown))
        self.at.run()
        self.client.fixtures.assert_called_once()
        self.at.button(key="api_import").click().run()
        self.assertFalse(self.at.exception)
        self.save.assert_called_once_with(self.batch, season_imports=[
            {"competition_code": "61", "league": "61", "season": 2025, "match_ids": ["101", "102"]}
        ])
        self.assertEqual(len(api.load_season_imports(self.store_path)), 1)
        self.assertEqual(len(api.load_fixtures(self.store_path)), 2)
        self.assertTrue(any("2 matchs API-Football conservés au total" in item.value for item in self.at.success))
        self.assertEqual({row["fixture"]["status"]["short"] for row in api.load_fixtures(self.store_path)}, {"FT", "NS"})
        self.at.run()
        self.assertFalse(self.at.exception)
        self.save.assert_called_once()
        self.client.leagues.assert_called_once()
        self.client.fixtures.assert_called_once()
        self.client.status.assert_not_called()

    def test_changing_season_prevents_importing_the_previous_batch(self):
        self._load_batch()
        self.assertTrue(any(button.key == "api_import" for button in self.at.button))
        self.at.selectbox(key="api_season_selected").set_value(2024).run()
        self.assertFalse(self.at.exception)
        self.assertFalse(any(button.key == "api_import" for button in self.at.button))
        self.save.assert_not_called()
        self.client.fixtures.assert_called_once_with(league="61", season=2025, timezone="UTC")
        self.assertFalse(self.store_path.exists())

    def test_empty_api_source_in_full_app_never_calls_the_network(self):
        with patch.object(api, 'load_matches', return_value=([], {'expected':0,'matched':0,'issues':[]})), \
             patch.object(api, 'data_signature', return_value=(str(self.store_path),None,None)):
            app=AppTest.from_file(str(Path(panel.__file__).parent/'app.py'),default_timeout=20).run()
            app.switch_page('ui_pages/explore.py').run()
            self.assertFalse(app.exception)
            self.assertTrue(any('Aucun résultat HT/FT API-Football utilisable' in item.value for item in app.info))
            self._assert_no_network()
            self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
