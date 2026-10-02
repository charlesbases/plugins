"""Confirmed capital and immutable, effective-dated principal-loss preferences."""
from decimal import Decimal, localcontext

import ledger
from contracts import ContractError, decimal_string, fields, fingerprint, instant, require, utc_now

PLAN_CONSTRAINT_FIELDS = {"platform", "currency", "goal", "excluded_categories", "position_limits"}


class PrincipalPolicyRequired(ContractError):
    code = "confirmation_required"

    def __init__(self):
        super().__init__("Net contributed capital is nonpositive; recorded withdrawals and investment history remain intact")
        self.required_actions = [{"action": "confirm_residual_asset_risk_policy",
                                  "reason": "Confirm an independent remaining-asset loss budget and linked new episode; do not invent contributed principal"}]


class CapitalReconciliationRequired(ContractError):
    code = "confirmation_required"

    def __init__(self, pending, baseline, account):
        super().__init__("A source correction affects contributed principal; confirm the corrected baseline before risk calculation")
        self.required_actions = [{"action": "capital_reconcile", "revision_ids": pending["revision_ids"],
                                  "previous_baseline_hash": fingerprint(baseline), "account_hash": fingerprint(account)}]


def _source(value, at):
    fields(value, {"message", "confirmed_at"}, label="user confirmation")
    require(isinstance(value["message"], str) and value["message"].strip(), "User confirmation text required")
    require(instant(value["confirmed_at"]) <= instant(at), "User confirmation is in the future")


def _rate(value):
    result = Decimal(decimal_string(value, "principal loss tolerance"))
    require(0 <= result < 1, "Principal loss tolerance must be at least zero and below one")
    return result


def _revisions(store, pointer, conn=None):
    result, identity = [], pointer["revision_id"]
    for _ in range(pointer["revision_count"]):
        row = store.get("risk_revision", identity, conn)
        require(row is not None and fingerprint(row) == identity, "Risk revision lineage differs")
        result.append(row)
        identity = row["previous_revision"]
    require(identity is None, "Risk revision count differs")
    return list(reversed(result))


def status(store, account_id="main"):
    pointer = store.get("risk_profile", account_id)
    baseline = store.get("capital_current", account_id)
    if pointer is None or baseline is None:
        result = {"status": "confirmation_required", "profile_hash": None, "required_inputs": [
            {"field": "principal", "question": "本次计划可投资的本金是多少？"},
            {"field": "loss_tolerance", "question": "相对投入本金，最多能接受多大比例的亏损？"},
            {"field": "account_confirmation", "question": "这些本金目前是可用现金，还是已经包含持仓、待确认交易或待到账款项？"}]}
        account = store.get("account", account_id)
        if account and account["sequence"] > 0 and Decimal(account["cash"]) == 0 and Decimal(account["units"]) == 0 and not any(
                account[name] for name in ("lots", "orders", "receivables", "pending_subscriptions", "transfers", "performance_pending")):
            result["required_inputs"].append({"field": "confirmed_no_prior_economic_activity",
                "question": "是否明确确认这是本计划第一次实际资金活动，之前没有投入、交易、损益或在途？预算不等于已到账现金。"})
        return result
    _revisions(store, pointer)
    pending = store.get("capital_reconciliation", account_id)
    if pending is not None and not pending["resolved"]:
        return {"status": "confirmation_required", "profile_hash": fingerprint(pointer), "capital_baseline": baseline,
                "required_inputs": [{"field": "capital_reconciliation", "question": "请根据更正后的原始资金流水核对投入本金；历史本金和亏损记录会完整保留。"}],
                "capital_reconciliation": pending}
    active, _ = _effective(_revisions(store, pointer), utc_now())
    return {"status": "confirmed", "profile_hash": fingerprint(pointer), "capital_baseline": baseline,
            "effective_loss_tolerance": active["loss_tolerance"],
            "plan_constraints": store.get("plan_constraints", account_id),
            "required_inputs": []}


