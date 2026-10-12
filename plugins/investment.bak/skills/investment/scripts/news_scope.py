"""Canonical full-policy aggregation of independently committed news shards."""
import copy

from contracts import fields, fingerprint, instant, require, utc_now


def accepted_retrieval(identity, run, registry, store, artifacts):
    """Authenticate the public plan, then rebuild its exact registry complement.

    Routing was independently checked by retrieval_prepare. Reusing its frozen
    selection grants no fact qualification; every captured original still goes
    through the existing news byte/body validators below.
    """
    import stage_validation
    operation = store.operation(identity)
    require(operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "retrieval_prepare"
            and operation["result"].get("automatic_validation") is not None,
            "Selected news needs a completed public retrieval_prepare acceptance")
    bindings = artifacts.read_json(operation["result"]["automatic_validation"]["operation_bindings_ref"])
    value, _ = stage_validation._operation_core(operation["request"], operation["result"], store, artifacts, bindings)
    require(value["retrieval_id"] == identity and value["workflow_id"]+":analysis" == run["run_id"]
            and value["publisher_discovery_id"] == run.get("publisher_discovery_id")
            and value["request"] == operation["request"]["payload"]
            and value["source_registry_hash"] == fingerprint(registry)
            and artifacts.read_json(value["source_registry_ref"]) == registry,
            "Retrieval selection belongs to another workflow or frozen original domain")
    sources = {source: rule for source, rule in registry["sources"].items() if rule["purpose"] == "news" and rule.get("default_urls")}
    require(set(sources) == set(run["news_policy"]["sources"]), "Selected retrieval changed the full frozen source policy")
    keys = set()
    for batch in value["selection_batches"]:
        fields(batch, {"source_id", "urls"}, label="original retrieval batch")
        require(batch["source_id"] in sources and type(batch["urls"]) is list and 0 < len(batch["urls"]) <= 8,
                "Selected retrieval batch is outside its original source or shard budget")
        original = set(sources[batch["source_id"]]["default_urls"])
        for url in batch["urls"]:
            key = (batch["source_id"], url)
            require(type(url) is str and url in original and key not in keys,
                    "Selected retrieval changed or duplicated an original protocol URL")
            keys.add(key)
    complement = artifacts.read_json(value["unselected_urls_ref"])
    expected = {"schema_id": "registry_news_seed_complement_v1", "source_registry_ref": value["source_registry_ref"],
        "source_registry_hash": value["source_registry_hash"],
        "selected_exact_keys": [{"source_id": source, "url": url} for source, url in sorted(keys)],
        "unselected_protocol_url_count": sum(len(set(rule["default_urls"])) for rule in sources.values())-len(keys),
        "scope": "all_original_news_default_urls_minus_selected_exact_protocol_URL_keys",
        "interpretation": "not_selected_by_this_engineering_plan_not_no_economic_effect"}
    require(complement == expected, "Retrieval omitted or changed part of its exact original URL complement")
    return value


def _parts(payload, store, artifacts):
    import news
    run, registry = news.run_policy(payload["run_id"], store, artifacts)
    require(payload["cutoff_at"] == run["publish_cutoff"] and payload["required_source_groups"] == run["news_policy"]["required_source_groups"], "Aggregate news policy or cutoff changed")
    ids = payload["collection_ids"]
    require(type(ids) is list and len(ids) == len(set(ids)) and all(type(key) is str and key for key in ids), "Distinct committed collection identities required")
    values = []
    for key in ids:
        journal = store.get("news-operation", fingerprint({"operation_id": key}))
        require(journal is not None, "Aggregate child has no committed source operation")
        manifest = artifacts.read_json(journal["manifest_ref"])
        require(manifest["collection_id"] == key and manifest["run_id"] == run["run_id"] and manifest["window_start"] == payload["window_start"]
            and manifest["source_selection_mode"] != "automatic_aggregate", "Aggregate child belongs to a different source window or is another aggregate")
        news.collection_policy(manifest, store, artifacts)
        news._verify_source_scopes(manifest, registry)
        at = instant(manifest["information_cutoff_at"])
        # Each child is independently replayed at its actual capture cutoff;
        # this does not promote a current capture to historical availability.
        if manifest["retrievals"]:
            news._verify_collection_bodies(manifest, registry, at, at, run["news_policy"]["max_age_seconds"], artifacts, store=store)
        values.append((manifest, journal["manifest_ref"]))
    observed = {source for value, _ in values for source in value["selected_source_ids"]}
    unknown = payload.get("unexamined_source_ids", [])
    require(type(unknown) is list and unknown == sorted(set(unknown)) and set(unknown) == set(run["news_policy"]["sources"])-observed,
        "Aggregate omitted part of the complete frozen publisher policy without its original unexamined scope")
    retrieval = (accepted_retrieval(payload["retrieval_operation_id"], run, registry, store, artifacts)
                 if payload.get("retrieval_operation_id") else None)
    if retrieval is not None:
        import source_fetch
        from workflow_news import validate_followup_scope
        derived = validate_followup_scope(payload, run, registry, store, artifacts)
        planned = {}
        for batch in retrieval["selection_batches"]:
            source = batch["source_id"]
            planned.setdefault(source, set()).update(source_fetch.checked_url(url, registry["sources"][source]) for url in batch["urls"])
        for manifest, _ in values:
            require(manifest["source_selection_mode"] == "explicit", "Selected retrieval requires original explicit-source shards")
            for scope in manifest["source_results"]:
                authorized = derived.get(manifest["collection_id"], {}).get(scope["source_id"], set())
                require(scope["source_id"] in planned and set(scope["urls"]) <= planned[scope["source_id"]] | authorized,
                        "News child was not scheduled by the accepted retrieval plan")
    return run, registry, values, retrieval


