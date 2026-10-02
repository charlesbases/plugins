"""Source-qualified comparison scopes and explicit purchase admission.

Ordering is for scheduling, never a performance rank. Unknown evidence remains
unknown; source and operational facts do not become an invented alpha score.
"""
import copy
import datetime as dt
from decimal import Decimal

from contracts import StaleSnapshot, fields, fingerprint, instant, require
from fee_contract import source_holding_ages


def validate_universe_policy(policy):
    fields(policy, {"market", "platform", "currency", "supported_instruments", "comparison_groups",
                    "require_complete_group"}, label="universe policy")
    require(policy["market"] == "CN_public_funds" and policy["platform"] == "TT" and policy["currency"] == "CNY",
            "Only the declared CNY TT fund market is implemented")
    require(policy["supported_instruments"] == ["off_exchange_nav"], "Unsupported instrument pricing or execution model")
    groups = policy["comparison_groups"]
    require(type(groups) is list and groups and len(set(groups)) == len(groups)
            and set(groups) <= {"same_benchmark", "same_legal_fund"}, "Explicit comparable group rules required")
    require(policy["require_complete_group"] is True, "Partial comparison groups cannot qualify new purchases")
    return fingerprint(policy)


def validate_plan_constraints(record):
    import risk_profile
    risk_profile.validate_plan(record)
    return fingerprint(record)


class DiscoveryPlanChanged(StaleSnapshot):
    def __init__(self, previous, current):
        super().__init__("Discovery belongs to a different confirmed plan; collect a new discovery revision")
        self.required_actions = [{"action": "fund_discover", "reason": "plan_constraints_changed",
            "previous_plan_constraints_hash": previous, "current_plan_constraints_hash": current,
            "required_output": "new request_id bound to the current confirmed plan; preserve the old discovery"}]


def require_current_plan(discovery, plan_constraints):
    current = validate_plan_constraints(plan_constraints)
    if discovery.get("plan_constraints_hash") != current:
        raise DiscoveryPlanChanged(discovery.get("plan_constraints_hash"), current)
    return current


def identity_constraints(identity, universe_policy, plan_constraints, *, category_labels):
    validate_universe_policy(universe_policy)
    validate_plan_constraints(plan_constraints)
    constraints = plan_constraints["constraints"]
    reasons = []
    if identity.get("source_evidence_gaps"):
        reasons.append("pending_requested_issuer_evidence")
    for name, required in (("currency", constraints["currency"]), ("dealing_currency", constraints["currency"]),
                           ("execution_venue", "off_exchange_nav"), ("instrument_type", "off_exchange_nav")):
        if identity.get(name) is None:
            reasons.append("unknown_"+name)
        elif identity[name] != required:
            reasons.append("unsupported_"+name)
    if identity.get("share_class") == "Y":
        reasons.append("pension_share_investor_eligibility_not_established")
    code = identity["code"]
    excluded = set(constraints["excluded_categories"])
    categories = {identity.get("asset_class"), identity.get("type")}
    if excluded & categories:
        reasons.append("category_excluded_by_plan")
    exposure = identity.get("sector_exposures") or {}
    measured = exposure.get("weights", {})
    if any(Decimal(str(measured.get(sector, "0"))) > 0 for sector in excluded):
        reasons.append("sector_excluded_by_plan")
    # Exact product-category labels come from the current source directory.
    # Historical holdings (including a reported zero) cannot prove today's
    # absence of an industry, nor map a broad industry to a narrower one.
    unresolved_exclusions = excluded - set(category_labels)
    if unresolved_exclusions:
        reasons.append("cannot_verify_all_plan_category_exclusions")
    for restriction in identity.get("company_restrictions") or []:
        if restriction.get("effect") in ("subscription_prohibited", "operation_suspended"):
            reasons.append("source_disclosed_company_restriction")
    return sorted(set(reasons))


