"""Independent cash arithmetic goldens and original disclosure interval inputs."""
import copy
import datetime as dt
import unittest
from decimal import Decimal

import cash_reference as reference
import fee_contract
from contracts import EvidenceError
from test_document_fee_formats import OriginalDocumentFeeTests


class ContractReferenceModeTests(unittest.TestCase):
    @staticmethod
    def terms():
        calendar = {"id": "analytical-example", "timezone": "Asia/Shanghai", "coverage_start": "2030-01-01",
            "coverage_end": "2030-01-10", "open_dates": ["2030-01-"+str(day).zfill(2) for day in range(1, 11)],
            "evidence_ref": {"fixture": "explicit analytical calendar, not a live source claim"}}
        return {"subscription": {"kind": "percentage", "rate": "0"}, "redemption": {"kind": "percentage", "rate": "0.015"},
            "trade_precision": {"money_step": "0.01", "share_step": "0.01", "fee_rounding": "unrounded",
                                "share_rounding": "down_fund", "redemption_net_rounding": "down_fund"},
            "execution_calendar": calendar, "order_cutoff_local": "15:00",
            "confirmation": {"lag_days": 0, "day_basis": "trading_days", "calendar": calendar},
            "settlement": {"lag_days": 1, "day_basis": "calendar_days"},
            "holding": {"minimum_days": 0, "day_basis": "calendar_days", "start_inclusive": True,
                        "end_inclusive": False, "end_event": "execution_date"}}

    def test_explicit_money_inclusions_and_holding_boundaries(self):
        rule = {"kind": "amount_tiers", "bands": [
            {"minimum": "0", "minimum_inclusive": True, "maximum": "1000000", "maximum_inclusive": True, "fee": {"kind": "fixed", "amount": "1"}},
            {"minimum": "1000000", "minimum_inclusive": False, "maximum": None, "maximum_inclusive": False, "fee": {"kind": "fixed", "amount": "2"}}]}
        self.assertEqual([reference.selected_rule(rule, Decimal(value))["amount"] for value in ("999999.99", "1000000", "1000000.01")], ["1", "1", "2"])

    def test_down_fund_entry_consumes_full_cash_and_preserves_surplus(self):
        quote = reference.entry_quote("100", self.terms(), "3")
        self.assertEqual(quote, {"shares": Decimal("33.33"), "gross": Decimal("100"), "fee": Decimal("0"),
            "debit": Decimal("100"), "refund": Decimal("0"), "share_surplus": Decimal("0.01")})

    def test_net_down_exit_preserves_exact_gross_and_fee_components(self):
        details = reference.exit_details("1.005", self.terms(), acquired_at="2029-12-01T00:00:00+08:00", submitted_at="2030-01-01T08:00:00+08:00")
        self.assertEqual(details, {"gross": Decimal("1.005"), "contractual_fee": Decimal("0.015075"),
            "rounding_loss": Decimal("0.009925"), "net": Decimal("0.98"), "effective_cost": Decimal("0.025")})
        self.assertEqual(reference.exit_fee("1.005", self.terms(), acquired_at="2029-12-01T00:00:00+08:00", submitted_at="2030-01-01T08:00:00+08:00"), Decimal("0.025"))

    def test_reconcile_rejects_old_prerounded_sale_and_changed_fee_components(self):
        terms = self.terms()
        context = {"as_of": "2030-01-01", "decision_at": "2030-01-01T08:00:00+08:00", "fee_contracts": {"C": terms},
            "snapshot": {"available_cash": "0", "reserved_cash": "0", "unsettled_cash": "0",
                         "positions": [{"lot_id": "old", "code": "C", "shares": "1", "reserved_shares": "0", "acquired_at": "2029-12-01T00:00:00+08:00"}]}}
        policy = {"proposed_contribution": "0", "current_action": {"buys": [], "sells": [{"lot_id": "old", "shares": "1"}]}}
        path = {"nav": {"2030-01-01": {"C": "1.005"}}, "distributions": []}
        rows = [{"kind": "price_sell", "date": "2030-01-01", "code": "C", "lot_id": "old", "shares": "1", "nav": "1.005",
            "gross": "1.005", "fee": "0.025", "contractual_fee": "0.015075", "rounding_loss": "0.009925",
            "receivable": "0.98", "pay_date": "2030-01-02", "confirmation_date": "2030-01-01", "cash_available_now": False,
            "fee_scope": "source_estimated_execution_cost"}, {"kind": "credit_sale", "date": "2030-01-02", "code": "C", "amount": "0.98",
            "available_before": "0", "available_delta": "0.98", "reserved_delta": "0", "available_after": "0.98", "reserved_after": "0"}]
        balances = {"cash": "0.98", "reserved_cash": "0", "receivables": "0"}
        self.assertEqual(reference.reconcile(context, policy, path, rows, "2030-01-02", balances)["fees"], Decimal("0.025"))
        forged = copy.deepcopy(rows)
        forged[0].update(gross="1.01", fee="0.02", receivable="0.99")
        with self.assertRaisesRegex(EvidenceError, "sale fee/net"):
            reference.reconcile(context, policy, path, forged, "2030-01-02", balances)
        forged = copy.deepcopy(rows)
        forged[0]["contractual_fee"] = "0.025"
        with self.assertRaisesRegex(EvidenceError, "contractual_fee"):
            reference.reconcile(context, policy, path, forged, "2030-01-02", balances)

    def test_age_only_cash_net_goldens_need_no_future_calendar(self):
        terms = self.terms()
        terms.pop("execution_calendar")
        expected = {"gross": Decimal("1.005"), "contractual_fee": Decimal("0.015075"),
            "rounding_loss": Decimal("0.009925"), "net": Decimal("0.98"), "effective_cost": Decimal("0.025")}
        self.assertEqual(reference.exit_details_at_age("1.005", terms, 7), expected)
        self.assertEqual(fee_contract.quote_exit_at_age_details("1.005", terms, 7), expected)
        terms["trade_precision"].update(fee_rounding="half_up", redemption_net_rounding="half_up")
        terms["redemption"] = {"kind": "percentage", "rate": "0"}
        self.assertEqual(reference.exit_details_at_age("1.005", terms, 7)["net"], Decimal("1.01"))
        self.assertEqual(fee_contract.quote_exit_at_age_details("1.005", terms, 7)["net"], Decimal("1.01"))

    def test_actual_ledger_fee_and_reported_net_are_never_replaced_by_model_cost(self):
        import ledger
        from test_ledger import opening, event, order, T1, T2, T3
        state, _ = opening("0", "1")
        request = order("sell", "1")
        request["fee_contract"] = self.terms()
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": request}, T1))
        def fill(fee, net, identity):
            data = {"order_id": "order-one", "fill_id": identity, "shares": "1", "price": "1.005",
                    "price_date": T2[:10], "gross_amount": "1.005", "fee": fee, "net_amount": net,
                    "settlement_at": T3, "final": True}
            return {"id": identity, "type": "sell_fill", "sequence": state["sequence"]+1,
                    "effective_at": T2, "known_at": T2, "recorded_at": T2, "data": data}
        actual = fill("0.015075", "0.98", "correct")
        after = ledger.apply_event(state, actual)
        self.assertEqual(actual["data"]["fee"], "0.015075")
        self.assertEqual(after["receivables"]["correct"]["amount"], "0.98")
        self.assertFalse(any(row["kind"] in ("fee_deviation", "redemption_component_reconciliation") for row in after["reconciliation"]))
        actual = fill("0.02", "0.97", "reported-difference")
        after = ledger.apply_event(state, actual)
        self.assertEqual(actual["data"]["fee"], "0.02")
        self.assertEqual(after["receivables"]["reported-difference"]["amount"], "0.97")
        self.assertTrue(any(row["kind"] == "fee_deviation" for row in after["reconciliation"]))
        self.assertTrue(any(row["kind"] == "redemption_component_reconciliation" for row in after["reconciliation"]))


