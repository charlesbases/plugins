"""Auditable conditional Elastic Net/CVaR research; never book proposed trades."""

import bisect
import copy
import datetime as dt
import hashlib
import json
import math
import os
import uuid
from pathlib import Path

import model_scenarios as market
import research
from storage import read_object, update_latest, write_small_json

SOURCE_FILES = ("allocation_runner.py", "allocation.py", "allocation_statistics.py",
                "model.py", "model_stats.py", "storage.py",
                "model_scenarios.py", "research.py", "research_data.py", "research_audit.py",
                "requirements-model.txt")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value, name, minimum=0):
    require(type(value) in (int, float) and math.isfinite(value) and value >= minimum,
            name + " must be finite and at least " + str(minimum))
    return float(value)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def source_hashes():
    folder = Path(__file__).resolve().parent
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def prepare_samples(nav, features, code_info, horizon, availability_lag):
    """Operating fees remain in NAV; investor transaction fees belong to the book."""
    require(type(horizon) is int and horizon > 0, "Positive horizon_days required")
    require(type(availability_lag) is int and availability_lag >= 0,
            "Explicit nonnegative availability lag required")
    original = market.build_samples(nav, features, code_info, [horizon], {
        "subscription_fee": 0, "redemption_fee": 0, "settlement_calendar_days": 0,
        "nav_availability_calendar_lag": availability_lag})
    return [{"decision_date": r["decision_date"], "label_available_date": r["label_available_proxy"],
             "code": r["code"], "fund_group_id": r["fund_group_id"], "x": r["x"],
             "return": None if r["y"] is None else r["y"]["net_return"],
             "horizon_days": r["horizon_days"], "entry_date": r["entry_date"],
             "exit_date": r["exit_date"], "feature_cutoff_date": r["cutoff_date"]}
            for r in original]


def prediction_rows(samples, codes, as_of, maximum_age):
    day = dt.date.fromisoformat(as_of)
    require(type(maximum_age) is int and maximum_age >= 0, "Explicit max_feature_age_days required")
    output = []
    for code in codes:
        available = [r for r in samples if r["code"] == code and r["decision_date"] <= as_of]
        require(available, "No available feature for " + code)
        latest = max(available, key=lambda r: r["decision_date"])
        require((day - dt.date.fromisoformat(latest["decision_date"])).days <= maximum_age,
                "Feature is stale for " + code)
        output.append({"code": code, "decision_date": as_of, "x": latest["x"],
                       "horizon_days": latest["horizon_days"],
                       "feature_cutoff_date": latest["feature_cutoff_date"]})
    return output


def fit_at(samples, codes, as_of, request):
    from allocation_statistics import fit_predict
    rows = prediction_rows(samples, codes, as_of, request["max_feature_age_days"])
    policy = dict(request["training_policy"])
    require(policy.get("feature_names", market.FEATURE_NAMES) == market.FEATURE_NAMES,
            "Training feature order differs from the NAV adapter")
    policy["feature_names"] = list(market.FEATURE_NAMES)
    return fit_predict(samples, rows, policy)


def _price(history, day):
    index = bisect.bisect_right(history["days"], day) - 1
    require(index >= 0, "No NAV on or before " + str(day))
    return history["prices"][history["days"][index]][0]


def _fee(asset, acquired, day):
    """A fee schedule is supplied evidence/assumption, never inferred from NAV."""
    schedule = asset.get("redemption_schedule")
    if schedule is None:
        fee = number(asset["redemption_fee"], "redemption_fee")
        require(fee < 1, "Redemption fee must be below one")
        return fee
    require(isinstance(schedule, list) and schedule, "Nonempty redemption_schedule required")
    age = (day - acquired).days
    previous = -1
    chosen = None
    for band in schedule:
        start = band["minimum_days"]
        require(type(start) is int and start >= 0 and start > previous, "Unordered redemption schedule")
        rate = number(band["rate"], "redemption fee")
        require(rate < 1, "Redemption fee must be below one")
        if age >= start:
            chosen = rate
        previous = start
    require(chosen is not None, "No redemption fee for this holding age")
    return chosen


