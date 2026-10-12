"""Source-backed news capture; reports evidence scope, never invents analysis."""

import datetime as dt
import hashlib
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from contracts import ContractError, EvidenceError, canonical_bytes, fields, fingerprint, instant, require, strict_json_loads, utc_now
import source_fetch
from registry_context import read_registry, registry_fingerprint


class _Page(HTMLParser):
    def __init__(self, body_ids, body_classes=(), publication_classes=(), body_ancestor_tags=(), body_tags=()):
        super().__init__(convert_charrefs=True)
        self.body_ids = set(body_ids)
        self.body_classes = set(body_classes)
        self.publication_classes = set(publication_classes)
        self.body_ancestor_tags = set(body_ancestor_tags)
        self.body_tags = set(body_tags)
        self.body_container_count = 0
        self.publication_depth, self.publication_parts = None, []
        self.stack, self.parts, self.links, self.meta, self.title = [], [], [], {}, []
        self.target_depth = None
        self.body_closed = False
        self.body_found = False
        self.script_count, self.jsonld_scripts, self.jsonld_active = 0, [], None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.script_count += 1
            if attributes.get("type", "").split(";", 1)[0].strip().lower() == "application/ld+json":
                line, column = self.getpos()
                self.jsonld_active = {"locator": "html/script/"+str(self.script_count),
                    "line": line, "column": column, "src": attributes.get("src"), "parts": [], "closed": False}
                self.jsonld_scripts.append(self.jsonld_active)
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
        if (attributes.get("id") in self.body_ids or self.body_classes.intersection(attributes.get("class", "").split()) or tag in self.body_tags) and self.body_ancestor_tags <= set(self.stack[:-1]):
            self.body_container_count += 1
            if not self.body_found:
                self.body_found, self.target_depth = True, len(self.stack)

    def handle_endtag(self, tag):
        if tag == "script" and self.jsonld_active is not None:
            self.jsonld_active["closed"] = True
            self.jsonld_active = None
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
        if self.jsonld_active is not None:
            self.jsonld_active["parts"].append(data)
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


def _publication_bounds(value):
    require(value is not None and value["precision"] in ("date", "timestamp"), "Publisher date is not parsed")
    if value["precision"] == "timestamp":
        stamp = instant(value["value"])
        return stamp, stamp
    start = dt.datetime.combine(dt.date.fromisoformat(value["value"]), dt.time(), ZoneInfo(value["timezone"]))
    end = start + dt.timedelta(days=1)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)-dt.timedelta(microseconds=1)


def _compatible_publications(left, right):
    a, b = _publication_bounds(left), _publication_bounds(right)
    return max(a[0], b[0]) <= min(a[1], b[1])


def _jsonld_uri(value, page_url):
    if type(value) is dict:
        require(set(value) <= {"@id", "@type", "url"}, "Unsupported JSON-LD page reference")
        values = [value[key] for key in ("@id", "url") if key in value]
        require(values, "JSON-LD page reference needs its original URL")
        resolved = [_jsonld_uri(item, page_url) for item in values]
        require(len({_document_url(uri) for uri in resolved}) == 1, "Conflicting JSON-LD page-reference URLs")
        return resolved[0]
    require(type(value) is str and value.strip(), "JSON-LD identity URL must be text")
    uri = urllib.parse.urlsplit(urllib.parse.urljoin(page_url, value.strip()))
    require(uri.scheme == "https" and uri.hostname and not uri.username and not uri.password
            and uri.port in (None, 443), "JSON-LD identity needs a verified HTTPS page URL")
    return urllib.parse.urlunsplit(("https", uri.hostname.lower(), uri.path or "/", uri.query, uri.fragment))


def _jsonld_nodes(value, pointer="", depth=0):
    require(depth <= 16, "JSON-LD graph nesting exceeded its parsing budget")
    if type(value) is list:
        require(len(value) <= 4096, "JSON-LD graph node budget exceeded")
        for index, item in enumerate(value):
            yield from _jsonld_nodes(item, pointer+"/"+str(index), depth+1)
    else:
        require(type(value) is dict, "JSON-LD graph nodes must be objects")
        yield pointer, value
        if "@graph" in value:
            require(type(value["@graph"]) is list, "JSON-LD graph must be an array")
            yield from _jsonld_nodes(value["@graph"], pointer+"/@graph", depth+1)


