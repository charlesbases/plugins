"""Durable orchestration: facts commit independently, decisions publish atomically."""
import copy
import datetime as dt
from decimal import Decimal
from pathlib import Path
import time
import hashlib
import math
import re
from zoneinfo import ZoneInfo

from artifacts import Artifacts
from contracts import (ContractError, ConflictError, EvidenceError, RetryableError, StaleSnapshot,
                       canonical_bytes, fields, fingerprint, instant, parse_request, require, strict_json_loads, utc_now)
from file_io import read_object
from state_store import Store, LeaseLost
import verify


def _validation_runtime(store):
    runtime = getattr(store._local, "validation_runtime", None)
    require(runtime is not None, "Automatic stage validation requires the operation controller")
    return runtime


def _step(store, artifacts, operation_id, name, function, *, inputs, namespaced=False, dependencies=()):
    import stage_validation
    runtime = _validation_runtime(store)
    base = name.split("@", 1)[0]
    if name != base:
        require(name.count("@") == 1 and re.fullmatch(r"[a-f0-9]{64}", name.split("@", 1)[1]), "Invalid typed stage instance")
        require(base in stage_validation.STAGE_NAMES, "Unknown typed stage base")
        runtime.validator_contracts[name] = copy.deepcopy(runtime.validator_contracts[base])
    require(store.require_context().operation_id == operation_id, "Stage belongs to another operation")
    return runtime.run(name, inputs,
        lambda attempt: function(attempt.namespace) if namespaced else function(),
        lambda value, frozen: stage_validation.validate(base, value, frozen, store, artifacts),
        force=runtime.round_no > 1, dependencies=dependencies)


def _numeric_stage_runner(store, unit_id=None):
    def execute(name, inputs, producer, validator):
        runtime = _validation_runtime(store)
        stage = name if unit_id is None else name+"@"+unit_id
        if unit_id is not None:
            require(re.fullmatch(r"[a-f0-9]{64}", unit_id), "Numeric stage requires a context hash")
            runtime.validator_contracts[stage] = copy.deepcopy(runtime.validator_contracts[name])
        return runtime.run(stage, inputs, lambda attempt: producer(), validator,
                           force=runtime.round_no > 1)
    return execute


def _time_inputs(store, operation_id):
    return {"not_before": store.operation(operation_id)["created_at"], "not_after": None}


def _report_time(store, operation_id):
    """Seal the post-collection business clock once, independently of logs."""
    operation = store.operation(operation_id)
    with store.transaction() as conn:
        saved = store.get("decision_clock", operation_id, conn)
        if saved is None:
            saved = {"request_hash": operation["request_hash"], "decision_at": utc_now()}
            store.put("decision_clock", operation_id, saved, conn=conn)
        require(saved["request_hash"] == operation["request_hash"], "Decision clock belongs to a different request")
        require(instant(operation["created_at"]) <= instant(saved["decision_at"]) <= instant(utc_now()),
                "Decision clock is outside the operation lifecycle")
        return saved["decision_at"]


def _market_inputs(prepared, discovery, market, artifacts):
    return {"prepared": prepared, "discovery_ref": artifacts.put_json(discovery),
            "market_record_ref": artifacts.put_json(market["record"]), "decision_at": market["record"]["market_ref"]["observed_at"]}


class UnreconciledConfirmation(ConflictError):
    code = "unreconciled_confirmation"


def feedback(payload, store):
    from contracts import canonical_bytes, validate_feedback_shape
    validate_feedback_shape(payload)
    require(payload.get("account_id", "main") == "main", "Only the actual account accepts user confirmations")
    try:
        return _apply_feedback(payload, store)
    except ValueError as exc:
        kinds = {"buy_fill", "sell_fill", "subscription_confirmed", "external_fill_confirmed"}
        if not any(row["type"] in kinds for row in payload["events"]):
            raise
        import ledger
        identity = "unreconciled-confirmation:" + fingerprint(payload)
        objects = Artifacts(store.base)
        raw_ref = objects.put_bytes(canonical_bytes(payload))
        with store.transaction() as conn:
            prior = store.get("unreconciled_fact", identity, conn)
            if prior is None:
                state = store.get("account", "main", conn) or ledger.initial_state(payload["currency"])
                at = utc_now()
                marker = {"id": identity, "type": "unknown", "sequence": state["sequence"] + 1,
                          "effective_at": at, "known_at": at, "recorded_at": at,
                          "data": {"reason": "Execution confirmation requires reconciliation: " + str(exc)}}
                state = ledger.apply_event(state, marker)
                store.put("unreconciled_fact", identity, {"raw_ref": raw_ref, "reason": str(exc),
                          "financial_projection_applied": False, "recorded_at": at}, conn=conn)
                store.put("ledger_event", identity, marker, conn=conn)
                store.put("ledger_event_owner", identity, {"account_id": "main"}, conn=conn)
                store.put("ledger_event_evidence", identity, [raw_ref], conn=conn)
                store.put("account", "main", state, immutable=False, conn=conn)
        failure = UnreconciledConfirmation("Execution evidence was preserved and account exposure requires reconciliation: " + str(exc))
        failure.required_actions = [{"unknown_id": identity,
            "action": "Provide corrected receipt facts or a complete confirmed account snapshot, then resolve this recorded unknown."}]
        raise failure from exc


def _apply_feedback(payload, store):
    import ledger
    from contracts import validate_feedback_shape
    validate_feedback_shape(payload)
    fields(payload, {"currency", "events"}, {"account_id", "evidence"}, "feedback")
    account_id = payload.get("account_id", "main")
    require(account_id == "main", "Feedback only changes the actual account; trials own their projections")
    require(type(payload["events"]) is list and 0 < len(payload["events"]) <= 1000, "Bounded event batch required")
    require(all(type(event) is dict for event in payload["events"]), "Feedback events must be objects")
    import allocation_runtime
    allocation_runtime.ensure_runtime(store.root)
    with store.transaction() as conn:
        state = store.get("account", account_id, conn) or ledger.initial_state(payload["currency"])
        require(state["currency"] == payload["currency"], "Account currency mismatch")
        added, repeated = [], []
        for event in payload["events"]:
            fields(event, {"id", "type", "effective_at", "known_at", "recorded_at", "data"}, label="feedback event")
            require(instant(event["recorded_at"]) <= instant(utc_now()), "Feedback cannot claim a future recording time")
            alias = store.get("ledger_event_alias", event["id"], conn)
            if alias:
                require(alias["account_id"] == account_id and alias["input_hash"] == fingerprint(event),
                        "Repeated event alias changed content")
                repeated.append(event["id"])
                continue
            prior = store.get("ledger_event", event["id"], conn)
            if prior:
                original = {k: v for k, v in prior.items() if k != "sequence"}
                require(store.get("ledger_event_owner", event["id"], conn) == {"account_id": account_id},
                        "Event belongs to another account")
                if fingerprint(original) != fingerprint(event):
                    raise ConflictError("Repeated event id carries changed content")
                repeated.append(event["id"])
                continue
            applied = {**event, "sequence": state["sequence"] + 1}
            ledger._event(applied)
            fact = ledger.financial_identity(applied)
            fact_key = account_id + ":" + fact["key"] if fact else None
            previous_fact = store.get("financial_fact", fact_key, conn) if fact else None
            if previous_fact:
                if previous_fact["content_hash"] != fact["content_hash"]:
                    raise ConflictError("Financial transaction identity has conflicting content")
                store.put("ledger_event_alias", event["id"], {"account_id": account_id,
                          "input_hash": fingerprint(event), "canonical_event_id": previous_fact["event_id"]}, conn=conn)
                repeated.append(event["id"])
                continue
            verify.references(event, Artifacts(store.base))
            if event["type"] in ("resolve_unknown", "lot_metadata_confirmed", "external_fill_confirmed", "account_snapshot_confirmed"):
                reference = event["data"].get("evidence_ref")
                require(type(reference) is dict and {"sha256", "size"} <= reference.keys()
                        and ("path" in reference or "chunks" in reference), "Resolution requires an artifact reference")
                objects = Artifacts(store.base)
                objects.read(reference)
                verify.references(reference, objects)
            try:
                state = ledger.apply_event(state, applied)
            except ledger.RebuildRequired:
                history = [item for key, item in store.scan("ledger_event", conn)
                           if store.get("ledger_event_owner", key, conn) == {"account_id": account_id}]
                state = ledger.rebuild(ledger.initial_state(payload["currency"]), history + [applied], utc_now())
            store.put("ledger_event", event["id"], applied, conn=conn)
            store.put("ledger_event_owner", event["id"], {"account_id": account_id}, conn=conn)
            if fact:
                store.put("financial_fact", fact_key, {"content_hash": fact["content_hash"], "event_id": event["id"]}, conn=conn)
            evidence = payload.get("evidence", {}).get(event["id"], [])
            verify.references(evidence, Artifacts(store.base))
            store.put("ledger_event_evidence", event["id"], evidence, conn=conn)
            added.append(event["id"])
        store.put("account", account_id, state, immutable=False, conn=conn)
    return {"status": "recorded", "account_id": account_id, "account_hash": fingerprint(state),
            "sequence": state["sequence"], "added": added, "repeated": repeated}


