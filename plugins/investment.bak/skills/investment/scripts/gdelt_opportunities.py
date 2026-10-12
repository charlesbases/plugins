"""Complete GDELT window metadata discovers opportunities, never source facts.

Official English and translated master manifests define the monitored domain.
Every selected GKG file remains in the denominator, including failed files.
The index clocks and themes never become an article publication or investment
feature. Only subsequent original-article capture and review can supply facts.
"""
import base64
import copy
import datetime as dt
import hashlib
import io
import math
import re
import time
import urllib.parse
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from contracts import fields, fingerprint, instant, require, utc_now
from source_journal import SourceJournal, validate_cost
import source_fetch

CHUNK = 8*1024*1024
TAIL_CHUNK = 64*1024
MAX_ZIP = source_fetch.MAX_BYTES
MAX_EXPANDED = 512*1024*1024
MANIFESTS = {"english": "https://data.gdeltproject.org/gdeltv2/masterfilelist.txt",
    "translated": "https://data.gdeltproject.org/gdeltv2/masterfilelist-translation.txt"}
FILE = re.compile(r"^https?://data\.gdeltproject\.org/gdeltv2/([0-9]{14})(\.translation)?\.(gkg\.csv|export\.CSV|mentions\.CSV)\.zip$")
FAMILY_CONTRACT = {"protocol_id": "GDELT_2_1_one_GKG_file_per_15_minute_update_and_language_stream_v1",
    "authorities": ["https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/", "https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf"],
    "scope": "single_GKG_update_file_English_and_translation_stream_names_not_all_global_news_or_article_publication_times"}


def expected_window_files(window_start, window_end, stream):
    start, end = instant(window_start), instant(window_end)
    require(stream in MANIFESTS and start <= end, "GDELT expected family window is reversed")
    step = 15*60
    first = math.ceil(start.timestamp()/step)*step
    last = math.floor(end.timestamp()/step)*step
    suffix = ".translation" if stream == "translated" else ""
    values = []
    for stamp in range(first, last+1, step):
        clock = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
        original_url = "http://data.gdeltproject.org/gdeltv2/"+clock.strftime("%Y%m%d%H%M%S")+suffix+".gkg.csv.zip"
        values.append({"file_id": fingerprint({"stream": stream, "url": original_url}), "stream": stream,
            "original_url": original_url, "https_url": original_url.replace("http://", "https://", 1), "nominal_batch_clock": clock.isoformat(),
            "clock_scope": "provider_batch_identifier_not_article_publication_or_first_observed"})
    return values


def parse_window_manifest_tail(raw, offset, stream, window_start, window_end):
    """All predefined file identities must have original rows; no sort assumption.

    Unknown bytes before this range are explicitly uninspected. The frozen file
    family supplies the window denominator, never the first old row in a suffix.
    Full-object replays remain a distinct stronger capture mode.
    """
    require(type(offset) is int and offset >= 0 and raw.endswith(b"\n"), "GDELT manifest tail is not a closed original object suffix")
    if offset:
        edge = raw.find(b"\n")
        require(edge >= 0, "GDELT suffix has no complete original row")
        offset += edge+1; raw = raw[edge+1:]
    parsed = parse_manifest_lines(raw.splitlines(keepends=True), stream, window_start, window_end)
    position = offset
    positions = {}
    for local_line, literal in enumerate(raw.splitlines(keepends=True), 1):
        positions[local_line] = position; position += len(literal)
    for row in parsed["window_files"]:
        row.update(manifest_absolute_byte_offset=positions[row["manifest_line"]], manifest_locator_scope="line_in_captured_suffix_and_absolute_object_byte_offset")
    expected = expected_window_files(window_start, window_end, stream)
    expected_ids = {row["file_id"] for row in expected}
    require({row["file_id"] for row in parsed["window_files"]} <= expected_ids, "GDELT current manifest has an additional window file outside the declared unique update family")
    return {**parsed, "suffix_first_complete_row_byte": offset, "family_contract": copy.deepcopy(FAMILY_CONTRACT),
        "expected_window_files": expected, "missing_expected_file_ids": sorted(expected_ids-{row["file_id"] for row in parsed["window_files"]}),
        "full_historical_manifest_bytes_captured": offset == 0, "window_family_accounted": {row["file_id"] for row in parsed["window_files"]} == expected_ids}


def _tls(record):
    require(record["transport"] in {"HTTPS_default_certificate_validation", "HTTPS_Windows_Schannel_certificate_validation"}, "GDELT metadata transport did not verify TLS")
    details = record["transport_details"]
    if record["transport"] == "HTTPS_Windows_Schannel_certificate_validation":
        require(details.get("backend") == "Schannel" and "Schannel" in details.get("curl_version", ""), "GDELT native TLS identity is absent")
    else:
        require(details.get("certificate_verification") == "required" and details.get("hostname_check") is True, "GDELT TLS hostname/certificate checks are absent")


