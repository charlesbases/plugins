"""Pure contracts for account -> originals -> industries -> peers -> decisions.

These functions do not read files, choose a latest object, fetch sources, repair
producer output, execute trades, or independently certify a supplied proof.
They bind explicit DTOs and independent-validator receipts. Source bytes and
scientific claims are verified by the existing engine's independent gates.
"""
import copy
import datetime as dt
import re
from decimal import Decimal

import contracts as wire

SCHEMA_ID = "investment_business_chain_v1"
SCHEMA_VERSION = 1
STAGES = (
    "account", "news_collection", "news_assessment", "fund_universe",
    "fund_sources", "model", "recommendation", "report",
)
STATUSES = frozenset({"complete", "awaiting_user", "awaiting_analysis", "needs_evidence", "failed"})
QUALIFICATIONS = (
    "source_integrity", "fact_qualification", "point_in_time",
    "predictive_validation", "comparison_scope", "recommendation_qualification",
    "trade_ready",
)
PAYLOAD_KEYS = dict(zip(STAGES, (
    "account_context", "news_material", "assessed_news", "frozen_universe",
    "source_bundle", "model_result", "recommendations", "report",
)))
ACCOUNT_FACTS = ("principal", "loss_tolerance", "cash", "holdings")
MODEL_EXECUTION_STATUSES = frozenset({"not_run", "insufficient_data", "unqualified", "qualified", "failed"})
MODEL_EVIDENCE_FIELDS = (
    "method_result_ref", "bundle_ref", "calculation_ref", "readiness_ref", "family_results_ref", "selection_ref",
)
HASH = re.compile(r"[0-9a-f]{64}")
ENVELOPE_FIELDS = {
    "schema_id", "schema_version", "chain_id", "stage", "status",
    "stage_complete", "investment_goal_achieved", "account_ref", "account_hash",
    "input_output_hash", "output_hash", "payload", "source_refs",
    "operation_receipts", "qualification_receipts", "qualifications",
    "processing_complete", "lineage_refs", "protocol_sha256", "implementation_identity",
}


class ChainContractError(wire.ContractError):
    code = "invalid_chain_contract"


def _require(condition, message):
    if not condition:
        raise ChainContractError(message)


def _object(value, label):
    _require(type(value) is dict, label + " must be an object")
    return value


def _required(value, fields, label):
    _object(value, label)
    _require(set(fields) <= value.keys(), label + ": missing=" + str(sorted(set(fields)-value.keys())))
    return value


def _identity(value, label):
    if type(value) is str:
        _hash(value, label)
    else:
        _object(value, label)
        _require(bool(value), label + " must identify the frozen implementation")
        for name, digest in value.items():
            _text(name, label + " file")
            _hash(digest, label + " file hash")


def _text(value, label):
    _require(type(value) is str and bool(value.strip()), label + " must be nonempty text")
    return value


def _hash(value, label):
    _require(type(value) is str and HASH.fullmatch(value) is not None, label + " must be a SHA256 hash")


def _decimal(value, label, *, positive=False, maximum=None):
    try:
        normalized = wire.decimal_string(value, label, positive=positive)
    except wire.ContractError as exc:
        raise ChainContractError(str(exc)) from exc
    number = Decimal(normalized)
    _require(number >= 0 and (maximum is None or number <= maximum), label + " is outside its permitted range")
    return number


def _instant(value, label):
    try:
        return wire.instant(value)
    except wire.ContractError as exc:
        raise ChainContractError(label + ": " + str(exc)) from exc


def _codes(value, label):
    _require(type(value) is list, label + " must be a complete explicit code list")
    for code in value:
        _text(code, label + " code")
    _require(len(value) == len(set(value)), label + " contains duplicate codes")
    return set(value)


def validate_ref(value, label="source_ref"):
    """Validate a CAS identity without reading its content or trusting its facts."""
    _object(value, label)
    _require({"sha256", "size"} <= value.keys(), label + " requires SHA256 and byte size")
    _hash(value["sha256"], label + ".sha256")
    _require(type(value["size"]) is int and value["size"] >= 0, label + ".size must be a nonnegative integer")
    if "chunks" in value:
        _require(type(value["chunks"]) is list and bool(value["chunks"]), label + " requires content-addressed chunks")
        for item in value["chunks"]:
            validate_ref(item, label + ".chunks")
        _require(sum(item["size"] for item in value["chunks"]) == value["size"], label + " chunk sizes differ")
    else:
        path = _text(value.get("path"), label + ".path")
        normalized = path.replace("\\", "/")
        _require(normalized.startswith("objects/") and ".." not in normalized.split("/")
                 and not normalized.startswith("/"), label + " must identify the immutable object store")
        _require(normalized.rsplit("/", 1)[-1] == value["sha256"], label + " path must identify its SHA256 object")
    return value


def _refs(value, label, *, nonempty=False):
    _require(type(value) is list and (bool(value) or not nonempty), label + " must be a source reference list")
    for item in value:
        validate_ref(item, label)
    _require(len({wire.fingerprint(item) for item in value}) == len(value), label + " contains duplicate references")


