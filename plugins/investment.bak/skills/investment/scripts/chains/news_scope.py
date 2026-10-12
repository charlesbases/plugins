"""Prerequisition research questions and independently checked original audits.

World coverage, seed transport closure and research-question completion are
different scopes. A download budget or an analyst's boolean proves none of them.
"""
import copy

from artifacts import Artifacts
from contracts import fields, fingerprint, instant, require, utc_now
from state_store import Store
import stage_validation
import verify

PLAN_SCHEMA = "investment_dynamic_news_research_plan_v1"
AUDIT_SCHEMA = "investment_dynamic_news_research_audit_v1"


def _native(store, artifacts, identity, operation, reference=None):
    saved = store.operation(identity)
    require(saved is not None and saved["status"] == "completed"
            and saved["request"]["operation"] == operation, "Research scope needs its exact completed public " + operation)
    result = saved["result"]
    require(reference is None or artifacts.read_json(reference) == result,
            "Scope source CAS differs from its canonical public operation")
    automatic = result.get("automatic_validation")
    require(type(automatic) is dict, "Scope inputs need independent accepted-attempt and atomic-commit proof")
    bindings = artifacts.read_json(automatic["operation_bindings_ref"])
    stage_validation.validate_operation_result(saved["request"], result, store, artifacts,
                                              {**bindings, "historical": True})
    return saved, {key: value for key, value in result.items() if key != "automatic_validation"}


def _questions(retrieval, registry, artifacts):
    gkg = set(artifacts.read_json(retrieval["GKG_source_ids_ref"]))
    questions = []
    for name, rule in sorted(registry["sources"].items()):
        if rule["purpose"] == "news" and rule.get("default_urls") and name not in gkg:
            questions.append({"question_id": "official-" + fingerprint({"source_id": name}),
                "kind": "original_publisher_research", "source_id": name,
                "question": "Read this current original publisher's economic evidence and seek contrary original evidence; explain remaining material unknowns.",
                "original_entry_urls": copy.deepcopy(rule["default_urls"])})
    for task in artifacts.read_json(retrieval["query_tasks_ref"]):
        questions.append({"question_id": task["query_id"], "kind": "original_metadata_question",
            "machine_theme": task["machine_theme"], "question": task["question"],
            "original_task_state": task["status"], "metadata_priority_hint": task["metadata_priority_hint"]})
    require(len({row["question_id"] for row in questions}) == len(questions), "Research questions have duplicate identities")
    return questions


def freeze_plan(root, plan, chain_id, retrieval, publishers, run, *, account_ref):
    """Prepare's pre-collection callback; same-id recovery keeps the first clock."""
    store, artifacts = Store(root, plan), None
    artifacts = Artifacts(store.base)
    source_refs = {}
    native = {}
    for suffix, operation, supplied in (("publishers", "publisher_discover", publishers),
            ("retrieval", "retrieval_prepare", retrieval), ("analysis", "analysis_start", {"run": run})):
        saved, value = _native(store, artifacts, chain_id + ":" + suffix, operation)
        if suffix == "analysis":
            require(value["run"] == run, "Scope analysis run was substituted")
        else:
            require(saved["result"] == supplied, "Scope source result was substituted")
        source_refs[suffix] = artifacts.put_json(saved["result"])
        native[suffix] = value
    registry = artifacts.read_json(native["publishers"]["source_registry_ref"])
    request = {"schema_id": PLAN_SCHEMA, "chain_id": chain_id, "source_refs": source_refs,
               "account_ref": account_ref, "run_id": run["run_id"], "policy_hash": run["news_policy_hash"]}
    identity = chain_id + ":scope-plan"
    saved = store.begin(identity, request)
    if saved["status"] == "completed":
        return saved["result"]["plan_ref"]
    # This declaration journal is not a public source/scientific qualification.
    # It records the real pre-collection clock and deterministic question set.
    with store.lease(identity):
        require(store.operation(chain_id + ":collect") is None,
                "A research scope cannot be declared retroactively after its collection")
        value = {**request, "frozen_at": store.operation(identity)["created_at"],
            "questions": _questions(native["retrieval"], registry, artifacts),
            "opportunity_domain_ref": native["retrieval"]["complete_opportunities_ref"],
            "complement_ref": native["retrieval"]["unselected_urls_ref"],
            "source_registry_ref": native["publishers"]["source_registry_ref"],
            "required_source_groups": copy.deepcopy(run["news_policy"]["required_source_groups"]),
            "scope": "all_current_dynamic_official_protocols_and_original_metadata_questions_not_all_world_news",
            "budget_role": "transport_only_not_a_question_or_coverage_limit"}
        reference = artifacts.put_json(value)
        store.complete(identity, {"plan_ref": reference})
        return reference


