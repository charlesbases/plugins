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
        save(self.root / "config.json", {"active_plan": "demo"})
        save(self.plan / "state/current.json", {"account_initialized": True, "holdings": []})
        protected = {"config.json": sha(self.root / "config.json"),
                     "plans/demo/state/current.json": sha(self.plan / "state/current.json")}
        save(self.run / "account-protection.json", {"root": str(self.root), "plan_id": "demo",
             "files": protected, "absent": ["plans/demo/profile.json"], "ledger_paths": []})
        self.days = [dt.date(2019, 1, 1) + dt.timedelta(days=i) for i in range(125)]
        self.code = "000001"
        self.policy = {"start": self.days[0].isoformat(), "end": self.days[-1].isoformat(),
                       "horizons": [1, 3], "lookback": 120, "policy_version": "market-nav-research-1",
                       "codes": [self.code], "root": str(self.root), "plan_id": "demo", "run_id": self.run.name,
                       "code_info": {self.code: {"code": self.code, "fund_group_id": "synthetic",
                                                "type": "synthetic_type", "benchmark_id": "synthetic_benchmark",
                                                "common_exposure": "synthetic_market"}},
                       "missing_blockers": list(audit.BLOCKERS)}
        self.policy.update(audit.POLICY_RULES)
        save(self.run / "policy.json", self.policy)
        registry = {"sources": {}, "anchors": [], "corrections": [], "code_info": self.policy["code_info"]}
        self.expected = {}
        for name in audit.EXECUTORS:
            path = self.run / "executor" / name
            if name == "research-evidence.json":
                save(path, registry)
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
        self.manifest = {"schema_version": 1, "run_id": self.run.name, "plan_id": "demo",
                         "codes": [self.code], "start": self.policy["start"], "end": self.policy["end"],
                         "horizons": [1, 3], "lookback": 120,
                         "policy_version": "market-nav-research-1", "executor_hashes": dict(self.expected),
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
        self.seal()

    def write_months(self, kind, rows, field):
        months = sorted({row[field][:7] for row in rows})
        for month in months:
            jsonl(self.run / kind / self.code / (month + "-part-000001.jsonl"),
                  [row for row in rows if row[field].startswith(month)])

    def seal(self):
        self.manifest["artifacts"] = [{"path": p.relative_to(self.run).as_posix(),
                                      "bytes": p.stat().st_size, "sha256": sha(p)}
                                     for p in sorted(self.run.rglob("*"))
                                     if p.is_file() and p.relative_to(self.run).as_posix() not in audit.EXCLUDED]
        self.manifest["artifacts"].sort(key=lambda row: row["path"])
        canonical = json.dumps(self.manifest["artifacts"], sort_keys=True,
                               separators=(",", ":"), ensure_ascii=True).encode()
        self.manifest["dataset_hash"] = hashlib.sha256(canonical).hexdigest()
        save(self.run / "manifest.json", self.manifest)

    def verify(self, protection=False):
        return audit.verify_run(self.run, self.expected, protection)

    def mutate_row(self, kind, transform):
        path = next((self.run / kind / self.code).glob("*.jsonl"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        transform(rows)
        jsonl(path, rows)

    def add_reviewed_distribution(self, provider_fixed=False):
        document = self.run / "raw/primary/issuer.pdf"
        document.parent.mkdir(parents=True)
        document.write_bytes(b"%PDF-1.4\nSynthetic issuer fixture: dividend 0.03 per share.\n")
        source = {"url": "https://example.com/issuer.pdf", "expected_sha256": sha(document), "kind": "pdf",
                  "reviewed_at": "2026-10-03", "review_method": "Synthetic fixture only"}
        correction = {"code": self.code, "date": self.days[10].isoformat(), "distribution_per_share": .03,
                      "source_id": "issuer", "location": "synthetic page 1", "venue": "off_exchange"}
        anchor = {"code": self.code, "date": self.days[40].isoformat(), "nav": 1,
                  "source_id": "issuer", "location": "synthetic page 2"}
        registry = {"sources": {"issuer": source}, "anchors": [anchor], "corrections": [correction],
                    "code_info": self.policy["code_info"]}
        save(self.run / "executor/research-evidence.json", registry)
        self.expected["research-evidence.json"] = sha(self.run / "executor/research-evidence.json")
        self.manifest["executor_hashes"] = dict(self.expected)
        save(self.run / "evidence-used.json", {"sources": {"issuer": source}, "anchors": [anchor],
             "corrections": [correction], "document_verification": {"issuer": "matched_reviewed_digest"}})
        self.sources.append({"source_id": "issuer", "url": source["url"], "path": "raw/primary/issuer.pdf",
                             "sha256": sha(document), "bytes": document.stat().st_size,
                             "retrieved_at": "2026-10-03T19:00:00+08:00", "expected_sha256": source["expected_sha256"],
                             "review_status": "matched_reviewed_digest"})
        self.raw_nav[10]["equityReturn"] = 3
        if provider_fixed:
            self.raw_nav[10]["unitMoney"] = "分红：每份派现金0.03元"
        for i in range(10, len(self.days)):
            self.cumulative[i][1] = 1.03
            self.normalized[i]["cumulative_nav"] = 1.03
        self.normalized[10].update(distribution_per_share=.03, provider_daily_return=.03,
                                  distribution_text=self.raw_nav[10]["unitMoney"],
                                  issuer_correction={**correction, "source_url": source["url"],
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
        result = self.verify(protection=True)
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

    def test_later_real_account_changes_do_not_invalidate_archived_market_data(self):
        save(self.plan / "state/current.json", {"account_initialized": True, "holdings": [{"code": "000002"}]})
        self.assertEqual(self.verify()["readiness"]["market_research"], "passed")
        with self.assertRaisesRegex(audit.AuditError, "Protected account file changed"):
            self.verify(protection=True)

    def test_new_ledger_and_previously_absent_account_file_fail_publication_guard(self):
        jsonl(self.plan / "ledger/2026/10/part-000001.jsonl", [{"event_id": "new"}])
        with self.assertRaisesRegex(audit.AuditError, "ledger inventory"):
            self.verify(protection=True)
        (self.plan / "ledger/2026/10/part-000001.jsonl").unlink()
        save(self.plan / "profile.json", {"purpose": "research"})
        with self.assertRaisesRegex(audit.AuditError, "absent account file"):
            self.verify(protection=True)

    def test_inventory_guards_remain_active_under_optimized_python(self):
        (self.run / "unexpected.json").write_text("{}", encoding="utf-8")
        code = ("import importlib.util,sys,pathlib;"
                "s=importlib.util.spec_from_file_location('audit',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "m.verify_run(pathlib.Path(sys.argv[2]))")
        result = subprocess.run([sys.executable, "-B", "-O", "-c", code, str(SCRIPT), str(self.run)],
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Artifact inventory", result.stderr)

    def test_reviewed_distribution_handles_missing_note_and_already_fixed_provider(self):
        self.add_reviewed_distribution()
        self.assertEqual(self.verify()["official_NAV_anchors"]["passed"], 1)
        self.raw_nav[10]["unitMoney"] = "每10份派现金0.30元"
        self.normalized[10]["distribution_text"] = self.raw_nav[10]["unitMoney"]
        self.normalized[10]["issuer_correction"]["mode"] = "provider_already_matches"
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
        document = self.run / "raw/primary/issuer.pdf"
        document.write_bytes(document.read_bytes() + b"Changed issuer bytes.\n")
        self.sources[1].update(sha256=sha(document), bytes=document.stat().st_size)
        save(self.run / "source-manifest.json", self.sources)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "cannot be marked reviewed"):
            self.verify()

    def test_resealed_wrong_vendor_request_or_response_identity_is_rejected(self):
        correct_url = self.sources[0]["url"]
        self.sources[0]["url"] = "https://fund.eastmoney.com/pingzhongdata/000002.js"
        save(self.run / "source-manifest.json", self.sources)
        self.seal()
        with self.assertRaisesRegex(audit.AuditError, "code/request provenance"):
            self.verify()
        self.sources[0]["url"] = correct_url
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
                with self.assertRaisesRegex(audit.AuditError, "reviewed registry"):
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
                with self.assertRaisesRegex(audit.AuditError, "policy identity"):
                    self.verify()
                self.policy[field] = original


if __name__ == "__main__":
    unittest.main()
