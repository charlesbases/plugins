"""独立重建整笔交易及派生结果；不导入producer或timing。"""
from copy import deepcopy
from decimal import Decimal

from investment.contracts import (ContractError, INPUT_RULES, OUTPUT_RULES, SPEC, USES, artifact, bounds, canonical, dataset, decimal_text, definitely_after, digest, exact, instant, is_known, known, na, now, parse, require, unknown)


def load_source_input(input_ref, resolver, run_id, invocation_id, frozen=None):
    schemas = resolver.schemas
    incoming = resolver.resolve(input_ref, "investment.stage_input")
    require(incoming["stage_id"] == "SC01" and incoming["run_id"] == run_id and incoming["invocation_id"] == invocation_id, "I01", "/run_id", "Input identity mismatch", "identity_mismatch")
    roles = {item["role"]: item["ref"] for item in incoming["input_refs"]}
    require(len(incoming["input_refs"]) == 3 and set(roles) == {"transaction_facts_ref", "changes_ref", "task_scope_ref"}, "I01", "/input_refs", "Exactly three registered input roles required")
    require(incoming["scope_ref"] == roles["task_scope_ref"], "I01", "/scope_ref", "Scope role mismatch")
    facts = resolver.resolve(roles["transaction_facts_ref"], "investment.transaction_facts")
    source = resolver.resolve(facts["source_record_ref"], "investment.source_record")
    changes = resolver.resolve(roles["changes_ref"], "investment.trade_changes")
    scope = resolver.resolve(incoming["scope_ref"], "investment.task_scope")
    baseline = resolver.resolve(frozen["baseline_ref"], "investment.ledger_baseline") if frozen else resolver.current(source["account_id"])
    cutoff = frozen["created_at"] if frozen else now()
    meta = baseline["meta"]
    generation = meta["generation"] if meta else 0
    require(meta is not None or (not source["changes"]["rows"] and not source["arrival_replies"]["rows"]), "I03", "/changes", "Initialization must confirm an empty opening ledger")
    require(source["base_generation"] == facts["base_generation"] == generation, "I02", "/base_generation", "Ledger generation mismatch", "parent_changed")
    require(source["changes"] == changes, "I03", "/changes", "Change projection mismatch", "source_mismatch")
    require(scope["account_id"] == source["account_id"] and scope["operation"] == source["operation"] == ("refresh" if meta else "initialize"), "I03", "/operation", "Source scope mismatch", "source_mismatch")
    require(source["source_type"] == "user_confirmation" or resolver.fixture_mode, "I03", "/source_type", "Fixture authority requires fixture mode", "use_not_allowed")
    require(scope["purpose"] == ("fixture_validation" if source["source_type"] == "synthetic_fixture" else "account_review"), "I03", "/purpose", "Source authority mismatch")
    if meta:
        for key, source_key in (("account_id", "account_id"), ("platform", "platform"), ("currency", "currency"), ("opening", "opening"), ("source_authority", "source_type")):
            require(meta[key] == source[source_key], "I03", "/" + source_key, "Ledger identity or authority changed", "source_mismatch")
        require(instant(source["confirmed_at"]) >= instant(meta["known_as_of"]) and instant(source["transaction_as_of"]) >= instant(meta["transaction_as_of"]), "I04", "/confirmed_at", "User fact clocks moved backwards", "date_order")
    times = [source["opening"]["start_at"], source["transaction_as_of"], source["confirmed_at"], incoming["information_cutoff_at"], cutoff]
    require(all(instant(a) <= instant(b) for a, b in zip(times, times[1:])), "I04", "/confirmed_at", "Invalid source time order", "date_order")
    require(scope["transaction_as_of_target"] == source["transaction_as_of"] and scope["account_information_cutoff_at"] == incoming["information_cutoff_at"], "I04", "/information_cutoff_at", "Scope cutoff mismatch")
    method_ref = artifact((SPEC / "method.json").read_bytes(), "investment.method", "investment-spec://v1/method.json")
    require(resolver.resolve(method_ref) == schemas.method, "I05", "/method_ref", "Unregistered method")
    params = resolver.resolve(incoming["parameters_ref"], "investment.parameter_set")
    expected = {"max_attempts": (3, "count", "engineering_budget"), "max_json_bytes": (10485760, "count", "engineering_budget"), "decimal_precision": (80, "count", "engineering_budget"), "execution_timezone": ("Asia/Shanghai", "code", "registered_business_rule")}
    require({p["name"] for p in params["parameters"]} == set(expected), "I05", "/parameters", "Parameter set mismatch")
    for param in params["parameters"]:
        require((param["value"], param["unit"], param["source_kind"]) == expected[param["name"]] and param["basis_ref"] == method_ref and param["determination_rule"] == "constant_v1" and instant(param["determined_at"]) <= instant(cutoff), "I05", "/parameters", "Parameter contract mismatch")
    bundle = {"input": incoming, "input_ref": input_ref, "source": source, "source_ref": facts["source_record_ref"], "baseline": baseline, "baseline_ref": frozen["baseline_ref"] if frozen else None, "scope": scope, "method_ref": method_ref, "cutoff": cutoff}
    validate_replies(source, baseline, cutoff)
    merged = verify_merge(source, baseline, cutoff)
    try:
        verify_trades(merged, source)
    except ContractError as error:
        # 合并表的下标不是SourceRecord下标，失败必须指向实际输入对象。
        parts = error.pointer.split("/")
        if len(parts) > 3 and parts[1:3] == ["trades", "rows"] and parts[3].isdigit():
            tid = merged[int(parts[3])]["trade_id"]
            index = next((i for i, m in enumerate(source["changes"]["rows"]) if m["trade"]["trade_id"] == tid), None)
            suffix = "/".join(parts[4:])
            if index is not None:
                error.object_ref = facts["source_record_ref"]
                error.pointer = f"/changes/rows/{index}/trade" + ("/" + suffix if suffix else "")
            elif frozen:
                error.object_ref = frozen["baseline_ref"]
                index = next(i for i, t in enumerate(baseline["trades"]["rows"]) if t["trade_id"] == tid)
                error.pointer = f"/trades/rows/{index}" + ("/" + suffix if suffix else "")
        elif error.pointer == "/trades/rows" and source["changes"]["rows"]:
            error.object_ref, error.pointer = facts["source_record_ref"], "/changes/rows"
        raise
    return bundle


