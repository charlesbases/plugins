"""Frozen H/peer domain -> original history, actual terms and explicit gaps."""
import copy

from contracts import require, utc_now
from chains.common import binding, bound_result, call, check_control, context, emit, state
from chains.fund_universe import _refs


def _completed_fee_contracts(root, plan, engine, codes):
    """Only explicitly supplied concrete inspections; never a latest scan."""
    _, artifacts = state(root, plan)
    references = []
    for item in engine.get("fee_inspections", []):
        require(type(item) is dict and type(item.get("operation_id")) is str
                and item["operation_id"].startswith(engine["workflow_id"] + ":"),
                "Fee inspection must belong to this exact current chain workflow")
        result = bound_result(root, plan, item, "fee_inspect")
        reference = result.get("contract_ref")
        # A committed inspection may preserve an original contract with gaps
        # in only some actions. The public market gate re-extracts it and
        # determines which actual dealing terms are supported.
        if reference is None:
            continue
        contract = artifacts.read_json(reference)
        require(contract["code"] in codes, "Bound fee contract is outside the frozen actual discovery")
        references.append(reference)
    require(len({artifacts.read_json(item)["code"] for item in references}) == len(references),
            "Multiple fee inspections cannot silently select a latest contract")
    return references


def _source_bundle(previous, research, market, artifacts, diagnostics):
    universe = previous["payload"]["frozen_universe"]
    excluded = copy.deepcopy(universe["excluded_codes"])
    excluded_map = {row["code"]: row for row in excluded}
    risk_codes = previous["payload"]["account_context"].get("risk_codes", universe["holding_codes"])
    inventory = set(risk_codes) | set(universe["holding_codes"]) | set(universe["candidate_codes"]) | set(universe["unknown_codes"]) | set(excluded_map)
    record = market.get("record", {}) if market is not None else {}
    original_market = record.get("market_ref", {})
    terms = {row["code"]: row for row in original_market.get("terms", []) if row.get("fee_contract_ref")}
    prices = original_market.get("prices", {})
    date_map = original_market.get("price_dates", {})
    # The accepted record points to every original; do not repeat the entire
    # all-fund evidence inventory in every per-code row (quadratic payload).
    originals = {"market_record_ref": market.get("record_ref") if market else None,
                 "market_data_ref": market.get("data_ref") if market else None,
                 "nav_ref": record.get("provenance", {}).get("nav_ref")}
    # A large original may itself have many CAS chunks. One small index keeps
    # those exact references once, instead of duplicating them per code.
    source_binding_ref = artifacts.put_json({"schema_id": "investment_market_source_binding_v1", **originals}) if _refs(originals) else None
    proof_refs = [source_binding_ref] if source_binding_ref is not None else []
    qualified, unknown, by_code = [], [], {}
    for code in sorted(inventory):
        if code in excluded_map:
            row = excluded_map[code]
            by_code[code] = {"status": "excluded", "reason": row["reason"], "source_refs": row["source_refs"]}
            continue
        # A nonempty NAV count is acquisition information, not a historical
        # availability or scientific-model qualification. Ready means actual
        # original valuation + applicable independently verified term fields.
        if code in prices and code in date_map and code in terms and proof_refs:
            qualified.append(code)
            code_refs = _refs(proof_refs, terms[code]["fee_contract_ref"])
            by_code[code] = {"status": "qualified", "source_refs": code_refs,
                "price_date": date_map[code], "currency": "CNY",
                "engine_refs": _refs(terms[code].get("fee_contract_ref"), source_binding_ref),
                "scope": "current_source_verified_NAV_and_applicable_terms_not_predictive_validation"}
        else:
            unknown.append(code)
            missing = []
            if code not in prices or code not in date_map:
                missing.append("actual source NAV and its actual date unavailable")
            if code not in terms:
                missing.append("applicable original issuer and TT dealing contract not independently verified")
            if not proof_refs:
                missing.append("immutable accepted market/source evidence reference unavailable")
            by_code[code] = {"status": "unknown", "reason": "; ".join(missing),
                "source_refs": _refs(universe["engine_refs"], proof_refs),
                "scope": "retained_original_candidate_or_held_risk_not_an_ineligible_fund_finding"}
    return {"holding_codes": copy.deepcopy(universe["holding_codes"]),
        "risk_codes": copy.deepcopy(risk_codes),
        "candidate_codes": copy.deepcopy(universe["candidate_codes"]),
        "qualified_codes": qualified, "unknown_codes": unknown, "excluded_codes": excluded,
        "by_code": by_code,
        "cash": {"currency": "CNY", "amount": previous["payload"]["account_context"]["cash"],
                 "source_refs": [previous["account_ref"]]},
        "evaluation_at": utc_now(),
        "historical_availability_refs": proof_refs,
        "dealing_terms_refs": _refs([terms[code].get("fee_contract_ref") for code in sorted(terms)]),
        "engine_refs": _refs(universe["engine_refs"], proof_refs),
        "market_source_binding_ref": source_binding_ref,
        "research_readiness": copy.deepcopy(research.get("readiness")) if research is not None else None,
        "diagnostics": diagnostics,
        "scope": "all_frozen_H_and_peer_dispositions_preserved_original_sources_not_a_best_fund_or_prediction_acceptance"}


