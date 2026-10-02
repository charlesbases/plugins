"""Independent capital, fee, execution and CVaR examples for allocation."""

import copy
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/allocation.py"
spec = importlib.util.spec_from_file_location("investment_allocation", SCRIPT)
allocation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(allocation)


def asset(code, **changes):
    result = {"code": code, "subscription_fee": 0, "min_buy": 1, "buyable": True}
    result.update(changes)
    return result


def lot(code, value, **changes):
    result = {"lot_id": code + "-1", "code": code, "value": value,
              "redemption_fee": 0, "settlement_days": 3, "sellable": True}
    result.update(changes)
    return result


def account(cash=0, positions=None, **changes):
    result = {"as_of": "2026-10-03", "cash": cash, "reserved_cash": 0,
              "unsettled_cash": 0, "positions": positions or []}
    result.update(changes)
    return result


def policy(**changes):
    result = {"tail_probability": .05, "max_cvar": .25, "funding_levels": [0]}
    result.update(changes)
    return result


def compare(state, products, returns, rules=None, probabilities=None):
    distribution = {"codes": [row["code"] for row in products], "returns": returns}
    if probabilities is not None:
        distribution["probabilities"] = probabilities
    return allocation.compare_allocations(state, products, distribution, rules or policy())


class AllocationTests(unittest.TestCase):
    def test_ab_to_c_requires_real_sale_funding_and_waits_for_settlement(self):
        state = account(positions=[lot("A", 6000), lot("B", 4000)])
        products = [asset("A", buyable=False), asset("B", buyable=False), asset("C", max_buy=2000)]
        result = compare(state, products, [[.05, .01, .10]])
        best = result["zero_addition_best"]
        # Existing A/B cannot accept new subscriptions; C improves B by nine
        # percentage points, so fund the limited C purchase from B, not A.
        self.assertEqual([row["code"] for row in best["sells"]], ["B"])
        self.assertAlmostEqual(best["sells"][0]["gross_value"], 2000, places=3)
        buy = best["buys"][0]
        self.assertEqual(buy["code"], "C")
        self.assertEqual(buy["dependencies"], ["awaiting_settlement"])
        self.assertFalse(buy["executable_now"])
        self.assertEqual(buy["funding"]["settled_cash"], 0)
        self.assertAlmostEqual(best["expected_profit"], 520, places=3)
        self.assertAlmostEqual(best["funding_identity"]["remaining_free_cash"], 0, places=4)

    def test_extra_contribution_is_not_profit_and_no_sale_alternative_is_preserved(self):
        result = compare(account(positions=[lot("A", 6000), lot("B", 4000)]),
                         [asset("A"), asset("B"), asset("C", max_buy=2000)],
                         [[.05, .01, .10]], policy(funding_levels=[0, 2000]))
        keep = next(row for row in result["funding_options"][1]["candidates"]
                    if row["mode"] == "retain_positions")
        self.assertFalse(keep["sells"])
        self.assertAlmostEqual(keep["expected_profit"], 540, places=3)
        self.assertAlmostEqual(keep["expected_net_return"], .045, places=6)
        self.assertEqual(keep["buys"][0]["dependencies"], ["awaiting_contribution"])
        self.assertEqual(keep["proposed_contribution"], 2000)
        self.assertIsNone(result["cross_budget_unique_winner"])

    def test_reserved_and_unsettled_money_cannot_be_spent(self):
        result = compare(account(1000, reserved_cash=800, unsettled_cash=900),
                         [asset("C")], [[.1]])
        best = result["zero_addition_best"]
        self.assertAlmostEqual(best["buys"][0]["cash_debit"], 200, places=3)
        self.assertAlmostEqual(best["expected_profit"], 20, places=3)
        self.assertEqual(result["starting_equity"], 1900)
        self.assertEqual(best["target_cash"]["reserved"], 800)
        self.assertEqual(best["target_cash"]["unsettled"], 900)

    def test_cvar_constraint_limits_purchase_by_hand_calculation(self):
        # Half probability +40%, half -20%: worst-half loss = .2*x.
        # Risk allowance .1*10000 => x<=5000; expected profit=.1*x=500.
        result = compare(account(10000), [asset("C")], [[.4], [-.2]],
                         policy(tail_probability=.5, max_cvar=.1))
        best = result["zero_addition_best"]
        self.assertAlmostEqual(best["buys"][0]["cash_debit"], 5000, places=3)
        self.assertAlmostEqual(best["expected_profit"], 500, places=3)
        self.assertAlmostEqual(best["absolute_cvar"], 1000, places=3)

    def test_empirical_tail_uses_partial_probability_at_boundary(self):
        result = compare(account(positions=[lot("A", 100, sellable=False)]),
                         [asset("A")], [[-.3], [-.1], [.2]],
                         policy(tail_probability=.2, max_cvar=1), probabilities=[.1, .2, .7])
        baseline = next(row for row in result["funding_options"][0]["candidates"]
                        if row["mode"] == "baseline")
        self.assertAlmostEqual(baseline["absolute_cvar"], 20)
        self.assertAlmostEqual(baseline["expected_profit"], 9)

    def test_minimum_purchase_is_zero_or_at_least_the_required_amount(self):
        result = compare(account(500), [asset("C", min_buy=600)], [[.1]])
        self.assertEqual(result["zero_addition_best"]["buys"], [])
        result = compare(account(1000), [asset("C", min_buy=600, max_buy=700)], [[.1]])
        self.assertAlmostEqual(result["zero_addition_best"]["buys"][0]["cash_debit"], 700, places=3)

    def test_risk_capacity_below_minimum_purchase_requires_no_trade(self):
        # Cash can fund 600, but a 600 purchase would lose 120 in the tail,
        # exceeding the 100 risk allowance. A fractional 500 order is invalid.
        result = compare(account(1000), [asset("C", min_buy=600)], [[.4], [-.2]],
                         policy(tail_probability=.5, max_cvar=.1))
        self.assertEqual(result["zero_addition_best"]["buys"], [])

    def test_subminimum_cash_cannot_leak_through_fractional_buy_direction(self):
        # Saleable holdings make a large big-M cash bound. The residual cash is
        # below the 10 minimum, and selling A to buy the identical-return asset
        # incurs a 0.5% fee; the correct optimum has no order in every mode.
        state = account(.00016544400341444998,
                        positions=[lot("A", 10000, redemption_fee=.005)])
        result = compare(state, [asset("A", min_buy=10), asset("C", min_buy=10)], [[.01, .01]])
        for candidate in result["funding_options"][0]["candidates"]:
            self.assertEqual(candidate["buys"], [])
            self.assertEqual(candidate["sells"], [])
            self.assertAlmostEqual(candidate["expected_profit"], 100, places=6)
            self.assertAlmostEqual(candidate["funding_identity"]["remaining_free_cash"],
                                   .00016544400341444998, places=12)

    def test_first_investment_with_zero_existing_equity_uses_only_proposed_funding(self):
        result = compare(account(), [asset("C")], [[.1]], policy(funding_levels=[0, 1000]))
        self.assertEqual(result["zero_addition_best"]["expected_profit"], 0)
        funded = result["additional_funding_alternatives"][0]
        self.assertAlmostEqual(funded["expected_profit"], 100, places=3)
        self.assertEqual(funded["buys"][0]["dependencies"], ["awaiting_contribution"])

    def test_lot_fees_are_deducted_once_and_low_fee_lot_is_sold_first(self):
        state = account(positions=[lot("A", 1000, lot_id="expensive", redemption_fee=.1),
                                   lot("A", 1000, lot_id="free")])
        result = compare(state, [asset("A"), asset("C", max_buy=1000, subscription_fee=.01)], [[0, .1]])
        best = result["zero_addition_best"]
        self.assertEqual([row["lot_id"] for row in best["sells"]], ["free"])
        self.assertAlmostEqual(best["expected_profit"], 1000 * 1.1 / 1.01 - 1000, places=3)
        self.assertAlmostEqual(best["fees"], 1000 - 1000 / 1.01, places=3)

    def test_zero_advantage_keeps_original_positions_without_churn(self):
        result = compare(account(positions=[lot("A", 1000)]),
                         [asset("A"), asset("B")], [[.1, .1]])
        best = result["zero_addition_best"]
        self.assertEqual(best["turnover"], 0)
        self.assertEqual(best["mode"], "baseline")

    def test_infeasible_hold_remains_diagnostic_and_never_wins(self):
        result = compare(account(positions=[lot("A", 1000, sellable=False)]),
                         [asset("A")], [[-.5]], policy(max_cvar=.1))
        self.assertIsNone(result["zero_addition_best"])
        baseline = result["funding_options"][0]["candidates"][0]
        self.assertFalse(baseline["eligible"])

    def test_cash_can_be_best_and_added_principal_cannot_create_performance(self):
        result = compare(account(10000), [asset("C")], [[-.1]], policy(funding_levels=[0, 10000]))
        for group in result["funding_options"]:
            self.assertEqual(group["best"]["expected_profit"], 0)
            self.assertEqual(group["best"]["turnover"], 0)
        self.assertTrue(all(item.startswith("funding-0-") for item in result["cross_budget_frontier"]))

    def test_input_is_not_modified_and_bad_numbers_or_missing_terms_are_rejected(self):
        state, products = account(1000), [asset("C")]
        before = copy.deepcopy((state, products))
        compare(state, products, [[.1]])
        self.assertEqual((state, products), before)
        for invalid in (-1, float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                compare(account(invalid), products, [[.1]])
        del products[0]["subscription_fee"]
        with self.assertRaisesRegex(ValueError, "missing"):
            compare(state, products, [[.1]])

    def test_joint_hedge_is_used_instead_of_independent_asset_risks(self):
        # Perfectly opposite returns permit a riskless 50/50 portfolio.
        result = compare(account(1000), [asset("A"), asset("B")],
                         [[.3, -.1], [-.1, .3]], policy(tail_probability=.5, max_cvar=0))
        best = result["zero_addition_best"]
        self.assertAlmostEqual(best["expected_profit"], 100, places=3)
        self.assertLessEqual(best["absolute_cvar"], 1e-6)
        self.assertEqual({row["code"] for row in best["buys"]}, {"A", "B"})


if __name__ == "__main__":
    unittest.main()
