"""Partitioned local records and read-only investment startup detection."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

MAX_SHARD_BYTES = 16 * 1024 * 1024
MAX_SMALL_BYTES = 1024 * 1024
LEDGER_TYPES = {"trade", "cashflow", "order", "holding_confirmation", "correction"}
STATUSES = {"submitted", "partial", "confirmed", "cancelled", "failed"}
UNSET = object()


class StorageError(ValueError):
    pass


def reject_constant(value):
    raise StorageError(f"Non-finite JSON value: {value}")


def default_root():
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    if os.name == "nt" and re.match(r"^/[A-Za-z]/", home):
        home = home[1].upper() + ":/" + home[3:]
    path = Path(home)
    if not path.is_absolute():
        raise StorageError("HOME must resolve to an absolute path")
    return path / ".investment"


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, ValueError) as exc:
        raise StorageError(f"Cannot read valid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StorageError(f"Expected JSON object at {path}")
    return value


def plan_path(root, plan):
    if not isinstance(plan, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", plan):
        raise StorageError("Invalid plan identifier")
    root = Path(root).resolve()
    path = (root / "plans" / plan).resolve()
    if not path.is_relative_to(root):
        raise StorageError("Plan path escapes data root")
    return path


def validate_record(record, ledger=False):
    if not isinstance(record, dict) or not isinstance(record.get("event_id"), str):
        raise StorageError("Record must be an object with a string event_id")
    if not 1 <= len(record["event_id"]) <= 256:
        raise StorageError("Invalid event_id length")
    if ledger:
        record_type, status = record.get("record_type"), record.get("status")
        if not isinstance(record_type, str) or not isinstance(status, str):
            raise StorageError("Ledger record_type and status must be strings")
        if record_type not in LEDGER_TYPES or status not in STATUSES:
            raise StorageError("Ledger record requires a valid record_type and status")


def read_records(path, ledger=False):
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line, parse_constant=reject_constant)
                validate_record(record, ledger)
                yield record
    except (OSError, ValueError) as exc:
        raise StorageError(f"Invalid record at {path}: {exc}") from exc


def path_present(path):
    if path.exists():
        return True
    if path.is_symlink():
        raise StorageError(f"Broken link at {path}")
    return False


def account_status(root, plan=None, explicit_holdings=False):
    root = Path(root)
    if explicit_holdings:
        return {"mode": "provided_holdings", "reason": "Latest user holdings take precedence", "data_root": str(root)}
    if not path_present(root):
        return {"mode": "first_investment", "reason": "Data root absent", "data_root": str(root)}
    if not root.is_dir():
        raise StorageError("Data root is not a directory")
    config_path = root / "config.json"
    config = read_object(config_path) if path_present(config_path) else {}
    plans = root / "plans"
    plans_present = path_present(plans)
    if plans_present and not plans.is_dir():
        raise StorageError("Plans path is not a directory")
    plan = plan or config.get("active_plan")
    if plan is None:
        choices = sorted(p.name for p in plans.iterdir() if path_present(p) and p.is_dir()) if plans_present else []
        if len(choices) > 1:
            return {"mode": "needs_plan_selection", "plan_count": len(choices), "plans": choices[:20]}
        plan = choices[0] if choices else None
    if plan is None:
        return {"mode": "first_investment", "reason": "No account plan", "data_root": str(root)}
    base = plan_path(root, plan)
    path_present(root / "plans" / plan)
    if base.exists() and not base.is_dir():
        raise StorageError("Plan path is not a directory")
    if not base.exists() and config.get("active_plan") == plan:
        raise StorageError("Active plan points to a missing directory")
    state_dir = base / "state"
    if path_present(state_dir) and not state_dir.is_dir():
        raise StorageError("State path is not a directory")
    current = state_dir / "current.json"
    if path_present(current):
        state = read_object(current)
        if not isinstance(state.get("account_initialized"), bool):
            raise StorageError("State must declare account_initialized")
        if state["account_initialized"]:
            if not isinstance(state.get("holdings"), list):
                raise StorageError("Initialized state must include a holdings list")
            return {"mode": "portfolio_review", "plan_id": plan, "state_path": str(current)}
    has_ledger = False
    ledger = base / "ledger"
    if path_present(ledger):
        if not ledger.is_dir():
            raise StorageError("Ledger is not a directory")
        for shard in sorted(ledger.rglob("*.jsonl")):
            for record in read_records(shard, ledger=True):
                has_ledger = True
    if has_ledger:
        return {"mode": "portfolio_review", "plan_id": plan, "reason": "Account events exist; reconcile state before analysis"}
    return {"mode": "first_investment", "plan_id": plan, "reason": "No account records; research files do not count"}


def partition_path(root, plan, collection, partition):
    parts = partition.split("/")
    if collection == "market":
        if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[0]):
            raise StorageError("Market partition must be identifier/YYYY/MM")
        date_parts = parts[1:]
    elif collection == "ledger" and len(parts) == 2:
        date_parts = parts
    elif collection == "news" and len(parts) == 3:
        date_parts = parts
    else:
        raise StorageError("Invalid collection or partition")
    if not re.fullmatch(r"\d{4}", date_parts[0]) or any(not re.fullmatch(r"\d{2}", p) for p in date_parts[1:]):
        raise StorageError("Invalid date partition")
    dt.date(int(date_parts[0]), int(date_parts[1]), int(date_parts[2]) if len(date_parts) == 3 else 1)
    base = plan_path(root, plan)
    target = (base / collection / partition).resolve()
    if not target.is_relative_to(base):
        raise StorageError("Partition escapes plan")
    return target


def append_records(root, plan, collection, partition, records, max_bytes=MAX_SHARD_BYTES):
    if not isinstance(records, list) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise StorageError("Expected records array and a positive shard limit")
    incoming = {}
    for record in records:
        validate_record(record, collection == "ledger")
        payload = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > max_bytes:
            raise StorageError("Single record exceeds shard limit; use an attachment reference")
        event = record["event_id"]
        if event in incoming and incoming[event][0] != record:
            raise StorageError(f"Conflicting duplicate event_id: {event}")
        incoming[event] = (record, payload)
    target = partition_path(root, plan, collection, partition)
    if not incoming:
        return {"written": 0, "skipped": 0, "partition": str(target)}
    target.mkdir(parents=True, exist_ok=True)
    lock = target / ".write.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StorageError(f"Partition is locked: {lock}") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        shards = sorted(target.glob("part-*.jsonl"))
        matched = set()
        for shard in shards:
            if not re.fullmatch(r"part-\d{6}\.jsonl", shard.name):
                raise StorageError("Unexpected shard filename")
            if shard.stat().st_size:
                with shard.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        raise StorageError("Shard has an incomplete trailing record; reconcile before appending")
            for old in read_records(shard, collection == "ledger"):
                event = old["event_id"]
                if event in incoming:
                    if incoming[event][0] != old:
                        raise StorageError(f"Stored event_id has different content: {event}")
                    matched.add(event)
        number = int(shards[-1].stem.split("-")[1]) if shards else 1
        current = target / f"part-{number:06d}.jsonl"
        size = current.stat().st_size if current.exists() else 0
        written = 0
        for record, payload in incoming.values():
            if record["event_id"] in matched:
                continue
            if size + len(payload) > max_bytes:
                number += 1
                if number > 999999:
                    raise StorageError("Shard numbering exhausted; create a new partition scheme")
                current = target / f"part-{number:06d}.jsonl"
                size = 0
            with current.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            size += len(payload)
            written += 1
        return {"written": written, "skipped": len(records) - written, "partition": str(target)}
    finally:
        lock.unlink()


def _small_payload(value):
    try:
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StorageError(f"Cannot serialize finite JSON: {exc}") from exc
    if len(payload) > MAX_SMALL_BYTES:
        raise StorageError("Small JSON exceeds 1 MiB; move detail to partitioned records")
    return payload


def _atomic_write_json(path, value):
    path = Path(path)
    payload = _small_payload(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_small_json(path, value):
    path = Path(path)
    resolved = path.resolve()
    if any(p.name.casefold() == "latest.json" and p.parent.name.casefold() == "indexes"
           for p in (path, resolved)):
        raise StorageError("Use update_latest for synchronized indexes/latest.json updates")
    _atomic_write_json(path, value)


def _owned_index_path(root, plan):
    root = Path(root).absolute()
    # Check aliases before resolve/mkdir: a broken link is never an empty cache.
    for path in reversed((root,) + tuple(root.parents)):
        if path_present(path) and not path.is_dir():
            raise StorageError(f"Data root ancestor is not a directory: {path}")
    root = root.resolve()
    base = plan_path(root, plan)
    intended = root / "plans" / plan
    if base != intended:
        raise StorageError("Plan alias does not own this plan's index namespace")
    for path in (root / "plans", intended, intended / "indexes"):
        present = path_present(path)
        if path.resolve() != path or path.is_symlink():
            raise StorageError(f"Index directory alias is not allowed: {path}")
        if present and not path.is_dir():
            raise StorageError(f"Index directory path is not a directory: {path}")
    index = intended / "indexes/latest.json"
    present = path_present(index)
    if index.resolve() != index or index.is_symlink():
        raise StorageError("Index file alias is not allowed")
    if present and (not index.is_file() or index.stat().st_nlink > 1):
        raise StorageError("Index must be an owned file without hard-link aliases")
    return index


def _index_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StorageError(f"Duplicate index field: {key}")
        result[key] = value
    return result


def update_latest(root, plan, changes, expected_sha256=UNSET):
    """Merge provided top-level index fields under a shared exclusive lock.

    None requires an absent index. UNSET reads and merges the latest index
    without a snapshot condition; a SHA256 requires exactly those prior bytes.
    """
    if not isinstance(changes, dict) or any(not isinstance(key, str) for key in changes):
        raise StorageError("Index changes must be a JSON object with string fields")
    _small_payload(changes)
    if expected_sha256 is not UNSET and expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
            raise StorageError("Expected index SHA256 must be 64 hexadecimal characters or None")
        expected_sha256 = expected_sha256.lower()
    index = _owned_index_path(root, plan)
    index.parent.mkdir(parents=True, exist_ok=True)
    lock = index.parent / ".latest-write.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StorageError(f"Latest index is locked: {lock}") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        if _owned_index_path(root, plan) != index:
            raise StorageError("Index owner changed before update")
        if index.exists() and index.stat().st_size > MAX_SMALL_BYTES:
            raise StorageError("Latest index exceeds the 1 MiB small-file limit")
        payload = index.read_bytes() if index.exists() else None
        actual = hashlib.sha256(payload).hexdigest() if payload is not None else None
        if expected_sha256 is not UNSET and actual != expected_sha256:
            raise StorageError("Latest index changed since the supplied snapshot; retry from current state")
        try:
            current = json.loads(payload, parse_constant=reject_constant, object_pairs_hook=_index_object) if payload is not None else {}
        except (UnicodeError, ValueError) as exc:
            raise StorageError(f"Cannot read valid latest index: {exc}") from exc
        if not isinstance(current, dict):
            raise StorageError("Latest index must be a JSON object")
        _small_payload(current)
        merged = dict(current)
        merged.update(changes)
        _small_payload(merged)
        _atomic_write_json(index, merged)
        return merged
    finally:
        os.close(fd)
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "append", "update-index"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--plan")
    parser.add_argument("--explicit-holdings", action="store_true")
    parser.add_argument("--collection", choices=["ledger", "news", "market"])
    parser.add_argument("--partition")
    parser.add_argument("--max-bytes", type=int, default=MAX_SHARD_BYTES)
    parser.add_argument("--expected-index-sha256")
    args = parser.parse_args()
    try:
        root = args.root if args.root is not None else default_root()
        if args.command == "status":
            result = account_status(root, args.plan, args.explicit_holdings)
        elif args.command == "append":
            if not args.plan or not args.collection or not args.partition:
                raise StorageError("append requires --plan, --collection and --partition")
            result = append_records(root, args.plan, args.collection, args.partition, json.load(sys.stdin), args.max_bytes)
        else:
            if not args.plan:
                raise StorageError("update-index requires --plan")
            expected = UNSET
            if args.expected_index_sha256 is not None:
                expected = None if args.expected_index_sha256 == "absent" else args.expected_index_sha256
            changes = json.load(sys.stdin, parse_constant=reject_constant, object_pairs_hook=_index_object)
            result = update_latest(root, args.plan, changes, expected)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (StorageError, OSError, ValueError) as exc:
        print(json.dumps({"mode": "data_unavailable", "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
