"""Source/PIT qualification families before a decision context is frozen.

This selects computable research sets, never a financial ranking. Every returned
family retains the full held/pending risk universe and has real forward joint
errors. No missing candidate receives fabricated prices, labels or covariance.
"""
import copy
import datetime as dt
from zoneinfo import ZoneInfo

from contracts import fingerprint, instant, require
import allocation_market as market
import allocation_statistics as statistics
import industry_model


def risk_codes(state):
    """The caller supplies its verified ledger snapshot, including live orders."""
    require(isinstance(state, dict) and isinstance(state.get("positions"), list)
            and isinstance(state.get("open_orders"), list), "Verified holdings/order snapshot required")
    return sorted({row["code"] for row in state["positions"] + state["open_orders"]})


def _clock(codes, supplied, spec, at, fee_contracts):
    if fee_contracts is not None:
        import single_step_wealth
        return single_step_wealth.build_clock_context({"spec": spec, "decision_at": at,
            "allocation_codes": codes, "fee_contracts": fee_contracts})
    require(set(codes) == set(supplied["assets"]),
            "Family-specific verified fee contracts are required to reconstruct its execution calendar")
    return copy.deepcopy(supplied)


def _samples(codes, data, spec, day, clock):
    return market.build_single_step_samples(*[{code: data[key][code] for code in codes}
        for key in ("nav", "features", "code_info")], spec["planning"]["primary_horizon_days"],
        spec["availability"]["nav_lag_calendar_days"], day, clock)


def _rows(samples, codes, day, maximum_age):
    rows = []
    for code in codes:
        available = [row for row in samples if row["code"] == code and row["decision_date"] == day]
        require(len(available) == 1, "One current horizon-aligned source feature required for " + code)
        row = available[0]
        require(row["industry_ready"], "Current source-qualified industry exposure required for " + code)
        require((dt.date.fromisoformat(day)-dt.date.fromisoformat(row["feature_cutoff_date"])).days <= maximum_age,
                "Stale current fund feature for " + code)
        rows.append({key: row[key] for key in ("code", "decision_date", "x", "horizon_days", "feature_cutoff_date")})
    return rows


def _policy(spec):
    policy = copy.deepcopy(spec["training"])
    policy["feature_names"] = market.FEATURE_NAMES + industry_model.FUND_FEATURE_NAMES
    return policy


def _family_fit(codes, data, spec, at, clock, industry):
    day = instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    raw = _samples(codes, data, spec, day, clock)
    samples = market.attach_industry_features(raw, industry, data["industry_exposures"], clock["order_time_local"])
    rows = _rows(samples, codes, day, spec["availability"]["max_feature_age_days"])
    policy = _policy(spec)
    fitted = statistics.fit_joint_targets(samples, rows, policy, clock)
    if fitted["status"] != "research_ready":
        return fitted
    ceiling = fitted["joint_scenarios"]["source_calibration_oos"][0]["origin_at"]
    sectors = {item["sector_id"] for row in samples if row["decision_date"] == day
               for item in row["industry_source"]["exposures"]}
    frozen = industry_model.freeze_current(industry, ceiling, sectors)
    current = market.attach_industry_features([row for row in raw if row["decision_date"] == day],
        frozen, data["industry_exposures"], clock["order_time_local"])
    samples = [row for row in samples if row["decision_date"] != day] + current
    rows = _rows(samples, codes, day, spec["availability"]["max_feature_age_days"])
    fitted = statistics.fit_joint_targets(samples, rows, policy, clock, ceiling)
    return {**fitted, "industry_training_ceiling_at": ceiling, "required_sector_ids": sorted(sectors),
            "samples_hash": fingerprint(samples), "clock_hash": fingerprint(clock),
            "industry_forecast": frozen, "current_prediction_samples": current}


def _mature_dates(samples, code, day, window):
    lower = (dt.date.fromisoformat(day)-dt.timedelta(days=window)).isoformat()
    mature = [value for row in samples if row["code"] == code
              if (value := market.mature_label_at(row, day)) is not None]
    return {row["decision_date"] for row in mature if row["code"] == code
            and lower <= row["decision_date"] < day and row.get("industry_ready") is True
            and row.get("targets") is not None and row.get("label_available_date") is not None
            and row["label_available_date"] < day}


