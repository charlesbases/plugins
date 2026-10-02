"""Independent Decimal reference for source dealing contracts and cash journals.

This module deliberately does not call production fee quotes, calendar arithmetic,
or the MPC simulator. Contract source extraction remains the publication gate's
responsibility; these calculations check its source-bound normalized facts.
"""
import datetime as dt
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo

from contracts import EvidenceError

ZONE = ZoneInfo("Asia/Shanghai")
CENT = Decimal(".01")
ZERO = Decimal(0)


def check(condition, message):
    if not condition:
        raise EvidenceError(message)


def decimal(value):
    result = Decimal(str(value))
    check(result.is_finite(), "Source cash amount is not finite")
    return result


def local_date(value):
    if len(value) == 10:
        return dt.date.fromisoformat(value)
    stamp = dt.datetime.fromisoformat(value)
    check(stamp.tzinfo is not None, "Source cash clock lacks timezone")
    return stamp.astimezone(ZONE).date()


def advance(day, count, basis, calendar):
    day = dt.date.fromisoformat(day) if isinstance(day, str) else day
    check(type(count) is int and count >= 0, "Invalid source clock lag")
    if basis == "calendar_days":
        return day + dt.timedelta(days=count)
    check(basis == "trading_days" and calendar is not None, "Missing source trading calendar")
    check(calendar["coverage_start"] <= str(day) <= calendar["coverage_end"], "Clock outside source calendar")
    opened = calendar["open_dates"]
    check(opened == sorted(set(opened)), "Invalid source open dates")
    if count == 0:
        check(str(day) in opened, "Zero-lag source date is closed")
        return day
    later = [value for value in opened if value > str(day)]
    check(len(later) >= count, "Source receipt exceeds calendar coverage")
    return dt.date.fromisoformat(later[count-1])


def pricing_day(terms, submitted_at):
    stamp = dt.datetime.fromisoformat(submitted_at).astimezone(ZONE)
    day = stamp.date()
    if stamp.time().replace(tzinfo=None) >= dt.time.fromisoformat(terms["order_cutoff_local"]):
        day += dt.timedelta(days=1)
    calendar = terms["execution_calendar"]
    check(calendar["coverage_start"] <= str(day) <= calendar["coverage_end"], "Submission outside source calendar")
    opened = [value for value in calendar["open_dates"] if value >= str(day)]
    check(opened, "Source pricing date is unavailable")
    return dt.date.fromisoformat(opened[0])


def submission_stamp(day, decision_at):
    local = dt.datetime.fromisoformat(decision_at).astimezone(ZONE)
    return dt.datetime.combine(dt.date.fromisoformat(day), local.time().replace(tzinfo=None), ZONE).isoformat()


def confirmation_day(terms, priced):
    rule = terms["confirmation"]
    return advance(priced, rule["lag_days"], rule["day_basis"], rule.get("calendar", terms["execution_calendar"]))


def payment_day(terms, confirmed):
    rule = terms["settlement"]
    return advance(confirmed, rule["lag_days"], rule["day_basis"], rule.get("calendar", terms["execution_calendar"]))


def holding_age(terms, acquired_at, ended):
    rule = terms["holding"]
    check(rule["day_basis"] == "calendar_days", "Unsupported source holding calendar")
    start = local_date(acquired_at)
    check(ended >= start, "Source holding ends before acquisition")
    return max(0, (ended-start).days-1+int(rule["start_inclusive"])+int(rule["end_inclusive"]))


def selected_rule(rule, gross, age=None):
    if rule["kind"] in ("amount_tiers", "holding_tiers"):
        coordinate = gross if rule["kind"] == "amount_tiers" else age
        check(coordinate is not None, "Source fee requires holding age")
        coordinate = decimal(coordinate)
        if "maximum" in rule["bands"][0]:
            applicable = []
            for band in rule["bands"]:
                low, high = decimal(band["minimum"]), band["maximum"]
                lower_ok = coordinate >= low if band["minimum_inclusive"] else coordinate > low
                upper_ok = high is None or (coordinate <= decimal(high) if band["maximum_inclusive"] else coordinate < decimal(high))
                if lower_ok and upper_ok:
                    applicable.append(band["fee"])
            check(len(applicable) == 1, "Source explicit fee intervals do not uniquely cover amount/age")
            return applicable[0]
        applicable = [band["fee"] for band in rule["bands"] if decimal(band["minimum"]) <= coordinate]
        check(applicable, "Source fee bands do not cover the amount/age")
        return applicable[-1]
    check(rule["kind"] in ("percentage", "fixed"), "Unsupported source fee rule")
    return rule