def _research(payload, store, operation_id, *, identity_snapshot_ref):
    import research
    fields(payload, {"codes", "start", "end", "horizons", "lookback"}, {"timeout"}, "research preparation")
    return research.prepare(store.root, store.plan, payload["codes"], payload["start"], payload["end"],
                            payload["horizons"], payload["lookback"], timeout=payload.get("timeout", 20),
                            operation_id=operation_id, identity_snapshot_ref=identity_snapshot_ref, store=store)


def _news(payload, store, artifacts, operation_id):
    import allocation_runtime
    allocation_runtime.ensure_runtime(store.root)
    import news
    run, _ = news.run_policy(payload.get("run_id"), store, artifacts)
    require(payload["cutoff_at"] == run["publish_cutoff"], "News cutoff changed within an analysis")
    return news.collect(payload, store, artifacts, operation_id)


def _open_analysis(payload, store, artifacts, run_id):
    fields(payload, {"news_policy"}, {"account_id"}, "analysis start")
    policy = payload["news_policy"]
    fields(policy, {"mode", "sources", "max_age_seconds", "required_source_groups"}, label="news policy")
    import news
    account_id = payload.get("account_id", "main")
    previous = store.get("analysis_run", run_id)
    if previous is not None:
        require(previous["news_policy"] == policy and previous["account_id"] == account_id, "Analysis identity changed")
        news.run_policy(run_id, store, artifacts)
        return previous
    import source_fetch
    registry = source_fetch.load_registry()
    sources = {name for name, rule in registry["sources"].items() if rule["purpose"] == "news"}
    require(policy["mode"] == "sealed_inputs" and type(policy["sources"]) is list
            and bool(policy["sources"]) and len(set(policy["sources"])) == len(policy["sources"])
            and set(policy["sources"]) <= sources, "Use registered free original news sources")
    require(type(policy["max_age_seconds"]) is int and policy["max_age_seconds"] > 0, "Invalid news freshness policy")
    groups = news._source_groups(policy["required_source_groups"], registry)
    require({name for members in groups.values() for name in members} <= set(policy["sources"]),
            "Required source group is outside frozen news sources")
    account = _analysis_account(store, account_id)
    at = store.operation(store.require_context().operation_id)["created_at"]
    value = {"schema_version": 4, "run_id": run_id, "started_at": at, "publish_cutoff": at,
             "account_id": account_id, "news_policy": policy, "news_policy_hash": fingerprint(policy),
             "source_registry_hash": fingerprint(registry), "registry_snapshot_ref": artifacts.put_json(registry),
             "initial_account_hash": fingerprint(account) if account else None,
             "free_news_only": True}
    store.put("analysis_run", run_id, value)
    return value


def _bound_analysis(run_id, review, spec, store, artifacts, account_id="main"):
    run = store.get("analysis_run", run_id)
    require(run is not None and run["account_id"] == account_id and run["news_policy"] == spec["news"],
            "News review requires its authoritative analysis run and policy")
    collection = artifacts.read_json(review["collection_manifest_ref"])
    require(collection.get("run_id") == review.get("run_id") == run_id
            and collection["cutoff_at"] == run["publish_cutoff"], "News review belongs to another analysis cutoff")
    return run


def _analysis_account(store, account_id):
    require(type(account_id) is str, "Analysis account identity must be text")
    if account_id != "main":
        binding = store.get("trial_risk_binding", account_id)
        require(binding is not None and account_id == "trial:" + binding["trial_id"] + ":strategy"
                and store.get("trial", binding["trial_id"]) is not None,
                "Analysis requires the actual account or a registered strategy trial book")
    return store.get("account", account_id)


def _document_refs(value):
    if type(value) is dict:
        if {"sha256", "size"} <= value.keys() and ("path" in value or "chunks" in value):
            yield value
            return
        for child in value.values():
            yield from _document_refs(child)
    elif type(value) is list:
        for child in value:
            yield from _document_refs(child)


def _market(prepared, payload, store, artifacts, operation_id):
    import allocation_runtime as runtime
    import fee_contract
    import research_audit
    import source_documents
    run, audit, manifest, policy, nav, features = runtime.load_data(store.root, store.base, prepared["run_path"])
    fields(payload, {"currency", "contracts"}, label="source-bound market contracts")
    require(payload["currency"] == "CNY", "The supported market contract is CNY fund NAV on Tiantian")
    require(type(payload["contracts"]) is list, "Product contract references must be a list")
    codes, terms, evidence, hashes = set(nav), [], [], {}
    seen_codes, term_gaps = set(), []
    observed_at = utc_now()
    for item in payload["contracts"]:
        require(type(item) is dict, "Each contract must be an object or immutable contract reference")
        if {"sha256", "size"} <= item.keys():
            reference, contract = item, artifacts.read_json(item)
        else:
            contract, reference = item, artifacts.put_json(item)
        require(contract.get("code") in codes, "Contract is outside researched coverage")
        require(contract["code"] not in seen_codes, "Duplicate contract for one product")
        seen_codes.add(contract["code"])
        try:
            term = fee_contract.normalize_to_terms(contract, artifacts, as_of=observed_at, contract_ref=reference)
        except fee_contract.EvidenceGap as exc:
            term_gaps.append({"code": contract["code"], "kind": "incomplete_source_evidence", "contract_ref": reference,
                              "required_actions": exc.required_actions})
        else:
            require(term["fee_contract"]["subject"]["currency"] == payload["currency"], "Contract currency differs")
            terms.append(term)
        for doc_ref in _document_refs(contract.get("bindings", contract.get("sources", {}))):
            if doc_ref.get("media_type") != "application/json":
                continue
            candidate_document = artifacts.read_json(doc_ref)
            if type(candidate_document) is not dict or "document_id" not in candidate_document:
                continue
            document = source_documents.read_extracted_document(doc_ref, artifacts)
            source_id = "terms:" + document["document_id"]
            if source_id not in hashes:
                hashes[source_id] = document["raw_sha256"]
                evidence.append({"source_id": source_id, "source_url": document["url"],
                    "retrieved_at": document["retrieved_at"], "role": "field_bound_terms",
                    "artifact": document["raw_ref"]})
    term_gaps.extend({"code": code, "kind": "contract_not_provided", "contract_ref": None,
                      "required_actions": [{"code": code, "action": "capture_and_inspect_current_product_dealing_terms"}]}
                     for code in sorted(codes-seen_codes))
    marks = {code: max(rows, key=lambda row: row["date"]) for code, rows in nav.items()}
    import allocation_market
    known_marks = {code: allocation_market.known_mark(row, observed_at) for code, row in marks.items()}
    prices = {code: mark["value"] for code, mark in known_marks.items()}
    price_dates = {code: mark["nav_date"] for code, mark in known_marks.items()}
    currencies = {code: policy["code_info"][code].get("currency") for code in nav}
    require(all(value == "CNY" for value in currencies.values()), "NAV currency needs independent CNY source evidence")
    for source in strict_json_loads((run / "source-manifest.json").read_text(encoding="utf-8")):
        if not source.get("path"):
            continue
        path = (run / source["path"]).resolve()
        require(path.is_relative_to(run), "Source path escapes research run")
        reference = artifacts.put_bytes(research_audit.source_bytes(run, source))
        evidence.append({"source_id": source["source_id"], "source_url": source["final_url"],
            "retrieved_at": source["retrieved_at"], "role": "public_source", "artifact": reference})
        hashes[source["source_id"]] = reference["sha256"]
    actions = []
    for code, rows in sorted(nav.items()):
        for row in rows:
            if Decimal(str(row["distribution_per_share"])) == 0:
                continue
            detail = copy.deepcopy(row.get("distribution_evidence") or {})
            actions.append({"code": code, "date": row["date"], "ex_date": row["date"],
                "record_date": detail.get("record_date"), "per_share": str(row["distribution_per_share"]),
                "pay_date": detail.get("cash_payment_date"), "rights_rule": detail.get("rights_rule"),
                "dividend_mode": detail.get("dividend_mode"), "currency": detail.get("currency"),
                "distribution_evidence": detail})
    market_ref = {"schema_version": 4, "currency": payload["currency"], "observed_at": observed_at,
        "price_dates": price_dates, "currencies": currencies, "prices": prices, "known_marks": known_marks, "terms": terms,
        "terms_basis": "source_verified", "source_hashes": hashes}
    verify.execution_sources(market_ref, artifacts)
    record = {"market_ref": market_ref, "evidence_refs": evidence, "provenance": {
        "price_dates": price_dates, "price_validation": "verified_source_NAV", "terms_basis": "source_verified",
        "distributions_by_code": {code: str(row["distribution_per_share"]) for code, row in marks.items()},
        "coverage_by_code": {code: {"start": min(row["date"] for row in rows), "end": max(row["date"] for row in rows)} for code, rows in nav.items()},
        "product_names": {row["code"]: row["name_observed_today"] for row in manifest["raw_summaries"]},
        "corporate_actions": actions, "action_inventory": manifest["action_inventory"], "nav_ref": artifacts.put_json(nav), "research_ref": prepared["run_path"],
        "dataset_hash": manifest["dataset_hash"], "audit": audit, "term_gaps": term_gaps,
        "terms_coverage_scope": "verified_present_contracts_with_explicit_per_code_gaps",
        "scope": "source_reproduction_and_field_semantics_not_authenticated_broker_quotes"}}
    market_id = fingerprint(record)
    verify.market_contract_coverage(record, artifacts)
    store.put("market", market_id, record)
    return {"status": "valuation_ready_with_term_gaps" if term_gaps else "market_ready", "market_id": market_id, "record": record,
        "data_ref": artifacts.put_json({"nav": nav, "features": features, "code_info": policy["code_info"]}),
        "record_ref": artifacts.put_json(record)}


