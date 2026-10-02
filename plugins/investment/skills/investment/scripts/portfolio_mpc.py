"""Nonanticipative finite-policy MPC on independently sourced joint paths.

Future trades are valuation rules, never orders or financial ledger events.
The policy family is frozen before its independent error panel is inspected.
"""
import copy
import datetime as dt
import itertools
import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import fee_contract as fees
import ledger
import trading_calendar
import risk_numbers
from contracts import ContractError, fingerprint, instant, require

ZONE = ZoneInfo("Asia/Shanghai")
ZERO = Decimal(0)
CENT = Decimal("0.01")
END_OF_DAY = "23:59:59.999999"


class PolicyInfeasible(ValueError):
    """A valid source-defined policy violates a financial constraint."""


def _allowed(condition, reason):
    if not condition:
        raise PolicyInfeasible(reason)


def _decimal(value):
    result = Decimal(str(value))
    require(result.is_finite(), "Finite path/account amount required")
    return result


def _date(value):
    return dt.date.fromisoformat(value)


def _stamp(day, time):
    return dt.datetime.combine(_date(day), dt.time.fromisoformat(time), ZONE).isoformat()


def _price(path, code, day, *, execution=False):
    rows = path["nav"]
    if execution:
        require(day in rows and code in rows[day], "Missing exact source-modeled execution NAV date")
        value = rows[day][code]
    else:
        dates = [date for date in rows if date <= day and code in rows[date]]
        mark = path.get("known_marks",{}).get(code)
        require(bool(dates) or mark is not None and mark["nav_date"] <= day, "No observed-prefix mark for portfolio value")
        value = rows[max(dates)][code] if dates else mark["value"]
    value = _decimal(value)
    require(value > 0, "Positive modeled NAV required")
    return value


def _initial(context, funding):
    snap = context["snapshot"]
    _allowed(not snap.get("open_orders"), "Actual pending orders require confirmation before a new policy")
    lots = {row["lot_id"]: {**copy.deepcopy(row), "shares": _decimal(row["shares"]),
            "reserved_shares": _decimal(row["reserved_shares"]),
            "ownership_at": row.get("ownership_at", row["acquired_at"])} for row in snap["positions"]}
    existing = []
    identities = set()
    for row in snap.get("receivables", []):
        identity = row["receivable_id"]
        require(type(identity) is str and identity and identity not in identities
                and row["source_identity"] == identity and row["kind"] in {"redemption", "dividend"},
                "Confirmed receivable identity or kind differs")
        identities.add(identity)
        _allowed(row.get("due_at") is not None, "Confirmed receivable arrival needs source completion")
        pay_day, clock = ledger.receipt_clock(row["due_at"])
        value = ledger.amount(row["amount"], "confirmed receivable", 0)
        existing.append({**copy.deepcopy(row), "amount": value, "pay_date": str(pay_day),
                         "clock_precision": "date" if clock is None else "instant"})
    require(sum((row["amount"] for row in existing), ZERO) == _decimal(snap["unsettled_cash"]),
            "Confirmed receivable details differ from unsettled cash")
    if "receivables" in context:
        require(context["receivables"] == snap.get("receivables", []), "Context receivables differ from source snapshot")
    return {"cash": _decimal(snap["available_cash"])+funding,
            "reserved_cash": _decimal(snap["reserved_cash"]),
            "existing_receivables": existing,
            "lots": lots, "pending": [], "receivables": [], "segments": [],
            "entitlements": {row["source_identity"]: {"amount": row["amount"], "posted": True}
                             for row in existing if row["kind"] == "dividend"},
            "log": [], "fees": ZERO, "future_used": False, "order_sequence": 0}


def _receivable_total(state):
    return sum((row["amount"] for row in state["existing_receivables"]+state["receivables"]), ZERO)


def _existing_dividend_actions(context, paths):
    actions, seen = [], set()
    for receipt in context["snapshot"].get("receivables", []):
        if receipt["kind"] != "dividend":
            continue
        for path in paths:
            for event in path["distributions"]:
                if (event["record_date"] > context["as_of"] or event["id"] == receipt["source_identity"]
                        or receipt.get("code") is not None and receipt["code"] != event["code"]):
                    continue
                identity = (receipt["receivable_id"], event["code"])
                if identity not in seen:
                    seen.add(identity)
                    actions.append({"action": "reconcile_existing_dividend_source_identity",
                        "receivable_id": receipt["receivable_id"], "code": event["code"],
                        "reason": "source_binding_required_for_already_entitled_right"})
    return actions


def _values(state, path, day):
    values = {}
    for lot in state["lots"].values():
        values[lot["code"]] = values.get(lot["code"], ZERO)+lot["shares"]*_price(path, lot["code"], day)
    return values


def _equity(state, path, day):
    return state["cash"]+state["reserved_cash"]+_receivable_total(state)+sum(_values(state, path, day).values(), ZERO)


def _cash_event(state, day, kind, available_delta=ZERO, reserved_delta=ZERO, **details):
    before = state["cash"]
    state["cash"] += available_delta
    state["reserved_cash"] += reserved_delta
    _allowed(state["cash"] >= 0 and state["reserved_cash"] >= 0, "Policy spends unreceived or reserved cash")
    state["log"].append({"date": day, "kind": kind, "available_before": str(before),
        "available_delta": str(available_delta), "available_after": str(state["cash"]),
        "reserved_delta": str(reserved_delta), "reserved_after": str(state["reserved_cash"]), **details})


def _submit(context, state, action, day, time, path, *, hypothetical):
    staged = copy.deepcopy(state)
    _submit_staged(context, staged, action, day, time, path, hypothetical=hypothetical)
    state.clear()
    state.update(staged)


def _submit_staged(context, state, action, day, time, path, *, hypothetical):
    from allocation import validate_source_action
    source_context = {**context, "decision_at": _stamp(day, time),
        "snapshot": {**context["snapshot"], "available_cash": str(state["cash"]),
                     "positions": list(state["lots"].values())}}
    try:
        validate_source_action(source_context, action)
    except ContractError as error:
        raise PolicyInfeasible(str(error)) from error
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    sold = set()
    for trade in action["sells"]:
        identity = trade["lot_id"]
        _allowed(identity in state["lots"], "Unknown redemption lot")
        lot = state["lots"][identity]
        code, contract = lot["code"], context["fee_contracts"][lot["code"]]
        quantity = fees.floor(_decimal(trade["shares"]), _decimal(contract["trade_precision"]["share_step"]))
        _allowed(assets[code]["sellable"] and 0 < quantity <= lot["shares"]-lot["reserved_shares"], "Sale exceeds eligible shares")
        submission = _stamp(day, time)
        priced = fees.pricing_date(contract, submission)
        confirmed = fees.normal_confirmation_end(contract, priced)
        rule = contract["settlement"]
        # Deliberately wait through confirmation plus the disclosed normal lag.
        # Actual publication never counts this hypothesis as an available balance.
        settled = trading_calendar.advance(confirmed, rule["lag_days"], rule["day_basis"],
            rule.get("calendar", contract["execution_calendar"]))
        lot["reserved_shares"] += quantity
        state["order_sequence"] += 1
        state["pending"].append({"id": str(state["order_sequence"]), "side": "sell", "code": code, "lot_id": identity, "shares": quantity,
            "submission": submission, "pricing_date": str(priced), "confirmation_date": str(confirmed),
            "settlement_date": str(settled), "priced": False, "hypothetical_future": hypothetical})
        sold.add(code)
    for trade in action["buys"]:
        code = trade["code"]
        _allowed(code in context["purchase_eligible_codes"] and code not in sold, "Ineligible or same-stage reversal purchase")
        contract, asset = context["fee_contracts"][code], assets[code]
        debit = fees.floor(_decimal(trade["cash_debit"]), _decimal(contract["trade_precision"]["money_step"]))
        _allowed(asset["buyable"] and asset["buy_allowed"] and _decimal(asset["min_buy"]) <= debit <= state["cash"], "Buy exceeds settled cash or minimum")
        if asset.get("max_buy") is not None:
            _allowed(debit <= _decimal(asset["max_buy"]), "Buy exceeds source cap")
        submission = _stamp(day, time)
        priced = fees.pricing_date(contract, submission)
        confirmed = fees.normal_confirmation_end(contract, priced)
        _allowed(str(priced) in path["nav"] and code in path["nav"][str(priced)],
                 "Missing exact source-modeled execution NAV date")
        _allowed(str(confirmed) <= max(path["nav"]), "Purchase ownership follows modeled endpoint")
        _cash_event(state, day, "reserve_buy", -debit, debit, code=code, request=str(debit),
                    hypothetical_future=hypothetical, submitted_at=submission)
        state["order_sequence"] += 1
        state["pending"].append({"id": str(state["order_sequence"]), "side": "buy", "code": code, "cash_debit": debit,
            "submission": submission, "pricing_date": str(priced), "confirmation_date": str(confirmed),
            "priced": False, "hypothetical_future": hypothetical})


