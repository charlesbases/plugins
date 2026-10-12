"""Bind host analysis to the actual prepared workflow and immutable policy.

Draft checks are read-only previews, not source assessments. Public submissions
still run the original controller, validators and persistent attempt budgets.
"""
import copy
import datetime as dt
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from artifacts import Artifacts
from contracts import fields, fingerprint, instant, require, utc_now
from file_io import plan_path, read_object
from state_store import Store


def _prepared(root, plan, workflow_id=None, *, bindings=None):
    require((plan_path(root, plan) / "active.sqlite").is_file(), "Prepare this plan before submitting its analysis")
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    if bindings is not None:
        require(type(bindings) is dict, "Business-chain engine bindings must be an object")
        require(workflow_id is not None and bindings.get("workflow_id") == workflow_id,
                "Explicit prepared bindings must name this workflow")
        from chains.common import bound_result
        bound_result(root, plan, bindings.get("analysis_run"), "analysis_start")
        bound_result(root, plan, bindings.get("collection"), "news_collect")
    if workflow_id is None:
        candidates = [run for key, run in store.scan("analysis_run")
                      if key.endswith(":analysis") and run.get("account_id") == "main"]
        require(candidates, "No prepared analysis exists in this plan")
        latest = max(candidates, key=lambda run: (instant(run["started_at"]), run["run_id"]))
        workflow_id = latest["run_id"][:-len(":analysis")]
    require(type(workflow_id) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", workflow_id),
            "Invalid prepared workflow identity")
    opened = store.operation(workflow_id + ":analysis")
    collected = store.operation(workflow_id + ":collect")
    require(opened is not None and opened["status"] == "completed"
            and opened["request"]["operation"] == "analysis_start", "Prepared analysis has not completed")
    require(collected is not None and collected["status"] == "completed"
            and collected["request"]["operation"] == "news_collect", "The latest prepared collection has not completed")
    run = store.get("analysis_run", workflow_id + ":analysis")
    require(run is not None and opened["result"]["run"] == run, "Prepared analysis differs from its immutable receipt")
    collection = collected["result"]
    manifest = artifacts.read_json(collection["manifest_ref"])
    journal = store.get("news-operation", fingerprint({"operation_id": manifest["collection_id"]}))
    require(journal is not None and journal["manifest_ref"] == collection["manifest_ref"]
            and manifest["run_id"] == run["run_id"] and manifest["cutoff_at"] == run["publish_cutoff"],
            "Prepared original collection belongs to another run")
    import news
    bound_run, _ = news.collection_policy(manifest, store, artifacts)
    require(bound_run == run, "Prepared collection changed its authoritative source policy")
    import stage_validation
    # This verifies original acceptance and bytes without creating any operation
    # or spending a producer/validator attempt on an analyst's draft.
    stage_validation.validate_operation_result(opened["request"], opened["result"], store, artifacts)
    stage_validation.validate_operation_result(collected["request"], collected["result"], store, artifacts)
    prepared = {"workflow_id": workflow_id, "store": store, "artifacts": artifacts,
                "run": run, "collection": manifest}
    if bindings is not None:
        require(bindings["analysis_run"]["operation_id"] == workflow_id + ":analysis"
                and bindings["collection"]["operation_id"] == workflow_id + ":collect",
                "Explicit prepared operations belong to another workflow")
        prepared["bindings"] = copy.deepcopy(bindings)
    return prepared


def _assessment_request(prepared, analysis=None):
    body = {"claims": [], "events": [], "industry_theses": [], "economic_observations": []}
    if analysis is not None:
        fields(analysis, {"claims", "events", "industry_theses"}, {"economic_observations"}, "host analysis body")
        body.update(copy.deepcopy(analysis))
    return {"schema_version": 4, "request_id": prepared["workflow_id"] + ":assess",
            "operation": "news_assess", "payload": {"run_id": prepared["run"]["run_id"],
                "collection_id": prepared["collection"]["collection_id"], **body}}


