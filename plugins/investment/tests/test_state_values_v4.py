"""Large logical values retain identity, fences and archive replay semantics."""
from contextlib import closing
import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
from artifacts import Artifacts
from contracts import ConflictError, EvidenceError, StaleSnapshot, canonical_bytes, fingerprint, strict_json_loads
import state_store
from state_store import CAS_JSON_PREFIX, ROW_LIMIT, LeaseLost, Store


def discovery():
    """Synthetic texts in the real discovery groups/exposures collection shape."""
    codes = [f"{number:06d}" for number in range(128)]
    return {"schema_version": 4, "codes": codes,
            "groups": [{"group_id": f"group-{code}", "kind": "same_legal_fund",
                        "identity_key": code, "members": [code], "coverage_status": "pending",
                        "membership_evidence": [], "coverage_scope": None} for code in codes],
            "exposures": {code: [{"thesis_id": f"thesis-{code}", "direction": "positive",
                                  "disclosed_text": "synthetic-source-excerpt " * 450}]
                          for code in codes},
            "coverage": {"scope_codes": codes, "unexamined_codes": []}}


class LargeStateValueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), "large")
        self.value = discovery()
        self.assertGreater(len(canonical_bytes(self.value)), ROW_LIMIT)

    def tearDown(self):
        self.tmp.cleanup()

    def raw_record(self, kind, key):
        with closing(sqlite3.connect(self.store.path)) as conn:
            return conn.execute("SELECT value,hash FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()

    def publish(self, operation="discovery"):
        self.store.begin(operation, self.value)
        with self.store.lease(operation):
            self.store.put("fund_discovery", "candidate-set", self.value)
            self.store.stage(operation, "discover", self.value)
            self.store.complete(operation, self.value)

    def test_large_discovery_preserves_logical_hash_and_all_profiles(self):
        self.publish()
        payload, digest = self.raw_record("fund_discovery", "candidate-set")
        self.assertTrue(payload.startswith(CAS_JSON_PREFIX))
        self.assertLessEqual(len(payload.encode()), ROW_LIMIT)
        self.assertEqual(digest, fingerprint(self.value))
        self.assertEqual(self.store.get("fund_discovery", "candidate-set"), self.value)
        self.assertEqual(list(self.store.scan("fund_discovery")), [("candidate-set", self.value)])
        self.assertEqual(len(self.store.operation("discovery")["result"]["exposures"]), 128)
        observed = []
        self.assertEqual(self.store.audit(observed.append)["status"], "passed")
        self.assertIn(self.value, observed)

    def test_large_request_result_stage_survive_archive_and_idempotent_retry(self):
        self.publish()
        with self.store.maintenance():
            archive = self.store.archive(max_bytes=ROW_LIMIT)
        self.assertEqual(archive["status"], "archived")
        retry = self.store.begin("discovery", self.value)
        self.assertEqual(retry["request"], self.value)
        self.assertEqual(retry["result"], self.value)
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(self.store.get("stage", "discovery:discover")["result"], self.value)
        self.assertEqual(self.store.audit()["archives"], 1)
        changed = copy.deepcopy(self.value)
        changed["exposures"]["000000"][0]["disclosed_text"] = "Changed fact"
        with self.assertRaises(ConflictError):
            self.store.begin("discovery", changed)
        with self.store.maintenance(), self.assertRaises(ConflictError):
            self.store.put("fund_discovery", "candidate-set", changed)
        with closing(sqlite3.connect(self.store.base / archive["path"])) as conn:
            request, result = conn.execute("SELECT request,result FROM operations WHERE id='discovery'").fetchone()
            self.assertTrue(request.startswith(CAS_JSON_PREFIX))
            self.assertTrue(result.startswith(CAS_JSON_PREFIX))
            self.assertLessEqual(max(len(request.encode()), len(result.encode())), ROW_LIMIT)

    def test_large_failure_result_can_be_retried_and_completed(self):
        self.store.begin("retry", {})
        with self.store.lease("retry"):
            self.store.fail("retry", "retryable", self.value)
        self.assertEqual(self.store.operation("retry")["result"], self.value)
        with self.store.lease("retry"):
            self.store.complete("retry", {"confirmed": self.value})
        self.assertEqual(self.store.operation("retry")["result"], {"confirmed": self.value})
        self.assertEqual(self.store.audit()["status"], "passed")

    def test_mutable_large_values_use_logical_compare_and_swap(self):
        self.store.begin("first", {})
        with self.store.lease("first"):
            self.store.put("fund_discovery", "current", self.value, False)
            self.store.complete("first", {})
        changed = copy.deepcopy(self.value)
        changed["groups"].pop()
        self.store.begin("update", {})
        with self.store.lease("update"):
            self.store.put("fund_discovery", "current", changed, False)
            self.store.complete("update", {})
        self.store.begin("stale", {})
        with self.store.lease("stale"), self.assertRaises(StaleSnapshot):
            self.store.complete("stale", self.value, expected=[
                {"kind": "fund_discovery", "key": "current", "hash": fingerprint(self.value)}])
        self.assertEqual(self.store.get("fund_discovery", "current"), changed)
        self.assertIsNone(self.store.get("operation_result", "stale"))

    def test_tampered_object_is_rejected_by_all_logical_read_paths(self):
        self.publish()
        payload, _ = self.raw_record("fund_discovery", "candidate-set")
        reference = strict_json_loads(payload[len(CAS_JSON_PREFIX):])["value_ref"]
        (self.store.base / reference["path"]).write_bytes(b"changed")
        checks = [lambda: self.store.get("fund_discovery", "candidate-set"),
                  lambda: list(self.store.scan("fund_discovery")),
                  lambda: self.store.operation("discovery"), self.store.audit]
        for check in checks:
            with self.subTest(reader=check), self.assertRaises(EvidenceError):
                check()

    def test_replaced_valid_reference_still_fails_logical_hash_check(self):
        self.publish()
        different = {"other": "x" * (ROW_LIMIT + 1)}
        encoded = state_store.encode_stored_json(different, self.store.base)
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("UPDATE records SET value=? WHERE kind='fund_discovery'", (encoded,))
            conn.commit()
        with self.assertRaisesRegex(EvidenceError, "Stored record content changed"):
            self.store.get("fund_discovery", "candidate-set")

    def test_archive_does_not_hide_missing_referenced_objects(self):
        self.publish()
        payload, _ = self.raw_record("fund_discovery", "candidate-set")
        reference = strict_json_loads(payload[len(CAS_JSON_PREFIX):])["value_ref"]
        with self.store.maintenance():
            self.store.archive(max_bytes=ROW_LIMIT)
        (self.store.base / reference["path"]).unlink()
        with self.assertRaises(EvidenceError):
            self.store.get("fund_discovery", "candidate-set")
        with self.assertRaises(EvidenceError):
            self.store.begin("discovery", self.value)
        with self.assertRaises(EvidenceError):
            self.store.audit()

    def test_user_values_cannot_collide_with_internal_envelopes(self):
        values = [CAS_JSON_PREFIX + '{"layers":0,"value_ref":{}}',
                  {"layers": 0, "value_ref": {"path": "ordinary user data"}},
                  CAS_JSON_PREFIX + "x" * (ROW_LIMIT + 1)]
        self.store.begin("values", {})
        with self.store.lease("values"):
            for number, value in enumerate(values):
                self.store.put("literal", str(number), value)
        for number, value in enumerate(values):
            self.assertEqual(self.store.get("literal", str(number)), value)
        self.assertTrue(self.raw_record("literal", "0")[0].startswith('"'))

    def test_expired_lease_after_object_write_cannot_publish_reference(self):
        self.store.begin("expired", {})
        actual_encoder = state_store.encode_stored_json
        with self.store.lease("expired"), self.assertRaises(LeaseLost):
            with self.store.transaction() as conn:
                def expire_after_encoding(value, base):
                    result = actual_encoder(value, base)
                    # Deterministically expire the current lease between object
                    # persistence and transaction publication; no wall-clock wait.
                    conn.execute("UPDATE operations SET lease_until=0 WHERE id='expired'")
                    return result
                with patch.object(state_store, "encode_stored_json", expire_after_encoding):
                    self.store.put("fund_discovery", "unpublished", self.value, conn=conn)
        self.assertIsNone(self.store.get("fund_discovery", "unpublished"))
        self.assertIsNone(self.store.get("operation_result", "expired"))
        self.assertTrue(list((self.store.base / "objects").rglob("*")))

    def test_large_json_uses_chunked_objects_with_verified_reassembly(self):
        value = {"source_text": "x" * (16 * ROW_LIMIT + 1)}
        self.store.begin("chunks", {})
        with self.store.lease("chunks"):
            self.store.put("source", "large", value)
        payload, _ = self.raw_record("source", "large")
        envelope = strict_json_loads(payload[len(CAS_JSON_PREFIX):])
        self.assertEqual(len(envelope["value_ref"]["chunks"]), 2)
        self.assertEqual(self.store.get("source", "large"), value)

    def test_reference_inventory_can_itself_be_stored_outside_bounded_cell(self):
        value = {"source_text": "x" * (32 * ROW_LIMIT + 1)}
        self.store.begin("inventory", {})
        # A smaller encoded-cell bound exercises the real reference-inventory
        # boundary without allocating hundreds of gigabytes of synthetic data.
        with patch.object(state_store, "ROW_LIMIT", 512), self.store.lease("inventory"):
            self.store.put("source", "inventory", value)
        payload, _ = self.raw_record("source", "inventory")
        self.assertLessEqual(len(payload.encode()), 512)
        envelope = strict_json_loads(payload[len(CAS_JSON_PREFIX):])
        self.assertEqual(envelope["layers"], 1)
        manifest = Artifacts(self.store.base).read_json(envelope["value_ref"])
        self.assertEqual(len(manifest["chunks"]), 3)
        self.assertEqual(self.store.get("source", "inventory"), value)


if __name__ == "__main__":
    unittest.main()