def initialize(payload, store, operation_id):
    fields(payload, {"principal", "currency", "loss_tolerance", "as_of", "user_source",
                     "confirmed_initial_all_cash", "account_hash"}, {"confirmed_no_prior_economic_activity"}, label="profile initialization")
    at = utc_now()
    _source(payload["user_source"], at)
    principal = Decimal(decimal_string(payload["principal"], "confirmed principal", positive=True))
    _rate(payload["loss_tolerance"])
    require(payload["currency"] == "CNY" and type(payload["confirmed_initial_all_cash"]) is bool,
            "CNY currency and explicit account confirmation required")
    require(instant(payload["as_of"]) <= instant(at), "Capital baseline cannot be in the future")
    request_hash = fingerprint(payload)
    with store.transaction() as conn:
        saved = store.get("profile_initialization", operation_id, conn)
        if saved:
            require(saved["request_hash"] == request_hash, "Profile initialization identity changed")
            return saved["result"]
        require(store.get("capital_baseline", "main", conn) is None
                and store.get("risk_profile", "main", conn) is None, "Capital baseline is already confirmed")
        account = store.get("account", "main", conn)
        require(payload["account_hash"] == (None if account is None else fingerprint(account)), "Account changed before confirmation")
        require(account is None or account["currency"] == payload["currency"], "Account currency differs")
        if account is not None:
            ledger._state(account)
            require(account["effective_at"] is None or instant(account["effective_at"]) <= instant(payload["as_of"]),
                    "Capital baseline must include all economic facts already in the account")
        if payload["confirmed_initial_all_cash"]:
            observed_only = account is not None and account != ledger.initial_state(payload["currency"])
            if observed_only:
                history = [row for key, row in store.scan("ledger_event", conn)
                           if store.get("ledger_event_owner", key, conn) == {"account_id": "main"}]
                require(payload.get("confirmed_no_prior_economic_activity") is True,
                        "Explicitly confirm no prior economic activity before establishing the first performance baseline")
                require(len(history) == account["sequence"] and all(row["type"] in {"unknown", "resolve_unknown", "valuation"} for row in history)
                        and Decimal(account["cash"]) == 0 and Decimal(account["units"]) == 0
                        and not any(account[name] for name in ("lots", "orders", "receivables", "pending_subscriptions", "transfers", "performance_pending")),
                        "Initial performance confirmation cannot erase prior economic activity or losses")
            else:
                account = ledger.initial_state(payload["currency"])
            event = {"id": "profile-opening:" + operation_id, "type": "performance_start" if observed_only else "opening", "sequence": account["sequence"] + 1,
                     "effective_at": payload["as_of"], "known_at": at, "recorded_at": at,
                     "data": ({"cash": ledger.decimal(principal), "confirmed_no_prior_economic_activity": True,
                               "resolved_unknown_ids": [row["id"] for row in account["unknown"]], "user_source": payload["user_source"]}
                              if observed_only else {"cash": ledger.decimal(principal), "lots": [], "prices": {}, "price_dates": {}})}
            account = ledger.apply_event(account, event)
            store.put("ledger_event", event["id"], event, conn=conn)
            store.put("ledger_event_owner", event["id"], {"account_id": "main"}, conn=conn)
            store.put("ledger_event_evidence", event["id"], [], conn=conn)
            store.put("account", "main", account, immutable=False, conn=conn)
        else:
            require(account is not None, "Existing holdings must first be recorded and reconciled")
        baseline = {"principal": ledger.decimal(principal), "currency": payload["currency"],
                    "as_of": payload["as_of"], "account_sequence": account["sequence"],
                    "account_hash": fingerprint(account), "user_source": payload["user_source"]}
        revision = {"mode": "base", "loss_tolerance": payload["loss_tolerance"], "start_at": at,
                    "end_at": None, "recorded_at": at, "user_source": payload["user_source"],
                    "previous_revision": None}
        identity = fingerprint(revision)
        pointer = {"revision_id": identity, "revision_count": 1}
        store.put("capital_baseline", "main", baseline, conn=conn)
        store.put("capital_revision", fingerprint(baseline), baseline, conn=conn)
        store.put("capital_current", "main", baseline, immutable=False, conn=conn)
        store.put("risk_revision", identity, revision, conn=conn)
        store.put("risk_profile", "main", pointer, immutable=False, conn=conn)
        result = {"status": "confirmed", "profile_hash": fingerprint(pointer), "account_hash": fingerprint(account),
                  "capital_baseline": baseline, "risk_revision": identity}
        store.put("profile_initialization", operation_id, {"request_hash": request_hash, "result": result}, conn=conn)
    return result


def _window_ends(revisions):
    ends = {fingerprint(row): row["end_at"] for row in revisions if row["mode"] == "temporary"}
    for row in revisions:
        if row["mode"] == "end_temporary":
            require(row["target_revision"] in ends, "Risk cancellation target is missing")
            prior = ends[row["target_revision"]]
            ends[row["target_revision"]] = min((prior, row["start_at"]), key=instant)
    return ends


