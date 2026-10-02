"""Independent decimal truths and real calibration; no investment evidence."""
import copy
import datetime as dt
import math
import unittest
from decimal import Decimal
from fractions import Fraction

import allocation
import portfolio_mpc as mpc
import risk_numbers
import verify
from contracts import fingerprint
from test_allocation import source_context
from test_mpc_contracts import FrozenPolicyContractTests
from test_portfolio_mpc import path


def decimal_boundary():
    context, paths, _ = FrozenPolicyContractTests().deadline_case()
    source = source_context(cash="0", shares="100")
    context.update(snapshot=source["snapshot"], fee_contracts=source["fee_contracts"], risk_state=source["risk_state"])
    context["fee_contracts"]["C"]["redemption"]["rate"] = "0"
    context["fee_contracts"]["C"]["trade_precision"]["share_step"] = "100"
    context["risk_state"].update(net_principal="100", loss_tolerance=".222", principal_floor="77.8", remaining_loss_budget="22.2", valid_until=None)
    context["account_hash"] = fingerprint({"engineering_account": source["snapshot"]})
    context["lot_terms"] = {row["lot_id"]: {"redemption_fee": "0", "redemption_schedule": None} for row in source["snapshot"]["positions"]}
    for asset in context["model_request"]["assets"]:
        asset["settlement_days"] = context["fee_contracts"][asset["code"]]["settlement"]["lag_days"]
    context["spec"]["planning"].update(cash_deadline_days=None, cash_required_amount=None)
    context["spec"]["decision"].update(max_current_actions=16, max_policy_count=64)
    context["spec"]["allocation"]["tail_probability"] = .05
    def quote(nav, identity, probability):
        return {**path(["C"], lambda _, day: nav if day == "2030-01-09" else 1.),
            "id": identity, "probability": probability, "price_schema": paths["schema"], "known_marks": paths["known_marks"]}
    paths["point_paths"] = [quote(1.1, "point", 1.)]
    paths["selection_paths"] = [quote(nav, "state-"+str(i), 1/3) for i, nav in enumerate((.994, .778, 1.166))]
    paths["prefix_groups"] = {day: [["state-0", "state-1", "state-2"]] for day in paths["stage_dates"]}
    paths["calibration_paths"] = []
    for i in range(480):
        day = dt.date(2025, 1, 1)+dt.timedelta(days=i)
        paths["calibration_paths"].append({"origin_at": day.isoformat()+"T20:00:00+08:00",
            "label_available_at": (day+dt.timedelta(days=8)).isoformat()+"T20:00:00+08:00",
            "source_hashes": [fingerprint({"engineering_paired_path": i})],
            "realized": quote(.99+.0001*math.sin(i*.17), "realized-"+str(i), 1.),
            "predicted": quote(.99+.0001*math.sin(i*.17)+.00005*math.cos(i*.71), "predicted-"+str(i), 1.)})
    paths["path_hash"] = fingerprint({k: v for k, v in paths.items() if k != "path_hash"})
    return context, paths


class ExactRiskArithmeticTests(unittest.TestCase):
    def test_finite_decimal_loss_preserves_money_boundary(self):
        losses = [risk_numbers.number("100")-risk_numbers.number(value) for value in (99.4, 77.8, 116.6)]
        self.assertEqual(losses[1], Fraction(111, 5))
        self.assertEqual(mpc.tail_mean(losses, [1/3]*3, .05, exact=True), Fraction(111, 5))

    def test_common_information_and_negative_losses_keep_one_measure(self):
        for count in (3, 11, 49):
            paths = [{"id": str(i), "probability": 1/count} for i in range(count)]
            losses = [Fraction(i-20, 10) for i in range(count)]
            expected = mpc.tail_mean(losses, [1/count]*count, .05, exact=True)
            for depth in (2, 4, 8):
                stages = [str(i) for i in range(depth)]
                groups = {day: [[row["id"] for row in paths]] for day in stages}
                actual, _ = mpc.nested_tail(losses, paths, groups, stages, .05, exact=True)
                self.assertEqual(actual, expected)

    def test_original_invalid_probability_and_nonfinite_inputs_are_rejected(self):
        for probabilities in ([0., 0.], [-.1, 1.1], [.4, .4], [math.nan, 1.]):
            with self.assertRaises(ValueError):
                mpc.tail_mean([1, 2], probabilities, .05, exact=True)
        for loss in (math.inf, math.nan):
            with self.assertRaises(ValueError):
                mpc.tail_mean([loss, 0], [.5, .5], .05, exact=True)


class ExactCalibratedBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        context, cls.paths = decimal_boundary()
        cls.cases = {}
        for name, rate in (("equal", ".222"), ("below", ".2219999999999999999999"), ("above", ".2220000000000000000001")):
            current = copy.deepcopy(context)
            floor = Decimal("100")*(1-Decimal(rate))
            current["risk_state"].update(loss_tolerance=rate, principal_floor=str(floor), remaining_loss_budget=str(Decimal("100")-floor))
            current["context_hash"] = fingerprint({k: v for k, v in current.items() if k != "context_hash"})
            comparison = allocation.build_source_actions(current)
            result = mpc.optimise(current, cls.paths, comparison)
            baseline = next(row for row in result["funding_options"][0]["candidates"] if row["mode"] == "baseline")
            cls.cases[name] = current, comparison, result, baseline

    def test_real_calibration_decimal_boundary_passes_both_validators(self):
        context, comparison, result, baseline = self.cases["equal"]
        self.assertEqual([row["terminal_wealth"] for row in baseline["selection"]], [99.4, 77.8, 116.6])
        self.assertEqual(result["calibration"]["candidates"][baseline["id"]]["status"], "calibrated")
        self.assertTrue(baseline["eligible"])
        self.assertEqual(baseline["absolute_cvar"], 22.2)
        self.assertEqual(verify.numerical_invariants(context, {"mpc": result, "paths": self.paths, "orders": {"orders": []}})["status"], "passed")
        self.assertEqual(mpc.validate_result(result, {"context": context, "paths": self.paths, "comparison": comparison})["status"], "passed")

    def test_budget_below_boundary_is_ineligible_even_when_display_is_equal(self):
        context, _, result, baseline = self.cases["below"]
        self.assertEqual(baseline["trade_guard"]["risk_budget"], 22.2)
        self.assertFalse(baseline["eligible"])
        self.assertGreater(baseline["risk_violation_amount"], 0)
        self.assertEqual(verify.numerical_invariants(context, {"mpc": result, "paths": self.paths, "orders": {"orders": []}})["status"], "passed")

    def test_budget_above_boundary_is_eligible_even_when_display_is_equal(self):
        context, _, result, baseline = self.cases["above"]
        self.assertEqual(baseline["trade_guard"]["risk_budget"], 22.2)
        self.assertTrue(baseline["eligible"])
        self.assertEqual(verify.numerical_invariants(context, {"mpc": result, "paths": self.paths, "orders": {"orders": []}})["status"], "passed")
