"""Independent engineering cash-flow truths; no live market performance claims."""
import copy
import datetime as dt
import unittest
from decimal import Decimal

import allocation
import mpc_calibration
import portfolio_mpc as mpc
from contracts import fingerprint
from test_allocation import source_context
from test_portfolio_mpc import HOLD, path, with_second


def protocol(cash="0", shares="8", funding=(0,), horizon=7):
    context = source_context(cash=cash, shares=shares, funding=funding)
    context["spec"]["decision"]["max_path_stages"] = 8
    context["spec"]["planning"]["primary_horizon_days"] = horizon
    return context


def frozen(context):
    return mpc.freeze_policies(context, allocation.build_source_actions(context),
        max_current_actions=context["spec"]["decision"]["max_current_actions"],cash_fractions=[1.])


class SourcePolicyGroupTests(unittest.TestCase):
    def nine_receipts(self,*,purchase):
        from test_portfolio_mpc import with_receivable
        context = protocol(cash="8",shares="0",horizon=30)
        context["purchase_eligible_codes"] = ["C"] if purchase else []
        for day in range(2,11):
            context = with_receivable(context,f"2030-01-{day:02}",amount="1",identity=f"confirmed-original-{day}")
        return context

    def test_nine_confirmed_receipts_are_accounting_not_hold_decisions(self):
        from test_strategy_execution import capture_engineering_marks
        context = self.nine_receipts(purchase=False)
        family = frozen(context)
        baseline = family["policies"][0]
        self.assertEqual(family["status"],"frozen")
        self.assertEqual(baseline["mode"],"baseline")
        self.assertEqual(baseline["local_decision_dates"],["2030-01-01","2030-01-31"])
        self.assertTrue(baseline["local_decision_scope_complete"])
        marks = capture_engineering_marks({**context["market_ref"],"observed_at":context["decision_at"]})["known_marks"]
        leaf = {"id":"analytical-original-receipts","nav":{str(dt.date(2030,1,1)+dt.timedelta(days=i)):{"C":1.} for i in range(31)},
            "price_schema":"source_role_price_paths_v3","known_marks":marks,"distributions":[]}
        value = mpc._policy_values(context,{"schema":"source_role_price_paths_v3","known_marks":marks},baseline,[leaf])[0]
        self.assertEqual(value["terminal_wealth"],17)
        self.assertEqual(value["initial_available_cash"],"8")
        self.assertEqual(Decimal(value["settled_cash"]),17)
        self.assertEqual(len([row for row in value["cash_flow_log"] if row["kind"]=="credit_existing_receivable"]),9)
        self.assertEqual(len(value["accounting_event_dates"]),31)

    def test_uncomputed_cash_feedback_local_budget_retains_registry_without_incumbent(self):
        context = self.nine_receipts(purchase=True)
        family = frozen(context)
        self.assertEqual(family["status"],"partial")
        self.assertEqual(family["groups"][0]["build_status"],"budget_partial")
        self.assertEqual(family["policies"],[])
        self.assertTrue(family["funding_registry"])
        gap = next(row for row in family["required_actions"] if row["action"]=="increase_declared_local_decision_budget_or_explicitly_narrow_scope")
        self.assertEqual((gap["required"],gap["budget"]),(11,8))
        self.assertTrue(gap["uncomputed_policies"])

    def partial_extra_optimizer_case(self, *, cash="0", shares="8", funding=(0,1000)):
        import ledger
        import newtrade_guard
        from test_strategy_execution import spec
        context = protocol(cash=cash,shares=shares,funding=funding,horizon=8)
        context["spec"] = spec()
        context["spec"]["planning"]["primary_horizon_days"] = 8
        context["spec"]["decision"].update(max_current_actions=10,max_policy_count=4096,future_cash_fractions=[1.])
        context["account_id"] = "main"
        context["trade_family_review_index"] = 1
        context["trade_state"] = newtrade_guard.derive_state(ledger.initial_state("CNY"),[],context["decision_at"],
            context["spec"]["trade_policy"]["economic"]["fee_window_days"])
        context["context_hash"] = fingerprint({key:value for key,value in context.items() if key!="context_hash"})
        marks = {"C":{"nav_date":"2030-01-01","value":"1","known_at":"2030-01-01T16:00:00+08:00",
            "source_ref":{"synthetic_analytical_known_mark":True}}}
        scenario = {**path(["C"]),"price_schema":"source_role_price_paths_v3","known_marks":marks}
        selection = [{**scenario,"id":identity,"probability":.5} for identity in ("paired-a","paired-b")]
        paths = {"schema":"source_role_price_paths_v3","known_marks":marks,"status":"research_ready","path_hash":"b"*64,
            "stage_dates":["2030-01-01","2030-01-09"],"point_paths":[scenario],"selection_paths":selection,
            "calibration_paths":[],"scope":"synthetic_cashflow_arithmetic_not_source_market_qualification"}
        from portfolio_paths import information_groups
        paths["prefix_groups"] = information_groups(selection,paths["stage_dates"])
        family = allocation.build_source_actions(context)
        result = mpc.optimise(context,paths,family)
        return context,paths,family,result

    def test_uncalibrated_held_baseline_stays_unqualified_when_extra_group_is_partial(self):
        # Original experiment is unchanged: cash0, shares8, no calibration.
        # Group isolation preserves its candidates; it cannot create evidence.
        context,paths,family,result = self.partial_extra_optimizer_case()
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["decision_status"],"risk_unresolved")
        self.assertIsNone(result["selected_policy"])
        self.assertEqual(result["current_action"],{"buys":[],"sells":[]})
        self.assertEqual(result["funding_options"][0]["build_status"],"complete")
        baseline = next(row for row in result["funding_options"][0]["candidates"] if row["mode"] == "baseline")
        self.assertFalse(baseline["eligible"])
        self.assertFalse(baseline["recovery_eligible"])
        self.assertIn("independent_path_policy_calibration_required",baseline["trade_guard"]["reasons"])
        self.assertEqual(baseline["point"]["initial_known_wealth"],"8")
        self.assertEqual((context["snapshot"]["cash"],context["snapshot"]["positions"][0]["shares"]),("0","8"))
        self.assertEqual(paths["calibration_paths"],[])
        self.assertEqual(result["funding_options"][1]["build_status"],"budget_partial")
        self.assertIsNone(result["funding_options"][1]["best"])
        self.assertEqual(result["calibration"]["groups"][1]["reserved_alpha_weight"],result["funding_options"][1]["reserved_alpha_weight"])
        self.assertEqual(mpc.validate_result(result,{"context":context,"paths":paths,"comparison":family})["status"],"passed")

    def test_source_cash_identity_remains_selectable_when_small_extra_precision_group_is_partial(self):
        # Different explicitly synthetic account: cash8, shares0 and proposed
        # extra8. Original one-unit source precision gives9vs17 choices with
        # unchanged computation budget10; original fee terms remain unchanged.
        context,paths,family,result = self.partial_extra_optimizer_case(cash="8",shares="0",funding=(0,8))
        self.assertEqual(result["status"],"research_ready")
        self.assertEqual(result["decision_status"],"feasible_selected")
        self.assertEqual(result["selected_policy"]["mode"],"baseline")
        self.assertEqual(result["selected_policy"]["proposed_contribution"],0)
        self.assertEqual(result["selected_policy"]["trade_guard"]["risk_evidence_basis"],"source_cash_identity")
        self.assertEqual(result["current_action"],{"buys":[],"sells":[]})
        self.assertEqual(result["funding_options"][0]["build_status"],"complete")
        self.assertEqual(result["funding_options"][1]["build_status"],"budget_partial")
        self.assertEqual(family["funding_options"][1]["combination_count"],17)
        self.assertIsNone(result["funding_options"][1]["best"])
        self.assertEqual(result["calibration"]["groups"][1]["reserved_alpha_weight"],result["funding_options"][1]["reserved_alpha_weight"])
        self.assertEqual(result["selected_policy"]["point"]["initial_known_wealth"],"8")
        self.assertEqual((context["snapshot"]["cash"],context["snapshot"]["positions"]),("8",[]))
        self.assertEqual(mpc.validate_result(result,{"context":context,"paths":paths,"comparison":family})["status"],"passed")

    def test_optional_policy_budget_keeps_zero_and_unspent_group_alpha(self):
        context = protocol(funding=(0,1000))
        complete = frozen(context)
        context["spec"]["decision"]["max_policy_count"] = 100
        partial = frozen(context)
        self.assertEqual(partial["status"],"frozen")
        self.assertEqual([row["build_status"] for row in partial["groups"]],["complete","budget_partial"])
        self.assertEqual(partial["groups"][0]["policies"],complete["groups"][0]["policies"])
        self.assertEqual(partial["funding_registry"],complete["funding_registry"])
        self.assertEqual(partial["groups"][1]["policies"],[])
        self.assertEqual(partial["groups"][1]["required_actions"][0]["required"], len(complete["groups"][1]["policies"]))

    def fee_boundary(self,boundary):
        context = protocol(shares="8")
        context["fee_contracts"]["C"]["trade_precision"]["money_step"] = ".01"
        context["decision_at"] = "2030-01-01T12:00:00+08:00"
        acquired = dt.date(2030,1,1)-dt.timedelta(days=boundary-1)
        context["snapshot"]["positions"][0]["acquired_at"] = str(acquired)+"T00:00:00+08:00"
        context["fee_contracts"]["C"]["redemption"] = {"kind":"holding_tiers","bands":[
            {"minimum":"0","fee":{"kind":"percentage","rate":".015"}},
            {"minimum":str(boundary),"fee":{"kind":"percentage","rate":"0"}}]}
        family = frozen(context)
        waits = [row for row in family["policies"] if row["current_action"] == {"buys": [], "sells": []} and row["future_rule"].get("conditional_sells")]
        self.assertTrue(waits)
        self.assertEqual({date for row in waits for date in row["future_rule"]["conditional_sells"][0]["decision_dates"]},{"2030-01-02"})
        return context, waits

    def test_source_seven_day_wait_node(self):self.fee_boundary(7)
    def test_source_thirty_day_wait_node(self):self.fee_boundary(30)
    def test_source_sixty_day_wait_node(self):self.fee_boundary(60)
    def test_source_ninety_day_wait_node(self):self.fee_boundary(90)

    def test_every_lower_source_fee_boundary_is_frozen_without_future_prices(self):
        context = protocol(shares="10000",horizon=30)
        context["decision_at"] = "2030-01-01T12:00:00+08:00"
        context["snapshot"]["positions"][0]["acquired_at"] = "2029-12-26T00:00:00+08:00"
        context["fee_contracts"]["C"]["redemption"] = {"kind":"holding_tiers","bands":[
            {"minimum":"0","fee":{"kind":"percentage","rate":".015"}},
            {"minimum":"7","fee":{"kind":"percentage","rate":".005"}},
            {"minimum":"30","fee":{"kind":"percentage","rate":"0"}}]}
        family = frozen(context)
        waits = [row for row in family["policies"] if row["future_rule"].get("conditional_sells",[{}])[0].get("source_action",{}).get("sells")==[{"lot_id":"old","shares":"10000"}]]
        self.assertEqual({date for row in waits for date in row["future_rule"]["conditional_sells"][0]["decision_dates"]},{"2030-01-02","2030-01-25"})
        self.assertTrue(all(len(row["local_decision_dates"])<=8 for row in waits))


    def test_latest_source_receipt_window_keeps_cash_after_primary_wealth(self):
        context = protocol(shares="8")
        context["decision_at"] = "2030-01-01T12:00:00+08:00"
        context["spec"]["planning"].update(cash_deadline_days=8,cash_required_amount="8")
        context["fee_contracts"]["C"]["trade_precision"]["money_step"] = ".01"
        context["fee_contracts"]["C"]["redemption"] = {"kind": "percentage", "rate": ".1"}
        family = frozen(context)
        wait = next(row for row in family["policies"] if row["future_rule"].get("conditional_sells",[{}])[0].get("source_action",{}).get("sells")==[{"lot_id":"old","shares":"8"}]
            and not row["future_rule"]["weights"] and row["future_rule"]["conditional_sells"][0]["comparison"] == "ge")
        self.assertEqual(wait["future_rule"]["conditional_sells"][0]["decision_dates"],["2030-01-02", "2030-01-06"])
        scenario = path(["C"], lambda code, day: 1 if day < "2030-01-05" else 10/9)
        value = mpc.simulate(context,scenario,wait["current_action"],wait["future_rule"],wait["local_decision_dates"])
        self.assertEqual(next(row["submitted_at"] for row in value["cash_flow_log"] if row["kind"] == "price_sell"), "2030-01-06T12:00:00+08:00")
        self.assertEqual(value["terminal_wealth"],8)
        self.assertEqual(Decimal(value["receivables"]),8)
        self.assertEqual(Decimal(value["settled_cash"]),0)
        self.assertEqual(Decimal(value["cash_deadline"]["available_cash"]),8)
        self.assertTrue(value["cash_deadline"]["passes"])

    def test_optional_action_budget_keeps_zero_family_and_reserved_multiplicity(self):
        context = protocol(funding=(0,1000))
        complete = frozen(context)
        context["spec"]["decision"]["max_current_actions"] = 10
        partial = frozen(context)
        self.assertEqual(partial["status"],"frozen")
        zero, extra = partial["groups"]
        self.assertEqual(zero["build_status"],"complete")
        self.assertEqual(extra["build_status"],"budget_partial")
        self.assertEqual(extra["policies"],[])
        self.assertEqual(zero["policies"],complete["groups"][0]["policies"])
        self.assertEqual(partial["funding_registry"],complete["funding_registry"])
        self.assertEqual(partial["funding_registry_hash"],complete["funding_registry_hash"])
        self.assertLess(zero["alpha_weight"],1)
        weight,total = mpc_calibration.funding_error_weight(partial["funding_registry"],
            partial["funding_registry_hash"],zero["funding_group_id"],zero["policies"])
        self.assertEqual(weight,zero["alpha_weight"])
        self.assertEqual(total,partial["planned_policy_count_upper"])
        tampered = copy.deepcopy(partial["funding_registry"])
        tampered.pop()
        with self.assertRaisesRegex(ValueError,"registry/hash"):
            mpc_calibration.funding_error_weight(tampered,partial["funding_registry_hash"],zero["funding_group_id"],zero["policies"])

    def test_normal_confirmation_reference_crosses_source_holding_fee_boundary(self):
        context = protocol(shares="1000")
        context["decision_at"] = "2030-01-07T12:00:00+08:00"
        context["as_of"] = "2030-01-07"
        context["snapshot"]["positions"][0]["acquired_at"] = "2030-01-01T00:00:00+08:00"
        terms = context["fee_contracts"]["C"]
        terms["holding"]["end_event"] = "confirmation_date"
        terms["redemption"] = {"kind":"holding_tiers","bands":[
            {"minimum":"0","fee":{"kind":"percentage","rate":".015"}},
            {"minimum":"7","fee":{"kind":"percentage","rate":".005"}}]}
        result = mpc.reference_valuation(context,{"buys":[],"sells":[{"lot_id":"old","shares":"1000"}]})
        self.assertEqual(result["legs"][0]["normal_confirmation_date"],"2030-01-08")
        self.assertEqual(Decimal(result["legs"][0]["contractual_fee"]),5)
        self.assertEqual(Decimal(result["total_products"][0]["net"]),995)
        self.assertFalse(result["actual_execution_asserted"])

    def test_frozen_fee_wait_policy_beats_hold_and_immediate_rotation_after_receipt(self):
        context = with_second(protocol(shares="10000"))
        context["market_ref"]["price_dates"]["D"] = "2030-01-01"
        context["decision_at"] = "2030-01-01T12:00:00+08:00"
        context["purchase_eligible_codes"] = ["D"]
        context["snapshot"]["positions"][0]["acquired_at"] = "2029-12-26T00:00:00+08:00"
        context["fee_contracts"]["C"]["redemption"] = {"kind":"holding_tiers","bands":[
            {"minimum":"0","fee":{"kind":"percentage","rate":".015"}},
            {"minimum":"7","fee":{"kind":"percentage","rate":".005"}}]}
        context["context_hash"] = fingerprint(context)
        family = frozen(context)
        full_sale = [{"lot_id":"old","shares":"10000"}]
        hold = next(row for row in family["policies"] if row["mode"]=="baseline")
        immediate = next(row for row in family["policies"] if row["current_action"]["sells"]==full_sale and row["future_rule"]["weights"]=={"D":"1.0"})
        wait = next(row for row in family["policies"] if row["future_rule"].get("conditional_sells",[{}])[0].get("source_action",{}).get("sells")==full_sale
            and row["future_rule"]["weights"]=={"D":"1.0"} and row["future_rule"]["conditional_sells"][0]["comparison"] == "le")
        self.assertEqual(wait["future_rule"]["conditional_sells"][0]["decision_dates"],["2030-01-02"])
        scenario = path(["C","D"],lambda code,day: (1 if day<="2030-01-02" else .98) if code=="C" else (1 if day<="2030-01-05" else 1.005))
        def evaluate(policy):
            return mpc.simulate(context,scenario,policy["current_action"],policy["future_rule"],policy["local_decision_dates"])
        held,sold,waited = (evaluate(row) for row in (hold,immediate,wait))
        self.assertEqual(held["terminal_wealth"],9800)
        self.assertAlmostEqual(sold["terminal_wealth"],9899.25,places=6)
        self.assertGreater(waited["terminal_wealth"],sold["terminal_wealth"])
        self.assertAlmostEqual(waited["terminal_wealth"],9950,places=2)
        self.assertEqual(waited["conditional_submission_count"],1)
        buys = [row for row in waited["cash_flow_log"] if row["kind"]=="price_buy"]
        self.assertEqual(buys[0]["date"],"2030-01-06")
        self.assertGreater(len(waited["accounting_event_dates"]),len(wait["local_decision_dates"]))


if __name__ == "__main__":
    unittest.main()
