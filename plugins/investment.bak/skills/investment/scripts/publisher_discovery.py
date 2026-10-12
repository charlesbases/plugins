"""Fresh authority directories, publisher identities and original news entrances.

The directories define a finite, auditable institutional domain. They do not
define all news in the world. A discovered publisher is never a verified fact,
and an unavailable home page remains in the original denominator.
"""
import copy
import hashlib
import math
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from contracts import fields, fingerprint, instant, require, utc_now
import source_fetch
from source_journal import SourceJournal, validate_cost

DIRECTORIES = {
    "cn_departments": {"url": "https://www.gov.cn/home/2023-03/29/content_5748953.htm",
        "class": "trs_editor_view", "tag": None, "scope": "China_State_Council_department_links", "region": "domestic"},
    "bis_members": {"url": "https://www.bis.org/about/organisation/members",
        "class": "table-striped", "tag": "table", "scope": "BIS_member_central_banks", "region": "international"},
    "un_system": {"url": "https://www.un.org/en/about-us/un-system",
        "class": "field-name-body", "tag": None, "scope": "UN_system_body_institutional_links", "region": "international"},
}
CHANNEL = re.compile(r"news|press|media|release|publication|announcement|policy|statement|speech|communiqu|actualité|noticia|presse|新闻|资讯|发布|公告|政策|要闻|时政|政务|动态", re.I)
MAX_BYTES = 2*1024*1024


class _Links(HTMLParser):
    def __init__(self, selector=None):
        super().__init__(convert_charrefs=True)
        self.selector, self.stack, self.active, self.links, self.current = selector, [], [], [], None
        self.containers, self.closed = 0, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        void = tag in {"meta", "img", "input", "link", "br", "hr", "source", "wbr", "area", "base", "embed", "param", "track", "col"}
        if not void:
            self.stack.append(tag)
        if self.selector and (self.selector["tag"] is None or self.selector["tag"] == tag) and self.selector["class"] in attrs.get("class", "").split():
            self.active.append(len(self.stack)); self.containers += 1
        inside = self.selector is None or bool(self.active)
        if tag == "a" and attrs.get("href") and inside:
            self.current = {"href": attrs["href"], "parts": [], "line": self.getpos()[0], "column": self.getpos()[1], "kind": "anchor"}
        if tag == "link" and inside and attrs.get("href") and "alternate" in attrs.get("rel", "").split() and attrs.get("type", "").split(";", 1)[0].lower() in {"application/rss+xml", "application/atom+xml"}:
            self.links.append({"href": attrs["href"], "label": attrs.get("title", ""), "line": self.getpos()[0], "column": self.getpos()[1], "kind": "syndication"})

    def handle_data(self, value):
        if self.current and not any(tag in self.stack for tag in ("script", "style", "noscript")):
            self.current["parts"].append(value)

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            value = self.current; self.current = None
            self.links.append({key: item for key, item in value.items() if key != "parts"} | {"label": " ".join("".join(value["parts"]).split())})
        if tag not in self.stack:
            return
        index = len(self.stack)-1-self.stack[::-1].index(tag)
        closing = [depth for depth in self.active if index+1 <= depth]
        self.closed += len(closing)
        self.active = [depth for depth in self.active if depth not in closing]
        self.stack = self.stack[:index]


def _https(url):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        and parsed.port in (None, 80, 443), "Directory publisher URL is not a public HTTP(S) identity")
    return urllib.parse.urlunsplit(("https", parsed.hostname.lower(), parsed.path or "/", parsed.query, ""))


def _rule(hosts, *, purpose):
    return {"purpose": purpose, "allowed_hosts": sorted(set(hosts)), "redirect_hosts": sorted(set(hosts)),
        "path_pattern": "^/", "default_urls": [], "encoding": "utf-8", "media_types": ["text/html", "application/rss+xml", "application/atom+xml", "application/xml", "text/xml"], "timezone": "UTC"}


def directory_registry():
    return {"schema_version": 1, "sources": {"directory_"+name: _rule([urllib.parse.urlsplit(row["url"]).hostname], purpose="publisher_directory") for name, row in DIRECTORIES.items()}}


