"""Explicit atomic offline account upgrade; no runtime format fallback."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from artifacts import Artifacts
from contracts import canonical_bytes, fingerprint, require, strict_json_loads, utc_now
from file_io import plan_path
import ledger

FACT_KINDS = {"ledger_event", "ledger_event_owner", "ledger_event_evidence", "ledger_event_alias",
              "financial_fact", "account_confirmation", "account_import", "capital_baseline",
              "capital_current", "capital_revision", "risk_profile", "risk_revision", "plan_constraints",
              "plan_revision", "cashflow_identity", "cashflow_revision", "cashflow_confirmation"}
ECONOMIC_FIELDS = ("currency", "cash", "units", "lots", "orders", "receivables", "pending_subscriptions",
                   "marks", "unknown", "sequence", "last_event", "effective_at", "known_at", "recorded_at")


def _records(conn, base):
    records = {}
    def adopt(reader):
        for row in reader.execute("SELECT kind,key,value,hash,immutable,created_at FROM records"):
            value = strict_json_loads(row[2])
            require(fingerprint(value) == row[3], "Historical record fingerprint differs")
            identity = (row[0], row[1])
            require(identity not in records or records[identity][3] == row[3], "Conflicting archived fact")
            records[identity] = tuple(row)
    adopt(conn)
    for item in conn.execute("SELECT path,hash FROM archives"):
        archive = (base / item[0]).resolve()
        require(archive.is_relative_to(base / "history") and archive.is_file() and not archive.is_symlink(), "Invalid historical archive")
        require(hashlib.sha256(archive.read_bytes()).hexdigest() == item[1], "Historical archive changed")
        with contextlib.closing(sqlite3.connect(archive.as_uri() + "?mode=ro&immutable=1", uri=True)) as reader:
            adopt(reader)
    return records


def _projection(records):
    accounts = [key for kind, key in records if kind == "account" and not key.startswith("trial:")]
    require(set(accounts) <= {"main"}, "Reconcile additional real accounts before upgrade")
    row = records.get(("account", "main"))
    if row is None:
        return None, None
    before = strict_json_loads(row[2])
    events = []
    for (kind, identity), item in records.items():
        if kind != "ledger_event":
            continue
        owner = records.get(("ledger_event_owner", identity))
        require(owner is not None, "Historical ledger event has no account owner")
        if strict_json_loads(owner[2]) == {"account_id": "main"}:
            events.append(strict_json_loads(item[2]))
    require(bool(events) or before["sequence"] == 0, "Financial history is incomplete")
    after = ledger.rebuild(ledger.initial_state(before["currency"]), sorted(events, key=lambda event: event["sequence"]), utc_now())
    for key in ECONOMIC_FIELDS:
        require(before[key] == after[key], "Upgrade changes economic fact: " + key)
    return before, after


def upgrade(root, plan, *, apply=False, expected_source_hash=None):
    base = plan_path(Path(root).resolve(), plan)
    path = base / "active.sqlite"
    require(path.is_file() and not path.is_symlink(), "An existing account database is required")
    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=2)) as conn:
        conn.execute("PRAGMA synchronous=EXTRA")
        conn.execute("BEGIN EXCLUSIVE" if apply else "BEGIN")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 4:
                conn.rollback()
                return {"status": "already_current", "schema_version": 4, "changed": False}
            require(version == 2, "Unrecognized account history requires a reviewed import")
            require(conn.execute("SELECT 1 FROM operations WHERE owner IS NOT NULL LIMIT 1").fetchone() is None,
                    "Stop and reconcile operation owners before offline upgrade")
            source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            require(not apply or expected_source_hash == source_hash, "Database changed since the upgrade preview")
            records = _records(conn, base)
            from verify import references
            for (kind, key), row in records.items():
                if kind in FACT_KINDS | {"account", "profile"} and not key.startswith("trial:"):
                    references(strict_json_loads(row[2]), Artifacts(base))
            before, after = _projection(records)
            kept = [row for (kind, key), row in records.items() if kind in FACT_KINDS and not key.startswith("trial:")]
            result = {"status": "ready_to_upgrade", "schema_version": 4, "source_hash": source_hash,
                "before_account_hash": fingerprint(before), "after_account_hash": fingerprint(after),
                "economic_fields_unchanged": list(ECONOMIC_FIELDS), "financial_records_preserved": len(kept),
                "research_policy": "invalidate_and_regenerate_from_current_sources", "changed": False}
            if not apply:
                conn.rollback()
                return result
            original = [{"kind": row[0], "key": row[1], "value": strict_json_loads(row[2]), "hash": row[3]}
                        for row in records.values() if row[0] in FACT_KINDS | {"account", "profile"}
                        and not row[1].startswith("trial:")]
            history_ref = Artifacts(base).put_json({"kind": "authentic_account_history", "source_hash": source_hash, "records": original})
            for table in ("records", "operations", "archives"):
                conn.execute("DELETE FROM " + table)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(operations)")}
            if "generation" not in columns:
                conn.execute("ALTER TABLE operations ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE TABLE IF NOT EXISTS administrative_lock (name TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL)")
            for row in kept:
                conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?)", row)
            def put(kind, key, value, immutable):
                conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?)",
                             (kind, key, canonical_bytes(value).decode(), fingerprint(value), int(immutable), utc_now()))
            if after is not None:
                put("account", "main", after, False)
            if ("capital_baseline", "main") in records and ("capital_current", "main") not in records:
                put("capital_current", "main", strict_json_loads(records["capital_baseline", "main"][2]), False)
            put("account_upgrade", "schema4", {**result, "history_ref": history_ref, "applied_at": utc_now()}, True)
            conn.execute("PRAGMA user_version=4")
            require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Upgraded SQLite integrity failed")
            conn.commit()
            return {**result, "status": "upgraded", "changed": True, "history_ref": history_ref}
        except BaseException:
            conn.rollback()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-source-hash")
    args = parser.parse_args(argv)
    try:
        result = upgrade(args.root, args.plan, apply=args.apply, expected_source_hash=args.expected_source_hash)
    except (ValueError, OSError, sqlite3.Error) as exc:
        result = {"status": "reconciliation_required", "reason": str(exc), "changed": False}
    print(json.dumps(result, ensure_ascii=False))
    return int(result["status"] == "reconciliation_required")


if __name__ == "__main__":
    raise SystemExit(main())