def build_groups(catalog_entries, identities, universe_policy):
    """Complete means a frozen catalogue class or an explicit issuer member list.

    A verified issuer list closes only that declared share-class scope. It never
    establishes all-market or all-index superiority. Unknown catalogue entries
    prevent closing a broad benchmark group.
    """
    validate_universe_policy(universe_policy)
    group_identities = {code: row for code, row in identities.items()
                        if not any(gap["critical_identity"] for gap in row.get("source_evidence_gaps", []))}
    groups = {}
    for code, identity in group_identities.items():
        for kind in universe_policy["comparison_groups"]:
            identity_key = identity.get("fund_group_id") if kind == "same_legal_fund" else identity.get("benchmark_id")
            if not identity_key:
                continue
            key = fingerprint([kind, identity_key, identity.get("asset_class"), identity.get("currency"),
                               identity.get("hedge_policy")])
            group = groups.setdefault(key, {"group_id": key, "kind": kind, "identity_key": identity_key,
                                           "members": [], "coverage_status": "pending", "pending_codes": [],
                                           "membership_evidence": [], "coverage_scope": None})
            group["members"].append(code)
    for group in groups.values():
        group["members"].sort()
        sample = identities[group["members"][0]]
        declarations = [identities[code].get("group_membership") for code in group["members"]]
        declarations = [value for value in declarations if value and value.get("kind") == group["kind"]]
        if declarations:
            require(all({key: value[key] for key in ("kind", "codes", "scope")} ==
                        {key: declarations[0][key] for key in ("kind", "codes", "scope")} for value in declarations),
                    "Conflicting source group member lists")
            declared = declarations[0]
            required = sorted(declared["codes"])
            group["pending_codes"] = sorted(set(required)-set(group["members"]))
            require(set(group["members"]) <= set(required), "Observed group member is missing from issuer's complete list")
            if not group["pending_codes"] and set(required) <= set(catalog_entries):
                group.update(coverage_status="complete_declared_scope", coverage_scope=declared["scope"],
                             membership_evidence=[copy.deepcopy(ref) for value in declarations for ref in value["evidence_refs"]])
        else:
            # Unknown categories cannot be declared irrelevant to this group.
            category = catalog_entries[sample["code"]].get("type")
            possible = {code for code, row in catalog_entries.items()
                        if not category or not row.get("type") or row["type"] == category}
            unknown = possible-set(group_identities)
            group["pending_codes"] = sorted(unknown)
            if not unknown and (group["kind"] == "same_legal_fund" or
                                all(identities[code].get("hedge_policy") is not None for code in group["members"])):
                group.update(coverage_status="complete_catalog_category", coverage_scope="all_members_of_frozen_catalog_category_classified")
    return sorted(groups.values(), key=lambda value: value["group_id"])


def finalize_selection(discovery, code_info, terms, plan_constraints, as_of, *, max_terms_age_seconds):
    """Finalize only after controller-verified fee contracts become available."""
    require(discovery.get("schema_version") == 4, "Current discovery contract required")
    validate_universe_policy(discovery["universe_policy"])
    constraints_hash = require_current_plan(discovery, plan_constraints)
    constraints = plan_constraints["constraints"]
    require(type(max_terms_age_seconds) is int and max_terms_age_seconds >= 0,
            "Frozen nonnegative dealing-quote age policy required")
    cutoff = instant(as_of)
    terms_policy = {"rule": "source_quote_observed_before_cutoff", "max_terms_age_seconds": max_terms_age_seconds}
    terms_by_code = {row["code"]: row for row in terms}
    require(len(terms_by_code) == len(terms), "Duplicate dealing terms")
    complete = {group["group_id"]: group for group in discovery["groups"]
                if group["coverage_status"].startswith("complete_")}
    candidates = set(discovery["completed_group_candidate_codes"])
    identities = copy.deepcopy(discovery["comparison_identities"])
    for code, identity in code_info.items():
        require(code not in identities or identities[code] == identity, "Research identity differs from its comparison source")
        identities[code] = identity
    per_code, eligible = {}, []
    for code in sorted(identities):
        identity = identities[code]
        reasons = identity_constraints(identity, discovery["universe_policy"], plan_constraints,
                                       category_labels=discovery["category_labels"])
        roles = [name for name, values in (("held", discovery["held_codes"]), ("monitoring", discovery["monitoring_codes"]),
                                          ("candidate", discovery["completed_group_candidate_codes"])) if code in values]
        group_ids = [key for key, group in complete.items() if code in group["members"]]
        if code not in candidates:
            reasons.append("not_an_active_complete_group_candidate")
            reasons.extend(discovery["prequalification"].get(code, {}).get("reasons", []))
        if not group_ids:
            reasons.append("comparison_group_incomplete")
        term = terms_by_code.get(code)
        contract = term.get("fee_contract") if term else None
        if (not contract or not term.get("fee_contract_ref")) and code in candidates:
            reasons.append("verified_dealing_contract_required")
        elif contract:
            if term.get("observed_at") is None:
                reasons.append("unknown_dealing_quote_time")
            else:
                age = (cutoff-instant(term["observed_at"])).total_seconds()
                if age < 0:
                    reasons.append("future_dealing_quote")
                elif age > max_terms_age_seconds:
                    reasons.append("stale_dealing_quote")
            subject = contract["subject"]
            if subject["code"] != code or subject["currency"] != identity.get("currency"):
                reasons.append("dealing_subject_differs_from_identity")
            if subject.get("investor_type") != "retail":
                reasons.append("contract_investor_scope_not_established_for_current_plan")
            if identity.get("share_class") and subject["share_class"] != identity["share_class"]:
                reasons.append("dealing_share_class_differs")
            if subject["channel"] not in ("天天基金", "天天基金销售", "TT"):
                reasons.append("dealing_channel_outside_plan")
            if not contract["buyable"]:
                reasons.append("subscription_unavailable")
            if contract.get("action_evidence", {}).get("buy", {}).get("status") == "needs_research":
                reasons.append("current_purchase_source_evidence_incomplete")
            if subject["channel"] not in (constraints["platform"], "天天基金", "天天基金销售"):
                reasons.append("dealing_channel_outside_plan")
            # Fee sensitivity ages describe cost diagnostics, not a promised
            # liquidation deadline. Source locks remain enforced by the exact
            # action clock and terminal wealth valuation.
        permitted = not reasons
        if permitted:
            eligible.append(code)
        per_code[code] = {"roles": roles, "eligible": permitted, "reasons": sorted(set(reasons)), "comparison_group_ids": group_ids,
                          "thesis_ids": [row["thesis_id"] for row in discovery["exposures"].get(code, [])],
                          "identity_scope": identity["identity_scope"], "currency": identity.get("currency"),
                          "execution_venue": identity.get("execution_venue"),
                          "exposure": copy.deepcopy(identity.get("sector_exposures")),
                          "due_diligence": {"manager_tenures": copy.deepcopy(identity.get("manager_tenures")),
                                            "company_restrictions": copy.deepcopy(identity.get("company_restrictions")),
                                            "manager_skill_estimated": False,
                                            "status": "source_facts_only_no_unobserved_quality_score"},
                          "unknown_fields": copy.deepcopy(identity.get("unknown_fields", [])),
                          "cost_evidence": None if not term else term.get("fee_contract_ref"),
                          "cost_scenarios": {str(day): {"status": "portfolio_model_required",
                              "reason": "needs_actual_cash_NAV_holding_basis_and_future_exit_scenarios"}
                              for day in source_holding_ages(contract)} if contract else {}}
    comparisons = [{"group_id": group["group_id"], "kind": group["kind"], "members": group["members"],
                    "qualified_members": [code for code in group["members"] if code in eligible],
                    "coverage_status": group["coverage_status"], "coverage_scope": group["coverage_scope"],
                    "method": "same_scope_net_cash_risk_comparison_in_portfolio_model_no_fixed_factor_score",
                    "manager_skill_estimated": False, "all_market_optimality_claimed": False}
                   for group in discovery["groups"]]
    result = {"schema_version": 4, "discovery_hash": fingerprint(discovery),
              "plan_constraints_hash": constraints_hash, "terms_hash": fingerprint(terms), "as_of": as_of,
              "terms_policy": terms_policy, "terms_policy_hash": fingerprint(terms_policy),
              "eligible_buy_codes": sorted(eligible), "per_code": per_code, "peer_comparisons": comparisons,
              "selection_scope": "completed_declared_comparison_groups_only", "performance_rank": None}
    return {**result, "selection_hash": fingerprint(result)}