def charge(gross, rule, age=None):
    gross = decimal(gross)
    chosen = selected_rule(rule, gross, age)
    if gross == 0:
        return ZERO
    value = gross*decimal(chosen["rate"]) if chosen["kind"] == "percentage" else decimal(chosen["amount"])
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def exit_details_at_age(gross, terms, age):
    """Independent conditional fee sensitivity without an invented future clock."""
    precision = terms["trade_precision"]
    gross, step = decimal(gross), decimal(precision["money_step"])
    age = decimal(age)
    check(age >= 0 and age == age.to_integral_value(), "Invalid source sensitivity holding age")
    net_mode = precision.get("redemption_net_rounding", "half_up")
    check(net_mode in ("half_up", "down_fund"), "Unsupported source net redemption rounding")
    if net_mode == "half_up":
        gross = gross.quantize(step, rounding=ROUND_HALF_UP)
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        rule = selected_rule(terms["redemption"], gross, age)
        fee = gross*decimal(rule["rate"]) if rule["kind"] == "percentage" else decimal(rule["amount"])
        if gross == ZERO:
            fee = ZERO
        fee_mode = precision["fee_rounding"]
        check(fee_mode in ("half_up", "unrounded"), "Unsupported source redemption fee rounding")
        if fee_mode == "half_up":
            fee = fee.quantize(step, rounding=ROUND_HALF_UP)
        check(ZERO <= fee <= gross, "Source redemption fee exceeds proceeds")
        before_rounding = gross-fee
        net = (before_rounding/step).to_integral_value(rounding=ROUND_DOWN)*step if net_mode == "down_fund" else before_rounding
        return dict(gross=gross, contractual_fee=fee, rounding_loss=before_rounding-net,
                    net=net, effective_cost=gross-net)


def exit_details(gross, terms, *, acquired_at, submitted_at, confirmed=None):
    priced = pricing_day(terms, submitted_at)
    ended = priced
    if terms["holding"]["end_event"] == "confirmation_date":
        ended = confirmed or confirmation_day(terms, priced)
    ages = [holding_age(terms, acquired_at, ended)] if confirmed is not None else [
        holding_age(terms, acquired_at, priced+dt.timedelta(days=offset))
        for offset in range((ended-priced).days+1)]
    check(min(ages) >= terms["holding"]["minimum_days"], "Source lot remains locked")
    return max((exit_details_at_age(gross, terms, age) for age in ages), key=lambda value: value["effective_cost"])


def exit_fee(gross, terms, *, acquired_at, submitted_at, confirmed=None):
    return exit_details(gross, terms, acquired_at=acquired_at, submitted_at=submitted_at, confirmed=confirmed)["effective_cost"]


def entry_quote(request, terms, nav):
    request, nav = decimal(request), decimal(nav)
    step = decimal(terms["trade_precision"]["share_step"])
    check(request >= 0 and nav > 0 and step > 0, "Invalid source subscription request")
    rule = selected_rule(terms["subscription"], request)
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        net = request/(1+decimal(rule["rate"])) if rule["kind"] == "percentage" else request-decimal(rule["amount"])
        mode = terms["trade_precision"]["share_rounding"]
        if mode in ("half_up_fund", "down_fund"):
            fee_mode = terms["trade_precision"]["fee_rounding"]
            check(fee_mode in ("half_up", "unrounded"), "Unsupported source subscription fee rounding")
            fee = request-net
            if fee_mode == "half_up":
                fee = fee.quantize(CENT, rounding=ROUND_HALF_UP)
            gross = request-fee
            shares = (gross/nav/step).to_integral_value(rounding=ROUND_DOWN if mode == "down_fund" else ROUND_HALF_UP)*step
            debit = request
        else:
            check(mode == "down_refund", "Unsupported source subscription rounding")
            net = (max(ZERO, net)/CENT).to_integral_value(rounding=ROUND_DOWN)*CENT
            shares = (net/nav/step).to_integral_value(rounding=ROUND_DOWN)*step
            gross = (shares*nav).quantize(CENT, rounding=ROUND_HALF_UP)
            fee = charge(gross, rule)
            debit = gross+fee
        check(ZERO <= fee <= request and gross >= ZERO and shares >= ZERO and debit <= request, "Source subscription debit is infeasible")
        result = dict(shares=shares, gross=gross, fee=fee, debit=debit, refund=request-debit)
        if mode == "down_fund":
            result["share_surplus"] = gross-shares*nav
        return result


