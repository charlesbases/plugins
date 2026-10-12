"""Necessary numerical-policy feasibility, without changing declared policy.

The public numerical pipeline remains the authority for source, PIT, fitting,
calibration, cash and publication qualification. This module detects impossible
window/sample combinations before a host mistakes repeated acquisition for a
route to qualification. It never constructs evidence or grants trade readiness.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation
from fractions import Fraction


def _positive_integer(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(field + " must be a positive integer")
    return value


def _tail(value):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError("tail_probability must be a finite number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid tail_probability") from exc
    if not number.is_finite() or not 0 < number < 1:
        raise ValueError("tail_probability must be strictly between zero and one")
    return Fraction(number)


def _day(value, field):
    if type(value) is not str:
        raise ValueError(field + " must be an ISO calendar date")
    try:
        result = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(field + " must be an ISO calendar date") from exc
    if result.isoformat() != value:
        raise ValueError(field + " must be an ISO calendar date")
    return result


def diagnose_numeric_policy(spec, acquisition=None):
    """Check conservative necessary bounds of the existing chronological split.

    ``fit_joint_targets`` admits at most one joint origin per calendar date in
    its trailing training window. Its latter half contains at most ceil(D/2)
    dates; mature-label embargo and complete-path/source gaps only remove dates.
    ``mpc_calibration`` requires q*n_calibration >= minimum_tail_observations.
    Therefore even perfect sources cannot repair an inadequate D. The bounds
    here do not establish enough mature observations or recommend an optimal D.

    Acquisition dates describe raw collection, not a third-party version claim.
    NAV warmup is counted in original observation intervals, not trading years.
    One NAV per calendar date provides a generous best-case collection bound;
    actual holidays, maturity, historical source times and synchronized funds
    still have to be reconstructed by the original numerical validators.
    """
    if type(spec) is not dict:
        raise ValueError("Actual numerical policy is required")
    try:
        training = spec["training"]
        inference = spec["trade_policy"]["inference"]
        window = _positive_integer(training["train_window_days"], "train_window_days")
        minimum_train = _positive_integer(training["min_train_dates"], "min_train_dates")
        folds = _positive_integer(training["cv_folds"], "cv_folds")
        minimum_joint = _positive_integer(training["min_joint_dates"], "min_joint_dates")
        minimum_tail = _positive_integer(inference["minimum_tail_observations"], "minimum_tail_observations")
        tail = _tail(spec["allocation"]["tail_probability"])
    except (KeyError, TypeError) as exc:
        raise ValueError("Actual training, tail and calibration policy fields are required") from exc
    quotient = Fraction(minimum_tail, 1) / tail
    tail_minimum = (quotient.numerator + quotient.denominator - 1) // quotient.denominator
    # mpc_calibration applies its source-panel floor to the calibration panel,
    # not just the combined joint OOS records before their chronological split.
    calibration_minimum = max(20, minimum_joint, tail_minimum)
    # A split of N records into floor(N/2) and ceil(N/2) is the most generous
    # case. The production maturity embargo cannot increase its latter part.
    joint_minimum = max(minimum_joint, 2 * calibration_minimum - 1)
    calibration_upper = (window + 1) // 2
    actions = []
    if calibration_upper < calibration_minimum:
        actions.append({
            "action": "prepare_longer_real_history_and_preregister_feasible_method_window",
            "field": "training.train_window_days",
            "reason": "half_window_cannot_supply_declared_tail_observations",
            "declared_days": window,
            "maximum_calibration_origins_before_embargo": calibration_upper,
            "minimum_calibration_origins": calibration_minimum,
            "minimum_joint_OOS_origins_before_embargo": joint_minimum,
            "meaning": "necessary_lower_bound_only; do_not_lower_tail_or_calibration_requirements",
        })
    acquisition_summary = None
    if acquisition is not None:
        if type(acquisition) is not dict:
            raise ValueError("Actual acquisition parameters must be an object")
        try:
            start = _day(acquisition["start"], "research.start")
            end = _day(acquisition["end"], "research.end")
            lookback = _positive_integer(acquisition["lookback"], "research.lookback")
        except (KeyError, TypeError) as exc:
            raise ValueError("Actual research start, end and lookback are required") from exc
        if start > end or lookback < 120:
            raise ValueError("Ordered research dates and original NAV lookback >=120 are required")
        span = (end - start).days + 1
        # Features first appear after lookback original NAV intervals. There
        # can be at most one original observation per calendar date. This is
        # a best-case lower bound, not a business-day or source-calendar guess.
        acquisition_summary = {
            "calendar_dates_in_declared_range": span,
            "NAV_warmup_intervals": lookback,
            "minimum_raw_calendar_dates_before_maturity_or_holidays": joint_minimum + lookback,
            "current_feature_or_label_versions_verified": False,
        }
        if span < joint_minimum + lookback:
            actions.append({
                "action": "obtain_longer_real_original_NAV_and_news_versions",
                "field": "research.start/end",
                "reason": "raw_range_cannot_supply_minimum_joint_origins_after_NAV_warmup",
                "calendar_dates_in_declared_range": span,
                "minimum_raw_calendar_dates_before_maturity_or_holidays": joint_minimum + lookback,
                "meaning": "source_calendar_and_label_availability_must_be_verified; no_invented_old_availability",
            })
    return {
        "schema_version": 4,
        "kind": "necessary_numeric_policy_feasibility",
        "status": "structurally_insufficient" if actions else "necessary_window_bounds_satisfied",
        "trade_ready": False,
        "model_fit_executed": False,
        "declared_training_window_days": window,
        "declared_minimum_train_dates": minimum_train,
        "declared_cv_folds": folds,
        "declared_minimum_joint_dates": minimum_joint,
        "tail_probability_exact": {"numerator": tail.numerator, "denominator": tail.denominator},
        "declared_minimum_tail_observations": minimum_tail,
        "minimum_calibration_origins_from_tail_requirement": tail_minimum,
        "minimum_independent_calibration_origins": calibration_minimum,
        "minimum_joint_OOS_origins_before_embargo": joint_minimum,
        "maximum_calibration_origins_before_embargo": calibration_upper,
        "acquisition": acquisition_summary,
        "required_actions": actions,
        "qualification_not_checked": [
            "original_NAV_and_cash_entitlement_versions", "chronological_mature_industry_and_fund_training",
            "synchronous_multi_fund_full_paths", "selection_label_maturity_embargo",
            "independent_original_policy_calibration", "source_fees_and_cash_constraints",
            "preregistered_error_and_review_budgets", "current_source_and_publication_qualification",
        ],
        "scope": "necessary_count_bounds_only_not_model_source_or_investment_qualification",
        "policy_changed": False,
    }
