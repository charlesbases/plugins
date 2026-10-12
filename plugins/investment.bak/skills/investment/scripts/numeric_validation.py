"""Read-only source and arithmetic checks; no refitting or account writes.

Passing establishes listed identities, not market effectiveness, future profit,
statistical coverage or an independently proved global optimum.
"""
import collections
import datetime as dt
import math
from decimal import Decimal
from zoneinfo import ZoneInfo
from contracts import EvidenceError, fingerprint, instant
import verify

TARGETS, LATENTS = verify.TARGETS, verify.LATENTS
SCOPE = "source_bound_numerical_arithmetic_only"


def check(condition, message):
    if not condition:
        raise EvidenceError("Stage validation: " + message)


def close(actual, expected, label, scale=1):
    actual, expected = float(actual), float(expected)
    check(math.isfinite(actual) and math.isfinite(expected)
          and abs(actual-expected) <= 1e-8+1e-10*max(1, abs(float(scale))), label)


def result(checks, readiness="ready", limitations=(), reason=None):
    partial = readiness in {"partial", "insufficient_evidence", "blocked", "awaiting_intervention", "awaiting_confirmation"}
    action = "provide_mature_source_samples_or_evidence" if readiness == "insufficient_evidence" else "resolve_order_readiness"
    actions = ([{"action": action, "reason": reason or readiness}]
               if partial else [])
    return {"status": "partial" if partial else "passed", "scope": SCOPE, "checks": checks,
            "readiness": {"status": readiness, "trade_ready": readiness in {"ready", "research_ready"}},
            "required_actions": actions,
            "limitations": ["no_market_effectiveness_or_future_profit_proof",
                            "no_statistical_coverage_claim", *limitations]}


def tail_mean(losses, probabilities, alpha):
    check(len(losses) == len(probabilities) and losses, "CVaR dimensions")
    return min(eta+math.fsum(p*max(loss-eta, 0) for loss, p in zip(losses, probabilities))/alpha
               for eta in set(losses))


def _distribution(joint):
    codes, probabilities = joint["codes"], joint["probabilities"]
    check(codes and len(set(codes)) == len(codes) and probabilities
          and all(math.isfinite(p) and p > 0 for p in probabilities), "joint universe/probability domain")
    close(math.fsum(probabilities), 1, "joint probability mass")
    check(set(joint["targets"]) == set(TARGETS) == set(joint["current_point_targets"]), "complete public targets")
    for name in TARGETS:
        for key, count in (("targets", len(probabilities)), ("current_point_targets", 1)):
            matrix = joint[key][name]
            check(len(matrix) == count and all(len(row) == len(codes) for row in matrix)
                  and all(math.isfinite(value) and value > 0 for row in matrix for value in row), "joint target dimensions/domain")
    check(len(joint["returns"]) == len(probabilities), "joint return inventory")
    for row, targets in zip(joint["returns"], joint["targets"]["hold"]):
        check(len(row) == len(codes), "joint return dimensions")
        for actual, target in zip(row, targets):
            close(actual, target-1, "hold return projection")


def validate_clock(output, inputs):
    context = inputs["context"]
    local = instant(context["decision_at"]).astimezone(ZoneInfo("Asia/Shanghai"))
    codes = context["allocation_codes"]
    check(set(output["assets"]) == set(codes), "clock universe")
    common, starts, ends = None, [], []
    for code in codes:
        terms, clock = context["fee_contracts"][code], output["assets"][code]
        calendar, rule = terms["execution_calendar"], terms["confirmation"]
        days = calendar["open_dates"]
        check(days == sorted(set(days)) and calendar["coverage_start"] <= local.date().isoformat()
              <= calendar["coverage_end"], "source calendar chronology/coverage")
        common = set(days) if common is None else common.intersection(days)
        starts.append(calendar["coverage_start"])
        ends.append(calendar["coverage_end"])
        after = local.time().replace(tzinfo=None) >= dt.time.fromisoformat(terms["order_cutoff_local"])
        first = (local.date()+dt.timedelta(days=int(after))).isoformat()
        priced = next((day for day in days if day >= first), None)
        check(priced is not None, "source pricing session absent")
        if rule["day_basis"] == "calendar_days":
            confirmed = (dt.date.fromisoformat(priced)+dt.timedelta(days=rule["lag_days"])).isoformat()
        else:
            check(rule["day_basis"] == "trading_days", "source confirmation basis")
            later = [day for day in days if day > priced]
            check(len(later) >= rule["lag_days"], "source confirmation sessions absent")
            confirmed = priced if rule["lag_days"] == 0 else later[rule["lag_days"]-1]
        acquisition = terms["acquisition_rule"]
        owned = priced if acquisition["ownership_start"] == "execution_date" else confirmed
        held = priced if acquisition["holding_start"] == "execution_date" else confirmed
        check(clock["pricing_date"] == priced and clock["confirmation_date"] == confirmed
              and clock["after_cutoff"] == after and clock["confirmation_rule"] == rule
              and clock["ownership_start"] == acquisition["ownership_start"]
              and clock["holding_start"] == acquisition["holding_start"], "clock source rules")
        check(clock["ownership_date_bounds"] == {"earliest": priced, "latest": owned}, "ownership bounds")
        for key, day in (("earliest", priced), ("latest", held)):
            check(clock["holding_start_bounds"][key] == day+"T00:00:00+08:00", "holding start bounds")
        check(clock["source_terms_hash"] == fingerprint(terms), "clock source hash")
    target = (local.date()+dt.timedelta(days=context["spec"]["planning"]["primary_horizon_days"])).isoformat()
    check(max(starts) <= target <= min(ends), "common terminal calendar coverage")
    expected = next((day for day in sorted(common) if day >= target), None)
    check(expected is not None and output["evaluation_date"] == expected, "common terminal clock")
    check(output["order_time_local"] == local.time().replace(tzinfo=None).isoformat(), "clock origin time")
    return result(["source_cutoff_and_confirmation", "ownership_and_holding_bounds", "common_terminal_calendar"])