def request_templates(scope_plan_ref, scope_plan):
    return {"submission": {"scope_plan_ref": scope_plan_ref,
        "analysis": {"claims": [], "events": [], "industry_theses": [], "economic_observations": []},
        "query_reviews": [{"question_id": row["question_id"], "support_claim_ids": [],
            "counter_claim_ids": [], "unresolved_material_claim_ids": [],
            "disposition": "unknown", "disposition_claim_id": None,
            "covered_question_id": None, "source_census_binding": None, "reason": ""}
            for row in scope_plan["questions"]]},
        "scope": "host_internal_original_reading_audit_not_user_algorithm_questionnaire_or_scientific_qualification"}


def _query_mappings(rows, questions, claims):
    """Validate a complete draft map without interpreting an unknown as success."""
    require(type(rows) is list, "Scope question reviews must be explicit")
    mappings = {}
    for row in rows:
        require(type(row) is dict and set(row) == {"question_id", "support_claim_ids", "counter_claim_ids", "unresolved_material_claim_ids",
            "disposition", "disposition_claim_id", "covered_question_id", "source_census_binding", "reason"},
                "Scope review must preserve supported, contrary and unresolved question evidence")
        require(type(row["question_id"]) is str and row["question_id"] in questions,
                "Scope inserted or substituted research questions")
        require(row["question_id"] not in mappings, "Scope question mapping is duplicated")
        require(type(row["disposition"]) is str and row["disposition"] in
                {"material", "nonmaterial", "covered_by_article_event", "no_current_material", "unknown"},
                "Research questions require actual material, conditional nonmaterial, duplicate, census or unknown dispositions")
        require(type(row["reason"]) is str and row["reason"].strip(), "Scope review needs its concrete original-evidence rationale")
        for field in ("support_claim_ids", "counter_claim_ids", "unresolved_material_claim_ids"):
            values = row[field]
            require(type(values) is list and all(type(identity) is str and identity in claims for identity in values)
                    and len(values) == len(set(values)), "Scope mapping references unsupported or duplicate claims")
        disposition_claim = row["disposition_claim_id"]
        require(disposition_claim is None or type(disposition_claim) is str and disposition_claim in claims,
                "Scope disposition references an unsupported claim")
        if row["disposition"] == "nonmaterial":
            require(disposition_claim in row["support_claim_ids"], "Conditional nonmaterial disposition needs its actual supported claim")
        covered = row["covered_question_id"]
        require(covered is None or type(covered) is str and covered in questions and covered != row["question_id"],
                "Scope duplicate disposition needs another original research question")
        if row["disposition"] == "covered_by_article_event":
            require(covered is not None, "Scope duplicate disposition needs another original research question")
        census = row["source_census_binding"]
        require(census is None or type(census) is dict and set(census) == {"operation_id", "result_ref"}
                and type(census["operation_id"]) is str and census["operation_id"]
                and type(census["result_ref"]) is dict, "Source census requires its actual canonical operation and result reference")
        mappings[row["question_id"]] = row
    require(set(mappings) == set(questions), "Scope audit must preserve every original research question; use explicit unknown dispositions")
    return mappings


