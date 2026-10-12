"""Current industry source tasks and authenticated contracts, never model readiness.

The host supplies originals through public source_capture/fee_inspect operations.
Invalid drafts are checked before any stable market request or source seal.
"""
import copy

from contracts import EvidenceError, fields, fingerprint, instant, require, utc_now
from chains.common import bound_result, context, state

TASK_SCHEMA = "investment_current_industry_source_task_v1"
BINDING_SCHEMA = "investment_current_industry_source_binding_v1"
TASK_KIND = "chain-industry-source-task"
BINDING_KIND = "chain-industry-source-binding"
SUBMISSION_FIELDS = {"source_task_ref", "benchmark_contracts", "industry_dispositions", "source_captures"}


def _task_request(task, reference):
    return {"schema_id": TASK_SCHEMA, "chain_id": task["chain_id"], "task_ref": reference,
            "purpose": "engineering_source_task_declaration_not_scientific_qualification"}


def _verify_task_journal(store, task, reference):
    operation = store.operation(task["chain_id"] + ":industry-source-task")
    require(operation is not None and operation["status"] == "completed"
            and operation["request"] == _task_request(task, reference)
            and operation["result"] == {"task_ref": reference},
            "Industry source task lacks its exact completed declaration journal")
    require(store.get(TASK_KIND, task["chain_id"]) == {"task_ref": reference},
            "Industry source task record differs from its atomic declaration")


def _basis(root, plan, chain_id, engine, account_ref):
    require(engine.get("workflow_id") == chain_id, "Industry source workflow differs from this chain")
    _, artifacts = state(root, plan)
    require(account_ref is not None, "Industry source task requires the first account reference")
    artifacts.read_json(account_ref)
    review = bound_result(root, plan, engine["news_review"], "news_assess")
    discovery = bound_result(root, plan, engine["discovery"], "fund_discover")
    core = {key: value for key, value in review.items() if key != "automatic_validation"}
    require(discovery["news_review_hash"] == fingerprint(core), "Industry source discovery belongs to another assessment")
    snapshot = artifacts.read_json(discovery["identity_snapshot_ref"])
    inventory = [{"thesis_id": row["thesis_id"], "sector_id": row.get("sector_id"),
                  "kind": row["kind"], "search_terms": copy.deepcopy(row["search_terms"]),
                  "claim_ids": copy.deepcopy(row["claim_ids"])} for row in core["industry_theses"]]
    require(len({row["thesis_id"] for row in inventory}) == len(inventory), "Original industry inventory is duplicated")
    hints = [{"code": code, "tracking_target": row.get("tracking_target"),
              "benchmark_description": row.get("benchmark_description"),
              "distributor_comparison_key": row.get("benchmark_id"),
              "sector_exposures": copy.deepcopy(row.get("sector_exposures")),
              "scope": "original_identity_query_leads_not_official_benchmark_contracts"}
             for code, row in sorted(snapshot["identities"].items())]
    return {"schema_id": TASK_SCHEMA, "chain_id": chain_id, "account_ref": account_ref,
            "news_review": copy.deepcopy(engine["news_review"]), "discovery": copy.deepcopy(engine["discovery"]),
            "news_review_hash": fingerprint(core), "identity_snapshot_ref": discovery["identity_snapshot_ref"],
            "industry_inventory": inventory, "fund_identity_leads": hints,
            "issuer_document_refs": [row["document_ref"] for row in snapshot["issuer_documents"]],
            "fee_scope_codes": sorted(discovery["codes"]),
            "source_capture_payload_fields": ["url", "source_id"],
            "fee_inspect_payload": {"schema_version": 4, "adapter": "document_formats_v2",
                "sources_fields": ["product", "prospectus", "platform", "cutoff", "calendars"],
                "scope": "actual_original_refs_required_no_default_fee_or_calendar"},
            "source_work": "read actual official identities, units and definitions; capture originals; retain unsupported industries as unknown",
            "historical_qualification_granted": False}


