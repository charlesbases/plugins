"""Real producer authorization/clock contracts with controlled original pages."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artifacts import Artifacts
from contracts import fingerprint, instant, utc_now
from state_store import Store
import pipeline
import news
import source_fetch
import stage_validation

URL = "https://www.gov.cn/yaowen/liebiao/202001/content_123456.htm"
BODY = "Original official policy action for a controlled source test. " * 8


class FrozenNewsPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name), "plan")
        self.store.begin("fixture", {"purpose": "source policy test"})
        self.enterContext(self.store.lease("fixture"))
        self.artifacts = Artifacts(self.store.base)
        self.registry = copy.deepcopy(source_fetch.load_registry())
        self.registry["sources"]["cn_state_council"]["default_urls"] = [URL]
        self.calls = []

    def start(self, name="analysis", sources=None, age=3600, groups=None):
        sources = sources or ["cn_state_council"]
        policy = {"mode": "sealed_inputs", "sources": sources, "max_age_seconds": age,
                  "required_source_groups": groups or {"policy": sources}}
        with patch.object(source_fetch, "load_registry", return_value=self.registry):
            return pipeline._open_analysis({"news_policy": policy}, self.store, self.artifacts, name)

    def payload(self, run, explicit=False, **kwargs):
        result = {"run_id": run["run_id"], "cutoff_at": run["publish_cutoff"],
                  "window_start": "2020-01-01T00:00:00Z",
                  "required_source_groups": run["news_policy"]["required_source_groups"]}
        if explicit:
            result["sources"] = [{"source_id": "cn_state_council", "urls": [URL]}]
        return {**result, **kwargs}

    def fetch(self, url, source_id, registry=None, **kwargs):
        self.calls.append((source_id, url))
        registry = registry or self.registry
        rule = source_fetch.source_rule(source_id, registry)
        marker = (rule.get("body_ids") or ["UCAP-CONTENT"])[0]
        text = '<html><head><meta name="PubDate" content="2020-01-01"><title>Policy</title></head>'
        text += '<body><div id="'+marker+'">'+BODY+'</div></body></html>'
        raw = text.encode("utf-8")
        return {"registry_source_id": source_id, "registry_hash": fingerprint(registry),
                "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
                "content_type": "text/html", "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
                "retrieved_at": utc_now(), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "text_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "raw_bytes": raw, "text": text}

    def collect(self, run, name="collect", explicit=False, **kwargs):
        with patch.object(source_fetch, "fetch", side_effect=self.fetch):
            return news.collect(self.payload(run, explicit, **kwargs), self.store, self.artifacts, name)

    def test_default_only_selects_frozen_subset_and_validates_snapshot(self):
        run = self.start()
        result = self.collect(run)
        self.assertEqual({source for source, _ in self.calls}, {"cn_state_council"})
        self.assertEqual(result["selected_source_ids"], ["cn_state_council"])
        self.assertEqual(result["news_policy_hash"], run["news_policy_hash"])
        self.assertEqual(self.artifacts.read_json(result["registry_snapshot_ref"]), self.registry)
        proof = stage_validation._news(result, {"payload": self.payload(run), "operation_id": "collect"}, self.store, self.artifacts)
        self.assertIn(proof["status"], ("passed", "partial"))

    def test_explicit_outside_policy_is_rejected_before_any_request(self):
        run = self.start()
        payload = self.payload(run, sources=[{"source_id": "us_fed",
            "urls": self.registry["sources"]["us_fed"]["default_urls"]}])
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("No request permitted")):
            with self.assertRaisesRegex(ValueError, "outside frozen news policy"):
                news.collect(payload, self.store, self.artifacts, "outside")
        self.assertEqual(self.calls, [])

    def test_required_group_outside_frozen_sources_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside frozen news sources"):
            self.start(groups={"policy": ["us_fed"]})

    def test_mutable_progress_is_policy_scoped_but_original_facts_are_global(self):
        first = self.start("one")
        second = self.start("two", age=7200)
        before = self.collect(first, "first")
        after = self.collect(second, "second")
        self.assertNotEqual(before["source_results"][0]["watermark_key"], after["source_results"][0]["watermark_key"])
        self.assertEqual(before["versions"][0]["version_id"], after["versions"][0]["version_id"])
        initial = self.store.get("news-version-first-observed", before["versions"][0]["version_id"])
        self.assertEqual(initial["collection_id"], "first")
        self.assertEqual(len(list(self.store.scan("news-source-scheduler"))), 2)

    def test_frozen_registry_is_used_after_installed_registry_changes(self):
        run = self.start()
        with patch.object(source_fetch, "load_registry", side_effect=AssertionError("Use immutable snapshot")):
            self.assertEqual(pipeline._open_analysis({"news_policy": run["news_policy"]}, self.store,
                self.artifacts, run["run_id"]), run)
            result = self.collect(run)
            news.collection_policy(result, self.store, self.artifacts)
        self.assertEqual(result["registry_hash"], run["source_registry_hash"])

    def test_publication_cutoff_precedes_capture_and_later_review_is_valid(self):
        run = self.start()
        collection = self.collect(run, explicit=True)
        captured = collection["retrievals"][0]["retrieved_at"]
        self.assertGreater(instant(captured), instant(run["publish_cutoff"]))
        version = collection["versions"][0]
        claim = {"id": "claim", "kind": "fact", "text": BODY[:30], "version_id": version["version_id"],
                 "quote": BODY[:30], "categories": ["policy"], "direction": "neutral",
                 "counterevidence": [{"missing_reason": "Controlled source contract"}],
                 "reviewer": "test", "review_method": "original quote"}
        review = news.assess({"run_id": run["run_id"], "collection_id": "collect",
            "claims": [claim], "events": [], "industry_theses": []}, self.store, self.artifacts, "assess")
        self.assertEqual(news.validate_review(review, {"kind": "numeric_policy", "news": run["news_policy"]},
            utc_now(), self.artifacts, store=self.store)["status"], "passed")
        with self.assertRaisesRegex(ValueError, "future"):
            news._verify_collection_bodies(collection, self.registry, instant(run["publish_cutoff"]),
                instant(run["publish_cutoff"]), 3600, self.artifacts, store=self.store)

    def test_pdf_parent_minimum_budget_remains_required(self):
        source = "cn_ndrc_policy"
        run = self.start(sources=[source])
        payload = self.payload(run, sources=[{"source_id": source,
            "urls": ["https://www.ndrc.gov.cn/xxgk/zcfb/tz/original.pdf"]}], max_urls_per_source=1)
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("No request before parent budget")):
            with self.assertRaises(news.LinkedPDFBudget):
                news.collect(payload, self.store, self.artifacts, "pdf")
