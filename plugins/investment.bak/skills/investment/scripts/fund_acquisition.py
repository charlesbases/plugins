"""Source availability for acquisition scheduling, never return or buy ranking.

Today's source count cannot prove that the same bytes or fund universe were
available historically. No fund is excluded from financial comparison here.
"""
import datetime as dt
import re

from contracts import require
from research_data import _assignment, _number, _timestamp


def profile_readiness(fields, locators):
    """Retain exact labelled cells; ambiguous dates and absent labels stay unknown."""
    date = None
    evidence = []
    dates = set()
    unresolved_date = False
    for label in ("成立日期", "基金成立日", "基金成立日期", "成立日期/规模", "成立日期／规模"):
        cell = fields.get(label)
        if cell is None:
            continue
        literal = cell["text"]
        evidence.append({"field": "inception_date", "label": label, "literal": literal,
                         "locator": locators[label]})
        match = re.fullmatch(r"\s*(\d{4})(?:年|-)(\d{1,2})(?:月|-)(\d{1,2})日?(?:\s*[/／].*)?\s*", literal)
        if match:
            try:
                dates.add(dt.date(*map(int, match.groups())).isoformat())
            except ValueError:
                unresolved_date = True
        else:
            unresolved_date = True
    if len(dates) == 1 and not unresolved_date:
        date = next(iter(dates))
    states = []
    for label in ("运作状态", "基金状态", "运作状况"):
        cell = fields.get(label)
        if cell is not None and cell["text"] not in ("", "--", "---", "暂无", "暂无数据"):
            states.append(cell["text"])
            evidence.append({"field": "operation_status", "label": label, "literal": cell["text"],
                             "locator": locators[label]})
    return {"inception_date": date, "operation_status": states[0] if len(set(states)) == 1 else None,
            "evidence": evidence,
            "scope": "current_labelled_profile_cells_not_historical_share_identity_or_buyability"}


def nav_availability(text, code):
    """Verify only subject, positive observations and matched increasing arrays.

    Distribution/return reconciliation still belongs to the original research
    parser. This precheck never produces normalized prices or model samples.
    """
    require(isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code), "NAV availability needs a six-digit fund code")
    require(_assignment(text, "fS_code") == code, "NAV availability subject code differs")
    name = _assignment(text, "fS_name")
    require(isinstance(name, str) and name.strip(), "NAV availability source name missing")
    nav = _assignment(text, "Data_netWorthTrend")
    cumulative = _assignment(text, "Data_ACWorthTrend")
    require(isinstance(nav, list) and isinstance(cumulative, list), "NAV availability needs two source arrays")
    cumulative_stamps, previous_stamp, previous_day = set(), None, None
    for pair in cumulative:
        require(isinstance(pair, list) and len(pair) == 2, "Malformed cumulative NAV availability observation")
        stamp, day = _timestamp(pair[0])
        require(previous_stamp is None or (stamp > previous_stamp and day > previous_day),
                "Cumulative NAV availability dates are not unique and increasing")
        _number(pair[1], "cumulative NAV", positive=True)
        cumulative_stamps.add(stamp)
        previous_stamp, previous_day = stamp, day
    stamps, days, previous_stamp, previous_day = set(), [], None, None
    for row in nav:
        require(isinstance(row, dict) and "x" in row and "y" in row, "Malformed NAV availability observation")
        stamp, day = _timestamp(row["x"])
        require(previous_stamp is None or (stamp > previous_stamp and day > previous_day),
                "NAV availability dates are not unique and increasing")
        _number(row["y"], "unit NAV", positive=True)
        require(stamp in cumulative_stamps, "NAV availability timestamp lacks cumulative observation")
        stamps.add(stamp)
        days.append(day.isoformat())
        previous_stamp, previous_day = stamp, day
    require(stamps == cumulative_stamps, "NAV availability and cumulative timestamp sets differ")
    return {"code": code, "name_observed_today": name, "observation_count": len(nav),
            "first_price_date": days[0] if days else None, "last_price_date": days[-1] if days else None,
            "status": "source_observations_available" if nav else "source_empty",
            "scope": "current_original_array_availability_not_return_reconciliation_PIT_or_model_qualification",
            "trade_ready": False}


def priority(identity, availability=None):
    """Evidence completion order only; no price value or return enters this key."""
    state = (availability or {}).get("status")
    source_rank = 0 if state == "source_observations_available" else 2 if state == "source_empty" else 1
    readiness = identity.get("source_readiness_profile", {})
    inception = readiness.get("inception_date")
    # A disclosed date orders evidence work, but does not establish share age.
    return source_rank, inception is None, inception or "", identity["code"]


def coverage_counts(entries, scope, captured_codes, identities, records, groups):
    """Keep full current-directory denominators, including unexamined subjects."""
    available = {code for code, row in records.items()
                 if (row.get("availability") or {}).get("status") == "source_observations_available"}
    empty = {code for code, row in records.items()
             if (row.get("availability") or {}).get("status") == "source_empty"}
    identity_ready = {code for code, row in identities.items() if row.get("currency") == "CNY"
        and row.get("execution_venue") == "off_exchange_nav"
        and not any(gap.get("critical_identity") for gap in row.get("source_evidence_gaps", []))}
    complete = [group for group in groups if group["coverage_status"].startswith("complete_")]
    return {"current_TT_catalog_subjects": len(entries), "current_relevant_scope_subjects": len(scope),
        "captured_profile_subjects": len(captured_codes), "verified_current_identity_subjects": len(identities),
        "source_identity_ready_subjects": len(identity_ready), "NAV_prechecked_subjects": len(records),
        "NAV_nonempty_source_subjects": len(available), "NAV_empty_source_subjects": len(empty),
        "NAV_unavailable_or_rejected_source_subjects": len(set(records) - available - empty),
        "relevant_scope_profiles_not_captured": len(set(scope) - set(captured_codes)),
        "relevant_scope_NAV_not_prechecked": len(set(scope) - set(records)),
        "observed_comparison_groups": len(groups), "complete_comparison_groups": len(complete),
        "pending_comparison_groups": len(groups) - len(complete),
        "denominator_scope": "whole_current_TT_directory_and_news_derived_scope_not_only_successful_funds",
        "majority_coverage_asserted": False, "trade_ready": False}
