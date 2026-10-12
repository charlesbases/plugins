"""Complete source-menu MILP, selection first and independent calibration next.

Only current lawful orders are chosen. The evaluation holds their confirmed
shares/cash through H; another real invocation re-solves from its actual account.
This replaces exponential hypothetical feedback-policy enumeration. It does not
restrict news, industries, funds, real cash rights or the source/PIT contract.
"""
import copy
import math
import warnings
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csc_array

import ledger
import risk_numbers
from contracts import EvidenceError, fingerprint, require

METHOD = "source_paths_single_stage_full_scope_milp_v1"
THEORY = {
    "CVaR_epigraph": "https://sites.math.washington.edu/~rtr/papers/rtr179-CVaR1.pdf",
    "mixed_integer_solver": "https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html",
    "scope": "finite_complete_source_menu_selection_optimum_to_reported_solver_precision; not_future_profit_or_optimum_after_calibration",
}
EMPTY = {"buys": [], "sells": []}
HOLD = {"kind": "hold", "weights": {}}


def _specific_lot_columns(context, sources, code, factors, dates, pricing):
    """Per-lot finite choices; aggregate minima are MILP constraints, not orders.

    Fee/terminal-exit rounding is evaluated for every complete lot choice.
    A declared source cash-entitlement pattern couples same-fund lots through
    cent rounding and needs the corresponding integer entitlement model; it is
    never removed or replaced by zero to make this factorisation pass.
    """
    import fee_contract as fees
    import portfolio_mpc as mpc
    import single_step_wealth
    import trading_calendar
    lots = {row["lot_id"]: row for row in context["snapshot"]["positions"] if row["code"] == code}
    terms = context["fee_contracts"][code]
    __import__("allocation").validate_redemption_fee_scope(terms, len(lots))
    clock = single_step_wealth.build_clock_context(context)["assets"][code]
    rows, menus, coupons = [], [], []
    existing_ids = {row["source_identity"] for row in context["snapshot"].get("receivables", []) if row["kind"] == "dividend"}
    from allocation_market import _rights
    from contracts import instant
    for source_index,path in enumerate(sources):
        for event in path["distributions"]:
            if event["code"] != code or event["id"] in existing_ids or event["record_date"] > dates[-1]:
                continue
            requirement=mpc.cash_requirement(context)
            wealth_relevant=event["ex_date"] <= dates[-1]
            cash_relevant=bool(requirement and event["ex_date"] <= requirement["date"] and event["pay_date"] <= requirement["date"])
            if not wealth_relevant and not cash_relevant:
                continue
            amount=sum((Decimal(lot["shares"])*Decimal(str(event["per_share"])) for lot in lots.values()
                if _rights(event,instant(lot.get("ownership_at",lot["acquired_at"])).astimezone(mpc.ZONE).date())),Decimal(0))
            coupons.append({"code":code,"source_index":source_index,"event":copy.deepcopy(event),
                "baseline_unrounded":ledger.decimal(amount),"baseline_amount":ledger.decimal(amount.quantize(Decimal(".01"),rounding=ROUND_HALF_UP)),
                "wealth_relevant":wealth_relevant,"deadline_relevant":cash_relevant})
    for factor in factors:
        identity = next(value["lot_id"] for value in factor if value is not None)
        lot = lots[identity]
        owned = Decimal(lot["shares"])
        permitted = trading_calendar.holding_days(lot["acquired_at"], context["decision_at"], terms["holding"]) >= terms["holding"]["minimum_days"]
        indices = []
        for value in factor:
            quantity = Decimal(value["shares"]) if value is not None else Decimal(0)
            choice = {"buys": [], "sells": [copy.deepcopy(value)] if value is not None else []}
            wealth, fee_delta, deadline = [], [], []
            for path in sources:
                if quantity == 0 or not permitted:
                    wealth.append("0");fee_delta.append("0");deadline.append("0");continue
                priced = mpc._price(path, code, clock["pricing_date"], execution=True)
                terminal = mpc._price(path, code, dates[-1], execution=True)
                confirmed = fees.normal_confirmation_end(terms, clock["pricing_date"])
                quote = fees.quote_exit_details(quantity*priced, terms, acquired_at=lot["acquired_at"],
                    execution_at=context["decision_at"], confirmed_at=mpc._stamp(str(confirmed), "00:00:00"))
                delta = quote["net"]-quantity*terminal
                cost = quote["effective_cost"]
                if context["spec"]["planning"]["primary_goal"] == "redeem":
                    original = fees.quote_exit_details(owned*terminal, terms, acquired_at=lot["acquired_at"],
                        execution_at=mpc._stamp(dates[-1], __import__("contracts").instant(context["decision_at"]).astimezone(mpc.ZONE).time().replace(tzinfo=None).isoformat()))
                    remainder = (fees.quote_exit_details((owned-quantity)*terminal, terms, acquired_at=lot["acquired_at"],
                        execution_at=mpc._stamp(dates[-1], __import__("contracts").instant(context["decision_at"]).astimezone(mpc.ZONE).time().replace(tzinfo=None).isoformat()))
                        if quantity < owned else {"net":Decimal(0),"contractual_fee":Decimal(0),"rounding_loss":Decimal(0)})
                    delta += (remainder["net"]-(owned-quantity)*terminal)-(original["net"]-owned*terminal)
                    cost += remainder["contractual_fee"]+remainder["rounding_loss"]-original["contractual_fee"]-original["rounding_loss"]
                rule = terms["settlement"]
                settled = trading_calendar.advance(confirmed, rule["lag_days"], rule["day_basis"], rule.get("calendar", terms["execution_calendar"]))
                requirement = mpc.cash_requirement(context)
                wealth.append(ledger.decimal(delta));fee_delta.append(ledger.decimal(cost))
                deadline.append(ledger.decimal(quote["net"]) if requirement and str(settled) <= requirement["date"] else "0")
            entitlement = {}
            for coupon_index,coupon in enumerate(coupons):
                event=coupon["event"]
                ownership=instant(lot.get("ownership_at",lot["acquired_at"])).astimezone(mpc.ZONE).date()
                change=int(_rights(event,ownership,dt_date(clock["pricing_date"])))-int(_rights(event,ownership))
                entitlement[str(coupon_index)]=ledger.decimal(quantity*Decimal(str(event["per_share"]))*change)
            row = {"code": code, "choice": choice, "current_cash_debit": "0", "wealth_delta": wealth,
                "fee_delta": fee_delta, "projected_value_delta": ledger.decimal(-quantity*pricing),
                "deadline_cash_delta": deadline if mpc.cash_requirement(context) else None,
                "source_allowed": quantity == 0 or permitted, "lot_quantity": ledger.decimal(quantity), "lot_id": identity,
                "entitlement_raw_deltas":entitlement}
            indices.append(len(rows)); rows.append(row)
        menus.append({"code": code, "indices": indices, "kind": "specific_lot", "lot_id": identity})
    return rows, menus, coupons


