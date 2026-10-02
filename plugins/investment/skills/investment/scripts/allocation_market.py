"""Gross NAV return samples for the current allocation research pipeline.

Availability and weekday execution dates are explicit research proxies. Investor
transaction fees and settlement belong exclusively to the account replay layer.
"""

import bisect
import datetime as dt
import math
import copy
import statistics

from research_data import source_time, source_version_available

NAV_FEATURE_NAMES = [
    "momentum_20_nav_observations", "momentum_60_nav_observations",
    "momentum_120_nav_observations", "volatility_60_nav_observations_annualized_252",
    "drawdown_60_nav_intervals",
]
FEATURE_NAMES = NAV_FEATURE_NAMES + ["feature_age_calendar_days"]


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


def nav_snapshot(row, boundary=None, *, require_known=False):
    """Select an evidenced vintage, never a later correction at an old origin."""
    versions = [version for version in row.get("source_versions", [])
                if source_version_available(version) is not None]
    value = {key: copy.deepcopy(item) for key, item in row.items() if key != "source_versions"}
    if not versions and not require_known:
        return value
    if not row.get("source_versions"):
        return None
    cutoff = source_time(boundary) if boundary is not None else None
    def known(version):
        return source_version_available(version) or source_time(version["observed_at"])
    eligible = [version for version in row["source_versions"] if cutoff is None or known(version) <= cutoff]
    if not eligible:
        return None
    selected = max(eligible, key=lambda version: (known(version), version["version_id"]))
    if selected["date"] != row["date"] or selected["code"] != row.get("code", selected["code"]):
        raise ValueError("NAV source vintage identity differs")
    value = {"code": row.get("code", selected["code"]), "date": row["date"]}
    for key in ("nav", "cumulative_nav", "distribution_per_share", "distribution_text", "provider_daily_return", "historical_published_at", "corporate_actions", "distribution_evidence"):
        if key in selected:
            value[key] = copy.deepcopy(selected[key])
    value.update(available_at=selected["available_at"], observed_at=selected["observed_at"],
                 source_version_id=selected["version_id"], raw_ref=selected["raw_ref"],
                 known_at=known(selected).isoformat(),
                 strict_PIT_verified=source_version_available(selected) is not None)
    return value


def known_mark(row, decision_at):
    """Current observed valuation; historical PIT qualification is separate."""
    from decimal import Decimal
    from zoneinfo import ZoneInfo
    from contracts import fingerprint, require
    cutoff = source_time(decision_at)
    eligible = []
    for version in row.get("source_versions", []):
        require(version.get("version_id") == fingerprint({key: value for key, value in version.items() if key != "version_id"}),
                "Known NAV source version identity changed")
        raw = version.get("raw_ref", {})
        require(isinstance(raw.get("source_id"), str) and raw["source_id"] and isinstance(raw.get("sha256"), str)
                and len(raw["sha256"]) == 64 and all(value in "0123456789abcdef" for value in raw["sha256"]),
                "Known NAV needs its original raw capture identity")
        observed = source_time(version["observed_at"])
        known = source_version_available(version) or observed
        require(version["date"] == row["date"] and version["code"] == row.get("code", version["code"]),
                "Known NAV source subject/date differs")
        require(_day(version["date"]) <= observed.astimezone(ZoneInfo("Asia/Shanghai")).date(),
                "Known NAV capture claims a future economic date")
        require(_number(version["nav"], "known NAV") > 0, "Known NAV must be positive")
        if known <= cutoff and observed <= cutoff:
            eligible.append((known, observed, version))
    require(eligible, "Current valuation needs an original NAV capture known before the decision")
    eligible.sort(key=lambda item: (item[0], item[1], item[2]["version_id"]))
    selected = eligible[-1]
    for earlier in reversed(eligible[:-1]):
        if (earlier[2]["date"], earlier[2]["nav"]) != (selected[2]["date"], selected[2]["nav"]):
            break
        selected = earlier
    known, observed, version = selected
    return {"nav_date": version["date"], "value": str(Decimal(str(version["nav"]))), "known_at": known.isoformat(),
            "source_ref": {"source_id": version["raw_ref"]["source_id"], "raw_sha256": version["raw_ref"]["sha256"],
                           "source_version_id": version["version_id"], "capture_observed_at": observed.isoformat()}}


def validate_known_mark(mark, row, decision_at):
    from contracts import require
    require(mark == known_mark(row, decision_at), "Known valuation mark differs from its original source vintage")
    return True