class ReplayBook:
    """Calendar-event ledger. Decision NAV availability and execution NAV are distinct."""

    def __init__(self, account, assets, series, lag, contribution):
        self.series, self.assets, self.lag = series, {a["code"]: a for a in assets}, lag
        self.day = dt.date.fromisoformat(account["as_of"])
        require(account["reserved_cash"] == 0 and account["unsettled_cash"] == 0,
                "Replay initial snapshot requires no unspecified pending transactions")
        self.cash = number(account["cash"], "cash") + contribution
        self.lots, self.orders, self.receivables, self.events = {}, [], [], []
        self.initial = self.cash
        self.sequence = 0
        for lot in account["positions"]:
            require(isinstance(lot["lot_id"], str) and lot["lot_id"] and lot["lot_id"] not in self.lots,
                    "Unique nonempty initial lot_id required")
            acquired = dt.date.fromisoformat(lot["acquired_date"])
            require(acquired <= self.day, "Holding acquisition follows replay start")
            value = number(lot["value"], "holding value")
            self.lots[lot["lot_id"]] = dict(lot, shares=value / _price(series[lot["code"]], self.day),
                                            acquired_date=acquired, confirmed=self.day, reserved=0.)
            self.initial += value
        require(self.initial > 0, "Positive replay equity required")
        self.events.append({"date": str(self.day), "event": "hypothetical_initial_contribution",
                            "amount": contribution, "is_investment_profit": False})

    def value(self, day):
        return (self.cash + sum(o["amount"] for o in self.orders if o["kind"] == "buy")
                + sum(r["amount"] for r in self.receivables)
                + sum(l["shares"] * _price(self.series[l["code"]], day) for l in self.lots.values()))

    def advance(self, day):
        self.day = day
        # Ex-date cash entitlement is an assumption of the NAV adapter, not a paid-date fact.
        for lot in self.lots.values():
            observation = self.series[lot["code"]]["prices"].get(day)
            if observation:
                amount = lot["shares"] * observation[1]
                if amount:
                    self.receivables.append({"amount": amount,
                        "due": day + dt.timedelta(days=self.lag),
                        "known_at": day + dt.timedelta(days=self.lag), "estimated_amount": 0.})
        remaining = []
        for order in self.orders:
            if order["fill"] != day:
                remaining.append(order)
                continue
            asset, code = self.assets[order["code"]], order["code"]
            nav = self.series[code]["prices"][day][0]
            if order["kind"] == "sell":
                lot = self.lots[order["lot_id"]]
                gross = order["shares"] * nav
                fee = gross * _fee(asset, lot["acquired_date"], day)
                lot["shares"] -= order["shares"]
                lot["reserved"] -= order["shares"]
                due = day + dt.timedelta(days=asset["settlement_days"])
                estimated = order["shares"] * _price(self.series[code], day - dt.timedelta(days=self.lag))
                estimated *= 1 - _fee(asset, lot["acquired_date"], day)
                self.receivables.append({"amount": gross - fee, "due": due,
                    "known_at": day + dt.timedelta(days=self.lag), "estimated_amount": estimated})
                if lot["shares"] < 1e-9:
                    del self.lots[order["lot_id"]]
            else:
                gross = order["amount"] / (1 + asset["subscription_fee"])
                fee = order["amount"] - gross
                ident = "sim-" + str(self.sequence)
                while ident in self.lots:
                    self.sequence += 1
                    ident = "sim-" + str(self.sequence)
                self.sequence += 1
                self.lots[ident] = {"lot_id": ident, "code": code, "shares": gross / nav,
                    "acquired_date": day, "confirmed": day + dt.timedelta(days=self.lag),
                    "known_invested_value": gross, "reserved": 0., "sellable": True}
            self.events.append({"date": str(day), "event": "assumed_" + order["kind"] + "_fill",
                                "code": code, "gross": gross, "fee": fee})
        self.orders = remaining
        unpaid = []
        for receipt in self.receivables:
            if receipt["due"] <= day:
                self.cash += receipt["amount"]
                self.events.append({"date": str(day), "event": "assumed_settlement", "amount": receipt["amount"]})
            else:
                unpaid.append(receipt)
        self.receivables = unpaid
        require(self.cash >= -1e-7, "Negative simulated cash")

    def snapshot(self, day):
        known = day - dt.timedelta(days=self.lag)
        lots = []
        for lot in self.lots.values():
            value = (lot["known_invested_value"] if lot["confirmed"] > day else
                     lot["shares"] * _price(self.series[lot["code"]], known))
            # A partially reserved lot is separated so free shares are not oversold.
            available = max(0., lot["shares"] - lot["reserved"])
            for suffix, shares, can_sell in (("", available, lot["confirmed"] <= day and lot["sellable"]),
                                             ("-reserved", lot["reserved"], False)):
                if shares <= 1e-12:
                    continue
                lots.append({"lot_id": lot["lot_id"] + suffix, "code": lot["code"],
                    "value": value * shares / lot["shares"], "sellable": can_sell,
                    "redemption_fee": _fee(self.assets[lot["code"]], lot["acquired_date"], day),
                    "settlement_days": self.assets[lot["code"]]["settlement_days"]})
        reserved = sum(o["amount"] for o in self.orders if o["kind"] == "buy")
        return {"as_of": str(day), "cash": self.cash + reserved, "reserved_cash": reserved,
                "unsettled_cash": sum(r["amount"] if r["known_at"] <= day else r["estimated_amount"]
                                      for r in self.receivables), "positions": lots}

    def submit(self, candidate, day):
        """Only current settled cash funds buys; future target legs are reconsidered later."""
        trades = [dict(t, action="sell", amount=t["gross_value"]) for t in candidate["sells"]]
        trades += [dict(t, action="buy", amount=t["cash_debit"]) for t in candidate["buys"]]
        for trade in trades:
            code = trade["code"]
            fill = market._at_or_after(self.series[code]["execution_days"], day, strictly=True)
            if fill is None:
                continue
            if trade["action"] == "sell":
                ident = trade["lot_id"]
                require(ident in self.lots, "Sale refers to a reserved or unknown lot")
                lot = self.lots[ident]
                known_price = _price(self.series[code], day - dt.timedelta(days=self.lag))
                shares = trade["amount"] / known_price
                require(shares <= lot["shares"] - lot["reserved"] + 1e-7, "Sale exceeds free shares")
                lot["reserved"] += shares
                self.orders.append({"kind": "sell", "code": code, "lot_id": ident,
                                    "shares": shares, "fill": fill})
            elif trade["action"] == "buy":
                if not trade["executable_now"]:
                    continue
                amount = trade["amount"]
                require(amount <= self.cash + 1e-7, "Purchase exceeds settled available cash")
                if amount < self.assets[code]["min_buy"] or amount <= 1e-8:
                    continue
                self.cash -= amount
                self.orders.append({"kind": "buy", "code": code, "amount": amount, "fill": fill})
            else:
                raise ValueError("Unknown allocation action")
            self.events.append({"date": str(day), "event": "simulated_order", "trade": trade,
                                "fill_date": str(fill), "cash_remaining": self.cash})


