"""Source-identified public asset inputs; predictions are fund covariates only."""
import csv
import datetime as dt
import io
import math
import re
import urllib.parse
from zoneinfo import ZoneInfo
from contracts import fields, instant, require, fingerprint
import source_documents
import source_fetch

INPUT_SCHEMA_ID = "source-news-asset-en-v2"
SERIES_ADAPTER_ID = "public_source_series_csv_v2"
DOMAIN_KINDS = ("equity", "bond", "commodity", "foreign", "mixed", "fof")
ROLES = ("single_sector", "multi_sector", "theme", "market", "rate", "duration", "credit", "commodity", "fx", "hedge", "lookthrough", "style")
TRANSFORMS = ("price_log_return", "level_change")
UNIT_SCALE = {"percent": .01, "basis_point": .0001, "decimal_rate": 1., "index_point": 1.,
              "CNY": 1., "USD": 1., "EUR": 1., "JPY": 1., "USD_per_CNY": 1., "CNY_per_USD": 1.,
              "years": 1., "tonnes": 1., "barrels": 1., "USD_per_barrel": 1., "USD_per_troy_ounce": 1., "CNY_per_gram": 1.}
UNIT_CUES = {
    "percent": ("percent", "percentage", "百分", "%", "％"), "basis_point": ("basis point", "基点"),
    "decimal_rate": ("decimal", "ratio", "比率"), "index_point": ("index", "指数", "points", "点"),
    "CNY": ("人民币", "CNY", "RMB", "Yuan Renminbi", "Chinese Yuan"), "USD": ("U.S. Dollars", "US Dollars", "USD", "美元"),
    "EUR": ("Euros", "EUR", "欧元"), "JPY": ("Japanese Yen", "JPY", "日元"),
    "CNY_per_USD": ("yuan to one u.s. dollar", "yuan renminbi to one u.s. dollar", "yuan per u.s. dollar", "cny per usd", "人民币元/美元"),
    "USD_per_CNY": ("u.s. dollars per yuan", "usd per cny", "美元/人民币元"),
    "years": ("years", "年"), "tonnes": ("tonnes", "tons", "吨"), "barrels": ("barrels", "桶"),
    "USD_per_barrel": ("dollars per barrel", "美元/桶"), "USD_per_troy_ounce": ("dollars per troy ounce", "美元/盎司"),
    "CNY_per_gram": ("人民币元/克", "cny per gram"),
}
ROLE_CUES = {
    "single_sector": ("行业", "sector", "industry"), "multi_sector": ("多行业", "各行业", "sectors", "industries", "diversified"),
    "theme": ("主题", "thematic", "theme"), "market": ("市场", "指数", "market", "index", "equity"),
    "rate": ("利率", "收益率", "yield", "interest rate"), "duration": ("久期", "duration"),
    "credit": ("信用", "利差", "credit", "spread"), "commodity": ("黄金", "商品", "原油", "gold", "commodity", "oil"),
    "fx": ("汇率", "exchange", "yuan to", "dollars to"), "hedge": ("对冲", "hedge", "hedging"),
    "lookthrough": ("持仓", "穿透", "holdings", "underlying fund"), "style": ("风格", "因子", "style", "factor"),
}

# Denomination belongs to the original observation, not the fund's CNY ledger.
# Unit scaling below changes percent to a decimal rate; it never converts FX.
UNIT_DENOMINATION = {"percent": "NONE", "basis_point": "NONE", "decimal_rate": "NONE",
    "years": "NONE", "tonnes": "NONE", "barrels": "NONE", "CNY": "CNY", "USD": "USD",
    "EUR": "EUR", "JPY": "JPY", "USD_per_barrel": "USD", "USD_per_troy_ounce": "USD",
    "CNY_per_gram": "CNY", "CNY_per_USD": "CNY", "USD_per_CNY": "USD"}


def verify_series_identity(contract, document):
    """Reproduce declared denomination and quotation direction from the source."""
    validate_series_contract(contract)
    target = contract["asset_domain"]["return_target"]
    _quoted(document, target["locator"], target["quote"])
    definition = (target["quote"]+" "+contract["identity_quote"]).casefold()
    require(any(cue.casefold() in definition for cue in UNIT_CUES[target["source_unit"]]),
            "Asset source unit or FX quotation direction lacks original identity/definition support")
    currency = contract["currency"]
    if currency != "NONE":
        explicit = any(cue.casefold() in definition for cue in UNIT_CUES.get(currency, (currency,)))
        # EIA crude-oil quotations use nominal US dollars. Require original
        # agency/commodity metadata; a FRED hostname alone proves no currency.
        original = " ".join(row["text"] for row in document["blocks"]).casefold()
        fred_eia_dollars = (currency == "USD" and target["source_unit"] == "USD_per_barrel"
                        and urllib.parse.urlsplit(document["capture"]["requested_url"]).hostname == "fred.stlouisfed.org"
                        and "u.s. energy information administration" in original and "crude oil" in original
                        and re.search(r"\bunits?\s*:\s*dollars per barrel\b", target["quote"].casefold()))
        require(explicit or fred_eia_dollars, "Declared series denomination lacks original identity support")
    return {"currency": currency, "source_unit": target["source_unit"], "canonical_unit": target["canonical_unit"]}


