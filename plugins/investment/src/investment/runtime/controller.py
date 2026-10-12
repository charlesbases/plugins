"""正式调度：冻结真实账本、独立进程验收、三次预算和提交后恢复。"""
import os
import sqlite3
import subprocess
import sys

from investment.contracts import (ContractError, OUTPUT_RULES, PLUGIN, SPEC, Schemas, artifact, canonical, digest, is_known, known, na, now, parse, require, unknown)
from investment.references import Resolver
from investment.runtime import runs
from investment.runtime.paths import validate_input_path, validate_root
from investment.storage import Store


def worker(phase, root, input_path, run_id, invocation_id, number, schemas, fixture_mode, directory):
    require(phase == "output", "V01", "/phase", "Only final independent validation is scheduled")
    command = [sys.executable, "-B", str((PLUGIN / "scripts/validate_sc01.py").resolve()), "--phase", phase, "--root", str(root), "--input", str(input_path), "--run-id", run_id, "--invocation-id", invocation_id, "--attempt-no", str(number)]
    if phase == "output":
        command += ["--candidate", str(directory / "candidate.json")]
    if fixture_mode:
        command.append("--fixture-mode")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
    print(f"Independent validator pid={process.pid} phase={phase}", file=sys.stderr, flush=True)
    try:
        output, diagnostic = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as error:
        process.kill(); process.communicate()
        raise ContractError("V01", "/", "Independent validator timed out", "validator_exception") from error
    require(process.returncode in (0, 1), "V01", "/", "Independent validator failed: " + diagnostic.decode("utf-8", errors="replace")[:800], "validator_exception")
    result = schemas.document("investment.validation_receipt", output)
    require(result["run_id"] == run_id and result["stage_id"] == "SC01" and result["validator_id"] == schemas.registry["validator_id"] and result["validator_version"] == "v1", "V01", "/", "Foreign validator result", "identity_mismatch")
    failure = schemas.document("investment.failure", diagnostic) if diagnostic else None
    require((process.returncode == 0) == (result["overall_verdict"] == "passed") == (failure is None), "V01", "/", "Validator verdict, exit and failure disagree", "checks_incomplete")
    if failure is None:
        require([c["rule_id"] for c in result["checks"]] == OUTPUT_RULES and all(c["verdict"] == "passed" for c in result["checks"]), "V01", "/checks", "Incomplete independent checks", "checks_incomplete")
    if failure:
        require((failure["run_id"], failure["invocation_id"], failure["attempt_no"]) == (run_id, invocation_id, number), "V01", "/", "Foreign failure detail")
    process.investment_output_sha256 = digest(output)
    return process, output, result, failure


def run_process(root, input_path, run_id, invocation_id, number, schemas, fixture_mode, directory):
    """真实流程子进程仅生成候选，不能取得验收或数据库写入许可。"""
    command = [sys.executable, "-B", str((PLUGIN / "scripts/process_sc01.py").resolve()), "--root", str(root), "--input", str(input_path), "--run-id", run_id, "--invocation-id", invocation_id, "--attempt-no", str(number)]
    if fixture_mode:
        command.append("--fixture-mode")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"))
    print(f"Process pid={process.pid} attempt={number}", file=sys.stderr, flush=True)
    try:
        output, diagnostic = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as error:
        process.kill(); process.communicate()
        raise ContractError("A02", "/", "Process timed out", "producer_exception") from error
    require(process.returncode in (0, 1), "A02", "/", "Process failed: " + diagnostic.decode("utf-8", errors="replace")[:800], "producer_exception")
    if diagnostic:
        print(diagnostic.decode("utf-8", errors="replace"), file=sys.stderr, end="", flush=True)
    if process.returncode == 1:
        failure = schemas.document("investment.failure", output)
        require((failure["run_id"], failure["invocation_id"], failure["attempt_no"], failure["phase"], failure["owner"]) == (run_id, invocation_id, number, "production", "producer"), "T04", "/failure_ref", "Foreign process failure")
        return process, None, failure
    reference = schemas.validate_type("ArtifactRef", parse(output))
    require(reference["schema_id"] == "investment.candidate_output" and reference["uri"] == runs.run_uri(root, directory / "candidate.json"), "R01", "/candidate_ref", "Process returned another attempt's candidate", "identity_mismatch")
    Resolver(root, schemas, fixture_mode=fixture_mode).resolve(reference, "investment.candidate_output", binding_only=True)
    return process, reference, None


