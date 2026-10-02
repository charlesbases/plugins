"""Bind current marks and surface unconfirmed income without inventing receipts."""
from decimal import Decimal
from zoneinfo import ZoneInfo

import ledger
from contracts import fingerprint, instant, require, utc_now


def review_account(account, market, as_of):
    """Explain a current valuation blocker without inventing risk or returns."""
    snapshot = ledger.snapshot(account, as_of, market["prices"], market["price_dates"])
    if not snapshot["blocked"]:
        return None
    actions = [dict(row) for row in snapshot["reconciliation"]
               if row["kind"] in {"corporate_action_unverified", "holding_metadata_unverified"}]
    actions += [{"action": "reconcile_account_exposure", "reason": reason}
                for reason in snapshot["reasons"]
                if not reason.startswith(("corporate_action_unverified:", "holding_metadata_unverified:"))]
    return {"status": "awaiting_account", "as_of": as_of, "account_hash": fingerprint(account),
            "snapshot_hash": snapshot["snapshot_hash"], "reasons": snapshot["reasons"],
            "required_actions": actions}


def source_actions(market_record, as_of):
    """Include known distributions whose ex-date NAV has not arrived yet."""
    market, provenance = market_record["market_ref"], market_record["provenance"]
    actions = [dict(row) for row in provenance.get("corporate_actions", [])]
    existing = {(row["code"], row.get("ex_date", row.get("date"))) for row in actions}
    captures = {row["source_id"]: row for row in market_record["evidence_refs"] if row["role"] == "public_source"}
    for code, inventory in provenance.get("action_inventory", {}).get("by_code", {}).items():
        require(code in market["price_dates"], "Corporate action inventory lacks an asset mark date")
        require(instant(inventory["known_at"]) <= instant(as_of), "Corporate action source follows account review")
        capture = captures.get(inventory["source_id"])
        require(capture is not None and capture["artifact"]["sha256"] == inventory["source_sha256"],
                "Corporate action inventory source differs from captured evidence")
        for row in inventory["dividends"]:
            identity = code, row["date"]
            if row["date"] <= market["price_dates"][code] or identity in existing:
                continue
            actions.append({"code": code, "ex_date": row["date"], "record_date": row["record_date"],
                            "per_share": str(row["distribution_per_share"]), "pay_date": row["cash_payment_date"],
                            "currency": row["currency"], "rights_rule": row["rights_rule"]})
            existing.add(identity)
    return actions


