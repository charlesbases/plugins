"""Independent accounting and ownership regressions for the v4 contract."""
import copy
import sys
import tempfile
import unittest
import sqlite3
import hashlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import cashflows
import ledger
import risk_profile as profile
from artifacts import Artifacts
from contracts import ConflictError, RetryableError, EvidenceError, fingerprint, parse_request
from state_store import Store, LeaseLost

AT = "2030-01-02T08:00:00Z"


class FinancialV4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="investment-v4-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name, "p")
        self.now = AT
        for module in (cashflows, profile):
            mocked = patch.object(module, "utc_now", side_effect=lambda: self.now)
            mocked.start(); self.addCleanup(mocked.stop)
        self.user = {"message": "Confirmed synthetic accounting fixture", "confirmed_at": AT}
        self.call("open", profile.initialize, {"principal": "1000", "currency": "CNY", "loss_tolerance": "0.25",
                  "as_of": AT, "user_source": self.user, "confirmed_initial_all_cash": True, "account_hash": None})

    def call(self, identity, function, payload):
        self.store.begin(identity, {"payload": payload})
        with self.store.lease(identity) as prior:
            if prior is not None:
                return prior
            result = function(payload, self.store, identity)
            return self.store.complete(identity, result)

    def draft(self, action, amount="100", provider=True):
        return self.call("draft-" + action, cashflows.prepare, {"client_action_id": action,
            "identity": {"kind": "provider", "institution": "synthetic-bank", "account_reference": "one", "transaction_reference": "transfer-one"}
                        if provider else {"kind": "manual"},
            "currency": "CNY", "amount": amount, "effective_at": self.now, "valuation": None, "evidence_refs": []})

    def confirm(self, identity, draft, choice="new", existing=None):
        return self.call(identity, cashflows.confirm, {"draft_id": draft["draft_id"], "draft_hash": draft["draft_hash"],
            "choice": choice, "existing_transfer_id": existing, "user_source": self.user})

    def test_provider_identity_across_requests_and_semantic_conflict(self):
        first = self.confirm("confirm-one", self.draft("one"))
        again = self.confirm("confirm-two", self.draft("two"))
        self.assertEqual(again["status"], "repeated")
        self.assertEqual(first["transfer_id"], again["transfer_id"])
        self.assertEqual(self.store.get("account", "main")["cash"], "1100")
        with self.assertRaises(ConflictError):
            self.confirm("conflicting", self.draft("three", "101"))
        self.assertEqual(self.store.get("account", "main")["cash"], "1100")

    def test_unknown_only_history_needs_explicit_first_activity_confirmation(self):
        import account_reconciliation
        self.store = Store(self.tmp.name, "not-started")
        old = {"id": "cash-not-confirmed", "type": "unknown", "sequence": 1, "effective_at": AT,
               "known_at": AT, "recorded_at": AT, "data": {"reason": "Budget is not deposited cash"}}
        with self.store.maintenance():
            self.store.put("account", "main", ledger.apply_event(ledger.initial_state("CNY"), old), False)
            self.store.put("ledger_event", old["id"], old)
            self.store.put("ledger_event_owner", old["id"], {"account_id": "main"})
            self.store.put("profile", "budget", {"budget": "1000", "deposited": False})
        payload = {"principal": "500", "currency": "CNY", "loss_tolerance": "0.25", "as_of": AT,
                   "user_source": {"message": "First actual available cash is 500; no previous economic activity, holdings or pending orders", "confirmed_at": AT},
                   "confirmed_initial_all_cash": True, "account_hash": fingerprint(self.store.get("account", "main"))}
        with self.assertRaisesRegex(ValueError, "Explicitly confirm"):
            self.call("missing-confirmation", profile.initialize, payload)
        payload["confirmed_no_prior_economic_activity"] = True
        self.call("confirmed-first-cash", profile.initialize, payload)
        state = self.store.get("account", "main")
        self.assertEqual((state["cash"], state["units"], state["unknown"]), ("500", "500", []))
        self.assertEqual(self.store.get("ledger_event", old["id"]), old)
        self.assertEqual(self.store.get("profile", "budget"), {"budget": "1000", "deposited": False})
        self.assertTrue(ledger.snapshot(state, AT)["performance_exact"])
        self.assertEqual(profile.resolve(self.store, state, AT, {})["remaining_loss_budget"], "125")
        market = {"market_ref": {"prices": {}, "price_dates": {}}, "provenance": {"corporate_actions": []}, "evidence_refs": []}
        self.store.begin("daily-mark", {})
        with self.store.lease("daily-mark"), patch.object(account_reconciliation, "utc_now", return_value=AT):
            reconciled = account_reconciliation.reconcile_market("main", market, self.store, Artifacts(self.store.base), "daily-mark")
            self.store.complete("daily-mark", reconciled)
        self.assertEqual(reconciled["status"], "reconciled")

    def test_first_activity_confirmation_cannot_reset_a_withdrawn_economic_history(self):
        self.store = Store(self.tmp.name, "economic-history")
        opening = {"id": "original-capital", "type": "opening", "sequence": 1, "effective_at": AT, "known_at": AT,
                   "recorded_at": AT, "data": {"cash": "100", "lots": [], "prices": {}, "price_dates": {}}}
        state = ledger.apply_event(ledger.initial_state("CNY"), opening)
        withdrawal = {"id": "withdrawn", "type": "cashflow", "sequence": 2, "effective_at": AT, "known_at": AT, "recorded_at": AT,
                      "data": {"transfer_id": "bank-out", "revision_id": "r1", "previous_revision": None, "amount": "-100", "valuation": None}}
        state = ledger.apply_event(state, withdrawal)
        with self.store.maintenance():
            self.store.put("account", "main", state, False)
            for event in (opening, withdrawal):
                self.store.put("ledger_event", event["id"], event)
                self.store.put("ledger_event_owner", event["id"], {"account_id": "main"})
        payload = {"principal": "500", "currency": "CNY", "loss_tolerance": "0.25", "as_of": AT,
                   "user_source": self.user, "confirmed_initial_all_cash": True, "confirmed_no_prior_economic_activity": True,
                   "account_hash": fingerprint(state)}
        with self.assertRaisesRegex(ValueError, "cannot erase prior economic"):
            self.call("cannot-restart", profile.initialize, payload)
        self.assertEqual(self.store.get("account", "main"), state)
        self.assertIsNone(self.store.get("capital_baseline", "main"))

    def test_manual_same_amount_requires_explicit_existing_or_new(self):
        first = self.confirm("first", self.draft("one", provider=False))
        second = self.draft("two", provider=False)
        self.assertEqual(len(second["candidates"]), 1)
        self.confirm("linked", second, "existing", first["transfer_id"])
        self.assertEqual(self.store.get("account", "main")["cash"], "1100")
        self.confirm("separate", self.draft("three", provider=False))
        self.assertEqual(self.store.get("account", "main")["cash"], "1200")

    def test_later_provider_identity_link_to_manual_fact_survives_future_retries(self):
        original = self.confirm("manual", self.draft("manual", provider=False))
        linked = self.confirm("link-provider", self.draft("provider"), "existing", original["transfer_id"])
        replayed = self.confirm("new-request", self.draft("provider-again"))
        self.assertEqual(linked["transfer_id"], replayed["transfer_id"])
        self.assertEqual(self.store.get("account", "main")["cash"], "1100")

    def test_flow_prices_need_audited_market_binding_and_correction_restores_exact_units(self):
        import pipeline
        import allocation_runtime
        objects = Artifacts(self.store.base)
        receipt = objects.put_bytes(b"Synthetic confirmed purchase and transfer evidence")
        fill = {"id": "owned-A", "type": "external_fill_confirmed", "effective_at": AT, "known_at": AT, "recorded_at": AT,
                "data": {"side": "buy", "code": "A", "fill_id": "A-fill", "lot_id": "A", "shares": "100", "price": "10",
                "price_date": AT[:10], "gross_amount": "1000", "fee": "0", "cash_amount": "1000", "settlement_at": None,
                "holding_started_at": AT, "ownership_at": AT, "evidence_ref": receipt}}
        with patch.object(allocation_runtime, "ensure_runtime", return_value=None), patch.object(pipeline, "utc_now", return_value=AT):
            self.call("purchase", lambda payload, store, operation: pipeline.feedback(payload, store), {"currency": "CNY", "events": [fill]})
        valuation = {"at": AT, "prices": {"A": "20"}, "price_dates": {"A": AT[:10]}, "evidence_refs": [receipt]}
        draft = self.call("flow-draft", cashflows.prepare, {"client_action_id": "cash", "identity": {"kind": "manual"},
            "currency": "CNY", "amount": "1000", "effective_at": AT, "valuation": valuation, "evidence_refs": [receipt]})
        first = self.confirm("cash", draft)
        self.assertEqual(first["valuation_evidence_level"], "unverified_user_valuation")
        self.assertEqual((first["cash"], first["performance_exact"]), ("1000", False))
        # This is the audited-market service boundary fixture, not live NAV evidence.
        market = {"market_ref": {"schema_version": 4, "currency": "CNY"}, "evidence_refs": [receipt],
                  "provenance": {"price_validation": "verified_source_NAV", "nav_ref": objects.put_json({"A": [{"date": AT[:10], "nav": 20}]})}}
        market_id = fingerprint(market)
        with self.store.maintenance(): self.store.put("market", market_id, market)
        self.now = "2030-01-03T08:00:00Z"
        wrong = self.call("wrong-price", cashflows.correct, {"transfer_id": first["transfer_id"], "expected_revision": first["revision_id"],
            "amount": "1000", "effective_at": AT, "valuation": {**valuation, "prices": {"A": "10"}, "source_market_id": market_id},
            "evidence_refs": [receipt], "user_source": self.user, "reason": "Source price still disagrees"})
        self.assertEqual((wrong["cash"], wrong["performance_exact"]), ("1000", False))
        result = self.call("verified-price", cashflows.correct, {"transfer_id": first["transfer_id"], "expected_revision": wrong["revision_id"],
            "amount": "1000", "effective_at": AT, "valuation": {**valuation, "source_market_id": market_id},
            "evidence_refs": [receipt], "user_source": self.user, "reason": "Independent NAV now agrees"})
        self.assertTrue(result["performance_exact"])
        state = ledger.snapshot(self.store.get("account", "main"), self.now, {"A": "20"}, {"A": AT[:10]})
        self.assertEqual((state["cash"], state["units"], state["unit_nav"]), ("1000", "1500", "2"))

    def test_pre_ex_mark_cannot_double_count_income_and_unowned_candidate_needs_no_entitlement(self):
        import pipeline
        import allocation_runtime
        import account_reconciliation
        objects = Artifacts(self.store.base); proof = objects.put_bytes(b"Synthetic source entitlement fixture")
        buy = {"id": "B-buy", "type": "external_fill_confirmed", "effective_at": AT, "known_at": AT, "recorded_at": AT,
               "data": {"side": "buy", "code": "B", "fill_id": "B-fill", "lot_id": "B", "shares": "100", "price": "10",
               "price_date": AT[:10], "gross_amount": "1000", "fee": "0", "cash_amount": "1000", "settlement_at": None,
               "holding_started_at": AT, "ownership_at": AT, "evidence_ref": proof}}
        feedback = lambda payload, store, operation: pipeline.feedback(payload, store)
        with patch.object(allocation_runtime, "ensure_runtime", return_value=None), patch.object(pipeline, "utc_now", side_effect=lambda: self.now):
            self.call("B-purchase", feedback, {"currency": "CNY", "events": [buy]})
            self.now = "2030-01-04T08:00:00Z"
            declaration = {"id": "B-dividend", "type": "dividend_declared", "effective_at": "2030-01-02T16:00:00Z",
                "known_at": self.now, "recorded_at": self.now, "data": {"distribution_id": "B-income", "code": "B", "per_share": "1",
                "pay_at": "2030-01-05T08:00:00Z", "record_at": "2030-01-02T15:59:59Z", "entitled_shares": "100"}}
            self.call("B-entitlement", feedback, {"currency": "CNY", "events": [declaration]})
        actions = [{"code": "B", "ex_date": "2030-01-03", "record_date": "2030-01-02", "per_share": "1"},
                   {"code": "C", "ex_date": "2030-01-03", "record_date": None, "per_share": "1"}]
        stale = {"market_ref": {"prices": {"B": "10", "C": "9"}, "price_dates": {"B": "2030-01-02", "C": "2030-01-03"}},
                 "provenance": {"corporate_actions": actions}, "evidence_refs": []}
        reconcile = lambda market, store, operation: account_reconciliation.reconcile_market("main", market, store, objects, operation)
        with patch.object(account_reconciliation, "utc_now", side_effect=lambda: self.now):
            first = self.call("stale-valuation", reconcile, stale)
            self.assertIn("obtain_post_ex_date_NAV", [row["action"] for row in first["required_actions"]])
            state = self.store.get("account", "main")
            self.assertEqual(state["marks"]["B"], "10")
            self.assertEqual(state["receivables"]["B-income"]["amount"], "100")
            self.assertIsNone(ledger.snapshot(state, self.now)["equity"])
            with self.assertRaisesRegex(ValueError, "reconciliation"):
                profile.resolve(self.store, state, self.now, stale["market_ref"]["prices"], price_dates=stale["market_ref"]["price_dates"])
            fresh = copy.deepcopy(stale); fresh["market_ref"]["prices"]["B"] = "9"; fresh["market_ref"]["price_dates"]["B"] = "2030-01-03"
            second = self.call("post-ex-valuation", reconcile, fresh)
        self.assertEqual((second["status"], second["required_actions"]), ("reconciled", []))
        state = self.store.get("account", "main")
        self.assertEqual(ledger.snapshot(state, self.now)["equity"], "1000")
        self.assertEqual(profile.resolve(self.store, state, self.now, fresh["market_ref"]["prices"], price_dates=fresh["market_ref"]["price_dates"])["remaining_loss_budget"], "250")

    def test_correction_preserves_old_knowledge_and_changes_cash_and_principal_once(self):
        first = self.confirm("first", self.draft("one"))
        self.now = "2030-01-03T08:00:00Z"
        self.call("correction", cashflows.correct, {"transfer_id": first["transfer_id"], "expected_revision": first["revision_id"],
            "amount": "80", "effective_at": AT, "valuation": None, "evidence_refs": [], "user_source": self.user,
            "reason": "Statement corrects the amount; original record remains visible"})
        account = self.store.get("account", "main")
        self.assertEqual(account["cash"], "1080")
        self.assertEqual(profile.resolve(self.store, account, self.now, {})["net_principal"], "1080")
        history = [row for _, row in self.store.scan("ledger_event")]
        prior = ledger.rebuild(ledger.initial_state("CNY"), history, AT)
        self.assertEqual(prior["cash"], "1100")
        self.assertEqual(len([row for row in history if row["type"] == "cashflow"]), 2)

    def test_explicit_base_update_can_end_temporary_override(self):
        temporary = self.call("temp", profile.update, {"mode": "temporary", "loss_tolerance": "0.5", "start_at": None,
            "end_at": "2030-01-09T08:00:00Z", "user_source": self.user,
            "current_profile_hash": profile.status(self.store)["profile_hash"]})
        self.now = "2030-01-03T08:00:00Z"
        result = self.call("base", profile.update, {"mode": "base", "loss_tolerance": "0.25", "start_at": None,
            "end_at": None, "end_active_temporary": True, "user_source": self.user,
            "current_profile_hash": temporary["profile_hash"]})
        self.assertEqual(result["effective_loss_tolerance"], "0.25")
        self.assertEqual(result["ended_temporary_revisions"], [temporary["risk_revision"]])
        self.assertEqual(profile.resolve(self.store, self.store.get("account", "main"), self.now, {})["principal_floor"], "750")
        self.assertIsNone(profile.resolve(self.store, self.store.get("account", "main"), self.now, {})["valid_until"])
        self.assertEqual(self.store.get("capital_baseline", "main")["principal"], "1000")

    def test_full_withdrawal_is_recorded_without_inventing_new_principal(self):
        self.confirm("withdraw", self.draft("out", "-1000"))
        state = self.store.get("account", "main")
        self.assertEqual(state["cash"], "0")
        with self.assertRaises(profile.PrincipalPolicyRequired):
            profile.resolve(self.store, state, self.now, {})
        self.assertEqual(self.store.get("capital_baseline", "main")["principal"], "1000")

    def test_real_receipt_intake_survives_existing_unknown_and_resolves_only_its_marker(self):
        import pipeline
        import allocation_runtime
        source = Artifacts(self.store.base).put_bytes(b"Explicit synthetic broker receipt for reconciliation test")
        original = {"id": "previous-gap", "type": "unknown", "effective_at": AT, "known_at": AT, "recorded_at": AT,
                    "data": {"reason": "Original independent account question"}}
        receipt = {"id": "confirmed-receipt", "type": "buy_fill", "effective_at": AT, "known_at": AT, "recorded_at": AT,
                   "data": {"order_id": "missing-order", "fill_id": "broker-fill", "lot_id": "lot", "shares": "1", "price": "10",
                            "fee": "0", "final": True, "price_date": AT[:10], "holding_started_at": AT, "ownership_at": AT,
                            "gross_amount": "10", "cash_debit": "10"}}
        def feedback(payload, store, operation_id):
            return pipeline.feedback(payload, store)
        with patch.object(allocation_runtime, "ensure_runtime", return_value=None), patch.object(pipeline, "utc_now", return_value=AT):
            self.call("original-gap", feedback, {"currency": "CNY", "events": [original]})
            payload = {"currency": "CNY", "events": [receipt], "evidence": {receipt["id"]: [source]}}
            for identity in ("intake-one", "intake-two"):
                with self.assertRaises(pipeline.UnreconciledConfirmation):
                    self.call(identity, feedback, payload)
            records = list(self.store.scan("unreconciled_fact"))
            self.assertEqual(len(records), 1)
            marker_id = records[0][0]
            self.assertEqual(Artifacts(self.store.base).read_json({**records[0][1]["raw_ref"], "media_type": "application/json"}), payload)
            self.assertEqual({row["id"] for row in self.store.get("account", "main")["unknown"]}, {"previous-gap", marker_id})
            resolve = {"id": "resolve-receipt", "type": "resolve_unknown", "effective_at": AT, "known_at": AT, "recorded_at": AT,
                       "data": {"unknown_id": marker_id, "evidence_ref": source, "resolution": "Original receipt independently reconciled"}}
            self.call("resolution", feedback, {"currency": "CNY", "events": [resolve]})
            state = self.store.get("account", "main")
            self.assertEqual([row["id"] for row in state["unknown"]], ["previous-gap"])
            self.assertEqual((state["cash"], state["lots"]), ("1000", {}))