def conditional_nodes(context, condition, horizon):
    """Independently reconstruct the source clocks; no producer calculations."""
    original = {row["lot_id"]: row for row in context["snapshot"]["positions"]}
    coordinates = [(original[row["lot_id"]]["code"], original[row["lot_id"]]["acquired_at"],
                    decimal(row["shares"])*decimal(context["snapshot"]["prices"][original[row["lot_id"]]["code"]]))
                   for row in condition["source_action"]["sells"]]
    bought = condition["current_buy"]
    joint_dates = set.intersection(*(set(context["fee_contracts"][code]["execution_calendar"]["open_dates"])
                                    for code in context["allocation_codes"]))
    initial_ready = local_date(context["as_of"])
    initial = condition["initial_action"]
    for code in [original[row["lot_id"]]["code"] for row in initial["sells"]]+[row["code"] for row in initial["buys"]]:
        terms = context["fee_contracts"][code]
        initial_ready = max(initial_ready, confirmation_day(terms, pricing_day(terms, context["decision_at"])))
    buy_known = None
    if bought is not None:
        terms = context["fee_contracts"][bought["code"]]
        priced = pricing_day(terms, context["decision_at"])
        confirmed_buy = confirmation_day(terms, priced)
        lag = max(1, context["spec"].get("availability", {}).get("nav_lag_calendar_days", 1))
        buy_known = priced+dt.timedelta(days=lag)
        acquired = priced if terms["acquisition_rule"]["holding_start"] == "execution_date" else confirmed_buy
        coordinates.append((bought["code"], str(acquired)+"T00:00:00+08:00", decimal(bought["cash_debit"])))
    nodes, previous, latest = [], None, None
    planning = context["spec"]["planning"]
    deadline = (str(local_date(context["as_of"])+dt.timedelta(days=planning["cash_deadline_days"]))
                if planning.get("cash_deadline_days") is not None else None)
    day = local_date(context["as_of"])+dt.timedelta(days=1)
    while str(day) < horizon:
        if day <= initial_ready or buy_known is not None and day < buy_known:
            day += dt.timedelta(days=1)
            continue
        costs, receipts, legal = [], [], True
        for code, acquired, gross in coordinates:
            terms = context["fee_contracts"][code]
            submitted = submission_stamp(str(day), context["decision_at"])
            if (str(day) not in terms["execution_calendar"]["open_dates"]
                    or holding_age(terms, acquired, day) < terms["holding"]["minimum_days"]):
                legal = False
                break
            try:
                priced = pricing_day(terms, submitted)
                if str(priced) >= horizon or str(priced) not in joint_dates:
                    legal = False
                    break
                confirmed = confirmation_day(terms, priced)
                costs.append(exit_details(gross, terms, acquired_at=acquired, submitted_at=submitted, confirmed=confirmed)["effective_cost"])
                receipts.append(str(payment_day(terms, confirmed)))
            except ValueError:
                legal = False
                break
        if legal and coordinates:
            cost = sum(costs, ZERO)
            if previous is None or cost < previous:
                nodes.append(str(day))
            previous = cost
            if deadline and all(date <= deadline for date in receipts):
                latest = str(day)
        day += dt.timedelta(days=1)
    return sorted(set(nodes+([latest] if latest else [])))


