"""Read-only, explicitly registered validation of source-bound workflow stages.

These receipts describe the checks actually executed. Partial evidence never
grants trade readiness, and validation never repairs or rolls back account facts.
"""
import copy
import datetime as dt
import hashlib
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from contracts import EvidenceError, fields, fingerprint, instant, parse_request, utc_now
from state_store import LeaseLost
import verify
from registry_context import registry_fingerprint


class ValidationError(EvidenceError):
    def __init__(self, message, required_actions=None):
        super().__init__(message)
        self.required_actions = required_actions or []


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def _need(inputs, *names):
    _require(type(inputs) is dict, "Validation caller bindings must be an object")
    missing = [name for name in names if name not in inputs]
    _require(not missing, "Missing validation caller bindings: " + ", ".join(missing))
    return [inputs[name] for name in names]


def _equal(actual, expected, label):
    _require(actual == expected, label + " differs from independently checked inputs")


def _out(scope, checks, *, partial=False, actions=(), trade_ready=False):
    _require(checks and scope, "Validation needs an explicit scope and executed checks")
    _require(not partial or actions, "Partial validation needs concrete required actions")
    return {"status": "partial" if partial else "passed", "scope": scope, "checks": list(checks),
            "readiness": {"stage_complete": True, "trade_ready": bool(trade_ready and not partial),
                          "investment_effectiveness_proven": False},
            "required_actions": list(actions)}


def _action(action, **details):
    return {"action": action, **details}


def _account_events(store, account_id, sequence=None):
    events = [event for key, event in store.scan("ledger_event")
              if store.get("ledger_event_owner", key) == {"account_id": account_id}
              and (sequence is None or event["sequence"] <= sequence)]
    events.sort(key=lambda event: event["sequence"])
    _equal([event["sequence"] for event in events], list(range(1, len(events)+1)), "Owner-bound ledger prefix")
    return events


def _replay_account(store, artifacts, account_id="main", sequence=None):
    import ledger
    current = store.get("account", account_id)
    _require(current is not None, "Confirmed account is missing")
    _require(account_id == "main", "Trial projections require their sealed trial-history validator")
    events = _account_events(store, account_id, sequence)
    if sequence is not None:
        _equal(len(events), sequence, "Requested ledger prefix length")
    verify.references(events, artifacts)
    for event in events:
        verify.references(store.get("ledger_event_evidence", event["id"]) or [], artifacts)
    at = max((event[key] for event in events for key in ("effective_at", "known_at", "recorded_at")),
             key=instant, default=utc_now())
    rebuilt = ledger.rebuild(ledger.initial_state(current["currency"]), events, at)
    if sequence is None or sequence == current["sequence"]:
        _equal(rebuilt, current, "Stored account versus independent ledger replay")
    return rebuilt


def _account_gaps(account):
    import ledger
    at = max((account[key] for key in ("effective_at", "known_at", "recorded_at") if account[key]),
             key=instant, default=utc_now())
    snapshot = ledger.snapshot(account, at)
    actions = list(snapshot["required_actions"])
    actions += [_action("reconcile_account_exposure", reason=reason) for reason in snapshot["reasons"]]
    if account["performance_pending"]:
        actions.append(_action("obtain_source_bound_flow_time_valuation",
                               pending=account["performance_pending"]))
    return actions


def _code(value, inputs, store, artifacts):
    _equal(value, verify.code_identity(), "Current implementation identity")
    return _out("current_source_identity_only", ["read_current_source_bytes"])


def _clock(value, inputs, store, artifacts):
    before, = _need(inputs, "not_before")
    _require(type(value) is dict and len(value) == 1, "A clock stage must contain one timestamp")
    field = next(iter(value))
    _require(field in {"cutoff_at", "at"}, "Unknown clock field")
    if type(before) in (int, float):
        before = dt.datetime.fromtimestamp(before, dt.timezone.utc)
    else:
        before = instant(before)
    after = inputs.get("not_after")
    after = instant(after if after is not None else utc_now())
    _require(before <= instant(value[field]) <= after, "Stage timestamp is outside authoritative operation bounds")
    return _out("operation_clock_bounds_only", ["parse_timezone", "operation_lower_bound", "authoritative_upper_bound"])


def _feedback(value, inputs, store, artifacts):
    from contracts import validate_feedback_shape
    payload, operation_id = _need(inputs, "payload", "operation_id")
    validate_feedback_shape(payload)
    account_id = payload.get("account_id", "main")
    _equal(value["status"], "recorded", "Feedback result status")
    _equal(value["account_id"], account_id, "Feedback account")
    _require(type(value["sequence"]) is int and value["sequence"] >= 0, "Feedback sequence must be an integer")
    _equal(Counter(value["added"]+value["repeated"]), Counter(row["id"] for row in payload["events"]),
           "Feedback result event inventory")
    for requested in payload["events"]:
        alias = store.get("ledger_event_alias", requested["id"])
        event_id = alias["canonical_event_id"] if alias else requested["id"]
        event = store.get("ledger_event", event_id)
        _require(event is not None and event["sequence"] <= value["sequence"], "Feedback event is not committed")
        _equal(store.get("ledger_event_owner", event_id), {"account_id": account_id}, "Feedback event owner")
        if alias:
            _equal(alias["input_hash"], fingerprint(requested), "Feedback alias original request")
        else:
            _equal({key: item for key, item in event.items() if key != "sequence"}, requested, "Confirmed feedback fact")
    account = _replay_account(store, artifacts, account_id, value["sequence"])
    _equal(account["currency"], payload["currency"], "Feedback currency")
    _equal(fingerprint(account), value["account_hash"], "Feedback account result")
    gaps = _account_gaps(account)
    return _out("confirmed_facts_and_ledger_prefix_not_attempt_novelty_or_trade_authorization",
                ["request_events", "owner_binding", "ledger_replay", "account_snapshot", "added_repeated_inventory_only"],
                partial=bool(gaps), actions=gaps)


def _collection_manifest(value, inputs, store, artifacts):
    import news
    import source_fetch
    payload, operation_id = _need(inputs, "payload", "operation_id")
    manifest = artifacts.read_json(value["manifest_ref"])
    _equal({key: item for key, item in value.items() if key not in {"manifest_ref", "reused"}},
           manifest, "Collection output manifest")
    # Retried producers collect under their persisted attempt namespace so
    # another attempt can genuinely refresh a previously incomplete capture.
    # Bind that namespace to this original news stage; do not accept a prefix.
    namespaces = {operation_id}
    namespaces.update(row["namespace"] for _, row in store.scan("validation_attempt_start")
                      if row.get("operation_id") == operation_id and row.get("stage") == "news")
    _require(manifest["operation_id"] in namespaces, "Collection operation lacks original news attempt provenance")
    _equal(manifest["collection_id"], manifest["operation_id"], "Collection identity")
    persisted = store.get("news-operation", fingerprint({"operation_id": manifest["operation_id"]}))
    _require(persisted is not None, "Collection lacks immutable producer journal")
    _equal(persisted["manifest_ref"], value["manifest_ref"], "Collection original persisted manifest")
    for key in ("cutoff_at", "window_start"):
        _equal(manifest[key], payload[key], "Collection requested " + key)
    run, registry = news.collection_policy(manifest, store, artifacts)
    _equal(manifest["run_id"], payload["run_id"], "Collection authoritative run")
    _equal(manifest["request_hash"], fingerprint({"payload": payload, "registry": registry,
        "news_policy_hash": run["news_policy_hash"], "registry_snapshot_ref": run["registry_snapshot_ref"]}), "Collection frozen request")
    selected = (sorted({item["source_id"] for item in payload["sources"]}) if payload.get("sources") else
                sorted(payload["source_ids"]) if payload.get("source_ids") else sorted(run["news_policy"]["sources"]))
    _equal(manifest["selected_source_ids"], selected, "Collection policy-bound source selection")
    selection_mode = ("automatic_aggregate" if "collection_ids" in payload else
                      "automatic_shard" if payload.get("source_ids") else
                      "explicit" if payload.get("sources") else "automatic")
    _equal(manifest["source_selection_mode"], selection_mode, "Collection source selection mode")
    _equal(manifest["registry_hash"], registry_fingerprint(registry), "Collection source registry")
    verify.references(manifest, artifacts)
    # Collection provenance is independently checked even when incomplete.
    # Current-decision freshness is enforced later by the frozen news policy.
    at = instant(inputs.get("decision_at") or utc_now())
    age = max(0, (at-instant(manifest["cutoff_at"])).total_seconds()) + 1
    if "collection_ids" in payload:
        import news_scope
        news_scope.validate_aggregate(manifest, store, artifacts)
    elif manifest["retrievals"]:
        news._verify_collection_bodies(manifest, registry, at, at, age, artifacts, store=store)
    else:
        _require(not manifest["versions"], "Empty retrieval collection contains article versions")
    news._verify_source_scopes(manifest, registry)
    groups = news._source_groups(payload.get("required_source_groups", {}), registry)
    _equal(manifest["required_source_groups"], groups, "Collection requested source groups")
    _equal(manifest["source_group_coverage"], news._group_coverage(groups, manifest["source_results"]),
           "Collection source-group coverage")
    complete = all(row["advance_cursor"] for row in manifest["source_results"])
    _equal(manifest["status"], "collected" if complete else "partial_collection", "Collection completeness status")
    _require(manifest["fact_verification"] == "not_performed"
             and manifest["coverage_complete_for_source_time_window"] is False,
             "Collection cannot claim verified truth or complete news coverage")
    return manifest


def _news(value, inputs, store, artifacts):
    manifest = _collection_manifest(value, inputs, store, artifacts)
    actions = [_action("complete_news_source_collection", source_id=row["source_id"],
                       pending_urls=row["pending_urls"])
               for row in manifest["source_results"] if not row["advance_cursor"]]
    return _out("captured_news_bytes_parsing_and_declared_scope_not_truth",
                ["original_bytes_reparse", "version_metadata", "source_scope", "caller_window"],
                partial=bool(actions), actions=actions)


def _analysis_run(value, inputs, store, artifacts):
    import source_fetch
    run_id, policy, account_id = _need(inputs, "run_id", "news_policy", "account_id")
    fields(value, {"schema_version", "run_id", "started_at", "publish_cutoff", "account_id", "news_policy",
                   "news_policy_hash", "source_registry_hash", "registry_snapshot_ref", "initial_account_hash", "free_news_only"},
           {"publisher_discovery_id"}, label="analysis run")
    _equal(value, store.get("analysis_run", run_id), "Authoritative immutable analysis run")
    _equal((value["run_id"], value["account_id"], value["news_policy"]), (run_id, account_id, policy), "Run caller policy")
    _require(value["schema_version"] == 4 and value["free_news_only"] is True, "Unsupported analysis run source contract")
    _equal(value["news_policy_hash"], fingerprint(policy), "Run source policy hash")
    import news
    _, registry = news.run_policy(run_id, store, artifacts)
    if value.get("publisher_discovery_id"):
        discovery_id = value["publisher_discovery_id"]
        _equal(registry, validated_publisher_registry(discovery_id, store, artifacts),
               "Run dynamic registry differs from independently verified original directories")
    _equal(value["source_registry_hash"], registry_fingerprint(registry), "Run immutable registered free sources")
    _equal(value["started_at"], value["publish_cutoff"], "Frozen run publication cutoff")
    _require(instant(value["started_at"]) <= instant(utc_now()), "Analysis run starts in the future")
    return _out("immutable_run_source_policy_and_publication_clock", ["committed_run", "policy_hash", "registry_hash", "frozen_clock"])


