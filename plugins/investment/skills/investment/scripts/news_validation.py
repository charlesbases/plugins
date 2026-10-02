"""Paired forward EN covariate diagnostics, never shadow investment orders."""
import collections
import copy
import datetime as dt
import math

from contracts import fingerprint, require, instant

SCOPE = "source_news_asset_covariate_block_paired_source_OOS_not_isolated_news_causality"
CURRENCY_SCOPE = "same_family_account_fees_clock_cashflows_after_fee_currency_wealth_risk_turnover"


def unavailable(reason):
    return {"status": "insufficient_evidence", "scope": SCOPE, "reason": reason,
            "baseline": "native_NAV_without_industry_news", "shadow_orders_created": False,
            "used_for_order_selection": False, "paired_rows": [],
            "currency_increment": currency_unavailable(reason),
            "required_actions": [{"action": "complete_original_mature_paired_NAV_and_industry_source_history", "reason": reason}]}


def _cohort(samples, codes, origin, policy, horizon):
    from allocation_market import mature_label_at
    lower = (dt.date.fromisoformat(origin)-dt.timedelta(days=policy["train_window_days"])).isoformat()
    values = []
    for original in samples:
        if (original.get("code") not in codes or original.get("horizon_days") != horizon or not lower <= original["decision_date"] < origin
                or original.get("industry_ready") is False):
            continue
        row = mature_label_at(original, origin)
        if row is not None and row.get("targets") is not None and row.get("label_available_date", origin) < origin:
            values.append(row)
    return sorted(values, key=lambda row: (row["decision_date"], row["code"]))


def _strict(row):
    return (row.get("feature_source", {}).get("strict_PIT_verified") is True
            and row.get("label_source", {}).get("strict_PIT_verified") is True
            and row.get("industry_source") is not None)


