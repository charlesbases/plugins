"""Transaction and recovery regressions with independently stated outcomes."""
from pathlib import Path
from contextlib import closing
import subprocess
import sqlite3
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
from artifacts import Artifacts
from contracts import ConflictError, EvidenceError, RetryableError, StaleSnapshot, fingerprint
from state_store import Store


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root, "case", timeout=0.01)
        self.maintenance = self.store.maintenance()
        self.maintenance.__enter__()
        self.maintenance_open = True

    def tearDown(self):
        if self.maintenance_open:
            self.maintenance.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_type_sensitive_idempotence(self):
        self.store.put("event", "x", {"confirmed": True})
        with self.assertRaises(ConflictError):
            self.store.put("event", "x", {"confirmed": 1})
        self.assertEqual(self.store.get("event", "x"), {"confirmed": True})

    def test_atomic_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as conn:
                self.store.put("account", "main", {"cash": "100"}, False, conn)
                self.store.put("event", "one", {"amount": "100"}, conn=conn)
                raise RuntimeError("injected precommit failure")
        self.assertIsNone(self.store.get("account", "main"))
        self.assertIsNone(self.store.get("event", "one"))

    def test_commit_before_response_is_replayed(self):
        request = {"amount": "100"}
        self.store.begin("one", request)
        self.store.complete("one", {"result": "confirmed"}, [
            {"kind": "account", "key": "main", "value": {"cash": "100"}, "immutable": False}])
        retry = self.store.begin("one", request)
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(retry["result"], {"result": "confirmed"})
        with self.assertRaises(ConflictError):
            self.store.begin("one", {"amount": "200"})

    def test_cross_archive_identity_and_integrity(self):
        self.store.begin("last-month", {"n": 1})
        self.store.complete("last-month", {"done": True})
        self.store.put("event", "global-id", {"amount": "10"})
        archived = self.store.archive()
        self.assertEqual(self.store.get("event", "global-id"), {"amount": "10"})
        self.assertEqual(self.store.begin("last-month", {"n": 1})["status"], "completed")
        with self.assertRaises(ConflictError):
            self.store.put("event", "global-id", {"amount": "11"})
        self.assertEqual(self.store.audit()["archives"], 1)
        with (self.store.base / archived["path"]).open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(EvidenceError):
            self.store.get("event", "global-id")

    def test_stale_snapshot_does_not_publish(self):
        original = {"cash": "10"}
        self.store.put("account", "main", original, False)
        self.store.begin("review", {})
        self.store.put("account", "main", {"cash": "20"}, False)
        with self.assertRaises(StaleSnapshot):
            self.store.complete("review", {"buy": "10"}, expected=[
                {"kind": "account", "key": "main", "hash": fingerprint(original)}])
        self.assertNotEqual(self.store.operation("review")["status"], "completed")

    def test_busy_is_retryable(self):
        with self.store.transaction():
            with self.assertRaises(RetryableError):
                with self.store.transaction():
                    pass

    def test_initialization_lock_is_retryable(self):
        with closing(sqlite3.connect(self.store.path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            try:
                with self.assertRaises(RetryableError):
                    Store(self.root, "case", timeout=0.01)
            finally:
                blocker.rollback()

    def test_audit_rejects_changed_completed_result(self):
        self.store.begin("done", {"operation": "status"})
        self.store.complete("done", {"cash": "100"})
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("UPDATE operations SET result=? WHERE id=?", ('{"cash":"999"}', "done"))
            conn.commit()
        with self.assertRaisesRegex(EvidenceError, "Completed operation result changed"):
            self.store.audit()

    def test_publication_guard_failure_rolls_back(self):
        self.store.begin("expired", {})
        def expired():
            raise StaleSnapshot("Temporary risk preference expired")
        with self.assertRaises(StaleSnapshot):
            self.store.complete("expired", {"done": True}, [{"kind": "decision", "key": "x", "value": {},
                                                              "immutable": True}], before_commit=expired)
        self.assertIsNone(self.store.get("decision", "x"))
        self.assertIsNone(self.store.get("operation_result", "expired"))

    def test_process_death_rolls_back_and_releases_lease(self):
        self.maintenance.__exit__(None, None, None)
        self.maintenance_open = False
        self.store.begin("crash", {"n": 1})
        script = (
            "import os,sys; sys.path.insert(0,sys.argv[1]); from state_store import Store; "
            "s=Store(sys.argv[2],'case'); lease=s.lease('crash'); lease.__enter__(); "
            "tx=s.transaction(); c=tx.__enter__(); s.put('event','crash',{'cash':'99'},conn=c); os._exit(17)")
        proc = subprocess.run([sys.executable, "-B", "-c", script, str(SCRIPTS), str(self.root)],
                              timeout=15, capture_output=True)
        self.assertEqual(proc.returncode, 17, proc.stderr)
        self.assertIsNone(self.store.get("event", "crash"))
        with self.store.lease("crash"):
            self.store.complete("crash", {"done": True})
        self.assertEqual(self.store.audit()["status"], "passed")

    def test_artifact_chunking_and_tamper(self):
        objects = Artifacts(self.store.base)
        raw = b"a" * (16 * 1024 * 1024 + 9)
        ref = objects.put_bytes(raw)
        self.assertEqual(len(ref["chunks"]), 2)
        self.assertEqual(objects.read(ref), raw)
        (self.store.base / ref["chunks"][1]["path"]).write_bytes(b"changed")
        with self.assertRaises(EvidenceError):
            objects.read(ref)


if __name__ == "__main__":
    unittest.main()
