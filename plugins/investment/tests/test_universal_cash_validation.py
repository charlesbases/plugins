"""Independent hand-calculated contracts and cash fault injections."""
import copy
import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

import cash_reference
import allocation
from contracts import EvidenceError, fingerprint
import portfolio_mpc as mpc
import verify
from test_portfolio_mpc import EMPTY, HOLD, path
from test_single_step import make_context, make_terms


class UniversalCashValidationTests(unittest.TestCase):
    def sale(self, end="2030-01-09"):
        context = make_context(cash="0", shares="100", exit_rate="0.01")
        scenario = path(["C"])
        value = mpc.simulate(context, scenario, {"buys": [], "sells": [{"lot_id": "old", "shares": "100"}]},
                             HOLD, ["2030-01-01", end])
        return context, scenario, value

    def validate(self, context, scenario, value, end=None):
        return verify.cash_deadline_invariants(context, {"proposed_contribution": 0}, scenario, value, primary_end=end)

    def test_no_deadline_rejects_conserved_fee_net_and_receipt_clock_mutants(self):
        context, scenario, value = self.sale()
        self.assertNotIn("cash_deadline", value)
        self.assertEqual(Decimal(value["settled_cash"]), Decimal("99"))
        self.assertTrue(self.validate(context, scenario, value))
        forged = copy.deepcopy(value)
        sale = next(row for row in forged["cash_flow_log"] if row["kind"] == "price_sell")
        sale.update(fee="0", receivable="100")
        with self.assertRaisesRegex(EvidenceError, "sale fee/net"):
            self.validate(context, scenario, forged)
        forged = copy.deepcopy(value)
        next(row for row in forged["cash_flow_log"] if row["kind"] == "price_sell")["pay_date"] = "2030-01-04"
        with self.assertRaisesRegex(EvidenceError, "receipt date"):
            self.validate(context, scenario, forged)
        # A faulty production quote is detected even when all generated amounts
        # agree with one another and every cash conservation identity holds.
        with patch("fee_contract.quote_exit_details", return_value={"gross":Decimal("100"),"contractual_fee":Decimal("0"),"rounding_loss":Decimal("0"),"net":Decimal("100"),"effective_cost":Decimal("0")}):
            context, scenario, mutant = self.sale()
        mpc.verify_cash_flow(mutant)
        with self.assertRaisesRegex(EvidenceError, "sale fee/net"):
            self.validate(context, scenario, mutant)

    def test_receipt_before_on_after_and_missing_duplicate_credit(self):
        for end, expected in (("2030-01-04", "0"), ("2030-01-05", "99"), ("2030-01-06", "99")):
            with self.subTest(end=end):
                context, scenario, value = self.sale(end)
                self.assertEqual(Decimal(value["settled_cash"]), Decimal(expected))
                self.assertTrue(self.validate(context, scenario, value, end))
        context, scenario, value = self.sale()
        forged = copy.deepcopy(value)
        forged["cash_flow_log"] = [row for row in forged["cash_flow_log"] if row["kind"] != "credit_sale"]
        with self.assertRaisesRegex(EvidenceError, "omits a mature receipt"):
            self.validate(context, scenario, forged)
        forged = copy.deepcopy(value)
        forged["cash_flow_log"].append(copy.deepcopy(forged["cash_flow_log"][-1]))
        with self.assertRaisesRegex(EvidenceError, "twice"):
            self.validate(context, scenario, forged)
        forged = copy.deepcopy(value)
        forged["cash_flow_log"][-1]["available_before"] = "1"
        forged["cash_flow_log"][-1]["available_after"] = "100"
        with self.assertRaisesRegex(EvidenceError, "diverges from source balance"):
            self.validate(context, scenario, forged)

    def test_reference_holding_tier_boundaries_and_horizon_golden_values(self):
        terms = make_terms()
        terms["redemption"] = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7", "fee": {"kind": "percentage", "rate": "0.005"}},
            {"minimum": "30", "fee": {"kind": "percentage", "rate": "0"}}]}
        started = "2030-01-01T00:00:00+08:00"
        # 1000 NAV consideration, half-up cents. Inclusive start/exclusive end.
        for age, expected in ((6,"15.00"),(7,"5.00"),(8,"5.00"),(29,"5.00"),(30,"0.00"),(31,"0.00"),(60,"0.00"),(90,"0.00")):
            with self.subTest(age=age):
                day = str(dt.date(2030,1,1)+dt.timedelta(days=age))
                self.assertEqual(cash_reference.exit_fee("1000", terms, acquired_at=started,
                    submitted_at=day+"T08:00:00+08:00"), Decimal(expected))

    def test_independent_purchase_rounding_refund_and_confirmation(self):
        context = make_context(cash="2")
        context["fee_contracts"]["C"]["trade_precision"]["share_step"] = "1"
        scenario = path(["C"], lambda _, day: 1.9)
        action = {"buys": [{"code": "C", "cash_debit": "2"}], "sells": []}
        for end, cash, receipt in (("2030-01-02","0",".1"),("2030-01-03",".1","0")):
            value = mpc.simulate(context, scenario, action, HOLD, ["2030-01-01",end])
            self.assertEqual(Decimal(value["settled_cash"]), Decimal(cash))
            self.assertEqual(Decimal(value["receivables"]), Decimal(receipt))
            self.assertTrue(self.validate(context, scenario, value, end))
        terms = make_terms()
        terms["subscription"] = {"kind":"percentage","rate":".01"}
        self.assertEqual(cash_reference.entry_quote("100", terms, "1"),
            dict(shares=Decimal("99"),gross=Decimal("99"),fee=Decimal(".99"),debit=Decimal("99.99"),refund=Decimal(".01")))
        terms["trade_precision"]["share_rounding"] = "half_up_fund"
        quote = cash_reference.entry_quote("100", terms, "3")
        self.assertEqual((quote["fee"],quote["shares"],quote["refund"]), (Decimal(".99"),Decimal("33"),Decimal("0")))

    def test_candidate_baseline_and_detached_selected_policy_are_validated(self):
        from test_mpc_contracts import FrozenPolicyContractTests
        context, paths, comparison = FrozenPolicyContractTests().deadline_case()
        context["spec"]["planning"].update(cash_deadline_days=None, cash_required_amount=None)
        context["context_hash"] = fingerprint({key:value for key,value in context.items() if key != "context_hash"})
        comparison = allocation.build_source_actions(context)
        actual = mpc.optimise(context, paths, comparison)
        calculation = {"mpc":actual,"paths":paths,"orders":{"orders":[]}}
        self.assertEqual(verify.numerical_invariants(context, calculation)["status"], "passed")
        forged = copy.deepcopy(calculation)
        baseline = next(row for row in forged["mpc"]["funding_options"][0]["candidates"] if row["mode"] == "baseline")
        baseline["selection"][0]["settled_cash"] = "101"
        baseline["selection"][0]["terminal_wealth"] = 101.
        baseline["selection"][0]["valuation_hash"] = fingerprint({key:value for key,value in baseline["selection"][0].items() if key != "valuation_hash"})
        with self.assertRaisesRegex(EvidenceError, "snapshot includes"):
            verify.numerical_invariants(context, forged)
        forged = copy.deepcopy(calculation)
        forged["mpc"]["selected_policy"] = copy.deepcopy(actual["selected_policy"])
        forged["mpc"]["selected_policy"]["point"]["settled_cash"] = "101"
        with self.assertRaisesRegex(EvidenceError, "Selected policy differs"):
            verify.numerical_invariants(context, forged)

    def test_existing_source_receipts_and_precise_future_cash_clock_mutants(self):
        from test_portfolio_mpc import with_receivable
        context = with_receivable(make_context(cash="0"), "2030-01-02T14:00:00+08:00")
        context["decision_at"] = "2030-01-01T08:00:00+08:00"
        scenario = path(["C"])
        rule = {"kind":"allocate_settled_cash","weights":{"C":"1"}}
        value = mpc.simulate(context, scenario, EMPTY, rule, ["2030-01-01","2030-01-02","2030-01-09"])
        self.assertEqual(value["terminal_wealth"], 10.)
        self.assertTrue(self.validate(context, scenario, value))
        forged = copy.deepcopy(value)
        reservation = next(row for row in forged["cash_flow_log"] if row["kind"] == "reserve_buy")
        reservation["submitted_at"] = "2030-01-02T08:00:00+08:00"
        with self.assertRaisesRegex(EvidenceError, "before credited instant"):
            self.validate(context, scenario, forged)
        forged = copy.deepcopy(value)
        forged["cash_flow_log"][0]["credited_at"] = "2030-01-02T13:59:59+08:00"
        with self.assertRaisesRegex(EvidenceError, "before due instant"):
            self.validate(context, scenario, forged)
        forged = copy.deepcopy(value)
        forged["cash_flow_log"].insert(1, copy.deepcopy(forged["cash_flow_log"][0]))
        with self.assertRaisesRegex(EvidenceError, "twice"):
            self.validate(context, scenario, forged)
        pending = with_receivable(make_context(cash="0"), "2030-01-08", amount="10")
        held = mpc.simulate(pending, scenario, EMPTY, HOLD, ["2030-01-01","2030-01-04"])
        self.assertTrue(self.validate(pending, scenario, held))
        held["terminal_existing_receivables"][0]["amount"] = "9"
        with self.assertRaisesRegex(EvidenceError, "identity/amount"):
            self.validate(pending, scenario, held)

    def test_independent_terminal_redemption_cost_and_UTC_decision_clock(self):
        from test_mpc_contracts import FrozenPolicyContractTests
        context, paths, comparison = FrozenPolicyContractTests().deadline_case()
        context["spec"]["planning"].update(cash_deadline_days=None, cash_required_amount=None, primary_goal="redeem")
        context["snapshot"].update(cash="0", available_cash="0", equity="100", positions=make_context(shares="100")["snapshot"]["positions"])
        context["fee_contracts"]["C"]["redemption"] = {"kind":"percentage","rate":".01"}
        context["spec"]["decision"].update(max_current_actions=16,max_policy_count=32)
        context["context_hash"] = fingerprint({key:value for key,value in context.items() if key != "context_hash"})
        comparison = allocation.build_source_actions(context)
        result = mpc.optimise(context, paths, comparison)
        baseline = next(row for row in result["funding_options"][0]["candidates"] if row["mode"] == "baseline")
        self.assertEqual(baseline["point"]["terminal_wealth"], 99.)
        self.assertEqual(baseline["point"]["fees"], 0.)
        self.assertEqual(verify.numerical_invariants(context, {"mpc":result,"paths":paths,"orders":{"orders":[]}})["status"], "passed")
        utc = make_context(cash="2")
        utc["decision_at"] = "2030-01-01T12:00:00+00:00"
        scenario = path(["C"])
        value = mpc.simulate(utc, scenario, {"buys":[{"code":"C","cash_debit":"2"}],"sells":[]}, HOLD, ["2030-01-01","2030-01-09"])
        self.assertTrue(self.validate(utc, scenario, value))

    def test_existing_dividend_requires_identity_binding_for_already_held_right(self):
        from test_portfolio_mpc import STAGES, with_receivable
        context = with_receivable(make_context(cash="0", shares="10"), "2030-01-05", amount="1",
                                  kind="dividend", identity="confirmed-right")
        event = {"code":"C","id":"confirmed-right","record_date":"2030-01-01","ex_date":"2030-01-02",
                 "pay_date":"2030-01-05","per_share":.1,"distribution_mode":"cash","currency":"CNY",
                 "entitlement_rule":{"subscribe_on_record_date":"excluded","redeem_on_record_date":"included"}}
        scenario = path(["C"], lambda *_: .9, [event])
        value = mpc.simulate(context, scenario, EMPTY, HOLD, STAGES)
        self.assertEqual(value["terminal_wealth"], 10.)
        self.assertTrue(self.validate(context, scenario, value))
        ambiguous = copy.deepcopy(scenario)
        ambiguous["distributions"][0]["id"] = "unlinked-news-right"
        with self.assertRaisesRegex(EvidenceError, "requires explicit binding"):
            self.validate(context, ambiguous, value)
        future = copy.deepcopy(event)
        future.update(id="later-right", record_date="2030-01-02", ex_date="2030-01-03")
        scenario = path(["C"], lambda *_: .9, [future])
        value = mpc.simulate(context, scenario, EMPTY, HOLD, STAGES)
        self.assertEqual(value["terminal_wealth"], 11.)
        self.assertTrue(self.validate(context, scenario, value))


if __name__ == "__main__":
    unittest.main()