def validate_request(request, codes, command):
    require(request.get("schema_version") == 1, "Allocation request schema_version must be 1")
    require(set(a["code"] for a in request["assets"]) == set(codes),
            "Assets must exactly match the prepared research universe")
    require(request["assumptions"]["transaction_terms"] in ("assumed", "documented_rule_simulation"),
            "This research adapter supports assumptions/documented rules, not actual fills")
    require(isinstance(request["assumptions"]["evidence"], str) and request["assumptions"]["evidence"].strip(),
            "Describe the actual sources or conditional assumptions")
    require(request["account"]["as_of"] == request["as_of"], "Account date and decision date differ")
    dt.date.fromisoformat(request["as_of"])
    if command == "allocation-validate":
        evaluation = request["evaluation"]
        start, end = map(dt.date.fromisoformat, (evaluation["start"], evaluation["end"]))
        require(start <= end and str(start) == request["as_of"], "Replay account must be valued at evaluation start")
        require(type(evaluation["decision_interval_days"]) is int and evaluation["decision_interval_days"] > 0,
                "Explicit positive decision interval required")
        require(evaluation["role"] in ("development", "preregistered_forward"), "Unknown evaluation role")
        for asset in request["assets"]:
            require(type(asset["settlement_days"]) is int and asset["settlement_days"] >= 0,
                    "Explicit nonnegative calendar settlement assumption required")
            _fee(asset, start, start)


