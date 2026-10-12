"""Independent current-chain evidence gates; producer flags grant no authority."""
import copy
from decimal import Decimal

from artifacts import Artifacts
from contracts import EvidenceError, StaleSnapshot, fingerprint, instant, utc_now
from state_store import Store
from chains import contracts, protocol
from chains.common import check_control
import stage_validation
import verify

VALIDATOR_ID = "investment:business-chain-independent-gate-v1"
VALIDATOR_SCOPE = "current_chain_actual_engine_acceptance_and_stage_business_postconditions_not_future_profit"
BINDING_FIELDS = ("chain_id", "stage", "account_ref", "account_hash", "input_output_hash",
                  "output_hash", "protocol_sha256", "implementation_identity")


class GateFailure(EvidenceError):
    code = "invalid_chain_evidence"

    def __init__(self, stage, phase, rule_path, reason):
        super().__init__(reason)
        self.stage, self.phase, self.rule_path = stage, phase, rule_path
        self.required_actions = [{"action": "resolve_independent_chain_evidence", "stage": stage,
                                  "phase": phase, "rule_path": rule_path, "reason": reason}]


def _check(condition, stage, rule, reason, phase="output_validation"):
    if not condition:
        raise GateFailure(stage, phase, rule, reason)


def _value(artifacts, reference):
    return artifacts.read_json(reference) if reference is not None else None


def inspect_model_evidence(bundle, artifacts):
    """Read executed facts only; this helper cannot authenticate qualification.

    A populated forecast is evidence of fitting, not of a complete scientific
    comparison. Compared codes require actual MPC evaluation on fitted paths.
    Cash requires an evaluated source baseline, never just a cash balance.
    """
    facts = {"model_fit_executed": False, "compared_codes": [], "cash_compared": False,
             "execution_status": "not_run", "comparison_gaps": []}
    if not bundle:
        facts["comparison_gaps"] = [{"action": "complete_current_source_bound_model_evaluation"}]
        return facts
    evaluations = []
    if bundle.get("calculation_ref"):
        evaluations.append((_value(artifacts, bundle["calculation_ref"]), bundle.get("context")))
    families = _value(artifacts, bundle.get("family_results_ref")) or {}
    for row in families.get("results", []):
        evaluations.append((_value(artifacts, row.get("calculation_ref")),
                            _value(artifacts, row.get("context_ref"))))
    compared = set()
    for calculation, context in evaluations:
        if not calculation or not context:
            continue
        fitted = calculation.get("fitting") or {}
        forecasts = fitted.get("forecasts") or []
        actual_fit = bool(fitted.get("models") and forecasts)
        facts["model_fit_executed"] |= actual_fit
        mpc = calculation.get("mpc") or {}
        frozen = mpc.get("frozen_policy_family") or mpc.get("preselection") or {}
        evaluated = (bundle.get("calculation_ref") is not None and calculation == evaluations[0][0]
                     and actual_fit and fitted.get("status") == "research_ready"
                     and calculation.get("paths", {}).get("status") == "research_ready"
                     and frozen.get("status") == "frozen" and bool(mpc.get("funding_options")))
        if not evaluated:
            facts["comparison_gaps"].extend(calculation.get("required_actions", []))
            continue
        fit_codes = {row["code"] for row in forecasts}
        compared.update(fit_codes & set(context.get("allocation_codes", [])))
        for option in mpc["funding_options"]:
            for candidate in option.get("candidates", []):
                point, paths = candidate.get("point"), candidate.get("selection")
                if point is None or not paths:
                    continue
                action, rule = candidate.get("current_action", {}), candidate.get("future_rule", {})
                # Holding existing fund units is a same-funded hold baseline,
                # not the mandatory cash alternative. Cash paths contain no
                # retained fund units and no future fund purchase allocation.
                pure_cash = (not action.get("buys") and not rule.get("weights")
                    and all("terminal_lots" in path and not any(Decimal(lot["shares"])
                        for lot in path["terminal_lots"]) for path in [point, *paths]))
                if pure_cash:
                    facts["cash_compared"] = True
    facts["compared_codes"] = sorted(compared)
    if facts["model_fit_executed"]:
        facts["execution_status"] = "unqualified" if compared else "insufficient_data"
    elif evaluations or bundle.get("readiness_ref"):
        facts["execution_status"] = "insufficient_data"
    if not facts["model_fit_executed"]:
        facts["comparison_gaps"].append({"action": "obtain_actual_mature_PIT_model_fit"})
    elif not compared or not facts["cash_compared"]:
        facts["comparison_gaps"].append({"action": "complete_actual_peer_and_cash_policy_comparison"})
    return facts


