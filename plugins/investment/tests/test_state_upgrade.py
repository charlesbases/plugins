"""Synthetic offline schema fixtures; no invented trade or historical profit."""
import contextlib
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[1] / "skills/investment"
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(SKILL / "scripts"))
import state_upgrade
import ledger
from artifacts import Artifacts
from contracts import canonical_bytes, fingerprint
from state_store import Store

AT = "2025-01-01T00:00:00Z"


class OfflineUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "plans/case"
        self.base.mkdir(parents=True)
        self.path = self.base / "active.sqlite"
        self.event = {"id": "original-user-unknown", "type": "unknown", "sequence": 1,
            "effective_at": AT, "known_at": AT, "recorded_at": AT,
            "data": {"reason": "cash and pending orders unconfirmed"}}
        self.account = ledger.apply_event(ledger.initial_state("CNY"), self.event)
        old = {key: value for key, value in self.account.items() if key not in
               {"mark_dates", "transfers", "performance_pending", "reconciliation"}}
        old["schema_version"] = 2
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.executescript('''CREATE TABLE records(kind TEXT,key TEXT,value TEXT,hash TEXT,immutable INTEGER,created_at TEXT,PRIMARY KEY(kind,key));
                CREATE TABLE operations(id TEXT PRIMARY KEY,request_hash TEXT,request TEXT,status TEXT,result TEXT,created_at TEXT,updated_at TEXT,owner TEXT,pid INTEGER,lease_until REAL);
                CREATE TABLE archives(path TEXT PRIMARY KEY,hash TEXT,records_count INTEGER,operations_count INTEGER,created_at TEXT);
                PRAGMA user_version=2;''')
            for kind, key, value in (("account", "main", old), ("ledger_event", self.event["id"], self.event),
                ("ledger_event_owner", self.event["id"], {"account_id": "main"}),
                ("ledger_event_evidence", self.event["id"], []),
                ("profile", "main", {"budget": "10000", "meaning": "not_deposited_cash"}),
                ("decision", "obsolete-research", {"kind": "synthetic retired research projection"})):
                conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?)", (kind,key,canonical_bytes(value).decode(),fingerprint(value),1,AT))
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_upgrade_preserves_unknown_cash_and_history_and_repeats(self):
        original = self.path.read_bytes()
        preview = state_upgrade.upgrade(self.root, "case")
        self.assertEqual(self.path.read_bytes(), original)
        result = state_upgrade.upgrade(self.root, "case", apply=True, expected_source_hash=preview["source_hash"])
        store = Store(self.root, "case")
        self.assertEqual(store.get("account", "main"), self.account)
        self.assertEqual(store.get("ledger_event", self.event["id"]), self.event)
        self.assertIsNone(store.get("decision", "obsolete-research"))
        history = Artifacts(self.base).read_json(result["history_ref"])
        self.assertTrue(any(row["kind"] == "profile" and row["value"]["budget"] == "10000" for row in history["records"]))
        self.assertEqual(store.get("account", "main")["cash"], "0")
        self.assertTrue(store.get("account", "main")["unknown"])
        self.assertEqual(state_upgrade.upgrade(self.root, "case", apply=True)["status"], "already_current")

    def test_changed_source_is_refused_without_mutation(self):
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed since"):
            state_upgrade.upgrade(self.root, "case", apply=True, expected_source_hash="f"*64)
        self.assertEqual(self.path.read_bytes(), original)

    def test_incomplete_history_is_refused_without_inventing_facts(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DELETE FROM records WHERE kind='ledger_event'")
            conn.commit()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "history is incomplete"):
            state_upgrade.upgrade(self.root, "case")
        self.assertEqual(self.path.read_bytes(), original)

    def test_live_owner_prevents_upgrade(self):
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("INSERT INTO operations(id,owner) VALUES ('running','owner-token')")
            conn.commit()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "operation owners"):
            state_upgrade.upgrade(self.root, "case")
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
