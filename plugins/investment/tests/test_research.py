"""Bundled orchestration contracts with synthetic public-response fixtures."""

import datetime as dt
import hashlib
import importlib.util
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
    spec = importlib.util.spec_from_file_location("investment_research", SCRIPTS / "research.py")
    research = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(research)
finally:
    sys.path.remove(str(SCRIPTS))

CODE = "123456"
OTHER_CODE = "654321"
START = dt.date(2020, 1, 1)
END = START + dt.timedelta(days=124)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def tree_snapshot(root):
    if not root.exists():
        return None
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def synthetic_download(url, path, source_id, timeout, expected=None):
    if source_id.startswith("NAV-"):
        code = source_id[4:]
        nav, cumulative = [], []
        for i in range(125):
            day = START + dt.timedelta(days=i)
            stamp = int(dt.datetime.combine(day, dt.time(), research.CN).timestamp() * 1000)
            nav.append({"x": stamp, "y": 2.0, "unitMoney": "", "equityReturn": 0.0})
            cumulative.append([stamp, 3.0])
        text = ("var fS_name = \"Synthetic constant NAV fixture\";\n"
                "var fS_code = " + json.dumps(code) + ";\n"
                "var Data_netWorthTrend = " + json.dumps(nav) + ";\n"
                "var Data_ACWorthTrend = " + json.dumps(cumulative) + ";\n")
        data = text.encode("utf-8")
    elif source_id.startswith("TT-fees-"):
        data = b"<html><body>Synthetic present-day fee snapshot, never historical evidence.</body></html>"
    else:
        raise AssertionError("Synthetic unknown-code fixture must not request issuer documents")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
    actual = hashlib.sha256(data).hexdigest()
    return {"source_id": source_id, "url": url, "path": None, "bytes": len(data),
            "sha256": actual, "retrieved_at": dt.datetime.now(research.CN).isoformat(),
            "expected_sha256": expected, "review_status": "provider_snapshot"}


class ResearchOrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.root = self.temp / "investment"

    def account(self, holdings=False):
        base = self.root / "plans" / "demo"
        write_json(self.root / "config.json", {"schema_version": 1, "active_plan": "demo"})
        write_json(base / "profile.json", {"schema_version": 1, "platform": "天天基金"})
        if holdings:
            write_json(base / "state/current.json", {"account_initialized": True,
                                                     "holdings": [{"code": CODE, "shares": 10}],
                                                     "cash": 300})
            ledger = base / "ledger/2020/01/part-000001.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text('{"event_id":"old-trade","record_type":"trade","status":"confirmed"}\n', encoding="utf-8")
        index = {"schema_version": 1, "latest_report": "unchanged-existing-report",
                 "training_lifecycle": {"model_status": "validated", "model_id": "existing-champion",
                                        "last_successful_training_at": "2020-01-01T20:00:00+08:00"}}
        write_json(base / "indexes/latest.json", index)
        return base, index

    def prepare(self, root=None, codes=None, horizons=None):
        with patch.object(research, "download", side_effect=synthetic_download):
            return research.prepare(root or self.root, None, codes or [CODE], START, END,
                                    horizons or [1, 3], 120)

    def test_broken_root_link_is_not_bootstrapped(self):
        target = self.temp / "missing-target"
        original = Path.is_symlink
        # Windows may prohibit creating links; exercise the detected-link
        # branch without requiring administrative filesystem privileges.
        with patch.object(Path, "is_symlink", lambda p: p == self.root or original(p)):
            with self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(target.exists())
        self.assertFalse(self.root.exists())

    def test_sibling_plan_links_are_rejected_before_writes(self):
        base, _ = self.account()
        sibling = self.root / "plans/other"
        sibling.mkdir()
        for name in ("research", "indexes"):
            with self.subTest(name=name):
                original = base / name
                before = tree_snapshot(sibling)
                is_link = Path.is_symlink
                with patch.object(Path, "is_symlink", lambda p: p == original or is_link(p)):
                    with self.assertRaises(ValueError):
                        self.prepare()
                self.assertEqual(tree_snapshot(sibling), before)

    def test_registered_verification_rejects_changed_or_missing_saved_audit(self):
        result = self.prepare()
        base = self.root / "plans" / result["plan_id"]
        run = base / result["run_path"]
        self.assertEqual(research.verify_selected(self.root.resolve(), base.resolve(), run.resolve(), True)["readiness"]["market_research"], "passed")
        saved = (run / "audit.json").read_bytes()
        (run / "audit.json").write_bytes(b'{}')
        with self.assertRaises(ValueError):
            research.verify_selected(self.root.resolve(), base.resolve(), run.resolve(), True)
        (run / "audit.json").unlink()
        with self.assertRaises((ValueError, OSError)):
            research.verify_selected(self.root.resolve(), base.resolve(), run.resolve(), True)
        (run / "audit.json").write_bytes(saved)
        write_json(base / "state/current.json", {"account_initialized": True, "holdings": []})
        self.assertEqual(research.verify_selected(self.root.resolve(), base.resolve(), run.resolve(), True)["readiness"]["market_research"], "passed")

    def test_copied_run_cannot_claim_another_plan_identity(self):
        import shutil
        result = self.prepare()
        base = self.root / "plans" / result["plan_id"]
        run = base / result["run_path"]
        other = self.root / "plans/other"
        copied = other / result["run_path"]
        shutil.copytree(run, copied)
        with self.assertRaises(ValueError):
            research.verify_selected(self.root.resolve(), other.resolve(), copied.resolve())

    def test_fresh_root_bootstrap_creates_research_without_holdings(self):
        self.assertFalse(self.root.exists())
        result = self.prepare()
        base = self.root / "plans" / result["plan_id"]
        run = base / result["run_path"]
        self.assertEqual(result["account_mode"], "first_investment")
        self.assertEqual(result["totals"], {"NAV_rows": 125, "feature_rows": 5, "label_rows": 10,
                                          "mature_market_labels": 6, "pending_market_labels": 4,
                                          "strict_historical_investor_labels": 0})
        self.assertFalse((base / "state/current.json").exists())
        self.assertFalse((base / "ledger").exists())
        self.assertFalse(read_json(base / "profile.json")["account_initialized"])
        self.assertEqual(read_json(self.root / "config.json")["active_plan"], base.name)
        self.assertEqual(read_json(run / "audit.json")["readiness"]["historical_investor"], "data_unavailable")
        lifecycle = read_json(base / "indexes/latest.json")["training_lifecycle"]
        self.assertEqual(lifecycle["model_status"], "untrained")
        self.assertFalse(lifecycle["candidate_preparation"]["actual_fit_executed"])

    def test_existing_holdings_and_validated_model_are_preserved(self):
        base, old_index = self.account(holdings=True)
        protected = [self.root / "config.json", base / "profile.json", base / "state/current.json",
                     base / "ledger/2020/01/part-000001.jsonl"]
        before = {path: path.read_bytes() for path in protected}
        result = self.prepare()
        self.assertEqual(result["account_mode"], "portfolio_review")
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        index = read_json(base / "indexes/latest.json")
        self.assertEqual(index["latest_report"], old_index["latest_report"])
        for key, value in old_index["training_lifecycle"].items():
            self.assertEqual(index["training_lifecycle"][key], value)
        candidate = index["training_lifecycle"]["candidate_preparation"]
        self.assertEqual(candidate["preparation_state"], "data_unavailable")
        self.assertEqual(candidate["market_research"], "passed")
        self.assertFalse(candidate["actual_fit_executed"])

    def test_invalid_inputs_and_ambiguous_plans_do_not_write(self):
        invalid_cases = [(["bad"], START, END), ([CODE], END, START),
                         ([CODE], START, dt.datetime.now(research.CN).date() + dt.timedelta(days=1))]
        for i, (codes, start, end) in enumerate(invalid_cases):
            root = self.temp / f"invalid-{i}"
            with self.subTest(case=i), patch.object(research, "download") as download:
                with self.assertRaises(research.ResearchError):
                    research.prepare(root, None, codes, start, end, [1, 3], 120)
                download.assert_not_called()
                self.assertFalse(root.exists())
        for name in ("alpha", "bravo"):
            write_json(self.root / "plans" / name / "profile.json", {"purpose": "research"})
        before = tree_snapshot(self.root)
        with patch.object(research, "download") as download:
            with self.assertRaisesRegex(research.ResearchError, "needs_plan_selection"):
                research.prepare(self.root, None, [CODE], START, END, [1, 3], 120)
            download.assert_not_called()
        self.assertEqual(tree_snapshot(self.root), before)

    def test_installation_root_rejection_leaves_no_rejected_writes(self):
        plugin = self.temp / "fake-plugin"
        scripts = plugin / "skills/investment/scripts"
        scripts.mkdir(parents=True)
        rejected = plugin / "rejected-research-data"
        before = tree_snapshot(plugin)
        with patch.object(research, "SCRIPT_DIR", scripts), patch.object(research, "download") as download:
            with self.assertRaisesRegex(research.ResearchError, "plugin installation"):
                research.prepare(rejected, None, [CODE], START, END, [1, 3], 120)
            download.assert_not_called()
        self.assertEqual(tree_snapshot(plugin), before)
        self.assertFalse(rejected.exists())

    def test_failed_collection_keeps_prior_index_and_records_failure(self):
        base, _ = self.account()
        index_path = base / "indexes/latest.json"
        before = index_path.read_bytes()
        with patch.object(research, "download", side_effect=OSError("synthetic network failure")):
            with self.assertRaisesRegex(OSError, "synthetic network failure"):
                research.prepare(self.root, None, [CODE], START, END, [1, 3], 120)
        self.assertEqual(index_path.read_bytes(), before)
        statuses = list((base / "research").rglob("run-status.json"))
        self.assertEqual(len(statuses), 1)
        status = read_json(statuses[0])
        self.assertEqual((status["status"], status["stage"]), ("failed", "collect"))
        self.assertFalse(status["published_as_success"])
        self.assertEqual(read_json(statuses[0].parent / "source-manifest.json")[0]["review_status"], "unavailable")
        self.assertFalse((base / ".research-prepare.lock").exists())

    def test_changed_saved_audit_readiness_is_rejected_before_publication(self):
        result = self.prepare()
        base = self.root / "plans" / result["plan_id"]
        run = base / result["run_path"]
        index_path = base / "indexes/latest.json"
        before = index_path.read_bytes()
        saved = read_json(run / "audit.json")
        saved["readiness"]["historical_investor"] = "passed"
        write_json(run / "audit.json", saved)
        with self.assertRaisesRegex(research.ResearchError, "Saved audit is not bound"):
            research.publish(run, self.root.resolve(), base.resolve(), research.file_hash(index_path))
        self.assertEqual(index_path.read_bytes(), before)

    def test_publication_metadata_failure_leaves_prior_index_unchanged(self):
        base, _ = self.account()
        index_path = base / "indexes/latest.json"
        before = index_path.read_bytes()
        actual_write = research.write_small_json

        def fail_publication(path, value):
            if path.name == "publication.json":
                raise OSError("synthetic publication metadata failure")
            return actual_write(path, value)

        with patch.object(research, "download", side_effect=synthetic_download), patch.object(research, "write_small_json", side_effect=fail_publication):
            with self.assertRaisesRegex(OSError, "synthetic publication metadata failure"):
                research.prepare(self.root, None, [CODE], START, END, [1, 3], 120)
        self.assertEqual(index_path.read_bytes(), before)
        statuses = list((base / "research").rglob("run-status.json"))
        self.assertEqual(len(statuses), 1)
        status = read_json(statuses[0])
        self.assertEqual((status["status"], status["stage"]), ("failed", "publish"))
        self.assertFalse(status["published_as_success"])
        self.assertTrue((statuses[0].parent / "audit.json").is_file())

    def test_nonowned_run_collision_does_not_overwrite_existing_artifacts(self):
        base, _ = self.account()
        fixed = dt.datetime(2026, 1, 15, 20, tzinfo=research.CN)
        actual_datetime = dt.datetime

        class FrozenDateTime(actual_datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

        run = base / "research/2026/01/15/nav-20260115T200000-12345678"
        write_json(run / "run-status.json", {"status": "existing-owned-run"})
        (run / "existing.txt").write_text("never overwrite this run", encoding="utf-8")
        before = tree_snapshot(run)
        with patch.object(research.dt, "datetime", FrozenDateTime), patch.object(research.uuid, "uuid4", return_value=types.SimpleNamespace(hex="12345678" + "a" * 24)), patch.object(research, "download") as download:
            with self.assertRaises(FileExistsError):
                research.prepare(self.root, None, [CODE], START, END, [1, 3], 120)
            download.assert_not_called()
        self.assertEqual(tree_snapshot(run), before)
        self.assertFalse((base / ".research-prepare.lock").exists())

    def test_sorted_codes_and_horizons_have_same_semantic_fingerprint(self):
        first = self.prepare(self.temp / "first", [OTHER_CODE, CODE], [3, 1])
        second = self.prepare(self.temp / "second", [CODE, OTHER_CODE], [1, 3])
        manifests = []
        for directory, result in (("first", first), ("second", second)):
            base = self.temp / directory / "plans" / result["plan_id"]
            manifest = read_json(base / result["run_path"] / "manifest.json")
            self.assertEqual(manifest["codes"], [CODE, OTHER_CODE])
            self.assertEqual(manifest["horizons"], [1, 3])
            self.assertEqual(result["totals"]["mature_market_labels"], 12)
            self.assertEqual(result["totals"]["pending_market_labels"], 8)
            manifests.append(manifest)
        self.assertEqual(manifests[0]["semantic_fingerprint"], manifests[1]["semantic_fingerprint"])


if __name__ == "__main__":
    unittest.main()
