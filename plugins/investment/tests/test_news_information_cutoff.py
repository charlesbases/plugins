"""Controlled source/clock regressions; no real news or investment evidence."""
import copy
import datetime as dt
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artifacts import Artifacts
from contracts import fingerprint, instant
from state_store import Store
import contracts
import news
import pipeline
import source_fetch
import stage_validation
import state_store
import validation_runtime

URL = "https://www.gov.cn/yaowen/liebiao/202001/content_123456.htm"
BODY = "This is a controlled engineering source body, not real market news. " * 8


class NewsInformationCutoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = "information-clock-engineering"
        self.store = Store(self.root, self.plan)
        self.artifacts = Artifacts(self.store.base)
        self.clock = "2020-04-02T12:00:00Z"
        for module in (contracts, news, pipeline, stage_validation, state_store, validation_runtime):
            self.enterContext(patch.object(module, "utc_now", side_effect=lambda: self.clock))
        self.policy = {"mode": "sealed_inputs", "sources": ["cn_state_council"],
            "required_source_groups": {"policy": ["cn_state_council"]}, "max_age_seconds": 3600}
        self.run = self.call("analysis", "analysis_start", {"news_policy": self.policy})["run"]

    def call(self, identity, operation, payload):
        return pipeline.run({"schema_version": 4, "request_id": identity,
            "operation": operation, "payload": payload}, self.root, self.plan)

    def collect(self, publications, identity="collect", *, captured="2020-04-02T12:01:00Z",
                sealed="2020-04-02T12:02:00Z", body=BODY, daily=False):
        urls = [URL.replace("123456", str(123456+i)) for i in range(len(publications))]
        def fetch(url, source_id, registry=None, **kwargs):
            text = '<meta name="PubDate" content="'+publications[urls.index(url)]+'"><title>Engineering</title>'
            text += '<div id="UCAP-CONTENT">'+body+'</div>'
            raw = text.encode("utf-8")
            self.clock = sealed
            return {"registry_source_id": source_id, "registry_hash": fingerprint(registry),
                "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
                "content_type": "text/html", "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
                "retrieved_at": captured, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "text_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "raw_bytes": raw, "text": text}
        payload = {"run_id": self.run["run_id"], "cutoff_at": self.run["publish_cutoff"],
            "window_start": "2020-04-01T00:00:00Z", "sources": [{"source_id": "cn_state_council", "urls": urls}],
            "required_source_groups": self.policy["required_source_groups"]}
        with patch.object(source_fetch, "fetch", side_effect=fetch):
            if daily:
                return self.call(identity, "daily_review", {"analysis_run_id": self.run["run_id"],
                    "news_policy": self.policy, "news": payload})
            return self.call(identity, "news_collect", payload)

    def assess(self, collection, identity="assess"):
        claims, events = [], []
        for index, version in enumerate(collection["versions"]):
            claim_id = "claim-"+str(index)
            claims.append({"id": claim_id, "kind": "fact", "text": BODY[:40], "quote": BODY[:40],
                "version_id": version["version_id"], "categories": ["policy"], "direction": "neutral",
                "counterevidence": [{"missing_reason": "Controlled engineering source scope"}],
                "reviewer": "engineering", "review_method": "original literal source"})
            events.append({"event_key": None, "version_ids": [version["version_id"]], "claim_ids": [claim_id],
                "event_at": None, "effective_from": None, "effective_until": None,
                "review_by": "2020-04-03T12:00:00Z", "supersedes": [], "retracts": [], "temporal_evidence": []})
        return self.call(identity, "news_assess", {"collection_id": collection["collection_id"],
            "claims": claims, "industry_theses": [], "events": events})

    def test_public_collect_assess_replay_preserves_precision_and_actual_information_cutoff(self):
        collected = self.collect(["2020-04-02", "2020-04-02T12:00:30Z"])
        self.assertEqual(collected["information_cutoff_at"], "2020-04-02T12:02:00Z")
        self.assertEqual(collected["cutoff_at"], "2020-04-02T12:00:00Z")
        self.assertTrue(all(row["within_information_cutoff"] for row in collected["retrievals"]))
        self.assertEqual({row["publication_cutoff_relation"] for row in collected["retrievals"]},
            {"overlaps_date_interval", "after"})
        self.assertEqual(collected["automatic_validation"]["status"], "passed")
        self.clock = "2020-04-02T12:03:00Z"
        reviewed = self.assess(collected)
        self.assertEqual(reviewed["automatic_validation"]["status"], "passed")
        raw = self.artifacts.read_json(reviewed["manifest_ref"])
        self.assertEqual({row["published_at"]["precision"] for row in raw["claims"]}, {"date", "timestamp"})
        self.assertEqual({row["publication_role"] for row in raw["events"]}, {"available_at_information_cutoff"})
        self.assertEqual(news.validate_review(raw, {"kind": "numeric_policy", "news": self.policy},
            self.clock, self.artifacts, store=self.store)["status"], "passed")
        with self.assertRaisesRegex(ValueError, "future"):
            news.validate_review(raw, {"kind": "numeric_policy", "news": self.policy},
                self.run["publish_cutoff"], self.artifacts, store=self.store)

    def test_future_publisher_time_and_future_local_date_do_not_qualify(self):
        collected = self.collect(["2020-04-02T12:01:30Z", "2020-04-03"])
        self.assertFalse(any(row["within_information_cutoff"] for row in collected["retrievals"]))
        self.assertEqual(collected["status"], "partial_collection")
        self.clock = "2020-04-02T12:03:00Z"
        with self.assertRaises(ValueError):
            self.assess(collected)

    def test_resealed_information_time_and_backdated_capture_cannot_replace_original_journals(self):
        collected = self.collect(["2020-04-02"])
        manifest = self.artifacts.read_json(collected["manifest_ref"])
        altered = copy.deepcopy(manifest)
        altered["information_cutoff_at"] = "2020-04-02T12:04:00Z"
        with self.assertRaisesRegex(ValueError, "original controller information cutoff seal"):
            news.collection_policy(altered, self.store, self.artifacts)
        altered = copy.deepcopy(manifest)
        altered["retrievals"][0]["retrieved_at"] = "2020-04-02T11:59:00Z"
        _, registry = news.collection_policy(altered, self.store, self.artifacts)
        with self.assertRaisesRegex(ValueError, "original producer journal"):
            news._verify_collection_bodies(altered, registry, instant(self.clock), instant(self.clock),
                3600, self.artifacts, store=self.store)

    def test_caller_cannot_supply_a_future_information_cutoff(self):
        payload = {"run_id": self.run["run_id"], "cutoff_at": self.run["publish_cutoff"],
            "window_start": "2020-04-01T00:00:00Z", "required_source_groups": self.policy["required_source_groups"],
            "information_cutoff_at": "2099-01-01T00:00:00Z"}
        self.store.begin("caller", payload)
        with self.store.lease("caller"), self.assertRaisesRegex(ValueError, "unknown=\\['information_cutoff_at'\\]"):
            news.collect(payload, self.store, self.artifacts, "caller")

    def test_repeated_public_operation_does_not_move_information_cutoff(self):
        collected = self.collect(["2020-04-02"])
        self.clock = "2020-04-02T12:05:00Z"
        journal = self.store.operation("collect")
        with patch.object(source_fetch, "fetch", side_effect=AssertionError("A replay cannot fetch")):
            repeated = pipeline.run(journal["request"], self.root, self.plan)
        self.assertEqual(repeated, collected)
        self.assertEqual(repeated["information_cutoff_at"], "2020-04-02T12:02:00Z")

    def test_new_capture_does_not_rewrite_first_observation_or_earlier_scope(self):
        first = self.collect(["2020-04-02"])
        version = first["versions"][0]["version_id"]
        known = copy.deepcopy(self.store.get("news-version-first-observed", version))
        self.clock = "2020-04-02T12:10:00Z"
        second = self.collect(["2020-04-02"], "second", captured="2020-04-02T12:11:00Z", sealed="2020-04-02T12:12:00Z")
        self.assertEqual(second["versions"][0]["version_id"], version)
        self.assertEqual(self.store.get("news-version-first-observed", version), known)
        self.assertEqual(first["information_cutoff_at"], "2020-04-02T12:02:00Z")
        self.assertEqual(second["information_cutoff_at"], "2020-04-02T12:12:00Z")
        publication = first["versions"][0]["published_at"]
        self.assertFalse(news._body_available(publication,
            instant("2020-04-02T12:11:00Z"), instant(first["information_cutoff_at"])))

    def test_public_daily_raw_draft_preserves_full_capture_interval_without_trade_qualification(self):
        result = self.collect(["2020-04-02"], "raw-daily", daily=True)
        self.assertEqual(result["automatic_validation"]["status"], "partial")
        proof = self.artifacts.read_json(result["automatic_validation"]["final_review_ref"])
        self.assertFalse(proof["readiness"]["trade_ready"])
        bundle = self.artifacts.read_json(result["bundle_ref"])
        self.assertIsNone(bundle["context"])
        self.assertEqual(bundle["orders"]["orders"], [])
        self.assertEqual(bundle["news"]["information_cutoff_at"], "2020-04-02T12:02:00Z")
        self.assertEqual(bundle["news"]["retrievals"][0]["retrieved_at"], "2020-04-02T12:01:00Z")

    def test_assessed_freshness_uses_source_clocks_even_when_query_start_has_expired(self):
        self.policy["max_age_seconds"] = 90
        self.run = self.call("short-analysis", "analysis_start", {"news_policy": self.policy})["run"]
        collected = self.collect(["2020-04-02"])
        self.clock = "2020-04-02T12:02:10Z"
        review = self.assess(collected)
        self.assertGreater((instant(self.clock)-instant(self.run["publish_cutoff"])).total_seconds(), 90)
        self.assertEqual(review["automatic_validation"]["status"], "passed")
        original = self.artifacts.read_json(review["manifest_ref"])
        with self.assertRaisesRegex(ValueError, "future or stale"):
            news.validate_review(original, {"kind": "numeric_policy", "news": self.policy},
                "2020-04-02T12:02:31Z", self.artifacts, store=self.store)

    def test_atomic_deadline_includes_earliest_original_capture_expiry(self):
        collected = self.collect(["2020-04-02"])
        self.clock = "2020-04-02T12:02:10Z"
        review = self.assess(collected)
        # A time-guard component fixture only; it claims no numerical eligibility.
        context = {"decision_at": self.clock, "risk_state": {"valid_until": None},
            "spec": {"availability": {"max_market_age_seconds": 86400, "max_terms_age_seconds": 86400},
                     "news": {"max_age_seconds": 90}},
            "market_ref": {"observed_at": self.clock, "terms": []}}
        package = {"writes": [{"kind": "decision", "value": {"context": context, "news": review}}]}
        deadline = pipeline._publication_deadline(package, self.artifacts)
        capture = instant(collected["retrievals"][0]["retrieved_at"])
        self.assertEqual(deadline, capture+dt.timedelta(seconds=90))
        self.assertLess(deadline, instant(review["information_cutoff_at"])+dt.timedelta(seconds=90))


if __name__ == "__main__":
    unittest.main()
