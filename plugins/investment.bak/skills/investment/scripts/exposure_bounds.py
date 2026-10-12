"""Original-contract bounds for cash securities industry assets / fund NAV.

Gross exposure can exceed one. Missing upper bounds stay None; these bounds do
not cover derivative notional, delta, guarantees, or future contract compliance.
No historical holdings table, fund name, or analyst inference tightens bounds.

Supported original clauses: a product-bound total assets/NAV cap, and an
explicit named-industry cash securities/NAV minimum or maximum. Different
denominators and unsupported layouts remain unqualified. Official industry
definitions reuse the primary-index proof adapter; no taxonomy alias map.
"""
import copy
import re
from decimal import Decimal

from contracts import fields, fingerprint, instant, require
import fund_quality
import industry_data
import source_fetch

SCOPE = "cash_securities_industry_gross_assets_over_fund_NAV"


def _unknown():
    return {"lower": "0", "upper": None, "scope": SCOPE, "status": "unknown", "evidence": []}


def _original(source, artifacts, at):
    document = fund_quality._document(source, artifacts, at)
    require(source_fetch.source_rule(document["source_id"])["purpose"] == "issuer_document",
            "Exposure constraints require original fund issuer documents")
    return document


def _texts(document, locators):
    require(type(locators) is list and locators and len(set(locators)) == len(locators), "Distinct original exposure locators required")
    rows = {row["locator"]: row for locator in locators for row in fund_quality._blocks(document, locator)}
    return list(rows.values())


def _subject(document, locators, identity):
    rows = _texts(document, locators)
    values = fund_quality._labels(rows)
    codes = {value for key, value in values.items() if key in ("基金代码", "基金编码", "Fund Code")}
    names = {value for key, value in values.items() if key in ("基金全称", "基金名称", "Fund Name")}
    # Exact primary code or exact declared legal name; share suffix removal
    # would invent a legal relationship between different fund products.
    if identity["code"] in codes or (identity.get("legal_name") is not None and identity["legal_name"] in names):
        return True
    legal = identity.get("legal_name")
    if not legal:
        return False
    title = re.escape(re.sub(r"\s+", "", legal))
    company = identity.get("company_legal_name") or identity.get("company_name")
    prefix = "(?:"+re.escape(re.sub(r"\s+", "", company))+")?" if company else ""
    return any(re.match(r"^"+prefix+title+r"(?:基金合同|更新的招募说明书|招募说明书)(?:[（(].*)?", re.sub(r"\s+", "", row["text"]))
               is not None for row in rows if row.get("kind") in ("pdf_page", "container_text", "p", "heading"))


def _number(text):
    require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) is not None, "Invalid original exposure percentage")
    value = Decimal(text)/100
    require(value.is_finite() and value >= 0, "Exposure bounds must be finite nonnegative numbers")
    return value


def _global_cap(text):
    compact = re.sub(r"\s+", "", text)
    # The grammatical subject is this fund. Regulatory examples about another
    # product, historical measurements, and derivative exposure are not caps.
    match = re.fullmatch(r"(?:[（(][0-9]+[）)]|[0-9]+[、.])?本基金(?:的)?(?:资产总值|总资产)(?:不得超过|不超过)(?:基金)?(?:资产净值|净资产)(?:的)?([0-9]+(?:\.[0-9]+)?)%[。；;.]?", compact)
    if not match:
        return None
    value = _number(match[1])
    require(value >= 1, "Total assets/NAV source cap contradicts nonnegative liabilities domain")
    return value


def _sector_clause(text, sector):
    compact = re.sub(r"\s+", "", text)
    subject = r"(?:本基金)?投资于"+re.escape(sector)+r"行业(?:的)?(?:股票|证券|证券资产)"
    basis = r"(?:占基金资产净值(?:的)?(?:比例)?|(?:的)?比例(?:为)?基金资产净值(?:的)?)"
    prefix = r"(?:[（(][0-9]+[）)]|[0-9]+[、.])?"+subject+basis
    for pattern, interpretation in ((prefix+r"不低于([0-9]+(?:\.[0-9]+)?)%且不超过([0-9]+(?:\.[0-9]+)?)%[。；;.]?", "range"),
                                    (prefix+r"不低于([0-9]+(?:\.[0-9]+)?)%[。；;.]?", "lower"),
                                    (prefix+r"(?:不超过|不得超过)([0-9]+(?:\.[0-9]+)?)%[。；;.]?", "upper")):
        match = re.fullmatch(pattern, compact)
        if match:
            values = [_number(value) for value in match.groups()]
            lower = values[0] if interpretation in ("range", "lower") else Decimal(0)
            upper = values[-1] if interpretation in ("range", "upper") else None
            require(upper is None or lower <= upper, "Source industry interval contradicts itself")
            return lower, upper
    return None


def _industry(contract, artifacts, at):
    definition = contract["sector"]
    fields(definition, {"sector_id", "benchmark_contract"}, label="formal exposure industry")
    benchmark = industry_data._contract(copy.deepcopy(definition["benchmark_contract"]))
    require(benchmark["sector_id"] == definition["sector_id"] and benchmark["benchmark_role"] == "sector_index",
            "Exposure industry differs from official sector index subject")
    document = industry_data._identity(benchmark, artifacts, at)
    relation = industry_data._sector_relation(benchmark, document)
    return definition["sector_id"], relation


