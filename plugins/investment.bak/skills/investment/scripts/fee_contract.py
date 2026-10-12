"""Re-extracted fee rules and exact investor cash/share arithmetic.

Only supported unambiguous source expressions become verified rules. An input
number is an assertion to compare with extraction, never fee evidence itself.
"""
import copy
import datetime as dt
import re
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, localcontext

from contracts import fields, fingerprint, instant, require, EvidenceError
import trading_calendar


class EvidenceGap(ValueError):
    code = "needs_research"

    def __init__(self, message, required_actions):
        super().__init__(message)
        self.required_actions = required_actions


def number(value, label="fee value"):
    require(not isinstance(value, bool), "Invalid " + label)
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, "Invalid " + label)
    return result


def floor(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def validate_rule(rule):
    require(type(rule) is dict and rule.get("kind") in ("percentage", "fixed", "amount_tiers", "holding_tiers"), "Unsupported fee rule")
    kind = rule["kind"]
    if kind == "percentage":
        fields(rule, {"kind", "rate"}, label="percentage fee")
        require(number(rule["rate"]) < 1, "Fee rate must be below one")
    elif kind == "fixed":
        fields(rule, {"kind", "amount"}, label="fixed fee")
        number(rule["amount"])
    else:
        fields(rule, {"kind", "bands"}, label="fee bands")
        require(type(rule["bands"]) is list and rule["bands"], "Fee bands required")
        previous = None
        previous_band = None
        explicit = "maximum" in rule["bands"][0]
        for band in rule["bands"]:
            fields(band, {"minimum", "fee"}, {"minimum_inclusive", "maximum", "maximum_inclusive"}, label="fee band")
            start = number(band["minimum"])
            require((previous is None and start == 0) or (previous is not None and start > previous), "Ordered fee bands must start at zero")
            require(("maximum" in band) == explicit, "Mixed implicit and explicit fee interval formats")
            if explicit:
                require(type(band.get("minimum_inclusive")) is bool and type(band.get("maximum_inclusive")) is bool, "Fee interval inclusions required")
                end = None if band["maximum"] is None else number(band["maximum"])
                require(end is None or end > start, "Invalid fee interval")
                if previous_band is None:
                    require(band["minimum_inclusive"], "Fee interval excludes zero")
                else:
                    previous_end = previous_band["maximum"]
                    require(previous_end is not None, "Open fee interval cannot precede another band")
                    previous_end = number(previous_end)
                    continuous = start == previous_end and band["minimum_inclusive"] != previous_band["maximum_inclusive"]
                    discrete = (kind == "holding_tiers" and start == previous_end + 1
                                and band["minimum_inclusive"] and previous_band["maximum_inclusive"])
                    require(continuous or discrete, "Fee intervals have a gap or overlap")
            require(band["fee"].get("kind") in ("percentage", "fixed"), "Nested fee bands are not supported")
            validate_rule(band["fee"])
            previous, previous_band = start, band
        if explicit:
            require(previous_band["maximum"] is None, "Fee schedule has no final open interval")
    return rule


def _select(rule, amount, age=None):
    validate_rule(rule)
    if rule["kind"] not in ("amount_tiers", "holding_tiers"):
        return rule
    coordinate = amount if rule["kind"] == "amount_tiers" else age
    require(coordinate is not None, "Holding age required for redemption quote")
    coordinate = number(coordinate, "fee coordinate")
    if "maximum" not in rule["bands"][0]:
        return [band["fee"] for band in rule["bands"] if number(band["minimum"]) <= coordinate][-1]
    matched = []
    for band in rule["bands"]:
        lower, upper = number(band["minimum"]), band["maximum"]
        above = coordinate > lower or coordinate == lower and band["minimum_inclusive"]
        below = upper is None or coordinate < number(upper) or coordinate == number(upper) and band["maximum_inclusive"]
        if above and below:
            matched.append(band["fee"])
    require(len(matched) == 1, "Fee coordinate has no unique source interval")
    return matched[0]


def source_holding_ages(terms):
    """Finite cost diagnostics from source fee/lock boundaries, never exit dates."""
    minimum = terms["holding"]["minimum_days"]
    require(type(minimum) is int and minimum >= 0, "Source minimum holding age must be a nonnegative integer")
    rule = validate_rule(terms["redemption"])
    ages = {minimum}
    if rule["kind"] == "holding_tiers":
        boundaries = {Decimal(minimum)}
        for band in rule["bands"]:
            boundaries.add(number(band["minimum"]))
            if band.get("maximum") is not None:
                boundaries.add(number(band["maximum"]))
        for boundary in boundaries:
            below = int(boundary.to_integral_value(rounding=ROUND_FLOOR))
            above = int(boundary.to_integral_value(rounding=ROUND_CEILING))
            # An integer endpoint can belong to either adjoining fee interval.
            ages.update((below - 1, below, below + 1) if below == above else (below, above))
    return sorted(age for age in ages if age >= 0)


def charge(gross, rule, *, age=None):
    gross = number(gross, "gross consideration")
    selected = _select(rule, gross, age)
    if gross == 0:
        return Decimal(0)
    fee = gross * number(selected["rate"]) if selected["kind"] == "percentage" else number(selected["amount"])
    return fee.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def quote_entry(cash_debit, terms, *, price, share_step):
    """Maximal affordable whole share steps with exact fee rounding."""
    cash, price, step = number(cash_debit), number(price), number(share_step)
    require(price > 0 and step > 0, "Positive NAV and share precision required")
    rule = terms["subscription"]
    require(rule["kind"] != "holding_tiers", "Entry fee cannot use unknown holding age")
    validate_rule(rule)
    with localcontext() as context:
        context.prec = 50
        # Amount tiers use the inclusive subscription cash debit. Inside one
        # fee band, rounded debit is monotone; integer bisection is exact.
        selected = _select(rule, cash)
        mode = terms["trade_precision"]["share_rounding"]
        require(mode in ("half_up_fund", "down_fund", "down_refund"), "Explicit source-supported share rounding required")
        if mode in ("half_up_fund", "down_fund"):
            raw_fee = cash - cash / (1 + number(selected["rate"])) if selected["kind"] == "percentage" else number(selected["amount"])
            fee_mode = terms["trade_precision"]["fee_rounding"]
            require(fee_mode in ("half_up", "unrounded"), "Source subscription fee rounding is unsupported")
            fee = raw_fee if fee_mode == "unrounded" else raw_fee.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            require(fee <= cash, "Subscription fee exceeds cash")
            gross = cash - fee
            shares = (gross / price / step).to_integral_value(rounding=ROUND_DOWN if mode == "down_fund" else ROUND_HALF_UP) * step
            result = {"shares": shares, "gross": gross, "fee": fee, "debit": cash, "remainder": Decimal(0)}
            if mode == "down_fund":
                result["share_surplus"] = gross - shares * price
            return result
        net_limit = cash / (1 + number(selected["rate"])) if selected["kind"] == "percentage" else cash-number(selected["amount"])
        if net_limit <= 0:
            return {"shares": Decimal(0), "gross": Decimal(0), "fee": Decimal(0), "debit": Decimal(0), "remainder": cash}
        net_limit = floor(net_limit, Decimal(".01"))
        # Never optimize through half-cent money rounding to obtain free share
        # fractions. The source convention first fixes the net cash budget.
        shares = floor(net_limit / price, step)
        gross = (shares * price).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        fee = charge(gross, selected) if gross else Decimal(0)
        require(gross+fee <= cash, "Rounded subscription exceeds its cash budget")
        return {"shares": shares, "gross": gross, "fee": fee, "debit": gross + fee, "remainder": cash - gross - fee}


def pricing_date(terms, submitted_at):
    from zoneinfo import ZoneInfo
    local = instant(submitted_at).astimezone(ZoneInfo("Asia/Shanghai"))
    calendar = terms["execution_calendar"]
    cutoff = dt.time.fromisoformat(terms["order_cutoff_local"])
    day = local.date() + (dt.timedelta(days=1) if local.time().replace(tzinfo=None) >= cutoff else dt.timedelta(0))
    return dt.date.fromisoformat(trading_calendar.on_or_after(day, calendar))


def normal_confirmation_end(terms, priced_day):
    rule = terms["confirmation"]
    return trading_calendar.advance(priced_day, rule["lag_days"], rule["day_basis"], rule.get("calendar", terms.get("execution_calendar")))


def holding_start_bounds(terms, submitted_at):
    from zoneinfo import ZoneInfo
    if "confirmation" in terms:
        earliest = pricing_date(terms, submitted_at)
        latest = normal_confirmation_end(terms, earliest)
        def stamp(day):
            return dt.datetime.combine(day, dt.time(), ZoneInfo("Asia/Shanghai")).isoformat()
        return {"earliest": stamp(earliest), "latest": stamp(latest), "scope": "normal_source_defined_registration_window_not_unconditional_maximum"}
    latest = instant(submitted_at)+dt.timedelta(days=terms["confirmation_max_calendar_days"])
    return {"earliest": submitted_at, "latest": latest.isoformat(), "scope": "explicit_source_calendar_bound"}


def confirmation_window_days(terms, submitted_at):
    from zoneinfo import ZoneInfo
    bounds = holding_start_bounds(terms, submitted_at)
    start = instant(submitted_at).astimezone(ZoneInfo("Asia/Shanghai")).date()
    latest = instant(bounds["latest"]).astimezone(ZoneInfo("Asia/Shanghai")).date()
    return max(1, (latest-start).days)


def holding_ages_at_exit(terms, started_bounds, submitted_at):
    from zoneinfo import ZoneInfo
    if "confirmation" in terms:
        first = pricing_date(terms, submitted_at)
        last = normal_confirmation_end(terms, first) if terms["holding"]["end_event"] == "confirmation_date" else first
        stamp = lambda day: dt.datetime.combine(day, dt.time(), ZoneInfo("Asia/Shanghai")).isoformat()
        earliest_end, latest_end = stamp(first), stamp(last)
    else:
        earliest_end = latest_end = submitted_at
    youngest = trading_calendar.holding_days(started_bounds["latest"], earliest_end, terms["holding"])
    oldest = trading_calendar.holding_days(started_bounds["earliest"], latest_end, terms["holding"])
    return list(range(youngest, oldest+1))


def _exit_at_age(gross, terms, age):
    precision = terms["trade_precision"]
    net_mode = precision.get("redemption_net_rounding", "half_up")
    require(net_mode in ("half_up", "down_fund"), "Unsupported redemption net rounding")
    gross = number(gross)
    if net_mode == "half_up":
        gross = gross.quantize(Decimal(precision["money_step"]), rounding=ROUND_HALF_UP)
    selected = _select(terms["redemption"], gross, Decimal(age))
    fee = (gross * number(selected["rate"]) if selected["kind"] == "percentage" else number(selected["amount"])) if gross else Decimal(0)
    require(precision["fee_rounding"] in ("half_up", "unrounded"), "Unsupported redemption fee rounding")
    if precision["fee_rounding"] == "half_up":
        fee = fee.quantize(Decimal(precision["money_step"]), rounding=ROUND_HALF_UP)
    require(fee <= gross, "Redemption fee exceeds proceeds")
    raw_net = gross - fee
    net = floor(raw_net, Decimal(precision["money_step"])) if net_mode == "down_fund" else raw_net
    return {"gross": gross, "contractual_fee": fee, "rounding_loss": raw_net-net,
            "net": net, "effective_cost": gross-net}


def quote_exit_at_age_details(gross, terms, age):
    """Conditional holding-age sensitivity; does not assert an executable date."""
    age = number(age, "source holding age")
    require(age == age.to_integral_value(), "Source holding age must be an integer")
    return _exit_at_age(gross, terms, age)


def quote_exit_details(gross, terms, *, acquired_at, execution_at, confirmed_at=None):
    """Estimated execution cash: keep contractual fee and fund rounding separate."""
    if confirmed_at is not None and terms["holding"]["end_event"] == "confirmation_date":
        ages = [trading_calendar.holding_days(acquired_at, confirmed_at, terms["holding"])]
    else:
        ages = holding_ages_at_exit(terms, {"earliest": acquired_at, "latest": acquired_at}, execution_at)
    require(min(ages) >= terms["holding"]["minimum_days"], "Lot remains locked")
    return max((quote_exit_at_age_details(gross, terms, age) for age in ages), key=lambda result: result["effective_cost"])


def quote_exit(gross, terms, *, acquired_at, execution_at, confirmed_at=None):
    """Investor exit cost for model valuation; never an asserted actual ledger fee."""
    return quote_exit_details(gross, terms, acquired_at=acquired_at, execution_at=execution_at, confirmed_at=confirmed_at)["effective_cost"]


def validate_sale(shares, total_shares, terms, *, acquired_at, execution_at):
    quantity, total = number(shares), number(total_shares)
    require(0 < quantity <= total, "Invalid redemption quantity")
    trading_calendar.holding_days(acquired_at, execution_at, terms["holding"])
    require(trading_calendar.holding_days(acquired_at, execution_at, terms["holding"]) >= terms["holding"]["minimum_days"], "Lot remains locked")
    if quantity == total and terms.get("full_redemption_allowed") is True:
        return
    require(terms.get("minimum_redemption_shares") is not None and terms.get("minimum_remaining_shares") is not None,
            "Partial redemption share thresholds need current source evidence")
    minimum, residual = number(terms["minimum_redemption_shares"]), number(terms["minimum_remaining_shares"])
    require(quantity >= minimum or quantity == total, "Redemption is below the minimum shares")
    require(total - quantity == 0 or total - quantity >= residual, "Redemption leaves a forbidden residual lot")


def fee_bounds(total_wealth, terms, ages):
    """Conservative range over redeemable NAV wealth; no invented dividend fee."""
    rule = validate_rule(terms["redemption"])
    if terms["trade_precision"].get("redemption_net_rounding") == "down_fund":
        total, step = number(total_wealth), number(terms["trade_precision"]["money_step"])
        bounds = [Decimal(0)]
        for age in ages:
            if rule["kind"] == "amount_tiers":
                intervals = [(band["fee"], number(band["minimum"]),
                    None if band.get("maximum") is None else number(band["maximum"])) for band in rule["bands"]]
                if "maximum" not in rule["bands"][0]:
                    intervals = [(band["fee"], number(band["minimum"]),
                        number(rule["bands"][i+1]["minimum"]) if i+1 < len(rule["bands"]) else None)
                        for i, band in enumerate(rule["bands"])]
            else:
                intervals = [(_select(rule, total, Decimal(age)), Decimal(0), None)]
            for fee_rule, lower, upper in intervals:
                limit = total if upper is None else min(total, upper)
                if limit <= 0 or limit < lower:
                    continue
                fee = limit * number(fee_rule["rate"]) if fee_rule["kind"] == "percentage" else number(fee_rule["amount"])
                if terms["trade_precision"]["fee_rounding"] == "half_up":
                    fee = fee.quantize(Decimal(terms["trade_precision"]["money_step"]), rounding=ROUND_HALF_UP)
                if fee <= limit:
                    # Net truncation loss is strictly below one money step.
                    # Evaluate the continuous upper limit even for an open
                    # band; a made-up cent epsilon would miss its supremum.
                    bounds.append(min(limit, fee + step))
        return Decimal(0), max(bounds)
    total = number(total_wealth).quantize(Decimal(terms["trade_precision"]["money_step"]), rounding=ROUND_HALF_UP)
    candidates = {Decimal(0), total}
    if rule["kind"] == "amount_tiers":
        for band in rule["bands"]:
            for boundary in (band["minimum"], band.get("maximum")):
                if boundary is not None:
                    boundary = number(boundary)
                    candidates.update(value for value in (boundary, boundary - Decimal(".01"), boundary + Decimal(".01")) if 0 <= value <= total)
    fees = [Decimal(0)]
    for age in ages:
        for value in candidates:
            if value > 0:
                selected = _select(rule, value, Decimal(age))
                raw_fee = value * number(selected["rate"]) if selected["kind"] == "percentage" else number(selected["amount"])
                if raw_fee <= value:
                    fees.append(_exit_at_age(value, terms, age)["effective_cost"])
    return min(fees), max(fees)


def parse_fee_block(block, field):
    """Supported labelled fee expressions; reject ambiguous source rows."""
    text = block["text"].replace("％", "%").replace("，", ",")
    labels = {"subscription": r"申购|认购|subscription", "redemption": r"赎回|redemption"}
    require(field in labels and re.search(labels[field], text, re.I), "Source row does not identify this fee")
    if re.search(r"不收取(?:申购|赎回|认购)费|免(?:申购|赎回|认购)费", text):
        require(not re.search(r"[1-9]\d*(?:\.\d+)?\s*%", text), "Ambiguous waiver and nonzero fee")
        return {"kind": "percentage", "rate": "0"}
    rates = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", text)
    fixed = re.findall(r"(?:每笔|固定|fixed)[^\d]{0,10}(\d+(?:\.\d+)?)\s*(?:元|CNY)", text, re.I)
    if len(rates) == 1 and not fixed and not re.search(r"持有|[<>≤≥]|以下|以上|不足|满|[MmNn]\s*[=：]", text):
        return validate_rule({"kind": "percentage", "rate": str(Decimal(rates[0]) / 100)})
    if len(fixed) == 1 and not rates:
        return validate_rule({"kind": "fixed", "amount": fixed[0]})
    raise ValueError("Fee expression needs the supported complete bound-row table parser")


def parse_fee_rows(blocks, field):
    if len(blocks) == 1:
        try:
            return parse_fee_block(blocks[0], field)
        except ValueError:
            pass
    bands, kind, previous_end = [], None, None
    for block in blocks:
        cells, headers = block.get("cells"), block.get("headers")
        require(cells and headers and len(cells) == len(headers), "Fee tiers require source table cells and headers")
        joined = " ".join(headers)
        require(("申购" if field == "subscription" else "赎回") in joined, "Fee table subject differs")
        columns = [i for i, header in enumerate(headers) if "费率" in header or "费用" in header]
        bounds = [i for i, header in enumerate(headers) if "持有" in header or "金额" in header]
        require(len(columns) == len(bounds) == 1, "Ambiguous fee table columns")
        rate_text, condition = str(cells[columns[0]]).replace("％", "%"), str(cells[bounds[0]])
        row_kind = "holding_tiers" if "持有" in headers[bounds[0]] else "amount_tiers"
        require(kind is None or kind == row_kind, "Mixed fee-tier coordinates")
        kind = row_kind
        condition = condition.replace("≤", "<=").replace("＜", "<").replace("≥", ">=").replace("　", " ")
        # Preserve upper bounds until continuity is proved; never turn a
        # collection of quoted percentages into an invented complete schedule.
        match = re.fullmatch(r"\s*(?:(\d+(?:\.\d+)?)\s*<=\s*)?[NMnm]\s*(?:<\s*(\d+(?:\.\d+)?))?\s*(天|日|元|万元)?\s*", condition)
        if match:
            lo, hi, unit = match.groups()
            lo, hi = Decimal(lo or "0"), None if hi is None else Decimal(hi)
        else:
            tail = re.fullmatch(r"\s*[NMnm]\s*>=\s*(\d+(?:\.\d+)?)\s*(天|日|元|万元)?\s*", condition)
            require(tail is not None, "Unsupported or ambiguous fee-tier interval")
            lo, hi, unit = Decimal(tail[1]), None, tail[2]
        factor = Decimal(10000) if unit == "万元" else Decimal(1)
        lo *= factor
        hi = None if hi is None else hi * factor
        require((not bands and lo == 0) or (bands and previous_end is not None and lo == previous_end), "Fee-tier intervals have a gap or overlap")
        require(hi is None or hi > lo, "Invalid fee interval")
        percentage = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%\s*", rate_text)
        fixed = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*元(?:/笔)?\s*", rate_text)
        require(percentage is not None or fixed is not None, "Unsupported fee-tier charge")
        fee = {"kind": "percentage", "rate": str(Decimal(percentage[1])/100)} if percentage else {"kind": "fixed", "amount": fixed[1]}
        bands.append({"minimum": str(lo), "fee": fee})
        previous_end = hi
    require(previous_end is None, "Fee schedule has no open-ended final band")
    return validate_rule({"kind": kind, "bands": bands})


LEAVES = {
    "subject.code": ("基金代码", "code"), "subject.share_class": ("份额类别", "text"),
    "subject.currency": ("交易币种", "currency"), "subject.channel": ("销售渠道", "text"),
    "subject.investor_type": ("投资者类别", "text"),
    "min_buy": ("最低申购金额", "amount"), "max_buy": ("最高申购金额", "optional_amount"),
    "buyable": ("申购状态", "buy_status"), "sellable": ("赎回状态", "sell_status"),
    "minimum_redemption_shares": ("最低赎回份额", "amount"),
    "minimum_remaining_shares": ("最低剩余份额", "amount"),
    "holding.minimum_days": ("最低持有天数", "integer"), "holding.day_basis": ("持有期日历", "basis"),
    "holding.start_inclusive": ("持有期含起始日", "boolean"), "holding.end_inclusive": ("持有期含结束日", "boolean"),
    "holding.end_event": ("持有期终点", "acquisition"),
    "settlement.lag_days": ("赎回到账间隔", "integer"), "settlement.day_basis": ("赎回到账日历", "basis"),
    "order_cutoff_local": ("订单截止时间", "local_time"),
    "confirmation.lag_days": ("正常确认间隔", "integer"),
    "confirmation.day_basis": ("正常确认日历", "basis"),
    "trade_precision.money_step": ("金额精度", "amount"), "trade_precision.share_step": ("份额精度", "amount"),
    "trade_precision.fee_rounding": ("费用取整", "rounding"),
    "trade_precision.share_rounding": ("份额取整", "share_rounding"),
    "acquisition_rule.holding_start": ("持有期起点", "acquisition"),
    "acquisition_rule.ownership_start": ("份额权益起点", "acquisition"),
}


def _leaf(block, label, kind):
    # Exact labels make the supported grammar reviewable. Additional issuer
    # adapters must map a proved table heading, not caller-created keywords.
    matches = re.findall(r"(?:^|[;；\n])\s*" + re.escape(label) + r"\s*[:：]\s*([^;；\n]+)", block["text"])
    if not matches and block.get("cells") and block.get("headers"):
        matches = [str(value).strip() for header, value in zip(block["headers"], block["cells"]) if header.strip() == label]
    if not matches:
        text = re.sub(r"\s+", "", block["text"])
        # Verbatim issuer wording adapters. No value is filled merely because
        # a product commonly follows a convention.
        if label == "费用取整" and re.search(r"(?:赎回费|赎回费用|赎回总额).*(?:四舍五入.*保留(?:到)?小数点后两位|保留(?:到)?小数点后两位.*四舍五入)", text):
            return "half_up"
        if label == "份额精度" and re.search(r"申购份额.*保留(?:到)?小数点后[两2]位", text):
            return "0.01"
        if label == "金额精度" and re.search(r"(?:申购金额|赎回金额|赎回总额).*保留(?:到)?小数点后[两2]位", text):
            return "0.01"
        if label == "份额取整" and re.search(r"申购份额.*四舍五入.*(?:基金财产|基金资产|基金承担)", text):
            return "half_up_fund"
        if label == "份额取整" and re.search(r"申购份额.*(?:截位|向下取整).*(?:返还|退回).*(?:投资者|投资人)", text):
            return "down_refund"
        if label in ("申购状态", "赎回状态"):
            values = [value for value in (("开放申购", "暂停申购") if label == "申购状态" else ("开放赎回", "暂停赎回")) if value in text]
            if len(values) == 1:
                return values[0].startswith("开放")
        if label == "基金代码":
            matches = re.findall(r"基金代码[：:]?(\d{6})(?!\d)", text)
        if label == "最低申购金额":
            matches = re.findall(r"(?:首次|单笔|最低)申购(?:的最低)?金额(?:不得低于|不低于|为)(\d+(?:\.\d+)?元)", text)
        if label == "最低赎回份额":
            matches = re.findall(r"(?:单笔|每笔|最低)赎回(?:的最低)?份额(?:不得低于|不低于|为)(\d+(?:\.\d+)?份)", text)
        if label == "最低剩余份额":
            matches = re.findall(r"(?:最低|最少)(?:保留|剩余)(?:基金)?份额(?:为|不得低于)(\d+(?:\.\d+)?份)", text)
    require(len(matches) == 1, "Missing or ambiguous source field: " + label)
    value = matches[0].strip()
    if kind in ("amount", "optional_amount", "integer"):
        if kind == "optional_amount" and value in ("无限额", "无上限"):
            return None
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:元|份|天|日)?", value)
        require(match is not None, "Unsupported monetary/day expression: " + label)
        parsed = number(match[1])
        if kind == "integer":
            require(parsed == parsed.to_integral_value(), "Fractional day count")
            return int(parsed)
        return str(parsed)
    if kind == "local_time":
        require(re.fullmatch(r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?", value) is not None,
                "Unsupported local order cutoff: " + label)
        return dt.time.fromisoformat(value).isoformat()
    if kind in ("instant", "optional_instant"):
        if kind == "optional_instant" and value == "长期有效":
            return None
        instant(value)
        return value
    choices = {"currency": {"人民币": "CNY", "CNY": "CNY"},
               "basis": {"自然日": "calendar_days", "交易日": "trading_days"},
               "boolean": {"是": True, "否": False},
               "rounding": {"四舍五入至分": "half_up"},
               "share_rounding": {"四舍五入误差归基金": "half_up_fund", "向下截位余款退回": "down_refund"},
               "buy_status": {"开放申购": True, "暂停申购": False},
               "sell_status": {"开放赎回": True, "暂停赎回": False},
               "acquisition": {"成交日": "execution_date", "确认日": "confirmation_date"}}
    if kind in choices:
        require(value in choices[kind], "Unsupported source convention: " + label)
        return choices[kind][value]
    if kind == "code":
        require(re.fullmatch(r"\d{6}", value) is not None, "Exact source product code required")
    require(bool(value), "Empty source subject")
    return value


def _assign(target, path, value):
    keys = path.split(".")
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value


def verify(contract, artifacts, *, as_of):
    """Re-extract every source-bound rule before trusting a frozen contract."""
    from source_documents import resolve_span
    fields(contract, {"schema_version", "adapter", "code", "quote_observed_at", "rule_effective_dates", "normalized", "contract_hash"}, {"bindings", "sources"}, label="fee contract")
    require(contract["schema_version"] == 4, "Only fee contract schema4 is accepted")
    require(contract["contract_hash"] == fingerprint({key: value for key, value in contract.items() if key != "contract_hash"}), "Fee contract changed")
    require(instant(contract["quote_observed_at"]) <= instant(as_of), "Quote source was observed after the decision")
    if contract["adapter"] == "document_formats_v2":
        from fee_sources import inspect_document_formats
        inspected = inspect_document_formats(contract["code"], contract["sources"], artifacts, as_of=as_of)
        if inspected["normalized"] != contract["normalized"] or inspected["quote_observed_at"] != contract["quote_observed_at"] or inspected["rule_effective_dates"] != contract["rule_effective_dates"]:
            raise EvidenceError("Issuer/platform contract differs from original-source extraction")
        thresholds = {"minimum_redemption_shares", "minimum_remaining_shares"}
        blocking = [action for action in inspected["required_actions"]
                    if action.get("action") != "bind_remaining_daily_subscription_limit_to_account_usage"
                    and not (action.get("field") in thresholds and inspected["normalized"].get("full_redemption_allowed") is True)]
        if blocking:
            raise EvidenceGap("Published dealing evidence is incomplete", inspected["required_actions"])
        return inspected["normalized"]
    require(contract["adapter"] == "field_bindings_v1" and "bindings" in contract and "sources" not in contract, "Unsupported fee evidence adapter")
    clock_paths = ("order_cutoff_local", "confirmation.lag_days", "confirmation.day_basis", "execution_calendar")
    missing = [path for path in clock_paths if type(contract["bindings"].get(path)) is not dict]
    if missing:
        raise EvidenceGap("Source normal pricing/confirmation evidence is incomplete", [
            {"action": "capture_source_normal_dealing_clock", "code": contract["code"], "field": path} for path in missing])
    extracted = {}
    for path, (label, kind) in LEAVES.items():
        binding = contract["bindings"].get(path)
        require(type(binding) is dict, "Missing field-level source binding: " + path)
        block = resolve_span(binding["document_ref"], binding["locator"], artifacts)
        try:
            value = _leaf(block, label, kind)
            if path == "subject.investor_type":
                from fee_sources import canonical_investor_scope
                value = canonical_investor_scope(value)
                if value == "unknown":
                    raise EvidenceGap("Original fee investor applicability is not established", [{
                        "action": "capture_exact_investor_fee_applicability", "code": contract["code"],
                        "field": path, "document_ref": binding["document_ref"], "locator": binding["locator"]}])
        except ValueError as exc:
            if path not in clock_paths:
                raise
            raise EvidenceGap("Source normal dealing clock is not established", [
                {"action": "inspect_source_normal_dealing_clock", "code": contract["code"],
                 "field": path, "reason": str(exc)}]) from exc
        _assign(extracted, path, value)
    extracted["confirmation"]["normal_conditions_only"] = True
    for field in ("subscription", "redemption"):
        bindings = contract["bindings"].get(field)
        require(type(bindings) is list and bindings, "Missing field-level fee source")
        blocks = [resolve_span(item["document_ref"], item["locator"], artifacts) for item in bindings]
        for block in blocks:
            scope_text = block["text"] + " " + " ".join(block.get("headers", []))
            mentioned_codes = set(re.findall(r"(?<!\d)\d{6}(?!\d)", scope_text))
            classes = set(re.findall(r"([A-Z])类", scope_text))
            require(not mentioned_codes or extracted["subject"]["code"] in mentioned_codes, "Fee row refers to another product code")
            require(not classes or extracted["subject"]["share_class"] in classes, "Fee row refers to another share class")
            require(extracted["subject"]["code"] in mentioned_codes or extracted["subject"]["share_class"] in classes
                    or "各类基金份额" in scope_text, "Fee row lacks unambiguous product/share-class scope")
        extracted[field] = parse_fee_rows(blocks, field)
    if extracted["settlement"]["day_basis"] == "trading_days":
        binding = contract["bindings"].get("settlement.calendar")
        require(type(binding) is dict, "Trading-day settlement requires a calendar source")
        extracted["settlement"]["calendar"] = trading_calendar.from_source(binding, artifacts)
    binding = contract["bindings"]["execution_calendar"]
    extracted["execution_calendar"] = trading_calendar.from_source(binding, artifacts)
    require(extracted["subject"]["code"] == contract["code"], "Fee source belongs to another product")
    require(extracted == contract["normalized"], "Normalized dealing fields differ from source re-extraction")
    # Each independent document must establish the product/class/channel and
    # applicability of its rows. A valid rate quotation from another fund is
    # not evidence for this contract.
    subject_document = contract["bindings"]["subject.code"]["document_ref"]
    for path, binding in contract["bindings"].items():
        for item in binding if isinstance(binding, list) else [binding]:
            if path in ("settlement.calendar", "execution_calendar") or item["document_ref"] == subject_document:
                continue
            scope = item.get("subject_scope")
            require(type(scope) is dict, "Independent fee/dealing document lacks subject scope")
            for key in ("code", "share_class", "currency", "channel", "investor_type"):
                require(key in scope, "Incomplete independent document subject")
                label, kind = LEAVES["subject." + key]
                value = _leaf(resolve_span(item["document_ref"], scope[key], artifacts), label, kind)
                if key == "investor_type":
                    from fee_sources import canonical_investor_scope
                    value = canonical_investor_scope(value)
                require(value == extracted["subject"][key], "Cross-document fee subject differs")
    require(extracted["trade_precision"]["money_step"] == "0.01" and number(extracted["trade_precision"]["share_step"]) > 0, "Unsupported monetary/share precision")
    from source_documents import read_extracted_document
    captures = [read_extracted_document(contract["bindings"][name]["document_ref"], artifacts)["retrieved_at"]
                for name in ("buyable", "sellable", "min_buy", "max_buy")]
    def clock_document_refs(value):
        if type(value) is dict:
            if "document_ref" in value:
                yield value["document_ref"]
            else:
                for child in value.values():
                    yield from clock_document_refs(child)
        elif type(value) is list:
            for child in value:
                yield from clock_document_refs(child)
    clock_captures = [read_extracted_document(reference, artifacts)["retrieved_at"]
                      for name in clock_paths for reference in clock_document_refs(contract["bindings"][name])]
    require(all(instant(stamp) <= instant(as_of) for stamp in captures+clock_captures), "Future-captured trading status or clock")
    require(min(captures, key=instant) == contract["quote_observed_at"], "Quote observation timestamp differs from its oldest required dynamic field")
    dates = contract["rule_effective_dates"]
    require(type(dates) is dict and dates.get("status") in ("unknown", "explicit"), "Explicit rule date knowledge state required")
    if dates["status"] == "unknown":
        fields(dates, {"status"}, label="unknown rule dates")
    else:
        fields(dates, {"status", "from", "until"}, label="source-declared rule dates")
        for name, label in (("from", "规则生效时间"), ("until", "规则失效时间")):
            if dates[name] is not None:
                binding = contract["bindings"].get("rule_effective_"+name)
                require(type(binding) is dict, "Explicit rule date lacks source binding")
                value = _leaf(resolve_span(binding["document_ref"], binding["locator"], artifacts), label, "instant")
                require(value == dates[name], "Rule date differs from source")
        if ((dates["from"] is not None and instant(dates["from"]) > instant(as_of))
                or (dates["until"] is not None and instant(as_of) >= instant(dates["until"]))):
            raise EvidenceGap("Source-declared fee rule does not apply at this decision", [{"action": "refresh_current_dealing_rule", "code": contract["code"], "rule_effective_dates": dates}])
    return extracted


def inspect_contract(request, artifacts, *, as_of):
    """Public fee_inspect payload: extract facts and retain concrete gaps."""
    fields(request, {"schema_version", "adapter", "code", "sources"}, label="fee inspection request")
    require(request["schema_version"] == 4 and request["adapter"] == "document_formats_v2", "Unsupported fee inspection adapter")
    from fee_sources import inspect_document_formats
    try:
        result = inspect_document_formats(request["code"], request["sources"], artifacts, as_of=as_of)
    except ValueError as exc:
        return {"status": "needs_research", "contract": None, "field_statuses": {},
                "required_actions": [{"action": "review_original_dealing_document", "code": request["code"], "reason": str(exc)}]}
    contract = {"schema_version": 4, "adapter": request["adapter"], "code": request["code"], "sources": copy.deepcopy(request["sources"]),
                "quote_observed_at": result["quote_observed_at"], "rule_effective_dates": result["rule_effective_dates"], "normalized": result["normalized"]}
    contract["contract_hash"] = fingerprint(contract)
    return {**result, "status": "needs_research" if result["required_actions"] else "ready", "contract": contract}


def normalize_to_terms(contract, artifacts, *, as_of, contract_ref):
    """The controller's sole route from bound source to numerical asset terms."""
    normalized = verify(contract, artifacts, as_of=as_of)
    investor_scope = normalized["subject"].get("investor_type")
    if investor_scope not in ("retail", "personal_pension"):
        raise EvidenceGap("Original fee investor applicability is not established", [{
            "action": "capture_exact_investor_fee_applicability", "code": contract["code"],
            "field": "subject.investor_type", "fee_contract_ref": contract_ref}])
    observed_at = contract["quote_observed_at"]
    def maximum_rate(rule):
        if rule["kind"] == "percentage":
            return number(rule["rate"])
        if rule["kind"] == "fixed":
            return Decimal(0)
        return max(maximum_rate(row["fee"]) for row in rule["bands"])
    result = {"code": contract["code"], "subscription_fee": str(maximum_rate(normalized["subscription"])),
              "investor_scope": investor_scope,
              "redemption_fee": str(maximum_rate(normalized["redemption"])), "min_buy": normalized["min_buy"],
              "max_weight": 1., "settlement_days": normalized["settlement"]["lag_days"],
              "buyable": normalized["buyable"], "sellable": normalized["sellable"], "observed_at": observed_at,
              "fee_contract": normalized, "fee_contract_hash": fingerprint(normalized), "fee_contract_ref": contract_ref,
              "confirmed_execution_max_calendar_days": confirmation_window_days(normalized, as_of),
              "execution_bound_source": {"fee_contract_ref": contract_ref}}
    if normalized["max_buy"] is not None:
        result["max_buy"] = normalized["max_buy"]
    return result
