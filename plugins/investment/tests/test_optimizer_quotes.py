"""Small independent complete-path financial truths; analytical fixtures only."""
import unittest
from decimal import Decimal

import allocation
import calibration
import portfolio_mpc as mpc
import single_step_wealth as wealth
from test_allocation import source_context
from test_single_step import make_context, scenario
from test_portfolio_mpc import path, with_second, STAGES, HOLD, EMPTY


class SourceQuoteOptimizerTests(unittest.TestCase):
    def test_source_support_stress_keeps_existing_principal_loss(self):
        context = make_context(cash="100")
        context["snapshot"].update(reserved_cash="20", available_cash="80", unsettled_cash="10", equity="110")
        context["risk_state"].update(net_principal="200", loss_tolerance=".25",
                                     principal_floor="150", remaining_loss_budget="-40")
        distribution = scenario(context)
        projection = wealth.project(context, {"buys": [{"code": "C", "cash_debit": "50"}], "sells": []}, distribution)
        value = wealth.value(context, projection, distribution)
        stress = value["source_principal_stress"]
        # Recorded cash 100 + receivable 10 - subscription reserve 50 leaves
        # 60 if risky proceeds are zero. The old principal floor remains 150.
        self.assertEqual(stress["wealth_lower"], 60.)
        self.assertEqual(stress["principal_floor"], 150.)
        self.assertFalse(stress["principal_floor_survives"])
        self.assertFalse(stress["probability_assigned"])

    def test_joint_loss_tail_statistic_has_independent_twenty_percent_truth(self):
        import datetime as dt
        losses = list(range(-40, 40))
        dates = [(dt.date(2029, 1, 1)+dt.timedelta(days=index)).isoformat() for index in range(80)]
        inference = {"confidence_level": .9, "minimum_net_advantage": 0., "maximum_ci_half_width": 100.,
            "mc_cdf_tolerance": .01, "mc_failure_probability": .1, "max_bootstrap_repetitions": 30000,
            "bootstrap_seed": 3, "block_sensitivity_factors": [.5, 1., 2.], "max_observation_gap_days": 2}
        result = calibration._trade_principal_risk(dates, losses, inference, .2)
        # Sixteen worst equally weighted losses are the integers 24..39.
        self.assertEqual(result["estimate"], 31.5)
        self.assertEqual(result["status"], "conditional_calibrated")
        self.assertFalse(result["finite_sample_future_loss_guarantee"])

    def test_fee_break_minimum_and_cap_have_independent_enumerated_optimum(self):
        context = source_context()
        context["model_request"]["assets"][0].update(min_buy=2, max_weight=.625)
        context["fee_contracts"]["C"]["subscription"] = {"kind":"amount_tiers", "bands":[
            {"minimum":"0","fee":{"kind":"percentage","rate":".25"}},
            {"minimum":"5","fee":{"kind":"percentage","rate":"0"}}]}
        family = allocation.build_source_actions(context)
        source_path = path(["C"], lambda code, day: 1.1 if day == STAGES[-1] else 1.)
        distribution = scenario(context, terminal_nav=1.1, hold=1.1, buy=1.1)
        outcomes = []
        for action in family["funding_options"][0]["actions"]:
            projection = wealth.project(context, action, distribution)
            if projection["concentration_feasible"]:
                value = mpc.simulate(context, source_path, action, HOLD, STAGES)
                outcomes.append((value["terminal_wealth"], action))
        best = max(outcomes, key=lambda row: row[0])
        # Requests 2..4 pay 25%; 5 is the inclusive zero-fee threshold.
        # The independent five-share cap excludes larger holdings.
        self.assertEqual(best, (8.5, {"buys":[{"code":"C","cash_debit":"5"}],"sells":[]}))

    def test_static_sale_ranking_differs_from_complete_settled_cash_policy(self):
        context = with_second(source_context(cash="0", shares="8"))
        context["market_ref"]["price_dates"]["D"] = context["as_of"]
        context["spec"]["planning"]["primary_horizon_days"] = 8
        context["purchase_eligible_codes"] = ["D"]
        family = allocation.build_source_actions(context)
        frozen = mpc.freeze_policies(context, family, max_current_actions=512, cash_fractions=[1.])
        source_path = path(["C","D"], lambda code, day: (1.1 if code == "C" else 2.) if day == STAGES[-1] else 1.)
        baseline = mpc.simulate(context, source_path, EMPTY, HOLD, STAGES)
        sell = {"buys":[], "sells":[{"lot_id":"old","shares":"8"}]}
        static = mpc.simulate(context, source_path, sell, HOLD, STAGES)
        self.assertEqual((baseline["terminal_wealth"], static["terminal_wealth"]), (8.8,8.))
        outcomes = [(mpc.simulate(context, source_path, row["current_action"], row["future_rule"], STAGES)["terminal_wealth"], row) for row in frozen["policies"]]
        best = max(outcomes, key=lambda row: row[0])
        self.assertEqual(best[0], 16.)
        self.assertEqual(best[1]["current_action"], sell)
        self.assertEqual(best[1]["future_rule"]["weights"], {"D":"1.0"})
        value = mpc.simulate(context, source_path, sell, best[1]["future_rule"], STAGES)
        credit = next(row for row in value["cash_flow_log"] if row["kind"] == "credit_sale")
        buy = next(row for row in value["cash_flow_log"] if row["kind"] == "reserve_buy")
        self.assertGreaterEqual(buy["date"], credit["date"])
        self.assertTrue(buy["hypothetical_future"])
        self.assertFalse(value["future_rule_is_order"])
        self.assertEqual(value["policy_turnover"], 16.)

    def test_large_precision_domain_discloses_finite_menu_without_global_claim(self):
        family = allocation.build_source_actions(source_context(cash="1000"))
        group = family["funding_options"][0]
        self.assertTrue(group["current_action_scope_complete"])
        self.assertFalse(group["source_precision_domain_complete"])
        self.assertFalse(family["global_investment_optimality_claimed"])
        self.assertEqual({Decimal(row["cash_debit"]) for action in group["actions"] for row in action["buys"]}, {Decimal(x) for x in (1,250,500,750,1000)})

    def test_ab_and_new_c_funding_stays_separate_until_received(self):
        import copy
        from test_single_step import make_terms
        context = with_second(source_context(cash="0", shares="4", funding=(0,4)))
        context["snapshot"]["positions"].append({"lot_id":"old-D", "code":"D", "shares":"4", "reserved_shares":"0", "acquired_at":"2029-01-01T00:00:00+08:00"})
        context["snapshot"]["equity"] = "8"
        context["allocation_codes"] = context["universe"] = ["C","D","E"]
        context["purchase_eligible_codes"] = ["E"]
        context["fee_contracts"]["E"] = make_terms("E")
        context["fee_contracts"]["E"]["trade_precision"].update(money_step="1",share_step="1")
        context["identities"]["E"] = {"fund_group_id":"fund-E","share_class":"C"}
        context["snapshot"]["prices"]["E"] = "1"
        context["market_ref"]["prices"] = copy.deepcopy(context["snapshot"]["prices"])
        context["market_ref"]["price_dates"] = {code:context["as_of"] for code in context["allocation_codes"]}
        context["spec"]["planning"]["primary_horizon_days"] = 8
        context["risk_state"].update(net_principal="8",principal_floor="6",remaining_loss_budget="2")
        context["model_request"]["assets"].append({"code":"E","sellable":True,"buyable":True,"buy_allowed":True,"min_buy":1,"max_weight":1})
        original = copy.deepcopy(context)
        family = allocation.build_source_actions(context)
        frozen = mpc.freeze_policies(context, family, max_current_actions=512, cash_fractions=[1.])
        full_sales = [{"lot_id":"old","shares":"4"},{"lot_id":"old-D","shares":"4"}]
        policies = [row for row in frozen["policies"] if row["current_action"]["sells"] == full_sales and row["future_rule"]["weights"] == {"E":"1.0"}]
        zero = next(row for row in policies if row["proposed_contribution"] == 0)
        funded = next(row for row in policies if row["proposed_contribution"] == 4 and row["current_action"]["buys"] == [{"code":"E","cash_debit":"4"}])
        source_path = path(["C","D","E"],lambda code,day: 2. if code=="E" and day==STAGES[-1] else 1.)
        zero_value = mpc.simulate(context,source_path,zero["current_action"],zero["future_rule"],zero["local_decision_dates"])
        funded_value = mpc.simulate(context,source_path,funded["current_action"],funded["future_rule"],funded["local_decision_dates"],funding=4)
        # Initial AB wealth 8 becomes E wealth 16; proposed principal 4
        # separately grows to 8, and never enters the real current order.
        self.assertEqual((zero_value["terminal_wealth"],funded_value["terminal_wealth"]),(16.,24.))
        self.assertEqual((zero_value["terminal_wealth"]-8,funded_value["terminal_wealth"]-12),(8.,12.))
        self.assertEqual(zero["current_action"]["buys"],[])
        buy = next(row for row in zero_value["cash_flow_log"] if row["kind"]=="reserve_buy")
        self.assertTrue(buy["hypothetical_future"])
        self.assertGreaterEqual(buy["date"],max(row["date"] for row in zero_value["cash_flow_log"] if row["kind"]=="credit_sale"))
        self.assertEqual(context,original)

    def test_holding_fee_boundaries_use_actual_new_lot_age_and_terminal_wealth(self):
        import datetime as dt
        context = source_context(cash="8")
        context["spec"]["planning"]["primary_goal"] = "redeem"
        context["fee_contracts"]["C"]["trade_precision"]["money_step"] = "0.01"
        context["fee_contracts"]["C"]["redemption"] = {"kind":"holding_tiers","bands":[
            {"minimum":"0","fee":{"kind":"percentage","rate":".015"}},
            {"minimum":"7","fee":{"kind":"percentage","rate":".005"}},
            {"minimum":"30","fee":{"kind":"percentage","rate":".002"}},
            {"minimum":"60","fee":{"kind":"percentage","rate":".001"}},
            {"minimum":"90","fee":{"kind":"percentage","rate":"0"}}]}
        action = next(action for action in allocation.build_source_actions(context)["funding_options"][0]["actions"] if action["buys"] == [{"code":"C","cash_debit":"8"}])
        # Submission at 20:00 prices on the following source open day;
        # the new lot starts on January 2, so exit age equals the horizon.
        for horizon, expected in ((6,7.88),(7,7.96),(29,7.96),(30,7.98),(59,7.98),(60,7.99),(89,7.99),(90,8.)):
            with self.subTest(horizon=horizon):
                dates=[str(dt.date(2030,1,1)+dt.timedelta(days=i)) for i in range(horizon+1)]
                source_path={"id":"analytical","nav":{day:{"C":1.} for day in dates},"distributions":[]}
                value=mpc.simulate(context,source_path,action,HOLD,[dates[0],dates[-1]])
                self.assertAlmostEqual(value["terminal_wealth"],expected)

    def test_ab_source_producer_mpc_public_compiler_never_spends_added_cash(self):
        import copy
        import execution, ledger, newtrade_guard
        from contracts import fingerprint
        from test_single_step import make_terms
        from test_strategy_execution import spec
        context=with_second(source_context(cash="0",shares="4",funding=(0,4)))
        context["snapshot"]["positions"].append({"lot_id":"old-D","code":"D","shares":"4","reserved_shares":"0","acquired_at":"2029-01-01T00:00:00+08:00"})
        context["snapshot"]["equity"]="8"
        context["allocation_codes"]=context["universe"]=["C","D","E"]
        context["purchase_eligible_codes"]=["E"]
        context["fee_contracts"]["E"]=make_terms("E")
        for terms in context["fee_contracts"].values():terms["trade_precision"].update(money_step="1",share_step="1")
        context["identities"]["E"]={"fund_group_id":"fund-E","share_class":"C"}
        context["snapshot"]["prices"]["E"]="1"
        context["market_ref"]["prices"] = copy.deepcopy(context["snapshot"]["prices"])
        context["market_ref"]["price_dates"] = {code:context["as_of"] for code in context["allocation_codes"]}
        context["model_request"]["assets"].append({"code":"E","sellable":True,"buyable":True,"buy_allowed":True,"min_buy":1,"max_weight":1})
        for row in context["model_request"]["assets"]:row.update(subscription_fee=0,settlement_days=2)
        context["spec"]=spec()
        context["spec"]["planning"].update(primary_horizon_days=8,cash_deadline_days=None,cash_required_amount=None)
        context["spec"]["decision"].update(max_current_actions=512,max_policy_count=2048,future_cash_fractions=[1.])
        context["spec"]["trade_policy"]["economic"]["minimum_risk_reduction_amount"]="1"
        context["risk_state"].update(net_principal="8",loss_tolerance=".25",principal_floor="6",remaining_loss_budget="2")
        context["account_id"],context["blocked"],context["trade_family_review_index"]="synthetic-book",False,1
        context["trade_state"]=newtrade_guard.derive_state(ledger.initial_state("CNY"),[],context["decision_at"],context["spec"]["trade_policy"]["economic"]["fee_window_days"],account_id=context["account_id"])
        context["lot_terms"]={row["lot_id"]:{"redemption_fee":"0","redemption_schedule":None} for row in context["snapshot"]["positions"]}
        context["context_hash"]=fingerprint({key:value for key,value in context.items() if key!="context_hash"})
        original=copy.deepcopy(context)
        observed=path(["C","D","E"],lambda code,day:(2. if code=="E" else .5) if day==STAGES[-1] else 1.)
        from test_strategy_execution import capture_engineering_marks
        captured = capture_engineering_marks({"prices":context["snapshot"]["prices"],
            "price_dates":context["market_ref"]["price_dates"],"observed_at":context["decision_at"]})
        marks = captured["known_marks"]
        observed.update(price_schema="source_role_price_paths_v3",known_marks=marks)
        # Two independently named equal-mass analytic leaves preserve complete
        # probability support at each policy's receipt/confirmation decision.
        selection = [{**observed,"id":identity,"probability":.5} for identity in ("analytic-a","analytic-b")]
        paths={"schema":"source_role_price_paths_v3","known_marks":marks,
            "status":"research_ready","scope":"analytic_cashflow_no_source_market_qualification",
            "path_hash":"a"*64,"stage_dates":[STAGES[0],STAGES[-1]],"point_paths":[observed],
            "selection_paths":selection,"calibration_paths":[]}
        family=allocation.build_source_actions(context)
        result=mpc.optimise(context,paths,family)
        self.assertEqual(result["status"],"research_ready",result.get("reason"))
        compiled=execution.compile_mpc_orders({"status":result["status"],"mpc":result,"paths":paths},context,{})
        self.assertEqual(compiled["status"],"ready")
        self.assertEqual({(row["lot_id"],row["share_limit"]) for row in compiled["orders"]},{("old","4"),("old-D","4")})
        self.assertFalse(any(row["side"]=="buy" for row in compiled["orders"]))
        self.assertEqual({row["code"] for row in compiled["redemption_requests"]},{"C","D"})
        self.assertTrue(any(row.get("proposed_contribution")=="4.0" for row in compiled["waiting"]))
        self.assertEqual(context,original)


if __name__ == "__main__":
    unittest.main()
