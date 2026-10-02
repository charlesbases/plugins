"""Prepare and verify reproducible public NAV research; never place trades."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import research_audit
from research_data import BLOCKERS, CN, POLICY_VERSION, ResearchError, build_samples, parse_nav, write_tables
from storage import account_status, default_root, path_present, plan_path, read_object, update_latest, write_small_json

SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTORS = ("research.py", "research_data.py", "research_audit.py", "storage.py", "research-evidence.json")
EXCLUDED = {"manifest.json", "audit.json", "run-status.json", "publication.json"}
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def executor_hashes():
    return {name: file_hash(SCRIPT_DIR / name) for name in EXECUTORS}


def within(path, parent):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(parent).resolve()):
        raise ResearchError("Path escapes its owner: " + str(path))
    return path


def inventory(run):
    items = []
    for path in sorted(run.rglob("*")):
        within(path, run)
        if path.is_symlink():
            raise ResearchError("Research artifacts cannot be symbolic links")
        if path.is_file():
            name = path.relative_to(run).as_posix()
            if name not in EXCLUDED:
                items.append({"path": name, "bytes": path.stat().st_size, "sha256": file_hash(path)})
    return sorted(items, key=lambda item: item["path"])


def owned_path(base, relative):
    """Reject aliases before resolving any plan-owned read or write."""
    relative = Path(relative)
    if relative.is_absolute() or any(part in ("..", ".") for part in relative.parts):
        raise ResearchError("Invalid owned path")
    current = Path(base).resolve()
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.resolve() != current:
            raise ResearchError("Linked paths cannot redirect plan-owned records: " + str(current))
    return within(current, base)


def select_plan(root, requested=None, create=False):
    path_present(Path(root))
    root = Path(root).resolve()
    plugin_root = SCRIPT_DIR.parents[2]
    if root.is_relative_to(plugin_root):
        raise ResearchError("Research data cannot be written into the plugin installation")
    owned_path(root, "config.json")
    owned_path(root, "plans")
    status = account_status(root, requested)
    if status["mode"] == "needs_plan_selection":
        raise ResearchError("needs_plan_selection: specify --plan; no plan was selected")
    plan_id = status.get("plan_id") or requested
    if plan_id is None:
        if not create:
            return root, None, status
        plan_id = "research-" + dt.datetime.now(CN).strftime("%Y%m%d")
    base = plan_path(root, plan_id)
    if owned_path(root, "plans/" + plan_id) != base:
        raise ResearchError("Selected plan identity is redirected")
    if base.is_relative_to(plugin_root):
        raise ResearchError("Research plan cannot resolve into the plugin installation")
    if not base.exists():
        if not create:
            raise ResearchError("Research plan does not exist")
        base.mkdir(parents=True, exist_ok=False)
        write_small_json(base / "profile.json", {"schema_version": 1, "purpose": "research", "account_initialized": False})
        config_path = root / "config.json"
        config = read_object(config_path) if config_path.exists() else {"schema_version": 1}
        if config.get("active_plan") is None:
            config["active_plan"] = plan_id
            write_small_json(config_path, config)
    for name in ("profile.json", "indexes/latest.json", "state/current.json", "ledger", "research"):
        path = owned_path(base, name)
        if name in ("ledger", "research"):
            continue
        if path.exists():
            read_object(path)
    return root, base, status


def protection(root, base):
    candidates = [root / "config.json", base / "profile.json", base / "state/current.json"]
    ledger = owned_path(base, "ledger")
    if ledger.exists() and not ledger.is_dir():
        raise ResearchError("Ledger is not a directory")
    ledger_paths = sorted(p for p in ledger.rglob("*") if p.is_file()) if ledger.exists() else []
    files, absent = {}, []
    for path in candidates + ledger_paths:
        if path != root / "config.json":
            owned_path(base, path.relative_to(base))
        within(path, root)
        relative = path.relative_to(root).as_posix()
        if path.exists():
            if not path.is_file():
                raise ResearchError("Protected record is not a file")
            files[relative] = file_hash(path)
        else:
            absent.append(relative)
    return {"root": str(root), "plan_id": base.name, "files": files, "absent": absent, "ledger_paths": [p.relative_to(root).as_posix() for p in ledger_paths]}


def download(url, path, source_id, timeout, expected=None):
    url = urllib.parse.quote(url, safe=":/?=&%")
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ResearchError("Public data downloads require HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.gffunds.com.cn/" if "gffunds.com.cn" in url else "https://fund.eastmoney.com/"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
        status = response.status
    if status != 200 or not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise ResearchError("Empty, failed or oversized public response: " + source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    actual = file_hash(path)
    return {"source_id": source_id, "url": url, "path": None, "bytes": len(data), "sha256": actual, "retrieved_at": dt.datetime.now(CN).isoformat(), "expected_sha256": expected, "review_status": "matched_reviewed_digest" if expected == actual else ("changed_requires_review" if expected else "provider_snapshot")}


def collect(run, codes, start, end, timeout, registry):
    sources, verification = [], {}
    anchors = [r for r in registry["anchors"] if r["code"] in codes and start.isoformat() <= r["date"] <= end.isoformat()]
    corrections = [r for r in registry["corrections"] if r["code"] in codes and start.isoformat() <= r["date"] <= end.isoformat()]
    required_sources = sorted({r["source_id"] for r in anchors + corrections})

    def fetch(url, relative, sid, expected=None, optional=False):
        try:
            record = download(url, run / relative, sid, timeout, expected)
            record["path"] = relative
            sources.append(record)
            return record
        except (OSError, ValueError) as exc:
            sources.append({"source_id": sid, "url": url, "retrieved_at": dt.datetime.now(CN).isoformat(), "expected_sha256": expected, "review_status": "unavailable", "error": str(exc)})
            if not optional:
                raise
            return None
        finally:
            write_small_json(run / "source-manifest.json", sources)

    for sid in required_sources:
        evidence = registry["sources"][sid]
        extension = ".pdf" if evidence["kind"] == "pdf" else ".html"
        record = fetch(evidence["url"], "raw/primary/" + sid + extension, sid, evidence["expected_sha256"], optional=True)
        verification[sid] = record["review_status"] if record else "unavailable"
    used = {"sources": {sid: registry["sources"][sid] for sid in required_sources}, "anchors": anchors, "corrections": corrections, "document_verification": verification}
    write_small_json(run / "evidence-used.json", used)
    rows_by_code, summaries = {}, []
    for code in codes:
        verified = {}
        for correction in corrections:
            sid = correction["source_id"]
            if correction["code"] == code and verification[sid] == "matched_reviewed_digest":
                source = registry["sources"][sid]
                verified[correction["date"]] = {**correction, "source_url": source["url"], "source_sha256": source["expected_sha256"]}
        relative = "raw/vendor/" + code + ".js"
        fetch("https://fund.eastmoney.com/pingzhongdata/" + code + ".js", relative, "NAV-" + code)
        rows, summary = parse_nav((run / relative).read_text(encoding="utf-8-sig"), code, start, end, verified)
        rows_by_code[code] = rows
        summaries.append(summary)
        fetch("https://fundf10.eastmoney.com/jjfl_" + code + ".html", "raw/current-fees/" + code + ".html", "TT-fees-" + code, optional=True)
    observed = {(code, row["date"]) for code, rows in rows_by_code.items() for row in rows}
    for kind, records in (("anchors", anchors), ("corrections", corrections)):
        included, excluded = [], []
        for record in records:
            if verification[record["source_id"]] != "matched_reviewed_digest":
                excluded.append({**record, "reason": "source_not_matched_to_reviewed_digest"})
            elif (record["code"], record["date"]) not in observed:
                excluded.append({**record, "reason": "no_observation_on_exact_evidence_date"})
            else:
                included.append(record)
        used[kind] = included
        used["excluded_" + kind] = excluded
    write_small_json(run / "evidence-used.json", used)
    return rows_by_code, summaries


def resolve_run(base, requested):
    if requested is None:
        latest = read_object(owned_path(base, "indexes/latest.json"))
        requested = latest.get("research_data_generation", {}).get("run_path")
        if requested is None:
            raise ResearchError("No bundled-executor run is registered; prepare new data first")
    parts = Path(requested).parts
    if len(parts) != 5 or parts[0] != "research" or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[-1]):
        raise ResearchError("--run must be research/YYYY/MM/DD/run-id within this plan")
    dt.date(int(parts[1]), int(parts[2]), int(parts[3]))
    path = owned_path(base, requested)
    if not path.is_dir() or path.is_symlink():
        raise ResearchError("Research run does not exist or is a link")
    return path


def verify_selected(root, base, run, registered=False):
    manifest = read_object(run / "manifest.json")
    policy = read_object(run / "policy.json")
    protected = read_object(run / "account-protection.json")
    if manifest.get("plan_id") != base.name or policy.get("plan_id") != base.name or policy.get("run_id") != run.name:
        raise ResearchError("Run does not belong to the selected plan")
    if Path(policy.get("root", "")).resolve() != root or Path(protected.get("root", "")).resolve() != root:
        raise ResearchError("Run does not belong to the selected data root")
    relative = run.relative_to(base).as_posix()
    if registered:
        latest = read_object(owned_path(base, "indexes/latest.json"))
        reference = latest.get("research_data_generation")
        if not isinstance(reference, dict) or reference.get("run_path") != relative:
            raise ResearchError("Registered research reference does not match this run")
        for name in ("manifest", "audit"):
            if reference.get(name + "_path") != relative + "/" + name + ".json" or reference.get(name + "_sha256") != file_hash(run / (name + ".json")):
                raise ResearchError("Registered research artifact changed: " + name)
        if reference.get("dataset_hash") != manifest.get("dataset_hash") or reference.get("executor_hashes") != manifest.get("executor_hashes"):
            raise ResearchError("Registered dataset/executor identity mismatch")
    checked = research_audit.verify_run(run, executor_hashes())
    saved = read_object(run / "audit.json")
    # The saved audit checked account protection at publication. Daily market
    # verification allows subsequent legitimate account updates.
    historical_check = {**checked, "account_protection_checked": True}
    if saved != historical_check:
        raise ResearchError("Saved audit differs from this independent market verification")
    return checked


def publish(run, root, base, prior_index_hash):
    checked = research_audit.verify_run(run, executor_hashes(), verify_protection=True)
    saved = read_object(run / "audit.json")
    if saved != checked:
        raise ResearchError("Saved audit is not bound to this current dataset")
    target = owned_path(base, "indexes/latest.json")
    actual_index_hash = file_hash(target) if target.exists() else None
    if actual_index_hash != prior_index_hash:
        raise ResearchError("Index changed during preparation; refusing to overwrite another writer")
    index = read_object(target) if target.exists() else {"schema_version": 1}
    previous = dict(index)
    lifecycle = index.get("training_lifecycle", {})
    if not isinstance(lifecycle, dict):
        raise ResearchError("Invalid training_lifecycle; cannot replace existing model state")
    lifecycle = dict(lifecycle)
    lifecycle.setdefault("model_status", "untrained")
    relative = run.relative_to(base).as_posix()
    manifest = read_object(run / "manifest.json")
    index["research_data_generation"] = {"schema_version": 1, "run_id": run.name, "run_path": relative, "manifest_path": relative + "/manifest.json", "manifest_sha256": file_hash(run / "manifest.json"), "audit_path": relative + "/audit.json", "audit_sha256": file_hash(run / "audit.json"), "dataset_hash": manifest["dataset_hash"], "semantic_fingerprint": manifest["semantic_fingerprint"], "executor_hashes": manifest["executor_hashes"], "scope": "market_NAV_research", "last_reviewed_at": dt.datetime.now(CN).isoformat()}
    lifecycle["candidate_preparation"] = {"preparation_state": "data_unavailable", "market_research": checked["readiness"]["market_research"], "manifest_path": relative + "/manifest.json", "audit_path": relative + "/audit.json", "codes": manifest["codes"], "horizons": manifest["horizons"], "missing_blockers": BLOCKERS, "actual_fit_executed": False}
    index["training_lifecycle"] = lifecycle
    if any(index.get(key) != value for key, value in previous.items() if key not in ("research_data_generation", "training_lifecycle")):
        raise ResearchError("Unrelated index values changed")
    write_small_json(run / "publication.json", {"prepared_at": dt.datetime.now(CN).isoformat(), "run_path": relative, "commit_rule": "Only a matching plan index confirms publication", "financial_records_changed": False, "retained_model_status": lifecycle["model_status"]})
    write_small_json(run / "run-status.json", {"status": "data_preparation_completed", "publication": "requires_matching_plan_index", "scope": "market_research_only", "preparation_state": "data_unavailable", "model_fit_executed": False})
    # The index replacement is the final commit: no success metadata writes follow.
    update_latest(root, base.name, {"research_data_generation": index["research_data_generation"], "training_lifecycle": lifecycle}, expected_sha256=prior_index_hash)
    return index["research_data_generation"]


def prepare(root, plan, codes, start, end, horizons, lookback, timeout=20):
    if not codes or len(codes) > 50 or len(set(codes)) != len(codes) or any(not re.fullmatch(r"[0-9]{6}", code) for code in codes):
        raise ResearchError("Use 1-50 unique six-digit fund codes; this is a shortlist data adapter")
    if start > end or end > dt.datetime.now(CN).date():
        raise ResearchError("Invalid or future date range")
    if lookback < 120 or not horizons or any(not isinstance(h, int) or not 1 <= h <= 730 for h in horizons) or len(set(horizons)) != len(horizons):
        raise ResearchError("Use lookback >=120 and unique 1-730 calendar-day horizons")
    if not 1 <= timeout <= 30:
        raise ResearchError("Per-request timeout must be 1-30 seconds")
    codes, horizons = sorted(codes), sorted(horizons)
    root, base, status = select_plan(root, plan, create=True)
    research_dir = owned_path(base, "research")
    research_dir.mkdir(parents=True, exist_ok=True)
    lock = owned_path(base, ".research-prepare.lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    run = None
    run_created = False
    stage = "initialize"
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        now = dt.datetime.now(CN)
        identifier = "nav-" + now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        run = owned_path(base, "research/" + now.strftime("%Y/%m/%d") + "/" + identifier)
        if run.is_relative_to(SCRIPT_DIR.parent):
            raise ResearchError("Research data cannot be written into the plugin installation")
        run.mkdir(parents=True, exist_ok=False)
        run_created = True
        index_path = owned_path(base, "indexes/latest.json")
        before_index_hash = file_hash(index_path) if index_path.exists() else None
        write_small_json(run / "account-protection.json", protection(root, base))
        hashes = executor_hashes()
        (run / "executor").mkdir()
        for name in EXECUTORS:
            shutil.copyfile(SCRIPT_DIR / name, run / "executor" / name)
        registry = read_object(SCRIPT_DIR / "research-evidence.json")
        code_info = {code: registry["code_info"].get(code, {"fund_group_id": None, "benchmark_id": None, "type": "not_verified"}) for code in codes}
        policy = {"schema_version": 1, "policy_version": POLICY_VERSION, "root": str(root), "plan_id": base.name, "run_id": identifier, "codes": codes, "start": start.isoformat(), "end": end.isoformat(), "horizons": horizons, "lookback": lookback, "code_info": code_info, "distribution_policy": "cash distribution entitlement; no reinvestment or receipt assumption", "feature_policy": "past NAV only; actual historical publication times unknown", "horizon_policy": "feature cutoff NAV date plus h calendar days; first observed NAV on/after target, explicit alignment delay; not a trading fill", "missing_blockers": BLOCKERS, "model_fit_executed": False, "numeric_tolerance": 1e-10, "scope": "conditional_current_shortlist_market_research_not_all_market_selection", "frozen_at": now.isoformat()}
        write_small_json(run / "policy.json", policy)
        stage = "collect"
        write_small_json(run / "run-status.json", {"status": "collecting", "stage": stage})
        rows_by_code, raw_summaries = collect(run, codes, start, end, timeout, registry)
        stage = "build_samples"
        write_small_json(run / "run-status.json", {"status": "preparing", "stage": stage})
        summaries = []
        observed_at = dt.datetime.now(CN).isoformat()
        for code in codes:
            rows = rows_by_code[code]
            xs, ys, summary = build_samples(rows, code_info[code], horizons, lookback, observed_at)
            for kind, records in (("normalized", rows), ("features", xs), ("labels", ys)):
                write_tables(run, kind, code, records)
            summaries.append(summary)
        artifacts = inventory(run)
        semantic = {"policy_version": POLICY_VERSION, "codes": codes, "horizons": horizons, "lookback": lookback, "code_info": code_info, "executors": hashes, "observations": {code: [{key: row[key] for key in ("code", "date", "nav", "cumulative_nav", "distribution_per_share")} for row in rows_by_code[code]] for code in codes}}
        manifest = {"schema_version": 1, "run_id": identifier, "plan_id": base.name, "codes": codes, "start": start.isoformat(), "end": end.isoformat(), "horizons": horizons, "lookback": lookback, "policy_version": POLICY_VERSION, "executor_hashes": hashes, "artifacts": artifacts, "dataset_hash": fingerprint(artifacts), "semantic_fingerprint": fingerprint(semantic), "funds": summaries, "raw_summaries": raw_summaries, "created_at": observed_at, "strict_historical_investor_dataset": "data_unavailable", "model_fit_executed": False}
        write_small_json(run / "manifest.json", manifest)
        stage = "audit"
        checked = research_audit.verify_run(run, hashes, verify_protection=True)
        write_small_json(run / "audit.json", checked)
        stage = "publish"
        pointer = publish(run, root, base, before_index_hash)
        return {"status": "market_research_prepared", "account_mode": status["mode"], "data_root": str(root), "plan_id": base.name, "run_path": pointer["run_path"], "totals": checked["totals"], "readiness": checked["readiness"], "model_fit_executed": False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if run_created:
            write_small_json(run / "run-status.json", {"status": "failed", "stage": stage, "error": str(exc), "published_as_success": False})
        raise
    finally:
        os.close(fd)
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "prepare", "verify"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--plan")
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--start", type=dt.date.fromisoformat)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.datetime.now(CN).date())
    parser.add_argument("--horizons", type=int, nargs="+", default=[30, 60, 90])
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--run")
    args = parser.parse_args()
    try:
        root = args.root if args.root is not None else default_root()
        if args.command == "prepare":
            if args.codes is None or args.start is None:
                raise ResearchError("prepare requires --codes and --start; no personal candidate list is inferred")
            result = prepare(root, args.plan, args.codes, args.start, args.end, args.horizons, args.lookback, args.timeout)
        else:
            root, base, status = select_plan(root, args.plan)
            if args.command == "status":
                result = {"account": status, "research": "absent"}
                if base and owned_path(base, "indexes/latest.json").exists():
                    index = read_object(owned_path(base, "indexes/latest.json"))
                    pointer = index.get("research_data_generation")
                    result["research"] = "registered_requires_verify" if isinstance(pointer, dict) and pointer.get("run_path") else ("legacy_requires_prepare" if pointer else "absent")
                    result["reference"] = pointer
            else:
                if base is None:
                    raise ResearchError("No research plan; prepare data first")
                run = resolve_run(base, args.run)
                result = verify_selected(root, base, run, registered=args.run is None)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "data_unavailable", "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
