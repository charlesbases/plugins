"""Declared policy and review-budget counterexamples; no market evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import allocation
import ledger
import newtrade_guard as guard
import portfolio_mpc
from artifacts import Artifacts
from contracts import fingerprint
from state_store import Store
from test_decision_certificate import risk_case

AT = "2026-10-05T12:00:00Z"


def declared_policy():
    scripts = Path(guard.__file__).parent
    policy = json.loads((scripts.parent / "references/strategy.example.json").read_text(encoding="utf-8"))["trade_policy"]
    policy["registration"] = "declared"
    policy["family"].update(start_at="2026-10-01T00:00:00Z", end_at="2026-11-01T00:00:00Z", max_reviews=2)
    return {"trade_policy": policy}


class PolicyGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name, "governance")
        self.artifacts = Artifacts(self.store.base)
        self.spec = declared_policy()
        event = {"id": "opening", "type": "opening", "sequence": 1,
                 "effective_at": AT, "known_at": AT, "recorded_at": AT,
                 "data": {"cash": "100", "lots": [], "prices": {}, "price_dates": {}}}
        state = ledger.apply_event(ledger.initial_state("CNY"), event)
        self.store.begin("setup", {"explicit_engineering_account": True})
        with self.store.lease("setup"):
            self.store.put("account", "main", state, immutable=False)
            self.store.put("ledger_event", "opening", event)
            self.store.put("ledger_event_owner", "opening", {"account_id": "main"})
            self.store.complete("setup", {"status": "synthetic"})

    def reserve(self, identity, spec=None):
        self.store.begin(identity, {"decision_at": AT, "spec": spec or self.spec})
        with self.store.lease(identity):
            return guard.reserve_review(self.store, self.artifacts, "main", spec or self.spec, AT, decision_id=identity)

    def test_no_winner_and_resumed_review_consume_one_logical_slot(self):
        first = self.reserve("no-winner")
        resumed = self.reserve("no-winner")
        second = self.reserve("another-no-winner")
        self.assertEqual(first, resumed)
        self.assertEqual(first["trade_family_review_index"], 1)
        self.assertEqual(second["trade_family_review_index"], 2)
        third = self.reserve("over-budget")
        self.assertFalse(third["qualification_scope"]["eligible"])
        self.assertIn("registered_review_budget_exhausted", third["qualification_scope"]["reasons"])

    def test_nonregistered_or_inactive_policy_cannot_reserve(self):
        for mode in ("illustrative", "before", "at-end"):
            spec = copy.deepcopy(self.spec)
            if mode == "illustrative":
                spec["trade_policy"]["registration"] = mode
            elif mode == "before":
                spec["trade_policy"]["family"]["start_at"] = "2026-10-06T00:00:00Z"
            else:
                spec["trade_policy"]["family"]["end_at"] = AT
            with self.subTest(mode=mode):
                result = self.reserve(mode, spec)
                self.assertFalse(result["qualification_scope"]["eligible"])
                self.assertIsNone(result["review_reservation"])

    def test_readonly_validation_does_not_spend_a_slot(self):
        self.reserve("one")
        before = self.store.get("trade_family_frontier", "main:" + fingerprint(self.spec["trade_policy"]["family"]))
        for _ in range(3):
            guard.read_inputs(self.store, self.artifacts, "main", self.spec, AT, decision_id="one")
        self.assertEqual(self.store.get("trade_family_frontier", "main:" + fingerprint(self.spec["trade_policy"]["family"])), before)

    def test_illustrative_policy_cannot_supply_recovery_winner(self):
        context, paths, _ = risk_case(calibration=False)
        context["spec"]["trade_policy"]["registration"] = "illustrative"
        context["context_hash"] = fingerprint({k: v for k, v in context.items() if k != "context_hash"})
        value = portfolio_mpc.optimise(context, paths, allocation.build_source_actions(context))
        self.assertIsNone(value["selected_policy"])
        self.assertEqual(value["current_action"], {"buys": [], "sells": []})

    def test_trade_history_stage_survives_reservation_and_readonly_revalidation(self):
        import stage_validation
        frozen = {"account_id": "main", "spec": self.spec, "decision_at": AT, "decision_id": "one"}
        history = guard.read_inputs(self.store, self.artifacts, "main", self.spec, AT, decision_id="one")["trade_state"]
        actual = self.reserve("one")
        for _ in range(3):
            self.assertEqual(stage_validation.validate("trade-history", history, frozen, self.store, self.artifacts)["status"], "passed")
            self.assertEqual(stage_validation.validate("trade-inputs", actual, frozen, self.store, self.artifacts)["status"], "passed")
        self.assertEqual(self.reserve("one")["trade_family_review_index"], 1)

    def test_preserved_unrecorded_comparison_cannot_reset_budget(self):
        self.store.begin("legacy", {"explicit_engineering_old_comparison": True})
        with self.store.lease("legacy"):
            self.store.put("decision", "legacy", {"account_id": "main", "context": {"spec": self.spec}})
            self.store.complete("legacy", {"status": "synthetic"})
        value = self.reserve("new")
        self.assertIsNone(value["review_reservation"])
        self.assertIn("historical_review_budget_unverified", value["qualification_scope"]["reasons"])

    def test_same_family_cannot_change_registered_confidence(self):
        self.reserve("original")
        changed = copy.deepcopy(self.spec)
        changed["trade_policy"]["inference"]["confidence_level"] = 0.6
        value = self.reserve("changed", changed)
        self.assertIsNone(value["review_reservation"])
        self.assertFalse(value["qualification_scope"]["eligible"])
        self.assertIn("registered_family_policy_changed", value["qualification_scope"]["reasons"])

    def test_governance_block_preserves_and_validates_ready_family_inventory(self):
        import candidate_readiness
        import verify
        from pipeline import select_family
        from test_economic_industry_readiness import inputs
        state, codes, data, model_spec, at, clock, terms = inputs()
        frozen = {"state": state, "eligible_codes": codes, "data": data, "spec": model_spec,
                  "decision_at": at, "clock_context": clock, "fee_contracts": terms}
        readiness = candidate_readiness.assess_readiness(**frozen)
        self.assertTrue(readiness["candidate_families"])
        refs = {"data_ref": self.artifacts.put_json(data), "readiness_ref": self.artifacts.put_json(readiness),
                "readiness_inputs_ref": self.artifacts.put_json(frozen)}
        self.store.begin("blocked-research", {"explicit_engineering_research": True})
        with self.store.lease("blocked-research"):
            actual = guard.reserve_review(self.store, self.artifacts, "main", self.spec, at, decision_id="blocked-research")
        self.assertFalse(actual["qualification_scope"]["eligible"])
        inventory = {"results": [], "selection": select_family([], self.artifacts), "readiness_ref": refs["readiness_ref"],
                     "governance_block": {"scope": actual["qualification_scope"], "families": readiness["candidate_families"]}}
        bundle = {**refs, "decision_id": "blocked-research", "account_id": "main", "decision_at": at,
                  "context": None, "orders": {"orders": []}, "trade_review_spec": self.spec,
                  "trade_review_inputs": actual, "family_results_ref": self.artifacts.put_json(inventory)}
        verify._verify_source_families(bundle, self.artifacts, self.store)
        import stage_validation
        stage_inputs = {"family_results": [], "readiness_ref": refs["readiness_ref"], "data": data,
                        "governance_block": inventory["governance_block"], "trade_review_inputs": actual,
                        "spec": self.spec, "account_id": "main", "decision_at": at, "decision_id": "blocked-research"}
        checked = stage_validation.validate("family-selection", inventory["selection"], stage_inputs, self.store, self.artifacts)
        self.assertEqual(checked["status"], "partial")
        self.assertFalse(checked["readiness"]["trade_ready"])
        for change in ("omit-governance", "omit-family"):
            bad = copy.deepcopy(bundle)
            if change == "omit-governance":
                bad["trade_review_inputs"] = None
            else:
                changed = copy.deepcopy(inventory)
                changed["governance_block"]["families"] = []
                bad["family_results_ref"] = self.artifacts.put_json(changed)
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify._verify_source_families(bad, self.artifacts, self.store)

    def test_interrupted_old_numerical_binding_is_not_zero_history(self):
        context = {"account_id": "main", "spec": self.spec}
        self.store.begin("old-interrupted", {"explicit_engineering_old_binding": True})
        with self.store.lease("old-interrupted"):
            self.store.put("validation_binding", "old-numeric", {"stage": "numeric_mpc", "operation_id": "old-interrupted",
                           "inputs_ref": self.artifacts.put_json({"context": context})})
            self.store.fail("old-interrupted", "retryable", {"status": "synthetic"})
        value = self.reserve("new")
        self.assertIsNone(value["review_reservation"])
        self.assertIn("historical_review_budget_unverified", value["qualification_scope"]["reasons"])

    def test_two_concurrent_requests_receive_distinct_serialized_slots(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(2)
        def run(identity):
            store = Store(self.temporary.name, "governance")
            store.begin(identity, {"decision_at": AT, "spec": self.spec})
            with store.lease(identity):
                barrier.wait(timeout=10)
                return guard.reserve_review(store, Artifacts(store.base), "main", self.spec, AT, decision_id=identity)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, name) for name in ("concurrent-a", "concurrent-b")]
            self.assertEqual({f.result()["trade_family_review_index"] for f in futures}, {1, 2})

    def test_expired_policy_cannot_supply_recovery_winner(self):
        context, paths, _ = risk_case(calibration=False)
        policy = context["spec"]["trade_policy"]
        policy["registration"] = "declared"
        policy["family"].update(start_at="2029-01-01T00:00:00+08:00", end_at=context["decision_at"])
        context["context_hash"] = fingerprint({k: v for k, v in context.items() if k != "context_hash"})
        value = portfolio_mpc.optimise(context, paths, allocation.build_source_actions(context))
        self.assertIsNone(value["selected_policy"])


if __name__ == "__main__":
    unittest.main()
