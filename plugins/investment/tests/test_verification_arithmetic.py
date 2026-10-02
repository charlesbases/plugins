"""Independent hand-calculated amounts; fixtures are explicitly synthetic."""
import copy
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import verify
from artifacts import Artifacts
from contracts import ContractError, EvidenceError, fingerprint


def hand_case(pricing=(60., 60.)):
    from decimal import Decimal
    import portfolio_mpc
    import fee_contract
    from test_single_step import make_context
    from test_portfolio_mpc import with_second, path, STAGES, HOLD
    context = with_second(make_context(cash="100", shares="10"))
    context["account_id"] = "analytical"
    context["snapshot"].update(equity="200", prices={"C":"10", "D":"60"})
    context["market_ref"].update(prices={"C":"10", "D":"60"}, price_dates={"C":"2030-01-01","D":"2030-01-01"})
    context["risk_state"] = {"remaining_loss_budget":"10", "loss_tolerance":"0.25"}
    context["spec"].update(kind="numeric_policy", currency="CNY", execution={"money_step":"0.01","share_step":"0.5"})
    context["spec"]["planning"]["primary_horizon_days"] = 8
    context["blocked"] = False
    for asset in context["model_request"]["assets"]:
        asset.update(subscription_fee=0, settlement_days=2, max_buy=1000)
    context["context_hash"] = fingerprint(context)
    action = {"buys":[{"code":"D", "cash_debit":"60"}], "sells":[]}
    scenarios = []
    for index, (old_terminal, new_terminal) in enumerate(((5.,90.),(15.,54.))):
        def prices(code, day, i=index, old=old_terminal, new=new_terminal):
            if day == STAGES[-1]:
                return old if code == "C" else new
            return 10. if code == "C" else (60. if day == STAGES[0] else pricing[i])
        scenario = path(["C","D"],prices)
        scenario.update(id=str(index), probability=.5, origin_at="2026-0"+str(index+1)+"-01T20:00:00+08:00")
        scenarios.append(scenario)
    point = path(["C","D"],lambda code,day: (10. if code=="C" else 72.) if day==STAGES[-1] else (10. if code=="C" else 60.))
    point_value = portfolio_mpc.simulate(context,point,action,HOLD,STAGES)
    values = [portfolio_mpc.simulate(context,scenario,action,HOLD,STAGES) for scenario in scenarios]
    profits = [value["terminal_wealth"]-200 for value in values]
    mean = sum(.5*profit for profit in profits)
    risk = portfolio_mpc.tail_mean([-profit for profit in profits],[.5,.5],.5)
    paths = {"status":"research_ready","selection_paths":scenarios,"point_paths":[point],"stage_dates":STAGES,
        "prefix_groups":{day:[[row["id"] for row in scenarios]] for day in STAGES}}
    paths["path_hash"] = fingerprint(paths)
    import single_step_wealth
    distribution = portfolio_mpc.as_current_order_distribution(context, paths)
    projection = single_step_wealth.project(context, action, distribution)
    candidate = {"id":"analytical-policy","proposed_contribution":0,"mode":"rolling_policy","current_action":action,
        "future_rule":HOLD,"point":point_value,"selection":values,"current_projection":projection,
        "point_profit":point_value["terminal_wealth"]-200,"expected_profit":mean,"expected_net_return":mean/200,
        "absolute_cvar":risk,"cvar_loss_fraction":risk/200,"eligible":False,
        "trade_guard":{"eligible":False,"status":"no_action","risk_budget":10.,"risk_cvar_upper":risk,
            "reasons":["principal_risk_budget_exceeded"]}}
    calculation = {"status":"research_ready","mpc":{"context_hash":context["context_hash"],
        "funding_options":[{"proposed_contribution":0,"candidates":[candidate]}],"selected_policy":None},
        "paths":paths,
        "orders":{"orders":[]}}
    return context,calculation

