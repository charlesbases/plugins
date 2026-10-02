"""Deterministic engineering boundaries; never statistical investment evidence."""
import copy
import unittest
from contracts import fingerprint
import execution
import allocation
from test_allocation import source_context
import portfolio_mpc as mpc
import single_step_wealth as wealth
from test_single_step import make_context


class FrozenPolicyContractTests(unittest.TestCase):
    def deadline_case(self):
        import ledger
        import newtrade_guard
        from test_strategy_execution import spec
        from test_portfolio_mpc import path
        context = source_context(cash="100")
        context["purchase_eligible_codes"] = []
        context["spec"] = spec()
        context["spec"]["planning"].update(primary_horizon_days=8, cash_deadline_days=8, cash_required_amount="200")
        context["spec"]["decision"].update(max_current_actions=4, max_policy_count=16, future_cash_fractions=[1])
        context["account_id"], context["blocked"] = "main", False
        context["risk_state"].update(net_principal="100", loss_tolerance="0.1", principal_floor="90", remaining_loss_budget="10")
        context["trade_family_review_index"] = 1
        context["trade_state"] = newtrade_guard.derive_state(ledger.initial_state("CNY"), [], context["decision_at"],
            context["spec"]["trade_policy"]["economic"]["fee_window_days"])
        context["context_hash"] = fingerprint(context)
        scenario = path(["C"])
        marks = {"C":{"nav_date":"2030-01-01","value":"1","known_at":"2030-01-01T16:00:00+08:00",
            "source_ref":{"synthetic_analytical_known_mark":True}}}
        scenario.update(price_schema="source_role_price_paths_v3",known_marks=marks)
        selection = [{**scenario,"id":identity,"probability":.5} for identity in ("paired-a","paired-b")]
        paths = {"schema":"source_role_price_paths_v3","known_marks":marks,
            "status": "research_ready", "path_hash": "b"*64, "source_hash": "c"*64,
            "stage_dates": ["2030-01-01", "2030-01-09"], "selection_paths": selection,
            "point_paths": [scenario], "prefix_groups": {}, "calibration_paths": []}
        comparison = allocation.build_source_actions(context)
        return context, paths, comparison

    def test_cash_requirement_rejects_even_the_unmet_baseline(self):
        context, paths, comparison = self.deadline_case()
        actual = mpc.optimise(context, paths, comparison)
        self.assertEqual(actual["status"], "partial")
        self.assertIsNone(actual["selected_policy"])
        self.assertEqual(actual["current_action"], {"buys": [], "sells": []})
        self.assertTrue(all(not row["eligible"] for group in actual["funding_options"] for row in group["candidates"]))
        self.assertEqual(actual["reason"], "cash_deadline_requirement_not_established")
        self.assertEqual(mpc.validate_result(actual, {"context": context, "paths": paths, "comparison": comparison})["status"], "passed")

    def test_independent_deadline_validation_rejects_invented_available_cash(self):
        import verify
        from contracts import EvidenceError
        context, paths, comparison = self.deadline_case()
        actual = mpc.optimise(context, paths, comparison)
        calculation = {"mpc": actual, "paths": paths, "orders": {"orders": []}}
        self.assertEqual(verify.numerical_invariants(context, calculation)["status"], "passed")
        forged = copy.deepcopy(calculation)
        policy = forged["mpc"]["funding_options"][0]["candidates"][0]
        policy["point"]["cash_deadline"].update(available_cash="200", passes=True)
        policy["point"]["valuation_hash"] = fingerprint({key: value for key, value in policy["point"].items() if key != "valuation_hash"})
        with self.assertRaisesRegex(EvidenceError, "Deadline snapshot"):
            verify.numerical_invariants(context, forged)

    def test_independent_deadline_validation_rejects_omitted_purchase_reservation(self):
        import verify
        from contracts import EvidenceError
        from test_portfolio_mpc import path, HOLD
        context = make_context()
        context["spec"]["planning"].update(primary_horizon_days=1, cash_deadline_days=2, cash_required_amount="100")
        scenario = path(["C"])
        value = mpc.simulate(context, scenario, {"buys": [{"code": "C", "cash_debit": "50"}], "sells": []},
                             HOLD, ["2030-01-01", "2030-01-02"])
        self.assertEqual(value["cash_deadline"]["available_cash"], "50")
        value["cash_deadline"].update(cash_flow_log=[], available_cash="100", passes=True)
        with self.assertRaisesRegex(EvidenceError, "Deadline journal omits"):
            verify.cash_deadline_invariants(context, {"proposed_contribution": 0}, scenario, value)

    def test_partial_source_path_has_valid_stage_envelope_without_trade_qualification(self):
        import validation_runtime
        context = {"context_hash": "a"*64}
        paths = {"status": "partial", "path_hash": "b"*64,
            "required_actions": [{"action": "supply_source_paths"}]}
        inputs = {"context": context, "paths": paths, "comparison": {}}
        actual = mpc.optimise(context, paths, {})
        self.assertEqual(actual["current_action"], {"buys": [], "sells": []})
        result = validation_runtime.validate_result(mpc.validate_result(actual, inputs))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["result_status"], "partial")
        self.assertFalse(result["checks"]["ready_policy_cash_flows_verified"])

    def test_financially_equal_amounts_have_same_frozen_policy_family(self):
        context = source_context(cash="5")
        a = mpc.freeze_policies(context, allocation.build_source_actions(context), max_current_actions=16, cash_fractions=[1])
        equivalent = copy.deepcopy(context)
        equivalent["snapshot"]["available_cash"] = "5.0000"
        b = mpc.freeze_policies(equivalent, allocation.build_source_actions(equivalent), max_current_actions=16, cash_fractions=[1])
        self.assertEqual(a["family_hash"],b["family_hash"])
        self.assertEqual({row["cash_debit"] for policy in a["policies"] for row in policy["current_action"]["buys"]},{"1","2","3","4","5"})

    def test_action_budget_shortfall_produces_no_truncated_policy(self):
        context = source_context(cash="5")
        result = mpc.freeze_policies(context, allocation.build_source_actions(context), max_current_actions=1, cash_fractions=[1])
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["policies"],[])
        self.assertEqual(result["required_actions"][0]["required"],6)

    def test_policy_budget_shortfall_does_not_evaluate_incumbent(self):
        from unittest.mock import patch
        context,paths,_ = self.deadline_case()
        context["spec"]["decision"]["max_policy_count"] = 1
        context["spec"]["decision"]["max_current_actions"] = 16
        context["purchase_eligible_codes"] = ["C"]
        with patch.object(mpc,"_policy_values",side_effect=AssertionError("unbudgeted valuation")):
            result = mpc.optimise(context,paths,allocation.build_source_actions(context))
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["reason"],"zero_funding_policy_scope_incomplete")
        self.assertTrue(result["frozen_policy_family"]["funding_registry"])
        self.assertIsNone(result["selected_policy"])

    def test_principal_breach_selects_source_sale_by_complete_path_risk(self):
        from test_portfolio_mpc import path
        context,paths,_ = self.deadline_case()
        source = source_context(cash="0",shares="8")
        context["snapshot"] = source["snapshot"]
        context["fee_contracts"] = source["fee_contracts"]
        context["risk_state"] = source["risk_state"]
        context["spec"]["planning"].update(cash_deadline_days=None,cash_required_amount=None)
        context["spec"]["decision"].update(max_current_actions=16,max_policy_count=64)
        context["spec"]["trade_policy"]["economic"]["minimum_risk_reduction_amount"] = "1"
        context["context_hash"] = fingerprint({key:value for key,value in context.items() if key!="context_hash"})
        source_path = path(["C"],lambda code,day: .5 if day=="2030-01-09" else 1.)
        source_path.update(price_schema=paths["schema"],known_marks=paths["known_marks"])
        paths.update(point_paths=[source_path],selection_paths=[{**source_path,"id":identity,"probability":.5}
            for identity in ("paired-a","paired-b")])
        result = mpc.optimise(context,paths,allocation.build_source_actions(context))
        self.assertEqual(result["status"],"research_ready")
        self.assertEqual(result["current_action"],{"buys":[],"sells":[{"lot_id":"old","shares":"8"}]})
        self.assertEqual(result["selected_policy"]["trade_guard"]["status"],"risk_recovery")
        self.assertEqual(result["decision_status"],"risk_recovery")
        self.assertFalse(result["selected_policy"]["eligible"])
        self.assertTrue(result["selected_policy"]["recovery_eligible"])
        self.assertEqual(result["selected_policy"]["point"]["terminal_wealth"],8.)
        self.assertEqual(result["selected_policy"]["absolute_cvar"],0.)

    def test_source_gross_exposure_above_NAV_changes_limit_result(self):
        identities = {"C": {"fund_group_id": "fund-C"}}
        constraints = {"fund_group_limits": {}, "sector_limits": {"medical": .6}}
        result = wealth.exposure_check({"C": 50}, 100, identities, constraints,
            {"C": {"medical": {"upper": "1.4"}}})
        self.assertEqual(result["sector_upper_values"]["medical"], 70)
        self.assertFalse(result["feasible"])
        self.assertIn("sector_limits:medical", result["violations"])

    def test_unknown_exposure_is_not_zero_and_zero_position_is_safe(self):
        identities = {"C": {"fund_group_id": "fund-C"}}
        constraints = {"fund_group_limits": {}, "sector_limits": {"medical": .6}}
        unknown = wealth.exposure_check({"C": 50}, 100, identities, constraints)
        self.assertFalse(unknown["feasible"])
        self.assertIn("unknown_sector_upper:C:medical", unknown["violations"])
        self.assertTrue(wealth.exposure_check({"C": 0}, 100, identities, constraints)["feasible"])

    def test_compiler_publishes_only_current_action_not_future_or_extra_funding(self):
        context = make_context()
        context["blocked"] = False
        from test_strategy_execution import spec
        context["spec"]["currency"] = "CNY"
        context["spec"]["trade_policy"] = spec()["trade_policy"]
        context["trade_family_review_index"] = 1
        context["spec"]["execution"] = {"money_step": ".01", "share_step": ".01"}
        context["model_request"]["assets"][0].update(settlement_days=2, subscription_fee=0)
        context["context_hash"] = fingerprint(context)
        original = copy.deepcopy(context)
        action = {"buys": [{"code": "C", "cash_debit": "5.12"}], "sells": []}
        condition = mpc._conditional_rule(context, action, {"buys": [], "sells": []},
            {"code": "C", "cash_debit": "5.12", "lot_id": "model:1"}, "ge", wealth.build_clock_context(context)["evaluation_date"])
        selected = {"id": "analytical-current-only", "proposed_contribution": 0,
            "eligible": True, "current_action": action, "future_rule": {"weights": {"C": "1"}, "conditional_sells": [condition]},
            "trade_guard": {"status": "eligible"}, "point": {"valuation_hash": "a"*64},
            "current_projection": {"scope": "unit_contract_only"},
            "reference_valuation":mpc.reference_valuation(context,action)}
        result = {"status": "research_ready", "paths": {"path_hash": "b"*64},
            "mpc": {"status": "research_ready", "context_hash": context["context_hash"],
                "selected_policy": selected, "result_hash": "c"*64,
                "funding_options": [{"proposed_contribution": 2000}]}}
        output = execution.compile_mpc_orders(result, context, {})
        self.assertEqual(output["status"], "ready")
        self.assertEqual(len(output["orders"]), 1)
        self.assertEqual(output["orders"][0]["cash_limit"], "5.12")
        self.assertFalse(output["mpc_verification"]["future_trades_are_orders"])
        self.assertEqual({row["condition"] for row in output["waiting"]},
            {"additional_cash_confirmation_then_new_decision", "actual_settlement_and_confirmation_then_new_analysis", "actual_observable_zero_gain_condition_then_new_analysis"})
        self.assertEqual(context, original)


if __name__ == "__main__":
    unittest.main()
