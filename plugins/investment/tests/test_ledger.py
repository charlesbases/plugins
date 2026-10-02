import copy
import json
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import ledger
from contracts import fingerprint

T0, T1, T2, T3, T4 = [f"2030-01-0{i}T00:00:00Z" for i in range(1, 6)]


def event(state, kind, data, at=T1, known=None, identity=None):
    # These synthetic fixtures explicitly use same-day NAV, ownership and
    # holding start; production confirmations have no such defaults.
    data = copy.deepcopy(data)
    if kind in ("opening", "valuation"):
        data["price_dates"] = {code: at[:10] for code in data["prices"]}
    if kind in ("buy_fill", "sell_fill", "subscription_confirmed"):
        data["price_date"] = at[:10]
        gross = Decimal(data["shares"]) * Decimal(data["price"])
        data["gross_amount"] = str(gross)
        if kind != "sell_fill":
            data.update(holding_started_at=at, ownership_at=at)
            data["cash_debit"] = str(gross + Decimal(data["fee"]))
        else:
            data["net_amount"] = str(gross - Decimal(data["fee"]))
    if kind == "cashflow":
        data = {"transfer_id": identity or f"flow-{state['sequence']+1}", "revision_id": identity or f"revision-{state['sequence']+1}",
                "previous_revision": None, "valuation": data.get("valuation"), **data}
    if kind in ("settlement", "dividend_paid"):
        data["amount"] = state["receivables"][data["receivable_id"]]["amount"]
    return {"id": identity or f"event-{state['sequence']+1}", "type": kind, "sequence": state["sequence"]+1,
            "effective_at": at, "known_at": known or at, "recorded_at": known or at, "data": data}


def opening(cash="1000", shares="0"):
    state = ledger.initial_state("CNY")
    lots = [] if shares == "0" else [{"lot_id": "old", "code": "000001", "shares": shares, "acquired_at": T0,
                                      "ownership_at": T0, "price_date": T0[:10]}]
    first = event(state, "opening", {"cash": cash, "lots": lots, "prices": {"000001": "10", "000002": "10"}}, T0)
    return ledger.apply_event(state, first), first


def order(side="buy", value="600"):
    return {"order_id": "order-one", "code": "000001", "side": side, "currency": "CNY", "lot_id": "old" if side == "sell" else None,
            "cash_limit": value if side == "buy" else "0", "share_limit": value if side == "sell" else "0", "fee_rate": "0",
            "settlement_days": 1, "share_step": "0.00000001", "context_hash": "a"*64}


