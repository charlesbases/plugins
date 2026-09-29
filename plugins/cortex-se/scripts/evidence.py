#!/usr/bin/env python3
"""Save source code credentials by investigation batch and check the evidentiary obligations of the claims in key locations."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time


OBLIGATIONS = {
    "behavior": ("entry", "flow", "boundary"),
    "defect": ("entry", "preconditions", "failure_path"),
    "root_cause": ("entry", "failure_path", "symptom"),
    "necessity": ("current_contract", "caller", "alternatives"),
}
STATUSES = {"confirmed", "refuted", "inferred", "unresolved"}
UNIT_BYTES = 4096
BATCH_BYTES = 16384
PATCH_TOOLS = {"apply_patch", "Edit", "Write"}


class EvidenceError(ValueError):
    """This indicates that the external input or evidence status does not meet the inspection conditions."""


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{name} must be a non-empty string")
    return value


def require_list(value, name):
    if not isinstance(value, list):
        raise EvidenceError(f"{name} must be a list")
    return value


def load_input():
    raw = sys.stdin.read(131073)
    if len(raw.encode("utf-8")) > 131072:
        raise EvidenceError("Input exceeds 128 KiB")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise EvidenceError("Input must be a JSON object")
    return value


def database_path(data_dir, session_id):
    require_text(session_id, "session_id")
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return Path(data_dir).resolve() / "evidence" / f"{key}.sqlite3"


def validate_stored_state(state):
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise EvidenceError("Unsupported evidence state schema")
    if not isinstance(state.get("receipts"), dict):
        raise EvidenceError("Invalid persisted receipts")
    for field in ("active", "hook_seen"):
        if type(state.get(field)) is not bool:
            raise EvidenceError(f"Invalid persisted {field}")
    require_text(state.get("workspace"), "stored workspace")
    if not Path(state["workspace"]).is_absolute() or not isinstance(state.get("scope"), str):
        raise EvidenceError("Invalid persisted workspace/scope")
    if "checkpoint" not in state or (
        state["checkpoint"] is not None and not isinstance(state["checkpoint"], dict)
    ):
        raise EvidenceError("Invalid persisted checkpoint")
    if "continued_turn" not in state or (
        state["continued_turn"] is not None and not isinstance(state["continued_turn"], str)
    ):
        raise EvidenceError("Invalid persisted continuation")
    for receipt_id, receipt in state["receipts"].items():
        if not isinstance(receipt, dict) or receipt.get("id") != receipt_id:
            raise EvidenceError("Invalid persisted receipt identity")
        path = require_text(receipt.get("path"), "stored receipt path")
        digest = require_text(receipt.get("sha256"), "stored receipt hash")
        if not Path(path).is_absolute() or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise EvidenceError("Invalid persisted receipt path/hash")
        start, end = receipt.get("start"), receipt.get("end")
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise EvidenceError("Invalid persisted receipt range")


def open_store(path, workspace):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT payload FROM state WHERE id = 1").fetchone()
    if row:
        try:
            state = json.loads(row[0])
            validate_stored_state(state)
        except (ValueError, EvidenceError):
            conn.close()
            raise
        root = Path(state["workspace"])
        if Path(workspace).resolve() != root:
            conn.close()
            raise EvidenceError("Session workspace does not match stored evidence")
    else:
        state = {
            "schema_version": 1,
            "workspace": str(Path(workspace).resolve()),
            "scope": "",
            "active": False,
            "receipts": {},
            "checkpoint": None,
            "continued_turn": None,
            "hook_seen": False,
        }
    return conn, state


def save_state(conn, state):
    conn.execute(
        "INSERT OR REPLACE INTO state (id, payload) VALUES (1, ?)",
        (json.dumps(state, ensure_ascii=False),),
    )
    conn.commit()


def read_batch(state, document):
    if not isinstance(document, dict):
        raise EvidenceError("Read input must be an object")
    scope = require_text(document.get("scope"), "scope")
    files = require_list(document.get("files"), "files")
    if not files:
        raise EvidenceError("files must not be empty")
    if scope != state["scope"]:
        state["receipts"] = {}
    state.update(scope=scope, active=True, checkpoint=None, continued_turn=None)
    output = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise EvidenceError("Each file range must be an object")
        path = Path(require_text(item.get("path"), "path"))
        if not path.is_absolute():
            path = Path(state["workspace"]) / path
        path = path.resolve(strict=True)
        start, end = item.get("start"), item.get("end")
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            raise EvidenceError("start/end must be positive, ordered line numbers")
        raw = path.read_bytes()
        lines = raw.decode("utf-8-sig").splitlines(keepends=True)
        if end > len(lines):
            raise EvidenceError(f"Range exceeds file length: {path}")
        content = "".join(lines[start - 1:end])
        size = len(content.encode("utf-8"))
        if size > UNIT_BYTES or total + size > BATCH_BYTES:
            raise EvidenceError("Use ranges <= 4 KiB and batches <= 16 KiB")
        total += size
        digest = hashlib.sha256(raw).hexdigest()
        identity = f"{path}\0{start}\0{end}\0{digest}"
        receipt_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        receipt = {
            "id": receipt_id, "path": str(path), "start": start, "end": end,
            "sha256": digest, "read_at": time.time(),
        }
        output.append({**receipt, "content": content})
    # 完整批次读取成功后才登记凭据，失败批次不产生部分成功。
    for receipt in output:
        state["receipts"][receipt["id"]] = {
            key: value for key, value in receipt.items() if key != "content"
        }
    return {"scope": scope, "receipts": output, "source_bytes": total}


def validate_checkpoint(state, document):
    if not isinstance(document, dict):
        raise EvidenceError("Checkpoint input must be an object")
    if require_text(document.get("purpose"), "purpose") not in {"analysis", "change"}:
        raise EvidenceError("purpose must be analysis or change")
    claims = require_list(document.get("claims"), "claims")
    if not claims:
        raise EvidenceError("claims must not be empty")
    errors, statuses, referenced = [], {}, set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise EvidenceError("Each claim must be an object")
        claim_id = require_text(claim.get("id"), "claim.id")
        require_text(claim.get("text"), "claim.text")
        if claim_id in statuses:
            raise EvidenceError(f"Duplicate claim id: {claim_id}")
        kind = require_text(claim.get("kind"), "claim.kind")
        status = require_text(claim.get("status"), "claim.status")
        if kind not in OBLIGATIONS or status not in STATUSES:
            raise EvidenceError(f"Invalid claim kind/status: {claim_id}")
        statuses[claim_id] = status
        gaps = require_list(claim.get("gaps"), "claim.gaps")
        for gap in gaps:
            require_text(gap, "gap")
        checks = claim.get("checks")
        if not isinstance(checks, dict) or set(checks) - set(OBLIGATIONS[kind]):
            raise EvidenceError(f"Invalid checks for {claim_id}")
        if status in {"confirmed", "refuted"} and gaps:
            errors.append(f"{claim_id}: established claim still has evidence gaps")
        if status in {"inferred", "unresolved"} and not gaps:
            errors.append(f"{claim_id}: uncertain claim must identify its evidence gap")
        for obligation in OBLIGATIONS[kind]:
            check = checks.get(obligation)
            if check is None:
                if status in {"confirmed", "refuted"}:
                    errors.append(f"{claim_id}: missing {obligation}")
                continue
            if not isinstance(check, dict):
                raise EvidenceError(f"{claim_id}.{obligation} must be an object")
            require_text(check.get("reason"), "check.reason")
            receipt_ids = require_list(check.get("receipts"), "check.receipts")
            if not receipt_ids:
                errors.append(f"{claim_id}.{obligation}: no source receipts")
            for receipt_id in receipt_ids:
                require_text(receipt_id, "receipt id")
                referenced.add(receipt_id)
    digests = {}
    for receipt_id in sorted(referenced):
        receipt = state["receipts"].get(receipt_id)
        if receipt is None:
            errors.append(f"Unknown receipt: {receipt_id}")
            continue
        path = receipt["path"]
        if path not in digests:
            try:
                digests[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                digests[path] = None
        if digests[path] != receipt["sha256"]:
            errors.append(f"Stale receipt: {receipt_id} ({path})")
    constraints = require_list(document.get("constraints", []), "constraints")
    for constraint in constraints:
        if not isinstance(constraint, dict):
            raise EvidenceError("Each constraint must be an object")
        name = require_text(constraint.get("name"), "constraint.name")
        require_text(constraint.get("source"), "constraint.source")
        if "expected" not in constraint or "proposed" not in constraint:
            raise EvidenceError("Constraint requires expected and proposed values")
        expected = json.dumps(constraint["expected"], sort_keys=True, ensure_ascii=False)
        proposed = json.dumps(constraint["proposed"], sort_keys=True, ensure_ascii=False)
        if expected != proposed:
            errors.append(f"Changed declared constraint: {name}")
    if document["purpose"] == "change":
        basis = require_list(document.get("basis", []), "basis")
        requirement = document.get("user_requirement", "")
        if not basis and not (isinstance(requirement, str) and requirement.strip()):
            errors.append("Change requires established basis or an explicit user requirement")
        for claim_id in basis:
            if not isinstance(claim_id, str) or statuses.get(claim_id) != "confirmed":
                errors.append(f"Change basis is not confirmed: {claim_id}")
    return {
        "ok": not errors,
        "purpose": document["purpose"],
        "claims": statuses,
        "errors": errors,
        "meaning": "Structural evidence checks only; not proof of semantic correctness or approval.",
    }


def handle_hook(event, data_dir):
    name = event.get("hook_event_name")
    if name not in {"SessionStart", "PreToolUse", "Stop"}:
        return {}
    if name == "PreToolUse" and event.get("tool_name") not in PATCH_TOOLS:
        return {}
    session_id = require_text(event.get("session_id"), "session_id")
    workspace = require_text(event.get("cwd"), "cwd")
    path = database_path(data_dir, session_id)
    if name != "SessionStart" and not path.exists():
        return {}
    conn, state = open_store(path, workspace)
    try:
        if name == "SessionStart":
            state["hook_seen"] = True
            script = Path(__file__).resolve().as_posix()
            command = shlex.join([
                Path(sys.executable).as_posix(), script, "--data-dir", Path(data_dir).resolve().as_posix(),
                "--session-id", session_id, "--workspace", Path(state["workspace"]).as_posix(),
            ])
            result = {"hookSpecificOutput": {
                "hookEventName": name,
                "additionalContext": (
                    "Cortex SE lightweight evidence is available. For repository conclusions, "
                    "follow code-tracing/references/evidence-contract.md. Batch necessary "
                    f"ranges with: {command} read. Check claims with: {command} check. "
                    "Both commands accept JSON on stdin. Reuse fresh receipts. "
                    "No per-command monitoring; only registered investigations are checked. "
                    f"Registered scope: {state['scope'] or '(none)'}. "
                    f"Pending investigation: {state['active']}."
                ),
            }}
        elif not state["active"]:
            result = {}
        else:
            checkpoint = state["checkpoint"]
            report = (
                validate_checkpoint(state, checkpoint) if checkpoint
                else {"ok": False, "errors": ["No evidence checkpoint for the active investigation"]}
            )
            if name == "PreToolUse":
                if report["ok"] and checkpoint["purpose"] == "change":
                    result = {}
                else:
                    reason = "; ".join(report["errors"]) or "Run a change checkpoint before writing"
                    result = {"hookSpecificOutput": {
                        "hookEventName": name, "permissionDecision": "deny",
                        "permissionDecisionReason": f"Cortex SE evidence: {reason}",
                    }}
            elif report["ok"]:
                state["active"] = False
                result = {}
            else:
                turn_id = require_text(event.get("turn_id"), "turn_id")
                reason = "; ".join(report["errors"])
                if event.get("stop_hook_active") or state["continued_turn"] == turn_id:
                    # 第二次仍不完整时结束续跑，保留失败事实，不把重试上限当作通过。
                    result = {
                        "continue": False,
                        "systemMessage": f"Cortex SE evidence remains incomplete: {reason}",
                    }
                else:
                    state["continued_turn"] = turn_id
                    result = {
                        "decision": "block",
                        "reason": (
                            f"Cortex SE evidence checkpoint failed: {reason}. "
                            "Read only the missing ranges, or submit an analysis checkpoint "
                            "with explicit unresolved claims and report the limitation. "
                            "Do not treat this continuation as permission to edit."
                        ),
                    }
        save_state(conn, state)
        return result
    finally:
        conn.close()


def run_cli(args, document=None):
    path = database_path(args.data_dir, args.session_id)
    conn, state = open_store(path, args.workspace)
    try:
        state["active"] = True
        state["checkpoint"] = None
        if document is None:
            document = load_input()
        if args.command == "read":
            result = read_batch(state, document)
            status = 0
        else:
            result = validate_checkpoint(state, document)
            state["checkpoint"] = document
            result["hook_observed"] = state["hook_seen"]
            status = 0 if result["ok"] else 1
        save_state(conn, state)
        return result, status
    except Exception:
        # 包括 JSON 解析失败；撤销旧检查后原样传播错误，禁止回退到旧通过状态。
        state["checkpoint"] = None
        save_state(conn, state)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("read", "check", "hook"))
    parser.add_argument("--data-dir", default=os.environ.get("PLUGIN_DATA"))
    parser.add_argument("--session-id", default=os.environ.get("CODEX_THREAD_ID"))
    parser.add_argument("--workspace", default=os.getcwd())
    args = parser.parse_args()
    try:
        if not args.data_dir:
            raise EvidenceError("Use --data-dir from SessionStart, or set PLUGIN_DATA")
        if args.command == "hook":
            result, status = handle_hook(load_input(), args.data_dir), 0
        else:
            result, status = run_cli(args)
        print(json.dumps(result, ensure_ascii=False))
        return status
    except (EvidenceError, OSError, UnicodeError, json.JSONDecodeError, sqlite3.Error) as error:
        message = f"Cortex SE evidence error: {error}"
        if args.command == "hook":
            # 不把故障静默视为通过；exit 2 使用宿主定义的阻断/续跑行为。
            print(message, file=sys.stderr)
            return 2
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    # Hook 与命令输入均为 UTF-8 JSON，不能采用 Windows 的默认 GBK 管道编码。
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
