import copy
from datetime import date, timedelta
from itertools import product
from math import isfinite, log, prod, sqrt
import unittest

from contextual_scenarios import (
    _combined_model, _ranking, _sequence_details, evaluate, forecast,
)
from league_model import VALUES
from league_patterns import repeated_scenarios
from scenario_predictor import fit_sequences, probability


def sample(days=30, size=2):
    rows = []
    for day_index in range(days):
        day = date(2026, 1, 1) + timedelta(days=day_index)
        for i, (home, away, early) in enumerate((
            ("Early H", "Early A", True), ("Early H2", "Early A2", True),
            ("Late H", "Late A", False), ("Late H2", "Late A2", False),
        )):
            rows.append(dict(match_id=str(len(rows) + 1), league="A", provider="sample",
                             home=home, away=away, timestamp=f"{day.isoformat()}T{12+i:02}:00:00",
                             day=day.isoformat(), ht="3-0" if early else "0-0", ft="3-0",
                             value="1st" if early else "2nd"))
    return repeated_scenarios(rows, size)["A"]


def fixtures(early=True, day="2026-02-01", size=2):
    role = "Early" if early else "Late"
    return [dict(home=f"{role} H" + ("2" if i % 2 else ""),
                 away=f"{role} A" + ("2" if i % 2 else ""),
                 timestamp=f"{day}T{12+i:02}:00:00", league="A", provider="sample")
            for i in range(size)]


class ExactDistributionTests(unittest.TestCase):
    def test_partition_function_matches_full_enumeration_for_two_and_four(self):
        for size in (2, 4):
            with self.subTest(size=size):
                seqs = [("1st",) * size] * 14 + [("2nd",) * size] * 7 + [("Égal",) * size] * 3
                fit = fit_sequences(seqs, size)
                clubs = [{"1st": .21 + i * .02, "Égal": .29 - i * .01, "2nd": .5 - i * .01}
                         for i in range(size)]
                model = _combined_model(fit, clubs)
                raw = {}
                for sequence in product(VALUES, repeat=size):
                    q = prod(dist[value] for dist, value in zip(clubs, sequence))
                    b = probability(fit, sequence, baseline=True)
                    raw[sequence] = q * sqrt(probability(fit, sequence) / b)
                z = sum(raw.values())
                self.assertAlmostEqual(model["normalization"], z)
                scored = [_sequence_details(model, seq) for seq in raw]
                self.assertAlmostEqual(sum(row["probability"] for row in scored), 1)
                for row in scored:
                    self.assertAlmostEqual(row["probability"], raw[row["sequence"]] / z)
                expected_top = sorted(raw, key=lambda seq: (-raw[seq], seq))[:3]
                self.assertEqual([row["sequence"] for row in _ranking(model, 3)], expected_top)

    def test_top_three_for_twenty_matches_without_enumerating_scenarios(self):
        size = 20
        fit = fit_sequences([("1st",) * size] * 20, size)
        # Binary-exact probabilities make the mathematical ties exact floats too.
        clubs = [{"1st": .25, "Égal": .25, "2nd": .5}] * size
        ranked = _ranking(_combined_model(fit, clubs), 3)
        self.assertEqual([row["sequence"] for row in ranked], [
            ("2nd",) * 20, ("1st",) + ("2nd",) * 19,
            ("2nd", "1st") + ("2nd",) * 18,
        ])
        self.assertLess(sum(row["probability"] for row in ranked), 1)

    def test_independent_uniform_history_does_not_invent_repetition_lift(self):
        fit = fit_sequences(list(product(VALUES, repeat=2)) * 20, 2)
        clubs = [{"1st": .6, "Égal": .1, "2nd": .3}, {"1st": .2, "Égal": .5, "2nd": .3}]
        model = _combined_model(fit, clubs)
        self.assertAlmostEqual(model["normalization"], 1)
        for sequence in product(VALUES, repeat=2):
            details = _sequence_details(model, sequence)
            self.assertAlmostEqual(details["probability"], details["club_probability"])
            self.assertAlmostEqual(details["lift"], 1)
            self.assertAlmostEqual(details["expected"], details["observed"])

    def test_stable_lexical_ties_and_observed_metadata(self):
        fit = fit_sequences(list(product(VALUES, repeat=2)) * 3, 2)
        model = _combined_model(fit, [dict.fromkeys(VALUES, 1 / 3)] * 2)
        expected = sorted(product(VALUES, repeat=2))[:3]
        self.assertEqual([row["sequence"] for row in _ranking(model, 3)], expected)
        details = _sequence_details(model, ("1st", "1st"))
        self.assertEqual(details["observed"], 3)
        self.assertAlmostEqual(details["frequency_probability"], 1 / 9)

    def test_partition_stays_positive_for_sparse_constant_and_extreme_club_data(self):
        for size in (2, 4, 20):
            for count in (0, 1, 20, 100):
                with self.subTest(size=size, count=count):
                    fit = fit_sequences([("1st",) * size] * count, size)
                    tiny = 1e-12
                    clubs = [{"1st": tiny, "Égal": tiny, "2nd": 1 - 2 * tiny}] * size
                    model = _combined_model(fit, clubs)
                    self.assertTrue(isfinite(model["normalization"]))
                    self.assertGreater(model["normalization"], 0)
                    for row in _ranking(model, 3):
                        self.assertTrue(isfinite(row["probability"]))
                        self.assertGreater(row["probability"], 0)
                        self.assertLessEqual(row["probability"], 1)
                    if size <= 4:
                        self.assertAlmostEqual(sum(_sequence_details(model, sequence)["probability"]
                                                   for sequence in product(VALUES, repeat=size)), 1)

    def test_constant_data_lift_is_a_smoothed_reference_not_significance(self):
        fit = fit_sequences([("1st", "1st")] * 20, 2)
        model = _combined_model(fit, [dict.fromkeys(VALUES, 1 / 3)] * 2)
        row = _sequence_details(model, ("1st", "1st"))
        self.assertLess(row["expected"], row["observed"])
        self.assertGreater(row["lift"], 1)
        self.assertEqual(row["chance_probability"], (21 / 23) ** 2)


