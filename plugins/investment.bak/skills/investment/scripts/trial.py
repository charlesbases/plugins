"""Prospective trials using the shared ledger and atomic Store commit packages.

Preparation and external-delivery progress are durable before the final business
transaction. Source documents prove their bytes, not independent broker identity.
"""
import copy
import datetime as dt
import hashlib
import tempfile
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import ledger
from contracts import (RetryableError, canonical_bytes, decimal_string, fields,
                       fingerprint, instant, require, utc_now)
from trial_evidence import anchor, verify_receipt, EvidenceTransportError, _atomic_evidence

SCRIPTS = Path(__file__).resolve().parent
OPERATIONS = {"trial_register", "trial_decision", "trial_outcome", "trial_evaluate", "trial_calibrate"}
PROTOCOL_FIELDS = {"schema_version", "trial_id", "name", "start_at", "end_at", "timezone", "schedule",
    "initial", "strategy", "benchmark", "horizon_days", "tail_probability", "risk_state",
    "validation_policy", "calibration_config", "trust", "evidence_refs",
    "observation_mode", "assumption_scope"}

def now():
    return instant(utc_now())

def sources():
    from verify import code_identity
    return code_identity()

def _write(kind, key, value, immutable=True):
    return {"kind": kind, "key": key, "value": value, "immutable": immutable}

def _expect(kind, key, value):
    return {"kind": kind, "key": key, "hash": None if value is None else fingerprint(value)}

def _package(result, writes=(), expected=()):
    return {"result": {"operation_complete": True, **result}, "writes": list(writes), "expected": list(expected)}

def _evidence(items, artifacts, cutoff, hashes=None):
    require(isinstance(items, list) and items, "Bound source artifacts required")
    seen = {}
    for item in items:
        fields(item, {"source_id", "source_url", "retrieved_at", "role", "artifact"}, label="source evidence")
        require(item["source_id"] not in seen, "Duplicate source identity")
        require(isinstance(item["source_url"], str) and item["source_url"].startswith("https://"), "HTTPS source provenance required")
        require(instant(item["retrieved_at"]) <= min(cutoff, now()), "Source was not yet known")
        data = artifacts.read(item["artifact"])
        require(data, "Empty source document")
        seen[item["source_id"]] = hashlib.sha256(data).hexdigest()
    if hashes is not None:
        require(hashes and all(seen.get(k) == v for k, v in hashes.items()), "Source hashes lack artifact backing")
    return seen

def _market(store, artifacts, identity, cutoff, universe, date=None):
    record = store.get("market", identity)
    require(record is not None, "Unknown system market record")
    fields(record, {"market_ref", "evidence_refs", "provenance"}, label="stored market")
    ref, provenance = record["market_ref"], record["provenance"]
    require(set(universe) <= set(ref["prices"]) and instant(ref["observed_at"]) <= cutoff, "Market coverage/time differs")
    _evidence(record["evidence_refs"], artifacts, cutoff, ref["source_hashes"])
    require(provenance.get("price_validation") == "verified_source_NAV", "Prices lack source NAV validation")
    if date is not None:
        require(all(ref["price_dates"][code] == date for code in universe), "NAV attribution date differs")
    for value in ref["prices"].values():
        decimal_string(value, "NAV", positive=True)
    return record

def validate_protocol(protocol):
    from strategy import validate_spec
    fields(protocol, PROTOCOL_FIELDS, label="trial protocol")
    require(protocol["schema_version"] == 4, "Unsupported trial contract")
    identity = protocol["trial_id"]
    require(isinstance(identity, str) and 0 < len(identity) <= 96 and all(c.isalnum() or c in "-_" for c in identity), "Invalid trial ID")
    require(isinstance(protocol["name"], str) and protocol["name"].strip(), "Trial name required")
    zone = ZoneInfo(protocol["timezone"])
    start, end = instant(protocol["start_at"]), instant(protocol["end_at"])
    require(start < end, "Fixed endpoint must follow start")
    fields(protocol["initial"], {"account_id", "account_hash", "as_of", "market_id", "date"}, label="initial binding")
    initial = protocol["initial"]
    require(instant(initial["as_of"]) <= start, "Initial state follows trial start")
    fields(protocol["strategy"], {"actor_identity", "spec"}, label="registered strategy")
    spec = validate_spec(protocol["strategy"]["spec"])
    trade_policy = spec["trade_policy"]
    require(trade_policy["registration"] == "declared", "Illustrative trade policies cannot register formal trials")
    family = trade_policy["family"]
    require(instant(family["start_at"]) <= start < end <= instant(family["end_at"]),
            "Formal trial must remain inside its predeclared comparison family")
    require(spec["timing"]["timezone"] == protocol["timezone"], "Strategy timezone differs")
    require(isinstance(protocol["strategy"]["actor_identity"], str) and protocol["strategy"]["actor_identity"], "Actor identity required")
    horizon = protocol["horizon_days"]
    require(type(horizon) is int and horizon > 0 and spec["planning"]["primary_horizon_days"] == horizon, "Risk horizons differ")
    require(spec["allocation"]["tail_probability"] == protocol["tail_probability"], "Tail probabilities differ")
    risk = protocol["risk_state"]
    fields(risk, {"profile_hash", "capital_baseline", "net_principal", "loss_tolerance", "principal_floor",
                  "remaining_loss_budget", "currency", "as_of", "valid_until"}, label="registered principal risk")
    require(0 <= Decimal(decimal_string(risk["loss_tolerance"])) < 1, "Invalid principal loss tolerance")
    require(risk["valid_until"] is None or instant(risk["valid_until"]) >= end,
            "Fixed-profile trial must end before its risk preference expires or changes")
    schedule = protocol["schedule"]
    require(isinstance(schedule, list) and 0 < len(schedule) <= 10000, "Bounded observation schedule required")
    dates, previous = [], instant(initial["as_of"])
    for row in schedule:
        fields(row, {"date", "decision_deadline", "execution_at", "valuation_at", "outcome_not_before", "outcome_deadline"}, label="schedule")
        day, cutoff, valued = dt.date.fromisoformat(row["date"]), instant(row["decision_deadline"]), instant(row["valuation_at"])
        require(start <= cutoff < instant(row["execution_at"]) <= valued <= instant(row["outcome_not_before"])
                <= instant(row["outcome_deadline"]) <= end and previous < instant(row["execution_at"]),
                "Invalid observation timing: execution must follow the previous valuation")
        require(valued.astimezone(zone).date() == day, "Valuation date/time mismatch")
        dates.append(day)
        previous = valued
    require(dates == sorted(set(dates)), "Observation dates must be ordered and unique")
    calibration = protocol["calibration_config"]
    require(calibration["sample_size"] == len(dates) and calibration["dates"] == [str(day) for day in dates], "Calibration differs from the exact trial calendar")
    fields(protocol["benchmark"], {"description", "rationale"}, label="benchmark")
    require(protocol["benchmark"]["description"].strip() and protocol["benchmark"]["rationale"].strip(), "Comparator rationale required")
    require(spec["benchmark"]["rule"] == "fixed_weights", "A fixed-weight passive comparator is required")
    decision_dates = {str(instant(row["decision_deadline"]).astimezone(zone).date()) for row in schedule}
    require(set(spec["benchmark"]["rebalance_dates"]) <= decision_dates, "Benchmark rebalance date lacks a scheduled decision")
    require(protocol["observation_mode"] in ("documented_rule_simulation", "user_confirmed_execution"), "Unknown observation mode")
    require(isinstance(protocol["assumption_scope"], str) and protocol["assumption_scope"].strip(), "Inference limitations required")
    return dates

def _formal_inference_policy(protocol):
    """One fixed-endpoint primary comparison, with a finite protocol family."""
    family = protocol["strategy"]["spec"]["trade_policy"]["family"]
    policy = copy.deepcopy(protocol["validation_policy"])
    from allocation_statistics import _inference_inputs
    _inference_inputs([], [], policy)
    divisor = family["max_candidates"] * family["max_reviews"]
    policy["confidence_level"] = 1 - (1 - policy["confidence_level"]) / divisor
    policy["mc_failure_probability"] /= divisor
    return policy

def _calculation(protocol):
    from calibration import calibrate
    return calibrate(protocol["calibration_config"], _formal_inference_policy(protocol))

def _trust_snapshot(protocol, artifacts):
    from trial_evidence import _trust
    trust = copy.deepcopy(protocol["trust"])
    checked = _trust(trust)
    trust.pop("ca_file")
    return {"config": trust, "ca_ref": artifacts.put_bytes(checked["ca_bytes"])}

def _verify_stamp(document, stamp, trust, artifacts, cutoff=None):
    with tempfile.TemporaryDirectory(prefix="investment-evidence-") as temporary:
        base = Path(temporary)
        (base / "ca.pem").write_bytes(artifacts.read(trust["ca_ref"]))
        folder = base / "timestamp"
        folder.mkdir()
        for name, field in (("request.tsq", "request_ref"), ("response.tsr", "response_ref")):
            (folder / name).write_bytes(artifacts.read(stamp[field]))
        return verify_receipt(canonical_bytes(document), folder, {**trust["config"], "ca_file": str(base / "ca.pem")}, not_after=cutoff)

