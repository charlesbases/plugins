"""Confirmed external transfers with stable identity and append-only corrections."""
import copy
from decimal import Decimal
from zoneinfo import ZoneInfo

import ledger
from artifacts import Artifacts
from contracts import ConflictError, canonical_bytes, decimal_string, fields, fingerprint, instant, require, utc_now
import verify


def _references(items, store, *, required=False):
    require(type(items) is list, "Evidence references must be a list")
    def count(value):
        if isinstance(value, dict):
            if {"sha256", "size"} <= value.keys() and ("path" in value or "chunks" in value):
                return 1
            return sum(count(child) for child in value.values())
        if isinstance(value, list):
            return sum(count(child) for child in value)
        return 0
    require(not required or count(items) > 0, "Original valuation artifact references are required")
    verify.references(items, Artifacts(store.base))


def _source(value, at):
    fields(value, {"message", "confirmed_at"}, label="transfer confirmation")
    require(type(value["message"]) is str and value["message"].strip(), "Explicit transfer confirmation required")
    require(instant(value["confirmed_at"]) <= instant(at), "Confirmation cannot be in the future")


def _semantic(payload, store):
    require(payload["currency"] == "CNY", "Transfer currency must match the CNY account")
    value = decimal_string(payload["amount"], "confirmed external transfer")
    effective = instant(payload["effective_at"])
    require(effective <= instant(utc_now()), "Confirmed transfer cannot be in the future")
    reported_valuation = copy.deepcopy(payload["valuation"])
    valuation, valuation_error = None, None
    level = "not_provided"
    value_to_check = reported_valuation
    if value_to_check is not None:
        fields(value_to_check, {"at", "prices", "price_dates", "evidence_refs"}, {"source_market_id"}, label="flow valuation")
        try:
            require(instant(value_to_check["at"]) == effective, "Flow valuation instant differs from transfer")
            ledger._prices(value_to_check["prices"])
            ledger._mark_dates(value_to_check["prices"], value_to_check["price_dates"], utc_now())
            _references(value_to_check["evidence_refs"], store)
            market_id = value_to_check.get("source_market_id")
            require(type(market_id) is str and market_id, "Independent flow valuation requires a system source_market_id")
            market = store.get("market", market_id)
            require(market is not None and fingerprint(market) == market_id
                    and market["market_ref"]["schema_version"] == 4 and market["market_ref"]["currency"] == "CNY"
                    and market["provenance"].get("price_validation") == "verified_source_NAV", "Unknown or unverified source market")
            objects = Artifacts(store.base)
            verify.references(market, objects)
            nav = objects.read_json(market["provenance"]["nav_ref"])
            day = effective.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for code, price in value_to_check["prices"].items():
                rows = [row for row in nav.get(code, []) if row["date"] == day]
                require(value_to_check["price_dates"][code] == day and len(rows) == 1
                        and Decimal(str(rows[0]["nav"])) == Decimal(price), "Flow valuation differs from independently audited economic-date NAV")
            valuation = value_to_check
            level = "audited_fund_NAV_for_flow_economic_date"
        except (ValueError, OSError, KeyError) as exc:
            # A valuation gap must not reject independently confirmed money.
            level, valuation_error = "unverified_user_valuation", str(exc)
    require(type(payload["evidence_refs"]) is list, "Transfer evidence must be a list")
    _references(payload["evidence_refs"], store)
    return {"currency": "CNY", "amount": value, "effective_at": effective.isoformat(), "valuation": valuation,
            "reported_valuation_ref": Artifacts(store.base).put_bytes(canonical_bytes(reported_valuation)) if reported_valuation is not None else None,
            "valuation_evidence_level": level, "valuation_error": valuation_error}


def _economic(value):
    return {key: value[key] for key in ("currency", "amount", "effective_at")}


def _saved(store, operation_id, payload, conn=None):
    prior = store.get("cashflow_result", operation_id, conn)
    if prior is not None:
        require(prior["request_hash"] == fingerprint(payload), "Transfer operation input changed")
        return prior["result"]
    return None


def _save(store, operation_id, payload, result, conn):
    store.put("cashflow_result", operation_id, {"request_hash": fingerprint(payload), "result": result}, conn=conn)
    return result


