"""Independently verify archived fund research; never fit or qualify a model."""

import bisect
import datetime as dt
import hashlib
import gzip
import io
import json
import re
import urllib.parse
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path, PurePosixPath

from contracts import canonical_bytes, fingerprint
from source_fetch import validate_capture_provenance, checked_url, source_rule
import fund_universe
import issuer_nav
from artifacts import Artifacts


class AuditError(ValueError):
    pass


EXCLUDED = {"manifest.json", "audit.json", "run-status.json"}
EXECUTORS = {
    "research.py", "research_data.py", "research_audit.py", "file_io.py", "contracts.py",
    "source_fetch.py", "source_registry.json", "fund_universe.py", "artifacts.py", "source_documents.py", "fund_screen.py", "risk_profile.py", "issuer_nav.py",
}
BLOCKERS = [
    "historical_NAV_publication_time", "historical_TT_buyability_limits",
    "historical_TT_subscription_redemption_fee_rules",
    "historical_order_confirmation_and_cash_arrival",
    "historical_surviving_and_terminated_fund_universe",
]
OUTCOMES = ("market_return", "terminal_loss", "principal_path_loss", "peak_drawdown")
TOLERANCE = Decimal("1e-10")
CN = dt.timezone(dt.timedelta(hours=8))
POLICY_RULES = {
    "policy_version": "market-nav-research-4",
    "distribution_policy": "cash distribution entitlement; no reinvestment or receipt assumption",
    "feature_policy": "past NAV only; actual historical publication times unknown",
    "horizon_policy": "feature cutoff NAV date plus h calendar days; first observed NAV on/after target, explicit alignment delay; not a trading fill",
    "numeric_tolerance": 1e-10,
}


def require(condition, message):
    if not condition:
        raise AuditError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_constant(value):
    raise AuditError("Non-finite JSON value: " + value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path, decimal=False):
    try:
        kwargs = {"parse_constant": reject_constant, "object_pairs_hook": unique_object}
        if decimal:
            kwargs["parse_float"] = Decimal
        return json.loads(path.read_text(encoding="utf-8-sig"), **kwargs)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AuditError(f"Cannot read valid JSON at {path}: {exc}") from exc


def safe_path(base, name):
    require(isinstance(name, str) and name, "Missing relative artifact path")
    pure = PurePosixPath(name)
    require(not pure.is_absolute() and "\\" not in name and ":" not in name
            and pure.as_posix() == name and all(p not in (".", "..") for p in pure.parts),
            "Invalid relative artifact path: " + name)
    path = base / name
    require(path.resolve().is_relative_to(base.resolve()), "Artifact path escapes root: " + name)
    return path


def inventory(run):
    result = []
    for path in sorted(run.rglob("*")):
        require(not path.is_symlink(), "Symlink in research inventory: " + str(path))
        require(path.resolve().is_relative_to(run), "Research path escapes run: " + str(path))
        if path.is_file() and path.relative_to(run).as_posix() not in EXCLUDED:
            result.append({"path": path.relative_to(run).as_posix(),
                           "bytes": path.stat().st_size, "sha256": digest(path)})
    return sorted(result, key=lambda value: value["path"])


def number(value, context):
    require(not isinstance(value, bool) and isinstance(value, (int, float, str, Decimal)),
            "Invalid number: " + context)
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AuditError("Invalid number: " + context) from exc
    require(result.is_finite(), "Non-finite number: " + context)
    return result


def near(actual, expected, context, tolerance=TOLERANCE):
    require(abs(number(actual, context) - expected) <= tolerance,
            "Independent recomputation mismatch: " + context)


def date(value, context):
    require(isinstance(value, str), "Invalid date: " + context)
    try:
        result = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise AuditError("Invalid date: " + context) from exc
    require(result.isoformat() == value, "Non-canonical date: " + context)
    return result


def json_assignment(text, name):
    matches = list(re.finditer(r"\bvar\s+" + re.escape(name) + r"\s*=\s*", text))
    require(len(matches) == 1, "Missing or duplicate vendor array: " + name)
    match = matches[0]
    decoder = json.JSONDecoder(parse_float=Decimal, parse_constant=reject_constant,
                               object_pairs_hook=unique_object)
    try:
        result, consumed = decoder.raw_decode(text[match.end():].lstrip())
    except ValueError as exc:
        raise AuditError("Invalid vendor array: " + name) from exc
    tail = text[match.end():].lstrip()[consumed:]
    require(tail.lstrip().startswith(";"), "Invalid vendor assignment boundary: " + name)
    return result


def json_array(text, name):
    result = json_assignment(text, name)
    require(isinstance(result, list), "Vendor field is not an array: " + name)
    return result


def distribution(text):
    require(isinstance(text, str), "Distribution text must be a string")
    if not text.strip():
        return Decimal(0)
    match = re.fullmatch(r"(?:分红[:：])?每(?:([0-9.]+))?份派现金([0-9.]+)元?", text.strip())
    require(match is not None, "Unresolved dividend or split text: " + text)
    units = number(match.group(1) or "1", "distribution units")
    amount = number(match.group(2), "distribution amount")
    require(units > 0 and amount >= 0, "Invalid distribution quantity")
    return amount / units


def tables(run, kind, code):
    folder = run / kind / code
    rows = []
    for path in sorted(folder.glob("*.jsonl")):
        require(re.fullmatch(r"\d{4}-\d{2}-part-\d+\.jsonl", path.name) is not None,
                "Unexpected table partition: " + str(path))
        require(path.stat().st_size <= 16 * 1024 * 1024, "Oversized table partition")
        content = path.read_bytes()
        require(not content or content.endswith(b"\n"), "Incomplete JSONL table: " + str(path))
        for line in content.decode("utf-8").splitlines():
            require(line.strip(), "Blank table row: " + str(path))
            try:
                row = json.loads(line, parse_constant=reject_constant, object_pairs_hook=unique_object)
            except ValueError as exc:
                raise AuditError("Invalid table JSON: " + str(path)) from exc
            require(isinstance(row, dict) and row.get("code") == code,
                    "Table identity mismatch: " + str(path))
            day = row.get("date") or row.get("feature_cutoff_nav_date") or row.get("start_nav_date")
            require(isinstance(day, str) and day[:7] == path.name[:7], "Table month mismatch")
            rows.append(row)
    return rows


