"""As-of Elastic Net and dependent-series inference; no account writes.

Block selection: Politis & White (2004), corrected by Patton, Politis & White
(2009), DOI 10.1080/07474930802459016. Formula/truncation reference:
https://bashtage.github.io/arch/bootstrap/generated/arch.bootstrap.optimal_block_length.html
Operating thresholds are declared research policy, not universal pass marks.
"""

import collections
import datetime as dt
import hashlib
import json
import math
import warnings

import numpy as np
from scipy.stats import norm
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import StandardScaler


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
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _weights(rows):
    counts = collections.Counter((r["decision_date"], r["fund_group_id"]) for r in rows)
    groups = collections.Counter(d for d, _ in counts)
    return np.array([1. / (groups[r["decision_date"]] *
                          counts[r["decision_date"], r["fund_group_id"]]) for r in rows])


def _fit(rows, alpha, ratio):
    x, y = np.array([r["x"] for r in rows]), np.array([r["return"] for r in rows])
    weights = _weights(rows)
    scaler = StandardScaler().fit(x, sample_weight=weights)
    estimator = ElasticNet(alpha=alpha, l1_ratio=ratio, max_iter=20000, tol=1e-8)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        estimator.fit(scaler.transform(x), y, sample_weight=weights)
    return {"scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
            "coefficients": estimator.coef_.tolist(), "intercept": float(estimator.intercept_),
            "alpha": alpha, "l1_ratio": ratio}


def predict(model, x):
    return float(model["intercept"] + np.dot(
        (np.asarray(x) - model["scaler_mean"]) / model["scaler_scale"], model["coefficients"]))


