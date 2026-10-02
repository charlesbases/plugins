"""Physical archived HTTP/source byte regressions, never profit evidence."""
import copy
import unittest
import paired_source_audit
from test_verified_data import VerifiedDataTests, CODE


class PhysicalPairedAuditTests(unittest.TestCase):
    setUp = VerifiedDataTests.setUp

    def inputs(self):
        data = self.artifacts.read_json(self.bundle["data_ref"])
        market = self.artifacts.read_json(self.bundle["market_record_ref"])["market_ref"]
        terms = {row["code"]: row["fee_contract"] for row in market["terms"]}
        context = {"decision_at": self.bundle["decision_at"], "allocation_codes": [CODE], "fee_contracts": terms}
        runtime = {"artifacts": self.artifacts, "store": self.store, "binding_bundle": self.bundle}
        frame = {"source_nav": data["nav"], "fee_scope": "current_terms_repriced", "fee_contracts": terms,
                 "source_frontier": [], "policy_scope": "current_action_then_hold"}
        return runtime, context, data, {"codes": [CODE]}, frame

    def test_physical_archive_and_current_terms_are_read_but_no_historical_certificate(self):
        runtime, context, data, record, frame = self.inputs()
        proof = paired_source_audit.audit_pair(runtime, context, data, [], record, frame)
        self.assertTrue(proof["physical_NAV_verified"])
        self.assertTrue(proof["physical_current_terms_verified"])
        self.assertFalse(proof["strict_PIT_verified"])
        self.assertEqual(proof["status"], "partial")
        self.assertTrue(proof["required_source_gaps"])

    def test_registry_resealed_NAV_cannot_override_physically_audited_archive(self):
        runtime, context, data, record, frame = self.inputs()
        frame = copy.deepcopy(frame)
        frame["source_nav"][CODE][0]["nav"] = 99.
        with self.assertRaisesRegex(ValueError, "physically audited original NAV"):
            paired_source_audit.audit_pair(runtime, context, data, [], record, frame)

    def test_resealed_outer_data_cannot_replace_original_captured_source_bytes(self):
        runtime, context, data, record, frame = self.inputs()
        altered = copy.deepcopy(data)
        altered["nav"][CODE][0]["nav"] = 99.
        binding = copy.deepcopy(runtime["binding_bundle"])
        binding["data_ref"] = self.artifacts.put_json(altered)
        runtime["binding_bundle"] = binding
        with self.assertRaisesRegex(ValueError, "independently audited raw research"):
            paired_source_audit.audit_pair(runtime, context, altered, [], record, frame)
