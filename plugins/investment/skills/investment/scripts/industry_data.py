"""Observed news facts and explicitly identified sector benchmark price labels.

The public daily index adapter supplies price returns, never dividend-adjusted
total returns. Historical rows first fetched today become available today.
"""
import datetime as dt
import hashlib
import math
import re
import urllib.parse
from zoneinfo import ZoneInfo

from contracts import EvidenceError, fields, fingerprint, instant, require, strict_json_loads, utc_now
import news
import source_documents
import source_fetch
import asset_domains

ADAPTER_ID = "eastmoney_nonadjusted_index_daily_v2"
SOURCE_ID = "eastmoney_index_daily"


def _day(value):
    require(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value), "Explicit benchmark ISO date required")
    return dt.date.fromisoformat(value)


def _contract(value):
    if value.get("adapter_id") == asset_domains.SERIES_ADAPTER_ID:
        return asset_domains.validate_series_contract(value)
    fields(value, {"sector_id", "thesis_ids", "benchmark_id", "index_code", "index_name", "provider_security_id",
                   "return_definition", "currency", "calendar_id", "timezone", "identity_quote", "start", "end", "benchmark_role", "asset_domain"},
           {"identity_document_ref", "identity_url", "identity_source_id", "provider_name", "pricing_lag_calendar_days", "sector_relation", "adapter_id"},
           "sector benchmark contract")
    asset_domains.validate_domain(value["asset_domain"])
    require(value["asset_domain"]["return_target"]["transform"] == "price_log_return", "Daily index labels require a price-return economic target")
    require(value.get("adapter_id", ADAPTER_ID) == ADAPTER_ID, "Obsolete index source adapter")
    require(all(type(value[key]) is str and value[key].strip() for key in
                ("sector_id", "benchmark_id", "index_code", "index_name", "provider_security_id", "identity_quote")),
            "Named benchmark identity and original identity quote required")
    require(type(value["thesis_ids"]) is list and value["thesis_ids"]
            and all(type(key) is str and key for key in value["thesis_ids"])
            and len(set(value["thesis_ids"])) == len(value["thesis_ids"]), "Distinct benchmark thesis bindings required")
    require(re.fullmatch(r"[01]\.[0-9]{6}", value["provider_security_id"])
            and value["provider_security_id"].split(".")[1] == value["index_code"], "Benchmark provider code differs")
    require(value["return_definition"] in ("price_return", "total_return"), "Explicit return definition required")
    require(value["return_definition"] == "price_return", "Public nonadjusted index adapter cannot supply total_return")
    require(value["benchmark_role"] in ("sector_index", "analyst_proxy"), "Explicit benchmark role required")
    if value["benchmark_role"] == "sector_index":
        require("sector_relation" in value or value["asset_domain"].get("taxonomy"), "Sector mapping needs its original publisher taxonomy")
    if "sector_relation" in value:
        fields(value["sector_relation"], {"locator", "quote", "sector_label"}, label="benchmark sector relation")
        require(all(type(item) is str and item.strip() for item in value["sector_relation"].values())
                and value["sector_relation"]["sector_label"], "Source sector label must be literal in the original definition")
    require(value["currency"] == "CNY" and value["timezone"] == "Asia/Shanghai"
            and value["calendar_id"] in ("SSE", "SZSE", "CN_A_SHARE"), "Public index adapter needs a declared mainland CNY clock")
    require(_day(value["start"]) < _day(value["end"]), "Benchmark range needs at least two distinct dates")
    require("identity_document_ref" in value or (type(value.get("identity_url")) is str
            and type(value.get("identity_source_id")) is str), "Original official benchmark identity evidence required")
    if "pricing_lag_calendar_days" in value:
        require(type(value["pricing_lag_calendar_days"]) is int and 0 <= value["pricing_lag_calendar_days"] <= 7,
                "Bounded declared pricing phase proxy required")
    return value


