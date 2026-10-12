"""SC01流程入口：实际处理冻结输入，只产生当前尝试的候选文件。"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment.chains.account.chain import load_process_input, produce
from investment.contracts import ContractError, Schemas, artifact, canonical, is_known, known, now, require
from investment.references import Resolver
from investment.runtime import runs
from investment.runtime.paths import validate_input_path, validate_root, validate_source_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--attempt-no", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--fixture-mode", action="store_true")
    args = parser.parse_args()
    root, schemas = validate_root(args.root, args.fixture_mode), Schemas()
    args.input = validate_input_path(root, args.input)
    resolver = Resolver(root, schemas, fixture_mode=args.fixture_mode)
    input_ref = artifact(args.input.read_bytes(), "investment.stage_input", runs.run_uri(root, args.input))
    try:
        frozen = resolver.frozen_invocation(input_ref, args.run_id, args.invocation_id)
        require(frozen["processing_state"] == "running" and len(frozen["attempts"]) == args.attempt_no and frozen["attempts"][-1]["state"] == "running" and frozen["attempts"][-1]["phase"] == "production", "I07", "/attempts", "Process must own the current production attempt")
        if args.attempt_no > 1:
            previous = frozen["attempts"][-2]
            require(previous["state"] == "failed" and is_known(previous["failure_ref"]), "I07", "/attempts", "Previous attempt must supply actual failure feedback")
            feedback = resolver.resolve(previous["failure_ref"]["value"], "investment.failure")
            require((feedback["run_id"], feedback["invocation_id"], feedback["attempt_no"]) == (args.run_id, args.invocation_id, args.attempt_no - 1), "I07", "/failure_ref", "Feedback belongs to another attempt")
            print(f"Reprocessing failure rule={feedback['rule_id']} code={feedback['reason_code']}", file=sys.stderr, flush=True)
        bundle = load_process_input(input_ref, resolver, args.run_id, args.invocation_id, frozen)
        validate_source_root(root, bundle["source"]["source_type"])
        base = args.input.resolve().parent / f"attempt-{args.attempt_no:02}"
        prefix = runs.run_uri(root, base / "candidate.json").rsplit("/", 1)[0] + "/"
        state, candidate = produce(bundle, schemas, prefix, f"output-{args.invocation_id}-{args.attempt_no}")
        runs.put(root, base / "state.json", state, "investment.staged_transactions", schemas)
        runs.put(root, base / "context.json", state["context"], "investment.transaction_context", schemas)
        for name, value in state["business_objects"].items():
            runs.put(root, base / (name + ".json"), value, "investment." + name, schemas)
        reference = runs.put(root, base / "candidate.json", candidate, "investment.candidate_output", schemas)
        sys.stdout.buffer.write(canonical(reference))
        return 0
    except (ContractError, OSError, ValueError) as error:
        contract = isinstance(error, ContractError)
        fault = getattr(error, "object_ref", None) or input_ref
        fault = {key: fault[key] for key in ("artifact_id", "schema_id", "schema_version", "media_type", "uri", "sha256", "byte_size")}
        failure = {"schema_version": 1, "failure_id": f"failure-{args.invocation_id}-{args.attempt_no}", "run_id": args.run_id, "stage_id": "SC01", "invocation_id": args.invocation_id, "attempt_no": args.attempt_no, "phase": "production", "detected_at": now(), "category": "production", "rule_id": error.rule if contract else "A02", "reason_code": error.code if contract else "producer_exception", "object_ref": known(fault), "json_pointer": known(error.pointer if contract else "/"), "reason": error.reason if contract else str(error)[:1000], "evidence_refs": [input_ref], "affected_uses": ["current_account_review", "reporting"], "owner": "producer", "gaps": []}
        schemas.validate("investment.failure", failure)
        # 失败行独立输出；已有重处理诊断在前，不作为业务或校验依据。
        sys.stdout.buffer.write(canonical(failure))
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, ValueError) as error:
        print(f"Process cannot start: {error}", file=sys.stderr)
        raise SystemExit(2)
