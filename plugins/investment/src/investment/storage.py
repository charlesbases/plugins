"""逐笔必要事实与当前提交标记；运行产物不进入数据库。"""
from contextlib import contextmanager
import os
import sqlite3
import subprocess

from investment.chains.account.validation import load_input, verify_output
from investment.contracts import ContractError, OUTPUT_RULES, PLUGIN, SPEC, Schemas, artifact, canonical, digest, instant, require
from investment.references import Resolver
from investment.runtime.paths import validate_root, validate_source_root


class Store:
    """只有真实独立校验结果绑定的事务可写；finalize不改变业务generation。"""

    def __init__(self, root, schemas: Schemas, fixture_mode=None):
        self.root, self.schemas = validate_root(root, fixture_mode), schemas
        directory = self.root / "data"
        directory.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(directory / "investment.sqlite", isolation_level=None, timeout=10)
        self.con.row_factory = sqlite3.Row
        self._permit = None
        self.last_error = None
        self.con.create_function("investment_trade_v1", 3, self._guard(self._trade_guard))
        self.con.create_function("investment_meta_v1", 2, self._guard(self._meta_guard))
        ddl = (SPEC / "storage.sql").read_text(encoding="utf-8")
        try:
            existing = self.con.execute("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1").fetchone()
            if existing is None:
                self.con.executescript(ddl)
            with sqlite3.connect(":memory:") as expected:
                expected.create_function("investment_trade_v1", 3, lambda *args: 1)
                expected.create_function("investment_meta_v1", 2, lambda *args: 1)
                expected.executescript(ddl)
                def layout(con):
                    return {row[0]: " ".join(row[1].split()).lower() for row in con.execute("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL")}
                require(layout(self.con) == layout(expected), "D01", "/schema", "Unsupported database layout; no migration or reset performed", "schema_violation")
        except Exception:
            self.close()
            raise

    def close(self):
        self.con.close()

    def _guard(self, function):
        def check(*args):
            try:
                function(*args)
                return 1
            except (ContractError, ValueError, TypeError, KeyError) as error:
                self.last_error = error
                return 0
        return check

    def _trade_guard(self, account, trade_id, text):
        self.schemas.validate("investment.trade_record", {"account_id": account, "trade_id": trade_id, "trade_json": text})
        trade = self.schemas.document("investment.trade", text.encode())
        require(trade["trade_id"] == trade_id and self._permit is not None and self._permit["mode"] == "commit" and self._permit["account_id"] == account and self._permit["trades"].get(trade_id) == text, "D01", "/trade_json", "No matching independently accepted trade permit", "checks_incomplete")

    def _meta_guard(self, account, text):
        meta = self.schemas.document("investment.ledger_meta", text.encode())
        require(meta["account_id"] == account and self._permit is not None and self._permit["account_id"] == account and self._permit["meta_json"] == text, "D02", "/meta_json", "No matching accepted metadata permit", "checks_incomplete")

    @contextmanager
    def acceptance(self, worker, receipt_raw, state, candidate, input_ref, invocation_id, baseline):
        validate_source_root(self.root, state["source_record"]["source_type"])
        require(isinstance(worker, subprocess.Popen) and worker.returncode == 0 and worker.pid != os.getpid() and str((PLUGIN / "scripts/validate_sc01.py").resolve()) in worker.args and "output" in worker.args and getattr(worker, "investment_output_sha256", None) == digest(receipt_raw), "V01", "/", "Actual independent validator permit required", "checks_incomplete")
        receipt = self.schemas.document("investment.validation_receipt", receipt_raw)
        require(receipt["overall_verdict"] == "passed" and [c["rule_id"] for c in receipt["checks"]] == OUTPUT_RULES and all(c["verdict"] == "passed" for c in receipt["checks"]), "V01", "/checks", "Incomplete independent acceptance", "checks_incomplete")
        resolver = Resolver(self.root, self.schemas, fixture_mode=state["source_record"]["source_type"] == "synthetic_fixture")
        bundle = load_input(input_ref, resolver, candidate["run_id"], invocation_id)
        frozen = resolver.frozen_invocation(input_ref, candidate["run_id"], invocation_id)
        number = int(worker.args[worker.args.index("--attempt-no") + 1])
        require(frozen["processing_state"] == "running" and len(frozen["attempts"]) == number and frozen["attempts"][-1]["state"] == "running" and frozen["attempts"][-1]["phase"] == "commit" and frozen["attempts"][-1]["output_validation_ref"]["value"]["sha256"] == digest(receipt_raw), "V01", "/attempts", "Acceptance must belong to the current committing attempt", "checks_incomplete")
        require(bundle["baseline"] == baseline and verify_output(bundle, receipt["candidate_output_ref"], resolver) == state, "D02", "/", "Acceptance objects or baseline differ")
        require(receipt["input_refs"] == candidate["input_refs"] and receipt["run_id"] == candidate["run_id"] and receipt["stage_id"] == "SC01" and receipt["validator_id"] == self.schemas.registry["validator_id"], "D02", "/", "Receipt binding mismatch")
        require(self._permit is None, "V01", "/", "Acceptance scope already active")
        self._permit = {"mode": "accepted", "account_id": state["context"]["account_id"], "state_sha256": digest(canonical(state)), "candidate_sha256": digest(canonical(candidate)), "receipt_sha256": digest(receipt_raw), "input_ref": input_ref, "invocation_id": invocation_id, "attempt_no": number, "baseline": baseline}
        try:
            yield receipt
        finally:
            self._permit = None

    def commit(self, state, candidate, receipt, stamp, baseline):
        permit = self._permit
        require(permit is not None and permit["mode"] == "accepted" and permit["baseline"] == baseline and permit["state_sha256"] == digest(canonical(state)) and permit["candidate_sha256"] == digest(canonical(candidate)) and permit["receipt_sha256"] == digest(canonical(receipt)), "V01", "/", "Commit is outside actual acceptance scope", "checks_incomplete")
        context = state["context"]
        validate_source_root(self.root, context["source_authority"])
        meta = {key: context[key] for key in ("account_id", "platform", "currency", "opening", "source_authority", "generation", "ledger_digest", "known_as_of", "transaction_as_of")}
        meta["last_commit"] = stamp
        self.schemas.validate("investment.ledger_meta", meta)
        require(stamp["attempt_no"] == permit["attempt_no"] and stamp["execution_date"] == permit["input_ref"]["uri"].split("/")[3], "D02", "/last_commit", "Commit marker run directory or attempt mismatch")
        require(stamp["input_sha256"] == permit["input_ref"]["sha256"] and stamp["candidate_sha256"] == permit["candidate_sha256"] and stamp["receipt_sha256"] == permit["receipt_sha256"] and stamp["invocation_id"] == permit["invocation_id"] and stamp["run_id"] == candidate["run_id"] and stamp["finalized"] is False and instant(stamp["sealed_at"]) >= instant(receipt["validated_at"]), "D02", "/last_commit", "Commit marker binding mismatch")
        trades = {t["trade_id"]: canonical(t).decode() for t in state["business_objects"]["trades"]["rows"]}
        account, text = context["account_id"], canonical(meta).decode()
        self.last_error = None
        try:
            self.con.execute("BEGIN IMMEDIATE")
            current = Resolver(self.root, self.schemas).current(account)
            require(current == baseline, "D03", "/base_generation", "Frozen ledger changed before commit", "parent_changed")
            require(current["meta"] is None or current["meta"]["last_commit"]["finalized"], "D04", "/last_commit", "Already committed; recover before submitting new work")
            self._permit.update(mode="commit", trades=trades, meta_json=text)
            for tid, trade in trades.items():
                self.con.execute("INSERT INTO trades VALUES(?,?,?) ON CONFLICT(account_id,trade_id) DO UPDATE SET trade_json=excluded.trade_json", (account, tid, trade))
            self.con.execute("INSERT INTO ledger_meta VALUES(?,?) ON CONFLICT(account_id) DO UPDATE SET meta_json=excluded.meta_json", (account, text))
            actual = [r[0] for r in self.con.execute("SELECT trade_json FROM trades WHERE account_id=? ORDER BY trade_id", (account,))]
            require(actual == [trades[k] for k in sorted(trades)], "D03", "/trades", "Committed trade set differs")
            self.con.execute("COMMIT")
        except Exception:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            if isinstance(self.last_error, ContractError):
                raise self.last_error
            raise
        return meta

    def finalize(self, account, stamp, sealed):
        current = Resolver(self.root, self.schemas).current(account)
        require(current["meta"] is not None and current["meta"]["last_commit"] == stamp, "D04", "/last_commit", "Commit marker changed during recovery", "parent_changed")
        validate_source_root(self.root, current["meta"]["source_authority"])
        self.schemas.validate("investment.sealed_output", sealed)
        prefix = f"investment-run://sc01/{stamp['execution_date']}/{stamp['invocation_id']}/"
        resolver = Resolver(self.root, self.schemas, fixture_mode=current["meta"]["source_authority"] == "synthetic_fixture")
        candidate = resolver.resolve(sealed["candidate_output_ref"], "investment.candidate_output")
        receipt = resolver.resolve(sealed["validation_receipt_ref"], "investment.validation_receipt")
        require(sealed["sealed_at"] == stamp["sealed_at"] and sealed["run_id"] == stamp["run_id"] == candidate["run_id"] and sealed["output_id"] == candidate["output_id"] and sealed["candidate_output_ref"] == receipt["candidate_output_ref"] and digest(canonical(candidate)) == stamp["candidate_sha256"] and digest(canonical(receipt)) == stamp["receipt_sha256"], "D04", "/last_commit", "Recovery artifacts differ from committed marker")
        input_ref = artifact(resolver.read(prefix + "input.json"), "investment.stage_input", prefix + "input.json")
        require(input_ref["sha256"] == stamp["input_sha256"], "D04", "/input_sha256", "Recovery input changed")
        state = verify_output(load_input(input_ref, resolver, stamp["run_id"], stamp["invocation_id"]), sealed["candidate_output_ref"], resolver)
        require(state["context"]["ledger_digest"] == current["meta"]["ledger_digest"], "D04", "/ledger_digest", "Recovered result differs from committed ledger")
        if stamp["finalized"]:
            return
        meta = dict(current["meta"], last_commit=dict(stamp, finalized=True))
        text = canonical(meta).decode()
        require(self._permit is None, "D04", "/", "Cannot finalize during another write")
        self._permit = {"mode": "finalize", "account_id": account, "meta_json": text}
        try:
            self.con.execute("BEGIN IMMEDIATE")
            row = self.con.execute("SELECT meta_json FROM ledger_meta WHERE account_id=?", (account,)).fetchone()
            require(row is not None and row[0] == canonical(current["meta"]).decode(), "D03", "/last_commit", "Finalize CAS failed", "parent_changed")
            self.con.execute("UPDATE ledger_meta SET meta_json=? WHERE account_id=?", (text, account))
            self.con.execute("COMMIT")
        except Exception:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            raise
        finally:
            self._permit = None
