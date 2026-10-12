"""Bounded CSRC daily filing acquisition with raw, independently replayable pages.

The complete requested share inventory is frozen before collection. A batch
limit never changes that inventory or makes a partial query complete. Plaintext
is available only as an explicitly named quarantine mode; it cannot grant a
historical availability time or enter the financial source registry.
"""

import datetime as dt
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

from contracts import canonical_bytes, fingerprint
from file_io import durable_replace, read_artifact_json, write_artifact_json
import source_fetch
import csrc_fund_vintages as vintages


PROTOCOL = "csrc_daily_filing_acquisition_v1"
HTTPS = "https_verified"
QUARANTINE = "http_unauthed_quarantine"
SOURCE_ID = "csrc_daily_filings"
MAX_REQUESTS = 256
MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 180
PAGE_SIZE = 20  # The original public table's page size, not a fund coverage cap.


def registry():
    return {"schema_version": 1, "registry_id": PROTOCOL, "sources": {
        SOURCE_ID: {"purpose": "provider_declared_fund_NAV_versions",
                    "allowed_hosts": [vintages.SOURCE_HOST], "redirect_hosts": [],
                    "path_pattern": "^" + vintages.SOURCE_PATH.replace(".", "\\.") + "$",
                    "default_urls": [], "encoding": "utf-8",
                    "media_types": ["text/html", "application/json", "text/plain"],
                    "timezone": "Asia/Shanghai"}}}


def query_url(fund_code, start, end, offset=0, *, mode=HTTPS):
    vintages._code(fund_code, "query fund code")
    vintages._require(vintages._day(start, "query start") <= vintages._day(end, "query end"),
                      "Inverted daily query date range")
    vintages._require(type(offset) is int and offset >= 0, "Invalid original page offset")
    vintages._require(mode in (HTTPS, QUARANTINE), "Unknown CSRC transport policy")
    values = [("sEcho", offset // PAGE_SIZE + 1), ("iColumns", 7),
              ("iDisplayStart", offset), ("iDisplayLength", PAGE_SIZE),
              ("sSearch", ""), ("bRegex", "false"), ("iSortingCols", 0),
              ("fundType", ""), ("reportTypeCode", "FB040"), ("reportYear", ""),
              ("fundCompanyShortName", ""), ("fundCode", fund_code),
              ("fundShortName", ""), ("startUploadDate", start), ("endUploadDate", end)]
    encoded = json.dumps([{"name": name, "value": value} for name, value in values],
                         ensure_ascii=True, separators=(",", ":"))
    scheme = "https" if mode == HTTPS else "http"
    return scheme + "://" + vintages.SOURCE_HOST + vintages.SOURCE_PATH + "?" + urllib.parse.urlencode({"aoData": encoded})


def _owned(root, relative):
    vintages._require(isinstance(relative, str) and relative and "\\" not in relative,
                      "Invalid provider archive path")
    path = Path(relative)
    vintages._require(not path.is_absolute() and all(part not in (".", "..") for part in path.parts),
                      "Provider archive path escapes its owner")
    current = Path(root).resolve()
    for part in path.parts:
        current = current / part
        vintages._require(not current.is_symlink() and current.resolve() == current,
                          "Provider archive path is linked")
    vintages._require(current.is_relative_to(Path(root).resolve()), "Provider archive path escapes its owner")
    return current


def _raw_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    vintages._require(not temporary.is_symlink(), "Linked temporary archive")
    with temporary.open("wb") as stream:
        stream.write(raw)
    durable_replace(temporary, path)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url, *, timeout, max_bytes, mode):
    if mode == HTTPS:
        return source_fetch.fetch(url, SOURCE_ID, timeout=timeout, max_bytes=max_bytes, registry=registry())
    # This separate client intentionally does not call the trusted HTTPS fetcher
    # or invent a TLS receipt. Its raw response remains quarantined.
    opener = urllib.request.build_opener(_NoRedirect())
    started = time.monotonic()
    headers = {"User-Agent": "InvestmentResearch/0.1.0", "Accept-Encoding": "identity"}
    with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        vintages._require(response.status == 200 and response.url == url, "Unreviewed daily source redirect/status")
        vintages._require(response.headers.get("Content-Encoding", "identity").lower() == "identity",
                          "Unreviewed daily source content encoding")
        raw = response.read(max_bytes + 1)
        content_type = response.headers.get_content_type()
        text, encoding = source_fetch.decode(raw, content_type, response.headers.get_content_charset(),
                                             registry()["sources"][SOURCE_ID])
    vintages._require(time.monotonic() - started <= timeout and 0 < len(raw) <= max_bytes,
                      "Plaintext source exceeded its reserved acquisition allowance")
    return {"requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": content_type, "encoding": encoding, "transport": "HTTP_plaintext",
            "transport_details": {"backend": "urllib.request", "tls_verified": False},
            "request_headers": headers,
            "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes": raw, "text": text}