def future_actions(market_record, as_of):
    captures = {row["source_id"]: row for row in market_record["evidence_refs"] if row["role"] == "public_source"}
    result = []
    for code, inventory in sorted(market_record["provenance"]["action_inventory"]["by_code"].items()):
        require(instant(inventory["known_at"]) <= instant(as_of), "Corporate action source follows the decision")
        proof = captures[inventory["source_id"]]
        require(proof["artifact"]["sha256"] == inventory["source_sha256"], "Corporate action inventory source differs")
        for row in inventory["dividends"]:
            if row["date"] > market_record["market_ref"]["price_dates"][code]:
                result.append({"code": code, "record_date": row["record_date"], "ex_date": row["date"],
                    "pay_date": row["cash_payment_date"], "per_share": str(row["distribution_per_share"]),
                    "currency": row["currency"], "known_at": inventory["known_at"],
                    "evidence_refs": [proof["artifact"]], "rights_rule": row["rights_rule"]})
    return result


def _check_plan(plan, spec):
    from strategy import validate_spec
    validate_spec(spec)
    values = plan["constraints"]
    require(values["currency"] == spec["currency"] and values["platform"] == spec["universe_policy"]["platform"],
            "Strategy differs from the confirmed currency/platform")
    def exact_limits(limits):
        return {kind: {key: Decimal(str(value)) for key, value in rows.items()} for kind, rows in limits.items()}
    require(exact_limits(values["position_limits"]) == exact_limits(spec["constraints"]),
            "Exposure limits differ from the confirmed plan")




def select_family(results, artifacts):
    """Compare only verified zero-addition policies on one real account basis."""
    import portfolio_mpc
    from risk_numbers import number, measure
    common, ranked, excluded = None, [], []
    exact_ranks = {}
    for item in results:
        context = artifacts.read_json(item["context_ref"])
        calculation = artifacts.read_json(item["calculation_ref"])
        basis = {key: context[key] for key in ("account_hash", "decision_at", "risk_state")}
        basis["capital"] = context["snapshot"]["equity"]
        require(common is None or basis == common, "Family financial comparisons use different capital/account/risk clocks")
        common = basis
        policy = calculation.get("mpc", {}).get("selected_policy")
        recovery = policy is not None and calculation.get("mpc", {}).get("decision_status") == "risk_recovery" and policy.get("recovery_eligible") is True
        eligible = (calculation.get("status") == "research_ready" and
            calculation.get("orders", {}).get("status") in ("ready", "no_action") and policy is not None
            and (policy.get("eligible") is True or recovery) and policy.get("proposed_contribution") == 0)
        if not eligible:
            excluded.append({"family_id": item["family"]["family_id"], "reason": calculation.get("reason", calculation["status"]),
                             "required_actions": calculation.get("required_actions", [])})
            continue
        probabilities = measure([path["probability"] for path in calculation["paths"]["selection_paths"]])
        capital = number(context["snapshot"]["equity"])
        losses = [capital-number(value["terminal_wealth"]) for value in policy["selection"]]
        profit = -sum(p*loss for p,loss in zip(probabilities,losses))
        risk = portfolio_mpc.tail_mean(losses, probabilities, context["spec"]["allocation"]["tail_probability"], exact=True)
        require(math.isfinite(profit) and math.isfinite(risk), "Nonfinite family wealth comparison")
        budget = capital-number(context["risk_state"]["net_principal"])*(1-number(context["risk_state"]["loss_tolerance"]))
        violation = max(0, risk-budget)
        exact_ranks[item["family"]["family_id"]] = (recovery, violation if recovery else 0,
            -profit, risk, number(policy["point"]["fees"]), item["family"]["family_id"])
        ranked.append({"family_id": item["family"]["family_id"], "context_ref": item["context_ref"],
            "calculation_ref": item["calculation_ref"], "policy_id": policy["id"],
            "expected_profit": float(profit), "absolute_cvar": float(risk), "fees": policy["point"]["fees"],
            "decision_status": "risk_recovery" if recovery else "feasible_selected",
            "risk_violation_amount": float(violation)})
    ranked.sort(key=lambda row: exact_ranks[row["family_id"]])
    result = {"ranked": ranked, "excluded": excluded,
        "selected_family_id": ranked[0]["family_id"] if ranked else None,
        "scope": "same_capital_feasible_net_wealth_else_registered_empirical_risk_recovery_priority",
        "decision_status": ranked[0]["decision_status"] if ranked else "risk_unresolved",
        "all_market_optimality_claimed": False}
    result["selection_hash"] = fingerprint(result)
    return result


