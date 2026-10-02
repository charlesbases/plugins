"""Original economic measurements and explicit missingness, never sentiment scores.

Categories are a declared schema; their usefulness needs temporal out-of-sample
evidence. Relative changes are dimensionless, not estimated standard deviations.
"""
import calendar
import datetime as dt
import hashlib
import math
import re
import unicodedata
from decimal import Decimal

from contracts import fields, fingerprint, instant, require

INPUT_SCHEMA_ID = "source-news-asset-en-v2"
EVENT_STATES = ("measured", "qualitative_encoded", "unsupported", "source_gap")

CATEGORIES = ("monetary_policy", "liquidity", "fiscal_policy", "trade_policy",
              "industry_policy", "economic_activity", "inflation", "corporate_results")
FEATURE_NAMES = tuple(f"economic_{category}_{field}" for category in CATEGORIES for field in
                      ("count", "relative_delta_mean", "delta_missing_share", "relative_surprise_mean",
                       "surprise_missing_share", "action_up_count", "action_down_count",
                       "action_introduced_count", "action_removed_count", "action_unchanged_count")) + tuple(
    "economic_event_"+state+"_count" for state in EVENT_STATES)
METRICS = {
    "policy_rate": ("monetary_policy", ("利率", "interest rate", "federal funds rate")),
    "reserve_ratio": ("liquidity", ("准备金率", "reserve requirement")),
    "liquidity_amount": ("liquidity", ("逆回购", "流动性", "liquidity", "repo")),
    "fiscal_amount": ("fiscal_policy", ("财政", "补贴", "国债", "fiscal", "subsid")),
    "tariff_rate": ("trade_policy", ("关税", "tariff")),
    "policy_action": ("industry_policy", ("政策", "办法", "通知", "policy", "regulation")),
    "output_growth": ("economic_activity", ("增加值", "生产", "GDP", "产量", "output", "growth")),
    "pmi": ("economic_activity", ("采购经理", "PMI", "purchasing managers")),
    "inflation": ("inflation", ("价格", "CPI", "PPI", "inflation", "price index")),
    "revenue": ("corporate_results", ("收入", "revenue", "sales")),
    "profit": ("corporate_results", ("利润", "profit", "earnings")),
}
UNITS = {
    "percent": ("percent", Decimal(1), ("%", "％", "percent", "per cent")),
    "percentage_point": ("percentage_point", Decimal(1), ("个百分点", "percentage point")),
    "basis_point": ("percentage_point", Decimal("0.01"), ("个基点", "基点", "basis point", "bp")),
    "CNY": ("CNY", Decimal(1), ("元", "yuan", "CNY", "RMB")),
    "CNY_10k": ("CNY", Decimal(10000), ("万元",)),
    "CNY_100m": ("CNY", Decimal(100000000), ("亿元",)),
    "index_point": ("index_point", Decimal(1), ("点", "points", "point")),
    "USD": ("USD", Decimal(1), ("USD", "dollars", "$")),
    "EUR": ("EUR", Decimal(1), ("EUR", "euros", "€")),
}
ACTIONS = {
    "up": ("提高", "上调", "增加", "raise", "increase", "raised"),
    "down": ("降低", "下调", "减少", "lower", "reduce", "cut"),
    "unchanged": ("保持不变", "维持", "unchanged", "maintain"),
    "introduced": ("发布", "印发", "设立", "introduc", "establish"),
    "removed": ("废止", "取消", "撤销", "repeal", "abolish", "withdraw"),
}


def _publication_bounds(value):
    require(type(value) is dict and value.get("precision") in ("date", "timestamp"),
            "Economic evidence needs parsed source publication time")
    if value["precision"] == "timestamp":
        at = instant(value["value"])
        return at, at
    from zoneinfo import ZoneInfo
    date = dt.date.fromisoformat(value["value"])
    start = dt.datetime.combine(date, dt.time.min, ZoneInfo(value["timezone"]))
    return start, start + dt.timedelta(days=1) - dt.timedelta(microseconds=1)