def dt_date(value):
    import datetime
    return datetime.date.fromisoformat(value)


def _local_context(context, code):
    snapshot = context["snapshot"]
    return {**context, "allocation_codes": [code], "universe": [code],
        "purchase_eligible_codes": [code] if code in context["purchase_eligible_codes"] else [],
        "fee_contracts": {code: context["fee_contracts"][code]},
        "identities": {code: context["identities"][code]},
        "snapshot": {**snapshot, "positions": [row for row in snapshot["positions"] if row["code"] == code],
                     "prices": {code: snapshot["prices"][code]}},
        "model_request": {**context["model_request"], "assets": [
            row for row in context["model_request"]["assets"] if row["code"] == code]}}


def _local_path(path, code, marks):
    return {**path, "nav": {day: {code: row[code]} for day, row in path["nav"].items()},
        "distributions": [row for row in path["distributions"] if row["code"] == code],
        "known_marks": {code: marks[code]}, "price_schema": "source_role_price_paths_v3"}


def exact_wealth(value, path, end):
    """Reconstruct Decimal wealth from the original replay's monetary journal."""
    total = sum((Decimal(value[key]) for key in ("settled_cash", "reserved_cash", "receivables")), Decimal(0))
    for lot in value["terminal_lots"]:
        total += Decimal(lot["shares"])*Decimal(str(path["nav"][end][lot["code"]]))
    for row in value["terminal_exit_assumptions"]:
        total += Decimal(row["net"])-Decimal(row["raw_gross"])
    require(math.isclose(float(total), value["terminal_wealth"], rel_tol=1e-12, abs_tol=1e-8),
            "Source replay wealth cannot be independently reconstructed")
    return total


def exact_fees(value):
    result = sum((Decimal(row["fee"]) for row in value["cash_flow_log"] if row["kind"] in {"price_buy", "price_sell"}), Decimal(0))
    result += sum((Decimal(row["contractual_fee"])+Decimal(row["rounding_loss"])
                   for row in value["terminal_exit_assumptions"]), Decimal(0))
    return result


