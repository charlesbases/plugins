"""Past-only learned NAV style slopes; these never certify actual holdings."""
import datetime as dt
import math
import warnings
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import StandardScaler
from contracts import fingerprint, instant
import asset_domains
from allocation_market import nav_snapshot
from industry_model import origin_at, prices_at


def _source_window(code, nav_rows, before, after, at):
    """Use every listed source observation; never infer missing calendar dates."""
    raw = sorted((row for row in nav_rows if before <= row["date"] <= after), key=lambda row: row["date"])
    if not raw or raw[0]["date"] != before or raw[-1]["date"] != after or len({row["date"] for row in raw}) != len(raw):
        return None
    quotes, events = [], []
    try:
        for row in raw:
            quote = nav_snapshot(row, at, require_known=True)
            if quote is None or quote.get("strict_PIT_verified") is not True or quote["code"] != code:
                return None
            amount = float(quote["distribution_per_share"])
            if not math.isfinite(amount) or amount < 0 or not math.isfinite(float(quote["nav"])) or float(quote["nav"]) <= 0:
                return None
            cash = [event for event in quote.get("corporate_actions", []) if event.get("kind") == "cash_distribution"]
            for event in cash:
                if (not all(event.get(key) for key in ("record_date", "ex_date", "pay_date", "known_at", "evidence_ref", "entitlement_rule"))
                    or event.get("code") != code or event.get("currency") != "CNY" or event.get("distribution_mode") != "cash"
                    or not isinstance(event["entitlement_rule"], dict)
                    or not event["record_date"] <= event["ex_date"] <= event["pay_date"] or event["ex_date"] != quote["date"]
                    or not math.isfinite(float(event["per_share"])) or float(event["per_share"]) <= 0
                    or instant(event["known_at"]) >= instant(at)):
                    return None
            if not math.isclose(math.fsum(float(event["per_share"]) for event in cash), amount, rel_tol=0., abs_tol=1e-12):
                return None
            quotes.append(quote)
            events.extend(cash)
        known = max([quote["known_at"] for quote in quotes]+[event["known_at"] for event in events], key=instant)
        if instant(known) >= instant(at):
            return None
        cash = math.fsum(float(event["per_share"]) for event in events if before < event["ex_date"] <= after)
        return {"NAV": [quotes[0], quotes[-1]], "window_NAV": quotes, "events": events,
            "known_at": known, "y": math.log((float(quotes[-1]["nav"])+cash)/float(quotes[0]["nav"])),
            "window_scope": "all_listed_source_observations; unlisted_calendar_absence_not_proven",
            "wealth_basis": "one_share_cash_held_without_reinvestment"}
    except (KeyError, TypeError, ValueError):
        return None


