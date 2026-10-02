"""Frozen, chronological research fits; no investor qualification or account writes."""

import collections
import datetime as dt
import hashlib
import json
import math
import warnings

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import StandardScaler

TARGETS = ("net_return", "terminal_loss", "principal_path_loss", "peak_drawdown")
GROUPS = {"momentum": [0, 1, 2], "volatility": [3], "drawdown": [4]}


class ModelError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ModelError(message)


def fingerprint(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ModelError("Inputs must contain finite JSON values") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def number(value):
    require(type(value) in (float, int) and math.isfinite(value), "Expected a finite number")
    return float(value)


def day(value):
    try:
        parsed = dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ModelError("Expected an ISO calendar date") from exc
    require(parsed.isoformat() == value, "Calendar dates must use YYYY-MM-DD")
    return parsed


def validate_samples(samples, feature_names):
    require(isinstance(feature_names, list) and len(feature_names) == 5
            and all(isinstance(s, str) and s for s in feature_names)
            and len(set(feature_names)) == 5, "Exactly five distinct ordered feature names are required")
    require(isinstance(samples, list) and samples, "Nonempty sample list required")
    identities, keys = set(), set()
    for row in samples:
        require(isinstance(row, dict), "Sample must be an object")
        for name in ("id", "code", "fund_group_id"):
            require(isinstance(row.get(name), str) and row[name], "Missing sample identity: " + name)
        require(row["id"] not in identities, "Duplicate sample id")
        identities.add(row["id"])
        require(row.get("scope") == "conditional_fee_scenario", "Unsupported research scope")
        require(type(row.get("horizon_days")) is int and 1 <= row["horizon_days"] <= 730,
                "Invalid calendar-day horizon")
        cutoff, decision = day(row.get("cutoff_date")), day(row.get("decision_date"))
        require(cutoff <= decision, "Feature cutoff follows decision")
        key = (row["code"], row["decision_date"], row["horizon_days"])
        require(key not in keys, "Run each cost scenario separately; duplicate fund/date/horizon")
        keys.add(key)
        require(isinstance(row.get("x"), list) and len(row["x"]) == 5, "Five feature values required")
        for value in row["x"]:
            number(value)
        require(row.get("status") in ("mature", "pending"), "Unknown sample maturity")
        if row["status"] == "mature":
            entry, end, available = (day(row.get(name)) for name in
                                     ("entry_date", "exit_date", "label_available_proxy"))
            require(decision <= entry <= end <= available, "Invalid trading/proxy date order")
            require(isinstance(row.get("y"), dict) and set(row["y"]) == set(TARGETS),
                    "All four targets must be present")
            for target in TARGETS:
                value = number(row["y"][target])
                require(value >= -1 if target == "net_return" else 0 <= value <= 1,
                        "Target is outside its declared domain")
    fingerprint(samples)


def freeze_policy(samples, feature_names):
    """Choose boundaries from dates only, before inspecting any model results."""
    validate_samples(samples, feature_names)
    dates = sorted({row["decision_date"] for row in samples})
    require(len(dates) >= 40, "At least forty unique decision dates required")
    train_stop, test_start = int(len(dates) * .6), int(len(dates) * .8)
    first = int(train_stop * .4)
    cuts = [first + (train_stop - first) * i // 3 for i in range(4)]
    return {"schema_version": 1, "policy_version": "conditional-elastic-net-1",
            "dataset_hash": fingerprint(samples), "feature_names": list(feature_names),
            "targets": list(TARGETS), "horizons": sorted({r["horizon_days"] for r in samples}),
            "calibration_start": dates[train_stop], "test_start": dates[test_start],
            "first_decision": dates[0], "last_decision": dates[-1],
            "cv_folds": [{"start": dates[cuts[i]], "end_exclusive": dates[cuts[i + 1]]}
                         for i in range(3)],
            "alphas": [.0001, .001, .01], "l1_ratios": [.1, .5, .9],
            "selection": "lowest equal-fold validation MSE, then smaller alpha, then smaller l1_ratio",
            "preprocessing": "weighted StandardScaler fit on each purged training segment only",
            "clamping": "return >= -1; each loss in [0,1]",
            "weighting": "equal decision date, equal fund group within date, equal share within group",
            "interval_levels": [.8, .9], "coverage_tolerance": .10,
            "bootstrap_repetitions": 500, "bootstrap_seed": 1729,
            "bootstrap_calendar_block_days": "horizon_days", "minimum_claim_blocks": 8,
            "confidence_level": .95, "advantage_lower_gain_required": 0.,
            "confidence_scope": "calendar-block bootstrap approximation; common exposure is not independent fund evidence",
            "ablation_groups": {name: list(indices) for name, indices in GROUPS.items()}, "strict_PIT_verified": False,
            "time_boundary": "label_available_proxy < next prediction stage start; assumed dates only"}


def weights(rows):
    counts = collections.Counter((r["decision_date"], r["fund_group_id"]) for r in rows)
    groups = collections.Counter(date for date, _ in counts)
    return np.array([1 / (groups[r["decision_date"]] * counts[r["decision_date"], r["fund_group_id"]])
                     for r in rows], dtype=float)


def clamp(value, target):
    return max(-1., value) if target == "net_return" else min(1., max(0., value))


def predict_model(model, x):
    """Reconstruct inference from portable JSON parameters, without pickle."""
    require(isinstance(model, dict) and model.get("target") in TARGETS, "Invalid model target")
    require(isinstance(x, list) and len(x) == 5, "Five prediction features required")
    values = [number(v) for v in x]
    indices = model.get("feature_indices")
    require(isinstance(indices, list) and indices and len(set(indices)) == len(indices)
            and all(type(i) is int and 0 <= i < 5 for i in indices), "Invalid model feature indices")
    arrays = [model.get(k) for k in ("scaler_mean", "scaler_scale", "coefficients")]
    require(all(isinstance(a, list) and len(a) == len(indices) for a in arrays),
            "Model parameter dimensions differ")
    means, scales, coefficients = [[number(v) for v in a] for a in arrays]
    require(all(s > 0 for s in scales), "Model scaler must be positive")
    result = number(model.get("intercept")) + sum(
        (values[i] - mean) / scale * coef for i, mean, scale, coef in
        zip(indices, means, scales, coefficients))
    return clamp(number(result), model["target"])


def fit_model(rows, target, alpha, ratio, indices=None):
    require(len({r["decision_date"] for r in rows}) >= 5, "Insufficient purged training dates")
    indices = list(range(5)) if indices is None else indices
    x = np.array([[r["x"][i] for i in indices] for r in rows], dtype=float)
    y, w = np.array([r["y"][target] for r in rows]), weights(rows)
    scaler = StandardScaler().fit(x, sample_weight=w)
    estimator = ElasticNet(alpha=alpha, l1_ratio=ratio, max_iter=20000, tol=1e-7, selection="cyclic")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        try:
            estimator.fit(scaler.transform(x), y, sample_weight=w)
        except ConvergenceWarning as exc:
            raise ModelError("Elastic Net did not converge under frozen iteration budget") from exc
    model = {"horizon_days": rows[0]["horizon_days"], "target": target,
             "feature_indices": indices, "scaler_mean": scaler.mean_.tolist(),
             "scaler_scale": scaler.scale_.tolist(), "coefficients": estimator.coef_.tolist(),
             "intercept": float(estimator.intercept_), "alpha": alpha, "l1_ratio": ratio,
             "baseline_mean": float(np.average(y, weights=w)), "train_mean": float(np.average(y, weights=w)),
             "training_rows": len(rows),
             "training_first_decision": min(r["decision_date"] for r in rows),
             "training_last_decision": max(r["decision_date"] for r in rows),
             "training_max_label_proxy": max(r["label_available_proxy"] for r in rows)}
    fingerprint(model)
    return model


def mse(model, rows, target):
    return float(np.average([(r["y"][target] - predict_model(model, r["x"])) ** 2 for r in rows],
                            weights=weights(rows)))


def weighted_quantile(values, w, quantile):
    order = np.argsort(values, kind="stable")
    ordered, cumulative = np.asarray(values)[order], np.cumsum(w[order])
    return float(ordered[min(int(np.searchsorted(cumulative, quantile * cumulative[-1])), len(order) - 1)])


def block_summary(rows, values, policy):
    """Bootstrap whole calendar blocks of date panels, never individual fund rows."""
    w = weights(rows)
    dates = sorted({r["decision_date"] for r in rows})
    origin, horizon = day(dates[0]), rows[0]["horizon_days"]
    blocks = {}
    for row, value, weight in zip(rows, values, w):
        key = (day(row["decision_date"]) - origin).days // horizon
        total = blocks.setdefault(key, [0., 0.])
        total[0] += float(value) * weight
        total[1] += weight
    table = np.array(list(blocks.values()))
    rng = np.random.default_rng(policy["bootstrap_seed"])
    sampled = table[rng.integers(0, len(table), size=(policy["bootstrap_repetitions"], len(table)))].sum(axis=1)
    bootstrap = sampled[:, 0] / sampled[:, 1]
    lower, upper = np.quantile(bootstrap, [.025, .975])
    return {"estimate": float(np.average(values, weights=w)), "ci95": [float(lower), float(upper)],
            "calendar_blocks": len(blocks), "decision_dates": len(dates),
            "claim_evidence": "sufficient" if len(blocks) >= policy["minimum_claim_blocks"] else "insufficient_evidence"}


def evaluate_predictions(predictions, policy):
    """Recompute held-out metrics entirely from the saved prediction records."""
    require(isinstance(predictions, list) and predictions, "Nonempty predictions required")
    require(isinstance(policy, dict) and policy.get("bootstrap_repetitions") == 500
            and policy.get("minimum_claim_blocks") == 8 and policy.get("coverage_tolerance") == .10
            and policy.get("bootstrap_seed") == 1729, "Unknown metric policy")
    groups, seen = {}, set()
    for row in predictions:
        require(isinstance(row, dict) and row.get("target") in TARGETS
                and row.get("split") in ("calibration", "test"), "Invalid prediction identity")
        require(isinstance(row.get("sample_id"), str) and isinstance(row.get("fund_group_id"), str),
                "Prediction lacks sample/group identity")
        require(type(row.get("horizon_days")) is int and row["horizon_days"] > 0, "Invalid prediction horizon")
        day(row.get("decision_date"))
        key = (row["sample_id"], row["target"], row["split"])
        require(key not in seen, "Duplicate prediction")
        seen.add(key)
        for name in ("actual", "predicted", "baseline_mean", "lo80", "hi80", "lo90", "hi90"):
            number(row.get(name))
        require(row["lo90"] <= row["lo80"] <= row["hi80"] <= row["hi90"], "Unordered prediction intervals")
        groups.setdefault((row["horizon_days"], row["target"], row["split"]), []).append(row)
    output = []
    for (horizon, target, split), rows in sorted(groups.items()):
        w = weights(rows)
        errors = np.array([r["actual"] - r["predicted"] for r in rows])
        result = {"horizon_days": horizon, "target": target, "split": split, "rows": len(rows),
                  "purpose": "calibration_in_sample_diagnostic" if split == "calibration" else "sealed_final_test",
                  "mae": float(np.average(abs(errors), weights=w)),
                  "rmse": float(math.sqrt(np.average(errors ** 2, weights=w))), "gains": {}, "intervals": {}}
        for baseline in ("baseline_mean", "zero"):
            gain = [(r["actual"] - (r[baseline] if baseline != "zero" else 0.)) ** 2 - e ** 2
                    for r, e in zip(rows, errors)]
            check = block_summary(rows, gain, policy)
            check["status"] = ("insufficient_evidence" if check["claim_evidence"] != "sufficient" else
                               "passed" if check["ci95"][0] > 0 else "failed")
            result["gains"][baseline] = check
        for level in (80, 90):
            lo, hi, nominal = "lo" + str(level), "hi" + str(level), level / 100
            check = block_summary(rows, [int(r[lo] <= r["actual"] <= r[hi]) for r in rows], policy)
            tail = (1 - nominal) / 2
            pinball = lambda residual, q: max(q * residual, (q - 1) * residual)
            check.update(width=float(np.average([r[hi] - r[lo] for r in rows], weights=w)),
                         lower_pinball=float(np.average([pinball(r["actual"] - r[lo], tail) for r in rows], weights=w)),
                         upper_pinball=float(np.average([pinball(r["actual"] - r[hi], 1 - tail) for r in rows], weights=w)),
                         lower_exceedance=float(np.average([r["actual"] < r[lo] for r in rows], weights=w)),
                         upper_exceedance=float(np.average([r["actual"] > r[hi] for r in rows], weights=w)))
            accepted = nominal - .10 - 1e-12 <= check["estimate"] <= nominal + .10 + 1e-12 and check["ci95"][0] >= nominal - .10 - 1e-12
            check["status"] = ("insufficient_evidence" if check["claim_evidence"] != "sufficient" else
                               "passed" if accepted else "failed")
            result["intervals"][str(level)] = check
        output.append(result)
    fingerprint(output)
    return output


def fit_evaluate(samples, policy):
    require(isinstance(policy, dict), "Frozen policy required")
    require(policy == freeze_policy(samples, policy.get("feature_names")), "Policy or input changed after freezing")
    models, predictions, trials, ablations, split_rows = [], [], [], [], []
    calibration_start, test_start = policy["calibration_start"], policy["test_start"]
    for horizon in policy["horizons"]:
        rows = [r for r in samples if r["status"] == "mature" and r["horizon_days"] == horizon]
        train = [r for r in rows if r["decision_date"] < calibration_start and r["label_available_proxy"] < calibration_start]
        calibration = [r for r in rows if calibration_start <= r["decision_date"] < test_start and r["label_available_proxy"] < test_start]
        test = [r for r in rows if r["decision_date"] >= test_start]
        require(calibration and test, "Insufficient purged calibration or final test samples")
        folds = []
        for boundary in policy["cv_folds"]:
            a = [r for r in train if r["decision_date"] < boundary["start"] and r["label_available_proxy"] < boundary["start"]]
            b = [r for r in train if boundary["start"] <= r["decision_date"] < boundary["end_exclusive"]]
            require(a and b, "Insufficient purged chronological CV samples")
            folds.append((a, b))
        split_rows.append({"horizon_days": horizon, "train_ids": [r["id"] for r in train],
                           "calibration_ids": [r["id"] for r in calibration], "test_ids": [r["id"] for r in test],
                           "folds": [{"train_ids": [r["id"] for r in a], "validation_ids": [r["id"] for r in b],
                                      "training_max_label_proxy": max(r["label_available_proxy"] for r in a),
                                      "prediction_start": min(r["decision_date"] for r in b)} for a, b in folds]})
        for target in TARGETS:
            candidates = []
            for alpha in policy["alphas"]:
                for ratio in policy["l1_ratios"]:
                    scores = []
                    for fold, (a, b) in enumerate(folds):
                        model = fit_model(a, target, alpha, ratio)
                        score = mse(model, b, target)
                        scores.append(score)
                        trials.append({"horizon_days": horizon, "target": target, "fold": fold,
                                       "alpha": alpha, "l1_ratio": ratio, "validation_mse": score,
                                       "fitted_model": model})
                    candidates.append((sum(scores) / len(scores), alpha, ratio))
            _, alpha, ratio = min(candidates)
            reference = next(t["validation_mse"] for t in reversed(trials)
                             if t["alpha"] == alpha and t["l1_ratio"] == ratio)
            for name, removed in GROUPS.items():
                a, b = folds[-1]
                reduced = fit_model(a, target, alpha, ratio, [i for i in range(5) if i not in removed])
                reduced_mse = mse(reduced, b, target)
                ablations.append({"horizon_days": horizon, "target": target, "removed": name,
                                  "fold": len(folds) - 1, "validation_mse": reduced_mse,
                                  "full_features_validation_mse": reference, "mse_change": reduced_mse - reference,
                                  "interpretation": "conditional validation sensitivity; not causal importance"})
            model = fit_model(train, target, alpha, ratio)
            residuals = [r["y"][target] - predict_model(model, r["x"]) for r in calibration]
            quantiles = {str(q): weighted_quantile(residuals, weights(calibration), q) for q in (.05, .1, .9, .95)}
            model["calibration"] = {"residual_quantiles": quantiles, "rows": len(calibration),
                                    "max_label_proxy": max(r["label_available_proxy"] for r in calibration)}
            models.append(model)
            for split, held_out in (("calibration", calibration), ("test", test)):
                for row in held_out:
                    predicted = predict_model(model, row["x"])
                    output = {"sample_id": row["id"], "code": row["code"], "fund_group_id": row["fund_group_id"],
                              "decision_date": row["decision_date"], "horizon_days": horizon, "target": target,
                              "actual": row["y"][target], "predicted": predicted,
                              "baseline_mean": model["baseline_mean"], "split": split}
                    for field, q in (("lo80", .1), ("hi80", .9), ("lo90", .05), ("hi90", .95)):
                        output[field] = clamp(predicted + quantiles[str(q)], target)
                    predictions.append(output)
    metrics = evaluate_predictions(predictions, policy)
    acceptance = [{"horizon_days": m["horizon_days"], "target": m["target"], "split": "test",
                   "execution_status": "completed", "mean_advantage": {k: v["status"] for k, v in m["gains"].items()},
                   "intervals": {k: v["status"] for k, v in m["intervals"].items()},
                   "investor_qualification": "insufficient_evidence"} for m in metrics if m["split"] == "test"]
    result = {"models": models, "predictions": predictions, "cv_trials": trials, "metrics": metrics,
              "acceptance": acceptance, "splits": {"policy": {k: policy[k] for k in ("calibration_start", "test_start", "cv_folds")},
                                                    "by_horizon": split_rows}, "ablations": ablations}
    fingerprint(result)
    return result