class PureLedgerV4Tests(unittest.TestCase):
    def test_public_feedback_cannot_create_internal_performance_start(self):
        event = {"id": "cannot-forge-start", "type": "performance_start", "effective_at": AT, "known_at": AT, "recorded_at": AT,
                 "data": {"cash": "500", "confirmed_no_prior_economic_activity": True, "resolved_unknown_ids": [],
                          "user_source": {"message": "Unrouted assertion", "confirmed_at": AT}}}
        feedback = {"currency": "CNY", "events": [event]}
        for operation, payload in (("feedback", feedback), ("daily_review", {"feedback": feedback})):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                parse_request({"schema_version": 4, "request_id": "forbidden-start", "operation": operation, "payload": payload})

    def test_external_confirmed_purchase_uses_actual_debit_and_statement_recovery_keeps_history_gap(self):
        opened = {"id": "open", "type": "opening", "sequence": 1, "effective_at": AT, "known_at": AT, "recorded_at": AT,
                  "data": {"cash": "1000", "lots": [], "prices": {}, "price_dates": {}}}
        state = ledger.apply_event(ledger.initial_state("CNY"), opened)
        receipt = {"sha256": "a" * 64, "size": 1, "path": "objects/synthetic"}
        confirmed = {"id": "external", "type": "external_fill_confirmed", "sequence": 2, "effective_at": AT, "known_at": AT,
                     "recorded_at": AT, "data": {"side": "buy", "code": "A", "fill_id": "broker-one", "lot_id": "A-lot",
                     "shares": "33.33", "price": "3", "price_date": AT[:10], "gross_amount": "100", "fee": "1",
                     "cash_amount": "101", "settlement_at": None, "holding_started_at": AT, "ownership_at": AT, "evidence_ref": receipt}}
        acquired = ledger.apply_event(state, confirmed)
        self.assertEqual(acquired["cash"], "899")
        self.assertEqual(acquired["lots"]["A-lot"]["shares"], "33.33")
        self.assertIn("share_rounding_transfer", [row["kind"] for row in acquired["reconciliation"]])
        unknown = {"id": "gap", "type": "unknown", "sequence": 3, "effective_at": AT, "known_at": AT, "recorded_at": AT,
                   "data": {"reason": "Missing historical adjustment"}}
        acquired = ledger.apply_event(acquired, unknown)
        snapshot = {"id": "statement", "type": "account_snapshot_confirmed", "sequence": 4, "effective_at": AT,
                    "known_at": AT, "recorded_at": AT, "data": {"cash": "900", "lots": list(acquired["lots"].values()),
                    "prices": {"A": "3"}, "price_dates": {"A": AT[:10]}, "orders": {}, "receivables": {},
                    "pending_subscriptions": {}, "evidence_ref": receipt}}
        recovered = ledger.apply_event(acquired, snapshot)
        self.assertEqual(recovered["cash"], "900")
        self.assertTrue(recovered["unknown"])
        self.assertTrue(recovered["performance_pending"])
        self.assertEqual(acquired["cash"], "899")

    def test_missing_flow_marks_preserve_cash_and_current_equity_without_false_return(self):
        initial = ledger.initial_state("CNY")
        opened = {"id": "opening", "type": "opening", "sequence": 1, "effective_at": AT, "known_at": AT, "recorded_at": AT,
                  "data": {"cash": "0", "lots": [{"lot_id": "owned", "code": "A", "shares": "100", "acquired_at": AT,
                           "ownership_at": AT, "price_date": AT[:10]}], "prices": {"A": "10"}, "price_dates": {"A": AT[:10]}}}
        account = ledger.apply_event(initial, opened)
        flow = {"id": "deposit", "type": "cashflow", "sequence": 2, "effective_at": AT, "known_at": AT, "recorded_at": AT,
                "data": {"transfer_id": "bank:one", "revision_id": "r1", "previous_revision": None, "amount": "1000", "valuation": None}}
        pending = ledger.apply_event(account, flow)
        snap = ledger.snapshot(pending, AT, {"A": "20"}, {"A": AT[:10]})
        self.assertEqual((snap["cash"], snap["available_cash"], snap["equity"]), ("1000", "1000", "3000"))
        self.assertFalse(snap["blocked"])
        self.assertFalse(snap["performance_exact"])
        self.assertIsNone(snap["unit_nav"])
        exact_flow = copy.deepcopy(flow)
        exact_flow["data"]["valuation"] = {"at": AT, "prices": {"A": "20"}, "price_dates": {"A": AT[:10]},
                                            "evidence_refs": [{"synthetic": "independent 20-per-share fixture"}]}
        exact = ledger.snapshot(ledger.apply_event(account, exact_flow), AT, {"A": "20"}, {"A": AT[:10]})
        self.assertEqual((exact["units"], exact["unit_nav"]), ("1500", "2"))