def _conditional_sale_legal(context, lots, sells, submitted):
    """Independent source precision, locks, minima, fee scope and lot allocation."""
    if not sells:
        return False
    code = lots[sells[0]["lot_id"]]["code"]
    terms = context["fee_contracts"][code]
    assets = {row["code"]: row for row in context["model_request"]["assets"]}
    if not assets[code]["sellable"] or not terms["sellable"]:
        return False
    quantities = {}
    for row in sells:
        lot, quantity = lots[row["lot_id"]], decimal(row["shares"])
        if (lot["code"] != code or quantity <= 0 or quantity % decimal(terms["trade_precision"]["share_step"])
                or quantity > lot["shares"]-decimal(lot["reserved_shares"])
                or holding_age(terms, lot["acquired_at"], local_date(submitted)) < terms["holding"]["minimum_days"]):
            return False
        quantities[row["lot_id"]] = quantity
    if len(quantities) > 1 and not (terms.get("redemption_fee_application") == "per_lot"
            and terms.get("redemption_fee_application_source") and terms.get("redemption_rounding_application") == "per_lot"
            and terms.get("redemption_rounding_application_source")):
        return False
    product = [row for row in lots.values() if row["code"] == code]
    held = sum((row["shares"] for row in product), ZERO)
    available = sum((row["shares"]-decimal(row["reserved_shares"]) for row in product), ZERO)
    quantity = sum(quantities.values(), ZERO)
    full = quantity == held
    evidence = terms.get("action_evidence")
    if evidence is not None:
        action = evidence.get("sell_full" if full else "sell_partial", {})
        if action.get("status") != "source_supported" or action.get("missing_fields") != []:
            return False
    minimum, residual = terms.get("minimum_redemption_shares"), terms.get("minimum_remaining_shares")
    if minimum is None or residual is None:
        if not full or terms.get("full_redemption_allowed") is not True:
            return False
    elif ((quantity < decimal(minimum) and not (full and terms.get("full_redemption_allowed") is True))
          or held-quantity != 0 and held-quantity < decimal(residual)):
        return False
    method = terms.get("redemption_allocation", {}).get("method")
    nonempty = [row for row in product if row["shares"]-decimal(row["reserved_shares"]) > 0]
    if len(nonempty) > 1 and quantity != available:
        if method not in {"specific_lot", "fifo", "lifo"}:
            return False
        if method in {"fifo", "lifo"}:
            ordered = sorted(nonempty, key=lambda row: (dt.datetime.fromisoformat(row["acquired_at"]), row["lot_id"]), reverse=method == "lifo")
            remaining, expected = quantity, {}
            for lot in ordered:
                used = min(remaining, lot["shares"]-decimal(lot["reserved_shares"]))
                if used:
                    expected[lot["lot_id"]] = used
                remaining -= used
            if expected != quantities:
                return False
    return True


