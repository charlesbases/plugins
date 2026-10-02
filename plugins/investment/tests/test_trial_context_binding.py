"""Trial context mathematics and binding only; no future trial result claims."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))

import pipeline
import trial
from test_ledger import opening, T0, T1
from test_strategy_execution import spec, market, risk, build_context


class TrialContextBindingTests(unittest.TestCase):
    def case(self, dividend=False):
        state, _ = opening()
        reference = market()
        record = {"market_ref": reference, "evidence_refs": [],
                  "provenance": {"action_inventory": {"by_code": {}}}}
        if dividend:
            record["evidence_refs"] = [{"role": "public_source", "source_id": "synthetic-action-document",
                                        "artifact": {"sha256": "d"*64}}]
            record["provenance"]["action_inventory"]["by_code"]["000001"] = {
                "known_at": T0, "source_id": "synthetic-action-document", "source_sha256": "d"*64,
                "dividends": [{"date": "2030-01-03", "record_date": "2030-01-02", "cash_payment_date": "2030-01-04",
                    "distribution_per_share": 0.1, "currency": "CNY",
                    "rights_rule": {"subscribe_on_record_date": "excluded", "redeem_on_record_date": "included"}}]}
        binding = {"bundle_hash": "a"*64, "news_ref": "b"*64, "event_frontier_hash": "c"*64}
        resolved = risk(state, T1, reference)
        context = build_context(spec(), state, T1, reference, risk_state=resolved,
                                industry_data_ref=binding, known_future_actions=pipeline.future_actions(record, T1))
        trade_inputs = {"trade_state": context["trade_state"], "trade_family_review_index": context["trade_family_review_index"]}
        return state, record, resolved, context, trade_inputs

    def test_rebuild_preserves_frozen_industry_and_authoritative_trade_state(self):
        state, record, resolved, context, inputs = self.case()
        before = copy.deepcopy(context)
        actual = trial._rebuild_context(context["spec"], state, record, context, resolved, inputs, account_id="main")
        self.assertEqual(actual, context)
        self.assertEqual(actual["industry_data_ref"], {"bundle_hash": "a"*64, "news_ref": "b"*64, "event_frontier_hash": "c"*64})
        self.assertEqual(actual["trade_state"], inputs["trade_state"])
        self.assertEqual(context, before)

    def test_rebuild_uses_true_future_action_reader_before_hashing(self):
        state, record, resolved, context, inputs = self.case(dividend=True)
        actual = trial._rebuild_context(context["spec"], state, record, context, resolved, inputs, account_id="main")
        self.assertEqual(actual["known_future_actions"][0]["per_share"], "0.1")
        self.assertEqual(actual["known_future_actions"][0]["ex_date"], "2030-01-03")
        self.assertEqual(actual["context_hash"], context["context_hash"])

    def test_rebuild_preserves_nonempty_family_and_source_exposure_bindings(self):
        from contracts import fingerprint
        state, record, resolved, original, inputs = self.case()
        context = build_context(original["spec"], state, original["decision_at"], record["market_ref"],
            risk_state=resolved, industry_data_ref=original["industry_data_ref"],
            readiness_ref={"explicit_engineering_readiness": "bound"}, family_id="registered-source-family",
            sector_exposure_bounds={"000001": {"sector-a": {"upper": 1.2}}},
            paired_currency_validation={"schema_version": 1, "frames": {}})
        actual = trial._rebuild_context(context["spec"], state, record, context, resolved, inputs, account_id="main")
        self.assertEqual(actual, context)
        self.assertEqual(actual["context_hash"], fingerprint({k: v for k, v in context.items() if k != "context_hash"}))


if __name__ == "__main__":
    unittest.main()