def _task(root, plan, previous):
    store, artifacts = state(root, plan)
    task = _basis(root, plan, previous["chain_id"], context(previous), previous["account_ref"])
    reference = artifacts.put_json(task)
    identity = previous["chain_id"] + ":industry-source-task"
    saved = store.begin(identity, _task_request(task, reference))
    if saved["status"] != "completed":
        with store.lease(identity):
            store.complete(identity, {"task_ref": reference}, writes=[
                {"kind": TASK_KIND, "key": previous["chain_id"], "value": {"task_ref": reference}, "immutable": True}])
    _verify_task_journal(store, task, reference)
    template = {"source_task_ref": reference, "benchmark_contracts": [],
        "industry_dispositions": [{"thesis_id": row["thesis_id"], "sector_id": row["sector_id"],
            "status": "unknown", "reason": ""} for row in task["industry_inventory"]], "source_captures": []}
    template_ref = artifacts.put_json({"submission": template,
        "instructions": "Fill actual authenticated contracts or specific unknown reasons; optional current-chain fee_inspections; never invent source values or times",
        "task_ref": reference})
    return task, reference, template_ref


def _authenticated_operations(root, plan, chain_id, supplied, operation):
    require(type(supplied) is list, "Source operation bindings must be an array")
    records, seen = [], set()
    for binding in supplied:
        require(type(binding) is dict and set(binding) == {"operation_id", "result_ref"}, "Exact source operation binding required")
        identity = binding["operation_id"]
        require(type(identity) is str and identity.startswith(chain_id + ":"), "Source operation must belong to this exact current chain")
        require(identity not in seen, "Source operation binding is duplicated")
        seen.add(identity)
        records.append((binding, bound_result(root, plan, binding, operation)))
    return records


def _preflight(root, plan, chain_id, task, task_ref, submission, at):
    fields(submission, SUBMISSION_FIELDS, {"fee_inspections"}, "internal industry source submission")
    require(submission["source_task_ref"] == task_ref, "Source submission uses a different original task")
    _, artifacts = state(root, plan)
    require(artifacts.read_json(task_ref) == task, "Original source task CAS changed")
    captures = _authenticated_operations(root, plan, chain_id, submission["source_captures"], "source_capture")
    captured_refs = {fingerprint(result["document_ref"]): result["document_ref"] for _, result in captures}
    contracts = submission["benchmark_contracts"]
    dispositions = submission["industry_dispositions"]
    require(type(contracts) is list and type(dispositions) is list, "Actual contracts and full industry dispositions must be arrays")
    inventory = {row["thesis_id"]: row for row in task["industry_inventory"]}
    by_thesis = {}
    for row in dispositions:
        fields(row, {"thesis_id", "sector_id", "status", "reason"}, label="industry source disposition")
        identity = row["thesis_id"]
        require(type(identity) is str and identity in inventory and identity not in by_thesis,
                "Industry disposition must belong once to the complete original inventory")
        require(row["sector_id"] == inventory[identity]["sector_id"], "Industry disposition changed the actual source sector identity")
        require(row["status"] in ("supported", "unknown") and type(row["reason"]) is str and row["reason"].strip(),
                "Industry disposition needs a supported/unknown status and a concrete reason")
        by_thesis[identity] = row
    require(set(by_thesis) == set(inventory), "Industry dispositions omitted the complete original inventory")
    import industry_data
    seen_sectors, supported = set(), set()
    definitions = []
    for supplied in contracts:
        contract = industry_data._contract(copy.deepcopy(supplied))
        require(contract["sector_id"] not in seen_sectors, "Repeated actual industry benchmark contract")
        seen_sectors.add(contract["sector_id"])
        require(contract.get("identity_document_ref") is not None,
                "Host must capture the real identity before submitting a source contract")
        require(fingerprint(contract["identity_document_ref"]) in captured_refs,
                "Industry identity lacks its authenticated current-chain source_capture binding")
        taxonomy_ref = contract["asset_domain"].get("taxonomy", {}).get("document_ref")
        require(taxonomy_ref is None or fingerprint(taxonomy_ref) in captured_refs,
                "Industry taxonomy lacks its authenticated current-chain source_capture binding")
        for identity in contract["thesis_ids"]:
            require(identity in inventory and inventory[identity]["sector_id"] == contract["sector_id"],
                    "Contract source sector or thesis differs from the original industry assessment")
            require(by_thesis[identity]["status"] == "supported", "A contract cannot overwrite an unknown industry disposition")
            supported.add(identity)
        document = industry_data._identity(contract, artifacts, at)
        relation = industry_data._sector_relation(contract, document, artifacts, at)
        require(relation["status"] == "verified", "Actual benchmark definition does not support the submitted industry source role")
        if contract["asset_domain"]["role"] == "single_sector":
            import source_documents
            label = source_documents.normalize_text(contract["sector_id"])
            literal = label in source_documents.normalize_text(relation["quote"])
            mapped = any(row["entity_id"] == contract["sector_id"] and row["role"] == "single_sector"
                         for row in contract["asset_domain"].get("taxonomy", {}).get("mappings", []))
            require(literal or mapped,
                    "Single-sector identity is absent from the actual quoted definition or authenticated original taxonomy")
        definitions.append(relation)
    require(supported == {identity for identity, row in by_thesis.items() if row["status"] == "supported"},
            "Supported industry disposition has no exact source contract")
    fees = _authenticated_operations(root, plan, chain_id, submission.get("fee_inspections", []), "fee_inspect")
    fee_codes = set()
    for _, result in fees:
        reference = result.get("contract_ref")
        if reference is None:
            continue
        contract = artifacts.read_json(reference)
        require(contract["code"] in task["fee_scope_codes"] and contract["code"] not in fee_codes,
                "Fee inspection is outside or duplicated in the original fund source inventory")
        fee_codes.add(contract["code"])
    unknown = [copy.deepcopy(row) for row in dispositions if row["status"] == "unknown"]
    status = "empty" if not inventory else "unknown" if unknown else "supported"
    actions = [{"action": "complete_original_industry_source_contract", **row} for row in unknown]
    return {"status": status, "all_supported": not unknown, "unknown_industries": unknown,
            "required_actions": actions, "benchmark_contracts": copy.deepcopy(contracts),
            "industry_dispositions": copy.deepcopy(dispositions), "definitions": definitions,
            "source_captures": copy.deepcopy(submission["source_captures"]),
            "fee_inspections": copy.deepcopy(submission.get("fee_inspections", [])),
            "historical_qualification_granted": False}


