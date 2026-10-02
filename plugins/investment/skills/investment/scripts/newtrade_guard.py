"""Source-auditable policy registration and canonical executed-fee state.

The controller owns raw market evidence and canonical account facts. Caller
qualification booleans never grant permission to publish a trade.
"""
import copy
import datetime as dt
import math
from decimal import Decimal

import ledger
from contracts import decimal_string, fields, fingerprint, instant, require


def validate_policy(policy):
    fields(policy, {"kind", "registration", "sample_window", "economic", "inference", "family"}, label="trade policy")
    require(policy["kind"] == "forward_oos_path_policy_error_v1", "Unsupported trade calibration method")
    require(policy["registration"] in {"declared", "illustrative"}, "Explicit trade-policy declaration required")
    window = policy["sample_window"]
    fields(window, {"start_date", "end_date"}, label="trade calibration window")
    start, end = (dt.date.fromisoformat(window[key]) for key in ("start_date", "end_date"))
    require(start <= end, "Invalid fixed OOS calibration window")
    economic = policy["economic"]
    fields(economic, {"minimum_net_advantage_amount", "minimum_reversal_advantage_amount",
                      "maximum_rolling_fee_amount", "fee_window_days", "minimum_risk_reduction_amount"}, label="trade economic requirements")
    amounts = {name: Decimal(decimal_string(economic[name], name)) for name in economic if name != "fee_window_days"}
    require(all(value >= 0 for value in amounts.values()), "Trade economic amounts must be nonnegative")
    require(amounts["minimum_reversal_advantage_amount"] > amounts["minimum_net_advantage_amount"],
            "Reversal advantage must exceed the ordinary requirement")
    require(type(economic["fee_window_days"]) is int and economic["fee_window_days"] > 0,
            "Explicit positive rolling-fee window required")
    require(amounts["minimum_risk_reduction_amount"] > 0, "Risk exception requires a positive economic reduction")
    family = policy["family"]
    fields(family, {"method", "candidate_rule", "max_candidates", "max_reviews", "start_at", "end_at"}, label="registered trade family")
    require(family["method"] == "joint_block_maxT_fixed_review_family" and family["candidate_rule"] == "source_settlement_mpc_policy_v1",
            "Unsupported candidate or repeated-review control")
    require(all(type(family[key]) is int and family[key] > 0 for key in ("max_candidates", "max_reviews")),
            "Positive fixed candidate and review budgets required")
    require(instant(family["start_at"]) < instant(family["end_at"]), "Fixed trade-family endpoint required")
    from allocation_statistics import _inference_inputs
    _inference_inputs([], [], policy["inference"])
    require(type(policy["inference"].get("minimum_tail_observations")) is int
            and policy["inference"]["minimum_tail_observations"] > 0,
            "Explicit positive tail-observation governance requirement")
    return copy.deepcopy(policy)


