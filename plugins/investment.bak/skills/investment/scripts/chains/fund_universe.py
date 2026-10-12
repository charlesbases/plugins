"""Current assessed industries -> the complete frozen source comparison domain."""
import copy

from contracts import require
from chains.common import binding, bound_result, check_control, context, emit, state


def _refs(*values):
    """Extract concrete CAS references from native producer evidence structures."""
    rows = {}

    def visit(value):
        if type(value) is dict:
            if {"sha256", "size"} <= value.keys() and ("path" in value or "chunks" in value):
                rows[value["sha256"]] = copy.deepcopy(value)
            else:
                for item in value.values():
                    visit(item)
        elif type(value) is list:
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return [rows[key] for key in sorted(rows)]


def _typed(previous, result, artifacts):
    catalog = artifacts.read_json(result["catalog_ref"])
    catalog_codes = sorted(catalog["entries"])
    pending = artifacts.read_json(result["pending_scopes_ref"])
    holdings = sorted({row["code"] for row in previous["payload"]["account_context"]["holdings"]})
    risk_codes = previous["payload"]["account_context"].get("risk_codes", holdings)
    industry_ids = {row["industry_id"] for row in previous["payload"]["assessed_news"]["industry_links"]}
    exposures = result["exposures"]
    source_refs = _refs(result["catalog_ref"], result["identity_snapshot_ref"], result["candidate_ref"])
    groups, members, linked = [], set(), set()
    for original in result["groups"]:
        codes = set(original["members"]) | set(pending[original["pending_code_hash"]])
        supported = {row["thesis_id"] for code in original["members"] for row in exposures.get(code, [])}
        supported &= industry_ids
        linked |= supported
        members |= codes
        groups.append({"group_id": original["group_id"], "industry_ids": sorted(supported),
            "member_codes": sorted(codes),
            "membership_complete": original["coverage_status"].startswith("complete_"),
            "source_refs": _refs(original["membership_evidence"], source_refs),
            "native_group": copy.deepcopy(original),
            "pending_members_scope": "possible_original_peers_not_verified_member_assertion"})
    # Retain all unexamined subjects in the producer's dynamically derived
    # scope. This explicitly unresolved domain cannot authorize a best claim.
    unresolved = set(result["coverage"]["scope_codes"]) - members
    if unresolved or industry_ids - linked:
        possible = unresolved or set(result["coverage"]["scope_codes"])
        members |= possible
        groups.append({"group_id": "unresolved-original-industry-domain", "industry_ids": sorted(industry_ids - linked),
            "member_codes": sorted(possible), "membership_complete": False,
            "source_refs": source_refs,
            "pending_members_scope": "whole_current_source_scope_possible_members_unknown_not_a_factual_peer_group"})
    eligible = set(result["completed_group_candidate_codes"])
    candidates, unknown, excluded = [], [], []
    for code in sorted(members):
        prequalification = result["prequalification"].get(code)
        if code in eligible and code in catalog_codes:
            candidates.append(code)
        elif prequalification is not None and not prequalification["eligible"] and code in catalog_codes and code not in risk_codes:
            excluded.append({"code": code, "reason": "; ".join(prequalification["reasons"]),
                "source_refs": source_refs, "native_prequalification": copy.deepcopy(prequalification),
                "scope": "current_source_identity_and_thesis_admission_not_forecast_ranking"})
        else:
            unknown.append(code)
    return {"currency": "CNY", "platform": "TT", "catalog_codes": catalog_codes,
        "catalog_ref": result["catalog_ref"], "holding_codes": holdings,
        "risk_codes": copy.deepcopy(risk_codes),
        "candidate_codes": candidates, "unknown_codes": unknown, "excluded_codes": excluded,
        "peer_groups": groups, "engine_refs": _refs(result["candidate_ref"], result["identity_snapshot_ref"], result["pending_scopes_ref"]),
        "coverage": copy.deepcopy(result["coverage"]), "required_actions": copy.deepcopy(result["required_actions"]),
        "scope": "whole_current_TT_catalog_and_complete_or_explicitly_unresolved_original_peer_denominator_not_best_fund_qualification"}


def discover(root, plan, previous, *, control_context):
    require(previous["stage"] == "news_assessment", "Fund universe must consume this chain's assessed news")
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    engine = context(previous)
    require(engine.get("workflow_id") == previous["chain_id"], "Fund discovery must use this exact current chain workflow")
    # Reading these exact references verifies the received run and analysis;
    # the workflow controller is never allowed to select a latest review.
    bound_result(root, plan, engine["news_review"], "news_assess")
    import workflow_discovery
    controller = workflow_discovery.discover(root, plan, workflow_id=engine["workflow_id"], bindings=engine)
    store, artifacts = state(root, plan)
    controller_ref = artifacts.put_json(controller)
    engine["discovery_controller_ref"] = controller_ref
    identity = controller.get("discovery_id")
    if identity is None:
        return emit(previous, "fund_universe", {"unavailable": True,
            "required_actions": controller.get("required_actions") or [{"action": "complete_current_original_fund_catalog_discovery"}],
            "diagnostics_ref": controller_ref}, engine_context=engine, status="needs_evidence", source_refs=[controller_ref],
            control_context=control_context)
    operation = store.operation(identity)
    require(operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "fund_discover",
            "The discovery controller did not return an accepted current public operation")
    result_ref = artifacts.put_json(operation["result"])
    receipt = {"operation_id": identity, "operation": "fund_discover", "status": "completed", "source_ref": result_ref}
    engine["discovery"] = binding(receipt, result_ref)
    result = bound_result(root, plan, engine["discovery"], "fund_discover")
    data = _typed(previous, result, artifacts)
    data["controller_ref"] = controller_ref
    return emit(previous, "fund_universe", data, engine_context=engine,
                receipts=[receipt], source_refs=_refs(result_ref, controller_ref, data["catalog_ref"]),
                control_context=control_context)