def load_input(input_ref, resolver, run_id, invocation_id):
    frozen = resolver.frozen_invocation(input_ref, run_id, invocation_id)
    require(frozen["parameters_ref"] == resolver.resolve(input_ref)["parameters_ref"], "I07", "/parameters_ref", "Frozen parameter binding changed")
    bundle = load_source_input(input_ref, resolver, run_id, invocation_id, frozen)
    require(bundle["method_ref"] == frozen["method_ref"] and bundle["source"]["base_generation"] == frozen["expected_generation"], "I07", "/base_generation", "Frozen method or baseline binding changed")
    return bundle


def validate_replies(source, baseline, cutoff):
    trades = {t["trade_id"]: t for t in baseline["trades"]["rows"]}
    trades.update({m["trade"]["trade_id"]: m["trade"] for m in source["changes"]["rows"]})
    seen = set()
    for index, reply in enumerate(source["arrival_replies"]["rows"]):
        p = f"/arrival_replies/rows/{index}"
        tid = reply["trade_id"]
        require(tid in trades and tid not in seen, "A13", p, "Reply trade missing or duplicated", "duplicate_key")
        seen.add(tid)
        trade = trades[tid]
        require(trade["confirmation"]["state"] not in ("cancelled", "failed"), "A13", p, "Terminal trade cannot receive arrival reply")
        if reply["result"] == "no_reply":
            require(reply["reported_at"]["status"] == reply["new_expected_at"]["status"] == "not_applicable", "A13", p, "No reply cannot carry report or forecast")
            continue
        require(is_known(reply["reported_at"]), "A13", p + "/reported_at", "Explicit report time required")
        report_time = instant(reply["reported_at"]["value"])
        require(report_time <= instant(source["confirmed_at"]) <= instant(cutoff), "A13", p + "/reported_at", "Reply after source or cutoff", "date_order")
        last = trade["last_arrival_reply"]
        require(not is_known(last) or report_time >= instant(last["value"]["reported_at"]), "A13", p, "Reply report time moved backwards", "date_order")
        if reply["result"] == "completed":
            require(reply["new_expected_at"]["status"] == "not_applicable", "A13", p, "Completed reply cannot carry forecast")
        else:
            phase = "confirmation" if trade["side"] == "buy" else "settlement"
            require(trade[phase]["basis"] != "user_reported", "A13", p, "Actual completion requires explicit trade correction before denial")
            forecast = bounds(reply["new_expected_at"])
            require(forecast is None or forecast[0] > report_time, "A13", p + "/new_expected_at", "New forecast must follow report", "date_order")