def derive_state(initial_account, canonical_events, decision_at, fee_window_days, *, account_id="main"):
    """Count executed fills at separate knowledge and economic cutoffs.

    Reservations and subscription payments only resolve confirmed fill identity;
    recommendations and unconfirmed payments never count as executed trades.
    """
    require(type(fee_window_days) is int and fee_window_days > 0, "Fee-history window required")
    ledger._state(initial_account)
    cutoff = instant(decision_at)
    window_start = cutoff - dt.timedelta(days=fee_window_days)
    require(type(canonical_events) is list, "Canonical account history required")
    orders = copy.deepcopy(initial_account["orders"])
    payments = copy.deepcopy(initial_account["pending_subscriptions"])
    selected, identities, financial = [], {}, {}
    for event in canonical_events:
        ledger._event(event)
        if event["sequence"] <= initial_account["sequence"]:
            continue
        digest = fingerprint(event)
        require(event["id"] not in identities or identities[event["id"]] == digest, "Trade fact ID conflict")
        if event["id"] in identities:
            continue
        identities[event["id"]] = digest
        fact = ledger.financial_identity(event)
        if fact:
            require(fact["key"] not in financial or financial[fact["key"]] == fact["content_hash"], "Trade financial identity conflict")
            if fact["key"] in financial:
                continue
            financial[fact["key"]] = fact["content_hash"]
        if all(instant(event[key]) <= cutoff for key in ("known_at", "recorded_at", "effective_at")):
            selected.append(event)
    selected.sort(key=lambda event: (instant(event["effective_at"]), event["sequence"]))
    last, fills, fee_total = {}, [], Decimal(0)
    for event in selected:
        data, kind = event["data"], event["type"]
        if kind == "order_reserved":
            orders[data["order"]["order_id"]] = data["order"]
        elif kind == "subscription_pending":
            payments[data["payment_id"]] = {"order_id": data["order_id"]}
        elif kind == "account_snapshot_confirmed":
            orders = copy.deepcopy(data["orders"])
            payments = copy.deepcopy(data["pending_subscriptions"])
        elif kind in {"buy_fill", "sell_fill", "subscription_confirmed", "external_fill_confirmed", "dividend_reinvested"}:
            if kind in {"external_fill_confirmed", "dividend_reinvested"}:
                code, side = data["code"], data.get("side", "buy")
                order_id = None
            else:
                if kind == "subscription_confirmed":
                    require(data["payment_id"] in payments, "Confirmed trade lacks its payment lineage")
                    order_id = payments.pop(data["payment_id"])["order_id"]
                else:
                    order_id = data["order_id"]
                require(order_id in orders, "Confirmed trade lacks its reserved-order lineage")
                code, side = orders[order_id]["code"], orders[order_id]["side"]
            fee = ledger.amount(data["fee"], "actual executed fee", 0)
            fill = {"event_id": event["id"], "fill_id": data["fill_id"], "code": code, "side": side,
                    "fee": ledger.decimal(fee), "effective_at": event["effective_at"],
                    "known_at": event["known_at"], "source_hash": fingerprint(event)}
            last[code] = fill
            if instant(event["effective_at"]) >= window_start:
                fee_total += fee
                fills.append(fill)
            if order_id is not None and data["final"]:
                orders.pop(order_id)
        elif kind == "cancel_confirmed":
            orders.pop(data["order_id"], None)
    result = {"schema_version": 4, "account_id": account_id, "as_of": decision_at,
              "fee_window_days": fee_window_days, "window_start": window_start.isoformat(),
              "last_executed_by_code": last, "actual_window_fees": ledger.decimal(fee_total),
              "executed_fills": fills, "initial_account_hash": fingerprint(initial_account),
              "source_events_hash": fingerprint(selected), "source_event_ids": [event["id"] for event in selected],
              "basis": "canonical_confirmed_execution_facts_only"}
    return {**result, "state_hash": fingerprint(result)}


def validate_state(state, account_id, decision_at, fee_window_days):
    fields(state, {"schema_version", "account_id", "as_of", "fee_window_days", "window_start",
                   "last_executed_by_code", "actual_window_fees", "executed_fills", "initial_account_hash",
                   "source_events_hash", "source_event_ids", "basis", "state_hash"}, label="system trade state")
    require(state["schema_version"] == 4 and state["account_id"] == account_id
            and state["as_of"] == decision_at and state["fee_window_days"] == fee_window_days,
            "Trade history belongs to another account, cutoff or policy window")
    require(state["basis"] == "canonical_confirmed_execution_facts_only"
            and state["state_hash"] == fingerprint({key: value for key, value in state.items() if key != "state_hash"}),
            "Trade state changed; the controller must rebuild it from source facts")
    require(ledger.amount(state["actual_window_fees"], "actual window fees", 0)
            == sum((ledger.amount(fill["fee"], "executed fee", 0) for fill in state["executed_fills"]), Decimal(0)),
            "Rolling executed fees do not reconcile")
    return state


