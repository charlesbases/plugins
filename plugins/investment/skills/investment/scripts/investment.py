"""The sole public command. It publishes suggestions, never executes trades."""
import argparse
import json
import sys

from contracts import ContractError, RetryableError, parse_request
from file_io import default_root, read_object


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--root", default=str(default_root()))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    try:
        request = read_object(args.request)
        from allocation_runtime import ensure_runtime
        ensure_runtime(args.root)
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Shanghai")
        from pipeline import run
        result = run(parse_request(request), args.root, args.plan)
    except (ValueError, OSError, RetryableError) as exc:
        result = {"status": getattr(exc, "code", "retryable" if isinstance(exc, OSError) else "invalid_input"),
                  "reason": str(exc)}
        if getattr(exc, "required_actions", None):
            result["required_actions"] = exc.required_actions
        if callable(getattr(exc, "as_dict", None)):
            result["validation_failure"] = exc.as_dict()
        elif getattr(exc, "validation_failure", None):
            result["validation_failure"] = exc.validation_failure
    except Exception as exc:
        result = {"status": "internal_error", "reason": type(exc).__name__ + ": " + str(exc)}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result["status"] in {"invalid_input", "invalid_evidence", "input_conflict", "retryable",
                                       "stale_snapshot", "unreconciled_confirmation", "lease_lost", "internal_error", "failed",
                                       "stage_validation_failed", "final_validation_failed"} else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
