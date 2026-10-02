"""Internal research contracts with synthetic public-source fixtures."""

import datetime as dt
import copy
import hashlib
import gzip
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
import research
import source_fetch
from contracts import fingerprint
from artifacts import Artifacts
from state_store import Store
from file_io import read_object
import fund_universe

CODE, OTHER_CODE = "123456", "654321"
START = dt.date(2020, 1, 1)
END = START + dt.timedelta(days=124)
SYNTHETIC_CODES = [CODE, OTHER_CODE]


def synthetic_capture(url, registry_source_id, **kwargs):
    if registry_source_id == "eastmoney_catalog":
        text = 'var r = ' + json.dumps([[code, 'FIXTURE', 'Synthetic constant NAV fixture', '指数型-其他', 'FIXTURE']
                                      for code in SYNTHETIC_CODES]) + ';'
        media = "application/javascript"
    elif registry_source_id == "eastmoney_profile":
        code = Path(url).stem.split('_')[1]
        text = ('<table><tr><th>基金全称</th><td>Synthetic legal gold fund</td><th>基金简称</th><td>Synthetic constant NAV fixture</td></tr>'
                '<tr><th>基金代码</th><td>' + code + '</td><th>基金类型</th><td>指数型-其他</td></tr>'
                '<tr><th>基金管理人</th><td><a href="//fund.eastmoney.com/company/12345.html">Fixture manager</a></td>'
                '<th>基金经理人</th><td><a href="//fund.eastmoney.com/manager/45678.html">Fixture person</a></td></tr>'
                '<tr><th>交易币种</th><td>人民币</td><th>交易场所</th><td>场外</td></tr>'
                '<tr><th>跟踪标的</th><td>黄金</td><th>业绩比较基准</th><td>Gold</td></tr></table>')
        media = 'text/html'
    elif registry_source_id == "eastmoney_actions":
        code = Path(url).stem.split('_')[1]
        text = ('<script>var strbzdm="' + code + '";</script><table><tr><th>年份</th><th>权益登记日</th><th>除息日</th>'
                '<th>每10份分红</th><th>分红发放日</th></tr><tr><td>暂无分红信息!</td></tr></table>'
                '<table><tr><th>年份</th><th>拆分折算日</th><th>拆分类型</th><th>拆分折算比例</th></tr>'
                '<tr><td>暂无拆分信息!</td></tr></table>')
        media = 'text/html'
    elif registry_source_id == "eastmoney_nav":
        code = Path(url).stem
        nav, cumulative = [], []
        for i in range(125):
            day = START + dt.timedelta(days=i)
            stamp = int(dt.datetime.combine(day, dt.time(), research.CN).timestamp() * 1000)
            nav.append({"x": stamp, "y": 2.0, "unitMoney": "", "equityReturn": 0.0})
            cumulative.append([stamp, 3.0])
        text = ('var fS_name = "Synthetic constant NAV fixture";\nvar fS_code = ' + json.dumps(code)
                + ';\nvar Data_netWorthTrend = ' + json.dumps(nav)
                + ';\nvar Data_ACWorthTrend = ' + json.dumps(cumulative) + ';\n')
        media = "application/javascript"
    else:
        text = "<html><body>Synthetic present-day fee capture, not historical platform evidence.</body></html>"
        media = "text/html"
    data = text.encode("utf-8")
    return {"registry_source_id": registry_source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": media, "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
            "retrieved_at": dt.datetime.now(research.CN).isoformat(), "bytes": len(data),
            "raw_sha256": hashlib.sha256(data).hexdigest(), "text_sha256": hashlib.sha256(data).hexdigest(),
            "raw_bytes": data, "text": text}