def max_drawdown(values):
    peak, result = values[0], Decimal(0)
    for value in values:
        if value > peak:
            peak = value
        result = max(result, (peak - value) / peak)
    return result


def source_bytes(run, source):
    """Read lossless local storage while verifying the actual captured bytes."""
    path = safe_path(run, source["path"])
    storage = source.get("archive_storage")
    if storage is not None:
        from source_fetch import MAX_BYTES
        require(isinstance(storage, dict) and storage.get("encoding") == "gzip"
                and type(source.get("bytes")) is int and 0 < source["bytes"] <= MAX_BYTES
                and type(storage.get("bytes")) is int and 0 < storage["bytes"] <= MAX_BYTES + 1024,
                "Invalid compressed NAV storage bounds")
        with path.open("rb") as stream:
            encoded = stream.read(storage["bytes"] + 1)
        require(len(encoded) == storage["bytes"] and hashlib.sha256(encoded).hexdigest() == storage.get("sha256"),
                "Compressed NAV storage changed")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(encoded), mode="rb") as stream:
                raw = stream.read(source["bytes"] + 1)
        except (OSError, EOFError) as exc:
            raise AuditError("Invalid compressed NAV original bytes") from exc
    else:
        with path.open("rb") as stream:
            raw = stream.read(source["bytes"] + 1)
    require(len(raw) == source["bytes"] and hashlib.sha256(raw).hexdigest() == source.get("sha256")
            and source.get("sha256") == source.get("raw_sha256"), "Source body changed: " + source["source_id"])
    return raw


def nav_archive_subject(identity):
    return {key: identity.get(key) for key in
            ("code", "name", "legal_name", "currency", "share_class", "fund_group_id", "execution_venue")}


def verify_nav_archive(source, sources, run, policy):
    """Bind imported bytes and times to a prior sealed capture, never its tables."""
    origin = source["archive_origin"]
    base = Path(policy["root"]).resolve() / "plans" / policy["plan_id"]
    require(run.resolve().is_relative_to(base), "NAV archive plan ownership differs")
    original_run = safe_path(base, origin["run_path"])
    require(original_run.resolve() != run.resolve(), "NAV archive must come from a prior sealed run")
    manifest_path, audit_path = original_run / "manifest.json", original_run / "audit.json"
    require(digest(manifest_path) == origin["manifest_sha256"]
            and digest(audit_path) == origin["audit_sha256"], "Original NAV archive seal changed")
    manifest, sealed_audit = read_json(manifest_path), read_json(audit_path)
    require(manifest.get("plan_id") == policy["plan_id"] and manifest.get("run_id") == original_run.name
            and sealed_audit.get("manifest_sha256") == origin["manifest_sha256"]
            and sealed_audit.get("dataset_hash") == manifest.get("dataset_hash")
            and read_json(original_run / "run-status.json").get("status") == "data_preparation_completed",
            "Original NAV archive is not a sealed capture")
    require(fingerprint(manifest["artifacts"]) == manifest["dataset_hash"], "Original NAV artifact seal differs")
    artifacts = {item["path"]: item for item in manifest["artifacts"]}

    def sealed_path(relative):
        path = safe_path(original_run, relative)
        item = artifacts.get(relative)
        require(item is not None and not path.is_symlink() and path.is_file()
                and path.stat().st_size == item["bytes"] and digest(path) == item["sha256"],
                "Original NAV archive artifact changed: " + relative)
        return path

    original_policy = read_json(sealed_path("policy.json"))
    request = read_json(sealed_path("request.json"))
    require(fingerprint(request) == manifest["request_hash"] and original_policy["root"] == policy["root"]
            and original_policy["plan_id"] == policy["plan_id"], "Original NAV archive request ownership differs")
    transport_registry = read_json(sealed_path("executor/source_registry.json"))
    identities = fund_universe.verify_identity(read_json(sealed_path("identity-snapshot.json")),
                                              Artifacts(original_run), registry=transport_registry)
    code = origin["subject"]["code"]
    require(code in identities and code in policy["code_info"]
            and nav_archive_subject(identities[code]) == origin["subject"]
            and origin["subject"] == nav_archive_subject(policy["code_info"][code]),
            "Original NAV archive fund subject differs")
    originals = {item["source_id"]: item for item in read_json(sealed_path("source-manifest.json"))}
    original = originals.get("NAV-" + code)
    require(isinstance(original, dict) and original.get("path") == f"raw/vendor/{code}.js"
            and original.get("requested_url") == f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
            "Original NAV archive request identity differs")
    validate_capture_provenance(original, transport_registry)
    require(digest(sealed_path(original["path"])) == original["sha256"], "Original NAV capture bytes changed")
    excluded = {"path", "source_id", "available_at", "availability_evidence", "archive_origin", "archive_storage"}
    require({key: value for key, value in source.items() if key not in excluded}
            == {key: value for key, value in original.items() if key not in excluded},
            "NAV archive metadata or original capture time differs")
    expected_evidence = {"kind": "archived_original_capture", "source_id": source["source_id"],
                         "raw_sha256": original["sha256"], "captured_at": original["retrieved_at"],
                         "origin_manifest_sha256": origin["manifest_sha256"]}
    require(source.get("available_at") == original["retrieved_at"]
            and source.get("availability_evidence") == expected_evidence,
            "NAV archive availability differs from original capture")
    raw = source_bytes(run, source)
    require(hashlib.sha256(raw).hexdigest() == original["sha256"], "Imported original NAV bytes changed")
    points = json_array(raw.decode(source["encoding"]), "Data_netWorthTrend")
    days = [dt.datetime.fromtimestamp(int(point["x"])/1000, CN).date().isoformat() for point in points]
    require(days and days == sorted(set(days)) and origin.get("coverage") == {"start": days[0], "end": days[-1]},
            "Original NAV archive coverage differs from captured bytes")
    action = originals.get("TT-actions-" + code)
    imported_action = sources.get(origin["action_source_id"])
    require(isinstance(action, dict) and isinstance(imported_action, dict), "Original NAV action capture missing")
    validate_capture_provenance(action, transport_registry)
    require({key: value for key, value in imported_action.items() if key not in {"path", "source_id"}}
            == {key: value for key, value in action.items() if key not in {"path", "source_id"}}
            and digest(sealed_path(action["path"])) == action["sha256"]
            and digest(safe_path(run, imported_action["path"])) == action["sha256"],
            "Original NAV action bytes or capture metadata changed")
    require(dt.datetime.fromisoformat(action["retrieved_at"]) <= dt.datetime.fromisoformat(original["retrieved_at"]),
            "Original NAV correction capture was unavailable at NAV capture")
    parsed = fund_universe.parse_actions(sealed_path(action["path"]).read_text(encoding=action["encoding"]), code)
    corrections = {}
    for item in parsed["dividends"]:
        if original_policy["start"] <= item["date"] <= original_policy["end"]:
            corrections[item["date"]] = {**item, "currency": identities[code]["currency"],
                "currency_basis": "same_share_source_identity", "source_id": imported_action["source_id"],
                "source_url": action["requested_url"], "source_sha256": action["sha256"]}
    return corrections