def _process_orders(context, state, path, day):
    for order in state["pending"]:
        contract = context["fee_contracts"][order["code"]]
        if not order["priced"] and order["pricing_date"] <= day:
            nav = _price(path, order["code"], order["pricing_date"], execution=True)
            if order["side"] == "sell":
                lot = state["lots"][order["lot_id"]]
                details = fees.quote_exit_details(order["shares"]*nav, contract, acquired_at=lot["acquired_at"], execution_at=order["submission"],
                    confirmed_at=_stamp(order["confirmation_date"], "00:00:00"))
                gross, fee = details["gross"], details["effective_cost"]
                state["segments"].append({"code": order["code"], "shares": order["shares"],
                    "ownership_at": lot["ownership_at"], "sold_date": order["pricing_date"]})
                lot["shares"] -= order["shares"]
                lot["reserved_shares"] -= order["shares"]
                state["receivables"].append({"kind": "sale", "amount": details["net"],
                    "pay_date": order["settlement_date"], "code": order["code"]})
                state["log"].append({"date": day, "kind": "price_sell", "code": order["code"],
                    "lot_id": order["lot_id"], "shares": str(order["shares"]), "nav": str(nav),
                    "gross": str(gross), "fee": str(fee), "fee_scope": "source_estimated_execution_cost",
                    "contractual_fee": str(details["contractual_fee"]), "rounding_loss": str(details["rounding_loss"]),
                    "receivable": str(details["net"]),
                    "cash_available_now": False, "pay_date": order["settlement_date"],
                    "submitted_at": order["submission"], "confirmation_date": order["confirmation_date"]})
            else:
                quote = fees.quote_entry(order["cash_debit"], contract, price=nav,
                    share_step=contract["trade_precision"]["share_step"])
                _allowed(quote["shares"] > 0, "Modeled request rounds to zero shares")
                fee = quote["fee"]
                holding = order["pricing_date"] if contract["acquisition_rule"]["holding_start"] == "execution_date" else order["confirmation_date"]
                ownership = order["pricing_date"] if contract["acquisition_rule"]["ownership_start"] == "execution_date" else order["confirmation_date"]
                _allowed("model:"+order["id"] not in state["lots"], "Modeled acquisition lot identity collides with source holdings")
                state["lots"]["model:"+order["id"]] = {"lot_id": "model:"+order["id"], "code": order["code"],
                    "shares": quote["shares"], "reserved_shares": ZERO,
                    "acquired_at": _stamp(holding, "00:00:00"), "ownership_at": _stamp(ownership, "00:00:00"),
                    "pricing_date": order["pricing_date"], "confirmation_date": order["confirmation_date"],
                    "initial_purchase": not order["hypothetical_future"]}
                refund = order["cash_debit"]-quote["debit"]
                _cash_event(state, day, "price_buy", reserved_delta=-order["cash_debit"], code=order["code"],
                    request=str(order["cash_debit"]), shares=str(quote["shares"]), nav=str(nav),
                    gross=str(quote["gross"]), fee=str(fee), refund=str(refund),
                    share_surplus=str(quote.get("share_surplus", ZERO)),
                    submitted_at=order["submission"], confirmation_date=order["confirmation_date"])
                if refund:
                    state["receivables"].append({"kind": "refund", "amount": refund,
                        "pay_date": order["confirmation_date"], "code": order["code"]})
            state["fees"] += fee
            order["priced"] = True
    state["pending"] = [order for order in state["pending"] if not (order["priced"] and order["confirmation_date"] <= day)]


def _distributions(state, path, day, *, existing_only=False):
    from allocation_market import _rights
    for event in path["distributions"]:
        if existing_only and event["id"] not in state["entitlements"]:
            continue
        require(event["distribution_mode"] == "cash" and event["currency"] == "CNY", "Unsupported path cash election or currency")
        require(event["record_date"] <= event["ex_date"] <= event["pay_date"], "Unsupported source record/ex/payment ordering")
        if event["record_date"] <= day and event["id"] not in state["entitlements"]:
            segments = [{**row, "sold_date": None} for row in state["lots"].values()]+state["segments"]
            entitled = sum((row["shares"] for row in segments if row["code"] == event["code"]
                and _rights(event, instant(row["ownership_at"]).astimezone(ZONE).date(),
                    _date(row["sold_date"]) if row["sold_date"] else None)), ZERO)
            state["entitlements"][event["id"]] = {"amount": (entitled*_decimal(event["per_share"])).quantize(CENT, rounding=ROUND_HALF_UP),
                "posted": False}
        right = state["entitlements"].get(event["id"])
        if right is not None and not right["posted"] and event["ex_date"] <= day:
            right["posted"] = True
            state["receivables"].append({"kind": "distribution", "amount": right["amount"],
                "pay_date": event["pay_date"], "code": event["code"]})
            state["log"].append({"date": day, "kind": "distribution_receivable", "code": event["code"],
                "event_id": event["id"], "amount": str(right["amount"]), "pay_date": event["pay_date"]})


def _credit(state, day):
    remaining = []
    for row in state["receivables"]:
        if row["pay_date"] <= day:
            _cash_event(state, day, "credit_"+row["kind"], row["amount"], code=row["code"], amount=str(row["amount"]))
        else:
            remaining.append(row)
    state["receivables"] = remaining


def _credit_existing(state, day, time):
    cutoff = instant(_stamp(day, time))
    remaining = []
    for row in state["existing_receivables"]:
        due_day, due_clock = ledger.receipt_clock(row["due_at"])
        arrived = (due_clock <= cutoff if due_clock is not None else
                   str(due_day) < day or str(due_day) == day and time == END_OF_DAY)
        if arrived:
            details = {key: row[key] for key in ("receivable_id", "source_identity", "source_event_id", "due_at", "code", "clock_precision") if key in row}
            _cash_event(state, day, "credit_existing_receivable", row["amount"], amount=str(row["amount"]),
                        receivable_kind=row["kind"], credited_at=_stamp(day, time), **details)
        else:
            remaining.append(row)
    state["existing_receivables"] = remaining


def _observed_price(context, path, code, day):
    """Only the declared lagged NAV prefix, or the already known account mark."""
    lag = max(1, context["spec"].get("availability", {}).get("nav_lag_calendar_days", 1))
    observed_day = str(_date(day)-dt.timedelta(days=lag))
    if any(date <= observed_day and code in row for date, row in path["nav"].items()):
        return _price(path, code, observed_day)
    return _decimal(context["snapshot"]["prices"][code])


def _conditional_action(context, state, path, day, rule):
    """A frozen zero-gain boundary is a candidate, not an optimal stopping rule."""
    if state["pending"]:
        return None
    sells = []
    for trade in rule["source_action"]["sells"]:
        lot = state["lots"].get(trade["lot_id"])
        if lot is None or _decimal(trade["shares"]) > lot["shares"]-lot["reserved_shares"]:
            return None
        sells.append(copy.deepcopy(trade))
    if rule["current_buy"] is not None:
        lot = state["lots"].get(rule["current_buy"]["lot_id"])
        lag = max(1, context["spec"].get("availability", {}).get("nav_lag_calendar_days", 1))
        observed_day = str(_date(day)-dt.timedelta(days=lag))
        if (lot is None or not lot.get("initial_purchase") or lot["confirmation_date"] >= day
                or lot["pricing_date"] > observed_day):
            return None
        sells.append({"lot_id": lot["lot_id"], "shares": str(lot["shares"]-lot["reserved_shares"])})
    action = {"buys": [], "sells": sells}
    source = {**context, "decision_at": _stamp(day, rule["submission_time"]),
              "snapshot": {**context["snapshot"], "positions": list(state["lots"].values()),
                           "available_cash": str(state["cash"])}}
    from allocation import validate_source_action
    try:
        validate_source_action(source, action)
        net = ZERO
        for trade in sells:
            lot = state["lots"][trade["lot_id"]]
            terms = context["fee_contracts"][lot["code"]]
            confirmed = fees.normal_confirmation_end(terms, fees.pricing_date(terms, source["decision_at"]))
            net += fees.quote_exit_details(_decimal(trade["shares"])*_observed_price(context, path, lot["code"], day),
                terms, acquired_at=lot["acquired_at"], execution_at=source["decision_at"],
                confirmed_at=_stamp(str(confirmed), "00:00:00"))["net"]
    except ContractError:
        return None
    reference = _decimal(rule["reference_value"])
    satisfies = net >= reference if rule["comparison"] == "ge" else net <= reference
    return action if satisfies else None


