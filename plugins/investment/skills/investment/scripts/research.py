"""Internal operation-addressed NAV research; the orchestrator publishes state."""

import datetime as dt
import hashlib
import gzip
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import research_audit
import source_fetch
import fund_universe
import issuer_nav
from artifacts import Artifacts
from contracts import fingerprint, strict_json_loads
from research_data import BLOCKERS, CN, POLICY_VERSION, ResearchError, ResearchEvidenceGap, bind_source_versions, build_samples, parse_nav, write_tables
from file_io import plan_path, read_object, write_small_json, durable_replace

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_BATCH_SIZE = 50
MAX_COLLECTION_SOURCE_REQUESTS = 256
MAX_COLLECTION_BYTES = 64 * 1024 * 1024
MAX_COLLECTION_SECONDS = 180
MAX_NAV_ARCHIVE_CAPTURES = 1024
EXECUTORS = ("research.py", "research_data.py", "research_audit.py", "file_io.py", "contracts.py",
             "source_fetch.py", "source_registry.json", "fund_universe.py", "artifacts.py", "source_documents.py", "fund_screen.py", "risk_profile.py", "issuer_nav.py")
EXCLUDED = {"manifest.json", "audit.json", "run-status.json"}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def executor_hashes():
    return {name: file_hash(SCRIPT_DIR / name) for name in EXECUTORS}


def within(path, parent):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(parent).resolve()):
        raise ResearchError("Path escapes its owner: " + str(path))
    return path


def owned_path(base, relative):
    relative = Path(relative)
    if relative.is_absolute() or any(part in ("..", ".") for part in relative.parts):
        raise ResearchError("Invalid owned path")
    current = Path(base).resolve()
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.resolve() != current:
            raise ResearchError("Linked paths cannot redirect research: " + str(current))
    return within(current, base)


def inventory(run):
    items = []
    for path in sorted(run.rglob("*")):
        within(path, run)
        if path.is_symlink():
            raise ResearchError("Research artifacts cannot be symbolic links")
        if path.is_file() and path.relative_to(run).as_posix() not in EXCLUDED:
            items.append({"path": path.relative_to(run).as_posix(), "bytes": path.stat().st_size, "sha256": file_hash(path)})
    return sorted(items, key=lambda item: item["path"])


def select_plan(root, requested, create=False):
    root = Path(root).resolve()
    if root.is_relative_to(SCRIPT_DIR.parents[2]):
        raise ResearchError("Research data cannot be written into the plugin installation")
    if not isinstance(requested, str) or not requested:
        raise ResearchError("An explicit plan is required")
    base = plan_path(root, requested)
    if owned_path(root, "plans/" + requested) != base:
        raise ResearchError("Selected plan identity is redirected")
    if create:
        base.mkdir(parents=True, exist_ok=True)
    if not base.is_dir():
        raise ResearchError("Research plan does not exist")
    return root, base, {"plan_id": requested, "data_root": str(root)}


def resolve_run(base, requested):
    if not isinstance(requested, str) or not requested:
        raise ResearchError("An explicit verified research run reference is required")
    parts = Path(requested).parts
    if (len(parts) != 5 or parts[0] != "work" or not re.fullmatch(r"[a-f0-9]{64}", parts[1])
            or not parts[2].isdigit() or int(parts[2]) < 1 or not re.fullmatch(r"[a-f0-9]{32}", parts[3])
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[4])):
        raise ResearchError("Run must be a generation-isolated Store attempt directory")
    path = owned_path(base, requested)
    if not path.is_dir():
        raise ResearchError("Research run does not exist")
    return path


def verify_selected(root, base, run):
    manifest, policy = read_object(run / "manifest.json"), read_object(run / "policy.json")
    if manifest.get("plan_id") != base.name or policy.get("plan_id") != base.name or policy.get("run_id") != run.name:
        raise ResearchError("Run does not belong to the selected plan")
    if Path(policy.get("root", "")).resolve() != Path(root).resolve():
        raise ResearchError("Run does not belong to the selected data root")
    checked = research_audit.verify_run(run, executor_hashes())
    if read_object(run / "audit.json") != checked:
        raise ResearchError("Saved audit differs from independent verification")
    return checked


