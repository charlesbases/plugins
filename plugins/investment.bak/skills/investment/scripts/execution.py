"""Shared order compiler and deterministic simulation event adapter; no IO."""
import copy
import datetime as dt
from zoneinfo import ZoneInfo
from decimal import Decimal, localcontext, ROUND_DOWN, ROUND_HALF_UP

import ledger
from contracts import ContractError, fields, fingerprint, instant, require


def _floor(value, step):
    return (value/step).to_integral_value(rounding=ROUND_DOWN)*step


def _redemption_requests(orders):
    groups = {}
    for order in orders:
        if order["side"] == "sell":
            group = groups.setdefault(order["code"],{"code":order["code"],"shares":Decimal(0),"lot_allocations":[],"ledger_order_ids":[]})
            group["shares"] += Decimal(order["share_limit"])
            group["lot_allocations"].append({"lot_id":order["lot_id"],"shares":order["share_limit"]})
            group["ledger_order_ids"].append(order["order_id"])
    return [{**row,"shares":ledger.decimal(row["shares"]),"request_kind":"single_fund_redemption_with_source_lot_allocations"} for row in groups.values()]


def compile_mpc_orders(result, context, data):
    """Publish only the source-qualified zero-funding MPC current action."""
    require(fingerprint({k: v for k, v in context.items() if k != "context_hash"}) == context["context_hash"], "Decision context changed")
    mpc = result["mpc"]
    require(mpc["context_hash"] == context["context_hash"], "MPC financial context differs")
    output = {"status": "blocked", "orders": [], "waiting": [], "funding_options": mpc.get("funding_options", []),
        "selected_candidate_id": None, "selected_plan_kind": "none", "trade_calibration": mpc.get("calibration", {}),
        "selected_plan_scope": "current_action_reoptimize_after_actual_confirmation"}
    if context["blocked"] or result["status"] != "research_ready" or mpc["status"] != "research_ready":
        return {**output, "reason": result.get("reason", mpc.get("reason", "source_path_qualification_required"))}
    from newtrade_guard import qualification_scope
    governance = qualification_scope(context["spec"]["trade_policy"], context["decision_at"],
                                     context.get("trade_family_review_index"))
    if not governance["eligible"]:
        return {**output, "reason": "registered_trade_governance_required", "qualification_scope": governance}
    selected = mpc["selected_policy"]
    if selected is None:
        return {**output, "status": "no_action", "reason": "no_qualified_current_policy"}
    recovery = mpc.get("decision_status") == "risk_recovery" and selected.get("recovery_eligible") is True
    require(selected["proposed_contribution"] == 0 and (selected["eligible"] or recovery), "Unconfirmed funding or ineligible MPC in current orders")
    if recovery:
        require(not selected["eligible"] and selected["current_action"]["sells"] and not selected["current_action"]["buys"]
            and not selected["future_rule"]["weights"] and not selected["future_rule"].get("conditional_sells"), "Risk recovery must remain a separately qualified pure redemption")
    action = selected["current_action"]
    orders, _, _ = _round_trades(action, context, output["waiting"])
    compiled = {"buys": [{"code": o["code"], "cash_debit": o["cash_limit"]} for o in orders if o["side"] == "buy"],
        "sells": [{"lot_id": o["lot_id"], "shares": o["share_limit"]} for o in orders if o["side"] == "sell"]}
    require(fingerprint(compiled) == fingerprint(action), "Current precision changed after MPC policy freeze")
    for order in orders:
        ledger.validate_order(order)
    for group in mpc["funding_options"]:
        if group["proposed_contribution"] > 0:
            output["waiting"].append({"condition": "additional_cash_confirmation_then_new_decision", "proposed_contribution": str(group["proposed_contribution"])})
    if selected["future_rule"].get("conditional_sells"):
        output["waiting"].append({"condition": "actual_observable_zero_gain_condition_then_new_analysis",
            "valuation_only_future_rule": selected["future_rule"], "future_order_created": False})
    if selected["future_rule"]["weights"]:
        output["waiting"].append({"condition": "actual_settlement_and_confirmation_then_new_analysis", "valuation_only_future_rule": selected["future_rule"], "future_order_created": False})
    from report_validation import reference_valuation_invariants
    valuation = copy.deepcopy(selected["reference_valuation"])
    reference_valuation_invariants(context, orders, valuation)
    return {**output, "status": "ready" if orders else "no_action", "orders": orders,
        "redemption_requests": _redemption_requests(orders), "reference_valuation": valuation,
        "selected_candidate_id": selected["id"], "selected_plan_kind": "risk_recovery" if recovery else "rolling_mpc_current_action",
        "decision_status": mpc.get("decision_status"), "risk_status": "risk_not_restored" if recovery else "within_verified_budget",
        "trade_guard": selected["trade_guard"], "mpc_verification": {"policy_id": selected["id"], "result_hash": mpc["result_hash"], "path_hash": result["paths"]["path_hash"], "point_valuation_hash": selected["point"]["valuation_hash"], "future_trades_are_orders": False},
        "rounding_verification": {"scope": "exact_current_source_cash_share_projection", "projection": selected["current_projection"]}}