def _mature_receipt(prepared, receipt):
    if prepared.get("not_before"):
        require(instant(receipt["gen_time"]) - dt.timedelta(seconds=receipt["accuracy_seconds"]) >= instant(prepared["not_before"]),
                "Signed outcome may precede maturity; a later valid timestamp is required")


def _recover_receipt(prepared, prior, store, artifacts, operation_id):
    """Read former attempts; a new owner never modifies an old owner's files."""
    if not prior or prior.get("query_ref") is None:
        return None
    directories = [prior["directory"]] if prior.get("directory") else []
    directories += [row["directory"] for _, row in store.scan("external_attempt")
                    if row["operation_id"] == operation_id and row["document_hash"] == fingerprint(prepared["document"])]
    for relative in dict.fromkeys(directories):
        folder = (store.base / relative).resolve()
        require(folder.is_relative_to(store.base / "work"), "Timestamp attempt escapes operation storage")
        evidence = folder / "timestamp"
        candidates = ([evidence / "response.tsr"] if (evidence / "response.tsr").is_file() else [])
        if evidence.is_dir():
            candidates += sorted(evidence.glob(".response.tsr.*.tmp"))
        for candidate in candidates:
            if candidate.is_symlink() or candidate.stat().st_size > 1024 * 1024:
                continue
            try:
                stamp = {"request_ref": prior["query_ref"], "response_ref": artifacts.put_bytes(candidate.read_bytes())}
                receipt = _verify_stamp(prepared["document"], stamp, prepared["trust"], artifacts, prepared["deadline"])
                _mature_receipt(prepared, receipt)
            except (ValueError, OSError):
                continue
            store.put("external_operation", operation_id, {**prior, "phase": "receipt_ready", "stamp": stamp,
                      "recovered_from": relative}, immutable=False)
            return stamp, receipt
    return None


def _seal(prepared, store, artifacts, operation_id):
    document, trust, cutoff = prepared["document"], prepared["trust"], prepared["deadline"]
    bound = fingerprint(document)
    prior = store.get("external_operation", operation_id)
    if prior:
        require(prior["document_hash"] == bound, "External operation payload conflict")
        if prior["phase"] == "receipt_ready":
            receipt = _verify_stamp(document, prior["stamp"], trust, artifacts, cutoff)
            _mature_receipt(prepared, receipt)
            return prior["stamp"], receipt
    recovered = _recover_receipt(prepared, prior, store, artifacts, operation_id)
    if recovered is not None:
        return recovered
    folder = store.attempt_directory("timestamp")
    store.put("external_attempt", fingerprint([operation_id, str(folder)]), {"operation_id": operation_id,
              "document_hash": bound, "directory": folder.relative_to(store.base).as_posix()})
    ca_file, ca = folder / "ca.pem", artifacts.read(trust["ca_ref"])
    if not ca_file.exists() or ca_file.read_bytes() != ca:
        _atomic_evidence(ca_file, ca)
    state = {"document_hash": bound, "phase": "request_ready", "directory": str(folder.relative_to(store.base)),
             "query_ref": prior.get("query_ref") if prior else None}
    store.put("external_operation", operation_id, state, immutable=False)
    def bind_query(query):
        if state["query_ref"] is not None:
            require(artifacts.read(state["query_ref"]) == query, "Fixed timestamp query changed")
            return
        reference = artifacts.put_bytes(query)
        state["query_ref"] = reference
        store.put("external_operation", operation_id, state, immutable=False)
    try:
        result = anchor(canonical_bytes(document), folder / "timestamp", {**trust["config"], "ca_file": str(ca_file)},
                        not_after=cutoff, guard=store.assert_owned, on_query=bind_query,
                        fixed_query=artifacts.read(state["query_ref"]) if state["query_ref"] else None)
    except EvidenceTransportError as exc:
        store.put("external_operation", operation_id, {**state, "phase": "delivery_unknown", "reason": str(exc)}, immutable=False)
        raise RetryableError(str(exc)) from exc
    _mature_receipt(prepared, result)
    stamp = {"request_ref": artifacts.put_bytes((folder / "timestamp/request.tsq").read_bytes()), "response_ref": artifacts.put_bytes((folder / "timestamp/response.tsr").read_bytes())}
    store.put("external_operation", operation_id, {**state, "phase": "receipt_ready", "stamp": stamp}, immutable=False)
    return stamp, result

def _prepared(operation, payload, store, artifacts, operation_id, build):
    identity = fingerprint({"operation": operation, "payload": payload})
    saved = store.get("trial_preparation", operation_id)
    if saved is not None:
        require(saved["input_hash"] == identity, "Trial preparation operation conflict")
        return artifacts.read_json(saved["artifact"])
    value = build()
    store.put("trial_preparation", operation_id, {"input_hash": identity, "artifact": artifacts.put_json(value)})
    return value

def _finish(prepared, store, artifacts, operation_id):
    stamp, receipt = _seal(prepared, store, artifacts, operation_id)
    if prepared.get("not_before"):
        require(instant(receipt["gen_time"]) - dt.timedelta(seconds=receipt["accuracy_seconds"]) >= instant(prepared["not_before"]), "Signed outcome may precede maturity")
    record = {"document": prepared["document"], "timestamp": stamp}
    writes = prepared["writes"] + [_write(prepared["record_kind"], prepared["record_key"], record)]
    return _package({**prepared["result"], "timestamp": receipt["gen_time"], "timestamp_revocation_checked": receipt.get("revocation_checked", False)}, writes, prepared["expected"])

def _load(trial_id, store, artifacts):
    trial = store.get("trial", trial_id)
    require(trial is not None, "Unknown trial")
    document = trial["document"]
    protocol = artifacts.read_json(document["protocol_ref"])
    require(document["source_hashes"] == sources(), "Registered implementation changed")
    validate_protocol(protocol)
    receipt = _verify_stamp(document, trial["timestamp"], document["trust"], artifacts, protocol["start_at"])
    _evidence(protocol["evidence_refs"], artifacts, instant(receipt["upper_time_bound"]))
    state = store.get("trial_state", trial_id)
    require(state is not None and state["registration_hash"] == fingerprint(document), "Trial state binding differs")
    return trial, protocol, state

def _slot(protocol, date):
    matches = [item for item in protocol["schedule"] if item["date"] == date]
    require(len(matches) == 1, "Date outside frozen schedule")
    return matches[0]

def _account_id(trial_id, role):
    return "trial:" + trial_id + ":" + role

def _event_key(trial_id, sequence):
    return trial_id + ":" + f"{sequence:08d}"

def _history(store, state, trial_id, role):
    events = []
    for number in range(1, state["ledger_counts"][role] + 1):
        item = store.get("trial_ledger_event", trial_id + ":" + role + ":" + f"{number:08d}")
        require(item is not None, "Missing trial ledger fact")
        events.append(item["event"])
    return events

def _ledger_plan(store, artifacts, registration, state, trial_id, role, additions, known_at, source_links=None):
    initial = artifacts.read_json(registration["initial_account_ref"])
    events = _history(store, state, trial_id, role)
    writes, count, batch = [], state["ledger_counts"][role], {}
    for source in additions:
        identity = trial_id + ":" + role + ":" + source["id"]
        source_hash = fingerprint({k: v for k, v in source.items() if k != "sequence"})
        seen = store.get("trial_fact", identity) or batch.get(identity)
        if seen is not None:
            require(seen["source_hash"] == source_hash, "Trial fact identity conflict")
            continue
        count += 1
        key = trial_id + ":" + role + ":" + f"{count:08d}"
        event = {**source, "sequence": initial["sequence"] + count}
        events.append(event)
        batch[identity] = {"source_hash": source_hash, "event_key": key}
        record = {"event": event, "source_hash": source_hash}
        if source_links and source["id"] in source_links:
            record["source"] = source_links[source["id"]]
        writes += [_write("trial_ledger_event", key, record), _write("trial_fact", identity, batch[identity])]
    return ledger.rebuild(initial, events, known_at), events, count, writes