def source_prefix(store, account_id, sequence, known_at):
    """A complete owner-bound source prefix, frozen at its knowledge cutoff."""
    cutoff = instant(known_at)
    events = [event for identity, event in store.scan("ledger_event")
              if store.get("ledger_event_owner", identity) == {"account_id": account_id}
              and event["sequence"] <= sequence]
    events.sort(key=lambda event: event["sequence"])
    require([event["sequence"] for event in events] == list(range(1, sequence + 1)),
            "Confirmed account source history is incomplete; reconcile canonical facts")
    require(all(instant(event["known_at"]) <= cutoff and instant(event["recorded_at"]) <= cutoff for event in events),
            "Initial trade-history prefix was not known at the frozen account cutoff")
    return events


def qualification_scope(policy, decision_at, review_index):
    """Declared finite-review governance; not a market probability claim."""
    at, family = instant(decision_at), policy["family"]
    reasons = []
    if policy["registration"] != "declared":
        reasons.append("illustrative_policy_research_only")
    if at < instant(family["start_at"]):
        reasons.append("registered_family_not_started")
    if at >= instant(family["end_at"]):
        reasons.append("registered_family_expired")
    if type(review_index) is not int or not 1 <= review_index <= family["max_reviews"]:
        reasons.append("registered_review_budget_exhausted")
    value = {"eligible": not reasons, "reasons": reasons, "decision_at": decision_at,
             "review_index": review_index, "policy_hash": fingerprint(policy),
             "family_hash": fingerprint(family), "valid_until": family["end_at"]}
    return {**value, "scope_hash": fingerprint(value)}


def reserve_review(store, artifacts, account_id, spec, decision_at, *, decision_id):
    """Spend one logical review before inspecting numerical outcomes.

    The lease and writer transaction serialize reservations. A restart retains
    its original slot even when no winner or published decision was produced.
    Source validators call read_inputs, which never performs this mutation.
    """
    inputs = read_inputs(store, artifacts, account_id, spec, decision_at, decision_id=decision_id)
    if inputs["review_reservation"] is not None or not inputs["qualification_scope"]["eligible"]:
        return inputs
    policy = spec["trade_policy"]
    family_hash, key = fingerprint(policy["family"]), account_id + ":" + fingerprint(policy["family"])
    with store.transaction() as conn:
        prior = store.get("trade_family_reservation", decision_id, conn)
        if prior is None:
            previous = store.get("trade_family_frontier", key, conn)
            count = previous["count"] if previous else 0
            policy_unchanged = previous is None or previous.get("policy_hash") == fingerprint(policy)
            if policy_unchanged and count < policy["family"]["max_reviews"]:
                record = {"schema_id": "logical_review_reservation_v1", "decision_id": decision_id,
                          "account_id": account_id, "family_hash": family_hash,
                          "policy_hash": fingerprint(policy), "index": count + 1,
                          "decision_at": decision_at, "trade_state_hash": inputs["trade_state"]["state_hash"]}
                store.put("trade_family_reservation", decision_id, record, conn=conn)
                store.put("trade_family_frontier", key, {"count": count + 1, "last_decision": decision_id,
                          "family_hash": family_hash, "policy_hash": fingerprint(policy),
                          "schema_id": "logical_review_reservation_v1"},
                          immutable=False, conn=conn)
    return read_inputs(store, artifacts, account_id, spec, decision_at, decision_id=decision_id)


