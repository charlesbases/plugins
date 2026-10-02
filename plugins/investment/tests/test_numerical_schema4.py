"""Independent cash/share and source-fee regression examples."""
import copy
import json
import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import fee_contract
import single_step_wealth
import allocation_runner
from contracts import fingerprint


def terms(code="B", rate="0", step="0.01"):
    calendar = {"id": "fixture", "timezone": "Asia/Shanghai", "coverage_start": "2030-01-01", "coverage_end": "2030-04-30",
                "open_dates": [(dt.date(2030, 1, 1)+dt.timedelta(days=i)).isoformat() for i in range(120)], "evidence_ref": {"fixture": "explicit_calendar"}}
    return {"subject": {"code": code, "share_class": "C", "currency": "CNY", "channel": "TT", "investor_type": "retail"},
            "subscription": {"kind": "percentage", "rate": rate}, "redemption": {"kind": "percentage", "rate": "0"},
            "holding": {"minimum_days": 0, "day_basis": "calendar_days", "start_inclusive": True, "end_inclusive": False, "end_event": "execution_date"},
            "trade_precision": {"money_step": "0.01", "share_step": step, "fee_rounding": "half_up", "share_rounding": "down_refund"},
            "settlement": {"lag_days": 2, "day_basis": "calendar_days"}, "execution_calendar": calendar,
            "acquisition_rule": {"holding_start": "execution_date", "ownership_start": "execution_date"},
            "minimum_redemption_shares": "0.01", "minimum_remaining_shares": "0", "min_buy": "1", "max_buy": None,
            "buyable": True, "sellable": True, "confirmation_max_calendar_days": 1}


def context(cash="100", shares="10", prices=None, budget="10"):
    prices = prices or {"B": "10", "C": "60"}
    contracts = {code: terms(code) for code in prices}
    positions = [{"lot_id": "old", "code": "B", "shares": shares, "reserved_shares": "0", "acquired_at": "2029-01-01T00:00:00Z", "value": str(Decimal(shares)*Decimal(prices["B"]))}]
    equity = str(Decimal(cash)+Decimal(shares)*Decimal(prices["B"]))
    return {"schema_version": 4, "decision_at": "2030-01-01T00:00:00Z", "as_of": "2030-01-01", "universe": list(prices), "allocation_codes": list(prices),
            "purchase_eligible_codes": ["C"], "fee_contracts": contracts,
            "identities": {code: {"fund_group_id": code, "sector_exposures": None} for code in prices},
            "snapshot": {"cash": cash, "available_cash": cash, "reserved_cash": "0", "unsettled_cash": "0", "positions": positions, "prices": prices, "equity": equity},
            "risk_state": {"remaining_loss_budget": budget},
            "spec": {"allocation": {"tail_probability": .5}, "constraints": {"fund_group_limits": {}, "sector_limits": {}},
                     "planning": {"primary_horizon_days": 30, "primary_goal": "hold", "cash_deadline_days": None, "cash_required_amount": None}},
            "model_request": {"assets": [{"code": code, "sellable": True, "buyable": True, "buy_allowed": code == "C", "min_buy": 1, "max_weight": 1} for code in prices]}}