def _preflight_assessment(payload, manifest, store, artifacts):
    """Run native read-only draft/source predicates before either immutable id.

    This derives no qualification and writes no operation, evidence or clock.
    The actual public assessment still repeats its independent boundary checks.
    """
    import news
    import news_economics
    fields(payload, {"collection_id", "claims", "industry_theses", "events"},
           {"run_id", "economic_observations"}, label="news assessment")
    structure = news.assessment_structure_preflight(payload, manifest)
    require(structure["status"] == "passed", "News assessment structural preflight failed: " + str(structure["issues"]))
    run, registry = news.collection_policy(manifest, store, artifacts)
    require(payload["run_id"] == run["run_id"] and payload["collection_id"] == manifest["collection_id"],
            "Scope draft belongs to another actual original run or collection")
    reviewed_at = utc_now()
    news._verify_collection_bodies(manifest, registry, instant(reviewed_at), instant(reviewed_at),
        run["news_policy"]["max_age_seconds"], artifacts, store=store)
    news._verify_source_scopes(manifest, registry)
    claims, _ = news._assemble_claims(payload, manifest, artifacts, set(run["news_policy"]["sources"]), registry)
    events = news._assemble_events(payload, manifest, claims, artifacts, reviewed_at, registry)
    for event in events:
        first_source = store.get("news-source-revision-first-known", event["source_revision_id"])
        if first_source is not None:
            event["source_known_at"] = first_source["first_known_at"]
        existing = store.get("news-event-revision", event["revision_id"])
        if existing is not None:
            for key in ("known_at", "first_seen_at", "publication_role"):
                event[key] = existing[key]
        else:
            earlier = [row["first_seen_at"] for _, row in store.scan("news-event-revision") if row["event_id"] == event["event_id"]]
            event["first_seen_at"] = min(earlier, key=instant) if earlier else reviewed_at
    news._validate_revision_links(events, store)
    theses = news._assemble_theses(payload, claims, events, manifest["required_source_groups"])
    economic = news_economics.assemble(payload.get("economic_observations", []), manifest, claims, events,
        artifacts, reviewed_at, store=store, registry=registry)
    news._require_economic_assignments(economic, theses)
    return {row["id"]: row for row in claims}


def submit_review(root, plan, chain_id, plan_ref, collection_binding, submission):
    """Submit quoted source evidence; the separate mapping is not a verdict."""
    require(type(submission) is dict and set(submission) == {"scope_plan_ref", "analysis", "query_reviews"},
            "Scope audit requires the exact plan, native quoted analysis and full question mapping")
    require(submission["scope_plan_ref"] == plan_ref, "Scope audit changed its prerequisition plan")
    require(type(submission["query_reviews"]) is list, "Scope question reviews must be explicit")
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    scope_plan = artifacts.read_json(plan_ref)
    _, collected = _native(store, artifacts, collection_binding["operation_id"], "news_collect", collection_binding["result_ref"])
    manifest = artifacts.read_json(collected["manifest_ref"])
    require(manifest["run_id"] == scope_plan["run_id"], "Scope audit belongs to another original collection run")
    require(type(submission["analysis"]) is dict, "Scope source analysis must be an explicit native object")
    # The full mapping is part of this invocation's immutable audit request.
    # A completed source quote operation cannot be reused to quietly delete
    # known material unknowns or reset a mapping under another CAS reference.
    identity = chain_id + ":scope-audit"
    request = {"schema_id": AUDIT_SCHEMA, "chain_id": chain_id, "scope_plan_ref": plan_ref,
               "collection_binding": collection_binding, "submission": copy.deepcopy(submission)}
    prior = store.operation(identity)
    if prior is not None and prior["status"] == "completed":
        require(prior["request"] == request, "Completed scope audit changed its original immutable submission")
        audit_ref = prior["result"]["audit_ref"]
        audit = artifacts.read_json(audit_ref)
        review_ref = audit["review_binding"]["result_ref"]
        return audit_ref, {"operation_id": chain_id + ":scope-assess", "operation": "news_assess",
                           "status": "completed", "source_ref": review_ref}
    payload = {"run_id": manifest["run_id"], "collection_id": manifest["collection_id"], **copy.deepcopy(submission["analysis"])}
    claims = _preflight_assessment(payload, manifest, store, artifacts)
    if not claims:
        # A blank internal template is pending host work, not a formal attempt
        # to seal an empty assessment that could never be edited under its id.
        return None, None
    questions = {row["question_id"]: row for row in scope_plan["questions"]}
    mappings = _query_mappings(submission["query_reviews"], questions, claims)
    for row in mappings.values():
        census = row["source_census_binding"]
        if census is not None:
            _, value = _native(store, artifacts, census["operation_id"], "news_collect", census["result_ref"])
            require(value["run_id"] == scope_plan["run_id"], "Source census belongs to another research window")
    # Only a source-valid, structurally complete draft may consume these ids.
    # Honest unknowns are valid sealed observations, not draft errors.
    store.begin(identity, request)
    import pipeline
    result = pipeline.run({"schema_version": 4, "request_id": chain_id + ":scope-assess",
                           "operation": "news_assess", "payload": payload}, root, plan)
    reference = artifacts.put_json(result)
    audit = {"schema_id": AUDIT_SCHEMA, "chain_id": chain_id, "scope_plan_ref": plan_ref,
        "collection_binding": collection_binding,
        "review_binding": {"operation_id": chain_id + ":scope-assess", "result_ref": reference},
        "query_reviews": copy.deepcopy(submission["query_reviews"]),
        "reviewed_at": utc_now(), "qualification": "untrusted_mapping_requires_independent_scope_validation"}
    audit_ref = artifacts.put_json(audit)
    with store.lease(identity):
        store.complete(identity, {"audit_ref": audit_ref})
    return audit_ref, {"operation_id": chain_id + ":scope-assess", "operation": "news_assess",
                                      "status": "completed", "source_ref": reference}