def compute(command, request, nav, features, code_info):
    from allocation import compare_allocations
    from allocation_statistics import evaluate_advantage, evaluate_tail_risk
    codes = sorted(nav)
    validate_request(request, codes, command)
    lag = request["assumptions"]["nav_availability_calendar_lag"]
    samples = prepare_samples(nav, features, code_info, request["horizon_days"], lag)
    if command == "allocate":
        fitted = fit_at(samples, codes, request["as_of"], request)
        if fitted["status"] != "research_ready":
            return {"status": "insufficient_evidence", "fitting": fitted, "live_prediction_allowed": False}
        comparison = compare_allocations(request["account"], request["assets"],
                                         fitted["joint_scenarios"], request["allocation_policy"])
        return {"status": "research_ready", "fitting": fitted, "comparison": comparison,
                "live_prediction_allowed": False, "actual_account_changed": False}
    evaluation = request["evaluation"]
    start, end = map(dt.date.fromisoformat, (evaluation["start"], evaluation["end"]))
    series = market._series(nav)
    require(all(end <= history["days"][-1] for history in series.values()), "Evaluation end exceeds observed NAV")
    decisions = []
    for offset in range((end - start).days + 1):
        day = start + dt.timedelta(days=offset)
        if day.weekday() < 5 and offset % evaluation["decision_interval_days"] == 0:
            decisions.append(day)
    require(len(decisions) <= evaluation["max_decisions"], "Evaluation exceeds declared computation budget")
    # Fit once per decision date across all funding alternatives; account state cannot affect training.
    forecasts = {str(day): fit_at(samples, codes, str(day), request) for day in decisions}
    outcomes = []
    for contribution in request["allocation_policy"]["funding_levels"]:
        book = ReplayBook(request["account"], request["assets"], series, lag, contribution)
        baseline = ReplayBook(request["account"], request["assets"], series, lag, contribution)
        wealth, comparisons, previous, base_previous = [], [], book.initial, baseline.initial
        for offset in range((end - start).days + 1):
            day = start + dt.timedelta(days=offset)
            # Initial account valuation already includes this day's ex-date effects.
            if day > start:
                book.advance(day)
                baseline.advance(day)
            fitted = forecasts.get(str(day))
            if fitted and fitted["status"] == "research_ready":
                policy = dict(request["allocation_policy"], funding_levels=[0])
                comparison = compare_allocations(book.snapshot(day), request["assets"], fitted["joint_scenarios"], policy)
                chosen = comparison["zero_addition_best"]
                if chosen is not None:
                    book.submit(chosen, day)
                comparisons.append({"decision_date": str(day), "comparison": comparison})
            value, base_value = book.value(day), baseline.value(day)
            require(previous > 0 and base_previous > 0, "Nonpositive portfolio value")
            wealth.append({"date": str(day), "wealth": value, "baseline_wealth": base_value,
                           "net_return": value / previous - 1, "baseline_return": base_value / base_previous - 1,
                           "gain": value / previous - base_value / base_previous})
            previous, base_previous = value, base_value
        # NAV-less weekends repeat values; the paired series uses actual common valuation dates.
        observations = [r for r in wealth if all(dt.date.fromisoformat(r["date"]) in s["prices"] for s in series.values())]
        # Recompute across selected valuation dates so skipped-day cash events are not lost.
        gains, dates = [], []
        prev, base_prev = book.initial, baseline.initial
        for row in observations:
            gains.append(row["wealth"] / prev - row["baseline_wealth"] / base_prev)
            dates.append(row["date"])
            prev, base_prev = row["wealth"], row["baseline_wealth"]
        statistical = copy.deepcopy(request["validation_policy"])
        # Historical choices already inspected and multiple funding alternatives cannot be certified as one sealed trial.
        # A request flag is not proof of a forecast captured before its outcome.
        # This historical NAV adapter never upgrades itself into a prospective trial.
        statistical["preregistered_single_strategy"] = False
        advantage = evaluate_advantage(dates, gains, statistical)
        losses, loss_dates = [], []
        all_days = [dt.date.fromisoformat(r["date"]) for r in observations]
        for i, row in enumerate(observations):
            j = bisect.bisect_left(all_days, all_days[i] + dt.timedelta(days=request["horizon_days"]))
            if j < len(observations):
                losses.append(1 - observations[j]["wealth"] / row["wealth"])
                loss_dates.append(row["date"])
        risk_policy = copy.deepcopy(request["risk_validation_policy"])
        risk_policy["preregistered_single_strategy"] = False
        risk = evaluate_tail_risk(loss_dates, losses, request["allocation_policy"]["tail_probability"],
                                  request["allocation_policy"]["max_cvar"], risk_policy)
        outcomes.append({"funding_amount": contribution, "initial_equity_including_contribution": book.initial,
            "net_profit": wealth[-1]["wealth"] - book.initial, "net_return": wealth[-1]["wealth"] / book.initial - 1,
            "maximum_drawdown": market._losses([book.initial] + [r["wealth"] for r in wealth])["peak_drawdown"],
            "advantage": advantage, "risk": risk, "wealth": wealth, "decisions": comparisons, "events": book.events})
    return {"status": "evaluated", "funding_evaluations": outcomes, "fitting_by_date": forecasts,
            "scope": "conditional_daily_replay_not_verified_historical_brokerage_fills",
            "live_prediction_allowed": False, "actual_account_changed": False,
            "baseline": "initial_positions_held_plus_same_contribution_kept_as_cash",
            "qualification": "Current NAV adapter lacks historical investor and publication evidence"}