def _register(payload, store, artifacts, operation_id):
    fields(payload, {"protocol"}, label="registration request")
    protocol = payload["protocol"]
    validate_protocol(protocol)
    trial_id = protocol["trial_id"]
    present = store.get("trial", trial_id)
    if present is not None:
        require(present["document"]["request_hash"] == fingerprint(payload), "Trial identity content conflict")
        _load(trial_id, store, artifacts)
        return _package({"status": "registered", "trial_id": trial_id, "reused": True, "actual_account_changed": False})
    def build():
        import risk_profile as profile
        require(now() < instant(protocol["start_at"]), "New registration must precede trial start")
        initial = protocol["initial"]
        account = store.get("account", initial["account_id"])
        require(account is not None and fingerprint(account) == initial["account_hash"], "Initial account snapshot changed")
        from verify import monitor_codes
        initial_codes = sorted(set(monitor_codes(account)) | set(protocol["strategy"]["spec"]["benchmark"]["weights"]))
        market = _market(store, artifacts, initial["market_id"], instant(initial["as_of"]), initial_codes, initial["date"])
        snapshot = ledger.snapshot(account, initial["as_of"], market["market_ref"]["prices"], market["market_ref"]["price_dates"])
        require(not snapshot["blocked"] and snapshot["performance_exact"] and Decimal(snapshot["equity"]) > 0, "Initial account needs exact performance reconciliation")
        require(not snapshot["open_orders"] and not account["receivables"], "Settle opening orders and receivables first")
        risk = profile.resolve(store, account, initial["as_of"], market["market_ref"]["prices"], initial["account_id"],
                               price_dates=market["market_ref"]["price_dates"])
        require(risk == protocol["risk_state"], "Registered capital/risk differs from confirmed account profile")
        profile_pointer = store.get("risk_profile", initial["account_id"])
        require(fingerprint(profile_pointer) == risk["profile_hash"], "Risk profile changed during registration; recompute")
        from newtrade_guard import source_prefix
        opening_trade_facts = source_prefix(store, initial["account_id"], account["sequence"], initial["as_of"])
        family = protocol["strategy"]["spec"]["trade_policy"]["family"]
        family_key = initial["account_id"] + ":" + fingerprint(family)
        prior_family = store.get("trial_comparison_family", family_key)
        family_count = 0 if prior_family is None else prior_family["registered_protocols"]
        require(family_count < family["max_reviews"], "Registered formal-comparison family is exhausted")
        comparison_family = {"family_hash": fingerprint(family), "registered_protocols": family_count + 1,
                             "maximum_protocols": family["max_reviews"],
                             "error_divisor": family["max_candidates"] * family["max_reviews"],
                             "primary_comparison": "strategy_vs_registered_fixed_benchmark_at_fixed_endpoint"}
        trial_baseline = {**risk["capital_baseline"], "principal": risk["net_principal"],
                          "account_sequence": account["sequence"], "account_hash": fingerprint(account),
                          "source_baseline_hash": fingerprint(risk["capital_baseline"])}
        _evidence(protocol["evidence_refs"], artifacts, instant(protocol["start_at"]))
        calibration = _calculation(protocol)
        document = {"schema_version": 4, "kind": "registration", "trial_id": trial_id, "request_hash": fingerprint(payload), "protocol_ref": artifacts.put_json(protocol), "initial_account_ref": artifacts.put_json(account), "initial_market_hash": fingerprint(market), "initial_unit_nav": snapshot["unit_nav"], "trial_capital_baseline": trial_baseline, "source_hashes": sources(), "calibration_ref": artifacts.put_json(calibration), "comparison_family": comparison_family, "trust": _trust_snapshot(protocol, artifacts)}
        document["initial_trade_events_ref"] = artifacts.put_json(opening_trade_facts)
        state = {"schema_version": 4, "registration_hash": fingerprint(document), "event_count": 0, "head_hash": fingerprint(document), "ledger_counts": {"strategy": 0, "benchmark": 0}, "last_completed_date": initial["date"], "last_unit_nav": dict.fromkeys(("strategy", "benchmark"), snapshot["unit_nav"])}
        writes = [_write("trial_state", trial_id, state, False),
                  _write("trial_comparison_family", family_key, comparison_family, False)]
        expected = [_expect("trial", trial_id, None), _expect("account", initial["account_id"], account),
                    _expect("risk_profile", initial["account_id"], profile_pointer),
                    _expect("trial_comparison_family", family_key, prior_family)]
        strategy_id = _account_id(trial_id, "strategy")
        writes += [_write("risk_profile", strategy_id, profile_pointer, False),
                   _write("capital_baseline", strategy_id, trial_baseline),
                   _write("capital_current", strategy_id, trial_baseline, False),
                   _write("trial_risk_binding", strategy_id, {"trial_id": trial_id,
                           "source_account_id": initial["account_id"], "profile_hash": risk["profile_hash"]})]
        plan = store.get("plan_constraints", initial["account_id"])
        require(plan is not None, "Confirm the investment plan before registering a trial")
        expected.append(_expect("plan_constraints", initial["account_id"], plan))
        writes.append(_write("plan_constraints", strategy_id, plan, False))
        for role in ("strategy", "benchmark"):
            key = _account_id(trial_id, role)
            expected.append(_expect("account", key, None))
            writes.append(_write("account", key, copy.deepcopy(account), False))
        return {"document": document, "trust": document["trust"], "deadline": protocol["start_at"], "record_kind": "trial", "record_key": trial_id, "writes": writes, "expected": expected, "result": {"status": "registered", "trial_id": trial_id, "account_ids": {role: _account_id(trial_id, role) for role in ("strategy", "benchmark")}, "calibration_status": calibration["status"], "actual_account_changed": False}}
    return _finish(_prepared("trial_register", payload, store, artifacts, operation_id, build), store, artifacts, operation_id)

def _rebuild_context(spec, account, market, context, risk, trade_inputs, *, account_id):
    """Rebuild after source verification using the current authoritative book."""
    from strategy import build_context
    from pipeline import future_actions
    return build_context(spec, account, context["decision_at"], market["market_ref"],
        risk_state=risk, purchase_eligible_codes=context["purchase_eligible_codes"],
        dynamic_universe=context["universe"], candidate_identities=context["identities"],
        account_id=account_id, sealed_intervention=context.get("sealed_intervention"),
        news_review_hash=context.get("news_review_hash"), industry_data_ref=context["industry_data_ref"],
        known_future_actions=future_actions(market, context["decision_at"]), action_inventory_scope="known_subset",
        trade_state=trade_inputs["trade_state"], trade_family_review_index=trade_inputs["trade_family_review_index"],
        readiness_ref=context.get("readiness_ref"), family_id=context.get("family_id"),
        sector_exposure_bounds=context.get("sector_exposure_bounds"),
        paired_currency_validation=context.get("paired_currency_validation"))


