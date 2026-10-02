"""Synthetic contracts for time exclusion and statistical failure modes."""

import copy
import datetime as dt
import importlib.util
import math
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
from arch.bootstrap import StationaryBootstrap, optimal_block_length

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/allocation_statistics.py"
sys.path.insert(0,str(SCRIPT.parent))
from allocation_market import LATENT_NAMES

SPEC = importlib.util.spec_from_file_location("allocation_statistics_tested", SCRIPT)
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)


def day(i):
    return (dt.date(2020, 1, 1) + dt.timedelta(days=i)).isoformat()


def training_policy():
    return {"train_window_days": 150, "min_train_dates": 20, "cv_folds": 3,
            "alpha_grid": [.00001, .001], "l1_ratio_grid": [.1, .9], "min_joint_dates": 30,
            "cv_initial_train_fraction": .5, "feature_names": ["signal", "risk"]}


def inference_policy():
    return {"confidence_level": .8, "minimum_net_advantage": .001,
            "maximum_ci_half_width": .005,
            "mc_cdf_tolerance": .04, "mc_failure_probability": .05,
            "max_bootstrap_repetitions": 3000, "bootstrap_seed": 541,
            "block_sensitivity_factors": [.5, 1, 2],
            "max_observation_gap_days": 7, "minimum_tail_observations": 10}


def enrich_row(row):
    import allocation_market as market
    from contracts import fingerprint
    result=copy.deepcopy(row)
    origin=dt.date.fromisoformat(row["decision_date"])
    outcome=1+row["return"]
    source={"code":row["code"],"base_date":str(origin-dt.timedelta(days=1)),"base_nav":1.,
        "pricing_date":str(origin),"pricing_nav":1.,"ownership_date":str(origin),
        "confirmation_date":str(origin),"end_date":str(origin+dt.timedelta(days=row["horizon_days"])),
        "terminal_nav":outcome,"dividends":[],"label_available":row["label_available_date"],
        "clock":{},"feature_cutoff_date":str(origin-dt.timedelta(days=1)),
        "dividend_values":{"hold":0.,"buy":0.,"sell":0.},
        "scope":"synthetic primitive prices and zero observed distributions, not market or fill evidence"}
    source["targets"]=market.recompute_source_targets(source)
    source["latent_targets"]=market.recompute_source_latents(source)
    source["source_hash"]=fingerprint(source)
    result.update(targets=source["targets"],latent_targets=source["latent_targets"],
        label_source=source,feature_cutoff_date=source["feature_cutoff_date"],exit_date=source["end_date"])
    return result


def fit_current(rows, prediction, policy):
    return stats.fit_joint_targets(rows,prediction,policy,{"order_time_local":"20:00:00","assets":{}})


def sample_rows(n=220):
    rows = []
    for i in range(n):
        for code, shift in (("A", 0), ("B", .25)):
            x = [math.sin(i/8)+shift, math.cos(i/13)]
            rows.append({"decision_date": day(i), "label_available_date": day(i+5),
                         "code": code, "fund_group_id": code, "x": x, "horizon_days": 3,
                         "return": .015*x[0] + .005*x[1] + .004*math.sin(i/3)})
    return [enrich_row(row) for row in rows]


def prediction_rows():
    return [{"decision_date": day(200), "code": c, "x": [.2+s, .1], "horizon_days": 3}
            for c, s in (("A", 0), ("B", .25))]


class FitContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = sample_rows()
        cls.result = fit_current(cls.rows, prediction_rows(), training_policy())

    def test_future_features_and_pending_outcomes_do_not_affect_any_output(self):
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row["label_available_date"] >= day(200):
                row["return"] = 100000
                row["x"] = [100000, -100000]
        self.assertEqual(self.result, fit_current(changed, prediction_rows(), training_policy()))

    def test_purged_cv_and_final_fit_use_strictly_available_labels(self):
        result = self.result
        self.assertEqual(result["status"], "research_ready")
        self.assertLess(result["training_audit"]["max_label_available_date"], day(200))
        self.assertEqual(result["training_audit"]["training_window_start"], day(50))
        for fold in result["training_audit"]["folds"]:
            self.assertLess(fold["training_max_label_available"], fold["validation_start"])
        expected = np.array([r["x"] for r in self.rows if day(50) <= r["decision_date"]
                             and r["label_available_date"] < day(200)]).mean(axis=0)
        np.testing.assert_allclose(result["models"][0]["scaler_mean"], expected, atol=1e-14)

    def test_known_joint_prediction_errors_keep_bias_and_dependence(self):
        rows = [{"decision_date": day(i), "label_available_date": day(i+1), "code": code,
                 "fund_group_id": code, "x": [0], "horizon_days": 1, "return": value}
                for i, outcomes in ((1, (.2, -.2)), (2, (-.1, .1)), (5, (.03, -.03)), (6, (-.02, .02)))
                for code, value in zip(("A", "B"), outcomes)]
        prediction = [{"decision_date": day(10), "horizon_days": 1, "code": code, "x": [0]}
                      for code in ("A", "B")]
        rows = [enrich_row(row) for row in rows]
        policy = {**training_policy(), "min_joint_dates": 2}
        def known_mean(samples, requested, policy, training_ceiling_at=None):
            means = (math.log(1.1), math.log(.9)) if requested[0]["decision_date"] == day(10) else (0, 0)
            return {"status": "fitted", "models": [{"fixture": "known_forecast", "target_names": list(LATENT_NAMES)}], "training_audit": {},
                    "forecasts": [{"code": code, "predicted_coordinates": [0,value,0,0,0,0]}
                                  for code, value in zip(("A", "B"), means)]}
        with patch.object(stats, "_fit_mean_at", side_effect=known_mean):
            result = fit_current(rows, prediction, policy)
        np.testing.assert_allclose(result["joint_scenarios"]["returns"], [[.32, -.28], [-.01, -.01]], atol=1e-14)
        np.testing.assert_allclose([row["expected_gross_return"] for row in result["forecasts"]], [.155, -.145], atol=1e-14)
        self.assertEqual(result["joint_scenarios"]["dates"], [day(1), day(2)])
        self.assertEqual([row["date"] for row in result["joint_scenarios"]["source_calibration_oos"]], [day(5), day(6)])
        self.assertTrue(all(not row["historically_sealed"] for row in result["training_audit"]["oos_predictions"]))

    def test_later_training_changes_cannot_reselect_past_oos_parameters(self):
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row["decision_date"] >= day(150):
                row["return"] += .5
                row["x"] = [20, -20]
        changed = [enrich_row(row) for row in changed]
        revised = fit_current(changed, prediction_rows(), training_policy())
        old = {row["date"]: row for row in self.result["training_audit"]["oos_predictions"]
               if row["date"] < day(150)}
        new = {row["date"]: row for row in revised["training_audit"]["oos_predictions"]
               if row["date"] < day(150)}
        self.assertEqual(old, new)
        self.assertTrue(old)

    def test_missing_code_history_cannot_invent_joint_distribution(self):
        rows = [r for r in self.rows if r["code"] == "A"]
        result = fit_current(rows, prediction_rows(), training_policy())
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["reason"], "insufficient_synchronous_forward_joint_target_history")

    def test_share_class_duplicates_have_no_extra_group_weight(self):
        rows = [{"decision_date": day(0), "fund_group_id": "same"},
                {"decision_date": day(0), "fund_group_id": "same"},
                {"decision_date": day(0), "fund_group_id": "other"}]
        np.testing.assert_allclose(stats._weights(rows), [.25, .25, .5])

    def test_duplicate_and_impossible_label_fail(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            fit_current(self.rows + [self.rows[150]], prediction_rows(), training_policy())
        rows = copy.deepcopy(self.rows)
        rows[150]["label_available_date"] = rows[150]["decision_date"]
        with self.assertRaisesRegex(ValueError, "horizon"):
            fit_current(rows, prediction_rows(), training_policy())


class InferenceContracts(unittest.TestCase):
    def test_no_advantage_and_short_series_cannot_pass(self):
        values = np.random.default_rng(78).normal(0, .004, 220)
        values -= values.mean()
        result = stats.evaluate_advantage([day(i) for i in range(len(values))], values.tolist(), inference_policy())
        self.assertEqual(result["status"], "not_supported")
        result = stats.evaluate_advantage([day(i) for i in range(10)], [.03]*10, inference_policy())
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_positive_design_and_deterministic_bootstrap_precision(self):
        values = np.random.default_rng(72).normal(.03, .004, 220).tolist()
        dates = [day(i) for i in range(len(values))]
        first = stats.evaluate_advantage(dates, values, inference_policy())
        self.assertEqual(first, stats.evaluate_advantage(dates, values, inference_policy()))
        self.assertEqual(first["status"], "supported")
        expected = math.ceil(math.log(2*3/.05)/(2*.04**2))
        self.assertEqual(first["bootstrap_repetitions"], expected)
        self.assertEqual(len(first["sensitivity"]), 3)

    def test_precision_and_budget_are_required(self):
        values = np.random.default_rng(7).normal(.03, .01, 100).tolist()
        dates = [day(i) for i in range(len(values))]
        for key, value in (("maximum_ci_half_width", .000000001), ("max_bootstrap_repetitions", 50)):
            policy = inference_policy()
            policy[key] = value
            self.assertEqual(stats.evaluate_advantage(dates, values, policy)["status"], "insufficient_evidence")

    def test_unknown_policy_fields_are_rejected(self):
        policy = inference_policy()
        policy["unexpected_field"] = True
        values = np.random.default_rng(47).normal(.03, .004, 100).tolist()
        with self.assertRaisesRegex(ValueError, "Unknown inference policy"):
            stats.evaluate_advantage([day(i) for i in range(100)], values, policy)

    def test_interval_crossing_advantage_is_inconclusive(self):
        values = np.random.default_rng(74).normal(0, .004, 220)
        values += .001-values.mean()
        result = stats.evaluate_advantage([day(i) for i in range(220)], values.tolist(), inference_policy())
        self.assertEqual(result["status"], "inconclusive")

    def test_empty_outcomes_are_insufficient_not_nan_or_error(self):
        result = stats.evaluate_advantage([], [], inference_policy())
        self.assertEqual(result["reason"], "no_mature_observations")
        self.assertIsNone(result["estimate"])
        with self.assertRaisesRegex(ValueError, "Paired"):
            stats.evaluate_advantage([day(0)], [], inference_policy())

    def test_degenerate_constant_series_not_false_certainty(self):
        result = stats.evaluate_advantage([day(i) for i in range(100)], [.03]*100, inference_policy())
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_many_null_series_not_routinely_accepted(self):
        # Finite deterministic size check, not proof of asymptotic validity.
        policy = inference_policy()
        policy["minimum_net_advantage"] = 0
        passed = 0
        for seed in range(20):
            values = np.random.default_rng(seed+400).normal(0, .005, 120).tolist()
            result = stats.evaluate_advantage([day(i) for i in range(120)], values, policy)
            passed += result["status"] == "supported"
        self.assertLessEqual(passed, 4)

    def test_positive_autocorrelation_increases_selected_block_length(self):
        noise = np.random.default_rng(821).normal(size=1000)
        ar = np.zeros(1000)
        for i in range(1, len(ar)):
            ar[i] = .85*ar[i-1]+noise[i]
        self.assertGreater(stats.optimal_stationary_block(ar)["length"],
                           stats.optimal_stationary_block(noise)["length"]*2)

    def test_selector_matches_arch_and_is_scale_invariant(self):
        values = np.random.default_rng(139).normal(size=200)
        for i in range(1, len(values)):
            values[i] += .7*values[i-1]
        expected = max(1., float(optimal_block_length(values)["stationary"].iloc[0]))
        for scale in (1, 1e-12, 100):
            self.assertAlmostEqual(stats.optimal_stationary_block(values*scale)["length"], expected, places=10)

    def test_resampling_matches_arch_distribution_with_same_seed(self):
        values = np.arange(24.)
        expected = [data[0][0].mean() for data in StationaryBootstrap(3.5, values, seed=173).bootstrap(257)]
        actual = stats._bootstrap(values, 3.5, 257, 173, lambda matrix: matrix.mean(axis=1))
        np.testing.assert_array_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()