def _feature_at(feature, rows, origin, availability_lag):
    cutoff = feature["feature_cutoff_nav_date"]
    history = [row for row in rows if row["date"] <= cutoff]
    evidence = any(source_version_available(version) is not None
                   for row in history[-121:] for version in row.get("source_versions", []))
    if not evidence:
        if _day(cutoff) > _day(origin[:10])-dt.timedelta(days=availability_lag):
            return None
        return [feature["values"][name] for name in NAV_FEATURE_NAMES], {
            "strict_PIT_verified": False, "available_at": None,
            "base_nav": history[-1]["nav"], "availability_basis": "declared_lag_research_proxy"}
    selected = [nav_snapshot(row, origin, require_known=True) for row in history[-121:]]
    if len(selected) < 121 or any(row is None for row in selected):
        return None
    rates = [(float(row["nav"])+float(row["distribution_per_share"]))/float(prior["nav"])-1
             for prior, row in zip(selected, selected[1:])]
    wealth = [1.]
    for rate in rates:
        wealth.append(wealth[-1]*(1+rate))
    values = [wealth[-1]/wealth[-1-period]-1 for period in (20, 60, 120)]
    values += [statistics.stdev(rates[-60:])*math.sqrt(252), _losses(wealth[-61:])["peak_drawdown"]]
    strict = all(row.get("strict_PIT_verified") is True for row in selected)
    if not strict and any(row.get("known_at") is None for row in selected):
        if _day(cutoff) > _day(origin[:10])-dt.timedelta(days=availability_lag):
            return None
    return values, {"strict_PIT_verified": strict,
        "available_at": max((row["available_at"] for row in selected), key=source_time) if strict else None,
        "base_nav": selected[-1]["nav"],
        "source_version_ids": [row.get("source_version_id") for row in selected],
        "availability_basis": "source_vintages" if strict else "partial_vintages_with_declared_proxy"}


def mature_label_at(row, boundary):
    versions = row.get("label_versions")
    value = {key: copy.deepcopy(item) for key, item in row.items() if key != "label_versions"}
    if versions is None:
        return value
    cutoff = source_time(boundary if "T" in boundary else boundary+"T00:00:00+08:00")
    eligible = [version for version in versions if source_time(version["label_available_at"]) < cutoff]
    if not eligible:
        return None
    value.update(max(eligible, key=lambda version: source_time(version["label_available_at"])))
    return value


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


