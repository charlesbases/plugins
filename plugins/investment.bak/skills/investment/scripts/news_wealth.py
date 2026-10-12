"""Explicit research-account currency replay; no actual orders or profit claims.

Frames are registered in context.paired_currency_validation.frames by OOS date.
Six-coordinate forecasts support only the declared current-action/hold study.
Full feedback studies require independently mature multi-date EN fit frames.
"""
import copy
import datetime as dt
import math
from decimal import Decimal

import allocation
import allocation_market as market
import fee_contract as fees
import portfolio_mpc as mpc
import portfolio_paths
import single_step_wealth as wealth
from contracts import ContractError, fingerprint, instant, require
from research_data import source_version_available

ACCOUNT_SCOPE = "declared_research_all_cash_not_actual_user_history"


def _gap(reason, **details):
    return {"status": "insufficient_evidence", "reason": reason, "profitability_claim": False,
            "actual_orders_created": False, "required_actions": [{"action": "complete_registered_source_currency_replay_frame", "reason": reason, **details}]}


def _archive(version, cutoff):
    available = source_version_available(version)
    require(available is not None and available <= instant(cutoff), "Original source capture is unavailable at its information cutoff")
    return available


def _quote(rows, date, cutoff):
    table = {row["date"]: row for row in rows}
    require(len(table) == len(rows) and date in table, "Original source NAV date is missing or duplicated")
    quote = market.nav_snapshot(table[date], cutoff, require_known=True)
    require(quote is not None, "Original source NAV vintage is unavailable")
    versions = table[date]["source_versions"]
    selected = next(row for row in versions if row["version_id"] == quote["source_version_id"])
    require(selected["version_id"] == fingerprint({key:value for key,value in selected.items() if key!="version_id"}), "Original NAV source version identity changed")
    _archive(selected, cutoff)
    require(fees.number(quote["nav"]) > 0, "Positive original NAV required")
    return quote


def _features(sample, raw, origin):
    cutoff = sample["feature_cutoff_date"]
    feature = {"feature_cutoff_nav_date": cutoff}
    selected = market._feature_at(feature, raw, origin, 1)
    require(selected is not None, "Original 121-observation feature history is unavailable")
    vector, proof = selected
    require(proof["availability_basis"] == "source_vintages", "Proxy NAV history cannot establish source replay")
    vector += [(dt.date.fromisoformat(origin[:10])-dt.date.fromisoformat(cutoff)).days]
    require(len(sample["x"]) >= len(vector) and all(math.isclose(a,b,abs_tol=1e-10,rel_tol=1e-10) for a,b in zip(vector,sample["x"])), "Historical native features differ from original source versions")
    require(sample["feature_source"]["source_version_ids"] == proof["source_version_ids"], "Historical feature source versions differ")
    return proof