def _jsonld_dates(page, capture, rule):
    """Original local article metadata, never an inferred clock or script execution.

    This declared adapter is versioned separately from legacy metadata parsing.
    Its locators and literals are rebuilt from the original captured HTML by the
    independent collection validator; caller flags cannot qualify a publication.
    """
    if not page.jsonld_scripts:
        return None, None
    require(len(page.jsonld_scripts) <= 128, "JSON-LD script budget exceeded")
    raw = capture.get("raw_bytes")
    require(type(raw) is bytes and 0 < len(raw) <= 2*1024*1024, "Original JSON-LD page bytes required")
    text, _ = source_fetch.decode(raw, capture["content_type"], capture.get("encoding"), rule)
    require(text == capture["text"], "JSON-LD source text differs from original page bytes")
    page_url = _jsonld_uri(capture["final_url"], capture["final_url"])
    page_document = _document_url(page_url)
    matched, node_count = [], 0
    for script in page.jsonld_scripts:
        require(script["closed"] and not script["src"], "JSON-LD needs a complete local script")
        literal = "".join(script["parts"])
        payload = literal.strip()
        # One terminal semicolon is present in WHO's original JSON-LD template.
        # No assignment, callback, comments or executable suffix is accepted.
        if payload.endswith(";"):
            payload = payload[:-1].rstrip()
        try:
            graph = strict_json_loads(payload)
        except RecursionError as error:
            raise ContractError("JSON-LD nesting exceeded its parsing budget") from error
        for pointer, node in _jsonld_nodes(graph):
            node_count += 1
            require(node_count <= 4096, "JSON-LD graph node budget exceeded")
            types = node.get("@type", [])
            types = [types] if type(types) is str else types
            require(type(types) is list and all(type(item) is str for item in types), "JSON-LD type must be text")
            if not set(types) & {"Article", "NewsArticle", "https://schema.org/Article", "https://schema.org/NewsArticle",
                                "http://schema.org/Article", "http://schema.org/NewsArticle"}:
                continue
            identities = [_jsonld_uri(node[key], page_url) for key in ("@id", "url", "mainEntityOfPage") if key in node]
            if not identities or any(_document_url(uri) != page_document for uri in identities):
                continue
            subject = _jsonld_uri(node["@id"], page_url) if "@id" in node else None
            matched.append((subject, pointer, node, script, literal))
    if not matched:
        return None, None
    require(len(matched) == 1 or (all(row[0] is not None for row in matched)
            and len({row[0] for row in matched}) == 1), "Multiple JSON-LD article subjects match this page")

    def read_date(field):
        selected = [row for row in matched if field in row[2]]
        if not selected:
            return None
        require(field != "datePublished" or len(selected) == len(matched), "Incomplete same-subject JSON-LD first-publication metadata")
        dates, locators = [], []
        for subject, pointer, node, script, literal in selected:
            value = node[field]
            require(type(value) is str and value.strip(), "JSON-LD publication date must be text")
            parsed = _publication({"pubdate": value}, timezone=rule["timezone"], formats=rule.get("publication_formats", ()))
            require(parsed["precision"] in ("date", "timestamp"), "JSON-LD publication date is not parsed")
            fractions = re.findall(r"[.,]([0-9]+)", value)
            require(all(not any(digit != "0" for digit in fraction[6:]) for fraction in fractions),
                    "JSON-LD clock has unsupported submicrosecond precision")
            require(all(_compatible_publications(parsed, previous) for previous in dates),
                    "Conflicting same-subject JSON-LD "+field)
            dates.append(parsed)
            locators.append({"script_locator": script["locator"], "line": script["line"], "column": script["column"],
                "script_sha256": hashlib.sha256(literal.encode("utf-8")).hexdigest(),
                "json_pointer": pointer+"/"+field, "raw_value": value, "subject_id": subject})
        parsed = next((value for value in dates if value["precision"] == "timestamp"), dates[0])
        return {**parsed, "basis": "publisher_jsonld_article_metadata", "evidence": {
            "parser_id": "article_jsonld_v1", "page_url": page_document,
            "raw_sha256": hashlib.sha256(raw).hexdigest(), "locators": locators}}

    return read_date("datePublished"), read_date("dateModified")