class ExactProjectionRegression(unittest.TestCase):
    def test_share_rounding_cannot_certify_a_missing_hedge(self):
        ctx = context(); ctx["fee_contracts"]["C"]["trade_precision"]["share_step"] = "1"
        distribution = {"codes": ["B", "C"], "returns": [[-.5, .5], [.5, -.1]], "probabilities": [.5, .5]}
        actual = single_step_wealth.project(ctx, {"buys": [{"code": "C", "cash_debit": 99.99}], "sells": []}, distribution)
        self.assertEqual(actual["buys"][0]["shares"], "1")
        self.assertEqual(Decimal(actual["cash"]), Decimal(40))
        self.assertEqual(actual["scenario_profits"], [-20, 44])
        self.assertEqual(actual["absolute_cvar"], 20)
        self.assertFalse(actual["risk_feasible"])

    def test_fee_cent_rounding_uses_confirmed_cost_not_continuous_ratio(self):
        quote = fee_contract.quote_entry("100", terms(rate="0.005"), price="1", share_step="0.01")
        self.assertEqual(quote, {"shares": Decimal("99.50"), "gross": Decimal("99.50"), "fee": Decimal("0.50"), "debit": Decimal("100.00"), "remainder": Decimal("0.00")})

    def test_fund_borne_share_rounding_does_not_invent_a_refund(self):
        rule = terms(); rule["trade_precision"]["share_rounding"] = "half_up_fund"
        quote = fee_contract.quote_entry("100", rule, price="3", share_step="0.01")
        self.assertEqual(quote["shares"], Decimal("33.33"))
        self.assertEqual(quote["debit"], Decimal(100))
        self.assertEqual(quote["remainder"], 0)

    def test_fine_share_precision_cannot_exploit_half_cent_money_rounding(self):
        quote = fee_contract.quote_entry("100", terms(step="0.00000001"), price="1", share_step="0.00000001")
        self.assertEqual(quote["shares"], Decimal(100))
        self.assertEqual(quote["gross"], Decimal(100))

    def test_group_limit_is_checked_across_share_codes(self):
        result = single_step_wealth.exposure_check({"A": 50, "C": 50}, 100,
                  {code: {"fund_group_id": "same", "sector_exposures": None} for code in ("A", "C")},
                  {"fund_group_limits": {"same": .5}, "sector_limits": {}})
        self.assertFalse(result["feasible"])
        self.assertEqual(result["fund_group_values"], {"same": 100})

    def test_terminal_unknown_dividend_fee_bound_is_not_actual_dividend_tax(self):
        rule = terms(); rule["redemption"] = {"kind": "percentage", "rate": ".01"}
        lower, upper = fee_contract.fee_bounds("10100", rule, [30])
        self.assertEqual((lower, upper), (Decimal(0), Decimal(101)))
        # With independently known NAV=10000 and cash dividend=100, the exact
        # charge is100 and wealth10000; the101 is only a valid conservative cap.
        self.assertEqual(fee_contract.quote_exit("10000", rule, acquired_at="2030-01-01T00:00:00Z", execution_at="2030-01-31T00:00:00Z"), 100)

    def test_fee_tier_supremum_checks_both_sides_of_discontinuity(self):
        rule = terms(); rule["redemption"] = {"kind": "amount_tiers", "bands": [{"minimum": "0", "fee": {"kind": "percentage", "rate": ".1"}}, {"minimum": "100", "fee": {"kind": "fixed", "amount": "1"}}]}
        self.assertEqual(fee_contract.fee_bounds("200", rule, [30])[1], Decimal("10.00"))

    def test_exit_quote_rounds_gross_before_selecting_amount_tier(self):
        rule = terms(); rule["redemption"] = {"kind": "amount_tiers", "bands": [{"minimum": "0", "fee": {"kind": "percentage", "rate": ".01"}}, {"minimum": "1000", "fee": {"kind": "fixed", "amount": "1"}}]}
        value = fee_contract.quote_exit(Decimal("333.33333333")*3, rule, acquired_at="2030-01-01T00:00:00Z", execution_at="2030-01-31T00:00:00Z")
        self.assertEqual(value, Decimal("1.00"))

    def test_new_lot_exit_bound_includes_later_confirmation_age(self):
        ctx = context(cash="100", shares="0", prices={"B": "1", "C": "1"}, budget="10")
        ctx["fee_contracts"]["C"]["redemption"] = {"kind": "holding_tiers", "bands": [{"minimum": "0", "fee": {"kind": "percentage", "rate": ".01"}}, {"minimum": "30", "fee": {"kind": "percentage", "rate": "0"}}]}
        dist = {"codes": ["B", "C"], "returns": [[0, 0]], "probabilities": [1.]}
        projection = single_step_wealth.project(ctx, {"buys": [{"code": "C", "cash_debit": 100}], "sells": []}, dist)
        bound = single_step_wealth.exit_bounds(ctx, projection, dist, 30)
        self.assertAlmostEqual(bound["fee_upper"][0], 1.005)
        self.assertAlmostEqual(bound["wealth_lower"][0], 98.995)


    def test_per_asset_dates_rebase_independently(self):
        fitted = {"forecasts": [{"code": code, "feature_cutoff_date": "2030-01-01"} for code in ("A", "B")],
                  "joint_scenarios": {"codes": ["A", "B"], "returns": [[.1, .1]], "targets": {name: [[1.1, 1.1]] for name in ("pricing", "terminal_nav", "hold", "buy", "sell")}, "probabilities": [1.]}}
        ctx = {"as_of": "2030-01-03", "market_ref": {"price_dates": {"A": "2030-01-01", "B": "2030-01-02"}, "prices": {"A": "1", "B": "2"}}}
        data = {"nav": {"A": [{"date": "2030-01-01", "nav": 1, "distribution_per_share": 0}],
                        "B": [{"date": "2030-01-01", "nav": 1, "distribution_per_share": 0}, {"date": "2030-01-02", "nav": 2, "distribution_per_share": 0}]}}
        result = allocation_runner._rebase_targets(copy.deepcopy(fitted), ctx, data)
        self.assertAlmostEqual(result["joint_scenarios"]["returns"][0][0], .1)
        self.assertAlmostEqual(result["joint_scenarios"]["returns"][0][1], -.45)







    def test_stale_manufacturing_disclosure_does_not_prove_no_appliances(self):
        identities = {"A": {"fund_group_id": "g", "sector_exposures": {"status": "measured_disclosure", "weights": {"manufacturing": 1}, "unmapped_weight": 0}}}
        result = single_step_wealth.exposure_check({"A": 100}, 100, identities, {"fund_group_limits": {}, "sector_limits": {"appliances": .2}})
        self.assertFalse(result["feasible"])
        self.assertNotIn("appliances", result["sector_upper_values"])
        self.assertEqual(result["unknown_sector_codes"], ["A:appliances"])
        self.assertEqual(result["violations"], ["unknown_sector_upper:A:appliances"])

    def test_partial_lot_sale_cannot_leave_an_illegal_product_residual(self):
        ctx = context(cash="0", shares="1", prices={"B": "1", "C": "1"})
        ctx["snapshot"]["positions"].append({**ctx["snapshot"]["positions"][0], "lot_id": "tiny", "shares": ".005"})
        ctx["fee_contracts"]["B"]["minimum_remaining_shares"] = ".01"
        with self.assertRaisesRegex(ValueError, "residual"):
            single_step_wealth.project(ctx, {"buys": [], "sells": [{"lot_id": "old", "shares": "1"}]}, {"codes": ["B", "C"], "returns": [[0,0]], "probabilities": [1.]})

    def test_cutoff_and_confirmation_use_source_open_days(self):
        rule = terms()
        rule["execution_calendar"]["open_dates"] = ["2030-01-02", "2030-01-03", "2030-01-07", "2030-01-08"]
        rule["order_cutoff_local"] = "15:00"
        rule["confirmation"] = {"lag_days": 1, "day_basis": "trading_days", "normal_conditions_only": True}
        self.assertEqual(str(fee_contract.pricing_date(rule, "2030-01-03T14:59:00+08:00")), "2030-01-03")
        self.assertEqual(str(fee_contract.pricing_date(rule, "2030-01-03T15:00:00+08:00")), "2030-01-07")
        self.assertEqual(fee_contract.holding_start_bounds(rule, "2030-01-03T20:00:00+08:00")["latest"], "2030-01-08T00:00:00+08:00")





