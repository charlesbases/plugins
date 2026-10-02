"""Independent analytical feedback-policy truths; no market profitability evidence."""
import copy
import unittest
from decimal import Decimal

import allocation
import cash_reference
import portfolio_mpc as mpc
import risk_numbers
import verify
from contracts import fingerprint
from test_allocation import source_context
from test_portfolio_mpc import path, EMPTY


def context(cash="0", shares="100"):
    value = source_context(cash=cash, shares=shares)
    value["decision_at"] = "2030-01-01T12:00:00+08:00"
    value["spec"]["planning"]["primary_horizon_days"] = 7
    value["fee_contracts"]["C"]["trade_precision"]["money_step"] = ".01"
    return value


def family(value):
    return mpc.freeze_policies(value, allocation.build_source_actions(value),
        max_current_actions=512, cash_fractions=[1.])


def candidate(value, comparison="ge", action=EMPTY, quantity="100"):
    return next(row for row in family(value)["policies"] if row["current_action"] == action
        and not row["future_rule"]["weights"] and row["future_rule"].get("conditional_sells")
        and row["future_rule"]["conditional_sells"][0]["comparison"] == comparison
        and (row["future_rule"]["conditional_sells"][0]["current_buy"] is not None if action["buys"] else
             row["future_rule"]["conditional_sells"][0]["source_action"]["sells"] == [{"lot_id": "old", "shares": quantity}]))


def evaluate(value, policy, scenario):
    result = mpc.simulate(value, scenario, policy["current_action"], policy["future_rule"], policy["local_decision_dates"])
    verify.cash_journal_invariants(value, policy, scenario, result, end_day=policy["local_decision_dates"][-1])
    return result


def sales(result):
    return [row for row in result["cash_flow_log"] if row["kind"] == "price_sell"]


