"""Versioned automatic method declaration, separate from financial qualification.

Only source dates, identities, dealing-menu cardinalities and confirmed account
facts determine this policy. No return, prediction, backtest result or win rate
chooses a window or a computation budget. Source/PIT/industry/path/calibration
validators still have to accept every financial input in the public pipeline.
"""
import copy
import datetime as dt
import math
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
from zoneinfo import ZoneInfo

import allocation_market
import industry_model
import asset_domains
import fund_screen
import ledger
from contracts import fingerprint, instant, require

METHOD_ID = "automatic_original_history_en_cvar_milp_v2"
METHOD = {
    "method_id": METHOD_ID,
    "primary_horizon_days": 30,
    "default_primary_goal": "hold",
    "tail_probability": 0.05,
    "confidence_level": 0.975,
    "minimum_tail_observations": 20,
    "min_train_dates": 40,
    "cv_folds": 3,
    "min_joint_dates": 30,
    "cv_initial_train_fraction": 0.5,
    "alpha_grid": [0.001, 0.01],
    "l1_ratio_grid": [0.25, 0.75],
    "minimum_advantage": 0.0005,
    "ordinary_advantage_principal_fraction": "0.005",
    "reversal_advantage_principal_fraction": "0.01",
    "rolling_fee_principal_fraction": "0.01",
    "risk_reduction_principal_fraction": "0.0025",
    "mc_cdf_tolerance": 0.01,
    "mc_failure_probability": 0.05,
    "block_sensitivity_factors": [0.5, 1, 2],
    "review_days": 365,
    "max_reviews": 365,
    "future_cash_fractions": [1],
    "parameter_status": "preregistered_internal_governance_not_empirical_optima_or_user_facts",
    "global_error_budget_rule": "epoch_j_global_alpha_over_j_times_j_plus_one_v1",
}
THEORY = {
    "elastic_net": "https://hastie.su.domains/Papers/elasticnet.pdf",
    "CVaR": "https://sites.math.washington.edu/~rtr/papers/rtr179-CVaR1.pdf",
    "stationary_bootstrap": "https://doi.org/10.1080/01621459.1994.10476870",
    "multiple_comparisons": "https://doi.org/10.1111/j.1468-0262.2005.00615.x",
    "exact_order_statistics": "https://doi.org/10.1214/aoms/1177731788",
    "scope": "method_rationale; papers_do_not_establish_the_chosen_thresholds_or_forecast_accuracy",
}


def _date(value):
    result = dt.date.fromisoformat(value)
    require(result.isoformat() == value, "Canonical source calendar date required")
    return result


def _incomplete(reason, actions, **body):
    return {"schema_id": METHOD_ID, "status": "automatic_strategy_needs_source",
            "reason": reason, "required_actions": actions, "trade_ready": False,
            "model_fit_executed": False, "all_market_optimality_claimed": False, **body}