def prepare(root, plan, previous, *, control_context, source_submission=None):
    require(previous["stage"] == "fund_universe", "Fund sources must consume this chain's frozen universe")
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    engine = context(previous)
    require(engine.get("workflow_id") == previous["chain_id"], "Fund sources must use this exact current chain workflow")
    _, artifacts = state(root, plan)
    if previous["payload"]["frozen_universe"].get("unavailable") is True:
        holding_codes = sorted({row["code"] for row in previous["payload"]["account_context"]["holdings"]})
        risk_codes = previous["payload"]["account_context"].get("risk_codes", holding_codes)
        candidate_codes = previous["payload"]["frozen_universe"].get("candidate_codes")
        return emit(previous, "fund_sources", {"unavailable": True,
            "holding_codes": holding_codes, "candidate_codes": copy.deepcopy(candidate_codes),
            "risk_codes": copy.deepcopy(risk_codes),
            "known_codes": sorted(set(risk_codes) | set(holding_codes) | set(candidate_codes or [])),
            "qualified_codes": [], "unknown_codes": sorted(set(risk_codes) | set(holding_codes) | set(candidate_codes or [])),
            "scope": "actual_current_H_and_only_already_known_candidates_peer_denominator_not_yet_available",
            "required_actions": [{"action": "complete_this_chain_original_fund_universe_before_source_comparison"}],
            "upstream_diagnostics": copy.deepcopy(previous["payload"]["frozen_universe"])},
            engine_context=engine, control_context=control_context, source_refs=previous["source_refs"])
    from chains import industry_sources
    industry = industry_sources.prepare(root, plan, previous, source_submission=source_submission)
    industry_fields = {key: copy.deepcopy(industry[key]) for key in (
        "source_task_ref", "source_submission_template_ref", "industry_source_binding_ref",
        "industry_source_status", "industry_source_required_actions")}
    engine["industry_source_task_ref"] = industry["source_task_ref"]
    if industry["status"] == "awaiting_analysis":
        data = _source_bundle(previous, None, None, artifacts, {"industry_source_work": industry_fields})
        data.update(industry_fields)
        return emit(previous, "fund_sources", data, engine_context=engine, status="awaiting_analysis",
                    source_refs=_refs(industry_fields), control_context=control_context)
    engine["industry_source_binding_ref"] = industry["industry_source_binding_ref"]
    supplied_fees = industry.get("fee_inspections", [])
    inherited_fees = engine.get("fee_inspections", [])
    if supplied_fees:
        require(not inherited_fees or inherited_fees == supplied_fees,
                "Current source submission changed already bound fee inspections")
        engine["fee_inspections"] = copy.deepcopy(supplied_fees)
    discovery = bound_result(root, plan, engine["discovery"], "fund_discover")
    import workflow_requests
    prepared = workflow_requests._prepared(root, plan, engine["workflow_id"], bindings=engine)
    built = workflow_requests._research_request(prepared)
    diagnostics = {"research_request": copy.deepcopy(built)}
    receipts, research, market = [], None, None
    if built["status"] == "research_request_prepared":
        research, receipt, reference = call(root, plan, previous["chain_id"], "research", "research_prepare", built["request"]["payload"])
        receipts.append(receipt)
        if receipt["status"] == "completed":
            engine["research"] = binding(receipt, reference)
            bound_result(root, plan, engine["research"], "research_prepare")
            # This is a source operation, even with term gaps; the original
            # controller preserves genuine NAV dates and refuses fake prices.
            research_parameters = {key: value for key, value in built["request"]["payload"].items()
                                   if key not in {"discovery_id", "source_scope"}}
            contract_refs = _completed_fee_contracts(root, plan, engine, set(discovery["codes"]))
            market, receipt, reference = call(root, plan, previous["chain_id"], "market", "market_prepare", {
                "discovery_id": engine["discovery"]["operation_id"], "research": research_parameters,
                "market_terms": {"currency": "CNY", "contracts": contract_refs},
            })
            receipts.append(receipt)
            if receipt["status"] == "completed":
                engine["market"] = binding(receipt, reference)
                bound_result(root, plan, engine["market"], "market_prepare")
            else:
                diagnostics["market_result_ref"] = reference
        else:
            diagnostics["research_result_ref"] = reference
    engine["source_diagnostics_ref"] = artifacts.put_json(diagnostics)
    data = _source_bundle(previous, research, market, artifacts, diagnostics)
    data.update(industry_fields)
    return emit(previous, "fund_sources", data, engine_context=engine,
                receipts=receipts, source_refs=_refs(data["engine_refs"], engine["source_diagnostics_ref"], industry_fields),
                control_context=control_context)