def verify_merge(source, baseline, cutoff):
    trades = {t["trade_id"]: deepcopy(t) for t in baseline["trades"]["rows"]}
    seen, change_ids = set(), set()
    for index, change in enumerate(source["changes"]["rows"]):
        trade = change["trade"]; tid = trade["trade_id"]; old = trades.get(tid); p = f"/changes/rows/{index}"
        require(tid not in seen and change["change_id"] not in change_ids, "I06", p, "Duplicate mutation", "duplicate_key")
        seen.add(tid); change_ids.add(change["change_id"])
        if change["mode"] == "create":
            require(old is None and change["expected_trade_hash"]["status"] == "not_applicable", "I06", p, "Create requires new stable trade ID")
            require(trade["last_arrival_reply"]["status"] == "unknown" and all(trade[n]["basis"] != "expected_due" for n in ("confirmation", "settlement")), "I06", p, "New user input cannot manufacture system metadata")
        else:
            require(old is not None and is_known(change["expected_trade_hash"]) and change["expected_trade_hash"]["value"] == digest(canonical(old)), "I06", p + "/expected_trade_hash", "Old trade hash mismatch", "parent_changed")
            require(trade["instrument_id"] == old["instrument_id"] and trade["side"] == old["side"], "I06", p, "Trade identity changed")
            require(trade["last_arrival_reply"] == old["last_arrival_reply"], "I06", p, "Latest arrival reply is system maintained")
            if change["mode"] == "update":
                for field in ("amount", "confirmed_shares", "requested_shares", "fee_amount", "trade_at", "holding_start"):
                    require(not is_known(old[field]) or trade[field] == old[field], "I06", p + "/trade/" + field, "Known actual fact conflict requires correction")
                for name in ("confirmation", "settlement"):
                    previous, stage = old[name], trade[name]
                    require(not is_known(previous["actual_at"]) or previous["actual_at"] == stage["actual_at"], "I06", p, "Known actual time conflict requires correction")
                    if previous["basis"] == "user_reported" or previous["state"] in ("cancelled", "failed"):
                        require(stage["state"] == previous["state"] and stage["basis"] == previous["basis"], "I06", p, "Actual lifecycle cannot be withdrawn by ordinary update")
                    if previous["basis"] == "expected_due" and stage["basis"] == "user_reported":
                        require(is_known(stage["actual_at"]) or (name == "confirmation" and is_known(trade["confirmed_shares"]) and not is_known(old["confirmed_shares"])), "I06", p, "Phase promotion needs actual confirmation or explicit arrival reply")
            require(change["mode"] != "correct" or (is_known(change["correction_reason"]) and change["correction_reason"]["value"].strip()), "I06", p + "/correction_reason", "Correction reason required")
        require(change["mode"] == "correct" or change["correction_reason"]["status"] == "not_applicable", "I06", p, "Ordinary mutation cannot carry correction reason")
        trades[tid] = deepcopy(trade)
    return [trades[key] for key in sorted(trades)]


def rebuild_timing(rows, source, cutoff):
    result = deepcopy(rows)
    replies = {r["trade_id"]: r for r in source["arrival_replies"]["rows"] if r["result"] != "no_reply"}
    for t in result:
        if t["confirmation"]["state"] in ("cancelled", "failed"):
            continue
        r = replies.get(t["trade_id"])
        if r:
            t["last_arrival_reply"] = known({"result": r["result"], "reported_at": r["reported_at"]["value"]})
        denied = r and r["result"] == "not_completed"
        if denied:
            for key in ["confirmation", "settlement"]:
                if t[key]["basis"] == "expected_due":
                    t[key].update(state="pending", basis="not_applicable", actual_at=unknown())
            forecast = r["new_expected_at"]
            if forecast["status"] in ("known", "unknown"):
                t["expected_at"] = deepcopy(forecast)
            elif is_known(t["expected_at"]) and bounds(t["expected_at"])[0] <= instant(cutoff):
                t["expected_at"] = unknown("withdrawn_forecast")
        else:
            explicit = r is not None and r["result"] == "completed"
            expired = is_known(t["expected_at"]) and bounds(t["expected_at"])[0] <= instant(cutoff)
            if not (explicit or expired):
                continue
            stages = ["confirmation"] + (["settlement"] if t["side"] == "sell" else [])
            for name in stages:
                phase = t[name]
                if phase["state"] == "pending":
                    phase["state"] = "confirmed" if name == "confirmation" else "settled"
                    phase["actual_at"] = unknown()
                    phase["basis"] = "user_reported" if explicit else "expected_due"
                elif explicit and phase["basis"] == "expected_due":
                    phase["basis"] = "user_reported"
    return result