class SourceHoldingAgeTests(unittest.TestCase):
    def test_no_age_fee_schedule_uses_only_source_minimum(self):
        for rule in ({"kind": "percentage", "rate": "0.01"}, {"kind": "fixed", "amount": "1"},
                     {"kind": "amount_tiers", "bands": [{"minimum": "0", "fee": {"kind": "percentage", "rate": "0.01"}}]}):
            with self.subTest(rule=rule):
                terms = {"holding": {"minimum_days": 400}, "redemption": rule}
                self.assertEqual(fee_contract.source_holding_ages(terms), [400])

    def test_decimal_implicit_boundaries_and_source_lock_keep_integer_neighbors(self):
        terms = {"holding": {"minimum_days": 30}, "redemption": {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7.5", "fee": {"kind": "percentage", "rate": "0.005"}},
            {"minimum": "365", "fee": {"kind": "percentage", "rate": "0"}}]}}
        self.assertEqual(fee_contract.source_holding_ages(terms), [0, 1, 7, 8, 29, 30, 31, 364, 365, 366])
        self.assertEqual([fee_contract.charge("10000", terms["redemption"], age=age) for age in (7, 8, 364, 365)],
                         [Decimal("150.00"), Decimal("50.00"), Decimal("50.00"), Decimal("0.00")])

    def test_explicit_inclusions_and_discrete_endpoints_preserve_fee_changes(self):
        continuous = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "minimum_inclusive": True, "maximum": "7", "maximum_inclusive": True,
             "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7", "minimum_inclusive": False, "maximum": "365", "maximum_inclusive": True,
             "fee": {"kind": "percentage", "rate": "0.005"}},
            {"minimum": "365", "minimum_inclusive": False, "maximum": None, "maximum_inclusive": False,
             "fee": {"kind": "percentage", "rate": "0"}}]}
        discrete = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "minimum_inclusive": True, "maximum": "6", "maximum_inclusive": True,
             "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7", "minimum_inclusive": True, "maximum": "364", "maximum_inclusive": True,
             "fee": {"kind": "percentage", "rate": "0.005"}},
            {"minimum": "365", "minimum_inclusive": True, "maximum": None, "maximum_inclusive": False,
             "fee": {"kind": "percentage", "rate": "0"}}]}
        for rule, ages, expected in ((continuous, (7, 8, 365, 366), ("150.00", "50.00", "50.00", "0.00")),
                                     (discrete, (6, 7, 364, 365), ("150.00", "50.00", "50.00", "0.00"))):
            with self.subTest(rule=rule):
                terms = {"holding": {"minimum_days": 0}, "redemption": rule}
                self.assertTrue(set(ages) <= set(fee_contract.source_holding_ages(terms)))
                self.assertEqual([fee_contract.charge("10000", rule, age=age) for age in ages],
                                 [Decimal(amount) for amount in expected])