class SourceFeeRegression(unittest.TestCase):
    def setUp(self):
        self.document_reader = patch("source_documents.read_extracted_document", return_value={"retrieved_at": "2029-01-01T00:00:00Z"})
        self.document_reader.start()
        self.addCleanup(self.document_reader.stop)

    def contract(self):
        raw = """基金代码：000001
份额类别：C
交易币种：CNY
销售渠道：TT
投资者类别：retail
最低申购金额：1元
最高申购金额：无上限
申购状态：开放申购
赎回状态：开放赎回
最低赎回份额：0.01份
最低剩余份额：0份
最低持有天数：0天
持有期日历：自然日
持有期含起始日：是
持有期含结束日：否
持有期终点：成交日
赎回到账间隔：2天
赎回到账日历：自然日
订单截止时间：15:00
正常确认间隔：1天
正常确认日历：交易日
金额精度：0.01元
份额精度：0.01份
费用取整：四舍五入至分
份额取整：向下截位余款退回
持有期起点：成交日
份额权益起点：成交日"""
        blocks = {"dealing": {"text": raw}, "entry": {"text": "000001基金C类份额申购费率：0.5%"}, "exit": {"text": "000001基金C类份额赎回费率：0%"}}
        reference = {"sha256": "a"*64}
        # Each binding is explicit; expected values below are independently
        # specified financial facts, not output copied from the parser.
        paths = ["subject.code", "subject.share_class", "subject.currency", "subject.channel", "subject.investor_type", "min_buy", "max_buy", "buyable", "sellable", "minimum_redemption_shares", "minimum_remaining_shares", "holding.minimum_days", "holding.day_basis", "holding.start_inclusive", "holding.end_inclusive", "holding.end_event", "settlement.lag_days", "settlement.day_basis", "order_cutoff_local", "confirmation.lag_days", "confirmation.day_basis", "trade_precision.money_step", "trade_precision.share_step", "trade_precision.fee_rounding", "trade_precision.share_rounding", "acquisition_rule.holding_start", "acquisition_rule.ownership_start"]
        bindings = {path: {"document_ref": reference, "locator": "dealing"} for path in paths}
        bindings.update(subscription=[{"document_ref": reference, "locator": "entry"}], redemption=[{"document_ref": reference, "locator": "exit"}])
        normalized = terms("000001", rate="0.005")
        normalized.pop("confirmation_max_calendar_days")
        normalized.update(order_cutoff_local="15:00:00",
            confirmation={"lag_days": 1, "day_basis": "trading_days", "normal_conditions_only": True})
        calendar = normalized["execution_calendar"]
        calendar["evidence_ref"] = {"document_ref": reference, "locator": "calendar"}
        blocks["calendar"] = {"text": json.dumps({key: value for key, value in calendar.items() if key != "evidence_ref"})}
        bindings["execution_calendar"] = {"kind": "explicit_open_dates_json", "document_ref": reference, "locator": "calendar"}
        contract = {"schema_version": 4, "adapter": "field_bindings_v1", "code": "000001", "quote_observed_at": "2029-01-01T00:00:00Z", "rule_effective_dates": {"status": "unknown"}, "normalized": normalized, "bindings": bindings}
        contract["contract_hash"] = fingerprint(contract)
        return contract, blocks

    def test_numeric_fee_cannot_override_verifiable_quote(self):
        contract, blocks = self.contract()
        with patch("source_documents.resolve_span", side_effect=lambda ref, loc, artifacts: blocks[loc]):
            self.assertEqual(fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")["subscription"]["rate"], "0.005")
            contract["normalized"]["subscription"]["rate"] = "0.0005"
            contract["contract_hash"] = fingerprint({k: v for k, v in contract.items() if k != "contract_hash"})
            with self.assertRaisesRegex(ValueError, "differ.*source"):
                fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")

    def test_reverification_cannot_refresh_an_old_trading_status(self):
        contract, blocks = self.contract()
        with patch("source_documents.resolve_span", side_effect=lambda ref, loc, artifacts: blocks[loc]), patch("source_documents.read_extracted_document", return_value={"retrieved_at": "2029-01-01T00:00:00Z"}):
            quote = fee_contract.normalize_to_terms(contract, None, as_of="2030-01-01T00:00:00Z", contract_ref={"sha256": "b"*64})
            self.assertEqual(quote["observed_at"], "2029-01-01T00:00:00Z")

    def test_holding_or_channel_numbers_are_source_bound_too(self):
        contract, blocks = self.contract()
        contract["normalized"]["holding"]["minimum_days"] = 7
        contract["contract_hash"] = fingerprint({k: v for k, v in contract.items() if k != "contract_hash"})
        with patch("source_documents.resolve_span", side_effect=lambda ref, loc, artifacts: blocks[loc]), self.assertRaisesRegex(ValueError, "differ.*source"):
            fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")

    def test_normal_clock_is_reextracted_and_source_tamper_is_rejected(self):
        contract, blocks = self.contract()
        with patch("source_documents.resolve_span", side_effect=lambda ref, loc, artifacts: blocks[loc]):
            found = fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")
            self.assertEqual(found["order_cutoff_local"], "15:00:00")
            self.assertEqual(found["confirmation"], {"lag_days": 1, "day_basis": "trading_days", "normal_conditions_only": True})
            blocks["dealing"]["text"] = blocks["dealing"]["text"].replace("订单截止时间：15:00", "订单截止时间：14:00")
            with self.assertRaisesRegex(ValueError, "differ.*source"):
                fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")

    def test_missing_calendar_cannot_be_replaced_by_normalized_injection(self):
        contract, blocks = self.contract()
        del contract["bindings"]["execution_calendar"]
        contract["contract_hash"] = fingerprint({key: value for key, value in contract.items() if key != "contract_hash"})
        with patch("source_documents.resolve_span", side_effect=lambda ref, loc, artifacts: blocks[loc]):
            with self.assertRaises(fee_contract.EvidenceGap) as caught:
                fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")
        self.assertEqual(caught.exception.required_actions[0]["field"], "execution_calendar")

    def test_matching_percentage_from_another_share_class_is_rejected(self):
        contract, blocks = self.contract()
        blocks["entry"]["text"] = "000002基金A类份额申购费率：0.5%"
        with patch("source_documents.resolve_span", side_effect=lambda ref, loc, artifacts: blocks[loc]), self.assertRaisesRegex(ValueError, "another product"):
            fee_contract.verify(contract, None, as_of="2030-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
