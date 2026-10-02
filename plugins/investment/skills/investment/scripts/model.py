"""Execute frozen conditional model experiments and all validation stages."""

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import research
import model_scenarios as scenarios
from research_data import read_table
from storage import default_root, read_object, update_latest, write_small_json

SCRIPTS = Path(__file__).resolve().parent
MODEL_FILES = ("model.py", "model_stats.py", "model_scenarios.py", "model_verify.py", "requirements-model.txt")
EXCLUDED = {"model-manifest.json", "model-status.json", "artifact-hashes.jsonl"}


def require(condition, text):
    if not condition:
        raise ValueError(text)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def runtime(root):
    require(sys.version_info >= (3, 12), "Pinned scientific model dependencies require Python 3.12 or later")
    requirements = SCRIPTS / "requirements-model.txt"
    expected = dict(line.split("==") for line in requirements.read_text().splitlines() if line.strip())
    folder = research.owned_path(root, "runtime/model-" + sha(requirements)[:16])
    if folder.is_dir():
        sys.path.insert(0, str(folder))
    def versions():
        try:
            return {name: importlib.metadata.version(name) for name in expected}
        except importlib.metadata.PackageNotFoundError:
            return {}
    current = versions()
    if current != expected:
        require(not folder.exists(), "Pinned model runtime is incomplete; inspect it before retrying")
        folder.parent.mkdir(parents=True, exist_ok=True)
        stage = folder.with_name(folder.name + "-install-" + uuid.uuid4().hex[:8])
        stage.mkdir()
        with (stage / "install.log").open("wb") as log:
            process = subprocess.run([sys.executable, "-B", "-m", "pip", "install", "--disable-pip-version-check",
                                      "--only-binary=:all:", "--no-compile", "--target", str(stage),
                                      "-r", str(requirements)], stdout=log, stderr=subprocess.STDOUT, timeout=180)
        require(process.returncode == 0, "Model dependency installation failed; see " + str(stage / "install.log"))
        stage.rename(folder)
        sys.path.insert(0, str(folder))
        current = versions()
    require(current == expected, "Model dependency versions do not match the source lock")
    import model_stats
    return model_stats, current


def rows_write(folder, records, field="decision_date"):
    """Bounded date partitions; never put all predictions in one small JSON."""
    buckets = {}
    for position, source in enumerate(records):
        require("_record_order" not in source, "Reserved storage metadata field")
        row = {**source, "_record_order": position}
        month = str(row.get(field, "undated"))[:7]
        require(month == "undated" or len(month) == 7 and month[4] == "-", "Bad record month")
        buckets.setdefault(month, []).append(row)
    folder.mkdir(parents=True, exist_ok=False)
    for month, values in sorted(buckets.items()):
        part, size, stream = 1, 0, None
        try:
            for value in values:
                line = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                require(len(line) <= 16 * 1024 * 1024, "Single model record exceeds shard size")
                if stream is None or size + len(line) > 16 * 1024 * 1024:
                    if stream:
                        stream.close()
                        part += 1
                    stream = (folder / f"{month}-{part:06d}.jsonl").open("xb")
                    size = 0
                stream.write(line)
                size += len(line)
        finally:
            if stream:
                stream.close()


def rows_read(folder):
    rows = [json.loads(line) for path in sorted(folder.glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line]
    require(sorted(r.get("_record_order", -1) for r in rows) == list(range(len(rows))), "Record order inventory changed")
    rows.sort(key=lambda r: r["_record_order"])
    for row in rows:
        row.pop("_record_order")
    return rows


def inventory(run):
    items = []
    for path in sorted(run.rglob("*")):
        require(not path.is_symlink() and path.resolve().is_relative_to(run.resolve()), "Model artifact path escaped")
        if path.is_file() and path.relative_to(run).as_posix() not in EXCLUDED:
            items.append({"path": path.relative_to(run).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)})
    return items


