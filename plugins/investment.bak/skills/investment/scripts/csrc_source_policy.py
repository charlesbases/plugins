"""Reviewed institutional CSRC dated versions with independent price checks.

Only the exact lawful EID daily endpoint is admitted. The official institution
is identified through an authenticated original CSRC regulatory PDF. The HTTP
metadata is still not independently authenticated: this protocol explicitly
relies on the institution's declared price/version/publication association.
It neither supplies a TLS flag nor invents an original capture/publication
instant. Source policy admission is not model or investor eligibility.
"""
import datetime as dt
from decimal import Decimal
import hashlib
from io import BytesIO
from pathlib import Path

from contracts import canonical_bytes, fingerprint, require
import csrc_fund_vintages as vintages
import csrc_fund_acquisition as acquisition
import source_fetch

PROTOCOL = "csrc_official_provider_declared_nav_v1"
EVIDENCE_KIND = "official_provider_declared_price_version"
AUTHORITY_SOURCE = "csrc_fund_vintage_authority"
AUTHORITY_URL = "https://www.csrc.gov.cn/csrc/c101950/c1047975/1047975/files/%E9%99%84%E4%BB%B61_%E4%B8%AD%E5%9B%BD%E8%AF%81%E7%9B%91%E4%BC%9A%E5%86%B3%E5%AE%9A%E4%BF%AE%E6%94%B9%E7%9A%84%E8%A7%84%E8%8C%83%E6%80%A7%E6%96%87%E4%BB%B6.pdf"
ASSUMPTIONS = ["official_institution_reports_this_price_version_publication_day",
               "declared_upload_and_publication_fields_follow_current_version",
               "independent_HTTPS_price_checks_do_not_authenticate_publication_metadata"]
SOURCE_RECORD_FIELDS = {"path", "source_id", "sha256", "text_sha256", "source_protocol", "authority_source_id",
                        "expected_sha256", "review_status", "requested_codes"}


def capture_metadata(source):
    return {field: value for field, value in source.items() if field not in SOURCE_RECORD_FIELDS}


def authority(raw, source, registry=None):
    """Re-read official original authority; an arbitrary HTTPS URL is insufficient."""
    source_fetch.validate_capture_provenance(source, registry)
    require(source["registry_source_id"] == AUTHORITY_SOURCE
            and source["requested_url"] == source["final_url"] == AUTHORITY_URL
            and source["redirect_chain"] == [], "Official provider identity authority differs")
    require(type(raw) is bytes and len(raw) == source["bytes"]
            and hashlib.sha256(raw).hexdigest() == source["raw_sha256"], "Official provider authority original changed")
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(raw), strict=True)
    require(len(reader.pages) > 120, "Official provider authority document is incomplete")
    text = "".join((reader.pages[index].extract_text() or "") for index in (118, 119, 120))
    compact = "".join(text.split())
    require("http://eid.csrc.gov.cn/fund" in compact and "中国证监会基金电子披露" in compact
            and "中证信息技术服务有限责任公司" in compact and "报送" in compact,
            "Original official document does not identify this provider/reporting contract")
    return {"source_id": source["source_id"], "raw_sha256": source["raw_sha256"],
            "location": "official_regulatory_pdf_pages_119_120_121", "publisher_identity_verified": True,
            "transport_authenticated_for_authority": True}