def validate_domain(domain):
    fields(domain, {"kind", "role", "return_target"}, {"taxonomy"}, "asset input domain")
    require(domain["kind"] in DOMAIN_KINDS and domain["role"] in ROLES, "Unregistered asset input domain or role")
    target = domain["return_target"]
    fields(target, {"transform", "economic_meaning", "source_unit", "canonical_unit", "locator", "quote"}, label="asset source return target")
    require(target["transform"] in TRANSFORMS and target["source_unit"] in UNIT_SCALE,
            "Asset target transform or source unit needs a registered parser")
    require(all(type(target[key]) is str and target[key].strip() for key in ("economic_meaning", "canonical_unit", "locator", "quote")),
            "Asset target economic meaning needs an original source locator and quote")
    if target["source_unit"] in ("percent", "basis_point"):
        require(target["canonical_unit"] == "decimal_rate" and target["transform"] == "level_change",
                "A yield/spread level is not an asset price or total return")
    else:
        require(target["canonical_unit"] == target["source_unit"], "Unregistered asset unit conversion")
    if "taxonomy" in domain:
        taxonomy = domain["taxonomy"]
        fields(taxonomy, {"id", "version", "locator", "quote", "mappings"}, {"document_ref"}, "source taxonomy")
        require(type(taxonomy["mappings"]) is list and taxonomy["mappings"], "Nonempty original classification mappings required")
        seen = set()
        for mapping in taxonomy["mappings"]:
            fields(mapping, {"source_label", "entity_id", "role"}, label="source classification mapping")
            require(mapping["role"] in ROLES and all(type(mapping[key]) is str and mapping[key].strip()
                    for key in ("source_label", "entity_id")), "Source taxonomy mapping is invalid")
            identity = (mapping["source_label"], mapping["entity_id"], mapping["role"])
            require(identity not in seen, "Duplicate taxonomy mapping")
            seen.add(identity)
    return domain


def _quoted(document, locator, quote):
    block = next((row for row in document["blocks"] if row["locator"] == locator), None)
    require(block is not None and source_documents.normalize_text(quote) in source_documents.normalize_text(block["text"]),
            "Asset definition differs from its original source block")
    return block


def verify_definition(contract, document, artifacts, at):
    domain = validate_domain(contract["asset_domain"])
    target = domain["return_target"]
    definition_block = _quoted(document, target["locator"], target["quote"])
    definition = (target["quote"]+" "+contract["identity_quote"]).casefold()
    require(any(cue.casefold() in definition for cue in ROLE_CUES[domain["role"]]),
            "Declared asset role has no original identity/definition language")
    if domain["role"] == "single_sector":
        original = source_documents.normalize_text(definition_block["text"])
        require(not re.search(r"行业(?:权重)?分布|十大权重股|多行业|各行业|不反映|does not|not reflect|sector weights|sector distribution", original, re.I),
                "A distribution table, negation or broad multi-industry definition does not establish a single-sector asset")
    require(source_documents.normalize_text(target["economic_meaning"]) in source_documents.normalize_text(target["quote"]),
            "Return target economic meaning is not literally present in its source definition")
    require(any(cue.casefold() in definition for cue in UNIT_CUES[target["source_unit"]]),
            "Asset source unit or FX quotation direction lacks original identity/definition support")
    taxonomy = domain.get("taxonomy")
    known = document["retrieved_at"]
    if taxonomy is not None:
        proof = source_documents.read_extracted_document(taxonomy["document_ref"], artifacts) if taxonomy.get("document_ref") else document
        require(instant(proof["retrieved_at"]) <= instant(at), "Taxonomy was observed after decision")
        _quoted(proof, taxonomy["locator"], taxonomy["quote"])
        require(taxonomy["id"] in taxonomy["quote"] and taxonomy["version"] in taxonomy["quote"],
                "Taxonomy identity/version lack literal original evidence")
        require(all(row["source_label"] in taxonomy["quote"] for row in taxonomy["mappings"]),
                "Source taxonomy labels are not in the cited original classification")
        known = max((known, proof["retrieved_at"]), key=instant)
    return {"status": "verified", "scope": "source_versioned_asset_definition", "sector_label": contract["sector_id"],
            "known_at": known, "observed_at": known, "document_ref": contract["identity_document_ref"],
            "document_id": document["document_id"], "quote": target["quote"], "locator": target["locator"],
            "adapter_id": SERIES_ADAPTER_ID, "role": domain["role"], "asset_domain": domain,
            "semantic_hash": fingerprint({"sector_id": contract["sector_id"], "asset_domain": domain}), "reason": None}