def _identity(contract, artifacts, at):
    document = source_documents.read_extracted_document(contract["identity_document_ref"], artifacts)
    require(source_fetch.source_rule(document["source_id"])["purpose"] in ("benchmark_identity", "asset_input_identity"),
            "Benchmark identity needs a registered official index publisher")
    if document["source_id"] == "csi_index_identity":
        filename = urllib.parse.urlsplit(document["capture"]["requested_url"]).path.rsplit("/", 1)[-1]
        require(re.fullmatch(re.escape(contract["index_code"])+r"factsheet(?:en)?\.pdf", filename),
                "Official index factsheet code differs from benchmark; constituent codes are not index identities")
    if urllib.parse.urlsplit(document["capture"]["requested_url"]).hostname == "fred.stlouisfed.org":
        require(urllib.parse.urlsplit(document["capture"]["requested_url"]).path.rstrip("/") == "/series/"+contract["series_id"],
                "FRED original metadata page identifies a different public series")
    require(instant(document["retrieved_at"]) <= instant(at), "Benchmark identity was captured after decision")
    text = " ".join(row["text"] for row in document["blocks"])
    quote = source_documents.normalize_text(contract["identity_quote"])
    identifier = contract.get("index_code", contract.get("series_id"))
    name = contract.get("index_name", contract.get("series_name"))
    require(quote in text and identifier in text and name in text,
            "Official benchmark identity quote/code/name differs from original source")
    provider_name = contract.get("provider_name", contract.get("index_name", contract.get("series_name")))
    require(type(provider_name) is str and provider_name in text, "Provider index alias needs official identity support")
    if contract.get("adapter_id") == asset_domains.SERIES_ADAPTER_ID:
        asset_domains.verify_series_identity(contract, document)
    else:
        require("人民币" in text or re.search(r"\b(CNY|RMB)\b", text), "Declared CNY denomination lacks official identity support")
    return document


def _url(contract):
    if contract.get("adapter_id") == asset_domains.SERIES_ADAPTER_ID:
        return contract["quote_url"]
    query = {"secid": contract["provider_security_id"], "fields1": "f1,f2,f3", "fields2": "f51,f52,f53,f54,f55,f56,f57",
             "klt": "101", "fqt": "0", "beg": contract["start"].replace("-", ""),
             "end": contract["end"].replace("-", ""), "lmt": "20000"}
    return "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(query)


def _sector_relation(contract, document, artifacts=None, at=None):
    """Recognize an explicit primary-index definition, never chart membership.

    The supported CSI source forms name a single sector in a definition or
    selection sentence. Other publishers/layouts require original-source work.
    """
    if document is not None and artifacts is not None:
        return asset_domains.verify_definition(contract, document, artifacts, at)
    scope = "analyst_proxy_only_no_sector_admission" if contract["benchmark_role"] == "analyst_proxy" else "unverified_sector_relation"
    declared = contract.get("sector_relation", {})
    result = {"status": "partial", "scope": scope, "sector_label": contract["sector_id"],
              "document_ref": contract.get("identity_document_ref"), "document_id": document["document_id"] if document else None,
              "locator": declared.get("locator"), "quote": declared.get("quote"),
              "known_at": document["retrieved_at"] if document else None,
              "adapter_id": "csi_single_sector_definition_v1", "reason": "analyst_proxy_is_not_a_sector_index" if scope.startswith("analyst")
                  else "original_single_sector_index_definition_not_supported"}
    if contract["benchmark_role"] == "analyst_proxy" or document is None or document["source_id"] != "csi_index_identity":
        return result
    blocks = document["blocks"]
    block = next((row for row in blocks if row["locator"] == declared["locator"]), None)
    if block is None or block.get("page") != 1 or block.get("kind") not in ("pdf_line", "pdf_page"):
        return result
    quote = source_documents.normalize_text(declared["quote"])
    text = source_documents.normalize_text(block["text"])
    if quote not in text or (block["kind"] == "pdf_line" and quote != text):
        return result
    compact = re.sub(r"\s+", "", quote)
    sector = re.sub(r"\s+", "", declared["sector_label"])
    if (re.search(r"[、/]|及|与|和|等|各|其他|全部|综合|多行业", sector)
            or re.search(r"不|未|非|除|各行业|其他行业|所有行业|多种行业|行业(?:权重)?分布|十大权重股|免责声明", compact)):
        return result
    name = re.escape(re.sub(r"\s+", "", contract["index_name"]))
    main = name + (r"(?:指数)?" if not contract["index_name"].endswith("指数") else "")
    intro = False
    complete_definition = False
    boundaries = ("全称", "指数代码", "指数走势", "收益率", "波动率", "基本面", "市值", "上市交易所权重分布",
                  "行业权重分布", "十大权重股", "成份股", "成分股", "衍生指数", "免责声明")
    if block["kind"] == "pdf_line":
        page_lines = sorted((row for row in blocks if row.get("kind") == "pdf_line" and row.get("page") == 1), key=lambda row: row["line"])
        previous = [row for row in page_lines if row["line"] < block["line"]]
        intro = bool(previous) and max(previous, key=lambda row: row["line"])["text"] in ("指数简介", "编制选样", "行业分类")
        definition_lines = []
        if intro:
            for row in page_lines:
                if row["line"] < block["line"]:
                    continue
                if any(row["text"] == boundary or row["text"].startswith(boundary+" ") for boundary in boundaries):
                    break
                definition_lines.append(row["text"])
            complete_definition = source_documents.normalize_text(" ".join(definition_lines)) == quote
    else:
        # Full page substring quotes can remove a negation or mix chart text
        # into a claimed definition. A bounded CSI preamble must reproduce
        # the whole definition, after its date/name and before its name table.
        before_table = text.split("全称", 1)[0] if "全称" in text else text
        preamble = re.sub(r"^[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日\s*", "", before_table).strip()
        alias = contract.get("provider_name", contract.get("index_name", contract.get("series_name")))
        if preamble.startswith(alias+" "):
            preamble = preamble[len(alias):].strip()
        complete_definition = preamble == quote
    if not complete_definition:
        return result
    subject = r"(?:"+main+(r"|本指数" if intro else "")+r")"
    label = re.escape(sector)
    remainder = compact.replace(re.sub(r"\s+", "", contract["index_name"]), "").replace(sector+"行业", "").replace("该行业", "")
    if "行业" in remainder:
        return result
    direct = subject+r"反映"+label+r"行业(?:公司|股票|证券)?的(?:市场|整体)表现[。.]?"
    selected = (subject+r"(?:选取|样本由|由|以)(?:[^，。；:：]{0,80})?"+label+
                r"行业(?:上市公司)?(?:证券|股票)(?:组成|作为指数样本)?[，,](?:以)?反映[^。；，]{0,80}(?:证券|股票)的整体表现[。.]?")
    if not (re.fullmatch(direct, compact) or re.fullmatch(selected, compact)):
        return result
    result.update(status="verified", scope="source_index_definition_single_sector", reason=None)
    return result


