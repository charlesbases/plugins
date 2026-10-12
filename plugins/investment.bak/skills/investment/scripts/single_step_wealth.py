"""Exact source-fee references and direct single executable decision wealth."""
import copy
import datetime as dt
import math
from decimal import Decimal, ROUND_HALF_UP

from contracts import require, instant, fingerprint
import fee_contract as fees
import trading_calendar
import ledger


def exposure_check(values, capital, identities, constraints, sector_bounds=None):
    groups, sectors, unknown = {}, {}, []
    for code, value in values.items():
        identity = identities[code]
        group = identity.get("fund_group_id")
        require(group, "Verified underlying fund identity required")
        groups[group] = groups.get(group, 0.) + float(value)
        for sector in constraints["sector_limits"]:
            upper = (sector_bounds or {}).get(code, {}).get(sector, {}).get("upper")
            if upper is None:
                if float(value) > 0:
                    unknown.append(code+":"+sector)
                continue
            require(math.isfinite(float(upper)) and float(upper) >= 0, "Invalid gross NAV exposure upper")
            sectors[sector] = sectors.get(sector, 0.) + float(value)*float(upper)
    failures = []
    for key, values_by_id in (("fund_group_limits", groups), ("sector_limits", sectors)):
        for identity, limit in constraints[key].items():
            if values_by_id.get(identity, 0.) > float(capital) * limit + 1e-7:
                failures.append(key + ":" + identity)
    failures += ["unknown_sector_upper:"+name for name in sorted(set(unknown))]
    return {"feasible": not failures, "violations": failures,
            "scope": "configured_limits_only" if any(constraints.values()) else "unconstrained",
            "fund_group_values": groups, "sector_upper_values": sectors, "unknown_sector_codes": unknown}