def target_coordinates(target, base, phase, terminal):
    if target["transform"] == "price_log_return":
        require(min(base, phase, terminal) > 0, "Asset price log return needs positive source levels")
        return [math.log(value/base) for value in (phase, terminal)]
    require(target["transform"] == "level_change", "Unknown asset target transform")
    return [value-base for value in (phase, terminal)]


def validate_series_contract(contract):
    fields(contract, {"sector_id", "thesis_ids", "benchmark_id", "series_id", "series_name", "return_definition", "currency",
        "calendar_id", "timezone", "identity_quote", "start", "end", "benchmark_role", "adapter_id", "quote_source_id",
        "quote_url", "series_columns", "asset_domain"},
        {"identity_document_ref", "identity_url", "identity_source_id", "delimiter"}, "public asset series contract")
    require(contract["adapter_id"] == SERIES_ADAPTER_ID, "Obsolete or unsupported series adapter")
    domain = validate_domain(contract["asset_domain"])
    require(contract["benchmark_role"] in ROLES and contract["benchmark_role"] == domain["role"], "Asset role differs from its source domain")
    require(contract["return_definition"] == domain["return_target"]["transform"], "Asset return definition differs from source transform")
    require(all(type(contract[key]) is str and contract[key].strip() for key in ("sector_id", "benchmark_id", "series_id", "series_name",
         "currency", "calendar_id", "timezone", "identity_quote", "quote_source_id", "quote_url")), "Source identity and explicit clock required")
    ZoneInfo(contract["timezone"])
    require(contract["currency"] in ("CNY", "USD", "EUR", "JPY", "GBP", "HKD", "CAD", "AUD", "CHF", "NONE"), "Unregistered source denomination")
    expected_currency = UNIT_DENOMINATION.get(domain["return_target"]["source_unit"])
    if expected_currency is not None:
        require(contract["currency"] == expected_currency, "Declared series currency conflicts with its original observation unit")
    require(dt.date.fromisoformat(contract["start"]) < dt.date.fromisoformat(contract["end"]), "Asset observation range is reversed")
    fields(contract["series_columns"], {"date", "value"}, label="original series column names")
    require(type(contract["thesis_ids"]) is list and contract["thesis_ids"] and len(set(contract["thesis_ids"])) == len(contract["thesis_ids"]),
            "Asset input must bind assessed source theses")
    require(contract.get("identity_document_ref") or (contract.get("identity_url") and contract.get("identity_source_id")), "Original public identity required")
    return contract


def parse_series_capture(capture, contract):
    source_fetch.validate_capture_provenance(capture)
    require(capture["registry_source_id"] == contract["quote_source_id"] and capture["requested_url"] == contract["quote_url"],
            "Asset input original source or requested identity differs")
    rows = csv.DictReader(io.StringIO(capture["text"]), delimiter=contract.get("delimiter", ","))
    columns = contract["series_columns"]
    require(columns["value"] == contract["series_id"], "Series source value header differs from the declared original identity")
    require(rows.fieldnames is not None and columns["date"] in rows.fieldnames and columns["value"] in rows.fieldnames,
            "Public series source headers differ")
    observed = instant(capture["retrieved_at"])
    result, previous = [], None
    target = contract["asset_domain"]["return_target"]
    for row in rows:
        day = dt.date.fromisoformat(row[columns["date"]])
        require(previous is None or day > previous, "Public series source is duplicated or unordered")
        previous = day
        require(day <= observed.astimezone(ZoneInfo(contract["timezone"])).date(), "Future public observation cannot be captured")
        if not contract["start"] <= day.isoformat() <= contract["end"]:
            continue
        literal = row[columns["value"]]
        if literal in ("", ".", "NA", "N/A"):
            continue
        value = float(literal)*UNIT_SCALE[target["source_unit"]]
        require(math.isfinite(value) and (target["transform"] != "price_log_return" or value > 0), "Invalid public asset level")
        result.append({"date": day.isoformat(), "close": value, "value_text": literal,
            "source_unit": target["source_unit"], "unit": target["canonical_unit"],
            "available_at": capture["retrieved_at"], "source_observed_at": capture["retrieved_at"],
            "system_observed_at": capture["retrieved_at"], "market_public_available_at": None,
            "availability_basis": "first_verified_capture_of_this_asset_version", "return_definition": contract["return_definition"]})
    require(result, "No source observations in the declared asset input range")
    return result