def build_samples(nav_by_code, features_by_code, code_info, horizons, availability_lag, decision_end_date,
                  order_time_local="00:00:00"):
    """Calendar-origin fixed-share returns and causally available information age.

    Each calendar origin has its own endpoint origin+H, including closed-market
    days. Equal date weights do not remove the dependence induced by repeated
    information and overlapping outcomes.
    """
    if type(availability_lag) is not int or availability_lag < 0:
        raise ValueError("Explicit nonnegative availability lag required")
    if not horizons or any(type(h) is not int or h <= 0 for h in horizons) or len(set(horizons)) != len(horizons):
        raise ValueError("Distinct positive calendar horizons required")
    end_date = _day(decision_end_date)
    series, samples = _series(nav_by_code), []
    common_execution_days = sorted(set.intersection(*(set(history["execution_days"]) for history in series.values())))
    for code in sorted(series):
        history = series[code]
        group = code_info.get(code, {}).get("fund_group_id")
        if not isinstance(group, str) or not group.strip():
            raise ValueError("Known nonempty fund_group_id required")
        cutoff_dates, feature_rows = [], []
        for feature in features_by_code.get(code, []):
            cutoff = _day(feature["feature_cutoff_nav_date"])
            if (feature.get("code") != code or cutoff not in history["prices"]
                    or (cutoff_dates and cutoff <= cutoff_dates[-1])
                    or _day(feature["known_max_nav_date"]) > cutoff):
                raise ValueError("Feature identity, cutoff, or chronology is invalid")
            cutoff_dates.append(cutoff)
            for key in NAV_FEATURE_NAMES:
                _number(feature["values"][key], key, minimum=-math.inf)
            feature_rows.append(feature)
        if not cutoff_dates:
            continue
        first = cutoff_dates[0] + dt.timedelta(days=availability_lag)
        for offset in range(max(0, (end_date-first).days+1)):
            decision = first + dt.timedelta(days=offset)
            origin = decision.isoformat()+"T"+order_time_local+"+08:00"
            chosen = None
            for index in range(bisect.bisect_right(cutoff_dates, decision)-1, -1, -1):
                candidate = _feature_at(feature_rows[index], nav_by_code[code], origin, availability_lag)
                if candidate is not None:
                    chosen = index, candidate
                    break
            if chosen is None:
                continue
            index, (vector, feature_source) = chosen
            cutoff = cutoff_dates[index]
            x = vector + [(decision-cutoff).days]
            execution_day = _at_or_after(history["execution_days"], decision, strictly=True)
            for horizon in horizons:
                target = decision+dt.timedelta(days=horizon)
                exit_day = (_at_or_after(common_execution_days, target)
                            if common_execution_days and target >= common_execution_days[0] else None)
                available = exit_day+dt.timedelta(days=availability_lag) if exit_day else None
                total_return = None
                if exit_day:
                    shares, cash = 1/float(feature_source["base_nav"]), 0.
                    start = bisect.bisect_right(history["days"], cutoff)
                    stop = bisect.bisect_right(history["days"], exit_day)
                    for day in history["days"][start:stop]:
                        cash += shares*history["prices"][day][1]
                    total_return = shares*history["prices"][exit_day][0]+cash-1
                samples.append({"decision_date": decision.isoformat(),
                                "label_available_date": available.isoformat() if available else None,
                                "code": code, "fund_group_id": group, "x": x,
                                "feature_source": feature_source,
                                "return": total_return, "horizon_days": horizon,
                                "entry_date": cutoff.isoformat(),
                                "planned_execution_date": execution_day.isoformat() if execution_day else None,
                                "return_basis": "constant_shares_cash_distributions",
                                "target_not_before": (decision+dt.timedelta(days=horizon)).isoformat(),
                                "exit_date": exit_day.isoformat() if exit_day else None,
                                "feature_cutoff_date": cutoff.isoformat(),
                                "information_age_role": "availability_covariate_not_validated_alpha",
                                "sampling_basis": "calendar_origins_with_overlapping_information_and_outcomes"})
    return samples




TARGET_NAMES = ("pricing", "terminal_nav", "hold", "buy", "sell")
LATENT_NAMES = ("log_pricing", "log_terminal_nav", "sqrt_cash_neither", "sqrt_cash_sale_only", "sqrt_cash_buy_only", "sqrt_cash_both")


def _rights(event, owned, sold=None):
    """Certain cash rights only; boundary ambiguity is a label evidence gap."""
    record = _day(event["record_date"])
    rule = event["entitlement_rule"]
    if owned > record or (sold is not None and sold < record):
        return False
    if owned == record:
        choice = rule.get("subscribe_on_record_date")
        if choice not in ("included", "excluded"):
            raise ValueError("Subscription record-date rights are unknown")
        if choice == "excluded":
            return False
    if sold == record:
        choice = rule.get("redeem_on_record_date")
        if choice not in ("included", "excluded"):
            raise ValueError("Redemption record-date rights are unknown")
        if choice == "excluded":
            return False
    return True


def _confirmation_day(days, priced, rule):
    lag = rule["lag_days"]
    if rule["day_basis"] == "calendar_days":
        return priced+dt.timedelta(days=lag)
    if rule["day_basis"] == "trading_days":
        following = [day for day in days if day > priced]
        if lag == 0:
            return priced
        return following[lag-1] if len(following) >= lag else None
    raise ValueError("Unsupported source confirmation day basis")


