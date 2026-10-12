"""Joint original-path calibration for a frozen finite policy family.

Politis/Romano stationary blocks preserve paired origins across every policy.
Romano/Wolf joint max statistics address selection; exact binomial order ranks
control simulation quantile uncertainty. Statistical coverage is asymptotic under
weak stationarity/dependence, not a future conditional profit or CVaR guarantee.
"""
import datetime as dt
import math
import re

import numpy as np
from arch.bootstrap import optimal_block_length
from scipy.stats import binom

from contracts import fingerprint, instant, require

THEORY = {
    "stationary_blocks": "https://doi.org/10.1080/01621459.1994.10476870",
    "joint_max_statistics": "https://doi.org/10.1111/j.1468-0262.2005.00615.x",
    "block_selection": "https://doi.org/10.1081/ETC-120028836",
    "order_statistics": "https://doi.org/10.1214/aoms/1177731788",
}
ASSUMPTIONS = "weak_stationarity_weak_dependence_finite_variance_regular_CVaR_functional; historical_current_terms_policy_diagnostic_not_future_conditional_coverage"


def _number(value):
    require(type(value) is not bool, "Boolean is not an economic amount")
    result = float(value)
    require(math.isfinite(result), "Finite source economic value required")
    return result


def exact_quantile_ranks(repetitions, probability, failure, epsilon):
    """Conservative generalized-quantile bracket, including discrete ties.

For Q_(k) < q_p, at least k IID bootstrap draws fall below q_p. Its
probability is bounded by P(Binomial(B,p)>=k); the lower endpoint is analogous.
The original epsilon limits this specified quantile's probability-rank bracket,
not a claimed uniform bound for the entire bootstrap CDF.
"""
    require(type(repetitions) is int and repetitions > 1 and 0 < probability < 1
            and 0 < failure < 1 and 0 < epsilon < .5, "Invalid exact Monte Carlo policy")
    upper = int(binom.ppf(1-failure/2, repetitions, probability))+1
    lower = int(binom.ppf(failure/2, repetitions, probability))
    good = 1 <= lower <= upper <= repetitions
    if good:
        good = (upper-lower)/(2*repetitions) <= epsilon
        good &= float(binom.sf(upper-1, repetitions, probability)) <= failure/2
        good &= float(binom.cdf(lower-1, repetitions, probability)) <= failure/2
    return {"supported": bool(good), "lower_rank": lower, "upper_rank": upper,
            "repetitions": repetitions, "target_probability": probability,
            "failure_probability": failure, "rank_half_width": (upper-lower)/(2*repetitions),
            "requested_cdf_tolerance": epsilon, "scope": "specified_quantile_probability_rank_bracket_not_uniform_CDF"}


def _required_repetitions(maximum, probability, failure, epsilon):
    """Return an exactly supported count within budget, not a global minimum.

    Integer order ranks make the normalized bracket width nonmonotone in B.
    A known supported high endpoint can still be bisected while preserving that
    endpoint invariant. An unsupported maximum does not prove every smaller B
    fails, so check downward until a supported count or a necessary rank bound.
    Adding one Bernoulli draw increases either binomial quantile by zero or one:
    lower(B) is nondecreasing and upper(B)-B is nonincreasing. Missing endpoint
    ranks therefore rule out every smaller B without changing any tolerance.
    The fallback examines at most maximum-2 counts and never expands the budget.
    """
    top = exact_quantile_ranks(maximum, probability, failure, epsilon)
    if not top["supported"]:
        if top["upper_rank"] > maximum or top["lower_rank"] < 1:
            return None, top
        for repetitions in range(maximum-1, 1, -1):
            checked = exact_quantile_ranks(repetitions, probability, failure, epsilon)
            if checked["supported"]:
                return repetitions, checked
            if checked["upper_rank"] > repetitions or checked["lower_rank"] < 1:
                break
        return None, top
    low, high = 2, maximum
    while low < high:
        middle = (low+high)//2
        if exact_quantile_ranks(middle, probability, failure, epsilon)["supported"]:
            high = middle
        else:
            low = middle+1
    return low, exact_quantile_ranks(low, probability, failure, epsilon)


