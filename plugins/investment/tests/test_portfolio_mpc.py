"""Analytical cash-flow truths; these fixtures are not investment evidence."""
import copy
import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import portfolio_mpc as mpc
from test_single_step import make_context, make_terms


def path(codes, price=None, events=()):
    dates = [(dt.date(2030, 1, 1)+dt.timedelta(days=index)).isoformat() for index in range(9)]
    return {"id": "analytical", "origin_at": "2029-01-01T20:00:00+08:00", "probability": 1.,
            "nav": {day: {code: price(code, day) if price else 1. for code in codes} for day in dates},
            "distributions": list(events), "scope": "synthetic_analytical_no_market_qualification"}


def with_second(context):
    context = copy.deepcopy(context)
    context["allocation_codes"] = context["universe"] = ["C", "D"]
    context["purchase_eligible_codes"] = ["C", "D"]
    context["fee_contracts"]["D"] = make_terms("D")
    context["identities"]["D"] = {"fund_group_id": "fund-D", "share_class": "C"}
    context["model_request"]["assets"].append({"code": "D", "sellable": True, "buyable": True,
        "buy_allowed": True, "min_buy": 1, "max_weight": 1})
    context["snapshot"]["prices"]["D"] = "1"
    context["market_ref"]["prices"]["D"] = "1"
    return context


def with_receivable(context, due_at, *, amount="10", kind="redemption", identity="old-receipt"):
    context = copy.deepcopy(context)
    row = {"receivable_id": identity, "source_identity": identity, "source_event_id": "source-"+identity,
           "kind": kind, "amount": amount, "due_at": due_at, "code": "C"}
    context["snapshot"].setdefault("receivables", []).append(row)
    context["snapshot"]["unsettled_cash"] = str(Decimal(context["snapshot"]["unsettled_cash"])+Decimal(amount))
    context["snapshot"]["equity"] = str(Decimal(context["snapshot"]["equity"])+Decimal(amount))
    context["receivables"] = copy.deepcopy(context["snapshot"]["receivables"])
    return context


STAGES = ["2030-01-01", "2030-01-02", "2030-01-03", "2030-01-05", "2030-01-09"]
HOLD = {"kind": "hold", "weights": {}}
EMPTY = {"buys": [], "sells": []}