def conditional_sales(context, policy, path, end_day):
    """Rebuild first observable trigger or continued holding, including initial buys."""
    rule = policy.get("future_rule", {})
    check(type(rule) is dict and set(rule) <= {"kind", "weights", "conditional_sells"}, "Unsupported future policy fields")
    conditions = policy.get("future_rule", {}).get("conditional_sells", [])
    check(len(conditions) <= 1, "Source conditional sale family changed")
    if not conditions:
        return []
    condition = conditions[0]
    check(condition["comparison"] in {"ge", "le"} and not condition["source_action"]["buys"], "Invalid frozen source condition")
    lots = {row["lot_id"]: {**row, "shares": decimal(row["shares"])} for row in context["snapshot"]["positions"]}
    action = policy["current_action"]
    check(condition["initial_action"] == action, "Conditional initial source action changed")
    for sale in action["sells"]:
        lots[sale["lot_id"]]["shares"] -= decimal(sale["shares"])
    sells = condition["source_action"]["sells"]
    check(all(row["lot_id"] in lots and ZERO < decimal(row["shares"]) <= lots[row["lot_id"]]["shares"]-decimal(lots[row["lot_id"]]["reserved_shares"])
              for row in sells), "Conditional target differs from initial remaining shares")
    reference = sum((decimal(row["shares"])*decimal(context["snapshot"]["prices"][lots[row["lot_id"]]["code"]]) for row in sells), ZERO)
    bought, confirmed_buy, priced_buy = condition["current_buy"], None, None
    if bought is not None:
        choices = [(index, row) for index, row in enumerate(action["buys"]) if row["code"] == bought["code"]]
        check(len(choices) == 1, "Conditional acquisition is not an initial source buy")
        index, source_buy = choices[0]
        expected_id = "model:"+str(len(action["sells"])+index+1)
        check(bought == {**source_buy, "lot_id": expected_id}, "Conditional initial purchase binding changed")
        check(expected_id not in lots, "Conditional modeled acquisition identity collides with source holdings")
        reference += decimal(source_buy["cash_debit"])
        terms = context["fee_contracts"][bought["code"]]
        priced_buy = pricing_day(terms, context["decision_at"])
        confirmed_buy = confirmation_day(terms, priced_buy)
        check(str(priced_buy) in path["nav"] and bought["code"] in path["nav"][str(priced_buy)], "Initial conditional buy has no exact dealing NAV")
        quote = entry_quote(source_buy["cash_debit"], terms, path["nav"][str(priced_buy)][bought["code"]])
        acquired = priced_buy if terms["acquisition_rule"]["holding_start"] == "execution_date" else confirmed_buy
        lots[expected_id] = {"lot_id": expected_id, "code": bought["code"], "shares": quote["shares"],
                            "reserved_shares": "0", "acquired_at": str(acquired)+"T00:00:00+08:00"}
        sells = [*sells, {"lot_id": expected_id, "shares": str(quote["shares"])}]
    check(reference > 0 and decimal(condition["reference_value"]) == reference, "Conditional zero-gain reference value changed")
    local = dt.datetime.fromisoformat(context["decision_at"]).astimezone(ZONE)
    check(condition["submission_time"] == local.time().replace(tzinfo=None).isoformat(), "Conditional source submission clock changed")
    expected_nodes = conditional_nodes(context, condition, policy["local_decision_dates"][-1])
    check(condition["decision_dates"] == expected_nodes
          and all(day in policy["local_decision_dates"] for day in expected_nodes), "Conditional source decision dates changed")
    lag = max(1, context["spec"].get("availability", {}).get("nav_lag_calendar_days", 1))
    for day in expected_nodes:
        if day > end_day:
            break
        observed = local_date(day)-dt.timedelta(days=lag)
        if confirmed_buy is not None and (str(confirmed_buy) >= day or priced_buy > observed):
            continue
        submitted = submission_stamp(day, context["decision_at"])
        if not _conditional_sale_legal(context, lots, sells, submitted):
            continue
        net = ZERO
        try:
            for sale in sells:
                lot = lots[sale["lot_id"]]
                code, terms = lot["code"], context["fee_contracts"][lot["code"]]
                dates = [date for date, row in path["nav"].items() if date <= str(observed) and code in row]
                mark = path["nav"][max(dates)][code] if dates else context["snapshot"]["prices"][code]
                confirmed = confirmation_day(terms, pricing_day(terms, submitted))
                net += exit_details(decimal(sale["shares"])*decimal(mark), terms, acquired_at=lot["acquired_at"],
                                    submitted_at=submitted, confirmed=confirmed)["net"]
        except ValueError:
            continue
        if net >= reference if condition["comparison"] == "ge" else net <= reference:
            return [{"lot_id": row["lot_id"], "shares": str(decimal(row["shares"])), "submitted_at": submitted}
                    for row in sells if str(pricing_day(context["fee_contracts"][lots[row["lot_id"]]["code"]], submitted)) <= end_day]
    return []


