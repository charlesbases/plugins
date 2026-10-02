"""Hand-calculated terminal cost regressions; synthetic contracts, not market evidence."""
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import portfolio_mpc as mpc
import verify
from contracts import EvidenceError, fingerprint
from test_single_step import make_context
from test_portfolio_mpc import EMPTY, HOLD, STAGES, path


class TerminalCostTests(unittest.TestCase):
    def simulate(self, *, goal="redeem", shares="100", nav=1, rate="0.1", net_mode="half_up"):
        context = make_context(cash="0", shares=shares, goal=goal, exit_rate=rate)
        context["fee_contracts"]["C"]["trade_precision"].update(
            fee_rounding="unrounded", redemption_net_rounding=net_mode)
        scenario = path(["C"], lambda *_: nav)
        value = mpc.simulate(context, scenario, EMPTY, HOLD, STAGES)
        policy = {"proposed_contribution": 0, "current_action": EMPTY, "future_rule": HOLD}
        return context, scenario, value, policy

    def test_terminal_fee_is_in_total_and_wealth_once(self):
        context, scenario, value, policy = self.simulate()
        self.assertEqual(value["terminal_wealth"], 90)
        self.assertEqual(value["path_execution_cost"], 0)
        self.assertEqual(value["terminal_exit_assumption_cost"], 10)
        self.assertEqual(value["fees"], 10)
        self.assertEqual(value["cash_flow_log"], [])
        oracle = verify.terminal_cost_invariants(context, scenario, value, STAGES[-1])
        self.assertEqual(oracle["terminal_asset_wealth"], Decimal("90"))
        self.assertEqual(oracle["terminal_wealth_drag"], Decimal("10"))
        verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])

    def test_hold_has_no_terminal_exit_cost(self):
        context, scenario, value, policy = self.simulate(goal="hold")
        self.assertEqual(value["terminal_wealth"], 100)
        self.assertEqual(value["fees"], 0)
        self.assertEqual(value["terminal_exit_assumption_cost"], 0)
        self.assertEqual(value["terminal_exit_assumptions"], [])
        verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])

    def test_gross_rounding_adjustment_can_be_negative(self):
        context, scenario, value, policy = self.simulate(shares="1", nav=1.236, rate="0")
        self.assertEqual(value["terminal_wealth"], 1.24)
        self.assertEqual(value["fees"], 0)
        self.assertEqual(value["terminal_gross_rounding_adjustment"], -0.004)
        self.assertEqual(value["terminal_wealth_drag"], -0.004)
        row = value["terminal_exit_assumptions"][0]
        self.assertEqual(Decimal(row["raw_gross"]), Decimal("1.236"))
        self.assertEqual(Decimal(row["quoted_gross"]), Decimal("1.24"))
        verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])

    def test_net_rounding_loss_and_contractual_fee_conserve_separately(self):
        context, scenario, value, policy = self.simulate(shares="8", nav=1.2345, rate="0.1", net_mode="down_fund")
        # Raw gross 9.876, contract fee .9876, raw net 8.8884, floor net 8.88.
        self.assertEqual(value["terminal_wealth"], 8.88)
        self.assertEqual(value["fees"], 0.996)
        self.assertEqual(value["terminal_gross_rounding_adjustment"], 0)
        self.assertEqual(value["terminal_wealth_drag"], 0.996)
        row = value["terminal_exit_assumptions"][0]
        self.assertEqual(Decimal(row["contractual_fee"]), Decimal("0.9876"))
        self.assertEqual(Decimal(row["rounding_loss"]), Decimal("0.0084"))
        self.assertEqual(Decimal(row["raw_gross"])-Decimal(row["net"]),
                         Decimal(row["gross_rounding_adjustment"])+Decimal(row["cost"]))
        self.assertFalse(row["is_order"])
        self.assertFalse(row["is_ledger_event"])
        verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])

    def test_resealed_missing_or_corrupt_terminal_costs_are_rejected(self):
        context, scenario, original, policy = self.simulate()
        for field in ("path_execution_cost", "terminal_exit_assumption_cost",
                      "terminal_gross_rounding_adjustment", "terminal_wealth_drag", "terminal_exit_assumptions"):
            with self.subTest(field=field):
                value = copy.deepcopy(original)
                del value[field]
                value["valuation_hash"] = fingerprint({key: item for key, item in value.items() if key != "valuation_hash"})
                with self.assertRaisesRegex(EvidenceError, "cost|assumption"):
                    verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])
        value = copy.deepcopy(original)
        value["fees"] = 0
        value["valuation_hash"] = fingerprint({key: item for key, item in value.items() if key != "valuation_hash"})
        with self.assertRaisesRegex(EvidenceError, "cost|fees"):
            verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])
        for field, changed in (("contractual_fee", "0"), ("gross_rounding_adjustment", "1"),
                               ("submitted_at", "2030-01-08T20:00:00+08:00"), ("is_ledger_event", True)):
            with self.subTest(witness=field):
                value = copy.deepcopy(original)
                value["terminal_exit_assumptions"][0][field] = changed
                value["valuation_hash"] = fingerprint({key: item for key, item in value.items() if key != "valuation_hash"})
                with self.assertRaisesRegex(EvidenceError, "assumption"):
                    verify.cash_journal_invariants(context, policy, scenario, value, end_day=STAGES[-1])

    def test_report_separates_point_expected_and_worst_fee_reserve(self):
        import report
        import report_validation
        from artifacts import Artifacts
        from test_report_semantics import bundle_case, envelope_case
        context, scenario, point, _ = self.simulate()
        policy = {"id": "synthetic-fee-policy", "expected_net_return": -0.1,
            "cvar_loss_fraction": 0.1, "eligible": False, "expected_profit": -10,
            "current_projection": {}, "future_rule": HOLD, "point": point,
            "selection": [{"fees": 10}, {"fees": 12}], "expected_policy_fees": 11}
        summary = report.summarize({"mpc": {"selected_policy": policy}}, context)
        self.assertEqual(summary["selected"]["fees"], 10)
        self.assertEqual(summary["selected"]["expected_policy_fees"], 11)
        self.assertEqual(summary["selected"]["worst_plan_fee_reserve"], 12)
        source_context, orders, envelope = envelope_case()
        with tempfile.TemporaryDirectory(prefix="terminal-report-", dir=Path(__file__).resolve().parents[1]) as directory:
            bundle = bundle_case(Artifacts(Path(directory)), source_context, orders, envelope)
            bundle["comparison_summary"] = summary
            text = report.render(bundle)
            report_validation.report_semantics(bundle, text)
            for fragment in ("终点退出假设成本 10.00", "情景期望总模型费用 11.00",
                             "最坏情景完整计划费用预留 12.00", "不是订单或台账费用"):
                with self.subTest(fragment=fragment):
                    with self.assertRaisesRegex(ValueError, "cost|scope"):
                        report_validation.report_semantics(bundle, text.replace(fragment, ""))


if __name__ == "__main__":
    unittest.main()
