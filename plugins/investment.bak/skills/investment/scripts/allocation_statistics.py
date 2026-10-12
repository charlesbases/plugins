"""As-of Elastic Net and dependent-series inference; no account writes.

Block selection and resampling use the pinned arch implementation. Evidence
states describe the supplied series, not trial registration or live eligibility.
Operating thresholds are declared research policy, not universal pass marks.
"""

import collections
import datetime as dt
import math
import warnings

import numpy as np
from arch import __version__ as arch_version
from arch.bootstrap import StationaryBootstrap, optimal_block_length
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import StandardScaler
from contracts import fingerprint


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    require(type(value) in (int, float) and math.isfinite(value), "Expected finite number")
    return float(value)


def date(value):
    require(isinstance(value, str), "Date must be YYYY-MM-DD")
    result = dt.date.fromisoformat(value)
    require(result.isoformat() == value, "Date must be YYYY-MM-DD")
    return result


def digest(value):
    return fingerprint(value)


def _weights(rows):
    counts = collections.Counter((r["decision_date"], r["fund_group_id"]) for r in rows)
    groups = collections.Counter(d for d, _ in counts)
    return np.array([1. / (groups[r["decision_date"]] *
                          counts[r["decision_date"], r["fund_group_id"]]) for r in rows])


def _fit(rows, alpha, ratio):
    from allocation_market import LATENT_NAMES
    x=np.array([row["x"] for row in rows])
    y=np.asarray([[row["latent_targets"][name] for name in LATENT_NAMES] for row in rows])
    weights=_weights(rows)
    scaler=StandardScaler().fit(x,sample_weight=weights)
    estimator=ElasticNet(alpha=alpha,l1_ratio=ratio,max_iter=20000,tol=1e-8)
    coefficients=np.zeros((y.shape[1],x.shape[1]))
    intercepts=y[0].copy()
    active=[index for index in range(y.shape[1]) if not np.all(y[:,index]==y[0,index])]
    if active:
        with warnings.catch_warnings():
            warnings.simplefilter("error",ConvergenceWarning)
            estimator.fit(scaler.transform(x),y[:,active],sample_weight=weights)
        coefficients[active]=estimator.coef_
        intercepts[active]=estimator.intercept_
    return {"scaler_mean":scaler.mean_.tolist(),"scaler_scale":scaler.scale_.tolist(),
        "coefficients":coefficients.tolist(),"intercept":intercepts.tolist(),"alpha":alpha,"l1_ratio":ratio,
        "target_names":list(LATENT_NAMES),"target_link":"two_log_NAV_prices_four_squared_cash_rights_memberships",
        "constant_target_solution":"exact_zero_coefficients_and_constant_intercept_for_constant_training_column"}


def predict(model, x):
    standardized = (np.asarray(x)-model["scaler_mean"])/model["scaler_scale"]
    value = np.asarray(model["intercept"])+np.asarray(model["coefficients"]).dot(standardized)
    return value.tolist() if value.ndim else float(value)


def _squared_prediction_error(row, model):
    expected = np.asarray([row["latent_targets"][name] for name in model["target_names"]])
    return float(np.mean(np.square(expected-predict(model, row["x"]))))