def parse_capture(capture, rule, *, linked_parent=None):
    """A closed registered article container is a supported body, not true news."""
    text = capture["text"]
    path = urllib.parse.urlsplit(capture["final_url"]).path
    if (rule.get("feed_format") == "rss" or rule.get("listing_adapter") == "semantic_site_v1") and capture["content_type"] in ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml"):
        require(text is not None and "<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper(), "Unsupported RSS document declarations")
        root = ET.fromstring(text)
        if root.tag == "{http://www.w3.org/2005/Atom}feed":
            rows = root.findall("{http://www.w3.org/2005/Atom}entry")
            require(len(rows) <= 5000, "Official Atom feed exceeds its per-page parsing budget")
            links = [item.attrib.get("href") for row in rows for item in row.findall("{http://www.w3.org/2005/Atom}link") if item.attrib.get("rel", "alternate") == "alternate"]
            following = [item.attrib.get("href") for item in root.findall("{http://www.w3.org/2005/Atom}link") if item.attrib.get("rel") == "next"]
            require(all(type(link) is str and link for link in links+following), "Atom source link is missing")
            return {"state": "lead", "reason": "official_atom_is_discovery_not_article_body", "body": None,
                "links": links, "listing_links": following, "parser_id": "official_atom_v1"}
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
    page = _Page(rule.get("body_ids", []), rule.get("body_classes", []), rule.get("publication_classes", []), rule.get("body_ancestor_tags", []), rule.get("body_tags", []))
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
        if rule.get("publication_adapter") == "article_jsonld_v1":
            json_published, json_modified = _jsonld_dates(page, capture, rule)
            for original, extracted, label in ((published, json_published, "datePublished"), (modified, json_modified, "dateModified")):
                require(original is None or extracted is None or _compatible_publications(original, extracted),
                        "JSON-LD and original publisher metadata conflict: "+label)
            published = json_published if json_published is not None else published
            modified = json_modified if json_modified is not None else modified
    except ValueError:
        published, modified = None, None
    if rule.get("article_adapter") == "semantic_article_v1":
        # A discovered website has no invented site-specific body selector.
        # Original JSON-LD binds the Article subject to this URL; the body is
        # either its literal articleBody or one closed HTML article element.
        semantic_bodies = []
        try:
            for script in page.jsonld_scripts:
                literal = "".join(script["parts"]).strip()
                if literal.endswith(";"):
                    literal = literal[:-1].rstrip()
                for _, node in _jsonld_nodes(strict_json_loads(literal)):
                    types = node.get("@type", [])
                    types = [types] if type(types) is str else types
                    if type(types) is not list or not all(type(item) is str for item in types) or not set(types) & {"Article", "NewsArticle", "https://schema.org/Article", "https://schema.org/NewsArticle", "http://schema.org/Article", "http://schema.org/NewsArticle"}:
                        continue
                    identities = [_jsonld_uri(node[key], capture["final_url"]) for key in ("@id", "url", "mainEntityOfPage") if key in node]
                    if not identities or any(_document_url(uri) != _document_url(capture["final_url"]) for uri in identities):
                        continue
                    if "articleBody" in node:
                        require(type(node["articleBody"]) is str, "Article body metadata must be original text")
                        semantic_bodies.append(node["articleBody"].strip())
            require(not semantic_bodies or len(set(semantic_bodies)) == 1, "Same-subject article bodies conflict")
            if semantic_bodies:
                body = semantic_bodies[0]
            else:
                require(page.body_container_count == 1 and page.body_closed, "One complete semantic article body is required")
            require(published is not None and published.get("basis") == "publisher_jsonld_article_metadata", "Semantic article has no URL-bound original publication metadata")
            if rule.get("require_explicit_publication_offset"):
                for original_date in (published, modified):
                    if original_date is not None:
                        require(original_date["precision"] == "timestamp" and original_date.get("basis") == "publisher_jsonld_article_metadata" and all(re.search(r"(?:Z|[+-][0-9]{2}:[0-9]{2})$", row["raw_value"].strip()) for row in original_date["evidence"]["locators"]), "Publisher timezone is unknown; original offset is required")
            require(len(body) >= rule.get("minimum_body_characters", 60), "Semantic body is insufficient")
            state, reason = "body", None
        except ValueError as error:
            body, state, reason = "", "lead", "semantic_article_protocol_unproved: "+str(error)
        if blocked:
            body, state, reason = "", "unavailable", "access_challenge"
        if state == "lead":
            # Explore every original channel entrance, without turning an
            # unknown whole website into an unbounded unrelated-site crawl.
            prefixes = [urllib.parse.urlsplit(url).path.rstrip("/") for url in rule.get("original_news_entrances", [])]
            page.links = [link for link in page.links if any(urllib.parse.urlsplit(urllib.parse.urljoin(capture["final_url"], link)).path.startswith(prefix+"/") or urllib.parse.urlsplit(urllib.parse.urljoin(capture["final_url"], link)).path == prefix for prefix in prefixes)]
    listing_links = []
    if state == "lead" and rule.get("listing_adapter") == "csrc_json":
        channel = page.meta.get("channelid")
        require(type(channel) is str and re.fullmatch(r"[a-f0-9]{32}", channel), "CSRC current list has no valid channel identity")
        listing_links = ["https://www.csrc.gov.cn/searchList/"+channel+"?_isAgg=true&_isJson=true&_pageSize=18&_template=index&page=1"]
        # Footer contact/privacy articles have the same URL suffix as news.
        # This listing's authoritative current article set comes from its
        # published channel API, never from unrelated navigation links.
        page.links = []
    if state == "lead" and rule.get("listing_adapter") == "ndrc_static":
        match = re.search(r"createPageHTML\(\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*[\"']index[\"']\s*,\s*[\"']html[\"']\s*\)", text)
        if match and int(match[2])+1 < int(match[1]):
            listing_links = [urllib.parse.urljoin(capture["final_url"], "index_"+str(int(match[2])+1)+".html")]
    return {"state": state, "reason": reason, "body": body or None, "title": title,
            "published_at": published, "modified_at": modified, "links": page.links, "listing_links": listing_links,
            "parser_id": "URL_bound_semantic_article_v1" if rule.get("article_adapter") == "semantic_article_v1" else "registered_article_container_jsonld_v2" if rule.get("publication_adapter") == "article_jsonld_v1"
                else "registered_article_container_v1", "fact_verification": "not_performed"}


def _document_url(url):
    parsed = urllib.parse.urlsplit(url)
    # Query parameters can identify different official documents. Tracking-only
    # aliases require an explicit source rule or numbered-event evidence.
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def publisher_identity(url):
    host = urllib.parse.urlsplit(url).hostname.lower()
    return host[4:] if host.startswith("www.") else host


def canonical_article_identity(url):
    parsed = urllib.parse.urlsplit(_document_url(url))
    canonical = urllib.parse.urlunsplit(("https", publisher_identity(url), parsed.path, parsed.query, ""))
    return fingerprint({"canonical_article_url": canonical})


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
    registry = read_registry(run, store, artifacts)
    policy = run["news_policy"]
    require(run["source_registry_hash"] == registry_fingerprint(registry)
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
            and mode in ("automatic", "automatic_shard", "automatic_aggregate", "explicit"), "Collection selected sources are outside frozen policy")
    require(mode != "automatic" or set(selected) == set(run["news_policy"]["sources"]),
            "Automatic collection omitted a frozen source")
    require(mode != "automatic_aggregate" or set(selected) == set(run["news_policy"]["sources"]),
            "Aggregate collection omitted a frozen source")
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
    fields(payload, {"cutoff_at", "window_start", "required_source_groups", "run_id"}, {"sources", "source_ids", "collection_ids", "unexamined_source_ids", "retrieval_operation_id", "derived_source_tasks_ref", "timeout_seconds", "max_urls_per_source", "budget_seconds", "refresh_tracked_articles", "max_pages_per_source"}, "news request")
    require("unexamined_source_ids" not in payload or "collection_ids" in payload, "Unexamined whole-domain scopes require a canonical aggregate")
    require("retrieval_operation_id" not in payload or "collection_ids" in payload,
            "Original opportunity complements require a canonical aggregate")
    require("derived_source_tasks_ref" not in payload or {"collection_ids", "retrieval_operation_id"} <= payload.keys(),
            "Source-derived tasks require their exact original selected aggregate")
    if "collection_ids" in payload:
        require(not {"sources", "source_ids", "timeout_seconds", "max_urls_per_source", "budget_seconds", "refresh_tracked_articles", "max_pages_per_source"} & payload.keys(), "Aggregate cannot launch new source requests")
        from news_scope import aggregate
        return aggregate(payload, store, artifacts, operation_id)
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
    selected_ids = payload.get("source_ids")
    require(selected_ids is None or selections is None, "An automatic shard cannot submit explicit URLs")
    require(selected_ids is None or type(selected_ids) is list and selected_ids and len(selected_ids) == len(set(selected_ids))
            and set(selected_ids) <= set(run["news_policy"]["sources"]), "Automatic shard sources are outside the frozen policy")
    scheduler_key = fingerprint({"registry_hash": registry_fingerprint(registry), "news_policy_hash": run["news_policy_hash"]})
    scheduler = store.get("news-source-scheduler", scheduler_key) or {}
    if selections is None:
        selections = [{"source_id": name, "urls": rule["default_urls"]}
                      for name, rule in sorted(registry["sources"].items(), key=lambda row: row[1].get("priority", 100))
                      if rule["purpose"] == "news" and name in (selected_ids or run["news_policy"]["sources"])]
        source_ids = [item["source_id"] for item in selections]
        if scheduler.get("next_source_id") in source_ids:
            offset = source_ids.index(scheduler["next_source_id"])
            selections = selections[offset:]+selections[:offset]
    require(isinstance(selections, list) and 0 < len(selections) <= len(run["news_policy"]["sources"]), "A frozen finite source selection is required")
    names = set()
    for selected in selections:
        fields(selected, {"source_id", "urls"}, label="source selection")
        require(selected["source_id"] not in names, "Duplicate news source")
        names.add(selected["source_id"])
        rule = source_fetch.source_rule(selected["source_id"], registry)
        require(rule["purpose"] == "news" and selected["source_id"] in run["news_policy"]["sources"],
                "Explicit source is outside frozen news policy")
        require(isinstance(selected["urls"], list) and 0 < len(selected["urls"]) <= max(5000, rule.get("original_opportunity_url_count", 0)), "Finite original source URL list required")
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
        source_limit = payload.get("max_urls_per_source", rule.get("default_max_urls_per_source", limit))
        require(type(source_limit) is int and 1 <= source_limit <= 20, "Registered news URL budget must be 1-20")
        pending = list(dict.fromkeys(source_fetch.checked_url(url, rule) for url in selected["urls"]))
        initial_urls = list(pending)
        if selected_ids and rule.get("original_opportunity_url_count") is not None:
            pending = [url for url in pending if store.get("news-run-url-observed", fingerprint({"run_id": run_id, "source_id": name, "url": url})) is None]
        watermark_key = fingerprint({"source_id": name, "registry_hash": registry_fingerprint(registry),
                                     "news_policy_hash": run["news_policy_hash"]})
        previous_watermark = store.get("news-source-watermark", watermark_key) or {}
        if not payload.get("sources"):
            refresh_urls = previous_watermark.get("tracked_article_urls", [])[:refresh_limit]
            pending = list(dict.fromkeys(pending + refresh_urls + previous_watermark.get("pending_urls", [])))
        next_kind = previous_watermark.get("next_task_kind", "article")
        requested_articles = []
        parent_urls = dict(previous_watermark.get("linked_parent_urls", {})) if not payload.get("sources") else {}
        if not payload.get("sources") and not any(url in parent_urls for url in pending):
            # Every new invocation starts with the publisher's current head.
            # Old pagination is preserved, but cannot displace that head.
            # A pending PDF still gets its atomic original-parent dependency.
            next_kind = "listing"
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
        while pending and len(visited) < source_limit and time.monotonic() < deadline:
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
            if rule.get("listing_adapter") != "semantic_site_v1" and listing_pages >= maximum_pages and re.search(rule.get("listing_pattern", r"$^"), urllib.parse.urlsplit(url).path):
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
                    doc_id = canonical_article_identity(capture["final_url"])
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
                    next_pages = parsed.get("listing_links", []) if parsed["state"] == "lead" else []
                    links = parsed.get("links", []) + next_pages
                    queued_articles = 0
                    fresh_articles = []
                    fresh_listings = []
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
                        current_channel = (rule.get("listing_adapter") == "csrc_json"
                                           and capture["content_type"] == "text/html" and target in next_pages)
                        if not payload.get("sources") and (is_article or is_listing) and not current_channel and store.get("news-run-url-observed", fingerprint({"run_id": run_id, "source_id": name, "url": target})) is not None:
                            # A current-run completed original is already part
                            # of the parent aggregate. A fresh invocation has a
                            # new run and really fetches it again; no old cache
                            # is mistaken for current evidence. The CSRC head's
                            # current channel response is refreshed each time;
                            # a previous page 1 cannot reveal newly added rows.
                            continue
                        if parsed["state"] == "body" and not (is_pdf and rule.get("pdf_attachments")):
                            continue
                        if is_pdf and parsed["state"] == "body":
                            linked_parents[target] = retrieval_id
                            parent_urls[target] = capture["final_url"]
                        if parsed["state"] == "lead" and target not in visited and not payload.get("sources"):
                            if target in next_pages:
                                fresh_listings.append(target)
                            if is_article:
                                target_document = canonical_article_identity(target)
                                if store.get("news-document", target_document) is None:
                                    fresh_articles.append(target)
                        if (is_article or is_listing) and target not in visited and target not in pending:
                            pending.append(target)
                            queued_articles += int(is_article)
                    pending_set = set(pending)
                    priority = list(dict.fromkeys(item for item in fresh_articles + fresh_listings if item in pending_set))
                    if priority:
                        # A changed publisher head must be reviewed before an
                        # old continuation backlog. Retain the backlog so an
                        # observed fresh head cannot imply exhaustive coverage.
                        fresh_set = set(priority)
                        pending = priority + [item for item in pending if item not in fresh_set]
                        if fresh_listings and not fresh_articles:
                            # Dynamic heads (CSRC) need their current channel
                            # response before any inherited article backlog.
                            next_kind = "listing"
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
                 "urls": requested_articles if automatic_scope else initial_urls, "registry_hash": registry_fingerprint(registry),
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
                "registry_hash": registry_fingerprint(registry), "registry_snapshot_ref": run["registry_snapshot_ref"],
                "news_policy_hash": run["news_policy_hash"], "selected_source_ids": sorted(names),
                "source_selection_mode": "explicit" if payload.get("sources") else "automatic_shard" if selected_ids else "automatic", "cutoff_at": payload["cutoff_at"],
                "information_cutoff_at": information_cutoff_at,
                "window_start": payload["window_start"], "required_source_groups": groups,
                "source_group_coverage": _group_coverage(groups, source_results),
                "source_results": source_results, "retrievals": retrievals,
                "versions": list({v["version_id"]: v for v in versions}.values()), "analysis": [],
                "fact_verification": "not_performed", "coverage_complete_for_source_time_window": False,
                "budget_seconds": budget, "capture_capacity": len(selections)*20}
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
            if retrieval["state"] in {"lead", "body"}:
                store.put("news-run-url-observed", fingerprint({"run_id": run_id, "source_id": retrieval["registry_source_id"], "url": retrieval["requested_url"]}),
                    {"run_id": run_id, "retrieval_id": retrieval["retrieval_id"], "raw_ref": retrieval["raw_ref"]}, immutable=False, conn=conn)
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
        require(result["registry_hash"] == registry_fingerprint(registry) and result["scope_key"] == fingerprint(scope)
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
    require(type(payload["events"]) is list, "Finite original source events required")
    versions = {row["version_id"]: row for row in manifest["versions"]}
    by_claim = {row["id"]: row for row in claims}
    result, seen = [], set()
    for item in payload["events"]:
        _event_structure(item, versions, by_claim)
        documents = sorted({versions[key]["document_id"] for key in item["version_ids"]})
        key = item["event_key"]
        bodies = {version: artifacts.read(versions[version]["body_ref"]).decode("utf-8") for version in item["version_ids"]}
        if key is not None:
            require(all(key in body for body in bodies.values()), "Official event number must occur in every aliased document")
        else:
            require(len(documents) == 1, "Unnumbered cross-document aliases require source review")
        event_id = fingerprint({"official_number": key}) if key else documents[0]
        require(event_id not in seen, "Duplicate event in one assessment")
        seen.add(event_id)
        require(instant(item["review_by"]) > instant(known_at), "Event analytical review deadline must be prospective")
        evidence = item["temporal_evidence"]
        for row in evidence:
            require(row["field"] in ("event_at", "effective_from", "effective_until", "supersedes", "retracts")
                    and row["version_id"] in bodies and type(row["quote"]) is str and row["quote"]
                    and row["quote"] in bodies[row["version_id"]], "Temporal quote lacks original source support")
        for field in ("event_at", "effective_from", "effective_until"):
            value = item[field]
            if value is None:
                continue
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
    require(isinstance(payload["claims"], list), "Finite source-quoted claim list required")
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
                "canonical_article_id": canonical_article_identity(capture["final_url"]),
                "publisher_identity": publisher_identity(capture["final_url"]),
                "quote_start": body.index(quote), "quote_length": len(quote)}
    ids, claims = set(), []
    for item in payload["claims"]:
        _claim_structure(item, ids, versions)
        primary = support(item["version_id"], item["quote"])
        event_at = item.get("event_at")
        counter = []
        for evidence in item["counterevidence"]:
            if isinstance(evidence, dict) and set(evidence) == {"missing_reason"}:
                counter.append(dict(evidence))
            else:
                checked = support(evidence["version_id"], evidence["quote"])
                counter.append({**evidence, "support": checked,
                    "source_independent_of_primary": checked["publisher_identity"] != primary["publisher_identity"] and checked["canonical_article_id"] != primary["canonical_article_id"] and checked["body_sha256"] != primary["body_sha256"],
                    "independence_scope": "different_observed_publisher_article_and_body_not_proof_of_independent_event_generation",
                    "different_original_article": checked["canonical_article_id"] != primary["canonical_article_id"]})
        claims.append({**item, "support": primary, "url": primary["source_url"],
                       "published_at": versions[item["version_id"]]["published_at"],
                       "event_at": event_at, "event_time_basis": "analyst_attestation" if event_at else "unknown",
                       "counterevidence": counter,
                       "qualification": "source_reported" if item["kind"] == "fact" else "analyst_inference"})
    return claims, list(evidence_refs.values())