def verify_trades(rows, source):
    seen = set()
    for index, t in enumerate(rows):
        p = f"/trades/rows/{index}"
        require(t["trade_id"] not in seen, "A01", p, "Duplicate stable trade ID", "duplicate_key")
        seen.add(t["trade_id"])
        confirmation, settlement = t["confirmation"], t["settlement"]
        cstate, sstate = confirmation["state"], settlement["state"]
        for field in ("amount", "confirmed_shares", "requested_shares", "fee_amount"):
            if is_known(t[field]):
                require(Decimal(t[field]["value"]) >= 0 if field == "fee_amount" else Decimal(t[field]["value"]) > 0, "A07", p + "/" + field, "Actual amount and shares must be positive; fee may be zero")
        if t["side"] == "buy":
            require(cstate in ("cancelled", "failed") or t["holding_start"]["status"] != "not_applicable", "A05", p + "/holding_start", "Live buy holding start must be known or explicitly unknown")
            require(sstate == "not_applicable" and settlement["actual_at"]["status"] == "not_applicable" and settlement["basis"] == "not_applicable" and t["requested_shares"]["status"] == "not_applicable", "A03", p, "Buy cannot carry cash receipt or redemption request")
            require(not (is_known(t["fee_amount"]) and is_known(t["amount"])) or Decimal(t["fee_amount"]["value"]) <= Decimal(t["amount"]["value"]), "A07", p, "Buy fee exceeds total actually paid")
        else:
            require(t["holding_start"]["status"] == "not_applicable", "A05", p + "/holding_start", "Sale has no new holding start")
            require(sstate != "not_applicable" or cstate in ("cancelled", "failed"), "A04", p, "Live sale requires pending or settled cash state")
            require(sstate != "settled" or cstate == "confirmed", "A04", p, "Cash completion requires confirmation")
        for name, complete in (("confirmation", "confirmed"), ("settlement", "settled")):
            phase = t[name]
            if phase["state"] == complete:
                require(phase["basis"] in ("user_reported", "expected_due") and phase["actual_at"]["status"] != "not_applicable", "A04", p + "/" + name, "Completed phase requires independent completion basis")
                require(phase["basis"] != "expected_due" or phase["actual_at"]["status"] == "unknown", "A14", p + "/" + name, "Forecast cannot invent actual time")
            else:
                require(phase["basis"] == ("user_reported" if phase["state"] in ("cancelled", "failed") else "not_applicable") and not is_known(phase["actual_at"]), "A04", p + "/" + name, "Inactive phase cannot carry completion or actual time")
        require(cstate == "confirmed" or not is_known(t["confirmed_shares"]), "A05", p + "/confirmed_shares", "Unconfirmed trade cannot carry actual confirmed shares")
        require(cstate in ("cancelled", "failed") or t["confirmed_shares"]["status"] != "not_applicable", "A05", p + "/confirmed_shares", "Live trade shares must be known or explicitly unknown")
        require(confirmation["basis"] != "expected_due" or not is_known(t["confirmed_shares"]), "A14", p + "/confirmed_shares", "Due cannot convert requested shares into actual shares")
        if cstate in ("cancelled", "failed"):
            require(sstate == "not_applicable", "A03", p, "Cancelled or failed whole trade cannot remain payable")
        opening = known({"kind": "instant", "value": source["opening"]["start_at"]})
        require(not definitely_after(t["trade_at"], t["expected_at"]), "A08", p + "/expected_at", "Expected completion precedes actual trade", "date_order")
        for field, value in (("trade_at", t["trade_at"]), ("holding_start", t["holding_start"]), ("confirmation/actual_at", confirmation["actual_at"]), ("settlement/actual_at", settlement["actual_at"])):
            require(not definitely_after(opening, value) and (not is_known(value) or bounds(value)[0] <= instant(source["transaction_as_of"])), "A08", p + "/" + field, "Actual time outside declared transaction coverage", "date_order")
        require(not definitely_after(t["trade_at"], confirmation["actual_at"]) and not definitely_after(confirmation["actual_at"], settlement["actual_at"]) and not definitely_after(t["trade_at"], t["holding_start"]), "A08", p, "Actual lifecycle time order invalid", "date_order")
    for instrument in {t["instrument_id"] for t in rows}:
        confirmed = [t for t in rows if t["instrument_id"] == instrument and t["confirmation"]["state"] == "confirmed"]
        if all(is_known(t["confirmed_shares"]) for t in confirmed):
            total = exact([("-" if t["side"] == "sell" else "") + t["confirmed_shares"]["value"] for t in confirmed])
            requested = [t["requested_shares"]["value"] for t in rows if t["instrument_id"] == instrument and t["side"] == "sell" and t["confirmation"]["state"] == "pending" and is_known(t["requested_shares"])]
            require(total >= 0 and exact(requested) <= total, "A09", "/trades/rows", "Confirmed holdings do not support reported redemption")


