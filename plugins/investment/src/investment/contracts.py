"""规范编码、Schema与精确基础类型；不包含交易归并算法。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

PLUGIN = Path(__file__).resolve().parents[2]
SPEC = PLUGIN / "docs" / "spec" / "v1"
MAX_BYTES = 10485760
INPUT_RULES = ["J01", "J02", "J03", "R01", "R02"] + [f"I{n:02}" for n in range(1, 8)]
OUTPUT_RULES = INPUT_RULES + [f"A{n:02}" for n in range(1, 15)] + ["V02", "U01"]
USES = ["current_account_review", "reporting"]
MONEY_FIELDS = {"amount", "fee_amount", "shares", "requested_amount", "requested_shares", "confirmed_shares", "confirmed_amount", "received_amount", "remaining_receivable"}


class ContractError(Exception):
    """携带规则、字段位置和固定错误码的校验失败。"""

    def __init__(self, rule: str, pointer: str, reason: str, code: str = "business_invariant"):
        super().__init__(reason)
        self.rule, self.pointer, self.reason, self.code = rule, pointer or "/", reason, code


def require(condition: bool, rule: str, pointer: str, reason: str, code: str = "business_invariant") -> None:
    if not condition:
        raise ContractError(rule, pointer, reason, code)


def pointer(parts) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def known(value):
    return {"status": "known", "value": value, "reason": ""}


def unknown(reason="not_provided"):
    return {"status": "unknown", "value": None, "reason": reason}


def na(reason="not_applicable"):
    return {"status": "not_applicable", "value": None, "reason": reason}


def is_known(value) -> bool:
    return isinstance(value, dict) and value.get("status") == "known"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("offset required")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ContractError("J02", "/", "Invalid instant", "schema_violation") from exc


def bounds(optional):
    if not is_known(optional):
        return None
    raw = optional["value"]
    if raw["kind"] == "instant":
        point = instant(raw["value"])
        return point, point
    try:
        day = datetime.strptime(raw["value"], "%Y-%m-%d").date()
        lower = datetime.combine(day, time(), ZoneInfo(raw["timezone"]))
        return lower.astimezone(timezone.utc), (lower + timedelta(days=1)).astimezone(timezone.utc)
    except (ValueError, KeyError) as exc:
        raise ContractError("J02", "/", "Invalid raw date", "schema_violation") from exc


def definitely_after(left, right) -> bool:
    a, b = bounds(left), bounds(right)
    if a is None or b is None:
        return False
    if a[0] == a[1] and b[0] == b[1]:
        return a[0] > b[0]
    return a[0] >= b[1]


def exact(values) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = 80
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            total = Decimal(0)
            for value in values:
                total += Decimal(value)
            return total
    except DecimalException as exc:
        raise ContractError("J03", "/", "Inexact decimal operation") from exc


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return "0" if value == 0 else text.rstrip("0").rstrip(".") if "." in text else text


def canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ContractError("J01", "/", "Not canonical UTF-8 JSON", "invalid_json") from exc


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _integer(text):
    value = int(text)
    if text == "-0" or abs(value) > 9007199254740991:
        raise ValueError("invalid integer")
    return value


def _no_float(text):
    raise ValueError("floating point JSON token")


def parse(raw: bytes):
    try:
        require(0 < len(raw) <= MAX_BYTES, "J01", "/", "Document size out of bounds", "invalid_json")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_int=_integer, parse_float=_no_float, parse_constant=_no_float)
        require(canonical(value) == raw, "J01", "/", "Noncanonical JSON bytes", "invalid_json")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("J01", "/", "Invalid canonical JSON", "invalid_json") from exc


def check_decimal_fields(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in MONEY_FIELDS and is_known(item):
                text = item["value"]
                if isinstance(text, str) and text.startswith("-") and Decimal(text) == 0:
                    raise ContractError("J03", pointer(path + (key, "value")), "Negative zero is forbidden")
            check_decimal_fields(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_decimal_fields(item, path + (index,))


def _datetime_format(value):
    if not isinstance(value, str):
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return "T" in value and parsed.tzinfo is not None
    except ValueError:
        return False


def _uri_format(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", value) or re.search(r"%(?![0-9A-Fa-f]{2})", value) or value.count("#") > 1:
        return False
    try:
        return bool(urlsplit(value).scheme)
    except ValueError:
        return False


class Schemas:
    """从固定安装目录加载强契约；不下载Schema或读取测试产物。"""

    def __init__(self):
        documents = [json.loads((SPEC / name).read_text(encoding="utf-8")) for name in ("common.schema.json", "sc01.schema.json", "storage.schema.json", "runs.schema.json")]
        for document in documents:
            Draft202012Validator.check_schema(document)
        self.resources = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in documents)
        self.registry = parse((SPEC / "registry.json").read_bytes())
        self.method = parse((SPEC / "method.json").read_bytes())
        self.targets = {item["schema_id"]: item for item in self.registry["schemas"]}
        self.format_checker = FormatChecker()
        self.format_checker.checks("date-time")(_datetime_format)
        self.format_checker.checks("uri")(_uri_format)
        def check_formats(node):
            if isinstance(node, dict):
                if "format" in node and isinstance(node["format"], str):
                    require(node["format"] in self.format_checker.checkers, "J02", "/", "Unknown format assertion", "schema_violation")
                for child in node.values():
                    check_formats(child)
            elif isinstance(node, list):
                for child in node:
                    check_formats(child)
        check_formats(documents)
        require(not self.format_checker.conforms("2026-02-30", "date"), "J02", "/", "Date assertions unavailable", "schema_violation")
        require(not self.format_checker.conforms("2026-02-30T00:00:00Z", "date-time"), "J02", "/", "Datetime assertions unavailable", "schema_violation")
        self.validate("investment.registry", self.registry)
        self.validate("investment.method", self.method)

    def validate(self, schema_id: str, value):
        require(schema_id in self.targets, "J02", "/schema_id", "Unregistered schema", "unregistered_schema")
        target = self.targets[schema_id]["target"]
        validator = Draft202012Validator({"$ref": target}, registry=self.resources, format_checker=self.format_checker)
        error = next(validator.iter_errors(value), None)
        if error is not None:
            raise ContractError("J02", pointer(error.absolute_path), error.message[:800], "schema_violation")
        check_decimal_fields(value)
        return value

    def document(self, schema_id: str, raw: bytes):
        return self.validate(schema_id, parse(raw))

    def validate_type(self, name: str, value):
        validator = Draft202012Validator({"$ref": "urn:investment:spec:v1:common#/$defs/" + name}, registry=self.resources, format_checker=self.format_checker)
        error = next(validator.iter_errors(value), None)
        if error is not None:
            raise ContractError("J02", pointer(error.absolute_path), error.message[:800], "schema_violation")
        return value


def artifact(raw: bytes, schema_id: str, uri: str):
    return {"artifact_id": f"{schema_id}:{digest(raw)[:24]}", "schema_id": schema_id, "schema_version": 1, "media_type": "application/json", "uri": uri, "sha256": digest(raw), "byte_size": len(raw)}


def dataset(value, schema_id: str, uri: str, schemas: Schemas):
    result = artifact(canonical(value), schema_id, uri)
    table = next(t for t in schemas.registry["tables"] if t["schema_id"] == schema_id)
    result.update(table_schema_ref=artifact((SPEC / "registry.json").read_bytes(), "investment.registry", "investment-spec://v1/registry.json"), row_count=len(value["rows"]), **{key: table[key] for key in ("primary_key", "units", "null_policy")})
    return result
