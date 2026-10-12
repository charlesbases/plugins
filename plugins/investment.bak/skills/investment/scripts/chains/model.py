"""Evaluate the fixed source bundle through the existing public numerical engine."""
import copy

from contracts import StaleSnapshot, fingerprint, require
from chains.common import binding, bound_result, call, check_control, context, emit, state
from chains.fund_universe import _refs


EVIDENCE_KEYS = ("method_result_ref", "bundle_ref", "calculation_ref", "readiness_ref",
                 "family_results_ref", "selection_ref")


def requested_method():
    from chains.protocol import load_definition
    definition, _ = load_definition()
    return {"family": "elastic_net", "chronological": True,
        "independent_calibration": True, "costs": True, "cvar": True,
        "basis": copy.deepcopy(definition["stages"]["model"]["method_basis"]),
        "scope": "requested_frozen_protocol_method_not_actual_execution_or_qualification"}


def _risk_domain(account, source, holdings):
    """Missing optional metadata inherits facts; explicit contradictions fail."""
    def codes(value, label):
        require(type(value) is list and all(type(code) is str and code.strip() for code in value)
                and len(value) == len(set(value)), label + " must contain distinct actual code identities")
        return set(value)

    held = set(holdings)
    actual = codes(account["risk_codes"], "Account risk_codes") if "risk_codes" in account else held
    require(held <= actual, "Account risk_codes omitted actual current holdings")
    source_risk = codes(source["risk_codes"], "Source risk_codes") if "risk_codes" in source else actual
    require(held <= source_risk and source_risk == actual,
            "Source risk_codes differs from the first account's actual held and pending risk")
    return sorted(held | actual | source_risk)


def _frozen_scope(previous):
    source = previous["payload"]["source_bundle"]
    account = previous["payload"]["account_context"]
    holdings = sorted({row["code"] for row in account["holdings"]})
    risk = _risk_domain(account, source, holdings)
    if source.get("unavailable"):
        return {"unavailable": True,
            "holding_codes": holdings, "risk_codes": risk,
            "candidate_codes": [], "unknown_codes": [], "excluded_codes": [],
            "candidate_scope_unavailable": True,
            "required_actions": copy.deepcopy(source["required_actions"])}
    scope = {key: copy.deepcopy(source[key]) for key in (
        "holding_codes", "candidate_codes", "unknown_codes", "excluded_codes")}
    require(set(scope["holding_codes"]) == set(holdings), "Source holdings differ from the first account's actual H")
    scope["risk_codes"] = risk
    return scope


def model_result(previous, method_ref, result, bundle, artifacts):
    """Project actual evidence; independent scheduler gates own qualification."""
    from chains.gates import inspect_model_evidence
    facts = inspect_model_evidence(bundle, artifacts) if bundle is not None else {
        "model_fit_executed": False, "compared_codes": [], "cash_compared": False,
        "execution_status": "failed", "comparison_gaps": ["Public daily_review did not produce an accepted bundle"],
    }
    evidence = {key: None for key in EVIDENCE_KEYS}
    evidence["method_result_ref"] = method_ref
    evidence["bundle_ref"] = result.get("bundle_ref")
    if bundle is not None:
        for key in EVIDENCE_KEYS[2:]:
            evidence[key] = copy.deepcopy(bundle.get(key))
    gaps = copy.deepcopy(facts["comparison_gaps"])
    gaps.extend(copy.deepcopy(bundle.get("missing", [])) if bundle is not None else [])
    for key, reference in evidence.items():
        if reference is None:
            gaps.append({"field": "evidence_refs." + key, "reason": "No actual bound engine artifact was produced"})
    return {**_frozen_scope(previous), **copy.deepcopy(facts),
        "method": requested_method(), "comparison_gaps": gaps, "evidence_refs": evidence,
        "engine_status": result.get("status"), "reason": bundle.get("reason") if bundle else result.get("message"),
        "scope": "actual_public_engine_evidence_independent_predictive_and_current_financial_gates_still_required"}


