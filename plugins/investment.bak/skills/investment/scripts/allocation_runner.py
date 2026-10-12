"""One numerical decision path for derived live contexts and historical event replay."""
import copy
import datetime as dt
import hashlib
import math
from decimal import Decimal
from pathlib import Path
import allocation_market as market
import execution
import ledger
import strategy
from contracts import fields, fingerprint, instant, require
from state_store import LeaseLost

def source_hashes():
    from verify import code_identity
    return code_identity()


def prepare_samples(nav, features, code_info, horizon, availability_lag, decision_end_date, clock_context):
    require(type(horizon) is int and horizon > 0, "Positive horizon required")
    require(type(availability_lag) is int and availability_lag >= 0, "Frozen availability lag required")
    return market.build_single_step_samples(nav, features, code_info, horizon, availability_lag, decision_end_date, clock_context)


def prediction_rows(samples, codes, as_of, maximum_age):
    day = dt.date.fromisoformat(as_of)
    output = []
    for code in codes:
        available = [row for row in samples if row["code"] == code and row["decision_date"] <= as_of]
        require(available, "No causally available feature for "+code)
        latest = max(available, key=lambda row: row["decision_date"])
        require((day-dt.date.fromisoformat(latest["feature_cutoff_date"])).days <= maximum_age,
                "Feature is stale relative to the actual decision day")
        require(latest["decision_date"] == as_of,
                "Feature origin does not match the actual decision horizon; a new horizon-aligned feature is required")
        require(latest.get("industry_ready") is not False,
                "Current source-bound industry forecast/exposure is unavailable for " + code)
        output.append({"code": code, "decision_date": as_of, "x": latest["x"], "horizon_days": latest["horizon_days"],
                       "feature_cutoff_date": latest["feature_cutoff_date"]})
    return output


def fund_training_policy(context, samples):
    from industry_model import FUND_FEATURE_NAMES
    policy = copy.deepcopy(context["model_request"]["training_policy"])
    if any("industry_ready" in row for row in samples):
        policy["feature_names"] = market.FEATURE_NAMES + FUND_FEATURE_NAMES
    return policy


def fit_at(samples, codes, context, clock_context, training_ceiling_at=None):
    from allocation_statistics import fit_joint_targets
    request = context["model_request"]
    rows = prediction_rows(samples, codes, context["as_of"], request["max_feature_age_days"])
    return fit_joint_targets(samples, rows, fund_training_policy(context, samples), clock_context, training_ceiling_at)


def _rebase_targets(fitted, context, data):
    """Separate known pre-mark cash from five direct normalized outcome targets."""
    joint, audit = fitted["joint_scenarios"], []
    for forecast in fitted["forecasts"]:
        code, base_date = forecast["code"], forecast["feature_cutoff_date"]
        marked_date = context["market_ref"]["price_dates"][code]
        require(dt.date.fromisoformat(marked_date) <= dt.date.fromisoformat(context["as_of"]), "Future asset price date")
        require(base_date is not None and base_date <= marked_date, "Forecast base follows current valuation")
        rows = {row["date"]: row for row in data["nav"][code] if row["date"] <= marked_date}
        require(base_date in rows and marked_date in rows, "Forecast and market NAV bases must both be evidenced")
        require(Decimal(str(rows[marked_date]["nav"])) == Decimal(context["market_ref"]["prices"][code]),
                "Current valuation differs from the evidenced NAV base")
        require(not any(row.get("split_factor", 1) != 1 for row in rows.values()),
                "Split-adjusted forecast history requires an explicit share-factor model")
        base, marked = float(rows[base_date]["nav"]), float(rows[marked_date]["nav"])
        paid = sum(float(row["distribution_per_share"]) for day, row in rows.items() if base_date < day <= marked_date)
        column = joint["codes"].index(code)
        target_sets = [joint["targets"]]
        if "current_point_targets" in joint:
            target_sets.append(joint["current_point_targets"])
        for target_set in target_sets:
            for name in ("pricing", "terminal_nav", "hold", "sell"):
                for scenario in target_set[name]:
                    scenario[column] = (scenario[column]*base-(paid if name in ("hold", "sell") else 0))/marked
                    require(math.isfinite(scenario[column]) and scenario[column] > 0,
                            "Rebased direct target has nonpositive economic wealth")
        if "current_point_targets" in joint:
            forecast["predicted_log_targets"] = {name: math.log(rows_[0][column]) for name, rows_ in joint["current_point_targets"].items()}
        joint["returns"] = [[value-1 for value in row] for row in joint["targets"]["hold"]]
        forecast["expected_gross_return"] = sum(p*row[column] for p, row in zip(joint["probabilities"], joint["returns"]))
        audit.append({"code": code, "forecast_base_date": base_date, "market_price_date": marked_date,
                      "forecast_base_nav": base, "market_nav": marked, "known_cash_distribution_per_share": paid})
    joint["valuation_rebase"] = audit
    joint["return_basis"] = "direct_current_order_targets_rebased_to_current_mark_known_cash_separate"
    return fitted