def _fit_mean_at(samples, prediction_rows, policy, training_ceiling_at=None):
    """Fit the current six latent coordinates using strictly mature labels.

    Policy declares train_window_days, min_train_dates, cv_folds, alpha_grid,
    l1_ratio_grid, min_joint_dates, cv_initial_train_fraction and feature_names.
    Costs belong to execution.
    """
    require(isinstance(samples, list) and isinstance(prediction_rows, list) and prediction_rows,
            "Sample and nonempty prediction lists required")
    require(isinstance(policy, dict), "Training policy required")
    for key in ("train_window_days", "min_train_dates", "cv_folds", "min_joint_dates"):
        require(type(policy.get(key)) is int and policy[key] > 0, "Positive integer policy: " + key)
    require(policy["min_train_dates"] >= 2, "At least two training dates required")
    require(0 < finite(policy.get("cv_initial_train_fraction")) < 1,
            "Prespecified initial inner-training fraction required")
    names = policy.get("feature_names")
    require(isinstance(names, list) and names and all(isinstance(n, str) and n for n in names)
            and len(set(names)) == len(names), "Distinct ordered feature names required")
    for key in ("alpha_grid", "l1_ratio_grid"):
        require(isinstance(policy.get(key), list) and policy[key], "Nonempty parameter grid: " + key)
        for value in policy[key]:
            finite(value)
            require(value > 0 if key == "alpha_grid" else 0 < value <= 1, "Invalid Elastic Net grid")
    decision = prediction_rows[0].get("decision_date")
    boundary = date(decision)
    horizon = prediction_rows[0].get("horizon_days")
    require(type(horizon) is int and horizon > 0, "Positive horizon required")
    codes = []
    for row in prediction_rows:
        require(row.get("decision_date") == decision and row.get("horizon_days") == horizon,
                "Predict one date and horizon per call")
        require(isinstance(row.get("code"), str) and row["code"], "Fund code required")
        require(isinstance(row.get("x"), list) and len(row["x"]) == len(names), "Feature dimension mismatch")
        for value in row["x"]:
            finite(value)
        codes.append(row["code"])
    require(len(set(codes)) == len(codes), "Duplicate prediction code")
    lower = boundary - dt.timedelta(days=policy["train_window_days"])
    from allocation_market import mature_label_at
    eligible, identities = [], set()
    ceiling_at = min(decision, training_ceiling_at[:10]) if training_ceiling_at is not None else decision
    for original in samples:
        row = original
        require(isinstance(row, dict), "Sample must be object")
        if row.get("horizon_days") != horizon or row.get("code") not in codes:
            continue
        observed = date(row.get("decision_date"))
        # Future features/outcomes must not affect this fit or its input fingerprint.
        if observed >= boundary or observed < lower:
            continue
        row = mature_label_at(original, ceiling_at)
        if row is None:
            continue
        available = row.get("label_available_date")
        ceiling = date(training_ceiling_at[:10]) if training_ceiling_at is not None else boundary
        if available is None or date(available) >= min(boundary, ceiling) or row.get("targets") is None or row.get("industry_ready") is False:
            continue
        require(date(available) >= observed + dt.timedelta(days=horizon),
                "Label cannot precede its return horizon")
        if row.get("targets") is not None:
            from allocation_market import TARGET_NAMES
            require(set(row["targets"]) == set(TARGET_NAMES)
                    and all(finite(row["targets"][name]) > 0 for name in TARGET_NAMES), "Positive complete joint targets required")
            from allocation_market import LATENT_NAMES
            require(set(row["latent_targets"]) == set(LATENT_NAMES)
                    and all(math.isfinite(value) for value in row["latent_targets"].values()), "Complete finite latent labels required")
        require(isinstance(row.get("fund_group_id"), str) and row["fund_group_id"], "Fund group required")
        require(isinstance(row.get("x"), list) and len(row["x"]) == len(names), "Feature dimension mismatch")
        for value in row["x"]:
            finite(value)
        identity = (row["decision_date"], row["code"])
        require(identity not in identities, "Duplicate eligible fund/date sample")
        identities.add(identity)
        eligible.append(row)
    eligible.sort(key=lambda row: (row["decision_date"], row["code"]))
    dates = sorted({r["decision_date"] for r in eligible})
    audit = {"decision_date": decision, "horizon_days": horizon, "policy_hash": digest(policy),
             "training_rows": len(eligible), "training_dates": len(dates),
             "training_data_hash": digest(eligible), "training_window_start": lower.isoformat(),
             "max_label_available_date": max((r["label_available_date"] for r in eligible), default=None),
             "target": "two_log_NAV_prices_four_square_root_cash_rights_memberships", "costs": "account_specific_execution_layer_only",
             "weighting": "equal_date_equal_fund_group_equal_share_class", "folds": []}
    if training_ceiling_at is not None:
        audit["training_ceiling_at"] = training_ceiling_at
    if eligible and eligible[0].get("latent_targets") is not None:
        from allocation_market import LATENT_NAMES, TARGET_NAMES
        audit.update(target="two_log_NAV_prices_four_square_root_cash_rights_memberships",
            target_link="positive_prices_and_squared_nonnegative_membership_cash",
            target_cv_weighting="equal_mean_latent_squared_error_design_choice_not_unique_scientific_weights")
        audit["target_scale_diagnostics"] = {
            "latent": {name: {"mean": float(np.mean([row["latent_targets"][name] for row in eligible])),
                                "standard_deviation": float(np.std([row["latent_targets"][name] for row in eligible])),
                                "constant": all(row["latent_targets"][name] == eligible[0]["latent_targets"][name] for row in eligible)} for name in LATENT_NAMES},
            "public": {name: {"mean": float(np.mean([row["targets"][name] for row in eligible])),
                                "standard_deviation": float(np.std([row["targets"][name] for row in eligible]))} for name in TARGET_NAMES},
            "hold_buy_relationship": "shared_terminal_NAV_and_rights_decoder_not_independent_duplicate_training_outputs"}
    output = {"status": "insufficient_evidence", "models": [], "forecasts": [],
              "joint_scenarios": None, "training_audit": audit}
    if len(dates) < policy["min_train_dates"] + policy["cv_folds"]:
        return {**output, "reason": "insufficient_chronological_training_dates"}
    initial = max(policy["min_train_dates"], int(len(dates) * policy["cv_initial_train_fraction"]))
    cuts = np.linspace(initial, len(dates), policy["cv_folds"] + 1, dtype=int)
    folds = []
    for start, stop in zip(cuts[:-1], cuts[1:]):
        if start >= stop:
            return {**output, "reason": "empty_inner_validation_fold"}
        first = dates[start]
        fold_lower = date(first) - dt.timedelta(days=policy["train_window_days"])
        train = [value for original in samples
                 if original.get("code") in codes and original.get("horizon_days") == horizon
                 and original.get("industry_ready") is not False
                 and fold_lower <= date(original["decision_date"]) < date(first)
                 if (value := mature_label_at(original, first)) is not None
                 and value.get("targets") is not None and value["label_available_date"] < first]
        validation = [r for r in eligible if dates[start] <= r["decision_date"] <= dates[stop - 1]]
        if len({r["decision_date"] for r in train}) < policy["min_train_dates"]:
            return {**output, "reason": "insufficient_purged_inner_training_dates"}
        folds.append((train, validation))
        audit["folds"].append({"validation_start": first, "validation_end": dates[stop - 1],
                               "training_max_label_available": max(r["label_available_date"] for r in train),
                               "training_rows": len(train), "validation_rows": len(validation)})
    trials = []
    try:
        for alpha in sorted(set(policy["alpha_grid"])):
            for ratio in sorted(set(policy["l1_ratio_grid"])):
                errors, fold_models = [], []
                for train, validation in folds:
                    fitted = _fit(train, alpha, ratio)
                    fold_models.append(fitted)
                    errors.append(float(np.average(
                        [_squared_prediction_error(r, fitted) for r in validation],
                        weights=_weights(validation))))
                trials.append({"alpha": alpha, "l1_ratio": ratio, "fold_mse": errors, "fold_models": fold_models,
                               "mean_mse": float(np.mean(errors))})
        selected = min(trials, key=lambda t: (t["mean_mse"], t["alpha"], t["l1_ratio"]))
        model = _fit(eligible, selected["alpha"], selected["l1_ratio"])
    except ConvergenceWarning:
        return {**output, "reason": "numerical_convergence_failure"}
    audit["cv_trials"] = trials
    forecasts = [{"code": r["code"], "predicted_coordinates": predict(model, r["x"]),
                  "feature_cutoff_date": r.get("feature_cutoff_date"),
                  "decision_date": decision, "horizon_days": horizon} for r in prediction_rows]
    output.update(status="fitted", models=[{**model, "feature_names": names, "horizon_days": horizon}], forecasts=forecasts)
    return output


