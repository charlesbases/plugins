"""The sole public command. It publishes suggestions, never executes trades."""
import argparse
import json
import sys

from contracts import ContractError, RetryableError, parse_request
from file_io import default_root, read_object


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["chain", "input-stage", "run", "prepare", "materials", "requests", "assess", "discover", "research", "market", "report"])
    parser.add_argument("--root")
    parser.add_argument("--plan", default="investment")
    parser.add_argument("--request")
    parser.add_argument("--request-id")
    parser.add_argument("--workflow-id")
    parser.add_argument("--chain-id")
    parser.add_argument("--through", choices=["account", "news_collection", "news_assessment", "fund_universe",
        "fund_sources", "model", "recommendation", "report"])
    parser.add_argument("--analysis")
    parser.add_argument("--scope-audit")
    parser.add_argument("--source-evidence")
    parser.add_argument("--inputs")
    parser.add_argument("--input")
    parser.add_argument("--input-kind", choices=["scope_audit", "source_evidence"])
    parser.add_argument("--output")
    parser.add_argument("--parameters")
    parser.add_argument("--cursor", type=int)
    parser.add_argument("--version-id")
    parser.add_argument("--body-cursor", type=int)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-urls-per-source", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=8)
    parser.add_argument("--budget-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    if args.command == "run" and not args.request:
        parser.error("run requires --request")
    if args.command != "run" and args.request:
        parser.error("Only run accepts --request; analysis submissions use --analysis")
    if args.command == "assess" and not args.analysis:
        parser.error("assess requires --analysis with the host's analysis body")
    if args.command not in {"assess", "chain"} and args.analysis:
        parser.error("Only assess or chain accepts --analysis")
    if args.command not in {"chain", "input-stage"} and args.chain_id is not None:
        parser.error("Only chain or input-stage accepts --chain-id")
    if args.command != "chain" and args.through is not None:
        parser.error("Only chain accepts --through")
    if args.command == "input-stage" and not all((args.chain_id, args.input, args.input_kind, args.output)):
        parser.error("input-stage requires --chain-id, --input-kind, --input and --output")
    if args.command != "input-stage" and any((args.input, args.input_kind, args.output)):
        parser.error("Only input-stage accepts --input, --input-kind or --output")
    if args.command != "chain" and args.scope_audit:
        parser.error("Only chain accepts --scope-audit")
    if args.command != "chain" and args.source_evidence:
        parser.error("Only chain accepts --source-evidence")
    if args.command != "report" and args.inputs:
        parser.error("Only report accepts --inputs")
    if args.command not in {"discover", "research"} and args.parameters:
        parser.error("Only discover or research accepts --parameters")
    if args.command not in {"assess", "discover", "research", "market", "report"} and args.preview:
        parser.error("Only assess, discover, research, market or report accepts --preview")
    if args.command not in {"materials", "requests", "assess", "discover", "research", "market", "report"} and args.workflow_id:
        parser.error("Only materials, requests, assess, discover, research, market or report accepts --workflow-id")
    if args.command != "materials" and any(value is not None for value in (args.cursor, args.version_id, args.body_cursor)):
        parser.error("Only materials accepts original reading cursors")
    if args.body_cursor is not None and args.version_id is None:
        parser.error("An original body cursor requires its version-id")
    try:
        args.root = args.root or str(default_root())
        raw_request = read_object(args.request) if args.command == "run" else None
        chain_analysis = read_object(args.analysis) if args.command == "chain" and args.analysis else None
        from allocation_runtime import ensure_runtime
        if args.command != "materials":
            ensure_runtime(args.root)
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Shanghai")
        request = parse_request(raw_request) if raw_request is not None else None
        from command_gate import command_gate
        with command_gate(args.root, args.plan, read_only=args.command in {"materials", "requests"} or args.preview):
            if args.command == "chain":
                from chain_inputs import load
                chain_scope = load(args.scope_audit, args.root, args.plan, args.chain_id, "scope_audit") if args.scope_audit else None
                chain_sources = load(args.source_evidence, args.root, args.plan, args.chain_id, "source_evidence") if args.source_evidence else None
                from chains.controller import execute, display
                result = execute(args.root, args.plan, chain_id=args.chain_id,
                    analysis=chain_analysis, scope_audit=chain_scope,
                    source_evidence=chain_sources, through=args.through)
                result = display(result)
            elif args.command == "input-stage":
                from chain_inputs import stage
                result = stage(args.input, args.output, args.root, args.plan, args.chain_id, args.input_kind)
            elif args.command == "prepare":
                from workflow_prepare import prepare
                from workflow_materials import compact
                result = prepare(args.root, args.plan, request_id=args.request_id, days=args.days,
                    max_urls_per_source=args.max_urls_per_source, timeout_seconds=args.timeout_seconds,
                    budget_seconds=args.budget_seconds)
                result = compact(result, args.root, args.plan)
            elif args.command == "materials":
                from workflow_materials import read
                result = read(args.root, args.plan, workflow_id=args.workflow_id, cursor=args.cursor,
                    version_id=args.version_id, body_cursor=args.body_cursor)
            elif args.command == "requests":
                from workflow_requests import request_templates
                result = request_templates(args.root, args.plan, workflow_id=args.workflow_id)
            elif args.command == "assess":
                from workflow_requests import assess
                result = assess(args.root, args.plan, read_object(args.analysis),
                    workflow_id=args.workflow_id, preview=args.preview)
            elif args.command == "report":
                from workflow_requests import report
                result = report(args.root, args.plan, workflow_id=args.workflow_id, preview=args.preview,
                    inputs=read_object(args.inputs) if args.inputs else None)
            elif args.command == "discover":
                from workflow_discovery import discover
                result = discover(args.root, args.plan, workflow_id=args.workflow_id, preview=args.preview,
                    parameters=read_object(args.parameters) if args.parameters else None)
            elif args.command == "research":
                from workflow_requests import research
                result = research(args.root, args.plan, workflow_id=args.workflow_id, preview=args.preview,
                    parameters=read_object(args.parameters) if args.parameters else None)
            elif args.command == "market":
                from workflow_requests import market
                result = market(args.root, args.plan, workflow_id=args.workflow_id, preview=args.preview)
            else:
                from pipeline import run
                result = run(request, args.root, args.plan)
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
                                       "stage_validation_failed", "final_validation_failed", "analysis_preflight_failed",
                                       "research_preflight_failed", "strategy_preflight_failed",
                                       "invalid_chain_contract", "invalid_chain_evidence"} else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
