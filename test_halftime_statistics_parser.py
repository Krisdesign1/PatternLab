"""Meaningful structural/value checks for the isolated coverage parser."""

import copy
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "coverage_parser", Path(__file__).with_name("halftime_statistics_parser.py"))
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def payload(home_value=0, away_value=8):
    return {"errors": [], "response": [
        {"team": {"id": 20}, "statistics": [
            {"type": "Shots on Goal", "value": away_value},
            {"type": "Ball Possession", "value": "60%"}]},
        {"team": {"id": 10}, "statistics": [
            {"type": "Shots on Goal", "value": home_value},
            {"type": "Ball Possession", "value": "40%"}]},
    ]}


class StatisticsCoverageTests(unittest.TestCase):
    def parse(self, value):
        return PARSER.parse_statistics(value, (10, 20))

    def test_zero_is_valid_and_provider_team_order_does_not_define_home(self):
        result = self.parse(payload())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["periods"]["full"]["pairs"]["Shots on Goal"], [0, 8])
        self.assertEqual(result["periods"]["full"]["pairs"]["Ball Possession"], [40, 60])
        self.assertFalse(result["halves_available"])

    def test_empty_response_is_distinct_from_error_and_missing_shape(self):
        self.assertEqual(self.parse({"errors": [], "response": []})["status"], "empty")
        self.assertEqual(self.parse({"errors": [], "response": {}})["status"], "unknown_shape")
        self.assertEqual(self.parse({})["status"], "unknown_shape")
        self.assertEqual(self.parse([])["status"], "invalid")

    def test_provider_error_suppresses_apparently_valid_data_without_echo(self):
        value = payload()
        value["errors"] = {"requests": "quota exceeded for secret-token"}
        result = self.parse(value)
        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["provider_error_kind"], "quota_or_rate")
        self.assertNotIn("secret-token", str(result))
        self.assertEqual(result["periods"]["full"]["pairs"], {})

    def test_missing_and_wrong_teams_never_count_as_coverage(self):
        for kind in ("missing", "wrong", "duplicate"):
            value = payload()
            if kind == "missing":
                value["response"].pop()
            elif kind == "wrong":
                value["response"][1]["team"]["id"] = 30
            else:
                value["response"].append(copy.deepcopy(value["response"][0]))
            result = self.parse(value)
            self.assertEqual(result["status"], "invalid", kind)
            self.assertEqual(result["periods"]["full"]["pairs"], {}, kind)

    def test_null_absent_invalid_values_remain_distinct(self):
        cases = [(None, "null"), (-1, "negative"), (float("nan"), "nonfinite"),
                 (float("inf"), "nonfinite"), (True, "boolean"),
                 (1.5, "noninteger_count"), ("2%", "invalid_number")]
        for value, problem in cases:
            result = self.parse(payload(value))
            report = result["periods"]["full"]
            self.assertNotIn("Shots on Goal", report["pairs"], repr(value))
            self.assertEqual(report["metric_issues"]["Shots on Goal"][0], problem)
        result = self.parse(payload())
        self.assertEqual(result["periods"]["full"]["metric_issues"]["Total Shots"],
                         ["metric_absent", "metric_absent"])

    def test_percentages_require_percent_units_and_bounds(self):
        for percentage in (40, .4, "40", "0.4", "-1%", "101%", "NaN%", "Infinity%"):
            value = payload()
            value["response"][1]["statistics"][1]["value"] = percentage
            self.assertNotIn("Ball Possession", self.parse(value)["periods"]["full"]["pairs"])
        value = payload()
        value["response"][1]["statistics"][1]["value"] = "0%"
        self.assertEqual(self.parse(value)["periods"]["full"]["pairs"]["Ball Possession"], [0, 60])

    def test_duplicate_metric_does_not_silently_choose_value(self):
        value = payload()
        value["response"][1]["statistics"].append({"type": "Shots on Goal", "value": 6})
        report = self.parse(value)["periods"]["full"]
        self.assertNotIn("Shots on Goal", report["pairs"])
        self.assertEqual(report["metric_issues"]["Shots on Goal"][0], "duplicate_metric")

    def test_unknown_half_structure_never_becomes_observed_halves(self):
        value = payload()
        for entry in value["response"]:
            entry["halftime"] = {"first": entry["statistics"], "second": entry["statistics"]}
        result = self.parse(value)
        self.assertFalse(result["halves_available"])
        self.assertFalse(result["periods"]["first"]["present"])
        self.assertIn("halftime", result["unknown_entry_keys"])
        self.assertIn("unrecognized_team_fields", result["diagnostics"])
        for entry in value["response"]:
            del entry["statistics"]
        self.assertEqual(self.parse(value)["status"], "unknown_shape")

    def test_verified_explicit_halves_are_parsed_without_subtracting_totals(self):
        value = payload()
        for entry in value["response"]:
            entry["statistics"] = [{"type": "Shots on Goal", "value": 2},
                                     {"type": "Total Shots", "value": 8}]
            entry["statistics_1h"] = [{"type": "Shots on Goal", "value": 0},
                                      {"type": "Total Shots", "value": 3}]
            entry["statistics_2h"] = [{"type": "Shots on Goal", "value": 2},
                                      {"type": "Total Shots", "value": 5}]
        result = self.parse(value)
        self.assertTrue(result["halves_available"])
        self.assertTrue(result["halves_core_shots_available"])
        self.assertEqual(result["periods"]["first"]["pairs"]["Shots on Goal"], [0, 0])
        self.assertEqual(result["periods"]["second"]["pairs"]["Total Shots"], [5, 5])
        self.assertEqual(result["complete_half_metrics"], ["Shots on Goal", "Total Shots"])

    def test_one_half_or_one_team_half_is_insufficient(self):
        value = payload()
        for entry in value["response"]:
            entry["statistics_1h"] = [{"type": "Total Shots", "value": 2}]
        self.assertFalse(self.parse(value)["halves_available"])
        value["response"][0]["statistics_2h"] = [{"type": "Total Shots", "value": 3}]
        self.assertFalse(self.parse(value)["halves_available"])
        value["response"][1]["statistics_2h"] = [{"type": "Total Shots", "value": None}]
        self.assertFalse(self.parse(value)["halves_available"])
        value["response"][1]["statistics_2h"][0]["value"] = 0
        result = self.parse(value)
        self.assertTrue(result["halves_available"])
        self.assertFalse(result["halves_core_shots_available"])

    def test_missing_value_and_malformed_block_are_diagnosed(self):
        value = payload()
        del value["response"][1]["statistics"][0]["value"]
        report = self.parse(value)["periods"]["full"]
        self.assertEqual(report["metric_issues"]["Shots on Goal"][0], "value_missing")
        value["response"][0]["statistics"] = {"type": "Shots on Goal", "value": 8}
        result = self.parse(value)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["periods"]["full"]["pairs"], {})

    def test_decimal_xg_is_supported_but_unknown_metrics_not_relabelled(self):
        value = payload()
        for entry in value["response"]:
            entry["statistics"].extend([
                {"type": "expected_goals", "value": "1.23"},
                {"type": "invented_shots", "value": 8}])
        report = self.parse(value)["periods"]["full"]
        self.assertEqual(report["pairs"]["expected_goals"], [1.23, 1.23])
        self.assertNotIn("invented_shots", report["pairs"])
        self.assertIn("invented_shots", report["unknown_metrics"])

    def test_input_is_unchanged_and_expected_id_validation_strict(self):
        value = payload()
        before = copy.deepcopy(value)
        self.parse(value)
        self.assertEqual(value, before)
        for ids in ((10, 10), (True, 20), (0, 20), (10,), (10, "abc")):
            with self.assertRaises(ValueError):
                PARSER.parse_statistics(value, ids)


if __name__ == "__main__":
    unittest.main()