def _historical_dates(origin, sessions, clock):
    priced = next((day for day in sessions if day > origin or day == origin and not clock["after_cutoff"]), None)
    if priced is None:
        return None, None, None
    rule = clock["confirmation_rule"]
    if rule["day_basis"] == "calendar_days":
        confirmed = (dt.date.fromisoformat(priced)+dt.timedelta(days=rule["lag_days"])).isoformat()
    else:
        check(rule["day_basis"] == "trading_days", "historical confirmation basis")
        later = [day for day in sessions if day > priced]
        confirmed = priced if rule["lag_days"] == 0 else later[rule["lag_days"]-1] if len(later) >= rule["lag_days"] else None
    return priced, confirmed, priced if clock["ownership_start"] == "execution_date" else confirmed


def _source_values(source, raw, base, priced, owned, end):
    prices = [Decimal(str(raw[day]["nav"])) for day in (base, priced, end)]
    events = []
    for day, row in raw.items():
        if base < day <= end:
            actions = [event for event in row.get("corporate_actions", []) if event.get("kind") == "cash_distribution"]
            close(math.fsum(float(event["per_share"]) for event in actions), row["distribution_per_share"], "source cash coverage")
            for event in actions:
                check(event.get("evidence_ref") and event.get("currency") == "CNY"
                      and event.get("distribution_mode") == "cash" and event["ex_date"] == day
                      and event["record_date"] <= day <= event["pay_date"], "source cash rights chronology")
            events.extend(actions)
    buckets = dict.fromkeys(("neither", "sale_only", "buy_only", "both"), Decimal(0))
    for event in events:
        bought = verify._source_right(event, owned)
        sold = verify._source_right(event, (dt.date.fromisoformat(base)-dt.timedelta(days=1)).isoformat(), priced)
        bucket = "both" if bought and sold else "buy_only" if bought else "sale_only" if sold else "neither"
        buckets[bucket] += Decimal(str(event["per_share"]))/prices[0]
    latent = {"log_pricing": math.log(float(prices[1]/prices[0])),
              "log_terminal_nav": math.log(float(prices[2]/prices[0])),
              **{"sqrt_cash_"+key: math.sqrt(float(value)) for key, value in buckets.items()}}
    targets = verify._decoded(latent)
    if source is not None:
        check(set(source["targets"]) == set(TARGETS) and set(source["latent_targets"]) == set(LATENTS), "complete source target coordinates")
        check(sorted(fingerprint(event) for event in events) == sorted(fingerprint(event) for event in source["dividends"]), "source cash binding")
        for key, value in zip(("base_nav", "pricing_nav", "terminal_nav"), prices):
            close(source[key], value, "source " + key)
        for name in LATENTS:
            close(source["latent_targets"][name], latent[name], "source latent " + name)
        for name in TARGETS:
            close(source["targets"][name], targets[name], "source target " + name)
    return latent, targets


