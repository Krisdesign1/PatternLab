"""Check the future xG quality gate without changing the frozen parser."""

import copy
import unittest

import halftime_statistics_parser as legacy
import halftime_statistics_quality as quality


def payload(home=("1.20", "0.50", "0.70"),
            away=("0.70", "0.40", "0.30"), *, shots=True):
    """Build reversed provider order with explicit, additive shots."""
    response = []
    for team_id, xg in ((20, away), (10, home)):
        entry = {"team": {"id": team_id}}
        for index, field in enumerate(("statistics", "statistics_1h", "statistics_2h")):
            entry[field] = [{"type": "expected_goals", "value": xg[index]}]
            if shots:
                entry[field].extend([
                    {"type": "Total Shots", "value": (8, 3, 5)[index]},
                    {"type": "Shots on Goal", "value": (3, 1, 2)[index]},
                    {"type": "Shots insidebox", "value": (5, 2, 3)[index]},
                    {"type": "Shots outsidebox", "value": (3, 1, 2)[index]},
                ])
        response.append(entry)
    return {"errors": [], "response": response}


class StatisticsQualityTests(unittest.TestCase):
    def parse(self, value):
        return quality.parse_statistics(value, (10, 20))

    def test_shared_triplet_gate_is_pure_and_checks_complete_sides(self):
        pairs = {"full": [0.89, None], "first": [0.89, 0.4], "second": [0.03, 0.3]}
        untouched = copy.deepcopy(pairs)
        self.assertEqual(quality.xg_consistency_issues(pairs), [dict(
            metric="expected_goals", side=0, issue="nonadditive_xg",
            full=0.89, first=0.89, second=0.03, rounding_tolerance=0.015)])
        self.assertEqual(pairs, untouched)
        self.assertEqual(quality.xg_consistency_issues({}), [])
        self.assertEqual(quality.xg_consistency_issues({"first": [0, 0], "second": None}), [])
        self.assertEqual(quality.xg_consistency_issues(
            {"full": [0, 0], "first": [0, 0], "second": [0, 0]}), [])

    def test_shared_triplet_gate_requires_normalized_measurements(self):
        for value in (True, "0.2", -0.2, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quality.xg_consistency_issues({"full": [value, 0]})
        for pair in ([], [0], [0, 0, 0], "00"):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                quality.xg_consistency_issues({"full": pair})

    def test_clean_result_keeps_legacy_contract_and_measurements(self):
        value = payload(home=(0, "0.00", 0.0))
        result = self.parse(value)
        before = legacy.parse_statistics(value, (10, 20))
        self.assertEqual(set(result), set(before))
        self.assertNotEqual(result["parser_version"], before["parser_version"])
        before["parser_version"] = quality.PARSER_VERSION
        self.assertEqual(result, before)
        for period in result["periods"].values():
            self.assertEqual(period["pairs"]["expected_goals"][0], 0.0)
        self.assertTrue(result["halves_available"])
        self.assertTrue(result["halves_core_shots_available"])

    def test_two_decimal_rounding_residuals_of_either_sign_are_retained(self):
        for triplet in (("0.30", "0.10", "0.19"),
                        ("0.30", "0.10", "0.21"),
                        ("0.30", "0.10", "0.20")):
            with self.subTest(triplet=triplet):
                result = self.parse(payload(home=triplet))
                self.assertEqual(result["consistency_issues"], [])
                self.assertIn("expected_goals", result["complete_half_metrics"])

    def test_exact_rounding_bound_is_inclusive_but_two_cents_is_rejected(self):
        # The parser also accepts numeric precision beyond the usual cents;
        # exercise the analytical 0.015 boundary without a float epsilon.
        for triplet in (("0.315", "0.10", "0.20"),
                        ("0.285", "0.10", "0.20")):
            self.assertEqual(self.parse(payload(home=triplet))["consistency_issues"], [])
        for triplet in (("0.32", "0.10", "0.20"),
                        ("0.28", "0.10", "0.20")):
            result = self.parse(payload(home=triplet))
            self.assertEqual(result["consistency_issues"][0]["issue"], "nonadditive_xg")
            self.assertNotIn("expected_goals", result["complete_half_metrics"])

    def test_documented_anomalies_quarantine_only_xg_without_repairing_raw(self):
        anomalies = (("1213857", 0, ("0.17", "0.17", "0.12")),
                     ("1208600", 0, ("0.89", "0.89", "0.03")),
                     ("1351043", 1, ("0.24", "0.24", "0.12")))
        for match_id, side, triplet in anomalies:
            with self.subTest(match_id=match_id):
                value = payload(**{"home" if side == 0 else "away": triplet})
                untouched = copy.deepcopy(value)
                baseline = legacy.parse_statistics(value, (10, 20))
                result = self.parse(value)
                self.assertEqual(value, untouched)
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["halves_core_shots_available"])
                self.assertEqual(result["consistency_issues"], [dict(
                    metric="expected_goals", side=side, issue="nonadditive_xg",
                    full=float(triplet[0]), first=float(triplet[1]), second=float(triplet[2]),
                    rounding_tolerance=0.015)])
                for name, period in result["periods"].items():
                    expected_shots = dict(baseline["periods"][name]["pairs"])
                    expected_shots.pop("expected_goals")
                    self.assertEqual(period["pairs"], expected_shots)
                    self.assertNotIn("expected_goals", period["valid_pair_metrics"])
                    self.assertEqual(period["metric_issues"]["expected_goals"],
                                     ["inconsistent_xg", "inconsistent_xg"])
                # The legacy entry point must remain unchanged for frozen work.
                self.assertIn("expected_goals", baseline["complete_half_metrics"])

    def test_xg_only_failure_recomputes_coverage_and_reports_each_bad_side(self):
        result = self.parse(payload(home=("0.89", "0.89", "0.03"),
                                    away=("0.24", "0.24", "0.12"), shots=False))
        self.assertEqual([issue["side"] for issue in result["consistency_issues"]], [0, 1])
        self.assertFalse(result["halves_available"])
        self.assertFalse(result["halves_core_shots_available"])
        self.assertEqual(result["complete_half_metrics"], [])
        for period in result["periods"].values():
            self.assertEqual(period["pairs"], {})

    def test_absent_null_and_invalid_xg_are_not_reconstructed_or_zero_filled(self):
        for missing_kind in ("metric", "period", "null", "invalid"):
            with self.subTest(missing_kind=missing_kind):
                value = payload()
                entry = value["response"][1]
                if missing_kind == "metric":
                    entry["statistics_2h"].pop(0)
                elif missing_kind == "period":
                    del entry["statistics_2h"]
                else:
                    entry["statistics_2h"][0]["value"] = (
                        None if missing_kind == "null" else "not an xG")
                result = self.parse(value)
                expected = legacy.parse_statistics(value, (10, 20))
                expected["parser_version"] = quality.PARSER_VERSION
                self.assertEqual(result, expected)
                self.assertNotIn("expected_goals", result["periods"]["second"]["pairs"])
                self.assertNotIn("expected_goals", result["complete_half_metrics"])
                self.assertIn("expected_goals", result["periods"]["first"]["pairs"])

    def test_legacy_shot_quarantine_and_error_states_are_preserved(self):
        value = payload()
        value["response"][0]["statistics"][1]["value"] = 9
        result = self.parse(value)
        self.assertTrue(any(issue["issue"] == "nonadditive_shots"
                            for issue in result["consistency_issues"]))
        self.assertIn("expected_goals", result["complete_half_metrics"])
        self.assertNotIn("Total Shots", result["complete_half_metrics"])
        for value in ({"errors": {"requests": "quota"}, "response": []},
                      {"errors": [], "response": []}, {"response": {}}, []):
            expected = legacy.parse_statistics(value, (10, 20))
            expected["parser_version"] = quality.PARSER_VERSION
            self.assertEqual(self.parse(value), expected)


if __name__ == "__main__":
    unittest.main()
