"""Bound, sequential discovery across the complete news-derived TT scope.

The journal contains engineering progress only. Canonical public operation
receipts and independently checked originals remain authoritative. A stopped
or incomplete domain is never a successful fund-selection acceptance.
"""
import contextlib
import copy
import math
import os
from pathlib import Path

from contracts import fields, fingerprint, require, RetryableError
from file_io import read_artifact_json, record_file_stat, write_artifact_json
import fund_universe
import workflow_requests

UNIVERSE = {"market": "CN_public_funds", "platform": "TT", "currency": "CNY",
    "supported_instruments": ["off_exchange_nav"],
    "comparison_groups": ["same_legal_fund", "same_benchmark"], "require_complete_group": True}
MAX_SOURCE_PRODUCER_ATTEMPTS = 9  # original 3 stage attempts in each of 3 final rounds
MAX_DECLARED_PARAMETER = 10**12


@contextlib.contextmanager
def _lock(path):
    """Native exclusive lock is released automatically if the process exits."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "Discovery controller lock cannot be a symlink")
    with path.open("a+b") as stream:
        record_file_stat(path)
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RetryableError("This discovery workflow is already running; resume the same workflow") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _parameters(parameters):
    result = {} if parameters is None else copy.deepcopy(parameters)
    require(type(result) is dict, "Discovery acquisition parameters must be an object")
    fields(result, set(), {"max_total_source_requests", "max_total_bytes", "max_total_elapsed_seconds", "batch_policy"},
           "Discovery engineering budget parameters")
    for key, value in result.items():
        if key != "batch_policy":
            require(type(value) is int and 1 <= value <= MAX_DECLARED_PARAMETER,
                    "Positive bounded integer total discovery budget required: " + key)
    batch = fund_universe._policy(result.get("batch_policy", {}))
    require(all(batch[key] <= fund_universe.DEFAULT_POLICY[key] for key in batch),
            "Internal batch overrides cannot enlarge the original per-operation budget")
    result["batch_policy"] = batch
    return result


def _contract(prepared, review, parameters):
    store = prepared["store"]
    plan = store.get("plan_constraints", "main")
    require(plan is not None, "A confirmed current TT/CNY plan is required before discovery")
    import fund_screen
    fund_screen.validate_plan_constraints(plan)
    return {"schema_id": "whole_scope_discovery_controller_v1", "workflow_id": prepared["workflow_id"],
        "run_hash": fingerprint(prepared["run"]), "review_hash": fingerprint(review),
        "plan_hash": fingerprint(plan), "account_hash": fingerprint(store.get("account", "main")),
        "universe_policy": copy.deepcopy(UNIVERSE), "parameters": parameters,
        "target": "whole_current_news_derived_TT_scope_and_complete_original_comparison_groups",
        "trade_ready": False}


def _global_plan(parameters, bootstrap=None, artifacts=None):
    """Declare a directory-derived finite engineering plan, not a financial horizon."""
    batch = parameters["batch_policy"]
    if bootstrap is None:
        batches, scope_count, pending_links = 1, None, None
    else:
        scope_count = len(bootstrap["coverage"]["scope_codes"])
        snapshot = artifacts.read_json(bootstrap["identity_snapshot_ref"])
        pending_links = sum(row["document_ref"] is None for row in snapshot["automatic_issuer_discovery"])
        # One profile, one NAV precheck, menu/script/API, up to three original
        # announcement documents per subject. Literal extra links stay visible;
        # their currently known pending requests are included separately.
        profile_batches = math.ceil(len(bootstrap["coverage"]["unexamined_codes"]) / batch["max_profile_requests"])
        source_batches = math.ceil((8 * scope_count + pending_links) / batch["max_source_requests"])
        batches = 1 + profile_batches + source_batches
    source_units = batches * MAX_SOURCE_PRODUCER_ATTEMPTS
    limits = {"source_requests": source_units * batch["max_source_requests"],
        "bytes": source_units * batch["max_total_bytes"], "elapsed_seconds": source_units * batch["max_elapsed_seconds"]}
    for parameter, key in (("max_total_source_requests", "source_requests"), ("max_total_bytes", "bytes"),
                           ("max_total_elapsed_seconds", "elapsed_seconds")):
        if parameter in parameters:
            limits[key] = min(limits[key], parameters[parameter])
    return {"phase": "directory_derived_frozen" if bootstrap else "bootstrap_before_directory",
        "scope_subjects": scope_count, "known_pending_document_links": pending_links,
        "planned_batch_capacity": batches, "max_source_producer_attempts_per_operation": MAX_SOURCE_PRODUCER_ATTEMPTS,
        "limits": limits, "bootstrap_ref": bootstrap["candidate_ref"] if bootstrap else None,
        "basis": "complete_scope_size_plus_original_source_recipe_and_original_retry_ceiling_not_fixed_fund_subset",
        "trade_ready": False}


def _accepted(prepared, identity):
    """Completed public receipt, canonical candidate and source validator must agree."""
    store, artifacts = prepared["store"], prepared["artifacts"]
    operation = store.operation(identity)
    require(operation and operation["status"] == "completed" and operation["request"]["operation"] == "fund_discover",
            "Discovery batch is not a completed public operation")
    payload = operation["request"]["payload"]
    require(payload["news_review_id"] == prepared["workflow_id"] + ":assess"
            and payload["news_policy"] == prepared["run"]["news_policy"]
            and payload["universe_policy"] == UNIVERSE,
            "Discovery batch belongs to another current review or universe")
    value = store.get("fund_discovery", identity)
    require(value is not None and {key: row for key, row in operation["result"].items()
        if key != "automatic_validation"} == value, "Discovery receipt differs from its canonical committed record")
    require(value["candidate_ref"] == operation["result"]["candidate_ref"], "Discovery receipt candidate reference changed")
    import stage_validation
    bindings = artifacts.read_json(operation["result"]["automatic_validation"]["operation_bindings_ref"])
    stage_validation.validate_operation_result(operation["request"], operation["result"], store, artifacts, bindings)
    return value


def _charge(prepared, request, accepted=None):
    """Rebuild actual producer costs, conservatively charging incomplete attempts."""
    store, artifacts = prepared["store"], prepared["artifacts"]
    limits = request["payload"]["policy"]
    attempts = [row for _, row in store.scan("validation_attempt_start")
                if row.get("operation_id") == request["request_id"] and row.get("stage") == "discovery"]
    if not attempts:
        return {"source_requests": 0, "bytes": 0, "elapsed_seconds": 0, "producer_attempts": 0}
    result = {"source_requests": 0, "bytes": 0, "elapsed_seconds": 0, "producer_attempts": len(attempts)}
    for attempt in attempts:
        pointer = store.get("candidate-set", attempt["namespace"])
        candidate = artifacts.read_json(pointer["candidate_ref"]) if pointer else None
        if candidate is not None:
            coverage = candidate["coverage"]
            require(candidate["request"]["policy"] == limits, "Source producer budget differs from the frozen batch")
            result["source_requests"] += coverage["source_requests"]
            result["bytes"] += coverage["source_byte_budget_charged"]
            result["elapsed_seconds"] += coverage["elapsed_seconds"]
        else:
            result["source_requests"] += limits["max_source_requests"]
            result["bytes"] += limits["max_total_bytes"]
            result["elapsed_seconds"] += limits["max_elapsed_seconds"]
    return result


def _vector(value, artifacts):
    snapshot = artifacts.read_json(value["identity_snapshot_ref"])
    records = value["coverage"]["source_readiness"]["records"]
    return {"captured_profiles": len(snapshot["profiles"]) + len(snapshot["rejected_profiles"]),
        "verified_identities": len(snapshot["identities"]),
        "identity_ready": value["coverage"]["source_readiness"]["counts"]["source_identity_ready_subjects"],
        "NAV_available_subjects": sum((row.get("availability") or {}).get("status") == "source_observations_available"
                                     for row in records.values()),
        "NAV_original_observations": sum((row.get("availability") or {}).get("observation_count", 0)
                                         for row in records.values()),
        "identity_source_fields_resolved": sum(identity.get(field) is not None
            for identity in snapshot["identities"].values() for field in ("currency", "dealing_currency", "execution_venue",
                "share_class", "group_membership", "tracking_target", "manager_tenures", "sector_exposures", "company_restrictions")),
        "complete_groups": sum(group["coverage_status"].startswith("complete_") for group in value["groups"]),
        "missing_source_tasks": len(snapshot["required_actions"]),
        "unexamined_profiles": len(value["coverage"]["unexamined_codes"])}


def _advances(previous, current):
    return (any(current[key] > previous[key] for key in ("captured_profiles", "verified_identities", "identity_ready",
            "NAV_available_subjects", "complete_groups"))
        or any(current.get(key, 0) > previous.get(key, 0) for key in ("NAV_original_observations", "identity_source_fields_resolved"))
        or any(current[key] < previous[key] for key in ("missing_source_tasks", "unexamined_profiles")))


def _closed(value, artifacts):
    snapshot = artifacts.read_json(value["identity_snapshot_ref"])
    return (not value["coverage"]["unexamined_codes"] and not snapshot["required_actions"]
            and bool(value["groups"]) and all(group["coverage_status"].startswith("complete_") for group in value["groups"]))


def _receipt(prepared, journal):
    accepted = [row for row in journal["batches"] if row.get("accepted")]
    latest_id = accepted[-1]["request"]["request_id"] if accepted else None
    latest = _accepted(prepared, latest_id) if latest_id else None
    actions = []
    if latest:
        missing_profiles = len(latest["coverage"]["unexamined_codes"])
        pending_groups = sum(not group["coverage_status"].startswith("complete_") for group in latest["groups"])
        if missing_profiles:
            actions.append({"action": "complete_whole_current_related_profile_scope", "subject_count": missing_profiles,
                            "candidate_ref": latest["candidate_ref"]})
        if latest["required_actions"]:
            actions.append({"action": "resolve_all_original_source_tasks", "task_count": len(latest["required_actions"]),
                "candidate_ref": latest["candidate_ref"], "identity_snapshot_ref": latest["identity_snapshot_ref"]})
        if pending_groups:
            actions.append({"action": "complete_all_original_comparison_groups", "group_count": pending_groups,
                "candidate_ref": latest["candidate_ref"], "pending_scopes_ref": latest["pending_scopes_ref"]})
    return {"status": journal["status"], "workflow_id": prepared["workflow_id"],
        "discovery_id": latest_id,
        "candidate_ref": latest["candidate_ref"] if latest else None,
        "global_resource_plan": journal["global_plan"], "consumed": journal["consumed"],
        "completed_batches": sum(row.get("accepted", False) for row in journal["batches"]),
        "coverage": latest["coverage"]["source_readiness"]["counts"] if latest else None,
        "complete_original_group_scope": bool(latest and _closed(latest, prepared["artifacts"])),
        "required_actions": actions,
        "reason": journal.get("reason"), "trade_ready": False, "all_market_optimality_claimed": False,
        "scope": "discovery_acquisition_only_not_model_best_fund_or_adjustment_amount_acceptance"}


def discover(root, plan, *, workflow_id=None, parameters=None, preview=False, bindings=None):
    prepared = workflow_requests._prepared(root, plan, workflow_id, bindings=bindings)
    review = workflow_requests._completed_assessment(prepared)
    if review is None:
        return {"status": "discovery_not_ready", "required_actions": [{"action": "complete_current_prepared_news_assessment"}],
                "trade_ready": False}
    parameters = _parameters(parameters)
    contract = _contract(prepared, review, parameters)
    initial_plan = _global_plan(parameters)
    if preview:
        return {"status": "discovery_budget_preview", "contract": contract, "bootstrap_resource_plan": initial_plan,
            "remaining_plan": "frozen_once_from_actual_bootstrap_directory_scope_before_continuation",
            "trade_ready": False}
    directory = prepared["store"].base / "workflow-discovery"
    journal_path = directory / (fingerprint(prepared["workflow_id"]) + ".json")
    require(journal_path.resolve().is_relative_to(prepared["store"].base.resolve()), "Discovery progress escapes this plan")
    with _lock(journal_path.with_suffix(".lock")):
        if journal_path.exists():
            journal = read_artifact_json(journal_path)
            require(journal["contract"] == contract and journal["contract_hash"] == fingerprint(contract),
                    "Discovery workflow budget, review, account or plan changed; use a new prepared analysis")
        else:
            journal = {"contract": contract, "contract_hash": fingerprint(contract), "global_plan": initial_plan,
                "batches": [], "consumed": {"source_requests": 0, "bytes": 0, "elapsed_seconds": 0, "producer_attempts": 0},
                "status": "running", "reason": None}
            write_artifact_json(journal_path, journal)
        import pipeline
        latest = None
        rebuilt = {key: 0 for key in journal["consumed"]}
        for number, row in enumerate(journal["batches"], 1):
            expected_identity = prepared["workflow_id"] + ":discover:" + str(number).zfill(6)
            require(row["request"]["request_id"] == expected_identity and row["request"]["operation"] == "fund_discover"
                    and row["request"]["payload"].get("continue_from") == (latest["candidate_ref"] if latest else None),
                    "Discovery journal changed sequential identities or its original continuation chain")
            operation = prepared["store"].operation(row["request"]["request_id"])
            require(operation is None or operation["request"] == row["request"],
                    "Discovery journal request differs from the immutable public request")
            if operation and operation["status"] == "completed":
                latest = _accepted(prepared, row["request"]["request_id"])
                require(latest["request"]["continue_from"] == (row["request"]["payload"].get("continue_from")),
                        "Discovery continuation changed its canonical ancestor")
                row["accepted"] = True
            charge = _charge(prepared, row["request"], latest)
            row["charge"] = charge
            for key, amount in charge.items():
                rebuilt[key] += amount
        journal["consumed"] = rebuilt
        if journal["batches"] and journal["batches"][0].get("accepted"):
            bootstrap = _accepted(prepared, journal["batches"][0]["request"]["request_id"])
            derived = _global_plan(parameters, bootstrap, prepared["artifacts"])
            require(journal["global_plan"] in (initial_plan, derived), "Frozen global discovery resource plan changed")
            journal["global_plan"] = derived
        if journal["status"] != "running":
            write_artifact_json(journal_path, journal)
            return _receipt(prepared, journal)
        while True:
            current = workflow_requests._prepared(root, plan, prepared["workflow_id"], bindings=bindings)
            require(_contract(current, workflow_requests._completed_assessment(current), parameters) == contract,
                    "Current analysis or confirmed account changed during discovery")
            if latest and _closed(latest, prepared["artifacts"]):
                journal["status"] = "discovery_source_scope_complete"
                break
            pending = journal["batches"][-1] if journal["batches"] and not journal["batches"][-1].get("accepted") else None
            if pending is None:
                remaining = {key: journal["global_plan"]["limits"][key] - journal["consumed"][key]
                             for key in ("source_requests", "bytes", "elapsed_seconds")}
                policy = copy.deepcopy(parameters["batch_policy"])
                for limit, key in (("max_source_requests", "source_requests"), ("max_total_bytes", "bytes"),
                                   ("max_elapsed_seconds", "elapsed_seconds")):
                    policy[limit] = min(policy[limit], math.floor(remaining[key] / MAX_SOURCE_PRODUCER_ATTEMPTS))
                if min(policy.values()) <= 0 or policy["max_source_requests"] < 2:
                    journal["status"] = "discovery_coverage_incomplete_budget_exhausted"
                    journal["reason"] = "Frozen whole-scope acquisition budget cannot reserve the original retry ceiling for another batch"
                    break
                number = len(journal["batches"]) + 1
                identity = prepared["workflow_id"] + ":discover:" + str(number).zfill(6)
                payload = {"news_review_id": prepared["workflow_id"] + ":assess",
                    "news_policy": copy.deepcopy(prepared["run"]["news_policy"]), "universe_policy": copy.deepcopy(UNIVERSE),
                    "policy": policy}
                if latest:
                    payload["continue_from"] = latest["candidate_ref"]
                pending = {"request": {"schema_version": 4, "request_id": identity, "operation": "fund_discover", "payload": payload},
                    "accepted": False, "previous_vector": _vector(latest, prepared["artifacts"]) if latest else None}
                journal["batches"].append(pending)
                write_artifact_json(journal_path, journal)
            operation = prepared["store"].operation(pending["request"]["request_id"])
            if operation and operation["status"] in {"invalid", "needs_research"}:
                journal["status"] = "discovery_coverage_incomplete_batch_failed"
                journal["reason"] = {"request_id": pending["request"]["request_id"], "status": operation["status"], "result": operation["result"]}
                break
            try:
                result = pipeline.run(pending["request"], root, plan)
            except (ValueError, OSError, RuntimeError) as error:
                journal["reason"] = {"request_id": pending["request"]["request_id"], "type": type(error).__name__, "message": str(error)}
                write_artifact_json(journal_path, journal)
                raise
            operation = prepared["store"].operation(pending["request"]["request_id"])
            charge = _charge(prepared, pending["request"])
            previous_charge = pending.get("charge", {key: 0 for key in charge})
            for key, amount in charge.items():
                journal["consumed"][key] += amount - previous_charge[key]
            pending["charge"] = charge
            if not operation or operation["status"] != "completed":
                journal["status"] = "discovery_coverage_incomplete_batch_failed"
                journal["reason"] = {"request_id": pending["request"]["request_id"], "result": result}
                break
            latest = _accepted(prepared, pending["request"]["request_id"])
            pending["accepted"] = True
            current_vector = _vector(latest, prepared["artifacts"])
            pending["vector"] = current_vector
            if len(journal["batches"]) == 1:
                journal["global_plan"] = _global_plan(parameters, latest, prepared["artifacts"])
            elif not _advances(pending["previous_vector"], current_vector) and not _closed(latest, prepared["artifacts"]):
                journal["status"] = "discovery_coverage_incomplete_no_substantive_progress"
                journal["reason"] = {"previous": pending["previous_vector"], "current": current_vector,
                    "acceptance": "new source fields or reduced original evidence gaps required; fresh timestamps are not progress"}
                break
            write_artifact_json(journal_path, journal)
        write_artifact_json(journal_path, journal)
        return _receipt(prepared, journal)
