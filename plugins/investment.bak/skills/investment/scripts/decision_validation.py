"""Independent finite-policy feasibility and selection certificate.

CVaR uses the Rockafellar--Uryasev variational expression. This certificate
checks a registered finite set, not global optimality or future profitability.
The calibrated statistical bounds are source inputs reconstructed separately;
this module never calls the producer's eligibility or sorting implementation.
"""
import math
from decimal import Decimal

from contracts import EvidenceError, fingerprint, instant
import cash_reference
import risk_numbers


def check(condition, message):
    if not condition:
        raise EvidenceError("Decision certificate: "+message)


def number(actual, expected, label):
    check(math.isfinite(float(actual)) and math.isclose(float(actual),float(expected),rel_tol=1e-10,abs_tol=1e-8),label)


def _published(actual, expected, label):
    """The displayed risk is exactly the boundary conversion of our result."""
    check(not isinstance(actual,bool) and risk_numbers.number(actual)==risk_numbers.number(float(expected)),label)


def _variational_tail(losses, probabilities, tail):
    """Independent RU minimization; does not use the producer's tail algorithm."""
    tail = risk_numbers.number(tail)
    check(losses and len(losses)==len(probabilities) and 0<tail<=1,"CVaR variational domain")
    return min(z+sum(p*max(loss-z,0) for p,loss in zip(probabilities,losses))/tail for z in losses)


def exposure(context, policy, capital):
    values = {code: Decimal(value) for code,value in policy["current_projection"]["values"].items()}
    assets = {row["code"]:row for row in context["model_request"]["assets"]}
    good = all(value <= Decimal(str(assets[code]["max_weight"]))*Decimal(str(capital)) for code,value in values.items())
    bounds = context["spec"]["constraints"]
    groups = {}
    for code,value in values.items():
        group = context["identities"][code]["fund_group_id"]
        groups[group] = groups.get(group,Decimal(0))+value
    for group,limit in bounds["fund_group_limits"].items():
        good = good and groups.get(group,Decimal(0)) <= Decimal(str(capital))*Decimal(str(limit))+Decimal("0.0000001")
    for sector,limit in bounds["sector_limits"].items():
        total = Decimal(0)
        for code,value in values.items():
            upper = context.get("sector_exposure_bounds",{}).get(code,{}).get(sector,{}).get("upper")
            if upper is None:
                good = good and value == 0
            else:
                check(math.isfinite(float(upper)) and float(upper)>=0,"invalid source sector bound")
                total += value*Decimal(str(upper))
        good = good and total <= Decimal(str(capital))*Decimal(str(limit))+Decimal("0.0000001")
    check(policy["current_projection"]["concentration_feasible"] == good,"exposure eligibility differs from source limits")
    return good


def source_action(context, policy):
    """Check current precision, unlocked lots and real cash without allocation."""
    action = policy["current_action"]
    check(set(action)=={"buys","sells"},"noncanonical current action")
    lots = {row["lot_id"]:row for row in context["snapshot"]["positions"]}
    assets = {row["code"]:row for row in context["model_request"]["assets"]}
    sold,seen = set(),set()
    for row in action["sells"]:
        check(row["lot_id"] in lots and row["lot_id"] not in seen,"unknown or duplicate sale lot")
        seen.add(row["lot_id"])
        lot = lots[row["lot_id"]]; code=lot["code"]; terms=context["fee_contracts"][code]
        quantity=Decimal(row["shares"]); step=Decimal(terms["trade_precision"]["share_step"])
        check(0 < quantity <= Decimal(lot["shares"])-Decimal(lot["reserved_shares"]) and quantity%step==0,"sale precision or inventory")
        check(assets[code]["sellable"] and terms["sellable"],"redemption source status")
        check(cash_reference.holding_age(terms,lot["acquired_at"],cash_reference.local_date(context["decision_at"]))>=terms["holding"]["minimum_days"],"sale locked by source")
        sold.add(code)
    available=Decimal(context["snapshot"]["available_cash"])+Decimal(str(policy["proposed_contribution"]))
    seen=set()
    for row in action["buys"]:
        code=row["code"]; terms=context["fee_contracts"][code]; asset=assets[code]
        check(code not in seen and code not in sold and code in context["purchase_eligible_codes"],"current purchase identity")
        seen.add(code); debit=Decimal(row["cash_debit"]); step=Decimal(terms["trade_precision"]["money_step"])
        check(debit>0 and debit%step==0 and debit<=available,"current purchase spends unavailable cash")
        check(terms["buyable"] and asset["buyable"] and asset["buy_allowed"] and debit>=Decimal(str(asset["min_buy"])),"current source purchase minimum/status")
        check(asset.get("max_buy") is None or debit<=Decimal(str(asset["max_buy"])),"current source purchase cap")
        available-=debit