def compile_orders(result, context, data):
    """Sole current production decision contract, with no old-policy fallback."""
    require("mpc" in result and "paths" in result, "Source-qualified rolling MPC required")
    return compile_mpc_orders(result, context, data)


def _round_trades(candidate, context, waiting):
    """Compile already source-quantized cash/share legs without reinterpreting units."""
    from allocation import validate_source_action
    validate_source_action(context,candidate)
    spec, request = context["spec"], context["model_request"]
    assets = {row["code"]: row for row in request["assets"]}
    lots = {row["lot_id"]: row for row in context["snapshot"]["positions"]}
    free_cash = Decimal(context["snapshot"]["available_cash"])
    orders, sale_values, buy_values = [], {}, {}
    with localcontext() as precision:
        precision.prec = 50
        for index, trade in enumerate(candidate["sells"]):
            identity = trade["lot_id"]
            require(identity in lots and identity not in sale_values, "Sale refers to an unknown, reserved or duplicate lot")
            lot, asset = lots[identity], assets[lots[identity]["code"]]
            require(asset["sellable"], "Sale is not allowed by current terms")
            price = Decimal(context["snapshot"]["prices"][lot["code"]])
            contract = context["fee_contracts"][lot["code"]]
            lot_step = Decimal(contract["trade_precision"]["share_step"])
            shares = Decimal(str(trade["shares"]))
            require(shares == _floor(shares,lot_step), "Source share quantity changed during compilation")
            order = {"order_id": "order-"+fingerprint([context["context_hash"], "sell", identity, index])[:32],
                     "code": lot["code"], "side": "sell", "currency": spec["currency"], "lot_id": identity,
                     "cash_limit": "0", "share_limit": ledger.decimal(shares), "fee_rate": context["lot_terms"][identity]["redemption_fee"],
                     "settlement_days": asset["settlement_days"], "share_step": ledger.decimal(lot_step), "context_hash": context["context_hash"], "fee_contract": copy.deepcopy(contract)}
            if context["lot_terms"][identity]["redemption_schedule"] is not None:
                order["redemption_schedule"] = copy.deepcopy(context["lot_terms"][identity]["redemption_schedule"])
            orders.append(order); sale_values[identity] = str(shares*price)
        for index, trade in enumerate(candidate["buys"]):
            code = trade["code"]
            require(code in context["purchase_eligible_codes"], "Purchase is excluded by the bound purchase eligibility")
            require(code in assets and code not in buy_values, "Unknown or duplicate buy code")
            asset = assets[code]
            contract = context["fee_contracts"][code]
            debit = Decimal(str(trade["cash_debit"]))
            require(asset["buy_allowed"], "Purchase is excluded by the bound purchase eligibility")
            require(asset["buyable"] and debit >= Decimal(str(asset["min_buy"])), "Rounded buy violates current minimum")
            require(debit <= free_cash and ("max_buy" not in asset or debit <= Decimal(str(asset["max_buy"]))), "Rounded buy exceeds settled budget or maximum")
            require(not any(order["code"] == code for order in orders), "Cannot buy and sell the same fund")
            free_cash -= debit
            order = {"order_id": "order-"+fingerprint([context["context_hash"], "buy", code, index])[:32],
                     "code": code, "side": "buy", "currency": spec["currency"], "lot_id": None,
                     "cash_limit": ledger.decimal(debit), "share_limit": "0", "fee_rate": ledger.decimal(Decimal(str(asset["subscription_fee"]))),
                     "settlement_days": asset["settlement_days"], "share_step": contract["trade_precision"]["share_step"], "context_hash": context["context_hash"], "fee_contract": copy.deepcopy(contract)}
            orders.append(order); buy_values[code] = str(debit)
    return orders, sale_values, buy_values




