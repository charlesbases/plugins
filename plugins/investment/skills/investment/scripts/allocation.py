"""Conditional CVaR allocations; pure proposals, never orders or account writes.

Amounts use one currency. ``cash`` includes its reserved subset; unsettled cash
is separate. Buys are cash debits inclusive of front-end subscription fees.
``tail_probability`` is tail mass (0.05 means the worst five percent), and
``max_cvar`` is a loss fraction of pre-trade equity plus proposed contribution.
Scenarios start after the proposed funding/settlement steps; they do not model
market movement while waiting or certify investment skill.
"""

import datetime as dt
import math
import warnings

SCOPE = "conditional_single_horizon_after_funding_and_settlement"
TOL = 1e-6


def _number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric data")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} outside supported range")
    return float(value)


def _flag(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")


def _validate(account, assets, distribution, policy):
    try:
        day = dt.date.fromisoformat(account["as_of"])
        if day.isoformat() != account["as_of"]:
            raise ValueError("as_of must be an ISO calendar date")
        cash = _number(account["cash"], "cash")
        reserved = _number(account["reserved_cash"], "reserved_cash", maximum=cash)
        unsettled = _number(account["unsettled_cash"], "unsettled_cash")
        lots = []
        for row in account["positions"]:
            item = dict(row)
            if not isinstance(item["lot_id"], str) or not item["lot_id"]:
                raise ValueError("lot_id must be a nonempty string")
            item["value"] = _number(item["value"], "position value")
            item["redemption_fee"] = _number(item["redemption_fee"], "redemption_fee", maximum=1)
            _flag(item["sellable"], "sellable")
            if type(item["settlement_days"]) is not int or item["settlement_days"] < 0:
                raise ValueError("settlement_days must be a nonnegative integer")
            lots.append(item)
        if len({row["lot_id"] for row in lots}) != len(lots):
            raise ValueError("duplicate lot_id")
        products = []
        for row in assets:
            item = dict(row)
            if not isinstance(item["code"], str) or not item["code"]:
                raise ValueError("code must be a nonempty string")
            _flag(item["buyable"], "buyable")
            item["subscription_fee"] = _number(item["subscription_fee"], "subscription_fee", maximum=1)
            item["min_buy"] = _number(item["min_buy"], "min_buy")
            if "max_buy" in item:
                item["max_buy"] = _number(item["max_buy"], "max_buy")
            item["max_weight"] = _number(item.get("max_weight", 1), "max_weight", maximum=1)
            products.append(item)
        codes = [row["code"] for row in products]
        if not codes or len(set(codes)) != len(codes):
            raise ValueError("assets must have unique codes")
        if any(row["code"] not in codes for row in lots):
            raise ValueError("each held code requires asset terms and scenarios")
        scenario_codes = distribution["codes"]
        if len(set(scenario_codes)) != len(scenario_codes) or set(scenario_codes) != set(codes):
            raise ValueError("scenario codes must exactly match assets")
        scenarios = []
        for row in distribution["returns"]:
            if len(row) != len(codes):
                raise ValueError("scenario row length mismatch")
            scenarios.append([_number(row[scenario_codes.index(code)], "return", minimum=-1)
                              for code in codes])
        if not scenarios:
            raise ValueError("at least one joint scenario is required")
        probabilities = distribution.get("probabilities", [1 / len(scenarios)] * len(scenarios))
        probabilities = [_number(p, "scenario probability", maximum=1) for p in probabilities]
        if len(probabilities) != len(scenarios) or not math.isclose(sum(probabilities), 1, rel_tol=0, abs_tol=1e-10):
            raise ValueError("scenario probabilities must sum to one")
        alpha = _number(policy["tail_probability"], "tail_probability", maximum=1)
        if alpha <= 0:
            raise ValueError("tail_probability must be positive")
        risk = _number(policy["max_cvar"], "max_cvar", maximum=1)
        levels = sorted(set(_number(x, "funding level") for x in policy["funding_levels"]))
        if not levels or levels[0] != 0:
            raise ValueError("funding_levels must explicitly include zero")
        advantage = _number(policy.get("minimum_advantage", 0), "minimum_advantage")
        equity = cash + unsettled + sum(row["value"] for row in lots)
        if not math.isfinite(equity + levels[-1]):
            raise ValueError("account and funding total must remain finite")
        return (cash, reserved, unsettled, lots, products, scenarios, probabilities,
                alpha, risk, levels, advantage, equity)
    except (KeyError, TypeError) as error:
        raise ValueError(f"missing or invalid allocation input: {error}") from error


def _cvar(losses, probabilities, alpha):
    """Independent exact integration of the empirical upper loss tail."""
    remaining, total = alpha, 0.0
    for loss, weight in sorted(zip(losses, probabilities), reverse=True):
        used = min(remaining, weight)
        total += used * loss
        remaining -= used
        if remaining <= 1e-12:
            break
    return total / alpha


def _solve(lots, products, scenarios, probabilities, available, funding,
           equity, alpha, risk, mode):
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, linprog, milp

    # Variables: cash buys, gross lot sales, buy-direction binaries, eta, z_s.
    n, m, count = len(products), len(lots), len(scenarios)
    direction, eta, z_start = n + m, 2 * n + m, 2 * n + m + 1
    size = z_start + count
    indices = {row["code"]: i for i, row in enumerate(products)}
    holding = np.zeros(n)
    for row in lots:
        holding[indices[row["code"]]] += row["value"]
    gross_budget = available + funding + sum(row["value"] * (1 - row["redemption_fee"])
                                            for row in lots if row["sellable"])
    lower, upper = np.zeros(size), np.full(size, np.inf)
    lower[eta] = -np.inf
    integrality = np.zeros(size)
    integrality[direction:eta], upper[direction:eta] = 1, 1
    rows, limits = [], []

    def constraint(coefficients, limit):
        rows.append(coefficients)
        limits.append(limit)

    cash_rule = np.zeros(size)
    cash_rule[:n] = 1
    for j, lot in enumerate(lots):
        upper[n + j] = lot["value"] if lot["sellable"] and mode != "retain_positions" else 0
        cash_rule[n + j] = -(1 - lot["redemption_fee"])
        row = np.zeros(size)
        row[n + j] = 1
        row[direction + indices[lot["code"]]] = lot["value"]
        constraint(row, lot["value"])
    constraint(cash_rule, available + funding)
    for i, product in enumerate(products):
        maximum = min(gross_budget, product.get("max_buy", gross_budget))
        if not product["buyable"] or mode == "sales_only" or maximum < product["min_buy"]:
            maximum = 0
        upper[i] = maximum
        row = np.zeros(size)
        row[i], row[direction + i] = 1, -maximum
        constraint(row, 0)
        row = np.zeros(size)
        row[i], row[direction + i] = -1, product["min_buy"]
        constraint(row, 0)
        row = np.zeros(size)
        row[i] = 1 / (1 + product["subscription_fee"])
        for j, lot in enumerate(lots):
            if lot["code"] == product["code"]:
                row[n + j] = -1
        constraint(row, product["max_weight"] * (equity + funding) - holding[i])
    returns = np.asarray(scenarios)
    initial_profit = returns @ holding
    profit_coefficients = np.zeros((count, size))
    for i, product in enumerate(products):
        profit_coefficients[:, i] = (1 + returns[:, i]) / (1 + product["subscription_fee"]) - 1
    for j, lot in enumerate(lots):
        profit_coefficients[:, n + j] = -returns[:, indices[lot["code"]]] - lot["redemption_fee"]
    for s in range(count):
        row = -profit_coefficients[s].copy()
        row[eta], row[z_start + s] = -1, -1
        constraint(row, initial_profit[s])
    row = np.zeros(size)
    row[eta] = 1
    row[z_start:] = np.asarray(probabilities) / alpha
    constraint(row, risk * (equity + funding))
    objective = -(np.asarray(probabilities) @ profit_coefficients)
    conditions = LinearConstraint(np.asarray(rows), -np.inf, np.asarray(limits))
    def integer_solve(cost, constraints):
        # SciPy forwards these supported HiGHS options. Its default integrality
        # tolerance 1e-6 can admit a fractional buy switch and a sub-minimum buy.
        options = {"time_limit": 30, "mip_rel_gap": 1e-9,
                   "mip_feasibility_tolerance": 1e-10, "primal_feasibility_tolerance": 1e-9}
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Unrecognized options detected:.*",
                                    category=RuntimeWarning)
            return milp(cost, integrality=integrality, bounds=Bounds(lower, upper),
                        constraints=constraints, options=options)

    result = integer_solve(objective, conditions)
    if result.status == 2:
        return None
    if not result.success:
        raise RuntimeError(f"allocation optimum not established: {result.message}")
    # Lexicographic tie break minimizes turnover; it is not a financial weight.
    rows.append(objective)
    limits.append(float(result.fun) + TOL)
    turnover = np.zeros(size)
    turnover[:n + m] = 1
    second = integer_solve(turnover, LinearConstraint(np.asarray(rows), -np.inf, np.asarray(limits)))
    if not second.success:
        raise RuntimeError(f"allocation tie-break optimum not established: {second.message}")
    # Fix only the integer choices, then solve continuous amounts again. Merely
    # rounding an invalid monetary order would not preserve funding or CVaR.
    fixed_lower, fixed_upper = lower.copy(), upper.copy()
    for i, bit in enumerate(second.x[direction:eta]):
        choice = round(float(bit))
        if abs(bit - choice) > 1e-8:
            raise RuntimeError("buy direction is not an integer solver decision")
        fixed_lower[direction + i] = fixed_upper[direction + i] = choice
        if choice:
            fixed_lower[i] = products[i]["min_buy"]
            for j, lot in enumerate(lots):
                if lot["code"] == products[i]["code"]:
                    fixed_upper[n + j] = 0
        else:
            fixed_upper[i] = 0
    polished = linprog(turnover, A_ub=np.asarray(rows), b_ub=np.asarray(limits),
                       bounds=list(zip(fixed_lower, fixed_upper)), method="highs",
                       options={"primal_feasibility_tolerance": 1e-9,
                                "dual_feasibility_tolerance": 1e-9, "time_limit": 30})
    if not polished.success or float(objective @ polished.x) > float(result.fun) + TOL + 1e-9:
        raise RuntimeError("fixed integer allocation could not preserve the optimal objective bound")
    return [float(max(0, x)) for x in polished.x[:n]], [float(max(0, x)) for x in polished.x[n:n + m]]