def predictive_increment(context, samples, fitting, policy, wealth_evaluator=None):
    """Assess the full covariate block on its original embargoed OOS panel."""
    from allocation_market import FEATURE_NAMES, LATENT_NAMES, mature_label_at
    from allocation_statistics import _fit_mean_at
    if wealth_evaluator is None:
        from news_wealth import evaluate_pair
        wealth_evaluator = evaluate_pair
    distribution = fitting.get("joint_scenarios") or {}
    records = distribution.get("source_oos") or []
    selected, calibration = distribution.get("source_selection_oos") or [], distribution.get("source_calibration_oos") or []
    if fitting.get("status") != "research_ready" or not records or not selected or len(calibration) < 2:
        return unavailable("original_embargoed_forward_EN_panel_unavailable")
    names = policy["feature_names"]
    if names[:len(FEATURE_NAMES)] != FEATURE_NAMES or len(names) <= len(FEATURE_NAMES):
        return unavailable("original_industry_and_news_covariate_block_unavailable")
    require(selected == records[:len(records)//2] and calibration == [row for row in records[len(records)//2:]
            if row["date"] > max(item["label_available"] for item in selected)], "Predictive increment changed the original OOS split")
    codes = distribution["codes"]
    require(codes == context["allocation_codes"], "Predictive increment changed the financial candidate family")
    table = {(row["decision_date"], row["code"]): row for row in samples}
    require(len(table) == len(samples), "Duplicate predictive increment source sample")
    native_samples = [{**copy.deepcopy(row), "x": row["x"][:len(FEATURE_NAMES)]} for row in samples]
    native_policy = {**copy.deepcopy(policy), "feature_names": list(FEATURE_NAMES)}
    paired, wealth_pairs, wealth_gaps = [], [], []
    for record in calibration:
        origin = record["date"]
        require(record["codes"] == codes, "Predictive increment OOS candidate pairing changed")
        horizon = record["model"]["horizon_days"]
        cohort = _cohort(samples, codes, origin, policy, horizon)
        source_rows = [mature_label_at(table[(origin, code)], context["as_of"]) if (origin, code) in table else None for code in codes]
        if (not cohort or any(row is None or not _strict(row) for row in source_rows)
                or any(not _strict(row) for row in cohort)
                or any(source.get("strict_PIT_verified") is not True for source in record["source_labels"].values())):
            return unavailable("original_source_vintages_or_forward_industry_history_unavailable")
        for fold in record["training_audit"]["folds"]:
            if any(not _strict(row) for row in _cohort(samples, codes, fold["validation_start"], policy, horizon)):
                return unavailable("original_inner_CV_source_vintages_unavailable")
        require(all(source_rows[index]["label_source"]["source_hash"] == record["source_hashes"][code]
                    for index, code in enumerate(codes)), "Predictive increment changed the original evaluation source version")
        if any(instant(source["label_available_at"]) >= instant(context["decision_at"])
               for source in record["source_labels"].values()):
            return unavailable("paired_outcomes_are_not_yet_observed")
        prediction = [{"code": code, "decision_date": origin, "horizon_days": source_rows[index]["horizon_days"],
                       "x": source_rows[index]["x"][:len(FEATURE_NAMES)]} for index, code in enumerate(codes)]
        shadow = _fit_mean_at(native_samples, prediction, native_policy)
        if shadow["status"] != "fitted":
            return unavailable("native_shadow_EN_"+shadow.get("reason", "fit_unavailable"))
        audit, original_audit = shadow["training_audit"], record["training_audit"]
        require(all(audit[key] == original_audit[key] for key in ("training_rows", "training_dates", "folds")),
                "Predictive increment changed the original mature cohort or CV dates")
        counts = collections.Counter(row["fund_group_id"] for row in source_rows)
        group_count = len(counts)
        differences, full_losses, native_losses = [], [], []
        for column, code in enumerate(codes):
            weights = 1/(group_count*counts[source_rows[column]["fund_group_id"]])
            truth = [record["realized_latents"][name][column] for name in LATENT_NAMES]
            full = [record["predicted_latents"][name][column] for name in LATENT_NAMES]
            native = shadow["forecasts"][column]["predicted_coordinates"]
            require(shadow["forecasts"][column]["code"] == code, "Native OOS code order changed")
            full_loss = [(value-estimate)**2 for value, estimate in zip(truth, full)]
            native_loss = [(value-estimate)**2 for value, estimate in zip(truth, native)]
            full_losses.append(weights*math.fsum(full_loss)/len(LATENT_NAMES))
            native_losses.append(weights*math.fsum(native_loss)/len(LATENT_NAMES))
            differences.append({"code": code, "weight": weights, "full_prediction": full, "native_prediction": native,
                                "realized": truth, "full_squared_errors": full_loss, "native_squared_errors": native_loss})
        if wealth_evaluator is not None:
            full_values = {code: {name: record["predicted_latents"][name][column] for name in LATENT_NAMES}
                           for column, code in enumerate(codes)}
            native_values = {code: dict(zip(LATENT_NAMES, shadow["forecasts"][column]["predicted_coordinates"]))
                             for column, code in enumerate(codes)}
            replay = wealth_evaluator(context=context, record=record, full_prediction=full_values,
                                      native_prediction=native_values, source_rows=source_rows)
            if replay.get("status") in ("source_replay_ready", "conditional_currency_replay_ready"):
                wealth_pairs.append(currency_pair(replay, record, codes, full_values, native_values))
            else:
                wealth_gaps.append({"date": origin, "reason": replay.get("reason", "historical_source_wealth_frame_unavailable")})
        paired.append({"date": origin, "origin_at": record["origin_at"], "label_available": record["label_available"],
            "full_loss": math.fsum(full_losses), "native_loss": math.fsum(native_losses),
            "loss_difference_native_minus_full": math.fsum(native_losses)-math.fsum(full_losses),
            "per_code": differences, "source_hashes": record["source_hashes"], "training_rows": audit["training_rows"],
            "training_dates": audit["training_dates"], "folds": audit["folds"],
            "native_model_hash": fingerprint(shadow["models"][0]), "full_model_hash": fingerprint(record["model"])})
    return {"status": "descriptive_ready", "scope": SCOPE, "baseline": "native_NAV_without_industry_news",
        "model_class": "same_ElasticNet_grid_and_purged_chronological_CV", "codes": codes,
        "loss_definition": "same_equal_latent_MSE_equal_fund_group_weights_not_currency_profit",
        "paired_rows": paired, "paired_dates": len(paired),
        "mean_loss_difference_native_minus_full": math.fsum(row["loss_difference_native_minus_full"] for row in paired)/len(paired),
        "selection_dates": [row["date"] for row in selected], "calibration_dates": [row["date"] for row in calibration],
        "dependence_scope": "paired_overlapping_horizon_time_series_descriptive_no_iid_significance_or_profit_claim",
        "fee_contracts_hash": fingerprint(context.get("fee_contracts", {})), "fees_used_in_forecast_loss": False,
        "currency_increment": currency_summary(wealth_pairs, wealth_gaps, len(paired)),
        "shadow_orders_created": False, "used_for_order_selection": False, "required_actions": []}


