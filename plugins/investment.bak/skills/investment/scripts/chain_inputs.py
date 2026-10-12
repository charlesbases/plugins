"""Complete current-chain internal JSON transport; staging grants no qualification."""
import re
from pathlib import Path

from artifacts import Artifacts
from contracts import fields, fingerprint, require, strict_json_loads, utc_now
from file_io import path_present, plan_path, read_object, record_file_stat, write_small_json
from state_store import Store

SCHEMA = "investment_internal_chain_input_ref_v1"
CONTAINER_FIELDS = {"schema_id", "purpose", "data_root", "plan", "chain_id", "account_ref", "binding_ref", "input_ref"}
PURPOSES = {"scope_audit": ("news_collection", "news_material", "scope_plan_ref"),
            "source_evidence": ("fund_sources", "source_bundle", "source_task_ref")}


def _context(root, plan, chain_id, purpose):
    require(purpose in PURPOSES, "Only current-chain scope audit or source evidence can use internal staging")
    require(type(chain_id) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", chain_id),
            "Internal input requires this exact current business chain id")
    path = plan_path(root, plan) / "business-chains" / (fingerprint(chain_id) + ".json")
    require(path.is_file(), "Internal input requires its actual pending business chain")
    from chains.controller import _read, _load
    _, journal = _read(root, plan, chain_id)
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    values = _load(journal, artifacts, root, plan)
    stage, key, binding_field = PURPOSES[purpose]
    require(values and values[-1]["stage"] == stage and values[-1]["status"] == "awaiting_analysis",
            "Internal input belongs to a different or already completed business boundary")
    last = values[-1]
    reference = last["payload"][key].get(binding_field)
    require(reference is not None, "The current pending boundary lacks its original source binding")
    bound = artifacts.read_json(reference)
    require(bound["chain_id"] == chain_id and bound["account_ref"] == last["account_ref"],
            "Internal source binding belongs to another chain or first account")
    return store, artifacts, last, reference, bound


def _scope_preflight(root, plan, chain_id, submission, bound_ref, scope_plan, last, store, artifacts):
    from chains import news_scope
    fields(submission, {"scope_plan_ref", "analysis", "query_reviews"}, label="complete internal scope audit")
    require(submission["scope_plan_ref"] == bound_ref, "Internal audit changed its exact original scope plan")
    analysis = submission["analysis"]
    fields(analysis, {"claims", "events", "industry_theses"}, {"economic_observations"}, "internal source analysis")
    questions = {row["question_id"]: row for row in scope_plan["questions"]}
    rows = submission["query_reviews"]
    require(type(rows) is list, "Internal audit must retain all original question reviews")
    expected_fields = {"question_id", "support_claim_ids", "counter_claim_ids", "unresolved_material_claim_ids",
        "disposition", "disposition_claim_id", "covered_question_id", "source_census_binding", "reason"}
    seen = set()
    for row in rows:
        fields(row, expected_fields, label="complete original query review")
        identity = row["question_id"]
        require(type(identity) is str and identity in questions and identity not in seen,
                "Internal audit duplicated, inserted or substituted an original question")
        seen.add(identity)
    require(seen == set(questions), "Internal audit omitted the complete original question inventory")
    engine = last["payload"]["engine_context"]
    _, collected = news_scope._native(store, artifacts, engine["collection"]["operation_id"],
        "news_collect", engine["collection"]["result_ref"])
    manifest = artifacts.read_json(collected["manifest_ref"])
    require(manifest["run_id"] == scope_plan["run_id"], "Internal audit collection belongs to another source window")
    payload = {"run_id": manifest["run_id"], "collection_id": manifest["collection_id"], **analysis}
    claims = news_scope._preflight_assessment(payload, manifest, store, artifacts)
    if not claims:
        for row in rows:
            require(row["disposition"] == "unknown" and type(row["reason"]) is str
                    and all(row[name] == [] for name in ("support_claim_ids", "counter_claim_ids", "unresolved_material_claim_ids"))
                    and all(row[name] is None for name in ("disposition_claim_id", "covered_question_id", "source_census_binding")),
                    "An unanalysed template cannot assert source evidence or disposition qualification")
        return "pending_analysis"
    mappings = news_scope._query_mappings(rows, questions, claims)
    for row in mappings.values():
        census = row["source_census_binding"]
        if census is not None:
            _, value = news_scope._native(store, artifacts, census["operation_id"], "news_collect", census["result_ref"])
            require(value["run_id"] == scope_plan["run_id"], "Internal source census changed its original window")
    return "source_draft_preflight_passed_not_qualification"