def compile_benchmark(spec, state, decision_at, market_ref, *, account_id, initial_allocation=False):
    """Predeclared fixed weights, calendar and costs; no prediction-model input."""
    import strategy
    from zoneinfo import ZoneInfo
    spec = strategy.validate_spec(spec)
    cutoff = instant(decision_at)
    require(market_ref.get("schema_version") == 4 and market_ref.get("currency") == spec["currency"],
            "Benchmark market contract differs")
    require(0 <= (cutoff-instant(market_ref["observed_at"])).total_seconds() <= spec["availability"]["max_market_age_seconds"],
            "Benchmark market is future or stale")
    codes = set(spec["benchmark"]["weights"]) | {row["code"] for row in state["lots"].values()}
    require(codes <= set(market_ref["prices"]) and bool(market_ref["source_hashes"]), "Benchmark monitoring prices are incomplete")
    require(market_ref.get("terms_basis") == "source_verified", "Benchmark requires source-verified dealing terms")
    require(codes <= set(market_ref["price_dates"]) and all(market_ref["currencies"][code] == spec["currency"] for code in codes), "Benchmark date/currency evidence incomplete")
    snapshot = ledger.snapshot(state, decision_at, market_ref["prices"], market_ref["price_dates"])
    require(not snapshot["blocked"], "Benchmark account requires reconciliation")
    assets, raw_terms = {}, {}
    for item in market_ref["terms"]:
        if item["code"] not in codes:
            continue
        raw_terms[item["code"]] = item
        require(item["code"] not in assets, "Duplicate benchmark asset terms")
        require(0 <= (cutoff-instant(item["observed_at"])).total_seconds() <= spec["availability"]["max_terms_age_seconds"],
                "Benchmark terms are future or stale")
        require(type(item["buyable"]) is bool and type(item["sellable"]) is bool, "Benchmark trading status required")
        assets[item["code"]] = {**item, "buy_allowed": item["code"] in spec["benchmark"]["weights"], **{key: float(item[key]) for key in
              ("subscription_fee", "redemption_fee", "min_buy", "max_buy") if key in item}}
        ledger.redemption_rate(item, decision_at, decision_at)
    require(set(assets) == codes, "Benchmark terms do not cover its independent universe")
    day = cutoff.astimezone(ZoneInfo(spec["timing"]["timezone"])).date().isoformat()
    context = {"spec": spec, "snapshot": snapshot, "decision_at": decision_at,
               "purchase_eligible_codes": list(spec["benchmark"]["weights"]),
               "fee_contracts": {code: item["fee_contract"] for code, item in raw_terms.items()},
               "model_request": {"assets": list(assets.values())},
               "lot_terms": {lot["lot_id"]: {"redemption_fee": ledger.redemption_rate(raw_terms[lot["code"]], lot["acquired_at"], decision_at),
                                            "redemption_schedule": copy.deepcopy(assets[lot["code"]].get("redemption_schedule"))}
                             for lot in snapshot["positions"]}}
    context["context_hash"] = fingerprint({"spec_hash": fingerprint(spec), "account_id": account_id,
        "account_hash": fingerprint(state), "decision_at": decision_at, "market_ref": market_ref,
        "initial_allocation": initial_allocation, "role": "fixed_weight_benchmark"})
    output = {"status": "no_action", "orders": [], "waiting": [], "context_hash": context["context_hash"],
              "benchmark_rule": "fixed_weights", "rule_hash": fingerprint(spec["benchmark"]),
              "initial_allocation": initial_allocation, "date": day,
              "scope": "predeclared_passive_weights_same_ledger_costs"}
    if not initial_allocation and day not in spec["benchmark"]["rebalance_dates"]:
        return output
    equity, free = Decimal(snapshot["equity"]), Decimal(snapshot["available_cash"])
    weights = spec["benchmark"]["weights"]
    held = {code: sum((Decimal(lot["value"]) for lot in snapshot["positions"] if lot["code"] == code), Decimal(0))
            for code in codes}
    candidate = {"buys": [], "sells": []}
    desired_buys = {}
    for code in sorted(codes):
        asset = assets[code]
        weight = Decimal(weights.get(code, "0"))
        require(weight <= Decimal(str(asset["max_weight"])), "Benchmark target exceeds platform concentration limit")
        target = equity * weight
        excess = held[code]-target
        if excess > 0:
            from allocation import _split_redemption
            import fee_contract
            lots = [lot for lot in snapshot["positions"] if lot["code"] == code]
            total = sum((Decimal(lot["shares"])-Decimal(lot["reserved_shares"]) for lot in lots),Decimal(0))
            contract = context["fee_contracts"][code]
            quantity = min(total,_floor(excess/Decimal(snapshot["prices"][code]),Decimal(contract["trade_precision"]["share_step"])))
            method = contract.get("redemption_allocation",{}).get("method")
            if asset["sellable"] and quantity > 0:
                if len(lots) == 1 or quantity == total or method in {"fifo","lifo","specific_lot"}:
                    candidate["sells"] += _split_redemption(lots,quantity,method if method in {"fifo","lifo"} else "fifo")
                    excess -= quantity*Decimal(snapshot["prices"][code])
                else:
                    output["waiting"].append({"condition":"source_redemption_lot_allocation_required","code":code})
            if excess > 0:
                output["waiting"].append({"condition":"benchmark_unsellable_reserved_or_source_precision_excess","code":code})
        elif excess < 0 and asset["buyable"]:
            desired_buys[code] = min(-excess*(1+Decimal(str(asset["subscription_fee"]))),
                                    Decimal(str(asset.get("max_buy", equity))))
        elif excess < 0:
            output["waiting"].append({"condition": "benchmark_subscription_unavailable", "code": code})
    budget = max(Decimal(0), free-equity*Decimal(spec["benchmark"]["cash_weight"]))
    needed = sum(desired_buys.values(), Decimal(0))
    scale = min(Decimal(1), budget/needed) if needed else Decimal(0)
    for code, desired in desired_buys.items():
        step = Decimal(context["fee_contracts"][code]["trade_precision"]["money_step"])
        debit = _floor(desired*scale, step)
        if debit >= Decimal(str(assets[code]["min_buy"])) and debit > 0:
            candidate["buys"].append({"code": code, "cash_debit": str(debit)})
        elif desired > 0:
            output["waiting"].append({"condition": "benchmark_minimum_or_settled_cash", "code": code})
    if scale < 1 and candidate["sells"]:
        output["waiting"].append({"condition": "benchmark_sale_settlement_then_next_scheduled_rebalance"})
    try:
        orders, _, _ = _round_trades(candidate, context, output["waiting"])
    except ContractError as error:
        return {**output,"status":"blocked","reason":str(error),"orders":[]}
    for order in orders:
        ledger.validate_order(order)
    return {**output, "status": "ready" if orders else "no_action", "orders": orders,"redemption_requests":_redemption_requests(orders)}