def _completed_assessment(prepared):
    store, artifacts = prepared["store"], prepared["artifacts"]
    identity = prepared["workflow_id"] + ":assess"
    if "bindings" in prepared:
        binding = prepared["bindings"].get("news_review")
        if binding is None:
            return None
        require(binding["operation_id"] == identity, "Explicit assessment belongs to another workflow")
        from chains.common import bound_result
        bound_result(store.root, store.plan, binding, "news_assess")
    operation = store.operation(identity)
    review = store.get("news-review", identity)
    ready = (operation is not None and operation["status"] == "completed" and review is not None
             and review.get("status") == "assessed" and bool(review.get("claims")))
    if ready:
        require(operation["request"]["operation"] == "news_assess"
                and review["run_id"] == prepared["run"]["run_id"]
                and review["collection_id"] == prepared["collection"]["collection_id"],
                "Prepared assessment belongs to another collection")
        import stage_validation
        bindings = artifacts.read_json(operation["result"]["automatic_validation"]["operation_bindings_ref"])
        stage_validation.validate_operation_result(operation["request"], operation["result"], store, artifacts, bindings)
        return {**artifacts.read_json(review["manifest_ref"]), "manifest_ref": review["manifest_ref"]}
    return None


def _completed_discovery(prepared, review=None):
    """Select a same-review public receipt without narrowing its original scope."""
    review = review or _completed_assessment(prepared)
    if review is None:
        return None
    store, artifacts = prepared["store"], prepared["artifacts"]
    if "bindings" in prepared:
        binding = prepared["bindings"].get("discovery")
        if binding is None:
            return None
        from chains.common import bound_result
        result = bound_result(store.root, store.plan, binding, "fund_discover")
        key = binding["operation_id"]
        discovery, operation = store.get("fund_discovery", key), store.operation(key)
        require(discovery is not None and discovery.get("news_review_hash") == fingerprint(review)
                and operation["request"]["payload"]["news_review_id"] == review["review_id"],
                "Explicit discovery belongs to another assessment")
        require({name: item for name, item in result.items() if name != "automatic_validation"} == discovery,
                "Explicit discovery differs from its canonical committed record")
        return key, discovery, operation
    candidates = []
    for key, value in store.scan("fund_discovery"):
        if value.get("news_review_hash") != fingerprint(review):
            continue
        operation = store.operation(key)
        if operation is not None and operation["status"] == "completed" and operation["request"]["operation"] == "fund_discover":
            candidates.append((key, value, operation))
    if not candidates:
        return None
    key, discovery, operation = max(candidates, key=lambda row: (instant(row[1]["observed_at"]), row[0]))
    require(operation["request"]["payload"]["news_review_id"] == review["review_id"],
            "Latest matching discovery belongs to another assessment")
    import stage_validation
    bindings = artifacts.read_json(operation["result"]["automatic_validation"]["operation_bindings_ref"])
    stage_validation.validate_operation_result(operation["request"], operation["result"], store, artifacts, bindings)
    return key, discovery, operation


def _observation_request(prepared, *, assessment_template=False, force_raw=False):
    review = None if force_raw else _completed_assessment(prepared)
    identity = prepared["workflow_id"] + ":assess"
    payload = {"analysis_run_id": prepared["run"]["run_id"],
               "news_policy": copy.deepcopy(prepared["run"]["news_policy"])}
    if review is not None or assessment_template:
        payload["news_review_id"] = identity
        # This is the plugin's fixed market/instrument comparison contract;
        # no illustrative statistical model or user preference is confirmed.
        reference = Path(__file__).resolve().parent.parent / "references/strategy.example.json"
        payload["universe_policy"] = read_object(reference)["universe_policy"]
        if review is not None:
            completed = _completed_discovery(prepared, review)
            if completed is not None:
                payload["discovery_id"] = completed[0]
    else:
        payload["news_collection_id"] = prepared["collection"]["collection_id"]
    suffix = "observation" if review is not None or assessment_template else "raw-observation"
    return {"schema_version": 4, "request_id": prepared["workflow_id"] + ":" + suffix,
            "operation": "daily_review", "payload": payload}


