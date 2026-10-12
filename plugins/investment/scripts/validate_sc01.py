"""正式只读独立验收进程；凭证写stdout，失败明细写stderr。"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from investment.chains.account.validation import report
from investment.contracts import ContractError, Schemas, artifact, canonical, known, now, unknown
from investment.references import Resolver
from investment.runtime.paths import validate_input_path, validate_root, validate_source_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("input", "output"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--attempt-no", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--fixture-mode", action="store_true")
    args = parser.parse_args()
    root, schemas = validate_root(args.root, args.fixture_mode), Schemas()
    args.input = validate_input_path(root, args.input)
    if args.candidate is not None:
        args.candidate = validate_input_path(root, args.candidate)
    resolver = Resolver(root, schemas, fixture_mode=args.fixture_mode)
    def reference(path, sid):
        return artifact(path.read_bytes(), sid, "investment-run://sc01/" + path.resolve().relative_to(root / "runs/sc01").as_posix())
    input_ref = reference(args.input, "investment.stage_input")
    candidate_ref = reference(args.candidate, "investment.candidate_output") if args.candidate else None
    identity = {"run_id": args.run_id, "invocation_id": args.invocation_id, "attempt_no": args.attempt_no}
    result, failure = report(args.phase, input_ref, resolver, identity, candidate_ref)
    if failure is None:
        incoming = resolver.resolve(input_ref)
        facts_ref = next(r["ref"] for r in incoming["input_refs"] if r["role"] == "transaction_facts_ref")
        facts = resolver.resolve(facts_ref)
        source = resolver.resolve(facts["source_record_ref"])
        validate_source_root(root, source["source_type"])
    if failure:
        fault, location = getattr(failure, "object_ref", None) or candidate_ref or input_ref, failure.pointer
        try:
            incoming = resolver.resolve(input_ref)
            roles = {r["role"]: r["ref"] for r in incoming["input_refs"]}
            facts = resolver.resolve(roles["transaction_facts_ref"])
            if location.startswith(("/changes", "/arrival_replies", "/base_generation", "/confirmed_at", "/operation", "/source_type", "/purpose")):
                fault = facts["source_record_ref"]
            if location.startswith("/parameters"):
                fault = incoming["parameters_ref"]
            if args.phase == "output" and location.startswith(("/trades", "/positions")):
                candidate = resolver.resolve(candidate_ref)
                context = resolver.resolve(candidate["payload_ref"])
                name = "trades" if location.startswith("/trades") else "positions"
                fault = context[name + "_ref"]
                location = location[len("/" + name):] or "/"
        except (ContractError, KeyError):
            fault, location = input_ref, "/"
        # Failure.object_ref引用实际对象字节，不携带DatasetRef的表元数据。
        fault = {key: fault[key] for key in ("artifact_id", "schema_id", "schema_version", "media_type", "uri", "sha256", "byte_size")}
        detail = {"schema_version": 1, "failure_id": f"failure-{args.invocation_id}-{args.attempt_no}", "run_id": args.run_id, "stage_id": "SC01", "invocation_id": args.invocation_id, "attempt_no": args.attempt_no, "phase": "input_check" if args.phase == "input" else "output_check", "detected_at": now(), "category": "input_semantic" if args.phase == "input" else "output_semantic", "rule_id": failure.rule, "reason_code": failure.code, "object_ref": known(fault), "json_pointer": known(location), "reason": failure.reason, "evidence_refs": [input_ref], "affected_uses": ["current_account_review", "reporting"], "owner": "validator", "gaps": []}
        schemas.validate("investment.failure", detail)
        sys.stderr.buffer.write(canonical(detail))
    sys.stdout.buffer.write(canonical(result))
    return 1 if failure else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, ValueError) as error:
        print(f"Validator cannot complete: {error}", file=sys.stderr)
        raise SystemExit(2)