def validate_binding(root, plan, chain_id, engine_context, reference, *, account_ref):
    """Independently rebuild source evidence and complete inventory at its seal."""
    store, artifacts = state(root, plan)
    value = artifacts.read_json(reference)
    fields(value, {"schema_id", "chain_id", "account_ref", "task_ref", "submission_ref", "contracts_ref", "sealed_at", "summary"},
           label="industry source binding")
    require(value["schema_id"] == BINDING_SCHEMA and value["chain_id"] == chain_id
            and value["account_ref"] == account_ref, "Industry binding belongs to another chain or first account")
    require(store.get(BINDING_KIND, chain_id) == {"binding_ref": reference}, "Industry binding lacks its original immutable source seal")
    task = _basis(root, plan, chain_id, engine_context, account_ref)
    _verify_task_journal(store, task, value["task_ref"])
    require(artifacts.read_json(value["task_ref"]) == task,
            "Industry binding changed its exact original task or operation inputs")
    declaration = store.operation(chain_id + ":industry-source-bind")
    require(declaration is not None and declaration["status"] == "completed"
            and declaration["request"] == {"schema_id": BINDING_SCHEMA, "chain_id": chain_id,
                "task_ref": value["task_ref"], "submission_ref": value["submission_ref"],
                "purpose": "engineering_source_binding_not_scientific_qualification"}
            and declaration["result"] == {"binding_ref": reference}
            and value["sealed_at"] == declaration["created_at"],
            "Industry source binding lacks its exact original atomic declaration and clock")
    require(instant(value["sealed_at"]) <= instant(utc_now()), "Industry source seal is dated in the future")
    submission = artifacts.read_json(value["submission_ref"])
    checked = _preflight(root, plan, chain_id, task, value["task_ref"], submission, value["sealed_at"])
    require(artifacts.read_json(value["contracts_ref"]) == checked["benchmark_contracts"]
            and value["summary"] == checked, "Industry binding changed original contracts, definitions or dispositions")
    return {**checked, "source_task_ref": value["task_ref"], "benchmark_contracts_ref": value["contracts_ref"],
            "binding_ref": reference}