def project(context, candidate, distribution, *, pricing_nav=None):
    """Rebuild exact monetary/share trades at the current known mark."""
    from allocation import _cvar, validate_source_action
    validate_source_action(context, {"buys": candidate["buys"], "sells": candidate["sells"]})
    snapshot = context["snapshot"]
    require(pricing_nav is not None or context.get("price_schema") != "source_role_price_paths_v3",
            "Current role-price projection requires scenario execution NAV")
    quote_prices = pricing_nav if pricing_nav is not None else snapshot["prices"]
    require(set(quote_prices) == set(context["allocation_codes"]), "Complete scenario execution price primitives required")
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    lots = {row["lot_id"]: copy.deepcopy(row) for row in snapshot["positions"]}
    cash = Decimal(snapshot["available_cash"])
    reserved, receivable = Decimal(snapshot["reserved_cash"]), Decimal(snapshot["unsettled_cash"])
    buys, sells, total_fees = [], [], Decimal(0)
    sold_codes = set()
    for trade in candidate["sells"]:
        identity = trade["lot_id"]
        require(identity in lots, "Unknown redemption lot")
        lot = lots[identity]
        code = lot["code"]
        terms = context["fee_contracts"][code]
        price = Decimal(str(quote_prices[code]))
        quantity = Decimal(trade["shares"])
        if quantity == 0:
            continue
        available = Decimal(lot["shares"]) - Decimal(lot["reserved_shares"])
        require(assets[code]["sellable"] and quantity <= available, "Redemption exceeds currently eligible shares")
        details = fees.quote_exit_details(quantity * price, terms, acquired_at=lot["acquired_at"], execution_at=context["decision_at"])
        gross, fee = details["gross"], details["effective_cost"]
        receivable += details["net"]
        total_fees += fee
        lot["shares"] = ledger.decimal(Decimal(lot["shares"]) - quantity)
        sold_codes.add(code)
        sells.append({"lot_id": identity, "code": code, "shares": ledger.decimal(quantity), "gross_value": float(gross),
                      "fee": float(fee), "contractual_fee": float(details["contractual_fee"]),
                      "rounding_loss": float(details["rounding_loss"]), "expected_net_proceeds": float(details["net"])})
    for index, trade in enumerate(candidate["buys"]):
        code, debit = trade["code"], Decimal(str(trade["cash_debit"]))
        require(code in assets and code not in sold_codes and assets[code]["buy_allowed"] and assets[code]["buyable"], "Ineligible current-stage purchase")
        terms = context["fee_contracts"][code]
        debit = fees.floor(debit, Decimal(terms["trade_precision"]["money_step"]))
        require(Decimal(str(assets[code]["min_buy"])) <= debit <= cash, "Purchase violates minimum or settled cash")
        require("max_buy" not in assets[code] or debit <= Decimal(str(assets[code]["max_buy"])), "Purchase exceeds limit")
        quote = fees.quote_entry(debit, terms, price=quote_prices[code], share_step=terms["trade_precision"]["share_step"])
        require(quote["shares"] > 0, "Purchase rounds to zero shares")
        cash -= quote["debit"]
        total_fees += quote["fee"]
        acquired = quote["shares"] * Decimal(str(quote_prices[code]))
        buys.append({"code": code, "cash_debit": float(quote["debit"]), "cash_limit": ledger.decimal(debit),
                     "acquired_value": float(acquired), "shares": ledger.decimal(quote["shares"]), "fee": float(quote["fee"]),
                     "unspent_cash": ledger.decimal(quote["remainder"])})
        lots["projection:" + str(index)] = {"code": code, "shares": ledger.decimal(quote["shares"]), "acquired_at": None,
            "holding_started_at_bounds": fees.holding_start_bounds(terms, context["decision_at"]),
            "holding_clock_basis": terms["acquisition_rule"]["holding_start"], "reserved_shares": "0"}
    values = {code: sum((Decimal(lot["shares"]) * Decimal(str(quote_prices[code])) for lot in lots.values() if lot["code"] == code), Decimal(0)) for code in context["allocation_codes"]}
    capital = Decimal(snapshot["equity"])
    profits = [float(cash + reserved + receivable + sum(values[code] * (1 + Decimal(str(row[distribution["codes"].index(code)]))) for code in values) - capital) for row in distribution["returns"]]
    probabilities = distribution["probabilities"]
    tail = _cvar([-value for value in profits], probabilities, context["spec"]["allocation"]["tail_probability"])
    exposure = exposure_check(values, capital, context["identities"], context["spec"]["constraints"], context.get("sector_exposure_bounds"))
    concentration = all(value <= Decimal(str(assets[code]["max_weight"])) * capital for code, value in values.items()) and exposure["feasible"]
    return {"buys": buys, "sells": sells, "lots": list(lots.values()), "values": {code: ledger.decimal(value) for code, value in values.items()},
            "pricing_nav": {code: str(value) for code, value in quote_prices.items()},
            "cash": ledger.decimal(cash + reserved), "receivables": ledger.decimal(receivable), "fees": float(total_fees),
            "scenario_profits": profits, "absolute_cvar": tail, "expected_profit": sum(p * v for p, v in zip(probabilities, profits)),
            "risk_feasible": tail <= float(context["risk_state"]["remaining_loss_budget"]) + 1e-7,
            "concentration_feasible": concentration, "exposure": exposure,
            "turnover": sum(row["cash_debit"] for row in buys) + sum(row["gross_value"] for row in sells)}


