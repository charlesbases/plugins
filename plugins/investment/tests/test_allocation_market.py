"""Independent total-return and information-time contracts for allocation data."""

import copy
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/allocation_market.py"
SPEC = importlib.util.spec_from_file_location("investment_allocation_market", SCRIPT)
market = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market)

CODE = "A"


def inputs(cutoffs=("2024-01-01",)):
    points = [("2024-01-01", 2, 0), ("2024-01-04", 2, .5),
              ("2024-01-05", 1.8, .2), ("2024-01-08", 2.2, 0),
              ("2024-01-09", 2.3, 0), ("2024-01-12", 2.4, 0)]
    nav = {CODE: [{"code": CODE, "date": day, "nav": value, "distribution_per_share": dividend}
                  for day, value, dividend in points]}
    features = {CODE: [{"code": CODE, "feature_cutoff_nav_date": day, "known_max_nav_date": day,
                        "values": dict(zip(market.NAV_FEATURE_NAMES, (.1, .2, .3, .4, .5)))}
                       for day in cutoffs]}
    return nav, features, {CODE: {"fund_group_id": "portfolio-A"}}


class AllocationMarketTests(unittest.TestCase):
    def test_closed_market_origins_keep_distinct_full_horizons_and_information_ages(self):
        samples = market.build_samples(*inputs(("2024-01-05",)), [2], 1, "2024-01-07")
        self.assertEqual([row["decision_date"] for row in samples], ["2024-01-06", "2024-01-07"])
        self.assertEqual([row["feature_cutoff_date"] for row in samples], ["2024-01-05"]*2)
        self.assertEqual([row["x"][-1] for row in samples], [1, 2])
        self.assertEqual([row["target_not_before"] for row in samples], ["2024-01-08", "2024-01-09"])
        self.assertEqual([row["exit_date"] for row in samples], ["2024-01-08", "2024-01-09"])
        self.assertAlmostEqual(samples[0]["return"], 2/9)
        self.assertAlmostEqual(samples[1]["return"], 5/18)

    def test_calendar_features_obey_publication_lag_and_future_changes_cannot_rewrite_past(self):
        nav, features, info = inputs(("2024-01-01", "2024-01-05"))
        original = market.build_samples(nav, features, info, [1], 2, "2024-01-08")
        changed_features = copy.deepcopy(features)
        changed_features[CODE][1]["values"] = dict.fromkeys(market.NAV_FEATURE_NAMES, 99)
        changed_nav = copy.deepcopy(nav); changed_nav[CODE][-1]["nav"] = 1000
        revised = market.build_samples(changed_nav, changed_features, info, [1], 2, "2024-01-08")
        for before, after in zip(original, revised):
            if before["decision_date"] < "2024-01-07":
                self.assertEqual(before["x"], after["x"])
            if before["label_available_date"] < "2024-01-12":
                self.assertEqual(before["return"], after["return"])
        self.assertEqual(revised[-2]["x"][:-1], [99]*5)
        self.assertEqual(revised[-2]["x"][-1], 2)


    def test_gross_return_owns_only_post_entry_dividends(self):
        sample = market.build_samples(*inputs(), [4], 2, "2024-01-03")[0]
        self.assertEqual((sample["decision_date"], sample["entry_date"], sample["exit_date"],
                          sample["label_available_date"]),
                         ("2024-01-03", "2024-01-01", "2024-01-08", "2024-01-10"))
        # Half a share costs 1 at the known mark; terminal NAV is 1.1 and cash dividends are .35.
        self.assertAlmostEqual(sample["return"], .45)
        self.assertEqual(sample["x"], [.1, .2, .3, .4, .5, 2])
        self.assertEqual(sample["fund_group_id"], "portfolio-A")

    def test_pending_entry_and_exit_remain_unknown(self):
        samples = market.build_samples(*inputs(("2024-01-01", "2024-01-12")), [30], 2, "2024-01-14")
        self.assertEqual(samples[0]["entry_date"], "2024-01-01")
        self.assertEqual(samples[-1]["entry_date"], "2024-01-12")
        self.assertIsNone(samples[-1]["planned_execution_date"])
        for sample in samples:
            self.assertIsNone(sample["return"])
            self.assertIsNone(sample["exit_date"])
            self.assertIsNone(sample["label_available_date"])

    def test_future_prices_change_labels_not_features(self):
        nav, features, info = inputs()
        before = market.build_samples(nav, features, info, [4], 2, "2024-01-03")[0]
        nav[CODE][3]["nav"] = 4
        after = market.build_samples(nav, features, info, [4], 2, "2024-01-03")[0]
        self.assertEqual(before["x"], after["x"])
        self.assertEqual(before["feature_cutoff_date"], after["feature_cutoff_date"])
        self.assertAlmostEqual(after["return"], 1.35)

    def test_feature_identity_future_knowledge_and_group_are_rejected(self):
        nav, features, info = inputs()
        for field, value in (("code", "B"), ("known_max_nav_date", "2024-01-12"),
                             ("feature_cutoff_nav_date", "2024-01-02")):
            bad = copy.deepcopy(features)
            bad[CODE][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "identity|cutoff"):
                market.build_samples(nav, bad, info, [4], 2, "2024-01-03")
        with self.assertRaisesRegex(ValueError, "fund_group_id"):
            market.build_samples(nav, features, {}, [4], 2, "2024-01-03")

    def test_horizons_availability_and_nav_must_be_valid(self):
        for horizons in ([], [0], [True], [4, 4]):
            with self.subTest(horizons=horizons), self.assertRaisesRegex(ValueError, "horizons"):
                market.build_samples(*inputs(), horizons, 2, "2024-01-03")
        for lag in (-1, True, 2.5):
            with self.subTest(lag=lag), self.assertRaisesRegex(ValueError, "availability"):
                market.build_samples(*inputs(), [4], lag, "2024-01-03")
        nav, features, info = inputs()
        nav[CODE][1]["nav"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            market.build_samples(nav, features, info, [4], 2, "2024-01-03")

    def test_terminal_principal_and_peak_losses_have_distinct_meanings(self):
        losses = market._losses([1, 1.5, .9, 1.1])
        self.assertAlmostEqual(losses["net_return"], .1)
        self.assertEqual(losses["terminal_loss"], 0)
        self.assertAlmostEqual(losses["principal_path_loss"], .1)
        self.assertAlmostEqual(losses["peak_drawdown"], .4)


if __name__ == "__main__":
    unittest.main()