def monte_carlo_budget(family_count, reviews, *, funding_weights=("1",), inference=None):
    """Freeze an exactly supported upper budget before reading financial values.

    Doubling is a search for a supported count, not a claim that support is
    monotone or that the returned budget is minimal. Each actual endpoint is
    checked with the same production exact binomial bracket. Actual calibration
    rechecks its own family/review/funding allocation independently.
    """
    from mpc_calibration import exact_quantile_ranks
    require(type(family_count) is int and family_count > 0
            and type(reviews) is int and reviews > 0, "Frozen positive family/review scope required")
    settings = inference or METHOD
    confidence = Fraction(str(settings["confidence_level"]))
    declared_mc = Fraction(str(settings["mc_failure_probability"]))
    factors = sorted(set(settings["block_sensitivity_factors"]))
    require(len(factors) >= 3 and min(factors) < 1 < max(factors), "Original block sensitivity scope required")
    weights = [Fraction(str(value)) for value in funding_weights]
    require(weights and all(0 < value <= 1 for value in weights)
            and sum(weights) == 1, "Complete pre-frozen funding weights required")
    total_alpha = 1-confidence
    mc_alpha = min(declared_mc, total_alpha/2)
    statistical_alpha = total_alpha-mc_alpha
    require(0 < total_alpha < Fraction(1, 2), "Original confidence must remain interior")
    plans, maximum = [], 2
    for weight in weights:
        alpha = statistical_alpha*weight/(family_count*reviews)
        failure = mc_alpha*weight/(family_count*reviews*len(factors))
        probability = float(1-alpha)
        require(0 < probability < 1 and float(failure) > 0,
                "Frozen error scope exceeds floating-point binomial precision; do not reduce scope or confidence")
        count = 2
        while True:
            checked = exact_quantile_ranks(count, probability, float(failure), settings["mc_cdf_tolerance"])
            if checked["supported"]:
                break
            count *= 2
            require(count <= 2**53, "Exact Monte Carlo budget exceeds numerical representation; numerical method needs repair")
        maximum = max(maximum, count)
        plans.append({"funding_weight": str(weight), "statistical_alpha": str(alpha),
                      "MC_failure": str(failure), "supported_budget": count,
                      "exact_binomial_check": checked})
    # A count supported for one quantile need not support another. Check all
    # groups at the single final budget instead of assuming integer-rank
    # support is monotone as B increases.
    while True:
        final = [exact_quantile_ranks(maximum, float(1-Fraction(row["statistical_alpha"])),
                    float(Fraction(row["MC_failure"])), settings["mc_cdf_tolerance"]) for row in plans]
        if all(checked["supported"] for checked in final):
            break
        maximum *= 2
        require(maximum <= 2**53, "Joint exact Monte Carlo budget exceeds numerical representation")
    for row, checked in zip(plans, final):
        row["exact_binomial_check_at_final_budget"] = checked
    return {"method": "prefrozen_scope_exact_binomial_checked_doubling_not_minimum",
            "family_count_upper": family_count, "registered_reviews": reviews,
            "max_bootstrap_repetitions": maximum, "funding_groups": plans,
            "confidence_level_unchanged": float(confidence), "financial_outcomes_examined": False}


def _menu_upper(contract, lot_count):
    # _amount_menu either enumerates <=16 values or uses 5 endpoint/quartile
    # values and at most three neighbours per original fee boundary. This is
    # solely the declared finite menu, never all admissible monetary orders.
    def boundaries(rule):
        if rule.get("kind") != "amount_tiers":
            return 0
        return sum(sum(band.get(key) is not None for key in ("minimum", "maximum"))
                   for band in rule.get("bands", []))
    buy = max(16, 5+3*boundaries(contract["subscription"]))
    sell = max(16, 5+3*(boundaries(contract["redemption"])+2))
    if contract.get("redemption_allocation", {}).get("method") == "specific_lot" and lot_count > 1:
        # The MILP retains one fund menu (hold/buy) and one complete menu
        # for each original lot. Its engineering columns add; the economic
        # combination count remains a product in the source descriptor.
        return 1+buy+lot_count*(1+sell)
    return 1+buy+sell


def source_resource_plan(codes, account, market_ref, calendar_span):
    """Count the entire supplied original scope, never only fitted survivors."""
    require(codes and codes == sorted(set(codes)), "Complete ordered dynamic source scope required")
    contracts = {row["code"]: row.get("fee_contract") for row in market_ref.get("terms", [])}
    missing = sorted(code for code in codes if not contracts.get(code))
    if missing:
        return {"status": "needs_source", "required_actions": [
            {"action": "complete_original_dealing_contract_for_method_budget", "code": code} for code in missing],
            "scope_codes": codes, "scope_was_narrowed": False}
    counts = []
    for code in codes:
        lots = sum(row["code"] == code for row in account["lots"].values())
        counts.append({"code": code, "choice_count_upper": _menu_upper(contracts[code], lots)})
    # One integer variable per original menu row replaces the product of
    # independent fund menus. Hold-to-H evaluation admits two frozen policies:
    # the selection-only MILP winner and the unchanged-account baseline.
    actions = sum(row["choice_count_upper"] for row in counts)
    n = len(codes)
    future = 1
    policy_upper = 2
    # Readiness constructs full/held baskets, at most one basket per calendar
    # origin, plus one per new candidate. No outcome determines this bound.
    families = max(2, n+calendar_span+METHOD["review_days"]+2)
    return {"status": "cardinality_bounds_prepared", "scope_codes": codes,
            "max_qualification_families": families, "max_current_actions": actions,
            "max_policy_count": policy_upper, "max_path_stages": METHOD["primary_horizon_days"]+1,
            "factorized_action_count_upper": counts, "future_rules_upper": future,
            "scope_was_narrowed": False, "financial_outcomes_examined": False,
            "scope": "complete_source_menu_MILP_columns_then_selection_winner_and_baseline_independent_calibration; not_continuous_global_optimality"}


