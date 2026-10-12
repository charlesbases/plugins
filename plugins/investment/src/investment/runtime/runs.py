"""运行根互斥、不可变业务文件、权威清单和可重建README。"""

from __future__ import annotations

import html
import copy
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from investment.contracts import ContractError, artifact, canonical, digest, instant, is_known, now, parse, require
from investment.references import Resolver, safe_path


@contextmanager
def root_lock(root: Path):
    directory = root / "runs" / "sc01"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".controller.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic(path: Path, raw: bytes, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        require(path.read_bytes() == raw, "T03", "/files", "Immutable artifact changed", "identity_mismatch")
        return
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_uri(root: Path, path: Path):
    return "investment-run://sc01/" + path.resolve().relative_to(root.resolve() / "runs" / "sc01").as_posix()


def put(root, path, value, sid, schemas):
    schemas.validate(sid, value)
    raw = canonical(value)
    atomic(path, raw, immutable=True)
    return artifact(raw, sid, run_uri(root, path))


def execution_date(created):
    return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def load_manifest(root, day, schemas):
    path = root / "runs" / "sc01" / day / "manifest.json"
    if path.exists():
        return schemas.document("investment.daily_manifest", path.read_bytes())
    return {"schema_version": 1, "stage_id": "SC01", "execution_date": day, "timezone": "Asia/Shanghai", "updated_at": now(), "invocations": []}


def collect_files(root, day, invocation):
    base = root / "runs" / "sc01" / day / invocation["invocation_id"]
    for file in invocation["files"] + [f for a in invocation["attempts"] for f in a["files"]]:
        path = safe_path(base.parent, *file["relative_path"].split("/"))
        raw = path.read_bytes()
        require(digest(raw) == file["sha256"] and len(raw) == file["byte_size"], "T03", "/files", "Previously recorded artifact changed", "identity_mismatch")
    roles = {"input.json": "frozen_input", "submitted-input.json": "frozen_input", "source-record.json": "source_record", "scope.json": "scope", "parameters.json": "parameters", "control-parameters.json": "parameters", "input-check.json": "input_check", "candidate.json": "candidate", "receipt.json": "validation", "sealed.json": "sealed_output", "failure.json": "failure", "execution.json": "execution", "terminal-failure.json": "terminal_failure"}
    def entry(path):
        relative = path.relative_to(base.parent).as_posix()
        raw = path.read_bytes()
        role = roles.get(path.name, "staged_object")
        purpose = "input_gate" if role == "input_check" else "output_gate" if role == "validation" else "business_handoff" if role in ("candidate", "sealed_output") else "failure_explanation" if role in ("failure", "terminal_failure") else "temporary_computation" if role == "staged_object" else "frozen_context"
        return {"role": role, "relative_path": relative, "media_type": "application/json", "sha256": digest(raw), "byte_size": len(raw), "purpose_code": purpose}
    invocation["files"] = [entry(path) for path in sorted(base.glob("*.json"))]
    for attempt in invocation["attempts"]:
        directory = base / attempt["directory"]
        attempt["files"] = [entry(path) for path in sorted(directory.glob("*.json"))]


def _escape(value):
    return html.escape(str(value)).replace("|", "\\|").replace("`", "\\`").replace("[", "\\[").replace("\n", "<br>").replace("\r", "")


