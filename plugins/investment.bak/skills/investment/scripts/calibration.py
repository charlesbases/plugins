"""Declared-process sensitivity of the return-advantage test, not market efficacy.

A stationary symmetric two-state Markov chain has exact zero mean, marginal
variance s² and lag-k correlation rho**k. Translating it gives a known mean
advantage. This bounded process supplies independent null/alternative truth;
it neither models the investment portfolio nor certifies future risk.
"""
import datetime as dt
import copy
import math
import re
import numpy as np
from scipy.stats import beta

import allocation_statistics as statistics
from contracts import fields, fingerprint, require


def method_fingerprint():
    from pathlib import Path
    import hashlib
    return hashlib.sha256(Path(__file__).read_bytes() +
                          Path(statistics.__file__).read_bytes()).hexdigest()


def _binomial_interval(successes, repetitions, confidence):
    alpha = 1 - confidence
    low = 0.0 if successes == 0 else float(beta.ppf(alpha / 2, successes, repetitions - successes + 1))
    high = 1.0 if successes == repetitions else float(beta.ppf(1 - alpha / 2, successes + 1, repetitions - successes))
    return [low, high]


def _markov_gains(generator, count, mean, sd, rho):
    signs = np.empty(count)
    signs[0] = 1 if generator.random() < .5 else -1
    for index in range(1, count):
        signs[index] = signs[index - 1] * (1 if generator.random() < (1 + rho) / 2 else -1)
    return mean + sd * signs


def calibrate(config, validation_policy):
    fields(config, {"sample_size", "replications", "seed", "scenarios", "advantage_effect",
                    "target_power", "size_tolerance", "confidence_level",
                    "max_total_bootstrap_repetitions", "max_total_bootstrap_observations", "dates"},
           label="return-test calibration")
    for name in ("sample_size", "replications", "max_total_bootstrap_repetitions", "max_total_bootstrap_observations"):
        require(type(config[name]) is int and config[name] > 0, "Positive calibration " + name + " required")
    require(type(config["seed"]) is int and config["seed"] >= 0, "Nonnegative seed required")
    dates = [dt.date.fromisoformat(value) for value in config["dates"]]
    require(len(dates) == config["sample_size"] and dates == sorted(set(dates)), "Exact ordered trial calendar required")
    for name in ("confidence_level", "target_power"):
        require(.5 < statistics.finite(config[name]) < 1, "Invalid calibration " + name)
    require(0 <= statistics.finite(config["size_tolerance"]) < 1 and
            statistics.finite(config["advantage_effect"]) > 0, "Invalid declared tolerance/effect")
    require(type(config["scenarios"]) is list and config["scenarios"], "Declared dependence scenarios required")
    for row in config["scenarios"]:
        fields(row, {"rho", "paired_return_sd"}, label="return calibration scenario")
        require(abs(statistics.finite(row["rho"])) < 1 and statistics.finite(row["paired_return_sd"]) > 0,
                "Stationary bounded process parameters required")
    _, capability = statistics._inference_inputs(config["dates"], [0.] * len(dates), validation_policy)
    runs = 2 * len(config["scenarios"]) * config["replications"]
    repetitions = capability["requested_bootstrap_repetitions"]
    factors = len(set(validation_policy["block_sensitivity_factors"]))
    total = runs * repetitions * factors
    result = {"status": "insufficient_evidence", "kind": "conditional_bounded_Markov_return_test_calibration",
              "config_hash": fingerprint(config), "policy_hash": fingerprint(validation_policy),
              "method_fingerprint": method_fingerprint(), "cases": [], "risk_qualification": "not_inferred",
              "assumption_scope": "declared_stationary_two_state_processes_only",
              "market_profitability_verified": False}
    if "reason" in capability:
        return {**result, "reason": capability["reason"]}
    if total > config["max_total_bootstrap_repetitions"]:
        return {**result, "reason": "calibration_computation_budget_exceeded"}
    if total * len(dates) > config["max_total_bootstrap_observations"]:
        return {**result, "reason": "calibration_observation_budget_exceeded"}
    # Bonferroni coverage for the finite declared size/power/coverage diagnostics.
    diagnostics = 4 * len(config["scenarios"])
    simultaneous_confidence = 1 - (1 - config["confidence_level"]) / diagnostics
    rng = np.random.default_rng(config["seed"])
    passed = True
    for scenario in config["scenarios"]:
        for alternative in (False, True):
            truth = validation_policy["minimum_net_advantage"] + (config["advantage_effect"] if alternative else 0)
            supported = covered = missing = 0
            for _ in range(config["replications"]):
                gains = _markov_gains(rng, len(dates), truth, scenario["paired_return_sd"], scenario["rho"])
                evaluation = statistics.evaluate_advantage(config["dates"], gains.tolist(), validation_policy)
                supported += evaluation["status"] == "supported"
                interval = evaluation["interval"]
                missing += interval is None
                covered += interval is not None and interval[0] <= truth <= interval[1]
            support = _binomial_interval(supported, config["replications"], simultaneous_confidence)
            coverage = _binomial_interval(covered, config["replications"], simultaneous_confidence)
            support_ok = support[0] >= config["target_power"] if alternative else support[1] <= 1 - validation_policy["confidence_level"] + config["size_tolerance"]
            coverage_ok = coverage[0] >= validation_policy["confidence_level"] - config["size_tolerance"]
            passed = passed and support_ok and coverage_ok
            result["cases"].append({"case": "positive_control" if alternative else "null_boundary",
                                    "scenario": scenario, "population_mean_advantage": truth,
                                    "replications": config["replications"], "supported": supported,
                                    "support_probability_interval": support, "covered": covered,
                                    "coverage_probability_interval": coverage, "missing_intervals": missing,
                                    "support_requirement_met": support_ok, "coverage_requirement_met": coverage_ok})
    return {**result, "status": "qualified" if passed else "insufficient_evidence",
            "reason": "declared_return_test_diagnostics_satisfied" if passed else "declared_return_test_diagnostics_not_established"}


def _trade_principal_risk(dates, losses, inference, alpha):
    """Time-block uncertainty for the joint-loss CVaR functional.

    The interval is conditional and asymptotic under the declared dependence
    assumptions. Monte Carlo precision concerns resampled CDFs only.
    """
    require(0 < alpha < 1, "Distinct CVaR tail probability required")
    values, result = statistics._inference_inputs(dates, losses, inference)
    result.update(estimand="CVaR_of_supplied_joint_principal_losses",
        tail_probability=alpha, finite_sample_future_loss_guarantee=False,
        observed_tail_probability_mass_count=alpha*len(values),
        assumption_scope="weak_stationarity_weak_dependence_regular_joint_loss_tail")
    if "reason" in result:
        return result
    minimum_tail = inference.get("minimum_tail_observations", 5)
    require(type(minimum_tail) is int and minimum_tail > 0, "Positive minimum tail observation requirement")
    result["minimum_tail_observations"] = minimum_tail
    if alpha*len(values) < minimum_tail:
        return {**result, "reason": "too_few_observed_joint_tail_losses_for_time_block_calibration"}
    n = len(values)
    mass = alpha*n
    whole, fractional = int(math.floor(mass)), mass-math.floor(mass)

    def tail_statistic(samples):
        ordered = np.sort(samples, axis=1)[:, ::-1]
        total = ordered[:, :whole].sum(axis=1)
        if fractional:
            total += fractional*ordered[:, whole]
        return total/mass

    result = statistics._intervals(values, inference, result, tail_statistic)
    if "reason" not in result:
        result.update(status="conditional_calibrated", absolute_cvar_upper=result["interval"][1])
    return result