def _benchmark(account, market_ref, at):
    snapshot = ledger.snapshot(account, at, market_ref["prices"], market_ref["price_dates"])
    require(not snapshot["blocked"] and snapshot["equity"] is not None,
            "Benchmark cannot guess unreconciled account valuations")
    values = {}
    for lot in snapshot["positions"]:
        values[lot["code"]] = values.get(lot["code"], Decimal(0))+Decimal(lot["value"])
    equity = Decimal(snapshot["equity"])
    require(equity > 0, "Nonpositive account wealth cannot define a passive benchmark")
    with localcontext() as context:
        context.prec = 50
        # Round down fund proportions and reserve the exact remainder as cash.
        # This preserves sum(weights)+cash=1 at the public decimal precision.
        quantum = Decimal("0.000000000001")
        weights = {code: ledger.decimal((value/equity).quantize(quantum, rounding="ROUND_DOWN"))
                   for code, value in sorted(values.items()) if value > 0}
        require(all(Decimal(value) > 0 for value in weights.values()),
                "Actual holding weight is below benchmark representation; do not drop the holding")
        cash = Decimal(1)-sum(map(Decimal, weights.values()), Decimal(0))
    return {"rule": "fixed_weights", "weights": weights, "cash_weight": ledger.decimal(cash),
            "rebalance_dates": []}