def _decision(payload, store, artifacts, operation_id):
    from execution import reserve_events, compile_benchmark
    fields(payload, {"trial_id", "date", "decision_id", "cashflows", "distributions", "evidence_refs"}, label="trial decision request")
    trial_id, date = payload["trial_id"], payload["date"]
    registration, protocol, current = _load(trial_id, store, artifacts)
    require(store.get("trial_scope_violation", trial_id) is None,
            "Trial execution departed from its registered scope; preserve this episode and register a new one")
    slot, key = _slot(protocol, date), trial_id + ":" + date
    existing = store.get("trial_slot", key)
    if existing is not None:
        require(existing["request_hash"] == fingerprint(payload), "Observation has a different decision")
        return _package({"status": "recorded", "trial_id": trial_id, "date": date, "reused": True})
    def build():
        require(now() <= instant(slot["decision_deadline"]), "Decision cutoff passed")
        bundle = store.get("decision", payload["decision_id"])
        require(bundle is not None, "Unknown system decision")
        require(bundle["source_hashes"] == registration["document"]["source_hashes"],
                "Decision implementation differs from the registered implementation")
        context = bundle["context"]
        require(context is not None and not context["blocked"], "Trial decision requires confirmed execution exposure; keep monitoring pending orders")
        account_id = _account_id(trial_id, "strategy")
        account = store.get("account", account_id)
        require(context["account_id"] == account_id and context["account_hash"] == fingerprint(account) and bundle["account_hash"] == fingerprint(account), "Decision does not bind the current trial account")
        require(context["spec_hash"] == fingerprint(protocol["strategy"]["spec"]) and bundle["spec_hash"] == context["spec_hash"], "Strategy specification changed")
        require(instant(protocol["start_at"]) <= instant(context["decision_at"]) <= instant(slot["decision_deadline"]), "Decision outside trial information window")
        zone = ZoneInfo(protocol["timezone"])
        require(instant(context["decision_at"]).astimezone(zone).date()
                == instant(slot["decision_deadline"]).astimezone(zone).date(), "Decision belongs to another scheduled information date")
        import risk_profile as profile
        market = _market(store, artifacts, bundle["market_id"], instant(context["decision_at"]), context["universe"])
        from verify import verify_bundle
        verify_bundle(bundle, artifacts, store=store)
        source_profile = store.get("risk_profile", protocol["initial"]["account_id"])
        risk = profile.resolve(store, account, context["decision_at"], market["market_ref"]["prices"], account_id,
                               price_dates=market["market_ref"]["price_dates"])
        require(fingerprint(source_profile) == risk["profile_hash"], "Risk profile changed during decision; recompute")
        from newtrade_guard import read_inputs
        trade_inputs = read_inputs(store, artifacts, account_id, protocol["strategy"]["spec"],
                                   context["decision_at"], decision_id=payload["decision_id"])
        require(_rebuild_context(protocol["strategy"]["spec"], account, market, context, risk, trade_inputs,
                                 account_id=account_id) == context,
                "Decision context is not reproducible from authoritative state")
        if context.get("sealed_intervention"):
            require(context["sealed_intervention"]["actor"] == protocol["strategy"]["actor_identity"], "Intervention actor changed")
        orders = copy.deepcopy(bundle["orders"]["orders"])
        fields(payload["cashflows"], {"open", "close"}, label="cashflows")
        for amount in payload["cashflows"].values():
            decimal_string(amount, "cashflow")
        require(isinstance(payload["distributions"], list), "Explicit distribution schedule required")
        identities = set()
        for item in payload["distributions"]:
            fields(item, {"code", "ex_date", "record_at", "per_share", "pay_at", "income_mode"}, label="distribution")
            identity = (item["code"], item["ex_date"])
            require(identity not in identities, "Duplicate prospective corporate action")
            identities.add(identity)
            require(item["code"] in context["universe"]
                    and _previous_date(protocol, date) < item["ex_date"] <= date
                    and Decimal(decimal_string(item["per_share"])) > 0
                    and instant(item["record_at"]) <= _ex_time(protocol, item["ex_date"])
                    and item["income_mode"] in {"cash", "reinvest"}
                    and instant(item["pay_at"]) >= _ex_time(protocol, item["ex_date"]), "Invalid distribution terms")
        _evidence(payload["evidence_refs"], artifacts, instant(context["decision_at"]))
        next_state, writes = copy.deepcopy(current), []
        expected = [_expect("trial_state", trial_id, current), _expect("trial_slot", key, None)]
        benchmark_account = store.get("account", _account_id(trial_id, "benchmark"))
        comparator = compile_benchmark(protocol["strategy"]["spec"], benchmark_account,
                                       context["decision_at"], market["market_ref"], account_id=_account_id(trial_id, "benchmark"),
                                       initial_allocation=date == protocol["schedule"][0]["date"])
        require(comparator["status"] != "blocked", "Benchmark execution is blocked; reconcile before sealing")
        both = {"strategy": orders, "benchmark": comparator["orders"]}
        expected.append(_expect("risk_profile", protocol["initial"]["account_id"], source_profile))
        require(len({o["order_id"] for rows in both.values() for o in rows}) == sum(map(len, both.values())), "Paired order IDs must be unique")
        for role, frozen in both.items():
            identity = _account_id(trial_id, role)
            before = account if role == "strategy" else benchmark_account
            expected.append(_expect("account", identity, before))
            events = reserve_events(before, frozen, context["decision_at"])
            after, _, count, entries = _ledger_plan(store, artifacts, registration["document"], current, trial_id, role, events, utc_now())
            writes += entries + [_write("account", identity, after, False)]
            next_state["ledger_counts"][role] = count
        body = {**copy.deepcopy(payload), "orders": both, "benchmark_compilation": comparator,
                "universe": context["universe"], "universe_policy": context["spec"]["universe_policy"],
                "identity_ref": artifacts.put_json(context["identities"]),
                "decision_hash": fingerprint(bundle), "account_hash": fingerprint(account), "context_hash": context["context_hash"]}
        document = {"kind": "decision", "trial_id": trial_id, "sequence": current["event_count"] + 1, "previous_hash": current["head_hash"], "registration_hash": current["registration_hash"], "body": body}
        next_state.update(event_count=document["sequence"], head_hash=fingerprint(document))
        writes += [_write("trial_state", trial_id, next_state, False), _write("trial_slot", key, {"request_hash": fingerprint(payload), "decision": body, "decision_event": _event_key(trial_id, document["sequence"]), "benchmark_simulated": False}, False)]
        return {"document": document, "trust": registration["document"]["trust"], "deadline": slot["decision_deadline"], "record_kind": "trial_event", "record_key": _event_key(trial_id, document["sequence"]), "writes": writes, "expected": expected, "result": {"status": "recorded", "trial_id": trial_id, "date": date, "event_sequence": document["sequence"], "actual_account_changed": False}}
    return _finish(_prepared("trial_decision", payload, store, artifacts, operation_id, build), store, artifacts, operation_id)

def _fact(identity, kind, data, effective, known):
    return {"id": identity, "type": kind, "sequence": 1, "effective_at": effective, "known_at": known, "recorded_at": known, "data": data}


def _projection(initial, history, additions, known, effective):
    numbered = [{**row, "sequence": initial["sequence"] + len(history) + index + 1} for index, row in enumerate(additions)]
    return ledger.rebuild(initial, history + numbered, known, effective)


def _flow_valuation(book, at, market, artifacts):
    """Each book uses its OWN holdings and independently bound date NAVs."""
    codes = {lot["code"] for lot in book["lots"].values()}
    if not codes:
        return None
    reference = market["provenance"].get("nav_ref")
    nav = artifacts.read_json(reference) if reference else {}
    day = instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    prices = {}
    for code in codes:
        rows = [row for row in nav.get(code, []) if row["date"] == day]
        if len(rows) != 1:
            return None
        prices[code] = str(rows[0]["nav"])
    return {"at": at, "prices": prices, "price_dates": dict.fromkeys(prices, day), "evidence_refs": [reference]}


def _flow_fact(identity, amount, at, known, initial, history, additions, market, artifacts):
    book = _projection(initial, history, additions, known, at)
    return _fact(identity, "cashflow", {"transfer_id": identity, "revision_id": fingerprint([identity, amount]),
                 "previous_revision": None, "amount": amount, "valuation": _flow_valuation(book, at, market, artifacts)}, at, known)