def _daily(payload, store, artifacts, operation_id):
    fields(payload, set(), {"feedback", "spec", "research", "market_terms", "news", "news_review_id", "account_id",
                           "basis_decision_id", "sealed_intervention", "discovery_id", "discovery_policy",
                           "issuer_disclosures", "news_policy", "universe_policy", "continue_from",
                           "analysis_run_id", "industry_data_id", "benchmark_contracts",
                           "quality_contracts", "quality_policy", "exposure_contracts", "paired_currency_validation"}, "daily review")
    import risk_profile as profile
    payload = copy.deepcopy(payload)
    if "spec" in payload or "basis_decision_id" in payload:
        import allocation_runtime
        allocation_runtime.ensure_runtime(store.root)
    basis = None
    if "basis_decision_id" in payload:
        require(not ({"spec", "research", "market_terms", "news", "feedback", "discovery_id", "discovery_policy",
                      "issuer_disclosures", "news_policy", "quality_contracts", "quality_policy", "exposure_contracts", "paired_currency_validation"} & payload.keys()),
                "A continued decision reuses its sealed inputs; changed facts require a fresh review")
        basis = store.get("decision", payload["basis_decision_id"])
        require(basis is not None and basis.get("context") is not None, "Unknown or incomplete decision basis")
        verify.verify_bundle(basis, artifacts, store=store)
        require(basis["context"]["spec"]["kind"] == "assisted_workflow", "Only assisted decisions accept an intervention")
        require(payload.get("account_id", basis["account_id"]) == basis["account_id"], "Decision account changed")
        payload.update(spec=basis["context"]["spec"], account_id=basis["account_id"])
    if "feedback" in payload:
        _step(store, artifacts, operation_id, "feedback", lambda: feedback(payload["feedback"], store),
              inputs={"payload": payload["feedback"], "operation_id": operation_id})
    account_id = payload.get("account_id", "main")
    account = _analysis_account(store, account_id)
    profile_status = profile.status(store, account_id)
    plan = store.get("plan_constraints", account_id)
    monitoring_codes = sorted((payload.get("spec") or {}).get("benchmark", {}).get("weights", {}))
    if basis:
        require(fingerprint(account) == basis["account_hash"], "Account changed since the decision basis")
    missing = []
    if plan is None:
        missing.append("确认平台、币种、持有目标、排除品类及组合约束；不要求固定赎回日期")
    elif payload.get("spec"):
        _check_plan(plan, payload["spec"])
    if account is None:
        missing.append("首次本金及已确认账户事件")
    if profile_status["status"] != "confirmed":
        missing.extend(item["question"] for item in profile_status["required_inputs"])
    if account and (account["unknown"] or account["pending_subscriptions"]):
        missing.append("核对账户待确认事实及申购份额，取得完整风险敞口后继续计算")
    news_result = {}
    news_policy = (payload.get("spec") or {}).get("news", payload.get("news_policy"))
    analysis_run = None
    if news_policy and not payload.get("news_review_id") and not payload.get("analysis_run_id"):
        payload["analysis_run_id"] = operation_id
        analysis_run = _open_analysis({"news_policy": news_policy, "account_id": account_id}, store, artifacts, operation_id)
    elif payload.get("analysis_run_id"):
        analysis_run = store.get("analysis_run", payload["analysis_run_id"])
        require(analysis_run is not None, "Unknown analysis run")
    if payload.get("news_review_id"):
        saved = store.get("news-review", payload["news_review_id"])
        require(saved is not None, "Unknown news review")
        news_result = {**artifacts.read_json(saved["manifest_ref"]), "manifest_ref": saved["manifest_ref"]}
        verify.references(news_result, artifacts)
        require(payload.get("analysis_run_id"), "Daily recommendations require an authoritative analysis run")
        analysis_run = _step(store, artifacts, operation_id, "analysis-run", lambda:
            _bound_analysis(payload["analysis_run_id"], news_result, {"news": news_policy}, store, artifacts, account_id),
            inputs={"run_id": payload["analysis_run_id"], "news_policy": news_policy, "account_id": account_id})
    elif "news" in payload:
        request = dict(payload["news"])
        if analysis_run:
            request.update(run_id=analysis_run["run_id"], cutoff_at=analysis_run["publish_cutoff"])
        news_result = _step(store, artifacts, operation_id, "news", lambda namespace: _news(request, store, artifacts, namespace),
                            inputs={"payload": request, "operation_id": operation_id}, namespaced=True)
    elif (payload.get("spec") or {}).get("news") or payload.get("news_policy"):
        stamp = analysis_run["publish_cutoff"]
        request = {"cutoff_at": stamp, "window_start": (instant(stamp) - dt.timedelta(days=1)).isoformat(),
                   "max_urls_per_source": 10, "timeout_seconds": 8, "run_id": analysis_run["run_id"],
                   "required_source_groups": (payload.get("spec") or {}).get("news", payload.get("news_policy", {})).get("required_source_groups", {})}
        news_result = _step(store, artifacts, operation_id, "news", lambda namespace: _news(request, store, artifacts, namespace),
                            inputs={"payload": request, "operation_id": operation_id}, namespaced=True)
    discovery = None
    if basis:
        require(payload.get("news_review_id") == basis["news"]["review_id"],
                "Continuation uses the same sealed news review; new evidence requires a new discovery")
        discovery = artifacts.read_json(basis["discovery_ref"])
    if not payload.get("news_review_id"):
        missing.append("依据已采集原文完成news_assess，形成有证据关联的industry_theses")
    elif "spec" in payload or "news_policy" in payload:
        import news
        import fund_universe
        news_spec = payload.get("spec") or {"kind": "numeric_policy", "news": payload["news_policy"]}
        news_result = _step(store, artifacts, operation_id, "news-review", lambda: news_result,
                            inputs={"spec": news_spec, "decision_at": None})
        checked_news = news.validate_review(news_result, news_spec, utc_now(), artifacts, store=store)
        held_codes = verify.monitor_codes(account)
        universe_policy = (payload.get("spec") or {}).get("universe_policy", payload.get("universe_policy"))
        require(universe_policy is not None, "A stable universe policy is required before discovery")
        if not basis:
            if "discovery_id" in payload:
                discovery = store.get("fund_discovery", payload["discovery_id"])
                require(discovery is not None, "Use a committed live fund discovery")
            else:
                discovery = _step(store, artifacts, operation_id, "discovery", lambda namespace: fund_universe.discover(
                    news_result, held_codes, store, artifacts, namespace, payload.get("discovery_policy", {}),
                    issuer_disclosures=payload.get("issuer_disclosures"), universe_policy=universe_policy,
                    continue_from=payload.get("continue_from"), plan_constraints=plan,
                    monitoring_codes=monitoring_codes, news_state=checked_news),
                    inputs={"review": news_result, "spec": news_spec, "decision_at": None,
                            "held_codes": held_codes, "monitoring_codes": monitoring_codes,
                            "plan_constraints": plan, "news_state": checked_news}, namespaced=True,
                    dependencies=("news-review",))
        if _validation_runtime(store).accepted("discovery") is None:
            discovery = _step(store, artifacts, operation_id, "discovery", lambda: discovery,
                inputs={"review": news_result, "spec": news_spec, "decision_at": None,
                        "held_codes": held_codes, "monitoring_codes": monitoring_codes,
                        "plan_constraints": plan, "news_state": checked_news}, dependencies=("news-review",))
        fund_universe.validate_discovery(discovery, news_result, held_codes, artifacts, utc_now(),
                                        monitoring_codes=monitoring_codes, news_state=checked_news, plan_constraints=plan)
        if discovery["status"] not in ("ready", "ready_with_pending_groups", "observation_only") or not set(held_codes) <= set(discovery["codes"]):
            missing.append("完成动态基金发现记录中的required_actions")
        require(discovery["universe_policy"] == universe_policy, "Discovery selection rule changed")
    else:
        missing.append("提交已确认的news_policy，核验本次新闻及行业判断")
    observation_only = discovery is not None and discovery["status"] == "observation_only"
    if not observation_only:
        for field in (("spec",) if basis else ("spec", "research", "market_terms")):
            if field not in payload:
                missing.append(field)
    orders = {"status": "blocked", "orders": [], "waiting": [], "funding_options": []}
    context, calculation_ref, market_id, spec_hash, data_ref = None, None, None, None, None
    industry_record_ref, base_data_ref = None, None
    quality_ref, exposure_ref, readiness_ref, family_results_ref = None, None, None, None
    quality_inputs_ref, exposure_inputs_ref, readiness_inputs_ref = None, None, None
    trade_inputs = None
    selection_ref = None
    account_review = None
    decision_cutoff = None
    term_gaps = []
    market_record_ref, product_names, comparison_summary, market_price_dates = None, {}, {}, {}
    result_status = "awaiting_input"
    reason = "请补齐列出的账户、来源或模型输入后继续计算。"
    verification = {"status": "not_applicable", "reason": "missing_inputs"}
    if not missing and observation_only:
        result_status = "observation_only"
        reason = "已核验信息未形成一致的增加方向，且当前没有持仓或未完成订单需要计算；本次保留现金并继续观察。"
        orders["status"] = "no_action"
        verification = {"status": "passed", "scope": "verified_news_and_observation_only_discovery"}
    elif not missing:
        import allocation_runner
        import strategy
        if basis:
            market = {"market_id": basis["market_id"], "record": store.get("market", basis["market_id"]),
                      "data_ref": basis["data_ref"]}
            require(market["record"] is not None, "Missing sealed market record")
            verify.references(market, artifacts)
        else:
            research_request = dict(payload["research"])
            require("codes" not in research_request, "Daily research codes are derived from verified discovery")
            research_request["codes"] = discovery["codes"]
            prepared = _step(store, artifacts, operation_id, "research", lambda: _research(
                research_request, store, operation_id, identity_snapshot_ref=discovery["identity_snapshot_ref"]),
                inputs={"payload": research_request, "identity_snapshot_ref": discovery["identity_snapshot_ref"],
                        "operation_id": operation_id}, dependencies=("discovery",))
            market = _step(store, artifacts, operation_id, "market", lambda namespace: _market(prepared, payload["market_terms"], store, artifacts, namespace),
                inputs={"prepared": prepared, "discovery_ref": artifacts.put_json(discovery)}, namespaced=True,
                dependencies=("research",))
        market_id = market["market_id"]
        data_ref = market["data_ref"]
        base_data_ref = basis.get("base_data_ref", data_ref) if basis else data_ref
        market_record_ref = artifacts.put_json(market["record"])
        product_names = market["record"]["provenance"]["product_names"]
        market_price_dates = market["record"]["provenance"]["price_dates"]
        term_gaps = market["record"]["provenance"]["term_gaps"]
        if not basis:
            import account_reconciliation
            _step(store, artifacts, operation_id, "account-reconciliation", lambda:
                account_reconciliation.reconcile_market(account_id, market["record"], store, artifacts, operation_id),
                inputs={"account_id": account_id, "market_record": market["record"], "operation_id": operation_id},
                dependencies=("market",))
            account = _analysis_account(store, account_id)
            profile_status = profile.status(store, account_id)
        if payload.get("sealed_intervention") is not None:
            require(bool(payload.get("news_review_id")), "An assisted decision requires a sealed news assessment")
            import news
            news.validate_review(news_result, payload["spec"], utc_now(), artifacts, store=store)
        import industry_data
        if basis:
            sector_value = artifacts.read_json(basis["industry_record_ref"])
        elif payload.get("industry_data_id"):
            sector_value = store.get("industry_data", payload["industry_data_id"])
            require(sector_value is not None, "Use committed industry source data")
        else:
            sector_value = _step(store, artifacts, operation_id, "industry-data", lambda namespace:
                industry_data.prepare_sector_data(news_result, payload["spec"], store, artifacts, namespace,
                    payload.get("benchmark_contracts", [])), namespaced=True,
                inputs={"spec": payload["spec"], "news_review_id": payload["news_review_id"],
                        "analysis_run_id": payload["analysis_run_id"]}, dependencies=("analysis-run", "news-review"))
        if _validation_runtime(store).accepted("industry-data") is None:
            sector_value = _step(store, artifacts, operation_id, "industry-data", lambda: sector_value,
                inputs={"spec": payload["spec"], "news_review_id": payload["news_review_id"],
                        "analysis_run_id": payload["analysis_run_id"]}, dependencies=("analysis-run", "news-review"))
        decision_cutoff = _step(store, artifacts, operation_id, "decision-cutoff", lambda: {"at": utc_now()},
                               inputs=_time_inputs(store, operation_id))["at"]
        import account_reconciliation
        account_review = account_reconciliation.review_account(account, market["record"]["market_ref"], decision_cutoff)
        if account_review is not None:
            result_status = "awaiting_account"
            reason = "已保留确认事实；当前账户估值需要补核，继续报告新闻和待核动作，暂不计算风险金额或交易方案。"
            verification = {"status": "passed", "scope": "verified_sources_and_account_reconciliation_no_quantitative_decision"}
        else:
            risk_input = profile.resolve(store, account, decision_cutoff, market["record"]["market_ref"]["prices"], account_id,
                                         price_dates=market_price_dates)
            import industry_binding
            industry_record_ref = artifacts.put_json(sector_value)
            data = _step(store, artifacts, operation_id, "model-data", lambda:
                industry_binding.bind_numerical_data(artifacts.read_json(base_data_ref), sector_value, artifacts,
                    store=store, at=decision_cutoff, spec=payload["spec"]),
                inputs={"base_data_ref": base_data_ref, "industry_record_ref": industry_record_ref,
                        "spec": payload["spec"], "decision_at": decision_cutoff}, dependencies=("industry-data",))
            data_ref = artifacts.put_json(data)
            import fund_quality
            import exposure_bounds
            quality_inputs = {"data_ref": base_data_ref, "contracts": payload.get("quality_contracts", []),
                              "decision_at": decision_cutoff, "policy": payload.get("quality_policy")}
            quality_bundle = _step(store, artifacts, operation_id, "fund-quality",
                lambda: fund_quality.prepare_source_quality(base_data_ref, quality_inputs["contracts"], artifacts, decision_cutoff,
                                              quality_inputs["policy"]), inputs=quality_inputs, dependencies=("model-data",))
            quality = quality_bundle["current_evaluation"]
            quality_ref = artifacts.put_json(quality)
            quality_inputs.update(source_bundle_ref=artifacts.put_json(quality_bundle), model_data_ref=data_ref)
            quality_inputs_ref = artifacts.put_json(quality_inputs)
            data = _step(store, artifacts, operation_id, "quality-model-data", lambda:
                fund_quality.bind_quality_features(artifacts.read_json(quality_inputs["model_data_ref"]), quality_bundle),
                inputs=quality_inputs, dependencies=("fund-quality", "model-data"))
            data_ref = artifacts.put_json(data)
            exposure_inputs = {"contracts": payload.get("exposure_contracts", []), "identities": data["code_info"],
                               "decision_at": decision_cutoff}
            exposure = _step(store, artifacts, operation_id, "sector-exposure",
                lambda: exposure_bounds.assemble(exposure_inputs["contracts"], data["code_info"], artifacts, decision_cutoff),
                inputs=exposure_inputs, dependencies=("model-data",))
            exposure_ref = artifacts.put_json(exposure)
            exposure_inputs_ref = artifacts.put_json(exposure_inputs)
            sector_bounds = {code: {sector: copy.deepcopy(product["sectors"].get(sector, product["default_bound"]))
                for sector in payload["spec"]["constraints"]["sector_limits"]}
                for code, product in exposure["products"].items()}
            industry_ref = {"bundle_hash": sector_value["bundle_hash"], "news_ref": fingerprint(data["industry"]["news_ref"]),
                            "event_frontier_hash": data["industry"]["news_state"]["event_frontier_hash"]}
            import fund_screen
            selection = _step(store, artifacts, operation_id, "final-selection", lambda:
                fund_screen.finalize_selection(discovery, data["code_info"], market["record"]["market_ref"]["terms"], plan, decision_cutoff,
                    max_terms_age_seconds=payload["spec"]["availability"]["max_terms_age_seconds"]),
                inputs={"discovery": discovery, "code_info": data["code_info"], "terms": market["record"]["market_ref"]["terms"],
                        "plan_constraints": plan, "decision_at": decision_cutoff,
                        "max_terms_age_seconds": payload["spec"]["availability"]["max_terms_age_seconds"]})
            selection = _step(store, artifacts, operation_id, "quality-annotation",
                lambda: fund_screen.attach_quality(selection, quality, quality_bundle["admission"]),
                inputs={"selection": selection, "quality": quality, "admission": quality_bundle["admission"],
                        "quality_inputs": quality_inputs},
                dependencies=("fund-quality", "final-selection"))
            selection_ref = artifacts.put_json(selection)
            import newtrade_guard
            trade_history = _step(store, artifacts, operation_id, "trade-history", lambda:
                newtrade_guard.read_inputs(store, artifacts, account_id, payload["spec"], decision_cutoff,
                                          decision_id=operation_id)["trade_state"], inputs={"account_id": account_id, "spec": payload["spec"],
                                              "decision_at": decision_cutoff, "decision_id": operation_id})
            import ledger
            import candidate_readiness
            market_ref = market["record"]["market_ref"]
            snapshot = ledger.snapshot(account, decision_cutoff, market_ref["prices"], market_ref["price_dates"])
            clock = {"order_time_local": instant(decision_cutoff).astimezone(ZoneInfo("Asia/Shanghai")).time().replace(tzinfo=None).isoformat()}
            contracts = {row["code"]: row["fee_contract"] for row in market_ref["terms"] if row.get("fee_contract")}
            readiness_inputs = {"state": snapshot, "eligible_codes": selection["eligible_buy_codes"], "data": data,
                "spec": payload["spec"], "decision_at": decision_cutoff, "clock_context": clock, "fee_contracts": contracts}
            readiness = _step(store, artifacts, operation_id, "candidate-readiness",
                lambda: candidate_readiness.assess_readiness(**readiness_inputs), inputs=readiness_inputs,
                dependencies=("quality-annotation", "trade-history"))
            readiness_ref = artifacts.put_json(readiness)
            readiness_inputs_ref = artifacts.put_json(readiness_inputs)
            build_kwargs = {"account_id": account_id, "risk_state": risk_input,
                "purchase_eligible_codes": selection["eligible_buy_codes"], "dynamic_universe": discovery["codes"],
                "candidate_identities": data["code_info"], "known_future_actions": future_actions(market["record"], decision_cutoff),
                "action_inventory_scope": "known_subset", "sealed_intervention": payload.get("sealed_intervention"),
                "news_review_hash": fingerprint(news_result) if payload.get("news_review_id") else None,
                "trade_state": trade_history,
                "industry_data_ref": industry_ref, "sector_exposure_bounds": sector_bounds,
                "readiness_ref": readiness_ref,
                "paired_currency_validation": payload.get("paired_currency_validation")}
            families = readiness["candidate_families"]
            governance_block = None
            if families:
                trade_inputs = _step(store, artifacts, operation_id, "trade-inputs", lambda:
                    newtrade_guard.reserve_review(store, artifacts, account_id, payload["spec"], decision_cutoff,
                        decision_id=operation_id), inputs={"account_id": account_id, "spec": payload["spec"],
                        "decision_at": decision_cutoff, "decision_id": operation_id}, dependencies=("candidate-readiness",))
                build_kwargs.update(trade_family_review_index=trade_inputs["trade_family_review_index"])
                require(trade_inputs["trade_state"] == trade_history,
                        "Confirmed history changed before numerical review reservation")
                if not trade_inputs["qualification_scope"]["eligible"] or trade_inputs["review_reservation"] is None:
                    missing.append({"action": "declare_current_trade_policy_or_review_budget", "reasons": trade_inputs["qualification_scope"]["reasons"]})
                    governance_block = {"scope": trade_inputs["qualification_scope"], "families": copy.deepcopy(families)}
                    families = []
            results = []
            for family in families:
                identity = family["family_id"]
                kwargs = {**build_kwargs, "purchase_eligible_codes": family["buy_codes"], "family_id": identity}
                context_name, calculation_name = "context@"+identity, "calculation@"+identity
                family_context = _step(store, artifacts, operation_id, context_name,
                    lambda kwargs=kwargs: strategy.build_context(payload["spec"], account, decision_cutoff, market_ref, **kwargs),
                    inputs={"spec": payload["spec"], "account": account, "decision_at": decision_cutoff,
                        "market_ref": market_ref, "build_kwargs": kwargs},
                    dependencies=("candidate-readiness", "sector-exposure", "trade-inputs"))
                source_binding = {"market_record_ref": market_record_ref, "data_ref": data_ref, "base_data_ref": base_data_ref,
                    "industry_record_ref": industry_record_ref, "discovery_ref": artifacts.put_json(discovery),
                    "quality_inputs_ref": quality_inputs_ref, "quality_ref": quality_ref,
                    "market_id": fingerprint(market["record"]), "decision_at": family_context["decision_at"], "context": family_context}
                source_runtime = {"artifacts": artifacts, "store": store, "binding_bundle": source_binding}
                family_calculation = _step(store, artifacts, operation_id, calculation_name,
                    lambda c=family_context, r=source_runtime: allocation_runner.predict(c, data,
                        stage_runner=_numeric_stage_runner(store, unit_id=c["context_hash"]), family_count=len(families),
                        source_runtime=r),
                    inputs={"context": family_context, "data": data, "family_count": len(families), "source_binding": source_binding},
                    dependencies=(context_name,))
                family_proof = _step(store, artifacts, operation_id, "reproduction@"+identity,
                    lambda c=family_context, v=family_calculation, r=source_runtime: verify.reproduce(c, data, v, source_runtime=r),
                    inputs={"context": family_context, "data": data, "calculation": family_calculation, "source_binding": source_binding},
                    dependencies=(calculation_name,))
                results.append({"family": family, "context_ref": artifacts.put_json(family_context),
                    "calculation_ref": artifacts.put_json(family_calculation), "verification": family_proof})
            ranked = _step(store, artifacts, operation_id, "family-selection",
                lambda: select_family(results, artifacts), inputs={"family_results": results, "readiness_ref": readiness_ref, "data": data,
                    "governance_block": governance_block, "trade_review_inputs": trade_inputs, "spec": payload["spec"],
                    "account_id": account_id, "decision_at": decision_cutoff, "decision_id": operation_id},
                dependencies=tuple("reproduction@"+family["family_id"] for family in families))
            family_results_ref = artifacts.put_json({"results": results, "selection": ranked, "readiness_ref": readiness_ref,
                                                    "governance_block": governance_block})
            if ranked["selected_family_id"] is not None:
                chosen = next(row for row in results if row["family"]["family_id"] == ranked["selected_family_id"])
                context = artifacts.read_json(chosen["context_ref"])
                calculation_ref = chosen["calculation_ref"]
                calculation = artifacts.read_json(calculation_ref)
                spec_hash = context["spec_hash"]
                orders, verification = calculation["orders"], chosen["verification"]
                from report import summarize
                comparison_summary = summarize(calculation, context)
                result_status = "conditional_research" if calculation["status"] == "research_ready" else calculation["status"]
                reason = calculation.get("reason", "在声明的合格资金集合中比较净收益、交易费用、现金到账与风险。")
            else:
                result_status = "partial"
                reason = "未形成完整、可比较且资格合格的资金方案；本次保留真实持仓并报告具体资料缺口。"
                orders = {"status": "blocked", "orders": [], "waiting": [], "funding_options": []}
                verification = {"status": "passed", "scope": "verified_source_readiness_and_no_unqualified_money"}
                missing.extend(readiness.get("required_actions", []))
                for excluded in ranked["excluded"]:
                    missing.extend(excluded["required_actions"])
    decision_at = context["decision_at"] if context else decision_cutoff or (account_review["as_of"] if account_review else _report_time(store, operation_id))
    from report import summarize_discovery
    bundle = {"schema_version": 4, "decision_id": operation_id, "decision_at": decision_at,
              "account_id": account_id, "account_hash": fingerprint(account) if account else None,
              "account_recorded_at": account["recorded_at"] if account else None, "market_price_dates": market_price_dates,
              "plan_constraints": plan, "monitoring_codes": monitoring_codes,
              "term_gaps": term_gaps,
              "spec_hash": spec_hash, "market_id": market_id, "context": context,
              "account_state_ref": artifacts.put_json(account) if account is not None else None, "data_ref": data_ref,
              "base_data_ref": base_data_ref, "model_data_ref": data_ref, "industry_record_ref": industry_record_ref,
              "quality_ref": quality_ref, "exposure_ref": exposure_ref, "readiness_ref": readiness_ref,
              "family_results_ref": family_results_ref,
              "quality_inputs_ref": quality_inputs_ref, "exposure_inputs_ref": exposure_inputs_ref,
              "readiness_inputs_ref": readiness_inputs_ref,
              "analysis_run_id": payload.get("analysis_run_id"),
              "analysis_run_ref": artifacts.put_json(analysis_run) if analysis_run else None,
              "market_record_ref": market_record_ref, "product_names": product_names,
              "comparison_summary": comparison_summary, "profile_status": profile_status,
              "account_review": account_review,
              "trade_review_inputs": trade_inputs,
              "trade_review_spec": copy.deepcopy(payload.get("spec")) if trade_inputs else None,
              "discovery_ref": artifacts.put_json(discovery) if discovery else None,
              "selection_ref": selection_ref,
              "selection_summary": summarize_discovery(discovery, artifacts),
              "calculation_ref": calculation_ref, "orders": orders, "news": news_result,
              "news_policy": (payload.get("spec") or {}).get("news", payload.get("news_policy")),
              "missing": missing, "status": result_status,
              "reason": reason, "verification": verification, "source_hashes": verify.code_identity(),
              "qualification": {"status": "conditional_research", "reason":
                  "来源和金额校验支持可复核的条件方案；预测优势须由冻结规则的样本外和前瞻证据评价，未来收益没有保证。"}}
    from report import explain_actions
    bundle["action_analysis"] = explain_actions(bundle, artifacts)
    bundle["bundle_hash"] = fingerprint(bundle)
    validation = verify.verify_bundle(bundle, artifacts, store=store)
    from report import render
    report = _step(store, artifacts, operation_id, "report", lambda: render(bundle), inputs={"bundle": bundle})
    report_check = verify.verify_report(bundle, report)
    bundle_ref, report_ref = artifacts.put_json(bundle), artifacts.put_bytes(report.encode("utf-8"))
    result = {"status": bundle["status"], "request_id": operation_id, "bundle_ref": bundle_ref,
              "report_ref": report_ref, "report_markdown": report, "verification": validation,
              "report_verification": report_check}
    expected = [{"kind": "account", "key": account_id, "hash": bundle["account_hash"]},
                {"kind": "risk_profile", "key": account_id, "hash": profile_status["profile_hash"]},
                {"kind": "plan_constraints", "key": account_id, "hash": fingerprint(plan) if plan else None}]
    binding = store.get("trial_risk_binding", account_id)
    if binding:
        expected.append({"kind": "risk_profile", "key": binding["source_account_id"], "hash": binding["profile_hash"]})
        expected.append({"kind": "plan_constraints", "key": binding["source_account_id"], "hash": fingerprint(plan) if plan else None})
    def guard():
        at = utc_now()
        store.assert_owned()
        if payload.get("news_review_id") and bundle["news_policy"]:
            import news
            checked_news = news.validate_review(news_result, {"kind": "numeric_policy", "news": bundle["news_policy"]},
                                                at, artifacts, store=store)
            if discovery is not None:
                import fund_universe
                fund_universe.validate_discovery(discovery, news_result, verify.monitor_codes(account), artifacts, at,
                                                monitoring_codes=monitoring_codes, news_state=checked_news,
                                                plan_constraints=store.get("plan_constraints", account_id))
        if context:
            import newtrade_guard
            scope = newtrade_guard.qualification_scope(context["spec"]["trade_policy"], at,
                                                       context["trade_family_review_index"])
            require(scope["eligible"], "Trade governance expired before publication")
            profile.assert_current(store, context["risk_input"], store.get("account", account_id), at,
                                   context["market_ref"]["prices"], account_id, price_dates=context["market_ref"]["price_dates"])
            verify.execution_sources(context["market_ref"], artifacts, as_of=at)
            require((instant(at)-instant(context["market_ref"]["observed_at"])).total_seconds()
                    <= context["spec"]["availability"]["max_market_age_seconds"], "Market expired before publication")
    guard()
    writes = [
        {"kind": "decision", "key": operation_id, "value": bundle, "immutable": True},
        {"kind": "latest", "key": "decision", "value": {"decision_id": operation_id, "bundle_ref": bundle_ref}, "immutable": False}]
    return {"result": result, "expected": expected, "publication_guard": guard, "writes": writes}