def _build(payload, operation_id, run, registry, parts, information_cutoff_at, *, retrieval=None, derived=None):
    import news
    retrievals = [copy.deepcopy(row) for value, _ in parts for row in value["retrievals"]]
    require(len({row["retrieval_id"] for row in retrievals}) == len(retrievals), "Aggregate duplicated an original retrieval")
    versions = {row["version_id"]: row for value, _ in parts for row in value["versions"]}
    aliases = {}
    for row in retrievals:
        if row["state"] == "body":
            aliases.setdefault(news.canonical_article_identity(row["final_url"]), set()).add(row["registry_source_id"])
    results = []
    selected = {}
    if retrieval is not None:
        for batch in retrieval["selection_batches"]:
            selected.setdefault(batch["source_id"], set()).update(batch["urls"])
    registry_hash = fingerprint(registry)
    for source in sorted(run["news_policy"]["sources"]):
        original = [row for value, _ in parts for row in value["source_results"] if row["source_id"] == source]
        if not original:
            urls = sorted(selected.get(source, ())) if retrieval is not None else list(registry["sources"][source]["default_urls"])
            scope = {"source_id": source, "scope": "enumerated_urls", "urls": urls, "registry_hash": registry_hash, "news_policy_hash": run["news_policy_hash"]}
            results.append({**scope, "scope_key": fingerprint(scope), "requested_window": {"start": payload["window_start"], "cutoff": payload["cutoff_at"]},
                "state": "partial", "visited_urls": [], "pending_urls": urls, "advance_cursor": False, "automatic_body_scope": False,
                "scheduled_article_urls": [], "discovery_pending_urls": urls, "next_task_kind": "listing", "linked_parent_urls": {},
                "watermark_key": fingerprint({"source_id": source, "registry_hash": registry_hash, "news_policy_hash": run["news_policy_hash"]}),
                "continued_from": None, "omitted_discovery_links": 0, "listing_pages_visited": 0, "maximum_listing_pages": registry["sources"][source].get("max_listing_pages", 3),
                "tracked_article_urls": [], "budget_exhausted": True, "coverage_claim": "unexamined_original_publisher_domain_no_source_or_window_coverage"})
            continue
        captures = [row for row in retrievals if row["registry_source_id"] == source]
        last = original[-1]
        urls = [row["requested_url"] for row in captures if news._article_url(row["requested_url"], registry["sources"][source])]
        scope = {"source_id": source, "scope": "explicit_article_urls" if urls else "enumerated_urls", "urls": urls or last["urls"],
            "registry_hash": registry_hash, "news_policy_hash": run["news_policy_hash"]}
        scoped = [row for row in captures if row["requested_url"] in scope["urls"]]
        pending = [url for url in scope["urls"] if url not in {row["requested_url"] for row in captures}]
        completed_urls = {row["requested_url"] for row in captures if row["state"] in {"lead", "body"}}
        discovery_pending = sorted({url for row in original
            for url in row["discovery_pending_urls"]+row["pending_urls"]}-completed_urls)
        linked_parents = {url: parent for row in original for url, parent in row["linked_parent_urls"].items()
                          if url in discovery_pending}
        complete = scope["scope"] == "explicit_article_urls" and bool(registry["sources"][source].get("complete_explicit_url_scope")) and bool(scoped) and not pending and all(row["state"] == "body" and row.get("within_information_cutoff") for row in scoped)
        results.append({**copy.deepcopy(last), **scope, "scope_key": fingerprint(scope), "automatic_body_scope": bool(urls),
            "scheduled_article_urls": urls, "visited_urls": sorted({row["requested_url"] for row in captures}), "pending_urls": pending,
            "discovery_pending_urls": discovery_pending, "linked_parent_urls": linked_parents,
            "advance_cursor": complete, "state": "checked_explicit_scope" if complete else "partial",
            "omitted_discovery_links": sum(row["omitted_discovery_links"] for row in original),
            "listing_pages_visited": sum(row["listing_pages_visited"] for row in original),
            "coverage_claim": "aggregated_exact_attempted_article_scope_not_source_window_exhaustiveness"})
    if retrieval is not None:
        import source_fetch
        for row in results:
            source = row["source_id"]
            planned = selected.get(source, set())
            original_count = len(set(registry["sources"][source]["default_urls"]))
            unselected = original_count-len(planned)
            visited = set(row["visited_urls"])
            unvisited = sorted(url for url in planned if source_fetch.checked_url(url, registry["sources"][source]) not in visited)
            selected_complete = row["advance_cursor"] and not unvisited
            row.update(selected_explicit_scope_complete=selected_complete,
                original_seed_count=original_count, unselected_original_seed_count=unselected,
                selected_seed_not_attempted_urls=unvisited,
                original_seed_complement={"descriptor_ref": retrieval["unselected_urls_ref"], "source_id": source,
                    "scope": "this_original_source_default_URLs_minus_accepted_selected_exact_keys"},
                original_shard_pending_refs=[{"collection_id": part["collection_id"], "manifest_ref": reference}
                    for part, reference in parts if source in part["selected_source_ids"]],
                original_seed_scope_complete=selected_complete and unselected == 0)
            if unselected or unvisited:
                row.update(scope="enumerated_urls", advance_cursor=False, state="partial",
                    coverage_claim="selected_retrieval_only_original_seed_complement_unexamined")
                row["pending_urls"] = sorted(set(row["pending_urls"]) | set(unvisited))
                row["scope_key"] = fingerprint({key: row[key] for key in ("source_id", "scope", "urls", "registry_hash", "news_policy_hash")})
    if payload.get("derived_source_tasks_ref") is not None:
        require(type(derived) is dict, "News aggregate lacks its authenticated source-derived inventory input")
        for row in results:
            remaining = [item["url"] for item in derived["pending"] if item["source_id"] == row["source_id"]]
            row["discovery_pending_urls"] = sorted(set(row["discovery_pending_urls"]) | set(remaining))
    result = {"schema_version": 4, "operation_id": operation_id, "collection_id": operation_id, "run_id": run["run_id"],
        "request_hash": fingerprint({"payload": payload, "registry": registry, "news_policy_hash": run["news_policy_hash"], "registry_snapshot_ref": run["registry_snapshot_ref"]}),
        "status": "collected" if all(row["advance_cursor"] for row in results) else "partial_collection",
        "registry_hash": fingerprint(registry), "registry_snapshot_ref": run["registry_snapshot_ref"], "news_policy_hash": run["news_policy_hash"],
        "selected_source_ids": sorted(run["news_policy"]["sources"]), "source_selection_mode": "automatic_aggregate",
        "cutoff_at": payload["cutoff_at"], "window_start": payload["window_start"], "information_cutoff_at": information_cutoff_at,
        "required_source_groups": copy.deepcopy(payload["required_source_groups"]), "source_group_coverage": news._group_coverage(payload["required_source_groups"], results),
        "source_results": results, "retrievals": retrievals, "versions": list(versions.values()), "analysis": [], "fact_verification": "not_performed",
        "coverage_complete_for_source_time_window": False, "budget_seconds": sum(value["budget_seconds"] for value, _ in parts),
        "capture_capacity": sum(value.get("capture_capacity", 400) for value, _ in parts),
        "aggregate_request": copy.deepcopy(payload), "aggregate_children": [{"collection_id": value["collection_id"], "manifest_ref": reference} for value, reference in parts],
        "coverage": {"frozen_protocols": len(run["news_policy"]["sources"]), "attempted_protocols": len({row["registry_source_id"] for row in retrievals}),
            "unique_original_publisher_identities": len({news.publisher_identity(row["final_url"]) for row in retrievals if row.get("final_url")}),
            "unique_original_article_identities": len({news.canonical_article_identity(row["final_url"]) for row in retrievals if row.get("final_url") and row["state"] == "body"}),
            "article_protocol_aliases": {identity: sorted(values) for identity, values in sorted(aliases.items())},
            "observed_article_versions": len(versions), "discovery_pending_urls": sum(len(row["discovery_pending_urls"]) for row in results),
            "omitted_discovery_links": sum(row["omitted_discovery_links"] for row in results), "world_news_denominator": None,
            "publisher_time_window_article_denominator": None, "news_time_window_exhaustiveness_proven": False},
        "trade_ready": False, "all_news_coverage_claimed": False}
    if retrieval is not None:
        result["retrieval_plan_binding"] = {"retrieval_operation_id": retrieval["retrieval_id"],
            "retrieval_hash": fingerprint(retrieval), "complete_opportunities_ref": retrieval["complete_opportunities_ref"],
            "query_tasks_ref": retrieval["query_tasks_ref"], "unselected_urls_ref": retrieval["unselected_urls_ref"],
            "scope": "accepted_engineering_selection_complete_original_domain_retained_not_news_exhaustiveness"}
        result["coverage"].update(unselected_original_protocol_urls=sum(row["unselected_original_seed_count"] for row in results),
            selected_original_protocol_urls_not_attempted=sum(len(row["selected_seed_not_attempted_urls"]) for row in results),
            complete_original_seed_scope=all(row["original_seed_scope_complete"] for row in results))
    if payload.get("derived_source_tasks_ref") is not None:
        result["source_derived_plan_binding"] = {"task_inventory_ref": payload["derived_source_tasks_ref"],
            "scope": "actual_original_parent_links_preregistered_tasks_same_cumulative_transport_budget_not_news_exhaustiveness"}
    return result


