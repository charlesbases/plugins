"""Explicit engine bindings shared by the business-stage adapters."""
import copy

from artifacts import Artifacts
from contracts import fingerprint, require
from state_store import Store
from chains.contracts import make_envelope


def state(root, plan):
    store = Store(root, plan)
    return store, Artifacts(store.base)


def context(previous):
    """The downstream input is the sealed predecessor, never a latest selector."""
    return copy.deepcopy(previous["payload"].get("engine_context", {}))


def check_control(root, plan, chain_id, control_context, *, previous=None):
    from chains.protocol import validate_control
    validate_control(control_context, root=root, plan=plan, chain_id=chain_id)
    reference = control_context.get("previous_output_ref")
    if previous is None:
        require(reference is None, "The account stage accepts no preceding business input")
    else:
        _, artifacts = state(root, plan)
        require(reference is not None and artifacts.read_json(reference) == previous,
                "Stage input differs from its exact scheduler predecessor reference")
        require(previous["chain_id"] == chain_id
                and previous["protocol_sha256"] == control_context["protocol_sha256"]
                and previous["implementation_identity"] == control_context["implementation_identity"],
                "Predecessor belongs to a different chain, protocol or implementation")
    return control_context


def call(root, plan, chain_id, suffix, operation, payload):
    import pipeline
    request = {"schema_version": 4, "request_id": chain_id + ":" + suffix,
               "operation": operation, "payload": copy.deepcopy(payload)}
    result = pipeline.run(request, root, plan)
    store, artifacts = state(root, plan)
    record = store.operation(request["request_id"])
    require(record is not None and record["request"] == request,
            "Business stage operation does not bind its exact request")
    reference = artifacts.put_json(result)
    receipt = {"operation_id": request["request_id"], "operation": operation,
               "status": "completed" if record["status"] == "completed" else "failed",
               "result_hash": fingerprint(result), "source_ref": reference}
    return result, receipt, reference


def bound_result(root, plan, binding, operation):
    """Reject a substituted operation, even if it has the same human purpose."""
    store, artifacts = state(root, plan)
    require(type(binding) is dict and set(binding) == {"operation_id", "result_ref"},
            "An explicit operation id and immutable result ref are required")
    record = store.operation(binding["operation_id"])
    require(record is not None and record["status"] == "completed"
            and record["request"]["operation"] == operation,
            "The bound engine operation has not completed")
    value = artifacts.read_json(binding["result_ref"])
    require(value == record["result"], "Bound engine result differs from its immutable public receipt")
    import stage_validation
    bindings = artifacts.read_json(value["automatic_validation"]["operation_bindings_ref"])
    stage_validation.validate_operation_result(record["request"], value, store, artifacts, bindings)
    return value


def binding(receipt, reference):
    return {"operation_id": receipt["operation_id"], "result_ref": reference}


def emit(previous, stage, data, *, control_context, engine_context=None, status="needs_evidence",
         receipts=None, source_refs=None, processing_complete=True):
    from chains.contracts import PAYLOAD_KEYS
    payload = {"account_context": copy.deepcopy(previous["payload"]["account_context"]),
               PAYLOAD_KEYS[stage]: data,
               "engine_context": context(previous) if engine_context is None else engine_context}
    return make_envelope(stage, previous["chain_id"], payload, previous=previous,
                         previous_ref=control_context["previous_output_ref"],
                         protocol_sha256=control_context["protocol_sha256"],
                         implementation_identity=control_context["implementation_identity"],
                         processing_complete=processing_complete,
                         allow_partial=control_context.get("delivery_mode") == "observation",
                         status=status, operation_receipts=receipts, source_refs=source_refs)