class ContextualForecastTests(unittest.TestCase):
    def test_real_club_profiles_change_the_ranked_scenarios(self):
        result = sample()
        early, late = forecast(result, fixtures(True)), forecast(result, fixtures(False))
        self.assertTrue(early["eligible"])
        self.assertEqual(early["personalized_matches"], 2)
        self.assertEqual(early["ranking"][0]["sequence"], ("1st", "1st"))
        self.assertEqual(late["ranking"][0]["sequence"], ("2nd", "2nd"))
        self.assertEqual(early["training_groups"], 60)
        self.assertEqual(early["training_matches"], 120)
        self.assertLess(sum(row["probability"] for row in early["ranking"]), 1)

    def test_sparse_club_profiles_are_explicit_and_keep_league_fallback(self):
        chosen = fixtures()
        chosen[0].update(home="Unknown home", away="Unknown away")
        result = forecast(sample(), chosen)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["personalized_matches"], 1)
        self.assertFalse(result["club_estimates"][0]["personalized"])
        self.assertEqual(result["club_estimates"][0]["probabilities"], result["club_estimates"][0]["baseline"])
        self.assertIn("faute d’historique des clubs", result["diagnostic"])

    def test_target_labels_are_never_used_and_same_day_changes_cannot_leak(self):
        history = sample()
        chosen = fixtures(day="2026-01-20")
        original = forecast(history, chosen)
        changed = copy.deepcopy(history)
        for row in changed["matches"]:
            if row["timestamp"][:10] >= "2026-01-20":
                row.update(ht="9-0", ft="9-0", value="1st")
        for block in changed["blocks"]:
            if min(row["timestamp"][:10] for row in block["matches"]) >= "2026-01-20":
                block["sequence"] = ("invalid future label",) * 2
        altered_fixtures = [dict(row, value="target outcome must not be read", ht=None, ft=None) for row in chosen]
        self.assertEqual(original, forecast(changed, altered_fixtures))
        self.assertEqual(original["before"], "2026-01-20")
        self.assertEqual(original["training_matches"], 19 * 4)
        self.assertEqual(original["training_groups"], 19 * 2)

    def test_a_block_crossing_the_cutoff_is_not_partly_used_as_a_sequence(self):
        history = sample(days=30, size=3)
        chosen = fixtures(day="2026-01-20", size=3)
        result = forecast(history, chosen)
        expected = sum(max(row["timestamp"][:10] for row in block["matches"]) < result["before"]
                       for block in history["blocks"])
        self.assertEqual(result["training_groups"], expected)
        self.assertEqual(result["training_matches"], 19 * 4)

    def test_adding_target_same_day_and_future_rows_cannot_change_a_forecast(self):
        original = sample()
        chosen = fixtures(day="2026-01-20")
        rows = copy.deepcopy(original["matches"])
        for i, day in enumerate(("2026-01-20", "2026-01-20", "2026-03-01", "2026-03-01")):
            rows.append(dict(rows[0], match_id=str(10000 + i), timestamp=f"{day}T19:{i:02}:00",
                             home="Extra H", away="Extra A", ht="0-0", ft="0-0", value="Égal"))
        changed = repeated_scenarios(rows, 2)["A"]
        self.assertEqual(forecast(original, chosen), forecast(changed, chosen))

    def test_a_future_group_uses_one_cutoff_even_across_several_dates(self):
        chosen = fixtures(day="2026-01-20")
        chosen[1]["timestamp"] = "2026-02-10T12:00:00"
        result = forecast(sample(), chosen)
        self.assertEqual(result["before"], "2026-01-20")
        self.assertTrue(all(item["before"] == "2026-01-20" for item in result["club_estimates"]))

    def test_small_history_returns_a_diagnostic_and_no_ranking(self):
        result = forecast(sample(days=5), fixtures())
        self.assertFalse(result["eligible"])
        self.assertEqual(result["ranking"], [])
        self.assertIsNone(result["normalization"])
        self.assertIn("20 groupes", result["diagnostic"])

    def test_empty_history_is_insufficient_and_does_not_crash(self):
        result = forecast({"size": 2, "matches": [], "blocks": []}, fixtures())
        self.assertFalse(result["eligible"])
        self.assertEqual(result["training_groups"], 0)
        self.assertEqual(result["training_matches"], 0)

    def test_later_cutoff_duplicate_mixed_league_source_and_wrong_size_are_refused(self):
        result, chosen = sample(), fixtures()
        cases = [
            (chosen, "2026-02-02"),
            ([chosen[0], chosen[0]], None),
            ([chosen[0], dict(chosen[1], league="B")], None),
            ([chosen[0], dict(chosen[1], provider="other")], None),
            (chosen[:1], None),
            (chosen + [dict(chosen[1], timestamp="2026-02-02T12:00:00")], None),
        ]
        for candidates, cutoff in cases:
            with self.subTest(candidates=candidates, cutoff=cutoff), self.assertRaises(ValueError):
                forecast(result, candidates, cutoff)
        with self.assertRaises(ValueError):
            forecast(result, chosen, limit=True)