def fit_joint_targets(samples, prediction_rows, policy, clock_context, training_ceiling_at=None):
    """Same Elastic Net family, coherent direct transaction targets.

    The estimator regularizes each output, sharing scaler and the declared
    parameter grid. Equal latent MSE is a design choice, not a unique
    scientific weighting. All historical predictions retune forward only.
    """
    from allocation_market import TARGET_NAMES, LATENT_NAMES, decode_latents, mature_label_at
    output = _fit_mean_at(samples, prediction_rows, policy, training_ceiling_at)
    if output["status"] != "fitted":
        return output
    require(output["models"][0].get("target_names") == list(LATENT_NAMES), "Joint latent target training records required")
    decision, horizon = prediction_rows[0]["decision_date"], prediction_rows[0]["horizon_days"]
    codes = [row["code"] for row in prediction_rows]
    lower = date(decision)-dt.timedelta(days=policy["train_window_days"])
    panels = collections.defaultdict(dict)
    for original in samples:
        row = mature_label_at(original, decision)
        if row is None:
            continue
        if (row.get("code") in codes and row.get("horizon_days") == horizon
                and lower <= date(row["decision_date"]) < date(decision)
                and row.get("label_available_date") is not None and row["label_available_date"] < decision
                and row.get("targets") is not None and row.get("industry_ready") is not False):
            panels[row["decision_date"]][row["code"]] = row
    records, errors, unavailable = [], [], []
    for origin in sorted(panels):
        if not all(code in panels[origin] for code in codes):
            unavailable.append({"decision_date": origin, "reason": "incomplete_synchronous_direct_target_labels"})
            continue
        rows = [{"decision_date": origin, "horizon_days": horizon, "code": code,
                 "x": panels[origin][code]["x"]} for code in codes]
        historical = _fit_mean_at(samples, rows, policy)
        if historical["status"] != "fitted":
            unavailable.append({"decision_date": origin, "reason": historical.get("reason", "unavailable")})
            if historical.get("reason") not in {"insufficient_chronological_training_dates", "insufficient_purged_inner_training_dates", "empty_inner_validation_fold"}:
                return {**output, "status": "insufficient_evidence", "reason": "joint_target_oos_reconstruction_failed"}
            continue
        predicted_latents = {name: [row["predicted_coordinates"][column] for row in historical["forecasts"]]
                             for column, name in enumerate(LATENT_NAMES)}
        decoded = [decode_latents({name: predicted_latents[name][column] for name in LATENT_NAMES}) for column in range(len(codes))]
        predicted = {name: [math.log(row[name]) for row in decoded] for name in TARGET_NAMES}
        realized = {name: [panels[origin][code]["targets"][name] for code in codes] for name in TARGET_NAMES}
        public_error = {name: [math.log(value)-guess for value, guess in zip(realized[name], predicted[name])] for name in TARGET_NAMES}
        realized_latents = {name: [panels[origin][code]["latent_targets"][name] for code in codes] for name in LATENT_NAMES}
        error = {name: [value-guess for value, guess in zip(realized_latents[name], predicted_latents[name])] for name in LATENT_NAMES}
        sources = {code: panels[origin][code]["label_source"] for code in codes}
        require(all(source is not None for source in sources.values()), "Independent source target evidence required")
        ends = {source["end_date"] for source in sources.values()}
        require(len(ends) == 1, "A joint label needs one common evaluation date")
        record = {"date": origin, "origin_at": origin+"T"+clock_context["order_time_local"]+"+08:00", "codes": codes,
                  "label_available": max(panels[origin][code]["label_available_date"] for code in codes),
                  "end_date": next(iter(ends)), "predicted_log_targets": predicted, "realized_targets": realized,
                  "source_labels": sources, "source_hashes": {code: source["source_hash"] for code, source in sources.items()},
                  "feature_cutoff_dates": {code: panels[origin][code]["feature_cutoff_date"] for code in codes},
                  "log_target_errors": public_error, "predicted_latents": predicted_latents,
                  "realized_latents": realized_latents, "latent_errors": error,
                  "model": historical["models"][0], "training_audit": historical["training_audit"],
                  "historically_sealed": False, "provenance": "reconstructed_forward_only_normal_clocks_not_historical_actual_fills"}
        if any(panels[origin][code].get("industry_source") is not None for code in codes):
            record["industry_sources"] = {code: panels[origin][code]["industry_source"] for code in codes}
            record["provenance"] = "reconstructed_forward_industry_to_fund_stack_normal_clocks_not_actual_fills"
        records.append(record)
        errors.append(error)
    audit = output["training_audit"]
    audit.update(target="two_log_NAV_prices_four_square_root_cash_rights_memberships", target_names=list(LATENT_NAMES),
                 public_target_names=list(TARGET_NAMES), target_link="positive_prices_and_squared_nonnegative_membership_cash",
                 target_cv_weighting="equal_mean_latent_squared_error_design_choice_not_unique_scientific_weights", oos_predictions=records,
                 oos_unavailable=unavailable, joint_oos_dates=len(records),
                 label_gaps=[{"decision_date": row["decision_date"], "code": row["code"], "reason": row.get("label_reason")}
                             for row in samples if row.get("targets") is None and row["decision_date"] < decision])
    if len(records) < policy["min_joint_dates"]:
        return {**output, "status": "insufficient_evidence", "reason": "insufficient_synchronous_forward_joint_target_history"}
    selection = records[:len(records)//2]
    selection_end = max(record["label_available"] for record in selection)
    calibration = [record for record in records[len(records)//2:] if record["date"] > selection_end]
    if not selection or not calibration:
        return {**output, "status": "insufficient_evidence", "reason": "insufficient_embargoed_selection_calibration_panels"}
    errors = [record["latent_errors"] for record in selection]
    means = [dict(zip(LATENT_NAMES, row["predicted_coordinates"])) for row in output["forecasts"]]
    targets = {name: [] for name in TARGET_NAMES}
    try:
        point = [decode_latents(mean) for mean in means]
        for error in errors:
            decoded = [decode_latents({name: mean[name]+error[name][index] for name in LATENT_NAMES}) for index, mean in enumerate(means)]
            for name in TARGET_NAMES:
                targets[name].append([row[name] for row in decoded])
    except (ValueError, OverflowError):
        return {**output, "status": "insufficient_evidence", "reason": "joint_target_scenario_outside_positive_wealth_domain"}
    for index, forecast in enumerate(output["forecasts"]):
        latest = next((row for row in samples if row["code"] == forecast["code"] and row["decision_date"] == decision), None)
        if latest is not None and latest.get("industry_source") is not None:
            forecast["industry_source"] = latest["industry_source"]
        forecast["predicted_latents"] = means[index]
        forecast.pop("predicted_coordinates")
        forecast["predicted_log_targets"] = {name: math.log(point[index][name]) for name in TARGET_NAMES}
        forecast["expected_gross_return"] = float(np.mean([row[index]-1 for row in targets["hold"]]))
    output.update(status="research_ready", joint_scenarios={"codes": codes, "dates": [record["date"] for record in selection],
        "probabilities": [1/len(selection)]*len(selection), "targets": targets,
        "current_point_targets": {name: [[row[name] for row in point]] for name in TARGET_NAMES},
        "returns": [[value-1 for value in row] for row in targets["hold"]], "clock_context": clock_context,
        "source_oos": records, "method": "forward_oos_joint_latent_errors_structural_cash_rights_decoder",
        "source_selection_oos": selection, "source_calibration_oos": calibration,
        "panel_split": {"kind": "chronological_label_embargo_v1", "selection_fraction": .5,
                        "selection_origin_dates": [record["date"] for record in selection],
                        "calibration_origin_dates": [record["date"] for record in calibration],
                        "selection_source_hash": digest(selection), "calibration_source_hash": digest(calibration)},
        "interpretation": "conditional_empirical_distribution_not_probability_guarantee",
        "serial_order_preserved": True, "cross_sectional_pairing_preserved": True,
        "empirical_prediction_error_included": True, "parameter_posterior": False,
        "historical_predictions": "reconstructed_forward_only_not_historical_seals"})
    return output




def optimal_stationary_block(values):
    """Library PW/PPW selector; reject degenerate inputs before resampling."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    require(x.ndim == 1 and n >= 20 and np.all(np.isfinite(x)),
            "Block selector requires 20 finite scalar observations")
    scale = float(np.std(x))
    require(np.ptp(x) > 0 and math.isfinite(scale) and scale > 0, "Degenerate observation variance")
    # Normalization makes numerical safeguards independent of return units.
    with np.errstate(divide="ignore", invalid="ignore"):
        length = float(optimal_block_length((x-x.mean())/scale)["stationary"].iloc[0])
    require(math.isfinite(length) and length >= 0, "Degenerate block length estimate")
    return {"length": max(1., length), "library_length": length,
            "method": "arch.optimal_block_length.stationary", "arch_version": arch_version}


def _bootstrap(values, length, repetitions, seed, statistic):
    bootstrap = StationaryBootstrap(length, np.asarray(values), seed=seed)
    result = []
    batch = []
    for positional, _ in bootstrap.bootstrap(repetitions):
        batch.append(positional[0])
        if len(batch) == 128:
            result.extend(statistic(np.stack(batch)).tolist())
            batch.clear()
    if batch:
        result.extend(statistic(np.stack(batch)).tolist())
    return np.array(result)


def _inference_inputs(dates, values, policy):
    require(isinstance(dates, list) and isinstance(values, list) and len(dates) == len(values),
            "Paired date and value lists required")
    parsed = [date(d) for d in dates]
    require(parsed == sorted(set(parsed)), "Dates must be unique and increasing")
    array = np.array([finite(v) for v in values])
    keys = ("confidence_level", "minimum_net_advantage", "maximum_ci_half_width",
            "mc_cdf_tolerance", "mc_failure_probability", "max_bootstrap_repetitions",
            "bootstrap_seed", "block_sensitivity_factors", "max_observation_gap_days")
    require(isinstance(policy, dict) and all(k in policy for k in keys), "Complete inference policy required")
    require(set(policy) <= set(keys) | {"minimum_tail_observations"}, "Unknown inference policy field")
    c, delta, width, eps, failure = [finite(policy[k]) for k in keys[:5]]
    require(.5 < c < 1 and 0 < eps < .5 and 0 < failure < 1 and width > 0,
            "Invalid statistical policy")
    require(type(policy["max_bootstrap_repetitions"]) is int and policy["max_bootstrap_repetitions"] > 1
            and type(policy["bootstrap_seed"]) is int and policy["bootstrap_seed"] >= 0,
            "Positive resampling budget and nonnegative integer seed required")
    require(type(policy["max_observation_gap_days"]) is int and policy["max_observation_gap_days"] > 0,
            "Positive maximum observation gap required")
    factors = policy["block_sensitivity_factors"]
    require(isinstance(factors, list) and len(factors) >= 3 and 1 in factors
            and all(finite(v) > 0 for v in factors) and min(factors) < 1 < max(factors),
            "Block sensitivity must include shorter, selected and longer blocks")
    # DKW plus a union bound covers all predeclared sensitivity distributions.
    # This controls simulation-CDF error, not statistical market uncertainty.
    repetitions = math.ceil(math.log(2 * len(set(factors)) / failure) / (2 * eps ** 2))
    base = {"status": "insufficient_evidence", "observations": len(array), "interval": None,
            "policy_hash": digest(policy), "requested_bootstrap_repetitions": repetitions,
            "bootstrap_repetitions": 0, "capability": {}, "sensitivity": [],
            "qualification_scope": "series_evidence_only_trial_eligibility_is_external",
            "assumption_scope": "weak_stationarity_weak_dependence_finite_variance"}
    reason = None
    if not len(array):
        reason = "no_mature_observations"
    elif len(array) < 20:
        reason = "too_short_for_block_length_estimation"
    elif max((b-a).days for a, b in zip(parsed[:-1], parsed[1:])) > policy["max_observation_gap_days"]:
        reason = "irregular_observation_gap_requires_review"
    elif repetitions > policy["max_bootstrap_repetitions"]:
        reason = "declared_monte_carlo_precision_exceeds_budget"
    elif eps >= (1-c)/2:
        reason = "monte_carlo_CDF_precision_too_coarse_for_interval_tail"
    if reason:
        base["reason"] = reason
    return array, base


def _intervals(values, policy, base, statistic, selector_values=None):
    try:
        selector = optimal_stationary_block(values if selector_values is None else selector_values)
    except ValueError as exc:
        base["reason"] = str(exc)
        return base
    alpha = 1 - policy["confidence_level"]
    estimate = float(statistic(values[None, :])[0])
    repetitions = base["requested_bootstrap_repetitions"]
    for factor in sorted(set(policy["block_sensitivity_factors"])):
        length = max(1., min(len(values) / 3, selector["length"] * factor))
        sampled = _bootstrap(values, length, repetitions, policy["bootstrap_seed"], statistic)
        # Widen endpoints by the DKW probability error instead of treating
        # finite simulated quantiles as exact conditional-bootstrap quantiles.
        epsilon = policy["mc_cdf_tolerance"]
        quantiles = np.quantile(sampled, [alpha/2-epsilon, 1-alpha/2+epsilon])
        bounds = (2 * estimate - quantiles[::-1]).tolist()
        se = float(np.std(sampled, ddof=1))
        if not np.all(np.isfinite(sampled)) or not math.isfinite(se) or se <= 0:
            base["reason"] = "degenerate_bootstrap_distribution"
            return base
        base["sensitivity"].append({"factor": factor, "block_length": length, "interval": bounds,
                                     "standard_error": se})
    lower = min(row["interval"][0] for row in base["sensitivity"])
    upper = max(row["interval"][1] for row in base["sensitivity"])
    half_width = (upper-lower)/2
    base.update(interval=[lower, upper], estimate=estimate, block_length=selector,
                bootstrap_repetitions=repetitions, capability={
                    "ci_half_width": half_width, "precision_satisfied": half_width <= policy["maximum_ci_half_width"],
                    "mc_cdf_tolerance": policy["mc_cdf_tolerance"],
                    "mc_failure_probability": policy["mc_failure_probability"]})
    if half_width > policy["maximum_ci_half_width"]:
        base["reason"] = "interval_precision_insufficient"
    return base


def evaluate_advantage(dates, gains, policy):
    """Mean advantage evidence for one supplied series and benchmark."""
    values, base = _inference_inputs(dates, gains, policy)
    base.update(estimate=float(values.mean()) if len(values) else None,
                minimum_net_advantage=policy["minimum_net_advantage"])
    if "reason" in base:
        return base
    base = _intervals(values, policy, base, lambda x: x.mean(axis=1))
    if "reason" in base:
        return base
    lower, upper = base["interval"]
    delta = policy["minimum_net_advantage"]
    if lower > delta:
        return {**base, "status": "supported", "reason": "robust_interval_exceeds_required_advantage"}
    if upper <= delta:
        return {**base, "status": "not_supported", "reason": "interval_does_not_reach_required_advantage"}
    return {**base, "status": "inconclusive", "reason": "interval_crosses_required_advantage"}