def _basis(context, paths, group):
    """Evaluate O(sum(menu sizes)*source origins), never their Cartesian product.

    The current-only replay is additive between different legal fund subjects.
    Each entire per-code choice retains all its lots and aggregate dividend
    rounding. The final combined action is independently replayed before use.
    """
    import portfolio_mpc as mpc
    import single_step_wealth
    require(group["proposed_contribution"] == 0,
            "Current full-scope method cannot invent or recommend additional principal")
    end = paths["stage_dates"][-1]
    dates = [context["as_of"], end]
    sources = [*paths["point_paths"], *paths["selection_paths"]]
    require(len(paths["point_paths"]) == 1 and paths["selection_paths"], "Current point and complete joint selection paths required")
    complete = [{**path, "known_marks": paths["known_marks"], "price_schema": paths["schema"]} for path in sources]
    baseline = [mpc.simulate(context, path, EMPTY, HOLD, dates) for path in complete]
    baseline_wealth = [exact_wealth(value, path, end) for value, path in zip(baseline, complete)]
    baseline_fees = [exact_fees(value) for value in baseline]
    execution_clock = single_step_wealth.build_clock_context(context)
    pricing = {code: mpc._price(complete[0], code,
        execution_clock["assets"][code]["pricing_date"], execution=True)
        for code in context["allocation_codes"]}
    distribution = {"codes": context["allocation_codes"], "returns": [[0]*len(context["allocation_codes"])], "probabilities": [1]}
    projection = single_step_wealth.project(context, EMPTY, distribution, pricing_nav=pricing)
    columns, menus, coupons = [], [], []
    for menu in group["descriptor"]["menus"]:
        code = menu["code"]
        local = _local_context(context, code)
        local_sources = [_local_path(path, code, paths["known_marks"]) for path in sources]
        local_hold = [mpc.simulate(local, path, EMPTY, HOLD, dates) for path in local_sources]
        local_wealth = [exact_wealth(value, path, end) for value, path in zip(local_hold, local_sources)]
        local_fees = [exact_fees(value) for value in local_hold]
        local_distribution = {"codes": [code], "returns": [[0]], "probabilities": [1]}
        held_projection = single_step_wealth.project(local, EMPTY, local_distribution, pricing_nav={code: pricing[code]})
        indices = []
        for choice in menu["choices"]:
            # A failure remains a source gap; it cannot remove a comparison
            # candidate merely because its point path is difficult to price.
            values = [mpc.simulate(local, path, choice, HOLD, dates) for path in local_sources]
            current = single_step_wealth.project(local, choice, local_distribution, pricing_nav={code: pricing[code]})
            debit = sum((Decimal(row["cash_debit"]) for row in choice["buys"]), Decimal(0))
            column = {"code": code, "choice": copy.deepcopy(choice), "current_cash_debit": ledger.decimal(debit),
                "wealth_delta": [ledger.decimal(exact_wealth(value, path, end)-old)
                                 for value, path, old in zip(values, local_sources, local_wealth)],
                "fee_delta": [ledger.decimal(exact_fees(value)-old) for value, old in zip(values, local_fees)],
                "projected_value_delta": ledger.decimal(Decimal(current["values"][code])-Decimal(held_projection["values"][code])),
                "deadline_cash_delta": [ledger.decimal(Decimal(value["cash_deadline"]["available_cash"])
                    -Decimal(old["cash_deadline"]["available_cash"])) for value, old in zip(values, local_hold)]
                    if mpc.cash_requirement(context) else None}
            indices.append(len(columns)); columns.append(column)
        require(indices, "Every original source menu needs a lawful unchanged choice")
        menus.append({"code": code, "indices": indices, "kind": "fund_choice"})
        if menu["specific_lot_menus"] is not None:
            lot_rows, lot_menus, lot_coupons = _specific_lot_columns(context, complete, code, menu["specific_lot_menus"], dates, pricing[code])
            offset = len(columns)
            coupon_offset=len(coupons)
            for row in lot_rows:
                row["entitlement_raw_deltas"]={str(int(key)+coupon_offset):value for key,value in row["entitlement_raw_deltas"].items()}
            for row in lot_menus:
                row["indices"] = [i+offset for i in row["indices"]]
            columns.extend(lot_rows); menus.extend(lot_menus);coupons.extend(lot_coupons)
    require(sorted({row["code"] for row in menus}) == sorted(context["allocation_codes"]), "Full original dynamic decision scope must be retained")
    value = {"schema_id": "source_current_action_additive_basis_v1", "columns": columns, "menus": menus,
        "baseline_wealth": list(map(ledger.decimal, baseline_wealth)), "baseline_fees": list(map(ledger.decimal, baseline_fees)),
        "baseline_projected_values": projection["values"],
        "baseline_deadline_cash": [row["cash_deadline"]["available_cash"] for row in baseline] if mpc.cash_requirement(context) else None,
        "source_selection_paths_hash": fingerprint(sources), "source_descriptor_hash": group["descriptor_hash"],
        "stage_dates": dates, "calibration_outcomes_examined": False,
        "source_menu_catalog": copy.deepcopy(group["descriptor"]["menus"]),
        "coupons":coupons,
        "scope": "complete_per_code_choices_current_order_and_hold_to_H_evaluation"}
    return {**value, "basis_hash": fingerprint(value)}


