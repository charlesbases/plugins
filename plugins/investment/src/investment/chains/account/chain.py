"""SC01生产：稳定交易ID归并、整笔完成策略、运行时派生输出。"""
from copy import deepcopy

from investment.contracts import SPEC, artifact, canonical, dataset, decimal_text, digest, exact, instant, is_known, known, now, require, unknown
from investment.chains.account.timing import apply_due_policy


def load_process_input(input_ref, resolver, run_id, invocation_id, frozen=None):
    """流程独立装载输入；不调用校验器的业务归并、状态重建或派生函数。"""
    incoming = resolver.resolve(input_ref, "investment.stage_input")
    require((incoming["stage_id"], incoming["run_id"], incoming["invocation_id"]) == ("SC01", run_id, invocation_id), "I01", "/run_id", "Process input identity mismatch", "identity_mismatch")
    roles = {r["role"]: r["ref"] for r in incoming["input_refs"]}
    require(len(incoming["input_refs"]) == 3 and set(roles) == {"transaction_facts_ref", "changes_ref", "task_scope_ref"} and roles["task_scope_ref"] == incoming["scope_ref"], "I01", "/input_refs", "Process requires the three formal input roles")
    facts = resolver.resolve(roles["transaction_facts_ref"], "investment.transaction_facts")
    source = resolver.resolve(facts["source_record_ref"], "investment.source_record")
    scope = resolver.resolve(incoming["scope_ref"], "investment.task_scope")
    changes = resolver.resolve(roles["changes_ref"], "investment.trade_changes")
    baseline = resolver.resolve(frozen["baseline_ref"], "investment.ledger_baseline") if frozen else resolver.current(source["account_id"])
    meta = baseline["meta"]
    generation = meta["generation"] if meta else 0
    require(source["base_generation"] == facts["base_generation"] == generation, "I02", "/base_generation", "Process baseline generation mismatch", "parent_changed")
    require(source["changes"] == changes and scope["account_id"] == source["account_id"] and scope["operation"] == source["operation"] == ("refresh" if meta else "initialize"), "I03", "/changes", "Process input projections disagree", "source_mismatch")
    require(source["source_type"] == "user_confirmation" or resolver.fixture_mode, "I03", "/source_type", "Fixture input requires fixture mode", "use_not_allowed")
    method_ref = artifact((SPEC / "method.json").read_bytes(), "investment.method", "investment-spec://v1/method.json")
    resolver.resolve(method_ref, "investment.method")
    parameters = resolver.resolve(incoming["parameters_ref"], "investment.parameter_set")
    expected = {"max_attempts": (3, "count", "engineering_budget"), "max_json_bytes": (10485760, "count", "engineering_budget"), "decimal_precision": (80, "count", "engineering_budget"), "execution_timezone": ("Asia/Shanghai", "code", "registered_business_rule")}
    cutoff = frozen["created_at"] if frozen else now()
    require({p["name"] for p in parameters["parameters"]} == set(expected), "I05", "/parameters", "Process parameter set mismatch")
    for parameter in parameters["parameters"]:
        require((parameter["value"], parameter["unit"], parameter["source_kind"]) == expected[parameter["name"]] and parameter["basis_ref"] == method_ref and parameter["determination_rule"] == "constant_v1" and instant(parameter["determined_at"]) <= instant(cutoff), "I05", "/parameters", "Process parameter contract mismatch")
    if frozen:
        require(frozen["parameters_ref"] == incoming["parameters_ref"] and frozen["method_ref"] == method_ref and frozen["expected_generation"] == generation, "I07", "/parameters_ref", "Process frozen bindings changed")
    return {"input": incoming, "input_ref": input_ref, "source": source, "source_ref": facts["source_record_ref"], "baseline": baseline, "baseline_ref": frozen["baseline_ref"] if frozen else None, "scope": scope, "method_ref": method_ref, "cutoff": cutoff}


