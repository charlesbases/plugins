"""Current public fund discovery, with identities reproduced from captured sources.

The distributor directory is a search index. Legal names and manager identifiers
come from each fund's disclosure page; their natural key describes the current
distributor identity, not a regulator-issued parent code or historical identity.
"""
import datetime as dt
import copy
import hashlib
import re
import time
import urllib.parse
import unicodedata
from html import unescape
from decimal import Decimal
from html.parser import HTMLParser

import source_fetch
import source_documents
import fund_screen
import manager_evidence
from contracts import EvidenceError, fields, fingerprint, strict_json_loads, utc_now

CATALOG_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
DEFAULT_POLICY = {"max_profile_requests": 50,
                  "max_total_bytes": 64 * 1024 * 1024, "timeout_seconds": 20,
                  "max_elapsed_seconds": 180, "max_age_seconds": 86400, "max_source_requests": 256}

CURRENCIES = {"人民币": "CNY", "人民币元": "CNY", "CNY": "CNY", "美元": "USD", "USD": "USD", "港币": "HKD", "HKD": "HKD", "欧元": "EUR", "EUR": "EUR"}


class DeferredFeeIdentity(EvidenceError):
    identity_status = "unsupported_deferred_fee_identity"

    def __init__(self, code, roles):
        super().__init__("The source explicitly labels " + code +
                         " as a back-end/deferred-fee dealing code; the current execution model requires a supported quote mapping")
        self.identity_details = {"requested_code": code, "billing_role": "deferred", "labelled_code_roles": roles}


class IssuerIdentityMismatch(EvidenceError):
    pass


def _identity_require(condition, message):
    if not condition:
        raise IssuerIdentityMismatch(message)


def _profile_failure(error):
    return {"kind": getattr(error, "identity_status", "profile_parse_rejected"),
            "exception": type(error).__name__, "message": str(error),
            "details": getattr(error, "identity_details", {})}


def _currency(value):
    return CURRENCIES.get(_text(value)) if isinstance(value, str) else None


def _roles(text):
    """Extract labelled relationships, never unordered word membership."""
    value = _text(text)
    parents = set(re.findall(r"(?:基金主代码|主基金代码|基金主编码|主代码)\s*[:：|]?\s*([0-9]{6})(?![0-9])", value))
    pairs = re.findall(r"(?<![A-Z])([A-Z])\s*类(?:基金份额)?(?:基金)?(?:代码|编码)?\s*[:：|]?\s*([0-9]{6})(?![0-9])", value)
    by_code = {}
    for share, code in pairs:
        _identity_require(code not in by_code or by_code[code] == share, "Conflicting explicitly labelled share class")
        by_code[code] = share
    _identity_require(len(parents) <= 1, "Ambiguous explicitly labelled parent code")
    return (next(iter(parents)) if parents else None), by_code


def _document_blocks(document, locators):
    require(type(locators) is list and locators and len(set(locators)) == len(locators), "Distinct original-document locators required")
    indexed = {row["locator"]: row for row in document["blocks"]}
    require(set(locators) <= set(indexed), "Disclosure locator is absent from the original document")
    return [indexed[value] for value in locators]


def _label_values(blocks, labels):
    values = set()
    for block in blocks:
        cells, headers = block.get("cells", []), block.get("headers", [])
        for index in range(0, len(cells)-1, 2):
            if _text(cells[index]).rstrip(":：") in labels:
                values.add(_text(cells[index+1]))
        if len(cells) == len(headers):
            values.update(_text(value) for header, value in zip(headers, cells) if _text(header).rstrip(":：") in labels)
        lines = block.get("lines", [block["text"]])
        for index, line in enumerate(lines):
            for label in labels:
                match = re.fullmatch(re.escape(label)+r"\s*[:：]\s*(.+)", _text(line))
                if match:
                    values.add(match[1])
                elif _text(line) == label and index+1 < len(lines):
                    values.add(_text(lines[index+1]))
    return {value for value in values if value and value not in ("-", "--", "暂无", "暂无数据")}


def _one(values, name):
    _identity_require(len(values) <= 1, "Conflicting source " + name)
    return next(iter(values)) if values else None



def _issuer_standard_fields(blocks, code, current):
    """Published overview/basic-information table adapters, preserving subject."""
    text = "\n".join(block["text"] for block in blocks)
    compact = re.sub(r"\s+", "", text)
    result = {}
    # A product overview labels the general fund and the particular share class
    # separately. The class is read from the subordinate name/code relation.
    overview = re.search(r"下属基金简称(.+?)下属基金代码([0-9]{6})", compact)
    if overview:
        _identity_require(overview[2] == code, "Product overview belongs to another share code")
        share = re.search(r"([A-Z])(?:类)?$", overview[1])
        require(share is not None, "Overview subordinate share class is unresolved")
        result["share_class"] = share[1]
        parent = re.search(r"(?<!下属)基金代码([0-9]{6})", compact)
        if parent:
            result["disclosed_parent_code"] = parent[1]
        currency = re.search(r"交易币种(人民币|美元|港币|欧元|CNY|USD|HKD|EUR)(?=运作方式|开放频率|基金经理|$)", compact)
        if currency:
            result["currency"] = result["dealing_currency"] = _currency(currency[1])
        if "运作方式普通开放式" in compact:
            result["execution_venue"] = result["instrument_type"] = "off_exchange_nav"
        disclosed = re.search(r"编制日期[:：](\d{4})年(\d{1,2})月(\d{1,2})日", compact)
        if disclosed:
            result["disclosed_as_of"] = dt.date(*map(int, disclosed.groups())).isoformat()
        result["identity_adapter"] = "issuer_product_overview_label_pairs_v1"
    member = re.search(r"下属分级基金的基金简称(.+?)下属分级基金的交易代码\s*((?:[0-9]{6}\s*)+)", text, re.S)
    if member:
        classes = re.findall(r"(?<![A-Z])([A-Z])(?:类)?(?=\s|$)", member[1])
        codes = re.findall(r"[0-9]{6}", member[2])
        require(len(classes) == len(codes) and len(set(codes)) == len(codes)
                and len(set(classes)) == len(classes) and code in codes,
                "Disclosure class-name/code table is incomplete or ambiguous")
        _identity_require(re.sub(r"\s+", "", current["legal_name"]) in compact, "Membership table belongs to a different legal fund")
        result["share_class"] = dict(zip(codes, classes))[code]
        result["declared_members"] = sorted(codes)
        result["identity_adapter"] = "issuer_periodic_report_class_table_v1"
    return result


def _identity_from_document(document, mapping, current):
    """Reproducible issuer fields with their locators and explicit unknowns."""
    require(set(mapping) == {"code", "source_id", "url", "role", "locators"}, "Current issuer disclosure contract required")
    require(mapping["role"] in ("identity", "holdings", "manager", "company_restriction"), "Unsupported issuer disclosure role")
    blocks = _document_blocks(document, mapping["locators"])
    if mapping["role"] == "manager":
        scoped, subject = manager_evidence.subject_blocks(document, current)
        _identity_require(subject["status"] != "legal_subject_conflict", "Issuer and distributor legal subject conflict")
        if subject["status"] == "unique_primary_product":
            blocks = scoped
    text = "\n".join(row["text"] for row in blocks)
    standard = _issuer_standard_fields(blocks, mapping["code"], current)
    parent, shares = _roles(text)
    for block in blocks:
        headers, cells = block.get("headers", []), block.get("cells", [])
        if len(headers) == len(cells) and "基金代码" in headers and "份额类别" in headers:
            code_value, class_value = cells[headers.index("基金代码")], cells[headers.index("份额类别")]
            if re.fullmatch(r"[0-9]{6}", code_value) and re.fullmatch(r"[A-Z]类?", class_value):
                require(code_value not in shares or shares[code_value] == class_value[0], "Conflicting source class row")
                shares[code_value] = class_value[0]
    labelled_codes = set(re.findall(r"(?:基金代码|基金编码)\s*[:：|]?\s*([0-9]{6})(?![0-9])", text))
    for value in _label_values(blocks, {"基金代码", "基金编码"}):
        labelled_codes.update(re.findall(r"(?<![0-9])[0-9]{6}(?![0-9])", value))
    _identity_require(mapping["code"] in labelled_codes or standard.get("share_class"), "Issuer fields require the primary product code, not a sibling mentioned in a relation")
    result = copy.deepcopy(current)
    result.update({key: value for key, value in standard.items() if key != "declared_members"})
    refs = [{"document_id": document["document_id"], "document_ref": mapping.get("document_ref"),
             "locator": block["locator"], "raw_sha256": document["raw_sha256"]} for block in blocks]
    # document_ref is attached by the resolver; values here come only from text.
    refs = [{key: value for key, value in row.items() if value is not None} for row in refs]
    labels = {"legal_name": {"基金全称", "基金名称"}, "company_name": {"基金管理人", "基金管理公司"},
              "type": {"基金类型"}, "currency": {"交易币种", "基金计价币种", "净值币种"},
              "dealing_currency": {"交易币种", "申购币种"}, "execution_venue": {"交易场所", "申赎渠道", "交易方式"}}
    for field, choices in labels.items():
        value = _one(_label_values(blocks, choices), field)
        if value is None:
            continue
        if field in ("currency", "dealing_currency"):
            parsed = _currency(value)
            _identity_require(parsed is not None, "Unsupported or ambiguous source currency")
            result[field] = parsed
        elif field == "execution_venue":
            _identity_require(value in ("场外", "场外申购赎回", "场外申购、赎回", "天天基金场外", "场内", "证券交易所"), "Unresolved execution venue")
            result[field] = "off_exchange_nav" if "场外" in value else "exchange_quote"
            result["instrument_type"] = result[field]
        elif field == "type":
            result["issuer_fund_type"] = value
        else:
            if field == "company_name" and result.get(field) != value:
                _identity_require(value == result.get(field, "")+"管理有限公司", "Issuer and distributor management company conflict")
                result["company_legal_name"] = value
            elif field == "legal_name":
                _identity_require(not result.get(field) or re.sub(r"\s+", "", result[field]) == re.sub(r"\s+", "", value),
                        "Issuer and distributor legal subject conflict")
            else:
                result[field] = value
    if mapping["code"] in shares:
        result["share_class"] = shares[mapping["code"]]
        result["share_class_basis"] = "source_labelled_code_class_relation"
    if parent:
        result["disclosed_parent_code"] = parent
    if standard.get("declared_members"):
        result["group_membership"] = {"kind": "same_legal_fund", "codes": standard["declared_members"],
                                      "scope": "issuer_periodic_report_declared_share_classes", "evidence_refs": refs}
    if shares and re.search(r"(?:全部份额类别|基金份额分为|份额类别包括)", text):
        result["group_membership"] = {"kind": "same_legal_fund", "codes": sorted(shares),
                                       "scope": "issuer_explicit_complete_share_class_list", "evidence_refs": refs}
    if mapping["role"] == "holdings":
        weights = {}
        dates = re.findall(r"(?:报告期末|持仓日期|截至)\s*[:：]?\s*(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})", text)
        require(len(set(dates)) == 1, "Holdings require one explicit disclosure as-of date")
        as_of = dt.date(*map(int, dates[0])).isoformat()
        for block in blocks:
            headers, cells = block.get("headers", []), block.get("cells", [])
            sectors = [i for i, value in enumerate(headers) if value in ("行业类别", "行业", "行业分类")]
            amounts = [i for i, value in enumerate(headers) if "占基金资产净值比例" in value]
            if len(sectors) == len(amounts) == 1 and len(cells) == len(headers) and cells[sectors[0]] not in headers:
                sector, value = cells[sectors[0]], cells[amounts[0]]
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*%?", value)
                if sector in ("合计", "总计"):
                    continue
                require(match is not None, "Unknown holdings weight cannot become zero")
                require(sector not in weights, "Duplicate holdings sector")
                weights[sector] = str(Decimal(match[1])/100)
        require(weights and sum(map(Decimal, weights.values())) <= 1, "Unsupported or incomplete measured exposure table")
        total = sum(map(Decimal, weights.values()))
        result["sector_exposures"] = {"status": "measured_disclosure", "disclosed_as_of": as_of, "weights": weights,
                                       "coverage_weight": str(total), "unmapped_weight": str(1-total), "evidence_refs": refs}
    elif mapping["role"] == "company_restriction":
        values = _label_values(blocks, {"基金运作限制", "申购限制"})
        require(values and values <= {"禁止新增申购", "暂停运作", "正常运作"}, "Company restrictions need explicit operational terms")
        result["company_restrictions"] = [{"effect": {"禁止新增申购": "subscription_prohibited", "暂停运作": "operation_suspended", "正常运作": "no_disclosed_operational_restriction"}[value], "evidence_refs": refs} for value in sorted(values)]
    if mapping["role"] in ("identity", "manager"):
        proof = manager_evidence.build_proof(document, result)
        if mapping["role"] == "manager" or proof["row_inventory"]:
            result = manager_evidence.merge(result, proof)
    result.setdefault("field_evidence", {}).setdefault(mapping["role"], []).extend(refs)
    result["identity_scope"] = "reextracted_current_source_fields_unknowns_preserved"
    result["unknown_fields"] = [key for key in ("currency", "dealing_currency", "execution_venue", "share_class", "manager_tenures", "sector_exposures", "company_restrictions") if result.get(key) is None]
    return result


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def _text(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _code(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value), "Six-digit fund code required")
    return value