def _period(label):
    require(type(label) is str and bool(label), "A literal economic calendar period is required")
    value = unicodedata.normalize("NFKC", label)
    daily = re.fullmatch(r"([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日", value)
    if daily:
        return dt.date(*(int(part) for part in daily.groups())).isoformat()
    match = re.fullmatch(r"([0-9]{4})[-年]([0-9]{1,2})(?:月|[-/]([0-9]{1,2})(?:日)?)?", value)
    if match:
        year, month = int(match[1]), int(match[2])
        day = int(match[3]) if match[3] else calendar.monthrange(year, month)[1]
        return dt.date(year, month, day).isoformat()
    match = re.fullmatch(r"([0-9]{4})年?第?([一二三四1-4])季度", value)
    if match:
        quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(match[2]) or int(match[2])
        month = quarter * 3
        return dt.date(int(match[1]), month, calendar.monthrange(int(match[1]), month)[1]).isoformat()
    match = re.fullmatch(r"([0-9]{4})年?", value)
    require(match is not None, "Economic period format needs a registered calendar parser")
    return f"{match[1]}-12-31"


def _source_period(label, quote):
    """Bind a literal label to one complete source calendar span."""
    require(type(label) is str and label in quote, "Economic calendar period is not quoted from source")
    normalized = unicodedata.normalize("NFKC", quote)
    # Longest registered forms precede their month/year prefixes. Bare monetary
    # numbers are not calendar years; a bare year needs its own lexical token.
    pattern = (r"(?<![0-9])(?:[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日"
               r"|[0-9]{4}[-年][0-9]{1,2}[-/][0-9]{1,2}日?"
               r"|[0-9]{4}年?第?[一二三四1-4]季度"
               r"|[0-9]{4}(?:年[0-9]{1,2}月|-[0-9]{1,2})"
               r"|[0-9]{4}年"
               r"|(?<![\w.$€￥+−/-])[0-9]{4}(?![\w.%+−/-]))"
               r"(?![0-9]|月|日|季度|[-/])")
    periods = [match[0] for match in re.finditer(pattern, normalized)]
    require(len(periods) == 1, "Economic quote needs one unambiguous complete source calendar period")
    source = periods[0]
    source_end = _period(source)
    require(_period_type(label) == _period_type(source) and _period(label) == source_end,
            "Economic calendar label differs from complete source period granularity or date")
    return source_end


def _support(point, manifest, artifacts, store, registry):
    fields(point, {"version_id", "quote"}, {"value_text", "unit", "period_label", "measurement_type", "value_role"}, "economic source point")
    versions = {row["version_id"]: row for row in manifest["versions"]}
    require(point["version_id"] in versions, "Economic point needs a captured body version")
    version = versions[point["version_id"]]
    capture = next((row for row in manifest["retrievals"] if row.get("version_id") == point["version_id"]
                    and row["state"] == "body" and row.get("within_information_cutoff")), None)
    require(capture is not None, "Economic source is outside the frozen information cutoff")
    quote = point["quote"]
    body = artifacts.read(version["body_ref"]).decode("utf-8")
    require(type(quote) is str and quote.strip() and quote in body, "Economic quote differs from original body")
    import source_fetch
    require(fingerprint(registry) == manifest["registry_hash"], "Economic registry differs from frozen collection")
    source_fetch.validate_capture_provenance(capture, registry)
    artifacts.read(capture["raw_ref"])
    first = store.get("news-version-first-observed", point["version_id"]) if store else None
    require(first is not None and first["version_id"] == point["version_id"],
            "Economic availability lacks its original first observation")
    original = store.get("news-retrieval", first["retrieval_id"])
    operation = store.get("news-operation", fingerprint({"operation_id": first["collection_id"]}))
    require(original is not None and operation is not None
            and original.get("version_id") == point["version_id"]
            and original["retrieved_at"] == first["observed_at"]
            and original["raw_ref"] == first["raw_ref"],
            "Economic first observation differs from its original capture journal")
    original_collection = artifacts.read_json(operation["manifest_ref"])
    import news
    _, original_registry = news.collection_policy(original_collection, store, artifacts)
    require(original in original_collection["retrievals"], "Economic first capture lacks its original collection")
    source_fetch.validate_capture_provenance(original, original_registry)
    raw = artifacts.read(first["raw_ref"])
    require(hashlib.sha256(raw).hexdigest() == original["raw_sha256"], "Economic original source bytes changed")
    known = first["observed_at"]
    require(instant(known) <= instant(capture["retrieved_at"]), "Economic availability exceeds real capture")
    return {"version_id": point["version_id"], "source_id": version["source_id"], "source_url": capture["final_url"],
            "quote": quote, "body_ref": version["body_ref"], "raw_ref": capture["raw_ref"],
            "known_at": known, "published_at": version["published_at"]}


