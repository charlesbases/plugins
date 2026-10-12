"""SC01指定契约与恢复边界测试；每个方法使用全新实际运行根。"""

import gc
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from investment.runtime.paths import test_parent, validate_root

PLUGIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sc01_acceptance_input", PLUGIN / "scripts" / "verify_sc01.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)


class RealFlowCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sc01-unit-", dir=test_parent())
        self.root = validate_root(Path(self.temporary.name), fixture_mode=True)
        self.invocations = []

    def tearDown(self):
        gc.collect()
        self.temporary.cleanup()

    def execute(self, changes, name="step", success=True, replies=None, prepare=False):
        folder, incoming, result = harness.execute(self.root, "test-account", changes, name, self.invocations, success, replies, prepare)
        record = {"folder": folder, "input": incoming, "result": result}
        if success and not prepare:
            record["state"] = json.loads((folder / f"attempt-{result['attempt_no']:02}" / "state.json").read_bytes())
        return record

    def ledger(self):
        return harness.current(self.root, "test-account")

    def validate_output(self, record, candidate_path=None):
        number = record["result"]["attempt_no"]
        candidate_path = candidate_path or record["folder"] / f"attempt-{number:02}" / "candidate.json"
        return subprocess.run([sys.executable, "-B", str(harness.VALIDATOR), "--root", str(self.root), "--input", str(record["folder"] / "input.json"), "--phase", "output", "--candidate", str(candidate_path), "--run-id", record["input"]["run_id"], "--invocation-id", record["input"]["invocation_id"], "--attempt-no", str(number), "--fixture-mode"], cwd=PLUGIN, env=dict(os.environ, PYTHONPATH=str(PLUGIN / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8")

    def candidate_counterexample(self, record, label, tables=None, context_changes=None, candidate_changes=None):
        directory = record["folder"] / f"attempt-{record['result']['attempt_no']:02}"
        candidate = json.loads((directory / "candidate.json").read_bytes())
        context = json.loads((directory / "context.json").read_bytes())
        registry = json.loads((harness.SPEC / "registry.json").read_text(encoding="utf-8"))
        prefix = f"investment-run://sc01/{directory.parent.parent.name}/{record['input']['invocation_id']}/{directory.name}/"
        for name, table in (tables or {}).items():
            filename = label + "-" + name + ".json"
            (directory / filename).write_bytes(harness.encode(table))
            reference = harness.ref(table, "investment." + name, prefix + filename)
            spec = next(t for t in registry["tables"] if t["schema_id"] == reference["schema_id"])
            reference.update(table_schema_ref=harness.ref(registry, "investment.registry", "investment-spec://v1/registry.json"), row_count=len(table["rows"]), **{k: spec[k] for k in ("primary_key", "units", "null_policy")})
            context[name + "_ref"] = reference
        context.update(context_changes or {})
        filename = label + "-context.json"
        (directory / filename).write_bytes(harness.encode(context))
        candidate["payload_ref"] = harness.ref(context, "investment.transaction_context", prefix + filename)
        candidate["datasets"] = [context[k + "_ref"] for k in ("trades", "positions", "pending_transactions")]
        candidate.update(candidate_changes or {})
        path = directory / (label + "-candidate.json")
        path.write_bytes(harness.encode(candidate))
        return path

    def first(self):
        return self.execute([], "first")

    def manifest(self):
        path = next((self.root / "runs" / "sc01").glob("*/manifest.json"))
        return path, json.loads(path.read_text(encoding="utf-8"))