def _draft_require(condition, field, message):
    """Keep the formal predicate and expose its draft field to read-only callers."""
    if not condition:
        error = ContractError(message)
        error.draft_field = field
        raise error


def _claim_structure(item, seen, versions):
    fields(item, {"id", "kind", "text", "version_id", "quote", "categories", "direction",
                  "counterevidence", "reviewer", "review_method"}, {"event_at"}, label="news claim")
    _draft_require(isinstance(item["id"], str) and item["id"] and item["id"] not in seen, "id", "Claim id must be unique")
    seen.add(item["id"])
    _draft_require(type(item["version_id"]) is str and item["version_id"] in versions,
                   "version_id", "Claim needs a body version available in this collection before cutoff")
    _draft_require(item["kind"] in ("fact", "inference"), "kind", "Unknown news claim kind")
    _draft_require(item["direction"] in ("increase", "decrease", "watch", "neutral"), "direction", "Unknown category direction")
    for key in ("text", "reviewer", "review_method"):
        _draft_require(isinstance(item[key], str) and item[key].strip(), key, "Claim text, reviewer and review method are required")
    _draft_require(isinstance(item["quote"], str) and item["quote"].strip(), "quote", "A nonempty verbatim source quote is required")
    _draft_require(isinstance(item["categories"], list) and item["categories"]
                   and all(isinstance(c, str) and c.strip() for c in item["categories"]), "categories", "Claim categories required")
    if item.get("event_at") is not None:
        value = item["event_at"]
        _draft_require(isinstance(value, str), "event_at", "Event time must be explicit ISO text or null")
        try:
            dt.date.fromisoformat(value) if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else instant(value)
        except ValueError as error:
            error.draft_field = "event_at"
            raise
    _draft_require(isinstance(item["counterevidence"], list) and item["counterevidence"],
                   "counterevidence", "Counterevidence or an explicit missing reason is required")
    for index, evidence in enumerate(item["counterevidence"]):
        key = f"counterevidence[{index}]"
        if isinstance(evidence, dict) and set(evidence) == {"missing_reason"}:
            _draft_require(isinstance(evidence["missing_reason"], str) and evidence["missing_reason"].strip(),
                           key+".missing_reason", "Missing counterevidence reason required")
        else:
            fields(evidence, {"version_id", "quote", "text"}, label="counterevidence")
            _draft_require(type(evidence["version_id"]) is str and evidence["version_id"] in versions,
                           key+".version_id", "Claim needs a body version available in this collection before cutoff")
            _draft_require(isinstance(evidence["text"], str) and evidence["text"].strip(), key+".text", "Counterevidence description required")
            _draft_require(isinstance(evidence["quote"], str) and evidence["quote"].strip(), key+".quote", "A nonempty verbatim source quote is required")