def _effective(revisions, as_of):
    cutoff, ends = instant(as_of), _window_ends(revisions)
    bases = [row for row in revisions if row["mode"] == "base" and instant(row["start_at"]) <= cutoff]
    require(bases, "No confirmed base risk preference at this time")
    active = max(bases, key=lambda row: (instant(row["start_at"]), instant(row["recorded_at"])))
    temporary = [row for row in revisions if row["mode"] == "temporary"
                 and instant(row["start_at"]) <= cutoff < instant(ends[fingerprint(row)])]
    require(len(temporary) <= 1, "Overlapping risk windows")
    return (temporary[0] if temporary else active), ends


def update(payload, store, operation_id):
    fields(payload, {"mode", "loss_tolerance", "start_at", "end_at", "user_source", "current_profile_hash"},
           {"target_revision", "end_active_temporary"}, label="risk update")
    saved = store.get("risk_update", operation_id)
    if saved:
        require(saved["request_hash"] == fingerprint(payload), "Risk update identity changed")
        return saved["result"]
    at = utc_now()
    _source(payload["user_source"], at)
    require(payload["mode"] in {"base", "temporary", "end_temporary"}, "Risk update mode required")
    if payload["mode"] != "end_temporary":
        _rate(payload["loss_tolerance"])
    else:
        require(payload["loss_tolerance"] is None, "Ending a temporary window does not replace the base rate")
    if payload["mode"] == "base":
        require(type(payload.get("end_active_temporary")) is bool,
                "Base change must explicitly say whether to end temporary overrides")
    start = payload["start_at"] or at
    require(instant(start) >= instant(at), "New risk preferences take effect now or in the future")
    end = payload["end_at"]
    require((payload["mode"] in {"base", "end_temporary"} and end is None) or
            (payload["mode"] == "temporary" and end is not None and instant(end) > instant(start)),
            "Temporary risk requires a finite future end; base risk has no end")
    request_hash = fingerprint(payload)
    with store.transaction() as conn:
        saved = store.get("risk_update", operation_id, conn)
        if saved:
            require(saved["request_hash"] == request_hash, "Risk update identity changed")
            return saved["result"]
        pointer = store.get("risk_profile", "main", conn)
        require(pointer is not None and fingerprint(pointer) == payload["current_profile_hash"], "Risk profile changed; refresh before updating")
        revisions = _revisions(store, pointer, conn)
        ends = _window_ends(revisions)
        targets = []
        if payload.get("target_revision") is not None:
            target = payload["target_revision"]
            require(target in ends and instant(start) < instant(ends[target]), "Temporary target is unknown or already ended")
            targets.append(target)
        if payload["mode"] == "end_temporary":
            require(len(targets) == 1, "Select the temporary revision to end")
        if payload["mode"] == "base" and payload["end_active_temporary"]:
            targets.extend(key for key, value in ends.items() if instant(value) > instant(start) and key not in targets)
        for target in targets:
            revision = {"mode": "end_temporary", "target_revision": target, "loss_tolerance": None,
                        "start_at": start, "end_at": None, "recorded_at": at, "user_source": payload["user_source"],
                        "previous_revision": pointer["revision_id"]}
            identity = fingerprint(revision)
            store.put("risk_revision", identity, revision, conn=conn)
            pointer = {"revision_id": identity, "revision_count": pointer["revision_count"] + 1}
            revisions.append(revision)
        ends = _window_ends(revisions)
        if payload["mode"] == "temporary":
            for row in revisions:
                if row["mode"] == "temporary":
                    require(instant(end) <= instant(row["start_at"]) or instant(start) >= instant(ends[fingerprint(row)]),
                            "Temporary risk windows must be disjoint")
        if payload["mode"] != "end_temporary":
            revision = {"mode": payload["mode"], "loss_tolerance": payload["loss_tolerance"],
                        "start_at": start, "end_at": end, "recorded_at": at,
                        "user_source": payload["user_source"], "previous_revision": pointer["revision_id"]}
            identity = fingerprint(revision)
            store.put("risk_revision", identity, revision, conn=conn)
            revisions.append(revision)
            pointer = {"revision_id": identity, "revision_count": pointer["revision_count"] + 1}
        next_pointer = pointer
        store.put("risk_profile", "main", next_pointer, immutable=False, conn=conn)
        result = {"status": "confirmed", "profile_hash": fingerprint(next_pointer), "risk_revision": identity,
                  "effective_from": start, "effective_until": end,
                  "effective_loss_tolerance": _effective(revisions, at)[0]["loss_tolerance"],
                  "ended_temporary_revisions": targets}
        store.put("risk_update", operation_id, {"request_hash": request_hash, "result": result}, conn=conn)
    return result


