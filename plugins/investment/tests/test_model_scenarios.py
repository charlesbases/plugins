"""Independent cash arithmetic and timing contracts for conditional scenarios."""

import copy
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/model_scenarios.py"
spec = importlib.util.spec_from_file_location("investment_model_scenarios", SCRIPT)
model = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model)
CODE = "123456"
OTHER = "654321"


def history(points, code=CODE):
    return [{"code": code, "date": day, "nav": nav, "distribution_per_share": dividend}
            for day, nav, dividend in points]


def features(days, code=CODE):
    return [{"code": code, "feature_cutoff_nav_date": day, "known_max_nav_date": day,
             "values": dict(zip(model.FEATURE_NAMES, (.1, .2, .3, .4, .5)))} for day in days]


def fixture(cutoffs=None, horizon=4, scenario=None):
    nav = {CODE: history([("2024-01-01", 2, 0), ("2024-01-03", 2, 0),
                          ("2024-01-04", 2, .5), ("2024-01-05", 1.8, .2),
                          ("2024-01-06", 1.6, 0), ("2024-01-08", 2.2, 0),
                          ("2024-01-12", 2.2, 0)])}
    xs = {CODE: features(cutoffs or ["2024-01-01"])}
    samples = model.build_samples(nav, xs, {CODE: {"fund_group_id": "portfolio-1"}}, [horizon], scenario)
    predictions = [dict(sample, target="net_return", split="test", predicted=.1) for sample in samples]
    return nav, samples, predictions