def _scope(codes, start, end, mode, limits):
    checked = vintages._requested(codes, "Asia/Shanghai")
    query_url(checked[0], start, end, mode=mode)
    return {"protocol": PROTOCOL, "requested_codes": checked, "start": start, "end": end,
            "transport_policy": mode, "page_size": PAGE_SIZE,
            "transport_registry_sha256": fingerprint(registry()), "limits": limits,
            "query_clock_semantics": "original_UI_startUploadDate_endUploadDate_fields",
            "source_echo_contract": "response_sEcho_equals_request_plus_one",
            "provider_publication_precision": "day", "provider_timezone": "Asia/Shanghai"}


def _limits(max_requests, max_bytes, max_seconds):
    vintages._require(type(max_requests) is int and max_requests > 0, "Positive total request limit required")
    vintages._require(type(max_bytes) is int and max_bytes > 0, "Positive total source byte limit required")
    vintages._require(type(max_seconds) in (int, float) and math.isfinite(max_seconds) and max_seconds > 0,
                      "Positive finite total elapsed limit required")
    return {"max_requests": max_requests, "max_bytes": max_bytes, "max_seconds": max_seconds}


def _capture_check(receipt, mode):
    if mode == HTTPS:
        source_fetch.validate_capture_provenance(receipt, registry())
    else:
        vintages._require(receipt.get("transport") == "HTTP_plaintext"
                          and receipt.get("transport_details", {}).get("tls_verified") is False
                          and receipt.get("request_headers") == {"User-Agent": "InvestmentResearch/0.1.0", "Accept-Encoding": "identity"},
                          "Quarantined source must retain its plaintext transport")