def _model(context, paths, basis, *, recovery=False):
    """Rockafellar--Uryasev epigraph with source-fixed integer choice rows."""
    columns, menus = basis["columns"], basis["menus"]
    probabilities = risk_numbers.measure([row["probability"] for row in paths["selection_paths"]])
    p = np.asarray([float(value) for value in probabilities])
    count, scenarios = len(columns), len(p)
    capital = Decimal(context["snapshot"]["equity"])
    require(capital > 0, "Positive actual source-marked equity required")
    budget = capital-Decimal(context["risk_state"]["net_principal"])*(1-Decimal(context["risk_state"]["loss_tolerance"]))
    scale = float(capital)
    wealth = np.asarray([[float(Decimal(row["wealth_delta"][i+1]))/scale for row in columns] for i in range(scenarios)])
    base = np.asarray([float(Decimal(value))/scale for value in basis["baseline_wealth"][1:]])
    tail = float(context["spec"]["allocation"]["tail_probability"])
    lot_codes = sorted({row["code"] for row in menus if row["kind"] == "specific_lot"})
    coupon_start=count+1+scenarios+2*len(lot_codes)
    width = coupon_start+len(basis["coupons"])
    objective = np.zeros(width)
    objective[:count] = -p@wealth
    if recovery:
        objective[:] = 0
        objective[count] = 1
        objective[count+1:count+1+scenarios] = p/tail
    lower = np.zeros(width); upper = np.ones(width)
    lower[count] = -np.inf; upper[count:] = np.inf
    integrality = np.zeros(width, dtype=np.uint8); integrality[:count] = 1
    if lot_codes:
        integrality[count+1+scenarios:] = 1
        upper[count+1+scenarios:] = 1
        for i,column in enumerate(columns):
            if column.get("source_allowed") is False:
                upper[i] = 0
    for coupon_index,coupon in enumerate(basis["coupons"]):
        variable=coupon_start+coupon_index
        upper[variable]=float(Decimal(coupon["baseline_amount"])*100+1)
        if not recovery and coupon["wealth_relevant"] and coupon["source_index"] > 0:
            objective[variable]=-p[coupon["source_index"]-1]*.01/scale
    if recovery:
        for i, column in enumerate(columns):
            if column["choice"]["buys"]:
                upper[i] = 0
    rows, lows, highs, labels = [], [], [], []
    def append(coefficients, low, high, label):
        rows.append(coefficients); lows.append(float(low)); highs.append(float(high)); labels.append(label)
    for menu in menus:
        append({index: 1 for index in menu["indices"]}, 1, 1, "one_original_choice:"+menu["code"])
    append({i: float(Decimal(row["current_cash_debit"]))/scale for i, row in enumerate(columns)},
           -np.inf, float(Decimal(context["snapshot"]["available_cash"]))/scale, "arrived_cash_only_no_redemption_credit")
    for coupon_index,coupon in enumerate(basis["coupons"]):
        coefficients={coupon_start+coupon_index:1.}
        decimals=[Decimal(coupon["baseline_unrounded"])*100]
        for i,column in enumerate(columns):
            delta=Decimal(column.get("entitlement_raw_deltas",{}).get(str(coupon_index),"0"))*100
            decimals.append(delta)
            if delta:
                coefficients[i]=-float(delta)
        precision=max(1,max(-value.as_tuple().exponent for value in decimals))
        step=float(Decimal(10)**-precision)
        baseline=float(decimals[0])
        append(coefficients,baseline-.5+step,baseline+.5,"source_fund_entitlement_half_up_cent:"+str(coupon_index))
    for code_index,code in enumerate(lot_codes):
        sale_var=count+1+scenarios+2*code_index;full_var=sale_var+1
        sold={i:float(Decimal(row["lot_quantity"])) for i,row in enumerate(columns) if row["code"]==code and row.get("lot_quantity") not in (None,"0")}
        buys={i:1 for i,row in enumerate(columns) if row["code"]==code and row["choice"]["buys"]}
        lots=[row for row in context["snapshot"]["positions"] if row["code"]==code]
        total=sum((Decimal(row["shares"]) for row in lots),Decimal(0))
        terms=context["fee_contracts"][code]
        minimum=Decimal(terms["minimum_redemption_shares"]);residual=Decimal(terms["minimum_remaining_shares"])
        append({**buys,sale_var:1},-np.inf,1,"no_same_stage_lot_sale_and_fund_purchase:"+code)
        append({**sold,sale_var:-float(total)},-np.inf,0,"lot_sale_activity_upper:"+code)
        append({**sold,full_var:-float(total)},0,np.inf,"source_full_redemption_quantity:"+code)
        append({**sold,sale_var:-float(minimum),full_var:float(minimum)},0,np.inf,"aggregate_source_redemption_minimum:"+code)
        append({**sold,sale_var:float(residual),full_var:-float(residual)},-np.inf,float(total),"aggregate_source_remaining_minimum:"+code)
        append({full_var:1,sale_var:-1},-np.inf,0,"source_full_requires_redemption:"+code)
        if not __import__("allocation")._action_supported(terms,"sell_full"):
            upper[full_var]=0
        if not terms.get("full_redemption_allowed"):
            append({**sold,sale_var:-float(minimum)},0,np.inf,"no_below_minimum_full_exception_without_source:"+code)
        if not __import__("allocation")._action_supported(terms,"sell_partial"):
            append({sale_var:1,full_var:-1},0,0,"source_only_full_redemption_supported:"+code)
    for i in range(scenarios):
        row = {j: -float(value) for j, value in enumerate(wealth[i]) if value != 0}
        row[count] = -1; row[count+1+i] = -1
        correction=0.
        for j,coupon in enumerate(basis["coupons"]):
            if coupon["source_index"] == i+1 and coupon["wealth_relevant"]:
                row[coupon_start+j]=-.01/scale
                correction+=float(Decimal(coupon["baseline_amount"]))/scale
        append(row, -np.inf, base[i]-1-correction, "RU_loss_epigraph:"+str(i))
    if not recovery:
        append({count: 1, **{count+1+i: p[i]/tail for i in range(scenarios)}},
               -np.inf, float(budget)/scale, "source_principal_loss_CVaR_budget")
    fees_cap = Decimal(context["spec"]["trade_policy"]["economic"]["maximum_rolling_fee_amount"])
    actual_fees = Decimal(context["trade_state"]["actual_window_fees"])
    for i in range(1, scenarios+1):
        append({j: float(Decimal(column["fee_delta"][i]))/scale for j, column in enumerate(columns)},
               -np.inf, float(fees_cap-actual_fees-Decimal(basis["baseline_fees"][i]))/scale, "complete_path_rolling_fees:"+str(i))
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    for code in context["allocation_codes"]:
        append({i: float(Decimal(row["projected_value_delta"]))/scale for i,row in enumerate(columns) if row["code"]==code},
               -np.inf, float(Decimal(str(assets[code]["max_weight"]))-Decimal(basis["baseline_projected_values"][code])/capital),
               "source_fund_concentration:"+code)
    for group, limit in context["spec"]["constraints"]["fund_group_limits"].items():
        codes = {code for code in context["allocation_codes"] if context["identities"][code]["fund_group_id"] == group}
        append({i: float(Decimal(row["projected_value_delta"]))/scale for i, row in enumerate(columns) if row["code"] in codes},
            -np.inf, float(Decimal(str(limit))-sum((Decimal(basis["baseline_projected_values"][code]) for code in codes), Decimal(0))/capital),
            "source_legal_fund_limit:"+group)
    for sector, limit in context["spec"]["constraints"]["sector_limits"].items():
        known, unknown = {}, []
        for code in context["allocation_codes"]:
            bound = context.get("sector_exposure_bounds", {}).get(code, {}).get(sector, {}).get("upper")
            if bound is None:
                unknown.append(code)
            else:
                require(math.isfinite(float(bound)) and float(bound) >= 0, "Nonnegative actual source sector bound required")
                known[code] = Decimal(str(bound))
        for code in unknown:
            append({i: float(Decimal(row["projected_value_delta"]))/scale for i, row in enumerate(columns) if row["code"] == code},
                -np.inf, -float(Decimal(basis["baseline_projected_values"][code]))/scale, "unknown_source_sector_requires_zero_exposure:"+code)
        current = sum((Decimal(basis["baseline_projected_values"][code])*bound for code, bound in known.items()), Decimal(0))
        append({i: float(Decimal(row["projected_value_delta"])*known[row["code"]])/scale
                for i, row in enumerate(columns) if row["code"] in known},
               -np.inf, float(Decimal(str(limit))-current/capital), "source_sector_upper_limit:"+sector)
    if basis["baseline_deadline_cash"] is not None:
        requirement = Decimal(context["spec"]["planning"]["cash_required_amount"])
        for i, current in enumerate(basis["baseline_deadline_cash"]):
            row={j: float(Decimal(column["deadline_cash_delta"][i]))/scale for j, column in enumerate(columns)}
            correction=Decimal(0)
            for j,coupon in enumerate(basis["coupons"]):
                if coupon["source_index"] == i and coupon["deadline_relevant"]:
                    row[coupon_start+j]=.01/scale;correction+=Decimal(coupon["baseline_amount"])
            append(row,float(requirement-Decimal(current)+correction)/scale,np.inf,"every_source_path_cash_deadline:"+str(i))
    row_indices, column_indices, values = [], [], []
    for row_index, row in enumerate(rows):
        for column_index, value in row.items():
            if value:
                row_indices.append(row_index); column_indices.append(column_index); values.append(float(value))
    matrix = csc_array((values, (row_indices, column_indices)), shape=(len(rows), width))
    finite_description = {"objective": objective.tolist(), "integrality": integrality.tolist(),
        "sparse_rows": [[row_indices[i], column_indices[i], values[i]] for i in range(len(values))],
        "constraint_labels": labels, "constraint_lower": [None if not math.isfinite(value) else value for value in lows],
        "constraint_upper": [None if not math.isfinite(value) else value for value in highs],
        "variable_lower": [None if not math.isfinite(value) else float(value) for value in lower],
        "variable_upper": [None if not math.isfinite(value) else float(value) for value in upper],
        "variable_count": width, "menu_binary_count": count, "source_scenario_count": scenarios,
        "coupon_variable_start":coupon_start,
        "currency_scale": ledger.decimal(capital), "recovery_objective": recovery}
    return objective, integrality, Bounds(lower, upper), LinearConstraint(matrix, lows, highs), finite_description


