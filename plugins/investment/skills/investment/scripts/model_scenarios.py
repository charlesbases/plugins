"""Conditional fee/availability scenarios; never historical brokerage fills."""

import bisect
import datetime as dt
import hashlib
import itertools
import json
import math

FEATURE_NAMES = [
    "momentum_20_nav_observations", "momentum_60_nav_observations",
    "momentum_120_nav_observations", "volatility_60_nav_observations_annualized_252",
    "drawdown_60_nav_intervals",
]
DEFAULT_SCENARIO = {"subscription_fee": .0015, "redemption_fee": .005,
                    "settlement_calendar_days": 3, "nav_availability_calendar_lag": 2,
                    "research_capital": 10000}
SCOPE = "conditional_fee_scenario"


def _number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite numeric data")
    if value < minimum or (maximum is not None and value >= maximum):
        raise ValueError(f"{name} outside the supported range")
    return float(value)


def _day(value):
    if not isinstance(value, str):
        raise ValueError("Date must be an ISO calendar date")
    day = dt.date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("Date must be an ISO calendar date")
    return day


def _scenario(value=None):
    result = dict(DEFAULT_SCENARIO)
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(result):
            raise ValueError("Unknown scenario parameters")
        result.update(value)
    for key in ("subscription_fee", "redemption_fee"):
        result[key] = _number(result[key], key, maximum=1)
    for key in ("settlement_calendar_days", "nav_availability_calendar_lag"):
        if type(result[key]) is not int or result[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if _number(result["research_capital"], "research_capital") <= 0:
        raise ValueError("research_capital must be positive")
    return result


def scenario_id(scenario):
    return hashlib.sha256(json.dumps(_scenario(scenario), sort_keys=True).encode()).hexdigest()[:16]


def scenario_grid():
    """A frozen 18-case sensitivity grid; parameters are assumptions."""
    return [dict(DEFAULT_SCENARIO, subscription_fee=s, redemption_fee=r,
                 settlement_calendar_days=days)
            for s, r, days in itertools.product((0, .0015, .015), (0, .005), (0, 3, 7))]


def _series(nav_by_code):
    result = {}
    if not isinstance(nav_by_code, dict) or not nav_by_code:
        raise ValueError("Nonempty normalized NAV data required")
    for code, rows in nav_by_code.items():
        if not isinstance(code, str) or not rows:
            raise ValueError("Fund identity and nonempty NAV history required")
        days, values = [], {}
        for row in rows:
            day = _day(row["date"])
            if row.get("code", code) != code or (days and day <= days[-1]):
                raise ValueError("NAV identity or chronology is invalid")
            nav = _number(row["nav"], "NAV")
            dividend = _number(row["distribution_per_share"], "distribution_per_share")
            if nav <= 0:
                raise ValueError("NAV must be positive")
            days.append(day)
            values[day] = (nav, dividend)
        result[code] = {"days": days, "prices": values,
                        "execution_days": [d for d in days if d.weekday() < 5]}
    return result


def _at_or_after(days, day, strictly=False):
    index = bisect.bisect_right(days, day) if strictly else bisect.bisect_left(days, day)
    return days[index] if index < len(days) else None


def _losses(wealth):
    peak, drawdown = wealth[0], 0.0
    for value in wealth:
        if not math.isfinite(value) or value < 0:
            raise ValueError("Invalid wealth path")
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    net = wealth[-1] / wealth[0] - 1
    return {"net_return": net, "terminal_loss": max(-net, 0),
            "principal_path_loss": max(1 - min(wealth) / wealth[0], 0),
            "peak_drawdown": drawdown}


def build_samples(nav_by_code, features_by_code, code_info, horizons, scenario=None):
    """Use cached past features and future prices only to construct labels."""
    scenario = _scenario(scenario)
    series, samples = _series(nav_by_code), []
    if not horizons or any(type(h) is not int or h <= 0 for h in horizons) or len(set(horizons)) != len(horizons):
        raise ValueError("Distinct positive calendar horizons required")
    assumptions = {"scenario": scenario, "fees_and_timing": "assumed_not_historical_facts",
                   "entry_and_exit": "observed_weekday_NAV_proxies_not_buyability",
                   "dividends": "cash_entitlements_no_interest_no_payment_date_proof",
                   "historical_published_at": None, "strict_PIT_verified": False}
    for code in sorted(series):
        history = series[code]
        group = code_info.get(code, {}).get("fund_group_id")
        if not isinstance(group, str) or not group.strip():
            raise ValueError("Known nonempty fund_group_id required")
        previous = None
        for feature in features_by_code.get(code, []):
            cutoff = _day(feature["feature_cutoff_nav_date"])
            if (feature.get("code") != code or cutoff not in history["prices"]
                    or (previous is not None and cutoff <= previous)
                    or _day(feature["known_max_nav_date"]) > cutoff):
                raise ValueError("Feature identity, cutoff, or chronology is invalid")
            previous = cutoff
            x = [_number(feature["values"][key], key, minimum=-math.inf) for key in FEATURE_NAMES]
            decision = cutoff + dt.timedelta(days=scenario["nav_availability_calendar_lag"])
            entry = _at_or_after(history["execution_days"], decision, strictly=True)
            for horizon in horizons:
                exit_day = _at_or_after(history["execution_days"], entry + dt.timedelta(days=horizon)) if entry else None
                available = (exit_day + dt.timedelta(days=max(scenario["nav_availability_calendar_lag"],
                                                             scenario["settlement_calendar_days"]))) if exit_day else None
                y = None
                if exit_day:
                    shares = 1 / ((1 + scenario["subscription_fee"]) * history["prices"][entry][0])
                    cash, wealth = 0.0, [1.0, shares * history["prices"][entry][0]]
                    start = bisect.bisect_right(history["days"], entry)
                    end = bisect.bisect_right(history["days"], exit_day)
                    for day in history["days"][start:end]:
                        nav, dividend = history["prices"][day]
                        cash += shares * dividend
                        wealth.append(shares * nav + cash)
                    wealth[-1] -= shares * history["prices"][exit_day][0] * scenario["redemption_fee"]
                    y = _losses(wealth)
                samples.append({"id": f"{code}:{cutoff}:{horizon}:{scenario_id(scenario)}", "code": code,
                                "fund_group_id": group, "cutoff_date": cutoff.isoformat(),
                                "decision_date": decision.isoformat(), "entry_date": entry.isoformat() if entry else None,
                                "exit_date": exit_day.isoformat() if exit_day else None,
                                "label_available_proxy": available.isoformat() if available else None,
                                "horizon_days": horizon, "x": x, "y": y,
                                "status": "mature" if y is not None else "pending", "scope": SCOPE,
                                "assumptions": dict(assumptions)})
    return samples


def _simulate(series, decisions, horizon, scenario, start, end, strategy, fixed_code=None):
    """Daily cash/positions/receivables book; no labels are inspected."""
    capital = float(scenario["research_capital"])
    cash, orders, positions, unsettled = capital, [], {}, []
    ledger, wealth, fees, turnover, deferred, unexecuted = [], [], 0.0, 0.0, 0, 0
    buy_hold = strategy in ("equal_weight_buy_hold", "single_fund_buy_hold")
    codes = [fixed_code] if fixed_code else sorted(series)
    day = start

    def check_book(expected=None):
        parts = {"cash": cash, "reserved_cash": sum(o["amount"] for o in orders),
                 "holdings_NAV": sum(p["shares"] * p["nav"] for p in positions.values()),
                 "dividend_receivable": sum(p["dividends"] for p in positions.values()),
                 "unsettled_cash": sum(p["amount"] for p in unsettled)}
        total = sum(parts.values())
        if (any(not math.isfinite(v) or v < -capital * 1e-10 for v in parts.values()) or not math.isfinite(total) or total <= 0
                or (expected is not None and not math.isclose(total, expected, rel_tol=1e-12, abs_tol=1e-9))):
            raise ValueError("Cash conservation or finite wealth invariant failed")
        return dict(parts, wealth=total)

    def settle(today):
        nonlocal cash, unsettled
        for payment in list(unsettled):
            if payment["date"] <= today:
                before = check_book()["wealth"]
                cash += payment["amount"]
                unsettled.remove(payment)
                check_book(before)
                ledger.append({"event": "settle", "date": str(today), "code": payment["code"],
                               "amount": payment["amount"], "cash_after": cash})

    while day <= end:
        settle(day)
        for order in list(orders):
            if order["entry"] != day:
                continue
            before = check_book()["wealth"]
            code, budget = order["code"], order["amount"]
            nav = series[code]["prices"][day][0]
            gross = budget / (1 + scenario["subscription_fee"])
            fee, shares = budget - gross, gross / nav
            fees += fee
            turnover += gross
            positions[code] = {"shares": shares, "nav": nav, "dividends": 0.0,
                               "entry": day, "exit": order["exit"]}
            orders.remove(order)
            check_book(before - fee)
            ledger.append({"event": "buy", "date": str(day), "decision_date": str(order["decision"]),
                           "code": code, "amount": budget, "shares": shares, "nav": nav,
                           "fee": fee, "cash_after": cash, "fill_kind": "assumed_NAV_proxy"})
        for code, position in list(positions.items()):
            if day in series[code]["prices"]:
                nav, dividend = series[code]["prices"][day]
                position["nav"] = nav
                if day > position["entry"] and dividend:
                    before = check_book()["wealth"]
                    amount = position["shares"] * dividend
                    position["dividends"] += amount
                    check_book(before + amount)
                    ledger.append({"event": "dividend_receivable", "date": str(day), "code": code,
                                   "amount": amount, "payment_date_verified": False})
            if position["exit"] == day:
                before = check_book()["wealth"]
                gross = position["shares"] * position["nav"]
                fee = gross * scenario["redemption_fee"]
                amount = gross - fee + position["dividends"]
                due = day + dt.timedelta(days=scenario["settlement_calendar_days"])
                fees += fee
                turnover += gross
                unsettled.append({"code": code, "date": due, "amount": amount})
                del positions[code]
                check_book(before - fee)
                ledger.append({"event": "sell", "date": str(day), "code": code,
                               "shares": position["shares"], "nav": position["nav"], "fee": fee,
                               "dividend_receivable": position["dividends"], "amount": amount,
                               "assumed_cash_date": str(due), "cash_after": cash,
                               "fill_kind": "assumed_NAV_proxy"})
        settle(day)  # A zero-day settlement is an explicit scenario, never observed truth.
        if day in decisions and (not buy_hold or day == start):
            if positions or orders or unsettled:
                deferred += 1
                ledger.append({"event": "deferred", "date": str(day), "reason": "capital_committed_or_unsettled"})
            else:
                selected = codes
                if strategy == "model":
                    ranked = sorted(decisions[day], key=lambda row: (-row["predicted"], row["code"]))
                    selected = [ranked[0]["code"]] if ranked and ranked[0]["predicted"] > 0 else []
                if not selected:
                    unexecuted += 1
                    ledger.append({"event": "no_order", "date": str(day), "reason": "nonpositive_prediction"})
                budget = cash / len(selected) if selected else 0
                for code in selected:
                    before = check_book()["wealth"]
                    execution_days = series[code]["execution_days"]
                    entry = _at_or_after(execution_days, day, strictly=True)
                    if entry is None or entry > end:
                        entry, exit_day = None, None
                    elif buy_hold:
                        index = bisect.bisect_right(execution_days, end) - 1
                        exit_day = execution_days[index] if index >= 0 else None
                    else:
                        exit_day = _at_or_after(execution_days, entry + dt.timedelta(days=horizon))
                        if exit_day is not None and exit_day > end:
                            exit_day = None
                    # Cash remains reserved while a future order has no observed price.
                    cash -= budget
                    orders.append({"code": code, "amount": budget, "entry": entry,
                                   "exit": exit_day, "decision": day})
                    check_book(before)
                    ledger.append({"event": "order", "date": str(day), "code": code, "amount": budget,
                                   "entry_date": str(entry) if entry else None, "cash_after": cash})
                    if entry is None:
                        unexecuted += 1
        wealth.append(dict(check_book(), date=str(day)))
        day += dt.timedelta(days=1)
    losses = _losses([capital] + [row["wealth"] for row in wealth])
    return {"strategy": strategy, "code": fixed_code, "initial_capital": capital, "metrics": dict(losses,
            turnover=turnover / capital, fees=fees, final_wealth=wealth[-1]["wealth"], final_cash=cash),
            "turnover_definition": "sum_subscription_NAV_and_redemption_NAV_over_initial_capital",
            "orders_deferred": deferred, "orders_not_executed": unexecuted,
            "pending": {"orders": len(orders), "positions": len(positions), "settlements": len(unsettled)},
            "trade_ledger": ledger, "period_wealth": wealth,
            "cash_constraint_checked": True, "strict_historical_execution_verified": False}


def replay(nav_by_code, samples, predictions, policy=None):
    """Replay frozen test predictions; sensitivity scenarios never refit models.

    policy may supply scenario, dataset_end, and model (when predictions include
    multiple model names). Daily NAV rows are price proxies, not buyability proof.
    """
    policy = policy or {}
    scenario = _scenario(policy.get("scenario"))
    series = _series(nav_by_code)
    selected = [p for p in predictions if p.get("target") == "net_return" and p.get("split") == "test"
                and ("model" not in policy or p.get("model") == policy["model"])]
    valid_samples = {(s["code"], s["decision_date"], s["horizon_days"]): s for s in samples}
    if len(valid_samples) != len(samples):
        raise ValueError("Replay requires one nominal sample per fund, date, and horizon")
    grouped, identities, groups = {}, set(), {}
    for prediction in selected:
        key = (prediction["code"], prediction["decision_date"], prediction["horizon_days"])
        if key not in valid_samples or key in identities or key[0] not in series:
            raise ValueError("Prediction identity is missing or duplicated; select one frozen model")
        identities.add(key)
        sample = valid_samples[key]
        if not sample.get("fund_group_id"):
            raise ValueError("Known fund group required")
        groups[key[0]] = sample["fund_group_id"]
        horizon = key[2]
        if type(horizon) is not int or horizon <= 0:
            raise ValueError("Positive integer horizon required")
        day = _day(key[1])
        value = _number(prediction["predicted"], "predicted", minimum=-math.inf)
        grouped.setdefault(horizon, {}).setdefault(day, []).append({"code": key[0], "predicted": value})
    end = min(history["days"][-1] for history in series.values())
    if "dataset_end" in policy:
        end = min(end, _day(policy["dataset_end"]))
    result = {"scope": SCOPE, "scenario_id": scenario_id(scenario), "scenario": scenario,
              "groups": groups, "dataset_end": str(end), "horizons": {},
              "assumptions": {"fees_and_timing": "assumed_not_historical_facts",
                              "strategy_predictions": "frozen_nominal_model_no_sensitivity_refitting",
                              "dividend_cash": "entitlements_locked_until_assumed_redemption_settlement",
                              "trades": "weekday_NAV_proxies_not_actual_brokerage_fills",
                              "valuation": "last_observed_NAV_plus_cash_and_receivables",
                              "terminal_positions": "fixed_horizon_positions_remain_open_until_their_exit",
                              "strict_PIT_verified": False}}
    for horizon, all_decisions in sorted(grouped.items()):
        decisions = {day: values for day, values in all_decisions.items() if day <= end}
        if not decisions:
            result["horizons"][str(horizon)] = {"status": "pending_no_test_prices", "strategies": {}}
            continue
        start = min(decisions)
        strategies = {name: _simulate(series, decisions, horizon, scenario, start, end, name)
                      for name in ("model", "periodic_equal_weight", "equal_weight_buy_hold")}
        for code in sorted(series):
            strategies["buy_hold_" + code] = _simulate(series, decisions, horizon, scenario, start, end,
                                                      "single_fund_buy_hold", code)
        model = {row["date"]: row["wealth"] for row in strategies["model"]["period_wealth"]}
        periodic = {row["date"]: row["wealth"] for row in strategies["periodic_equal_weight"]["period_wealth"]}
        blocks, block_start = [], start
        while block_start < end:
            block_end = min(block_start + dt.timedelta(days=horizon), end)
            left, right = str(block_start), str(block_end)
            model_return, benchmark_return = model[right] / model[left] - 1, periodic[right] / periodic[left] - 1
            blocks.append({"start": left, "end": right, "model_return": model_return,
                           "periodic_equal_weight_return": benchmark_return,
                           "paired_gain": model_return - benchmark_return})
            block_start = block_end
        result["horizons"][str(horizon)] = {"status": "conditional_replay", "start": str(start),
                                           "strategies": strategies, "paired_date_blocks": blocks}
    return result