def estimate(code, nav_rows, origins, sectors, policy, order_time_local):
    result = []
    for day in sorted(set(origins)):
        at = origin_at(day, order_time_local)
        active = [sector for sector in sectors if sector.get("sector_relation", {}).get("status") == "verified"
                  and sector["sector_relation"].get("known_at") and instant(sector["sector_relation"]["known_at"]) < instant(at)]
        if not active:
            continue
        nav = {row["date"]: quote for row in nav_rows if row["date"] < day
               and (quote := nav_snapshot(row, at, require_known=True)) is not None and quote.get("strict_PIT_verified") is True}
        factors = [{row["date"]: row for row in prices_at(sector, at) if row["date"] < day} for sector in active]
        common = sorted(set(nav).intersection(*(set(rows) for rows in factors)))
        lower = (dt.date.fromisoformat(day)-dt.timedelta(days=policy["train_window_days"])).isoformat()
        common = [value for value in common if value >= lower]
        panel = []
        for before, after in zip(common, common[1:]):
            window = _source_window(code, nav_rows, before, after, at)
            if window is None:
                continue
            source = {key: window[key] for key in ("NAV", "window_NAV", "events", "window_scope", "wealth_basis")}
            source["assets"] = [[values[before], values[after]] for values in factors]
            known = max([window["known_at"]]+[row["available_at"] for rows in source["assets"] for row in rows], key=instant)
            if instant(known) >= instant(at):
                continue
            x = [asset_domains.target_coordinates(sector["asset_domain"]["return_target"],
                 float(values[before]["close"]), float(values[after]["close"]), float(values[after]["close"]))[0]
                 for sector, values in zip(active, factors)]
            panel.append({"date": after, "known_at": known, "x": x, "y": window["y"], "source": source})
        minimum = max(20, policy["min_train_dates"])
        if len(panel) < minimum+5:
            continue
        cut = max(minimum, int(len(panel)*policy["cv_initial_train_fraction"]))
        if cut >= len(panel):
            continue
        validation_known = panel[cut]["known_at"]
        inner_train = [row for row in panel[:cut] if instant(row["known_at"]) < instant(validation_known)]
        if len(inner_train) < minimum:
            continue
        def fitted(rows, alpha, ratio):
            x = np.asarray([row["x"] for row in rows]); y = np.asarray([row["y"] for row in rows])
            scaler = StandardScaler().fit(x)
            estimator = ElasticNet(alpha=alpha, l1_ratio=ratio, max_iter=20000, tol=1e-8)
            with warnings.catch_warnings():
                warnings.simplefilter("error", ConvergenceWarning)
                estimator.fit(scaler.transform(x), y)
            return scaler, estimator
        def witness(scaler, estimator):
            return {"scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
                    "coefficients": estimator.coef_.tolist(), "intercept": float(estimator.intercept_)}
        try:
            trials = []
            for alpha in policy["alpha_grid"]:
                for ratio in policy["l1_ratio_grid"]:
                    scaler, estimator = fitted(inner_train, alpha, ratio)
                    guesses = estimator.predict(scaler.transform([row["x"] for row in panel[cut:]]))
                    trials.append({"alpha": alpha, "l1_ratio": ratio, "model": witness(scaler, estimator),
                        "mse": math.fsum((row["y"]-float(guess))**2 for row, guess in zip(panel[cut:], guesses))/len(guesses)})
            chosen = min(trials, key=lambda row: (row["mse"], row["alpha"], row["l1_ratio"]))
            scaler, estimator = fitted(panel, chosen["alpha"], chosen["l1_ratio"])
        except ConvergenceWarning:
            continue
        audit = {"input_schema_id": asset_domains.INPUT_SCHEMA_ID, "code": code, "origin_at": at,
                 "sector_ids": [sector["sector_id"] for sector in active], "panel": panel, "training_data_hash": fingerprint(panel), "training_rows": len(panel), "trials": trials,
                 "validation_start": panel[cut]["date"], "validation_first_known_at": validation_known,
                 "inner_train_max_known_at": max((row["known_at"] for row in inner_train), key=instant), "selected": chosen, "source_information_known_at": max((row["known_at"] for row in panel), key=instant),
                 "scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
                 "coefficients": estimator.coef_.tolist(), "intercept": float(estimator.intercept_),
                 "role": "reproducible_PIT_NAV_style_estimate_not_observed_holdings"}
        for index, sector in enumerate(active):
            beta = float(estimator.coef_[index])/float(scaler.scale_[index])
            result.append({"sector_id": sector["sector_id"], "role": sector["asset_domain"]["role"],
                "coefficient": beta, "as_of_date": day, "known_at": at, "source_known_at": audit["source_information_known_at"],
                "basis": "model_estimate", "actual_exposure_status": "unknown", "actual_weight": None,
                "stale": False, "audit": audit,
                "evidence_refs": [quote["raw_ref"] for row in panel for quote in row["source"]["window_NAV"]]
                    + [event["evidence_ref"] for row in panel for event in row["source"]["events"]],
                "model_hash": fingerprint(audit)})
    return result