def verify_sources(run, used, registry):
    require(isinstance(used, dict) and isinstance(used.get("sources"), dict), "Invalid evidence-used registry")
    require(isinstance(registry, dict) and isinstance(registry.get("sources"), dict), "Invalid curated registry")
    sources = read_json(run / "source-manifest.json")
    require(isinstance(sources, list), "Source manifest must be an array")
    indexed = {}
    transport_registry = read_json(run / "executor/source_registry.json")
    for source in sources:
        require(isinstance(source, dict) and isinstance(source.get("source_id"), str), "Invalid source record")
        ident = source["source_id"]
        require(ident not in indexed, "Duplicate source identifier: " + ident)
        if source.get("path") is not None:
            raw = source_bytes(run, source)
            validate_capture_provenance(source, transport_registry)
            require(source.get("raw_sha256") == source.get("sha256"), "Raw source hash differs")
            if source.get("encoding") is not None:
                decoded = raw.decode(source["encoding"], errors="strict")
                require(hashlib.sha256(decoded.encode("utf-8")).hexdigest() == source.get("text_sha256"),
                        "Decoded source text identity differs")
            else:
                require(source.get("text_sha256") is None, "Binary source has invented decoded text")
        else:
            require(isinstance(source.get("error"), str) and source["error"]
                    and source.get("sha256") is None and source.get("bytes") is None,
                    "Missing source body without a failure record: " + ident)
        require(isinstance(source.get("requested_url"), str) and source["requested_url"].startswith("https://"), "Invalid source URL")
        require(isinstance(source.get("retrieved_at"), str), "Missing source retrieval time")
        try:
            retrieved = dt.datetime.fromisoformat(source["retrieved_at"])
        except ValueError as exc:
            raise AuditError("Invalid source retrieval time") from exc
        require(retrieved.tzinfo is not None, "Source retrieval time lacks timezone")
        indexed[ident] = source
    statuses = used.get("document_verification")
    require(isinstance(statuses, dict), "Missing independent document verification")
    matched = set()
    for ident, recorded in used["sources"].items():
        require(ident in registry["sources"] and recorded == registry["sources"][ident],
                "Evidence source differs from reviewed registry: " + ident)
        original = registry["sources"][ident]
        expected = original.get("expected_sha256")
        require(isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected),
                "Missing reviewed source digest: " + ident)
        source = indexed.get(ident)
        expected_url = urllib.parse.quote(original.get("url", ""), safe=":/?=&%")
        actual = (source is not None and source.get("requested_url") == expected_url
                  and source.get("sha256") == expected)
        if actual:
            require(statuses.get(ident) == "reproduced_current_source", "Current source reproduction status mismatch: " + ident)
            matched.add(ident)
        else:
            require(statuses.get(ident) in ("changed_requires_review", "unavailable"),
                    "Unverified document cannot be marked reviewed: " + ident)
    require(set(statuses) == set(used["sources"]), "Document verification inventory mismatch")
    return matched


def verify_run(run, expected_executor_hashes=None):
    """Return a fresh audit; all comparisons remain active with python -O."""
    try:
        path = Path(run)
        require(not path.is_symlink(), "Research run cannot be a symlink")
        return _verify_run(path.resolve(), expected_executor_hashes)
    except AuditError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise AuditError("Malformed or unavailable research inputs: " + str(exc)) from exc


