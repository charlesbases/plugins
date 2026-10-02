"""Private physical-source audit for optional currency studies, never JSON trust."""
from contracts import EvidenceError, fingerprint, require


def audit_pair(runtime, context, data, source_rows, record, frame):
    """Re-read the public archive and match the study to its authenticated data."""
    import verify
    artifacts, store, binding = runtime["artifacts"], runtime["store"], runtime["binding_bundle"]
    require(binding["data_ref"] and binding["decision_at"] == context["decision_at"], "Study source runtime differs from its decision")
    proof = verify.verify_research_binding(binding, artifacts, store=store)
    rebuilt = artifacts.read_json(binding["data_ref"])
    require(rebuilt == data, "Study runtime dataset differs from re-extracted public sources")
    codes = record["codes"]
    require(frame["source_nav"] == {code: rebuilt["nav"][code] for code in codes},
            "Study NAV registry differs from physically audited original NAV versions")
    market = artifacts.read_json(binding["market_record_ref"])["market_ref"]
    verify.execution_sources(market, artifacts, as_of=context["decision_at"])
    terms = {row["code"]: row["fee_contract"] for row in market["terms"]}
    current = {code: context["fee_contracts"][code] for code in codes}
    require(current == {code: terms[code] for code in codes}, "Study current dealing terms differ from original field-bound sources")
    gaps = []
    if frame["fee_scope"] == "current_terms_repriced":
        require(frame["fee_contracts"] == current, "Study repricing contracts differ from physically re-extracted current terms")
    else:
        gaps.append("historical_fee_and_clock_original_artifacts_not_bound_to_public_study_runtime")
    for sample in source_rows:
        require(sample["code"] in codes and sample["label_source"] == record["source_labels"][sample["code"]],
                "Study outer sample differs from the frozen source panel")
        witness = sample["industry_source"]
        matching = [row for row in frame["source_frontier"] if row.get("value_hash") == witness["source_hash"]]
        require(matching, "Study source feature frontier is missing")
        for ref in matching:
            physical = ref.get("artifact_ref")
            if physical is None:
                gaps.append("source_feature_frontier_has_no_physical_original_artifact")
                continue
            # A content address by itself does not certify its historical time.
            stored = artifacts.read_json(physical)
            require(stored == witness, "Study source frontier artifact differs from the pipeline feature witness")
            gaps.append("historical_feature_frontier_producer_and_origin_time_not_independently_bound")
    if frame["policy_scope"] == "full_source_mpc":
        gaps.append("multi_date_forecast_training_frontiers_need_physical_source_reconstruction")
    return {"status": "partial", "scope": "physical_current_NAV_asset_sources_and_current_terms_not_full_historical_study_certificate",
            "physical_NAV_verified": True, "physical_current_terms_verified": True,
            "research_binding": proof, "dataset_hash": fingerprint(rebuilt),
            "required_source_gaps": sorted(set(gaps or ["historical_study_source_frontier_clock_not_fully_authenticated"])),
            "strict_PIT_verified": False, "full_production_MPC_advantage_validated": False}


def evaluator(runtime, data):
    """Return a private closure; neither objects nor callables enter stage JSON."""
    from news_wealth import evaluate_pair
    def evaluate(**inputs):
        result = evaluate_pair(**inputs)
        if runtime is None or result.get("status") not in ("source_replay_ready", "conditional_currency_replay_ready"):
            return result
        context, record = inputs["context"], inputs["record"]
        frame = context["paired_currency_validation"]["frames"][record["date"]]
        try:
            audit = audit_pair(runtime, context, data, inputs["source_rows"], record, frame)
        except (EvidenceError, ValueError, KeyError, TypeError, OSError) as error:
            return {"status": "insufficient_evidence", "reason": "physical_source_audit_rejected: "+str(error),
                    "strict_PIT_verified": False, "physical_NAV_verified": False,
                    "actual_orders_created": False, "profitability_claim": False,
                    "full_production_MPC_advantage_validated": False,
                    "required_actions": [{"action": "repair_original_source_study_binding", "reason": str(error)}]}
        result["status"] = "conditional_currency_replay_ready"
        result["source_evidence"].update(strict_PIT_verified=False, physical_source_audit=audit)
        result["source_evidence"]["frame_source_PIT_verified"] = False
        result["full_production_MPC_advantage_validated"] = False
        result.pop("result_hash", None)
        result["result_hash"] = fingerprint(result)
        return result
    return evaluate


def recheck_pairs(context, data, value, runtime):
    """Fresh public verification cannot promote a conditional study certificate."""
    import verify
    artifacts, store, binding = runtime["artifacts"], runtime["store"], runtime["binding_bundle"]
    verify.verify_research_binding(binding, artifacts, store=store)
    require(artifacts.read_json(binding["data_ref"]) == data, "Published paired study dataset differs from the physical archive")
    market = artifacts.read_json(binding["market_record_ref"])["market_ref"]
    verify.execution_sources(market, artifacts, as_of=context["decision_at"])
    terms = {row["code"]: row["fee_contract"] for row in market["terms"]}
    codes = context["allocation_codes"]
    for pair in value["currency_increment"]["paired_rows"]:
        frame = context["paired_currency_validation"]["frames"][pair["date"]]
        require(frame["source_nav"] == {code: data["nav"][code] for code in codes}, "Published source NAV frame differs from independently re-read data")
        if frame["fee_scope"] == "current_terms_repriced":
            require(frame["fee_contracts"] == {code: terms[code] for code in codes}, "Published study repricing terms differ from physical product sources")
        require(pair["source_artifacts_independently_verified"] is False
                and pair["source_evidence"].get("strict_PIT_verified") is False,
                "A partial source study audit cannot publish a strict historical certificate")
    return {"status": "partial", "scope": "physical_current_source_data_rechecked_historical_study_gaps_retained"}