def _relation_key(contract, document, relation):
    domain = {**contract["asset_domain"]}
    if domain.get("taxonomy"):
        domain["taxonomy"] = {key: value for key, value in domain["taxonomy"].items() if key != "document_ref"}
    return fingerprint({"source_id": document["source_id"], "source_identity": contract.get("index_code", contract.get("series_id")),
                        "asset_domain": domain, "quote": source_documents.normalize_text(relation["quote"]),
                        "adapter_id": relation["adapter_id"]})


def _first_relation(contract, document, relation, artifacts, store, at):
    """Fresh document plus re-extracted committed old proof, without backfill."""
    if relation["status"] != "verified":
        return relation
    key = _relation_key(contract, document, relation)
    relation = {**relation, "semantic_hash": key, "observed_at": document["retrieved_at"],
                "first_proof_ref": contract["identity_document_ref"]}
    first = store.get("sector-relation-first-observed", key)
    if first is None:
        return relation
    prior_operation = store.get("sector-data-operation", first["producer_operation_id"])
    require(prior_operation is not None, "First sector definition has no committed producer operation")
    prior_entries = artifacts.read_json(prior_operation["entries_ref"])
    require(any(row["status"] == "captured" and row["contract"] == first["contract"] for row in prior_entries),
            "First sector definition is not bound to its original captured source")
    previous_document = _identity(first["contract"], artifacts, at)
    previous_relation = _sector_relation(first["contract"], previous_document, artifacts, at)
    require(previous_relation["status"] == "verified"
            and _relation_key(first["contract"], previous_document, previous_relation) == key
            and previous_document["retrieved_at"] == first["known_at"]
            and instant(first["known_at"]) <= instant(document["retrieved_at"]),
            "First-known sector definition differs from re-extracted original and fresh source")
    return {**relation, "known_at": first["known_at"], "first_proof_ref": first["contract"]["identity_document_ref"]}