def _vintage_quote(row, at, *, require_known=False):
    from research_data import source_time, source_version_available
    versions = row.get("source_versions", [])
    if not require_known and not any(source_version_available(version) is not None for version in versions):
        return {key: value for key, value in row.items() if key != "source_versions"}
    eligible = [(source_version_available(version) or source_time(version["observed_at"]), version)
                for version in versions]
    eligible = [(known, version) for known, version in eligible if known <= source_time(at)]
    if not eligible:
        return None
    known, version = max(eligible, key=lambda item: (item[0], item[1]["version_id"]))
    result = {"code": row.get("code", version["code"]), "date": row["date"]}
    for key in ("nav", "cumulative_nav", "distribution_per_share", "distribution_text", "provider_daily_return", "historical_published_at", "corporate_actions", "distribution_evidence"):
        if key in version:
            result[key] = version[key]
    result.update(source_version_id=version["version_id"], available_at=version["available_at"],
        observed_at=version["observed_at"], known_at=known.isoformat(), raw_ref=version["raw_ref"],
        strict_PIT_verified=source_version_available(version) is not None)
    return result


def _vintage_feature(features, raw, origin, time, lag):
    import statistics
    from research_data import source_version_available
    at = origin+"T"+time+"+08:00"
    knowledge = (dt.date.fromisoformat(origin)-dt.timedelta(days=lag)).isoformat()
    for feature in sorted(features, key=lambda row: row["feature_cutoff_nav_date"], reverse=True):
        base = feature["feature_cutoff_nav_date"]
        if base > origin:
            continue
        window = [raw[day] for day in sorted(raw) if day <= base][-121:]
        actual = any(source_version_available(version) is not None
                     for quote in window for version in quote.get("source_versions", []))
        if not actual:
            if base <= knowledge:
                from allocation_market import NAV_FEATURE_NAMES
                return base, [feature["values"][name] for name in NAV_FEATURE_NAMES]
            continue
        quotes = [_vintage_quote(quote, at, require_known=True) for quote in window]
        if len(quotes) < 121 or any(quote is None for quote in quotes):
            continue
        if base > knowledge and any(quote.get("known_at") is None for quote in quotes):
            continue
        rates = [(float(quote["nav"])+float(quote["distribution_per_share"]))/float(prior["nav"])-1
                 for prior, quote in zip(quotes, quotes[1:])]
        wealth = [1.]
        for rate in rates:
            wealth.append(wealth[-1]*(1+rate))
        peak, drawdown = wealth[-61], 0.
        for value in wealth[-61:]:
            peak = max(peak, value)
            drawdown = max(drawdown, 1-value/peak)
        return base, [wealth[-1]/wealth[-1-n]-1 for n in (20,60,120)]+[statistics.stdev(rates[-60:])*math.sqrt(252),drawdown]
    return None


