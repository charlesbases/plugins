"""正式SC01运行入口，输出SealedOutput或三次失败汇总。"""

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment.contracts import ContractError, Schemas, artifact, canonical, known, now, unknown
from investment.chains.account.chain import load_process_input
from investment.chains.account.timing import prepare_questions
from investment.references import Resolver
from investment.runtime.runs import run_uri
from investment.runtime.controller import Controller
from investment.runtime.paths import validate_input_path, validate_root, validate_source_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.home() / ".investment")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--fixture-mode", action="store_true")
    parser.add_argument("--prepare", action="store_true", help="Read-only batch of optional arrival questions; no invocation or attempt")
    args = parser.parse_args()
    args.root = validate_root(args.root, args.fixture_mode)
    args.input = validate_input_path(args.root, args.input)
    if args.prepare:
        schemas = Schemas()
        resolver = Resolver(args.root, schemas, fixture_mode=args.fixture_mode)
        incoming = artifact(args.input.read_bytes(), "investment.stage_input", run_uri(args.root, args.input))
        bundle = load_process_input(incoming, resolver, args.run_id, args.invocation_id)
        validate_source_root(args.root, bundle["source"]["source_type"])
        prepared_at = now()
        result = prepare_questions(bundle, prepared_at)
        schemas.validate("investment.arrival_preparation", result)
        sys.stdout.buffer.write(canonical(result))
        return 0
    controller = Controller(args.root, args.fixture_mode)
    try:
        code, result = controller.run(args.input, args.run_id, args.invocation_id)
    except ContractError as error:
        if error.rule not in ("T01", "T02", "T03", "T04", "S01", "S02"):
            raise
        reason_codes = {"T01": "invalid_attempt_state", "T02": "invalid_attempt_state", "T03": "invalid_manifest", "T04": "invalid_attempt_state", "S01": "retention_blocked", "S02": "invalid_cleanup_state"}
        result = {"schema_version": 1, "rejection_id": "rejection-" + args.invocation_id, "stage_id": "SC01", "run_id": known(args.run_id), "invocation_id": known(args.invocation_id), "detected_at": now(), "rule_id": error.rule, "object_ref": unknown("control_input_unverified"), "json_pointer": known(error.pointer), "reason_code": "retention_blocked" if error.rule.startswith("S") else "invalid_attempt_state", "reason": error.reason, "owner": "storage" if error.rule.startswith("S") else "controller", "action": "refuse_cleanup" if error.rule.startswith("S") else "refuse_control_mutation"}
        result["reason_code"] = reason_codes[error.rule]
        controller.schemas.validate("investment.protocol_rejection", result)
        code = 2
    sys.stdout.buffer.write(canonical(result))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, ValueError) as error:
        print(f"SC01 cannot start or recover: {error}", file=sys.stderr)
        raise SystemExit(2)