def source_case():
    # Three independently stated source NAVs, no dividends, exact zero residual.
    target = {"pricing": 1.1, "terminal_nav": 1.2, "hold": 1.2, "buy": 12/11, "sell": 1.1}
    latent = {"log_pricing": math.log(1.1), "log_terminal_nav": math.log(1.2),
              "sqrt_cash_neither": 0., "sqrt_cash_sale_only": 0., "sqrt_cash_buy_only": 0., "sqrt_cash_both": 0.}
    source = {"code": "B", "base_date": "2030-01-01", "base_nav": 10., "pricing_date": "2030-01-02",
        "pricing_nav": 11., "ownership_date": "2030-01-02", "confirmation_date": "2030-01-02",
        "end_date": "2030-01-07", "terminal_nav": 12., "dividends": [], "targets": target,
        "latent_targets": latent, "label_available": "2030-01-08", "feature_cutoff_date": "2030-01-01",
        "clock": {"after_cutoff": False, "confirmation_rule": {"lag_days": 0, "day_basis": "calendar_days"},
                  "ownership_start": "execution_date"}}
    source["source_hash"] = fingerprint(source)
    record = {"date": "2030-01-02", "origin_at": "2030-01-02T09:00:00+08:00", "codes": ["B"],
        "label_available": "2030-01-08", "end_date": "2030-01-07", "source_labels": {"B": source},
        "source_hashes": {"B": source["source_hash"]}, "training_audit": {"max_label_available_date": "2030-01-01"},
        "predicted_latents": {name: [value] for name, value in latent.items()},
        "realized_latents": {name: [value] for name, value in latent.items()},
        "latent_errors": {name: [0.] for name in latent},
        "predicted_log_targets": {name: [math.log(value)] for name, value in target.items()},
        "realized_targets": {name: [value] for name, value in target.items()},
        "log_target_errors": {name: [0.] for name in target}}
    # A separate calibration origin follows the selection outcome's maturity.
    calibration_target = {"pricing": 14/13, "terminal_nav": 15/13, "hold": 15/13, "buy": 15/14, "sell": 14/13}
    calibration_latent = {**dict.fromkeys(latent, 0.), "log_pricing": math.log(14/13), "log_terminal_nav": math.log(15/13)}
    calibration_source = copy.deepcopy(source)
    calibration_source.update(base_date="2030-01-08", base_nav=13., pricing_date="2030-01-09", pricing_nav=14.,
        ownership_date="2030-01-09", confirmation_date="2030-01-09", end_date="2030-01-14", terminal_nav=15.,
        targets=calibration_target, latent_targets=calibration_latent, label_available="2030-01-15", feature_cutoff_date="2030-01-08")
    calibration_source["source_hash"] = fingerprint({key: value for key, value in calibration_source.items() if key != "source_hash"})
    calibration_record = {**copy.deepcopy(record), "date": "2030-01-09", "origin_at": "2030-01-09T09:00:00+08:00",
        "label_available": "2030-01-15", "end_date": "2030-01-14", "source_labels": {"B": calibration_source},
        "source_hashes": {"B": calibration_source["source_hash"]}, "training_audit": {"max_label_available_date": "2030-01-08"},
        "predicted_latents": {name: [value] for name, value in calibration_latent.items()},
        "realized_latents": {name: [value] for name, value in calibration_latent.items()},
        "predicted_log_targets": {name: [math.log(value)] for name, value in calibration_target.items()},
        "realized_targets": {name: [value] for name, value in calibration_target.items()}}
    fitting = {"forecasts": [{"code": "B", "feature_cutoff_date": "2030-01-01", "predicted_latents": latent}],
        "joint_scenarios": {"codes": ["B"], "dates": [record["date"]], "source_oos": [record, calibration_record],
            "source_selection_oos": [record], "source_calibration_oos": [calibration_record],
            "targets": {name: [[value]] for name, value in target.items()},
            "current_point_targets": {name: [[value]] for name, value in target.items()}}}
    context = {"as_of": "2030-01-17", "market_ref": {"prices": {"B": "10"}, "price_dates": {"B": "2030-01-01"}},
        "spec": {"planning": {"primary_horizon_days": 5}, "availability": {"nav_lag_calendar_days": 1}}}
    data = {"nav": {"B": [{"date": day, "nav": price, "distribution_per_share": 0., "corporate_actions": []}
        for day, price in (("2030-01-01", 10.), ("2030-01-02", 11.), ("2030-01-07", 12.),
                           ("2030-01-08", 13.), ("2030-01-09", 14.), ("2030-01-14", 15.))]},
        "features": {"B": [{"feature_cutoff_nav_date": "2030-01-01"}, {"feature_cutoff_nav_date": "2030-01-08"}]}}
    return context, fitting, data


