"""New cross-boundary trial regressions; source/network/statistics are fixtures."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_trial import TrialServiceTests, synthetic_terms
import trial
import strategy
import risk_profile as profile
from contracts import fingerprint, StaleSnapshot


class TrialV4Regression(unittest.TestCase):
    def setUp(self):
        self.f = TrialServiceTests("test_early_endpoint_waits_and_incomplete_final_window_is_explicit")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def complete_two_slots(self):
        f = self.f
        f.protocol["observation_mode"] = "user_confirmed_execution"
        f.calculation.return_value = {"status": "qualified", "kind": "explicit_statistical_boundary_fixture"}
        f.register(); f.clock = "2030-01-01T10:00:00Z"
        f.commit(f.prepare("trial_decision", f.decision(buy=False), "d1"), "d1")
        f.market("m1", "2030-01-02", "2030-01-02T10:00:00Z"); f.clock = "2030-01-02T11:00:00Z"
        f.commit(f.prepare("trial_outcome", f.outcome(), "o1"), "o1")
        f.clock = "2030-01-02T11:30:00Z"
        f.commit(f.prepare("trial_decision", f.decision("decision2", "2030-01-03", False), "d2"), "d2")
        f.market("m2", "2030-01-03", "2030-01-03T10:00:00Z"); f.clock = "2030-01-03T11:00:00Z"
        f.commit(f.prepare("trial_outcome", f.outcome("2030-01-03", "m2"), "o2"), "o2")
        f.clock = "2030-01-06T00:00:00Z"

    def bound_decision(self, identity, market_id, date, orders):
        f = self.f; account_id = "trial:study:strategy"
        account, market = f.store.get("account", account_id), f.store.get("market", market_id)["market_ref"]
        risk = profile.resolve(f.store, account, f.clock, market["prices"], account_id, price_dates=market["price_dates"])
        import newtrade_guard
        inputs = newtrade_guard.read_inputs(f.store, f.artifacts, account_id, f.spec, f.clock, decision_id=identity)
        context = strategy.build_context(f.spec, account, f.clock, market, risk_state=risk,
            purchase_eligible_codes=list(market["prices"]), account_id=account_id, dynamic_universe=list(market["prices"]),
            candidate_identities=market["identities"], trade_state=inputs["trade_state"],
            trade_family_review_index=inputs["trade_family_review_index"])
        for order in orders:
            order["context_hash"] = context["context_hash"]
        bundle = {"context": context, "orders": {"orders": orders, "status": "ready" if orders else "no_action"},
            "account_hash": context["account_hash"], "spec_hash": context["spec_hash"], "source_hashes": trial.sources(),
            "market_id": market_id, "verification": {"status": "passed"}}
        f.publish_review(identity, bundle, inputs)
        return {"trial_id": "study", "date": date, "decision_id": identity, "cashflows": {"open": "0", "close": "0"},
                "distributions": [], "evidence_refs": f.evidence}

    def test_exit_from_dynamic_pool_does_not_lose_original_fill_price_evidence(self):
        f = self.f
        f.protocol["schedule"].append({"date": "2030-01-04", "decision_deadline": "2030-01-03T12:00:00Z",
            "execution_at": "2030-01-04T07:00:00Z", "valuation_at": "2030-01-04T07:00:00Z",
            "outcome_not_before": "2030-01-04T10:00:00Z", "outcome_deadline": "2030-01-05T12:00:00Z"})
        f.protocol["calibration_config"] = {"sample_size": 3, "dates": [row["date"] for row in f.protocol["schedule"]]}
        f.register(); f.clock = "2030-01-01T10:00:00Z"
        f.commit(f.prepare("trial_decision", f.decision(), "d1"), "d1")
        f.market("m1", "2030-01-02", "2030-01-02T10:00:00Z"); f.clock = "2030-01-02T11:00:00Z"
        f.commit(f.prepare("trial_outcome", f.outcome(), "o1"), "o1")
        f.clock = "2030-01-02T11:30:00Z"
        lot = next(row for row in f.store.get("account", "trial:study:strategy")["lots"].values() if row["code"] == "000001")
        sell = {"order_id": "exit-A", "code": "000001", "side": "sell", "currency": "CNY", "lot_id": lot["lot_id"],
            "cash_limit": "0", "share_limit": lot["shares"], "fee_rate": "0", "settlement_days": 2,
            "share_step": "0.00000001", "fee_contract": synthetic_terms("000001", step="0.00000001")}
        f.commit(f.prepare("trial_decision", self.bound_decision("exit", "m1", "2030-01-03", [sell]), "d2"), "d2")
        f.market("m2", "2030-01-03", "2030-01-03T10:00:00Z"); f.clock = "2030-01-03T11:00:00Z"
        f.commit(f.prepare("trial_outcome", f.outcome("2030-01-03", "m2"), "o2"), "o2")
        f.market("m3-full", "2030-01-04", "2030-01-04T10:00:00Z")
        for original, name in (("m2", "m2-only-B"), ("m3-full", "m3-only-B")):
            record = copy.deepcopy(f.store.get("market", original)); ref = record["market_ref"]
            for key in ("prices", "price_dates", "currencies", "identities", "known_marks"):
                ref[key] = {"000002": ref[key]["000002"]}
            ref["terms"] = [row for row in ref["terms"] if row["code"] == "000002"]
            nav = f.artifacts.read_json(record["provenance"]["nav_ref"])
            record["provenance"]["nav_ref"] = f.artifacts.put_json({"000002": nav["000002"]})
            record["provenance"]["coverage_by_code"] = {"000002": record["provenance"]["coverage_by_code"]["000002"]}
            f.store.put("market", name, record)
        f.clock = "2030-01-03T11:30:00Z"
        f.commit(f.prepare("trial_decision", self.bound_decision("B-only", "m2-only-B", "2030-01-04", []), "d3"), "d3")
        f.clock = "2030-01-04T11:00:00Z"
        f.commit(f.prepare("trial_outcome", f.outcome("2030-01-04", "m3-only-B"), "o3"), "o3")
        f.clock = "2030-01-06T00:00:00Z"
        result = f.prepare("trial_evaluate", {"trial_id": "study"}, "evaluate")["result"]
        self.assertEqual(result["reason"], "design_calibration_not_qualified")
        self.assertEqual(f.store.get("trial_slot", "study:2030-01-04")["decision"]["universe"], ["000002"])
        self.assertTrue(all(row["paired_return"] == 0 for _, row in f.store.scan("trial_observation")))

    def test_user_flow_valuation_disagreement_cannot_enter_formal_observation(self):
        f = self.f; f.protocol["observation_mode"] = "user_confirmed_execution"
        f.register(); f.clock = "2030-01-01T10:00:00Z"
        request = f.decision(buy=False); request["cashflows"]["close"] = "100"
        f.commit(f.prepare("trial_decision", request, "d"), "d")
        f.market("m1", "2030-01-02", "2030-01-02T10:00:00Z", b="1.1")
        fact = f.source_fact("wrong-flow-mark", "cashflow", {"amount": "100", "valuation": {"at": "2030-01-02T07:00:00Z",
            "prices": {"000002": "2"}, "price_dates": {"000002": "2030-01-02"}, "evidence_refs": [f.source]}})
        before = f.store.get("account", "main"); f.clock = "2030-01-02T11:00:00Z"
        with self.assertRaisesRegex(ValueError, "independently audited economic-date NAV"):
            f.prepare("trial_outcome", f.outcome(), "invalid-performance")
        self.assertIsNone(f.store.get("trial_observation", "study:2030-01-02"))
        self.assertEqual(f.store.get("ledger_event", fact["id"]), fact)
        self.assertEqual(f.store.get("account", "main"), before)

    def test_unknown_only_first_cash_confirmation_can_register_a_trial(self):
        import ledger
        from state_store import Store
        from artifacts import Artifacts
        f = self.f
        source = f.artifacts.read(f.source)
        plan = f.store.get("plan_constraints", "main")
        f.pause_administration(); f.store = Store(f.temp.name, "first-start"); f.resume_administration()
        f.artifacts = Artifacts(f.store.base)
        f.source = f.artifacts.put_bytes(source); f.evidence[0]["artifact"] = f.source
        f.artifacts.put_bytes(b"synthetic-ca-only")
        at = "2029-12-31T08:00:00Z"
        unknown = {"id": "before-start", "type": "unknown", "sequence": 1, "effective_at": at, "known_at": at,
                   "recorded_at": at, "data": {"reason": "No actual cash confirmed yet"}}
        account = ledger.apply_event(ledger.initial_state("CNY"), unknown)
        f.store.put("account", "main", account, False); f.store.put("ledger_event", unknown["id"], unknown)
        f.store.put("ledger_event_owner", unknown["id"], {"account_id": "main"})
        with patch.object(profile, "utc_now", return_value="2029-12-31T09:00:00Z"):
            profile.initialize({"principal": "500", "currency": "CNY", "loss_tolerance": "0.25", "as_of": at,
                "confirmed_initial_all_cash": True, "confirmed_no_prior_economic_activity": True,
                "account_hash": fingerprint(account), "user_source": {"message": "First cash 500; no prior financial activity", "confirmed_at": at}}, f.store, "first")
        f.initial = f.store.get("account", "main")
        f.store.put("plan_constraints", "main", plan, False)
        f.market("initial", "2029-12-31", at)
        f.protocol["initial"]["account_hash"] = fingerprint(f.initial)
        f.protocol["risk_state"] = profile.resolve(f.store, f.initial, "2029-12-31T12:00:00Z", {"000001": "1", "000002": "1"})
        self.assertEqual(f.register()["status"], "registered")
        self.assertEqual(f.store.get("account", "trial:study:strategy")["units"], "500")

    def test_fixed_timestamp_query_survives_artifact_month_partition_change(self):
        from contracts import RetryableError
        from trial_evidence import EvidenceTransportError
        f = self.f
        def lost_response(data, folder, trust, not_after=None, **kwargs):
            import hashlib
            folder.mkdir(parents=True, exist_ok=True)
            query = kwargs.get("fixed_query") or hashlib.sha256(data).digest()
            (folder / "request.tsq").write_bytes(query)
            kwargs["on_query"](query)
            raise EvidenceTransportError("Synthetic lost response after fixed query binding")
        f.anchor.side_effect = lost_response
        with self.assertRaises(RetryableError):
            f.prepare("trial_register", {"protocol": f.protocol}, "month-boundary")
        saved_query = f.store.get("external_operation", "month-boundary")["query_ref"]
        query = f.artifacts.read(saved_query)
        original_put = f.artifacts.put_bytes
        def next_partition(value):
            reference = original_put(value)
            if value == query:
                reference = {**reference, "path": "objects/next-month/" + reference["sha256"]}
                target = f.artifacts.base / reference["path"]
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(value)
            return reference
        f.anchor.side_effect = f.fake_anchor
        with patch.object(f.artifacts, "put_bytes", side_effect=next_partition):
            package = f.prepare("trial_register", {"protocol": f.protocol}, "month-boundary")
            f.commit(package, "month-boundary")
        self.assertEqual(f.store.get("external_operation", "month-boundary")["query_ref"], saved_query)
        self.assertEqual(f.anchor.call_count, 2)

    def test_valid_response_before_state_commit_recovers_after_cutoff_without_redelivery(self):
        f = self.f
        def interrupted(data, folder, trust, not_after=None, **kwargs):
            f.fake_anchor(data, folder, trust, not_after, **kwargs)
            raise OSError("Synthetic process interruption after verified response publication")
        f.anchor.side_effect = interrupted
        with self.assertRaises(OSError):
            f.prepare("trial_register", {"protocol": f.protocol}, "response-before-state")
        self.assertEqual(f.store.get("external_operation", "response-before-state")["phase"], "request_ready")
        f.clock = "2030-01-01T01:00:00Z"
        f.anchor.side_effect = AssertionError("A valid earlier response must be recovered without a new TSA request")
        result = f.commit(f.prepare("trial_register", {"protocol": f.protocol}, "response-before-state"), "response-before-state")
        self.assertEqual(result["status"], "registered")
        self.assertEqual(f.anchor.call_count, 1)
        self.assertIn("recovered_from", f.store.get("external_operation", "response-before-state"))

    def test_premature_accuracy_bound_does_not_poison_later_outcome_timestamp(self):
        f = self.f; f.register(); f.clock = "2030-01-01T10:00:00Z"
        f.commit(f.prepare("trial_decision", f.decision(buy=False), "d"), "d")
        f.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        f.clock = "2030-01-02T10:00:00Z"
        before = f.anchor.call_count
        with self.assertRaisesRegex(ValueError, "precede maturity"):
            f.prepare("trial_outcome", f.outcome(), "maturity")
        self.assertEqual(f.store.get("external_operation", "maturity")["phase"], "request_ready")
        f.clock = "2030-01-02T10:00:02Z"
        result = f.commit(f.prepare("trial_outcome", f.outcome(), "maturity"), "maturity")
        self.assertEqual(result["status"], "observation_recorded")
        self.assertEqual(f.anchor.call_count, before + 2)

    def test_pending_sale_also_blocks_a_new_quantitative_decision(self):
        f = self.f; f.register(); f.clock = "2030-01-01T10:00:00Z"
        order = {"order_id": "pending-sale", "code": "000002", "side": "sell",
            "currency": "CNY", "lot_id": "b", "cash_limit": "0", "share_limit": "50", "fee_rate": "0",
            "settlement_days": 2, "share_step": "0.00000001", "context_hash": "assigned_by_bound_decision",
            "fee_contract": synthetic_terms("000002", step="0.00000001")}
        request = self.bound_decision("sale", "initial", "2030-01-02", [order])
        f.commit(f.prepare("trial_decision", request, "sale-slot"), "sale-slot")
        f.market("m1", "2030-01-02", "2030-01-02T10:00:00Z"); f.clock = "2030-01-02T11:00:00Z"
        with self.assertRaisesRegex(ValueError, "confirmed execution exposure"):
            f.prepare("trial_decision", f.decision("next", "2030-01-03", False), "next-slot")
        account = f.store.get("account", "trial:study:strategy")
        self.assertEqual(account["lots"]["b"]["shares"], "100")
        self.assertEqual(account["orders"]["pending-sale"]["remaining_shares"], "50")

    def test_late_actual_fact_after_evaluation_preparation_cannot_publish_supported_result(self):
        import allocation_statistics
        import pipeline
        self.complete_two_slots(); f = self.f
        with patch.object(allocation_statistics, "evaluate_advantage", return_value={"status": "supported", "interval": [.01, .02]}):
            package = f.prepare("trial_evaluate", {"trial_id": "study"}, "evaluate")
        self.assertTrue(package["result"]["return_advantage_supported"])
        f.pause_administration()
        try:
            f.store.begin("late-source", {"fixture": "late actual economic fact"})
            with f.store.lease("late-source"), patch.object(pipeline, "utc_now", return_value=f.clock):
                result = pipeline.feedback({"currency": "CNY", "events": [{"id": "late-source-fact", "type": "unknown",
                    "effective_at": "2030-01-02T07:00:00Z", "known_at": f.clock, "recorded_at": f.clock,
                    "data": {"reason": "Late broker adjustment inside registered interval"}}],
                    "evidence": {"late-source-fact": f.evidence}}, f.store)
                f.store.complete("late-source", result)
        finally:
            f.resume_administration()
        with self.assertRaises(StaleSnapshot):
            f.commit(package, "evaluate")
        self.assertFalse(list(f.store.scan("trial_evaluation")))

    def test_registered_policy_accepts_new_dynamic_code_without_changing_policy_hash(self):
        f = self.f; f.register(); f.clock = "2030-01-01T10:00:00Z"
        request = f.decision(buy=False)
        market = copy.deepcopy(f.store.get("market", "initial"))
        ref = market["market_ref"]
        ref["prices"]["000003"] = "1"; ref["price_dates"]["000003"] = "2029-12-31"; ref["currencies"]["000003"] = "CNY"
        ref["identities"]["000003"] = {"fund_group_id": "new-fund", "sector_exposures": None}
        terms = copy.deepcopy(ref["terms"][0]); contract = synthetic_terms("000003", step="0.00000001")
        terms.update(code="000003", fee_contract=contract, fee_contract_hash=fingerprint(contract), fee_contract_ref=f.artifacts.put_json(contract))
        ref["terms"].append(terms)
        f.bind_market_marks(ref)
        f.store.put("market", "expanded", market)
        account = f.store.get("account", "trial:study:strategy")
        risk = profile.resolve(f.store, account, f.clock, ref["prices"], "trial:study:strategy", price_dates=ref["price_dates"])
        import newtrade_guard
        inputs = newtrade_guard.read_inputs(f.store, f.artifacts, "trial:study:strategy", f.spec, f.clock, decision_id="expanded")
        context = strategy.build_context(f.spec, account, f.clock, ref, risk_state=risk, purchase_eligible_codes=["000003"],
                                         account_id="trial:study:strategy", dynamic_universe=list(ref["prices"]), candidate_identities=ref["identities"],
                                         trade_state=inputs["trade_state"], trade_family_review_index=inputs["trade_family_review_index"])
        bundle = {"context": context, "orders": {"orders": [], "status": "no_action"}, "account_hash": context["account_hash"],
                  "spec_hash": context["spec_hash"], "source_hashes": trial.sources(), "market_id": "expanded", "verification": {"status": "passed"}}
        f.publish_review("expanded", bundle, inputs); request["decision_id"] = "expanded"
        f.commit(f.prepare("trial_decision", request, "expanded-slot"), "expanded-slot")
        frozen = f.store.get("trial_slot", "study:2030-01-02")["decision"]
        self.assertEqual(frozen["universe"], ["000001", "000002", "000003"])
        self.assertEqual(context["spec_hash"], fingerprint(f.protocol["strategy"]["spec"]))

    def test_benchmark_flow_valuation_uses_its_own_holdings(self):
        f = self.f; f.protocol["observation_mode"] = "user_confirmed_execution"
        f.register(); f.clock = "2030-01-01T10:00:00Z"
        request = f.decision(); request["cashflows"]["close"] = "100"
        f.commit(f.prepare("trial_decision", request, "d"), "d")
        f.market("m1", "2030-01-02", "2030-01-02T10:00:00Z", a="2")
        f.source_fact("purchase", "buy_fill", {"order_id": "buy-A", "fill_id": "actual", "lot_id": "A", "shares": "50", "price": "2", "fee": "0", "final": True})
        f.source_fact("deposit", "cashflow", {"amount": "100", "valuation": {"at": "2030-01-02T07:00:00Z",
            "prices": {"000001": "2", "000002": "1"}, "price_dates": {"000001": "2030-01-02", "000002": "2030-01-02"},
            "evidence_refs": [f.source]}}, sequence=3)
        f.clock = "2030-01-02T11:00:00Z"
        result = f.commit(f.prepare("trial_outcome", f.outcome(), "outcome"), "outcome")
        self.assertEqual(result["status"], "observation_recorded")
        flow_prices = {role: [row["event"]["data"]["valuation"]["prices"] for key, row in f.store.scan("trial_ledger_event")
                             if key.startswith("study:" + role + ":") and row["event"]["type"] == "cashflow"][0]
                       for role in ("strategy", "benchmark")}
        self.assertEqual(set(flow_prices["strategy"]), {"000001", "000002"})
        self.assertEqual(set(flow_prices["benchmark"]), {"000002"})
        self.assertEqual(f.store.get("trial_observation", "study:2030-01-02")["paired_return"], 0)


if __name__ == "__main__":
    unittest.main()