def _replay(root, scope, pages, state=None):
    """Reconstruct pagination and every version directly from original pages."""
    if state is None:
        queries = {code: {"offset": 0, "total": None, "row_ids": set(), "complete": False,
                          "price_rows": 0, "requested_price_rows": 0} for code in scope["requested_codes"]}
        observed, candidates, associations = [], [], vintages.VersionAssociations()
    else:
        queries, observed, candidates, associations = state
    for saved in pages:
        vintages._require(set(saved) == {"query_code", "offset", "raw_path", "receipt_path", "candidate_path"},
                          "Changed provider page descriptor")
        code = saved["query_code"]
        vintages._require(code in queries, "Provider page belongs to another inventory")
        query = queries[code]
        vintages._require(saved["offset"] == query["offset"] and not query["complete"],
                          "Provider pagination is missing, repeated or out of order")
        raw = _owned(root, saved["raw_path"]).read_bytes()
        receipt = read_artifact_json(_owned(root, saved["receipt_path"]))
        value = read_artifact_json(_owned(root, saved["candidate_path"]))
        expected_url = query_url(code, scope["start"], scope["end"], saved["offset"], mode=scope["transport_policy"])
        vintages._require(receipt.get("requested_url") == expected_url, "Provider query differs from frozen source scope")
        _capture_check(receipt, scope["transport_policy"])
        vintages.validate_vintages(value, raw, receipt, requested_codes=scope["requested_codes"],
                                  source_timezone_contract="Asia/Shanghai")
        associations.update(value["observed_versions"])
        page = vintages._page(raw, receipt["encoding"])
        # This provider increments the public DataTables request counter. Both
        # first and later actual pages were checked; it is not an exact echo.
        vintages._require(str(page.get("sEcho")) == str(saved["offset"] // PAGE_SIZE + 2),
                          "Provider response does not echo its requested page")
        total, count = page["iTotalDisplayRecords"], len(page["aaData"])
        vintages._require(count <= PAGE_SIZE and saved["offset"] + count <= total,
                          "Provider page count differs from its original query")
        if query["total"] is None:
            query["total"] = total
        vintages._require(query["total"] == total, "Provider inventory changed during pagination; restart its source snapshot")
        vintages._require(count > 0 or total == 0, "Provider returned an empty page before its inventory ended")
        for row in page["aaData"]:
            row_id = vintages._identifier(row.get("idStr"), "pagination row idStr")
            vintages._require(row_id not in query["row_ids"], "Provider pagination repeats an original row entity")
            query["row_ids"].add(row_id)
        query["offset"] += count
        query["complete"] = query["offset"] == total
        query["price_rows"] += len(value["observed_versions"])
        query["requested_price_rows"] += len(value["records"])
        observed.extend({"page": saved, "capture": receipt, "record": record} for record in value["observed_versions"])
        candidates.extend({"page": saved, "capture": receipt, "record": record} for record in value["records"])
    return queries, observed, candidates, associations


def validate_archive(root, value=None):
    """Replay a provider archive; this never grants financial qualification."""
    root = Path(root).resolve()
    value = read_artifact_json(_owned(root, "archive.json")) if value is None else value
    vintages._require(set(value) == {"schema_version", "scope", "scope_sha256", "pages", "attempts"},
                      "Unknown provider archive fields")
    scope = value["scope"]
    expected = _scope(scope["requested_codes"], scope["start"], scope["end"], scope["transport_policy"],
                      _limits(**scope["limits"]))
    vintages._require(scope == expected and value["scope_sha256"] == fingerprint(scope)
                      and type(value["schema_version"]) is int and value["schema_version"] == 1,
                      "Provider archive source scope changed")
    queries, observed, candidates, _ = _replay(root, scope, value["pages"])
    successful = [attempt for attempt in value["attempts"] if attempt["status"] == "completed"]
    vintages._require(len(successful) == len(value["pages"]), "Provider raw pages differ from successful acquisition attempts")
    for attempt, page in zip(successful, value["pages"]):
        vintages._require(attempt["query_code"] == page["query_code"] and attempt["offset"] == page["offset"],
                          "Provider attempt/source page association changed")
        receipt = read_artifact_json(_owned(root, page["receipt_path"]))
        vintages._require(attempt["charged_bytes"] == receipt["bytes"], "Provider attempt byte consumption changed")
    consumed = {"requests": 0, "bytes": 0, "seconds": 0}
    limits = scope["limits"]
    for attempt in value["attempts"]:
        vintages._require(isinstance(attempt, dict) and attempt["status"] in ("reserved", "completed", "failed")
                          and attempt["query_code"] in queries and type(attempt["offset"]) is int and attempt["offset"] >= 0
                          and type(attempt["charged_bytes"]) is int and attempt["charged_bytes"] >= 0
                          and type(attempt["charged_seconds"]) in (int, float)
                          and math.isfinite(attempt["charged_seconds"]) and attempt["charged_seconds"] >= 0,
                          "Invalid provider acquisition consumption")
        fields = {"query_code", "offset", "status", "charged_bytes", "charged_seconds"}
        if attempt["status"] == "failed":
            fields.add("error")
            vintages._require(isinstance(attempt["error"], str) and attempt["error"], "Missing original acquisition failure")
        vintages._require(set(attempt) == fields, "Unexpected provider acquisition receipt fields")
        allowance = min(vintages.MAX_BYTES, limits["max_bytes"] - consumed["bytes"])
        remaining_seconds = limits["max_seconds"] - consumed["seconds"]
        vintages._require(consumed["requests"] < limits["max_requests"] and allowance > 0 and remaining_seconds > 0,
                          "Provider attempt exceeded its frozen total resource budget")
        vintages._require(attempt["charged_bytes"] <= allowance
                          and attempt["charged_seconds"] <= min(30, remaining_seconds),
                          "Provider acquisition consumption exceeds its original allowance")
        if attempt["status"] in ("reserved", "failed"):
            vintages._require(attempt["charged_bytes"] == allowance and attempt["charged_seconds"] > 0,
                              "Unknown response consumption must retain its full byte reservation")
        consumed["requests"] += 1
        consumed["bytes"] += attempt["charged_bytes"]
        consumed["seconds"] += attempt["charged_seconds"]
    complete = all(query["complete"] for query in queries.values())
    authenticated = scope["transport_policy"] == HTTPS and bool(value["pages"])
    remaining_contracts = ([] if authenticated else ["source_channel_authentication"]) + [
        "provider_version_contract_admission", "currency_and_share_class_identity", "distributions_and_corporate_actions",
        "tt_transactions_fees_and_status", "complete_historical_paths", "model_training_and_independent_calibration"]
    last_failure = value["attempts"][-1].get("error") if value["attempts"] and value["attempts"][-1]["status"] == "failed" else None
    return {"protocol": PROTOCOL, "scope_sha256": value["scope_sha256"], "complete_query_inventory": complete,
            "requested_codes": scope["requested_codes"], "query_coverage": {
                code: {key: item for key, item in query.items() if key != "row_ids"} for code, query in queries.items()},
            "consumed": consumed, "observed_versions": observed, "candidates": candidates,
            "source_transport_authenticated": authenticated, "last_acquisition_failure": last_failure,
            "financial_eligibility_granted": False, "strict_historical_eligibility_granted": False,
            "remaining_contracts": remaining_contracts}


def acquire(root, *, requested_codes, start, end, mode=HTTPS, max_requests=MAX_REQUESTS,
            max_bytes=MAX_BYTES, max_seconds=MAX_SECONDS, timeout=20, fetcher=None, owner_check=None):
    """Acquire/resume the same frozen complete query inventory and total budget.

    A failed or interrupted request is charged its full reserved byte/time
    allowance. Calling again cannot reset that budget. The caller can start a
    new source operation when its complete-scope resource plan requires one.
    """
    root_path = Path(root)
    vintages._require(not root_path.is_symlink(), "Provider archive owner cannot be linked")
    root = root_path.resolve()
    scope = _scope(requested_codes, start, end, mode, _limits(max_requests, max_bytes, max_seconds))
    vintages._require(type(timeout) in (int, float) and 0 < timeout <= 30, "Bounded source timeout required")
    if owner_check:
        owner_check()
    root.mkdir(parents=True, exist_ok=True)
    archive_path = _owned(root, "archive.json")
    if archive_path.exists():
        value = read_artifact_json(archive_path)
        vintages._require(value.get("scope") == scope, "Provider acquisition scope or total budget changed")
    else:
        value = {"schema_version": 1, "scope": scope, "scope_sha256": fingerprint(scope), "pages": [], "attempts": []}
        write_artifact_json(archive_path, value)
    replay = validate_archive(root, value)
    state = _replay(root, scope, value["pages"])
    fetcher = _fetch if fetcher is None else fetcher
    failure = value["attempts"][-1].get("error") if value["attempts"] and value["attempts"][-1]["status"] == "failed" else None
    while not replay["complete_query_inventory"] and not failure:
        remaining = {"requests": max_requests - replay["consumed"]["requests"],
                     "bytes": max_bytes - replay["consumed"]["bytes"],
                     "seconds": max_seconds - replay["consumed"]["seconds"]}
        if min(remaining.values()) <= 0:
            break
        code = next(code for code in scope["requested_codes"] if not replay["query_coverage"][code]["complete"])
        offset = replay["query_coverage"][code]["offset"]
        allowance, source_timeout = min(vintages.MAX_BYTES, remaining["bytes"]), min(timeout, remaining["seconds"])
        attempt = {"query_code": code, "offset": offset, "status": "reserved",
                   "charged_bytes": allowance, "charged_seconds": source_timeout}
        value["attempts"].append(attempt)
        if owner_check:
            owner_check()
        write_artifact_json(archive_path, value)
        started = time.monotonic()
        try:
            url = query_url(code, start, end, offset, mode=mode)
            captured = fetcher(url, timeout=source_timeout, max_bytes=allowance, mode=mode)
            raw = captured["raw_bytes"]
            receipt = {key: item for key, item in captured.items() if key not in ("raw_bytes", "text")}
            _capture_check(receipt, mode)
            vintages._require(receipt.get("requested_url") == url and len(raw) <= allowance
                              and time.monotonic() - started <= source_timeout, "Provider source exceeded frozen request identity/budget")
            candidate = vintages.extract_vintages(raw, receipt, requested_codes=scope["requested_codes"],
                                                 source_timezone_contract="Asia/Shanghai")
            stem = "pages/" + code + "-" + str(offset).zfill(9)
            saved = {"query_code": code, "offset": offset, "raw_path": stem + ".raw",
                     "receipt_path": stem + ".receipt.json", "candidate_path": stem + ".candidate.json"}
            if owner_check:
                owner_check()
            _raw_write(_owned(root, saved["raw_path"]), raw)
            write_artifact_json(_owned(root, saved["receipt_path"]), receipt)
            write_artifact_json(_owned(root, saved["candidate_path"]), candidate)
            # Independent raw replay runs before this page enters the archive.
            state = _replay(root, scope, [saved], state)
            vintages._require(time.monotonic() - started <= source_timeout,
                              "Provider archival replay exceeded its reserved elapsed allowance")
            value["pages"].append(saved)
            attempt.update(status="completed", charged_bytes=len(raw), charged_seconds=time.monotonic() - started)
        except (OSError, ValueError, KeyError, TypeError) as error:
            attempt.update(status="failed", error=type(error).__name__ + ": " + str(error)[:1000])
            failure = attempt["error"]
        if owner_check:
            owner_check()
        write_artifact_json(archive_path, value)
        replay["consumed"] = {"requests": len(value["attempts"]),
                              "bytes": sum(item["charged_bytes"] for item in value["attempts"]),
                              "seconds": sum(item["charged_seconds"] for item in value["attempts"])}
        replay["complete_query_inventory"] = all(query["complete"] for query in state[0].values())
        replay["query_coverage"] = {code: {key: item for key, item in query.items() if key != "row_ids"}
                                    for code, query in state[0].items()}
        if failure:
            break
    replay = validate_archive(root, value)
    return {**replay, "status": "source_query_inventory_complete" if replay["complete_query_inventory"] else
            ("source_acquisition_failed" if failure else "source_acquisition_budget_exhausted"), "failure": failure}