def validate_samples(output, inputs):
    data, context, clock = inputs["data"], inputs["context"], inputs["clock"]
    request = context["model_request"]
    lag, horizon, end_date = request["nav_availability_calendar_lag"], request["horizon_days"], context["as_of"]
    raw = {code: {row["date"]: row for row in rows} for code, rows in data["nav"].items()}
    sessions = {code: sorted(day for day in rows if dt.date.fromisoformat(day).weekday() < 5) for code, rows in raw.items()}
    common = sorted(set.intersection(*(set(days) for days in sessions.values())))
    expected, feature_values = [], {}
    for code in sorted(raw):
        dates = [row["date"] for row in data["nav"][code]]
        check(dates == sorted(set(dates)), "raw NAV chronology")
        for row in data["nav"][code]:
            check(row.get("code", code) == code and float(row["nav"]) > 0
                  and math.isfinite(float(row["nav"])), "raw NAV identity/domain")
        features = data["features"].get(code, [])
        cuts = [row["feature_cutoff_nav_date"] for row in features]
        check(cuts == sorted(set(cuts)), "feature chronology")
        for feature in features:
            check(feature["code"] == code and feature["known_max_nav_date"] <= feature["feature_cutoff_nav_date"]
                  and feature["feature_cutoff_nav_date"] in raw[code], "feature source availability")
        if cuts:
            first = dt.date.fromisoformat(cuts[0])+dt.timedelta(days=lag)
            for i in range(max(0, (dt.date.fromisoformat(end_date)-first).days+1)):
                origin = (first+dt.timedelta(days=i)).isoformat()
                feature = _vintage_feature(features, raw[code], origin, clock["order_time_local"], lag)
                if feature is not None:
                    expected.append((code, origin))
                    feature_values[(code, origin)] = feature
    check([(row["code"], row["decision_date"]) for row in output] == expected, "complete calendar origin inventory")
    mature = 0
    for row in output:
        code, origin = row["code"], row["decision_date"]
        base, values = feature_values[(code, origin)]
        vector = values+[(dt.date.fromisoformat(origin)-dt.date.fromisoformat(base)).days]
        check(row["feature_cutoff_date"] == base and len(row["x"]) == len(vector)
              and all(math.isfinite(float(value)) for value in vector), "latest feature vector and age")
        for actual, expected_value in zip(row["x"], vector):
            close(actual, expected_value, "source-vintage feature")
        check(row["fund_group_id"] == data["code_info"][code]["fund_group_id"] and row["horizon_days"] == horizon, "sample identity/horizon")
        target = (dt.date.fromisoformat(origin)+dt.timedelta(days=horizon)).isoformat()
        end = next((day for day in common if day >= target), None) if common and target >= common[0] else None
        available = (dt.date.fromisoformat(end)+dt.timedelta(days=lag)).isoformat() if end else None
        source = row.get("label_source")
        if source is not None and source.get("label_available_at") is not None:
            available = source["label_available_at"][:10]
        check(row["exit_date"] == end and row["label_available_date"] == available
              and row["target_not_before"] == target, "common label endpoint/maturity")
        priced, confirmed, owned = _historical_dates(origin, sessions[code], clock["assets"][code])
        if None in (priced, confirmed, owned, end) or owned > end or priced > end:
            check(row["targets"] is None and row["label_source"] is None and row["label_reason"], "unobserved complete label")
            continue
        source_raw = dict(raw[code])
        if source is not None and source.get("source_quotes") is not None:
            at = source["label_available_at"]
            for quote in source["source_quotes"]:
                expected_quote = _vintage_quote(raw[code][quote["date"]], at,
                    require_known=source.get("vintage_selection") == "known_source_versions")
                check(expected_quote is not None and quote == expected_quote, "original mature source NAV vintage")
                source_raw[quote["date"]] = expected_quote
            base_quote = _vintage_quote(raw[code][base], origin+"T"+clock["order_time_local"]+"+08:00",
                require_known=row["feature_source"]["availability_basis"] != "declared_lag_research_proxy")
            check(base_quote is not None and source.get("base_quote") == base_quote, "source feature base vintage")
            source_raw[base] = base_quote
            from research_data import source_time
            observed = [quote.get("known_at") or quote.get("available_at") for quote in source["source_quotes"]]
            observed = [value for value in observed if value is not None]
            if not all(quote.get("strict_PIT_verified") is True for quote in source["source_quotes"]):
                observed.append((dt.date.fromisoformat(end)+dt.timedelta(days=lag)).isoformat()+"T00:00:00+08:00")
            observed.extend(event["known_at"] for event in source["dividends"] if event.get("known_at"))
            check(max(observed, key=source_time) == at, "source label genuine maturity or declared proxy")
        try:
            latent, targets = _source_values(None, source_raw, base, priced, owned, end)
        except (EvidenceError, ValueError, KeyError):
            check(row["targets"] is None and row["label_source"] is None and row["label_reason"], "source label gap")
            continue
        source = row["label_source"]
        check(source is not None and source["code"] == code and source["base_date"] == base
              and source["pricing_date"] == priced and source["confirmation_date"] == confirmed
              and source["ownership_date"] == owned and source["end_date"] == end
              and source["label_available"] == available, "source label dates")
        _source_values(source, source_raw, base, priced, owned, end)
        check(source["source_hash"] == fingerprint({key: value for key, value in source.items() if key != "source_hash"}), "source label hash")
        for name in LATENTS:
            close(row["latent_targets"][name], latent[name], "sample latent " + name)
        for name in TARGETS:
            close(row["targets"][name], targets[name], "sample target " + name)
        close(row["return"], targets["hold"]-1, "sample hold return")
        mature += 1
    return result(["raw_source_and_calendar_origins", "latest_causal_features", "common_label_maturity",
                   "five_targets_and_six_rights_latents"], "ready" if mature else "insufficient_evidence")


def _eligible(samples, codes, origin, horizon, policy, training_ceiling_at=None):
    lower = (dt.date.fromisoformat(origin)-dt.timedelta(days=policy["train_window_days"])).isoformat()
    from research_data import source_time
    boundary = min(origin, training_ceiling_at[:10] if training_ceiling_at else origin)
    selected = []
    for original in samples:
        row = {key: value for key, value in original.items() if key != "label_versions"}
        if "label_versions" in original:
            versions = [version for version in original["label_versions"]
                        if source_time(version["label_available_at"]) < source_time(boundary+"T00:00:00+08:00")]
            if not versions:
                continue
            row.update(max(versions, key=lambda version: source_time(version["label_available_at"])))
        selected.append(row)
    return sorted((row for row in selected if row["code"] in codes and row["horizon_days"] == horizon
                   and lower <= row["decision_date"] < origin and row.get("label_available_date") is not None
                   and row["label_available_date"] < min(origin, training_ceiling_at[:10] if training_ceiling_at else origin) and row.get("targets") is not None
                   and row.get("industry_ready") is not False),
                  key=lambda row: (row["decision_date"], row["code"]))


