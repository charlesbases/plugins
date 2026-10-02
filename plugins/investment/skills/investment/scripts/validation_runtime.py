"""Persistent producer retries gated by caller-owned validation contracts.

Validators return a nonempty ``scope`` and ``checks`` with status ``passed``
or ``partial``. Partial results additionally declare ``readiness.trade_ready``
as False and nonempty ``required_actions``. This contract does not turn a
producer's own status into validation evidence.
"""
from dataclasses import asdict, dataclass
import traceback
import uuid

from artifacts import Artifacts
from contracts import (ContractError, EvidenceError, StaleSnapshot,
                       canonical_bytes, fingerprint, strict_json_loads, utc_now)
from state_store import LeaseLost


@dataclass(frozen=True)
class StageAttempt:
    number: int
    namespace: str
    operation_id: str
    round_no: int


class StageValidationFailure(ContractError):
    code = "stage_validation_failed"

    def __init__(self, stage, required_actions, attempts, evidence, *, round_no,
                 cause=None, failure_phase=None, cause_info=None):
        super().__init__(f"Stage {stage} exhausted validation attempts in round {round_no}")
        self.stage, self.round_no = stage, round_no
        self.required_actions = required_actions
        self.attempts, self.evidence = attempts, evidence
        self.cause = cause
        self.failure_phase, self.cause_info = failure_phase, cause_info

    def as_dict(self):
        return {"code": self.code, "stage": self.stage, "round_no": self.round_no,
                "message": str(self), "required_actions": self.required_actions,
                "attempts": self.attempts, "evidence": self.evidence,
                "failure_phase": self.failure_phase, "cause": self.cause_info}

    @classmethod
    def from_dict(cls, value):
        """Restore structured evidence without inventing a live error object."""
        return cls(value["stage"], value["required_actions"], value["attempts"],
                   value["evidence"], round_no=value["round_no"],
                   failure_phase=value.get("failure_phase"), cause_info=value.get("cause"))


def _copy(value):
    return strict_json_loads(canonical_bytes(value).decode("utf-8"))


def _error_info(exc):
    return {"type": type(exc).__name__, "code": getattr(exc, "code", None),
            "message": str(exc)}


def validate_result(result):
    """Enforce the callback evidence envelope, without evaluating its checks."""
    canonical_bytes(result)
    if (type(result) is not dict or result.get("status") not in ("passed", "partial")
            or type(result.get("scope")) is not str or not result["scope"]
            or type(result.get("checks")) not in (dict, list) or not result["checks"]):
        raise ContractError("Validator requires passed/partial status, scope and checks")
    if result["status"] == "partial":
        readiness = result.get("readiness")
        if (type(readiness) is not dict or readiness.get("trade_ready") is not False
                or type(result.get("required_actions")) is not list
                or not result["required_actions"]):
            raise ContractError("Partial validation requires non-trade readiness and required actions")
    return result


