"""Forward-only news-conditioned benchmark covariates for the fund model.

Benchmark returns never add to fund cash outcomes or downstream scenarios.
"""
import datetime as dt
import math
import warnings
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import StandardScaler

from contracts import fingerprint, instant, require
import news_economics
import asset_domains

INPUT_SCHEMA_ID = asset_domains.INPUT_SCHEMA_ID

OBSERVATION_FEATURE_NAMES = ["observed_source_facts_7d", "observed_source_facts_30d",
                 "observed_source_facts_90d", "observed_revisions_30d",
                 "newest_fact_age_days", "mean_fact_age_30d",
                 "effective_source_facts_30d", "benchmark_observation_age_days"]
FEATURE_NAMES = list(news_economics.FEATURE_NAMES) + OBSERVATION_FEATURE_NAMES
TARGET_NAMES = ["asset_pricing_target", "asset_terminal_target"]
FUND_FEATURE_NAMES = [(f"asset_{role}_{phase}" if phase == "available_indicator" else f"asset_{role}_{phase}_target") for role in asset_domains.ROLES for phase in ("pricing", "terminal", "available_indicator")] + [
    "asset_unmapped_weight", "asset_stale_disclosure_share", "asset_model_estimate_indicator"]
PHASE = "first_source_observation_date_strictly_after_source_local_decision_date"


def origin_at(day, order_time_local):
    return day + "T" + order_time_local + "+08:00"


def _day(value):
    result = dt.date.fromisoformat(value)
    require(result.isoformat() == value, "Industry date must be canonical")
    return result


def _known(event):
    require(event.get("event_type", event.get("kind")) == "source_fact_presence", "Only quoted objective source fact events are model inputs")
    values = [event.get(key) for key in ("known_at", "first_seen_at")]
    require(event.get("published_at"), "News source publication metadata required")
    if isinstance(event["published_at"], str):
        values.append(event["published_at"])
    values += [event[key] for key in ("source_observed_at", "first_available_at", "available_at") if event.get(key)]
    require(all(values), "News event has no genuine publication/first-seen boundary")
    require(event.get("evidence_refs"), "News event lacks source quote evidence")
    return max(values, key=instant)


def prices_at(sector, at):
    """Choose observed price versions using their genuine availability time."""
    selected = {}
    for row in sector.get("price_versions", sector["prices"]):
        if instant(row["available_at"]) > instant(at):
            continue
        old = selected.get(row["date"])
        if old is None or (instant(row["available_at"]), fingerprint(row)) > (instant(old["available_at"]), fingerprint(old)):
            selected[row["date"]] = row
    return [selected[day] for day in sorted(selected)]