def dispatch(request, store, artifacts, *, producer_id=None):
    operation, payload, operation_id = request["operation"], request["payload"], request["request_id"]
    derived_id = producer_id or operation_id
    if operation == "analysis_start":
        run = _open_analysis(payload, store, artifacts, operation_id)
        return {"result": {"status": "analysis_opened", "run": run, "run_ref": artifacts.put_json(run)}}
    if operation == "industry_prepare":
        import allocation_runtime
        allocation_runtime.ensure_runtime(store.root)
        import industry_data
        fields(payload, {"analysis_run_id", "news_review_id", "spec"}, {"benchmark_contracts"}, "industry preparation")
        saved = store.get("news-review", payload["news_review_id"])
        require(saved is not None, "Unknown news review")
        review = {**artifacts.read_json(saved["manifest_ref"]), "manifest_ref": saved["manifest_ref"]}
        run = store.get("analysis_run", payload["analysis_run_id"])
        require(run is not None, "Unknown analysis run")
        _analysis_account(store, run["account_id"])
        _bound_analysis(payload["analysis_run_id"], review, payload["spec"], store, artifacts, run["account_id"])
        value = _step(store, artifacts, operation_id, "industry-data", lambda namespace:
            industry_data.prepare_sector_data(review, payload["spec"], store, artifacts, namespace,
                payload.get("benchmark_contracts", [])), namespaced=True,
            inputs={"spec": payload["spec"], "news_review_id": payload["news_review_id"],
                    "analysis_run_id": payload["analysis_run_id"]})
        return {"result": value, "writes": [{"kind": "industry_data", "key": operation_id, "value": value, "immutable": True}],
                "validation_bindings": {"spec": payload["spec"], "news_review_id": payload["news_review_id"],
                                        "analysis_run_id": payload["analysis_run_id"]}}
    if operation in {"cashflow_prepare", "cashflow_confirm", "cashflow_correct", "profile_initialize", "fund_discover", "news_assess"}:
        import allocation_runtime
        allocation_runtime.ensure_runtime(store.root)
    if operation == "status":
        fields(payload, set(), label="status")
        account = store.get("account", "main")
        import risk_profile as profile
        return {"result": {"status": "ready", "mode": "first_investment" if account is None else "existing_account",
                           "account_hash": fingerprint(account) if account else None,
                           "account": account, "profile": profile.status(store),
                           "plan_constraints": store.get("plan_constraints", "main"),
                           "latest": store.get("latest", "decision")}}
    if operation == "feedback":
        return {"result": feedback(payload, store)}
    if operation in ("profile_initialize", "risk_update", "plan_update", "capital_reconcile"):
        import risk_profile as profile
        function = {"profile_initialize": profile.initialize, "risk_update": profile.update,
                    "plan_update": profile.update_constraints, "capital_reconcile": profile.reconcile_capital}[operation]
        return {"result": function(payload, store, operation_id)}
    if operation in ("cashflow_prepare", "cashflow_confirm", "cashflow_correct"):
        import cashflows
        function = {"cashflow_prepare": cashflows.prepare, "cashflow_confirm": cashflows.confirm,
                    "cashflow_correct": cashflows.correct}[operation]
        return {"result": function(payload, store, operation_id)}
    if operation == "source_capture":
        import allocation_runtime
        import source_documents
        fields(payload, {"url", "source_id"}, {"timeout"}, "document capture")
        allocation_runtime.ensure_runtime(store.root)
        reference = source_documents.capture_document(payload["url"], payload["source_id"], artifacts,
            timeout=payload.get("timeout", 20), store=store)
        document = artifacts.read_json(reference)
        return {"result": {"status": document["status"], "document_ref": reference,
            "document": document, "required_actions": document["required_actions"]}}
    if operation == "fee_inspect":
        import allocation_runtime
        import fee_contract
        allocation_runtime.ensure_runtime(store.root)
        inspection_at = utc_now()
        inspected = fee_contract.inspect_contract(payload, artifacts, as_of=inspection_at)
        contract_ref = artifacts.put_json(inspected["contract"]) if inspected["contract"] is not None else None
        return {"result": {"status": inspected["status"], "inspection_ref": artifacts.put_json(inspected),
                           "contract_ref": contract_ref, "required_actions": inspected["required_actions"],
                           "field_statuses": inspected["field_statuses"]}, "validation_bindings": {"decision_at": inspection_at}}
    if operation == "fund_discover":
        import fund_universe
        import news
        fields(payload, {"news_review_id", "news_policy", "universe_policy"},
               {"policy", "issuer_disclosures", "account_id", "continue_from", "monitoring_codes"}, "fund discovery")
        saved = store.get("news-review", payload["news_review_id"])
        require(saved is not None, "Assess news before discovering funds")
        reviewed = {**artifacts.read_json(saved["manifest_ref"]), "manifest_ref": saved["manifest_ref"]}
        news_spec = {"kind": "numeric_policy", "news": payload["news_policy"]}
        reviewed = _step(store, artifacts, operation_id, "news-review", lambda: reviewed,
                         inputs={"spec": news_spec, "decision_at": None})
        checked_news = news.validate_review(reviewed, news_spec, utc_now(), artifacts, store=store)
        account_id = payload.get("account_id", "main")
        account = _analysis_account(store, account_id) or {}
        plan = store.get("plan_constraints", account_id)
        monitoring = payload.get("monitoring_codes", [])
        held_codes = verify.monitor_codes(account)
        discovered = _step(store, artifacts, operation_id, "discovery", lambda namespace: fund_universe.discover(
            reviewed, held_codes, store, artifacts, namespace, payload.get("policy", {}),
            issuer_disclosures=payload.get("issuer_disclosures"), universe_policy=payload["universe_policy"],
            continue_from=payload.get("continue_from"), plan_constraints=plan, monitoring_codes=monitoring, news_state=checked_news),
            inputs={"review": reviewed, "spec": news_spec, "decision_at": None, "held_codes": held_codes,
                    "monitoring_codes": monitoring, "plan_constraints": plan, "news_state": checked_news},
            namespaced=True, dependencies=("news-review",))
        fund_universe.validate_discovery(discovered, reviewed, held_codes, artifacts, utc_now(),
            monitoring_codes=monitoring, news_state=checked_news, plan_constraints=plan)
        return {"result": discovered, "writes": [{"kind": "fund_discovery", "key": operation_id,
                "value": discovered, "immutable": True}]}
    if operation == "news_collect":
        collected = _step(store, artifacts, operation_id, "news", lambda namespace: _news(payload, store, artifacts, namespace),
                          inputs={"payload": payload, "operation_id": operation_id}, namespaced=True)
        return {"result": {"status": "collected", **collected}}
    if operation == "news_assess":
        import news
        assessed = news.assess(payload, store, artifacts, derived_id)
        run, _ = news.run_policy(assessed["run_id"], store, artifacts)
        return {"result": assessed, "validation_bindings": {
            "spec": {"kind": "numeric_policy", "news": run["news_policy"]}, "analysis_run_id": run["run_id"]}}
    if operation == "research_prepare":
        import allocation_runtime
        import fund_universe
        fields(payload, {"discovery_id", "start", "end", "horizons", "lookback"}, {"timeout"}, "research preparation")
        allocation_runtime.ensure_runtime(store.root)
        discovered = store.get("fund_discovery", payload["discovery_id"])
        require(discovered is not None, "Research requires a committed live discovery")
        fund_universe.verify_identity(artifacts.read_json(discovered["identity_snapshot_ref"]), artifacts, as_of=utc_now())
        research_request = {key: value for key, value in payload.items() if key != "discovery_id"}
        research_request["codes"] = discovered["codes"]
        prepared = _step(store, artifacts, operation_id, "research", lambda: _research(research_request, store, operation_id,
                          identity_snapshot_ref=discovered["identity_snapshot_ref"]),
                          inputs={"payload": research_request, "identity_snapshot_ref": discovered["identity_snapshot_ref"],
                                  "operation_id": operation_id})
        return {"result": prepared, "writes": [{"kind": "latest", "key": "research", "value": prepared, "immutable": False}]}
    if operation == "research_verify":
        import research
        fields(payload, {"run_path"}, label="research verification")
        result = research.verify_selected(store.root, store.base, research.resolve_run(store.base, payload["run_path"]))
        return {"result": {"status": "passed", "audit": result}}
    if operation == "market_prepare":
        import allocation_runtime
        import fund_universe
        fields(payload, {"discovery_id", "research", "market_terms"}, label="market preparation")
        allocation_runtime.ensure_runtime(store.root)
        discovered = store.get("fund_discovery", payload["discovery_id"])
        require(discovered is not None, "Market preparation requires a committed live discovery")
        fund_universe.verify_identity(artifacts.read_json(discovered["identity_snapshot_ref"]), artifacts, as_of=utc_now())
        research_request = dict(payload["research"])
        require("codes" not in research_request, "Market research codes come from discovery")
        research_request["codes"] = discovered["codes"]
        prepared = _step(store, artifacts, operation_id, "research", lambda: _research(research_request, store, operation_id,
                          identity_snapshot_ref=discovered["identity_snapshot_ref"]),
                          inputs={"payload": research_request, "identity_snapshot_ref": discovered["identity_snapshot_ref"],
                                  "operation_id": operation_id})
        market = _step(store, artifacts, operation_id, "market", lambda namespace: _market(prepared, payload["market_terms"], store, artifacts, namespace),
                      inputs={"prepared": prepared, "discovery_ref": artifacts.put_json(discovered)}, namespaced=True,
                      dependencies=("research",))
        return {"result": {"status": "market_ready", **market}, "validation_bindings": {
            "prepared": prepared, "discovery_ref": artifacts.put_json(discovered)}}
    if operation == "daily_review":
        return _daily(payload, store, artifacts, operation_id)
    if operation == "allocation_replay":
        import allocation_runtime
        import allocation_runner
        fields(payload, {"spec", "initial_account_id", "steps"}, label="replay")
        initial = store.get("account", payload["initial_account_id"])
        require(initial is not None, "Replay requires a recorded account")
        allocation_runtime.ensure_runtime(store.root)
        result = allocation_runner.replay(payload["spec"], initial, payload["steps"])
        return {"result": {"status": "evaluated", "result_ref": artifacts.put_json(result), "scope": "conditional_replay"}}
    if operation.startswith("trial_"):
        import allocation_runtime
        allocation_runtime.ensure_runtime(store.root)
        import trial
        return trial.execute(operation, payload, store, artifacts, operation_id)
    if operation == "audit":
        fields(payload, set(), {"decision_id"}, "audit")
        result = store.audit(verify.reference_checker(artifacts))
        if "decision_id" in payload:
            bundle = store.get("decision", payload["decision_id"])
            require(bundle is not None, "Unknown decision")
            if (bundle.get("context") is not None or bundle.get("calculation_ref") is not None
                    or (bundle.get("news") or {}).get("collection_manifest_ref")):
                import allocation_runtime
                allocation_runtime.ensure_runtime(store.root)
            result["decision"] = verify.verify_bundle(bundle, artifacts, store=store)
            saved = store.operation(payload["decision_id"])["result"]
            result["report"] = verify.verify_report(bundle, artifacts.read(saved["report_ref"]).decode("utf-8"))
        return {"result": result}
    if operation == "archive":
        fields(payload, set(), label="archive")
        return {"result": store.archive()}
    raise ContractError("Unsupported operation")