def build_automatic_strategy(*, account, plan_constraints, risk_state, analysis_run,
                             discovery, historical_data, market_ref, frozen=None, decision_at=None,
                             _epoch_number=1, _parent_epoch_hash=None, _renew_for_review_exhaustion=False):
    """Declare a complete real-bound spec or return concrete missing work.

    The public caller must revalidate these inputs from original bytes. This
    constructor never accepts caller qualification flags or grants readiness.
    ``frozen`` is the prior immutable method record, not a previous report.
    """
    ledger._state(account)
    require(account["currency"] == risk_state["currency"] == market_ref["currency"] == "CNY",
            "Automatic policy requires the confirmed CNY account")
    at = decision_at or max((analysis_run["started_at"], market_ref["observed_at"]), key=instant)
    day = instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date()
    constraints = plan_constraints["constraints"]
    require(constraints["platform"] == "TT" and constraints["currency"] == "CNY",
            "Automatic policy is for the actual confirmed TT/CNY plan")
    # The confirmed plan permits a free-text research purpose. Only an exact
    # canonical terminal-goal value is an execution instruction; other prose
    # keeps the preregistered hold-to-H objective, with its original text bound
    # in the declaration inputs rather than invented user redemption facts.
    primary_goal = (constraints["goal"] if constraints["goal"] in ("hold", "redeem")
                    else METHOD["default_primary_goal"])
    principal = ledger.amount(risk_state["net_principal"], "actual net principal", 0)
    require(principal > 0, "Automatic policy must not infer or create invested principal")
    tolerance = ledger.amount(risk_state["loss_tolerance"], "actual loss tolerance", 0)
    require(tolerance < 1, "Actual principal loss tolerance required")
    codes = sorted(set(discovery["codes"]) | {row["code"] for row in account["lots"].values()}
                   | {row["code"] for row in account["orders"].values()})
    groups = discovery["groups"]
    if discovery.get("universe_policy") is None:
        return _incomplete("actual_discovery_universe_policy_missing", [{
            "action": "complete_this_discovery_original_universe_policy"}],
            original_codes=codes, original_groups=copy.deepcopy(groups))
    fund_screen.validate_universe_policy(discovery["universe_policy"])
    universe_policy = copy.deepcopy(discovery["universe_policy"])
    if discovery["status"] not in ("ready", "ready_with_pending_groups"):
        return _incomplete("original_candidate_group_scope_not_complete", [{
            "action": "complete_original_dynamic_discovery_groups_without_restricting_candidates"}],
            original_codes=codes, original_groups=copy.deepcopy(groups))
    complete_members = {code for group in groups if group["coverage_status"].startswith("complete_") for code in group["members"]}
    prequalification = discovery.get("prequalification", {})
    omitted = complete_members-set(codes)
    require(all(code in prequalification and prequalification[code].get("eligible") is False
                and prequalification[code].get("reasons") for code in omitted),
            "Every omitted original comparison member needs its source-bound identity/plan/thesis exclusion")
    required_candidates = {code for code in complete_members if prequalification.get(code, {}).get("eligible") is True}
    require(required_candidates <= set(codes), "Automatic policy omitted a source-admitted complete-group candidate")
    nav = historical_data.get("nav", {})
    missing = sorted(code for code in codes if not nav.get(code))
    if missing:
        return _incomplete("actual_original_NAV_history_missing", [{
            "action": "obtain_actual_original_NAV_versions", "code": code} for code in missing], original_codes=codes)
    dates = []
    for code in codes:
        current = [_date(row["date"]) for row in nav[code]]
        require(current == sorted(set(current)) and current[-1] <= day,
                "Original NAV identity/date chronology is invalid")
        dates.extend(current)
    start, end = min(dates), day-dt.timedelta(days=1)
    span = (day-start).days
    require(span > 0, "Historical sources must precede the actual analysis")
    resources = source_resource_plan(codes, account, market_ref, span)
    if resources["status"] != "cardinality_bounds_prepared":
        return _incomplete(resources["status"], resources["required_actions"], resource_plan=resources)
    if frozen is not None:
        require(frozen["schema_id"] == METHOD_ID and frozen["method_hash"] == fingerprint(METHOD),
                "Frozen method belongs to another implementation")
        previous = frozen["spec"]
        expansion = sorted(set(codes)-set(frozen["source_scope_codes"]))
        changed_limits = previous["constraints"] != constraints["position_limits"]
        changed_goal = previous["planning"]["primary_goal"] != primary_goal
        changed_universe_policy = previous["universe_policy"] != universe_policy
        # The initial family bound already reserves one possible origin per
        # calendar day in this entire epoch. Later dates do not reserve another
        # year's reviews or enlarge that frozen allocation.
        current_families = max(2, len(codes)+span+2)
        current_resources = {**resources, "max_qualification_families": current_families}
        enlarged = any(current_resources[key] > previous["decision"][key] for key in (
                "max_qualification_families", "max_current_actions", "max_policy_count"))
        if expansion or changed_limits or changed_goal or changed_universe_policy or enlarged or _renew_for_review_exhaustion or instant(at) >= instant(previous["trade_policy"]["family"]["end_at"]):
            require(frozen["global_error_budget"]["rule"] == METHOD["global_error_budget_rule"],
                    "An old epoch that spent its full alpha cannot invent remaining global allowance")
            return build_automatic_strategy(account=account, plan_constraints=plan_constraints, risk_state=risk_state,
                analysis_run=analysis_run, discovery=discovery, historical_data=historical_data, market_ref=market_ref,
                decision_at=at, _epoch_number=frozen["global_error_budget"]["epoch_number"]+1,
                _parent_epoch_hash=fingerprint(frozen))
        spec = copy.deepcopy(previous)
        spec["news"] = copy.deepcopy(analysis_run["news_policy"])
        spec["benchmark"] = _benchmark(account, market_ref, at)
        resource_plan = copy.deepcopy(frozen["resource_plan"])
        error_budget = copy.deepcopy(frozen["global_error_budget"])
    else:
        require(type(_epoch_number) is int and _epoch_number >= 1, "Monotone immutable epoch number required")
        global_alpha = Fraction(1, 1)-Fraction(str(METHOD["confidence_level"]))
        epoch_alpha = global_alpha/(_epoch_number*(_epoch_number+1))
        confidence = float(1-epoch_alpha)
        if Fraction.from_float(confidence) < 1-epoch_alpha:
            confidence = math.nextafter(confidence, math.inf)
        require(confidence < 1, "Global epoch confidence exceeds numerical precision; exact-tail implementation required")
        error_budget = {"rule": METHOD["global_error_budget_rule"], "global_confidence_level": METHOD["confidence_level"],
            "global_alpha_exact": str(global_alpha), "epoch_number": _epoch_number,
            "epoch_alpha_exact": str(epoch_alpha), "epoch_confidence_level": confidence,
            "effective_float_alpha_exact": str(1-Fraction.from_float(confidence)),
            "allocated_prefix_alpha_exact": str(global_alpha*Fraction(_epoch_number, _epoch_number+1)),
            "reserved_future_alpha_exact": str(global_alpha/Fraction(_epoch_number+1)),
            "parent_epoch_hash": _parent_epoch_hash,
            "theory": "countable_union_bound_and_telescoping_sum_1_over_j_times_j_plus_1_equals_1",
            "scope": "sum_of_registered_historical_statistical_and_MC_error_allowances; not_future_conditional_profit_or_CVaR_guarantee"}
        training = {key: copy.deepcopy(METHOD[key]) for key in (
            "min_train_dates", "cv_folds", "alpha_grid", "l1_ratio_grid", "min_joint_dates", "cv_initial_train_fraction")}
        training.update(train_window_days=span, feature_names=allocation_market.FEATURE_NAMES)
        amounts = {}
        for name, fraction in (("minimum_net_advantage_amount", "ordinary_advantage_principal_fraction"),
                               ("minimum_reversal_advantage_amount", "reversal_advantage_principal_fraction"),
                               ("maximum_rolling_fee_amount", "rolling_fee_principal_fraction"),
                               ("minimum_risk_reduction_amount", "risk_reduction_principal_fraction")):
            amounts[name] = ledger.decimal(max(Decimal("0.01"),
                (principal*Decimal(METHOD[fraction])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
        amounts["minimum_reversal_advantage_amount"] = ledger.decimal(max(
            Decimal(amounts["minimum_reversal_advantage_amount"]), Decimal(amounts["minimum_net_advantage_amount"])+Decimal("0.01")))
        inference = {"confidence_level": confidence, "minimum_net_advantage": 0,
            "maximum_ci_half_width": 0.001, "mc_cdf_tolerance": METHOD["mc_cdf_tolerance"],
            "mc_failure_probability": METHOD["mc_failure_probability"], "bootstrap_seed": 1729,
            "block_sensitivity_factors": METHOD["block_sensitivity_factors"],
            "max_observation_gap_days": 15, "minimum_tail_observations": METHOD["minimum_tail_observations"]}
        mc = monte_carlo_budget(resources["max_qualification_families"], METHOD["max_reviews"], inference=inference)
        inference["max_bootstrap_repetitions"] = mc["max_bootstrap_repetitions"]
        family_end = instant(at)+dt.timedelta(days=METHOD["review_days"])
        spec = {"schema_version": 4, "kind": "numeric_policy", "currency": "CNY",
            "universe_policy": copy.deepcopy(universe_policy),
            "model": {"algorithm": "news_industry_elastic_net_rolling_cvar", "version": "0.1.0", "provider_model": None, "prompt_hash": None},
            "training": training, "features": {"names": allocation_market.FEATURE_NAMES, "pipeline_id": "calendar-nav-features-v2", "missing": "reject"},
            "industry": {"pipeline_id": asset_domains.INPUT_SCHEMA_ID, "feature_names": industry_model.FEATURE_NAMES,
                "fund_feature_names": industry_model.FUND_FEATURE_NAMES, "pricing_phase_definition": industry_model.PHASE,
                "missing": "partial_no_trade", "training": {**training, "feature_names": industry_model.FEATURE_NAMES}},
            "availability": {"nav_lag_calendar_days": 1, "max_feature_age_days": 7, "max_market_age_seconds": 172800,
                "max_terms_age_seconds": 2592000, "terms_rule": "observed_before_cutoff"},
            "timing": {"timezone": "Asia/Shanghai", "as_of": "decision_local_date", "model_refit": "each_decision"},
            "allocation": {"tail_probability": METHOD["tail_probability"], "funding_levels": ["0"], "minimum_advantage": METHOD["minimum_advantage"]},
            "planning": {"primary_horizon_days": METHOD["primary_horizon_days"], "primary_goal": primary_goal,
                "cash_deadline_days": None, "cash_required_amount": None},
            "constraints": copy.deepcopy(constraints["position_limits"]),
            "execution": {"money_step": "0.01", "share_step": "0.00000001", "fee_rounding": "half_up", "waiting_cash": "next_decision", "fill_policy": "confirmed_events"},
            "benchmark": _benchmark(account, market_ref, at), "news": copy.deepcopy(analysis_run["news_policy"]),
            "intervention": {"mode": "forbidden", "max_turnover_fraction": 0},
            "decision": {"method": "source_paths_single_stage_full_scope_milp_v1", **{key: resources[key] for key in (
                "max_qualification_families", "max_current_actions", "max_policy_count", "max_path_stages")},
                "future_cash_fractions": METHOD["future_cash_fractions"]},
            "trade_policy": {"kind": "forward_oos_path_policy_error_v1", "registration": "declared",
                "sample_window": {"start_date": start.isoformat(), "end_date": end.isoformat()},
                "economic": {**amounts, "fee_window_days": 30}, "inference": inference,
                "family": {"method": "joint_block_maxT_fixed_review_family", "candidate_rule": "source_settlement_mpc_policy_v1",
                    "max_candidates": resources["max_policy_count"], "max_reviews": METHOD["max_reviews"],
                    "start_at": at, "end_at": family_end.isoformat(),
                    "error_budget_registration": {key: error_budget[key] for key in (
                        "rule", "epoch_number", "global_alpha_exact", "epoch_alpha_exact", "parent_epoch_hash")}}}}
        resource_plan = {**resources, "MC": mc}
    import strategy
    from workflow_strategy import diagnose_numeric_policy
    spec = strategy.validate_spec(spec)
    diagnostics = diagnose_numeric_policy(spec)
    status = "automatic_strategy_policy_prepared" if diagnostics["status"] != "structurally_insufficient" else "automatic_strategy_structurally_insufficient"
    return {"schema_id": METHOD_ID, "status": status, "method_hash": fingerprint(METHOD),
        "method": copy.deepcopy(METHOD), "spec": spec, "source_scope_codes": codes,
        "resource_plan": resource_plan, "diagnostics": diagnostics,
        "global_error_budget": error_budget,
        "account_hash": fingerprint(account), "analysis_run_id": analysis_run["run_id"],
        "economic_thresholds_basis": "first_frozen_confirmed_net_principal_times_versioned_internal_governance_fractions",
        "benchmark_scope": "independent_passive_account_weights; daily_MPC_uses_its_same_funded_hold_account_baseline",
        "primary_goal_source": ("canonical_confirmed_terminal_goal" if constraints["goal"] in ("hold", "redeem")
            else "preregistered_internal_hold_to_H_default; original_free_text_goal_preserved_in_declaration_inputs"),
        "parameter_status": METHOD["parameter_status"], "theory": THEORY,
        "frozen_epoch_reused": frozen is not None, "trade_ready": False, "model_fit_executed": False,
        "all_market_optimality_claimed": False, "required_actions": diagnostics["required_actions"],
        "original_comparison_groups": copy.deepcopy(groups),
        "original_excluded_comparison_members": {code: copy.deepcopy(prequalification[code]) for code in sorted(omitted)},
        "pending_original_groups": [copy.deepcopy(group) for group in groups if not group["coverage_status"].startswith("complete_")],
        "optimality_admission_scope": "all_source_admitted_complete_group_candidates_and_every_held_risk_code; explicit_original_exclusions_and_pending_groups_retained",
        "qualification_not_checked": diagnostics["qualification_not_checked"],
        "scope": "automatic_method_declaration_only; public_source_and_numerical_qualification_still_required"}


def _method_source_inputs(prepared, market_operation_id, at):
    """Bind to a completed public market operation and immutable method epoch.

    This writes only an engineering method record. It never initializes capital,
    changes holdings, records a trade or grants source/PIT/calibration readiness.
    The caller must obtain missing source material in this same invocation.
    """
    import stage_validation
    import research_scope
    import risk_profile
    import verify

    store, artifacts = prepared["store"], prepared["artifacts"]
    operation = store.operation(market_operation_id)
    require(operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "market_prepare",
            "Automatic policy requires a completed public market_prepare operation")
    bindings = artifacts.read_json(operation["result"]["automatic_validation"]["operation_bindings_ref"])
    stage_validation.validate_operation_result(operation["request"], operation["result"], store, artifacts, bindings)
    discovery_id = operation["request"]["payload"]["discovery_id"]
    discovery, account = research_scope.current_discovery(discovery_id, store, artifacts, as_of=at)
    require(discovery["news_review_hash"] == fingerprint(
        __import__("workflow_requests")._completed_assessment(prepared)),
        "Automatic policy discovery belongs to another prepared assessment")
    result = operation["result"]
    record, data = result["record"], artifacts.read_json(result["data_ref"])
    require(store.get("market", result["market_id"]) == record and result["market_id"] == fingerprint(record),
            "Automatic policy changed the committed public market")
    verify.references(record, artifacts)
    verify.market_contract_coverage(record, artifacts)
    require(data["nav"] == artifacts.read_json(record["provenance"]["nav_ref"]),
            "Automatic policy historical inventory differs from the original market")
    market = record["market_ref"]
    risk = risk_profile.resolve(store, account, at, market["prices"], price_dates=market["price_dates"])
    return {"account": account, "plan_constraints": store.get("plan_constraints", "main"),
        "risk_state": risk, "analysis_run": prepared["run"], "discovery": discovery, "historical_data": data,
        "market_ref": market, "decision_at": at}


def freeze_automatic_strategy(prepared, market_operation_id, *, preview=False):
    """Declare inside the public controller lease; preview performs no writes."""
    from contracts import utc_now
    store, artifacts = prepared["store"], prepared["artifacts"]
    at = utc_now()
    inputs = _method_source_inputs(prepared, market_operation_id, at)
    key = "main:"+METHOD_ID
    frozen = store.get("automatic_strategy_policy", key)
    review_count = _reviews_before(store, frozen, at)
    exhausted = frozen is not None and review_count >= frozen["spec"]["trade_policy"]["family"]["max_reviews"]
    value = build_automatic_strategy(**inputs, frozen=frozen, _renew_for_review_exhaustion=exhausted)
    previous_ref = artifacts.put_json(frozen) if frozen is not None and not preview else None
    # Preview neither publishes an artifact nor writes a method frontier.
    input_hash = fingerprint(inputs)
    state_ref = None
    if not preview:
        state = {kind: store.get(kind, "main") for kind in (
            "account", "plan_constraints", "risk_profile", "capital_current", "capital_reconciliation")}
        prefix = [[identity, row] for identity, row in list(store.scan("ledger_event"))
            if store.get("ledger_event_owner", identity) == {"account_id": "main"}
            and row["sequence"] <= inputs["account"]["sequence"]]
        prefix.sort(key=lambda item: item[1]["sequence"])
        state_ref = artifacts.put_json({"state": state, "ledger_prefix": prefix,
            "source_account_sequence": inputs["account"]["sequence"],
            "source_known_cutoff": at, "scope": "original_controller_read_state_and_immutable_owner_ledger_prefix"})
    value.update(method_declared_at=at, previous_frozen_ref=previous_ref,
                 declaration_inputs_hash=input_hash, declaration_state_ref=state_ref,
                 previous_review_count=review_count)
    created = value["status"] == "automatic_strategy_policy_prepared" and not value["frozen_epoch_reused"]
    if created and not preview:
        epoch_id = fingerprint(value)
        store.put("automatic_strategy_epoch", epoch_id, value)
        value["epoch_record_id"] = epoch_id
        # Only this engineering cursor advances. Immutable epoch declarations
        # and the public numerical review reservations remain preserved.
        store.put("automatic_strategy_policy", key, value, immutable=False)
    else:
        value["epoch_record_id"] = frozen.get("epoch_record_id") if frozen is not None else None
    discovery_id = store.operation(market_operation_id)["request"]["payload"]["discovery_id"]
    return {**value, "market_operation_id": market_operation_id, "discovery_id": discovery_id,
            "preview_only": preview, "frozen_method_record_key": key if frozen is not None or (
                value["status"] == "automatic_strategy_policy_prepared" and not preview) else None}


def _reviews_before(store, frozen, at):
    if frozen is None:
        return 0
    family_hash = fingerprint(frozen["spec"]["trade_policy"]["family"])
    reservations = [row for _, row in store.scan("trade_family_reservation")
        if row["account_id"] == "main" and row["family_hash"] == family_hash and instant(row["decision_at"]) < instant(at)]
    require(len({row["index"] for row in reservations}) == len(reservations), "Immutable epoch review reservations have duplicate indices")
    return max((row["index"] for row in reservations), default=0)


def validate_frozen_strategy(value, prepared, market_operation_id):
    """Re-read the public originals and rebuild the pre-write declaration.

    The previous immutable record, original market operation, current source
    discovery, account and risk lineage are authoritative. A just-written copy
    of this value is never used as input to reconstruct its own policy.
    """
    from contracts import utc_now
    # The public controller appends its separately validated acceptance receipt
    # after this business declaration is produced. Historical callers may pass
    # that completed result; it is not a policy field or financial permission.
    value = {key:item for key,item in value.items() if key != "automatic_validation"}
    require(value["market_operation_id"] == market_operation_id, "Method market operation changed")
    at = value["method_declared_at"]
    require(instant(prepared["run"]["started_at"]) <= instant(at) <= instant(utc_now()),
            "Method declaration timestamp is outside its actual analysis")
    source_prepared = prepared
    if value["declaration_state_ref"] is not None:
        historical = prepared["artifacts"].read_json(value["declaration_state_ref"])
        actual = prepared["store"]
        require(historical["source_known_cutoff"] == at, "Method source-state cutoff changed")
        saved_account = historical["state"]["account"]
        require(saved_account["sequence"] == historical["source_account_sequence"], "Method account source sequence changed")
        require([row["sequence"] for _, row in historical["ledger_prefix"]] == list(range(1, saved_account["sequence"]+1)),
                "Original method account source prefix is incomplete")
        for identity, row in historical["ledger_prefix"]:
            require(actual.get("ledger_event", identity) == row and actual.get("ledger_event_owner", identity) == {"account_id": "main"},
                    "Original method source event or ownership changed")
            require(instant(row["known_at"]) <= instant(at) and instant(row["recorded_at"]) <= instant(at),
                    "Method declaration borrowed a subsequently known account event")
        rebuilt_account = ledger.initial_state("CNY")
        for _, row in historical["ledger_prefix"]:
            rebuilt_account = ledger.apply_event(rebuilt_account, row)
        require(rebuilt_account == saved_account, "Original method account does not reproduce from its immutable financial events")
        class OriginalReadState:
            def __getattr__(self, name):
                return getattr(actual, name)
            def get(self, kind, key, *args, **kwargs):
                if key == "main" and kind in historical["state"]:
                    return copy.deepcopy(historical["state"][kind])
                return actual.get(kind, key, *args, **kwargs)
            def scan(self, kind, *args, **kwargs):
                if kind == "ledger_event":
                    return copy.deepcopy(historical["ledger_prefix"])
                return actual.scan(kind, *args, **kwargs)
        source_prepared = {**prepared, "store": OriginalReadState()}
    inputs = _method_source_inputs(source_prepared, market_operation_id, at)
    require(value["declaration_inputs_hash"] == fingerprint(inputs),
            "Method declaration changed its original account, market, scope or risk lineage")
    artifacts, store = prepared["artifacts"], prepared["store"]
    key = "main:"+METHOD_ID
    previous = (store.get("automatic_strategy_policy", key) if value["preview_only"] else
                artifacts.read_json(value["previous_frozen_ref"]) if value["previous_frozen_ref"] is not None else None)
    review_count = _reviews_before(store, previous, at)
    exhausted = previous is not None and review_count >= previous["spec"]["trade_policy"]["family"]["max_reviews"]
    rebuilt = build_automatic_strategy(**inputs, frozen=previous, _renew_for_review_exhaustion=exhausted)
    rebuilt.update(method_declared_at=at, previous_frozen_ref=value["previous_frozen_ref"],
                   declaration_inputs_hash=fingerprint(inputs), declaration_state_ref=value["declaration_state_ref"],
                   previous_review_count=review_count)
    created = rebuilt["status"] == "automatic_strategy_policy_prepared" and not rebuilt["frozen_epoch_reused"]
    rebuilt["epoch_record_id"] = fingerprint(rebuilt) if created and not value["preview_only"] else previous.get("epoch_record_id") if previous is not None else None
    expected = {**rebuilt, "market_operation_id": market_operation_id,
        "discovery_id": store.operation(market_operation_id)["request"]["payload"]["discovery_id"],
        "preview_only": value["preview_only"], "frozen_method_record_key": key if previous is not None or (
            rebuilt["status"] == "automatic_strategy_policy_prepared" and not value["preview_only"]) else None}
    require(value == expected, "Automatic strategy does not reproduce its original source-bound pre-write policy")
    current = store.get("automatic_strategy_policy", key)
    if not value["preview_only"] and rebuilt["status"] == "automatic_strategy_policy_prepared":
        epoch = store.get("automatic_strategy_epoch", value["epoch_record_id"])
        require(epoch is not None and fingerprint(epoch) == value["epoch_record_id"], "Immutable method epoch was replaced or removed")
        require(epoch["global_error_budget"] == rebuilt["global_error_budget"]
            and epoch["spec"]["trade_policy"] == rebuilt["spec"]["trade_policy"], "Method epoch changed its reserved global allowance or review family")
        # Revalidating an older operation after a legitimate subsequent epoch
        # still checks its immutable record. The engineering cursor never moves
        # backwards, and every later declaration retains its parent witness.
        require(current is not None and current["global_error_budget"]["epoch_number"] >= epoch["global_error_budget"]["epoch_number"],
                "Method frontier was restarted during declaration")
    return {"status": "passed", "scope": "original_public_market_account_scope_and_prewrite_method_reconstruction_only",
        "trade_ready": False, "financial_qualification_granted": False,
        "declaration_inputs_hash": fingerprint(inputs)}