class _Tables(HTMLParser):
    """Read visible table cells and their links; never evaluate page scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("th", "td") and self.row is not None:
            if self.cell is not None:
                self._finish_cell()
            self.cell = {"parts": [], "links": []}
        elif tag == "a" and self.cell is not None:
            href = dict(attrs).get("href", "")
            self.cell["links"].append(href)

    def _finish_cell(self):
        self.row.append({"text": _text("".join(self.cell["parts"])), "links": self.cell["links"]})
        self.cell = None

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in ("th", "td") and self.cell is not None:
            self._finish_cell()
        elif tag == "tr" and self.row is not None:
            if self.cell is not None:
                self._finish_cell()
            if self.table is not None:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table, self.row, self.cell = None, None, None

    def handle_data(self, data):
        if not self.hidden and self.cell is not None:
            self.cell["parts"].append(data)


def _tables(text):
    parser = _Tables()
    parser.feed(text)
    parser.close()
    return parser.tables


def parse_catalog(text):
    match = re.fullmatch(r"\s*var\s+r\s*=\s*(\[.*\])\s*;?\s*", text, re.S)
    require(match is not None, "Directory must contain the declared JSON array")
    rows = strict_json_loads(match[1])
    require(isinstance(rows, list) and rows, "Current fund directory is empty")
    output = {}
    for row in rows:
        require(isinstance(row, list) and len(row) == 5 and all(isinstance(x, str) for x in row),
                "Directory row must contain code, initials, name, category and spelling")
        code = _code(row[0])
        require(code not in output and row[2].strip(), "Duplicate or unnamed directory identity")
        output[code] = {"code": code, "name": _text(row[2]), "type": _text(row[3]),
                        "initials": row[1], "spelling": row[4]}
    return output


def parse_profile(text, code, catalog_row):
    _code(code)
    matches = []
    for table in _tables(text):
        fields = {}
        for row in table:
            for i in range(0, len(row) - 1, 2):
                label, value = row[i]["text"], row[i + 1]
                if label in fields:
                    require(fields[label] == value, "Conflicting repeated fund profile field: " + label)
                fields[label] = value
        if "基金全称" in fields:
            matches.append(fields)
    require(len(matches) == 1, "One legal fund profile table required: " + code)
    fields = matches[0]
    for key in ("基金全称", "基金简称", "基金代码", "基金类型", "基金管理人", "基金经理人"):
        require(key in fields and fields[key]["text"] not in ("", "--", "---"), "Profile requires " + key)
    code_text = fields["基金代码"]["text"]
    role_pairs = re.findall(r"(?<![0-9])([0-9]{6})\s*\((前端|后端)\)", code_text)
    roles = {}
    for labelled_code, role in role_pairs:
        require(labelled_code not in roles or roles[labelled_code] == role, "Conflicting labelled dealing-code roles")
        roles[labelled_code] = role
    if roles:
        require(code in roles, "Profile does not label the requested dealing code")
        if roles[code] == "后端":
            raise DeferredFeeIdentity(code, roles)
    else:
        require(re.match(r"^" + code + r"(?:\D|$)", code_text), "Profile fund code conflicts with request")
    require(fields["基金简称"]["text"] == catalog_row["name"]
            and (not catalog_row["type"] or fields["基金类型"]["text"] == catalog_row["type"]),
            "Current directory and profile identity conflict; retrieve the issuer's latest disclosure")
    company_ids = {m[1] for link in fields["基金管理人"]["links"]
                   if (m := re.fullmatch(r"(?:https?:)?//fund\.eastmoney\.com/company/(\d+)\.html", link))}
    require(len(company_ids) == 1, "Profile requires an unambiguous disclosed management company identity")
    company_id = "eastmoney-company:" + next(iter(company_ids))
    legal_name = fields["基金全称"]["text"]
    # Keep the full legal name. Removing A/C, LOF or other suffixes would invent a
    # relationship when a fund has transformed or the distributor has stale data.
    group = "distributor-legal:" + fingerprint({"company_id": company_id, "legal_name": legal_name})[:32]
    managers = []
    for link in fields["基金经理人"]["links"]:
        match = re.fullmatch(r"(?:https?:)?//fund\.eastmoney\.com/manager/(\d+)\.html", link)
        if match:
            managers.append("eastmoney-manager:" + match[1])
    benchmark = fields.get("业绩比较基准", {}).get("text")
    target = fields.get("跟踪标的", {}).get("text")
    if target == "该基金无跟踪标的":
        target = None
    scope = ""
    for section in re.finditer(r"<h4\b[^>]*>(.*?)</h4>(.*?)(?=<h4\b|$)", text, re.S | re.I):
        if _text(re.sub(r"<[^>]+>", "", section[1])) == "投资范围":
            paragraph = re.search(r"<p\b[^>]*>(.*?)</p>", section[2], re.S | re.I)
            if paragraph:
                scope = _text(unescape(re.sub(r"<[^>]+>", " ", paragraph[1])))
    explicit_share = fields.get("基金份额类别", fields.get("份额类别", {})).get("text")
    share = re.fullmatch(r"([A-Z])(?:类)?", explicit_share or "")
    currency = _currency(fields.get("交易币种", fields.get("基金计价币种", {})).get("text"))
    venue = fields.get("交易场所", fields.get("申赎渠道", {})).get("text")
    venue = "off_exchange_nav" if venue in ("场外", "场外申购赎回", "天天基金场外") else "exchange_quote" if venue in ("场内", "证券交易所") else None
    return {"code": code, "name": catalog_row["name"], "legal_name": legal_name,
            "company_id": company_id, "company_name": fields["基金管理人"]["text"],
            "fund_group_id": group, "share_class": share[1] if share else None,
            "share_class_basis": "labelled_share_class" if share else "unknown",
            "currency": currency, "dealing_currency": _currency(fields.get("申购币种", fields.get("交易币种", {})).get("text")),
            "execution_venue": venue, "instrument_type": venue, "asset_class": fields["基金类型"]["text"],
            "sector_exposures": None, "company_restrictions": None, "group_membership": None, "field_evidence": {},
            "unknown_fields": [key for key, value in (("currency", currency), ("execution_venue", venue), ("share_class", share)) if value is None],
            "type": fields["基金类型"]["text"], "manager_names": fields["基金经理人"]["text"],
            "manager_ids": sorted(set(managers)), "manager_tenures": None,
            "benchmark_id": "disclosed-benchmark:" + fingerprint(benchmark)[:24] if benchmark else None,
            "benchmark_description": benchmark, "tracking_target": target, "investment_scope": scope,
            "identity_scope": "current_distributor_legal_name_and_management_company",
            "regulatory_parent_code": None, "historical_identity_verified": False}


def parse_actions(text, code):
    """Derive cash per share from the disclosed units and identify every split."""
    _code(code)
    code_match = re.search(r"(?:var\s+)?strbzdm\s*=\s*['\"]([0-9]{6})['\"]", text)
    require(code_match is not None and code_match[1] == code, "Corporate-action page identity is absent or conflicting")
    dividends, splits, found_cash, found_split = [], [], False, False
    for table in _tables(text):
        if not table:
            continue
        headers = [cell["text"] for cell in table[0]]
        if {"权益登记日", "除息日", "每10份分红", "分红发放日"} <= set(headers):
            require(not found_cash, "Duplicate cash-distribution table")
            found_cash = True
            for row in table[1:]:
                cells = [cell["text"] for cell in row]
                if len(cells) == 1 and "暂无分红" in cells[0]:
                    continue
                require(len(cells) == len(headers), "Incomplete corporate-action cash row")
                data = dict(zip(headers, cells))
                ex_date = dt.date.fromisoformat(data["除息日"]).isoformat()
                record_date = dt.date.fromisoformat(data["权益登记日"]).isoformat()
                pay_date = dt.date.fromisoformat(data["分红发放日"]).isoformat()
                amount = re.fullmatch(r"每([0-9]+(?:\.[0-9]+)?)份派现金([0-9]+(?:\.[0-9]+)?)元", data["每10份分红"])
                require(amount is not None and Decimal(amount[1]) == 10, "Cash units conflict with per-ten-shares table heading")
                require(record_date <= ex_date <= pay_date, "Corporate-action date sequence conflicts")
                dividends.append({"code": code, "date": ex_date, "record_date": record_date,
                                  "distribution_per_share": float(Decimal(amount[2]) / Decimal(amount[1])),
                                  "cash_payment_date": pay_date, "unit": "source_yuan_per_share",
                                  "disclosed_units": amount[1], "disclosed_cash": amount[2],
                                  "location": "cash distribution table; ex-date " + ex_date,
                                  "venue": "off_exchange", "currency": None, "dividend_mode": "cash_distribution_declared",
                                  "rights_rule": {"position_basis": "record_date_close",
                                      "subscribe_on_record_date": "unknown", "redeem_on_record_date": "unknown",
                                      "evidence_refs": [], "status": "issuer_qualification_terms_required"},
                                  "reinvestment_scope": "investor_election_unknown",
                                  "evidence_basis": "current_distributor_action_table"})
        elif {"拆分折算日", "拆分类型", "拆分折算比例"} <= set(headers):
            require(not found_split, "Duplicate split table")
            found_split = True
            for row in table[1:]:
                cells = [cell["text"] for cell in row]
                if len(cells) == 1 and "暂无拆分" in cells[0]:
                    continue
                require(len(cells) == len(headers), "Incomplete split row")
                data = dict(zip(headers, cells))
                splits.append({"date": dt.date.fromisoformat(data["拆分折算日"]).isoformat(),
                               "type": data["拆分类型"], "ratio_text": data["拆分折算比例"]})
    require(found_cash and found_split, "Cash and split disclosure tables are both required")
    require(len({row["date"] for row in dividends}) == len(dividends), "Duplicate cash ex-date needs original issuer review")
    return {"code": code, "dividends": sorted(dividends, key=lambda row: row["date"]),
            "splits": sorted(splits, key=lambda row: row["date"]),
            "scope": "distributor_disclosure_reproduction_not_independent_issuer_confirmation"}


def _capture(url, source_id, artifacts, timeout, max_bytes=source_fetch.MAX_BYTES, *, store, context=None):
    context = context or store.require_context()
    store.assert_owned(context)
    capture = source_fetch.fetch(url, source_id, timeout=timeout, max_bytes=max_bytes)
    store.assert_owned(context)
    return {"capture": {key: value for key, value in capture.items() if key not in ("raw_bytes", "text")},
            "raw_ref": artifacts.put_bytes(capture["raw_bytes"])}


def capture_text(record, artifacts, registry=None):
    source_fetch.validate_capture_provenance(record["capture"], registry)
    raw = artifacts.read(record["raw_ref"])
    capture = record["capture"]
    require(hashlib.sha256(raw).hexdigest() == capture["raw_sha256"] and len(raw) == capture["bytes"],
            "Captured source and artifact differ")
    require(isinstance(capture["encoding"], str), "Text source required; retrieve a supported issuer text disclosure")
    text = raw.decode(capture["encoding"], errors="strict")
    require(hashlib.sha256(text.encode("utf-8")).hexdigest() == capture["text_sha256"], "Captured text differs")
    return text


def _fresh(record, as_of, maximum_age):
    observed = dt.datetime.fromisoformat(record["capture"]["retrieved_at"])
    boundary = dt.datetime.fromisoformat(as_of)
    require(observed.utcoffset() is not None and boundary.utcoffset() is not None, "Source times require timezone")
    require(0 <= (boundary - observed).total_seconds() <= maximum_age,
            "Current identity source is future or stale; collect a new snapshot")


def collect_catalog(store, artifacts, operation_id, *, timeout=20, max_bytes=source_fetch.MAX_BYTES):
    context = store.require_context()
    store.assert_owned(context)
    prior = store.get("fund-catalog", operation_id)
    if prior is not None:
        snapshot = artifacts.read_json(prior)
        require(parse_catalog(capture_text(snapshot["source"], artifacts)) == snapshot["entries"], "Saved catalog changed")
        return prior
    source = _capture(CATALOG_URL, "eastmoney_catalog", artifacts, timeout, max_bytes, store=store, context=context)
    snapshot = {"schema_version": 4, "observed_at": source["capture"]["retrieved_at"],
                "source": source, "entries": parse_catalog(capture_text(source, artifacts)),
                "scope": "current_distributor_directory_search_index"}
    reference = artifacts.put_json(snapshot)
    if store is not None:
        store.put("fund-catalog", operation_id, reference)
    return reference


def _review_task(code, reason):
    if getattr(reason, "identity_status", None) == "unsupported_deferred_fee_identity":
        return {"code": code, "reason": str(reason), "identity_status": reason.identity_status,
                "source_role": reason.identity_details, "action": "obtain_deferred_fee_quote_mapping",
                "required_output": "source-supported deferred subscription/redemption terms and an implemented dealing-code mapping",
                "acceptance": "code remains excluded until the execution and independent audit models support its disclosed billing role"}
    return {"code": code, "reason": str(reason), "action": "retrieve_original_issuer_disclosure",
            "required_output": {"code": code, "legal_name": "quote", "management_company": "quote",
                                "parent_and_share_codes": "quote identifying the legal fund and share relationship",
                                "publication_date": "ISO date", "source_url": "registered HTTPS issuer URL"},
            "acceptance": "capture source bytes; verify literal quotes, publication date and code relationship; rerun identity resolution"}



def _issuer_failure(error):
    return {"kind": "issuer_document_rejected", "exception": type(error).__name__, "message": str(error),
            "critical_identity": isinstance(error, IssuerIdentityMismatch)}


def _issuer_task(item):
    mapping, failure = item["mapping"], item["failure"]
    return {"code": mapping["code"], "action": "resolve_issuer_source_evidence",
            "mapping_hash": fingerprint(mapping), "role": mapping["role"], "source_url": mapping["url"],
            "reason": failure["message"], "evidence_status": failure["kind"],
            "required_output": "original subject-bound disclosure whose requested fields can be reproduced",
            "acceptance": "unresolved issuer evidence cannot confer new purchase eligibility"}


def _apply_issuer(reference, mapping, identity, artifacts, registry=None):
    document = source_documents.read_extracted_document(reference, artifacts, registry=registry)
    require(source_fetch.source_rule(mapping["source_id"], registry)["purpose"] == "issuer_document",
            "Original issuer document required")
    require(document["source_id"] == mapping["source_id"] and document["capture"]["requested_url"] ==
            source_fetch.checked_url(mapping["url"], source_fetch.source_rule(mapping["source_id"], registry)),
            "Issuer document source differs")
    result = _identity_from_document(document, mapping, identity)
    if any(proof["document_id"] == document["document_id"] for proof in result.get("manager_evidence_proofs", [])):
        result = manager_evidence.merge(result, manager_evidence.build_proof(document, result, reference))
    for refs in result.get("field_evidence", {}).values():
        for ref in refs:
            if ref["document_id"] == document["document_id"]:
                ref["document_ref"] = reference
    membership = result.get("group_membership")
    if membership:
        for ref in membership["evidence_refs"]:
            if ref["document_id"] == document["document_id"]:
                ref["document_ref"] = reference
    return result


def _issuer_gaps(identities, rejected, unavailable):
    for item in rejected+unavailable:
        mapping = item["mapping"]
        if mapping["code"] in identities:
            identities[mapping["code"]].setdefault("source_evidence_gaps", []).append({
                "mapping_hash": fingerprint(mapping), "role": mapping["role"],
                "kind": item["failure"]["kind"], "message": item["failure"]["message"],
                "document_ref": item.get("document_ref"), "source_url": mapping["url"],
                "critical_identity": mapping["role"] == "identity" or item["failure"].get("critical_identity", False)})
    return identities


class _ProfileLinks(HTMLParser):
    """Discover hrefs without interpreting unrelated optional table closes."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.scripts, self.base_href = set(), set(), None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and isinstance(dict(attrs).get("src"), str):
            self.scripts.add(dict(attrs)["src"])
        href = dict(attrs).get("href")
        if isinstance(href, str) and href.strip():
            if tag == "a":
                self.links.add(href)
            elif tag == "base" and self.base_href is None:
                self.base_href = href


