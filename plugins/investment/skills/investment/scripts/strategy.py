"""Strict frozen strategies and system-derived cutoff contexts; no persistence."""
import copy
import datetime as dt
import math
import re
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

import allocation_market as market
from contracts import fields, fingerprint, instant, decimal_string, require

SPEC_FIELDS = {"schema_version", "kind", "currency", "universe_policy", "model", "training", "features",
               "availability", "timing", "allocation", "execution", "benchmark", "news", "intervention", "planning", "constraints", "trade_policy", "industry", "decision"}


def _integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, label + " must be an integer in range")


def _finite(value, label, low=0, high=None):
    require(type(value) in (int, float) and math.isfinite(value) and value >= low
            and (high is None or value <= high), "Invalid " + label)


def validate_spec(spec):
    require(type(spec) is dict and "trade_policy" in spec, "new_spec_required: declare rolling source-path trade policy")
    fields(spec, SPEC_FIELDS, label="StrategySpec")
    require(spec["schema_version"] == 4 and spec["currency"] == "CNY", "Only strategy schema4/CNY is accepted")
    require(spec["kind"] in ("numeric_policy", "assisted_workflow"), "Unknown strategy kind")
    fields(spec["universe_policy"], {"market", "platform", "currency", "supported_instruments", "comparison_groups", "require_complete_group"}, label="frozen selection rule")
    selection = spec["universe_policy"]
    require(selection["market"] == "CN_public_funds" and selection["platform"] == "TT" and selection["currency"] == spec["currency"]
            and selection["supported_instruments"] == ["off_exchange_nav"]
            and selection["comparison_groups"] == ["same_benchmark", "same_legal_fund"]
            and selection["require_complete_group"] is True, "Unsupported or incomplete frozen selection policy")
    fields(spec["model"], {"algorithm", "version", "provider_model", "prompt_hash"}, label="model")
    require(spec["model"]["algorithm"] == "news_industry_elastic_net_rolling_cvar" and spec["model"]["version"] == "0.0.1",
            "Unsupported numerical implementation")
    decision = spec["decision"]
    fields(decision, {"method", "max_qualification_families", "max_path_stages", "max_current_actions", "max_policy_count", "future_cash_fractions"}, label="rolling computation and policy scope")
    require(decision["method"] == "source_paths_nonanticipative_receding_horizon", "Unsupported rolling decision method")
    for key in ("max_qualification_families", "max_current_actions", "max_policy_count"):
        _integer(decision[key], key, 1)
    _integer(decision["max_path_stages"], "max_path_stages", 2)
    fractions = decision["future_cash_fractions"]
    require(type(fractions) is list and fractions and fractions == sorted(set(fractions))
            and all(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 1 for value in fractions),
            "Declared candidate cash fraction lattice required; not preset portfolio weights")
    import industry_model
    import asset_domains
    industry = spec["industry"]
    fields(industry, {"pipeline_id", "feature_names", "fund_feature_names", "pricing_phase_definition", "missing", "training"}, label="industry forecast contract")
    require(industry["pipeline_id"] == asset_domains.INPUT_SCHEMA_ID
            and industry["feature_names"] == industry_model.FEATURE_NAMES
            and industry["fund_feature_names"] == industry_model.FUND_FEATURE_NAMES
            and industry["pricing_phase_definition"] == industry_model.PHASE
            and industry["missing"] == "partial_no_trade", "Unsupported industry bridge contract")
    require(industry["training"] == {**spec["training"], "feature_names": industry_model.FEATURE_NAMES},
            "Industry training must use the frozen chronological policy and its own feature names")
    fields(spec["features"], {"names", "pipeline_id", "missing"}, label="features")
    require(spec["features"] == {"names": market.FEATURE_NAMES, "pipeline_id": "calendar-nav-features-v2", "missing": "reject"},
            "Feature implementation or order differs")
    training = spec["training"]
    fields(training, {"train_window_days", "min_train_dates", "cv_folds", "alpha_grid", "l1_ratio_grid",
                      "min_joint_dates", "cv_initial_train_fraction", "feature_names"}, label="training")
    require(training["feature_names"] == market.FEATURE_NAMES, "Training features differ")
    for key in ("train_window_days", "min_train_dates", "cv_folds", "min_joint_dates"):
        _integer(training[key], key, 1)
    _finite(training["cv_initial_train_fraction"], "initial training fraction", 0, 1)
    require(0 < training["cv_initial_train_fraction"] < 1, "Initial training fraction must be interior")
    for key in ("alpha_grid", "l1_ratio_grid"):
        require(type(training[key]) is list and training[key], "Nonempty parameter grid required")
        for value in training[key]:
            _finite(value, key, 0, 1 if key == "l1_ratio_grid" else None)
            require(value > 0, "Parameter grid values must be positive")
    fields(spec["availability"], {"nav_lag_calendar_days", "max_feature_age_days", "max_market_age_seconds",
                                  "max_terms_age_seconds", "terms_rule"}, label="availability")
    for key in ("nav_lag_calendar_days", "max_feature_age_days", "max_market_age_seconds", "max_terms_age_seconds"):
        _integer(spec["availability"][key], key)
    require(spec["availability"]["terms_rule"] == "observed_before_cutoff", "Unsupported terms rule")
    fields(spec["timing"], {"timezone", "as_of", "model_refit"}, label="timing")
    require(spec["timing"] == {"timezone": "Asia/Shanghai", "as_of": "decision_local_date", "model_refit": "each_decision"},
            "Unsupported decision clock or refit rule")
    policy = spec["allocation"]
    fields(policy, {"tail_probability", "funding_levels", "minimum_advantage"}, label="allocation")
    fields(spec["planning"], {"primary_horizon_days", "primary_goal", "cash_deadline_days", "cash_required_amount"}, label="internal evaluation and cash goals")
    _integer(spec["planning"]["primary_horizon_days"], "primary horizon", 1)
    require(spec["planning"]["primary_goal"] in ("hold", "redeem"), "Explicit primary terminal goal required")
    deadline = spec["planning"]["cash_deadline_days"]
    require(deadline is None or type(deadline) is int and deadline >= spec["planning"]["primary_horizon_days"], "Cash deadline must follow the primary horizon")
    required_cash = spec["planning"]["cash_required_amount"]
    require((deadline is None) == (required_cash is None), "Cash deadline and required amount must be specified together")
    if required_cash is not None:
        decimal_string(required_cash, "required cash", positive=True)
    fields(spec["constraints"], {"fund_group_limits", "sector_limits"}, label="exposure limits")
    for key in ("fund_group_limits", "sector_limits"):
        require(type(spec["constraints"][key]) is dict, "Explicit limits or empty unconstrained map required")
        for identity, limit in spec["constraints"][key].items():
            require(type(identity) is str and bool(identity), "Exposure identity required")
            _finite(limit, "exposure limit", 0, 1)
    _finite(policy["tail_probability"], "tail probability", 0, 1)
    require(0 < policy["tail_probability"] < 1, "Tail probability must be interior")
    _finite(policy["minimum_advantage"], "minimum_advantage")
    require(type(policy["funding_levels"]) is list and policy["funding_levels"], "Funding levels required")
    levels = [Decimal(decimal_string(value, "funding level")) for value in policy["funding_levels"]]
    require(min(levels) == 0 and len(set(levels)) == len(levels), "Distinct nonnegative funding levels must include zero")
    fields(spec["execution"], {"money_step", "share_step", "fee_rounding", "waiting_cash", "fill_policy"}, label="execution")
    require(spec["execution"]["money_step"] == "0.01" and spec["execution"]["fee_rounding"] == "half_up"
            and spec["execution"]["waiting_cash"] == "next_decision"
            and spec["execution"]["fill_policy"] == "confirmed_events", "Unsupported execution convention")
    decimal_string(spec["execution"]["share_step"], "share_step", positive=True)
    fields(spec["benchmark"], {"rule", "weights", "cash_weight", "rebalance_dates"}, label="benchmark")
    require(spec["benchmark"]["rule"] == "fixed_weights", "Only the fixed_weights benchmark is accepted")
    weights = spec["benchmark"]["weights"]
    require(type(weights) is dict and weights and all(type(code) is str and code for code in weights), "Independent fixed benchmark codes required")
    values = [Decimal(decimal_string(value, "benchmark weight")) for value in weights.values()]
    cash_weight = Decimal(decimal_string(spec["benchmark"]["cash_weight"], "benchmark cash weight"))
    require(all(0 < value <= 1 for value in values) and 0 <= cash_weight < 1
            and sum(values) + cash_weight == 1, "Benchmark weights including cash must sum exactly to one")
    dates = spec["benchmark"]["rebalance_dates"]
    require(type(dates) is list and all(type(value) is str for value in dates)
            and dates == sorted(set(dates)), "Benchmark rebalance dates must be ordered and unique")
    for value in dates:
        require(dt.date.fromisoformat(value).isoformat() == value, "Invalid benchmark rebalance date")
    fields(spec["news"], {"mode", "sources", "max_age_seconds", "required_source_groups"}, label="news")
    require(spec["news"]["mode"] in ("none", "sealed_inputs") and type(spec["news"]["sources"]) is list,
            "Invalid news policy")
    _integer(spec["news"]["max_age_seconds"], "news max age")
    require(type(spec["news"]["required_source_groups"]) is dict, "Required news source groups must be explicit")
    for group, members in spec["news"]["required_source_groups"].items():
        require(type(group) is str and group and type(members) is list and members
                and len(members) == len(set(members)) and set(members) <= set(spec["news"]["sources"]), "Invalid required news source group")
    fields(spec["intervention"], {"mode", "max_turnover_fraction"}, label="intervention")
    _finite(spec["intervention"]["max_turnover_fraction"], "max turnover fraction", 0, 2)
    if spec["kind"] == "numeric_policy":
        require(spec["intervention"]["mode"] == "forbidden"
                and spec["model"]["provider_model"] is None and spec["model"]["prompt_hash"] is None,
                "Numeric policies cannot admit external decision inputs")
    else:
        require(spec["intervention"]["mode"] == "bounded_override" and spec["news"]["mode"] == "sealed_inputs",
                "Assisted workflow must declare its intervention contract")
        import source_fetch
        news_sources = spec["news"]["sources"]
        require(news_sources and all(type(s) is str for s in news_sources) and len(set(news_sources)) == len(news_sources),
                "Assisted workflow requires distinct registered news source IDs")
        registry = source_fetch.load_registry()
        require(all(s in registry["sources"] and registry["sources"][s]["purpose"] == "news" for s in news_sources),
                "Assisted workflow contains an unknown or non-news source")
        require(type(spec["model"]["provider_model"]) is str and spec["model"]["provider_model"], "Pinned provider/model identity required")
        require(type(spec["model"]["prompt_hash"]) is str and re.fullmatch(r"[a-f0-9]{64}", spec["model"]["prompt_hash"]),
                "Pinned prompt digest required")
    from newtrade_guard import validate_policy
    validate_policy(spec["trade_policy"])
    return copy.deepcopy(spec)