def fit_predict(samples, prediction_rows, policy):
    """One date/horizon; use only strictly mature gross NAV total returns.

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
    eligible, identities = [], set()
    for row in samples:
        require(isinstance(row, dict), "Sample must be object")
        if row.get("horizon_days") != horizon or row.get("code") not in codes:
            continue
        observed = date(row.get("decision_date"))
        # Future features/outcomes must not affect this fit or its input fingerprint.
        if observed >= boundary or observed < lower:
            continue
        available = row.get("label_available_date")
        if available is None or date(available) >= boundary or row.get("return") is None:
            continue
        require(date(available) >= observed + dt.timedelta(days=horizon),
                "Label cannot precede its return horizon")
        require(finite(row["return"]) >= -1, "Gross total return below -100%")
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
             "target": "gross_NAV_total_return", "costs": "account_specific_execution_layer_only",
             "weighting": "equal_date_equal_fund_group_equal_share_class", "folds": []}
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
        train = [r for r in eligible if r["label_available_date"] < first
                 and date(r["decision_date"]) >= fold_lower]
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
                errors = []
                for train, validation in folds:
                    fitted = _fit(train, alpha, ratio)
                    errors.append(float(np.average(
                        [(r["return"] - predict(fitted, r["x"])) ** 2 for r in validation],
                        weights=_weights(validation))))
                trials.append({"alpha": alpha, "l1_ratio": ratio, "fold_mse": errors,
                               "mean_mse": float(np.mean(errors))})
        selected = min(trials, key=lambda t: (t["mean_mse"], t["alpha"], t["l1_ratio"]))
        model = _fit(eligible, selected["alpha"], selected["l1_ratio"])
    except ConvergenceWarning:
        return {**output, "reason": "numerical_convergence_failure"}
    audit["cv_trials"] = trials
    forecasts = [{"code": r["code"], "expected_gross_return": predict(model, r["x"]),
                  "decision_date": decision, "horizon_days": horizon} for r in prediction_rows]
    output.update(models=[{**model, "feature_names": names, "horizon_days": horizon}], forecasts=forecasts)
    if any(r["expected_gross_return"] < -1 for r in forecasts):
        return {**output, "reason": "forecast_outside_gross_return_domain"}
    panels = collections.defaultdict(dict)
    for row in eligible:
        panels[row["decision_date"]][row["code"]] = row["return"]
    joint_dates = sorted(d for d, values in panels.items() if all(c in values for c in codes))
    if len(joint_dates) < policy["min_joint_dates"]:
        return {**output, "reason": "insufficient_synchronous_mature_history"}
    matrix = np.array([[panels[d][c] for c in codes] for d in joint_dates])
    means = np.array([row["expected_gross_return"] for row in forecasts])
    shifted = matrix - matrix.mean(axis=0) + means
    if np.min(shifted) < -1:
        return {**output, "reason": "mean_shift_outside_gross_return_domain"}
    output.update(status="research_ready", joint_scenarios={
        "codes": codes, "returns": shifted.tolist(), "dates": joint_dates,
        "probabilities": [1 / len(joint_dates)] * len(joint_dates),
        "method": "synchronous_historical_gross_returns_mean_shift",
        "interpretation": "conditional_empirical_distribution_not_probability_guarantee",
        "serial_order_preserved": True, "cross_sectional_pairing_preserved": True,
        "parameter_uncertainty_included": False})
    return output


def optimal_stationary_block(values):
    """PW selector with corrected D_SB=2*(long-run variance)^2 (PPW 2009)."""
    x = np.asarray(values, dtype=float)
    n = len(x)
    require(n >= 20 and np.all(np.isfinite(x)), "Block selector requires 20 finite observations")
    centered = x - x.mean()
    run = max(5, int(math.log10(n)))
    maximum_lag = min(n - 2, math.ceil(math.sqrt(n)) + run)
    covariance = np.array([np.dot(centered[k:], centered[:n-k]) / n for k in range(maximum_lag + 1)])
    correlations = []
    for k in range(maximum_lag + 1):
        denominator = math.sqrt(float(np.dot(centered[k+1:], centered[k+1:]) *
                                      np.dot(centered[:n-k-1], centered[:n-k-1])))
        correlations.append(abs(covariance[k] * n) / denominator if denominator else math.inf)
    threshold = 2 * math.sqrt(math.log10(n) / n)
    cutoff = next((i - run for i in range(run, maximum_lag + 1)
                   if all(c < threshold for c in correlations[i-run:i])), None)
    bandwidth = min(2 * max(cutoff, 1), maximum_lag) if cutoff is not None else maximum_lag
    lag = np.arange(1, bandwidth + 1)
    kernel = np.minimum(1., 2 * (1 - lag / bandwidth))
    long_run = float(covariance[0] + 2 * np.dot(kernel, covariance[1:bandwidth+1]))
    derivative = float(2 * np.dot(kernel * lag, covariance[1:bandwidth+1]))
    require(long_run > np.finfo(float).eps * max(1., float(covariance[0])),
            "Degenerate or nonpositive long-run variance")
    length = (n * derivative ** 2 / long_run ** 2) ** (1 / 3)
    return {"length": max(1., min(length, math.ceil(min(3 * math.sqrt(n), n / 3)))),
            "bandwidth": bandwidth, "long_run_variance": long_run,
            "method": "Politis_White_2004_Patton_Politis_White_2009_correction"}


def _bootstrap(values, length, repetitions, seed, statistic):
    generator = np.random.default_rng(seed)
    values = np.asarray(values)
    result = []
    for first in range(0, repetitions, 128):
        size = min(128, repetitions - first)
        indices = np.empty((size, len(values)), dtype=int)
        indices[:, 0] = generator.integers(0, len(values), size=size)
        for j in range(1, len(values)):
            restart = generator.random(size) < 1 / length
            indices[:, j] = np.where(restart, generator.integers(0, len(values), size=size),
                                     (indices[:, j-1] + 1) % len(values))
        result.extend(statistic(values[indices]).tolist())
    return np.array(result)


def _inference_inputs(dates, values, policy):
    require(isinstance(dates, list) and isinstance(values, list) and len(dates) == len(values),
            "Paired date and value lists required")
    parsed = [date(d) for d in dates]
    require(parsed == sorted(set(parsed)), "Dates must be unique and increasing")
    array = np.array([finite(v) for v in values])
    keys = ("confidence_level", "minimum_net_advantage", "maximum_ci_half_width", "design_effect",
            "target_power", "mc_cdf_tolerance", "mc_failure_probability", "max_bootstrap_repetitions",
            "bootstrap_seed", "block_sensitivity_factors", "preregistered_single_strategy",
            "assumption_review", "max_observation_gap_days")
    require(isinstance(policy, dict) and all(k in policy for k in keys), "Complete inference policy required")
    c, delta, width, effect, power, eps, failure = [finite(policy[k]) for k in keys[:7]]
    require(.5 < c < 1 and .5 < power < 1 and 0 < eps < .5 and 0 < failure < 1
            and width > 0 and effect > 0, "Invalid statistical policy")
    require(type(policy["max_bootstrap_repetitions"]) is int and policy["max_bootstrap_repetitions"] > 1
            and type(policy["bootstrap_seed"]) is int, "Integer resampling budget and seed required")
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
            "qualification_limitations": [],
            "multiple_testing": "unsupported_only_one_preregistered_strategy_vs_one_benchmark",
            "assumption_scope": "weak_stationarity_weak_dependence_finite_variance"}
    review = policy["assumption_review"]
    reason = None
    if policy["preregistered_single_strategy"] is not True:
        base["qualification_limitations"].append("selection_or_multiple_testing_not_controlled")
    if not (isinstance(review, dict) and review.get("weak_stationarity") is True
            and review.get("weak_dependence") is True and isinstance(review.get("evidence"), str)
            and review["evidence"].strip()):
        base["qualification_limitations"].append("dependent_bootstrap_assumptions_not_reviewed")
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
        detectable = float((norm.ppf(1-alpha/2) + norm.ppf(policy["target_power"])) * se)
        base["sensitivity"].append({"factor": factor, "block_length": length, "interval": bounds,
                                     "standard_error": se, "detectable_effect": detectable})
    lower = min(row["interval"][0] for row in base["sensitivity"])
    upper = max(row["interval"][1] for row in base["sensitivity"])
    half_width = (upper-lower)/2
    detectable = max(row["detectable_effect"] for row in base["sensitivity"])
    base.update(interval=[lower, upper], estimate=estimate, block_length=selector,
                conditional_diagnostic_only=bool(base["qualification_limitations"]),
                bootstrap_repetitions=repetitions, capability={
                    "ci_half_width": half_width, "precision_satisfied": half_width <= policy["maximum_ci_half_width"],
                    "detectable_effect": detectable, "design_effect": policy["design_effect"],
                    "design_capability_satisfied": detectable <= policy["design_effect"],
                    "power_method": "normal_approximation_at_prespecified_effect_not_observed_power",
                    "mc_cdf_tolerance": policy["mc_cdf_tolerance"],
                    "mc_failure_probability": policy["mc_failure_probability"]})
    if half_width > policy["maximum_ci_half_width"] or detectable > policy["design_effect"]:
        base["reason"] = "precision_or_design_effect_capability_insufficient"
    return base


def evaluate_advantage(dates, gains, policy):
    """Single preregistered series; no SPA/multiple-strategy claim or observed power."""
    values, base = _inference_inputs(dates, gains, policy)
    base.update(estimate=float(values.mean()) if len(values) else None,
                minimum_net_advantage=policy["minimum_net_advantage"])
    if "reason" in base:
        return base
    base = _intervals(values, policy, base, lambda x: x.mean(axis=1))
    if "reason" in base:
        return base
    if base["qualification_limitations"]:
        return {**base, "reason": "qualification_not_met_conditional_diagnostic_only"}
    lower, upper = base["interval"]
    delta = policy["minimum_net_advantage"]
    if lower > delta:
        return {**base, "status": "passed", "reason": "robust_interval_exceeds_preregistered_advantage"}
    if upper <= delta:
        return {**base, "status": "failed", "reason": "interval_does_not_reach_required_advantage"}
    return {**base, "reason": "interval_crosses_required_advantage"}


def evaluate_tail_risk(dates, losses, tail_probability, budget, policy):
    """Upper-tail CVaR with dependent resampling and explicit tail-support policy.

    Losses must have the same horizon as the risk budget. A minimum tail count is
    only a necessary caller-defined guard, never by itself sufficient evidence.
    """
    q, limit = finite(tail_probability), finite(budget)
    require(0 < q < 1 and limit >= 0, "Tail probability and loss budget invalid")
    values, base = _inference_inputs(dates, losses, policy)
    require(type(policy.get("minimum_tail_observations")) is int and policy["minimum_tail_observations"] >= 2,
            "Prespecified minimum_tail_observations required")
    def cvar(matrix):
        ordered = np.sort(matrix, axis=1)[:, ::-1]
        count = len(values) * q
        whole = int(count)
        total = ordered[:, :whole].sum(axis=1)
        if whole < len(values):
            total += (count-whole) * ordered[:, whole]
        return total / count
    base.update(estimate=float(cvar(values[None, :])[0]) if len(values) else None, tail_probability=q, budget=limit,
                tail_observations=len(values)*q)
    if "reason" in base:
        return base
    if len(values)*q < policy["minimum_tail_observations"]:
        return {**base, "reason": "insufficient_tail_observations"}
    review = policy["assumption_review"]
    if not isinstance(review, dict) or review.get("tail_quantile_regular") is not True:
        base["qualification_limitations"].append("tail_quantile_regularity_not_reviewed")
    # CVaR influence, rather than raw loss autocorrelation, selects tail blocks.
    threshold = float(np.quantile(values, 1-q))
    influence = threshold + np.maximum(values-threshold, 0)/q
    base = _intervals(values, policy, base, cvar, selector_values=influence)
    if "reason" in base:
        return base
    if base["qualification_limitations"]:
        return {**base, "reason": "qualification_not_met_conditional_diagnostic_only"}
    if base["interval"][1] <= limit:
        return {**base, "status": "passed", "reason": "upper_CVaR_bound_within_budget"}
    if base["interval"][0] > limit:
        return {**base, "status": "failed", "reason": "lower_CVaR_bound_exceeds_budget"}
    return {**base, "reason": "CVaR_interval_crosses_budget"}
