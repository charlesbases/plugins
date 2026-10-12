"""实际SQLite事务、CAS、恢复及清运行产物后的事实保留。"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from investment.contracts import ContractError, Schemas
from investment.references import Resolver
from investment.runtime import runs
from investment.runtime.controller import Controller
from investment.storage import Store
from tests import RealFlowCase, harness


class StorageTests(RealFlowCase):
    def test_commit_guards_and_rollback(self):
        self.first()
        database = self.root / "data/investment.sqlite"
        before = self.ledger()
        with sqlite3.connect(database) as con:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("INSERT INTO trades VALUES(?,?,?)", ("test-account", "bad", "{}"))
            con.create_function("investment_trade_v1", 3, lambda *args: None)
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("INSERT INTO trades VALUES(?,?,?)", ("test-account", "bad", "{}"))
        store = Store(self.root, Schemas())
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store.con.execute("UPDATE ledger_meta SET meta_json=meta_json")
            self.assertEqual(store.last_error.rule, "D02")
            with self.assertRaises(sqlite3.DatabaseError):
                store.con.execute("DELETE FROM ledger_meta")
        finally:
            store.close()
        self.assertEqual(self.ledger(), before)

    def test_compare_and_swap(self):
        self.first()
        buy_a, buy_b = harness.make_trade("buy", "50", "100"), harness.make_trade("buy", "1000", "2000")
        folder_b, incoming_b = harness.build_input(self.root, "test-account", [harness.mutate(buy_b)])
        folder_a, incoming_a = harness.build_input(self.root, "test-account", [harness.mutate(buy_a)])
        original = Store.commit
        interleaved = []
        def interleave(store, state, candidate, receipt, stamp, baseline):
            if stamp["invocation_id"] == incoming_a["invocation_id"] and not interleaved:
                interleaved.append(True)
                # 在A已取得真实验收、尚未BEGIN之间，另一写连接完成真实B事务。
                other = Store(self.root, Schemas())
                try:
                    code, _ = Controller(self.root, True)._run_locked(other, (folder_b / "submitted-input.json").read_bytes(), incoming_b["run_id"], incoming_b["invocation_id"])
                    self.assertEqual(code, 0)
                finally:
                    other.close()
            return original(store, state, candidate, receipt, stamp, baseline)
        with patch.object(Store, "commit", interleave):
            code, result = Controller(self.root, True).run(folder_a / "submitted-input.json", incoming_a["run_id"], incoming_a["invocation_id"])
        self.assertEqual(code, 1)
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual([t["trade_id"] for t in self.ledger()["trades"]["rows"]], [buy_b["trade_id"]])
        for path in folder_a.glob("attempt-*/failure.json"):
            self.assertEqual(json.loads(path.read_bytes())["rule_id"], "D03")

    def test_reopen_and_recover(self):
        self.first()
        self.execute([harness.mutate(harness.make_trade("buy", "1000", "2000"))], "holding")
        before = self.ledger()
        store = Store(self.root, Schemas()); store.close()
        self.assertEqual(Resolver(self.root, Schemas()).current("test-account"), before)
        # 不支持的实际表布局必须明确拒绝，不能以空账户继续。
        old_root = self.root / "unsupported"; (old_root / "data").mkdir(parents=True)
        with sqlite3.connect(old_root / "data/investment.sqlite") as con:
            con.execute("CREATE TABLE accepted_state(value TEXT)"); con.execute("INSERT INTO accepted_state VALUES('preserve')")
        with self.assertRaises(ContractError):
            Store(old_root, Schemas())
        with sqlite3.connect(old_root / "data/investment.sqlite") as con:
            self.assertEqual(con.execute("SELECT value FROM accepted_state").fetchone()[0], "preserve")

    def test_atomic_ledger_commit(self):
        self.first(); before = self.ledger()
        buys = sorted([harness.make_trade("buy", "100", "200"), harness.make_trade("buy", "50", "100")], key=lambda t:t["trade_id"])
        folder, incoming = harness.build_input(self.root, "test-account", [harness.mutate(t) for t in buys])
        original = Store._trade_guard
        def fail_second(store, account, tid, text):
            original(store, account, tid, text)
            if tid == buys[1]["trade_id"]:
                raise ContractError("D01", "/trade_json", "simulated_second_row_write_failure")
        with patch.object(Store, "_trade_guard", fail_second):
            code, _ = Controller(self.root, True).run(folder / "submitted-input.json", incoming["run_id"], incoming["invocation_id"])
        self.assertEqual(code, 1)
        self.assertEqual(self.ledger(), before)
        with sqlite3.connect(self.root / "data/investment.sqlite") as con:
            self.assertEqual(con.execute("SELECT count(*) FROM trades").fetchone()[0], 0)

    def test_finalize_before_next_commit(self):
        self.first()
        folder, incoming = harness.build_input(self.root, "test-account", [harness.mutate(harness.make_trade("buy", "100", "200"))])
        with patch.object(Controller, "_complete", side_effect=OSError("simulated_after_commit")):
            with self.assertRaises(OSError):
                Controller(self.root, True).run(folder / "submitted-input.json", incoming["run_id"], incoming["invocation_id"])
        original_marker = self.ledger()["meta"]["last_commit"]
        self.assertFalse(original_marker["finalized"])
        # 恢复材料缺失时不得用新一次执行覆盖当前未收尾标记。
        receipt_path = folder / "attempt-01/receipt.json"
        receipt = receipt_path.read_bytes(); receipt_path.unlink()
        next_folder, next_input = harness.build_input(self.root, "test-account", [])
        blocked = harness.invoke(self.root, next_folder, next_input)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("Already committed", blocked.stderr)
        self.assertEqual(self.ledger()["meta"]["last_commit"], original_marker)
        receipt_path.write_bytes(receipt)
        completed = harness.invoke(self.root, next_folder, next_input)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.ledger()["meta"]["last_commit"]["invocation_id"], next_input["invocation_id"])
        _, manifest = self.manifest()
        prior = next(v for v in manifest["invocations"] if v["invocation_id"] == incoming["invocation_id"])
        self.assertEqual(prior["processing_state"], "completed")

    def test_ledger_survives_run_cleanup(self):
        self.first()
        buy = harness.make_trade("buy", "1000", "2000")
        record = self.execute([harness.mutate(buy)], "holding")
        before = self.ledger()
        path, manifest = self.manifest()
        inv = next(v for v in manifest["invocations"] if v["invocation_id"] == record["input"]["invocation_id"])
        store = Store(self.root, Schemas())
        unlink, removed = Path.unlink, []
        def interrupted(target, *args, **kwargs):
            if target.is_relative_to(record["folder"]):
                if removed:
                    raise OSError("simulated_cleanup_interruption")
                removed.append(target)
            return unlink(target, *args, **kwargs)
        try:
            with runs.root_lock(self.root), patch.object(Path, "unlink", interrupted):
                with self.assertRaises(OSError):
                    runs.cleanup(self.root, manifest, inv, Schemas(), store)
        finally:
            store.close()
        self.assertTrue(removed)
        refreshed = self.execute([], "from-database-after-cleanup")
        self.assertEqual(self.ledger()["trades"], before["trades"])
        self.assertEqual(refreshed["state"]["business_objects"]["positions"]["rows"][0]["confirmed_shares"], harness.k("1000"))
        self.assertEqual(self.ledger()["meta"]["generation"], before["meta"]["generation"])
        self.assertFalse(list(record["folder"].rglob("*.json")))
        self.assertIn("已清理", (path.parent / "README.md").read_text(encoding="utf-8"))
        old = self.ledger()["trades"]["rows"][0]
        self.execute([harness.mutate(old, old, "update")], "stable-id-dedup")
        self.assertEqual(len(self.ledger()["trades"]["rows"]), 1)