class Controller:
    """单根目录互斥保护一次冻结与提交；等待用户答复发生在本入口之前。"""
    def __init__(self, root, fixture_mode=False):
        self.root, self.schemas, self.fixture_mode = validate_root(root, fixture_mode), Schemas(), fixture_mode

    def _result(self, inv):
        resolver = Resolver(self.root, self.schemas, fixture_mode=self.fixture_mode)
        sealed = resolver.resolve(inv["sealed_output_ref"]["value"], "investment.sealed_output")
        receipt = resolver.resolve(sealed["validation_receipt_ref"], "investment.validation_receipt")
        result = {"schema_version": 1, "status": "completed", "run_id": inv["run_id"], "stage_id": "SC01", "invocation_id": inv["invocation_id"], "attempt_no": inv["selected_attempt"]["value"], "next_chain_allowed": True, "sealed_output_ref": inv["sealed_output_ref"]["value"], "allowed_uses": receipt["allowed_uses"]}
        self.schemas.validate("investment.run_result", result)
        return result

    def _complete(self, store, manifest, inv, meta):
        stamp = meta["last_commit"]
        day = manifest["execution_date"]
        base = self.root / "runs/sc01" / day / inv["invocation_id"]
        directory = base / f"attempt-{stamp['attempt_no']:02}"
        resolver = Resolver(self.root, self.schemas, fixture_mode=self.fixture_mode)
        require(stamp["input_sha256"] == inv["frozen_input_ref"]["sha256"] and stamp["run_id"] == inv["run_id"] and stamp["invocation_id"] == inv["invocation_id"], "D04", "/last_commit", "Already committed; recovery input binding differs")
        candidate_raw, receipt_raw = (directory / "candidate.json").read_bytes(), (directory / "receipt.json").read_bytes()
        require(digest(candidate_raw) == stamp["candidate_sha256"] and digest(receipt_raw) == stamp["receipt_sha256"], "D04", "/last_commit", "Already committed; recovery artifacts changed")
        candidate = self.schemas.document("investment.candidate_output", candidate_raw)
        receipt = self.schemas.document("investment.validation_receipt", receipt_raw)
        candidate_ref = artifact(candidate_raw, "investment.candidate_output", runs.run_uri(self.root, directory / "candidate.json"))
        receipt_ref = artifact(receipt_raw, "investment.validation_receipt", runs.run_uri(self.root, directory / "receipt.json"))
        require(receipt["candidate_output_ref"] == candidate_ref, "D04", "/last_commit", "Already committed; recovery receipt binding differs")
        sealed = {"run_id": inv["run_id"], "stage_id": "SC01", "output_id": candidate["output_id"], "candidate_output_ref": candidate_ref, "validation_receipt_ref": receipt_ref, "sealed_at": stamp["sealed_at"]}
        sealed_ref = runs.put(self.root, directory / "sealed.json", sealed, "investment.sealed_output", self.schemas)
        attempt = inv["attempts"][stamp["attempt_no"] - 1]
        execution = {"run_id": inv["run_id"], "stage_id": "SC01", "invocation_id": inv["invocation_id"], "input_ref": inv["frozen_input_ref"], "processing_state": "completed", "started_at": attempt["started_at"], "finished_at": known(stamp["sealed_at"]), "failure_ref": na()}
        execution_ref = runs.put(self.root, directory / "execution.json", execution, "investment.execution_record", self.schemas)
        attempt.update(state="passed", phase="finished", finished_at=known(stamp["sealed_at"]), candidate_ref=known(candidate_ref), output_validation_ref=known(receipt_ref), sealed_ref=known(sealed_ref), execution_record_ref=known(execution_ref), failure_ref=na())
        inv.update(processing_state="completed", validation_state="passed", finished_at=known(stamp["sealed_at"]), selected_attempt=known(stamp["attempt_no"]), sealed_output_ref=known(sealed_ref), next_chain_allowed=True)
        inv["retention"] = {"state": "eligible", "blockers": [], "persistent_refs": [], "evaluated_at": known(stamp["sealed_at"])}
        runs.save_manifest(self.root, manifest, self.schemas)
        store.finalize(meta["account_id"], stamp, sealed)
        return self._result(inv)

    def _recover_pending(self, store):
        for row in store.con.execute("SELECT meta_json FROM ledger_meta").fetchall():
            meta = self.schemas.document("investment.ledger_meta", row[0].encode())
            stamp = meta["last_commit"]
            if stamp["finalized"]:
                continue
            try:
                manifest = runs.load_manifest(self.root, stamp["execution_date"], self.schemas)
                inv = next(v for v in manifest["invocations"] if v["invocation_id"] == stamp["invocation_id"])
                self._complete(store, manifest, inv, meta)
            except (OSError, StopIteration, ContractError) as error:
                raise ContractError("D04", "/last_commit", "Already committed; bound recovery material unavailable or invalid; do not resubmit: " + str(error), "missing_evidence") from error

    def _failure(self, error, inv, number, phase):
        contract = isinstance(error, ContractError)
        rule = error.rule if contract else "D01" if phase == "commit" else "A02"
        code = error.code if contract else "write_failed" if phase == "commit" else "producer_exception"
        reason = error.reason if contract else str(error)[:1000] or type(error).__name__
        return {"schema_version": 1, "failure_id": f"failure-{inv['invocation_id']}-{number}", "run_id": inv["run_id"], "stage_id": "SC01", "invocation_id": inv["invocation_id"], "attempt_no": number, "phase": phase, "detected_at": now(), "category": "storage" if phase == "commit" else "production" if phase == "production" else "independent_validation", "rule_id": rule, "reason_code": code, "object_ref": known(inv["frozen_input_ref"]), "json_pointer": known(error.pointer if contract else "/"), "reason": reason, "evidence_refs": [inv["frozen_input_ref"]], "affected_uses": ["current_account_review", "reporting"], "owner": "storage" if phase == "commit" else "controller", "gaps": []}

    def run(self, input_path, run_id, invocation_id):
        input_path = validate_input_path(self.root, input_path)
        for identifier in (run_id, invocation_id):
            self.schemas.validate_type("PathID", identifier)
        with runs.root_lock(self.root):
            store = Store(self.root, self.schemas, fixture_mode=self.fixture_mode)
            try:
                self._recover_pending(store)
                raw = input_path.read_bytes()
                return self._run_locked(store, raw, run_id, invocation_id)
            finally:
                store.close()

    def _run_locked(self, store, raw, run_id, invocation_id):
        existing = []
        for path in sorted((self.root / "runs/sc01").glob("*/manifest.json")):
            manifest = self.schemas.document("investment.daily_manifest", path.read_bytes())
            runs.resume_cleanups(self.root, manifest, self.schemas, store)
            existing += [(manifest, inv) for inv in manifest["invocations"] if inv["invocation_id"] == invocation_id]
        require(len(existing) <= 1, "T03", "/invocation_id", "Invocation appears in multiple manifests")
        if existing:
            manifest, inv = existing[0]
            require(inv["run_id"] == run_id and inv["frozen_input_ref"]["sha256"] == digest(raw), "T03", "/frozen_input_ref", "Frozen invocation cannot change input or identity", "identity_mismatch")
            require(inv["retention"]["state"] != "cleaned", "T03", "/retention", "Historical run was cleaned; submit a new invocation using stable trade IDs")
            runs.save_manifest(self.root, manifest, self.schemas)
            if inv["processing_state"] == "completed":
                return 0, self._result(inv)
            if inv["processing_state"] == "failed":
                return 1, Resolver(self.root, self.schemas).resolve(inv["terminal_failure_ref"]["value"])
        else:
            created = now(); day = runs.execution_date(created)
            base = self.root / "runs/sc01" / day / invocation_id
            input_ref = artifact(raw, "investment.stage_input", runs.run_uri(self.root, base / "input.json"))
            runs.atomic(base / "input.json", raw, immutable=True)
            method_ref = artifact((SPEC / "method.json").read_bytes(), "investment.method", "investment-spec://v1/method.json")
            runs.atomic(base / "method.json", (SPEC / "method.json").read_bytes(), immutable=True)
            baseline, account = {"meta": None, "trades": {"rows": []}}, unknown()
            active_refs = []
            params_ref = method_ref
            try:
                resolver = Resolver(self.root, self.schemas, fixture_mode=self.fixture_mode)
                incoming = resolver.resolve(input_ref)
                params_ref = incoming["parameters_ref"]
                ref_keys = ("artifact_id", "schema_id", "schema_version", "media_type", "uri", "sha256", "byte_size")
                active_refs = [{key: r["ref"][key] for key in ref_keys} for r in incoming["input_refs"]] + [incoming["parameters_ref"]]
                roles = {r["role"]: r["ref"] for r in incoming["input_refs"]}
                facts = resolver.resolve(roles["transaction_facts_ref"], "investment.transaction_facts")
                active_refs.append(facts["source_record_ref"])
                source = resolver.resolve(facts["source_record_ref"], "investment.source_record")
                account = known(source["account_id"])
                baseline = resolver.current(source["account_id"])
            except (ContractError, KeyError):
                pass
            baseline_ref = runs.put(self.root, base / "baseline.json", baseline, "investment.ledger_baseline", self.schemas)
            inv = {"run_id": run_id, "invocation_id": invocation_id, "account_id": account, "created_at": created, "finished_at": unknown(), "frozen_input_ref": input_ref, "parameters_ref": params_ref, "method_ref": method_ref, "baseline_ref": baseline_ref, "expected_generation": baseline["meta"]["generation"] if baseline["meta"] else 0, "processing_state": "pending", "validation_state": "not_evaluated", "attempts": [], "selected_attempt": na(), "sealed_output_ref": na(), "next_chain_allowed": False, "terminal_failure_ref": na(), "files": [], "retention": {"state": "retain", "blockers": ["attempt_unfinished"], "persistent_refs": active_refs, "evaluated_at": unknown()}}
            manifest = runs.load_manifest(self.root, day, self.schemas)
            manifest["invocations"].append(inv)
            runs.save_manifest(self.root, manifest, self.schemas)
        day = manifest["execution_date"]; base = self.root / "runs/sc01" / day / invocation_id
        input_ref = inv["frozen_input_ref"]
        # 中断的未提交尝试占用原预算，不能凭重启获得第四次。
        if inv["attempts"] and inv["attempts"][-1]["state"] == "running":
            attempt = inv["attempts"][-1]
            self._finish_failure(manifest, inv, attempt, self._failure(ContractError("V01", "/", "Attempt interrupted before commit", "validator_exception"), inv, attempt["attempt_no"], attempt["phase"]), base)
        while len(inv["attempts"]) < 3:
            number = len(inv["attempts"]) + 1
            directory = base / f"attempt-{number:02}"
            attempt = {"attempt_no": number, "directory": directory.name, "state": "running", "phase": "production", "started_at": known(now()), "finished_at": unknown(), "files": [], **{key: na() for key in ("input_check_ref", "candidate_ref", "output_validation_ref", "sealed_ref", "execution_record_ref", "failure_ref")}}
            inv["attempts"].append(attempt); inv.update(processing_state="running", validation_state="checking")
            runs.save_manifest(self.root, manifest, self.schemas)
            failure, committed = None, False
            try:
                _, candidate_ref, failure = run_process(self.root, base / "input.json", run_id, invocation_id, number, self.schemas, self.fixture_mode, directory)
                if failure is None:
                    attempt["candidate_ref"] = known(candidate_ref)
                    attempt["phase"] = "output_check"
                    runs.save_manifest(self.root, manifest, self.schemas)
                    process, receipt_raw, receipt, failure = worker("output", self.root, base / "input.json", run_id, invocation_id, number, self.schemas, self.fixture_mode, directory)
                    attempt["output_validation_ref"] = known(runs.put(self.root, directory / "receipt.json", receipt, "investment.validation_receipt", self.schemas))
                    if failure is None:
                        resolver = Resolver(self.root, self.schemas, fixture_mode=self.fixture_mode)
                        candidate = resolver.resolve(candidate_ref, "investment.candidate_output")
                        state = self.schemas.document("investment.staged_transactions", (directory / "state.json").read_bytes())
                        baseline = resolver.resolve(inv["baseline_ref"], "investment.ledger_baseline")
                        attempt["phase"] = "commit"
                        runs.save_manifest(self.root, manifest, self.schemas)
                        stamp = {"run_id": run_id, "invocation_id": invocation_id, "execution_date": day, "attempt_no": number, "input_sha256": input_ref["sha256"], "candidate_sha256": attempt["candidate_ref"]["value"]["sha256"], "receipt_sha256": digest(receipt_raw), "sealed_at": now(), "finalized": False}
                        with store.acceptance(process, receipt_raw, state, candidate, input_ref, invocation_id, baseline):
                            meta = store.commit(state, candidate, receipt, stamp, baseline)
                        committed = True
            except (ContractError, OSError, ValueError, sqlite3.DatabaseError) as error:
                failure = self._failure(error, inv, number, attempt["phase"])
            if committed:
                return 0, self._complete(store, manifest, inv, meta)
            self._finish_failure(manifest, inv, attempt, failure, base)
        summary = {"schema_version": 1, "run_id": run_id, "stage_id": "SC01", "invocation_id": invocation_id, "status": "failed", "attempt_count": 3, "failure_refs": [a["failure_ref"]["value"] for a in inv["attempts"]], "terminated_at": now(), "next_chain_allowed": False, "termination_code": "attempts_exhausted"}
        summary_ref = runs.put(self.root, base / "terminal-failure.json", summary, "investment.failure_summary", self.schemas)
        inv.update(processing_state="failed", validation_state="exhausted", finished_at=known(summary["terminated_at"]), terminal_failure_ref=known(summary_ref))
        inv["retention"] = {"state": "eligible", "blockers": [], "persistent_refs": [], "evaluated_at": known(summary["terminated_at"])}
        runs.save_manifest(self.root, manifest, self.schemas)
        return 1, summary

    def _finish_failure(self, manifest, inv, attempt, failure, base):
        require(failure is not None, "T04", "/failure_ref", "Failed attempt requires structured reason")
        directory = base / attempt["directory"]
        failure_ref = runs.put(self.root, directory / "failure.json", failure, "investment.failure", self.schemas)
        finished = now()
        execution = {"run_id": inv["run_id"], "stage_id": "SC01", "invocation_id": inv["invocation_id"], "input_ref": inv["frozen_input_ref"], "processing_state": "failed", "started_at": attempt["started_at"], "finished_at": known(finished), "failure_ref": known(failure_ref)}
        execution_ref = runs.put(self.root, directory / "execution.json", execution, "investment.execution_record", self.schemas)
        attempt.update(state="failed", phase="finished", finished_at=known(finished), execution_record_ref=known(execution_ref), failure_ref=known(failure_ref))
        runs.save_manifest(self.root, manifest, self.schemas)