def _source_transition(point, metric_label, quote, aliases):
    """Registered single-subject source clause, with explicit positional roles."""
    role = point["value_role"]
    require(role in ("before", "after"), "Unknown source transition value role")
    require(re.search(r"[与及和、，,；;。]|以及|同时", metric_label) is None,
            "Source transition requires one metric and subject")
    number = r"[+−-]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?"
    units = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    period = re.escape(point["period_label"])
    pattern = (r"(?:自|从)?"+period+r"(?:起)?[，,：:]?\s*"+re.escape(metric_label)
               +r"\s*(?:由|从)(?:此前的|原来的)?\s*(?P<before>"+number+r")\s*(?P<unit>"+units+r")"
               +r"\s*(?P<direction>调整|上调|下调)(?:为|至)\s*(?P<after>"+number+r")\s*(?P=unit)[。.]?")
    match = re.fullmatch(pattern, quote)
    require(match is not None, "Source transition needs a unique registered metric/calendar/before/after clause")
    require(point["value_text"] == match[role], "Source transition role and literal numeric token differ")
    before, after = (Decimal(match[name].replace(",", "").replace("−", "-")) for name in ("before", "after"))
    require((match["direction"] != "上调" or after > before)
            and (match["direction"] != "下调" or after < before), "Source transition direction contradicts its numbers")
    return {"kind": "source_stated_transition", "metric_label": metric_label,
            "period_label": point["period_label"], "source_unit": match["unit"],
            "before_value_text": match["before"], "after_value_text": match["after"],
            "direction": match["direction"]}


def _measurement(point, metric_id, metric_label, manifest, artifacts, store, registry, *, expectation=False):
    if point is None:
        return None
    require(type(point) is dict and {"value_text", "unit", "period_label"} <= point.keys(),
            "Economic measurement needs literal value, unit and period")
    result = _support(point, manifest, artifacts, store, registry)
    quote = result["quote"]
    require(metric_label in quote and any(alias.lower() in metric_label.lower() for alias in METRICS[metric_id][1]),
            "Metric label/category lacks original economic quote support")
    token = point["value_text"]
    require(type(token) is str and re.fullmatch(r"[+−-]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", token),
            "Economic numeric token must be an explicit finite decimal")
    require(point["unit"] in UNITS, "Economic unit has no registered conversion")
    canonical_unit, multiplier, aliases = UNITS[point["unit"]]
    patterns = [r"(?<![0-9.+−-])" + re.escape(token) + r"\s*" + re.escape(alias) for alias in aliases]
    patterns += [re.escape(alias) + r"\s*" + re.escape(token) + r"(?![0-9.])" for alias in aliases if alias in ("$", "€", "USD", "EUR", "CNY", "RMB")]
    require(any(re.search(pattern, quote, re.I) for pattern in patterns), "Economic value/unit is not a literal source pair")
    pair_pattern = r"(?<![0-9.])[+−-]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?\s*(?:"+"|".join(re.escape(alias) for alias in aliases)+r")"
    pairs = list(re.finditer(pair_pattern, quote, re.I))
    transition = None
    if "value_role" in point:
        require(not expectation and len(pairs) == 2, "Source transition roles require exactly two actual source values")
        transition = _source_transition(point, metric_label, quote, aliases)
    else:
        require(len(pairs) == 1, "Ambiguous economic quote needs one metric/calendar/value/unit clause")
    require(len(quote) <= 600 and metric_label in quote[:pairs[0].end()]
            and re.search(r"未|没有|并非|not\b|never\b", quote, re.I) is None,
            "Economic measurement needs one affirmative source metric clause")
    period_end = _source_period(point["period_label"], quote)
    value = Decimal(token.replace(",", "").replace("−", "-")) * multiplier
    prefix = quote[:pairs[0].start()]
    negative_change = re.search(r"(?:下降|下跌|减少|降低)(?:了)?\s*$|(?:declined|fell|decreased|reduced)\s+by\s*$", prefix, re.I)
    reported_change = negative_change or re.search(r"(?:增长|上涨|增加|上调|下调|提高)(?:了)?\s*$|(?:increased|rose|raised|grew)\s+by\s*$", prefix, re.I)
    measurement_type = "reported_change" if reported_change else "level"
    require(point.get("measurement_type", measurement_type) == measurement_type,
            "Economic measurement type differs from its literal source clause")
    if negative_change:
        require(value >= 0, "Ambiguous double-negative economic change needs source review")
        value = -value
    require(value.is_finite() and math.isfinite(float(value)), "Economic number must be finite")
    if expectation:
        require(re.search(r"预期|预计|预测|forecast|expect|project", quote, re.I) is not None,
                "Economic expectation needs explicit source forecast language")
    else:
        require(re.search(r"预期|预计|预测|forecast|expected|project(?:ed|ions?)\b", quote, re.I) is None,
                "A quoted forecast cannot be presented as an actual/prior economic measurement")
        if METRICS[metric_id][0] in ("economic_activity", "inflation", "corporate_results"):
            _, published_end = _publication_bounds(result["published_at"])
            require(dt.date.fromisoformat(period_end) <= published_end.date(),
                    "Actual economic observation cannot describe an unfinished future calendar period")
    measured = {**result, "value": float(value), "unit": canonical_unit, "source_unit": point["unit"],
            "value_text": token, "measurement_type": measurement_type,
            "expectation_scope": "quoted_source_forecast_not_assumed_market_consensus" if expectation else None,
            "sign_basis": "explicit_source_decline" if negative_change else "literal_numeric_sign",
            "period_label": point["period_label"], "period_end": period_end}
    if transition is not None:
        measured.update(value_role=point["value_role"], source_transition=transition)
    return measured


