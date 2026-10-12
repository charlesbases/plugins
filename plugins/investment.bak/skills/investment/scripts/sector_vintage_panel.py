"""Automatic full-origin ALFRED panels bound to verified economic series.

The plan is derived from all supplied, source-identified series contracts and
the complete declared training/date domain before any values are fetched. It
never selects a small successful series/date subset as a complete panel.
"""
import datetime as dt
import hashlib
import time
import math
from pathlib import Path

from contracts import fingerprint, instant, require
import alfred_vintages
import source_fetch

MAX_REQUESTS = 256
MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 180
MAX_SOURCE_ATTEMPTS = 3
BATCH_REQUESTS = 256


def plan(contracts, spec, decision_at):
    decision = instant(decision_at)
    scopes, requests = [], {}
    training = spec.get("industry", {}).get("training", spec.get("training", {}))
    window = training.get("train_window_days")
    require(window is None or (type(window) is int and window > 0), "Invalid declared whole-origin industry training window")
    for contract in contracts:
        if contract.get("quote_source_id") not in ("fred_series_csv", alfred_vintages.SOURCE_ID):
            continue
        upper = min(dt.date.fromisoformat(contract["end"]), decision.date())
        lower = dt.date.fromisoformat(contract["start"])
        if window is not None:
            lower = max(lower, upper - dt.timedelta(days=window))
        require(lower <= upper, "Economic series has no declared original training domain")
        origins = [(lower + dt.timedelta(days=i)).isoformat() for i in range((upper - lower).days + 1)]
        # The UTC+2-day availability bound of this as-of day precedes the
        # complete origin day even at the earliest civil timezone UTC+14.
        dates = sorted({(dt.date.fromisoformat(origin) - dt.timedelta(days=3)).isoformat() for origin in origins})
        if contract.get("vintage_date"):
            dates = sorted(set(dates + [contract["vintage_date"]]))
        for day in dates:
            url = alfred_vintages.quote_url(contract["series_id"], day)
            requests[url] = {"series_id": contract["series_id"], "vintage_date": day, "url": url}
        scopes.append({"sector_id": contract["sector_id"], "series_id": contract["series_id"],
                       "origin_dates": origins, "vintage_dates": dates,
                       "semantic_contract_sha256": fingerprint(contract)})
    return {"schema_version": 1, "kind": "all_origin_ALFRED_panel", "decision_date": decision.date().isoformat(),
            "scopes": scopes, "requests": [requests[url] for url in sorted(requests)],
            "registry_sha256": fingerprint(source_fetch.load_registry()),
            "source_adapter_sha256": hashlib.sha256(Path(alfred_vintages.__file__).read_bytes()).hexdigest(),
            "vintage_rule": "origin_day_minus_3; provider_day_bound_before_origin_in_all_civil_timezones",
            "origin_domain_basis": "declared_training_window_and_source_interval" if window is not None else "complete_declared_source_interval_no_model_window_invented",
            "limits": {"max_requests": max(MAX_REQUESTS, len(requests) * MAX_SOURCE_ATTEMPTS),
                       "max_bytes": max(MAX_BYTES, len(requests) * MAX_SOURCE_ATTEMPTS * alfred_vintages.MAX_BYTES),
                       "max_seconds": max(MAX_SECONDS, len(requests) * MAX_SOURCE_ATTEMPTS * 12)},
            "batch_requests": BATCH_REQUESTS, "max_source_attempts": MAX_SOURCE_ATTEMPTS}


def _read(reference, item, artifacts):
    capture = artifacts.read_json(reference)
    raw = artifacts.read(capture["raw_ref"])
    version = alfred_vintages.extract(raw, capture, series_id=item["series_id"], as_of_date=item["vintage_date"])
    return capture, version