def _future_action(context, state, path, day, rule):
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    original_cash = state["cash"]
    action = {"buys": [], "sells": []}
    values = {}
    for lot in state["lots"].values():
        code = lot["code"]
        values[code] = values.get(code, ZERO)+lot["shares"]*_observed_price(context, path, code, day)
    equity = state["cash"]+state["reserved_cash"]+_receivable_total(state)+sum(values.values(), ZERO)
    for code, fraction in rule.get("weights", {}).items():
        asset, contract = assets[code], context["fee_contracts"][code]
        cap = min(original_cash*_decimal(fraction), state["cash"],
            max(ZERO, equity*_decimal(asset["max_weight"])-values.get(code, ZERO)))
        if asset.get("max_buy") is not None:
            cap = min(cap, _decimal(asset["max_buy"]))
        constraints = context["spec"]["constraints"]
        for group, limit in constraints["fund_group_limits"].items():
            if context["identities"][code]["fund_group_id"] == group:
                used = sum((value for member, value in values.items() if context["identities"][member]["fund_group_id"] == group), ZERO)
                cap = min(cap, max(ZERO, equity*_decimal(limit)-used))
        for sector, limit in constraints["sector_limits"].items():
            upper = context.get("sector_exposure_bounds", {}).get(code, {}).get(sector, {}).get("upper")
            unknown = any(value > 0 and context.get("sector_exposure_bounds", {}).get(member, {}).get(sector, {}).get("upper") is None for member, value in values.items())
            if upper is None or unknown:
                cap = ZERO
            elif float(upper) > 0:
                used = sum((value*_decimal(context.get("sector_exposure_bounds", {}).get(member, {}).get(sector, {}).get("upper", 0.)) for member, value in values.items() if value > 0), ZERO)
                cap = min(cap, max(ZERO, (equity*_decimal(limit)-used)/_decimal(upper)))
        request = fees.floor(cap, _decimal(contract["trade_precision"]["money_step"]))
        if request < _decimal(asset["min_buy"]):
            continue
        # Quotes are reserved together; no child policy can spend cash twice.
        state["cash"] -= request
        action["buys"].append({"code": code, "cash_debit": str(request)})
        values[code] = values.get(code, ZERO)+request
    state["cash"] = original_cash
    return action


def cash_requirement(context):
    """Bind optional receipt-date cash to the original planning contract."""
    planning = context["spec"]["planning"]
    days, amount = planning.get("cash_deadline_days"), planning.get("cash_required_amount")
    require((days is None) == (amount is None), "Cash deadline and amount must be declared together")
    value = None
    if days is not None:
        require(type(days) is int and days >= planning["primary_horizon_days"], "Cash deadline precedes the primary horizon")
        required = _decimal(amount)
        require(required > 0, "Cash requirement must be positive")
        value = {"date": (_date(context["as_of"])+dt.timedelta(days=days)).isoformat(),
                 "required_amount": ledger.decimal(required),
                 "scope": "settled_unreserved_cash_at_end_of_source_receipt_date"}
    if "cash_requirement" in context:
        require(context["cash_requirement"] == value, "Cash requirement differs from the planning contract")
    return value


def simulate(context, path, current_action, future_rule, stage_dates, *, funding=0):
    """Pure valuation; no ledger writes, orders, or source facts are produced."""
    funding = _decimal(funding)
    require(funding >= 0 and stage_dates == sorted(set(stage_dates)) and len(stage_dates) >= 2, "Bound source stage dates required")
    _allowed(len(stage_dates)<=context["spec"].get("decision",{}).get("max_path_stages",8),"Policy local decision budget exceeded")
    require(set(context["allocation_codes"]) <= set(context["snapshot"]["prices"]), "Known account marks are missing")
    if path.get("price_schema") == "source_role_price_paths_v3":
        require(set(path["known_marks"]) == set(context["allocation_codes"]), "Path known-mark universe differs")
    _allowed(not _existing_dividend_actions(context, [path]), "reconcile_existing_dividend_source_identity")
    state = _initial(context, funding)
    requirement = cash_requirement(context)
    primary_end = stage_dates[-1]
    observation_end = max(primary_end, requirement["date"]) if requirement else primary_end
    initial_cash = state["cash"]
    primary_state, deadline_state = None, None
    time = instant(context["decision_at"]).astimezone(ZONE).time().replace(tzinfo=None).isoformat()
    _submit(context, state, current_action, stage_dates[0], time, path, hypothetical=False)
    conditional = future_rule.get("conditional_sells", [])
    require(type(future_rule) is dict and set(future_rule) <= {"kind", "weights", "conditional_sells"},
            "Unsupported future policy fields")
    _allowed(len(conditional) <= 1 and all(row["comparison"] in {"ge", "le"}
        and row["decision_dates"] == sorted(set(row["decision_dates"]))
        and all(stage_dates[0] < date < stage_dates[-1] and date in stage_dates for date in row["decision_dates"])
        for row in conditional), "Conditional sale is outside frozen local decisions")
    conditional_done = set()
    conditional_decisions = []
    timeline = set(path["nav"]) | set(stage_dates)
    if requirement:
        timeline.add(requirement["date"])
    for event in path["distributions"]:
        timeline.update(event[key] for key in ("record_date", "ex_date", "pay_date"))
    timeline.update(row["pay_date"] for row in state["existing_receivables"])
    day = stage_dates[0]
    while day <= observation_end:
        for index, rule in enumerate(conditional):
            if index not in conditional_done and day in rule["decision_dates"]:
                action = _conditional_action(context, state, path, day, rule)
                conditional_decisions.append({"date": day, "comparison": rule["comparison"],
                    "reference_value": rule["reference_value"], "action": action or {"buys": [], "sells": []}})
                if action is not None:
                    _submit(context, state, action, day, rule["submission_time"], path, hypothetical=True)
                    conditional_done.add(index)
        prior_pending = bool(state["pending"])
        _process_orders(context, state, path, day)
        # Current orders are already submitted using actual arrived cash only.
        # Precise source clocks may fund a later modeled submission.
        if day > stage_dates[0]:
            _credit_existing(state, day, time)
        if (stage_dates[0] < day < stage_dates[-1] and day in stage_dates and not state["future_used"]
                and len(conditional_done)==len(conditional)
                and not prior_pending
                and not state["pending"] and not any(row["kind"] == "sale" for row in state["receivables"])):
            action = _future_action(context, state, path, day, future_rule)
            if action["buys"]:
                try:
                    _submit(context, state, action, day, time, path, hypothetical=True)
                except PolicyInfeasible:
                    # The rule is explicitly wait/hold when source minimum or
                    # ownership feasibility cannot be met at the observed state.
                    pass
                else:
                    state["future_used"] = True
                    # Same-day pricing must precede end-of-day record rights.
                    _process_orders(context, state, path, day)
        _distributions(state, path, day, existing_only=day > primary_end)
        _credit(state, day)
        _credit_existing(state, day, END_OF_DAY)
        if (stage_dates[0] < day < stage_dates[-1] and day in stage_dates and not state["future_used"]
                and len(conditional_done)==len(conditional)
                and not state["pending"] and not any(row["kind"] == "sale" for row in state["receivables"])):
            action = _future_action(context, state, path, day, future_rule)
            if action["buys"]:
                try:
                    # Date-only confirmations/receipts do not prove 08:00 cash.
                    # Wait through that date; the source cutoff moves pricing
                    # to the next open date. This is still only a valuation.
                    _submit(context, state, action, day, END_OF_DAY, path, hypothetical=True)
                except PolicyInfeasible:
                    pass
                else:
                    state["future_used"] = True
        for order in state["pending"]:
            timeline.update(order[key] for key in ("pricing_date", "confirmation_date"))
            if order.get("settlement_date"):
                timeline.add(order["settlement_date"])
        timeline.update(row["pay_date"] for row in state["receivables"])
        if day == primary_end:
            primary_state = copy.deepcopy(state)
        if requirement and day == requirement["date"]:
            deadline_state = copy.deepcopy(state)
        later = sorted(date for date in timeline if day < date <= observation_end)
        if not later:
            break
        day = later[0]
    require(primary_state is not None, "Primary wealth observation was not reached")
    state = primary_state
    terminal = _equity(state, path, primary_end)
    terminal_cost, terminal_gross_adjustment, terminal_drag = ZERO, ZERO, ZERO
    terminal_assumptions = []
    if context["spec"]["planning"]["primary_goal"] == "redeem":
        from allocation import validate_redemption_fee_scope
        for code in context["allocation_codes"]:
            try:
                validate_redemption_fee_scope(context["fee_contracts"][code],sum(row["code"] == code and row["shares"] > 0 for row in state["lots"].values()))
            except ContractError as error:
                raise PolicyInfeasible(str(error)) from error
        for lot in state["lots"].values():
            nav = _price(path, lot["code"], primary_end)
            gross = lot["shares"]*nav
            contract = context["fee_contracts"][lot["code"]]
            submitted = _stamp(primary_end, time)
            details = fees.quote_exit_details(gross, contract,
                acquired_at=lot["acquired_at"], execution_at=submitted)
            cost = details["contractual_fee"]+details["rounding_loss"]
            adjustment, drag = gross-details["gross"], gross-details["net"]
            terminal_cost += cost
            terminal_gross_adjustment += adjustment
            terminal_drag += drag
            # Terminal exit is a valuation assumption. Its net already adjusts
            # wealth here; reporting its cost must not subtract it a second time.
            terminal += details["net"]-gross
            terminal_assumptions.append({"lot_id": lot["lot_id"], "code": lot["code"],
                "shares": str(lot["shares"]), "nav": str(nav), "acquired_at": lot["acquired_at"],
                "submitted_at": submitted, "fee_contract_hash": fingerprint(contract),
                "raw_gross": str(gross), "quoted_gross": str(details["gross"]),
                "contractual_fee": str(details["contractual_fee"]), "rounding_loss": str(details["rounding_loss"]),
                "net": str(details["net"]), "cost": str(cost),
                "gross_rounding_adjustment": str(adjustment), "wealth_drag": str(drag),
                "is_order": False, "is_ledger_event": False,
                "scope": "terminal_exit_valuation_assumption_not_order_or_ledger"})
    result = {"terminal_wealth": float(terminal), "fees": float(state["fees"]+terminal_cost),
        "path_execution_cost": float(state["fees"]), "terminal_exit_assumption_cost": float(terminal_cost),
        "terminal_gross_rounding_adjustment": float(terminal_gross_adjustment),
        "terminal_wealth_drag": float(terminal_drag), "terminal_exit_assumptions": terminal_assumptions,
        "policy_turnover": float(sum((_decimal(row["request"]) for row in state["log"] if row["kind"] == "reserve_buy"), ZERO)
            +sum((_decimal(row["gross"]) for row in state["log"] if row["kind"] == "price_sell"), ZERO)),
        "settled_cash": str(state["cash"]), "reserved_cash": str(state["reserved_cash"]),
        "receivables": str(_receivable_total(state)),
        "terminal_existing_receivables": [{**row, "amount": str(row["amount"])} for row in state["existing_receivables"]],
        "initial_available_cash": ledger.decimal(initial_cash), "primary_observed_through": primary_end,
        "terminal_lots": [{**row, "shares": str(row["shares"]), "reserved_shares": str(row["reserved_shares"])} for row in state["lots"].values()],
        "cash_flow_log": state["log"], "future_used": state["future_used"],
        "initial_known_wealth":context["snapshot"]["equity"],"local_decision_dates":stage_dates,
        "accounting_event_dates":sorted(timeline),"conditional_submission_count":len(conditional_done),
        "conditional_decisions":conditional_decisions,
        "current_action": copy.deepcopy(current_action), "future_rule": copy.deepcopy(future_rule),
        "future_rule_is_order": False, "scope": "normal_source_clocks_model_path_valuation_not_actual_fills"}
    if requirement:
        require(deadline_state is not None, "Cash deadline observation was not reached")
        result["cash_deadline"] = {**requirement,
            "initial_available_cash": ledger.decimal(initial_cash),
            "available_cash": ledger.decimal(deadline_state["cash"]),
            "reserved_cash": ledger.decimal(deadline_state["reserved_cash"]),
            "receivables": ledger.decimal(_receivable_total(deadline_state)),
            "cash_flow_log": deadline_state["log"], "observed_through": requirement["date"],
            "passes": deadline_state["cash"] >= _decimal(requirement["required_amount"])}
    result["valuation_hash"] = fingerprint(result)
    return result