def aggregate(payload, store, artifacts, operation_id):
    store.assert_owned()
    run, registry, parts, retrieval = _parts(payload, store, artifacts)
    previous = store.get("news-operation", fingerprint({"operation_id": operation_id}))
    if previous:
        result = artifacts.read_json(previous["manifest_ref"])
        require(result.get("aggregate_request") == payload, "News aggregate inputs changed")
        validate_aggregate(result, store, artifacts)
        return {**result, "manifest_ref": previous["manifest_ref"], "reused": True}
    require(operation_id not in payload["collection_ids"], "Aggregate cannot refer to itself")
    derived = artifacts.read_json(payload["derived_source_tasks_ref"]) if payload.get("derived_source_tasks_ref") is not None else None
    value = _build(payload, operation_id, run, registry, parts, utc_now(), retrieval=retrieval, derived=derived)
    reference = artifacts.put_json(value)
    with store.transaction() as conn:
        store.put("news-information-seal", operation_id, {key: value[key] for key in ("operation_id", "run_id", "request_hash", "information_cutoff_at", "news_policy_hash", "registry_hash")}, immutable=True, conn=conn)
        store.put("news-operation", fingerprint({"operation_id": operation_id}), {"request_hash": value["request_hash"], "manifest_ref": reference}, immutable=True, conn=conn)
    return {**value, "manifest_ref": reference, "reused": False}


def validate_aggregate(value, store, artifacts):
    import news
    require(value.get("source_selection_mode") == "automatic_aggregate", "Not a canonical news aggregate")
    payload = value["aggregate_request"]
    run, registry, parts, retrieval = _parts(payload, store, artifacts)
    require(instant(value["information_cutoff_at"]) >= max((instant(part["information_cutoff_at"]) for part, _ in parts), default=instant(run["publish_cutoff"])), "Aggregate precedes an original child capture")
    derived = artifacts.read_json(payload["derived_source_tasks_ref"]) if payload.get("derived_source_tasks_ref") is not None else None
    rebuilt = _build(payload, value["collection_id"], run, registry, parts, value["information_cutoff_at"], retrieval=retrieval, derived=derived)
    canonical = {key: item for key, item in value.items() if key not in {"manifest_ref", "reused"}}
    require(canonical == rebuilt, "News aggregate differs from independently replayed original children")
    news.collection_policy(value, store, artifacts)
    return rebuilt