def _automatic_issuer_links(profiles, artifacts, registry=None):
    """Only links literally present in verified current distributor profiles."""
    registry = registry or source_fetch.load_registry()
    result = {}
    for code, record in sorted(profiles.items()):
        text = capture_text(record, artifacts, registry)
        parser = _ProfileLinks()
        parser.feed(text)
        parser.close()
        base_url = urllib.parse.urljoin(record["capture"]["final_url"], parser.base_href or "")
        for href in sorted(parser.links):
            try:
                url = urllib.parse.urljoin(base_url, href).split("#", 1)[0]
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme == "http" and parsed.hostname and not parsed.username and not parsed.password and parsed.port in (None, 80):
                    # Preserve the published href; only the transport changes.
                    # The registered host/path still needs a real TLS capture.
                    url = urllib.parse.urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))
                source_id = source_fetch.identify_source(url, registry)
                rule = source_fetch.source_rule(source_id, registry)
                if rule["purpose"] != "issuer_document":
                    continue
                url = source_fetch.checked_url(url, rule)
            except (ValueError, TypeError):
                continue
            key = fingerprint([code, source_id, url])
            result.setdefault(key, {"code": code, "source_id": source_id, "url": url, "href": href})
    return [result[key] for key in sorted(result)]


def _published_url(base, href):
    url = urllib.parse.urljoin(base, href).split("#", 1)[0]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "http" and parsed.hostname and not parsed.username and not parsed.password and parsed.port in (None, 80):
        url = urllib.parse.urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, ""))
    return url


def _announcement_menu(code, profile, artifacts, registry=None):
    parser = _ProfileLinks()
    parser.feed(capture_text(profile, artifacts, registry))
    parser.close()
    menus = {}
    for href in sorted(parser.links):
        try:
            url = _published_url(profile["capture"]["final_url"], href)
            if source_fetch.identify_source(url, registry) != "eastmoney_announcements_page":
                continue
            match = re.fullmatch(r"/jjgg_([0-9]{6})(?:_([1-6]))?\.html", urllib.parse.urlsplit(url).path)
            if match and match[1] == code:
                menus.setdefault(url, {"code": code, "href": href, "url": url, "category": match[2]})
        except ValueError:
            continue
    return min(menus.values(), key=lambda row: (row["category"] is not None, row["url"])) if menus else None


