"""Strict NAV parsing and bounded, market-only historical research samples."""

import bisect
import collections
import datetime as dt
import json
import math
import re
import statistics
from decimal import Decimal, InvalidOperation
from pathlib import Path

CN = dt.timezone(dt.timedelta(hours=8))
POLICY_VERSION = "market-nav-research-1"
BLOCKERS = [
    "historical_NAV_publication_time",
    "historical_TT_buyability_limits",
    "historical_TT_subscription_redemption_fee_rules",
    "historical_order_confirmation_and_cash_arrival",
    "historical_surviving_and_terminated_fund_universe",
]
KINDS = {"normalized", "features", "labels"}
MAX_SHARD_BYTES = 16 * 1024 * 1024


class ResearchError(ValueError):
    pass


def _reject_constant(value):
    raise ResearchError(f"Non-finite JSON value: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ResearchError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _number(value, field, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ResearchError(f"Invalid numeric {field}")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResearchError(f"Invalid numeric {field}") from exc
    if not number.is_finite() or not math.isfinite(float(number)) or (number != 0 and float(number) == 0):
        raise ResearchError(f"Non-finite {field}")
    if positive and number <= 0:
        raise ResearchError(f"Non-positive {field}")
    return number


def _code(code):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise ResearchError("Fund code must contain exactly six digits")


def _day(value, field="date"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ResearchError(f"Invalid {field}")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ResearchError(f"Invalid {field}") from exc


def _assignment(text, name):
    matches = list(re.finditer(r"\bvar\s+" + re.escape(name) + r"\s*=\s*", text))
    if len(matches) != 1:
        raise ResearchError(f"Missing or duplicate data assignment: {name}")
    decoder = json.JSONDecoder(parse_float=Decimal, parse_constant=_reject_constant,
                               object_pairs_hook=_unique_object)
    try:
        value, end = decoder.raw_decode(text, matches[0].end())
    except (ValueError, TypeError) as exc:
        raise ResearchError(f"Invalid JSON assignment: {name}") from exc
    if not text[end:].lstrip().startswith(";"):
        raise ResearchError(f"Non-JSON data assignment: {name}")
    return value


def _timestamp(value):
    number = _number(value, "timestamp")
    if number != number.to_integral_value():
        raise ResearchError("NAV timestamp must be integral milliseconds")
    try:
        day = dt.datetime.fromtimestamp(int(number) / 1000, CN).date()
    except (OSError, OverflowError, ValueError) as exc:
        raise ResearchError("NAV timestamp is outside the supported date range") from exc
    return int(number), day


def _distribution(note):
    if not isinstance(note, str):
        raise ResearchError("Dividend note must be a string")
    if not note.strip():
        return Decimal(0)
    match = re.fullmatch(r"(?:分红[:：])?每(?:([0-9.]+))?份派现金([0-9.]+)元?", note.strip())
    if not match:
        raise ResearchError(f"Unresolved dividend or split text: {note}")
    units = _number(match[1] or "1", "dividend units", positive=True)
    amount = _number(match[2], "distribution amount")
    if amount < 0:
        raise ResearchError("Negative cash distribution")
    return _number(amount / units, "per-share cash distribution")


def _correction(record, observed):
    if not isinstance(record, dict):
        raise ResearchError("Verified correction must be an object")
    for field in ("source_url", "location"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ResearchError(f"Verified correction lacks {field}")
    if not record["source_url"].startswith("https://"):
        raise ResearchError("Verified correction source must use HTTPS")
    if not isinstance(record.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["source_sha256"]):
        raise ResearchError("Correction requires a verified issuer document SHA256")
    expected = _number(record.get("distribution_per_share"), "issuer distribution", positive=True)
    if observed not in (Decimal(0), expected):
        raise ResearchError("Provider distribution conflicts with verified issuer evidence")
    evidence = dict(record)
    evidence["distribution_per_share"] = float(expected)
    evidence["mode"] = "provider_already_matches" if observed == expected else "evidence补录"
    return expected, evidence


def parse_nav(text, code, start, end, verified_corrections):
    """Parse public JSON assignments, preserving raw notes and verified corrections.

    The caller must verify source bytes against each correction's source_sha256
    before passing that evidence. This parser never executes downloaded code.
    """
    _code(code)
    if not isinstance(text, str) or type(start) is not dt.date or type(end) is not dt.date:
        raise ResearchError("Text and date boundaries are required")
    if start > end or not isinstance(verified_corrections, dict):
        raise ResearchError("Invalid date range or correction mapping")
    for day in verified_corrections:
        _day(day, "correction date")
    nav = _assignment(text, "Data_netWorthTrend")
    cumulative = _assignment(text, "Data_ACWorthTrend")
    name = _assignment(text, "fS_name")
    if _assignment(text, "fS_code") != code:
        raise ResearchError("Provider fund identity differs from the requested code")
    if not isinstance(nav, list) or not nav or not isinstance(cumulative, list):
        raise ResearchError("NAV and cumulative NAV must be nonempty JSON arrays")
    if not isinstance(name, str) or not name.strip():
        raise ResearchError("Missing fund name")
    accumulated = {}
    cumulative_days = set()
    previous_cumulative_stamp, previous_cumulative_day = None, None
    for pair in cumulative:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ResearchError("Malformed cumulative NAV observation")
        stamp, day = _timestamp(pair[0])
        if stamp in accumulated or day in cumulative_days or (previous_cumulative_stamp is not None and (stamp <= previous_cumulative_stamp or day <= previous_cumulative_day)):
            raise ResearchError("Cumulative NAV timestamps and dates must be unique and increasing")
        accumulated[stamp] = _number(pair[1], "cumulative NAV", positive=True)
        cumulative_days.add(day)
        previous_cumulative_stamp, previous_cumulative_day = stamp, day
    rows, exact = [], []
    previous_stamp, previous_day = None, None
    stamps = set()
    for raw in nav:
        if not isinstance(raw, dict) or "x" not in raw or "y" not in raw:
            raise ResearchError("Malformed NAV observation")
        stamp, day = _timestamp(raw["x"])
        if previous_stamp is not None and (stamp <= previous_stamp or day <= previous_day):
            raise ResearchError("NAV timestamps and dates must be unique and increasing")
        previous_stamp, previous_day = stamp, day
        stamps.add(stamp)
        value = _number(raw["y"], "unit NAV", positive=True)
        if stamp not in accumulated:
            raise ResearchError("NAV timestamp has no matching cumulative NAV")
        if not start <= day <= end:
            continue
        note = raw.get("unitMoney", "")
        amount = _distribution(note)
        row = {"code": code, "date": day.isoformat(), "nav": float(value),
               "cumulative_nav": float(accumulated[stamp]), "distribution_per_share": float(amount),
               "distribution_text": note, "provider_daily_return": None,
               "historical_published_at": None}
        if day.isoformat() in verified_corrections:
            amount, evidence = _correction(verified_corrections[day.isoformat()], amount)
            row.update(distribution_per_share=float(amount), issuer_correction=evidence)
        quoted = None
        if raw.get("equityReturn") is not None:
            quoted = _number(raw["equityReturn"], "provider daily return") / 100
            row["provider_daily_return"] = float(quoted)
        rows.append(row)
        exact.append((value, accumulated[stamp], amount, quoted))
    if stamps != set(accumulated):
        raise ResearchError("NAV and cumulative NAV timestamp sets differ")
    if not rows:
        raise ResearchError("No NAV observations in the requested date range")
    differences = []
    for i in range(1, len(exact)):
        n0, c0, _, _ = exact[i - 1]
        n1, c1, amount, quoted = exact[i]
        implied = c1 - c0 - (n1 - n0)
        calculated = (n1 + amount) / n0 - 1
        # Four-decimal NAV and two-decimal percentage return rounding envelope.
        tolerance = (Decimal("0.0001") + abs(1 + (quoted or 0)) * Decimal("0.0001")) / n0 + Decimal("0.0001")
        if abs(implied - amount) > Decimal("0.00025"):
            raise ResearchError(f"Unexplained distribution or split on {rows[i]['date']}")
        if quoted is not None and abs(calculated - quoted) > tolerance:
            raise ResearchError(f"Inconsistent provider daily return on {rows[i]['date']}")
        differences.append(float(abs(calculated - quoted)) if quoted is not None else 0.0)
    dividends = [row for row in rows if row["distribution_per_share"]]
    return rows, {"code": code, "name_observed_today": name, "rows": len(rows),
                  "start": rows[0]["date"], "end": rows[-1]["date"],
                  "source_total_history_rows": len(nav), "dividends": dividends,
                  "dividend_count": len(dividends), "max_daily_return_difference": max(differences, default=0.0)}


def _drawdown(values):
    peak, result = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        result = max(result, 1 - value / peak)
    return result


def build_samples(rows, code_info, horizons, lookback, observed_at):
    """Build market prices/entitlements only; missing trading facts stay unknown."""
    if not isinstance(rows, list) or not rows or not isinstance(code_info, dict):
        raise ResearchError("Nonempty normalized rows and fund metadata are required")
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 120:
        raise ResearchError("Research lookback must be at least 120 NAV intervals")
    if not isinstance(horizons, list) or not horizons or any(isinstance(h, bool) or not isinstance(h, int) or h <= 0 for h in horizons):
        raise ResearchError("Horizons must be positive integer calendar days")
    if len(horizons) != len(set(horizons)):
        raise ResearchError("Duplicate research horizons")
    try:
        observed = dt.datetime.fromisoformat(observed_at)
    except (TypeError, ValueError) as exc:
        raise ResearchError("Invalid research observation timestamp") from exc
    if observed.utcoffset() is None:
        raise ResearchError("Research observation timestamp requires an offset")
    code = rows[0].get("code") if isinstance(rows[0], dict) else None
    _code(code)
    if code_info.get("code", code) != code:
        raise ResearchError("Fund metadata code differs from normalized rows")
    group = code_info.get("fund_group_id")
    if group is not None and (not isinstance(group, str) or not group.strip()):
        raise ResearchError("Invalid underlying portfolio group")
    days, navs, dividends = [], [], []
    for row in rows:
        if not isinstance(row, dict) or row.get("code") != code:
            raise ResearchError("Mixed or malformed normalized fund rows")
        day = _day(row.get("date"))
        if days and day <= days[-1]:
            raise ResearchError("Normalized NAV dates must be unique and increasing")
        if day > observed.astimezone(CN).date():
            raise ResearchError("NAV observation lies after this research run")
        nav = _number(row.get("nav"), "normalized NAV", positive=True)
        _number(row.get("cumulative_nav"), "normalized cumulative NAV", positive=True)
        dividend = _number(row.get("distribution_per_share"), "normalized distribution")
        if dividend < 0:
            raise ResearchError("Negative normalized distribution")
        days.append(day)
        navs.append(float(nav))
        dividends.append(float(dividend))
    rates = [(navs[i] + dividends[i]) / navs[i - 1] - 1 for i in range(1, len(rows))]
    tr = [1.0]
    for rate in rates:
        value = tr[-1] * (1 + rate)
        if not math.isfinite(value) or value <= 0:
            raise ResearchError("Non-finite or non-positive research return index")
        tr.append(value)
    features, labels = [], []
    for i in range(lookback, len(rows)):
        day = days[i].isoformat()
        values = {f"momentum_{n}_nav_observations": tr[i] / tr[i - n] - 1 for n in (20, 60, 120)}
        values["volatility_60_nav_observations_annualized_252"] = statistics.stdev(rates[i - 60:i]) * math.sqrt(252)
        values["drawdown_60_nav_intervals"] = _drawdown(tr[i - 60:i + 1])
        if not all(math.isfinite(v) for v in values.values()):
            raise ResearchError("Non-finite research feature")
        features.append({"id": code + ":" + day, "code": code, "feature_cutoff_nav_date": day,
                         "feature_window_start_date": days[i - lookback].isoformat(),
                         "feature_version": "market-nav-baseline-1", "values": values,
                         "known_max_nav_date": day, "historical_max_source_available_at": None,
                         "strict_PIT_verified": False,
                         "excluded_features": ["current_manager", "current_fees", "current_company_profile", "news"],
                         "missing_flags": list(BLOCKERS), "use_scope": "ex_post_market_research_only"})
        for horizon in horizons:
            try:
                target = days[i] + dt.timedelta(days=horizon)
            except (OverflowError, ValueError) as exc:
                raise ResearchError("Research horizon exceeds the supported date range") from exc
            j = bisect.bisect_left(days, target)
            label = {"id": code + ":" + day + ":" + str(horizon), "feature_id": code + ":" + day,
                     "code": code, "horizon_calendar_days": horizon, "horizon_origin": "feature_cutoff_nav_date",
                     "start_nav_date": day, "target_date": target.isoformat(),
                     "label_kind": "cash_distribution_entitlement_market_NAV", "historical_label_available_at": None,
                     "observed_by_this_research_run_at": observed_at, "actual_investor_net_return": None,
                     "fee_rule_id": None, "entry_order_at": None, "cash_available_at": None,
                     "strict_investor_eligibility": False, "missing_flags": list(BLOCKERS)}
            if j == len(rows):
                label.update(status="pending_future_NAV", end_nav_date=None,
                             alignment_delay_calendar_days=None, market_return=None, terminal_loss=None,
                             principal_path_loss=None, peak_drawdown=None)
            else:
                shares, cash, wealth = 1 / navs[i], 0.0, [1.0]
                for k in range(i + 1, j + 1):
                    cash += shares * dividends[k]
                    wealth.append(shares * navs[k] + cash)
                if not all(math.isfinite(v) and v > 0 for v in wealth):
                    raise ResearchError("Invalid research wealth path")
                result = wealth[-1] - 1
                label.update(status="mature_market_label", end_nav_date=days[j].isoformat(),
                             alignment_delay_calendar_days=(days[j] - target).days,
                             market_return=result, terminal_loss=max(-result, 0),
                             principal_path_loss=max(1 - min(wealth), 0), peak_drawdown=_drawdown(wealth))
            labels.append(label)
    by_horizon = {}
    for horizon in horizons:
        subset = [y for y in labels if y["horizon_calendar_days"] == horizon]
        mature = [y for y in subset if y["status"] == "mature_market_label"]
        by_horizon[str(horizon)] = {"mature_market": len(mature), "pending": len(subset) - len(mature),
                                    "qualified_historical_investor_net": 0,
                                    "max_alignment_delay_calendar_days": max((y["alignment_delay_calendar_days"] for y in mature), default=None)}
    summary = {"code": code, "fund_group_id": group, "common_exposure": code_info.get("common_exposure"),
               "feature_rows": len(features), "warmup_rows_excluded": min(lookback, len(rows)),
               "first_feature_date": features[0]["feature_cutoff_nav_date"] if features else None,
               "last_feature_date": features[-1]["feature_cutoff_nav_date"] if features else None,
               "horizons": by_horizon, "status": "generated" if features else "insufficient_history"}
    return features, labels, summary


def _directory(run, kind, code):
    _code(code)
    if not isinstance(kind, str) or kind not in KINDS:
        raise ResearchError("Unsupported research table kind")
    run = Path(run).resolve()
    if not run.is_dir():
        raise ResearchError("Research run directory is missing")
    for path in (run / kind, run / kind / code):
        if path.is_symlink():
            raise ResearchError("Research table directory must not be a symbolic link")
        if path.exists() and not path.is_dir():
            raise ResearchError("Research table path is not a directory")
        if not path.resolve().is_relative_to(run):
            raise ResearchError("Research table path escapes its run")
    return run, run / kind / code


def _record_day(record, kind, code):
    if not isinstance(record, dict) or record.get("code") != code:
        raise ResearchError("Research table record has the wrong fund code")
    field = {"normalized": "date", "features": "feature_cutoff_nav_date", "labels": "start_nav_date"}[kind]
    return _day(record.get(field), field)


def write_tables(run, kind, code, records, max_bytes=MAX_SHARD_BYTES):
    """Write exclusive, month-partitioned shards inside a newly owned table."""
    run, directory = _directory(run, kind, code)
    if not isinstance(records, list) or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_SHARD_BYTES:
        raise ResearchError("Invalid research records or shard byte limit")
    grouped = collections.defaultdict(list)
    for record in records:
        day = _record_day(record, kind, code)
        try:
            payload = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ResearchError("Record cannot be serialized as finite JSON") from exc
        if len(payload) > max_bytes:
            raise ResearchError("Research record exceeds the shard byte limit")
        grouped[day.isoformat()[:7]].append(payload)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        paths = []
        for month, lines in sorted(grouped.items()):
            parts, current = [], bytearray()
            for line in lines:
                if current and len(current) + len(line) > max_bytes:
                    parts.append(bytes(current))
                    current = bytearray()
                current.extend(line)
            if current:
                parts.append(bytes(current))
            for index, payload in enumerate(parts, 1):
                path = directory / f"{month}-part-{index:06d}.jsonl"
                with path.open("xb") as stream:
                    stream.write(payload)
                paths.append(path.relative_to(run).as_posix())
        return paths
    except OSError as exc:
        raise ResearchError(f"Cannot create exclusive research table: {exc}") from exc


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ResearchError("Non-finite JSON number")
    return result


def read_table(run, kind, code):
    """Read only owned monthly JSONL shards, rejecting corruption and links."""
    _, directory = _directory(run, kind, code)
    if not directory.is_dir():
        raise ResearchError("Research table directory is missing")
    result = []
    try:
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-part-[0-9]{6}\.jsonl", path.name):
                raise ResearchError("Unexpected research table artifact")
            if path.stat().st_size > MAX_SHARD_BYTES:
                raise ResearchError("Oversized research table shard")
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        raise ResearchError("Blank research table record")
                    record = json.loads(line, parse_float=_finite_float, parse_constant=_reject_constant,
                                        object_pairs_hook=_unique_object)
                    if _record_day(record, kind, code).isoformat()[:7] != path.name[:7]:
                        raise ResearchError("Record date does not match its monthly shard")
                    result.append(record)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResearchError(f"Cannot read valid research table: {exc}") from exc
    return result
