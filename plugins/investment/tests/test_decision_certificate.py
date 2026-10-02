"""Explicit engineering paths and true calibration; no investment/PIT claim."""
import copy
import datetime as dt
import math
import tempfile
import unittest

import allocation
import execution
import portfolio_mpc as mpc
import verify
from contracts import EvidenceError, fingerprint
from test_allocation import source_context
import test_mpc_contracts as contract_fixtures
from test_portfolio_mpc import path


def risk_case(*, calibration=False, shares="8", cash="0", principal=None, exit_rate="0"):
    context,paths,_=contract_fixtures.FrozenPolicyContractTests().deadline_case()
    source=source_context(cash=cash,shares=shares)
    context["snapshot"]=source["snapshot"]; context["fee_contracts"]=source["fee_contracts"]
    context["fee_contracts"]["C"]["redemption"]["rate"]=exit_rate
    context["account_hash"]=fingerprint({"explicit_engineering_account":source["snapshot"]})
    context["risk_state"]=source["risk_state"]
    context["risk_state"]["valid_until"]=None
    context["lot_terms"]={row["lot_id"]:{"redemption_fee":context["fee_contracts"][row["code"]]["redemption"]["rate"],
        "redemption_schedule":None} for row in context["snapshot"]["positions"]}
    for asset in context["model_request"]["assets"]:
        asset["settlement_days"]=context["fee_contracts"][asset["code"]]["settlement"]["lag_days"]
    if principal is not None:
        context["risk_state"].update(net_principal=str(principal),principal_floor=str(float(principal)*.75),
            remaining_loss_budget=str(float(context["snapshot"]["equity"])-float(principal)*.75))
    context["spec"]["planning"].update(cash_deadline_days=None,cash_required_amount=None)
    context["spec"]["decision"].update(max_current_actions=16,max_policy_count=64)
    # This independently stated two-state mathematical experiment has tail
    # mass .5 and minimum absolute risk improvement 1 monetary unit. Production
    # settings/seed/confidence/error budget remain untouched.
    context["spec"]["allocation"]["tail_probability"]=.5
    context["spec"]["trade_policy"]["economic"]["minimum_risk_reduction_amount"]="1"
    def quote(terminal,identity,probability):
        value=path(["C"],lambda _,day: terminal if day=="2030-01-09" else 1.)
        return {**value,"id":identity,"probability":probability,"price_schema":paths["schema"],"known_marks":paths["known_marks"]}
    paths["point_paths"]=[quote(1.4,"point",1.)]
    paths["selection_paths"]=[quote(.5,"down",.5),quote(2.3,"up",.5)]
    paths["prefix_groups"]={day:[["down","up"]] for day in paths["stage_dates"]}
    if calibration:
        pairs=[]
        for i in range(120):
            day=dt.date(2026,2,1)+dt.timedelta(days=i)
            realized=quote(.99+.0001*math.sin(i*.17),"realized-"+str(i),1.)
            predicted=quote(.99+.0001*math.sin(i*.17)+.00005*math.cos(i*.71),"predicted-"+str(i),1.)
            pairs.append({"origin_at":day.isoformat()+"T20:00:00+08:00",
                "label_available_at":(day+dt.timedelta(days=8)).isoformat()+"T20:00:00+08:00",
                "source_hashes":[fingerprint({"synthetic_source_cash_path":i})],"predicted":predicted,"realized":realized})
        paths["calibration_paths"]=pairs
    context["context_hash"]=fingerprint({key:value for key,value in context.items() if key!="context_hash"})
    paths["path_hash"]=fingerprint({key:value for key,value in paths.items() if key!="path_hash"})
    result=mpc.optimise(context,paths,allocation.build_source_actions(context))
    return context,paths,result


def calculation(result,paths):
    return {"mpc":result,"paths":paths,"orders":{"orders":[]}}


