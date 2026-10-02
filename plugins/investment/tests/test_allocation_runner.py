import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "investment" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import allocation_runner as runner
import model_scenarios as market


def assets():
    return [{"code": code, "subscription_fee": 0, "redemption_fee": 0,
             "min_buy": 1, "settlement_days": 3, "buyable": True}
            for code in ("A", "C")]


def account():
    return {"as_of": "2024-01-08", "cash": 0, "reserved_cash": 0, "unsettled_cash": 0,
            "positions": [{"lot_id": "old-A", "code": "A", "value": 1000,
                "acquired_date": "2023-01-01", "sellable": True, "redemption_fee": 0,
                "settlement_days": 3}]}


def data():
    nav, features = {}, {}
    for code in ("A", "C"):
        nav[code], features[code] = [], []
        for n in range(31):
            day = dt.date(2024, 1, 1) + dt.timedelta(days=n)
            if day.weekday() >= 5:
                continue
            price = 9 if code == "A" and day >= dt.date(2024, 1, 9) else 10
            nav[code].append({"code": code, "date": str(day), "nav": price, "distribution_per_share": 0})
            features[code].append({"code": code, "feature_cutoff_nav_date": str(day),
                "known_max_nav_date": str(day), "values": dict.fromkeys(market.FEATURE_NAMES, 0)})
    return nav, features