def _label_versions(sample, source, raw):
    from contracts import fingerprint
    base, end = source["base_date"], source["end_date"]
    dates = sorted(day for day in raw if base < str(day) <= end)
    proxy = sample["label_available_date"]+"T00:00:00+08:00"
    require_known = sample["feature_source"].get("availability_basis") in ("source_vintages", "partial_vintages_with_declared_proxy") or sample["feature_source"].get("strict_PIT_verified") is True
    require_known |= any(source_version_available(version) is not None for day in dates for version in raw[day].get("source_versions", []))
    times = {proxy}
    for day in dates:
        quote = raw[day]
        has_archive = any(source_version_available(item) is not None for item in quote.get("source_versions", []))
        if has_archive or require_known:
            times.update(version.get("available_at") or version["observed_at"] for version in quote.get("source_versions", []))
        if quote.get("available_at"):
            times.add(quote["available_at"])
        times.update(event["known_at"] for event in quote.get("corporate_actions", []) if event.get("known_at"))
    output, previous_state, income_gap = [], None, None
    for at in sorted(times, key=source_time):
        quotes = [nav_snapshot(raw[day], at, require_known=require_known) for day in dates]
        if any(quote is None for quote in quotes):
            continue
        events = []
        try:
            for quote in quotes:
                distributions = [event for event in quote.get("corporate_actions", []) if event.get("kind") == "cash_distribution"]
                if not math.isclose(sum(float(event["per_share"]) for event in distributions),
                                    float(quote["distribution_per_share"]), abs_tol=1e-12):
                    raise ValueError("Cash distribution lacks complete source entitlement records")
                for event in distributions:
                    if not all(event.get(key) is not None for key in ("record_date", "ex_date", "pay_date", "entitlement_rule", "evidence_ref")):
                        raise ValueError("Cash distribution record/ex/payment/rights source is incomplete")
                    if event.get("distribution_mode") != "cash" or event.get("currency") != "CNY" or event["ex_date"] != quote["date"]:
                        raise ValueError("Unsupported cash-distribution election/currency/ex-date")
                events.extend(copy.deepcopy(distributions))
        except (ValueError, KeyError) as error:
            # An incomplete older archive cannot supply a zero cash label or
            # prevent a later complete original vintage from qualifying.
            income_gap = str(error)
            previous_state = None
            continue
        strict = all(quote.get("strict_PIT_verified") is True for quote in quotes)
        available = [quote.get("known_at") or quote.get("available_at") for quote in quotes]
        available = [value for value in available if value is not None]
        if not strict:
            available.append(proxy)
        available.extend(event["known_at"] for event in events if event.get("known_at"))
        mature = max(available, key=source_time)
        if source_time(mature) > source_time(at):
            continue
        table = {quote["date"]: quote for quote in quotes}
        nb, np_, nt = float(source["base_nav"]), float(table[source["pricing_date"]]["nav"]), float(table[end]["nav"])
        owned = _day(source["ownership_date"])
        old_cash = sum(float(event["per_share"]) for event in events)
        try:
            new_cash = sum(float(event["per_share"]) for event in events if _rights(event, owned))
            sold_cash = sum(float(event["per_share"]) for event in events if _rights(event, _day(base)-dt.timedelta(days=1), _day(source["pricing_date"])))
        except ValueError as error:
            income_gap = str(error)
            previous_state = None
            continue
        targets = {"pricing": np_/nb, "terminal_nav": nt/nb, "hold": (nt+old_cash)/nb,
                   "buy": (nt+new_cash)/np_, "sell": (np_+sold_cash)/nb}
        original = {key: copy.deepcopy(value) for key, value in source.items() if key not in ("source_hash", "latent_targets")}
        original.update(pricing_nav=np_, terminal_nav=nt, dividends=events, targets=targets,
            source_quotes=quotes, label_available=mature[:10], label_available_at=mature,
            strict_PIT_verified=strict and sample["feature_source"]["strict_PIT_verified"],
            vintage_selection="known_source_versions" if require_known else "declared_lag_proxy",
            dividend_values={"hold": old_cash, "buy": new_cash, "sell": sold_cash})
        original["latent_targets"] = recompute_source_latents(original)
        original["source_hash"] = fingerprint(original)
        value = {"label_available_at": mature, "label_available_date": mature[:10],
            "label_source": original, "targets": targets, "latent_targets": original["latent_targets"],
            "return": targets["hold"]-1}
        # Capture identity/time can change without changing the observed
        # economic result. Preserve its first complete original proof. A
        # correction, reversion or stronger source qualification is a new state.
        economic_state = fingerprint({
            "quotes": [{key: quote.get(key) for key in
                        ("code", "date", "nav", "cumulative_nav", "distribution_per_share")} for quote in quotes],
            "cash": [{key: event.get(key) for key in
                      ("kind", "code", "id", "record_date", "ex_date", "pay_date", "per_share",
                       "currency", "distribution_mode", "entitlement_rule")} for event in events],
            "targets": targets, "latents": original["latent_targets"],
            "income": original["dividend_values"],
            "source_qualification": {"quotes_strict_PIT": [quote.get("strict_PIT_verified") is True for quote in quotes],
                                     "label_strict_PIT": original["strict_PIT_verified"],
                                     "selection": original["vintage_selection"]}})
        if economic_state != previous_state:
            output.append(value)
            previous_state = economic_state
    if not output and income_gap is not None:
        raise ValueError(income_gap)
    return output


