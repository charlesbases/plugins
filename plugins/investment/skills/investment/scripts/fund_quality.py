"""Source-rebuilt descriptive quality, separate from forecast and allocation.

OLS alpha is model-dependent historical association, not manager skill or alpha
available for a new trade. Moving-block bootstrap assumes approximate weak
stationarity within a disclosed management regime. Holm correction applies to
the declared finite set of alpha hypotheses, using approximate bootstrap p's.
"""
import copy
import datetime as dt
import math
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

from contracts import EvidenceError, fields, fingerprint, instant, require
import fund_universe
import manager_evidence
import research_data
import source_documents
import source_fetch

MODELS = {"equity_market": ("equity", {"equity_market"}),
          "bond_duration_credit": ("bond", {"bond_duration", "bond_credit"}),
          "mixed_equity_bond": ("mixed", {"equity_market", "bond_duration", "bond_credit"})}
DEFAULT_POLICY = {"lookback_sessions": 252, "min_observations": 60,
                  "bootstrap_resamples": 999, "block_length": 5,
                  "confidence": .95, "annualization_sessions": 252, "seed": 0}
REFERENCES = {"factor_evaluation": "https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/portfolio-performance-evaluation",
              "dependent_bootstrap": "https://doi.org/10.1214/aos/1176347265",
              "holm": "https://www.jstor.org/stable/4615733",
              "tracking": "https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/exchange-traded-funds-mechanics-applications"}


class QualityUnavailable(EvidenceError):
    """A genuine missing qualification, not a failed source integrity check."""


def _need(value, reason):
    if not value:
        raise QualityUnavailable(reason)


def _policy(value):
    result = copy.deepcopy(DEFAULT_POLICY if value is None else value)
    fields(result, set(DEFAULT_POLICY), label="quality policy")
    for key in ("lookback_sessions", "min_observations", "bootstrap_resamples", "block_length", "annualization_sessions", "seed"):
        require(type(result[key]) is int and result[key] >= (0 if key == "seed" else 1), "Quality integer policy invalid: " + key)
    require(10 <= result["min_observations"] <= result["lookback_sessions"] <= 20000,
            "Quality sample policy invalid")
    require(199 <= result["bootstrap_resamples"] <= 5000 and 1 < result["block_length"] <= result["min_observations"]//2,
            "Quality block/Monte Carlo budget invalid")
    require(type(result["confidence"]) in (int, float) and .8 <= result["confidence"] < 1,
            "Quality confidence policy invalid")
    return result


def _blocks(document, locator):
    require(type(locator) is str and locator, "Quality original locator required")
    rows = [row for row in document["blocks"] if row["locator"] == locator or row["locator"].startswith(locator+"/")]
    _need(rows, "unsupported_or_missing_original_quality_locator")
    return rows


def _labels(blocks):
    values = {}
    for block in blocks:
        headers, cells = block.get("headers", []), block.get("cells", [])
        pairs = []
        if block.get("kind") == "table_row" and len(cells) == 2 and cells != headers:
            pairs.append(tuple(cells))
        elif block.get("kind") != "table_cell":
            for entry in re.split(r"[;；\n]", block["text"]):
                match = re.fullmatch(r"\s*([^:：]+)[:：]\s*(.+?)\s*", entry)
                if match:
                    pairs.append(match.groups())
        for label, value in pairs:
            require(label not in values or values[label] == value, "Conflicting original quality metadata")
            values[label] = value
    return values


def _label(values, *names):
    found = {values[name] for name in names if name in values}
    _need(len(found) == 1, "missing_or_ambiguous_quality_metadata:" + names[0])
    return next(iter(found))


def _document(reference, artifacts, at):
    document = source_documents.read_extracted_document(reference, artifacts)
    require(instant(document["retrieved_at"]) <= instant(at), "Future quality source document")
    _need(source_fetch.source_rule(document["source_id"])["purpose"] in ("issuer_document", "benchmark_identity"),
          "quality_requires_registered_financial_original_source")
    return document