def expected_derived(rows):
    positions, pending, gaps = [], [], []
    groups = {}
    for t in rows:
        if t["confirmation"]["state"] == "confirmed":
            groups.setdefault(t["instrument_id"], []).append(t)
    for instrument in sorted(groups):
        trades = groups[instrument]
        missing = any(not is_known(t["confirmed_shares"]) for t in trades)
        net = unknown("share_confirmation_missing") if missing else known(decimal_text(exact([("-" if t["side"] == "sell" else "") + t["confirmed_shares"]["value"] for t in trades])))
        positions.append({"instrument_id": instrument, "confirmed_shares": net, "trade_ids": sorted(t["trade_id"] for t in trades), "coverage": "recorded_transactions_since_start"})
    for i, t in enumerate(rows):
        if t["confirmation"]["state"] == "pending":
            phase = "confirmation"
        elif t["confirmation"]["state"] == "confirmed" and t["settlement"]["state"] == "pending":
            phase = "settlement"
        else:
            phase = None
        if phase:
            pending.append({"trade_id": t["trade_id"], "instrument_id": t["instrument_id"], "side": t["side"], "pending_phase": phase, "expected_at": t["expected_at"]})
        for field in ("confirmed_shares", "amount", "fee_amount", "holding_start"):
            if t[field]["status"] != "unknown" or field == "confirmed_shares" and t["confirmation"]["state"] != "confirmed":
                continue
            gaps.append({"rule_id": "A11", "field_or_object": f"/trades/rows/{i}/{field}", "reason": "not_provided", "owner": "user", "required_evidence": ["share_confirmation" if field == "confirmed_shares" else "transaction_confirmation"], "affected_uses": ["current_account_review" if field == "confirmed_shares" else "decision_advice"]})
    return {"trades": {"rows": rows}, "positions": {"rows": positions}, "pending_transactions": {"rows": pending}}, gaps