def build_single_step_samples(nav_by_code, features_by_code, code_info, horizon, availability_lag,
                              decision_end_date, clock_context):
    """Direct five-target labels, conditional on normal dealing clocks.

    Historical sessions are observed NAV dates, not archived actual fills.
    Target dates and rights are outcomes. They never enter feature vectors.
    All five labels mature at the common terminal observation, so no partial
    short execution label can leak a not-yet-mature terminal outcome.
    """
    from contracts import fingerprint
    import copy
    skeleton = build_samples(nav_by_code, features_by_code, code_info, [horizon], availability_lag, decision_end_date,
                             clock_context.get("order_time_local", "00:00:00"))
    series = _series(nav_by_code)
    raw = {code: {_day(row["date"]): row for row in rows} for code, rows in nav_by_code.items()}
    output = []
    for sample in skeleton:
        row = copy.deepcopy(sample)
        row["return"] = None
        row["targets"] = None
        row["label_source"] = None
        row["label_reason"] = None
        code, origin = row["code"], _day(row["decision_date"])
        clock = clock_context["assets"][code]
        days = series[code]["execution_days"]
        priced = _at_or_after(days, origin, strictly=clock["after_cutoff"])
        end = _day(row["exit_date"]) if row["exit_date"] else None
        base = _day(row["feature_cutoff_date"])
        confirmed = _confirmation_day(days, priced, clock["confirmation_rule"]) if priced else None
        owned = priced if clock["ownership_start"] == "execution_date" else confirmed
        try:
            if priced is None or confirmed is None or owned is None or end is None or owned > end or priced > end:
                raise ValueError("Normal pricing/ownership or terminal NAV is not observed")
            events = []
            for day, source in raw[code].items():
                if not base < day <= end:
                    continue
                distributions = [action for action in source.get("corporate_actions", []) if action.get("kind") == "cash_distribution"]
                if not math.isclose(sum(float(event["per_share"]) for event in distributions),
                                    float(source["distribution_per_share"]), abs_tol=1e-12):
                    raise ValueError("Cash distribution lacks complete source entitlement records")
                for event in distributions:
                    if not all(event.get(key) is not None for key in ("record_date", "ex_date", "pay_date", "entitlement_rule", "evidence_ref")):
                        raise ValueError("Cash distribution record/ex/payment/rights source is incomplete")
                    if event.get("distribution_mode") != "cash" or event.get("currency") != "CNY" or _day(event["ex_date"]) != day:
                        raise ValueError("Unsupported cash-distribution election/currency/ex-date")
                    events.append(copy.deepcopy(event))
            nb = float(row["feature_source"]["base_nav"])
            np_, nt = [float(raw[code][day]["nav"]) for day in (priced, end)]
            old_cash = sum(float(event["per_share"]) for event in events)
            new_cash = sum(float(event["per_share"]) for event in events if _rights(event, owned))
            sold_cash = sum(float(event["per_share"]) for event in events if _rights(event, base-dt.timedelta(days=1), priced))
            targets = {"pricing": np_/nb, "terminal_nav": nt/nb, "hold": (nt+old_cash)/nb,
                       "buy": (nt+new_cash)/np_, "sell": (np_+sold_cash)/nb}
            if not all(math.isfinite(value) and value > 0 for value in targets.values()):
                raise ValueError("Single-step target is outside the positive-wealth domain")
            historical_clock = copy.deepcopy(clock)
            historical_clock.update(pricing_date=str(priced), confirmation_date=str(confirmed),
                ownership_date_bounds={"earliest": str(priced), "latest": str(owned)})
            holding_end = priced if clock["holding_start"] == "execution_date" else confirmed
            historical_clock["holding_start_bounds"] = {"earliest": str(priced)+"T00:00:00+08:00", "latest": str(holding_end)+"T00:00:00+08:00"}
            source = {"code": code, "base_date": str(base), "base_nav": nb, "pricing_date": str(priced),
                      "pricing_nav": np_, "ownership_date": str(owned), "confirmation_date": str(confirmed),
                      "end_date": str(end), "terminal_nav": nt, "dividends": events, "targets": targets,
                      "label_available": row["label_available_date"], "clock": historical_clock,
                      "schedule_basis": "normal_source_clock_on_observed_NAV_sessions_not_actual_historical_fill",
                      "feature_source": copy.deepcopy(row["feature_source"]),
                      "base_quote": nav_snapshot(raw[code][base], row["decision_date"]+"T"+clock_context.get("order_time_local", "00:00:00")+"+08:00",
                          require_known=row["feature_source"]["availability_basis"] != "declared_lag_research_proxy"),
                      "feature_cutoff_date": row["feature_cutoff_date"],
                      "dividend_values": {"hold": old_cash, "buy": new_cash, "sell": sold_cash}}
            source["latent_targets"] = recompute_source_latents(source)
            source["source_hash"] = fingerprint(source)
            versions = _label_versions(row, source, raw[code])
            if not versions:
                raise ValueError("No mature source NAV vintage for the complete target")
            row.update(versions[-1], label_versions=versions)
        except (ValueError, KeyError) as exc:
            row["label_reason"] = str(exc)
        output.append(row)
    return output