def reconcile_market(account_id, market_record, store, artifacts, operation_id):
    import verify
    verify.references(market_record, artifacts)
    market = market_record["market_ref"]
    require("price_dates" in market, "Current marks require asset-specific price dates")
    key = operation_id + ":" + account_id
    if account_id != "main":
        # Trial projections and their source counters are owned by trial_outcome.
        return {"status": "trial_projection_managed_by_trial", "account_changed": False, "required_actions": []}
    with store.transaction() as conn:
        prior = store.get("market_account_reconciliation", key, conn)
        if prior is not None:
            require(prior["market_hash"] == fingerprint(market_record), "Reconciliation market identity changed")
            return prior["result"]
        account = store.get("account", account_id, conn)
        require(account is not None, "Account must be confirmed before market reconciliation")
        events = [value for identity, value in store.scan("ledger_event", conn)
                  if store.get("ledger_event_owner", identity, conn) == {"account_id": account_id}]
        openings = [row for row in events if row["type"] in {"opening", "performance_start"}]
        require(len(openings) == 1, "Reconciliation requires one complete immutable opening history")
        opened = instant(openings[0]["effective_at"])
        held = {row["code"] for row in account["lots"].values()}
        ever_owned = set(held)
        source_orders = {event["data"]["order"]["order_id"]: event["data"]["order"]["code"]
                         for event in events if event["type"] == "order_reserved"}
        payments = {event["data"]["payment_id"]: event["data"]["order_id"] for event in events if event["type"] == "subscription_pending"}
        for event in events:
            data = event["data"]
            if event["type"] in {"opening", "account_snapshot_confirmed"}:
                ever_owned.update(lot["code"] for lot in data["lots"])
            elif event["type"] == "buy_fill" and data["order_id"] in source_orders:
                ever_owned.add(source_orders[data["order_id"]])
            elif event["type"] == "subscription_confirmed" and payments.get(data["payment_id"]) in source_orders:
                ever_owned.add(source_orders[payments[data["payment_id"]]])
            elif event["type"] == "dividend_reinvested" or (event["type"] == "external_fill_confirmed" and data["side"] == "buy"):
                ever_owned.add(data["code"])
        current_day = instant(utc_now()).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        required = []
        for action in source_actions(market_record, utc_now()):
            day = action.get("ex_date", action.get("date"))
            require(type(day) is str, "Corporate action economic date required")
            if day > current_day:
                continue
            if action["code"] in held and market["price_dates"].get(action["code"], "") < day:
                required.append({"kind": "corporate_action_unverified", "code": action["code"], "ex_date": day,
                                 "action": "obtain_post_ex_date_NAV",
                                 "reason": "A pre-ex NAV plus dividend receivable double counts income; do not synthesize NAV by subtracting the distribution"})
            if action["code"] not in ever_owned:
                continue
            if day <= opened.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
                continue  # Already included in the explicitly confirmed opening balance.
            declared = [event for event in events if event["type"] == "dividend_declared"
                        and event["data"]["code"] == action["code"]
                        and instant(event["effective_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() == day]
            if len(declared) != 1:
                required.append({"kind": "corporate_action_unverified", "code": action["code"], "ex_date": day,
                                 "action": "confirm_record_date_entitled_shares_and_cash_or_reinvestment_election",
                                 "reason": "No unique confirmed entitlement; fund NAV does not prove account ownership or income election"})
                continue
            event = declared[0]
            if Decimal(event["data"]["per_share"]) != Decimal(str(action["per_share"])):
                required.append({"kind": "corporate_action_unverified", "code": action["code"], "ex_date": day,
                                 "action": "reconcile_declared_distribution_with_original_source"})
            record_at = event["data"]["record_at"]
            source_record_date = action.get("record_date")
            if source_record_date is None or instant(record_at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != source_record_date:
                required.append({"kind": "corporate_action_unverified", "code": action["code"], "ex_date": day,
                                 "action": "verify_original_distribution_record_date"})
            elif instant(record_at) >= opened:
                record_book = ledger.rebuild(ledger.initial_state("CNY"), events, utc_now(), record_at)
                shares = sum((Decimal(lot["shares"]) for lot in record_book["lots"].values()
                              if lot["code"] == action["code"] and instant(lot["ownership_at"]) <= instant(record_at)), Decimal(0))
                if record_book["unknown"] or record_book["pending_subscriptions"] or shares != Decimal(event["data"]["entitled_shares"]):
                    required.append({"kind": "corporate_action_unverified", "code": action["code"], "ex_date": day,
                                     "action": "reconcile_record_date_owned_shares_with_confirmed_entitlement"})
        at = utc_now()
        valuation = {"id": "market-valuation:" + fingerprint([operation_id, account_id]), "type": "valuation",
                     "sequence": account["sequence"] + 1, "effective_at": at, "known_at": at, "recorded_at": at,
                     "data": {"prices": market["prices"], "price_dates": market["price_dates"], "reconciliation": required}}
        updated = ledger.apply_event(account, valuation)
        # Derived blockers can be resolved by later confirmed ledger facts; the
        # immutable reconciliation receipt preserves each previous finding.
        store.put("ledger_event", valuation["id"], valuation, conn=conn)
        store.put("ledger_event_owner", valuation["id"], {"account_id": account_id}, conn=conn)
        store.put("ledger_event_evidence", valuation["id"], market_record["evidence_refs"], conn=conn)
        store.put("account", account_id, updated, immutable=False, conn=conn)
        result = {"status": "reconciliation_required" if required else "reconciled", "account_changed": True,
                  "account_hash": fingerprint(updated), "required_actions": required, "valuation_event_id": valuation["id"]}
        store.put("market_account_reconciliation", key, {"market_hash": fingerprint(market_record), "result": result}, conn=conn)
        return result