def _announcement_script(menu, artifacts, registry=None):
    parser = _ProfileLinks()
    text = capture_text(menu, artifacts, registry)
    try:
        parser.feed(text)
        parser.close()
    except AssertionError as error:
        raise ValueError("Original announcement HTML declaration is malformed: " + str(error)) from error
    for href in sorted(parser.scripts):
        try:
            url = _published_url(menu["capture"]["final_url"], href)
            if source_fetch.identify_source(url, registry) == "eastmoney_announcements_script":
                return url
        except ValueError:
            continue
    raise ValueError("Original announcement menu has no supported published script link")


def _announcement_recipe(code, menu, script, artifacts, registry=None):
    html, javascript = capture_text(menu, artifacts, registry), capture_text(script, artifacts, registry)
    require(script["capture"]["requested_url"] == _announcement_script(menu, artifacts, registry), "Announcement script is not the original menu link")
    block = re.search(r'<script[^>]*id=[\"\x27]jjggtmp[\"\x27][^>]*>(.*?)</script>', html, re.S)
    require(block is not None, "Original announcement link template is unavailable")
    template = re.search(r'href=[\"\x27](https?://pdf\.dfcfw\.com/pdf/H2_\{\{value\.ID\}\}_1\.pdf)[\"\x27]', block[1])
    require(template is not None and 'value.ATTACHTYPE' in block[1], "Original PDF attachment template is unsupported")
    host = re.search(r'var\s+apiHost\s*=\s*[\"\x27]([^\"\x27]+)[\"\x27]', javascript)
    require(host is not None and '/f10/JJGG?callback=?&fundcode=' in javascript
            and all(value in javascript for value in ('&pageIndex=', '&pageSize=', '&type=')), "Published announcement API recipe is unsupported")
    subject = re.search(r'var\s+strbzdm\s*=\s*[\"\x27]([0-9]{6})[\"\x27]', html)
    params = re.search(r'var\s+params\s*=\s*\{\s*code:\s*strbzdm,\s*pindex:\s*(\d+),\s*pernum:\s*(\d+),\s*type:\s*(\d+)\s*\}', html)
    require(subject is not None and subject[1] == code and params is not None, "Announcement menu subject or declared request parameters differ")
    page, size, kind = map(int, params.groups())
    require(page == 1 and 1 <= size <= 20 and 0 <= kind <= 6, "Only the declared bounded first announcement page is supported")
    api = _published_url(menu["capture"]["final_url"], host[1]+"/f10/JJGG")
    api += "?" + urllib.parse.urlencode({"fundcode": code, "pageIndex": page, "pageSize": size, "type": kind})
    require(source_fetch.identify_source(api, registry) == "eastmoney_announcements_api", "Announcement recipe resolves outside its registered endpoint")
    return {"api_url": api, "pdf_template": template[1], "page_size": size}


def _announcement_links(trace, profile, artifacts, registry=None, boundary=None, age=None):
    origin = _announcement_menu(trace["code"], profile, artifacts, registry)
    require(origin is not None and trace["origin"] == origin, "Announcement directory is not the original profile menu")
    packets = [trace[key] for key in ("menu", "script", "api")]
    for packet in packets:
        if packet is not None:
            capture_text(packet, artifacts, registry)
            if boundary is not None:
                _fresh(packet, boundary, age)
    if any(packet is None for packet in packets):
        require(trace["failure"] and trace["failure"].get("message"), "Unfinished announcement discovery needs an explicit source task")
        return []
    require(trace["menu"]["capture"]["requested_url"] == origin["url"], "Announcement menu capture belongs to another source")
    recipe = _announcement_recipe(trace["code"], trace["menu"], trace["script"], artifacts, registry)
    require(trace["api"]["capture"]["requested_url"] == recipe["api_url"], "Announcement API parameters differ from original menu/script")
    data = strict_json_loads(capture_text(trace["api"], artifacts, registry))
    require(isinstance(data, dict) and data.get("ErrCode") == 0 and data.get("PageIndex") == 1
            and data.get("PageSize") == recipe["page_size"] and isinstance(data.get("Data"), list)
            and len(data["Data"]) <= recipe["page_size"], "Announcement first-page response is unsupported")
    rows = []
    for index, row in enumerate(data["Data"]):
        require(row.get("FUNDCODE") == trace["code"] and isinstance(row.get("TITLE"), str)
                and re.fullmatch(r"AN[0-9]{18}", row.get("ID", "")), "Announcement row subject or original attachment identity differs")
        if str(row.get("ATTACHTYPE")) != "0":
            continue
        priorities = ("资料概要", "招募说明书", "基金合同", "中期报告", "年度报告", "季度报告")
        priority = next((value for value, term in enumerate(priorities) if term in row["TITLE"]), None)
        if priority is not None:
            rows.append((priority, index, row))
    links = []
    for _, _, row in sorted(rows)[:3]:
        href = recipe["pdf_template"].replace("{{value.ID}}", row["ID"])
        url = _published_url(trace["menu"]["capture"]["final_url"], href)
        require(source_fetch.identify_source(url, registry) == "issuer_eastmoney_pdf", "Published PDF template resolves outside its registered source")
        links.append({"code": trace["code"], "source_id": "issuer_eastmoney_pdf", "url": url, "href": href})
    return links


def _automatic_issuer_mappings(document, code, current):
    # Broad documents are not narrowed by guessing the relevant paragraphs.
    # Ambiguous subjects/fields are rejected by the same source adapters as
    # explicitly provided evidence. No URL, product code or locator is invented.
    original = [row for row in document["blocks"] if row["kind"] != "table_cell"]
    if any(manager_evidence.HEADERS <= set(row.get("headers", [])) for row in original):
        blocks, subject = manager_evidence.subject_blocks(document, current)
        require(blocks and subject["status"] == "unique_primary_product", "Discovered issuer source primary subject is unresolved")
    else:
        blocks = original
    require(blocks, "Discovered issuer source has no supported structural blocks")
    locators = [row["locator"] for row in blocks]
    roles = ["identity"]
    if any({"基金经理", "任职日期", "离任日期"} <= set(row.get("headers", [])) for row in blocks):
        roles.append("manager")
    if any(any("占基金资产净值比例" in value for value in row.get("headers", [])) for row in blocks):
        roles.append("holdings")
    mappings, rejections = [], []
    for role in roles:
        mapping = {"code": code, "source_id": document["source_id"],
                   "url": document["capture"]["requested_url"], "role": role, "locators": locators}
        try:
            _identity_from_document(document, mapping, current)
        except (ValueError, KeyError) as error:
            rejections.append({"role": role, "failure": _issuer_failure(error)})
        else:
            mappings.append(mapping)
    return {"mappings": mappings, "rejections": rejections}


def _automatic_issuer_task(record):
    return {"code": record["code"], "action": "complete_discovered_issuer_source",
            "source_id": record["source_id"], "source_url": record["url"],
            "original_profile_href": record["href"], "failure": record["failure"],
            "rejections": record["rejections"],
            "acceptance": "capture the linked original; reproduce primary subject and supported locators without inferred fields"}


def _verify_automatic_issuer(snapshot, rebuilt, artifacts, registry, boundary, age):
    records = snapshot["automatic_issuer_discovery"]
    require(type(records) is list, "Automatic issuer discovery must preserve its actual link inventory")
    if snapshot["automatic_issuer_scope"] == "not_requested":
        require(not records, "Unrequested automatic discovery contains invented records")
        return
    require(snapshot["automatic_issuer_scope"] == "budgeted_profile_href_links", "Unknown automatic issuer source scope")
    links = _automatic_issuer_links(snapshot["profiles"], artifacts, registry)
    traces = snapshot.get("announcement_discovery", [])
    require(type(traces) is list and len({trace["code"] for trace in traces}) == len(traces), "Announcement discovery source inventory is duplicated")
    for trace in traces:
        try:
            discovered = _announcement_links(trace, snapshot["profiles"][trace["code"]], artifacts, registry, boundary, age)
        except (ValueError, KeyError) as error:
            require(trace["failure"] == _issuer_failure(error), "Preserved announcement rejection differs from original source")
        else:
            if all(trace[key] is not None for key in ("menu", "script", "api")):
                require(trace["failure"] is None, "A valid captured announcement recipe cannot be hidden as rejected")
            links.extend(discovered)
    expected = {fingerprint(row): row for row in links}
    seen, requests = set(), {fingerprint(row) for row in snapshot["issuer_requests"]}
    actions = {fingerprint(row) for row in snapshot["required_actions"]}
    for record in records:
        fields(record, {"code", "source_id", "url", "href", "document_ref", "mappings", "rejections", "failure"},
               label="automatic issuer discovery")
        link = {key: record[key] for key in ("code", "source_id", "url", "href")}
        key = fingerprint(link)
        require(key in expected and key not in seen, "Automatic issuer URL is not a unique original profile link")
        seen.add(key)
        if record["document_ref"] is None:
            require(record["failure"] and record["failure"].get("kind") in ("capture_pending", "capture_unavailable")
                    and record["failure"].get("message") and not record["mappings"] and not record["rejections"],
                    "Uncaptured issuer link must retain its specific missing source task")
        else:
            metadata = artifacts.read_json(record["document_ref"])
            _fresh({"capture": metadata["capture"]}, boundary, age)
            require(metadata["source_id"] == record["source_id"]
                    and metadata["capture"]["requested_url"] == record["url"], "Automatic issuer capture belongs to another link")
            try:
                document = source_documents.read_extracted_document(record["document_ref"], artifacts, registry=registry)
                interpretation = _automatic_issuer_mappings(document, record["code"], rebuilt[record["code"]])
            except (ValueError, KeyError) as error:
                require(record["failure"] == _issuer_failure(error) and not record["mappings"] and not record["rejections"],
                        "Automatic issuer rejection does not reproduce its original source")
            else:
                require(record["failure"] is None and all(record[key] == interpretation[key] for key in interpretation),
                        "Automatic issuer locators differ from original document re-extraction")
                require({fingerprint(row) for row in record["mappings"]} <= requests,
                        "Automatically discovered source mapping was omitted from identity resolution")
        if record["failure"] or record["rejections"] or not record["mappings"]:
            require(fingerprint(_automatic_issuer_task(record)) in actions, "Automatic issuer gap lost its source-specific task")
    require(seen == set(expected), "Automatic issuer source inventory omitted actual supported profile links")


