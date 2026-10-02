"""Synthetic arithmetic/fault cases; these make no investment efficacy claim."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import allocation
import allocation_runner as runner
import execution
import numeric_validation as validation
import single_step_wealth
from contracts import EvidenceError, fingerprint
from test_allocation_runner import data, build_context
from test_strategy_execution import spec, market
from test_ledger import opening, T1


class NumericValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = spec()
        selected["planning"]["primary_horizon_days"] = 3
        selected["training"].update(min_train_dates=4, cv_folds=1, min_joint_dates=2,
                                    cv_initial_train_fraction=.7, alpha_grid=[.001], l1_ratio_grid=[.5])
        state, _ = opening()
        cls.context = build_context(selected, state, T1, market())
        cls.data = data()
        for key in ("nav", "features"):
            for code in cls.data[key]:
                cls.data[key][code] = cls.data[key][code][-45:]
        cls.clock = single_step_wealth.build_clock_context(cls.context)
        request = cls.context["model_request"]
        cls.samples = runner.prepare_samples(cls.data["nav"], cls.data["features"], cls.data["code_info"],
                                             3, request["nav_availability_calendar_lag"], cls.context["as_of"], cls.clock)
        cls.fitting = runner.fit_at(cls.samples, cls.context["allocation_codes"], cls.context, cls.clock)
        cls.rebased = runner._rebase_targets(copy.deepcopy(cls.fitting), cls.context, cls.data)
        # Fund source/model and fee-quote primitives remain independently
        # testable. Current order arithmetic uses the real MPC simulator and
        # source whole-share projection, without invented OOS calibration.
        from test_verification_arithmetic import hand_case
        cls.order_context, cls.calculation = hand_case()
        cls.calculation["paths"]["source_hash"] = fingerprint({})
        cls.calculation["paths"]["path_hash"] = fingerprint({key: value for key, value in cls.calculation["paths"].items() if key != "path_hash"})
        cls.calculation["mpc"]["status"] = "research_ready"
        cls.orders = execution.compile_orders(cls.calculation, cls.order_context, {})

    def sample_inputs(self):
        return {"context": self.context, "data": self.data, "clock": self.clock}

    def fit_inputs(self):
        return {"context": self.context, "data": self.data, "samples": self.samples}

    def test_source_NAV_tamper_rejected_after_self_hash_repair(self):
        self.assertEqual(validation.validate_samples(self.samples, self.sample_inputs())["status"], "passed")
        samples = copy.deepcopy(self.samples)
        source = next(row["label_source"] for row in samples if row["label_source"] is not None)
        source["base_nav"] += 1
        source["source_hash"] = fingerprint({key: value for key, value in source.items() if key != "source_hash"})
        with self.assertRaisesRegex(EvidenceError, "source base_nav"):
            validation.validate_samples(samples, self.sample_inputs())

    def test_latent_cash_tamper_is_independently_rejected(self):
        samples = copy.deepcopy(self.samples)
        row = next(row for row in samples if row["targets"] is not None)
        row["latent_targets"]["sqrt_cash_both"] = .5
        with self.assertRaisesRegex(EvidenceError, "latent sqrt_cash_both"):
            validation.validate_samples(samples, self.sample_inputs())

    def test_point_forecast_must_equal_standardized_model_dot_product(self):
        self.assertEqual(validation.validate_fit(self.fitting, self.fit_inputs())["status"], "passed")
        values = copy.deepcopy(self.data)
        for features in values["features"].values():
            for feature in features:
                feature["values"]["momentum_20_nav_observations"] = .1
        samples = runner.prepare_samples(values["nav"], values["features"], values["code_info"],
            3, self.context["model_request"]["nav_availability_calendar_lag"], self.context["as_of"], self.clock)
        constant = runner.fit_at(samples, self.context["allocation_codes"], self.context, self.clock)
        self.assertEqual(constant["status"], "research_ready", constant.get("reason"))
        self.assertEqual(validation.validate_fit(constant, {"context": self.context, "data": values, "samples": samples})["status"], "passed")
        fitting = copy.deepcopy(self.fitting)
        fitting["forecasts"][0]["predicted_latents"]["log_pricing"] += .1
        with self.assertRaisesRegex(EvidenceError, "standardized point dot product"):
            validation.validate_fit(fitting, self.fit_inputs())

    def test_CV_error_is_recomputed_from_fold_model_and_validation_sources(self):
        fitting = copy.deepcopy(self.fitting)
        trial = fitting["training_audit"]["cv_trials"][0]
        trial["fold_mse"][0] += .1
        trial["mean_mse"] = trial["fold_mse"][0]
        with self.assertRaisesRegex(EvidenceError, "independent weighted CV MSE"):
            validation.validate_fit(fitting, self.fit_inputs())

    def test_rebase_wrong_current_target_is_rejected(self):
        fitting = copy.deepcopy(self.rebased)
        fitting["joint_scenarios"]["current_point_targets"]["hold"][0][0] += .01
        with self.assertRaisesRegex(EvidenceError, "current mark rebase hold"):
            validation.validate_rebase(fitting, {"context": self.context, "data": self.data, "fitting": self.fitting})
        for field in ("predicted_log_targets", "expected_gross_return"):
            with self.subTest(rebased_forecast_field=field):
                fitting = copy.deepcopy(self.rebased)
                if field == "predicted_log_targets":
                    fitting["forecasts"][0][field]["hold"] += 100
                else:
                    fitting["forecasts"][0][field] += 100
                with self.assertRaisesRegex(EvidenceError, "rebased (point log target|expected gross return)"):
                    validation.validate_rebase(fitting, {"context": self.context, "data": self.data, "fitting": self.fitting})

    def test_rounded_order_wealth_tamper_is_rejected(self):
        self.assertEqual(validation.validate_compile(self.orders, {"context": self.order_context, "data": {},
                                                                   "calculation": self.calculation})["status"], "passed")
        value = copy.deepcopy(self.calculation)
        path = value["mpc"]["funding_options"][0]["candidates"][0]["selection"][0]
        path["terminal_wealth"] += 1
        path["valuation_hash"] = fingerprint({key: item for key, item in path.items() if key != "valuation_hash"})
        with self.assertRaisesRegex(EvidenceError, "terminal NAV/cash/receivable wealth"):
            validation.validate_compile(self.orders, {"context": self.order_context, "data": {}, "calculation": value})
        value = copy.deepcopy(self.calculation)
        value["mpc"]["funding_options"][0]["candidates"][0]["current_projection"]["values"]["D"] = "99.99"
        with self.assertRaisesRegex(EvidenceError, "current whole-share source NAV value"):
            validation.validate_compile(self.orders, {"context": self.order_context, "data": {}, "calculation": value})

    def test_failed_validation_runs_actual_producer_three_times(self):
        produced = []
        def producer():
            produced.append(len(produced)+1)
            return {"amount": 1}
        def validator(output, inputs):
            validation.close(output["amount"], 2, "synthetic independent expected amount")
        with self.assertRaisesRegex(runner.NumericStageFailure, "synthetic independent"):
            runner._local_stage("synthetic", {}, producer, validator)
        self.assertEqual(produced, [1, 2, 3])
        for error in (ZeroDivisionError, AssertionError):
            with self.subTest(producer_error=error.__name__):
                produced.clear()
                def broken_producer():
                    produced.append(len(produced)+1)
                    raise error("synthetic producer failure")
                with self.assertRaises(runner.NumericStageFailure):
                    runner._local_stage("synthetic", {}, broken_producer, validator)
                self.assertEqual(produced, [1, 2, 3])
        proof = validation.result(["synthetic_identity"])
        self.assertEqual(proof["scope"], "source_bound_numerical_arithmetic_only")
        self.assertIn("no_statistical_coverage_claim", proof["limitations"])

    def test_terminal_stage_failure_propagates_to_the_supplied_runner(self):
        calls = []
        def terminal(name, inputs, producer, validator):
            calls.append(name)
            raise runner.NumericStageFailure("synthetic exhausted stage")
        with self.assertRaisesRegex(runner.NumericStageFailure, "synthetic exhausted"):
            runner.predict(self.context, self.data, stage_runner=terminal)
        self.assertEqual(calls, ["numeric_clock"])
        def lease_lost(name, inputs, producer, validator):
            raise runner.LeaseLost("synthetic lost ownership")
        with self.assertRaises(runner.LeaseLost):
            runner.predict(self.context, self.data, stage_runner=lease_lost)
        partial = validation.result(["synthetic_maturity"], "insufficient_evidence", reason="synthetic_missing_labels")
        self.assertEqual(partial["status"], "partial")
        self.assertFalse(partial["readiness"]["trade_ready"])
        self.assertEqual(partial["required_actions"][0]["reason"], "synthetic_missing_labels")



class SourceActionValidationTests(unittest.TestCase):
    def setUp(self):
        from test_allocation import source_context
        self.context = source_context()
        self.comparison = allocation.build_source_actions(self.context)

    def test_unfunded_purchase_leg_is_rejected(self):
        comparison = copy.deepcopy(self.comparison)
        buy = next(row for group in comparison["funding_options"] for action in group["actions"] for row in action["buys"])
        buy["cash_debit"] = str(float(self.context["snapshot"]["available_cash"])+1)
        comparison["action_family_hash"] = fingerprint(comparison["funding_options"])
        with self.assertRaisesRegex(EvidenceError, "source action family"):
            validation.validate_comparison(comparison, {"context":self.context})

    def test_exact_candidate_order_is_recomputed(self):
        comparison = copy.deepcopy(self.comparison)
        comparison["funding_options"][0]["actions"].reverse()
        comparison["action_family_hash"] = fingerprint(comparison["funding_options"])
        with self.assertRaisesRegex(EvidenceError, "source action family"):
            validation.validate_comparison(comparison, {"context":self.context})


if __name__ == "__main__":
    unittest.main()
