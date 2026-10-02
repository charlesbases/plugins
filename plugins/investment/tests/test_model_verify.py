"""Independent audit regression fixtures; never a replay of live investor trades."""

import copy
import datetime as dt
import importlib.util
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("investment_model_verify", SCRIPTS / "model_verify.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
stats = audit.model_stats


def synthetic_samples():
    rows = []
    for i in range(400):
        day = dt.date(2020, 1, 1) + dt.timedelta(days=i)
        for code, group, offset in (("111111", "same", 0.), ("111112", "same", .001), ("222222", "other", -.001)):
            x = [math.sin(i / 7) + offset, math.cos(i / 13), math.sin(i / 23),
                 .03 + .01 * math.cos(i / 5), .1 + .05 * math.sin(i / 17)]
            ret = .02 * x[0] - .01 * x[1] + .015 * x[2]
            rows.append({"id": code + ":" + str(i), "code": code, "fund_group_id": group,
                         "cutoff_date": str(day), "decision_date": str(day), "entry_date": str(day),
                         "exit_date": str(day + dt.timedelta(days=10)), "label_available_proxy": str(day + dt.timedelta(days=12)),
                         "horizon_days": 10, "x": x, "y": {"net_return": ret, "terminal_loss": max(-ret, 0),
                         "principal_path_loss": .05 + .02 * x[0], "peak_drawdown": .1 + .03 * x[1]},
                         "status": "mature", "scope": "conditional_fee_scenario", "assumptions": {}})
    return rows


class ModelVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = synthetic_samples()
        cls.policy = stats.freeze_policy(cls.samples, ["m20", "m60", "m120", "vol", "dd"])
        cls.fitted = stats.fit_evaluate(cls.samples, cls.policy)

    def verify(self, fitted=None, samples=None, policy=None):
        return audit.audit_fit(samples if samples is not None else self.samples,
                               policy if policy is not None else self.policy,
                               fitted if fitted is not None else self.fitted)

    def test_complete_fit_is_verified_without_any_refitting(self):
        with patch.object(stats, "fit_model", side_effect=AssertionError("Must not refit")), \
                patch.object(stats, "fit_evaluate", side_effect=AssertionError("Must not rerun test")):
            result = self.verify()
        self.assertEqual(result["acceptance"], self.fitted["acceptance"])
        self.assertEqual(result["models_checked"], 4)
        self.assertEqual(result["CV_trials_checked"], 108)
        self.assertLessEqual(result["maximum_kkt_residual"], 1e-4)

    def test_mutated_purged_split_is_rejected(self):
        changed = copy.deepcopy(self.fitted)
        split = changed["splits"]["by_horizon"][0]
        split["train_ids"].append(split["test_ids"][0])
        with self.assertRaisesRegex(audit.FitAuditError, "purged splits"):
            self.verify(changed)

    def test_baseline_intervals_and_prediction_identity_are_source_bound(self):
        for key, value in (("baseline_mean", 1.), ("lo80", -1.), ("fund_group_id", "fake"),
                           ("decision_date", "1999-01-01"), ("code", "999999"), ("horizon_days", 90),
                           ("split", "test"), ("actual", .9)):
            changed = copy.deepcopy(self.fitted)
            changed["predictions"][0][key] = value
            with self.subTest(field=key), self.assertRaises(audit.FitAuditError):
                self.verify(changed)

    def test_empty_or_incomplete_target_inventories_fail(self):
        for key in ("models", "cv_trials", "predictions", "acceptance", "ablations"):
            for empty in (True, False):
                changed = copy.deepcopy(self.fitted)
                changed[key] = [] if empty else changed[key][:-1]
                with self.subTest(field=key, empty=empty), self.assertRaises(audit.FitAuditError):
                    self.verify(changed)

    def test_forged_acceptance_is_rejected_instead_of_trusted(self):
        changed = copy.deepcopy(self.fitted)
        changed["acceptance"][0]["intervals"]["80"] = "fabricated_pass"
        with self.assertRaisesRegex(audit.FitAuditError, "recomputed acceptance"):
            self.verify(changed)

    def test_fake_scaler_parameters_and_selected_hyperparameters_fail(self):
        for field in ("scaler_mean", "scaler_scale", "coefficients", "intercept", "baseline_mean", "alpha"):
            changed = copy.deepcopy(self.fitted)
            model = changed["models"][0]
            if isinstance(model[field], list):
                model[field][0] += .25
            else:
                model[field] += .25
            with self.subTest(field=field), self.assertRaises(audit.FitAuditError):
                self.verify(changed)

    def test_calibration_quantiles_and_target_membership_are_checked(self):
        changed = copy.deepcopy(self.fitted)
        changed["models"][0]["calibration"]["residual_quantiles"]["0.05"] -= .1
        with self.assertRaisesRegex(audit.FitAuditError, "calibration"):
            self.verify(changed)
        changed = copy.deepcopy(self.fitted)
        changed["models"][0]["target"] = changed["models"][1]["target"]
        with self.assertRaisesRegex(audit.FitAuditError, "duplicate identity"):
            self.verify(changed)

    def test_changed_final_outcomes_are_rejected_without_retraining(self):
        changed = copy.deepcopy(self.samples)
        for row in changed:
            if row["decision_date"] >= self.policy["test_start"]:
                row["y"] = {target: .7 for target in stats.TARGETS}
        policy = stats.freeze_policy(changed, self.policy["feature_names"])
        with patch.object(stats, "fit_model", side_effect=AssertionError("Must not refit")), \
                patch.object(stats, "fit_evaluate", side_effect=AssertionError("Must not rerun test")), \
                self.assertRaisesRegex(audit.FitAuditError, "prediction"):
            self.verify(samples=changed, policy=policy)


if __name__ == "__main__":
    unittest.main()