def _event_structure(item, versions, by_claim):
    fields(item, {"event_key", "version_ids", "claim_ids", "event_at", "effective_from", "effective_until",
                  "review_by", "supersedes", "retracts", "temporal_evidence"}, label="source event")
    for key, known, message in (("version_ids", versions, "Event needs captured document versions"),
                                ("claim_ids", by_claim, "Event needs assessed claims")):
        _draft_require(type(item[key]) is list and bool(item[key]) and all(type(value) is str for value in item[key])
                       and len(set(item[key])) == len(item[key]) and set(item[key]) <= set(known), key, message)
    _draft_require(all(by_claim[key].get("version_id") in item["version_ids"] for key in item["claim_ids"]),
                   "claim_ids", "Event claim/version differs")
    key = item["event_key"]
    _draft_require(key is None or (type(key) is str and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,}〔[0-9]{4}〕[0-9]+号", key)),
                   "event_key", "Event alias must be a complete officially numbered document, or null")
    try:
        instant(item["review_by"])
    except ValueError as error:
        error.draft_field = "review_by"
        raise
    evidence = item["temporal_evidence"]
    _draft_require(type(evidence) is list, "temporal_evidence", "Temporal source evidence must be a list")
    for index, row in enumerate(evidence):
        fields(row, {"field", "version_id", "quote"}, label="event temporal evidence")
        _draft_require(row["field"] in ("event_at", "effective_from", "effective_until", "supersedes", "retracts")
                       and type(row["version_id"]) is str and row["version_id"] in item["version_ids"]
                       and type(row["quote"]) is str and bool(row["quote"]),
                       f"temporal_evidence[{index}]", "Temporal quote lacks original source support")
    for field in ("event_at", "effective_from", "effective_until"):
        value = item[field]
        if value is None:
            continue
        fields(value, {"value", "precision", "timezone"}, label="source event date")
        _draft_require(value["precision"] in ("date", "timestamp"), field+".precision", "Explicit source date precision required")
        try:
            _event_boundary(value)
        except (ValueError, TypeError, KeyError) as error:
            _draft_require(False, field, str(error))
        _draft_require(any(row["field"] == field for row in evidence), field,
                       "Event date timezone must match its publisher")
    _draft_require(item["effective_until"] is None or item["effective_from"] is None
                   or _event_boundary(item["effective_from"]) < _event_boundary(item["effective_until"], end=True),
                   "effective_until", "Administrative interval is reversed")
    for field in ("supersedes", "retracts"):
        _draft_require(type(item[field]) is list and all(type(value) is str and re.fullmatch("[0-9a-f]{64}", value) for value in item[field])
                       and len(set(item[field])) == len(item[field]), field, "Event revision links must be distinct event identities")
        _draft_require(not item[field] or any(row["field"] == field for row in evidence), field, "Revision relation needs original quoted evidence")


