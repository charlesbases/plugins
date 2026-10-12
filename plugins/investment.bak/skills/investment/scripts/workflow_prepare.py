"""Prepare a fresh, source-backed review for the invoking skill's analyst.

The host supplies economic interpretation after reading these original bodies.
This module deliberately does not turn keywords into forecasts or trade orders.
"""
import datetime as dt
import re
import uuid

from artifacts import Artifacts
from contracts import instant, require, utc_now
from state_store import Store


def prepare(root, plan, *, request_id=None, days=7, max_urls_per_source=8,
            timeout_seconds=8, budget_seconds=60, scope_plan_callback=None):
    import pipeline
    import publisher_discovery
    import workflow_news

    identity = request_id or "review-" + uuid.uuid4().hex
    require(type(identity) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", identity),
            "Invalid review request_id")
    require(type(days) is int and 1 <= days <= 31, "Recent news window must be 1-31 days")
    require(type(max_urls_per_source) is int and 2 <= max_urls_per_source <= 20,
            "News URL batch budget must be 2-20 for an original parent and attachment")
    require(type(timeout_seconds) in (int, float) and 0 < timeout_seconds <= 30,
            "News timeout must be 0-30 seconds")
    require(type(budget_seconds) in (int, float) and 0 < budget_seconds <= 60,
            "News batch budget must be 0-60 seconds")

    def call(suffix, operation, payload):
        return pipeline.run({"schema_version": 4, "request_id": identity + ":" + suffix,
                             "operation": operation, "payload": payload}, root, plan)

    account = call("status", "status", {})
    store = Store(root, plan)
    # Same-id recovery keeps the original opportunity window; a new user
    # invocation gets a fresh identity and a new authoritative clock.
    discovery_window_end = store.operation(identity + ":status")["created_at"]
    publishers = call("publishers", "publisher_discover", {"timeout_seconds": timeout_seconds,
        "gdelt_window": {"window_start": (instant(discovery_window_end)-dt.timedelta(days=days)).isoformat(),
                         "window_end": discovery_window_end}})
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    original_publishers = store.get("publisher-discovery", identity + ":publishers")
    require(original_publishers is not None, "Current public publisher discovery has not completed")
    from stage_validation import validated_publisher_registry
    registry = validated_publisher_registry(identity + ":publishers", store, artifacts)
    retrieval = call("retrieval", "retrieval_prepare", {"workflow_id": identity,
        "publisher_discovery_id": identity + ":publishers"})
    sources = sorted(name for name, rule in registry["sources"].items()
                     if rule["purpose"] == "news" and rule.get("default_urls"))
    require(sources, "No registered free original news sources")
    # Original directory regions describe the discovered domain. Unknown
    # regions stay explicit; a protocol id is not a publisher's nationality.
    entities = {row["entity_id"]: row for row in original_publishers["directory_entities"]}
    regions = {name: {entities[key]["region"] for key in rule.get("publisher_directory_entity_ids", [])}
               for name, rule in registry["sources"].items() if name in sources}
    domestic = [name for name in sources if name.startswith("cn_") or "domestic" in regions[name]]
    international = [name for name in sources if "international" in regions[name] or name.startswith(("us_", "eu_"))]
    unclassified = [name for name in sources if name not in domestic and name not in international]
    groups = {"registered_domestic_publishers": domestic,
              "registered_international_publishers": international,
              "unclassified_publisher_region": unclassified}
    groups = {name: members for name, members in groups.items() if members}
    policy = {"mode": "sealed_inputs", "sources": sources,
              "max_age_seconds": 86400, "required_source_groups": groups}
    opened = call("analysis", "analysis_start", {"news_policy": policy,
        "publisher_discovery_id": identity + ":publishers"})
    run = opened["run"]
    scope_plan_ref = (scope_plan_callback(root, plan, identity, retrieval, publishers, run)
                      if scope_plan_callback is not None else None)
    collected = workflow_news.collect(root, plan, run, identity,
        (instant(run["publish_cutoff"]) - dt.timedelta(days=days)).isoformat(), parameters={
            "max_urls_per_source": max_urls_per_source, "timeout_seconds": timeout_seconds,
            "budget_seconds": budget_seconds}, retrieval=retrieval)
    if "collection" not in collected:
        return {"status": collected["status"], "workflow_id": identity, "currency": "CNY", "platform": "TT",
                "account_status": account, "analysis_run": run, "publisher_discovery": publishers,
                "news_retrieval": retrieval, "scope_plan_ref": scope_plan_ref,
                "news_controller": collected["controller"], "trade_ready": False,
                "required_actions": collected["required_actions"],
                "scope": "current_original_publisher_domain_incomplete_not_a_completed_news_or_investment_review"}
    collection = collected["collection"]
    from workflow_materials import read, OUTPUT_BYTES
    indexed = call("materials", "material_prepare", {"workflow_id": identity,
        "run_id": run["run_id"], "collection_id": collection["collection_id"]})
    require(indexed.get("status") == "original_materials_prepared",
            "Current original materials have not passed the public preparation controller")
    # This first bounded page is for reading convenience. Its total and cursor
    # describe every canonical original; neither bodies nor a full material
    # list are copied into RAM before reducing stdout.
    first_page = read(root, plan, workflow_id=identity, output_bytes=OUTPUT_BYTES//2)
    index = artifacts.read_json(indexed["material_index_ref"])
    excluded = (artifacts.read_json(index["excluded_ref"])
                if index["excluded_ref"]["size"] <= OUTPUT_BYTES//4 else [])
    from workflow_requests import request_templates
    templates = request_templates(root, plan, workflow_id=identity)
    return {"status": "research_prepared", "workflow_id": identity, "currency": "CNY",
            "platform": "TT", "account_status": account, "analysis_run": run,
            "collection": collection, "assessment_material": first_page["assessment_material"],
            "material_page": first_page, "total_materials": indexed["total_materials"],
            "total_original_versions": indexed["total_original_versions"],
            "assessment_material_index_ref": indexed["material_index_ref"],
            "publisher_discovery": publishers, "news_controller": collected["controller"],
            "news_retrieval": retrieval, "scope_plan_ref": scope_plan_ref,
            "excluded_body_versions": excluded, "excluded_body_versions_ref": index["excluded_ref"],
            "excluded_body_versions_count": indexed["excluded_count"], "trade_ready": False,
            "request_templates": templates,
            "scope": "current_dynamic_publisher_original_news_and_recorded_account_for_host_assessment_global_coverage_unknown",
            "next_operation": "news_assess",
            "required_actions": ["Host must read original bodies, verify factual claims and economic mechanisms, "
                "then assess, discover funds, obtain applicable contracts and run daily_review in this invocation. "
                "Missing predictive history must remain an explicit research limitation."]}