def _weights(rows):
    counts = collections.Counter((row["decision_date"], row["fund_group_id"]) for row in rows)
    groups = collections.Counter(day for day, _ in counts)
    return [1/(groups[row["decision_date"]]*counts[row["decision_date"], row["fund_group_id"]]) for row in rows]


def _model_prediction(model, vector):
    return {name: float(model["intercept"][index])+math.fsum(coefficient*(value-mean)/scale
             for coefficient, value, mean, scale in zip(model["coefficients"][index], vector, model["scaler_mean"], model["scaler_scale"]))
            for index, name in enumerate(LATENTS)}


def _model(model, rows, names):
    check(rows, "nonempty mature rows required for a fitted model")
    size = len(names)
    check(model["target_names"] == list(LATENTS) and model.get("feature_names", names) == names
          and len(model["scaler_mean"]) == len(model["scaler_scale"]) == size
          and len(model["coefficients"]) == len(model["intercept"]) == len(LATENTS)
          and all(len(row) == size for row in model["coefficients"]), "model dimensions and ordering")
    check(all(math.isfinite(float(value)) for row in model["coefficients"] for value in row)
          and all(math.isfinite(float(value)) for value in model["intercept"]), "model finite coefficients")
    weights = _weights(rows)
    mass = math.fsum(weights)
    for index in range(size):
        mean = math.fsum(weight*row["x"][index] for row, weight in zip(rows, weights))/mass
        variance = math.fsum(weight*(row["x"][index]-mean)**2 for row, weight in zip(rows, weights))/mass
        close(model["scaler_mean"][index], mean, "weighted scaler mean")
        # The pinned scaler treats variance below the float64 moment error
        # bound as constant; repeated decimal features can have a tiny residue.
        epsilon = math.ulp(1.)
        constant_bound = mass*epsilon*variance+(mass*mean*epsilon)**2
        scale = 1 if variance <= constant_bound else math.sqrt(variance)
        close(model["scaler_scale"][index], scale, "weighted scaler scale")
        check(float(model["scaler_scale"][index]) > 0, "positive scaler scale")
    for index, name in enumerate(LATENTS):
        value = rows[0]["latent_targets"][name]
        if all(row["latent_targets"][name] == value for row in rows):
            check(all(coefficient == 0 for coefficient in model["coefficients"][index]), "constant latent zero coefficients")
            close(model["intercept"][index], value, "constant latent intercept")