def decision_certificate(context, paths, result, *, _qualification_diagnostic=False):
    if result.get("independent_preselection_enforced") is True and not _qualification_diagnostic:
        return _preselection_certificate(context, paths, result)
    frozen=result.get("frozen_policy_family")
    if frozen is None or frozen.get("status")!="frozen":
        check(result.get("selected_policy") is None,"selection without complete frozen policies")
        return {"status":"passed","scope":"no_source_qualified_policy_selection"}
    policy_contract = context["spec"]["trade_policy"]
    family_contract = policy_contract["family"]
    index = context.get("trade_family_review_index")
    governance = (policy_contract["registration"] == "declared"
        and instant(family_contract["start_at"]) <= instant(context["decision_at"]) < instant(family_contract["end_at"])
        and type(index) is int and 1 <= index <= family_contract["max_reviews"])
    check(paths["status"]=="research_ready","selection without qualified source paths")
    probabilities=[float(row["probability"]) for row in paths["selection_paths"]]
    check(probabilities and all(p>0 and math.isfinite(p) for p in probabilities)
        and math.isclose(math.fsum(probabilities),1.,abs_tol=1e-10),"original probability support")
    probabilities=risk_numbers.measure(probabilities)
    numeric=risk_numbers.number
    tail=numeric(context["spec"]["allocation"]["tail_probability"])
    check(0<tail<=1,"CVaR tail mass")
    economic=context["spec"]["trade_policy"]["economic"]; state=context["trade_state"]
    checked=result["calibration"]["candidates"]
    options={row["funding_group_id"]:row for row in result["funding_options"]}
    check(len(options)==len(result["funding_options"]) and set(options)=={row["funding_group_id"] for row in frozen["groups"]},"funding-group completeness")
    certificates=[]; selected=None; zero_status="risk_unresolved"
    for original in frozen["groups"]:
        group=options[original["funding_group_id"]]
        check(group["proposed_contribution"]==original["proposed_contribution"] and group["build_status"]==original["build_status"],"funding scope changed")
        candidates=group["candidates"]; identities=[row["id"] for row in candidates]
        source_ids={row["id"] for row in original["policies"]}
        invalid=set(result["source_invalid_policies"])&source_ids
        check(len(identities)==len(set(identities)) and set(identities)<=source_ids and set(identities)|invalid==source_ids,"candidate disposition completeness")
        originals={row["id"]:row for row in original["policies"]}
        for policy in candidates:
            check(all(policy[key]==value for key,value in originals[policy["id"]].items()),"candidate changed frozen source policy")
        baselines=[row for row in candidates if row["mode"]=="baseline"]
        check(len(baselines)==1 or not candidates,"exactly one same-funded baseline required")
        baseline=baselines[0] if baselines else None
        ranks=[]; certificates_group=[]
        for policy in candidates:
            source_action(context,policy)
            amount=numeric(policy["proposed_contribution"])
            capital=numeric(context["snapshot"]["equity"])+amount
            principal=numeric(context["risk_state"]["net_principal"])+amount
            tolerance=numeric(context["risk_state"]["loss_tolerance"])
            budget=capital-principal*(1-tolerance)
            check(capital>0 and len(policy["selection"])==len(probabilities),"original capital/path inventory")
            check(all(len(rows)==1 and set(rows[0])=={row["id"] for row in paths["selection_paths"]}
                for rows in paths.get("prefix_groups",{}).values()),"undeclared information partition")
            losses=[capital-numeric(row["terminal_wealth"]) for row in policy["selection"]]
            risk=_variational_tail(losses,probabilities,tail)
            expected=-sum(p*loss for p,loss in zip(probabilities,losses))
            fees=sum(p*numeric(row["fees"]) for p,row in zip(probabilities,policy["selection"]))
            turnover=sum(p*sum(numeric(entry["request"] if entry["kind"]=="reserve_buy" else entry["gross"])
                for entry in row["cash_flow_log"] if entry["kind"] in {"reserve_buy","price_sell"}) for p,row in zip(probabilities,policy["selection"]))
            for actual,value,label in ((policy["absolute_cvar"],risk,"independent CVaR"),(policy["expected_profit"],expected,"expected profit"),
                (policy["expected_policy_fees"],fees,"expected fees"),(policy["expected_policy_turnover"],turnover,"expected turnover")):
                number(actual,value,label)
            _published(policy["absolute_cvar"],risk,"published independent CVaR")
            from verify import cash_deadline_invariants
            cash_ok=all([cash_deadline_invariants(context,policy,paths["point_paths"][0],policy["point"],primary_end=paths["stage_dates"][-1]),
                *[cash_deadline_invariants(context,policy,path,value,primary_end=paths["stage_dates"][-1]) for path,value in zip(paths["selection_paths"],policy["selection"])]])
            concentration=exposure(context,policy,float(capital))
            baseline_risk=_variational_tail([capital-numeric(row["terminal_wealth"]) for row in baseline["selection"]],probabilities,tail)
            reducing=bool((budget<0 or baseline_risk>budget) and policy["current_action"]["sells"] and not policy["current_action"]["buys"]
                and not policy["future_rule"]["weights"] and not policy["future_rule"].get("conditional_sells")
                and baseline_risk-risk>=numeric(economic["minimum_risk_reduction_amount"]))
            directions=[(row["code"],"buy") for row in policy["current_action"]["buys"]]
            lots={row["lot_id"]:row for row in context["snapshot"]["positions"]}
            directions += [(lots[row["lot_id"]]["code"],"sell") for row in policy["current_action"]["sells"]]
            reversal=any(code in state["last_executed_by_code"] and state["last_executed_by_code"][code]["side"]!=side for code,side in directions)
            delta=numeric(economic["minimum_reversal_advantage_amount"] if reversal else economic["minimum_net_advantage_amount"])
            advantage=numeric(policy["point"]["terminal_wealth"])-numeric(baseline["point"]["terminal_wealth"])
            calibration=checked.get(policy["id"],{}); upper=calibration.get("risk_cvar_upper"); buffer=calibration.get("optimism_buffer_amount")
            cash_identity=(policy["mode"]=="baseline" and not any(Decimal(row["shares"]) for row in context["snapshot"]["positions"])
                and Decimal(context["snapshot"]["reserved_cash"])==0 and Decimal(context["snapshot"]["unsettled_cash"])==0
                and Decimal(context["snapshot"]["available_cash"])==Decimal(context["snapshot"]["equity"])
                and all(Decimal(str(row["terminal_wealth"]))==capital and row["fees"]==0 for row in [policy["point"],*policy["selection"]]))
            if cash_identity:
                upper,buffer=0.,0.
            calibrated=cash_identity or calibration.get("status")=="calibrated" and upper is not None and buffer is not None
            rolling=numeric(state["actual_window_fees"])+max(numeric(row["fees"]) for row in policy["selection"])
            fee_ok=rolling<=numeric(economic["maximum_rolling_fee_amount"])
            ordinary=bool(governance and calibrated and risk<=budget and numeric(upper)<=budget and fee_ok and concentration and cash_ok
                and (policy["mode"]=="baseline" or reducing or advantage>delta+numeric(buffer)))
            recovery=bool(governance and not ordinary and reducing and cash_ok and concentration and fee_ok)
            check(policy["eligible"] is ordinary and policy["trade_guard"]["eligible"] is ordinary,"ordinary risk eligibility")
            check(policy["recovery_eligible"] is recovery and policy["trade_guard"]["recovery_eligible"] is recovery,"separate risk recovery eligibility")
            violation=max(0,risk-budget)
            _published(policy["risk_violation_amount"],violation,"risk violation")
            guard=policy["trade_guard"]
            for actual,value,label in ((guard["risk_budget"],budget,"principal budget"),(guard["net_advantage"],advantage,"point advantage"),
                (guard["required_advantage"],delta,"registered advantage"),(guard["rolling_fee_total"],rolling,"rolling fee")):
                _published(actual,value,label)
            _published(guard["risk_violation_amount"],violation,"guard risk violation")
            _published(guard["fee_budget"],numeric(economic["maximum_rolling_fee_amount"]),"registered fee budget")
            check(guard["risk_cvar_upper"]==upper and guard["optimism_buffer_amount"]==buffer and guard["reversal"] is reversal,"calibrated guard inputs")
            check(guard["risk_status"]==("within_verified_budget" if ordinary else "risk_not_restored"),"risk status falsely restored")
            expected_status="cash_requirement_failed" if not cash_ok else "passed" if ordinary else "risk_recovery" if recovery else "needs_calibration" if buffer is None else "no_action"
            check(guard["status"]==expected_status,"guard status does not match independent qualification")
            check(guard["risk_evidence_basis"]==("source_cash_identity" if cash_identity else "independent_policy_calibration" if calibrated else "empirical_path_only"),"risk evidence basis")
            rank=(0 if ordinary else 1 if recovery else 2,0. if ordinary else violation,-expected,risk,fees,turnover,policy["id"])
            ranks.append((rank,policy,ordinary,recovery))
            certificates_group.append({"policy_id":policy["id"],"ordinary":ordinary,"recovery":recovery,"rank":[float(value) for value in rank[:-1]]+[rank[-1]]})
        ranks.sort(key=lambda row:row[0])
        check(identities==[row[1]["id"] for row in ranks],"registered deterministic candidate ordering")
        ordinary_best=next((row[1] for row in ranks if row[2]),None)
        recovery_best=next((row[1] for row in ranks if row[3]),None) if ordinary_best is None else None
        best=ordinary_best or recovery_best
        check(group["best"]==best and group["ordinary_best"]==ordinary_best and group["recovery_best"]==recovery_best,"winner violates feasible argmax or recovery priority")
        status="feasible_selected" if ordinary_best else "risk_recovery" if recovery_best else "risk_unresolved"
        check(group["decision_status"]==status,"group decision status")
        if group["proposed_contribution"]==0:
            selected=best; zero_status=status
        certificates.append({"funding_group_id":group["funding_group_id"],"candidates":certificates_group,"decision_status":status})
    check(result["selected_policy"]==selected and result["decision_status"]==zero_status,"confirmed-funding selected winner")
    check(result["current_action"]==(selected["current_action"] if selected else {"buys":[],"sells":[]}),"published current action")
    certificate={"status":"passed","decision_status":zero_status,"scope":"independent_registered_finite_set_feasibility_selection_not_future_profit",
        "groups":certificates,"selected_policy_id":selected["id"] if selected else None}
    certificate["certificate_hash"]=fingerprint(certificate)
    return certificate


