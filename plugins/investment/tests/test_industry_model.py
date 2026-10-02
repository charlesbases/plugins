"""Causal benchmark/news numerical contracts using declared synthetic archives."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest
import math

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import industry_model as model
import asset_domains
import allocation_market as market
import numeric_validation as validation
import allocation_statistics as statistics
from contracts import EvidenceError, fingerprint, instant


def archive(constant=False):
    first = dt.date(2028, 1, 1)
    prices, events, economics = [], [], []
    for index in range(80):
        day = (first+dt.timedelta(days=index)).isoformat()
        at = day+"T09:00:00+08:00"
        prices.append({"date": day, "close": 100 if constant else 100+index*.1+((index//7)%2)*.8,
                       "available_at": at, "availability_basis": "synthetic_archived_capture",
                       "raw_ref": {"synthetic_source_id": "price-"+day}})
        if index % 7 == 0:
            events.append({"event_id": "event-"+day, "revision_id": "revision-"+day,
                           "source_revision_id": "synthetic-source-revision-"+day,
                           "known_at": at, "first_seen_at": at, "published_at": at,
                           "available_at": at, "effective_at": at,
                           "event_type": "source_fact_presence", "evidence_refs": [{"synthetic_quote_id": day}]})
            economics.append({"observation_id": "synthetic-economic-"+day,
                "observation_key": "synthetic-economic-key-"+day, "source_revision_id": "synthetic-source-revision-"+day,
                "event_id": "event-"+day, "revision_id": "revision-"+day, "sector_ids": ["industry-a"],
                "known_at": at, "assessed_at": at, "event_at": at, "category": "inflation", "metric_id": "synthetic-cpi",
                "metric_label": "Synthetic CPI", "source_concept": {"canonical_label": "synthetic cpi", "period_type": "month", "unit": "percent", "alias_evidence": []}, "relative_delta": .01 if (index//7)%2 else -.01,
                "relative_surprise": None, "source_action": None,
                "current": {"value": "2.02" if (index//7)%2 else "1.98", "unit": "percent",
                            "measurement_type": "level", "synthetic_quote_id": day},
                "prior": {"value": "2.0", "unit": "percent", "measurement_type": "level"},
                "expectation": None, "scope": "explicit_synthetic_numerical_archive"})
    return {"schema_version": 4, "input_schema_id": asset_domains.INPUT_SCHEMA_ID, "news_ref": {"synthetic_review": "source-archive"},
            "news_state": {"event_frontier_hash": "synthetic-frontier"},
            "sectors": [{"sector_id": "industry-a", "benchmark_id": "synthetic-benchmark-a",
                         "return_definition": "price_return", "currency": "CNY",
                         "calendar_id": "synthetic-daily-observations", "timezone": "Asia/Shanghai",
                         "input_schema_id": asset_domains.INPUT_SCHEMA_ID,
                         "asset_domain": {"kind": "equity", "role": "single_sector", "return_target": {
                             "transform": "price_log_return", "economic_meaning": "Synthetic Price Index", "source_unit": "index_point",
                             "canonical_unit": "index_point", "locator": "synthetic/definition", "quote": "Synthetic Price Index"}},
                         "prices": prices, "features": events, "economic_observations": economics,
                         "coverage": {"absence_proven": False}}]}


def policy():
    return {"train_window_days": 200, "min_train_dates": 4, "cv_folds": 2,
            "cv_initial_train_fraction": .6, "alpha_grid": [.01], "l1_ratio_grid": [.5],
            "min_joint_dates": 2, "feature_names": model.FEATURE_NAMES}


def origins(bundle):
    return [row["date"] for row in bundle["sectors"][0]["prices"][:65]]


class IndustryModelTests(unittest.TestCase):
    def test_future_price_and_news_cannot_change_prior_forecast_or_training_hash(self):
        data = archive()
        samples, _ = model.build_samples(data, origins(data), 3, "12:00:00")
        before = model.fit_at(samples, samples[50], policy(), "12:00:00")
        changed = copy.deepcopy(data)
        for row in changed["sectors"][0]["prices"][55:]:
            row["close"] *= 20
        changed["sectors"][0]["features"][-1]["event_id"] = "future-poison"
        after_samples, _ = model.build_samples(changed, origins(changed), 3, "12:00:00")
        after = model.fit_at(after_samples, after_samples[50], policy(), "12:00:00")
        self.assertEqual(before["status"], "fitted")
        self.assertEqual(before, after)

    def test_same_origin_labels_never_enter_industry_fit(self):
        data = archive()
        samples, _ = model.build_samples(data, origins(data), 3, "12:00:00")
        before = model.fit_at(samples, samples[50], policy(), "12:00:00")
        poisoned = copy.deepcopy(samples)
        poisoned[50]["targets"] = [99., -99.]
        after = model.fit_at(poisoned, poisoned[50], policy(), "12:00:00")
        self.assertEqual(before, after)
        self.assertLess(before["training_audit"]["max_label_available_at"], before["origin_at"])

    def test_news_facts_enter_feature_vector_without_forced_return(self):
        data = archive(constant=True)
        samples, _ = model.build_samples(data, origins(data), 3, "12:00:00")
        self.assertEqual(samples[7]["x"][model.FEATURE_NAMES.index("observed_source_facts_7d")], 1)
        self.assertEqual(samples[7]["x"][model.FEATURE_NAMES.index("observed_source_facts_30d")], 2)
        self.assertGreater(samples[7]["x"][model.FEATURE_NAMES.index("effective_source_facts_30d")], 0)
        fitted = model.fit_at(samples, samples[50], policy(), "12:00:00")
        self.assertEqual(fitted["predicted_coordinates"], [0., 0.])
        self.assertTrue(all(value == 0 for row in fitted["model"]["coefficients"] for value in row))

    def test_newly_captured_history_and_no_archived_scope_are_partial(self):
        data = archive()
        for row in data["sectors"][0]["prices"]:
            row["available_at"] = "2028-03-30T12:00:00+08:00"
        output = model.build_forward(data, origins(data), policy(), 3, "12:00:00")
        self.assertEqual(output["status"], "insufficient_evidence")
        self.assertFalse(output["trade_ready"])
        self.assertEqual(output["samples"], [])

    def test_independent_validator_rejects_source_label_tampering(self):
        data = archive()
        dates = origins(data)
        output = model.build_forward(data, dates, policy(), 3, "12:00:00")
        inputs = {"data": data, "origins": dates, "policy": policy(), "clock": {"order_time_local": "12:00:00"},
                  "context": {"model_request": {"horizon_days": 3}}}
        self.assertEqual(validation.validate_industry(output, inputs)["status"], "passed")
        changed = copy.deepcopy(output)
        changed["samples"][20]["targets"][1] += .1
        with self.assertRaises(EvidenceError):
            validation.validate_industry(changed, inputs)

    def test_bridge_preserves_cash_targets_and_never_backdates_disclosure(self):
        data = archive(constant=True)
        industry = model.build_forward(data, origins(data), policy(), 3, "12:00:00")
        day = origins(data)[50]
        samples = [{"code": "synthetic-fund", "decision_date": day, "horizon_days": 3,
                    "x": [0.]*6, "targets": {"hold": 1.2}, "latent_targets": {"cash": .2}}]
        exposure = {"synthetic-fund": [{"sector_id": "industry-a", "weight": ".7", "as_of_date": day,
                     "known_at": day+"T11:00:00+08:00", "basis": "historical_disclosure", "role": "single_sector", "source_label": "industry-a",
                     "evidence_refs": [{"synthetic_disclosure": "holdings"}]}]}
        attached = market.attach_industry_features(samples, industry, exposure, "12:00:00")
        self.assertTrue(attached[0]["industry_ready"])
        self.assertEqual(attached[0]["targets"], samples[0]["targets"])
        self.assertEqual(attached[0]["x"][-len(model.FUND_FEATURE_NAMES):-len(model.FUND_FEATURE_NAMES)+2], [0., 0.])
        self.assertEqual(attached[0]["x"][-3], 1.)
        self.assertAlmostEqual(attached[0]["industry_source"]["disclosed_reference_unmapped_weight"], .3)
        inputs = {"samples": samples, "industry": industry, "exposures": exposure,
                  "clock": {"order_time_local": "12:00:00"}}
        self.assertEqual(validation.validate_industry_bridge(attached, inputs)["status"], "passed")
        exposure["synthetic-fund"][0]["known_at"] = day+"T13:00:00+08:00"
        self.assertFalse(market.attach_industry_features(samples, industry, exposure, "12:00:00")[0]["industry_ready"])

    def test_current_industry_model_excludes_calibration_labels(self):
        data = archive()
        dates = origins(data)
        industry = model.build_forward(data, dates, policy(), 3, "12:00:00")
        ceiling = dates[40]+"T12:00:00+08:00"
        frozen = model.freeze_current(industry, ceiling)
        changed = copy.deepcopy(industry)
        for row in changed["samples"]:
            if row["targets"] is not None and row["label_available_at"] >= ceiling:
                row["targets"] = [9., -9.]
        after = model.freeze_current(changed, ceiling)
        before_row = next(row for row in frozen["forecasts"] if row["decision_date"] == dates[-1])
        after_row = next(row for row in after["forecasts"] if row["decision_date"] == dates[-1])
        self.assertEqual(before_row, after_row)
        self.assertLess(before_row["training_audit"]["max_label_available_at"], ceiling)
        inputs = {"data": data, "origins": dates, "policy": policy(), "clock": {"order_time_local": "12:00:00"},
                  "context": {"model_request": {"horizon_days": 3}}}
        self.assertEqual(validation.validate_industry(frozen, inputs)["status"], "passed")

    def test_fund_joint_scenarios_use_one_whole_origin_error_and_embargo_panels(self):
        data = archive(constant=True)
        data["sectors"][0]["sector_id"] = "黄金"
        for observation in data["sectors"][0]["economic_observations"]:
            observation["sector_ids"] = ["黄金"]
        dates = origins(data)
        industry = model.build_forward(data, dates, policy(), 3, "12:00:00")
        nav, features, info = {}, {}, {}
        for code, slope in (("fund-a", .001), ("fund-b", .002)):
            nav[code] = [{"code": code, "date": row["date"], "nav": 1+index*slope,
                          "distribution_per_share": 0} for index, row in enumerate(data["sectors"][0]["prices"])]
            features[code] = [{"code": code, "feature_cutoff_nav_date": row["date"], "known_max_nav_date": row["date"],
                               "values": dict.fromkeys(market.NAV_FEATURE_NAMES, 0.)} for row in nav[code]]
            info[code] = {"fund_group_id": code}
        clock = {"order_time_local": "12:00:00", "assets": {code: {"after_cutoff": False,
                 "ownership_start": "execution_date", "holding_start": "execution_date",
                 "confirmation_rule": {"lag_days": 0, "day_basis": "calendar_days"}} for code in nav}}
        raw = market.build_single_step_samples(nav, features, info, 3, 0, dates[-1], clock)
        exposures = {code: [{"sector_id": "黄金", "weight": ".8", "as_of_date": dates[0],
                      "basis": "historical_disclosure", "role": "single_sector", "source_label": "黄金",
                      "actual_exposure_status": "historical_disclosed_not_current", "actual_weight": ".8", "stale": True,
                      "known_at": dates[0]+"T09:00:00+08:00", "evidence_refs": [{"synthetic_disclosure": code}]}] for code in nav}
        attached = market.attach_industry_features(raw, industry, exposures, "12:00:00")
        current = [row for row in attached if row["decision_date"] == dates[-1]]
        self.assertTrue(all(row["industry_source"]["actual_current_unknown_weight"] == 1. for row in current))
        fund_policy = {**policy(), "feature_names": market.FEATURE_NAMES+model.FUND_FEATURE_NAMES}
        fitted = statistics.fit_joint_targets(attached, current, fund_policy, clock)
        self.assertEqual(fitted["status"], "research_ready")
        # Independently select only the label known at this origin. The
        # complete revision menu is provenance, not a fitted input record.
        boundary = instant(dates[-1]+"T00:00:00+08:00")
        expected_rows = []
        for original in attached:
            if not original["industry_ready"] or original["decision_date"] >= dates[-1]:
                continue
            mature = [value for value in original.get("label_versions", []) if instant(value["label_available_at"]) < boundary]
            if not mature:
                continue
            chosen = {key: value for key, value in original.items() if key != "label_versions"}
            chosen.update(max(mature, key=lambda value: instant(value["label_available_at"])))
            if chosen["targets"] is not None and chosen["label_available_date"] < dates[-1]:
                expected_rows.append(chosen)
        expected_rows.sort(key=lambda row: (row["decision_date"], row["code"]))
        self.assertEqual(fitted["training_audit"]["training_data_hash"], fingerprint(expected_rows))
        validation._training_audit(fitted["models"][0], fitted["training_audit"], attached, list(nav), dates[-1], 3, fund_policy)
        joint = fitted["joint_scenarios"]
        selection, calibration = joint["source_selection_oos"], joint["source_calibration_oos"]
        self.assertLess(max(row["label_available"] for row in selection), min(row["date"] for row in calibration))
        self.assertEqual(joint["dates"], [row["date"] for row in selection])
        for scenario, record in zip(joint["targets"]["hold"], selection):
            for column, forecast in enumerate(fitted["forecasts"]):
                coordinates = {name: forecast["predicted_latents"][name]+record["latent_errors"][name][column]
                               for name in market.LATENT_NAMES}
                expected = math.exp(coordinates["log_terminal_nav"])+math.fsum(coordinates[name]**2 for name in market.LATENT_NAMES[2:])
                self.assertAlmostEqual(scenario[column], expected)
                self.assertEqual(record["industry_sources"][forecast["code"]]["decision_date"], record["date"])
        ceiling = calibration[0]["origin_at"]
        frozen = statistics.fit_joint_targets(attached, current, fund_policy, clock, ceiling)
        self.assertEqual(frozen["status"], "research_ready")
        self.assertLess(frozen["training_audit"]["max_label_available_date"], ceiling[:10])

    def test_sector_relation_known_later_cannot_relabel_earlier_benchmark_history(self):
        data = archive(constant=True)
        dates = origins(data)
        data["sectors"][0]["sector_relation"] = {"status": "verified", "known_at": dates[40]+"T09:00:00+08:00",
            "scope": "synthetic_original_relation_available_only_at_this_time"}
        industry = model.build_forward(data, dates, policy(), 3, "12:00:00")
        self.assertEqual(industry["samples"][0]["decision_date"], dates[40])
        self.assertTrue(all(row["decision_date"] >= dates[40] for row in industry["samples"]))
        inputs = {"data": data, "origins": dates, "policy": policy(), "clock": {"order_time_local": "12:00:00"},
                  "context": {"model_request": {"horizon_days": 3}}}
        self.assertEqual(validation.validate_industry(industry, inputs)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