class FinalValidationFailure(EvidenceError):
    code = "final_validation_failed"

    def __init__(self, evidence):
        super().__init__("The complete workflow failed verification in all three rounds")
        self.evidence = evidence
        self.required_actions = [{"action": "resolve_complete_workflow_validation", "evidence": evidence}]

    def as_dict(self):
        return {"code": self.code, "evidence": self.evidence, "required_actions": self.required_actions}


def _runtime_contracts():
    import stage_validation
    from contracts import OPERATIONS
    require(set(stage_validation.OPERATION_NAMES) == set(OPERATIONS), "Operation verification registry is incomplete")
    names = set(stage_validation.STAGE_NAMES) | {"operation_" + name for name in OPERATIONS}
    names |= {"numeric_clock", "numeric_samples", "numeric_industry", "numeric_industry_bridge", "numeric_fit", "numeric_paths",
              "numeric_comparison", "numeric_mpc", "numeric_compile", "numeric_predictive_increment"}
    return {name: {"name": "investment:" + name, "version": "automatic-validation-v1"} for name in names}


def _review_package(request, package, store, artifacts):
    import stage_validation
    from validation_runtime import validate_result
    verify.references(package["result"], artifacts)
    result = stage_validation.validate_operation_result(request, package["result"], store, artifacts,
                                                        package.get("validation_bindings", {}))
    return validate_result(result)