class NumericStageFailure(ValueError):
    """Three actual producer attempts failed their independent checks."""


def _local_stage(name, inputs, producer, validator):
    for _ in range(3):
        try:
            output = producer()
            validator(output, inputs)
            return output
        except LeaseLost:
            raise
        except Exception as exc:
            failure = exc
    raise NumericStageFailure(name+": "+str(failure)) from failure


def predict(context, data, *, validate_steps=True, stage_runner=None, family_count=1, source_runtime=None):
    """No free-standing account or as_of input; controller owns persistence."""
    fields(data, {"nav", "features", "code_info"}, {"industry", "industry_exposures", "industry_data_ref", "quality_source_bundle"}, label="numerical data")
    require(context["schema_version"] == 4 and fingerprint({key: value for key, value in context.items()
                                                           if key != "context_hash"}) == context["context_hash"],
            "Decision context was changed")
    require(context["spec_hash"] == fingerprint(strategy.validate_spec(context["spec"])), "Strategy identity changed")
    empty = {"status": "blocked", "orders": [], "waiting": [], "funding_options": []}
    output = {"status": "blocked", "context_hash": context["context_hash"], "spec_hash": context["spec_hash"],
              "family_count": family_count,
              "as_of": context["as_of"], "input_hash": fingerprint(data), "orders": empty,
              "scope": context["spec"]["kind"], "live_execution_authorized": False}
    import news_validation
    output["predictive_increment"] = news_validation.unavailable("formal_source_forecast_not_ready")
    output["return_basis"] = "source_multi_date_nonanticipative_rolling_policy_current_action_only"
    if context["blocked"]:
        return {**output, "status": "awaiting_confirmation" if any(reason.startswith("pending_order:") for reason in context["reasons"]) else "blocked",
                "reason": ";".join(context["reasons"]), "required_actions": context.get("required_actions", [])}
    if not context["allocation_codes"]:
        return {**output, "status": "observation_only", "reason": "monitoring_without_strategy_exposure_or_eligible_purchase", "orders": {**empty, "status": "no_action"}}
    if data.get("industry") is None or context.get("industry_data_ref") is None:
        return {**output, "status": "partial", "trade_ready": False, "reason": "source_bound_industry_data_required",
                "required_actions": [{"action": "capture_verified_news_and_sector_benchmark_data",
                                      "reason": "source_bound_industry_data_required"}]}
    reference, industry_data = context["industry_data_ref"], data["industry"]
    require(reference["bundle_hash"] == fingerprint(industry_data)
            and reference["news_ref"] == fingerprint(industry_data["news_ref"])
            and reference["event_frontier_hash"] == industry_data["news_state"]["event_frontier_hash"],
            "Industry data binding differs from decision context")
    import industry_binding
    family_scope = industry_binding.family_industry_scope(data, context["allocation_codes"], context["decision_at"])
    industry_data = family_scope["industry"]
    output["family_source_scope"] = {key:value for key,value in family_scope.items() if key != "industry"}
    if family_scope["missing_exposure_codes"]:
        return {**output, "status": "partial", "trade_ready": False, "reason": "current_family_source_exposure_missing",
                "required_actions": [{"action": "complete_current_source_sector_exposure", "code": code}
                                     for code in family_scope["missing_exposure_codes"]]}
    if industry_data.get("required_actions"):
        return {**output, "status": "partial", "trade_ready": False, "reason": "industry_source_or_PIT_evidence_gap",
                "required_actions": copy.deepcopy(industry_data["required_actions"])}
    request = context["model_request"]
    require(set(data["nav"]) == set(context["universe"]), "Data universe differs from this decision")
    trace = []
    if validate_steps:
        import numeric_validation as checks
        sources = source_hashes()
        output["validation_trace"] = trace

    def stage(name, inputs, producer, validator):
        if not validate_steps:
            return producer()
        record = {"name": name, "input_hash": fingerprint(inputs), "source_hashes": sources,
                  "output_hash": None, "validation": None, "failed_attempts": []}
        trace.append(record)
        def produce():
            record["output_hash"] = None
            try:
                produced = producer()
                return produced
            except Exception as exc:
                record["failed_attempts"].append({"phase": "producer", "error": str(exc)})
                raise
        def validate(produced, actual_inputs):
            try:
                before = fingerprint(produced)
                record["output_hash"] = before
                validation = validator(produced, actual_inputs)
                require(validation["status"] in {"passed", "partial"}, "Independent stage validation did not pass")
                require(fingerprint(actual_inputs) == record["input_hash"], "Stage inputs mutated during validation")
                require(fingerprint(produced) == before, "Stage validator modified the result")
                record["validation"] = validation
                return validation
            except Exception as exc:
                record["failed_attempts"].append({"phase": "validator", "error": str(exc),
                                                  "output_hash": record["output_hash"]})
                raise
        return (stage_runner or _local_stage)(name, inputs, produce, validate)

    try:
        import single_step_wealth
        clock = stage("numeric_clock", {"context": context},
                      lambda: single_step_wealth.build_clock_context(context),
                      checks.validate_clock if validate_steps else None)
        selected_data = {key: {code: data[key][code] for code in context["allocation_codes"]}
                         for key in ("nav", "features", "code_info")}
        samples = stage("numeric_samples", {"context": context, "data": selected_data, "clock": clock},
                        lambda: prepare_samples(selected_data["nav"], selected_data["features"], selected_data["code_info"], request["horizon_days"],
                                                request["nav_availability_calendar_lag"], context["as_of"], clock),
                        checks.validate_samples if validate_steps else None)
        import industry_model
        industry_policy = copy.deepcopy(context["spec"].get("industry", {}).get("training", request["training_policy"]))
        industry_policy["feature_names"] = industry_model.FEATURE_NAMES
        origins = sorted({row["decision_date"] for row in samples})
        industry = stage("numeric_industry", {"context": context, "data": industry_data, "origins": origins,
                                              "policy": industry_policy, "clock": clock},
                         lambda: industry_model.build_forward(industry_data, origins, industry_policy,
                                                              request["horizon_days"], clock["order_time_local"]),
                         checks.validate_industry if validate_steps else None)
        output["industry_forecast"] = industry
        if not set(family_scope["required_current_sector_ids"]) <= set(industry["ready_sector_ids"]):
            return {**output, "status": "partial", "trade_ready": False, "reason": industry["reason"],
                    "required_actions": industry["required_actions"]}
        samples = stage("numeric_industry_bridge", {"context": context, "samples": samples, "industry": industry,
                                                   "exposures": data.get("industry_exposures", {}), "clock": clock},
                        lambda: market.attach_industry_features(samples, industry, data.get("industry_exposures", {}), clock["order_time_local"]),
                        checks.validate_industry_bridge if validate_steps else None)
        current_gaps = [row for row in samples if row["decision_date"] == context["as_of"] and not row["industry_ready"]]
        if current_gaps:
            return {**output, "status": "partial", "trade_ready": False, "reason": "source_bound_sector_exposure_required",
                    "required_actions": [{"action": "supply_PIT_sector_exposure", "code": row["code"], "reason": row["industry_reason"]}
                                         for row in current_gaps]}
        def fit_hierarchy():
            value = fit_at(samples, context["allocation_codes"], context, clock)
            if value["status"] != "research_ready":
                return {**value, "industry_forecast_hash": industry["forecast_hash"], "industry_data_hash": industry["dataset_hash"]}
            ceiling = value["joint_scenarios"]["source_calibration_oos"][0]["origin_at"]
            frozen = industry_model.freeze_current(industry, ceiling, family_scope["required_current_sector_ids"])
            current_primitives = [row for row in samples if row["decision_date"] == context["as_of"]]
            current_primitives = [{key: value for key, value in row.items() if not key.startswith("industry_")}
                                  for row in current_primitives]
            for row in current_primitives:
                row["x"] = row["x"][:len(market.FEATURE_NAMES)]
            current = market.attach_industry_features(current_primitives, frozen, data.get("industry_exposures", {}), clock["order_time_local"])
            frozen_samples = [row for row in samples if row["decision_date"] != context["as_of"]] + current
            value = fit_at(frozen_samples, context["allocation_codes"], context, clock, ceiling)
            return {**value, "industry_forecast": frozen, "current_prediction_samples": current,
                    "industry_forecast_hash": frozen["forecast_hash"], "industry_data_hash": frozen["dataset_hash"]}
        fitted = stage("numeric_fit", {"context": context, "data": data, "samples": samples, "clock": clock, "industry": industry},
                       fit_hierarchy,
                       checks.validate_fit if validate_steps else None)
        if "industry_forecast" in fitted:
            output["industry_forecast"] = fitted["industry_forecast"]
        if fitted["status"] != "research_ready":
            return {**output, "reason": fitted.get("reason", "forecast_unavailable"), "fitting": fitted}
        increment_inputs = {"context": context, "samples": samples, "fitting": fitted,
                            "policy": fund_training_policy(context, samples)}
        from paired_source_audit import evaluator as source_wealth_evaluator
        wealth_evaluator = source_wealth_evaluator(source_runtime, data)
        output["predictive_increment"] = stage("numeric_predictive_increment", increment_inputs,
            lambda: news_validation.predictive_increment(**increment_inputs, wealth_evaluator=wealth_evaluator),
            (lambda value, actual: news_validation.validate_predictive_increment(value, actual, wealth_evaluator=wealth_evaluator))
            if validate_steps else None)
        import portfolio_paths, portfolio_mpc
        path_inputs = {"context": context, "data": data, "samples": samples, "fitting": fitted,
                       "max_stages": context["spec"]["decision"]["max_path_stages"]}
        paths = stage("numeric_paths", path_inputs,
            lambda: portfolio_paths.build_paths(context, data, samples, fitted, max_stages=path_inputs["max_stages"]),
            portfolio_paths.validate_paths if validate_steps else None)
        output.update(paths=paths, fitting=fitted)
        if paths["status"] != "research_ready":
            return {**output, "status": "partial", "trade_ready": False, "reason": paths["reason"],
                    "required_actions": paths["required_actions"]}
        distribution = portfolio_mpc.as_current_order_distribution(context, paths)
        from allocation import build_source_actions
        comparison = stage("numeric_comparison", {"context": context, "fitting": {"joint_scenarios": distribution}},
                           lambda: build_source_actions(context),
                           checks.validate_comparison if validate_steps else None)
        mpc_inputs = {"context": context, "paths": paths, "comparison": comparison, "family_count": family_count}
        mpc = stage("numeric_mpc", mpc_inputs,
                    lambda: portfolio_mpc.optimise(context, paths, comparison, family_count=family_count),
                    portfolio_mpc.validate_result if validate_steps else None)
        output.update(status=mpc["status"], mpc=mpc, comparison=comparison,
                      trade_ready=mpc["status"] == "research_ready")
        if mpc["status"] != "research_ready":
            output.update(reason=mpc.get("reason", "source_policy_qualification_required"),
                          required_actions=copy.deepcopy(mpc.get("required_actions", [])))
        output["product_cost_comparison"] = single_step_wealth.product_cost_comparison(context)
        output["trade_calibration"] = mpc.get("calibration")
        output["execution_clocks"] = clock
        calculation = {key: value for key, value in output.items() if key != "validation_trace"}
        output["orders"] = stage("numeric_compile", {"context": context, "data": data, "calculation": calculation},
                                 lambda: execution.compile_mpc_orders(calculation, context, data),
                                 checks.validate_compile if validate_steps else None)
        if output["orders"]["status"] == "blocked":
            output["status"] = ("awaiting_intervention" if output["orders"].get("reason") == "sealed_external_decision_required" else "blocked")
            output["reason"] = output["orders"].get("reason", "orders_unavailable")
    except LeaseLost:
        raise
    except (ValueError, RuntimeError) as exc:
        if stage_runner is not None:
            from validation_runtime import StageValidationFailure
            if isinstance(exc, (NumericStageFailure, StageValidationFailure)):
                raise
        output.update(status="blocked", reason=str(exc), orders=empty)
    return output