def load_data(root, base, requested=None):
    data_run = research.resolve_run(base, requested)
    audit = research.verify_selected(root, base, data_run, registered=requested is None)
    manifest = read_object(data_run / "manifest.json")
    policy = read_object(data_run / "policy.json")
    nav = {code: read_table(data_run, "normalized", code) for code in manifest["codes"]}
    features = {code: read_table(data_run, "features", code) for code in manifest["codes"]}
    return data_run, audit, manifest, policy, nav, features


def save_replay(run, result):
    folder = run / "replay" / result["scenario_id"]
    summary = {k: v for k, v in result.items() if k != "horizons"}
    summary["horizons"] = {}
    for horizon, group in result["horizons"].items():
        target = {k: v for k, v in group.items() if k != "strategies"}
        target["strategies"] = {}
        for name, strategy in group["strategies"].items():
            detail = folder / horizon / name
            rows_write(detail / "wealth", strategy["period_wealth"], "date")
            rows_write(detail / "trades", strategy["trade_ledger"], "date")
            target["strategies"][name] = {k: v for k, v in strategy.items() if k not in ("period_wealth", "trade_ledger")}
        summary["horizons"][horizon] = target
    write_small_json(folder / "summary.json", summary)
    return summary


def read_replay(folder):
    value = read_object(folder / "summary.json")
    for horizon, group in value["horizons"].items():
        for name, strategy in group["strategies"].items():
            detail = folder / horizon / name
            strategy["period_wealth"] = rows_read(detail / "wealth")
            strategy["trade_ledger"] = rows_read(detail / "trades")
    return value


def strategy_checks(summaries, policy):
    import numpy as np
    results = []
    for summary in summaries:
        for horizon, group in summary["horizons"].items():
            gains = [row["paired_gain"] for row in group.get("paired_date_blocks", [])]
            require(gains, "Strategy produced no paired date blocks")
            rng = np.random.default_rng(policy["bootstrap_seed"])
            values = np.asarray(gains)
            simulated = values[rng.integers(0, len(values), (policy["bootstrap_repetitions"], len(values)))].mean(axis=1)
            interval = [float(x) for x in np.quantile(simulated, [.025, .975])]
            enough = len(gains) >= policy["minimum_claim_blocks"]
            strategies = group["strategies"]
            model_return = strategies["model"]["metrics"]["net_return"]
            beats_hold = all(model_return > value["metrics"]["net_return"] for name, value in strategies.items() if "buy_hold" in name)
            results.append({"scenario_id": summary["scenario_id"], "horizon_days": int(horizon),
                            "execution_status": "completed", "paired_blocks": len(gains),
                            "paired_mean_gain": float(values.mean()), "gain_ci95": interval,
                            "beats_all_buy_hold": beats_hold,
                            "acceptance_status": "passed" if enough and interval[0] > 0 and beats_hold else "failed",
                            "reason": "date-block improvement and hold comparisons required" if enough else "too_few_date_blocks"})
    return results