def resolve(store, account, as_of, prices, account_id="main", *, price_dates=None):
    pointer = store.get("risk_profile", account_id)
    baseline = store.get("capital_current", account_id)
    require(pointer is not None and baseline is not None, "Confirm principal and risk preferences first")
    reconciliation = store.get("capital_reconciliation", account_id)
    if reconciliation is not None and not reconciliation["resolved"]:
        raise CapitalReconciliationRequired(reconciliation, baseline, account)
    binding = store.get("trial_risk_binding", account_id)
    if binding:
        require(fingerprint(store.get("risk_profile", binding["source_account_id"])) == binding["profile_hash"],
                "Registered risk profile changed; close this fixed-profile trial and register a new episode")
    revisions, cutoff = _revisions(store, pointer), instant(as_of)
    active, ends = _effective(revisions, as_of)
    boundaries = {row["start_at"] for row in revisions if row["mode"] == "base" and instant(row["start_at"]) > cutoff}
    for row in revisions:
        if row["mode"] == "temporary" and instant(row["start_at"]) < instant(ends[fingerprint(row)]):
            boundaries.update(value for value in (row["start_at"], ends[fingerprint(row)]) if instant(value) > cutoff)
    changes = [value for value in sorted(boundaries, key=instant)
               if _rate(_effective(revisions, value)[0]["loss_tolerance"]) != _rate(active["loss_tolerance"])]
    valid_until = changes[0] if changes else None
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        principal = Decimal(baseline["principal"])
        require(account["currency"] == baseline["currency"] and account["sequence"] >= baseline["account_sequence"], "Account differs from capital baseline")
        if binding:
            prefix = binding["trial_id"] + ":strategy:"
            events = [item["event"] for identity, item in store.scan("trial_ledger_event") if identity.startswith(prefix)]
        else:
            events = [event for identity, event in store.scan("ledger_event")
                      if store.get("ledger_event_owner", identity) == {"account_id": account_id}]
        for event in ledger.canonical_cashflows(events, as_of):
            if event["sequence"] > baseline["account_sequence"] and event["sequence"] <= account["sequence"] and event["type"] == "cashflow":
                require(instant(event["effective_at"]) >= instant(baseline["as_of"]), "Late external flow predates confirmed capital baseline; reconcile principal")
                require(instant(event["known_at"]) <= cutoff and instant(event["effective_at"]) <= cutoff, "Capital flow is outside the valuation cutoff")
                principal += Decimal(event["data"]["amount"])
        if principal <= 0:
            raise PrincipalPolicyRequired()
        snapshot = ledger.snapshot(account, as_of, prices, price_dates)
        require(not snapshot["blocked"], "Account reconciliation required before risk calculation")
        rate = _rate(active["loss_tolerance"])
        floor = principal * (1 - rate)
        return {"profile_hash": fingerprint(pointer), "capital_baseline": baseline,
                "net_principal": ledger.decimal(principal), "loss_tolerance": ledger.decimal(rate),
                "principal_floor": ledger.decimal(floor),
                "remaining_loss_budget": ledger.decimal(Decimal(snapshot["equity"]) - floor),
                "currency": baseline["currency"], "as_of": as_of, "valid_until": valid_until}


def assert_current(store, risk_state, account, as_of, prices, account_id="main", *, price_dates=None):
    """Recheck both mutable profile lineage and time-based expiry before publishing."""
    current = resolve(store, account, as_of, prices, account_id, price_dates=price_dates)
    require({k: v for k, v in current.items() if k != "as_of"} ==
            {k: v for k, v in risk_state.items() if k != "as_of"}, "Risk profile, principal or effective window changed; recompute")
    return current


def validate_constraints(value):
    fields(value, PLAN_CONSTRAINT_FIELDS, label="confirmed plan constraints")
    require(value["currency"] == "CNY" and type(value["platform"]) is str and value["platform"].strip()
            and type(value["goal"]) is str and value["goal"].strip(), "Explicit platform, currency and goal required")
    require(type(value["excluded_categories"]) is list
            and all(type(item) is str and item.strip() for item in value["excluded_categories"])
            and len(set(value["excluded_categories"])) == len(value["excluded_categories"]), "Distinct explicit exclusions required")
    fields(value["position_limits"], {"fund_group_limits", "sector_limits"}, label="position limits")
    for limits in value["position_limits"].values():
        require(type(limits) is dict, "Position limit groups must be mappings")
        for key, rate in limits.items():
            require(type(key) is str and key.strip() and 0 < Decimal(decimal_string(rate, "position limit")) <= 1,
                    "Position limits must be fractions above zero and at most one")
    return value