class DecisionCertificateTests(unittest.TestCase):
    def test_terminal_exit_fee_budget_rejects_calibrated_baseline(self):
        context, paths, _ = risk_case(calibration=True, shares="100", exit_rate="0.1")
        context["spec"]["planning"]["primary_goal"] = "redeem"
        context["fee_contracts"]["C"]["trade_precision"].update(money_step="0.01", fee_rounding="unrounded", share_step="100")
        for leaf in [*paths["point_paths"], *paths["selection_paths"]]:
            for day in leaf["nav"]:
                leaf["nav"][day]["C"] = 1.0
        paths["path_hash"] = fingerprint({k: v for k, v in paths.items() if k != "path_hash"})
        def optimise():
            context["context_hash"] = fingerprint({k: v for k, v in context.items() if k != "context_hash"})
            return mpc.optimise(context, paths, allocation.build_source_actions(context))
        wide = optimise()
        baseline = next(p for p in wide["funding_options"][0]["candidates"] if p["mode"] == "baseline")
        self.assertTrue(baseline["eligible"])
        self.assertEqual(baseline["point"]["terminal_wealth"], 90)
        self.assertEqual(baseline["point"]["fees"], 10)
        context["spec"]["trade_policy"]["economic"]["maximum_rolling_fee_amount"] = "1"
        narrow = optimise()
        self.assertIsNone(narrow["selected_policy"])
        baseline = next(p for p in narrow["funding_options"][0]["candidates"] if p["mode"] == "baseline")
        self.assertEqual(baseline["trade_guard"]["rolling_fee_total"], 10)
        self.assertEqual(baseline["trade_guard"]["reasons"], ["registered_rolling_fee_budget_exceeded"])
        self.assertEqual(verify.numerical_invariants(context, calculation(narrow, paths))["decision_certificate"]["status"], "passed")
        baseline["eligible"] = baseline["trade_guard"]["eligible"] = True
        narrow["result_hash"] = fingerprint({k: v for k, v in narrow.items() if k != "result_hash"})
        with self.assertRaisesRegex(EvidenceError, "ordinary risk eligibility"):
            verify.numerical_invariants(context, calculation(narrow, paths))

    @classmethod
    def setUpClass(cls):
        cls.qualified=risk_case(calibration=True)

    def test_infeasible_profitable_hold_cannot_defeat_feasible_half_sale(self):
        context,paths,result=self.qualified
        baseline=next(row for row in result["funding_options"][0]["candidates"] if row["mode"]=="baseline")
        self.assertAlmostEqual(baseline["expected_profit"],3.2)
        self.assertEqual(baseline["absolute_cvar"],4.)
        self.assertFalse(baseline["eligible"])
        self.assertEqual(result["decision_status"],"feasible_selected")
        self.assertEqual(result["current_action"],{"buys":[],"sells":[{"lot_id":"old","shares":"4"}]})
        self.assertEqual(result["selected_policy"]["absolute_cvar"],2.)
        self.assertTrue(result["selected_policy"]["eligible"])
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["decision_certificate"]["status"],"passed")

    def test_correct_money_forged_hold_eligibility_is_rejected(self):
        context,paths,result=copy.deepcopy(self.qualified)
        baseline=next(row for row in result["funding_options"][0]["candidates"] if row["mode"]=="baseline")
        baseline["eligible"]=baseline["trade_guard"]["eligible"]=True
        with self.assertRaisesRegex(EvidenceError,"ordinary risk eligibility"):
            verify.numerical_invariants(context,calculation(result,paths))

    def test_correct_money_wrong_eligible_winner_is_rejected(self):
        context,paths,result=copy.deepcopy(self.qualified)
        alternative=next(row for row in result["funding_options"][0]["candidates"] if row["eligible"] and row["current_action"]["sells"][0]["shares"]=="5")
        result["selected_policy"]=alternative; result["current_action"]=alternative["current_action"]
        result["funding_options"][0]["best"]=result["funding_options"][0]["ordinary_best"]=alternative
        with self.assertRaisesRegex(EvidenceError,"winner violates"):
            verify.numerical_invariants(context,calculation(result,paths))

    def test_missing_candidate_cannot_create_false_finite_set_winner(self):
        context,paths,result=copy.deepcopy(self.qualified)
        result["funding_options"][0]["candidates"].pop()
        with self.assertRaisesRegex(EvidenceError,"candidate disposition completeness"):
            verify.numerical_invariants(context,calculation(result,paths))

    def test_no_calibration_sale_is_only_uncertified_recovery(self):
        context,paths,result=risk_case()
        self.assertEqual(result["decision_status"],"risk_recovery")
        selected=result["selected_policy"]
        self.assertFalse(selected["eligible"]); self.assertTrue(selected["recovery_eligible"])
        self.assertIsNone(selected["trade_guard"]["risk_cvar_upper"])
        self.assertEqual(selected["trade_guard"]["risk_status"],"risk_not_restored")
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["status"],"passed")
        result["result_hash"]=fingerprint({key:value for key,value in result.items() if key!="result_hash"})
        compiled=execution.compile_mpc_orders({"status":"research_ready","mpc":result,"paths":paths},context,{})
        self.assertEqual(compiled["selected_plan_kind"],"risk_recovery")
        self.assertEqual(compiled["risk_status"],"risk_not_restored")
        self.assertTrue(all(row["side"]=="sell" for row in compiled["orders"]))

    def test_negative_budget_cannot_be_certified_restored(self):
        context,paths,result=risk_case(principal=20)
        self.assertEqual(result["decision_status"],"risk_recovery")
        self.assertGreater(result["selected_policy"]["risk_violation_amount"],0)
        self.assertFalse(any(row["eligible"] for row in result["funding_options"][0]["candidates"]))
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["status"],"passed")

    def test_source_pure_cash_has_identity_risk_without_historical_calibration(self):
        context,paths,result=risk_case(shares="0",cash="8")
        self.assertEqual(result["decision_status"],"feasible_selected")
        self.assertEqual(result["selected_policy"]["mode"],"baseline")
        self.assertEqual(result["selected_policy"]["trade_guard"]["risk_evidence_basis"],"source_cash_identity")
        self.assertEqual(result["selected_policy"]["absolute_cvar"],0.)
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["status"],"passed")

    def test_decimal_optional_cash_identity_preserves_confirmed_principal(self):
        context,paths,_=risk_case(shares="0",cash=".1")
        context["model_request"]["allocation_policy"]["funding_levels"]=[0,.2]
        context["context_hash"]=fingerprint({key:value for key,value in context.items() if key!="context_hash"})
        result=mpc.optimise(context,paths,allocation.build_source_actions(context))
        additional=next(row for row in result["funding_options"] if row["proposed_contribution"]==.2)
        self.assertEqual(additional["best"]["point"]["terminal_wealth"],.3)
        self.assertEqual(additional["best"]["trade_guard"]["risk_evidence_basis"],"source_cash_identity")
        self.assertEqual(result["selected_policy"]["proposed_contribution"],0)
        self.assertEqual(context["snapshot"]["cash"],".1")
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["status"],"passed")

    def test_CVaR_constant_normalization_covers_gains_and_original_support(self):
        for constant in (.0791131324163489,-.0791131324163489,0.):
            for tail in (.5,.05,1.):
                self.assertEqual(mpc.tail_mean([constant]*11,[1/11]*11,tail),constant)
        for probabilities,tail in (([0.]*11,.5),([-1/11]*11,.5),([1/11]*11,0.),([1/11]*11,1.1)):
            with self.assertRaises(ValueError):
                mpc.tail_mean([.0791131324163489]*11,probabilities,tail)
        for constant in (math.inf,math.nan):
            with self.assertRaises(ValueError):
                mpc.tail_mean([constant]*11,[1/11]*11,.5)

    def test_equal_source_wealth_with_more_information_nodes_has_equal_risk_order(self):
        for count in (11,49):
            with self.subTest(path_count=count):
                self.source_equal_wealth_order(count)

    def source_equal_wealth_order(self,count):
        from portfolio_paths import information_groups
        context,paths,_=risk_case(cash="8",shares="0")
        context["purchase_eligible_codes"]=["C"]
        context["context_hash"]=fingerprint({key:value for key,value in context.items() if key!="context_hash"})
        paths["selection_paths"]=[]
        for index in range(count):
            terminal=.99+.007*math.sin(index*.37)
            leaf=path(["C"],lambda _,day,terminal=terminal:terminal if day=="2030-01-09" else 1.)
            leaf.update(id=str(index),probability=1/count,price_schema=paths["schema"],known_marks=paths["known_marks"])
            paths["selection_paths"].append(leaf)
        paths["prefix_groups"]=information_groups(paths["selection_paths"],paths["stage_dates"])
        comparison=allocation.build_source_actions(context)
        result=mpc.optimise(context,paths,comparison)
        candidates=result["funding_options"][0]["candidates"]
        equal_wealth={}
        for candidate in candidates:
            key=tuple(value["terminal_wealth"] for value in candidate["selection"])
            equal_wealth.setdefault(key,[]).append(candidate)
        equivalent=next(rows for rows in equal_wealth.values() if len({len(row["local_decision_dates"]) for row in rows})>1
            and any(row["absolute_cvar"]>0 for row in rows))
        self.assertEqual(len({row["absolute_cvar"] for row in equivalent}),1)
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["decision_certificate"]["status"],"passed")
        self.assertEqual(mpc.validate_result(result,{"context":context,"paths":paths,"comparison":comparison})["status"],"passed")

    def test_common_information_CVaR_is_original_PMF_independent_of_depth_matrix(self):
        # Same random variable/distribution under every trivial information
        # node; exact normalization follows from CVaR(c)=c, not a tolerance.
        for count in (2,3,7,11,17,31,49,50,97,129):
            weights=([1/count]*count,[(i+1)/(count*(count+1)/2) for i in range(count)])
            for probabilities in weights:
                paths=[{"id":str(i),"probability":p} for i,p in enumerate(probabilities)]
                for offset in (0.,-.2):
                    losses=[offset+.07+.005*math.sin(i*.17) for i in range(count)]
                    for tail in (.05,.5,1.):
                        expected=mpc.tail_mean(losses,probabilities,tail)
                        for depth in (0,1,2,5):
                            stages=["2030-01-01"]+[f"2030-01-{i+2:02}" for i in range(depth)]+["2030-01-09"]
                            groups={day:[[row["id"] for row in paths]] for day in stages}
                            actual,_=mpc.nested_tail(losses,paths,groups,stages,tail)
                            with self.subTest(count=count,tail=tail,depth=depth,offset=offset):
                                self.assertEqual(actual,expected)

    def test_nested_risk_checks_original_probability_before_conditioning(self):
        groups={"2030-01-02":[["0","1"],["2","3"]]}
        stages=["2030-01-01","2030-01-02","2030-01-09"]
        for probabilities in ([.5]*4,[.25,.25,-.25,.75],[math.nan,.25,.25,.25]):
            paths=[{"id":str(i),"probability":p} for i,p in enumerate(probabilities)]
            with self.assertRaises(ValueError):
                mpc.nested_tail([.1]*4,paths,groups,stages,.5)
        paths=[{"id":"same","probability":.5} for _ in range(2)]
        with self.assertRaises(ValueError):
            mpc.nested_tail([.1]*2,paths,{},[stages[0],stages[-1]],.5)

    def test_all_policies_above_budget_are_recovery_not_ordinary(self):
        # Fixture money precision is one unit: 8*.4 rounds to fee3,
        # so even full redemption retains a three-unit loss above budget2.
        context,paths,result=risk_case(exit_rate=".4")
        self.assertEqual(result["decision_status"],"risk_recovery")
        self.assertFalse(any(row["eligible"] for row in result["funding_options"][0]["candidates"]))
        self.assertGreater(result["selected_policy"]["absolute_cvar"],2.)
        self.assertEqual(result["selected_policy"]["trade_guard"]["risk_status"],"risk_not_restored")
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["status"],"passed")

    def test_negative_budget_cash_has_no_fabricated_recovery(self):
        context,paths,result=risk_case(shares="0",cash="8",principal=20)
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["decision_status"],"risk_unresolved")
        self.assertIsNone(result["selected_policy"])
        self.assertEqual(result["current_action"],{"buys":[],"sells":[]})
        self.assertEqual(verify.numerical_invariants(context,calculation(result,paths))["status"],"passed")

    def test_missing_baseline_source_scope_cannot_publish_partial_comparison(self):
        context,paths,_=risk_case()
        # Two source lots declare 5*5=25 combinations, including rejected
        # choices. Register enough engineering computation to reach valuation.
        context["spec"]["decision"]["max_current_actions"]=64
        context["spec"]["planning"]["primary_goal"]="redeem"
        context["snapshot"]["positions"][0]["shares"]="4"
        context["snapshot"]["positions"].append({**context["snapshot"]["positions"][0],"lot_id":"second"})
        context["fee_contracts"]["C"]["redemption_allocation"]={"method":"specific_lot"}
        # Two held lots lack the original source multi-lot fee application.
        # Redeeming either whole lot has a legal one-lot terminal valuation;
        # the required hold comparator itself has unproved two-lot fees.
        context["context_hash"]=fingerprint({key:value for key,value in context.items() if key!="context_hash"})
        comparison=allocation.build_source_actions(context)
        self.assertEqual(comparison["status"],"frozen")
        result=mpc.optimise(context,paths,comparison)
        self.assertEqual(result["status"],"partial")
        self.assertIsNone(result["selected_policy"])
        self.assertEqual(result["funding_options"][0]["candidates"],[])
        self.assertTrue(any("source_comparison_baseline_unavailable" in value for value in result["source_invalid_policies"].values()))
        self.assertEqual(mpc.validate_result(result,{"context":context,"paths":paths,"comparison":comparison})["status"],"passed")

    def test_family_certificate_prioritises_ordinary_and_rejects_wrong_winner(self):
        from artifacts import Artifacts
        from pathlib import Path
        from pipeline import select_family
        from decision_validation import family_certificate
        with tempfile.TemporaryDirectory(prefix="investment-family-certificate-") as directory:
            artifacts=Artifacts(Path(directory)); rows=[]
            for identity,values in (("ordinary",self.qualified),("recovery",risk_case())):
                context,paths,result=values
                value={"status":result["status"],"mpc":result,"paths":paths,"orders":{"status":"no_action","orders":[]}}
                rows.append({"family":{"family_id":identity},"context_ref":artifacts.put_json(context),"calculation_ref":artifacts.put_json(value)})
            selected=select_family(rows,artifacts)
            self.assertEqual(selected["selected_family_id"],"ordinary")
            self.assertEqual(family_certificate(rows,artifacts,selected)["status"],"passed")
            selected["ranked"].reverse(); selected["selected_family_id"]="recovery"
            with self.assertRaisesRegex(EvidenceError,"family winner ordering"):
                family_certificate(rows,artifacts,selected)


if __name__=="__main__":
    unittest.main()