def _operation(store, artifacts, binding, expected, chain_id, stage):
    _check(type(binding) is dict and {"operation_id", "result_ref"} <= set(binding), stage,
           "operation.binding", "Actual operation id and immutable result reference are required")
    identity = binding["operation_id"]
    _check(identity.startswith(chain_id + ":"), stage, "operation.current_chain",
           "A previous invocation operation cannot enter this chain")
    saved = store.operation(identity)
    _check(saved is not None and saved["status"] == "completed", stage, "operation.completed",
           "The exact public operation has not completed")
    _check(saved["request"]["operation"] == expected, stage, "operation.type",
           "The bound public operation has a different type")
    result = _value(artifacts, binding["result_ref"])
    _check(result == saved["result"], stage, "operation.canonical_result",
           "Result CAS differs from the canonical public operation")
    automatic = result.get("automatic_validation")
    _check(type(automatic) is dict, stage, "operation.independent_acceptance",
           "A producer receipt without independent accepted-attempt proof is insufficient",
           "independent_qualification")
    bindings = _value(artifacts, automatic["operation_bindings_ref"])
    proof = stage_validation.validate_operation_result(saved["request"], result, store, artifacts,
                                                       {**bindings, "historical": True})
    return {"operation_id": identity, "operation": expected, "result": result, "proof": proof,
            "result_ref": binding["result_ref"], "request": saved["request"]}


def _operations(output, store, artifacts):
    """Authenticate every supplied receipt, including honest failed operations."""
    authenticated = {}
    stage = output["stage"]
    for row in output["operation_receipts"]:
        saved = store.operation(row["operation_id"])
        _check(saved is not None and saved["request"]["operation"] == row["operation"], stage,
               "operation_receipts.operation", "Receipt does not identify its actual operation")
        _check(row["operation_id"].startswith(output["chain_id"] + ":"), stage,
               "operation_receipts.chain", "Receipt belongs to another invocation")
        reference = row.get("source_ref")
        _check(reference is not None and _value(artifacts, reference) == saved["result"], stage,
               "operation_receipts.source_ref", "Receipt requires the exact canonical result CAS")
        expected_status = "completed" if saved["status"] == "completed" else "failed"
        _check(row["status"] == expected_status, stage, "operation_receipts.status",
               "An incomplete or failed operation cannot be labeled completed")
        if "result_hash" in row:
            _check(row["result_hash"] == fingerprint(saved["result"]), stage,
                   "operation_receipts.result_hash", "Canonical result hash changed")
        if expected_status == "completed":
            authenticated[row["operation_id"]] = _operation(store, artifacts,
                {"operation_id": row["operation_id"], "result_ref": reference}, row["operation"],
                output["chain_id"], stage)
    return authenticated


def _engine(output, key, expected, store, artifacts, authenticated, *, required=False):
    binding = output["payload"].get("engine_context", {}).get(key)
    if binding is None:
        _check(not required, output["stage"], "engine_context." + key,
               "This business result requires its exact " + expected + " operation")
        return None
    native = authenticated.get(binding.get("operation_id"))
    if native is None:
        native = _operation(store, artifacts, binding, expected, output["chain_id"], output["stage"])
        authenticated[native["operation_id"]] = native
    _check(native["operation"] == expected and native["result_ref"] == binding["result_ref"],
           output["stage"], "engine_context." + key, "Engine binding differs from its authenticated result")
    return native


def _lineage(output, previous, artifacts, control):
    values = [_value(artifacts, ref) for ref in output["lineage_refs"]]
    _check(len(values) == contracts.STAGES.index(output["stage"]), output["stage"],
           "lineage.length", "Every earlier current-chain envelope must be retained")
    for index, value in enumerate(values):
        _check(value["stage"] == contracts.STAGES[index] and value["chain_id"] == output["chain_id"],
               output["stage"], "lineage.order", "Lineage contains a skipped or different-chain stage")
        _check(all(value[field] == output[field] for field in (
            "account_ref", "account_hash", "protocol_sha256", "implementation_identity")),
            output["stage"], "lineage.identity", "Lineage account or frozen control identity changed")
        contracts.validate_envelope(value, previous=values[index - 1] if index else None,
                                    allow_partial=control["delivery_mode"] == "observation")
    if previous is not None:
        _check(values[-1] == previous and output["lineage_refs"][-1] == control["previous_output_ref"],
               output["stage"], "lineage.previous", "Lineage does not end at the exact received predecessor")
    return values


def _current_account(output, lineage, store, artifacts):
    from chains.account import assert_current_account
    first = output if output["stage"] == "account" else lineage[0]
    assert_current_account(store.root, store.plan, first)
    if output["account_ref"] is not None:
        frozen = _value(artifacts, output["account_ref"])
        account = frozen["ledger_state"]
        if account is not None:
            stage_validation._replay_account(store, artifacts)
        _check(frozen["account_context"] == output["payload"]["account_context"], output["stage"],
               "account.facts", "Business facts differ from the current independently replayed snapshot")


