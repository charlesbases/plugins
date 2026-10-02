"""Shared latest-index update contracts; isolated, synthetic plan records."""

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/storage.py"
spec = importlib.util.spec_from_file_location("investment_index_storage", SCRIPT)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class IndexUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "investment"
        self.index = self.root / "plans/demo/indexes/latest.json"

    def seed(self):
        return storage.update_latest(self.root, "demo", {
            "schema_version": 1, "latest_news": "news-original", "latest_report": "report-original",
            "training_lifecycle": {"model_status": "validated", "model_id": "champion"}}, None)

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() if p.is_file() else None
                for p in self.root.rglob("*")}

    def test_merge_preserves_other_namespaces_and_is_shallow(self):
        original = self.seed()
        merged = storage.update_latest(self.root, "demo", {"latest_report": "report-new"})
        self.assertEqual(merged, {**original, "latest_report": "report-new"})
        replacement = {"model_status": "untrained"}
        merged = storage.update_latest(self.root, "demo", {"training_lifecycle": replacement})
        self.assertEqual(merged["training_lifecycle"], replacement)
        self.assertEqual(merged["latest_news"], "news-original")
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8")), merged)
        self.assertFalse((self.index.parent / ".latest-write.lock").exists())

    def test_stale_hash_and_expected_absent_do_not_change_index(self):
        self.seed()
        old_hash = hashlib.sha256(self.index.read_bytes()).hexdigest()
        storage.update_latest(self.root, "demo", {"latest_news": "news-concurrent"})
        before = self.index.read_bytes()
        for expected in (old_hash, None, "not-a-sha256"):
            with self.subTest(expected=expected), self.assertRaises(storage.StorageError):
                storage.update_latest(self.root, "demo", {"latest_report": "must-not-publish"}, expected)
            self.assertEqual(self.index.read_bytes(), before)
            self.assertFalse((self.index.parent / ".latest-write.lock").exists())

    def test_existing_shared_lock_is_not_cleared_or_overwritten(self):
        self.seed()
        lock = self.index.parent / ".latest-write.lock"
        lock.write_bytes(b"another-writer-owns-this-lock")
        before = self.index.read_bytes()
        with self.assertRaisesRegex(storage.StorageError, "locked"):
            storage.update_latest(self.root, "demo", {"latest_report": "must-not-publish"})
        self.assertEqual(lock.read_bytes(), b"another-writer-owns-this-lock")
        self.assertEqual(self.index.read_bytes(), before)

    def test_interleaved_report_and_news_updates_preserve_model_namespace(self):
        original = self.seed()
        actual_owner_check = storage._owned_index_path
        pending = True

        def interleave(root, plan):
            nonlocal pending
            if pending:
                pending = False
                storage.update_latest(root, plan, {"latest_news": "news-between-report-snapshot-and-update"})
            return actual_owner_check(root, plan)

        with patch.object(storage, "_owned_index_path", side_effect=interleave):
            merged = storage.update_latest(self.root, "demo", {"latest_report": "report-after-news-update"})
        self.assertEqual(merged["latest_news"], "news-between-report-snapshot-and-update")
        self.assertEqual(merged["latest_report"], "report-after-news-update")
        self.assertEqual(merged["training_lifecycle"], original["training_lifecycle"])

    def test_sibling_plan_index_aliases_are_rejected_before_writes(self):
        self.seed()
        storage.update_latest(self.root, "sibling", {"latest_news": "sibling-private"}, None)
        sibling = self.root / "plans/sibling"
        base = self.root / "plans/demo"
        original_resolve = Path.resolve
        for alias, target in ((base, sibling), (base / "indexes", sibling / "indexes"),
                              (self.index, sibling / "indexes/latest.json")):
            before = self.snapshot()
            resolved_target = original_resolve(target)

            def model_alias(path, *args, **kwargs):
                return resolved_target if path == alias else original_resolve(path, *args, **kwargs)

            # Exercise alias semantics without requiring Windows symlink privileges.
            with self.subTest(alias=alias), patch.object(Path, "resolve", autospec=True, side_effect=model_alias):
                with self.assertRaises(storage.StorageError):
                    storage.update_latest(self.root, "demo", {"latest_report": "wrong-owner"})
            self.assertEqual(self.snapshot(), before)

    def test_broken_root_and_plan_aliases_do_not_create_directories(self):
        broken_root = self.root.parent / "broken-root"
        original_link = Path.is_symlink
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == broken_root or original_link(p)):
            with self.assertRaisesRegex(storage.StorageError, "Broken link"):
                storage.update_latest(broken_root, "demo", {"latest_news": "never-written"})
        self.assertFalse(broken_root.exists())
        self.seed()
        base = self.root / "plans/demo"
        before = self.snapshot()
        original_exists = Path.exists
        with patch.object(Path, "exists", autospec=True, side_effect=lambda p: False if p == base else original_exists(p)), patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == base or original_link(p)):
            with self.assertRaisesRegex(storage.StorageError, "Broken link"):
                storage.update_latest(self.root, "demo", {"latest_news": "never-written"})
        self.assertEqual(self.snapshot(), before)

    def test_direct_latest_writes_are_refused_and_other_small_files_work(self):
        with self.assertRaisesRegex(storage.StorageError, "update_latest"):
            storage.write_small_json(self.index, {"latest_report": "unsynchronized"})
        self.assertFalse(self.root.exists())
        other = self.root / "config.json"
        storage.write_small_json(other, {"active_plan": "demo"})
        self.assertEqual(json.loads(other.read_text(encoding="utf-8")), {"active_plan": "demo"})

    def test_nonfinite_and_oversized_updates_leave_index_unchanged(self):
        self.seed()
        before = self.index.read_bytes()
        for invalid in ({"value": float("nan")}, {"value": "x" * storage.MAX_SMALL_BYTES}, []):
            with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(storage.StorageError):
                storage.update_latest(self.root, "demo", invalid)
            self.assertEqual(self.index.read_bytes(), before)
            self.assertFalse((self.index.parent / ".latest-write.lock").exists())

    def test_update_index_cli_requires_plan_and_supports_snapshot_conditions(self):
        command = [sys.executable] + (["-O"] if sys.flags.optimize else []) + ["-B", str(SCRIPT), "update-index", "--root", str(self.root)]
        missing = subprocess.run(command, input='{"latest_news":"never-written"}', text=True, capture_output=True)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("requires --plan", json.loads(missing.stdout)["error"])
        self.assertFalse(self.root.exists())
        created = subprocess.run(command + ["--plan", "demo", "--expected-index-sha256", "absent"],
                                 input='{"schema_version":1,"latest_news":"cli-news"}', text=True, capture_output=True)
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        self.assertEqual(json.loads(created.stdout), {"schema_version": 1, "latest_news": "cli-news"})
        expected = hashlib.sha256(self.index.read_bytes()).hexdigest()
        updated = subprocess.run(command + ["--plan", "demo", "--expected-index-sha256", expected],
                                 input='{"latest_report":"cli-report"}', text=True, capture_output=True)
        self.assertEqual(updated.returncode, 0, updated.stderr + updated.stdout)
        self.assertEqual(json.loads(updated.stdout)["latest_news"], "cli-news")
        before = self.index.read_bytes()
        stale = subprocess.run(command + ["--plan", "demo", "--expected-index-sha256", expected],
                               input='{"latest_news":"stale-cli-write"}', text=True, capture_output=True)
        self.assertEqual(stale.returncode, 1)
        self.assertEqual(self.index.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