def _source_times(value, cutoff):
    """No supplied timestamp/date is silently replaced with the invocation clock."""
    if type(value) is list:
        for item in value:
            _source_times(item, cutoff)
    elif type(value) is dict:
        for key, item in value.items():
            if key in {"retrieved_at", "known_at", "recorded_at", "available_at", "information_cutoff_at"} and item is not None:
                _require(_instant(item, key) <= cutoff, key + " is after the information cutoff")
            elif key == "published_at" and item is not None:
                if type(item) is str:
                    _require(_instant(item, key) <= cutoff, "publication is after the information cutoff")
                elif type(item) is dict:
                    precision = item.get("precision")
                    if precision == "timestamp":
                        _require(_instant(item.get("value"), key) <= cutoff, "publication is after the information cutoff")
                    elif precision == "date":
                        try:
                            day = dt.date.fromisoformat(item.get("value"))
                        except (ValueError, TypeError) as exc:
                            raise ChainContractError("Invalid source publication date") from exc
                        _text(item.get("timezone"), "publication timezone")
                        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                        try:
                            timezone = dt.timezone.utc if item["timezone"] == "UTC" else ZoneInfo(item["timezone"])
                            source_day = cutoff.astimezone(timezone).date()
                        except (ValueError, ZoneInfoNotFoundError) as exc:
                            raise ChainContractError("Invalid source publication timezone") from exc
                        _require(day <= source_day, "publication date is after the information cutoff")
                    else:
                        raise ChainContractError("Source publication must retain its declared precision")
                else:
                    raise ChainContractError("Invalid original publication metadata")
            elif key in {"price_date", "nav_date"} and item is not None:
                try:
                    day = dt.date.fromisoformat(item)
                except (ValueError, TypeError) as exc:
                    raise ChainContractError("Invalid actual source price date") from exc
                # A date without a declared timezone is an interval, not a
                # guessed UTC instant. Reject impossible future civil dates;
                # the independent source gate checks actual availability.
                latest_source_day = cutoff.astimezone(dt.timezone(dt.timedelta(hours=14))).date()
                _require(day <= latest_source_day, "Source price date is after the information cutoff")
            elif key in {"price_date_inferred", "price_date_assumed", "publication_assumed", "future_guaranteed"}:
                _require(item is False, "Invented dates and guaranteed future utility are forbidden")
            else:
                _source_times(item, cutoff)


def _binding(value):
    return {name: value[name] for name in (
        "chain_id", "stage", "account_ref", "account_hash", "input_output_hash", "output_hash",
        "protocol_sha256", "implementation_identity",
    )}


def _receipts(value):
    _require(type(value["operation_receipts"]) is list, "operation_receipts must be a list")
    ids = set()
    for receipt in value["operation_receipts"]:
        _object(receipt, "operation receipt")
        identity = _text(receipt.get("operation_id"), "operation_id")
        _require(identity not in ids, "Duplicate operation receipt")
        ids.add(identity)
        _text(receipt.get("operation"), "operation name")
        _require(receipt.get("status") in {"completed", "partial", "failed"}, "Invalid operation receipt status")
        if "result_hash" in receipt:
            _hash(receipt["result_hash"], "operation result_hash")
        if "source_ref" in receipt:
            validate_ref(receipt["source_ref"], "operation source_ref")
        if "engine_refs" in receipt:
            _refs(receipt["engine_refs"], "operation engine_refs")
    _require(type(value["qualification_receipts"]) is list, "qualification_receipts must be a list")
    passed = set()
    for receipt in value["qualification_receipts"]:
        _object(receipt, "qualification receipt")
        _require(receipt.get("status") == "passed" and receipt.get("independent") is True,
                 "Qualification requires a passed independent-validator receipt")
        _require(_text(receipt.get("operation_id"), "qualification operation_id") in ids,
                 "Qualification receipt lacks its concrete operation receipt")
        operation = next(item for item in value["operation_receipts"] if item["operation_id"] == receipt["operation_id"])
        _require(operation["status"] == "completed", "Failed or partial operation cannot qualify a claim")
        _text(receipt.get("validator_scope"), "validator_scope")
        _text(receipt.get("validator_id"), "concrete independent validator_id")
        validate_ref(receipt.get("source_ref"), "qualification source_ref")
        _require(all(receipt.get(key) == item for key, item in _binding(value).items()),
                 "Qualification receipt belongs to another chain, stage, account, input or output")
        claims = receipt.get("qualifications")
        _require(type(claims) is list and bool(claims) and len(claims) == len(set(claims))
                 and set(claims) <= set(QUALIFICATIONS), "Qualification receipt requires concrete known claims")
        passed.update(claims)
    claims = value["qualifications"]
    _require(type(claims) is dict and set(claims) == set(QUALIFICATIONS), "Every qualification flag must be explicit")
    _require(all(type(flag) is bool for flag in claims.values()), "Qualification flags must be booleans")
    _require({key for key, flag in claims.items() if flag} <= passed,
             "Source capture or self-declaration cannot qualify scientific claims")
    # This research chain has no execution authority. A structurally valid DTO
    # must never be treated as independent permission to place a trade.
    _require(claims["trade_ready"] is False, "Business-chain contracts do not grant trade execution authority")