def _weighted_tail(losses, probabilities, tail):
    """Producer algorithm on an already validated exact probability measure."""
    losses, tail = [risk_numbers.number(value) for value in losses], risk_numbers.number(tail)
    require(0 < tail <= 1 and len(losses) == len(probabilities) and losses, "Finite weighted tail required")
    remaining, total = tail, 0
    for loss, probability in sorted(zip(losses, probabilities), reverse=True):
        mass = min(remaining, probability)
        total += loss*mass
        remaining -= mass
        if remaining == 0:
            break
    return total/tail


def tail_mean(losses, probabilities, tail, *, exact=False):
    value = _weighted_tail(losses, risk_numbers.measure(probabilities), tail)
    return value if exact else float(value)


def nested_tail(losses, paths, prefix_groups, stages, tail, *, exact=False):
    """Conditional CVaR recursion on the declared, frozen information partition."""
    ids = [path["id"] for path in paths]
    require(len(ids) == len(set(ids)) and len(ids) == len(losses), "Unique original risk support required")
    original_probabilities = risk_numbers.measure([path["probability"] for path in paths])
    masses = dict(zip(ids, original_probabilities))
    _weighted_tail(losses, original_probabilities, tail)
    values = dict(zip(ids, map(risk_numbers.number, losses)))
    audit = []
    for stage in reversed(stages[1:-1]):
        groups = prefix_groups[stage]
        require(sorted(member for group in groups for member in group) == sorted(ids), "Risk information partition changed probability support")
        for group in groups:
            mass = sum(masses[member] for member in group)
            require(mass > 0 and len(group) >= 2, "Degenerate future identifying group")
            probabilities = ([masses[member] for member in group] if len(groups) == 1
                else [masses[member]/mass for member in group])
            risk = _weighted_tail([values[member] for member in group], probabilities, tail)
            for member in group:
                values[member] = risk
            audit.append({"date": stage, "members": list(group), "mass": float(mass), "conditional_cvar": float(risk)})
    risk = _weighted_tail([values[member] for member in ids], original_probabilities, tail)
    return risk if exact else float(risk), {"kind": "nested_conditional_CVaR_on_declared_information_partition", "nodes": audit,
        "full_information_time_consistency_claimed": False}


def reference_valuation(context, action, *, funding=0):
    """Current known NAV reference under explicit normal source clocks; no fills."""
    from allocation import validate_source_action
    validate_source_action(context,action,funding=funding)
    lots = {row["lot_id"]:row for row in context["snapshot"]["positions"]}
    legs, products = [],{}
    for side,trades in (("sell",action["sells"]),("buy",action["buys"])):
        for trade in trades:
            lot = lots[trade["lot_id"]] if side == "sell" else None
            code = lot["code"] if lot else trade["code"]
            terms = context["fee_contracts"][code]
            price = _decimal(context["snapshot"]["prices"][code])
            priced = fees.pricing_date(terms,context["decision_at"])
            confirmed = fees.normal_confirmation_end(terms,priced)
            leg = {"side":side,"code":code,"lot_id":lot["lot_id"] if lot else None,
                "reference_nav":str(price),"reference_nav_date":context["market_ref"]["price_dates"][code],
                "submitted_at":context["decision_at"],"pricing_date":str(priced),"normal_confirmation_date":str(confirmed),
                "confirmation_basis":"source_normal_estimate_not_actual","holding_end_event":terms["holding"]["end_event"]}
            if lot:
                quantity = _decimal(trade["shares"])
                quote = fees.quote_exit_details(quantity*price,terms,acquired_at=lot["acquired_at"],
                    execution_at=context["decision_at"],confirmed_at=_stamp(str(confirmed),"00:00:00"))
                rule = terms["settlement"]
                leg.update(shares=str(quantity),acquired_at=lot["acquired_at"],
                    normal_cash_date=str(trading_calendar.advance(confirmed,rule["lag_days"],rule["day_basis"],rule.get("calendar",terms["execution_calendar"]))),
                    **{key:str(quote[key]) for key in ("gross","contractual_fee","rounding_loss","net","effective_cost")})
                product = products.setdefault(code,{"code":code,"redemption_shares":ZERO,"lot_allocations":[],
                    "gross":ZERO,"contractual_fee":ZERO,"rounding_loss":ZERO,"net":ZERO,
                    "fee_application":terms.get("redemption_fee_application","single_lot"),
                    "rounding_application":terms.get("redemption_rounding_application","single_lot")})
                product["redemption_shares"] += quantity
                product["lot_allocations"].append({"lot_id":lot["lot_id"],"shares":str(quantity)})
                for key in ("gross","contractual_fee","rounding_loss","net"):product[key] += quote[key]
            else:
                quote = fees.quote_entry(trade["cash_debit"],terms,price=price,share_step=terms["trade_precision"]["share_step"])
                leg.update(cash_debit=str(trade["cash_debit"]),shares=str(quote["shares"]),gross=str(quote["gross"]),
                    contractual_fee=str(quote["fee"]),rounding_loss="0",net=str(quote["gross"]),
                    quoted_debit=str(quote["debit"]),normal_refund=str(_decimal(trade["cash_debit"])-quote["debit"]),
                    share_surplus=str(quote.get("share_surplus",ZERO)))
            legs.append(leg)
    totals = [{**row,**{key:str(row[key]) for key in ("redemption_shares","gross","contractual_fee","rounding_loss","net")}} for row in products.values()]
    return {"schema_id":"source_reference_cash_valuation_v1","basis":"current_known_NAV_reference_not_future_dealing_NAV",
        "submitted_at":context["decision_at"],"legs":legs,"total_products":totals,"actual_execution_asserted":False}


def _cash_rules(context, fractions):
    codes = sorted(context["purchase_eligible_codes"])
    rules = [{"kind":"hold","weights":{}}]
    rules += [{"kind":"allocate_settled_cash_once_then_redecide_in_reality","weights":{code:str(fraction)}} for code in codes for fraction in fractions]
    if len(codes)>1:
        rules += [{"kind":"allocate_settled_cash_once_then_redecide_in_reality","weights":{code:str(fraction/Decimal(len(codes))) for code in codes}} for fraction in fractions]
    return rules