class PortfolioCashFlowTests(unittest.TestCase):
    def test_existing_receipts_cross_horizon_and_cash_deadline_once(self):
        import verify
        context = with_receivable(make_context(cash="0"), "2030-01-02T07:00:00+08:00", amount="7")
        context = with_receivable(context, "2030-01-05", amount="3", kind="dividend", identity="old-dividend")
        context["spec"]["planning"].update(primary_horizon_days=3, cash_deadline_days=5, cash_required_amount="10")
        scenario = path(["C"])
        value = mpc.simulate(context, scenario, EMPTY, HOLD, ["2030-01-01", "2030-01-04"])
        self.assertEqual(value["terminal_wealth"], 10.)
        self.assertEqual((Decimal(value["settled_cash"]), Decimal(value["receivables"])), (7, 3))
        self.assertEqual(value["terminal_existing_receivables"][0]["receivable_id"], "old-dividend")
        self.assertEqual(Decimal(value["cash_deadline"]["available_cash"]), 10)
        self.assertEqual(Decimal(value["cash_deadline"]["receivables"]), 0)
        credits = [row for row in value["cash_deadline"]["cash_flow_log"] if row["kind"] == "credit_existing_receivable"]
        self.assertEqual([row["receivable_id"] for row in credits], ["old-receipt", "old-dividend"])
        self.assertEqual([row["date"] for row in credits], ["2030-01-02", "2030-01-05"])
        self.assertTrue(verify.cash_deadline_invariants(context, {"proposed_contribution": 0}, scenario, value))

    def test_existing_receipt_precision_controls_future_pricing_date(self):
        rule = {"kind": "allocate_settled_cash", "weights": {"C": "1"}}
        scenario = path(["C"], lambda _, day: 2. if day == "2030-01-02" else 1.)
        for due_at, pricing_date, expected_wealth in (("2030-01-01T23:00:00Z", "2030-01-02", 5.),
                                                     ("2030-01-02", "2030-01-03", 10.)):
            with self.subTest(due_at=due_at):
                context = with_receivable(make_context(cash="0"), due_at)
                context["decision_at"] = "2030-01-01T08:00:00+08:00"
                value = mpc.simulate(context, scenario, EMPTY, rule, ["2030-01-01", "2030-01-02", "2030-01-09"])
                buy = next(row for row in value["cash_flow_log"] if row["kind"] == "price_buy")
                self.assertEqual(buy["date"], pricing_date)
                self.assertEqual(value["terminal_wealth"], expected_wealth)
                self.assertTrue(value["future_used"])
                self.assertEqual(len([row for row in value["cash_flow_log"] if row["kind"] == "credit_existing_receivable"]), 1)

    def test_existing_receipt_cannot_fund_current_order_even_when_overdue(self):
        context = with_receivable(make_context(cash="0"), "2029-12-31T12:00:00+08:00")
        with self.assertRaisesRegex(mpc.PolicyInfeasible, "Buy spends unreceived, reserved or unconfirmed cash"):
            mpc.simulate(context, path(["C"]), {"buys": [{"code": "C", "cash_debit": "10"}], "sells": []}, HOLD, STAGES)
        held = mpc.simulate(context, path(["C"]), EMPTY, HOLD, STAGES)
        self.assertEqual(held["initial_available_cash"], "0")
        self.assertEqual(Decimal(held["settled_cash"]), 10)
        self.assertEqual(held["cash_flow_log"][0]["date"], "2030-01-01")

    def test_existing_dividend_identity_suppresses_duplicate_path_entitlement(self):
        context = with_receivable(make_context(cash="0", shares="10"), "2030-01-05", amount="1",
                                  kind="dividend", identity="analytical-right")
        event = {"code": "C", "id": "analytical-right", "record_date": "2030-01-01",
            "ex_date": "2030-01-02", "pay_date": "2030-01-05", "per_share": .1,
            "distribution_mode": "cash", "currency": "CNY",
            "entitlement_rule": {"subscribe_on_record_date": "excluded", "redeem_on_record_date": "included"}}
        value = mpc.simulate(context, path(["C"], lambda *_: .9, [event]), EMPTY, HOLD, STAGES)
        self.assertEqual(value["terminal_wealth"], 10.)
        self.assertEqual(Decimal(value["settled_cash"]), 1)
        self.assertEqual([row["kind"] for row in value["cash_flow_log"]], ["credit_existing_receivable"])

    def test_existing_dividend_different_source_ids_require_reconciliation(self):
        context = with_receivable(make_context(cash="0", shares="10"), "2030-01-05", amount="1",
                                  kind="dividend", identity="platform-receipt")
        event = {"code": "C", "id": "announcement-source", "record_date": "2030-01-01",
            "ex_date": "2030-01-02", "pay_date": "2030-01-05", "per_share": .1,
            "distribution_mode": "cash", "currency": "CNY",
            "entitlement_rule": {"subscribe_on_record_date": "excluded", "redeem_on_record_date": "included"}}
        scenario = path(["C"], lambda *_: .9, [event])
        with self.assertRaisesRegex(mpc.PolicyInfeasible, "reconcile_existing_dividend_source_identity"):
            mpc.simulate(context, scenario, EMPTY, HOLD, STAGES)
        context["context_hash"] = "analytical"
        result = mpc.optimise(context, {"status": "research_ready", "point_paths": [scenario],
            "selection_paths": [], "calibration_paths": []}, {})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["reason"], "existing_dividend_source_identity_unresolved")
        self.assertEqual(result["required_actions"][0]["action"], "reconcile_existing_dividend_source_identity")
        self.assertEqual(result["current_action"], EMPTY)
        event["record_date"] = "2030-01-02"
        event["ex_date"] = "2030-01-03"
        event["pay_date"] = "2030-01-06"
        new_right = mpc.simulate(context, path(["C"], lambda *_: .9, [event]), EMPTY, HOLD, STAGES)
        self.assertEqual(new_right["terminal_wealth"], 11.)
        self.assertEqual(Decimal(new_right["settled_cash"]), 2)

    def test_existing_receipt_at_last_instant_counts_by_end_of_source_date(self):
        context = with_receivable(make_context(cash="0"), "2030-01-02T23:59:59.999999+08:00")
        context["spec"]["planning"].update(primary_horizon_days=1, cash_deadline_days=1, cash_required_amount="10")
        value = mpc.simulate(context, path(["C"]), EMPTY, HOLD, ["2030-01-01", "2030-01-02"])
        self.assertEqual(Decimal(value["cash_deadline"]["available_cash"]), 10)
        self.assertEqual(value["cash_flow_log"][0]["credited_at"], "2030-01-02T23:59:59.999999+08:00")

    def test_existing_receipt_corruption_and_unknown_arrival_are_rejected(self):
        context = with_receivable(make_context(cash="0"), "2030-01-02")
        corruptions = [("amount", "11"), ("source_identity", "unbound"), ("due_at", None)]
        for field, replacement in corruptions:
            with self.subTest(field=field):
                corrupt = copy.deepcopy(context)
                corrupt["snapshot"]["receivables"][0][field] = replacement
                corrupt["receivables"] = copy.deepcopy(corrupt["snapshot"]["receivables"])
                with self.assertRaises(ValueError):
                    mpc.simulate(corrupt, path(["C"]), EMPTY, HOLD, STAGES)
        duplicate = copy.deepcopy(context)
        duplicate["snapshot"]["receivables"].append(copy.deepcopy(duplicate["snapshot"]["receivables"][0]))
        duplicate["receivables"] = copy.deepcopy(duplicate["snapshot"]["receivables"])
        with self.assertRaisesRegex(ValueError, "identity"):
            mpc.simulate(duplicate, path(["C"]), EMPTY, HOLD, STAGES)
        missing = copy.deepcopy(context)
        del missing["snapshot"]["receivables"]
        del missing["receivables"]
        with self.assertRaisesRegex(ValueError, "details differ"):
            mpc.simulate(missing, path(["C"]), EMPTY, HOLD, STAGES)

    def test_cash_deadline_credits_sale_on_receipt_date_without_extending_primary_wealth(self):
        import verify
        context = make_context(cash="0", shares="100")
        context["spec"]["planning"].update(primary_horizon_days=3, cash_deadline_days=4, cash_required_amount="100")
        scenario = path(["C"])
        scenario["nav"] = {day: prices for day, prices in scenario["nav"].items() if day <= "2030-01-04"}
        action = {"buys": [], "sells": [{"lot_id": "old", "shares": "100"}]}
        value = mpc.simulate(context, scenario, action, HOLD, ["2030-01-01", "2030-01-04"])
        self.assertEqual(value["terminal_wealth"], 100)
        self.assertEqual(Decimal(value["settled_cash"]), 0)
        self.assertEqual(Decimal(value["receivables"]), 100)
        self.assertEqual(Decimal(value["cash_deadline"]["available_cash"]), 100)
        self.assertTrue(verify.cash_deadline_invariants(context, {"proposed_contribution": 0}, scenario, value))
        earlier = copy.deepcopy(context)
        earlier["spec"]["planning"]["cash_deadline_days"] = 3
        before_receipt = mpc.simulate(earlier, scenario, action, HOLD, ["2030-01-01", "2030-01-04"])
        self.assertEqual(before_receipt["terminal_wealth"], value["terminal_wealth"])
        self.assertEqual(Decimal(before_receipt["cash_deadline"]["available_cash"]), 0)
        self.assertFalse(verify.cash_deadline_invariants(earlier, {"proposed_contribution": 0}, scenario, before_receipt))

    def test_cash_deadline_refund_requires_confirmation_and_unreserved_balance(self):
        import verify
        context = make_context(cash="2")
        context["fee_contracts"]["C"]["trade_precision"]["share_step"] = "1"
        context["spec"]["planning"].update(primary_horizon_days=1, cash_deadline_days=1, cash_required_amount="0.1")
        scenario = path(["C"], lambda _, day: 1.9)
        action = {"buys": [{"code": "C", "cash_debit": "2"}], "sells": []}
        value = mpc.simulate(context, scenario, action, HOLD, ["2030-01-01", "2030-01-02"])
        self.assertEqual(Decimal(value["cash_deadline"]["available_cash"]), 0)
        self.assertFalse(verify.cash_deadline_invariants(context, {"proposed_contribution": 0}, scenario, value))
        context["spec"]["planning"]["cash_deadline_days"] = 2
        received = mpc.simulate(context, scenario, action, HOLD, ["2030-01-01", "2030-01-02"])
        self.assertEqual(Decimal(received["cash_deadline"]["available_cash"]), Decimal(".1"))
        self.assertTrue(verify.cash_deadline_invariants(context, {"proposed_contribution": 0}, scenario, received))
        occupied = make_context(cash="100")
        occupied["snapshot"].update(available_cash="80", reserved_cash="20")
        occupied = with_receivable(occupied, "2030-01-09", amount="500")
        occupied["spec"]["planning"].update(primary_horizon_days=1, cash_deadline_days=1, cash_required_amount="90")
        blocked = mpc.simulate(occupied, path(["C"]), EMPTY, HOLD, ["2030-01-01", "2030-01-02"])
        self.assertEqual(Decimal(blocked["cash_deadline"]["available_cash"]), 80)
        self.assertFalse(verify.cash_deadline_invariants(occupied, {"proposed_contribution": 0}, path(["C"]), blocked))

    def test_cash_deadline_cannot_price_without_exact_nav(self):
        context = make_context(cash="0", shares="100")
        context["spec"]["planning"].update(primary_horizon_days=2, cash_deadline_days=4, cash_required_amount="100")
        scenario = path(["C"])
        del scenario["nav"]["2030-01-02"]
        with self.assertRaisesRegex(ValueError, "Missing exact source-modeled execution NAV date"):
            mpc.simulate(context, scenario, {"buys": [], "sells": [{"lot_id": "old", "shares": "100"}]},
                         HOLD, ["2030-01-01", "2030-01-03"])

    def test_null_cash_deadline_keeps_the_original_wealth_and_journal(self):
        context = make_context()
        original = mpc.simulate(context, path(["C"]), EMPTY, HOLD, STAGES)
        context["spec"]["planning"].update(cash_deadline_days=None, cash_required_amount=None)
        unchanged = mpc.simulate(context, path(["C"]), EMPTY, HOLD, STAGES)
        self.assertEqual(unchanged, original)
        self.assertNotIn("cash_deadline", unchanged)

    def test_fixed_sale_quantity_higher_fee_reduces_wealth_by_exact_cash_charge(self):
        context = make_context(cash="0", shares="100")
        action = {"buys": [], "sells": [{"lot_id": "old", "shares": "100"}]}
        original = mpc.simulate(context, path(["C"]), action, HOLD, STAGES)
        context["fee_contracts"]["C"]["redemption"]["rate"] = "0.01"
        charged = mpc.simulate(context, path(["C"]), action, HOLD, STAGES)
        self.assertEqual(original["terminal_wealth"], 100)
        self.assertEqual(charged["terminal_wealth"], 99)
        self.assertEqual(charged["fees"]-original["fees"], 1)
        self.assertEqual(Decimal(original["settled_cash"])-Decimal(charged["settled_cash"]), 1)

    def test_full_book_rotation_counts_future_value_but_not_current_cash(self):
        context = with_second(make_context(cash="0", shares="100", exit_rate=".01"))
        def prices(code, day):
            return (1.02 if day >= "2030-01-02" else 1.) if code == "C" else (1.2 if day == "2030-01-09" else 1.)
        original = copy.deepcopy(context)
        scenario = path(["C", "D"], prices)
        action = {"buys": [], "sells": [{"lot_id": "old", "shares": "100"}]}
        value = mpc.simulate(context, scenario, action, {"kind": "allocate_settled_cash", "weights": {"D": "1"}}, STAGES)
        held = mpc.simulate(context, scenario, EMPTY, HOLD, STAGES)
        # 100*1.02 - 1% exit fee =100.98; then 100.98 D shares at 1,
        # terminal D NAV1.20 yields121.176, compared with held C102.
        self.assertAlmostEqual(value["terminal_wealth"], 121.176)
        self.assertAlmostEqual(held["terminal_wealth"], 102.)
        self.assertAlmostEqual(value["fees"], 1.02)
        credit = next(row for row in value["cash_flow_log"] if row["kind"] == "credit_sale")
        future = next(row for row in value["cash_flow_log"] if row["kind"] == "reserve_buy")
        self.assertEqual(credit["date"], "2030-01-05")
        self.assertGreaterEqual(future["date"], credit["date"])
        self.assertTrue(future["hypothetical_future"])
        self.assertEqual(value["current_action"], action)
        self.assertFalse(value["future_rule_is_order"])
        self.assertEqual(context, original)
        self.assertEqual(mpc.verify_cash_flow(value)["status"], "passed")

    def test_multiple_stages_do_not_overwrite_acquired_lot(self):
        context = make_context(cash="100")
        value = mpc.simulate(context, path(["C"], lambda _, day: 1.1 if day == STAGES[-1] else 1.),
            {"buys": [{"code": "C", "cash_debit": "50"}], "sells": []},
            {"kind": "allocate_settled_cash", "weights": {"C": "1"}}, STAGES)
        self.assertEqual(len(value["terminal_lots"]), 2)
        self.assertEqual(sum(Decimal(row["shares"]) for row in value["terminal_lots"]), Decimal("100"))
        self.assertAlmostEqual(value["terminal_wealth"], 110.)

    def test_refund_is_available_after_confirmation_and_conserved(self):
        context = make_context(cash="2")
        context["fee_contracts"]["C"]["trade_precision"]["share_step"] = "1"
        value = mpc.simulate(context, path(["C"], lambda _, day: 2.09 if day == STAGES[-1] else 1.9),
            {"buys": [{"code": "C", "cash_debit": "2"}], "sells": []}, HOLD, STAGES)
        priced = next(row for row in value["cash_flow_log"] if row["kind"] == "price_buy")
        refund = next(row for row in value["cash_flow_log"] if row["kind"] == "credit_refund")
        self.assertEqual(Decimal(priced["available_after"]), 0)
        self.assertEqual(Decimal(refund["available_after"]), Decimal(".1"))
        self.assertEqual(refund["date"], "2030-01-03")
        self.assertAlmostEqual(value["terminal_wealth"], 2.19)

    def test_record_ex_and_pay_do_not_double_count_or_advance_cash(self):
        context = make_context(cash="0", shares="10")
        event = {"code": "C", "id": "analytical-right", "record_date": "2030-01-02",
            "ex_date": "2030-01-03", "pay_date": "2030-01-05", "per_share": .1,
            "distribution_mode": "cash", "currency": "CNY",
            "entitlement_rule": {"subscribe_on_record_date": "excluded", "redeem_on_record_date": "included"}}
        value = mpc.simulate(context, path(["C"], lambda _, day: .9 if day >= "2030-01-03" else 1., [event]), EMPTY, HOLD, STAGES)
        self.assertAlmostEqual(value["terminal_wealth"], 10.)
        right = next(row for row in value["cash_flow_log"] if row["kind"] == "distribution_receivable")
        credit = next(row for row in value["cash_flow_log"] if row["kind"] == "credit_distribution")
        self.assertEqual(right["date"], "2030-01-03")
        self.assertEqual(credit["date"], "2030-01-05")
        self.assertEqual(Decimal(credit["amount"]), Decimal("1"))

    def test_failed_multileg_submission_rolls_back_all_reservations(self):
        context = with_second(make_context(cash="10"))
        state = mpc._initial(context, Decimal(0))
        before = copy.deepcopy(state)
        with self.assertRaises(mpc.PolicyInfeasible):
            mpc._submit(context, state, {"buys": [{"code": "C", "cash_debit": "5"},
                {"code": "D", "cash_debit": "10"}], "sells": []}, STAGES[0], "20:00:00", path(["C", "D"]), hypothetical=True)
        self.assertEqual(state, before)

    def test_risk_recursion_has_independent_weighted_tail_truth(self):
        paths = [{"id": str(index), "probability": .25} for index in range(4)]
        losses = [20., 0., -10., -20.]
        self.assertEqual(mpc.tail_mean(losses, [.25]*4, .5), 10.)
        value, audit = mpc.nested_tail(losses, paths, {"2030-01-05": [["0", "1"], ["2", "3"]]},
            ["2030-01-01", "2030-01-05", "2030-01-09"], .5)
        self.assertEqual(value, 20.)
        self.assertFalse(audit["full_information_time_consistency_claimed"])

    def test_historical_pricing_gap_is_not_filled_by_interpolation(self):
        context = make_context(cash="2")
        scenario = path(["C"])
        del scenario["nav"]["2030-01-02"]
        with self.assertRaises(ValueError):
            mpc.simulate(context, scenario, {"buys": [{"code": "C", "cash_debit": "2"}], "sells": []}, HOLD, STAGES)


    def test_unknown_multi_lot_fee_rounding_blocks_terminal_redeem_baseline(self):
        context = make_context(cash="0",shares="3")
        context["snapshot"]["positions"].append({"lot_id":"new","code":"C","shares":"5","reserved_shares":"0","acquired_at":"2029-12-31T00:00:00+08:00"})
        context["snapshot"]["equity"]="8"
        marked = mpc.simulate(context,path(["C"]),EMPTY,HOLD,STAGES)
        self.assertEqual(marked["terminal_wealth"],8.)
        context["spec"]["planning"]["primary_goal"]="redeem"
        with self.assertRaisesRegex(mpc.PolicyInfeasible,"rounding scope required"):
            mpc.simulate(context,path(["C"]),EMPTY,HOLD,STAGES)


if __name__ == "__main__":
    unittest.main()