def parse_directory(raw, capture, directory_id):
    require(directory_id in DIRECTORIES, "Unknown authority directory protocol")
    original = DIRECTORIES[directory_id]
    registry = directory_registry()
    source_fetch.validate_capture_provenance(capture, registry)
    require(capture["requested_url"] == original["url"] and capture["registry_source_id"] == "directory_"+directory_id,
        "Directory capture does not match its authority URL")
    require(hashlib.sha256(raw).hexdigest() == capture["raw_sha256"] and len(raw) == capture["bytes"], "Directory raw bytes differ")
    text, encoding = source_fetch.decode(raw, capture["content_type"], capture["encoding"], registry["sources"][capture["registry_source_id"]])
    require(encoding == capture["encoding"] and hashlib.sha256(text.encode()).hexdigest() == capture["text_sha256"], "Directory decoded text differs")
    page = _Links(original); page.feed(text); page.close()
    require(page.containers > 0 and page.closed == page.containers and not page.active, "Authority directory body was not closed")
    entities, excluded = [], []
    for link in page.links:
        target = urllib.parse.urljoin(capture["final_url"], link["href"])
        try:
            url = _https(target)
        except ValueError:
            excluded.append({**link, "reason": "unsupported_institution_url"}); continue
        path = urllib.parse.urlsplit(url).path.lower()
        if not link["label"] or path.endswith((".pdf", ".jpg", ".png", ".zip")):
            excluded.append({**link, "reason": "non_institutional_document_link"}); continue
        if directory_id == "un_system" and urllib.parse.urlsplit(url).hostname == "www.un.org":
            excluded.append({**link, "reason": "UN_internal_navigation_not_external_publisher"}); continue
        entities.append({"entity_id": fingerprint({"directory_id": directory_id, "label": link["label"], "url": url}),
            "directory_id": directory_id, "label": link["label"], "original_href": link["href"], "https_probe_url": url,
            "publisher_host": urllib.parse.urlsplit(url).hostname, "region": original["region"],
            "locator": {"line": link["line"], "column": link["column"], "anchor_text": link["label"]},
            "scope": original["scope"], "source_raw_sha256": capture["raw_sha256"]})
    require(entities, "Authority directory contains no supported institutional entities")
    return {"entities": list({row["entity_id"]: row for row in entities}.values()), "excluded_links": excluded,
        "container_count": page.containers, "world_publisher_denominator": None}


def _publisher_rule(host):
    other = host[4:] if host.startswith("www.") else "www."+host
    return _rule([host, other], purpose="publisher_home")


def _entrances(raw, capture, rule):
    source_fetch.validate_capture_provenance(capture, {"schema_version": 1, "sources": {capture["registry_source_id"]: rule}})
    require(hashlib.sha256(raw).hexdigest() == capture["raw_sha256"] and len(raw) == capture["bytes"], "Publisher home bytes differ")
    text, encoding = source_fetch.decode(raw, capture["content_type"], capture["encoding"], rule)
    require(encoding == capture["encoding"] and hashlib.sha256(text.encode()).hexdigest() == capture["text_sha256"], "Publisher home text differs")
    parser = _Links(); parser.feed(text); parser.close()
    rows, excluded = [], []
    for link in parser.links:
        target = urllib.parse.urljoin(capture["final_url"], link["href"]).split("#", 1)[0]
        if link["kind"] != "syndication" and not CHANNEL.search(link["label"]):
            continue
        try:
            url = source_fetch.checked_url(_https(target), rule)
        except ValueError:
            excluded.append({**link, "reason": "news_entrance_outside_verified_publisher_host"}); continue
        rows.append({"url": url, "label": link["label"], "kind": link["kind"], "locator": {"line": link["line"], "column": link["column"]}})
    return {"entrances": list({row["url"]: row for row in rows}.values()), "excluded": excluded}