def _industry_data(value, inputs, store, artifacts):
    import industry_data
    spec, review_id, run_id = _need(inputs, "spec", "news_review_id", "analysis_run_id")
    review = store.get("news-review", review_id)
    _require(review is not None, "Sector data requires a committed news review")
    run = store.get("analysis_run", run_id)
    _require(run is not None, "Sector data requires an authoritative analysis run")
    _analysis_run(run, {"run_id": run_id, "news_policy": spec["news"], "account_id": run["account_id"]}, store, artifacts)
    collection = artifacts.read_json(review["collection_manifest_ref"])
    _require(review.get("run_id") == collection.get("run_id") == run_id
             and collection["cutoff_at"] == run["publish_cutoff"], "Sector source news belongs to another analysis run")
    request = artifacts.read_json(value["request_ref"])
    _equal({key: row for key, row in request["review"].items() if key != "manifest_ref"},
           {key: row for key, row in review.items() if key != "manifest_ref"}, "Sector original review")
    at = inputs.get("decision_at") or value["decision_at"]
    derived = industry_data.validate_sector_data(value, spec, at, artifacts, store=store)
    actions = derived["required_actions"]
    return _out("raw_sector_benchmark_closes_and_finite_source_facts", ["authoritative_run", "source_review", "original_quotes", "source_price_returns"],
                partial=bool(actions), actions=actions)


def _model_data(value, inputs, store, artifacts):
    import industry_binding
    industry_binding.validate_model_data(value, inputs, store, artifacts)
    actions = list(value["industry"]["required_actions"])
    for code in value["code_info"]:
        rows = value["industry_exposures"].get(code, [])
        if not rows:
            actions.append(_action("obtain_source_disclosure_or_PIT_calibrated_asset_style", code=code))
        for row in rows:
            _require(row["basis"] in ("historical_disclosure", "model_estimate"), "Obsolete or unsupported asset exposure basis")
            if row["basis"] == "model_estimate":
                _require(row["actual_exposure_status"] == "unknown" and row["actual_weight"] is None,
                         "A learned coefficient cannot certify current capital exposure")
            else:
                _require(row["actual_exposure_status"] == "historical_disclosed_not_current",
                         "Historical holdings cannot certify current capital exposure")
    return _out("audited_NAV_source_asset_covariates_with_current_capital_unknown",
                ["base_three_fields", "independent_asset_source_reader", "source_taxonomy_reference_weights",
                 "PIT_calibrated_style_EN_KKT", "genuine_capture_clocks", "historical_and_estimated_exposure_not_current_capital"],
                partial=bool(actions), actions=actions)


def _news_review(value, inputs, store, artifacts):
    import news
    spec, decision_at = _need(inputs, "spec", "decision_at")
    decision_at = decision_at or utc_now()
    if value["status"] == "assessed":
        proof = news.validate_review(value, spec, decision_at, artifacts, store=store)
        _require(proof["status"] == "passed", "News review did not pass source verification")
        return _out("source_quotes_events_and_theses_not_truth_or_alpha",
                    ["source_bodies", "quoted_claims", "event_lifecycle", "frozen_news_policy"])
    _equal(value["status"], "awaiting_analysis", "Unfinished review status")
    verify.references(value, artifacts)
    request = artifacts.read_json(value["request_ref"])
    manifest = artifacts.read_json(value["collection_manifest_ref"])
    _equal(value["request_hash"], fingerprint(request), "Unfinished review original request")
    _equal(value["collection_id"], request["collection_id"], "Unfinished review collection")
    run, registry = news.collection_policy(manifest, store, artifacts)
    _equal(spec["news"], run["news_policy"], "Unfinished review authoritative source policy")
    _equal((value["cutoff_at"], value["information_cutoff_at"]),
           (manifest["cutoff_at"], manifest["information_cutoff_at"]), "Unfinished review information binding")
    _require(instant(value["information_cutoff_at"]) <= instant(value["reviewed_at"]) <= instant(decision_at),
             "Unfinished review exceeds its sealed information or decision clock")
    originals = []
    for key, attempt in store.scan("validation_attempt_start"):
        if attempt.get("stage") != "news" or attempt.get("namespace") != manifest["operation_id"]:
            continue
        binding = store.get("validation_binding", attempt["binding_key"])
        _require(binding is not None and binding["stage"] == "news"
                 and binding["operation_id"] == attempt["operation_id"], "Original news input binding is missing")
        frozen = artifacts.read_json(binding["inputs_ref"])
        _equal(fingerprint(frozen), binding["inputs_hash"], "Original news full input binding")
        _equal(frozen["operation_id"], attempt["operation_id"], "Original news operation binding")
        ended = store.get("validation_attempt_end", key)
        _require(ended is not None and ended.get("status") in {"passed", "partial"}
                 and ended.get("value_ref"), "Original collection attempt was not source-accepted")
        captured = artifacts.read_json(ended["value_ref"])
        _equal(captured["manifest_ref"], value["collection_manifest_ref"], "Original accepted collection manifest")
        originals.append({"payload": frozen["payload"], "operation_id": attempt["operation_id"]})
    if not originals:
        journal = store.operation(manifest["operation_id"])
        _require(journal is not None and journal["request"].get("operation") == "news_collect",
                 "Original full collection request binding unavailable")
        originals.append({"payload": journal["request"]["payload"], "operation_id": manifest["operation_id"]})
    _require(len({fingerprint(item) for item in originals}) == 1, "Original collection request binding is ambiguous")
    original = {**manifest, "manifest_ref": value["collection_manifest_ref"], "reused": False}
    _collection_manifest(original, {**originals[0], "decision_at": decision_at}, store, artifacts)
    claims, refs = news._assemble_claims(request, manifest, artifacts, run["news_policy"]["sources"], registry)
    _require(not claims and not value["claims"] and not value["industry_theses"], "Unfinished review hides assessed claims")
    _equal(refs, value["evidence_refs"], "Unfinished review source evidence")
    if "manifest_ref" in value:
        _equal(artifacts.read_json(value["manifest_ref"]), {k: v for k, v in value.items() if k != "manifest_ref"},
               "Unfinished review sealed content")
    return _out("verified_collection_and_unfinished_analysis", ["collection_sources", "empty_assessment_request"],
                partial=True, actions=[_action("complete_news_assessment", collection_id=value["collection_id"])])


def _discovery(value, inputs, store, artifacts):
    import news
    import fund_universe
    review, spec, at, held, monitoring, plan = _need(
        inputs, "review", "spec", "decision_at", "held_codes", "monitoring_codes", "plan_constraints")
    at = at or utc_now()
    state = news.validate_review(review, spec, at, artifacts, store=store)
    if "news_state" in inputs:
        _equal(inputs["news_state"], state, "Discovery news-state binding")
    fund_universe.validate_discovery(value, review, held, artifacts, at, monitoring_codes=monitoring,
                                    news_state=state, plan_constraints=plan)
    actions = value["required_actions"]
    partial = bool(actions) or value["status"] not in {"ready", "observation_only"}
    if partial and not actions:
        actions = [_action("complete_fund_discovery", discovery_status=value["status"])]
    return _out("source_identity_coverage_and_declared_admission_not_market_optimality",
                ["news_lifecycle", "original_catalog_and_profiles", "coverage_reconstruction", "confirmed_plan"],
                partial=partial, actions=actions)


def _research(value, inputs, store, artifacts):
    import research
    from file_io import read_object
    payload, identity, operation_id = _need(inputs, "payload", "identity_snapshot_ref", "operation_id")
    if inputs.get("source_scope_ref") is not None:
        import research_scope
        scope = artifacts.read_json(inputs["source_scope_ref"])
        original = artifacts.read_json(scope["discovery_ref"])
        research_scope.validate(scope, original, artifacts)
        if not inputs.get("historical"):
            current, current_account = research_scope.current_discovery(inputs["discovery_id"], store, artifacts)
            _equal(current, original, "Current original research discovery")
            _equal(scope["account_hash"], fingerprint(current_account) if current_account is not None else None,
                   "Research stage complete current held-risk scope")
        _equal(scope["identity_snapshot_ref"], identity, "Research scope original identity")
        _equal(payload["codes"], scope["selected_codes"], "Research source-selected code scope")
        _require(not scope["blocking_held_codes"], "Research cannot delete unknown held risk")
    run = research.resolve_run(store.base, value["run_path"])
    audit = research.verify_selected(store.root, store.base, run)
    _equal(value, research._reference(run, store.base, audit, value["reused"]), "Research published reference")
    request = read_object(run/"request.json")
    _equal(request["operation_id"], operation_id, "Research operation binding")
    _equal(request["identity_snapshot_ref"], identity, "Research discovery identity")
    _equal(request.get("allow_source_gaps", False), "source_scope_ref" in inputs,
           "Empty NAV acceptance requires the source-only scope")
    for key in ("codes", "start", "end", "horizons", "lookback"):
        _equal(request[key], sorted(payload[key]) if key in {"codes", "horizons"} else payload[key],
               "Research caller " + key)
    readiness = audit["readiness"]
    partial = readiness["market_research"] != "passed"
    actions = readiness.get("required_actions", [])
    if partial and not actions:
        actions = [_action("complete_research_evidence", readiness=readiness)]
    return _out("independent_raw_research_audit_and_caller_binding_not_model_effectiveness",
                ["owned_run", "raw_research_audit", "identity_snapshot", "request_dates_and_codes"],
                partial=partial, actions=actions)


def _market(value, inputs, store, artifacts):
    prepared, discovery_ref = _need(inputs, "prepared", "discovery_ref")
    record = value["record"]
    record_ref = value.get("record_ref") or inputs.get("market_record_ref")
    _require(record_ref is not None, "Market record reference is missing")
    at = inputs.get("decision_at") or record["market_ref"]["observed_at"]
    _equal(artifacts.read_json(record_ref), record, "Market record artifact")
    _equal(value["market_id"], fingerprint(record), "Market semantic identity")
    _equal(record["provenance"]["research_ref"], prepared["run_path"], "Market prepared research")
    _equal(store.get("market", value["market_id"]), record, "Persisted source market")
    verify.market_contract_coverage(record, artifacts)
    verify.verify_research_binding({"market_record_ref": record_ref, "data_ref": value["data_ref"],
        "discovery_ref": discovery_ref, "decision_at": at, "market_id": value["market_id"]}, artifacts)
    gaps = record["provenance"]["term_gaps"]
    _equal(value["status"], "valuation_ready_with_term_gaps" if gaps else "market_ready", "Market readiness status")
    actions = [action for gap in gaps for action in gap["required_actions"]]
    return _out("audited_NAV_identity_and_present_contracts_with_explicit_gaps",
                ["research_to_market_reconstruction", "present_contract_source_reextraction", "per_product_gap_coverage"],
                partial=bool(gaps), actions=actions)