def _base_context(context, record, frame, samples):
    codes = record["codes"]
    require(codes == context["allocation_codes"] and len(samples) == len(codes), "Paired source fund family differs")
    account = frame["account"]
    require(account["kind"] == "declared_all_cash_research" and account["currency"] == "CNY"
            and account["external_cashflows"] == [], "Explicit all-cash research account with zero external flows required")
    cash = fees.number(account["cash_cny"])
    require(cash > 0, "Positive declared research capital required")
    require(frame["fee_scope"] in {"current_terms_repriced", "source_historical_terms"}, "Explicit fee replay scope required")
    contracts = frame["fee_contracts"]
    require(set(contracts) == set(codes), "Replay fee contract family differs")
    if frame["fee_scope"] == "current_terms_repriced":
        require(fingerprint(contracts) == fingerprint({code:context["fee_contracts"][code] for code in codes}), "Current-terms replay differs from verified current contracts")
    else:
        for code in codes:
            proof = frame["fee_sources"][code]
            _archive(proof, record["origin_at"])
            require(proof["raw_ref"]["sha256"] == fingerprint(contracts[code]), "Historical fee contract differs from its source frame")
    require(frame["study_mode"] in {"retrospective_source_oos", "prospective_registered_observation"}, "Explicit retrospective or prospective study mode required")
    require(frame["protocol_hash"] == fingerprint(frame["protocol"]), "Frozen study protocol changed")
    if frame["study_mode"] == "prospective_registered_observation":
        _archive(frame["registration_source"], record["origin_at"])
        require(frame["registration_source"]["raw_ref"]["sha256"] == fingerprint(frame["protocol"]), "Prospective protocol registration differs")
    require(frame["policy_scope"] in {"current_action_then_hold", "full_source_mpc"}, "Explicit study policy scope required")
    protocol = frame["protocol"]
    stages = frame["stage_dates"]
    require(stages == sorted(set(stages)) and len(stages) >= 2 and stages[0] == record["date"], "Registered source stages differ from paired origin")
    raw = frame["source_nav"]
    require(set(raw) == set(codes), "Original NAV family differs")
    prices, price_dates, known_marks = {}, {}, {}
    for code,sample in zip(codes,samples):
        require(sample["code"] == code and sample["decision_date"] == record["date"], "Original source sample identity differs")
        proof = _features(sample,raw[code],record["origin_at"])
        prices[code] = str(proof["base_nav"])
        price_dates[code] = sample["feature_cutoff_date"]
        mark = _quote(raw[code], price_dates[code], record["origin_at"])
        require(fees.number(mark["nav"]) == fees.number(prices[code]), "Research mark differs from original feature NAV")
        known_marks[code] = {"nav_date": price_dates[code], "value": prices[code],
                             "known_at": mark["known_at"], "source_ref": copy.deepcopy(mark["raw_ref"])}
        industry = sample["industry_source"]
        require(industry["origin_at"] == record["origin_at"] and industry["source_hash"] == fingerprint({k:v for k,v in industry.items() if k!="source_hash"}), "Original covariate source witness differs")
        references = [row for row in frame["source_frontier"] if row.get("value_hash") == industry["source_hash"]]
        require(references, "Original news/factor frontier is missing")
        for ref in references: _archive(ref,record["origin_at"])
    research = copy.deepcopy(context)
    research.pop("paired_currency_validation",None)
    research.pop("cash_requirement",None)
    research.update(decision_at=record["origin_at"],as_of=record["date"],allocation_codes=codes,universe=codes,
                    known_future_actions=[],receivables=[],account_scope=ACCOUNT_SCOPE,
                    price_schema="source_role_price_paths_v3",known_marks=known_marks)
    research["fee_contracts"] = copy.deepcopy(contracts)
    research["snapshot"] = {"cash":str(cash),"available_cash":str(cash),"reserved_cash":"0","unsettled_cash":"0",
        "equity":str(cash),"positions":[],"receivables":[],"open_orders":[],"prices":prices}
    research["market_ref"] = {"prices":prices,"price_dates":price_dates,"known_marks":copy.deepcopy(known_marks)}
    research["model_request"]["allocation_policy"] = {"funding_levels":[0]}
    research["model_request"]["assets"] = [copy.deepcopy(row) for row in context["model_request"]["assets"] if row["code"] in codes]
    research["purchase_eligible_codes"] = [code for code in context["purchase_eligible_codes"] if code in codes]
    horizon = (dt.date.fromisoformat(stages[-1])-dt.date.fromisoformat(stages[0])).days
    research["spec"]["planning"].update(primary_horizon_days=horizon,primary_goal=protocol["primary_goal"],cash_deadline_days=None,cash_required_amount=None)
    research["spec"]["decision"].update({key:protocol[key] for key in ("max_current_actions","max_policy_count","future_cash_fractions")})
    research["spec"]["allocation"]["tail_probability"] = protocol["tail_probability"]
    research["spec_hash"] = fingerprint(research["spec"])
    tolerance = fees.number(protocol["loss_tolerance"])
    require(0 <= tolerance < 1, "Declared research loss tolerance required")
    research["risk_state"] = {"net_principal":str(cash),"loss_tolerance":str(tolerance),"principal_floor":str(cash*(1-tolerance)),"remaining_loss_budget":str(cash*tolerance)}
    research["context_hash"] = fingerprint({key:value for key,value in research.items() if key!="context_hash"})
    return research