def request_templates(root, plan, *, workflow_id=None):
    prepared = _prepared(root, plan, workflow_id)
    research = _research_request(prepared)
    return {"status": "review_requests_prepared", "workflow_id": prepared["workflow_id"],
            "news_assess": _assessment_request(prepared),
            "daily_observation": _observation_request(prepared, assessment_template=True),
            "raw_observation": _observation_request(prepared, force_raw=True),
            "research": research, "market": _market_request(prepared), "trade_ready": False,
            "scope": "bound_internal_templates_not_completed_assessment_or_model_qualification"}


def _research_parameters(prepared, parameters=None):
    """Range-only preview; these settings do not establish model optimality."""
    end = instant(prepared["run"]["started_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date()-dt.timedelta(days=1)
    # An open lower date requests every actual source observation. It is not
    # a claimed fund birth date or an invented old information timestamp.
    # The model window is later derived from actual verified source records.
    body = {"start": dt.date.min.isoformat(), "end": end.isoformat(),
            "lookback": 120, "horizons": [30], "timeout": 20}
    parameter_source = "all_available_original_history_query_not_a_claim_of_historical_availability"
    # An already frozen context for this same review keeps its evaluation H;
    # the helper never invents or registers a model to obtain one.
    contexts = [] if "bindings" in prepared else [row for _, row in prepared["store"].scan("decision")
                if row.get("analysis_run_id") == prepared["run"]["run_id"] and row.get("context")]
    if contexts:
        latest = max(contexts, key=lambda row: instant(row["decision_at"]))
        body["horizons"] = [latest["context"]["spec"]["planning"]["primary_horizon_days"]]
        parameter_source = "current_frozen_context_horizon_and_engineering_acquisition_defaults"
    issues = []
    if parameters is not None:
        if not isinstance(parameters, dict):
            issues.append({"field": "parameters", "reason": "object_required"})
        else:
            for key in sorted(set(parameters)-set(body)):
                issues.append({"field": key, "reason": "only_acquisition_parameters_no_discovery_codes_or_bindings"})
            body.update({key: copy.deepcopy(value) for key, value in parameters.items() if key in body})
    dates = {}
    for key in ("start", "end"):
        try:
            value = body[key]
            date = dt.date.fromisoformat(value) if type(value) is str else None
            if date is None or date.isoformat() != value:
                raise ValueError("Canonical ISO date required")
            dates[key] = date
        except (ValueError, TypeError):
            issues.append({"field": key, "provided": body[key], "reason": "canonical_ISO_date_required"})
    if len(dates) == 2:
        if dates["start"] > dates["end"]:
            issues.append({"field": "start/end", "reason": "start_must_not_follow_end"})
        today = instant(utc_now()).astimezone(ZoneInfo("Asia/Shanghai")).date()
        if dates["end"] > today:
            issues.append({"field": "end", "reason": "future_date_range_not_allowed", "latest_allowed": today.isoformat()})
    if type(body["lookback"]) is not int or body["lookback"] < 120:
        issues.append({"field": "lookback", "provided": body["lookback"], "reason": "integer_at_least_120_NAV_intervals", "minimum": 120})
    horizons = body["horizons"]
    if (type(horizons) is not list or not horizons or any(type(value) is not int or not 1 <= value <= 730 for value in horizons)
            or len(horizons) != len(set(horizons))):
        issues.append({"field": "horizons", "provided": horizons, "reason": "nonempty_unique_integer_calendar_day_horizons_1_to_730"})
    timeout = body["timeout"]
    if type(timeout) not in (int, float) or not 1 <= timeout <= 30 or not math.isfinite(timeout):
        issues.append({"field": "timeout", "provided": timeout, "reason": "finite_source_timeout_1_to_30_seconds"})
    return {"status": "invalid_parameters" if issues else "passed", "parameters": body, "issues": issues,
            "parameter_source": parameter_source, "scope": "acquisition_ranges_only_not_model_or_source_qualification",
            "meaning": "lookback counts NAV intervals; horizons are engineering label windows, not proven optimal horizons or user holding periods"}


def _research_request(prepared, parameters=None):
    checked = _research_parameters(prepared, parameters)
    if checked["status"] != "passed":
        return {"status": "research_preflight_failed", "diagnostics": checked, "trade_ready": False,
                "scope": "parameters_not_submitted_no_formal_attempt_consumed"}
    review = _completed_assessment(prepared)
    if review is None:
        return {"status": "research_not_ready", "diagnostics": checked, "trade_ready": False,
                "required_actions": [{"action": "complete_current_prepared_news_assessment"}]}
    store, artifacts = prepared["store"], prepared["artifacts"]
    completed = _completed_discovery(prepared, review)
    if completed is None:
        return {"status": "research_not_ready", "diagnostics": checked, "trade_ready": False,
                "required_actions": [{"action": "complete_current_assessment_fund_discovery"}]}
    key, discovery, operation = completed
    import news
    import fund_universe
    import verify
    current_at = utc_now()
    news_spec = {"kind": "numeric_policy", "news": prepared["run"]["news_policy"]}
    state = news.validate_review(review, news_spec, current_at, artifacts, store=store)
    account = store.get("account", "main")
    fund_universe.validate_discovery(discovery, review, verify.monitor_codes(account), artifacts, current_at,
        monitoring_codes=operation["request"]["payload"].get("monitoring_codes", []), news_state=state,
        plan_constraints=store.get("plan_constraints", "main"))
    import research_scope
    source_scope = research_scope.derive(discovery, account, artifacts, as_of=current_at)
    research_scope.validate(source_scope, discovery, artifacts)
    if source_scope["blocking_held_codes"]:
        return {"status": "needs_source", "diagnostics": checked, "trade_ready": False,
                "discovery_id": key, "source_scope": source_scope, "current_risk_unknown": True,
                "required_actions": source_scope["required_actions"]}
    if not source_scope["selected_codes"]:
        return {"status": "research_not_ready", "diagnostics": checked, "trade_ready": False,
                "discovery_id": key, "source_scope": source_scope, "required_actions": discovery["required_actions"] or [
                    {"action": "complete_current_discovery_identity_or_observe_without_candidates"}]}
    request = {"schema_version": 4, "request_id": prepared["workflow_id"] + ":research",
               "operation": "research_prepare", "payload": {"discovery_id": key, "source_scope": research_scope.MODE, **checked["parameters"]}}
    return {"status": "research_request_prepared", "request": request, "diagnostics": checked,
            "source_scope": source_scope,
            "trade_ready": False, "scope": "current_source_task_not_PIT_model_or_trade_qualification"}


def research(root, plan, *, workflow_id=None, parameters=None, preview=False):
    prepared = _prepared(root, plan, workflow_id)
    built = _research_request(prepared, parameters)
    if built["status"] != "research_request_prepared" or preview:
        return built
    import pipeline
    return pipeline.run(built["request"], root, plan)


def _market_request(prepared):
    """Bind acquisition settings and inspected contracts from public receipts."""
    completed = _completed_discovery(prepared)
    if completed is None:
        return {"status": "market_not_ready", "trade_ready": False,
                "required_actions": [{"action": "complete_current_assessment_fund_discovery"}]}
    discovery_id, discovery, _ = completed
    store, artifacts = prepared["store"], prepared["artifacts"]
    research_operation = store.operation(prepared["workflow_id"] + ":research")
    if (research_operation is None or research_operation["status"] != "completed"
            or research_operation["request"]["operation"] != "research_prepare"
            or research_operation["request"]["payload"]["discovery_id"] != discovery_id):
        return {"status": "market_not_ready", "discovery_id": discovery_id, "trade_ready": False,
                "required_actions": [{"action": "complete_current_source_bound_research_preparation"}]}
    import stage_validation
    bindings = artifacts.read_json(research_operation["result"]["automatic_validation"]["operation_bindings_ref"])
    stage_validation.validate_operation_result(research_operation["request"], research_operation["result"], store, artifacts, bindings)
    research = {key: copy.deepcopy(value) for key, value in research_operation["request"]["payload"].items()
                if key not in {"discovery_id", "source_scope"}}
    codes = set(discovery["codes"])
    scope = research_operation["result"].get("source_scope")
    if not codes or (scope is not None and not codes <= set(scope["selected_codes"])):
        return {"status": "needs_source", "discovery_id": discovery_id, "trade_ready": False,
                "source_scope": scope, "required_actions": discovery["required_actions"] or [
                    {"action": "complete_original_financial_candidate_identity_source"}]}
    # Only actual accepted fee inspections supply contracts; a missing contract
    # stays a gap in the original market controller rather than a guessed rule.
    contracts = {}
    for key, result in store.scan("operation_result"):
        if not isinstance(result, dict) or not result.get("contract_ref"):
            continue
        operation = store.operation(key)
        if operation is None or operation["status"] != "completed" or operation["request"]["operation"] != "fee_inspect":
            continue
        contract = artifacts.read_json(result["contract_ref"])
        code = contract.get("code")
        if code not in codes:
            continue
        bindings = artifacts.read_json(result["automatic_validation"]["operation_bindings_ref"])
        stage_validation.validate_operation_result(operation["request"], result, store, artifacts, bindings)
        previous = contracts.get(code)
        if previous is None or (instant(operation["created_at"]), key) > previous[:2]:
            contracts[code] = (instant(operation["created_at"]), key, copy.deepcopy(result["contract_ref"]))
    request = {"schema_version": 4, "request_id": prepared["workflow_id"] + ":market", "operation": "market_prepare",
               "payload": {"discovery_id": discovery_id, "research": research,
                   "market_terms": {"currency": "CNY", "contracts": [contracts[code][2] for code in sorted(contracts)]}}}
    return {"status": "market_request_prepared", "request": request, "trade_ready": False,
            "required_actions": [{"code": code, "action": "capture_and_inspect_current_product_dealing_terms"}
                                 for code in sorted(codes-set(contracts))],
            "scope": "current_public_source_receipts_not_model_or_trade_qualification"}


def market(root, plan, *, workflow_id=None, preview=False):
    prepared = _prepared(root, plan, workflow_id)
    built = _market_request(prepared)
    if built["status"] != "market_request_prepared" or preview:
        return built
    import pipeline
    return pipeline.run(built["request"], root, plan)


def assess(root, plan, analysis, *, workflow_id=None, preview=False):
    prepared = _prepared(root, plan, workflow_id)
    request = _assessment_request(prepared, analysis)
    import news
    diagnostic = news.assessment_structure_preflight(request["payload"], prepared["collection"])
    if diagnostic["status"] != "passed":
        return {"status": "analysis_preflight_failed", "workflow_id": prepared["workflow_id"],
                "diagnostics": diagnostic, "trade_ready": False,
                "scope": "draft_not_submitted_no_formal_attempt_consumed",
                "required_actions": [{"action": "correct_analysis_draft_references", "diagnostics": diagnostic}]}
    if preview:
        return {"status": "analysis_request_prepared", "request": request, "diagnostics": diagnostic,
                "trade_ready": False, "scope": "structural_preview_not_source_assessment"}
    import pipeline
    return pipeline.run(request, root, plan)


def _automatic_report_method(prepared, *, preview=False, market_operation_id=None):
    """Select current source receipts, never an illustrative or past model."""
    store, artifacts = prepared["store"], prepared["artifacts"]
    review = _completed_assessment(prepared)
    if review is None:
        return {"status": "automatic_strategy_needs_source", "trade_ready": False,
                "required_actions": [{"action": "complete_current_prepared_news_assessment"}]}
    if market_operation_id is None:
        candidates = []
        for key, index in store.scan("market-operation"):
            discovery = store.get("fund_discovery", index["discovery_id"])
            if discovery is not None and discovery.get("news_review_hash") == fingerprint(review):
                candidates.append((index["created_at"], key))
        if not candidates:
            return {"status": "automatic_strategy_needs_source", "trade_ready": False,
                    "required_actions": [{"action": "complete_current_source_bound_market_preparation",
                        "reason": "No completed current-market source receipt; do not ask the user for model parameters"}]}
        market_operation_id = max(candidates, key=lambda row: (instant(row[0]), row[1]))[1]
    operation = store.operation(market_operation_id)
    require(operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "market_prepare",
            "Automatic model requires the latest completed public market preparation")
    if preview:
        import automatic_strategy_policy
        return automatic_strategy_policy.freeze_automatic_strategy(prepared, market_operation_id, preview=True)
    import pipeline
    return pipeline.run({"schema_version": 4, "request_id": prepared["workflow_id"] + ":method",
        "operation": "method_prepare", "payload": {"workflow_id": prepared["workflow_id"],
        "market_operation_id": market_operation_id}}, store.root, store.plan)


def report(root, plan, *, workflow_id=None, preview=False, inputs=None):
    prepared = _prepared(root, plan, workflow_id)
    request = _observation_request(prepared)
    if inputs is not None:
        fields(inputs, set(), {"spec", "research", "market_terms", "discovery_id", "discovery_policy",
            "issuer_disclosures", "universe_policy", "continue_from", "industry_data_id", "benchmark_contracts",
            "quality_contracts", "quality_policy", "exposure_contracts", "paired_currency_validation", "market_operation_id"},
            "host model and source inputs")
        supplied = copy.deepcopy(inputs)
        market_operation_id = supplied.pop("market_operation_id", None)
        if "spec" in supplied:
            require(type(supplied["spec"]) is dict, "Host model spec must be an object")
            policy = prepared["run"]["news_policy"]
            require("news" not in supplied["spec"] or supplied["spec"]["news"] == policy,
                    "Explicit model news policy differs from immutable analysis run")
            supplied["spec"]["news"] = copy.deepcopy(policy)
        request["payload"].update(supplied)
    else:
        market_operation_id = None
    automatic_method = None
    if "spec" not in request["payload"]:
        automatic_method = _automatic_report_method(prepared, preview=preview,
            market_operation_id=market_operation_id)
        if not preview and automatic_method.get("market_operation_id"):
            method_operation = prepared["store"].operation(prepared["workflow_id"] + ":method")
            if method_operation is not None and method_operation["status"] == "completed":
                request["payload"]["method_operation_id"] = prepared["workflow_id"] + ":method"
        if automatic_method["status"] == "automatic_strategy_policy_prepared":
            market_operation = prepared["store"].operation(automatic_method["market_operation_id"])
            request["request_id"] = prepared["workflow_id"] + ":numeric-review"
            request["payload"].update(spec=copy.deepcopy(automatic_method["spec"]),
                discovery_id=automatic_method["discovery_id"],
                research=copy.deepcopy(market_operation["request"]["payload"]["research"]),
                market_terms=copy.deepcopy(market_operation["request"]["payload"]["market_terms"]))
    diagnostic = None
    spec = request["payload"].get("spec")
    if spec is not None:
        from workflow_strategy import diagnose_numeric_policy
        try:
            diagnostic = diagnose_numeric_policy(spec, request["payload"].get("research"))
        except ValueError as error:
            diagnostic = {"status": "method_fields_not_complete", "reason": str(error), "trade_ready": False,
                          "scope": "necessary_window_fields_only_not_formal_strategy_validation"}
    if diagnostic is not None and diagnostic["status"] == "structurally_insufficient":
        return {"status": "strategy_preflight_failed", "request": request, "trade_ready": False,
                "diagnostics": diagnostic,
                "scope": "impossible_method_window_not_submitted_no_formal_attempt_consumed"}
    if preview:
        result = {"status": "observation_request_prepared", "request": request, "trade_ready": False,
                  "scope": "bound_observation_preview_not_a_completed_report"}
        if diagnostic is not None:
            result["method_diagnostics"] = diagnostic
        if automatic_method is not None:
            result["automatic_method"] = automatic_method
        return result
    import pipeline
    result = pipeline.run(request, root, plan)
    if automatic_method is not None:
        result = {**result, "automatic_method_diagnostics": automatic_method}
    return result
