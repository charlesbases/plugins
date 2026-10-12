"""The scheduler owns checked IO, lineage, recovery and the investment goal."""
import copy
import re
import time
import uuid

from contracts import fingerprint, require, utc_now
from file_io import plan_path, read_artifact_json, write_artifact_json
from chains.contracts import STAGES, validate_chain
from chains.common import state
from chains import protocol


def display(result, *, output_bytes=16 * 1024):
    """Bound host stdout while retaining the complete checked CAS output."""
    import json
    encoded = lambda value: len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if encoded(result) <= output_bytes:
        return result
    value = copy.deepcopy(result)
    original = value.pop("output")
    value["output_summary"] = {key: original[key] for key in (
        "stage", "status", "processing_complete", "stage_complete", "investment_goal_achieved",
        "qualifications", "output_hash", "account_hash")}
    value["complete_output_ref"] = value["output_ref"]
    material = original["payload"].get("news_material", {})
    if original["stage"] == "news_collection":
        value["output_summary"]["scope_plan_ref"] = material.get("scope_plan_ref")
        value["output_summary"]["scope_request_templates"] = material.get("scope_request_templates")
    if original["stage"] == "fund_sources":
        source = original["payload"].get("source_bundle", {})
        value["output_summary"].update({key: source.get(key) for key in (
            "source_task_ref", "source_submission_template_ref", "industry_source_binding_ref",
            "industry_source_status")})
        value["output_summary"]["industry_source_required_action_count"] = len(
            source.get("industry_source_required_actions", []))
    report = original["payload"].get("report")
    if report:
        value["output_summary"]["conclusion"] = report["conclusion"]
        value["output_summary"]["report_refs"] = original["payload"].get("engine_context", {}).get("daily_review")
    value["display_scope"] = "bounded_display_complete_business_output_and_scope_retained_in_immutable_CAS"
    boundary = value.get("failed_boundary")
    if encoded(value) > output_bytes and boundary is not None:
        actions = boundary.pop("required_actions", [])
        boundary["required_action_count"] = len(actions)
        boundary["complete_required_actions_path"] = value["journal_path"]
    require(encoded(value) <= output_bytes, "Scheduler metadata exceeds the display budget; read its sealed output")
    return value


def handlers():
    from chains.account import read_account
    from chains.news_collection import collect
    from chains.news_assessment import assess
    from chains.fund_universe import discover
    from chains.fund_sources import prepare
    from chains.model import calculate
    from chains.recommendation import recommend
    from chains.reporting import report
    return dict(zip(STAGES, (read_account, collect, assess, discover, prepare,
                            calculate, recommend, report)))


def _read(root, plan, identity):
    path = plan_path(root, plan) / "business-chains" / (fingerprint(identity) + ".json")
    current = protocol.identities()
    if not path.exists():
        return path, {"schema_id": "investment_business_chain_journal_v2", "chain_id": identity,
            "plan": plan, "requested_goal": "investment_recommendation", "created_at": utc_now(),
            "stages": [], "attempts": [], "failed_boundary": None, **current}
    journal = read_artifact_json(path)
    require(journal.get("schema_id") == "investment_business_chain_journal_v2"
            and journal.get("chain_id") == identity and journal.get("plan") == plan
            and journal.get("requested_goal") == "investment_recommendation",
            "Business chain journal identity changed")
    require(all(journal.get(key) == value for key, value in current.items()),
            "This interrupted chain belongs to another protocol or implementation")
    return path, journal


def _load(journal, artifacts, root, plan, *, receipt_verifier=None):
    if receipt_verifier is None:
        from chains.gates import verify_stage_receipt
        receipt_verifier = verify_stage_receipt
    values = [artifacts.read_json(row["output_ref"]) for row in journal["stages"]]
    if values:
        validate_chain(values, allow_partial=True)
    for index, (row, value) in enumerate(zip(journal["stages"], values)):
        require(row["stage"] == value["stage"] and row["output_hash"] == fingerprint(value),
                "Journal substituted a sealed stage output")
        require(value["lineage_refs"] == [item["output_ref"] for item in journal["stages"][:index]],
                "Journal and actual upstream lineage differ")
        proof = artifacts.read_json(row["validation_ref"])
        require(proof.get("binding") == {key: value[key] for key in (
            "chain_id", "stage", "account_ref", "account_hash", "input_output_hash", "output_hash",
            "protocol_sha256", "implementation_identity")},
            "Independent stage proof belongs to another input, account, chain or implementation")
        control = protocol.control(root, plan, journal["chain_id"],
            previous_output_ref=journal["stages"][index-1]["output_ref"] if index else None,
            delivery_mode="observation" if any(not item["stage_complete"] for item in values[:index])
                else "qualified_recommendation")
        # Pending first facts are intentionally re-read after confirmation.
        # The obsolete account is discarded; downstream cannot use it.
        if not (len(values) == 1 and index == 0 and value["status"] == "awaiting_user"):
            receipt_verifier(root, plan, values[index-1] if index else None, value,
                             row["validation_ref"], control_context=control)
    return values


