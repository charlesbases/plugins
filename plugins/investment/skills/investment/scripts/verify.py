"""Publication gates: numerical reproduction is distinct from investment efficacy."""
import hashlib
import math
import re
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from contracts import EvidenceError, fingerprint, strict_json_loads, instant


def code_identity():
    base = Path(__file__).resolve().parent
    files = list(base.glob("*.py")) + list(base.glob("*.json")) + [base / "requirements-model.txt"]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def monitor_codes(account):
    account = account or {}
    return sorted({row["code"] for kind in ("lots", "orders") for row in account.get(kind, {}).values()})


def execution_sources(market_ref, artifacts, *, as_of=None):
    """Re-extract all normalized terms from their original field-bound sources."""
    import fee_contract
    at = as_of or market_ref["observed_at"]
    if market_ref.get("schema_version") != 4 or market_ref.get("terms_basis") != "source_verified":
        raise EvidenceError("Only current source-verified market terms are accepted")
    for term in market_ref["terms"]:
        contract = artifacts.read_json(term["fee_contract_ref"])
        rebuilt = fee_contract.normalize_to_terms(contract, artifacts, as_of=at, contract_ref=term["fee_contract_ref"])
        if rebuilt != term:
            raise EvidenceError("Market terms differ from re-extracted product contract")
    return {"status": "passed", "scope": "field_bound_product_dealing_contracts"}


def reference_checker(artifacts):
    """Create one private validation traversal; later invocations reread bytes."""
    seen = set()
    active = set()

    def walk(item):
        if type(item) is dict:
            if {"sha256", "size"} <= item.keys() and ("path" in item or "chunks" in item):
                key = fingerprint(item)
                if key in seen or key in active:
                    return
                active.add(key)
                try:
                    raw = artifacts.read(item)
                    if item.get("media_type") == "application/json":
                        walk(strict_json_loads(raw.decode("utf-8")))
                    seen.add(key)
                finally:
                    active.remove(key)
                return
            for child in item.values():
                walk(child)
        elif type(item) is list:
            for child in item:
                walk(child)

    return walk


def references(value, artifacts):
    # Public single-value calls never retain validation state across invocations.
    reference_checker(artifacts)(value)


def market_contract_coverage(record, artifacts):
    """Each observed product has either re-extracted terms or an explicit gap."""
    import fee_contract
    market = record["market_ref"]
    execution_sources(market, artifacts)
    complete = [term["code"] for term in market["terms"]]
    gaps = record["provenance"]["term_gaps"]
    missing = [row["code"] for row in gaps]
    if len(set(complete+missing)) != len(complete)+len(missing) or set(complete+missing) != set(market["prices"]):
        raise EvidenceError("Product term coverage is duplicated or incomplete")
    for row in gaps:
        if row["contract_ref"] is None:
            expected = [{"code": row["code"], "action": "capture_and_inspect_current_product_dealing_terms"}]
            if row["kind"] != "contract_not_provided" or row["required_actions"] != expected:
                raise EvidenceError("Missing-contract status was altered")
            continue
        contract = artifacts.read_json(row["contract_ref"])
        if contract["code"] != row["code"] or row["kind"] != "incomplete_source_evidence":
            raise EvidenceError("Source gap belongs to another product")
        try:
            fee_contract.verify(contract, artifacts, as_of=market["observed_at"])
        except fee_contract.EvidenceGap as exc:
            if exc.required_actions != row["required_actions"]:
                raise EvidenceError("Contract gap differs from source re-extraction")
        else:
            raise EvidenceError("A complete product contract was hidden as a gap")
    return {"status": "passed", "verified_contracts": complete, "pending_contracts": missing}


def verify_research_binding(bundle, artifacts, *, store=None):
    """Derive numerical inputs and marks from the independently audited archive."""
    import allocation_runtime
    import fund_universe
    from file_io import read_object

    market = artifacts.read_json(bundle["market_record_ref"])
    provenance = market["provenance"]
    base = artifacts.base
    run, audit, manifest, policy, nav, features = allocation_runtime.load_data(
        base.parent.parent, base, provenance["research_ref"])
    data = artifacts.read_json(bundle["data_ref"])
    expected_data = {"nav": nav, "features": features, "code_info": policy["code_info"]}
    if "quality_source_bundle" in data:
        import fund_quality
        inputs_ref = bundle.get("quality_inputs_ref")
        if inputs_ref is None:
            raise EvidenceError("Quality model data lacks its original source input binding")
        inputs = artifacts.read_json(inputs_ref)
        prepared = artifacts.read_json(inputs["source_bundle_ref"])
        fund_quality.validate_source_quality(prepared, inputs["data_ref"], inputs["contracts"], artifacts,
                                             inputs["decision_at"], inputs.get("policy"))
        source_model_data = artifacts.read_json(inputs["model_data_ref"])
        if data != fund_quality.bind_quality_features(source_model_data, prepared):
            raise EvidenceError("Quality features differ from original source-vintage reconstruction")
        data = source_model_data
    allowed = {"nav", "features", "code_info", "industry", "industry_exposures", "industry_data_ref"}
    if (not set(data) <= allowed or not {"nav", "features", "code_info"} <= set(data)
            or fingerprint({key: data[key] for key in expected_data}) != fingerprint(expected_data)):
        raise EvidenceError("Numerical data differs from independently audited raw research sources")
    extras = set(data)-set(expected_data)
    if extras:
        if extras != {"industry", "industry_exposures", "industry_data_ref"} or store is None:
            raise EvidenceError("Source industry data must include its independently validated bridge")
        import industry_binding
        record_ref = bundle.get("industry_record_ref") or data["industry_data_ref"]["record_ref"]
        if record_ref != data["industry_data_ref"]["record_ref"]:
            raise EvidenceError("Model industry record linkage differs")
        sector_value = artifacts.read_json(record_ref)
        original = artifacts.read_json(sector_value["request_ref"])
        spec = bundle.get("context", {}).get("spec") if bundle.get("context") else original["spec"]
        base_ref = bundle.get("base_data_ref")
        if base_ref is None or artifacts.read_json(base_ref) != expected_data:
            raise EvidenceError("Augmented data lacks its exact audited base artifact")
        industry_binding.validate_model_data(data, {"base_data_ref": base_ref, "industry_record_ref": record_ref,
            "spec": spec, "decision_at": bundle["decision_at"]}, store, artifacts)
    discovery = artifacts.read_json(bundle["discovery_ref"])
    snapshot = artifacts.read_json(discovery["identity_snapshot_ref"])
    identities = fund_universe.verify_identity(snapshot, artifacts, as_of=bundle["decision_at"])
    if (set(nav) != set(discovery["codes"]) or not set(nav) <= set(identities)
            or data["code_info"] != {code: identities[code] for code in nav}):
        raise EvidenceError("Numerical fund identities differ from the source-derived discovery snapshot")
    request = read_object(run / "request.json")
    if request["identity_snapshot_ref"] != discovery["identity_snapshot_ref"]:
        raise EvidenceError("Research request did not bind this discovery identity snapshot")
    if (provenance["dataset_hash"] != manifest["dataset_hash"] or provenance["audit"] != audit
            or fingerprint(market) != bundle["market_id"]):
        raise EvidenceError("Market dataset, audit or market identity differs from its research archive")
    from decimal import Decimal
    marks = {code: sorted(rows, key=lambda row: row["date"])[-1] for code, rows in nav.items()}
    import allocation_market
    known_marks = {code: allocation_market.known_mark(row, market["market_ref"]["observed_at"])
                   for code, row in marks.items()}
    prices = {code: mark["value"] for code, mark in known_marks.items()}
    price_dates = {code: mark["nav_date"] for code, mark in known_marks.items()}
    currencies = {code: identities[code].get("currency") for code in nav}
    coverage = {code: {"start": min(row["date"] for row in rows), "end": max(row["date"] for row in rows)} for code, rows in nav.items()}
    distributions = {code: str(row["distribution_per_share"]) for code, row in marks.items()}
    if (market["market_ref"]["prices"] != prices or market["market_ref"]["price_dates"] != price_dates
            or market["market_ref"].get("known_marks") != known_marks
            or market["market_ref"]["currencies"] != currencies or any(value != "CNY" for value in currencies.values())
            or provenance["price_dates"] != price_dates or provenance["coverage_by_code"] != coverage
            or provenance["distributions_by_code"] != distributions):
        raise EvidenceError("Market marks, currencies or per-asset coverage differ from audited observations")
    names = {row["code"]: row["name_observed_today"] for row in manifest["raw_summaries"]}
    actions = []
    for code, rows in sorted(nav.items()):
        for row in rows:
            if Decimal(str(row["distribution_per_share"])) == 0:
                continue
            detail = row.get("distribution_evidence") or {}
            actions.append({"code": code, "date": row["date"], "ex_date": row["date"],
                "record_date": detail.get("record_date"), "per_share": str(row["distribution_per_share"]),
                "pay_date": detail.get("cash_payment_date"), "rights_rule": detail.get("rights_rule"),
                "dividend_mode": detail.get("dividend_mode"), "currency": detail.get("currency"),
                "distribution_evidence": detail})
    if (provenance["product_names"] != names or provenance["corporate_actions"] != actions
            or provenance["action_inventory"] != manifest["action_inventory"]
            or artifacts.read_json(provenance["nav_ref"]) != nav):
        raise EvidenceError("Market names, cash entitlements or NAV reference differ from source research")
    captures = strict_json_loads((run / "source-manifest.json").read_text(encoding="utf-8"))
    expected_hashes = {row["source_id"]: row["sha256"] for row in captures if row.get("path")}
    copied_sources = [row for row in market["evidence_refs"] if row["role"] == "public_source"]
    copied_hashes = {row["source_id"]: row["artifact"]["sha256"] for row in copied_sources}
    if (len(copied_sources) != len(expected_hashes) or copied_hashes != expected_hashes
            or any(market["market_ref"]["source_hashes"].get(key) != value for key, value in expected_hashes.items())):
        raise EvidenceError("Published market source references differ from the research captures")
    return {"status": "passed", "dataset_hash": manifest["dataset_hash"], "price_dates": price_dates,
            "scope": "raw_research_to_numerical_data_identity_and_marks_reproduced"}


