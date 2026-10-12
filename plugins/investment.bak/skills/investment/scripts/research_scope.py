"""Source-only acquisition scope; unknown new candidates remain in discovery.

This selects readable CNY/off-exchange source tasks, not optimal investments or
statistically ready funds. Held/order/in-transit risk can never be discarded.
"""
import copy

from contracts import fields, fingerprint, require, utc_now

MODE = "verified_identity_subset_v1"


def current_discovery(discovery_id, store, artifacts, *, as_of=None):
    """The acquisition subset still belongs to a current accepted source run."""
    import fund_universe
    import news
    import verify
    at = as_of or utc_now()
    discovery = store.get("fund_discovery", discovery_id)
    operation = store.operation(discovery_id)
    require(discovery is not None and operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "fund_discover", "Source scope requires a completed public discovery")
    require(operation["result"].get("candidate_ref") == discovery.get("candidate_ref")
            and {key: value for key, value in operation["result"].items() if key != "automatic_validation"} == discovery,
            "Committed source discovery differs from its completed operation receipt")
    payload = operation["request"]["payload"]
    saved = store.get("news-review", payload["news_review_id"])
    require(saved is not None, "Original discovery news review is missing")
    review = {**artifacts.read_json(saved["manifest_ref"]), "manifest_ref": saved["manifest_ref"]}
    spec = {"kind": "numeric_policy", "news": payload["news_policy"]}
    state = news.validate_review(review, spec, at, artifacts, store=store)
    account = store.get("account", "main")
    fund_universe.validate_discovery(discovery, review, verify.monitor_codes(account), artifacts, at,
        monitoring_codes=payload.get("monitoring_codes", []), news_state=state,
        plan_constraints=store.get("plan_constraints", "main"))
    return discovery, account


def _gaps(identity):
    if identity is None:
        return ["original_subject_identity_missing"]
    gaps = []
    if identity.get("currency") != "CNY":
        gaps.append("original_source_CNY_currency_not_verified")
    if identity.get("execution_venue") != "off_exchange_nav":
        gaps.append("original_source_off_exchange_NAV_venue_not_verified")
    if any(row.get("critical_identity") is True for row in identity.get("source_evidence_gaps", [])):
        gaps.append("critical_original_subject_evidence_missing")
    return gaps


def derive(discovery, account, artifacts, *, as_of=None):
    import fund_universe
    import verify
    at = as_of or utc_now()
    identities = fund_universe.verify_identity(artifacts.read_json(discovery["identity_snapshot_ref"]), artifacts, as_of=at)
    held = set(verify.monitor_codes(account))
    original = set(discovery["codes"])
    group_members = {code for group in discovery["groups"] for code in group["members"] if code in identities}
    inventory = original | group_members | held
    examined = inventory
    reasons = {code: _gaps(identities.get(code)) for code in sorted(examined)}
    selected = sorted(code for code in inventory if not reasons[code])
    blocked = sorted(code for code in held if reasons[code])
    excluded = [{"code": code, "reasons": reasons[code], "scope": "unverified_new_source_task_not_a_cannot_buy_finding"}
                for code in sorted(inventory-held) if reasons[code]]
    groups = copy.deepcopy(discovery["groups"])
    group_prices = [{"group_id": group["group_id"], "original_members": list(group["members"]),
        "uncollected_members": sorted(set(group["members"])-set(selected)),
        "original_coverage_status": group["coverage_status"],
        "price_scope_complete": group["coverage_status"].startswith("complete_") and set(group["members"]) <= set(selected)} for group in groups]
    partial = bool(excluded or discovery["status"] != "ready" or any(not row["price_scope_complete"] for row in group_prices))
    gaps = [{"action": "complete_original_held_identity_source", "code": code,
             "reasons": reasons.get(code) or ["held_risk_missing_from_original_discovery"],
             "current_risk_unknown": True} for code in blocked]
    gaps += [{"action": "complete_uncollected_new_candidate_identity_source", **row} for row in excluded]
    if partial and not gaps:
        gaps = [{"action": "complete_original_discovery_group_source_coverage", "original_status": discovery["status"]}]
    return {"schema_id": "research_source_scope_v1", "mode": MODE, "as_of": at,
            "discovery_ref": artifacts.put_json(discovery), "discovery_hash": fingerprint(discovery),
            "identity_snapshot_ref": discovery["identity_snapshot_ref"],
            "account_state_ref": artifacts.put_json(account) if account is not None else None,
            "account_hash": fingerprint(account) if account is not None else None,
            "original_codes": sorted(original), "held_risk_codes": sorted(held),
            "acquisition_inventory_codes": sorted(inventory),
            "original_group_identity_members": sorted(group_members),
            "selected_codes": selected, "excluded_new_candidates": excluded, "blocking_held_codes": blocked,
            "original_groups": groups, "original_coverage": copy.deepcopy(discovery["coverage"]),
            "group_price_coverage": group_prices,
            "status": "needs_source" if blocked else "partial_source_scope" if partial else "source_scope_prepared",
            "trade_ready": False, "all_market_optimality_claimed": False,
            "scope": "original_source_acquisition_only_not_model_group_trade_or_optimality_qualification",
            "required_actions": gaps}