def replay(spec, initial, steps, *, account_id="main"):
    """Replay confirmed facts before each decision using the same pure engines.

    Events must already be known by decision_at. Deterministic paper fill events
    come from execution.simulate_events; this runner never invents a fill.
    """
    spec = strategy.validate_spec(spec)
    require(type(steps) is list and 0 < len(steps) <= 1000, "Replay requires a bounded nonempty step list")
    state, events, decisions, observations = copy.deepcopy(initial), [], [], []
    financial = set()
    previous_at = initial["known_at"]
    previous_nav = ledger.snapshot(initial, initial["recorded_at"] or steps[0]["decision_at"])["unit_nav"]
    for step in steps:
        fields(step, {"decision_at", "market_ref", "data", "events", "risk_state", "purchase_eligible_codes"}, {"sealed_intervention", "news_review_hash"}, "replay step")
        at = step["decision_at"]
        require(previous_at is None or instant(at) > instant(previous_at), "Replay decision times must increase")
        for event in step["events"]:
            require(instant(event["known_at"]) <= instant(at) and instant(event["recorded_at"]) <= instant(at), "Replay received future facts")
            fact = ledger.financial_identity(event)
            require(fact is None or fact["key"] not in financial, "Duplicate financial fact in replay")
            if fact:
                financial.add(fact["key"])
            events.append(copy.deepcopy(event))
            try:
                state = ledger.apply_event(state, event)
            except ledger.RebuildRequired:
                state = ledger.rebuild(initial, events, at)
        import newtrade_guard
        trade_state = newtrade_guard.derive_state(initial, events, at, spec["trade_policy"]["economic"]["fee_window_days"], account_id=account_id)
        context = strategy.build_context(spec, state, at, step["market_ref"], account_id=account_id,
                                         risk_state=step["risk_state"],
                                         purchase_eligible_codes=step["purchase_eligible_codes"],
                                         sealed_intervention=step.get("sealed_intervention"), news_review_hash=step.get("news_review_hash"),
                                         trade_state=trade_state, trade_family_review_index=len(decisions)+1,
                                         industry_data_ref=({"bundle_hash": fingerprint(step["data"]["industry"]),
                                            "news_ref": fingerprint(step["data"]["industry"]["news_ref"]),
                                            "event_frontier_hash": step["data"]["industry"]["news_state"]["event_frontier_hash"]}
                                            if step["data"].get("industry") is not None else None))
        result = predict(context, step["data"])
        decisions.append({"decision_at": at, "context_hash": context["context_hash"], "calculation": result})
        for event in execution.reserve_events(state, result["orders"]["orders"], at):
            fact = ledger.financial_identity(event)
            require(fact["key"] not in financial, "Duplicate proposed order identity")
            financial.add(fact["key"])
            state = ledger.apply_event(state, event)
            events.append(event)
        observation = ledger.observe(state, at, step["market_ref"]["prices"], previous_nav or "1")
        observations.append({"decision_at": at, **observation})
        if observation["unit_nav"] is not None:
            previous_nav = observation["unit_nav"]
        previous_at = at
    return {"schema_version": 4, "status": "replayed", "spec_hash": fingerprint(spec), "state": state,
            "decisions": decisions, "observations": observations, "events": events,
            "scope": "conditional_event_replay_not_prospective_qualification"}
