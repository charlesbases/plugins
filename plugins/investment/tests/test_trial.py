"""Store/ledger integration. Synthetic market/TSA boundaries are not efficacy evidence."""
import copy
import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_numerical_schema4 import terms as legacy_synthetic_terms
import trial
import ledger
import strategy
import allocation_market
import research_data
import newtrade_guard
import risk_profile as profile
from artifacts import Artifacts
from contracts import fingerprint, instant, canonical_bytes, StaleSnapshot
from state_store import Store


def synthetic_terms(*args, **kwargs):
    """Explicit same-NAV-day toy registration; no real dealing evidence."""
    result = legacy_synthetic_terms(*args, **kwargs)
    result.pop("confirmation_max_calendar_days")
    result.update(order_cutoff_local="15:00:00",
                  confirmation={"lag_days": 0, "day_basis": "trading_days", "normal_conditions_only": True})
    result["execution_calendar"]["coverage_start"] = "2029-12-30"
    result["execution_calendar"]["open_dates"] = ["2029-12-30", "2029-12-31"] + result["execution_calendar"]["open_dates"]
    return result


class TrialServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="investment-trial-contract-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name), "p")
        self.administration = None
        self.resume_administration()
        self.addCleanup(self.pause_administration)
        self.artifacts = Artifacts(self.store.base)
        self.clock = "2029-12-31T13:00:00Z"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(trial, "utc_now", side_effect=lambda: self.clock))
        self.anchor = self.stack.enter_context(patch.object(trial, "anchor", side_effect=self.fake_anchor))
        self.stack.enter_context(patch.object(trial, "verify_receipt", side_effect=self.fake_verify))
        import verify
        self.stack.enter_context(patch.object(verify, "verify_bundle", return_value={"status": "passed", "scope": "synthetic_trial_boundary"}))
        self.calculation = self.stack.enter_context(patch.object(trial, "_calculation", return_value={"status": "insufficient_evidence", "kind": "unit_test_boundary"}))
        self.stack.enter_context(patch.object(trial, "_trust_snapshot", return_value={"config": {}, "ca_ref": self.artifacts.put_bytes(b"synthetic-ca-only")}))
        self.source = self.artifacts.put_bytes(b"Synthetic NAV and terms fixture; no external investment evidence")
        self.evidence = [{"source_id": "nav", "source_url": "https://example.invalid/source", "role": "public_source", "retrieved_at": "2029-12-30T00:00:00Z", "artifact": self.source}]
        self.spec = json.loads((SCRIPTS.parent / "references/strategy.example.json").read_text())
        self.spec["allocation"].update(tail_probability=.1)
        self.spec["planning"].update(primary_horizon_days=1)
        self.spec["trade_policy"]["registration"] = "declared"
        self.spec["trade_policy"]["family"].update(start_at="2029-12-01T00:00:00Z", end_at="2030-02-01T00:00:00Z")
        self.spec["benchmark"] = {"rule": "fixed_weights", "weights": {"000002": "0.090909090909"}, "cash_weight": "0.909090909091", "rebalance_dates": []}
        self.spec["availability"]["max_market_age_seconds"] = 400000
        initial = ledger.initial_state("CNY")
        event = {"id": "initial", "type": "opening", "sequence": 1, "effective_at": "2029-12-31T07:00:00Z", "known_at": "2029-12-31T08:00:00Z", "recorded_at": "2029-12-31T08:00:00Z", "data": {"cash": "1000", "lots": [{"lot_id": "b", "code": "000002", "shares": "100", "acquired_at": "2029-12-30T00:00:00Z"}], "prices": {"000001": "1", "000002": "1"}}}
        event["data"]["price_dates"] = dict.fromkeys(event["data"]["prices"], "2029-12-31")
        event["data"]["lots"][0].update(ownership_at="2029-12-30T00:00:00Z", price_date="2029-12-30")
        self.initial = ledger.apply_event(initial, event)
        self.store.put("account", "main", self.initial, immutable=False)
        self.store.put("ledger_event", event["id"], event)
        self.store.put("ledger_event_owner", event["id"], {"account_id": "main"})
        self.market("initial", "2029-12-31", "2029-12-31T08:00:00Z")
        with patch.object(profile, "utc_now", return_value="2029-12-31T09:00:00Z"):
            profile.initialize({"principal": "1100", "currency": "CNY", "loss_tolerance": "0.25",
                "as_of": "2029-12-31T08:00:00Z", "user_source": {"message": "Synthetic test principal and tolerance", "confirmed_at": "2029-12-31T09:00:00Z"},
                "confirmed_initial_all_cash": False, "account_hash": fingerprint(self.initial)}, self.store, "initial-profile")
            profile.update_constraints({"constraints": {"platform": "TT", "currency": "CNY", "goal": "Synthetic accounting trial",
                "excluded_categories": ["money_market"],
                "position_limits": {"fund_group_limits": {}, "sector_limits": {}}}, "current_constraints_hash": None,
                "user_source": {"message": "Explicit synthetic test plan", "confirmed_at": "2029-12-31T09:00:00Z"}}, self.store, "plan")
        risk = profile.resolve(self.store, self.initial, "2029-12-31T12:00:00Z", {"000001": "1", "000002": "1"})
        schedule = [{"date": "2030-01-0" + str(day), "decision_deadline": "2030-01-0" + str(day-1) + "T12:00:00Z", "execution_at": "2030-01-0" + str(day) + "T07:00:00Z", "valuation_at": "2030-01-0" + str(day) + "T07:00:00Z", "outcome_not_before": "2030-01-0" + str(day) + "T10:00:00Z", "outcome_deadline": "2030-01-05T12:00:00Z"} for day in (2, 3)]
        self.protocol = {"schema_version": 4, "trial_id": "study", "name": "Synthetic contract test", "start_at": "2030-01-01T00:00:00Z", "end_at": "2030-01-06T00:00:00Z", "timezone": "Asia/Shanghai", "schedule": schedule, "initial": {"account_id": "main", "account_hash": fingerprint(self.initial), "as_of": "2029-12-31T12:00:00Z", "market_id": "initial", "date": "2029-12-31"}, "strategy": {"actor_identity": "operator", "spec": self.spec}, "benchmark": {"description": "Fixed opening allocation", "rationale": "Predeclared weights and shared flows/costs"}, "horizon_days": 1, "tail_probability": .1, "risk_state": risk, "validation_policy": copy.deepcopy(self.spec["trade_policy"]["inference"]), "calibration_config": {"sample_size": 2, "dates": [s["date"] for s in schedule]}, "trust": {}, "evidence_refs": self.evidence, "observation_mode": "documented_rule_simulation", "assumption_scope": "Synthetic fixture; never an investment recommendation"}

    def pause_administration(self):
        if self.administration is not None:
            self.administration.__exit__(None, None, None)
            self.administration = None

    def resume_administration(self):
        self.administration = self.store.maintenance()
        self.administration.__enter__()

    def fake_anchor(self, data, folder, trust, not_after=None, **kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        query = kwargs.get("fixed_query") or hashlib.sha256(data).digest()
        if kwargs.get("on_query"):
            kwargs["on_query"](query)
        (folder / "request.tsq").write_bytes(query)
        receipt = {"gen_time": self.clock, "upper_time_bound": (instant(self.clock)+dt.timedelta(seconds=1)).isoformat(), "accuracy_seconds": 1, "revocation_checked": False}
        (folder / "response.tsr").write_bytes(canonical_bytes(receipt))
        return self.fake_verify(data, folder, trust, not_after)

    def fake_verify(self, data, folder, trust, not_after=None):
        self.assertEqual((folder / "request.tsq").read_bytes(), hashlib.sha256(data).digest())
        result = json.loads((folder / "response.tsr").read_text())
        if not_after is not None and instant(result["upper_time_bound"]) > instant(not_after):
            raise ValueError("synthetic boundary: late timestamp")
        return result

    def bind_market_marks(self, reference, nav_rows=None):
        """Real version/known-mark producers over explicit synthetic CAS source bytes.

        Observation clocks are this fixture's captures; no historical PIT publication
        or external market effectiveness is asserted.
        """
        marks = {}
        for code, price in reference["prices"].items():
            originals = (nav_rows or {}).get(code) or [{"date": reference["price_dates"][code], "nav": float(price)}]
            normalized = [{**row, "code": code, "cumulative_nav": row.get("cumulative_nav", row["nav"]),
                           "distribution_per_share": row.get("distribution_per_share", 0.)} for row in originals]
            raw = self.artifacts.put_bytes(canonical_bytes(normalized))
            metadata = {"source_id": "synthetic-trial-NAV-"+code, "sha256": raw["sha256"],
                        "retrieved_at": reference["observed_at"]}
            bound = research_data.bind_source_versions(normalized, metadata)
            row = next(row for row in bound if row["date"] == reference["price_dates"][code])
            marks[code] = allocation_market.known_mark(row, reference["observed_at"])
        reference["known_marks"] = marks
        return reference

    def market(self, identity, date, observed, a="1", b="1", corporate_actions=None, nav_rows=None):
        ref = json.loads((SCRIPTS.parent / "references/market.example.json").read_text())
        ref.pop("price_date", None)
        ref.update(schema_version=4, terms_basis="source_verified", observed_at=observed, prices={"000001": a, "000002": b},
                   price_dates={"000001": date, "000002": date}, currencies={"000001": "CNY", "000002": "CNY"},
                   identities={code: {"fund_group_id": code, "sector_exposures": None} for code in ("000001", "000002")},
                   source_hashes={"nav": self.source["sha256"]})
        for row in ref["terms"]:
            row["observed_at"] = "2029-12-30T00:00:00Z"
            contract = synthetic_terms(row["code"], step="0.00000001")
            row.update(fee_contract=contract, fee_contract_hash=fingerprint(contract), fee_contract_ref=self.artifacts.put_json(contract))
        record = {"market_ref": ref, "evidence_refs": self.evidence, "provenance": {"price_validation": "verified_source_NAV", "price_date": date, "distributions_by_code": {"000001": "0", "000002": "0"}, "corporate_actions": corporate_actions or [], "coverage_start": "2029-12-31", "coverage_end": date, "terms_basis": "conditional"}}
        record["provenance"]["coverage_by_code"] = {code: {"start": "2029-12-31", "end": date} for code in ref["prices"]}
        record["provenance"]["corporate_actions"] = [{**row, "ex_date": row["date"], "record_date": "2029-12-31",
            "rights_rule": "confirmed_ownership_at_record_date", "dividend_mode": "cash", "currency": "CNY", "distribution_evidence": {"fixture": True}}
            for row in (corporate_actions or [])]
        if nav_rows is None:
            prices = {r["provenance"]["price_date"]: r["market_ref"]["prices"] for _, r in self.store.scan("market")}
            prices[date] = ref["prices"]
            nav_rows = {code: [{"date": day, "nav": float(values[code]), "distribution_per_share": 0}
                               for day, values in sorted(prices.items())] for code in ref["prices"]}
        record["provenance"]["nav_ref"] = self.artifacts.put_json(nav_rows)
        self.bind_market_marks(ref, nav_rows)
        record["provenance"]["action_inventory"] = {"scope": "synthetic_known_subset_only", "by_code": {
            code: {"known_at": observed, "source_id": "nav", "source_sha256": self.source["sha256"], "dividends": []}
            for code in ref["prices"]}}
        self.store.put("market", identity, record)
        return record

    def prepare(self, operation, payload, identity):
        self.pause_administration()
        try:
            self.store.begin(identity, {"operation": operation, "payload": payload})
            with self.store.lease(identity) as completed:
                if completed is not None:
                    return {"result": completed, "writes": [], "expected": []}
                return trial.execute(operation, payload, self.store, self.artifacts, identity)
        finally:
            self.resume_administration()

    def commit(self, package, identity):
        self.pause_administration()
        try:
            with self.store.lease(identity) as completed:
                return completed if completed is not None else self.store.complete(identity, package["result"], package["writes"], package["expected"])
        finally:
            self.resume_administration()

    def register(self):
        return self.commit(self.prepare("trial_register", {"protocol": self.protocol}, "registration"), "registration")

    def decision(self, identity="d1", date="2030-01-02", buy=True):
        account_id = "trial:study:strategy"
        state = self.store.get("account", account_id)
        market_id = "initial" if date == "2030-01-02" else "m1"
        risk = profile.resolve(self.store, state, self.clock, self.store.get("market", market_id)["market_ref"]["prices"], account_id)
        trade_inputs = newtrade_guard.reserve_review(self.store, self.artifacts, account_id, self.spec, self.clock, decision_id=identity)
        context = strategy.build_context(self.spec, state, self.clock, self.store.get("market", market_id)["market_ref"],
                                         risk_state=risk, purchase_eligible_codes=["000001", "000002"], account_id=account_id,
                                         trade_state=trade_inputs["trade_state"], trade_family_review_index=trade_inputs["trade_family_review_index"])
        orders = [] if not buy else [{"order_id": "buy-A", "code": "000001", "side": "buy", "currency": "CNY", "lot_id": None, "cash_limit": "100", "share_limit": "0", "fee_rate": "0", "settlement_days": 0, "share_step": "0.00000001", "context_hash": context["context_hash"]}]
        for order in orders:
            order["fee_contract"] = synthetic_terms(order["code"], step="0.00000001")
        bundle = {"context": context, "orders": {"orders": orders, "status": "ready" if orders else "no_action"}, "account_hash": context["account_hash"], "spec_hash": context["spec_hash"], "source_hashes": trial.sources(), "market_id": market_id, "verification": {"status": "passed"}}
        self.publish_review(identity, bundle, trade_inputs)
        return {"trial_id": "study", "date": date, "decision_id": identity, "cashflows": {"open": "0", "close": "0"}, "distributions": [], "evidence_refs": self.evidence}

    def publish_review(self, identity, bundle, trade_inputs):
        self.store.put("decision", identity, {**bundle, "trade_review_inputs": trade_inputs,
            "trade_review_spec": self.spec})

    def outcome(self, date="2030-01-02", market_id="m1", events=None):
        return {"trial_id": "study", "date": date, "market_id": market_id, "strategy_event_ids": events or [], "evidence_refs": self.evidence}

    def test_registration_package_atomic_failure_and_receipt_reuse(self):
        package = self.prepare("trial_register", {"protocol": self.protocol}, "registration")
        self.assertIsNone(self.store.get("trial", "study"))
        real_write = self.store._write_prepared
        def interrupted(item, digest, payload, conn):
            if item["kind"] == "account" and item["key"] == "trial:study:strategy":
                raise OSError("injected transactional interruption")
            return real_write(item, digest, payload, conn)
        with patch.object(self.store, "_write_prepared", side_effect=interrupted), self.assertRaises(OSError):
            self.commit(package, "registration")
        self.assertIsNone(self.store.get("trial_state", "study"))
        recovered = self.prepare("trial_register", {"protocol": self.protocol}, "registration")
        self.commit(recovered, "registration")
        self.assertEqual(self.anchor.call_count, 1)
        self.assertEqual(self.calculation.call_count, 1)
        self.assertEqual(self.store.get("account", "main"), self.initial)

    def test_changed_payload_or_snapshot_does_not_rebind_signed_operation(self):
        package = self.prepare("trial_register", {"protocol": self.protocol}, "registration")
        changed = copy.deepcopy(self.initial); changed["cash"] = "999"
        self.store.put("account", "main", changed, immutable=False)
        with self.assertRaises(StaleSnapshot):
            self.commit(package, "registration")
        self.assertIsNone(self.store.get("trial", "study"))
        changed_protocol = copy.deepcopy(self.protocol); changed_protocol["name"] = "different"
        with self.assertRaises(ValueError):
            self.prepare("trial_register", {"protocol": changed_protocol}, "registration")

    def test_current_account_and_context_are_bound_before_timestamp(self):
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        request = self.decision()
        state = self.store.get("account", "trial:study:strategy")
        changed = copy.deepcopy(state); changed["cash"] = "900"
        self.store.put("account", "trial:study:strategy", changed, immutable=False)
        calls = self.anchor.call_count
        with self.assertRaisesRegex(ValueError, "current trial account"):
            self.prepare("trial_decision", request, "decision")
        self.assertEqual(self.anchor.call_count, calls)

    def test_simulation_uses_shared_ledger_and_independent_two_period_values(self):
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(), "decision1"), "decision1")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        self.clock = "2030-01-02T11:00:00Z"
        first = self.commit(self.prepare("trial_outcome", self.outcome(), "outcome1"), "outcome1")
        self.assertEqual(first["status"], "observation_recorded")
        self.clock = "2030-01-02T11:30:00Z"
        self.commit(self.prepare("trial_decision", self.decision("d2", "2030-01-03", False), "decision2"), "decision2")
        self.market("m2", "2030-01-03", "2030-01-03T10:00:00Z", a="1.1")
        self.clock = "2030-01-03T11:00:00Z"
        self.commit(self.prepare("trial_outcome", self.outcome("2030-01-03", "m2"), "outcome2"), "outcome2")
        row = self.store.get("trial_observation", "study:2030-01-03")
        self.assertAlmostEqual(row["paired_return"], 10/1100, places=13)
        self.assertEqual(row["benchmark_unit_nav"], "1")
        self.assertEqual((row["principal_risk"]["net_principal"], row["principal_risk"]["equity"],
                          row["principal_risk"]["principal_floor"]), ("1100", "1110", "825"))
        self.clock = "2030-01-06T00:00:00Z"
        report = self.prepare("trial_evaluate", {"trial_id": "study"}, "evaluate")
        self.assertEqual(report["result"]["status"], "insufficient_evidence")
        self.assertEqual(report["result"]["reason"], "design_calibration_not_qualified")
        self.assertEqual(report["result"]["risk"]["status"], "observed_compliant")
        with patch.object(profile, "utc_now", return_value=self.clock):
            profile.update({"mode": "base", "loss_tolerance": "0.4", "start_at": None, "end_at": None, "end_active_temporary": True,
                            "current_profile_hash": profile.status(self.store)["profile_hash"],
                            "user_source": {"message": "New risk after completed interval", "confirmed_at": self.clock}},
                           self.store, "post-trial-risk")
        later = self.prepare("trial_evaluate", {"trial_id": "study"}, "evaluate-after-update")
        self.assertEqual(later["result"], report["result"])

    def test_partial_facts_commit_but_block_next_quantitative_decision(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(), "decision1"), "decision1")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        fact = {"id": "actual-partial", "type": "buy_fill", "sequence": 2, "effective_at": "2030-01-02T07:00:00Z", "known_at": "2030-01-02T10:01:00Z", "recorded_at": "2030-01-02T10:01:00Z", "data": {"order_id": "buy-A", "fill_id": "partial", "lot_id": "A-lot", "shares": "50", "price": "1", "fee": "0", "final": False}}
        self.store.put("ledger_event", fact["id"], self.v4_fact(fact))
        self.store.put("ledger_event_owner", fact["id"], {"account_id": "main"})
        self.store.put("ledger_event_evidence", fact["id"], self.evidence)
        self.clock = "2030-01-02T11:00:00Z"
        result = self.commit(self.prepare("trial_outcome", self.outcome(events=[fact["id"]]), "partial"), "partial")
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertTrue(result["operation_complete"])
        self.assertIsNone(self.store.get("trial_observation", "study:2030-01-02"))
        state = self.store.get("account", "trial:study:strategy")
        snap = ledger.snapshot(state, self.clock)
        self.assertFalse(snap["blocked"])
        self.assertEqual(snap["reserved_cash"], "50")
        self.clock = "2030-01-02T11:30:00Z"
        with self.assertRaisesRegex(ValueError, "confirmed execution exposure"):
            self.prepare("trial_decision", self.decision("d2", "2030-01-03", False), "decision2")
        self.assertIsNone(self.store.get("trial_slot", "study:2030-01-03"))
        self.assertEqual(self.store.get("account", "trial:study:strategy")["cash"], "950")

    def test_actual_fee_deviation_is_recorded_and_reconciled_without_losing_fill(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(), "decision1"), "decision1")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        fact = {"id": "wrong-fee", "type": "buy_fill", "sequence": 2, "effective_at": "2030-01-02T07:00:00Z", "known_at": "2030-01-02T10:01:00Z", "recorded_at": "2030-01-02T10:01:00Z", "data": {"order_id": "buy-A", "fill_id": "wrong", "lot_id": "A-lot", "shares": "50", "price": "1", "fee": "1", "final": True}}
        self.store.put("ledger_event", fact["id"], self.v4_fact(fact)); self.store.put("ledger_event_owner", fact["id"], {"account_id": "main"}); self.store.put("ledger_event_evidence", fact["id"], self.evidence)
        before, calls = self.store.get("trial_state", "study"), self.anchor.call_count
        self.clock = "2030-01-02T11:00:00Z"
        result = self.commit(self.prepare("trial_outcome", self.outcome(events=[fact["id"]]), "actual-fee"), "actual-fee")
        self.assertEqual(result["status"], "observation_recorded")
        actual = self.store.get("account", "trial:study:strategy")
        self.assertEqual(actual["cash"], "949")
        self.assertEqual(actual["lots"]["A-lot"]["shares"], "50")
        self.assertIn("fee_deviation", [row["kind"] for row in actual["reconciliation"]])
        self.assertEqual(self.anchor.call_count, calls + 1)

    def v4_fact(self, fact):
        # Explicit synthetic fixture: same-day ownership, half-up cents, no
        # external evidence claim. Production receipts must supply these values.
        fact = copy.deepcopy(fact)
        data, kind = fact["data"], fact["type"]
        if kind in ("buy_fill", "sell_fill", "subscription_confirmed"):
            data["price_date"] = fact["effective_at"][:10]
            gross = Decimal(data["shares"]) * Decimal(data["price"])
            data["gross_amount"] = str(gross)
            if kind != "sell_fill":
                data.update(holding_started_at=fact["effective_at"], ownership_at=fact["effective_at"], cash_debit=str(gross + Decimal(data["fee"])))
            else:
                data["net_amount"] = str(gross - Decimal(data["fee"]))
        elif kind == "cashflow":
            day = fact["effective_at"][:10]
            matching = [row for _, row in self.store.scan("market") if row["provenance"]["price_date"] == day]
            prices = {"000002": matching[-1]["market_ref"]["prices"]["000002"]} if matching else {}
            data.update(transfer_id="fixture:" + fact["id"], revision_id="revision:" + fact["id"], previous_revision=None)
            if "valuation" not in data:
                data["valuation"] = {"at": fact["effective_at"], "prices": prices, "price_dates": dict.fromkeys(prices, day), "evidence_refs": [self.source]} if prices else None
        elif kind == "dividend_declared":
            data.update(record_at="2029-12-31T15:59:59Z", entitled_shares="100")
        elif kind in ("dividend_paid", "settlement"):
            data["amount"] = "10"
        return fact

    def source_fact(self, identity, kind, data, sequence=2, effective="2030-01-02T07:00:00Z", known="2030-01-02T10:01:00Z", owner="main"):
        fact = {"id": identity, "type": kind, "sequence": sequence, "effective_at": effective, "known_at": known, "recorded_at": known, "data": data}
        fact = self.v4_fact(fact)
        self.store.put("ledger_event", identity, fact)
        self.store.put("ledger_event_owner", identity, {"account_id": owner})
        self.store.put("ledger_event_evidence", identity, self.evidence)
        return fact

    def test_identity_includes_runner_and_rejects_changed_runtime(self):
        import verify
        self.register()
        frozen = trial.sources()
        self.assertIn("allocation_runner.py", frozen)
        self.assertIn("pipeline.py", frozen)
        self.assertEqual(frozen, verify.code_identity())
        altered = {**frozen, "allocation_runner.py": "0" * 64}
        with patch.object(verify, "code_identity", return_value=altered), self.assertRaisesRegex(ValueError, "implementation"):
            self.prepare("trial_evaluate", {"trial_id": "study"}, "different-runtime")

    def test_foreign_decision_implementation_rejected_before_timestamp(self):
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        request = self.decision()
        foreign = copy.deepcopy(self.store.get("decision", "d1"))
        foreign["source_hashes"]["allocation_runner.py"] = "0" * 64
        self.store.put("decision", "foreign", foreign)
        request["decision_id"] = "foreign"
        calls = self.anchor.call_count
        with self.assertRaisesRegex(ValueError, "implementation"):
            self.prepare("trial_decision", request, "foreign")
        self.assertEqual(self.anchor.call_count, calls)

    def test_fixed_weights_reject_independent_benchmark_orders(self):
        self.protocol["benchmark"]["orders_by_date"] = {"2030-01-02": [{"order_id": "arbitrary"}]}
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.register()
        self.assertEqual(self.anchor.call_count, 0)

    def test_cash_initial_registration_compiles_actual_passive_purchases(self):
        original_source = self.artifacts.read(self.source)
        self.pause_administration()
        self.store = Store(Path(self.temp.name), "cash-start")
        self.resume_administration()
        self.artifacts = Artifacts(self.store.base)
        self.source = self.artifacts.put_bytes(original_source)
        self.artifacts.put_bytes(b"synthetic-ca-only")
        self.evidence[0]["artifact"] = self.source
        opening = {
            "id": "cash-opening", "type": "opening", "sequence": 1,
            "effective_at": "2029-12-31T07:00:00Z", "known_at": "2029-12-31T08:00:00Z",
            "recorded_at": "2029-12-31T08:00:00Z", "data": {"cash": "10000", "lots": [], "prices": {}, "price_dates": {}}}
        self.initial = ledger.apply_event(ledger.initial_state("CNY"), opening)
        self.store.put("ledger_event", opening["id"], opening)
        self.store.put("ledger_event_owner", opening["id"], {"account_id": "main"})
        self.store.put("account", "main", self.initial, immutable=False)
        self.market("initial", "2029-12-31", "2029-12-31T08:00:00Z")
        with patch.object(profile, "utc_now", return_value="2029-12-31T09:00:00Z"):
            profile.initialize({"principal": "10000", "currency": "CNY", "loss_tolerance": "0.25",
                "as_of": "2029-12-31T08:00:00Z", "user_source": {"message": "Synthetic cash-only test", "confirmed_at": "2029-12-31T09:00:00Z"},
                "confirmed_initial_all_cash": False, "account_hash": fingerprint(self.initial)}, self.store, "cash-profile")
            profile.update_constraints({"constraints": {"platform": "TT", "currency": "CNY", "goal": "Synthetic cash-start trial", "excluded_categories": [], "position_limits": {"fund_group_limits": {}, "sector_limits": {}}}, "current_constraints_hash": None, "user_source": {"message": "Synthetic plan", "confirmed_at": "2029-12-31T09:00:00Z"}}, self.store, "plan")
        self.spec["benchmark"] = {"rule": "fixed_weights", "weights": {"000001": "0.5", "000002": "0.5"}, "cash_weight": "0", "rebalance_dates": []}
        self.protocol["initial"]["account_hash"] = fingerprint(self.initial)
        self.protocol["risk_state"] = profile.resolve(self.store, self.initial, "2029-12-31T12:00:00Z", {"000001": "1", "000002": "1"})
        self.register()
        self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(buy=False), "cash-decision"), "cash-decision")
        orders = self.store.get("trial_slot", "study:2030-01-02")["decision"]["orders"]["benchmark"]
        self.assertEqual({row["code"] for row in orders}, {"000001", "000002"})
        self.assertTrue(all(row["side"] == "buy" and Decimal(row["cash_limit"]) == 5000 for row in orders))
        self.assertEqual(self.store.get("account", "main"), self.initial)

    def test_changed_risk_blocks_new_decision_without_rebasing_capital(self):
        self.register()
        self.clock = "2030-01-01T10:00:00Z"
        with patch.object(profile, "utc_now", return_value=self.clock):
            profile.update({"mode": "base", "loss_tolerance": "0.4", "start_at": None, "end_at": None, "end_active_temporary": True,
                            "current_profile_hash": profile.status(self.store)["profile_hash"],
                            "user_source": {"message": "Confirmed test risk change", "confirmed_at": self.clock}},
                           self.store, "new-risk")
        with self.assertRaisesRegex(ValueError, "Registered risk profile changed"):
            self.decision()
        self.assertEqual(self.store.get("capital_baseline", "main")["principal"], "1100")
        self.assertEqual(self.store.get("capital_baseline", "trial:study:strategy")["principal"], "1100")

    def test_principal_observation_detects_breach_independent_of_unit_return(self):
        book = ledger.apply_event(ledger.initial_state("CNY"), {
            "id": "risk-opening", "type": "opening", "sequence": 1,
            "effective_at": "2029-12-31T07:00:00Z", "known_at": "2029-12-31T08:00:00Z",
            "recorded_at": "2029-12-31T08:00:00Z", "data": {"cash": "800", "lots": [], "prices": {}, "price_dates": {}}})
        result = trial._principal_observation(self.protocol, book, [], self.clock, self.clock, {})
        self.assertEqual((result["net_principal"], result["principal_floor"], result["remaining_loss_budget"]), ("1100", "825", "-25"))
        self.assertFalse(result["floor_satisfied"])

    def cross_day_schedule(self, execution_day="02"):
        self.protocol["schedule"] = [{"date": "2030-01-03", "decision_deadline": "2030-01-01T12:00:00Z",
            "execution_at": "2030-01-" + execution_day + "T07:00:00Z", "valuation_at": "2030-01-03T07:00:00Z",
            "outcome_not_before": "2030-01-03T10:00:00Z", "outcome_deadline": "2030-01-05T12:00:00Z"}]
        self.protocol["calibration_config"] = {"sample_size": 1, "dates": ["2030-01-03"]}
        self.market("m1", "2029-12-31", "2029-12-31T08:00:00Z")

    def test_economic_execution_windows_must_not_overlap_prior_valuation(self):
        self.protocol["schedule"][1]["decision_deadline"] = self.protocol["schedule"][0]["decision_deadline"]
        self.protocol["schedule"][1]["execution_at"] = self.protocol["schedule"][0]["valuation_at"]
        with self.assertRaisesRegex(ValueError, "previous valuation"):
            self.register()
        self.assertEqual(self.anchor.call_count, 0)

    def test_cross_day_benchmark_uses_registered_nav_and_later_cash_is_retained(self):
        self.cross_day_schedule()
        self.spec["benchmark"] = {"rule": "fixed_weights", "weights": {"000001": "0.5", "000002": "0.5"}, "cash_weight": "0", "rebalance_dates": []}
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        request = self.decision(date="2030-01-03")
        request["cashflows"]["close"] = "100"
        self.commit(self.prepare("trial_decision", request, "cross-decision"), "cross-decision")
        nav = {code: [{"date": day, "nav": value, "distribution_per_share": 0}
                      for day, value in [("2029-12-31", 1), ("2030-01-02", 1.5 if code == "000001" else 1),
                                         ("2030-01-03", 2 if code == "000001" else 1)]] for code in ("000001", "000002")}
        self.market("m2", "2030-01-03", "2030-01-03T10:00:00Z", a="2", nav_rows=nav)
        self.clock = "2030-01-03T11:00:00Z"
        self.commit(self.prepare("trial_outcome", self.outcome("2030-01-03", "m2"), "cross-outcome"), "cross-outcome")
        benchmark = self.store.get("account", "trial:study:benchmark")
        a_shares = sum(Decimal(row["shares"]) for row in benchmark["lots"].values() if row["code"] == "000001")
        self.assertEqual(a_shares, Decimal("366.66666666"))
        self.assertEqual(benchmark["cash"], "100")
        row = self.store.get("trial_observation", "study:2030-01-03")
        self.assertAlmostEqual(row["paired_return"], -150 / 1100, places=11)
        self.assertEqual(row["principal_risk"]["net_principal"], "1200")
        for _, record in self.store.scan("trial_ledger_event"):
            event = record["event"]
            if event["type"] == "buy_fill":
                self.assertEqual(event["effective_at"], "2030-01-02T07:00:00Z")
        self.clock = "2030-01-06T00:00:00Z"
        self.assertEqual(self.prepare("trial_evaluate", {"trial_id": "study"}, "cross-evaluate")["result"]["reason"],
                         "design_calibration_not_qualified")
        history = trial._history
        def changed_clock(*args):
            events = copy.deepcopy(history(*args))
            for event in events:
                if event["type"] == "buy_fill" and event["id"].startswith("sim:"):
                    event["effective_at"] = "2030-01-03T07:00:00Z"
            return events
        with patch.object(trial, "_history", side_effect=changed_clock), self.assertRaisesRegex(ValueError, "common clock"):
            trial._recheck_observations(self.store.get("trial", "study")["document"], self.protocol,
                                        [row], self.store, self.artifacts)

    def test_execution_date_deviation_retains_source_and_invalidates_episode(self):
        self.cross_day_schedule(execution_day="03")
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(date="2030-01-03"), "deviation-decision"), "deviation-decision")
        self.market("m2", "2030-01-03", "2030-01-03T10:00:00Z", a="1.1")
        self.source_fact("early-fill", "buy_fill", {"order_id": "buy-A", "fill_id": "early-fill", "lot_id": "A-lot",
            "shares": "100", "price": "1", "fee": "0", "final": True}, known="2030-01-03T10:01:00Z")
        original = self.store.get("ledger_event", "early-fill")
        self.clock = "2030-01-03T11:00:00Z"
        result = self.commit(self.prepare("trial_outcome", self.outcome("2030-01-03", "m2"), "deviation"), "deviation")
        self.assertEqual(result["status"], "invalid_evidence")
        self.assertEqual(self.store.get("ledger_event", "early-fill"), original)
        violation = self.store.get("trial_scope_violation", "study")
        self.assertEqual(self.artifacts.read_json(violation["source_event_ref"]), original)
        self.assertEqual((violation["registered_execution_date"], violation["actual_execution_date"]), ("2030-01-03", "2030-01-02"))
        self.assertEqual(self.prepare("trial_evaluate", {"trial_id": "study"}, "deviation-review")["result"]["status"], "invalid_evidence")

    def test_sparse_interior_dividend_cannot_be_omitted(self):
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(buy=False), "decision"), "decision")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z",
                    corporate_actions=[{"code": "000002", "date": "2030-01-01", "per_share": "0.1", "pay_date": "2030-01-02"}])
        self.clock = "2030-01-02T11:00:00Z"
        calls = self.anchor.call_count
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.prepare("trial_outcome", self.outcome(), "omitted")
        self.assertEqual(self.anchor.call_count, calls)
        self.assertIsNone(self.store.get("trial_observation", "study:2030-01-02"))

    def dividend_decision(self):
        request = self.decision(buy=False)
        request["distributions"] = [{"code": "000002", "ex_date": "2030-01-01", "record_at": "2029-12-31T15:59:59Z", "income_mode": "cash", "per_share": "0.1", "pay_at": "2030-01-01T16:00:00Z"}]
        self.commit(self.prepare("trial_decision", request, "decision"), "decision")

    def test_unverified_payment_date_reports_insufficient_without_guessing(self):
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.dividend_decision()
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z",
                    corporate_actions=[{"code": "000002", "date": "2030-01-01", "per_share": "0.1", "pay_date": None}])
        self.clock = "2030-01-02T11:00:00Z"
        calls = self.anchor.call_count
        result = self.prepare("trial_outcome", self.outcome(), "missing-payment")["result"]
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertFalse(result["observation_committed"])
        self.assertEqual(self.anchor.call_count, calls)
        self.assertIsNone(self.store.get("trial_observation", "study:2030-01-02"))

    def test_sparse_dividend_uses_ex_date_and_payment_date_and_conserves_value(self):
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.dividend_decision()
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z", b="0.9",
                    corporate_actions=[{"code": "000002", "date": "2030-01-01", "per_share": "0.1", "pay_date": "2030-01-02"}])
        self.clock = "2030-01-02T11:00:00Z"
        result = self.commit(self.prepare("trial_outcome", self.outcome(), "dividend"), "dividend")
        self.assertEqual(result["status"], "observation_recorded")
        for role in ("strategy", "benchmark"):
            state = self.store.get("account", "trial:study:" + role)
            self.assertEqual(Decimal(state["cash"]), Decimal("1010"))
            self.assertEqual(ledger.observe(state, self.clock, {"000001": "1", "000002": "0.9"}, "1")["unit_nav"], "1")
            rows = [row["event"] for key, row in self.store.scan("trial_ledger_event") if key.startswith("study:" + role + ":")]
            self.assertEqual([instant(e["effective_at"]) for e in rows if e["type"] == "dividend_declared"], [instant("2029-12-31T16:00:00Z")])
            self.assertEqual([instant(e["effective_at"]) for e in rows if e["type"] == "dividend_paid"], [instant("2030-01-01T16:00:00Z")])
        self.assertEqual(self.store.get("trial_observation", "study:2030-01-02")["paired_return"], 0)

    def test_omitted_known_partial_and_unknown_are_adopted_automatically(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(), "decision"), "decision")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        fill = self.source_fact("actual-partial", "buy_fill", {"order_id": "buy-A", "fill_id": "partial", "lot_id": "A-lot", "shares": "50", "price": "1", "fee": "0", "final": False})
        self.source_fact("unresolved", "unknown", {"reason": "Unconfirmed broker adjustment"}, sequence=3)
        self.source_fact("other-account", "cashflow", {"amount": "999"}, sequence=2, owner="other")
        self.clock = "2030-01-02T11:00:00Z"
        result = self.commit(self.prepare("trial_outcome", self.outcome(events=[]), "automatic"), "automatic")
        self.assertEqual(result["status"], "awaiting_confirmation")
        state = self.store.get("account", "trial:study:strategy")
        self.assertEqual(Decimal(state["cash"]), Decimal("950"))
        self.assertIn("study:strategy:source:unresolved", [row["id"] for row in state["unknown"]])
        found = [row for _, row in self.store.scan("trial_ledger_event") if row.get("source", {}).get("source_event_id") == fill["id"]]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"]["source_event_hash"], fingerprint(fill))

    def test_confirmed_partial_funding_is_mirrored_once_with_independent_unit_nav(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        request = self.decision(buy=False); request["cashflows"]["close"] = "100"
        self.commit(self.prepare("trial_decision", request, "decision"), "decision")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z", b="1.1")
        self.source_fact("deposit1", "cashflow", {"amount": "50"})
        self.clock = "2030-01-02T11:00:00Z"
        first = self.commit(self.prepare("trial_outcome", self.outcome(), "funding1"), "funding1")
        self.assertEqual(first["status"], "awaiting_confirmation")
        self.source_fact("deposit2", "cashflow", {"amount": "50"}, sequence=3, known="2030-01-02T11:01:00Z")
        self.clock = "2030-01-02T11:02:00Z"
        second = self.commit(self.prepare("trial_outcome", self.outcome(), "funding2"), "funding2")
        self.assertEqual(second["status"], "observation_recorded")
        for role in ("strategy", "benchmark"):
            state = self.store.get("account", "trial:study:" + role)
            self.assertEqual(Decimal(state["cash"]), Decimal("1100"))
            snap = ledger.observe(state, self.clock, {"000001": "1", "000002": "1.1"}, "1")
            self.assertEqual(Decimal(snap["equity"]), Decimal("1210"))
            self.assertAlmostEqual(float(snap["unit_nav"]), 1110/1100, places=14)
            flows = [row["event"] for key, row in self.store.scan("trial_ledger_event") if key.startswith("study:" + role + ":") and row["event"]["type"] == "cashflow"]
            self.assertEqual(len(flows), 2)
        self.assertEqual(self.store.get("trial_observation", "study:2030-01-02")["paired_return"], 0)

    def test_new_source_account_change_blocks_prepared_outcome_commit(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(buy=False), "decision"), "decision")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        self.clock = "2030-01-02T11:00:00Z"
        package = self.prepare("trial_outcome", self.outcome(), "racing-outcome")
        changed = copy.deepcopy(self.initial); changed["sequence"] += 1
        self.store.put("account", "main", changed, immutable=False)
        with self.assertRaises(StaleSnapshot):
            self.commit(package, "racing-outcome")
        self.assertIsNone(self.store.get("trial_observation", "study:2030-01-02"))

    def test_late_known_fact_invalidates_final_observation_inventory(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(buy=False), "decision1"), "decision1")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        self.clock = "2030-01-02T11:00:00Z"
        self.commit(self.prepare("trial_outcome", self.outcome(), "outcome1"), "outcome1")
        self.clock = "2030-01-02T11:30:00Z"
        self.commit(self.prepare("trial_decision", self.decision("d2", "2030-01-03", False), "decision2"), "decision2")
        self.market("m2", "2030-01-03", "2030-01-03T10:00:00Z")
        self.clock = "2030-01-03T11:00:00Z"
        self.commit(self.prepare("trial_outcome", self.outcome("2030-01-03", "m2"), "outcome2"), "outcome2")
        self.source_fact("late-adjustment", "unknown", {"reason": "New broker fact"}, known="2030-01-05T00:00:00Z")
        self.clock = "2030-01-06T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "absent from finalized"):
            self.prepare("trial_evaluate", {"trial_id": "study"}, "final")

    def test_late_fill_completes_original_slot_without_backfilling_missed_decision(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(), "decision1"), "decision1")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z")
        self.source_fact("partial", "buy_fill", {"order_id": "buy-A", "fill_id": "partial", "lot_id": "A-lot", "shares": "50", "price": "1", "fee": "0", "final": False})
        self.clock = "2030-01-02T11:00:00Z"
        self.commit(self.prepare("trial_outcome", self.outcome(), "outcome1"), "outcome1")
        self.clock = "2030-01-02T11:30:00Z"
        with self.assertRaisesRegex(ValueError, "confirmed execution exposure"):
            self.prepare("trial_decision", self.decision("d2", "2030-01-03", False), "decision2")
        self.market("m2", "2030-01-03", "2030-01-03T10:00:00Z", a="1.1")
        self.source_fact("final", "buy_fill", {"order_id": "buy-A", "fill_id": "final", "lot_id": "A-final-lot", "shares": "50", "price": "1", "fee": "0", "final": True}, sequence=3, known="2030-01-03T10:01:00Z")
        self.clock = "2030-01-03T11:00:00Z"
        result = self.commit(self.prepare("trial_outcome", self.outcome(), "first-final"), "first-final")
        self.assertEqual(result["status"], "observation_recorded")
        lots = self.store.get("account", "trial:study:strategy")["lots"]
        self.assertEqual(sum(Decimal(lot["shares"]) for lot in lots.values() if lot["code"] == "000001"), Decimal("100"))
        self.assertIsNone(self.store.get("trial_slot", "study:2030-01-03"))
        self.clock = "2030-01-06T00:00:00Z"
        evaluated = self.prepare("trial_evaluate", {"trial_id": "study"}, "incomplete-evaluate")["result"]
        self.assertEqual(evaluated["reason"], "incomplete_frozen_schedule")

    def test_missing_execution_day_nav_is_insufficient_before_timestamp(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.commit(self.prepare("trial_decision", self.decision(), "decision"), "decision")
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z", nav_rows={})
        self.source_fact("fill", "buy_fill", {"order_id": "buy-A", "fill_id": "fill", "lot_id": "A-lot", "shares": "100", "price": "1", "fee": "0", "final": True})
        self.clock = "2030-01-02T11:00:00Z"
        calls = self.anchor.call_count
        result = self.prepare("trial_outcome", self.outcome(), "missing-nav")["result"]
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["reason"], "execution_date_nav_unverified")
        self.assertEqual(self.anchor.call_count, calls)
        self.assertIsNone(self.store.get("trial_observation", "study:2030-01-02"))

    def test_confirmed_dividend_is_auto_adopted_without_double_credit(self):
        self.protocol["observation_mode"] = "user_confirmed_execution"
        self.register(); self.clock = "2030-01-01T10:00:00Z"
        self.dividend_decision()
        self.market("m1", "2030-01-02", "2030-01-02T10:00:00Z", b="0.9",
                    corporate_actions=[{"code": "000002", "date": "2030-01-01", "per_share": "0.1", "pay_date": "2030-01-02"}])
        self.clock = "2030-01-02T11:00:00Z"
        partial = self.commit(self.prepare("trial_outcome", self.outcome(), "unconfirmed"), "unconfirmed")
        self.assertEqual(partial["status"], "awaiting_confirmation")
        self.source_fact("dividend-right", "dividend_declared", {"distribution_id": "issuer-dividend", "code": "000002", "per_share": "0.1", "pay_at": "2030-01-01T16:00:00Z"}, effective="2029-12-31T16:00:00Z")
        self.source_fact("dividend-paid", "dividend_paid", {"receivable_id": "issuer-dividend"}, sequence=3, effective="2030-01-01T16:00:00Z")
        result = self.commit(self.prepare("trial_outcome", self.outcome(), "confirmed"), "confirmed")
        self.assertEqual(result["status"], "observation_recorded")
        for role in ("strategy", "benchmark"):
            self.assertEqual(self.store.get("account", "trial:study:" + role)["cash"], "1010")
        self.assertEqual(self.store.get("trial_observation", "study:2030-01-02")["paired_return"], 0)

    def test_early_endpoint_waits_and_incomplete_final_window_is_explicit(self):
        self.register()
        self.assertEqual(self.prepare("trial_evaluate", {"trial_id": "study"}, "early")["result"]["status"], "awaiting_observations")
        self.clock = "2030-01-06T00:00:00Z"
        self.assertEqual(self.prepare("trial_evaluate", {"trial_id": "study"}, "late")["result"]["status"], "invalid_evidence")


if __name__ == "__main__":
    unittest.main()
