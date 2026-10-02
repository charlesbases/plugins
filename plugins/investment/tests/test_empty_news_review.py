"""Real run/collection provenance, controlled transport; no financial qualification."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import news
import pipeline
import source_fetch
import stage_validation
import verify
from state_store import Store
from validation_runtime import StageRuntime
from test_news import NewsTests, capture, URL


class EmptyNewsReviewTests(unittest.TestCase):
    setUp = NewsTests.setUp

    def _fetch(self, url, source_id, **kwargs):
        value = capture(url, source_id)
        if url != URL:
            raw = json.dumps([{"URL": URL, "TITLE": "controlled original source"}]).encode()
            value.update(content_type="application/json", raw_bytes=raw, text=raw.decode(), bytes=len(raw),
                         raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
        return value

    def _collect_stage(self, operation, *, automatic=False):
        control = Store(self.store.root, self.store.plan)
        payload = copy.deepcopy(self.payload)
        payload.update(max_urls_per_source=7, timeout_seconds=3)
        if automatic:
            payload.pop("sources")
        operation_id = operation+("-automatic" if automatic else "-explicit")
        outer = payload if operation == "news_collect" else {
            "analysis_run_id": self.news_run["run_id"], "news_policy": copy.deepcopy(self.news_run["news_policy"])}
        if operation == "daily_analyse" and not automatic:
            outer["news"] = {**payload, "cutoff_at": "2000-01-01T00:00:00Z"}
        request = {"schema_version": 4, "request_id": operation_id, "operation": operation, "payload": outer}
        control.begin(operation_id, request)
        with control.lease(operation_id), patch.object(source_fetch, "fetch", side_effect=self._fetch):
            control._local.validation_runtime = StageRuntime(control, self.artifacts, code_identity=verify.code_identity(),
                validator_contracts=pipeline._runtime_contracts(), round_no=2 if operation == "daily_analyse" else 1)
            value = pipeline._step(control, self.artifacts, operation_id, "news",
                lambda namespace: news.collect(payload, control, self.artifacts, namespace),
                inputs={"payload": payload, "operation_id": operation_id}, namespaced=True)
        return control, value

    def _review(self, control, collection_id):
        request_id = "assess-"+collection_id
        request = {"schema_version": 4, "request_id": request_id, "operation": "news_assess",
                   "payload": {"collection_id": collection_id, "claims": [], "industry_theses": [], "events": []}}
        control.begin(request_id, request)
        with control.lease(request_id):
            package = pipeline.dispatch(request, control, self.artifacts)
            self.assertEqual(package["result"]["status"], "awaiting_analysis")
            return stage_validation.validate_operation_result(request, package["result"], control, self.artifacts,
                                                               package["validation_bindings"])

    def test_public_empty_claim_assessment_keeps_source_checked_partial(self):
        control, collection = self._collect_stage("news_collect")
        proof = self._review(control, collection["collection_id"])
        self.assertEqual(proof["status"], "partial")
        self.assertFalse(proof["readiness"]["trade_ready"])

    def test_daily_normalized_original_inputs_support_retry_namespace(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                control, collection = self._collect_stage("daily_analyse", automatic=automatic)
                self.assertIn(":validation:", collection["collection_id"])
                outer = control.operation("daily_analyse"+("-automatic" if automatic else "-explicit"))["request"]["payload"]
                self.assertTrue("news" not in outer or outer["news"]["cutoff_at"] != collection["cutoff_at"])
                proof = self._review(control, collection["collection_id"])
                self.assertEqual(proof["status"], "partial")
                self.assertFalse(proof["readiness"]["trade_ready"])

    def test_without_original_collect_request_binding_is_explicitly_rejected(self):
        with patch.object(source_fetch, "fetch", side_effect=self._fetch):
            collection = news.collect(self.payload, self.store, self.artifacts, "unbound-original-collection")
        control = Store(self.store.root, self.store.plan)
        with self.assertRaisesRegex(stage_validation.ValidationError, "Original full collection request binding unavailable"):
            self._review(control, collection["collection_id"])


if __name__ == "__main__":
    unittest.main()