def build_context(spec, state, decision_at, market_ref, *, risk_state, purchase_eligible_codes, account_id="main", sealed_intervention=None, news_review_hash=None, dynamic_universe=None, candidate_identities=None, known_future_actions=None, action_inventory_scope="known_subset", trade_state=None, trade_family_review_index=None, industry_data_ref=None, sector_exposure_bounds=None, readiness_ref=None, family_id=None, paired_currency_validation=None):
    import ledger
    spec = validate_spec(spec)
    require(trade_state is not None and trade_family_review_index is not None,
            "authoritative_trade_inputs_required: confirmed history and registered review index")
    codes = sorted(market_ref["prices"]) if dynamic_universe is None else list(dynamic_universe)
    require(codes and len(set(codes)) == len(codes) and all(type(code) is str and code for code in codes), "Dynamic decision universe required")
    require(type(purchase_eligible_codes) is list and all(type(code) is str for code in purchase_eligible_codes)
            and len(set(purchase_eligible_codes)) == len(purchase_eligible_codes)
            and set(purchase_eligible_codes) <= set(codes), "Explicit purchase eligibility must be a unique universe subset")
    require(type(account_id) is str and account_id, "Account ID required")
    require(state.get("projection_cutoff") is None, "Historical observation projection cannot drive a current decision")
    cutoff = instant(decision_at)
    fields(market_ref, {"schema_version", "currency", "observed_at", "price_dates", "currencies", "prices", "terms", "terms_basis", "source_hashes", "known_marks"}, {"market_id", "identities"}, "market reference")
    require(market_ref["schema_version"] == 4 and market_ref["currency"] == spec["currency"], "Market currency or schema differs")
    require(market_ref["terms_basis"] == "source_verified", "Source-verified field-level dealing contracts required")
    observed = instant(market_ref["observed_at"])
    require(0 <= (cutoff-observed).total_seconds() <= spec["availability"]["max_market_age_seconds"], "Market snapshot is future or stale")
    require(type(market_ref["source_hashes"]) is dict and market_ref["source_hashes"]
            and all(type(v) is str and re.fullmatch(r"[a-f0-9]{64}", v) for v in market_ref["source_hashes"].values()),
            "Bound source hashes required; source authenticity is checked by the controller")
    require(set(market_ref["prices"]) == set(codes) == set(market_ref["price_dates"]) == set(market_ref["currencies"]), "Each dynamic asset requires its mark/date/currency")
    from zoneinfo import ZoneInfo
    local_day = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
    require(set(market_ref["known_marks"]) == set(codes), "Every current valuation needs its original known mark")
    for code in codes:
        mark = market_ref["known_marks"][code]
        fields(mark, {"nav_date", "value", "known_at", "source_ref"}, label="known valuation mark")
        require(mark["nav_date"] == market_ref["price_dates"][code]
                and ledger.amount(mark["value"]) == ledger.amount(market_ref["prices"][code])
                and instant(mark["known_at"]) <= cutoff and isinstance(mark["source_ref"], dict) and mark["source_ref"],
                "Current valuation differs from original known NAV")
        marked = dt.date.fromisoformat(market_ref["price_dates"][code])
        require(market_ref["currencies"][code] == spec["currency"] and marked <= local_day, "Foreign-currency or future asset mark")
        require((local_day - marked).days <= spec["availability"]["max_feature_age_days"], "Asset valuation mark is stale")
    snapshot = ledger.snapshot(state, decision_at, market_ref["prices"], market_ref["price_dates"])
    allocation_codes = sorted({lot["code"] for lot in snapshot["positions"]} | set(purchase_eligible_codes))
    required_terms_codes = set(allocation_codes) | {row["code"] for row in snapshot["open_orders"]}
    fields(risk_state, {"profile_hash", "capital_baseline", "net_principal", "loss_tolerance", "principal_floor",
                        "remaining_loss_budget", "currency", "as_of", "valid_until"}, label="resolved risk state")
    require(type(risk_state["profile_hash"]) is str and re.fullmatch(r"[a-f0-9]{64}", risk_state["profile_hash"]),
            "Risk profile digest required")
    require(risk_state["currency"] == spec["currency"] and instant(risk_state["as_of"]) <= cutoff,
            "Risk currency differs or risk state is future")
    require(risk_state["valid_until"] is None or cutoff < instant(risk_state["valid_until"]), "Risk profile expired")
    principal = ledger.amount(risk_state["net_principal"], "net principal", 0)
    require(principal > 0, "Nonpositive net principal requires a new explicit baseline")
    tolerance = Decimal(decimal_string(risk_state["loss_tolerance"], "loss tolerance"))
    require(0 <= tolerance < 1, "Principal loss tolerance must be in [0, 1)")
    with localcontext() as precision:
        precision.prec = 50
        floor = principal*(1-tolerance)
    require(ledger.amount(risk_state["principal_floor"], "principal floor") == floor,
            "Resolved principal floor differs from the profile")
    resolved_risk = copy.deepcopy(risk_state)
    if snapshot["equity"] is not None:
        equity = Decimal(snapshot["equity"])
        with localcontext() as precision:
            precision.prec = 50
            remaining = equity-floor
        require(ledger.amount(risk_state["remaining_loss_budget"], "remaining loss budget") == remaining,
                "Resolved risk state differs from current marked equity")
        resolved_risk.update(equity=snapshot["equity"], principal_floor=ledger.decimal(floor),
                             remaining_loss_budget=ledger.decimal(remaining),
                             status="risk_budget_breached" if remaining < 0 else "within_principal_floor")
    else:
        resolved_risk.update(equity=None, remaining_loss_budget=None, status="unreconciled_equity")
    terms, term_gaps, stale_codes, seen_terms = {}, {}, [], set()
    for row in market_ref["terms"]:
        fields(row, {"code", "subscription_fee", "redemption_fee", "min_buy", "max_weight", "settlement_days",
                     "buyable", "sellable", "observed_at", "fee_contract", "fee_contract_hash", "fee_contract_ref"},
                    {"max_buy", "redemption_schedule", "confirmed_execution_max_calendar_days", "execution_bound_source", "investor_scope"}, "asset terms")
        require(row["code"] not in seen_terms, "Duplicate asset terms")
        seen_terms.add(row["code"])
        require(fingerprint(row["fee_contract"]) == row["fee_contract_hash"], "Dealing contract projection changed")
        subject_scope = row["fee_contract"].get("subject", {}).get("investor_type")
        require("investor_scope" not in row or row["investor_scope"] == subject_scope, "Investor scope projection changed")
        if subject_scope != "retail":
            term_gaps[row["code"]] = {"reason": "investor_scope_not_applicable_to_ordinary_cash",
                "investor_scope": subject_scope, "action": "public_pension_cash_scope_unsupported" if subject_scope == "personal_pension" else "capture_exact_investor_fee_applicability",
                "required_actions": [{"action": "switch_account_scope" if subject_scope == "personal_pension" else "capture_exact_investor_fee_applicability",
                                      "code": row["code"], "source_investor_scope": subject_scope}]}
            continue
        import fee_contract
        fee_contract.validate_rule(row["fee_contract"]["subscription"])
        fee_contract.validate_rule(row["fee_contract"]["redemption"])
        age_seconds = (cutoff-instant(row["observed_at"])).total_seconds()
        require(age_seconds >= 0, "Asset terms are future")
        if age_seconds > spec["availability"]["max_terms_age_seconds"]:
            stale_codes.append(row["code"])
            term_gaps[row["code"]] = {"reason": "source_terms_stale", "observed_at": row["observed_at"],
                                      "action": "refresh_source_bound_dealing_contract"}
            continue
        for name in ("subscription_fee", "redemption_fee", "min_buy", "max_buy"):
            if name in row:
                value = Decimal(decimal_string(row[name], name))
                require(value >= 0 and (name not in ("subscription_fee", "redemption_fee") or value < 1), "Invalid monetary terms")
        require(type(row["buyable"]) is bool and type(row["sellable"]) is bool, "Trading eligibility must be explicit")
        _integer(row["settlement_days"], "settlement days")
        if "confirmed_execution_max_calendar_days" in row:
            _integer(row["confirmed_execution_max_calendar_days"], "execution confirmation maximum days", 1)
            require(type(row.get("execution_bound_source")) is dict and bool(row["execution_bound_source"]),
                    "Execution confirmation bound requires source evidence")
        _finite(row["max_weight"], "max weight", 0, 1)
        terms[row["code"]] = row
        ledger.redemption_rate(row, decision_at, decision_at)
    require(set(terms) <= set(codes), "Asset terms refer outside the dynamic universe")
    purchase_eligible_codes = [code for code in purchase_eligible_codes if code in terms]
    allocation_codes = sorted({lot["code"] for lot in snapshot["positions"]} | set(purchase_eligible_codes))
    required_terms_codes = set(allocation_codes) | {row["code"] for row in snapshot["open_orders"]}
    missing_terms = sorted(required_terms_codes-set(terms))
    as_of = cutoff.astimezone(ZoneInfo(spec["timing"]["timezone"])).date().isoformat()
    positions = []
    lot_terms = {}
    occupied = {lot["lot_id"] for lot in snapshot["positions"]}
    blocked = [reason for reason in snapshot["reasons"] if reason != "no_positive_unitized_equity"]
    blocked.extend("required_dealing_contract_missing:"+code for code in missing_terms)
    pending_orders = snapshot["open_orders"]
    blocked.extend("pending_order:"+row["order_id"] for row in pending_orders)
    for lot in snapshot["positions"]:
        if lot["code"] not in terms:
            continue
        asset = terms[lot["code"]]
        import trading_calendar
        age = trading_calendar.holding_days(lot["acquired_at"], decision_at, asset["fee_contract"]["holding"])
        unlocked = age >= asset["fee_contract"]["holding"]["minimum_days"]
        effective_fee = ledger.redemption_rate(asset, lot["acquired_at"], decision_at)
        lot_terms[lot["lot_id"]] = {"redemption_fee": effective_fee, "observed_at": asset["observed_at"],
                                  "redemption_schedule": copy.deepcopy(asset.get("redemption_schedule"))}
        shares, reserved = Decimal(lot["shares"]), Decimal(lot["reserved_shares"])
        for suffix, quantity, sellable in (("", shares-reserved, asset["sellable"] and unlocked), (":reserved", reserved, False)):
            if quantity > 0 and lot["value"] is not None:
                identity = lot["lot_id"]
                if suffix:
                    identity += suffix
                    while identity in occupied:
                        identity += suffix
                    occupied.add(identity)
                positions.append({"lot_id": identity, "code": lot["code"],
                    "value": float(quantity*Decimal(snapshot["prices"][lot["code"]])), "sellable": sellable,
                    "redemption_fee": float(effective_fee), "settlement_days": asset["settlement_days"],
                    "shares": str(quantity), "acquired_at": lot["acquired_at"], "price": snapshot["prices"][lot["code"]], "fee_contract": asset["fee_contract"]})
    account = {"as_of": as_of, "cash": float(snapshot["cash"]), "reserved_cash": float(snapshot["reserved_cash"]),
               "unsettled_cash": float(snapshot["unsettled_cash"]), "positions": positions}
    assets = [{**{key: value for key, value in row.items() if key != "observed_at"},
               "buy_allowed": row["code"] in purchase_eligible_codes,
               **{key: float(row[key]) for key in ("subscription_fee", "redemption_fee", "min_buy", "max_buy") if key in row}}
              for row in terms.values()]
    assets = [asset for asset in assets if asset["code"] in allocation_codes]
    allocation_policy = dict(spec["allocation"])
    allocation_policy["funding_levels"] = [float(v) for v in allocation_policy["funding_levels"]]
    allocation_policy.update(net_principal=float(principal), loss_tolerance=float(tolerance))
    request = {"as_of": as_of, "account": account, "assets": assets, "training_policy": spec["training"],
               "allocation_policy": allocation_policy, "horizon_days": spec["planning"]["primary_horizon_days"],
               "max_feature_age_days": spec["availability"]["max_feature_age_days"],
               "nav_availability_calendar_lag": spec["availability"]["nav_lag_calendar_days"]}
    identity_map = copy.deepcopy(candidate_identities if candidate_identities is not None else market_ref.get("identities", {}))
    require(set(identity_map) == set(codes), "Source-bound identities required for every decision asset")
    for code in terms:
        subject = terms[code]["fee_contract"]["subject"]
        require(subject["code"] == code and subject["currency"] == spec["currency"], "Fee contract product/currency differs")
        require(subject["channel"] in ("TT", "天天基金"), "Fee quote belongs to another channel")
        if identity_map[code].get("share_class") is not None:
            require(subject["share_class"] == identity_map[code]["share_class"], "Fee quote share class differs from verified identity")
    exposure_rows = []
    for group, limit in spec["constraints"]["fund_group_limits"].items():
        exposure_rows.append({"name": "fund_group:"+group, "limit": limit,
                              "weights": {code: int(identity_map[code].get("fund_group_id") == group) for code in allocation_codes}})
    for sector, limit in spec["constraints"]["sector_limits"].items():
        weights = {}
        for code in allocation_codes:
            bound = (sector_exposure_bounds or {}).get(code, {}).get(sector, {})
            upper = bound.get("upper")
            require(upper is None or math.isfinite(float(upper)) and float(upper) >= 0,
                    "Source gross industry exposure must be finite nonnegative or explicitly unknown")
            weights[code] = None if upper is None else float(upper)
        # This preliminary menu cannot represent infinity. The final gross
        # exposure verifier never treats the omitted unknown row as satisfied.
        if all(value is not None for value in weights.values()):
            exposure_rows.append({"name": "sector:"+sector, "limit": limit, "weights": weights})
    allocation_policy["exposure_rows"] = exposure_rows
    result = {"schema_version": 4, "account_id": account_id, "decision_at": decision_at, "as_of": as_of,
              "known_future_actions": copy.deepcopy(known_future_actions or []), "action_inventory_scope": action_inventory_scope,
              "universe": codes, "allocation_codes": allocation_codes, "identities": identity_map, "fee_contracts": {code: copy.deepcopy(row["fee_contract"]) for code, row in terms.items()},
              "risk_state": resolved_risk, "risk_input": copy.deepcopy(risk_state),
              "purchase_eligible_codes": copy.deepcopy(purchase_eligible_codes),
              "term_gaps": term_gaps, "stale_term_codes": sorted(stale_codes),
              "spec": spec, "spec_hash": fingerprint(spec), "account_hash": fingerprint(state), "snapshot": snapshot,
              "receivables": copy.deepcopy(snapshot["receivables"]),
              "market_ref": copy.deepcopy(market_ref), "market_ref_hash": fingerprint(market_ref),
              "model_request": request, "lot_terms": lot_terms, "blocked": bool(blocked), "reasons": blocked,
              "industry_data_ref": copy.deepcopy(industry_data_ref),
              "sector_exposure_bounds": copy.deepcopy(sector_exposure_bounds or {}),
              "readiness_ref": copy.deepcopy(readiness_ref), "family_id": family_id}
    deadline = spec["planning"]["cash_deadline_days"]
    if paired_currency_validation is not None:
        fields(paired_currency_validation, {"schema_version", "frames"}, label="paired source wealth research")
        require(paired_currency_validation["schema_version"] == 1
                and type(paired_currency_validation["frames"]) is dict,
                "Invalid paired source wealth registry")
        result["paired_currency_validation"] = copy.deepcopy(paired_currency_validation)
    result["cash_requirement"] = (None if deadline is None else {
        "date": (local_day + dt.timedelta(days=deadline)).isoformat(),
        "required_amount": ledger.decimal(Decimal(spec["planning"]["cash_required_amount"])),
        "scope": "settled_unreserved_cash_at_end_of_source_receipt_date"})
    if industry_data_ref is not None:
        fields(industry_data_ref, {"bundle_hash", "news_ref", "event_frontier_hash"}, label="industry source binding")
        require(all(type(industry_data_ref[key]) is str and re.fullmatch(r"[a-f0-9]{64}", industry_data_ref[key])
                    for key in ("bundle_hash", "event_frontier_hash")), "Invalid industry source hashes")
    if trade_state is not None:
        from newtrade_guard import validate_state
        validate_state(trade_state, account_id, decision_at, spec["trade_policy"]["economic"]["fee_window_days"])
        result["trade_state"] = copy.deepcopy(trade_state)
    if trade_family_review_index is not None:
        _integer(trade_family_review_index, "system trade-family review index", 1)
        result["trade_family_review_index"] = trade_family_review_index
    result["required_actions"] = [{"action": "confirm_fill_or_confirm_cancellation", "order_id": row["order_id"],
                                   "side": row["side"], "reason": "pending_order_changes_risk_exposure_until_final_confirmation"} for row in pending_orders]
    result["required_actions"] += [{"action": "complete_source_bound_dealing_contract", "code": code} for code in missing_terms]
    for action in result["known_future_actions"]:
        code = action["code"]
        require(code in codes and action["currency"] == spec["currency"] and instant(action["known_at"]) <= cutoff, "Corporate-action identity/currency/cutoff differs")
        if market_ref["price_dates"][code] < action["ex_date"] <= as_of:
            result["reasons"].append("post_ex_NAV_required:"+code)
            result["required_actions"].append({"action": "obtain_post_ex_NAV_and_reconcile_cash_rights", "code": code, "ex_date": action["ex_date"]})
    result["known_marks"] = copy.deepcopy(market_ref["known_marks"])
    result["price_schema"] = "source_role_price_paths_v3"
    result["blocked"] = bool(result["reasons"])
    base_values = {key: result[key] for key in ("spec_hash", "account_id", "account_hash", "market_ref_hash", "model_request")}
    # The full context retains its exact cutoff and review index. A seal binds
    # financial facts, so a later observation of unchanged facts is reusable.
    base_values["trade_state"] = {key: value for key, value in result["trade_state"].items()
                                  if key not in {"as_of", "window_start", "state_hash"}}
    if not result["blocked"] and result["allocation_codes"]:
        from single_step_wealth import build_clock_context
        clock = build_clock_context(result)
        base_values["execution_clock"] = {key: value for key, value in clock.items() if key != "order_time_local"}
    else:
        base_values["execution_clock"] = {"status": "not_executable", "reasons": result["reasons"]}
    # A later read of the same valid risk profile is not a financial change.
    # Its exact observation timestamp remains bound by the full context hash.
    base_values["risk_state"] = {key: value for key, value in resolved_risk.items() if key != "as_of"}
    base_hash = fingerprint(base_values)
    if sealed_intervention is not None:
        require(spec["kind"] == "assisted_workflow", "Numeric policies reject sealed interventions")
        require(type(news_review_hash) is str and re.fullmatch(r"[a-f0-9]{64}", news_review_hash), "Sealed news review digest required")
    result.update(base_context_hash=base_hash, sealed_intervention=copy.deepcopy(sealed_intervention), news_review_hash=news_review_hash)
    return {**result, "context_hash": fingerprint(result)}
