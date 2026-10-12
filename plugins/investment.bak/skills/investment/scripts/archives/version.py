"""Independent provider-version witness; no historical review/model shortcut."""
import base64
import datetime as dt
import gzip
import hashlib
import io
import re
import urllib.parse

from artifacts import Artifacts
from contracts import fields, fingerprint, instant, require, strict_json_loads, utc_now
from source_journal import validate_cost
from state_store import Store
import source_fetch
from archives import commoncrawl as cc

SCHEMA = "provider_archived_publisher_version_v1"


def _source(reference, name, expected_url, store, artifacts, rules, attempts, begin, completed, *, row=None):
    candidates = [attempt for attempt in attempts if attempt["source_id"] == name and attempt["status"] == "captured"]
    require(len(candidates) == 1, "Archive source lacks one original captured transport attempt")
    attempt = candidates[0]
    original = store.get(cc.SOURCE_KIND+"-original", attempt["attempt_id"])
    require(original == {"original_ref": reference} and attempt["original_ref"] == reference and attempt["url"] == expected_url,
            "Archive source was substituted for its immutable original transport journal")
    value = artifacts.read_json(reference)
    fields(value, {"capture", "raw_ref"}, label="actual archive source original")
    capture, raw = value["capture"], artifacts.read(value["raw_ref"])
    require(capture["registry_source_id"] == name and capture["registry_hash"] == fingerprint(rules["source_registry"])
            and capture["requested_url"] == capture["final_url"] == expected_url and capture["redirect_chain"] == []
            and capture["bytes"] == len(raw) == attempt["bytes_charged"]
            and capture["raw_sha256"] == hashlib.sha256(raw).hexdigest(),
            "Archive source identity or actual bytes differ")
    require(instant(begin) <= instant(attempt["started_at"]) <= instant(capture["retrieved_at"])
            <= instant(attempt["finished_at"]) <= instant(completed) <= instant(utc_now()),
            "Archive source clock is outside its actual reserve/capture/finish/completion sequence")
    if row is None:
        source_fetch.validate_capture_provenance(capture, rules["source_registry"])
        text, encoding = source_fetch.decode(raw, capture["content_type"], capture["encoding"], rules["source_registry"]["sources"][name])
        require(capture["http_status"] == 200 and encoding == capture["encoding"]
                and capture["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "Archive index or inventory was not its actual registered text response")
    else:
        start, length = int(row["offset"]), int(row["length"])
        require(capture["http_status"] == 206 and len(raw) == length
                and capture["request_headers"] == {"Range": "bytes="+str(start)+"-"+str(start+length-1)}
                and capture["transport"] in {"HTTPS_default_certificate_validation", "HTTPS_Windows_Schannel_certificate_validation"},
                "Archive range lacks its exact registered TLS/range request")
        details = capture.get("transport_details", {})
        if capture["transport"] == "HTTPS_Windows_Schannel_certificate_validation":
            require(details.get("backend") == "Schannel" and "Schannel" in details.get("curl_version", ""),
                    "Archive range lacks its actual native TLS backend identity")
        else:
            require(type(details.get("backend")) is str and "OpenSSL" in details["backend"],
                    "Archive range lacks its actual default TLS backend identity")
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", capture["content_range"])
        require(match is not None and int(match[1]) == start and int(match[2]) == start+length-1
                and int(match[3]) > start+length-1, "Archive range response does not correspond to the provider index")
    return capture, raw


def _headers(raw, *, status=False):
    lines = raw.split(b"\r\n")
    if status:
        require(re.fullmatch(rb"HTTP/[0-9.]+ 200(?: .*)?", lines[0]), "Archived publisher response is not an actual HTTP200")
    else:
        require(lines[0] in {b"WARC/1.0", b"WARC/1.1"}, "Archive WARC version is unsupported")
    result = {}
    for line in lines[1:]:
        require(b":" in line and not line.startswith((b" ", b"\t")), "Archive header is malformed or folded")
        name, value = line.split(b":", 1)
        key = name.decode("ascii").lower()
        # Repeated noncritical HTTP Set-Cookie/Vary can remain in raw headers;
        # identity/framing fields must be unique before their meaning is used.
        if key in result:
            critical = ({"content-length", "content-type", "content-encoding", "transfer-encoding", "location"} if status else
                {"warc-type", "warc-date", "warc-target-uri", "warc-record-id", "content-length", "content-type", "warc-payload-digest", "warc-block-digest"})
            require(key not in critical,
                    "Archive identity or framing header is duplicated")
        else:
            result[key] = value.strip().decode("utf-8", errors="strict")
    return result


def parse_warc(raw, row, rules):
    require(len(raw) == int(row["length"]) and 0 < len(raw) <= rules["max_compressed_range_bytes"],
            "Archive compressed record length differs")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        record = stream.read(rules["max_uncompressed_record_bytes"]+1)
        require(len(record) <= rules["max_uncompressed_record_bytes"] and not stream.read(1), "Archive decompression exceeds its finite record bound")
    head, remaining = record.split(b"\r\n\r\n", 1)
    require(len(head) <= 65536, "Archive WARC headers exceed their finite bound")
    warc = _headers(head)
    require(warc.get("warc-type") == "response" and warc.get("content-type", "").replace(" ", "").lower() == "application/http;msgtype=response",
            "Archive record is not an original response; revisits need a separate declared lineage")
    require(warc.get("warc-target-uri") == row["url"] and re.fullmatch(r"<urn:uuid:[0-9a-f-]{36}>", warc.get("warc-record-id", "")),
            "Archive publisher target or record identity differs")
    captured = instant(warc["warc-date"])
    require(captured.strftime("%Y%m%d%H%M%S") == row["timestamp"] and captured <= instant(utc_now()),
            "Actual provider WARC/CDX capture clocks disagree or are future")
    length = warc.get("content-length", "")
    require(re.fullmatch(r"[1-9][0-9]*", length) and int(length) <= rules["max_uncompressed_record_bytes"],
            "Archive WARC content length is missing or unsupported")
    block = remaining[:int(length)]
    require(len(block) == int(length) and remaining[int(length):] in {b"\r\n\r\n", b"\n\n"},
            "Archive WARC block is truncated, extended or contains another record")
    http_head, payload = block.split(b"\r\n\r\n", 1)
    require(len(http_head) <= 65536 and payload, "Archived publisher HTTP block has no bounded original payload")
    http = _headers(http_head, status=True)
    require(http.get("transfer-encoding", "identity").lower() == "identity"
            and http.get("content-encoding", "identity").lower() == "identity", "Archive HTTP payload requires an unimplemented original decoding protocol")
    if "content-length" in http:
        require(re.fullmatch(r"[0-9]+", http["content-length"]) and int(http["content-length"]) == len(payload),
                "Archived publisher payload framing differs")
    digest = lambda value: base64.b32encode(hashlib.sha1(value).digest()).decode("ascii").rstrip("=")
    require(digest(payload) == row["digest"] and warc.get("warc-payload-digest") == "sha1:"+row["digest"]
            and warc.get("warc-block-digest") == "sha1:"+digest(block),
            "Archive payload/block digest is not independently reproduced from actual provider bytes")
    return {"provider_capture_at": warc["warc-date"], "archive_record_id": warc["warc-record-id"],
        "archive_record_type": "response", "publisher_response_status": 200,
        "publisher_media_type": http.get("content-type"), "provider_payload_digest": "sha1:"+row["digest"],
        "payload_sha256": hashlib.sha256(payload).hexdigest()}, payload, http_head


def validate(root, plan, capture_ref):
    store = Store(root, plan); artifacts = Artifacts(store.base)
    value = artifacts.read_json(capture_ref)
    fields(value, {"schema_id", "operation_id", "request_ref", "protocol_ref", "provider_registry_ref", "input_inventory_ref", "index_ref", "range_ref",
        "exact_index_row_ref", "publisher_payload_ref", "original_http_headers_ref", "source_attempts_ref", "source_reservations_ref", "consumed", "status", "unknown",
        "claimed_original_receipt_ref", "numeric_qualification", "historical_review_qualification"}, label="archive source capture")
    operation = store.operation(value["operation_id"])
    request = cc.request_value(artifacts.read_json(value["request_ref"]))
    raw_policy, rules = cc.policy()
    import verify
    require(value["schema_id"] == cc.SCHEMA and value["numeric_qualification"] is False and value["historical_review_qualification"] is False
            and artifacts.read(value["protocol_ref"]) == raw_policy and operation is not None and operation["status"] == "completed"
            and operation["request"] == {"schema_id": cc.SCHEMA, "payload": request, "protocol_ref": value["protocol_ref"], "code_identity": verify.code_identity()}
            and operation["result"] == {"capture_ref": capture_ref}
            and store.get("operation_result", value["operation_id"]) == operation["result"]
            and store.get("archive-capture", value["operation_id"]) == {"capture_ref": capture_ref},
            "Archive capture lacks its original current-code protocol/request/atomic completion")
    require(artifacts.read_json(value["provider_registry_ref"]) == rules["source_registry"],
            "Archive provider registry differs from its original exact policy")
    attempts = artifacts.read_json(value["source_attempts_ref"])
    reservations = artifacts.read_json(value["source_reservations_ref"])
    require(store.get(cc.SOURCE_KIND+"-request", value["operation_id"]) == {"request": request, "request_hash": fingerprint(request)},
            "Archive source ledger lacks its original request/budget seal")
    consumed = validate_cost(store, cc.SOURCE_KIND, value["operation_id"], attempts, reservations)
    require(value["consumed"] == consumed and consumed["source_requests"] <= request["max_source_operations"]
            and consumed["bytes_charged"] <= request["max_total_bytes"] and consumed["elapsed_seconds"] <= request["max_elapsed_seconds"],
            "Archive source ledger reset or exceeded its shared original transport budget")
    require(value["claimed_original_receipt_ref"] == request.get("claimed_original_receipt_ref"), "Archive old unverified control receipt was substituted")
    claimed = None
    if value["claimed_original_receipt_ref"] is not None:
        old = artifacts.read_json(value["claimed_original_receipt_ref"])
        claimed = old.get("retrieved_at") if isinstance(old, dict) else None
    witness = {"schema_id": SCHEMA, "capture_ref": capture_ref, "provider_id": "commoncrawl", "protocol_ref": value["protocol_ref"],
        "provider_registry_ref": value["provider_registry_ref"], "parser_code_identity": operation["request"]["code_identity"],
        "qualification": "unknown", "verified_at": utc_now(), "index_attested_at": None, "system_retrieved_at": None,
        "claimed_original_system_retrieved_at": claimed, "claimed_original_transport_origin": "unknown" if claimed is not None else None,
        "provider_capture_at": None, "provider_capture_precision": None, "provider_public_release_at": None,
        "historical_public_information_bound": {"status": "unknown", "reason": "provider_capture_does_not_prove_archive_public_release_or_investor_knowledge"},
        "original_url": request["original_url"], "archive_record_id": None, "archive_record_type": None,
        "publisher_payload_ref": None, "payload_sha256": None, "provider_payload_digest": None,
        "publisher_response_status": None, "publisher_media_type": None, "original_http_headers_ref": None,
        "numeric_qualification": False, "historical_review_qualification": False,
        "scope": "authenticated_provider_archived_response_version_only_not_publisher_truth_coverage_or_past_investor_knowledge", "unknown": copy_unknown(value["unknown"])}
    if value["input_inventory_ref"] is None:
        require(value["status"] == "unknown" and value["index_ref"] is None and value["range_ref"] is None,
                "Missing archive inventory was promoted to a later source stage")
        return witness
    begin, completed = operation["created_at"], operation["updated_at"]
    _, inventory_raw = _source(value["input_inventory_ref"], "archive_cc_inventory", rules["inventory_url"], store, artifacts, rules, attempts, begin, completed)
    url = cc.index_url(request, strict_json_loads(inventory_raw), rules)
    if value["index_ref"] is None:
        require(value["status"] == "unknown" and value["range_ref"] is None, "Missing original index was promoted to a range capture")
        return witness
    index_capture, index_raw = _source(value["index_ref"], "archive_cc_index", url, store, artifacts, rules, attempts, begin, completed)
    row = cc.exact_row(index_raw, request, rules)
    require(artifacts.read_json(value["exact_index_row_ref"]) == row, "Archive selected record differs from the actual official index")
    witness["index_attested_at"] = index_capture["retrieved_at"]
    if value["range_ref"] is None:
        require(value["status"] == "unknown", "Missing archive original payload was promoted to captured")
        return witness
    range_capture, range_raw = _source(value["range_ref"], "archive_cc_warc_range", cc.range_url(row, rules),
        store, artifacts, rules, attempts, begin, completed, row=row)
    require(instant(index_capture["retrieved_at"]) <= instant(range_capture["retrieved_at"]), "Archive range preceded its actual official record selection")
    try:
        parsed, payload, headers = parse_warc(range_raw, row, rules)
    except (ValueError, KeyError, TypeError, OSError, EOFError) as error:
        require(value["publisher_payload_ref"] is None and value["original_http_headers_ref"] is None
                and {"stage": "archive_version", "reason": str(error)} in witness["unknown"],
                "Unsupported archive version was given a fabricated publisher payload")
        return witness
    require(value["publisher_payload_ref"] is not None and value["original_http_headers_ref"] is not None
            and artifacts.read(value["publisher_payload_ref"]) == payload
            and artifacts.read(value["original_http_headers_ref"]) == headers,
            "Archive publisher payload/header refs differ from independently parsed actual bytes")
    require(instant(parsed["provider_capture_at"]) <= instant(range_capture["retrieved_at"])
            and value["status"] == "captured", "Archive provider capture is later than its actual present acquisition")
    witness.update(parsed)
    witness.update(qualification="provider_version_verified", provider_capture_precision="second",
        system_retrieved_at=range_capture["retrieved_at"], publisher_payload_ref=value["publisher_payload_ref"],
        original_http_headers_ref=value["original_http_headers_ref"])
    return witness


def copy_unknown(value):
    require(type(value) is list and all(type(row) is dict and set(row) == {"stage", "reason"}
            and type(row["stage"]) is str and type(row["reason"]) is str for row in value), "Archive unknown dispositions are invalid")
    return [dict(row) for row in value]