def _training_audit(model, audit, samples, codes, origin, horizon, policy):
    rows = _eligible(samples, codes, origin, horizon, policy, audit.get("training_ceiling_at"))
    dates = sorted({row["decision_date"] for row in rows})
    check(audit["decision_date"] == origin and audit["training_rows"] == len(rows)
          and audit["training_dates"] == len(dates) and audit["training_data_hash"] == fingerprint(rows)
          and audit["policy_hash"] == fingerprint(policy), "training audit source binding")
    expected_max = max((row["label_available_date"] for row in rows), default=None)
    check(audit["max_label_available_date"] == expected_max, "strict mature training boundary")
    if model is None:
        return
    _model(model, rows, policy["feature_names"])
    initial = max(policy["min_train_dates"], int(len(dates)*policy["cv_initial_train_fraction"]))
    cuts = [initial+(len(dates)-initial)*index//policy["cv_folds"] for index in range(policy["cv_folds"]+1)]
    folds = []
    for start, stop in zip(cuts[:-1], cuts[1:]):
        check(start < stop, "nonempty chronological CV fold")
        first = dates[start]
        train = _eligible(samples, codes, first, horizon, policy)
        validation = [row for row in rows if first <= row["decision_date"] <= dates[stop-1]]
        folds.append((train, validation))
    check(len(audit["folds"]) == len(folds), "CV fold inventory")
    for declared, (train, validation) in zip(audit["folds"], folds):
        check(declared["validation_start"] == validation[0]["decision_date"]
              and declared["validation_end"] == validation[-1]["decision_date"]
              and declared["training_rows"] == len(train) and declared["validation_rows"] == len(validation)
              and declared["training_max_label_available"] == max(row["label_available_date"] for row in train)
              and len({row["decision_date"] for row in train}) >= policy["min_train_dates"], "CV purge and chronology")
    trials = audit["cv_trials"]
    check([(trial["alpha"], trial["l1_ratio"]) for trial in trials]
          == [(alpha, ratio) for alpha in sorted(set(policy["alpha_grid"])) for ratio in sorted(set(policy["l1_ratio_grid"]))], "complete CV parameter grid")
    for trial in trials:
        check(len(trial["fold_mse"]) == len(folds) == len(trial["fold_models"]), "CV prediction evidence inventory")
        for declared, fitted, (train, validation) in zip(trial["fold_mse"], trial["fold_models"], folds):
            _model(fitted, train, policy["feature_names"])
            check(fitted["alpha"] == trial["alpha"] and fitted["l1_ratio"] == trial["l1_ratio"], "CV fold model parameters")
            weights, errors = _weights(validation), []
            for row in validation:
                prediction = _model_prediction(fitted, row["x"])
                errors.append(math.fsum((row["latent_targets"][name]-prediction[name])**2 for name in LATENTS)/len(LATENTS))
            close(declared, math.fsum(weight*error for weight, error in zip(weights, errors))/math.fsum(weights), "independent weighted CV MSE")
        close(trial["mean_mse"], math.fsum(trial["fold_mse"])/len(folds), "CV mean error")
    chosen = min(trials, key=lambda trial: (trial["mean_mse"], trial["alpha"], trial["l1_ratio"]))
    check(model["alpha"] == chosen["alpha"] and model["l1_ratio"] == chosen["l1_ratio"], "CV parameter selection")


def validate_fit(output, inputs):
    context, samples = inputs["context"], inputs["samples"]
    if "industry_forecast" in output:
        frozen = output["industry_forecast"]
        from industry_binding import family_industry_scope
        scoped = family_industry_scope(inputs["data"], context["allocation_codes"], context["decision_at"])["industry"]
        validate_industry(frozen, {"context": context, "data": scoped,
            "origins": sorted({row["decision_date"] for row in samples}), "policy": frozen["policy"], "clock": inputs["clock"]})
        primitive = [row for row in samples if row["decision_date"] == context["as_of"]]
        primitive = [{key: value for key, value in row.items() if not key.startswith("industry_")} for row in primitive]
        from allocation_market import FEATURE_NAMES
        for row in primitive:
            row["x"] = row["x"][:len(FEATURE_NAMES)]
        validate_industry_bridge(output["current_prediction_samples"], {"samples": primitive, "industry": frozen,
            "exposures": inputs["data"]["industry_exposures"], "clock": inputs["clock"]})
        samples = [row for row in samples if row["decision_date"] != context["as_of"]] + output["current_prediction_samples"]
        check(output["industry_forecast_hash"] == frozen["forecast_hash"]
              and output["industry_data_hash"] == fingerprint(scoped), "frozen current stack source binding")
    codes, request = context["allocation_codes"], context["model_request"]
    from allocation_runner import fund_training_policy
    policy, horizon = fund_training_policy(context, samples), request["horizon_days"]
    models = output.get("models", [])
    check(len(models) <= 1, "single Elastic Net family")
    model = models[0] if models else None
    _training_audit(model, output["training_audit"], samples, codes, context["as_of"], horizon, policy)
    by_code = {row["code"]: row for row in output["forecasts"]}
    if model is not None:
        check(len(output["forecasts"]) == len(codes) and set(by_code) == set(codes), "point forecast inventory")
        for code in codes:
            latest = max((row for row in samples if row["code"] == code and row["decision_date"] <= context["as_of"]), key=lambda row: row["decision_date"])
            check(latest["decision_date"] == context["as_of"] and by_code[code]["feature_cutoff_date"] == latest["feature_cutoff_date"], "current causal forecast origin")
            prediction = _model_prediction(model, latest["x"])
            declared = by_code[code].get("predicted_latents", dict(zip(LATENTS, by_code[code].get("predicted_coordinates", []))))
            for name in LATENTS:
                close(declared[name], prediction[name], "standardized point dot product " + name)
    records = output["training_audit"].get("oos_predictions", [])
    for record in records:
        _training_audit(record["model"], record["training_audit"], samples, codes, record["date"], horizon, policy)
        for column, code in enumerate(codes):
            row = next(row for row in samples if row["decision_date"] == record["date"] and row["code"] == code)
            prediction = _model_prediction(record["model"], row["x"])
            for name in LATENTS:
                close(record["predicted_latents"][name][column], prediction[name], "OOS model dot product " + name)
    joint = output.get("joint_scenarios")
    if joint is not None:
        _distribution(joint)
        check(output["status"] == "research_ready" and joint["source_oos"] == records
              and len(records) >= policy["min_joint_dates"] and joint["codes"] == codes, "joint OOS evidence inventory")
        selected = joint["source_selection_oos"]
        calibration = joint["source_calibration_oos"]
        expected_selected = records[:len(records)//2]
        expected_calibration = [record for record in records[len(records)//2:]
                                if record["date"] > max(row["label_available"] for row in expected_selected)]
        split = joint["panel_split"]
        check(selected == expected_selected and calibration == expected_calibration and selected and calibration
              and split["kind"] == "chronological_label_embargo_v1" and split["selection_fraction"] == .5
              and split["selection_origin_dates"] == joint["dates"] == [row["date"] for row in selected]
              and split["calibration_origin_dates"] == [row["date"] for row in calibration]
              and split["selection_source_hash"] == fingerprint(selected)
              and split["calibration_source_hash"] == fingerprint(calibration), "independent selection calibration panels with label embargo")
        if "industry_forecast" in output:
            ceiling = calibration[0]["origin_at"]
            check(output["training_audit"].get("training_ceiling_at") == ceiling
                  and output["industry_forecast"].get("training_ceiling_at") == ceiling,
                  "current industry and fund supervised labels excluded from calibration")
        check(len(joint["probabilities"]) == len(selected), "joint probability inventory")
        for probability in joint["probabilities"]:
            close(probability, 1/len(selected), "empirical origin probability")
        verify._joint_source_invariants(context, output, inputs["data"], close, rebased=False)
        for index, forecast in enumerate(output["forecasts"]):
            for name in TARGETS:
                close(forecast["predicted_log_targets"][name], math.log(joint["current_point_targets"][name][0][index]), "current point log target")
            close(forecast["expected_gross_return"], math.fsum(p*(row[index]-1) for p, row in zip(joint["probabilities"], joint["targets"]["hold"])), "point empirical return")
    else:
        check(output["status"] == "insufficient_evidence" and output.get("reason"), "unready fit evidence status")
    checked = ["source_mature_training"]
    if model is not None:
        checked.extend(["model_shape_and_weighted_scaler", "point_dot_products", "CV_chronology_errors_and_selection"])
    if records:
        checked.append("OOS_dot_products_and_forward_training")
    if joint is not None:
        checked.append("source_residuals_and_joint_pairing")
    return result(checked,
                  output["status"], ["Elastic_Net_optimality_not_independently_proven"], output.get("reason"))


def _industry_prediction(model, row):
    from industry_validation import prediction
    return prediction(model, row)


def _industry_model(model, train, names):
    from industry_validation import model_math
    return model_math(model, train, names)


def validate_industry(output, inputs):
    from industry_validation import validate
    return validate(output, inputs)


def validate_industry_bridge(output, inputs):
    from industry_model import FUND_FEATURE_NAMES, INPUT_SCHEMA_ID
    from asset_domains import ROLES
    from industry_validation import validate_bridge_witness
    samples, industry, exposures = inputs["samples"], inputs["industry"], inputs["exposures"]
    time = inputs["clock"]["order_time_local"]
    forecasts = {(row["decision_date"], row["sector_id"]): row for row in industry["forecasts"]}
    check(len(output) == len(samples), "industry bridge calendar inventory")
    ready = 0
    for row, before in zip(output, samples):
        check(all(row[key] == value for key, value in before.items() if key != "x"), "industry bridge preserves fund cash/price source outcomes")
        day, code = before["decision_date"], before["code"]
        at = day+"T"+time+"+08:00"
        known = [item for item in exposures.get(code, []) if item["as_of_date"] <= day and instant(item["known_at"]) <= instant(at)]
        mapping = []
        for basis in ("historical_disclosure", "model_estimate"):
            group = [item for item in known if item["basis"] == basis]
            if group:
                snapshot = max((item["as_of_date"], item["known_at"]) for item in group)
                mapping.extend(item for item in group if (item["as_of_date"], item["known_at"]) == snapshot)
        disclosed = {item["sector_id"] for item in mapping if item["basis"] == "historical_disclosure"}
        mapping = [item for item in mapping if item["basis"] != "model_estimate" or item["sector_id"] not in disclosed]
        identities = [(item["sector_id"],item["role"],item.get("source_label"),item["basis"]) for item in mapping]
        valid = bool(mapping) and len(set(identities)) == len(identities) and all(item.get("evidence_refs") and item["role"] in ROLES for item in mapping)
        reference = {}
        for item in mapping:
            coefficient = float(item["coefficient"] if item["basis"] == "model_estimate" else item["weight"])
            valid = valid and math.isfinite(coefficient) and (item["basis"] == "model_estimate" or 0 < coefficient <= 1)
            if item["basis"] == "historical_disclosure":
                label = item["source_label"]
                valid = valid and (label not in reference or reference[label] == coefficient)
                reference[label] = coefficient
            forecast = forecasts.get((day,item["sector_id"]))
            valid = valid and forecast is not None and forecast["horizon_days"] == before["horizon_days"] and forecast.get("input_schema_id") == INPUT_SCHEMA_ID
        valid = valid and math.fsum(reference.values()) <= 1+1e-12
        check(row["industry_ready"] == valid, "industry bridge source readiness")
        if not valid:
            check(row["x"] == before["x"] and row["industry_source"] is None and row["industry_reason"], "missing industry evidence is a gap without invented zero")
            continue
        witness = row["industry_source"]
        predicted = {entity: forecast for (origin,entity),forecast in forecasts.items() if origin == day}
        validate_bridge_witness(witness, exposures.get(code, []), predicted)
        check(row["x"] == before["x"]+witness["values"] and witness["feature_names"] == FUND_FEATURE_NAMES,
              "asset role covariates preserve the original NAV source targets")
        ready += 1
    return result(["PIT_source_exposures", "forward_sector_prediction_binding", "no_industry_cash_or_error_addition"],
                  "ready" if ready else "insufficient_evidence")


def validate_rebase(output, inputs):
    before, context, data = inputs["fitting"], inputs["context"], inputs["data"]
    check(output["models"] == before["models"] and output["training_audit"] == before["training_audit"], "rebase preserves fit evidence")
    joint, old = output["joint_scenarios"], before["joint_scenarios"]
    _distribution(joint)
    check(len(output["forecasts"]) == len(joint["codes"]), "rebased forecast inventory")
    for code in joint["codes"]:
        column = joint["codes"].index(code)
        forecast = next(row for row in before["forecasts"] if row["code"] == code)
        base, mark = forecast["feature_cutoff_date"], context["market_ref"]["price_dates"][code]
        rows = {row["date"]: row for row in data["nav"][code]}
        check(base <= mark <= context["as_of"], "current mark chronology")
        close(rows[mark]["nav"], context["market_ref"]["prices"][code], "current mark source")
        paid = math.fsum(float(row["distribution_per_share"]) for day, row in rows.items() if base < day <= mark)
        for key in ("targets", "current_point_targets"):
            for name in TARGETS:
                check(len(joint[key][name]) == len(old[key][name]), "rebase scenario inventory")
                for actual, original in zip(joint[key][name], old[key][name]):
                    expected = original[column] if name == "buy" else (original[column]*float(rows[base]["nav"])-(paid if name in ("hold", "sell") else 0))/float(rows[mark]["nav"])
                    close(actual[column], expected, "independent current mark rebase " + name)
                    check(actual[column] > 0, "positive rebased wealth")
        current = next(row for row in output["forecasts"] if row["code"] == code)
        for name in TARGETS:
            close(current["predicted_log_targets"][name], math.log(joint["current_point_targets"][name][0][column]), "rebased point log target")
        close(current["expected_gross_return"], math.fsum(p*(row[column]-1) for p, row in zip(joint["probabilities"], joint["targets"]["hold"])), "rebased expected gross return")
    verify._joint_source_invariants(context, output, data, close)
    return result(["source_current_mark", "known_cash_separation", "independent_five_target_rebase"])


def validate_comparison(output, inputs):
    from allocation import build_source_actions, validate_source_action
    context = inputs["context"]
    expected = build_source_actions(context)
    check(output == expected, "source action family differs from sealed source reconstruction")
    if output["status"] == "frozen":
        for group in output["funding_options"]:
            check(group["current_action_scope_complete"], "incomplete declared source family")
            for action in group["actions"]:
                validate_source_action(context, action, funding=group["proposed_contribution"])
    return result(["canonical_source_cash_share_actions", "declared_family_computation_complete_or_explicit_partial",
                   "settled_cash_and_counterfactual_funding_separation"],
                  limitations=["selection_requires_complete_multi_period_path_wealth", "global_investment_optimality_not_claimed"])


def validate_compile(output, inputs):
    calculation = {**inputs["calculation"], "orders": output}
    arithmetic = verify.numerical_invariants(inputs["context"], calculation, inputs["data"])
    return result(["source_MPC_current_action", "independent_cash_share_flow", arithmetic], output["status"], reason=output.get("reason"))