def parse_index_capture(capture, contract):
    """Reparse nonadjusted daily closes; publication clocks are not backdated."""
    source_fetch.validate_capture_provenance(capture)
    require(capture["registry_source_id"] == SOURCE_ID, "Unexpected benchmark quote provider")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(capture["requested_url"]).query, strict_parsing=True)
    require(all(len(values) == 1 for values in query.values())
            and set(query) == {"secid", "fields1", "fields2", "klt", "fqt", "beg", "end", "lmt"}, "Unexpected daily quote query")
    require(query["secid"] == [contract["provider_security_id"]] and query["klt"] == ["101"] and query["fqt"] == ["0"]
            and query["fields1"] == ["f1,f2,f3"] and query["fields2"] == ["f51,f52,f53,f54,f55,f56,f57"]
            and query["lmt"] == ["20000"], "Daily index query changed return semantics or identity")
    start = dt.datetime.strptime(query["beg"][0], "%Y%m%d").date()
    end = dt.datetime.strptime(query["end"][0], "%Y%m%d").date()
    require(start <= end and capture["final_url"] == capture["requested_url"], "Unexpected quote redirect or reversed range")
    value = strict_json_loads(capture["text"])
    require(type(value) is dict, "Daily benchmark quote root must be an object")
    data = value.get("data")
    require(value.get("rc") == 0 and type(data) is dict and data.get("code") == contract["index_code"]
            and str(data.get("market")) == contract["provider_security_id"].split(".")[0]
            and data.get("name") == contract.get("provider_name", contract.get("index_name", contract.get("series_name"))), "Quote response benchmark identity differs")
    rows = data.get("klines")
    require(type(rows) is list and 0 < len(rows) <= 20000, "No bounded benchmark daily price history")
    observed = instant(capture["retrieved_at"])
    local = observed.astimezone(ZoneInfo(contract["timezone"]))
    result, previous = [], None
    for line in rows:
        require(type(line) is str, "Daily price row must be CSV text")
        parts = line.split(",")
        require(len(parts) == 7, "Daily price row field layout changed")
        day = _day(parts[0])
        require(start <= day <= end and (previous is None or day > previous), "Daily prices are unordered, duplicated or outside request")
        previous = day
        values = [float(item) for item in parts[1:]]
        require(all(math.isfinite(item) for item in values) and all(item > 0 for item in values[:4])
                and all(item >= 0 for item in values[4:]), "Invalid daily price or volume")
        opened, closed, high, low = values[:4]
        require(low <= min(opened, closed) <= max(opened, closed) <= high, "Daily OHLC bounds differ")
        require(day <= local.date(), "Future trading session cannot be observed")
        if day == local.date() and local.time() < dt.time(15, 0):
            continue
        result.append({"date": day.isoformat(), "close": closed, "open": opened, "high": high, "low": low,
                       "available_at": capture["retrieved_at"], "source_observed_at": capture["retrieved_at"],
                       "availability_basis": "first_verified_capture_of_this_price_version",
                       "return_definition": "price_return"})
    return result


def _read_capture(reference, contract, artifacts, at):
    capture = artifacts.read_json(reference)
    require(instant(capture["retrieved_at"]) <= instant(at), "Benchmark capture is after the decision")
    raw = artifacts.read(capture["raw_ref"])
    require(len(raw) == capture["bytes"] and hashlib.sha256(raw).hexdigest() == capture["raw_sha256"], "Benchmark raw quote changed")
    rule = source_fetch.source_rule(contract.get("quote_source_id", SOURCE_ID))
    text, encoding = source_fetch.decode(raw, capture["content_type"], capture["encoding"], rule)
    require(encoding == capture["encoding"] and hashlib.sha256(text.encode()).hexdigest() == capture["text_sha256"],
            "Benchmark decoded quote changed")
    return [{**row, "raw_ref": capture["raw_ref"], "capture_ref": reference}
            for row in (asset_domains.parse_series_capture({**capture, "text": text}, contract)
                        if contract.get("adapter_id") == asset_domains.SERIES_ADAPTER_ID else parse_index_capture({**capture, "text": text}, contract))]


def _benchmark_key(contract):
    return fingerprint({key: contract.get(key) for key in ("benchmark_id", "index_code", "provider_security_id", "series_id", "quote_source_id",
        "return_definition", "currency", "calendar_id", "timezone", "asset_domain")})


