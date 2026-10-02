"""Forward-only joint multi-date wealth/cash targets for the same EN family.

Every historical NAV ordinate is measured, never interpolated. A frozen cash
timing dictionary is learned only from the initial training segment. Its zero
estimate is empirical, never evidence that future distributions cannot occur.
All leaves share a coarse risk partition. Frozen policies may use the lagged
observable prefix; the terminal-risk study does not claim full dynamic conditional
CVaR or identify an unobserved future outcome from a unique historical prefix. Historical errors are a reconstructed
normal-clock study, not evidence of historical actual fills or future profit.
"""
import copy
import datetime as dt
import math
import json
import warnings
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import StandardScaler
from sklearn.exceptions import ConvergenceWarning

from contracts import fingerprint, instant, require
import allocation_statistics as statistics
import allocation_market as market
import industry_model
import fee_contract as fees
import single_step_wealth
import trading_calendar


def stage_contract(context, max_stages=8):
    require(type(max_stages) is int and max_stages >= 2, "Explicit path-stage computation budget >=2 required")
    clock = single_step_wealth.build_clock_context(context)
    day = context["as_of"]
    end = clock["evaluation_date"]
    codes = context["allocation_codes"]
    calendars = [context["fee_contracts"][code]["execution_calendar"] for code in codes]
    common = trading_calendar.intersection(calendars)
    known_marks = context.get("known_marks") or context.get("market_ref", {}).get("known_marks")
    require(isinstance(known_marks, dict) and set(codes) <= set(known_marks), "Source-known valuation marks required for role-price paths")
    known_marks = {code: known_marks[code] for code in codes}
    dates = sorted(date for date in common["open_dates"] if day <= date <= end)
    require(dates[-1] == end, "Current common terminal observation is outside verified source calendars")
    stages, boundaries = {day, end}, []
    for code in codes:
        terms, asset = context["fee_contracts"][code], clock["assets"][code]
        rule = terms["settlement"]
        # Conservative normal arrival; actual receipt still requires feedback.
        settled = trading_calendar.advance(asset["confirmation_date"], rule["lag_days"],
            rule["day_basis"], rule.get("calendar", terms["execution_calendar"]))
        record = {"code": code, "pricing_date": asset["pricing_date"],
                  "confirmation_date": asset["confirmation_date"], "settlement_date": str(settled),
                  "settlement_basis": "normal_confirmation_then_source_lag_not_actual_credit_guarantee"}
        boundaries.append(record)
        require(record["pricing_date"] in dates, "Source pricing date requires its own complete closing-NAV coordinate")
        for key in ("pricing_date", "confirmation_date", "settlement_date"):
            date = record[key]
            if day < date < end:
                stages.add(date)
    # Quote and accounting observations do not spend a policy's local decision budget.
    return {"stage_dates": sorted(stages), "nav_dates": dates, "codes": codes, "origin_date": day,
            "known_marks": copy.deepcopy(known_marks), "decision_at": context["decision_at"],
            "price_schema": "source_role_price_paths_v3",
            "offsets": [(dt.date.fromisoformat(date)-dt.date.fromisoformat(day)).days for date in dates],
            "boundaries": boundaries, "clock_hash": fingerprint(clock), "max_stages": max_stages}