def _thesis_structure(item, seen, claim_ids, event_claims, groups):
    """The single pure structural contract used by preview and final assembly.

    Source availability, quote truth/support, freshness and economic semantics
    are deliberately verified by the ordinary source validators afterward.
    """
    fields(item, {"thesis_id", "kind", "label", "direction", "horizon_days", "search_terms", "claim_ids", "event_ids",
                  "required_source_groups", "valid_until", "event_basis"}, {"sector_id"}, label="industry thesis")
    if "sector_id" in item:
        _draft_require(type(item["sector_id"]) is str and item["sector_id"].strip() == item["sector_id"]
                       and 0 < len(item["sector_id"]) <= 100, "sector_id", "Stable explicit thesis sector identity required")
    for key in ("thesis_id", "kind", "label", "direction"):
        _draft_require(type(item[key]) is str and bool(item[key].strip()), key,
                       "Thesis identifiers, kind, label and direction must be nonempty text")
    _draft_require(item["thesis_id"] not in seen, "thesis_id", "Industry thesis identifiers must be unique")
    seen.add(item["thesis_id"])
    _draft_require(item["kind"] in {"asset_class", "industry", "theme", "broad_market"}, "kind", "Unknown thesis kind")
    _draft_require(item["direction"] in {"increase", "decrease", "hold", "watch"}, "direction", "Unknown thesis direction")
    _draft_require(type(item["horizon_days"]) is int and item["horizon_days"] > 0,
                   "horizon_days", "Positive thesis horizon required")
    for key in ("search_terms", "claim_ids"):
        _draft_require(type(item[key]) is list and 0 < len(item[key])
                       and all(type(value) is str and value.strip() for value in item[key])
                       and len(set(item[key])) == len(item[key]), key, "Distinct nonempty thesis " + key + " required")
    _draft_require(set(item["claim_ids"]) <= claim_ids, "claim_ids", "Thesis needs assessed supporting claims")
    _draft_require(item["event_basis"] in ("standing_policy", "announcement_information"),
                   "event_basis", "Explicit thesis event basis required")
    _draft_require(type(item["event_ids"]) is list and bool(item["event_ids"])
                   and all(type(value) is str for value in item["event_ids"])
                   and set(item["event_ids"]) <= set(event_claims), "event_ids", "Thesis requires declared source events")
    _draft_require(type(item["required_source_groups"]) is list and bool(item["required_source_groups"])
                   and all(type(value) is str for value in item["required_source_groups"])
                   and set(item["required_source_groups"]) <= set(groups),
                   "required_source_groups", "Thesis source groups must belong to collection policy")
    try:
        instant(item["valid_until"])
    except ValueError as error:
        error.draft_field = "valid_until"
        raise
    supported = {claim for event in item["event_ids"] for claim in event_claims[event]}
    _draft_require(set(item["claim_ids"]) <= supported, "claim_ids", "Thesis claims must belong to its declared events")