def _missing(rule, action):
    return {"rule_path": rule, "action": action}


def _current_references(output, store, artifacts):
    """Verify every byte; reuse only authenticated source JSON closures."""
    material_id = output["chain_id"] + ":materials"
    material_operation = store.operation(material_id)
    if material_operation is not None and material_operation["status"] == "completed":
        stage_validation.accepted_source_reference_checker(material_id, store, artifacts)(output)
    else:
        verify.references(output, artifacts)


def _derive(output, previous, lineage, store, artifacts, authenticated):
    """Return independently established facts, never producer qualification flags."""
    stage = output["stage"]
    data = output["payload"][contracts.PAYLOAD_KEYS[stage]]
    flags = {name: False for name in contracts.QUALIFICATIONS}
    gaps, complete, support = [], False, None
    if data.get("unavailable") is True:
        return complete, flags, [_missing(stage + ".actual_object", "obtain_actual_stage_evidence")], support
    if stage == "account":
        complete = not output["payload"]["missing_fields"] and not any(
            reason in output["payload"]["account_context"].get("limitations", [])
            for reason in ("principal_policy_required", "cash_reconciliation_required"))
    elif stage == "news_collection":
        material = _engine(output, "materials", "material_prepare", store, artifacts, authenticated)
        collection = _engine(output, "collection", "news_collect", store, artifacts, authenticated)
        publisher = _engine(output, "publisher_discovery", "publisher_discover", store, artifacts, authenticated)
        if material and collection and publisher:
            support = material
            index = _value(artifacts, material["result"]["material_index_ref"])
            _check(index["collection_manifest_ref"] == collection["result"]["manifest_ref"], stage,
                   "news.material_collection", "Original material index belongs to another collection")
            originals = [row for part in index["pages"] for row in _value(artifacts, part["part_ref"])]
            by_version = {row["version_id"]: row for row in originals}
            _check({row["document_id"] for row in data["documents"]} == set(by_version), stage,
                   "news.original_inventory", "Typed news omitted or inserted original material versions")
            for document in data["documents"]:
                raw = by_version[document["document_id"]]
                _check(all(document[field] == raw[native] for field, native in (
                    ("source_ref", "body_ref"), ("source_id", "source_id"), ("url", "url"),
                    ("title", "title"), ("published_at", "published_at"), ("retrieved_at", "retrieved_at"))),
                    stage, "news.original_fields", "Original source, publication or body was substituted")
            flags["source_integrity"] = bool(originals)
            from chains.news_scope import validate_scope
            scope = validate_scope(store.root, store.plan, output["chain_id"], data["scope_plan_ref"],
                {"operation_id": collection["operation_id"], "result_ref": collection["result_ref"]},
                {"operation_id": material["operation_id"], "result_ref": material["result_ref"]},
                data.get("scope_audit_ref"), account_ref=output["account_ref"])
            _check(data["coverage"]["source_scope_complete"] == scope["research_scope_sufficient"], stage,
                   "news.actual_research_scope", "Producer scope completion differs from independently checked original research")
            complete = flags["source_integrity"] and scope["research_scope_sufficient"]
            gaps.extend(scope["required_actions"])
        if not complete:
            gaps.append(_missing("news.declared_broad_scope", "complete_or_independently_establish_current_broad_original_research_scope"))
    elif stage == "news_assessment":
        review = _engine(output, "news_review", "news_assess", store, artifacts, authenticated)
        if review:
            support = review
            native = review["result"]
            collection = _engine(previous, "collection", "news_collect", store, artifacts, authenticated, required=True)
            _check(native["collection_manifest_ref"] == collection["result"]["manifest_ref"], stage,
                   "assessment.current_collection", "The assessed facts do not belong to the received current originals")
            flags["source_integrity"] = native.get("status") == "assessed"
            flags["fact_qualification"] = flags["source_integrity"] and review["proof"]["status"] == "passed"
            declared = {row["industry_id"] for row in data["industry_links"]}
            actual = {row["thesis_id"] for row in native.get("industry_theses", [])}
            _check(declared == actual, stage, "assessment.industry_inventory", "Industry mechanisms differ from the actual original review")
            by_id = {row["thesis_id"]: row for row in native.get("industry_theses", [])}
            claims = {row["id"]: row for row in native.get("claims", [])}
            documents = {row.get("version_id", row["document_id"]): row["document_id"]
                         for row in previous["payload"]["news_material"]["documents"]}
            events = {row["event_id"]: row for row in native.get("events", [])}
            for row in data["industry_links"]:
                thesis = by_id[row["industry_id"]]
                _check(row.get("canonical_thesis") == thesis, stage,
                       "assessment.original_mechanism", "Industry advice must retain its actual independently accepted thesis")
                direct = [claims[key] for key in thesis["claim_ids"]]
                support_ids = set(thesis["claim_ids"])
                for identity in thesis.get("event_ids", []):
                    support_ids.update(events[identity].get("claim_ids", []))
                expected_documents = {documents[claims[key]["version_id"]] for key in support_ids
                                      if claims[key].get("version_id") in documents}
                _check(row["facts"] == direct and row["mechanism"] == "；".join(claim["text"] for claim in direct)
                       and set(row["document_ids"]) == expected_documents
                       and row["direction"] == thesis["direction"]
                       and row["time_horizon"]["days"] == thesis["horizon_days"]
                       and row["time_horizon"]["valid_until"] == thesis["valid_until"], stage,
                       "assessment.actual_fact_direction_horizon", "Facts, mechanism, direction or horizon were substituted")
                _check(row["counterevidence"] == [item for claim in direct for item in claim["counterevidence"]]
                       and _value(artifacts, row["economic_observations_ref"]) == native["economic_observations"],
                       stage, "assessment.counterevidence_and_economics", "Original counterevidence or economic observations changed")
            complete = flags["fact_qualification"] and (bool(actual) or data.get("no_material_industry_impact") is True)
        if not complete:
            gaps.append(_missing("assessment.actual_original_review", "complete_host_analysis_and_independent_original_fact_review"))
    elif stage == "fund_universe":
        discovery = _engine(output, "discovery", "fund_discover", store, artifacts, authenticated)
        if discovery:
            support = discovery
            native = discovery["result"]
            catalog = _value(artifacts, native["catalog_ref"])
            review = _engine(previous, "news_review", "news_assess", store, artifacts, authenticated, required=True)
            core_review = {key: value for key, value in review["result"].items() if key != "automatic_validation"}
            _check(native["news_review_hash"] == fingerprint(core_review), stage,
                   "universe.current_industry_review", "The fund universe was derived from another industry assessment")
            _check(data["catalog_ref"] == native["catalog_ref"] and set(data["catalog_codes"]) == set(catalog["entries"]),
                   stage, "universe.catalog", "Typed catalog is not the complete current public catalog")
            flags["source_integrity"] = True
            pending = _value(artifacts, native["pending_scopes_ref"])
            actual_groups = {row["group_id"]: row for row in native["groups"]}
            _check(set(data["candidate_codes"]) == set(native["completed_group_candidate_codes"]) & set(catalog["entries"]),
                   stage, "universe.actual_candidates", "The complete independently eligible peer set was changed")
            for exclusion in data["excluded_codes"]:
                admission = native["prequalification"].get(exclusion["code"])
                _check(admission is not None and admission["eligible"] is False, stage,
                       "universe.original_exclusion", "A peer was excluded without actual source-admission evidence")
            for group in data["peer_groups"]:
                raw = actual_groups.get(group["group_id"])
                if raw is None:
                    _check(group["membership_complete"] is False, stage, "universe.peer_scope", "Unknown membership cannot be complete")
                    continue
                members = set(raw["members"]) | set(pending[raw["pending_code_hash"]])
                _check(set(group["member_codes"]) == members and group["membership_complete"] == raw["coverage_status"].startswith("complete_"),
                       stage, "universe.peer_denominator", "A peer denominator or membership qualification changed")
            complete = all(row["membership_complete"] for row in data["peer_groups"]) and not native["coverage"].get("remaining_scope_codes")
        if not complete:
            gaps.append(_missing("universe.complete_original_peers", "complete_current_industry_peer_membership_and_catalog_evidence"))
    elif stage == "fund_sources":
        from chains import industry_sources
        source_binding = data.get("industry_source_binding_ref")
        industry = None
        if source_binding is not None:
            industry = industry_sources.validate_binding(store.root, store.plan, output["chain_id"],
                output["payload"]["engine_context"], source_binding, account_ref=output["account_ref"])
            _check(data.get("industry_source_status") == industry["status"], stage,
                   "sources.actual_industry_status", "Industry source status differs from its original evidence")
            gaps.extend(industry["required_actions"])
        elif data.get("source_task_ref") is not None:
            industry = industry_sources.validate_task(store.root, store.plan, output["chain_id"],
                output["payload"]["engine_context"], data["source_task_ref"], account_ref=output["account_ref"])
            _check(data.get("industry_source_status") == industry["industry_source_status"], stage,
                   "sources.actual_industry_task", "Pending work differs from this chain's real source task")
            gaps.append(_missing("sources.host_original_industry_contracts", "complete_host_original_industry_source_submission"))
        else:
            gaps.append(_missing("sources.current_industry_binding", "prepare_this_chain_original_industry_source_binding"))
        market = _engine(output, "market", "market_prepare", store, artifacts, authenticated)
        if market:
            support = market
            discovery = previous["payload"]["engine_context"]["discovery"]
            _check(market["request"]["payload"]["discovery_id"] == discovery["operation_id"], stage,
                   "sources.current_discovery", "Original NAV and terms belong to another frozen universe")
            record = market["result"]["record"]["market_ref"]
            terms = {row["code"] for row in record["terms"] if row.get("fee_contract_ref")}
            actual = set(record["prices"]) & set(record["price_dates"]) & terms
            _check(set(data["qualified_codes"]) <= actual, stage, "sources.actual_eligibility", "Source eligibility was not established by actual NAV and terms")
            original_exclusions = {row["code"] for row in previous["payload"]["frozen_universe"]["excluded_codes"]}
            _check({row["code"] for row in data["excluded_codes"]} <= original_exclusions, stage,
                   "sources.independent_exclusions", "Unknown source evidence cannot be relabeled as an independently established exclusion")
            flags["source_integrity"] = True
            complete = (not data["unknown_codes"] and set(data["holding_codes"]) <= actual
                        and industry is not None and industry["all_supported"])
        if not complete:
            gaps.append(_missing("sources.material_current_and_historical_inputs", "complete_all_current_risk_and_required_comparison_source_evidence"))
    elif stage == "model":
        daily = _engine(output, "daily_review", "daily_review", store, artifacts, authenticated)
        bundle = _value(artifacts, daily["result"]["bundle_ref"]) if daily else None
        facts = inspect_model_evidence(bundle, artifacts)
        for key in ("model_fit_executed", "compared_codes", "cash_compared"):
            _check(data[key] == facts[key], stage, "model." + key, "Declared model execution differs from actual calculation")
        if daily:
            support = daily
            discovery = _engine(previous, "discovery", "fund_discover", store, artifacts, authenticated)
            if discovery is not None:
                actual_policy = discovery["result"]["universe_policy"]
                payload = daily["request"]["payload"]
                _check(discovery["request"]["payload"]["universe_policy"] == actual_policy
                       and payload.get("discovery_id") == discovery["operation_id"]
                       and payload.get("universe_policy") == actual_policy, stage,
                       "model.current_discovery_policy", "Numerical request replaced the original discovery or selection policy")
                if "spec" in payload:
                    _check(payload["spec"]["universe_policy"] == actual_policy, stage,
                           "model.current_method_universe_policy", "Frozen method differs from its original discovery selection policy")
            source = previous["payload"]["source_bundle"]
            if source.get("industry_source_binding_ref") is not None:
                from chains import industry_sources
                industry = industry_sources.validate_binding(store.root, store.plan, output["chain_id"],
                    previous["payload"]["engine_context"], source["industry_source_binding_ref"],
                    account_ref=output["account_ref"])
                _check(daily["request"]["payload"].get("benchmark_contracts") == industry["benchmark_contracts"], stage,
                       "model.current_industry_contracts", "Numerical request replaced or omitted the sealed source contracts")
            evidence = data["evidence_refs"]
            _check(evidence["bundle_ref"] == daily["result"]["bundle_ref"], stage,
                   "model.actual_bundle_ref", "Model facts point at another decision bundle")
            for field in ("calculation_ref", "readiness_ref", "family_results_ref", "selection_ref"):
                _check(evidence[field] == bundle.get(field), stage, "model.evidence_refs." + field,
                       "Model evidence reference differs from the authenticated numerical evaluation")
            snapshot = _value(artifacts, output["account_ref"])
            _check(bundle.get("account_snapshot_ref") == output["account_ref"]
                   and daily["request"]["payload"].get("account_snapshot_ref") == output["account_ref"], stage,
                   "model.first_account_snapshot", "Actual numerical evaluation bypassed this chain's immutable first financial snapshot")
            _check(bundle["account_hash"] == fingerprint(snapshot["ledger_state"]), stage,
                   "model.account", "Actual numerical evaluation used another financial account")
            calculation = _value(artifacts, bundle.get("calculation_ref")) or {}
            fitting = calculation.get("fitting") or {}
            joint = fitting.get("joint_scenarios") or {}
            mpc = calculation.get("mpc") or {}
            winner = mpc.get("selected_policy") or {}
            calibration = mpc.get("calibration", {}).get("candidates", {}).get(winner.get("id"), {})
            qualified_scope = set(previous["payload"]["source_bundle"]["qualified_codes"])
            flags["source_integrity"] = facts["model_fit_executed"]
            flags["point_in_time"] = (bool(joint.get("source_selection_oos")) and bool(joint.get("source_calibration_oos"))
                and joint.get("panel_split", {}).get("kind") == "chronological_label_embargo_v1")
            flags["comparison_scope"] = (set(facts["compared_codes"]) == qualified_scope and facts["cash_compared"]
                and not previous["payload"]["source_bundle"]["unknown_codes"])
            flags["predictive_validation"] = (flags["point_in_time"] and calculation.get("status") == "research_ready"
                and calibration.get("status") == "calibrated" and winner.get("eligible") is True
                and winner.get("trade_guard", {}).get("risk_evidence_basis") == "independent_policy_calibration")
            # The public daily validator independently reproduces source data,
            # numerical stages, complete families and finite-set certificates.
            complete = (daily["proof"]["status"] == "passed" and facts["model_fit_executed"]
                        and all(flags[key] for key in ("point_in_time", "comparison_scope", "predictive_validation")))
        gaps.extend(facts["comparison_gaps"])
        if not complete:
            gaps.append(_missing("model.independent_scientific_qualification", "complete_actual_PIT_peer_cash_comparison_and_frozen_winner_calibration"))
    elif stage == "recommendation":
        daily = _engine(output, "daily_review", "daily_review", store, artifacts, authenticated)
        if daily:
            support = daily
            bundle = _value(artifacts, daily["result"]["bundle_ref"])
            actual = bundle["action_analysis"]
            # Exact action amounts are compared to canonical engine advice by
            # code. This does not turn a blocked recommendation into a trade.
            rows = {}
            for raw in actual.get("actions", []):
                rows.setdefault(raw["code"], []).append(raw)
            complete = previous["stage_complete"] and previous["qualifications"]["predictive_validation"]
            for advice in data["actions"]:
                raw = rows.get(advice["code"], [])
                if not complete:
                    _check(advice["action"] == "hold" and all(advice[field] is None
                        for field in ("amount_CNY", "shares", "fees_CNY")), stage,
                        "recommendation.unqualified_amount", "Unqualified models cannot authorize current money")
                    continue
                if not raw:
                    if complete:
                        _check(advice["action"] == "hold" and all(advice[field] == "0"
                            for field in ("amount_CNY", "shares", "fees_CNY")), stage,
                            "recommendation.actual_hold", "Verified no-action must retain its exact zero adjustment")
                    continue
                sides = {row["side"] for row in raw}
                _check(len(sides) == 1, stage, "recommendation.actual_sides", "Opposite current action legs cannot be collapsed silently")
                side = next(iter(sides))
                expected = {"amount_CNY": sum((Decimal(row["reference_valuation"]["cash_debit" if side == "buy" else "gross"]) for row in raw), Decimal(0)),
                            "shares": sum((Decimal(row["reference_valuation"]["shares"]) for row in raw), Decimal(0)),
                            "fees_CNY": sum((Decimal(row["estimated_fee"]) for row in raw), Decimal(0))}
                _check(advice["action"] == ("increase" if side == "buy" else "reduce"), stage,
                       "recommendation.actual_action", "Advice differs from the actual evaluated action")
                for field, value in expected.items():
                    _check(advice[field] is not None and Decimal(advice[field]) == value, stage,
                           "recommendation." + field, "Advice amount differs from verified canonical action evidence")
            flags["recommendation_qualification"] = bool(complete and bundle["orders"]["status"] in {"ready", "no_action"})
            complete = flags["recommendation_qualification"]
        if not complete:
            gaps.append(_missing("recommendation.current_qualified_advice", "obtain_independently_qualified_current_account_action_analysis"))
    elif stage == "report":
        complete = False
        daily = _engine(output, "daily_review", "daily_review", store, artifacts, authenticated)
        if daily:
            support = daily
            bundle = _value(artifacts, daily["result"]["bundle_ref"])
            verify.verify_report(bundle, daily["result"]["report_markdown"])
            _check(data["report_markdown"] == daily["result"]["report_markdown"], stage,
                   "report.actual_markdown", "The report is not the actual independently verified current report")
            complete = bool(data.get("conclusion"))
        # Aggregate only independently checked earlier stage evidence below;
        # the final readable observation itself grants no scientific flags.
    if previous is not None and not previous["stage_complete"] and stage != "report":
        complete = False
        gaps.append(_missing("upstream.business_postcondition", "complete_the_original_upstream_business_scope"))
    return bool(complete), flags, gaps, support