def resolve_identity(codes, catalog_ref, artifacts, *, store, context=None, timeout=20, as_of=None, sources=None,
                     issuer_disclosures=None, issuer_sources=None, fetch_missing=True, automatic_issuer_discovery=None, announcement_discovery=None):
    context = context or store.require_context()
    store.assert_owned(context)
    catalog = artifacts.read_json(catalog_ref)
    entries = parse_catalog(capture_text(catalog["source"], artifacts))
    profiles, rejected_profiles, identities, pending, documents, captured_documents = {}, {}, {}, [], [], {}
    for code in sorted(set(codes)):
        _code(code)
        record = None
        try:
            require(code in entries, "Code absent from current directory")
            record = (sources or {}).get(code)
            if record is None:
                require(fetch_missing, "Profile capture is pending in the current coverage batch")
                record = _capture("https://fundf10.eastmoney.com/jbgk_"+code+".html", "eastmoney_profile",
                                  artifacts, timeout, store=store, context=context)
            identities[code] = parse_profile(capture_text(record, artifacts), code, entries[code])
            profiles[code] = record
        except (OSError, ValueError) as error:
            if record is not None:
                rejected_profiles[code] = {"source": record, "failure": _profile_failure(error)}
            pending.append(_review_task(code, error))
    rejected_documents, unavailable_documents = [], []
    require(len({fingerprint(row) for row in issuer_disclosures or []}) == len(issuer_disclosures or []),
            "Repeated issuer interpretation request")
    for mapping in issuer_disclosures or []:
        code = _code(mapping.get("code"))
        require(code in codes, "Issuer supplement must belong to the requested scope")
        reference = None
        if code not in identities:
            item = {"mapping": copy.deepcopy(mapping), "failure": {"kind": "issuer_profile_unavailable",
                    "exception": None, "message": "Issuer interpretation awaits a supported dealing identity"}}
            unavailable_documents.append(item)
            pending.append(_issuer_task(item))
            continue
        key = fingerprint(mapping)
        try:
            require(source_fetch.source_rule(mapping["source_id"])["purpose"] == "issuer_document", "Original issuer document required")
            reference = (issuer_sources or {}).get(key)
            document_key = fingerprint([mapping["source_id"], mapping["url"]])
            reference = reference or captured_documents.get(document_key)
            if reference is None:
                require(fetch_missing, "Original issuer capture is pending in the current coverage batch")
                reference = source_documents.capture_document(mapping["url"], mapping["source_id"], artifacts,
                                                               timeout=timeout, store=store, context=context)
            captured_documents[document_key] = reference
            identities[code] = _apply_issuer(reference, mapping, identities[code], artifacts)
            documents.append({"mapping": copy.deepcopy(mapping), "document_ref": reference})
        except (OSError, ValueError, KeyError) as error:
            item = {"mapping": copy.deepcopy(mapping), "failure": _issuer_failure(error)}
            if reference is not None:
                item["document_ref"] = reference
                rejected_documents.append(item)
            else:
                item["failure"]["kind"] = "issuer_capture_unavailable"
                unavailable_documents.append(item)
            pending.append(_issuer_task(item))
    automatic_records = copy.deepcopy(automatic_issuer_discovery or [])
    announcement_records = copy.deepcopy(announcement_discovery or [])
    for trace in announcement_records:
        if trace["failure"]:
            pending.append({"code": trace["code"], "action": "complete_original_announcement_discovery", "source_url": trace["origin"]["url"], "failure": trace["failure"]})
    for record in automatic_records:
        if record["failure"] or record["rejections"] or not record["mappings"]:
            pending.append(_automatic_issuer_task(record))
    _issuer_gaps(identities, rejected_documents, unavailable_documents)
    snapshot = {"schema_version": 4, "observed_at": as_of or utc_now(), "catalog_ref": catalog_ref,
                "profiles": profiles, "rejected_profiles": rejected_profiles, "identities": identities, "required_actions": pending,
                "issuer_documents": documents, "rejected_issuer_documents": rejected_documents,
                "pending_issuer_requests": unavailable_documents, "issuer_requests": copy.deepcopy(issuer_disclosures or []),
                "automatic_issuer_discovery": automatic_records,
                "announcement_discovery": announcement_records,
                "automatic_issuer_scope": "budgeted_profile_href_links" if automatic_issuer_discovery is not None else "not_requested",
                "requested_codes": sorted(set(codes)), "max_age_seconds": DEFAULT_POLICY["max_age_seconds"],
                "scope": "current_source_identity_with_explicit_units_and_unknowns"}
    store.assert_owned(context)
    return artifacts.put_json(snapshot)


def verify_identity(snapshot, artifacts, *, as_of=None, max_age_seconds=None, registry=None):
    require(snapshot.get("schema_version") == 4, "Current identity snapshot schema required")
    requested = snapshot["requested_codes"]
    require(type(requested) is list and requested == sorted(set(requested)), "Distinct identity codes required")
    boundary = as_of or snapshot["observed_at"]
    age = max_age_seconds if max_age_seconds is not None else snapshot["max_age_seconds"]
    require(type(age) is int and 0 < age <= 7*86400, "Bounded source freshness required")
    catalog = artifacts.read_json(snapshot["catalog_ref"])
    _fresh(catalog["source"], boundary, age)
    require(catalog["source"]["capture"]["requested_url"] == CATALOG_URL, "Catalog source changed")
    entries = parse_catalog(capture_text(catalog["source"], artifacts, registry))
    require(entries == catalog["entries"], "Directory differs from captured source")
    rebuilt = {}
    require(not set(snapshot["profiles"]) & set(snapshot["rejected_profiles"]), "A profile cannot be accepted and rejected together")
    for code, record in snapshot["profiles"].items():
        require(code in requested and record["capture"]["requested_url"] == "https://fundf10.eastmoney.com/jbgk_"+code+".html",
                "Profile belongs to another code")
        _fresh(record, boundary, age)
        rebuilt[code] = parse_profile(capture_text(record, artifacts, registry), code, entries[code])
    for code, rejected in snapshot["rejected_profiles"].items():
        record = rejected["source"]
        require(code in requested and code in entries
                and record["capture"]["requested_url"] == "https://fundf10.eastmoney.com/jbgk_"+code+".html",
                "Rejected capture belongs to another requested code")
        _fresh(record, boundary, age)
        # Provenance/byte corruption is always fatal; only interpretation
        # rejection is scoped to this code.
        text = capture_text(record, artifacts, registry)
        try:
            parse_profile(text, code, entries[code])
        except ValueError as error:
            require(rejected["failure"] == _profile_failure(error), "Rejected profile failure differs from original source")
            require(fingerprint(_review_task(code, error)) in {fingerprint(row) for row in snapshot["required_actions"]},
                    "Rejected profile lost its specific qualification action")
        else:
            require(False, "A successfully parsed profile cannot be hidden as rejected evidence")
    _verify_automatic_issuer(snapshot, rebuilt, artifacts, registry, boundary, age)
    accepted = {fingerprint(row["mapping"]): row for row in snapshot["issuer_documents"]}
    rejected = {fingerprint(row["mapping"]): row for row in snapshot["rejected_issuer_documents"]}
    unavailable = {fingerprint(row["mapping"]): row for row in snapshot["pending_issuer_requests"]}
    requested_mappings = {fingerprint(row) for row in snapshot["issuer_requests"]}
    require(len(accepted) == len(snapshot["issuer_documents"])
            and len(rejected) == len(snapshot["rejected_issuer_documents"])
            and len(unavailable) == len(snapshot["pending_issuer_requests"])
            and len(requested_mappings) == len(snapshot["issuer_requests"])
            and not (set(accepted)&set(rejected) or set(accepted)&set(unavailable) or set(rejected)&set(unavailable))
            and set(accepted)|set(rejected)|set(unavailable) == requested_mappings,
            "Every issuer request needs one preserved success, rejected capture or unavailable-source record")
    actions = {fingerprint(row) for row in snapshot["required_actions"]}
    for mapping in snapshot["issuer_requests"]:
        key, code = fingerprint(mapping), mapping["code"]
        if key in unavailable:
            item = unavailable[key]
            require(item["failure"]["kind"] in ("issuer_profile_unavailable", "issuer_capture_unavailable")
                    and bool(item["failure"]["message"]) and fingerprint(_issuer_task(item)) in actions,
                    "Unavailable issuer evidence lost its required action")
            if item["failure"]["kind"] == "issuer_profile_unavailable":
                require(code not in rebuilt, "Issuer profile is available but marked unavailable")
            continue
        item = accepted.get(key) or rejected[key]
        reference = item["document_ref"]
        document = artifacts.read_json(reference)
        _fresh({"capture": document["capture"]}, boundary, age)
        require(code in rebuilt, "Issuer interpretation lacks its source profile")
        if key in accepted:
            rebuilt[code] = _apply_issuer(reference, mapping, rebuilt[code], artifacts, registry)
        else:
            try:
                _apply_issuer(reference, mapping, rebuilt[code], artifacts, registry)
            except (OSError, ValueError, KeyError) as error:
                require(item["failure"] == _issuer_failure(error) and fingerprint(_issuer_task(item)) in actions,
                        "Rejected issuer source no longer reproduces its preserved reason")
            else:
                require(False, "A readable valid issuer source cannot be hidden as rejected")
    _issuer_gaps(rebuilt, snapshot["rejected_issuer_documents"], snapshot["pending_issuer_requests"])
    require(rebuilt == snapshot["identities"], "Identity fields differ from original labelled source re-extraction")
    missing = set(requested)-set(rebuilt)
    require(missing <= {row["code"] for row in snapshot["required_actions"]}, "Unresolved identities need an explicit source task")
    return rebuilt


def archive_identity(snapshot, source_artifacts, destination_artifacts):
    """Copy verified source bytes so the archive remains independently readable."""
    verify_identity(snapshot, source_artifacts)
    result = copy.deepcopy(snapshot)
    catalog = source_artifacts.read_json(result["catalog_ref"])
    catalog["source"]["raw_ref"] = destination_artifacts.put_bytes(source_artifacts.read(catalog["source"]["raw_ref"]))
    result["catalog_ref"] = destination_artifacts.put_json(catalog)
    for record in result["profiles"].values():
        record["raw_ref"] = destination_artifacts.put_bytes(source_artifacts.read(record["raw_ref"]))
    for rejected in result["rejected_profiles"].values():
        record = rejected["source"]
        record["raw_ref"] = destination_artifacts.put_bytes(source_artifacts.read(record["raw_ref"]))
    replacement = {}
    for item in result["issuer_documents"]+result["rejected_issuer_documents"]:
        old = item["document_ref"]
        item["document_ref"] = source_documents.archive_document(old, source_artifacts, destination_artifacts)
        replacement[fingerprint(old)] = item["document_ref"]
    for item in result["automatic_issuer_discovery"]:
        old = item["document_ref"]
        if old is not None:
            replacement.setdefault(fingerprint(old), source_documents.archive_document(old, source_artifacts, destination_artifacts))
            item["document_ref"] = replacement[fingerprint(old)]
    for trace in result.get("announcement_discovery", []):
        for key in ("menu", "script", "api"):
            if trace[key] is not None:
                trace[key]["raw_ref"] = destination_artifacts.put_bytes(source_artifacts.read(trace[key]["raw_ref"]))
    def relocate(value):
        if isinstance(value, dict):
            if "document_ref" in value and fingerprint(value["document_ref"]) in replacement:
                value["document_ref"] = replacement[fingerprint(value["document_ref"])]
            for child in value.values():
                relocate(child)
            if "proof_hash" in value and "row_inventory" in value:
                value["proof_hash"] = fingerprint({key: item for key, item in value.items() if key != "proof_hash"})
        elif isinstance(value, list):
            for child in value:
                relocate(child)
    relocate(result["identities"])
    verify_identity(result, destination_artifacts)
    return result


