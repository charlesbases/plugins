"""Audit saved research fits without fitting or selecting another model."""

import collections
import math

import numpy as np

import model_stats

TARGETS = ("net_return", "terminal_loss", "principal_path_loss", "peak_drawdown")
KKT_TOLERANCE = 1e-4
VALUE_TOLERANCE = 1e-10


class FitAuditError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise FitAuditError(message)


def finite(value):
    require(type(value) in (int, float) and math.isfinite(value), "Expected finite audit value")
    return float(value)


def compare(actual, expected, context):
    """Exact structure and identities; bounded tolerance only for computed floats."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), context + ": fields differ")
        for key, value in expected.items():
            compare(actual[key], value, context + "." + key)
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), context + ": list differs")
        for index, value in enumerate(expected):
            compare(actual[index], value, context + ":" + str(index))
    elif type(expected) is float:
        require(math.isclose(finite(actual), expected, rel_tol=1e-9, abs_tol=VALUE_TOLERANCE),
                context + ": numeric value differs")
    else:
        require(type(actual) is type(expected) and actual == expected, context + ": value differs")


def indexed(records, keys, expected, context):
    require(isinstance(records, list) and records, context + ": empty inventory")
    result = {}
    for row in records:
        require(isinstance(row, dict) and all(key in row for key in keys), context + ": missing identity")
        identity = tuple(row[key] for key in keys)
        require(identity not in result, context + ": duplicate identity")
        result[identity] = row
    require(set(result) == set(expected), context + ": incomplete or unexpected inventory")
    return result


def panel_weights(rows):
    shares, groups = collections.Counter(), {}
    for row in rows:
        date, group = row["decision_date"], row["fund_group_id"]
        shares[date, group] += 1
        groups.setdefault(date, set()).add(group)
    return np.array([1. / (len(groups[r["decision_date"]]) * shares[r["decision_date"], r["fund_group_id"]])
                     for r in rows])


def bounded(value, target):
    return max(-1., value) if target == "net_return" else max(0., min(1., value))


def prediction(model, row):
    raw = finite(model["intercept"]) + math.fsum(
        (finite(value) - mean) / scale * coefficient for value, mean, scale, coefficient in
        zip(row["x"], model["scaler_mean"], model["scaler_scale"], model["coefficients"]))
    return bounded(finite(raw), model["target"])


def partitions(samples, policy):
    """Reconstruct membership using dates and maturity, never persisted split IDs."""
    dates = sorted({r["decision_date"] for r in samples})
    train_stop, test_at = int(len(dates) * .6), int(len(dates) * .8)
    first = int(train_stop * .4)
    cuts = [first + (train_stop - first) * i // 3 for i in range(4)]
    boundaries = [{"start": dates[cuts[i]], "end_exclusive": dates[cuts[i + 1]]} for i in range(3)]
    expected_policy = {"calibration_start": dates[train_stop], "test_start": dates[test_at], "cv_folds": boundaries}
    compare({key: policy[key] for key in expected_policy}, expected_policy, "date boundaries")
    selected, saved = {}, []
    for horizon in policy["horizons"]:
        mature = [r for r in samples if r["horizon_days"] == horizon and r["status"] == "mature"]
        train = [r for r in mature if r["decision_date"] < dates[train_stop]
                 and r["label_available_proxy"] < dates[train_stop]]
        calibration = [r for r in mature if dates[train_stop] <= r["decision_date"] < dates[test_at]
                       and r["label_available_proxy"] < dates[test_at]]
        test = [r for r in mature if r["decision_date"] >= dates[test_at]]
        require(train and calibration and test, "Missing purged training/calibration/test segment")
        folds = []
        for boundary in boundaries:
            training = [r for r in train if r["decision_date"] < boundary["start"]
                        and r["label_available_proxy"] < boundary["start"]]
            validation = [r for r in train if boundary["start"] <= r["decision_date"] < boundary["end_exclusive"]]
            require(training and validation, "Missing purged CV segment")
            folds.append({"train_ids": [r["id"] for r in training], "validation_ids": [r["id"] for r in validation],
                          "training_max_label_proxy": max(r["label_available_proxy"] for r in training),
                          "prediction_start": min(r["decision_date"] for r in validation)})
        selected[horizon] = (train, calibration, test)
        saved.append({"horizon_days": horizon, "train_ids": [r["id"] for r in train],
                      "calibration_ids": [r["id"] for r in calibration], "test_ids": [r["id"] for r in test], "folds": folds})
    return selected, {"policy": expected_policy, "by_horizon": saved}


def audit_model(model, train, target, selected_parameters):
    compare([model.get("alpha"), model.get("l1_ratio")], list(selected_parameters), "CV selected parameters")
    compare(model.get("feature_indices"), [0, 1, 2, 3, 4], "final model features")
    require(isinstance(model.get("coefficients"), list) and len(model["coefficients"]) == 5, "Coefficient dimensions differ")
    coefficients = np.array([finite(v) for v in model["coefficients"]])
    x, y, w = np.array([r["x"] for r in train]), np.array([r["y"][target] for r in train]), panel_weights(train)
    mass = float(w.sum())
    means = np.sum(x * w[:, None], axis=0) / mass
    variances = np.sum((x - means) ** 2 * w[:, None], axis=0) / mass
    eps = np.finfo(float).eps
    # The constant-feature tolerance follows the floating-point variance bound.
    constant = variances <= mass * eps * variances + (mass * means * eps) ** 2
    scales = np.where(constant, 1., np.sqrt(variances))
    compare(model.get("scaler_mean"), means.tolist(), "training-only scaler mean")
    compare(model.get("scaler_scale"), scales.tolist(), "training-only scaler scale")
    mean_y = float(np.sum(y * w) / mass)
    for name in ("baseline_mean", "train_mean"):
        compare(model.get(name), mean_y, "training-only " + name)
    for name, value in {"training_rows": len(train), "training_first_decision": min(r["decision_date"] for r in train),
                        "training_last_decision": max(r["decision_date"] for r in train),
                        "training_max_label_proxy": max(r["label_available_proxy"] for r in train)}.items():
        compare(model.get(name), value, name)
    standardized = (x - means) / scales
    residual = standardized @ coefficients + finite(model["intercept"]) - y
    gradient = standardized.T @ (w * residual) / mass
    alpha, ratio = selected_parameters
    smooth_gradient = gradient + alpha * (1 - ratio) * coefficients
    kkt = np.where(coefficients != 0, abs(smooth_gradient + alpha * ratio * np.sign(coefficients)),
                   np.maximum(abs(smooth_gradient) - alpha * ratio, 0))
    violation = max(float(np.max(kkt)), abs(float(np.sum(w * residual) / mass)))
    require(math.isfinite(violation) and violation <= KKT_TOLERANCE,
            "Saved coefficients/intercept fail weighted Elastic Net KKT conditions")
    return violation


def calibration_quantiles(model, rows):
    residuals = [(r["y"][model["target"]] - prediction(model, r), float(w)) for r, w in zip(rows, panel_weights(rows))]
    residuals.sort(key=lambda item: item[0])
    cumulative_weights, running = [], 0.
    for _, weight in residuals:
        running += weight
        cumulative_weights.append(running)
    total = cumulative_weights[-1]
    result = {}
    for quantile in (.05, .1, .9, .95):
        value = residuals[-1][0]
        for (residual, _), cumulative in zip(residuals, cumulative_weights):
            if cumulative >= quantile * total:
                value = residual
                break
        result[str(quantile)] = value
    return result


def audit_fit(samples, policy, fitted):
    """Return canonical acceptance after checking source-bound saved fit artifacts."""
    require(isinstance(policy, dict) and isinstance(fitted, dict), "Policy and fit artifacts required")
    require(policy == model_stats.freeze_policy(samples, policy.get("feature_names")), "Frozen policy or input changed")
    selected, expected_splits = partitions(samples, policy)
    compare(fitted.get("splits"), expected_splits, "purged splits")
    required_models = {(h, target) for h in policy["horizons"] for target in TARGETS}
    models = indexed(fitted.get("models"), ("horizon_days", "target"), required_models, "models")
    trial_keys = {(h, t, fold, alpha, ratio) for h, t in required_models for fold in range(3)
                  for alpha in policy["alphas"] for ratio in policy["l1_ratios"]}
    trials = indexed(fitted.get("cv_trials"), ("horizon_days", "target", "fold", "alpha", "l1_ratio"), trial_keys, "CV trials")
    sample_index = {row["id"]: row for row in samples}
    fold_index = {part["horizon_days"]: part["folds"] for part in expected_splits["by_horizon"]}
    cv_kkt = 0.
    for row in trials.values():
        require(finite(row.get("validation_mse")) >= 0, "Negative CV error")
        fold = fold_index[row["horizon_days"]][row["fold"]]
        training = [sample_index[key] for key in fold["train_ids"]]
        validation = [sample_index[key] for key in fold["validation_ids"]]
        cv_model = row.get("fitted_model")
        require(isinstance(cv_model, dict), "Missing CV fitted parameters")
        require(cv_model.get("horizon_days") == row["horizon_days"] and cv_model.get("target") == row["target"], "CV parameter identity mismatch")
        cv_kkt = max(cv_kkt, audit_model(cv_model, training, row["target"], (row["alpha"], row["l1_ratio"])))
        errors = [(sample["y"][row["target"]] - prediction(cv_model, sample)) ** 2 for sample in validation]
        weights = panel_weights(validation)
        compare(row["validation_mse"], float(np.sum(weights * errors) / weights.sum()), "independent CV error")
    ablation_keys = {(h, target, group) for h, target in required_models for group in policy["ablation_groups"]}
    ablations = indexed(fitted.get("ablations"), ("horizon_days", "target", "removed"), ablation_keys, "ablations")
    expected_predictions, maximum_kkt = {}, 0.
    for horizon, target in sorted(required_models):
        candidates = []
        for alpha in policy["alphas"]:
            for ratio in policy["l1_ratios"]:
                mean_error = sum(trials[horizon, target, fold, alpha, ratio]["validation_mse"] for fold in range(3)) / 3
                candidates.append((mean_error, alpha, ratio))
        _, alpha, ratio = min(candidates)
        model = models[horizon, target]
        train, calibration, test = selected[horizon]
        maximum_kkt = max(maximum_kkt, audit_model(model, train, target, (alpha, ratio)))
        quantiles = calibration_quantiles(model, calibration)
        compare(model.get("calibration"), {"residual_quantiles": quantiles, "rows": len(calibration),
                                          "max_label_proxy": max(r["label_available_proxy"] for r in calibration)}, "calibration")
        for name in policy["ablation_groups"]:
            row = ablations[horizon, target, name]
            compare(row.get("fold"), 2, "ablation fold")
            reference = trials[horizon, target, 2, alpha, ratio]["validation_mse"]
            compare(row.get("full_features_validation_mse"), reference, "ablation reference")
            require(finite(row.get("validation_mse")) >= 0, "Negative ablation error")
            compare(row.get("mse_change"), float(row["validation_mse"] - reference), "ablation difference")
        for split, rows in (("calibration", calibration), ("test", test)):
            for row in rows:
                estimate = prediction(model, row)
                expected = {"sample_id": row["id"], "code": row["code"], "fund_group_id": row["fund_group_id"],
                            "decision_date": row["decision_date"], "horizon_days": horizon, "target": target,
                            "actual": row["y"][target], "predicted": estimate,
                            "baseline_mean": model["baseline_mean"], "split": split}
                for field, q in (("lo80", .1), ("hi80", .9), ("lo90", .05), ("hi90", .95)):
                    expected[field] = bounded(estimate + quantiles[str(q)], target)
                expected_predictions[row["id"], target, split] = expected
    predictions = indexed(fitted.get("predictions"), ("sample_id", "target", "split"), expected_predictions, "predictions")
    for identity, expected in expected_predictions.items():
        compare(predictions[identity], expected, "prediction " + str(identity))
    # Metrics use the tested deterministic evaluator after their full inputs are audited.
    metrics = model_stats.evaluate_predictions(fitted["predictions"], policy)
    compare(fitted.get("metrics"), metrics, "recomputed metrics")
    canonical = [{"horizon_days": m["horizon_days"], "target": m["target"], "split": "test",
                  "execution_status": "completed", "mean_advantage": {k: v["status"] for k, v in m["gains"].items()},
                  "intervals": {k: v["status"] for k, v in m["intervals"].items()},
                  "investor_qualification": "insufficient_evidence"} for m in metrics if m["split"] == "test"]
    indexed(canonical, ("horizon_days", "target"), required_models, "recomputed acceptance")
    compare(fitted.get("acceptance"), canonical, "recomputed acceptance")
    return {"verification": "passed", "acceptance": canonical, "metrics": metrics,
            "models_checked": len(models), "predictions_checked": len(predictions), "CV_trials_checked": len(trials),
            "maximum_kkt_residual": maximum_kkt, "kkt_tolerance": KKT_TOLERANCE,
            "CV_maximum_kkt_residual": cv_kkt,
            "cv_scope": "all saved CV coefficients/KKT and held-out errors independently recomputed; no refitting"}