class ModelScenarioTests(unittest.TestCase):
    def test_cash_dividends_fees_and_availability_are_exact(self):
        scenario = dict(model.DEFAULT_SCENARIO, subscription_fee=.01, redemption_fee=.02)
        _, samples, _ = fixture(scenario=scenario)
        sample = samples[0]
        self.assertEqual((sample["decision_date"], sample["entry_date"], sample["exit_date"]),
                         ("2024-01-03", "2024-01-04", "2024-01-08"))
        self.assertEqual(sample["label_available_proxy"], "2024-01-11")
        # 1 / (1.01 * 2) shares; entry-date .5 dividend is not owned.
        # Sell only the NAV component for 98%; .2 cash dividend is not charged again.
        self.assertAlmostEqual(sample["y"]["net_return"], (2.2 * .98 + .2) / 2.02 - 1)
        self.assertAlmostEqual(sample["y"]["principal_path_loss"], 1 - 1.8 / 2.02)
        self.assertEqual(sample["y"]["terminal_loss"], 0)
        self.assertEqual(sample["scope"], "conditional_fee_scenario")
        self.assertFalse(sample["assumptions"]["strict_PIT_verified"])

    def test_terminal_principal_and_peak_loss_are_different(self):
        points = [("2024-01-01", 1, 0), ("2024-01-04", 1, 0),
                  ("2024-01-05", 1.5, 0), ("2024-01-08", .9, 0), ("2024-01-09", 1.1, 0)]
        samples = model.build_samples({CODE: history(points)}, {CODE: features(["2024-01-01"])},
                                      {CODE: {"fund_group_id": "portfolio-1"}}, [5],
                                      {"subscription_fee": 0, "redemption_fee": 0})
        self.assertAlmostEqual(samples[0]["y"]["net_return"], .1)
        self.assertEqual(samples[0]["y"]["terminal_loss"], 0)
        self.assertAlmostEqual(samples[0]["y"]["principal_path_loss"], .1)
        self.assertAlmostEqual(samples[0]["y"]["peak_drawdown"], .4)

    def test_pending_labels_do_not_turn_into_zeros(self):
        _, samples, _ = fixture(cutoffs=["2024-01-01", "2024-01-12"], horizon=30)
        self.assertEqual([s["status"] for s in samples], ["pending", "pending"])
        self.assertTrue(all(s["y"] is None and s["label_available_proxy"] is None for s in samples))
        self.assertEqual(samples[0]["entry_date"], "2024-01-04")
        self.assertIsNone(samples[1]["entry_date"])

    def test_features_never_read_future_prices_and_ids_identify_scenarios(self):
        nav, samples, _ = fixture()
        changed = copy.deepcopy(nav)
        changed[CODE][-1]["nav"] = 99
        again = model.build_samples(changed, {CODE: features(["2024-01-01"])},
                                    {CODE: {"fund_group_id": "portfolio-1"}}, [4],
                                    {"subscription_fee": 0})
        self.assertEqual(samples[0]["x"], again[0]["x"])
        self.assertNotEqual(samples[0]["id"], again[0]["id"])
        bad = features(["2024-01-01"])
        bad[0]["known_max_nav_date"] = "2024-01-12"
        with self.assertRaisesRegex(ValueError, "cutoff"):
            model.build_samples(nav, {CODE: bad}, {CODE: {"fund_group_id": "portfolio-1"}}, [4])
        with self.assertRaisesRegex(ValueError, "fund_group_id"):
            model.build_samples(nav, {CODE: features(["2024-01-01"])}, {}, [4])

    def test_replay_matches_sample_cash_math_without_double_fees(self):
        scenario = dict(model.DEFAULT_SCENARIO, subscription_fee=.01, redemption_fee=.02)
        nav, samples, predictions = fixture(scenario=scenario)
        result = model.replay(nav, samples, predictions, {"scenario": scenario})
        run = result["horizons"]["4"]["strategies"]["model"]
        expected = 10000 / 2.02 * (2.2 * .98 + .2)
        self.assertAlmostEqual(run["metrics"]["final_wealth"], expected)
        self.assertAlmostEqual(run["metrics"]["final_cash"], expected)
        self.assertAlmostEqual(run["metrics"]["net_return"], samples[0]["y"]["net_return"])
        self.assertAlmostEqual(run["metrics"]["fees"], 10000 - 10000 / 1.01 + 10000 / 2.02 * 2.2 * .02)
        dividends = [e for e in run["trade_ledger"] if e["event"] == "dividend_receivable"]
        self.assertEqual(len(dividends), 1)
        self.assertAlmostEqual(dividends[0]["amount"], 10000 / 2.02 * .2)
        self.assertFalse(run["strict_historical_execution_verified"])

    def test_delayed_cash_cannot_be_used_twice_and_pending_is_retained(self):
        nav, samples, predictions = fixture(cutoffs=["2024-01-01", "2024-01-06", "2024-01-08"])
        result = model.replay(nav, samples, predictions, {"scenario": {"settlement_calendar_days": 7}})
        run = result["horizons"]["4"]["strategies"]["model"]
        self.assertEqual(len([e for e in run["trade_ledger"] if e["event"] == "buy"]), 1)
        self.assertEqual(run["orders_deferred"], 2)
        self.assertEqual(run["pending"]["settlements"], 1)
        self.assertEqual(run["metrics"]["final_cash"], 0)
        self.assertGreater(run["metrics"]["final_wealth"], 0)
        for row in run["period_wealth"]:
            self.assertGreaterEqual(row["cash"], -1e-8)
            self.assertAlmostEqual(row["wealth"], row["cash"] + row["reserved_cash"] + row["holdings_NAV"]
                                   + row["dividend_receivable"] + row["unsettled_cash"])

    def test_choice_depends_only_on_prediction_ties_are_by_code(self):
        nav, samples, predictions = fixture()
        nav[OTHER] = [dict(row, code=OTHER) for row in nav[CODE]]
        other = [dict(s, code=OTHER, id="other", fund_group_id="portfolio-2", y={"net_return": 999}) for s in samples]
        samples.extend(other)
        predictions += [dict(s, target="net_return", split="test", predicted=.1) for s in other]
        result = model.replay(nav, samples, predictions)
        trades = result["horizons"]["4"]["strategies"]["model"]["trade_ledger"]
        self.assertEqual([row["code"] for row in trades if row["event"] == "buy"], [CODE])
        for sample in samples:
            sample["y"] = None
        again = model.replay(nav, samples, predictions)
        self.assertEqual(result, again)
        for prediction in predictions:
            prediction["predicted"] = 0
        cash = model.replay(nav, samples, predictions)["horizons"]["4"]["strategies"]["model"]
        self.assertEqual(cash["metrics"]["net_return"], 0)
        self.assertFalse(any(e["event"] == "buy" for e in cash["trade_ledger"]))

    def test_fixed_end_baselines_and_grid_are_reproducible(self):
        nav, samples, predictions = fixture()
        result = model.replay(nav, samples, predictions)
        strategies = result["horizons"]["4"]["strategies"]
        self.assertEqual(set(strategies), {"model", "periodic_equal_weight", "equal_weight_buy_hold", "buy_hold_" + CODE})
        self.assertEqual(strategies["equal_weight_buy_hold"]["metrics"], strategies["buy_hold_" + CODE]["metrics"])
        self.assertEqual(result, model.replay(nav, samples, predictions))
        grid = model.scenario_grid()
        self.assertEqual(len(grid), 18)
        self.assertEqual(len({model.scenario_id(s) for s in grid}), 18)

    def test_nonfinite_fees_and_unseen_future_entries_are_rejected_or_pending(self):
        for name in ("subscription_fee", "redemption_fee"):
            for fee in (float("nan"), float("inf"), -.1, True):
                with self.subTest(name=name, fee=fee), self.assertRaises(ValueError):
                    fixture(scenario={name: fee})
        nav, samples, predictions = fixture(cutoffs=["2024-01-08"])
        result = model.replay(nav, samples, predictions, {"dataset_end": "2024-01-10"})
        run = result["horizons"]["4"]["strategies"]["model"]
        self.assertFalse(any(e["event"] == "buy" for e in run["trade_ledger"]))
        self.assertEqual(run["pending"]["orders"], 1)
        self.assertEqual(run["metrics"]["final_wealth"], 10000)

    def test_missing_exit_keeps_real_position_without_fabricated_redemption(self):
        nav, samples, predictions = fixture(horizon=30)
        result = model.replay(nav, samples, predictions)
        run = result["horizons"]["30"]["strategies"]["model"]
        self.assertEqual(run["pending"]["positions"], 1)
        self.assertFalse(any(e["event"] == "sell" for e in run["trade_ledger"]))
        self.assertAlmostEqual(run["metrics"]["final_wealth"], 10000 / 2.003 * 2.4)
        self.assertEqual(run["metrics"]["final_cash"], 0)


if __name__ == "__main__":
    unittest.main()