def assess_readiness(state, eligible_codes, data, spec, decision_at, clock_context, *, fee_contracts=None):
    """Finite, declared qualification budget; each admitted family is re-fitted."""
    held = risk_codes(state)
    require(isinstance(eligible_codes, list) and len(eligible_codes) == len(set(eligible_codes)),
            "Distinct source-eligible buy codes required")
    maximum = spec["decision"]["max_qualification_families"]
    require(type(maximum) is int and maximum >= 2, "Explicit qualification-family computation budget >= 2 required")
    day = instant(decision_at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    codes = sorted(set(held) | set(eligible_codes))
    per_code, primitives, dates = {}, [], {}
    for code in codes:
        try:
            require(all(code in data[key] for key in ("nav", "features", "code_info")),
                    "Source NAV/features/identity missing")
            clock = _clock([code], clock_context, spec, decision_at, fee_contracts)
            raw = _samples([code], data, spec, day, clock)
            primitives.extend(raw)
            per_code[code] = {"status": "source_samples_available", "risk_required": code in held,
                              "sample_hash": fingerprint(raw), "required_actions": []}
        except (ValueError, KeyError) as error:
            per_code[code] = {"status": "insufficient_evidence", "risk_required": code in held,
                "required_actions": [{"action": "complete_candidate_source_samples", "code": code, "reason": str(error)}]}
    industry_policy = copy.deepcopy(spec["industry"]["training"])
    industry = industry_model.build_forward(data["industry"], sorted({row["decision_date"] for row in primitives}),
        industry_policy, spec["planning"]["primary_horizon_days"], clock_context["order_time_local"])
    attached = market.attach_industry_features(primitives, industry, data["industry_exposures"], clock_context["order_time_local"])
    viable = []
    for code in codes:
        if per_code[code]["status"] == "insufficient_evidence":
            continue
        try:
            current = _rows(attached, [code], day, spec["availability"]["max_feature_age_days"])
            fit = statistics._fit_mean_at(attached, current, _policy(spec))
            require(fit["status"] == "fitted", fit.get("reason", "Insufficient chronological fund training"))
            dates[code] = _mature_dates(attached, code, day, spec["training"]["train_window_days"])
            per_code[code].update(status="individual_fit_ready", fit_hash=fingerprint(fit),
                                  mature_dates=sorted(dates[code]))
            viable.append(code)
        except (ValueError, KeyError) as error:
            per_code[code].update(status="insufficient_evidence", required_actions=[{
                "action": "complete_candidate_PIT_industry_and_fund_training", "code": code, "reason": str(error)}])
    new = sorted(set(viable)-set(held))
    baskets = {tuple(sorted(set(held) | set(new))): "full_source_qualified_basket"}
    if held:
        baskets[tuple(held)] = "held_risk_baseline"
    for origin in sorted(set().union(*(dates.get(code, set()) for code in viable))):
        if all(origin in dates.get(code, set()) for code in held):
            basket = tuple(sorted(set(held) | {code for code in new if origin in dates[code]}))
            if basket:
                baskets.setdefault(basket, "observed_joint_PIT_panel_basket")
    observed = [basket for basket, role in baskets.items() if role == "observed_joint_PIT_panel_basket"]
    for basket in observed:
        if any(set(basket) < set(other) for other in observed):
            baskets.pop(basket)
    for code in new:
        baskets.setdefault(tuple(sorted(set(held) | {code})), "single_new_candidate_with_all_held_risk")
    baskets.pop((), None)
    def coverage(basket):
        return len(set.intersection(*(dates.get(code, set()) for code in basket)))
    priorities = {"full_source_qualified_basket": 0, "held_risk_baseline": 1,
                  "observed_joint_PIT_panel_basket": 2, "single_new_candidate_with_all_held_risk": 3}
    ordered = sorted(baskets, key=lambda basket: (priorities[baskets[basket]], -coverage(basket),
                                                -len(basket), fingerprint(list(basket))))
    admitted, attempted = [], []
    for basket in ordered[:maximum]:
        if any(code not in viable for code in held):
            attempted.append({"codes": list(basket), "status": "insufficient_evidence", "reason": "held_risk_evidence_incomplete"})
            continue
        try:
            clock = _clock(list(basket), clock_context, spec, decision_at, fee_contracts)
            fit = _family_fit(list(basket), data, spec, decision_at, clock, industry)
            evidence = {"codes": list(basket), "status": fit["status"], "fit_hash": fingerprint(fit),
                        "reason": fit.get("reason"), "training_audit_hash": fingerprint(fit.get("training_audit", {}))}
            attempted.append(evidence)
            if fit["status"] != "research_ready":
                continue
            joint = fit["joint_scenarios"]
            admitted.append({"family_id": fingerprint([list(basket), evidence["fit_hash"]]),
                "allocation_codes": list(basket), "buy_codes": sorted(set(basket) & set(eligible_codes)),
                "required_risk_codes": held, "required_sector_ids": fit["required_sector_ids"],
                "qualification_role": baskets[basket], "fit_hash": evidence["fit_hash"],
                "training_audit_hash": evidence["training_audit_hash"], "clock_hash": fit["clock_hash"],
                "samples_hash": fit["samples_hash"], "selection_origins": [row["origin_at"] for row in joint["source_selection_oos"]],
                "calibration_origins": [row["origin_at"] for row in joint["source_calibration_oos"]]})
        except (ValueError, KeyError) as error:
            attempted.append({"codes": list(basket), "status": "insufficient_evidence", "reason": str(error)})
    qualified = sorted(set().union(*(set(row["buy_codes"]) for row in admitted))) if admitted else []
    for code in sorted(set(new)-set(qualified)):
        relevant = [row for row in attempted if code in row["codes"]]
        per_code[code]["individual_fit_ready"] = True
        per_code[code]["status"] = "joint_family_not_qualified" if relevant else "qualification_budget_not_assessed"
        per_code[code]["required_actions"] = [{"action": "complete_forward_joint_family_evidence" if relevant
                                              else "allocate_explicit_qualification_computation_budget",
            "code": code, "reasons": [row.get("reason") for row in relevant if row.get("reason")]}]
    actions = [action for code in codes for action in per_code[code]["required_actions"]]
    missing_held = sorted(code for code in held if per_code[code]["status"] != "individual_fit_ready")
    result = {"schema_version": 4, "kind": "candidate_readiness", "decision_at": decision_at,
        "input_hash": fingerprint({"state": state, "eligible_codes": eligible_codes, "data": data,
                                  "spec": spec, "decision_at": decision_at, "clock_context": clock_context,
                                  "fee_contracts": fee_contracts}),
        "status": "qualified_families" if admitted and not missing_held else "insufficient_evidence",
        "trade_ready": False, "required_risk_codes": held, "missing_held_codes": missing_held,
        "readiness_by_code": per_code, "sector_readiness": industry["sector_readiness"],
        "qualified_buy_codes": qualified, "excluded_new_codes": sorted(set(eligible_codes)-set(qualified)-set(held)),
        "candidate_families": admitted, "family_attempts": attempted, "required_actions": actions,
        "coverage": {"rule": "full_basket_then_maximal_observed_PIT_baskets_then_single_new_baskets",
            "financial_ranking": False, "declared_family_count": len(ordered), "attempted_family_count": len(attempted),
            "omitted_family_count": max(0, len(ordered)-maximum), "max_family_count": maximum,
            "declared_families_exhausted": len(ordered) <= maximum, "all_subsets_enumerated": False,
            "budget_reason": "explicit_computation_budget_not_a_financial_or_statistical_threshold",
            "source_candidate_codes": eligible_codes}}
    if not admitted:
        result["reason"] = "held_risk_evidence_incomplete" if missing_held else "no_source_qualified_joint_family"
        if not actions:
            actions.append({"action": "complete_source_eligible_fund_and_joint_calibration_evidence",
                            "reason": result["reason"]})
    result["qualification_hash"] = fingerprint(result)
    return result


def validate_readiness(value, inputs):
    expected = assess_readiness(inputs["state"], inputs["eligible_codes"], inputs["data"], inputs["spec"],
        inputs["decision_at"], inputs["clock_context"], fee_contracts=inputs.get("fee_contracts"))
    require(value == expected, "Candidate qualification differs from sealed source/PIT joint reconstruction")
    return {"status": "passed", "qualification_hash": value["qualification_hash"],
            "required_risk_codes": value["required_risk_codes"], "family_count": len(value["candidate_families"])}