def _observed_path(context, record, frame, samples, research):
    import numeric_validation
    nav, events = {}, {}
    origin, end = record["date"], frame["stage_dates"][-1]
    for code,sample in zip(record["codes"],samples):
        source = sample["label_source"]
        require(source == record["source_labels"][code] and source["source_hash"] == record["source_hashes"][code]
                and source["source_hash"] == fingerprint({k:v for k,v in source.items() if k!="source_hash"}), "Outer source label differs from preserved paired record")
        require(instant(source["label_available_at"]) < instant(context["decision_at"]) and source["end_date"] == end, "Outer label is immature or has a different endpoint")
        raw = frame["source_nav"][code]
        selected = {row["date"]:_quote(raw,row["date"],source["label_available_at"]) for row in raw if source["base_date"] <= row["date"] <= end}
        selected[source["base_date"]] = _quote(raw,source["base_date"],record["origin_at"])
        preserved = {quote["date"]:quote for quote in source["source_quotes"]}
        for date,quote in selected.items():
            if date in preserved:
                require(quote["source_version_id"] == preserved[date]["source_version_id"] and quote["raw_ref"] == preserved[date]["raw_ref"], "Outer original NAV identity differs from preserved source label")
        numeric_validation._source_values(source,selected,source["base_date"],source["pricing_date"],source["ownership_date"],end)
        for date,quote in selected.items():
            if date >= origin: nav.setdefault(date,{})[code] = quote["nav"]
        for event in source["dividends"]:
            require(event.get("known_at") and instant(event["known_at"]) <= instant(source["label_available_at"]), "Cash entitlement original availability is missing")
            if event["ex_date"] <= origin:
                continue
            enriched = {**event,"code":code,"id":event.get("id",fingerprint(event))}
            events[enriched["id"]] = enriched
    require(nav and end in nav and set(nav[end]) == set(record["codes"]), "Outer source terminal NAV support is incomplete")
    return {"id":"observed-"+fingerprint(record["source_hashes"])[:24],"origin_at":record["origin_at"],
            "price_schema":"source_role_price_paths_v3","known_marks":copy.deepcopy(research["known_marks"]),
            "probability":1.,"nav":nav,"distributions":list(events.values()),"scope":"original_source_observed_prices_research_account_cash_policy"}