def _verify_source_families(bundle, artifacts, store=None):
    if bundle.get("quality_ref"):
        import fund_quality
        inputs = artifacts.read_json(bundle["quality_inputs_ref"])
        prepared = artifacts.read_json(inputs["source_bundle_ref"])
        fund_quality.validate_source_quality(prepared, inputs["data_ref"], inputs["contracts"], artifacts,
                                             inputs["decision_at"], inputs.get("policy"))
        _check(prepared["current_evaluation"] == artifacts.read_json(bundle["quality_ref"]),
               "Displayed quality differs from original source-vintage bundle")
        if bundle.get("data_ref"):
            _check(artifacts.read_json(bundle["data_ref"]) == fund_quality.bind_quality_features(
                artifacts.read_json(inputs["model_data_ref"]), prepared), "Quality numerical data binding changed")
    if bundle.get("exposure_ref"):
        import exposure_bounds
        inputs = artifacts.read_json(bundle["exposure_inputs_ref"])
        exposure_bounds.validate(artifacts.read_json(bundle["exposure_ref"]), inputs["contracts"], inputs["identities"], artifacts, inputs["decision_at"])
    if not bundle.get("readiness_ref"):
        return
    import candidate_readiness
    inputs = artifacts.read_json(bundle["readiness_inputs_ref"])
    data = artifacts.read_json(bundle["data_ref"])
    _check(inputs["data"] == data and inputs["decision_at"] == bundle["decision_at"], "Readiness differs from original source inputs")
    readiness = artifacts.read_json(bundle["readiness_ref"])
    candidate_readiness.validate_readiness(readiness, inputs)
    if bundle.get("family_results_ref") is None:
        _check(not readiness["candidate_families"], "Qualified families omitted from publication")
        return
    recorded = artifacts.read_json(bundle["family_results_ref"])
    _check(recorded["readiness_ref"] == bundle["readiness_ref"], "Family list changed qualification binding")
    by_id = {row["family_id"]: row for row in readiness["candidate_families"]}
    block = recorded.get("governance_block")
    if block is not None:
        import newtrade_guard
        _check(store is not None and bundle.get("trade_review_inputs") is not None and bundle.get("trade_review_spec") is not None,
               "Governance-blocked research lacks source-bound review inputs")
        authoritative = newtrade_guard.read_inputs(store, artifacts, bundle["account_id"], bundle["trade_review_spec"],
            bundle["decision_at"], decision_id=bundle["decision_id"])
        _check(authoritative == bundle["trade_review_inputs"] and block["scope"] == authoritative["qualification_scope"]
               and not authoritative["qualification_scope"]["eligible"], "Governance blockage is not established by source policy")
        _check(block["families"] == readiness["candidate_families"] and not recorded["results"]
               and bundle.get("context") is None and not bundle.get("calculation_ref")
               and not bundle["orders"].get("orders"), "Governance-blocked source family inventory changed")
        from pipeline import select_family
        _check(recorded["selection"] == select_family([], artifacts), "Blocked research contains a family winner")
        return
    _check(len(recorded["results"]) == len(by_id), "Qualified family inventory changed")
    if recorded["results"]:
        import newtrade_guard
        _check(store is not None and bundle.get("trade_review_spec") is not None,
               "Numerical family inventory lacks source-bound review authority")
        authoritative = newtrade_guard.read_inputs(store, artifacts, bundle["account_id"], bundle["trade_review_spec"],
            bundle["decision_at"], decision_id=bundle["decision_id"])
        _check(authoritative == bundle.get("trade_review_inputs") and authoritative["review_reservation"] is not None,
               "Numerical families differ from the actual reserved review")
    for row in recorded["results"]:
        family = row["family"]
        _check(by_id.get(family["family_id"]) == family, "Family differs from source-qualified set")
        context = artifacts.read_json(row["context_ref"])
        calculation = artifacts.read_json(row["calculation_ref"])
        _check(context["family_id"] == family["family_id"] and context["readiness_ref"] == bundle["readiness_ref"]
               and context["purchase_eligible_codes"] == family["buy_codes"], "Family context has unqualified purchase codes")
        _check(calculation["family_count"] == len(by_id), "Simultaneous family calibration budget differs")
        _check(context["account_hash"] == bundle["account_hash"] and context["decision_at"] == bundle["decision_at"], "Families have different principal/state cutoffs")
        _check(context["spec"] == bundle["trade_review_spec"] and context["account_id"] == bundle["account_id"]
               and context["trade_state"] == authoritative["trade_state"]
               and context["trade_family_review_index"] == authoritative["trade_family_review_index"],
               "Family context differs from owner-bound policy, history or reserved index")
        reproduce(context, data, calculation)
    from pipeline import select_family
    ranked = select_family(recorded["results"], artifacts)
    _check(ranked == recorded["selection"], "Selected family ranking differs from verified financial results")
    from decision_validation import family_certificate
    family_certificate(recorded["results"], artifacts, recorded["selection"])
    selected = ranked["selected_family_id"]
    if selected is not None:
        row = next(row for row in recorded["results"] if row["family"]["family_id"] == selected)
        _check(artifacts.read_json(row["context_ref"]) == bundle["context"] and row["calculation_ref"] == bundle["calculation_ref"], "Publication did not select the frozen qualified family")