def assemble(items, manifest, claims, events, artifacts, known_at, *, store, registry):
    import news
    _, authoritative = news.collection_policy(manifest, store, artifacts)
    require(registry == authoritative, "Economic registry is not the authoritative run snapshot")
    require(type(items) is list and len(items) <= 200, "Bounded economic observations required")
    event_map = {row["event_id"]: row for row in events}
    fact_versions = {row["version_id"] for row in claims if row["kind"] == "fact"}
    result, identities = [], set()
    for item in items:
        fields(item, {"event_id", "sector_ids", "category", "metric_id", "metric_label", "current", "prior", "expectation", "action"},
               {"concept"}, label="economic observation")
        require(item["event_id"] in event_map, "Economic observation requires an assessed source event")
        event = event_map[item["event_id"]]
        require(type(item["sector_ids"]) is list and item["sector_ids"] and len(set(item["sector_ids"])) == len(item["sector_ids"])
                and all(type(value) is str and value.strip() == value and 0 < len(value) <= 100 for value in item["sector_ids"]),
                "Stable explicit sector identities are required")
        metric = item["metric_id"]
        require(metric in METRICS and item["category"] == METRICS[metric][0], "Economic metric/category differs from schema")
        require(type(item["metric_label"]) is str and item["metric_label"].strip(), "Literal economic metric label required")
        current = _measurement(item["current"], metric, item["metric_label"], manifest, artifacts, store, registry)
        prior = _measurement(item["prior"], metric, item["metric_label"], manifest, artifacts, store, registry)
        expectation = _measurement(item["expectation"], metric, item["metric_label"], manifest, artifacts, store, registry, expectation=True)
        action = None
        if item["action"] is not None:
            fields(item["action"], {"version_id", "quote", "value"}, label="economic source action")
            source = _support({key: item["action"][key] for key in ("version_id", "quote")}, manifest, artifacts, store, registry)
            value = item["action"]["value"]
            require(value in ACTIONS and item["metric_label"] in source["quote"]
                    and any(word.lower() in source["quote"].lower() for word in ACTIONS[value])
                    and re.search(r"未|没有|不再|并非|not\b|never\b", source["quote"], re.I) is None,
                    "Economic action must be affirmative source language, not analyst sentiment")
            action = {**source, "value": value}
        require(current is not None or action is not None, "Economic observation needs measured actual or source action")
        actual = current or action
        require(actual["version_id"] in event["version_ids"] and actual["version_id"] in fact_versions,
                "Economic actual must belong to a source-reported fact in its event")
        typed_transition = current is not None and "value_role" in current
        if typed_transition:
            require(prior is not None and current["value_role"] == "after" and prior.get("value_role") == "before"
                    and all(current[key] == prior[key] for key in ("version_id", "source_id", "quote", "unit",
                        "source_unit", "measurement_type", "period_label", "period_end", "source_transition")),
                    "Current/prior transition needs the same source clause and explicit after/before roles")
        elif prior is not None:
            require("value_role" not in prior, "A transition prior needs its matching after-role current")
            require(current is not None and prior["unit"] == current["unit"] and prior["measurement_type"] == current["measurement_type"]
                    and prior["period_end"] < current["period_end"] and _period_type(prior["period_label"]) == _period_type(current["period_label"]),
                    "Prior needs same metric/unit and earlier source calendar period")
        if expectation is not None:
            require(current is not None and expectation["unit"] == current["unit"] and expectation["measurement_type"] == current["measurement_type"]
                    and expectation["period_end"] == current["period_end"] and _period_type(expectation["period_label"]) == _period_type(current["period_label"]),
                    "Expectation needs same metric/unit/current calendar period")
            release_start, _ = _publication_bounds(current["published_at"])
            _, expected_end = _publication_bounds(expectation["published_at"])
            require(expected_end < release_start and instant(expectation["known_at"]) < release_start,
                    "Expectation must be published and genuinely captured before actual release")
        delta = (current["value"]-prior["value"])/abs(prior["value"]) if prior and prior["value"] != 0 else None
        surprise = (current["value"]-expectation["value"])/abs(expectation["value"]) if expectation and expectation["value"] != 0 else None
        concept = source_concept(item, current, manifest, artifacts, store, registry)
        record = {"event_id": event["event_id"], "revision_id": event["revision_id"], "source_revision_id": event["source_revision_id"], "sector_ids": sorted(item["sector_ids"]),
                  "category": item["category"], "metric_id": metric, "metric_label": item["metric_label"], "source_concept": concept,
                  "current": current, "prior": prior, "expectation": expectation, "source_action": action,
                  "relative_delta": delta, "relative_surprise": surprise,
                  "event_at": event["event_at"], "assessed_at": known_at}
        if typed_transition:
            record["comparison_basis"] = "source_stated_transition"
        point_fields = ("version_id", "source_id", "value", "unit", "measurement_type", "period_end", "value_role", "source_transition")
        identity_fields = {"source_revision_id": event["source_revision_id"], "sector_ids": record["sector_ids"],
            "category": record["category"], "metric_id": metric,
            "source_concept": {key: concept[key] for key in ("canonical_label", "period_type", "unit")},
            "points": {name: {key: row[key] for key in point_fields if key in row} if row else None
                       for name, row in (("current", current), ("prior", prior), ("expectation", expectation), ("action", action))},
            "action_value": action["value"] if action else None}
        if typed_transition:
            identity_fields["comparison_basis"] = record["comparison_basis"]
        observation_key = fingerprint(identity_fields)
        record["observation_key"] = observation_key
        identity = fingerprint(record)
        require(identity not in identities, "Duplicate economic observation")
        identities.add(identity)
        first = store.get("news-economic-first-known", observation_key) if store else None
        if first is not None:
            original = store.get("news-economic-observation", first["observation_id"])
            first_review = store.get("news-review", first["review_id"])
            require(original is not None and original["observation_key"] == observation_key
                    and original["known_at"] == first["first_known_at"] and first_review is not None
                    and first_review["reviewed_at"] == first["first_known_at"]
                    and any(row == original for row in first_review["economic_observations"]),
                    "Economic first knowledge lacks its original committed assessment")
        availability = first["first_known_at"] if first else known_at
        require(all(instant(row["known_at"]) <= instant(availability) for row in (current, prior, expectation, action) if row),
                "Economic observation cannot precede real source availability")
        result.append({**record, "observation_id": identity, "known_at": availability,
                       "scope": "source_economic_measurements_not_market_sentiment"})
    return sorted(result, key=lambda row: row["observation_id"])


