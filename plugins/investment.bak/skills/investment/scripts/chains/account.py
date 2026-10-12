"""Freeze actual account facts before any source or scientific business chain."""
import copy
import re
from decimal import Decimal, localcontext

import ledger
import risk_profile
from artifacts import Artifacts
from contracts import StaleSnapshot, fingerprint, instant, require, utc_now
from state_store import Store
from chains.contracts import ACCOUNT_FACTS, make_envelope, validate_envelope
from chains.common import check_control


QUESTIONS = {
    "principal": "本次计划已实际投入的本金是多少？",
    "loss_tolerance": "相对投入本金，最多能接受多大比例的亏损？",
    "cash": "目前已到账、可用的现金是多少？",
    "holdings": "目前持有哪些基金及份额？没有持仓请明确确认。",
}


def _financial_snapshot(store):
    """One consistent entry snapshot; no operation/news/latest identifiers enter it."""
    at = utc_now()
    with store._reader() as reader:
        records = {kind: store.get(kind, "main", reader) for kind in (
            "account", "capital_baseline", "capital_current", "risk_profile",
            "plan_constraints", "capital_reconciliation",
        )}
        account, baseline, pointer = (records[key] for key in (
            "account", "capital_current", "risk_profile"))
        revisions = risk_profile._revisions(store, pointer, reader) if pointer else []
        active = risk_profile._effective(revisions, at)[0] if revisions else None
        records["risk_revisions"] = revisions
        records["active_risk_revision"] = active
        plan = records["plan_constraints"]
        if plan is not None:
            risk_profile.validate_plan(plan)
        records["ledger_events"] = [event for identity, event in store.scan("ledger_event", reader)
                                    if store.get("ledger_event_owner", identity, reader) == {"account_id": "main"}]

    if account is not None:
        ledger._state(account)
    confirmed = account is not None and account["sequence"] > 0
    # An observation-only unknown event does not confirm the initial zero
    # balance. Preserve that projection, but do not turn its placeholders into
    # an explicitly confirmed empty account.
    if confirmed and account["unknown"]:
        confirmed = any(event["type"] not in {"unknown", "resolve_unknown"}
                        for event in records["ledger_events"])
    holdings = None
    if confirmed:
        holdings = []
        for identity, lot in sorted(account["lots"].items()):
            row = copy.deepcopy(lot)
            row.setdefault("lot_id", identity)
            row.setdefault("purchase_cost", None)
            row["cost_status"] = "known" if row["purchase_cost"] is not None else "unknown"
            row["missing_metadata"] = [key for key in (
                "acquired_at", "ownership_at", "price_date", "purchase_cost") if row.get(key) is None]
            holdings.append(row)

    principal = baseline["principal"] if baseline is not None else None
    net_principal = principal
    limitations = []
    pending = records["capital_reconciliation"]
    if pending is not None and not pending["resolved"]:
        principal = net_principal = None
        limitations.append("capital_reconciliation_required")
    elif baseline is not None and account is not None:
        require(account["currency"] == baseline["currency"]
                and account["sequence"] >= baseline["account_sequence"], "Account differs from capital baseline")
        with localcontext() as context:
            context.prec = 50
            amount = Decimal(baseline["principal"])
            for event in ledger.canonical_cashflows(records["ledger_events"], at):
                if baseline["account_sequence"] < event["sequence"] <= account["sequence"]:
                    require(instant(event["effective_at"]) >= instant(baseline["as_of"]),
                            "Late external flow predates confirmed capital baseline; reconcile principal")
                    require(instant(event["effective_at"]) <= instant(at), "Capital flow is outside the account cutoff")
                    amount += Decimal(event["data"]["amount"])
            net_principal = ledger.decimal(amount)
            if amount > 0:
                principal = net_principal
            else:
                # Keep the confirmed original principal and the actual net
                # contribution separately; an exhausted contribution requires
                # the existing residual-asset policy, never invented capital.
                limitations.append("principal_policy_required")

    cash = account["cash"] if confirmed else None
    available_cash = None
    if confirmed:
        available_cash = ledger.decimal(max(Decimal(0), ledger.amount(cash) - ledger.reserved_cash(account)))
        if ledger.amount(cash) < 0:
            cash = None
            limitations.append("cash_reconciliation_required")
        limitations.extend(row["reason"] for row in account["unknown"])
        limitations.extend(row["reason"] for row in account["performance_pending"])
        if account["pending_subscriptions"]:
            limitations.append("unconfirmed_subscription_shares")

    import verify
    facts = {
        "currency": "CNY", "platform": "TT", "principal": principal,
        "confirmed_principal": baseline["principal"] if baseline is not None else None,
        "net_principal": net_principal,
        "loss_tolerance": active["loss_tolerance"] if active is not None else None,
        "cash": cash, "available_cash": available_cash, "holdings": holdings,
        "risk_codes": verify.monitor_codes(account),
        "account_sequence": account["sequence"] if account is not None else None,
        "record_hashes": {key: fingerprint(value) if value is not None else None
                          for key, value in records.items()},
        "limitations": sorted(set(limitations)),
    }
    return {"ledger_state": account, "account_context": facts, "records": records}


def read_account(root, plan, chain_id, *, control_context):
    """The first business chain accepts no prior business payload or assumptions."""
    require(type(chain_id) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", chain_id),
            "Invalid business chain identity")
    check_control(root, plan, chain_id, control_context)
    store = Store(root, plan)
    snapshot = _financial_snapshot(store)
    facts = snapshot["account_context"]
    missing = [field for field in ACCOUNT_FACTS if facts[field] is None]
    artifacts = Artifacts(store.base)
    actual_records = any(snapshot["records"][key] is not None for key in (
        "account", "capital_baseline", "capital_current", "risk_profile", "plan_constraints"))
    reference = artifacts.put_json(snapshot) if actual_records else None
    status = "awaiting_user" if missing else (
        "needs_evidence" if "principal_policy_required" in facts["limitations"] else "complete")
    return make_envelope("account", chain_id, {
        "account_context": facts, "missing_fields": missing,
        "questions": [{"field": field, "question": QUESTIONS[field]} for field in missing],
        "engine_context": {"account_snapshot_ref": reference},
    }, account_ref=reference, account_hash=reference["sha256"] if reference else None,
        status=status, source_refs=[reference] if reference else [],
        protocol_sha256=control_context["protocol_sha256"],
        implementation_identity=control_context["implementation_identity"],
        processing_complete=True)


def assert_current_account(root, plan, account_envelope):
    """Reject mixing current financial facts into an already frozen chain."""
    validate_envelope(account_envelope)
    require(account_envelope["stage"] == "account", "Current account check requires the first account envelope")
    store = Store(root, plan)
    current = _financial_snapshot(store)
    reference = account_envelope["account_ref"]
    if reference is None:
        unchanged = (account_envelope["payload"]["account_context"] == current["account_context"]
                     and not any(current["records"][key] is not None for key in (
                         "account", "capital_baseline", "capital_current", "risk_profile", "plan_constraints")))
    else:
        frozen = Artifacts(store.base).read_json(reference)
        require(frozen["account_context"] == account_envelope["payload"]["account_context"],
                "Account context differs from its immutable snapshot")
        unchanged = fingerprint(current) == account_envelope["account_hash"]
    if not unchanged:
        raise StaleSnapshot("Account, contributed principal, risk preference or plan changed; start a new business chain")
    return {"status": "passed", "account_hash": account_envelope["account_hash"]}