def execute(root, plan, *, chain_id=None, analysis=None, scope_audit=None, source_evidence=None,
            through=None, adapters=None, verifier=None,
            receipt_verifier=None):
    """Normal CLI uses real adapters/gates; injection is for auxiliary IO tests."""
    identity = chain_id or "chain-" + uuid.uuid4().hex
    require(type(identity) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", identity), "Invalid chain identity")
    require(through is None or through in STAGES, "Unknown target business stage")
    path, journal = _read(root, plan, identity)
    _, artifacts = state(root, plan)
    values = _load(journal, artifacts, root, plan, receipt_verifier=receipt_verifier)
    routes = handlers() if adapters is None else adapters
    if verifier is None:
        from chains.gates import validate_stage
        verifier = validate_stage
    require(set(routes) == set(STAGES), "All eight adapters must be registered")
    if values and values[-1]["status"] in {"awaiting_user", "awaiting_analysis", "failed"}:
        pending = values.pop()
        journal["stages"].pop()
        if (journal["failed_boundary"] or {}).get("stage") == pending["stage"]:
            journal["failed_boundary"] = None
    observation = any(not value["stage_complete"] for value in values)
    while len(values) < len(STAGES):
        stage = STAGES[len(values)]
        if through is not None and STAGES.index(stage) > STAGES.index(through):
            break
        previous = copy.deepcopy(values[-1]) if values else None
        previous_ref = journal["stages"][-1]["output_ref"] if values else None
        control = protocol.control(root, plan, identity, previous_output_ref=previous_ref,
            delivery_mode="observation" if observation else "qualified_recommendation")
        started, begun = utc_now(), time.monotonic()
        phase = "input_validation"
        journal["inflight"] = {"stage": stage, "started_at": started, "input_ref": previous_ref}
        write_artifact_json(path, journal)
        try:
            if values:
                from chains.account import assert_current_account
                assert_current_account(root, plan, values[0])
            phase = "execution"
            options = {"analysis": analysis} if stage == "news_assessment" else {}
            if stage == "news_collection":
                options["scope_submission"] = scope_audit
            if stage == "fund_sources":
                options["source_submission"] = source_evidence
            produced = (routes[stage](root, plan, identity, control_context=control) if stage == "account"
                        else routes[stage](root, plan, previous, control_context=control, **options))
            phase = "output_validation"
            validate_chain(values + [produced], allow_partial=observation)
            phase = "independent_qualification"
            checked = verifier(root, plan, previous, produced, control_context=control)
            value, validation_ref = checked["output"], checked["validation_ref"]
            validate_chain(values + [value], allow_partial=observation)
            if values:
                assert_current_account(root, plan, values[0])
            phase = "publication"
            output_ref = artifacts.put_json(value)
            row = {"stage": stage, "input_ref": previous_ref, "output_ref": output_ref,
                "output_hash": fingerprint(value), "validation_ref": validation_ref,
                "started_at": started, "ended_at": utc_now(), "elapsed_seconds": time.monotonic()-begun}
            journal["attempts"].append(copy.deepcopy(row))
            journal["stages"].append(row)
            journal["updated_at"] = row["ended_at"]
            if not value["stage_complete"] and journal["failed_boundary"] is None:
                actions = checked["validation"].get("required_actions", [])
                first_gap = actions[0] if actions else {}
                journal["failed_boundary"] = {"stage": stage, "phase": "independent_qualification",
                    "rule_path": first_gap.get("rule_path", stage + ".business_postcondition"),
                    "input_ref": previous_ref,
                    "reason": first_gap.get("reason", "The required business postcondition has not been independently met"),
                    "attempt": sum(item.get("stage") == stage for item in journal["attempts"]),
                    "retryable": False, "required_actions": actions}
            journal.pop("inflight", None)
            write_artifact_json(path, journal)
        except Exception as exc:
            failure = {"stage": stage, "phase": phase, "rule_path": getattr(exc, "rule_path", phase),
                "input_ref": previous_ref, "reason": str(exc),
                "attempt": 1 + sum(item.get("stage") == stage for item in journal["attempts"]),
                "retryable": getattr(exc, "code", None) == "retryable",
                "required_actions": getattr(exc, "required_actions", []), "error_type": type(exc).__name__}
            journal["failed_boundary"] = failure
            journal["attempts"].append({**journal["inflight"], "ended_at": utc_now(),
                "elapsed_seconds": time.monotonic()-begun, "status": "failed", "failure": failure})
            write_artifact_json(path, journal)
            raise
        values.append(value)
        observation |= not value["stage_complete"]
        if value["status"] in {"awaiting_user", "awaiting_analysis", "failed"}:
            break
        if not value["stage_complete"] and (stage == "account" or not value["processing_complete"]):
            break
    require(values, "No stage was produced")
    summary = validate_chain(values, allow_partial=True)
    last = values[-1]
    goal = bool(len(values) == len(STAGES) and all(value["stage_complete"] for value in values)
                and summary["investment_goal_achieved"])
    status = ("goal_achieved" if goal else last["status"]
              if last["status"] in {"awaiting_user", "awaiting_analysis", "failed"} else "goal_unmet")
    return {"requested_goal": "investment_recommendation", "chain_id": identity, "status": status,
        "delivery_mode": "qualified_recommendation" if goal else "pending"
            if status in {"awaiting_user", "awaiting_analysis", "failed"} else "observation",
        "current_stage": last["stage"], "chain_outputs": copy.deepcopy(journal["stages"]),
        "last_good_output_ref": next((row["output_ref"] for row, value in
            reversed(list(zip(journal["stages"], values))) if value["stage_complete"]), None),
        "failed_boundary": journal["failed_boundary"],
        "goal_evidence": {"investment_goal_achieved": goal,
            "business_stages_completed": [value["stage"] for value in values if value["stage_complete"]],
            "validation_refs": [row["validation_ref"] for row in journal["stages"]]},
        "output": last, "output_ref": journal["stages"][-1]["output_ref"], "journal_path": str(path),
        "next_stage": STAGES[len(values)] if len(values) < len(STAGES) and last["processing_complete"]
            and last["status"] not in {"awaiting_user", "awaiting_analysis"} else last["stage"],
        "investment_goal_achieved": goal, "trade_ready": False}