def validate_plan(record):
    fields(record, {"schema_version", "revision_id", "revision_count", "constraints", "user_source", "previous_hash", "effective_at"},
           label="confirmed plan revision")
    require(record["schema_version"] == 4 and type(record["revision_count"]) is int and record["revision_count"] > 0,
            "Current plan revision required")
    require(record["revision_id"] == fingerprint({key: value for key, value in record.items() if key != "revision_id"}),
            "Plan revision content hash differs")
    _source(record["user_source"], record["effective_at"])
    require(type(record["constraints"]) is dict, "Recorded plan constraints must be an object")
    # The full original revision remains hashed. Retired metadata is historical
    # evidence only; current calculations use the same business fields as new plans.
    validate_constraints({key: value for key, value in record["constraints"].items()
                          if key in PLAN_CONSTRAINT_FIELDS})
    return record


def update_constraints(payload, store, operation_id):
    fields(payload, {"constraints", "current_constraints_hash", "user_source"}, label="plan constraints")
    at = utc_now()
    _source(payload["user_source"], at)
    value = validate_constraints(payload["constraints"])
    with store.transaction() as conn:
        saved = store.get("plan_update", operation_id, conn)
        if saved:
            require(saved["request_hash"] == fingerprint(payload), "Plan update identity changed")
            return saved["result"]
        previous = store.get("plan_constraints", "main", conn)
        previous_hash = None if previous is None else fingerprint(previous)
        require(previous_hash == payload["current_constraints_hash"], "Plan constraints changed; refresh before updating")
        revision = {"schema_version": 4, "constraints": value, "user_source": payload["user_source"],
                    "previous_hash": previous_hash, "effective_at": at,
                    "revision_count": 1 if previous is None else previous["revision_count"] + 1}
        identity = fingerprint(revision)
        record = {**revision, "revision_id": identity}
        store.put("plan_revision", identity, revision, conn=conn)
        store.put("plan_constraints", "main", record, immutable=False, conn=conn)
        result = {"status": "confirmed", "constraints_hash": fingerprint(record), "constraints": value,
                  "revision_id": identity}
        store.put("plan_update", operation_id, {"request_hash": fingerprint(payload), "result": result}, conn=conn)
    return result


def reconcile_capital(payload, store, operation_id):
    from artifacts import Artifacts
    import verify
    fields(payload, {"principal", "as_of", "account_hash", "previous_baseline_hash", "reason", "user_source", "evidence_refs"},
           label="corrected contributed capital")
    at = utc_now()
    _source(payload["user_source"], at)
    principal = decimal_string(payload["principal"], "corrected contributed principal", positive=True)
    require(type(payload["reason"]) is str and payload["reason"].strip()
            and type(payload["evidence_refs"]) is list and payload["evidence_refs"], "Capital correction explanation and source evidence required")
    require(instant(payload["as_of"]) <= instant(at), "Capital correction cannot be future")
    verify.references(payload["evidence_refs"], Artifacts(store.base))
    with store.transaction() as conn:
        saved = store.get("capital_reconcile_result", operation_id, conn)
        if saved:
            require(saved["request_hash"] == fingerprint(payload), "Capital reconciliation identity changed")
            return saved["result"]
        pending = store.get("capital_reconciliation", "main", conn)
        require(pending is not None and not pending["resolved"], "No unresolved source correction authorizes a capital baseline revision")
        previous = store.get("capital_current", "main", conn)
        account = store.get("account", "main", conn)
        require(previous is not None and fingerprint(previous) == payload["previous_baseline_hash"]
                and account is not None and fingerprint(account) == payload["account_hash"], "Capital/account changed before reconciliation")
        require(instant(account["effective_at"]) <= instant(payload["as_of"]), "Reconciliation must include all known economic facts")
        baseline = {"principal": principal, "currency": "CNY", "as_of": payload["as_of"],
                    "account_sequence": account["sequence"], "account_hash": fingerprint(account),
                    "user_source": payload["user_source"], "previous_baseline_hash": fingerprint(previous),
                    "prior_principal": previous["principal"], "reason": payload["reason"],
                    "corrected_transfer_revisions": pending["revision_ids"], "evidence_refs": payload["evidence_refs"]}
        identity = fingerprint(baseline)
        store.put("capital_revision", identity, baseline, conn=conn)
        store.put("capital_current", "main", baseline, immutable=False, conn=conn)
        store.put("capital_reconciliation", "main", {**pending, "resolved": True, "capital_revision": identity}, immutable=False, conn=conn)
        result = {"status": "confirmed", "capital_revision": identity, "capital_baseline": baseline}
        store.put("capital_reconcile_result", operation_id, {"request_hash": fingerprint(payload), "result": result}, conn=conn)
    return result