def factual_event_features(observations, sector_id, origin, *, window_days=30, events=()):
    require(type(window_days) is int and 1 <= window_days <= 365, "Frozen economic window must be 1-365 days")
    at, lower = instant(origin), instant(origin)-dt.timedelta(days=window_days)
    visible = [row for row in observations if sector_id in row["sector_ids"] and lower < instant(row["known_at"]) <= at
               and instant(row["assessed_at"]) <= at]
    by_observation = {}
    for row in visible:
        previous = by_observation.get(row["observation_key"])
        if previous is None or (instant(row["assessed_at"]), row["observation_id"]) < (instant(previous["assessed_at"]), previous["observation_id"]):
            by_observation[row["observation_key"]] = row
    rows = [by_observation[key] for key in sorted(by_observation)]
    require(len({row["observation_id"] for row in rows}) == len(rows), "Repeated economic observations in window")
    values = []
    for category in CATEGORIES:
        bucket = [row for row in rows if row["category"] == category]
        deltas = [row["relative_delta"] for row in bucket if row["relative_delta"] is not None]
        surprises = [row["relative_surprise"] for row in bucket if row["relative_surprise"] is not None]
        values.extend((len(bucket), sum(deltas)/len(deltas) if deltas else 0.0,
                       1-len(deltas)/len(bucket) if bucket else 1.0,
                       sum(surprises)/len(surprises) if surprises else 0.0,
                       1-len(surprises)/len(bucket) if bucket else 1.0,
                       sum(bool(row["source_action"] and row["source_action"]["value"] == "up") for row in bucket),
                       sum(bool(row["source_action"] and row["source_action"]["value"] == "down") for row in bucket),
                       sum(bool(row["source_action"] and row["source_action"]["value"] == "introduced") for row in bucket),
                       sum(bool(row["source_action"] and row["source_action"]["value"] == "removed") for row in bucket),
                       sum(bool(row["source_action"] and row["source_action"]["value"] == "unchanged") for row in bucket)))
    states = event_entity_states(events, rows, sector_id)
    values.extend(sum(row["status"] == state for row in states) for state in EVENT_STATES)
    require(all(math.isfinite(value) for value in values), "Economic features must be finite")
    witness = {"origin": origin, "sector_id": sector_id, "window_days": window_days, "observations": rows, "event_entity_states": states}
    return {"feature_names": list(FEATURE_NAMES), "feature_values": values, "observations": rows, "event_entity_states": states,
            "input_schema_id": INPUT_SCHEMA_ID,
            "witness_hash": fingerprint(witness), "scope": "source_economic_measurements_not_market_sentiment"}


