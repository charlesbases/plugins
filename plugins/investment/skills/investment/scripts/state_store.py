"""One mutable database per plan; sealed history remains globally addressable."""
import contextlib
import datetime as dt
import hashlib
import os
import sqlite3
import time
import uuid
import threading
from dataclasses import dataclass
from pathlib import Path

from contracts import (ConflictError, EvidenceError, RetryableError, StaleSnapshot,
                       canonical_bytes, fingerprint, strict_json_loads, utc_now)
from artifacts import Artifacts
from file_io import plan_path, durable_replace

ROW_LIMIT = 1024 * 1024
# This is deliberately outside JSON syntax: no canonical user JSON can be
# confused with a storage envelope, including user strings with this prefix.
CAS_JSON_PREFIX = "@investment-cas-json:1:"
ARCHIVE_TRIGGER = 64 * 1024 * 1024
ACTIVE_LIMIT = 128 * 1024 * 1024
TABLES = """
CREATE TABLE IF NOT EXISTS records (
 kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, hash TEXT NOT NULL,
 immutable INTEGER NOT NULL CHECK(immutable IN (0,1)), created_at TEXT NOT NULL,
 PRIMARY KEY(kind,key));
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, request TEXT NOT NULL,
 status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 owner TEXT, pid INTEGER, lease_until REAL, generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS archives (
 path TEXT PRIMARY KEY, hash TEXT NOT NULL, records_count INTEGER NOT NULL,
 operations_count INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS administrative_lock (
 name TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL);
"""


def encode_stored_json(value, base):
    """Encode one logical JSON value without increasing the SQLite cell bound."""
    payload = canonical_bytes(value)
    if len(payload) <= ROW_LIMIT:
        return payload.decode("utf-8")
    objects = Artifacts(base)
    reference = objects.put_bytes(payload)
    layers = 0
    while True:
        encoded = CAS_JSON_PREFIX + canonical_bytes({"layers": layers, "value_ref": reference}).decode("utf-8")
        if len(encoded.encode("utf-8")) <= ROW_LIMIT:
            return encoded
        # Extremely large values can themselves need a partitioned reference
        # inventory. Seal that inventory too; the final row remains bounded.
        reference = objects.put_json(reference)
        layers += 1
        if layers > 8:
            raise EvidenceError("Artifact reference inventory exceeds storage nesting bound")


def decode_stored_json(payload, base):
    """Restore and verify storage encoding; returned JSON is never re-decoded."""
    if not payload.startswith(CAS_JSON_PREFIX):
        return strict_json_loads(payload)
    try:
        envelope = strict_json_loads(payload[len(CAS_JSON_PREFIX):])
        if (type(envelope) is not dict or set(envelope) != {"layers", "value_ref"}
                or type(envelope["layers"]) is not int or not 0 <= envelope["layers"] <= 8):
            raise EvidenceError("Invalid content-addressed JSON envelope")
        objects, reference = Artifacts(base), envelope["value_ref"]
        for _ in range(envelope["layers"]):
            reference = objects.read_json(reference)
        return objects.read_json(reference)
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as exc:
        raise EvidenceError("Stored JSON artifact is missing, malformed or changed") from exc


class LeaseLost(RetryableError):
    """This executor no longer has authority to change business state."""


@dataclass(frozen=True)
class OperationContext:
    operation_id: str
    owner: str
    generation: int



def _alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextlib.contextmanager
def _busy_boundary():
    try:
        yield
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise RetryableError("State database is busy; retry the same request") from exc
        raise


