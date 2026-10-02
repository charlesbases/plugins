"""Bounded numerical IO and dependency identity, through the sole entry."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
import allocation_runtime as support


class AllocationRuntimeTests(unittest.TestCase):
    def test_rows_roundtrip_preserves_cross_month_order_and_input(self):
        rows = [{"decision_date": "2026-02-01", "value": 2}, {"decision_date": "2026-01-01", "value": 1}]
        original = copy.deepcopy(rows)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows"
            support.rows_write(path, rows)
            self.assertEqual(support.rows_read(path), rows)
            self.assertEqual(rows, original)
            shard = next(path.glob("2026-01*.jsonl"))
            record = json.loads(shard.read_text())
            record["_record_order"] = 0
            shard.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "order inventory"):
                support.rows_read(path)

    def test_shard_size_and_single_record_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [{"decision_date": "2026-01-01", "data": "x" * (9 * 1024 * 1024)} for _ in range(2)]
            path = Path(folder) / "rows"
            support.rows_write(path, rows)
            self.assertEqual(len(list(path.glob("*.jsonl"))), 2)
            self.assertTrue(all(p.stat().st_size <= 16 * 1024 * 1024 for p in path.glob("*.jsonl")))
            with self.assertRaisesRegex(ValueError, "exceeds shard"):
                support.rows_write(Path(folder) / "too-large", [{"data": "x" * (16 * 1024 * 1024)}])

    def test_satisfied_dependency_lock_needs_no_installation(self):
        expected = dict(line.split("==") for line in (SCRIPTS / "requirements-model.txt").read_text().splitlines() if line.strip())
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(support.importlib.metadata, "version", side_effect=lambda name: expected[name]), \
             patch.object(support.subprocess, "run") as install:
            self.assertEqual(support.ensure_runtime(Path(folder)), expected)
            install.assert_not_called()

    def test_unsupported_command_rejects_before_creating_data(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "investment.py"), "unsupported-operation",
                                     "--root", str(data)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid choice", result.stderr)
            self.assertFalse(data.exists())


if __name__ == "__main__":
    unittest.main()
