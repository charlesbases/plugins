"""Pure bounded account projection; Store owns source events and global ID deduplication."""
import copy
import re
import datetime as dt
from decimal import Decimal, localcontext, ROUND_HALF_UP, ROUND_DOWN
from zoneinfo import ZoneInfo
from contracts import ContractError, fields, fingerprint, instant, require, validate_redemption_confirmation

STATE_FIELDS = {"schema_version", "currency", "sequence", "last_event", "effective_at", "known_at", "recorded_at",
                "cash", "lots", "orders", "receivables", "pending_subscriptions", "marks", "units", "unknown", "projection_cutoff",
                "mark_dates", "transfers", "performance_pending", "reconciliation"}
ORDER_FIELDS = {"order_id", "code", "side", "currency", "lot_id", "cash_limit", "share_limit", "fee_rate",
                "settlement_days", "share_step", "context_hash"}
EVENT_DATA = {
    "opening": {"cash", "lots", "prices", "price_dates"}, "valuation": {"prices", "price_dates"},
    "cashflow": {"transfer_id", "revision_id", "previous_revision", "amount", "valuation"},
    "order_reserved": {"order"}, "cancel_requested": {"order_id"}, "cancel_confirmed": {"order_id"},
    "buy_fill": {"order_id", "fill_id", "lot_id", "shares", "price", "fee", "final", "price_date", "holding_started_at", "ownership_at", "gross_amount", "cash_debit"},
    "sell_fill": {"order_id", "fill_id", "shares", "price", "fee", "settlement_at", "final", "price_date", "gross_amount", "net_amount"},
    "subscription_pending": {"order_id", "payment_id", "amount"},
    "subscription_confirmed": {"payment_id", "fill_id", "lot_id", "shares", "price", "fee", "final", "price_date", "holding_started_at", "ownership_at", "gross_amount", "cash_debit"},
    "settlement": {"receivable_id", "amount"}, "dividend_paid": {"receivable_id", "amount"},
    "dividend_declared": {"distribution_id", "code", "per_share", "pay_at", "record_at", "entitled_shares"},
    "dividend_reinvested": {"receivable_id", "fill_id", "lot_id", "code", "shares", "price", "fee", "cash_adjustment", "price_date", "holding_started_at", "ownership_at", "gross_amount"},
    "lot_metadata_confirmed": {"lot_id", "holding_started_at", "ownership_at", "price_date", "evidence_ref"},
    "external_fill_confirmed": {"side", "code", "fill_id", "lot_id", "shares", "price", "price_date", "gross_amount", "fee", "cash_amount",
                                "settlement_at", "holding_started_at", "ownership_at", "evidence_ref"},
    "account_snapshot_confirmed": {"cash", "lots", "prices", "price_dates", "orders", "receivables", "pending_subscriptions", "evidence_ref"},
    "performance_start": {"cash", "confirmed_no_prior_economic_activity", "resolved_unknown_ids", "user_source"},
    "split": {"code", "ratio"}, "unknown": {"reason"},
    "resolve_unknown": {"unknown_id", "evidence_ref", "resolution"}}


class RebuildRequired(ContractError):
    code = "rebuild_required"


def amount(value, label="amount", minimum=None):
    require(type(value) is str and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value), label+" must be a decimal string")
    result = Decimal(value)
    require(result.is_finite() and len(result.as_tuple().digits) <= 80 and result.as_tuple().exponent >= -60,
            label+" exceeds ledger precision")
    require(minimum is None or result >= minimum, label+" is below its permitted minimum")
    return result


def decimal(value):
    require(value.is_finite(), "Nonfinite ledger result")
    return "0" if value == 0 else format(value.normalize(), "f")


def receipt_clock(value):
    """Preserve source date precision without inventing an arrival instant."""
    if type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return dt.date.fromisoformat(value), None
    clock = instant(value)
    return clock.astimezone(ZoneInfo("Asia/Shanghai")).date(), clock


def _receipt_follows(value, effective_at):
    day, clock = receipt_clock(value)
    effective = instant(effective_at)
    return (clock >= effective if clock is not None else
            day >= effective.astimezone(ZoneInfo("Asia/Shanghai")).date())


def _text(value, label):
    require(type(value) is str and bool(value.strip()), label+" must be nonempty")


def _prices(values):
    require(type(values) is dict, "prices must be a mapping")
    for code, value in values.items():
        _text(code, "price code")
        require(amount(value, "price", 0) > 0, "Price must be positive")
    return copy.deepcopy(values)


def initial_state(currency):
    require(currency == "CNY", "Only CNY is supported by this ledger contract")
    return {"schema_version": 4, "currency": currency, "sequence": 0, "last_event": None,
            "effective_at": None, "known_at": None, "recorded_at": None, "cash": "0", "lots": {}, "orders": {},
            "receivables": {}, "pending_subscriptions": {}, "marks": {}, "mark_dates": {}, "units": "0", "unknown": [],
            "transfers": {}, "performance_pending": [], "reconciliation": [], "projection_cutoff": None}


def _state(state):
    fields(state, STATE_FIELDS, label="ledger state")
    require(state["schema_version"] == 4 and state["currency"] == "CNY", "Ledger requires the offline v4 migration")
    require(type(state["sequence"]) is int and state["sequence"] >= 0, "Invalid ledger sequence")
    amount(state["cash"], "cash"); amount(state["units"], "units", 0)
    for name in ("lots", "orders", "receivables", "pending_subscriptions", "marks", "mark_dates", "transfers"):
        require(type(state[name]) is dict, name+" must be a mapping")
    require(type(state["unknown"]) is list, "unknown must be a list")
    require(type(state["performance_pending"]) is list and type(state["reconciliation"]) is list, "Account gaps must be lists")