def _forecast_paths(arm, forecast, frame, record, samples, research):
    """Reconstruct a registered multi-date EN fit and decode without interpolation."""
    require(forecast["method"] == "source_multi_date_elastic_net", "Registered multi-date EN forecast required")
    columns = forecast["input_columns"]
    require(columns == sorted(set(columns)) and columns and all(type(i) is int and i >= 0 for i in columns), "Declared forecast covariate columns required")
    rows = forecast["training_rows"]
    require(rows and len({(row["code"],row["decision_date"]) for row in rows}) == len(rows), "Independent mature training rows required")
    prepared = []
    for original in rows:
        row = copy.deepcopy(original)
        witness = row["label_source"]
        require(row["source_hash"] == fingerprint(witness) and witness["code"] == row["code"]
                and instant(row["label_available_at"]) < instant(record["origin_at"]), "Forecast training uses an immature or altered outer label")
        original_source = witness["original_label_source"]
        proof_sample = {"x":row["x"],"feature_cutoff_date":witness["base_date"],"feature_source":original_source["feature_source"]}
        _features(proof_sample,frame["source_nav"][row["code"]],row["decision_date"]+"T"+record["origin_at"][11:])
        require(row["input_source"]["raw_ref"]["sha256"] == fingerprint(row["x"]), "Forecast training covariates differ from their original frontier")
        _archive(row["input_source"],row["decision_date"]+"T"+record["origin_at"][11:])
        base = _quote(frame["source_nav"][row["code"]],witness["base_date"],row["decision_date"]+"T"+record["origin_at"][11:])
        require(math.isclose(float(base["nav"]),row["base_nav"],abs_tol=1e-10), "Training source base NAV differs")
        window = witness["window_quotes"]
        expected_dates = [quote["date"] for quote in frame["source_nav"][row["code"]]
                          if witness["base_date"] < quote["date"] <= witness["window_end_date"]]
        require([quote["date"] for quote in window] == expected_dates, "Training full source cash/NAV window is incomplete")
        events = []
        for quote in window:
            original_quote = _quote(frame["source_nav"][row["code"]],quote["date"],row["label_available_at"])
            require(original_quote["source_version_id"] == quote["source_version_id"]
                    and original_quote["nav"] == quote["nav"] and original_quote["raw_ref"] == quote["raw_ref"],
                    "Training window NAV source differs")
            cash = [event for event in original_quote.get("corporate_actions",[]) if event.get("kind") == "cash_distribution"]
            require(math.isclose(math.fsum(float(event["per_share"]) for event in cash),float(original_quote["distribution_per_share"]),abs_tol=1e-12),
                    "Training full-window cash source is incomplete")
            require(all(all(event.get(key) is not None for key in
                            ("record_date","ex_date","pay_date","entitlement_rule","evidence_ref","known_at"))
                        and event["record_date"] <= event["ex_date"] <= event["pay_date"]
                        and event["ex_date"] == quote["date"] and event.get("currency") == "CNY"
                        and event.get("distribution_mode") == "cash"
                        and instant(event["known_at"]) <= instant(row["label_available_at"])
                        for event in cash), "Training full-window cash rights or maturity source is incomplete")
            events += copy.deepcopy(cash)
        require(events == witness["distributions"], "Training complete cash source differs")
        future = [event for event in events if event["ex_date"] > row["decision_date"]]
        require(future == witness["future_distributions"] and row["cash"] == future,
                "Training future cash source differs from economic ex-date scope")
        measured = []
        for date,quote in zip(witness["observation_dates"],witness["nav_quotes"]):
            original_quote = _quote(frame["source_nav"][row["code"]],date,row["label_available_at"])
            require(original_quote["source_version_id"] == quote["source_version_id"] and original_quote["nav"] == quote["nav"], "Multi-date training NAV source differs")
            cash = math.fsum(float(event["per_share"]) for event in future if event["ex_date"] <= date)
            measured.append((float(original_quote["nav"])+cash)/float(base["nav"]))
        require(len(measured) == len(row["wealth_ratios"]) and all(math.isclose(a,b,abs_tol=1e-10) for a,b in zip(measured,row["wealth_ratios"])), "Training path wealth ratios differ from original NAV/cash")
        row["x"] = [row["x"][i] for i in columns]
        prepared.append(row)
    from industry_model import FUND_FEATURE_NAMES
    names = market.FEATURE_NAMES+FUND_FEATURE_NAMES
    model = portfolio_paths._fit_model(prepared,forecast["cash_keys"],forecast["fund_groups"],forecast["alpha"],forecast["l1_ratio"],feature_names=[names[i] for i in columns])
    require(fingerprint(model) == forecast["model_hash"], "Registered multi-date model differs from mature source refit")
    contract = forecast["contract"]
    common = set.intersection(*(set(research["fee_contracts"][code]["execution_calendar"]["open_dates"])
                                for code in record["codes"]))
    expected_dates = sorted(day for day in common if record["date"] <= day <= frame["stage_dates"][-1])
    require(contract.get("price_schema") == "source_role_price_paths_v3"
            and contract["codes"] == record["codes"] and contract["origin_date"] == record["date"]
            and contract["decision_at"] == record["origin_at"]
            and contract["known_marks"] == research["known_marks"]
            and contract["nav_dates"] == expected_dates
            and contract["nav_dates"] == sorted(set(contract["nav_dates"]))
            and contract["nav_dates"][0] >= record["date"]
            and contract["nav_dates"][-1] == frame["stage_dates"][-1], "Forecast source role/date contract differs")
    latent = {}
    for code,sample in zip(record["codes"],samples):
        row = {**sample,"x":[sample["x"][i] for i in columns]}
        values = portfolio_paths._predict(model,row)
        require(len(values) == len(contract["nav_dates"])+len(model["cash_keys"]), "Multi-date forecast coordinate count differs")
        latent[code] = {"values":list(values),"cash_keys":model["cash_keys"]}
    point = portfolio_paths._decode(latent,{code:float(research["snapshot"]["prices"][code]) for code in record["codes"]},
        contract,record["origin_at"],[row["label_source"] for row in rows],arm+"-point",1.,record["label_available"])
    return [point], fingerprint(model)