def _write_source_bytes(path, raw):
    path = Path(path)
    if path.is_symlink():
        raise ResearchError("Download target cannot be a link")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return file_hash(path)


def download(url, path, source_id, timeout, expected=None, *, max_bytes=source_fetch.MAX_BYTES):
    capture = source_fetch.fetch(url, source_fetch.identify_source(url), timeout=timeout, max_bytes=max_bytes)
    actual = _write_source_bytes(path, capture["raw_bytes"])
    return {**{k: v for k, v in capture.items() if k not in ("raw_bytes", "text")},
            "source_id": source_id, "path": None, "sha256": actual, "expected_sha256": expected,
            "review_status": "matched_reviewed_digest" if expected == actual else
                             ("changed_requires_review" if expected else "provider_snapshot")}


def _nav_archive_kind(identity, registry_hash):
    return "research_nav_archive:" + fingerprint({"subject": research_audit.nav_archive_subject(identity),
                                                  "registry_hash": registry_hash})


def _archive_sealed_nav(run, store, context, code_info):
    """Publish bounded immutable byte references only after the run is sealed."""
    store.assert_owned(context)
    sources = {item["source_id"]: item for item in
               strict_json_loads((run / "source-manifest.json").read_text(encoding="utf-8"))}
    artifacts = Artifacts(store.base)
    origin = {"run_path": run.relative_to(store.base).as_posix(),
              "manifest_sha256": file_hash(run / "manifest.json"), "audit_sha256": file_hash(run / "audit.json")}
    with store.transaction() as conn:
        store.assert_owned(context, conn)
        for code, identity in code_info.items():
            capture, action = sources["NAV-" + code], sources["TT-actions-" + code]
            raw = owned_path(run, capture["path"]).read_bytes()
            points = research_audit.json_array(raw.decode(capture["encoding"]), "Data_netWorthTrend")
            days = [dt.datetime.fromtimestamp(int(point["x"])/1000, CN).date().isoformat() for point in points]
            record = {"schema_version": 4, "subject": research_audit.nav_archive_subject(identity),
                      "capture": capture, "action_capture": action, "origin": origin,
                      "coverage": {"start": days[0], "end": days[-1]}}
            key = fingerprint({name: value for name, value in record.items() if name != "origin"})
            kind = _nav_archive_kind(identity, capture["registry_hash"])
            prior = store.get(kind, key, conn=conn)
            if prior is not None:
                artifacts.read(prior["raw_ref"])
                artifacts.read(prior["action_raw_ref"])
                continue
            record["raw_ref"] = artifacts.put_bytes(gzip.compress(raw, mtime=0))
            record["action_raw_ref"] = artifacts.put_bytes(owned_path(run, action["path"]).read_bytes())
            store.put(kind, key, record, conn=conn)