def _conditional_sale_nodes(context, initial_action, source_action, current_buy, horizon):
    """First lawful source clock, fee unlocks, and an optional receipt window."""
    lots = {row["lot_id"]: row for row in context["snapshot"]["positions"]}
    coordinates = [(lots[row["lot_id"]]["code"], lots[row["lot_id"]]["acquired_at"],
                    _decimal(row["shares"])*_decimal(context["snapshot"]["prices"][lots[row["lot_id"]]["code"]]))
                   for row in source_action["sells"]]
    joint_dates = set.intersection(*(set(context["fee_contracts"][code]["execution_calendar"]["open_dates"])
                                    for code in context["allocation_codes"]))
    initial_ready = _date(context["as_of"])
    for code in [lots[row["lot_id"]]["code"] for row in initial_action["sells"]]+[row["code"] for row in initial_action["buys"]]:
        terms = context["fee_contracts"][code]
        confirmed = fees.normal_confirmation_end(terms, fees.pricing_date(terms, context["decision_at"]))
        initial_ready = max(initial_ready, confirmed)
    buy_known = None
    if current_buy is not None:
        code = current_buy["code"]
        terms = context["fee_contracts"][code]
        priced = fees.pricing_date(terms, context["decision_at"])
        buy_confirmed = fees.normal_confirmation_end(terms, priced)
        lag = max(1, context["spec"].get("availability", {}).get("nav_lag_calendar_days", 1))
        buy_known = priced+dt.timedelta(days=lag)
        acquired = priced if terms["acquisition_rule"]["holding_start"] == "execution_date" else buy_confirmed
        coordinates.append((code, _stamp(str(acquired), "00:00:00"), _decimal(current_buy["cash_debit"])))
    if not coordinates:
        return []
    time = instant(context["decision_at"]).astimezone(ZONE).time().replace(tzinfo=None).isoformat()
    previous, nodes, latest = None, [], None
    deadline = cash_requirement(context)
    day = _date(context["as_of"])+dt.timedelta(days=1)
    while str(day) < horizon:
        if day <= initial_ready or buy_known is not None and day < buy_known:
            day += dt.timedelta(days=1)
            continue
        costs, arrivals = [], []
        try:
            for code, acquired, gross in coordinates:
                terms = context["fee_contracts"][code]
                require(str(day) in terms["execution_calendar"]["open_dates"], "Conditional submission date is closed")
                require(trading_calendar.holding_days(acquired, _stamp(str(day), time), terms["holding"])
                        >= terms["holding"]["minimum_days"], "Conditional lot remains locked")
                priced = fees.pricing_date(terms, _stamp(str(day), time))
                require(str(priced) < horizon and str(priced) in joint_dates, "Conditional sale pricing is outside the joint source grid")
                confirmed = fees.normal_confirmation_end(terms, priced)
                costs.append(fees.quote_exit_details(gross, terms, acquired_at=acquired,
                    execution_at=_stamp(str(day), time), confirmed_at=_stamp(str(confirmed), "00:00:00"))["effective_cost"])
                settlement = terms["settlement"]
                arrivals.append(str(trading_calendar.advance(confirmed, settlement["lag_days"], settlement["day_basis"],
                    settlement.get("calendar", terms["execution_calendar"]))))
        except (ContractError, ValueError):
            day += dt.timedelta(days=1)
            continue
        cost = sum(costs, ZERO)
        if previous is None or cost < previous:
            nodes.append(str(day))
        previous = cost
        if deadline and all(date <= deadline["date"] for date in arrivals):
            latest = str(day)
        day += dt.timedelta(days=1)
    return sorted(set(nodes+([latest] if latest else [])))


def _conditional_rule(context, initial_action, source_action, current_buy, comparison, horizon):
    nodes = _conditional_sale_nodes(context, initial_action, source_action, current_buy, horizon)
    if not nodes:
        return None
    lots = {row["lot_id"]: row for row in context["snapshot"]["positions"]}
    reference = sum((_decimal(row["shares"])*_decimal(context["snapshot"]["prices"][lots[row["lot_id"]]["code"]])
                     for row in source_action["sells"]), ZERO)
    if current_buy is not None:
        reference += _decimal(current_buy["cash_debit"])
    return {"initial_action": copy.deepcopy(initial_action), "source_action": copy.deepcopy(source_action), "current_buy": copy.deepcopy(current_buy),
        "comparison": comparison, "reference_value": str(reference), "decision_dates": nodes,
        "submission_time": instant(context["decision_at"]).astimezone(ZONE).time().replace(tzinfo=None).isoformat(),
        "trigger_basis": "lagged_observable_net_proceeds_versus_frozen_known_mark_and_initial_debit_zero_gain",
        "node_basis": "first_lawful_joint_source_pricing_fee_unlock_or_latest_receipt_window",
        "scope": "finite_candidate_not_optimal_stopping_or_future_conditional_EN"}


def freeze_policies(context, comparison, *, max_current_actions, cash_fractions):
    """Freeze every logical group; incomplete groups never return an incumbent."""
    from allocation import validate_source_action
    import single_step_wealth
    require(type(max_current_actions) is int and max_current_actions >= 1, "Explicit action computation budget required")
    fractions = [_decimal(value) for value in cash_fractions]
    require(fractions and fractions == sorted(set(fractions)) and all(0 < value <= 1 for value in fractions), "Declared future cash fractions required")
    require(comparison["status"] in {"frozen","partial"},"Declared source funding groups required")
    require(comparison["context_hash"] == context["context_hash"]
            and comparison["action_family_hash"] == fingerprint(comparison["funding_options"]), "Source action family binding differs")
    require(comparison["funding_registry_hash"] == fingerprint(comparison["funding_registry"]),"Funding registry changed")
    future = _cash_rules(context,fractions)
    horizon = single_step_wealth.build_clock_context(context)["evaluation_date"]
    groups, budgets, registry = [], [], []
    for group in comparison["funding_options"]:
        actions = group["actions"]
        menu_upper = sum(menu["choice_count_upper"]+len(audit["rejected_source_choices"])
                         for menu, audit in zip(group["descriptor"]["menus"], group["menus"]))
        upper = (group["action_count_upper"]*(1+2*len(context["allocation_codes"]))+2*menu_upper)*len(future)
        entry = {"proposed_contribution":group["proposed_contribution"],"source_descriptor_hash":group["descriptor_hash"],
            "policy_count_upper":max(1,upper),"cash_rules_hash":fingerprint(future),
            "conditional_rule":"source_menu_and_initial_acquisition_ge_le_zero_gain_prefix_feedback_v1"}
        entry["group_id"] = "funding-"+fingerprint(entry)[:32]
        registry.append(entry)
        status = group["build_status"]
        group_gaps = list(group["required_actions"])
        if status == "complete" and len(actions)>max_current_actions:
            status = "budget_partial"
            group_gaps.append({"action":"increase_declared_current_action_budget_or_explicitly_narrow_scope",
                "required":len(actions),"budget":max_current_actions,"funding":group["proposed_contribution"]})
        policies = {}
        local_budget_gaps = []
        def append(action,rule):
            value = {"schema_id":"source_local_policy_v2","current_action":copy.deepcopy(action),"future_rule":copy.deepcopy(rule),
                "proposed_contribution":group["proposed_contribution"],"funding_group_id":entry["group_id"]}
            dates = {context["as_of"],horizon}
            references = reference_valuation(context,action,funding=group["proposed_contribution"])
            if rule["weights"]:
                # A possible cash-allocation submission is a decision. Receipt,
                # confirmation and pricing events alone remain accounting.
                dates.add(str(_date(context["as_of"])+dt.timedelta(days=1)))
                dates.update(row.get("normal_cash_date",row["normal_confirmation_date"]) for row in references["legs"])
                dates.update(str(ledger.receipt_clock(row["due_at"])[0]) for row in context["snapshot"].get("receivables",[]) if row.get("due_at"))
            for condition in rule.get("conditional_sells", []):
                dates.update(condition["decision_dates"])
                if rule["weights"]:
                    code = (condition["current_buy"]["code"] if condition["current_buy"] is not None else
                            next(row["code"] for row in context["snapshot"]["positions"]
                                 if row["lot_id"] == condition["source_action"]["sells"][0]["lot_id"]))
                    terms = context["fee_contracts"][code]
                    settlement = terms["settlement"]
                    for day in condition["decision_dates"]:
                        confirmed = fees.normal_confirmation_end(terms, fees.pricing_date(terms, _stamp(day, condition["submission_time"])))
                        dates.add(str(trading_calendar.advance(confirmed, settlement["lag_days"], settlement["day_basis"],
                            settlement.get("calendar", terms["execution_calendar"]))))
            value["local_decision_dates"] = sorted(day for day in dates if context["as_of"]<=day<=horizon)
            value["local_decision_scope_complete"] = len(value["local_decision_dates"])<=context["spec"]["decision"]["max_path_stages"]
            value["mode"] = "baseline" if not action["buys"] and not action["sells"] and not rule["weights"] and not rule.get("conditional_sells") else "rolling_policy"
            value["id"] = "mpc-"+fingerprint(value)[:32]
            policies[value["id"]] = value
            if not value["local_decision_scope_complete"]:
                local_budget_gaps.append({"policy_id":value["id"],"local_decision_dates":value["local_decision_dates"]})
        if status == "complete":
            require(group["current_action_scope_complete"] and len(actions)<=max_current_actions,"Complete source action group required")
            for action in actions:
                validate_source_action(context,action,funding=group["proposed_contribution"])
                for rule in future:append(action,rule)
            targets = {}
            for action in actions:
                sold = {row["lot_id"]: _decimal(row["shares"]) for row in action["sells"]}
                for code in context["allocation_codes"]:
                    remaining = [{"lot_id": lot["lot_id"], "shares": str(_decimal(lot["shares"])-_decimal(lot["reserved_shares"])-sold.get(lot["lot_id"], ZERO))}
                        for lot in context["snapshot"]["positions"] if lot["code"] == code
                        and _decimal(lot["shares"])-_decimal(lot["reserved_shares"])-sold.get(lot["lot_id"], ZERO) > 0]
                    bought = next((dict(row, lot_id="model:"+str(len(action["sells"])+index+1))
                        for index, row in enumerate(action["buys"]) if row["code"] == code), None)
                    if remaining or bought is not None:
                        target = [action, {"buys": [], "sells": remaining}, bought]
                        targets[fingerprint(target)] = target
            singles = {fingerprint(action): action for action in actions if action["sells"] and not action["buys"]}
            for audit in group["menus"]:
                for rejected in audit["rejected_source_choices"]:
                    action = rejected["action"]
                    if action["sells"] and not action["buys"]:
                        singles[fingerprint(action)] = action
            for sale in singles.values():
                codes = {next(lot["code"] for lot in context["snapshot"]["positions"] if lot["lot_id"] == row["lot_id"])
                         for row in sale["sells"]}
                if len(codes) == 1:
                    target = [{"buys": [], "sells": []}, sale, None]
                    targets[fingerprint(target)] = target
            for action, sale, bought in targets.values():
                for comparator in ("ge", "le"):
                    condition = _conditional_rule(context, action, sale, bought, comparator, horizon)
                    if condition is not None:
                        for rule in future:
                            append(action, {**rule, "kind": "conditional_source_sale_then_arrived_cash_allocation",
                                            "conditional_sells": [condition]})
            require(len(policies)<=upper,"Declared policy upper bound understates generated family")
            if local_budget_gaps:
                status = "budget_partial"
                group_gaps.append({"action":"increase_declared_local_decision_budget_or_explicitly_narrow_scope",
                    "funding":group["proposed_contribution"],"required":max(len(row["local_decision_dates"]) for row in local_budget_gaps),
                    "budget":context["spec"]["decision"]["max_path_stages"],"uncomputed_policies":local_budget_gaps})
                policies = {}
            if len(policies)>context["spec"]["decision"]["max_policy_count"]:
                status = "budget_partial"
                budgets.append({"action":"increase_declared_policy_budget_or_explicitly_narrow_scope",
                    "funding":group["proposed_contribution"],"required":len(policies),
                    "budget":context["spec"]["decision"]["max_policy_count"]})
                policies = {}
        groups.append({"proposed_contribution":group["proposed_contribution"],"funding_group_id":entry["group_id"],"build_status":status,
            "policies":list(policies.values()),"policy_count_upper":entry["policy_count_upper"],
            "policy_hash":fingerprint(list(policies.values())),"current_action_scope_complete":status=="complete",
            "current_quote_combination_count":group["combination_count"],"required_actions":group_gaps+
                [row for row in budgets if row.get("action") and row["funding"]==group["proposed_contribution"]]})
        budgets.append({"funding": group["proposed_contribution"], "current_actions": len(actions), "future_rules": len(future)})
    policies = [policy for group in groups for policy in group["policies"]]
    total = sum(row["policy_count_upper"] for row in registry)
    for group in groups:group["alpha_weight"] = group["policy_count_upper"]/total
    zero = next(group for group in groups if group["proposed_contribution"]==0)
    return {"schema_id":"source_frozen_funding_policies_v2","status":"frozen" if zero["build_status"]=="complete" else "partial",
            "reason":None if zero["build_status"]=="complete" else "zero_funding_policy_scope_incomplete",
            "required_actions":[gap for group in groups for gap in group["required_actions"]],
            "funding_registry":registry,"funding_registry_hash":fingerprint(registry),"planned_policy_count_upper":total,
            "groups": groups, "policies": policies, "family_hash": fingerprint(policies),
            "source_action_family_hash": comparison["action_family_hash"], "scope": "finite_source_quote_current_actions_and_declared_lagged_prefix_feedback_policies",
            "computation": budgets, "future_trades_published_as_orders": False, "global_investment_optimality_claimed": False}