def _tail(values, probability):
    n = values.shape[-2]
    mass = n*probability
    whole, fraction = int(math.floor(mass)), mass-math.floor(mass)
    # Only the largest whole observations and one fractional boundary matter.
    # Partition selects the same weighted order-statistic tail without sorting
    # every original observation in every IID Monte Carlo replicate.
    first = n-whole-1 if fraction else n-whole
    partitioned = np.partition(values, min(first, n-1), axis=-2)
    result = partitioned[..., n-whole:, :].sum(axis=-2) if whole else np.zeros(values.shape[:-2]+(values.shape[-1],))
    if fraction:
        result = result + fraction*partitioned[..., n-whole-1, :]
    return result/mass


def _hac_scale(series, length):
    """Deterministic Bartlett HAC scale, fixed before IID bootstrap replicas."""
    n = len(series)
    centered = series-series.mean(axis=0)
    variance = np.sum(centered*centered, axis=0)/n
    bandwidth = min(n-1, max(1, int(math.ceil(length))))
    for lag in range(1, bandwidth+1):
        covariance = np.sum(centered[lag:]*centered[:-lag], axis=0)/n
        variance += 2*(1-lag/(bandwidth+1))*covariance
    return np.sqrt(np.maximum(variance, 0)/n)


def _stationary_indices(rng, count, n, length):
    """Vectorized exact geometric restart mechanism, not IID original rows."""
    require(type(count) is int and count > 0 and type(n) is int and n > 0
            and math.isfinite(length) and length >= 1, "Valid stationary original-row sampling required")
    restarts = np.empty((count, n), dtype=bool)
    restarts[:, 0] = True
    restarts[:, 1:] = rng.random((count, n-1)) < 1/length
    positions = np.arange(n, dtype=np.int64)
    latest = np.maximum.accumulate(np.where(restarts, positions, 0), axis=1)
    fresh = rng.integers(0, n, size=(count, n), dtype=np.int64)
    starts = np.take_along_axis(fresh, latest, axis=1)
    return (starts+positions-latest)%n