def _endpoint_values(research, policies, prediction, record):
    codes = record["codes"]
    require(set(prediction) == set(codes), "Paired prediction fund family differs")
    decoded = {code:market.decode_latents(prediction[code]) for code in codes}
    targets = {name:[[decoded[code][name] for code in codes]] for name in next(iter(decoded.values()))}
    distribution = {"codes":codes,"dates":[record["date"]],"probabilities":[1.],"targets":targets,
        "returns":[[decoded[code]["hold"]-1 for code in codes]],"clock_context":wealth.build_clock_context(research)}
    require(all(record["source_labels"][code]["pricing_date"] == distribution["clock_context"]["assets"][code]["pricing_date"]
                for code in codes), "Endpoint forecast pricing role differs from source clock")
    pricing_nav = {code:float(research["snapshot"]["prices"][code])*decoded[code]["pricing"] for code in codes}
    values = []
    for policy in policies:
        try:
            projection = wealth.project(research,policy["current_action"],distribution,pricing_nav=pricing_nav)
        except ContractError:
            continue
        result = wealth.value(research,projection,distribution)
        if projection["concentration_feasible"] and all(row["targets_modeled"] for row in result["scenario_rows"]):
            terminal = result["wealth_lower"][0]
            values.append((terminal,float(research["snapshot"]["equity"])-terminal,projection["fees"],projection["turnover"],policy))
    return values


def _path_values(research, policies, paths):
    values = []
    distribution = mpc.as_current_order_distribution(research,{"status":"research_ready","point_paths":[paths[0]],"selection_paths":paths,"path_hash":fingerprint(paths)})
    pricing_nav = {code:mpc._price(paths[0],code,distribution["clock_context"]["assets"][code]["pricing_date"],execution=True)
                   for code in research["allocation_codes"]}
    for policy in policies:
        try:
            projection = wealth.project(research,policy["current_action"],distribution,pricing_nav=pricing_nav)
        except ContractError:
            continue
        if not projection["concentration_feasible"]:
            continue
        try:
            require(policy["local_decision_scope_complete"], "Registered policy local decision scope is incomplete")
            outcomes = [mpc.simulate(research,path,policy["current_action"],policy["future_rule"],policy["local_decision_dates"]) for path in paths]
        except mpc.PolicyInfeasible:
            continue
        masses = [path["probability"] for path in paths]
        terminal = math.fsum(p*row["terminal_wealth"] for p,row in zip(masses,outcomes))
        risk = mpc.tail_mean([float(research["snapshot"]["equity"])-row["terminal_wealth"] for row in outcomes],masses,research["spec"]["allocation"]["tail_probability"])
        values.append((terminal,risk,math.fsum(p*row["fees"] for p,row in zip(masses,outcomes)),
                       math.fsum(p*row["policy_turnover"] for p,row in zip(masses,outcomes)),policy))
    return values