def _cash_key(event, origin):
    base = dt.date.fromisoformat(origin)
    return tuple((dt.date.fromisoformat(event[key])-base).days for key in ("record_date", "ex_date", "pay_date")) + (
        event["currency"], event["distribution_mode"],
        json.dumps(event["entitlement_rule"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _frames(context, data, samples, contract):
    """Independent multi-date labels; scalar fund labels are provenance only."""
    raw = {code: {row["date"]:row for row in data["nav"][code]} for code in context["allocation_codes"]}
    common_dates = sorted(set.intersection(*(set(rows) for rows in raw.values())))
    frames,unavailable = [],[]
    for sample in samples:
        if sample["code"] not in raw or sample.get("industry_ready") is not True:
            continue
        code,origin = sample["code"],sample["decision_date"]
        try:
            scalar = sample.get("label_source")
            if scalar is None:
                versions = sample.get("label_versions",[])
                scalar = next((value.get("label_source") for value in versions if value.get("label_source")),None)
            require(scalar is not None,"Original feature/base source association is missing")
            requested = [(dt.date.fromisoformat(origin)+dt.timedelta(days=offset)).isoformat() for offset in contract["offsets"]]
            dates = [next(date for date in common_dates if date >= target) for target in requested]
            last = dates[-1]
            base = float(sample["feature_source"]["base_nav"])
            require(base > 0,"Positive historical feature NAV required")
            window_dates = sorted(date for date in raw[code] if scalar["base_date"] < date <= last)
            require(window_dates and window_dates[-1] == last,"Complete source path window is missing")
            times = set()
            for date in window_dates:
                item = raw[code][date]
                for version in item.get("source_versions",[]):
                    at = market.source_version_available(version)
                    if at is not None: times.add(at.isoformat())
                if item.get("available_at"): times.add(item["available_at"])
                times.update(event["known_at"] for event in item.get("corporate_actions",[]) if event.get("known_at"))
            require(times,"Independent full-window source availability is missing")
            produced,previous_state = [],None
            for at in sorted(times,key=instant):
                quotes = [market.nav_snapshot(raw[code][date],at,require_known=True) for date in window_dates]
                if any(quote is None for quote in quotes):
                    continue
                require(all(quote.get("known_at") and quote.get("strict_PIT_verified") is True for quote in quotes),"Unverified original NAV window cannot establish mature path labels")
                events = []
                for quote in quotes:
                    cash = [event for event in quote.get("corporate_actions",[]) if event.get("kind") == "cash_distribution"]
                    require(math.isclose(math.fsum(float(event["per_share"]) for event in cash),float(quote["distribution_per_share"]),abs_tol=1e-12),"Full-window cash coverage differs from raw NAV source")
                    for event in cash:
                        require(all(event.get(key) is not None for key in ("record_date","ex_date","pay_date","entitlement_rule","evidence_ref","known_at"))
                                and event["currency"] == "CNY" and event["distribution_mode"] == "cash"
                                and event["ex_date"] == quote["date"] and event["record_date"] <= event["ex_date"] <= event["pay_date"],"Full-window cash rights/availability source is incomplete")
                    events += copy.deepcopy(cash)
                maturity = max([quote["known_at"] for quote in quotes]+[event["known_at"] for event in events],key=instant)
                if instant(maturity) > instant(at):
                    continue
                table = {quote["date"]:quote for quote in quotes}
                observed = [table[date] for date in dates]
                # A later identical capture confirms an already observed result.
                # Only a changed price or entitlement starts a new label vintage.
                economic_state = fingerprint({"quotes":[{key:quote[key] for key in ("date","nav","distribution_per_share")} for quote in quotes],
                    "cash":[{key:event.get(key) for key in ("kind","code","id","record_date","ex_date","pay_date","per_share","currency","distribution_mode","entitlement_rule")} for event in events]})
                if economic_state == previous_state:
                    continue
                future = [event for event in events if event["ex_date"] > origin]
                prior = [event for event in events if event["ex_date"] <= origin]
                wealth = [(float(quote["nav"])+math.fsum(float(event["per_share"]) for event in future if event["ex_date"] <= quote["date"]))/base for quote in observed]
                witness = {"code":code,"origin":origin,"base_nav":base,"base_date":scalar["base_date"],
                    "requested_phase_dates":requested,"observation_dates":dates,"actual_quote_dates":dates,
                    "nav_quotes":observed,"window_quotes":quotes,"window_end_date":last,"distributions":events,
                    "future_distributions":future,"prior_entitlements":prior,
                    "cash_scope":"new_economic_ex_dates_strictly_after_origin; earlier_or_same_day_ex_rights_belong_to_source_bound_account_snapshot",
                    "original_label_source":copy.deepcopy(scalar),"availability_basis":"original_source_vintages_independent_multi_date_window",
                    "mapping_scope":"current_calendar_phase_targets_to_next_observed_historical_quote_no_interpolation"}
                row = {"code":code,"decision_date":origin,"fund_group_id":sample["fund_group_id"],"x":sample["x"],
                    "wealth_ratios":wealth,"base_nav":base,"cash":future,
                    "label_available_date":instant(maturity).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                    "label_available_at":maturity,"label_source":witness,"source_hash":fingerprint(witness)}
                produced.append(row)
                previous_state = economic_state
            if not produced:
                unavailable.append({"code":code,"decision_date":origin,"reason":"No complete mature original source multi-date window"})
            else:
                for row in produced:
                    row["label_reason"] = None
                chosen = copy.deepcopy(produced[-1]);chosen["frame_versions"] = produced;frames.append(chosen)
        except (ValueError,KeyError,StopIteration) as error:
            unavailable.append({"code":code,"decision_date":origin,"reason":str(error)})
    return sorted(frames,key=lambda row:(row["decision_date"],row["code"])),unavailable


def _eligible(frames, codes, day, policy, ceiling=None):
    boundary = min(day, instant(ceiling).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()) if ceiling is not None else day
    lower = (dt.date.fromisoformat(day)-dt.timedelta(days=policy["train_window_days"])).isoformat()
    selected = []
    for row in frames:
        if row["code"] not in codes or not lower <= row["decision_date"] < day:
            continue
        versions = [version for version in row.get("frame_versions", [row])
                    if version["label_available_date"] < boundary]
        if versions:
            chosen = copy.deepcopy(max(versions, key=lambda value: (instant(value["label_available_at"]), value["source_hash"])))
            # Inner folds reselect among only the histories admitted by this boundary.
            chosen["frame_versions"] = copy.deepcopy(versions)
            selected.append(chosen)
    return selected


def _training_gap(frames,codes,day,policy):
    train = _eligible(frames,codes,day,policy)
    dates = sorted({row["decision_date"] for row in train})
    required = policy["min_train_dates"]+policy["cv_folds"]
    if len(dates) < required:
        return {"reason":"Insufficient mature multi-date training","mature_dates":len(dates),"required_dates":required}
    initial = max(policy["min_train_dates"],int(len(dates)*policy["cv_initial_train_fraction"]))
    cuts = [initial+(len(dates)-initial)*index//policy["cv_folds"] for index in range(policy["cv_folds"]+1)]
    for start,stop in zip(cuts[:-1],cuts[1:]):
        if start >= stop:
            return {"reason":"Empty path inner fold","mature_dates":len(dates)}
        mature = _eligible(train,codes,dates[start],policy)
        count = len({row["decision_date"] for row in mature})
        if count < policy["min_train_dates"]:
            return {"reason":"Insufficient purged path fold","fold_origin":dates[start],"mature_dates":count,"required_dates":policy["min_train_dates"]}
    return None


def _quality_frame_keys(frames, codes, origins, current_day, policy, current_ceiling):
    """Only actual prediction and outer-training keys; inner folds are subsets."""
    keys = {(origin, code) for origin in origins for code in codes}
    for origin in origins:
        keys.update((row["decision_date"], row["code"]) for row in _eligible(frames, codes, origin, policy))
    keys.update((row["decision_date"], row["code"])
                for row in _eligible(frames, codes, current_day, policy, current_ceiling))
    return keys


def _dictionary(rows):
    return sorted({_cash_key(event, row["decision_date"]) for row in rows for event in row["cash"]})


def _targets(row, cash_keys):
    values = [math.log(value) for value in row["wealth_ratios"]]
    measured = {}
    for event in row["cash"]:
        key = _cash_key(event, row["decision_date"])
        require(key in cash_keys, "Observed cash timing pattern is outside the frozen training dictionary")
        measured[key] = measured.get(key, 0.) + float(event["per_share"])/row["base_nav"]
    return values + [math.sqrt(measured.get(key, 0.)) for key in cash_keys]


def _fit_model(rows, cash_keys, groups, alpha, ratio, feature_names=None):
    from industry_model import FUND_FEATURE_NAMES, INPUT_SCHEMA_ID
    from fund_quality import QUALITY_FEATURE_NAMES
    width = len(rows[0]["x"])
    base_names = market.FEATURE_NAMES+FUND_FEATURE_NAMES
    canonical = base_names+QUALITY_FEATURE_NAMES
    if feature_names is None:
        require(width in (len(market.FEATURE_NAMES), len(base_names), len(canonical)), "Source path input width is obsolete or needs explicit registered covariate names")
        feature_names = market.FEATURE_NAMES if width == len(market.FEATURE_NAMES) else base_names if width == len(base_names) else canonical
    require(len(feature_names) == width and len(set(feature_names)) == width and all(name in canonical for name in feature_names)
            and all(len(row["x"]) == width for row in rows), "Registered source path covariate names differ from actual input dimensions")
    x = np.asarray([row["x"] + [float(row["fund_group_id"] == group) for group in groups] for row in rows])
    y = np.asarray([_targets(row, cash_keys) for row in rows])
    weights = statistics._weights(rows)
    scaler = StandardScaler().fit(x, sample_weight=weights)
    coefficients, intercept = np.zeros((y.shape[1], x.shape[1])), y[0].copy()
    active = [index for index in range(y.shape[1]) if not np.all(y[:, index] == y[0, index])]
    if active:
        estimator = ElasticNet(alpha=alpha, l1_ratio=ratio, max_iter=20000, tol=1e-8)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            estimator.fit(scaler.transform(x), y[:, active], sample_weight=weights)
        coefficients[active], intercept[active] = estimator.coef_, estimator.intercept_
    return {"coefficients": coefficients.tolist(), "intercept": intercept.tolist(),
            "scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
            "alpha": alpha, "l1_ratio": ratio, "cash_keys": [list(key) for key in cash_keys], "fund_groups": groups,
            "feature_names": list(feature_names)+["source_legal_fund_group:"+group for group in groups],
            "input_schema_id": INPUT_SCHEMA_ID,
            "target_weighting": "equal_mean_latent_MSE_declared_design_not_unique_financial_weights",
            "target_link": "log_total_NAV_plus_ex_date_cash_claims_and_squared_nonnegative_cash_per_source_timing_pattern"}


def _predict(model, row):
    from industry_model import INPUT_SCHEMA_ID
    require(model.get("input_schema_id") == INPUT_SCHEMA_ID, "Obsolete source path EN model requires retraining")
    require(row["fund_group_id"] in model["fund_groups"], "Fund legal identity is outside the frozen training group set")
    x = row["x"] + [float(row["fund_group_id"] == group) for group in model["fund_groups"]]
    return statistics.predict(model, x)


def _fit_at(frames, codes, day, policy, cash_keys, groups, ceiling=None, feature_names=None):
    train = _eligible(frames, codes, day, policy, ceiling)
    dates = sorted({row["decision_date"] for row in train})
    require(len(dates) >= policy["min_train_dates"]+policy["cv_folds"], "Insufficient mature multi-date training")
    initial = max(policy["min_train_dates"], int(len(dates)*policy["cv_initial_train_fraction"]))
    cuts = [initial+(len(dates)-initial)*index//policy["cv_folds"] for index in range(policy["cv_folds"]+1)]
    trials = []
    for alpha in sorted(set(policy["alpha_grid"])):
        for ratio in sorted(set(policy["l1_ratio_grid"])):
            losses, folds = [], []
            for start, stop in zip(cuts[:-1], cuts[1:]):
                require(start < stop, "Empty path inner fold")
                validation_dates = set(dates[start:stop])
                validation = [row for row in train if row["decision_date"] in validation_dates]
                mature = _eligible(train, codes, dates[start], policy)
                require(len({row["decision_date"] for row in mature}) >= policy["min_train_dates"], "Insufficient purged path fold")
                model = _fit_model(mature, cash_keys, groups, alpha, ratio, feature_names)
                loss = math.fsum(math.fsum((actual-predicted)**2 for actual, predicted in
                    zip(_targets(row, cash_keys), _predict(model, row)))/len(model["intercept"]) for row in validation)/len(validation)
                losses.append(loss)
                folds.append({"origin": dates[start], "training_hash": fingerprint(mature),
                              "maximum_label_available": max(row["label_available_date"] for row in mature), "loss": loss})
            trials.append({"alpha": alpha, "ratio": ratio, "loss": math.fsum(losses)/len(losses), "folds": folds})
    selected = min(trials, key=lambda trial: (trial["loss"], trial["alpha"], trial["ratio"]))
    model = _fit_model(train, cash_keys, groups, selected["alpha"], selected["ratio"], feature_names)
    audit = {"origin": day, "training_hash": fingerprint(train), "training_rows": len(train),
             "training_origin_dates": dates, "training_source_keys": [[row["code"], row["decision_date"], row["source_hash"]] for row in train],
             "maximum_label_available": max(row["label_available_date"] for row in train),
             "training_ceiling_at": ceiling, "trials": trials, "selected_alpha": selected["alpha"],
             "selected_ratio": selected["ratio"], "dictionary_frozen": True}
    return model, audit


def _decode(latents, marks, contract, origin, witnesses, path_id, probability, label_available, constraints=()):
    require(contract.get("price_schema") == "source_role_price_paths_v3", "Obsolete derived price-coordinate contract")
    count = len(contract["nav_dates"])
    base = dt.date.fromisoformat(contract["origin_date"])
    nav = {}
    events = []
    for code in contract["codes"]:
        values, cash_keys = latents[code]["values"], latents[code]["cash_keys"]
        for column, key in enumerate(cash_keys):
            amount = float(marks[code])*values[count+column]**2
            if amount == 0:
                continue
            events.append({"code": code, "id": fingerprint([path_id, code, list(key)]),
                "record_date": (base+dt.timedelta(days=key[0])).isoformat(),
                "ex_date": (base+dt.timedelta(days=key[1])).isoformat(),
                "pay_date": (base+dt.timedelta(days=key[2])).isoformat(), "per_share": amount,
                "currency": key[3], "distribution_mode": key[4], "entitlement_rule": json.loads(key[5]),
                "evidence_ref": {"path_training_witnesses_hash": fingerprint(witnesses)}, "scenario_only": True})
    for event in constraints:
        if event.get("kind") != "cash_distribution":
            continue
        require(all(event.get(key) is not None for key in ("code", "record_date", "ex_date", "pay_date", "per_share", "evidence_ref")),
                "Known distribution constraint lacks complete source dates/rights")
        key = _cash_key(event, contract["origin_date"])
        require(key in latents[event["code"]]["cash_keys"], "Known current distribution is outside trained path timing support")
        events = [row for row in events if not (row["code"] == event["code"] and
                  (row["record_date"], row["ex_date"], row["pay_date"]) == (event["record_date"], event["ex_date"], event["pay_date"]))]
        events.append({**event, "id": event.get("id", fingerprint(event)), "scenario_only": False,
                       "source_known_constraint": True})
    for column, date in enumerate(contract["nav_dates"]):
        nav[date] = {}
        for code in contract["codes"]:
            wealth = float(marks[code])*math.exp(latents[code]["values"][column])
            cash = math.fsum(float(row["per_share"]) for row in events if row["code"] == code and row["ex_date"] <= date)
            value = wealth-cash
            known = contract["known_marks"][code]
            if known["nav_date"] == date and instant(known["known_at"]) <= instant(contract["decision_at"]):
                value = float(known["value"])
            require(math.isfinite(value) and value > 0, "Joint path is outside positive NAV after cash-rights reconstruction")
            nav[date][code] = value
    return {"id": path_id, "origin_at": origin, "probability": probability, "nav": nav,
            "price_schema": contract["price_schema"], "known_marks": copy.deepcopy(contract["known_marks"]),
            "distributions": events, "label_available_at": label_available,
            "source_witnesses": witnesses, "scope": "model_path_hypothesis_not_future_source_prices_or_announcements"}


def _partial(context, data, reason, actions, contract=None):
    return {"schema_version": 4, "status": "partial", "trade_ready": False,
        "stage_dates": contract["stage_dates"] if contract else [], "nav_dates": contract["nav_dates"] if contract else [],
        "codes": context["allocation_codes"], "point_paths": [], "selection_paths": [], "calibration_paths": [],
        "prefix_groups": {}, "known_action_constraints": context.get("known_future_actions", []),
        "source_hash": fingerprint(data), "model_hash": None, "reason": reason, "required_actions": actions,
        "scope": "no_multi_date_amount_recommendation_without_source_qualified_joint_path_evidence"}


def build_paths(context, data, samples, fitting, *, max_stages=8):
    contract = None
    unavailable,skipped = [],[]
    try:
        contract = stage_contract(context, max_stages)
        codes, day = contract["codes"], context["as_of"]
        frames, unavailable = _frames(context, data, samples, contract)
        policy = context["model_request"]["training_policy"]
        joint = fitting["joint_scenarios"]
        selection_origins = [record["date"] for record in joint["source_selection_oos"]]
        calibration_origins = [record["date"] for record in joint["source_calibration_oos"]]
        require(len(selection_origins) >= 2 and len(calibration_origins) >= 2,
                "At least two distinct joint selection and calibration origins required")
        table = {(row["decision_date"],row["code"]):row for row in _eligible(frames,codes,day,policy)}
        skipped = []
        def qualified(origins,partition):
            eligible = []
            for origin in origins:
                missing = [code for code in codes if (origin,code) not in table]
                gap = {"reason":"Incomplete multi-fund/date source path panel","missing_codes":missing} if missing else _training_gap(frames,codes,origin,policy)
                if gap is not None: skipped.append({"origin":origin,"partition":partition,**gap})
                else: eligible.append(origin)
            return eligible
        selection_origins = qualified(selection_origins,"selection")
        calibration_origins = qualified(calibration_origins,"calibration")
        require(len(selection_origins) >= 2,"At least two source-qualified forward path selection origins required")
        last_selection_maturity = max(instant(table[(origin,code)]["label_available_at"]) for origin in selection_origins for code in codes)
        retained = []
        for origin in calibration_origins:
            origin_at = origin+"T"+joint["clock_context"]["order_time_local"]+"+08:00"
            if instant(origin_at) <= last_selection_maturity:
                skipped.append({"origin":origin,"partition":"calibration","reason":"Independent full-window path maturity embargo","selection_label_available_at":last_selection_maturity.isoformat()})
            else: retained.append(origin)
        calibration_origins = retained
        require(len(calibration_origins) >= 2,"At least two independent full-window path calibration origins required")
        first = min(selection_origins)
        initial = _eligible(frames,codes,first,policy)
        require(initial,"No initial independent source training segment")
        cash_keys = _dictionary(initial)
        groups = sorted({row["fund_group_id"] for row in initial})
        from industry_model import FUND_FEATURE_NAMES
        from fund_quality import QUALITY_FEATURE_NAMES
        off_names = market.FEATURE_NAMES + FUND_FEATURE_NAMES
        on_names = off_names + QUALITY_FEATURE_NAMES
        quality_bundle = data.get("quality_source_bundle")
        quality_frames, quality_gaps = [], []
        try:
            current_quality_rows = [row for row in samples if row["code"] in codes and row["decision_date"] == day]
            require({row["code"] for row in current_quality_rows} == set(codes), "Current quality feature universe is incomplete")
            for row in current_quality_rows:
                _quality_features(row, quality_bundle, context["decision_at"])
            quality_keys = _quality_frame_keys(frames, codes, selection_origins+calibration_origins, day, policy,
                calibration_origins[0]+"T"+joint["clock_context"]["order_time_local"]+"+08:00")
            for frame in frames:
                if (frame["decision_date"], frame["code"]) not in quality_keys:
                    continue
                origin_at = frame["decision_date"]+"T"+joint["clock_context"]["order_time_local"]+"+08:00"
                quality_frames.append(_quality_features(frame, quality_bundle, origin_at))
        except (ValueError, KeyError) as error:
            quality_frames = []
            quality_gaps.append({"action": "complete_original_source_quality_training_panel", "reason": str(error)})
        marks = {code: float(context["market_ref"]["prices"][code]) for code in codes}
        def decoded(record, values, identity):
            return _decode({code: {"values": values[code], "cash_keys": cash_keys} for code in codes},
                marks, contract, record["origin_at"], record["source_witnesses"], identity, 1., record["label_available_at"])
        selection_by_arm = {"off": [], "on": []}
        on_records = []
        def make_record(source_frames, origin, names):
            model, audit = _fit_at(source_frames, codes, origin, policy, cash_keys, groups, feature_names=names)
            source_table = {(row["decision_date"], row["code"]): row for row in _eligible(source_frames, codes, day, policy)}
            selected_rows = [source_table[(origin, code)] for code in codes]
            actual = {row["code"]: _targets(row, cash_keys) for row in selected_rows}
            predicted = {row["code"]: _predict(model, row) for row in selected_rows}
            return {"date": origin, "origin_at": origin+"T"+joint["clock_context"]["order_time_local"]+"+08:00",
                "label_available_at": max((row["label_available_at"] for row in selected_rows), key=instant),
                "predicted": predicted, "actual": actual, "errors": {code: [a-p for a,p in zip(actual[code], predicted[code])] for code in codes},
                "source_witnesses": [row["label_source"] for row in selected_rows], "model": model, "model_training_audit": audit}
        records = []
        for origin in selection_origins:
            records.append(make_record(frames, origin, off_names))
            if quality_frames:
                try:
                    on_records.append(make_record(quality_frames, origin, on_names))
                except (ValueError, KeyError, ConvergenceWarning) as error:
                    quality_frames, on_records = [], []
                    quality_gaps.append({"action": "complete_mature_quality_on_inner_folds", "reason": str(error)})
        for arm, arm_records in (("off", records), ("on", on_records)):
            for record in arm_records:
                selection_by_arm[arm].append({"origin_at": record["origin_at"], "label_available_at": record["label_available_at"],
                    "source_hashes": [fingerprint(source) for source in record["source_witnesses"]],
                    "predicted": decoded(record, record["predicted"], "quality-predicted-"+record["date"]),
                    "realized": decoded(record, record["actual"], "quality-realized-"+record["date"]),
                    "model_training_audit": record["model_training_audit"]})
        from quality_selection import select_quality_arm
        quality_selection = select_quality_arm(context, contract, selection_by_arm, source_hash=fingerprint(data),
            feature_names_by_arm={"off": off_names, "on": on_names}, on_source_gaps=quality_gaps)
        chosen_arm = quality_selection["selected_arm"]
        chosen_names = on_names if chosen_arm == "on" else off_names
        chosen_frames = quality_frames if chosen_arm == "on" else frames
        records = on_records if chosen_arm == "on" else records
        # The feature block is frozen before any independent calibration outcomes are read.
        for origin in calibration_origins:
            records.append(make_record(chosen_frames, origin, chosen_names))
        split = len(selection_origins)
        selection, calibration = records[:split], records[split:]
        require(max(instant(row["label_available_at"]) for row in selection) < min(instant(row["origin_at"]) for row in calibration),
                "Path selection/calibration embargo differs")
        ceiling = calibration[0]["origin_at"]
        model, audit = _fit_at(chosen_frames, codes, day, policy, cash_keys, groups, ceiling, chosen_names)
        source_industry = fitting["industry_forecast"]
        require(instant(source_industry["training_ceiling_at"]) <= instant(ceiling),
                "Current industry feature model crosses path calibration ceiling")
        current_raw = [copy.deepcopy(row) for row in samples if row["code"] in codes and row["decision_date"] == day]
        for row in current_raw:
            row["x"] = row["x"][:len(market.FEATURE_NAMES)]
        rebuilt = market.attach_industry_features(current_raw, source_industry, data["industry_exposures"],
                                                   joint["clock_context"]["order_time_local"])
        require(all(row["industry_ready"] for row in rebuilt), "Frozen current industry inputs are unavailable")
        current = {row["code"]: row for row in rebuilt}
        if chosen_arm == "on":
            current = {code: _quality_features(row, quality_bundle, context["decision_at"]) for code, row in current.items()}
        require(set(current) == set(codes), "Current fund source inputs are incomplete")
        mu = {code: _predict(model, current[code]) for code in codes}
        marks = {code: float(context["market_ref"]["prices"][code]) for code in codes}
        base_nav = {code: float(current[code].get("feature_source", {}).get("base_nav", next(row["nav"] for row in data["nav"][code]
                    if row["date"] == current[code]["feature_cutoff_date"]))) for code in codes}
        scales = {code: base_nav[code]/marks[code] for code in codes}
        count = len(contract["nav_dates"])
        mu = {code: [value+math.log(scales[code]) if column < count else value*math.sqrt(scales[code])
                     for column, value in enumerate(values)] for code, values in mu.items()}
        constraints = context.get("known_future_actions", [])
        model_hash = fingerprint({"model": model, "audit": audit, "dictionary": [list(key) for key in cash_keys]})
        def decode(values, record, identity, probability=1., use_known=True):
            latent = {code: {"values": values[code], "cash_keys": cash_keys} for code in codes}
            return _decode(latent, marks, contract, record["origin_at"], record.get("source_witnesses", []), identity,
                probability, record.get("label_available_at"), constraints if use_known else ())
        point = decode(mu, {"origin_at": context["decision_at"], "source_witnesses": [{"model_hash": model_hash}]}, "current-point")
        selection_paths = []
        for record in selection:
            values = {code: [mean+(error if column < count else error*math.sqrt(scales[code]))
                             for column, (mean, error) in enumerate(zip(mu[code], record["errors"][code]))] for code in codes}
            selection_paths.append(decode(values, record, "selection-"+record["date"], 1./len(selection)))
        calibration_paths = [{"origin_at": row["origin_at"], "label_available_at": row["label_available_at"],
            "predicted": decode(row["predicted"], row, "calibration-predicted-"+row["date"], use_known=False),
            "realized": decode(row["actual"], row, "calibration-realized-"+row["date"], use_known=False),
            "source_hashes": [fingerprint(source) for source in row["source_witnesses"]],
            "model_training_audit": row["model_training_audit"], "original_model": row["model"]} for row in calibration]
        result = {"schema_version": 4, "schema": "source_role_price_paths_v3", "known_marks": copy.deepcopy(contract["known_marks"]),
            "status": "research_ready", "trade_ready": True,
            "stage_dates": contract["stage_dates"], "nav_dates": contract["nav_dates"], "codes": codes,
            "point_paths": [point], "selection_paths": selection_paths, "calibration_paths": calibration_paths,
            "prefix_groups": {date: [[path["id"] for path in selection_paths]] for date in contract["stage_dates"]},
            "known_action_constraints": constraints, "source_hash": fingerprint(data), "model_hash": model_hash,
            "model": model, "model_training_audit": audit, "cash_dictionary": [list(key) for key in cash_keys],
            "dictionary_training_hash": fingerprint(initial), "path_source_records": records, "source_contract": contract,
            "current_rebase": {code: {"feature_NAV": base_nav[code], "feature_date": current[code]["feature_cutoff_date"],
                                      "current_mark": marks[code], "scale": scales[code]} for code in codes},
            "current_frozen_industry_hash": source_industry["forecast_hash"],
            "quality_feature_selection": quality_selection, "selected_quality_arm": chosen_arm,
            "unavailable_source_frames": unavailable, "skipped_source_origins":skipped, "required_actions": [],
            "scope": {"estimator": "Elastic_Net_multi_date_source_targets", "policy_information": "frozen_lagged_prefix_feedback_not_future_conditional_EN",
                "risk_information": "all_leaf_coarse_terminal_CVaR_not_full_dynamic_conditional_risk",
                "historical_evidence": "independent_original_multi_date_window_current_terms_hypothesis_not_historical_actual_fills",
                "historical_quote_mapping":"requested_calendar_phases_and_actual_source_quote_dates_kept_separate",
                "cash_model": "empirical_conditional_cash_estimate_not_guaranteed_future_zero",
                "unmodeled_cash_timing": "new_observed_pattern_refuses_qualification",
                "risk_inference": "declared_empirical_paths_not_full_information_dynamic_distribution_guarantee"}}
        result["path_hash"] = fingerprint(result)
        return result
    except (ValueError, KeyError, StopIteration, OverflowError, ConvergenceWarning) as error:
        value = _partial(context, data, str(error), [{"action": "complete_source_PIT_multi_date_NAV_cash_and_forward_joint_calibration",
                                                   "reason": str(error)}], contract)
        value.update(unavailable_source_frames=unavailable,skipped_source_origins=skipped)
        value["path_hash"] = fingerprint({key:item for key,item in value.items() if key!="path_hash"})
        return value


def validate_paths(value, inputs):
    expected = build_paths(inputs["context"], inputs["data"], inputs["samples"], inputs["fitting"],
                           max_stages=inputs.get("max_stages", 8))
    require(value == expected, "Multi-date paths differ from source/PIT/EN reconstruction")
    if value["status"] == "research_ready":
        require(all(len(group) >= 2 for groups in value["prefix_groups"].values() for group in groups),
                "A unique prefix may identify an unobserved future leaf")
        for row in value["calibration_paths"]:
            require(row["model_training_audit"]["maximum_label_available"] < row["origin_at"][:10],
                    "Calibration path fit uses a nonmature outcome")
        require(value["model_training_audit"]["maximum_label_available"] <
                value["model_training_audit"]["training_ceiling_at"][:10], "Current path model crosses calibration ceiling")
    return {"status": "passed", "scope": "source_PIT_multi_date_NAV_cash_EN_and_nonanticipative_information_contract",
            "checks": ["sealed_source_path_reconstruction", "mature_forward_labels_and_frozen_calibration_boundary",
                       "nonanticipative_shared_information_groups", "unknown_source_patterns_fail_closed"],
            "path_hash": value.get("path_hash"), "model_hash": value["model_hash"], "result_status": value["status"]}


def information_groups(paths, dates):
    """Coarse terminal-risk partition, separate from lawful policy observations."""
    require(dates == sorted(set(dates)) and paths, "Complete local information grid required")
    return {date: [[path["id"] for path in paths]] for date in dates}


def _quality_features(row, bundle, origin_at):
    """Only an original-source panel row known at the prediction origin."""
    require(bundle and bundle.get("quality_bundle_hash") == fingerprint({key: value for key, value in bundle.items()
            if key != "quality_bundle_hash"}), "Source quality panel binding changed")
    candidates = [value for value in bundle["feature_panel"] if value["code"] == row["code"]
                  and instant(value["decision_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date() == instant(origin_at).astimezone(ZoneInfo("Asia/Shanghai")).date()
                  and instant(value["decision_at"]) <= instant(origin_at) and instant(value["source_known_at"]) <= instant(origin_at)]
    require(candidates, "No source-quality feature vintage for this origin's current team")
    selected = max(candidates, key=lambda value: instant(value["decision_at"]))
    require(selected["feature_hash"] == fingerprint({key: value for key, value in selected.items() if key != "feature_hash"})
            and len(selected["values"]) == len(bundle["feature_names"]), "Source quality feature changed")
    value = copy.deepcopy(row)
    value["x"] = row["x"] + list(selected["values"])
    value["quality_feature_hash"] = selected["feature_hash"]
    for version in value.get("frame_versions", []):
        version["x"] = list(version["x"]) + list(selected["values"])
        version["quality_feature_hash"] = selected["feature_hash"]
    return value