def _check_flow_valuation(event, market, artifacts):
    valuation = event["data"]["valuation"]
    if valuation is None:
        return  # Noncash exposure then remains performance_pending in ledger.
    require(instant(valuation["at"]) == instant(event["effective_at"]), "Cashflow valuation instant differs from the real flow")
    nav = artifacts.read_json(market["provenance"]["nav_ref"])
    day = instant(event["effective_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    for code, price in valuation["prices"].items():
        rows = [row for row in nav.get(code, []) if row["date"] == day]
        require(valuation["price_dates"][code] == day and len(rows) == 1
                and Decimal(str(rows[0]["nav"])) == Decimal(price), "Cashflow valuation differs from independently audited economic-date NAV")


def _dividend_fact(identity, code, distribution, initial, history, additions, known):
    require(initial["effective_at"] is None or instant(distribution["record_at"]) >= instant(initial["effective_at"]),
            "Simulated record-date entitlement predates confirmed opening history")
    record = _projection(initial, history, additions, known, distribution["record_at"])
    require(not record["unknown"] and not record["pending_subscriptions"], "Record-date ownership requires reconciliation")
    shares = sum((Decimal(lot["shares"]) for lot in record["lots"].values()
                  if lot["code"] == code and instant(lot["ownership_at"]) <= instant(distribution["record_at"])), Decimal(0))
    return _fact(identity, "dividend_declared", {"distribution_id": identity, "code": code,
                 "per_share": distribution["per_share"], "pay_at": distribution["pay_at"],
                 "record_at": distribution["record_at"], "entitled_shares": ledger.decimal(shares)},
                 distribution["ex_at"], known)

def _previous_date(protocol, date):
    earlier = [item["date"] for item in protocol["schedule"] if item["date"] < date]
    return earlier[-1] if earlier else protocol["initial"]["date"]

def _ex_time(protocol, date):
    day = dt.date.fromisoformat(date)
    require(day.isoformat() == date, "Canonical corporate action date required")
    return dt.datetime.combine(day, dt.time(), ZoneInfo(protocol["timezone"])).astimezone(dt.timezone.utc)

def _corporate_actions(protocol, frozen, market, date):
    """Compare every ex-date in the observation interval, including interior days."""
    previous, proof = _previous_date(protocol, date), market["provenance"]
    universe = frozen["decision"]["universe"]
    coverage = proof.get("coverage_by_code", {})
    require(all(code in coverage and coverage[code]["start"] <= previous and coverage[code]["end"] >= date for code in universe),
            "Corporate action evidence does not cover the complete observation interval")
    rows = proof.get("corporate_actions")
    require(isinstance(rows, list), "Complete source corporate action inventory required")
    actual = {}
    for row in rows:
        fields(row, {"code", "date", "ex_date", "record_date", "per_share", "pay_date", "rights_rule", "dividend_mode", "currency", "distribution_evidence"}, label="source corporate action")
        _ex_time(protocol, row["date"])
        amount = Decimal(decimal_string(row["per_share"], "distribution", positive=True))
        if row["code"] in universe and previous < row["date"] <= date:
            key = (row["code"], row["date"])
            require(key not in actual, "Duplicate source corporate action")
            actual[key] = {**row, "amount": amount}
    declared = {(row["code"], row["ex_date"]): row for row in frozen["decision"]["distributions"]}
    require(len(declared) == len(frozen["decision"]["distributions"])
            and set(declared) == set(actual), "Prospective corporate action inventory omits or invents an interval event")
    missing = []
    for key, row in actual.items():
        require(Decimal(declared[key]["per_share"]) == row["amount"], "Distribution differs from source NAV")
        if row["record_date"] is None or row["rights_rule"] is None:
            missing.append({"code": key[0], "ex_date": key[1], "field": "record_date_or_rights_rule"})
        else:
            require(instant(declared[key]["record_at"]).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat() == row["record_date"],
                    "Record date differs from original source")
        declared[key] = {**declared[key], "ex_at": _ex_time(protocol, key[1]).isoformat()}
        if row["pay_date"] is None:
            missing.append({"code": key[0], "ex_date": key[1]})
        else:
            _ex_time(protocol, row["pay_date"])
            require(instant(declared[key]["pay_at"]).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat()
                    == row["pay_date"], "Payment date differs from source evidence")
    return declared, missing

def _outcome(payload, store, artifacts, operation_id):
    fields(payload, {"trial_id", "date", "market_id", "strategy_event_ids", "evidence_refs"}, label="trial outcome request")
    trial_id, date = payload["trial_id"], payload["date"]
    registration, protocol, current = _load(trial_id, store, artifacts)
    slot, key = _slot(protocol, date), trial_id + ":" + date
    frozen = store.get("trial_slot", key)
    require(frozen is not None, "Outcome has no committed decision")
    existing = store.get("trial_observation", key)
    if existing is not None:
        require(existing["request_hash"] == fingerprint(payload), "Completed observation cannot be replaced")
        return _package({"status": "observation_recorded", "trial_id": trial_id, "date": date, "reused": True})
    def build():
        current_time = utc_now()
        require(instant(slot["outcome_not_before"]) <= instant(current_time) <= instant(slot["outcome_deadline"]), "Outcome outside submission window")
        market = _market(store, artifacts, payload["market_id"], instant(current_time), frozen["decision"]["universe"], date)
        _evidence(payload["evidence_refs"], artifacts, instant(current_time))
        return _outcome_plan(payload, store, artifacts, registration, protocol, current, slot, frozen, market, current_time)
    prepared = _prepared("trial_outcome", payload, store, artifacts, operation_id, build)
    if "deferred_result" in prepared:
        return _package(prepared["deferred_result"], prepared.get("writes", ()), prepared.get("expected", ()))
    return _finish(prepared, store, artifacts, operation_id)

def _settle_due(initial, history, additions, known, close):
    from execution import settlement_events
    def view(at):
        numbered = [{**event, "sequence": initial["sequence"] + len(history) + i + 1} for i, event in enumerate(additions)]
        return ledger.rebuild(initial, history + numbered, known, at)
    due_dates = sorted({row["due_at"] for row in view(close)["receivables"].values()
                        if instant(row["due_at"]) <= instant(close)}, key=instant)
    for due in due_dates:
        additions += settlement_events(view(due), due, known_at=known, recorded_at=known, allow_economic_view=True)

def _outcome_plan(payload, store, artifacts, registration, protocol, current, slot, frozen, market, current_time):
    from execution import simulate_events
    trial_id, date = payload["trial_id"], payload["date"]
    key, document = trial_id + ":" + date, registration["document"]
    declared, missing = _corporate_actions(protocol, frozen, market, date)
    if missing:
        return {"deferred_result": {"status": "insufficient_evidence", "reason": "corporate_action_payment_date_unverified",
                "trial_id": trial_id, "date": date, "missing_payment_dates": missing,
                "observation_committed": False, "actual_account_changed": False}}
    if any(item["income_mode"] == "reinvest" for item in declared.values()):
        return {"deferred_result": {"status": "insufficient_evidence", "reason": "simulated_reinvestment_terms_unverified",
                "trial_id": trial_id, "date": date, "observation_committed": False, "actual_account_changed": False,
                "required_action": "bind_reinvestment_price_fee_and_ownership_rules_before_simulating_the_benchmark"}}
    close, execute_at = slot["valuation_at"], slot["execution_at"]
    execution_date = instant(execute_at).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat()
    violation = store.get("trial_scope_violation", trial_id)
    if violation is not None:
        return {"deferred_result": {"status": "invalid_evidence", "reason": "registered_execution_date_deviation",
                "trial_id": trial_id, "violation": violation, "actual_account_changed": False}}
    opening = (instant(slot["decision_deadline"]) + dt.timedelta(microseconds=1)).isoformat()
    next_state, writes, observed, books, streams = copy.deepcopy(current), [], {}, {}, {}
    expected = [_expect("trial_state", trial_id, current), _expect("trial_slot", key, frozen), _expect("trial_observation", key, None)]
    confirmed = {"events": [], "source_links": {}, "mirrored": []}
    if protocol["observation_mode"] == "user_confirmed_execution":
        source_id = protocol["initial"]["account_id"]
        expected.append(_expect("account", source_id, store.get("account", source_id)))
        confirmed = _confirmed_facts(payload, store, artifacts, protocol, frozen, market, current_time,
                                     opening, close, artifacts.read_json(document["initial_account_ref"]))
        if confirmed.get("scope_violation"):
            violation = confirmed["scope_violation"]
            return {"deferred_result": {"status": "invalid_evidence", "reason": "registered_execution_date_deviation",
                    "trial_id": trial_id, "date": date, "violation": violation,
                    "required_action": "preserve_actual_facts_and_register_a_new_trial_episode",
                    "observation_committed": False, "actual_account_changed": False},
                    "writes": [_write("trial_scope_violation", trial_id, violation)],
                    "expected": expected + [_expect("trial_scope_violation", trial_id, None)]}
        if confirmed.get("missing_execution_nav"):
            return {"deferred_result": {"status": "insufficient_evidence", "reason": "execution_date_nav_unverified",
                    "trial_id": trial_id, "date": date, "missing_execution_nav": confirmed["missing_execution_nav"],
                    "observation_committed": False, "actual_account_changed": False}}
    nav_ref = market["provenance"].get("nav_ref")
    nav = artifacts.read_json(nav_ref) if nav_ref else {}
    execution_prices = {}
    execution_price_dates = {}
    simulation_codes = {order["code"] for role in ("strategy", "benchmark")
                        if role == "benchmark" or protocol["observation_mode"] == "documented_rule_simulation"
                        for order in frozen["decision"]["orders"][role]}
    missing_nav = []
    for code in sorted(simulation_codes):
        rows = [row for row in nav.get(code, []) if row["date"] == execution_date]
        if len(rows) != 1:
            missing_nav.append({"code": code, "date": execution_date})
        else:
            execution_prices[code] = str(rows[0]["nav"])
            execution_price_dates[code] = rows[0]["date"]
    if missing_nav:
        return {"deferred_result": {"status": "insufficient_evidence", "reason": "registered_execution_nav_unverified",
                "trial_id": trial_id, "date": date, "missing_execution_nav": missing_nav,
                "observation_committed": False, "actual_account_changed": False}}
    for role in ("strategy", "benchmark"):
        account_id = _account_id(trial_id, role)
        before = store.get("account", account_id)
        expected.append(_expect("account", account_id, before))
        initial = artifacts.read_json(document["initial_account_ref"])
        history = _history(store, current, trial_id, role)
        additions, closing_mirrors = [], []
        simulated = role == "benchmark" or protocol["observation_mode"] == "documented_rule_simulation"
        links = confirmed["source_links"]
        if role == "benchmark" and protocol["observation_mode"] == "user_confirmed_execution":
            for mirrored in confirmed["mirrored"]:
                mirrored = copy.deepcopy(mirrored)
                if mirrored["type"] == "cashflow":
                    previous = store.get("trial_fact", trial_id + ":benchmark:" + mirrored["id"])
                    if previous:
                        mirrored = store.get("trial_ledger_event", previous["event_key"])["event"]
                    else:
                        book = _projection(initial, history, additions, current_time, mirrored["effective_at"])
                        mirrored["data"]["valuation"] = _flow_valuation(book, mirrored["effective_at"], market, artifacts)
                target = closing_mirrors if mirrored["type"] == "cashflow" and instant(mirrored["effective_at"]) == instant(close) else additions
                target.append(mirrored)
        if simulated and not frozen.get(role + "_simulated", False):
            if protocol["observation_mode"] == "documented_rule_simulation" and Decimal(frozen["decision"]["cashflows"]["open"]) != 0:
                additions.append(_flow_fact(f"{trial_id}:{role}:{date}:flow:open", frozen["decision"]["cashflows"]["open"],
                    opening, current_time, initial, history, additions, market, artifacts))
            for (code, ex_date), distribution in sorted(declared.items()):
                if instant(distribution["record_at"]) < instant(execute_at):
                    additions.append(_dividend_fact(f"{trial_id}:{role}:{ex_date}:{code}", code, distribution,
                                                   initial, history, additions, current_time))
            _settle_due(initial, history, additions, current_time, execute_at)
            numbered = [{**event, "sequence": initial["sequence"] + len(history) + i + 1} for i, event in enumerate(additions)]
            at_execution = ledger.rebuild(initial, history + numbered, current_time, execute_at)
            additions += simulate_events(at_execution, frozen["decision"]["orders"][role], execution_prices, execute_at,
                                         known_at=current_time, recorded_at=current_time, price_dates=execution_price_dates, allow_economic_view=True)
            for (code, ex_date), distribution in sorted(declared.items()):
                if instant(distribution["record_at"]) >= instant(execute_at):
                    additions.append(_dividend_fact(f"{trial_id}:{role}:{ex_date}:{code}", code, distribution,
                                                   initial, history, additions, current_time))
            _settle_due(initial, history, additions, current_time, close)
            additions.append(_fact(f"{trial_id}:{role}:{date}:valuation", "valuation", {
                "prices": market["market_ref"]["prices"], "price_dates": market["market_ref"]["price_dates"]}, close, current_time))
            additions += closing_mirrors
            if protocol["observation_mode"] == "documented_rule_simulation" and Decimal(frozen["decision"]["cashflows"]["close"]) != 0:
                additions.append(_flow_fact(f"{trial_id}:{role}:{date}:flow:close", frozen["decision"]["cashflows"]["close"],
                    close, current_time, initial, history, additions, market, artifacts))
        elif not simulated:
            additions = confirmed["events"]
        else:
            additions += closing_mirrors
        after, stream, count, entries = _ledger_plan(store, artifacts, document, current, trial_id, role, additions, current_time, links)
        writes += entries + [_write("account", account_id, after, False)]
        next_state["ledger_counts"][role] = count
        books[role] = after
        streams[role] = stream
        historical = ledger.rebuild(initial, stream, current_time, close)
        observed[role] = ledger.observe(historical, current_time, market["market_ref"]["prices"], current["last_unit_nav"][role])
    order_ids = {o["order_id"] for group in frozen["decision"]["orders"].values() for o in group}
    pending = any(order_ids & set(book["orders"]) or book["pending_subscriptions"] or book["unknown"] for book in books.values())
    complete = not pending and all(not item["blocked"] and item["performance_exact"] for item in observed.values())
    if protocol["observation_mode"] == "user_confirmed_execution":
        for (code, ex_date), distribution in declared.items():
            entitled = ledger.rebuild(initial, streams["strategy"], current_time, distribution["record_at"])
            shares = sum(Decimal(lot["shares"]) for lot in entitled["lots"].values() if lot["code"] == code
                         and instant(lot["ownership_at"]) <= instant(distribution["record_at"]))
            if shares:
                matching = [fact for fact in streams["strategy"] if fact["type"] == "dividend_declared"
                            and fact["data"]["code"] == code and instant(fact["effective_at"]) == _ex_time(protocol, ex_date)]
                complete = complete and len(matching) == 1
        complete = complete and not any(row["kind"] == "dividend" and instant(row["due_at"]) <= instant(close)
                                        for row in books["strategy"]["receivables"].values())
        flows = {"open": Decimal(0), "close": Decimal(0)}
        for fact in ledger.canonical_cashflows(streams["strategy"], current_time, initial["transfers"]):
            if fact["type"] == "cashflow" and instant(opening) <= instant(fact["effective_at"]) <= instant(close):
                phase = "close" if instant(fact["effective_at"]) == instant(close) else "open"
                flows[phase] += Decimal(fact["data"]["amount"])
        for phase, value in flows.items():
            expected_flow = Decimal(frozen["decision"]["cashflows"][phase])
            require(abs(value) <= abs(expected_flow), "Confirmed cashflow exceeds prospective funding")
        complete = complete and all(flows[p] == Decimal(frozen["decision"]["cashflows"][p]) for p in flows)
    earlier = [r["date"] for r in protocol["schedule"] if r["date"] < date]
    if complete and current["last_completed_date"] != (earlier[-1] if earlier else protocol["initial"]["date"]):
        complete = False
    observation = None
    if complete:
        observation = {"date": date, "request_hash": fingerprint(payload), "market_id": payload["market_id"], "market_hash": fingerprint(market), "known_as_of": current_time, "ledger_counts": copy.deepcopy(next_state["ledger_counts"]), "strategy_unit_nav": observed["strategy"]["unit_nav"], "benchmark_unit_nav": observed["benchmark"]["unit_nav"], "paired_return": float(Decimal(observed["strategy"]["net_return"]) - Decimal(observed["benchmark"]["net_return"]))}
        risk_book = ledger.rebuild(artifacts.read_json(document["initial_account_ref"]),
                                   streams["strategy"], current_time, close)
        observation["principal_risk"] = _principal_observation(protocol, risk_book, streams["strategy"],
                                                              current_time, close, market["market_ref"]["prices"])
        writes.append(_write("trial_observation", key, observation))
        next_state.update(last_completed_date=date, last_unit_nav={role: observed[role]["unit_nav"] for role in observed})
    event = {"kind": "outcome", "trial_id": trial_id, "sequence": current["event_count"] + 1, "previous_hash": current["head_hash"], "registration_hash": current["registration_hash"], "body": {"request": payload, "market_hash": fingerprint(market), "complete": complete, "observation": observation, "ledger_counts": next_state["ledger_counts"], "adopted_source_facts_ref": artifacts.put_json(confirmed["source_links"])}}
    next_state.update(event_count=event["sequence"], head_hash=fingerprint(event))
    updated_slot = {**frozen, "market_id": payload["market_id"], "benchmark_simulated": True, "strategy_simulated": protocol["observation_mode"] == "documented_rule_simulation"}
    writes += [_write("trial_state", trial_id, next_state, False), _write("trial_slot", key, updated_slot, False)]
    return {"document": event, "trust": document["trust"], "deadline": slot["outcome_deadline"], "not_before": slot["outcome_not_before"], "record_kind": "trial_event", "record_key": _event_key(trial_id, event["sequence"]), "writes": writes, "expected": expected, "result": {"status": "observation_recorded" if complete else "awaiting_confirmation", "trial_id": trial_id, "date": date, "actual_account_changed": False}}

def _confirmed_facts(payload, store, artifacts, protocol, frozen, market, known, opening, close, initial):
    """Adopt the complete known source set; caller IDs cannot select favorable facts."""
    hints = payload["strategy_event_ids"]
    require(isinstance(hints, list) and len(hints) == len(set(hints)), "Distinct source identity hints required")
    trial_id, cutoff = payload["trial_id"], instant(known)
    slots = {row["decision"]["date"]: row for key, row in store.scan("trial_slot") if key.startswith(trial_id + ":")}
    orders = {order["order_id"]: (date, order) for date, row in slots.items() for order in row["decision"]["orders"]["strategy"]}
    account = store.get("account", _account_id(trial_id, "strategy"))
    payments = {key: row["order_id"] for key, row in account["pending_subscriptions"].items()}
    pool = {}
    for identity, event in store.scan("ledger_event"):
        if store.get("ledger_event_owner", identity) != {"account_id": protocol["initial"]["account_id"]}:
            continue
        if event["sequence"] <= initial["sequence"] or instant(event["known_at"]) > cutoff or instant(event["recorded_at"]) > cutoff:
            continue
        if instant(event["effective_at"]) > min(cutoff, instant(protocol["end_at"])):
            continue
        kind = event["type"]
        if instant(event["effective_at"]) > instant(close) and kind not in ("cancel_requested", "cancel_confirmed", "settlement", "dividend_paid", "dividend_reinvested", "lot_metadata_confirmed", "unknown", "resolve_unknown"):
            continue
        require(kind != "opening", "The source account was reopened after trial registration")
        if kind == "valuation":
            continue  # Trial valuations are independently bound to audited market records.
        require(instant(event["effective_at"]) >= instant(protocol["initial"]["as_of"]), "A late source correction invalidates the opening reconciliation")
        pool[identity] = event
        if kind == "subscription_pending":
            payments[event["data"]["payment_id"]] = event["data"]["order_id"]
    require(set(hints) <= set(pool), "Source hint is outside the known account/trial scope")
    latest_flows = {row["id"] for row in ledger.canonical_cashflows(list(pool.values()), known, initial["transfers"])}
    result, mirrored, links = [], [], {}
    for identity, event in sorted(pool.items(), key=lambda item: item[1]["sequence"]):
        _evidence(store.get("ledger_event_evidence", identity), artifacts, cutoff)
        kind, data = event["type"], event["data"]
        copied = copy.deepcopy(event)
        copied["id"] = trial_id + ":strategy:source:" + identity
        if kind == "resolve_unknown":
            copied["data"]["unknown_id"] = trial_id + ":strategy:source:" + data["unknown_id"]
            artifacts.read(data["evidence_ref"])
        source_link = {"source_event_id": identity, "source_event_hash": fingerprint(event), "source_account_id": protocol["initial"]["account_id"], "projection_event_id": copied["id"]}
        if kind == "order_reserved":
            order = data["order"]
            require(order["order_id"] in orders and order == orders[order["order_id"]][1], "Source order differs from the prospective order")
            source_link["projection_event_id"] = "reserve:" + order["order_id"]
            links[source_link["projection_event_id"]] = source_link
            continue  # Already reserved exactly once by the committed prospective decision.
        links[copied["id"]] = source_link
        if kind in ("cashflow", "split"):
            mirror = copy.deepcopy(copied)
            mirror["id"] = trial_id + ":benchmark:source:" + identity
            if kind == "cashflow":
                mirror["data"]["transfer_id"] = trial_id + ":benchmark:" + data["transfer_id"]
            mirrored.append(mirror)
            links[mirror["id"]] = {**source_link, "projection_event_id": mirror["id"]}
        seen = store.get("trial_fact", trial_id + ":strategy:" + copied["id"])
        if seen is not None:
            require(seen["source_hash"] == fingerprint({k: v for k, v in copied.items() if k != "sequence"}), "Previously processed source fact changed")
            continue
        if kind in ("buy_fill", "sell_fill", "subscription_confirmed", "subscription_pending", "cancel_requested", "cancel_confirmed"):
            order_id = data.get("order_id", payments.get(data.get("payment_id")))
            require(order_id in orders, "A source execution has no prospective trial order")
            order_date, order = orders[order_id]
            if kind in ("buy_fill", "sell_fill", "subscription_confirmed"):
                require(store.get("trial_observation", trial_id + ":" + order_date) is None, "A new source execution changes a finalized observation")
                decision = store.get("decision", slots[order_date]["decision"]["decision_id"])
                require(decision is not None and fingerprint(decision) == slots[order_date]["decision"]["decision_hash"],
                        "Prospective execution decision lineage differs")
                require(instant(decision["context"]["decision_at"]) < instant(event["effective_at"]) <= instant(close),
                        "Confirmed fill precedes its decision or follows the observation")
                execution_date = instant(event["effective_at"]).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat()
                registered_date = instant(_slot(protocol, order_date)["execution_at"]).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat()
                if execution_date != registered_date or data["price_date"] != registered_date:
                    return {"scope_violation": {"source_event_id": identity, "source_event_hash": fingerprint(event),
                            "source_event_ref": artifacts.put_json(event), "registered_execution_date": registered_date,
                            "actual_execution_date": execution_date, "actual_price_date": data["price_date"], "order_id": order_id,
                            "registered_slot": order_date, "observed_at": known}}
                nav_ref = market["provenance"].get("nav_ref")
                nav = artifacts.read_json(nav_ref) if nav_ref is not None else {}
                rows = [row for row in nav.get(order["code"], []) if row["date"] == execution_date]
                if not rows:
                    return {"missing_execution_nav": [{"source_event_id": identity, "code": order["code"], "date": execution_date}]}
                require(len(rows) == 1 and Decimal(data["price"]) == Decimal(str(rows[0]["nav"])),
                        "Confirmed fill price differs from its economic date source NAV")
        if kind == "cashflow":
            if identity in latest_flows:
                _check_flow_valuation(event, market, artifacts)
            matches = [(date, row) for date, row in slots.items()
                       if instant(_slot(protocol, date)["decision_deadline"]) < instant(event["effective_at"]) <= instant(_slot(protocol, date)["valuation_at"])]
            require(len(matches) == 1, "Source cashflow is outside prospective funding windows")
            flow_date, row = matches[0]
            phase = "close" if instant(event["effective_at"]) == instant(_slot(protocol, flow_date)["valuation_at"]) else "open"
            actual, expected = Decimal(data["amount"]), Decimal(row["decision"]["cashflows"][phase])
            require(actual * expected >= 0 and abs(actual) <= abs(expected), "Cashflow differs from prospective amount")
            require(store.get("trial_observation", trial_id + ":" + flow_date) is None, "A new cashflow changes a finalized observation")
        if kind == "dividend_declared":
            ex_date = instant(event["effective_at"]).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat()
            require(instant(event["effective_at"]) == _ex_time(protocol, ex_date), "Dividend entitlement must enter at the ex-date boundary")
            matches = [d for row in slots.values() for d in row["decision"]["distributions"] if d["code"] == data["code"] and d["ex_date"] == ex_date]
            require(len(matches) == 1 and all(matches[0][k] == data[k] for k in ("per_share", "pay_at", "record_at")), "Source distribution differs from prospective terms")
        result.append(copied)
    valuation = _fact(trial_id + ":strategy:valuation:" + payload["market_id"], "valuation", {
        "prices": market["market_ref"]["prices"], "price_dates": market["market_ref"]["price_dates"]}, close, market["market_ref"]["observed_at"])
    return {"events": [valuation] + result, "source_links": links, "mirrored": mirrored}

def _principal_observation(protocol, book, events, known_at, effective_at, prices):
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        principal = Decimal(protocol["risk_state"]["net_principal"])
        principal += sum((Decimal(event["data"]["amount"]) for event in ledger.canonical_cashflows(events, known_at)
                          if event["type"] == "cashflow" and instant(event["known_at"]) <= instant(known_at)
                          and instant(event["effective_at"]) <= instant(effective_at)), Decimal(0))
        require(principal > 0, "Observed net contributed principal must remain positive")
        snapshot = ledger.snapshot(book, known_at, prices)
        require(not snapshot["blocked"] and snapshot["performance_exact"], "Principal-risk observation needs exact reconciled performance")
        equity = Decimal(snapshot["equity"])
        floor = principal * (1 - Decimal(protocol["risk_state"]["loss_tolerance"]))
        return {"net_principal": ledger.decimal(principal), "equity": ledger.decimal(equity),
                "principal_floor": ledger.decimal(floor), "remaining_loss_budget": ledger.decimal(equity - floor),
                "loss_fraction": ledger.decimal((principal - equity) / principal), "floor_satisfied": equity >= floor}


def _evaluate(payload, store, artifacts):
    fields(payload, {"trial_id"}, label="trial evaluation")
    trial_id = payload["trial_id"]
    trial, protocol, state = _load(trial_id, store, artifacts)
    violation = store.get("trial_scope_violation", trial_id)
    if violation is not None:
        return _package({"status": "invalid_evidence", "trial_id": trial_id,
                         "reason": "registered_execution_date_deviation", "violation": violation,
                         "qualified_for_registered_scope": False, "live_prediction_allowed": False,
                         "future_profit_guaranteed": False, "actual_account_changed": False})
    previous, bound_observations = state["registration_hash"], {}
    for sequence in range(1, state["event_count"] + 1):
        event = store.get("trial_event", _event_key(trial_id, sequence))
        require(event is not None, "Missing committed trial event")
        document = event["document"]
        require(document["sequence"] == sequence and document["previous_hash"] == previous and document["registration_hash"] == state["registration_hash"], "Trial chain differs")
        date = document["body"]["date"] if document["kind"] == "decision" else document["body"]["request"]["date"]
        slot = _slot(protocol, date)
        deadline = slot["decision_deadline"] if document["kind"] == "decision" else slot["outcome_deadline"]
        receipt = _verify_stamp(document, event["timestamp"], trial["document"]["trust"], artifacts, deadline)
        if document["kind"] == "outcome":
            require(instant(receipt["gen_time"]) - dt.timedelta(seconds=receipt["accuracy_seconds"]) >= instant(slot["outcome_not_before"]), "Premature signed outcome")
            if document["body"]["complete"]:
                require(date not in bound_observations, "Repeated finalized observation")
                bound_observations[date] = document["body"]["observation"]
        previous = fingerprint(document)
    require(previous == state["head_hash"], "Committed trial head differs")
    scope = {"trial_id": trial_id, "actual_account_changed": False, "qualified_for_registered_scope": False, "live_prediction_allowed": False, "future_profit_guaranteed": False, "observation_mode": protocol["observation_mode"], "assumption_scope": protocol["assumption_scope"], "execution_authenticity": "source_artifact_consistency_not_authenticated_broker_connection", "evidence_scope": "committed_chain_consistency_not_proof_of_no_hidden_trials"}
    if now() < instant(protocol["end_at"]):
        return _package({**scope, "status": "awaiting_observations", "formal_evaluation_not_before": protocol["end_at"]})
    observations = [store.get("trial_observation", trial_id + ":" + row["date"]) for row in protocol["schedule"]]
    if any(item is None for item in observations):
        return _package({**scope, "status": "invalid_evidence", "reason": "incomplete_frozen_schedule"})
    for observation in observations:
        require(bound_observations.get(observation["date"]) == observation, "Observation differs from its signed computation")
    _recheck_observations(trial["document"], protocol, observations, store, artifacts)
    risk = {"status": "observed_compliant" if all(item["principal_risk"]["floor_satisfied"] for item in observations)
            and Decimal(protocol["risk_state"]["remaining_loss_budget"]) >= 0 else "observed_breach",
            "basis": "confirmed_net_contributed_principal", "loss_tolerance": protocol["risk_state"]["loss_tolerance"],
            "breach_dates": [item["date"] for item in observations if not item["principal_risk"]["floor_satisfied"]],
            "scope": "initial_and_scheduled_valuation_points_only", "future_loss_probability_verified": False,
            "intraperiod_floor_compliance_verified": False}
    current_profile = store.get("risk_profile", protocol["initial"]["account_id"])
    import risk_profile as profile
    registered_pointer = store.get("risk_profile", _account_id(trial_id, "strategy"))
    require(fingerprint(registered_pointer) == protocol["risk_state"]["profile_hash"], "Registered trial risk pointer changed")
    revisions = profile._revisions(store, current_profile)
    changed_during_trial = any(instant(row["recorded_at"]) < instant(protocol["end_at"])
                               for row in revisions[registered_pointer["revision_count"]:])
    if changed_during_trial:
        return _package({**scope, "status": "invalid_evidence", "reason": "registered_risk_profile_changed",
                         "risk": risk, "required_action": "retain_this_episode_and_register_new_profile_episode"})
    result_key = trial_id + ":" + state["head_hash"]
    saved = store.get("trial_evaluation", result_key)
    if saved is not None:
        return _package(artifacts.read_json(saved["result_ref"]))
    calibration = artifacts.read_json(trial["document"]["calibration_ref"])
    if calibration["status"] != "qualified":
        return _package({**scope, "status": "insufficient_evidence", "reason": "design_calibration_not_qualified", "risk": risk})
    from allocation_statistics import evaluate_advantage
    dates = [item["date"] for item in observations]
    adjusted_policy = _formal_inference_policy(protocol)
    advantage = evaluate_advantage(dates, [item["paired_return"] for item in observations], adjusted_policy)
    status = "observed_risk_breach" if risk["status"] == "observed_breach" else advantage["status"]
    result = {**scope, "status": status, "advantage": advantage, "risk": risk,
              "return_advantage_supported": advantage["status"] == "supported",
              "qualified_for_registered_scope": False, "observations": len(observations),
              "calibration_scope": calibration["kind"], "evidence_head": state["head_hash"],
              "comparison_family": trial["document"]["comparison_family"],
              "formal_inference_policy": adjusted_policy,
              "comparison_scope": "one_preregistered_primary_benchmark_comparison_at_the_fixed_endpoint"}
    return _package(result, [_write("trial_evaluation", result_key, {"result_ref": artifacts.put_json(result)}), _write("index", "prospective_trial", {"trial_id": trial_id, "status": status}, False)], [_expect("trial_state", trial_id, state), _expect("trial_evaluation", result_key, None)])

def _recheck_observations(registration, protocol, observations, store, artifacts):
    previous = dict.fromkeys(("strategy", "benchmark"), registration["initial_unit_nav"])
    initial = artifacts.read_json(registration["initial_account_ref"])
    scheduled_orders = {}
    markets = {}
    for row in observations:
        frozen = store.get("trial_slot", protocol["trial_id"] + ":" + row["date"])
        require(frozen is not None, "Missing registered execution slot")
        market = _market(store, artifacts, row["market_id"], instant(row["known_as_of"]), frozen["decision"]["universe"], row["date"])
        require(fingerprint(market) == row["market_hash"], "Observation source lineage changed")
        markets[row["date"]] = market
    for slot in protocol["schedule"]:
        frozen = store.get("trial_slot", protocol["trial_id"] + ":" + slot["date"])
        require(frozen is not None, "Missing registered execution slot")
        for role, orders in frozen["decision"]["orders"].items():
            for order in orders:
                require((role, order["order_id"]) not in scheduled_orders, "Repeated registered order identity")
                scheduled_orders[role, order["order_id"]] = (slot["execution_at"], order["code"], slot["date"])
    if protocol["observation_mode"] == "user_confirmed_execution":
        _audit_source_inventory(protocol, observations, initial, store, artifacts)
    for row in observations:
        slot = _slot(protocol, row["date"])
        frozen = store.get("trial_slot", protocol["trial_id"] + ":" + row["date"])
        market = markets[row["date"]]
        require(fingerprint(market) == row["market_hash"], "Observation source lineage changed")
        frozen = store.get("trial_slot", protocol["trial_id"] + ":" + row["date"])
        _, missing = _corporate_actions(protocol, frozen, market, row["date"])
        require(not missing, "Observation lacks verified company-action payment dates")
        returns = {}
        for role in ("strategy", "benchmark"):
            events = _history(store, row, protocol["trial_id"], role)
            for flow in ledger.canonical_cashflows(events, row["known_as_of"], initial["transfers"]):
                flow_slots = [item for item in protocol["schedule"]
                              if instant(item["decision_deadline"]) < instant(flow["effective_at"]) <= instant(item["valuation_at"])]
                require(len(flow_slots) == 1, "Cashflow is outside a registered funding window")
                _check_flow_valuation(flow, markets[flow_slots[0]["date"]], artifacts)
            for event in events:
                if event["type"] not in ("buy_fill", "sell_fill") or not event["id"].startswith("sim:"):
                    continue
                target = scheduled_orders.get((role, event["data"]["order_id"]))
                require(target is not None and instant(event["effective_at"]) == instant(target[0]),
                        "Simulated execution time differs from the preregistered common clock")
                economic_date = instant(target[0]).astimezone(ZoneInfo(protocol["timezone"])).date().isoformat()
                nav = artifacts.read_json(markets[target[2]]["provenance"]["nav_ref"])
                prices = [value["nav"] for value in nav.get(target[1], []) if value["date"] == economic_date]
                require(len(prices) == 1 and Decimal(event["data"]["price"]) == Decimal(str(prices[0])),
                        "Simulated fill differs from the independent economic-date NAV")
            book = ledger.rebuild(initial, events, row["known_as_of"], slot["valuation_at"])
            result = ledger.observe(book, row["known_as_of"], market["market_ref"]["prices"], previous[role])
            require(not result["blocked"] and result["performance_exact"] and result["unit_nav"] == row[role + "_unit_nav"], "Recomputed unit NAV differs")
            if role == "strategy":
                require(_principal_observation(protocol, book, events, row["known_as_of"], slot["valuation_at"],
                                               market["market_ref"]["prices"]) == row["principal_risk"],
                        "Recomputed principal-risk observation differs")
            previous[role], returns[role] = result["unit_nav"], Decimal(result["net_return"])
        require(float(returns["strategy"] - returns["benchmark"]) == row["paired_return"], "Recomputed paired return differs")

def _audit_source_inventory(protocol, observations, initial, store, artifacts):
    """A late source fact cannot disappear behind a previously finalized sample."""
    trial_id, cutoff = protocol["trial_id"], now()
    last_close = instant(_slot(protocol, observations[-1]["date"])["valuation_at"])
    slots = [row for key, row in store.scan("trial_slot") if key.startswith(trial_id + ":")]
    orders = {o["order_id"]: o for slot in slots for o in slot["decision"]["orders"]["strategy"]}
    for identity, event in store.scan("ledger_event"):
        if store.get("ledger_event_owner", identity) != {"account_id": protocol["initial"]["account_id"]}:
            continue
        if event["sequence"] <= initial["sequence"] or instant(event["known_at"]) > cutoff or instant(event["recorded_at"]) > cutoff:
            continue
        if instant(event["effective_at"]) > last_close or event["type"] == "valuation":
            continue
        _evidence(store.get("ledger_event_evidence", identity), artifacts, cutoff)
        if event["type"] == "order_reserved":
            order = event["data"]["order"]
            require(orders.get(order["order_id"]) == order, "Source order is absent from the prospective trial")
            continue
        fact = store.get("trial_fact", trial_id + ":strategy:" + trial_id + ":strategy:source:" + identity)
        require(fact is not None, "Known source fact is absent from finalized observations")
        entry = store.get("trial_ledger_event", fact["event_key"])
        require(entry is not None and entry.get("source", {}).get("source_event_hash") == fingerprint(event),
                "Finalized source fact lineage differs")

def execute(operation, payload, store, artifacts, operation_id):
    """Pipeline alone atomically publishes the returned business mutations."""
    require(operation in OPERATIONS, "Unsupported trial operation")
    if operation == "trial_register":
        return _register(payload, store, artifacts, operation_id)
    if operation == "trial_decision":
        return _decision(payload, store, artifacts, operation_id)
    if operation == "trial_outcome":
        return _outcome(payload, store, artifacts, operation_id)
    if operation == "trial_evaluate":
        # Capture the source frontier BEFORE any inventory or statistical check.
        # Feedback commits the account and facts together; publication CAS then
        # rejects every late source fact, including one arriving during evaluation.
        fields(payload, {"trial_id"}, label="trial evaluation")
        registered = store.get("trial", payload["trial_id"])
        require(registered is not None, "Unknown trial")
        protocol = artifacts.read_json(registered["document"]["protocol_ref"])
        source_id = protocol["initial"]["account_id"]
        frontier = [_expect(kind, key, store.get(kind, key)) for kind, key in (
            ("account", source_id), ("risk_profile", source_id),
            ("plan_constraints", source_id), ("trial_scope_violation", payload["trial_id"]))]
        result = _evaluate(payload, store, artifacts)
        result.setdefault("expected", []).extend(frontier)
        return result
    fields(payload, {"protocol"}, label="trial calibration request")
    validate_protocol(payload["protocol"])
    return _package({"status": "calibrated", "protocol_hash": fingerprint(payload["protocol"]), "calibration": _calculation(payload["protocol"])})
