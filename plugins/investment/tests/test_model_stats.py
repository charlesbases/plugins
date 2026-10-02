"""Finite synthetic validation contracts; never evidence of investment advantage."""

import copy
import datetime as dt
import importlib.util
import json
import math
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/model_stats.py"
SPEC = importlib.util.spec_from_file_location("investment_model_stats", SCRIPT)
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)
FEATURES = ["momentum20", "momentum60", "momentum120", "volatility60", "drawdown60"]


def samples(count=500):
    result = []
    start = dt.date(2020, 1, 1)
    for i in range(count):
        date = start + dt.timedelta(days=i)
        x = [math.sin(i / 7), math.cos(i / 13), math.sin(i / 23), .03 + .01 * math.cos(i / 5),
             .1 + .05 * math.sin(i / 17)]
        ret = .02 * x[0] - .01 * x[1] + .015 * x[2]
        result.append({"id": str(i), "code": "123456", "fund_group_id": "portfolio-a",
                       "cutoff_date": date.isoformat(), "decision_date": date.isoformat(),
                       "entry_date": date.isoformat(), "exit_date": (date + dt.timedelta(days=10)).isoformat(),
                       "label_available_proxy": (date + dt.timedelta(days=12)).isoformat(),
                       "horizon_days": 10, "x": x, "y": {"net_return": ret, "terminal_loss": max(-ret, 0),
                       "principal_path_loss": .05 + .02 * x[0], "peak_drawdown": .1 + .03 * x[1]},
                       "status": "mature", "scope": "conditional_fee_scenario", "assumptions": {}})
    return result


class ModelStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = samples()
        cls.policy = stats.freeze_policy(cls.samples, FEATURES)
        cls.result = stats.fit_evaluate(cls.samples, cls.policy)

    def test_purges_every_training_and_calibration_boundary(self):
        index = {row["id"]: row for row in self.samples}
        boundary = self.policy["calibration_start"]
        test_start = self.policy["test_start"]
        split = self.result["splits"]["by_horizon"][0]
        for fold in split["folds"]:
            self.assertLess(fold["training_max_label_proxy"], fold["prediction_start"])
            self.assertTrue(set(fold["train_ids"]).isdisjoint(fold["validation_ids"]))
        for ident in split["train_ids"]:
            self.assertLess(index[ident]["label_available_proxy"], boundary)
        for ident in split["calibration_ids"]:
            self.assertGreaterEqual(index[ident]["decision_date"], boundary)
            self.assertLess(index[ident]["label_available_proxy"], test_start)
        for ident in split["test_ids"]:
            self.assertGreaterEqual(index[ident]["decision_date"], test_start)
        # Labels exactly at the boundary must be excluded, not admitted by <=.
        on_boundary = next(r for r in self.samples if r["label_available_proxy"] == boundary)
        self.assertNotIn(on_boundary["id"], split["train_ids"])

    def test_standardization_and_baseline_use_purged_training_only(self):
        ids = set(self.result["splits"]["by_horizon"][0]["train_ids"])
        train = [r for r in self.samples if r["id"] in ids]
        model = next(m for m in self.result["models"] if m["target"] == "net_return")
        for column, actual in enumerate(model["scaler_mean"]):
            expected = sum(r["x"][column] for r in train) / len(train)
            self.assertAlmostEqual(actual, expected, places=13)
            variance = sum((r["x"][column] - expected) ** 2 for r in train) / len(train)
            self.assertAlmostEqual(model["scaler_scale"][column], math.sqrt(variance), places=13)
        self.assertAlmostEqual(model["baseline_mean"], sum(r["y"]["net_return"] for r in train) / len(train), places=13)

    def test_frozen_policy_and_inputs_cannot_be_changed(self):
        changed = copy.deepcopy(self.policy)
        changed["coverage_tolerance"] = .9
        with self.assertRaisesRegex(stats.ModelError, "changed after freezing"):
            stats.fit_evaluate(self.samples, changed)
        changed_samples = copy.deepcopy(self.samples)
        changed_samples[-1]["y"]["net_return"] += .01
        with self.assertRaisesRegex(stats.ModelError, "changed after freezing"):
            stats.fit_evaluate(changed_samples, self.policy)
        first = stats.freeze_policy(self.samples, FEATURES)
        first["ablation_groups"]["momentum"].append(4)
        self.assertEqual(stats.freeze_policy(self.samples, FEATURES)["ablation_groups"]["momentum"], [0, 1, 2])

    def test_portable_prediction_known_relation_and_metric_recompute(self):
        model = next(m for m in self.result["models"] if m["target"] == "net_return")
        portable = json.loads(json.dumps(model, allow_nan=False))
        index = {r["id"]: r for r in self.samples}
        subset = [p for p in self.result["predictions"] if p["target"] == "net_return" and p["split"] == "test"]
        for record in subset:
            self.assertEqual(stats.predict_model(portable, index[record["sample_id"]]["x"]), record["predicted"])
        expected_rmse = math.sqrt(sum((p["actual"] - p["predicted"]) ** 2 for p in subset) / len(subset))
        metric = next(m for m in self.result["metrics"] if m["target"] == "net_return" and m["split"] == "test")
        self.assertAlmostEqual(metric["rmse"], expected_rmse, places=14)
        self.assertLess(metric["rmse"], .003)
        self.assertEqual(stats.evaluate_predictions(self.result["predictions"], self.policy), self.result["metrics"])
        self.assertEqual(len(self.result["cv_trials"]), 108)
        self.assertEqual(len(self.result["ablations"]), 12)
        json.dumps(self.result, allow_nan=False)

    def test_final_test_values_do_not_change_model_selection_or_calibration(self):
        changed = copy.deepcopy(self.samples)
        for row in changed:
            if row["decision_date"] >= self.policy["test_start"]:
                row["x"] = [v + 100 for v in row["x"]]
                row["y"] = {target: .7 for target in stats.TARGETS}
        result = stats.fit_evaluate(changed, stats.freeze_policy(changed, FEATURES))
        self.assertEqual(result["models"], self.result["models"])
        self.assertEqual(result["cv_trials"], self.result["cv_trials"])
        self.assertEqual(result["ablations"], self.result["ablations"])

    def test_bad_intervals_fail_and_small_samples_cannot_pass(self):
        records = [{"sample_id": r["id"], "code": r["code"], "fund_group_id": r["fund_group_id"],
                    "decision_date": r["decision_date"], "horizon_days": 10, "target": "net_return",
                    "actual": .3, "predicted": 0., "baseline_mean": 0., "lo80": -.01, "hi80": .01,
                    "lo90": -.02, "hi90": .02, "split": "test"} for r in self.samples]
        metric = stats.evaluate_predictions(records, self.policy)[0]
        self.assertEqual(metric["intervals"]["80"]["status"], "failed")
        self.assertEqual(metric["intervals"]["90"]["estimate"], 0)
        self.assertEqual(metric["gains"]["baseline_mean"]["status"], "failed")
        short = stats.evaluate_predictions(records[:20], self.policy)[0]
        self.assertEqual(short["intervals"]["80"]["status"], "insufficient_evidence")
        self.assertEqual(short["gains"]["zero"]["status"], "insufficient_evidence")

    def test_date_and_portfolio_weights_do_not_reward_duplicate_share_classes(self):
        rows = [{"decision_date": "2020-01-01", "fund_group_id": "same"},
                {"decision_date": "2020-01-01", "fund_group_id": "same"},
                {"decision_date": "2020-01-01", "fund_group_id": "other"},
                {"decision_date": "2020-01-02", "fund_group_id": "same"}]
        self.assertEqual(stats.weights(rows).tolist(), [.25, .25, .5, 1.])

    def test_malformed_inputs_and_portable_models_are_rejected(self):
        for field, value in (("x", [0., 0., float("nan"), 0., 0.]), ("horizon_days", True),
                             ("decision_date", "20200101"), ("label_available_proxy", "2010-01-01"),
                             ("fund_group_id", ""), ("y", {"net_return": 0.})):
            changed = copy.deepcopy(self.samples)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(stats.ModelError):
                stats.freeze_policy(changed, FEATURES)
        model = copy.deepcopy(self.result["models"][0])
        model["scaler_scale"][0] = 0
        with self.assertRaises(stats.ModelError):
            stats.predict_model(model, [0.] * 5)


if __name__ == "__main__":
    unittest.main()