def _bootstrap_chunks(errors, losses, length, repetitions, seed, tail):
    """All requested IID block draws; bounded batches retain every source row."""
    rng = np.random.default_rng(seed)
    n, columns = errors.shape
    # Working-array engineering bound, not a cap on sources or repetitions.
    # Actual ndarray/runtime overhead and the input panel remain separate.
    batch = min(1024, max(1, (32*1024*1024)//max(1, n*(8+2*columns)*8)))
    for start in range(0, repetitions, batch):
        count = min(batch, repetitions-start)
        indices = _stationary_indices(rng, count, n, length)
        sampled = np.empty((count, columns*2))
        sampled[:, :columns] = errors[indices].mean(axis=1)
        sampled[:, columns:] = _tail(losses[indices], tail)
        yield sampled


def _bootstrap_statistics(errors, losses, length, repetitions, seed, tail):
    """Materialized adapter for small diagnostic callers; calibration streams."""
    return np.concatenate(list(_bootstrap_chunks(errors, losses, length, repetitions, seed, tail)), axis=0)


def _bootstrap_critical(errors, losses, length, repetitions, seed, tail, *, levels, scales, active, upper_rank):
    """The exact specified upper order statistic of ALL requested pivots.

    Keep B-r+1 largest pivots; their minimum is Q_(r). This is a deterministic
    selection identity. No outcome-dependent early stopping, fewer replicates,
    approximate quantile, source subsampling or independent-column sampling.
    """
    require(type(upper_rank) is int and 1 <= upper_rank <= repetitions, "Supported exact upper order rank required")
    retain = repetitions-upper_rank+1
    kept = np.empty(0)
    consumed = 0
    for sampled in _bootstrap_chunks(errors, losses, length, repetitions, seed, tail):
        pivot = np.max(np.abs((sampled[:, active]-levels[active])/scales[active]), axis=1)
        combined = np.concatenate((kept, pivot))
        if len(combined) > retain:
            combined = np.partition(combined, len(combined)-retain)[-retain:]
        kept = combined
        consumed += len(sampled)
    require(consumed == repetitions and len(kept) == retain, "Monte Carlo streaming lost an original IID bootstrap draw")
    return float(np.min(kept)), {"sampler": "joint_geometric_restart_last_start_vectorized_v2",
        "quantile": "exact_upper_order_rank_streaming_top_B_minus_rank_plus_one",
        "draws_consumed": consumed, "upper_rank": upper_rank, "pivot_values_retained": retain,
        "source_origins_used_in_each_draw": len(errors), "source_columns_jointly_paired": errors.shape[1],
        "outcome_dependent_early_stop": False}


def _lineage(errors, losses, dates, context, family_hash, bound):
    lineage = context["mpc_calibration_lineage"]
    policies, origins = lineage["policies"], lineage["origins"]
    require(fingerprint(policies) == family_hash and len(policies) == bound, "Frozen complete policy family/hash changed")
    ids = [row["id"] for row in policies]
    require(len(set(ids)) == len(ids) and set(errors) == set(losses) <= set(ids), "Policy matrices differ from the frozen family")
    require(len(origins) == len(dates), "Original calibration source origins do not align")
    require([row["date"] for row in origins] == dates, "Original paired source dates changed")
    parsed = [dt.date.fromisoformat(date) for date in dates]
    require(parsed == sorted(set(parsed)), "Original source dates must be increasing and unique")
    window = context["spec"]["trade_policy"]["sample_window"]
    records = context["mpc_matrix_records"]
    require(set(records) == set(errors) and fingerprint(records) == context["matrix_source_hash"], "Raw policy simulation matrix/hash changed")
    for i, row in enumerate(origins):
        require(window["start_date"] <= row["date"] <= window["end_date"], "Origin falls outside the registered sample window")
        require(instant(row["origin_at"]) < instant(row["outcome_available_at"]) <= instant(context["decision_at"]),
                "Original policy outcome is not mature before the current decision")
        require(re.fullmatch(r"[0-9a-f]{64}", row["source_maturity_hash"]) is not None,
                "Original source maturity identity is absent")
        actual = {identity: records[identity][i] for identity in sorted(records)}
        require(fingerprint(actual) == row["simulation_hash"], "Origin simulation hash differs from original financial records")
        for identity, record in actual.items():
            require(record["origin_at"] == row["origin_at"] and record["label_available_at"] == row["outcome_available_at"],
                    "Original policy simulations lost paired origin/maturity")
            require(_number(record["optimism"]) == _number(errors[identity][i])
                    and _number(record["principal_loss"]) == _number(losses[identity][i])
                    and math.isclose(_number(record["predicted_gain"])-_number(record["realized_gain"]), _number(record["optimism"]), rel_tol=1e-12, abs_tol=1e-9),
                    "Errors/losses do not reproduce the original policy cash-flow simulation")
            require(type(record["source_hashes"]) is list and bool(record["source_hashes"])
                    and all(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) for value in record["source_hashes"]),
                    "Raw original source hashes are missing")
            require(fingerprint(record["source_hashes"]) == row["source_maturity_hash"],
                    "Original maturity hash differs from paired path source witnesses")
            for key in ("predicted_valuation_hash", "realized_valuation_hash"):
                require(re.fullmatch(r"[0-9a-f]{64}", record[key]) is not None, "Original financial valuation identity is absent")
    return policies, origins, parsed


def funding_error_weight(registry, registry_hash, group_id, policies):
    """Reserve error allowance for every planned group before seeing outcomes."""
    require(fingerprint(registry) == registry_hash and bool(registry), "Frozen funding registry/hash changed")
    ids = [row["group_id"] for row in registry]
    require(len(ids) == len(set(ids)), "Funding registry contains duplicate groups")
    require(all(type(row["policy_count_upper"]) is int and row["policy_count_upper"] > 0 for row in registry),
            "Positive pre-frozen policy upper bounds required")
    groups = [row for row in registry if row["group_id"] == group_id]
    require(len(groups) == 1, "Calibration group is absent from the planned registry")
    group = groups[0]
    require(len(policies) <= group["policy_count_upper"] and all(row["funding_group_id"] == group_id
            and row["proposed_contribution"] == group["proposed_contribution"] for row in policies),
            "Policy family differs from its pre-frozen funding group")
    total = sum(row["policy_count_upper"] for row in registry)
    return group["policy_count_upper"]/total, total


def calibrate(errors, losses, dates, context, *, frozen_family_hash, total_family_count, policy_count_bound,
              funding_registry, funding_registry_hash, funding_group_id):
    policy = context["spec"]["trade_policy"]
    inference, family = policy["inference"], policy["family"]
    required = {"confidence_level", "minimum_net_advantage", "maximum_ci_half_width", "mc_cdf_tolerance",
                "mc_failure_probability", "max_bootstrap_repetitions", "bootstrap_seed", "block_sensitivity_factors", "max_observation_gap_days"}
    require(required <= inference.keys(), "Complete original statistical source policy required")
    require(type(total_family_count) is int and total_family_count > 0 and type(policy_count_bound) is int and policy_count_bound > 0,
            "Declared family and policy computation budgets required")
    confidence, failure = _number(inference["confidence_level"]), _number(inference["mc_failure_probability"])
    require(.5 < confidence < 1 and 0 < failure < 1, "Interior original confidence and Monte Carlo failure cap required")
    # The declared simulation failure is an upper bound, not a demand to spend
    # all of it. Fix this allocation before examining any observed outcomes:
    # reserve at most half the total alpha for MC, then use the remainder for
    # statistical coverage. This preserves the original overall confidence by
    # the union bound even when the allowed MC cap exceeds the total alpha.
    total_alpha = 1-confidence
    allocated_mc_failure = min(failure, total_alpha/2)
    statistical_alpha = total_alpha-allocated_mc_failure
    maximum = inference["max_bootstrap_repetitions"]
    require(type(maximum) is int and maximum > 1 and type(inference["bootstrap_seed"]) is int and inference["bootstrap_seed"] >= 0,
            "Original seed and positive computation budget required")
    factors = sorted(set(_number(value) for value in inference["block_sensitivity_factors"]))
    require(len(factors) >= 3 and min(factors) < 1 < max(factors) and 1 in factors and min(factors) > 0, "Frozen shorter/base/longer block checks required")
    registered_reviews = family["max_reviews"]
    require(type(registered_reviews) is int and registered_reviews > 0, "Registered review budget required")
    reviews = context["trade_family_review_index"]
    policies, origins, parsed = _lineage(errors, losses, dates, context, frozen_family_hash, policy_count_bound)
    weight, planned_count = funding_error_weight(funding_registry, funding_registry_hash, funding_group_id, policies)
    ids = sorted(errors)
    out = {"schema_id": "source_group_joint_policy_calibration_v2", "status": "needs_calibration", "candidates": {row["id"]: {"status": "needs_calibration", "reason": "no_qualified_original_policy_panel"} for row in policies},
           "policy_hash": fingerprint(policy), "context_hash": context["context_hash"], "frozen_family_hash": frozen_family_hash,
           "matrix_source_hash": context["matrix_source_hash"], "source_origins": origins, "confidence": confidence,
           "error_budget": {"total_alpha": total_alpha, "declared_MC_failure_upper": failure,
                            "allocated_MC_failure": allocated_mc_failure, "allocated_statistical_alpha": statistical_alpha,
                            "rule": "pre_frozen_funding_upper_weight_then_family_review_union_bound",
                            "funding_group_weight": weight, "planned_policy_count_upper": planned_count,
                            "funding_registry_hash": funding_registry_hash, "funding_group_id": funding_group_id,
                            "group_statistical_alpha": statistical_alpha*weight/(total_family_count*registered_reviews),
                            "group_MC_failure": allocated_mc_failure*weight/(total_family_count*registered_reviews)},
           "assumption_scope": ASSUMPTIONS, "theory": THEORY, "policy_count_bound": policy_count_bound,
           "same_origin_joint_pairing": True, "MC": {}, "sensitivity": []}
    if type(reviews) is not int or not 1 <= reviews <= registered_reviews:
        return {**out, "reason": "registered_repeated_review_budget_exhausted"}
    if not ids or len(dates) < max(20, context["spec"]["training"]["min_joint_dates"]):
        return {**out, "reason": "too_few_mature_original_paired_dates"}
    require(all(len(errors[key]) == len(losses[key]) == len(dates) for key in ids), "All policy columns must use identical original origins")
    if max((b-a).days for a, b in zip(parsed, parsed[1:])) > inference["max_observation_gap_days"]:
        return {**out, "reason": "original_observation_gap_outside_registered_policy"}
    tail = _number(context["spec"]["allocation"]["tail_probability"])
    require(0 < tail < 1, "Interior original CVaR tail mass required")
    if "minimum_tail_observations" not in inference:
        return {**out, "reason": "minimum_tail_observations_not_declared"}
    require(type(inference["minimum_tail_observations"]) is int and inference["minimum_tail_observations"] > 0,
            "Declared minimum tail observation count must be a positive integer")
    if len(dates)*tail < inference["minimum_tail_observations"]:
        return {**out, "reason": "original_tail_sample_insufficient"}
    alpha = statistical_alpha*weight/(total_family_count*registered_reviews)
    mc_failure = allocated_mc_failure*weight/(total_family_count*registered_reviews*len(factors))
    probability = 1-alpha
    repetitions, mc = _required_repetitions(maximum, probability, mc_failure, _number(inference["mc_cdf_tolerance"]))
    out["MC"] = {**mc, "method": "exact_binomial_order_statistic_joint_maxT", "family_count": total_family_count,
                 "registered_reviews": registered_reviews, "alpha_for_this_family_review": alpha, "seed": inference["bootstrap_seed"]}
    if repetitions is None:
        return {**out, "reason": "declared_exact_Monte_Carlo_quantile_precision_exceeds_budget"}
    by_id = {row["id"]: row for row in policies}
    capital = np.asarray([_number(context["mpc_calibration_lineage"]["capital_amount"])+_number(by_id[key]["proposed_contribution"]) for key in ids])
    require(np.all(capital > 0), "Policy capital must include its own declared hypothetical contribution")
    error = np.asarray([[_number(value) for value in errors[key]] for key in ids]).T/capital
    loss = np.asarray([[_number(value) for value in losses[key]] for key in ids]).T/capital
    levels = np.concatenate((error.mean(axis=0), _tail(loss, tail)))
    quantile = np.quantile(loss, 1-tail, axis=0, method="inverted_cdf")
    influence = quantile+np.maximum(loss-quantile, 0)/tail
    data = np.concatenate((error, influence), axis=1)
    varying = np.ptp(data, axis=0) > 0
    if not np.any(varying):
        return {**out, "reason": "degenerate_original_policy_distribution"}
    normalized = (data[:, varying]-data[:, varying].mean(axis=0))/data[:, varying].std(axis=0)
    selected = optimal_block_length(normalized)
    length = float(selected["stationary"].max())
    require(math.isfinite(length) and length >= 0, "Block selector failed on original joint policy panel")
    length = max(1., length)
    lower, upper = np.full_like(levels, math.inf), np.full_like(levels, -math.inf)
    for factor in factors:
        block = max(1., length*factor)
        if block >= len(dates):
            return {**out, "reason": "block_sensitivity_has_too_few_original_blocks"}
        scales = _hac_scale(data, block)
        active = varying & (scales > 0) & np.isfinite(scales)
        critical, computation = _bootstrap_critical(error, loss, block, repetitions, inference["bootstrap_seed"], tail,
            levels=levels, scales=scales, active=active, upper_rank=mc["upper_rank"])
        interval_low, interval_high = levels-critical*scales, levels+critical*scales
        lower, upper = np.minimum(lower, interval_low), np.maximum(upper, interval_high)
        out["sensitivity"].append({"factor": factor, "block_length": block, "critical_max_abs_t": critical,
            "deterministic_studentization": "Bartlett_HAC_before_IID_bootstrap_draws", "repetitions": repetitions,
            "computation": computation})
    for j, identity in enumerate(ids):
        baseline_identity = by_id[identity]["mode"] == "baseline" and np.all(error[:, j] == 0)
        pure_cash = baseline_identity and not context["snapshot"]["positions"] and _number(context["snapshot"]["reserved_cash"]) == 0 and _number(context["snapshot"]["unsettled_cash"]) == 0 and np.all(loss[:, j] == 0)
        nondegenerate = (varying[j] or baseline_identity) and (varying[len(ids)+j] or pure_cash)
        halfwidth = max((upper[j]-lower[j])/2, (upper[len(ids)+j]-lower[len(ids)+j])/2)
        status = "calibrated" if nondegenerate and halfwidth <= inference["maximum_ci_half_width"] else "needs_calibration"
        out["candidates"][identity] = {"status": status, "reason": "historical_joint_policy_diagnostic" if status == "calibrated" else "original_variation_or_interval_precision_insufficient",
            "optimism_buffer_amount": max(0., float(upper[j]*capital[j])) if status == "calibrated" else None,
            "error_interval": [float(lower[j]*capital[j]), float(upper[j]*capital[j])],
            "risk_cvar_upper": float(upper[len(ids)+j]*capital[j]) if status == "calibrated" else None,
            "risk_cvar_interval": [float(lower[len(ids)+j]*capital[j]), float(upper[len(ids)+j]*capital[j])],
            "capital_amount": float(capital[j]), "interval_half_width_rate": float(halfwidth),
            "source_policy_hash": fingerprint(by_id[identity]), "scope": ASSUMPTIONS}
    out["status"] = "calibrated" if any(row["status"] == "calibrated" for row in out["candidates"].values()) else "needs_calibration"
    return out