def validate_predictive_increment(value, inputs, *, wealth_evaluator=None):
    expected = predictive_increment(inputs["context"], inputs["samples"], inputs["fitting"], inputs["policy"], wealth_evaluator or inputs.get("wealth_evaluator"))
    require(value == expected, "Predictive increment differs from the sealed original source comparison")
    require(value["shadow_orders_created"] is False and value["used_for_order_selection"] is False,
            "Predictive increment escaped its research boundary")
    for row in value["paired_rows"]:
        require(math.isclose(row["loss_difference_native_minus_full"], row["native_loss"]-row["full_loss"], abs_tol=1e-12),
                "Paired predictive loss difference changed")
        require(row["origin_at"][:10] < row["label_available"] and all(fold["training_max_label_available"] < fold["validation_start"]
                for fold in row["folds"]), "Predictive increment uses immature training labels")
    partial = value["status"] == "insufficient_evidence" or value["currency_increment"]["status"] != "descriptive_source_replay_ready"
    result = {"status": "partial" if partial else "passed", "scope": "paired_source_prediction_loss_and_maturity_not_investment_efficacy",
              "checks": {"same_class_reconstruction": True, "paired_loss_arithmetic": True, "no_shadow_orders": True}}
    if partial:
        result.update(readiness={"trade_ready": False}, required_actions=value["required_actions"]+value["currency_increment"].get("required_actions", []))
    return result


def currency_unavailable(reason):
    return {"status": "insufficient_evidence", "scope": CURRENCY_SCOPE, "reason": reason, "paired_rows": [],
            "profitability_claim": False, "causal_news_claim": False,
            "required_actions": [{"action": "supply_original_PIT_historical_account_fee_clock_cashflow_frames", "reason": reason}]}


