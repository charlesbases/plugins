"""Stage gates verify synthetic source facts, without repairing the producer."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

SCRIPTS = Path(__file__).resolve().parents[1]/"skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))

import contracts
import ledger
import news
import pipeline
import source_fetch
import source_documents
import stage_validation as gates
import verify
from artifacts import Artifacts
from state_store import Store, LeaseLost

AT = "2020-01-01T08:00:00Z"
URL = "https://www.gov.cn/yaowen/liebiao/202001/content_123456.htm"


def synthetic_capture(url, source_id, **kwargs):
    text = ('<html><head><meta name="PubDate" content="2020-01-01"><title>Synthetic source title</title></head>'
            '<body><div id="UCAP-CONTENT">' + "Synthetic source evidence; this is not real investment news. "*30
            + '</div></body></html>')
    raw = text.encode("utf-8")
    return {"registry_source_id": source_id, "registry_hash": contracts.fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": "text/html", "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
            "retrieved_at": contracts.utc_now(), "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "text_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": raw, "text": text}


class StageValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="investment-stage-validation-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name, "synthetic")
        self.objects = Artifacts(self.store.base)

    def commit_event(self, identity, kind, data, at=AT):
        current = self.store.get("account", "main") or ledger.initial_state("CNY")
        requested = {"id": identity, "type": kind, "effective_at": at, "known_at": at, "recorded_at": at, "data": data}
        payload = {"currency": "CNY", "events": [requested]}
        request = {"schema_version": 4, "request_id": identity, "operation": "feedback", "payload": payload}
        self.store.begin(identity, request)
        with self.store.lease(identity):
            event = {**requested, "sequence": current["sequence"]+1}
            account = ledger.apply_event(current, event)
            self.store.put("ledger_event", identity, event)
            self.store.put("ledger_event_owner", identity, {"account_id": "main"})
            self.store.put("ledger_event_evidence", identity, [])
            self.store.put("account", "main", account, immutable=False)
            result = {"status": "recorded", "account_id": "main", "account_hash": contracts.fingerprint(account),
                      "sequence": account["sequence"], "added": [identity], "repeated": []}
            self.store.complete(identity, result)
        return result, {"payload": payload, "operation_id": identity}

    def opening(self):
        return self.commit_event("opening", "opening", {"cash": "100", "lots": [], "prices": {}, "price_dates": {}})

    def collection(self, unavailable=False):
        policy = {"mode": "sealed_inputs", "sources": ["cn_state_council"], "max_age_seconds": 3600,
                  "required_source_groups": {"policy": ["cn_state_council"]}}
        start = {"news_policy": policy}
        self.store.begin("news-authority", {"operation": "analysis_start", "payload": start})
        with self.store.lease("news-authority"):
            run = pipeline._open_analysis(start, self.store, self.objects, "stage-news-run")
            self.store.complete("news-authority", run)
        payload = {"run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": "2019-12-01T00:00:00Z",
                   "sources": [{"source_id": "cn_state_council", "urls": [URL]}],
                   "required_source_groups": run["news_policy"]["required_source_groups"]}
        self.store.begin("news", {"payload": payload})
        with self.store.lease("news"):
            with patch.object(source_fetch, "fetch", side_effect=OSError("synthetic unavailable") if unavailable else synthetic_capture):
                result = news.collect(payload, self.store, self.objects, "news")
            self.store.complete("news", result)
        return result, {"payload": payload, "operation_id": "news"}

    def reseal_collection(self, value):
        manifest = {key: item for key, item in value.items() if key not in {"manifest_ref", "reused"}}
        value["manifest_ref"] = self.objects.put_json(manifest)

    def unreadable_document(self):
        url, source_id = "https://www.cmfchina.com/web/fundDetail/123456/index.html", "issuer_cmfchina"
        capture = synthetic_capture(url, source_id)
        raw = b"Synthetic invalid PDF, no readable original text."
        capture.update(raw_bytes=raw, text=None, encoding="binary", content_type="application/pdf",
                       bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=None)
        request = {"schema_version": 4, "request_id": "document", "operation": "source_capture",
                   "payload": {"url": url, "source_id": source_id}}
        self.store.begin("document", request)
        with self.store.lease("document"):
            with patch.object(source_fetch, "fetch", return_value=capture):
                reference = source_documents.capture_document(url, source_id, self.objects, store=self.store)
            document = self.objects.read_json(reference)
            result = {"status": document["status"], "document_ref": reference, "document": document,
                      "required_actions": document["required_actions"]}
            self.store.complete("document", result)
        return request, result

    def test_registry_is_exhaustive_and_unknown_names_rejected(self):
        self.assertEqual(gates.OPERATION_NAMES, contracts.OPERATIONS)
        for name in ("invented", "numeric_unknown", "code-via-user-flag"):
            with self.assertRaisesRegex(gates.ValidationError, "Unknown stage"):
                gates.validate(name, {"status": "passed"}, {}, self.store, self.objects)
        with self.assertRaises(gates.ValidationError):
            gates.validate_operation_result({"schema_version": 4, "request_id": "bad", "operation": "invented", "payload": {}},
                                            {"status": "passed"}, self.store, self.objects)

    def test_code_identity_cannot_be_caller_resealed(self):
        value = verify.code_identity()
        self.assertEqual(gates.validate("code", value, {}, self.store, self.objects)["status"], "passed")
        value["ledger.py"] = "0"*64
        with self.assertRaisesRegex(gates.ValidationError, "implementation identity"):
            gates.validate("code", value, {}, self.store, self.objects)

    def test_clock_uses_operation_bounds(self):
        inputs = {"not_before": AT, "not_after": "2020-01-02T08:00:00Z"}
        self.assertEqual(gates.validate("decision-cutoff", {"at": AT}, inputs, self.store, self.objects)["status"], "passed")
        for value in ("2019-12-31T08:00:00Z", "2020-01-03T08:00:00Z"):
            with self.assertRaisesRegex(gates.ValidationError, "operation bounds"):
                gates.validate("news-window", {"cutoff_at": value}, inputs, self.store, self.objects)

    def test_feedback_replays_facts_without_mutating_store_or_artifacts(self):
        value, inputs = self.opening()
        before = copy.deepcopy(self.store.get("account", "main"))
        with patch.object(self.store, "put", side_effect=AssertionError("validator wrote business state")), \
                patch.object(self.objects, "put_json", side_effect=AssertionError("validator wrote artifacts")):
            receipt = gates.validate("feedback", value, inputs, self.store, self.objects)
        self.assertEqual(receipt["status"], "passed")
        self.assertFalse(receipt["readiness"]["trade_ready"])
        self.assertEqual(self.store.get("account", "main"), before)

    def test_feedback_metadata_tamper_is_rejected_without_rollback(self):
        value, inputs = self.opening()
        value["account_hash"] = "0"*64
        with self.assertRaises(gates.ValidationError):
            gates.validate("feedback", value, inputs, self.store, self.objects)
        self.assertEqual(self.store.get("account", "main")["cash"], "100")

    def test_persisted_account_tamper_is_rejected_and_not_repaired(self):
        value, inputs = self.opening()
        self.store.begin("tamper", {"synthetic": True})
        with self.store.lease("tamper"):
            changed = self.store.get("account", "main")
            changed["cash"] = "999"
            self.store.put("account", "main", changed, immutable=False)
            self.store.complete("tamper", {"status": "synthetic_fixture"})
        with self.assertRaisesRegex(gates.ValidationError, "ledger replay"):
            gates.validate("feedback", value, inputs, self.store, self.objects)
        self.assertEqual(self.store.get("account", "main")["cash"], "999")

    def test_cached_financial_prefix_survives_later_valuation(self):
        value, inputs = self.opening()
        self.commit_event("later", "valuation", {"prices": {}, "price_dates": {}}, "2020-01-02T08:00:00Z")
        self.assertEqual(gates.validate("feedback", value, inputs, self.store, self.objects)["status"], "passed")
        self.assertEqual(self.store.get("account", "main")["sequence"], 2)

    def test_confirmed_unknown_is_partial_and_never_trade_ready(self):
        value, inputs = self.commit_event("unknown", "unknown", {"reason": "Synthetic account evidence missing"})
        receipt = gates.validate("feedback", value, inputs, self.store, self.objects)
        self.assertEqual(receipt["status"], "partial")
        self.assertFalse(receipt["readiness"]["trade_ready"])
        self.assertTrue(receipt["required_actions"])
        self.assertEqual(self.store.get("account", "main")["cash"], "0")

    def accepted_status(self):
        import pipeline
        request = {"schema_version": 4, "request_id": "snapshot", "operation": "status", "payload": {}}
        result = pipeline.run(request, self.store.root, self.store.plan)
        return request, result

    def test_historical_empty_snapshot_survives_later_financial_facts(self):
        request, result = self.accepted_status()
        original = copy.deepcopy(result)
        self.opening()
        receipt = gates.validate_operation_result(request, result, self.store, self.objects, {"historical": True})
        self.assertIn("historical_snapshot", receipt["scope"])
        self.assertEqual(receipt["status"], "partial")
        self.assertFalse(receipt["readiness"]["trade_ready"])
        self.assertEqual(result, original)
        self.assertIsNone(result["account"])
        self.assertEqual(self.store.get("account", "main")["cash"], "100")

    def test_historical_core_cannot_be_resealed_against_accepted_operation(self):
        request, result = self.accepted_status()
        result["mode"] = "invented"
        result["automatic_validation"]["core_result_hash"] = contracts.fingerprint(
            {key: value for key, value in result.items() if key != "automatic_validation"})
        with self.assertRaisesRegex(gates.ValidationError, "immutable core"):
            gates.validate_operation_result(request, result, self.store, self.objects, {"historical": True})

    def test_historical_snapshot_requires_original_proof_and_current_code(self):
        request, result = self.accepted_status()
        core = {key: value for key, value in result.items() if key != "automatic_validation"}
        with self.assertRaisesRegex(gates.ValidationError, "new revision"):
            gates.validate_operation_result(request, core, self.store, self.objects, {"historical": True})
        with patch.object(verify, "code_identity", return_value={"synthetic_changed_source": "0"*64}):
            with self.assertRaisesRegex(gates.ValidationError, "source changed"):
                gates.validate_operation_result(request, result, self.store, self.objects, {"historical": True})

    def test_historical_final_verdict_cannot_be_resealed(self):
        request, result = self.accepted_status()
        result["automatic_validation"]["scope"] = "invented_qualification"
        proof = self.objects.read_json(result["automatic_validation"]["final_review_ref"])
        proof["scope"] = "invented_qualification"
        result["automatic_validation"]["final_review_ref"] = self.objects.put_json(proof)
        with self.assertRaisesRegex(gates.ValidationError, "final publication"):
            gates.validate_operation_result(request, result, self.store, self.objects, {"historical": True})

    def test_unavailable_news_is_source_checked_partial(self):
        value, inputs = self.collection(unavailable=True)
        receipt = gates.validate("news", value, inputs, self.store, self.objects)
        self.assertEqual(receipt["status"], "partial")
        self.assertFalse(receipt["readiness"]["trade_ready"])
        self.assertTrue(receipt["required_actions"])

    def test_article_metadata_resealed_against_original_bytes_is_rejected(self):
        value, inputs = self.collection()
        self.assertEqual(gates.validate("news", value, inputs, self.store, self.objects)["status"], "passed")
        value["versions"][0]["title"] = "Invented article title"
        self.reseal_collection(value)
        with self.assertRaises(gates.ValidationError):
            gates.validate("news", value, inputs, self.store, self.objects)

    def test_changed_original_article_bytes_are_rejected(self):
        value, inputs = self.collection()
        value["retrievals"][0]["raw_ref"] = self.objects.put_bytes(b"<html>altered source</html>")
        self.reseal_collection(value)
        with self.assertRaisesRegex(gates.ValidationError, "Collection original persisted manifest"):
            gates.validate("news", value, inputs, self.store, self.objects)

    def test_unreadable_document_is_partial_and_cannot_invent_extracted_blocks(self):
        request, result = self.unreadable_document()
        checked = gates.validate_operation_result(request, result, self.store, self.objects)
        self.assertEqual(checked["status"], "partial")
        self.assertFalse(checked["readiness"]["trade_ready"])
        document = copy.deepcopy(result["document"])
        document["blocks"] = [{"text": "Invented parsed original"}]
        document["text_sha256"] = contracts.fingerprint(document["blocks"])
        document["document_id"] = contracts.fingerprint({key: value for key, value in document.items()
                                                         if key not in {"document_id", "raw_ref", "registry_ref"}})
        result.update(document=document, document_ref=self.objects.put_json(document))
        with self.assertRaisesRegex(gates.ValidationError, "body blocks"):
            gates.validate_operation_result(request, result, self.store, self.objects)

    def test_lease_loss_is_not_converted_to_partial(self):
        with patch.object(verify, "code_identity", side_effect=LeaseLost("synthetic lost owner")):
            with self.assertRaises(LeaseLost):
                gates.validate("code", {}, {}, self.store, self.objects)

    def test_failed_numeric_trace_is_rejected_but_recovered_errors_remain(self):
        context, data = {"context_hash": "a"*64}, {}
        value = {"status": "blocked", "reason": "synthetic insufficient evidence", "context_hash": context["context_hash"],
                 "input_hash": contracts.fingerprint(data), "orders": {"status": "blocked", "orders": []},
                 "validation_trace": [{"validation": None, "output_hash": "b"*64, "failed_attempts": [{"error": "original failure"}]}]}
        with patch.object(verify, "numerical_invariants") as arithmetic:
            with self.assertRaisesRegex(gates.ValidationError, "unrecovered"):
                gates.validate("calculation", value, {"context": context, "data": data}, self.store, self.objects)
            arithmetic.assert_not_called()
        value["validation_trace"][0]["validation"] = {"status": "partial"}
        with patch.object(verify, "numerical_invariants", return_value={"status": "not_applicable"}), \
                patch.object(verify, "reproduce", return_value={"status": "passed"}) as replay:
            receipt = gates.validate("calculation", value, {"context": context, "data": data}, self.store, self.objects)
        replay.assert_called_once()
        self.assertEqual(receipt["status"], "partial")
        self.assertFalse(receipt["readiness"]["trade_ready"])
        self.assertEqual(len(value["validation_trace"][0]["failed_attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
