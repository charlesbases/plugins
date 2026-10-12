"""Source-derived unit NAV checkpoints; unsupported document grammar stays explicit."""

import datetime as dt
import re
from decimal import Decimal

import fund_universe
import source_documents
from contracts import fingerprint, require


ADAPTER = "issuer_report_nav_note_v1"


def _compact(value):
    return re.sub(r"\s+", "", value)


def parse_report(document, identity):
    """Recognize a dated NAV note under one labelled accounting subject.

    Class-to-code mapping must be explicit in this report as well as agree with
    the independently reconstructed identity. Other formats are not guessed.
    """
    blocks = [b for b in document["blocks"] if b["kind"] != "pdf_page"]
    lines = [(b["locator"], line.strip()) for b in blocks
             for line in b["text"].splitlines() if line.strip()]
    text = "\n".join(line for _, line in lines)
    subjects = [(loc, _compact(line.split(":", 1)[1])) for loc, raw in lines
                if (line := raw.replace("：", ":")).startswith("会计主体:")]
    require(subjects and {name for _, name in subjects} == {_compact(identity["legal_name"])},
            "Accounting subject is missing, multiple, or differs from legal fund")
    tables = list(re.finditer(r"下属分级基金的基金简称(.+?)下属分级基金的交易代码\s*((?:[0-9]{6}\s*)+)", text, re.S))
    require(len(tables) == 1, "One unambiguous share-class/code table required")
    table = tables[0]
    # This adapter supports unqualified class names ending their text row.
    # Parenthetical currencies or any unparsed suffix require a richer adapter;
    # accounting CNY must never silently override a dealing-currency qualifier.
    require(not re.search(r"[()（）]", table[1]), "Qualified share-class names require explicit currency review")
    class_rows = list(re.finditer(r"(?<![A-Z])([A-Z])(?:类)?[ \t]*(?=\n|$)", table[1]))
    classes = [row[1] for row in class_rows]
    codes = re.findall(r"[0-9]{6}", table[2])
    require(len(classes) == len(codes) and len(set(classes)) == len(classes)
            and len(set(codes)) == len(codes), "Duplicate class/currency or incomplete class-code mapping")
    mapping = dict(zip(codes, classes))
    require(mapping.get(identity["code"]) == identity["share_class"], "Unit NAV share identity differs")
    require(identity["currency"] == "CNY", "Only source-confirmed CNY NAV notes are supported")
    require(class_rows and not table[1][class_rows[-1].end():].strip(),
            "Unparsed share-class suffix requires currency review")
    offsets, cursor = [], 0
    for locator, line in lines:
        offsets.append((cursor, cursor + len(line), locator))
        cursor += len(line) + 1

    def locators(start, end):
        return [loc for left, right, loc in offsets if left < end and right > start]

    result = []
    date_pattern = r"报告截止日\s*[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*[,，]"
    value_pattern = r"\s*([A-Z])\s*类基金份额(?:单位)?净值\s*([0-9]+\.[0-9]+)\s*(人民币元|元)"
    for match in re.finditer(date_pattern, text):
        when = dt.date(*map(int, match.groups())).isoformat()
        # The accounting currency must be explicitly stated after the nearest
        # accounting subject and before this note; a bare '元' is insufficient.
        preceding = text[:match.start()]
        section = preceding[preceding.rfind("会计主体"):]
        units = re.findall(r"单位\s*[:：]\s*([^\n]+)", section)
        require(units and _compact(units[-1]) == "人民币元", "NAV accounting currency unresolved")
        position, values = match.end(), []
        while value := re.match(value_pattern, text[position:]):
            values.append((value[1], value[2], position, position + value.end()))
            position += value.end()
            separator = re.match(r"\s*[,，]\s*", text[position:])
            if not separator:
                break
            position += separator.end()
        if not values:
            continue
        require(len({v[0] for v in values}) == len(values)
                and set(v[0] for v in values) <= set(classes), "Duplicate or unknown class in NAV note")
        # A partial parse must not silently discard a currency-qualified or
        # otherwise unrecognised additional NAV entry in the same sentence.
        tail = re.split(r"[;；。]", text[position:], maxsplit=1)[0]
        require("净值" not in tail, "Partially parsed NAV sentence is ambiguous")
        for share, literal, start, end in values:
            if share == identity["share_class"]:
                require(Decimal(literal) > 0, "Unit NAV must be positive")
                result.append({"code": identity["code"], "date": when, "nav": literal,
                               "decimal_places": len(literal.split(".")[1]), "currency": "CNY",
                               "nav_kind": "unit_nav", "adapter": ADAPTER,
                               "subject_locators": sorted({loc for loc, _ in subjects}),
                               "identity_locators": locators(table.start(), table.end()),
                               "date_locators": locators(match.start(), match.end()),
                               "value_locators": locators(start, end)})
    require(result, "No supported dated unit NAV note for this share")
    require(len({(r["date"], r["nav"]) for r in result}) == len({r["date"] for r in result}),
            "Conflicting unit NAV notes for the same share/date")
    return result


def inventory(snapshot, artifacts, codes, *, transport_registry=None):
    """Rebuild identity and raw documents before interpreting any NAV number."""
    identities = fund_universe.verify_identity(snapshot, artifacts, registry=transport_registry)
    anchors, gaps, sources, records, seen, documents = [], [], {}, {}, set(), {}
    for item in snapshot["issuer_documents"]:
        code, reference = item["mapping"]["code"], item["document_ref"]
        if code not in codes:
            continue
        key = fingerprint(reference)
        if (code, key) in seen:
            continue
        seen.add((code, key))
        if key not in documents:
            documents[key] = source_documents.read_extracted_document(reference, artifacts, registry=transport_registry)
        document = documents[key]
        sid = "issuer-NAV-" + document["document_id"]
        raw = document["raw_ref"]
        records[sid] = {**document["capture"], "source_id": sid, "path": raw["path"],
                        "sha256": document["raw_sha256"], "expected_sha256": document["raw_sha256"],
                        "review_status": "matched_reviewed_digest"}
        sources[sid] = {"url": document["capture"]["requested_url"], "expected_sha256": document["raw_sha256"],
                        "kind": document["media_type"], "scope": "source_disclosed_unit_NAV_points"}
        try:
            parsed = parse_report(document, identities[code])
        except ValueError as error:
            gaps.append({"code": code, "source_id": sid, "document_ref": reference,
                         "reason": str(error), "status": "unsupported_or_ambiguous_format"})
        else:
            anchors.extend({**row, "source_id": sid, "document_ref": reference,
                            "identity_mapping": item["mapping"]} for row in parsed)
    for code in codes:
        if not any(item[0] == code for item in seen):
            gaps.append({"code": code, "status": "issuer_document_not_collected"})
    return {"anchors": sorted(anchors, key=lambda r: (r["code"], r["date"], r["source_id"])),
            "gaps": gaps, "sources": sources, "source_records": list(records.values())}


def rounding_tolerance(anchor):
    """Half the last published decimal unit, derived solely from the literal."""
    literal = anchor["nav"]
    require(isinstance(literal, str) and re.fullmatch(r"[0-9]+\.[0-9]+", literal), "NAV literal precision required")
    places = len(literal.split(".")[1])
    require(anchor["decimal_places"] == places, "NAV precision differs from source literal")
    return Decimal(5).scaleb(-places - 1)
