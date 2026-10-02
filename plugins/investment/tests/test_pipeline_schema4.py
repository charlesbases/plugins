"""Public schema4 integration with an explicitly synthetic network boundary.

No investment-validity claim: constant NAV makes cash a known feasible option.
Every source, fee quote and news claim below is labelled as a synthetic fixture.
The CLI, Store, source extraction, research audit, fitting, optimizer, bundle
replay and report checks use production implementations.
"""
import contextlib
import copy
import datetime as dt
import hashlib
import html
import io
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
from artifacts import Artifacts
from contracts import fingerprint, instant, utc_now
from file_io import read_object
from state_store import Store
import investment
import source_fetch
import test_numerical_schema4 as numerical_fixture
import test_research as raw_fixture

CODE = raw_fixture.CODE
NEWS_URL = "https://www.gov.cn/yaowen/liebiao/202001/content_123456.htm"
FEE_URL = "https://www.cmfchina.com/web/fundDetail/" + CODE + "/index.html"
NEWS_BODY = "合成集成测试资料：黄金产品可以纳入研究；此文字不是现实新闻，也不是收益预测。" * 4


def fixture_contract_text():
    contract, blocks = numerical_fixture.SourceFeeRegression().contract()
    contract["code"] = contract["normalized"]["subject"]["code"] = CODE
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    calendar = contract["normalized"]["execution_calendar"]
    first, last = today-dt.timedelta(days=10), today+dt.timedelta(days=190)
    calendar.update(id="synthetic_current_normal_calendar", coverage_start=first.isoformat(), coverage_end=last.isoformat(),
        open_dates=[(first+dt.timedelta(days=i)).isoformat() for i in range((last-first).days+1)
                    if (first+dt.timedelta(days=i)).weekday() < 5])
    blocks["calendar"]["text"] = json.dumps({key: value for key, value in calendar.items() if key != "evidence_ref"})
    text = "<html>" + "".join("<p>" + html.escape(blocks[key]["text"].replace("000001", CODE).replace("\n", "；"))
                              + "</p>" for key in ("dealing", "entry", "exit", "calendar")) + "</html>"
    return contract, text