def _assembled(base_registry, directories, home_probes):
    registry = copy.deepcopy(base_registry)
    entities = [row for item in directories for row in item.get("parsed", {}).get("entities", [])]
    by_host = {}
    for row in entities:
        by_host.setdefault(row["publisher_host"], []).append(row)
    probes = {row["publisher_host"]: row for row in home_probes}
    publishers = []
    for host, members in sorted(by_host.items()):
        probe = probes.get(host)
        entrances = probe.get("parsed", {}).get("entrances", []) if probe else []
        source_id = "publisher_"+fingerprint({"host": host})[:24]
        rule = _publisher_rule(host)
        rule.update(purpose="news", default_urls=[row["url"] for row in entrances],
            collection_role="official_article", listing_adapter="semantic_site_v1", article_adapter="semantic_article_v1",
            publication_adapter="article_jsonld_v1", body_ids=[], body_classes=[], body_tags=["article"],
            article_pattern="^/", listing_pattern="^/", minimum_body_characters=60, timezone="Asia/Shanghai" if any(row["region"] == "domestic" for row in members) else "UTC",
            require_explicit_publication_offset=not any(row["region"] == "domestic" for row in members),
            timezone_basis="directory_China_domestic_publication_clock" if any(row["region"] == "domestic" for row in members) else "unknown_UTC_placeholder_never_qualifies_without_original_offset",
            original_news_entrances=[row["url"] for row in entrances],
            publisher_directory_entity_ids=[row["entity_id"] for row in members],
            complete_explicit_url_scope=True, max_follow_links=5000 if entrances else 0, max_listing_pages=10)
        # All discovered institutions stay in the publisher denominator. A
        # missing channel is registered as pending, never silently dropped.
        if not rule["default_urls"]:
            rule["default_urls"] = [members[0]["https_probe_url"]]
        registry["sources"][source_id] = rule
        publishers.append({"publisher_host": host, "source_id": source_id, "entity_ids": [row["entity_id"] for row in members],
            "entrances": entrances, "status": "original_entrances_discovered" if entrances else "publisher_news_protocol_pending",
            "home_probe_status": probe["status"] if probe else "not_examined"})
    return registry, entities, publishers


def _stored_capture(capture, artifacts):
    return {"capture": {key: value for key, value in capture.items() if key not in {"raw_bytes", "text"}}, "raw_ref": artifacts.put_bytes(capture["raw_bytes"])}


def _limits(payload, publisher_count, timeout):
    count = len(DIRECTORIES)+publisher_count
    result = {"source_requests": count, "bytes": count*MAX_BYTES, "elapsed_seconds": count*timeout}
    for parameter, key in (("max_source_requests", "source_requests"), ("max_total_bytes", "bytes"), ("max_elapsed_seconds", "elapsed_seconds")):
        if parameter in payload:
            require(type(payload[parameter]) is int and payload[parameter] > 0, "Positive publisher budget required")
            result[key] = min(result[key], payload[parameter])
    return result


def _coverage(directories, entities, publishers):
    return {"directory_count": len(DIRECTORIES), "captured_directories": sum(row["status"] == "captured" for row in directories),
        "directory_entities": len(entities), "unique_publisher_hosts": len(publishers),
        "publisher_entrances_ready": sum(row["status"] == "original_entrances_discovered" for row in publishers),
        "pending_publishers": sum(row["status"] != "original_entrances_discovered" for row in publishers),
        "world_publisher_denominator": None, "world_news_denominator": None, "news_time_window_exhaustiveness_proven": False,
        "scope_gap": "authority_directories_do_not_enumerate_all_company_market_local_or_international_news"}