def _proposal(account, products, lots, scenarios, probabilities, alpha, risk,
              equity, funding, buys, sells, mode):
    indices = {row["code"]: i for i, row in enumerate(products)}
    target = [0.0] * len(products)
    for row in lots:
        target[indices[row["code"]]] += row["value"]
    proceeds, fees = 0.0, 0.0
    sale_legs = []
    for lot, amount in zip(lots, sells):
        if amount <= TOL:
            continue
        if not lot["sellable"] or amount > lot["value"] + TOL:
            raise RuntimeError("invalid lot sale from optimizer")
        fee = amount * lot["redemption_fee"]
        proceeds += amount - fee
        fees += fee
        target[indices[lot["code"]]] -= amount
        sale_legs.append({"lot_id": lot["lot_id"], "code": lot["code"],
                          "gross_value": amount, "fee": fee, "expected_net_proceeds": amount - fee,
                          "settlement_days": lot["settlement_days"], "status": "proposed_not_submitted"})
    free = account["cash"] - account["reserved_cash"]
    original_free = free
    contribution_left, redemption_left = funding, proceeds
    buy_legs = []
    for product, amount in zip(products, buys):
        if amount <= TOL:
            continue
        if (not product["buyable"] or amount + TOL < product["min_buy"]
                or amount > product.get("max_buy", math.inf) + TOL):
            raise RuntimeError("invalid purchase from optimizer")
        if any(leg["code"] == product["code"] for leg in sale_legs):
            raise RuntimeError("same product cannot be bought and sold")
        settled = min(amount, free)
        contribution = min(max(0, amount - settled), contribution_left)
        redemption = max(0, amount - settled - contribution)
        if redemption > redemption_left + TOL:
            raise RuntimeError("purchase has no complete funding source")
        free -= settled
        contribution_left -= contribution
        redemption_left -= redemption
        invested = amount / (1 + product["subscription_fee"])
        fee = amount - invested
        fees += fee
        target[indices[product["code"]]] += invested
        dependencies = []
        if contribution > TOL:
            dependencies.append("awaiting_contribution")
        if redemption > TOL:
            dependencies.append("awaiting_settlement")
        buy_legs.append({"code": product["code"], "cash_debit": amount, "acquired_value": invested,
                         "fee": fee, "status": "proposed_not_submitted",
                         "dependencies": dependencies,
                         "funding": {"settled_cash": settled, "proposed_contribution": contribution,
                                     "expected_redemption_proceeds": redemption},
                         "executable_now": not dependencies})
    cash_target = original_free + funding + proceeds - sum(buys)
    if cash_target < -TOL:
        raise RuntimeError("negative free cash after proposed trades")
    cash_target = max(0, cash_target)
    capital = equity + funding
    wealth = [cash_target + account["reserved_cash"] + account["unsettled_cash"]
              + sum(value * (1 + rate) for value, rate in zip(target, row)) for row in scenarios]
    profits = [value - capital for value in wealth]
    mean_profit = sum(p * value for p, value in zip(probabilities, profits))
    cvar = _cvar([-value for value in profits], probabilities, alpha)
    concentration_ok = all(value <= product["max_weight"] * capital + TOL
                           for value, product in zip(target, products))
    if mode == "baseline":
        kind = "hold_and_keep_proposed_contribution_as_cash" if funding else "hold"
    elif buy_legs and sale_legs:
        kind = "mixed_adjustment" if funding else "internal_reallocation"
    elif buy_legs:
        kind = "additional_funding_purchase" if funding else "purchase_with_available_cash"
    elif sale_legs:
        kind = "reduce_and_keep_cash"
    else:
        kind = "hold"
    return {"mode": mode, "kind": kind, "proposed_contribution": funding,
            "capital_before_trade": capital, "expected_profit": mean_profit,
            "expected_net_return": mean_profit / capital if capital else 0,
            "absolute_cvar": cvar, "cvar_loss_fraction": cvar / capital if capital else 0,
            "fees": fees, "turnover": sum(buys) + sum(sells),
            "risk_feasible": cvar <= risk * capital + TOL,
            "concentration_feasible": concentration_ok,
            "target_positions": [{"code": row["code"], "value": value,
                                  "weight_of_prefee_capital": value / capital if capital else 0}
                                 for row, value in zip(products, target)],
            "target_cash": {"free_after_dependencies": cash_target,
                            "reserved": account["reserved_cash"], "unsettled": account["unsettled_cash"]},
            "sells": sale_legs, "buys": buy_legs,
            "funding_identity": {"available_now": original_free, "proposed_contribution": funding,
                                 "expected_redemption_proceeds": proceeds, "buy_cash_debits": sum(buys),
                                 "remaining_free_cash": cash_target},
            "scenario_profits": profits, "scope": SCOPE,
            "revalidate_at_contribution_and_settlement": bool(funding or sale_legs)}