def reserved_cash(state):
    return sum((amount(row["remaining_cash"], "reserved cash", 0) for row in state["orders"].values()), Decimal(0))


def reserved_shares(state, lot_id):
    return sum((amount(row["remaining_shares"], "reserved shares", 0) for row in state["orders"].values()
                if row["lot_id"] == lot_id), Decimal(0))


def _equity(state, prices, provisional_payments=False):
    require(not state["unknown"] and (provisional_payments or not state["pending_subscriptions"]), "Unknown exposure requires reconciliation")
    value = amount(state["cash"])
    for lot in state["lots"].values():
        require(lot["code"] in prices, "Missing price for "+lot["code"])
        value += amount(lot["shares"])*amount(prices[lot["code"]])
    value += sum((amount(row["amount"]) for row in state["receivables"].values()), Decimal(0))
    if provisional_payments:
        value += sum((amount(row["amount"]) for row in state["pending_subscriptions"].values()), Decimal(0))
    return value


def validate_order(order, currency="CNY"):
    fields(order, ORDER_FIELDS, {"redemption_schedule", "fee_contract"}, label="order")
    for name in ("order_id", "code", "context_hash"):
        _text(order[name], name)
    require(order["currency"] == currency and order["side"] in ("buy", "sell"), "Invalid order currency or side")
    cash, shares = amount(order["cash_limit"], "cash limit", 0), amount(order["share_limit"], "share limit", 0)
    require(0 <= amount(order["fee_rate"]) < 1 and amount(order["share_step"]) > 0, "Invalid order fee or precision")
    require(type(order["settlement_days"]) is int and order["settlement_days"] >= 0, "Invalid settlement delay")
    if order["side"] == "buy":
        require(cash > 0 and shares == 0 and order["lot_id"] is None, "Buy requires only a cash limit")
        require("redemption_schedule" not in order, "Buy cannot carry a redemption schedule")
    else:
        require(shares > 0 and cash == 0 and type(order["lot_id"]) is str, "Sell requires a lot and shares")


def redemption_rate(terms, acquired_at, at):
    """Frozen fee bands evaluated on the actual Shanghai holding calendar."""
    if "redemption_schedule" not in terms:
        rate = amount(terms.get("redemption_fee", terms.get("fee_rate")), "redemption fee", 0)
        require(rate < 1, "Redemption fee must be below one")
        return decimal(rate)
    schedule = terms["redemption_schedule"]
    require(type(schedule) is list and bool(schedule), "Nonempty redemption schedule required")
    zone = ZoneInfo("Asia/Shanghai")
    age = (instant(at).astimezone(zone).date()-instant(acquired_at).astimezone(zone).date()).days
    require(age >= 0, "Holding acquisition follows the fee cutoff")
    previous, selected = -1, None
    for band in schedule:
        fields(band, {"minimum_days", "rate"}, label="redemption fee band")
        start = band["minimum_days"]
        rate = amount(band["rate"], "redemption band rate", 0)
        require(type(start) is int and start >= 0 and start > previous and rate < 1, "Invalid fee schedule order/rate")
        if previous == -1:
            require(start == 0, "Fee schedule must cover new holdings")
        if age >= start:
            selected = rate
        previous = start
    require(selected is not None, "Fee schedule has no matching holding age")
    return decimal(selected)


def _lot(state, identity, code, shares, at, ownership_at, price_date):
    _text(identity, "lot ID")
    require(identity not in state["lots"], "Each confirmed buy needs a fresh lot ID")
    instant(at); instant(ownership_at)
    require(dt.date.fromisoformat(price_date).isoformat() == price_date, "Canonical acquisition price date required")
    state["lots"][identity] = {"lot_id": identity, "code": code, "shares": decimal(shares), "acquired_at": at,
                              "ownership_at": ownership_at, "price_date": price_date}


def _difference(state, event, kind, expected, actual):
    if expected != actual:
        state["reconciliation"].append({"event_id": event["id"], "kind": kind,
                                        "expected": str(expected), "actual": str(actual)})


