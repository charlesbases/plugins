"""正式 CLI、独立校验进程和真实 SQLite 验收；不导入生产逻辑。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from investment.runtime.paths import test_parent, validate_root

PLUGIN = Path(__file__).resolve().parents[1]
SPEC = PLUGIN / "docs/spec/v1"
RUNNER = PLUGIN / "scripts/run_sc01.py"
VALIDATOR = PLUGIN / "scripts/validate_sc01.py"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def k(value):
    return {"status": "known", "value": value, "reason": ""}


def u(reason="not_provided"):
    return {"status": "unknown", "value": None, "reason": reason}


def n(reason="not_applicable"):
    return {"status": "not_applicable", "value": None, "reason": reason}


def clock():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def offset(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def wait_until(value):
    deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    while datetime.now(timezone.utc) < deadline:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(0, min(remaining, 0.2)))


def raw(value):
    return k({"kind": "instant", "value": value})


def ref(value, sid, uri):
    payload = encode(value)
    sha = hashlib.sha256(payload).hexdigest()
    return {"artifact_id": sid + ":" + sha[:24], "schema_id": sid, "schema_version": 1, "media_type": "application/json", "uri": uri, "sha256": sha, "byte_size": len(payload)}


def current(root, account):
    database = root / "data/investment.sqlite"
    if not database.exists():
        return None
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as con:
        row = con.execute("SELECT meta_json FROM ledger_meta WHERE account_id=?", (account,)).fetchone()
        if row is None:
            return None
        trades = [json.loads(r[0]) for r in con.execute("SELECT trade_json FROM trades WHERE account_id=? ORDER BY trade_id", (account,))]
    return {"meta": json.loads(row[0]), "trades": {"rows": trades}}


def build_input(root, account, changes, replies=None):
    root = validate_root(root, fixture_mode=True)
    previous = current(root, account)
    moment = clock()
    run_id, invocation_id = "run-" + uuid.uuid4().hex, "inv-" + uuid.uuid4().hex
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    folder = root / "runs/sc01" / day / invocation_id
    folder.mkdir(parents=True)
    prefix = f"investment-run://sc01/{day}/{invocation_id}/"

    def write(name, value, sid):
        (folder / name).write_bytes(encode(value))
        return ref(value, sid, prefix + name)

    meta = previous["meta"] if previous else None
    opening = meta["opening"] if meta else {"start_at": moment, "positions": "confirmed_empty", "coverage": "recorded_transactions_since_start"}
    fresh = not meta or changes or any(r["result"] != "no_reply" for r in (replies or []))
    fact_time = moment if fresh else meta["known_as_of"]
    as_of = moment if fresh else meta["transaction_as_of"]
    generation = meta["generation"] if meta else 0
    source = {"record_id": "record-" + uuid.uuid4().hex, "source_type": "synthetic_fixture", "operation": "refresh" if meta else "initialize", "account_id": account, "base_generation": generation, "confirmed_at": fact_time, "transaction_as_of": as_of, "platform": "user-confirmed-records", "currency": "CNY", "opening": opening, "changes": {"rows": changes}, "arrival_replies": {"rows": replies or []}}
    source_ref = write("source-record.json", source, "investment.source_record")
    facts_ref = write("transaction-facts.json", {"source_record_ref": source_ref, "base_generation": generation}, "investment.transaction_facts")
    changes_ref = write("changes.json", source["changes"], "investment.trade_changes")
    registry = json.loads((SPEC / "registry.json").read_text(encoding="utf-8"))
    table = next(t for t in registry["tables"] if t["schema_id"] == "investment.trade_changes")
    changes_ref.update(table_schema_ref=ref(registry, "investment.registry", "investment-spec://v1/registry.json"), row_count=len(changes), **{key: table[key] for key in ("primary_key", "units", "null_policy")})
    scope = {"scope_id": "scope-" + uuid.uuid4().hex, "account_id": account, "operation": source["operation"], "transaction_as_of_target": as_of, "account_information_cutoff_at": fact_time, "purpose": "fixture_validation", "requested_uses": ["current_account_review", "reporting"]}
    scope_ref = write("scope.json", scope, "investment.task_scope")
    method = json.loads((SPEC / "method.json").read_text(encoding="utf-8"))
    method_ref = ref(method, "investment.method", "investment-spec://v1/method.json")
    params = {"schema_version": 1, "stage_id": "SC01", "parameters": [{"name": name, "value": value, "unit": unit, "source_kind": kind, "basis_ref": method_ref, "determination_rule": "constant_v1", "determined_at": moment} for name, value, unit, kind in (("max_attempts", 3, "count", "engineering_budget"), ("max_json_bytes", 10485760, "count", "engineering_budget"), ("decimal_precision", 80, "count", "engineering_budget"), ("execution_timezone", "Asia/Shanghai", "code", "registered_business_rule"))]}
    params_ref = write("parameters.json", params, "investment.parameter_set")
    incoming = {"contract_id": "investment.stage_input", "contract_version": 1, "run_id": run_id, "stage_id": "SC01", "invocation_id": invocation_id, "decision_at": offset(600), "information_cutoff_at": fact_time, "scope_ref": scope_ref, "input_refs": [{"role": "transaction_facts_ref", "ref": facts_ref}, {"role": "changes_ref", "ref": changes_ref}, {"role": "task_scope_ref", "ref": scope_ref}], "parameters_ref": params_ref}
    write("submitted-input.json", incoming, "investment.stage_input")
    return folder, incoming


def mutate(trade, old=None, mode="create"):
    return {"change_id": "change-" + uuid.uuid4().hex, "mode": mode, "expected_trade_hash": k(hashlib.sha256(encode(old)).hexdigest()) if old else n("new_trade"), "correction_reason": k("explicit_user_correction") if mode == "correct" else n("ordinary_update"), "trade": trade}


def make_trade(side, shares, amount, pending=False):
    return {"trade_id": "trade-" + uuid.uuid4().hex, "instrument_id": "fund:000001:A", "side": side, "amount": k(amount) if amount is not None else u(), "confirmed_shares": u() if pending or shares is None else k(shares), "requested_shares": k(shares) if side == "sell" and pending and shares is not None else n(), "fee_amount": u(), "trade_at": raw(clock()), "holding_start": u() if side == "buy" else n(), "expected_at": u(), "confirmation": {"state": "pending" if pending else "confirmed", "actual_at": u() if pending else raw(clock()), "basis": "not_applicable" if pending else "user_reported"}, "settlement": {"state": "not_applicable" if side == "buy" else "pending", "actual_at": n() if side == "buy" else u(), "basis": "not_applicable"}, "last_arrival_reply": u()}


def invoke(root, folder, incoming, prepare=False):
    root = validate_root(root, fixture_mode=True)
    env = dict(os.environ, PYTHONPATH=str(PLUGIN / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-B", str(RUNNER), "--root", str(root), "--input", str(folder / "submitted-input.json"), "--run-id", incoming["run_id"], "--invocation-id", incoming["invocation_id"], "--fixture-mode"]
    if prepare:
        cmd.append("--prepare")
    return subprocess.run(cmd, env=env, cwd=PLUGIN, capture_output=True, text=True, encoding="utf-8")


def execute(root, account, changes, label, invocations, success=True, replies=None, prepare=False):
    folder, incoming = build_input(root, account, changes, replies)
    process = invoke(root, folder, incoming, prepare)
    invocations.append({"name": label, "run_id": incoming["run_id"], "invocation_id": incoming["invocation_id"], "exit_code": process.returncode, "input_path": str(folder / "submitted-input.json"), "stdout": process.stdout, "stderr": process.stderr})
    assert (process.returncode == 0) == success, (label, process.stdout, process.stderr)
    result = json.loads(process.stdout)
    if success and not prepare:
        assert result["next_chain_allowed"] is True
        # 编排器已实际执行流程和独立校验；读取本次凭证，不再重复启动校验。
        attempt = result["attempt_no"]
        receipt = json.loads((folder / f"attempt-{attempt:02}" / "receipt.json").read_bytes())
        expected_rules = ["J01", "J02", "J03", "R01", "R02"] + [f"I{n:02}" for n in range(1, 8)] + [f"A{n:02}" for n in range(1, 15)] + ["V02", "U01"]
        assert receipt["overall_verdict"] == "passed" and [c["rule_id"] for c in receipt["checks"]] == expected_rules and all(c["verdict"] == "passed" for c in receipt["checks"])
        assert not (folder / f"attempt-{attempt:02}" / "input-check.json").exists()
    return folder, incoming, result


def reply(trade, result, expected=None):
    return {"trade_id": trade["trade_id"], "result": result, "reported_at": n() if result == "no_reply" else k(clock()), "new_expected_at": expected if expected is not None else n()}


def run_suite(root, invocations, checks):
    account = "real-flow-fixture"
    def check(name, condition):
        assert condition, name
        checks.append({"name": name, "passed": True})
    execute(root, account, [], "initial-zero", invocations)
    check("first_zero", current(root, account)["trades"]["rows"] == [])
    buy = make_trade("buy", None, "4000", True)
    buy["expected_at"] = raw(offset(86400))
    execute(root, account, [mutate(buy)], "submitted-buy", invocations)
    before = encode(current(root, account))
    _, _, prepared = execute(root, account, [], "readonly-prepare", invocations, prepare=True)
    check("prepare_readonly", before == encode(current(root, account)) and len(prepared["questions"]) == 1)
    execute(root, account, [], "early-completed-buy", invocations, replies=[reply(buy, "completed")])
    old = current(root, account)["trades"]["rows"][0]
    check("unknown_is_not_zero", old["confirmation"]["state"] == "confirmed" and old["confirmed_shares"] == u())
    buy = copy.deepcopy(old); buy["confirmed_shares"] = k("4000"); buy["confirmation"]["actual_at"] = raw(clock())
    execute(root, account, [mutate(buy, old, "update")], "supplement-shares", invocations)
    sale = make_trade("sell", "100", "99.8")
    sale["expected_at"] = raw(offset(5))
    execute(root, account, [mutate(sale)], "confirmed-sale-awaiting", invocations)
    wait_until(sale["expected_at"]["value"]["value"])
    execute(root, account, [], "confirmed-sale-due", invocations)
    trades = current(root, account)["trades"]["rows"]
    sale = next(t for t in trades if t["side"] == "sell")
    check("whole_sale_due", sale["settlement"]["state"] == "settled" and sale["settlement"]["basis"] == "expected_due" and sale["settlement"]["actual_at"] == u())
    check("confirmation_basis_independent", sale["confirmation"]["basis"] == "user_reported")
    old = copy.deepcopy(sale); sale["fee_amount"] = k("0.2")
    execute(root, account, [mutate(sale, old, "update")], "fee-only", invocations)
    sale = next(t for t in current(root, account)["trades"]["rows"] if t["side"] == "sell")
    check("fee_does_not_confirm_cash", sale["settlement"]["basis"] == "expected_due")
    execute(root, account, [], "denied-due", invocations, replies=[reply(sale, "not_completed")])
    sale = next(t for t in current(root, account)["trades"]["rows"] if t["side"] == "sell")
    check("denial_only_cash", sale["settlement"]["state"] == "pending" and sale["confirmation"]["state"] == "confirmed" and sale["confirmed_shares"] == k("100") and sale["expected_at"]["status"] == "unknown")
    execute(root, account, [], "no-reply", invocations, replies=[reply(sale, "no_reply")])
    sale2 = next(t for t in current(root, account)["trades"]["rows"] if t["side"] == "sell")
    check("denial_persists", sale2 == sale)
    execute(root, account, [], "postpone", invocations, replies=[reply(sale, "not_completed", raw(offset(86400)))])
    execute(root, account, [], "cash-completed", invocations, replies=[reply(sale, "completed")])
    sale = next(t for t in current(root, account)["trades"]["rows"] if t["side"] == "sell")
    check("whole_cash_once", sale["settlement"]["state"] == "settled" and sale["amount"] == k("99.8"))
    count = len(current(root, account)["trades"]["rows"])
    folder, incoming, result = execute(root, account, [mutate(sale, sale, "update")], "duplicate-notice", invocations)
    baseline = encode(current(root, account)); again = invoke(root, folder, incoming)
    check("replay_exact", again.returncode == 0 and json.loads(again.stdout) == result and encode(current(root, account)) == baseline)
    check("no_duplicate_trade", len(current(root, account)["trades"]["rows"]) == count)
    estimate = make_trade("sell", "1000", None)
    execute(root, account, [mutate(estimate)], "estimate-unknown", invocations)
    old = next(t for t in current(root, account)["trades"]["rows"] if t["trade_id"] == estimate["trade_id"])
    actual = copy.deepcopy(old); actual["amount"] = k("998")
    execute(root, account, [mutate(actual, old, "update")], "actual-998", invocations)
    check("actual_not_estimate", next(t for t in current(root, account)["trades"]["rows"] if t["trade_id"] == actual["trade_id"])["amount"] == k("998"))
    old = copy.deepcopy(actual); invalid = copy.deepcopy(actual); invalid["amount"] = k("1000")
    _, _, failed = execute(root, account, [mutate(invalid, old, "update")], "known-conflict", invocations, success=False)
    check("three_attempts_stop", failed["attempt_count"] == 3 and not failed["next_chain_allowed"])
    check("failed_no_commit", current(root, account)["trades"]["rows"] == sorted(current(root, account)["trades"]["rows"], key=lambda t:t["trade_id"]) and next(t for t in current(root, account)["trades"]["rows"] if t["trade_id"] == actual["trade_id"])["amount"] == k("998"))
    pending = make_trade("buy", None, "20", True); pending["expected_at"] = raw(offset(120))
    execute(root, account, [mutate(pending)], "future-cutoff", invocations)
    check("decision_time_not_cutoff", next(t for t in current(root, account)["trades"]["rows"] if t["trade_id"] == pending["trade_id"])["confirmation"]["state"] == "pending")
    with sqlite3.connect(root / "data/investment.sqlite") as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        check("two_tables", tables == {"trades", "ledger_meta"})
        check("no_snapshots", len(list(con.execute("SELECT * FROM ledger_meta"))) == 1)
        try:
            con.execute("INSERT INTO trades VALUES(?,?,?)", (account, "bad", "{}"))
        except sqlite3.DatabaseError:
            check("unguarded_sql_rejected", True)
        else:
            raise AssertionError("unguarded SQL accepted")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sc01-whole-", dir=test_parent()))
    report = {"schema_version": 1, "status": "failed", "root": str(root), "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "checks": [], "invocations": [], "failure": None}
    try:
        run_suite(root, report["invocations"], report["checks"])
        report["status"] = "passed"
    except Exception as error:
        report["failure"] = repr(error)
    # 报告封闭字段；明细来自本次实际进程执行。
    schema = {"type": "object", "additionalProperties": False, "required": list(report), "properties": {"schema_version": {"const": 1}, "status": {"enum": ["passed", "failed"]}, "root": {"type": "string"}, "verifier_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "checks": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "passed"], "properties": {"name": {"type": "string"}, "passed": {"const": True}}}}, "invocations": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["name", "run_id", "invocation_id", "exit_code", "input_path", "stdout", "stderr"], "properties": {**{k: {"type": "string"} for k in ["name", "run_id", "invocation_id", "input_path", "stdout", "stderr"]}, "exit_code": {"type": "integer"}}}}, "failure": {"type": ["string", "null"]}}}
    Draft202012Validator(schema).validate(report)
    (root / "verification-report.json").write_bytes(encode(report))
    print(json.dumps({"status": report["status"], "root": str(root), "checks": len(report["checks"]), "failure": report["failure"], "verifier_sha256": report["verifier_sha256"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