def verify_bundle(bundle, artifacts, *, store, check_current_code=True):
    if bundle.get("schema_version") != 4:
        raise EvidenceError("Invalid decision bundle schema")
    body = {k: v for k, v in bundle.items() if k != "bundle_hash"}
    if fingerprint(body) != bundle.get("bundle_hash"):
        raise EvidenceError("Decision bundle content changed")
    if check_current_code and bundle["source_hashes"] != code_identity():
        raise EvidenceError("Decision code identity changed")
    references(bundle, artifacts)
    _verify_source_families(bundle, artifacts, store)
    if bundle.get("analysis_run_id") is not None:
        run = store.get("analysis_run", bundle["analysis_run_id"])
        if (run is None or run["news_policy"] != bundle["news_policy"]
                or run["news_policy_hash"] != fingerprint(bundle["news_policy"])
                or run["account_id"] != bundle["account_id"] or run["free_news_only"] is not True):
            raise EvidenceError("Publication analysis run differs from immutable source policy")
        import news
        raw_collection = bundle["news"].get("status") in {"collected", "partial_collection"}
        if raw_collection:
            _check(bundle.get("context") is None and bundle.get("calculation_ref") is None
                   and not any(bundle["orders"].get(key) for key in ("orders", "waiting", "funding_options")),
                   "Unassessed collection cannot authorize a numerical decision or trades")
            ref = bundle["news"]["manifest_ref"]
            collection = artifacts.read_json(ref)
            _check(collection == {key: value for key, value in bundle["news"].items()
                                  if key not in {"manifest_ref", "reused"}}, "Raw collection differs from its original manifest")
            journal = store.get("news-operation", fingerprint({"operation_id": collection["operation_id"]}))
            _check(journal is not None and journal["manifest_ref"] == ref,
                   "Unassessed collection lacks its immutable producer journal")
        else:
            _check(bundle["news"].get("status") in {"assessed", "awaiting_analysis"}, "Unknown news publication state")
            collection = artifacts.read_json(bundle["news"]["collection_manifest_ref"])
        _, registry = news.collection_policy(collection, store, artifacts)
        if raw_collection:
            at = instant(bundle["decision_at"])
            # This branch only verifies source provenance for a blocked draft.
            # Assessed numerical advice retains the frozen policy age checks.
            oldest = min([instant(collection["cutoff_at"])] +
                         [instant(row["retrieved_at"]) for row in collection["retrievals"]])
            age = max(0., (at-oldest).total_seconds())+1
            if collection["retrievals"]:
                news._verify_collection_bodies(collection, registry, at, at, age, artifacts, store=store)
            else:
                _check(not collection["versions"], "Empty unassessed collection contains article versions")
            news._verify_source_scopes(collection, registry)
            _check(collection["source_group_coverage"] == news._group_coverage(collection["required_source_groups"], collection["source_results"])
                   and collection["fact_verification"] == "not_performed"
                   and collection["coverage_complete_for_source_time_window"] is False,
                   "Unassessed collection cannot manufacture verified facts or coverage")
        if (collection.get("run_id") != run["run_id"] or bundle["news"].get("run_id") != run["run_id"]
                or collection["cutoff_at"] != run["publish_cutoff"]):
            raise EvidenceError("Publication news was not freshly bound to its analysis run")
    if bundle.get("industry_record_ref") is not None:
        import industry_data
        sector_value = artifacts.read_json(bundle["industry_record_ref"])
        original = artifacts.read_json(sector_value["request_ref"])
        context_spec = bundle.get("context", {}).get("spec") if bundle.get("context") else original["spec"]
        sector = industry_data.validate_sector_data(sector_value, context_spec, bundle["decision_at"], artifacts, store=store)
        if {key: row for key, row in original["review"].items() if key != "manifest_ref"} != {
                key: row for key, row in bundle["news"].items() if key != "manifest_ref"}:
            raise EvidenceError("Industry source review differs from publication news")
        if sector["news_ref"] != bundle["news"].get("manifest_ref"):
            raise EvidenceError("Industry raw review artifact differs from publication news")
        if bundle.get("model_data_ref") is not None and bundle["model_data_ref"] != bundle["data_ref"]:
            raise EvidenceError("Numerical and published model data references differ")
        if bundle.get("context") and bundle["context"].get("industry_data_ref") != {
                "bundle_hash": sector_value["bundle_hash"], "news_ref": fingerprint(sector["news_ref"]),
                "event_frontier_hash": sector["news_state"]["event_frontier_hash"]}:
            raise EvidenceError("Context industry source/hash/frontier linkage differs")
    account_state = artifacts.read_json(bundle["account_state_ref"]) if bundle.get("account_state_ref") else None
    if bundle["account_recorded_at"] != (account_state["recorded_at"] if account_state else None):
        raise EvidenceError("Displayed account record time differs from the sealed account")
    if bundle["account_hash"] != (fingerprint(account_state) if account_state is not None else None):
        raise EvidenceError("Account binding differs from the recorded account artifact")
    calculation = artifacts.read_json(bundle["calculation_ref"]) if bundle.get("calculation_ref") else None
    from report import summarize_discovery
    discovery = artifacts.read_json(bundle["discovery_ref"]) if bundle.get("discovery_ref") else None
    if bundle["selection_summary"] != summarize_discovery(discovery, artifacts):
        raise EvidenceError("Displayed discovery evidence differs from its sealed snapshot")
    if discovery is not None:
        import news
        import fund_universe
        checked_news = news.validate_review(bundle["news"], {"kind": "numeric_policy", "news": bundle["news_policy"]},
                             bundle["decision_at"], artifacts, store=store)
        fund_universe.validate_discovery(discovery, bundle["news"], monitor_codes(account_state), artifacts, bundle["decision_at"],
            monitoring_codes=bundle["monitoring_codes"], news_state=checked_news, plan_constraints=bundle["plan_constraints"])
    if bundle["status"] == "observation_only":
        if (discovery is None or discovery["status"] != "observation_only" or discovery["codes"]
                or discovery["monitor_codes"] or bundle.get("context") or bundle.get("calculation_ref")
                or account_state is None or account_state.get("unknown") or account_state.get("pending_subscriptions")
                or bundle["profile_status"].get("status") != "confirmed"
                or any(bundle["orders"].get(key) for key in ("orders", "waiting", "funding_options"))):
            raise EvidenceError("Observation-only publication requires verified watch evidence and no numerical decision or trades")
    if bundle["status"] == "awaiting_account":
        if (account_state is None or bundle.get("context") is not None or bundle.get("calculation_ref") is not None
                or bundle.get("selection_ref") is not None or bundle["orders"].get("status") != "blocked"
                or any(bundle["orders"].get(key) for key in ("orders", "waiting", "funding_options"))):
            raise EvidenceError("Awaiting-account report cannot publish a quantitative decision or trades")
        market = artifacts.read_json(bundle["market_record_ref"])
        market_contract_coverage(market, artifacts)
        verify_research_binding(bundle, artifacts, store=store)
        from account_reconciliation import review_account
        expected_review = review_account(account_state, market["market_ref"], bundle["decision_at"])
        if expected_review is None or bundle.get("account_review") != expected_review:
            raise EvidenceError("Awaiting-account reasons differ from its frozen account and valuation")
        if (bundle["market_price_dates"] != market["provenance"]["price_dates"]
                or bundle["product_names"] != market["provenance"]["product_names"]
                or bundle["term_gaps"] != market["provenance"]["term_gaps"]):
            raise EvidenceError("Awaiting-account market details differ from source evidence")
    elif bundle.get("account_review") is not None:
        raise EvidenceError("Account-reconciliation report state differs from its status")
    context = bundle.get("context")
    review_inputs = bundle.get("trade_review_inputs")
    if bundle.get("family_results_ref"):
        inventory = artifacts.read_json(bundle["family_results_ref"])
        if (inventory["results"] or inventory.get("governance_block") is not None) and review_inputs is None:
            raise EvidenceError("Family research omitted its owner-bound review governance")
    if review_inputs is not None:
        import newtrade_guard
        review_spec = bundle.get("trade_review_spec")
        if store is None or review_spec is None:
            raise EvidenceError("Numerical review lacks its frozen policy and budget authority")
        expected_inputs = newtrade_guard.read_inputs(store, artifacts, bundle["account_id"], review_spec,
            bundle["decision_at"], decision_id=bundle["decision_id"])
        if expected_inputs != review_inputs:
            raise EvidenceError("Numerical review reservation differs from source-bound history")
        if (context is not None or bundle.get("family_results_ref") is not None) and review_inputs["qualification_scope"]["eligible"]:
            if review_inputs["review_reservation"] is None:
                raise EvidenceError("A completed numerical comparison did not consume its registered review")
    if context:
        if discovery is None or context["decision_at"] != bundle["decision_at"] or context["spec"]["news"] != bundle["news_policy"]:
            raise EvidenceError("Decision cutoff and frozen news policy must match the verified discovery publication")
        if context["spec_hash"] != bundle["spec_hash"] or fingerprint(context["spec"]) != bundle["spec_hash"]:
            raise EvidenceError("Decision strategy binding changed")
        if context["account_hash"] != bundle["account_hash"] or context["account_id"] != bundle["account_id"]:
            raise EvidenceError("Decision account binding changed")
        if fingerprint({k: v for k, v in context.items() if k != "context_hash"}) != context["context_hash"]:
            raise EvidenceError("Decision context content changed")
        state = artifacts.read_json(bundle["account_state_ref"])
        if fingerprint(state) != bundle["account_hash"]:
            raise EvidenceError("Frozen account projection changed")
        if context["market_ref_hash"] != fingerprint(context["market_ref"]):
            raise EvidenceError("Market binding changed")
        market = artifacts.read_json(bundle["market_record_ref"])
        market_contract_coverage(market, artifacts)
        if bundle["term_gaps"] != market["provenance"]["term_gaps"]:
            raise EvidenceError("Reported dealing gaps differ from market evidence")
        verify_research_binding(bundle, artifacts, store=store)
        if market["market_ref"]["terms_basis"] != market["provenance"]["terms_basis"]:
            raise EvidenceError("Market trading-terms evidence basis changed")
        if bundle["market_price_dates"] != market["provenance"]["price_dates"]:
            raise EvidenceError("Displayed NAV date differs from audited market data")
        if fingerprint(market["market_ref"]) != context["market_ref_hash"]:
            raise EvidenceError("Market source record differs from calculation")
        if bundle["product_names"] != market["provenance"]["product_names"]:
            raise EvidenceError("Reported product names differ from audited source")
        from pipeline import _check_plan, future_actions
        _check_plan(bundle["plan_constraints"], context["spec"])
        if discovery["universe_policy"] != context["spec"]["universe_policy"]:
            raise EvidenceError("Discovery rule differs from the frozen strategy")
        import newtrade_guard
        trade_inputs = newtrade_guard.read_inputs(store, artifacts, context["account_id"], context["spec"],
            context["decision_at"], decision_id=bundle["decision_id"])
        if (trade_inputs["trade_state"] != context.get("trade_state")
                or trade_inputs["trade_family_review_index"] != context.get("trade_family_review_index")):
            raise EvidenceError("Frozen trade inputs differ from canonical owner-bound financial facts")
        if bundle.get("trade_review_inputs") != trade_inputs:
            raise EvidenceError("Published numerical review lacks its immutable budget reservation")
        if trade_inputs["review_reservation"] is None or not trade_inputs["qualification_scope"]["eligible"]:
            raise EvidenceError("Actionable decision lacks a registered numerical review slot")
        from strategy import build_context
        rebuilt = build_context(context["spec"], state, context["decision_at"], context["market_ref"],
                                risk_state=context["risk_input"],
                                purchase_eligible_codes=context["purchase_eligible_codes"],
                                dynamic_universe=context["universe"], candidate_identities=context["identities"],
                                known_future_actions=future_actions(market, context["decision_at"]), action_inventory_scope="known_subset",
                                account_id=context["account_id"], sealed_intervention=context.get("sealed_intervention"),
                                news_review_hash=context.get("news_review_hash"),
                                trade_state=trade_inputs["trade_state"],
                                trade_family_review_index=trade_inputs["trade_family_review_index"],
                                industry_data_ref=context.get("industry_data_ref"),
                                sector_exposure_bounds=context.get("sector_exposure_bounds"),
                                readiness_ref=context.get("readiness_ref"), family_id=context.get("family_id"),
                                paired_currency_validation=context.get("paired_currency_validation"))
        if fingerprint(rebuilt) != fingerprint(context):
            raise EvidenceError("Context cannot be derived from its frozen account and market facts")
        if context["news_review_hash"] != fingerprint(bundle["news"]):
            raise EvidenceError("Decision news binding changed")
        if set(discovery["codes"]) != set(context["universe"]):
            raise EvidenceError("Model universe differs from verified live discovery")
        import fund_screen
        selection = artifacts.read_json(bundle["selection_ref"])
        rebuilt_selection = fund_screen.finalize_selection(discovery, context["identities"], context["market_ref"]["terms"],
            bundle["plan_constraints"], context["decision_at"],
            max_terms_age_seconds=context["spec"]["availability"]["max_terms_age_seconds"])
        # _verify_source_families has already revalidated the quality result
        # against its original source contracts. Reproduce the same complete
        # annotation that the daily controller seals, retaining strict identity.
        quality_inputs = artifacts.read_json(bundle["quality_inputs_ref"])
        quality_bundle = artifacts.read_json(quality_inputs["source_bundle_ref"])
        rebuilt_selection = fund_screen.attach_quality(rebuilt_selection, artifacts.read_json(bundle["quality_ref"]),
                                                        quality_bundle["admission"])
        if fingerprint(selection) != fingerprint(rebuilt_selection):
            raise EvidenceError("Final product comparison differs from verified source contracts")
        if not set(context["purchase_eligible_codes"]) <= set(selection["eligible_buy_codes"]):
            raise EvidenceError("Purchase eligibility differs from the assessed industry direction")
        if bundle["profile_status"]["profile_hash"] != context["risk_input"]["profile_hash"]:
            raise EvidenceError("Displayed profile differs from the bound risk input")
        if calculation and calculation["input_hash"] != fingerprint(artifacts.read_json(bundle["data_ref"])):
            raise EvidenceError("Calculation data differs from the frozen dataset")
        from report import summarize
        if calculation and bundle["comparison_summary"] != summarize(calculation, context):
            raise EvidenceError("Displayed model metrics differ from calculation")
        if calculation and fingerprint(calculation.get("orders")) != fingerprint(bundle["orders"]):
            raise EvidenceError("Report orders differ from verified computation")
        if calculation:
            bound_data = artifacts.read_json(bundle["data_ref"])
            numerical_invariants(context, calculation, bound_data)
            if calculation.get("predictive_increment", {}).get("currency_increment", {}).get("paired_rows"):
                from paired_source_audit import recheck_pairs
                recheck_pairs(context, bound_data, calculation["predictive_increment"],
                              {"artifacts": artifacts, "store": store, "binding_bundle": bundle})
    elif bundle["orders"]["orders"]:
        raise EvidenceError("Orders require a verified decision context")
    from report import explain_actions
    from report_validation import reference_valuation_invariants, public_request_invariants
    public_request_invariants(bundle["orders"])
    if bundle["orders"]["orders"]:
        reference_valuation_invariants(context, bundle["orders"]["orders"], bundle["orders"]["reference_valuation"])
    if bundle["action_analysis"] != explain_actions(bundle, artifacts):
        raise EvidenceError("Action explanations differ from source-bound amounts and comparisons")
    if bundle["qualification"]["status"] != "conditional_research" and bundle.get("calculation_ref"):
        # No caller-supplied boolean can grant live investment qualification.
        raise EvidenceError("Unsupported investment qualification")
    return {"status": "passed", "bundle_hash": bundle["bundle_hash"],
            "scope": "input_binding_artifact_integrity_order_consistency_not_profitability"}