def _mark_dates(prices, dates, known_at):
    require(type(dates) is dict and set(prices) == set(dates), "Every mark needs its own price date")
    for day in dates.values():
        require(type(day) is str and dt.date.fromisoformat(day).isoformat() == day
                and day <= instant(known_at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                "Price date must be canonical and already known")
    return copy.deepcopy(dates)


def _cashflow(state, event):
    data = event["data"]
    for key in ("transfer_id", "revision_id"):
        _text(data[key], key)
    if data["transfer_id"] in state["transfers"] or data["previous_revision"] is not None:
        raise RebuildRequired("Transfer revision requires full immutable-history replay")
    flow, valuation = amount(data["amount"]), data["valuation"]
    if flow == 0:
        state["transfers"][data["transfer_id"]] = data["revision_id"]
        return
    exact = not state["performance_pending"] and not state["unknown"] and not state["pending_subscriptions"]
    prices = {}
    if state["lots"]:
        exact = exact and valuation is not None
        if valuation is not None:
            fields(valuation, {"at", "prices", "price_dates", "evidence_refs"}, {"source_market_id"}, label="flow valuation")
            require(instant(valuation["at"]) == instant(event["effective_at"]), "Cashflow valuation must bind the exact economic instant")
            prices = _prices(valuation["prices"])
            _mark_dates(prices, valuation["price_dates"], event["known_at"])
            day = instant(event["effective_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            exact = bool(exact and set(prices) == {lot["code"] for lot in state["lots"].values()}
                    and all(value == day for value in valuation["price_dates"].values())
                    and isinstance(valuation["evidence_refs"], list) and (valuation["evidence_refs"] or valuation.get("source_market_id")))
    if exact:
        value, units = _equity(state, prices), amount(state["units"])
        nav = value / units if units else Decimal(1)
        if nav > 0 and units + flow / nav >= 0:
            state["units"] = decimal(units + flow / nav)
        else:
            exact = False
    if not exact:
        state["performance_pending"].append({"transfer_id": data["transfer_id"], "event_id": event["id"],
                                               "reason": "flow_time_equity_unverified"})
    state["cash"] = decimal(amount(state["cash"]) + flow)
    state["transfers"][data["transfer_id"]] = data["revision_id"]


def _fill(state, event, data, order, prepaid=None):
    require(type(data["final"]) is bool, "Explicit terminal fill status required")
    _text(data["fill_id"], "fill ID")
    shares, price, fee = (amount(data[key], key, 0) for key in ("shares", "price", "fee"))
    require(shares > 0 and price > 0, "Invalid fill quantity/price")
    require(dt.date.fromisoformat(data["price_date"]).isoformat() == data["price_date"]
            and data["price_date"] <= instant(event["known_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "Confirmed execution price date required")
    if shares % amount(order["share_step"]) != 0:
        _difference(state, event, "share_precision_deviation", order["share_step"], decimal(shares))
    gross = amount(data["gross_amount"], "confirmed gross consideration", 0)
    expected_gross = (shares*price).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    require(gross > 0, "Fill consideration rounds to zero")
    priced_stamp = dt.datetime.combine(dt.date.fromisoformat(data["price_date"]), dt.time(), ZoneInfo("Asia/Shanghai")).isoformat()
    expected_fee = None
    if "fee_contract" not in order:
        rate = (redemption_rate(order, state["lots"][order["lot_id"]]["acquired_at"], priced_stamp)
                if order["side"] == "sell" else order["fee_rate"])
        expected_fee = (gross*amount(rate)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if "fee_contract" in order:
        from fee_contract import quote_entry, quote_exit_at_age_details
        try:
            if order["side"] == "sell":
                import trading_calendar
                contract = order["fee_contract"]
                holding = contract["holding"]
                end = priced_stamp if holding["end_event"] == "execution_date" else data.get("actual_redemption_confirmed_at")
                require(holding["end_event"] in ("execution_date", "confirmation_date"), "Unsupported actual holding endpoint")
                precision = contract["trade_precision"]
                expected_gross = shares*price
                if precision.get("redemption_net_rounding", "half_up") == "half_up":
                    expected_gross = expected_gross.quantize(Decimal(precision["money_step"]), rounding=ROUND_HALF_UP)
                if end is None:
                    expected_fee = None
                    state["reconciliation"].append({"event_id": event["id"], "kind": "expected_quote_unknown",
                        "field": "actual_redemption_confirmed_at", "reason": "Source fee holding endpoint requires an explicit actual redemption confirmation",
                        "required_actions": [{"action": "provide_actual_redemption_confirmation", "fill_id": data["fill_id"]}]})
                else:
                    age = trading_calendar.holding_days(state["lots"][order["lot_id"]]["acquired_at"], end, holding)
                    require(age >= holding["minimum_days"], "Actual redemption precedes the source lot unlock")
                    details = quote_exit_at_age_details(shares*price, contract, age)
                    expected_gross, expected_fee = details["gross"], details["contractual_fee"]
            else:
                expected_fee = quote_entry(data["cash_debit"], order["fee_contract"],
                    price=price, share_step=order["share_step"])["fee"]
        except ValueError as exc:
            expected_fee = None
            state["reconciliation"].append({"event_id": event["id"], "kind": "dealing_contract_deviation", "reason": str(exc)})
    _difference(state, event, "share_rounding_transfer", expected_gross, gross)
    if expected_fee is not None:
        _difference(state, event, "fee_deviation", expected_fee, fee)
    if order["side"] == "buy":
        debit = amount(data["cash_debit"], "confirmed cash debit", 0)
        _difference(state, event, "cash_component_reconciliation", gross + fee, debit)
        if prepaid is None:
            if debit > amount(order["remaining_cash"]):
                _difference(state, event, "reserved_cash_exceeded", order["remaining_cash"], decimal(debit))
            state["cash"] = decimal(amount(state["cash"])-debit)
            order["remaining_cash"] = decimal(max(Decimal(0), amount(order["remaining_cash"])-debit))
        else:
            if debit > prepaid:
                _difference(state, event, "subscription_payment_deviation", prepaid, debit)
            state["cash"] = decimal(amount(state["cash"])+prepaid-debit)
        require(instant(data["holding_started_at"]) <= instant(event["known_at"])
                and instant(data["ownership_at"]) <= instant(event["known_at"]), "Confirmed holding/ownership dates cannot be future")
        _lot(state, data["lot_id"], order["code"], shares, data["holding_started_at"], data["ownership_at"], data["price_date"])
    else:
        require(shares <= amount(order["remaining_shares"]), "Fill exceeds reserved shares")
        lot = state["lots"][order["lot_id"]]
        left = amount(lot["shares"])-shares
        require(left >= 0, "Invalid sale quantity")
        net = amount(data["net_amount"], "confirmed net redemption amount", 0)
        expected_net = gross-fee
        if "fee_contract" in order:
            precision = order["fee_contract"]["trade_precision"]
            step = Decimal(precision["money_step"])
            rounding = ROUND_DOWN if precision.get("redemption_net_rounding", "half_up") == "down_fund" else ROUND_HALF_UP
            expected_net = (expected_net/step).to_integral_value(rounding=rounding)*step
        _difference(state, event, "redemption_component_reconciliation", expected_net, net)
        lot["shares"] = decimal(left)
        order["remaining_shares"] = decimal(amount(order["remaining_shares"])-shares)
        require(_receipt_follows(data["settlement_at"], event["effective_at"]), "Settlement predates fill")
        require(data["fill_id"] not in state["receivables"], "Duplicate active receivable")
        state["receivables"][data["fill_id"]] = {"kind": "redemption", "amount": decimal(net),
                                                  "due_at": data["settlement_at"], "order_id": order["order_id"]}
        if left == 0:
            del state["lots"][order["lot_id"]]
    if data["final"]:
        require(not any(row["order_id"] == order["order_id"] for row in state["pending_subscriptions"].values()),
                "Order has another unconfirmed payment")
        del state["orders"][order["order_id"]]


def _event(event):
    fields(event, {"id", "type", "sequence", "effective_at", "known_at", "recorded_at", "data"}, label="ledger event")
    _text(event["id"], "event ID")
    require(event["type"] in EVENT_DATA and type(event["sequence"]) is int and event["sequence"] > 0, "Invalid event type/sequence")
    optional = {"reconciliation"} if event["type"] == "valuation" else {"actual_redemption_confirmed_at"} if event["type"] == "sell_fill" else ()
    fields(event["data"], EVENT_DATA[event["type"]], optional, label=event["type"]+" data")
    require(instant(event["effective_at"]) <= instant(event["known_at"]) <= instant(event["recorded_at"]), "Event times are not causal")
    validate_redemption_confirmation(event)


def financial_identity(event):
    """Natural transaction identities outlive active orders and receivables."""
    kind, data = event["type"], event["data"]
    if kind in ("buy_fill", "sell_fill", "subscription_confirmed", "external_fill_confirmed"):
        identity = ("fill", data["fill_id"])
    elif kind == "subscription_pending":
        identity = ("payment", data["payment_id"])
    elif kind == "order_reserved":
        identity = ("order", data["order"]["order_id"])
    elif kind == "dividend_declared":
        identity = ("distribution", data["distribution_id"])
    elif kind in ("settlement", "dividend_paid", "dividend_reinvested"):
        identity = ("settled_receivable", data["receivable_id"])
    elif kind in ("cancel_requested", "cancel_confirmed"):
        identity = (kind, data["order_id"])
    elif kind == "split":
        identity = ("split", fingerprint([data["code"], instant(event["effective_at"]).isoformat()]))
    elif kind == "resolve_unknown":
        identity = ("resolution", data["unknown_id"])
    elif kind == "cashflow":
        identity = ("transfer_revision", data["transfer_id"], data["revision_id"])
    else:
        return None
    return {"key": fingerprint(list(identity)), "content_hash": fingerprint({
        "type": kind, "effective_at": instant(event["effective_at"]).isoformat(), "data": data})}


def apply_event(state, event):
    """Apply a single known fact. Late facts require rebuild using Store history."""
    _state(state); _event(event)
    require(state["projection_cutoff"] is None, "Historical projection is observation-only; rebuild current state first")
    digest = fingerprint(event)
    if state["last_event"] and state["last_event"]["id"] == event["id"]:
        require(state["last_event"]["hash"] == digest, "Event ID content conflict")
        return copy.deepcopy(state)
    require(event["sequence"] == state["sequence"]+1, "Event sequence gap or duplicate")
    if state["effective_at"] is not None and instant(event["effective_at"]) < instant(state["effective_at"]):
        raise RebuildRequired("Late confirmation requires economic-order checkpoint replay")
    out, data, kind = copy.deepcopy(state), event["data"], event["type"]
    with localcontext() as context:
        context.prec = 50
        if kind == "performance_start":
            require(data["confirmed_no_prior_economic_activity"] is True and amount(out["cash"]) == 0
                    and amount(out["units"]) == 0 and not any(out[name] for name in
                        ("lots", "orders", "receivables", "pending_subscriptions", "transfers", "performance_pending")),
                    "A first performance start cannot replace existing economic history")
            require(type(data["resolved_unknown_ids"]) is list
                    and set(data["resolved_unknown_ids"]) == {row["id"] for row in out["unknown"]},
                    "Initial confirmation must explicitly cover every recorded unknown")
            fields(data["user_source"], {"message", "confirmed_at"}, label="first economic activity confirmation")
            _text(data["user_source"]["message"], "initial user confirmation")
            require(instant(data["user_source"]["confirmed_at"]) <= instant(event["known_at"]), "Initial confirmation is not yet known")
            cash = amount(data["cash"], "first confirmed available cash", 0)
            require(cash > 0, "Positive actual first cash required")
            out["cash"], out["units"], out["unknown"] = decimal(cash), decimal(cash), []
        elif kind == "opening":
            require(state["sequence"] == 0, "Opening requires an empty projection")
            out["cash"], out["marks"] = decimal(amount(data["cash"], "opening cash", 0)), _prices(data["prices"])
            out["mark_dates"] = _mark_dates(out["marks"], data["price_dates"], event["known_at"])
            require(type(data["lots"]) is list, "Opening lots must be a list")
            for lot in data["lots"]:
                fields(lot, {"lot_id", "code", "shares", "acquired_at", "ownership_at", "price_date"}, label="opening lot")
                require(instant(lot["acquired_at"]) <= instant(event["effective_at"]) and amount(lot["shares"]) > 0, "Invalid initial lot")
                require(instant(lot["ownership_at"]) <= instant(event["effective_at"]), "Opening ownership follows account snapshot")
                _lot(out, lot["lot_id"], lot["code"], amount(lot["shares"]), lot["acquired_at"], lot["ownership_at"], lot["price_date"])
            out["units"] = decimal(_equity(out, out["marks"]))
        elif kind == "valuation":
            out["marks"].update(_prices(data["prices"]))
            out["mark_dates"].update(_mark_dates(data["prices"], data["price_dates"], event["known_at"]))
            if "reconciliation" in data:
                require(type(data["reconciliation"]) is list
                        and all(type(row) is dict and row.get("kind") == "corporate_action_unverified" for row in data["reconciliation"]),
                        "Only explicit corporate-action reconciliation findings belong to valuation")
                out["reconciliation"] = [row for row in out["reconciliation"] if row["kind"] != "corporate_action_unverified"] + copy.deepcopy(data["reconciliation"])
        elif kind == "cashflow":
            _cashflow(out, event)
        elif kind == "order_reserved":
            order = copy.deepcopy(data["order"]); validate_order(order, out["currency"])
            require(not out["unknown"] and not out["pending_subscriptions"], "Unconfirmed exposure blocks new orders")
            require(order["order_id"] not in out["orders"], "Order is already active")
            if order["side"] == "buy":
                require(amount(order["cash_limit"]) <= amount(out["cash"])-reserved_cash(out), "Order exceeds settled free cash")
            else:
                require(order["lot_id"] in out["lots"], "Unknown sale lot")
                lot = out["lots"][order["lot_id"]]
                require(lot["code"] == order["code"] and amount(order["share_limit"]) <= amount(lot["shares"])-reserved_shares(out, order["lot_id"]), "Order exceeds free shares")
                require(amount(order["fee_rate"]) == amount(redemption_rate(order, lot["acquired_at"], event["effective_at"])), "Quoted fee differs from holding-period terms")
            out["orders"][order["order_id"]] = {**order, "remaining_cash": order["cash_limit"], "remaining_shares": order["share_limit"], "cancel_requested": False}
        elif kind in ("cancel_requested", "cancel_confirmed"):
            require(data["order_id"] in out["orders"], "Unknown cancellation order")
            order = out["orders"][data["order_id"]]
            if kind == "cancel_requested":
                order["cancel_requested"] = True
            else:
                require(order["cancel_requested"] and not any(row["order_id"] == data["order_id"] for row in out["pending_subscriptions"].values()), "Cancellation is unconfirmed or has unresolved payment")
                del out["orders"][data["order_id"]]
        elif kind in ("buy_fill", "sell_fill"):
            require(data["order_id"] in out["orders"], "Fill lacks a reserved order")
            order = out["orders"][data["order_id"]]
            require(order["side"] == ("buy" if kind == "buy_fill" else "sell"), "Wrong fill side")
            _fill(out, event, data, order)
        elif kind == "subscription_pending":
            require(data["order_id"] in out["orders"], "Unknown subscription order")
            order = out["orders"][data["order_id"]]; paid = amount(data["amount"])
            require(order["side"] == "buy" and 0 < paid <= amount(order["remaining_cash"]), "Subscription exceeds reservation")
            require(data["payment_id"] not in out["pending_subscriptions"], "Duplicate payment")
            out["cash"] = decimal(amount(out["cash"])-paid)
            order["remaining_cash"] = decimal(amount(order["remaining_cash"])-paid)
            out["pending_subscriptions"][data["payment_id"]] = {"order_id": data["order_id"], "amount": decimal(paid)}
        elif kind == "subscription_confirmed":
            require(data["payment_id"] in out["pending_subscriptions"], "Unknown pending payment")
            paid = out["pending_subscriptions"].pop(data["payment_id"])
            _fill(out, event, data, out["orders"][paid["order_id"]], amount(paid["amount"]))
        elif kind in ("settlement", "dividend_paid"):
            require(data["receivable_id"] in out["receivables"], "Unknown or settled receivable")
            row = out["receivables"][data["receivable_id"]]
            require(kind != "dividend_paid" or row["kind"] == "dividend", "Wrong receivable type")
            received = amount(data["amount"], "confirmed settlement amount", 0)
            _difference(out, event, "settlement_amount_deviation", amount(row["amount"]), received)
            due_day, due_clock = receipt_clock(row["due_at"])
            actual_clock = instant(event["effective_at"])
            _difference(out, event, "settlement_time_deviation", due_clock if due_clock is not None else due_day,
                        actual_clock if due_clock is not None else actual_clock.astimezone(ZoneInfo("Asia/Shanghai")).date())
            out["cash"] = decimal(amount(out["cash"])+received)
            del out["receivables"][data["receivable_id"]]
        elif kind == "dividend_declared":
            require(data["distribution_id"] not in out["receivables"] and _receipt_follows(data["pay_at"], event["effective_at"]), "Invalid dividend identity or payment time")
            require(instant(data["record_at"]) <= instant(event["effective_at"]), "Dividend record instant follows entitlement")
            shares = amount(data["entitled_shares"], "confirmed record-date entitlement", 0)
            out["receivables"][data["distribution_id"]] = {"kind": "dividend", "amount": decimal(shares*amount(data["per_share"], "dividend", 0)),
                "due_at": data["pay_at"], "code": data["code"], "record_at": data["record_at"], "entitled_shares": data["entitled_shares"]}
        elif kind == "dividend_reinvested":
            require(data["receivable_id"] in out["receivables"], "Unknown reinvestment entitlement")
            row = out["receivables"][data["receivable_id"]]
            require(row["kind"] == "dividend" and row["code"] == data["code"], "Reinvestment must consume its own dividend")
            shares, price, fee = (amount(data[key], key, 0) for key in ("shares", "price", "fee"))
            require(shares > 0 and price > 0, "Reinvestment quantity/price must be positive")
            cash = amount(data["cash_adjustment"])
            gross = amount(data["gross_amount"], "confirmed reinvestment amount", 0)
            _difference(out, event, "share_rounding_transfer", (shares * price).quantize(Decimal(".01"), rounding=ROUND_HALF_UP), gross)
            _difference(out, event, "reinvestment_amount_deviation", amount(row["amount"]), gross + fee + cash)
            _lot(out, data["lot_id"], data["code"], shares, data["holding_started_at"], data["ownership_at"], data["price_date"])
            out["cash"] = decimal(amount(out["cash"]) + cash)
            del out["receivables"][data["receivable_id"]]
        elif kind == "lot_metadata_confirmed":
            require(data["lot_id"] in out["lots"], "Unknown lot metadata confirmation")
            require(instant(data["holding_started_at"]) <= instant(event["known_at"])
                    and instant(data["ownership_at"]) <= instant(event["known_at"]), "Lot confirmation cannot claim future ownership")
            require(dt.date.fromisoformat(data["price_date"]).isoformat() == data["price_date"], "Canonical acquisition price date required")
            reference = data["evidence_ref"]
            require(type(reference) is dict and {"sha256", "size"} <= reference.keys()
                    and ("path" in reference or "chunks" in reference), "Lot metadata requires original evidence")
            out["lots"][data["lot_id"]].update(acquired_at=data["holding_started_at"], ownership_at=data["ownership_at"], price_date=data["price_date"])
            out["reconciliation"] = [row for row in out["reconciliation"]
                                      if not (row["kind"] == "holding_metadata_unverified" and row["lot_id"] == data["lot_id"])]
        elif kind == "external_fill_confirmed":
            require(data["side"] in {"buy", "sell"}, "External fill side required")
            reference = data["evidence_ref"]
            require(type(reference) is dict and {"sha256", "size"} <= reference.keys()
                    and ("path" in reference or "chunks" in reference), "External fill requires its original confirmation")
            _text(data["fill_id"], "fill ID"); _text(data["code"], "asset code")
            shares, price, gross, fee, cash = (amount(data[key], key, 0) for key in ("shares", "price", "gross_amount", "fee", "cash_amount"))
            require(shares > 0 and price > 0 and gross > 0, "Confirmed fill consideration must be positive")
            _mark_dates({data["code"]: data["price"]}, {data["code"]: data["price_date"]}, event["known_at"])
            _difference(out, event, "share_rounding_transfer", (shares * price).quantize(Decimal(".01"), rounding=ROUND_HALF_UP), gross)
            if data["side"] == "buy":
                require(data["settlement_at"] is None, "Confirmed external purchase must identify its paid cash, not future receivable")
                require(instant(data["holding_started_at"]) <= instant(event["known_at"])
                        and instant(data["ownership_at"]) <= instant(event["known_at"]), "External ownership cannot be future")
                _difference(out, event, "cash_component_reconciliation", gross + fee, cash)
                out["cash"] = decimal(amount(out["cash"]) - cash)
                _lot(out, data["lot_id"], data["code"], shares, data["holding_started_at"], data["ownership_at"], data["price_date"])
            else:
                require(data["holding_started_at"] is None and data["ownership_at"] is None, "A sale uses its existing confirmed lot dates")
                require(data["lot_id"] in out["lots"], "External sale lacks a reconciled prior lot")
                lot = out["lots"][data["lot_id"]]
                require(lot["code"] == data["code"] and shares <= amount(lot["shares"]), "External sale exceeds confirmed owned shares")
                require(_receipt_follows(data["settlement_at"], event["effective_at"]), "Redemption receipt precedes sale")
                _difference(out, event, "redemption_component_reconciliation", gross - fee, cash)
                lot["shares"] = decimal(amount(lot["shares"]) - shares)
                if amount(lot["shares"]) == 0:
                    del out["lots"][data["lot_id"]]
                require(data["fill_id"] not in out["receivables"], "Duplicate external receivable")
                out["receivables"][data["fill_id"]] = {"kind": "redemption", "amount": decimal(cash), "due_at": data["settlement_at"], "order_id": None}
        elif kind == "account_snapshot_confirmed":
            reference = data["evidence_ref"]
            require(type(reference) is dict and {"sha256", "size"} <= reference.keys()
                    and ("path" in reference or "chunks" in reference), "Account snapshot needs original statement evidence")
            require(type(data["lots"]) is list and all(type(data[key]) is dict for key in ("orders", "receivables", "pending_subscriptions")),
                    "A confirmed account snapshot must include every exposure category")
            out["cash"] = decimal(amount(data["cash"]))
            out["lots"] = {}
            for lot in data["lots"]:
                fields(lot, {"lot_id", "code", "shares", "acquired_at", "ownership_at", "price_date"}, label="confirmed account lot")
                require(amount(lot["shares"]) > 0 and instant(lot["acquired_at"]) <= instant(event["known_at"])
                        and instant(lot["ownership_at"]) <= instant(event["known_at"]), "Confirmed lot dates/shares must already be known")
                _lot(out, lot["lot_id"], lot["code"], amount(lot["shares"]), lot["acquired_at"], lot["ownership_at"], lot["price_date"])
            for identity, row in data["orders"].items():
                fields(row, ORDER_FIELDS | {"remaining_cash", "remaining_shares", "cancel_requested"}, {"redemption_schedule", "fee_contract"}, "confirmed open order")
                require(identity == row["order_id"] and type(row["cancel_requested"]) is bool, "Open order identity/status differs")
                validate_order({key: value for key, value in row.items() if key not in {"remaining_cash", "remaining_shares", "cancel_requested"}})
                require(amount(row["remaining_cash"], "remaining cash", 0) <= amount(row["cash_limit"])
                        and amount(row["remaining_shares"], "remaining shares", 0) <= amount(row["share_limit"]), "Remaining order exposure exceeds its confirmed limit")
            for identity, row in data["receivables"].items():
                _text(identity, "confirmed receivable identity")
                require(type(row) is dict and row.get("kind") in {"redemption", "dividend"}, "Confirmed receivable kind required")
                amount(row.get("amount"), "confirmed receivable", 0); receipt_clock(row.get("due_at"))
            for row in data["pending_subscriptions"].values():
                fields(row, {"order_id", "amount"}, label="confirmed subscription payment")
                require(row["order_id"] in data["orders"] and amount(row["amount"], "subscription payment", 0) > 0,
                        "Pending subscription requires a confirmed order and amount")
            for category in ("orders", "receivables", "pending_subscriptions"):
                out[category] = copy.deepcopy(data[category])
            out["marks"] = _prices(data["prices"])
            out["mark_dates"] = _mark_dates(data["prices"], data["price_dates"], event["known_at"])
            out["performance_pending"].append({"event_id": event["id"], "reason": "historical_return_unreconciled"})
        elif kind == "split":
            ratio = amount(data["ratio"])
            require(ratio > 0 and not out["pending_subscriptions"], "Split has unresolved subscription exposure")
            for lot in out["lots"].values():
                if lot["code"] == data["code"]:
                    lot["shares"] = decimal(amount(lot["shares"])*ratio)
            for order in out["orders"].values():
                if order["code"] == data["code"] and order["side"] == "sell":
                    for name in ("share_limit", "remaining_shares"):
                        order[name] = decimal(amount(order[name])*ratio)
            if data["code"] in out["marks"]:
                out["marks"][data["code"]] = decimal(amount(out["marks"][data["code"]])/ratio)
        elif kind == "unknown":
            _text(data["reason"], "unknown reason")
            require(not any(row.get("id") == event["id"] for row in out["unknown"]), "Duplicate unresolved fact identity")
            out["unknown"].append({"id": event["id"], "reason": data["reason"]})
        elif kind == "resolve_unknown":
            reference = data["evidence_ref"]
            require(type(reference) is dict and {"sha256", "size"} <= reference.keys()
                    and ("path" in reference or "chunks" in reference), "Resolution requires an artifact reference")
            require(type(reference["size"]) is int and reference["size"] >= 0
                    and type(reference["sha256"]) is str and re.fullmatch(r"[a-f0-9]{64}", reference["sha256"]),
                    "Invalid resolution artifact identity")
            _text(data["resolution"], "resolution explanation")
            require(any(row["id"] == data["unknown_id"] for row in out["unknown"]), "Resolution has no matching unknown fact")
            out["unknown"] = [row for row in out["unknown"] if row["id"] != data["unknown_id"]]
        if amount(out["cash"]) < reserved_cash(out):
            require(kind in {"cashflow", "buy_fill", "subscription_confirmed", "sell_fill", "settlement", "dividend_paid", "dividend_reinvested", "external_fill_confirmed", "account_snapshot_confirmed"},
                    "Reservation exceeds settled cash")
            _difference(out, event, "cash_reservation_reconciliation", reserved_cash(out), amount(out["cash"]))
    out.update(sequence=event["sequence"], last_event={"id": event["id"], "hash": digest}, effective_at=event["effective_at"],
               known_at=max(filter(None, (state["known_at"], event["known_at"])), key=instant),
               recorded_at=max(filter(None, (state["recorded_at"], event["recorded_at"])), key=instant))
    return out


def canonical_cashflows(events, known_as_of, initial_transfers=None):
    """Resolve append-only transfer revisions at a knowledge cutoff."""
    cutoff, latest = instant(known_as_of), {}
    for event in sorted(events, key=lambda row: row["sequence"]):
        if event["type"] != "cashflow" or max(instant(event["known_at"]), instant(event["recorded_at"])) > cutoff:
            continue
        data = event["data"]
        prior = latest.get(data["transfer_id"])
        expected = prior["data"]["revision_id"] if prior else (initial_transfers or {}).get(data["transfer_id"])
        require(data["previous_revision"] == expected, "Transfer revisions are incomplete or branched")
        require(prior is not None or expected is None, "Transfer correction predates replay checkpoint")
        latest[data["transfer_id"]] = event
    return list(latest.values())


def rebuild(initial, events, known_as_of, effective_as_of=None):
    """Replay immutable source facts at separate knowledge/economic cutoffs."""
    _state(initial)
    known, effective = instant(known_as_of), instant(effective_as_of or known_as_of)
    require(effective <= known, "Economic cutoff cannot follow knowledge cutoff")
    require(initial["projection_cutoff"] is None, "Rebuild needs a current checkpoint")
    selected, received, identities, financial = [], [], set(), set()
    require(type(events) is list, "Rebuild events must be a list")
    for event in events:
        _event(event)
        fact = financial_identity(event)
        require(fact is None or fact["key"] not in financial, "Duplicate financial fact in source replay")
        if fact:
            financial.add(fact["key"])
    ordered_source = sorted(events, key=lambda event: event["sequence"])
    for index, event in enumerate(ordered_source, initial["sequence"]+1):
        _event(event)
        require(event["sequence"] == index, "Checkpoint source sequence is incomplete")
        require(event["id"] not in identities, "Duplicate source event ID")
        identities.add(event["id"])
        if instant(event["known_at"]) <= known and instant(event["recorded_at"]) <= known:
            received.append(event)
            if event["type"] != "cashflow" and instant(event["effective_at"]) <= effective:
                selected.append(event)
    latest_flows = canonical_cashflows(events, known_as_of, initial["transfers"])
    for event in latest_flows:
        if instant(event["effective_at"]) <= effective:
            selected.append({**event, "data": {**event["data"], "previous_revision": None}})
    selected.sort(key=lambda event: (instant(event["effective_at"]), event["sequence"]))
    state = copy.deepcopy(initial)
    for event in selected:
        projection = {**event, "sequence": state["sequence"]+1}
        state = apply_event(state, projection)
    require([event["sequence"] for event in received] == list(range(initial["sequence"]+1, initial["sequence"]+len(received)+1)),
            "Knowledge cutoff does not contain a complete source prefix")
    if received:
        latest = received[-1]
        state["last_event"] = {"id": latest["id"], "hash": fingerprint(latest)}
        state["sequence"] = initial["sequence"]+len(received)
        state["known_at"] = max([event["known_at"] for event in received]+([initial["known_at"]] if initial["known_at"] else []), key=instant)
        state["recorded_at"] = max([event["recorded_at"] for event in received]+([initial["recorded_at"]] if initial["recorded_at"] else []), key=instant)
    if effective < known:
        state["projection_cutoff"] = {"known_as_of": known_as_of, "effective_as_of": effective_as_of}
    return state


def snapshot(state, as_of, prices=None, price_dates=None):
    _state(state)
    require(all(state[key] is None or instant(state[key]) <= instant(as_of) for key in ("effective_at", "known_at", "recorded_at")),
            "Historical snapshot requires checkpoint replay")
    marks = dict(state["marks"])
    if prices is not None:
        marks.update(_prices(prices))
    dates = dict(state["mark_dates"])
    if price_dates is not None:
        dates.update(_mark_dates(prices or {}, price_dates, as_of))
    reasons = [row["reason"] for row in state["unknown"]]+(["unconfirmed_subscription_shares"] if state["pending_subscriptions"] else [])
    reasons += ["corporate_action_unverified:" + row["code"] for row in state["reconciliation"]
                if row["kind"] == "corporate_action_unverified"]
    reasons += ["holding_metadata_unverified:" + row["lot_id"] for row in state["reconciliation"]
                if row["kind"] == "holding_metadata_unverified"]
    if amount(state["cash"]) < reserved_cash(state):
        reasons.append("cash_or_reservation_requires_reconciliation")
    receivables = []
    for identity, row in sorted(state["receivables"].items()):
        _text(identity, "receivable identity")
        require(row.get("kind") in {"redemption", "dividend"}, "Confirmed receivable kind required")
        amount(row.get("amount"), "confirmed receivable", 0)
        if row.get("due_at") is None:
            reasons.append("receivable_arrival_unknown:"+identity)
        else:
            receipt_clock(row["due_at"])
        receivables.append({"receivable_id": identity, "source_identity": identity,
            **{key: copy.deepcopy(row[key]) for key in ("kind", "amount", "due_at", "code", "source_event_id", "record_at", "entitled_shares") if key in row}})
    positions = []
    with localcontext() as context:
        context.prec = 50
        for identity, lot in sorted(state["lots"].items()):
            if lot["code"] not in marks:
                reasons.append("missing_price:"+lot["code"])
            positions.append({**lot, "reserved_shares": decimal(reserved_shares(state, identity)),
                              "value": decimal(amount(lot["shares"])*amount(marks[lot["code"]])) if lot["code"] in marks else None})
        equity = None if reasons else _equity(state, marks)
        units = amount(state["units"])
        if not state["performance_pending"] and (units <= 0 or equity is not None and equity <= 0):
            reasons.append("no_positive_unitized_equity")
        nav = None if reasons or state["performance_pending"] else equity/units
        pending_buys = [copy.deepcopy(row) for row in state["orders"].values() if row["side"] == "buy" and amount(row["remaining_cash"]) > 0]
        result = {"schema_version": 4, "currency": state["currency"], "as_of": as_of, "state_hash": fingerprint(state),
                  "cash": state["cash"], "reserved_cash": decimal(reserved_cash(state)),
                  "available_cash": decimal(max(Decimal(0), amount(state["cash"])-reserved_cash(state))),
                  "unsettled_cash": decimal(sum((amount(row["amount"]) for row in state["receivables"].values()), Decimal(0))),
                  "receivables": receivables,
                  "positions": positions, "open_orders": copy.deepcopy(list(state["orders"].values())), "prices": marks,
                  "equity": None if equity is None else decimal(equity), "units": state["units"],
                  "unit_nav": None if nav is None else decimal(nav), "blocked": bool(reasons), "reasons": sorted(set(reasons)),
                  "price_dates": dates, "performance_exact": not state["performance_pending"],
                  "performance_pending": copy.deepcopy(state["performance_pending"]), "reconciliation": copy.deepcopy(state["reconciliation"]),
                  "pending_buy_orders": pending_buys, "pending_buy_loss_bound": None,
                  "pending_orders": copy.deepcopy(list(state["orders"].values())),
                  "required_actions": [{"action": "confirm_or_cancel_pending_order", "order_id": row["order_id"], "side": row["side"]}
                                       for row in state["orders"].values()]}
    return {**result, "snapshot_hash": fingerprint(result)}


def observe(state, as_of, prices, previous_unit_nav):
    result = snapshot(state, as_of, prices)
    if result["blocked"] or not result["performance_exact"]:
        return {**result, "net_return": None}
    previous = amount(previous_unit_nav, "previous unit NAV", 0)
    require(previous > 0, "Previous unit NAV must be positive")
    with localcontext() as context:
        context.prec = 50
        return {**result, "net_return": decimal(amount(result["unit_nav"])/previous-1)}