def _verify_run(run, expected_executor_hashes):
    require(run.is_dir(), "Research run is not a directory")
    manifest = read_json(run / "manifest.json")
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 4, "Unsupported research manifest")
    require(manifest.get("model_fit_executed", False) is False
            and manifest.get("strict_historical_investor_dataset", "data_unavailable") == "data_unavailable",
            "Market research manifest cannot claim fitted or qualified investor model")
    require(type(manifest.get("execution_generation")) is int and manifest["execution_generation"] > 0
            and manifest["execution_generation"] == read_json(run / "request.json").get("execution_generation"),
            "Research generation differs")
    require(manifest.get("run_id") == run.name, "Manifest run identity mismatch")
    require(isinstance(manifest.get("plan_id"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", manifest["plan_id"]), "Invalid manifest plan")
    codes = manifest.get("codes")
    require(isinstance(codes, list) and codes and codes == sorted(set(codes))
            and all(isinstance(code, str) and re.fullmatch(r"\d{6}", code) for code in codes), "Invalid fund universe")
    start, end = date(manifest.get("start"), "manifest start"), date(manifest.get("end"), "manifest end")
    require(start <= end, "Invalid research date range")
    horizons, lookback = manifest.get("horizons"), manifest.get("lookback")
    require(isinstance(horizons, list) and horizons and horizons == sorted(set(horizons))
            and all(type(h) is int and h > 0 for h in horizons), "Invalid label horizons")
    require(type(lookback) is int and lookback >= 120, "Feature lookback must cover all windows")
    artifacts = manifest.get("artifacts")
    require(isinstance(artifacts, list), "Missing artifact inventory")
    observed = inventory(run)
    require(artifacts == observed, "Artifact inventory, content or size changed")
    canonical = canonical_bytes(artifacts)
    require(hashlib.sha256(canonical).hexdigest() == manifest.get("dataset_hash"), "Dataset fingerprint mismatch")
    executors = manifest.get("executor_hashes")
    require(isinstance(executors, dict) and set(executors) == EXECUTORS, "Incomplete executor inventory")
    for name, expected in executors.items():
        require(digest(run / "executor" / name) == expected, "Archived executor changed: " + name)
    if expected_executor_hashes is not None:
        require(executors == expected_executor_hashes, "Run executors differ from installed executors")
    policy = read_json(run / "policy.json")
    require(isinstance(policy, dict), "Missing research policy")
    require(policy.get("schema_version") == 4, "Unsupported research policy schema")
    request = read_json(run / "request.json")
    require(isinstance(request, dict) and request.get("schema_version") == 4
            and fingerprint(request) == manifest.get("request_hash")
            and request.get("operation_id") == manifest.get("operation_id"), "Research operation binding differs")
    for field in ("root", "plan_id", "codes", "start", "end", "horizons", "lookback"):
        require(request.get(field) == policy.get(field), "Research request/policy differs: " + field)
    require(request.get("executor_hashes") == executors, "Research request executor differs")
    for field in ("start", "end", "horizons", "lookback", "policy_version"):
        require(policy.get(field) == manifest.get(field), "Manifest/policy mismatch: " + field)
    for field in ("codes", "plan_id", "run_id"):
        require(policy.get(field) == manifest.get(field), "Manifest/policy identity mismatch: " + field)
    require(isinstance(policy.get("root"), str) and Path(policy["root"]).is_absolute(),
            "Policy root must be absolute")
    require(isinstance(policy.get("code_info"), dict) and set(policy["code_info"]) == set(codes),
            "Policy universe mismatch")
    require(policy.get("missing_blockers") == BLOCKERS, "Historical investor blocker policy changed")
    require(policy.get("model_fit_executed", False) is False, "Market policy cannot claim model fitting")
    for field, expected in POLICY_RULES.items():
        require(policy.get(field) == expected, "Unsupported research policy: " + field)
    used = read_json(run / "evidence-used.json")
    registry = read_json(run / "evidence-registry.json")
    identities = fund_universe.verify_identity(read_json(run / "identity-snapshot.json"), Artifacts(run),
                                              registry=read_json(run / "executor/source_registry.json"))
    require(set(codes) <= set(identities), "Complete source-derived identities required")
    require(policy["code_info"] == {code: identities[code] for code in codes},
            "Policy identity differs from captured legal fund profiles")
    matched = verify_sources(run, used, registry)
    verify_action_registry(run, codes, start, end, registry)
    # The following stage is independent of research_data's parsing and calculations.
    with localcontext() as context:
        context.prec = 40
        results = verify_tables(run, codes, start, end, horizons, lookback, policy, used, registry, matched)
    return {
        "manifest_sha256": digest(run / "manifest.json"), "dataset_hash": manifest["dataset_hash"],
        "totals": results["totals"], "by_horizon_days": results["by_horizon_days"],
        "official_NAV_anchors": results["official_NAV_anchors"],
        "readiness": {"market_research": "passed" if results["totals"]["feature_rows"] else "insufficient_history",
                      "historical_investor": "data_unavailable", "strict_PIT": "not_verified"},
        "model_fit_executed": False, "missing_blockers": BLOCKERS,
        "artifacts_checked": len(artifacts), "account_state_managed_by": "orchestrator",
        "verification_scope": "Full arithmetic market research audit; issuer checks cover selected points only",
    }


def verify_action_registry(run, codes, start, end, registry):
    """Reproduce each correction from its current source, including disclosed units."""
    sources = {row["source_id"]: row for row in read_json(run / "source-manifest.json")}
    expected_sources, expected_corrections, inventory = {}, [], {}
    for code in codes:
        sid = "TT-actions-" + code
        source = sources[sid]
        url = "https://fundf10.eastmoney.com/fhsp_" + code + ".html"
        require(source["requested_url"] == url, "Action source code URL mismatch")
        text = safe_path(run, source["path"]).read_text(encoding=source["encoding"])
        actions = fund_universe.parse_actions(text, code)
        for item in actions["dividends"]:
            item["currency"] = read_json(run / "policy.json")["code_info"][code]["currency"]
            item["currency_basis"] = "same_share_source_identity"
        inventory[code] = {**actions, "known_at": source["retrieved_at"], "source_id": sid,
                           "source_sha256": source["sha256"], "source_path": source["path"], "source_url": url,
                           "coverage": "all_rows_in_current_distributor_table_not_all_future_announcements",
                           "rights_terms_status": "issuer_entitlement_rules_not_in_distributor_table"}
        require(not any(start.isoformat() <= row["date"] <= end.isoformat() for row in actions["splits"]),
                "Retrieve and validate original split disclosure before numerical preparation")
        expected_sources[sid] = {"url": url, "expected_sha256": source["sha256"], "kind": "html",
                                 "code": code, "scope": actions["scope"]}
        expected_corrections.extend({**row, "source_id": sid} for row in actions["dividends"]
                                    if start.isoformat() <= row["date"] <= end.isoformat())
    saved_inventory = read_json(run / "source-actions.json")
    require(saved_inventory == {"schema_version": 4, "by_code": inventory}
            and read_json(run / "manifest.json")["action_inventory"] == saved_inventory,
            "Full captured action inventory differs from original tables")
    nav_evidence = issuer_nav.inventory(read_json(run / "identity-snapshot.json"), Artifacts(run), codes,
                                      transport_registry=read_json(run / "executor/source_registry.json"))
    require(read_json(run / "issuer-nav-evidence.json") == nav_evidence,
            "Issuer NAV evidence differs from original source and identity re-extraction")
    for record in nav_evidence["source_records"]:
        require(sources.get(record["source_id"]) == record, "Issuer NAV capture record differs from raw document")
    expected_sources.update(nav_evidence["sources"])
    require(registry == {"schema_version": 4, "sources": expected_sources, "anchors": nav_evidence["anchors"], "corrections": expected_corrections},
            "Runtime action registry differs from source tables and per-share unit calculation")


def verify_tables(run, codes, start, end, horizons, lookback, policy, used, registry, matched):
    require(isinstance(used.get("anchors"), list) and isinstance(used.get("corrections"), list),
            "Missing selected evidence records")
    require(isinstance(registry.get("anchors"), list) and isinstance(registry.get("corrections"), list),
            "Missing reviewed evidence records")
    source_rows = read_json(run / "source-manifest.json")
    manifest = read_json(run / "manifest.json")
    raw_summaries = summary_index(manifest.get("raw_summaries"), codes, "raw_summaries")
    fund_summaries = summary_index(manifest.get("funds"), codes, "funds")
    source_index = {source["source_id"]: source for source in source_rows}
    archive_corrections = {ident: verify_nav_archive(source, source_index, run, policy)
                           for ident, source in source_index.items() if "archive_origin" in source}
    version_sources = {}
    corrections = {}
    for record in used["corrections"]:
        require(isinstance(record, dict), "Invalid selected correction")
        key = (record.get("code"), record.get("date"))
        require(key not in corrections, "Duplicate selected correction")
        original = next((item for item in registry["corrections"]
                         if item.get("code") == key[0] and item.get("date") == key[1]), None)
        require(original is not None and record.get("source_id") in matched,
                "Correction lacks reviewed matching evidence")
        for field in ("code", "date", "distribution_per_share", "source_id", "location", "venue"):
            require(record.get(field) == original.get(field), "Correction differs from reviewed registry: " + field)
        source = registry["sources"][record["source_id"]]
        unpinned = {key: value for key, value in record.items() if key not in ("source_url", "source_sha256")}
        require(unpinned == original, "Selected correction metadata differs from reviewed registry")
        if "source_sha256" in record:
            require(record["source_sha256"] == source["expected_sha256"], "Selected correction pin differs")
        if "source_url" in record:
            require(record["source_url"] == source["url"], "Selected correction URL differs")
        require(record.get("venue") == "off_exchange", "Correction venue mismatch")
        corrections[key] = {**record, "source_sha256": source["expected_sha256"],
                            "source_url": source["url"]}
    totals = {"NAV_rows": 0, "feature_rows": 0, "label_rows": 0,
              "mature_market_labels": 0, "pending_market_labels": 0,
              "strict_historical_investor_labels": 0}
    by_horizon = {str(h): {"mature_market_labels": 0, "pending_market_labels": 0} for h in horizons}
    all_normalized, semantic_observations = {}, {}
    retrieved_at = max(dt.datetime.fromisoformat(row["retrieved_at"]) for row in source_rows)
    for kind in ("normalized", "features", "labels"):
        for path in (run / kind).rglob("*"):
            if path.is_file():
                require(path.parent.name in codes and path.parent.parent == run / kind,
                        "Unexpected table/code inventory: " + str(path))
    for code in codes:
        vendor_source = source_index.get("NAV-" + code)
        require(isinstance(vendor_source, dict)
                and vendor_source.get("path") == f"raw/vendor/{code}.js"
                and vendor_source.get("requested_url") == f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                "Vendor snapshot lacks matching code/request provenance: " + code)
        require(isinstance(vendor_source.get("encoding"), str), "Vendor source lacks text encoding")
        text = source_bytes(run, vendor_source).decode(vendor_source["encoding"])
        require(json_assignment(text, "fS_code") == code, "Vendor response fund code mismatch")
        observed_name = json_assignment(text, "fS_name")
        require(isinstance(observed_name, str) and observed_name.strip(), "Missing vendor fund name")
        require(fund_universe._text(observed_name) == policy["code_info"][code]["name"],
                "Current NAV name differs from the captured identity profile")
        raw = json_array(text, "Data_netWorthTrend")
        cumulative_items = json_array(text, "Data_ACWorthTrend")
        accumulated, cumulative_dates = {}, set()
        for item in cumulative_items:
            require(isinstance(item, list) and len(item) == 2 and item[0] not in accumulated,
                    "Duplicate or invalid cumulative NAV observation")
            stamp = number(item[0], "cumulative NAV timestamp")
            require(stamp == stamp.to_integral_value(), "Cumulative NAV timestamp is not integral")
            day = dt.datetime.fromtimestamp(int(stamp) / 1000, CN).date()
            require(day not in cumulative_dates, "Duplicate cumulative NAV date")
            cumulative_dates.add(day)
            accumulated[int(stamp)] = number(item[1], "cumulative NAV")
        expected, raw_dates, raw_stamps = [], [], set()
        for item in raw:
            require(isinstance(item, dict), "Invalid raw NAV observation")
            timestamp = number(item.get("x"), "NAV timestamp")
            require(timestamp == timestamp.to_integral_value(), "NAV timestamp is not integral")
            nav_date = dt.datetime.fromtimestamp(int(timestamp) / 1000, CN).date()
            raw_dates.append(nav_date)
            raw_stamps.add(int(timestamp))
            require(item["x"] in accumulated, "Missing raw cumulative NAV")
            nav = number(item.get("y"), "raw unit NAV")
            require(nav > 0 and accumulated[item["x"]] > 0, "Non-positive raw NAV")
            if not start <= nav_date <= end:
                continue
            text_distribution = item.get("unitMoney", "")
            original_distribution = distribution(text_distribution)
            cash = original_distribution
            correction = corrections.get((code, nav_date.isoformat()))
            if correction:
                official = number(correction["distribution_per_share"], "reviewed distribution")
                require(official >= 0 and cash in (Decimal(0), official), "Provider/issuer distribution conflict")
                cash = official
            quoted = item.get("equityReturn")
            quoted = None if quoted is None else number(quoted, "provider return") / 100
            expected.append({"code": code, "date": nav_date.isoformat(), "nav": nav,
                             "cumulative_nav": accumulated[item["x"]],
                             "distribution_per_share": cash, "distribution_text": text_distribution,
                             "provider_daily_return": quoted, "historical_published_at": None,
                             "correction": correction, "original_distribution": original_distribution})
        require(raw_dates == sorted(set(raw_dates)), "Duplicate or unordered vendor NAV dates")
        require(raw_stamps == set(accumulated), "NAV/cumulative timestamp coverage mismatch")
        require(expected, "No real NAV observations in declared window: " + code)
        normalized = tables(run, "normalized", code)
        require(len(normalized) == len(expected), "Normalized NAV coverage mismatch: " + code)
        for expected_row, row in zip(expected, normalized):
            key = (code, expected_row["date"])
            require(key not in all_normalized and row.get("date") == expected_row["date"],
                    "Normalized NAV identity/order mismatch")
            required = {"code", "date", "nav", "cumulative_nav", "distribution_per_share", "distribution_text",
                        "provider_daily_return", "historical_published_at"}
            require(required <= set(row) <= required | {"distribution_evidence", "source_versions"}, "Normalized field inventory mismatch")
            for field in ("nav", "cumulative_nav", "distribution_per_share"):
                near(row.get(field), expected_row[field], code + ":" + row["date"] + ":" + field)
            require(row.get("distribution_text") == expected_row["distribution_text"]
                    and row.get("historical_published_at") is None, "Normalized provenance mismatch")
            if expected_row["provider_daily_return"] is None:
                require(row.get("provider_daily_return") is None, "Invented provider return")
            else:
                near(row.get("provider_daily_return"), expected_row["provider_daily_return"], "provider return")
            correction = expected_row["correction"]
            if correction:
                saved = row.get("distribution_evidence")
                require(isinstance(saved, dict), "Reviewed correction provenance missing")
                for field, value in correction.items():
                    require(saved.get(field) == value, "Normalized correction provenance differs: " + field)
                mode = "provider_already_matches" if expected_row["original_distribution"] else "evidence补录"
                require(saved.get("mode") == mode, "Correction mode mismatch")
            else:
                require(row.get("distribution_evidence") is None, "Unreviewed normalized correction")
            if "source_versions" in row:
                verify_nav_versions(row, source_index, run, archive_corrections, version_sources)
            compare_summary({key: value for key, value in row.items() if key != "source_versions"},
                            canonical_nav(expected_row), "normalized:" + code + ":" + row["date"])
            all_normalized[key] = expected_row
        semantic_observations[code] = [{"code": code, "date": row["date"], "nav": float(row["nav"]),
                                       "cumulative_nav": float(row["cumulative_nav"]),
                                       "distribution_per_share": float(row["distribution_per_share"])}
                                      for row in expected]
        returns, reinvested, return_differences = [], [Decimal(1)], []
        for previous, current in zip(expected, expected[1:]):
            daily = (current["nav"] + current["distribution_per_share"]) / previous["nav"] - 1
            implied = current["cumulative_nav"] - previous["cumulative_nav"] - (current["nav"] - previous["nav"])
            require(abs(implied - current["distribution_per_share"]) <= Decimal("0.00025"),
                    "Unexplained cumulative NAV event")
            quoted = current["provider_daily_return"]
            tolerance = ((Decimal("0.0001") + abs(1 + (quoted or 0)) * Decimal("0.0001"))
                         / previous["nav"] + Decimal("0.0001"))
            require(quoted is None or abs(daily - quoted) <= tolerance, "Provider return mismatch")
            return_differences.append(abs(daily - quoted) if quoted is not None else Decimal(0))
            returns.append(daily)
            reinvested.append(reinvested[-1] * (1 + daily))
        verified_dividends = [canonical_nav(row) for row in expected if row["distribution_per_share"] > 0]
        expected_raw = {"code": code, "name_observed_today": observed_name, "rows": len(expected),
                        "start": expected[0]["date"], "end": expected[-1]["date"],
                        "source_total_history_rows": len(raw), "dividends": verified_dividends,
                        "dividend_count": len(verified_dividends),
                        "max_daily_return_difference": float(max(return_differences, default=Decimal(0)))}
        compare_summary(raw_summaries[code], expected_raw, "raw_summaries:" + code)
        xs, ys = tables(run, "features", code), tables(run, "labels", code)
        expected_indices = range(lookback, len(expected))
        require(len(xs) == len(expected_indices), "Feature coverage mismatch")
        expected_ids = [code + ":" + expected[i]["date"] for i in expected_indices]
        require([row.get("id") for row in xs] == expected_ids, "Feature identity/order mismatch")
        for i, row in zip(expected_indices, xs):
            verify_feature(row, expected, i, reinvested, returns, lookback)
        wanted_labels = {ident + ":" + str(h): (i, h)
                         for ident, i in zip(expected_ids, expected_indices) for h in horizons}
        require(len(ys) == len(wanted_labels), "Label coverage mismatch")
        seen = set()
        days = [dt.date.fromisoformat(row["date"]) for row in expected]
        horizon_summary = {str(h): {"mature_market": 0, "pending": 0,
                                   "qualified_historical_investor_net": 0,
                                   "max_alignment_delay_calendar_days": None} for h in horizons}
        for row in ys:
            ident = row.get("id")
            require(ident in wanted_labels and ident not in seen, "Duplicate or unknown label identity")
            seen.add(ident)
            i, horizon = wanted_labels[ident]
            mature = verify_label(row, expected, days, i, horizon)
            require(dt.datetime.fromisoformat(row["observed_by_this_research_run_at"]) >= retrieved_at,
                    "Label observed before research inputs were acquired")
            key = "mature_market_labels" if mature else "pending_market_labels"
            totals[key] += 1
            by_horizon[str(horizon)][key] += 1
            per_horizon = horizon_summary[str(horizon)]
            per_horizon["mature_market" if mature else "pending"] += 1
            if mature:
                target = days[i] + dt.timedelta(days=horizon)
                alignment = (days[bisect.bisect_left(days, target)] - target).days
                previous = per_horizon["max_alignment_delay_calendar_days"]
                per_horizon["max_alignment_delay_calendar_days"] = alignment if previous is None else max(previous, alignment)
        identity = policy["code_info"][code]
        expected_summary = {"code": code, "fund_group_id": identity.get("fund_group_id"),
                            "common_exposure": identity.get("common_exposure"), "feature_rows": len(xs),
                            "warmup_rows_excluded": min(lookback, len(expected)),
                            "first_feature_date": expected[lookback]["date"] if xs else None,
                            "last_feature_date": expected[-1]["date"] if xs else None,
                            "horizons": horizon_summary,
                            "status": "generated" if xs else "insufficient_history"}
        compare_summary(fund_summaries[code], expected_summary, "funds:" + code)
        totals["NAV_rows"] += len(expected)
        totals["feature_rows"] += len(xs)
        totals["label_rows"] += len(ys)
    applicable, excluded = [], []
    for anchor in registry["anchors"]:
        key = (anchor.get("code"), anchor.get("date"))
        if key in all_normalized and anchor.get("source_id") in matched:
            applicable.append(anchor)
        elif anchor.get("code") in codes and start.isoformat() <= anchor.get("date", "") <= end.isoformat():
            excluded.append({"code": key[0], "date": key[1], "source_id": anchor.get("source_id"),
                             "reason": "source_not_reviewed" if anchor.get("source_id") not in matched
                             else "no_exact_NAV_observation"})
    require(used["anchors"] == applicable, "Selected official anchor inventory mismatch")
    for anchor in applicable:
        near(all_normalized[(anchor["code"], anchor["date"])]["nav"],
             number(anchor.get("nav"), "official NAV anchor"), "official NAV anchor", issuer_nav.rounding_tolerance(anchor))
    applicable_corrections = {(item.get("code"), item.get("date")) for item in registry["corrections"]
                             if (item.get("code"), item.get("date")) in all_normalized
                             and item.get("source_id") in matched}
    require(set(corrections) == applicable_corrections, "Selected correction inventory mismatch")
    if manifest.get("semantic_fingerprint") is not None:
        semantic = {"policy_version": policy["policy_version"], "codes": codes, "horizons": horizons,
                    "lookback": lookback, "code_info": policy["code_info"],
                    "executors": manifest["executor_hashes"], "observations": semantic_observations}
        canonical = canonical_bytes(semantic)
        require(hashlib.sha256(canonical).hexdigest() == manifest["semantic_fingerprint"],
                "Semantic data fingerprint mismatch")
    return {"totals": totals, "by_horizon_days": by_horizon,
            "official_NAV_anchors": {"passed": len(applicable), "NAV_rows": totals["NAV_rows"],
                                     "coverage_is_partial": True,
                                     "format_gaps": read_json(run / "issuer-nav-evidence.json")["gaps"],
                                     "excluded_in_scope": excluded,
                                     "codes_without_anchors": [code for code in codes
                                                               if not any(a["code"] == code for a in applicable)]}}


def verify_nav_versions(row, sources, run, archive_corrections=None, version_sources=None):
    """Re-read original capture bytes; qualification flags are not evidence."""
    from research_data import source_version_available
    versions = row["source_versions"]
    require(isinstance(versions, list) and versions, "Missing NAV source versions")
    version_sources = {} if version_sources is None else version_sources
    ids = set()
    for version in versions:
        require(version.get("code") == row["code"] and version.get("date") == row["date"], "NAV version identity mismatch")
        original = {key: value for key, value in version.items() if key != "version_id"}
        require(version.get("version_id") == fingerprint(original) and version["version_id"] not in ids,
                "NAV source version identity/hash mismatch")
        ids.add(version["version_id"])
        reference = version.get("raw_ref", {})
        source = sources.get(reference.get("source_id"))
        require(source is not None and reference.get("sha256") == source.get("sha256"), "NAV version raw source mismatch")
        require(version.get("observed_at") == source["retrieved_at"]
                and version.get("available_at") == source.get("available_at")
                and version.get("availability_evidence") == source.get("availability_evidence"), "NAV source version times differ from original capture")
        source_version_available(version)
        if source.get("available_at") is not None:
            require("archive_origin" in source, "NAV availability lacks its original sealed archive")
            if archive_corrections is None:
                archive_corrections = {source["source_id"]: verify_nav_archive(
                    source, sources, run, read_json(run / "policy.json"))}
        if source["source_id"] not in version_sources:
            text = source_bytes(run, source).decode(source.get("encoding", "utf-8"))
            points = {}
            for point in json_array(text, "Data_netWorthTrend"):
                day = dt.datetime.fromtimestamp(int(point["x"])/1000, CN).date().isoformat()
                points.setdefault(day, []).append(point)
            accumulated = {int(stamp): value for stamp, value in json_array(text, "Data_ACWorthTrend")}
            version_sources[source["source_id"]] = (json_assignment(text, "fS_code"), points, accumulated)
        code, points, accumulated = version_sources[source["source_id"]]
        require(code == row["code"], "NAV archive fund identity mismatch")
        matches = points.get(row["date"], [])
        require(len(matches) == 1, "NAV archive date is missing or duplicated")
        point = matches[0]
        near(version["nav"], number(point["y"], "original NAV"), "version NAV")
        near(version["cumulative_nav"], number(accumulated[int(point["x"])], "original cumulative NAV"), "version cumulative NAV")
        require(number(version["nav"], "version NAV") > 0
                and number(version["cumulative_nav"], "version cumulative NAV") > 0
                and number(version["distribution_per_share"], "version cash") >= 0, "NAV source version financial domain")
        if "distribution_text" in version:
            require(version["distribution_text"] == point.get("unitMoney", ""), "NAV version original distribution text differs")
        if "provider_daily_return" in version:
            quoted = point.get("equityReturn")
            if quoted is None:
                require(version["provider_daily_return"] is None, "Invented vintage provider return")
            else:
                near(version["provider_daily_return"], number(quoted, "original provider return")/100, "vintage provider return")
        if "historical_published_at" in version:
            require(version["historical_published_at"] is None, "Archived capture does not prove an original publication timestamp")
        amount = distribution(point.get("unitMoney", ""))
        correction = (archive_corrections or {}).get(source["source_id"], {}).get(row["date"])
        if "archive_origin" in source:
            if correction:
                official = number(correction["distribution_per_share"], "original action cash")
                require(amount in (Decimal(0), official), "Original NAV/action cash conflict")
                expected = {**correction, "mode": "provider_already_matches" if amount else "evidence补录"}
                compare_summary(version.get("distribution_evidence"), expected, "original NAV correction")
                near(version["distribution_per_share"], official, "archived correction cash")
            else:
                require(version.get("distribution_evidence") is None, "Unreviewed archived NAV correction")
                near(version["distribution_per_share"], amount, "archived original cash")
        elif amount != number(version["distribution_per_share"], "version cash"):
            require(version.get("distribution_evidence") == row.get("distribution_evidence"), "NAV version cash lacks independently reviewed correction")
            evidence = version["distribution_evidence"]
            supporting = [record for record in sources.values() if record.get("sha256") == evidence.get("source_sha256")]
            require(supporting and any(dt.datetime.fromisoformat(record["retrieved_at"]) <= dt.datetime.fromisoformat(version["observed_at"]) for record in supporting),
                    "Cash source correction was not known at this NAV capture")
        require(dt.date.fromisoformat(row["date"]) <= dt.datetime.fromisoformat(version["observed_at"]).astimezone(CN).date(), "NAV version observes a future economic date")


def verify_feature(row, observations, i, reinvested, returns, lookback):
    cutoff = observations[i]["date"]
    require(row.get("feature_cutoff_nav_date") == cutoff and row.get("known_max_nav_date") == cutoff
            and row.get("feature_window_start_date") == observations[i - lookback]["date"],
            "Feature date boundary mismatch")
    require(row.get("feature_version") == "market-nav-baseline-1"
            and row.get("strict_PIT_verified") is False
            and row.get("historical_max_source_available_at") is None
            and row.get("use_scope") == "ex_post_market_research_only",
            "Feature scope/provenance mismatch")
    require(row.get("missing_flags") == BLOCKERS
            and row.get("excluded_features") == ["current_manager", "current_fees", "current_company_profile", "news"],
            "Feature missingness policy mismatch")
    values = row.get("values")
    expected = {f"momentum_{n}_nav_observations": reinvested[i] / reinvested[i - n] - 1
                for n in (20, 60, 120)}
    window = returns[i - 60:i]
    mean = sum(window) / len(window)
    variance = sum((value - mean) ** 2 for value in window) / (len(window) - 1)
    expected["volatility_60_nav_observations_annualized_252"] = (variance * 252).sqrt()
    expected["drawdown_60_nav_intervals"] = max_drawdown(reinvested[i - 60:i + 1])
    require(isinstance(values, dict) and set(values) == set(expected), "Feature field inventory mismatch")
    for name, value in expected.items():
        near(values[name], value, row["id"] + ":" + name)


def summary_index(records, codes, name):
    require(isinstance(records, list) and len(records) == len(codes), "Missing or incomplete " + name)
    require(all(isinstance(row, dict) for row in records)
            and [row.get("code") for row in records] == codes, "Summary fund identity/order mismatch: " + name)
    return {row["code"]: row for row in records}


def canonical_nav(observation):
    result = {field: observation[field] for field in ("code", "date", "distribution_text", "historical_published_at")}
    result.update({field: float(observation[field]) for field in ("nav", "cumulative_nav", "distribution_per_share")})
    quoted = observation["provider_daily_return"]
    result["provider_daily_return"] = None if quoted is None else float(quoted)
    if observation["correction"]:
        evidence = dict(observation["correction"])
        evidence["distribution_per_share"] = float(number(evidence["distribution_per_share"], "issuer distribution"))
        evidence["mode"] = ("provider_already_matches" if observation["original_distribution"] == observation["distribution_per_share"]
                            else "evidence补录")
        result["distribution_evidence"] = evidence
    return result


def compare_summary(actual, expected, context):
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), "Summary field inventory mismatch: " + context)
        for field, value in expected.items():
            compare_summary(actual[field], value, context + ":" + field)
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), "Summary array coverage mismatch: " + context)
        for i, value in enumerate(expected):
            compare_summary(actual[i], value, context + ":" + str(i))
    elif isinstance(expected, float):
        near(actual, Decimal(str(expected)), "Summary " + context)
    else:
        require(type(actual) is type(expected) and actual == expected, "Summary value mismatch: " + context)