def _account(payload, status):
    account = _required(payload["account_context"], {"currency", "platform", *ACCOUNT_FACTS}, "account_context")
    _require(account.get("currency") == "CNY" and account.get("platform") == "TT", "Account defaults must be TT/CNY")
    missing = [key for key in ACCOUNT_FACTS if account.get(key) is None]
    _require(payload.get("missing_fields") == missing, "missing_fields must contain only the necessary unknown account facts")
    questions = payload.get("questions")
    _require(type(questions) is list, "Account questions must be an explicit list")
    for item in questions:
        _object(item, "account question")
        _require(set(item) == {"field", "question"} and item["field"] in missing, "Account questions may ask only missing necessary facts")
        _text(item["question"], "account question text")
    _require(len(questions) == len(missing) and {item["field"] for item in questions} == set(missing),
             "Known facts must not be asked again; every missing necessary fact requires one question")
    _require((status == "awaiting_user") == bool(missing), "Missing account facts require awaiting_user")
    if account.get("principal") is not None:
        _decimal(account["principal"], "principal", positive=True)
    if account.get("loss_tolerance") is not None:
        _decimal(account["loss_tolerance"], "loss_tolerance", maximum=Decimal(1))
    if account.get("cash") is not None:
        _decimal(account["cash"], "cash")
    if account.get("holdings") is not None:
        holdings = account["holdings"]
        _require(type(holdings) is list, "holdings must be confirmed list, including an explicitly empty list")
        for row in holdings:
            _object(row, "holding")
            _text(row.get("code"), "holding code")
            _decimal(row.get("shares"), "holding shares", positive=True)
        if "risk_codes" in account:
            risk = _codes(account["risk_codes"], "actual account risk_codes")
            _require({row["code"] for row in holdings} <= risk, "Actual account risk inventory cannot omit a holding")
    _require("strategy_spec" not in account and "algorithm_parameters" not in account
             and "user_strategy_spec" not in account, "Account facts must not require user strategy or algorithm parameters")


def _news(payload, status):
    material = _required(payload["news_material"], {"window_start", "cutoff_at", "documents", "coverage"}, "news_material")
    cutoff = _instant(material.get("cutoff_at"), "news cutoff_at")
    _require(_instant(material.get("window_start"), "news window_start") <= cutoff, "Invalid recent news window")
    documents = material.get("documents")
    _require(type(documents) is list, "Original material documents must be a list")
    identifiers = set()
    for row in documents:
        _required(row, {"document_id", "source_ref", "source_id", "url", "title", "published_at", "retrieved_at"}, "original material")
        for key in ("source_id", "url", "title"):
            _text(row[key], "original material " + key)
        document = _text(row.get("document_id"), "document_id")
        identity = (document, row.get("version_id"))
        _require(identity not in identifiers, "Duplicate original document")
        identifiers.add(identity)
        validate_ref(row.get("source_ref"), "original material source_ref")
    coverage = _object(material.get("coverage"), "news coverage")
    for region in ("domestic_source_ids", "international_source_ids", "pending_source_ids"):
        _codes(coverage.get(region), region)
    _require(type(coverage.get("global_coverage_known")) is bool, "Global news denominator must be explicitly known or unknown")
    _require(type(coverage.get("source_scope_complete")) is bool, "Frozen source coverage must be explicit")
    if status == "complete":
        _require(coverage["domestic_source_ids"] and coverage["international_source_ids"], "Complete collection requires domestic and international original sources")
    _source_times(material, cutoff)


def _assessment(payload, previous, status):
    assessment = _object(payload["assessed_news"], "assessed_news")
    _require(assessment.get("material_hash") == previous["output_hash"], "Industry assessment must use this chain's original material")
    links = assessment.get("industry_links")
    _require(type(links) is list, "Industry links must be explicit analyst output")
    original_ids = {row["document_id"] for row in previous["payload"]["news_material"]["documents"]}
    for row in links:
        _required(row, {"industry_id", "mechanism", "document_ids", "facts", "counterevidence", "uncertainties",
                        "direction", "time_horizon", "economic_observations_ref"}, "industry link")
        _text(row.get("industry_id"), "industry_id")
        _text(row.get("mechanism"), "source-grounded economic mechanism")
        ids = _codes(row.get("document_ids"), "assessed original documents")
        _require(ids and ids <= original_ids, "Industry link must cite original documents from the current collection")
        for key in ("facts", "counterevidence", "uncertainties"):
            _require(type(row[key]) is list, "Industry " + key + " must be explicit")
        _text(row["direction"], "industry direction")
        _require(type(row["time_horizon"]) in {str, dict}, "Industry time_horizon must retain its actual declared scope")
        if type(row["time_horizon"]) is str:
            _text(row["time_horizon"], "industry time_horizon")
        else:
            _require(bool(row["time_horizon"]), "Industry time_horizon cannot be empty")
        validate_ref(row["economic_observations_ref"], "industry economic_observations_ref")
    if status == "complete":
        _require(bool(links) or assessment.get("no_material_industry_impact") is True,
                 "Complete analysis requires explicit affected industries or a justified no-impact result")
        if not links:
            _text(assessment.get("reason"), "no-impact reason")


def _exclusions(value, label):
    _require(type(value) is list, label + " must preserve documented exclusions")
    result = []
    for row in value:
        _object(row, label + " entry")
        result.append(_text(row.get("code"), label + " code"))
        _text(row.get("reason"), label + " reason")
        _refs(row.get("source_refs"), label + " source_refs", nonempty=True)
    _require(len(result) == len(set(result)), label + " duplicates a code")
    return set(result)