class AllocationRunnerTests(unittest.TestCase):
    def test_new_lot_cannot_overwrite_user_lot_id(self):
        nav, _ = data()
        initial = account()
        initial["cash"] = 1000
        initial["positions"][0]["lot_id"] = "sim-0"
        book = runner.ReplayBook(initial, assets(), market._series(nav), 2, 0)
        book.submit({"sells": [], "buys": [{"code": "C", "cash_debit": 1000, "executable_now": True}]},
                    dt.date(2024, 1, 8))
        book.advance(dt.date(2024, 1, 9))
        self.assertEqual(set(book.lots), {"sim-0", "sim-1"})
        self.assertEqual(book.value(dt.date(2024, 1, 9)), 1900)
        initial["positions"].append(dict(initial["positions"][0]))
        with self.assertRaisesRegex(ValueError, "Unique"):
            runner.ReplayBook(initial, assets(), market._series(nav), 2, 0)

    def test_adapter_supplies_its_ordered_feature_contract(self):
        nav, features = data()
        rows = runner.prepare_samples(nav, features, {c: {"fund_group_id": c} for c in nav}, 3, 2)
        request = {"max_feature_age_days": 10, "training_policy": {}}
        with patch("allocation_statistics.fit_predict", return_value={"status": "insufficient_evidence"}) as fit:
            runner.fit_at(rows, ["A", "C"], "2024-01-08", request)
        self.assertEqual(fit.call_args.args[2]["feature_names"], market.FEATURE_NAMES)
        self.assertEqual(request["training_policy"], {})
        request["training_policy"]["feature_names"] = ["unrelated"]
        with self.assertRaisesRegex(ValueError, "feature order"):
            runner.fit_at(rows, ["A", "C"], "2024-01-08", request)

    def test_proposed_sale_cannot_fund_buy_before_actual_settlement(self):
        nav, _ = data()
        rules = assets()
        rules[0]["redemption_fee"] = .01
        original = account()
        before = copy.deepcopy(original)
        book = runner.ReplayBook(original, rules, market._series(nav), 2, 0)
        proposal = {"sells": [{"code": "A", "lot_id": "old-A", "gross_value": 1000}],
                    "buys": [{"code": "C", "cash_debit": 990, "executable_now": False}]}
        book.submit(proposal, dt.date(2024, 1, 8))
        self.assertEqual(len(book.orders), 1)
        book.advance(dt.date(2024, 1, 9))
        self.assertEqual(book.cash, 0)
        self.assertAlmostEqual(book.value(dt.date(2024, 1, 9)), 891)
        self.assertAlmostEqual(book.snapshot(dt.date(2024, 1, 9))["unsettled_cash"], 990)
        book.advance(dt.date(2024, 1, 11))
        self.assertEqual(book.cash, 0)
        self.assertAlmostEqual(book.snapshot(dt.date(2024, 1, 11))["unsettled_cash"], 891)
        book.advance(dt.date(2024, 1, 12))
        self.assertEqual(book.cash, 891)
        self.assertFalse(book.receivables)
        book.submit({"sells": [], "buys": [{"code": "C", "cash_debit": 891, "executable_now": True}]},
                    dt.date(2024, 1, 12))
        self.assertEqual(book.cash, 0)
        self.assertEqual(book.snapshot(dt.date(2024, 1, 12))["reserved_cash"], 891)
        self.assertEqual(original, before)

    def test_unconfirmed_units_do_not_reveal_unknown_execution_price(self):
        nav, _ = data()
        first = dict(account(), cash=1000, positions=[])
        book = runner.ReplayBook(first, assets(), market._series(nav), 2, 0)
        book.submit({"sells": [], "buys": [{"code": "A", "cash_debit": 900, "executable_now": True}]},
                    dt.date(2024, 1, 8))
        book.advance(dt.date(2024, 1, 9))
        self.assertAlmostEqual(book.lots["sim-0"]["shares"], 100)
        snapshot = book.snapshot(dt.date(2024, 1, 9))
        self.assertEqual(snapshot["positions"][0]["value"], 900)
        self.assertFalse(snapshot["positions"][0]["sellable"])
        self.assertEqual(snapshot["cash"], 100)

    def test_additional_principal_is_not_profit_and_pending_start_is_rejected(self):
        nav, _ = data()
        original = account()
        book = runner.ReplayBook(original, assets(), market._series(nav), 2, 2000)
        self.assertEqual(book.initial, 3000)
        self.assertEqual(book.value(dt.date(2024, 1, 8)) - book.initial, 0)
        self.assertEqual(original["cash"], 0)
        with self.assertRaisesRegex(ValueError, "pending"):
            runner.ReplayBook(dict(original, unsettled_cash=10), assets(), market._series(nav), 2, 0)

    def test_fee_changes_by_age_and_invalid_rates_are_rejected(self):
        rule = {"redemption_schedule": [{"minimum_days": 0, "rate": .015}, {"minimum_days": 7, "rate": 0}]}
        self.assertEqual(runner._fee(rule, dt.date(2024, 1, 1), dt.date(2024, 1, 7)), .015)
        self.assertEqual(runner._fee(rule, dt.date(2024, 1, 1), dt.date(2024, 1, 8)), 0)
        with self.assertRaises(ValueError):
            runner._fee({"redemption_fee": 1}, dt.date(2024, 1, 1), dt.date(2024, 1, 8))

    def test_samples_exclude_account_fees_and_prediction_uses_only_available_features(self):
        nav, features = data()
        rows = runner.prepare_samples(nav, features, {c: {"fund_group_id": c} for c in nav}, 3, 2)
        first = next(r for r in rows if r["code"] == "A" and r["decision_date"] == "2024-01-03")
        self.assertEqual(first["return"], 0)
        selected = runner.prediction_rows(rows, ["A", "C"], "2024-01-08", 10)
        self.assertTrue(all(r["feature_cutoff_date"] <= "2024-01-06" for r in selected))
        with self.assertRaisesRegex(ValueError, "stale"):
            runner.prediction_rows(rows, ["A"], "2025-01-01", 10)

    def test_partition_roundtrip_preserves_decisions_and_events(self):
        original = {"as_of": "2024-01-31", "status": "evaluated",
            "fitting_by_date": {"2024-01-31": {"status": "research_ready"}, "2024-02-01": {"status": "insufficient_evidence"}},
            "funding_evaluations": [{"funding_amount": 0,
                "wealth": [{"date": "2024-01-31", "wealth": 100}, {"date": "2024-02-01", "wealth": 101}],
                "events": [{"date": "2024-01-31", "event": "one"}],
                "decisions": [{"decision_date": "2024-02-01", "comparison": {}}]}]}
        with tempfile.TemporaryDirectory() as folder:
            runner.save_result(Path(folder), original)
            self.assertEqual(runner.load_result(Path(folder)), original)

    def test_daily_replay_invokes_same_allocator_with_no_repeated_contributions(self):
        nav, features = data()
        example = Path(__file__).resolve().parents[1] / "skills/investment/references/allocation-request.example.json"
        request = json.loads(example.read_text(encoding="utf-8"))
        request.update(as_of="2024-01-08", horizon_days=3, account=account(), assets=assets())
        request["allocation_policy"] = {"tail_probability": .05, "max_cvar": .5,
                                         "funding_levels": [0, 2000], "minimum_advantage": 0}
        request["evaluation"] = {"start": "2024-01-08", "end": "2024-01-19",
                                  "decision_interval_days": 1, "max_decisions": 20, "role": "development"}
        fitted = {"status": "research_ready", "joint_scenarios": {"codes": ["A", "C"], "returns": [[-.1, .1]]}}
        with patch.object(runner, "fit_at", return_value=fitted), \
             patch("allocation_statistics.evaluate_advantage", return_value={"status": "insufficient_evidence"}) as advantage, \
             patch("allocation_statistics.evaluate_tail_risk", return_value={"status": "insufficient_evidence"}) as risk:
            result = runner.compute("allocation-validate", request, nav, features, {c: {"fund_group_id": c} for c in nav})
        self.assertFalse(result["live_prediction_allowed"])
        self.assertEqual([r["initial_equity_including_contribution"] for r in result["funding_evaluations"]], [1000, 3000])
        for outcome in result["funding_evaluations"]:
            self.assertEqual(sum(e["event"] == "hypothetical_initial_contribution" for e in outcome["events"]), 1)
            self.assertEqual(outcome["net_profit"], outcome["wealth"][-1]["wealth"] - outcome["initial_equity_including_contribution"])
            self.assertTrue(all(e["cash_remaining"] >= 0 for e in outcome["events"] if e["event"] == "simulated_order"))
        for call in risk.call_args_list:
            self.assertEqual(len(call.args[0]), len(set(call.args[0])))
            self.assertEqual(call.args[4]["maximum_ci_half_width"], request["risk_validation_policy"]["maximum_ci_half_width"])
        self.assertTrue(all(not c.args[2]["preregistered_single_strategy"] for c in advantage.call_args_list))


if __name__ == "__main__":
    unittest.main()