def _final_goal(lineage, store, artifacts, current_flags, report_complete):
    accumulated = dict(current_flags)
    all_business = True
    for index, prior in enumerate(lineage):
        native = _operations(prior, store, artifacts)
        passed, flags, _, _ = _derive(prior, lineage[index - 1] if index else None,
            lineage[:index], store, artifacts, native)
        all_business &= passed
        for name, value in flags.items():
            accumulated[name] |= value
    accumulated["trade_ready"] = False
    goal = (report_complete and len(lineage) == 7 and all_business
            and all(accumulated[key] for key in contracts.QUALIFICATIONS if key != "trade_ready"))
    return accumulated, bool(goal)


def _authenticate_prefix(lineage, store, artifacts):
    """A predecessor's declared completion/qualification is also untrusted."""
    for index, prior in enumerate(lineage):
        authenticated = _operations(prior, store, artifacts)
        complete, flags, _, _ = _derive(prior, lineage[index - 1] if index else None,
                                      lineage[:index], store, artifacts, authenticated)
        _check(prior["stage_complete"] == complete and prior["qualifications"] == flags,
               prior["stage"], "lineage.actual_business_acceptance",
               "Upstream business completion or qualification is not supported by its actual evidence",
               "input_validation")


def validate_stage(root, plan, previous, output, control_context):
    """Authenticate actual evidence and return a checked DTO plus separate proof."""
    stage = output.get("stage", "unknown")
    try:
        check_control(root, plan, output["chain_id"], control_context, previous=previous)
        allow_partial = control_context["delivery_mode"] == "observation"
        contracts.validate_envelope(output, previous=previous, allow_partial=allow_partial,
            expected_protocol_sha256=control_context["protocol_sha256"],
            expected_implementation_identity=control_context["implementation_identity"])
        store = Store(root, plan)
        artifacts = Artifacts(store.base)
        _current_references(output, store, artifacts)
        lineage = _lineage(output, previous, artifacts, control_context)
        _current_account(output, lineage, store, artifacts)
        _authenticate_prefix(lineage, store, artifacts)
        authenticated = _operations(output, store, artifacts)
        # Producer qualification declarations are inputs to verify, never
        # evidence. They must not smuggle a fabricated independent receipt.
        _check(not output["qualification_receipts"] and not any(output["qualifications"].values()), stage,
               "qualification.producer_claim", "Only the independent gate may issue qualification receipts",
               "independent_qualification")
        complete, flags, gaps, support = _derive(output, previous, lineage, store, artifacts, authenticated)
        checked = copy.deepcopy(output)
        checked["qualifications"] = flags
        checked["qualification_receipts"] = []
        checked["processing_complete"] = True
        checked["stage_complete"] = complete
        checked["investment_goal_achieved"] = False
        if output["status"] == "failed":
            checked["status"] = "failed"
            checked["stage_complete"] = complete = False
        elif stage == "account" and checked["payload"]["missing_fields"]:
            checked["status"] = "awaiting_user"
        elif (stage == "news_collection" and not complete and support is not None and flags["source_integrity"]
                and any(row.get("rule_path") == "scope.actual_original_audit" for row in gaps)):
            checked["status"] = "awaiting_analysis"
        elif stage == "news_assessment" and not complete and support is None:
            checked["status"] = "awaiting_analysis"
        elif stage == "fund_sources" and not complete and any(
                row.get("rule_path") == "sources.host_original_industry_contracts" for row in gaps):
            checked["status"] = "awaiting_analysis"
        else:
            checked["status"] = "complete" if complete else "needs_evidence"
        if stage == "model":
            checked["payload"]["model_result"]["execution_status"] = "qualified" if complete else (
                "unqualified" if checked["payload"]["model_result"]["model_fit_executed"]
                else checked["payload"]["model_result"]["execution_status"])
            checked["output_hash"] = fingerprint(checked["payload"])
        if stage == "report":
            # Re-derive every earlier stage from its real current engine
            # receipts; a prior literal flag or gate-id string is insufficient.
            flags, goal = _final_goal(lineage, store, artifacts, flags, complete)
            checked["qualifications"] = flags
            checked["investment_goal_achieved"] = goal
            checked["payload"]["report"]["goal_achieved"] = goal
            checked["output_hash"] = fingerprint(checked["payload"])
        binding = {key: checked[key] for key in BINDING_FIELDS}
        definition, _ = protocol.load_definition()
        scopes = definition["common"]["qualification_authorities"]
        validation = {"schema_id": "investment_business_chain_independent_validation_v1",
            "validator_id": VALIDATOR_ID, "validator_scope": VALIDATOR_SCOPE,
            "status": "passed" if complete else "partial", "binding": binding,
            "processing_complete": True, "stage_complete": complete,
            "qualifications": flags, "investment_goal_achieved": checked["investment_goal_achieved"],
            "qualification_results": {name: {"status": "passed" if value else "not_established",
                "scope": scopes[name]} for name, value in flags.items()},
            "checks": ["frozen_protocol_and_implementation", "exact_input_and_lineage_CAS", "current_account_replay",
                       "canonical_public_operations_and_accepted_attempts", "independent_business_postconditions"],
            "required_actions": gaps, "operations": [{"operation_id": row["operation_id"],
                "result_ref": row["result_ref"], "scope": row["proof"]["scope"]}
                for _, row in sorted(authenticated.items())]}
        reference = artifacts.put_json(validation)
        claims = [name for name, value in flags.items() if value]
        if claims:
            _check(support is not None, stage, "qualification.supporting_operation", "Qualification requires a real accepted public supporting operation")
            if not any(row["operation_id"] == support["operation_id"] for row in checked["operation_receipts"]):
                checked["operation_receipts"].append({"operation_id": support["operation_id"],
                    "operation": support["operation"], "status": "completed", "source_ref": support["result_ref"]})
            checked["qualification_receipts"] = [{**binding, "operation_id": support["operation_id"],
                "status": "passed", "independent": True, "validator_id": VALIDATOR_ID,
                "validator_scope": "; ".join(scopes[name] for name in claims),
                "source_ref": reference, "qualifications": claims}]
        contracts.validate_envelope(checked, previous=previous, allow_partial=allow_partial)
        protocol.validate_control(control_context, root=root, plan=plan, chain_id=output["chain_id"])
        return {"output": checked, "validation_ref": reference, "validation": validation}
    except (GateFailure, StaleSnapshot):
        raise
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise GateFailure(stage, "output_validation", "actual_evidence.authentication", str(error)) from error


