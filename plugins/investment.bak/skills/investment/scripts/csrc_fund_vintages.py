"""Reconstruct CSRC's declared filing versions from the public daily JSON.

The daily endpoint associates each numeric share row with its parent fund,
uploadInfoDetail, uploadDatetime and reportSendDate in the same object. This
adapter preserves that association. It does not assert database immutability,
recover missing superseded prices, authenticate an HTTP connection, establish
CNY/share-class identity, reconstruct distributions, or qualify a strategy.

reportSendDate has day precision. A provider-declared version can therefore be
considered for a historical origin only on a later Shanghai calendar day, and
only after the caller separately authenticates the source/version contract.
retrieved_at always remains the actual capture time from the receipt.
"""

import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import urllib.parse


PROTOCOL = "csrc_daily_provider_declared_version_v1"
VERSION_SCOPE = "provider-declared-version_opaqueDocumentScope"
SOURCE_HOST = "eid.csrc.gov.cn"
SOURCE_PATH = "/fund/disclose/daily_report_list_page.do"
SHANGHAI = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
MAX_BYTES = 16 * 1024 * 1024
_SOURCE_FIELDS = ("code", "idStr", "classification", "fund", "shareNetValue",
                  "totalNetValue", "valuationDate", "uploadInfoDetail",
                  "uploadDatetime", "reportSendDate")


class VintageError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise VintageError(message)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "Duplicate JSON member: " + key)
        value[key] = item
    return value


def _canonical(value):
    """Keep JSON types distinct (False != 0 and an entity integer != float)."""
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise VintageError("Adapter value is not finite canonical JSON") from error


def _day(value, field):
    _require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value),
             "Missing or invalid date: " + field)
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise VintageError("Invalid calendar date: " + field) from error


def _aware(value, field):
    _require(isinstance(value, str), "Missing aware timestamp: " + field)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VintageError("Invalid timestamp: " + field) from error
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
             "Timestamp needs an explicit offset: " + field)
    return parsed


def _identifier(value, field):
    _require(type(value) is int and value > 0, "Invalid source entity identifier: " + field)
    return value


def _code(value, field):
    _require(isinstance(value, str) and re.fullmatch(r"\d{6}", value),
             "Invalid source fund/share code: " + field)
    return value


def _nav(value, field):
    # The public recipe returns decimal strings, not binary floating values.
    _require(isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value),
             "Missing or invalid decimal price: " + field)
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise VintageError("Invalid decimal price: " + field) from error
    _require(number.is_finite() and number > 0, "Nonpositive source price: " + field)
    return value


def _official_url(value):
    _require(isinstance(value, str) and len(value) <= 8192, "Invalid source URL")
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except ValueError as error:
        raise VintageError("Invalid source URL") from error
    _require(url.scheme in ("http", "https") and url.hostname == SOURCE_HOST
             and url.path == SOURCE_PATH and not url.username and not url.password
             and not url.fragment and port in (None, 80 if url.scheme == "http" else 443),
             "Daily source URL is outside the reviewed official recipe")
    return url


def _receipt(raw, capture):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "Empty or oversized raw JSON")
    _require(isinstance(capture, dict), "Missing actual capture receipt")
    requested = _official_url(capture.get("requested_url"))
    final = _official_url(capture.get("final_url"))
    _require(capture.get("requested_url") == capture.get("final_url")
             and capture.get("redirect_chain", []) == [], "Unreviewed source redirect")
    _require(type(capture.get("http_status")) is int and capture["http_status"] == 200,
             "Unsuccessful source HTTP status")
    _require(type(capture.get("bytes")) is int and capture["bytes"] == len(raw)
             and capture.get("raw_sha256") == hashlib.sha256(raw).hexdigest(),
             "Raw JSON differs from its capture receipt")
    captured = _aware(capture.get("retrieved_at"), "retrieved_at")
    encoding = capture.get("encoding")
    _require(encoding in ("utf-8", "utf-8-sig"), "Reviewed daily JSON needs UTF-8 encoding")
    transport = capture.get("transport", "unrecorded")
    details = capture.get("transport_details", {})
    _require(isinstance(transport, str) and isinstance(details, dict), "Invalid transport receipt")
    if requested.scheme == "http" or final.scheme == "http":
        _require(not transport.startswith("HTTPS") and capture.get("tls_verified") is not True
                 and details.get("tls_verified") is not True
                 and details.get("certificate_verification") not in ("required", "verified"),
                 "HTTP receipt must not claim TLS verification")
        verification = "not_tls"
    else:
        # A caller's receipt is preserved, not treated as an independently
        # authenticated channel merely because the URL begins with https.
        verification = "TLS_receipt_requires_separate_validation"
    _require(isinstance(capture.get("content_type"), str) and capture["content_type"],
             "Missing source content-type receipt")
    return captured, {"requested_url": capture["requested_url"], "final_url": capture["final_url"],
                      "redirect_chain": [], "http_status": 200,
                      "content_type": capture["content_type"], "encoding": encoding,
                      "transport": transport, "transport_details": details,
                      "transport_verification": verification,
                      "actual_capture_at": capture["retrieved_at"],
                      "raw_sha256": capture["raw_sha256"], "bytes": len(raw)}