def discover(payload, store, artifacts, operation_id):
    fields(payload, set(), {"timeout_seconds", "max_source_requests", "max_total_bytes", "max_elapsed_seconds", "gdelt_window"}, "publisher discovery")
    store.assert_owned()
    require(type(operation_id) is str and operation_id, "Stable publisher discovery operation required")
    timeout = payload.get("timeout_seconds", 8)
    require(type(timeout) in {int, float} and 0 < timeout <= 30, "Publisher timeout must be bounded")
    request_hash = fingerprint(payload)
    previous = store.get("publisher-discovery", operation_id)
    if previous:
        require(previous["request_hash"] == request_hash, "Publisher discovery inputs changed")
        validate(previous, store, artifacts)
        return previous
    journal = SourceJournal(store, "publisher-source-attempt", operation_id, payload)
    base_seal = store.get("publisher-base-registry-seal", operation_id)
    base = artifacts.read_json(base_seal["registry_ref"]) if base_seal else source_fetch.load_registry()
    base_ref = base_seal["registry_ref"] if base_seal else artifacts.put_json(base)
    store.put("publisher-base-registry-seal", operation_id, {"registry_ref": base_ref, "registry_hash": fingerprint(base)}, immutable=True)
    directories, probes, elapsed = [], [], 0
    used = journal.used
    registry = directory_registry()
    started = time.monotonic()
    # This first budget is known before any current article content is read.
    # The publisher phase is frozen from the complete original directories.
    limits = _limits(payload, 0, timeout)
    attempts = journal.attempts
    recovery_kind = "publisher-source-recovery-seal"
    blocked = set()
    for key, seal in store.scan(recovery_kind):
        if key.startswith(operation_id + ":recovery:"):
            blocked.update(seal["blocked_attempt_ids"])
    verified_originals = {}
    for attempt in attempts:
        if attempt["status"] != "captured" or attempt["attempt_id"] in blocked:
            continue
        original = journal.original(attempt["attempt_id"])
        if original is None:
            blocked.add(attempt["attempt_id"])
            continue
        try:
            raw = artifacts.read(original["raw_ref"])
        except FileNotFoundError:
            blocked.add(attempt["attempt_id"])
            continue
        require(hashlib.sha256(raw).hexdigest() == attempt["raw_sha256"]
                and len(raw) == attempt["bytes_charged"], "Original publisher CAS changed")
        verified_originals[attempt["attempt_id"]] = (original, raw)
    if attempts:
        seal = {"attempt_ids": [row["attempt_id"] for row in attempts], "blocked_attempt_ids": sorted(blocked)}
        store.put(recovery_kind, operation_id + ":recovery:" + fingerprint(seal), seal, immutable=True)
    def permitted():
        return used["source_requests"]+1 <= limits["source_requests"] and used["bytes_charged"]+MAX_BYTES <= limits["bytes"] and used["elapsed_seconds"]+timeout <= limits["elapsed_seconds"]
    def recovered(url, source_id, current_registry):
        for attempt in reversed(attempts):
            if (attempt["requested_url"] != url or attempt["source_id"] != source_id
                    or attempt["registry_hash"] != fingerprint(current_registry)):
                continue
            if attempt["status"] == "unavailable":
                return {"error": attempt["reason"]}
            if attempt["attempt_id"] in blocked:
                continue  # An old SHA alone cannot manufacture a TLS receipt.
            pair = verified_originals.get(attempt["attempt_id"])
            original = pair[0] if pair else journal.original(attempt["attempt_id"])
            if original is None:
                continue
            capture = copy.deepcopy(original["capture"])
            raw = pair[1] if pair else artifacts.read(original["raw_ref"])
            source_fetch.validate_capture_provenance(capture, current_registry)
            require(capture["requested_url"] == url and capture["registry_source_id"] == source_id
                    and capture["source_attempt_id"] == attempt["attempt_id"]
                    and capture["raw_sha256"] == attempt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
                    and capture["bytes"] == attempt["bytes_charged"] == len(raw)
                    and capture["retrieved_at"] == attempt["retrieved_at"],
                    "Recovered publisher capture differs from its original receipt")
            return {"capture": {**capture, "raw_bytes": raw}}
        return None
    def obtain(url, source_id, registry):
        before = time.monotonic()
        reservation = journal.reserve(url, MAX_BYTES, timeout, source_id=source_id, registry_hash=fingerprint(registry))
        attempt = {"attempt_id": reservation["attempt_id"], "requested_url": url, "source_id": source_id,
            "registry_hash": fingerprint(registry), "started_at": reservation["started_at"], "timeout_seconds": timeout, "max_bytes": MAX_BYTES}
        original = None
        try:
            result = source_fetch.fetch(url, source_id, timeout=timeout, max_bytes=MAX_BYTES, registry=registry)
            result["source_attempt_id"] = attempt["attempt_id"]
            original = _stored_capture(result, artifacts)
            attempt.update(status="captured", bytes_charged=len(result["raw_bytes"]), raw_sha256=result["raw_sha256"], retrieved_at=result["retrieved_at"])
            return result
        except (OSError, ValueError) as error:
            attempt.update(status="unavailable", bytes_charged=MAX_BYTES, reason=str(error))
            raise
        finally:
            amount = time.monotonic()-before
            attempt.update(ended_at=utc_now(), elapsed_seconds=amount)
            # BaseException/SIGTERM leaves the pre-network reservation charged.
            if "status" in attempt:
                journal.commit(attempt, original)
    for name, definition in DIRECTORIES.items():
        prior = recovered(definition["url"], "directory_"+name, registry)
        if prior is None and not permitted():
            directories.append({"directory_id": name, "status": "not_examined_global_budget", "parsed": {"entities": []}}); continue
        capture = None
        try:
            if prior and "error" in prior:
                raise OSError(prior["error"])
            capture = prior["capture"] if prior else obtain(definition["url"], "directory_"+name, registry)
            directories.append({"directory_id": name, "status": "captured", **_stored_capture(capture, artifacts), "parsed": parse_directory(capture["raw_bytes"], capture, name)})
        except (OSError, ValueError) as error:
            directories.append({"directory_id": name, "status": "protocol_unproved" if capture else "unavailable", "reason": str(error), "parsed": {"entities": []}, **(_stored_capture(capture, artifacts) if capture else {})})
    subjects = [row for item in directories for row in item["parsed"]["entities"]]
    hosts = sorted({row["publisher_host"] for row in subjects})
    limits = _limits(payload, len(hosts), timeout)
    # Only network reads are concurrent. The owner reserves the full wave
    # before dispatch and alone commits capture/attempt records to this plan.
    # A four-request wave is transport scheduling, never a publisher filter.
    def home_read(job):
        begun = time.monotonic()
        try:
            capture = source_fetch.fetch(job["target"], job["source_id"], timeout=timeout, max_bytes=MAX_BYTES, registry=job["registry"])
            return {"capture": capture, "elapsed_seconds": time.monotonic()-begun, "ended_at": utc_now()}
        except (OSError, ValueError) as error:
            return {"error": str(error), "elapsed_seconds": time.monotonic()-begun, "ended_at": utc_now()}
    def home_probe(job, fetched):
        capture = fetched.get("capture")
        try:
            if not capture:
                raise ValueError(fetched["error"])
            probes.append({"publisher_host": job["host"], "status": "captured", "rule": job["rule"],
                           **_stored_capture(capture, artifacts), "parsed": _entrances(capture["raw_bytes"], capture, job["rule"])})
        except ValueError as error:
            probes.append({"publisher_host": job["host"], "status": "protocol_unproved" if capture else "unavailable",
                           "rule": job["rule"], "reason": str(error), **(_stored_capture(capture, artifacts) if capture else {})})
    with ThreadPoolExecutor(max_workers=4) as pool:
        cursor = 0
        while cursor < len(hosts):
            wave = []
            batch = hosts[cursor:cursor+4]
            for host in batch:
                source_id = "home_"+fingerprint({"host": host})[:24]; rule = _publisher_rule(host)
                target = next(row["https_probe_url"] for row in subjects if row["publisher_host"] == host)
                job = {"host": host, "source_id": source_id, "rule": rule, "target": target,
                       "registry": {"schema_version": 1, "sources": {source_id: rule}}}
                prior = recovered(target, source_id, job["registry"])
                if prior is not None:
                    home_probe(job, prior)
                    continue
                if not permitted():
                    probes.append({"publisher_host": host, "status": "not_examined_global_budget"})
                    continue
                job["reservation"] = journal.reserve(target, MAX_BYTES, timeout,
                    source_id=source_id, registry_hash=fingerprint(job["registry"]))
                wave.append(job)
            cursor += len(batch)
            if not wave:
                continue
            pending_jobs = [(job, pool.submit(home_read, job)) for job in wave]
            for job, future in pending_jobs:
                fetched = future.result(); capture = fetched.get("capture")
                reservation = job["reservation"]
                attempt = {"attempt_id": reservation["attempt_id"], "requested_url": job["target"], "source_id": job["source_id"],
                    "registry_hash": fingerprint(job["registry"]), "started_at": reservation["started_at"], "timeout_seconds": timeout, "max_bytes": MAX_BYTES,
                    "ended_at": fetched["ended_at"], "elapsed_seconds": fetched["elapsed_seconds"], "status": "captured" if capture else "unavailable",
                    "bytes_charged": len(capture["raw_bytes"]) if capture else MAX_BYTES}
                if capture:
                    capture["source_attempt_id"] = attempt["attempt_id"]
                    attempt.update(raw_sha256=capture["raw_sha256"], retrieved_at=capture["retrieved_at"])
                else:
                    attempt["reason"] = fetched["error"]
                journal.commit(attempt, _stored_capture(capture, artifacts) if capture else None)
                home_probe(job, fetched)
    built, entities, publishers = _assembled(base, directories, probes)
    gdelt = None
    if "gdelt_window" in payload:
        import gdelt_opportunities
        fields(payload["gdelt_window"], {"window_start", "window_end"}, label="full GDELT opportunity window")
        gdelt_payload = {**payload["gdelt_window"], "timeout_seconds": timeout}
        remaining = {}
        for parent_key, consumed_key in (("max_source_requests", "source_requests"), ("max_total_bytes", "bytes_charged"), ("max_elapsed_seconds", "elapsed_seconds")):
            if parent_key in payload:
                remaining[parent_key] = max(0, math.floor(payload[parent_key]-used[consumed_key]))
        if all(amount > 0 for amount in remaining.values()):
            gdelt_payload.update(remaining)
            gdelt = gdelt_opportunities.discover(gdelt_payload, store, artifacts, operation_id+":gdelt")
            dynamic = gdelt_opportunities.validate(gdelt, store, artifacts)
            built["sources"].update(dynamic["sources"])
    result = {"schema_version": 1, "discovery_id": operation_id, "request_hash": request_hash, "request": copy.deepcopy(payload),
        "captured_at": utc_now(), "base_registry_ref": base_ref, "source_registry_ref": artifacts.put_json(built),
        "directory_captures": directories, "publisher_home_probes": probes, "directory_entities": entities, "publishers": publishers,
        "global_budget": limits, "consumed": used, "source_attempts": attempts,
        "source_reservations": journal.reservations,
        "charged_unreplayed_source_attempts": [attempt["attempt_id"] for attempt in attempts if attempt["status"] == "captured"
            and not any(row.get("capture", {}).get("source_attempt_id") == attempt["attempt_id"] for row in directories+probes)],
        "coverage": _coverage(directories, entities, publishers),
        "status": "publisher_domain_discovered" if all(row["status"] == "captured" for row in directories) else "partial_publisher_domain",
        "trade_ready": False, "all_news_coverage_claimed": False,
        "scope": "fresh_complete_links_of_captured_authority_directories_not_all_world_publishers_or_articles"}
    if "gdelt_window" in payload:
        result.update(gdelt_window=copy.deepcopy(payload["gdelt_window"]), gdelt_discovery_id=gdelt["discovery_id"] if gdelt else None,
            gdelt_opportunity_ref=artifacts.put_json(gdelt) if gdelt else None,
            gdelt_coverage=gdelt["coverage"] if gdelt else {"manifest_window_files": None, "complete_captured_GDELT_window_domain": False,
                "global_news_denominator": None, "reason": "parent_global_budget_exhausted_before_full_opportunity_manifest"},
            overall_consumed={"source_requests": used["source_requests"]+(gdelt["consumed"]["source_requests"] if gdelt else 0),
                "bytes_charged": used["bytes_charged"]+(gdelt["consumed"]["bytes_charged"] if gdelt else 0),
                "elapsed_seconds": used["elapsed_seconds"]+(gdelt["consumed"]["elapsed_seconds"] if gdelt else 0)},
            scope="fresh_authority_directories_and_complete_GDELT_monitored_window_when_all_metadata_files_captured_not_all_world_news_or_article_facts")
        if gdelt is None or gdelt["status"] != "metadata_window_discovered":
            result["status"] = "partial_publisher_domain"
    validate(result, store, artifacts, require_committed=False)
    store.put("publisher-discovery", operation_id, result, immutable=True)
    return result


