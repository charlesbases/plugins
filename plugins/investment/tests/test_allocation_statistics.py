"""Synthetic contracts for time exclusion and statistical failure modes."""

import copy
import datetime as dt
import importlib.util
import math
import unittest
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/allocation_statistics.py"
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
            "maximum_ci_half_width": .005, "design_effect": .01, "target_power": .8,
            "mc_cdf_tolerance": .04, "mc_failure_probability": .05,
            "max_bootstrap_repetitions": 3000, "bootstrap_seed": 541,
            "block_sensitivity_factors": [.5, 1, 2], "preregistered_single_strategy": True,
            "max_observation_gap_days": 7, "minimum_tail_observations": 10,
            "assumption_review": {"weak_stationarity": True, "weak_dependence": True,
                                  "tail_quantile_regular": True, "evidence": "synthetic bounded DGP"}}


def sample_rows(n=220):
    rows = []
    for i in range(n):
        for code, shift in (("A", 0), ("B", .25)):
            x = [math.sin(i/8)+shift, math.cos(i/13)]
            rows.append({"decision_date": day(i), "label_available_date": day(i+5),
                         "code": code, "fund_group_id": code, "x": x, "horizon_days": 3,
                         "return": .015*x[0] + .005*x[1] + .004*math.sin(i/3)})
    return rows


def prediction_rows():
    return [{"decision_date": day(200), "code": c, "x": [.2+s, .1], "horizon_days": 3}
            for c, s in (("A", 0), ("B", .25))]


class FitContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = sample_rows()
        cls.result = stats.fit_predict(cls.rows, prediction_rows(), training_policy())

    def test_future_features_and_pending_outcomes_do_not_affect_any_output(self):
        changed = copy.deepcopy(self.rows)
        for row in changed:
            if row["label_available_date"] >= day(200):
                row["return"] = 100000
                row["x"] = [100000, -100000]
        self.assertEqual(self.result, stats.fit_predict(changed, prediction_rows(), training_policy()))

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

    def test_joint_scenarios_preserve_synchronous_covariance(self):
        joint = self.result["joint_scenarios"]
        self.assertEqual(joint["codes"], ["A", "B"])
        actual = np.array(joint["returns"])
        historical = np.array([[next(r["return"] for r in self.rows
                                     if r["decision_date"] == d and r["code"] == c)
                                for c in joint["codes"]] for d in joint["dates"]])
        np.testing.assert_allclose(np.cov(actual.T), np.cov(historical.T), atol=1e-15)
        np.testing.assert_allclose(actual.mean(axis=0),
                                   [r["expected_gross_return"] for r in self.result["forecasts"]])
        self.assertLess(joint["dates"][-1], day(195))

    def test_missing_code_history_cannot_invent_joint_distribution(self):
        rows = [r for r in self.rows if r["code"] == "A"]
        result = stats.fit_predict(rows, prediction_rows(), training_policy())
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["reason"], "insufficient_synchronous_mature_history")

    def test_share_class_duplicates_have_no_extra_group_weight(self):
        rows = [{"decision_date": day(0), "fund_group_id": "same"},
                {"decision_date": day(0), "fund_group_id": "same"},
                {"decision_date": day(0), "fund_group_id": "other"}]
        np.testing.assert_allclose(stats._weights(rows), [.25, .25, .5])

    def test_duplicate_and_impossible_label_fail(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            stats.fit_predict(self.rows + [self.rows[150]], prediction_rows(), training_policy())
        rows = copy.deepcopy(self.rows)
        rows[150]["label_available_date"] = rows[150]["decision_date"]
        with self.assertRaisesRegex(ValueError, "horizon"):
            stats.fit_predict(rows, prediction_rows(), training_policy())


class InferenceContracts(unittest.TestCase):
    def test_no_advantage_and_short_series_cannot_pass(self):
        values = np.random.default_rng(78).normal(0, .004, 220)
        values -= values.mean()
        result = stats.evaluate_advantage([day(i) for i in range(len(values))], values.tolist(), inference_policy())
        self.assertNotEqual(result["status"], "passed")
        result = stats.evaluate_advantage([day(i) for i in range(10)], [.03]*10, inference_policy())
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_positive_design_and_deterministic_bootstrap_precision(self):
        values = np.random.default_rng(72).normal(.03, .004, 220).tolist()
        dates = [day(i) for i in range(len(values))]
        first = stats.evaluate_advantage(dates, values, inference_policy())
        self.assertEqual(first, stats.evaluate_advantage(dates, values, inference_policy()))
        self.assertEqual(first["status"], "passed")
        expected = math.ceil(math.log(2*3/.05)/(2*.04**2))
        self.assertEqual(first["bootstrap_repetitions"], expected)
        self.assertEqual(len(first["sensitivity"]), 3)

    def test_assumptions_selection_precision_and_budget_are_required(self):
        values = np.random.default_rng(7).normal(.03, .01, 100).tolist()
        dates = [day(i) for i in range(len(values))]
        for key, value in (("preregistered_single_strategy", False), ("assumption_review", {}),
                           ("maximum_ci_half_width", .000000001), ("max_bootstrap_repetitions", 50)):
            policy = inference_policy()
            policy[key] = value
            self.assertEqual(stats.evaluate_advantage(dates, values, policy)["status"], "insufficient_evidence")

    def test_unqualified_runs_keep_conditional_diagnostics_but_never_pass(self):
        policy = inference_policy()
        policy["preregistered_single_strategy"] = False
        policy["assumption_review"] = {}
        values = np.random.default_rng(47).normal(.03, .004, 100).tolist()
        result = stats.evaluate_advantage([day(i) for i in range(100)], values, policy)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIsNotNone(result["interval"])
        self.assertTrue(result["conditional_diagnostic_only"])
        self.assertEqual(len(result["qualification_limitations"]), 2)

    def test_empty_outcomes_are_insufficient_not_nan_or_error(self):
        result = stats.evaluate_advantage([], [], inference_policy())
        self.assertEqual(result["reason"], "no_mature_observations")
        self.assertIsNone(result["estimate"])
        result = stats.evaluate_tail_risk([], [], .05, .25, inference_policy())
        self.assertEqual(result["reason"], "no_mature_observations")
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
            passed += result["status"] == "passed"
        self.assertLessEqual(passed, 4)

    def test_positive_autocorrelation_increases_selected_block_length(self):
        noise = np.random.default_rng(821).normal(size=1000)
        ar = np.zeros(1000)
        for i in range(1, len(ar)):
            ar[i] = .85*ar[i-1]+noise[i]
        self.assertGreater(stats.optimal_stationary_block(ar)["length"],
                           stats.optimal_stationary_block(noise)["length"]*2)

    def test_tail_risk_requires_support_and_detects_budget_violation(self):
        values = np.random.default_rng(452).normal(.03, .003, 250).tolist()
        dates = [day(i) for i in range(len(values))]
        policy = inference_policy()
        result = stats.evaluate_tail_risk(dates, values, .1, .01, policy)
        self.assertEqual(result["status"], "failed")
        result = stats.evaluate_tail_risk(dates[:50], values[:50], .05, .25, policy)
        self.assertEqual(result["reason"], "insufficient_tail_observations")
        policy["assumption_review"]["tail_quantile_regular"] = False
        result = stats.evaluate_tail_risk(dates, values, .1, .25, policy)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("tail_quantile_regularity_not_reviewed", result["qualification_limitations"])
        self.assertIsNotNone(result["interval"])


if __name__ == "__main__":
    unittest.main()