def _assemble_theses(payload, claims, events, groups):
    """Bind each research direction to assessed claims, retaining its inference scope."""
    items = payload["industry_theses"]
    require(type(items) is list, "A finite source-supported industry thesis list is required")
    by_id, seen, result = {claim["id"]: claim for claim in claims}, set(), []
    event_claims = {event["event_id"]: event["claim_ids"] for event in events}
    for item in items:
        _thesis_structure(item, seen, set(by_id), event_claims, groups)
        supports = [by_id[claim_id]["support"] for claim_id in item["claim_ids"]]
        result.append({**item, "evidence_refs": supports, "qualification": "analyst_inference_from_assessed_claims"})
    return result


def _fact_feature_key(event, claims):
    return fingerprint({"event_source_revision": event["source_revision_id"], "source_facts": sorted(
        [[row["version_id"], row["quote"]] for row in claims
         if row["id"] in event["claim_ids"] and row["kind"] == "fact"])})


def _economic_assignment_mismatches(observations, theses):
    """Explain every uncovered event/sector pair without repairing the draft."""
    sectors = {}
    for thesis in theses:
        sector = thesis.get("sector_id")
        if isinstance(sector, str):
            sectors.setdefault(sector, []).append(thesis)
    mismatches = []
    for index, observation in enumerate(observations):
        for sector in observation["sector_ids"]:
            matching = sectors.get(sector, [])
            declared = sorted({event_id for thesis in matching for event_id in thesis["event_ids"]})
            if observation["event_id"] in declared:
                continue
            mismatches.append({"observation_index": index,
                "observation_id": observation.get("observation_id"), "metric_id": observation.get("metric_id"),
                "event_id": observation["event_id"], "sector_id": sector,
                "reason": "event_not_in_sector_theses" if matching else "sector_has_no_thesis",
                "thesis_event_ids": [{"thesis_id": thesis["thesis_id"], "event_ids": list(thesis["event_ids"])}
                                     for thesis in matching],
                "declared_event_ids": declared})
    return mismatches