def validate(value, store, artifacts, *, require_committed=True):
    require(value["schema_version"] == 1 and value["request_hash"] == fingerprint(value["request"]), "Publisher discovery request differs")
    request_seal = store.get("publisher-source-attempt-request", value["discovery_id"])
    if request_seal is not None:
        require(request_seal == {"request": value["request"], "request_hash": value["request_hash"]},
                "Publisher final receipt changed its frozen source request/budget")
    if require_committed:
        require(store.get("publisher-discovery", value["discovery_id"]) == value, "Publisher discovery is not its canonical committed record")
    rebuilt_directories, rebuilt_probes = [], []
    require([row["directory_id"] for row in value["directory_captures"]] == list(DIRECTORIES), "Publisher directory scope omitted an authority")
    for row in value["directory_captures"]:
        rebuilt = copy.deepcopy(row)
        if row["status"] == "captured":
            parsed = parse_directory(artifacts.read(row["raw_ref"]), row["capture"], row["directory_id"])
            require(parsed == row["parsed"], "Directory institution facts differ from original bytes")
        elif row["status"] == "protocol_unproved":
            try:
                parse_directory(artifacts.read(row["raw_ref"]), row["capture"], row["directory_id"])
            except ValueError as error:
                require(str(error) == row["reason"] and row["parsed"] == {"entities": []}, "Unsupported directory protocol reason differs")
            else:
                require(False, "A supported directory was labeled unproved")
        else:
            require(row["status"] in {"unavailable", "not_examined_global_budget"} and (row["status"] != "unavailable" or row.get("reason")) and row["parsed"] == {"entities": []}, "Missing directory cannot manufacture entities")
        rebuilt_directories.append(rebuilt)
    hosts = {row["publisher_host"] for item in rebuilt_directories for row in item["parsed"]["entities"]}
    require({row["publisher_host"] for row in value["publisher_home_probes"]} == hosts and len(value["publisher_home_probes"]) == len(hosts), "Home probe scope differs from complete directory institutions")
    for row in value["publisher_home_probes"]:
        if row.get("capture"):
            target = next(entity["https_probe_url"] for item in rebuilt_directories for entity in item["parsed"]["entities"] if entity["publisher_host"] == row["publisher_host"])
            require(row["capture"]["requested_url"] == target, "Publisher home target differs from its original directory entity")
        if row["status"] == "captured":
            require(row["rule"] == _publisher_rule(row["publisher_host"]), "Publisher home authorization changed")
            target = next(entity["https_probe_url"] for item in rebuilt_directories for entity in item["parsed"]["entities"] if entity["publisher_host"] == row["publisher_host"])
            require(row["capture"]["requested_url"] == target, "Publisher home target differs from its original directory entity")
            parsed = _entrances(artifacts.read(row["raw_ref"]), row["capture"], row["rule"])
            require(parsed == row["parsed"], "Publisher news entrances differ from original bytes")
        elif row["status"] == "protocol_unproved":
            require(row["rule"] == _publisher_rule(row["publisher_host"]), "Unsupported home authorization changed")
            try:
                _entrances(artifacts.read(row["raw_ref"]), row["capture"], row["rule"])
            except ValueError as error:
                require(str(error) == row["reason"], "Unsupported home protocol reason differs")
            else:
                require(False, "A supported home was labeled unproved")
        else:
            require(row["status"] in {"unavailable", "not_examined_global_budget"} and not row.get("parsed"), "Missing publisher home cannot manufacture a news entrance")
        rebuilt_probes.append(copy.deepcopy(row))
    registry, entities, publishers = _assembled(artifacts.read_json(value["base_registry_ref"]), rebuilt_directories, rebuilt_probes)
    gdelt = None
    if "gdelt_window" in value["request"]:
        require(value.get("gdelt_window") == value["request"]["gdelt_window"], "Original GDELT opportunity window changed")
        if value.get("gdelt_discovery_id"):
            import gdelt_opportunities
            gdelt = store.get("gdelt-opportunity-discovery", value["gdelt_discovery_id"])
            require(gdelt is not None and artifacts.read_json(value["gdelt_opportunity_ref"]) == gdelt and gdelt["request"]["window_start"] == value["gdelt_window"]["window_start"] and gdelt["request"]["window_end"] == value["gdelt_window"]["window_end"], "GDELT source domain differs from canonical window discovery")
            registry["sources"].update(gdelt_opportunities.validate(gdelt, store, artifacts)["sources"])
            require(value["gdelt_coverage"] == gdelt["coverage"], "GDELT monitored-domain coverage differs")
        else:
            require(value.get("gdelt_opportunity_ref") is None and value["gdelt_coverage"] == {"manifest_window_files": None, "complete_captured_GDELT_window_domain": False,
                "global_news_denominator": None, "reason": "parent_global_budget_exhausted_before_full_opportunity_manifest"}, "Missing metadata cannot manufacture an opportunity denominator")
    base_seal = store.get("publisher-base-registry-seal", value["discovery_id"])
    require(base_seal == {"registry_ref": value["base_registry_ref"], "registry_hash": fingerprint(artifacts.read_json(value["base_registry_ref"]))}, "Original installed base registry seal differs")
    require(artifacts.read_json(value["source_registry_ref"]) == registry and value["directory_entities"] == entities and value["publishers"] == publishers,
        "Dynamic source registry differs from original authority and home bytes")
    require(value["coverage"] == _coverage(rebuilt_directories, entities, publishers), "Publisher coverage statistics differ from complete original domain")
    complete = all(row["status"] == "captured" for row in rebuilt_directories) and ("gdelt_window" not in value["request"] or gdelt is not None and gdelt["status"] == "metadata_window_discovered")
    require(value["status"] == ("publisher_domain_discovered" if complete else "partial_publisher_domain"), "Publisher directory completeness status differs")
    require(value["global_budget"] == _limits(value["request"], len(publishers), value["request"].get("timeout_seconds", 8)), "Publisher global budget differs from original entity count")
    attempts = value["source_attempts"]
    require(len({row["attempt_id"] for row in attempts}) == len(attempts), "Publisher source attempt was duplicated")
    consumed = validate_cost(store, "publisher-source-attempt", value["discovery_id"], attempts,
                             value.get("source_reservations", []))
    captures = [row["capture"] for row in rebuilt_directories+rebuilt_probes if row.get("capture")]
    blocked = set()
    for key, seal in store.scan("publisher-source-recovery-seal"):
        if key.startswith(value["discovery_id"] + ":recovery:"):
            blocked.update(seal["blocked_attempt_ids"])
    require(all(capture.get("source_attempt_id") not in blocked for capture in captures),
            "Missing original publisher CAS was backfilled from a later capture")
    unreplayed = []
    for attempt in attempts:
        require(store.get("publisher-source-attempt", attempt["attempt_id"]) == attempt, "Publisher attempt differs from its original controller record")
        require(instant(attempt["started_at"]) <= instant(attempt["ended_at"]) <= instant(value["captured_at"]), "Publisher capture clocks are reversed or future")
        require(type(attempt["elapsed_seconds"]) in {int, float} and 0 <= attempt["elapsed_seconds"], "Publisher source elapsed time is invalid")
        if attempt["status"] == "captured":
            matches = [capture for capture in captures if capture["requested_url"] == attempt["requested_url"]
                and capture["registry_source_id"] == attempt["source_id"]
                and capture.get("source_attempt_id", attempt["attempt_id"]) == attempt["attempt_id"]
                and capture["retrieved_at"] == attempt["retrieved_at"]]
            if not matches and "charged_unreplayed_source_attempts" in value:
                unreplayed.append(attempt["attempt_id"])
            else:
                require(len(matches) == 1 and attempt["bytes_charged"] == matches[0]["bytes"] and attempt["raw_sha256"] == matches[0]["raw_sha256"] and instant(attempt["started_at"]) <= instant(attempt["retrieved_at"]) <= instant(attempt["ended_at"]), "Publisher charged capture differs from original attempt")
        else:
            require(attempt["status"] == "unavailable" and attempt["bytes_charged"] == MAX_BYTES and attempt.get("reason"), "Failed publisher source must consume its full byte reservation")
    if "charged_unreplayed_source_attempts" in value:
        require(value["charged_unreplayed_source_attempts"] == unreplayed,
                "Publisher original capture omissions differ from charged history")
    else:
        require(len(captures) == sum(attempt["status"] == "captured" for attempt in attempts),
                "Publisher source captures and original charged attempts differ")
    require(consumed == value["consumed"] and consumed["source_requests"] <= value["global_budget"]["source_requests"] and consumed["bytes_charged"] <= value["global_budget"]["bytes"], "Publisher cumulative resource costs differ or exceed the frozen budget")
    require(consumed["elapsed_seconds"] <= value["global_budget"]["elapsed_seconds"], "Publisher cumulative source time exceeds its frozen budget")
    if "gdelt_window" in value["request"]:
        overall = {key: amount+(gdelt["consumed"][key] if gdelt else 0) for key, amount in consumed.items()}
        require(overall == value["overall_consumed"], "Whole directory/opportunity cumulative resource charge differs")
        for parent_key, key in (("max_source_requests", "source_requests"), ("max_total_bytes", "bytes_charged"), ("max_elapsed_seconds", "elapsed_seconds")):
            if parent_key in value["request"]:
                require(overall[key] <= value["request"][parent_key], "Full news-discovery resource cost exceeds original parent cap")
    require(value["trade_ready"] is False and value["all_news_coverage_claimed"] is False and value["coverage"]["world_news_denominator"] is None
        and value["coverage"]["world_publisher_denominator"] is None and value["coverage"]["news_time_window_exhaustiveness_proven"] is False, "Publisher discovery cannot grant global news or financial qualification")
    return registry