def _feature(sector, day, order_time_local):
    at = origin_at(day, order_time_local)
    relation = sector.get("sector_relation")
    if relation is not None:
        require(relation.get("status") == "verified" and relation.get("known_at") is not None
                and instant(relation["known_at"]) <= instant(at),
                "Sector relation was not source-verified at this origin")
    source_day = instant(at).astimezone(ZoneInfo(sector["timezone"])).date().isoformat()
    prices = [row for row in prices_at(sector, at) if row["date"] <= source_day]
    require(prices, "No point-in-time benchmark observation")
    base = max(prices, key=lambda row: row["date"])
    coverage, source_events = sector.get("coverage", {}), sector["features"]
    economic_observations = sector.get("economic_observations", [])
    if "news_scopes" in sector:
        observed_scopes = [scope for scope in sector["news_scopes"]
                           if instant(scope["coverage"]["available_at"]) <= instant(at)]
        scopes = [scope for scope in observed_scopes
                  if instant(scope["coverage"]["covered_from_at"]) <= instant(at)
                  < instant(scope["coverage"]["covered_until_at"])]
        require(scopes, "No archived finite news observation scope at this origin")
        scope = max(scopes, key=lambda value: instant(value["coverage"]["available_at"]))
        coverage = scope["coverage"]
        source_events = list({(row["event_id"], row.get("source_revision_id", row["revision_id"])): row
                             for known_scope in observed_scopes for row in known_scope["features"]}.values())
        economic_observations = list({row["observation_id"]: row for known_scope in observed_scopes
                                     for row in known_scope.get("economic_observations", [])}.values())
    absent = (coverage.get("absence_proven") is True and coverage.get("absence_proof_scope") == "sealed_finite_input_set"
              and coverage.get("available_at") is not None and instant(coverage["available_at"]) <= instant(at)
              and instant(coverage["covered_from_at"]) <= instant(at) < instant(coverage["covered_until_at"]))
    events = [event for event in source_events if instant(_known(event)) <= instant(at)]
    require(events or absent,
            "No source-bound news event or proven observation coverage")
    recent_events = [event for event in events if (instant(at)-instant(_known(event))).total_seconds() <= 30*86400]
    economics = news_economics.factual_event_features(economic_observations, sector["sector_id"], at, events=recent_events)
    require(economics["feature_names"] == list(news_economics.FEATURE_NAMES), "Economic feature contract differs")
    recent_events = [event for event in events if (instant(at)-instant(_known(event))).total_seconds() <= 30*86400]
    require(all(row["status"] in ("measured", "qualitative_encoded") for row in economics["event_entity_states"]),
            "A source event lacks its own verified economic encoding; aggregate old measurements cannot close this event gap")
    ages = [(instant(at)-instant(_known(event))).total_seconds()/86400 for event in events]
    recent = [age for age in ages if age <= 30]
    revisions = {event.get("source_revision_id", event["revision_id"]) for event, age in zip(events, ages) if age <= 30}
    effective = [event for event, age in zip(events, ages) if age <= 30
                 and event.get("effective_at") is not None
                 and instant(event["effective_at"]) <= instant(at)]
    auxiliary = [sum(age <= window for age in ages) for window in (7, 30, 90)]
    auxiliary += [len(revisions), min(ages) if ages else 90.,
               math.fsum(recent)/len(recent) if recent else 30., len(effective),
               (_day(day)-_day(base["date"])).days]
    vector = economics["feature_values"] + auxiliary
    witness = {"decision_date": day, "origin_at": at, "benchmark_base": base,
               "events": events, "economic_features": economics, "coverage": coverage,
               "feature_names": FEATURE_NAMES, "x": vector, "input_schema_id": INPUT_SCHEMA_ID,
               "asset_domain": sector["asset_domain"]}
    if relation is not None:
        witness["sector_relation"] = relation
    witness["source_hash"] = fingerprint(witness)
    return vector, witness


feature_at = _feature


def label_versions(sector, feature_source, day, horizon_days):
    """Rebuild outcome vintages; late corrections remain late information."""
    prices = sector["prices"]
    origin = instant(feature_source["origin_at"])
    source_day = origin.astimezone(ZoneInfo(sector["timezone"])).date().isoformat()
    phase = next((price for price in prices if price["date"] > source_day), None)
    target = (origin+dt.timedelta(days=horizon_days)).astimezone(ZoneInfo(sector["timezone"])).date().isoformat()
    terminal = next((price for price in prices if price["date"] >= target), None)
    if phase is None or terminal is None or phase["date"] > terminal["date"]:
        return []
    source_versions = sector.get("price_versions", prices)
    relevant = [row for row in source_versions if row["date"] in (phase["date"], terminal["date"])]
    versions = []
    for at in sorted({row["available_at"] for row in relevant}, key=instant):
        observed = {row["date"]: row for row in prices_at(sector, at)}
        if phase["date"] not in observed or terminal["date"] not in observed:
            continue
        base = feature_source["benchmark_base"]
        source = {"sector_id": sector["sector_id"], "benchmark_id": sector["benchmark_id"],
                  "return_definition": sector["return_definition"], "currency": sector["currency"],
                  "calendar_id": sector["calendar_id"], "timezone": sector["timezone"],
                  "asset_domain": sector["asset_domain"], "input_schema_id": INPUT_SCHEMA_ID,
                  "base": base, "phase": observed[phase["date"]], "terminal": observed[terminal["date"]],
                  "pricing_phase_definition": PHASE, "source_decision_date": source_day, "target_not_before": target,
                  "calendar_scope": "source_local_observation_dates_date_precision_not_fund_execution_clock"}
        source["source_hash"] = fingerprint(source)
        available = max((source["phase"]["available_at"], source["terminal"]["available_at"]), key=instant)
        version = {"label_available_at": available, "label_source": source,
                   "targets": asset_domains.target_coordinates(sector["asset_domain"]["return_target"], float(base["close"]),
                       float(source["phase"]["close"]), float(source["terminal"]["close"]))}
        if not versions or version != versions[-1]:
            versions.append(version)
    return versions