def render(root, manifest):
    result = [f"# sc01：{manifest['execution_date']} 执行说明", "", f"执行日期：{manifest['execution_date']}", "时区：Asia/Shanghai", "", "| invocation_id | 执行内容 | 主要文件及用途 | 执行状态 | 保留与清理条件 |", "|---|---|---|---|---|"]
    resolver = Resolver(root, None)
    for inv in sorted(manifest["invocations"], key=lambda v: (v["created_at"], v["invocation_id"])):
        cleaned = inv["retention"]["state"] == "cleaned"
        files = inv["files"] + [file for attempt in inv["attempts"] for file in attempt["files"]]
        descriptions = "; ".join(f["relative_path"] + ":" + f["purpose_code"] for f in sorted({f["relative_path"]: f for f in files}.values(), key=lambda v: v["relative_path"]))
        status = f"{inv['processing_state']}/{inv['validation_state']}; attempts={len(inv['attempts'])}"
        if not cleaned:
            for attempt in inv["attempts"]:
                if is_known(attempt["failure_ref"]):
                    failure = parse(resolver.read(attempt["failure_ref"]["value"]["uri"]))
                    status += f"; {attempt['attempt_no']}:{failure['rule_id']}:{failure['json_pointer'].get('value')}:{failure['reason_code']}:{failure['reason']}"
        content = f"account={inv['account_id'].get('value')}; base_generation={inv['expected_generation']}"
        if inv["processing_state"] == "completed" and not cleaned:
            attempt = next(a for a in inv["attempts"] if a["attempt_no"] == inv["selected_attempt"]["value"])
            state_uri = attempt["candidate_ref"]["value"]["uri"].rsplit("/", 1)[0] + "/state.json"
            state = parse(resolver.read(state_uri))
            pending = state["business_objects"]["pending_transactions"]
            source = state["source_record"]
            content += f"; operation={source['operation']}; mutation={len(source['changes']['rows'])}; reply={len(source['arrival_replies']['rows'])}; 交易={len(state['business_objects']['trades']['rows'])}; 剩余在途={len(pending['rows'])}"
        result.append("| " + " | ".join(_escape(x) for x in (inv["invocation_id"], content, "已清理" if cleaned else descriptions, status, inv["retention"]["state"] + "; blockers=" + ",".join(inv["retention"]["blockers"]))) + " |")
    return ("\n".join(result) + "\n").encode("utf-8")