def _batch(scope, store, artifacts, analysis_id):
    """Capture all planned vintages under one stable analysis/source budget."""
    store.assert_owned()
    scope_hash = fingerprint(scope)
    key = fingerprint({"analysis_id": analysis_id, "scope_hash": scope_hash})
    ledger = store.get("sector-vintage-budget", key) or {"scope_hash": scope_hash, "attempts": []}
    require(ledger["scope_hash"] == scope_hash, "Original vintage panel acquisition scope changed")
    captures, required, pending = {}, [], []
    consumed = {"requests": len(ledger["attempts"]),
                "bytes": sum(row["charged_bytes"] for row in ledger["attempts"]),
                "seconds": sum(row["charged_seconds"] for row in ledger["attempts"])}
    limits = scope["limits"]
    attempts_by_url = {}
    for row in ledger["attempts"]:
        attempts_by_url[row["url"]] = attempts_by_url.get(row["url"], 0) + 1
    batch_used = 0
    for item in scope["requests"]:
        cache_key = fingerprint({"url": item["url"], "registry": scope["registry_sha256"],
                                 "adapter": scope["source_adapter_sha256"]})
        saved = store.get("sector-vintage-capture", cache_key)
        if saved:
            _read(saved["capture_ref"], item, artifacts)
            captures[item["url"]] = saved["capture_ref"]
            continue
        if attempts_by_url.get(item["url"], 0) >= scope["max_source_attempts"]:
            pending.append(item)
            continue
        if batch_used >= scope["batch_requests"]:
            pending.append(item)
            continue
        remaining = {"requests": limits["max_requests"] - consumed["requests"],
                     "bytes": limits["max_bytes"] - consumed["bytes"],
                     "seconds": limits["max_seconds"] - consumed["seconds"]}
        if min(remaining.values()) <= 0:
            pending.append(item)
            continue
        allowance = min(alfred_vintages.MAX_BYTES, remaining["bytes"])
        timeout = min(12, remaining["seconds"])
        attempt = {"url": item["url"], "status": "reserved", "charged_bytes": allowance, "charged_seconds": timeout}
        ledger["attempts"].append(attempt)
        attempts_by_url[item["url"]] = attempts_by_url.get(item["url"], 0) + 1
        batch_used += 1
        store.put("sector-vintage-budget", key, ledger, immutable=False)
        started = time.monotonic()
        try:
            result = alfred_vintages.fetch(item["series_id"], item["vintage_date"], timeout=timeout, max_bytes=allowance)
            capture = result["capture"]
            metadata = {field: value for field, value in capture.items() if field not in ("raw_bytes", "text")}
            reference = artifacts.put_json({**metadata, "raw_ref": artifacts.put_bytes(capture["raw_bytes"])})
            _read(reference, item, artifacts)
            require(time.monotonic() - started <= timeout, "Original vintage capture/replay exceeded elapsed allowance")
            store.put("sector-vintage-capture", cache_key, {"capture_ref": reference})
            captures[item["url"]] = reference
            attempt.update(status="completed", charged_bytes=capture["bytes"], charged_seconds=time.monotonic() - started)
        except (OSError, ValueError) as error:
            attempt.update(status="failed", reason=type(error).__name__ + ": " + str(error)[:1000])
            pending.append(item)
        store.put("sector-vintage-budget", key, ledger, immutable=False)
        consumed = {"requests": len(ledger["attempts"]),
                    "bytes": sum(row["charged_bytes"] for row in ledger["attempts"]),
                    "seconds": sum(row["charged_seconds"] for row in ledger["attempts"])}
    if pending:
        required.append({"action": "complete_original_economic_vintage_panel", "scope_hash": scope_hash,
                         "requested_unique_series_vintages": len(scope["requests"]), "pending_unique_series_vintages": len(pending),
                         "reason": "declared_full_origin_scope_has_unavailable_or_unfetched_originals",
                         "acceptance": "every requested original version is authenticated and independently replayed; no subset closure"})
    return {"schema_version": 1, "scope_hash": scope_hash, "scope_ref": artifacts.put_json(scope),
            "captures": captures, "pending": pending, "consumed": consumed, "ledger_ref": artifacts.put_json(ledger),
            "status": "complete" if not pending else "partial", "required_actions": required,
            "financial_fund_NAV_or_cash_claimed": False}