def _policy(policy):
    require(isinstance(policy, dict), "Discovery budget policy required")
    require(not (set(policy) - set(DEFAULT_POLICY)), "Unknown discovery budget field")
    result = {**DEFAULT_POLICY, **policy}
    for key, value in result.items():
        require(type(value) is int and value > 0, "Positive integer discovery budget required: " + key)
    require(result["timeout_seconds"] <= 30, "Per-source timeout <=30 required")
    return result


def _theses(review):
    rows = review.get("industry_theses", [])
    require(isinstance(rows, list), "Sealed industry_theses must be an array")
    output = []
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("thesis_id"), str) and row["thesis_id"],
                "Industry thesis requires its taxonomy identity")
        terms = row.get("search_terms")
        require(isinstance(terms, list) and terms and all(isinstance(term, str) and len(term.strip()) >= 2 for term in terms),
                "Industry thesis requires explicit retrieval terms")
        require(row.get("direction") in ("increase", "decrease", "hold", "watch"),
                "Explicit industry direction required")
        require(row.get("kind") in ("asset_class", "industry", "theme", "broad_market"), "Industry thesis kind required")
        require(type(row.get("horizon_days")) is int and row["horizon_days"] > 0, "Industry thesis horizon required")
        require(isinstance(row.get("claim_ids"), list) and row["claim_ids"], "Verified news claim IDs required")
        require(isinstance(row.get("evidence_refs"), list) and row["evidence_refs"], "Sealed news evidence references required")
        output.append({**row, "search_terms": [_text(term) for term in terms]})
    require(len({row["thesis_id"] for row in output}) == len(output), "Distinct sealed industry thesis IDs required")
    return output


def _qualified_exposures(identity, theses):
    """A labelled target/category or measured disclosure establishes the relation.

    Free-form permitted investment scope is a research lead, not evidence that
    an asset is held or that a negated/optional sector should be bought.
    """
    matches, actions = [], []
    for thesis in theses:
        support, matched, weight = [], set(), None
        target = identity.get("tracking_target")
        target_terms = [term for term in thesis["search_terms"] if target and term in target]
        if target_terms:
            support.append({"field": "tracking_target", "text": target, "scope": "explicit_source_tracking_target"})
            matched.update(target_terms)
        category = identity.get("asset_class") or identity.get("type")
        category_terms = [term for term in thesis["search_terms"] if category and term in category]
        if thesis["kind"] == "asset_class" and category_terms:
            support.append({"field": "asset_class", "text": category, "scope": "source_disclosed_asset_category"})
            matched.update(category_terms)
        disclosure = identity.get("sector_exposures") or {}
        if disclosure.get("status") == "measured_disclosure":
            sectors = {sector: value for sector, value in disclosure["weights"].items()
                       if Decimal(str(value)) > 0 and any(term in sector for term in thesis["search_terms"])}
            if sectors:
                weight = str(sum(Decimal(str(value)) for value in sectors.values()))
                require(Decimal(weight) <= 1, "Disclosed exposure weights exceed the reported portfolio")
                support.append({"field": "sector_exposures", "text": ", ".join(sorted(sectors)),
                    "weights": sectors, "disclosed_as_of": disclosure["disclosed_as_of"],
                    "evidence_refs": disclosure["evidence_refs"], "scope": "last_disclosed_weight_not_live_holdings"})
                matched.update(term for term in thesis["search_terms"] if any(term in sector for sector in sectors))
        if support:
            matches.append({"thesis_id": thesis["thesis_id"], "direction": thesis["direction"],
                "matched_terms": sorted(matched), "disclosed_text": " | ".join(row["text"] for row in support),
                "supporting_fields": support, "scope": "source_labeled_or_measured_exposure", "weight": weight})
        else:
            mandate = identity.get("investment_scope") or ""
            lead_terms = [term for term in thesis["search_terms"] if term in mandate]
            if lead_terms:
                actions.append({"code": identity["code"], "action": "verify_actual_product_exposure",
                    "thesis_id": thesis["thesis_id"], "matched_terms": lead_terms, "disclosed_mandate": mandate,
                    "reason": "Permitted or prohibited investment-scope wording does not establish actual exposure",
                    "required_output": "subject-bound tracking target, explicit asset category or dated measured holdings with original locators",
                    "acceptance": "re-extract the semantic source relationship; do not infer exposure from a word or its negation"})
    return matches, actions


def _derive_selection(entries, identities, held, monitoring, theses, universe_policy, budget, plan):
    groups = fund_screen.build_groups(entries, identities, universe_policy)
    category_labels = sorted({row["type"] for row in entries.values() if row.get("type")}
                             | {row["type"] for row in identities.values() if row.get("type")})
    exposures, directions, exposure_actions = {}, {}, []
    for code, identity in identities.items():
        matches, actions = _qualified_exposures(identity, theses)
        exposure_actions.extend(actions)
        exposures[code] = matches
        directions[code] = {row["direction"] for row in matches}
    candidates = set()
    mandatory = set(held) | set(monitoring)
    comparison_codes = mandatory & set(identities)
    prequalification = {}
    for group in groups:
        if group["coverage_status"].startswith("complete_"):
            comparison_codes |= set(group["members"])
    for code in sorted(comparison_codes):
        reasons = fund_screen.identity_constraints(identities[code], universe_policy, plan, category_labels=category_labels)
        if not exposures[code]:
            reasons.append("no_active_source_linked_industry_thesis")
        prequalification[code] = {"eligible": not reasons, "reasons": sorted(set(reasons)),
                                 "stage": "identity_plan_and_thesis_before_fee_verification"}
    selected = set(mandatory) & set(identities)
    for group in groups:
        if not group["coverage_status"].startswith("complete_"):
            continue
        qualifying = {code for code in group["members"] if prequalification[code]["eligible"]}
        # All peer evidence remains in groups/comparison_identities. Products
        # already excluded by source identity or the plan need no price/fee run.
        # The remaining qualified members are admitted as one group, not split.
        selected |= qualifying
        candidates |= qualifying
    seed = {code for code, entry in entries.items() if any(term in entry["name"] or term in entry["type"]
            for row in theses for term in row["search_terms"])} | mandatory
    categories = {entries[code]["type"] for code in seed if code in entries}
    scope = seed | {code for code, row in entries.items() if row["type"] in categories or not row["type"]}
    pending = (any(group["coverage_status"] == "pending" for group in groups)
               or bool(scope-set(identities)))
    critical = [code for code in mandatory if code not in identities or
                any(identities[code].get(key) != value for key, value in
                    (("currency", "CNY"), ("execution_venue", "off_exchange_nav")))
                or any(gap["critical_identity"] for gap in identities[code].get("source_evidence_gaps", []))]
    if critical:
        status = "needs_research"
    elif selected:
        status = "ready_with_pending_groups" if pending else "ready"
    elif pending:
        status = "awaiting_coverage"
    else:
        status = "observation_only"
    pending_scopes, compact_groups = {}, []
    for group in groups:
        pending = group["pending_codes"]
        scope_hash = fingerprint(pending)
        pending_scopes[scope_hash] = pending
        compact_groups.append({key: value for key, value in group.items() if key != "pending_codes"})
        compact_groups[-1].update(pending_code_hash=scope_hash, pending_code_count=len(pending))
    return {"status": status, "groups": compact_groups, "pending_scopes": pending_scopes, "category_labels": category_labels,
            "comparison_identities": {code: identities[code] for code in sorted(comparison_codes)},
            "prequalification": prequalification, "codes": sorted(selected), "exposures": exposures,
            "exposure_required_actions": exposure_actions,
            "completed_group_candidate_codes": sorted(candidates), "pending_fee_codes": sorted(candidates),
            "eligible_buy_codes": [],
            "critical_identity_codes": sorted(critical)}


