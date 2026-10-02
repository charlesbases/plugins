"""Known-process algorithm tests; simulation diagnostics are not market evidence."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import calibration


def policy():
    return {"confidence_level": .8, "minimum_net_advantage": .001, "maximum_ci_half_width": .01,
            "mc_cdf_tolerance": .075, "mc_failure_probability": .2,
            "max_bootstrap_repetitions": 400, "bootstrap_seed": 13,
            "block_sensitivity_factors": [.5, 1, 2], "max_observation_gap_days": 2}


def config():
    return {"sample_size": 80, "replications": 8, "seed": 25,
            "dates": [(dt.date(2000, 1, 1) + dt.timedelta(days=i)).isoformat() for i in range(80)],
            "scenarios": [{"rho": .2, "paired_return_sd": .002}],
            "advantage_effect": .05, "target_power": .6, "size_tolerance": .1,
            "confidence_level": .6, "max_total_bootstrap_repetitions": 50000,
            "max_total_bootstrap_observations": 4000000}


class CalibrationTests(unittest.TestCase):
    def test_markov_generator_has_known_bounded_support_and_exact_transition_rule(self):
        generator = unittest.mock.Mock()
        generator.random.side_effect = [.1, .2, .9, .7]
        values = calibration._markov_gains(generator, 4, .1, .02, .5)
        np.testing.assert_allclose(values, [.12, .12, .08, .08])

    def test_binomial_intervals_do_not_claim_certainty_at_zero_or_all(self):
        zero = calibration._binomial_interval(0, 20, .95)
        all_success = calibration._binomial_interval(20, 20, .95)
        self.assertEqual(zero[0], 0)
        self.assertGreater(zero[1], 0)
        self.assertLess(all_success[0], 1)
        self.assertEqual(all_success[1], 1)

    def test_budget_is_checked_before_generation(self):
        with patch.object(calibration, "_markov_gains") as generator:
            result = calibration.calibrate({**config(), "max_total_bootstrap_repetitions": 1}, policy())
        generator.assert_not_called()
        self.assertEqual(result["reason"], "calibration_computation_budget_exceeded")

    def test_small_real_calibration_is_reproducible_and_does_not_prove_efficacy(self):
        first = calibration.calibrate(config(), policy())
        self.assertEqual(first, calibration.calibrate(config(), policy()))
        self.assertEqual(first["status"], "insufficient_evidence")
        self.assertFalse(first["market_profitability_verified"])
        self.assertEqual(first["risk_qualification"], "not_inferred")
        null, positive = first["cases"]
        self.assertAlmostEqual(null["population_mean_advantage"], .001)
        self.assertAlmostEqual(positive["population_mean_advantage"], .051)
        self.assertEqual(positive["supported"], 8)
        self.assertLess(null["supported"], positive["supported"])

    def test_missing_intervals_count_as_noncoverage(self):
        with patch.object(calibration.statistics, "evaluate_advantage", return_value={"status": "insufficient_evidence", "interval": None}):
            result = calibration.calibrate(config(), policy())
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertTrue(all(case["covered"] == 0 and case["missing_intervals"] == 8 for case in result["cases"]))

    def test_calendar_and_stationarity_validation(self):
        for change in ({"dates": list(reversed(config()["dates"]))},
                       {"scenarios": [{"rho": 1, "paired_return_sd": .01}]},
                       {"risk_margin": .1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                calibration.calibrate({**config(), **change}, policy())


if __name__ == "__main__":
    unittest.main()
