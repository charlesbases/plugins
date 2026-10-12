"""真实CLI重试、冻结身份及真实提交后中断恢复。"""
import copy
import json
from unittest.mock import patch

from investment.contracts import ContractError, Schemas, unknown
from investment.runtime import runs
from investment.runtime.controller import Controller
from investment.runtime import controller as runtime_controller
from investment.references import Resolver
from tests import RealFlowCase, harness


class RuntimeTests(RealFlowCase):
    def test_three_failed_attempts(self):
        self.first()
        before = self.ledger()
        buy = harness.make_trade("buy", "10", "20"); buy["confirmed_shares"] = harness.k(10)
        record = self.execute([harness.mutate(buy)], "invalid-type", False)
        self.assertEqual(record["result"]["attempt_count"], 3)
        self.assertFalse(record["result"]["next_chain_allowed"])
        self.assertEqual(self.ledger(), before)
        for path in record["folder"].glob("attempt-*/failure.json"):
            self.assertEqual(json.loads(path.read_bytes())["rule_id"], "J02")
            self.assertFalse((path.parent / "candidate.json").exists())
        folder, incoming = harness.build_input(self.root, "test-account", [])
        incoming["input_refs"] = [r for r in incoming["input_refs"] if r["role"] != "transaction_facts_ref"]
        (folder / "submitted-input.json").write_bytes(harness.encode(incoming))
        result = harness.invoke(self.root, folder, incoming)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["attempt_count"], 3)
        for path in folder.glob("attempt-*/failure.json"):
            self.assertEqual(json.loads(path.read_bytes())["rule_id"], "I01")
        wrong_time = harness.make_trade("buy", "10", "20")
        wrong_time["trade_at"] = harness.raw("2000-01-01T00:00:00Z")
        record = self.execute([harness.mutate(wrong_time)], "outside-opening-coverage", False)
        for path in record["folder"].glob("attempt-*/failure.json"):
            failure = json.loads(path.read_bytes())
            self.assertEqual(failure["rule_id"], "A08")
            self.assertEqual(failure["object_ref"]["value"]["schema_id"], "investment.source_record")
            self.assertEqual(failure["json_pointer"]["value"], "/changes/rows/0/trade/trade_at")
        self.assertEqual(self.ledger(), before)

    def test_committed_before_manifest(self):
        self.first()
        buy = harness.make_trade("buy", "1000", "2000")
        folder, incoming = harness.build_input(self.root, "test-account", [harness.mutate(buy)])
        controller = Controller(self.root, True)
        with patch.object(Controller, "_complete", side_effect=OSError("simulated_after_real_commit")):
            with self.assertRaises(OSError):
                controller.run(folder / "submitted-input.json", incoming["run_id"], incoming["invocation_id"])
        committed = self.ledger()
        self.assertFalse(committed["meta"]["last_commit"]["finalized"])
        result = harness.invoke(self.root, folder, incoming)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.ledger()
        self.assertEqual(after["trades"], committed["trades"])
        self.assertEqual(after["meta"]["generation"], committed["meta"]["generation"])
        self.assertTrue(after["meta"]["last_commit"]["finalized"])
        sealed_path = folder / "attempt-01/sealed.json"
        sealed_bytes = sealed_path.read_bytes()
        replay = harness.invoke(self.root, folder, incoming)
        self.assertEqual(json.loads(result.stdout), json.loads(replay.stdout))
        self.assertEqual(sealed_path.read_bytes(), sealed_bytes)
        _, manifest = self.manifest()
        inv = next(v for v in manifest["invocations"] if v["invocation_id"] == incoming["invocation_id"])
        self.assertEqual(len(inv["attempts"]), 1)

    def test_terminal_invocation_and_readme(self):
        record = self.first()
        path, manifest = self.manifest(); inv = manifest["invocations"][0]
        incoming = copy.deepcopy(record["input"]); incoming["decision_at"] = "2099-01-01T00:00:00Z"
        (record["folder"] / "submitted-input.json").write_bytes(harness.encode(incoming))
        rejected = harness.invoke(self.root, record["folder"], incoming)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(json.loads(rejected.stdout)["rule_id"], "T03")
        (record["folder"] / "submitted-input.json").write_bytes(harness.encode(record["input"]))
        actual = {p.relative_to(path.parent).as_posix() for p in record["folder"].rglob("*.json")}
        listed = {f["relative_path"] for f in inv["files"] + [f for a in inv["attempts"] for f in a["files"]]}
        self.assertEqual(actual, listed)
        self.assertIn(inv["invocation_id"], (path.parent / "README.md").read_text(encoding="utf-8"))
        for field, value in (("processing_state", "pending"), ("finished_at", unknown()), ("next_chain_allowed", False)):
            invalid = copy.deepcopy(manifest); invalid["invocations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                runs.save_manifest(self.root, invalid, Schemas())
        source_path = record["folder"] / "source-record.json"
        original = source_path.read_bytes(); source_path.write_bytes(original + b" ")
        with self.assertRaises(ContractError):
            runs.collect_files(self.root, path.parent.name, inv)
        source_path.write_bytes(original)

    def test_rejected_output_returns_to_processor(self):
        self.first()
        before = self.ledger()
        buy = harness.make_trade("buy", "500", "1000")
        folder, incoming = harness.build_input(self.root, "test-account", [harness.mutate(buy)])
        real_process = runtime_controller.run_process
        attempts, input_bytes = [], (folder / "submitted-input.json").read_bytes()

        def wrong_candidate(*args):
            process, reference, failure = real_process(*args)
            self.assertIsNone(failure)
            directory = args[-1]
            attempts.append(args[4])
            self.assertEqual((folder / "input.json").read_bytes(), input_bytes)
            # 正常买入输入经过真实流程；仅在测试边界注入错误输出以检验拒绝和回退。
            state = json.loads((directory / "state.json").read_bytes())
            candidate = json.loads((directory / "candidate.json").read_bytes())
            positions = state["business_objects"]["positions"]
            positions["rows"][0]["confirmed_shares"] = harness.k("501")
            context = state["context"]
            position_ref = context["positions_ref"]
            position_ref.update(harness.ref(positions, "investment.positions", position_ref["uri"]))
            candidate["payload_ref"] = harness.ref(context, "investment.transaction_context", candidate["payload_ref"]["uri"])
            candidate["datasets"] = [context[name + "_ref"] for name in ("trades", "positions", "pending_transactions")]
            for name, value in (("positions", positions), ("context", context), ("state", state), ("candidate", candidate)):
                (directory / (name + ".json")).write_bytes(harness.encode(value))
            return process, harness.ref(candidate, "investment.candidate_output", reference["uri"]), None

        with patch.object(runtime_controller, "run_process", wrong_candidate):
            code, result = Controller(self.root, True).run(folder / "submitted-input.json", incoming["run_id"], incoming["invocation_id"])
        self.assertEqual(code, 1)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertFalse(result["next_chain_allowed"])
        self.assertEqual(self.ledger(), before)
        resolver = Resolver(self.root, Schemas(), fixture_mode=True)
        for reference in result["failure_refs"]:
            failure = resolver.resolve(reference, "investment.failure")
            self.assertEqual((failure["phase"], failure["rule_id"]), ("output_check", "A10"))
        for directory in folder.glob("attempt-*"):
            self.assertFalse((directory / "input-check.json").exists())
            self.assertFalse((directory / "sealed.json").exists())
            self.assertTrue((directory / "receipt.json").exists())
        accepted = self.execute([harness.mutate(buy)], "correct-process-output")
        self.assertEqual(accepted["state"]["business_objects"]["positions"]["rows"][0]["confirmed_shares"], harness.k("500"))
        self.assertEqual(len(self.ledger()["trades"]["rows"]), 1)