def compare_allocations(account, assets, distribution, policy):
    """Compare same-budget choices and a cross-budget Pareto set, without IO.

    At each explicit finite funding level, compare holding, unrestricted
    rebalancing, purchases retaining all old positions, and sales retaining cash.
    Minimum advantage is a return fraction, compared only at the same budget.
    The result does not certify data/model qualification or authorize execution.
    """
    (cash, reserved, _, lots, products, scenarios, probabilities, alpha,
     risk, levels, advantage, equity) = _validate(account, assets, distribution, policy)
    groups = []
    for funding in levels:
        baseline = _proposal(account, products, lots, scenarios, probabilities, alpha, risk,
                             equity, funding, [0] * len(products), [0] * len(lots), "baseline")
        candidates = [baseline]
        for mode in ("rebalance", "retain_positions", "sales_only"):
            solution = _solve(lots, products, scenarios, probabilities, cash - reserved, funding,
                              equity, alpha, risk, mode)
            if solution is None:
                continue
            proposal = _proposal(account, products, lots, scenarios, probabilities, alpha, risk,
                                 equity, funding, *solution, mode)
            if not proposal["risk_feasible"] or not proposal["concentration_feasible"]:
                raise RuntimeError("optimizer proposal failed independent constraint reconstruction")
            candidates.append(proposal)
        feasible_baseline = baseline["risk_feasible"] and baseline["concentration_feasible"]
        for item in candidates:
            item["advantage_vs_same_budget_hold"] = item["expected_net_return"] - baseline["expected_net_return"]
            item["eligible"] = item["risk_feasible"] and item["concentration_feasible"] and (
                not feasible_baseline or item is baseline or item["advantage_vs_same_budget_hold"] + 1e-12 >= advantage)
        candidates.sort(key=lambda item: (not item["eligible"], -item["expected_profit"],
                                         item["absolute_cvar"], item["turnover"]))
        for rank, item in enumerate(candidates, 1):
            item["same_budget_rank"] = rank
            item["id"] = f"funding-{funding:g}-{item['mode']}"
        groups.append({"proposed_contribution": funding, "candidates": candidates,
                       "best": next((item for item in candidates if item["eligible"]), None)})
    eligible = [item for group in groups for item in group["candidates"] if item["eligible"]]

    def dominates(left, right):
        a = (-left["expected_net_return"], max(0, left["absolute_cvar"]), left["proposed_contribution"])
        b = (-right["expected_net_return"], max(0, right["absolute_cvar"]), right["proposed_contribution"])
        return all(x <= y + 1e-10 for x, y in zip(a, b)) and any(x < y - 1e-10 for x, y in zip(a, b))

    frontier = [item["id"] for item in eligible if not any(dominates(other, item) for other in eligible)]
    return {"scope": SCOPE, "live_prediction_allowed": False,
            "qualification": "conditional_scenario_only_not_investment_effectiveness_evidence",
            "as_of": account["as_of"], "starting_equity": equity,
            "cash_available_now": cash - reserved, "funding_options": groups,
            "zero_addition_best": groups[0]["best"],
            "additional_funding_alternatives": [group["best"] for group in groups[1:] if group["best"]],
            "cross_budget_frontier": frontier, "cross_budget_unique_winner": None,
            "cross_budget_policy": "Pareto on net return, nonnegative currency CVaR, and proposed contribution; no funding preference supplied",
            "tail_probability": alpha, "max_cvar": risk,
            "probability_basis": "supplied" if "probabilities" in distribution else "uniform_scenario_assumption",
            "execution_precision": "continuous monetary proposals; platform rounding requires revalidation",
            "turnover_tie_break_profit_tolerance_currency": TOL,
            "assumptions": ["Zero interest on cash; no leverage or borrowing",
                            "Joint scenario returns begin after funding and settlement; waiting-period returns are not modeled",
                            "Current value is used for proposed redemption; confirmation NAV and actual proceeds require recomputation",
                            "max_weight denominator is starting equity plus contribution, before upfront fees",
                            "No recommendation is executed; reserved and already unsettled cash cannot fund a buy"]}