class LedgerTests(unittest.TestCase):
    def test_cashflow_unitization_preserves_return(self):
        state, _ = opening("0", "10")
        state = ledger.apply_event(state, event(state, "valuation", {"prices": {"000001": "15"}}))
        state = ledger.apply_event(state, event(state, "cashflow", {"amount": "30", "valuation": {
            "at": T1, "prices": {"000001": "15"}, "price_dates": {"000001": T1[:10]}, "evidence_refs": [{"synthetic": "fixture"}]}}))
        snap = ledger.snapshot(state, T1)
        self.assertEqual(snap["units"], "120")
        self.assertEqual(snap["equity"], "180")
        self.assertEqual(snap["unit_nav"], "1.5")
        self.assertEqual(ledger.observe(state, T1, {}, "1")["net_return"], "0.5")

    def test_partial_fill_and_cancel_only_release_confirmed_reservation(self):
        state, _ = opening()
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order()}))
        self.assertEqual(ledger.snapshot(state, T1)["available_cash"], "400")
        state = ledger.apply_event(state, event(state, "buy_fill", {"order_id": "order-one", "fill_id": "part1", "lot_id": "new",
            "shares": "20", "price": "10", "fee": "0", "final": False}, T2))
        state = ledger.apply_event(state, event(state, "cancel_requested", {"order_id": "order-one"}, T2))
        self.assertEqual(ledger.snapshot(state, T2)["available_cash"], "400")
        state = ledger.apply_event(state, event(state, "cancel_confirmed", {"order_id": "order-one"}, T3))
        snap = ledger.snapshot(state, T3)
        self.assertEqual((snap["cash"], snap["reserved_cash"], snap["equity"]), ("800", "0", "1000"))

    def test_receivable_is_not_buying_power_before_settlement(self):
        state, _ = opening("0", "100")
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order("sell", "30")}))
        state = ledger.apply_event(state, event(state, "sell_fill", {"order_id": "order-one", "fill_id": "sale", "shares": "10",
            "price": "10", "fee": "0", "settlement_at": T3, "final": False}, T2))
        self.assertEqual(ledger.snapshot(state, T2)["unsettled_cash"], "100")
        buy = {**order(value="1"), "order_id": "buy-two"}
        with self.assertRaisesRegex(ValueError, "settled free cash"):
            ledger.apply_event(state, event(state, "order_reserved", {"order": buy}, T2))
        early = ledger.apply_event(state, event(state, "settlement", {"receivable_id": "sale"}, T2))
        self.assertEqual(early["cash"], "100")
        self.assertEqual(early["reconciliation"][-1]["kind"], "settlement_time_deviation")
        state = ledger.apply_event(state, event(state, "settlement", {"receivable_id": "sale"}, T3))
        self.assertEqual(ledger.snapshot(state, T3)["available_cash"], "100")
        self.assertEqual(ledger.snapshot(state, T3)["equity"], "1000")

    def test_snapshot_preserves_source_identity_and_date_only_arrival(self):
        state, _ = opening("0", "10")
        declared = event(state, "dividend_declared", {"distribution_id": "div", "code": "000001",
            "per_share": "1", "pay_at": T3[:10], "record_at": T0, "entitled_shares": "10"})
        state = ledger.apply_event(state, declared)
        snap = ledger.snapshot(state, T1)
        self.assertEqual(snap["receivables"], [{"receivable_id": "div", "source_identity": "div",
            "kind": "dividend", "amount": "10", "due_at": T3[:10], "code": "000001",
            "record_at": T0, "entitled_shares": "10"}])
        self.assertEqual((snap["cash"], snap["available_cash"], snap["unsettled_cash"]), ("0", "0", "10"))
        settled = ledger.apply_event(state, event(state, "dividend_paid", {"receivable_id": "div"}, T3))
        self.assertEqual(ledger.snapshot(settled, T3)["receivables"], [])
        self.assertFalse(any(row["kind"] == "settlement_time_deviation" for row in settled["reconciliation"]))

    def test_snapshot_blocks_unknown_arrival_and_rejects_corrupted_amount(self):
        state, _ = opening()
        state["receivables"]["unknown"] = {"kind": "redemption", "amount": "10", "due_at": None}
        snap = ledger.snapshot(state, T1)
        self.assertIn("receivable_arrival_unknown:unknown", snap["reasons"])
        self.assertTrue(snap["blocked"])
        state["receivables"]["unknown"]["amount"] = "-1"
        with self.assertRaisesRegex(ValueError, "permitted minimum"):
            ledger.snapshot(state, T1)

    def test_existing_schema4_receivables_rebuild_and_confirm_without_new_metadata(self):
        state, first = opening("0", "10")
        declared = event(state, "dividend_declared", {"distribution_id": "div", "code": "000001",
            "per_share": "1", "pay_at": T3, "record_at": T0, "entitled_shares": "10"})
        legacy = ledger.apply_event(state, declared)
        legacy["receivables"] = {"div": {"kind": "dividend", "amount": "10", "due_at": T3,
            "code": "000001", "record_at": T0, "entitled_shares": "10"}}
        persisted = json.loads(json.dumps(legacy))
        self.assertEqual(ledger.rebuild(ledger.initial_state("CNY"), [first, declared], T1), persisted)
        before = fingerprint(persisted)
        ledger.snapshot(persisted, T1)
        self.assertEqual(fingerprint(persisted), before)
        confirmation = event(persisted, "dividend_paid", {"receivable_id": "div"}, T3)
        updated = ledger.apply_event(persisted, confirmation)
        rebuilt = ledger.rebuild(ledger.initial_state("CNY"), [first, declared, confirmation], T3)
        self.assertEqual(updated, rebuilt)
        self.assertEqual((updated["cash"], updated["receivables"]), ("10", {}))

    def test_pending_subscription_is_unknown_then_confirmed_without_zero_fill(self):
        state, _ = opening()
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order()}))
        state = ledger.apply_event(state, event(state, "subscription_pending", {"order_id": "order-one", "payment_id": "paid", "amount": "500"}, T2))
        self.assertTrue(ledger.snapshot(state, T2)["blocked"])
        state = ledger.apply_event(state, event(state, "cashflow", {"amount": "100"}, T2))
        state = ledger.apply_event(state, event(state, "subscription_confirmed", {"payment_id": "paid", "fill_id": "confirmed", "lot_id": "new",
            "shares": "50", "price": "10", "fee": "0", "final": True}, T3))
        snap = ledger.snapshot(state, T3)
        self.assertFalse(snap["blocked"])
        self.assertEqual((snap["equity"], snap["unit_nav"], snap["cash"]), ("1100", None, "600"))
        self.assertFalse(snap["performance_exact"])

    def test_dividend_and_split_preserve_total_wealth(self):
        state, _ = opening("0", "10")
        state = ledger.apply_event(state, event(state, "dividend_declared", {"distribution_id": "div", "code": "000001", "per_share": "1", "pay_at": T3,
            "record_at": T0, "entitled_shares": "10"}))
        state = ledger.apply_event(state, event(state, "valuation", {"prices": {"000001": "9"}}))
        self.assertEqual(ledger.snapshot(state, T1)["unit_nav"], "1")
        state = ledger.apply_event(state, event(state, "dividend_paid", {"receivable_id": "div"}, T3))
        state = ledger.apply_event(state, event(state, "split", {"code": "000001", "ratio": "2"}, T3))
        snap = ledger.snapshot(state, T3)
        self.assertEqual((snap["equity"], snap["unit_nav"], snap["positions"][0]["shares"]), ("100", "1", "20"))

    def test_late_confirmation_rebuild_preserves_source_and_both_cutoffs(self):
        state, first = opening()
        reserve = event(state, "order_reserved", {"order": order(value="200")}, T1)
        state = ledger.apply_event(state, reserve)
        mark = event(state, "valuation", {"prices": {"000001": "20"}}, T3)
        state = ledger.apply_event(state, mark)
        fill = event(state, "buy_fill", {"order_id": "order-one", "fill_id": "late", "lot_id": "new", "shares": "20", "price": "10", "fee": "0", "final": True}, T2, T4)
        with self.assertRaises(ledger.RebuildRequired):
            ledger.apply_event(state, fill)
        facts = [first, reserve, mark, fill]; before = copy.deepcopy(facts)
        rebuilt = ledger.rebuild(ledger.initial_state("CNY"), facts, T4)
        self.assertEqual(rebuilt["sequence"], 4)
        self.assertEqual(rebuilt["last_event"], {"id": fill["id"], "hash": fingerprint(fill)})
        self.assertEqual(ledger.snapshot(rebuilt, T4)["equity"], "1200")
        with self.assertRaisesRegex(ValueError, "Historical snapshot"):
            ledger.snapshot(rebuilt, T3)
        prior = ledger.rebuild(ledger.initial_state("CNY"), facts, T3)
        self.assertEqual(ledger.snapshot(prior, T3)["equity"], "1000")
        historical = ledger.rebuild(ledger.initial_state("CNY"), facts, T4, T2)
        self.assertEqual(ledger.snapshot(historical, T4)["equity"], "1000")
        with self.assertRaisesRegex(ValueError, "observation-only"):
            ledger.apply_event(historical, event(historical, "valuation", {"prices": {}}, T4))
        self.assertEqual(facts, before)

    def test_duplicate_conflict_unknown_and_read_only_inputs(self):
        state, _ = opening(); original = copy.deepcopy(state)
        unknown = event(state, "unknown", {"reason": "confirmation_missing"})
        changed = ledger.apply_event(state, unknown)
        self.assertEqual(state, original)
        self.assertEqual(ledger.apply_event(changed, unknown), changed)
        with self.assertRaisesRegex(ValueError, "content conflict"):
            ledger.apply_event(changed, {**unknown, "data": {"reason": "different"}})
        self.assertTrue(ledger.snapshot(changed, T1)["blocked"])
        resolved = ledger.apply_event(changed, event(changed, "resolve_unknown", {"unknown_id": unknown["id"],
            "evidence_ref": {"sha256": "a"*64, "size": 1, "path": "objects/fixture"},
            "resolution": "confirmed no unrecorded trade"}, T2))
        self.assertEqual(ledger.snapshot(resolved, T2)["equity"], "1000")
        self.assertFalse(ledger.snapshot(resolved, T2)["blocked"])


if __name__ == "__main__":
    unittest.main()
