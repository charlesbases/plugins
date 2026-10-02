"""Synthetic source-bound holdings/data contracts; no economic validity claim."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
from contracts import fingerprint, utc_now
import fund_universe
import industry_binding
import industry_data
import asset_domains
import source_documents
import source_fetch
import stage_validation
import test_industry_data as data_fixtures


class IndustryBindingTests(unittest.TestCase):
    setUp = data_fixtures.IndustryDataTests.setUp
    collect = data_fixtures.IndustryDataTests.collect
    make_review = data_fixtures.IndustryDataTests.make_review
    fact_review = data_fixtures.IndustryDataTests.fact_review
    open_policy = data_fixtures.IndustryDataTests.open_policy

    def verified_sector(self, label):
        quote = "本指数反映"+label+"行业的市场表现。"
        taxonomy_quote = "FixtureIssuerTaxonomy v2 "+label+" single_sector"
        raw = data_fixtures.source_archival_fixture.unicodePDF(
            ["CSI300 000300 CNY", "指数简介", quote, taxonomy_quote])
        url = data_fixtures.IDENTITY_URL
        capture = data_fixtures.transport(raw, url, "csi_index_identity", "application/pdf")
        with patch.object(source_fetch, "fetch", return_value=capture):
            reference = source_documents.capture_document(url, "csi_index_identity", self.artifacts, store=self.store)
        document = source_documents.read_extracted_document(reference, self.artifacts)
        domain = {"kind": "equity", "role": "single_sector", "return_target": {
            "transform": "price_log_return", "economic_meaning": "市场表现", "source_unit": "index_point",
            "canonical_unit": "index_point", "locator": "pdf/page/1/line/3", "quote": quote},
            "taxonomy": {"id": "FixtureIssuerTaxonomy", "version": "v2", "locator": "pdf/page/1/line/4",
                "quote": taxonomy_quote, "mappings": [{"source_label": label, "entity_id": label, "role": "single_sector"}]}}
        contract = {"sector_id": label, "identity_quote": "CSI300 000300 CNY",
                    "identity_document_ref": reference, "asset_domain": domain}
        relation = asset_domains.verify_definition(contract, document, self.artifacts, utc_now())
        return {"sector_id": label, "asset_domain": domain, "benchmark_role": "sector_index",
                "sector_relation": relation, "sector_relation_scope": relation["scope"]}

    def identity(self):
        url = "https://www.cmfchina.com/fundarticle/123456"
        raw = ('<p>基金代码:123456 报告期末:2025-12-31</p>'
               '<table><tr><th>行业类别</th><th>占基金资产净值比例</th></tr>'
               '<tr><td>工业</td><td>40%</td></tr><tr><td>信息技术</td><td>10%</td></tr></table>').encode()
        capture = data_fixtures.transport(raw, url, "issuer_cmfchina", "text/html")
        with patch.object(source_fetch, "fetch", return_value=capture):
            reference = source_documents.capture_document(url, "issuer_cmfchina", self.artifacts, store=self.store)
        document = source_documents.read_extracted_document(reference, self.artifacts)
        mapping = {"code": "123456", "source_id": "issuer_cmfchina", "url": url, "role": "holdings",
                   "locators": [row["locator"] for row in document["blocks"]]}
        return fund_universe._apply_issuer(reference, mapping, {"code": "123456", "currency": "CNY"}, self.artifacts), document

    def test_exact_holdings_weights_keep_original_capture_clock(self):
        identity, document = self.identity()
        sectors = [self.verified_sector("工业"), self.verified_sector("信息技术")]
        rows = industry_binding.build_exposures({"123456": identity}, {"sectors": sectors},
                                               self.artifacts, at=utc_now())["123456"]
        self.assertEqual({row["sector_id"]: row["weight"] for row in rows}, {"工业": "0.4", "信息技术": "0.1"})
        clocks = {sector["sector_id"]: max(document["retrieved_at"], sector["sector_relation"]["known_at"]) for sector in sectors}
        self.assertTrue(all(row["known_at"] == clocks[row["sector_id"]] for row in rows))
        self.assertEqual({row["as_of_date"] for row in rows}, {"2025-12-31"})
        self.assertEqual(identity["sector_exposures"]["unmapped_weight"], "0.5")
        self.assertTrue(all(row["evidence_refs"] and row["basis"] == "historical_disclosure" for row in rows))
        self.assertTrue(all(row["actual_exposure_status"] == "historical_disclosed_not_current" for row in rows))

    def test_sector_name_mismatch_never_becomes_assumed_exposure(self):
        identity, _ = self.identity()
        result = industry_binding.derive_exposures({"123456": identity}, {"sectors": [self.verified_sector("半导体主题")]},
                                                   self.artifacts, at=utc_now())
        self.assertEqual(result, {"123456": []})

    def test_changed_weight_and_future_source_are_rejected(self):
        identity, _ = self.identity()
        changed = copy.deepcopy(identity)
        changed["sector_exposures"]["weights"]["工业"] = "0.9"
        with self.assertRaisesRegex(ValueError, "differ from original holdings"):
            industry_binding.build_exposures({"123456": changed}, {"sectors": [self.verified_sector("工业")]}, self.artifacts, at=utc_now())
        with self.assertRaisesRegex(ValueError, "captured after decision"):
            industry_binding.build_exposures({"123456": identity}, {"sectors": [self.verified_sector("工业")]},
                                             self.artifacts, at="2020-01-01T00:00:00Z")

    def test_model_bridge_preserves_base_and_rejects_changed_nav(self):
        review, spec = self.fact_review()
        sector = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "binding-sector")
        base = {"nav": {"123456": [{"date": "2025-01-02", "nav": 1.0}]}, "features": [], "code_info": {"123456": {"currency": "CNY"}}}
        at = utc_now()
        value = industry_binding.bind_numerical_data(base, sector, self.artifacts, store=self.store, at=at, spec=spec)
        inputs = {"base_data_ref": self.artifacts.put_json(base), "industry_record_ref": self.artifacts.put_json(sector),
                  "spec": spec, "decision_at": at}
        self.assertEqual({key: value[key] for key in base}, base)
        self.assertEqual(industry_binding.validate_model_data(value, inputs, self.store, self.artifacts)["status"], "passed")
        stage = stage_validation.validate("model-data", value, inputs, self.store, self.artifacts)
        self.assertEqual(stage["status"], "partial")
        self.assertFalse(stage["readiness"]["trade_ready"])
        changed = copy.deepcopy(value)
        changed["nav"]["123456"][0]["nav"] = 2.0
        with self.assertRaisesRegex(ValueError, "differs from independent"):
            industry_binding.validate_model_data(changed, inputs, self.store, self.artifacts)

    def test_authoritative_analysis_and_industry_stages_keep_source_gaps_partial(self):
        run = self.open_policy("binding-run", copy.deepcopy(self.news_run["news_policy"]))
        review, spec = self.fact_review()
        proof = stage_validation.validate("analysis-run", run, {"run_id": "binding-run", "news_policy": spec["news"], "account_id": "main"},
                                          self.store, self.artifacts)
        self.assertEqual(proof["status"], "passed")
        sector = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "bound-sector")
        proof = stage_validation.validate("industry-data", sector, {"spec": spec, "news_review_id": review["review_id"],
            "analysis_run_id": "binding-run"}, self.store, self.artifacts)
        self.assertEqual(proof["status"], "partial")
        self.assertTrue(proof["required_actions"])
        with self.assertRaisesRegex(ValueError, "Run caller policy"):
            stage_validation.validate("analysis-run", run, {"run_id": "binding-run", "news_policy": {**spec["news"], "max_age_seconds": 99},
                "account_id": "main"}, self.store, self.artifacts)

    def test_proxy_and_unverified_sector_do_not_enter_the_fund_bridge(self):
        identity, _ = self.identity()
        for missing_mapping in (False, True):
            sector = self.verified_sector("工业")
            if missing_mapping:
                sector["asset_domain"]["taxonomy"]["mappings"] = []
            else:
                sector["sector_relation"]["status"] = "partial"
            result = industry_binding.build_exposures({"123456": identity}, {"sectors": [sector]}, self.artifacts, at=utc_now())
            self.assertEqual(result["123456"], [])


if __name__ == "__main__":
    unittest.main()
