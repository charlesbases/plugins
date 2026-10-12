"""Actual, bounded Common Crawl transport with immutable source lineage."""
import copy
import datetime as dt
import hashlib
import http.client
from pathlib import Path
import re
import ssl
import time
import urllib.parse

from artifacts import Artifacts
from contracts import fields, fingerprint, instant, require, strict_json_loads, utc_now
from source_journal import SourceJournal, validate_cost
from state_store import Store
import source_fetch

SCHEMA = "archive_source_capture_v1"
SOURCE_KIND = "archive-provider-source"
POLICY_PATH = Path(__file__).resolve().parents[2]/"references/archive-provider-policy.json"


def policy():
    raw = POLICY_PATH.read_bytes()
    value = strict_json_loads(raw)
    require(value["schema_id"] == "investment_archive_provider_policy_v1"
            and value["provider_id"] == "commoncrawl" and value["numeric_qualification"] is False
            and value["historical_review_qualification"] is False, "Archive provider policy changed its source-only role")
    return raw, value


def request_value(request):
    fields(request, {"provider_id", "collection_id", "original_url", "capture_timestamp", "timeout_seconds",
        "max_source_operations", "max_total_bytes", "max_elapsed_seconds"}, {"claimed_original_receipt_ref"}, "archive capture request")
    _, rules = policy()
    require(request["provider_id"] == "commoncrawl" and re.fullmatch(rules["collection_pattern"], request["collection_id"]),
            "Archive provider or collection is not registered")
    url = urllib.parse.urlsplit(request["original_url"])
    require(url.scheme in {"http", "https"} and bool(url.hostname) and not url.username and not url.password
            and not url.fragment and url.port in (None, 80, 443) and len(request["original_url"]) <= 8192,
            "Archive original URL is not a finite original resource identity")
    stamp = request["capture_timestamp"]
    require(type(stamp) is str and re.fullmatch(r"[0-9]{14}", stamp), "Exact provider capture timestamp required")
    captured = dt.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    require(captured.strftime("%Y%m%d%H%M%S") == stamp and captured <= instant(utc_now()), "Provider capture request is invalid or future")
    require(type(request["timeout_seconds"]) in {int, float} and 0 < request["timeout_seconds"] <= 30,
            "Archive transport timeout must be positive and at most 30 seconds")
    for key in ("max_source_operations", "max_total_bytes", "max_elapsed_seconds"):
        require(type(request[key]) is int and 0 < request[key] <= 10**10, "Finite positive shared archive source budget required")
    return copy.deepcopy(request)


def index_url(request, inventory, rules):
    require(type(inventory) is list and len({row["id"] for row in inventory}) == len(inventory),
            "Provider inventory has invalid or duplicate collections")
    rows = [row for row in inventory if row["id"] == request["collection_id"]]
    require(len(rows) == 1, "Requested collection is absent from actual provider inventory")
    endpoint = rows[0]["cdx-api"]
    require(endpoint == "https://"+rules["index_host"]+"/"+request["collection_id"]+"-index",
            "Provider inventory endpoint escaped its registered exact collection")
    return endpoint+"?"+urllib.parse.urlencode({"url": request["original_url"], "output": "json", "filter": "status:200"})


