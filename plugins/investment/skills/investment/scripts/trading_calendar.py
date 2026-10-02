"""Evidence-bound calendars. Weekdays alone are never a trading calendar."""
import datetime as dt
import re

from contracts import fields, instant, require


def validate(calendar):
    fields(calendar, {"id", "timezone", "open_dates", "coverage_start", "coverage_end", "evidence_ref"}, label="trading calendar")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(calendar["timezone"])
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise ValueError("Unsupported source calendar timezone") from error
    days = [dt.date.fromisoformat(day) for day in calendar["open_dates"]]
    start, end = (dt.date.fromisoformat(calendar[key]) for key in ("coverage_start", "coverage_end"))
    require(start <= end and days == sorted(set(days)) and all(start <= day <= end for day in days), "Invalid calendar coverage")
    require(isinstance(calendar["evidence_ref"], dict) and calendar["evidence_ref"], "Calendar evidence required")
    return calendar


def advance(day, count, basis, calendar=None):
    day = dt.date.fromisoformat(day) if isinstance(day, str) else day
    require(type(count) is int and count >= 0, "Nonnegative settlement lag required")
    if basis == "calendar_days":
        return day + dt.timedelta(days=count)
    require(basis == "trading_days" and calendar is not None, "Explicit trading calendar required")
    validate(calendar)
    require(calendar["coverage_start"] <= day.isoformat() <= calendar["coverage_end"], "Date outside verified calendar")
    if count == 0:
        require(day.isoformat() in calendar["open_dates"], "Zero-lag execution requires an open date")
        return day
    later = [value for value in calendar["open_dates"] if value > day.isoformat()]
    require(len(later) >= count, "Settlement exceeds verified calendar coverage")
    return dt.date.fromisoformat(later[count - 1])


def on_or_after(day, calendar):
    validate(calendar)
    day = day.isoformat() if isinstance(day, dt.date) else day
    require(calendar["coverage_start"] <= day <= calendar["coverage_end"], "Date outside verified calendar")
    available = [value for value in calendar["open_dates"] if value >= day]
    require(available, "No verified execution day remains")
    return available[0]


def holding_days(acquired_at, execution_at, rule):
    from zoneinfo import ZoneInfo
    fields(rule, {"minimum_days", "day_basis", "start_inclusive", "end_inclusive", "end_event"}, label="holding rule")
    require(rule["day_basis"] == "calendar_days", "Holding day convention is not implemented")
    require(type(rule["minimum_days"]) is int and rule["minimum_days"] >= 0, "Invalid holding lock")
    require(type(rule["start_inclusive"]) is bool and type(rule["end_inclusive"]) is bool, "Holding endpoints must be explicit")
    zone = ZoneInfo("Asia/Shanghai")
    start, end = instant(acquired_at).astimezone(zone).date(), instant(execution_at).astimezone(zone).date()
    require(end >= start, "Execution precedes acquisition")
    return max(0, (end - start).days - 1 + int(rule["start_inclusive"]) + int(rule["end_inclusive"]))


def intersection(calendars, identity="source-calendar-intersection"):
    require(calendars, "At least one source calendar is required")
    for calendar in calendars:
        validate(calendar)
    require(len({calendar["timezone"] for calendar in calendars}) == 1,
            "Calendar dates in different timezones need an explicit issuer date projection")
    start, end = max(c["coverage_start"] for c in calendars), min(c["coverage_end"] for c in calendars)
    require(start <= end, "Calendar sources have no common coverage")
    common = set(calendars[0]["open_dates"])
    for calendar in calendars[1:]:
        common.intersection_update(calendar["open_dates"])
    return validate({"id": identity, "timezone": calendars[0]["timezone"], "coverage_start": start, "coverage_end": end,
                     "open_dates": sorted(day for day in common if start <= day <= end),
                     "evidence_ref": {"calendars": [calendar["evidence_ref"] for calendar in calendars]}})


def from_exchange_notice(rule_text, notice_text, year, evidence_ref):
    """Derive a bounded calendar from the exchange's rule and annual notice."""
    require(type(year) is int and 2000 <= year <= 2100, "Explicit exchange notice year required")
    rule = re.sub(r"\s+", "", rule_text)
    text = re.sub(r"\s+", "", notice_text)
    require("交易日为每周一至周五" in rule and "休市" in rule, "Exchange weekday/closure rule is not evidenced")
    require(str(year)+"年" in text and "休市安排" in text, "Annual holiday notice year differs")
    require(all(name in text for name in ("元旦", "春节", "清明", "劳动", "端午", "中秋", "国庆")), "Annual holiday notice is incomplete")
    pattern = r"(\d{1,2})月(\d{1,2})日(?:[（(][^）)]*[）)])?至(?:(\d{1,2})月)?(\d{1,2})日(?:[（(][^）)]*[）)])?休市"
    closed = set()
    ranges = re.findall(pattern, text)
    require(len(ranges) >= 7, "Unsupported or incomplete annual closure intervals")
    for month, day, end_month, end_day in ranges:
        start = dt.date(year, int(month), int(day))
        end = dt.date(year, int(end_month or month), int(end_day))
        require(start <= end, "Cross-year closure needs an explicit adapter")
        closed.update(start+dt.timedelta(days=offset) for offset in range((end-start).days+1))
    first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
    dates = [first+dt.timedelta(days=offset) for offset in range((last-first).days+1)]
    return validate({"id": "exchange-notice-"+str(year), "timezone": "Asia/Shanghai", "coverage_start": str(first),
                     "coverage_end": str(last), "open_dates": [str(day) for day in dates if day.weekday() < 5 and day not in closed],
                     "evidence_ref": evidence_ref})


def from_source(binding, artifacts):
    from source_documents import resolve_span
    kind = binding.get("kind")
    if kind == "exchange_annual_notice":
        rule = binding["weekday_rule"]
        rule_text = resolve_span(rule["document_ref"], rule["locator"], artifacts)["text"]
        notices = [resolve_span(item["document_ref"], item["locator"], artifacts)["text"] for item in binding["notice_spans"]]
        return from_exchange_notice(rule_text, "\n".join(notices), binding["year"], {"source_binding": binding})
    require(kind == "explicit_open_dates_json", "Explicit calendar source adapter required")
    import json
    block = resolve_span(binding["document_ref"], binding["locator"], artifacts)
    value = json.loads(block["text"])
    value["evidence_ref"] = {"document_ref": binding["document_ref"], "locator": binding["locator"]}
    return validate(value)