class StageRuntime:
    """One runtime round within an existing Store operation lease.

    Inputs are frozen per stage and round, while code and validator identities
    are frozen for the operation. Changing a round is reserved for the caller's
    final validation loop; force does not reset the persisted attempt budget.
    ``dependencies`` contains stage names that must already be accepted.
    """

    def __init__(self, store, artifacts=None, *, code_identity,
                 validator_contracts, round_no=1, max_attempts=3):
        if type(round_no) is not int or not 1 <= round_no <= 3:
            raise ValueError("Validation round must be between one and three")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("Stage attempt budget must be between one and three")
        self.store = store
        self.artifacts = artifacts or Artifacts(store.base)
        self.code_identity = _copy(code_identity)
        self.validator_contracts = _copy(validator_contracts)
        self.round_no, self.max_attempts = round_no, max_attempts

    def _stage_key(self, name):
        if type(name) is not str or not name:
            raise ContractError("Stage name must be a nonempty string")
        context = self.store.assert_owned()
        return fingerprint({"operation_id": context.operation_id, "stage": name})

    def _contract(self, name):
        contract = self.validator_contracts.get(name)
        if (type(contract) is not dict or set(contract) != {"name", "version"}
                or any(type(contract[key]) is not str or not contract[key]
                       for key in ("name", "version"))):
            raise ContractError(f"Stage {name} requires a trusted validator name/version")
        return contract

    def _bind(self, name, inputs, dependencies):
        stage_key, contract = self._stage_key(name), self._contract(name)
        context = self.store.require_context()
        frozen = _copy(inputs)
        binding = {"operation_id": context.operation_id, "stage": name,
                   "round_no": self.round_no, "inputs_hash": fingerprint(frozen),
                   "code_identity_hash": fingerprint(self.code_identity),
                   "validator": contract, "dependencies": list(dependencies),
                   "max_attempts": self.max_attempts}
        key = f"{stage_key}:r{self.round_no}"
        with self.store.transaction() as conn:
            prior = self.store.get("validation_binding", key, conn)
            # Content-addressed paths contain a month. Resume keeps the sealed
            # reference even when the writer would now choose another path.
            binding["inputs_ref"] = (prior["inputs_ref"] if prior is not None
                                     else self.artifacts.put_json(frozen))
            self.store.put("validation_code", fingerprint(context.operation_id),
                           {"operation_id": context.operation_id,
                            "code_identity": self.code_identity}, conn=conn)
            self.store.put("validation_contract", stage_key, contract, conn=conn)
            self.store.put("validation_binding", key, binding, conn=conn)
        return stage_key, key, binding

    def _capture(self, value):
        try:
            return self.artifacts.put_json(value), True
        except (TypeError, ValueError):
            return {**self.artifacts.put_bytes(repr(value).encode("utf-8")),
                    "media_type": "text/x-python-repr"}, False

    def _exception(self, exc):
        return self.artifacts.put_json({**_error_info(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            "required_actions": getattr(exc, "required_actions", [])})

    def _validate(self, validator, value, inputs, evidence):
        before_value, before_inputs = fingerprint(value), fingerprint(inputs)
        result = validator(value, inputs)
        evidence["validation_ref"], serializable = self._capture(result)
        if not serializable:
            raise ContractError("Validator result must contain finite JSON values")
        if fingerprint(value) != before_value or fingerprint(inputs) != before_inputs:
            raise ContractError("Validator must not alter the value or frozen inputs")
        if type(result) is dict and type(result.get("required_actions")) is list:
            evidence["required_actions"] = result["required_actions"]
        return validate_result(result)

    def _cas_accepted(self, stage_key, expected, replacement, conn):
        current = self.store.get("validation_accepted", stage_key, conn)
        if fingerprint(current) != fingerprint(expected):
            raise StaleSnapshot("Accepted validation pointer changed before publication")
        self.store.put("validation_accepted", stage_key, replacement, False, conn)

    def accepted(self, name):
        pointer = self.store.get("validation_accepted", self._stage_key(name))
        return pointer if (pointer and pointer["status"] == "accepted"
                           and pointer.get("validated_round_no", pointer["round_no"]) == self.round_no) else None

    def _dependencies(self, dependencies):
        for name in dependencies:
            if self.accepted(name) is None:
                evidence = self.evidence(name)
                raise StageValidationFailure(name,
                    [{"action": "validate_dependency", "stage": name}],
                    evidence["attempts"], evidence, round_no=self.round_no)

    def _cache(self, stage_key, key, binding, validator, expected):
        evidence = {"operation_id": binding["operation_id"], "stage": binding["stage"],
                    "round_no": self.round_no, "accepted_pointer": expected, "at": utc_now()}
        terminal = None
        try:
            value = self.artifacts.read_json(expected["value_ref"])
            evidence["value_ref"] = expected["value_ref"]
            inputs = self.artifacts.read_json(binding["inputs_ref"])
            self.store.assert_owned()
            checked = self._validate(validator, value, inputs, evidence)
            evidence["status"] = checked["status"]
        except LeaseLost:
            raise
        except Exception as exc:
            evidence["status"] = "failed"
            evidence["failure_phase"], evidence["error"] = "validator", _error_info(exc)
            evidence["exception_ref"] = self._exception(exc)
            if isinstance(exc, StageValidationFailure):
                terminal = exc
                evidence["terminal_failure_ref"] = self.artifacts.put_json(exc.as_dict())
            if getattr(exc, "required_actions", None):
                evidence["required_actions"] = exc.required_actions
        with self.store.transaction() as conn:
            self.store.put("validation_cache_review", key + ":" + uuid.uuid4().hex,
                           evidence, conn=conn)
            if evidence["status"] == "failed":
                invalidated = {**expected, "status": "invalidated",
                               "invalidation_ref": self.artifacts.put_json(evidence)}
                self._cas_accepted(stage_key, expected, invalidated, conn)
            else:
                refreshed = {**expected, "validated_round_no": self.round_no,
                             "validation_ref": evidence["validation_ref"],
                             "validation_status": evidence["status"],
                             "cache_review_ref": self.artifacts.put_json(evidence)}
                self._cas_accepted(stage_key, expected, refreshed, conn)
        if terminal is not None:
            raise terminal
        if evidence["status"] == "failed":
            return False, None, invalidated
        return True, value, refreshed

    def _reserve(self, stage_key, key, binding):
        with self.store.transaction() as conn:
            budget = self.store.get("validation_budget", key, conn) or {"used": 0}
            # A recorded start consumes its budget even if the process died.
            for number in range(1, budget["used"] + 1):
                attempt_key = f"{key}:a{number}"
                if self.store.get("validation_attempt_start", attempt_key, conn) is None:
                    raise EvidenceError("Persisted validation budget lacks attempt evidence")
                if self.store.get("validation_attempt_end", attempt_key, conn) is None:
                    self.store.put("validation_attempt_end", attempt_key,
                        {"status": "interrupted", "finished_at": utc_now(),
                         "exception_ref": self.artifacts.put_json({"type": "InterruptedAttempt",
                             "message": "Attempt started without a committed outcome"})}, conn=conn)
            if budget["used"] >= self.max_attempts:
                return None
            number = budget["used"] + 1
            operation_id = binding["operation_id"]
            namespace = (operation_id if self.round_no == 1 and number == 1 else
                         f"{operation_id}:validation:{stage_key[:16]}:r{self.round_no}:a{number}")
            attempt = StageAttempt(number, namespace, operation_id, self.round_no)
            self.store.put("validation_attempt_start", f"{key}:a{number}",
                {**asdict(attempt), "stage": binding["stage"], "binding_key": key,
                 "started_at": utc_now()}, conn=conn)
            self.store.put("validation_budget", key, {"used": number}, False, conn)
        return attempt

    def _terminal_failure(self, name):
        evidence = self.evidence(name)
        outcomes = [attempt["end"] or {} for attempt in evidence["attempts"]]
        for outcome in outcomes + evidence["cache_reviews"]:
            if "terminal_failure_ref" in outcome:
                return StageValidationFailure.from_dict(
                    self.artifacts.read_json(outcome["terminal_failure_ref"]))
        return None

    def _failure(self, name, *, cause=None, failure_phase=None):
        evidence = self.evidence(name)
        actions = []
        outcomes = [attempt["end"] or {} for attempt in evidence["attempts"]]
        for end in outcomes + evidence["cache_reviews"]:
            for action in end.get("required_actions", []):
                if action not in actions:
                    actions.append(action)
        if not actions:
            actions = [{"action": "resolve_stage_validation_failure", "stage": name,
                        "round_no": self.round_no}]
        failed = [outcome for outcome in outcomes + evidence["cache_reviews"]
                  if outcome.get("status") in ("failed", "interrupted")]
        latest = max(failed, key=lambda outcome: outcome.get("finished_at", outcome.get("at", "")),
                     default={})
        if cause is not None:
            cause_info = _error_info(cause)
        else:
            cause_info = latest.get("error")
            if cause_info is None and "exception_ref" in latest:
                raw = self.artifacts.read_json(latest["exception_ref"])
                cause_info = {key: raw.get(key) for key in ("type", "code", "message")}
            failure_phase = latest.get("failure_phase")
        return StageValidationFailure(name, actions, evidence["attempts"], evidence,
            round_no=self.round_no, cause=cause, failure_phase=failure_phase, cause_info=cause_info)

    def run(self, name, inputs, producer, validator, *, force=False, dependencies=()):
        if not callable(producer) or not callable(validator):
            raise ContractError("Stage producer and validator must be callable")
        if type(force) is not bool or type(dependencies) not in (tuple, list):
            raise ContractError("Invalid force or dependencies")
        if any(type(dependency) is not str or not dependency for dependency in dependencies):
            raise ContractError("Dependency names must be nonempty strings")
        self._dependencies(dependencies)
        stage_key, key, binding = self._bind(name, inputs, dependencies)
        terminal = self._terminal_failure(name)
        if terminal is not None:
            raise terminal
        expected = self.store.get("validation_accepted", stage_key)
        compatible = (expected and expected["status"] == "accepted"
                and expected["inputs_hash"] == binding["inputs_hash"]
                and expected["code_identity_hash"] == binding["code_identity_hash"]
                and expected["validator"] == binding["validator"])
        if not force and compatible:
            hit, value, expected = self._cache(stage_key, key, binding, validator, expected)
            if hit:
                return value
        elif expected and expected["status"] == "accepted":
            invalidation = {"operation_id": binding["operation_id"], "stage": name,
                "round_no": self.round_no, "at": utc_now(),
                "reason": "force" if force else "changed_stage_binding",
                "accepted_pointer": expected}
            invalidated = {**expected, "status": "invalidated",
                           "invalidation_ref": self.artifacts.put_json(invalidation)}
            with self.store.transaction() as conn:
                self.store.put("validation_invalidation", key + ":" + uuid.uuid4().hex,
                               invalidation, conn=conn)
                self._cas_accepted(stage_key, expected, invalidated, conn)
            expected = invalidated
        last_error, last_phase = None, None
        while True:
            attempt = self._reserve(stage_key, key, binding)
            if attempt is None:
                failure = self._failure(name, cause=last_error, failure_phase=last_phase)
                raise failure from last_error
            outcome = {"status": "failed", "finished_at": None}
            terminal, phase = None, "producer"
            try:
                self.store.assert_owned()
                value = producer(attempt)
                phase = "validator"
                self.store.assert_owned()
                outcome["value_ref"], serializable = self._capture(value)
                if not serializable:
                    raise ContractError("Stage value must contain finite JSON values")
                frozen_value = self.artifacts.read_json(outcome["value_ref"])
                frozen_inputs = self.artifacts.read_json(binding["inputs_ref"])
                checked = self._validate(validator, frozen_value, frozen_inputs, outcome)
                self.store.assert_owned()
                outcome["status"] = checked["status"]
                outcome["required_actions"] = checked.get("required_actions", [])
            except LeaseLost:
                raise
            except Exception as exc:
                outcome["failure_phase"], outcome["error"] = phase, _error_info(exc)
                outcome["exception_ref"] = self._exception(exc)
                if isinstance(exc, StageValidationFailure):
                    terminal = exc
                    outcome["terminal_failure_ref"] = self.artifacts.put_json(exc.as_dict())
                else:
                    last_error, last_phase = exc, phase
                if getattr(exc, "required_actions", None):
                    outcome["required_actions"] = exc.required_actions
            outcome["finished_at"] = utc_now()
            with self.store.transaction() as conn:
                self.store.put("validation_attempt_end", f"{key}:a{attempt.number}", outcome, conn=conn)
                if outcome["status"] in ("passed", "partial"):
                    pointer = {"status": "accepted", "operation_id": attempt.operation_id,
                        "stage": name, "round_no": self.round_no, "attempt": attempt.number,
                        "inputs_hash": binding["inputs_hash"],
                        "code_identity_hash": binding["code_identity_hash"],
                        "validator": binding["validator"], "value_ref": outcome["value_ref"],
                        "validation_ref": outcome["validation_ref"], "binding_key": key,
                        "validation_status": outcome["status"],
                        "accepted_at": utc_now()}
                    self._cas_accepted(stage_key, expected, pointer, conn)
            if terminal is not None:
                raise terminal
            if outcome["status"] in ("passed", "partial"):
                return self.artifacts.read_json(outcome["value_ref"])

    def evidence(self, name, *, round_no=None):
        stage_key = self._stage_key(name)
        selected_round = self.round_no if round_no is None else round_no
        key = f"{stage_key}:r{selected_round}"
        budget = self.store.get("validation_budget", key) or {"used": 0}
        attempts = [{"start": self.store.get("validation_attempt_start", f"{key}:a{number}"),
                     "end": self.store.get("validation_attempt_end", f"{key}:a{number}")}
                    for number in range(1, budget["used"] + 1)]
        operation_id = self.store.require_context().operation_id
        return {"binding": self.store.get("validation_binding", key),
                "attempts": attempts, "accepted": self.store.get("validation_accepted", stage_key),
                "cache_reviews": [value for _, value in self.store.scan("validation_cache_review")
                                  if value["operation_id"] == operation_id
                                  and value["stage"] == name and value["round_no"] == selected_round]}

    def manifest(self):
        operation_id = self.store.assert_owned().operation_id
        bindings = [value for _, value in self.store.scan("validation_binding")
                    if value["operation_id"] == operation_id]
        return {"operation_id": operation_id, "round_no": self.round_no,
                "code_identity": self.code_identity,
                "stages": {value["stage"]: self.evidence(value["stage"])
                           for value in bindings if value["round_no"] == self.round_no},
                "rounds": {str(round_no): {
                    value["stage"]: self.evidence(value["stage"], round_no=round_no)
                    for value in bindings if value["round_no"] == round_no}
                    for round_no in sorted({value["round_no"] for value in bindings})},
                "cache_reviews": [value for _, value in self.store.scan("validation_cache_review")
                                  if value["operation_id"] == operation_id]}