def prepare(payload, store, operation_id):
    fields(payload, {"client_action_id", "identity", "currency", "amount", "effective_at", "valuation", "evidence_refs"}, label="transfer draft")
    require(type(payload["client_action_id"]) is str and payload["client_action_id"].strip(), "Stable client action identity required")
    identity = payload["identity"]
    require(type(identity) is dict and identity.get("kind") in {"manual", "provider"}, "Transfer identity kind required")
    if identity["kind"] == "provider":
        fields(identity, {"kind", "institution", "account_reference", "transaction_reference"}, label="provider transfer identity")
        require(all(type(value) is str and value.strip() for value in identity.values()), "Complete provider identity required")
        transfer_id = "provider:" + fingerprint(["main", identity])
    else:
        fields(identity, {"kind"}, label="manual transfer identity")
        transfer_id = "manual:" + fingerprint(["main", payload["client_action_id"]])
    semantic = _semantic(payload, store)
    require(ledger.amount(semantic["amount"]) != 0, "A new transfer must have a nonzero amount")
    draft_id = fingerprint(["main", payload["client_action_id"]])
    with store.transaction() as conn:
        saved = _saved(store, operation_id, payload, conn)
        if saved is not None:
            return saved
        prior = store.get("cashflow_draft", draft_id, conn)
        if prior is not None:
            require(prior["request_hash"] == fingerprint(payload), "Client action identity changed its draft")
            return _save(store, operation_id, payload, prior["result"], conn)
        candidates = [{"transfer_id": key, "revision_id": row["revision_id"], "semantic": row["semantic"]}
                      for key, row in store.scan("transfer_head", conn)
                      if key == transfer_id or (row["semantic"]["amount"] == semantic["amount"]
                          and row["semantic"]["effective_at"][:10] == semantic["effective_at"][:10])]
        draft = {"draft_id": draft_id, "transfer_id": transfer_id, "identity": identity,
                 "semantic": semantic, "evidence_refs": payload["evidence_refs"], "request_hash": fingerprint(payload)}
        draft_hash = fingerprint(draft)
        result = {"status": "confirmation_required", "draft_id": draft_id, "draft_hash": draft_hash,
                  "transfer_id": transfer_id, "candidates": candidates,
                  "required_action": "confirm_new_transfer_or_link_existing; matching_amount_is_not_deduplication"}
        store.put("cashflow_draft", draft_id, {**draft, "draft_hash": draft_hash, "result": result}, conn=conn)
        return _save(store, operation_id, payload, result, conn)


def _append(store, conn, transfer_id, identity, semantic, evidence, source, operation_id, previous=None, reason=None):
    account = store.get("account", "main", conn)
    require(account is not None, "Confirm the opening account before external transfers")
    prior = store.get("transfer_head", transfer_id, conn)
    require((None if prior is None else prior["revision_id"]) == previous, "Transfer revision changed; refresh before correction")
    revision_id = fingerprint({"transfer_id": transfer_id, "operation_id": operation_id,
                               "previous_revision": previous, "semantic": semantic, "source": source, "reason": reason})
    recorded = utc_now()
    event = {"id": "transfer:" + revision_id, "type": "cashflow", "sequence": account["sequence"] + 1,
             "effective_at": semantic["effective_at"], "known_at": recorded, "recorded_at": recorded,
             "data": {"transfer_id": transfer_id, "revision_id": revision_id, "previous_revision": previous,
                      "amount": semantic["amount"], "valuation": semantic["valuation"]}}
    ledger._event(event)
    history = [value for key, value in store.scan("ledger_event", conn)
               if store.get("ledger_event_owner", key, conn) == {"account_id": "main"}]
    state = ledger.rebuild(ledger.initial_state("CNY"), history + [event], recorded)
    first_sequence = prior["first_sequence"] if prior else event["sequence"]
    row = {"revision_id": revision_id, "previous_revision": previous, "identity": identity, "semantic": semantic,
           "event_id": event["id"], "first_sequence": first_sequence, "user_source": source,
           "reason": reason, "evidence_refs": evidence}
    store.put("ledger_event", event["id"], event, conn=conn)
    store.put("ledger_event_owner", event["id"], {"account_id": "main"}, conn=conn)
    store.put("ledger_event_evidence", event["id"], evidence, conn=conn)
    fact = ledger.financial_identity(event)
    store.put("financial_fact", "main:" + fact["key"], {"content_hash": fact["content_hash"], "event_id": event["id"]}, conn=conn)
    store.put("transfer_revision", revision_id, row, conn=conn)
    store.put("transfer_head", transfer_id, row, immutable=False, conn=conn)
    store.put("account", "main", state, immutable=False, conn=conn)
    baseline = store.get("capital_current", "main", conn) or store.get("capital_baseline", "main", conn)
    if baseline and (first_sequence <= baseline["account_sequence"] or instant(semantic["effective_at"]) < instant(baseline["as_of"])):
        pending = store.get("capital_reconciliation", "main", conn) or {"revision_ids": [], "resolved": True}
        store.put("capital_reconciliation", "main", {"revision_ids": pending["revision_ids"] + [revision_id], "resolved": False}, immutable=False, conn=conn)
    return {"status": "recorded", "transfer_id": transfer_id, "revision_id": revision_id,
            "event_id": event["id"], "account_hash": fingerprint(state), "sequence": state["sequence"],
            "performance_exact": not state["performance_pending"], "cash": state["cash"],
            "valuation_evidence_level": semantic["valuation_evidence_level"], "performance_pending": state["performance_pending"]}