def reconcile(context, policy, path, journal, end_day, balances, *, prefix="Source", entitlement_cutoff=None):
    """Reconstruct every cash transition and require every matured receipt once."""
    cash = decimal(context["snapshot"]["available_cash"])+decimal(policy["proposed_contribution"])
    if context.get("price_schema") == "source_role_price_paths_v3":
        known = {code: context["known_marks"][code] for code in context["allocation_codes"]}
        check(path.get("price_schema") == context["price_schema"] and path.get("known_marks") == known,
              "Source role-price path/known valuation binding changed")
    expected_conditional = conditional_sales(context, policy, path, end_day)
    decision_local = dt.datetime.fromisoformat(context["decision_at"]).astimezone(ZONE)
    decision_time = decision_local.time().replace(tzinfo=None).isoformat()
    reserved = decimal(context["snapshot"]["reserved_cash"])
    frozen = decimal(context["snapshot"]["unsettled_cash"])
    lots = {row["lot_id"]: {**row, "shares": decimal(row["shares"])} for row in context["snapshot"]["positions"]}
    segments = [{"code": row["code"], "shares": row["shares"], "owned": str(local_date(row.get("ownership_at", row["acquired_at"]))),
                 "sold": None, "lot_id": row["lot_id"]} for row in lots.values()]
    receipts, pending_buys, posted = [], [], set()
    initial_buys, source_sales, purchased, credit_clocks = [], [], [], []
    fees = ZERO
    known = context["snapshot"].get("receivables")
    if known is not None:
        check(sum((decimal(row["amount"]) for row in known), ZERO) == frozen, "Source existing receivable inventory differs from unsettled cash")
        identities = [row["receivable_id"] for row in known]
        check(len(identities) == len(set(identities)), "Duplicate source existing receivable identity")
        frozen = ZERO
        for row in known:
            due = str(local_date(row["due_at"]))
            if row["kind"] == "dividend":
                posted.add(row["receivable_id"])
                for event in path["distributions"]:
                    if (event["record_date"] <= context["as_of"] and event["id"] != row["source_identity"]
                            and (row.get("code") is None or row["code"] == event["code"])):
                        check(False, "Source existing dividend right identity requires explicit binding")
            receipts.append({"kind": "existing_receivable", "code": row.get("code"), "date": max(context["as_of"], due),
                             "amount": decimal(row["amount"]), "source": row})
    def due(kind, code, day, amount):
        if amount or kind == "distribution":
            receipts.append(dict(kind=kind, code=code, date=str(day), amount=amount))
    previous = context["as_of"]
    for row in journal:
        day, kind, code = row["date"], row["kind"], row.get("code")
        check(previous <= day <= end_day, prefix+" cash journal is outside its causal date order")
        previous = day
        terms = context["fee_contracts"].get(code)
        expected_delta, expected_reserved = ZERO, ZERO
        if kind == "reserve_buy":
            request = decimal(row["request"])
            check(request > 0, "Source purchase reservation must be positive")
            expected_delta, expected_reserved = -request, request
            submitted = row.get("submitted_at", submission_stamp(day, context["decision_at"]))
            future = row.get("hypothetical_future", False)
            allowed_times = [decision_time]+(["23:59:59.999999"] if future else [])
            submitted_local = dt.datetime.fromisoformat(submitted).astimezone(ZONE)
            check(str(submitted_local.date()) == day and submitted_local.time().replace(tzinfo=None).isoformat() in allowed_times, "Source purchase submission clock changed")
            check(all(dt.datetime.fromisoformat(submitted) >= clock for clock in credit_clocks), "Source purchase spends receipt before credited instant")
            if not future:
                check(day == context["as_of"] and submitted_local == decision_local, "Source initial purchase submission changed")
                initial_buys.append(dict(code=code, cash_debit=str(request)))
            pending_buys.append(dict(code=code, request=request, day=day, future=future, submitted=submitted))
        elif kind in ("price_buy", "price_sell"):
            check(day in path["nav"] and code in path["nav"][day], "Source execution NAV is absent")
            nav = decimal(path["nav"][day][code])
            check(nav > 0 and decimal(row["nav"]) == nav, "Source execution NAV changed")
            confirmed = confirmation_day(terms, day)
            if "confirmation_date" in row:
                check(row["confirmation_date"] == str(confirmed), "Source execution confirmation date changed")
            if kind == "price_buy":
                request = decimal(row["request"])
                reservation = next((item for item in pending_buys if item["code"] == code and item["request"] == request), None)
                check(reservation is not None, "Source purchase lacks a cash reservation")
                check(str(pricing_day(terms, reservation["submitted"])) == day, "Source purchase pricing clock changed")
                if "submitted_at" in row:
                    check(row["submitted_at"] == reservation["submitted"], "Source purchase pricing submission changed")
                pending_buys.remove(reservation)
                quote = entry_quote(request, terms, nav)
                for key in ("shares", "gross", "fee", "refund"):
                    check(decimal(row[key]) == quote[key], "Source purchase "+key+" changed")
                if "share_surplus" in row:
                    check(decimal(row["share_surplus"]) == quote.get("share_surplus", ZERO), "Source purchase fund share surplus changed")
                fees += quote["fee"]
                due("refund", code, confirmed, quote["refund"])
                expected_reserved = -request
                owned = day if terms["acquisition_rule"]["ownership_start"] == "execution_date" else str(confirmed)
                holding = day if terms["acquisition_rule"]["holding_start"] == "execution_date" else str(confirmed)
                holding_lot = dict(code=code, shares=quote["shares"], reserved_shares="0",
                                   acquired_at=holding+"T00:00:00+08:00", ownership_at=owned+"T00:00:00+08:00")
                if not reservation["future"]:
                    index = next(i for i, item in enumerate(policy["current_action"]["buys"]) if item["code"] == code)
                    lot_id = "model:"+str(len(policy["current_action"]["sells"])+index+1)
                    check(lot_id not in lots, "Source modeled acquisition identity collides with source holdings")
                    lots[lot_id] = {**holding_lot, "lot_id": lot_id}
                else:
                    lot_id = None
                    purchased.append(holding_lot)
                segments.append(dict(code=code, shares=quote["shares"], owned=owned, sold=None, lot_id=lot_id))
            else:
                check(row["lot_id"] in lots, "Source sale lot is absent")
                lot = lots[row["lot_id"]]
                quantity = decimal(row["shares"])
                check(0 < quantity <= lot["shares"]-decimal(lot["reserved_shares"]), "Source sale oversells the lot")
                submitted = row.get("submitted_at", context["decision_at"])
                submitted_local = dt.datetime.fromisoformat(submitted).astimezone(ZONE)
                if submitted_local != decision_local:
                    check(any(item["submitted_at"] == submitted_local.isoformat() and item["lot_id"] == row["lot_id"]
                              and decimal(item["shares"]) == quantity for item in expected_conditional),
                          "Source sale differs from first satisfied frozen observable condition")
                check(str(pricing_day(terms, submitted)) == day, "Source sale pricing clock changed")
                details = exit_details(quantity*nav, terms, acquired_at=lot["acquired_at"], submitted_at=submitted, confirmed=confirmed)
                gross, fee, net = details["gross"], details["effective_cost"], details["net"]
                check(decimal(row["gross"]) == gross and decimal(row["fee"]) == fee and decimal(row["receivable"]) == net,
                      "Source sale fee/net proceeds changed")
                extra_fields = {"contractual_fee": "contractual_fee", "rounding_loss": "rounding_loss"}
                if terms["trade_precision"].get("redemption_net_rounding") == "down_fund" or any(key in row for key in extra_fields):
                    for key, expected in extra_fields.items():
                        check(key in row and decimal(row[key]) == details[expected], "Source sale "+key+" changed")
                    check(row.get("fee_scope") == "source_estimated_execution_cost", "Source sale fee scope changed")
                check(row["pay_date"] == str(payment_day(terms, confirmed)), "Source sale receipt date changed")
                check(row["cash_available_now"] is False, "Source sale prematurely available")
                due("sale", code, row["pay_date"], net)
                source_sales.append(dict(lot_id=row["lot_id"], shares=str(quantity), submitted_at=submitted_local.isoformat()))
                fees += fee
                lot["shares"] -= quantity
                segment = next(item for item in segments if item["lot_id"] == row["lot_id"] and item["sold"] is None)
                segment["shares"] -= quantity
                segments.append({**segment, "shares": quantity, "sold": day})
        elif kind == "distribution_receivable":
            event = next((item for item in path["distributions"] if item["id"] == row["event_id"]), None)
            check(event is not None and row["event_id"] not in posted, "Unknown or duplicate source distribution")
            check(code == event["code"] and day == max(context["as_of"], event["ex_date"]) and row["pay_date"] == event["pay_date"], "Source distribution receipt clock changed")
            from verify import _source_right
            shares = sum((item["shares"] for item in segments if item["code"] == code and _source_right(event, item["owned"], item["sold"])), ZERO)
            amount = (shares*decimal(event["per_share"])).quantize(CENT, rounding=ROUND_HALF_UP)
            check(decimal(row["amount"]) == amount, "Source distribution entitlement changed")
            due("distribution", code, event["pay_date"], amount)
            posted.add(row["event_id"])
        elif kind.startswith("credit_"):
            receipt_kind = kind[len("credit_"):]
            amount = decimal(row["amount"])
            candidates = [item for item in receipts if item["kind"] == receipt_kind and item["code"] == code and item["date"] == day and item["amount"] == amount]
            if receipt_kind == "existing_receivable":
                candidates = [item for item in candidates if item["source"]["receivable_id"] == row.get("receivable_id")]
                if candidates:
                    source = candidates[0]["source"]
                    check(row.get("source_identity") == source["source_identity"] and row.get("due_at") == source["due_at"]
                          and row.get("receivable_kind") == source["kind"], "Source existing receipt identity/clock changed")
                    if source.get("source_event_id") is not None:
                        check(row.get("source_event_id") == source["source_event_id"], "Source existing receipt event changed")
                    credited = dt.datetime.fromisoformat(row["credited_at"])
                    credit_clocks.append(credited)
                    check(credited.tzinfo is not None and str(credited.astimezone(ZONE).date()) == day, "Source existing credit timestamp changed")
                    if len(source["due_at"]) > 10:
                        check(credited >= dt.datetime.fromisoformat(source["due_at"]) and row["clock_precision"] == "instant", "Source existing receipt credited before due instant")
                    else:
                        check(credited.astimezone(ZONE).time().replace(tzinfo=None) == dt.time(23, 59, 59, 999999)
                              and row["clock_precision"] == "date", "Source date-only receipt must wait through due date")
            check(candidates, "Source cash credited prematurely, twice, or without receivable")
            receipts.remove(candidates[0])
            expected_delta = amount
        else:
            check(False, "Unknown source cash journal event")
        moves_cash = kind == "reserve_buy" or kind == "price_buy" or kind.startswith("credit_")
        check(("available_delta" in row) == moves_cash, "Source cash journal transition fields missing or unexpected")
        if moves_cash:
            check(decimal(row["available_before"]) == cash, "Source cash journal diverges from source balance")
            check(decimal(row["available_delta"]) == expected_delta and decimal(row["reserved_delta"]) == expected_reserved, "Source available/reserved cash movement changed")
            cash += expected_delta
            reserved += expected_reserved
            check(cash >= 0 and reserved >= 0 and decimal(row["available_after"]) == cash and decimal(row["reserved_after"]) == reserved,
                  "Source available/reserved cash does not reconcile")
    check(all(item["date"] > end_day for item in receipts), "Source cash journal omits a mature receipt")
    for reservation in pending_buys:
        check(str(pricing_day(context["fee_contracts"][reservation["code"]], reservation["submitted"])) > end_day,
              "Source cash journal omits a priced purchase")
    action = policy.get("current_action")
    if action is not None:
        check(initial_buys == [{"code": row["code"], "cash_debit": str(decimal(row["cash_debit"]))} for row in action["buys"]],
              "Source journal initial purchase differs from frozen action")
        expected_sales = [{"lot_id": row["lot_id"], "shares": str(decimal(row["shares"])), "submitted_at": decision_local.isoformat()} for row in action["sells"]
                          if str(pricing_day(context["fee_contracts"][lots[row["lot_id"]]["code"]], context["decision_at"])) <= end_day]
        expected_sales.extend(expected_conditional)
        sale_key = lambda row: (row["submitted_at"], row["lot_id"], row["shares"])
        check(sorted(source_sales, key=sale_key) == sorted(expected_sales, key=sale_key), "Source journal sale differs from frozen action")
    # Missing zero or positive entitlements also invalidate source-path coverage.
    for event in path["distributions"]:
        if event["record_date"] <= (entitlement_cutoff or end_day) and event["ex_date"] <= end_day:
            check(event["id"] in posted, "Source cash journal omits a distribution right")
    check(decimal(balances["cash"]) == cash and decimal(balances["reserved_cash"]) == reserved,
          prefix+" snapshot includes unreceived or occupied cash")
    check(decimal(balances["receivables"]) == frozen+sum((item["amount"] for item in receipts), ZERO), "Source receivable inventory differs from cash flows")
    return dict(cash=cash, reserved_cash=reserved, fees=fees,
                holdings=[*lots.values(), *purchased],
                outstanding_existing=[item["source"] for item in receipts if item["kind"] == "existing_receivable"])