def validate_scope(root, plan, chain_id, plan_ref, collection_binding, material_binding, audit_ref=None, *, account_ref):
    """Derive domain completion from all real questions, quoted support and counters.

    The source-quote gate verifies each original before these coverage checks.
    Original machine routing states and unselected complements remain unchanged.
    Missing questions or genuine material uncertainty never become exclusions.
    """
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    scope_plan = artifacts.read_json(plan_ref)
    declared = store.operation(chain_id + ":scope-plan")
    require(declared is not None and declared["status"] == "completed"
            and declared["result"] == {"plan_ref": plan_ref}, "Scope plan was not actually sealed before collection")
    require(scope_plan["schema_id"] == PLAN_SCHEMA and scope_plan["chain_id"] == chain_id
            and scope_plan["account_ref"] == account_ref, "Research plan belongs to another chain or account")
    material_operation = store.operation(material_binding["operation_id"])
    if (material_operation is not None and material_operation["status"] == "completed"
            and type(material_operation["result"]) is dict
            and material_operation["result"].get("source_reference_closure_ref") is not None):
        reference_check = stage_validation.accepted_source_reference_checker(
            material_binding["operation_id"], store, artifacts)
        reference_check(scope_plan)
    else:
        verify.references(scope_plan, artifacts)
    _, publisher = _native(store, artifacts, chain_id + ":publishers", "publisher_discover", scope_plan["source_refs"]["publishers"])
    _, retrieval = _native(store, artifacts, chain_id + ":retrieval", "retrieval_prepare", scope_plan["source_refs"]["retrieval"])
    registry = artifacts.read_json(publisher["source_registry_ref"])
    require(scope_plan["questions"] == _questions(retrieval, registry, artifacts)
            and scope_plan["complement_ref"] == retrieval["unselected_urls_ref"]
            and scope_plan["opportunity_domain_ref"] == retrieval["complete_opportunities_ref"],
            "Scope changed the complete original question domain or complement")
    collection_op, collected = _native(store, artifacts, collection_binding["operation_id"], "news_collect", collection_binding["result_ref"])
    require(instant(scope_plan["frozen_at"]) < instant(collection_op["created_at"]), "Scope was frozen after original collection")
    _, material = _native(store, artifacts, material_binding["operation_id"], "material_prepare", material_binding["result_ref"])
    require(material["collection_manifest_ref"] == collected["manifest_ref"] and material["run_id"] == scope_plan["run_id"],
            "Scope originals do not belong to its exact current collection")
    manifest = artifacts.read_json(collected["manifest_ref"])
    result = {"status": "needs_evidence", "source_integrity": material["total_materials"] > 0,
        "research_scope_sufficient": False, "global_coverage_known": False,
        "actual_host_audit_completed": False,
        "original_seed_scope_complete": manifest.get("complete_original_seed_scope") is True,
        "plan_ref": plan_ref, "audit_ref": audit_ref, "query_dispositions": [], "required_actions": [],
        "complement_ref": scope_plan["complement_ref"], "opportunity_domain_ref": scope_plan["opportunity_domain_ref"]}
    gaps = result["required_actions"]
    if not all(row["status"] == "captured" for row in publisher["directory_captures"]):
        gaps.append({"rule_path": "scope.dynamic_publisher_domain", "action": "complete_actual_authority_directory_evidence_for_dynamic_research_scope"})
    if audit_ref is None:
        gaps.append({"rule_path": "scope.actual_original_audit", "action": "host_read_current_originals_and_submit_scope_audit"})
        return result
    audit = artifacts.read_json(audit_ref)
    audit_operation = store.operation(chain_id + ":scope-audit")
    require(audit_operation is not None and audit_operation["status"] == "completed"
            and audit_operation["result"] == {"audit_ref": audit_ref}
            and audit_operation["request"]["submission"]["query_reviews"] == audit["query_reviews"],
            "Scope question mapping differs from its original immutable complete submission")
    require(audit["schema_id"] == AUDIT_SCHEMA and audit["chain_id"] == chain_id
            and audit["scope_plan_ref"] == plan_ref and audit["collection_binding"] == collection_binding,
            "Scope audit binds another plan, collection or chain")
    require(audit["review_binding"]["operation_id"] == chain_id + ":scope-assess",
            "Scope audit cannot depend on the later formal industry assessment")
    _, review = _native(store, artifacts, chain_id + ":scope-assess", "news_assess", audit["review_binding"]["result_ref"])
    require(review["collection_manifest_ref"] == collected["manifest_ref"] and review["run_id"] == scope_plan["run_id"],
            "Quoted scope evidence belongs to another current original collection")
    result["actual_host_audit_completed"] = review["status"] == "assessed" and bool(review["claims"])
    if not result["actual_host_audit_completed"]:
        gaps.append({"rule_path": "scope.actual_original_audit", "action": "host_complete_actual_original_scope_reading_analysis"})
        result["status"] = "awaiting_analysis"
        return result
    claims = {row["id"]: row for row in review["claims"]}
    versions = {row["version_id"]: row for row in manifest["versions"]}
    questions = {row["question_id"]: row for row in scope_plan["questions"]}
    mappings = _query_mappings(audit["query_reviews"], questions, claims)
    # The accepted material boundary independently rebuilt this exact-version
    # index from the complete original domain. Reauthenticate its current CAS,
    # code, request and commit proof instead of replaying every metadata row.
    from news_membership import validated_memberships
    memberships = validated_memberships(material_binding["operation_id"], store, artifacts,
        collection_manifest_ref=collected["manifest_ref"],
        retrieval_operation_id=chain_id + ":retrieval",
        retrieval_result_ref=scope_plan["source_refs"]["retrieval"])
    observed_sources = set()
    for identity, question in questions.items():
        row = mappings.get(identity)
        support = [claims[key] for key in row["support_claim_ids"]] if row else []
        counter = [claims[key] for key in row["counter_claim_ids"]] if row else []
        relevant = [claim for claim in support if (versions[claim["version_id"]]["source_id"] == question.get("source_id")
            if question["kind"] == "original_publisher_research" else
            question["machine_theme"] in memberships[claim["version_id"]])]
        # A second unrelated quote is not a counter. The accepted original
        # claim must itself bind that exact contrary quote and independent
        # publisher/article/body provenance.
        def has_counter(claim):
            return any(evidence.get("source_independent_of_primary") is True
                and evidence.get("version_id") == b["version_id"] and evidence.get("quote") == b["quote"]
                for evidence in claim["counterevidence"] for b in counter)
        different = bool(relevant) and all(has_counter(claim) for claim in relevant)
        complete = bool(row and relevant and different and not row["unresolved_material_claim_ids"])
        finding = "material_evidence_pending"
        if row and row["disposition"] == "unknown":
            complete = False
        elif row and row["disposition"] == "nonmaterial":
            inference = claims.get(row["disposition_claim_id"])
            # This is a bounded, quote-backed research finding. It never
            # asserts zero economic effect or certifies the wider complement.
            complete &= bool(inference and inference in relevant and inference["kind"] == "inference"
                             and inference["direction"] == "neutral" and has_counter(inference))
            finding = "current_research_no_material_association_established" if complete else finding
        elif row and row["disposition"] == "covered_by_article_event":
            target = mappings.get(row["covered_question_id"])
            require(row["covered_question_id"] != identity, "A research question cannot cover itself")
            target_claims = [claims[key] for key in target["support_claim_ids"]] if target else []
            same_article = any(a["support"]["canonical_article_id"] == b["support"]["canonical_article_id"]
                               and a["support"]["body_sha256"] == b["support"]["body_sha256"] for a in relevant for b in target_claims)
            complete &= bool(target and target["disposition"] in {"material", "nonmaterial"}
                             and not target["unresolved_material_claim_ids"] and same_article)
            finding = "same_actual_original_article_covered" if complete else finding
        elif row and row["disposition"] == "no_current_material":
            # A plan's empty transport lane is not a source census. This branch
            # requires an actually accepted, complete original enumeration;
            # current unsupported source-window protocols remain pending.
            census = row["source_census_binding"]
            complete = False
            if census is not None:
                _, census_result = _native(store, artifacts, census["operation_id"], "news_collect", census["result_ref"])
                census_manifest = artifacts.read_json(census_result["manifest_ref"])
                require(census_manifest["run_id"] == scope_plan["run_id"], "Source census belongs to another research window")
                census_rows = [item for item in census_manifest["source_results"]
                               if item["source_id"] == question.get("source_id")]
                complete = bool(census_manifest.get("coverage_complete_for_source_time_window") is True
                    and census_rows and all(item["advance_cursor"] for item in census_rows)
                    and not census_manifest["versions"] and not row["unresolved_material_claim_ids"])
            finding = "complete_original_census_no_current_material" if complete else finding
        elif complete:
            finding = "material_originals_and_counterevidence_checked"
        disposition = {"question_id": identity, "status": "originals_and_counterevidence_checked" if complete else "material_evidence_pending",
            "finding": finding, "disposition": row["disposition"] if row else "unknown",
            "support_claim_ids": row["support_claim_ids"] if row else [], "counter_claim_ids": row["counter_claim_ids"] if row else []}
        result["query_dispositions"].append(disposition)
        if complete:
            observed_sources.update(versions[claim["version_id"]]["source_id"] for claim in relevant + counter)
        else:
            gaps.append({"rule_path": "scope.question." + identity, "action": "complete_original_support_counterevidence_and_material_unknowns"})
    groups = scope_plan["required_source_groups"]
    domestic = groups.get("registered_domestic_publishers", [])
    international = groups.get("registered_international_publishers", [])
    if not set(domestic) & observed_sources or not set(international) & observed_sources:
        gaps.append({"rule_path": "scope.current_domestic_and_international_originals", "action": "complete_current_original_geographic_research_evidence"})
    if not questions:
        gaps.append({"rule_path": "scope.dynamic_question_domain", "action": "obtain_actual_broad_dynamic_research_questions"})
    result["research_scope_sufficient"] = not gaps
    result["status"] = "passed" if not gaps else "needs_evidence"
    result["supporting_operation_id"] = chain_id + ":scope-assess"
    result["qualification_scope"] = "conditional_current_prerequisition_dynamic_research_questions_originals_and_counterevidence_not_zero_impact_global_coverage_or_predictive_validation"
    return result
