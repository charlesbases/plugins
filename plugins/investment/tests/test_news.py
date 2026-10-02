"""Finite official-page fixtures; no network or claim-truth assertion."""
import copy
import datetime as dt
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
from artifacts import Artifacts
from contracts import fingerprint, instant, utc_now
from state_store import Store
import news
import source_fetch
import pipeline

URL = "https://www.gov.cn/yaowen/liebiao/202001/content_123456.htm"
BODY = "官方发布资料仅作为正文引用核验，不代表其事实已被独立证明。" * 4


def capture(url=URL, source_id="cn_state_council", body=BODY, marker=True, **kw):
    text = '<html><head><meta name="PubDate" content="2020-01-01"><title>正式来源</title></head><body>'
    marker_id = (source_fetch.source_rule(source_id).get("body_ids") or ["UCAP-CONTENT"])[0]
    text += ('<div id="'+marker_id+'">'+body+'</div>') if marker else '<a href="'+URL+'">仅有列表</a>'
    text += '</body></html>'
    data = text.encode("utf-8")
    return {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": "text/html", "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
            "retrieved_at": utc_now(), "raw_sha256": hashlib.sha256(data).hexdigest(), "text_sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "raw_bytes": data, "text": text}


class NewsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name), "plan")
        self.store.begin("fixture", {"kind": "test"})
        self.enterContext(self.store.lease("fixture"))
        self.artifacts = Artifacts(self.store.base)
        policy = {"mode": "sealed_inputs", "sources": ["cn_state_council"], "max_age_seconds": 3600,
                  "required_source_groups": {"policy": ["cn_state_council"]}}
        self.news_run = pipeline._open_analysis({"news_policy": policy}, self.store, self.artifacts, "run-one")
        self.payload = {"run_id": self.news_run["run_id"], "cutoff_at": self.news_run["publish_cutoff"],
                        "window_start": "2020-01-01T00:00:00Z",
                        "sources": [{"source_id": "cn_state_council", "urls": [URL]}],
                        "required_source_groups": policy["required_source_groups"]}

    def open_policy(self, run_id, policy, registry=None):
        if registry is None:
            run = pipeline._open_analysis({"news_policy": policy}, self.store, self.artifacts, run_id)
        else:
            with patch.object(source_fetch, "load_registry", return_value=registry):
                run = pipeline._open_analysis({"news_policy": policy}, self.store, self.artifacts, run_id)
        self.news_run = run
        self.payload.update(run_id=run["run_id"], cutoff_at=run["publish_cutoff"],
                            required_source_groups=copy.deepcopy(policy["required_source_groups"]))
        return run

    def collect(self, operation="one", body=BODY, **kw):
        with patch.object(source_fetch, "fetch", side_effect=lambda url, source_id, **kwargs: capture(url, source_id, body, **kw)):
            return news.collect(self.payload, self.store, self.artifacts, operation)

    def test_repeated_operation_reuses_and_article_version_is_global(self):
        first = self.collect()
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("must not fetch")):
            again = news.collect(self.payload, self.store, self.artifacts, "one")
        self.assertTrue(again["reused"])
        next_capture = self.collect("two")
        self.assertEqual(first["versions"][0]["version_id"], next_capture["versions"][0]["version_id"])
        revision = self.collect("three", body=BODY+"修订内容")
        self.assertNotEqual(first["versions"][0]["version_id"], revision["versions"][0]["version_id"])
        self.assertEqual(first["versions"][0]["document_id"], revision["versions"][0]["document_id"])

    def test_lead_partial_and_failure_never_advance_article_cursor(self):
        for operation, body, marker in (("lead", BODY, False), ("short", "正文不足", True)):
            manifest = self.collect(operation, body, marker=marker)
            result = manifest["source_results"][0]
            state = self.store.get("news-source-state", result["scope_key"])
            self.assertFalse(result["advance_cursor"])
            self.assertNotIn("checked_through", state)
        with patch.object(source_fetch, "fetch", side_effect=OSError("offline")):
            manifest = news.collect(self.payload, self.store, self.artifacts, "offline")
        self.assertEqual(manifest["retrievals"][0]["state"], "unavailable")
        self.assertFalse(manifest["source_results"][0]["advance_cursor"])

    def test_finite_article_scope_only_advances_atomically(self):
        manifest = self.collect()
        state = self.store.get("news-source-state", manifest["source_results"][0]["scope_key"])
        self.assertEqual(state["checked_through"], self.payload["cutoff_at"])
        self.assertFalse(manifest["coverage_complete_for_source_time_window"])
        self.assertEqual(manifest["fact_verification"], "not_performed")

    def test_assessment_quotes_versions_and_inference_remain_distinct(self):
        manifest = self.collect()
        version = manifest["versions"][0]["version_id"]
        item = {"id": "claim", "kind": "inference", "text": "分析者判断，不能称预测正确。", "version_id": version,
                "quote": BODY[:20], "categories": ["权益"], "direction": "watch",
                "counterevidence": [{"missing_reason": "该固定来源集合未找到反向材料"}],
                "reviewer": "test-analyst", "review_method": "human_or_agent_attestation"}
        payload = {"collection_id": "one", "claims": [item], "industry_theses": [], "events": []}
        review = news.assess(payload, self.store, self.artifacts, "review")
        self.assertEqual(review["status"], "assessed")
        self.assertEqual(review["claims"][0]["url"], URL)
        self.assertEqual(review["claims"][0]["published_at"]["precision"], "date")
        self.assertIsNone(review["claims"][0]["event_at"])
        self.assertEqual(review["claims"][0]["qualification"], "analyst_inference")
        self.assertEqual(news.assess(payload, self.store, self.artifacts, "review"), review)
        wrong = copy.deepcopy(payload)
        wrong["claims"][0]["quote"] = "根本不存在的引用"
        with self.assertRaisesRegex(ValueError, "does not occur"):
            news.assess(wrong, self.store, self.artifacts, "bad-review")
        wrong["claims"][0]["version_id"] = "unrelated"
        with self.assertRaisesRegex(ValueError, "body version"):
            news.assess(wrong, self.store, self.artifacts, "wrong-version")

    def test_future_cutoff_and_changed_idempotency_payload_are_rejected(self):
        future = {**self.payload, "cutoff_at": "2099-01-01T00:00:00Z"}
        with self.assertRaisesRegex(ValueError, "future"):
            news.collect(future, self.store, self.artifacts, "future")
        self.collect()
        with self.assertRaisesRegex(ValueError, "changed inputs"):
            news.collect({**self.payload, "timeout_seconds": 3}, self.store, self.artifacts, "one")

    def test_international_article_adapters_and_elapsed_budget_are_explicit(self):
        registry = source_fetch.load_registry()
        self.assertEqual(registry["sources"]["us_fed"]["collection_role"], "official_article")
        self.assertEqual(registry["sources"]["us_fed"]["body_ids"], ["article"])
        self.assertEqual(registry["sources"]["eu_ecb"]["body_ancestor_tags"], ["main"])
        self.open_policy("budget-run", {**self.news_run["news_policy"], "sources": [name for name, rule in registry["sources"].items() if rule["purpose"] == "news"]})
        clock = [0]
        def fetch(url, source_id, **kwargs):
            clock[0] += 10
            return capture(url, source_id, marker=False)
        with patch.object(news.time, "monotonic", side_effect=lambda: clock[0]), patch.object(source_fetch, "fetch", side_effect=fetch):
            result = news.collect({"run_id": self.news_run["run_id"], "window_start": self.payload["window_start"], "cutoff_at": self.payload["cutoff_at"],
                                   "budget_seconds": 15, "required_source_groups": self.payload["required_source_groups"]}, self.store, self.artifacts, "budget")
        self.assertLessEqual(len(result["retrievals"]), 2)
        self.assertTrue(any(r["budget_exhausted"] for r in result["source_results"]))


    def make_review(self, *, counter_source=False):
        if counter_source:
            policy = {**self.news_run["news_policy"], "sources": ["cn_state_council", "cn_pbc"]}
            self.news_run = pipeline._open_analysis({"news_policy": policy}, self.store, self.artifacts, "counter-run")
            self.payload.update(run_id=self.news_run["run_id"], cutoff_at=self.news_run["publish_cutoff"])
            self.payload["sources"].append({"source_id": "cn_pbc", "urls": [
                "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/202001011234567890/index.html"]})
        collection = self.collect()
        primary = next(v for v in collection["versions"] if v["source_id"] == "cn_state_council")
        counter = [{"missing_reason": "未找到反向材料"}]
        if counter_source:
            second = next(v for v in collection["versions"] if v["source_id"] == "cn_pbc")
            counter = [{"version_id": second["version_id"], "quote": BODY[:20], "text": "待分析的另一来源"}]
        item = {"id": "one-claim", "kind": "inference", "text": "明确归属于分析者的判断", "version_id": primary["version_id"],
                "quote": BODY[:20], "categories": ["权益"], "direction": "watch", "counterevidence": counter,
                "reviewer": "fixture-reviewer", "review_method": "source_quote_review"}
        thesis = {"thesis_id": "equity", "kind": "asset_class", "label": "权益", "direction": "watch",
                  "horizon_days": 60, "search_terms": ["股票"], "claim_ids": ["one-claim"],
                  "event_ids": [primary["document_id"]], "required_source_groups": ["policy"],
                  "valid_until": (instant(utc_now())+dt.timedelta(days=1)).isoformat(), "event_basis": "standing_policy"}
        event = {"event_key": None, "version_ids": [primary["version_id"]], "claim_ids": ["one-claim"],
                 "event_at": None, "effective_from": None, "effective_until": None,
                 "review_by": (instant(utc_now())+dt.timedelta(days=1)).isoformat(),
                 "supersedes": [], "retracts": [], "temporal_evidence": []}
        result = news.assess({"collection_id": "one", "claims": [item], "industry_theses": [thesis], "events": [event]},
                             self.store, self.artifacts, "assessed")
        spec = {"kind": "assisted_workflow", "news": copy.deepcopy(self.news_run["news_policy"])}
        return result, spec

    def test_assisted_gate_reconstructs_nonempty_review_from_closed_artifacts(self):
        review, spec = self.make_review()
        verified = news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(verified["status"], "passed")
        self.assertEqual(verified["source_ids"], ["cn_state_council"])
        raw = self.artifacts.read_json(review["manifest_ref"])
        self.assertEqual(news.validate_review(raw, spec, utc_now(), self.artifacts, store=self.store)["status"], "passed")
        raw["claims"][0]["quote"] = "未经来源支持的替换文字"
        with self.assertRaisesRegex(ValueError, "recomputed original quotes"):
            news.validate_review(raw, spec, utc_now(), self.artifacts, store=self.store)

    def test_assisted_gate_rejects_empty_and_all_failed_collection_reviews(self):
        with patch.object(source_fetch, "fetch", side_effect=OSError("all unavailable")):
            news.collect(self.payload, self.store, self.artifacts, "offline")
        empty = news.assess({"collection_id": "offline", "claims": [], "industry_theses": [], "events": []}, self.store, self.artifacts, "empty")
        spec = {"kind": "assisted_workflow", "news": {"mode": "sealed_inputs", "sources": ["cn_state_council"],
                                                         "max_age_seconds": 3600, "required_source_groups": {"policy": ["cn_state_council"]}}}
        for value in (empty, {**empty, "status": "assessed"}):
            with self.assertRaisesRegex(ValueError, "nonempty"):
                news.validate_review(value, spec, utc_now(), self.artifacts, store=self.store)

    def test_thesis_must_rederive_from_original_assessed_claims(self):
        review, spec = self.make_review()
        raw = self.artifacts.read_json(review["manifest_ref"])
        raw["industry_theses"][0]["direction"] = "increase"
        with self.assertRaisesRegex(ValueError, "Industry theses differ"):
            news.validate_review(raw, spec, utc_now(), self.artifacts, store=self.store)
        request = self.artifacts.read_json(review["request_ref"])
        request["industry_theses"][0]["claim_ids"] = ["unassessed"]
        with self.assertRaisesRegex(ValueError, "assessed supporting claims"):
            news.assess(request, self.store, self.artifacts, "unlinked-thesis")

    def test_assisted_gate_rejects_primary_and_counter_sources_outside_spec(self):
        review, spec = self.make_review(counter_source=True)
        allowed = copy.deepcopy(spec)
        self.assertEqual(news.validate_review(review, allowed, utc_now(), self.artifacts, store=self.store)["source_ids"],
                         ["cn_pbc", "cn_state_council"])
        spec["news"]["sources"] = ["cn_state_council"]
        with self.assertRaisesRegex(ValueError, "authoritative analysis run"):
            news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)
        spec["news"]["sources"] = ["cn_pbc"]
        with self.assertRaisesRegex(ValueError, "authoritative analysis run"):
            news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)

    def test_assisted_gate_rejects_unknown_duplicate_or_non_news_source_names(self):
        review, spec = self.make_review()
        for sources in ([], ["unregistered"], ["eastmoney_nav"], ["cn_state_council", "cn_state_council"]):
            with self.subTest(sources=sources):
                spec["news"]["sources"] = sources
                with self.assertRaisesRegex(ValueError, "authoritative analysis run"):
                    news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)

    def test_assisted_gate_rejects_future_or_stale_review_and_collection(self):
        review, spec = self.make_review()
        raw = self.artifacts.read_json(review["manifest_ref"])
        decision = instant(utc_now())
        raw["reviewed_at"] = (decision+dt.timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "review is future or stale"):
            news.validate_review(raw, spec, decision.isoformat(), self.artifacts, store=self.store)
        with self.assertRaisesRegex(ValueError, "review is future or stale"):
            news.validate_review(review, spec, (decision+dt.timedelta(hours=2)).isoformat(), self.artifacts, store=self.store)
        raw = self.artifacts.read_json(review["manifest_ref"])
        collection = self.artifacts.read_json(raw["collection_manifest_ref"])
        collection["cutoff_at"] = (decision+dt.timedelta(seconds=1)).isoformat()
        raw["cutoff_at"] = collection["cutoff_at"]
        raw["collection_manifest_ref"] = self.artifacts.put_json(collection)
        with self.assertRaisesRegex(ValueError, "authoritative frozen source policy"):
            news.validate_review(raw, spec, decision.isoformat(), self.artifacts, store=self.store)

    def test_new_review_cannot_refresh_an_expired_collection(self):
        review, spec = self.make_review()
        request = self.artifacts.read_json(review["request_ref"])
        later = (instant(review["reviewed_at"])+dt.timedelta(hours=2)).isoformat()
        with patch.object(news, "utc_now", return_value=later):
            refreshed = news.assess(request, self.store, self.artifacts, "new-review-old-collection")
        with self.assertRaisesRegex(ValueError, "collection is future or stale"):
            news.validate_review(refreshed, spec, later, self.artifacts, store=self.store)

    def test_assisted_gate_reparses_source_instead_of_trusting_body_flag(self):
        review, spec = self.make_review()
        raw = self.artifacts.read_json(review["manifest_ref"])
        collection = self.artifacts.read_json(raw["collection_manifest_ref"])
        retrieval = collection["retrievals"][0]
        source = self.artifacts.read(retrieval["raw_ref"]).replace(b' id="UCAP-CONTENT"', b' id="not-a-body"')
        retrieval["raw_ref"] = self.artifacts.put_bytes(source)
        retrieval["raw_sha256"] = retrieval["text_sha256"] = hashlib.sha256(source).hexdigest()
        retrieval["bytes"] = len(source)
        raw["collection_manifest_ref"] = self.artifacts.put_json(collection)
        with self.assertRaisesRegex(ValueError, "classification differs"):
            news.validate_review(raw, spec, utc_now(), self.artifacts, store=self.store)

    def test_assisted_gate_reads_all_closed_source_objects(self):
        review, spec = self.make_review()
        raw = self.artifacts.read_json(review["manifest_ref"])
        collection = self.artifacts.read_json(raw["collection_manifest_ref"])
        reference = collection["retrievals"][0]["raw_ref"]
        self.artifacts._path(reference["path"]).unlink()
        with self.assertRaises(ValueError):
            news.validate_review(raw, spec, utc_now(), self.artifacts, store=self.store)



    def test_source_local_date_boundary_does_not_use_utc_calendar_day(self):
        publication = {"value": "2030-01-01", "precision": "date", "timezone": "Asia/Shanghai"}
        self.assertEqual(news._publication_relation(publication, instant("2030-01-01T17:00:00Z")), "proved_before")
        self.assertEqual(news._publication_relation(publication, instant("2030-01-01T10:00:00Z")), "overlaps_date_interval")

    def test_unknown_legal_end_can_support_reviewed_background(self):
        self.payload["window_start"] = (instant(utc_now())-dt.timedelta(days=1)).isoformat()
        review, spec = self.make_review()
        state = news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(state["active_thesis_ids"], ["equity"])
        self.assertIsNone(review["events"][0]["effective_until"])
        self.assertEqual(review["events"][0]["publication_role"], "background")

    def test_missing_group_blocks_only_dependent_thesis(self):
        policy = {**self.news_run["news_policy"], "sources": ["cn_state_council", "us_fed"],
                  "required_source_groups": {"policy": ["cn_state_council"], "foreign": ["us_fed"]}}
        self.open_policy("missing-group-run", policy)
        review, spec = self.make_review()
        request = self.artifacts.read_json(review["request_ref"])
        request["industry_theses"].append({**request["industry_theses"][0], "thesis_id": "foreign-equity",
                                           "required_source_groups": ["foreign"]})
        second = news.assess(request, self.store, self.artifacts, "two-theses")
        spec["news"]["required_source_groups"] = self.payload["required_source_groups"]
        state = news.validate_review(second, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(state["active_thesis_ids"], ["equity"])
        self.assertIn("foreign-equity", state["blocked_theses"])

    def test_event_reassessment_keeps_first_seen_and_does_not_become_new_news(self):
        self.payload["window_start"] = (instant(utc_now())-dt.timedelta(days=1)).isoformat()
        review, spec = self.make_review()
        request = self.artifacts.read_json(review["request_ref"])
        request["events"][0]["review_by"] = (instant(utc_now())+dt.timedelta(days=2)).isoformat()
        revised = news.assess(request, self.store, self.artifacts, "later-review")
        self.assertEqual(review["events"][0]["first_seen_at"], revised["events"][0]["first_seen_at"])
        self.assertEqual(revised["events"][0]["publication_role"], "background")
        old = news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)
        self.assertEqual(old["active_thesis_ids"], [])
        historical = news.validate_review(review, spec, review["reviewed_at"], self.artifacts, store=self.store)
        self.assertEqual(historical["active_thesis_ids"], ["equity"])

    def test_expiry_recomputes_eligibility_without_mutating_sealed_review(self):
        self.open_policy("expiry-run", {**self.news_run["news_policy"], "max_age_seconds": 3*86400})
        review, spec = self.make_review()
        before = fingerprint(review)
        later = (instant(review["reviewed_at"])+dt.timedelta(days=2)).isoformat()
        state = news.validate_review(review, spec, later, self.artifacts, store=self.store)
        self.assertEqual(state["active_thesis_ids"], [])
        self.assertIn("analytical_thesis_review_due", state["blocked_theses"]["equity"])
        self.assertEqual(fingerprint(review), before)

    def test_administrative_time_cannot_be_invented_from_date_only_quote(self):
        review, spec = self.make_review()
        request = self.artifacts.read_json(review["request_ref"])
        request["events"][0]["effective_from"] = {"value": "2020-01-01T12:00:00+08:00",
                                                 "precision": "timestamp", "timezone": "Asia/Shanghai"}
        request["events"][0]["temporal_evidence"] = [{"field": "effective_from",
            "version_id": request["events"][0]["version_ids"][0], "quote": BODY[:20]}]
        with self.assertRaisesRegex(ValueError, "precision must be supported"):
            news.assess(request, self.store, self.artifacts, "invented-clock")


    def test_resealed_coverage_success_requires_actual_article_attempts(self):
        review, spec = self.make_review()
        raw = self.artifacts.read_json(review["manifest_ref"])
        collection = self.artifacts.read_json(raw["collection_manifest_ref"])
        collection["source_results"][0]["visited_urls"] = []
        raw["source_results"] = collection["source_results"]
        raw["collection_manifest_ref"] = self.artifacts.put_json(collection)
        with self.assertRaisesRegex(ValueError, "actual retrieval attempts"):
            news.validate_review(raw, spec, utc_now(), self.artifacts, store=self.store)

    def test_later_source_retraction_blocks_current_but_preserves_historical_thesis(self):
        review, spec = self.make_review()
        target = review["events"][0]["event_id"]
        new_url = URL.replace("123456", "123457")
        self.payload["sources"] = [{"source_id": "cn_state_council", "urls": [new_url]}]
        collection = self.collect("withdrawal", body=BODY+"撤销公告："+URL)
        version = collection["versions"][0]
        claim = {"id": "withdrawn", "kind": "fact", "text": "原文明确撤销旧公告", "version_id": version["version_id"],
                 "quote": "撤销公告："+URL, "categories": ["权益"], "direction": "neutral",
                 "counterevidence": [{"missing_reason": "synthetic exact retraction fixture"}],
                 "reviewer": "fixture", "review_method": "verbatim_original"}
        event = {"event_key": None, "version_ids": [version["version_id"]], "claim_ids": ["withdrawn"],
                 "event_at": None, "effective_from": None, "effective_until": None,
                 "review_by": (instant(utc_now())+dt.timedelta(days=1)).isoformat(), "supersedes": [], "retracts": [target],
                 "temporal_evidence": [{"field": "retracts", "version_id": version["version_id"], "quote": "撤销公告："+URL}]}
        news.assess({"collection_id": "withdrawal", "claims": [claim], "industry_theses": [], "events": [event]},
                    self.store, self.artifacts, "withdrawal-review")
        current = news.validate_review(review, spec, utc_now(), self.artifacts, store=self.store)
        self.assertIn("event_superseded_or_retracted", current["blocked_theses"]["equity"])
        historical = news.validate_review(review, spec, review["reviewed_at"], self.artifacts, store=self.store)
        self.assertEqual(historical["active_thesis_ids"], ["equity"])

    def test_query_addressed_documents_are_not_collapsed(self):
        self.assertNotEqual(news._document_url(URL+"?id=1"), news._document_url(URL+"?id=2"))

    def test_assessment_analysis_run_must_match_collection(self):
        self.payload["run_id"] = "run-one"
        review, spec = self.make_review()
        self.assertEqual(review["run_id"], "run-one")
        request = self.artifacts.read_json(review["request_ref"])
        with self.assertRaisesRegex(ValueError, "another analysis run"):
            news.assess({**request, "run_id": "run-two"}, self.store, self.artifacts, "wrong-run")
        valid = news.assess({**request, "run_id": "run-one"}, self.store, self.artifacts, "same-run")
        self.assertEqual(news.validate_review(valid, spec, utc_now(), self.artifacts, store=self.store)["status"], "passed")

    def test_automatic_source_continuation_fetches_fresh_head_without_false_watermark(self):
        registry = source_fetch.load_registry()
        registry["sources"] = {"cn_state_council": registry["sources"]["cn_state_council"]}
        feed = registry["sources"]["cn_state_council"]["default_urls"][0]
        urls = [URL.replace("123456", str(number)) for number in (123456, 123457, 123458)]
        def fetch(url, source_id, **kwargs):
            row = capture(url, source_id)
            if url == feed:
                text = __import__("json").dumps([{"URL": item, "TITLE": "fixture"} for item in urls])
                raw = text.encode()
                row.update(content_type="application/json", text=text, raw_bytes=raw, bytes=len(raw),
                           raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
            row["registry_hash"] = fingerprint(registry)
            return row
        payload = {key: value for key, value in self.payload.items() if key != "sources"}
        payload["max_urls_per_source"] = 2
        with patch.object(source_fetch, "load_registry", return_value=registry), patch.object(source_fetch, "fetch", side_effect=fetch):
            run = self.open_policy("auto-run", self.news_run["news_policy"], registry)
            payload.update(run_id=run["run_id"], cutoff_at=run["publish_cutoff"])
            first = news.collect(payload, self.store, self.artifacts, "auto-one")
            second = news.collect(payload, self.store, self.artifacts, "auto-two")
        self.assertEqual(second["retrievals"][0]["requested_url"], feed)
        self.assertEqual(second["retrievals"][1]["requested_url"], urls[1])
        self.assertEqual(second["source_results"][0]["continued_from"], "auto-one")
        state = self.store.get("news-source-watermark", second["source_results"][0]["watermark_key"])
        self.assertNotIn("checked_through", state)
        self.assertFalse(state["coverage_complete_for_source_time_window"])

    def test_ndrc_two_default_lists_make_body_progress_and_preserve_finite_scope(self):
        registry = source_fetch.load_registry()
        registry["sources"] = {"cn_ndrc_policy": registry["sources"]["cn_ndrc_policy"]}
        lists = registry["sources"]["cn_ndrc_policy"]["default_urls"]
        articles = ["https://www.ndrc.gov.cn/xxgk/zcfb/tz/t20200101_"+str(n)+".html" for n in (1, 2)]
        calls = []
        def fetch(url, source_id, **kwargs):
            calls.append(url)
            row = capture(url, source_id)
            if url in lists:
                text = '<title>Synthetic official listing fixture</title>'+''.join('<a href="'+x+'">fixture</a>' for x in articles)
            else:
                text = '<meta name="PubDate" content="2020-01-01"><title>Synthetic article</title><div class="article_con">'+BODY+'</div>'
            raw = text.encode()
            row.update(text=text, raw_bytes=raw, bytes=len(raw), registry_hash=fingerprint(registry),
                       raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
            return row
        payload = {"cutoff_at": utc_now(), "window_start": "2020-01-01T00:00:00Z",
                   "required_source_groups": {"policy": ["cn_ndrc_policy"]}}
        with patch.object(source_fetch, "load_registry", return_value=registry), patch.object(source_fetch, "fetch", side_effect=fetch):
            policy = {"mode": "sealed_inputs", "sources": ["cn_ndrc_policy"], "max_age_seconds": 3600,
                      "required_source_groups": {"policy": ["cn_ndrc_policy"]}}
            run = self.open_policy("ndrc-run", policy, registry)
            payload.update(run_id=run["run_id"], cutoff_at=run["publish_cutoff"])
            first = news.collect(payload, self.store, self.artifacts, "ndrc-one")
            second = news.collect(payload, self.store, self.artifacts, "ndrc-two")
            for manifest in (first, second):
                news._verify_source_scopes(manifest, registry)
                self.assertTrue(manifest["source_results"][0]["advance_cursor"])
                self.assertFalse(manifest["coverage_complete_for_source_time_window"])
            self.assertIn(articles[0], calls[:2])
            self.assertIn(articles[1], calls[2:4])
            before = list(calls)
            self.assertTrue(news.collect(payload, self.store, self.artifacts, "ndrc-two")["reused"])
            self.assertEqual(calls, before)
            damaged = copy.deepcopy(second)
            damaged["source_results"][0]["scheduled_article_urls"] = []
            with self.assertRaisesRegex(ValueError, "omitted an attempted article"):
                news._verify_source_scopes(damaged, registry)

    def test_registered_nbs_publisher_clock_keeps_source_precision(self):
        rule = source_fetch.source_rule("cn_stats")
        capture = {"text": '<meta name="PubDate" content="2026/09/30 09:30"><title>fixture</title>'
                   '<div class="trs_editor_view">'+BODY+'</div>', "content_type": "text/html",
                   "final_url": "https://www.stats.gov.cn/sj/zxfb/202609/t20260930_1965449.html"}
        parsed = news.parse_capture(capture, rule)
        self.assertEqual(parsed["state"], "body")
        self.assertEqual(parsed["published_at"]["value"], "2026-09-30T09:30:00+08:00")
        self.assertEqual(parsed["published_at"]["precision"], "timestamp")
        unknown = news._publication({"pubdate": "2026/09/30 09:30"}, timezone="Asia/Shanghai")
        self.assertEqual(unknown["precision"], "unparsed")

    def test_pending_pdf_reacquires_its_original_parent_within_next_budget(self):
        # Scheduling fixture only: PDF parsing itself is tested independently.
        registry = source_fetch.load_registry()
        registry["sources"] = {"cn_ndrc_policy": registry["sources"]["cn_ndrc_policy"]}
        listing = registry["sources"]["cn_ndrc_policy"]["default_urls"][0]
        parent = "https://www.ndrc.gov.cn/xxgk/zcfb/tz/t20200101_1.html"
        pdf = "https://www.ndrc.gov.cn/xxgk/zcfb/tz/original.pdf"
        calls, parser = [], news.parse_capture
        def fetch(url, source_id, **kwargs):
            calls.append(url)
            row = capture(url, source_id)
            text = ('<a href="'+parent+'">fixture</a>') if url != parent else (
                '<meta name="PubDate" content="2020-01-01"><title>Synthetic parent</title>'
                '<div class="article_con">'+BODY+'<a href="'+pdf+'">PDF</a></div>')
            raw = text.encode()
            row.update(text=text, raw_bytes=raw, bytes=len(raw), registry_hash=fingerprint(registry),
                       raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
            if url == pdf:
                row["content_type"] = "application/pdf"
            return row
        def parse(cap, rule, *, linked_parent=None):
            if cap["final_url"] != pdf:
                return parser(cap, rule, linked_parent=linked_parent)
            self.assertIsNotNone(linked_parent)
            return {**linked_parent, "links": [], "parser_id": "synthetic_dependency_only"}
        payload = {"cutoff_at": utc_now(), "window_start": "2020-01-01T00:00:00Z",
                   "required_source_groups": {"policy": ["cn_ndrc_policy"]}}
        with patch.object(source_fetch, "load_registry", return_value=registry), patch.object(source_fetch, "fetch", side_effect=fetch), patch.object(news, "parse_capture", side_effect=parse):
            policy = {"mode": "sealed_inputs", "sources": ["cn_ndrc_policy"], "max_age_seconds": 3600,
                      "required_source_groups": {"policy": ["cn_ndrc_policy"]}}
            run = self.open_policy("pdf-run", policy, registry)
            payload.update(run_id=run["run_id"], cutoff_at=run["publish_cutoff"])
            first = news.collect(payload, self.store, self.artifacts, "pdf-one")
            with self.assertRaises(news.LinkedPDFBudget) as error:
                news.collect({**payload, "max_urls_per_source": 1}, self.store, self.artifacts, "pdf-budget-one")
            self.assertEqual(error.exception.required_actions[0]["minimum_max_urls_per_source"], 2)
            second = news.collect(payload, self.store, self.artifacts, "pdf-two")
        self.assertEqual(calls[:2], [listing, parent])
        self.assertEqual(calls[2:4], [parent, pdf])
        self.assertEqual(first["source_results"][0]["linked_parent_urls"], {pdf: parent})
        child = second["retrievals"][1]
        self.assertEqual(child["linked_parent_retrieval_id"], second["retrievals"][0]["retrieval_id"])

    def test_nhsa_cdata_discovery_is_not_a_body_and_original_clock_is_preserved(self):
        rule = source_fetch.source_rule("cn_nhsa_policy")
        listing = news.parse_capture({"text": '<record><![CDATA[<li><a href="/art/2026/4/14/art_53_20215.html">fixture</a></li>]]></record>',
            "content_type": "text/html", "final_url": rule["default_urls"][0]}, rule)
        self.assertEqual(listing["state"], "lead")
        self.assertIsNone(listing["body"])
        article = news.parse_capture({"text": '<meta name="PubDate" content="2026-04-14 19:00"><div id="zoom">'+BODY+'</div>',
            "content_type": "text/html", "final_url": "https://www.nhsa.gov.cn/art/2026/4/14/art_53_20215.html"}, rule)
        self.assertEqual(article["state"], "body")
        self.assertEqual(article["published_at"]["value"], "2026-04-14T19:00:00+08:00")

if __name__ == "__main__": unittest.main()
