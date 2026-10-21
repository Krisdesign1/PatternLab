import copy
from datetime import date, timedelta
import importlib.util
from itertools import product
import json
from math import prod
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import catboost_scenarios as ml
from league_model import VALUES
from scenario_predictor import fit_sequences, probability


def sample(count=240, league="39", provider="api-football"):
    scores = (("2-0", "2-0", "1st"), ("1-0", "1-1", "Égal"), ("0-0", "1-0", "2nd"))
    rows = []
    for i in range(count):
        day = date(2025, 1, 1) + timedelta(days=i // 4)
        ht, ft, value = scores[i % 3]
        rows.append(dict(match_id=str(i + 1), league=league, provider=provider,
                         home="H" + str(i % 8), away="A" + str(i % 7),
                         home_id=str(100 + i % 8), away_id=str(200 + i % 7), season=2024,
                         timestamp=f"{day.isoformat()}T{12+i%4:02}:00:00", ht=ht, ft=ft, value=value))
    return rows


def fixtures():
    return [dict(league="39", provider="api-football", home="H" + str(i), away="A" + str(i),
                 home_id=str(100 + i), away_id=str(200 + i), season=2025,
                 timestamp=f"2025-03-20T{12+i:02}:00:00", match_id=str(9000 + i)) for i in range(4)]


class DistributionTests(unittest.TestCase):
    def test_exact_partition_and_top_three_for_all_alphas(self):
        for size in (2, 4):
            fit = fit_sequences([("1st",) * size] * 12 + [("2nd",) * size] * 8, size)
            distributions = [{"1st": .25, "Égal": .25, "2nd": .5}] * size
            for alpha in ml.ALPHAS:
                with self.subTest(size=size, alpha=alpha):
                    model = ml._distribution(fit, distributions, alpha)
                    raw = {}
                    for sequence in product(VALUES, repeat=size):
                        q = prod(p[v] for p, v in zip(distributions, sequence))
                        raw[sequence] = q * (probability(fit, sequence) / probability(fit, sequence, baseline=True)) ** alpha
                    z = sum(raw.values())
                    self.assertAlmostEqual(model["z"], z)
                    self.assertAlmostEqual(sum(ml._details(model, seq)["probability"] for seq in raw), 1)
                    self.assertEqual([row["sequence"] for row in ml._rank(model)],
                                     sorted(raw, key=lambda seq: (-raw[seq], seq))[:3])

    def test_twenty_match_ranking_is_bounded_and_alpha_zero_is_plain_product(self):
        fit = fit_sequences([("1st",) * 20] * 30, 20)
        model = ml._distribution(fit, [{"1st": .25, "Égal": .25, "2nd": .5}] * 20, 0)
        ranked = ml._rank(model)
        self.assertEqual(ranked[0]["sequence"], ("2nd",) * 20)
        self.assertEqual(len(ranked), 3)
        self.assertAlmostEqual(ranked[0]["probability"], .5 ** 20)

    def test_whole_group_features_and_baselines_share_the_first_day_cutoff(self):
        history = sample(40)
        block = {"matches": [history[0], history[8], history[12], history[20]], "sequence": ("1st",) * 4}
        class FeatureStub:
            def __init__(self):
                self.cutoffs = []
            def feature_for_fixture(self, rows, fixture, before=None):
                self.cutoffs.append(before)
                return {"cutoff": before}
        module = FeatureStub()
        with patch.object(ml, "estimate", return_value={"probabilities": dict.fromkeys(VALUES, 1 / 3)}) as estimate:
            ml._group_inputs(module, history, [block])
        self.assertEqual(module.cutoffs, ["2025-01-01"] * 4)
        self.assertTrue(all(call.args[-1] == "2025-01-01" for call in estimate.call_args_list))

    def test_source_scope_and_utc_cutoff_precede_result_validation(self):
        rows = sample(12)
        future = dict(rows[0], match_id="999", timestamp="2025-03-20T00:30:00+02:00", ht=None, value="invalid")
        self.assertEqual(ml._rows(rows + [future], "39", "API-Football", "2025-03-19"), rows)
        other = [dict(row, league="61", value="invalid") for row in rows]
        self.assertEqual(ml._rows(rows + other, "39", "API-Football"), rows)


@unittest.skipUnless(importlib.util.find_spec("catboost"), "CatBoost optional runtime is not installed")
class NativeRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.rows = sample()
        with patch.object(ml, "CANDIDATES", ({"depth": 2, "iterations": 8, "l2_leaf_reg": 5},)), \
             patch.object(ml, "ALPHAS", (0.0, .5)), patch.object(ml, "_today", return_value="2025-03-15"):
            cls.report = ml.train(cls.rows, "39", "API-Football", cls.directory.name)

    def test_actual_native_training_roundtrip_and_identical_test_targets(self):
        report = ml.load_report("API-Football", "39", self.directory.name)
        self.assertEqual(report, self.report)
        self.assertEqual(report["source"], "API-Football")
        self.assertEqual(set(report["validation"]), set(ml.METHODS))
        self.assertEqual(set(report["test"]), set(ml.METHODS))
        self.assertLess(report["split"]["train"]["end"], report["split"]["validation"]["start"])
        self.assertLess(report["split"]["validation"]["end"], report["split"]["test"]["start"])
        self.assertFalse(report["policy"]["test_selection"])
        self.assertEqual(len({row["tested"] for row in report["test"].values()}), 1)
        generation = Path(self.directory.name) / "api-football" / "39" / report["deployment"]["generation"]
        self.assertTrue((generation / "model.cbm").exists())
        self.assertEqual(list(generation.glob("*.pkl")), [])
        result = ml.forecast(self.rows, fixtures(), "API-Football", self.directory.name)
        self.assertTrue(result["eligible"])
        self.assertEqual(len(result["ranking"]), 3)
        self.assertTrue(all(0 < row["probability"] < 1 for row in result["ranking"]))
        self.assertEqual(result["validated_size"], 4)

    def test_report_read_does_not_import_or_load_catboost(self):
        with patch.object(ml, "_classifier", side_effect=AssertionError("JSON report read must stay cheap")):
            self.assertIsNotNone(ml.load_report("API-Football", "39", self.directory.name))

    def test_target_and_future_outcomes_are_not_read_at_inference(self):
        original = ml.forecast(self.rows, fixtures(), "API-Football", self.directory.name)
        future = dict(self.rows[0], match_id="9999", timestamp="2025-03-20T11:00:00", value="invalid", ht=None, ft=None)
        chosen = [dict(row, value="target label ignored", ht=None) for row in fixtures()]
        changed = ml.forecast(self.rows + [future], chosen, "API-Football", self.directory.name)
        self.assertEqual(original, changed)

    def test_only_own_training_scope_corrections_mark_artifact_stale(self):
        other = [dict(row, league="61", value="invalid") for row in self.rows]
        self.assertEqual(ml.forecast(self.rows, fixtures(), "API-Football", self.directory.name),
                         ml.forecast(self.rows + other, fixtures(), "API-Football", self.directory.name))
        changed = copy.deepcopy(self.rows)
        changed[0].update(ht="0-0", ft="0-0", value="Égal")
        with self.assertRaisesRegex(ml.ModelError, "changé"):
            ml.forecast(changed, fixtures(), "API-Football", self.directory.name)

    def test_model_cannot_predict_a_day_already_seen_in_training_including_offsets(self):
        chosen = fixtures()
        for row in chosen:
            row["timestamp"] = self.report["deployment"]["trained_through"] + "T20:00:00"
        with self.assertRaisesRegex(ml.ModelError, "déjà vu"):
            ml.forecast(self.rows, chosen, "API-Football", self.directory.name)
        last = date.fromisoformat(self.report["deployment"]["trained_through"])
        for row in chosen:
            row["timestamp"] = (last + timedelta(days=1)).isoformat() + "T00:30:00+02:00"
        with self.assertRaisesRegex(ml.ModelError, "déjà vu"):
            ml.forecast(self.rows, chosen, "API-Football", self.directory.name)

    def test_no_report_for_other_provider_and_mixed_source_fixtures_are_refused(self):
        with self.assertRaises(ml.ModelError):
            ml.load_report("football-data.org", "39", self.directory.name)
        chosen = fixtures()
        chosen[0]["provider"] = "football-data.org"
        with self.assertRaises(ml.ModelError):
            ml.forecast(self.rows, chosen, "API-Football", self.directory.name)

    def test_validation_selection_is_unchanged_when_only_test_labels_change(self):
        changed = copy.deepcopy(self.rows)
        test_start = self.report["split"]["test"]["start"]
        for row in changed:
            if row["timestamp"][:10] >= test_start:
                row.update(ht="0-0", ft="0-0", value="Égal")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ml, "CANDIDATES", ({"depth": 2, "iterations": 8, "l2_leaf_reg": 5},)), \
             patch.object(ml, "ALPHAS", (0.0, .5)), patch.object(ml, "_today", return_value="2025-03-15"):
            report = ml.train(changed, "39", "API-Football", tmp)
        self.assertEqual(self.report["selection"], report["selection"])
        self.assertEqual(self.report["validation"], report["validation"])

    def test_invalid_model_bytes_or_report_pointer_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(self.directory.name, tmp, dirs_exist_ok=True)
            base = Path(tmp) / "api-football" / "39"
            model_file = base / self.report["deployment"]["generation"] / "model.cbm"
            model_file.write_bytes(b"corrupt native model")
            with self.assertRaisesRegex(ml.ModelError, "fichier"):
                ml.forecast(self.rows, fixtures(), "API-Football", tmp)
            (base / "CURRENT.json").write_text('{"generation":"../unsafe"}')
            with self.assertRaises(ml.ModelError):
                ml.load_report("API-Football", "39", tmp)

    def test_feature_version_change_requires_retraining(self):
        module = ml._features()
        with patch.object(module, "FEATURE_VERSION", "future-feature-schema"):
            with self.assertRaisesRegex(ml.ModelError, "caractéristiques"):
                ml.forecast(self.rows, fixtures(), "API-Football", self.directory.name)

    def test_failed_pointer_commit_keeps_the_previous_complete_generation_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(self.directory.name, tmp, dirs_exist_ok=True)
            path = ml._location("API-Football", "39", tmp) / self.report["deployment"]["generation"] / "model.cbm"
            model = ml._classifier()()
            model.load_model(str(path), format="cbm")
            report = copy.deepcopy(self.report)
            report["created_at"] = "2025-03-16T12:00:00+00:00"
            real_replace = ml.os.replace
            def fail_pointer(source, target):
                if Path(target).name == "CURRENT.json":
                    raise OSError("failed pointer commit")
                return real_replace(source, target)
            with patch.object(ml.os, "replace", side_effect=fail_pointer):
                with self.assertRaises(OSError):
                    ml._publish(model, report, "API-Football", "39", tmp)
            self.assertEqual(ml.load_report("API-Football", "39", tmp), self.report)

    def test_training_never_reads_excluded_current_day_result_fields(self):
        future = dict(self.rows[0], match_id="99999", timestamp="2025-03-15T12:00:00", ht=None, ft=None, value="invalid")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ml, "CANDIDATES", ({"depth": 2, "iterations": 8, "l2_leaf_reg": 5},)), \
             patch.object(ml, "ALPHAS", (0.0, .5)), patch.object(ml, "_today", return_value="2025-03-15"):
            report = ml.train(self.rows + [future], "39", "API-Football", tmp)
        self.assertEqual(report["selection"], self.report["selection"])
        self.assertEqual(report["deployment"]["excluded_current_or_future"], 1)

    def test_explicit_cutoff_cannot_be_future_or_invalid(self):
        with patch.object(ml,'_today',return_value='2025-03-15'), \
             patch.object(ml,'_fit',side_effect=AssertionError('No fit for invalid cutoff')):
            for before in ('2025-03-16','2025-02-30','2025-03-15T12:00:00',True):
                with self.subTest(before=before),self.assertRaises(ml.ModelError):
                    ml.train(self.rows,'39','API-Football',before=before)


if __name__ == "__main__":
    unittest.main()