def save_result(run, result):
    import model
    summary = {k: v for k, v in result.items() if k not in ("fitting_by_date", "funding_evaluations")}
    if "fitting_by_date" in result:
        model.rows_write(run / "fitting", [{"decision_date": date, "fitting": value}
                         for date, value in sorted(result["fitting_by_date"].items())])
        summary["fitting_partitioned"] = True
    if "funding_evaluations" in result:
        summary["funding_evaluations"] = []
        for i, outcome in enumerate(result["funding_evaluations"]):
            entry = {k: v for k, v in outcome.items() if k not in ("wealth", "events", "decisions")}
            entry["partition"] = "funding-" + str(i)
            for name in ("wealth", "events", "decisions"):
                model.rows_write(run / entry["partition"] / name, outcome[name],
                                 field="decision_date" if name == "decisions" else "date")
            summary["funding_evaluations"].append(entry)
    # The daily allocation result is bounded by the request's universe and model trials.
    model.rows_write(run / "summary", [{"decision_date": result["as_of"], "summary": summary}])


def load_result(run):
    import model
    records = model.rows_read(run / "summary")
    require(len(records) == 1, "Missing allocation summary")
    result = records[0]["summary"]
    if result.pop("fitting_partitioned", False):
        result["fitting_by_date"] = {r["decision_date"]: r["fitting"] for r in model.rows_read(run / "fitting")}
    for i, outcome in enumerate(result.get("funding_evaluations", [])):
        require(outcome.pop("partition") == "funding-" + str(i), "Unexpected allocation partition")
        for name in ("wealth", "events", "decisions"):
            outcome[name] = model.rows_read(run / ("funding-" + str(i)) / name)
    return result