def exit_bounds(context, projection, distribution, horizon_days):
    """Bounds for a separate specified exit date; no horizon probabilities."""
    from allocation import _cvar
    date = instant(context["decision_at"]) + dt.timedelta(days=horizon_days)
    codes = distribution["codes"]
    wealth, wealth_upper, fees_upper, cash_lower = [], [], [], []
    for row in distribution["returns"]:
        fees_total = Decimal(0)
        initial_cash = Decimal(projection["cash"])
        economic = initial_cash + Decimal(projection["receivables"])
        for lot in projection["lots"]:
            code = lot["code"]
            total = Decimal(lot["shares"]) * Decimal(context["snapshot"]["prices"][code]) * (1 + Decimal(str(row[codes.index(code)])))
            require(total >= 0, "Negative terminal total wealth")
            terms = context["fee_contracts"][code]
            if lot.get("holding_started_at_bounds"):
                bounds = lot["holding_started_at_bounds"]
            else:
                bounds = {"earliest": lot["acquired_at"], "latest": lot["acquired_at"]}
            ages = fees.holding_ages_at_exit(terms, bounds, date.isoformat())
            require(min(ages) >= terms["holding"]["minimum_days"], "Exit horizon may precede the source-confirmed lot unlock")
            _, upper = fees.fee_bounds(total, terms, ages)
            economic += total
            fees_total += upper + (Decimal(".005") if total > 0 else Decimal(0))
        wealth.append(float(economic - fees_total))
        wealth_upper.append(float(economic))
        fees_upper.append(float(fees_total))
        # With only terminal total return, future dividend payment dates and
        # redemption settlement are not proved. Existing settled cash is a
        # valid bound; principal/NAV and receivables cannot become spendable.
        cash_lower.append(float(initial_cash))
    capital = float(context["snapshot"]["equity"])
    losses = [capital - value for value in wealth]
    return {"horizon_days": horizon_days, "goal": "redeem", "wealth_lower": wealth, "wealth_upper": wealth_upper,
            "fee_upper": fees_upper, "cash_available_lower": cash_lower,
            "absolute_cvar_upper": _cvar(losses, distribution["probabilities"], context["spec"]["allocation"]["tail_probability"]),
            "scope": "conditional_fee_bound_over_unknown_NAV_dividend_decomposition",
            "future_rate_assumption": "current_source_terms_continue",
            "consideration_rounding_loss_upper_per_nonzero_lot": "0.005",
            "cash_deadline_verified": False, "requires_horizon_aligned_distribution": True}



