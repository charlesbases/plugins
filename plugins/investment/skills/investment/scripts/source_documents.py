"""Raw-bound HTML and text-PDF evidence with reproducible structural locators.

Extraction establishes what text was obtained from the captured document. It
does not establish the publisher's truth, OCR accuracy or an analyst's reading.
"""
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import source_fetch
from contracts import EvidenceError, canonical_bytes, fields, fingerprint, require

EXTRACTOR_ID = "source-document-4"
HTML_EXTRACTOR_ID = "source-document-html-4.1"
PYPDF_VERSION = "6.19.0"
MAX_TEXT_CHARACTERS = 2_000_000
MAX_BLOCKS = 20_000
MAX_PDF_PAGES = 300
PDF_TIMEOUT_SECONDS = 20


class DocumentNeedsReview(EvidenceError):
    code = "needs_research"

    def __init__(self, reason, actions=None):
        super().__init__(reason)
        self.required_actions = actions or [{"action": "obtain_readable_original_document", "reason": reason}]


def normalize_text(value):
    require(type(value) is str, "Document text must be a string")
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _block(locator, text, kind, **extra):
    text = normalize_text(text)
    return {"locator": locator, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "kind": kind, **extra}


class _HTMLBlocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.links, self.hidden = [], [], 0
        self.table_index, self.row_index = -1, -1
        self.in_table, self.cells, self.cell, self.cell_tags = False, None, None, []
        self.paragraph, self.paragraph_tag, self.paragraph_index = None, None, 0
        self.headers, self.all_text = [], []
        self.table_stack, self.next_table_index, self.cell_spans = [], 0, []
        self.heading_locators = []
        self.heading_levels = {}
        self.free_text, self.free_index, self.containers = [], 0, []
        self.free_context = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag in ("div", "section", "article", "main", "header", "footer", "nav",
                   "table", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"):
            self._finish_free_text()
        if tag in ("div", "section", "article", "main", "header", "footer", "nav"):
            self.containers.append({"tag": tag, "id": attrs.get("id"), "classes": attrs.get("class", "").split()})
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "table":
            self.table_stack.append((self.in_table, self.table_index, self.row_index, self.headers,
                                     self.cells, self.cell, self.cell_tags, self.cell_spans))
            self.in_table, self.table_index, self.row_index, self.headers = True, self.next_table_index, -1, []
            self.next_table_index += 1
            self.cells, self.cell, self.cell_tags, self.cell_spans = None, None, [], []
        elif tag == "tr" and self.in_table:
            require(self.cells is None, "Unclosed document table row")
            self.row_index += 1
            self.cells, self.cell_tags, self.cell_spans = [], [], []
        elif tag in ("td", "th") and self.cells is not None:
            require(self.cell is None, "Unclosed document table cell")
            spans = {}
            for key in ("colspan", "rowspan"):
                value = attrs.get(key, "1")
                require(value.isdecimal() and 1 <= int(value) <= 100, "Unsupported table cell span")
                spans[key] = int(value)
            self.cell_spans.append(spans)
            self.cell, self.cell_tags = [], self.cell_tags + [tag]
        elif tag in ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6") and not self.in_table:
            if self.paragraph is not None:
                self._finish_paragraph()
            self.paragraph, self.paragraph_tag = [], tag
        elif tag == "br":
            if self.cell is not None:
                self.cell.append(" ")
            if self.paragraph is not None:
                self.paragraph.append(" ")
            elif self.free_text:
                self.free_text.append(" ")

    def _finish_free_text(self):
        text = normalize_text("".join(self.free_text))
        if text:
            self.blocks.append(_block(f"html/text/{self.free_index}", text, "container_text",
                                      heading_locators=list(self.heading_locators),
                                      container_path=copy.deepcopy(self.free_context)))
            self.free_index += 1
        self.free_text, self.free_context = [], []

    def _finish_paragraph(self):
        text = normalize_text("".join(self.paragraph or []))
        if text:
            locator = f"html/block/{self.paragraph_index}"
            self.blocks.append(_block(locator, text, self.paragraph_tag or "text",
                                      heading_locators=list(self.heading_locators)))
            if self.paragraph_tag and re.fullmatch("h[1-6]", self.paragraph_tag):
                level = int(self.paragraph_tag[1])
                self.heading_levels = {key: value for key, value in self.heading_levels.items() if key < level}
                self.heading_levels[level] = locator
                self.heading_locators = [self.heading_levels[key] for key in sorted(self.heading_levels)]
            self.paragraph_index += 1
        self.paragraph, self.paragraph_tag = None, None

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden-1)
            return
        if self.hidden:
            return
        if tag in ("div", "section", "article", "main", "header", "footer", "nav",
                   "table", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "body", "html"):
            self._finish_free_text()
        if tag in ("div", "section", "article", "main", "header", "footer", "nav"):
            positions = [index for index, value in enumerate(self.containers) if value["tag"] == tag]
            if positions:
                self.containers = self.containers[:positions[-1]]
        if tag in ("td", "th") and self.cell is not None:
            self.cells.append(normalize_text("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.cells is not None:
            require(self.cell is None, "Unclosed document table cell")
            locator = f"html/table/{self.table_index}/row/{self.row_index}"
            if self.cells:
                if all(value == "th" for value in self.cell_tags):
                    self.headers = list(self.cells)
                self.blocks.append(_block(locator, " | ".join(self.cells), "table_row",
                                          cells=list(self.cells), cell_tags=list(self.cell_tags),
                                          headers=list(self.headers), cell_spans=list(self.cell_spans),
                                          heading_locators=list(self.heading_locators), table=self.table_index, row=self.row_index))
                for index, value in enumerate(self.cells):
                    self.blocks.append(_block(locator+f"/cell/{index}", value, "table_cell",
                                              table=self.table_index, row=self.row_index, column=index))
            self.cells = None
        elif tag == "table":
            require(self.cell is None and self.cells is None, "Unclosed document table")
            require(self.table_stack, "Unmatched document table closure")
            (self.in_table, self.table_index, self.row_index, self.headers,
             self.cells, self.cell, self.cell_tags, self.cell_spans) = self.table_stack.pop()
        elif self.paragraph is not None and tag == self.paragraph_tag:
            self._finish_paragraph()

    def handle_data(self, data):
        if self.hidden:
            return
        self.all_text.append(data)
        if self.cell is not None:
            self.cell.append(data)
        elif self.paragraph is not None:
            self.paragraph.append(data)
        else:
            if not self.free_text:
                self.free_context = copy.deepcopy(self.containers)
            self.free_text.append(data)


def _extract_pdf_in_worker(raw):
    try:
        import pypdf
    except ImportError as exc:
        raise DocumentNeedsReview("The pinned text-PDF extractor is unavailable") from exc
    require(pypdf.__version__ == PYPDF_VERSION, "PDF extraction library differs from the frozen version")
    reader = pypdf.PdfReader(io.BytesIO(raw), strict=True)
    if reader.is_encrypted:
        raise DocumentNeedsReview("Encrypted PDF requires an accessible original disclosure")
    require(0 < len(reader.pages) <= MAX_PDF_PAGES, "PDF page budget exceeded")
    blocks, unreadable, text_size = [], [], 0
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if not text.strip() or "\ufffd" in text:
            unreadable.append(page_number)
            continue
        text_size += len(text)
        require(text_size <= MAX_TEXT_CHARACTERS, "PDF text budget exceeded")
        lines = [normalize_text(line) for line in text.splitlines()]
        blocks.append(_block(f"pdf/page/{page_number}", text, "pdf_page", page=page_number, lines=lines))
        for line_number, line in enumerate(lines, 1):
            if line:
                blocks.append(_block(f"pdf/page/{page_number}/line/{line_number}", line, "pdf_line",
                                     page=page_number, line=line_number))
        require(len(blocks) <= MAX_BLOCKS, "PDF block budget exceeded")
    if not blocks:
        raise DocumentNeedsReview("PDF has no reliably extractable text; image/OCR evidence requires separate review")
    return {"blocks": blocks, "links": [], "unreadable_pages": unreadable,
            "extractor": {"id": EXTRACTOR_ID, "format": "pdf_text", "library_version": pypdf.__version__},
            "extraction_scope": "machine_readable_text_only_no_OCR_accuracy_claim"}


def extract_document(raw, media_type, encoding=None):
    require(type(raw) is bytes and 0 < len(raw) <= source_fetch.MAX_BYTES, "Bounded raw document bytes required")
    if media_type in ("application/pdf", "application/octet-stream"):
        if not raw.startswith(b"%PDF-"):
            raise DocumentNeedsReview("Binary source is not a supported text PDF")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(str(value) for value in sys.path if value)
        try:
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--pdf-worker"],
                input=raw, capture_output=True, timeout=PDF_TIMEOUT_SECONDS, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise DocumentNeedsReview("Text-PDF extraction exceeded its bounded processing time") from exc
        require(len(result.stdout) <= 16*1024*1024, "PDF extraction output exceeded its budget")
        value = json.loads(result.stdout.decode("utf-8")) if result.stdout else {}
        if result.returncode or value.get("status") != "extracted":
            raise DocumentNeedsReview(value.get("reason", "PDF extraction failed; obtain a readable original"))
        return value["extraction"]
    if media_type not in ("text/html", "text/plain") or type(encoding) is not str:
        raise DocumentNeedsReview("Document media needs a registered text extractor")
    text = raw.decode(encoding, errors="strict")
    require(len(text) <= MAX_TEXT_CHARACTERS, "Document text budget exceeded")
    if media_type == "text/html":
        parser = _HTMLBlocks()
        try:
            parser.feed(text)
            parser.close()
            parser._finish_free_text()
            require(not parser.in_table and not parser.table_stack and parser.cell is None and parser.cells is None, "Incomplete HTML table")
            if parser.paragraph is not None:
                parser._finish_paragraph()
        except (ValueError, AssertionError) as exc:
            raise DocumentNeedsReview(str(exc)) from exc
        blocks, links = parser.blocks, sorted(set(parser.links))
        if not blocks and normalize_text(" ".join(parser.all_text)):
            blocks = [_block("html/text/0", " ".join(parser.all_text), "unstructured_text")]
    else:
        blocks = [_block(f"text/line/{number}", line, "text_line", line=number)
                  for number, line in enumerate(text.splitlines(), 1) if normalize_text(line)]
        links = []
    require(blocks and len(blocks) <= MAX_BLOCKS, "Readable bounded document blocks required")
    return {"blocks": blocks, "links": links, "unreadable_pages": [],
            "extractor": {"id": HTML_EXTRACTOR_ID, "format": "html" if media_type == "text/html" else "text", "library_version": "stdlib"},
            "extraction_scope": "source_visible_text_and_explicit_table_structure"}


def capture_document(url, source_id, artifacts, *, timeout=20, max_bytes=source_fetch.MAX_BYTES, store, context=None):
    context = context or store.require_context()
    store.assert_owned(context)
    registry = source_fetch.load_registry()
    capture = source_fetch.fetch(url, source_id, timeout=timeout, max_bytes=max_bytes, registry=registry)
    store.assert_owned(context)
    raw = capture["raw_bytes"]
    try:
        extraction = extract_document(raw, capture["content_type"], capture["encoding"])
        status, actions = "extracted", []
    except DocumentNeedsReview as exc:
        extraction = {"blocks": [], "links": [], "unreadable_pages": [],
                      "extractor": {"id": EXTRACTOR_ID, "format": "unsupported", "library_version": None},
                      "extraction_scope": "not_extracted"}
        status, actions = "needs_review", exc.required_actions
    store.assert_owned(context)
    raw_ref = artifacts.put_bytes(raw)
    metadata = {key: value for key, value in capture.items() if key not in ("raw_bytes", "text")}
    document = {"schema_version": 4, "source_id": source_id, "url": capture["final_url"],
                "capture": metadata, "raw_ref": raw_ref, "raw_sha256": capture["raw_sha256"],
                "media_type": capture["content_type"], "retrieved_at": capture["retrieved_at"],
                **extraction, "status": status, "required_actions": actions,
                "registry_ref": artifacts.put_json(registry)}
    document["text_sha256"] = fingerprint(document["blocks"])
    document["document_id"] = fingerprint({key: value for key, value in document.items() if key not in ("raw_ref", "registry_ref")})
    store.assert_owned(context)
    return artifacts.put_json(document)


def read_extracted_document(document_ref, artifacts, *, registry=None):
    document = artifacts.read_json(document_ref)
    fields(document, {"schema_version", "source_id", "url", "capture", "raw_ref", "raw_sha256", "media_type",
                      "retrieved_at", "blocks", "links", "unreadable_pages", "extractor", "extraction_scope",
                      "status", "required_actions", "text_sha256", "document_id"}, {"registry_ref"}, label="source document")
    require(document["schema_version"] == 4 and document["document_id"] == fingerprint(
        {key: value for key, value in document.items() if key not in ("document_id", "raw_ref", "registry_ref")}), "Source document identity changed")
    if document.get("registry_ref") is not None:
        bound_registry = artifacts.read_json(document["registry_ref"])
        require(fingerprint(bound_registry) == document["capture"]["registry_hash"], "Document source registry snapshot changed")
        if registry is not None:
            require(fingerprint(registry) == fingerprint(bound_registry), "Document and expected source registries differ")
        registry = bound_registry
    source_fetch.validate_capture_provenance(document["capture"], registry)
    require(document["capture"]["registry_source_id"] == document["source_id"]
            and document["capture"]["final_url"] == document["url"]
            and document["capture"]["content_type"] == document["media_type"]
            and document["capture"]["retrieved_at"] == document["retrieved_at"], "Document capture binding changed")
    raw = artifacts.read(document["raw_ref"])
    require(hashlib.sha256(raw).hexdigest() == document["raw_sha256"] == document["capture"]["raw_sha256"],
            "Document raw source changed")
    if document["status"] != "extracted":
        raise DocumentNeedsReview("Original document requires readable-source review", document["required_actions"])
    extraction = extract_document(raw, document["media_type"], document["capture"]["encoding"])
    require(all(document[key] == value for key, value in extraction.items())
            and fingerprint(extraction["blocks"]) == document["text_sha256"], "Document extraction differs from its raw source")
    return document


def archive_document(document_ref, source_artifacts, destination_artifacts):
    """Relocate verified raw bytes without changing their semantic identity."""
    try:
        document = copy.deepcopy(read_extracted_document(document_ref, source_artifacts))
    except DocumentNeedsReview:
        # Rejected extraction is evidence too. The read above checked the
        # capture/hash boundary before reporting its preserved review gap.
        document = copy.deepcopy(source_artifacts.read_json(document_ref))
    document["raw_ref"] = destination_artifacts.put_bytes(source_artifacts.read(document["raw_ref"]))
    if document.get("registry_ref") is not None:
        document["registry_ref"] = destination_artifacts.put_json(source_artifacts.read_json(document["registry_ref"]))
    reference = destination_artifacts.put_json(document)
    try:
        read_extracted_document(reference, destination_artifacts)
    except DocumentNeedsReview:
        pass
    return reference


def bind_registry_snapshot(document_ref, registry, artifacts):
    """Attach proven original provenance; never refresh quote time or body."""
    document = copy.deepcopy(read_extracted_document(document_ref, artifacts, registry=registry))
    require(fingerprint(registry) == document["capture"]["registry_hash"], "Original source registry cannot be substituted")
    document["registry_ref"] = artifacts.put_json(registry)
    reference = artifacts.put_json(document)
    read_extracted_document(reference, artifacts)
    return reference


def resolve_span(document_ref, locator, artifacts):
    require(type(locator) is str and locator, "Explicit document locator required")
    document = read_extracted_document(document_ref, artifacts)
    selected = [block for block in document["blocks"] if block["locator"] == locator]
    require(len(selected) == 1, "Document locator is missing or ambiguous")
    context_locators = selected[0].get("heading_locators", [])
    return {**copy.deepcopy(selected[0]), "context_blocks": [copy.deepcopy(block) for block in document["blocks"]
                                                            if block["locator"] in context_locators],
            "document_id": document["document_id"], "source_id": document["source_id"],
            "url": document["url"], "raw_sha256": document["raw_sha256"], "retrieved_at": document["retrieved_at"]}


if __name__ == "__main__":
    if sys.argv[1:] != ["--pdf-worker"]:
        raise SystemExit("Private bounded PDF worker only")
    try:
        data = sys.stdin.buffer.read(source_fetch.MAX_BYTES+1)
        require(0 < len(data) <= source_fetch.MAX_BYTES, "PDF source byte budget exceeded")
        output = {"status": "extracted", "extraction": _extract_pdf_in_worker(data)}
        exit_code = 0
    except Exception as error:
        output, exit_code = {"status": "needs_review", "reason": str(error)}, 1
    sys.stdout.buffer.write(canonical_bytes(output))
    raise SystemExit(exit_code)