def exact_row(raw, request, rules):
    rows = [strict_json_loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    require(rows and len(rows) <= 5000, "Provider index must contain bounded actual records")
    matched = [row for row in rows if row.get("url") == request["original_url"] and row.get("timestamp") == request["capture_timestamp"]]
    require(len(matched) == 1, "Actual provider index did not uniquely confirm the requested original/version")
    row = matched[0]
    require(row.get("status") == "200" and re.fullmatch(rules["filename_pattern"], row.get("filename", ""))
            and row["filename"].startswith("crawl-data/"+request["collection_id"]+"/"), "Archive record source or status is not registered")
    for key in ("offset", "length"):
        require(type(row.get(key)) is str and re.fullmatch(r"0|[1-9][0-9]*", row[key]), "Native archive range is invalid")
    require(0 < int(row["length"]) <= rules["max_compressed_range_bytes"]
            and type(row.get("digest")) is str and re.fullmatch(r"[A-Z2-7]{32}", row["digest"]),
            "Provider record has no bounded range or native SHA1 digest")
    return row


def range_url(row, rules):
    require(re.fullmatch(rules["filename_pattern"], row["filename"]), "Unregistered archive range path")
    return "https://"+rules["data_host"]+"/"+row["filename"]


def _fetch_range(url, row, rules, timeout):
    source = "archive_cc_warc_range"
    rule = rules["source_registry"]["sources"][source]
    require(source_fetch.checked_url(url, rule) == url, "Archive range URL changed")
    start, length = int(row["offset"]), int(row["length"])
    requested = "bytes="+str(start)+"-"+str(start+length-1)
    deadline = time.monotonic()+timeout
    with source_fetch._open(url, timeout, length, headers={"Range": requested}) as response:
        require(response.geturl() == url and response.status == 206, "Archive transport did not return the exact direct range")
        require(response.headers.get("Content-Encoding", "identity").lower() == "identity", "Archive range transport changed content encoding")
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range)
        require(match is not None and int(match[1]) == start and int(match[2]) == start+length-1
                and int(match[3]) > start+length-1, "Archive response range differs from the native index")
        data = response.read(length+1)
        require(len(data) == length and time.monotonic() < deadline, "Archive range is truncated, oversized or outside its clock budget")
        capture = {"registry_source_id": source, "registry_hash": fingerprint(rules["source_registry"]),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 206,
            "content_type": response.headers.get_content_type(), "content_range": content_range,
            "request_headers": {"Range": requested}, "transport": getattr(response, "transport", "HTTPS_default_certificate_validation"),
            "transport_details": getattr(response, "transport_details", {"backend": ssl.OPENSSL_VERSION}), "retrieved_at": utc_now(),
            "raw_sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return capture, data


def _capture(store, artifacts, journal, name, url, limit, rules, request, *, row=None):
    existing = next((attempt for attempt in journal.attempts if attempt.get("source_id") == name), None)
    if existing is not None:
        require(existing["url"] == url, "Archive resumed source selection changed")
        if existing["status"] == "unavailable":
            return None, existing["reason"]
        return journal.original(existing["attempt_id"])["original_ref"], None
    used = journal.refresh()
    timeout = request["timeout_seconds"]
    if (used["source_requests"]+1 > request["max_source_operations"]
            or used["bytes_charged"]+limit > request["max_total_bytes"]
            or used["elapsed_seconds"]+timeout > request["max_elapsed_seconds"]):
        return None, "archive_shared_source_budget_exhausted"
    reservation = journal.reserve(url, limit, timeout, source_id=name)
    started = time.monotonic()
    try:
        if row is None:
            fetched = source_fetch.fetch(url, name, timeout=timeout, max_bytes=limit, registry=rules["source_registry"])
            capture = {key: value for key, value in fetched.items() if key not in {"raw_bytes", "text"}}
            data = fetched["raw_bytes"]
        else:
            capture, data = _fetch_range(url, row, rules, timeout)
        original = {"capture": capture, "raw_ref": artifacts.put_bytes(data)}
        reference = artifacts.put_json(original)
        attempt = {**reservation, "source_id": name, "status": "captured", "bytes_charged": len(data),
            "elapsed_seconds": time.monotonic()-started, "original_ref": reference, "finished_at": utc_now()}
        journal.commit(attempt, {"original_ref": reference})
        return reference, None
    except (OSError, ValueError, http.client.HTTPException) as error:
        attempt = {**reservation, "source_id": name, "status": "unavailable", "bytes_charged": limit,
            "elapsed_seconds": time.monotonic()-started, "reason": str(error), "finished_at": utc_now()}
        journal.commit(attempt)
        return None, str(error)


def collect(root, plan, operation_id, request):
    request = request_value(request)
    store, artifacts = Store(root, plan), Artifacts(Store(root, plan).base)
    raw_policy, rules = policy()
    protocol_ref = {**artifacts.put_bytes(raw_policy), "media_type": "application/json"}
    import verify
    frozen = {"schema_id": SCHEMA, "payload": request, "protocol_ref": protocol_ref, "code_identity": verify.code_identity()}
    saved = store.begin(operation_id, frozen)
    if saved["status"] == "completed":
        reference = saved["result"]["capture_ref"]
        from archives.version import validate
        validate(root, plan, reference)
        return reference
    with store.lease(operation_id):
        journal = SourceJournal(store, SOURCE_KIND, operation_id, request)
        refs, unknown = {"input_inventory_ref": None, "index_ref": None, "range_ref": None}, []
        inventory_ref, reason = _capture(store, artifacts, journal, "archive_cc_inventory", rules["inventory_url"],
            rules["max_inventory_bytes"], rules, request)
        refs["input_inventory_ref"] = inventory_ref
        selected_ref = None
        if inventory_ref is None:
            unknown.append({"stage": "inventory", "reason": reason})
        else:
            try:
                inventory = strict_json_loads(artifacts.read(artifacts.read_json(inventory_ref)["raw_ref"]))
                url = index_url(request, inventory, rules)
                index_ref, reason = _capture(store, artifacts, journal, "archive_cc_index", url, rules["max_index_bytes"], rules, request)
                refs["index_ref"] = index_ref
                if index_ref is None:
                    unknown.append({"stage": "index", "reason": reason})
                else:
                    row = exact_row(artifacts.read(artifacts.read_json(index_ref)["raw_ref"]), request, rules)
                    selected_ref = artifacts.put_json(row)
                    range_ref, reason = _capture(store, artifacts, journal, "archive_cc_warc_range", range_url(row, rules),
                        int(row["length"]), rules, request, row=row)
                    refs["range_ref"] = range_ref
                    if range_ref is None:
                        unknown.append({"stage": "range", "reason": reason})
            except (ValueError, KeyError, TypeError) as error:
                unknown.append({"stage": "provider_record", "reason": str(error)})
        payload_ref, http_ref = None, None
        if refs["range_ref"] is not None:
            from archives.version import parse_warc
            try:
                range_raw = artifacts.read(artifacts.read_json(refs["range_ref"])["raw_ref"])
                _, payload, http = parse_warc(range_raw, artifacts.read_json(selected_ref), rules)
                payload_ref, http_ref = artifacts.put_bytes(payload), artifacts.put_bytes(http)
            except (ValueError, KeyError, TypeError, OSError, EOFError) as error:
                unknown.append({"stage": "archive_version", "reason": str(error)})
        consumed = validate_cost(store, SOURCE_KIND, operation_id, journal.attempts, journal.reservations)
        value = {"schema_id": SCHEMA, "operation_id": operation_id, "request_ref": artifacts.put_json(request),
            "protocol_ref": protocol_ref, "provider_registry_ref": artifacts.put_json(rules["source_registry"]),
            **refs, "exact_index_row_ref": selected_ref, "publisher_payload_ref": payload_ref, "original_http_headers_ref": http_ref,
            "source_attempts_ref": artifacts.put_json(journal.attempts), "source_reservations_ref": artifacts.put_json(journal.reservations),
            "consumed": consumed, "status": "captured" if all(refs.values()) else "unknown", "unknown": unknown,
            "claimed_original_receipt_ref": request.get("claimed_original_receipt_ref"),
            "numeric_qualification": False, "historical_review_qualification": False}
        reference = artifacts.put_json(value)
        require(verify.code_identity() == frozen["code_identity"] and POLICY_PATH.read_bytes() == raw_policy,
                "Archive implementation or original provider policy changed during acquisition")
        store.complete(operation_id, {"capture_ref": reference}, writes=[
            {"kind": "archive-capture", "key": operation_id, "value": {"capture_ref": reference}, "immutable": True}])
    from archives.version import validate
    validate(root, plan, reference)
    return reference