def _preselection_certificate(context, paths, result):
    """Check full-scope MILP inputs/gap and forbid reselection after calibration."""
    import copy
    import allocation
    original = result["frozen_policy_family"]
    selection = result["selection_optimizer"]
    check(selection == original["selection_optimizer"], "frozen selection differs from the solver declaration")
    check(selection["selection_hash"] == fingerprint({key: value for key, value in selection.items() if key != "selection_hash"}),
          "full-scope selection hash changed")
    check(selection["scope_codes"] == context["allocation_codes"] and selection["scope_narrowed"] is False,
          "solver deleted original candidate or held risk scope")
    check(selection["calibration_outcomes_examined"] is False and selection["basis"]["calibration_outcomes_examined"] is False,
          "selection used independent calibration outcomes")
    comparison = allocation.build_source_actions(context)
    descriptor = comparison["funding_options"][0]["descriptor"]
    basis = selection["basis"]
    check(basis["basis_hash"] == fingerprint({key: value for key, value in basis.items() if key != "basis_hash"}), "source payoff basis hash changed")
    check(basis["source_descriptor_hash"] == fingerprint(descriptor), "solver changed the complete original source menus")
    check(basis["source_selection_paths_hash"] == fingerprint([*paths["point_paths"], *paths["selection_paths"]]), "solver changed source selection origins")
    check(basis["source_menu_catalog"] == descriptor["menus"], "solver omitted or changed original fund/lot factor menus")
    funds = [row for row in basis["menus"] if row["kind"] == "fund_choice"]
    for menu, source in zip(funds, descriptor["menus"]):
        check(menu["code"] == source["code"] and [basis["columns"][i]["choice"] for i in menu["indices"]] == source["choices"],
              "solver source fund menu lost or invented a lawful amount choice")
        factor_groups = [row for row in basis["menus"] if row["code"] == source["code"] and row["kind"] == "specific_lot"]
        for factor, original_factor in zip(factor_groups, source["specific_lot_menus"] or []):
            expected=[{"buys":[],"sells":[row] if row is not None else []} for row in original_factor]
            check([basis["columns"][i]["choice"] for i in factor["indices"]] == expected, "solver lost a source lot precision choice")
        check(len(factor_groups) == len(source["specific_lot_menus"] or []), "solver omitted an original held lot")
    check(len(funds) == len(descriptor["menus"]), "solver omitted an original fund menu")
    solver = selection["solver"]; model = solver["model"]
    check(solver["status"] == 0 and solver["model_hash"] == fingerprint(model), "unsupported full-scope MILP certificate")
    check(solver["reported_relative_gap"] is not None and 0 <= solver["reported_relative_gap"] <= 1e-9,
          "full-scope optimality gap is not qualified")
    x = solver["primal_variables"]
    check(type(x) is list and len(x) == model["variable_count"] and all(math.isfinite(value) for value in x), "solver primal vector is incomplete")
    for value, low, high, integral in zip(x, model["variable_lower"], model["variable_upper"], model["integrality"]):
        check((low is None or value >= low-1e-8) and (high is None or value <= high+1e-8), "solver primal variable violates its source bound")
        check(not integral or abs(value-round(value)) <= 1e-7, "solver rounded a noninteger menu choice")
    lhs = [0.0]*len(model["constraint_labels"])
    for row, column, coefficient in model["sparse_rows"]:
        lhs[row] += coefficient*x[column]
    for value, low, high in zip(lhs, model["constraint_lower"], model["constraint_upper"]):
        check((low is None or value >= low-1e-8) and (high is None or value <= high+1e-8), "solver primal constraint violation")
    primal = math.fsum(coefficient*value for coefficient, value in zip(model["objective"], x))
    number(solver["primal_min_objective"], primal, "solver primal objective")
    check(solver["dual_min_bound"] is not None and math.isfinite(solver["dual_min_bound"])
          and solver["dual_min_bound"] <= primal+1e-8, "solver global bound does not bracket its primal objective")
    action = {key: [copy.deepcopy(row) for i in selection["chosen_columns"] for row in basis["columns"][i]["choice"][key]] for key in ("buys", "sells")}
    check(action == selection["action"], "MILP selected action changed its menu columns")
    frozen = [policy for policy in original["policies"] if policy["id"] == result["preselected_policy_id"]]
    check(len(frozen) == 1 and frozen[0]["current_action"] == action and frozen[0]["future_rule"] == {"kind": "hold", "weights": {}},
          "frozen preselected policy differs from the source-menu optimum")
    diagnostic = copy.deepcopy(result)
    diagnostic["selected_policy"] = result["qualification_diagnostic_selected_policy"]
    diagnostic["decision_status"] = result["qualification_diagnostic_decision_status"]
    diagnostic["current_action"] = diagnostic["selected_policy"]["current_action"] if diagnostic["selected_policy"] else {"buys": [], "sells": []}
    ordinary = decision_certificate(context, paths, diagnostic, _qualification_diagnostic=True)
    winner = next((row for group in result["funding_options"] for row in group["candidates"]
        if row["id"] == result["preselected_policy_id"] and (row["eligible"] or row.get("recovery_eligible"))), None)
    check(result["selected_policy"] == winner and result["current_action"] == (winner["current_action"] if winner else {"buys": [], "sells": []}),
          "calibration caused selection of another policy after the winner was frozen")
    check(result["status"] == ("research_ready" if winner else "partial"), "independent preselected winner status")
    value = {"status": "passed", "scope": "full_original_menu_solver_gap_and_preselection_before_independent_calibration_not_future_profit",
        "numerical_solver_relative_gap": solver["reported_relative_gap"],
        "source_scope_codes": selection["scope_codes"], "preselected_policy_id": result["preselected_policy_id"],
        "independently_qualified": winner is not None, "qualification_diagnostic": ordinary}
    return {**value, "certificate_hash": fingerprint(value)}


