"""封闭URI定位与实际字节校验。"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from investment.contracts import ContractError, SPEC, Schemas, artifact, canonical, digest, parse, require


def safe_path(root: Path, *parts: str) -> Path:
    target = root.joinpath(*parts)
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError("R01", "/uri", "Path escapes runtime root", "invalid_reference") from exc
    return target


class Resolver:
    """只读运行文件、必要数据库节点及本次待验收节点。"""

    def __init__(self, root: Path, schemas: Schemas, staging=None, fixture_mode=False):
        self.root, self.schemas = root.resolve(), schemas
        self.fixture_mode = fixture_mode
        require(staging is None, "R01", "/", "Staging overlays are unsupported", "invalid_reference")

    def current(self, account):
        database = self.root / "data" / "investment.sqlite"
        if not database.exists():
            return {"meta": None, "trades": {"rows": []}}
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as con:
                con.execute("BEGIN")
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                require(tables == {"trades", "ledger_meta"}, "D01", "/", "Unsupported database layout; existing data was not changed")
                row = con.execute("SELECT meta_json FROM ledger_meta WHERE account_id=?", (account,)).fetchone()
                rows = [parse(r[0].encode()) for r in con.execute("SELECT trade_json FROM trades WHERE account_id=? ORDER BY trade_id", (account,))]
                con.execute("COMMIT")
            result = {"meta": parse(row[0].encode()) if row else None, "trades": {"rows": rows}}
            self.schemas.validate("investment.ledger_baseline", result)
            require(row is not None or not rows, "D01", "/", "Orphan trade rows")
            if row:
                meta = result["meta"]
                scope = {k: meta[k] for k in ("account_id", "platform", "currency", "opening", "source_authority", "known_as_of", "transaction_as_of")}
                require(meta["account_id"] == account and meta["ledger_digest"] == digest(canonical({"scope": scope, "trades": result["trades"]})), "D01", "/ledger_digest", "Ledger content digest mismatch")
            return result
        except sqlite3.DatabaseError as exc:
            raise ContractError("D01", "/", "Cannot read transaction store") from exc

    def read(self, uri: str) -> bytes:
        require(not any(c in uri for c in ("%", "\\", "..", "?")), "R01", "/uri", "Forbidden URI syntax", "invalid_reference")
        parsed = urlsplit(uri)
        require(not parsed.query, "R01", "/uri", "Queries are forbidden", "invalid_reference")
        try:
            if parsed.scheme == "investment-spec":
                name = parsed.path.lstrip("/")
                require(parsed.netloc == "v1" and name in ("registry.json", "method.json") and not parsed.fragment, "R01", "/uri", "Unknown specification locator", "invalid_reference")
                return (SPEC / name).read_bytes()
            if parsed.scheme == "investment-run":
                parts = parsed.path.strip("/").split("/")
                require(parsed.netloc == "sc01" and len(parts) in (3, 4) and re.fullmatch(r"\d{8}", parts[0]) and re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]) and not parsed.fragment, "R01", "/uri", "Invalid run locator", "invalid_reference")
                if len(parts) == 4:
                    require(parts[2] in ("attempt-01", "attempt-02", "attempt-03"), "R01", "/uri", "Invalid attempt directory", "invalid_reference")
                require(bool(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json", parts[-1])), "R01", "/uri", "Invalid artifact filename", "invalid_reference")
                return safe_path(self.root, "runs", "sc01", *parts).read_bytes()
            if parsed.scheme == "fixture":
                require(self.fixture_mode and not parsed.fragment, "R01", "/uri", "Fixture access forbidden", "invalid_reference")
                relative = (parsed.netloc + parsed.path).strip("/")
                if relative == "registry.json":
                    return (SPEC / relative).read_bytes()
                return safe_path(SPEC / "examples", *relative.split("/")).read_bytes()
            raise ContractError("R01", "/uri", "Historical data locators are unsupported; use frozen run artifacts", "invalid_reference")
        except (OSError, KeyError, ValueError) as exc:
            raise ContractError("R01", "/uri", "Reference is unreadable", "invalid_reference") from exc

    def frozen_invocation(self, input_ref, run_id, invocation_id):
        """执行判断时点只取受信控制清单，不能由业务输入填写。"""
        parsed = urlsplit(input_ref["uri"])
        parts = parsed.path.strip("/").split("/")
        require(parsed.scheme == "investment-run" and parsed.netloc == "sc01" and not parsed.fragment and not parsed.query and len(parts) == 3 and re.fullmatch(r"\d{8}", parts[0]) and parts[1] == invocation_id and parts[2] == "input.json", "I07", "/invocation_id", "Frozen run input required", "source_mismatch")
        try:
            raw = safe_path(self.root, "runs", "sc01", parts[0], "manifest.json").read_bytes()
        except OSError as exc:
            raise ContractError("I07", "/invocation_id", "Frozen invocation manifest missing", "missing_evidence") from exc
        manifest = self.schemas.document("investment.daily_manifest", raw)
        matches = [inv for inv in manifest["invocations"] if inv["invocation_id"] == invocation_id]
        require(manifest["execution_date"] == parts[0] and len(matches) == 1 and matches[0]["run_id"] == run_id and matches[0]["frozen_input_ref"] == input_ref, "I07", "/invocation_id", "Frozen invocation binding mismatch", "source_mismatch")
        return matches[0]

    def resolve(self, reference, expected=None, binding_only=False):
        try:
            return self._resolve(reference, expected, binding_only)
        except ContractError as error:
            if not hasattr(error, "object_ref"):
                try:
                    raw = self.read(reference["uri"])
                    sid = expected or reference["schema_id"]
                    error.object_ref = artifact(raw, sid, reference["uri"]) if sid in self.schemas.targets else None
                except (ContractError, KeyError):
                    error.object_ref = None
            raise

    def _resolve(self, reference, expected=None, binding_only=False):
        self.schemas.validate_type("DatasetRef" if "row_count" in reference else "ArtifactRef", reference)
        require(expected is None or reference["schema_id"] == expected, "R01", "/schema_id", "Reference role mismatch", "identity_mismatch")
        require(reference["schema_id"] in self.schemas.targets and reference["schema_version"] == 1 and reference["media_type"] == "application/json", "R01", "/schema_id", "Unregistered reference type", "invalid_reference")
        raw = self.read(reference["uri"])
        require(len(raw) == reference["byte_size"] and digest(raw) == reference["sha256"], "R01", "/sha256", "Reference hash or byte size mismatch", "hash_mismatch")
        if binding_only:
            return raw
        value = self.schemas.document(reference["schema_id"], raw)
        if "row_count" in reference:
            registry = self.resolve(reference["table_schema_ref"], "investment.registry")
            require(registry == self.schemas.registry, "R02", "/table_schema_ref", "Untrusted table registry")
            table = next((t for t in registry["tables"] if t["schema_id"] == reference["schema_id"]), None)
            require(table is not None, "R02", "/schema_id", "Unregistered table")
            for key in ("primary_key", "units", "null_policy"):
                require(reference[key] == table[key], "R02", "/" + key, "Table metadata mismatch")
            require(reference["row_count"] == len(value["rows"]), "R02", "/row_count", "Incorrect row count")
            keys = [tuple(canonical(row[key]) for key in table["primary_key"]) for row in value["rows"]]
            require(len(keys) == len(set(keys)), "R02", "/rows", "Duplicate table key", "duplicate_key")
        return value