def validate_task(root, plan, chain_id, engine_context, task_ref, *, account_ref):
    """Authenticate real pending host work; a literal pending flag is insufficient."""
    store, artifacts = state(root, plan)
    task = _basis(root, plan, chain_id, engine_context, account_ref)
    _verify_task_journal(store, task, task_ref)
    require(artifacts.read_json(task_ref) == task, "Pending industry task differs from its exact original inputs")
    require(store.get(BINDING_KIND, chain_id) is None, "Accepted industry source work cannot be relabeled pending")
    require(bool(task["industry_inventory"]), "No actual industry source work is pending")
    return {"status": "awaiting_analysis", "industry_source_status": "pending", "all_supported": False,
            "benchmark_contracts": [], "industry_inventory": task["industry_inventory"],
            "unknown_industries": copy.deepcopy(task["industry_inventory"]),
            "required_actions": [{"action": "complete_host_original_industry_source_submission", "task_ref": task_ref}],
            "historical_qualification_granted": False}


def prepare(root, plan, previous, *, source_submission=None):
    """Prepare internal host work or bind a source-checked submission, never fit."""
    store, artifacts = state(root, plan)
    task, task_ref, template_ref = _task(root, plan, previous)
    response = {"status": "awaiting_analysis", "source_task_ref": task_ref,
                "source_submission_template_ref": template_ref, "industry_source_binding_ref": None,
                "industry_source_status": "pending", "industry_source_required_actions": [
                    {"action": "complete_host_original_industry_source_submission", "task_ref": task_ref,
                     "submission_template_ref": template_ref}], "industry_inventory": task["industry_inventory"]}
    saved = store.get(BINDING_KIND, previous["chain_id"])
    if saved is not None:
        checked = validate_binding(root, plan, previous["chain_id"], context(previous), saved["binding_ref"],
                                   account_ref=previous["account_ref"])
        if source_submission is not None:
            bound = artifacts.read_json(saved["binding_ref"])
            require(artifacts.read_json(bound["submission_ref"]) == source_submission,
                    "An accepted industry source submission cannot change inside the same chain")
        return {**response, "status": "bound", "industry_source_binding_ref": saved["binding_ref"],
                "industry_source_status": checked["status"], "industry_source_required_actions": checked["required_actions"],
                "fee_inspections": checked["fee_inspections"]}
    if source_submission is None and task["industry_inventory"]:
        return response
    if source_submission is None:
        source_submission = {"source_task_ref": task_ref, "benchmark_contracts": [],
                             "industry_dispositions": [], "source_captures": []}
    at = utc_now()
    try:
        checked = _preflight(root, plan, previous["chain_id"], task, task_ref, source_submission, at)
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {**response, "industry_source_required_actions": [{"action": "correct_host_original_source_submission",
            "reason": str(error), "task_ref": task_ref, "submission_template_ref": template_ref}]}
    submission_ref = artifacts.put_json(source_submission)
    identity = previous["chain_id"] + ":industry-source-bind"
    saved = store.begin(identity, {"schema_id": BINDING_SCHEMA, "chain_id": previous["chain_id"],
        "task_ref": task_ref, "submission_ref": submission_ref,
        "purpose": "engineering_source_binding_not_scientific_qualification"})
    at = saved["created_at"]
    # All original source captures already preceded preflight. A resumed
    # declaration retains this actual original clock and identical submission.
    checked = _preflight(root, plan, previous["chain_id"], task, task_ref, source_submission, at)
    value = {"schema_id": BINDING_SCHEMA, "chain_id": previous["chain_id"], "account_ref": previous["account_ref"],
             "task_ref": task_ref, "submission_ref": submission_ref,
             "contracts_ref": artifacts.put_json(checked["benchmark_contracts"]), "sealed_at": at, "summary": checked}
    reference = artifacts.put_json(value)
    if saved["status"] != "completed":
        with store.lease(identity):
            store.complete(identity, {"binding_ref": reference}, writes=[
                {"kind": BINDING_KIND, "key": previous["chain_id"], "value": {"binding_ref": reference}, "immutable": True}])
    else:
        require(saved["result"] == {"binding_ref": reference}, "Completed industry source binding changed during resume")
    validate_binding(root, plan, previous["chain_id"], context(previous), reference, account_ref=previous["account_ref"])
    return {**response, "status": "bound", "industry_source_binding_ref": reference,
            "industry_source_status": checked["status"], "industry_source_required_actions": checked["required_actions"],
            "fee_inspections": checked["fee_inspections"]}
