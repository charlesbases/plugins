"""Load the frozen definition before execution; this is not a scientific gate."""
import hashlib
from pathlib import Path

from contracts import fingerprint, require, strict_json_loads

REFERENCES = Path(__file__).resolve().parents[2] / "references"


def load_definition():
    frozen = strict_json_loads((REFERENCES / "chain-contract-freeze.json").read_text(encoding="utf-8"))
    require(frozen.get("schema_id") == "investment_business_contract_freeze_v1"
            and frozen.get("status") == "definition_frozen_before_contract_based_stage_design",
            "Business input/output definition has not been frozen")
    for name, expected in frozen["files"].items():
        require(Path(name).name == name, "Frozen definition path escapes references")
        raw = (REFERENCES / name).read_bytes()
        require(len(raw) == expected["size"] and hashlib.sha256(raw).hexdigest() == expected["sha256"],
                "Frozen business/scientific definition changed: " + name)
    definition = strict_json_loads((REFERENCES / "business-chain.protocol.json").read_text(encoding="utf-8"))
    science = definition["common"]["scientific_definition"]
    require(science["sha256"] == frozen["files"]["scientific-contracts.md"]["sha256"],
            "Scientific definition differs from the protocol identity")
    return definition, frozen


def identities():
    _, frozen = load_definition()
    import verify
    return {"protocol_sha256": frozen["files"]["business-chain.protocol.json"]["sha256"],
            "implementation_identity": fingerprint(verify.code_identity())}


def control(root, plan, chain_id, *, previous_output_ref=None, delivery_mode="qualified_recommendation"):
    require(delivery_mode in {"qualified_recommendation", "observation", "pending"}, "Unknown delivery mode")
    return {"data_root": str(Path(root).resolve()), "plan": plan, "chain_id": chain_id,
            "previous_output_ref": previous_output_ref, "delivery_mode": delivery_mode,
            "requested_goal": "investment_recommendation", **identities()}


def validate_control(control_context, *, root=None, plan=None, chain_id=None):
    require(type(control_context) is dict, "Scheduler control_context must be explicit")
    current = identities()
    require(all(control_context.get(key) == value for key, value in current.items()),
            "Current protocol or implementation differs from scheduler control")
    require(control_context.get("requested_goal") == "investment_recommendation",
            "The primary investment goal cannot be replaced to obtain a passing result")
    if root is not None:
        require(Path(control_context["data_root"]).resolve() == Path(root).resolve(), "Control data root changed")
    if plan is not None:
        require(control_context["plan"] == plan, "Control plan changed")
    if chain_id is not None:
        require(control_context["chain_id"] == chain_id, "Control chain changed")
    return current