def attach_quality(selection, quality, admission):
    """Attach verified descriptive evaluation without granting trading alpha.

    The controller must validate quality against its original contracts first;
    hashes here bind the annotation, not assert source or statistical truth.
    """
    require(selection.get("selection_hash") == fingerprint({key: value for key, value in selection.items()
                                                            if key != "selection_hash"}), "Selection annotation input changed")
    require(quality.get("quality_hash") == fingerprint({key: value for key, value in quality.items()
                                                       if key != "quality_hash"}), "Quality annotation input changed")
    require(quality.get("selection_role") == "evaluation_only_no_EN_return_addition", "Quality cannot add forecast returns")
    require(isinstance(admission, dict) and set(admission) == {"excluded_new_codes", "source_reasons"}, "Source-derived quality admission contract required")
    require(set(admission["excluded_new_codes"]) <= set(selection["per_code"]), "Quality admission refers outside source selection")
    value = copy.deepcopy(selection)
    value.pop("selection_hash")
    for code, result in value["per_code"].items():
        product = quality["products"].get(code)
        if product is None:
            continue
        diligence = result["due_diligence"]
        diligence.update(quality_readiness=product["readiness"], quality_hash=quality["quality_hash"],
                         quality_required_actions=copy.deepcopy(product["required_actions"]),
                         factor_alpha_estimated=any(row["status"] == "estimated" for row in
                             product.get("statistics", {}).get("manager_regimes", [])),
                         manager_skill_proven=False)
        diligence["status"] = "source_facts_and_model_dependent_descriptive_quality"
        if code in admission["excluded_new_codes"]:
            result["eligible"] = False
            result["reasons"] = sorted(set(result["reasons"] + admission["source_reasons"].get(code, [])))
    value["quality_evaluation"] = {"quality_hash": quality["quality_hash"], "status": quality["status"],
                                   "selection_role": quality["selection_role"], "scope": quality["scope"],
                                   "peer_comparisons": copy.deepcopy(quality["peer_comparisons"])}
    value["eligible_buy_codes"] = [code for code in selection["eligible_buy_codes"] if code not in admission["excluded_new_codes"]]
    value["quality_admission"] = copy.deepcopy(admission)
    return {**value, "selection_hash": fingerprint(value)}
