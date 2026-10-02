"""Real SQLite publication concurrency; no market or future gain assertions."""
import datetime as dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))

from contracts import StaleSnapshot, fingerprint, instant, utc_now
from state_store import Store, LeaseLost
import pipeline


class PublicationFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root, "case", timeout=.1, lease_seconds=.25, heartbeat_seconds=.04)

    def tearDown(self):
        self.tmp.cleanup()

    def test_readonly_guard_outlasts_multiple_leases_with_real_heartbeat(self):
        self.store.begin("review", {})
        with self.store.lease("review"):
            original = self.store.operation("review")["lease_until"]
            renewed = []
            def slow_readonly():
                time.sleep(.7)
                renewed.append(self.store.operation("review")["lease_until"])
            result = self.store.complete("review", {"published": True}, before_commit=slow_readonly)
        self.assertTrue(result["published"])
        self.assertGreater(renewed[0], original+.25)
        self.assertEqual(self.store.operation("review")["status"], "completed")

    def test_competing_financial_update_during_guard_cannot_publish_old_order(self):
        self.store.begin("opening", {})
        with self.store.lease("opening"):
            self.store.complete("opening", {}, [{"kind": "account", "key": "main", "value": {"cash": "10"}, "immutable": False}])
        self.store.begin("review", {})
        other = Store(self.root, "case", timeout=.1)
        def change_cash():
            other.begin("confirmed-withdrawal", {})
            with other.lease("confirmed-withdrawal"):
                other.complete("confirmed-withdrawal", {}, [{"kind": "account", "key": "main", "value": {"cash": "0"}, "immutable": False}])
        with self.store.lease("review"):
            with self.assertRaises(StaleSnapshot):
                self.store.complete("review", {"buy_cash": "10"},
                    [{"kind": "decision", "key": "old-order", "value": {"buy_cash": "10"}}],
                    expected=[{"kind": "account", "key": "main", "hash": fingerprint({"cash": "10"})}], before_commit=change_cash)
        self.assertEqual(self.store.get("account", "main"), {"cash": "0"})
        self.assertIsNone(self.store.get("decision", "old-order"))
        self.assertIsNone(self.store.get("operation_result", "review"))

    def test_expired_original_owner_cannot_publish_after_real_takeover(self):
        self.store.begin("review", {})
        other = Store(self.root, "case", timeout=.1)
        def replace_owner():
            time.sleep(.35)
            other.begin("review", {})
            with other.lease("review"):
                other.complete("review", {"winner": "new-generation"})
        with patch.object(self.store, "renew", side_effect=LeaseLost("simulated stopped heartbeat")):
            with self.store.lease("review"):
                with self.assertRaises(LeaseLost):
                    self.store.complete("review", {"winner": "old-generation"},
                        [{"kind": "decision", "key": "old-order", "value": {}}], before_commit=replace_owner)
        self.assertEqual(self.store.operation("review")["result"], {"winner": "new-generation"})
        self.assertIsNone(self.store.get("decision", "old-order"))

    def test_clock_expiry_during_readonly_validation_rolls_back_all_writes(self):
        self.store.begin("review", {})
        deadline = instant(utc_now())+dt.timedelta(seconds=.1)
        seen = []
        def fence(encoded):
            seen.append(json.loads(encoded))
            if instant(utc_now()) >= deadline:
                raise StaleSnapshot("Confirmed temporary risk window expired")
        with self.store.lease("review"):
            with self.assertRaises(StaleSnapshot):
                self.store.complete("review", {"buy_cash": "10"},
                    [{"kind": "decision", "key": "expired-order", "value": {}}],
                    before_commit=lambda: time.sleep(.2), commit_guard=fence)
        self.assertEqual(seen, [{"buy_cash": "10"}])
        self.assertIsNone(self.store.get("decision", "expired-order"))
        self.assertIsNone(self.store.get("operation_result", "review"))

    def test_pipeline_deadline_preserves_temporary_risk_time_boundary(self):
        package = {"writes": [{"kind": "decision", "value": {"context": {
            "decision_at": "2030-01-01T00:00:00Z", "risk_state": {"valid_until": "2030-01-01T00:00:10Z"},
            "market_ref": {"observed_at": "2030-01-01T00:00:00Z", "terms": []},
            "spec": {"availability": {"max_market_age_seconds": 100, "max_terms_age_seconds": 100},
                     "trade_policy": {"family": {"end_at": "2030-01-02T00:00:00Z"}}}}}}]}
        self.assertEqual(pipeline._publication_deadline(package, None), instant("2030-01-01T00:00:10Z"))
        package["writes"][0]["value"]["context"]["spec"]["trade_policy"]["family"]["end_at"] = "2030-01-01T00:00:05Z"
        self.assertEqual(pipeline._publication_deadline(package, None), instant("2030-01-01T00:00:05Z"))


if __name__ == "__main__":
    unittest.main()