def verify_label(row, observations, days, i, horizon):
    start = days[i]
    target = start + dt.timedelta(days=horizon)
    require(row.get("feature_id") == row["code"] + ":" + start.isoformat()
            and row.get("start_nav_date") == start.isoformat() and row.get("target_date") == target.isoformat()
            and row.get("horizon_calendar_days") == horizon
            and row.get("horizon_origin") == "feature_cutoff_nav_date"
            and row.get("label_kind") == "cash_distribution_entitlement_market_NAV", "Label scope/date mismatch")
    for field in ("historical_label_available_at", "actual_investor_net_return", "fee_rule_id", "entry_order_at", "cash_available_at"):
        require(field in row and row[field] is None, "Missing or invented historical investor field: " + field)
    require(row.get("strict_investor_eligibility") is False and row.get("missing_flags") == BLOCKERS,
            "Label investor eligibility mismatch")
    observed_at = dt.datetime.fromisoformat(row.get("observed_by_this_research_run_at", ""))
    require(observed_at.tzinfo is not None, "Label observation time lacks timezone")
    j = bisect.bisect_left(days, target)
    if j == len(days):
        require(row.get("status") == "pending_future_NAV", "Incorrect pending label status")
        require(all(row.get(field) is None for field in OUTCOMES + ("end_nav_date", "alignment_delay_calendar_days")),
                "Pending label contains invented outcome")
        return False
    require(row.get("status") == "mature_market_label" and row.get("end_nav_date") == days[j].isoformat()
            and row.get("alignment_delay_calendar_days") == (days[j] - target).days, "Mature label date/alignment mismatch")
    require(observed_at.astimezone(CN).date() >= days[j], "Outcome observed before terminal NAV date")
    shares = Decimal(1) / observations[i]["nav"]
    cash, wealth = Decimal(0), [Decimal(1)]
    for current in observations[i + 1:j + 1]:
        cash += shares * current["distribution_per_share"]
        wealth.append(shares * current["nav"] + cash)
    ret = wealth[-1] - 1
    expected = {"market_return": ret, "terminal_loss": max(-ret, Decimal(0)),
                "principal_path_loss": max(1 - min(wealth), Decimal(0)),
                "peak_drawdown": max_drawdown(wealth)}
    for name, value in expected.items():
        near(row.get(name), value, row["id"] + ":" + name)
    return True