def discover(news_review, held_codes, store, artifacts, operation_id, policy, *, universe_policy,
             news_state, issuer_disclosures=None, continue_from=None, plan_constraints=None, monitoring_codes=()):
    """Advance a frozen-source coverage ledger without ranking code order."""
    context = store.require_context()
    store.assert_owned(context)
    budget = _policy(policy)
    fund_screen.validate_universe_policy(universe_policy)
    fund_screen.validate_plan_constraints(plan_constraints)
    held, monitoring = sorted({_code(code) for code in held_codes}), sorted({_code(code) for code in monitoring_codes})
    theses = [row for row in _theses(news_review) if row["thesis_id"] in news_state["active_thesis_ids"]]
    request = {"news_review_hash": fingerprint(news_review), "news_state": news_state, "held_codes": held,
               "monitoring_codes": monitoring, "policy": budget, "universe_policy": universe_policy,
               "issuer_disclosures": issuer_disclosures or [], "continue_from": continue_from,
               "plan_constraints": plan_constraints}
    prior = store.get("candidate-set", operation_id)
    if prior is not None:
        require(prior["request_hash"] == fingerprint(request), "Discovery operation changed inputs")
        prior = {**artifacts.read_json(prior["candidate_ref"]), "candidate_ref": prior["candidate_ref"]}
        validate_discovery(prior, news_review, held, artifacts, utc_now(), monitoring_codes=monitoring,
                           news_state=news_state, plan_constraints=plan_constraints)
        return prior
    started = time.monotonic()
    catalog_ref = collect_catalog(store, artifacts, operation_id, timeout=budget["timeout_seconds"],
                                  max_bytes=min(source_fetch.MAX_BYTES, budget["max_total_bytes"]))
    catalog = artifacts.read_json(catalog_ref)
    entries, profiles, issuer_sources, inherited_mappings = catalog["entries"], {}, {}, []
    previously_attempted = set()
    if continue_from is not None:
        previous = artifacts.read_json(continue_from)
        require(previous["schema_version"] == 4 and previous["universe_policy"] == universe_policy,
                "Continuation requires the same current universe rules")
        old = artifacts.read_json(previous["identity_snapshot_ref"])
        verify_identity(old, artifacts, as_of=utc_now(), max_age_seconds=budget["max_age_seconds"])
        old_entries = artifacts.read_json(old["catalog_ref"])["entries"]
        profiles = {code: record for code, record in old["profiles"].items()
                    if code in entries and entries[code] == old_entries[code]}
        profiles.update({code: row["source"] for code, row in old["rejected_profiles"].items()
                         if code in entries and entries[code] == old_entries[code]})
        issuer_sources = {fingerprint(item["mapping"]): item["document_ref"] for item in old["issuer_documents"]+old["rejected_issuer_documents"]}
        inherited_mappings = old["issuer_requests"]
        previously_attempted = set(previous["coverage"]["attempted_history"])
    mappings = list({fingerprint(item): item for item in inherited_mappings+(issuer_disclosures or [])}.values())
    seed = {code for code, entry in entries.items() if any(
        term in entry["name"] or term in entry["type"] for row in theses for term in row["search_terms"])}
    seed |= set(held) | set(monitoring) | {item["code"] for item in mappings}
    # All catalogue members of relevant categories remain in the declared search
    # scope. Ordering only schedules I/O; it never grants purchase eligibility.
    categories = {entries[code]["type"] for code in seed if code in entries}
    scope = seed | {code for code, entry in entries.items() if entry["type"] in categories or not entry["type"]}
    mandatory = set(held) | set(monitoring)
    declared = {item["code"] for item in mappings}
    byte_count = catalog["source"]["capture"]["bytes"]
    requests, attempted, failures = 1, [], []
    io_records = [{"phase": "directory", "source_id": "eastmoney_catalog", "url": CATALOG_URL,
        "code": None, "outcome": "captured", "reserved_bytes": min(source_fetch.MAX_BYTES, budget["max_total_bytes"]),
        "charged_bytes": byte_count, "raw_ref": catalog["source"]["raw_ref"], "error": None}]
    document_cache = {}
    for mapping in mappings:
        reference = issuer_sources.get(fingerprint(mapping))
        if reference:
            document_cache[fingerprint([mapping["source_id"], mapping["url"]])] = reference
    if continue_from is not None:
        for item in old["automatic_issuer_discovery"]:
            if item["document_ref"] is not None:
                document_cache[fingerprint([item["source_id"], item["url"]])] = item["document_ref"]

    def reserve(phase, source_id, url, code):
        nonlocal requests, byte_count
        if (requests >= budget["max_source_requests"] or byte_count >= budget["max_total_bytes"]
                or time.monotonic()-started >= budget["max_elapsed_seconds"]):
            return None
        allowance = min(source_fetch.MAX_BYTES, budget["max_total_bytes"]-byte_count)
        # Reserve the maximum possible response before starting I/O. On failure
        # its size is unknown, so the reservation remains charged.
        byte_count += allowance
        requests += 1
        record = {"phase": phase, "source_id": source_id, "url": url, "code": code,
                  "outcome": "failed", "reserved_bytes": allowance, "charged_bytes": allowance,
                  "raw_ref": None, "error": None}
        io_records.append(record)
        timeout = min(budget["timeout_seconds"], max(.001, budget["max_elapsed_seconds"]-(time.monotonic()-started)))
        return record, timeout

    def accept(io_record, source):
        nonlocal byte_count
        size = source["capture"]["bytes"]
        require(0 < size <= io_record["reserved_bytes"], "Response exceeded its reserved byte allowance")
        byte_count -= io_record["reserved_bytes"]-size
        io_record.update(outcome="captured", charged_bytes=size, raw_ref=source["raw_ref"])

    def fetch_profile(code, phase):
        if code in profiles or code in attempted or len(attempted) >= budget["max_profile_requests"]:
            return
        url = "https://fundf10.eastmoney.com/jbgk_"+code+".html"
        reserved = reserve(phase, "eastmoney_profile", url, code)
        if reserved is None:
            return
        io_record, timeout = reserved
        attempted.append(code)
        try:
            record = _capture(url, "eastmoney_profile", artifacts, timeout, io_record["reserved_bytes"],
                              store=store, context=context)
            accept(io_record, record)
            profiles[code] = record
        except (OSError, ValueError) as error:
            io_record["error"] = str(error)
            failures.append(_review_task(code, error))

    def fetch_documents(codes, phase):
        for mapping in mappings:
            code, key = mapping["code"], fingerprint(mapping)
            if code not in codes or key in issuer_sources or code not in profiles:
                continue
            cache_key = fingerprint([mapping["source_id"], mapping["url"]])
            if cache_key in document_cache:
                issuer_sources[key] = document_cache[cache_key]
                continue
            # A disclosed but unsupported dealing code does not consume issuer
            # document budget; the resolver preserves and rechecks its refusal.
            try:
                parse_profile(capture_text(profiles[code], artifacts), code, entries[code])
            except ValueError:
                continue
            require(source_fetch.source_rule(mapping["source_id"])["purpose"] == "issuer_document",
                    "Original issuer document source required")
            reserved = reserve(phase, mapping["source_id"], mapping["url"], code)
            if reserved is None:
                failures.append(_review_task(code, "Issuer document pending after the declared source/byte/time budget"))
                continue
            io_record, timeout = reserved
            try:
                reference = source_documents.capture_document(mapping["url"], mapping["source_id"], artifacts,
                    timeout=timeout, max_bytes=io_record["reserved_bytes"], store=store, context=context)
                document = artifacts.read_json(reference)
                accept(io_record, {"capture": document["capture"], "raw_ref": document["raw_ref"]})
                issuer_sources[key] = document_cache[cache_key] = reference
            except (OSError, ValueError) as error:
                io_record["error"] = str(error)
                failures.append(_review_task(code, error))

    automatic = []
    automatic_seen = set()
    announcement_traces, announcement_seen, announcement_cache = [], set(), {}
    announcement_deadline = time.monotonic()+min(60, budget["max_elapsed_seconds"])
    announcement_requests = 0
    def capture_announcement(url, source_id, code):
        nonlocal announcement_requests
        key = fingerprint([source_id, url])
        if key in announcement_cache:
            return announcement_cache[key]
        require(announcement_requests < 128 and time.monotonic() < announcement_deadline,
                "Announcement discovery sub-budget exhausted")
        reserved = reserve("announcement_discovery", source_id, url, code)
        require(reserved is not None, "Declared discovery source/byte/time budget exhausted")
        announcement_requests += 1
        io_record, timeout = reserved
        try:
            record = _capture(url, source_id, artifacts, min(timeout, max(.001, announcement_deadline-time.monotonic())),
                io_record["reserved_bytes"], store=store, context=context)
            accept(io_record, record)
            announcement_cache[key] = record
            return record
        except (OSError, ValueError) as error:
            io_record["error"] = str(error)
            raise

    def fetch_announcement_links(code, profile):
        if code in announcement_seen:
            return []
        announcement_seen.add(code)
        origin = _announcement_menu(code, profile, artifacts)
        if origin is None:
            return []
        trace = {"code": code, "origin": origin, "menu": None, "script": None, "api": None, "failure": None}
        announcement_traces.append(trace)
        try:
            trace["menu"] = capture_announcement(origin["url"], "eastmoney_announcements_page", code)
            script_url = _announcement_script(trace["menu"], artifacts)
            trace["script"] = capture_announcement(script_url, "eastmoney_announcements_script", code)
            recipe = _announcement_recipe(code, trace["menu"], trace["script"], artifacts)
            trace["api"] = capture_announcement(recipe["api_url"], "eastmoney_announcements_api", code)
            return _announcement_links(trace, profile, artifacts)
        except (OSError, ValueError) as error:
            trace["failure"] = _issuer_failure(error)
            return []

    def fetch_automatic_documents(codes):
        nonlocal announcement_requests
        auto_profiles, base_identities = {}, {}
        for code in sorted(set(codes)&set(profiles)):
            try:
                base_identities[code] = parse_profile(capture_text(profiles[code], artifacts), code, entries[code])
            except ValueError:
                continue
            auto_profiles[code] = profiles[code]
        links = _automatic_issuer_links(auto_profiles, artifacts)
        for code, profile in sorted(auto_profiles.items()):
            links.extend(fetch_announcement_links(code, profile))
        for link in links:
            link_key = fingerprint(link)
            if link_key in automatic_seen:
                continue
            automatic_seen.add(link_key)
            record = {**link, "document_ref": None, "mappings": [], "rejections": [], "failure": None}
            key = fingerprint([link["source_id"], link["url"]])
            reference = document_cache.get(key)
            if reference is None:
                notice_pdf = link["source_id"] == "issuer_eastmoney_pdf"
                allowed = not notice_pdf or announcement_requests < 128 and time.monotonic() < announcement_deadline
                reserved = reserve("automatic_issuer", link["source_id"], link["url"], link["code"]) if allowed else None
                if reserved is None:
                    record["failure"] = {"kind": "capture_pending", "message": "Declared source, byte or elapsed budget exhausted"}
                else:
                    io_record, timeout = reserved
                    if notice_pdf:
                        announcement_requests += 1
                        timeout = min(timeout, max(.001, announcement_deadline-time.monotonic()))
                    try:
                        reference = source_documents.capture_document(link["url"], link["source_id"], artifacts,
                            timeout=timeout, max_bytes=io_record["reserved_bytes"], store=store, context=context)
                        metadata = artifacts.read_json(reference)
                        accept(io_record, {"capture": metadata["capture"], "raw_ref": metadata["raw_ref"]})
                        document_cache[key] = reference
                    except (OSError, ValueError) as error:
                        io_record["error"] = str(error)
                        record["failure"] = {"kind": "capture_unavailable", "message": str(error)}
            if reference is not None:
                record["document_ref"] = reference
                try:
                    document = source_documents.read_extracted_document(reference, artifacts)
                    interpretation = _automatic_issuer_mappings(document, link["code"], base_identities[link["code"]])
                except (ValueError, KeyError) as error:
                    record["failure"] = _issuer_failure(error)
                else:
                    record.update(interpretation)
                    for mapping in record["mappings"]:
                        identity = fingerprint(mapping)
                        issuer_sources[identity] = reference
                        if identity not in {fingerprint(item) for item in mappings}:
                            mappings.append(mapping)
            automatic.append(record)

    # Scheduling protects mandatory source evidence; it is never a return rank.
    for code in sorted(mandatory):
        fetch_profile(code, "required_identity")
    fetch_documents(mandatory, "required_issuer")
    fetch_automatic_documents(mandatory)
    for code in sorted(declared-mandatory):
        fetch_profile(code, "declared_group_identity")
    fetch_documents(declared, "declared_group_issuer")
    fetch_automatic_documents(declared)
    exploration = scope-mandatory-declared-set(profiles)
    # Failed exploration is retained, but does not monopolize every continuation.
    for code in sorted(exploration-previously_attempted)+sorted(exploration & previously_attempted):
        fetch_profile(code, "remaining_exploration")
    fetch_automatic_documents(profiles)
    requested = sorted(set(profiles) | set(held) | set(monitoring))
    effective_mappings = [item for item in mappings if item["code"] in profiles]
    identity_ref = resolve_identity(requested, catalog_ref, artifacts, store=store, context=context,
        timeout=budget["timeout_seconds"], sources=profiles, issuer_disclosures=effective_mappings,
        issuer_sources=issuer_sources, fetch_missing=False, automatic_issuer_discovery=automatic,
        announcement_discovery=announcement_traces)
    snapshot = artifacts.read_json(identity_ref)
    identities = verify_identity(snapshot, artifacts, as_of=utc_now(), max_age_seconds=budget["max_age_seconds"])
    derived = _derive_selection(entries, identities, held, monitoring, theses, universe_policy, budget, plan_constraints)
    pending_scopes_ref = artifacts.put_json(derived.pop("pending_scopes"))
    observations = list(derived["exposure_required_actions"])
    for code, rows in derived["exposures"].items():
        if not rows and code not in held and code not in monitoring:
            observations.append(_review_task(code, "Name match is not source-supported exposure; retrieve holdings or tracked asset evidence"))
        elif len({row["direction"] for row in rows}) > 1:
            observations.append({"code": code, "action": "resolve_industry_direction_conflict"})
    observed = utc_now()
    result = {"schema_version": 4, **derived, "request_hash": fingerprint(request), "request": request,
        "catalog_ref": catalog_ref, "identity_snapshot_ref": identity_ref, "pending_scopes_ref": pending_scopes_ref, "held_codes": held,
        "monitoring_codes": monitoring, "monitor_codes": sorted(set(held)|set(monitoring)),
        "news_review_hash": fingerprint(news_review), "news_state_hash": fingerprint(news_state),
        "industry_theses_hash": fingerprint(theses), "universe_policy": copy.deepcopy(universe_policy),
        "plan_constraints_hash": fingerprint(plan_constraints), "policy": budget, "observed_at": observed,
        "valid_until": (dt.datetime.fromisoformat(observed)+dt.timedelta(seconds=budget["max_age_seconds"])).isoformat(),
        "required_actions": failures+snapshot["required_actions"]+observations, "attempted_codes": attempted,
        "coverage": {"catalog_count": len(entries), "scope_codes": sorted(scope),
            "unexamined_codes": sorted(scope-set(profiles)), "reused_profiles": len(profiles)-len([code for code in attempted if code in profiles]),
            "rejected_profile_codes": sorted(snapshot["rejected_profiles"]),
            "identities_verified": len(identities), "source_requests": requests, "source_byte_budget_charged": byte_count,
            "io_records": io_records, "attempted_history": sorted(previously_attempted | set(attempted)),
            "directory_content_hash": fingerprint(entries),
            "scheduling_policy": ["required_identity_and_issuer", "declared_group_evidence", "remaining_exploration", "actual_profile_issuer_links"],
            "elapsed_seconds": time.monotonic()-started, "scope": "complete_declared_groups_with_explicit_pending_catalog_scope",
            "ordering_is_rank": False, "all_market_optimality_claimed": False}}
    store.assert_owned(context)
    result["candidate_ref"] = artifacts.put_json(result)
    store.put("candidate-set", operation_id, {"request_hash": result["request_hash"], "candidate_ref": result["candidate_ref"]})
    return result