def build_clock_context(context):
    """Current source clocks; no promise of an unconditional fill/arrival time."""
    from zoneinfo import ZoneInfo
    local = instant(context["decision_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
    evaluation = local.date()+dt.timedelta(days=context["spec"]["planning"]["primary_horizon_days"])
    contracts = {code: context["fee_contracts"][code] for code in context["allocation_codes"]}
    calendars = [terms["execution_calendar"] for terms in contracts.values()]
    evaluation = dt.date.fromisoformat(trading_calendar.on_or_after(evaluation, trading_calendar.intersection(calendars)))
    assets = {}
    for code, terms in contracts.items():
        require("confirmation" in terms and "order_cutoff_local" in terms,
                "Source normal pricing/confirmation rules are required")
        priced = fees.pricing_date(terms, context["decision_at"])
        confirmed = fees.normal_confirmation_end(terms, priced)
        rule = terms["acquisition_rule"]
        first_owned = priced
        last_owned = priced if rule["ownership_start"] == "execution_date" else confirmed
        starts = fees.holding_start_bounds(terms, context["decision_at"])
        if rule["holding_start"] == "execution_date":
            stamp = dt.datetime.combine(priced, dt.time(), ZoneInfo("Asia/Shanghai")).isoformat()
            starts = {"earliest": stamp, "latest": stamp, "scope": "normal_source_pricing_date"}
        assets[code] = {"pricing_date": str(priced), "confirmation_date": str(confirmed),
            "ownership_date_bounds": {"earliest": str(first_owned), "latest": str(last_owned)},
            "holding_start_bounds": starts, "holding_start": rule["holding_start"], "ownership_start": rule["ownership_start"],
            "confirmation_rule": copy.deepcopy(terms["confirmation"]),
            "after_cutoff": local.time().replace(tzinfo=None) >= dt.time.fromisoformat(terms["order_cutoff_local"]),
            "normal_processing_only": True, "source_terms_hash": fingerprint(terms)}
    return {"evaluation_date": str(evaluation), "assets": assets, "normal_processing_only": True,
            "order_time_local": local.time().replace(tzinfo=None).isoformat(),
            "historical_schedule_basis": "source_normal_rule_on_observed_NAV_sessions_not_archived_actual_fills",
            "terminal_goal": context["spec"]["planning"]["primary_goal"]}


def product_cost_comparison(context, reference_amount=None):
    """Entry and exit cost references at exact holding ages, never order dates."""
    reference = Decimal(context["snapshot"]["equity"]) if reference_amount is None else fees.number(reference_amount)
    output = {"reference_amount": str(reference),
        "reference_basis": "recorded_account_equity" if reference_amount is None else "explicit_same_amount",
        "scope": "entry_exit_cost_at_fixed_current_NAV_and_exact_holding_ages",
        "operating_cost_treatment": "published_NAV_already_net_of_operating_costs_no_second_deduction",
        "all_market_optimality_claimed": False, "actual_order": False, "products": []}
    if reference <= 0:
        return {**output, "status": "reference_amount_unavailable"}
    for code in context["universe"]:
        identity = context["identities"][code]
        row = {"code": code, "fund_group_id": identity["fund_group_id"], "share_class": identity.get("share_class"),
            "reference_amount": str(reference), "purchase_eligible": code in context["purchase_eligible_codes"], "horizons": []}
        if code not in context["fee_contracts"]:
            output["products"].append({**row, "status": "source_terms_incomplete"})
            continue
        terms = context["fee_contracts"][code]
        try:
            require(reference >= Decimal(terms["min_buy"]) and (terms["max_buy"] is None or reference <= Decimal(terms["max_buy"])),
                    "Reference amount violates source purchase limits")
            price = context["snapshot"]["prices"][code]
            entry = fees.quote_entry(reference, terms, price=price, share_step=terms["trade_precision"]["share_step"])
            gross = entry["shares"]*Decimal(price)
            row.update(entry_fee=str(entry["fee"]), confirmed_share_projection=str(entry["shares"]),
                unspent_cash=str(entry["remainder"]), current_NAV=price,
                current_NAV_date=context["market_ref"]["price_dates"][code])
            for age in fees.source_holding_ages(terms):
                if age < terms["holding"]["minimum_days"]:
                    row["horizons"].append({"holding_days": age, "status": "not_established",
                                           "reason": "holding_age_precedes_source_lock"})
                    continue
                details = fees.quote_exit_at_age_details(gross, terms, age)
                cost = details["effective_cost"]
                wealth = entry["remainder"]+details["net"]
                row["horizons"].append({"holding_days": age, "holding_age_basis": terms["holding"]["day_basis"],
                    "entry_fee": str(entry["fee"]), "exit_cost_upper": float(cost), "exit_cost": str(cost),
                    "contractual_exit_fee": str(details["contractual_fee"]), "exit_rounding_loss": str(details["rounding_loss"]),
                    "reference_net_wealth_lower": float(wealth), "reference_net_wealth_upper": float(wealth),
                    "cash_available_lower": 0., "future_terms_assumption": "current_source_terms_continue",
                    "cash_arrival_guaranteed": False})
            row["status"] = "conditional_cost_reference"
        except ValueError as exc:
            row.update(status="reference_not_applicable", reason=str(exc))
        output["products"].append(row)
    return {**output, "status": "calculated"}


def _rights_bounds(actions, owned, sold=None):
    low = high = Decimal(0)
    for event in actions:
        record = dt.date.fromisoformat(event["record_date"])
        rule = event.get("rights_rule", event.get("entitlement_rule", {}))
        if owned > record or (sold is not None and sold < record):
            continue
        certain = owned < record and (sold is None or sold > record)
        if owned == record:
            choice = rule.get("subscribe_on_record_date")
            if choice == "excluded":
                continue
            certain = choice == "included" and (sold is None or sold > record)
        if sold == record:
            choice = rule.get("redeem_on_record_date")
            if choice == "excluded":
                continue
            certain = owned < record and choice == "included"
        amount = Decimal(str(event["per_share"]))
        high += amount
        if certain:
            low += amount
    return low, high


def _terminal_fee(context, code, quantity, terminal_nav, started_bounds, evaluation):
    if context["spec"]["planning"]["primary_goal"] != "redeem" or quantity == 0:
        return Decimal(0), Decimal(0)
    terms = context["fee_contracts"][code]
    stamp = evaluation+"T09:00:00+08:00"
    ages = fees.holding_ages_at_exit(terms, started_bounds, stamp)
    require(ages and min(ages) >= terms["holding"]["minimum_days"], "Terminal redemption may precede lot unlock")
    gross = quantity*terminal_nav
    costs = [gross-fees.quote_exit_at_age_details(gross, terms, age)["net"] for age in ages]
    require(max(costs) <= gross, "Terminal fee exceeds NAV redemption proceeds")
    # Charge NAV proceeds only, never dividend economic wealth.
    return min(costs), max(costs)


def _targets_at(distribution, index):
    from allocation_market import TARGET_NAMES
    codes = distribution["codes"]
    require(set(distribution["targets"]) == set(TARGET_NAMES), "Complete direct targets required")
    result = {}
    for column, code in enumerate(codes):
        values = {name: Decimal(str(distribution["targets"][name][index][column])) for name in TARGET_NAMES}
        require(all(value.is_finite() and value > 0 for value in values.values()), "Nonpositive/nonfinite direct target")
        p, t, h, b, s = [values[name] for name in TARGET_NAMES]
        tolerance = Decimal("0.00000001")*max(Decimal(1), p, t, h, p*b, s)
        require(h+tolerance >= t and p*b+tolerance >= t and p*b <= h+tolerance
                and s+tolerance >= p and s-p <= h-t+tolerance, "Direct targets have incompatible NAV/dividend rights")
        result[code] = values
    return result


def value(context, projection, distribution):
    """One executable decision, then hold to one common economic endpoint.

    Exact share/cent fees use each scenario pricing NAV. No future reinvestment,
    zero-dividend or immediately available redemption cash is assumed.
    Incompatible scenarios retain probability with nonnegative-asset bounds.
    """
    from allocation import _cvar
    from zoneinfo import ZoneInfo
    clock = distribution["clock_context"]
    evaluation = clock["evaluation_date"]
    require(dt.date.fromisoformat(evaluation) >= dt.date.fromisoformat(context["as_of"]), "Past evaluation date")
    dates, probabilities = distribution["dates"], distribution["probabilities"]
    require(len(dates) == len(probabilities) and probabilities and all(math.isfinite(p) and p >= 0 for p in probabilities)
            and abs(sum(probabilities)-1) <= 1e-8, "Original aligned probability mass required")
    codes = distribution["codes"]
    require(type(codes) is list and len(codes) == len(set(codes)) and set(codes) == set(context["allocation_codes"]),
            "Direct target codes differ from this decision")
    require(all(type(matrix) is list and len(matrix) == len(probabilities)
                and all(type(row) is list and len(row) == len(codes) for row in matrix)
                for matrix in distribution["targets"].values()), "Every direct target row must retain aligned probability mass")
    require(fingerprint(clock) == fingerprint(build_clock_context(context)), "Direct target clocks differ from source-derived current clocks")
    capital = float(context["snapshot"]["equity"])
    sales = {row["lot_id"]: row for row in projection["sells"]}
    rows = []
    for index, (origin, probability) in enumerate(zip(dates, probabilities)):
        cash = Decimal(context["snapshot"]["cash"])
        receivable = Decimal(context["snapshot"]["unsettled_cash"])
        lower = upper = cash+receivable
        records, reasons = [], []
        try:
            target_values = _targets_at(distribution, index)
            for code, target in target_values.items():
                marked = Decimal(context["snapshot"]["prices"][code])
                known = [event for event in context.get("known_future_actions", [])
                         if event["code"] == code and context["market_ref"]["price_dates"][code] < event["ex_date"] <= evaluation]
                announced = sum((Decimal(str(event["per_share"])) for event in known), Decimal(0))
                cash_total = marked*(target["hold"]-target["terminal_nav"])
                require(cash_total+Decimal("0.00000001") >= announced,
                        "Forecast cash decomposition contradicts a known cash distribution")
            for lot in context["snapshot"]["positions"]:
                code, quantity = lot["code"], Decimal(lot["shares"])
                terms, target = context["fee_contracts"][code], target_values[code]
                marked = Decimal(context["snapshot"]["prices"][code])
                priced_nav, terminal_nav = marked*target["pricing"], marked*target["terminal_nav"]
                current_clock = clock["assets"][code]
                priced = dt.date.fromisoformat(current_clock["pricing_date"])
                require(priced <= dt.date.fromisoformat(evaluation), "Current sale prices after evaluation")
                owned = instant(lot.get("ownership_at", lot["acquired_at"])).astimezone(ZoneInfo("Asia/Shanghai")).date()
                marked_date = dt.date.fromisoformat(context["market_ref"]["price_dates"][code])
                known = [event for event in context.get("known_future_actions", [])
                         if event["code"] == code and str(marked_date) < event["ex_date"] <= evaluation]
                announced = sum((Decimal(str(event["per_share"])) for event in known), Decimal(0))
                dividend = marked*(target["hold"]-target["terminal_nav"])
                unannounced = max(Decimal(0), dividend-announced)
                held_low, held_high = _rights_bounds(known, owned)
                if owned <= marked_date:
                    held_low += unannounced
                held_high += unannounced
                sold = Decimal(sales.get(lot["lot_id"], {}).get("shares", "0"))
                require(0 <= sold <= quantity, "Sale quantity exceeds original shares")
                remaining = quantity-sold
                fee_low, fee_high = _terminal_fee(context, code, remaining, terminal_nav,
                    {"earliest": lot["acquired_at"], "latest": lot["acquired_at"]}, evaluation)
                terminal_gross = remaining*terminal_nav
                lower += terminal_gross+remaining*held_low-fee_high
                upper += terminal_gross+remaining*held_high-fee_low
                if sold:
                    sale_dividend = marked*(target["sell"]-target["pricing"])
                    known_low, known_high = _rights_bounds(known, owned, priced)
                    require(sale_dividend+Decimal("0.00000001") >= known_low
                            and sale_dividend <= known_high+unannounced+Decimal("0.00000001"),
                            "Sale target contradicts known record-date rights")
                    details = fees.quote_exit_details(sold*priced_nav, terms, acquired_at=lot["acquired_at"], execution_at=context["decision_at"])
                    gross, fee = details["gross"], details["effective_cost"]
                    lower += gross-fee+sold*sale_dividend
                    upper += gross-fee+sold*(sale_dividend+known_high-known_low)
                    records.append({"side": "sell", "code": code, "lot_id": lot["lot_id"], "shares": str(sold),
                        "pricing_NAV": str(priced_nav), "gross": str(gross), "fee": str(fee),
                        "economic_proceeds_lower": str(gross-fee+sold*sale_dividend), "spendable_now": False})
            for buy in projection["buys"]:
                code, target = buy["code"], target_values[buy["code"]]
                terms, current_clock = context["fee_contracts"][code], clock["assets"][code]
                marked = Decimal(context["snapshot"]["prices"][code])
                priced_nav, terminal_nav = marked*target["pricing"], marked*target["terminal_nav"]
                quote = fees.quote_entry(buy.get("cash_limit", buy["cash_debit"]), terms,
                                        price=priced_nav, share_step=terms["trade_precision"]["share_step"])
                require(quote["shares"] > 0, "Current subscription rounds to zero shares")
                require(current_clock["ownership_date_bounds"]["latest"] <= evaluation, "Current ownership follows evaluation")
                known = [event for event in context.get("known_future_actions", [])
                         if event["code"] == code and context["market_ref"]["price_dates"][code] < event["ex_date"] <= evaluation]
                announced = sum((Decimal(str(event["per_share"])) for event in known), Decimal(0))
                old_cash = marked*(target["hold"]-target["terminal_nav"])
                new_cash = priced_nav*target["buy"]-terminal_nav
                latest = dt.date.fromisoformat(current_clock["ownership_date_bounds"]["latest"])
                earliest = dt.date.fromisoformat(current_clock["ownership_date_bounds"]["earliest"])
                known_low, _ = _rights_bounds(known, latest)
                _, known_high = _rights_bounds(known, earliest)
                require(new_cash+Decimal("0.00000001") >= known_low
                        and new_cash <= known_high+max(Decimal(0), old_cash-announced)+Decimal("0.00000001"),
                        "Buy target contradicts known record-date rights")
                fee_low, fee_high = _terminal_fee(context, code, quote["shares"], terminal_nav,
                    current_clock["holding_start_bounds"], evaluation)
                gross = quote["shares"]*terminal_nav
                lower += gross+quote["shares"]*new_cash-fee_high-quote["debit"]
                upper += gross+quote["shares"]*(new_cash+known_high-known_low)-fee_low-quote["debit"]
                records.append({"side": "buy", "code": code, "shares": str(quote["shares"]), "pricing_NAV": str(priced_nav),
                    "gross": str(quote["gross"]), "fee": str(quote["fee"]), "cash_debit": str(quote["debit"]),
                    "terminal_NAV_wealth": str(gross), "economic_dividend_per_share_lower": str(new_cash)})
            modeled = True
        except (ValueError, KeyError, IndexError) as exc:
            modeled = False
            reasons.append(str(exc))
            lower = cash+receivable-sum((Decimal(str(row.get("cash_limit", row["cash_debit"]))) for row in projection["buys"]), Decimal(0))
            require(lower >= 0, "Current action spends unconfirmed or occupied cash")
            upper = None
        rows.append({"origin": origin, "probability": probability, "wealth_lower": float(lower),
            "wealth_upper": None if upper is None else float(upper), "targets_modeled": modeled,
            "trades": records if modeled else [], "reasons": reasons})
    wealth = [row["wealth_lower"] for row in rows]
    high = [row["wealth_upper"] for row in rows]
    profits = [amount-capital for amount in wealth]
    stress_cash = (Decimal(context["snapshot"]["cash"])+Decimal(context["snapshot"]["unsettled_cash"])
                   -sum((Decimal(str(row.get("cash_limit", row["cash_debit"]))) for row in projection["buys"]), Decimal(0)))
    principal_floor = context["risk_state"].get("principal_floor")
    stress = {"wealth_lower": float(stress_cash),
        "basis": "source_settled_and_recorded_receivable_cash_nonnegative_risky_asset_proceeds_zero_no_borrowing",
        "principal_floor": None if principal_floor is None else float(principal_floor),
        "principal_floor_survives": None if principal_floor is None else stress_cash >= Decimal(principal_floor),
        "probability_assigned": False, "scope": "support_and_execution_stress_not_future_probability_guarantee"}
    return {"capital": capital, "evaluation_date": evaluation, "wealth_lower": wealth, "wealth_upper": high,
        "source_principal_stress": stress,
        "scenario_profits_lower": profits, "probabilities": probabilities,
        "absolute_cvar_upper": _cvar([-amount for amount in profits], probabilities, context["spec"]["allocation"]["tail_probability"]),
        "expected_profit_lower": sum(p*profit for p, profit in zip(probabilities, profits)),
        "expected_profit_upper": None if any(amount is None for amount in high) else sum(p*(amount-capital) for p, amount in zip(probabilities, high)),
        "scenario_rows": rows,
        "qualification": "conditional_normal_processing_current_terms_continue" if all(row["targets_modeled"] for row in rows) else "conservative_support_bounds_with_unresolved_targets",
        "future_execution_guaranteed": False,
        "cash_available_lower": float(Decimal(context["snapshot"]["available_cash"])-sum((Decimal(str(row.get("cash_limit", row["cash_debit"]))) for row in projection["buys"]), Decimal(0))),
        "support_assumption": "nonnegative_NAV_no_leverage_fees_not_above_realized_proceeds"}