def _range(url, start, end, timeout, *, etag=None):
    require(url in MANIFESTS.values() and type(start) is int and type(end) is int and 0 <= start <= end and end-start+1 <= CHUNK, "GDELT manifest range is outside its original metadata protocol")
    headers = {"Range": "bytes="+str(start)+"-"+str(end)}
    if etag is not None:
        headers["If-Match"] = etag
    deadline = time.monotonic()+timeout
    with source_fetch._open(url, timeout, end-start+1, headers=headers) as response:
        require(response.geturl() == url and response.status == 206, "GDELT server did not honor the original pinned range request")
        original_headers = {key.lower(): value for key, value in response.headers.items()}
        pieces, size = [], 0
        reader = getattr(response, "read1", response.read)
        while size <= end-start+1:
            remaining = deadline-time.monotonic()
            require(remaining > 0, "GDELT manifest range elapsed-time budget exhausted")
            socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if socket is not None:
                socket.settimeout(remaining)
            part = reader(min(65536, end-start+2-size))
            require(time.monotonic() < deadline, "GDELT manifest range exceeded its original elapsed-time budget")
            if not part:
                break
            pieces.append(part); size += len(part)
        raw = b"".join(pieces)
        value = {"url": url, "requested_range": [start, end], "request_headers": headers, "response_headers": original_headers,
            "http_status": response.status, "transport": response.transport, "transport_details": response.transport_details,
            "retrieved_at": utc_now(), "raw_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    validate_range(value, raw, expected_etag=etag)
    return value, raw


def validate_range(value, raw, *, expected_etag=None):
    _tls(value)
    require(value["url"] in MANIFESTS.values() and value["http_status"] == 206, "GDELT range has no authoritative manifest identity")
    start, end = value["requested_range"]
    require(type(start) is int and type(end) is int and 0 <= start <= end and end-start+1 <= CHUNK, "GDELT byte range is invalid")
    expected = {"Range": "bytes="+str(start)+"-"+str(end)}
    if expected_etag is not None:
        expected["If-Match"] = expected_etag
    require(value["request_headers"] == expected, "GDELT conditional range headers changed")
    headers = value["response_headers"]
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", headers.get("content-range", ""))
    require(match and [int(match[1]), int(match[2])] == [start, end] and int(match[3]) > end, "GDELT original Content-Range differs")
    etag = headers.get("etag")
    require(type(etag) is str and re.fullmatch(r'"[A-Za-z0-9._-]+"', etag), "Strong original GDELT manifest ETag required")
    require(expected_etag is None or etag == expected_etag, "GDELT manifest changed during its pinned capture")
    require(len(raw) == end-start+1 == value["bytes"] and hashlib.sha256(raw).hexdigest() == value["raw_sha256"], "GDELT range raw identity differs")
    return {"total_bytes": int(match[3]), "etag": etag, "provider_md5": _provider_md5(headers)}


def _provider_md5(headers):
    matches = [item.split("=", 1)[1].strip() for item in headers.get("x-goog-hash", "").split(",") if item.strip().startswith("md5=")]
    require(len(matches) == 1, "GDELT manifest lacks its original whole-object checksum")
    try:
        raw = base64.b64decode(matches[0], validate=True)
    except ValueError as error:
        raise ValueError("Malformed GDELT provider checksum") from error
    require(len(raw) == 16, "GDELT original MD5 checksum size changed")
    return raw.hex()


def parse_manifest_lines(lines, stream, window_start, window_end):
    start, end = instant(window_start), instant(window_end)
    require(stream in MANIFESTS and start <= end, "GDELT original window is reversed")
    selected, total, seen = [], 0, set()
    for line_number, literal in enumerate(lines, 1):
        text = literal.decode("ascii", errors="strict").rstrip("\r\n")
        parts = text.split()
        require(len(parts) == 3 and parts[0].isdigit() and int(parts[0]) > 0 and re.fullmatch(r"[a-f0-9]{32}", parts[1]), "GDELT original manifest row is malformed")
        match = FILE.fullmatch(parts[2])
        require(match and bool(match[2]) == (stream == "translated"), "GDELT manifest row protocol or language stream changed")
        total += 1
        require(parts[2] not in seen, "GDELT manifest repeated a file identity")
        seen.add(parts[2])
        clock = dt.datetime.strptime(match[1], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        if match[3] == "gkg.csv" and start <= clock <= end:
            selected.append({"file_id": fingerprint({"stream": stream, "url": parts[2]}), "stream": stream,
                "original_url": parts[2], "https_url": parts[2].replace("http://", "https://", 1), "bytes": int(parts[0]), "provider_md5": parts[1],
                "nominal_batch_clock": clock.isoformat(), "manifest_line": line_number,
                "manifest_literal_sha256": hashlib.sha256(literal).hexdigest(), "clock_scope": "provider_batch_identifier_not_article_publication_or_first_observed"})
    return {"manifest_rows": total, "window_files": selected}


def _zip_rule():
    return {"purpose": "news_opportunity_metadata", "allowed_hosts": ["data.gdeltproject.org", "storage.googleapis.com"], "redirect_hosts": ["data.gdeltproject.org", "storage.googleapis.com"],
        "path_pattern": r"^/(?:gdeltv2|data\.gdeltproject\.org/gdeltv2)/[0-9]{14}(?:\.translation)?\.gkg\.csv\.zip$", "default_urls": [], "encoding": None,
        "media_types": ["application/zip", "application/x-zip-compressed", "application/octet-stream"], "timezone": "UTC"}


def object_urls(file):
    """Original exact object through GCS's documented HTTPS path endpoint.

    The bucket is the original GDELT public-domain bucket, not an arbitrary
    third-party mirror. The entire returned ZIP must additionally match the
    exact size and MD5 in the independently captured HTTPS GDELT manifest.
    MD5 is a provider metadata checksum, not a new financial authentication
    or original-news/PIT qualification rule.
    """
    require(FILE.fullmatch(file["https_url"]), "GDELT object alias lacks its indexed exact file identity")
    return ["https://storage.googleapis.com/data.gdeltproject.org"+urllib.parse.urlsplit(file["https_url"]).path, file["https_url"]]


def parse_gkg(raw, file):
    require(len(raw) == file["bytes"] and hashlib.md5(raw).hexdigest() == file["provider_md5"], "GDELT compressed original differs from the complete manifest")
    archive = zipfile.ZipFile(io.BytesIO(raw))
    members = archive.infolist()
    require(len(members) == 1 and not members[0].is_dir() and not members[0].flag_bits & 1 and members[0].file_size <= MAX_EXPANDED,
        "GDELT ZIP needs one bounded unencrypted original GKG member")
    require(members[0].filename.rsplit("/", 1)[-1].lower().endswith(".gkg.csv") and not any(part == ".." for part in members[0].filename.split("/")), "GDELT member name is unsupported")
    records, excluded, themes, source_names, count = [], [], Counter(), Counter(), 0
    # ZipExtFile.readline repeatedly peeks/decompresses while finding a newline.
    # Buffer raw bytes without decoding, and reject an oversized row after only
    # the original 4 MiB budget plus one byte. Valid rows still reach ZIP EOF/CRC.
    with archive.open(members[0]) as compressed_stream, io.BufferedReader(compressed_stream, buffer_size=64*1024) as stream:
        for line_number, raw_line in enumerate(iter(lambda: stream.readline(4*1024*1024+1), b""), 1):
            require(len(raw_line) <= 4*1024*1024, "GDELT row exceeds its original parser memory budget")
            count += 1
            locator = {"metadata_file_id": file["file_id"], "metadata_row": line_number,
                "metadata_row_sha256": hashlib.sha256(raw_line).hexdigest(), "original_row_bytes": len(raw_line), "original_article_verified": False}
            try:
                # A provider row may contain invalid UTF-8. Do not guess a
                # legacy charset or silently replace bytes; quarantine exactly
                # that row while retaining the full verified original ZIP and
                # the true total row/file denominator.
                columns = raw_line.decode("utf-8", errors="strict").rstrip("\r\n").split("\t")
            except UnicodeDecodeError as error:
                excluded.append({**locator, "reason": "original_row_not_losslessly_UTF8_decodable", "decode_error": str(error), "scope": "unsupported_original_metadata_row_not_article_facts"})
                continue
            if len(columns) != 27 or not columns[0] or not (re.fullmatch(r"[0-9]{14}", columns[1]) or columns[1] == "0") or columns[2] not in {"1", "2", "3", "4", "5", "6"}:
                excluded.append({**locator, "reason": "original_GKG_2_1_row_shape_or_identity_unsupported", "original_field_count": len(columns), "scope": "unsupported_original_metadata_row_not_article_facts"})
                continue
            url = columns[4]
            parsed = urllib.parse.urlsplit(url)
            observation = {"record_id": columns[0], "provider_reported_article_clock": columns[1], "provider_clock_basis": "GKG_V2_1DATE_not_original_publisher_or_first_observed_evidence", "source_collection": columns[2], "source_name": columns[3],
                "original_document_identifier": url, "metadata_file_id": file["file_id"], "metadata_row": line_number,
                "metadata_row_sha256": hashlib.sha256(raw_line).hexdigest(), "themes": sorted(set(columns[7].split(";"))-{""}),
                "translation_info": columns[25], "article_publication_at": None, "original_article_verified": False,
                "scope": "machine_generated_opportunity_metadata_not_original_facts_or_predictive_features"}
            themes.update(observation["themes"]); source_names.update([columns[3]])
            if columns[2] != "1" or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                excluded.append({**observation, "reason": "non_web_or_unsupported_original_document_identifier"}); continue
            observation.update(https_probe_url=urllib.parse.urlunsplit(("https", parsed.hostname.lower(), parsed.path or "/", parsed.query, "")), publisher_host=parsed.hostname.lower())
            records.append(observation)
    return {"metadata_rows": count, "records": records, "excluded_records": excluded, "all_observed_theme_counts": dict(themes), "source_name_counts": dict(source_names)}


def _article_registry(records):
    by_host = {}
    for row in records:
        by_host.setdefault(row["publisher_host"], set()).add(row["https_probe_url"])
    sources = {}
    for host, urls in sorted(by_host.items()):
        source_id = "gdelt_publisher_"+fingerprint({"host": host})[:24]
        sources[source_id] = {"purpose": "news", "allowed_hosts": [host], "redirect_hosts": [host], "path_pattern": "^/",
            "default_urls": sorted(urls), "encoding": "utf-8", "media_types": ["text/html"], "timezone": "UTC",
            "timezone_basis": "unknown_never_qualifies_without_original_offset", "require_explicit_publication_offset": True,
            "article_adapter": "semantic_article_v1", "publication_adapter": "article_jsonld_v1", "body_tags": ["article"], "body_ids": [], "body_classes": [],
            "article_pattern": "^/", "minimum_body_characters": 60, "max_follow_links": 0, "original_opportunity_url_count": len(urls),
            "complete_explicit_url_scope": True, "collection_role": "unvetted_indexed_article_requires_original_fact_review",
            "discovery_scope": "all_original_URLs_in_captured_GDELT_monitored_window_metadata_not_all_world_news"}
    return {"schema_version": 1, "sources": sources}


def _file_records(parsed_files, artifacts, field):
    for row in parsed_files:
        if row["status"] == "captured":
            yield from artifacts.read_json(row["parsed_ref"])[field]


def _metadata_refs(parsed_files, field):
    return {"schema_id": "source_partitioned_GKG_metadata_v1", "field": field,
        "files": [{"file_id": row["file_id"], "parsed_ref": row["parsed_ref"]} for row in parsed_files if row["status"] == "captured"]}


def _cache_key(file, registry):
    return fingerprint({"file_id": file["file_id"], "original_provider_md5": file["provider_md5"], "original_bytes": file["bytes"], "source_registry_hash": fingerprint(registry)})


def _cached_object(file, registry, store, artifacts, *, freeze_missing=False, discovery_id=None):
    """Recheck an actual same-plan original against a fresh provider index.

    A new news collection still captures article bodies afresh. This cache is
    restricted to immutable GKG bytes whose exact provider checksum is present
    in this invocation's fresh original manifest. No previous test-plan data or
    normalized parsed result substitutes for current source verification.
    """
    key = _cache_key(file, registry); original = store.get("gdelt-object-original", key)
    if original is None:
        return None
    require(original["cache_key"] == key and original["file_id"] == file["file_id"], "Cached GDELT original belongs to another exact object version")
    capture = original["capture"]
    capture_operation = capture["source_attempt_id"].rsplit(":source:", 1)[0]
    require(original["source_discovery_id"] == capture_operation, "Cached GDELT capture changed its original source operation")
    if capture["source_attempt_id"] in _source_recovery_blocks(store, capture_operation):
        return None
    source_fetch.validate_capture_provenance(capture, registry)
    require(capture["requested_url"] in object_urls(file), "Cached GDELT object is outside its original provider namespace")
    attempt = store.get("gdelt-source-attempt", capture["source_attempt_id"])
    require(attempt is not None and attempt["status"] == "captured" and attempt["captured_metadata"] == capture, "Cached GDELT bytes have no original actual request provenance")
    try:
        raw = artifacts.read(original["raw_ref"])
    except FileNotFoundError:
        if freeze_missing:
            _freeze_missing_cached_original(original, store, discovery_id)
        return None
    require(len(raw) == capture["bytes"] and hashlib.sha256(raw).hexdigest() == capture["raw_sha256"], "Cached GDELT original raw bytes differ")
    return {"original": original, "parsed": parse_gkg(raw, file)}


def _coverage(files, parsed_files, records, excluded):
    captured = {row["file_id"] for row in parsed_files if row["status"] == "captured"}
    urls, hosts, count = set(), set(), 0
    for row in records:
        count += 1; urls.add(row["https_probe_url"]); hosts.add(row["publisher_host"])
    excluded_count = sum(1 for _ in excluded)
    themes = Counter()
    for row in parsed_files:
        if row["status"] == "captured":
            themes.update(row["parsed_summary"]["all_observed_theme_counts"])
    return {"manifest_window_files": len(files), "captured_window_files": len(captured), "pending_window_file_ids": sorted({row["file_id"] for row in files}-captured),
        "metadata_rows": count+excluded_count, "unique_original_web_URLs": len(urls), "unique_indexed_publishers": len(hosts),
        "excluded_metadata_rows": excluded_count, "all_observed_machine_theme_counts": dict(themes),
        "complete_captured_GDELT_window_domain": bool(files) and len(captured) == len(files), "global_news_denominator": None,
        "original_article_verified_count": 0, "article_first_publication_or_historical_PIT_claimed": False}


def _source_recovery_blocks(store, operation_id):
    legacy = store.get("gdelt-legacy-recovery-seal", operation_id)
    blocked = set(legacy["charged_unreplayed_source_attempts"]) if legacy else set()
    prefix = operation_id + ":recovery:"
    for key, seal in store.scan("gdelt-source-recovery-seal"):
        if not key.startswith(prefix):
            continue
        require(key == prefix+fingerprint(seal) and set(seal["blocked_attempt_ids"]) <= set(seal["attempt_ids"]),
                "GDELT source recovery seal changed its original attempt scope")
        if "observed_by_discovery_id" in seal:
            original = store.get("gdelt-object-original", seal["original_cache_key"])
            require(original is not None and original["source_discovery_id"] == operation_id
                    and original["capture"]["source_attempt_id"] == seal["original_attempt_id"]
                    and original["raw_ref"] == seal["original_raw_ref"]
                    and seal["original_attempt_id"] in seal["blocked_attempt_ids"]
                    and store.get("gdelt-source-attempt-request", seal["observed_by_discovery_id"]) is not None,
                    "GDELT missing cache observation differs from its original capture")
        blocked.update(seal["blocked_attempt_ids"])
    return blocked


def _freeze_missing_cached_original(original, store, discovery_id):
    """An owner observing another operation's lost cache cannot revive its clock."""
    store.assert_owned()
    require(type(discovery_id) is str and store.get("gdelt-source-attempt-request", discovery_id) is not None,
            "Missing GDELT cache must be observed by a frozen discovery")
    operation_id = original["source_discovery_id"]
    prefix = operation_id+":source:"
    attempts = [row for key, row in store.scan("gdelt-source-attempt")
        if key.startswith(prefix) and re.fullmatch(r"[1-9][0-9]*", key[len(prefix):])]
    attempts.sort(key=lambda row: int(row["attempt_id"].rsplit(":", 1)[1]))
    blocked = _source_recovery_blocks(store, operation_id)
    blocked.add(original["capture"]["source_attempt_id"])
    seal = {"attempt_ids": [row["attempt_id"] for row in attempts], "blocked_attempt_ids": sorted(blocked),
        "observed_by_discovery_id": discovery_id, "original_cache_key": original["cache_key"],
        "original_attempt_id": original["capture"]["source_attempt_id"], "original_raw_ref": original["raw_ref"]}
    require(blocked <= set(seal["attempt_ids"]), "Missing GDELT cache is outside its original source journal")
    store.put("gdelt-source-recovery-seal", operation_id+":recovery:"+fingerprint(seal), seal, immutable=True)


def _original_capture(attempt, store, artifacts):
    """Recover only complete original metadata and hash-verified original bytes."""
    capture = attempt.get("captured_metadata")
    if attempt.get("status") != "captured" or not capture:
        return None
    operation_id = attempt["attempt_id"].rsplit(":source:", 1)[0]
    recovery = store.get("gdelt-legacy-recovery-seal", operation_id)
    if attempt["attempt_id"] in _source_recovery_blocks(store, operation_id):
        return None
    original = store.get("gdelt-source-attempt-original", attempt["attempt_id"])
    if original is None and recovery:
        original = recovery["originals"].get(attempt["attempt_id"])
    if original is None and store.get("gdelt-source-attempt-reservation", attempt["attempt_id"]) is not None:
        return None
    if original is None:
        for _, cached in store.scan("gdelt-object-original"):
            if cached["capture"].get("source_attempt_id") == attempt["attempt_id"]:
                original = {"capture": cached["capture"], "raw_ref": cached["raw_ref"]}
                break
    if original is None:
        # Legacy range captures had no receipt containing their CAS reference.
        # Locate an existing whole CAS object; never reconstruct absent bytes.
        digest, size = capture.get("raw_sha256"), capture.get("bytes")
        require(type(digest) is str and re.fullmatch(r"[a-f0-9]{64}", digest)
                and type(size) is int and size > 0, "Original GDELT raw identity is invalid")
        for path in sorted((artifacts.base / "objects").glob("*/*/"+digest[:2]+"/"+digest)):
            reference = {"sha256": digest, "size": size, "path": path.relative_to(artifacts.base).as_posix()}
            try:
                artifacts.read(reference)
            except FileNotFoundError:
                continue
            original = {"capture": capture, "raw_ref": reference}
            break
    if original is None:
        return None
    require(original["capture"] == capture, "Recovered GDELT receipt changed original capture metadata")
    try:
        raw = artifacts.read(original["raw_ref"])
    except FileNotFoundError:
        return None
    require(len(raw) == capture["bytes"] and hashlib.sha256(raw).hexdigest() == capture["raw_sha256"],
            "Recovered GDELT raw bytes differ from their original attempt")
    return {"capture": copy.deepcopy(capture), "raw_ref": copy.deepcopy(original["raw_ref"]), "raw_bytes": raw}


def _freeze_legacy_originals(journal, store, artifacts):
    """Keep later captures from supplying bytes absent at the recovery boundary."""
    if store.get("gdelt-legacy-recovery-seal", journal.operation_id) is not None:
        return
    originals, unreplayed = {}, []
    for attempt in journal.attempts:
        if attempt["status"] != "captured" or journal.original(attempt["attempt_id"]) is not None:
            continue
        original = _original_capture(attempt, store, artifacts)
        if original is None:
            unreplayed.append(attempt["attempt_id"])
        else:
            originals[attempt["attempt_id"]] = {"capture": original["capture"], "raw_ref": original["raw_ref"]}
    store.put("gdelt-legacy-recovery-seal", journal.operation_id,
        {"attempt_ids": [row["attempt_id"] for row in journal.attempts],
         "originals": originals, "charged_unreplayed_source_attempts": unreplayed}, immutable=True)


def _freeze_source_originals(journal, store, artifacts):
    """Every recovery checks all old originals before any new source request."""
    blocked = _source_recovery_blocks(store, journal.operation_id)
    for attempt in journal.attempts:
        if attempt["status"] == "captured" and attempt["attempt_id"] not in blocked:
            if _original_capture(attempt, store, artifacts) is None:
                blocked.add(attempt["attempt_id"])
    if journal.attempts:
        seal = {"attempt_ids": [row["attempt_id"] for row in journal.attempts], "blocked_attempt_ids": sorted(blocked)}
        store.put("gdelt-source-recovery-seal", journal.operation_id+":recovery:"+fingerprint(seal), seal, immutable=True)


def _manifest_cost(attempts, reservations):
    completed = {row["attempt_id"] for row in attempts}
    cost = {"source_requests": 0, "bytes_charged": 0, "elapsed_seconds": 0.0}
    elapsed = []
    for row in attempts:
        if row["url"] in MANIFESTS.values():
            cost["source_requests"] += 1
            cost["bytes_charged"] += row["bytes_charged"]
            elapsed.append(row["elapsed_seconds"])
    for row in reservations:
        if row["attempt_id"] not in completed and row["url"] in MANIFESTS.values():
            cost["source_requests"] += 1
            cost["bytes_charged"] += row["bytes_reserved"]
            elapsed.append(row["timeout_seconds"])
    cost["elapsed_seconds"] = math.fsum(elapsed)
    return cost


def discover(payload, store, artifacts, operation_id):
    fields(payload, {"window_start", "window_end"}, {"timeout_seconds", "max_source_requests", "max_total_bytes", "max_elapsed_seconds", "manifest_capture_mode"}, "GDELT opportunity discovery")
    start, end = instant(payload["window_start"]), instant(payload["window_end"])
    require(start <= end <= instant(utc_now()) and end-start <= dt.timedelta(days=31), "GDELT opportunity window is future, reversed or exceeds the declared recent domain")
    store.assert_owned(); previous = store.get("gdelt-opportunity-discovery", operation_id)
    if previous:
        require(previous["request"] == payload, "GDELT discovery changed its source window or budget")
        validate(previous, store, artifacts); return previous
    timeout = payload.get("timeout_seconds", 30)
    require(type(timeout) in {int, float} and 0 < timeout <= 30, "GDELT source timeout is outside verified transport bounds")
    caps = {"source_requests": payload.get("max_source_requests", 10**12), "bytes": payload.get("max_total_bytes", 10**12), "elapsed_seconds": payload.get("max_elapsed_seconds", 10**12)}
    require(all(type(value) is int and value > 0 for value in caps.values()), "Positive finite metadata resource caps required")
    journal = SourceJournal(store, "gdelt-source-attempt", operation_id, payload)
    _freeze_legacy_originals(journal, store, artifacts)
    _freeze_source_originals(journal, store, artifacts)
    used = journal.used
    manifests, all_files, parsed_files = [], [], []
    mode = payload.get("manifest_capture_mode", "complete_window_file_family")
    require(mode in {"complete_window_file_family", "whole_object"}, "Unknown GDELT manifest capture protocol")
    def permitted(bytes):
        return used["source_requests"]+1 <= caps["source_requests"] and used["bytes_charged"]+bytes <= caps["bytes"] and used["elapsed_seconds"]+timeout <= caps["elapsed_seconds"]
    def perform(url, bytes, function, **reservation_metadata):
        reservation = journal.reserve(url, bytes, timeout, **reservation_metadata)
        begun = time.monotonic()
        attempt = {key: reservation[key] for key in ("attempt_id", "url", "bytes_reserved", "started_at")}
        attempt["transport_started_at"] = utc_now()
        original = None
        try:
            result = function()
            attempt.update(ended_at=utc_now(), elapsed_seconds=time.monotonic()-begun)
            actual = len(result[1]) if type(result) is tuple else len(result["raw_bytes"])
            metadata = result[0] if type(result) is tuple else result
            metadata["source_attempt_id"] = attempt["attempt_id"]
            attempt.update(status="captured", bytes_charged=actual, captured_metadata={key: copy.deepcopy(item) for key, item in metadata.items() if key not in {"raw_bytes", "text"}})
            original = {"capture": attempt["captured_metadata"], "raw_ref": artifacts.put_bytes(result[1] if type(result) is tuple else result["raw_bytes"])}
            return result
        except (OSError, ValueError) as error:
            if "status" not in attempt:
                attempt.update(status="unavailable", bytes_charged=bytes, reason=str(error))
            raise
        finally:
            if "status" in attempt:
                if "ended_at" not in attempt:
                    attempt.update(ended_at=utc_now(), elapsed_seconds=time.monotonic()-begun)
                journal.commit(attempt, original)

    def recover_range(url, left, right, etag=None):
        for attempt in journal.attempts:
            capture = attempt.get("captured_metadata", {})
            if attempt["url"] != url or capture.get("requested_range") != [left, right]:
                continue
            if etag is not None and capture.get("response_headers", {}).get("etag") != etag:
                continue
            original = _original_capture(attempt, store, artifacts)
            if original is not None:
                validate_range(original["capture"], original["raw_bytes"],
                    expected_etag=original["capture"]["request_headers"].get("If-Match"))
                return original["capture"], original["raw_bytes"]
        return None
    for stream, url in MANIFESTS.items():
        manifest = {"stream": stream, "url": url, "status": "not_examined_global_budget", "ranges": [], "capture_mode": mode}
        if mode == "complete_window_file_family":
            manifest.update(expected_window_files=expected_window_files(payload["window_start"], payload["window_end"], stream), family_contract=copy.deepcopy(FAMILY_CONTRACT))
        manifests.append(manifest)
        try:
            prior_probe = next((row["captured_metadata"] for row in journal.attempts
                if row["url"] == url and row.get("captured_metadata", {}).get("requested_range") == [0, 0]), None)
            pinned_etag = prior_probe["response_headers"]["etag"] if prior_probe else None
            recovered = recover_range(url, 0, 0, pinned_etag)
            if recovered is None:
                if not permitted(1):
                    continue
                # Even a legacy capture whose bytes were lost pins this
                # operation's original manifest generation before new IO.
                probe, raw = perform(url, 1, lambda: _range(url, 0, 0, timeout, etag=pinned_etag), requested_range=[0, 0])
            else:
                probe, raw = recovered
            descriptor = validate_range(probe, raw, expected_etag=probe["request_headers"].get("If-Match"))
            manifest.update(probe={**probe, "raw_ref": artifacts.put_bytes(raw)}, **descriptor, status="partial_manifest")
            if mode == "complete_window_file_family":
                manifest["expected_window_files"] = expected_window_files(payload["window_start"], payload["window_end"], stream)
                manifest["family_contract"] = copy.deepcopy(FAMILY_CONTRACT)
                blocks = [(max(0, right-TAIL_CHUNK+1), right) for right in range(descriptor["total_bytes"]-1, -1, -TAIL_CHUNK)]
            else:
                blocks = [(left, min(descriptor["total_bytes"]-1, left+CHUNK-1)) for left in range(0, descriptor["total_bytes"], CHUNK)]
            for left, right in blocks:
                recovered = recover_range(url, left, right, descriptor["etag"])
                if recovered is None:
                    if not permitted(right-left+1):
                        break
                    value, data = perform(url, right-left+1, lambda left=left, right=right: _range(url, left, right, timeout, etag=descriptor["etag"]), requested_range=[left, right], manifest_etag=descriptor["etag"])
                else:
                    value, data = recovered
                manifest["ranges"].append({**value, "raw_ref": artifacts.put_bytes(data)})
                if mode == "complete_window_file_family":
                    suffix = b"".join(artifacts.read(row["raw_ref"]) for row in reversed(manifest["ranges"]))
                    parsed = parse_window_manifest_tail(suffix, left, stream, payload["window_start"], payload["window_end"])
                    manifest["observed_suffix"] = parsed
                    if parsed["window_family_accounted"]:
                        manifest.update(status="captured", parsed=parsed, suffix_sha256=hashlib.sha256(suffix).hexdigest(),
                            full_historical_manifest_bytes_captured=left == 0, full_object_checksum_verified=left == 0 and hashlib.md5(suffix).hexdigest() == descriptor["provider_md5"])
                        all_files.extend(parsed["window_files"])
                        break
            if mode == "whole_object" and sum(row["bytes"] for row in manifest["ranges"]) == descriptor["total_bytes"]:
                whole = b"".join(artifacts.read(row["raw_ref"]) for row in manifest["ranges"])
                require(hashlib.md5(whole).hexdigest() == descriptor["provider_md5"] and whole.endswith(b"\n"), "GDELT complete manifest object checksum or final row differs")
                parsed = parse_manifest_lines(whole.splitlines(keepends=True), stream, payload["window_start"], payload["window_end"])
                manifest.update(status="captured", parsed=parsed, whole_sha256=hashlib.sha256(whole).hexdigest(), full_historical_manifest_bytes_captured=True, full_object_checksum_verified=True)
                all_files.extend(parsed["window_files"])
        except (OSError, ValueError) as error:
            manifest.update(status="unavailable_or_changed_manifest", reason=str(error))
        if mode == "complete_window_file_family" and manifest["status"] != "captured" and manifest.get("observed_suffix"):
            all_files.extend(manifest["observed_suffix"]["window_files"])
    # The complete original manifests define every file before ZIP selection.
    # If either manifest is partial, there is no honest finite window total.
    manifests_complete = all(row["status"] == "captured" for row in manifests)
    manifest_cost = _manifest_cost(journal.attempts, journal.reservations)
    execution_plan = {"phase": "complete_manifests_frozen" if manifests_complete else "manifest_domain_incomplete",
        "window_file_count": len(all_files) if manifests_complete else None, "window_compressed_bytes": sum(row["bytes"] for row in all_files) if manifests_complete else None,
        "source_request_capacity": manifest_cost["source_requests"]+2*len(all_files), "source_byte_capacity": manifest_cost["bytes_charged"]+2*sum(row["bytes"] for row in all_files),
        "source_elapsed_capacity": manifest_cost["elapsed_seconds"]+2*len(all_files)*timeout,
        "basis": "every_original_window_file_from_both_complete_provider_manifests_before_reading_GKG_records"}
    registry = {"schema_version": 1, "sources": {"gdelt_gkg": _zip_rule()}}
    def read_object(job):
        begun, started_at = time.monotonic(), utc_now()
        try:
            capture = source_fetch.fetch(job["target"], "gdelt_gkg", timeout=timeout, max_bytes=job["file"]["bytes"], registry=registry)
            return {"capture": capture, "started_at": started_at,
                    "elapsed_seconds": time.monotonic()-begun, "ended_at": utc_now()}
        except (OSError, ValueError) as error:
            return {"error": str(error), "started_at": started_at,
                    "elapsed_seconds": time.monotonic()-begun, "ended_at": utc_now()}

    def settle_object(job, fetched):
        """Only the controller charges and journals actual readonly requests."""
        file, capture = job["file"], fetched.get("capture")
        charged = len(capture["raw_bytes"]) if capture else file["bytes"]
        reserved = job["reservation"]
        attempt = {"attempt_id": reserved["attempt_id"],
            "url": job["target"], "bytes_reserved": file["bytes"], "started_at": reserved["started_at"],
            "transport_started_at": fetched["started_at"],
            "ended_at": fetched["ended_at"], "elapsed_seconds": fetched["elapsed_seconds"],
            "status": "captured" if capture else "unavailable", "bytes_charged": charged}
        original = None
        if capture:
            capture["source_attempt_id"] = attempt["attempt_id"]
            attempt["captured_metadata"] = {key: copy.deepcopy(item) for key, item in capture.items()
                                            if key not in {"raw_bytes", "text"}}
            original = {"capture": attempt["captured_metadata"], "raw_ref": artifacts.put_bytes(capture["raw_bytes"])}
        else:
            attempt["reason"] = fetched["error"]
        journal.commit(attempt, original)
        return capture

    def seal_object(job, capture, failures):
        """Retain the original checksum, CRC, parsing and provenance gates."""
        file, entry = job["file"], job["entry"]
        entry["original_object_transport_attempts"] = failures
        try:
            require(capture is not None, "Indexed GDELT object is unavailable through its declared HTTPS endpoints: "+str(failures))
            source_fetch.validate_capture_provenance(capture, registry)
            parsed = parse_gkg(capture["raw_bytes"], file)
            entry.update(status="captured", capture={key: value for key, value in capture.items() if key not in {"raw_bytes", "text"}}, raw_ref=artifacts.put_bytes(capture["raw_bytes"]),
                parsed_ref=artifacts.put_json(parsed), parsed_summary={key: item for key, item in parsed.items() if key not in {"records", "excluded_records"}})
            key = _cache_key(file, registry)
            if store.get("gdelt-object-original", key) is None:
                store.put("gdelt-object-original", key, {"cache_key": key, "file_id": file["file_id"], "source_discovery_id": operation_id,
                    "capture": entry["capture"], "raw_ref": entry["raw_ref"]}, immutable=True)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
            entry.update(status="unavailable_or_unsupported_protocol", reason=str(error))
            if capture:
                entry.update(capture={key: value for key, value in capture.items() if key not in {"raw_bytes", "text"}}, raw_ref=artifacts.put_bytes(capture["raw_bytes"]))

    # Each wave has four readonly network requests. Settle all primary jobs
    # before reserving a fallback wave, so aliases cannot spend pending primary
    # reservations. The controller alone journals and seals every result.
    with ThreadPoolExecutor(max_workers=4) as pool:
        cursor = 0
        while cursor < len(all_files):
            wave, failed = [], []
            while cursor < len(all_files) and len(wave)+len(failed) < 4:
                file = all_files[cursor]; cursor += 1
                entry = {"file_id": file["file_id"], "status": "not_examined_global_budget"}; parsed_files.append(entry)
                cached = _cached_object(file, registry, store, artifacts, freeze_missing=True, discovery_id=operation_id)
                if cached is not None:
                    parsed = cached["parsed"]; original = cached["original"]
                    entry.update(status="captured", capture=original["capture"], raw_ref=original["raw_ref"], parsed_ref=artifacts.put_json(parsed),
                        parsed_summary={key: item for key, item in parsed.items() if key not in {"records", "excluded_records"}})
                    if original["source_discovery_id"] != operation_id:
                        entry["reused_original_source"] = {"cache_key": original["cache_key"], "source_discovery_id": original["source_discovery_id"],
                            "original_capture_at": original["capture"]["retrieved_at"], "current_manifest_checksum_rechecked": True}
                    else:
                        entry["original_object_transport_attempts"] = [{"url": row["url"], "reason": row["reason"]}
                            for row in journal.attempts if row["url"] in object_urls(file) and row["status"] == "unavailable"]
                    continue
                if file["bytes"] > MAX_ZIP:
                    entry.update(status="source_transport_size_unsupported", reason="original compressed file exceeds verified transport per-response limit"); continue
                prior = [row for row in journal.attempts if row["url"] in object_urls(file)]
                failures = [{"url": row["url"], "reason": row["reason"]} for row in prior if row["status"] == "unavailable"]
                job = {"file": file, "entry": entry, "target": object_urls(file)[0]}
                recovered = next((original for row in prior
                    if (original := _original_capture(row, store, artifacts)) is not None), None)
                if recovered is not None:
                    seal_object(job, {**recovered["capture"], "raw_bytes": recovered["raw_bytes"], "text": None}, failures)
                    continue
                if any(row["url"] == object_urls(file)[1] for row in failures):
                    seal_object(job, None, failures)
                    continue
                if failures:
                    failed.append({**job, "failures": failures})
                    continue
                if not permitted(file["bytes"]):
                    continue
                job["reservation"] = journal.reserve(job["target"], file["bytes"], timeout, file_id=file["file_id"])
                wave.append(job)
            futures = [(job, pool.submit(read_object, job)) for job in wave]
            for job, future in futures:
                fetched = future.result(); file = job["file"]
                capture = settle_object(job, fetched)
                if capture is not None:
                    seal_object(job, capture, [])
                else:
                    failed.append({**job, "failures": [{"url": job["target"], "reason": fetched["error"]}]})
            # Futures retain their raw results: release the primary wave before
            # starting another four responses, keeping the raw working set bounded.
            futures.clear()
            if wave:
                del future, fetched, capture
            while failed:
                fallback, deferred = [], []
                for job in failed:
                    file = job["file"]
                    if permitted(file["bytes"]):
                        target = object_urls(file)[1]
                        fallback.append({**job, "target": target,
                            "reservation": journal.reserve(target, file["bytes"], timeout, file_id=file["file_id"])})
                    else:
                        deferred.append(job)
                if not fallback:
                    for job in deferred:
                        seal_object(job, None, job["failures"])
                    break
                futures = [(job, pool.submit(read_object, job)) for job in fallback]
                for job, future in futures:
                    fetched = future.result(); file = job["file"]
                    capture = settle_object(job, fetched)
                    failures = job["failures"] + ([] if capture is not None else [{"url": job["target"], "reason": fetched["error"]}])
                    seal_object(job, capture, failures)
                futures.clear()
                del future, fetched, capture
                # Actual charges can release bytes/clock reserved for this wave.
                # Recheck deferred jobs without another attempt on any sent URL.
                failed = deferred
    selected_attempt_ids = {row["source_attempt_id"] for row in
        [manifest["probe"] for manifest in manifests if manifest.get("probe")]
        + [part for manifest in manifests for part in manifest["ranges"]]
        + [row["capture"] for row in parsed_files if row.get("capture") and not row.get("reused_original_source")]}
    retained, unreplayed = [], []
    for attempt in journal.attempts:
        if attempt["status"] != "captured" or attempt["attempt_id"] in selected_attempt_ids:
            continue
        original = _original_capture(attempt, store, artifacts)
        if original is None:
            unreplayed.append(attempt["attempt_id"])
        else:
            retained.append({"capture": original["capture"], "raw_ref": original["raw_ref"]})
    article_registry = _article_registry(_file_records(parsed_files, artifacts, "records"))
    value = {"schema_version": 1, "discovery_id": operation_id, "request": copy.deepcopy(payload), "request_hash": fingerprint(payload), "captured_at": utc_now(),
        "manifests": manifests, "window_files": all_files, "parsed_files": parsed_files, "records_ref": artifacts.put_json(_metadata_refs(parsed_files, "records")), "excluded_records_ref": artifacts.put_json(_metadata_refs(parsed_files, "excluded_records")),
        "source_registry_ref": artifacts.put_json(article_registry), "execution_plan": execution_plan, "global_caps": caps,
        "consumed": validate_cost(store, "gdelt-source-attempt", operation_id, journal.attempts, journal.reservations),
        "source_attempts": copy.deepcopy(journal.attempts), "source_reservations": copy.deepcopy(journal.reservations),
        "retained_source_captures": retained, "charged_unreplayed_source_attempts": unreplayed,
        "coverage": _coverage(all_files, parsed_files, _file_records(parsed_files, artifacts, "records"), _file_records(parsed_files, artifacts, "excluded_records")), "status": "metadata_window_discovered" if manifests_complete and all(row["status"] == "captured" for row in parsed_files) else "partial_metadata_window",
        "original_news_fact_qualification": False, "historical_PIT_qualification": False, "trade_ready": False,
        "scope": "complete_provider_monitored_window_when_all_manifests_and_GKG_files_captured_not_all_world_news_or_original_articles"}
    if not manifests_complete:
        value["coverage"]["manifest_window_files"] = None
        value["coverage"]["complete_captured_GDELT_window_domain"] = False
    value["coverage"].update(manifest_capture_mode=mode, expected_window_file_count=sum(len(row.get("expected_window_files", [])) for row in manifests) if mode == "complete_window_file_family" else None,
        missing_expected_manifest_file_ids=sorted({file["file_id"] for row in manifests for file in row.get("expected_window_files", [])}-{file["file_id"] for file in all_files}),
        full_historical_manifest_bytes_captured=all(row.get("full_historical_manifest_bytes_captured") is True for row in manifests),
        full_historical_object_checksum_verified=all(row.get("full_object_checksum_verified") is True for row in manifests))
    validate(value, store, artifacts, require_committed=False)
    store.put("gdelt-opportunity-discovery", operation_id, value, immutable=True)
    return value


def validate(value, store, artifacts, *, require_committed=True):
    require((not require_committed or store.get("gdelt-opportunity-discovery", value["discovery_id"]) == value)
            and value["request_hash"] == fingerprint(value["request"]), "GDELT metadata differs from its canonical source operation")
    request_seal = store.get("gdelt-source-attempt-request", value["discovery_id"])
    require(request_seal is None or request_seal == {"request": value["request"], "request_hash": value["request_hash"]},
            "GDELT request differs from its original frozen window/budget")
    files = []
    require([row["stream"] for row in value["manifests"]] == list(MANIFESTS), "GDELT metadata omitted a language stream")
    for row in value["manifests"]:
        require(row["url"] == MANIFESTS[row["stream"]], "GDELT manifest authority URL changed")
        mode = value["request"].get("manifest_capture_mode", "complete_window_file_family")
        require(row["capture_mode"] == mode, "GDELT manifest capture protocol differs from request")
        if mode == "complete_window_file_family":
            require(row.get("family_contract") == FAMILY_CONTRACT and row.get("expected_window_files") == expected_window_files(value["request"]["window_start"], value["request"]["window_end"], row["stream"]), "GDELT predefined complete window family differs")
        if row.get("probe"):
            original_probe = next((attempt["captured_metadata"] for attempt in value["source_attempts"]
                if attempt["url"] == row["url"] and attempt.get("captured_metadata", {}).get("requested_range") == [0, 0]), None)
            require(original_probe is not None and row["probe"]["response_headers"]["etag"] == original_probe["response_headers"]["etag"],
                    "GDELT recovery changed the original pinned manifest generation")
            descriptor = validate_range(row["probe"], artifacts.read(row["probe"]["raw_ref"]),
                expected_etag=row["probe"]["request_headers"].get("If-Match"))
            require(all(row[key] == descriptor[key] for key in descriptor), "GDELT manifest object descriptor differs")
            mode = value["request"].get("manifest_capture_mode", "complete_window_file_family")
            require(row["capture_mode"] == mode, "GDELT manifest capture protocol differs from request")
            if mode == "complete_window_file_family":
                require(row.get("family_contract") == FAMILY_CONTRACT and row.get("expected_window_files") == expected_window_files(value["request"]["window_start"], value["request"]["window_end"], row["stream"]), "GDELT predefined complete window family differs")
            left, right, chunks = 0, descriptor["total_bytes"]-1, []
            for part in row["ranges"]:
                raw = artifacts.read(part["raw_ref"]); actual = validate_range(part, raw, expected_etag=descriptor["etag"])
                require(part["url"] == row["url"] and actual == descriptor, "GDELT manifest byte range changed its source generation")
                if mode == "whole_object":
                    require(part["requested_range"][0] == left, "GDELT whole object has a missing original byte range")
                else:
                    require(part["requested_range"][1] == right, "GDELT suffix has a missing original byte range")
                    right = part["requested_range"][0]-1
                left += len(raw); chunks.append(raw)
            if mode == "complete_window_file_family" and chunks:
                suffix = b"".join(reversed(chunks))
                parsed = parse_window_manifest_tail(suffix, right+1, row["stream"], value["request"]["window_start"], value["request"]["window_end"])
                require(parsed == row["observed_suffix"], "GDELT original window suffix inventory differs")
                require(row["status"] != "captured" or parsed["window_family_accounted"] and parsed == row["parsed"] and hashlib.sha256(suffix).hexdigest() == row["suffix_sha256"], "GDELT complete-window claim omitted a predefined original file")
                if row["status"] == "captured":
                    require(row["full_historical_manifest_bytes_captured"] is (right+1 == 0) and row["full_object_checksum_verified"] is (right+1 == 0 and hashlib.md5(suffix).hexdigest() == descriptor["provider_md5"]), "Window evidence cannot claim unread historical manifest bytes or checksum")
                files.extend(parsed["window_files"])
            elif row["status"] == "captured":
                whole = b"".join(chunks)
                require(left == descriptor["total_bytes"] and hashlib.md5(whole).hexdigest() == descriptor["provider_md5"] and hashlib.sha256(whole).hexdigest() == row["whole_sha256"], "GDELT whole-manifest raw identity differs")
                parsed = parse_manifest_lines(whole.splitlines(keepends=True), row["stream"], value["request"]["window_start"], value["request"]["window_end"])
                require(parsed == row["parsed"], "GDELT complete window inventory differs from original manifest rows")
                files.extend(parsed["window_files"])
        require(row["status"] in {"captured", "not_examined_global_budget", "partial_manifest", "unavailable_or_changed_manifest"}, "GDELT manifest completion label is unsupported")
    require(files == value["window_files"], "GDELT window file denominator changed")
    require([row["file_id"] for row in value["parsed_files"]] == [row["file_id"] for row in files], "GDELT source file scope omitted a pending file")
    registry = {"schema_version": 1, "sources": {"gdelt_gkg": _zip_rule()}}
    for file, row in zip(files, value["parsed_files"]):
        if row.get("capture"):
            source_fetch.validate_capture_provenance(row["capture"], registry)
            require(row["capture"]["requested_url"] in object_urls(file), "GDELT metadata ZIP is not the indexed exact original object")
            raw = artifacts.read(row["raw_ref"])
            require(hashlib.sha256(raw).hexdigest() == row["capture"]["raw_sha256"] and len(raw) == row["capture"]["bytes"], "GDELT ZIP raw identity differs")
            if row["status"] == "captured":
                parsed = parse_gkg(raw, file)
                require(parsed == artifacts.read_json(row["parsed_ref"]) and row["parsed_summary"] == {key: item for key, item in parsed.items() if key not in {"records", "excluded_records"}}, "GDELT opportunity URL/theme rows differ from original compressed metadata")
                if row.get("reused_original_source"):
                    cached = _cached_object(file, registry, store, artifacts)
                    require(cached is not None and row["capture"] == cached["original"]["capture"] and row["raw_ref"] == cached["original"]["raw_ref"]
                        and row["reused_original_source"] == {"cache_key": cached["original"]["cache_key"], "source_discovery_id": cached["original"]["source_discovery_id"],
                            "original_capture_at": cached["original"]["capture"]["retrieved_at"], "current_manifest_checksum_rechecked": True}, "Reused metadata is not independently rechecked original bytes")
            else:
                require(row["status"] == "unavailable_or_unsupported_protocol", "A captured original was hidden behind a missing-source label")
                try:
                    parse_gkg(raw, file)
                except (ValueError, zipfile.BadZipFile, RuntimeError) as error:
                    require(str(error) == row["reason"], "GDELT unsupported compressed protocol reason differs")
                else:
                    require(False, "A supported GKG original was marked unsupported")
        require(row["status"] in {"captured", "not_examined_manifest_incomplete", "not_examined_global_budget", "source_transport_size_unsupported", "unavailable_or_unsupported_protocol"}, "GDELT file completion label is unsupported")
    article_registry = _article_registry(_file_records(value["parsed_files"], artifacts, "records"))
    require(artifacts.read_json(value["records_ref"]) == _metadata_refs(value["parsed_files"], "records") and artifacts.read_json(value["excluded_records_ref"]) == _metadata_refs(value["parsed_files"], "excluded_records") and artifacts.read_json(value["source_registry_ref"]) == article_registry, "Dynamic original-article domain differs from complete parsed opportunity metadata")
    coverage = _coverage(files, value["parsed_files"], _file_records(value["parsed_files"], artifacts, "records"), _file_records(value["parsed_files"], artifacts, "excluded_records"))
    complete = all(row["status"] == "captured" for row in value["manifests"])
    if not complete:
        coverage["manifest_window_files"] = None; coverage["complete_captured_GDELT_window_domain"] = False
    mode = value["request"].get("manifest_capture_mode", "complete_window_file_family")
    coverage.update(manifest_capture_mode=mode, expected_window_file_count=sum(len(row.get("expected_window_files", [])) for row in value["manifests"]) if mode == "complete_window_file_family" else None,
        missing_expected_manifest_file_ids=sorted({file["file_id"] for row in value["manifests"] for file in row.get("expected_window_files", [])}-{file["file_id"] for file in files}),
        full_historical_manifest_bytes_captured=all(row.get("full_historical_manifest_bytes_captured") is True for row in value["manifests"]),
        full_historical_object_checksum_verified=all(row.get("full_object_checksum_verified") is True for row in value["manifests"]))
    require(coverage == value["coverage"], "GDELT opportunity coverage statistics differ from original complete domain")
    consumed = validate_cost(store, "gdelt-source-attempt", value["discovery_id"], value["source_attempts"], value.get("source_reservations", []))
    for reservation in value.get("source_reservations", []):
        require(instant(reservation["started_at"]) <= instant(value["captured_at"]),
                "GDELT source reservation follows the final receipt")
    require(len({row["attempt_id"] for row in value["source_attempts"]}) == len(value["source_attempts"]), "GDELT source attempt was duplicated")
    captured_records = [row["probe"] for row in value["manifests"] if row.get("probe")]+[part for row in value["manifests"] for part in row["ranges"]]+[row["capture"] for row in value["parsed_files"] if row.get("capture") and not row.get("reused_original_source")]
    for original in value.get("retained_source_captures", []):
        capture, raw = original["capture"], artifacts.read(original["raw_ref"])
        require(len(raw) == capture["bytes"] and hashlib.sha256(raw).hexdigest() == capture["raw_sha256"],
                "Retained GDELT source bytes differ from their original capture")
        if capture.get("url") in MANIFESTS.values():
            validate_range(capture, raw, expected_etag=capture["request_headers"].get("If-Match"))
        else:
            source_fetch.validate_capture_provenance(capture, registry)
        captured_records.append(capture)
    unreplayed = value.get("charged_unreplayed_source_attempts", [])
    require(type(unreplayed) is list and len(set(unreplayed)) == len(unreplayed), "Unreplayed GDELT source attempt was duplicated")
    blocked = _source_recovery_blocks(store, value["discovery_id"])
    require(blocked <= set(unreplayed) and not blocked.intersection(record["source_attempt_id"] for record in captured_records),
            "GDELT recovery supplied later bytes for an originally missing capture")
    require(len(captured_records)+len(unreplayed) == sum(row["status"] == "captured" for row in value["source_attempts"]), "GDELT source records differ from charged original network attempts")
    for row in value["source_attempts"]:
        require(store.get("gdelt-source-attempt", row["attempt_id"]) == row, "GDELT source attempt costs differ from their original controller journal")
        require(instant(row["started_at"]) <= instant(row["ended_at"]) <= instant(value["captured_at"]), "GDELT capture clocks are future or reversed")
        if "transport_started_at" in row:
            require(instant(row["started_at"]) <= instant(row["transport_started_at"]) <= instant(row["ended_at"]),
                    "GDELT worker start precedes its owner reservation or follows completion")
        require(type(row["elapsed_seconds"]) in {int, float} and 0 <= row["elapsed_seconds"] and row["status"] in {"captured", "unavailable"}, "GDELT source attempt cost or state is invalid")
        if row["status"] == "captured":
            metadata = row["captured_metadata"]
            require(metadata.get("url", metadata.get("requested_url")) == row["url"]
                    and row["bytes_charged"] == metadata["bytes"]
                    and instant(row["started_at"]) <= instant(metadata["retrieved_at"]) <= instant(row["ended_at"]),
                    "GDELT charged original capture identity or clock differs")
            matches = [record for record in captured_records if record["source_attempt_id"] == row["attempt_id"]]
            if row["attempt_id"] in unreplayed:
                require(not matches and _original_capture(row, store, artifacts) is None,
                        "Unreplayed GDELT source has recoverable bytes or a claimed capture")
                continue
            require(len(matches) == 1 and {key: item for key, item in matches[0].items() if key != "raw_ref"} == row["captured_metadata"] and row["bytes_charged"] == matches[0]["bytes"], "GDELT original captured record differs from its immutable attempt")
            require(instant(row["started_at"]) <= instant(matches[0]["retrieved_at"]) <= instant(row["ended_at"]), "GDELT source capture clock differs from its request")
            if "transport_started_at" in row:
                require(instant(row["transport_started_at"]) <= instant(matches[0]["retrieved_at"]),
                        "GDELT capture precedes the original readonly worker")
        else:
            require(row["bytes_charged"] == row["bytes_reserved"] and row.get("reason"), "Failed GDELT source must retain its whole original reservation")
    require(consumed == value["consumed"] and consumed["source_requests"] <= value["global_caps"]["source_requests"] and consumed["bytes_charged"] <= value["global_caps"]["bytes"], "GDELT cumulative source costs differ or exceed frozen caps")
    caps = {"source_requests": value["request"].get("max_source_requests", 10**12), "bytes": value["request"].get("max_total_bytes", 10**12), "elapsed_seconds": value["request"].get("max_elapsed_seconds", 10**12)}
    require(value["global_caps"] == caps and consumed["elapsed_seconds"] <= caps["elapsed_seconds"], "GDELT original source caps or cumulative elapsed clock differs")
    manifest_cost = _manifest_cost(value["source_attempts"], value.get("source_reservations", []))
    plan = {"phase": "complete_manifests_frozen" if complete else "manifest_domain_incomplete", "window_file_count": len(files) if complete else None,
        "window_compressed_bytes": sum(row["bytes"] for row in files) if complete else None,
        "source_request_capacity": manifest_cost["source_requests"]+2*len(files), "source_byte_capacity": manifest_cost["bytes_charged"]+2*sum(row["bytes"] for row in files),
        "source_elapsed_capacity": manifest_cost["elapsed_seconds"]+2*len(files)*value["request"].get("timeout_seconds", 30),
        "basis": "every_original_window_file_from_both_complete_provider_manifests_before_reading_GKG_records"}
    require(value["execution_plan"] == plan, "GDELT execution scope was not frozen from its complete original manifests")
    require(value["status"] == ("metadata_window_discovered" if complete and all(row["status"] == "captured" for row in value["parsed_files"]) else "partial_metadata_window"), "GDELT metadata completion status differs from originals")
    require(value["original_news_fact_qualification"] is False and value["historical_PIT_qualification"] is False and value["trade_ready"] is False, "Opportunity metadata cannot grant facts, historical information or trades")
    return article_registry