def _reconciliation(value, inputs, store, artifacts):
    import ledger
    import account_reconciliation
    account_id, market, operation_id = _need(inputs, "account_id", "market_record", "operation_id")
    verify.references(market, artifacts)
    if account_id != "main":
        _equal(value, {"status": "trial_projection_managed_by_trial", "account_changed": False, "required_actions": []},
               "Trial reconciliation ownership")
        _require(store.get("trial_risk_binding", account_id) is not None, "Trial reconciliation lacks registered ownership")
        return _out("trial_projection_owned_by_sealed_trial_workflow", ["trial_owner_binding"])
    receipt = store.get("market_account_reconciliation", operation_id+":"+account_id)
    _require(receipt is not None, "Reconciliation receipt is missing")
    _equal(receipt, {"market_hash": fingerprint(market), "result": value}, "Reconciliation market and result")
    event = store.get("ledger_event", value["valuation_event_id"])
    _require(event is not None and event["type"] == "valuation", "Reconciliation valuation fact is missing")
    _equal(event["data"]["prices"], market["market_ref"]["prices"], "Reconciliation source marks")
    _equal(event["data"]["price_dates"], market["market_ref"]["price_dates"], "Reconciliation source mark dates")
    _equal(event["data"]["reconciliation"], value["required_actions"], "Reconciliation declared actions")
    account = _replay_account(store, artifacts, account_id, event["sequence"])
    _equal(fingerprint(account), value["account_hash"], "Reconciled account projection")
    actions = account_reconciliation.source_actions(market, event["known_at"])
    held = {row["code"] for row in account["lots"].values()}
    for action in actions:
        ex_date = action.get("ex_date", action.get("date"))
        day = instant(event["effective_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if action["code"] in held and ex_date <= day and market["market_ref"]["price_dates"][action["code"]] < ex_date:
            _require(any(row.get("code") == action["code"] and row.get("ex_date") == ex_date
                         and row.get("action") == "obtain_post_ex_date_NAV" for row in value["required_actions"]),
                     "Pre-ex-date valuation omitted its source-supported reconciliation blocker")
    _equal(value["status"], "reconciliation_required" if value["required_actions"] else "reconciled", "Reconciliation readiness")
    gaps = _account_gaps(account)
    return _out("persisted_valuation_source_actions_and_ledger_replay", ["market_dates", "source_action_inventory", "ledger_replay"],
                partial=bool(gaps), actions=gaps)


def _projection_required_actions(account, events, market, at, artifacts):
    """Rebuild every applicable source/ownership gap, without the producer helper."""
    import account_reconciliation
    import ledger
    zone = ZoneInfo("Asia/Shanghai")
    openings = [event for event in events if event["type"] in {"opening", "performance_start"}]
    _require(len(openings) <= 1, "Read-only valuation has conflicting performance openings")
    snapshots = [event for event in events if event["type"] == "account_snapshot_confirmed"]
    snapshot_history = not openings
    if snapshot_history:
        _require(snapshots and any(row.get("reason") == "historical_return_unreconciled"
                                  for row in account["performance_pending"]),
                 "Read-only statement valuation requires its actual pending-history anchor")
        anchor = max(snapshots, key=lambda event: (instant(event["effective_at"]), event["sequence"]))
        verify.references(anchor["data"]["evidence_ref"], artifacts)
    else:
        anchor = openings[0]
    opened = instant(anchor["effective_at"])
    anchor_day, current_day = opened.astimezone(zone).date().isoformat(), instant(at).astimezone(zone).date().isoformat()
    held = {lot["code"] for lot in account["lots"].values()}
    owned = set(held)
    orders, payments = {}, {}
    declarations = {}
    for event in events:
        data, kind = event["data"], event["type"]
        if kind == "order_reserved":
            orders[data["order"]["order_id"]] = data["order"]["code"]
        elif kind == "subscription_pending":
            payments[data["payment_id"]] = data["order_id"]
        elif kind in {"opening", "account_snapshot_confirmed"}:
            owned.update(lot["code"] for lot in data["lots"])
        elif kind == "buy_fill" and data["order_id"] in orders:
            owned.add(orders[data["order_id"]])
        elif kind == "subscription_confirmed" and payments.get(data["payment_id"]) in orders:
            owned.add(orders[payments[data["payment_id"]]])
        elif kind == "dividend_reinvested" or kind == "external_fill_confirmed" and data["side"] == "buy":
            owned.add(data["code"])
        elif kind == "dividend_declared":
            day = instant(event["effective_at"]).astimezone(zone).date().isoformat()
            declarations.setdefault((data["code"], day), []).append(event)
    required = []
    for action in account_reconciliation.source_actions(market, at):
        code, day = action["code"], action.get("ex_date", action.get("date"))
        _require(type(day) is str, "Original distribution requires its economic ex-date")
        if day > current_day:
            continue
        base = {"kind": "corporate_action_unverified", "code": code, "ex_date": day}
        if code in held and market["market_ref"]["price_dates"].get(code, "") < day:
            required.append({**base, "action": "obtain_post_ex_date_NAV", "reason":
                "A pre-ex NAV plus dividend receivable double counts income; do not synthesize NAV by subtracting the distribution"})
        if code not in owned or day < anchor_day or day == anchor_day and not snapshot_history:
            continue
        declared = declarations.get((code, day), [])
        if len(declared) != 1:
            required.append({**base, "action": "confirm_record_date_entitled_shares_and_cash_or_reinvestment_election", "reason":
                "No unique confirmed entitlement; fund NAV does not prove account ownership or income election"})
            continue
        event = declared[0]
        if Decimal(event["data"]["per_share"]) != Decimal(str(action["per_share"])):
            required.append({**base, "action": "reconcile_declared_distribution_with_original_source"})
        record_at = event["data"]["record_at"]
        if (action.get("record_date") is None or instant(record_at).astimezone(zone).date().isoformat() != action["record_date"]):
            required.append({**base, "action": "verify_original_distribution_record_date"})
        elif instant(record_at) >= opened:
            book = ledger.rebuild(ledger.initial_state("CNY"), events, at, record_at)
            owned_shares = sum((Decimal(lot["shares"]) for lot in book["lots"].values()
                if lot["code"] == code and instant(lot["ownership_at"]) <= instant(record_at)), Decimal(0))
            if book["unknown"] or book["pending_subscriptions"] or owned_shares != Decimal(event["data"]["entitled_shares"]):
                required.append({**base, "action": "reconcile_record_date_owned_shares_with_confirmed_entitlement"})
    return required


def _valuation_projection(value, inputs, store, artifacts):
    """Independent read-only value/rights reconstruction of the first raw account."""
    import ledger
    account_id, market, operation_id, at, account_ref = _need(inputs,
        "account_id", "market_record", "operation_id", "as_of", "account_snapshot_ref")
    _require(account_id == "main", "Only the actual frozen account uses read-only market valuation")
    fields(value, {"mode", "account_id", "status", "account_changed", "account_hash", "market_hash",
                   "as_of", "snapshot", "required_actions", "review"}, label="read-only account valuation")
    _equal(value["mode"], "read_only_market_projection", "Valuation projection mode")
    _equal(value["account_id"], account_id, "Valuation projection owner")
    _equal(value["account_changed"], False, "Read-only valuation must not change financial records")
    operation = store.operation(operation_id)
    _require(operation is not None and instant(operation["created_at"]) <= instant(at) <= instant(utc_now()),
             "Read-only valuation clock is outside its actual operation lifecycle")
    _equal(value["as_of"], at, "Authoritative projection decision clock")
    verify.references([account_ref, market], artifacts)
    _equal(store.get("market", fingerprint(market)), market,
           "Read-only source market versus its canonical immutable record")
    frozen = artifacts.read_json(account_ref)
    account = _replay_account(store, artifacts, account_id)
    _equal(frozen["ledger_state"], account, "First financial snapshot versus current independently replayed account")
    _equal(value["account_hash"], fingerprint(account), "Raw financial account identity")
    _equal(value["market_hash"], fingerprint(market), "Source market identity")
    events = _account_events(store, account_id, account["sequence"])
    required = _projection_required_actions(account, events, market, at, artifacts)
    _equal(value["required_actions"], required, "Complete source distribution and account ownership gaps")
    snapshot = ledger.snapshot(account, at, market["market_ref"]["prices"], market["market_ref"]["price_dates"])
    if required:
        snapshot = copy.deepcopy(snapshot)
        snapshot["reconciliation"].extend(copy.deepcopy(required))
        snapshot["reasons"] = sorted(set(snapshot["reasons"]) | {
            "corporate_action_unverified:" + item["code"] for item in required})
        snapshot["blocked"] = bool(snapshot["reasons"])
        snapshot["equity"] = snapshot["unit_nav"] = None
        snapshot["snapshot_hash"] = fingerprint({key: item for key, item in snapshot.items() if key != "snapshot_hash"})
    _equal(value["snapshot"], snapshot, "Independent source-mark valuation and preserved financial facts")
    review = None
    if snapshot["blocked"]:
        actions = [dict(item) for item in snapshot["reconciliation"]
                   if item["kind"] in {"corporate_action_unverified", "holding_metadata_unverified"}]
        actions += [{"action": "reconcile_account_exposure", "reason": reason}
                    for reason in snapshot["reasons"]
                    if not reason.startswith(("corporate_action_unverified:", "holding_metadata_unverified:"))]
        review = {"status": "awaiting_account", "as_of": at, "account_hash": fingerprint(account),
                  "snapshot_hash": snapshot["snapshot_hash"], "reasons": snapshot["reasons"], "required_actions": actions}
    _equal(value["review"], review, "Read-only source account reconciliation report")
    _equal(value["status"], "reconciliation_required" if review is not None else "reconciled", "Read-only account readiness")
    _require(store.get("ledger_event", "market-valuation:" + fingerprint([operation_id, account_id])) is None,
             "Read-only valuation unexpectedly recorded a financial valuation event")
    _equal(store.get("account", account_id), account, "Financial account after read-only validation")
    actions = review["required_actions"] if review else []
    return _out("read_only_source_value_and_entitlement_projection_not_financial_or_performance_mutation",
                ["raw_account_ledger_replay", "first_snapshot_binding", "original_mark_dates",
                 "complete_entitlement_gap_reconstruction", "source_valuation_snapshot", "no_financial_mutation"],
                partial=review is not None, actions=actions)


def _selection(value, inputs, store, artifacts):
    import fund_screen
    discovery, identities, terms, plan, at, maximum_age = _need(
        inputs, "discovery", "code_info", "terms", "plan_constraints", "decision_at", "max_terms_age_seconds")
    expected = fund_screen.finalize_selection(discovery, identities, terms, plan, at, max_terms_age_seconds=maximum_age)
    _equal(value, expected, "Final source-qualified selection")
    return _out("declared_source_admission_recomputed_not_independent_investment_merit",
                ["plan_and_identity_binding", "verified_contract_admission", "eligibility_and_exclusions"])


def _trade_inputs(value, inputs, store, artifacts):
    import newtrade_guard
    account_id, spec, at, identity = _need(inputs, "account_id", "spec", "decision_at", "decision_id")
    expected = newtrade_guard.read_inputs(store, artifacts, account_id, spec, at, decision_id=identity)
    _equal(value, expected, "Authoritative financial trade inputs")
    return _out("owner_bound_financial_history_and_registered_review_budget", ["financial_fact_replay", "review_frontier"])


def _trade_history(value, inputs, store, artifacts):
    import newtrade_guard
    account_id, spec, at, identity = _need(inputs, "account_id", "spec", "decision_at", "decision_id")
    expected = newtrade_guard.read_inputs(store, artifacts, account_id, spec, at, decision_id=identity)["trade_state"]
    _equal(value, expected, "Owner-bound confirmed trade history")
    return _out("confirmed_history_before_statistical_reservation", ["financial_fact_replay"])


def _fund_quality(value, inputs, store, artifacts):
    import fund_quality
    reference, contracts, at, policy = _need(inputs, "data_ref", "contracts", "decision_at", "policy")
    fund_quality.validate_source_quality(value, reference, contracts, artifacts, at, policy)
    actions = list(value["source_gaps"])
    return _out("source_vintage_quality_features_and_current_evaluation",
                ["original_quality_sources", "causal_feature_availability", "quality_bundle_binding"],
                partial=bool(actions), actions=actions)


def _quality_model_data(value, inputs, store, artifacts):
    import fund_quality
    prepared = artifacts.read_json(inputs["source_bundle_ref"])
    fund_quality.validate_source_quality(prepared, inputs["data_ref"], inputs["contracts"], artifacts,
                                         inputs["decision_at"], inputs.get("policy"))
    expected = fund_quality.bind_quality_features(artifacts.read_json(inputs["model_data_ref"]), prepared)
    _equal(value, expected, "Original-vintage quality model data")
    return _out("source_bound_quality_features_not_current_statistics_backfilled",
                ["independent_source_reprepare", "exact_augmented_data_binding"],
                partial=bool(prepared["source_gaps"]), actions=prepared["source_gaps"])


def _exposure(value, inputs, store, artifacts):
    import exposure_bounds
    contracts, identities, at = _need(inputs, "contracts", "identities", "decision_at")
    checked = exposure_bounds.validate(value, contracts, identities, artifacts, at)
    actions = [action for product in value["products"].values() for action in product["required_actions"]]
    return _out(checked["scope"], checked["checks"], partial=bool(actions), actions=actions)


def _quality_annotation(value, inputs, store, artifacts):
    import fund_screen
    import fund_quality
    selection, quality, admission, bound = _need(inputs, "selection", "quality", "admission", "quality_inputs")
    prepared = artifacts.read_json(bound["source_bundle_ref"])
    fund_quality.validate_source_quality(prepared, bound["data_ref"], bound["contracts"], artifacts,
                                         bound["decision_at"], bound.get("policy"))
    _equal(quality, prepared["current_evaluation"], "Displayed source quality")
    _equal(admission, prepared["admission"], "Source quality admission")
    _equal(value, fund_screen.attach_quality(selection, quality, admission), "Verified source quality admission and annotation")
    return _out("source_quality_admission_and_registered_features_not_extra_alpha",
                ["original_source_admission", "causal_source_bundle", "no_duplicated_forecast_return"])


def _readiness(value, inputs, store, artifacts):
    import candidate_readiness
    checked = candidate_readiness.validate_readiness(value, inputs)
    partial = value["status"] != "qualified_families"
    actions = value["required_actions"] or ([_action("complete_held_and_candidate_joint_evidence")] if partial else [])
    return _out("source_PIT_individual_and_joint_qualification_before_financial_ranking",
        ["source_samples", "held_risk_in_every_family", "chronological_joint_panels", "declared_qualification_budget"],
        partial=partial, actions=actions)


def _family_selection(value, inputs, store, artifacts):
    import pipeline
    results, reference, data = _need(inputs, "family_results", "readiness_ref", "data")
    readiness = artifacts.read_json(reference)
    block = inputs.get("governance_block")
    if block is not None:
        import newtrade_guard
        account, spec, at, identity, actual = _need(inputs, "account_id", "spec", "decision_at", "decision_id", "trade_review_inputs")
        authoritative = newtrade_guard.read_inputs(store, artifacts, account, spec, at, decision_id=identity)
        _equal(actual, authoritative, "Blocked family governance authority")
        _require(not authoritative["qualification_scope"]["eligible"] and not results,
                 "Governance blockage cannot hide an eligible family result")
        _equal(block["scope"], authoritative["qualification_scope"], "Blocked family policy scope")
        _equal(block["families"], readiness["candidate_families"], "All blocked source families retained")
        _equal(value, pipeline.select_family([], artifacts), "Blocked research has no trading winner")
        return _out("source_families_retained_under_governance_block", ["owner_bound_policy", "full_source_inventory"],
                    partial=True, actions=[_action("declare_active_trade_policy_and_review_budget")])
    _equal(sorted(row["family"]["family_id"] for row in results),
           sorted(row["family_id"] for row in readiness["candidate_families"]), "Every qualified declared family evaluated")
    for result in results:
        context = artifacts.read_json(result["context_ref"])
        calculation = artifacts.read_json(result["calculation_ref"])
        if inputs.get("trade_review_inputs") is not None:
            actual = inputs["trade_review_inputs"]
            _equal(context["spec"], inputs["spec"], "Frozen family policy")
            _equal(context["trade_state"], actual["trade_state"], "Owner-bound family history")
            _equal(context["trade_family_review_index"], actual["trade_family_review_index"], "Reserved family review index")
        _require(set(readiness["required_risk_codes"]) <= set(context["allocation_codes"]), "Family omitted held/pending risk")
        _equal(context["family_id"], result["family"]["family_id"], "Family identity")
        _equal(calculation["input_hash"], fingerprint(data), "Family uses original audited dataset")
        verify.numerical_invariants(context, calculation, data)
    _equal(value, pipeline.select_family(results, artifacts), "Same-principal qualified net wealth family ordering")
    from decision_validation import family_certificate
    family_certificate(results, artifacts, value)
    partial = value["selected_family_id"] is None
    return _out("qualified_family_expected_net_wealth_ranking_not_universal_optimality",
        ["all_declared_families", "same_capital_account_risk", "source_arithmetic", "independent_feasible_and_recovery_ordering"],
        partial=partial, actions=[_action("complete_comparable_qualified_policy_evidence")] if partial else [])


def _context(value, inputs, store, artifacts):
    import strategy
    spec, account, at, market, kwargs = _need(inputs, "spec", "account", "decision_at", "market_ref", "build_kwargs")
    expected = strategy.build_context(spec, account, at, market, **kwargs)
    _equal(value, expected, "Decision context construction")
    verify.execution_sources(market, artifacts, as_of=at)
    partial = value["blocked"]
    actions = value["required_actions"] or [_action("resolve_context_blockers", reasons=value["reasons"])] if partial else []
    return _out("frozen_source_account_constraints_and_context_derivation", ["input_binding", "source_terms", "context_reconstruction"],
                partial=partial, actions=actions)


def _currency_source_runtime(inputs, store, artifacts):
    binding = inputs.get("source_binding")
    return {"artifacts": artifacts, "store": store, "binding_bundle": binding} if binding else None


def _calculation(value, inputs, store, artifacts):
    context, data = _need(inputs, "context", "data")
    _equal(value["context_hash"], context["context_hash"], "Calculation context")
    _equal(value["input_hash"], fingerprint(data), "Calculation dataset")
    trace = value.get("validation_trace", [])
    _require(all(type(row.get("validation")) is dict and row["validation"].get("status") in {"passed", "partial"}
                 and type(row.get("output_hash")) is str and len(row["output_hash"]) == 64 for row in trace),
             "Calculation contains an unrecovered numerical-stage validation failure")
    arithmetic = verify.numerical_invariants(context, value, data)
    if arithmetic["status"] == "not_applicable":
        # A blocked/insufficient producer assertion needs a real frozen-input
        # rerun; an input hash plus a claimed reason is not an evidence check.
        verify.reproduce(context, data, value, source_runtime=_currency_source_runtime(inputs, store, artifacts))
    if value.get("predictive_increment", {}).get("currency_increment", {}).get("paired_rows") and inputs.get("source_binding"):
        from paired_source_audit import recheck_pairs
        recheck_pairs(context, data, value["predictive_increment"], _currency_source_runtime(inputs, store, artifacts))
    partial = value["status"] != "research_ready"
    actions = value.get("required_actions", []) or ([_action("resolve_calculation_readiness", reason=value.get("reason", value["status"]))] if partial else [])
    return _out("independent_financial_and_source_target_arithmetic_not_market_efficacy",
                ["frozen_inputs", "source_target_reconstruction", "cash_shares_risk_and_guard_arithmetic"],
                partial=partial, actions=actions, trade_ready=value.get("orders", {}).get("status") == "ready")


def _reproduction(value, inputs, store, artifacts):
    context, data, calculation = _need(inputs, "context", "data", "calculation")
    _equal(value, verify.reproduce(context, data, calculation, source_runtime=_currency_source_runtime(inputs, store, artifacts)), "Frozen-input reproduction")
    return _out("actual_frozen_input_reexecution_and_independent_arithmetic",
                ["same_inputs_rerun", "calculation_identity", "independent_amount_checks"])


def _report(value, inputs, store, artifacts):
    bundle, = _need(inputs, "bundle")
    _require(type(value) is str, "Report stage requires canonical Markdown text")
    verify.verify_report(bundle, value)
    return _out("canonical_report_matches_bound_bundle_not_future_effectiveness", ["deterministic_report_reconstruction"])

def _saved_result(store, kind, request, value):
    saved = store.get(kind, request["request_id"])
    _require(saved is not None, "Operation-specific committed receipt is missing: " + kind)
    _equal(saved["request_hash"], fingerprint(request["payload"]), "Operation original payload")
    _equal(saved["result"], value, "Operation committed result")
    return saved


def _risk_pointer(store, revision_id):
    import risk_profile
    current, seen = revision_id, set()
    while current is not None:
        _require(current not in seen, "Risk revision chain contains a cycle")
        seen.add(current)
        row = store.get("risk_revision", current)
        _require(row is not None and fingerprint(row) == current, "Risk revision is missing or changed")
        current = row["previous_revision"]
    pointer = {"revision_id": revision_id, "revision_count": len(seen)}
    risk_profile._revisions(store, pointer)
    return pointer


def _status_result(request, value, store, artifacts, bindings):
    import risk_profile
    if bindings.get("historical"):
        return _historical_status(value, store, artifacts, bindings)
    account = store.get("account", "main")
    _equal(value["status"], "ready", "Status operation")
    _equal(value["mode"], "first_investment" if account is None else "existing_account", "Account mode")
    _equal(value["account"], account, "Status account")
    _equal(value["account_hash"], fingerprint(account) if account is not None else None, "Status account identity")
    _equal(value["plan_constraints"], store.get("plan_constraints", "main"), "Status plan")
    _equal(value["latest"], store.get("latest", "decision"), "Status latest decision")
    _equal(value["profile"], risk_profile.status(store), "Status risk preferences")
    if account is not None:
        _replay_account(store, artifacts)
    missing = value["profile"].get("required_inputs", [])
    actions = [_action("confirm_profile_input", **item) for item in missing]
    return _out("current_account_preferences_and_ledger_consistency", ["account_replay", "profile_revisions", "plan_binding"],
                partial=bool(actions), actions=actions)


def _historical_status(value, store, artifacts, bindings):
    import risk_profile
    _require(bindings.get("accepted_snapshot_at"), "Historical snapshot requires original acceptance evidence")
    account = value["account"]
    _equal(value["status"], "ready", "Historical status operation")
    _equal(value["mode"], "first_investment" if account is None else "existing_account", "Historical account mode")
    _equal(value["account_hash"], fingerprint(account) if account is not None else None, "Historical account identity")
    if account is not None:
        _equal(account, _replay_account(store, artifacts, sequence=account["sequence"]), "Historical financial prefix")
    plan = value["plan_constraints"]
    if plan is not None:
        risk_profile.validate_plan(plan)
        _equal(store.get("plan_revision", plan["revision_id"]),
               {key: item for key, item in plan.items() if key != "revision_id"}, "Historical plan revision")
    profile = value["profile"]
    if profile["profile_hash"] is not None:
        pointers = [_risk_pointer(store, key) for key, _ in store.scan("risk_revision")]
        matches = [pointer for pointer in pointers if fingerprint(pointer) == profile["profile_hash"]]
        _require(len(matches) == 1, "Historical risk pointer is missing or ambiguous")
        active, _ = risk_profile._effective(risk_profile._revisions(store, matches[0]), bindings["accepted_snapshot_at"])
        baseline = profile["capital_baseline"]
        _equal(store.get("capital_revision", fingerprint(baseline)), baseline, "Historical capital revision")
        _equal(fingerprint(_replay_account(store, artifacts, sequence=baseline["account_sequence"])),
               baseline["account_hash"], "Historical capital financial prefix")
        if profile["status"] == "confirmed":
            _equal(profile["effective_loss_tolerance"], active["loss_tolerance"], "Historical risk tolerance")
            _equal(profile["plan_constraints"], plan, "Historical profile plan")
    else:
        _equal(profile["status"], "confirmation_required", "Historical missing profile")
    latest = value["latest"]
    if latest is not None:
        verify.references(latest, artifacts)
    actions = [_action("confirm_profile_input", **item) for item in profile.get("required_inputs", [])]
    return _out("historical_snapshot_original_acceptance_and_surviving_financial_revisions",
                ["accepted_original_snapshot", "account_prefix_replay", "risk_capital_plan_revisions",
                 "no_claim_about_current_account_readiness"], partial=bool(actions), actions=actions)


def _profile_result(request, value, store, artifacts, bindings):
    import risk_profile
    operation, payload = request["operation"], request["payload"]
    kind = {"profile_initialize": "profile_initialization", "risk_update": "risk_update",
            "plan_update": "plan_update", "capital_reconcile": "capital_reconcile_result"}[operation]
    _saved_result(store, kind, request, value)
    _equal(value["status"], "confirmed", "Preference confirmation")
    if operation in {"profile_initialize", "risk_update"}:
        pointer = _risk_pointer(store, value["risk_revision"])
        _equal(value["profile_hash"], fingerprint(pointer), "Confirmed risk revision pointer")
        revision = store.get("risk_revision", value["risk_revision"])
        _equal(revision["user_source"], payload["user_source"], "Risk confirmation source")
    if operation in {"profile_initialize", "capital_reconcile"}:
        baseline = value["capital_baseline"]
        _equal(store.get("capital_revision", fingerprint(baseline)), baseline, "Immutable capital baseline")
        _equal(Decimal(baseline["principal"]), Decimal(payload["principal"]), "Confirmed principal")
        _equal(baseline["user_source"], payload["user_source"], "Capital confirmation source")
        _equal(instant(baseline["as_of"]), instant(payload["as_of"]), "Capital confirmation economic cutoff")
        account = _replay_account(store, artifacts, sequence=baseline["account_sequence"])
        _equal(fingerprint(account), baseline["account_hash"], "Capital's account prefix")
        if "account_hash" in value:
            _equal(value["account_hash"], baseline["account_hash"], "Initialization account result")
    elif operation == "plan_update":
        revision = store.get("plan_revision", value["revision_id"])
        _require(revision is not None and fingerprint(revision) == value["revision_id"], "Immutable plan revision differs")
        record = {**revision, "revision_id": value["revision_id"]}
        risk_profile.validate_plan(record)
        _equal(revision["constraints"], payload["constraints"], "Confirmed plan constraints")
        _equal(revision["user_source"], payload["user_source"], "Plan confirmation source")
        _equal(revision["previous_hash"], payload["current_constraints_hash"], "Plan revision predecessor")
        _equal(value["constraints"], payload["constraints"], "Published plan constraints")
        _equal(value["constraints_hash"], fingerprint(record), "Confirmed plan identity")
    return _out("confirmed_preference_revision_and_financial_prefix_only",
                ["operation_receipt", "original_confirmation", "immutable_revisions", "ledger_prefix"])


def _cashflow_result(request, value, store, artifacts, bindings):
    _saved_result(store, "cashflow_result", request, value)
    payload = request["payload"]
    if request["operation"] == "cashflow_prepare":
        draft = store.get("cashflow_draft", value["draft_id"])
        _require(draft is not None, "Transfer draft is missing")
        _equal(draft["request_hash"], fingerprint(payload), "Transfer draft original request")
        _equal(draft["result"], value, "Transfer draft result")
        _equal(draft["draft_hash"], fingerprint({key: item for key, item in draft.items() if key not in {"draft_hash", "result"}}),
               "Transfer draft semantic identity")
        _equal(Decimal(draft["semantic"]["amount"]), Decimal(payload["amount"]), "Draft amount")
        _equal(instant(draft["semantic"]["effective_at"]), instant(payload["effective_at"]), "Draft economic instant")
        verify.references(draft, artifacts)
        return _out("source_bound_transfer_draft_not_confirmed_money", ["draft_original_inputs", "semantic_amount_and_time", "evidence"],
                    partial=True, actions=[_action(value["required_action"], draft_id=value["draft_id"])])
    revision = store.get("transfer_revision", value["revision_id"])
    event = store.get("ledger_event", value["event_id"])
    _require(revision is not None and event is not None, "Confirmed transfer fact is missing")
    _equal(revision["event_id"], event["id"], "Transfer revision event")
    _equal(event["data"]["transfer_id"], value["transfer_id"], "Transfer financial identity")
    _equal(event["data"]["revision_id"], value["revision_id"], "Transfer revision")
    _equal(Decimal(event["data"]["amount"]), Decimal(revision["semantic"]["amount"]), "Confirmed transfer amount")
    _equal(instant(event["effective_at"]), instant(revision["semantic"]["effective_at"]), "Confirmed transfer economic time")
    if request["operation"] == "cashflow_correct":
        _equal(Decimal(event["data"]["amount"]), Decimal(payload["amount"]), "Corrected transfer requested amount")
        _equal(instant(event["effective_at"]), instant(payload["effective_at"]), "Corrected transfer requested time")
    else:
        draft = store.get("cashflow_draft", payload["draft_id"])
        _require(draft is not None, "Confirmed transfer original draft is missing")
        _equal(draft["draft_hash"], payload["draft_hash"], "Confirmed transfer draft binding")
        _equal(Decimal(event["data"]["amount"]), Decimal(draft["semantic"]["amount"]), "Confirmed draft amount")
        _equal(instant(event["effective_at"]), instant(draft["semantic"]["effective_at"]), "Confirmed draft economic time")
    account = _replay_account(store, artifacts, sequence=value.get("sequence", event["sequence"]))
    if value["status"] == "recorded":
        _equal(value["account_hash"], fingerprint(account), "Transfer account result")
        _equal(value["cash"], account["cash"], "Transfer cash")
        _equal(value["performance_pending"], account["performance_pending"], "Transfer valuation gaps")
        _equal(value["performance_exact"], not account["performance_pending"], "Transfer performance readiness")
    else:
        _equal(value["status"], "repeated", "Repeated transfer result")
    verify.references(revision, artifacts)
    actions = _account_gaps(account)
    return _out("confirmed_cash_and_append_only_revision_replayed", ["financial_identity", "economic_fact", "ledger_replay", "valuation_gap_preserved"],
                partial=bool(actions), actions=actions)


def _source_result(request, value, store, artifacts, bindings):
    import source_documents
    import source_fetch
    document = artifacts.read_json(value["document_ref"])
    _equal(document, value["document"], "Captured document output")
    _equal(document["source_id"], request["payload"]["source_id"], "Requested source")
    requested = source_fetch.checked_url(request["payload"]["url"],
        source_fetch.source_rule(request["payload"]["source_id"]))
    _equal(document["capture"]["requested_url"], requested, "Requested capture URL")
    try:
        source_documents.read_extracted_document(value["document_ref"], artifacts)
        partial = False
    except source_documents.DocumentNeedsReview:
        # Its reader already verifies capture and raw-byte identity. Re-run the
        # extraction failure to ensure a caller cannot downgrade valid text.
        try:
            source_documents.extract_document(artifacts.read(document["raw_ref"]), document["media_type"],
                                              document["capture"]["encoding"])
        except source_documents.DocumentNeedsReview as error:
            _equal(document["required_actions"], error.required_actions, "Unreadable source review gap")
            _equal(document["blocks"], [], "Unreadable source body blocks")
            _equal(document["links"], [], "Unreadable source links")
            _equal(document["unreadable_pages"], [], "Unreadable source page metadata")
            _equal(document["extraction_scope"], "not_extracted", "Unreadable source extraction scope")
            _equal(document["text_sha256"], fingerprint([]), "Unreadable source text identity")
            _equal(document["extractor"], {"id": source_documents.EXTRACTOR_ID, "format": "unsupported", "library_version": None},
                   "Unreadable source extractor metadata")
        else:
            raise ValidationError("A readable source was mislabeled as requiring review")
        partial = True
    _equal(value["status"], document["status"], "Document extraction status")
    _equal(value["required_actions"], document["required_actions"], "Document review actions")
    return _out("captured_source_bytes_and_supported_extraction_only", ["capture_provenance", "raw_bytes", "extraction"],
                partial=partial, actions=value["required_actions"])


def _fee_result(request, value, store, artifacts, bindings):
    import fee_contract
    inspection = artifacts.read_json(value["inspection_ref"])
    at = bindings.get("decision_at") or utc_now()
    expected = fee_contract.inspect_contract(request["payload"], artifacts, as_of=at)
    _equal(inspection, expected, "Source fee inspection")
    for key in ("status", "required_actions", "field_statuses"):
        _equal(value[key], inspection[key], "Fee inspection " + key)
    if inspection["contract"] is None:
        _require(value["contract_ref"] is None, "Incomplete fee inspection acquired a contract")
    else:
        _equal(artifacts.read_json(value["contract_ref"]), inspection["contract"], "Inspected fee contract")
        try:
            fee_contract.verify(inspection["contract"], artifacts, as_of=at)
        except fee_contract.EvidenceGap as error:
            _equal(error.required_actions, inspection["required_actions"], "Re-extracted fee source gaps")
    partial = inspection["contract"] is None or bool(inspection["required_actions"])
    actions = inspection["required_actions"] or ([_action("complete_source_bound_dealing_contract")] if partial else [])
    return _out("reextracted_fee_fields_and_explicit_gaps_not_executable_quote",
                ["original_field_reextraction", "contract_and_output_binding"], partial=partial, actions=actions)


def _news_result(request, value, store, artifacts, bindings):
    return _news(value, {"payload": request["payload"], "operation_id": request["request_id"],
                         **bindings}, store, artifacts)


def _publisher_result(request, value, store, artifacts, bindings):
    import publisher_discovery
    _equal(value["discovery_id"], request["request_id"], "Publisher discovery operation identity")
    _equal(value["request"], request["payload"], "Publisher discovery original request")
    _equal(value["request_hash"], fingerprint(request["payload"]), "Publisher discovery request hash")
    registry = publisher_discovery.validate(value, store, artifacts)
    verify.references(value, artifacts)
    _equal(artifacts.read_json(value["source_registry_ref"]), registry, "Publisher reconstructed registry")
    actions = [_action("complete_original_publisher_entrances", publisher_host=row["publisher_host"],
                       status=row["status"]) for row in value["publishers"]
               if row["status"] != "original_entrances_discovered"]
    actions += [_action("retrieve_original_authority_directory", directory_id=row["directory_id"])
                for row in value["directory_captures"] if row["status"] != "captured"]
    return _out("original_authority_directory_and_publisher_entrances_not_global_news_or_investment_qualification",
                ["directory_raw_reparse", "publisher_raw_reparse", "dynamic_registry_reconstruction", "scope_and_budget"],
                partial=bool(actions), actions=actions)


def _news_retrieval(value, inputs, store, artifacts):
    import news_retrieval
    payload, = _need(inputs, "payload")
    _equal(value["retrieval_id"], payload["workflow_id"] + ":retrieval",
           "Source-bound retrieval stage identity")
    proof = news_retrieval.validate(value, store, artifacts, payload=payload, replay_originals=True)
    _require(proof["original_opportunity_scope_preserved"] is True,
             "News retrieval changed its complete original opportunity domain")
    return _out("source_bound_retrieval_planning_not_news_fact_PIT_sector_or_trade_qualification",
                ["completed_public_publisher_binding", "independent_original_source_replay",
                 "complete_original_opportunity_partition", "preregistered_retrieval_and_resource_budget"])


def _retrieval_result(request, value, store, artifacts, bindings):
    import news_retrieval
    _equal(request["request_id"], request["payload"]["workflow_id"] + ":retrieval",
           "News retrieval operation identity")
    _equal(value["retrieval_id"], request["request_id"], "News retrieval result operation identity")
    proof = news_retrieval.validate(value, store, artifacts, payload=request["payload"], replay_originals=False)
    _require(proof["original_opportunity_scope_preserved"] is True,
             "News retrieval changed its complete original opportunity domain")
    return _out("source_bound_retrieval_planning_not_news_fact_PIT_sector_or_trade_qualification",
                ["completed_public_publisher_binding", "canonical_original_registry_and_metadata_partition",
                 "complete_original_opportunity_partition", "preregistered_retrieval_and_resource_budget"])


def _original_materials(value, inputs, store, artifacts):
    import workflow_materials
    payload, = _need(inputs, "payload")
    proof = workflow_materials.validate(value, payload, store, artifacts)
    return _out(proof["scope"], proof["checks"])


def _material_result(request, value, store, artifacts, bindings):
    import workflow_materials
    _equal(request["request_id"], request["payload"]["workflow_id"] + ":materials",
           "Original material operation identity")
    proof = workflow_materials.validate(value, request["payload"], store, artifacts, replay_originals=False)
    return _out(proof["scope"], proof["checks"])


def _method_result(request, value, store, artifacts, bindings):
    from workflow_requests import _prepared
    import automatic_strategy_policy
    prepared = _prepared(store.root, store.plan, request["payload"]["workflow_id"])
    prepared.update(store=store, artifacts=artifacts)
    automatic_strategy_policy.validate_frozen_strategy(value, prepared,
        request["payload"]["market_operation_id"])
    journal = store.get("method-operation", request["request_id"])
    _require(journal is not None, "Automatic method lacks its original producer journal")
    _equal(journal["request_hash"], fingerprint(request["payload"]), "Automatic method original request")
    _equal(artifacts.read_json(journal["result_ref"]), value, "Automatic method original declaration")
    verify.references(value, artifacts)
    actions = value.get("required_actions", [])
    return _out("source_bound_preregistered_method_not_model_fit_or_investment_qualification",
                ["completed_public_market", "original_NAV_account_and_scope", "prewrite_method_reconstruction",
                 "immutable_frontier_and_original_error_budget"],
                partial=value["status"] != "automatic_strategy_policy_prepared", actions=actions)


def _analysis_start_result(request, value, store, artifacts, bindings):
    _equal(value["status"], "analysis_opened", "Analysis opening status")
    run = value["run"]
    _equal(artifacts.read_json(value["run_ref"]), run, "Opened run artifact")
    _equal(run["run_id"], request["payload"].get("run_id", request["request_id"]), "Analysis business identity")
    operation = store.operation(request["request_id"])
    _require(operation is not None, "Opening operation is missing")
    _equal(run["started_at"], operation["created_at"], "Authoritative opening operation clock")
    return _analysis_run(run, {"run_id": run["run_id"], "news_policy": request["payload"]["news_policy"],
                              "account_id": request["payload"].get("account_id", "main")}, store, artifacts)


def _industry_prepare_result(request, value, store, artifacts, bindings):
    _equal(store.get("sector-data-operation", value["operation_id"]), value, "Committed sector producer record")
    published = store.get("industry_data", request["request_id"])
    if published is not None:
        _equal(published, value, "Published business sector record")
    payload = request["payload"]
    return _industry_data(value, {"spec": payload["spec"], "news_review_id": payload["news_review_id"],
                                 "analysis_run_id": payload["analysis_run_id"],
                                 "decision_at": bindings.get("decision_at") or value["decision_at"]}, store, artifacts)


def _review_result(request, value, store, artifacts, bindings):
    import news
    _equal(store.get("news-review", request["request_id"]), value, "Committed news assessment")
    _equal(value["request_hash"], fingerprint(request["payload"]), "Assessment original request")
    manifest = artifacts.read_json(value["collection_manifest_ref"])
    run, _ = news.collection_policy(manifest, store, artifacts)
    at = bindings.get("decision_at") or bindings.get("accepted_snapshot_at") or utc_now()
    spec = bindings.get("spec") or {"kind": "numeric_policy", "news": run["news_policy"]}
    _equal(spec["news"], run["news_policy"], "Assessment authoritative source policy")
    return _news_review(value, {"spec": spec, "decision_at": at}, store, artifacts)


def _discovery_result(request, value, store, artifacts, bindings):
    payload = request["payload"]
    review = store.get("news-review", payload["news_review_id"])
    _require(review is not None, "Discovery news review is not committed")
    review = {**artifacts.read_json(review["manifest_ref"]), "manifest_ref": review["manifest_ref"]}
    account_id = payload.get("account_id", "main")
    frozen = value["request"] if bindings.get("historical") else None
    return _discovery(value, {"review": review, "spec": {"kind": "numeric_policy", "news": payload["news_policy"]},
        "decision_at": bindings.get("decision_at") or bindings.get("accepted_snapshot_at"),
        "held_codes": frozen["held_codes"] if frozen else verify.monitor_codes(store.get("account", account_id)),
        "monitoring_codes": payload.get("monitoring_codes", []),
        "plan_constraints": frozen["plan_constraints"] if frozen else store.get("plan_constraints", account_id)}, store, artifacts)


def _research_result(request, value, store, artifacts, bindings):
    payload = request["payload"]
    discovery = store.get("fund_discovery", payload["discovery_id"])
    _require(discovery is not None, "Research discovery is not committed")
    research_payload = {key: item for key, item in payload.items() if key not in {"discovery_id", "source_scope"}}
    inputs = {"payload": research_payload, "operation_id": request["request_id"],
              "identity_snapshot_ref": discovery["identity_snapshot_ref"]}
    if "source_scope" not in payload:
        research_payload["codes"] = discovery["codes"]
        return _research(value, inputs, store, artifacts)
    import research_scope
    _equal(payload["source_scope"], research_scope.MODE, "Source-qualified research mode")
    reference = bindings.get("source_scope_ref")
    _require(reference is not None, "Source-selected research lacks its original scope binding")
    scope = artifacts.read_json(reference)
    _equal(value.get("source_scope"), scope, "Published source-selected acquisition scope")
    scope_proof = research_scope.validate(scope, discovery, artifacts)
    if not bindings.get("historical"):
        checked_discovery, _ = research_scope.current_discovery(payload["discovery_id"], store, artifacts)
        _equal(checked_discovery, discovery, "Current source discovery qualification")
        current = store.get("account", "main")
        _equal(scope["account_hash"], fingerprint(current) if current is not None else None,
               "Current complete held-risk source scope")
    if value["status"] == "needs_source":
        _require(scope["blocking_held_codes"] or not scope["selected_codes"], "Source task was incorrectly downgraded")
        _require(value["model_fit_executed"] is False and value["trade_ready"] is False,
                 "Unknown held source cannot grant model or trading readiness")
        actions = scope["required_actions"] or [{"action": "complete_original_candidate_identity_source"}]
        _equal(value["required_actions"], actions, "Unready original source actions")
        return _out("held_risk_preserved_and_unverified_original_source_scope", ["original_source_identity", "complete_held_risk"],
                    partial=True, actions=actions)
    research_payload["codes"] = scope["selected_codes"]
    inputs["source_scope_ref"] = reference
    inputs["discovery_id"] = payload["discovery_id"]
    inputs["historical"] = bool(bindings.get("historical"))
    plain = {key: item for key, item in value.items() if key != "source_scope"}
    proof = _research(plain, inputs, store, artifacts)
    if scope_proof["status"] == "partial":
        return _out("audited_qualified_source_subset_original_discovery_and_group_gaps_preserved",
                    [*proof["checks"], "original_identity_reconstruction", "excluded_candidate_and_group_coverage"],
                    partial=True, actions=proof.get("required_actions", [])+scope["required_actions"])
    return proof


def _research_verify_result(request, value, store, artifacts, bindings):
    import research
    expected = research.verify_selected(store.root, store.base, research.resolve_run(store.base, request["payload"]["run_path"]))
    _equal(value, {"status": "passed", "audit": expected}, "Independent research audit result")
    return _out("independent_raw_research_audit", ["owned_run", "raw_data_reconstruction", "reported_audit"])


def _market_result(request, value, store, artifacts, bindings):
    prepared = bindings.get("prepared") or {"run_path": value["record"]["provenance"]["research_ref"]}
    inputs = {**bindings, "prepared": prepared}
    inputs.setdefault("decision_at", value["record"]["market_ref"]["observed_at"])
    return _market(value, inputs, store, artifacts)


def _daily_result(request, value, store, artifacts, bindings):
    bundle = artifacts.read_json(value["bundle_ref"])
    _equal(bundle["decision_id"], request["request_id"], "Decision result request")
    _equal(value["request_id"], request["request_id"], "Public result identity")
    _equal(value["status"], bundle["status"], "Decision result status")
    proof = verify.verify_bundle(bundle, artifacts, store=store)
    _equal(value["verification"], proof, "Published bundle verification")
    markdown = artifacts.read(value["report_ref"]).decode("utf-8")
    _equal(markdown, value["report_markdown"], "Published report bytes")
    _equal(value["report_verification"], verify.verify_report(bundle, markdown), "Published report check")
    actions = list(bundle.get("missing", [])) + list((bundle.get("account_review") or {}).get("required_actions", []))
    actions += [action for gap in bundle.get("term_gaps", []) for action in gap["required_actions"]]
    partial = bundle["status"] not in {"conditional_research", "observation_only", "no_eligible_products"}
    if partial and not actions:
        actions = [_action("resolve_decision_readiness", reason=bundle["reason"])]
    return _out("bound_source_account_decision_and_canonical_report_not_investment_efficacy",
                ["full_bundle_reverification", "report_bytes", "canonical_render"], partial=partial,
                actions=actions, trade_ready=bundle["orders"].get("status") == "ready")


def _replay_result(request, value, store, artifacts, bindings):
    import allocation_runner
    payload = request["payload"]
    initial = bindings.get("initial_account") or store.get("account", payload["initial_account_id"])
    _require(initial is not None, "Replay initial account is missing")
    expected = allocation_runner.replay(payload["spec"], initial, payload["steps"])
    _equal(artifacts.read_json(value["result_ref"]), expected, "Conditional ledger/model replay")
    _equal(value["scope"], "conditional_replay", "Replay qualification")
    return _out("provided_facts_and_frozen_steps_reexecuted_not_historical_market_validation",
                ["initial_account", "event_and_decision_replay"])


def _audit_result(request, value, store, artifacts, bindings):
    proof = store.audit(verify.reference_checker(artifacts))
    _equal(value["status"], proof["status"], "Storage audit status")
    # Validation journals can grow after the producer read. The independent
    # audit checks the actual database; counters are observations, not proof.
    _require(all(type(value[key]) is int and 0 <= value[key] <= proof[key] for key in ("records", "operations", "archives")),
             "Audit inventory counters exceed the independently observed inventory")
    if "decision_id" in request["payload"]:
        identity = request["payload"]["decision_id"]
        bundle = store.get("decision", identity)
        _require(bundle is not None, "Audited decision is missing")
        _equal(value["decision"], verify.verify_bundle(bundle, artifacts, store=store), "Decision audit")
        result = store.operation(identity)["result"]
        _equal(value["report"], verify.verify_report(bundle, artifacts.read(result["report_ref"]).decode("utf-8")), "Report audit")
    return _out("database_integrity_artifact_inventory_and_requested_decision", ["SQLite_integrity", "all_stored_references", "requested_decision"])


def _archive_result(request, value, store, artifacts, bindings):
    store.audit(lambda item: verify.references(item, artifacts))
    if value["status"] == "archived":
        path = (store.base/value["path"]).resolve()
        _require(path.is_relative_to(store.base.resolve()) and path.is_file(), "Archive is outside its owner")
        _equal(path.name, "sealed-"+hashlib.sha256(path.read_bytes()).hexdigest()+".sqlite", "Sealed archive bytes")
        with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as connection:
            _equal(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok", "Archive SQLite integrity")
            for table in ("records", "operations"):
                _equal(connection.execute("SELECT COUNT(*) FROM "+table).fetchone()[0], value[table], "Archived "+table)
    else:
        _equal(value, {"status": "nothing_to_archive"}, "Empty archive result")
    return _out("sealed_archive_integrity_and_retained_business_records", ["all_store_integrity", "archive_bytes_and_inventory"])


def _trial_result(request, value, store, artifacts, bindings):
    import trial
    operation, payload, operation_id = request["operation"], request["payload"], request["request_id"]
    verify.references(value, artifacts)
    if operation == "trial_calibrate":
        trial.validate_protocol(payload["protocol"])
        _equal(value["protocol_hash"], fingerprint(payload["protocol"]), "Trial calibration protocol")
        _equal(value["calibration"], trial._calculation(payload["protocol"]), "Declared-design calibration")
        partial = value["calibration"]["status"] != "qualified"
        return _out("declared_simulation_design_recomputed_not_market_effectiveness",
                    ["protocol", "design_simulation_reexecution"], partial=partial,
                    actions=[_action("revise_or_complete_registered_design_evidence")] if partial else [])
    if operation in {"trial_register", "trial_decision", "trial_outcome"}:
        saved = store.get("trial_preparation", operation_id)
        if saved is None and operation == "trial_outcome" and value.get("reused"):
            trial._load(payload["trial_id"], store, artifacts)
            observation = store.get("trial_observation", payload["trial_id"]+":"+payload["date"])
            _require(observation is not None and observation["request_hash"] == fingerprint(payload), "Repeated trial observation differs")
            return _out("committed_sealed_observation_reference_only", ["registered_protocol", "original_observation_request"])
        _require(saved is not None, "Trial source preparation is missing")
        _equal(saved["input_hash"], fingerprint({"operation": operation, "payload": payload}), "Trial preparation request")
        prepared = artifacts.read_json(saved["artifact"])
        verify.references(prepared, artifacts)
        if "deferred_result" in prepared:
            _equal(value, {"operation_complete": True, **prepared["deferred_result"]}, "Preserved trial evidence gap")
            trial._load(payload["trial_id"], store, artifacts)
            actions = value.get("required_actions") or [_action("resolve_registered_trial_evidence_gap", reason=value["reason"])]
            return _out("registered_trial_and_preserved_source_gap_not_completed_observation",
                        ["registered_source_and_timestamp", "original_request", "preserved_gap"], partial=True, actions=actions)
        external = store.get("external_operation", operation_id)
        _require(external is not None and external.get("stamp") is not None, "Trial timestamp receipt is missing")
        receipt = trial._verify_stamp(prepared["document"], external["stamp"], prepared["trust"], artifacts, prepared["deadline"])
        expected = {"operation_complete": True, **prepared["result"], "timestamp": receipt["gen_time"],
                    "timestamp_revocation_checked": receipt.get("revocation_checked", False)}
        _equal(value, expected, "Source-bound sealed trial result")
        _require(value.get("actual_account_changed") is False, "Trial result cannot claim actual-account mutation")
        if operation == "trial_register":
            protocol = artifacts.read_json(prepared["document"]["protocol_ref"])
            _equal(protocol, payload["protocol"], "Sealed trial protocol")
            trial.validate_protocol(protocol)
        else:
            trial._load(payload["trial_id"], store, artifacts)
        partial = value["status"] == "awaiting_confirmation"
        return _out("original_request_preregistered_protocol_and_verified_timestamp_not_profitability",
                    ["prepared_request", "sealed_document", "timestamp_receipt", "actual_account_separation"],
                    partial=partial, actions=[_action("complete_trial_execution_confirmations")] if partial else [])
    _equal(operation, "trial_evaluate", "Trial evaluation operation")
    registration, protocol, state = trial._load(payload["trial_id"], store, artifacts)
    _require(value.get("qualified_for_registered_scope") is False and value.get("live_prediction_allowed") is False
             and value.get("future_profit_guaranteed") is False, "Trial report cannot promote future investment qualification")
    previous = state["registration_hash"]
    for sequence in range(1, state["event_count"]+1):
        event = store.get("trial_event", trial._event_key(payload["trial_id"], sequence))
        _require(event is not None, "Trial evaluation is missing a committed event")
        document = event["document"]
        _require(document["sequence"] == sequence and document["previous_hash"] == previous
                 and document["registration_hash"] == state["registration_hash"], "Trial evaluation chain differs")
        day = document["body"]["date"] if document["kind"] == "decision" else document["body"]["request"]["date"]
        slot = trial._slot(protocol, day)
        deadline = slot["decision_deadline"] if document["kind"] == "decision" else slot["outcome_deadline"]
        receipt = trial._verify_stamp(document, event["timestamp"], registration["document"]["trust"], artifacts, deadline)
        if document["kind"] == "outcome":
            _require(instant(receipt["gen_time"])-dt.timedelta(seconds=receipt["accuracy_seconds"])
                     >= instant(slot["outcome_not_before"]), "Trial outcome timestamp precedes maturity")
            if document["body"]["complete"]:
                _equal(store.get("trial_observation", payload["trial_id"]+":"+day), document["body"]["observation"],
                       "Observation signed source binding")
        previous = fingerprint(document)
    _equal(previous, state["head_hash"], "Trial evaluation sealed head")
    observations = [store.get("trial_observation", payload["trial_id"]+":"+slot["date"]) for slot in protocol["schedule"]]
    if all(item is not None for item in observations):
        trial._recheck_observations(registration["document"], protocol, observations, store, artifacts)
        if "advantage" in value:
            from allocation_statistics import evaluate_advantage
            expected = evaluate_advantage([row["date"] for row in observations], [row["paired_return"] for row in observations],
                                          trial._formal_inference_policy(protocol))
            _equal(value["advantage"], expected, "Fixed-endpoint paired trial inference")
            _equal(value["return_advantage_supported"], expected["status"] == "supported", "Trial observed advantage")
    else:
        _require(value["status"] in {"awaiting_observations", "invalid_evidence", "insufficient_evidence"},
                 "Incomplete registered schedule acquired a final statistical result")
    partial = value["status"] not in {"supported", "observed_risk_breach"}
    return _out("registered_observation_source_recheck_and_fixed_endpoint_statistics_not_future_efficacy",
                ["sealed_registration", "observation_inventory", "available_observation_recheck", "qualification_limits"],
                partial=partial, actions=[_action("resolve_trial_evaluation_readiness", status=value["status"])] if partial else [])


STAGE_VALIDATORS = {
    "code": _code, "news-window": _clock, "decision-cutoff": _clock,
    "feedback": _feedback, "news": _news, "news-review": _news_review,
    "original_materials": _original_materials,
    "news-retrieval": _news_retrieval,
    "analysis-run": _analysis_run, "industry-data": _industry_data, "model-data": _model_data,
    "discovery": _discovery, "research": _research, "market": _market,
    "account-reconciliation": _reconciliation, "account-valuation-projection": _valuation_projection,
    "final-selection": _selection,
    "trade-history": _trade_history, "trade-inputs": _trade_inputs, "context": _context, "calculation": _calculation,
    "fund-quality": _fund_quality, "quality-model-data": _quality_model_data, "sector-exposure": _exposure,
    "quality-annotation": _quality_annotation, "candidate-readiness": _readiness, "family-selection": _family_selection,
    "reproduction": _reproduction, "report": _report,
}
OPERATION_VALIDATORS = {
    "status": _status_result, "feedback": lambda request, value, store, artifacts, bindings:
        _feedback(value, {"payload": request["payload"], "operation_id": request["request_id"], **bindings}, store, artifacts),
    "profile_initialize": _profile_result, "risk_update": _profile_result,
    "plan_update": _profile_result, "capital_reconcile": _profile_result,
    "cashflow_prepare": _cashflow_result, "cashflow_confirm": _cashflow_result, "cashflow_correct": _cashflow_result,
    "source_capture": _source_result, "fee_inspect": _fee_result, "publisher_discover": _publisher_result,
    "retrieval_prepare": _retrieval_result, "material_prepare": _material_result, "method_prepare": _method_result,
    "news_collect": _news_result, "news_assess": _review_result, "fund_discover": _discovery_result,
    "analysis_start": _analysis_start_result, "industry_prepare": _industry_prepare_result,
    "research_prepare": _research_result, "research_verify": _research_verify_result, "market_prepare": _market_result,
    "daily_review": _daily_result, "allocation_replay": _replay_result, "audit": _audit_result, "archive": _archive_result,
    "trial_calibrate": _trial_result, "trial_register": _trial_result, "trial_decision": _trial_result,
    "trial_outcome": _trial_result, "trial_evaluate": _trial_result,
}
STAGE_NAMES = frozenset(STAGE_VALIDATORS) | {"full-result"}
OPERATION_NAMES = frozenset(OPERATION_VALIDATORS)


def _operation_core(request, result, store, artifacts, bindings, *, reference_check=None):
    """Authenticate a published wrapper without treating its old verdict as a new check."""
    from validation_runtime import validate_result
    automatic = result.get("automatic_validation")
    if automatic is None:
        _require(not bindings.get("historical"), "Historical result lacks automatic source proof; use a new revision")
        return result, bindings
    _require(type(automatic) is dict, "Automatic validation proof must be an object")
    core = {key: item for key, item in result.items() if key != "automatic_validation"}
    _equal(automatic["core_result_hash"], fingerprint(core), "Automatic validation core result")
    (reference_check or verify.reference_checker(artifacts))(automatic)
    manifest = artifacts.read_json(automatic["manifest_ref"])
    final = validate_result(artifacts.read_json(automatic["final_review_ref"]))
    frozen_bindings = artifacts.read_json(automatic["operation_bindings_ref"])
    _equal({key: item for key, item in bindings.items() if key != "historical"}, frozen_bindings,
           "Original operation validation bindings")
    _equal(automatic["status"], final["status"], "Automatic final status")
    _equal(automatic["scope"], final["scope"], "Automatic final scope")
    _equal(automatic["max_stage_attempts"], 3, "Automatic stage retry limit")
    _equal(automatic["max_final_rounds"], 3, "Automatic final retry limit")
    operation_id, round_no = request["request_id"], automatic["round_no"]
    _require(type(round_no) is int and 1 <= round_no <= 3, "Invalid automatic review round")
    _equal(manifest["operation_id"], operation_id, "Manifest operation")
    _equal(manifest["round_no"], round_no, "Manifest final round")
    _require(manifest["code_identity"] == verify.code_identity(), "Historical source changed; use a new revision")
    name = "operation_" + request["operation"]
    evidence = manifest["stages"][name]
    pointer, binding = evidence["accepted"], evidence["binding"]
    key = fingerprint({"operation_id": operation_id, "stage": name}) + ":r" + str(round_no)
    _equal(binding, store.get("validation_binding", key), "Immutable operation validation binding")
    _equal(binding["inputs_hash"], fingerprint({"request": request}), "Original accepted operation request")
    _equal(artifacts.read_json(binding["inputs_ref"]), {"request": request},
           "Original accepted operation input bytes")
    _equal(binding["operation_id"], operation_id, "Operation binding owner")
    _equal(binding["stage"], name, "Operation validation stage")
    _equal(binding["round_no"], round_no, "Operation binding round")
    _equal(binding["code_identity_hash"], fingerprint(manifest["code_identity"]), "Operation source identity")
    _equal(binding["validator"], {"name": "investment:" + name, "version": "automatic-validation-v1"},
           "Trusted operation validator contract")
    _equal(pointer["status"], "accepted", "Original accepted operation")
    for field in ("operation_id", "stage", "round_no", "inputs_hash", "code_identity_hash", "validator"):
        _equal(pointer[field], binding[field], "Accepted operation " + field)
    _equal(pointer["binding_key"], key, "Accepted binding key")
    attempt = pointer["attempt"]
    _require(type(attempt) is int and 1 <= attempt <= 3, "Invalid accepted attempt")
    attempt_key = key + ":a" + str(attempt)
    original_end = store.get("validation_attempt_end", attempt_key)
    _require(original_end is not None, "Original accepted attempt is missing")
    _equal(evidence["attempts"][attempt-1]["end"], original_end, "Immutable accepted outcome")
    _equal(evidence["attempts"][attempt-1]["start"], store.get("validation_attempt_start", attempt_key),
           "Immutable original attempt")
    for field in ("value_ref", "validation_ref"):
        _equal(pointer[field], original_end[field], "Accepted operation " + field)
    original_proof = validate_result(artifacts.read_json(pointer["validation_ref"]))
    _equal(original_end["status"], original_proof["status"], "Original attempt proof")
    _equal(pointer["validation_status"], original_proof["status"], "Accepted operation verdict")
    _equal(artifacts.read_json(pointer["value_ref"]), core, "Original immutable core result")
    completion = store.get("workflow_validation_end", operation_id + ":r" + str(round_no))
    _equal(completion, {"status": "passed", "operation_id": operation_id, "round_no": round_no,
                        **{field: automatic[field] for field in ("final_review_ref", "manifest_ref",
                           "operation_bindings_ref", "core_result_hash")}}, "Immutable final publication proof")
    return core, {**bindings, "accepted_snapshot_at": original_end["finished_at"]}


def _completed_publisher_proof(request, result, core, store, artifacts, reference_check):
    """Reuse source semantics only after authenticating their committed boundary.

    The original proof came from the operation validator, not the discovery
    producer. Every call rereads the referenced CAS bytes; no verdict or byte
    cache survives this boundary. Account/market/order validators do not use it.
    """
    from validation_runtime import validate_result
    receipt = store.operation(request["request_id"])
    _require(receipt is not None and receipt["status"] == "completed",
             "Source validation reuse requires an atomically completed operation")
    _equal(receipt["request"], request, "Completed source operation request")
    _equal(receipt["result"], result, "Completed source operation result")
    _equal(core, store.get("publisher-discovery", request["request_id"]),
           "Completed canonical publisher discovery")
    reference_check(core)
    proof = validate_result(artifacts.read_json(result["automatic_validation"]["final_review_ref"]))
    _require(proof["readiness"]["trade_ready"] is False,
             "Source verification cannot grant trade readiness")
    return {**proof, "checks": [*proof["checks"], "immutable_completed_source_validation",
                                "current_referenced_content_sha256"],
            "validation_basis": "reused_independent_completed_source_verification"}


def _completed_planned_source_proof(request, result, core, store, artifacts, reference_check):
    """Reuse only committed retrieval/material semantics, checking source bytes anew."""
    from validation_runtime import validate_result
    operation = request["operation"]
    _require(operation in {"retrieval_prepare", "material_prepare"},
             "Completed planned-source reuse cannot verify financial or model operations")
    receipt = store.operation(request["request_id"])
    _require(receipt is not None and receipt["status"] == "completed",
             "Source validation reuse requires an atomically completed operation")
    _equal(receipt["request"], request, "Completed source operation request")
    _equal(receipt["result"], result, "Completed source operation result")
    _equal(store.get("operation_result", request["request_id"]), result,
           "Completed canonical source publication")
    payload = request["payload"]
    workflow_id = payload["workflow_id"]
    if operation == "retrieval_prepare":
        _equal(request["request_id"], workflow_id + ":retrieval", "Completed retrieval operation identity")
        _equal(core["retrieval_id"], request["request_id"], "Completed retrieval result identity")
        _equal(core["request"], payload, "Completed retrieval original payload")
        _equal(core["request_hash"], fingerprint(payload), "Completed retrieval original request hash")
        _equal(core["workflow_id"], workflow_id, "Completed retrieval workflow")
        _equal(core["publisher_discovery_id"], payload["publisher_discovery_id"],
               "Completed retrieval publisher identity")
        _equal(core["original_domain_ref"], core["complete_opportunities_ref"],
               "Completed retrieval complete opportunity domain")
        publisher_id = payload["publisher_discovery_id"]
        publisher = store.operation(publisher_id)
        _require(publisher is not None and publisher["status"] == "completed"
                 and publisher["request"]["operation"] == "publisher_discover",
                 "Completed retrieval lost its original completed publisher source")
        publisher_result = publisher["result"]
        automatic = publisher_result.get("automatic_validation")
        _require(type(automatic) is dict, "Completed retrieval publisher lacks independent validation proof")
        bindings = artifacts.read_json(automatic["operation_bindings_ref"])
        publisher_core, _ = _operation_core(publisher["request"], publisher_result, store, artifacts,
            {**bindings, "historical": True}, reference_check=reference_check)
        _completed_publisher_proof(publisher["request"], publisher_result, publisher_core,
                                   store, artifacts, reference_check)
        _equal(core["source_registry_ref"], publisher_core["source_registry_ref"],
               "Completed retrieval original publisher registry")
        _equal(core["source_registry_hash"], fingerprint(artifacts.read_json(publisher_core["source_registry_ref"])),
               "Completed retrieval original publisher registry hash")
    else:
        import workflow_materials
        _equal(request["request_id"], workflow_id + ":materials", "Completed material operation identity")
        _equal(payload["run_id"], workflow_id + ":analysis", "Completed material run identity")
        for field in ("workflow_id", "run_id", "collection_id"):
            _equal(core[field], payload[field], "Completed material " + field)
        index = artifacts.read_json(core["material_index_ref"])
        _equal(index["schema_id"], workflow_materials.INDEX_SCHEMA, "Completed material index schema")
        _equal(index["operation_id"], request["request_id"], "Completed material index operation")
        for field in ("workflow_id", "run_id", "collection_id", "collection_manifest_ref",
                      "query_membership_ref", "total_materials", "total_original_versions", "excluded_count", "scope"):
            _equal(index[field], core[field], "Completed material index " + field)
        membership = artifacts.read_json(core["query_membership_ref"])
        retrieval_id = membership["retrieval_operation_id"]
        _equal(retrieval_id, workflow_id + ":retrieval", "Completed material original retrieval identity")
        retrieval = store.operation(retrieval_id)
        _require(retrieval is not None and retrieval["status"] == "completed"
                 and retrieval["request"]["operation"] == "retrieval_prepare",
                 "Completed material lost its original completed retrieval source")
        retrieval_result = artifacts.read_json(membership["retrieval_result_ref"])
        _equal(retrieval_result, retrieval["result"], "Completed material canonical retrieval CAS")
        automatic = retrieval_result.get("automatic_validation")
        _require(type(automatic) is dict, "Completed material retrieval lacks independent validation proof")
        bindings = artifacts.read_json(automatic["operation_bindings_ref"])
        retrieval_core, _ = _operation_core(retrieval["request"], retrieval_result, store, artifacts,
            {**bindings, "historical": True}, reference_check=reference_check)
        _completed_planned_source_proof(retrieval["request"], retrieval_result, retrieval_core,
                                        store, artifacts, reference_check)
    reference_check(core)
    proof = validate_result(artifacts.read_json(result["automatic_validation"]["final_review_ref"]))
    _require(proof["readiness"]["trade_ready"] is False,
             "Source verification cannot grant trade readiness")
    if operation == "material_prepare":
        _require({"complete_original_metadata_scan", "exact_version_themes",
                  "full_reference_closure_independent_rebuild"} <= set(proof["checks"]),
                 "Completed material lacks original independent query membership verification")
    return {**proof, "checks": [*proof["checks"], "immutable_completed_source_validation",
                                "current_referenced_content_sha256"],
            "validation_basis": "reused_independent_completed_source_verification"}


def accepted_source_reference_checker(material_operation_id, store, artifacts):
    """Authenticate a full source closure before exposing its private SHA traversal.

    Bootstrap uses the candidate only to avoid decoding its source nodes. It
    still checks every current byte and the original immutable accepted value,
    attempt, request, implementation and final commit; no verdict is memoized.
    """
    import source_reference_closure
    receipt = store.operation(material_operation_id)
    _require(receipt is not None and receipt["status"] == "completed"
             and receipt["request"]["operation"] == "material_prepare",
             "A source closure requires its exact atomically completed material operation")
    result = receipt["result"]
    automatic = result.get("automatic_validation")
    _require(type(automatic) is dict and result.get("source_reference_closure_ref") is not None,
             "Completed material lacks its independently accepted source closure")
    inventory = artifacts.read_json(result["source_reference_closure_ref"])
    candidate = source_reference_closure.candidate_checker(inventory, artifacts)
    bindings = artifacts.read_json(automatic["operation_bindings_ref"])
    core, _ = _operation_core(receipt["request"], result, store, artifacts,
        {**bindings, "historical": True}, reference_check=candidate)
    manifest = artifacts.read_json(automatic["manifest_ref"])
    pointer = manifest["stages"]["operation_material_prepare"]["accepted"]
    attempt_proof = artifacts.read_json(pointer["validation_ref"])
    _require("full_reference_closure_independent_rebuild" in attempt_proof["checks"],
             "Accepted material attempt lacks independent source closure reconstruction")
    _completed_planned_source_proof(receipt["request"], result, core, store, artifacts, candidate)
    roots = source_reference_closure.material_roots(receipt["request"]["payload"], core,
        store, artifacts, recorded_roots=inventory["roots"])
    _equal(inventory["roots"], roots, "Accepted source closure exact canonical material forest")
    return candidate


def _completed_material_identity(request, store):
    """Only exact current-workflow source identities can select an accepted forest."""
    operation = request["operation"]
    if operation in {"retrieval_prepare", "material_prepare"}:
        workflow_id = request["payload"]["workflow_id"]
    elif operation == "publisher_discover" and request["request_id"].endswith(":publishers"):
        workflow_id = request["request_id"][:-len(":publishers")]
    else:
        return None
    identity = workflow_id + ":materials"
    receipt = store.operation(identity)
    if (receipt is not None and receipt["status"] == "completed"
            and receipt["request"]["operation"] == "material_prepare"
            and receipt["result"].get("source_reference_closure_ref") is not None):
        return identity
    return None


def validated_source_operation(operation_id, expected, store, artifacts):
    """Authenticate one exact completed source input without replaying its semantics."""
    _require(expected in {"publisher_discover", "retrieval_prepare", "material_prepare"},
             "Source-only receipt consumers cannot bypass financial or model verification")
    receipt = store.operation(operation_id)
    _require(receipt is not None and receipt["status"] == "completed"
             and receipt["request"]["operation"] == expected,
             "Source input requires its exact completed public " + expected)
    result = receipt["result"]
    automatic = result.get("automatic_validation")
    _require(type(automatic) is dict, "Completed source lacks independent validation proof")
    bindings = artifacts.read_json(automatic["operation_bindings_ref"])
    validate_operation_result(receipt["request"], result, store, artifacts, {**bindings, "historical": True})
    return {key: value for key, value in result.items() if key != "automatic_validation"}


def validated_publisher_registry(discovery_id, store, artifacts):
    """Check one completed public source boundary and return its sealed registry.

    This is a consumer API, not a producer shortcut. New discoveries must first
    pass the independent operation validator and atomic publication fence.
    """
    receipt = store.operation(discovery_id)
    _require(receipt is not None and receipt["status"] == "completed"
             and receipt["request"]["operation"] == "publisher_discover",
             "Dynamic registry requires a completed public publisher discovery")
    result = receipt["result"]
    automatic = result.get("automatic_validation")
    _require(type(automatic) is dict, "Completed publisher lacks independent validation proof")
    bindings = artifacts.read_json(automatic["operation_bindings_ref"])
    validate_operation_result(receipt["request"], result, store, artifacts,
                              {**bindings, "historical": True})
    return artifacts.read_json(result["source_registry_ref"])


def validate_operation_result(request, result, store, artifacts, bindings=None, *, reuse_completed=True):
    from registry_context import registry_phase
    try:
        with registry_phase(store, artifacts, inputs={"request": request}):
            return _validate_operation_result(request, result, store, artifacts, bindings,
                                              reuse_completed=reuse_completed)
    except (LeaseLost, ValidationError):
        raise
    except (ValueError, KeyError, TypeError, IndexError, OSError, ArithmeticError) as error:
        raise ValidationError("Operation result validation failed: " + str(error),
                              getattr(error, "required_actions", [])) from error


def _validate_operation_result(request, result, store, artifacts, bindings=None, *, reuse_completed=True):
    try:
        request = parse_request(request)
        operation = request["operation"]
        _require(operation in OPERATION_VALIDATORS, "Unknown operation validation contract: " + operation)
        _require(type(result) is dict, "Operation result must be an object")
        # A traversal is private to this validation boundary. The wrapper and
        # core can reference the same huge source inventory without rereading
        # it twice, while the next process/call rechecks every byte again.
        material_identity = (_completed_material_identity(request, store) if reuse_completed
            and operation in {"publisher_discover", "retrieval_prepare", "material_prepare"}
            and "automatic_validation" in result else None)
        reference_check = (accepted_source_reference_checker(material_identity, store, artifacts)
                           if material_identity is not None else verify.reference_checker(artifacts))
        core, checked_bindings = _operation_core(request, result, store, artifacts, bindings or {},
                                                 reference_check=reference_check)
        if reuse_completed and operation == "publisher_discover" and "automatic_validation" in result:
            proof = _completed_publisher_proof(request, result, core, store, artifacts, reference_check)
        elif reuse_completed and operation in {"retrieval_prepare", "material_prepare"} and "automatic_validation" in result:
            proof = _completed_planned_source_proof(request, result, core, store, artifacts, reference_check)
        else:
            proof = OPERATION_VALIDATORS[operation](request, core, store, artifacts, checked_bindings)
        if checked_bindings.get("historical"):
            proof = {**proof, "scope": "historical_snapshot:" + proof["scope"],
                     "readiness": {**proof["readiness"], "trade_ready": False},
                     "checks": [*proof["checks"], "immutable_acceptance_and_final_review_binding"]}
        return proof
    except LeaseLost:
        raise
    except ValidationError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, OSError, ArithmeticError) as error:
        raise ValidationError("Operation result validation failed: " + str(error),
                              getattr(error, "required_actions", [])) from error


def validate(name, value, inputs, store, artifacts):
    try:
        _require(type(name) is str and name in STAGE_NAMES, "Unknown stage validation contract: " + str(name))
        _require(type(inputs) is dict, "Stage validation inputs must be an object")
        if name == "full-result":
            request, = _need(inputs, "request")
            return validate_operation_result(request, value, store, artifacts, inputs.get("bindings"))
        return STAGE_VALIDATORS[name](value, inputs, store, artifacts)
    except LeaseLost:
        raise
    except ValidationError:
        raise
    except (ValueError, KeyError, TypeError, IndexError, OSError, ArithmeticError) as error:
        raise ValidationError("Stage " + str(name) + " validation failed: " + str(error),
                              getattr(error, "required_actions", [])) from error