def assessment_structure_preflight(payload, collection):
    """Read-only draft reference checks, never source or model qualification.

    The collection manifest supplies captured version-to-document identities.
    Officially numbered events use the normal official-number hash. Draft
    observations have no derived observation_id yet, so diagnostics retain
    their original zero-based index and metric_id. No Store, source bytes,
    clock, input mutation or validation attempt is involved. All ordinary
    quote, alias, timing, unit and economic validators still apply afterward.
    """
    issues = []
    result = {"status": "invalid_draft", "scope": "structural_only_not_source_verification",
              "event_ids": [], "issues": issues, "economic_assignment_mismatches": []}
    if not isinstance(payload, dict) or not isinstance(collection, dict):
        issues.append({"path": "payload_or_collection", "reason": "objects_required"})
        return result
    lists = {}
    # A batch/page budget must not become a global research-universe limit.
    # Every supplied row still passes the ordinary structure and source checks.
    for key in ("claims", "events", "industry_theses", "economic_observations"):
        rows = payload.get(key, [] if key == "economic_observations" else None)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            issues.append({"path": key, "reason": "object_list_required"})
            rows = []
        lists[key] = rows

    def references(row, key, path, *, unique=False):
        values = row.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values):
            issues.append({"path": path+"."+key, "reason": "nonempty_text_reference_list_required"})
            return None
        if unique and len(set(values)) != len(values):
            issues.append({"path": path+"."+key, "reason": "distinct_text_references_required"})
            return None
        return values

    versions = collection.get("versions")
    if not isinstance(versions, list) or any(not isinstance(row, dict) for row in versions):
        issues.append({"path": "collection.versions", "reason": "captured_versions_required"})
        versions = []
    by_version = {}
    for index, row in enumerate(versions):
        version, document = row.get("version_id"), row.get("document_id")
        if not isinstance(version, str) or not version or not isinstance(document, str) or not document or version in by_version:
            issues.append({"path": f"collection.versions[{index}]", "reason": "distinct_version_and_document_identity_required"})
        else:
            by_version[version] = document
    by_claim, seen_claims = {}, set()
    for index, row in enumerate(lists["claims"]):
        path = f"claims[{index}]"
        try:
            _claim_structure(row, seen_claims, by_version)
        except ValueError as error:
            field = getattr(error, "draft_field", "")
            issues.append({"path": path+("."+field if field else ""), "reason": "formal_claim_structure", "constraint": str(error)})
        identity, version = row.get("id"), row.get("version_id")
        if not isinstance(identity, str) or not identity or identity in by_claim:
            issues.append({"path": f"claims[{index}].id", "reason": "distinct_claim_identity_required"})
        else:
            by_claim[identity] = row
        if not isinstance(version, str) or version not in by_version:
            issues.append({"path": f"claims[{index}].version_id", "reason": "claim_version_not_in_collection", "version_id": version})
    by_event = {}
    for index, event in enumerate(lists["events"]):
        path = f"events[{index}]"
        version_ids, claim_ids = references(event, "version_ids", path, unique=True), references(event, "claim_ids", path, unique=True)
        if version_ids is None or claim_ids is None:
            continue
        try:
            _event_structure(event, by_version, by_claim)
        except ValueError as error:
            field = getattr(error, "draft_field", "")
            issues.append({"path": path+("."+field if field else ""), "reason": "formal_event_structure", "constraint": str(error)})
            continue
        missing = [value for value in version_ids if value not in by_version]
        if missing:
            issues.append({"path": path, "reason": "event_versions_not_in_collection", "version_ids": missing})
            continue
        documents, key = sorted({by_version[value] for value in version_ids}), event.get("event_key")
        if key is not None and (not isinstance(key, str) or re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,}〔[0-9]{4}〕[0-9]+号", key) is None):
            issues.append({"path": path+".event_key", "reason": "complete_official_number_or_null_required"})
            continue
        if key is None and len(documents) != 1:
            issues.append({"path": path, "reason": "unnumbered_event_requires_one_document", "document_ids": documents})
            continue
        identity = fingerprint({"official_number": key}) if key else documents[0]
        if identity in by_event:
            issues.append({"path": path, "reason": "duplicate_source_event", "event_id": identity})
        else:
            by_event[identity] = event
        for claim_id in claim_ids:
            if claim_id not in by_claim or by_claim[claim_id].get("version_id") not in version_ids:
                issues.append({"path": path+".claim_ids", "reason": "event_claim_version_mismatch", "claim_id": claim_id})
    checked_theses, seen_theses = [], set()
    event_claims = {identity: event["claim_ids"] for identity, event in by_event.items()}
    groups = collection.get("required_source_groups", {})
    if type(groups) is not dict:
        issues.append({"path": "collection.required_source_groups", "reason": "source_group_object_required"})
        groups = {}
    for index, thesis in enumerate(lists["industry_theses"]):
        path = f"industry_theses[{index}]"
        # An unrelated malformed field does not erase the declared event/sector
        # relation and manufacture extra economic-assignment failures.
        if (type(thesis.get("thesis_id")) is str and type(thesis.get("sector_id")) is str
                and type(thesis.get("event_ids")) is list
                and all(type(value) is str for value in thesis["event_ids"])):
            checked_theses.append(thesis)
        try:
            _thesis_structure(thesis, seen_theses, set(by_claim), event_claims, groups)
        except ValueError as error:
            field = getattr(error, "draft_field", "")
            issue = {"path": path+("."+field if field else ""), "reason": "formal_thesis_structure",
                     "constraint": str(error), "thesis_id": thesis.get("thesis_id")}
            if field in ("search_terms", "claim_ids") and type(thesis.get(field)) is list:
                values = thesis[field]
                issue["observed_count"] = len(values)
                if all(type(value) is str for value in values):
                    counts = {}
                    for value in values:
                        counts[value] = counts.get(value, 0)+1
                    issue["duplicates"] = sorted(value for value, count in counts.items() if count > 1)
            issues.append(issue)
            continue
    for index, observation in enumerate(lists["economic_observations"]):
        path = f"economic_observations[{index}]"
        identity, sectors = observation.get("event_id"), references(observation, "sector_ids", path)
        if not isinstance(identity, str):
            issues.append({"path": path+".event_id", "reason": "economic_event_not_declared", "event_id": identity})
            continue
        if identity not in by_event:
            issues.append({"path": path+".event_id", "reason": "economic_event_not_declared", "event_id": identity})
        if sectors is None:
            continue
        if len(set(sectors)) != len(sectors) or any(value.strip() != value or len(value) > 100 for value in sectors):
            issues.append({"path": path+".sector_ids", "reason": "distinct_stable_sector_identities_required"})
            continue
        mismatches = _economic_assignment_mismatches([observation], checked_theses)
        for mismatch in mismatches:
            mismatch["observation_index"] = index
        result["economic_assignment_mismatches"].extend(mismatches)
    result["event_ids"] = list(by_event)
    result["status"] = "passed" if not issues and not result["economic_assignment_mismatches"] else "invalid_draft"
    return result


def _require_economic_assignments(observations, theses):
    mismatches = _economic_assignment_mismatches(observations, theses)
    require(not mismatches,
            "Economic industry assignment requires the same stable sector and source event in assessed theses: "
            + canonical_bytes({"economic_assignment_mismatches": mismatches}).decode("utf-8"))


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
    structure = assessment_structure_preflight(payload, manifest)
    require(structure["status"] == "passed", "News assessment structural preflight failed: " + canonical_bytes(structure).decode("utf-8"))
    import news_economics
    economic = news_economics.assemble(payload.get("economic_observations", []), manifest, claims, events,
                                       artifacts, reviewed_at, store=store, registry=registry)
    _require_economic_assignments(economic, theses)
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
    require(manifest.get("schema_version") == 4 and manifest.get("registry_hash") == registry_fingerprint(registry),
            "Collection schema or registered source identity differs")
    if manifest.get("source_selection_mode") == "automatic_aggregate":
        from news_scope import validate_aggregate
        validate_aggregate(manifest, store, artifacts)
        capacity = manifest["capture_capacity"]
    else:
        capacity = manifest.get("capture_capacity", 400)
        require(type(capacity) is int and 0 < capacity <= len(manifest["selected_source_ids"])*20,
                "Per-shard source capture capacity differs from its finite source domain")
    require(isinstance(manifest.get("retrievals"), list) and 0 < len(manifest["retrievals"]) <= capacity,
            "A source-budget-bounded captured news collection is required")
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
        # Linked PDFs need the verified parent's links, title and source clocks.
        # Retaining every parent's full text would copy the complete news body
        # universe into RAM; each body is verified below and kept in its CAS.
        parsed_captures[identity] = {key: value for key, value in parsed.items() if key != "body"}
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
        doc_id = canonical_article_identity(retrieval["final_url"])
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