class ChronologicalEvaluationTests(unittest.TestCase):
    def test_metrics_compare_the_three_models_on_identical_targets(self):
        result = evaluate(sample(days=15))
        self.assertEqual(result["summary"]["tested"], 10)
        self.assertEqual(result["trials"][0]["training_groups"], 20)
        self.assertEqual(result["trials"][0]["training_matches"], 40)
        self.assertEqual(set(result["summary"]["models"]), {"frequency", "clubs", "combined"})
        for name, summary in result["summary"]["models"].items():
            scores = [trial["models"][name] for trial in result["trials"]]
            self.assertEqual(summary["tested"], len(scores))
            self.assertEqual(summary["top3"], sum(score["top3"] for score in scores))
            self.assertAlmostEqual(summary["loss"], sum(-log(score["probability"]) for score in scores) / len(scores))
            self.assertTrue(all(score["loss"] >= 0 for score in scores))

    def test_mutating_future_and_same_day_targets_does_not_change_earlier_trials(self):
        original = sample(days=20)
        changed = copy.deepcopy(original)
        cutoff = "2026-01-16"
        for row in changed["matches"]:
            if row["timestamp"][:10] >= cutoff:
                row.update(ht="0-0", ft="0-0", value="Égal")
        for block in changed["blocks"]:
            block["sequence"] = tuple(row["value"] for row in block["matches"])
        a, b = evaluate(original), evaluate(changed)
        self.assertEqual([trial for trial in a["trials"] if trial["day"] < cutoff],
                         [trial for trial in b["trials"] if trial["day"] < cutoff])
        target = next(block for block in original["blocks"] if block["matches"][0]["timestamp"][:10] == cutoff)
        chosen = [{key: row[key] for key in ("home", "away", "timestamp", "league", "match_id")} for row in target["matches"]]
        self.assertEqual(forecast(original, chosen), forecast(changed, chosen))

    def test_no_trials_when_all_history_is_on_the_same_day(self):
        history = sample(days=15)
        for row in history["matches"]:
            row["timestamp"] = "2026-01-01T12:00:00"
        result = evaluate(history)
        self.assertEqual(result["trials"], [])
        self.assertEqual(result["summary"]["tested"], 0)
        self.assertTrue(all(model["loss"] is None for model in result["summary"]["models"].values()))


if __name__ == "__main__":
    unittest.main()