def _page(raw, encoding):
    try:
        value = json.loads(raw.decode(encoding, errors="strict"), object_pairs_hook=_unique_object,
                           parse_constant=lambda x: (_ for _ in ()).throw(VintageError("Nonfinite JSON number")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise VintageError("Invalid raw daily JSON") from error
    _require(isinstance(value, dict) and isinstance(value.get("aaData"), list), "Missing daily source rows")
    for field in ("iTotalRecords", "iTotalDisplayRecords"):
        _require(type(value.get(field)) is int and value[field] >= len(value["aaData"]),
                 "Invalid source page count: " + field)
    _require(value["iTotalDisplayRecords"] <= value["iTotalRecords"], "Source filtered count exceeds total")
    return value


def _row(row, captured):
    _require(isinstance(row, dict), "Invalid daily source row")
    price = row.get("shareNetValue")
    cumulative = row.get("totalNetValue")
    if price in (None, "") and cumulative in (None, ""):
        return None  # Blank parent/summary row is an inventory item, never zero NAV.
    _nav(price, "shareNetValue")
    _nav(cumulative, "totalNetValue")
    share_code = _code(row.get("code"), "code")
    row_id = _identifier(row.get("idStr"), "idStr")
    fund, detail, classification = row.get("fund"), row.get("uploadInfoDetail"), row.get("classification")
    _require(isinstance(fund, dict) and isinstance(detail, dict) and isinstance(classification, dict),
             "Numeric price has no same-object fund/class/version association")
    fund_code = _code(fund.get("code"), "fund.code")
    fund_id = _identifier(fund.get("idStr"), "fund.idStr")
    detail_id = _identifier(detail.get("idStr"), "uploadInfoDetail.idStr")
    class_code = classification.get("code")
    _require(isinstance(class_code, str) and re.fullmatch(r"2030-\d{4}", class_code),
             "Missing source share classification code")
    operation = fund.get("operationType", {})
    _require(isinstance(operation, dict), "Invalid source fund operation classification")
    _require(operation.get("code") != "6030-1030" or class_code in
             ("2030-1030", "2030-1020", "2030-1010"),
             "Structured subshare uses a reference-price recipe, not this direct NAV recipe")
    valuation = _day(row.get("valuationDate"), "valuationDate")
    publication = _day(row.get("reportSendDate"), "reportSendDate")
    uploaded_literal = row.get("uploadDatetime")
    _require(isinstance(uploaded_literal, str)
             and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", uploaded_literal),
             "Missing source version upload timestamp")
    try:
        uploaded = dt.datetime.strptime(uploaded_literal, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
    except ValueError as error:
        raise VintageError("Invalid source upload timestamp") from error
    _require(publication >= dt.date(2009, 1, 1), "Publication is outside the reviewed modern source clock")
    _require(valuation <= uploaded.date() <= publication,
             "NAV/version/publication dates are inverted or their association is unknown")
    _require(uploaded <= captured and publication <= captured.astimezone(SHANGHAI).date(),
             "Source declares a version/publication after actual capture")
    currency = fund.get("raiseCurrency")
    _require(currency is None or isinstance(currency, str), "Invalid raw currency identity")
    source_row = {field: row[field] for field in _SOURCE_FIELDS if field in row}
    return {"fund_share_code": share_code, "source_share_classification_code": class_code,
            "parent_fund_code": fund_code, "parent_fund_entity_id": fund_id,
            "price_row_entity_id": row_id, "filing_detail_entity_id": detail_id,
            "valuation_date": row["valuationDate"], "unit_nav": price, "cumulative_nav": cumulative,
            "source_uploaded_at_literal": uploaded_literal,
            "source_uploaded_at": uploaded.isoformat(), "source_upload_precision": "second",
            "source_publication_date": row["reportSendDate"], "source_publication_precision": "day",
            "source_clock_contract": "Asia/Shanghai",
            "earliest_candidate_origin_day": (publication + dt.timedelta(days=1)).isoformat(),
            "source_raise_currency_literal": currency,
            "currency_identity_qualified": False, "share_class_identity_qualified": False,
            "version_scope": VERSION_SCOPE, "source_row": source_row}


def _identity(record):
    return (record["price_row_entity_id"], record["filing_detail_entity_id"],
            record["source_publication_date"])


def _economic_identity(record):
    return (record["fund_share_code"], record["parent_fund_code"], record["parent_fund_entity_id"],
            record["source_share_classification_code"], record["valuation_date"],
            record["source_uploaded_at_literal"], record["source_raise_currency_literal"],
            Decimal(record["unit_nav"]), Decimal(record["cumulative_nav"]))


class VersionAssociations:
    """Incremental original-entity conflict checks across a full source panel."""

    def __init__(self):
        self.versions, self.details, self.share_dates = {}, {}, {}

    def add(self, record):
        versions, details, share_dates = self.versions, self.details, self.share_dates
        identity, economic = _identity(record), _economic_identity(record)
        _require(identity not in versions or versions[identity] == economic,
                 "Conflicting price/identity for the same version entity and publication date")
        versions[identity] = economic
        detail = record["filing_detail_entity_id"]
        association = (record["parent_fund_entity_id"], record["parent_fund_code"],
                       record["valuation_date"], record["source_uploaded_at_literal"],
                       record["source_publication_date"], record["source_raise_currency_literal"])
        _require(detail not in details or details[detail] == association,
                 "Filing detail is linked to inconsistent parent/version metadata")
        details[detail] = association
        share_date = (detail, record["fund_share_code"], record["source_publication_date"])
        _require(share_date not in share_dates or share_dates[share_date] == economic,
                 "Conflicting numeric rows for one share within one declared filing version")
        share_dates[share_date] = economic

    def update(self, records):
        for record in records:
            self.add(record)


def _check_associations(records):
    VersionAssociations().update(records)


def _requested(codes, source_timezone_contract):
    _require(source_timezone_contract == "Asia/Shanghai", "Explicit reviewed source clock contract required")
    _require(isinstance(codes, (list, tuple)) and len(codes) > 0, "Explicit requested share-code inventory required")
    checked = [_code(code, "requested_codes") for code in codes]
    _require(len(set(checked)) == len(checked), "Duplicate requested share code")
    return sorted(checked)


def extract_vintages(raw_bytes, capture_metadata, *, requested_codes, source_timezone_contract):
    """Produce candidates without network, Store writes, or financial qualification."""
    codes = _requested(requested_codes, source_timezone_contract)
    captured, receipt = _receipt(raw_bytes, capture_metadata)
    page = _page(raw_bytes, receipt["encoding"])
    records, observed, blank, outside = [], [], [], []
    for index, row in enumerate(page["aaData"]):
        record = _row(row, captured)
        if record is None:
            blank.append(index)
        else:
            record["source_row_index"] = index
            observed.append(record)
            if record["fund_share_code"] not in codes:
                outside.append(index)
            else:
                records.append(record)
    # Check every numeric row, including classes outside the caller's inventory;
    # filtering must not hide a contradictory version association.
    _check_associations(observed)
    return {"schema_version": 1, "protocol": PROTOCOL, "version_scope": VERSION_SCOPE,
            "source_clock_contract": source_timezone_contract, "requested_codes": codes,
            "capture": receipt, "records": records, "observed_versions": observed,
            "page": {"total_records": page["iTotalRecords"], "filtered_records": page["iTotalDisplayRecords"],
                     "returned_rows": len(page["aaData"]), "blank_price_row_indexes": blank,
                     "outside_requested_share_row_indexes": outside,
                     "pagination_complete": page["iTotalDisplayRecords"] == len(page["aaData"])},
            "source_original_signature_status": "not_obtained",
            "source_authentication_checked": False, "strict_historical_eligibility_granted": False,
            "financial_eligibility_granted": False,
            "qualification_not_checked": ["source_channel_authentication", "provider_version_contract_admission",
                                          "currency_and_share_class_identity", "distributions_and_corporate_actions",
                                          "tt_transactions_fees_and_status", "complete_historical_paths",
                                          "model_training_and_independent_calibration"]}


def validate_vintages(value, raw_bytes, capture_metadata, *, requested_codes, source_timezone_contract,
                      previous_versions=()):
    """Validate against raw fields, receipt and previously seen version identities.

    The validator starts with original bytes, not the producer's claims. The
    comparison also detects modifications to values, source association, day
    precision, actual capture or unsupported qualification flags.
    """
    _require(isinstance(value, dict), "Missing candidate archive")
    _require(set(value) == {"schema_version", "protocol", "version_scope", "source_clock_contract",
             "requested_codes", "capture", "records", "observed_versions", "page",
             "source_original_signature_status", "source_authentication_checked",
             "strict_historical_eligibility_granted", "financial_eligibility_granted",
             "qualification_not_checked"}, "Unexpected or missing adapter archive fields")
    codes = _requested(requested_codes, source_timezone_contract)
    captured, receipt = _receipt(raw_bytes, capture_metadata)
    page = _page(raw_bytes, receipt["encoding"])
    records = value.get("records")
    _require(isinstance(records, list), "Invalid candidate inventory")
    expected_indices, blanks, outside = [], [], []
    source_numeric_records = []
    for index, row in enumerate(page["aaData"]):
        source_record = _row(row, captured)
        if source_record is None:
            blanks.append(index)
            continue
        source_record["source_row_index"] = index
        source_numeric_records.append(source_record)
        if source_record["fund_share_code"] not in codes:
            outside.append(index)
            continue
        expected_indices.append(index)
    _require(len(records) == len(expected_indices), "Candidate count differs from raw requested numeric rows")
    _require(_canonical(value.get("observed_versions")) == _canonical(source_numeric_records),
             "Observed version inventory differs from all raw numeric rows")
    for candidate, index in zip(records, expected_indices):
        source_record = _row(page["aaData"][index], captured)
        source_record["source_row_index"] = index
        _require(_canonical(candidate) == _canonical(source_record),
                 "Candidate does not reconstruct its original price/version row")
    _check_associations(source_numeric_records)
    _require(isinstance(previous_versions, (list, tuple)), "Invalid previous-version inventory")
    for previous in previous_versions:
        _require(isinstance(previous, dict), "Invalid previous version")
        # Previous archives must themselves have passed raw reconstruction when
        # stored. This comparison is an extra conflict check, not that proof.
        try:
            _identity(previous), _economic_identity(previous)
        except (KeyError, InvalidOperation) as error:
            raise VintageError("Incomplete previous version identity") from error
    _check_associations([*previous_versions, *source_numeric_records])
    expected_page = {"total_records": page["iTotalRecords"], "filtered_records": page["iTotalDisplayRecords"],
                     "returned_rows": len(page["aaData"]), "blank_price_row_indexes": blanks,
                     "outside_requested_share_row_indexes": outside,
                     "pagination_complete": page["iTotalDisplayRecords"] == len(page["aaData"])}
    _require(_canonical(value.get("page")) == _canonical(expected_page)
             and _canonical(value.get("capture")) == _canonical(receipt),
             "Page inventory or actual capture receipt changed")
    _require(type(value.get("schema_version")) is int and value["schema_version"] == 1
             and value.get("protocol") == PROTOCOL
             and value.get("version_scope") == VERSION_SCOPE
             and value.get("source_clock_contract") == source_timezone_contract
             and value.get("requested_codes") == codes,
             "Candidate source contract identity changed")
    for field in ("source_authentication_checked", "strict_historical_eligibility_granted",
                  "financial_eligibility_granted"):
        _require(value.get(field) is False, "Adapter may not grant qualification: " + field)
    _require(value.get("source_original_signature_status") == "not_obtained",
             "Adapter has not obtained or verified an original filing signature")
    _require(value.get("qualification_not_checked") == ["source_channel_authentication",
             "provider_version_contract_admission", "currency_and_share_class_identity",
             "distributions_and_corporate_actions", "tt_transactions_fees_and_status",
             "complete_historical_paths", "model_training_and_independent_calibration"],
             "Missing remaining qualification inventory")
    return True


def version_day_is_before_origin(record, origin_at):
    """Day-precision availability bound only; source authentication is separate."""
    _require(isinstance(record, dict) and record.get("source_clock_contract") == "Asia/Shanghai"
             and record.get("source_publication_precision") == "day"
             and record.get("version_scope") == VERSION_SCOPE, "Unknown version availability contract")
    publication = _day(record.get("source_publication_date"), "source_publication_date")
    return publication < _aware(origin_at, "origin_at").astimezone(SHANGHAI).date()