def verify_cash_flow(value):
    """Independent accounting identities over the published valuation journal."""
    for row in value["cash_flow_log"]:
        if "available_delta" in row:
            require(_decimal(row["available_after"]) == _decimal(row["available_before"])+_decimal(row["available_delta"])
                    and _decimal(row["available_after"]) >= 0 and _decimal(row["reserved_after"]) >= 0,
                    "MPC available/reserved cash conservation failed")
        if row["kind"] == "price_buy":
            require(_decimal(row["request"]) == _decimal(row["gross"])+_decimal(row["fee"])+_decimal(row["refund"]),
                    "MPC subscription gross/fee/refund conservation failed")
        if row["kind"] == "price_sell":
            require(_decimal(row["receivable"]) == _decimal(row["gross"])-_decimal(row["fee"])
                    and row["cash_available_now"] is False, "MPC redemption proceeds conservation failed")
    require(value["future_rule_is_order"] is False and value["terminal_wealth"] >= 0,
            "Modeled future policy escaped current publication scope")
    return {"status": "passed", "scope": "cash_share_flow_and_future_publication_boundaries_not_market_profitability"}


def as_current_order_distribution(context, paths):
    """Derive current quote primitives; final selection always uses whole paths."""
    from allocation_market import _rights
    import single_step_wealth
    require(paths["status"] == "research_ready", "Qualified joint paths required")
    clock = single_step_wealth.build_clock_context(context)
    codes, end = context["allocation_codes"], clock["evaluation_date"]
    def targets(path):
        out = {name: [] for name in ("pricing", "terminal_nav", "hold", "buy", "sell")}
        for code in codes:
            phase = clock["assets"][code]
            base = _decimal(context["snapshot"]["prices"][code])
            priced = _price(path, code, phase["pricing_date"], execution=True)
            terminal = _price(path, code, end, execution=True)
            events = [event for event in path["distributions"] if event["code"] == code]
            old = _date(context["as_of"])-dt.timedelta(days=1)
            new, sold = _date(phase["ownership_date_bounds"]["latest"]), _date(phase["pricing_date"])
            cash = lambda owned, redeemed=None: sum((_decimal(event["per_share"]) for event in events if _rights(event, owned, redeemed)), ZERO)
            values = {"pricing": priced/base, "terminal_nav": terminal/base,
                "hold": (terminal+cash(old))/base, "buy": (terminal+cash(new))/priced,
                "sell": (priced+cash(old, sold))/base}
            for name in out:
                out[name].append(float(values[name]))
        return out
    selected = [targets(path) for path in paths["selection_paths"]]
    point = targets(paths["point_paths"][0])
    return {"codes": codes, "dates": [row["origin_at"][:10] for row in paths["selection_paths"]],
        "probabilities": [row["probability"] for row in paths["selection_paths"]],
        "targets": {name: [row[name] for row in selected] for name in point},
        "current_point_targets": {name: [values] for name, values in point.items()},
        "returns": [[value-1 for value in row["hold"]] for row in selected], "clock_context": clock,
        "method": "source_joint_multi_date_paths_current_quote_adapter", "path_hash": paths["path_hash"],
        "empirical_prediction_error_included": True, "serial_order_preserved": True,
        "cross_sectional_pairing_preserved": True, "interpretation": "conditional_empirical_not_probability_guarantee"}


def _policy_values(context, paths, policy, source):
    _allowed(policy["local_decision_scope_complete"],"Policy local decision scope incomplete")
    require(paths["schema"] == "source_role_price_paths_v3", "Source-role path schema required")
    values = [simulate(context, {**path,"known_marks":paths.get("known_marks",{}),"price_schema":paths.get("schema")},
        policy["current_action"], policy["future_rule"], policy["local_decision_dates"],
        funding=policy["proposed_contribution"]) for path in source]
    for value in values:
        verify_cash_flow(value)
    return values


def _calibration_matrix(context, paths, policies, baselines):
    window = context["spec"]["trade_policy"]["sample_window"]
    pairs = [row for row in paths["calibration_paths"] if window["start_date"] <= row["origin_at"][:10] <= window["end_date"]
             and row["label_available_at"][:10] < context["as_of"]]
    dates = [row["origin_at"][:10] for row in pairs]
    require(dates == sorted(set(dates)), "Calibration origins lost temporal pairing")
    errors, losses, records, invalid = {}, {}, {}, {}
    for policy in policies:
        baseline = baselines[str(policy["proposed_contribution"])]
        capital = risk_numbers.number(context["snapshot"]["equity"])+risk_numbers.number(policy["proposed_contribution"])
        identity = policy["id"]
        errors[identity], losses[identity], records[identity] = [], [], []
        try:
            for pair in pairs:
                predicted, realized = (_policy_values(context, paths, policy, [pair[k]])[0] for k in ("predicted", "realized"))
                predicted_hold, realized_hold = (_policy_values(context, paths, baseline, [pair[k]])[0] for k in ("predicted", "realized"))
                estimate = risk_numbers.number(predicted["terminal_wealth"])-risk_numbers.number(predicted_hold["terminal_wealth"])
                truth = risk_numbers.number(realized["terminal_wealth"])-risk_numbers.number(realized_hold["terminal_wealth"])
                errors[identity].append(float(estimate-truth))
                losses[identity].append(float(capital-risk_numbers.number(realized["terminal_wealth"])))
                records[identity].append({"origin_at": pair["origin_at"], "label_available_at": pair["label_available_at"],
                    "source_hashes": pair["source_hashes"], "predicted_gain": float(estimate), "realized_gain": float(truth),
                    "optimism": float(estimate-truth), "principal_loss": float(capital-risk_numbers.number(realized["terminal_wealth"])),
                    "predicted_valuation_hash": predicted["valuation_hash"], "realized_valuation_hash": realized["valuation_hash"]})
        except PolicyInfeasible as error:
            invalid[identity] = str(error)
            errors.pop(identity); losses.pop(identity); records.pop(identity)
    return errors, losses, dates, records, invalid


