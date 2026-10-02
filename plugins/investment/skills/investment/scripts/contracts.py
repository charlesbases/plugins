"""The current wire contract. Parsing never guesses financial facts."""
import datetime as dt
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation

SCHEMA_VERSION = 4


class ContractError(ValueError):
    code = "invalid_input"


class ConflictError(ContractError):
    code = "input_conflict"


class EvidenceError(ContractError):
    code = "invalid_evidence"


class RetryableError(RuntimeError):
    code = "retryable"


class StaleSnapshot(ConflictError):
    code = "stale_snapshot"


def require(condition, message):
    if not condition:
        raise ContractError(message)


def fields(value, required, optional=(), label="object"):
    require(type(value) is dict, f"{label} must be an object")
    missing, extra = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    require(not missing and not extra, f"{label}: missing={sorted(missing)}, unknown={sorted(extra)}")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ContractError(f"Non-finite JSON number: {value}")


def _float(value):
    number = float(value)
    require(math.isfinite(number), "Non-finite JSON number")
    return number


def strict_json_loads(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ContractError(str(exc)) from exc


def _json_types(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_types(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_types(item)
        return
    raise ContractError("Expected finite JSON values with string object keys")


def canonical_bytes(value):
    _json_types(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def decimal_string(value, label="amount", positive=False):
    require(type(value) is str and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value),
            f"{label} must be an exact decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"Invalid {label}") from exc
    require(number.is_finite() and (not positive or number > 0), f"Invalid {label}")
    require(len(number.as_tuple().digits) <= 28 and number.as_tuple().exponent >= -12,
            f"{label} exceeds supported precision")
    return format(number.normalize(), "f") if number else "0"


def instant(value):
    require(type(value) is str, "Timestamp must be a string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("Invalid ISO timestamp") from exc
    require(parsed.tzinfo is not None, "Timestamp requires an explicit timezone")
    return parsed.astimezone(dt.timezone.utc)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


OPERATIONS = {"status", "feedback", "analysis_start", "industry_prepare", "daily_review", "news_collect", "news_assess", "research_prepare", "research_verify",
              "allocation_replay", "trial_register", "trial_decision", "trial_outcome",
              "trial_evaluate", "trial_calibrate", "audit", "archive", "profile_initialize", "risk_update", "fund_discover",
              "cashflow_prepare", "cashflow_confirm", "cashflow_correct", "plan_update", "capital_reconcile",
              "source_capture", "fee_inspect", "market_prepare"}


def validate_redemption_confirmation(event):
    """Actual confirmation is an explicit fact, never the information timestamp."""
    data = event["data"]
    stamp = data.get("actual_redemption_confirmed_at")
    if stamp is None:
        return
    require(event["type"] == "sell_fill", "Actual redemption confirmation is only valid for a sell fill")
    confirmed = instant(stamp)
    require(confirmed <= instant(event["known_at"]), "Actual redemption confirmation is not yet known")
    require(confirmed <= instant(event["recorded_at"]), "Actual redemption confirmation is not yet recorded")
    require(type(data.get("price_date")) is str, "Actual redemption confirmation requires the actual pricing date")
    priced = dt.date.fromisoformat(data["price_date"])
    from zoneinfo import ZoneInfo
    require(confirmed.astimezone(ZoneInfo("Asia/Shanghai")).date() >= priced,
            "Actual redemption confirmation precedes the actual pricing date")


def validate_feedback_shape(payload):
    fields(payload, {"currency", "events"}, {"account_id", "evidence"}, "feedback")
    require(type(payload["currency"]) is str, "Feedback currency must be text")
    require(type(payload["events"]) is list and 0 < len(payload["events"]) <= 1000,
            "Feedback requires a bounded nonempty event list")
    require(type(payload.get("evidence", {})) is dict, "Feedback evidence must be an object")
    for event in payload["events"]:
        fields(event, {"id", "type", "effective_at", "known_at", "recorded_at", "data"}, label="feedback event")
        for key in ("id", "type"):
            require(type(event[key]) is str and bool(event[key]), f"Event {key} must be nonempty text")
        require(type(event["data"]) is dict, "Event data must be an object")
        for key in ("effective_at", "known_at", "recorded_at"):
            instant(event[key])
        validate_redemption_confirmation(event)
        require(event["type"] != "cashflow", "External cash requires cashflow_prepare/confirm/correct")
        require(event["type"] != "performance_start", "Initial performance requires the confirmed profile_initialize workflow")


def parse_request(value):
    fields(value, {"schema_version", "request_id", "operation", "payload"}, {"revision_of"}, "request")
    require(type(value["schema_version"]) is int and value["schema_version"] == SCHEMA_VERSION,
            "Only the current request schema is accepted")
    require(type(value["request_id"]) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", value["request_id"]),
            "Invalid stable request_id")
    require(type(value["operation"]) is str and value["operation"] in OPERATIONS, "Unknown operation")
    require(type(value["payload"]) is dict, "payload must be an object")
    if value["operation"] == "feedback":
        validate_feedback_shape(value["payload"])
    if value["operation"] == "daily_review" and "feedback" in value["payload"]:
        validate_feedback_shape(value["payload"]["feedback"])
    if "revision_of" in value:
        require(type(value["revision_of"]) is str and value["revision_of"] != value["request_id"],
                "revision_of must identify another operation")
    canonical_bytes(value)
    return value
