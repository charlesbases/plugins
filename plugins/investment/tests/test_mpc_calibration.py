"""Independent statistical/lineage tests using explicitly synthetic cash-flow panels."""
import copy
import datetime as dt
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import numpy as np
from contracts import fingerprint
import mpc_calibration as calibration


def panel(n=120):
    dates = [(dt.date(2020, 1, 1)+dt.timedelta(days=i)).isoformat() for i in range(n)]
    policies = [{"id": "baseline", "mode": "baseline", "proposed_contribution": "0"},
                {"id": "alternative", "mode": "rolling_policy", "proposed_contribution": "1000"}]
    for row in policies:row["funding_group_id"] = "group-"+row["id"]
    errors = {"baseline": [0.]*n, "alternative": [3+2*math.sin(i*.7)+1.5*math.cos(i*.19) for i in range(n)]}
    losses = {"baseline": [10+7*math.sin(i*.13)+4*math.cos(i*.71) for i in range(n)],
              "alternative": [12+7*math.sin(i*.13)+5*math.cos(i*.39) for i in range(n)]}
    records = {}
    for policy in policies:
        key = policy["id"]
        records[key] = [{"origin_at": date+"T00:00:00Z", "label_available_at": (dt.date.fromisoformat(date)+dt.timedelta(days=1)).isoformat()+"T00:00:00Z",
            "source_hashes": [fingerprint({"synthetic": True, "date": date})],
            "predicted_gain": errors[key][i], "realized_gain": 0., "optimism": errors[key][i], "principal_loss": losses[key][i],
            "predicted_valuation_hash": fingerprint({"synthetic_predicted": key, "date": date}),
            "realized_valuation_hash": fingerprint({"synthetic_realized": key, "date": date})} for i, date in enumerate(dates)]
    origins = [{"date": date, "origin_at": date+"T00:00:00Z", "outcome_available_at": records["baseline"][i]["label_available_at"],
        "source_maturity_hash": fingerprint(records["baseline"][i]["source_hashes"]),
        "simulation_hash": fingerprint({key: records[key][i] for key in sorted(records)})} for i, date in enumerate(dates)]
    inference = {"confidence_level": .95, "minimum_net_advantage": 0., "maximum_ci_half_width": .01,
        "mc_cdf_tolerance": .01, "mc_failure_probability": .001, "max_bootstrap_repetitions": 10000,
        "bootstrap_seed": 42, "block_sensitivity_factors": [.5, 1., 2.], "max_observation_gap_days": 3,
        "minimum_tail_observations": 5}
    context = {"decision_at": "2020-05-03T00:00:00Z", "context_hash": fingerprint({"synthetic_financial_context": True}),
        "trade_family_review_index": 1, "snapshot": {"positions": [{"engineering": True}], "reserved_cash": "0", "unsettled_cash": "0"},
        "spec": {"trade_policy": {"inference": inference, "family": {"max_reviews": 1},
            "sample_window": {"start_date": "2020-01-01", "end_date": dates[-1]}}, "allocation": {"tail_probability": .1},
            "training": {"min_joint_dates": 20}}, "matrix_source_hash": fingerprint(records), "mpc_matrix_records": records,
        "mpc_calibration_lineage": {"origins": origins, "policies": policies, "capital_amount": "10000"}}
    return errors, losses, dates, context, fingerprint(policies)