class Store:
    def __init__(self, root, plan, timeout=2.0, *, lease_seconds=120.0, heartbeat_seconds=20.0):
        self.root, self.plan = Path(root).resolve(), plan
        self.base = plan_path(self.root, plan)
        self.base.mkdir(parents=True, exist_ok=True)
        self.path = self.base / "active.sqlite"
        self.timeout = timeout
        if not 0 < heartbeat_seconds < lease_seconds / 2:
            raise ValueError("Heartbeat interval must be below half the lease duration")
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self._local = threading.local()
        if self.path.is_symlink():
            raise EvidenceError("State database cannot be a symlink")
        if not self.path.exists() and any(self.base.iterdir()):
            raise EvidenceError("Existing plan files require a verified one-time import; no format fallback")
        fresh = not self.path.exists()
        if not fresh:
            # Inspect legacy state read-only, before journal pragmas or DDL can
            # change it. Refusal must leave migration source bytes untouched.
            with _busy_boundary(), contextlib.closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True,
                                                                       timeout=self.timeout)) as reader:
                version = reader.execute("PRAGMA user_version").fetchone()[0]
                tables = {row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if version not in (0, 4) or (version == 0 and tables):
                    raise EvidenceError("State schema requires the explicit offline v4 migration")
                fresh = version == 0
                if not fresh:
                    required = {"records": {"kind", "key", "value", "hash", "immutable", "created_at"},
                                "operations": {"id", "request_hash", "request", "status", "result", "created_at", "updated_at", "owner", "pid", "lease_until", "generation"},
                                "archives": {"path", "hash", "records_count", "operations_count", "created_at"},
                                "administrative_lock": {"name", "owner", "pid"}}
                    if any(name not in tables or {row[1] for row in reader.execute("PRAGMA table_info(" + name + ")")} != columns
                           for name, columns in required.items()):
                        raise EvidenceError("State v4 table inventory differs; explicit offline repair required")
        with _busy_boundary(), contextlib.closing(self._connect()) as conn:
            if fresh:
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.executescript("BEGIN IMMEDIATE;\n" + TABLES + "\nPRAGMA user_version=4;\nCOMMIT;")

    def _connect(self):
        with _busy_boundary():
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=EXTRA")
                conn.execute("PRAGMA foreign_keys=ON")
                return conn
            except BaseException:
                conn.close()
                raise

    @contextlib.contextmanager
    def _metadata_transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise RetryableError("State database is busy; retry the same request") from exc
            raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def current_context(self):
        return getattr(self._local, "context", None)

    def require_context(self):
        context = self.current_context
        if context is None:
            raise LeaseLost("Business mutation requires an active operation lease")
        return context

    def assert_owned(self, context=None, conn=None, *, allow_terminal=False):
        context = context or self.require_context()
        with self._reader(conn) as reader:
            row = reader.execute("SELECT owner,generation,lease_until,status FROM operations WHERE id=?",
                                 (context.operation_id,)).fetchone()
            if (row is None or row["owner"] != context.owner or row["generation"] != context.generation
                    or (not allow_terminal and row["status"] != "running") or row["lease_until"] is None
                    or row["lease_until"] <= time.time()):
                raise LeaseLost("Operation ownership expired or changed; retry the same request")
        return context

    @contextlib.contextmanager
    def maintenance(self):
        """Explicit offline import/test capability; never entered by public dispatch."""
        if self.current_context is not None:
            raise ValueError("Maintenance cannot bypass an active business lease")
        if getattr(self._local, "maintenance", False):
            yield self
            return
        token = uuid.uuid4().hex
        with self._metadata_transaction() as conn:
            self._check_maintenance(conn)
            if conn.execute("SELECT 1 FROM operations WHERE owner IS NOT NULL AND lease_until>? LIMIT 1",
                            (time.time(),)).fetchone():
                raise RetryableError("Stop all operation owners before offline maintenance")
            conn.execute("INSERT INTO administrative_lock VALUES ('offline',?,?)", (token, os.getpid()))
        prior = getattr(self._local, "maintenance", False)
        self._local.maintenance = True
        self._local.maintenance_owner = token
        try:
            yield self
        finally:
            self._local.maintenance = prior
            self._local.maintenance_owner = None
            with self._metadata_transaction() as conn:
                conn.execute("DELETE FROM administrative_lock WHERE name='offline' AND owner=?", (token,))

    def _check_maintenance(self, conn):
        row = conn.execute("SELECT owner,pid FROM administrative_lock WHERE name='offline'").fetchone()
        if row is None or row["owner"] == getattr(self._local, "maintenance_owner", None):
            return
        if _alive(row["pid"]):
            raise RetryableError("Plan is locked for explicit offline maintenance")
        conn.execute("DELETE FROM administrative_lock WHERE name='offline'")

    @contextlib.contextmanager
    def transaction(self):
        context = None if getattr(self._local, "maintenance", False) else self.require_context()
        with self._metadata_transaction() as conn:
            if context is not None:
                self.assert_owned(context, conn)
            yield conn
            if context is not None:
                self.assert_owned(context, conn, allow_terminal=True)

    def attempt_directory(self, label, context=None):
        context = self.assert_owned(context)
        if not isinstance(label, str) or not label or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in label):
            raise ValueError("Invalid attempt label")
        identity = hashlib.sha256(context.operation_id.encode()).hexdigest()
        path = self.base / "work" / identity / str(context.generation) / uuid.uuid4().hex / label
        path.mkdir(parents=True, exist_ok=False)
        return path

    def renew(self, context):
        with self._metadata_transaction() as conn:
            stamp = time.time()
            changed = conn.execute("UPDATE operations SET lease_until=? WHERE id=? AND owner=? AND generation=? "
                                   "AND status='running' AND lease_until>?",
                                   (stamp + self.lease_seconds, context.operation_id, context.owner,
                                    context.generation, stamp)).rowcount
            if changed != 1:
                raise LeaseLost("Operation cannot renew an expired or superseded lease")

    @contextlib.contextmanager
    def _reader(self, conn=None):
        if conn is not None:
            yield conn
        else:
            reader = self._connect()
            try:
                reader.execute("BEGIN")
                yield reader
            finally:
                reader.rollback()
                reader.close()

    def _archives(self, conn):
        for row in conn.execute("SELECT * FROM archives ORDER BY path"):
            path = (self.base / row["path"]).resolve()
            if not path.is_relative_to(self.base / "history") or path.is_symlink():
                raise EvidenceError("Invalid archive path")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["hash"]:
                raise EvidenceError("Published archive is missing or changed")
            db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()

    def _find(self, table, clause, params, conn):
        row = conn.execute(f"SELECT * FROM {table} WHERE {clause}", params).fetchone()
        if row is not None:
            return dict(row)
        for db in self._archives(conn):
            row = db.execute(f"SELECT * FROM {table} WHERE {clause}", params).fetchone()
            if row is not None:
                return dict(row)
        return None

    def get(self, kind, key, conn=None):
        with self._reader(conn) as reader:
            row = self._find("records", "kind=? AND key=?", (kind, key), reader)
            if row is None:
                return None
            value = decode_stored_json(row["value"], self.base)
            if fingerprint(value) != row["hash"]:
                raise EvidenceError("Stored record content changed")
            return value

    def put(self, kind, key, value, immutable=True, conn=None):
        if conn is None:
            with self.transaction() as writer:
                return self.put(kind, key, value, immutable, writer)
        if not getattr(self._local, "maintenance", False):
            self.assert_owned(conn=conn)
        if type(kind) is not str or type(key) is not str or not kind or not key or len(key) > 512:
            raise ValueError("Invalid record identity")
        digest = fingerprint(value)
        current = self._find("records", "kind=? AND key=?", (kind, key), conn)
        if current:
            if current["hash"] == digest:
                if fingerprint(decode_stored_json(current["value"], self.base)) != digest:
                    raise EvidenceError("Stored record content changed")
                return value
            if current["immutable"] or immutable:
                raise ConflictError(f"Record identity has different content: {kind}/{key}")
            payload = encode_stored_json(value, self.base)
            conn.execute("UPDATE records SET value=?,hash=? WHERE kind=? AND key=?",
                         (payload, digest, kind, key))
        else:
            payload = encode_stored_json(value, self.base)
            conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?)",
                         (kind, key, payload, digest, int(immutable), utc_now()))
        return value

    def operation(self, operation_id, conn=None):
        with self._reader(conn) as reader:
            row = self._find("operations", "id=?", (operation_id,), reader)
            if row is None:
                return None
            row["request"] = decode_stored_json(row["request"], self.base)
            if fingerprint(row["request"]) != row["request_hash"]:
                raise EvidenceError("Operation input changed")
            row["result"] = decode_stored_json(row["result"], self.base) if row["result"] else None
            if row["status"] == "completed":
                sealed = self.get("operation_result", operation_id, reader)
                if sealed is None or fingerprint(sealed) != fingerprint(row["result"]):
                    raise EvidenceError("Completed operation result changed")
            return row

    def scan(self, kind, conn=None):
        """Read a complete logical collection, including published history."""
        with self._reader(conn) as reader:
            def rows(db):
                for row in db.execute("SELECT key,value,hash FROM records WHERE kind=? ORDER BY key", (kind,)):
                    value = decode_stored_json(row["value"], self.base)
                    if fingerprint(value) != row["hash"]:
                        raise EvidenceError("Stored record content changed")
                    yield row["key"], value
            yield from rows(reader)
            for archive in self._archives(reader):
                yield from rows(archive)

    def begin(self, operation_id, request):
        digest = fingerprint(request)
        with self._metadata_transaction() as conn:
            self._check_maintenance(conn)
            current = self.operation(operation_id, conn)
            if current:
                if current["request_hash"] != digest:
                    raise ConflictError("The same request_id cannot carry changed input")
                return current
            stamp = utc_now()
            conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                operation_id, digest, encode_stored_json(request, self.base), "accepted", None,
                stamp, stamp, None, None, None, 0))
            return self.operation(operation_id, conn)

    @contextlib.contextmanager
    def lease(self, operation_id):
        if self.current_context is not None:
            raise ValueError("Nested operation leases are not supported")
        token = uuid.uuid4().hex
        completed = None
        with self._metadata_transaction() as conn:
            self._check_maintenance(conn)
            row = self.operation(operation_id, conn)
            if row["status"] == "completed":
                completed = row["result"]
            elif row["owner"] and row["lease_until"] > time.time() and _alive(row["pid"]):
                raise RetryableError("This operation is already running")
            else:
                generation = row["generation"] + 1
                conn.execute("UPDATE operations SET owner=?,pid=?,lease_until=?,generation=?,status='running' WHERE id=?",
                             (token, os.getpid(), time.time() + self.lease_seconds, generation, operation_id))
        if completed is not None:
            yield completed
            return
        context = OperationContext(operation_id, token, generation)
        self._local.context = context
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    self.renew(context)
                except LeaseLost:
                    return
                except RetryableError:
                    continue  # A transient writer lock is retried before expiry.
                except (sqlite3.Error, OSError):
                    return
        worker = threading.Thread(target=heartbeat, name="investment-lease-heartbeat", daemon=True)
        worker.start()
        try:
            yield None
        finally:
            stop.set()
            worker.join(timeout=self.timeout + 1)
            self._local.context = None
            with self._metadata_transaction() as conn:
                conn.execute("UPDATE operations SET owner=NULL,pid=NULL,lease_until=NULL WHERE id=? AND owner=? AND generation=?",
                             (operation_id, token, generation))

    def stage(self, operation_id, name, result, elapsed=0):
        if self.current_context is not None and self.current_context.operation_id != operation_id:
            raise LeaseLost("Cannot publish a stage for another operation")
        value = {"operation_id": operation_id, "result": result, "elapsed_seconds": elapsed}
        key = operation_id + ":" + name
        current = self.get("stage", key)
        if current:
            if fingerprint(current["result"]) != fingerprint(result):
                raise ConflictError("Completed stage input/result cannot be replaced during retry")
            return current["result"]
        self.put("stage", key, value)
        return result

    def expected(self, items, conn):
        for item in items:
            current = self.get(item["kind"], item["key"], conn)
            if (fingerprint(current) if current is not None else None) != item["hash"]:
                raise StaleSnapshot("A bound state version changed during computation")

    def _publication_state(self, conn=None):
        """Logical database contents, excluding operation/heartbeat metadata."""
        digest = hashlib.sha256()
        with self._reader(conn) as reader:
            for table, order in (("records", "kind,key"), ("archives", "path")):
                for row in reader.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                    digest.update(canonical_bytes([table, list(row)]))
        return digest.hexdigest()

    def _write_prepared(self, item, digest, payload, conn):
        """Commit source-checked, pre-encoded JSON under the final state fence."""
        if not getattr(self._local, "maintenance", False):
            self.assert_owned(conn=conn)
        kind, key, immutable = item["kind"], item["key"], item.get("immutable", True)
        current = self._find("records", "kind=? AND key=?", (kind, key), conn)
        if current is not None:
            if current["hash"] == digest:
                return
            if current["immutable"] or immutable:
                raise ConflictError(f"Record identity has different content: {kind}/{key}")
            conn.execute("UPDATE records SET value=?,hash=? WHERE kind=? AND key=?", (payload, digest, kind, key))
        else:
            conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?)", (kind, key, payload, digest, int(immutable), utc_now()))

    def complete(self, operation_id, result, writes=(), expected=(), *, before_commit=None, commit_guard=None):
        if self.current_context is not None and self.current_context.operation_id != operation_id:
            raise LeaseLost("Cannot complete another operation")
        if self.current_context is not None:
            self.assert_owned()
        prior = self.operation(operation_id)
        if prior["status"] == "completed":
            return prior["result"]
        snapshot = self._publication_state()
        result_bytes = canonical_bytes(result)
        frozen_result = strict_json_loads(result_bytes.decode("utf-8"))
        result_payload = encode_stored_json(frozen_result, self.base)
        items = strict_json_loads(canonical_bytes(list(writes)).decode("utf-8"))
        items.append({"kind": "operation_result", "key": operation_id, "value": frozen_result, "immutable": True})
        prepared = []
        for item in items:
            kind, key = item["kind"], item["key"]
            if type(kind) is not str or type(key) is not str or not kind or not key or len(key) > 512:
                raise ValueError("Invalid record identity")
            # Validate existing stored JSON outside the writer; the raw state
            # digest below proves those bytes did not change before the write.
            self.get(kind, key)
            prepared.append((item, fingerprint(item["value"]), encode_stored_json(item["value"], self.base)))
        if before_commit is not None:
            before_commit()
        if self.current_context is not None:
            self.assert_owned()
        with self.transaction() as conn:
            prior = self.operation(operation_id, conn)
            if prior["status"] == "completed":
                return prior["result"]
            if snapshot != self._publication_state(conn):
                raise StaleSnapshot("Bound database state changed during publication verification")
            self.expected(expected, conn)
            if commit_guard is not None:
                commit_guard(result_bytes)
            for item, digest, payload in prepared:
                self._write_prepared(item, digest, payload, conn)
            # The transaction's final fence checks ownership; terminal status is
            # written only after that check and remains protected by the writer lock.
            if self.current_context is not None:
                self.assert_owned(conn=conn)
            conn.execute("UPDATE operations SET status='completed',result=?,updated_at=? WHERE id=?",
                         (result_payload, utc_now(), operation_id))
        return frozen_result

    def fail(self, operation_id, status, result):
        if self.current_context is not None and self.current_context.operation_id != operation_id:
            raise LeaseLost("Cannot fail another operation")
        with self.transaction() as conn:
            prior = self.operation(operation_id, conn)
            if prior["status"] == "completed":
                return
            conn.execute("UPDATE operations SET status=?,result=?,updated_at=? WHERE id=?",
                         (status, encode_stored_json(result, self.base), utc_now(), operation_id))

    @contextlib.contextmanager
    def _audit_reader(self):
        """Validate one consistent snapshot without blocking lease renewal."""
        snapshot = sqlite3.connect(":memory:", isolation_level=None)
        snapshot.row_factory = sqlite3.Row
        blocked = [None]
        def progress(status, remaining, total):
            if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                blocked[0] = blocked[0] or time.monotonic()
                if time.monotonic()-blocked[0] >= self.timeout:
                    raise RetryableError("Audit snapshot database is busy; retry the same request")
            else:
                blocked[0] = None
        try:
            # SQLite's online backup API copies actual committed state. Slow
            # original-artifact checks then run on this read-only copy, after
            # releasing the active database's DELETE-journal read lock.
            # https://www.sqlite.org/backup.html
            with self._reader() as source, _busy_boundary():
                source.backup(snapshot, pages=128, progress=progress, sleep=min(.01,self.timeout))
            snapshot.execute("PRAGMA query_only=ON")
            yield snapshot
        finally:
            snapshot.close()

    def audit(self, validate_value=None):
        counts = {"records": 0, "operations": 0, "archives": 0}
        identities = set()
        with self._audit_reader() as conn:
            def check(db):
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise EvidenceError("SQLite integrity check failed")
                for row in db.execute("SELECT * FROM records"):
                    identity = ("record", row["kind"], row["key"])
                    value = decode_stored_json(row["value"], self.base)
                    if identity in identities or fingerprint(value) != row["hash"]:
                        raise EvidenceError("Duplicate identity or changed record")
                    if validate_value is not None:
                        validate_value(value)
                    identities.add(identity)
                    counts["records"] += 1
                for row in db.execute("SELECT * FROM operations"):
                    identity = ("operation", row["id"])
                    request = decode_stored_json(row["request"], self.base)
                    if identity in identities or fingerprint(request) != row["request_hash"]:
                        raise EvidenceError("Duplicate identity or changed operation")
                    result = decode_stored_json(row["result"], self.base) if row["result"] else None
                    if row["status"] == "completed":
                        sealed = self.get("operation_result", row["id"], conn)
                        if sealed is None or fingerprint(sealed) != fingerprint(result):
                            raise EvidenceError("Completed operation result changed")
                    if validate_value is not None and result is not None:
                        validate_value(result)
                    identities.add(identity)
                    counts["operations"] += 1
            check(conn)
            for archive in self._archives(conn):
                check(archive)
                counts["archives"] += 1
        return {"status": "passed", **counts}

    def archive(self, max_bytes=ARCHIVE_TRIGGER):
        if type(max_bytes) is not int or max_bytes < ROW_LIMIT or max_bytes > ARCHIVE_TRIGGER:
            raise ValueError("Archive segment limit must be 1–64 MiB")
        # One writer locks the exact export set and catalog. Network and model
        # calls never occur here. A precommit crash leaves all active rows intact.
        with self.transaction() as conn:
            records, operations, size = [], [], 0
            for row in conn.execute("SELECT * FROM records WHERE immutable=1 ORDER BY created_at,kind,key"):
                # Only stages belonging to terminal operations can leave active state.
                if row["kind"] == "stage":
                    stage = decode_stored_json(row["value"], self.base)
                    operation = self.operation(stage["operation_id"], conn)
                    if not operation or operation["status"] != "completed" or operation["owner"] is not None:
                        continue
                estimated = len(row["value"].encode("utf-8")) + 1024
                if size + estimated > max_bytes // 2:
                    break
                records.append(tuple(row))
                size += estimated
            for row in conn.execute("SELECT * FROM operations WHERE status='completed' AND owner IS NULL ORDER BY created_at"):
                estimated = len(row["request"].encode("utf-8")) + len((row["result"] or "").encode("utf-8")) + 1024
                if size + estimated > max_bytes // 2:
                    break
                operations.append(tuple(row))
                size += estimated
            if not records and not operations:
                return {"status": "nothing_to_archive"}
            folder = self.base / f"history/{dt.datetime.now(dt.timezone.utc):%Y/%m}"
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / ("pending-" + uuid.uuid4().hex + ".sqlite")
            target = sqlite3.connect(temporary)
            try:
                target.executescript(TABLES)
                target.executemany("INSERT INTO records VALUES (?,?,?,?,?,?)", records)
                target.executemany("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)", operations)
                target.commit()
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise EvidenceError("Archive integrity check failed")
                exported = list(target.execute("SELECT kind,key,hash FROM records ORDER BY kind,key"))
                if exported != sorted((r[0], r[1], r[3]) for r in records):
                    raise EvidenceError("Archive export inventory differs")
            finally:
                target.close()
            if temporary.stat().st_size > max_bytes:
                temporary.unlink()
                raise ValueError("Archive exceeds bounded segment limit")
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
                digest = hashlib.sha256(stream.read()).hexdigest()
            path = folder / ("sealed-" + digest + ".sqlite")
            durable_replace(temporary, path)
            relative = path.relative_to(self.base).as_posix()
            conn.execute("INSERT INTO archives VALUES (?,?,?,?,?)",
                         (relative, digest, len(records), len(operations), utc_now()))
            conn.executemany("DELETE FROM records WHERE kind=? AND key=? AND hash=?", [(r[0], r[1], r[3]) for r in records])
            conn.executemany("DELETE FROM operations WHERE id=? AND request_hash=?", [(r[0], r[1]) for r in operations])
        with contextlib.closing(self._connect()) as conn:
            conn.execute("PRAGMA incremental_vacuum")
        return {"status": "archived", "path": relative, "records": len(records), "operations": len(operations)}

    def maintain(self):
        if self.path.stat().st_size >= ARCHIVE_TRIGGER:
            with self.maintenance():
                self.archive()
        if self.path.stat().st_size >= ACTIVE_LIMIT:
            with self._reader() as conn:
                used = (conn.execute("PRAGMA page_count").fetchone()[0] -
                        conn.execute("PRAGMA freelist_count").fetchone()[0]) * conn.execute("PRAGMA page_size").fetchone()[0]
            if used >= ACTIVE_LIMIT:
                raise RetryableError("Active state capacity exceeded; no live state was deleted")