def produce(bundle, schemas, prefix, output_id):
    source, baseline = bundle["source"], bundle["baseline"]
    trades = {t["trade_id"]: deepcopy(t) for t in baseline["trades"]["rows"]}
    for change in source["changes"]["rows"]:
        trades[change["trade"]["trade_id"]] = deepcopy(change["trade"])
    rows = apply_due_policy([trades[key] for key in sorted(trades)], source["arrival_replies"]["rows"], bundle["cutoff"])
    positions, pending, gaps = [], [], []
    instruments = sorted({t["instrument_id"] for t in rows if t["confirmation"]["state"] == "confirmed"})
    for instrument in instruments:
        group = [t for t in rows if t["instrument_id"] == instrument and t["confirmation"]["state"] == "confirmed"]
        shares = unknown("share_confirmation_missing")
        if all(is_known(t["confirmed_shares"]) for t in group):
            shares = known(decimal_text(exact([("-" if t["side"] == "sell" else "") + t["confirmed_shares"]["value"] for t in group])))
        positions.append({"instrument_id": instrument, "confirmed_shares": shares, "trade_ids": sorted(t["trade_id"] for t in group), "coverage": "recorded_transactions_since_start"})
    for index, trade in enumerate(rows):
        phase = "confirmation" if trade["confirmation"]["state"] == "pending" else "settlement" if trade["settlement"]["state"] == "pending" and trade["confirmation"]["state"] == "confirmed" else None
        if phase:
            pending.append({"trade_id": trade["trade_id"], "instrument_id": trade["instrument_id"], "side": trade["side"], "pending_phase": phase, "expected_at": trade["expected_at"]})
        for field, evidence, uses in (("confirmed_shares", "share_confirmation", ["current_account_review"]), ("amount", "transaction_confirmation", ["decision_advice"]), ("fee_amount", "transaction_confirmation", ["decision_advice"]), ("holding_start", "transaction_confirmation", ["decision_advice"])):
            if trade[field]["status"] == "unknown" and (field != "confirmed_shares" or trade["confirmation"]["state"] == "confirmed"):
                gaps.append({"rule_id": "A11", "field_or_object": f"/trades/rows/{index}/{field}", "reason": "not_provided", "owner": "user", "required_evidence": [evidence], "affected_uses": uses})
    business = {"trades": {"rows": rows}, "positions": {"rows": positions}, "pending_transactions": {"rows": pending}}
    meta = baseline["meta"]
    changed = meta is None or business["trades"] != baseline["trades"]
    user_changed = meta is None or [trades[key] for key in sorted(trades)] != baseline["trades"]["rows"] or any(r["result"] != "no_reply" and trades[r["trade_id"]]["last_arrival_reply"] != known({"result": r["result"], "reported_at": r["reported_at"]["value"]}) for r in source["arrival_replies"]["rows"])
    scope = {"account_id": source["account_id"], "platform": source["platform"], "currency": source["currency"], "opening": source["opening"], "source_authority": source["source_type"], "known_as_of": source["confirmed_at"] if user_changed else meta["known_as_of"], "transaction_as_of": source["transaction_as_of"] if user_changed else meta["transaction_as_of"]}
    generation = (meta["generation"] if meta else 0) + int(changed)
    context = dict(scope, generation=generation, ledger_digest=digest(canonical({"scope": scope, "trades": business["trades"]})), execution_cutoff_at=bundle["cutoff"], trade_count=len(rows), missing_facts=gaps)
    for name in business:
        sid = "investment." + name
        context[name + "_ref"] = dataset(business[name], sid, prefix + name + ".json", schemas)
    state = {"context": context, "source_record": source, "business_objects": business}
    candidate = {"contract_id": "investment.candidate_output", "contract_version": 1, "run_id": bundle["input"]["run_id"], "stage_id": "SC01", "output_id": output_id, "created_at": now(), "processing_state": "completed", "input_refs": bundle["input"]["input_refs"], "scope_ref": bundle["input"]["scope_ref"], "method_ref": bundle["method_ref"], "payload_ref": artifact(canonical(context), "investment.transaction_context", prefix + "context.json"), "datasets": [context[name + "_ref"] for name in business], "evidence_refs": [bundle["source_ref"], bundle["baseline_ref"]], "gaps": gaps}
    return state, candidate