def validate(value, discovery, artifacts):
    """Re-read original bytes and rebuild eligibility independently of derive."""
    import fund_universe
    import verify
    fields(value, {"schema_id", "mode", "as_of", "discovery_ref", "discovery_hash", "identity_snapshot_ref",
        "account_state_ref", "account_hash", "original_codes", "held_risk_codes", "selected_codes",
        "acquisition_inventory_codes", "original_group_identity_members",
        "excluded_new_candidates", "blocking_held_codes", "original_groups", "original_coverage", "group_price_coverage",
        "status", "trade_ready", "all_market_optimality_claimed", "scope", "required_actions"}, label="research source scope")
    require(value["schema_id"] == "research_source_scope_v1" and value["mode"] == MODE, "Unknown research source scope")
    require(artifacts.read_json(value["discovery_ref"]) == discovery and value["discovery_hash"] == fingerprint(discovery)
            and value["identity_snapshot_ref"] == discovery["identity_snapshot_ref"], "Research scope changed original discovery or identity")
    account = artifacts.read_json(value["account_state_ref"]) if value["account_state_ref"] is not None else None
    require(value["account_hash"] == (fingerprint(account) if account is not None else None), "Research scope changed held account")
    identities = fund_universe.verify_identity(artifacts.read_json(discovery["identity_snapshot_ref"]), artifacts, as_of=value["as_of"])
    held = set(verify.monitor_codes(account))
    original = set(discovery["codes"])
    group_members = set()
    for group in discovery["groups"]:
        for code in group["members"]:
            if code in identities:
                group_members.add(code)
    inventory = original | group_members | held
    selected, failed = [], {}
    for code in sorted(inventory):
        identity = identities.get(code)
        reasons = []
        if identity is None:
            reasons = ["original_subject_identity_missing"]
        else:
            if identity.get("currency") != "CNY":
                reasons.append("original_source_CNY_currency_not_verified")
            if identity.get("execution_venue") != "off_exchange_nav":
                reasons.append("original_source_off_exchange_NAV_venue_not_verified")
            if any(row.get("critical_identity") is True for row in identity.get("source_evidence_gaps", [])):
                reasons.append("critical_original_subject_evidence_missing")
        failed[code] = reasons
        if not reasons:
            selected.append(code)
    blocked = sorted(code for code in held if failed[code])
    excluded = [{"code": code, "reasons": failed[code], "scope": "unverified_new_source_task_not_a_cannot_buy_finding"}
                for code in sorted(inventory-held) if failed[code]]
    require(value["original_codes"] == sorted(original) and value["held_risk_codes"] == sorted(held)
            and value["acquisition_inventory_codes"] == sorted(inventory)
            and value["original_group_identity_members"] == sorted(group_members)
            and value["selected_codes"] == selected and value["blocking_held_codes"] == blocked
            and value["excluded_new_candidates"] == excluded, "Research scope deleted risk or changed source eligibility")
    require(value["original_groups"] == discovery["groups"] and value["original_coverage"] == discovery["coverage"],
            "Research scope changed original group coverage")
    coverage = [{"group_id": group["group_id"], "original_members": list(group["members"]),
        "uncollected_members": sorted(set(group["members"])-set(selected)), "original_coverage_status": group["coverage_status"],
        "price_scope_complete": group["coverage_status"].startswith("complete_") and set(group["members"]) <= set(selected)} for group in discovery["groups"]]
    partial = bool(excluded or discovery["status"] != "ready" or any(not row["price_scope_complete"] for row in coverage))
    actions = [{"action": "complete_original_held_identity_source", "code": code,
        "reasons": failed.get(code) or ["held_risk_missing_from_original_discovery"], "current_risk_unknown": True} for code in blocked]
    actions += [{"action": "complete_uncollected_new_candidate_identity_source", **row} for row in excluded]
    if partial and not actions:
        actions = [{"action": "complete_original_discovery_group_source_coverage", "original_status": discovery["status"]}]
    require(value["group_price_coverage"] == coverage and value["required_actions"] == actions
            and value["status"] == ("needs_source" if blocked else "partial_source_scope" if partial else "source_scope_prepared")
            and value["trade_ready"] is False and value["all_market_optimality_claimed"] is False
            and value["scope"] == "original_source_acquisition_only_not_model_group_trade_or_optimality_qualification",
            "Research source scope promoted incomplete evidence")
    return {"status": "partial" if blocked or partial else "passed", "scope": "original_identity_bytes_and_complete_held_risk_inventory",
            "selected_codes": selected, "required_actions": actions}
