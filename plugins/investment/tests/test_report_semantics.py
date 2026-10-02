"""Source-clock/report counterexamples; isolated engineering, not market proof."""
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import test_single_step as fixture
import execution
import portfolio_mpc
import report
import report_validation
import verify
from artifacts import Artifacts


def envelope_case(*, multiple=False):
    context = fixture.make_context(cash="0", shares="1000")
    context.update(decision_at="2030-01-07T12:00:00+08:00", as_of="2030-01-07")
    context["market_ref"]["price_dates"]["C"] = "2030-01-06"
    lot = context["snapshot"]["positions"][0]
    lot["acquired_at"] = "2030-01-01T00:00:00+08:00"
    terms = context["fee_contracts"]["C"]
    terms["holding"]["end_event"] = "confirmation_date"
    terms["redemption"] = {"kind": "holding_tiers", "bands": [
        {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
        {"minimum": "7", "fee": {"kind": "percentage", "rate": "0.005"}}]}
    terms.update(redemption_allocation={"method": "fifo"}, redemption_fee_application="per_lot",
        redemption_fee_application_source={"engineering_original": "each_batch"},
        redemption_rounding_application="per_lot", redemption_rounding_application_source={"engineering_original": "each_batch"})
    quantities = [("old", "1000")]
    if multiple:
        lot["shares"] = "3"
        new = {**lot, "lot_id": "new", "shares": "5", "acquired_at": "2030-01-02T00:00:00+08:00"}
        context["snapshot"]["positions"].append(new)
        context["snapshot"]["equity"] = "8"
        terms.update(minimum_redemption_shares="4", minimum_remaining_shares="2")
        quantities = [("old", "3"), ("new", "1")]
    principal = Decimal(context["snapshot"]["equity"])
    context["risk_state"] = {"net_principal": str(principal), "loss_tolerance": "0.25",
        "principal_floor": str(principal * Decimal("0.75")),
        "remaining_loss_budget": str(principal * Decimal("0.25")), "valid_until": None}
    action = {"buys": [], "sells": [{"lot_id": identity, "shares": shares} for identity, shares in quantities]}
    orders = [{"order_id": "engineering-"+identity, "side": "sell", "code": "C", "lot_id": identity,
               "share_limit": shares, "cash_limit": "0", "currency": "CNY"} for identity, shares in quantities]
    value = portfolio_mpc.reference_valuation(context, action)
    return context, orders, value


def bundle_case(objects, context, orders, value):
    state = {"lots": {row["lot_id"]: row for row in context["snapshot"]["positions"]}}
    instruction = {"orders": orders, "redemption_requests": execution._redemption_requests(orders),
                   "reference_valuation": value, "selected_plan_kind": "rolling_mpc_current_action"}
    bundle = {"context": context, "account_state_ref": objects.put_json(state),
        "calculation_ref": objects.put_json({}), "selection_ref": None, "orders": instruction,
        "news": {"claims": [{"id": "engineering-news", "kind": "inference", "text": "工程来源影响说明",
            "categories": ["工程品类"], "direction": "watch", "counterevidence": [{"missing_reason": "工程反证资料待核"}]}]},
        "decision_at": context["decision_at"], "account_recorded_at": context["decision_at"],
        "market_price_dates": context["market_ref"]["price_dates"], "account_id": "engineering-only",
        "status": "conditional_research", "product_names": {"C": "工程基金"}, "comparison_summary": {},
        "qualification": {"reason": "engineering only, no market prediction or account qualification"},
        "decision_id": "engineering-report", "bundle_hash": "engineering"}
    bundle["action_analysis"] = report.explain_actions(bundle, objects)
    return bundle


class SourceReportSemanticsTests(unittest.TestCase):
    def test_confirmation_fee_reference_is_five_not_fifteen(self):
        context, orders, value = envelope_case()
        report_validation.reference_valuation_invariants(context, orders, value)
        with tempfile.TemporaryDirectory(prefix="investment-report-proof-") as folder:
            bundle = bundle_case(Artifacts(Path(folder)), context, orders, value)
            detail = bundle["action_analysis"]["actions"][0]
            self.assertEqual(float(detail["estimated_fee"]), 5.)
            self.assertEqual(float(detail["funding"]["estimated_receivable"]), 995.)
            self.assertEqual(detail["reference_valuation"]["normal_confirmation_date"], "2030-01-08")

    def test_product_minimum_is_one_public_request_and_counterevidence_is_visible(self):
        context, orders, value = envelope_case(multiple=True)
        with tempfile.TemporaryDirectory(prefix="investment-report-proof-") as folder:
            bundle = bundle_case(Artifacts(Path(folder)), context, orders, value)
            text = report.render(bundle)
            self.assertEqual(text.count("：卖出 "), 1)
            self.assertIn("：卖出 4 份。", text)
            self.assertIn("工程反证资料待核", text)
            self.assertEqual(verify.verify_report(bundle, text)["semantic_validation"]["status"], "passed")

    def test_resealed_normal_confirmation_or_fee_is_rejected_independently(self):
        context, orders, value = envelope_case()
        for changed in ("normal_confirmation_date", "contractual_fee"):
            corrupt = copy.deepcopy(value)
            corrupt["legs"][0][changed] = "2030-01-07" if changed.endswith("date") else "15"
            with self.subTest(field=changed), self.assertRaises(ValueError):
                report_validation.reference_valuation_invariants(context, orders, corrupt)

    def test_product_allocation_and_readable_instruction_tampering_is_rejected(self):
        context, orders, value = envelope_case(multiple=True)
        with tempfile.TemporaryDirectory(prefix="investment-report-proof-") as folder:
            bundle = bundle_case(Artifacts(Path(folder)), context, orders, value)
            text = report.render(bundle)
            with self.assertRaisesRegex(ValueError, "product-level"):
                report_validation.report_semantics(bundle, text.replace("：卖出 4 份。", "：卖出 3 份。"))
            with self.assertRaisesRegex(ValueError, "counterevidence"):
                report_validation.report_semantics(bundle, text.replace("工程反证资料待核", ""))
            changed = copy.deepcopy(bundle["orders"])
            changed["redemption_requests"][0]["shares"] = "5"
            with self.assertRaisesRegex(ValueError, "public redemption total"):
                report_validation.public_request_invariants(changed)


if __name__ == "__main__":
    unittest.main()