def _partition(candidates, qualified, unknown, excluded, label):
    _require(not (qualified & unknown or qualified & excluded or unknown & excluded), label + " qualification partitions overlap")
    _require(candidates <= qualified | unknown | excluded, label + " drops candidates from the full peer denominator")


def _universe(payload, previous, account):
    universe = _object(payload["frozen_universe"], "frozen_universe")
    _require(universe.get("currency") == "CNY" and universe.get("platform") == "TT", "Fund universe must remain TT/CNY")
    catalog = _codes(universe.get("catalog_codes"), "catalog_codes")
    validate_ref(universe.get("catalog_ref"), "current full TT catalog_ref")
    holdings = _codes(universe.get("holding_codes"), "holding_codes")
    _require(holdings == {row["code"] for row in account["holdings"]}, "Every actual current holding must be preserved")
    risk = set(account.get("risk_codes", sorted(holdings)))
    _require(_codes(universe.get("risk_codes", sorted(holdings)), "universe risk_codes") == risk,
             "Frozen universe must preserve the actual account risk inventory")
    candidates = _codes(universe.get("candidate_codes"), "candidate_codes")
    unknown = _codes(universe.get("unknown_codes"), "unknown_codes")
    excluded = _exclusions(universe.get("excluded_codes"), "excluded_codes")
    groups = universe.get("peer_groups")
    _require(type(groups) is list, "peer_groups must be the full source-defined comparison denominator")
    members, ids = set(), set()
    industries = {row["industry_id"] for row in previous["payload"]["assessed_news"]["industry_links"]}
    linked_industries = set()
    for row in groups:
        _object(row, "peer group")
        identity = _text(row.get("group_id"), "group_id")
        _require(identity not in ids, "Duplicate comparison group")
        ids.add(identity)
        members |= _codes(row.get("member_codes"), "peer member_codes")
        _require(type(row.get("membership_complete")) is bool, "Unknown peer membership must be retained explicitly")
        _refs(row.get("source_refs"), "peer source_refs", nonempty=True)
        row_industries = _codes(row.get("industry_ids"), "peer industry_ids")
        _require(row_industries <= industries, "Fund peers must follow the current assessed industries")
        linked_industries |= row_industries
    _require(linked_industries == industries, "Affected industries cannot disappear before fund discovery")
    _require(candidates | unknown | excluded == members, "Candidates, unknowns and documented exclusions must preserve every peer member")
    _require(not (candidates & unknown or candidates & excluded or unknown & excluded), "Universe dispositions overlap")
    _require(members - catalog <= unknown, "Peer members outside the full frozen catalog must remain explicitly unknown")


def _sources(payload, previous):
    source = _required(payload["source_bundle"], {"holding_codes", "candidate_codes", "qualified_codes", "unknown_codes",
                       "excluded_codes", "by_code", "cash", "evaluation_at", "historical_availability_refs", "dealing_terms_refs"}, "source_bundle")
    evaluation = _instant(source["evaluation_at"], "source evaluation_at")
    _refs(source["historical_availability_refs"], "historical_availability_refs")
    _refs(source["dealing_terms_refs"], "dealing_terms_refs")
    universe = previous["payload"]["frozen_universe"]
    for key in ("holding_codes", "candidate_codes"):
        _require(_codes(source.get(key), key) == set(universe[key]), "Source bundle changed frozen " + key)
    risk = set(payload["account_context"].get("risk_codes", source["holding_codes"]))
    _require(_codes(source.get("risk_codes", source["holding_codes"]), "source risk_codes") == risk,
             "Source bundle changed the actual account risk inventory")
    required = risk | set(source["holding_codes"]) | set(source["candidate_codes"]) | set(universe["unknown_codes"])
    qualified = _codes(source.get("qualified_codes"), "qualified_codes")
    unknown = _codes(source.get("unknown_codes"), "unknown_codes")
    excluded = _exclusions(source.get("excluded_codes"), "excluded_codes")
    _partition(required, qualified, unknown, excluded, "source bundle")
    _require(qualified | unknown | excluded == required | {row["code"] for row in universe["excluded_codes"]},
             "Source dispositions changed the frozen H and peer denominator")
    prior_exclusions = {row["code"]: row for row in universe["excluded_codes"]}
    exclusions = {row["code"]: row for row in source["excluded_codes"]}
    _require(all(exclusions.get(code) == row for code, row in prior_exclusions.items()), "Frozen exclusions must not disappear or change")
    by_code = _object(source.get("by_code"), "source by_code")
    _require(set(by_code) == qualified | unknown | excluded, "Every H and peer subject requires an explicit source disposition")
    for code, row in by_code.items():
        _object(row, "per-code source evidence")
        expected = "qualified" if code in qualified else "unknown" if code in unknown else "excluded"
        _require(row.get("status") == expected, "Per-code source status differs from the full disposition")
        _refs(row.get("source_refs"), "per-code source_refs", nonempty=expected == "qualified")
        if "engine_refs" in row:
            _refs(row["engine_refs"], "per-code engine_refs")
        if expected != "qualified":
            _text(row.get("reason"), "unknown/excluded source reason")
    cash = _object(source.get("cash"), "cash evidence")
    _require(cash.get("currency") == "CNY", "Cash is a mandatory CNY comparison asset")
    _decimal(cash.get("amount"), "cash amount")
    _require(cash["amount"] == payload["account_context"]["cash"], "Cash evidence must preserve the actual current account cash")
    _refs(cash.get("source_refs"), "cash source_refs", nonempty=True)
    _source_times(source, evaluation)