def validate_discovery(result, news_review, held_codes, artifacts, as_of, *, monitoring_codes=(), news_state, plan_constraints):
    require(result.get("schema_version") == 4, "Current candidate-set schema required")
    fund_screen.require_current_plan(result, plan_constraints)
    require(artifacts.read_json(result["candidate_ref"]) == {key: value for key, value in result.items() if key != "candidate_ref"},
            "Candidate set differs from its sealed artifact")
    require(result["request_hash"] == fingerprint(result["request"]), "Discovery request binding changed")
    held, monitoring = sorted({_code(code) for code in held_codes}), sorted({_code(code) for code in monitoring_codes})
    require(result["held_codes"] == held and result["monitoring_codes"] == monitoring, "Discovery roles changed")
    require(result["news_review_hash"] == fingerprint(news_review) and result["news_state_hash"] == fingerprint(news_state),
            "News evidence or current event lifecycle changed; rebuild discovery")
    budget = _policy(result["policy"])
    observed, boundary = dt.datetime.fromisoformat(result["observed_at"]), dt.datetime.fromisoformat(as_of)
    require(0 <= (boundary-observed).total_seconds() <= budget["max_age_seconds"], "Discovery is future or stale")
    require(result["valid_until"] == (observed+dt.timedelta(seconds=budget["max_age_seconds"])).isoformat(), "Discovery expiry changed")
    snapshot = artifacts.read_json(result["identity_snapshot_ref"])
    require(snapshot["catalog_ref"] == result["catalog_ref"], "Identity catalogue differs")
    inherited_mappings = []
    if result["request"]["continue_from"] is not None:
        previous = artifacts.read_json(result["request"]["continue_from"])
        previous_identity = artifacts.read_json(previous["identity_snapshot_ref"])
        inherited_mappings = previous_identity["issuer_requests"]
    mappings = list({fingerprint(row): row for row in inherited_mappings+result["request"]["issuer_disclosures"]}.values())
    captured_codes = set(snapshot["profiles"]) | set(snapshot["rejected_profiles"])
    mapping_keys = {fingerprint(row) for row in mappings}
    for source in snapshot["automatic_issuer_discovery"]:
        for row in source["mappings"]:
            if fingerprint(row) not in mapping_keys:
                mappings.append(row)
                mapping_keys.add(fingerprint(row))
    require(snapshot["issuer_requests"] == [row for row in mappings if row["code"] in captured_codes],
            "Issuer request set differs from the bound discovery; rejected evidence cannot be erased")
    identities = verify_identity(snapshot, artifacts, as_of=as_of, max_age_seconds=budget["max_age_seconds"])
    require(snapshot["automatic_issuer_scope"] == "budgeted_profile_href_links", "Discovery omitted automatic actual-link source review")
    entries = artifacts.read_json(result["catalog_ref"])["entries"]
    theses = [row for row in _theses(news_review) if row["thesis_id"] in news_state["active_thesis_ids"]]
    require(result["industry_theses_hash"] == fingerprint(theses), "Effective thesis set changed")
    expected = _derive_selection(entries, identities, held, monitoring, theses, result["universe_policy"], budget,
                                 result["request"]["plan_constraints"])
    require(artifacts.read_json(result["pending_scopes_ref"]) == expected.pop("pending_scopes"),
            "Shared pending comparison scopes differ from the complete captured directory")
    require(all(result[key] == value for key, value in expected.items()), "Group coverage or source-qualified admission changed")
    required = list(snapshot["required_actions"]) + expected["exposure_required_actions"]
    for code, rows in expected["exposures"].items():
        if not rows and code not in held and code not in monitoring:
            required.append(_review_task(code, "Name match is not source-supported exposure; retrieve holdings or tracked asset evidence"))
        elif len({row["direction"] for row in rows}) > 1:
            required.append({"code": code, "action": "resolve_industry_direction_conflict"})
    require({fingerprint(row) for row in required} <= {fingerprint(row) for row in result["required_actions"]},
            "Preserve required source research actions")
    require(result["plan_constraints_hash"] == fingerprint(result["request"]["plan_constraints"]), "Plan binding changed")
    require(result["request"]["universe_policy"] == result["universe_policy"] and result["request"]["policy"] == result["policy"],
            "Discovery rules differ from its bound request")
    seed = {code for code, entry in entries.items() if any(
        term in entry["name"] or term in entry["type"] for thesis in theses for term in thesis["search_terms"])}
    seed |= set(held) | set(monitoring) | {row["code"] for row in mappings}
    categories = {entries[code]["type"] for code in seed if code in entries}
    scope = seed | {code for code, row in entries.items() if row["type"] in categories or not row["type"]}
    coverage, attempted = result["coverage"], result["attempted_codes"]
    require(len(set(attempted)) == len(attempted) and set(attempted) <= scope
            and len(attempted) <= budget["max_profile_requests"], "Discovery attempt budget or scope differs")
    captured_codes = set(snapshot["profiles"]) | set(snapshot["rejected_profiles"])
    require(coverage["catalog_count"] == len(entries) and coverage["scope_codes"] == sorted(scope)
            and coverage["unexamined_codes"] == sorted(scope-captured_codes)
            and coverage["rejected_profile_codes"] == sorted(snapshot["rejected_profiles"])
            and coverage["identities_verified"] == len(identities)
            and coverage["reused_profiles"] == len(captured_codes)-len(set(attempted)&captured_codes),
            "Discovery coverage counts differ from its captured directory and profiles")
    require(type(coverage["source_requests"]) is int and 1+len(attempted) <= coverage["source_requests"] <= budget["max_source_requests"]
            and type(coverage["source_byte_budget_charged"]) is int
            and 0 < coverage["source_byte_budget_charged"] <= budget["max_total_bytes"]
            and coverage["ordering_is_rank"] is False and coverage["all_market_optimality_claimed"] is False,
            "Discovery resource accounting or selection qualification changed")
    history = set(attempted)
    if result["request"]["continue_from"] is not None:
        history |= set(previous["coverage"]["attempted_history"])
    require(coverage["attempted_history"] == sorted(history) and coverage["directory_content_hash"] == fingerprint(entries),
            "Coverage continuation history or directory identity changed")
    records = coverage["io_records"]
    require(type(records) is list and len(records) == coverage["source_requests"]
            and sum(row["charged_bytes"] for row in records) == coverage["source_byte_budget_charged"],
            "Source budget ledger differs from attempted captures")
    require([row["code"] for row in records if row["source_id"] == "eastmoney_profile"] == attempted,
            "Profile scheduling differs from source attempts")
    for row in records:
        require(0 < row["charged_bytes"] <= row["reserved_bytes"] <= source_fetch.MAX_BYTES,
                "Source reservation is not bounded")
        if row["outcome"] == "captured":
            require(row["error"] is None and len(artifacts.read(row["raw_ref"])) == row["charged_bytes"],
                    "Successful source charge differs from its actual bytes")
        else:
            require(row["outcome"] == "failed" and bool(row["error"]) and row["raw_ref"] is None
                    and row["charged_bytes"] == row["reserved_bytes"], "Failed source lost its conservative reservation")
    require(coverage["scheduling_policy"] == ["required_identity_and_issuer", "declared_group_evidence", "remaining_exploration", "actual_profile_issuer_links"],
            "Discovery scheduling must not become an undeclared financial rank")
    return {"status": result["status"], "codes": result["codes"], "eligible_buy_codes": [],
            "identity_snapshot_ref": result["identity_snapshot_ref"]}