def acceptance(data_audit, fitted, strategy, policy):
    expected = {(h, target) for h in policy["horizons"] for target in policy["targets"]}
    models = fitted["models"]
    require(expected and len(models) == len(expected) and {(m["horizon_days"], m["target"]) for m in models} == expected,
            "Acceptance requires a complete nonempty model inventory")
    metrics = [m for m in fitted["metrics"] if m["split"] == "test"]
    require(len(metrics) == len(expected) and {(m["horizon_days"], m["target"]) for m in metrics} == expected,
            "Acceptance requires all final-test metrics")
    required_trials = len(expected) * len(policy["cv_folds"]) * len(policy["alphas"]) * len(policy["l1_ratios"])
    require(len(fitted["cv_trials"]) == required_trials, "Incomplete CV experiment budget")
    live_data = (data_audit["readiness"]["historical_investor"] == "passed"
                 and data_audit["readiness"]["strict_PIT"] == "verified" and policy["strict_PIT_verified"] is True)
    mean_ok = all(set(m["gains"]) == {"baseline_mean", "zero"} and all(v["status"] == "passed" for v in m["gains"].values()) for m in metrics)
    risk_ok = all(set(m["intervals"]) == {"80", "90"} and all(v["status"] == "passed" for v in m["intervals"].values()) for m in metrics)
    strategy_ok = bool(strategy) and all(row["acceptance_status"] == "passed" for row in strategy)
    checks = [{"name": "source_data_arithmetic", "execution_status": "completed", "acceptance_status": data_audit["readiness"]["market_research"]},
              {"name": "historical_investor_data_qualification", "execution_status": "completed", "acceptance_status": "passed" if live_data else "failed", "reasons": data_audit["missing_blockers"]},
              {"name": "model_fitting", "execution_status": "completed", "acceptance_status": "passed", "models": len(fitted["models"]), "CV_fits": len(fitted["cv_trials"])},
              {"name": "out_of_sample_mean_advantage", "execution_status": "completed", "acceptance_status": "passed" if mean_ok else "failed"},
              {"name": "risk_calibration", "execution_status": "completed", "acceptance_status": "passed" if risk_ok else "failed"},
              {"name": "fee_and_settlement_strategy_advantage", "execution_status": "completed", "acceptance_status": "passed" if strategy_ok else "failed"}]
    return {"execution_status": "completed", "checks": checks, "live_prediction_allowed": all((live_data, mean_ok, risk_ok, strategy_ok)),
            "candidate_status": "accepted" if all((live_data, mean_ok, risk_ok, strategy_ok)) else "rejected",
            "scope": "real_NAV_conditional_fee_scenarios_not_verified_historical_brokerage_fills"}


def strategy_predictions(samples, models, policy):
    """Predict every final-period decision, including outcomes not yet known."""
    import model_stats
    lookup = {m["horizon_days"]: m for m in models if m["target"] == "net_return"}
    require(set(lookup) == set(policy["horizons"]), "Missing frozen strategy model")
    result = []
    for sample in samples:
        if sample["decision_date"] < policy["test_start"]:
            continue
        result.append({"sample_id": sample["id"], "code": sample["code"], "fund_group_id": sample["fund_group_id"],
                       "decision_date": sample["decision_date"], "horizon_days": sample["horizon_days"],
                       "target": "net_return", "split": "test", "outcome_status": sample["status"],
                       "actual": sample["y"]["net_return"] if sample["status"] == "mature" else None,
                       "predicted": model_stats.predict_model(lookup[sample["horizon_days"]], sample["x"])})
    return result


def load_fit(run):
    fitted = {name: json.loads((run / (name + ".json")).read_text(encoding="utf-8"))
              for name in ("models", "cv_trials", "metrics", "acceptance", "ablations")}
    split_index = read_object(run / "splits.json")
    fitted["splits"] = {"policy": split_index["policy"],
                        "by_horizon": [read_object(research.owned_path(run, name)) for name in split_index["parts"]]}
    fitted["predictions"] = rows_read(run / "predictions")
    return fitted


