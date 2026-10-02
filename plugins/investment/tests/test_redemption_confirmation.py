"""Actual confirmation endpoint regressions; calendars are explicit engineering inputs."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal

import ledger
import execution
import pipeline
from contracts import validate_feedback_shape, fingerprint, ConflictError
from state_store import Store
from test_ledger import opening, event, order, T1
from test_numerical_schema4 import terms


class RedemptionConfirmationTests(unittest.TestCase):
    @staticmethod
    def reserved(end_event="confirmation_date"):
        state, _ = opening("0", "1000")
        contract = terms("000001")
        contract["holding"]["end_event"] = end_event
        contract["order_cutoff_local"] = "15:00"
        contract["confirmation"] = {"lag_days": 1, "day_basis": "trading_days"}
        contract["redemption"] = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7", "fee": {"kind": "percentage", "rate": "0.005"}}]}
        request = order("sell", "1000")
        request["fee_contract"] = contract
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": request}, T1))
        return state, request

    @staticmethod
    def reported(state, day, fee, *, confirmed=True, known=None, price_day=None):
        stamp = day + "T18:00:00+08:00"
        data = {"order_id": "order-one", "fill_id": "actual:"+day, "shares": "1000", "price": "1", "fee": fee,
            "gross_amount": "1000", "net_amount": str(1000-int(fee)), "price_date": price_day or day,
            "settlement_at": "2030-01-20T00:00:00+08:00", "final": True}
        if confirmed:
            data["actual_redemption_confirmed_at"] = stamp
        return {"id": "reported:"+day, "type": "sell_fill", "sequence": state["sequence"]+1,
            "effective_at": stamp, "known_at": known or stamp, "recorded_at": known or stamp, "data": data}

    def test_confirmed_after_cutoff_before_on_after_holding_fee_boundary(self):
        for day, fee in (("2030-01-07", "15"), ("2030-01-08", "5"), ("2030-01-09", "5")):
            with self.subTest(day=day):
                state, _ = self.reserved()
                report = self.reported(state, day, fee)
                validate_feedback_shape({"currency": "CNY", "events": [{key:value for key,value in report.items() if key != "sequence"}]})
                after = ledger.apply_event(state, report)
                self.assertFalse(any(row["kind"] in ("fee_deviation", "expected_quote_unknown") for row in after["reconciliation"]))
                self.assertEqual(report["data"]["fee"], fee)
                self.assertEqual(after["receivables"]["actual:"+day]["amount"], str(1000-int(fee)))

    def test_late_knowledge_does_not_replace_actual_confirmation_or_identity(self):
        state, _ = self.reserved()
        report = self.reported(state, "2030-01-07", "15", known="2030-01-15T08:00:00+08:00")
        immediate = copy.deepcopy(report)
        immediate["known_at"] = immediate["recorded_at"] = report["data"]["actual_redemption_confirmed_at"]
        self.assertEqual(ledger.financial_identity(report), ledger.financial_identity(immediate))
        after = ledger.apply_event(state, report)
        self.assertFalse(any(row["kind"] == "fee_deviation" for row in after["reconciliation"]))
        self.assertEqual(after["receivables"]["actual:2030-01-07"]["amount"], "985")

    def test_unknown_actual_confirmation_keeps_reported_money_without_fallback_fee(self):
        state, _ = self.reserved()
        report = self.reported(state, "2030-01-07", "15", confirmed=False, known="2030-01-15T08:00:00+08:00")
        original = fingerprint(report)
        after = ledger.apply_event(state, report)
        self.assertTrue(any(row["kind"] == "expected_quote_unknown" and row["field"] == "actual_redemption_confirmed_at" for row in after["reconciliation"]))
        self.assertFalse(any(row["kind"] == "fee_deviation" for row in after["reconciliation"]))
        self.assertEqual(after["receivables"]["actual:2030-01-07"]["amount"], "985")
        self.assertEqual(fingerprint(report), original)

    def test_execution_ended_fee_uses_actual_price_date_not_confirmation_clock(self):
        state, _ = self.reserved("execution_date")
        report = self.reported(state, "2030-01-08", "15", confirmed=False, price_day="2030-01-07")
        after = ledger.apply_event(state, report)
        self.assertFalse(any(row["kind"] in ("fee_deviation", "expected_quote_unknown") for row in after["reconciliation"]))

    def test_public_feedback_persists_confirmation_and_unknown_quote_with_actual_receipts(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as directory:
                initial, first = opening("0", "1000")
                state, request = self.reserved()
                reserve = event(initial, "order_reserved", {"order": request}, T1)
                report = self.reported(state, "2030-01-07", "15", confirmed=confirmed,
                    known="2030-01-15T08:00:00+08:00")
                # Rebase the explicitly synthetic calendar and receipt to past
                # dates so the actual feedback recording-time gate is exercised.
                rows = json.loads(json.dumps([first, reserve, report]).replace("2030-", "2025-"))
                for row in rows:
                    row.pop("sequence")
                root = Path(directory) / "data"
                result = pipeline.run({"schema_version": 4, "request_id": "receipt", "operation": "feedback",
                    "payload": {"currency": "CNY", "events": rows}}, root, "confirmation")
                self.assertEqual(result["status"], "recorded")
                store = Store(root, "confirmation")
                stored = store.get("ledger_event", rows[-1]["id"])
                self.assertEqual(stored["data"], rows[-1]["data"])
                account = store.get("account", "main")
                self.assertEqual(account["receivables"]["actual:2025-01-07"]["amount"], "985")
                self.assertEqual(any(row["kind"] == "expected_quote_unknown" for row in account["reconciliation"]), not confirmed)
                alias = copy.deepcopy(rows[-1])
                alias.update(id="later-receipt", known_at="2025-01-16T08:00:00+08:00", recorded_at="2025-01-16T08:00:00+08:00")
                repeated = pipeline.run({"schema_version": 4, "request_id": "later", "operation": "feedback",
                    "payload": {"currency": "CNY", "events": [alias]}}, root, "confirmation")
                self.assertEqual(repeated["added"], [])
                self.assertEqual(store.get("ledger_event", rows[-1]["id"]), stored)
                if confirmed:
                    changed = copy.deepcopy(alias)
                    changed["id"] = "changed-confirmation"
                    changed["data"]["actual_redemption_confirmed_at"] = "2025-01-08T18:00:00+08:00"
                    with self.assertRaises(ConflictError):
                        pipeline.run({"schema_version": 4, "request_id": "changed", "operation": "feedback",
                            "payload": {"currency": "CNY", "events": [changed]}}, root, "confirmation")
                    self.assertEqual(store.get("ledger_event", rows[-1]["id"]), stored)

    def test_confirmation_metadata_cannot_precede_price_or_follow_knowledge(self):
        state, _ = self.reserved()
        report = self.reported(state, "2030-01-07", "15")
        for bad, message in (("2030-01-06T23:00:00+08:00", "precedes"), ("2030-01-08T00:00:00+08:00", "not yet known")):
            with self.subTest(stamp=bad):
                altered = copy.deepcopy(report)
                altered["data"]["actual_redemption_confirmed_at"] = bad
                with self.assertRaisesRegex(ValueError, message):
                    ledger.apply_event(state, altered)

    def test_simulation_emits_source_confirmation_and_avoids_after_cutoff_resubmission(self):
        state, request = self.reserved()
        events = execution.simulate_events(state, [request], {"000001": "1"}, "2030-01-07T18:00:00+08:00",
            price_dates={"000001": "2030-01-06"})
        self.assertEqual(events[0]["data"]["actual_redemption_confirmed_at"], "2030-01-07T00:00:00+08:00")
        self.assertEqual(Decimal(events[0]["data"]["fee"]), Decimal("15"))
        after = ledger.apply_event(state, events[0])
        self.assertFalse(any(row["kind"] in ("fee_deviation", "expected_quote_unknown") for row in after["reconciliation"]))

    def test_simulated_late_confirmation_does_not_leak_to_another_order(self):
        state, request = self.reserved()
        state = copy.deepcopy(state)
        state["cash"] = "100"
        request["fee_contract"]["confirmation"]["lag_days"] = 3
        state["orders"][request["order_id"]]["fee_contract"] = request["fee_contract"]
        buy = order("buy", "100")
        buy["order_id"] = "buy-independent"
        buy["fee_contract"] = terms("000001")
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": buy}, T1))
        events = execution.simulate_events(state, [request, buy], {"000001": "1"}, "2030-01-06T08:00:00+08:00",
            price_dates={"000001": "2030-01-06"})
        self.assertEqual(events[0]["data"]["actual_redemption_confirmed_at"], "2030-01-09T00:00:00+08:00")
        self.assertEqual(events[1]["known_at"], "2030-01-06T08:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
