"""Source-backed news capture; reports evidence scope, never invents analysis."""

import datetime as dt
import hashlib
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from contracts import EvidenceError, fields, fingerprint, instant, require, strict_json_loads, utc_now
import source_fetch


class _Page(HTMLParser):
    def __init__(self, body_ids, body_classes=(), publication_classes=(), body_ancestor_tags=()):
        super().__init__(convert_charrefs=True)
        self.body_ids = set(body_ids)
        self.body_classes = set(body_classes)
        self.publication_classes = set(publication_classes)
        self.body_ancestor_tags = set(body_ancestor_tags)
        self.publication_depth, self.publication_parts = None, []
        self.stack, self.parts, self.links, self.meta, self.title = [], [], [], {}, []
        self.target_depth = None
        self.body_closed = False
        self.body_found = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("content"):
            self.meta[(attributes.get("name") or attributes.get("property") or "").lower()] = attributes["content"]
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if tag in {"br", "hr", "meta", "img", "input", "link", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            if self.target_depth is not None:
                self.parts.append("\n")
            return
        self.stack.append(tag)
        if self.publication_classes.intersection(attributes.get("class", "").split()):
            self.publication_depth = len(self.stack)
        if (attributes.get("id") in self.body_ids or self.body_classes.intersection(attributes.get("class", "").split())) and not self.body_found and self.body_ancestor_tags <= set(self.stack[:-1]):
            self.body_found, self.target_depth = True, len(self.stack)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        if self.publication_depth is not None and index + 1 <= self.publication_depth:
            self.meta["pubdate"] = "".join(self.publication_parts).strip()
            self.publication_depth = None
        if self.target_depth is not None and index + 1 <= self.target_depth:
            self.body_closed = True
            self.target_depth = None
        self.stack = self.stack[:index]

    def handle_data(self, data):
        if any(tag in self.stack for tag in ("script", "style", "noscript")):
            return
        if "title" in self.stack:
            self.title.append(data)
        if self.publication_depth is not None:
            self.publication_parts.append(data)
        if self.target_depth is not None:
            self.parts.append(data)


def _publication(meta, keys=("pubdate", "publishdate", "publish_date", "article:published_time", "firstpublishedtime"), *, timezone, formats=()):
    raw = next((meta[key] for key in keys if key in meta), None)
    if raw is None:
        return None
    value = raw.strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        dt.date.fromisoformat(value)
        return {"value": value, "precision": "date", "timezone": timezone, "basis": "publisher_metadata"}
    try:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
            for pattern in formats:
                try:
                    parsed = dt.datetime.strptime(value, pattern)
                    if not any(token in pattern for token in ("%H", "%M", "%S")):
                        return {"value": parsed.date().isoformat(), "precision": "date", "timezone": timezone,
                                "basis": "publisher_metadata_registered_format"}
                    break
                except ValueError:
                    continue
            if parsed is None:
                raise ValueError("Unknown publisher date format")
        if parsed.tzinfo is None:
            zone = ZoneInfo(timezone)
            require(parsed.replace(tzinfo=zone, fold=0).utcoffset() == parsed.replace(tzinfo=zone, fold=1).utcoffset(),
                    "Ambiguous publisher local clock requires an explicit UTC offset")
            parsed = parsed.replace(tzinfo=zone)
        return {"value": parsed.isoformat(), "precision": "timestamp", "timezone": timezone, "basis": "publisher_metadata"}
    except ValueError:
        return {"value": raw, "precision": "unparsed", "timezone": timezone, "basis": "publisher_metadata"}


def parse_capture(capture, rule, *, linked_parent=None):
    """A closed registered article container is a supported body, not true news."""
    text = capture["text"]
    path = urllib.parse.urlsplit(capture["final_url"]).path
    if rule.get("feed_format") == "rss" and capture["content_type"] in ("application/rss+xml", "application/xml", "text/xml"):
        require(text is not None and "<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper(), "Unsupported RSS document declarations")
        root = ET.fromstring(text)
        rows = root.findall("./channel/item")
        require(root.tag == "rss" and len(rows) <= 5000, "Official RSS feed shape changed")
        links = [row.findtext("link") for row in rows]
        require(all(type(link) is str and link for link in links), "RSS article link is missing")
        return {"state": "lead", "reason": "official_rss_is_discovery_not_article_body", "body": None,
                "links": links, "listing_links": [], "parser_id": "official_rss_v1"}
    if rule.get("listing_adapter") == "csrc_json" and capture["content_type"] == "application/json":
        data = strict_json_loads(text).get("data")
        require(type(data) is dict and type(data.get("results")) is list and len(data["results"]) <= 100,
                "CSRC current news API shape changed")
        rows = data["results"]
        require(all(type(row) is dict and type(row.get("url")) is str and type(row.get("title")) is str for row in rows),
                "CSRC news rows need original article URLs/titles")
        page, size, total = data.get("page"), data.get("rows"), data.get("total")
        require(type(page) is int and page > 0 and type(size) is int and 0 < size <= 100
                and type(total) is int and total >= 0, "CSRC pagination metadata is invalid")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(capture["final_url"]).query)
        following = []
        if page * size < total:
            query["page"] = [str(page+1)]
            following = [urllib.parse.urlunsplit(("https", urllib.parse.urlsplit(capture["final_url"]).netloc,
                         path, urllib.parse.urlencode(query, doseq=True), ""))]
        return {"state": "lead", "reason": "official_api_is_discovery_not_article_body", "body": None,
                "links": [row["url"] for row in rows], "listing_links": following, "parser_id": "csrc_current_news_api_v1"}
    if rule.get("listing_adapter") == "nhsa_records" and re.search(rule["listing_pattern"], path):
        records = re.findall(r"<record>\s*<!\[CDATA\[(.*?)\]\]>\s*</record>", text, re.S)
        require(records and len(records) <= 5000, "NHSA original listing record layout changed")
        page = _Page([])
        for record in records:
            page.feed(record)
        page.close()
        links = [link for link in page.links if re.search(rule["article_pattern"], urllib.parse.urlsplit(link).path)]
        require(links, "NHSA original records contain no article links")
        return {"state": "lead", "reason": "official_records_are_discovery_not_article_body", "body": None,
                "links": list(dict.fromkeys(links)), "listing_links": [], "parser_id": "nhsa_original_records_v1"}
    if capture["content_type"] in ("application/pdf", "application/octet-stream") and rule.get("pdf_attachments"):
        from source_documents import extract_document, DocumentNeedsReview
        try:
            document = extract_document(capture["raw_bytes"], capture["content_type"])
        except DocumentNeedsReview as error:
            return {"state": "partial", "reason": str(error), "body": None, "links": []}
        body = "\n".join(row["text"] for row in document["blocks"] if row["kind"] == "pdf_page")
        if linked_parent is None or linked_parent.get("state") != "body" or not linked_parent.get("published_at"):
            return {"state": "partial", "reason": "pdf_requires_verified_linked_article_publication", "body": body, "links": []}
        return {"state": "body", "reason": None, "body": body, "title": linked_parent["title"]+" [PDF attachment]",
                "published_at": {**linked_parent["published_at"], "basis": "linked_article_metadata_not_independent_pdf_release"}, "modified_at": linked_parent["modified_at"],
                "links": [], "parser_id": "linked_official_pdf_text_v1", "fact_verification": "not_performed"}
    if text is not None and urllib.parse.urlsplit(capture["final_url"]).path == rule.get("feed_path"):
        rows = strict_json_loads(text)
        require(isinstance(rows, list) and len(rows) <= 5000
                and all(isinstance(row, dict) and isinstance(row.get("URL"), str)
                        and isinstance(row.get("TITLE"), str) for row in rows), "Official news feed shape changed")
        return {"state": "lead", "reason": "official_list_is_discovery_not_article_body", "body": None,
                "links": [row["URL"] for row in rows], "parser_id": "gov_news_list_v1"}
    if text is None or capture["content_type"] != "text/html":
        return {"state": "partial", "reason": "unsupported_article_media", "body": None, "links": []}
    page = _Page(rule.get("body_ids", []), rule.get("body_classes", []), rule.get("publication_classes", []), rule.get("body_ancestor_tags", []))
    page.feed(text)
    page.close()
    body = re.sub(r"[ \t\r\f\v]+", " ", "".join(page.parts)).strip()
    title = page.meta.get("articletitle") or page.meta.get("og:title") or page.meta.get("citation_title") or "".join(page.title).strip()
    blocked = any(word in title.lower() for word in ("access denied", "captcha", "验证码", "访问验证"))
    if blocked:
        state, reason = "unavailable", "access_challenge"
    elif not page.body_found:
        state, reason = "lead", "no_registered_article_body"
    elif not page.body_closed or len(body) < rule.get("minimum_body_characters", 60):
        state, reason = "partial", "incomplete_or_insufficient_body"
    else:
        state, reason = "body", None
    try:
        published = _publication(page.meta, timezone=rule["timezone"], formats=rule.get("publication_formats", ()))
        modified = _publication(page.meta, ("lastmodifiedtime", "article:modified_time"), timezone=rule["timezone"],
                                formats=rule.get("publication_formats", ()))
    except ValueError:
        published, modified = None, None
    listing_links = []
    if state == "lead" and rule.get("listing_adapter") == "csrc_json":
        channel = page.meta.get("channelid")
        require(type(channel) is str and re.fullmatch(r"[a-f0-9]{32}", channel), "CSRC current list has no valid channel identity")
        listing_links = ["https://www.csrc.gov.cn/searchList/"+channel+"?_isAgg=true&_isJson=true&_pageSize=18&_template=index&page=1"]
    if state == "lead" and rule.get("listing_adapter") == "ndrc_static":
        match = re.search(r"createPageHTML\(\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*[\"']index[\"']\s*,\s*[\"']html[\"']\s*\)", text)
        if match and int(match[2])+1 < int(match[1]):
            listing_links = [urllib.parse.urljoin(capture["final_url"], "index_"+str(int(match[2])+1)+".html")]
    return {"state": state, "reason": reason, "body": body or None, "title": title,
            "published_at": published, "modified_at": modified, "links": page.links, "listing_links": listing_links,
            "parser_id": "registered_article_container_v1", "fact_verification": "not_performed"}


def _document_url(url):
    parsed = urllib.parse.urlsplit(url)
    # Query parameters can identify different official documents. Tracking-only
    # aliases require an explicit source rule or numbered-event evidence.
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _publication_relation(publication, cutoff):
    """Publisher precision gives an interval, never an invented publication hour."""
    if publication is None or publication["precision"] not in ("date", "timestamp"):
        return "unknown"
    if publication["precision"] == "timestamp":
        return "proved_before" if instant(publication["value"]) <= cutoff else "after"
    day = dt.date.fromisoformat(publication["value"])
    cutoff_day = cutoff.astimezone(ZoneInfo(publication["timezone"])).date()
    return "proved_before" if day < cutoff_day else "overlaps_date_interval" if day == cutoff_day else "after"


def _body_available(publication, captured, information_cutoff):
    if captured > information_cutoff or publication is None or publication["precision"] not in ("date", "timestamp"):
        return False
    # An actual capture proves availability, not a publisher's missing hour.
    if publication["precision"] == "timestamp":
        return instant(publication["value"]) <= captured
    return dt.date.fromisoformat(publication["value"]) <= captured.astimezone(
        ZoneInfo(publication["timezone"])).date()


def _article_url(url, rule):
    return bool(re.search(rule.get("article_pattern", r"$^"), urllib.parse.urlsplit(url).path))


class LinkedPDFBudget(EvidenceError):
    def __init__(self, source_id):
        super().__init__("Linked PDF and its original parent require max_urls_per_source >= 2")
        self.required_actions = [{"action": "set_atomic_parent_pdf_request_budget", "source_id": source_id,
                                  "minimum_max_urls_per_source": 2}]


def _next_task(pending, rule, kind):
    """Round robin body and discovery tasks without jumping ahead of their FIFO."""
    wanted = kind == "article"
    index = next((i for i, url in enumerate(pending) if _article_url(url, rule) == wanted), 0)
    url = pending.pop(index)
    return url, "listing" if _article_url(url, rule) else "article"


def run_policy(run_id, store, artifacts):
    """Read the authoritative immutable source policy and registry snapshot."""
    require(type(run_id) is str and run_id, "Authoritative analysis run required")
    run = store.get("analysis_run", run_id)
    require(run is not None and run.get("free_news_only") is True, "Unknown free-news analysis run")
    require(run.get("registry_snapshot_ref"), "Analysis run lacks its immutable registry snapshot")
    registry = artifacts.read_json(run["registry_snapshot_ref"])
    policy = run["news_policy"]
    require(run["source_registry_hash"] == fingerprint(registry)
            and run["news_policy_hash"] == fingerprint(policy), "Frozen news policy or registry differs")
    sources = policy["sources"]
    require(type(sources) is list and sources and len(sources) == len(set(sources))
            and all(source_fetch.source_rule(name, registry)["purpose"] == "news" for name in sources),
            "Frozen news sources must be unique registered sources")
    groups = _source_groups(policy["required_source_groups"], registry)
    require({name for members in groups.values() for name in members} <= set(sources),
            "Required source group is outside frozen news sources")
    return run, registry


def collection_policy(manifest, store, artifacts):
    """A collection cannot define or widen its own authorization."""
    run, registry = run_policy(manifest.get("run_id"), store, artifacts)
    require(manifest.get("news_policy_hash") == run["news_policy_hash"]
            and manifest.get("registry_snapshot_ref") == run["registry_snapshot_ref"]
            and manifest["registry_hash"] == run["source_registry_hash"]
            and manifest["cutoff_at"] == run["publish_cutoff"]
            and manifest["required_source_groups"] == run["news_policy"]["required_source_groups"],
            "Collection differs from its authoritative frozen source policy")
    selected = manifest.get("selected_source_ids")
    mode = manifest.get("source_selection_mode")
    require(type(selected) is list and selected and selected == sorted(set(selected))
            and set(selected) <= set(run["news_policy"]["sources"])
            and mode in ("automatic", "explicit"), "Collection selected sources are outside frozen policy")
    require(mode != "automatic" or set(selected) == set(run["news_policy"]["sources"]),
            "Automatic collection omitted a frozen source")
    require({row["source_id"] for row in manifest["source_results"]} <= set(selected)
            and {row["registry_source_id"] for row in manifest["retrievals"]} <= set(selected),
            "Collection contains an unauthorized source")
    seal = store.get("news-information-seal", manifest["operation_id"])
    require(seal == {key: manifest[key] for key in ("operation_id", "run_id", "request_hash",
            "information_cutoff_at", "news_policy_hash", "registry_hash")},
            "Collection lacks its original controller information cutoff seal")
    require(instant(manifest["cutoff_at"]) <= instant(manifest["information_cutoff_at"]),
            "Information cutoff precedes the publication query cutoff")
    return run, registry


def collect(payload, store, artifacts, operation_id):
    fields(payload, {"cutoff_at", "window_start", "required_source_groups", "run_id"}, {"sources", "timeout_seconds", "max_urls_per_source", "budget_seconds", "refresh_tracked_articles", "max_pages_per_source"}, "news request")
    cutoff, start = instant(payload["cutoff_at"]), instant(payload["window_start"])
    require(start <= cutoff, "News window is reversed")
    require(cutoff <= instant(utc_now()), "News cutoff cannot be in the future")
    require(isinstance(operation_id, str) and operation_id, "Stable news operation_id required")
    run_id = payload["run_id"]
    require(type(run_id) is str and bool(run_id), "News run_id must be nonempty text")
    run, registry = run_policy(run_id, store, artifacts)
    require(payload["cutoff_at"] == run["publish_cutoff"], "News publication cutoff changed")
    require(payload["required_source_groups"] == run["news_policy"]["required_source_groups"], "News source groups changed")
    groups = _source_groups(payload["required_source_groups"], registry)
    context = store.require_context()
    store.assert_owned(context)
    operation_key = fingerprint({"operation_id": operation_id})
    request_hash = fingerprint({"payload": payload, "registry": registry, "news_policy_hash": run["news_policy_hash"],
                                "registry_snapshot_ref": run["registry_snapshot_ref"]})
    existing = store.get("news-operation", operation_key)
    if existing is not None:
        require(existing["request_hash"] == request_hash, "News operation changed inputs")
        manifest = artifacts.read_json(existing["manifest_ref"])
        collection_policy(manifest, store, artifacts)
        for retrieval in manifest["retrievals"]:
            for key in ("raw_ref", "body_ref"):
                if retrieval.get(key):
                    artifacts.read(retrieval[key])
        return {**manifest, "manifest_ref": existing["manifest_ref"], "reused": True}
    selections = payload.get("sources")
    scheduler_key = fingerprint({"registry_hash": fingerprint(registry), "news_policy_hash": run["news_policy_hash"]})
    scheduler = store.get("news-source-scheduler", scheduler_key) or {}
    if selections is None:
        selections = [{"source_id": name, "urls": rule["default_urls"]}
                      for name, rule in sorted(registry["sources"].items(), key=lambda row: row[1].get("priority", 100))
                      if rule["purpose"] == "news" and name in run["news_policy"]["sources"]]
        source_ids = [item["source_id"] for item in selections]
        if scheduler.get("next_source_id") in source_ids:
            offset = source_ids.index(scheduler["next_source_id"])
            selections = selections[offset:]+selections[:offset]
    require(isinstance(selections, list) and 0 < len(selections) <= 20, "A bounded source selection is required")
    names = set()
    for selected in selections:
        fields(selected, {"source_id", "urls"}, label="source selection")
        require(selected["source_id"] not in names, "Duplicate news source")
        names.add(selected["source_id"])
        rule = source_fetch.source_rule(selected["source_id"], registry)
        require(rule["purpose"] == "news" and selected["source_id"] in run["news_policy"]["sources"],
                "Explicit source is outside frozen news policy")
        require(isinstance(selected["urls"], list) and 0 < len(selected["urls"]) <= 20, "Explicit source URL list required")
        for url in selected["urls"]:
            source_fetch.checked_url(url, rule)
    limit = payload.get("max_urls_per_source", 2)
    timeout = payload.get("timeout_seconds", 6)
    budget = payload.get("budget_seconds", 60)
    require(type(limit) is int and 1 <= limit <= 20, "News URL budget must be 1-20")
    require(type(timeout) in (int, float) and 0 < timeout <= 30, "News timeout must be 0-30 seconds")
    require(type(budget) in (int, float) and 0 < budget <= 60, "News global budget must be 0-60 seconds")
    refresh_limit = payload.get("refresh_tracked_articles", min(2, limit//5))
    page_limit = payload.get("max_pages_per_source")
    require(type(refresh_limit) is int and 0 <= refresh_limit <= min(10, limit), "Bounded tracked-article refresh budget required")
    require(page_limit is None or type(page_limit) is int and 1 <= page_limit <= 10, "News page budget must be 1-10")
    deadline = time.monotonic() + budget
    retrievals, versions, source_results = [], [], []
    last_served_source = None
    for selected in selections:
        name, rule = selected["source_id"], registry["sources"][selected["source_id"]]
        pending = list(dict.fromkeys(source_fetch.checked_url(url, rule) for url in selected["urls"]))
        initial_urls = list(pending)
        watermark_key = fingerprint({"source_id": name, "registry_hash": fingerprint(registry),
                                     "news_policy_hash": run["news_policy_hash"]})
        previous_watermark = store.get("news-source-watermark", watermark_key) or {}
        if not payload.get("sources"):
            refresh_urls = previous_watermark.get("tracked_article_urls", [])[:refresh_limit]
            pending = list(dict.fromkeys(previous_watermark.get("pending_urls", []) + refresh_urls + pending))
        next_kind = previous_watermark.get("next_task_kind", "article")
        requested_articles = []
        parent_urls = dict(previous_watermark.get("linked_parent_urls", {})) if not payload.get("sources") else {}
        if limit < 2 and (any(url in parent_urls for url in pending) or
                (payload.get("sources") and rule.get("pdf_attachments")
                 and any(urllib.parse.urlsplit(url).path.lower().endswith(".pdf") for url in pending))):
            raise LinkedPDFBudget(name)
        source_retrievals, visited = [], set()
        linked_parents, parsed_captures = {}, {}
        listing_pages = 0
        deferred_pages = []
        maximum_pages = page_limit or rule.get("max_listing_pages", 3)
        omitted_links = 0
        while pending and len(visited) < limit and time.monotonic() < deadline:
            if payload.get("sources"):
                url = pending.pop(0)
            else:
                ready_pdf = next((item for item in pending if item in linked_parents), None)
                if ready_pdf is not None:
                    pending.remove(ready_pdf)
                    url, next_kind = ready_pdf, "listing"
                else:
                    url, next_kind = _next_task(pending, rule, next_kind)
                if url in parent_urls and url not in linked_parents:
                    parent_url = source_fetch.checked_url(parent_urls[url], rule)
                    if parent_url in visited:
                        deferred_pages.append(url)
                        continue
                    pending.insert(0, url)
                    if parent_url in pending:
                        pending.remove(parent_url)
                    url, next_kind = parent_url, "article"
            if url in visited:
                continue
            if listing_pages >= maximum_pages and re.search(rule.get("listing_pattern", r"$^"), urllib.parse.urlsplit(url).path):
                deferred_pages.append(url)
                continue
            visited.add(url)
            last_served_source = name
            if not payload.get("sources") and _article_url(url, rule):
                # Scope is selected before fetch; failed requests cannot be
                # removed to manufacture a successful article collection.
                requested_articles.append(url)
            retrieval_id = fingerprint({"operation_id": operation_id, "source_id": name, "url": url})
            try:
                capture = source_fetch.fetch(url, name, timeout=min(timeout, max(.001, deadline-time.monotonic())),
                                             max_bytes=2*1024*1024, registry=registry)
                store.assert_owned(context)
                parent_id = linked_parents.get(url)
                parsed = parse_capture(capture, rule, linked_parent=parsed_captures.get(parent_id))
                parsed_captures[retrieval_id] = parsed
                if parsed["state"] == "lead":
                    listing_pages += 1
                raw_ref = artifacts.put_bytes(capture["raw_bytes"])
                body_ref = artifacts.put_bytes(parsed["body"].encode("utf-8")) if parsed.get("body") else None
                retrieval = {key: value for key, value in capture.items() if key not in ("raw_bytes", "text")}
                retrieval.update(retrieval_id=retrieval_id, state=parsed["state"], reason=parsed["reason"],
                                 raw_ref=raw_ref, body_ref=body_ref, fact_verification="not_performed")
                if parent_id is not None:
                    retrieval["linked_parent_retrieval_id"] = parent_id
                if parsed["state"] == "body":
                    doc_id = fingerprint({"source_id": name, "url": _document_url(capture["final_url"])})
                    semantic = {"document_id": doc_id, "source_id": name, "title": parsed["title"],
                                "body_sha256": body_ref["sha256"], "published_at": parsed["published_at"],
                                "modified_at": parsed["modified_at"],
                                "parser_id": parsed["parser_id"]}
                    version_id = fingerprint(semantic)
                    version = {**semantic, "version_id": version_id, "body_ref": body_ref}
                    previous_version = store.get("news-version", version_id)
                    if previous_version is not None:
                        require({k: v for k, v in previous_version.items() if k != "body_ref"}
                                == {k: v for k, v in version.items() if k != "body_ref"}, "News version identity conflict")
                        artifacts.read(previous_version["body_ref"])
                        version = previous_version
                    versions.append(version)
                    captured = instant(retrieval["retrieved_at"])
                    retrieval.update(document_id=doc_id, version_id=version_id,
                                     publication_cutoff_relation=_publication_relation(parsed["published_at"], cutoff),
                                     within_information_cutoff=(_body_available(parsed["published_at"], captured, captured)
                                        and (parsed["modified_at"] is None or _body_available(parsed["modified_at"], captured, captured))))
                if parsed["state"] in ("lead", "body"):
                    next_pages = parsed.get("listing_links", []) if parsed["state"] == "lead" and listing_pages < maximum_pages else []
                    links = parsed.get("links", []) + next_pages
                    queued_articles = 0
                    for link in links:
                        if rule.get("max_follow_links", 5) == 0:
                            break
                        target = urllib.parse.urljoin(capture["final_url"], link).split("#", 1)[0]
                        try:
                            target = source_fetch.checked_url(target, rule)
                        except ValueError:
                            continue
                        target_path = urllib.parse.urlsplit(target).path
                        is_pdf = bool(re.search(r"\.pdf$", target_path, re.I))
                        is_article = bool(re.search(rule.get("article_pattern", r"$^"), target_path))
                        is_listing = target in next_pages or bool(re.search(rule.get("listing_pattern", r"$^"), target_path))
                        if parsed["state"] == "body" and not (is_pdf and rule.get("pdf_attachments")):
                            continue
                        if is_listing and listing_pages >= maximum_pages:
                            omitted_links += 1
                            continue
                        if is_pdf and parsed["state"] == "body":
                            linked_parents[target] = retrieval_id
                            parent_urls[target] = capture["final_url"]
                        if is_article and queued_articles >= rule.get("max_follow_links", 20):
                            omitted_links += 1
                            continue
                        if (is_article or is_listing) and target not in visited and target not in pending:
                            if len(pending) < 5000:
                                pending.append(target)
                                queued_articles += int(is_article)
                            else:
                                omitted_links += 1
            except (OSError, ValueError) as error:
                retrieval = {"retrieval_id": retrieval_id, "registry_source_id": name,
                             "requested_url": url, "retrieved_at": utc_now(), "state": "unavailable",
                             "reason": str(error), "raw_ref": None, "body_ref": None,
                             "fact_verification": "not_performed"}
            source_retrievals.append(retrieval)
            retrievals.append(retrieval)
        pending = list(dict.fromkeys(pending + deferred_pages))
        if not payload.get("sources") and any(url in parent_urls for url in pending):
            next_kind = "article"
        automatic_scope = not payload.get("sources") and bool(requested_articles)
        scope = {"source_id": name, "scope": "explicit_article_urls" if payload.get("sources") or automatic_scope else "enumerated_urls",
                 "urls": requested_articles if automatic_scope else initial_urls, "registry_hash": fingerprint(registry),
                 "news_policy_hash": run["news_policy_hash"]}
        if not payload.get("sources"):
            pending = list(dict.fromkeys(pending + [row["requested_url"] for row in source_retrievals
                                                    if row["state"] in ("unavailable", "partial")]))
        # A listing crawl cannot prove a time window is exhaustive. Only the
        # exact finite, explicitly requested article set gets its own cursor.
        scoped_retrievals = [r for r in source_retrievals if r["requested_url"] in scope["urls"]]
        scoped_pending = [url for url in scope["urls"] if url not in visited] if automatic_scope else pending
        complete = bool(payload.get("sources") or automatic_scope) and bool(rule.get("complete_explicit_url_scope")) and not scoped_pending
        complete &= bool(scoped_retrievals) and all(r["state"] == "body" and r.get("within_information_cutoff", False) for r in scoped_retrievals)
        source_results.append({**scope, "scope_key": fingerprint(scope), "requested_window": {
            "start": payload["window_start"], "cutoff": payload["cutoff_at"]},
            "state": "checked_explicit_scope" if complete else "partial", "visited_urls": sorted(visited),
            "pending_urls": scoped_pending, "advance_cursor": complete,
            "automatic_body_scope": automatic_scope, "scheduled_article_urls": requested_articles,
            "discovery_pending_urls": pending if not payload.get("sources") else [], "next_task_kind": next_kind,
            "linked_parent_urls": {url: parent for url, parent in parent_urls.items() if url in pending},
            "watermark_key": watermark_key, "continued_from": previous_watermark.get("collection_id") if not payload.get("sources") else None,
            "omitted_discovery_links": omitted_links,
            "listing_pages_visited": listing_pages, "maximum_listing_pages": maximum_pages,
            "tracked_article_urls": list(dict.fromkeys([row["requested_url"] for row in source_retrievals
                if row["state"] == "body" and row.get("content_type") == "text/html"]
                + previous_watermark.get("tracked_article_urls", [])))[:20],
            "budget_exhausted": bool(pending) and time.monotonic() >= deadline,
            "coverage_claim": "exact_requested_articles_only" if complete else "visited_pages_only_not_source_time_coverage"})
    information_cutoff_at = utc_now()
    require(cutoff <= instant(information_cutoff_at)
            and all(instant(row["retrieved_at"]) <= instant(information_cutoff_at) for row in retrievals),
            "Real news capture follows the controller information cutoff")
    manifest = {"schema_version": 4, "operation_id": operation_id, "collection_id": operation_id, "run_id": run_id, "request_hash": request_hash,
                "status": "collected" if all(r["advance_cursor"] for r in source_results) else "partial_collection",
                "registry_hash": fingerprint(registry), "registry_snapshot_ref": run["registry_snapshot_ref"],
                "news_policy_hash": run["news_policy_hash"], "selected_source_ids": sorted(names),
                "source_selection_mode": "explicit" if payload.get("sources") else "automatic", "cutoff_at": payload["cutoff_at"],
                "information_cutoff_at": information_cutoff_at,
                "window_start": payload["window_start"], "required_source_groups": groups,
                "source_group_coverage": _group_coverage(groups, source_results),
                "source_results": source_results, "retrievals": retrievals,
                "versions": list({v["version_id"]: v for v in versions}.values()), "analysis": [],
                "fact_verification": "not_performed", "coverage_complete_for_source_time_window": False,
                "budget_seconds": budget}
    reference = artifacts.put_json(manifest)
    # Bodies/evidence are durable and re-read before the transaction advances
    # any state; an interrupted transaction cannot publish a cursor alone.
    artifacts.read(reference)
    with store.transaction() as conn:
        store.put("news-information-seal", operation_id, {key: manifest[key] for key in (
            "operation_id", "run_id", "request_hash", "information_cutoff_at", "news_policy_hash", "registry_hash")},
            immutable=True, conn=conn)
        for version in manifest["versions"]:
            store.put("news-version", version["version_id"], version, immutable=True, conn=conn)
            prior = store.get("news-document", version["document_id"], conn=conn)
            captured_at = max(r["retrieved_at"] for r in retrievals if r.get("version_id") == version["version_id"])
            if prior is None or instant(captured_at) > instant(prior["captured_at"]):
                store.put("news-document", version["document_id"], {"version_id": version["version_id"],
                          "previous_version_id": prior["version_id"] if prior else None,
                          "captured_at": captured_at,
                          "first_seen_at": prior["first_seen_at"] if prior else captured_at}, immutable=False, conn=conn)
        for retrieval in retrievals:
            store.put("news-retrieval", retrieval["retrieval_id"], retrieval, immutable=True, conn=conn)
            if retrieval.get("version_id"):
                first = store.get("news-version-first-observed", retrieval["version_id"], conn=conn)
                if first is None:
                    store.put("news-version-first-observed", retrieval["version_id"], {
                        "version_id": retrieval["version_id"], "observed_at": retrieval["retrieved_at"],
                        "collection_id": operation_id, "retrieval_id": retrieval["retrieval_id"],
                        "raw_ref": retrieval["raw_ref"]}, immutable=True, conn=conn)
        for result in source_results:
            previous = store.get("news-source-state", result["scope_key"], conn=conn) or {}
            state = {**previous, "last_attempt_at": utc_now(), "manifest_ref": reference,
                     "scope": result["scope"], "scope_key": result["scope_key"], "status": result["state"]}
            if result["advance_cursor"] and (previous.get("checked_through") is None
                    or cutoff >= instant(previous["checked_through"])):
                state.update(checked_through=payload["cutoff_at"], last_success_at=utc_now())
            store.put("news-source-state", result["scope_key"], state, immutable=False, conn=conn)
            if not payload.get("sources"):
                store.put("news-source-watermark", result["watermark_key"], {
                    "source_id": result["source_id"], "collection_id": operation_id, "run_id": run_id,
                    "manifest_ref": reference, "pending_urls": result["discovery_pending_urls"],
                    "next_task_kind": result["next_task_kind"],
                    "linked_parent_urls": result["linked_parent_urls"],
                    "tracked_article_urls": result["tracked_article_urls"],
                    "last_attempt_at": utc_now(), "coverage_complete_for_source_time_window": False,
                    "omitted_discovery_links": result["omitted_discovery_links"]}, immutable=False, conn=conn)
        if not payload.get("sources") and last_served_source is not None:
            source_ids = [item["source_id"] for item in selections]
            next_source = source_ids[(source_ids.index(last_served_source)+1) % len(source_ids)]
            store.put("news-source-scheduler", scheduler_key, {"next_source_id": next_source,
                "collection_id": operation_id, "manifest_ref": reference}, immutable=False, conn=conn)
        store.put("news-operation", operation_key, {"request_hash": request_hash, "manifest_ref": reference},
                  immutable=True, conn=conn)
    return {**manifest, "manifest_ref": reference, "reused": False}


def _source_groups(groups, registry):
    require(type(groups) is dict and groups, "Explicit minimum source groups required")
    for name, members in groups.items():
        require(type(name) is str and name and type(members) is list and members
                and len(set(members)) == len(members), "Source groups need distinct registered members")
        for source_id in members:
            require(source_fetch.source_rule(source_id, registry)["purpose"] == "news", "Source group contains a non-news source")
    return groups


def _group_coverage(groups, results):
    checked = {row["source_id"] for row in results if row["state"] == "checked_explicit_scope"}
    return {name: {"required_sources": members, "checked_sources": sorted(set(members)&checked),
                   "missing_sources": sorted(set(members)-checked),
                   "scope": "declared_minimum_explicit_article_scope_not_global_news_exhaustiveness"}
            for name, members in groups.items()}


def _verify_source_scopes(manifest, registry):
    """A resealed state label cannot replace checked article requests."""
    seen = set()
    for result in manifest["source_results"]:
        source = result["source_id"]
        require(source not in seen, "Repeated source coverage scope")
        seen.add(source)
        rule = source_fetch.source_rule(source, registry)
        scope = {key: result[key] for key in ("source_id", "scope", "urls", "registry_hash", "news_policy_hash")}
        require(result["registry_hash"] == fingerprint(registry) and result["scope_key"] == fingerprint(scope)
                and result["news_policy_hash"] == manifest["news_policy_hash"]
                and source in manifest["selected_source_ids"],
                "Source coverage scope identity differs")
        require(result["requested_window"] == {"start": manifest["window_start"], "cutoff": manifest["cutoff_at"]},
                "Source coverage time window differs")
        captured = [row for row in manifest["retrievals"] if row["registry_source_id"] == source]
        require(set(result["visited_urls"]) == {row["requested_url"] for row in captured},
                "Source coverage lacks its actual retrieval attempts")
        if result.get("automatic_body_scope"):
            scheduled = result["scheduled_article_urls"]
            require(scheduled == result["urls"] and scheduled == [row["requested_url"] for row in captured
                    if _article_url(row["requested_url"], rule)], "Automatic body scope omitted an attempted article")
            require(all(_article_url(url, rule) for url in scheduled), "Automatic body scope contains a listing")
            captured = [row for row in captured if row["requested_url"] in scheduled]
        complete = (result["scope"] == "explicit_article_urls" and bool(rule.get("complete_explicit_url_scope"))
                    and bool(captured) and not result["pending_urls"]
                    and set(result["urls"]) <= set(result["visited_urls"])
                    and all(row["state"] == "body" and row.get("within_information_cutoff") for row in captured))
        require(result["advance_cursor"] is complete
                and result["state"] == ("checked_explicit_scope" if complete else "partial"),
                "Source coverage success is not reproduced by original article attempts")
    require({row["registry_source_id"] for row in manifest["retrievals"]} <= seen, "Retrieval has no source coverage scope")


def _event_boundary(value, *, end=False):
    if value["precision"] == "timestamp":
        return instant(value["value"])
    day = dt.date.fromisoformat(value["value"])
    if end:
        day += dt.timedelta(days=1)
    return dt.datetime.combine(day, dt.time(), ZoneInfo(value["timezone"]))


def _assemble_events(payload, manifest, claims, artifacts, known_at, registry=None):
    require(type(payload["events"]) is list and len(payload["events"]) <= 100, "Bounded source events required")
    versions = {row["version_id"]: row for row in manifest["versions"]}
    by_claim = {row["id"]: row for row in claims}
    result, seen = [], set()
    for item in payload["events"]:
        fields(item, {"event_key", "version_ids", "claim_ids", "event_at", "effective_from", "effective_until",
                      "review_by", "supersedes", "retracts", "temporal_evidence"}, label="source event")
        require(type(item["version_ids"]) is list and item["version_ids"]
                and len(set(item["version_ids"])) == len(item["version_ids"]) and set(item["version_ids"]) <= set(versions),
                "Event needs captured document versions")
        require(type(item["claim_ids"]) is list and item["claim_ids"]
                and len(set(item["claim_ids"])) == len(item["claim_ids"]) and set(item["claim_ids"]) <= set(by_claim),
                "Event needs assessed claims")
        require(all(by_claim[key]["version_id"] in item["version_ids"] for key in item["claim_ids"]), "Event claim/version differs")
        documents = sorted({versions[key]["document_id"] for key in item["version_ids"]})
        key = item["event_key"]
        require(key is None or (type(key) is str and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,}〔[0-9]{4}〕[0-9]+号", key)),
                "Event alias must be a complete officially numbered document, or null")
        bodies = {version: artifacts.read(versions[version]["body_ref"]).decode("utf-8") for version in item["version_ids"]}
        if key is not None:
            require(all(key in body for body in bodies.values()), "Official event number must occur in every aliased document")
        else:
            require(len(documents) == 1, "Unnumbered cross-document aliases require source review")
        event_id = fingerprint({"official_number": key}) if key else documents[0]
        require(event_id not in seen, "Duplicate event in one assessment")
        seen.add(event_id)
        instant(item["review_by"])
        require(instant(item["review_by"]) > instant(known_at), "Event analytical review deadline must be prospective")
        evidence = item["temporal_evidence"]
        require(type(evidence) is list, "Temporal source evidence must be a list")
        for row in evidence:
            fields(row, {"field", "version_id", "quote"}, label="event temporal evidence")
            require(row["field"] in ("event_at", "effective_from", "effective_until", "supersedes", "retracts")
                    and row["version_id"] in bodies and type(row["quote"]) is str and row["quote"]
                    and row["quote"] in bodies[row["version_id"]], "Temporal quote lacks original source support")
        for field in ("event_at", "effective_from", "effective_until"):
            value = item[field]
            if value is None:
                continue
            fields(value, {"value", "precision", "timezone"}, label="source event date")
            require(value["precision"] in ("date", "timestamp"), "Explicit source date precision required")
            parsed = _event_boundary(value)
            matching = [row for row in evidence if row["field"] == field]
            require(matching and all(value["timezone"] == source_fetch.source_rule(versions[row["version_id"]]["source_id"], registry)["timezone"]
                                     for row in matching), "Event date timezone must match its publisher")
            if value["precision"] == "date":
                day = parsed.date()
                representations = (day.isoformat(), f"{day.year}年{day.month}月{day.day}日")
            else:
                representations = (value["value"],)
            require(any(any(text in row["quote"] for text in representations) for row in matching),
                    "Event time precision must be supported literally by the original quote")
        require(item["effective_until"] is None or item["effective_from"] is None
                or _event_boundary(item["effective_from"]) < _event_boundary(item["effective_until"], end=True),
                "Administrative interval is reversed")
        for field in ("supersedes", "retracts"):
            require(type(item[field]) is list and len(set(item[field])) == len(item[field])
                    and all(type(value) is str and re.fullmatch("[0-9a-f]{64}", value) for value in item[field]),
                    "Event revision links must be distinct event identities")
            require(not item[field] or any(row["field"] == field for row in evidence), "Revision relation needs original quoted evidence")
        semantic = {**item, "event_id": event_id, "identity_scope": "official_number" if key else "document_identity_only",
                    "document_ids": documents, "publication_times": [versions[key]["published_at"] for key in item["version_ids"]],
                    "modified_times": [versions[key]["modified_at"] for key in item["version_ids"]],
                    "source_urls": sorted({row["final_url"] for row in manifest["retrievals"]
                        if row.get("version_id") in item["version_ids"]})}
        publications = [value for value in semantic["publication_times"] if value and value["precision"] in ("date", "timestamp")]
        source_semantic = {key: semantic[key] for key in ("event_id", "event_key", "event_at", "effective_from", "effective_until")}
        source_semantic.update(version_ids=sorted(item["version_ids"]), supersedes=sorted(item["supersedes"]),
                               retracts=sorted(item["retracts"]), source_urls=semantic["source_urls"])
        source_revision = fingerprint(source_semantic)
        window = instant(manifest["window_start"])
        published_after_start = bool(publications) and all(
            (_event_boundary(value) if value["precision"] == "date" else instant(value["value"])) >= window for value in publications)
        published_in_window = published_after_start and all(
            _publication_relation(value, instant(manifest["cutoff_at"])) == "proved_before" for value in publications)
        result.append({**semantic, "revision_id": fingerprint(semantic), "known_at": known_at,
                       "source_revision_id": source_revision, "source_known_at": known_at,
                       "first_seen_at": known_at,
                       "publication_role": "within_declared_window" if published_in_window else
                           "available_at_information_cutoff" if published_after_start else "background"})
    return result


def _validate_revision_links(events, store):
    known = {}
    for _, row in store.scan("news-event-revision"):
        known.setdefault(row["event_id"], []).append(row)
    for row in events:
        for field in ("supersedes", "retracts"):
            for target in row[field]:
                require(target in known and target != row["event_id"], "Revision target needs a previously known distinct event")
                numbered = {value["event_key"] for value in known[target] if value["event_key"]}
                numbered |= {url for value in known[target] for url in value["source_urls"]}
                require(numbered and any(evidence["field"] == field and any(key in evidence["quote"] for key in numbered)
                                         for evidence in row["temporal_evidence"]),
                        "Source revision relation must explicitly identify the target document number or canonical URL")


def event_state(review, collection, decision_at, store):
    """Read-only bitemporal view; later retractions do not rewrite earlier audits."""
    boundary = instant(decision_at)
    revisions = [value for _, value in store.scan("news-event-revision") if instant(value["known_at"]) <= boundary]
    latest = {}
    for row in sorted(revisions, key=lambda value: (instant(value["known_at"]), value["revision_id"])):
        latest[row["event_id"]] = row
    retired = {event for row in latest.values() for event in row["supersedes"]+row["retracts"]}
    active, blocked = [], {}
    review_events = {row["event_id"]: row for row in review["events"]}
    coverage = collection["source_group_coverage"]
    for thesis in review["industry_theses"]:
        reasons = []
        if boundary >= instant(thesis["valid_until"]):
            reasons.append("analytical_thesis_review_due")
        for group in thesis["required_source_groups"]:
            if coverage[group]["missing_sources"]:
                reasons.append("minimum_source_group_incomplete:"+group)
        for event_id in thesis["event_ids"]:
            row = review_events[event_id]
            if event_id in retired:
                reasons.append("event_superseded_or_retracted")
            if latest.get(event_id, {}).get("revision_id") != row["revision_id"]:
                reasons.append("new_event_revision_requires_reassessment")
            if boundary >= instant(row["review_by"]):
                reasons.append("event_analytical_review_due")
            if thesis["event_basis"] == "standing_policy":
                if row["effective_from"] and boundary < _event_boundary(row["effective_from"]):
                    reasons.append("administrative_period_not_started")
                if row["effective_until"] and boundary >= _event_boundary(row["effective_until"], end=True):
                    reasons.append("administrative_period_ended")
        if reasons:
            blocked[thesis["thesis_id"]] = sorted(set(reasons))
        else:
            active.append(thesis["thesis_id"])
    relevant = {event_id for row in review["industry_theses"] for event_id in row["event_ids"]}
    frontier = {key: value["revision_id"] for key, value in sorted(latest.items())
                if key in relevant or relevant & set(value["supersedes"]+value["retracts"])}
    return {"active_thesis_ids": sorted(active), "blocked_theses": blocked,
            "source_group_coverage": coverage, "event_frontier_hash": fingerprint(frontier),
            "event_state_scope": "known_revisions_and_declared_review_deadlines"}


def _assemble_claims(payload, manifest, artifacts, allowed_sources=None, registry=None):
    versions = {v["version_id"]: v for v in manifest["versions"]}
    eligible = {r["version_id"] for r in manifest["retrievals"]
                if r["state"] == "body" and r.get("within_information_cutoff")}
    require(isinstance(payload["claims"], list) and len(payload["claims"]) <= 100, "Bounded claim list required")
    evidence_refs = {}
    def support(version_id, quote):
        require(version_id in versions and version_id in eligible, "Claim needs a body version available in this collection before cutoff")
        source_id = versions[version_id]["source_id"]
        require(source_fetch.source_rule(source_id, registry).get("purpose") == "news", "Claim source is not a registered news source")
        if allowed_sources is not None:
            require(source_id in allowed_sources, "Claim or counterevidence source is outside the frozen strategy")
        require(isinstance(quote, str) and quote.strip(), "A nonempty verbatim source quote is required")
        body = artifacts.read(versions[version_id]["body_ref"]).decode("utf-8")
        require(quote in body, "Claim quote does not occur in the stored source body")
        capture = next(r for r in manifest["retrievals"] if r.get("version_id") == version_id and r["state"] == "body")
        source_fetch.validate_capture_provenance(capture, registry)
        for reference in (versions[version_id]["body_ref"], capture["raw_ref"]):
            artifacts.read(reference)
            evidence_refs[reference["sha256"]] = reference
        return {"version_id": version_id, "body_sha256": versions[version_id]["body_sha256"],
                "source_url": capture["final_url"], "retrieval_id": capture["retrieval_id"],
                "quote_start": body.index(quote), "quote_length": len(quote)}
    ids, claims = set(), []
    for item in payload["claims"]:
        fields(item, {"id", "kind", "text", "version_id", "quote", "categories", "direction",
                      "counterevidence", "reviewer", "review_method"}, {"event_at"}, label="news claim")
        require(isinstance(item["id"], str) and item["id"] and item["id"] not in ids, "Claim id must be unique")
        ids.add(item["id"])
        require(item["kind"] in ("fact", "inference"), "Unknown news claim kind")
        require(item["direction"] in ("increase", "decrease", "watch", "neutral"), "Unknown category direction")
        require(all(isinstance(item[key], str) and item[key].strip() for key in ("text", "reviewer", "review_method")),
                "Claim text, reviewer and review method are required")
        require(isinstance(item["categories"], list) and item["categories"]
                and all(isinstance(c, str) and c.strip() for c in item["categories"]), "Claim categories required")
        primary = support(item["version_id"], item["quote"])
        event_at = item.get("event_at")
        if event_at is not None:
            require(isinstance(event_at, str), "Event time must be explicit ISO text or null")
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", event_at):
                dt.date.fromisoformat(event_at)
            else:
                instant(event_at)
        require(isinstance(item["counterevidence"], list) and item["counterevidence"],
                "Counterevidence or an explicit missing reason is required")
        counter = []
        for evidence in item["counterevidence"]:
            if isinstance(evidence, dict) and set(evidence) == {"missing_reason"}:
                require(isinstance(evidence["missing_reason"], str) and evidence["missing_reason"].strip(), "Missing counterevidence reason required")
                counter.append(dict(evidence))
            else:
                fields(evidence, {"version_id", "quote", "text"}, label="counterevidence")
                require(isinstance(evidence["text"], str) and evidence["text"].strip(), "Counterevidence description required")
                counter.append({**evidence, "support": support(evidence["version_id"], evidence["quote"])})
        claims.append({**item, "support": primary, "url": primary["source_url"],
                       "published_at": versions[item["version_id"]]["published_at"],
                       "event_at": event_at, "event_time_basis": "analyst_attestation" if event_at else "unknown",
                       "counterevidence": counter,
                       "qualification": "source_reported" if item["kind"] == "fact" else "analyst_inference"})
    return claims, list(evidence_refs.values())


def _assemble_theses(payload, claims, events, groups):
    """Bind each research direction to assessed claims, retaining its inference scope."""
    items = payload["industry_theses"]
    require(type(items) is list and len(items) <= 50, "A bounded industry thesis list is required")
    by_id, seen, result = {claim["id"]: claim for claim in claims}, set(), []
    for item in items:
        fields(item, {"thesis_id", "kind", "label", "direction", "horizon_days", "search_terms", "claim_ids", "event_ids", "required_source_groups", "valid_until", "event_basis"},
               {"sector_id"}, label="industry thesis")
        if "sector_id" in item:
            require(type(item["sector_id"]) is str and item["sector_id"].strip() == item["sector_id"]
                    and 0 < len(item["sector_id"]) <= 100, "Stable explicit thesis sector identity required")
        require(all(type(item[key]) is str and item[key].strip() for key in ("thesis_id", "kind", "label", "direction")),
                "Thesis identifiers, kind, label and direction must be nonempty text")
        require(item["thesis_id"] not in seen, "Industry thesis identifiers must be unique")
        seen.add(item["thesis_id"])
        require(item["kind"] in {"asset_class", "industry", "theme", "broad_market"}, "Unknown thesis kind")
        require(item["direction"] in {"increase", "decrease", "hold", "watch"}, "Unknown thesis direction")
        require(type(item["horizon_days"]) is int and item["horizon_days"] > 0, "Positive thesis horizon required")
        for key in ("search_terms", "claim_ids"):
            require(type(item[key]) is list and 0 < len(item[key]) <= 30
                    and all(type(value) is str and value.strip() for value in item[key])
                    and len(set(item[key])) == len(item[key]), "Distinct nonempty thesis " + key + " required")
        require(set(item["claim_ids"]) <= set(by_id), "Thesis needs assessed supporting claims")
        require(item["event_basis"] in ("standing_policy", "announcement_information"), "Explicit thesis event basis required")
        require(type(item["event_ids"]) is list and item["event_ids"] and set(item["event_ids"]) <= {e["event_id"] for e in events},
                "Thesis requires declared source events")
        require(type(item["required_source_groups"]) is list and item["required_source_groups"]
                and set(item["required_source_groups"]) <= set(groups), "Thesis source groups must belong to collection policy")
        instant(item["valid_until"])
        require(set(item["claim_ids"]) <= {claim for event in events if event["event_id"] in item["event_ids"] for claim in event["claim_ids"]},
                "Thesis claims must belong to its declared events")
        supports = [by_id[claim_id]["support"] for claim_id in item["claim_ids"]]
        result.append({**item, "evidence_refs": supports, "qualification": "analyst_inference_from_assessed_claims"})
    return result


def _fact_feature_key(event, claims):
    return fingerprint({"event_source_revision": event["source_revision_id"], "source_facts": sorted(
        [[row["version_id"], row["quote"]] for row in claims
         if row["id"] in event["claim_ids"] and row["kind"] == "fact"])})


def assess(payload, store, artifacts, operation_id):
    """Validate quoted support for an attributed review; do not certify truth."""
    fields(payload, {"collection_id", "claims", "industry_theses", "events"}, {"run_id", "economic_observations"}, label="news assessment")
    store.assert_owned()
    require(isinstance(operation_id, str) and operation_id, "Stable review operation_id required")
    request_hash = fingerprint(payload)
    existing = store.get("news-review", operation_id)
    if existing is not None:
        require(existing["request_hash"] == request_hash, "News review operation changed inputs")
        artifacts.read(existing["manifest_ref"])
        for reference in existing["evidence_refs"]:
            artifacts.read(reference)
        return existing
    require(isinstance(payload["collection_id"], str), "collection_id must be text")
    collection = store.get("news-operation", fingerprint({"operation_id": payload["collection_id"]}))
    require(collection is not None, "News assessment needs a committed collection")
    manifest = artifacts.read_json(collection["manifest_ref"])
    run, registry = collection_policy(manifest, store, artifacts)
    run_id = manifest["run_id"]
    if "run_id" in payload:
        require(type(payload["run_id"]) is str and payload["run_id"] == run_id, "Assessment belongs to another analysis run")
    claims, evidence_refs = _assemble_claims(payload, manifest, artifacts, set(run["news_policy"]["sources"]), registry)
    reviewed_at = utc_now()
    events = _assemble_events(payload, manifest, claims, artifacts, reviewed_at, registry)
    for event in events:
        first_source = store.get("news-source-revision-first-known", event["source_revision_id"])
        if first_source is not None:
            event["source_known_at"] = first_source["first_known_at"]
        existing_event = store.get("news-event-revision", event["revision_id"])
        if existing_event is not None:
            event["known_at"] = existing_event["known_at"]
            event["first_seen_at"] = existing_event["first_seen_at"]
            event["publication_role"] = existing_event["publication_role"]
        else:
            earlier = [row["first_seen_at"] for _, row in store.scan("news-event-revision") if row["event_id"] == event["event_id"]]
            event["first_seen_at"] = min(earlier, key=instant) if earlier else reviewed_at
    _validate_revision_links(events, store)
    theses = _assemble_theses(payload, claims, events, manifest["required_source_groups"])
    import news_economics
    economic = news_economics.assemble(payload.get("economic_observations", []), manifest, claims, events,
                                       artifacts, reviewed_at, store=store, registry=registry)
    declared_sectors = {}
    for row in theses:
        if "sector_id" in row:
            declared_sectors.setdefault(row["sector_id"], set()).update(row["event_ids"])
    require(all(all(sector in declared_sectors and row["event_id"] in declared_sectors[sector]
                    for sector in row["sector_ids"]) for row in economic),
            "Economic industry assignment requires the same stable sector and source event in assessed theses")
    review = {"schema_version": 4, "review_id": operation_id, "run_id": run_id, "request_hash": request_hash,
              "request_ref": artifacts.put_json(payload),
              "status": "assessed" if claims else "awaiting_analysis",
              "collection_id": payload["collection_id"], "collection_manifest_ref": collection["manifest_ref"],
              "cutoff_at": manifest["cutoff_at"], "information_cutoff_at": manifest["information_cutoff_at"],
              "claims": claims, "source_results": manifest["source_results"],
              "evidence_refs": evidence_refs, "industry_theses": theses, "events": events, "economic_observations": economic,
              "required_source_groups": manifest["required_source_groups"],
              "qualification": "source_quotes_checked_not_truth_proven", "reviewed_at": reviewed_at}
    reference = artifacts.put_json(review)
    artifacts.read(reference)
    result = {**review, "manifest_ref": reference}
    with store.transaction() as conn:
        for observation in economic:
            if store.get("news-economic-first-known", observation["observation_key"], conn=conn) is None:
                store.put("news-economic-first-known", observation["observation_key"], {
                    "observation_key": observation["observation_key"], "first_known_at": observation["known_at"],
                    "review_id": operation_id, "observation_id": observation["observation_id"]}, immutable=True, conn=conn)
            store.put("news-economic-observation", observation["observation_id"], observation, immutable=True, conn=conn)
        for event in events:
            if store.get("news-source-revision-first-known", event["source_revision_id"], conn=conn) is None:
                store.put("news-source-revision-first-known", event["source_revision_id"], {
                    "source_revision_id": event["source_revision_id"], "first_known_at": reviewed_at,
                    "review_id": operation_id}, immutable=True, conn=conn)
            store.put("news-event-revision", event["revision_id"], event, immutable=True, conn=conn)
            if any(row["kind"] == "fact" and row["id"] in event["claim_ids"] for row in claims):
                key = _fact_feature_key(event, claims)
                if store.get("news-fact-feature-first-known", key, conn=conn) is None:
                    store.put("news-fact-feature-first-known", key, {"first_known_at": reviewed_at,
                              "review_id": operation_id, "feature_key": key}, immutable=True, conn=conn)
        store.put("news-review", operation_id, result, immutable=True, conn=conn)
    return result


def _closed_review_references(value, artifacts, seen):
    if isinstance(value, dict):
        if {"sha256", "size"} <= value.keys() and ("path" in value or "chunks" in value):
            key = fingerprint(value)
            if key in seen:
                return
            require(len(seen) < 4096, "News evidence graph exceeds supported size")
            seen.add(key)
            artifacts.read(value)
            if value.get("media_type") == "application/json":
                _closed_review_references(artifacts.read_json(value), artifacts, seen)
        else:
            for child in value.values():
                _closed_review_references(child, artifacts, seen)
    elif isinstance(value, list):
        for child in value:
            _closed_review_references(child, artifacts, seen)


def _verify_collection_bodies(manifest, registry, reviewed_at, decision_at, max_age, artifacts, *, store):
    require(manifest.get("schema_version") == 4 and manifest.get("registry_hash") == fingerprint(registry),
            "Collection schema or registered source identity differs")
    require(isinstance(manifest.get("retrievals"), list) and 0 < len(manifest["retrievals"]) <= 400,
            "A bounded captured news collection is required")
    require(isinstance(manifest.get("versions"), list), "Collection versions are missing")
    cutoff = instant(manifest["cutoff_at"])
    information_cutoff = instant(manifest["information_cutoff_at"])
    require(cutoff <= information_cutoff <= reviewed_at
            and 0 <= (decision_at-information_cutoff).total_seconds() <= max_age,
            "News collection is future or stale")
    versions = {}
    for version in manifest["versions"]:
        fields(version, {"document_id", "source_id", "title", "body_sha256", "published_at", "modified_at",
                         "parser_id", "version_id", "body_ref"}, label="stored news version")
        require(version["version_id"] not in versions, "Repeated stored news version")
        versions[version["version_id"]] = version
    seen_versions, retrieval_ids, parsed_captures = set(), set(), {}
    for retrieval in manifest["retrievals"]:
        identity, source_id = retrieval["retrieval_id"], retrieval["registry_source_id"]
        require(identity not in retrieval_ids, "Repeated news retrieval")
        retrieval_ids.add(identity)
        rule = source_fetch.source_rule(source_id, registry)
        require(rule.get("purpose") == "news", "Collection uses a non-news source")
        captured = instant(retrieval["retrieved_at"])
        original = store.get("news-retrieval", identity)
        require(original is not None and original["retrieved_at"] == retrieval["retrieved_at"],
                "News retrieval clock differs from the original producer journal")
        require(captured <= information_cutoff <= reviewed_at and 0 <= (decision_at-captured).total_seconds() <= max_age,
                "News retrieval is future or stale")
        if retrieval.get("raw_ref") is None:
            require(retrieval["state"] == "unavailable" and retrieval.get("body_ref") is None
                    and not retrieval.get("version_id") and bool(retrieval.get("reason")),
                    "Missing source bytes cannot qualify as news")
            continue
        source_fetch.validate_capture_provenance(retrieval, registry)
        require(type(retrieval["raw_ref"]["size"]) is int and 0 < retrieval["raw_ref"]["size"] <= 2*1024*1024,
                "News source exceeds capture limit")
        raw = artifacts.read(retrieval["raw_ref"])
        require(hashlib.sha256(raw).hexdigest() == retrieval["raw_sha256"] and len(raw) == retrieval["bytes"],
                "News raw source identity differs")
        text, encoding = source_fetch.decode(raw, retrieval["content_type"], retrieval["encoding"], rule)
        require(encoding == retrieval["encoding"] and
                (hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None) == retrieval["text_sha256"],
                "News decoded source identity differs")
        parent_id = retrieval.get("linked_parent_retrieval_id")
        parent = parsed_captures.get(parent_id)
        if parent_id is not None:
            require(parent is not None and parent["state"] == "body", "PDF parent was not a source-verified preceding article")
            parent_capture = next(row for row in manifest["retrievals"] if row["retrieval_id"] == parent_id)
            require(parent_capture["registry_source_id"] == source_id and any(
                urllib.parse.urljoin(parent_capture["final_url"], link).split("#", 1)[0] == retrieval["requested_url"]
                for link in parent["links"]), "PDF URL is not an original link in its official parent article")
        parsed = parse_capture({**retrieval, "text": text, "raw_bytes": raw}, rule, linked_parent=parent)
        parsed_captures[identity] = parsed
        require(parsed["state"] == retrieval["state"], "Stored news body classification differs from source")
        body = parsed.get("body")
        if body:
            require(retrieval.get("body_ref") is not None and artifacts.read(retrieval["body_ref"]) == body.encode("utf-8"),
                    "Stored article body differs from parsed source")
        else:
            require(retrieval.get("body_ref") is None, "Source has no supported article body")
        if parsed["state"] != "body":
            require(not retrieval.get("version_id"), "A source lead cannot become a body version")
            continue
        doc_id = fingerprint({"source_id": source_id, "url": _document_url(retrieval["final_url"])})
        semantic = {"document_id": doc_id, "source_id": source_id, "title": parsed["title"],
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "published_at": parsed["published_at"], "modified_at": parsed["modified_at"],
                    "parser_id": parsed["parser_id"]}
        version_id = fingerprint(semantic)
        require(retrieval.get("document_id") == doc_id and retrieval.get("version_id") == version_id
                and version_id in versions, "News document/version differs from actual body")
        version = versions[version_id]
        require(fingerprint({key: version[key] for key in semantic}) == fingerprint(semantic)
                and artifacts.read(version["body_ref"]) == body.encode("utf-8"), "News version contents differ")
        eligible = (_body_available(parsed["published_at"], captured, information_cutoff)
                    and (parsed["modified_at"] is None or _body_available(parsed["modified_at"], captured, information_cutoff)))
        require(type(retrieval.get("within_information_cutoff")) is bool
                and retrieval["within_information_cutoff"] == eligible
                and retrieval.get("publication_cutoff_relation") == _publication_relation(parsed["published_at"], cutoff),
                "News information eligibility or publisher precision relation differs")
        seen_versions.add(version_id)
    require(seen_versions == set(versions), "Collection has unbound article versions")


def validate_review(review, spec, decision_at, artifacts, *, store):
    """Re-derive evidence and enforce the frozen assisted-workflow news policy."""
    try:
        return _validate_review(review, spec, decision_at, artifacts, store=store)
    except EvidenceError:
        raise
    except (ValueError, KeyError, TypeError, OSError, UnicodeError) as error:
        raise EvidenceError("News review verification failed: " + str(error)) from error


def factual_event_features(review, spec, decision_at, artifacts, *, store):
    """Quoted fact presence in a finite known set, with distinct source clocks."""
    state = validate_review(review, spec, decision_at, artifacts, store=store)
    by_claim = {row["id"]: row for row in review["claims"]}
    active = {row["thesis_id"]: row for row in review["industry_theses"]
              if row["thesis_id"] in state["active_thesis_ids"]}
    result, actions = [], []
    checked_original_manifests = set()
    for event in review["events"]:
        thesis_ids = sorted(key for key, value in active.items() if event["event_id"] in value["event_ids"])
        claims = [by_claim[key] for key in event["claim_ids"] if by_claim[key]["kind"] == "fact"]
        if not claims or not thesis_ids:
            continue
        key = _fact_feature_key(event, claims)
        fact = store.get("news-fact-feature-first-known", key)
        if fact is None:
            actions.append({"action": "obtain_committed_first_fact_feature", "event_id": event["event_id"]})
            continue
        first_review = store.get("news-review", fact["review_id"])
        require(first_review is not None and first_review["reviewed_at"] == fact["first_known_at"]
                and fact["feature_key"] == key, "First fact feature has no committed assessment")
        first_event = next((row for row in first_review["events"] if row["source_revision_id"] == event["source_revision_id"]), None)
        require(first_event is not None and _fact_feature_key(first_event, first_review["claims"]) == key,
                "First fact feature differs from original assessed quotes")
        observations = []
        for version_id in sorted({row["version_id"] for row in claims}):
            first = store.get("news-version-first-observed", version_id)
            if first is None:
                actions.append({"action": "obtain_committed_first_observation", "version_id": version_id})
                continue
            operation = store.get("news-operation", fingerprint({"operation_id": first["collection_id"]}))
            require(operation is not None, "First observation has no committed collection")
            manifest = artifacts.read_json(operation["manifest_ref"])
            manifest_key = fingerprint(operation["manifest_ref"])
            if manifest_key not in checked_original_manifests:
                original_at = instant(manifest["information_cutoff_at"])
                _, original_registry = collection_policy(manifest, store, artifacts)
                _verify_collection_bodies(manifest, original_registry, original_at, original_at, 3600, artifacts, store=store)
                checked_original_manifests.add(manifest_key)
            captured = next((row for row in manifest["retrievals"] if row["retrieval_id"] == first["retrieval_id"]), None)
            require(captured is not None and captured.get("version_id") == version_id
                    and captured["retrieved_at"] == first["observed_at"] and captured["raw_ref"] == first["raw_ref"],
                    "First observation differs from original capture")
            _, original_registry = collection_policy(manifest, store, artifacts)
            source_fetch.validate_capture_provenance(captured, original_registry)
            raw = artifacts.read(first["raw_ref"])
            require(hashlib.sha256(raw).hexdigest() == captured["raw_sha256"], "First observation raw source changed")
            rule = source_fetch.source_rule(captured["registry_source_id"], original_registry)
            version = store.get("news-version", version_id)
            require(version is not None and captured["state"] == "body"
                    and hashlib.sha256(artifacts.read(version["body_ref"])).hexdigest() == version["body_sha256"],
                    "First observation does not reproduce its article version")
            observations.append(first)
        if len(observations) != len({row["version_id"] for row in claims}):
            continue
        observed = max((row["observed_at"] for row in observations), key=instant)
        available = max((observed, event["source_known_at"], fact["first_known_at"]), key=instant)
        require(instant(available) <= instant(decision_at), "Future source fact cannot become a feature")
        result.append({"event_id": event["event_id"], "revision_id": event["revision_id"],
                       "source_revision_id": event["source_revision_id"],
                       "event_type": "source_fact_presence", "thesis_ids": thesis_ids,
                       "sector_ids": sorted({active[key]["sector_id"] for key in thesis_ids if active[key].get("sector_id")}),
                       "known_at": fact["first_known_at"], "revision_known_at": event["source_known_at"],
                       "fact_first_known_at": fact["first_known_at"], "first_seen_at": event["first_seen_at"],
                       "source_observed_at": observed, "first_available_at": available,
                       "available_at": available, "published_at": event["publication_times"],
                       "effective_at": event["effective_from"]["value"] if event["effective_from"]
                           and event["effective_from"]["precision"] == "timestamp" else None,
                       "effective_source": event["effective_from"], "claim_ids": sorted(row["id"] for row in claims),
                       "effective_until": event["effective_until"], "review_by": event["review_by"],
                       "supersedes": event["supersedes"], "retracts": event["retracts"],
                       "evidence_refs": [row["raw_ref"] for row in observations],
                       "quoted_support": [row["support"] for row in claims],
                       "scope": "source_reported_facts_in_sealed_observed_set_not_global_news_truth"})
    deadlines = [instant(review["reviewed_at"]) + dt.timedelta(seconds=spec["news"]["max_age_seconds"])]
    deadlines += [instant(row["valid_until"]) for row in active.values()]
    deadlines += [instant(row["review_by"]) for row in review["events"]]
    complete = all(not row["missing_sources"] for row in state["source_group_coverage"].values())
    import news_economics
    economic = [row for row in review["economic_observations"] if instant(row["known_at"]) <= instant(decision_at)]
    collection = artifacts.read_json(review["collection_manifest_ref"])
    encoding_events, unmapped = [], []
    for event in review["events"]:
        thesis_ids = [key for key, thesis in active.items() if event["event_id"] in thesis["event_ids"]]
        if not thesis_ids:
            continue
        entities = sorted({active[key]["sector_id"] for key in thesis_ids if active[key].get("sector_id")})
        refs = [capture["raw_ref"] for capture in collection["retrievals"]
                if capture.get("version_id") in event["version_ids"] and capture.get("state") == "body"]
        if not entities:
            unmapped.append({"event_id": event["event_id"], "source_revision_id": event["source_revision_id"],
                "sector_id": None, "status": "unsupported", "known_at": event["known_at"], "observation_ids": [],
                "reason": "source_event_has_no_verified_entity_mapping"})
            continue
        encoding_events.append({**event, "sector_ids": entities, "evidence_refs": refs,
                                "available_at": event["known_at"]})
    states = [row for sector in sorted({key for event in encoding_events for key in event["sector_ids"]})
              for row in news_economics.event_entity_states(encoding_events, economic, sector)] + unmapped
    actions += [{"action": "complete_event_economic_encoding", **row} for row in states
                if row["status"] in ("unsupported", "source_gap")]
    return {"features": sorted(result, key=lambda row: (instant(row["available_at"]), row["event_id"])),
            "economic_observations": economic, "event_entity_states": states,
            "input_schema_id": news_economics.INPUT_SCHEMA_ID,
            "required_actions": actions, "news_state": state,
            "coverage": {"absence_proven": complete, "absence_proof_scope": "sealed_finite_input_set",
                         "known_at": review["reviewed_at"], "available_at": review["reviewed_at"],
                         "covered_from_at": review["reviewed_at"], "covered_until_at": min(deadlines).isoformat(),
                         "review_ref": review.get("manifest_ref"), "collection_ref": review["collection_manifest_ref"],
                         "scope": "sealed_finite_input_set", "zero_meaning": "no_observed_matching_event_in_this_set",
                         "source_time_window_complete": False}}


def _validate_review(review, spec, decision_at, artifacts, *, store):
    required = {"schema_version", "review_id", "request_hash", "request_ref", "status", "collection_id",
                "collection_manifest_ref", "cutoff_at", "information_cutoff_at", "claims", "source_results", "evidence_refs",
                "qualification", "reviewed_at", "industry_theses", "events", "required_source_groups", "economic_observations"}
    fields(review, required, {"manifest_ref", "run_id"}, "news review")
    require(review["schema_version"] == 4 and review["status"] == "assessed"
            and isinstance(review["claims"], list) and bool(review["claims"]),
            "Assisted decisions require an assessed nonempty news review")
    require(spec.get("kind") in ("numeric_policy", "assisted_workflow") and spec.get("news", {}).get("mode") == "sealed_inputs",
            "A frozen source-bound news policy is required")
    run, registry = run_policy(review.get("run_id"), store, artifacts)
    policy = spec["news"]
    require(fingerprint(policy) == run["news_policy_hash"], "Review policy differs from authoritative analysis run")
    sources = policy.get("sources")
    require(isinstance(sources, list) and bool(sources)
            and all(isinstance(source_id, str) and source_id in registry["sources"]
                    and registry["sources"][source_id].get("purpose") == "news" for source_id in sources)
            and len(sources) == len(set(sources)), "Frozen news sources must be unique registered news sources")
    require({source_id for members in policy["required_source_groups"].values() for source_id in members} <= set(sources),
            "Source group is outside the frozen strategy")
    max_age = policy.get("max_age_seconds")
    require(type(max_age) is int and max_age > 0, "Positive frozen news age limit required")
    decision, reviewed = instant(decision_at), instant(review["reviewed_at"])
    require(0 <= (decision-reviewed).total_seconds() <= max_age, "News review is future or stale")
    _closed_review_references(review, artifacts, set())
    if "manifest_ref" in review:
        saved = artifacts.read_json(review["manifest_ref"])
        require(fingerprint(saved) == fingerprint({k: v for k, v in review.items() if k != "manifest_ref"}),
                "News review differs from its stored manifest")
    payload = artifacts.read_json(review["request_ref"])
    fields(payload, {"collection_id", "claims", "industry_theses", "events"}, {"run_id", "economic_observations"}, label="original news assessment")
    require(fingerprint(payload) == review["request_hash"] and payload["collection_id"] == review["collection_id"],
            "Original news assessment binding differs")
    collection = artifacts.read_json(review["collection_manifest_ref"])
    collection_policy(collection, store, artifacts)
    run_id = collection["run_id"]
    require(review.get("run_id", run_id) == run_id and payload.get("run_id", run_id) == run_id,
            "News assessment analysis run binding differs")
    require(collection["collection_id"] == review["collection_id"]
            and collection["operation_id"] == review["collection_id"]
            and collection["cutoff_at"] == review["cutoff_at"]
            and collection["information_cutoff_at"] == review["information_cutoff_at"], "News collection binding differs")
    _verify_collection_bodies(collection, registry, reviewed, decision, max_age, artifacts, store=store)
    _verify_source_scopes(collection, registry)
    claims, references = _assemble_claims(payload, collection, artifacts, set(sources), registry)
    require(bool(claims) and fingerprint(claims) == fingerprint(review["claims"]),
            "News claims differ from recomputed original quotes")
    require(_source_groups(policy["required_source_groups"], registry) == collection["required_source_groups"] == review["required_source_groups"],
            "Source group policy changed")
    require(collection["source_group_coverage"] == _group_coverage(collection["required_source_groups"], collection["source_results"]),
            "Source group coverage changed")
    events = _assemble_events(payload, collection, claims, artifacts, review["reviewed_at"], registry)
    for event in events:
        committed = store.get("news-event-revision", event["revision_id"])
        require(committed is not None, "Source event revision is not committed")
        event["known_at"] = committed["known_at"]
        event["first_seen_at"] = committed["first_seen_at"]
        event["publication_role"] = committed["publication_role"]
        first_source = store.get("news-source-revision-first-known", event["source_revision_id"])
        require(first_source is not None and first_source["source_revision_id"] == event["source_revision_id"],
                "Source event revision lacks committed first knowledge")
        event["source_known_at"] = first_source["first_known_at"]
        require(event == committed, "Stored event revision differs from its source-derived request")
    require(events == review["events"], "Review event binding changed")
    import news_economics
    economic = news_economics.assemble(payload.get("economic_observations", []), collection, claims, events,
                                       artifacts, review["reviewed_at"], store=store, registry=registry)
    require(economic == review["economic_observations"], "Economic observations differ from recomputed original measurements")
    for row in economic:
        require(store.get("news-economic-observation", row["observation_id"]) == row,
                "Economic observation is not the committed source-bound record")
    require(fingerprint(_assemble_theses(payload, claims, events, collection["required_source_groups"])) == fingerprint(review["industry_theses"]),
            "Industry theses differ from the assessed claims and original request")
    require(fingerprint(references) == fingerprint(review["evidence_refs"])
            and fingerprint(collection["source_results"]) == fingerprint(review["source_results"]),
            "News evidence or coverage inventory differs")
    require(review["qualification"] == "source_quotes_checked_not_truth_proven", "Unsupported news qualification")
    return {**event_state(review, collection, decision_at, store), "status": "passed", "review_id": review["review_id"], "collection_id": review["collection_id"],
            "review_hash": fingerprint(review), "source_ids": sorted({versions["source_id"]
                for versions in collection["versions"] if any(claim["version_id"] == versions["version_id"]
                or any(item.get("version_id") == versions["version_id"] for item in claim["counterevidence"])
                for claim in claims)}), "scope": "quote_and_artifact_consistency_not_truth"}