def execute(root, plan, data_reference=None):
    root, base, _ = research.select_plan(root, plan)
    require(base is not None, "Prepare real fund data first")
    lock = research.owned_path(base, ".model-validation.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        return _execute(root, base.name, data_reference)
    finally:
        os.close(fd)
        lock.unlink()


def _execute(root, plan, data_reference=None):
    root, base, _ = research.select_plan(root, plan)
    require(base is not None, "Prepare real fund data first")
    data_run, data_audit, data_manifest, data_policy, nav, features = load_data(root, base, data_reference)
    stats, versions = runtime(root)
    current_code = {name: sha(SCRIPTS / name) for name in MODEL_FILES}
    latest = read_object(research.owned_path(base, "indexes/latest.json"))
    previous = latest.get("model_validation")
    if isinstance(previous, dict):
        prior_run, prior_manifest = resolve_model(root, base, None)
        if (prior_manifest["code_hashes"] == current_code and prior_manifest["dependencies"] == versions
                and prior_manifest["input_semantic_hash"] == data_manifest["semantic_fingerprint"]):
            return {"reused_completed_experiment": True, **verify(root, base.name, None)}
    now = dt.datetime.now(research.CN)
    relative = "research/" + now.strftime("%Y/%m/%d") + "/model-" + now.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run = research.owned_path(base, relative)
    run.mkdir(parents=True, exist_ok=False)
    stage = "sample_construction"
    experiment_path = None
    try:
        write_small_json(run / "account-protection.json", research.protection(root, base))
        original_index = research.file_hash(research.owned_path(base, "indexes/latest.json"))
        samples = scenarios.build_samples(nav, features, data_policy["code_info"], data_manifest["horizons"], scenarios.DEFAULT_SCENARIO)
        policy = stats.freeze_policy(samples, scenarios.FEATURE_NAMES)
        code_hashes = current_code
        frozen = {"stats": policy, "nominal_scenario": scenarios.DEFAULT_SCENARIO, "scenario_grid": scenarios.scenario_grid(),
                  "execution_budget": "one final test per frozen data/code/policy; no retuning after final outcomes",
                  "strict_historical_facts": False, "dependencies": versions, "code_hashes": code_hashes}
        experiment_id = digest({"data": data_manifest["semantic_fingerprint"], "policy": frozen})
        experiment_path = research.owned_path(base, "research/experiments/" + experiment_id + ".json")
        prior_experiment = read_object(experiment_path) if experiment_path.exists() else None
        if prior_experiment and prior_experiment.get("status") == "completed":
            return {"reused_completed_experiment": True, **verify(root, base.name, prior_experiment["run_path"])}
        write_small_json(experiment_path, {"experiment_id": experiment_id, "status": "running", "run_path": relative,
                         "frozen_policy_hash": digest(frozen), "recovery_of": prior_experiment,
                         "rule": "Repeat identical completed experiment by verification only; failures retain recovery lineage"})
        write_small_json(run / "frozen-policy.json", frozen)
        rows_write(run / "samples", samples)
        stage = "training_calibration_and_test"
        write_small_json(run / "model-status.json", {"stage": stage, "status": "running"})
        fitted = stats.fit_evaluate(samples, policy)
        for name in ("models", "cv_trials", "metrics", "acceptance", "ablations"):
            write_small_json(run / (name + ".json"), fitted[name])
        split_files = []
        for part in fitted["splits"]["by_horizon"]:
            path = "splits/horizon-" + str(part["horizon_days"]) + ".json"
            write_small_json(run / path, part)
            split_files.append(path)
        write_small_json(run / "splits.json", {"policy": fitted["splits"]["policy"], "parts": split_files})
        rows_write(run / "predictions", fitted["predictions"])
        trading_predictions = strategy_predictions(samples, fitted["models"], policy)
        rows_write(run / "strategy-predictions", trading_predictions)
        stage = "cost_and_settlement_replay"
        write_small_json(run / "model-status.json", {"stage": stage, "status": "running"})
        summaries = [save_replay(run, scenarios.replay(nav, samples, trading_predictions, {"scenario": value})) for value in frozen["scenario_grid"]]
        strategies = strategy_checks(summaries, policy)
        write_small_json(run / "strategy-acceptance.json", strategies)
        result = acceptance(data_audit, fitted, strategies, policy)
        write_small_json(run / "validation-result.json", result)
        stage = "artifact_registration"
        items = inventory(run)
        artifact_index = run / "artifact-hashes.jsonl"
        artifact_index.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")
        require(artifact_index.stat().st_size <= 16 * 1024 * 1024, "Model artifact index exceeds shard limit")
        manifest = {"schema_version": 1, "root": str(root), "plan_id": base.name, "run_path": relative,
                    "data_run": data_run.relative_to(base).as_posix(), "data_manifest_sha256": sha(data_run / "manifest.json"),
                    "code_hashes": code_hashes, "dependencies": versions, "artifact_index_sha256": sha(artifact_index), "artifact_hash": digest(items),
                    "input_data_hash": data_manifest["dataset_hash"], "input_semantic_hash": data_manifest["semantic_fingerprint"],
                    "policy_hash": digest(frozen), "actual_fit_executed": True}
        write_small_json(run / "model-manifest.json", manifest)
        stage = "independent_artifact_verification"
        verify(root, base.name, relative, recheck_replay=True)
        pointer = {"run_path": relative, "manifest_sha256": sha(run / "model-manifest.json"),
                   "result_sha256": sha(run / "validation-result.json"), "execution_status": "completed",
                   "candidate_status": result["candidate_status"], "live_prediction_allowed": result["live_prediction_allowed"],
                   "actual_fit_executed": True}
        write_small_json(run / "model-status.json", {"stage": "evaluated", **pointer})
        write_small_json(experiment_path, {"experiment_id": experiment_id, "status": "completed", "run_path": relative,
                                          "frozen_policy_hash": digest(frozen), "candidate_status": result["candidate_status"]})
        update_latest(root, base.name, {"model_validation": pointer}, expected_sha256=original_index)
        return {"run_path": relative, **result}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        write_small_json(run / "model-status.json", {"stage": stage, "status": "failed", "error": str(exc)})
        if experiment_path is not None:
            write_small_json(experiment_path, {"status": "failed", "stage": stage, "run_path": relative, "error": str(exc)})
        raise


def resolve_model(root, base, reference):
    pointer = None
    if reference is None:
        pointer = read_object(research.owned_path(base, "indexes/latest.json")).get("model_validation")
        require(isinstance(pointer, dict), "No model validation run registered")
        reference = pointer["run_path"]
    run = research.resolve_run(base, reference)
    manifest = read_object(run / "model-manifest.json")
    require(manifest["root"] == str(root) and manifest["plan_id"] == base.name and manifest["run_path"] == reference, "Model owner mismatch")
    if pointer is not None:
        require(pointer["manifest_sha256"] == sha(run / "model-manifest.json")
                and pointer["result_sha256"] == sha(run / "validation-result.json"), "Registered model reference changed")
    return run, manifest


def verify(root, plan, reference=None, recheck_replay=True):
    root, base, _ = research.select_plan(root, plan)
    require(base is not None, "No model plan")
    run, manifest = resolve_model(root, base, reference)
    stats, versions = runtime(root)
    require(versions == manifest["dependencies"], "Model dependency environment changed")
    require(manifest["code_hashes"] == {name: sha(SCRIPTS / name) for name in MODEL_FILES}, "Model source code changed")
    items = inventory(run)
    artifact_index = run / "artifact-hashes.jsonl"
    require(sha(artifact_index) == manifest["artifact_index_sha256"], "Model artifact index changed")
    expected = [json.loads(line) for line in artifact_index.read_text(encoding="utf-8").splitlines()]
    require(items == expected and digest(items) == manifest["artifact_hash"], "Model artifact inventory changed")
    data_run, data_audit, data_manifest, data_policy, nav, features = load_data(root, base, manifest["data_run"])
    require(sha(data_run / "manifest.json") == manifest["data_manifest_sha256"], "Source data lineage changed")
    frozen = read_object(run / "frozen-policy.json")
    require(digest(frozen) == manifest["policy_hash"], "Frozen experiment policy changed")
    samples = rows_read(run / "samples")
    rebuilt = scenarios.build_samples(nav, features, data_policy["code_info"], data_manifest["horizons"], frozen["nominal_scenario"])
    require(samples == rebuilt, "Conditional targets differ from source NAV and declared assumptions")
    require(frozen["stats"] == stats.freeze_policy(samples, scenarios.FEATURE_NAMES), "Statistical policy changed")
    fitted = load_fit(run)
    from model_verify import audit_fit
    audit_fit(samples, frozen["stats"], fitted)
    models, predictions = fitted["models"], fitted["predictions"]
    lookup = {(m["horizon_days"], m["target"]): m for m in models}
    sample_map = {r["id"]: r for r in samples}
    for row in predictions:
        sample, model = sample_map[row["sample_id"]], lookup[(row["horizon_days"], row["target"])]
        raw = model["intercept"] + math.fsum((sample["x"][i] - mean) / scale * coef for i, mean, scale, coef in
                zip(model["feature_indices"], model["scaler_mean"], model["scaler_scale"], model["coefficients"]))
        value = max(-1., raw) if row["target"] == "net_return" else min(1., max(0., raw))
        require(abs(value - row["predicted"]) <= 1e-10 and sample["y"][row["target"]] == row["actual"], "Saved prediction or outcome changed")
    metrics = stats.evaluate_predictions(predictions, frozen["stats"])
    require(metrics == json.loads((run / "metrics.json").read_text(encoding="utf-8")), "Held-out metrics changed")
    trading_predictions = rows_read(run / "strategy-predictions")
    require(trading_predictions == strategy_predictions(samples, models, frozen["stats"]), "Strategy forecast coverage changed")
    summaries = []
    for value in frozen["scenario_grid"]:
        folder = run / "replay" / scenarios.scenario_id(value)
        saved = read_replay(folder)
        if recheck_replay:
            require(saved == scenarios.replay(nav, samples, trading_predictions, {"scenario": value}), "Strategy replay differs from source and frozen decisions")
        summaries.append(read_object(folder / "summary.json"))
    strategies = strategy_checks(summaries, frozen["stats"])
    require(strategies == json.loads((run / "strategy-acceptance.json").read_text(encoding="utf-8")), "Strategy confidence results changed")
    fitted["metrics"] = metrics
    expected = acceptance(data_audit, fitted, strategies, frozen["stats"])
    require(expected == read_object(run / "validation-result.json"), "Acceptance result changed")
    return {"verification": "passed", "run_path": manifest["run_path"], **expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate-all", "train", "verify", "predict",
                                          "allocate", "allocation-validate", "allocation-verify"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--plan")
    parser.add_argument("--run")
    parser.add_argument("--data-run")
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--start", type=dt.date.fromisoformat)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.datetime.now(research.CN).date())
    parser.add_argument("--research", action="store_true")
    parser.add_argument("--request", type=Path, help="Frozen allocation/account/research protocol JSON")
    args = parser.parse_args()
    try:
        root = args.root if args.root is not None else default_root()
        if args.command in ("allocate", "allocation-validate", "allocation-verify"):
            require(args.research, "Current allocation data adapter is conditional research; explicitly use --research")
            from allocation_runner import execute as allocation_execute
            result = allocation_execute(args.command, root, args.plan, args.request, args.data_run, args.run)
            print(json.dumps(result, ensure_ascii=True, allow_nan=False))
            return 0
        if args.command == "validate-all":
            require(args.codes and args.start, "validate-all requires explicit --codes and --start")
            prepared = research.prepare(root, args.plan, args.codes, args.start, args.end, [30, 60, 90], 120)
            result = execute(root, prepared["plan_id"], prepared["run_path"])
        elif args.command == "train":
            result = execute(root, args.plan, args.data_run)
        else:
            result = verify(root, args.plan, args.run)
            if args.command == "predict":
                require(args.research or result["live_prediction_allowed"], "Model rejected for live recommendations; use --research only for diagnostics")
                selected_root, base, _ = research.select_plan(root, args.plan)
                run, manifest = resolve_model(selected_root, base, args.run)
                _, _, dm, _, _, features = load_data(selected_root, base, manifest["data_run"])
                stats, _ = runtime(selected_root)
                models = json.loads((run / "models.json").read_text(encoding="utf-8"))
                result["research_forecasts"] = [{"code": code, "cutoff_date": features[code][-1]["feature_cutoff_nav_date"],
                    "horizon_days": model["horizon_days"], "target": model["target"],
                    "conditional_prediction": stats.predict_model(model, [features[code][-1]["values"][key] for key in scenarios.FEATURE_NAMES])}
                    for code in dm["codes"] for model in models]
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0 if args.command in ("verify", "predict") or result["live_prediction_allowed"] else 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"execution_status": "failed", "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