def recompute_source_targets(source):
    """Independent arithmetic from preserved primitive price/rights evidence."""
    base, priced, end = [_day(source[key]) for key in ("base_date", "pricing_date", "end_date")]
    owned = _day(source["ownership_date"])
    if not base <= priced <= end or not priced <= owned <= end:
        raise ValueError("Source target chronology is invalid")
    nb, np_, nt = [_number(source[key], key) for key in ("base_nav", "pricing_nav", "terminal_nav")]
    if min(nb, np_, nt) <= 0:
        raise ValueError("Source NAV must be positive")
    events = source["dividends"]
    for event in events:
        if not base < _day(event["ex_date"]) <= end or _day(event["record_date"]) > _day(event["ex_date"]) or _day(event["pay_date"]) < _day(event["ex_date"]):
            raise ValueError("Source cash entitlement chronology is invalid")
        if not event.get("evidence_ref") or event.get("currency") != "CNY" or event.get("distribution_mode") != "cash":
            raise ValueError("Source cash entitlement evidence is invalid")
    old = sum(_number(event["per_share"], "cash per share") for event in events)
    new = sum(_number(event["per_share"], "cash per share") for event in events if _rights(event, owned))
    sold = sum(_number(event["per_share"], "cash per share") for event in events if _rights(event, base-dt.timedelta(days=1), priced))
    return {"pricing": np_/nb, "terminal_nav": nt/nb, "hold": (nt+old)/nb, "buy": (nt+new)/np_, "sell": (np_+sold)/nb}


def recompute_source_latents(source):
    """Unique source rights membership buckets, including legally overlapping rights."""
    measured = recompute_source_targets(source)
    base, priced, owned = [_day(source[key]) for key in ("base_date", "pricing_date", "ownership_date")]
    buckets = {name: 0. for name in LATENT_NAMES[2:]}
    for event in source["dividends"]:
        sold = _rights(event, base-dt.timedelta(days=1), priced)
        bought = _rights(event, owned)
        membership = "both" if sold and bought else "sale_only" if sold else "buy_only" if bought else "neither"
        buckets["sqrt_cash_"+membership] += float(event["per_share"])/source["base_nav"]
    return {"log_pricing": math.log(measured["pricing"]), "log_terminal_nav": math.log(measured["terminal_nav"]),
            **{name: math.sqrt(amount) for name, amount in buckets.items()}}


def decode_latents(values):
    """Structural positive-price/squared-cash link; never clip/reweight scenarios."""
    if set(values) != set(LATENT_NAMES) or not all(math.isfinite(value) for value in values.values()):
        raise ValueError("Complete finite latent coordinates required")
    p, t = math.exp(values["log_pricing"]), math.exp(values["log_terminal_nav"])
    cash = {name: values[name]**2 for name in LATENT_NAMES[2:]}
    total = sum(cash.values())
    result = {"pricing": p, "terminal_nav": t, "hold": t+total,
              "buy": (t+cash["sqrt_cash_buy_only"]+cash["sqrt_cash_both"])/p,
              "sell": p+cash["sqrt_cash_sale_only"]+cash["sqrt_cash_both"]}
    if not all(math.isfinite(value) and value > 0 for value in result.values()):
        raise ValueError("Decoded economic wealth is outside the numerical domain")
    return result