class ConditionalPolicyTests(unittest.TestCase):
    def test_same_observable_prefix_ignores_unseen_close_and_terminal_prices(self):
        value = context()
        policy = candidate(value)
        low = path(["C"], lambda code, day: 1 if day == "2030-01-01" else .5)
        high = path(["C"], lambda code, day: 1 if day == "2030-01-01" else 2)
        left, right = (evaluate(value, policy, scenario) for scenario in (low, high))
        self.assertEqual(left["conditional_decisions"], right["conditional_decisions"])
        self.assertEqual([row["submitted_at"] for row in sales(left)], ["2030-01-02T12:00:00+08:00"])
        self.assertEqual(sales(left)[0]["shares"], sales(right)[0]["shares"])
        self.assertEqual((left["terminal_wealth"], right["terminal_wealth"]), (50, 200))

    def test_ge_le_candidates_and_no_trigger_keep_initial_shares(self):
        value = context()
        value["fee_contracts"]["C"]["redemption"] = {"kind": "percentage", "rate": ".01"}
        scenario = path(["C"])
        ge, le = (evaluate(value, candidate(value, comparison), scenario) for comparison in ("ge", "le"))
        self.assertEqual(sales(ge), [])
        self.assertEqual(Decimal(ge["terminal_lots"][0]["shares"]), 100)
        self.assertEqual((ge["terminal_wealth"], ge["fees"]), (100, 0))
        self.assertEqual((le["terminal_wealth"], le["fees"]), (99, 1))
        self.assertEqual(le["conditional_submission_count"], 1)
        self.assertEqual({row["future_rule"]["conditional_sells"][0]["comparison"] for row in family(value)["policies"]
                          if row["future_rule"].get("conditional_sells")}, {"ge", "le"})

    def test_initial_buy_uses_actual_unknown_dealing_nav_after_confirmation(self):
        value = context(cash="10", shares="0")
        action = {"buys": [{"code": "C", "cash_debit": "10"}], "sells": []}
        policy = candidate(value, action=action)
        condition = policy["future_rule"]["conditional_sells"][0]
        self.assertEqual(Decimal(condition["reference_value"]), 10)
        self.assertEqual(condition["decision_dates"], ["2030-01-03"])
        scenario = path(["C"], lambda code, day: 2 if day == "2030-01-01" else 3 if day == "2030-01-02" else 4)
        result = evaluate(value, policy, scenario)
        buy = next(row for row in result["cash_flow_log"] if row["kind"] == "price_buy")
        self.assertEqual(Decimal(buy["shares"]), 5)
        self.assertEqual((sales(result)[0]["lot_id"], Decimal(sales(result)[0]["shares"])), ("model:1", 5))
        self.assertEqual(sales(result)[0]["submitted_at"], "2030-01-03T12:00:00+08:00")
        self.assertEqual(result["terminal_wealth"], 20)
        forged = copy.deepcopy(policy)
        forged["future_rule"]["conditional_sells"][0]["current_buy"]["lot_id"] = "model:2"
        with self.assertRaisesRegex(ValueError, "purchase binding"):
            cash_reference.reconcile(value, forged, scenario, result["cash_flow_log"], "2030-01-08",
                {"cash": "20", "reserved_cash": "0", "receivables": "0"})

    def test_initial_buy_first_node_includes_declared_nav_availability_lag(self):
        value = context(cash="10", shares="0")
        value["spec"]["availability"] = {"nav_lag_calendar_days": 4}
        action = {"buys": [{"code": "C", "cash_debit": "10"}], "sells": []}
        policy = candidate(value, action=action)
        self.assertEqual(policy["future_rule"]["conditional_sells"][0]["decision_dates"], ["2030-01-05"])
        result = evaluate(value, policy, path(["C"], lambda code, day: 2))
        self.assertEqual(sales(result)[0]["submitted_at"], "2030-01-05T12:00:00+08:00")

    def tier_case(self):
        value = context()
        value["snapshot"]["positions"][0]["acquired_at"] = "2029-12-29T00:00:00+08:00"
        value["fee_contracts"]["C"]["redemption"] = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": ".03"}},
            {"minimum": "6", "fee": {"kind": "percentage", "rate": ".01"}},
            {"minimum": "8", "fee": {"kind": "percentage", "rate": "0"}}]}
        policy = candidate(value)
        early = path(["C"], lambda code, day: 1.04)
        later = path(["C"], lambda code, day: 1 if day < "2030-01-03" else 1.02)
        held = path(["C"], lambda code, day: .9)
        return value, policy, early, later, held

    def test_weighted_fees_follow_no_sale_and_different_source_holding_ages(self):
        value, policy, *scenarios = self.tier_case()
        self.assertEqual(policy["future_rule"]["conditional_sells"][0]["decision_dates"], ["2030-01-02", "2030-01-04", "2030-01-06"])
        results = [evaluate(value, policy, scenario) for scenario in scenarios]
        self.assertEqual([result["fees"] for result in results], [3.12, 1.02, 0])
        self.assertEqual([sales(result)[0]["date"] if sales(result) else None for result in results], ["2030-01-02", "2030-01-04", None])
        weighted = sum(p*risk_numbers.number(result["fees"]) for p, result in zip(risk_numbers.measure([.2, .3, .5]), results))
        self.assertEqual(weighted, risk_numbers.number(".93"))
        self.assertEqual(results[-1]["terminal_wealth"], 90)

    def test_independent_reference_rejects_forged_and_missing_first_sale(self):
        value, policy, early, _, _ = self.tier_case()
        result = evaluate(value, policy, early)
        altered_prefix = path(["C"], lambda code, day: .8 if day == "2030-01-01" else 1.04)
        with self.assertRaisesRegex(ValueError, "first satisfied"):
            verify.cash_journal_invariants(value, policy, altered_prefix, result, end_day="2030-01-08")
        omitted = copy.deepcopy(result)
        omitted["cash_flow_log"] = []
        omitted.update(settled_cash="0", receivables="0", terminal_wealth=104, path_execution_cost=0, fees=0)
        omitted["terminal_lots"] = copy.deepcopy(value["snapshot"]["positions"])
        omitted["valuation_hash"] = fingerprint({key: item for key, item in omitted.items() if key != "valuation_hash"})
        with self.assertRaisesRegex(ValueError, "sale differs from frozen action"):
            verify.cash_journal_invariants(value, policy, early, omitted, end_day="2030-01-08")
        forged = copy.deepcopy(policy)
        forged["future_rule"]["conditional_sells"][0]["reference_value"] = "99"
        with self.assertRaisesRegex(ValueError, "reference value"):
            verify.cash_journal_invariants(value, forged, early, {**result, "future_rule": forged["future_rule"]}, end_day="2030-01-08")

    def test_paired_calibration_replays_each_original_prefix_and_induced_fee(self):
        value, policy, early, later, _ = self.tier_case()
        baseline = next(row for row in family(value)["policies"] if row["mode"] == "baseline")
        value["spec"]["trade_policy"] = {"sample_window": {"start_date": "2026-01-01", "end_date": "2026-01-01"}}
        marks = {"C": {"nav_date": "2029-12-31", "value": "1", "known_at": "2030-01-01T08:00:00+08:00", "source_ref": {"analytical": True}}}
        pair = {"origin_at": "2026-01-01T12:00:00+08:00", "label_available_at": "2026-01-10T12:00:00+08:00",
                "source_hashes": [fingerprint(early), fingerprint(later)], "predicted": early, "realized": later}
        paths = {"schema": "source_role_price_paths_v3", "known_marks": marks, "calibration_paths": [pair]}
        errors, losses, dates, records, invalid = mpc._calibration_matrix(value, paths, [policy], {"0.0": baseline})
        self.assertEqual(invalid, {})
        self.assertEqual(dates, ["2026-01-01"])
        self.assertAlmostEqual(records[policy["id"]][0]["predicted_gain"], -3.12)
        self.assertAlmostEqual(records[policy["id"]][0]["realized_gain"], -1.02)
        self.assertAlmostEqual(errors[policy["id"]][0], -2.10)
        self.assertAlmostEqual(losses[policy["id"]][0], -.98)


    def test_conditional_nodes_use_joint_source_pricing_grid(self):
        from test_portfolio_mpc import with_second
        value = with_second(context())
        value["fee_contracts"]["D"]["execution_calendar"] = copy.deepcopy(value["fee_contracts"]["D"]["execution_calendar"])
        value["fee_contracts"]["D"]["execution_calendar"]["open_dates"].remove("2030-01-02")
        policy = candidate(value)
        self.assertEqual(policy["future_rule"]["conditional_sells"][0]["decision_dates"], ["2030-01-03"])
        scenario = path(["C", "D"])
        del scenario["nav"]["2030-01-02"]
        result = evaluate(value, policy, scenario)
        self.assertEqual(sales(result)[0]["date"], "2030-01-03")

    def test_reinvestment_waits_when_exact_joint_grid_dealing_nav_is_absent(self):
        from test_portfolio_mpc import with_second
        value = with_second(context())
        value["market_ref"]["price_dates"]["D"] = "2030-01-01"
        value["fee_contracts"]["C"]["execution_calendar"] = copy.deepcopy(value["fee_contracts"]["C"]["execution_calendar"])
        value["fee_contracts"]["C"]["execution_calendar"]["open_dates"].remove("2030-01-06")
        policy = next(row for row in family(value)["policies"] if row["current_action"] == EMPTY
            and row["future_rule"]["weights"] == {"D": "1.0"} and row["future_rule"].get("conditional_sells")
            and row["future_rule"]["conditional_sells"][0]["comparison"] == "ge"
            and row["future_rule"]["conditional_sells"][0]["source_action"]["sells"] == [{"lot_id": "old", "shares": "100"}])
        scenario = path(["C", "D"])
        del scenario["nav"]["2030-01-06"]
        result = evaluate(value, policy, scenario)
        self.assertEqual(Decimal(result["settled_cash"]), 100)
        self.assertFalse(result["future_used"])
        self.assertFalse(any(row["kind"] == "reserve_buy" for row in result["cash_flow_log"]))

    def test_current_buy_cannot_overwrite_a_source_modeled_lot_identity(self):
        value = context(cash="10")
        value["snapshot"]["positions"][0]["lot_id"] = "model:1"
        action = {"buys": [{"code": "C", "cash_debit": "10"}], "sells": []}
        policy = candidate(value, action=action)
        with self.assertRaisesRegex(mpc.PolicyInfeasible, "identity collides"):
            mpc.simulate(value, path(["C"]), action, policy["future_rule"], policy["local_decision_dates"])
        with self.assertRaisesRegex(ValueError, "identity collides"):
            cash_reference.conditional_sales(value, policy, path(["C"]), "2030-01-08")
        self.assertEqual(Decimal(value["snapshot"]["positions"][0]["shares"]), 100)

    def test_optimiser_publishes_induced_probability_weighted_fees(self):
        import ledger
        import newtrade_guard
        from test_mpc_contracts import FrozenPolicyContractTests
        from portfolio_paths import information_groups
        value, target, early, later, held = self.tier_case()
        registered, _, _ = FrozenPolicyContractTests().deadline_case()
        value["spec"] = registered["spec"]
        value["spec"]["planning"].update(primary_horizon_days=7, cash_deadline_days=None, cash_required_amount=None)
        value["spec"]["decision"].update(max_current_actions=512, max_policy_count=4096, max_path_stages=8)
        value["purchase_eligible_codes"] = []
        value["account_id"], value["blocked"] = "main", False
        value["trade_family_review_index"] = 1
        value["trade_state"] = newtrade_guard.derive_state(ledger.initial_state("CNY"), [], value["decision_at"],
            value["spec"]["trade_policy"]["economic"]["fee_window_days"])
        value["context_hash"] = fingerprint({key: row for key, row in value.items() if key != "context_hash"})
        target = candidate(value)
        marks = {"C": {"nav_date": "2029-12-31", "value": "1", "known_at": "2030-01-01T08:00:00+08:00", "source_ref": {"analytical": True}}}
        selection = [{**scenario, "id": str(index), "probability": probability} for index, (scenario, probability)
                     in enumerate(zip((early, later, held), (.2, .3, .5)))]
        paths = {"schema": "source_role_price_paths_v3", "known_marks": marks, "status": "research_ready", "path_hash": "b"*64,
            "stage_dates": ["2030-01-01", "2030-01-08"], "point_paths": [early], "selection_paths": selection, "calibration_paths": []}
        paths["prefix_groups"] = information_groups(selection, paths["stage_dates"])
        comparison = allocation.build_source_actions(value)
        result = mpc.optimise(value, paths, comparison)
        evaluated = next(row for row in result["funding_options"][0]["candidates"] if row["id"] == target["id"])
        self.assertAlmostEqual(evaluated["expected_policy_fees"], .93)
        self.assertAlmostEqual(evaluated["expected_profit"], -4.53)
        self.assertEqual(mpc.validate_result(result, {"context": value, "paths": paths, "comparison": comparison})["status"], "passed")


if __name__ == "__main__":
    unittest.main()