def _source_preflight(root, plan, chain_id, submission, bound_ref, task, last, store, artifacts):
    from chains import industry_sources
    fields(submission, industry_sources.SUBMISSION_FIELDS, {"fee_inspections"}, "complete internal source evidence")
    require(submission["source_task_ref"] == bound_ref, "Internal source evidence changed its original source task")
    industry_sources._verify_task_journal(store, task, bound_ref)
    require(industry_sources._basis(root, plan, chain_id, last["payload"]["engine_context"], last["account_ref"]) == task,
            "Internal source task differs from this exact current industry and discovery")
    inventory = {row["thesis_id"]: row for row in task["industry_inventory"]}
    rows = submission["industry_dispositions"]
    require(type(rows) is list, "Internal source evidence needs the complete industry inventory")
    seen = set()
    for row in rows:
        fields(row, {"thesis_id", "sector_id", "status", "reason"}, label="internal industry disposition")
        identity = row["thesis_id"]
        require(type(identity) is str and identity in inventory and identity not in seen
                and row["sector_id"] == inventory[identity]["sector_id"],
                "Internal industry evidence duplicated, omitted or changed its original source subject")
        require(type(row["status"]) is str and row["status"] in {"supported", "unknown"} and type(row["reason"]) is str,
                "Internal industry evidence requires its actual supported or unknown disposition")
        seen.add(identity)
    require(seen == set(inventory), "Internal source evidence omitted the complete industry inventory")
    for key in ("benchmark_contracts", "source_captures", "fee_inspections"):
        require(type(submission.get(key, [])) is list, "Internal source arrays must be explicit")
    if (not submission["benchmark_contracts"] and not submission["source_captures"]
            and not submission.get("fee_inspections") and all(row["status"] == "unknown" for row in rows)
            and any(not row["reason"].strip() for row in rows)):
        return "pending_evidence"
    industry_sources._preflight(root, plan, chain_id, task, bound_ref, submission, utc_now())
    return "source_draft_preflight_passed_not_qualification"


def preflight(root, plan, chain_id, purpose, submission):
    store, artifacts, last, reference, bound = _context(root, plan, chain_id, purpose)
    status = (_scope_preflight(root, plan, chain_id, submission, reference, bound, last, store, artifacts)
              if purpose == "scope_audit" else
              _source_preflight(root, plan, chain_id, submission, reference, bound, last, store, artifacts))
    return {"status": status, "binding_ref": reference, "account_ref": last["account_ref"], "qualified": False}, artifacts


def load(path, root, plan, chain_id, purpose):
    """Keep generic small JSON limits; only exact current internal CAS is unwrapped."""
    value = read_object(path)
    if value.get("schema_id") != SCHEMA:
        require("schema_id" not in value, "Unknown internal input reference schema")
        preflight(root, plan, chain_id, purpose, value)
        return value
    fields(value, CONTAINER_FIELDS, label="internal chain input reference")
    _, artifacts, last, reference, _ = _context(root, plan, chain_id, purpose)
    require(value["purpose"] == purpose and value["plan"] == plan and value["chain_id"] == chain_id
            and value["data_root"] == str(Path(root).resolve()) and value["account_ref"] == last["account_ref"]
            and value["binding_ref"] == reference,
            "Internal input reference changed its exact root, plan, chain, account, purpose or source binding")
    from chains.contracts import validate_ref
    validate_ref(value["input_ref"], "complete internal input_ref")
    require(value["input_ref"].get("media_type") == "application/json", "Internal input CAS must retain its complete JSON media type")
    submission = artifacts.read_json(value["input_ref"])
    require(type(submission) is dict, "Referenced internal submission must be a complete JSON object")
    preflight(root, plan, chain_id, purpose, submission)
    return submission


def stage(path, output, root, plan, chain_id, purpose):
    """Preflight before immutable bytes; no business ids, budgets or money mutate."""
    record_file_stat(path)
    require(Path(output).resolve() != Path(path).resolve(), "Internal staging cannot replace its original input")
    require(not Path(output).resolve().is_relative_to(Path(root).resolve()),
            "Internal input reference files belong in the working directory, outside the financial data root")
    if path_present(output):
        record_file_stat(output)
    with Path(path).open("rb") as stream:
        raw = stream.read()
    value = strict_json_loads(raw.decode("utf-8"))
    require(type(value) is dict, "Large internal submission must be a complete JSON object")
    checked, artifacts = preflight(root, plan, chain_id, purpose, value)
    container = {"schema_id": SCHEMA, "purpose": purpose, "data_root": str(Path(root).resolve()),
        "plan": plan, "chain_id": chain_id, "account_ref": checked["account_ref"],
        "binding_ref": checked["binding_ref"], "input_ref": artifacts.put_json(value)}
    write_small_json(output, container)
    return {"status": "internal_input_staged", "purpose": purpose, "chain_id": chain_id,
        "input_bytes": len(raw), "input_ref": container["input_ref"], "reference_path": str(Path(output).resolve()),
        "preflight_status": checked["status"], "qualified": False, "investment_goal_achieved": False, "trade_ready": False}