def build_samples(bundle, origins, horizon_days, order_time_local):
    require(type(horizon_days) is int and horizon_days > 0, "Industry horizon required")
    require(bundle.get("schema_version") == 4 and bundle.get("input_schema_id") == INPUT_SCHEMA_ID and isinstance(bundle.get("sectors"), list),
            "Source-bound industry dataset required")
    rows, gaps = [], []
    for sector in sorted(bundle["sectors"], key=lambda value: value["sector_id"]):
        asset_domains.validate_domain(sector["asset_domain"])
        require(sector.get("input_schema_id") == INPUT_SCHEMA_ID and sector["return_definition"] in ("price_return", "total_return", *asset_domains.TRANSFORMS)
                and sector["currency"] and sector["calendar_id"] and sector["timezone"], "Source asset identity, economic meaning and explicit clock required")
        prices = sector["prices"]
        dates = [row["date"] for row in prices]
        require(dates == sorted(set(dates)) and all(math.isfinite(float(row["close"]))
                and (sector["asset_domain"]["return_target"]["transform"] != "price_log_return" or float(row["close"]) > 0) and row.get("raw_ref") for row in prices),
                "Industry price chronology/domain/source invalid")
        for day in sorted(set(origins)):
            try:
                x, feature_source = _feature(sector, day, order_time_local)
            except (ValueError, KeyError) as exc:
                gaps.append({"sector_id": sector["sector_id"], "decision_date": day, "reason": str(exc)})
                continue
            versions = label_versions(sector, feature_source, day, horizon_days)
            label = versions[-1] if versions else {"label_available_at": None, "targets": None, "label_source": None}
            rows.append({"sector_id": sector["sector_id"], "decision_date": day,
                         "horizon_days": horizon_days, "x": x, "feature_source": feature_source,
                         **label, "label_versions": versions})
    return rows, gaps


def _eligible(samples, sector_id, day, policy, order_time_local, training_ceiling_at=None):
    lower = (_day(day)-dt.timedelta(days=policy["train_window_days"])).isoformat()
    boundary = instant(origin_at(day, order_time_local))
    if training_ceiling_at is not None:
        boundary = min(boundary, instant(training_ceiling_at))
    selected = []
    for row in samples:
        if row["sector_id"] != sector_id or not lower <= row["decision_date"] < day:
            continue
        versions = [version for version in row["label_versions"]
                    if instant(version["label_available_at"]) < boundary]
        if not versions:
            continue
        value = {key: item for key, item in row.items() if key != "label_versions"}
        value.update(max(versions, key=lambda version: instant(version["label_available_at"])))
        value["label_versions"] = versions
        selected.append(value)
    return sorted(selected, key=lambda row: row["decision_date"])


eligible_at = _eligible


METRIC_FEATURE_NAMES = ["observed_count", "actual_level_mean", "actual_missing_share",
    "absolute_change_mean", "absolute_change_missing_share", "absolute_surprise_mean", "absolute_surprise_missing_share",
    "relative_delta_mean", "relative_delta_missing_share", "relative_surprise_mean", "relative_surprise_missing_share",
    "action_up_count", "action_down_count", "action_unchanged_count", "action_introduced_count", "action_removed_count"]


class UnknownEconomicMetric(ValueError):
    def __init__(self, descriptors):
        self.descriptors = descriptors
        super().__init__("Source economic metric is outside the past-only fitted vocabulary")