def select(context, paths, group):
    """Produce only the complete-scope selection decision, before calibration."""
    import portfolio_mpc as mpc
    basis = _basis(context, paths, group)
    attempts = []
    solved = None
    for recovery in (False, True):
        c, integrality, bounds, constraints, description = _model(context, paths, basis, recovery=recovery)
        with warnings.catch_warnings():
            # SciPy documents forwarding options it does not list to HiGHS.
            # The explicit tight primal/integer tolerances are recorded here;
            # actual Decimal/Fraction replay remains the final feasibility gate.
            warnings.filterwarnings("ignore", message="Unrecognized options detected")
            solved = milp(c, integrality=integrality, bounds=bounds, constraints=constraints,
                options={"mip_rel_gap": 0.0, "presolve": True,
                         "mip_feasibility_tolerance": 1e-9, "primal_feasibility_tolerance": 1e-9})
        attempt = {"status": int(solved.status), "message": str(solved.message), "recovery": recovery,
            "model_hash": fingerprint(description), "model": description,
            "mip_node_count": int(solved.mip_node_count) if solved.get("mip_node_count") is not None else None,
            "primal_min_objective": float(solved.fun) if solved.fun is not None else None,
            "dual_min_bound": float(solved.mip_dual_bound) if solved.get("mip_dual_bound") is not None else None,
            "reported_relative_gap": float(solved.mip_gap) if solved.get("mip_gap") is not None else None,
            "primal_variables": solved.x.tolist() if solved.x is not None else None}
        attempts.append(attempt)
        if solved.status == 0:
            break
        if solved.status != 2:
            return {"status": "selection_solver_not_qualified", "attempts": attempts, "basis": basis,
                    "reason": "no_supported_complete_scope_solver_certificate", "trade_ready": False}
    if solved.status != 0:
        return {"status": "selection_source_constraints_infeasible", "attempts": attempts, "basis": basis,
                "reason": "complete_source_risk_or_cash_fee_constraints_infeasible", "trade_ready": False}
    count = len(basis["columns"])
    chosen = []
    for menu in basis["menus"]:
        indices = [i for i in menu["indices"] if solved.x[i] > .5]
        require(len(indices) == 1 and all(abs(solved.x[i]-round(solved.x[i])) <= 1e-7 for i in menu["indices"]),
                "Integer selection cannot be reconstructed from the full source menus")
        chosen.append(indices[0])
    action = {key: [copy.deepcopy(row) for i in chosen for row in basis["columns"][i]["choice"][key]] for key in ("buys", "sells")}
    __import__("allocation").validate_source_action(context, action)
    for coupon_index,coupon in enumerate(basis["coupons"]):
        raw=Decimal(coupon["baseline_unrounded"])+sum((Decimal(basis["columns"][i].get("entitlement_raw_deltas",{}).get(str(coupon_index),"0")) for i in chosen),Decimal(0))
        actual_cents=raw.quantize(Decimal(".01"),rounding=ROUND_HALF_UP)*100
        require(abs(solved.x[description["coupon_variable_start"]+coupon_index]-float(actual_cents)) <= 1e-7,
            "Integer solver entitlement rounding differs from the exact source cent amount; do not publish its incumbent")
    # A source-mechanism interaction (including duplicate entitlement IDs or
    # aggregate rounding) may never be hidden by a separability assumption.
    # Reconstruct the whole selected account on every point/selection path.
    original_sources = [*paths["point_paths"], *paths["selection_paths"]]
    actual_wealth = []
    for source_index, path in enumerate(original_sources):
        current = {**path, "known_marks": paths["known_marks"], "price_schema": paths["schema"]}
        replay = mpc.simulate(context, current, action, HOLD, basis["stage_dates"])
        actual = exact_wealth(replay, current, basis["stage_dates"][-1])
        reconstructed = Decimal(basis["baseline_wealth"][source_index])+sum((
            Decimal(basis["columns"][i]["wealth_delta"][source_index]) for i in chosen), Decimal(0))
        for coupon_index,coupon in enumerate(basis["coupons"]):
            if coupon["source_index"] == source_index and coupon["wealth_relevant"]:
                raw=Decimal(coupon["baseline_unrounded"])+sum((Decimal(basis["columns"][i].get("entitlement_raw_deltas",{}).get(str(coupon_index),"0")) for i in chosen),Decimal(0))
                reconstructed+=raw.quantize(Decimal(".01"),rounding=ROUND_HALF_UP)-Decimal(coupon["baseline_amount"])
        require(actual == reconstructed, "Entire source-account replay violates additive menu wealth; do not publish the MILP incumbent")
        actual_wealth.append(actual)
    probabilities = risk_numbers.measure([row["probability"] for row in paths["selection_paths"]])
    capital = risk_numbers.number(context["snapshot"]["equity"])
    losses = [capital-risk_numbers.number(ledger.decimal(value)) for value in actual_wealth[1:]]
    actual_risk = mpc.tail_mean(losses, probabilities, context["spec"]["allocation"]["tail_probability"], exact=True)
    budget = capital-risk_numbers.number(context["risk_state"]["net_principal"])*(1-risk_numbers.number(context["risk_state"]["loss_tolerance"]))
    require(attempts[-1]["recovery"] or actual_risk <= budget,
            "Integer solver tolerance violates the exact original principal-loss budget; do not relax the gate")
    require(attempts[-1]["reported_relative_gap"] is not None and attempts[-1]["reported_relative_gap"] <= 1e-9,
            "No supported numerical global gap for the complete source-menu model")
    selected = {"status": "selection_policy_frozen", "action": action, "chosen_columns": chosen,
        "solver": attempts[-1], "attempts": attempts, "basis": basis,
        "objective_scope": "minimum_empirical_CVaR_pure_redemption_if_ordinary_constraints_infeasible" if attempts[-1]["recovery"] else
                           "maximum_source_selection_expected_net_wealth_subject_to_empirical_CVaR_and_complete_cash_fee_exposure_constraints",
        "scope_codes": list(context["allocation_codes"]), "scope_narrowed": False,
        "calibration_outcomes_examined": False, "trade_ready": False, "theory": THEORY,
        "selected_original_replay_wealth": list(map(ledger.decimal, actual_wealth)),
        "actual_source_CVaR": float(actual_risk),
        "solver_absolute_objective_gap_amount": max(0, attempts[-1]["primal_min_objective"]-attempts[-1]["dual_min_bound"])*float(capital),
        "optimality_scope": "solver_reported_global_gap_for_the_frozen_source_menu_selection_model; not_all_independently_qualified_policies_or_future_optimum"}
    selected["selection_hash"] = fingerprint(selected)
    return selected


