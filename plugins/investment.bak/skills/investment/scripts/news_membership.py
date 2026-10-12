"""Accepted exact original-version metadata membership; themes are not facts.

Production and independent verification scan the complete original domain.
Consumers authenticate the accepted source operation and current CAS bytes,
then read the small exact-version index without rebuilding domain semantics.
"""
from contracts import fields, require

SCHEMA = "original_material_query_membership_v1"
SCOPE = "exact_captured_version_original_metadata_themes_not_news_fact_or_research_sufficiency"


def _retrieval(payload, store, artifacts, reference=None):
    from stage_validation import validated_source_operation
    identity = payload["workflow_id"] + ":retrieval"
    value = validated_source_operation(identity, "retrieval_prepare", store, artifacts)
    operation = store.operation(identity)
    require(value["workflow_id"] == payload["workflow_id"]
            and value["retrieval_id"] == identity,
            "Material metadata belongs to another exact retrieval workflow")
    if reference is None:
        reference = artifacts.put_json(operation["result"])
    else:
        require(artifacts.read_json(reference) == operation["result"],
                "Material metadata retrieval CAS differs from its canonical accepted result")
    return identity, value, reference


def _version_urls(manifest):
    versions = {row["version_id"] for row in manifest["versions"]}
    require(len(versions) == len(manifest["versions"]), "Material metadata has duplicate original versions")
    urls = {identity: set() for identity in versions}
    for row in manifest["retrievals"]:
        if row.get("state") == "body" and row.get("version_id") in versions:
            urls[row["version_id"]].update((row["requested_url"], row["final_url"]))
    require(all(urls.values()), "Material metadata version lacks its actual original body capture")
    return urls


def _header(payload, manifest, manifest_ref, identity, retrieval, retrieval_ref):
    require(payload["run_id"] == payload["workflow_id"] + ":analysis"
            and manifest["run_id"] == payload["run_id"]
            and manifest["collection_id"] == payload["collection_id"],
            "Material metadata run or collection changed")
    return {"schema_id": SCHEMA, **{key: payload[key] for key in ("workflow_id", "run_id", "collection_id")},
        "collection_manifest_ref": manifest_ref, "retrieval_operation_id": identity,
        "retrieval_result_ref": retrieval_ref,
        "original_domain_ref": retrieval["original_domain_ref"],
        "opportunity_domain_ref": retrieval["complete_opportunities_ref"],
        "complement_ref": retrieval["unselected_urls_ref"], "scope": SCOPE}


def build(payload, manifest, manifest_ref, store, artifacts, *, retrieval_result_ref=None):
    """Derive complete memberships only from exact captured URLs and original rows."""
    identity, retrieval, retrieval_ref = _retrieval(payload, store, artifacts, retrieval_result_ref)
    urls = _version_urls(manifest)
    by_url = {}
    for version_id, values in urls.items():
        for url in values:
            by_url.setdefault(url, set()).add(version_id)
    memberships = {version_id: set() for version_id in urls}
    domain = artifacts.read_json(retrieval["original_domain_ref"])
    for part in domain["parsed_parts"]:
        for row in artifacts.read_json(part["parsed_ref"])["records"]:
            for version_id in by_url.get(row["https_probe_url"], ()):
                memberships[version_id].update(row["themes"] or [None])
    return {**_header(payload, manifest, manifest_ref, identity, retrieval, retrieval_ref),
        "version_themes": {identity: sorted(themes, key=lambda theme: (theme is not None, theme or ""))
                           for identity, themes in sorted(memberships.items())}}


def validate(reference, payload, manifest, manifest_ref, store, artifacts):
    """Independently rescan all original metadata; no producer or cached verdict."""
    actual = artifacts.read_json(reference)
    require(actual == build(payload, manifest, manifest_ref, store, artifacts,
                           retrieval_result_ref=actual["retrieval_result_ref"]),
            "Material query membership differs from independently rebuilt exact original versions")
    return {"status": "passed", "scope": SCOPE,
        "checks": ["complete_original_metadata_scan", "actual_capture_requested_and_final_urls",
                   "complete_original_version_inventory", "exact_version_themes", "original_opportunity_complement_retained"],
        "trade_ready": False}


def validated_memberships(material_operation_id, store, artifacts, *, collection_manifest_ref,
                          retrieval_operation_id, retrieval_result_ref=None):
    """Read only accepted memberships; authenticate actual byte identities anew."""
    from stage_validation import validated_source_operation
    material = validated_source_operation(material_operation_id, "material_prepare", store, artifacts)
    operation = store.operation(material_operation_id)
    payload = operation["request"]["payload"]
    require(material["collection_manifest_ref"] == collection_manifest_ref,
            "Accepted material metadata belongs to another source collection")
    value = artifacts.read_json(material["query_membership_ref"])
    expected_id, retrieval, accepted_retrieval_ref = _retrieval(payload, store, artifacts, value["retrieval_result_ref"])
    require(expected_id == retrieval_operation_id
            and (retrieval_result_ref is None or retrieval_result_ref == accepted_retrieval_ref),
            "Accepted material metadata belongs to another exact retrieval operation or result")
    index = artifacts.read_json(material["material_index_ref"])
    require(index["query_membership_ref"] == material["query_membership_ref"],
            "Accepted material index substituted its query membership reference")
    manifest = artifacts.read_json(collection_manifest_ref)
    header = _header(payload, manifest, collection_manifest_ref, expected_id, retrieval, accepted_retrieval_ref)
    fields(value, set(header) | {"version_themes"}, label="accepted original metadata membership")
    require(all(value[key] == item for key, item in header.items()),
            "Accepted material metadata index changed its exact source binding")
    memberships = value["version_themes"]
    require(type(memberships) is dict and set(memberships) == set(_version_urls(manifest)),
            "Accepted material metadata omitted or inserted an original version")
    for themes in memberships.values():
        require(type(themes) is list and all(theme is None or type(theme) is str and theme for theme in themes)
                and len(themes) == len(set(themes))
                and themes == sorted(themes, key=lambda theme: (theme is not None, theme or "")),
                "Accepted material metadata themes are not exact distinct original identities")
    return {identity: set(themes) for identity, themes in memberships.items()}