def execute(command, root, plan, request_path=None, data_reference=None, reference=None):
    """Persist research only. Verification independently re-executes the frozen input."""
    import model
    root, base, _ = research.select_plan(root, plan)
    require(base is not None, "Prepare market research data first")
    _, versions = model.runtime(root)
    lock = research.owned_path(base, ".allocation.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if command == "allocation-verify":
            require(reference, "allocation-verify requires --run")
            run = research.owned_path(base, reference)
            manifest = read_object(run / "manifest.json")
            require(manifest["source_hashes"] == source_hashes(), "Allocation implementation changed")
            require(manifest["dependencies"] == versions, "Allocation dependencies changed")
            request = read_object(run / "request.json")
            require(fingerprint(request) == manifest["request_hash"], "Allocation request changed")
            data_reference, operation = manifest["data_run"], manifest["operation"]
        else:
            require(request_path is not None, "Allocation commands require --request")
            require(Path(request_path).stat().st_size <= 1024 * 1024, "Request exceeds 1 MiB")
            request, operation = read_object(Path(request_path)), command
        data_run, audit, dm, dp, nav, features = model.load_data(root, base, data_reference)
        before = research.protection(root, base)
        frozen_source = source_hashes()
        result = compute(operation, request, nav, features, dp["code_info"])
        result["as_of"] = request["as_of"]
        require(source_hashes() == frozen_source, "Source changed during allocation computation; rerun from a fixed version")
        require(before == research.protection(root, base), "Allocation calculation changed account files")
        result["data_qualification"] = audit["readiness"]
        if command == "allocation-verify":
            require(model.sha(data_run / "manifest.json") == manifest["data_manifest_hash"], "Market lineage changed")
            saved = load_result(run)
            require(fingerprint(saved) == manifest["result_hash"], "Allocation result changed")
            require(fingerprint(result) == manifest["result_hash"], "Recomputed allocation differs")
            return {"verification": "passed", "run_path": reference, "live_prediction_allowed": False}
        now = dt.datetime.now(research.CN)
        relative = "research/" + now.strftime("%Y/%m/%d") + "/allocation-" + uuid.uuid4().hex[:12]
        run = research.owned_path(base, relative)
        run.mkdir(parents=True)
        write_small_json(run / "request.json", request)
        save_result(run, result)
        manifest = {"schema_version": 1, "operation": operation, "request_hash": fingerprint(request),
            "recorded_at": now.isoformat(), "decision_as_of": request["as_of"],
            "registration_scope": "contemporaneous_conditional" if request["as_of"] == now.date().isoformat() else "retrospective_research",
            "result_hash": fingerprint(result), "data_run": data_run.relative_to(base).as_posix(),
            "data_manifest_hash": model.sha(data_run / "manifest.json"), "source_hashes": frozen_source,
            "dependencies": versions, "actual_account_changed": False}
        write_small_json(run / "manifest.json", manifest)
        update_latest(root, base.name, {"allocation_research": {"run_path": relative,
                      "manifest_sha256": model.sha(run / "manifest.json"), "live_prediction_allowed": False}})
        summary = {k: v for k, v in result.items() if k not in ("fitting_by_date", "funding_evaluations")}
        if "funding_evaluations" in result:
            summary["funding_evaluations"] = [{k: v for k, v in r.items() if k not in ("wealth", "events", "decisions")}
                                              for r in result["funding_evaluations"]]
        def public(value):
            if isinstance(value, dict):
                return {k: public(v) for k, v in value.items()
                        if k not in ("joint_scenarios", "scenario_profits", "cv_trials")}
            if isinstance(value, list):
                return [public(v) for v in value]
            return value
        return {"run_path": relative, "full_artifacts_saved": True, **public(summary)}
    finally:
        os.close(fd)
        lock.unlink()