class MPCCalibrationTests(unittest.TestCase):
    def call(self, values):
        errors, losses, dates, context, family_hash = values
        policies = context["mpc_calibration_lineage"]["policies"]
        if fingerprint(policies) != family_hash:
            raise ValueError("Frozen source test policy family changed")
        registry = [{"group_id":row["funding_group_id"],"proposed_contribution":row["proposed_contribution"],"policy_count_upper":1}
            for row in policies]
        results = []
        for row in policies:
            identity = row["id"]
            single = copy.deepcopy(context)
            records = {identity:single["mpc_matrix_records"][identity]}
            single["mpc_matrix_records"] = records
            single["matrix_source_hash"] = fingerprint(records)
            single["mpc_calibration_lineage"]["policies"] = [row]
            for i,origin in enumerate(single["mpc_calibration_lineage"]["origins"]):
                origin["simulation_hash"] = fingerprint({identity:records[identity][i]})
            results.append(calibration.calibrate({identity:errors[identity]},{identity:losses[identity]},dates,single,
                frozen_family_hash=fingerprint([row]),total_family_count=1,policy_count_bound=1,
                funding_registry=registry,funding_registry_hash=fingerprint(registry),funding_group_id=row["funding_group_id"]))
        return {**results[0],"candidates":{key:value for result in results for key,value in result["candidates"].items()}}

    def test_order_statistic_rank_has_exact_binomial_coverage(self):
        n, probability, delta = 200, .95, .01
        row = calibration.exact_quantile_ranks(n, probability, delta, .1)
        self.assertTrue(row["supported"])
        upper = sum(math.comb(n, i)*probability**i*(1-probability)**(n-i) for i in range(row["upper_rank"], n+1))
        lower = sum(math.comb(n, i)*probability**i*(1-probability)**(n-i) for i in range(row["lower_rank"]))
        self.assertLessEqual(upper+lower, delta)
        impossible = calibration.exact_quantile_ranks(100, .9999, .001, .01)
        self.assertFalse(impossible["supported"])

    def test_fractional_tail_cash_amount_is_an_independent_truth(self):
        x = np.asarray([[0.], [1.], [2.], [3.]])
        self.assertEqual(float(calibration._tail(x, .5)[0]), 2.5)
        self.assertAlmostEqual(float(calibration._tail(x, .375)[0]), (3+.5*2)/1.5)

    def test_entire_origin_rows_are_jointly_resampled(self):
        x = np.arange(30., dtype=float)
        error = np.column_stack((x, x))
        loss = np.column_stack((-x, -x))
        sampled = calibration._bootstrap_statistics(error, loss, 3., 200, 7, .1)
        self.assertTrue(np.array_equal(sampled[:, 0], sampled[:, 1]))
        self.assertTrue(np.array_equal(sampled[:, 2], sampled[:, 3]))

    def test_joint_intervals_are_reproducible_and_capital_specific(self):
        values = panel()
        first, second = self.call(values), self.call(values)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "calibrated")
        self.assertEqual(first["candidates"]["baseline"]["capital_amount"], 10000.)
        self.assertEqual(first["candidates"]["alternative"]["capital_amount"], 11000.)
        self.assertGreater(first["candidates"]["alternative"]["optimism_buffer_amount"], 3.)
        self.assertIn("not_future_conditional", first["assumption_scope"])

    def test_raw_amount_or_family_or_maturity_tampering_is_rejected(self):
        for variant in ("amount", "family", "maturity"):
            values = copy.deepcopy(panel())
            if variant == "amount": values[0]["alternative"][0] += 1
            if variant == "family": values[3]["mpc_calibration_lineage"]["policies"][0]["proposed_contribution"] = "5"
            if variant == "maturity": values[3]["mpc_calibration_lineage"]["origins"][0]["outcome_available_at"] = "2021-01-01T00:00:00Z"
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                self.call(values)

    def test_small_Monte_Carlo_budget_preserves_policy_and_declines_calibration(self):
        values = panel()
        values[3]["spec"]["trade_policy"]["inference"]["max_bootstrap_repetitions"] = 5
        before = copy.deepcopy(values)
        result = self.call(values)
        self.assertEqual(result["status"], "needs_calibration")
        self.assertEqual(result["confidence"], .95)
        self.assertEqual(values, before)
        self.assertIn("precision_exceeds_budget", result["reason"])

    def test_declared_MC_upper_cap_preserves_original_joint_confidence(self):
        values = panel()
        inference = values[3]["spec"]["trade_policy"]["inference"]
        inference["confidence_level"] = .975
        inference["mc_failure_probability"] = .05
        before = copy.deepcopy(values)
        result = self.call(values)
        budget = result["error_budget"]
        self.assertEqual(result["confidence"], .975)
        self.assertEqual(budget["declared_MC_failure_upper"], .05)
        self.assertLessEqual(budget["allocated_MC_failure"], .05)
        self.assertAlmostEqual(budget["allocated_MC_failure"], .0125)
        self.assertAlmostEqual(budget["allocated_statistical_alpha"]+budget["allocated_MC_failure"], 1-.975)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