def optimise(context, paths, comparison, *, family_count=1):
    from newtrade_guard import qualification_scope
    governance = qualification_scope(context["spec"]["trade_policy"], context["decision_at"],
                                     context.get("trade_family_review_index"))
    """Choose all eligible policies in a frozen finite source-quoted family."""
    out = {"status": "partial", "context_hash": context["context_hash"], "path_hash": paths.get("path_hash"),
        "funding_options": [], "selected_policy": None, "current_action": {"buys": [], "sells": []},
        "required_actions": [], "live_execution_authorized": False,
        "scope": "finite_source_quoted_lagged_prefix_feedback_policies_not_global_investment_optimum"}
    if paths["status"] != "research_ready":
        return {**out, "reason": paths.get("reason"), "required_actions": paths["required_actions"]}
    source_paths = list(paths["point_paths"])+list(paths["selection_paths"])
    source_paths += [pair[kind] for pair in paths["calibration_paths"] for kind in ("predicted", "realized")]
    receipt_actions = _existing_dividend_actions(context, source_paths)
    if receipt_actions:
        return {**out, "reason": "existing_dividend_source_identity_unresolved", "required_actions": receipt_actions}
    decision = context["spec"]["decision"]
    frozen = freeze_policies(context, comparison, max_current_actions=decision["max_current_actions"], cash_fractions=decision["future_cash_fractions"])
    if frozen["status"] != "frozen":
        return {**out, "reason": frozen["reason"], "required_actions": frozen["required_actions"],"frozen_policy_family":frozen}
    distribution = as_current_order_distribution(context, paths)
    baselines = {str(row["proposed_contribution"]): row for row in frozen["policies"] if row["mode"] == "baseline"}
    probabilities = risk_numbers.measure([row["probability"] for row in paths["selection_paths"]])
    numeric = risk_numbers.number
    from allocation import funded_context
    import single_step_wealth
    from portfolio_paths import information_groups
    evaluated, invalid, exact_values = {}, {}, {}
    for policy in frozen["policies"]:
        try:
            point = _policy_values(context, paths, policy, paths["point_paths"])[0]
            selection = _policy_values(context, paths, policy, paths["selection_paths"])
            capital = numeric(context["snapshot"]["equity"])+numeric(policy["proposed_contribution"])
            risk, proof = nested_tail([capital-numeric(row["terminal_wealth"]) for row in selection], paths["selection_paths"],
                information_groups(paths["selection_paths"],policy["local_decision_dates"]),
                policy["local_decision_dates"], context["spec"]["allocation"]["tail_probability"], exact=True)
            profit = sum(p*(numeric(row["terminal_wealth"])-capital) for p,row in zip(probabilities,selection))
            policy_fees = sum(p*numeric(row["fees"]) for p,row in zip(probabilities,selection))
            turnover = sum(p*sum(numeric(entry["request"] if entry["kind"]=="reserve_buy" else entry["gross"])
                for entry in row["cash_flow_log"] if entry["kind"] in {"reserve_buy","price_sell"}) for p,row in zip(probabilities,selection))
            exact_values[policy["id"]] = {"risk":risk,"profit":profit,"fees":policy_fees,"turnover":turnover}
            pricing_nav = {code:_price(paths["point_paths"][0],code,distribution["clock_context"]["assets"][code]["pricing_date"],execution=True)
                for code in context["allocation_codes"]}
            projection = single_step_wealth.project(funded_context(context, policy["proposed_contribution"]), policy["current_action"], distribution,
                pricing_nav=pricing_nav)
            evaluated[policy["id"]] = {**policy, "point": point, "selection": selection, "current_projection": projection,
                "reference_valuation":reference_valuation(context,policy["current_action"],funding=policy["proposed_contribution"]),
                "point_profit": float(numeric(point["terminal_wealth"])-capital),
                "expected_profit": float(profit), "expected_policy_fees": float(policy_fees),
                "expected_policy_turnover": float(turnover),
                "absolute_cvar": float(risk), "risk_recursion": proof, "eligible": False, "qualification": "conditional_research"}
        except PolicyInfeasible as error:
            invalid[policy["id"]] = str(error)
    # Same-funded advantage and risk restoration require an evidenced baseline.
    # A legal alternative alone cannot reconstruct that missing comparison.
    # Isolate this source gap to its group; do not fabricate a baseline or
    # redistribute its already reserved statistical error allowance.
    for group in frozen["groups"]:
        if group["build_status"] != "complete":
            continue
        baseline = baselines[str(group["proposed_contribution"])]
        if baseline["id"] not in evaluated:
            reason = "source_comparison_baseline_unavailable:"+invalid.get(baseline["id"], "source_valuation_missing")
            for policy in group["policies"]:
                if policy["id"] in evaluated:
                    evaluated.pop(policy["id"])
                    invalid[policy["id"]] = reason
    from mpc_calibration import calibrate
    records, cal_invalid = {}, {}
    calibration = {"schema_id":"source_funding_calibration_registry_v2","status":"needs_calibration",
        "funding_registry":frozen["funding_registry"],"funding_registry_hash":frozen["funding_registry_hash"],
        "planned_policy_count_upper":frozen["planned_policy_count_upper"],"groups":[],"candidates":{}}
    for group in frozen["groups"]:
        if group["build_status"] != "complete":
            calibration["groups"].append({"funding_group_id":group["funding_group_id"],
                "status":"budget_partial","reserved_alpha_weight":group["alpha_weight"],"candidates":{}})
            continue
        feasible = [policy for policy in group["policies"] if policy["id"] in evaluated]
        if not feasible:
            result = {"funding_group_id":group["funding_group_id"], "status":"needs_calibration",
                "reserved_alpha_weight":group["alpha_weight"], "reason":"source_comparison_baseline_unavailable",
                "candidates":{policy["id"]:{"status":"needs_calibration", "reason":"source_comparison_baseline_unavailable"}
                    for policy in group["policies"]}}
            calibration["groups"].append(result)
            calibration["candidates"].update(result["candidates"])
            continue
        errors, losses, dates, group_records, group_invalid = _calibration_matrix(context, paths, feasible, baselines)
        records.update(group_records); cal_invalid.update(group_invalid)
        cal_context = {**context,"matrix_source_hash":fingerprint(group_records),"mpc_matrix_records":group_records}
        first = next(iter(group_records),None)
        cal_context["mpc_calibration_lineage"] = {"policies":group["policies"],
            "capital_amount":float(context["snapshot"]["equity"]),
            "origins":[{"date":date,"origin_at":group_records[first][i]["origin_at"],
                "outcome_available_at":group_records[first][i]["label_available_at"],
                "source_maturity_hash":fingerprint(group_records[first][i]["source_hashes"]),
                "simulation_hash":fingerprint({identity:group_records[identity][i] for identity in sorted(group_records)})}
                for i,date in enumerate(dates)] if first is not None else []}
        result = calibrate(errors,losses,dates,cal_context,frozen_family_hash=group["policy_hash"],
            total_family_count=family_count,policy_count_bound=len(group["policies"]),
            funding_registry=frozen["funding_registry"],funding_registry_hash=frozen["funding_registry_hash"],
            funding_group_id=group["funding_group_id"])
        calibration["groups"].append(result)
        calibration["candidates"].update(result["candidates"])
    calibration["status"] = "calibrated" if any(row["status"]=="calibrated" for row in calibration["groups"]) else "needs_calibration"
    economic, state = context["spec"]["trade_policy"]["economic"], context["trade_state"]
    import newtrade_guard
    newtrade_guard.validate_state(state, context["account_id"], context["decision_at"], economic["fee_window_days"])
    for policy in evaluated.values():
        baseline = evaluated.get(baselines[str(policy["proposed_contribution"])]["id"])
        if baseline is None:
            continue
        capital = numeric(context["snapshot"]["equity"])+numeric(policy["proposed_contribution"])
        principal = numeric(context["risk_state"]["net_principal"])+numeric(policy["proposed_contribution"])
        budget = capital-principal*(1-numeric(context["risk_state"]["loss_tolerance"]))
        risk = exact_values[policy["id"]]["risk"]
        baseline_risk = exact_values[baseline["id"]]["risk"]
        advantage = numeric(policy["point"]["terminal_wealth"])-numeric(baseline["point"]["terminal_wealth"])
        projection = policy["current_projection"]
        directions = [(row["code"], "buy") for row in projection["buys"]]+[(row["code"], "sell") for row in projection["sells"]]
        reversal = any(code in state["last_executed_by_code"] and state["last_executed_by_code"][code]["side"] != side for code, side in directions)
        checked = calibration.get("candidates", {}).get(policy["id"], {})
        buffer, upper = checked.get("optimism_buffer_amount"), checked.get("risk_cvar_upper")
        cash_identity = (policy["mode"] == "baseline" and not any(_decimal(row["shares"]) for row in context["snapshot"]["positions"])
            and _decimal(context["snapshot"]["reserved_cash"]) == 0 and _decimal(context["snapshot"]["unsettled_cash"]) == 0
            and _decimal(context["snapshot"]["available_cash"]) == _decimal(context["snapshot"]["equity"])
            and all(numeric(row["terminal_wealth"]) == capital and row["fees"] == 0 for row in [policy["point"],*policy["selection"]]))
        if cash_identity:
            buffer, upper = 0., 0.
        delta = numeric(economic["minimum_reversal_advantage_amount"] if reversal else economic["minimum_net_advantage_amount"])
        rolling = numeric(state["actual_window_fees"])+max((numeric(row["fees"]) for row in policy["selection"]), default=0)
        # A comparison baseline is not automatically a feasible winner. The
        # registered principal-loss constraint applies to every ordinary policy.
        reducing = bool((budget < 0 or baseline_risk > budget)
            and policy["current_action"]["sells"] and not policy["current_action"]["buys"]
            and not policy["future_rule"]["weights"] and not policy["future_rule"].get("conditional_sells")
            and baseline_risk-risk >= numeric(economic["minimum_risk_reduction_amount"]))
        reasons = list(governance["reasons"])
        calibrated = cash_identity or checked.get("status") == "calibrated" and buffer is not None and upper is not None
        if not calibrated:
            reasons.append("independent_path_policy_calibration_required")
        if risk > budget or upper is not None and numeric(upper) > budget:
            reasons.append("principal_risk_budget_exceeded")
        if rolling > numeric(economic["maximum_rolling_fee_amount"]):
            reasons.append("registered_rolling_fee_budget_exceeded")
        if not projection["concentration_feasible"]:
            reasons.append("source_exposure_or_concentration_constraint")
        # A baseline has no advantage over itself. Registered risk restoration
        # is a risk objective, not an assertion of a positive financial alpha.
        if policy["mode"] != "baseline" and not reducing and calibrated and advantage <= delta+numeric(buffer):
            reasons.append("net_advantage_below_independent_error_reversal_threshold")
        cash_ok = all(row.get("cash_deadline", {}).get("passes", False)
                      for row in [policy["point"], *policy["selection"]]) if cash_requirement(context) else True
        if not cash_ok:
            reasons.append("cash_deadline_requirement_not_met_in_every_path")
        policy["eligible"] = not reasons
        # An infeasible risk state may support a legal pure redemption, but
        # empirical improvement cannot certify the registered risk bound.
        recovery = bool(governance["eligible"] and not policy["eligible"] and reducing and cash_ok
            and projection["concentration_feasible"] and rolling <= numeric(economic["maximum_rolling_fee_amount"]))
        policy["recovery_eligible"] = recovery
        violation = max(0, risk-budget)
        exact_values[policy["id"]]["violation"] = violation
        policy["risk_violation_amount"] = float(violation)
        policy.update(net_advantage=float(advantage), conditional_lower_bound=float(advantage-numeric(buffer)) if buffer is not None else None,
            expected_net_return=float(exact_values[policy["id"]]["profit"]/capital) if capital else None, cvar_loss_fraction=float(risk/capital) if capital else None,
            trade_guard={"status": "cash_requirement_failed" if not cash_ok else "passed" if policy["eligible"] else "risk_recovery" if recovery else "needs_calibration" if buffer is None else "no_action",
                "eligible": policy["eligible"], "recovery_eligible": recovery, "risk_status": "within_verified_budget" if policy["eligible"] else "risk_not_restored",
                "risk_evidence_basis": "source_cash_identity" if cash_identity else "independent_policy_calibration" if calibrated else "empirical_path_only",
                "risk_violation_amount": policy["risk_violation_amount"], "risk_recovery_basis": "registered_empirical_violation_priority_not_probability_guarantee" if recovery else None,
                "reversal": reversal, "net_advantage": float(advantage), "optimism_buffer_amount": buffer,
                "required_advantage": float(delta), "risk_budget": float(budget), "risk_cvar_upper": upper, "rolling_fee_total": float(rolling),
                "fee_budget": float(economic["maximum_rolling_fee_amount"]), "reasons": reasons})
    for group in frozen["groups"]:
        candidates = [evaluated[policy["id"]] for policy in group["policies"] if policy["id"] in evaluated]
        candidates.sort(key=lambda policy: (0 if policy["eligible"] else 1 if policy.get("recovery_eligible") else 2,
            0 if policy["eligible"] else exact_values[policy["id"]]["violation"],
            -exact_values[policy["id"]]["profit"], exact_values[policy["id"]]["risk"],
            exact_values[policy["id"]]["fees"], exact_values[policy["id"]]["turnover"], policy["id"]))
        best = next((policy for policy in candidates if policy["eligible"]), None)
        recovery = next((policy for policy in candidates if policy.get("recovery_eligible")), None)
        out["funding_options"].append({"proposed_contribution": group["proposed_contribution"], "candidates": candidates,
            "best": best or recovery, "ordinary_best": best, "recovery_best": recovery if best is None else None,
            "decision_status": "feasible_selected" if best else "risk_recovery" if recovery else "risk_unresolved",
            "current_action_scope_complete": group["current_action_scope_complete"],
            "build_status":group["build_status"],"funding_group_id":group["funding_group_id"],
            "policy_count_upper":group["policy_count_upper"],"reserved_alpha_weight":group["alpha_weight"],
            "required_actions":group["required_actions"]})
    zero = next((group for group in out["funding_options"] if group["proposed_contribution"] == 0), None)
    selected = zero["best"] if zero else None
    deadline_blocked = cash_requirement(context) is not None and selected is None
    source_blocked = selected is None and bool(invalid)
    out.update(status="research_ready" if selected else "partial", decision_status=zero["decision_status"] if zero else "risk_unresolved",
        selected_policy=selected, current_action=selected["current_action"] if selected else {"buys": [], "sells": []},
        frozen_policy_family=frozen, calibration=calibration, calibration_records=records, source_invalid_policies={**invalid, **cal_invalid},
        selection_scope="registered_finite_feasible_net_wealth_else_empirical_violation_priority",
        risk_scope="empirical_terminal_CVaR_on_coarse_declared_partition_not_full_dynamic_conditional_risk_or_future_loss_guarantee")
    if deadline_blocked:
        out.update(reason="cash_deadline_requirement_not_established", required_actions=[{
            "action": "revise_cash_requirement_or_complete_source_cash_path",
            **cash_requirement(context)}])
    if source_blocked and not deadline_blocked:
        out.update(reason="source_policy_feasibility_unavailable",required_actions=[{"action":"complete_source_policy_fee_or_execution_scope","reasons":sorted(set(invalid.values()))}])
    if selected is None and not deadline_blocked and not source_blocked:
        out.update(reason="no_policy_satisfies_registered_risk_or_calibration_contract", required_actions=[{"action":"review_risk_or_complete_independent_policy_calibration"}])
    if not governance["eligible"]:
        out.update(qualification_scope=governance, reason="registered_trade_governance_required",
                   required_actions=[{"action": "declare_active_policy_and_review_budget", "reasons": governance["reasons"]}])
    out["result_hash"] = fingerprint(out)
    return out