def reproduce(context, data, calculation, *, source_runtime=None):
    from allocation_runner import predict
    rebuilt = predict(context, data, validate_steps=False, family_count=calculation.get("family_count", 1), source_runtime=source_runtime)
    def computational_value(value):
        return {key: item for key, item in value.items() if key != "validation_trace"}
    if fingerprint(computational_value(rebuilt)) != fingerprint(computational_value(calculation)):
        raise EvidenceError("Frozen-input rerun differs from the saved calculation")
    arithmetic = numerical_invariants(context, calculation, data)
    return {"status": "passed", "context_hash": fingerprint(context),
            "calculation_hash": fingerprint(calculation), "method": "frozen_input_reexecution",
            "independent_arithmetic": arithmetic}



TARGETS = ("pricing", "terminal_nav", "hold", "buy", "sell")
LATENTS = ("log_pricing", "log_terminal_nav", "sqrt_cash_neither", "sqrt_cash_sale_only",
           "sqrt_cash_buy_only", "sqrt_cash_both")


def _check(condition, message):
    if not condition:
        raise EvidenceError(message)


def _decoded(values):
    """Independent price/cash identities, without the estimator decoder."""
    _check(set(values) == set(LATENTS) and all(math.isfinite(float(x)) for x in values.values()),
           "Complete finite latent coordinates required")
    price, terminal = math.exp(values["log_pricing"]), math.exp(values["log_terminal_nav"])
    cash = {name: values["sqrt_cash_"+name]**2 for name in ("neither", "sale_only", "buy_only", "both")}
    return {"pricing": price, "terminal_nav": terminal, "hold": terminal+math.fsum(cash.values()),
            "buy": (terminal+cash["buy_only"]+cash["both"])/price,
            "sell": price+cash["sale_only"]+cash["both"]}


def _source_right(event, owned, sold=None):
    record = event["record_date"]
    if owned > record or sold is not None and sold < record:
        return False
    for boundary, field in ((owned == record, "subscribe_on_record_date"),
                            (sold == record, "redeem_on_record_date")):
        if boundary:
            choice = event["entitlement_rule"].get(field)
            _check(choice in ("included", "excluded"), "Unknown source record-date ownership")
            if choice == "excluded":
                return False
    return True


def _source_nav_quote(row, boundary, *, require_known=False):
    """Independently reconstruct an original NAV vintage at a knowledge cutoff."""
    import copy
    versions = row.get("source_versions", [])
    archived = [version for version in versions if version.get("available_at") is not None]
    output = {key: copy.deepcopy(value) for key, value in row.items() if key != "source_versions"}
    if not archived and not require_known:
        return output
    for version in archived:
        evidence, raw = version.get("availability_evidence") or {}, version.get("raw_ref") or {}
        _check(evidence.get("kind") == "archived_original_capture"
               and evidence.get("source_id") == raw.get("source_id")
               and evidence.get("raw_sha256") == raw.get("sha256")
               and evidence.get("captured_at") == version["available_at"] == version.get("observed_at"),
               "NAV vintage lacks its original availability capture")
    eligible = [version for version in versions
                if instant(version.get("available_at") or version["observed_at"]) <= instant(boundary)]
    if not eligible:
        return None
    selected = max(eligible, key=lambda version: (instant(version.get("available_at") or version["observed_at"]), version["version_id"]))
    _check(selected["date"] == row["date"] and selected["code"] == row.get("code", selected["code"]),
           "NAV vintage identity differs from the original source")
    output = {"code": row.get("code", selected["code"]), "date": row["date"]}
    for key in ("nav", "cumulative_nav", "distribution_per_share", "distribution_text", "provider_daily_return",
                "historical_published_at", "corporate_actions", "distribution_evidence"):
        if key in selected:
            output[key] = copy.deepcopy(selected[key])
    known = selected.get("available_at") or selected["observed_at"]
    output.update(available_at=selected.get("available_at"), observed_at=selected["observed_at"],
                  source_version_id=selected["version_id"], raw_ref=selected["raw_ref"],
                  known_at=dt.datetime.fromisoformat(known).isoformat(),
                  strict_PIT_verified=selected.get("available_at") is not None)
    return output