def currency_pair(replay, record, codes, full_prediction, native_prediction):
    """Verify paired research frames and distinguish current-terms repricing."""
    frame_keys = ("candidate_family_hash", "account_state_hash", "fee_contracts_hash", "clock_hash",
                  "external_cashflows_hash", "initial_wealth_cny")
    require(all(key in replay for key in frame_keys), "Paired wealth replay has no complete common financial frame")
    frame = {key: replay[key] for key in frame_keys}
    require(replay["candidate_family_hash"] == fingerprint(codes) and replay["frame_hash"] == fingerprint(frame),
            "Paired wealth changed the candidate family or common frame")
    evidence = replay["source_evidence"]
    qualified = replay["status"] == "source_replay_ready"
    require((not qualified or evidence.get("strict_PIT_verified") is True) and evidence.get("historical_frame_ref")
            and evidence.get("source_labels_hash") == fingerprint(record["source_hashes"]),
            "Paired currency comparison lacks original historical source frame and observed outer labels")
    require(evidence.get("replay_scope") in ("current_terms_repriced", "source_historical_terms")
            and evidence.get("account_scope"), "Paired wealth needs explicit account and source-terms scope")
    require(math.isfinite(float(frame["initial_wealth_cny"])) and frame["initial_wealth_cny"] > 0,
            "Paired source financial frame requires positive observed currency wealth")
    metrics = ("after_fee_terminal_wealth_cny", "maximum_drawdown", "cvar_loss_cny", "turnover_cny")
    for branch in ("full", "native"):
        values = replay[branch]
        require(all(key in values and math.isfinite(float(values[key]))
                    and (key == "cvar_loss_cny" or values[key] >= 0) for key in metrics),
                "Paired fee-net wealth/risk/turnover metric is invalid")
        require(values["maximum_drawdown"] <= 1, "Paired source maximum drawdown exceeds its economic domain")
        require(values.get("frame_hash") == replay["frame_hash"], "Paired branch uses a different financial frame")
    return {"date": record["date"], "origin_at": record["origin_at"], "frame": frame, "frame_hash": replay["frame_hash"],
            "source_evidence": evidence, "replay_scope": evidence["replay_scope"], "account_scope": evidence["account_scope"],
            "source_artifacts_independently_verified": qualified and evidence.get("strict_PIT_verified") is True,
            "scenario_risk_scope": replay.get("scenario_risk_scope", evidence.get("scenario_risk_scope")),
            "no_residual_risk_evaluation": replay.get("no_residual_risk_evaluation", True),
            "full_production_MPC_advantage_validated": replay.get("full_production_MPC_advantage_validated", False),
            "full": replay["full"], "native": replay["native"],
            "full_prediction_hash": fingerprint(full_prediction), "native_prediction_hash": fingerprint(native_prediction),
            "after_fee_wealth_difference_cny": replay["full"]["after_fee_terminal_wealth_cny"]-replay["native"]["after_fee_terminal_wealth_cny"],
            "maximum_drawdown_difference": replay["full"]["maximum_drawdown"]-replay["native"]["maximum_drawdown"],
            "cvar_difference_cny": replay["full"]["cvar_loss_cny"]-replay["native"]["cvar_loss_cny"],
            "turnover_difference_cny": replay["full"]["turnover_cny"]-replay["native"]["turnover_cny"]}


def currency_summary(rows, gaps, required_pairs):
    if gaps or len(rows) != required_pairs or not rows:
        result = currency_unavailable("complete_paired_historical_currency_replay_unavailable")
        result.update(paired_rows=rows, source_gaps=gaps, required_pairs=required_pairs)
        return result
    if any(row["source_artifacts_independently_verified"] is not True for row in rows):
        result = currency_unavailable("registered_frame_arithmetic_without_independently_audited_original_source_artifacts")
        result.update(paired_rows=rows, conditional_arithmetic_ready=True,
                      no_residual_risk_evaluation=any(row["no_residual_risk_evaluation"] for row in rows),
                      full_production_MPC_advantage_validated=False)
        return result
    historical = all(row["replay_scope"] == "source_historical_terms"
                     and row["source_evidence"].get("frame_source_PIT_verified") is True for row in rows)
    production_risk = all(row["no_residual_risk_evaluation"] is False
                          and row["full_production_MPC_advantage_validated"] is True for row in rows)
    return {"status": "descriptive_source_replay_ready" if historical else "descriptive_current_terms_repricing_ready",
            "scope": CURRENCY_SCOPE, "paired_rows": rows,
            "replay_scopes": sorted({row["replay_scope"] for row in rows}),
            "account_scopes": sorted({row["account_scope"] for row in rows}),
            "historical_source_terms_verified": historical,
            "scenario_risk_scopes": sorted({row["scenario_risk_scope"] for row in rows if row["scenario_risk_scope"]}),
            "no_residual_risk_evaluation": not production_risk,
            "full_production_MPC_advantage_validated": production_risk,
            "paired_dates": len(rows),
            "mean_after_fee_wealth_difference_cny": math.fsum(row["after_fee_wealth_difference_cny"] for row in rows)/len(rows),
            "mean_cvar_difference_cny": math.fsum(row["cvar_difference_cny"] for row in rows)/len(rows),
            "mean_turnover_difference_cny": math.fsum(row["turnover_difference_cny"] for row in rows)/len(rows),
            "dependence_scope": "paired_dependent_outer_OOS_descriptive_not_iid_or_causal_news_evidence",
            "profitability_claim": False, "causal_news_claim": False,
            "required_actions": [] if historical else [{"action": "supply_original_historical_fee_clock_frames_for_historical_terms_efficacy"}]}