def save_manifest(root, manifest, schemas):
    schemas.validate("investment.daily_manifest", manifest)
    # 先核对已登记的不可变文件，再补登记本次新增文件，不能用重算哈希掩盖改写。
    for inv in manifest["invocations"]:
        if inv["retention"]["state"] == "cleaned":
            continue
        for file in inv["files"] + [f for a in inv["attempts"] for f in a["files"]]:
            path = safe_path(root / "runs" / "sc01" / manifest["execution_date"], *file["relative_path"].split("/"))
            raw = path.read_bytes()
            require(digest(raw) == file["sha256"] and len(raw) == file["byte_size"], "T03", "/files", "Previously recorded artifact changed", "identity_mismatch")
        collect_files(root, manifest["execution_date"], inv)
    schemas.validate("investment.daily_manifest", manifest)
    day = manifest["execution_date"]
    require(manifest["stage_id"] == "SC01" and len({v["invocation_id"] for v in manifest["invocations"]}) == len(manifest["invocations"]), "T03", "/invocations", "Manifest identities invalid", "identity_mismatch")
    for inv in manifest["invocations"]:
        require(execution_date(inv["created_at"]) == day, "T03", "/execution_date", "Execution date changed", "identity_mismatch")
        numbers = [a["attempt_no"] for a in inv["attempts"]]
        require(numbers == list(range(1, len(numbers) + 1)) and len(numbers) <= 3, "T01", "/attempts", "Attempt budget invalid")
        passed = [a for a in inv["attempts"] if a["state"] == "passed"]
        require(len(passed) <= 1 and (not passed or passed[0] is inv["attempts"][-1]), "T02", "/attempts", "Invalid passed attempt")
        processing = inv["processing_state"]
        expected_validation = {"pending": "not_evaluated", "running": "checking", "completed": "passed", "failed": "exhausted"}
        require(inv["validation_state"] == expected_validation[processing], "T04", "/validation_state", "Processing and validation states disagree")
        terminal = processing in ("completed", "failed")
        require(inv["finished_at"]["status"] == ("known" if terminal else "unknown") and inv["next_chain_allowed"] == (processing == "completed"), "T04", "/finished_at", "Invocation terminal fields disagree")
        require(inv["terminal_failure_ref"]["status"] == ("known" if processing == "failed" else "not_applicable"), "T04", "/terminal_failure_ref", "Invalid terminal failure reference")
        if processing != "completed":
            require(inv["selected_attempt"]["status"] == inv["sealed_output_ref"]["status"] == "not_applicable", "T04", "/selected_attempt", "Unaccepted invocation has a selected output")
        states = [a["state"] for a in inv["attempts"]]
        if processing == "pending":
            require(not states, "T04", "/attempts", "Pending invocation already has attempts")
        elif processing == "running":
            # 失败落盘到下一次开始/终态汇总之间也是可恢复的确定状态。
            require(bool(states) and (states == ["failed"] * len(states) or states == ["failed"] * (len(states) - 1) + ["running"]), "T04", "/attempts", "Invalid active attempt sequence")
        elif processing == "failed":
            require(states == ["failed", "failed", "failed"], "T02", "/attempts", "Failure requires exactly three failed attempts")
        else:
            require(bool(states) and states == ["failed"] * (len(states) - 1) + ["passed"], "T02", "/attempts", "Completion requires one final passed attempt")
        previous_finish = instant(inv["created_at"])
        for attempt in inv["attempts"]:
            require(attempt["directory"] == f"attempt-{attempt['attempt_no']:02}", "T01", "/attempts", "Attempt directory mismatch")
            require(is_known(attempt["started_at"]) and instant(attempt["started_at"]["value"]) >= previous_finish, "T04", "/attempts/started_at", "Attempt starts before prior completion")
            if attempt["state"] == "running":
                require(attempt["finished_at"]["status"] == "unknown" and all(attempt[k]["status"] == "not_applicable" for k in ("execution_record_ref", "failure_ref", "sealed_ref")), "T04", "/attempts", "Running attempt has terminal artifacts")
            if attempt["state"] in ("passed", "failed"):
                require(is_known(attempt["finished_at"]) and is_known(attempt["execution_record_ref"]), "T04", "/attempts", "Finished attempt lacks execution record")
                previous_finish = instant(attempt["finished_at"]["value"])
                require(previous_finish >= instant(attempt["started_at"]["value"]), "T04", "/attempts/finished_at", "Attempt finished before it started")
            if attempt["state"] == "passed":
                require(all(is_known(attempt[k]) for k in ("candidate_ref", "output_validation_ref", "sealed_ref", "execution_record_ref")) and attempt["failure_ref"]["status"] == "not_applicable", "T04", "/attempts", "Passed attempt lacks accepted artifacts")
            if attempt["state"] == "failed":
                require(is_known(attempt["failure_ref"]) and attempt["sealed_ref"]["status"] == "not_applicable", "T04", "/attempts", "Invalid failed attempt")
            require(all(f["relative_path"].startswith(inv["invocation_id"] + "/" + attempt["directory"] + "/") for f in attempt["files"]), "T03", "/attempts/files", "Attempt file is in another directory", "identity_mismatch")
        if terminal:
            require(instant(inv["finished_at"]["value"]) >= previous_finish, "T04", "/finished_at", "Invocation finished before its attempts")
        if inv["validation_state"] == "exhausted":
            require(len(numbers) == 3 and not passed and not inv["next_chain_allowed"] and is_known(inv["terminal_failure_ref"]), "T02", "/attempts", "Exhausted invocation is incomplete")
        if inv["validation_state"] == "passed":
            require(bool(passed) and inv["selected_attempt"].get("value") == passed[0]["attempt_no"] and is_known(inv["sealed_output_ref"]) and inv["next_chain_allowed"], "T02", "/selected_attempt", "Invalid completed invocation")
        if inv["retention"]["state"] != "cleaned":
            for file in inv["files"] + [f for a in inv["attempts"] for f in a["files"]]:
                path = safe_path(root / "runs" / "sc01" / day, *file["relative_path"].split("/"))
                require(file["relative_path"].startswith(inv["invocation_id"] + "/"), "T03", "/files", "Wrong invocation file", "identity_mismatch")
                raw = path.read_bytes()
                require(digest(raw) == file["sha256"] and len(raw) == file["byte_size"], "T03", "/files", "File inventory changed", "identity_mismatch")
    manifest["updated_at"] = now()
    directory = root / "runs" / "sc01" / day
    atomic(directory / "manifest.json", canonical(manifest))
    atomic(directory / "README.md", render(root, manifest))


