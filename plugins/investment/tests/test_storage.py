"""Isolated synthetic storage fixtures, never investment data."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/storage.py"
spec = importlib.util.spec_from_file_location("investment_storage", SCRIPT)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "investment"

    def test_empty_and_research_only_use_first_mode(self):
        self.assertEqual(storage.account_status(self.root)["mode"], "first_investment")
        self.assertFalse(self.root.exists())
        storage.write_small_json(self.root / "config.json", {"active_plan": "demo"})
        storage.write_small_json(self.root / "plans/demo/profile.json", {"purpose": "research"})
        storage.append_records(self.root, "demo", "market", "FUND/2026/10", [{"event_id": "quote-1", "nav": "1.0"}])
        self.assertEqual(storage.account_status(self.root)["mode"], "first_investment")
        self.assertEqual(storage.account_status(self.root, explicit_holdings=True)["mode"], "provided_holdings")

    def test_initialized_empty_portfolio_is_history(self):
        storage.write_small_json(self.root / "config.json", {"active_plan": "demo"})
        storage.write_small_json(self.root / "plans/demo/state/current.json", {"account_initialized": True, "holdings": []})
        self.assertEqual(storage.account_status(self.root)["mode"], "portfolio_review")

    def test_pending_order_prevents_duplicate_first_purchase(self):
        record = {"event_id": "order-1", "record_type": "order", "status": "submitted", "amount": 100}
        storage.append_records(self.root, "demo", "ledger", "2026/10", [record])
        self.assertEqual(storage.account_status(self.root, "demo")["mode"], "portfolio_review")

    def test_corrupt_records_and_missing_active_plan_fail_closed(self):
        storage.write_small_json(self.root / "config.json", {"active_plan": "missing"})
        with self.assertRaises(storage.StorageError):
            storage.account_status(self.root)
        shard = self.root / "plans/demo/ledger/2026/10/part-000001.jsonl"
        shard.parent.mkdir(parents=True)
        shard.write_text('{broken\n', encoding="utf-8")
        with self.assertRaises(storage.StorageError):
            storage.account_status(self.root, "demo")

    def test_broken_account_links_are_not_empty(self):
        original_exists, original_link = Path.exists, Path.is_symlink
        for field in ("root", "config", "plans", "selected_plan", "state", "current", "ledger"):
            with self.subTest(field=field):
                root = self.root / field
                if field == "root":
                    broken = root
                else:
                    root.mkdir(parents=True)
                    if field in ("config", "plans"):
                        broken = root / ("config.json" if field == "config" else "plans")
                    else:
                        storage.write_small_json(root / "config.json", {"active_plan": "demo"})
                        plan = root / "plans/demo"
                        plan.mkdir(parents=True)
                        broken = plan if field == "selected_plan" else plan / ("state/current.json" if field == "current" else field)
                        broken.parent.mkdir(parents=True, exist_ok=True)
                before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                # Model the filesystem contract without requiring Windows symlink privileges.
                with patch.object(Path, "exists", autospec=True, side_effect=lambda p: False if p == broken else original_exists(p)), patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: True if p == broken else original_link(p)):
                    for explicit_plan in (None, "demo") if field == "plans" else (None,):
                        with self.subTest(explicit_plan=explicit_plan):
                            with self.assertRaisesRegex(storage.StorageError, "Broken link"):
                                storage.account_status(root, explicit_plan)
                self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_broken_config_link_returns_structured_cli_error(self):
        self.root.mkdir()
        broken = self.root / "config.json"
        original_exists, original_link = Path.exists, Path.is_symlink
        with patch.object(Path, "exists", autospec=True, side_effect=lambda p: False if p == broken else original_exists(p)), patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: True if p == broken else original_link(p)), patch.object(sys, "argv", [str(SCRIPT), "status", "--root", str(self.root)]), patch("builtins.print") as output:
            self.assertEqual(storage.main(), 1)
        self.assertEqual(json.loads(output.call_args.args[0])["mode"], "data_unavailable")
        self.assertFalse(broken.exists())

    def test_invalid_account_directories_return_structured_cli_errors(self):
        for field in ("plans", "plans/demo", "plans/demo/state"):
            with self.subTest(field=field):
                root = self.root / field.replace("/", "-")
                storage.write_small_json(root / "config.json", {"active_plan": "demo"})
                directory = root / field
                directory.parent.mkdir(parents=True, exist_ok=True)
                directory.write_text("not a directory", encoding="utf-8")
                before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with self.assertRaises(storage.StorageError):
                    storage.account_status(root)
                result = subprocess.run([sys.executable, "-B", str(SCRIPT), "status", "--root", str(root)], text=True, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout)["mode"], "data_unavailable")
                self.assertEqual(result.stderr, "")
                self.assertEqual(before, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_rotation_preserves_records_and_retry_is_idempotent(self):
        records = [{"event_id": f"news-{i}", "text": "测试" * 8} for i in range(5)]
        result = storage.append_records(self.root, "demo", "news", "2026/10/02", records, max_bytes=180)
        shards = sorted(Path(result["partition"]).glob("part-*.jsonl"))
        self.assertGreater(len(shards), 1)
        self.assertTrue(all(p.stat().st_size <= 180 for p in shards))
        saved = [json.loads(line) for p in shards for line in p.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(saved, records)
        before = {p.name: p.read_bytes() for p in shards}
        self.assertEqual(storage.append_records(self.root, "demo", "news", "2026/10/02", records, max_bytes=180)["written"], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in shards})
        with self.assertRaises(storage.StorageError):
            storage.append_records(self.root, "demo", "news", "2026/10/02", [{"event_id": "news-0", "text": "different"}])

    def test_stored_conflicting_retry_preserves_all_shards(self):
        target = storage.partition_path(self.root, "demo", "ledger", "2026/10")
        target.mkdir(parents=True)
        old = {"event_id": "trade-1", "record_type": "trade", "status": "confirmed", "amount": 100}
        new = {**old, "event_id": "trade-2", "amount": 50}
        for duplicate_amount in (100, 200):
            with self.subTest(duplicate_amount=duplicate_amount):
                (target / "part-000001.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
                duplicate = {**old, "amount": duplicate_amount}
                (target / "part-000002.jsonl").write_text(json.dumps(duplicate) + "\n", encoding="utf-8")
                before = {p.name: p.read_bytes() for p in target.glob("*.jsonl")}
                if duplicate_amount == 200:
                    with self.assertRaises(storage.StorageError):
                        storage.append_records(self.root, "demo", "ledger", "2026/10", [new, old])
                    self.assertEqual(before, {p.name: p.read_bytes() for p in target.glob("*.jsonl")})
                else:
                    result = storage.append_records(self.root, "demo", "ledger", "2026/10", [new, old])
                    self.assertEqual((result["written"], result["skipped"]), (1, 1))
                self.assertFalse((target / ".write.lock").exists())

    def test_invalid_ledger_field_types_return_structured_cli_errors(self):
        record = {"event_id": "bad", "record_type": "trade", "status": "confirmed"}
        for field in ("record_type", "status"):
            for value in ([], {}, None, 1):
                for command in ("append", "status"):
                    with self.subTest(field=field, value=value, command=command):
                        bad = {**record, field: value}
                        root = self.root / (field + str(type(value).__name__) + command)
                        args = [sys.executable, "-B", str(SCRIPT), command, "--root", str(root), "--plan", "demo"]
                        if command == "status":
                            shard = root / "plans/demo/ledger/2026/10/part-000001.jsonl"
                            shard.parent.mkdir(parents=True)
                            shard.write_text(json.dumps(bad) + "\n", encoding="utf-8")
                            before = shard.read_bytes()
                        else:
                            args += ["--collection", "ledger", "--partition", "2026/10"]
                        result = subprocess.run(args, input=json.dumps([bad]), text=True, capture_output=True)
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(json.loads(result.stdout)["mode"], "data_unavailable")
                        self.assertEqual(result.stderr, "")
                        if command == "status":
                            self.assertEqual(shard.read_bytes(), before)
                        else:
                            self.assertFalse(root.exists())

    def test_invalid_paths_oversized_records_and_locks_do_not_write(self):
        with self.assertRaises(storage.StorageError):
            storage.append_records(self.root, "../escape", "ledger", "2026/10", [])
        with self.assertRaises(storage.StorageError):
            storage.append_records(self.root, "demo", "market", "../2026/10", [])
        with self.assertRaises(storage.StorageError):
            storage.append_records(self.root, "demo", "news", "2026/10/02", [{"event_id": "big", "text": "x" * 200}], max_bytes=100)
        self.assertFalse(self.root.exists())
        target = storage.partition_path(self.root, "demo", "news", "2026/10/02")
        target.mkdir(parents=True)
        lock = target / ".write.lock"
        lock.write_text("test-owner", encoding="utf-8")
        with self.assertRaises(storage.StorageError):
            storage.append_records(self.root, "demo", "news", "2026/10/02", [{"event_id": "locked"}])
        self.assertEqual(lock.read_text(encoding="utf-8"), "test-owner")

    def test_partial_tail_and_small_json_limit_preserve_old_data(self):
        target = storage.partition_path(self.root, "demo", "news", "2026/10/02")
        target.mkdir(parents=True)
        shard = target / "part-000001.jsonl"
        shard.write_text('{"event_id":"old"}', encoding="utf-8")
        before = shard.read_bytes()
        with self.assertRaises(storage.StorageError):
            storage.append_records(self.root, "demo", "news", "2026/10/02", [{"event_id": "new"}])
        self.assertEqual(shard.read_bytes(), before)
        state = self.root / "config.json"
        storage.write_small_json(state, {"active_plan": "demo"})
        before = state.read_bytes()
        with self.assertRaises(storage.StorageError):
            storage.write_small_json(state, {"large": "x" * storage.MAX_SMALL_BYTES})
        self.assertEqual(state.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