def read_inputs(store, artifacts, account_id, spec, decision_at, *, decision_id=None):
    """Controller-derived history and review count; no business writes occur here."""
    policy = validate_policy(spec["trade_policy"])
    family_hash = fingerprint(policy["family"])
    account = store.get("account", account_id)
    require(account is not None, "Confirmed account required for trade inputs")
    initial = ledger.initial_state(account["currency"])
    binding = store.get("trial_risk_binding", account_id)
    if binding is None:
        require(account_id == "main", "Unknown account trade-history authority")
        cutoff = instant(decision_at)
        known_sequences = [event["sequence"] for identity, event in store.scan("ledger_event")
                           if store.get("ledger_event_owner", identity) == {"account_id": account_id}
                           and event["sequence"] <= account["sequence"]
                           and instant(event["known_at"]) <= cutoff and instant(event["recorded_at"]) <= cutoff]
        events = source_prefix(store, account_id, max(known_sequences, default=0), decision_at)
    else:
        trial_id = binding["trial_id"]
        require(account_id == "trial:" + trial_id + ":strategy", "Trial history belongs to another role")
        registered = store.get("trial", trial_id)
        require(registered is not None, "Unknown registered trial history")
        document = registered["document"]
        require("initial_trade_events_ref" in document, "new_spec_required: frozen opening trade history missing")
        events = artifacts.read_json(document["initial_trade_events_ref"])
        frozen_account = artifacts.read_json(document["initial_account_ref"])
        state = store.get("trial_state", trial_id)
        require(state is not None, "Trial state required for trade history")
        for number in range(1, state["ledger_counts"]["strategy"] + 1):
            identity = trial_id + ":strategy:" + f"{number:08d}"
            row = store.get("trial_ledger_event", identity)
            require(row is not None and row["event"]["sequence"] == frozen_account["sequence"] + number,
                    "Trial canonical trade history is incomplete")
            events.append(row["event"])
    trade_state = derive_state(initial, events, decision_at, policy["economic"]["fee_window_days"], account_id=account_id)
    key = account_id + ":" + family_hash
    previous = store.get("trade_family_frontier", key)
    count = 0 if previous is None else previous["count"]
    require(type(count) is int and count >= 0, "Invalid registered review frontier")
    reservation = store.get("trade_family_reservation", decision_id) if decision_id else None
    review = reservation["index"] if reservation is not None else count + 1
    scope = qualification_scope(policy, decision_at, review)
    if previous is not None and previous.get("policy_hash") != fingerprint(policy):
        scope["eligible"] = False
        scope["reasons"].append("registered_family_policy_changed")
        scope["scope_hash"] = fingerprint({k: v for k, v in scope.items() if k != "scope_hash"})
    history_gap = previous is not None and previous.get("schema_id") != "logical_review_reservation_v1"
    if previous is None:
        # An absent frontier does not prove that earlier no-winner comparisons
        # never occurred. Preserved decision evidence remains authoritative.
        for identity, decision in store.scan("decision"):
            if decision.get("account_id") != account_id or store.get("trade_family_reservation", identity) is not None:
                continue
            contexts = [decision["context"]] if decision.get("context") else []
            if decision.get("family_results_ref"):
                result = artifacts.read_json(decision["family_results_ref"])
                contexts += [artifacts.read_json(row["context_ref"]) for row in result.get("results", [])]
            if any(fingerprint(row["spec"]["trade_policy"]["family"]) == family_hash for row in contexts):
                history_gap = True
                break
        if not history_gap:
            for _, bound in store.scan("validation_binding"):
                if bound.get("stage", "").split("@", 1)[0] != "numeric_mpc":
                    continue
                if store.get("trade_family_reservation", bound["operation_id"]) is not None:
                    continue
                frozen = artifacts.read_json(bound["inputs_ref"])
                context = frozen.get("context", {})
                old_policy = context.get("spec", {}).get("trade_policy")
                if (context.get("account_id") == account_id and old_policy
                        and old_policy.get("registration") == "declared"
                        and fingerprint(old_policy["family"]) == family_hash):
                    history_gap = True
                    break
    if history_gap:
        scope["eligible"] = False
        scope["reasons"].append("historical_review_budget_unverified")
        scope["scope_hash"] = fingerprint({k: v for k, v in scope.items() if k != "scope_hash"})
    if reservation is not None:
        expected = {"schema_id": "logical_review_reservation_v1", "decision_id": decision_id,
                    "account_id": account_id, "family_hash": family_hash, "policy_hash": fingerprint(policy),
                    "index": review, "decision_at": decision_at, "trade_state_hash": trade_state["state_hash"]}
        require(reservation == expected and previous is not None and previous["count"] >= review,
                "Immutable numerical review reservation differs from its frozen inputs")
    return {"trade_state": trade_state, "trade_family_review_index": review,
            "review_reservation": reservation, "qualification_scope": scope}