def expected_available(record, observed_at):
    publication = dt.date.fromisoformat(record["source_publication_date"])
    day_bound = dt.datetime.combine(publication + dt.timedelta(days=1), dt.time(), vintages.SHANGHAI)
    observed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    require(observed.utcoffset() is not None, "Actual provider receipt needs an aware timestamp")
    uploaded = dt.datetime.strptime(record["source_uploaded_at_literal"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=vintages.SHANGHAI)
    require(dt.date.fromisoformat(record["valuation_date"]) <= uploaded.date() <= publication
            and uploaded <= observed and publication <= observed.astimezone(vintages.SHANGHAI).date(),
            "Current provider price version does not support its declared publication day")
    return min(day_bound, observed).isoformat()


def _source_currency(record):
    """An explicit inconsistent/unknown currency cannot pass a CNY check.

    The parent fund's empty currency is still unknown. Matching its current
    share price does not manufacture historical currency continuity.
    """
    import fund_universe
    literal = record.get("source_raise_currency_literal")
    require(literal in (None, "") or fund_universe._currency(literal) == "CNY",
            "Original provider currency declaration conflicts with or does not establish CNY")


def admitted_version(record, source, corroboration, identity, action_source, actions, authority_proof):
    """Only exact independent price matches and a complete zero-cash census pass.

    Nonzero/unknown cash rights require their own original version protocol.
    They are deliberately not assigned the NAV provider's publication day.
    """
    code, day = record["fund_share_code"], record["valuation_date"]
    require(identity.get("code") == code and identity.get("currency") == "CNY"
            and identity.get("execution_venue") == "off_exchange_nav", "Original CNY off-exchange share identity required")
    _source_currency(record)
    require(corroboration["code"] == code and corroboration["date"] == day
            and Decimal(str(corroboration["nav"])) == Decimal(record["unit_nav"])
            and Decimal(str(corroboration["cumulative_nav"])) == Decimal(record["cumulative_nav"]),
            "Current HTTPS source price/date/class differs from provider-declared version")
    require(not actions["dividends"] and not actions["splits"]
            and Decimal(str(corroboration["distribution_per_share"])) == 0,
            "Original provider NAV protocol cannot grant historical dividend/split rights")
    available = expected_available(record, source["retrieved_at"])
    cash = {"kind": "complete_current_distributor_historical_zero_action_census",
            "source_id": action_source["source_id"], "raw_sha256": action_source["raw_sha256"],
            "historical_nonzero_rights_granted": False}
    evidence = {"kind": EVIDENCE_KIND, "protocol": PROTOCOL, "source_id": source["source_id"],
        "raw_sha256": source["raw_sha256"], "source_row_index": record["source_row_index"],
        "provider_record": {key: record[key] for key in ("fund_share_code", "parent_fund_code", "parent_fund_entity_id",
            "price_row_entity_id", "filing_detail_entity_id", "source_share_classification_code", "valuation_date",
            "unit_nav", "cumulative_nav", "source_publication_date", "source_uploaded_at_literal", "source_raise_currency_literal")},
        "provider_publication_precision": "day", "source_clock_contract": "Asia/Shanghai",
        "available_by_bound": available, "actual_capture_at": source["retrieved_at"],
        "authority": authority_proof, "price_corroboration": corroboration["raw_ref"], "cash_census": cash,
        "publisher_identity_verified": True, "transport_authenticated": False,
        "publication_metadata_independently_authenticated": False, "assumptions": list(ASSUMPTIONS)}
    version = {"code": code, "date": day, "nav": float(record["unit_nav"]),
        "cumulative_nav": float(record["cumulative_nav"]), "distribution_per_share": 0.,
        "distribution_text": "", "provider_daily_return": None, "historical_published_at": None,
        "corporate_actions": [], "observed_at": source["retrieved_at"], "available_at": available,
        "raw_ref": {"source_id": source["source_id"], "sha256": source["raw_sha256"]},
        "availability_evidence": evidence}
    version["version_id"] = fingerprint(version)
    return version


def available(version):
    """Structural consumer contract; the independent audit re-reads all originals."""
    proof, raw = version.get("availability_evidence"), version.get("raw_ref")
    require(isinstance(proof, dict) and proof.get("kind") == EVIDENCE_KIND and proof.get("protocol") == PROTOCOL,
            "Unsupported institution price-version evidence")
    require(isinstance(raw, dict) and raw == {"source_id": proof["source_id"], "sha256": proof["raw_sha256"]}
            and proof["actual_capture_at"] == version["observed_at"]
            and proof["provider_publication_precision"] == "day" and proof["source_clock_contract"] == "Asia/Shanghai"
            and proof["transport_authenticated"] is False
            and proof["publication_metadata_independently_authenticated"] is False
            and proof["publisher_identity_verified"] is True and proof["assumptions"] == ASSUMPTIONS,
            "Institution source version changed its provenance or falsely authenticates HTTP")
    record = proof["provider_record"]
    _source_currency(record)
    require(version["code"] == record["fund_share_code"] and version["date"] == record["valuation_date"]
            and Decimal(str(version["nav"])) == Decimal(record["unit_nav"])
            and Decimal(str(version["cumulative_nav"])) == Decimal(record["cumulative_nav"])
            and version["distribution_per_share"] == 0 and version.get("corporate_actions") == [],
            "Provider version price/class/cash association changed")
    require(version["available_at"] == proof["available_by_bound"]
            == expected_available(record, version["observed_at"]), "Provider conservative day bound changed")
    return dt.datetime.fromisoformat(version["available_at"])


def source_record(page, archive_root, run, authority_source_id):
    """Archive actual HTTP provenance separately from the global HTTPS fetcher."""
    from file_io import read_artifact_json
    receipt = read_artifact_json(Path(archive_root) / page["receipt_path"])
    raw = (Path(archive_root) / page["raw_path"]).read_bytes()
    candidate = read_artifact_json(Path(archive_root) / page["candidate_path"])
    vintages.validate_vintages(candidate, raw, receipt, requested_codes=candidate["requested_codes"],
                               source_timezone_contract="Asia/Shanghai")
    acquisition._capture_check(receipt, acquisition.QUARANTINE)
    relative = (Path(archive_root) / page["raw_path"]).relative_to(run).as_posix()
    source_id = "CSRC-provider-" + fingerprint({"url": receipt["requested_url"], "sha256": receipt["raw_sha256"]})
    return {**receipt, "source_id": source_id, "path": relative, "sha256": receipt["raw_sha256"],
            "text_sha256": hashlib.sha256(raw.decode(receipt["encoding"]).encode("utf-8")).hexdigest(),
            "source_protocol": PROTOCOL, "authority_source_id": authority_source_id,
            "requested_codes": candidate["requested_codes"], "expected_sha256": None,
            "review_status": "institution_declared_version_requires_independent_price_and_time_contract"}


def verify_http_source(source, raw, authority_source, authority_raw, registry=None, authority_proof=None):
    require(source.get("source_protocol") == PROTOCOL, "General HTTP source cannot enter financial registry")
    proof = authority_proof if authority_proof is not None else authority(authority_raw, authority_source, registry)
    require(source["authority_source_id"] == authority_source["source_id"], "Provider authority source identity changed")
    acquisition._capture_check(source, acquisition.QUARANTINE)
    value = vintages.extract_vintages(raw, source, requested_codes=source["requested_codes"],
                                      source_timezone_contract="Asia/Shanghai")
    vintages.validate_vintages(value, raw, source, requested_codes=source["requested_codes"],
                               source_timezone_contract="Asia/Shanghai")
    return value, proof


def verify_prior_original(prior, raw, run, policy, *, cached=None):
    """Bind every conflict-check page to its original completed research seal."""
    import research_audit as audit
    from file_io import read_artifact_json
    base = Path(policy["root"]).resolve() / "plans" / policy["plan_id"]
    require(Path(run).resolve().is_relative_to(base), "Provider archive plan ownership differs")
    origin = prior["origin"]
    original_run = audit.safe_path(base, origin["run_path"])
    require(original_run.resolve() != Path(run).resolve(), "Provider conflict history must come from a prior sealed run")
    cache_key = "prior-seal:" + fingerprint(origin)
    if cached is not None and cache_key in cached:
        sealed, originals, registry, artifacts = cached[cache_key]
    else:
        manifest_path, audit_path = original_run / "manifest.json", original_run / "audit.json"
        require(audit.digest(manifest_path) == origin["manifest_sha256"]
                and audit.digest(audit_path) == origin["audit_sha256"], "Original provider archive seal changed")
        manifest, sealed = audit.read_json(manifest_path), audit.read_json(audit_path)
        require(manifest.get("plan_id") == policy["plan_id"] and manifest.get("run_id") == original_run.name
                and sealed.get("manifest_sha256") == origin["manifest_sha256"]
                and sealed.get("dataset_hash") == manifest.get("dataset_hash")
                and audit.read_json(original_run / "run-status.json").get("status") == "data_preparation_completed"
                and fingerprint(manifest["artifacts"]) == manifest["dataset_hash"],
                "Original provider archive is not a completed sealed capture")
        artifacts = {item["path"]: item for item in manifest["artifacts"]}
        def original_bytes(relative):
            path = audit.safe_path(original_run, relative)
            item = artifacts.get(relative)
            require(item is not None and path.is_file() and not path.is_symlink()
                    and path.stat().st_size == item["bytes"] and audit.digest(path) == item["sha256"],
                    "Original provider archive artifact changed: " + relative)
            return path.read_bytes()
        original_policy = audit.read_json(audit.safe_path(original_run, "policy.json"))
        original_bytes("policy.json")
        request = read_artifact_json(audit.safe_path(original_run, "request.json"))
        original_bytes("request.json")
        require(original_policy["root"] == policy["root"] and original_policy["plan_id"] == policy["plan_id"]
                and fingerprint(request) == manifest["request_hash"], "Original provider archive request ownership differs")
        original_bytes("source-manifest.json")
        originals = {source["source_id"]: source for source in audit.read_json(original_run / "source-manifest.json")}
        original_bytes("executor/source_registry.json")
        registry = audit.read_json(original_run / "executor/source_registry.json")
        if cached is not None:
            cached[cache_key] = (sealed, originals, registry, artifacts)
    matches = [source for source in originals.values() if source.get("source_protocol") == PROTOCOL
               and capture_metadata(source) == prior["capture"] and source["requested_codes"] == prior["requested_codes"]]
    require(len(matches) == 1, "Prior provider capture metadata differs from sealed original source")
    source = matches[0]
    original_path = audit.safe_path(original_run, source["path"])
    item = artifacts.get(source["path"])
    require(item is not None and original_path.is_file() and not original_path.is_symlink()
            and original_path.stat().st_size == item["bytes"] and audit.digest(original_path) == item["sha256"]
            and original_path.read_bytes() == raw, "Prior provider original page bytes changed")
    authority_source = originals[source["authority_source_id"]]
    authority_path = audit.safe_path(original_run, authority_source["path"])
    authority_item = artifacts.get(authority_source["path"])
    require(authority_item is not None and authority_path.is_file() and not authority_path.is_symlink()
            and authority_path.stat().st_size == authority_item["bytes"]
            and audit.digest(authority_path) == authority_item["sha256"], "Prior official provider authority original changed")
    authority_key = cache_key + ":authority:" + authority_source["source_id"]
    proof = None if cached is None else cached.get(authority_key)
    if proof is None:
        proof = authority(authority_path.read_bytes(), authority_source, registry)
        if cached is not None:
            cached[authority_key] = proof
    value, _ = verify_http_source(source, raw, authority_source, None, registry, proof)
    return value


def corroborate(record, sources, run, identity, *, registry=None, cached=None):
    """Re-read every HTTPS price/cash original; hashes alone are not corroboration."""
    import research_audit as audit
    import fund_universe
    code, day = record["fund_share_code"], record["valuation_date"]
    nav_source = sources.get("NAV-" + code)
    action_source = sources.get("TT-actions-" + code)
    require(nav_source is not None and action_source is not None, "Independent exact-share NAV and cash sources are missing")
    for source in (nav_source, action_source):
        source_fetch.validate_capture_provenance(source, registry)
    cache_key = "price-and-cash:" + code
    if cached is not None and cache_key in cached:
        rows, actions = cached[cache_key]
        require(day in rows, "Independent exact-share/date original NAV is missing")
        return rows[day], action_source, actions
    text = audit.source_bytes(run, nav_source).decode(nav_source["encoding"])
    require(audit.json_assignment(text, "fS_code") == code
            and fund_universe._text(audit.json_assignment(text, "fS_name")) == identity["name"],
            "Independent NAV share code/name differs from original legal identity")
    cumulative = {}
    for stamp, value in audit.json_array(text, "Data_ACWorthTrend"):
        require(int(stamp) not in cumulative, "Independent cumulative NAV date is duplicated")
        cumulative[int(stamp)] = value
    action_text = audit.source_bytes(run, action_source).decode(action_source["encoding"])
    actions = fund_universe.parse_actions(action_text, code)
    if not actions["dividends"] and not actions["splits"]:
        require("暂无分红" in action_text and "暂无拆分" in action_text,
                "Empty action tables do not prove a complete zero-action census")
    rows = {}
    for point in audit.json_array(text, "Data_netWorthTrend"):
        date = dt.datetime.fromtimestamp(int(point["x"]) / 1000, vintages.SHANGHAI).date().isoformat()
        require(date not in rows and int(point["x"]) in cumulative, "Independent exact-share NAV date is missing or duplicated")
        rows[date] = {"code": code, "date": date, "nav": point["y"], "cumulative_nav": cumulative[int(point["x"])],
            "distribution_per_share": audit.distribution(point.get("unitMoney", "")),
            "raw_ref": {"source_id": nav_source["source_id"], "sha256": nav_source["raw_sha256"]}}
    if cached is not None:
        cached[cache_key] = (rows, actions)
    require(day in rows, "Independent exact-share/date original NAV is missing")
    return rows[day], action_source, actions


def rebuild_versions(sources, run, code_info, *, registry=None, previous_versions=()):
    """Re-extract all admitted rows, retaining unsupported/mismatched rows."""
    import research_audit as audit
    result, quarantine, observed = {}, [], []
    old = list(previous_versions)
    associations = vintages.VersionAssociations()
    associations.update(old)
    cached = {}
    authorities = {}
    originals, conflicting_details = [], {}
    for source in sources.values():
        if source.get("source_protocol") != PROTOCOL:
            continue
        authority_source = sources[source["authority_source_id"]]
        aid = authority_source["source_id"]
        if aid not in authorities:
            authorities[aid] = authority(audit.source_bytes(run, authority_source), authority_source, registry)
        value, authority_proof = verify_http_source(source, audit.source_bytes(run, source), authority_source,
                                                    None, registry, authorities[aid])
        originals.append((source, value, authority_proof))
        # All unrequested original classes participate in conflict checks.
        # Finish the complete panel before admitting any row: a later page may
        # contradict a previously seen filing entity. In that case every share
        # of that filing is excluded, including the first matching price.
        for record in value["observed_versions"]:
            try:
                associations.add(record)
            except ValueError as error:
                conflicting_details[record["filing_detail_entity_id"]] = str(error)
    for source, value, authority_proof in originals:
        for record in value["observed_versions"]:
            observed.append(record)
            conflict = conflicting_details.get(record["filing_detail_entity_id"])
            if conflict is not None:
                quarantine.append({"code": record["fund_share_code"], "date": record["valuation_date"],
                                   "source_id": source["source_id"], "reason": conflict})
                continue
            code = record["fund_share_code"]
            if code not in code_info:
                continue
            try:
                price, action_source, actions = corroborate(record, sources, run, code_info[code], registry=registry, cached=cached)
                version = admitted_version(record, source, price, code_info[code], action_source, actions, authority_proof)
                available(version)
                result.setdefault((code, record["valuation_date"]), []).append(version)
            except ValueError as error:
                quarantine.append({"code": code, "date": record["valuation_date"],
                                   "source_id": source["source_id"], "reason": str(error)})
    return result, quarantine, observed


def verify_version(version, sources, run, identity, *, registry=None, cached=None):
    """Independent original read for one consumed NAV version."""
    import research_audit as audit
    source = sources[version["raw_ref"]["source_id"]]
    require(source.get("source_protocol") == PROTOCOL, "Provider price version does not have the reviewed exact source protocol")
    authority_source = sources[source["authority_source_id"]]
    key = source["source_id"]
    if cached is not None and key in cached:
        value, proof = cached[key]
    else:
        authority_key = "authority:" + authority_source["source_id"]
        if cached is not None and authority_key in cached:
            authority_proof = cached[authority_key]
        else:
            authority_proof = authority(audit.source_bytes(run, authority_source), authority_source, registry)
            if cached is not None:
                cached[authority_key] = authority_proof
        value, proof = verify_http_source(source, audit.source_bytes(run, source), authority_source,
                                          None, registry, authority_proof)
        if cached is not None:
            cached[key] = (value, proof)
    expected = version["availability_evidence"]["provider_record"]
    matches = [record for record in value["records"] if record["fund_share_code"] == version["code"]
               and record["valuation_date"] == version["date"] and record["source_row_index"] == version["availability_evidence"]["source_row_index"]]
    require(len(matches) == 1, "Consumed provider price/version entity is missing in original raw page")
    record = matches[0]
    require({field: record[field] for field in expected} == expected, "Original provider version association differs")
    price, action_source, actions = corroborate(record, sources, run, identity, registry=registry, cached=cached)
    reconstructed = admitted_version(record, source, price, identity, action_source, actions, proof)
    require(canonical_bytes(version) == canonical_bytes(reconstructed), "Consumed provider version differs from independently reconstructed originals")
    return True