def collect(run, codes, start, end, timeout, *, store, context, code_info):
    store.assert_owned(context)
    codes = sorted(codes)
    started = time.monotonic()
    deadline = started + MAX_COLLECTION_SECONDS
    source_artifacts = Artifacts(store.base)
    scope = fingerprint({"operation_id": context.operation_id, "codes": codes,
        "start": start.isoformat(), "end": end.isoformat(),
        "registry_hash": fingerprint(source_fetch.load_registry()),
        "subjects": {code: {key: code_info[code].get(key) for key in
            ("code", "legal_name", "currency", "share_class", "fund_group_id")} for code in codes}})
    completed_codes = []
    source_requests, charged_bytes = 0, 0

    def progress(status):
        value = {"schema_version": 4, "scope_hash": scope, "codes": codes,
            "batch_size": SOURCE_BATCH_SIZE, "completed_codes": list(completed_codes),
            "pending_codes": sorted(set(codes)-set(completed_codes)), "status": status,
            "source_requests": source_requests, "source_byte_budget_charged": charged_bytes,
            "archive_byte_budget_basis": "lossless_encoded_local_proof_bytes; uncompressed_originals_verified_and_bounded_per_source",
            "max_source_requests": MAX_COLLECTION_SOURCE_REQUESTS,
            "max_total_bytes": MAX_COLLECTION_BYTES, "max_elapsed_seconds": MAX_COLLECTION_SECONDS,
            "elapsed_seconds": time.monotonic()-started,
            "scope": "whole_candidate_domain_source_acquisition_no_batch_winner_selection"}
        store.assert_owned(context)
        write_small_json(run / "collection-progress.json", value)
        store.put("research_collection_progress", scope, value, immutable=False)

    def exhausted():
        progress("partial_collection")
        raise ResearchEvidenceGap("Whole-domain research acquisition exhausted its declared budget",
            [{"action": "complete_whole_domain_source_acquisition", "scope_hash": scope,
              "pending_codes": sorted(set(codes)-set(completed_codes)),
              "acceptance": "resume verified raw captures under the same operation; publish only one complete domain manifest"}])

    progress("collecting")
    source_path = run / "source-manifest.json"
    sources = strict_json_loads(source_path.read_text(encoding="utf-8")) if source_path.exists() else []
    if not isinstance(sources, list) or len({r["source_id"] for r in sources}) != len(sources):
        raise ResearchError("Invalid recovered source inventory")
    indexed, verification = {r["source_id"]: r for r in sources}, {}
    registry = {"schema_version": 4, "sources": {}, "anchors": [], "corrections": []}
    nav_evidence = issuer_nav.inventory(read_object(run / "identity-snapshot.json"), Artifacts(run), codes)
    registry["sources"].update(nav_evidence["sources"])
    registry["anchors"] = nav_evidence["anchors"]
    for record in nav_evidence["source_records"]:
        indexed[record["source_id"]] = record
        verification[record["source_id"]] = "reproduced_current_source"
    write_small_json(run / "issuer-nav-evidence.json", nav_evidence)
    action_inventory = {}

    def fetch(url, relative, sid, expected=None, optional=False):
        nonlocal source_requests, charged_bytes
        if time.monotonic() >= deadline:
            exhausted()
        prior = indexed.get(sid)
        if prior is not None and prior.get("path") is not None:
            source_fetch.validate_capture_provenance(prior)
            normalized = source_fetch.checked_url(url, source_fetch.source_rule(prior["registry_source_id"]))
            if prior["requested_url"] != normalized or prior["expected_sha256"] != expected:
                raise ResearchError("Recovered source identity changed")
            if file_hash(owned_path(run, prior["path"])) != prior["sha256"]:
                raise ResearchError("Recovered source bytes changed")
            return prior
        cache_key = fingerprint({"scope_hash": scope, "source_id": sid, "url": url, "expected_sha256": expected})
        saved = store.get("research_source_capture", cache_key)
        if saved is not None:
            record = dict(saved["capture"])
            source_fetch.validate_capture_provenance(record)
            raw = source_artifacts.read(saved["raw_ref"])
            raw_hash = hashlib.sha256(raw).hexdigest()
            if (saved["scope_hash"] != scope or record["source_id"] != sid or record["requested_url"] != url
                    or record["expected_sha256"] != expected or len(raw) != record["bytes"]
                    or raw_hash != record["raw_sha256"] or raw_hash != record["sha256"]):
                raise ResearchError("Recovered operation source binding or bytes changed")
            store.assert_owned(context)
            if _write_source_bytes(owned_path(run, relative), raw) != record["sha256"]:
                raise ResearchError("Restored source body changed")
            record["path"] = relative
            indexed[sid] = record
            write_small_json(run / "source-manifest.json", list(indexed.values()))
            return record
        if source_requests >= MAX_COLLECTION_SOURCE_REQUESTS or charged_bytes >= MAX_COLLECTION_BYTES:
            exhausted()
        allowance = min(source_fetch.MAX_BYTES, MAX_COLLECTION_BYTES-charged_bytes)
        source_requests += 1
        charged_bytes += allowance
        progress("collecting")
        fetch_status = "collecting"
        try:
            store.assert_owned(context)
            record = download(url, owned_path(run, relative), sid,
                              min(timeout, max(.001, deadline-time.monotonic())), expected, max_bytes=allowance)
            store.assert_owned(context)
            if not 0 < record["bytes"] <= allowance:
                raise ResearchError("Source body exceeded its reserved acquisition allowance")
            charged_bytes -= allowance-record["bytes"]
            record["path"] = relative
            indexed[sid] = record
            cache_record = {key: value for key, value in record.items() if key != "path"}
            raw = owned_path(run, relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != record["raw_sha256"] or len(raw) != record["bytes"]:
                raise ResearchError("Captured source differs from its downloaded bytes")
            store.put("research_source_capture", cache_key, {"scope_hash": scope, "capture": cache_record,
                "raw_ref": source_artifacts.put_bytes(raw)})
            return record
        except (OSError, ValueError) as exc:
            indexed[sid] = {"source_id": sid, "requested_url": url, "retrieved_at": dt.datetime.now(CN).isoformat(),
                            "expected_sha256": expected, "review_status": "unavailable", "error": str(exc)}
            if not optional:
                fetch_status = "partial_collection"
                raise
            return None
        finally:
            write_small_json(run / "source-manifest.json", list(indexed.values()))
            progress(fetch_status)

    rows_by_code, summaries = {}, []
    # I/O chunks share one frozen financial domain and one eventual manifest.
    batches = (codes[index:index+SOURCE_BATCH_SIZE] for index in range(0, len(codes), SOURCE_BATCH_SIZE))
    for code in (code for batch in batches for code in batch):
        sid = "TT-actions-" + code
        action_url = "https://fundf10.eastmoney.com/fhsp_" + code + ".html"
        action_source = fetch(action_url, "raw/actions/" + code + ".html", sid)
        actions = fund_universe.parse_actions((run / action_source["path"]).read_text(encoding=action_source["encoding"]), code)
        for item in actions["dividends"]:
            item["currency"] = code_info[code]["currency"]
            item["currency_basis"] = "same_share_source_identity"
        action_inventory[code] = {**actions, "known_at": action_source["retrieved_at"],
                                  "source_id": sid, "source_sha256": action_source["sha256"],
                                  "source_path": action_source["path"], "source_url": action_url,
                                  "coverage": "all_rows_in_current_distributor_table_not_all_future_announcements",
                                  "rights_terms_status": "issuer_entitlement_rules_not_in_distributor_table"}
        in_scope_splits = [item for item in actions["splits"] if start.isoformat() <= item["date"] <= end.isoformat()]
        if in_scope_splits:
            raise ResearchEvidenceGap("Retrieve original issuer split/conversion disclosure and verify share ratios before preparing " + code,
                                      [{"code": code, "action": "verify_split_and_implement_share_factors", "events": in_scope_splits,
                                        "required_output": "original issuer disclosure, effective dates and share conversion factors",
                                        "acceptance": "all return, execution and audit paths apply the verified share factors"}])
        registry["sources"][sid] = {"url": action_url, "expected_sha256": action_source["sha256"],
                                    "kind": "html", "code": code, "scope": actions["scope"]}
        verification[sid] = "reproduced_current_source"
        corrections = [{**item, "source_id": sid} for item in actions["dividends"]
                       if start.isoformat() <= item["date"] <= end.isoformat()]
        registry["corrections"].extend(corrections)
        verified = {item["date"]: {**item, "source_url": action_url, "source_sha256": action_source["sha256"]}
                    for item in corrections}
        relative = "raw/vendor/" + code + ".js"
        captured = fetch("https://fund.eastmoney.com/pingzhongdata/" + code + ".js", relative, "NAV-" + code)
        if not isinstance(captured["encoding"], str):
            raise ResearchError("NAV source must be text")
        try:
            rows, summary = parse_nav((run / relative).read_text(encoding=captured["encoding"]), code, start, end, verified)
        except ResearchError as exc:
            raise ResearchEvidenceGap(str(exc), [{"code": code, "action": "reconcile_original_issuer_nav_and_actions",
                                                 "required_output": "dated issuer NAV, per-share cash amount and any conversion ratio",
                                                 "acceptance": "reproduce every NAV row and distribution without unexplained differences"}]) from exc
        archived = []
        kind = _nav_archive_kind(code_info[code], captured["registry_hash"])
        for count, (key, saved) in enumerate(store.scan(kind), 1):
            if count > MAX_NAV_ARCHIVE_CAPTURES or time.monotonic() >= deadline:
                exhausted()
            identity = {field: saved[field] for field in ("schema_version", "subject", "capture", "action_capture", "coverage")}
            if fingerprint(identity) != key or saved["subject"] != research_audit.nav_archive_subject(code_info[code]):
                raise ResearchError("Original NAV archive subject or index identity changed")
            original, original_action = saved["capture"], saved["action_capture"]
            if original == captured and original_action == action_source:
                # A sealed preparation may precede a downstream operation failure.
                # Reusing that same capture never establishes an earlier vintage.
                continue
            if dt.datetime.fromisoformat(original["retrieved_at"]) >= dt.datetime.fromisoformat(captured["retrieved_at"]):
                raise ResearchError("Original NAV archive is unavailable at current capture")
            coverage = saved["coverage"]
            first, last = dt.date.fromisoformat(coverage["start"]), dt.date.fromisoformat(coverage["end"])
            if first > last or last > dt.datetime.fromisoformat(original["retrieved_at"]).astimezone(CN).date():
                raise ResearchError("Invalid original NAV archive date coverage")
            if last < start or first > end:
                continue
            size = saved["raw_ref"]["size"] + saved["action_raw_ref"]["size"]
            if charged_bytes + size > MAX_COLLECTION_BYTES:
                exhausted()
            charged_bytes += size
            encoded, action_raw = source_artifacts.read(saved["raw_ref"]), source_artifacts.read(saved["action_raw_ref"])
            sid, action_sid = "NAV-archive-" + key, "TT-actions-archive-" + key
            nav_path, action_path = "raw/nav-archives/" + key + ".js.gz", "raw/nav-archives/" + key + ".actions.html"
            store.assert_owned(context)
            _write_source_bytes(owned_path(run, nav_path), encoded)
            _write_source_bytes(owned_path(run, action_path), action_raw)
            metadata = {**original, "source_id": sid, "path": nav_path, "available_at": original["retrieved_at"],
                "archive_storage": {"encoding": "gzip", "sha256": saved["raw_ref"]["sha256"], "bytes": saved["raw_ref"]["size"]},
                "availability_evidence": {"kind": "archived_original_capture", "source_id": sid,
                    "raw_sha256": original["sha256"], "captured_at": original["retrieved_at"],
                    "origin_manifest_sha256": saved["origin"]["manifest_sha256"]},
                "archive_origin": {**saved["origin"], "subject": saved["subject"], "coverage": coverage, "action_source_id": action_sid}}
            indexed[sid], indexed[action_sid] = metadata, {**original_action, "source_id": action_sid, "path": action_path}
            corrections = research_audit.verify_nav_archive(metadata, indexed, run, read_object(run / "policy.json"))
            raw = research_audit.source_bytes(run, metadata)
            old_rows, _ = parse_nav(raw.decode(metadata["encoding"]), code, start, end, corrections)
            archived.append({"rows": old_rows, "capture_metadata": metadata})
            write_small_json(run / "source-manifest.json", list(indexed.values()))
        rows = bind_source_versions(rows, captured, archived)
        rows_by_code[code] = rows
        summaries.append(summary)
        fetch("https://fundf10.eastmoney.com/jjfl_" + code + ".html", "raw/current-fees/" + code + ".html", "TT-fees-" + code, optional=True)
        completed_codes.append(code)
        progress("collecting")
    observed = {(code, row["date"]) for code, rows in rows_by_code.items() for row in rows}
    missing = [item for item in registry["corrections"] if (item["code"], item["date"]) not in observed]
    if missing:
        raise ResearchEvidenceGap("Retrieve issuer ex-date and NAV evidence for distributions without exact NAV observations",
                                  [{"action": "reconcile_ex_date_with_nav", "records": missing,
                                    "required_output": "original issuer ex-date and matching NAV",
                                    "acceptance": "every in-window entitlement has its exact NAV observation"}])
    used = {"sources": registry["sources"], "anchors": [row for row in registry["anchors"]
            if (row["code"], row["date"]) in observed], "corrections": registry["corrections"],
            "document_verification": verification}
    store.assert_owned(context)
    write_small_json(run / "source-actions.json", {"schema_version": 4, "by_code": action_inventory})
    write_small_json(run / "evidence-registry.json", registry)
    write_small_json(run / "evidence-used.json", used)
    progress("collection_completed")
    return rows_by_code, summaries


def _clear_unsealed_tables(run):
    for name in ("normalized", "features", "labels"):
        target = owned_path(run, name)
        if target.exists():
            if target == run or not target.is_relative_to(run) or any(p.is_symlink() for p in target.rglob("*")):
                raise ResearchError("Unsafe derived table recovery path")
            shutil.rmtree(target)


def _reference(run, base, checked, reused):
    manifest = read_object(run / "manifest.json")
    return {"status": "market_research_prepared", "plan_id": base.name,
            "run_path": run.relative_to(base).as_posix(), "manifest_sha256": file_hash(run / "manifest.json"),
            "audit_sha256": file_hash(run / "audit.json"), "request_hash": manifest["request_hash"],
            "totals": checked["totals"], "readiness": checked["readiness"], "model_fit_executed": False,
            "reused": reused, "publication": "orchestrator_required"}


def prepare(root, plan, codes, start, end, horizons, lookback, timeout=20, *, operation_id, identity_snapshot_ref, store, context=None):
    context = context or store.require_context()
    store.assert_owned(context)
    if isinstance(start, str):
        start = dt.date.fromisoformat(start)
    if isinstance(end, str):
        end = dt.date.fromisoformat(end)
    if type(start) is not dt.date or type(end) is not dt.date:
        raise ResearchError("Research dates must be ISO dates")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ResearchError("Stable operation_id is required")
    if not codes or len(set(codes)) != len(codes) or any(not re.fullmatch(r"[0-9]{6}", code) for code in codes):
        raise ResearchError("Use a nonempty complete set of unique six-digit fund codes")
    if start > end or end > dt.datetime.now(CN).date():
        raise ResearchError("Invalid or future date range")
    if type(lookback) is not int or lookback < 120 or not horizons or any(type(h) is not int or not 1 <= h <= 730 for h in horizons) or len(set(horizons)) != len(horizons):
        raise ResearchError("Use lookback >=120 and unique 1-730 calendar-day horizons")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
        raise ResearchError("Per-source timeout must be 1-30 seconds")
    codes, horizons = sorted(codes), sorted(horizons)
    root, base, _ = select_plan(root, plan, create=True)
    hashes = executor_hashes()
    request = {"schema_version": 4, "operation_id": operation_id, "root": str(root), "plan_id": base.name,
               "codes": codes, "start": start.isoformat(), "end": end.isoformat(),
               "horizons": horizons, "lookback": lookback, "executor_hashes": hashes,
               "identity_snapshot_ref": identity_snapshot_ref}
    identifier = "nav-" + fingerprint({"operation_id": operation_id})[:24]
    run = store.attempt_directory(identifier, context)
    if not run.is_relative_to(base):
        raise ResearchError("Store and research plan ownership differ")
    request["execution_generation"] = context.generation
    write_small_json(run / "request.json", request)
    stage = "collect"
    try:
        executor_dir = run / "executor"
        if executor_dir.exists():
            if any(p.name not in hashes or p.is_symlink() or file_hash(p) != hashes[p.name] for p in executor_dir.iterdir()):
                raise ResearchError("Recovered executor snapshot changed")
        else:
            executor_dir.mkdir()
        for name in EXECUTORS:
            if not (executor_dir / name).exists():
                shutil.copyfile(SCRIPT_DIR / name, executor_dir / name)
        identity_path = run / "identity-snapshot.json"
        local_artifacts = Artifacts(run)
        if identity_path.exists():
            identity_snapshot = read_object(identity_path)
        else:
            artifacts = Artifacts(base)
            if identity_snapshot_ref is None:
                raise ResearchError("Explicit current discovery identity snapshot required")
            identity_snapshot = artifacts.read_json(identity_snapshot_ref)
            fund_universe.verify_identity(identity_snapshot, artifacts, as_of=dt.datetime.now(CN).isoformat())
            identity_snapshot = fund_universe.archive_identity(identity_snapshot, artifacts, local_artifacts)
            write_small_json(identity_path, identity_snapshot)
        identities = fund_universe.verify_identity(identity_snapshot, local_artifacts)
        if not set(codes) <= set(identities):
            raise ResearchEvidenceGap("Complete original issuer identity review before numerical preparation",
                                      identity_snapshot["required_actions"])
        code_info = {code: identities[code] for code in codes}
        if any(row.get("currency") != "CNY" or row.get("execution_venue") != "off_exchange_nav" for row in code_info.values()):
            raise ResearchEvidenceGap("Source-verified CNY off-exchange identities are required", identity_snapshot["required_actions"])
        policy = {"schema_version": 4, "policy_version": POLICY_VERSION, "root": str(root), "plan_id": base.name,
                  "run_id": identifier, "codes": codes, "start": start.isoformat(), "end": end.isoformat(),
                  "horizons": horizons, "lookback": lookback, "code_info": code_info,
                  "distribution_policy": "cash distribution entitlement; no reinvestment or receipt assumption",
                  "feature_policy": "past NAV only; actual historical publication times unknown",
                  "horizon_policy": "feature cutoff NAV date plus h calendar days; first observed NAV on/after target, explicit alignment delay; not a trading fill",
                  "missing_blockers": BLOCKERS, "model_fit_executed": False, "numeric_tolerance": 1e-10,
                  "scope": "conditional_current_shortlist_market_research_not_all_market_selection"}
        write_small_json(run / "policy.json", policy)
        write_small_json(run / "run-status.json", {"status": "collecting", "stage": stage})
        rows_by_code, raw_summaries = collect(run, codes, start, end, timeout, store=store, context=context, code_info=code_info)
        for summary in raw_summaries:
            if fund_universe._text(summary["name_observed_today"]) != code_info[summary["code"]]["name"]:
                raise ResearchEvidenceGap("Current NAV name differs from the captured identity profile",
                                          [fund_universe._review_task(summary["code"], "Resolve current distributor name disagreement with issuer disclosure")])
        stage = "build_samples"
        _clear_unsealed_tables(run)
        summaries = []
        observed_at = dt.datetime.now(CN).isoformat()
        for code in codes:
            store.assert_owned(context)
            rows = rows_by_code[code]
            xs, ys, summary = build_samples(rows, code_info[code], horizons, lookback, observed_at)
            for kind, records in (("normalized", rows), ("features", xs), ("labels", ys)):
                write_tables(run, kind, code, records)
            summaries.append(summary)
        if executor_hashes() != hashes:
            raise ResearchError("Implementation changed during data preparation")
        artifacts = inventory(run)
        semantic = {"policy_version": POLICY_VERSION, "codes": codes, "horizons": horizons, "lookback": lookback,
                    "code_info": code_info, "executors": hashes,
                    "observations": {code: [{key: row[key] for key in ("code", "date", "nav", "cumulative_nav", "distribution_per_share")}
                                           for row in rows_by_code[code]] for code in codes}}
        manifest = {"schema_version": 4, "run_id": identifier, "plan_id": base.name, "operation_id": operation_id,
                    "request_hash": fingerprint(request), "codes": codes, "start": start.isoformat(), "end": end.isoformat(),
                    "horizons": horizons, "lookback": lookback, "policy_version": POLICY_VERSION,
                    "executor_hashes": hashes, "artifacts": artifacts, "dataset_hash": fingerprint(artifacts),
                    "semantic_fingerprint": fingerprint(semantic), "funds": summaries, "raw_summaries": raw_summaries,
                    "action_inventory": read_object(run / "source-actions.json"), "execution_generation": context.generation,
                    "created_at": observed_at, "strict_historical_investor_dataset": "data_unavailable", "model_fit_executed": False}
        write_small_json(run / "manifest.json", manifest)
        stage = "audit"
        checked = research_audit.verify_run(run, hashes)
        write_small_json(run / "audit.json", checked)
        verify_selected(root, base, run)
        write_small_json(run / "run-status.json", {"status": "data_preparation_completed", "publication": "orchestrator_required"})
        store.assert_owned(context)
        _archive_sealed_nav(run, store, context, code_info)
        return _reference(run, base, checked, False)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        write_small_json(run / "run-status.json", {"status": getattr(exc, "code", "failed"), "stage": stage, "error": str(exc),
                                                    "required_actions": getattr(exc, "required_actions", [])})
        raise