def metric_descriptor(observation):
    current = observation["current"]
    concept = observation["source_concept"]
    return {"category": observation["category"], "metric_id": observation["metric_id"],
            "source_concept": concept["canonical_label"], "period_type": concept["period_type"],
            "canonical_unit": current["unit"] if current is not None else "source_policy_action",
            "measurement_type": current["measurement_type"] if current is not None else "source_action"}


def fit_feature_encoder(rows):
    descriptors = {fingerprint(metric_descriptor(item)): metric_descriptor(item) for row in rows
        for item in row["feature_source"]["economic_features"]["observations"]}
    keys = sorted(descriptors)
    vocabulary = [{"key": key, "descriptor": descriptors[key]} for key in keys]
    return {"kind": "past_source_metric_onehot_and_numeric_interactions_v2", "input_schema_id": INPUT_SCHEMA_ID, "base_feature_names": FEATURE_NAMES,
            "vocabulary": vocabulary, "vocabulary_hash": fingerprint(vocabulary),
            "feature_names": FEATURE_NAMES+["source_metric:"+key+":"+name for key in keys for name in METRIC_FEATURE_NAMES]}


def encode_features_for_model(model, row):
    encoder = model["feature_encoder"] if "feature_encoder" in model else model
    require(encoder.get("input_schema_id") == INPUT_SCHEMA_ID, "Obsolete source feature encoder; retraining required")
    require(encoder["base_feature_names"] == FEATURE_NAMES and len(row["x"]) == len(FEATURE_NAMES),
            "Source economic base-feature schema differs")
    observations = row["feature_source"]["economic_features"]["observations"]
    vocabulary = {item["key"] for item in encoder["vocabulary"]}
    unknown = {fingerprint(metric_descriptor(item)): metric_descriptor(item) for item in observations
               if fingerprint(metric_descriptor(item)) not in vocabulary}
    if unknown:
        raise UnknownEconomicMetric([unknown[key] for key in sorted(unknown)])
    vector = list(row["x"])
    for item in encoder["vocabulary"]:
        bucket = [observation for observation in observations if fingerprint(metric_descriptor(observation)) == item["key"]]
        vector.append(len(bucket))
        actual, changes, surprises, relative, relative_surprises = [], [], [], [], []
        for observation in bucket:
            current, prior, expectation = [observation[key] for key in ("current", "prior", "expectation")]
            if current is not None:
                value = float(current["value"])
                require(math.isfinite(value), "Nonfinite source economic actual")
                actual.append(value)
                if prior is not None:
                    changes.append(value-float(prior["value"]))
                if expectation is not None:
                    surprises.append(value-float(expectation["value"]))
            if observation["relative_delta"] is not None:
                relative.append(observation["relative_delta"])
            if observation["relative_surprise"] is not None:
                relative_surprises.append(observation["relative_surprise"])
        for values in (actual, changes, surprises, relative, relative_surprises):
            vector += [math.fsum(values)/len(values) if values else 0., 1-len(values)/len(bucket) if bucket else 1.]
        for action in ("up", "down", "unchanged", "introduced", "removed"):
            vector.append(sum(bool(observation["source_action"] and observation["source_action"]["value"] == action)
                              for observation in bucket))
    require(len(vector) == len(encoder["feature_names"]) and all(math.isfinite(float(value)) for value in vector),
            "Expanded source economic vector is invalid")
    return vector


def _fit(rows, alpha, ratio):
    encoder = fit_feature_encoder(rows)
    x, y = np.asarray([encode_features_for_model(encoder, row) for row in rows]), np.asarray([row["targets"] for row in rows])
    scaler = StandardScaler().fit(x)
    coefficients, intercept = np.zeros((2, x.shape[1])), y[0].copy()
    active = [column for column in range(2) if not np.all(y[:, column] == y[0, column])]
    if active:
        estimator = ElasticNet(alpha=alpha, l1_ratio=ratio, max_iter=20000, tol=1e-8)
        with warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            estimator.fit(scaler.transform(x), y[:, active])
        coefficients[active], intercept[active] = estimator.coef_, estimator.intercept_
    return {"scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
            "coefficients": coefficients.tolist(), "intercept": intercept.tolist(),
            "alpha": alpha, "l1_ratio": ratio, "feature_names": encoder["feature_names"],
            "feature_encoder": encoder, "source_metric_vocabulary_hash": encoder["vocabulary_hash"],
            "target_names": TARGET_NAMES, "input_schema_id": INPUT_SCHEMA_ID}