def family_certificate(results, artifacts, selection):
    """Independent same-capital feasible argmax, then risk recovery priority."""
    basis=None; rows=[]; identities=set()
    for item in results:
        identity=item["family"]["family_id"]
        check(identity not in identities,"duplicate registered family")
        identities.add(identity)
        context=artifacts.read_json(item["context_ref"]); calculation=artifacts.read_json(item["calculation_ref"])
        current={key:context[key] for key in ("account_hash","decision_at","risk_state")}
        current["capital"]=context["snapshot"]["equity"]
        check(basis is None or basis==current,"family capital or risk origin differs")
        basis=current
        policy=calculation.get("mpc",{}).get("selected_policy")
        if calculation.get("status")!="research_ready" or calculation.get("orders",{}).get("status") not in {"ready","no_action"} or policy is None:
            continue
        check(policy["proposed_contribution"]==0,"optional capital entered family winner")
        ordinary=policy["eligible"] is True
        recovery=calculation["mpc"].get("decision_status")=="risk_recovery" and policy.get("recovery_eligible") is True
        if not ordinary and not recovery:
            continue
        contract=context["spec"]["trade_policy"]; family=contract["family"]; index=context.get("trade_family_review_index")
        check(contract["registration"]=="declared" and instant(family["start_at"])<=instant(context["decision_at"])<instant(family["end_at"])
            and type(index) is int and 1<=index<=family["max_reviews"],"family winner lacks registered governance")
        check(not ordinary or not recovery,"family recovery disguised as ordinary qualification")
        probabilities=risk_numbers.measure([row["probability"] for row in calculation["paths"]["selection_paths"]])
        capital=risk_numbers.number(context["snapshot"]["equity"])
        profit=sum(p*(risk_numbers.number(value["terminal_wealth"])-capital) for p,value in zip(probabilities,policy["selection"]))
        losses=[capital-risk_numbers.number(value["terminal_wealth"]) for value in policy["selection"]]; tail=context["spec"]["allocation"]["tail_probability"]
        risk=_variational_tail(losses,probabilities,tail)
        budget=capital-risk_numbers.number(context["risk_state"]["net_principal"])*(1-risk_numbers.number(context["risk_state"]["loss_tolerance"]))
        violation=max(0,risk-budget)
        fees=policy["point"]["fees"]
        rank=(recovery,violation if recovery else 0.,-profit,risk,fees,identity)
        rows.append((rank,identity,profit,risk,fees,violation,"risk_recovery" if recovery else "feasible_selected"))
    rows.sort(key=lambda row:row[0])
    check([row["family_id"] for row in selection["ranked"]]==[row[1] for row in rows],"family winner ordering")
    check(selection["selected_family_id"]==(rows[0][1] if rows else None),"selected family differs from independent finite-set optimum")
    check(selection["decision_status"]==(rows[0][6] if rows else "risk_unresolved"),"family risk state")
    for actual,expected in zip(selection["ranked"],rows):
        for key,index in (("expected_profit",2),("absolute_cvar",3),("fees",4),("risk_violation_amount",5)):
            number(actual[key],expected[index],"family "+key)
        _published(actual["absolute_cvar"],expected[3],"family published risk")
        _published(actual["risk_violation_amount"],expected[5],"family published violation")
        check(actual["decision_status"]==expected[6],"family qualification label")
    return {"status":"passed","scope":"independent_registered_family_feasible_then_risk_recovery_ordering_not_global_optimality"}