def _model(payload, previous, status):
    model = _required(payload["model_result"], {"holding_codes", "candidate_codes", "unknown_codes", "excluded_codes",
        "compared_codes", "cash_compared", "method", "execution_status", "model_fit_executed", "comparison_gaps", "evidence_refs"}, "model_result")
    source = previous["payload"]["source_bundle"]
    for key in ("holding_codes", "candidate_codes", "unknown_codes", "excluded_codes"):
        _require(model.get(key) == source[key], "Model dropped or changed frozen " + key)
    risk = set(payload["account_context"].get("risk_codes", source["holding_codes"]))
    _require(_codes(model.get("risk_codes", model["holding_codes"]), "model risk_codes") == risk,
             "Model must preserve all actual holdings, orders and in-transit risk codes")
    compared = _codes(model.get("compared_codes"), "compared_codes")
    _require(type(model["model_fit_executed"]) is bool and type(model["cash_compared"]) is bool, "Actual model execution and cash comparison flags must be booleans")
    _require(model["execution_status"] in MODEL_EXECUTION_STATUSES, "Unknown actual model execution_status")
    _require(type(model["comparison_gaps"]) is list, "Model comparison_gaps must be explicit")
    evidence = _required(model["evidence_refs"], MODEL_EVIDENCE_FIELDS, "actual model evidence_refs")
    for key in MODEL_EVIDENCE_FIELDS:
        if evidence[key] is not None:
            validate_ref(evidence[key], "actual model " + key)
    _require(compared <= set(source["qualified_codes"]), "Scientifically compared subjects cannot exceed the source-qualified scope")
    if model.get("model_fit_executed") is False:
        _require(not compared, "Observation without model execution cannot claim scientifically compared codes")
        _require(model.get("cash_compared") is False, "Observation without model execution cannot claim a modeled cash comparison")
        _require(model["execution_status"] in {"not_run", "insufficient_data", "failed"}, "Unexecuted model cannot claim actual qualified or unqualified fit")
    elif model["execution_status"] == "qualified":
        _require(compared == set(source["qualified_codes"]), "Model must compare every source-qualified H and peer fund")
    if compared or model["cash_compared"]:
        _require(evidence["calculation_ref"] is not None, "Scientific comparison requires an actual calculation reference")
    method = _object(model.get("method"), "scientific method")
    _require(method.get("family") == "elastic_net", "Existing Elastic Net model must be retained")
    for key in ("chronological", "independent_calibration", "costs", "cvar"):
        _require(method.get(key) is True, "Scientific model requires " + key)
    _require(any(evidence[key] is not None for key in MODEL_EVIDENCE_FIELDS) or bool(model["comparison_gaps"]),
             "Model requires actual engine evidence or concrete missing-evidence diagnostics")
    if "engine_refs" in model:
        _refs(model["engine_refs"], "model engine_refs", nonempty=True)
    if status == "complete":
        _require(model["execution_status"] == "qualified" and model["model_fit_executed"], "Scientific model business completion requires an actual qualified evaluation")
        _require(risk <= compared, "Complete model must retain scientifically compared actual held and pending risk")
        _require(model.get("cash_compared") is True, "Model must compare the mandatory current cash baseline")
        _require(all(evidence[key] is not None for key in ("calculation_ref", "readiness_ref", "family_results_ref", "selection_ref")),
                 "Qualified model requires actual calculation, readiness, family and selection evidence")


def _recommendation(payload, previous, status):
    recommendation = _object(payload["recommendations"], "recommendations")
    model = previous["payload"]["model_result"]
    _require(recommendation.get("model_hash") == previous["output_hash"], "Recommendations must use the current bound scientific result")
    for key in ("holding_codes", "candidate_codes"):
        _require(recommendation.get(key) == model[key], "Recommendations changed frozen " + key)
    _text(recommendation.get("scope"), "explicit recommendation comparison scope")
    actions = recommendation.get("actions")
    _require(type(actions) is list, "Actions must explicitly describe changes to current holdings")
    codes = set()
    for row in actions:
        _required(row, {"code", "action", "reason", "amount_CNY", "shares", "fees_CNY", "evidence_refs", "qualification"}, "holding recommendation")
        code = _text(row.get("code"), "recommendation code")
        _require(code not in codes, "Duplicate recommendation subject")
        codes.add(code)
        _require(code in set(model["holding_codes"]) | set(model["compared_codes"]), "Recommendation subject was not held or scientifically compared")
        _require(row.get("action") in {"increase", "reduce", "hold"}, "Recommendation must explicitly increase, reduce or hold")
        _text(row.get("reason"), "evidence-grounded recommendation reason")
        for field in ("amount_CNY", "shares", "fees_CNY"):
            if row[field] is not None:
                _decimal(row[field], "recommendation " + field)
        _refs(row["evidence_refs"], "recommendation evidence_refs")
        verdict = row["qualification"]
        _require(verdict in {"qualified", "unqualified", "unknown"}, "Recommendation qualification must retain its concrete verdict")
        if verdict == "unknown":
            _require(all(row[field] is None for field in ("amount_CNY", "shares", "fees_CNY")),
                     "Unknown recommendation amounts, shares and fees must remain null, not invented zero")
        if row["action"] != "hold":
            _require(code in model["compared_codes"], "Unqualified holdings cannot receive an invented adjustment")
            _require(verdict == "qualified" and all(row[field] is not None for field in ("amount_CNY", "shares", "fees_CNY"))
                     and bool(row["evidence_refs"]), "Actual adjustments require qualified amount, shares, fees and evidence")
        if status == "complete":
            _require(verdict == "qualified", "Recommendation business completion requires every actual action to be independently qualified")
    if status == "complete":
        _require(set(model["holding_codes"]) <= codes, "Every actual current holding requires an explicit recommendation")