def _merge(target, lower, upper, proof):
    target["lower"] = str(max(Decimal(target["lower"]), lower))
    if upper is not None:
        target["upper"] = str(upper if target["upper"] is None else min(Decimal(target["upper"]), upper))
    require(target["upper"] is None or Decimal(target["lower"]) <= Decimal(target["upper"]), "Conflicting current exposure constraints")
    target["status"] = "source_bound"
    target["evidence"].append(proof)


def assemble(contracts, identities, artifacts, decision_at):
    require(type(contracts) is list and len(contracts) <= 200 and type(identities) is dict, "Bounded source exposure contracts and identities required")
    products = {}
    for code, identity in sorted(identities.items()):
        require(identity.get("code") == code, "Exposure identity differs from its keyed fund code")
        products[code] = {"code": code, "default_bound": _unknown(), "sectors": {}, "required_actions": []}
    for contract in contracts:
        fields(contract, {"code", "document_ref", "subject_locators", "constraint_locators"}, {"sector"}, "exposure source contract")
        code = contract["code"]
        require(code in identities, "Exposure source contract refers to an unknown fund")
        product = products[code]
        document = _original(contract["document_ref"], artifacts, decision_at)
        if not _subject(document, contract["subject_locators"], identities[code]):
            product["required_actions"].append({"action": "obtain_same_primary_product_constraint_source", "code": code})
            continue
        rows = _texts(document, contract["constraint_locators"])
        sector, relation = None, None
        if "sector" in contract:
            sector, relation = _industry(contract, artifacts, decision_at)
            product["sectors"].setdefault(sector, _unknown())
            if relation["status"] != "verified":
                product["required_actions"].append({"action": "obtain_official_single_industry_definition", "sector_id": sector})
        parsed = False
        for row in rows:
            if row["kind"] == "table_cell":
                continue
            # Splitting on complete sentence boundaries preserves the original
            # subject and denominator; a substring inside a negation is refused.
            sentences = [value.strip() for value in re.findall(r"[^。]+(?:。|$)", row["text"]) if value.strip()]
            for sentence in sentences:
                proof = {"document_ref": contract["document_ref"], "document_id": document["document_id"],
                         "raw_sha256": document["raw_sha256"], "locator": row["locator"], "quote": sentence,
                         "known_at": document["retrieved_at"], "scope": "source_stated_current_contract_vintage_not_future_compliance_certificate"}
                cap = _global_cap(sentence)
                if cap is not None:
                    _merge(product["default_bound"], Decimal(0), cap, proof)
                    parsed = True
                interval = _sector_clause(sentence, sector) if sector and relation["status"] == "verified" else None
                if interval is not None:
                    proof["industry_definition"] = relation
                    _merge(product["sectors"][sector], *interval, proof)
                    parsed = True
        if not parsed:
            product["required_actions"].append({"action": "obtain_explicit_cash_securities_gross_NAV_clause", "code": code,
                "reason": "historical_weight_or_non_NAV_or_index_link_or_derivative_or_unknown_grammar_not_a_verified_current_sector_bound"})
    for product in products.values():
        for bound in product["sectors"].values():
            base = product["default_bound"]
            if base["upper"] is not None:
                _merge(bound, Decimal(0), Decimal(base["upper"]), {"derived_from_total_asset_cap": copy.deepcopy(base["evidence"]),
                    "derivation": "nonnegative_cash_securities_industry_subset_is_no_larger_than_total_assets"})
        if product["default_bound"]["upper"] is None:
            product["required_actions"].append({"action": "obtain_reliable_finite_gross_asset_upper_bound", "code": product["code"]})
    result = {"schema_version": 4, "kind": "sector_exposure_bounds", "decision_at": decision_at,
              "contracts_hash": fingerprint(contracts), "identities_hash": fingerprint(identities), "products": products,
              "scope": SCOPE, "upper_unknown_representation": None,
              "status": "partial" if any(row["required_actions"] for row in products.values()) else "source_bound",
              "assumptions": ["cash_securities_asset_values_nonnegative", "stated_source_contract_constraints_obeyed"],
              "excluded_risk_measures": ["derivative_notional", "derivative_delta", "indirect_fund_lookthrough_without_two_source_layers"]}
    result["bounds_hash"] = fingerprint(result)
    return result


def validate(value, contracts, identities, artifacts, decision_at):
    expected = assemble(contracts, identities, artifacts, decision_at)
    require(value == expected, "Exposure bounds differ from original-source reconstruction")
    return {"status": "passed", "scope": SCOPE, "bounds_hash": expected["bounds_hash"],
            "readiness": {"trade_ready": False}, "checks": ["primary_product_binding", "original_byte_reextraction",
                "gross_NAV_denominator", "no_historical_weight_or_name_as_current_bound", "no_unit_upper_clamp"]}
