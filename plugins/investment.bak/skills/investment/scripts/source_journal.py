"""Owner-written source reservations and append-only interruption recovery.

This journal records transport costs, not financial or publication eligibility.
An interrupted reservation retains its full allowance; original captures may
be reused only by the producer's independent raw-byte parser.
"""
import copy
import math
import re

from contracts import fingerprint, require, utc_now


def _number(value, label):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            "Invalid source journal cost: " + label)
    return value


def _rows(store, kind, operation_id):
    prefix = operation_id + ":source:"
    rows = []
    for key, value in store.scan(kind):
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        require(re.fullmatch(r"[1-9][0-9]*", suffix) is not None
                and value.get("attempt_id") == key, "Source journal identity changed")
        rows.append((int(suffix), value))
    require(len({index for index, _ in rows}) == len(rows), "Duplicate original source journal entry")
    return [copy.deepcopy(value) for _, value in sorted(rows)]


def validate_cost(store, kind, operation_id, attempts, reservations=()):
    """Re-read the full original ledger; omission cannot reset spent budget."""
    originals = _rows(store, kind, operation_id)
    reserved = _rows(store, kind + "-reservation", operation_id)
    require(fingerprint(attempts) == fingerprint(originals)
            and fingerprint(list(reservations)) == fingerprint(reserved),
            "Source receipt omits or changes original attempts/reservations")
    return _cost(originals, reserved)


def _cost(originals, reserved):
    by_id = {row["attempt_id"]: row for row in reserved}
    completed = set()
    consumed = {"source_requests": len(originals), "bytes_charged": 0, "elapsed_seconds": 0.0}
    elapsed = []
    for attempt in originals:
        require(attempt.get("status") in {"captured", "unavailable"}, "Unknown terminal source attempt")
        consumed["bytes_charged"] += _number(attempt.get("bytes_charged"), "bytes")
        elapsed.append(_number(attempt.get("elapsed_seconds"), "seconds"))
        reservation = by_id.get(attempt["attempt_id"])
        if reservation is not None:
            require(attempt["started_at"] == reservation["started_at"]
                    and attempt.get("url", attempt.get("requested_url")) == reservation["url"]
                    and attempt["bytes_charged"] <= reservation["bytes_reserved"],
                    "Completed source differs from its original reservation")
        completed.add(attempt["attempt_id"])
    for reservation in reserved:
        _number(reservation.get("bytes_reserved"), "reserved bytes")
        _number(reservation.get("timeout_seconds"), "reserved seconds")
        if reservation["attempt_id"] not in completed:
            consumed["source_requests"] += 1
            consumed["bytes_charged"] += reservation["bytes_reserved"]
            elapsed.append(reservation["timeout_seconds"])
    consumed["elapsed_seconds"] = math.fsum(elapsed)
    return consumed


class SourceJournal:
    def __init__(self, store, kind, operation_id, request):
        store.assert_owned()
        self.store, self.kind, self.operation_id = store, kind, operation_id
        prior_attempts = _rows(store, kind, operation_id)
        prior_reservations = _rows(store, kind + "-reservation", operation_id)
        seal_kind = kind + "-request"
        seal = {"request": copy.deepcopy(request), "request_hash": fingerprint(request)}
        existing = store.get(seal_kind, operation_id)
        if existing is not None:
            require(existing == seal, "Source discovery changed its frozen request/window/budget")
        else:
            # Existing pipeline operations already bind the original payload.
            # A GDELT child has no separate operation, so its parent binds the
            # original window. Never infer a new window from today's clock.
            operation = store.operation(operation_id)
            request_proved = False
            if operation and isinstance(operation.get("request"), dict) and "payload" in operation["request"]:
                require(operation["request"]["payload"] == request, "Original source operation request changed")
                request_proved = True
            if operation_id.endswith(":gdelt"):
                parent = store.operation(operation_id[:-6])
                if parent and isinstance(parent.get("request"), dict):
                    window = parent["request"].get("payload", {}).get("gdelt_window")
                    if window is not None:
                        parent_payload = parent["request"]["payload"]
                        expected = {**window, "timeout_seconds": parent_payload.get("timeout_seconds", 8)}
                        parent_attempts = _rows(store, "publisher-source-attempt", operation_id[:-6])
                        parent_reservations = _rows(store, "publisher-source-attempt-reservation", operation_id[:-6])
                        parent_cost = _cost(parent_attempts, parent_reservations)
                        for parameter, charged in (("max_source_requests", "source_requests"),
                                ("max_total_bytes", "bytes_charged"), ("max_elapsed_seconds", "elapsed_seconds")):
                            if parameter in parent_payload:
                                expected[parameter] = max(0, math.floor(parent_payload[parameter] - parent_cost[charged]))
                        require(request == expected, "Original GDELT opportunity window/budget changed")
                        request_proved = True
            require(not (prior_attempts or prior_reservations) or request_proved,
                    "Original frozen source request is missing; recovery cannot invent its window/budget")
            store.put(seal_kind, operation_id, seal, immutable=True)
        self.attempts = prior_attempts
        self.reservations = prior_reservations
        self.used = validate_cost(store, kind, operation_id, self.attempts, self.reservations)
        indices = [int(row["attempt_id"].rsplit(":", 1)[1]) for row in self.attempts + self.reservations]
        self.next_ordinal = max(indices, default=0) + 1

    def refresh(self):
        self.used.clear()
        self.used.update(_cost(self.attempts, self.reservations))
        return self.used

    def reserve(self, url, bytes_reserved, timeout_seconds, **metadata):
        require(type(bytes_reserved) is int and bytes_reserved > 0
                and _number(timeout_seconds, "reservation timeout") > 0,
                "Positive original source reservation required")
        value = {**copy.deepcopy(metadata), "attempt_id": self.operation_id + ":source:" + str(self.next_ordinal),
                 "url": url, "bytes_reserved": bytes_reserved,
                 "timeout_seconds": timeout_seconds, "started_at": utc_now()}
        self.store.put(self.kind + "-reservation", value["attempt_id"], value, immutable=True)
        self.next_ordinal += 1
        self.reservations.append(value)
        self.refresh()
        return copy.deepcopy(value)

    def commit(self, attempt, original=None):
        """Commit the original receipt and terminal attempt in one owner transaction."""
        reservation = next((row for row in self.reservations if row["attempt_id"] == attempt["attempt_id"]), None)
        require(reservation is not None and not any(row["attempt_id"] == attempt["attempt_id"] for row in self.attempts),
                "New source completion has no unique original reservation")
        require(attempt["started_at"] == reservation["started_at"]
                and attempt.get("url", attempt.get("requested_url")) == reservation["url"]
                and _number(attempt.get("bytes_charged"), "bytes") <= reservation["bytes_reserved"],
                "Source completion changed its reserved identity/cost")
        _number(attempt.get("elapsed_seconds"), "seconds")
        with self.store.transaction() as conn:
            if original is not None:
                self.store.put(self.kind + "-original", attempt["attempt_id"], original, immutable=True, conn=conn)
            self.store.put(self.kind, attempt["attempt_id"], attempt, immutable=True, conn=conn)
        self.attempts.append(copy.deepcopy(attempt))
        self.attempts.sort(key=lambda row: int(row["attempt_id"].rsplit(":", 1)[1]))
        self.refresh()

    def original(self, attempt_id):
        return self.store.get(self.kind + "-original", attempt_id)