def attach_industry_features(samples, industry, exposures, order_time_local):
    """Separate source roles; source estimates never certify current holdings."""
    from contracts import fingerprint, instant, require
    from industry_model import FUND_FEATURE_NAMES, origin_at, INPUT_SCHEMA_ID
    import asset_domains
    require(industry.get("input_schema_id") == INPUT_SCHEMA_ID, "Obsolete asset forecasts require retraining")
    predictions = {(item["decision_date"], item["sector_id"]): item for item in industry["forecasts"]}
    output = []
    for sample in samples:
        row = copy.deepcopy(sample)
        at, day = origin_at(row["decision_date"], order_time_local), row["decision_date"]
        row.update(industry_ready=False, industry_source=None, industry_reason=None)
        try:
            known = [item for item in exposures.get(row["code"], []) if item["as_of_date"] <= day and instant(item["known_at"]) <= instant(at)]
            if not known:
                raise ValueError("No source-qualified disclosure or learned asset style at this origin")
            disclosed = [item for item in known if item["basis"] == "historical_disclosure"]
            estimated = [item for item in known if item["basis"] == "model_estimate"]
            mapping = []
            for group in (disclosed, estimated):
                if group:
                    snapshot = max((item["as_of_date"], item["known_at"]) for item in group)
                    mapping.extend(item for item in group if (item["as_of_date"], item["known_at"]) == snapshot)
            measured_ids = {item["sector_id"] for item in mapping if item["basis"] == "historical_disclosure"}
            mapping = [item for item in mapping if item["basis"] != "model_estimate" or item["sector_id"] not in measured_ids]
            keys = [(item["sector_id"], item["role"], item.get("source_label"), item["basis"]) for item in mapping]
            if not mapping or len(keys) != len(set(keys)) or not all(item.get("evidence_refs") for item in mapping):
                raise ValueError("Asset input snapshot is duplicate or lacks original source evidence")
            role_values = {role: [0., 0., 0.] for role in asset_domains.ROLES}
            source_hashes, reference_weights, stale_weights = {}, {}, {}
            for item in mapping:
                role, basis = item["role"], item["basis"]
                if role not in role_values:
                    raise ValueError("Unknown asset input role")
                coefficient = float(item["coefficient"] if basis == "model_estimate" else item["weight"])
                if not math.isfinite(coefficient) or (basis != "model_estimate" and not 0 < coefficient <= 1):
                    raise ValueError("Invalid source disclosure weight or learned factor coefficient")
                source = predictions[(day, item["sector_id"])]
                if source["horizon_days"] != row["horizon_days"] or source.get("input_schema_id") != INPUT_SCHEMA_ID:
                    raise ValueError("Asset forecast horizon or input schema differs from fund label")
                for column in range(2):
                    role_values[role][column] += coefficient*source["predicted_coordinates"][column]
                role_values[role][2] = 1.
                source_hashes[item["sector_id"]] = source["source_hash"]
                if basis == "historical_disclosure":
                    target = reference_weights
                    label = item["source_label"]
                    previous = target.get(label)
                    if previous is not None and previous != coefficient:
                        raise ValueError("Overlapping taxonomy assigns conflicting original source weight")
                    target[label] = coefficient
                    if item["as_of_date"] < day:
                        stale_weights[label] = coefficient
            covered = math.fsum(reference_weights.values())
            stale = math.fsum(stale_weights.values())
            if covered > 1+1e-12 or stale > 1+1e-12:
                raise ValueError("Original disclosure capital weights exceed one")
            values = [value for role in asset_domains.ROLES for value in role_values[role]]
            values += [1., stale, float(any(item["basis"] == "model_estimate" for item in mapping))]
            witness = {"code": row["code"], "decision_date": day, "origin_at": at,
                "input_schema_id": INPUT_SCHEMA_ID, "exposures": mapping, "role_values": role_values,
                "sector_prediction_hashes": source_hashes, "feature_names": FUND_FEATURE_NAMES, "values": values,
                "actual_current_unknown_weight": 1., "disclosed_reference_unmapped_weight": 1-covered,
                "estimated_exposure_role": "learned_source_NAV_covariate_coefficient_not_actual_capital_weight",
                "unmapped_weight_role": "current_actual_unknown_component_retained_in_fund_NAV_and_whole_stack_risk",
                "role": "covariates_only_no_asset_cash_FX_fee_or_error_addition"}
            witness["source_hash"] = fingerprint(witness)
            row.update(x=row["x"]+values, industry_ready=True, industry_source=witness)
        except (ValueError, KeyError) as exc:
            row["industry_reason"] = str(exc)
        output.append(row)
    return output