def _assemble(review, spec, entries, artifacts, store, at, history=()):
    factual = news.factual_event_features(review, spec, at, artifacts, store=store)
    sectors, actions = [], list(factual["required_actions"])
    actions += [{"action": "complete_industry_news_source_group", "source_group": name,
                 "missing_sources": row["missing_sources"]}
                for name, row in factual["news_state"]["source_group_coverage"].items() if row["missing_sources"]]
    active = set(factual["news_state"]["active_thesis_ids"])
    scopes = [factual]
    for record in history:
        require(instant(record["known_at"]) <= instant(at), "Archived observed news scope is after decision")
        archived = artifacts.read_json(record["review_ref"])
        committed = store.get("news-review", archived["review_id"])
        require(committed is not None and committed["manifest_ref"] == record["review_ref"],
                "Archived observed set has no committed source review")
        archived_spec = {"kind": "numeric_policy", "news": record["news_policy"]}
        archived_result = news.factual_event_features({**archived, "manifest_ref": record["review_ref"]},
            archived_spec, record["known_at"], artifacts, store=store)
        scopes.append(archived_result)
    bound = set()
    for entry in entries:
        contract = entry["contract"]
        sector_id = contract["sector_id"]
        bound.update(contract["thesis_ids"])
        news_scopes = []
        for scope in scopes:
            economic = [row for row in scope.get("economic_observations", []) if sector_id in row["sector_ids"]]
            economic_events = {(row["event_id"], row.get("source_revision_id", row["revision_id"])) for row in economic}
            features = [{**row, "sector_id": sector_id} for row in scope["features"]
                        if sector_id in row.get("sector_ids", [])
                        or (row["event_id"], row.get("source_revision_id", row["revision_id"])) in economic_events
                        or (scope is factual and set(row["thesis_ids"]) & set(contract["thesis_ids"]))]
            news_scopes.append({"coverage": scope["coverage"], "features": features,
                                "economic_observations": economic,
                                "identity_rule": "source_sector_label_and_source_event_revision_not_cross_day_thesis_name"})
        relevant = list({(row["event_id"], row.get("source_revision_id", row["revision_id"])): row for scope in news_scopes
                         for row in scope["features"]}.values())
        economic_observations = list({row["observation_id"]: row for scope in news_scopes
                                     for row in scope["economic_observations"]}.values())
        prices, returns, price_versions = [], [], []
        relation = _sector_relation(contract, None)
        if entry["status"] == "captured":
            _contract(contract)
            identity = _identity(contract, artifacts, at)
            relation = _first_relation(contract, identity, _sector_relation(contract, identity, artifacts, at), artifacts, store, at)
            versions = {}
            for reference in entry["capture_refs"]:
                for row in _read_capture(reference, contract, artifacts, at):
                    if contract["start"] <= row["date"] <= contract["end"]:
                        versions.setdefault(row["date"], []).append(row)
            # A later correction is selected only as of this sealed decision;
            # earlier identical price versions retain their first capture time.
            for day in sorted(versions):
                rows = sorted(versions[day], key=lambda row: (instant(row["available_at"]), fingerprint(row["capture_ref"])))
                price_versions.extend(list({fingerprint(row): row for row in rows}.values()))
                latest = rows[-1]
                earlier_same = [row for row in rows if row["close"] == latest["close"]]
                prices.append(min(earlier_same, key=lambda row: instant(row["available_at"])))
            for before, after in zip(prices, prices[1:]):
                returns.append({"date": after["date"], "previous_date": before["date"],
                                "return": (after["close"]/before["close"]-1 if contract["asset_domain"]["return_target"]["transform"] == "price_log_return" else after["close"]-before["close"]),
                                "available_at": max((before["available_at"], after["available_at"]), key=instant),
                                "return_definition": contract["return_definition"], "currency": contract["currency"]})
        else:
            actions.append({"action": "obtain_verified_sector_benchmark_source", "sector_id": sector_id,
                            "reason": entry["reason"]})
        if relation["status"] != "verified":
            actions.append({"action": "obtain_official_single_sector_index_definition" if contract["benchmark_role"] == "sector_index"
                            else "validate_analyst_proxy_in_separate_research_scope", "sector_id": sector_id,
                            "benchmark_id": contract["benchmark_id"], "reason": relation["reason"]})
        if len(prices) < 2:
            actions.append({"action": "obtain_nonempty_sector_price_history", "sector_id": sector_id})
        if not relevant and not factual["coverage"]["absence_proven"]:
            actions.append({"action": "obtain_quoted_fact_event_features", "sector_id": sector_id})
        historical = [row for row in price_versions if instant(row["available_at"]).astimezone(ZoneInfo(contract["timezone"])).date()
                      <= _day(row["date"])]
        if len(historical) < 2:
            actions.append({"action": "accumulate_archived_point_in_time_sector_captures", "sector_id": sector_id,
                            "reason": "Newly fetched old closes were not available to historical decisions"})
        sectors.append({"sector_id": sector_id, "thesis_ids": contract["thesis_ids"], "benchmark_id": contract["benchmark_id"],
                        "index_code": contract.get("index_code", contract.get("series_id")), "index_name": contract.get("index_name", contract.get("series_name")),
                        "benchmark_role": contract["benchmark_role"], "sector_relation_scope": relation["scope"],
                        "sector_relation": relation,
                        "source_id": contract.get("quote_source_id", SOURCE_ID), "adapter_id": contract.get("adapter_id", ADAPTER_ID),
                        "asset_domain": contract["asset_domain"], "input_schema_id": asset_domains.INPUT_SCHEMA_ID,
                        "return_definition": contract["return_definition"], "currency": contract["currency"],
                        "calendar_id": contract["calendar_id"], "timezone": contract["timezone"], "benchmark": contract,
                        "prices": prices, "price_versions": price_versions, "returns": returns,
                        "features": relevant, "economic_observations": economic_observations, "coverage": factual["coverage"],
                        "news_scopes": news_scopes,
                        "pricing_lag_calendar_days": contract.get("pricing_lag_calendar_days", 1),
                        "pricing_phase_scope": "declared_calendar_day_proxy_requires_fund_clock_validation"})
    for thesis_id in sorted(active-bound):
        actions.append({"action": "supply_source_verified_sector_benchmark_contract", "thesis_id": thesis_id})
    if not entries:
        actions.append({"action": "supply_source_verified_sector_benchmark_contracts"})
    return {"schema_version": 4, "kind": "sector_data", "input_schema_id": asset_domains.INPUT_SCHEMA_ID, "decision_at": at,
            "news_ref": review.get("manifest_ref"), "news_state": factual["news_state"],
            "benchmarks": [entry["contract"] for entry in entries], "sectors": sectors,
            "status": "partial" if actions else "ready", "trade_ready": False, "required_actions": actions,
            "scope": "observed_source_facts_and_declared_benchmark_price_returns"}


