"""Principal-risk arithmetic, confirmation and time boundaries; no market evidence."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import risk_profile as profile
import ledger
from contracts import fingerprint
from state_store import Store


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="investment-profile-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name), "p")
        maintenance = self.store.maintenance()
        maintenance.__enter__()
        self.addCleanup(maintenance.__exit__, None, None, None)
        self.now = "2030-01-01T08:00:00Z"
        self.clock = patch.object(profile, "utc_now", side_effect=lambda: self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)

    def initialize(self, rate="0.25"):
        return profile.initialize({"principal": "10000", "currency": "CNY", "loss_tolerance": rate,
            "as_of": self.now, "user_source": {"message": "Confirmed test principal and risk", "confirmed_at": self.now},
            "confirmed_initial_all_cash": True, "account_hash": None}, self.store, "start")

    def state(self):
        return self.store.get("account", "main")

    def apply(self, kind, data, identity):
        if kind == "cashflow":
            data = {"transfer_id": identity, "revision_id": identity, "previous_revision": None, "valuation": None, **data}
        state = self.state()
        event = {"id": identity, "sequence": state["sequence"] + 1, "type": kind,
                 "effective_at": self.now, "known_at": self.now, "recorded_at": self.now, "data": data}
        state = ledger.apply_event(state, event)
        self.store.put("ledger_event", identity, event)
        self.store.put("ledger_event_owner", identity, {"account_id": "main"})
        self.store.put("account", "main", state, immutable=False)

    def test_fresh_requires_inputs_and_zero_risk_is_explicit(self):
        self.assertEqual(profile.status(self.store)["status"], "confirmation_required")
        self.assertIsNone(self.store.get("account", "main"))
        self.initialize("0")
        risk = profile.resolve(self.store, self.state(), self.now, {})
        self.assertEqual(risk["principal_floor"], "10000")
        self.assertEqual(risk["remaining_loss_budget"], "0")

    def test_new_plan_requires_no_manual_review_or_holding_days(self):
        self.initialize()
        account, capital = copy.deepcopy(self.state()), self.store.get("capital_current", "main")
        constraints = {"platform": "TT", "currency": "CNY", "goal": "Synthetic rolling investment",
            "excluded_categories": [], "position_limits": {"fund_group_limits": {}, "sector_limits": {}}}
        payload = {"constraints": constraints, "current_constraints_hash": None,
            "user_source": {"message": "Synthetic business constraints only", "confirmed_at": self.now}}
        result = profile.update_constraints(payload, self.store, "new-plan")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(profile.update_constraints(payload, self.store, "new-plan"), result)
        self.assertEqual(self.state(), account)
        self.assertEqual(self.store.get("capital_current", "main"), capital)
        for field, value in (("review_horizon_days", 365), ("holding_period_days", [7, 30, 60, 90])):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unknown"):
                profile.validate_constraints({**constraints, field: value})

    def test_historical_plan_hash_and_lineage_preserve_retired_metadata(self):
        self.initialize()
        account, capital = copy.deepcopy(self.state()), self.store.get("capital_current", "main")
        constraints = {"platform": "TT", "currency": "CNY", "goal": "Synthetic historical plan",
            "excluded_categories": [], "position_limits": {"fund_group_limits": {}, "sector_limits": {}}}
        revision = {"schema_version": 4, "revision_count": 1, "previous_hash": None, "effective_at": self.now,
            "user_source": {"message": "Original synthetic plan", "confirmed_at": self.now},
            "constraints": {**constraints, "review_horizon_days": 365, "holding_period_days": [7, 30, 60, 90]}}
        identity = fingerprint(revision)
        record = {**revision, "revision_id": identity}
        original = copy.deepcopy(record)
        self.store.put("plan_revision", identity, revision)
        self.store.put("plan_constraints", "main", record, immutable=False)
        self.assertEqual(profile.validate_plan(record), original)
        tampered = copy.deepcopy(record)
        tampered["constraints"]["review_horizon_days"] = 730
        with self.assertRaisesRegex(ValueError, "content hash"):
            profile.validate_plan(tampered)
        result = profile.update_constraints({"constraints": constraints, "current_constraints_hash": fingerprint(record),
            "user_source": {"message": "Synthetic business-only revision", "confirmed_at": self.now}}, self.store, "replace-plan")
        current = self.store.get("plan_constraints", "main")
        self.assertEqual(current["revision_count"], 2)
        self.assertEqual(current["previous_hash"], fingerprint(original))
        self.assertEqual(current["constraints"], constraints)
        self.assertEqual(result["revision_id"], current["revision_id"])
        self.assertEqual(self.store.get("plan_revision", identity), revision)
        self.assertEqual(record, original)
        self.assertEqual(self.state(), account)
        self.assertEqual(self.store.get("capital_current", "main"), capital)

    def test_principal_loss_not_market_drawdown_and_external_flows(self):
        self.initialize()
        self.assertEqual(profile.resolve(self.store, self.state(), self.now, {})["principal_floor"], "7500")
        # A market loss changes equity; the confirmed principal remains 10,000.
        state = self.state()
        state.update(cash="8500")
        self.store.put("account", "main", state, immutable=False)
        risk = profile.resolve(self.store, state, self.now, {})
        self.assertEqual(risk["remaining_loss_budget"], "1000")
        self.now = "2030-01-02T08:00:00Z"
        self.apply("cashflow", {"amount": "2000"}, "deposit")
        risk = profile.resolve(self.store, self.state(), self.now, {})
        self.assertEqual((risk["net_principal"], risk["principal_floor"], risk["remaining_loss_budget"]), ("12000", "9000", "1500"))
        self.apply("cashflow", {"amount": "-1000"}, "withdrawal")
        risk = profile.resolve(self.store, self.state(), self.now, {})
        self.assertEqual((risk["net_principal"], risk["principal_floor"], risk["remaining_loss_budget"]), ("11000", "8250", "1250"))

    def test_temporary_window_reverts_and_stale_publication_rejected(self):
        self.initialize()
        payload = {"mode": "temporary", "loss_tolerance": "0.4", "start_at": None,
                   "end_at": "2030-01-02T08:00:00Z", "user_source": {"message": "Temporary test change", "confirmed_at": self.now},
                   "current_profile_hash": profile.status(self.store)["profile_hash"]}
        result = profile.update(payload, self.store, "change")
        risk = profile.resolve(self.store, self.state(), self.now, {})
        self.assertEqual(risk["principal_floor"], "6000")
        self.now = "2030-01-02T08:00:00Z"
        self.assertEqual(profile.update(payload, self.store, "change"), result)
        self.assertEqual(profile.resolve(self.store, self.state(), self.now, {})["principal_floor"], "7500")
        with self.assertRaisesRegex(ValueError, "changed"):
            profile.assert_current(self.store, risk, self.state(), self.now, {})

    def test_unknown_existing_account_cannot_be_reset_as_cash(self):
        state = ledger.initial_state("CNY")
        state["unknown"] = [{"reason": "cash_unknown"}]
        self.store.put("account", "main", state, immutable=False)
        with self.assertRaisesRegex(ValueError, "Account changed"):
            self.initialize()
        self.assertEqual(self.state(), state)

    def test_overlapping_temporary_windows_and_future_confirmation_rejected(self):
        self.initialize()
        payload = {"mode": "temporary", "loss_tolerance": "0.4", "start_at": "2030-01-02T08:00:00Z",
                   "end_at": "2030-01-04T08:00:00Z", "user_source": {"message": "Test", "confirmed_at": self.now},
                   "current_profile_hash": profile.status(self.store)["profile_hash"]}
        profile.update(payload, self.store, "one")
        payload.update(start_at="2030-01-03T08:00:00Z", current_profile_hash=profile.status(self.store)["profile_hash"])
        with self.assertRaisesRegex(ValueError, "disjoint"):
            profile.update(payload, self.store, "two")
        payload["user_source"]["confirmed_at"] = "2031-01-01T08:00:00Z"
        with self.assertRaisesRegex(ValueError, "future"):
            profile.update(payload, self.store, "future")


if __name__ == "__main__":
    unittest.main()
