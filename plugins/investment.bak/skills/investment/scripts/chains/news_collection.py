"""Account output -> sealed current domestic/international original inventory."""
import copy

from contracts import require
from chains.common import state, emit, binding, context, check_control


def collect(root, plan, previous, *, control_context, scope_submission=None):
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    from workflow_prepare import prepare
    from chains import news_scope
    def freeze_scope(scope_root, scope_name, identity, retrieval, publishers, run):
        return news_scope.freeze_plan(scope_root, scope_name, identity, retrieval, publishers, run,
                                      account_ref=previous["account_ref"])
    prepared = prepare(root, plan, request_id=previous["chain_id"], scope_plan_callback=freeze_scope)
    store, artifacts = state(root, plan)
    receipts, engine = [], context(previous)
    engine["workflow_id"] = previous["chain_id"]
    for key, suffix, operation in (
        ("publisher_discovery", "publishers", "publisher_discover"),
        ("news_retrieval", "retrieval", "retrieval_prepare"),
        ("analysis_run", "analysis", "analysis_start"),
        ("collection", "collect", "news_collect"),
        ("materials", "materials", "material_prepare"),
    ):
        identity = previous["chain_id"] + ":" + suffix
        saved = store.operation(identity)
        if saved is None:
            continue
        require(saved["request"]["operation"] == operation,
                "News subchain operation identity was substituted")
        ref = artifacts.put_json(saved["result"])
        receipt = {"operation_id": identity, "operation": operation,
                   "status": "completed" if saved["status"] == "completed" else "failed",
                   "source_ref": ref}
        receipts.append(receipt)
        engine[key] = binding(receipt, ref)
    documents = []
    index_ref = prepared.get("assessment_material_index_ref")
    if index_ref is not None:
        index = artifacts.read_json(index_ref)
        for page in index["pages"]:
            for row in artifacts.read_json(page["part_ref"]):
                documents.append({"document_id": row["version_id"], "version_id": row["version_id"],
                    "canonical_document_id": row["document_id"],
                    "source_ref": row["body_ref"], "source_id": row["source_id"], "url": row["url"],
                    "title": row["title"], "published_at": copy.deepcopy(row["published_at"]),
                    "retrieved_at": row["retrieved_at"]})
        engine["material_index_ref"] = index_ref
    run = prepared["analysis_run"]
    collection = prepared.get("collection", {})
    controller = prepared["news_controller"]
    groups = run["news_policy"]["required_source_groups"]
    coverage = {"domestic_source_ids": groups.get("registered_domestic_publishers", []),
        "international_source_ids": groups.get("registered_international_publishers", []),
        "pending_source_ids": controller.get("pending_source_ids", []),
        "global_coverage_known": False,
        "source_scope_complete": False,
        "controller": copy.deepcopy(controller),
        "scope": "actual_originals_only_whole_opportunity_complement_preserved_not_majority_coverage_proof"}
    data = {"window_start": collection.get("window_start", run["publish_cutoff"]),
            "cutoff_at": collection.get("information_cutoff_at", run["publish_cutoff"]),
            "documents": documents, "coverage": coverage,
            "original_material_index_ref": index_ref,
            "declared_research_scope_refs": [engine[key]["result_ref"] for key in (
                "publisher_discovery", "news_retrieval", "collection", "materials") if key in engine],
            "required_actions": copy.deepcopy(prepared.get("required_actions", []))}
    data["scope_plan_ref"] = prepared["scope_plan_ref"]
    scope_plan = artifacts.read_json(data["scope_plan_ref"])
    template = news_scope.request_templates(data["scope_plan_ref"], scope_plan)
    data["scope_request_templates"] = {"scope_submission_template_ref": artifacts.put_json(template),
        "scope_plan_ref": data["scope_plan_ref"], "question_count": len(scope_plan["questions"]),
        "submission_role": "host_internal_scope_audit_before_formal_industry_assessment"}
    audit_ref = None
    if scope_submission is not None:
        require("collection" in engine and "materials" in engine, "Current original collection must finish before its scope audit")
        require(documents, "Scope audit requires actual current original bodies")
        audit_ref, receipt = news_scope.submit_review(root, plan, previous["chain_id"],
            data["scope_plan_ref"], engine["collection"], scope_submission)
        if receipt is not None:
            receipts.append(receipt)
    data["scope_audit_ref"] = audit_ref
    status = "needs_evidence"
    if "collection" in engine and "materials" in engine:
        scope = news_scope.validate_scope(root, plan, previous["chain_id"], data["scope_plan_ref"],
            engine["collection"], engine["materials"], audit_ref, account_ref=previous["account_ref"])
        data["scope_validation_ref"] = artifacts.put_json(scope)
        coverage["source_scope_complete"] = scope["research_scope_sufficient"]
        coverage["original_seed_scope_complete"] = scope["original_seed_scope_complete"]
        coverage["scope"] = "conditional_declared_dynamic_research_domain_not_global_or_source_window_exhaustiveness"
        data["required_actions"] = scope["required_actions"]
        status = "awaiting_analysis" if documents and not scope["actual_host_audit_completed"] else "needs_evidence"
    return emit(previous, "news_collection", data, control_context=control_context, engine_context=engine,
                status=status,
                receipts=receipts, source_refs=[index_ref] if index_ref is not None else [])