def reserve_events(state, orders, at):
    require(type(orders) is list, "orders must be the compiled order list")
    events, current = [], state
    for order in orders:
        event = {"id": "reserve:"+order["order_id"], "type": "order_reserved", "sequence": current["sequence"]+1,
                 "effective_at": at, "known_at": at, "recorded_at": at, "data": {"order": order}}
        current = ledger.apply_event(current, event); events.append(event)
    return events


def _simulation_view(state, at, allow_economic_view):
    current = copy.deepcopy(state)
    if current["projection_cutoff"] is not None:
        require(allow_economic_view and instant(current["projection_cutoff"]["effective_as_of"]) == instant(at),
                "Economic view simulation requires its exact cutoff and explicit permission")
        current["projection_cutoff"] = None
    return current


def simulate_events(state, orders, prices, effective_at, known_at=None, recorded_at=None, *, price_dates, allow_economic_view=False):
    """Generate only deterministic full fills of previously reserved orders."""
    supplied_known, supplied_recorded = known_at, recorded_at
    known_at, recorded_at = known_at or effective_at, recorded_at or known_at or effective_at
    events, current = [], _simulation_view(state, effective_at, allow_economic_view)
    with localcontext() as precision:
        precision.prec = 50
        for original in orders:
            event_known, event_recorded = known_at, recorded_at
            require(original["order_id"] in current["orders"], "Simulation order was not reserved")
            order = current["orders"][original["order_id"]]
            import fee_contract
            import trading_calendar
            contract = order["fee_contract"]
            price, step = Decimal(prices[order["code"]]), Decimal(order["share_step"])
            require(order["code"] in price_dates, "Execution NAV date evidence required")
            require(price > 0, "Execution NAV must be positive")
            if order["side"] == "buy":
                quote = fee_contract.quote_entry(order["remaining_cash"], contract, price=price, share_step=step)
                shares = quote["shares"]
            else:
                shares = Decimal(order["remaining_shares"])
            require(shares > 0, "Execution rounding produced zero shares")
            if order["side"] == "buy":
                gross,fee = quote["gross"],quote["fee"]
            else:
                price_day = dt.date.fromisoformat(price_dates[order["code"]])
                priced_stamp = dt.datetime.combine(price_day, dt.time(), ZoneInfo("Asia/Shanghai")).isoformat()
                confirmation_stamp = None
                if contract["holding"]["end_event"] == "confirmation_date":
                    confirmation_day = fee_contract.normal_confirmation_end(contract, price_day)
                    confirmation_stamp = dt.datetime.combine(confirmation_day, dt.time(), ZoneInfo("Asia/Shanghai")).isoformat()
                    if instant(event_known) < instant(confirmation_stamp):
                        require(supplied_known is None, "Modeled redemption confirmation follows the supplied knowledge timestamp")
                        event_known = confirmation_stamp
                    if instant(event_recorded) < instant(event_known):
                        require(supplied_recorded is None, "Modeled redemption confirmation follows the supplied recording timestamp")
                        event_recorded = event_known
                exit_quote = fee_contract.quote_exit_details(shares*price,contract,
                    acquired_at=current["lots"][order["lot_id"]]["acquired_at"],execution_at=priced_stamp,
                    confirmed_at=confirmation_stamp)
                gross,fee = exit_quote["gross"],exit_quote["contractual_fee"]
            fill_id = "fill:"+fingerprint([order["order_id"], effective_at])[:32]
            data = {"order_id": order["order_id"], "fill_id": fill_id, "shares": ledger.decimal(shares),
                    "price": ledger.decimal(price), "price_date": price_dates[order["code"]], "fee": ledger.decimal(fee), "final": True}
            if order["side"] == "buy":
                data["lot_id"] = "lot:"+fill_id
                data["gross_amount"] = ledger.decimal(quote["gross"])
                data["cash_debit"] = ledger.decimal(quote["debit"])
                rule = contract["acquisition_rule"]
                require(rule["holding_start"] in ("execution_date", "confirmation_date") and rule["ownership_start"] in ("execution_date", "confirmation_date"), "Unsupported acquisition/entitlement rule")
                data["holding_started_at"] = effective_at if rule["holding_start"] == "execution_date" else event_known
                data["ownership_at"] = effective_at if rule["ownership_start"] == "execution_date" else event_known
            else:
                data["gross_amount"] = ledger.decimal(gross)
                data["net_amount"] = ledger.decimal(exit_quote["net"])
                if confirmation_stamp is not None:
                    data["actual_redemption_confirmed_at"] = confirmation_stamp
                settlement = contract["settlement"]
                payment_start = confirmation_day if confirmation_stamp is not None else price_day
                day = trading_calendar.advance(payment_start, settlement["lag_days"], settlement["day_basis"], settlement.get("calendar"))
                data["settlement_at"] = instant(effective_at).replace(year=day.year, month=day.month, day=day.day).isoformat()
            event = {"id": "sim:"+fill_id, "type": order["side"]+"_fill", "sequence": current["sequence"]+1,
                     "effective_at": effective_at, "known_at": event_known, "recorded_at": event_recorded, "data": data}
            current = ledger.apply_event(current, event); events.append(event)
    return events


def settlement_events(state, at, known_at=None, recorded_at=None, *, allow_economic_view=False):
    """Deterministic simulation-only due-payment adapter; real payments need evidence."""
    events, current = [], _simulation_view(state, at, allow_economic_view)
    known_at, recorded_at = known_at or at, recorded_at or known_at or at
    for identity, row in sorted(state["receivables"].items()):
        if instant(row["due_at"]) <= instant(at):
            event = {"id": "sim-settlement:"+identity, "type": "dividend_paid" if row["kind"] == "dividend" else "settlement",
                     "sequence": current["sequence"]+1, "effective_at": at, "known_at": known_at, "recorded_at": recorded_at,
                     "data": {"receivable_id": identity, "amount": row["amount"]}}
            current = ledger.apply_event(current, event); events.append(event)
    return events