class StrategyInvestorScopeTests(unittest.TestCase):
    def test_nonretail_held_cash_scope_is_blocked_without_changing_financial_facts(self):
        from test_strategy_execution import opening, market, spec, build_context, T1
        from contracts import fingerprint
        for scope in ("personal_pension", "unknown"):
            with self.subTest(scope=scope):
                state, _ = opening("0", "1")
                original = fingerprint(state)
                reference = market()
                row = reference["terms"][0]
                row["fee_contract"]["subject"]["investor_type"] = scope
                row["investor_scope"] = scope
                row["fee_contract_hash"] = fingerprint(row["fee_contract"])
                context = build_context(spec(), state, T1, reference)
                self.assertTrue(context["blocked"])
                self.assertEqual(context["snapshot"]["positions"][0]["code"], "000001")
                self.assertEqual(context["term_gaps"]["000001"]["investor_scope"], scope)
                self.assertNotIn("000001", context["fee_contracts"])
                self.assertEqual(fingerprint(state), original)

    def test_investor_tag_cannot_replace_source_subject_applicability(self):
        from test_strategy_execution import opening, market, spec, build_context, T1
        state, _ = opening()
        reference = market()
        reference["terms"][0]["investor_scope"] = "personal_pension"
        with self.assertRaisesRegex(ValueError, "Investor scope projection changed"):
            build_context(spec(), state, T1, reference)


class OriginalReferenceIntervalTests(OriginalDocumentFeeTests):
    def test_reference_selects_original_channel_money_and_age_intervals(self):
        terms = self.results["ef"]["normalized"]
        self.assertEqual([reference.selected_rule(terms["subscription"], Decimal(value))["rate"]
            for value in ("999999.99", "1000000", "1000000.01")], ["0.0012", "0.0008", "0.0008"])
        self.assertEqual([reference.selected_rule(terms["redemption"], Decimal("10000"), Decimal(age))["rate"]
            for age in (6, 7, 8)], ["0.015", "0.005", "0.005"])


if __name__ == "__main__":
    unittest.main()