def assert_source_number(actual, expected, label, scale):
    if not math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-10):
        raise EvidenceError(label)


class ArithmeticGateTests(unittest.TestCase):
    def test_decimal_capital_and_manual_tail_loss_twenty(self):
        context, calculation = hand_case()
        policy = calculation["mpc"]["funding_options"][0]["candidates"][0]
        # Independent accounting: 40 cash + 10*C5 + 1*D90 = 180;
        # other path: 40 + 10*C15 + 1*D54 = 244. Equal masses give profit12.
        self.assertEqual([row["terminal_wealth"] for row in policy["selection"]], [180., 244.])
        self.assertEqual((policy["expected_profit"], policy["absolute_cvar"]), (12., 20.))
        self.assertEqual(verify.numerical_invariants(context, calculation)["status"], "passed")

    def test_continuous_value_cannot_replace_whole_share_holdings(self):
        context, calculation = hand_case()
        calculation["mpc"]["funding_options"][0]["candidates"][0]["current_projection"]["values"]["D"] = "99.99"
        with self.assertRaisesRegex(EvidenceError, "current whole-share source NAV value"):
            verify.numerical_invariants(context, calculation)

    def test_stale_report_fraction_and_false_eligibility_are_rejected(self):
        for field, value, expected in (("cvar_loss_fraction", .000025, "CVaR capital fraction"), ("eligible", True, "eligibility")):
            context, calculation = hand_case()
            calculation["mpc"]["funding_options"][0]["candidates"][0][field] = value
            with self.assertRaisesRegex(EvidenceError, expected):
                verify.numerical_invariants(context, calculation)

    def test_path_valuation_keeps_probability_mass_and_unknown_support(self):
        for probabilities in ([1.], [.5, .4], [-.5, 1.5], [float("nan"), .5]):
            context, calculation = hand_case()
            for row, probability in zip(calculation["paths"]["selection_paths"], probabilities):
                row["probability"] = probability
            if len(probabilities) == 1:
                calculation["paths"]["selection_paths"] = calculation["paths"]["selection_paths"][:1]
            with self.assertRaisesRegex(EvidenceError, "probability mass|finite path inventory"):
                verify.numerical_invariants(context, calculation)
        for value in (float("nan"), 13.):
            context, calculation = hand_case()
            calculation["mpc"]["funding_options"][0]["candidates"][0]["expected_profit"] = value
            with self.assertRaisesRegex(EvidenceError, "afterfee profit"):
                verify.numerical_invariants(context, calculation)
        context, calculation = hand_case()
        value = calculation["mpc"]["funding_options"][0]["candidates"][0]["selection"][0]
        value["terminal_wealth"] = 181.
        # Self-hash repair must not bypass source NAV/share/cash arithmetic.
        value["valuation_hash"] = fingerprint({k:v for k,v in value.items() if k != "valuation_hash"})
        with self.assertRaisesRegex(EvidenceError, "terminal NAV/cash/receivable wealth"):
            verify.numerical_invariants(context, calculation)
        context, calculation = hand_case()
        calculation["paths"]["status"] = "partial"
        with self.assertRaisesRegex(EvidenceError, "Unqualified paths"):
            verify.numerical_invariants(context, calculation)

    def test_actual_pricing_changes_new_shares_and_wealth(self):
        context, calculation = hand_case(pricing=(120., 30.))
        policy = calculation["mpc"]["funding_options"][0]["candidates"][0]
        values = policy["selection"]
        buys = [next(row for row in value["cash_flow_log"] if row["kind"] == "price_buy") for value in values]
        from decimal import Decimal
        self.assertEqual([Decimal(row["shares"]) for row in buys], [Decimal("0.5"), Decimal("2")])
        self.assertEqual([value["terminal_wealth"] for value in values], [135., 298.])
        self.assertEqual((policy["expected_profit"], policy["absolute_cvar"]), (16.5, 65.))
        self.assertEqual(verify.numerical_invariants(context, calculation)["status"], "passed")
        buys[0]["nav"] = "60"
        values[0]["valuation_hash"] = fingerprint({k:v for k,v in values[0].items() if k != "valuation_hash"})
        with self.assertRaisesRegex(EvidenceError, "Source execution NAV changed"):
            verify.numerical_invariants(context, calculation)

    def test_joint_sources_recompute_NAV_even_after_self_hash_repair(self):
        context, fitting, data = source_case()
        self.assertEqual(verify._joint_source_invariants(context, fitting, data, assert_source_number), 2)
        record = fitting["joint_scenarios"]["source_oos"][0]
        source = record["source_labels"]["B"]
        source["pricing_nav"] = 10.
        source["source_hash"] = fingerprint({key: value for key, value in source.items() if key != "source_hash"})
        record["source_hashes"]["B"] = source["source_hash"]
        with self.assertRaisesRegex(EvidenceError, "source NAV pricing_nav"):
            verify._joint_source_invariants(context, fitting, data, assert_source_number)

    def test_joint_sources_reject_future_training_and_wrong_point_decoder(self):
        context, fitting, data = source_case()
        fitting["joint_scenarios"]["source_oos"][0]["training_audit"]["max_label_available_date"] = "2030-01-03"
        with self.assertRaisesRegex(EvidenceError, "future information"):
            verify._joint_source_invariants(context, fitting, data, assert_source_number)
        context, fitting, data = source_case()
        fitting["joint_scenarios"]["current_point_targets"]["buy"][0][0] = 1.2
        with self.assertRaisesRegex(EvidenceError, "current point target"):
            verify._joint_source_invariants(context, fitting, data, assert_source_number)

    def test_calibration_recomputes_point_optimism_amount(self):
        import portfolio_mpc, mpc_calibration
        from test_portfolio_mpc import path, STAGES, HOLD, EMPTY
        from test_mpc_calibration import panel
        context, calculation = hand_case()
        import allocation
        from test_strategy_execution import capture_engineering_marks
        context["spec"]["decision"] = {"max_current_actions":256,"max_policy_count":2048,"max_path_stages":8,"future_cash_fractions":[1.]}
        context["model_request"]["allocation_policy"] = {"funding_levels":[0]}
        # This independent experiment's published minimum is 60 cash units;
        # at the stated D60 dealing price it buys exactly one share.
        next(row for row in context["model_request"]["assets"] if row["code"]=="D")["min_buy"] = 60
        context["fee_contracts"]["D"]["min_buy"] = "60"
        frozen = portfolio_mpc.freeze_policies(context,allocation.build_source_actions(context),max_current_actions=256,cash_fractions=[1.])
        group = frozen["groups"][0]
        policies = group["policies"]
        policy = next(row for row in policies if row["current_action"]=={"buys":[{"code":"D","cash_debit":"60"}],"sells":[]} and row["future_rule"]==HOLD)
        baseline = next(row for row in policies if row["mode"]=="baseline")
        # Full analytical paths, not interpolated endpoints. D60 buys one share.
        predicted = path(["C","D"],lambda code,day: 10. if code=="C" else (90. if day==STAGES[-1] else 60.))
        realized = path(["C","D"],lambda code,day: 10. if code=="C" else (72. if day==STAGES[-1] else 60.))
        pair = {"origin_at":"2026-01-01T20:00:00+08:00", "label_available_at":"2026-01-10T20:00:00+08:00",
            "source_hashes":[fingerprint(predicted),fingerprint(realized)],
            "predicted":predicted,"realized":realized}
        settings = panel()[3]
        context["spec"]["trade_policy"] = copy.deepcopy(settings["spec"]["trade_policy"])
        context["spec"]["trade_policy"]["sample_window"] = {"start_date":"2026-01-01","end_date":"2026-01-01"}
        context["spec"]["training"] = {"min_joint_dates":20}
        context["trade_family_review_index"] = 1
        marks = capture_engineering_marks({**context["market_ref"],"observed_at":context["decision_at"]})["known_marks"]
        paths = {"schema":"source_role_price_paths_v3","known_marks":marks,"stage_dates":STAGES,"calibration_paths":[pair]}
        errors,losses,dates,records,invalid = portfolio_mpc._calibration_matrix(context,paths,policies,{"0.0":baseline})
        record = records[policy["id"]][0]
        self.assertEqual(invalid,{})
        self.assertEqual((record["predicted_gain"],record["realized_gain"],record["optimism"]),(30.,12.,18.))
        origins = [{"date":dates[0],"origin_at":pair["origin_at"],"outcome_available_at":pair["label_available_at"],
            "source_maturity_hash":fingerprint(pair["source_hashes"]),
            "simulation_hash":fingerprint({key:records[key][0] for key in sorted(records)})}]
        context.update(matrix_source_hash=fingerprint(records),mpc_matrix_records=records,
            mpc_calibration_lineage={"origins":origins,"policies":policies,"capital_amount":"200"})
        kwargs = {"frozen_family_hash":fingerprint(policies),"total_family_count":1,"policy_count_bound":len(policies),
            "funding_registry":frozen["funding_registry"],"funding_registry_hash":frozen["funding_registry_hash"],
            "funding_group_id":group["funding_group_id"]}
        result = mpc_calibration.calibrate(errors,losses,dates,context,**kwargs)
        self.assertEqual(result["reason"],"too_few_mature_original_paired_dates")
        # Repair all self-hashes and the error vector: source gains30-12 still
        # require18, so the independent accounting identity must reject zero.
        record["optimism"] = errors[policy["id"]][0] = 0.
        context["matrix_source_hash"] = fingerprint(records)
        origins[0]["simulation_hash"] = fingerprint({key:records[key][0] for key in sorted(records)})
        with self.assertRaisesRegex(ContractError,"original policy cash-flow simulation"):
            mpc_calibration.calibrate(errors,losses,dates,context,**kwargs)

    def test_report_distinguishes_point_guard_and_holding_age_costs(self):
        from report import summarize, render
        candidate = {"id": "current-policy", "proposed_contribution": 0, "expected_net_return": .02,
            "expected_profit": 2., "absolute_cvar": 5., "cvar_loss_fraction": .05,
            "current_projection": {"fees": 1.}, "point": {"terminal_wealth": 111., "fees": 1.}, "eligible": True,
            "future_rule": {"weights": {}}, "conditional_lower_bound": 5.,
            "trade_guard": {"status": "passed", "reasons": []}}
        calculation = {"mpc": {"selected_policy": candidate,
            "funding_options": [{"candidates": [candidate]}], "calibration": {"status": "calibrated", "reason": "source_OOS"}},
            "orders": {"selected_candidate_id": "single", "trade_guard": candidate["trade_guard"]},
            "product_cost_comparison": {"reference_amount": "100", "products": [{"code": "B", "horizons": [
                {"holding_days": age, "entry_fee": "0", "exit_cost": "1", "reference_net_wealth_lower": 99.}
                for age in (7, 30, 60, 90)]}]}}
        summary = summarize(calculation, {"spec": {"planning": {"primary_horizon_days": 30}}})
        self.assertNotIn("conditional_switches", summary)
        bundle = {"decision_at": "2030-01-01T00:00:00Z", "account_id": "main", "account_recorded_at": None,
            "market_price_dates": {}, "action_analysis": {"actions": []}, "status": "conditional_research", "news": {},
            "decision_id": "synthetic", "bundle_hash": "a"*64, "qualification": {"reason": "synthetic conditional research"},
            "comparison_summary": summary, "orders": {"orders": [], "waiting": [], "funding_options": []}}
        rendered = render(bundle)
        self.assertIn("计费持有日", rendered)
        self.assertIn("| B | 7 |", rendered)
        self.assertIn("情景均值", rendered)
        self.assertIn("点预测净优势", rendered)
        self.assertIn("不是未来盈利置信区间", rendered)
        self.assertNotIn("scenario_rows", rendered)
        self.assertEqual(verify.verify_report(bundle, rendered)["status"], "passed")
        candidate.update(eligible=False, expected_net_return=.99, trade_guard={"status": "needs_calibration", "reasons": ["source_path_pricing_unavailable"]})
        calculation["mpc"]["selected_policy"] = None
        bundle["comparison_summary"] = summarize(calculation, {"spec": {"planning": {"primary_horizon_days": 30}}})
        bundle["orders"]["funding_options"] = [{"proposed_contribution": 100., "best": None, "candidates": [candidate]}]
        blocked_report = render(bundle)
        self.assertIn("尚无已验证可选方案；可能需要补齐来源或风险证据", blocked_report)
        self.assertNotIn("99.00%", blocked_report)

    def test_report_sale_uses_rounded_gross_at_tier_boundary(self):
        from test_numerical_schema4 import terms
        from report import explain_actions
        from portfolio_mpc import reference_valuation
        import execution
        contract = terms("B", step="0.00000001")
        contract.update(order_cutoff_local="15:00:00", confirmation={"lag_days": 1, "day_basis": "trading_days"})
        contract["redemption"] = {"kind": "amount_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.01"}},
            {"minimum": "1000", "fee": {"kind": "fixed", "amount": "1"}}]}
        with tempfile.TemporaryDirectory() as folder:
            objects = Artifacts(Path(folder))
            state = {"lots": {"lot": {"code": "B", "shares": "333.33333333", "acquired_at": "2020-01-01T00:00:00Z"}}}
            context = {"snapshot": {"available_cash": "0", "equity": "1000", "positions": [
                    {**state["lots"]["lot"], "lot_id": "lot", "reserved_shares": "0"}], "prices": {"B": "3"}},
                "market_ref": {"prices": {"B": "3"}, "price_dates": {"B": "2030-01-01"}},
                "decision_at": "2030-01-01T00:00:00Z", "fee_contracts": {"B": contract},
                "purchase_eligible_codes": [], "model_request": {"assets": [{"code": "B", "sellable": True}]}}
            bundle = {"context": context, "account_state_ref": objects.put_json(state),
                "calculation_ref": objects.put_json({"comparison": {}}), "selection_ref": None,
                "orders": {"orders": [{"order_id": "sell", "code": "B", "side": "sell", "lot_id": "lot", "share_limit": "333.33333333"}]}}
            bundle["orders"]["reference_valuation"] = reference_valuation(context,
                {"buys": [], "sells": [{"lot_id": "lot", "shares": "333.33333333"}]})
            bundle["orders"]["redemption_requests"] = execution._redemption_requests(bundle["orders"]["orders"])
            row = explain_actions(bundle, objects)["actions"][0]
            self.assertEqual(float(row["estimated_fee"]), 1.)
            self.assertEqual(float(row["funding"]["estimated_receivable"]), 999.)


if __name__ == "__main__":
    unittest.main()
