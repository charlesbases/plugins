"""Synthetic audit fixtures only; never claim these are real training data."""

import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/research_audit.py"
sys.path.insert(0, str(SCRIPT.parent))
from contracts import canonical_bytes, fingerprint
import source_fetch
import fund_universe
from artifacts import Artifacts
from contracts import utc_now
spec = importlib.util.spec_from_file_location("investment_research_audit", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class ResearchAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "investment"
        self.plan = self.root / "plans/demo"
        self.run = self.plan / "research/2026/10/03/fixture"
        self.run.mkdir(parents=True)
        self.days = [dt.date(2019, 1, 1) + dt.timedelta(days=i) for i in range(125)]
        self.code = "000001"
        self.policy = {"schema_version": 4, "start": self.days[0].isoformat(), "end": self.days[-1].isoformat(),
                       "horizons": [1, 3], "lookback": 120, "policy_version": "market-nav-research-4",
                       "codes": [self.code], "root": str(self.root), "plan_id": "demo", "run_id": self.run.name,
                       "code_info": {self.code: {"code": self.code, "fund_group_id": "synthetic",
                                                "type": "synthetic_type", "benchmark_id": "synthetic_benchmark",
                                                "common_exposure": "synthetic_market"}},
                       "missing_blockers": list(audit.BLOCKERS)}
        self.policy.update(audit.POLICY_RULES)
        save(self.run / "policy.json", self.policy)
        registry = {"sources": {}, "anchors": [], "corrections": [], "code_info": self.policy["code_info"]}
        self.transport_registry = source_fetch.load_registry()
        self.expected = {}
        for name in audit.EXECUTORS:
            path = self.run / "executor" / name
            if name == "source_registry.json":
                save(path, self.transport_registry)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"# Synthetic executor snapshot, never executed.\n")
            self.expected[name] = sha(path)
        save(self.run / "evidence-used.json", {"sources": {}, "anchors": [], "corrections": [],
                                               "document_verification": {}})
        raw_nav, cumulative, normalized = [], [], []
        for day in self.days:
            stamp = int(dt.datetime.combine(day, dt.time(), audit.CN).timestamp()) * 1000
            raw_nav.append({"x": stamp, "y": 1, "unitMoney": "", "equityReturn": 0})
            cumulative.append([stamp, 1])
            normalized.append({"code": self.code, "date": day.isoformat(), "nav": 1,
                               "cumulative_nav": 1, "distribution_per_share": 0,
                               "distribution_text": "", "provider_daily_return": 0,
                               "historical_published_at": None})
        vendor = self.run / "raw/vendor" / (self.code + ".js")
        vendor.parent.mkdir(parents=True)
        vendor.write_text("var fS_code = \"" + self.code + "\";\nvar fS_name = \"Synthetic fixtures\";\nvar Data_netWorthTrend = "
                          + json.dumps(raw_nav) + ";\nvar Data_ACWorthTrend = "
                          + json.dumps(cumulative) + ";\n", encoding="utf-8")
        self.sources = [{"source_id": "NAV-" + self.code, "url": "https://fund.eastmoney.com/pingzhongdata/" + self.code + ".js",
                         "path": vendor.relative_to(self.run).as_posix(), "bytes": vendor.stat().st_size,
                         "sha256": sha(vendor), "retrieved_at": "2026-10-03T20:00:00+08:00",
                         "expected_sha256": None, "review_status": "vendor_snapshot_not_independent"}]
        save(self.run / "source-manifest.json", self.sources)
        self.write_months("normalized", normalized, "date")
        self.raw_nav, self.cumulative, self.normalized = raw_nav, cumulative, normalized
        xs, ys = [], []
        values = {"momentum_20_nav_observations": 0, "momentum_60_nav_observations": 0,
                  "momentum_120_nav_observations": 0,
                  "volatility_60_nav_observations_annualized_252": 0,
                  "drawdown_60_nav_intervals": 0}
        # Constant unit NAV means zero returns, volatility, losses and drawdowns.
        # Counts and pending boundaries are specified independently of the generator.
        for i in range(120, 125):
            day = self.days[i].isoformat()
            ident = self.code + ":" + day
            xs.append({"id": ident, "code": self.code, "feature_cutoff_nav_date": day,
                       "feature_window_start_date": self.days[i - 120].isoformat(),
                       "feature_version": "market-nav-baseline-1", "values": dict(values),
                       "known_max_nav_date": day, "historical_max_source_available_at": None,
                       "strict_PIT_verified": False,
                       "excluded_features": ["current_manager", "current_fees", "current_company_profile", "news"],
                       "missing_flags": list(audit.BLOCKERS), "use_scope": "ex_post_market_research_only"})
            for h in (1, 3):
                target = self.days[i] + dt.timedelta(days=h)
                pending = target > self.days[-1]
                row = {"id": ident + ":" + str(h), "feature_id": ident, "code": self.code,
                       "horizon_calendar_days": h, "horizon_origin": "feature_cutoff_nav_date",
                       "start_nav_date": day, "target_date": target.isoformat(),
                       "label_kind": "cash_distribution_entitlement_market_NAV",
                       "historical_label_available_at": None,
                       "observed_by_this_research_run_at": "2026-10-03T20:00:00+08:00",
                       "actual_investor_net_return": None, "fee_rule_id": None, "entry_order_at": None,
                       "cash_available_at": None, "strict_investor_eligibility": False,
                       "missing_flags": list(audit.BLOCKERS),
                       "status": "pending_future_NAV" if pending else "mature_market_label",
                       "end_nav_date": None if pending else target.isoformat(),
                       "alignment_delay_calendar_days": None if pending else 0}
                row.update({field: None if pending else 0 for field in audit.OUTCOMES})
                ys.append(row)
        self.write_months("features", xs, "feature_cutoff_nav_date")
        self.write_months("labels", ys, "start_nav_date")
        self.manifest = {"schema_version": 4, "run_id": self.run.name, "plan_id": "demo",
                         "codes": [self.code], "start": self.policy["start"], "end": self.policy["end"],
                         "horizons": [1, 3], "lookback": 120,
                         "policy_version": "market-nav-research-4", "executor_hashes": dict(self.expected), "execution_generation": 1,
                         "raw_summaries": [{"code": self.code, "name_observed_today": "Synthetic fixtures",
                                            "rows": 125, "start": self.days[0].isoformat(), "end": self.days[-1].isoformat(),
                                            "source_total_history_rows": 125, "dividends": [], "dividend_count": 0,
                                            "max_daily_return_difference": 0.0}],
                         "funds": [{"code": self.code, "fund_group_id": "synthetic", "common_exposure": "synthetic_market",
                                    "feature_rows": 5, "warmup_rows_excluded": 120,
                                    "first_feature_date": self.days[120].isoformat(), "last_feature_date": self.days[124].isoformat(),
                                    "horizons": {"1": {"mature_market": 4, "pending": 1, "qualified_historical_investor_net": 0,
                                                       "max_alignment_delay_calendar_days": 0},
                                                 "3": {"mature_market": 2, "pending": 3, "qualified_historical_investor_net": 0,
                                                       "max_alignment_delay_calendar_days": 0}}, "status": "generated"}]}
        self.request = {"schema_version": 4, "operation_id": "audit-fixture", "root": str(self.root), "execution_generation": 1,
            "plan_id": "demo", "codes": [self.code], "start": self.policy["start"], "end": self.policy["end"],
            "horizons": [1, 3], "lookback": 120, "executor_hashes": dict(self.expected)}
        save(self.run / "request.json", self.request)
        self.manifest.update(operation_id="audit-fixture", request_hash=fingerprint(self.request))
        self.install_identity_and_actions()
        self.seal()

    def install_identity_and_actions(self):
        artifacts = Artifacts(self.run)
        def captured(text, sid, url):
            raw = text.encode('utf-8')
            return {"raw_ref": artifacts.put_bytes(raw), "capture": {
                "registry_source_id": sid, "registry_hash": fingerprint(self.transport_registry),
                "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
                "content_type": "application/javascript" if sid == "eastmoney_catalog" else "text/html",
                "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation", "retrieved_at": utc_now(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}
        catalog_text = 'var r = [["000001","FIXTURE","Synthetic fixtures","synthetic_type","FIXTURE"]];'
        entries = fund_universe.parse_catalog(catalog_text)
        catalog_source = captured(catalog_text, "eastmoney_catalog", fund_universe.CATALOG_URL)
        catalog_ref = artifacts.put_json({"schema_version": 4, "source": catalog_source, "entries": entries})
        profile = ('<table><tr><th>基金全称</th><td>Synthetic legal fund</td><th>基金简称</th><td>Synthetic fixtures</td></tr>'
                   '<tr><th>基金代码</th><td>000001</td><th>基金类型</th><td>synthetic_type</td></tr>'
                   '<tr><th>基金管理人</th><td><a href="//fund.eastmoney.com/company/12345.html">Fixture manager</a></td>'
                   '<th>基金经理人</th><td>Fixture person</td></tr>'
                   '<tr><th>业绩比较基准</th><td>synthetic_benchmark</td></tr></table>')
        identity = fund_universe.parse_profile(profile, self.code, entries[self.code])
        save(self.run / "identity-snapshot.json", {"schema_version": 4, "catalog_ref": catalog_ref,
             "profiles": {self.code: captured(profile, "eastmoney_profile", "https://fundf10.eastmoney.com/jbgk_000001.html")},
             "identities": {self.code: identity}, "observed_at": utc_now(), "max_age_seconds": 86400,
             "requested_codes": [self.code], "required_actions": [], "issuer_documents": [], "issuer_requests": [],
             "automatic_issuer_discovery": [], "automatic_issuer_scope": "not_requested",
             "rejected_profiles": {}, "rejected_issuer_documents": [], "pending_issuer_requests": []})
        self.policy["code_info"] = {self.code: identity}
        save(self.run / "issuer-nav-evidence.json", {"anchors": [], "sources": {}, "source_records": [],
             "gaps": [{"code": self.code, "status": "issuer_document_not_collected"}]})
        save(self.run / "policy.json", self.policy)
        self.manifest["funds"][0].update(fund_group_id=identity["fund_group_id"], common_exposure=None)
        self.save_actions([])

    def save_actions(self, cash_rows):
        text = ('<script>var strbzdm="000001";</script><table><tr><th>年份</th><th>权益登记日</th>'
                '<th>除息日</th><th>每10份分红</th><th>分红发放日</th></tr>'
                + ''.join(cash_rows) + ('<tr><td>暂无分红信息!</td></tr>' if not cash_rows else '')
                + '</table><table><tr><th>年份</th><th>拆分折算日</th><th>拆分类型</th><th>拆分折算比例</th></tr>'
                '<tr><td>暂无拆分信息!</td></tr></table>')
        document = self.run / 'raw/actions/000001.html'
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(text, encoding='utf-8')
        sid, url = 'TT-actions-000001', 'https://fundf10.eastmoney.com/fhsp_000001.html'
        actions = fund_universe.parse_actions(text, self.code)
        for row in actions['dividends']:
            row.update(currency=self.policy['code_info'][self.code]['currency'], currency_basis='same_share_source_identity')
        source = {"url": url, "expected_sha256": sha(document), "kind": "html", "code": self.code, "scope": actions['scope']}
        corrections = [{**row, "source_id": sid} for row in actions['dividends']]
        save(self.run / 'evidence-registry.json', {"schema_version": 4, "sources": {sid: source}, "anchors": [], "corrections": corrections})
        save(self.run / 'evidence-used.json', {"sources": {sid: source}, "anchors": [], "corrections": corrections,
                                             "document_verification": {sid: "reproduced_current_source"}})
        self.sources[:] = [row for row in self.sources if row['source_id'] != sid]
        self.sources.append({"source_id": sid, "url": url, "path": 'raw/actions/000001.html', "sha256": sha(document),
                             "bytes": document.stat().st_size, "retrieved_at": '2026-10-03T19:00:00+08:00',
                             "expected_sha256": None, "review_status": "provider_snapshot"})
        inventory = {"schema_version": 4, "by_code": {self.code: {**actions,
            "known_at": "2026-10-03T19:00:00+08:00", "source_id": sid, "source_sha256": sha(document),
            "source_path": "raw/actions/000001.html", "source_url": url,
            "coverage": "all_rows_in_current_distributor_table_not_all_future_announcements",
            "rights_terms_status": "issuer_entitlement_rules_not_in_distributor_table"}}}
        save(self.run / "source-actions.json", inventory)
        self.manifest["action_inventory"] = inventory
        return source, corrections

    def write_months(self, kind, rows, field):
        months = sorted({row[field][:7] for row in rows})
        for month in months:
            jsonl(self.run / kind / self.code / (month + "-part-000001.jsonl"),
                  [row for row in rows if row[field].startswith(month)])

    def seal(self):
        for record in self.sources:
            body = self.run / record["path"]
            record["raw_sha256"] = record["sha256"]
            registry_id = "eastmoney_nav" if record["source_id"].startswith("NAV-") else "eastmoney_actions"
            record.setdefault("registry_source_id", registry_id)
            record.setdefault("registry_hash", fingerprint(self.transport_registry))
            record.setdefault("requested_url", record.pop("url", None))
            record.setdefault("final_url", record["requested_url"])
            record.setdefault("redirect_chain", [])
            record.setdefault("http_status", 200)
            record.setdefault("content_type", "application/javascript" if registry_id == "eastmoney_nav" else "text/html")
            record.setdefault("encoding", "utf-8")
            record.setdefault("transport", "HTTPS_default_certificate_validation")
            record["text_sha256"] = hashlib.sha256(body.read_bytes()).hexdigest() if record["encoding"] else None
        save(self.run / "source-manifest.json", self.sources)
        self.manifest["artifacts"] = [{"path": p.relative_to(self.run).as_posix(),
                                      "bytes": p.stat().st_size, "sha256": sha(p)}
                                     for p in sorted(self.run.rglob("*"))
                                     if p.is_file() and p.relative_to(self.run).as_posix() not in audit.EXCLUDED]
        self.manifest["artifacts"].sort(key=lambda row: row["path"])
        canonical = canonical_bytes(self.manifest["artifacts"])
        self.manifest["dataset_hash"] = hashlib.sha256(canonical).hexdigest()
        save(self.run / "manifest.json", self.manifest)

    def verify(self):
        return audit.verify_run(self.run, self.expected)

    def test_resealed_nav_registry_cannot_invent_an_issuer_point(self):
        for filename in ("issuer-nav-evidence.json", "evidence-registry.json"):
            path = self.run / filename
            original = json.loads(path.read_text(encoding="utf-8"))
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["anchors"] = [{"code": self.code, "date": "2024-01-01", "nav": "1.0000",
                                   "decimal_places": 4, "source_id": "TT-actions-"+self.code}]
            save(path, changed)
            self.seal()
            with self.subTest(filename=filename), self.assertRaisesRegex(audit.AuditError, "differs from"):
                self.verify()
            save(path, original)
            self.seal()

    def mutate_row(self, kind, transform):
        path = next((self.run / kind / self.code).glob("*.jsonl"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        transform(rows)
        jsonl(path, rows)

    def add_reviewed_distribution(self, provider_fixed=False):
        day, payment = self.days[10].isoformat(), self.days[11].isoformat()
        source, corrections = self.save_actions([f'<tr><td>2019</td><td>{day}</td><td>{day}</td>'
                                                f'<td>每10份派现金0.3000元</td><td>{payment}</td></tr>'])
        correction = corrections[0]
        self.raw_nav[10]["equityReturn"] = 3
        if provider_fixed:
            self.raw_nav[10]["unitMoney"] = "分红：每份派现金0.03元"
        for i in range(10, len(self.days)):
            self.cumulative[i][1] = 1.03
            self.normalized[i]["cumulative_nav"] = 1.03
        self.normalized[10].update(distribution_per_share=.03, provider_daily_return=.03,
                                  distribution_text=self.raw_nav[10]["unitMoney"],
                                  distribution_evidence={**correction, "source_url": source["url"],
                                  "source_sha256": source["expected_sha256"],
                                  "mode": "provider_already_matches" if provider_fixed else "evidence补录"})
        vendor = self.run / "raw/vendor" / (self.code + ".js")
        vendor.write_text("var fS_code = \"" + self.code + "\";\nvar fS_name = \"Synthetic fixtures\";\nvar Data_netWorthTrend = "
                          + json.dumps(self.raw_nav) + ";\nvar Data_ACWorthTrend = "
                          + json.dumps(self.cumulative) + ";\n", encoding="utf-8")
        self.sources[0].update(sha256=sha(vendor), bytes=vendor.stat().st_size)
        save(self.run / "source-manifest.json", self.sources)
        self.write_months("normalized", self.normalized, "date")
        self.mutate_row("features", lambda rows: [row["values"].update(momentum_120_nav_observations=.03) for row in rows])
        self.manifest["raw_summaries"][0].update(dividends=[dict(self.normalized[10])], dividend_count=1)
        self.seal()

    def test_constant_market_fixture_passes_with_known_maturity_counts(self):
        result = self.verify()
        self.assertEqual(result["totals"], {"NAV_rows": 125, "feature_rows": 5, "label_rows": 10,
                                           "mature_market_labels": 6, "pending_market_labels": 4,
                                           "strict_historical_investor_labels": 0})
        self.assertEqual(result["readiness"]["historical_investor"], "data_unavailable")
        self.assertFalse(result["model_fit_executed"])
        self.assertEqual(result["official_NAV_anchors"]["codes_without_anchors"], [self.code])

    def test_added_missing_and_changed_artifacts_fail_exact_inventory(self):
        for name in ("unreviewed.json", "raw/audit.json"):
            target = self.run / name
            target.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(audit.AuditError, "inventory"):
                self.verify()
            target.unlink()
        path = self.run / "policy.json"
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(audit.AuditError, "inventory"):
            self.verify()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(audit.AuditError, "inventory"):
            self.verify()

    def test_resealed_wrong_feature_and_pending_outcome_fail_independent_math(self):
        self.mutate_row("features", lambda rows: rows[0]["values"].update(momentum_20_nav_observations=.1))
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "recomputation"):
            self.verify()
        self.mutate_row("features", lambda rows: rows[0]["values"].update(momentum_20_nav_observations=0))
        self.mutate_row("labels", lambda rows: rows[-1].update(market_return=0))
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "Pending label"):
            self.verify()

    def test_resealed_future_feature_and_missing_nav_fail_boundaries(self):
        self.mutate_row("features", lambda rows: rows[0].update(known_max_nav_date="2030-01-01"))
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "Feature date boundary"):
            self.verify()
        self.mutate_row("features", lambda rows: rows[0].update(known_max_nav_date=rows[0]["feature_cutoff_nav_date"]))
        self.mutate_row("normalized", lambda rows: rows.pop())
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "NAV coverage"):
            self.verify()

    def test_changed_policy_and_executor_cannot_be_resealed_into_success(self):
        self.policy["missing_blockers"] = []
        save(self.run / "policy.json", self.policy)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "blocker policy"):
            self.verify()
        self.policy["missing_blockers"] = list(audit.BLOCKERS)
        save(self.run / "policy.json", self.policy)
        path = self.run / "executor/research_data.py"
        path.write_bytes(b"# Changed executor.\n")
        self.manifest["executor_hashes"]["research_data.py"] = sha(path)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "installed executors"):
            self.verify()



    def test_inventory_guards_remain_active_under_optimized_python(self):
        (self.run / "unexpected.json").write_text("{}", encoding="utf-8")
        code = ("import importlib.util,sys,pathlib;sys.path.insert(0,str(pathlib.Path(sys.argv[1]).parent));"
                "s=importlib.util.spec_from_file_location('audit',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "m.verify_run(pathlib.Path(sys.argv[2]))")
        result = subprocess.run([sys.executable, "-B", "-O", "-c", code, str(SCRIPT), str(self.run)],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Artifact inventory", result.stderr)

    def test_reviewed_distribution_handles_missing_note_and_already_fixed_provider(self):
        self.add_reviewed_distribution()
        self.assertEqual(self.verify()["official_NAV_anchors"]["passed"], 0)
        self.raw_nav[10]["unitMoney"] = "每10份派现金0.30元"
        self.normalized[10]["distribution_text"] = self.raw_nav[10]["unitMoney"]
        self.normalized[10]["distribution_evidence"]["mode"] = "provider_already_matches"
        vendor = self.run / "raw/vendor" / (self.code + ".js")
        vendor.write_text("var fS_code = \"" + self.code + "\";\nvar fS_name = \"Synthetic fixtures\";\nvar Data_netWorthTrend = "
                          + json.dumps(self.raw_nav) + ";\nvar Data_ACWorthTrend = "
                          + json.dumps(self.cumulative) + ";\n", encoding="utf-8")
        self.sources[0].update(sha256=sha(vendor), bytes=vendor.stat().st_size)
        save(self.run / "source-manifest.json", self.sources)
        self.write_months("normalized", self.normalized, "date")
        self.manifest["raw_summaries"][0]["dividends"] = [dict(self.normalized[10])]
        self.seal()
        self.assertEqual(self.verify()["readiness"]["market_research"], "passed")

    def test_resealed_changed_issuer_body_cannot_reuse_reviewed_status(self):
        self.add_reviewed_distribution(provider_fixed=True)
        document = self.run / "raw/actions/000001.html"
        document.write_bytes(document.read_bytes() + b"Changed issuer bytes.\n")
        self.sources[1].update(sha256=sha(document), bytes=document.stat().st_size)
        save(self.run / "source-manifest.json", self.sources)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "cannot be marked reviewed"):
            self.verify()

    def test_resealed_wrong_vendor_request_or_response_identity_is_rejected(self):
        correct_url = self.sources[0]["requested_url"]
        self.sources[0]["requested_url"] = "https://fund.eastmoney.com/pingzhongdata/000002.js"
        self.sources[0]["final_url"] = self.sources[0]["requested_url"]
        save(self.run / "source-manifest.json", self.sources)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "code/request provenance"):
            self.verify()
        self.sources[0]["requested_url"] = correct_url
        self.sources[0]["final_url"] = correct_url
        vendor = self.run / "raw/vendor" / (self.code + ".js")
        vendor.write_text(vendor.read_text(encoding="utf-8").replace('fS_code = "000001"', 'fS_code = "000002"'),
                          encoding="utf-8")
        self.sources[0].update(sha256=sha(vendor), bytes=vendor.stat().st_size)
        save(self.run / "source-manifest.json", self.sources)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "response fund code"):
            self.verify()

    def test_resealed_group_type_and_benchmark_changes_cannot_override_reviewed_identity(self):
        for field in ("fund_group_id", "type", "benchmark_id"):
            with self.subTest(field=field):
                original = self.policy["code_info"][self.code][field]
                self.policy["code_info"][self.code][field] = "forged"
                save(self.run / "policy.json", self.policy)
                self.seal()
                with self.assertRaisesRegex(audit.AuditError, "captured legal fund profiles"):
                    self.verify()
                self.policy["code_info"][self.code][field] = original

    def test_resealed_name_counts_and_maturity_summaries_are_independently_checked(self):
        changes = [("raw_summaries", "name_observed_today", "Forged name"),
                   ("raw_summaries", "source_total_history_rows", 999),
                   ("raw_summaries", "dividend_count", 1),
                   ("funds", "feature_rows", 999), ("funds", "fund_group_id", "forged")]
        for collection, field, value in changes:
            with self.subTest(collection=collection, field=field):
                row = self.manifest[collection][0]
                original = row[field]
                row[field] = value
                self.seal()
                with self.assertRaisesRegex(audit.AuditError, "Summary"):
                    self.verify()
                row[field] = original
        self.manifest["funds"][0]["horizons"]["1"]["mature_market"] = 5
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "Summary"):
            self.verify()

    def test_policy_plan_and_run_identity_mismatch_fails_after_resealing(self):
        for field, value in (("run_id", "other_run"), ("plan_id", "other_plan"), ("codes", ["000002"])):
            with self.subTest(field=field):
                original = self.policy[field]
                self.policy[field] = value
                save(self.run / "policy.json", self.policy)
                self.seal()
                with self.assertRaisesRegex(audit.AuditError, "policy identity|request/policy"):
                    self.verify()
                self.policy[field] = original


if __name__ == "__main__":
    unittest.main()