def _frozen(context, comparison, selection):
    """Freeze the globally selected policy and its same-funded baseline only."""
    end = selection["basis"]["stage_dates"][-1]
    descriptor = {"complete_source_descriptor_hash": comparison["funding_options"][0]["descriptor_hash"],
        "preselection_method": METHOD, "selection_hash": selection["selection_hash"], "policy_count_upper": 2,
        "proposed_contribution": 0, "candidate_rule": "selection_first_source_menu_integer_policy_then_independent_calibration"}
    entry = {"group_id": "funding-"+fingerprint(descriptor)[:32], "policy_count_upper": 2,
             "proposed_contribution": 0, "source_descriptor_hash": descriptor["complete_source_descriptor_hash"],
             "selection_descriptor_hash": fingerprint(descriptor)}
    policies = []
    for action in (EMPTY, selection["action"]):
        value = {"schema_id": "source_local_policy_v2", "current_action": copy.deepcopy(action), "future_rule": copy.deepcopy(HOLD),
            "proposed_contribution": 0, "funding_group_id": entry["group_id"], "local_decision_dates": [context["as_of"], end],
            "local_decision_scope_complete": True, "mode": "baseline" if action == EMPTY else "rolling_policy"}
        value["id"] = "mpc-"+fingerprint(value)[:32]
        if value not in policies:
            policies.append(value)
    group = {"proposed_contribution": 0, "funding_group_id": entry["group_id"], "build_status": "complete",
        "policies": policies, "policy_count_upper": 2, "policy_hash": fingerprint(policies),
        "current_action_scope_complete": True, "current_quote_combination_count": comparison["funding_options"][0]["combination_count"],
        "required_actions": [], "alpha_weight": 1}
    result = {"schema_id": "source_frozen_funding_policies_v2", "status": "frozen", "reason": None,
        "required_actions": [], "funding_registry": [entry], "funding_registry_hash": fingerprint([entry]),
        "planned_policy_count_upper": 2, "groups": [group], "policies": policies, "family_hash": fingerprint(policies),
        "source_action_family_hash": comparison["action_family_hash"],
        "scope": "full_source_menu_selection_then_frozen_winner_and_baseline_independent_calibration",
        "computation": [{"current_menu_rows": len(selection["basis"]["columns"]), "policies": len(policies)}],
        "future_trades_published_as_orders": False, "global_investment_optimality_claimed": False,
        "selection_optimizer": selection}
    selected_id = next(policy["id"] for policy in policies if policy["current_action"] == selection["action"])
    return result, selected_id