def source_capture(url, source_id, **kwargs):
    """Only this external transport boundary is replaced by test data."""
    if source_id in {"cn_state_council", "csi_index_identity", "eastmoney_index_daily", "eastmoney_nav"} or source_id == "issuer_cmfchina" and "/fundarticle/" in url:
        import source_archival_fixture
        extra = source_archival_fixture.extra_capture(url, source_id)
        if extra is not None:
            return extra
    if source_id == "cn_state_council":
        text = '<html><head><meta name="PubDate" content="2020-01-01"><title>合成新闻</title></head><body><div id="UCAP-CONTENT">' + NEWS_BODY + "</div></body></html>"
        media = "text/html"
    elif source_id == "issuer_cmfchina":
        _, text = fixture_contract_text()
        media = "text/html"
    elif source_id == "eastmoney_catalog":
        text = 'var r = ' + json.dumps([[CODE, "SYNTHETIC", "Synthetic constant NAV fixture", "指数型-其他", "SYNTHETIC"]]) + ";"
        media = "application/javascript"
    elif source_id == "eastmoney_nav":
        end = (dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() - dt.timedelta(days=1))
        first = end - dt.timedelta(days=319)
        observations = []
        cumulative = []
        for offset in range(320):
            day = first + dt.timedelta(days=offset)
            stamp = int(dt.datetime.combine(day, dt.time(), dt.timezone(dt.timedelta(hours=8))).timestamp()*1000)
            observations.append({"x": stamp, "y": 2., "unitMoney": "", "equityReturn": 0.})
            cumulative.append([stamp, 2.])
        text = ('var fS_code = "' + CODE + '";\nvar fS_name = "Synthetic constant NAV fixture";\n'
                'var Data_netWorthTrend = ' + json.dumps(observations) + ";\nvar Data_ACWorthTrend = " + json.dumps(cumulative) + ";")
        media = "application/javascript"
    else:
        return raw_fixture.synthetic_capture(url, source_id, **kwargs)
    raw = text.encode("utf8")
    return {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": media, "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
            "retrieved_at": utc_now(), "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "text_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": raw, "text": text}


class PublicSchema4Tests(unittest.TestCase):
    def setUp(self):
        debug = os.environ.get("INVESTMENT_PUBLIC_DEBUG_DIR")
        if debug:
            self.directory = Path(debug).resolve()
            self.directory.mkdir(parents=True,exist_ok=False)
            import portfolio_paths
            original_frames,original_fit = portfolio_paths._frames,portfolio_paths._fit_at
            def capture_frames(context,data,samples,contract):
                captured=original_frames(context,data,samples,contract)
                (self.directory/"source-path-inputs.json").write_text(json.dumps({"context":context,"data":data,"samples":samples,"contract":contract},ensure_ascii=False),encoding="utf-8")
                (self.directory/"source-path-frames.json").write_text(json.dumps({"frames":captured[0],"unavailable":captured[1]},ensure_ascii=False),encoding="utf-8")
                return captured
            def capture_fit(frames,codes,day,policy,cash_keys,groups,ceiling=None):
                eligible=portfolio_paths._eligible(frames,codes,day,policy,ceiling)
                with (self.directory/"source-path-fit-probes.jsonl").open("a",encoding="utf-8") as stream:
                    stream.write(json.dumps({"day":day,"policy":policy,"frame_count":len(frames),"eligible_count":len(eligible),"eligible_dates":sorted({row["decision_date"] for row in eligible}),"gap":portfolio_paths._training_gap(frames,codes,day,policy)})+chr(10))
                return original_fit(frames,codes,day,policy,cash_keys,groups,ceiling)
            self.enterContext(patch.object(portfolio_paths,"_frames",side_effect=capture_frames))
            self.enterContext(patch.object(portfolio_paths,"_fit_at",side_effect=capture_fit))
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self.directory = Path(temporary.name)
        self.root = self.directory / "synthetic-cache"
        self.plan = "synthetic-only"
        self.artifacts = Artifacts(self.root / "plans" / self.plan)
        self.enterContext(patch.object(source_fetch, "fetch", side_effect=source_capture))
        self.policy = {"mode": "sealed_inputs", "sources": ["cn_state_council"],
                       "required_source_groups": {"synthetic-policy": ["cn_state_council"]}, "max_age_seconds": 400*86400}
        self.spec = read_object(SCRIPTS.parent / "references/strategy.example.json")
        self.spec["news"] = self.policy
        self.spec["benchmark"] = {"rule": "fixed_weights", "weights": {CODE: "1"}, "cash_weight": "0", "rebalance_dates": []}
        # A declared engineering protocol exercises production qualification;
        # the unmodified illustrative example has its own rejection regression.
        self.spec["trade_policy"]["registration"] = "declared"
        self.spec["training"].update(train_window_days=180, min_train_dates=16, cv_folds=2,
            alpha_grid=[0.01], l1_ratio_grid=[0.5], min_joint_dates=8)
        import industry_model
        self.spec["industry"]["training"] = {**self.spec["training"], "feature_names": industry_model.FEATURE_NAMES}
        self.spec["planning"].update(primary_horizon_days=3)
        self.spec["allocation"]["funding_levels"] = ["0"]
        now = utc_now()
        self.confirmation = {"message": "SYNTHETIC isolated test account only; no real account facts", "confirmed_at": now}
        self.call("status", "status", {})
        self.call("profile", "profile_initialize", {"principal": "10000", "currency": "CNY",
            "loss_tolerance": "0.25", "as_of": now, "user_source": self.confirmation,
            "confirmed_initial_all_cash": True, "account_hash": None})
        self.call("plan", "plan_update", {"constraints": {"platform": "TT", "currency": "CNY",
            "goal": "SYNTHETIC constant NAV integration",
            "excluded_categories": [], "position_limits": self.spec["constraints"]},
            "current_constraints_hash": None, "user_source": self.confirmation})

    def call(self, identity, operation, payload, expected_code=0):
        if operation == "daily_review" and payload.get("news_review_id") and getattr(self, "analysis_run_id", None):
            payload = {**payload, "analysis_run_id": self.analysis_run_id}
            if getattr(self, "industry_data_id", None):
                payload["industry_data_id"] = self.industry_data_id
        path = self.directory / (identity + ".json")
        request = {"schema_version": 4, "request_id": "synthetic:" + identity, "operation": operation, "payload": payload}
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = investment.main(["run", "--root", str(self.root), "--plan", self.plan, "--request", str(path)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, expected_code, {"status": result.get("status"), "reason": result.get("reason"),
                                             "cause": result.get("validation_failure", {}).get("cause")})
        self.assertNotEqual(result["status"], "internal_error", result)
        return result

    def prepare_news_and_discovery(self, expected_candidates=None):
        archival = None
        if self._testMethodName in {"test_public_daily_reproduces_source_numeric_report_and_idempotent_result",
                                  "test_unpriced_nonheld_peer_does_not_block_another_qualified_product"}:
            import source_archival_fixture
            archival = source_archival_fixture.seed_archives(self)
        opened = self.call("analysis-open", "analysis_start", {"news_policy": self.policy})
        self.analysis_run_id = opened["run"]["run_id"]
        collected = self.call("news", "news_collect", {"cutoff_at": opened["run"]["publish_cutoff"], "run_id": self.analysis_run_id,
            "window_start": "2020-01-01T00:00:00Z", "sources": [{"source_id": "cn_state_council", "urls": [NEWS_URL]}],
            "required_source_groups": self.policy["required_source_groups"]})
        version = collected["versions"][0]
        until = (instant(utc_now()) + dt.timedelta(hours=1)).isoformat()
        event = {"event_key": None, "version_ids": [version["version_id"]], "claim_ids": ["synthetic-claim"],
                 "event_at": None, "effective_from": None, "effective_until": None, "review_by": until,
                 "supersedes": [], "retracts": [], "temporal_evidence": []}
        claim = {"id": "synthetic-claim", "kind": "fact", "text": NEWS_BODY[:25],
                 "version_id": version["version_id"], "quote": NEWS_BODY[:25], "categories": ["黄金"], "direction": "increase",
                 "counterevidence": [{"missing_reason": "Synthetic fixture defines a constant NAV world"}],
                 "reviewer": "synthetic-test", "review_method": "fixture_quote_consistency"}
        thesis = {"thesis_id": "synthetic-gold", "sector_id": "黄金", "kind": "asset_class", "label": "黄金", "direction": "increase",
                  "horizon_days": 3, "search_terms": ["黄金"], "claim_ids": ["synthetic-claim"],
                  "event_ids": [version["document_id"]], "required_source_groups": ["synthetic-policy"],
                  "valid_until": until, "event_basis": "standing_policy"}
        assessed = self.call("assessment", "news_assess", {"collection_id": "synthetic:news", "run_id": self.analysis_run_id,
            "claims": [claim], "industry_theses": [thesis], "events": [event],
            "economic_observations": [{"event_id": version["document_id"], "sector_ids": ["黄金"], "category": "industry_policy",
                "metric_id": "policy_action", "metric_label": "黄金行业政策", "current": None, "prior": None, "expectation": None,
                "action": {"version_id": version["version_id"], "quote": __import__("source_archival_fixture").ECONOMIC_QUOTE, "value": "introduced"}}]})
        self.assertEqual(assessed["status"], "assessed")
        discovery_payload = {"news_review_id": "synthetic:assessment", "news_policy": self.policy,
                             "universe_policy": self.spec["universe_policy"], "monitoring_codes": [CODE]}
        if archival:
            subjects = set(expected_candidates if expected_candidates is not None else [CODE])
            discovery_payload["issuer_disclosures"] = [row for row in archival["issuer_disclosures"] if row["code"] in subjects]
        discovered = self.call("discovery", "fund_discover", discovery_payload)
        self.assertEqual(discovered["completed_group_candidate_codes"], [CODE] if expected_candidates is None else expected_candidates)
        if archival:
            sector = self.call("industry-current", "industry_prepare", {"analysis_run_id": self.analysis_run_id,
                "news_review_id": "synthetic:assessment", "spec": self.spec, "benchmark_contracts": archival["benchmark_contracts"]})
            self.assertEqual(sector["status"], "ready", sector["required_actions"])
            self.industry_data_id = "synthetic:industry-current"
        return discovered

    def contract(self):
        captured = self.call("source-fees", "source_capture", {"url": FEE_URL, "source_id": "issuer_cmfchina"})
        self.assertEqual(captured["status"], "extracted")
        contract, _ = fixture_contract_text()
        contract["quote_observed_at"] = captured["document"]["retrieved_at"]
        locators = {"dealing": "html/block/0", "entry": "html/block/1", "exit": "html/block/2", "calendar": "html/block/3"}
        for value in contract["bindings"].values():
            for binding in value if isinstance(value, list) else [value]:
                binding.update(document_ref=captured["document_ref"], locator=locators[binding["locator"]])
        contract["normalized"]["execution_calendar"]["evidence_ref"] = {
            "document_ref": captured["document_ref"], "locator": locators["calendar"]}
        contract["contract_hash"] = fingerprint({key: value for key, value in contract.items() if key != "contract_hash"})
        return contract

    def research_request(self):
        end = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() - dt.timedelta(days=1)
        return {"start": (end-dt.timedelta(days=319)).isoformat(), "end": end.isoformat(),
                "horizons": [3], "lookback": 120}

    def _verified_nav_archive_import(self):
        import source_archival_fixture,research,allocation_runtime,allocation_market
        archival = source_archival_fixture.seed_archives(self,capture_offsets=(123,127))
        self.assertEqual(archival["fund_nav_archive_capture_count"],2)
        self.call("current-nav-discovery","fund_discover",{
            "news_review_id":"synthetic:archive-assessment","news_policy":self.policy,
            "universe_policy":self.spec["universe_policy"],"monitoring_codes":[CODE],
            "issuer_disclosures":[row for row in archival["issuer_disclosures"] if row["code"]==CODE]})
        prepared = self.call("current-nav-import","research_prepare",{"discovery_id":"synthetic:current-nav-discovery",**self.research_request()})
        store = Store(self.root,self.plan)
        _,_,_,_,loaded,_ = allocation_runtime.load_data(self.root.resolve(),store.base,prepared["run_path"])
        row = loaded[CODE][0]
        original = next(version for version in row["source_versions"] if version["available_at"] is not None)
        prior = (dt.datetime.fromisoformat(original["available_at"])-dt.timedelta(seconds=1)).isoformat()
        self.assertIsNone(allocation_market.nav_snapshot(row,prior,require_known=True))
        known = allocation_market.nav_snapshot(row,original["available_at"],require_known=True)
        self.assertEqual((known["nav"],known["known_at"]),(2.,original["available_at"]))
        run = research.resolve_run(store.base,prepared["run_path"])
        sources = json.loads((run/"source-manifest.json").read_text(encoding="utf8"))
        source = next(source for source in sources if source["source_id"]==original["raw_ref"]["source_id"])
        self.assertEqual(hashlib.sha256(research.research_audit.source_bytes(run,source)).hexdigest(),original["raw_ref"]["sha256"])
        self.assertEqual(self.call("current-nav-audit","research_verify",{"run_path":prepared["run_path"]})["status"],"passed")
        return prepared,store

    def test_virtual_nav_archives_enter_normal_research_import(self):
        self._verified_nav_archive_import()

    def test_market_reconstructs_original_compressed_nav_captures(self):
        import research,verify,copy
        _,store = self._verified_nav_archive_import()
        result = self.call("compressed-nav-market","market_prepare",{
            "discovery_id":"synthetic:current-nav-discovery","research":self.research_request(),
            "market_terms":{"currency":"CNY","contracts":[]}})
        record = result["record"]
        run = research.resolve_run(store.base,record["provenance"]["research_ref"])
        sources = json.loads((run/"source-manifest.json").read_text(encoding="utf8"))
        expected = {row["source_id"]:row["sha256"] for row in sources if row.get("path")}
        published = {row["source_id"]:row for row in record["evidence_refs"] if row["role"]=="public_source"}
        self.assertEqual({key:row["artifact"]["sha256"] for key,row in published.items()},expected)
        compressed = [row for row in sources if row.get("archive_storage")]
        self.assertTrue(compressed)
        for source in compressed:
            artifact = published[source["source_id"]]["artifact"]
            self.assertEqual(self.artifacts.read(artifact),research.research_audit.source_bytes(run,source))
            self.assertNotEqual(artifact["sha256"],source["archive_storage"]["sha256"])
        wrong = copy.deepcopy(record)
        source = compressed[0]
        encoded = self.artifacts.put_bytes((run/source["path"]).read_bytes())
        next(row for row in wrong["evidence_refs"] if row["source_id"]==source["source_id"])["artifact"] = encoded
        wrong["market_ref"]["source_hashes"][source["source_id"]] = encoded["sha256"]
        discovery = store.get("fund_discovery","synthetic:current-nav-discovery")
        with self.assertRaisesRegex(ValueError,"Published market source references differ"):
            verify.verify_research_binding({"market_record_ref":self.artifacts.put_json(wrong),"data_ref":result["data_ref"],
                "discovery_ref":self.artifacts.put_json(discovery),"decision_at":record["market_ref"]["observed_at"],"market_id":fingerprint(wrong)},self.artifacts)

    def test_public_daily_reproduces_source_numeric_report_and_idempotent_result(self):
        self.prepare_news_and_discovery()
        contract = self.contract()
        payload = {"spec": self.spec, "news_review_id": "synthetic:assessment", "discovery_id": "synthetic:discovery",
                   "research": self.research_request(), "market_terms": {"currency": "CNY", "contracts": [contract]}}
        result = self.call("daily", "daily_review", payload)
        self.assertEqual(result["status"], "conditional_research", result)
        bundle = self.artifacts.read_json(result["bundle_ref"])
        calculation = self.artifacts.read_json(bundle["calculation_ref"])
        self.assertEqual(calculation["fitting"]["status"], "research_ready")
        self.assertEqual(bundle["context"]["risk_state"]["principal_floor"], "7500")
        self.assertEqual(bundle["orders"]["orders"], [])
        self.assertEqual(result, self.call("daily", "daily_review", payload))
        audited = self.call("audit", "audit", {"decision_id": "synthetic:daily"})
        self.assertEqual(audited["decision"]["status"], "passed")
        self.assertEqual(audited["report"]["status"], "passed")

    def test_public_research_requires_discovery_and_rejects_free_codes(self):
        self.prepare_news_and_discovery()
        prepared = self.call("research", "research_prepare", {"discovery_id": "synthetic:discovery", **self.research_request()})
        self.assertEqual(prepared["status"], "market_research_prepared")
        verified = self.call("research-audit", "research_verify", {"run_path": prepared["run_path"]})
        self.assertEqual(verified["status"], "passed")
        rejected = self.call("free-codes", "research_prepare", {"codes": [CODE], **self.research_request()}, expected_code=1)
        self.assertEqual(rejected["status"], "invalid_input")



    def test_public_absent_contract_is_auditable_pending_evidence_not_zero_fees(self):
        self.prepare_news_and_discovery()
        payload = {"spec": self.spec, "news_review_id": "synthetic:assessment", "discovery_id": "synthetic:discovery",
                   "research": self.research_request(), "market_terms": {"currency": "CNY", "contracts": []}}
        result = self.call("missing-contract-daily", "daily_review", payload)
        self.assertEqual(result["status"], "partial", result)
        bundle = self.artifacts.read_json(result["bundle_ref"])
        readiness_inputs = self.artifacts.read_json(bundle["readiness_inputs_ref"])
        self.assertEqual(bundle["decision_at"], readiness_inputs["decision_at"])
        self.assertEqual(self.artifacts.read_json(bundle["readiness_ref"])["candidate_families"], [])
        market = self.artifacts.read_json(bundle["market_record_ref"])
        self.assertEqual(market["market_ref"]["terms"], [])
        self.assertEqual(bundle["term_gaps"][0]["code"], CODE)
        self.assertEqual(bundle["term_gaps"][0]["kind"], "contract_not_provided")
        self.assertEqual(bundle["term_gaps"][0]["required_actions"][0]["action"],
                         "capture_and_inspect_current_product_dealing_terms")
        self.assertEqual(bundle["orders"]["orders"], [])
        self.assertEqual(self.call("missing-contract-audit", "audit",
                         {"decision_id": "synthetic:missing-contract-daily"})["decision"]["status"], "passed")

    def test_unpriced_nonheld_peer_does_not_block_another_qualified_product(self):
        other = raw_fixture.OTHER_CODE
        def network(url, source_id, **kwargs):
            captured = source_capture(url, source_id, **kwargs)
            if source_id == "eastmoney_catalog":
                text = "var r = " + json.dumps([[code, "SYNTHETIC", "Synthetic constant NAV fixture",
                    "指数型-其他", "SYNTHETIC"] for code in (CODE, other)]) + ";"
            elif source_id == "eastmoney_nav" and other in url:
                text = captured["text"].replace('var fS_code = "'+CODE+'";', 'var fS_code = "'+other+'";')
            else:
                return captured
            raw = text.encode("utf8")
            captured.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(),
                            text_sha256=hashlib.sha256(raw).hexdigest())
            return captured
        with patch.object(source_fetch, "fetch", side_effect=network):
            self.prepare_news_and_discovery(expected_candidates=sorted([CODE, other]))
            contract = self.contract()
            payload = {"spec": self.spec, "news_review_id": "synthetic:assessment", "discovery_id": "synthetic:discovery",
                       "research": self.research_request(), "market_terms": {"currency": "CNY", "contracts": [contract]}}
            result = self.call("partial-contract-daily", "daily_review", payload)
        self.assertEqual(result["status"], "conditional_research", result)
        bundle = self.artifacts.read_json(result["bundle_ref"])
        selected = self.artifacts.read_json(bundle["selection_ref"])
        self.assertEqual(selected["eligible_buy_codes"], [CODE])
        self.assertFalse(selected["per_code"][other]["eligible"])
        self.assertEqual(bundle["context"]["allocation_codes"], [CODE])
        self.assertEqual(bundle["term_gaps"][0]["code"], other)
        self.assertEqual(self.call("partial-contract-audit", "audit",
                         {"decision_id": "synthetic:partial-contract-daily"})["decision"]["status"], "passed")


    def test_clearing_plan_exclusion_requires_new_discovery_before_daily(self):
        status = self.call("plan-before-exclusion", "status", {})
        old_constraints = copy.deepcopy(status["plan_constraints"]["constraints"])
        old_constraints["excluded_categories"] = ["指数型-其他"]
        self.call("exclude-category", "plan_update", {"constraints": old_constraints,
            "current_constraints_hash": fingerprint(status["plan_constraints"]),
            "user_source": {"message": "SYNTHETIC exclude category revision", "confirmed_at": utc_now()}})
        self.prepare_news_and_discovery(expected_candidates=[])
        before_clear = self.call("plan-before-clear", "status", {})
        current_constraints = copy.deepcopy(before_clear["plan_constraints"]["constraints"])
        current_constraints["excluded_categories"] = []
        self.call("clear-category", "plan_update", {"constraints": current_constraints,
            "current_constraints_hash": fingerprint(before_clear["plan_constraints"]),
            "user_source": {"message": "SYNTHETIC remove category exclusion", "confirmed_at": utc_now()}})
        payload = {"spec": self.spec, "news_review_id": "synthetic:assessment", "discovery_id": "synthetic:discovery",
                   "research": self.research_request(), "market_terms": {"currency": "CNY", "contracts": []}}
        stale = self.call("stale-plan-daily", "daily_review", payload, expected_code=1)
        self.assertEqual(stale["status"], "stale_snapshot")
        self.assertEqual(stale["required_actions"][0]["action"], "fund_discover")
        refreshed = self.call("refreshed-discovery", "fund_discover", {
            "news_review_id": "synthetic:assessment", "news_policy": self.policy,
            "universe_policy": self.spec["universe_policy"], "monitoring_codes": [CODE]})
        self.assertEqual(refreshed["completed_group_candidate_codes"], [CODE])
        self.assertNotIn("category_excluded_by_plan", refreshed["prequalification"][CODE]["reasons"])

if __name__ == "__main__":
    unittest.main()