def cleanup(root, manifest, invocation, schemas, store):
    # 调用者持有root_lock；意图复用DailyManifest，不引入自由格式控制数据。
    base = root / "runs" / "sc01" / manifest["execution_date"]
    journal_path = base / (".cleanup-" + invocation["invocation_id"] + ".json")
    resuming = journal_path.exists()
    if resuming:
        journal = schemas.document("investment.daily_manifest", journal_path.read_bytes())
        require(journal["stage_id"] == "SC01" and journal["execution_date"] == manifest["execution_date"] and len(journal["invocations"]) == 1, "S02", "/retention", "Invalid cleanup intent")
        expected = journal["invocations"][0]
        actual = copy.deepcopy(invocation)
        if actual["retention"]["state"] == "cleaned":
            actual["retention"]["state"] = "eligible"
        require(expected == actual and expected["retention"]["state"] == "eligible", "S02", "/retention", "Cleanup intent does not match invocation")
    paths = [safe_path(base, *f["relative_path"].split("/")) for f in invocation["files"] + [f for a in invocation["attempts"] for f in a["files"]]]
    if invocation["retention"]["state"] == "cleaned" and resuming:
        require(not any(p.exists() for p in paths), "S02", "/files", "Cleaned invocation still has artifacts")
        save_manifest(root, manifest, schemas)
        journal_path.unlink()
        return
    require(invocation["processing_state"] in ("completed", "failed") and invocation["retention"]["state"] == "eligible" and not invocation["retention"]["blockers"] and is_known(invocation["retention"]["evaluated_at"]), "S02", "/retention", "Cleanup is not eligible")
    prefix = f"investment-run://sc01/{manifest['execution_date']}/{invocation['invocation_id']}/"
    def references(value):
        if isinstance(value, dict):
            if isinstance(value.get("uri"), str):
                yield value["uri"]
            for child in value.values():
                yield from references(child)
        elif isinstance(value, list):
            for child in value:
                yield from references(child)
    for row in store.con.execute("SELECT meta_json FROM ledger_meta"):
        marker = parse(row[0].encode())["last_commit"]
        require(marker["finalized"] or marker["invocation_id"] != invocation["invocation_id"], "S01", "/retention", "Unfinalized commit still needs these files")
    for other_path in (root / "runs" / "sc01").glob("*/manifest.json"):
        other = schemas.document("investment.daily_manifest", other_path.read_bytes())
        for consumer in other["invocations"]:
            if consumer["invocation_id"] != invocation["invocation_id"]:
                require(not any(uri.startswith(prefix) for uri in references(consumer["retention"]["persistent_refs"])), "S01", "/persistent_refs", "Active downstream use still references these files")
    resolver = Resolver(root, schemas)
    for reference in invocation["retention"]["persistent_refs"]:
        resolver.resolve(reference)
    if not resuming:
        save_manifest(root, manifest, schemas)
        journal = dict(manifest, invocations=[copy.deepcopy(invocation)])
        schemas.validate("investment.daily_manifest", journal)
        atomic(journal_path, canonical(journal), immutable=True)
    paths = [safe_path(base, *f["relative_path"].split("/")) for f in invocation["files"] + [f for a in invocation["attempts"] for f in a["files"]]]
    for file, path in zip(invocation["files"] + [f for a in invocation["attempts"] for f in a["files"]], paths):
        if path.exists():
            raw = path.read_bytes()
            require(digest(raw) == file["sha256"] and len(raw) == file["byte_size"], "T03", "/files", "Cleanup artifact changed", "identity_mismatch")
    for path in paths:
        path.unlink(missing_ok=True)
    invocation["retention"]["state"] = "cleaned"
    save_manifest(root, manifest, schemas)
    journal_path.unlink()


def resume_cleanups(root, manifest, schemas, store):
    base = root / "runs" / "sc01" / manifest["execution_date"]
    for path in sorted(base.glob(".cleanup-*.json")):
        journal = schemas.document("investment.daily_manifest", path.read_bytes())
        require(len(journal["invocations"]) == 1, "S02", "/invocations", "Cleanup intent must name one invocation")
        identity = journal["invocations"][0]["invocation_id"]
        require(path.name == ".cleanup-" + identity + ".json", "S02", "/invocation_id", "Cleanup intent filename mismatch")
        invocation = next((v for v in manifest["invocations"] if v["invocation_id"] == identity), None)
        require(invocation is not None, "S02", "/invocations", "Cleanup invocation missing")
        cleanup(root, manifest, invocation, schemas, store)