class ResearchServiceTests(unittest.TestCase):
    def test_whole_51_code_domain_has_one_audited_manifest(self):
        codes = [str(index).zfill(6) for index in range(1, 52)]
        with patch.dict(globals(), {"SYNTHETIC_CODES": codes}), patch.object(source_fetch, "fetch", side_effect=synthetic_capture):
            result = self.raw_prepare(self.root, "demo", list(reversed(codes)), START, END, [1, 3], 120, operation_id="full-domain")
        manifest = read_object(self.run_path(result) / "manifest.json")
        progress = read_object(self.run_path(result) / "collection-progress.json")
        self.assertEqual(manifest["codes"], codes)
        self.assertEqual(len(manifest["funds"]), 51)
        self.assertEqual(progress["completed_codes"], codes)
        self.assertEqual(progress["pending_codes"], [])
        self.assertEqual(progress["status"], "collection_completed")
        research.verify_selected(self.root.resolve(), self.store.base, self.run_path(result))

    def test_acquisition_budget_preserves_raw_sources_for_same_operation_resume(self):
        with patch.object(source_fetch, "fetch", side_effect=synthetic_capture), patch.object(research, "MAX_COLLECTION_SOURCE_REQUESTS", 2):
            with self.assertRaisesRegex(research.ResearchEvidenceGap, "exhausted"):
                self.raw_prepare(self.root, "demo", [CODE], START, END, [1, 3], 120, operation_id="budget-resume")
        captured = list(self.store.scan("research_source_capture"))
        self.assertEqual(len(captured), 2)
        attempts = []
        def resumed_fetch(url, source_id, **kwargs):
            attempts.append(source_id)
            return synthetic_capture(url, source_id, **kwargs)
        with patch.object(source_fetch, "fetch", side_effect=resumed_fetch):
            result = self.raw_prepare(self.root, "demo", [CODE], START, END, [1, 3], 120, operation_id="budget-resume")
        self.assertNotIn("eastmoney_actions", attempts)
        self.assertNotIn("eastmoney_nav", attempts)
        self.assertEqual(read_object(self.run_path(result) / "collection-progress.json")["source_requests"], 1)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "investment"
        self.store = Store(self.root, "demo")
        self.artifacts = Artifacts(self.store.base)

    def raw_prepare(self, root, plan, codes, start, end, horizons, lookback, *, operation_id):
        request = {"codes": codes, "start": str(start), "end": str(end), "horizons": horizons, "lookback": lookback}
        self.store.begin(operation_id, request)
        with self.store.lease(operation_id) as completed:
            if completed is not None:
                run = research.resolve_run(self.store.base, completed["run_path"])
                research.verify_selected(self.root.resolve(), self.store.base, run)
                return {**completed, "reused": True}
            if codes and all(len(code) == 6 and code.isdigit() for code in codes) and start <= end:
                catalog = fund_universe.collect_catalog(self.store, self.artifacts, operation_id)
                identity = fund_universe.resolve_identity(codes, catalog, self.artifacts, store=self.store)
            else:
                identity = None
            result = research.prepare(root, plan, codes, start, end, horizons, lookback,
                                      operation_id=operation_id, identity_snapshot_ref=identity, store=self.store)
            self.store.complete(operation_id, result)
            return result

    def prepare(self, operation="test", codes=None, horizons=None):
        with patch.object(source_fetch, "fetch", side_effect=synthetic_capture):
            return self.raw_prepare(self.root, "demo", codes or [CODE], START, END, horizons or [1, 3], 120,
                                    operation_id=operation)

    def run_path(self, result):
        return self.root / "plans/demo" / result["run_path"]

    def test_prepare_independently_audits_without_writing_account_or_global_state(self):
        result = self.prepare()
        self.assertEqual(result["totals"]["NAV_rows"], 125)
        self.assertEqual(result["totals"]["mature_market_labels"], 6)
        self.assertEqual(result["totals"]["pending_market_labels"], 4)
        self.assertEqual(result["readiness"]["historical_investor"], "data_unavailable")
        self.assertEqual(result["publication"], "orchestrator_required")
        for relative in ("config.json", "plans/demo/profile.json", "plans/demo/indexes/latest.json", "plans/demo/state/current.json"):
            self.assertFalse((self.root / relative).exists())
        self.assertFalse(hasattr(research, "main"))
        self.assertFalse(hasattr(research, "publish"))

    def test_successful_operation_is_reused_without_downloading_or_building(self):
        first = self.prepare()
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("No new download")), patch.object(research, "build_samples", side_effect=AssertionError("No rebuild")):
            second = self.raw_prepare(self.root, "demo", [CODE], START, END, [1, 3], 120, operation_id="test")
        self.assertTrue(second["reused"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(first["run_path"], second["run_path"])

    def test_same_operation_changed_input_and_tampered_sealed_data_are_rejected(self):
        result = self.prepare()
        with self.assertRaisesRegex(ValueError, "changed input"):
            self.prepare(horizons=[1])
        manifest = self.run_path(result) / "manifest.json"
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["schema_version"] = 1
        manifest.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsupported research manifest"):
            self.prepare()

    def daily_prepare(self, operation, at, nav=2.0):
        real_datetime = dt.datetime

        class CaptureDay(real_datetime):
            @classmethod
            def now(cls, tz=None):
                value = real_datetime.fromisoformat(at)
                return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

        def daily_capture(url, source_id, **kwargs):
            captured = synthetic_capture(url, source_id, **kwargs)
            captured["retrieved_at"] = at
            if source_id == "eastmoney_nav" and nav != 2.0:
                text = captured["text"].replace('"y": 2.0', '"y": ' + str(nav))
                text = text.replace(', 3.0]', ', ' + str(nav + 1.0) + ']')
                raw = text.encode("utf-8")
                captured.update(text=text, raw_bytes=raw, bytes=len(raw),
                    raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
            return captured
        with patch.object(source_fetch, "fetch", side_effect=daily_capture), patch.object(research.dt, "datetime", CaptureDay):
            return self.raw_prepare(self.root, "demo", [CODE], START, END, [1, 3], 120, operation_id=operation)

    def test_distinct_sealed_daily_runs_load_actual_original_vintages(self):
        import allocation_runtime
        import allocation_market
        first_at = (dt.datetime.now(research.CN) - dt.timedelta(days=2)).isoformat()
        second_at = (dt.datetime.now(research.CN) - dt.timedelta(days=1)).isoformat()
        first = self.daily_prepare("day-one", first_at)
        second = self.daily_prepare("day-two", second_at, 4.0)
        self.assertNotEqual(first["run_path"], second["run_path"])
        _, _, _, _, loaded, _ = allocation_runtime.load_data(self.root.resolve(), self.store.base, second["run_path"])
        row = loaded[CODE][0]
        self.assertEqual(len(row["source_versions"]), 2)
        original, current = row["source_versions"]
        self.assertEqual((original["nav"], original["observed_at"], original["available_at"]), (2.0, first_at, first_at))
        self.assertEqual((current["nav"], current["observed_at"], current["available_at"]), (4.0, second_at, None))
        before = allocation_market.nav_snapshot(row, first_at, require_known=True)
        after = allocation_market.nav_snapshot(row, second_at, require_known=True)
        self.assertEqual((before["nav"], after["nav"]), (2.0, 4.0))
        self.assertEqual(before["raw_ref"], original["raw_ref"])
        self.assertIsNone(allocation_market.nav_snapshot(row,
            (dt.datetime.fromisoformat(first_at) - dt.timedelta(seconds=1)).isoformat(), require_known=True))
        source = next(item for item in json.loads((self.run_path(second) / "source-manifest.json").read_text())
                      if item["source_id"] == original["raw_ref"]["source_id"])
        self.assertEqual((source["archive_origin"]["run_path"], source["retrieved_at"]), (first["run_path"], first_at))
        raw = research.research_audit.source_bytes(self.run_path(second), source)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), original["raw_ref"]["sha256"])
        self.assertLess(source["archive_storage"]["bytes"], source["bytes"])
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("No new capture")):
            reused = self.raw_prepare(self.root, "demo", [CODE], START, END, [1, 3], 120, operation_id="day-two")
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["manifest_sha256"], second["manifest_sha256"])

    def test_daily_archive_rejects_original_raw_time_and_foreign_subject(self):
        at = (dt.datetime.now(research.CN) - dt.timedelta(days=2)).isoformat()
        later = (dt.datetime.now(research.CN) - dt.timedelta(days=1)).isoformat()
        first = self.daily_prepare("original", at)
        policy = read_object(self.run_path(first) / "policy.json")
        source = next(item for item in json.loads((self.run_path(first) / "source-manifest.json").read_text())
                      if item["source_id"] == "NAV-" + CODE)
        kind = research._nav_archive_kind(policy["code_info"][CODE], source["registry_hash"])
        _, saved = next(self.store.scan(kind))
        for field in ("retrieved_at", "currency"):
            with self.subTest(field=field):
                changed = copy.deepcopy(saved)
                if field == "retrieved_at":
                    changed["capture"][field] = (dt.datetime.fromisoformat(at) + dt.timedelta(seconds=1)).isoformat()
                else:
                    changed["subject"][field] = "USD"
                key = fingerprint({name: changed[name] for name in
                    ("schema_version", "subject", "capture", "action_capture", "coverage")})
                operation = "poison-" + field
                self.store.begin(operation, {})
                with self.store.lease(operation):
                    self.store.put(kind, key, changed)
                    self.store.complete(operation, {})
                with self.assertRaisesRegex(ValueError, "archive.*(time|subject)|capture time"):
                    self.daily_prepare("reject-" + field, later)
                with self.store.maintenance():
                    with self.store.transaction() as conn:
                        conn.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))
        raw_path = self.store.base / saved["raw_ref"]["path"]
        raw_path.write_bytes(b"corrupted original captured NAV")
        with self.assertRaisesRegex(ValueError, "Artifact content or size changed"):
            self.daily_prepare("reject-raw", later)

    def test_compressed_original_nav_rejects_corruption_and_expansion(self):
        raw = b"captured original NAV"
        path = self.root / "proof.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = gzip.compress(raw, mtime=0)
        path.write_bytes(encoded)
        source = {"source_id": "test", "path": path.name, "bytes": len(raw),
                  "sha256": hashlib.sha256(raw).hexdigest(), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                  "archive_storage": {"encoding": "gzip", "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}}
        self.assertEqual(research.research_audit.source_bytes(self.root, source), raw)
        path.write_bytes(encoded[:-1] + bytes([encoded[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "Compressed NAV storage changed"):
            research.research_audit.source_bytes(self.root, source)
        expanded = gzip.compress(raw * 1000, mtime=0)
        path.write_bytes(expanded)
        source["archive_storage"].update(bytes=len(expanded), sha256=hashlib.sha256(expanded).hexdigest())
        with self.assertRaisesRegex(ValueError, "Source body changed"):
            research.research_audit.source_bytes(self.root, source)

    def test_same_operation_resumes_after_sealed_preparation_without_backfill(self):
        operation = "sealed-before-downstream-failure"
        request = {"codes": [CODE], "start": str(START), "end": str(END), "horizons": [1, 3], "lookback": 120}
        self.store.begin(operation, request)
        with patch.object(source_fetch, "fetch", side_effect=synthetic_capture):
            with self.store.lease(operation):
                catalog = fund_universe.collect_catalog(self.store, self.artifacts, operation)
                identity = fund_universe.resolve_identity([CODE], catalog, self.artifacts, store=self.store)
                first = research.prepare(self.root, "demo", [CODE], START, END, [1, 3], 120,
                    operation_id=operation, identity_snapshot_ref=identity, store=self.store)
            second = self.raw_prepare(self.root, "demo", [CODE], START, END, [1, 3], 120, operation_id=operation)
        self.assertNotEqual(first["run_path"], second["run_path"])
        rows = research.research_audit.tables(self.run_path(second), "normalized", CODE)
        self.assertTrue(all(len(row["source_versions"]) == 1
                            and row["source_versions"][0]["available_at"] is None for row in rows))
        policy = read_object(self.run_path(second) / "policy.json")
        kind = research._nav_archive_kind(policy["code_info"][CODE], fingerprint(source_fetch.load_registry()))
        archived = list(self.store.scan(kind))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0][1]["origin"]["run_path"], first["run_path"])

    def test_disjoint_original_nav_window_does_not_block_later_preparation(self):
        at = (dt.datetime.now(research.CN) - dt.timedelta(days=2)).isoformat()
        first = self.daily_prepare("old-window", at)
        policy = read_object(self.run_path(first) / "policy.json")
        kind = research._nav_archive_kind(policy["code_info"][CODE], fingerprint(source_fetch.load_registry()))
        _, archived = next(self.store.scan(kind))
        self.assertEqual(archived["coverage"], {"start": START.isoformat(), "end": END.isoformat()})
        # The next original document contains an entirely later actual NAV window.
        later_start = END + dt.timedelta(days=1)
        later_end = later_start + dt.timedelta(days=124)
        with patch.dict(globals(), {"START": later_start, "END": later_end}):
            later = self.prepare("later-window")
        rows = research.research_audit.tables(self.run_path(later), "normalized", CODE)
        self.assertEqual(len(rows), 125)
        self.assertTrue(all(len(row["source_versions"]) == 1 for row in rows))

    def test_second_fund_archive_failure_rolls_back_all_origins_before_retry(self):
        operation = "partial-archive-publication"
        original_put = self.store.put

        def fail_second_archive(kind, key, value, *args, **kwargs):
            if kind.startswith("research_nav_archive:") and value["subject"]["code"] == OTHER_CODE:
                raise ValueError("Synthetic second fund archive publication failure")
            return original_put(kind, key, value, *args, **kwargs)

        with patch.object(self.store, "put", side_effect=fail_second_archive):
            with self.assertRaisesRegex(ValueError, "second fund archive publication failure"):
                self.prepare(operation, [CODE, OTHER_CODE])
        failed = next(self.store.base.glob("work/*/*/*/nav-*"))
        self.assertEqual(read_object(failed / "run-status.json")["status"], "failed")
        policy = read_object(failed / "policy.json")
        registry_hash = fingerprint(source_fetch.load_registry())
        for code in (CODE, OTHER_CODE):
            kind = research._nav_archive_kind(policy["code_info"][code], registry_hash)
            self.assertEqual(list(self.store.scan(kind)), [])
        retried = self.prepare(operation, [CODE, OTHER_CODE])
        self.assertNotEqual(self.run_path(retried), failed)
        for code in (CODE, OTHER_CODE):
            kind = research._nav_archive_kind(policy["code_info"][code], registry_hash)
            archived = list(self.store.scan(kind))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0][1]["origin"]["run_path"], retried["run_path"])
        next_day = self.prepare("daily-after-publication-retry", [CODE, OTHER_CODE])
        research.verify_selected(self.root.resolve(), self.store.base, self.run_path(next_day))
        for code in (CODE, OTHER_CODE):
            rows = research.research_audit.tables(self.run_path(next_day), "normalized", code)
            self.assertTrue(all(len(row["source_versions"]) == 2 for row in rows))
            self.assertTrue(all(row["source_versions"][0]["available_at"] is not None for row in rows))

    def test_failed_collection_restarts_in_isolated_generation(self):
        calls = []
        def partial(url, source_id, **kwargs):
            calls.append(url)
            if OTHER_CODE in url and source_id == "eastmoney_nav":
                raise OSError("Synthetic temporary failure")
            return synthetic_capture(url, source_id, **kwargs)
        with patch.object(source_fetch, "fetch", side_effect=partial):
            with self.assertRaises(OSError):
                self.raw_prepare(self.root, "demo", [CODE, OTHER_CODE], START, END, [1, 3], 120, operation_id="resume")
        def resumed(url, source_id, **kwargs):
            calls.append(url)
            return synthetic_capture(url, source_id, **kwargs)
        with patch.object(source_fetch, "fetch", side_effect=resumed):
            result = self.raw_prepare(self.root, "demo", [CODE, OTHER_CODE], START, END, [1, 3], 120, operation_id="resume")
        # A fresh lease generation has isolated output paths but restores the
        # same operation's original captured bytes instead of downloading again.
        self.assertEqual(calls.count("https://fund.eastmoney.com/pingzhongdata/" + CODE + ".js"), 1)
        self.assertEqual(result["totals"]["NAV_rows"], 250)

    def test_explicit_plan_and_run_are_required(self):
        with self.assertRaisesRegex(ValueError, "explicit plan"):
            research.select_plan(self.root, None, create=True)
        result = self.prepare()
        base = self.root / "plans/demo"
        with self.assertRaisesRegex(ValueError, "explicit verified"):
            research.resolve_run(base, None)
        self.assertEqual(research.resolve_run(base, result["run_path"]), self.run_path(result).resolve())

    def test_copied_run_and_changed_saved_audit_are_rejected(self):
        result = self.prepare()
        source = self.run_path(result)
        other = self.root / "plans/other"
        copied = other / result["run_path"]
        shutil.copytree(source, copied)
        with self.assertRaisesRegex(ValueError, "selected plan"):
            research.verify_selected(self.root.resolve(), other.resolve(), copied.resolve())
        (source / "audit.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "independent verification"):
            research.verify_selected(self.root.resolve(), source.parents[4], source)

    def test_source_provenance_changes_are_rejected_after_resealing(self):
        result = self.prepare()
        run = self.run_path(result)
        path = run / "source-manifest.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows[0]["final_url"] = "http://untrusted.example/file.js"
        path.write_text(json.dumps(rows), encoding="utf-8")
        manifest_path = run / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"] = research.inventory(run)
        manifest["dataset_hash"] = fingerprint(manifest["artifacts"])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Final source"):
            research.research_audit.verify_run(run, research.executor_hashes())

    def test_input_validation_happens_before_collection(self):
        for options in (([], START, END), ([CODE], END, START), (["not-a-code"], START, END)):
            with patch.object(source_fetch, "fetch", side_effect=AssertionError("Must reject first")):
                with self.assertRaises(ValueError):
                    self.raw_prepare(self.root, "demo", *options, [1], 120, operation_id="invalid")

    def test_disclosed_split_requires_original_evidence_before_samples(self):
        def split_capture(url, sid, **kwargs):
            result = synthetic_capture(url, sid, **kwargs)
            if sid == 'eastmoney_actions':
                text = result['text'].replace('<tr><td>暂无拆分信息!</td></tr>',
                    '<tr><td>2020</td><td>2020-02-01</td><td>份额折算</td><td>1:2</td></tr>')
                raw = text.encode('utf-8')
                result.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(),
                              text_sha256=hashlib.sha256(raw).hexdigest())
            return result
        with patch.object(source_fetch, 'fetch', side_effect=split_capture), \
             patch.object(research, 'build_samples', side_effect=AssertionError('Split data must not reach samples')):
            with self.assertRaises(research.ResearchEvidenceGap) as caught:
                self.raw_prepare(self.root, 'demo', [CODE], START, END, [1], 120, operation_id='split')
        self.assertEqual(caught.exception.code, 'needs_research')
        self.assertEqual(caught.exception.required_actions[0]['action'], 'verify_split_and_implement_share_factors')
        status_path = next(self.root.rglob('run-status.json'))
        self.assertEqual(json.loads(status_path.read_text(encoding='utf-8'))['status'], 'needs_research')


if __name__ == "__main__": unittest.main()
