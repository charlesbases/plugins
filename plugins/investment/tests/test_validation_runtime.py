"""Retry outcomes, persistent budgets, and evidence publication regressions."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
from contracts import ConflictError, ContractError
from state_store import LeaseLost, Store
from validation_runtime import StageRuntime, StageValidationFailure, validate_result


def passed(value, inputs):
    if value["number"] != inputs["expected"]:
        raise ContractError("Number differs from independent expectation")
    return {"status": "passed", "scope": "independent_test_contract",
            "checks": {"expected_number": True}}


class ValidationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root, "validation")
        self.store.begin("original-operation", {"request": "frozen"})

    def tearDown(self):
        self.tmp.cleanup()

    def runtime(self, *, store=None, round_no=1):
        return StageRuntime(store or self.store, code_identity={"revision": "fixed"},
            validator_contracts={name: {"name": "test." + name, "version": "1"}
                                 for name in ("calculation", "downstream")}, round_no=round_no)

    def test_two_failed_values_then_third_producer_is_accepted(self):
        calls = []
        def producer(attempt):
            calls.append(attempt)
            return {"number": attempt.number, "status": "passed"}
        def validator(value, inputs):
            return {"status": "passed" if value["number"] == 3 else "failed",
                    "scope": "third_value_required", "checks": {"number": value["number"] == 3}}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            result = runtime.run("calculation", {"expected": 3}, producer, validator)
            self.assertEqual(result["number"], 3)
            self.assertEqual([attempt.number for attempt in calls], [1, 2, 3])
            evidence = runtime.evidence("calculation")
            self.assertEqual([attempt["end"]["status"] for attempt in evidence["attempts"]],
                             ["failed", "failed", "passed"])
            self.assertEqual(runtime.artifacts.read_json(evidence["accepted"]["value_ref"])["number"], 3)
            self.assertEqual(runtime.artifacts.read_json(evidence["attempts"][0]["end"]["validation_ref"])["status"], "failed")

    def test_exhausted_budget_survives_new_store_and_blocks_dependency(self):
        calls, outer_calls, leaves = [], [], []
        def producer(attempt):
            calls.append(attempt.number)
            return {"number": 0, "status": "passed"}
        validator = lambda value, inputs: {"status": "failed", "scope": "reject",
                                           "checks": {"observed": value["number"]}}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            def outer(attempt):
                outer_calls.append(attempt.number)
                try:
                    return runtime.run("calculation", {"expected": 1}, producer, validator)
                except StageValidationFailure as exc:
                    leaves.append(exc)
                    raise
            with self.assertRaises(StageValidationFailure) as first:
                runtime.run("downstream", {}, outer, validator)
            self.assertIs(first.exception, leaves[0])
            self.assertEqual(len(first.exception.attempts), 3)
            self.assertEqual(outer_calls, [1])
            outer_attempts = runtime.evidence("downstream")["attempts"]
            self.assertEqual(len(outer_attempts), 1)
            self.assertEqual(runtime.artifacts.read_json(outer_attempts[0]["end"]["terminal_failure_ref"])["stage"], "calculation")
            self.assertIsNone(runtime.accepted("calculation"))
        restored = Store(self.root, "validation")
        with restored.lease("original-operation"):
            runtime = self.runtime(store=restored)
            with self.assertRaises(StageValidationFailure) as resumed:
                runtime.run("calculation", {"expected": 1}, producer, validator)
            self.assertIsNone(resumed.exception.cause)
            self.assertEqual(resumed.exception.failure_phase, "validator")
            self.assertEqual(resumed.exception.cause_info["type"], "ContractError")
            with self.assertRaises(StageValidationFailure) as resumed_outer:
                runtime.run("downstream", {}, lambda attempt: outer_calls.append(attempt.number), validator)
            self.assertEqual(resumed_outer.exception.stage, "calculation")
            self.assertIsNone(resumed_outer.exception.cause)
            self.assertEqual(outer_calls, [1])
            downstream = []
            with self.assertRaises(StageValidationFailure):
                runtime.run("downstream", {}, lambda attempt: downstream.append(attempt),
                            validator, dependencies=("calculation",))
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(downstream, [])
            self.assertIsNone(runtime.accepted("calculation"))

    def test_cached_value_is_revalidated_and_invalid_cache_reproduced(self):
        producers, validations = [], []
        reject_cached = False
        terminal_leaf = None
        def producer(attempt):
            producers.append(attempt.number)
            return {"number": attempt.number}
        def validator(value, inputs):
            if terminal_leaf is not None:
                raise terminal_leaf
            validations.append(value["number"])
            return {"status": "failed" if reject_cached and value["number"] == 1 else "passed",
                    "scope": "fresh_validation", "checks": {"actual_number": value["number"]}}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            self.assertEqual(runtime.run("calculation", {}, producer, validator)["number"], 1)
            self.assertEqual(runtime.run("calculation", {}, producer, validator)["number"], 1)
            reject_cached = True
            self.assertEqual(runtime.run("calculation", {}, producer, validator)["number"], 2)
            self.assertEqual(producers, [1, 2])
            self.assertEqual(validations, [1, 1, 1, 2])
            reviews = runtime.manifest()["cache_reviews"]
            self.assertEqual(sorted(review["status"] for review in reviews), ["failed", "passed"])
            def failed_leaf(attempt):
                raise ContractError("terminal leaf")
            with self.assertRaises(StageValidationFailure) as leaf:
                runtime.run("downstream", {}, failed_leaf, validator)
            terminal_leaf = leaf.exception
            with self.assertRaises(StageValidationFailure) as cached_terminal:
                runtime.run("calculation", {}, producer, validator)
            self.assertIs(cached_terminal.exception, terminal_leaf)
            self.assertEqual(producers, [1, 2])
            self.assertIsNone(runtime.accepted("calculation"))
            with self.assertRaises(StageValidationFailure) as restored_terminal:
                runtime.run("calculation", {}, producer, validator)
            self.assertEqual(restored_terminal.exception.stage, "downstream")
            self.assertIsNone(restored_terminal.exception.cause)
            self.assertEqual(producers, [1, 2])

    def test_force_recomputes_and_does_not_reset_same_round_budget(self):
        calls = []
        def producer(attempt):
            calls.append(attempt.number)
            return {"number": attempt.number}
        validator = lambda value, inputs: {"status": "passed", "scope": "observed",
                                           "checks": {"number": value["number"]}}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            runtime.run("calculation", {}, producer, validator)
            runtime.run("calculation", {}, producer, validator, force=True)
            runtime.run("calculation", {}, producer, validator, force=True)
            with self.assertRaises(StageValidationFailure):
                runtime.run("calculation", {}, producer, validator, force=True)
            self.assertEqual(calls, [1, 2, 3])
            self.assertIsNone(runtime.accepted("calculation"))

    def test_exhausted_cache_failure_preserves_its_specific_actions(self):
        stale = False
        action = {"action": "refresh_current_source"}
        calls = []
        def producer(attempt):
            calls.append(attempt.number)
            return {"number": attempt.number}
        def validator(value, inputs):
            return {"status": "failed" if stale or value["number"] < 3 else "passed",
                    "scope": "current_source", "checks": {"stale": stale},
                    "required_actions": [action] if stale else []}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            runtime.run("calculation", {}, producer, validator)
            stale = True
            with self.assertRaises(StageValidationFailure) as caught:
                runtime.run("calculation", {}, producer, validator)
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(caught.exception.required_actions, [action])
            review = caught.exception.evidence["cache_reviews"][0]
            self.assertEqual(runtime.artifacts.read_json(review["validation_ref"])["required_actions"], [action])
            self.assertIsNone(runtime.accepted("calculation"))

    def test_resume_retains_frozen_input_reference_across_artifact_locations(self):
        inputs = {"expected": 1}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            runtime.run("calculation", inputs, lambda attempt: {"number": 1}, passed)
            original = runtime.evidence("calculation")["binding"]["inputs_ref"]
            alternate = {**original, "path": "objects/later-month/" + original["sha256"]}
            alternate_path = runtime.artifacts.base / alternate["path"]
            alternate_path.parent.mkdir(parents=True)
            alternate_path.write_bytes(runtime.artifacts.read(original))
            self.assertEqual(runtime.artifacts.read_json(alternate), inputs)
            put_json = runtime.artifacts.put_json
            def changed_location(value):
                return alternate if value == inputs else put_json(value)
            with patch.object(runtime.artifacts, "put_json", side_effect=changed_location):
                result = runtime.run("calculation", inputs,
                                     lambda attempt: self.fail("Cache was unexpectedly reproduced"), passed)
            self.assertEqual(result, {"number": 1})
            self.assertEqual(runtime.evidence("calculation")["binding"]["inputs_ref"], original)

    def test_failure_retains_value_validator_exception_and_required_actions(self):
        action = {"action": "obtain_missing_source", "source": "issuer"}
        def producer(attempt):
            if attempt.number == 1:
                error = ValueError("raw producer failure")
                error.required_actions = [action]
                raise error
            return {"number": attempt.number, "raw": "rejected producer output"}
        def validator(value, inputs):
            return {"status": "failed", "scope": "missing_source", "checks": {"verified": False},
                    "required_actions": [action], "raw": "rejected validation output"}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            with self.assertRaises(StageValidationFailure) as caught:
                runtime.run("calculation", {}, producer, validator)
            failure = caught.exception
            self.assertEqual(failure.failure_phase, "validator")
            self.assertIsInstance(failure.cause, ContractError)
            self.assertEqual(failure.required_actions, [action])
            ends = [attempt["end"] for attempt in failure.attempts]
            self.assertEqual(runtime.artifacts.read_json(ends[0]["exception_ref"])["message"],
                             "raw producer failure")
            self.assertEqual(runtime.artifacts.read_json(ends[1]["value_ref"])["raw"],
                             "rejected producer output")
            self.assertEqual(runtime.artifacts.read_json(ends[1]["validation_ref"])["raw"],
                             "rejected validation output")
            self.assertEqual(failure.as_dict()["code"], "stage_validation_failed")
            self.assertIsNone(runtime.accepted("calculation"))
            business_error = ConflictError("original business conflict")
            business_calls = []
            def producer_conflict(attempt):
                business_calls.append(attempt.number)
                raise business_error
            with self.assertRaises(StageValidationFailure) as business:
                runtime.run("downstream", {}, producer_conflict, passed)
            self.assertEqual(business_calls, [1, 2, 3])
            self.assertIs(business.exception.cause, business_error)
            self.assertIs(business.exception.__cause__, business_error)
            self.assertEqual(business.exception.failure_phase, "producer")
            self.assertEqual(business.exception.cause_info["code"], "input_conflict")
            second_round = self.runtime(round_no=2)
            validator_error = ConflictError("validator rejects business output")
            def validator_conflict(value, inputs):
                raise validator_error
            with self.assertRaises(StageValidationFailure) as checked:
                second_round.run("calculation", {}, lambda attempt: {"number": 1}, validator_conflict)
            self.assertIs(checked.exception.cause, validator_error)
            self.assertEqual(checked.exception.failure_phase, "validator")
        restored = Store(self.root, "validation")
        with restored.lease("original-operation"):
            runtime = self.runtime(store=restored)
            with self.assertRaises(StageValidationFailure) as resumed:
                runtime.run("downstream", {}, lambda attempt: self.fail("Exhausted producer resumed"), passed)
            self.assertIsNone(resumed.exception.cause)
            self.assertEqual(resumed.exception.failure_phase, "producer")
            self.assertEqual(resumed.exception.cause_info,
                             {"type": "ConflictError", "code": "input_conflict", "message": "original business conflict"})

    def test_lease_lost_exits_immediately_and_interrupted_attempt_consumes_budget(self):
        calls = []
        def lost(attempt):
            calls.append(attempt.number)
            raise LeaseLost("injected lease loss")
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            with self.assertRaises(LeaseLost):
                runtime.run("calculation", {"expected": 2}, lost, passed)
            self.assertEqual(calls, [1])
            self.assertIsNone(runtime.evidence("calculation")["attempts"][0]["end"])
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            value = runtime.run("calculation", {"expected": 2},
                                lambda attempt: {"number": attempt.number}, passed)
            self.assertEqual(value["number"], 2)
            self.assertEqual(runtime.evidence("calculation")["attempts"][0]["end"]["status"], "interrupted")

    def test_original_identity_and_retry_round_namespaces_remain_distinct(self):
        attempts = []
        def producer(attempt):
            attempts.append(attempt)
            return {"number": attempt.number}
        def third(value, inputs):
            return {"status": "passed" if value["number"] == 3 else "failed",
                    "scope": "third", "checks": {"number": value["number"]}}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            runtime.run("calculation", {}, producer, third)
            next_round = self.runtime(round_no=2)
            next_round.run("calculation", {"expected": 1}, producer, passed, force=True)
            self.assertEqual([attempt.operation_id for attempt in attempts], ["original-operation"] * 4)
            self.assertEqual(attempts[0].namespace, "original-operation")
            self.assertEqual(len({attempt.namespace for attempt in attempts}), 4)
            self.assertEqual([attempt.round_no for attempt in attempts], [1, 1, 1, 2])
            self.assertEqual(set(next_round.manifest()["rounds"]), {"1", "2"})

    def test_unknown_stage_and_changed_frozen_inputs_do_not_run_producer(self):
        calls = []
        def producer(attempt):
            calls.append(attempt.number)
            return {"number": 1}
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            with self.assertRaises(ContractError):
                runtime.run("unregistered", {}, producer, passed)
            runtime.run("calculation", {"expected": 1}, producer, passed)
            with self.assertRaises(ConflictError):
                runtime.run("calculation", {"expected": 2}, producer, passed)
            self.assertEqual(calls, [1])

    def test_partial_validation_requires_explicit_non_trade_readiness(self):
        valid = {"status": "partial", "scope": "research_only", "checks": {"sources": "reviewed"},
                 "readiness": {"trade_ready": False},
                 "required_actions": [{"action": "collect_mature_labels"}]}
        self.assertEqual(validate_result(valid), valid)
        for invalid in ({"status": "passed"}, {**valid, "readiness": {"trade_ready": True}},
                        {**valid, "required_actions": []}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ContractError):
                    validate_result(invalid)
        with self.store.lease("original-operation"):
            runtime = self.runtime()
            runtime.run("calculation", {}, lambda attempt: {"number": 1},
                        lambda value, inputs: valid)
            self.assertEqual(runtime.accepted("calculation")["validation_status"], "partial")


if __name__ == "__main__":
    unittest.main()