def _period_type(period):
    period = unicodedata.normalize("NFKC", period) if period else period
    return ("date" if period and re.fullmatch(r"[0-9]{4}(?:年[0-9]{1,2}月[0-9]{1,2}日|[-年][0-9]{1,2}[-/][0-9]{1,2}日?)", period)
            else "quarter" if period and "季度" in period else "month" if period and re.search(r"月|^[0-9]{4}-[0-9]{1,2}$", period)
            else "year" if period else "policy_action")


def source_concept(item, current, manifest, artifacts, store, registry):
    """Canonical labels require a source statement linking their literal aliases."""
    literal = unicodedata.normalize("NFKC", item["metric_label"]).casefold().strip()
    supplied = item.get("concept")
    label, proofs = literal, []
    if supplied is not None:
        fields(supplied, {"canonical_label", "alias_evidence"}, label="source indicator concept")
        label = unicodedata.normalize("NFKC", supplied["canonical_label"]).casefold().strip()
        require(label and type(supplied["alias_evidence"]) is list, "Source concept and alias proofs required")
        for point in supplied["alias_evidence"]:
            proof = _support(point, manifest, artifacts, store, registry)
            text = unicodedata.normalize("NFKC", proof["quote"]).casefold()
            require(label in text and literal in text, "Indicator synonym relationship lacks original source quotation")
            proofs.append(proof)
        require(label == literal or proofs, "A new label cannot silently rename a source concept")
    period = current["period_label"] if current else None
    period_type = _period_type(period)
    return {"canonical_label": label, "period_type": period_type, "alias_evidence": proofs,
            "unit": current["unit"] if current else "source_policy_action"}


def event_entity_states(events, observations, sector_id):
    """A measured old event cannot certify the content of another event."""
    result = []
    for event in events:
        if event.get("sector_ids") and sector_id not in event["sector_ids"]:
            continue
        revision = event.get("source_revision_id", event.get("revision_id"))
        matched = [row for row in observations if row["event_id"] == event["event_id"]
                   and row.get("source_revision_id", row.get("revision_id")) == revision
                   and sector_id in row["sector_ids"]]
        status = ("source_gap" if not event.get("evidence_refs") else "measured" if any(row["current"] is not None for row in matched)
                  else "qualitative_encoded" if any(row["source_action"] is not None for row in matched) else "unsupported")
        result.append({"event_id": event["event_id"], "source_revision_id": revision, "sector_id": sector_id,
                       "status": status, "observation_ids": sorted(row["observation_id"] for row in matched),
                       "known_at": event.get("available_at", event.get("known_at")),
                       "reason": None if status in ("measured", "qualitative_encoded") else "source_event_has_no_verified_economic_encoding"})
    return sorted(result, key=lambda row: (row["event_id"], row["source_revision_id"] or ""))