def _report(payload, previous, value):
    report = _required(payload["report"], {"conclusion", "recommendations_hash", "model_hash", "coverage", "goal_achieved", "report_markdown", "limitations"}, "report")
    _text(report.get("conclusion"), "final expected conclusion")
    _text(report["report_markdown"], "final readable Chinese report_markdown")
    _require(type(report["limitations"]) is list, "Final report limitations must be explicit")
    _require(report.get("recommendations_hash") == previous["output_hash"], "Report must conclude from this chain's actual recommendations")
    _require(report.get("model_hash") == previous["payload"]["recommendations"]["model_hash"], "Report must retain its current model binding")
    coverage = _object(report.get("coverage"), "final explicit coverage")
    _require(type(coverage.get("source_scope_complete")) is bool
             and type(coverage.get("comparison_scope_complete")) is bool,
             "Final coverage cannot be implied from a downloaded artifact")
    _refs(coverage.get("source_refs"), "final coverage source_refs", nonempty=True)
    _require(type(report.get("goal_achieved")) is bool and report["goal_achieved"] == value["investment_goal_achieved"],
             "Final investment goal must be explicit and agree with its envelope")
    if value["investment_goal_achieved"]:
        _require(value["status"] == "complete" and previous["status"] == "complete", "Partial work cannot become a completed investment goal")
        _require(coverage["source_scope_complete"] and coverage["comparison_scope_complete"], "Unknown coverage prevents a full investment-goal claim")
        _require(all(value["qualifications"][key] for key in QUALIFICATIONS if key != "trade_ready"),
                 "Investment goal requires independently qualified sources, facts, timing, model, peers and recommendations")
        _require(previous["qualifications"]["recommendation_qualification"], "Report goal requires the actual independently qualified recommendation")


def _account_context(previous):
    # The exact account projection is propagated by every adapter in its DTO.
    return previous["payload"].get("account_context")


def make_envelope(stage, chain_id, payload, *, previous=None, account_ref=None,
                  account_hash=None, status="complete", source_refs=None,
                  operation_receipts=None, qualification_receipts=None,
                  qualifications=None, investment_goal_achieved=False,
                  previous_ref=None, protocol_sha256=None, implementation_identity=None,
                  processing_complete=True, allow_partial=False):
    """Construct and check a DTO; qualification flags default explicitly false."""
    if previous is not None:
        if account_ref is None:
            account_ref = previous["account_ref"]
        if account_hash is None:
            account_hash = previous["account_hash"]
        if protocol_sha256 is None:
            protocol_sha256 = previous["protocol_sha256"]
        if implementation_identity is None:
            implementation_identity = previous["implementation_identity"]
        validate_ref(previous_ref, "exact previous envelope reference")
        _require(previous_ref["sha256"] == wire.fingerprint(previous)
                 and previous_ref["size"] == len(wire.canonical_bytes(previous)),
                 "Previous envelope reference does not identify the exact complete upstream output")
        lineage_refs = copy.deepcopy(previous["lineage_refs"]) + [copy.deepcopy(previous_ref)]
    else:
        _require(previous_ref is None, "First account chain accepts no previous reference")
        lineage_refs = []
    value = {
        "schema_id": SCHEMA_ID, "schema_version": SCHEMA_VERSION,
        "chain_id": chain_id, "stage": stage, "status": status,
        "stage_complete": status == "complete",
        "processing_complete": processing_complete,
        "investment_goal_achieved": investment_goal_achieved,
        "lineage_refs": lineage_refs, "protocol_sha256": protocol_sha256,
        "implementation_identity": copy.deepcopy(implementation_identity),
        "account_ref": copy.deepcopy(account_ref), "account_hash": account_hash,
        "input_output_hash": wire.fingerprint(previous) if previous is not None else None,
        "output_hash": wire.fingerprint(payload), "payload": copy.deepcopy(payload),
        "source_refs": copy.deepcopy(source_refs or []),
        "operation_receipts": copy.deepcopy(operation_receipts or []),
        "qualification_receipts": copy.deepcopy(qualification_receipts or []),
        "qualifications": {key: False for key in QUALIFICATIONS},
    }
    if qualifications is not None:
        _object(qualifications, "qualification flags")
        _require(set(qualifications) <= set(QUALIFICATIONS), "Unknown qualification claim")
        value["qualifications"].update(copy.deepcopy(qualifications))
    validate_envelope(value, previous=previous, allow_partial=allow_partial)
    return value