def validate_result(value, inputs):
    expected = optimise(inputs["context"], inputs["paths"], inputs["comparison"], family_count=inputs.get("family_count", 1))
    require(value == expected, "MPC policy differs from sealed source/path reconstruction")
    independent = None
    if value.get("frozen_policy_family", {}).get("status") == "frozen":
        from verify import numerical_invariants
        independent = numerical_invariants(inputs["context"], {"mpc": value, "paths": inputs["paths"], "orders": {"orders": []}})
    if value["status"] == "research_ready":
        for group in value["funding_options"]:
            for policy in group["candidates"]:
                for valuation in [policy["point"], *policy["selection"]]: verify_cash_flow(valuation)
        selected = value["selected_policy"]
        require(selected is None or selected["proposed_contribution"] == 0, "Unconfirmed extra funding entered current orders")
    return {"status": "passed", "result_status": value["status"], "scope": "source_paths_cash_policy_risk_not_market_profitability",
        "independent_decision_validation": independent,
        "checks": {"exact_source_path_policy_reconstruction": True,
            "ready_policy_cash_flows_verified": value["status"] == "research_ready",
            "current_orders_never_use_unconfirmed_funding": value["status"] != "research_ready"
                or value["selected_policy"] is None or value["selected_policy"]["proposed_contribution"] == 0}}