def prepare_sector_data(review, spec, store, artifacts, operation_id, benchmark_contracts=None):
    """Capture real registered prices and keep missing evidence explicit."""
    store.assert_owned()
    require(type(operation_id) is str and operation_id, "Stable sector data operation required")
    contracts = benchmark_contracts if benchmark_contracts is not None else spec.get("industry", {}).get("benchmark_contracts", [])
    require(type(contracts) is list and len(contracts) <= 50, "Bounded dynamic sector benchmark contracts required")
    request = {"review": review, "spec": spec, "benchmark_contracts": contracts}
    request_hash = fingerprint(request)
    existing = store.get("sector-data-operation", operation_id)
    if existing is not None:
        require(existing["request_hash"] == request_hash, "Sector data operation changed inputs")
        validate_sector_data(existing, spec, existing["decision_at"], artifacts, store=store)
        return existing
    news.validate_review(review, spec, utc_now(), artifacts, store=store)
    entries, captures, seen = [], [], set()
    for original in contracts:
        contract = _contract(dict(original))
        require(contract["sector_id"] not in seen, "Repeated sector benchmark")
        seen.add(contract["sector_id"])
        require(set(contract["thesis_ids"]) <= {row["thesis_id"] for row in review["industry_theses"]},
                "Sector benchmark refers to unknown assessed thesis")
        try:
            if "identity_document_ref" not in contract:
                require(source_fetch.source_rule(contract["identity_source_id"])["purpose"] in ("benchmark_identity", "asset_input_identity"),
                        "Official registered benchmark identity source required")
                contract["identity_document_ref"] = source_documents.capture_document(contract["identity_url"],
                    contract["identity_source_id"], artifacts, timeout=12, store=store)
            _identity(contract, artifacts, utc_now())
            capture = source_fetch.fetch(_url(contract), contract.get("quote_source_id", SOURCE_ID), timeout=12)
            store.assert_owned()
            if contract.get("adapter_id") == asset_domains.SERIES_ADAPTER_ID:
                asset_domains.parse_series_capture(capture, contract)
            else:
                parse_index_capture(capture, contract)
            metadata = {key: value for key, value in capture.items() if key not in ("raw_bytes", "text")}
            reference = artifacts.put_json({**metadata, "raw_ref": artifacts.put_bytes(capture["raw_bytes"])})
            prior = [row["capture_ref"] for _, row in store.scan("sector-price-capture")
                     if row["benchmark_key"] == _benchmark_key(contract)]
            entries.append({"contract": contract, "capture_refs": list({fingerprint(ref): ref for ref in prior+[reference]}.values()),
                            "status": "captured", "reason": None})
            captures.append({"benchmark_key": _benchmark_key(contract), "capture_ref": reference})
        except (OSError, ValueError) as error:
            entries.append({"contract": contract, "capture_refs": [], "status": "unavailable", "reason": str(error)})
    at = utc_now()
    history = [row for _, row in store.scan("sector-news-scope")
               if instant(row["known_at"]) <= instant(at) and row["news_policy"] == spec["news"]
               and row["review_ref"] != review.get("manifest_ref")]
    bundle = _assemble(review, spec, entries, artifacts, store, at, history)
    result = {"schema_version": 4, "kind": "sector_data", "operation_id": operation_id,
              "request_ref": artifacts.put_json(request), "request_hash": request_hash,
              "decision_at": at, "entries_ref": artifacts.put_json(entries), "news_history_ref": artifacts.put_json(history),
              "data_ref": artifacts.put_json(bundle),
              "bundle_hash": fingerprint(bundle), "status": bundle["status"], "trade_ready": False,
              "sector_ids": [row["sector_id"] for row in bundle["sectors"]], "required_actions": bundle["required_actions"]}
    validate_sector_data(result, spec, at, artifacts, store=store)
    with store.transaction() as conn:
        for record in captures:
            store.put("sector-price-capture", fingerprint(record), record, conn=conn)
        for entry, sector in zip(entries, bundle["sectors"]):
            relation = sector["sector_relation"]
            if relation["status"] == "verified" and store.get("sector-relation-first-observed", relation["semantic_hash"], conn=conn) is None:
                store.put("sector-relation-first-observed", relation["semantic_hash"], {
                    "contract": entry["contract"], "producer_operation_id": operation_id,
                    "known_at": relation["known_at"]}, conn=conn)
        if review.get("manifest_ref"):
            scope = {"review_ref": review["manifest_ref"], "news_policy": spec["news"], "known_at": at}
            store.put("sector-news-scope", fingerprint(scope), scope, conn=conn)
        store.put("sector-data-operation", operation_id, result, conn=conn)
    return result