def confirm(payload, store, operation_id):
    fields(payload, {"draft_id", "draft_hash", "choice", "existing_transfer_id", "user_source"}, label="transfer confirmation")
    _source(payload["user_source"], utc_now())
    require(payload["choice"] in {"new", "existing"}, "Explicit transfer identity decision required")
    with store.transaction() as conn:
        saved = _saved(store, operation_id, payload, conn)
        if saved is not None:
            return saved
        draft = store.get("cashflow_draft", payload["draft_id"], conn)
        require(draft is not None and draft["draft_hash"] == payload["draft_hash"], "Transfer draft changed or is unknown")
        prior_confirmation = store.get("cashflow_confirmation", payload["draft_id"], conn)
        if prior_confirmation is not None:
            require(prior_confirmation["request_hash"] == fingerprint(payload), "Draft was already confirmed differently")
            return _save(store, operation_id, payload, prior_confirmation["result"], conn)
        alias = store.get("transfer_identity_alias", draft["transfer_id"], conn)
        transfer_id = payload["existing_transfer_id"] if payload["choice"] == "existing" else (alias["transfer_id"] if alias else draft["transfer_id"])
        require(type(transfer_id) is str and transfer_id, "Selected transfer identity required")
        require(payload["choice"] == "existing" or payload["existing_transfer_id"] is None, "New transfer cannot link another identity")
        prior = store.get("transfer_head", transfer_id, conn)
        if prior is not None:
            require(alias is None or alias["transfer_id"] == transfer_id, "Confirmed transfer identity alias cannot be rebound")
            if draft["identity"]["kind"] == "provider" and prior["identity"]["kind"] == "provider":
                require(prior["identity"] == draft["identity"], "Distinct provider transaction identities cannot be merged by matching amount")
            if _economic(prior["semantic"]) != _economic(draft["semantic"]):
                raise ConflictError("Transfer identity carries conflicting economic facts; use an explicit correction")
            if transfer_id != draft["transfer_id"] and alias is None:
                store.put("transfer_identity_alias", draft["transfer_id"], {"transfer_id": transfer_id,
                          "identity": draft["identity"], "user_source": payload["user_source"]}, conn=conn)
            result = {"status": "repeated", "transfer_id": transfer_id, "revision_id": prior["revision_id"], "event_id": prior["event_id"]}
        else:
            require(payload["choice"] == "new", "Selected existing transfer is unknown")
            result = _append(store, conn, transfer_id, draft["identity"], draft["semantic"], draft["evidence_refs"],
                             payload["user_source"], operation_id)
        store.put("cashflow_confirmation", payload["draft_id"], {"request_hash": fingerprint(payload), "result": result}, conn=conn)
        return _save(store, operation_id, payload, result, conn)


def correct(payload, store, operation_id):
    fields(payload, {"transfer_id", "expected_revision", "amount", "effective_at", "valuation", "evidence_refs", "user_source", "reason"}, label="transfer correction")
    _source(payload["user_source"], utc_now())
    require(type(payload["reason"]) is str and payload["reason"].strip(), "Correction reason required")
    semantic = _semantic({**payload, "currency": "CNY"}, store)
    with store.transaction() as conn:
        saved = _saved(store, operation_id, payload, conn)
        if saved is not None:
            return saved
        prior = store.get("transfer_head", payload["transfer_id"], conn)
        require(prior is not None and prior["revision_id"] == payload["expected_revision"], "Transfer revision changed or is unknown")
        result = _append(store, conn, payload["transfer_id"], prior["identity"], semantic, payload["evidence_refs"],
                         payload["user_source"], operation_id, payload["expected_revision"], payload["reason"])
        return _save(store, operation_id, payload, result, conn)
