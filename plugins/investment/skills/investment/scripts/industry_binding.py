"""Original measured holdings bridge sector forecasts into unchanged fund data."""
import copy
from decimal import Decimal
from zoneinfo import ZoneInfo

from contracts import EvidenceError, fingerprint, instant, require
import fund_universe
import industry_data
import source_documents
import asset_domains

BASE_FIELDS = {"nav", "features", "code_info"}
INDUSTRY_FIELDS = {"industry", "industry_exposures", "industry_data_ref"}


def _identity_exposures(identities, sectors, artifacts, at):
    output = {code: [] for code in identities}
    for code, identity in identities.items():
        disclosure = identity.get("sector_exposures") or {}
        if disclosure.get("status") != "measured_disclosure":
            continue
        refs = disclosure.get("evidence_refs")
        require(type(refs) is list and refs, "Measured exposure lacks original document evidence")
        documents = {}
        for reference in refs:
            require(type(reference) is dict and reference.get("document_ref") and reference.get("locator"),
                    "Measured exposure needs original source document and locator")
            documents.setdefault(fingerprint(reference["document_ref"]), []).append(reference)
        matched, capture_times = False, []
        for references in documents.values():
            document_ref = references[0]["document_ref"]
            document = source_documents.read_extracted_document(document_ref, artifacts)
            require(instant(document["retrieved_at"]) <= instant(at), "Fund exposure source was captured after decision")
            require(all(row["document_id"] == document["document_id"] and row["raw_sha256"] == document["raw_sha256"]
                        for row in references), "Fund exposure evidence identity differs")
            mapping = {"code": code, "source_id": document["source_id"], "url": document["capture"]["requested_url"],
                       "role": "holdings", "locators": list(dict.fromkeys(row["locator"] for row in references))}
            rebuilt = fund_universe._apply_issuer(document_ref, mapping, identity, artifacts)["sector_exposures"]
            require(all(rebuilt[key] == disclosure[key] for key in
                        ("status", "disclosed_as_of", "weights", "coverage_weight", "unmapped_weight")),
                    "Fund exposure weights or disclosure date differ from original holdings table")
            matched = True
            capture_times.append(document["retrieved_at"])
        require(matched, "No source-reproducible measured exposure")
        known = max(capture_times, key=instant)
        require(disclosure["disclosed_as_of"] <= instant(at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                "Fund exposure disclosure date is after decision")
        for source_label, raw_weight in sorted(disclosure["weights"].items()):
            weight = Decimal(raw_weight)
            require(weight.is_finite() and 0 <= weight <= 1, "Source exposure weight is invalid")
            if weight == 0:
                continue
            for sector in sectors:
                relation = sector.get("sector_relation", {})
                if relation.get("status") != "verified" or not relation.get("known_at") or instant(relation["known_at"]) > instant(at):
                    continue
                domain = sector["asset_domain"]
                maps = domain.get("taxonomy", {}).get("mappings", [])
                matched = [row for row in maps if row["source_label"] == source_label and row["entity_id"] == sector["sector_id"]]
                if not matched:
                    continue
                role = domain["role"]
                if not any(row["role"] == role for row in matched):
                    continue
                proof_known = max((known, relation["known_at"]), key=instant)
                output[code].append({"sector_id": sector["sector_id"], "weight": raw_weight, "role": role,
                    "source_label": source_label, "source_taxonomy": domain["taxonomy"],
                    "as_of_date": disclosure["disclosed_as_of"], "known_at": proof_known,
                    "evidence_refs": refs+[relation["document_ref"]], "basis": "historical_disclosure",
                    "actual_exposure_status": "historical_disclosed_not_current", "actual_weight": raw_weight,
                    "stale": disclosure["disclosed_as_of"] < instant(at).date().isoformat()})
    return output


def build_exposures(identities, sector_bundle, artifacts, *, store=None, at, nav=None, features=None, policy=None, order_time_local="00:00:00"):
    """Source-versioned taxonomy plus optional past-only learned NAV style."""
    require(type(identities) is dict, "Source fund identities must be keyed by code")
    sectors = sector_bundle["sectors"]
    output = _identity_exposures(identities, sectors, artifacts, at)
    if store is not None:
        for _, discovery in store.scan("fund_discovery"):
            if not discovery.get("identity_snapshot_ref") or instant(discovery["observed_at"]) > instant(at):
                continue
            snapshot = artifacts.read_json(discovery["identity_snapshot_ref"])
            archived = fund_universe.verify_identity(snapshot, artifacts, as_of=discovery["observed_at"])
            historical = _identity_exposures({code: value for code, value in archived.items() if code in identities},
                                            sectors, artifacts, discovery["observed_at"])
            for code, rows in historical.items():
                output[code].extend(rows)
    if nav is not None and features is not None and policy is not None:
        import asset_exposure
        for code in identities:
            origins = [row["feature_cutoff_nav_date"] for row in features.get(code, []) if row["feature_cutoff_nav_date"] <= instant(at).date().isoformat()]
            origins.append(instant(at).date().isoformat())
            output[code].extend(asset_exposure.estimate(code, nav.get(code, []), origins, sectors, policy, order_time_local))
    for code, rows in output.items():
        output[code] = sorted({fingerprint(row): row for row in rows}.values(),
                              key=lambda row: (row["as_of_date"], instant(row["known_at"]), row["sector_id"]))
    return output


derive_exposures = build_exposures


def family_industry_scope(data, codes, decision_at):
    """Select all genuinely known sectors while preserving the full audit hash."""
    day = instant(decision_at).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    historical, current, missing = set(), set(), []
    for code in codes:
        known = [row for row in data["industry_exposures"].get(code, []) if row["as_of_date"] <= day
                 and instant(row["known_at"]) <= instant(decision_at)
                 and (row.get("basis") == "model_estimate" or Decimal(row["weight"]) > 0)]
        historical.update(row["sector_id"] for row in known)
        if not known:
            missing.append(code)
            continue
        latest = max((row["as_of_date"], row["known_at"]) for row in known)
        current.update(row["sector_id"] for row in known if (row["as_of_date"], row["known_at"]) == latest)
    original = data["industry"]
    scoped = copy.deepcopy(original)
    scoped["sectors"] = [row for row in scoped["sectors"] if row["sector_id"] in historical]
    scoped["benchmarks"] = [row for row in scoped.get("benchmarks", []) if row["sector_id"] in historical]
    theses = {key for row in scoped["sectors"] for key in row.get("thesis_ids", [])}
    scoped["required_actions"] = [row for row in scoped.get("required_actions", [])
        if (row.get("sector_id") in current if row.get("sector_id") is not None else
            row.get("thesis_id") in theses if row.get("thesis_id") is not None else True)]
    scoped["source_scope"] = {"full_bundle_hash": fingerprint(original), "fund_codes": sorted(codes),
        "historical_sector_ids": sorted(historical), "required_current_sector_ids": sorted(current)}
    scoped["status"] = "partial" if scoped["required_actions"] else "ready"
    return {"industry": scoped, "required_current_sector_ids": sorted(current),
            "missing_exposure_codes": missing, "scope_hash": fingerprint(scoped)}


def _augmented(base_data, sector_value, record_ref, artifacts, *, store, at, spec):
    require(type(base_data) is dict and set(base_data) == BASE_FIELDS, "Only the audited base three data fields may be augmented")
    sector = industry_data.validate_sector_data(sector_value, spec, at, artifacts, store=store)
    data = copy.deepcopy(base_data)
    data.update(industry=sector, industry_exposures=build_exposures(base_data["code_info"], sector, artifacts, store=store, at=at, nav=base_data["nav"], features=base_data["features"],
                    policy=spec.get("industry", {}).get("training"), order_time_local=instant(at).astimezone(ZoneInfo("Asia/Shanghai")).time().replace(tzinfo=None).isoformat()),
                industry_data_ref={"record_ref": record_ref, "bundle_hash": sector_value["bundle_hash"],
                                   "request_hash": sector_value["request_hash"]})
    return data


def bind_numerical_data(base_data, sector_value, artifacts, *, store, at, spec):
    return _augmented(base_data, sector_value, artifacts.put_json(sector_value), artifacts, store=store, at=at, spec=spec)


def validate_model_data(value, inputs, store, artifacts):
    """Read-only reconstruction of both source adapters and all unchanged fields."""
    base = artifacts.read_json(inputs["base_data_ref"])
    sector_value = artifacts.read_json(inputs["industry_record_ref"])
    expected = _augmented(base, sector_value, inputs["industry_record_ref"], artifacts,
                          store=store, at=inputs["decision_at"], spec=inputs["spec"])
    import asset_exposure
    asset_exposure.validate_estimates(value["industry_exposures"], base["nav"], value["industry"]["sectors"])
    require(set(value) == BASE_FIELDS|INDUSTRY_FIELDS and value == expected,
            "Model data differs from independent sector/holdings source reconstruction")
    return {"status": "passed", "base_data_hash": fingerprint(base), "model_data_hash": fingerprint(value),
            "industry_bundle_hash": sector_value["bundle_hash"], "industry_exposures_hash": fingerprint(expected["industry_exposures"])}