def validate_estimates(exposures, nav_by_code, sectors):
    """Independent source arithmetic, maturity, scaling, EN KKT and holdout loss."""
    from numeric_validation import check, close
    lookup = {sector["sector_id"]: sector for sector in sectors}
    checked = set()
    for code, rows in exposures.items():
        raw_nav = {row["date"]: row for row in nav_by_code.get(code, [])}
        for row in rows:
            if row["basis"] != "model_estimate":
                continue
            audit = row["audit"]
            check(row["actual_weight"] is None and row["actual_exposure_status"] == "unknown", "estimated beta is not actual capital weight")
            check(row["model_hash"] == fingerprint(audit) and audit["input_schema_id"] == asset_domains.INPUT_SCHEMA_ID, "current style estimate audit hash")
            index = audit["sector_ids"].index(row["sector_id"])
            close(row["coefficient"], audit["coefficients"][index]/audit["scaler_scale"][index], "style raw-unit slope")
            if row["model_hash"] in checked:
                continue
            checked.add(row["model_hash"])
            panel, at = audit["panel"], audit["origin_at"]
            check(panel and audit["training_data_hash"] == fingerprint(panel) and audit["training_rows"] == len(panel), "original style source panel")
            expected_refs = [quote["raw_ref"] for value in panel for quote in value["source"]["window_NAV"]]
            expected_refs += [event["evidence_ref"] for value in panel for event in value["source"]["events"]]
            check(row["evidence_refs"] == expected_refs, "complete style NAV and cash evidence references")
            factor_quotes = {entity: prices_at(lookup[entity], at) for entity in audit["sector_ids"]}
            for value in panel:
                source = value["source"]
                before, after = source["NAV"]
                # Rebuild from original observations, independently of the label helper.
                dates = sorted(date for date in raw_nav if before["date"] <= date <= after["date"])
                quotes = [nav_snapshot(raw_nav[date], at, require_known=True) for date in dates]
                check(quotes and all(quote is not None and quote.get("strict_PIT_verified") is True and quote["code"] == code
                    for quote in quotes), "complete strictly known original style NAV window")
                check(source["window_NAV"] == quotes and before == quotes[0] and after == quotes[-1], "complete style source NAV versions")
                check(source["window_scope"] == "all_listed_source_observations; unlisted_calendar_absence_not_proven"
                    and source["wealth_basis"] == "one_share_cash_held_without_reinvestment", "explicit style source coverage and cash convention")
                events = []
                for quote in quotes:
                    amount = float(quote["distribution_per_share"])
                    check(math.isfinite(amount) and amount >= 0 and math.isfinite(float(quote["nav"])) and float(quote["nav"]) > 0,
                          "finite original style NAV and explicit distribution")
                    cash = [event for event in quote.get("corporate_actions", []) if event.get("kind") == "cash_distribution"]
                    for event in cash:
                        check(all(event.get(key) for key in ("record_date", "ex_date", "pay_date", "known_at", "evidence_ref", "entitlement_rule"))
                              and event.get("code") == code and event.get("currency") == "CNY" and event.get("distribution_mode") == "cash"
                              and isinstance(event["entitlement_rule"], dict) and event["record_date"] <= event["ex_date"] <= event["pay_date"]
                              and event["ex_date"] == quote["date"] and math.isfinite(float(event["per_share"])) and float(event["per_share"]) > 0
                              and instant(event["known_at"]) < instant(at), "independent complete style cash rights source")
                    close(math.fsum(float(event["per_share"]) for event in cash), amount, "independent style cash coverage")
                    events.extend(cash)
                check(source["events"] == events, "complete original style cash event window")
                cash_balance = math.fsum(float(event["per_share"]) for event in events if before["date"] < event["ex_date"] <= after["date"])
                close(value["y"], math.log((float(after["nav"])+cash_balance)/float(before["nav"])), "style one-share CNY NAV plus held cash label")
                check(instant(value["known_at"]) < instant(at) and before["date"] < after["date"] < at[:10], "strict style label maturity")
                for column, entity in enumerate(audit["sector_ids"]):
                    first, last = source["assets"][column]
                    check(first in factor_quotes[entity] and last in factor_quotes[entity]
                          and first["date"] == before["date"] and last["date"] == after["date"], "original style factor versions and aligned source periods")
                    target = lookup[entity]["asset_domain"]["return_target"]
                    base, terminal = float(first["close"]), float(last["close"])
                    expected = math.log(terminal/base) if target["transform"] == "price_log_return" else terminal-base
                    close(value["x"][column], expected, "independent style factor economic target")
                known = max([quote["known_at"] for quote in quotes]+[event["known_at"] for event in events]
                    +[quote["available_at"] for pair in source["assets"] for quote in pair], key=instant)
                check(value["known_at"] == known, "style source known time is not a calendar backfill")
            check(audit["source_information_known_at"] == max((value["known_at"] for value in panel), key=instant)
                  and row["source_known_at"] == audit["source_information_known_at"], "full-window style source maturity")
            def model_math(model, training, alpha, ratio):
                width = len(audit["sector_ids"])
                for column in range(width):
                    values = [value["x"][column] for value in training]
                    mean = math.fsum(values)/len(values)
                    variance = math.fsum((value-mean)**2 for value in values)/len(values)
                    bound = len(values)*math.ulp(1.)*variance+(len(values)*mean*math.ulp(1.))**2
                    close(model["scaler_mean"][column], mean, "independent style scaler mean")
                    close(model["scaler_scale"][column], 1. if variance <= bound else math.sqrt(variance), "independent style scaler scale")
                z = [[(x-m)/scale for x,m,scale in zip(value["x"],model["scaler_mean"],model["scaler_scale"])] for value in training]
                residual = [value["y"]-model["intercept"]-math.fsum(b*x for b,x in zip(model["coefficients"],x)) for value,x in zip(training,z)]
                tolerance = 2e-6*max(1.,alpha,max(abs(value) for value in residual))
                check(abs(math.fsum(residual)/len(residual)) <= tolerance, "style EN intercept stationarity")
                for column,beta in enumerate(model["coefficients"]):
                    gradient = math.fsum(vector[column]*error for vector,error in zip(z,residual))/len(residual)
                    smooth, threshold = gradient-alpha*(1-ratio)*beta, alpha*ratio
                    check(abs(smooth-threshold*math.copysign(1.,beta)) <= tolerance if beta else abs(smooth) <= threshold+tolerance, "style EN KKT")
            validation = [value for value in panel if value["date"] >= audit["validation_start"]]
            train = [value for value in panel if value["date"] < audit["validation_start"]
                     and instant(value["known_at"]) < instant(audit["validation_first_known_at"])]
            check(train and validation and max((value["known_at"] for value in train),key=instant) == audit["inner_train_max_known_at"], "strict style holdout source boundary")
            for trial in audit["trials"]:
                model = trial["model"]
                model_math(model,train,trial["alpha"],trial["l1_ratio"])
                errors = []
                for value in validation:
                    prediction = model["intercept"]+math.fsum(beta*(x-mean)/scale for beta,x,mean,scale in
                        zip(model["coefficients"],value["x"],model["scaler_mean"],model["scaler_scale"]))
                    errors.append((value["y"]-prediction)**2)
                close(trial["mse"],math.fsum(errors)/len(errors),"independent style holdout MSE")
            selected = min(audit["trials"],key=lambda item:(item["mse"],item["alpha"],item["l1_ratio"]))
            check(selected == audit["selected"], "past holdout style parameter selection")
            model_math(audit,panel,selected["alpha"],selected["l1_ratio"])
    return {"status": "passed", "scope": "source_PIT_style_beta_not_actual_holdings", "checked_model_count": len(checked)}