def validate_sector_data(value, spec, decision_at, artifacts, *, store):
    """Independent raw reader; no serialized label, feature or readiness trust."""
    try:
        require(value.get("schema_version") == 4 and value.get("kind") == "sector_data", "Sector data schema differs")
        at = value["decision_at"]
        require(instant(at) <= instant(decision_at), "Sector data is after decision")
        request = artifacts.read_json(value["request_ref"])
        require(fingerprint(request) == value["request_hash"] and request["spec"] == spec, "Sector request or frozen policy differs")
        entries = artifacts.read_json(value["entries_ref"])
        require(len(entries) == len(request["benchmark_contracts"]), "Sector contract inventory changed")
        for original, entry in zip(request["benchmark_contracts"], entries):
            expected = dict(original)
            actual = dict(entry["contract"])
            if "identity_document_ref" not in expected:
                actual.pop("identity_document_ref", None)
            require(expected == actual, "Sector benchmark definition changed")
            require(entry["status"] in ("captured", "unavailable"), "Unknown sector acquisition state")
        history = artifacts.read_json(value["news_history_ref"])
        derived = _assemble(request["review"], spec, entries, artifacts, store, at, history)
        stored = artifacts.read_json(value["data_ref"])
        require(derived == stored and fingerprint(derived) == value["bundle_hash"], "Sector labels or fact features differ from source")
        require(value["status"] == derived["status"] and value["trade_ready"] is False
                and value["required_actions"] == derived["required_actions"]
                and value["sector_ids"] == [row["sector_id"] for row in derived["sectors"]], "Sector summary differs from independent source reading")
        return derived
    except EvidenceError:
        raise
    except (ValueError, KeyError, TypeError, OSError, UnicodeError) as error:
        raise EvidenceError("Sector data verification failed: " + str(error)) from error


def load_for_prediction(context, value):
    return validate_sector_data(value, context["spec"], context["decision_at"], context["artifacts"], store=context["store"])
