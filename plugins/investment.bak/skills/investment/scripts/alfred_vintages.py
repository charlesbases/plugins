"""Authenticated ALFRED as-of CSV originals, without an invented release clock.

Official semantics: https://alfred.stlouisfed.org/help . ALFRED retains values
reported on past dates. Its CSV column binds a series to the requested as-of
day; this is not a claim that every observation was first published that day.

An as-of date has no public intraday timezone contract in this adapter. UTC
midnight two days later is an explicit conservative availability bound: a
civil day in any timezone UTC-12..UTC+14 has ended by then. Today's authenticated
capture is also a valid witness if it precedes that bound. Neither witness is
backfilled into the actual capture time or described as a publication instant.
"""

import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import re
import urllib.parse

from contracts import canonical_bytes, instant, require
import source_fetch

SOURCE_ID = "alfred_series_vintage_csv"
ADAPTER_ID = "public_alfred_asof_csv_v1"
MAX_BYTES = 16 * 1024 * 1024


def quote_url(series_id, as_of_date):
    require(isinstance(series_id, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", series_id), "Invalid original ALFRED series identifier")
    day = dt.date.fromisoformat(as_of_date)
    require(day.isoformat() == as_of_date, "Canonical ALFRED as-of date required")
    return "https://alfred.stlouisfed.org/graph/alfredgraph.csv?" + urllib.parse.urlencode({"id": series_id, "vintage_date": as_of_date})


def extract(raw, capture, *, series_id, as_of_date):
    """Parse original values with a provider as-of bound, not a first-release time."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "Missing or oversized original ALFRED CSV")
    source_fetch.validate_capture_provenance(capture)
    expected_url = quote_url(series_id, as_of_date)
    require(capture["registry_source_id"] == SOURCE_ID
            and capture["requested_url"] == capture["final_url"] == expected_url
            and capture["redirect_chain"] == [], "ALFRED source/series/as-of query changed")
    require(capture["bytes"] == len(raw) and capture["raw_sha256"] == hashlib.sha256(raw).hexdigest(),
            "Original ALFRED values differ from authenticated capture")
    observed = instant(capture["retrieved_at"])
    as_of = dt.date.fromisoformat(as_of_date)
    require(as_of <= observed.date(), "Cannot request an ALFRED version dated after actual capture")
    text, encoding = source_fetch.decode(raw, capture["content_type"], capture["encoding"], source_fetch.source_rule(SOURCE_ID))
    require(encoding == capture["encoding"] and hashlib.sha256(text.encode("utf-8")).hexdigest() == capture["text_sha256"],
            "ALFRED original decoded identity changed")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    value_column = series_id + "_" + as_of.strftime("%Y%m%d")
    require(header == ["observation_date", value_column], "ALFRED CSV does not bind this original series/as-of day")
    day_bound = dt.datetime.combine(as_of + dt.timedelta(days=2), dt.time(), dt.timezone.utc)
    available_bound = min(day_bound, observed)
    records, missing, previous = [], [], None
    for index, row in enumerate(reader, 2):
        require(len(row) == 2, "Original ALFRED CSV row layout changed")
        day = dt.date.fromisoformat(row[0])
        require(row[0] == day.isoformat() and (previous is None or day > previous), "Original ALFRED dates are duplicated or unordered")
        previous = day
        # Future-period forecasts require a separate economic interpretation;
        # this level adapter cannot turn them into realized historical labels.
        require(day <= as_of, "Future-period ALFRED forecasts need a separate source contract")
        literal = row[1]
        if literal in ("", ".", "NA", "N/A"):
            missing.append({"observation_date": row[0], "csv_row": index, "source_literal": literal})
            continue
        require(isinstance(literal, str) and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", literal), "Nondecimal ALFRED value")
        try:
            number = Decimal(literal)
        except InvalidOperation as error:
            raise ValueError("Invalid original ALFRED value") from error
        require(number.is_finite(), "Nonfinite original ALFRED value")
        records.append({"series_id": series_id, "observation_date": row[0], "value_text": literal,
                        "csv_row": index, "provider_as_of_date": as_of_date, "provider_as_of_precision": "day",
                        "available_by_bound": available_bound.isoformat(),
                        "availability_witness": "authenticated_current_raw_capture" if observed <= day_bound else "authenticated_provider_as_of_day_bound",
                        "actual_capture_at": capture["retrieved_at"], "source_publication_instant": None})
    require(records, "ALFRED has no nonmissing original values in this version")
    return {"schema_version": 1, "adapter_id": ADAPTER_ID, "series_id": series_id,
            "provider_as_of_date": as_of_date, "source_value_column": value_column,
            "raw_sha256": capture["raw_sha256"], "bytes": len(raw), "actual_capture_at": capture["retrieved_at"],
            "records": records, "missing_values": missing, "publication_instant_invented": False,
            "source_level_units_inferred": False, "fund_NAV_or_investor_return_claimed": False}


def validate(value, raw, capture, *, series_id, as_of_date):
    """Re-read authenticated originals; caller flags cannot change their clock."""
    expected = extract(raw, capture, series_id=series_id, as_of_date=as_of_date)
    require(canonical_bytes(value) == canonical_bytes(expected), "ALFRED original values, as-of day or actual capture changed")
    return True


def fetch(series_id, as_of_date, *, timeout=20, max_bytes=MAX_BYTES):
    capture = source_fetch.fetch(quote_url(series_id, as_of_date), SOURCE_ID, timeout=timeout, max_bytes=max_bytes)
    value = extract(capture["raw_bytes"], capture, series_id=series_id, as_of_date=as_of_date)
    validate(value, capture["raw_bytes"], capture, series_id=series_id, as_of_date=as_of_date)
    return {"capture": capture, "version": value}