def collect(scope, store, artifacts, analysis_id):
    """Continue real batches automatically; all batches share the frozen ledger."""
    previous_consumed = None
    while True:
        result = _batch(scope, store, artifacts, analysis_id)
        validate(result, scope, artifacts)
        if result["status"] == "complete":
            return result
        consumed = result["consumed"]
        ledger = artifacts.read_json(result["ledger_ref"])
        counts = {}
        for attempt in ledger["attempts"]:
            counts[attempt["url"]] = counts.get(attempt["url"], 0) + 1
        can_retry = any(counts.get(item["url"], 0) < scope["max_source_attempts"] for item in result["pending"])
        exhausted = any(consumed[name] >= scope["limits"]["max_" + name] for name in consumed)
        if exhausted or not can_retry or consumed == previous_consumed:
            return result
        previous_consumed = dict(consumed)


def validate(value, expected_scope, artifacts):
    require(value["scope_hash"] == fingerprint(expected_scope) and artifacts.read_json(value["scope_ref"]) == expected_scope,
            "Original economic panel scope changed")
    requested = {item["url"]: item for item in expected_scope["requests"]}
    require(set(value["captures"]) <= set(requested), "Unrequested economic vintage entered panel")
    for url, reference in value["captures"].items():
        _read(reference, requested[url], artifacts)
    missing = [requested[url] for url in requested if url not in value["captures"]]
    require(missing == value["pending"] and value["status"] == ("partial" if missing else "complete")
            and value["financial_fund_NAV_or_cash_claimed"] is False, "Economic panel falsely closes or grants fund eligibility")
    ledger = artifacts.read_json(value["ledger_ref"])
    require(ledger["scope_hash"] == value["scope_hash"], "Economic panel budget scope changed")
    consumed = {"requests": len(ledger["attempts"]), "bytes": sum(row["charged_bytes"] for row in ledger["attempts"]),
                "seconds": sum(row["charged_seconds"] for row in ledger["attempts"])}
    counts, used_bytes, used_seconds = {}, 0, 0
    for attempt in ledger["attempts"]:
        url = attempt["url"]
        require(url in requested and attempt["status"] in ("completed", "failed", "reserved")
                and type(attempt["charged_bytes"]) is int and attempt["charged_bytes"] >= 0
                and type(attempt["charged_seconds"]) in (int, float)
                and math.isfinite(attempt["charged_seconds"]) and attempt["charged_seconds"] >= 0,
                "Malformed original panel source attempt")
        counts[url] = counts.get(url, 0) + 1
        require(counts[url] <= expected_scope["max_source_attempts"], "Panel source retry allowance changed")
        allowance = min(alfred_vintages.MAX_BYTES, expected_scope["limits"]["max_bytes"] - used_bytes)
        source_seconds = min(12, expected_scope["limits"]["max_seconds"] - used_seconds)
        require(0 <= attempt["charged_bytes"] <= allowance and 0 <= attempt["charged_seconds"] <= source_seconds,
                "Original panel source allowance exceeded")
        if attempt["status"] == "completed":
            require(url in value["captures"], "Completed original vintage lost its raw capture")
            capture, _ = _read(value["captures"][url], requested[url], artifacts)
            require(attempt["charged_bytes"] == capture["bytes"], "Panel completed source byte count changed")
        else:
            require(attempt["charged_bytes"] == allowance and attempt["charged_seconds"] == source_seconds,
                    "Unknown original vintage consumption must retain its full reservation")
        used_bytes += attempt["charged_bytes"]
        used_seconds += attempt["charged_seconds"]
    require(consumed == value["consumed"] and all(consumed[name] <= expected_scope["limits"]["max_" + name]
            for name in consumed), "Economic panel budget consumption changed/exceeded")
    require(bool(value["required_actions"]) == bool(missing), "Economic panel loses full-scope required action")
    return True