class LeaseV4Tests(unittest.TestCase):
    def test_refusing_legacy_database_does_not_change_original_bytes_or_schema(self):
        with tempfile.TemporaryDirectory(prefix="investment-legacy-") as root:
            folder = Path(root) / "plans/p"; folder.mkdir(parents=True)
            path = folder / "active.sqlite"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript("CREATE TABLE records(old TEXT); PRAGMA user_version=2;")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(EvidenceError): Store(root, "p")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("records",)])

    def test_stale_generation_cannot_write_stage_failure_or_final_result(self):
        with tempfile.TemporaryDirectory(prefix="investment-fence-") as root:
            first, second = Store(root, "p"), Store(root, "p")
            first.begin("one", {})
            with first.lease("one"):
                old = first.current_context
                with first._metadata_transaction() as conn:
                    conn.execute("UPDATE operations SET lease_until=0 WHERE id='one'")
                with second.lease("one"):
                    self.assertEqual(second.current_context.generation, old.generation + 1)
                    for action in (lambda: first.put("account", "main", {}),
                                   lambda: first.stage("one", "x", {}),
                                   lambda: first.fail("one", "failed", {}),
                                   lambda: first.complete("one", {"winner": "old"})):
                        with self.assertRaises(LeaseLost): action()
                    second.complete("one", {"winner": "new"})
            self.assertEqual(first.operation("one")["result"], {"winner": "new"})
            with self.assertRaises(LeaseLost): first.put("account", "main", {})

    def test_live_owner_renews_and_offline_maintenance_excludes_other_writers(self):
        with tempfile.TemporaryDirectory(prefix="investment-fence-") as root:
            store, other = Store(root, "p"), Store(root, "p")
            store.begin("one", {})
            with store.lease("one"):
                before = store.operation("one")["lease_until"]
                store.renew(store.current_context)
                self.assertGreaterEqual(store.operation("one")["lease_until"], before)
            with store.maintenance():
                with self.assertRaises(RetryableError) as raised: other.begin("two", {})
                self.assertIn("offline maintenance", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