def optimise(context, paths, comparison, *, family_count=1):
    import portfolio_mpc as mpc
    out = {"status": "partial", "context_hash": context["context_hash"], "path_hash": paths.get("path_hash"),
        "funding_options": [], "selected_policy": None, "current_action": copy.deepcopy(EMPTY),
        "required_actions": [], "live_execution_authorized": False, "scope": THEORY["scope"]}
    if paths.get("status") != "research_ready":
        return {**out, "reason": paths.get("reason"), "required_actions": paths.get("required_actions", [])}
    if comparison.get("status") != "frozen" or len(comparison["funding_options"]) != 1:
        return {**out, "reason": "complete_zero_funding_original_menu_scope_required",
                "required_actions": comparison.get("required_actions", [])}
    try:
        selection = select(context, paths, comparison["funding_options"][0])
    except (mpc.PolicyInfeasible, ValueError) as error:
        return {**out, "reason": "full_scope_source_optimizer_requires_repair_or_evidence",
                "required_actions": [{"action": "complete_full_source_menu_optimization_without_dropping_candidates", "reason": str(error)}]}
    if selection["status"] != "selection_policy_frozen":
        return {**out, "reason": selection["reason"], "selection_optimizer": selection,
                "required_actions": [{"action": "resolve_full_scope_source_solver_constraints", "reason": selection["reason"]}]}
    frozen, identity = _frozen(context, comparison, selection)
    result = mpc.optimise(context, paths, comparison, family_count=family_count, _prefrozen=frozen)
    qualified = next((policy for policy in result["funding_options"][0]["candidates"]
        if policy["id"] == identity and (policy["eligible"] or policy.get("recovery_eligible"))), None)
    # Keep the legacy independent eligibility diagnostic intact. Its ranking
    # does not authorize choosing another winner after examining calibration.
    result["qualification_diagnostic_selected_policy"] = result["selected_policy"]
    result["qualification_diagnostic_decision_status"] = result["decision_status"]
    result.update(selected_policy=qualified, current_action=qualified["current_action"] if qualified else copy.deepcopy(EMPTY),
        status="research_ready" if qualified else "partial",
        decision_status="feasible_selected" if qualified and qualified["eligible"] else "risk_recovery" if qualified else "preselected_policy_not_independently_qualified",
        preselected_policy_id=identity, selection_optimizer=selection, independent_preselection_enforced=True,
        selection_scope="full_source_menu_selection_solver_optimum_then_only_frozen_winner_checked_on_independent_calibration")
    if qualified is None:
        result.update(reason="full_scope_selection_winner_failed_independent_qualification",
            required_actions=[{"action": "complete_frozen_winner_independent_source_calibration_or_observe_without_adjustment",
                               "do_not_choose_another_policy_after_calibration": True}])
    result["result_hash"] = fingerprint({key:value for key,value in result.items() if key != "result_hash"})
    return result