def bound_bundle(root, plan, previous):
    """Read precisely this chain's public decision, including its accepted receipt."""
    engine = context(previous)
    require(engine.get("daily_review") is not None, "This chain has no bound public daily_review")
    result = bound_result(root, plan, engine["daily_review"], "daily_review")
    _, artifacts = state(root, plan)
    bundle = artifacts.read_json(result["bundle_ref"])
    snapshot = artifacts.read_json(previous["account_ref"])
    if bundle["account_hash"] != fingerprint(snapshot["ledger_state"]):
        raise StaleSnapshot("The public decision changed this chain's frozen account")
    return result, bundle, artifacts


def calculate(root, plan, previous, *, control_context):
    require(previous["stage"] == "fund_sources", "Model must consume this chain's exact fund sources")
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    engine = context(previous)
    import workflow_requests
    prepared = workflow_requests._prepared(root, plan, engine["workflow_id"], bindings=engine)
    request = workflow_requests._observation_request(prepared)
    request["payload"]["account_snapshot_ref"] = previous["account_ref"]
    if engine.get("discovery") is not None:
        discovery = bound_result(root, plan, engine["discovery"], "fund_discover")
        request["payload"]["discovery_id"] = engine["discovery"]["operation_id"]
        request["payload"]["universe_policy"] = copy.deepcopy(discovery["universe_policy"])
    source = previous["payload"]["source_bundle"]
    industry = None
    if not source.get("unavailable"):
        from chains import industry_sources
        require(source.get("industry_source_binding_ref") is not None,
                "Model requires this chain's accepted industry source binding; pending source work cannot be skipped")
        industry = industry_sources.validate_binding(root, plan, previous["chain_id"], engine,
            source["industry_source_binding_ref"], account_ref=previous["account_ref"])
        require(source.get("industry_source_status") == industry["status"],
                "Model source status differs from independently rebuilt industry sources")
        request["payload"]["benchmark_contracts"] = copy.deepcopy(industry["benchmark_contracts"])
    receipts, method_ref = [], None
    if engine.get("market") is not None:
        bound_result(root, plan, engine["market"], "market_prepare")
        method, receipt, method_ref = call(root, plan, previous["chain_id"], "method", "method_prepare", {
            "workflow_id": engine["workflow_id"], "market_operation_id": engine["market"]["operation_id"]})
        receipts.append(receipt)
        if receipt["status"] != "completed":
            data = model_result(previous, method_ref, method, None, prepared["artifacts"])
            return emit(previous, "model", data, control_context=control_context,
                        engine_context=engine, status="failed", receipts=receipts, source_refs=[method_ref])
        engine["method"] = binding(receipt, method_ref)
        bound_result(root, plan, engine["method"], "method_prepare")
        request["payload"]["method_operation_id"] = receipt["operation_id"]
        if method["status"] == "automatic_strategy_policy_prepared":
            operation = prepared["store"].operation(engine["market"]["operation_id"])
            market_input = operation["request"]["payload"]
            require(market_input["discovery_id"] == engine["discovery"]["operation_id"]
                    and method["discovery_id"] == engine["discovery"]["operation_id"],
                    "Numerical method changed the fixed discovery binding")
            request["payload"].update(spec=copy.deepcopy(method["spec"]),
                discovery_id=engine["discovery"]["operation_id"],
                research=copy.deepcopy(market_input["research"]),
                market_terms=copy.deepcopy(market_input["market_terms"]))
    suffix = "numeric-review" if "spec" in request["payload"] else "observation"
    result, receipt, reference = call(root, plan, previous["chain_id"], suffix,
                                    "daily_review", request["payload"])
    receipts.append(receipt)
    engine["daily_review"] = binding(receipt, reference)
    _, artifacts = state(root, plan)
    bundle = None
    if receipt["status"] == "completed":
        result = bound_result(root, plan, engine["daily_review"], "daily_review")
        bundle = artifacts.read_json(result["bundle_ref"])
        snapshot = artifacts.read_json(previous["account_ref"])
        if bundle["account_hash"] != fingerprint(snapshot["ledger_state"]):
            raise StaleSnapshot("The public model operation changed this chain's frozen account")
    data = model_result(previous, method_ref, result, bundle, artifacts)
    return emit(previous, "model", data, control_context=control_context,
                engine_context=engine, status="needs_evidence" if receipt["status"] == "completed" else "failed",
                receipts=receipts, source_refs=_refs(reference, data["evidence_refs"]))