def _cash_curve(initial, path, value):
    """Independent state totals from the modeled source cash/share journal."""
    cash,reserved,receivable = Decimal(str(initial)),Decimal(0),Decimal(0)
    shares,curve = {},[]
    logs = value["cash_flow_log"]
    for date in sorted(set(path["nav"]) | {row["date"] for row in logs} | {path["origin_at"][:10]}):
        for row in [item for item in logs if item["date"] == date]:
            if "available_delta" in row:
                cash += Decimal(str(row["available_delta"]))
                reserved += Decimal(str(row["reserved_delta"]))
            kind = row["kind"]
            if kind == "price_buy":
                shares[row["code"]] = shares.get(row["code"],Decimal(0))+fees.number(row["shares"])
                receivable += fees.number(row["refund"])
            elif kind == "price_sell":
                shares[row["code"]] -= fees.number(row["shares"])
                receivable += fees.number(row["receivable"])
            elif kind == "distribution_receivable":
                receivable += fees.number(row["amount"])
            elif kind.startswith("credit_"):
                receivable -= fees.number(row["amount"])
            require(cash >= 0 and reserved >= 0 and receivable >= 0, "Research cash/share journal violates conservation")
        total = cash+reserved+receivable
        for code,quantity in shares.items():
            total += quantity*mpc._price(path,code,date)
        curve.append({"date":date,"wealth_cny":float(total)})
    require(math.isclose(curve[-1]["wealth_cny"],float(value["settled_cash"])+float(value["reserved_cash"])+float(value["receivables"])
        +math.fsum(float(row["shares"])*float(mpc._price(path,row["code"],max(path["nav"]))) for row in value["terminal_lots"]),abs_tol=1e-8), "Independent observed currency curve differs from source terminal state")
    curve[-1]["wealth_cny"] = value["terminal_wealth"]
    return curve