def verify_stage_receipt(root, plan, previous, output, validation_ref, control_context):
    """Resume authentication rederives evidence; a named gate/proof is not trust.

    Sealed outputs already contain gate-derived qualifications, so they use a
    separate path from untrusted producer output. No prior verdict bypasses
    current CAS, account, input, accepted-attempt or public-operation checks.
    """
    stage = output.get("stage", "unknown")
    try:
        check_control(root, plan, output["chain_id"], control_context, previous=previous)
        allow_partial = control_context["delivery_mode"] == "observation"
        contracts.validate_envelope(output, previous=previous, allow_partial=allow_partial,
            expected_protocol_sha256=control_context["protocol_sha256"],
            expected_implementation_identity=control_context["implementation_identity"])
        store = Store(root, plan)
        artifacts = Artifacts(store.base)
        _current_references(output, store, artifacts)
        proof = _value(artifacts, validation_ref)
        _check(proof.get("schema_id") == "investment_business_chain_independent_validation_v1"
               and proof.get("validator_id") == VALIDATOR_ID and proof.get("validator_scope") == VALIDATOR_SCOPE, stage,
               "gate_receipt.authority", "The sealed output has no recognized independent gate receipt")
        _check(proof["binding"] == {key: output[key] for key in BINDING_FIELDS}, stage,
               "gate_receipt.binding", "Gate receipt binds a different input, output, account or implementation")
        lineage = _lineage(output, previous, artifacts, control_context)
        _current_account(output, lineage, store, artifacts)
        _authenticate_prefix(lineage, store, artifacts)
        native = _operations(output, store, artifacts)
        complete, flags, gaps, _ = _derive(output, previous, lineage, store, artifacts, native)
        if output["status"] == "failed":
            complete = False
        goal = False
        if stage == "report":
            flags, goal = _final_goal(lineage, store, artifacts, flags, complete)
        _check(output["stage_complete"] == complete and proof["stage_complete"] == complete
               and proof["processing_complete"] is True
               and proof["status"] == ("passed" if complete else "partial")
               and output["qualifications"] == flags and proof["qualifications"] == flags
               and output["investment_goal_achieved"] == goal and proof["investment_goal_achieved"] == goal,
               stage, "gate_receipt.actual_postconditions", "Recorded qualifications differ from current independently rederived evidence",
               "independent_qualification")
        actual_operations = [{"operation_id": row["operation_id"], "result_ref": row["result_ref"],
                              "scope": row["proof"]["scope"]} for _, row in sorted(native.items())]
        _check(proof["operations"] == actual_operations, stage, "gate_receipt.actual_operations",
               "The proof cites operations other than the independently authenticated actual stage evidence")
        definition, _ = protocol.load_definition()
        scopes = definition["common"]["qualification_authorities"]
        expected_qualifications = {name: {"status": "passed" if value else "not_established", "scope": scopes[name]}
                                  for name, value in flags.items()}
        _check(proof["qualification_results"] == expected_qualifications, stage,
               "gate_receipt.independent_qualification_scopes", "A specific qualification verdict or scientific scope was substituted")
        _check(all(receipt["source_ref"] == validation_ref and receipt["validator_id"] == VALIDATOR_ID
                   and receipt["validator_scope"] == "; ".join(scopes[name] for name in receipt["qualifications"])
                   for receipt in output["qualification_receipts"]), stage,
               "gate_receipt.qualification_proofs", "Qualifications point at a different or producer-authored gate proof")
        protocol.validate_control(control_context, root=root, plan=plan, chain_id=output["chain_id"])
        return {"output": copy.deepcopy(output), "validation_ref": validation_ref, "validation": proof}
    except (GateFailure, StaleSnapshot):
        raise
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise GateFailure(stage, "output_validation", "gate_receipt.actual_evidence", str(error)) from error