def validate_envelope(value, *, previous=None, expected_account_ref=None,
                      expected_account_hash=None, expected_protocol_sha256=None,
                      expected_implementation_identity=None, allow_partial=False):
    """Check a stage against its exact caller-supplied predecessor, without I/O."""
    _object(value, "business-chain envelope")
    _require(set(value) == ENVELOPE_FIELDS, "Business-chain envelope has missing or unknown fields")
    try:
        wire.canonical_bytes(value)
    except wire.ContractError as exc:
        raise ChainContractError(str(exc)) from exc
    _require(value["schema_id"] == SCHEMA_ID and type(value["schema_version"]) is int
             and value["schema_version"] == SCHEMA_VERSION, "Unsupported business-chain schema")
    _text(value["chain_id"], "chain_id")
    _hash(value["protocol_sha256"], "frozen protocol_sha256")
    _identity(value["implementation_identity"], "frozen implementation_identity")
    _require(type(allow_partial) is bool, "allow_partial must explicitly identify observation delivery")
    stage = value["stage"]
    _require(stage in STAGES, "Unknown business stage")
    status = value["status"]
    _require(status in STATUSES, "Unknown business stage status")
    _require(type(value["stage_complete"]) is bool and value["stage_complete"] == (status == "complete"), "stage_complete differs from the business status")
    _require(type(value["processing_complete"]) is bool, "processing_complete must be an explicit technical-processing flag")
    _require(not value["stage_complete"] or value["processing_complete"], "Business completion requires completed processing")
    _require(type(value["investment_goal_achieved"]) is bool, "investment_goal_achieved must be explicit")
    _require(not value["investment_goal_achieved"] or stage == "report", "Only the final report may claim the investment goal")
    if value["account_ref"] is not None:
        validate_ref(value["account_ref"], "current account_ref")
        _hash(value["account_hash"], "current account_hash")
        _require(value["account_ref"]["sha256"] == value["account_hash"], "account_ref does not bind the entire current account hash")
    else:
        _require(value["account_hash"] is None and stage == "account" and status == "awaiting_user", "A completed or downstream stage must bind the current account")
    if expected_account_ref is not None:
        _require(value["account_ref"] == expected_account_ref, "Envelope does not belong to the expected current account reference")
    if expected_account_hash is not None:
        _require(value["account_hash"] == expected_account_hash, "Envelope does not belong to the expected current account hash")
    if expected_protocol_sha256 is not None:
        _require(value["protocol_sha256"] == expected_protocol_sha256, "Envelope does not belong to the frozen protocol")
    if expected_implementation_identity is not None:
        _require(value["implementation_identity"] == expected_implementation_identity, "Envelope does not belong to the frozen implementation")
    _refs(value["lineage_refs"], "current chain lineage_refs")
    if stage == "account":
        _require(previous is None and value["input_output_hash"] is None, "First account chain accepts no business input")
        _require(not value["lineage_refs"], "First account chain accepts no previous lineage")
    else:
        _object(previous, "exact previous stage envelope")
        _require(previous.get("stage") == STAGES[STAGES.index(stage)-1], "Business stages cannot skip or reorder their upstream inputs")
        _require(previous.get("chain_id") == value["chain_id"], "Previous output belongs to another chain")
        _require(previous.get("account_ref") == value["account_ref"] and previous.get("account_hash") == value["account_hash"], "Current account changed between business stages")
        _require(previous.get("protocol_sha256") == value["protocol_sha256"]
                 and previous.get("implementation_identity") == value["implementation_identity"],
                 "Frozen protocol or implementation changed between business stages")
        _require(value["input_output_hash"] == wire.fingerprint(previous), "Previous output hash differs; latest or old objects cannot substitute for this chain's input")
        _require(previous.get("output_hash") == wire.fingerprint(previous.get("payload")), "Previous payload hash changed")
        upstream_complete = previous.get("status") == "complete" and previous.get("stage_complete") is True
        analysis_ready = (previous.get("stage") != "news_assessment"
                          or previous.get("qualifications", {}).get("fact_qualification") is True)
        observation_partial = (allow_partial and previous.get("stage") != "account" and analysis_ready
                               and previous.get("status") == "needs_evidence" and previous.get("processing_complete") is True)
        _require(upstream_complete or observation_partial, "An incomplete upstream business stage cannot be passed as complete input")
        _require(upstream_complete or not value["stage_complete"] or stage == "report",
                 "Observation partial input cannot become a completed scientific or financial business stage")
        expected_lineage = previous.get("lineage_refs")
        _require(type(expected_lineage) is list and len(value["lineage_refs"]) == len(expected_lineage)+1
                 and value["lineage_refs"][:-1] == expected_lineage, "Current chain lineage changed or omitted an upstream output")
        reference = value["lineage_refs"][-1]
        _require(reference["sha256"] == wire.fingerprint(previous)
                 and reference["size"] == len(wire.canonical_bytes(previous)), "Lineage reference does not identify the exact complete predecessor envelope")
    _require(len(value["lineage_refs"]) == STAGES.index(stage), "Lineage must retain every prior business stage in order")
    _hash(value["output_hash"], "output_hash")
    _require(value["output_hash"] == wire.fingerprint(value["payload"]), "Business payload hash changed")
    payload = _object(value["payload"], "typed business payload")
    _object(payload.get("engine_context"), "explicit engine_context")
    _require(PAYLOAD_KEYS[stage] in payload, "Stage requires its typed " + PAYLOAD_KEYS[stage] + " payload")
    _refs(value["source_refs"], "envelope source_refs")
    _receipts(value)
    if stage == "account":
        _account(payload, status)
    else:
        account = _object(payload.get("account_context"), "propagated account_context")
        _require(account == _account_context(previous), "Account facts must propagate unchanged from the first chain")
        typed = _object(payload[PAYLOAD_KEYS[stage]], "typed " + PAYLOAD_KEYS[stage])
        unavailable = typed.get("unavailable") is True
        if unavailable:
            _require(status != "complete" and not value["investment_goal_achieved"]
                     and not any(value["qualifications"].values()), "Unavailable business input cannot become complete or qualified")
            actions = typed.get("required_actions")
            _require(type(actions) is list and bool(actions), "Unavailable stage requires concrete missing-evidence actions")
            for action in actions:
                _object(action, "unavailable evidence action")
                _text(action.get("action"), "missing-evidence action")
        elif stage == "news_collection":
            _news(payload, status)
        elif stage == "news_assessment":
            _assessment(payload, previous, status)
        elif stage == "fund_universe":
            _universe(payload, previous, account)
        elif stage == "fund_sources":
            _sources(payload, previous)
            if value["stage_complete"]:
                source = payload["source_bundle"]
                risk = set(account.get("risk_codes", source["holding_codes"]))
                _require(risk <= set(source["qualified_codes"]),
                         "Current held risk cannot be excluded from complete source qualification")
                _require(not (set(source["unknown_codes"]) & (risk | set(source["candidate_codes"]))),
                         "Material unresolved current risk or candidate sources prevent source business completion")
        elif stage == "model":
            _model(payload, previous, status)
            if typed.get("model_fit_executed") is False:
                _require(not value["qualifications"]["predictive_validation"], "Unexecuted model cannot claim independent predictive validation")
        elif stage == "recommendation":
            _recommendation(payload, previous, status)
        elif stage == "report":
            _report(payload, previous, value)
    if "engine_refs" in payload:
        _refs(payload["engine_refs"], "payload engine_refs")
    return {"status": "passed", "stage": stage, "stage_complete": value["stage_complete"],
            "processing_complete": value["processing_complete"],
            "investment_goal_achieved": value["investment_goal_achieved"],
            "scope": "DTO_binding_only_independent_evidence_and_scientific_receipts_required",
            "trade_ready": False}


