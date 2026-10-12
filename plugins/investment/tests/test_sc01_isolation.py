"""测试路径、来源和业务输出边界；拒绝用例不得接触正式运行数据。"""

import copy
import json
import os
import subprocess
import sys

from investment.contracts import ContractError, Schemas
from investment.runtime.controller import Controller
from investment.runtime.paths import production_root, validate_input_path, validate_root, validate_source_root
from investment.storage import Store
from tests import PLUGIN, RealFlowCase


class IsolationTests(RealFlowCase):
    def test_mode_root_guard(self):
        production = production_root()
        self.assertEqual(validate_root(production, False), production)
        self.assertEqual(validate_root(self.root, True), self.root)
        for root, mode in ((production, True), (self.root, False), (production / "test", True), (production / "data", False), (production.parent / "other-root", True)):
            with self.subTest(root=root, mode=mode), self.assertRaises(ContractError) as error:
                validate_root(root, mode)
            self.assertEqual(error.exception.rule, "T04")
        for root, authority in ((production, "synthetic_fixture"), (self.root, "user_confirmation")):
            with self.subTest(root=root, authority=authority), self.assertRaises(ContractError):
                validate_source_root(root, authority)

    def test_reject_before_production_write(self):
        production = production_root()
        targets = [production / "data", production / "runs", production / "runs/sc01/.controller.lock"]
        if (production / "data").exists():
            targets.extend((production / "data").glob("investment.sqlite*"))
        def footprint():
            return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in targets}
        before = footprint()
        with self.assertRaises(ContractError):
            Controller(production, True)
        with self.assertRaises(ContractError):
            Store(production, Schemas(), fixture_mode=True)
        # 无效根必须先被拒绝；不创建假冻结输入或向正式目录投放测试文件。
        missing = self.root / "not-created.json"
        with self.assertRaises(ContractError):
            Controller(production).run(missing, "run-root-guard", "inv-root-guard")
        for script in ("run_sc01.py", "process_sc01.py", "validate_sc01.py"):
            command = [sys.executable, "-B", str(PLUGIN / "scripts" / script), "--root", str(production), "--fixture-mode", "--input", str(missing), "--run-id", "run-root-guard", "--invocation-id", "inv-root-guard"]
            if script != "run_sc01.py":
                command += ["--attempt-no", "1"]
            if script == "validate_sc01.py":
                command += ["--phase", "output", "--candidate", str(missing)]
            with self.subTest(script=script):
                process = subprocess.run(command, cwd=PLUGIN, env=dict(os.environ, PYTHONPATH=str(PLUGIN / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(process.returncode, 2, process.stderr)
                self.assertEqual(process.stdout, "")
                self.assertIn("Runtime mode does not match", process.stderr)
        self.assertEqual(footprint(), before)

    def test_input_namespace_guard(self):
        production = production_root()
        inside = self.root / "submitted-input.json"
        self.assertEqual(validate_input_path(self.root, inside), inside)
        for root, incoming in ((production, inside), (self.root, production / "runs/input.json"), (self.root, self.root.parent / "another-batch/input.json")):
            with self.subTest(root=root, incoming=incoming), self.assertRaises(ContractError):
                validate_input_path(root, incoming)
        # 遗漏测试参数不能把测试入口交给正式实例；错误在文件读取前返回。
        command = [sys.executable, "-B", str(PLUGIN / "scripts/run_sc01.py"), "--input", str(inside), "--run-id", "run-input-guard", "--invocation-id", "inv-input-guard"]
        process = subprocess.run(command, cwd=PLUGIN, env=dict(os.environ, PYTHONPATH=str(PLUGIN / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8"), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(process.stdout, "")
        self.assertIn("Input path crosses", process.stderr)

    def test_business_output_and_report_are_separate(self):
        record = self.first()
        base = record["folder"] / "attempt-01"
        self.assertTrue(self.root.is_relative_to(production_root() / "test"))
        self.assertTrue((self.root / "data/investment.sqlite").is_file())
        for name in ("candidate", "context", "trades", "positions", "pending_transactions"):
            value = json.loads((base / (name + ".json")).read_bytes())
            self.assertNotIn("invocations", value)
            self.assertNotIn("checks", value)
        self.assertEqual(record["state"]["context"]["source_authority"], "synthetic_fixture")
        receipt = json.loads((base / "receipt.json").read_bytes())
        self.assertEqual(receipt["business_result"], "observation_only")
        self.assertEqual(receipt["allowed_uses"], ["reporting"])
        self.assertEqual(self.invocations[0]["name"], "first")
        self.assertEqual(record["state"]["business_objects"]["trades"], {"rows": []})

    def test_source_binding_before_store_permission(self):
        record = self.first()
        before = self.ledger()
        state = copy.deepcopy(record["state"])
        state["source_record"]["source_type"] = "user_confirmation"
        store = Store(self.root, Schemas())
        try:
            with self.assertRaises(ContractError) as error:
                with store.acceptance(None, b"", state, {}, {}, "inv-source-guard", {}):
                    self.fail("Mismatched source obtained write permission")
            self.assertEqual(error.exception.rule, "T04")
            self.assertEqual(error.exception.code, "use_not_allowed")
        finally:
            store.close()
        self.assertEqual(self.ledger(), before)