def evaluate_pair(*, context, record, full_prediction, native_prediction, source_rows, source_verifier=None):
    registry = context.get("paired_currency_validation")
    if not registry or record["date"] not in registry.get("frames",{}):
        return _gap("registered_currency_research_frame_unavailable",origin=record["date"])
    try:
        require(registry["schema_version"] == 1, "Unsupported currency study frame schema")
        frame = registry["frames"][record["date"]]
        archive_proof = None
        if source_verifier is not None:
            archive_proof = source_verifier(context=context,record=record,frame=frame,source_rows=source_rows)
            require(archive_proof["status"] == "verified_original_archives"
                    and archive_proof["frame_ref"] == fingerprint(frame)
                    and archive_proof["source_labels_hash"] == fingerprint(record["source_hashes"])
                    and archive_proof["audited_artifact_ids"], "Independent original archive verification is incomplete")
        research = _base_context(context,record,frame,source_rows)
        comparison = allocation.build_source_actions(research)
        require(comparison["status"] == "frozen" and not comparison["required_actions"], "Source action family is incomplete for registered study")
        frozen = mpc.freeze_policies(research,comparison,max_current_actions=frame["protocol"]["max_current_actions"],cash_fractions=frame["protocol"]["future_cash_fractions"])
        require(frozen["status"] == "frozen", "Declared source policy family computation is incomplete")
        policies = frozen["policies"]
        if frame["policy_scope"] == "current_action_then_hold":
            policies = [row for row in policies if row["future_rule"]["kind"] == "hold"]
        require(len(policies) <= frame["protocol"]["max_policy_count"], "Declared currency replay policy budget exceeded")
        require(policies, "Nonempty frozen research family required")
        family_hash = fingerprint(policies)
        if frame["policy_scope"] == "full_source_mpc":
            require("forecast_full" in frame and "forecast_native" in frame, "Registered per-arm multi-date source forecast is missing")
            full_fit,native_fit = frame["forecast_full"],frame["forecast_native"]
            cohort = lambda fit: sorted((row["code"],row["decision_date"],row["source_hash"]) for row in fit["training_rows"])
            require(cohort(full_fit) == cohort(native_fit), "Paired multi-date forecasts changed the original mature cohort")
            require(all(full_fit[key] == native_fit[key] for key in ("contract","fund_groups","cash_keys")), "Paired multi-date target or fund/date support differs")
            require(native_fit["input_columns"] == list(range(len(market.FEATURE_NAMES)))
                    and set(native_fit["input_columns"]) <= set(full_fit["input_columns"]), "Native multi-date arm must retain the same six source NAV inputs")
        selected,models = {},{}
        for arm,prediction in (("full",full_prediction),("native",native_prediction)):
            if frame["policy_scope"] == "full_source_mpc":
                require("forecast_"+arm in frame, "Registered per-arm multi-date source forecast is missing")
                paths,models[arm] = _forecast_paths(arm,frame["forecast_"+arm],frame,record,source_rows,research)
                candidates = _path_values(research,policies,paths)
            else:
                candidates = _endpoint_values(research,policies,prediction,record)
                models[arm] = fingerprint(prediction)
            feasible = [row for row in candidates if row[1] <= float(research["risk_state"]["remaining_loss_budget"])]
            require(feasible, "No same-budget source-feasible policy exists")
            selected[arm] = min(feasible,key=lambda row:(-row[0],row[1],row[2],row[3],row[4]["id"]))
        observed = _observed_path(context,record,frame,source_rows,research)
        common = {"candidate_family_hash":fingerprint(record["codes"]),"account_state_hash":fingerprint(research["snapshot"]),
            "fee_contracts_hash":fingerprint(research["fee_contracts"]),"clock_hash":fingerprint(wealth.build_clock_context(research)),
            "external_cashflows_hash":fingerprint(frame["account"]["external_cashflows"]),"initial_wealth_cny":float(research["snapshot"]["equity"])}
        frame_hash = fingerprint(common)
        result = {"status":"source_replay_ready" if archive_proof is not None else "conditional_currency_replay_ready",**common,"frame_hash":frame_hash,"policy_family_hash":family_hash,
            "study_policy_scope":frame["policy_scope"],"account_scope":ACCOUNT_SCOPE,"fee_scope":frame["fee_scope"],"study_mode":frame["study_mode"],
            "actual_orders_created":False,"profitability_claim":False,"full_production_MPC_advantage_validated":False,
            "source_evidence":{"strict_PIT_verified":archive_proof is not None,"frame_source_PIT_verified":archive_proof is not None and frame["fee_scope"] == "source_historical_terms",
                "provenance_status":"independently_audited_original_archives" if archive_proof is not None else "metadata_consistent_original_archive_authenticity_unverified",
                "archive_proof":archive_proof,
                "historical_frame_ref":fingerprint(frame),"source_labels_hash":fingerprint(record["source_hashes"]),
                "replay_scope":frame["fee_scope"],"account_scope":ACCOUNT_SCOPE,"study_policy_scope":frame["policy_scope"],
                "study_mode":frame["study_mode"],"protocol_hash":frame["protocol_hash"],"policy_family_hash":family_hash,
                "risk_metric_scope":"common_horizon_predicted_loss_degenerate_point_CVaR_no_residual_risk_evaluation",
                "no_residual_risk_evaluation":True},
            "limitations":["retrospective_posthoc_strategy_simulation_not_prospective_trial" if frame["study_mode"] == "retrospective_source_oos" else "declared_research_account_not_actual_user_PnL",
                "covariate_block_comparison_not_isolated_news_causality","unadjusted_descriptive_currency_pairs_not_profit_evidence"]}
        for arm in ("full","native"):
            expected,risk,_,_,policy = selected[arm]
            require(policy["local_decision_scope_complete"], "Registered policy local decision scope is incomplete")
            value = mpc.simulate(research,observed,policy["current_action"],policy["future_rule"],policy["local_decision_dates"])
            mpc.verify_cash_flow(value)
            curve = _cash_curve(common["initial_wealth_cny"],observed,value)
            peak,drawdown = common["initial_wealth_cny"],0.
            drawdown_amounts = []
            for row in curve:
                peak = max(peak,row["wealth_cny"])
                drawdown = max(drawdown,1-row["wealth_cny"]/peak)
                drawdown_amounts.append(peak-row["wealth_cny"])
            result[arm] = {"frame_hash":frame_hash,"after_fee_terminal_wealth_cny":value["terminal_wealth"],
                "maximum_drawdown":drawdown,"cvar_loss_cny":risk,
                "path_drawdown_tail_mean_cny":mpc.tail_mean(drawdown_amounts,[1/len(curve)]*len(curve),frame["protocol"]["tail_probability"]),
                "turnover_cny":value["policy_turnover"],"predicted_terminal_wealth_cny":expected,"predicted_cvar_loss_cny":risk,
                "selected_policy":policy,"model_hash":models[arm],"valuation_hash":value["valuation_hash"],"observed_wealth_curve":curve}
        result["result_hash"] = fingerprint(result)
        return result
    except (ContractError,ValueError,KeyError,TypeError,IndexError,StopIteration) as error:
        return _gap(str(error),origin=record["date"])