def verify_output(bundle, candidate_ref, resolver):
    schemas = resolver.schemas
    candidate = resolver.resolve(candidate_ref, "investment.candidate_output")
    incoming = bundle["input"]
    require(candidate["run_id"] == incoming["run_id"] and candidate["stage_id"] == "SC01" and candidate["input_refs"] == incoming["input_refs"] and candidate["scope_ref"] == incoming["scope_ref"] and candidate["method_ref"] == bundle["method_ref"], "A01", "/input_refs", "Candidate binding mismatch")
    require(instant(candidate["created_at"]) >= instant(bundle["cutoff"]), "A01", "/created_at", "Candidate predates execution")
    context = resolver.resolve(candidate["payload_ref"], "investment.transaction_context")
    tables = {name: resolver.resolve(context[name + "_ref"], "investment." + name) for name in ("trades", "positions", "pending_transactions")}
    merged = verify_merge(bundle["source"], bundle["baseline"], bundle["cutoff"])
    expected_rows = rebuild_timing(merged, bundle["source"], bundle["cutoff"])
    require(tables["trades"]["rows"] == expected_rows, "A14", "/trades", "Whole-trade transition differs from independent reconstruction")
    verify_trades(expected_rows, bundle["source"])
    expected, gaps = expected_derived(expected_rows)
    require(tables == expected, "A10", "/positions", "Derived positions or pending differs")
    source, meta = bundle["source"], bundle["baseline"]["meta"]
    changed = meta is None or expected["trades"] != bundle["baseline"]["trades"]
    prior = {t["trade_id"]: t for t in merged}
    user_changed = meta is None or merged != bundle["baseline"]["trades"]["rows"] or any(r["result"] != "no_reply" and prior[r["trade_id"]]["last_arrival_reply"] != known({"result": r["result"], "reported_at": r["reported_at"]["value"]}) for r in source["arrival_replies"]["rows"])
    scope = {"account_id": source["account_id"], "platform": source["platform"], "currency": source["currency"], "opening": source["opening"], "source_authority": source["source_type"], "known_as_of": source["confirmed_at"] if user_changed else meta["known_as_of"], "transaction_as_of": source["transaction_as_of"] if user_changed else meta["transaction_as_of"]}
    generation = (meta["generation"] if meta else 0) + int(changed)
    expected_context = dict(scope, generation=generation, ledger_digest=digest(canonical({"scope": scope, "trades": expected["trades"]})), execution_cutoff_at=bundle["cutoff"], trade_count=len(expected_rows), missing_facts=gaps)
    prefix = candidate_ref["uri"].rsplit("/", 1)[0] + "/"
    for name in expected:
        expected_context[name + "_ref"] = dataset(expected[name], "investment." + name, prefix + name + ".json", schemas)
    require(context == expected_context and candidate["gaps"] == gaps, "A11", "/payload_ref", "Context facts, clocks, references or gaps differ")
    require(candidate["payload_ref"] == artifact(canonical(context), "investment.transaction_context", prefix + "context.json") and candidate["datasets"] == [context[name + "_ref"] for name in expected] and candidate["evidence_refs"] == [bundle["source_ref"], bundle["baseline_ref"]], "A12", "/datasets", "Candidate output references differ")
    return {"context": context, "source_record": source, "business_objects": tables}


def report(phase, input_ref, resolver, identity, candidate_ref=None):
    rules = INPUT_RULES if phase == "input" else OUTPUT_RULES
    failure, bundle = None, None
    try:
        bundle = load_input(input_ref, resolver, identity["run_id"], identity["invocation_id"])
        if phase == "output":
            verify_output(bundle, candidate_ref, resolver)
    except ContractError as error:
        failure = error
    checks = [{"rule_id": rule, "verdict": "passed" if failure is None else "failed" if rule == failure.rule else "not_evaluated", "reason": "" if failure is None else failure.reason if rule == failure.rule else "prerequisite_failed", "evidence_refs": [input_ref]} for rule in rules]
    if failure and failure.rule not in rules:
        checks.append({"rule_id": failure.rule, "verdict": "failed", "reason": failure.reason, "evidence_refs": [input_ref]})
    base = {"validator_id": resolver.schemas.registry["validator_id"], "validator_version": "v1", "run_id": identity["run_id"], "stage_id": "SC01", "validated_at": now(), "checks": checks, "overall_verdict": "failed" if failure else "passed"}
    if phase == "input":
        result = dict(base, schema_version=1, check_id=f"input-{identity['invocation_id']}-{identity['attempt_no']}", invocation_id=identity["invocation_id"], attempt_no=identity["attempt_no"], input_refs=[input_ref])
        resolver.schemas.validate("investment.input_check", result)
    else:
        fixture = bundle is not None and bundle["source"]["source_type"] == "synthetic_fixture"
        allowed = [] if failure else list(USES)
        if not failure:
            state = resolver.resolve(candidate_ref)
            if fixture or any("current_account_review" in gap["affected_uses"] for gap in state["gaps"]):
                allowed = ["reporting"]
        result = dict(base, input_refs=bundle["input"]["input_refs"] if bundle else [{"role": "stage_input", "ref": input_ref}], candidate_output_ref=candidate_ref, business_result="rejected" if failure else "observation_only" if fixture else "usable", allowed_uses=allowed, blockers=[])
        resolver.schemas.validate("investment.validation_receipt", result)
    return result, failure
