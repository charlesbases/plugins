"""Independent source cash/share contracts; no market effectiveness claims."""
import copy
import unittest
from decimal import Decimal

import allocation
import portfolio_mpc as mpc
from contracts import ContractError, fingerprint
from test_single_step import make_context
from test_portfolio_mpc import path, with_second, STAGES, HOLD


def source_context(cash="8", shares="0", funding=(0,)):
    context = make_context(cash=cash, shares=shares)
    equity = Decimal(context["snapshot"]["equity"])
    context["risk_state"].update(net_principal=str(equity), loss_tolerance=".25", principal_floor=str(equity*Decimal('.75')), remaining_loss_budget=str(equity*Decimal('.25')))
    context["spec"]["decision"] = {"max_current_actions": 512, "max_policy_count": 4096, "max_path_stages":8, "future_cash_fractions": [1.]}
    context["model_request"]["allocation_policy"] = {"funding_levels": list(funding)}
    context["fee_contracts"]["C"]["trade_precision"].update(money_step="1", share_step="1")
    context["context_hash"] = fingerprint(context)
    return context


class AllocationTests(unittest.TestCase):
    def test_precision_lattice_and_minimum_have_independent_inventory(self):
        context = source_context()
        context["model_request"]["assets"][0].update(min_buy=2, max_buy=5)
        family = allocation.build_source_actions(context)
        self.assertEqual(family["status"], "frozen")
        actions = family["funding_options"][0]["actions"]
        self.assertEqual({Decimal(action["buys"][0]["cash_debit"]) for action in actions if action["buys"]}, {Decimal(x) for x in range(2,6)})
        self.assertEqual(sum(not action["buys"] and not action["sells"] for action in actions), 1)

    def test_reserved_unsettled_and_sale_cash_cannot_fund_current_buy(self):
        context = source_context(cash="2", shares="8")
        context["snapshot"].update(available_cash="1", reserved_cash="1", unsettled_cash="5")
        actions = allocation.build_source_actions(context)["funding_options"][0]["actions"]
        self.assertEqual({Decimal(row["cash_debit"]) for action in actions for row in action["buys"]}, {Decimal(1)})
        with self.assertRaisesRegex(ContractError, "unreceived"):
            allocation.validate_source_action(context, {"buys": [{"code":"C","cash_debit":"2"}], "sells": []})

    def test_source_fifo_rejects_cheaper_new_lot_and_uses_aggregate_minimum(self):
        context = source_context(cash="0", shares="3")
        context["snapshot"]["positions"].append({"lot_id":"new", "code":"C", "shares":"5", "reserved_shares":"0", "acquired_at":"2029-12-31T00:00:00+08:00"})
        terms = context["fee_contracts"]["C"]
        terms.update(redemption_allocation={"method":"fifo"}, minimum_redemption_shares="4", minimum_remaining_shares="2",
            redemption_fee_application="per_lot",redemption_fee_application_source={"synthetic_original_fee_scope":"each_batch"},
            redemption_rounding_application="per_lot",redemption_rounding_application_source={"synthetic_original_cash_scope":"each_batch"})
        action = {"buys":[], "sells":[{"lot_id":"old","shares":"3"},{"lot_id":"new","shares":"1"}]}
        self.assertTrue(allocation.validate_source_action(context, action))
        with self.assertRaisesRegex(ContractError, "source lot order"):
            allocation.validate_source_action(context, {"buys":[], "sells":[{"lot_id":"new","shares":"4"}]})
        actions = allocation.build_source_actions(context)["funding_options"][0]["actions"]
        self.assertIn(action, actions)

    def test_unknown_multi_lot_allocation_does_not_invent_fifo(self):
        context = source_context(cash="0", shares="3")
        context["snapshot"]["positions"].append({"lot_id":"new", "code":"C", "shares":"5", "reserved_shares":"0", "acquired_at":"2029-12-31T00:00:00+08:00"})
        context["fee_contracts"]["C"].update(redemption_fee_application="per_lot",redemption_fee_application_source={"synthetic_original_fee_scope":"each_batch"},
            redemption_rounding_application="per_lot",redemption_rounding_application_source={"synthetic_original_cash_scope":"each_batch"})
        family = allocation.build_source_actions(context)
        sales = [action["sells"] for action in family["funding_options"][0]["actions"] if action["sells"]]
        self.assertEqual(sales, [[{"lot_id":"old","shares":"3"},{"lot_id":"new","shares":"5"}]])
        self.assertEqual(family["required_actions"], [{"action":"capture_source_redemption_lot_allocation","code":"C"}])
        with self.assertRaisesRegex(ContractError, "Source redemption allocation"):
            allocation.validate_source_action(context, {"buys":[], "sells":[{"lot_id":"new","shares":"4"}]})

    def test_real_sale_producer_survives_freeze_and_full_path(self):
        context = source_context(cash="0", shares="8")
        family = allocation.build_source_actions(context)
        frozen = mpc.freeze_policies(context, family, max_current_actions=512, cash_fractions=[1.])
        full = next(row for row in frozen["policies"] if row["current_action"]["sells"] == [{"lot_id":"old","shares":"8"}] and row["future_rule"] == HOLD)
        value = mpc.simulate(context, path(["C"]), full["current_action"], HOLD, STAGES)
        self.assertEqual(value["terminal_wealth"], 8.)
        self.assertEqual(Decimal(value["settled_cash"]), Decimal("8"))
        sale = next(row for row in value["cash_flow_log"] if row["kind"] == "price_sell")
        self.assertFalse(sale["cash_available_now"])

    def test_prior_loss_and_proposed_capital_have_independent_floor_arithmetic(self):
        context = source_context(cash="80")
        context["risk_state"].update(net_principal="100", loss_tolerance=".1", principal_floor="90", remaining_loss_budget="-10")
        funded = allocation.funded_context(context, 20)
        self.assertEqual(funded["risk_state"]["net_principal"], "120")
        self.assertEqual(Decimal(funded["risk_state"]["principal_floor"]), Decimal("108"))
        self.assertEqual(Decimal(funded["risk_state"]["remaining_loss_budget"]), Decimal("-8"))
        self.assertEqual(context["snapshot"]["available_cash"], "80")

    def test_declared_budget_returns_no_incumbent_and_family_is_stable(self):
        context = source_context()
        self.assertEqual(allocation.build_source_actions(context), allocation.build_source_actions(copy.deepcopy(context)))
        context["spec"]["decision"]["max_current_actions"] = 2
        partial = allocation.build_source_actions(context)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["funding_options"][0]["actions"], [])
        self.assertEqual(partial["funding_options"][0]["build_status"], "budget_partial")
        self.assertTrue(partial["funding_registry"])
        self.assertEqual(partial["required_actions"][0]["required"], 9)

    def test_extra_contribution_group_does_not_change_actual_cash(self):
        context = source_context(cash="0", shares="8", funding=(0,4))
        family = allocation.build_source_actions(context)
        zero, funded = family["funding_options"]
        self.assertFalse(any(action["buys"] for action in zero["actions"]))
        self.assertEqual({Decimal(row["cash_debit"]) for action in funded["actions"] for row in action["buys"]}, {Decimal(x) for x in range(1,5)})
        self.assertEqual((zero["capital_before_trade"], funded["capital_before_trade"]), ("8", "12"))
        self.assertEqual(context["snapshot"]["available_cash"], "0")


    def test_unknown_minima_allow_only_source_proven_full_redemption(self):
        context = source_context(cash="0", shares="8")
        terms = context["fee_contracts"]["C"]
        terms.update(minimum_redemption_shares=None, minimum_remaining_shares=None, full_redemption_allowed=True,
            action_evidence={"sell_full":{"status":"source_supported","missing_fields":[]},
                "sell_partial":{"status":"needs_research","missing_fields":["minimum_redemption_shares","minimum_remaining_shares"]}})
        family = allocation.build_source_actions(context)
        sales = [action["sells"] for action in family["funding_options"][0]["actions"] if action["sells"]]
        self.assertEqual(sales, [[{"lot_id":"old","shares":"8"}]])
        value = mpc.simulate(context, path(["C"]), {"buys":[],"sells":sales[0]}, HOLD, STAGES)
        self.assertEqual(value["terminal_wealth"], 8.)
        context["snapshot"]["positions"][0]["reserved_shares"] = "2"
        with self.assertRaisesRegex(ContractError, "Source evidence for redemption"):
            allocation.validate_source_action(context, {"buys":[],"sells":[{"lot_id":"old","shares":"6"}]})

    def test_published_buy_cap_does_not_prove_remaining_account_limit(self):
        context = source_context(cash="8")
        context["model_request"]["assets"][0]["max_buy"] = 8
        context["fee_contracts"]["C"]["action_evidence"] = {
            "buy":{"status":"needs_research","missing_fields":["remaining_daily_subscription_limit"]}}
        family = allocation.build_source_actions(context)
        self.assertEqual(family["funding_options"][0]["actions"], [{"buys":[],"sells":[]}])
        self.assertEqual(family["required_actions"][0]["missing_fields"], ["remaining_daily_subscription_limit"])
        with self.assertRaisesRegex(ContractError, "Source evidence for purchase"):
            allocation.validate_source_action(context, {"buys":[{"code":"C","cash_debit":"1"}],"sells":[]})


if __name__ == "__main__":
    unittest.main()