def _completed_review(request, result, store, artifacts):
    # Immutable results are historical snapshots. Recompute their derived
    # evidence; do not call any financial mutation or publish current orders.
    import stage_validation
    from validation_runtime import validate_result
    operation = request["operation"]
    runtime_needed = (operation in {"news_collect", "news_assess", "industry_prepare", "fund_discover",
                                   "research_prepare", "research_verify", "market_prepare", "allocation_replay"}
                      or operation.startswith("trial_"))
    if operation == "daily_review":
        bundle = artifacts.read_json(result["bundle_ref"])
        runtime_needed = bool(bundle.get("context") or bundle.get("calculation_ref")
                              or (bundle.get("news") or {}).get("collection_manifest_ref"))
    elif operation == "audit" and request["payload"].get("decision_id"):
        bundle = store.get("decision", request["payload"]["decision_id"])
        runtime_needed = bool(bundle and (bundle.get("context") or bundle.get("calculation_ref")
                              or (bundle.get("news") or {}).get("collection_manifest_ref")))
    if runtime_needed:
        import allocation_runtime
        allocation_runtime.ensure_runtime(store.root)
    errors = []
    for attempt in range(1, 4):
        try:
            verify.references(result, artifacts)
            binding_ref = (result.get("automatic_validation") or {}).get("operation_bindings_ref")
            bindings = artifacts.read_json(binding_ref) if binding_ref else {}
            bindings["historical"] = True
            validate_result(stage_validation.validate_operation_result(request, result, store, artifacts, bindings))
            return result
        except LeaseLost:
            raise
        except (ValueError, OSError, RuntimeError) as exc:
            errors.append({"attempt": attempt, "type": type(exc).__name__, "reason": str(exc)})
    raise FinalValidationFailure(errors)


