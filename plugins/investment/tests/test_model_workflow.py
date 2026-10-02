"""Synthetic workflow guards; no downloads or real-data model fitting."""

import copy
import importlib.util
import itertools
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    spec = importlib.util.spec_from_file_location("investment_model_workflow", SCRIPTS / "model.py")
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)
finally:
    sys.path.remove(str(SCRIPTS))

TARGETS = ("net_return", "terminal_loss", "principal_path_loss", "peak_drawdown")
HORIZONS = (30, 60, 90)
POLICY = {"horizons": list(HORIZONS), "targets": list(TARGETS), "cv_folds": [{}, {}, {}],
          "alphas": [.0001, .001, .01], "l1_ratios": [.1, .5, .9], "strict_PIT_verified": True}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def complete_evidence():
    models = [{"horizon_days": h, "target": target} for h, target in itertools.product(HORIZONS, TARGETS)]
    metrics = [{"horizon_days": h, "target": target, "split": "test", "rows": 100,
                "gains": {name: {"status": "passed"} for name in ("baseline_mean", "zero")},
                "intervals": {level: {"status": "passed"} for level in ("80", "90")}}
               for h, target in itertools.product(HORIZONS, TARGETS)]
    acceptance = [{"horizon_days": h, "target": target, "split": "test", "execution_status": "completed",
                   "mean_advantage": {name: "passed" for name in ("baseline_mean", "zero")},
                   "intervals": {level: "passed" for level in ("80", "90")},
                   "investor_qualification": "passed"}
                  for h, target in itertools.product(HORIZONS, TARGETS)]
    trials = [{"horizon_days": h, "target": target, "fold": fold, "alpha": alpha,
               "l1_ratio": ratio, "validation_mse": .01}
              for h, target, fold, alpha, ratio in itertools.product(HORIZONS, TARGETS, range(3),
                                                                    (.0001, .001, .01), (.1, .5, .9))]
    strategy = [{"scenario_id": model.scenarios.scenario_id(scenario), "horizon_days": h,
                 "execution_status": "completed", "acceptance_status": "passed", "paired_blocks": 10,
                 "paired_mean_gain": .02, "gain_ci95": [.01, .03], "beats_all_buy_hold": True}
                for scenario, h in itertools.product(model.scenarios.scenario_grid(), HORIZONS)]
    audit = {"readiness": {"market_research": "passed", "historical_investor": "passed", "strict_PIT": "verified"},
             "missing_blockers": []}
    return audit, {"models": models, "cv_trials": trials, "metrics": metrics, "acceptance": acceptance}, strategy


class ModelWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "investment"
        self.base = self.root / "plans" / "demo"

    def registered_run(self):
        reference = "research/2024/01/01/model-fixture"
        run = self.base / reference
        write_json(run / "validation-result.json", {"live_prediction_allowed": False})
        items = model.inventory(run)
        index = run / "artifact-hashes.jsonl"
        index.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
        manifest = {"root": str(self.root), "plan_id": self.base.name, "run_path": reference,
                    "dependencies": {"synthetic-runtime": "1"}, "code_hashes": {},
                    "artifact_index_sha256": model.sha(index), "artifact_hash": model.digest(items)}
        write_json(run / "model-manifest.json", manifest)
        write_json(self.base / "indexes/latest.json", {"model_validation": {
            "run_path": reference, "manifest_sha256": model.sha(run / "model-manifest.json"),
            "result_sha256": model.sha(run / "validation-result.json")}})
        return run, reference, manifest

    def assert_not_live(self, audit, fitted, strategies):
        try:
            result = model.acceptance(audit, fitted, strategies, POLICY)
        except ValueError:
            return  # Explicit rejection also satisfies the closed eligibility boundary.
        self.assertFalse(result["live_prediction_allowed"])
        self.assertNotEqual(result["candidate_status"], "accepted")

    def test_rows_roundtrip_preserves_cross_month_producer_order(self):
        records = [{"decision_date": "2024-02-02", "id": "first"},
                   {"decision_date": "2024-01-03", "id": "second"},
                   {"decision_date": "2024-02-01", "id": "third"}]
        folder = self.root / "records"
        model.rows_write(folder, records)
        self.assertEqual(model.rows_read(folder), records)
        self.assertTrue(all("_record_order" not in r for r in records))
        shard = sorted(folder.glob("*.jsonl"))[0]
        stored = [json.loads(line) for line in shard.read_text().splitlines()]
        stored[0]["_record_order"] = 0
        shard.write_text("".join(json.dumps(r) + "\n" for r in stored), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "order inventory"):
            model.rows_read(folder)

    def test_rows_shard_by_size_and_reject_oversized_record(self):
        payload = "x" * (9 * 1024 * 1024)
        records = [{"decision_date": "2024-01-01", "payload": payload, "id": i} for i in (1, 2)]
        folder = self.root / "bounded"
        model.rows_write(folder, records)
        shards = sorted(folder.glob("*.jsonl"))
        self.assertEqual(len(shards), 2)
        self.assertTrue(all(path.stat().st_size <= 16 * 1024 * 1024 for path in shards))
        self.assertEqual(model.rows_read(folder), records)
        with self.assertRaisesRegex(ValueError, "exceeds shard size"):
            model.rows_write(self.root / "oversized", [{"decision_date": "2024-01-01",
                                                        "payload": "x" * (16 * 1024 * 1024)}])

    def test_registered_manifest_and_result_tampering_are_rejected(self):
        run, _, manifest = self.registered_run()
        self.assertEqual(model.resolve_model(self.root, self.base, None)[0], run)
        manifest_path, result_path = run / "model-manifest.json", run / "validation-result.json"
        for path in (manifest_path, result_path):
            original = path.read_bytes()
            value = json.loads(original)
            value["unexpected_rewrite"] = True
            write_json(path, value)
            with self.subTest(file=path.name), self.assertRaisesRegex(ValueError, "Registered model reference changed"):
                model.resolve_model(self.root, self.base, None)
            path.write_bytes(original)
        self.assertEqual(model.resolve_model(self.root, self.base, None)[1], manifest)

    def test_source_root_and_plan_mismatches_are_rejected(self):
        run, reference, manifest = self.registered_run()
        for field, invalid in (("root", str(self.root.parent / "other")), ("plan_id", "other-plan")):
            changed = dict(manifest, **{field: invalid})
            write_json(run / "model-manifest.json", changed)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "owner mismatch"):
                model.resolve_model(self.root, self.base, reference)
        write_json(run / "model-manifest.json", manifest)
        with self.assertRaises(ValueError):
            model.resolve_model(self.root, self.base, "../outside")

    def test_artifact_index_tampering_fails_before_source_data_access(self):
        run, reference, _ = self.registered_run()
        with (run / "artifact-hashes.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("{}\n")
        with patch.object(model.research, "select_plan", return_value=(self.root, self.base, {})), \
             patch.object(model, "runtime", return_value=(types.SimpleNamespace(), {"synthetic-runtime": "1"})), \
             patch.object(model, "MODEL_FILES", ()), patch.object(model, "load_data") as load:
            with self.assertRaisesRegex(ValueError, "artifact index changed"):
                model.verify(self.root, "demo", reference)
            load.assert_not_called()

    def test_artifact_content_change_fails_even_without_registered_pointer(self):
        run, reference, _ = self.registered_run()
        write_json(run / "validation-result.json", {"live_prediction_allowed": True})
        with patch.object(model.research, "select_plan", return_value=(self.root, self.base, {})), \
             patch.object(model, "runtime", return_value=(types.SimpleNamespace(), {"synthetic-runtime": "1"})), \
             patch.object(model, "MODEL_FILES", ()), patch.object(model, "load_data") as load:
            with self.assertRaisesRegex(ValueError, "artifact inventory changed"):
                model.verify(self.root, "demo", reference)
            load.assert_not_called()

    def test_empty_or_incomplete_evidence_cannot_enable_live_predictions(self):
        audit, fitted, strategies = complete_evidence()
        self.assertTrue(model.acceptance(audit, fitted, strategies, POLICY)["live_prediction_allowed"])
        for field in ("models", "cv_trials", "metrics"):
            for remaining in ([], fitted[field][:-1]):
                changed = copy.deepcopy(fitted)
                changed[field] = remaining
                with self.subTest(field=field, count=len(remaining)):
                    self.assert_not_live(audit, changed, strategies)

    def test_cached_pass_flags_cannot_override_failed_computed_metrics(self):
        audit, fitted, strategies = complete_evidence()
        fitted["metrics"][0]["gains"]["baseline_mean"]["status"] = "failed"
        self.assert_not_live(audit, fitted, strategies)
        audit, fitted, strategies = complete_evidence()
        fitted["metrics"][0]["intervals"]["90"]["status"] = "insufficient_evidence"
        self.assert_not_live(audit, fitted, strategies)

    def test_pending_strategy_predictions_preserve_unknown_outcomes(self):
        rows = [{"id": "mature", "code": "123456", "fund_group_id": "portfolio-a", "horizon_days": 30,
                 "decision_date": "2024-02-01", "cutoff_date": "2024-01-30", "x": [.2, 0., 0., 0., 0.],
                 "status": "mature", "y": {target: .1 for target in TARGETS}},
                {"id": "pending", "code": "123456", "fund_group_id": "portfolio-a", "horizon_days": 30,
                 "decision_date": "2024-03-01", "cutoff_date": "2024-02-28", "x": [.3, 0., 0., 0., 0.],
                 "status": "pending", "y": None}]
        portable = {"horizon_days": 30, "target": "net_return", "feature_indices": [0],
                    "scaler_mean": [0.], "scaler_scale": [1.], "coefficients": [2.], "intercept": .01}
        rows.insert(0, dict(rows[0], id="before-test", cutoff_date="2024-01-01", decision_date="2024-01-03"))
        policy = {"test_start": "2024-02-01", "horizons": [30]}
        with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            predicted = model.strategy_predictions(rows, [portable], policy)
        self.assertNotIn("before-test", [r["sample_id"] for r in predicted])
        pending = next(r for r in predicted if r["sample_id"] == "pending")
        self.assertAlmostEqual(pending["predicted"], .61)
        self.assertIsNone(pending["actual"])
        self.assertEqual(pending["decision_date"], "2024-03-01")
        changed = copy.deepcopy(rows)
        for row in changed:
            row["y"] = {target: .9 for target in TARGETS}
        with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            repeated = model.strategy_predictions(changed, [portable], policy)
        self.assertEqual([r["predicted"] for r in predicted], [r["predicted"] for r in repeated])
        self.assertIsNone(next(r for r in repeated if r["sample_id"] == "pending")["actual"])


if __name__ == "__main__":
    unittest.main()