def _joint_source_invariants(context, fitting, data, close, *, rebased=True):
    """Bind OOS arithmetic to audited NAV/action rows, not self-reported hashes."""
    distribution = fitting["joint_scenarios"]
    codes, records = distribution["codes"], distribution["source_oos"]
    selected = distribution["source_selection_oos"]
    calibration = distribution["source_calibration_oos"]
    _check(records and selected and calibration and selected == records[:len(records)//2]
           and calibration == [row for row in records[len(records)//2:] if row["date"] > max(item["label_available"] for item in selected)],
           "OOS selection/calibration split differs from full source audit")
    _check([row["date"] for row in selected] == distribution["dates"], "Selection origins differ from scenario pairing")
    scenario_indices = {row["date"]: index for index, row in enumerate(selected)}
    industry = fitting.get("industry_forecast")
    sector_predictions = {(row["decision_date"], row["sector_id"]): row for row in industry["forecasts"]} if industry else {}
    _check(distribution["dates"] == sorted(set(distribution["dates"])), "OOS origins must be unique and increasing")
    raw = {code: {row["date"]: row for row in data["nav"][code]} for code in codes}
    forecasts = {row["code"]: row for row in fitting["forecasts"]}
    lag = context["spec"]["availability"]["nav_lag_calendar_days"]
    common_days = sorted(set.intersection(*({day for day in rows if dt.date.fromisoformat(day).weekday() < 5}
                                           for rows in raw.values())))
    for index, record in enumerate(records):
        origin, available, end = record["date"], record["label_available"][:10], record["end_date"]
        _check(record["codes"] == codes and origin < available < context["as_of"] and end <= available,
               "OOS label uses future information or a different universe")
        _check(record["origin_at"][:10] == origin, "OOS origin instant differs")
        target_day = (dt.date.fromisoformat(origin)+dt.timedelta(days=context["spec"]["planning"]["primary_horizon_days"])).isoformat()
        _check(end == next((day for day in common_days if day >= target_day), None), "OOS common endpoint differs from source NAV dates")
        actual_maturity = [source.get("label_available_at") for source in record["source_labels"].values()]
        expected_maturity = (max(actual_maturity, key=instant)[:10] if all(actual_maturity) else
                             (dt.date.fromisoformat(end)+dt.timedelta(days=lag)).isoformat())
        _check(available == expected_maturity, "OOS maturity differs from source endpoint or original capture")
        training = record.get("training_audit", {})
        _check(not training.get("max_label_available_date") or training["max_label_available_date"] < origin,
               "OOS training uses future information")
        for column, code in enumerate(codes):
            source = record["source_labels"][code]
            base, priced = source["base_date"], source["pricing_date"]
            _check(source["code"] == code and source["end_date"] == end and source["label_available"][:10] == available,
                   "OOS source identity or dates differ")
            feature_source = source.get("feature_source", {})
            native_values = None
            base_quote = _source_nav_quote(raw[code][base], record["origin_at"],
                require_known=feature_source.get("availability_basis", "declared_lag_research_proxy") != "declared_lag_research_proxy")
            _check(base_quote is not None, "OOS base NAV was unavailable at the original origin")
            if source.get("base_quote") is not None:
                _check(source["base_quote"] == base_quote, "OOS base NAV vintage changed")
            if feature_source.get("strict_PIT_verified") is True:
                import statistics
                history = [_source_nav_quote(raw[code][day], record["origin_at"], require_known=True)
                           for day in sorted(raw[code]) if day <= base][-121:]
                _check(len(history) == 121 and all(row is not None and row.get("strict_PIT_verified") is True for row in history),
                       "OOS native features lack the original mature source history")
                _check(feature_source.get("source_version_ids") == [row["source_version_id"] for row in history]
                       and instant(feature_source["available_at"]) <= instant(record["origin_at"]),
                       "OOS feature source vintage or availability changed")
                rates = [(float(row["nav"])+float(row["distribution_per_share"]))/float(prior["nav"])-1
                         for prior, row in zip(history, history[1:])]
                wealth = [1.]
                for rate in rates:
                    wealth.append(wealth[-1]*(1+rate))
                drawdown, peak = 0., wealth[-61]
                for value in wealth[-61:]:
                    peak = max(peak, value)
                    drawdown = max(drawdown, 1-value/peak)
                native_values = [wealth[-1]/wealth[-1-period]-1 for period in (20, 60, 120)]
                native_values += [statistics.stdev(rates[-60:])*math.sqrt(252), drawdown]
                _check(base == source["feature_cutoff_date"], "OOS actual feature cutoff changed")
            else:
                cutoff = (dt.date.fromisoformat(origin)-dt.timedelta(days=lag)).isoformat()
                feature_dates = [row["feature_cutoff_nav_date"] for row in data["features"][code]
                                 if row["feature_cutoff_nav_date"] <= cutoff]
                _check(feature_dates and base == max(feature_dates) == source["feature_cutoff_date"], "OOS proxy feature cutoff is not source available")
            if industry is not None:
                from allocation_market import NAV_FEATURE_NAMES, FEATURE_NAMES
                from industry_model import FUND_FEATURE_NAMES
                witness = record.get("industry_sources", {}).get(code)
                _check(witness is not None and witness["decision_date"] == origin and witness["code"] == code
                       and witness["source_hash"] == fingerprint({key: value for key, value in witness.items() if key != "source_hash"}),
                       "OOS industry bridge witness changed")
                from industry_validation import validate_bridge_witness
                exposure = data["industry_exposures"].get(code, [])
                predicted_sectors = {entity: prediction for (prediction_origin, entity), prediction in sector_predictions.items()
                                     if prediction_origin == origin}
                validate_bridge_witness(witness, exposure, predicted_sectors)
                values = witness["values"]
                feature = next(row for row in data["features"][code] if row["feature_cutoff_nav_date"] == base)
                vector = (native_values if native_values is not None else [feature["values"][name] for name in NAV_FEATURE_NAMES])+[(dt.date.fromisoformat(origin)-dt.date.fromisoformat(base)).days]+values
                model = record["model"]
                _check(model["feature_names"] == FEATURE_NAMES+FUND_FEATURE_NAMES, "Whole-stack fund model feature order")
                for latent_index, name in enumerate(LATENTS):
                    value = model["intercept"][latent_index]+math.fsum(coefficient*(value-mean)/scale
                        for coefficient, value, mean, scale in zip(model["coefficients"][latent_index], vector, model["scaler_mean"], model["scaler_scale"]))
                    close(record["predicted_latents"][name][column], value, "whole-stack OOS source feature dot product", 1)
            clock = source["clock"]
            sessions = sorted(day for day in raw[code] if dt.date.fromisoformat(day).weekday() < 5)
            expected_price = next((day for day in sessions if day > origin or day == origin and not clock["after_cutoff"]), None)
            _check(priced == expected_price, "OOS pricing date differs from observed source sessions")
            rule = clock["confirmation_rule"]
            if rule["day_basis"] == "calendar_days":
                confirmed = (dt.date.fromisoformat(priced)+dt.timedelta(days=rule["lag_days"])).isoformat()
            else:
                _check(rule["day_basis"] == "trading_days", "Unknown source confirmation clock")
                later = [day for day in sessions if day > priced]
                confirmed = priced if rule["lag_days"] == 0 else later[rule["lag_days"]-1]
            owned = priced if clock["ownership_start"] == "execution_date" else confirmed
            _check(source["confirmation_date"] == confirmed and source["ownership_date"] == owned,
                   "OOS ownership differs from source clock")
            outcome_rows = raw[code]
            if source.get("source_quotes") is not None:
                quoted = {row["date"]: row for row in source["source_quotes"]}
                expected_dates = {day for day in raw[code] if base < day <= end}
                _check(set(quoted) == expected_dates and len(quoted) == len(source["source_quotes"]), "OOS outcome source quote coverage changed")
                boundary = source.get("label_available_at") or available+"T00:00:00+08:00"
                for day, quote in quoted.items():
                    _check(quote == _source_nav_quote(raw[code][day], boundary,
                        require_known=source.get("vintage_selection") == "known_source_versions"),
                        "OOS outcome NAV vintage differs from original source capture")
                outcome_rows = {base: base_quote, **quoted}
            prices = [Decimal(str((base_quote if day == base else outcome_rows[day])["nav"])) for day in (base, priced, end)]
            for name, value in zip(("base_nav", "pricing_nav", "terminal_nav"), prices):
                close(source[name], value, "source NAV " + name, value)
            events = []
            for day, row in outcome_rows.items():
                if base < day <= end:
                    selected = [event for event in row.get("corporate_actions", []) if event.get("kind") == "cash_distribution"]
                    close(sum(Decimal(str(event["per_share"])) for event in selected), row["distribution_per_share"], "source dividend coverage", prices[0])
                    _check(all(event["ex_date"] == day and event["currency"] == "CNY" and event["distribution_mode"] == "cash"
                               and event.get("evidence_ref") for event in selected), "Source dividend identity or evidence differs")
                    events.extend(selected)
            _check(sorted(fingerprint(event) for event in events) == sorted(fingerprint(event) for event in source["dividends"]),
                   "OOS dividends differ from audited NAV source actions")
            buckets = dict.fromkeys(("neither", "sale_only", "buy_only", "both"), Decimal(0))
            old_owner = (dt.date.fromisoformat(base)-dt.timedelta(days=1)).isoformat()
            for event in events:
                buy_right, sale_right = _source_right(event, owned), _source_right(event, old_owner, priced)
                key = "both" if buy_right and sale_right else "buy_only" if buy_right else "sale_only" if sale_right else "neither"
                buckets[key] += Decimal(str(event["per_share"]))/prices[0]
            measured = {"log_pricing": math.log(float(prices[1]/prices[0])), "log_terminal_nav": math.log(float(prices[2]/prices[0])),
                        **{"sqrt_cash_"+name: math.sqrt(float(amount)) for name, amount in buckets.items()}}
            targets = _decoded(measured)
            predicted = {name: record["predicted_latents"][name][column] for name in LATENTS}
            decoded_prediction = _decoded(predicted)
            for name in LATENTS:
                close(source["latent_targets"][name], measured[name], "raw source latent", 1)
                close(record["realized_latents"][name][column], measured[name], "realized source latent", 1)
                close(record["latent_errors"][name][column], measured[name]-predicted[name], "source latent residual", 1)
            for name in TARGETS:
                close(source["targets"][name], targets[name], "raw source target", 1)
                close(record["realized_targets"][name][column], targets[name], "realized source target", 1)
                close(record["predicted_log_targets"][name][column], math.log(decoded_prediction[name]), "point target decoder", 1)
                close(record["log_target_errors"][name][column], math.log(targets[name])-math.log(decoded_prediction[name]), "public target residual", 1)
            _check(source["source_hash"] == record["source_hashes"][code]
                   == fingerprint({key: value for key, value in source.items() if key != "source_hash"}), "OOS source identity changed")
            mean = forecasts[code]["predicted_latents"]
            current = _decoded({name: mean[name]+record["latent_errors"][name][column] for name in LATENTS})
            point = _decoded(mean)
            forecast_base, mark_date = forecasts[code]["feature_cutoff_date"], context["market_ref"]["price_dates"][code]
            base_nav, mark_nav = float(raw[code][forecast_base]["nav"]), float(raw[code][mark_date]["nav"])
            close(context["market_ref"]["prices"][code], mark_nav, "current source NAV", mark_nav)
            known_cash = math.fsum(float(row["distribution_per_share"]) for day, row in raw[code].items() if forecast_base < day <= mark_date)
            for name in TARGETS:
                def transform(value):
                    if not rebased:
                        return value
                    return value if name == "buy" else (value*base_nav-(known_cash if name in ("hold", "sell") else 0))/mark_nav
                if origin in scenario_indices:
                    close(distribution["targets"][name][scenario_indices[origin]][column], transform(current[name]), "rebased joint selection scenario", 1)
                close(distribution["current_point_targets"][name][0][column], transform(point[name]), "rebased current point target", 1)
    return len(records)


def _cash_right_bounds(events, owned, sold=None):
    lower = upper = Decimal(0)
    for event in events:
        record = event["record_date"]
        if record < owned or sold is not None and record > sold:
            continue
        allowed = []
        rule = event.get("rights_rule", event.get("entitlement_rule", {}))
        if owned == record:
            allowed.append(rule.get("subscribe_on_record_date"))
        if sold == record:
            allowed.append(rule.get("redeem_on_record_date"))
        if "excluded" in allowed:
            continue
        amount = Decimal(str(event["per_share"]))
        upper += amount
        if all(value == "included" for value in allowed):
            lower += amount
    return lower, upper


def _scenario_wealth(context, projection, distribution, index, funding, close, trades=None):
    """Reconstruct individual asset/cash contributions at scenario pricing."""
    import fee_contract, cash_reference
    from zoneinfo import ZoneInfo
    snapshot, clock = context["snapshot"], distribution["clock_context"]
    evaluation = clock["evaluation_date"]
    columns = {code: index for index, code in enumerate(distribution["codes"])}
    lower = upper = Decimal(snapshot["cash"])+funding+Decimal(snapshot["unsettled_cash"])
    observed = [] if trades is None else trades
    sales = {row["lot_id"]: row for row in projection["sells"]}
    def values(code):
        column = columns[code]
        target = {name: Decimal(str(distribution["targets"][name][index][column])) for name in TARGETS}
        _check(all(value.is_finite() and value > 0 for value in target.values()), "Positive scenario targets required")
        price, terminal, hold, buy, sell = [target[name] for name in TARGETS]
        tolerance = Decimal("0.00000001")*max(Decimal(1), price, terminal, hold, price*buy, sell)
        _check(hold+tolerance >= terminal and price*buy+tolerance >= terminal and price*buy <= hold+tolerance
               and sell+tolerance >= price and sell-price <= hold-terminal+tolerance, "Inconsistent scenario dividend rights")
        marked = Decimal(context["market_ref"]["prices"][code])
        events = [event for event in context.get("known_future_actions", [])
                  if event["code"] == code and context["market_ref"]["price_dates"][code] < event["ex_date"] <= evaluation]
        announced = sum((Decimal(str(event["per_share"])) for event in events), Decimal(0))
        _check(marked*(hold-terminal)+Decimal("0.00000001") >= announced, "Targets contradict known distributions")
        return target, marked, events
    def terminal(quantity, price, terms, starts):
        gross = quantity*price
        if context["spec"]["planning"]["primary_goal"] != "redeem" or not quantity:
            return gross, Decimal(0), Decimal(0)
        ages = fee_contract.holding_ages_at_exit(terms, starts, evaluation+"T09:00:00+08:00")
        _check(ages and min(ages) >= terms["holding"]["minimum_days"], "Terminal redemption precedes lock")
        fees = [gross-cash_reference.exit_details_at_age(gross, terms, age)["net"] for age in ages]
        return gross, min(fees), max(fees)
    for lot in snapshot["positions"]:
        code, quantity = lot["code"], Decimal(lot["shares"])
        target, marked, known = values(code)
        terms = context["fee_contracts"][code]
        priced, endpoint = marked*target["pricing"], marked*target["terminal_nav"]
        owned = instant(lot.get("ownership_at", lot["acquired_at"])).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        total_cash = marked*(target["hold"]-target["terminal_nav"])
        announced = sum((Decimal(str(event["per_share"])) for event in known), Decimal(0))
        unknown_cash = max(Decimal(0), total_cash-announced)
        rights_low, rights_high = _cash_right_bounds(known, owned)
        rights_high += unknown_cash
        if owned <= context["market_ref"]["price_dates"][code]:
            rights_low += unknown_cash
        sold = Decimal(sales.get(lot["lot_id"], {}).get("shares", "0"))
        _check(0 <= sold <= quantity, "Single-step sale quantity differs from holdings")
        gross, fee_low, fee_high = terminal(quantity-sold, endpoint, terms,
            {"earliest": lot["acquired_at"], "latest": lot["acquired_at"]})
        lower += gross+(quantity-sold)*rights_low-fee_high
        upper += gross+(quantity-sold)*rights_high-fee_low
        if sold:
            sale_day = clock["assets"][code]["pricing_date"]
            known_low, known_high = _cash_right_bounds(known, owned, sale_day)
            dividend = marked*(target["sell"]-target["pricing"])
            details = cash_reference.exit_details(sold*priced, terms, acquired_at=lot["acquired_at"], submitted_at=context["decision_at"])
            gross, fee = details["gross"], details["effective_cost"]
            lower += gross-fee+sold*dividend
            upper += gross-fee+sold*(dividend+known_high-known_low)
            if trades is not None:
                actual = next((row for row in observed if row["side"] == "sell" and row["lot_id"] == lot["lot_id"]), None)
                _check(actual is not None and actual.get("spendable_now") is False, "Sale proceeds cannot fund current buys")
                for key, value in (("pricing_NAV", priced), ("shares", sold), ("gross", gross), ("fee", fee)):
                    close(actual[key], value, "scenario sale "+key, gross)
    for buy in projection["buys"]:
        code = buy["code"]
        target, marked, known = values(code)
        terms, asset_clock = context["fee_contracts"][code], clock["assets"][code]
        priced, endpoint = marked*target["pricing"], marked*target["terminal_nav"]
        quote = fee_contract.quote_entry(buy.get("cash_limit", buy["cash_debit"]), terms,
                                        price=priced, share_step=terms["trade_precision"]["share_step"])
        _check(quote["shares"] > 0, "Scenario purchase has no shares")
        known_low, _ = _cash_right_bounds(known, asset_clock["ownership_date_bounds"]["latest"])
        _, known_high = _cash_right_bounds(known, asset_clock["ownership_date_bounds"]["earliest"])
        dividend = priced*target["buy"]-endpoint
        gross, fee_low, fee_high = terminal(quote["shares"], endpoint, terms, asset_clock["holding_start_bounds"])
        lower += gross+quote["shares"]*dividend-fee_high-quote["debit"]
        upper += gross+quote["shares"]*(dividend+known_high-known_low)-fee_low-quote["debit"]
        if trades is not None:
            actual = next((row for row in observed if row["side"] == "buy" and row["code"] == code), None)
            _check(actual is not None, "Scenario buy record is missing")
            for key, value in (("pricing_NAV", priced), ("shares", quote["shares"]), ("gross", quote["gross"]),
                               ("fee", quote["fee"]), ("cash_debit", quote["debit"]), ("terminal_NAV_wealth", gross)):
                close(actual[key], value, "scenario buy "+key, gross)
    if trades is not None:
        _check(len(observed) == len(projection["buys"])+len(projection["sells"]), "Scenario trade inventory differs")
    return float(lower), float(upper)


def terminal_cost_invariants(context, path, value, terminal_day, *, source_path_cost=None):
    """Reprice terminal assumptions from source terms, outside the cash journal."""
    import cash_reference
    required = {"path_execution_cost", "terminal_exit_assumption_cost",
                "terminal_gross_rounding_adjustment", "terminal_wealth_drag", "terminal_exit_assumptions"}
    _check(required <= value.keys(), "Terminal cost/assumption fields are absent")
    rows = value["terminal_exit_assumptions"]
    _check(type(rows) is list, "Terminal exit assumptions must be a list")
    redeem = context["spec"]["planning"]["primary_goal"] == "redeem"
    lots = value["terminal_lots"]
    _check(len(rows) == (len(lots) if redeem else 0), "Terminal exit assumption inventory changed")
    witnesses = {row["lot_id"]: row for row in rows}
    _check(len(witnesses) == len(rows), "Terminal exit assumption lot duplicated")
    cost, adjustment, drag, assets = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    for lot in lots:
        known = [day for day in path["nav"] if day <= terminal_day and lot["code"] in path["nav"][day]]
        _check(known, "Terminal source model mark is absent")
        nav = Decimal(str(path["nav"][max(known)][lot["code"]]))
        _check(nav > 0, "Terminal NAV outside domain")
        gross = Decimal(lot["shares"])*nav
        if not redeem:
            assets += gross
            continue
        contract = context["fee_contracts"][lot["code"]]
        submitted = cash_reference.submission_stamp(terminal_day, context["decision_at"])
        details = cash_reference.exit_details(gross, contract, acquired_at=lot["acquired_at"], submitted_at=submitted)
        charge = details["contractual_fee"]+details["rounding_loss"]
        gross_adjustment, wealth_drag = gross-details["gross"], gross-details["net"]
        expected = {"shares": Decimal(lot["shares"]), "nav": nav, "raw_gross": gross,
            "quoted_gross": details["gross"], "contractual_fee": details["contractual_fee"],
            "rounding_loss": details["rounding_loss"], "net": details["net"], "cost": charge,
            "gross_rounding_adjustment": gross_adjustment, "wealth_drag": wealth_drag}
        row = witnesses.get(lot["lot_id"])
        _check(row is not None and row.get("code") == lot["code"] and row.get("acquired_at") == lot["acquired_at"]
               and row.get("submitted_at") == submitted and row.get("fee_contract_hash") == fingerprint(contract),
               "Terminal exit assumption source binding changed")
        _check(row.get("is_order") is False and row.get("is_ledger_event") is False
               and row.get("scope") == "terminal_exit_valuation_assumption_not_order_or_ledger",
               "Terminal exit assumption escaped valuation scope")
        for key, amount in expected.items():
            _check(key in row and Decimal(str(row[key])) == amount, "Terminal exit assumption "+key+" changed")
        _check(charge+gross_adjustment == wealth_drag, "Terminal exit assumption cost/rounding conservation failed")
        cost += charge
        adjustment += gross_adjustment
        drag += wealth_drag
        assets += details["net"]
    for field, amount in (("terminal_exit_assumption_cost", cost),
                          ("terminal_gross_rounding_adjustment", adjustment), ("terminal_wealth_drag", drag)):
        _check(Decimal(str(value[field])) == Decimal(str(float(amount))), "Source terminal cost "+field+" changed")
    path_cost = source_path_cost if source_path_cost is not None else sum(
        (Decimal(str(row["fee"])) for row in value["cash_flow_log"] if row["kind"] in {"price_buy", "price_sell"}), Decimal(0))
    _check(path_cost >= 0 and Decimal(str(value["path_execution_cost"])) == Decimal(str(float(path_cost))),
           "Source path execution cost differs from journal costs")
    _check(Decimal(str(value["fees"])) == Decimal(str(float(path_cost+cost))),
           "Source total model fees differ from path and terminal costs")
    return {"terminal_asset_wealth": assets, "terminal_exit_assumption_cost": cost,
            "terminal_gross_rounding_adjustment": adjustment, "terminal_wealth_drag": drag}


def cash_journal_invariants(context, policy, path, value, *, end_day=None):
    """Validate every primary cash path, independently of an optional deadline."""
    import cash_reference
    end_day = end_day or value.get("primary_observed_through") or min(
        (dt.date.fromisoformat(context["as_of"])+dt.timedelta(
            days=context["spec"]["planning"]["primary_horizon_days"])).isoformat(), max(path["nav"]))
    if "primary_observed_through" in value:
        _check(value["primary_observed_through"] == end_day, "Primary cash observation date changed")
    initial = Decimal(context["snapshot"]["available_cash"])+Decimal(str(policy["proposed_contribution"]))
    if "initial_available_cash" in value:
        _check(Decimal(value["initial_available_cash"]) == initial, "Primary initial cash differs from source account")
    for field in ("current_action", "future_rule"):
        if field in policy:
            _check(value[field] == policy[field], "Source valuation differs from frozen policy "+field)
    source_policy = {**{field: value[field] for field in ("current_action", "future_rule", "local_decision_dates")}, **policy}
    balances = {"cash": value["settled_cash"], "reserved_cash": value["reserved_cash"], "receivables": value["receivables"]}
    result = cash_reference.reconcile(context, source_policy, path, value["cash_flow_log"], end_day, balances)
    _check("path_execution_cost" in value, "Path execution cost is absent")
    _check(Decimal(str(value["path_execution_cost"])) == Decimal(str(float(result["fees"]))),
           "Source path execution cost changed")
    def inventory(rows):
        totals = {}
        for row in rows:
            key = (row["code"], row["acquired_at"], row.get("ownership_at", row["acquired_at"]))
            totals[key] = totals.get(key, Decimal(0))+Decimal(str(row["shares"]))
        return {key: amount for key, amount in totals.items() if amount}
    _check(inventory(value["terminal_lots"]) == inventory(result["holdings"]),
           "Source terminal shares/holding/ownership differ from journal")
    if "terminal_existing_receivables" in value:
        rows = value["terminal_existing_receivables"]
        expected = result["outstanding_existing"]
        _check(len(rows) == len(expected), "Source terminal existing receipt inventory changed")
        actual = {row["receivable_id"]: row for row in rows}
        _check(len(actual) == len(rows), "Source terminal existing receipt identity duplicated")
        for source in expected:
            _check(source["receivable_id"] in actual and all(actual[source["receivable_id"]].get(key) == item
                   for key, item in source.items()), "Source terminal existing receipt identity/amount changed")
    terminal_cost_invariants(context, path, value, end_day, source_path_cost=result["fees"])
    return result


def cash_deadline_invariants(context, policy, path, value, *, primary_end=None):
    """The deadline is an additional source receipt cutoff, after universal checks."""
    import cash_reference
    cash_journal_invariants(context, policy, path, value, end_day=primary_end)
    planning = context["spec"]["planning"]
    days, required = planning.get("cash_deadline_days"), planning.get("cash_required_amount")
    _check((days is None) == (required is None), "Cash deadline contract is incomplete")
    proof = value.get("cash_deadline")
    if days is None:
        _check(proof is None, "Unexpected cash deadline proof")
        return True
    deadline = (dt.date.fromisoformat(context["as_of"])+dt.timedelta(days=days)).isoformat()
    _check(proof is not None and proof["date"] == proof["observed_through"] == deadline,
           "Cash deadline path does not cover the declared date")
    _check(Decimal(proof["required_amount"]) == Decimal(required), "Cash deadline amount changed")
    initial = Decimal(context["snapshot"]["available_cash"])+Decimal(str(policy["proposed_contribution"]))
    _check(Decimal(proof["initial_available_cash"]) == initial, "Deadline initial cash differs from source account")
    primary_journal = [row for row in value["cash_flow_log"] if row["date"] <= deadline]
    _check(proof["cash_flow_log"][:len(primary_journal)] == primary_journal,
           "Deadline journal omits or changes primary cash movements")
    balances = {"cash": proof["available_cash"], "reserved_cash": proof["reserved_cash"], "receivables": proof["receivables"]}
    source_policy = {**{field: value[field] for field in ("current_action", "future_rule", "local_decision_dates")}, **policy}
    result = cash_reference.reconcile(context, source_policy, path, proof["cash_flow_log"], deadline, balances,
        prefix="Deadline", entitlement_cutoff=value.get("primary_observed_through", primary_end))
    passed = result["cash"] >= Decimal(required)
    _check(type(proof["passes"]) is bool and proof["passes"] == passed, "Deadline cash qualification changed")
    return passed


def numerical_invariants(context, calculation, data=None):
    """Independent source-bound flow and current-action publication checks."""
    import portfolio_mpc, ledger
    mpc, paths = calculation.get("mpc"), calculation.get("paths")
    orders = calculation.get("orders", {}).get("orders", [])
    if mpc is None or paths is None:
        _check(not orders, "No orders without verified MPC paths")
        _check(calculation.get("status") != "research_ready", "Research-ready financial result requires verified MPC paths")
        return {"status": "not_applicable", "reason": "no_source_qualified_MPC_amount"}
    _check(mpc["context_hash"] == context["context_hash"], "MPC context hash differs")
    _check(paths["source_hash"] == fingerprint(data) if data is not None else True, "Path source data differs")
    _check(paths["status"] == "research_ready", "Unqualified paths cannot create financial plans")
    probabilities = [row["probability"] for row in paths["selection_paths"]]
    _check(probabilities and all(math.isfinite(p) and p > 0 for p in probabilities)
           and math.isclose(math.fsum(probabilities),1.,abs_tol=1e-10), "Original selection probability mass required")
    from risk_numbers import number as model_number, measure
    probabilities = measure(probabilities)
    from decimal import Decimal, ROUND_HALF_UP
    from numeric_validation import close
    import cash_reference
    terminal_day = paths["stage_dates"][-1]
    def valuation(value, path):
        portfolio_mpc.verify_cash_flow(value)
        _check(value["valuation_hash"] == fingerprint({k:v for k,v in value.items() if k != "valuation_hash"}), "MPC valuation content changed")
        money = sum((Decimal(value[key]) for key in ("settled_cash","reserved_cash","receivables")),Decimal(0))
        ids = [row["lot_id"] for row in value["terminal_lots"]]
        _check(len(ids) == len(set(ids)), "Duplicated terminal holding lot")
        for lot in value["terminal_lots"]:
            shares,reserved = Decimal(lot["shares"]),Decimal(lot["reserved_shares"])
            _check(0 <= reserved <= shares and lot["code"] in context["allocation_codes"], "Terminal share inventory is infeasible")
        money += terminal_cost_invariants(context, path, value, terminal_day)["terminal_asset_wealth"]
        close(value["terminal_wealth"],money,"independent terminal NAV/cash/receivable wealth",float(context["snapshot"]["equity"]))
    for group in mpc["funding_options"]:
        for policy in group["candidates"]:
            capital = model_number(context["snapshot"]["equity"])+model_number(policy["proposed_contribution"])
            _check(capital > 0 and len(policy["selection"]) == len(probabilities), "Original finite path inventory required")
            valuation(policy["point"],paths["point_paths"][0])
            deadline_passes = [cash_deadline_invariants(context, policy, paths["point_paths"][0], policy["point"], primary_end=terminal_day)]
            for value,path in zip(policy["selection"],paths["selection_paths"]):
                valuation(value,path)
                deadline_passes.append(cash_deadline_invariants(context, policy, path, value, primary_end=terminal_day))
            _check(not policy["eligible"] or all(deadline_passes), "Eligible policy violates the declared cash deadline")
            profits = [model_number(row["terminal_wealth"])-capital for row in policy["selection"]]
            expected = sum(p*v for p,v in zip(probabilities,profits))
            close(policy["expected_profit"],expected,"independent afterfee profit excluding external funding",capital)
            close(policy["point_profit"],model_number(policy["point"]["terminal_wealth"])-capital,"point profit",capital)
            losses = [-value for value in profits]
            tail = model_number(context["spec"]["allocation"]["tail_probability"])
            _check(0 < tail <= 1,"CVaR tail mass outside domain")
            # For the declared common information group, nested CVaR reduces
            # to the independently minimized Rockafellar-Uryasev expression.
            common = all(len(groups) == 1 and set(groups[0]) == {path["id"] for path in paths["selection_paths"]}
                         for groups in paths["prefix_groups"].values())
            _check(common,"Unverified dynamic information partition")
            risk = min(level+sum(p*max(loss-level,0) for p,loss in zip(probabilities,losses))/tail for level in losses)
            close(policy["absolute_cvar"],risk,"independent variational CVaR",capital)
            close(policy["cvar_loss_fraction"],risk/capital,"CVaR capital fraction",1)
            close(policy["expected_net_return"],expected/capital,"net return capital fraction",1)
            _check(policy["eligible"] == policy["trade_guard"]["eligible"],"Financial eligibility flag changed")
            _check(policy["current_action"].keys() == {"buys","sells"},"Current action keys differ")
            projection = policy["current_projection"]
            role_prices = {}
            for code in context["allocation_codes"]:
                priced = str(cash_reference.pricing_day(context["fee_contracts"][code], context["decision_at"]))
                _check(priced in paths["point_paths"][0]["nav"] and code in paths["point_paths"][0]["nav"][priced],
                       "Current point projection has no exact economic execution NAV")
                role_prices[code] = Decimal(str(paths["point_paths"][0]["nav"][priced][code]))
            _check({code: Decimal(value) for code, value in projection["pricing_nav"].items()} == role_prices,
                   "Current projection substituted known valuation for dealing NAV")
            values = {code: sum((Decimal(lot["shares"])*role_prices[code]
                               for lot in projection["lots"] if lot["code"] == code),Decimal(0)) for code in context["allocation_codes"]}
            _check(set(projection["values"]) == set(values),"Current projection asset inventory changed")
            for code,value in values.items():
                close(projection["values"][code],value,"current whole-share source NAV value",capital)
            expected_shares = {lot["lot_id"]: Decimal(lot["shares"]) for lot in context["snapshot"]["positions"]}
            for sale in policy["current_action"]["sells"]:
                _check(sale["lot_id"] in expected_shares,"Current source lot absent")
                expected_shares[sale["lot_id"]] -= Decimal(sale["shares"])
                _check(expected_shares[sale["lot_id"]] >= 0,"Current lot oversold")
            for lot in projection["lots"]:
                if lot.get("lot_id") in expected_shares:
                    close(lot["shares"],expected_shares[lot["lot_id"]],"current source lot share conservation",1)
            for buy in projection["buys"]:
                terms = context["fee_contracts"][buy["code"]]
                quote = cash_reference.entry_quote(Decimal(buy["cash_limit"]), terms, role_prices[buy["code"]])
                close(buy["shares"],quote["shares"],"whole-source current purchase shares",1)
                close(buy["acquired_value"],quote["shares"]*role_prices[buy["code"]],"whole-source current acquired value",capital)
    selected = mpc["selected_policy"]
    if selected is not None:
        verified = [policy for group in mpc["funding_options"] for policy in group["candidates"]
                    if policy["id"] == selected["id"]]
        _check(len(verified) == 1 and fingerprint(selected) == fingerprint(verified[0]),
               "Selected policy differs from the source-verified candidate")
        recovery = mpc.get("decision_status") == "risk_recovery" and selected.get("recovery_eligible") is True
        _check((selected["eligible"] or recovery) and selected["proposed_contribution"] == 0,
               "Selected policy requires eligible confirmed funding")
        _check(mpc["current_action"] == selected["current_action"], "Published current action differs from selected policy")
    for group in mpc["funding_options"]:
        if group.get("best") is not None:
            _check(any(fingerprint(group["best"]) == fingerprint(policy) for policy in group["candidates"]),
                   "Best policy differs from the source-verified candidates")
    if orders:
        _check(selected is not None and (selected["eligible"] or mpc.get("decision_status") == "risk_recovery" and selected.get("recovery_eligible") is True)
            and selected["proposed_contribution"] == 0, "Current orders require eligible confirmed-funding policy")
        buys = [{"code": row["code"], "cash_debit": row["cash_limit"]} for row in orders if row["side"] == "buy"]
        sells = [{"lot_id": row["lot_id"], "shares": row["share_limit"]} for row in orders if row["side"] == "sell"]
        _check(fingerprint({"buys": buys, "sells": sells}) == fingerprint(selected["current_action"]), "Orders differ from frozen MPC current action")
        for order in orders:
            ledger.validate_order(order)
            _check(order["code"] in context["allocation_codes"], "Order outside risk-qualified family")
    from decision_validation import decision_certificate
    certificate = decision_certificate(context, paths, mpc)
    return {"status": "passed", "scope": "source_MPC_current_orders_and_cash_share_flow_not_future_profit", "decision_certificate": certificate}


def verify_report(bundle, report):
    from report_validation import report_semantics
    semantic = report_semantics(bundle, report)
    from report import render
    if report != render(bundle):
        raise EvidenceError("Rendered report differs from the verified bundle")
    return {"status": "passed", "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
            "semantic_validation": semantic}
