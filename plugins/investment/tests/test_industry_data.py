"""Synthetic transport fixtures test source boundaries, never economic readiness."""
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
from contracts import fingerprint, instant, utc_now
import industry_data
import news
import source_documents
import source_fetch
import test_news as news_fixtures
import test_source_documents as document_fixtures
import source_archival_fixture

IDENTITY_URL = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/en/000300factsheeten.pdf"


def transport(raw, url, source_id, media_type="application/json"):
    text = None if media_type == "application/pdf" else raw.decode("utf-8")
    return {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": media_type, "encoding": "utf-8" if text else None,
            "transport": "HTTPS_default_certificate_validation", "retrieved_at": utc_now(),
            "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None,
            "bytes": len(raw), "raw_bytes": raw, "text": text}


class IndustryDataTests(unittest.TestCase):
    collect = news_fixtures.NewsTests.collect
    make_review = news_fixtures.NewsTests.make_review
    open_policy = news_fixtures.NewsTests.open_policy

    def setUp(self):
        news_fixtures.NewsTests.setUp(self)

    def fact_review(self):
        review, spec = self.make_review()
        request = self.artifacts.read_json(review["request_ref"])
        request["claims"][0]["kind"] = "fact"
        request["claims"][0]["text"] = request["claims"][0]["quote"]
        return news.assess(request, self.store, self.artifacts, "fact-review"), spec

    def contract(self):
        quote = "本指数反映合成产业行业的市场表现。"
        raw = source_archival_fixture.unicodePDF(["CSI300 000300 CNY", "指数简介", quote])
        with patch.object(source_fetch, "fetch", return_value=transport(raw, IDENTITY_URL, "csi_index_identity", "application/pdf")):
            reference = source_documents.capture_document(IDENTITY_URL, "csi_index_identity", self.artifacts, store=self.store)
        return {"sector_id": "合成产业", "thesis_ids": ["equity"], "benchmark_id": "fixture-csi300",
                "index_code": "000300", "index_name": "CSI300", "provider_security_id": "1.000300",
                "return_definition": "price_return", "currency": "CNY", "calendar_id": "CN_A_SHARE",
                "timezone": "Asia/Shanghai", "identity_quote": "CSI300 000300 CNY", "identity_document_ref": reference,
                "start": "2025-01-02", "end": "2025-01-03", "benchmark_role": "sector_index",
                "asset_domain": {"kind": "equity", "role": "single_sector", "return_target": {
                    "transform": "price_log_return", "economic_meaning": "市场表现", "source_unit": "index_point",
                    "canonical_unit": "index_point", "locator": "pdf/page/1/line/3", "quote": quote}},
                "sector_relation": {"locator": "pdf/page/1/line/3", "quote": quote, "sector_label": "合成产业"}}

    def quotes(self, url, source_id, **kwargs):
        raw = json.dumps({"rc": 0, "data": {"code": "000300", "name": "CSI300", "market": 1,
             "klines": ["2025-01-02,100,100,101,99,10,1000", "2025-01-03,100,110,111,99,12,1300"]}}).encode()
        return transport(raw, url, source_id)

    def prepare(self, operation="sector-one"):
        review, spec = self.fact_review()
        contract = self.contract()
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, operation, [contract])
        return value, spec, contract

    def test_source_price_returns_keep_capture_time_and_partial_pit_status(self):
        value, spec, contract = self.prepare()
        bundle = industry_data.validate_sector_data(value, spec, utc_now(), self.artifacts, store=self.store)
        sector = bundle["sectors"][0]
        self.assertAlmostEqual(sector["returns"][0]["return"], 0.1)
        self.assertEqual(sector["return_definition"], "price_return")
        self.assertEqual(sector["sector_relation_scope"], "source_versioned_asset_definition")
        self.assertEqual(sector["sector_relation"]["status"], "verified")
        self.assertGreater(instant(sector["prices"][0]["available_at"]).year, 2025)
        self.assertEqual(value["status"], "partial")
        self.assertFalse(value["trade_ready"])
        self.assertIn("accumulate_archived_point_in_time_sector_captures", [row["action"] for row in value["required_actions"]])
        self.assertTrue(sector["coverage"]["absence_proven"])
        self.assertFalse(sector["coverage"]["source_time_window_complete"])
        self.assertEqual(sector["coverage"]["zero_meaning"], "no_observed_matching_event_in_this_set")

    def test_missing_contracts_produce_actionable_partial_without_invented_prices(self):
        review, spec = self.fact_review()
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("no registered contract to fetch")):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "missing")
        bundle = industry_data.validate_sector_data(value, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(bundle["sectors"], [])
        self.assertEqual(value["status"], "partial")
        self.assertIn("supply_source_verified_sector_benchmark_contracts", [row["action"] for row in value["required_actions"]])

    def test_nonadjusted_provider_cannot_be_labelled_total_return(self):
        review, spec = self.fact_review()
        contract = {**self.contract(), "return_definition": "total_return"}
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("unsupported semantics cannot fetch")):
            with self.assertRaisesRegex(ValueError, "cannot supply total_return"):
                industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "total-return", [contract])

    def test_resealed_return_and_future_capture_mutations_are_rejected(self):
        value, spec, contract = self.prepare()
        bundle = self.artifacts.read_json(value["data_ref"])
        bundle["sectors"][0]["returns"][0]["return"] = 9.0
        mutated = {**value, "data_ref": self.artifacts.put_json(bundle), "bundle_hash": fingerprint(bundle)}
        with self.assertRaisesRegex(ValueError, "differ from source"):
            industry_data.validate_sector_data(mutated, spec, utc_now(), self.artifacts, store=self.store)
        entries = self.artifacts.read_json(value["entries_ref"])
        record = self.artifacts.read_json(entries[0]["capture_refs"][0])
        record["retrieved_at"] = "2099-01-01T00:00:00Z"
        entries[0]["capture_refs"] = [self.artifacts.put_json(record)]
        mutated = {**value, "entries_ref": self.artifacts.put_json(entries)}
        with self.assertRaisesRegex(ValueError, "after the decision"):
            industry_data.validate_sector_data(mutated, spec, utc_now(), self.artifacts, store=self.store)

    def test_factual_reassessment_preserves_first_feature_clock(self):
        review, spec = self.fact_review()
        original = news.factual_event_features(review, spec, utc_now(), self.artifacts, store=self.store)
        request = self.artifacts.read_json(review["request_ref"])
        repeated = news.assess(request, self.store, self.artifacts, "fact-review-repeat")
        result = news.factual_event_features(repeated, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(original["features"], result["features"])
        self.assertEqual(len(result["features"]), 1)
        feature = result["features"][0]
        self.assertGreaterEqual(instant(feature["first_available_at"]), instant(review["reviewed_at"]))
        self.assertLess(instant(feature["revision_known_at"]), instant(feature["fact_first_known_at"]))
        self.assertNotIn("direction", feature)
        self.assertNotIn("value", feature)

    def test_retry_reuses_frozen_prices_new_operation_fetches_and_preserves_archive(self):
        value, spec, contract = self.prepare()
        request = self.artifacts.read_json(value["request_ref"])
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("same operation must reuse")):
            repeated = industry_data.prepare_sector_data(request["review"], spec, self.store, self.artifacts,
                                                        value["operation_id"], [contract])
        self.assertEqual(repeated, value)
        with patch.object(source_fetch, "fetch", side_effect=self.quotes) as fetch:
            newer = industry_data.prepare_sector_data(request["review"], spec, self.store, self.artifacts, "sector-two", [contract])
        self.assertEqual(fetch.call_count, 1)
        first = self.artifacts.read_json(value["data_ref"])["sectors"][0]
        second = self.artifacts.read_json(newer["data_ref"])["sectors"][0]
        self.assertEqual(first["prices"][0]["available_at"], second["prices"][0]["available_at"])
        self.assertEqual(len(second["features"]), 1)

    def test_fact_effective_clock_keeps_source_date_precision(self):
        literal = "2020年1月1日生效 2020-01-01T09:30:00+08:00"
        self.collect(body=news_fixtures.BODY+literal)
        review, spec = self.fact_review()
        request = self.artifacts.read_json(review["request_ref"])
        event = request["events"][0]
        event["effective_from"] = {"value": "2020-01-01", "precision": "date", "timezone": "Asia/Shanghai"}
        event["temporal_evidence"] = [{"field": "effective_from", "version_id": event["version_ids"][0], "quote": literal}]
        dated = news.assess(request, self.store, self.artifacts, "source-date")
        feature = news.factual_event_features(dated, spec, utc_now(), self.artifacts, store=self.store)["features"][0]
        self.assertIsNone(feature["effective_at"])
        self.assertEqual(feature["effective_source"]["precision"], "date")
        event["effective_from"] = {"value": "2020-01-01T09:30:00+08:00", "precision": "timestamp", "timezone": "Asia/Shanghai"}
        timed = news.assess(request, self.store, self.artifacts, "source-timestamp")
        feature = news.factual_event_features(timed, spec, utc_now(), self.artifacts, store=self.store)["features"][0]
        self.assertEqual(feature["effective_at"], "2020-01-01T09:30:00+08:00")

    def test_complete_finite_set_can_have_zero_observed_facts_without_global_absence(self):
        review, spec = self.make_review()
        contract = self.contract()
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "observed-zero", [contract])
        bundle = industry_data.validate_sector_data(value, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(bundle["sectors"][0]["features"], [])
        self.assertTrue(bundle["sectors"][0]["coverage"]["absence_proven"])
        self.assertFalse(bundle["sectors"][0]["coverage"]["source_time_window_complete"])
        self.assertNotIn("obtain_quoted_fact_event_features", [row["action"] for row in value["required_actions"]])
        self.assertEqual(value["status"], "partial")

    def test_missing_current_source_group_remains_actionable_despite_fact_support(self):
        policy = copy.deepcopy(self.news_run["news_policy"])
        policy["required_source_groups"]["international"] = ["us_fed"]
        policy["sources"].append("us_fed")
        self.open_policy("missing-international-run", policy)
        review, spec = self.fact_review()
        contract = self.contract()
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "partial-source", [contract])
        action = next(row for row in value["required_actions"] if row["action"] == "complete_industry_news_source_group")
        self.assertEqual(action["missing_sources"], ["us_fed"])
        self.assertEqual(value["status"], "partial")
        self.assertFalse(value["trade_ready"])

    def test_factsheet_constituent_cannot_impersonate_the_declared_index(self):
        contract = self.contract()
        raw = document_fixtures.SourceDocumentTests.pdf("CSI300 000300 CNY Constituent 600519 SyntheticStock")
        with patch.object(source_fetch, "fetch", return_value=transport(raw, IDENTITY_URL, "csi_index_identity", "application/pdf")):
            reference = source_documents.capture_document(IDENTITY_URL, "csi_index_identity", self.artifacts, store=self.store)
        contract.update(index_code="600519", provider_security_id="1.600519", index_name="SyntheticStock",
                        identity_quote="Constituent 600519 SyntheticStock", identity_document_ref=reference)
        with self.assertRaisesRegex(ValueError, "constituent codes are not index identities"):
            industry_data._identity(contract, self.artifacts, utc_now())

    def relation_document(self, lines):
        raw = source_archival_fixture.unicodePDF(lines)
        with patch.object(source_fetch, "fetch", return_value=transport(raw, IDENTITY_URL, "csi_index_identity", "application/pdf")):
            return source_documents.capture_document(IDENTITY_URL, "csi_index_identity", self.artifacts, store=self.store)

    def test_benchmark_role_is_mandatory_and_caller_verified_flags_are_rejected(self):
        contract = self.contract()
        missing = {key: value for key, value in contract.items() if key != "benchmark_role"}
        with self.assertRaisesRegex(ValueError, "benchmark_role"):
            industry_data._contract(missing)
        flagged = {**contract, "sector_relation": {**contract["sector_relation"], "verified": True}}
        with self.assertRaisesRegex(ValueError, "verified"):
            industry_data._contract(flagged)

    def test_constituent_and_industry_distribution_text_do_not_define_a_sector_index(self):
        review, spec = self.fact_review()
        contract = self.contract()
        contract["identity_document_ref"] = self.relation_document(["CSI300 000300 CNY", "指数简介",
            "本指数反映全市场的整体表现。", "行业权重分布", "合成产业 7%"])
        contract["sector_relation"].update(locator="pdf/page/1/line/5", quote="合成产业 7%")
        document = source_documents.read_extracted_document(contract["identity_document_ref"], self.artifacts)
        distribution_quote = next(row["text"] for row in document["blocks"] if row["locator"] == "pdf/page/1/line/4")
        contract["asset_domain"] = copy.deepcopy(contract["asset_domain"])
        contract["asset_domain"]["return_target"].update(locator="pdf/page/1/line/4",
            quote=distribution_quote, economic_meaning=distribution_quote)
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            with self.assertRaisesRegex(ValueError, "distribution table|single-sector"):
                industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "broad-index", [contract])
        # The same original source may be used under its actual broad-market role.
        market_quote = next(row["text"] for row in document["blocks"] if row["locator"] == "pdf/page/1/line/3")
        contract["asset_domain"]["role"] = "market"
        contract["asset_domain"]["return_target"].update(locator="pdf/page/1/line/3",
            quote=market_quote, economic_meaning=market_quote)
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "broad-market-source", [contract])
        bundle = industry_data.validate_sector_data(value, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(bundle["sectors"][0]["asset_domain"]["role"], "market")
        self.assertEqual(len(bundle["sectors"][0]["prices"]), 2)
        self.assertEqual(value["status"], "partial")

    def test_negated_multisector_and_unknown_source_layout_are_not_verified(self):
        for quote, heading in [("本指数不反映合成产业行业的市场表现。", "指数简介"),
                               ("本指数反映合成产业和其他行业的市场表现。", "指数简介"),
                               ("本指数反映各行业的市场表现。", "指数简介"),
                               ("本指数反映合成产业行业的市场表现。", "未识别版式")]:
            with self.subTest(quote=quote, heading=heading):
                contract = self.contract()
                reference = self.relation_document(["CSI300 000300 CNY", heading, quote])
                contract["identity_document_ref"] = reference
                contract["sector_relation"]["quote"] = quote
                document = source_documents.read_extracted_document(reference, self.artifacts)
                self.assertEqual(industry_data._sector_relation(contract, document)["status"], "partial")

    def test_analyst_proxy_preserves_raw_prices_but_stays_partial(self):
        review, spec = self.fact_review()
        contract = {**self.contract(), "benchmark_role": "analyst_proxy"}
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "proxy", [contract])
        sector = self.artifacts.read_json(value["data_ref"])["sectors"][0]
        self.assertEqual(len(sector["prices"]), 2)
        self.assertEqual(sector["sector_relation_scope"], "source_versioned_asset_definition")
        self.assertEqual(sector["asset_domain"]["role"], "single_sector")
        self.assertEqual(value["status"], "partial")
        self.assertFalse(value["trade_ready"])

    def test_same_definition_url_refresh_keeps_genuine_committed_first_known(self):
        review, spec = self.fact_review()
        contract = self.contract()
        contract.pop("identity_document_ref")
        contract.update(identity_url=IDENTITY_URL, identity_source_id="csi_index_identity")
        quote = contract["sector_relation"]["quote"]
        raw = source_archival_fixture.unicodePDF(["CSI300 000300 CNY", "指数简介", quote])
        def fetch(url, source_id, **kwargs):
            return transport(raw, url, source_id, "application/pdf") if source_id == "csi_index_identity" else self.quotes(url, source_id)
        with patch.object(source_fetch, "fetch", side_effect=fetch):
            first = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "url-first", [contract])
            second = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "url-second", [contract])
        before = self.artifacts.read_json(first["data_ref"])["sectors"][0]["sector_relation"]
        after = self.artifacts.read_json(second["data_ref"])["sectors"][0]["sector_relation"]
        self.assertEqual(after["known_at"], before["known_at"])
        self.assertEqual(after["first_proof_ref"], before["document_ref"])
        self.assertNotEqual(after["document_ref"], before["document_ref"])
        self.assertGreater(instant(after["observed_at"]), instant(before["observed_at"]))
        self.assertEqual(industry_data.validate_sector_data(second, spec, utc_now(), self.artifacts, store=self.store)["sectors"][0]["sector_relation"], after)

    def test_changed_definition_never_inherits_older_relation_clock(self):
        review, spec = self.fact_review()
        first_contract = self.contract()
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            first = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "definition-old", [first_contract])
        quote = "本指数反映合成产业行业股票的整体表现。"
        fresh_ref = self.relation_document(["CSI300 000300 CNY", "指数简介", quote])
        second_contract = {**first_contract, "identity_document_ref": fresh_ref,
                           "sector_relation": {**first_contract["sector_relation"], "quote": quote},
                           "asset_domain": copy.deepcopy(first_contract["asset_domain"])}
        second_contract["asset_domain"]["return_target"].update(quote=quote, economic_meaning=quote)
        with patch.object(source_fetch, "fetch", side_effect=self.quotes):
            second = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, "definition-new", [second_contract])
        before = self.artifacts.read_json(first["data_ref"])["sectors"][0]["sector_relation"]
        after = self.artifacts.read_json(second["data_ref"])["sectors"][0]["sector_relation"]
        self.assertNotEqual(before["semantic_hash"], after["semantic_hash"])
        self.assertEqual(after["known_at"], self.artifacts.read_json(fresh_ref)["retrieved_at"])
        self.assertGreater(instant(after["known_at"]), instant(before["known_at"]))
        self.assertEqual(after["first_proof_ref"], fresh_ref)

    def test_cherry_picked_positive_clause_from_negated_page_is_rejected(self):
        contract = self.contract()
        quote = "CSI300指数反映合成产业行业的整体表现。"
        for lines, locator in [(["CSI300 000300 CNY", "所谓"+quote+"这一说法并不正确。"], "pdf/page/1"),
                               (["CSI300 000300 CNY", "指数简介", quote, "上述说法并不正确。"], "pdf/page/1/line/3")]:
            ref = self.relation_document(lines)
            candidate = {**contract, "identity_document_ref": ref,
                         "sector_relation": {"locator": locator, "quote": quote, "sector_label": "合成产业"}}
            document = source_documents.read_extracted_document(ref, self.artifacts)
            self.assertEqual(industry_data._sector_relation(candidate, document)["status"], "partial")

    def test_other_industry_in_selection_prefix_cannot_be_hidden(self):
        contract = self.contract()
        quote = "CSI300指数选取金融行业股票和合成产业行业上市公司证券作为指数样本，以反映该行业证券的整体表现。"
        ref = self.relation_document(["CSI300 000300 CNY", "指数简介", quote])
        contract.update(identity_document_ref=ref, sector_relation={"locator": "pdf/page/1/line/3", "quote": quote, "sector_label": "合成产业"})
        self.assertEqual(industry_data._sector_relation(contract, source_documents.read_extracted_document(ref, self.artifacts))["status"], "partial")


if __name__ == "__main__":
    unittest.main()