def prediction(model, row):
    require(model.get("input_schema_id") == INPUT_SCHEMA_ID, "Obsolete asset EN model; retraining required")
    vector = encode_features_for_model(model, row)
    return [float(intercept)+math.fsum(coefficient*(value-mean)/scale
            for coefficient, value, mean, scale in zip(coefficients, vector,
                                                        model["scaler_mean"], model["scaler_scale"]))
            for intercept, coefficients in zip(model["intercept"], model["coefficients"])]


def fit_at(samples, feature_row, policy, order_time_local, training_ceiling_at=None):
    sector, day = feature_row["sector_id"], feature_row["decision_date"]
    train = _eligible(samples, sector, day, policy, order_time_local, training_ceiling_at)
    audit = {"sector_id": sector, "decision_date": day, "origin_at": origin_at(day, order_time_local),
             "training_rows": len(train), "training_data_hash": fingerprint(train),
             "max_label_available_at": max((row["label_available_at"] for row in train), key=instant, default=None),
             "policy_hash": fingerprint(policy), "folds": [], "cv_trials": []}
    if training_ceiling_at is not None:
        audit["training_ceiling_at"] = training_ceiling_at
    minimum, count = policy["min_train_dates"], policy["cv_folds"]
    if len(train) < minimum+count:
        return {"status": "insufficient_evidence", "reason": "insufficient_industry_mature_training", "training_audit": audit}
    initial = max(minimum, int(len(train)*policy["cv_initial_train_fraction"]))
    cuts = [initial+(len(train)-initial)*index//count for index in range(count+1)]
    folds = []
    for start, stop in zip(cuts[:-1], cuts[1:]):
        if start >= stop:
            return {"status": "insufficient_evidence", "reason": "empty_industry_cv_fold", "training_audit": audit}
        validation = train[start:stop]
        mature = _eligible(train, sector, validation[0]["decision_date"], policy, order_time_local)
        if len(mature) < minimum:
            return {"status": "insufficient_evidence", "reason": "insufficient_industry_purged_cv", "training_audit": audit}
        folds.append((mature, validation))
        audit["folds"].append({"validation_start": validation[0]["decision_date"], "validation_end": validation[-1]["decision_date"],
                               "training_data_hash": fingerprint(mature), "training_rows": len(mature),
                               "training_max_label_available_at": max((row["label_available_at"] for row in mature), key=instant)})
    try:
        for alpha in sorted(set(policy["alpha_grid"])):
            for ratio in sorted(set(policy["l1_ratio_grid"])):
                errors, models = [], []
                for mature, validation in folds:
                    model = _fit(mature, alpha, ratio)
                    models.append(model)
                    errors.append(math.fsum(math.fsum((actual-guess)**2 for actual, guess in
                        zip(row["targets"], prediction(model, row)))/2 for row in validation)/len(validation))
                audit["cv_trials"].append({"alpha": alpha, "l1_ratio": ratio, "fold_mse": errors,
                                            "mean_mse": math.fsum(errors)/len(errors), "fold_models": models})
        selected = min(audit["cv_trials"], key=lambda trial: (trial["mean_mse"], trial["alpha"], trial["l1_ratio"]))
        model = _fit(train, selected["alpha"], selected["l1_ratio"])
        coordinates = prediction(model, feature_row)
    except UnknownEconomicMetric as error:
        return {"status": "insufficient_evidence", "reason": "unseen_source_economic_metric",
                "training_audit": audit, "required_actions": [{"action": "accumulate_source_verified_metric_history_and_mature_labels",
                    "descriptor": descriptor, "sector_id": sector} for descriptor in error.descriptors]}
    except ConvergenceWarning:
        return {"status": "insufficient_evidence", "reason": "industry_numerical_convergence_failure", "training_audit": audit}
    output = {"status": "fitted", "input_schema_id": INPUT_SCHEMA_ID, "sector_id": sector, "decision_date": day, "horizon_days": feature_row["horizon_days"],
              "origin_at": origin_at(day, order_time_local), "feature_source": feature_row["feature_source"],
              "model": model, "training_audit": audit, "predicted_coordinates": coordinates,
              "pricing_phase_definition": PHASE, "asset_domain": feature_row["feature_source"]["asset_domain"],
              "provenance": "forward_fit_with_strictly_earlier_mature_benchmark_labels"}
    output["source_hash"] = fingerprint(output)
    return output


def freeze_current(industry, training_ceiling_at, required_sector_ids=None):
    import copy
    output = copy.deepcopy(industry)
    day = output["decision_date"]
    required = set(required_sector_ids if required_sector_ids is not None else output["ready_sector_ids"])
    current = [row for row in output["samples"] if row["decision_date"] == day and row["sector_id"] in required]
    require({row["sector_id"] for row in current} == required, "Required industry has no current source-qualified sample")
    frozen = [fit_at(output["samples"], row, output["policy"], output["order_time_local"], training_ceiling_at) for row in current]
    require(all(row["status"] == "fitted" for row in frozen), "Insufficient industry training before calibration ceiling")
    output["forecasts"] = [row for row in output["forecasts"]
                           if row["decision_date"] != day or row["sector_id"] not in required] + frozen
    output["training_ceiling_at"] = training_ceiling_at
    output["forecast_hash"] = fingerprint({key: value for key, value in output.items() if key != "forecast_hash"})
    return output


def build_forward(bundle, origins, policy, horizon_days, order_time_local):
    samples, gaps = build_samples(bundle, origins, horizon_days, order_time_local)
    forecasts, unavailable = [], list(gaps)
    for sample in samples:
        fitted = fit_at(samples, sample, policy, order_time_local)
        if fitted["status"] == "fitted":
            forecasts.append(fitted)
        else:
            unavailable.append({"sector_id": sample["sector_id"], "decision_date": sample["decision_date"],
                                "reason": fitted["reason"], "training_audit": fitted["training_audit"]})
    latest = max(origins) if origins else None
    ids = sorted(sector["sector_id"] for sector in bundle["sectors"])
    readiness = {sector: {"status": "ready" if any(row["sector_id"] == sector and row["decision_date"] == latest
                                                   for row in forecasts) else "insufficient_evidence",
                           "required_actions": []} for sector in ids}
    for sector, record in readiness.items():
        if record["status"] != "ready":
            record["required_actions"] = [{"action": "supply_archived_PIT_economic_news_and_benchmark_observations",
                                          "sector_id": sector,
                                          "reasons": [row["reason"] for row in unavailable
                                                      if row["sector_id"] == sector and row["decision_date"] == latest]}]
    ready_ids = [sector for sector in ids if readiness[sector]["status"] == "ready"]
    ready = bool(ids) and len(ready_ids) == len(ids)
    output = {"schema_version": 4, "input_schema_id": INPUT_SCHEMA_ID, "status": "research_ready" if ready else "insufficient_evidence",
              "trade_ready": ready, "decision_date": latest, "horizon_days": horizon_days,
              "dataset_hash": fingerprint(bundle), "news_ref": bundle["news_ref"],
              "event_frontier_hash": bundle["news_state"]["event_frontier_hash"],
              "sector_ids": ids, "ready_sector_ids": ready_ids, "sector_readiness": readiness,
              "policy": policy, "order_time_local": order_time_local,
              "samples": samples, "forecasts": forecasts, "unavailable": unavailable,
              "required_actions": [action for record in readiness.values() for action in record["required_actions"]],
              "return_role": "fund_covariate_only_never_added_to_fund_cash_or_joint_errors"}
    if not ready:
        output["reason"] = "insufficient_forward_industry_evidence"
    output["forecast_hash"] = fingerprint(output)
    return output