def _series(source, artifacts, at):
    fields(source, {"document_ref", "metadata_locator", "table_locator"}, label="quality return series")
    document = _document(source["document_ref"], artifacts, at)
    values = _labels(_blocks(document, source["metadata_locator"]))
    metadata = {"series_id": _label(values, "序列标识", "Series ID"),
                "currency": _label(values, "币种", "Currency"),
                "hedge_policy": _label(values, "对冲口径", "Hedge Policy"),
                "return_definition": _label(values, "收益口径", "Return Definition"),
                "frequency": _label(values, "频率", "Frequency"),
                "asset_scope": _label(values, "资产类型", "Asset Scope")}
    metadata["currency"] = {"人民币": "CNY"}.get(metadata["currency"], metadata["currency"])
    metadata["hedge_policy"] = {"未对冲": "unhedged", "对冲": "hedged"}.get(metadata["hedge_policy"], metadata["hedge_policy"])
    metadata["frequency"] = {"日频": "daily"}.get(metadata["frequency"], metadata["frequency"])
    metadata["asset_scope"] = {"股票": "equity", "债券": "bond", "混合": "mixed"}.get(metadata["asset_scope"], metadata["asset_scope"])
    metadata["return_definition"] = {"分红再投资总回报": "total_return", "超额收益": "excess_return",
                                      "无风险区间收益": "risk_free_return"}.get(metadata["return_definition"], metadata["return_definition"])
    metadata["factor_role"] = values.get("因子作用", values.get("Factor Role"))
    _need(metadata["frequency"] == "daily" and metadata["return_definition"] in
          ("total_return", "excess_return", "risk_free_return"), "unsupported_quality_return_frequency_or_definition")
    output = {}
    for row in _blocks(document, source["table_locator"]):
        if row.get("kind") != "table_row" or row.get("cells") == row.get("headers"):
            continue
        headers, cells = row["headers"], row["cells"]
        _need(len(headers) == len(cells) and len(set(headers)) == len(headers), "quality_return_table_layout_unknown")
        record = dict(zip(headers, cells))
        day = dt.date.fromisoformat(_label(record, "日期", "Date")).isoformat()
        before = dt.date.fromisoformat(_label(record, "上一观察日", "Previous Date")).isoformat()
        _need(before < day and dt.date.fromisoformat(day) <= instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date(), "quality_return_period_invalid_or_future")
        raw = _label(record, "收益率", "Return")
        require(re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?%?", raw) is not None, "Invalid source quality return")
        rate = float(Decimal(raw.rstrip("%"))/(100 if raw.endswith("%") else 1))
        require(math.isfinite(rate) and (metadata["return_definition"] == "excess_return" or rate > -1), "Invalid quality return domain")
        available = _label(record, "可得时间", "Available At")
        # A publisher's historical availability quote cannot backdate capture.
        known = max((available, document["retrieved_at"]), key=instant)
        require(instant(available) <= instant(at) and instant(known) <= instant(at), "Future quality factor vintage")
        require(day not in output, "Duplicate source quality return date")
        output[day] = {"date": day, "previous_date": before, "return": rate, "known_at": known,
                       "source_available_at": available, "locator": row["locator"]}
    _need(output, "empty_original_quality_return_table")
    return {**metadata, "rows": output, "source_ref": source, "document_id": document["document_id"],
            "known_at": document["retrieved_at"], "raw_sha256": document["raw_sha256"]}


def _fund_scope(contract, identity, artifacts, at):
    source = contract.get("fund_scope_source")
    _need(source, "original_fund_style_currency_hedge_and_asset_scope_missing")
    fields(source, {"document_ref", "locator"}, label="fund quality scope source")
    document = _document(source["document_ref"], artifacts, at)
    _need(source_fetch.source_rule(document["source_id"])["purpose"] == "issuer_document", "original_fund_scope_requires_issuer")
    labels = _labels(_blocks(document, source["locator"]))
    code = _label(labels, "基金代码", "Fund Code")
    asset = _label(labels, "资产类型", "Asset Scope")
    asset = {"股票": "equity", "债券": "bond", "混合": "mixed"}.get(asset, asset)
    mode = _label(labels, "管理方式", "Management Style")
    mode = {"主动": "active", "被动": "passive"}.get(mode, mode)
    currency = _label(labels, "币种", "Currency")
    currency = {"人民币": "CNY"}.get(currency, currency)
    hedge = _label(labels, "对冲口径", "Hedge Policy")
    hedge = {"未对冲": "unhedged", "对冲": "hedged"}.get(hedge, hedge)
    _need(code == identity["code"] and asset == contract["asset_scope"] and mode == contract["mode"]
          and currency == identity.get("currency") and hedge == contract["hedge_policy"],
          "fund_quality_contract_differs_from_original_primary_product_scope")
    return {"source": source, "asset_scope": asset, "mode": mode, "currency": currency,
            "hedge_policy": hedge, "known_at": document["retrieved_at"], "document_id": document["document_id"]}


def _nav(data, contract, artifacts, at):
    code = contract["code"]
    record = artifacts.read_json(contract["nav_source_ref"])
    fields(record, {"capture", "raw_ref"}, label="quality original NAV capture")
    require(source_fetch.source_rule(record["capture"]["registry_source_id"])["purpose"] == "market", "Quality NAV must be a registered market source")
    require(instant(record["capture"]["retrieved_at"]) <= instant(at), "Future quality NAV capture")
    text = fund_universe.capture_text(record, artifacts)
    rows = data["nav"].get(code, [])
    _need(len(rows) >= 2, "quality_nav_history_missing")
    require(rows[-1]["date"] <= instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(), "Future quality NAV date")
    rebuilt, _ = research_data.parse_nav(text, code, dt.date.fromisoformat(rows[0]["date"]), dt.date.fromisoformat(rows[-1]["date"]), {})
    require([(row["date"], row["nav"], row.get("distribution_per_share")) for row in rebuilt] ==
            [(row["date"], row["nav"], row.get("distribution_per_share")) for row in rows],
            "Quality NAV or distributions differ from original source")
    return [{"date": after["date"], "previous_date": before["date"],
             "return": (after["nav"]+after["distribution_per_share"])/before["nav"]-1}
            for before, after in zip(rebuilt, rebuilt[1:])]


def _manager_facts(identity, artifacts, at):
    _need(identity.get("manager_tenures"), "official_manager_tenure_missing")
    proofs = identity.get("manager_evidence_proofs")
    _need(proofs, "manager_complete_original_proof_missing")
    rebuilt_identity = {key: value for key, value in identity.items()
                        if key not in ("manager_evidence_proofs", "manager_tenures")}
    rebuilt_proofs = []
    for supplied in proofs:
        _need(supplied.get("document_ref"), "manager_complete_original_document_missing")
        document = _document(supplied["document_ref"], artifacts, at)
        rebuilt = manager_evidence.build_proof(document, identity, supplied["document_ref"])
        require(rebuilt == supplied, "Manager proof differs from independently rebuilt complete original")
        disclosed = rebuilt["coverage"]["through_date"]
        if disclosed:
            require(disclosed <= instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                    "Future manager disclosure as-of date")
        rebuilt_proofs.append(rebuilt)
        rebuilt_identity = manager_evidence.merge(rebuilt_identity, rebuilt)
    require(rebuilt_identity.get("manager_tenures") == identity["manager_tenures"],
            "Manager tenure differs from complete original proof")
    _need(any(proof["coverage"]["status"] == "complete" for proof in rebuilt_proofs),
          "manager_coverage_unknown:"+",".join(sorted({gap for proof in rebuilt_proofs for gap in proof["coverage"]["gaps"]})))
    facts = []
    for proof in rebuilt_proofs:
        for row in proof["appointments"]:
            facts.append({**{key: row[key] for key in ("name", "start_date", "end_date")},
                          "disclosed_through_date": proof["coverage"]["through_date"],
                          "endpoint_semantics": proof["coverage"]["endpoint_semantics"],
                          "proof_id": proof["document_id"]})
    return {"appointments": facts, "proofs": rebuilt_proofs,
            "evidence_refs": [ref for proof in rebuilt_proofs for ref in proof["evidence_refs"]],
            "distributor_current_manager_names": identity.get("manager_names"),
            "attribution_scope": "disclosed_complete_joint_management_regimes_not_individual_causal_skill"}


def _aligned(rows, series):
    result = []
    for row in rows:
        entries = [value["rows"].get(row["date"]) for value in series]
        _need(all(entry and entry["previous_date"] == row["previous_date"] for entry in entries),
              "quality_series_missing_or_mismatched_return_period")
        result.append([row["return"]]+[entry["return"] for entry in entries])
    return result


def _indices(count, policy, rng):
    import numpy as np
    length = policy["block_length"]
    starts = rng.integers(0, count-length+1, size=math.ceil(count/length))
    return np.concatenate([np.arange(start, start+length) for start in starts])[:count]


def _interval(values, policy):
    import numpy as np
    tail = (1-policy["confidence"])/2
    return [float(value) for value in np.quantile(values, [tail, 1-tail])]


def _ols(matrix, roles):
    import numpy as np
    rf = matrix[:, -1]
    y = matrix[:, 0]-rf
    factors = matrix[:, 1:-1].copy()
    for index, role in enumerate(roles):
        if role == "equity_market":
            factors[:, index] -= rf
    x = np.column_stack([np.ones(len(y)), factors])
    coefficients, _, rank, _ = np.linalg.lstsq(x, y, rcond=None)
    _need(rank == x.shape[1] and len(y) > rank, "factor_design_rank_or_degrees_of_freedom_insufficient")
    residual = y-x@coefficients
    return coefficients, float(np.sqrt(np.dot(residual, residual)/(len(y)-rank)))


def _team_regime(facts, day):
    """A conflict-free team component within source-declared complete coverage."""
    candidates = []
    for proof in facts["proofs"]:
        coverage = proof["coverage"]
        if coverage["status"] != "complete" or not coverage["from_date"] <= day <= coverage["through_date"]:
            continue
        appointments = proof["appointments"]
        exclusive = coverage["endpoint_semantics"] == "start_inclusive_end_exclusive"
        active = [row for row in appointments if row["start_date"] <= day and
                  (row["end_date"] is None or (day < row["end_date"] if exclusive else day <= row["end_date"]))]
        if not active:
            continue
        # The source explicitly determines whether the departure date belongs
        # to the old team. No end-date convention is supplied by the caller.
        boundaries = {row["start_date"] for row in appointments}
        boundaries.update(row["end_date"] if exclusive else
                          (dt.date.fromisoformat(row["end_date"])+dt.timedelta(days=1)).isoformat()
                          for row in appointments if row["end_date"] is not None)
        begin = max(coverage["from_date"], max(boundary for boundary in boundaries if boundary <= day))
        following = sorted(boundary for boundary in boundaries if boundary > day)
        end = min(coverage["through_date"], (dt.date.fromisoformat(following[0])-dt.timedelta(days=1)).isoformat()) if following else coverage["through_date"]
        team = sorted([{key: row[key] for key in ("name", "start_date", "end_date")} for row in active],
                      key=lambda row: (row["name"], row["start_date"]))
        if len({row["name"] for row in team}) != len(team):
            return {"status": "unknown", "reason": "conflicting_same_manager_appointments", "managers": []}
        candidates.append({"status": "known", "managers": [row["name"] for row in team],
            "regime_start_date": begin, "regime_end_date": end,
            "team_regime_id": fingerprint({"team": team, "regime_start_date": begin, "regime_end_date": end})})
    if not candidates:
        return {"status": "unknown", "reason": "current_team_not_covered_by_original_disclosure", "managers": []}
    if len({row["team_regime_id"] for row in candidates}) != 1:
        return {"status": "unknown", "reason": "conflicting_complete_manager_disclosures", "managers": []}
    return candidates[0]


def _active(rows, identity, contract, artifacts, at, policy):
    import numpy as np
    facts = _manager_facts(identity, artifacts, at)
    _need(contract["model"] in MODELS and MODELS[contract["model"]][0] == contract["asset_scope"], "factor_model_asset_scope_mismatch")
    roles = [item["role"] for item in contract["factor_series"]]
    _need(len(roles) == len(set(roles)) and set(roles) == MODELS[contract["model"]][1], "missing_or_wrong_asset_factor_roles")
    factors = [_series(item["source"], artifacts, at) for item in contract["factor_series"]]
    rf = _series(contract["risk_free_source"], artifacts, at)
    _need(rf["return_definition"] == "risk_free_return", "risk_free_source_is_not_period_risk_free_return")
    for role, factor in zip(roles, factors):
        _need(factor["factor_role"] == role and factor["asset_scope"] == ("equity" if role == "equity_market" else "bond"),
              "factor_role_or_asset_identity_not_source_supported")
        _need(factor["return_definition"] == ("total_return" if role == "equity_market" else "excess_return"), "factor_return_semantics_mismatch")
    _need(all(value["currency"] == identity["currency"] and value["hedge_policy"] == contract["hedge_policy"] for value in factors+[rf]),
          "factor_currency_or_hedge_scope_mismatch")
    aligned = _aligned(rows, factors+[rf])
    groups, previous_regime = [], None
    for row, values in zip(rows, aligned):
        before = _team_regime(facts, row["previous_date"])
        regime = _team_regime(facts, row["date"])
        team = before["status"] == regime["status"] == "known" and before["team_regime_id"] == regime["team_regime_id"]
        # Date-only appointments do not identify the intraday handover. Every
        # endpoint touched by a NAV interval remains excluded for either team.
        if any(row["previous_date"] <= boundary <= row["date"]
               for tenure in facts["appointments"] for boundary in (tenure["start_date"], tenure["end_date"])
               if boundary is not None):
            team = False
        if team and regime["status"] == "known":
            if regime["team_regime_id"] != previous_regime:
                groups.append((regime, []))
            groups[-1][1].append((row, values))
        previous_regime = regime["team_regime_id"] if team and regime["status"] == "known" else None
    stats = []
    for regime, observations in groups:
        team = regime["managers"]
        identity_fields = {key: regime[key] for key in ("team_regime_id", "regime_start_date", "regime_end_date")}
        if len(observations) < policy["min_observations"]:
            stats.append({**identity_fields, "managers": list(team), "status": "unknown", "reason": "manager_regime_sample_insufficient", "observations": len(observations)})
            continue
        matrix = np.asarray([values for _, values in observations], dtype=float)
        coefficients, residual = _ols(matrix, roles)
        seed_inputs = {"code": identity["code"], "team": list(team),
                       "observations": [{"row": row, "values": values} for row, values in observations]}
        seed = (policy["seed"]+int(fingerprint(seed_inputs)[:8], 16)) % 2**32
        rng = np.random.default_rng(seed)
        estimates, risks = [], []
        for _ in range(policy["bootstrap_resamples"]):
            estimate, risk = _ols(matrix[_indices(len(matrix), policy, rng)], roles)
            estimates.append(estimate)
            risks.append(risk)
        boot = np.asarray(estimates)
        alpha = float(coefficients[0])
        p = float((1+np.sum(np.abs(boot[:, 0]-alpha) >= abs(alpha)))/(len(boot)+1))
        stats.append({**identity_fields, "status": "estimated", "managers": list(team), "observations": len(matrix),
                      "from_date": observations[0][0]["previous_date"], "until_date": observations[-1][0]["date"],
                      "alpha_daily": alpha, "alpha_daily_interval": _interval(boot[:, 0], policy),
                      "alpha_annual_arithmetic": alpha*policy["annualization_sessions"],
                      "betas": {role: float(coefficients[i+1]) for i, role in enumerate(roles)},
                      "beta_intervals": {role: _interval(boot[:, i+1], policy) for i, role in enumerate(roles)},
                      "residual_daily_std": residual, "residual_daily_std_interval": _interval(risks, policy),
                      "alpha_bootstrap_p_approximate": p,
                      "method": "OLS_with_moving_paired_blocks_model_dependent_descriptive_estimate",
                      "inference_assumptions": "approximately_stationary_weak_dependence_within_disclosed_joint_team_regime",
                      "bootstrap_seed": seed, "factor_roles": roles})
    _need(stats and any(item["status"] == "estimated" for item in stats), "no_qualified_manager_regime_statistics")
    current = _team_regime(facts, instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
    roster = facts.get("distributor_current_manager_names")
    if current["status"] == "known" and roster:
        remaining = roster
        for name in current["managers"]:
            remaining = remaining.replace(name, "")
        if any(name not in roster for name in current["managers"]) or re.sub(r"[、,，;；\s]+", "", remaining):
            current = {"status": "unknown", "reason": "current_distributor_and_official_team_conflict", "managers": []}
    matching = [row for row in stats if row["team_regime_id"] == current.get("team_regime_id")]
    if current["status"] == "known":
        current = copy.deepcopy(matching[-1]) if matching else {**current, "status": "unknown",
            "reason": "current_manager_regime_zero_observations", "observations": 0}
    return {"manager_facts": facts, "manager_regimes": stats, "factor_model": contract["model"],
            "current_manager_regime": current,
            "factor_sources": factors, "risk_free_source": rf, "manager_skill_proven": False,
            "date_precision_rule": "exclude_return_intervals_touching_any_appointment_or_departure_date",
            "open_tenure_rule": "until_report_disclosure_as_of_only_not_until_capture_or_current_date"}


def _tracking(matrix, policy):
    import numpy as np
    difference = matrix[:, 0]-matrix[:, 1]
    centered = difference-float(np.mean(difference))
    n, bandwidth = len(centered), min(policy["block_length"]-1, len(centered)-1)
    variance = float(np.dot(centered, centered)/n)
    for lag in range(1, bandwidth+1):
        variance += 2*(1-lag/(bandwidth+1))*float(np.dot(centered[lag:], centered[:-lag])/n)
    require(variance >= -1e-12, "Negative Bartlett long-run tracking variance")
    fund = math.prod(1+float(value) for value in matrix[:, 0])
    benchmark = math.prod(1+float(value) for value in matrix[:, 1])
    return {"tracking_difference_period": fund-benchmark,
            "tracking_difference_annualized": fund**(policy["annualization_sessions"]/n)-benchmark**(policy["annualization_sessions"]/n),
            "tracking_error_daily": float(np.std(difference, ddof=1)),
            "tracking_error_annual_long_run": math.sqrt(max(0., variance)*policy["annualization_sessions"]),
            "long_run_bandwidth": bandwidth}


def _passive(rows, identity, contract, artifacts, at, policy):
    import numpy as np
    tracking = contract["tracking_source"]
    fields(tracking, {"document_ref", "locator"}, label="original passive tracking relation")
    document = _document(tracking["document_ref"], artifacts, at)
    labels = _labels(_blocks(document, tracking["locator"]))
    _need(_label(labels, "基金代码", "Fund Code") == identity["code"] and
          _label(labels, "管理方式", "Management Style") in ("被动", "passive"), "passive_management_and_primary_code_not_source_supported")
    target = _label(labels, "跟踪标的", "Tracking Target")
    benchmark = _series(contract["benchmark_source"], artifacts, at)
    _need(target == benchmark["series_id"] and benchmark["return_definition"] == "total_return"
          and benchmark["currency"] == identity["currency"] and benchmark["hedge_policy"] == contract["hedge_policy"],
          "passive_benchmark_total_return_currency_or_hedge_mismatch")
    matrix = np.asarray(_aligned(rows, [benchmark]), dtype=float)
    _need(len(matrix) >= policy["min_observations"], "tracking_sample_insufficient")
    stats = _tracking(matrix, policy)
    seed = (policy["seed"]+int(fingerprint({"code": identity["code"], "rows": rows, "benchmark": benchmark["raw_sha256"]})[:8], 16)) % 2**32
    rng = np.random.default_rng(seed)
    boot = [_tracking(matrix[_indices(len(matrix), policy, rng)], policy) for _ in range(policy["bootstrap_resamples"])]
    stats["intervals"] = {key: _interval([row[key] for row in boot], policy) for key in stats if key != "long_run_bandwidth"}
    return {"tracking_statistics": stats, "benchmark_id": benchmark["series_id"], "benchmark_source": benchmark,
            "tracking_source": tracking, "bootstrap_seed": seed, "observations": len(rows),
            "annualization_assumption": "equally_spaced_trading_observations_at_declared_sessions_per_year_with_Bartlett_HAC", "manager_skill_proven": False}


def _holm(products, confidence):
    hypotheses = [(code, index, row) for code, product in products.items()
                  for index, row in enumerate(product.get("statistics", {}).get("manager_regimes", [])) if row["status"] == "estimated"]
    hypotheses.sort(key=lambda item: (item[2]["alpha_bootstrap_p_approximate"], item[0], item[1]))
    last, count = 0., len(hypotheses)
    for order, (_, _, row) in enumerate(hypotheses):
        last = max(last, min(1., (count-order)*row["alpha_bootstrap_p_approximate"]))
        row["alpha_holm_p_approximate"] = last
        row["alpha_zero_rejected_in_declared_family"] = last <= 1-confidence
        row["multiple_comparison_family_size"] = count
        row["multiple_comparison_method"] = "Holm_step_down_on_approximate_block_bootstrap_pvalues"
    return count


def evaluate(data_ref, contracts, artifacts, decision_at, policy=None):
    """Rebuild original inputs; unknown quality never means an ineligible fund."""
    data = artifacts.read_json(data_ref)
    fields(data, {"nav", "features", "code_info"}, label="quality audited base data")
    policy = _policy(policy)
    require(type(contracts) is list and len(contracts) <= 100, "Bounded quality contracts required")
    by_code = {}
    for contract in contracts:
        fields(contract, {"code", "mode", "asset_scope", "hedge_policy", "nav_source_ref"},
               {"model", "factor_series", "risk_free_source", "benchmark_source", "tracking_source", "fund_scope_source"}, "quality product contract")
        require(contract["code"] in data["code_info"] and contract["code"] not in by_code, "Unknown or duplicate quality product")
        require(contract["mode"] in ("active", "passive") and contract["asset_scope"] in ("equity", "bond", "mixed"), "Quality product style/asset scope invalid")
        by_code[contract["code"]] = contract
    products = {}
    for code, identity in sorted(data["code_info"].items()):
        row = {"code": code, "readiness": "unknown", "statistics": {}, "required_actions": [],
               "manager_facts": copy.deepcopy(identity.get("manager_tenures")),
               "company_facts": copy.deepcopy(identity.get("company_restrictions")),
               "selection_role": "evaluation_only_no_EN_return_addition", "manager_skill_proven": False}
        products[code] = row
        contract = by_code.get(code)
        try:
            _need(contract, "source_quality_contract_missing")
            scope = _fund_scope(contract, identity, artifacts, decision_at)
            row["source_product_scope"] = scope
            rows = _nav(data, contract, artifacts, decision_at)[-policy["lookback_sessions"]:]
            _need(len(rows) >= policy["min_observations"], "quality_sample_insufficient")
            if contract["mode"] == "active":
                _need(all(key in contract for key in ("model", "factor_series", "risk_free_source")), "source_active_factor_contract_incomplete")
                stats = _active(rows, identity, contract, artifacts, decision_at, policy)
            else:
                _need(all(key in contract for key in ("benchmark_source", "tracking_source")), "source_passive_contract_incomplete")
                stats = _passive(rows, identity, contract, artifacts, decision_at, policy)
            row.update(readiness="descriptive_estimate", statistics=stats, manager_facts=stats.get("manager_facts", row["manager_facts"]),
                       return_basis="NAV_after_operating_expenses_cash_distribution_reinvestment_before_investor_dealing_fees",
                       operating_fee_subtracted_again=False)
        except QualityUnavailable as exc:
            row["required_actions"].append({"action": "obtain_original_quality_evidence", "reason": str(exc), "code": code})
    family = _holm(products, policy["confidence"])
    estimated = [row for row in products.values() if row["readiness"] == "descriptive_estimate"]
    peers = []
    for index, left in enumerate(estimated):
        for right in estimated[index+1:]:
            a, b = by_code[left["code"]], by_code[right["code"]]
            if a["mode"] == b["mode"] and a["asset_scope"] == b["asset_scope"] and a["hedge_policy"] == b["hedge_policy"]:
                same = a["model"] == b["model"] if a["mode"] == "active" else left["statistics"]["benchmark_id"] == right["statistics"]["benchmark_id"]
                if same and data["code_info"][left["code"]]["currency"] == data["code_info"][right["code"]]["currency"]:
                    peers.append({"codes": [left["code"], right["code"]], "mode": a["mode"], "scope": "descriptive_only_no_performance_rank_or_causal_skill_claim"})
    value = {"schema_version": 4, "kind": "fund_quality", "status": "evaluated" if len(estimated) == len(products) and products else "partial",
             "decision_at": decision_at, "data_ref": data_ref, "data_hash": fingerprint(data), "contracts_hash": fingerprint(contracts),
             "policy": policy, "products": products, "peer_comparisons": peers, "alpha_family_size": family,
             "selection_role": "evaluation_only_no_EN_return_addition", "scope": "retrospective_source_vintage_descriptive_quality_not_forecast_qualification",
             "model_guarantee": False, "operating_fee_double_counted": False, "references": REFERENCES}
    value["quality_hash"] = fingerprint(value)
    return value


def validate(value, data_ref, contracts, artifacts, decision_at, policy=None):
    expected = evaluate(data_ref, contracts, artifacts, decision_at, policy)
    require(value == expected, "Fund quality differs from independently rebuilt sources and statistics")
    return {"status": "passed", "scope": expected["scope"], "quality_hash": expected["quality_hash"],
            "readiness": {"trade_ready": False}, "checks": ["original_source_reextraction", "NAV_distribution_rebuild", "style_factor_scope",
                "disclosed_joint_manager_regimes", "paired_block_bootstrap", "finite_family_Holm", "no_operating_fee_double_count"]}


QUALITY_FEATURE_NAMES = ["quality_alpha_daily", "quality_residual_daily_std", "quality_tracking_difference",
    "quality_tracking_error", "quality_observations", "quality_team_tenure_days", "quality_manager_change",
    "quality_source_age_days", "quality_active_estimate_available", "quality_passive_estimate_available"]


def _source_quality_contracts(contracts, data, artifacts):
    """Read original evidence clocks; caller flags/timestamps cannot qualify a vintage."""
    result = []
    def document_refs(value):
        if isinstance(value, dict):
            if "document_ref" in value:
                yield value["document_ref"]
            for item in value.values():
                yield from document_refs(item)
        elif isinstance(value, list):
            for item in value:
                yield from document_refs(item)
    for supplied in contracts:
        vintages = supplied.get("source_vintages", [supplied])
        for original in vintages:
            contract = copy.deepcopy(original)
            identity_ref = contract.pop("identity_ref", None)
            contract.pop("source_vintages", None)
            identity = artifacts.read_json(identity_ref) if identity_ref else data["code_info"][contract["code"]]
            require(identity["code"] == contract["code"], "Quality identity vintage refers to another product")
            nav = artifacts.read_json(contract["nav_source_ref"])
            source_fetch.validate_capture_provenance(nav["capture"])
            fund_universe.capture_text(nav, artifacts)
            refs = list(document_refs(contract)) + list(document_refs(identity.get("manager_tenures", []))) + list(document_refs(identity.get("manager_evidence_proofs", [])))
            refs = list({fingerprint(ref): ref for ref in refs}.values())
            clocks = [nav["capture"]["retrieved_at"]]
            for ref in refs:
                document = source_documents.read_extracted_document(ref, artifacts)
                clocks.append(document["retrieved_at"])
            result.append({"contract": contract, "identity": identity, "known_at": max(clocks, key=instant),
                           "source_refs": [contract["nav_source_ref"], *refs]})
    return result


def prepare_source_quality(data_ref, contracts, artifacts, decision_at, policy=None):
    """Source-as-of feature panel; current evaluation is never backfilled into history."""
    data = artifacts.read_json(data_ref)
    settings = _policy(policy)
    vintages = _source_quality_contracts(contracts, data, artifacts)
    selected = {}
    for vintage in vintages:
        code = vintage["contract"]["code"]
        if instant(vintage["known_at"]) <= instant(decision_at):
            if code not in selected or instant(vintage["known_at"]) > instant(selected[code]["known_at"]):
                selected[code] = vintage
    current_data = copy.deepcopy(data)
    for code, vintage in selected.items():
        current_data["code_info"][code] = vintage["identity"]
    current = evaluate(artifacts.put_json(current_data), [item["contract"] for item in selected.values()],
                       artifacts, decision_at, settings)
    panel, gaps = [], []
    def feature_row(code, stamp, vintage, evaluation):
        product = evaluation["products"][code]
        if product["readiness"] != "descriptive_estimate":
            return None
        stats = product["statistics"]
        regimes = stats.get("manager_regimes", [])
        regime = stats["current_manager_regime"] if vintage["contract"]["mode"] == "active" else {}
        # A prior team's alpha is not a feature of an unqualified new team.
        if regime and regime["status"] != "estimated":
            return None
        tracking = stats.get("tracking_statistics", {})
        values = [regime.get("alpha_daily", 0.), regime.get("residual_daily_std", 0.),
            tracking.get("tracking_difference_period", 0.), tracking.get("tracking_error_daily", 0.),
            regime.get("observations", stats.get("observations", 0)),
            (instant(stamp).astimezone(ZoneInfo("Asia/Shanghai")).date()-dt.date.fromisoformat(regime["regime_start_date"])).days if regime else 0,
            max(0, len(regimes)-1), (instant(stamp)-instant(vintage["known_at"])).total_seconds()/86400,
            int(bool(regime)), int(bool(tracking))]
        row = {"code": code, "decision_at": stamp, "values": values, "source_refs": vintage["source_refs"],
               "source_known_at": vintage["known_at"], "team_regime": regime.get("managers", []),
               "team_regime_id": regime.get("team_regime_id"),
               "quality_evaluation_ref": artifacts.put_json(evaluation)}
        row["feature_hash"] = fingerprint(row)
        return row
    origins = sorted({row["date"] for rows in data["nav"].values() for row in rows})
    for code in sorted(data["code_info"]):
        available = [item for item in vintages if item["contract"]["code"] == code]
        for day in origins:
            stamp = day + "T00:00:00+08:00"
            if instant(stamp) > instant(decision_at):
                continue
            old = [item for item in available if instant(item["known_at"]) < instant(stamp)]
            if not old:
                continue
            vintage = max(old, key=lambda item: instant(item["known_at"]))
            raw_nav = artifacts.read_json(vintage["contract"]["nav_source_ref"])
            source_text = fund_universe.capture_text(raw_nav, artifacts)
            history, _ = research_data.parse_nav(source_text, code,
                dt.date.fromisoformat(min(row["date"] for row in data["nav"][code])),
                dt.date.fromisoformat(day)-dt.timedelta(days=1), {})
            sliced = {"nav": {code: history},
                      "features": {code: data["features"].get(code, [])}, "code_info": {code: vintage["identity"]}}
            if len(sliced["nav"][code]) < 2:
                continue
            evaluation = evaluate(artifacts.put_json(sliced), [vintage["contract"]], artifacts, stamp, settings)
            row = feature_row(code, stamp, vintage, evaluation)
            if row:
                panel.append(row)
        if not any(row["code"] == code for row in panel):
            gaps.append({"code": code, "action": "supply_original_quality_vintages_before_training_origins",
                         "scope": "quality_on_evidence_only_not_off_price_model"})
        if code in selected:
            row = feature_row(code, decision_at, selected[code], current)
            if row:
                panel.append(row)
            elif current["products"][code].get("statistics", {}).get("current_manager_regime", {}).get("status") == "unknown":
                gaps.append({"code": code, "action": "obtain_current_team_quality_evidence",
                             "reason": current["products"][code]["statistics"]["current_manager_regime"]["reason"],
                             "scope": "quality_on_evidence_only_not_off_price_model"})
    excluded, reasons = [], {}
    for code, identity in data["code_info"].items():
        hard = [row for row in identity.get("company_restrictions") or []
                if row.get("effect") in ("subscription_prohibited", "operation_suspended")]
        if hard:
            excluded.append(code);reasons[code] = ["source_disclosed_company_restriction"]
    result = {"schema_version": 4, "current_evaluation": current, "feature_names": QUALITY_FEATURE_NAMES,
              "feature_panel": panel, "source_gaps": gaps,
              "admission": {"excluded_new_codes": sorted(excluded), "source_reasons": reasons},
              "input_binding": {"data_ref": data_ref, "contracts_hash": fingerprint(contracts),
                                "decision_at": decision_at, "policy_hash": fingerprint(settings)}}
    result["quality_bundle_hash"] = fingerprint(result)
    return result


def bind_quality_features(data, prepared):
    require(prepared["quality_bundle_hash"] == fingerprint({key: value for key, value in prepared.items()
            if key != "quality_bundle_hash"}), "Source quality bundle changed")
    value = copy.deepcopy(data)
    require("quality_source_bundle" not in value, "Quality binding must start at the original model data")
    value["quality_source_bundle"] = copy.deepcopy(prepared)
    return value


def validate_source_quality(value, data_ref, contracts, artifacts, decision_at, policy=None):
    require(value == prepare_source_quality(data_ref, contracts, artifacts, decision_at, policy),
            "Quality source-vintage panel differs from original evidence")
    return {"status": "passed", "scope": "source_asof_quality_panel_not_manager_skill_or_return_uplift",
            "quality_bundle_hash": value["quality_bundle_hash"]}
