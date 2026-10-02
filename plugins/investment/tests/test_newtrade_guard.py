"""Current policy/state boundaries; synthetic receipts are engineering only."""
import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import ledger
import newtrade_guard as guard
from contracts import ContractError, fingerprint

NOW = "2030-01-01T08:00:00Z"


def policy():
    p = Path(__file__).resolve().parents[1]/"skills/investment/references/strategy.example.json"
    value = json.loads(p.read_text(encoding="utf-8"))["trade_policy"]
    value["registration"] = "declared"
    return value


def executed(side="sell", fee="3"):
    at = "2029-12-31T08:00:00Z"
    return {"id": "receipt", "type": "external_fill_confirmed", "sequence": 1,
        "effective_at": at, "known_at": at, "recorded_at": at,
        "data": {"side": side, "code": "A", "fill_id": "actual-fill", "lot_id": "a", "shares": "100",
            "price": "1", "price_date": "2029-12-31", "gross_amount": "100", "fee": fee,
            "cash_amount": "97", "settlement_at": "2030-01-02T08:00:00Z",
            "holding_started_at": None, "ownership_at": None,
            "evidence_ref": {"path": "synthetic-receipt", "sha256": "a"*64, "size": 1}}}


class TradeStateContractTests(unittest.TestCase):
    def state(self, events=()):
        return guard.derive_state(ledger.initial_state("CNY"), list(events), NOW, 30)

    def test_latest_registered_policy_round_trips_without_mutation(self):
        p = policy(); original = copy.deepcopy(p)
        self.assertEqual(guard.validate_policy(p), original)
        self.assertEqual(p, original)

    def test_unknown_policy_method_cannot_grant_qualification(self):
        p = policy(); p["kind"] = "unsupported_method"
        with self.assertRaises(ContractError): guard.validate_policy(p)

    def test_fixed_review_family_requires_supported_source_settlement_rule(self):
        p = policy(); p["family"]["candidate_rule"] = "unregistered_rule"
        with self.assertRaises(ContractError): guard.validate_policy(p)

    def test_tail_sample_governance_must_be_explicit_positive_integer(self):
        for value in (None, 0, -1, "20", True):
            p = policy(); p["inference"]["minimum_tail_observations"] = value
            with self.assertRaises(ContractError): guard.validate_policy(p)

    def test_reversal_requires_more_net_advantage_than_ordinary_trade(self):
        p = policy()
        p["economic"]["minimum_reversal_advantage_amount"] = p["economic"]["minimum_net_advantage_amount"]
        with self.assertRaises(ContractError): guard.validate_policy(p)

    def test_actual_fees_deduplicate_same_source_receipt(self):
        receipt = executed()
        state = self.state([receipt, copy.deepcopy(receipt)])
        self.assertEqual(state["actual_window_fees"], "3")
        self.assertEqual(len(state["executed_fills"]), 1)
        guard.validate_state(state, "main", NOW, 30)

    def test_conflicting_receipt_identity_is_rejected(self):
        first = executed(); changed = executed(fee="4")
        with self.assertRaises(ContractError): self.state([first, changed])

    def test_late_receipt_does_not_rewrite_preknowledge_state(self):
        receipt = executed()
        receipt["known_at"] = receipt["recorded_at"] = "2030-01-02T08:00:00Z"
        state = self.state([receipt])
        self.assertEqual(state["actual_window_fees"], "0")
        self.assertEqual(state["last_executed_by_code"], {})

    def test_account_cutoff_and_window_bind_actual_fee_state(self):
        state = self.state([executed()])
        for account, at, window in (("other", NOW, 30), ("main", "2030-01-02T08:00:00Z", 30), ("main", NOW, 7)):
            with self.assertRaises(ContractError): guard.validate_state(state, account, at, window)

    def test_rehash_cannot_hide_fee_sum_inconsistency(self):
        state = self.state([executed()]); state["actual_window_fees"] = "0"
        state["state_hash"] = fingerprint({k:v for k,v in state.items() if k != "state_hash"})
        with self.assertRaises(ContractError): guard.validate_state(state, "main", NOW, 30)

    def test_execution_outside_window_keeps_direction_but_not_current_fee(self):
        receipt = executed()
        for key in ("known_at", "recorded_at", "effective_at"): receipt[key] = "2029-11-01T08:00:00Z"
        state = self.state([receipt])
        self.assertEqual(state["actual_window_fees"], "0")
        self.assertEqual(state["last_executed_by_code"]["A"]["side"], "sell")


if __name__ == "__main__":
    unittest.main()