def _publication_deadline(package, artifacts):
    """Existing time boundaries checked again without source IO under writer lock."""
    limits = []
    for item in package.get("writes", ()):
        if item["kind"] != "decision" or not item["value"].get("context"):
            continue
        bundle, context = item["value"], item["value"]["context"]
        spec, market = context["spec"], context["market_ref"]
        limits.append(instant(spec["trade_policy"]["family"]["end_at"]))
        limits += [instant(context["decision_at"])+dt.timedelta(seconds=1800),
                   instant(market["observed_at"])+dt.timedelta(seconds=spec["availability"]["max_market_age_seconds"])]
        boundary = context["risk_state"].get("valid_until")
        if boundary is not None:
            limits.append(instant(boundary))
        limits += [instant(term["observed_at"])+dt.timedelta(seconds=spec["availability"]["max_terms_age_seconds"])
                   for term in market["terms"]]
        news = bundle.get("news") or {}
        if news:
            limits.append(instant(news["information_cutoff_at"])+dt.timedelta(seconds=spec["news"]["max_age_seconds"]))
            collection = artifacts.read_json(news["collection_manifest_ref"])
            limits += [instant(row["retrieved_at"])+dt.timedelta(seconds=spec["news"]["max_age_seconds"])
                       for row in collection["retrievals"]]
        if bundle.get("discovery_ref"):
            discovery = artifacts.read_json(bundle["discovery_ref"])
            limits.append(instant(discovery["valid_until"]))
            active = set(discovery["request"]["news_state"]["active_thesis_ids"])
            theses = [row for row in news.get("industry_theses", []) if row["thesis_id"] in active]
            limits += [instant(row["valid_until"]) for row in theses]
            events = {identity for row in theses for identity in row["event_ids"]}
            limits += [instant(row["review_by"]) for row in news.get("events", []) if row["event_id"] in events]
    return min(limits) if limits else None


def run(request, root, plan):
    request = parse_request(request)
    store = Store(root, plan)
    artifacts = Artifacts(store.base)
    operation_id = request["request_id"]
    existing = store.begin(operation_id, request)
    if existing["status"] == "completed":
        return _completed_review(request, existing["result"], store, artifacts)
    with store.lease(operation_id) as completed:
        if completed is not None:
            return _completed_review(request, completed, store, artifacts)
        try:
            import stage_validation
            from validation_runtime import StageRuntime, StageValidationFailure
            identity = verify.code_identity()
            final_errors = []
            for round_no in range(1, 4):
                round_key = operation_id + ":r" + str(round_no)
                prior_end = store.get("workflow_validation_end", round_key)
                if prior_end and prior_end["status"] == "failed":
                    final_errors.append(prior_end)
                    continue
                runtime = StageRuntime(store, artifacts, code_identity=identity,
                                       validator_contracts=_runtime_contracts(), round_no=round_no)
                store._local.validation_runtime = runtime
                store.put("workflow_validation_start", round_key,
                          {"operation_id": operation_id, "round_no": round_no, "code_identity": identity})
                identity = _step(store, artifacts, operation_id, "code", verify.code_identity, inputs={})
                require(identity == verify.code_identity(), "Source changed during an incomplete operation; use a new revision")
                holder = {}
                def produce_operation(attempt):
                    package = dispatch(request, store, artifacts, producer_id=attempt.namespace)
                    holder["package"] = package
                    return package["result"]
                result = runtime.run("operation_" + request["operation"], {"request": request}, produce_operation,
                    lambda value, frozen: _review_package(request, {**holder["package"], "result": value}, store, artifacts), force=True)
                package = holder["package"]
                if package.get("operation_complete", True) is False:
                    store.fail(operation_id, "retryable", result)
                    return result
                try:
                    proof = _review_package(request, package, store, artifacts)
                    require(identity == verify.code_identity(), "Source changed before publication; use a new revision")
                    manifest_ref = artifacts.put_json(runtime.manifest())
                    bindings_ref = artifacts.put_json(package.get("validation_bindings", {}))
                    checked_result = {**result, "automatic_validation": {
                        "status": proof["status"], "scope": proof["scope"], "round_no": round_no,
                        "max_stage_attempts": 3, "max_final_rounds": 3, "manifest_ref": manifest_ref,
                        "final_review_ref": artifacts.put_json(proof), "operation_bindings_ref": bindings_ref,
                        "core_result_hash": fingerprint(result)}}
                    original_guard = package.get("publication_guard")
                    core_bytes = canonical_bytes(result)
                    checked_bytes = canonical_bytes(checked_result)
                    checked_hash = hashlib.sha256(checked_bytes).hexdigest()
                    frozen_checked = strict_json_loads(checked_bytes.decode("utf-8"))
                    deadline = _publication_deadline(package, artifacts)
                    def guard():
                        verify.references(frozen_checked, artifacts)
                        require(fingerprint(package["result"]) == checked_result["automatic_validation"]["core_result_hash"],
                                "Core result changed after complete workflow verification")
                        if original_guard is not None:
                            original_guard()
                    def commit_guard(prepared_bytes):
                        require(hashlib.sha256(prepared_bytes).hexdigest() == checked_hash,
                                "Pre-encoded result changed before atomic publication")
                        require(hashlib.sha256(core_bytes).hexdigest() == frozen_checked["automatic_validation"]["core_result_hash"],
                                "Core result commitment changed before atomic publication")
                        require(identity == verify.code_identity(), "Source changed during final transaction verification")
                        if deadline is not None and instant(utc_now()) >= deadline:
                            raise StaleSnapshot("A bound publication time window expired during verification")
                    final_writes = list(package.get("writes", ())) + [{"kind": "workflow_validation_end", "key": round_key,
                        "value": {"status": "passed", "operation_id": operation_id, "round_no": round_no,
                                  "final_review_ref": checked_result["automatic_validation"]["final_review_ref"],
                                  "manifest_ref": manifest_ref, "operation_bindings_ref": bindings_ref,
                                  "core_result_hash": fingerprint(result)}, "immutable": True}]
                    result = store.complete(operation_id, frozen_checked, final_writes,
                                            package.get("expected", ()), before_commit=guard, commit_guard=commit_guard)
                except StageValidationFailure:
                    raise
                except LeaseLost:
                    raise
                except (ValueError, OSError, RuntimeError) as exc:
                    failure = {"status": "failed", "operation_id": operation_id, "round_no": round_no,
                               "error_type": type(exc).__name__, "reason": str(exc),
                               "required_actions": getattr(exc, "required_actions", []),
                               "manifest_ref": artifacts.put_json(runtime.manifest()),
                               "result_ref": artifacts.put_json(result)}
                    store.put("workflow_validation_end", round_key, failure)
                    final_errors.append(failure)
                    continue
                break
            else:
                raise FinalValidationFailure(final_errors)
        except LeaseLost:
            raise
        except (ValueError, OSError, RetryableError) as exc:
            original_failure = exc
            from validation_runtime import StageValidationFailure
            if (isinstance(exc, StageValidationFailure) and exc.failure_phase == "validator"
                    and isinstance(getattr(exc.cause, "__cause__", None), StaleSnapshot)):
                exc = exc.cause.__cause__
                exc.validation_failure = original_failure.as_dict()
            if (isinstance(exc, StageValidationFailure) and exc.failure_phase == "producer"
                    and isinstance(exc.cause, (ValueError, OSError, RetryableError))):
                exc = exc.cause
                exc.validation_failure = original_failure.as_dict()
            status = getattr(exc, "code", "retryable" if isinstance(exc, OSError) else "invalid_input")
            failure = {"status": status, "reason": str(exc)}
            if getattr(exc, "required_actions", None):
                failure["required_actions"] = exc.required_actions
            store.fail(operation_id, status, failure)
            concurrent = store.operation(operation_id)
            if concurrent["status"] == "completed":
                return _completed_review(request, concurrent["result"], store, artifacts)
            if exc is not original_failure:
                raise exc from original_failure
            raise
        finally:
            store._local.validation_runtime = None
    store.maintain()
    return result