def validate_chain(values, *, require_goal=False, expected_account_ref=None,
                   expected_account_hash=None, expected_protocol_sha256=None,
                   expected_implementation_identity=None, allow_partial=False):
    """Validate the entire explicit prefix, or require all eight qualified steps."""
    _require(type(values) is list and bool(values), "Business chain must be a nonempty explicit stage list")
    _require(len(values) <= len(STAGES), "Duplicate or extra business stages")
    previous = None
    for index, value in enumerate(values):
        _require(type(value) is dict and value.get("stage") == STAGES[index], "Business chain must start with account and preserve stage order")
        validate_envelope(value, previous=previous, expected_account_ref=expected_account_ref,
                          expected_account_hash=expected_account_hash, expected_protocol_sha256=expected_protocol_sha256,
                          expected_implementation_identity=expected_implementation_identity, allow_partial=allow_partial)
        _require(value["lineage_refs"] == [values[next_index]["lineage_refs"][-1] for next_index in range(1, index+1)],
                 "Full chain lineage must preserve each authenticated predecessor reference")
        previous = value
    goal = len(values) == len(STAGES) and values[-1]["investment_goal_achieved"]
    if require_goal:
        _require(goal, "The intended investment goal has not passed all eight business stages")
    if goal:
        _require(all(value["stage_complete"] and value["status"] == "complete" for value in values),
                 "Investment goal requires every actual business postcondition, not eight technical outputs")
        model = values[STAGES.index("model")]
        _require(model["status"] == "complete" and model["qualifications"]["predictive_validation"], "Final goal requires the actual qualified scientific model")
        universe = values[STAGES.index("fund_universe")]["payload"]["frozen_universe"]
        _require(all(row["membership_complete"] for row in universe["peer_groups"]),
                 "Incomplete peer denominator prevents the full investment goal")
        source = values[STAGES.index("fund_sources")]["payload"]["source_bundle"]
        for code in model["payload"]["model_result"]["unknown_codes"]:
            row = source["by_code"][code]
            _refs(row["source_refs"], "unknown comparison eligibility source_refs", nonempty=True)
            _text(row["reason"], "source-backed unknown comparison eligibility explanation")
    return {"status": "passed", "stages_validated": len(values),
            "stage_complete": values[-1]["stage_complete"],
            "investment_goal_achieved": bool(goal), "trade_ready": False,
            "scope": "DTO_binding_only_independent_evidence_and_scientific_receipts_required"}
