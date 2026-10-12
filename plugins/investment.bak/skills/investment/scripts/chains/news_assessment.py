"""Original inventory -> host's source-grounded industry mechanisms."""
import copy

from contracts import require
from chains.common import call, binding, bound_result, context, emit, state, check_control


def assess(root, plan, previous, *, control_context, analysis=None):
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    engine = context(previous)
    material = previous["payload"]["news_material"]
    data = {"material_hash": previous["output_hash"], "industry_links": []}
    if analysis is None:
        data["required_actions"] = [{"action": "host_read_all_original_pages_and_submit_source_grounded_analysis",
            "material_index_ref": material["original_material_index_ref"],
            "ask_user_for_model_parameters": False}]
        return emit(previous, "news_assessment", data, control_context=control_context,
                    status="awaiting_analysis", processing_complete=False)
    require(type(analysis) is dict, "Host original analysis must be an object")
    run_result = bound_result(root, plan, engine["analysis_run"], "analysis_start")
    collected = bound_result(root, plan, engine["collection"], "news_collect")
    store, artifacts = state(root, plan)
    manifest = artifacts.read_json(collected["manifest_ref"])
    payload = {"run_id": run_result["run"]["run_id"], "collection_id": manifest["collection_id"],
               **copy.deepcopy(analysis)}
    import news
    check = news.assessment_structure_preflight(payload, manifest)
    if check["status"] != "passed":
        data["diagnostics"] = check
        return emit(previous, "news_assessment", data, control_context=control_context,
                    status="awaiting_analysis", processing_complete=False)
    result, receipt, ref = call(root, plan, previous["chain_id"], "assess", "news_assess", payload)
    engine["news_review"] = binding(receipt, ref)
    version_documents = {row["version_id"]: row["document_id"] for row in material["documents"]}
    claims = {row["id"]: row for row in result.get("claims", [])}
    events = {row["event_id"]: row for row in result.get("events", [])}
    for thesis in result.get("industry_theses", []):
        ids = set()
        for event_id in thesis.get("event_ids", []):
            for claim_id in events[event_id].get("claim_ids", []):
                claim = claims[claim_id]
                if claim.get("version_id") in version_documents:
                    ids.add(version_documents[claim["version_id"]])
        # The canonical thesis also binds claims directly for non-event facts.
        for claim_id in thesis.get("claim_ids", []):
            version_id = claims[claim_id].get("version_id")
            if version_id in version_documents:
                ids.add(version_documents[version_id])
        require(ids, "Industry mechanism lacks this chain's original document support")
        data["industry_links"].append({"industry_id": thesis["thesis_id"],
            "mechanism": "；".join(claims[key]["text"] for key in thesis["claim_ids"]),
            "document_ids": sorted(ids),
            "facts": [copy.deepcopy(claims[key]) for key in thesis["claim_ids"]],
            "counterevidence": [copy.deepcopy(row) for key in thesis["claim_ids"]
                                for row in claims[key]["counterevidence"]],
            "uncertainties": [row["missing_reason"] for key in thesis["claim_ids"]
                              for row in claims[key]["counterevidence"] if "missing_reason" in row],
            "direction": thesis["direction"], "time_horizon": {"days": thesis["horizon_days"],
                "valid_until": thesis["valid_until"], "scope": "analyst_thesis_not_model_return_forecast"},
            "economic_observations_ref": artifacts.put_json(result.get("economic_observations", [])),
            "canonical_thesis": copy.deepcopy(thesis)})
    data["engine_review_ref"] = ref
    if not data["industry_links"] and result.get("status") == "assessed":
        data.update(no_material_industry_impact=True,
                    reason="本次已核原文评审未建立可支持的行业变动机制，保留原评审及未知范围。")
    return emit(previous, "news_assessment", data, control_context=control_context, engine_context=engine,
                status="needs_evidence" if result.get("status") == "assessed" else "awaiting_analysis",
                processing_complete=result.get("status") == "assessed",
                receipts=[receipt], source_refs=[ref])
