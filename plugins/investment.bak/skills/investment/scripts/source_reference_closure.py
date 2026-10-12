"""Complete source CAS closures; consumers still read current bytes every call.

A candidate traversal grants no authority. The source operation gate must
authenticate its exact independently accepted inventory before exposing it.
"""
from contracts import fields, fingerprint, require, strict_json_loads

SCHEMA = "source_reference_closure_v1"
SCOPE = "complete_accepted_source_CAS_graph_not_fact_PIT_model_or_trade_qualification"
ROOTS = {"publisher_result_ref", "retrieval_result_ref", "collection_manifest_ref",
         "query_membership_ref", "material_index_ref"}


def is_reference(value):
    return type(value) is dict and {"sha256", "size"} <= value.keys() and ("path" in value or "chunks" in value)


def material_roots(payload, result, store, artifacts, *, recorded_roots=None):
    """Bind this material's forest to actual canonical preceding operations."""
    workflow_id = payload["workflow_id"]
    publisher = store.operation(workflow_id + ":publishers")
    retrieval = store.operation(workflow_id + ":retrieval")
    require(publisher is not None and publisher["status"] == "completed"
            and publisher["request"]["operation"] == "publisher_discover"
            and retrieval is not None and retrieval["status"] == "completed"
            and retrieval["request"]["operation"] == "retrieval_prepare",
            "Source closure needs its exact completed original publisher and retrieval")
    membership = artifacts.read_json(result["query_membership_ref"])
    require(membership["retrieval_operation_id"] == retrieval["id"]
            and artifacts.read_json(membership["retrieval_result_ref"]) == retrieval["result"],
            "Source closure retrieval differs from this material's canonical prior source")
    if recorded_roots is None:
        publisher_ref = artifacts.put_json(publisher["result"])
    else:
        fields(recorded_roots, ROOTS, label="material source closure roots")
        publisher_ref = recorded_roots["publisher_result_ref"]
        require(artifacts.read_json(publisher_ref) == publisher["result"],
                "Source closure publisher differs from its exact canonical prior source")
    return {"publisher_result_ref": publisher_ref,
        "retrieval_result_ref": membership["retrieval_result_ref"],
        **{key: result[key] for key in ("collection_manifest_ref", "query_membership_ref", "material_index_ref")}}


def build(roots, artifacts):
    """Fully parse the complete forest, matching the ordinary reference traversal."""
    fields(roots, ROOTS, label="source closure roots")
    references, active = {}, set()

    def walk(value):
        if type(value) is dict:
            if is_reference(value):
                key = fingerprint(value)
                if key in references or key in active:
                    return
                active.add(key)
                raw = artifacts.read(value)
                if value.get("media_type") == "application/json":
                    walk(strict_json_loads(raw.decode("utf-8")))
                references[key] = value
                active.remove(key)
            else:
                for child in value.values():
                    walk(child)
        elif type(value) is list:
            for child in value:
                walk(child)
    walk(roots)
    return {"schema_id": SCHEMA, "roots": roots,
        "references": [references[key] for key in sorted(references)], "scope": SCOPE}


def validate(reference, roots, artifacts):
    """Independent full rebuild, including every nested original CAS reference."""
    require(artifacts.read_json(reference) == build(roots, artifacts),
            "Source reference closure differs from independently rebuilt complete original graph")
    return {"status": "passed", "scope": SCOPE,
        "checks": ["complete_source_reference_forest", "full_reference_closure_independent_rebuild",
                   "original_source_opportunity_complement_and_index_bytes"], "trade_ready": False}


def candidate_checker(inventory, artifacts):
    """SHA-check every declared CAS now; caller must authenticate inventory next."""
    fields(inventory, {"schema_id", "roots", "references", "scope"}, label="source reference closure inventory")
    require(inventory["schema_id"] == SCHEMA and inventory["scope"] == SCOPE,
            "Source reference closure schema or scope changed")
    fields(inventory["roots"], ROOTS, label="source reference closure inventory roots")
    references = inventory["references"]
    require(type(references) is list and all(is_reference(row) for row in references),
            "Source reference closure must preserve its actual explicit CAS inventory")
    keys = [fingerprint(row) for row in references]
    require(keys == sorted(set(keys)) and all(fingerprint(row) in keys for row in inventory["roots"].values()),
            "Source reference closure duplicated, reordered or omitted its original roots")
    seen = set()
    for reference in references:
        # Artifacts.read validates storage chunks and the complete SHA/size;
        # no JSON decode is needed after independent closure reconstruction.
        artifacts.read(reference)
        seen.add(fingerprint(reference))
    active = set()

    def walk(value):
        if type(value) is dict:
            if is_reference(value):
                key = fingerprint(value)
                if key in seen or key in active:
                    return
                active.add(key)
                try:
                    raw = artifacts.read(value)
                    if value.get("media_type") == "application/json":
                        walk(strict_json_loads(raw.decode("utf-8")))
                    seen.add(key)
                finally:
                    active.remove(key)
            else:
                for child in value.values():
                    walk(child)
        elif type(value) is list:
            for child in value:
                walk(child)
    return walk
