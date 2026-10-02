"""Engineering contracts, explicitly synthetic; no investment profitability claim."""
import copy
import unittest
from unittest.mock import patch
import asset_domains
import news_economics
import industry_model
import industry_validation
import numeric_validation
import allocation_market
import asset_exposure
import portfolio_paths
from contracts import fingerprint
from test_industry_model import archive, origins


class AssetDomainV2Tests(unittest.TestCase):
    def test_a_second_unencoded_event_is_not_certified_by_old_measured_news(self):
        data = archive()
        sector = data["sectors"][0]
        day = origins(data)[20]
        event = copy.deepcopy(sector["features"][0])
        event.update(event_id="new-policy-without-encoding", source_revision_id="new-policy-revision",
                     available_at=day+"T09:00:00+08:00", known_at=day+"T09:00:00+08:00", first_seen_at=day+"T09:00:00+08:00")
        sector["features"].append(event)
        state = news_economics.factual_event_features(sector["economic_observations"], "industry-a",
                    day+"T12:00:00+08:00", events=sector["features"][:1]+[event])
        self.assertEqual([row["status"] for row in state["event_entity_states"]], ["measured", "unsupported"])
        samples, gaps = industry_model.build_samples(data, [day], 3, "12:00:00")
        self.assertEqual(samples, [])
        self.assertIn("own verified economic encoding", gaps[0]["reason"])

    def test_negative_rate_and_zero_level_use_changes_not_log_price_returns(self):
        target = {"transform": "level_change"}
        self.assertEqual(asset_domains.target_coordinates(target, -.001, 0., .002), [.001, .003])
        with self.assertRaises(ValueError):
            asset_domains.target_coordinates({"transform": "price_log_return"}, -.001, 0., .002)

    def test_foreign_asset_targets_have_explicit_source_currency_and_clock(self):
        data = archive()
        sector = data["sectors"][0]
        sector.update(currency="USD", calendar_id="source_US_observation_sessions", timezone="America/New_York")
        sector["asset_domain"].update(kind="foreign", role="market")
        for quote in sector["prices"]:
            quote["available_at"] = quote["date"]+"T17:00:00-05:00"
        samples, gaps = industry_model.build_samples(data, [origins(data)[20]], 3, "12:00:00")
        self.assertEqual(gaps, [])
        self.assertEqual(samples[0]["label_source"]["currency"], "USD")
        self.assertEqual(samples[0]["label_source"]["calendar_id"], "source_US_observation_sessions")
        self.assertEqual(samples[0]["label_source"]["source_decision_date"], "2028-01-20")
        self.assertEqual(samples[0]["label_source"]["phase"]["date"], "2028-01-21")

    def test_overlapping_theme_and_sector_preserve_one_capital_weight_and_NAV(self):
        day = "2028-02-01"
        forecasts = [{"decision_date": day, "sector_id": entity, "horizon_days": 3,
            "input_schema_id": asset_domains.INPUT_SCHEMA_ID, "predicted_coordinates": [.1, .2], "source_hash": entity}
            for entity in ("sector-a", "theme-a")]
        industry = {"input_schema_id": asset_domains.INPUT_SCHEMA_ID, "forecasts": forecasts}
        sample = {"code": "synthetic-fund", "decision_date": day, "horizon_days": 3,
                  "x": [0.]*6, "targets": {"hold": 1.123}, "latent_targets": {"log_terminal_nav": .2}}
        exposure = [{"sector_id": entity, "role": role, "source_label": "same original industrial holdings",
            "weight": ".7", "basis": "historical_disclosure", "as_of_date": day,
            "known_at": day+"T09:00:00+08:00", "evidence_refs": [{"synthetic_source": "holding-table"}]}
            for entity, role in (("sector-a", "single_sector"), ("theme-a", "theme"))]
        row = allocation_market.attach_industry_features([sample], industry, {"synthetic-fund": exposure}, "12:00:00")[0]
        self.assertTrue(row["industry_ready"])
        self.assertEqual(row["targets"], sample["targets"])
        self.assertEqual(row["latent_targets"], sample["latent_targets"])
        self.assertEqual(row["industry_source"]["actual_current_unknown_weight"], 1.)
        self.assertAlmostEqual(row["industry_source"]["disclosed_reference_unmapped_weight"], .3)
        self.assertEqual(row["industry_source"]["role_values"]["market"], [0., 0., 0.])
        self.assertAlmostEqual(row["industry_source"]["role_values"]["theme"][0], .07)
        self.assertAlmostEqual(row["industry_source"]["role_values"]["theme"][1], .14)
        self.assertEqual(row["industry_source"]["role_values"]["theme"][2], 1.)
        predicted = {item["sector_id"]: item for item in forecasts}
        self.assertEqual(industry_validation.validate_bridge_witness(row["industry_source"], exposure, predicted)["status"], "passed")
        self.assertEqual(numeric_validation.validate_industry_bridge([row], {
            "samples": [sample], "industry": industry, "exposures": {"synthetic-fund": exposure},
            "clock": {"order_time_local": "12:00:00"}})["status"], "passed")
        altered = copy.deepcopy(row["industry_source"])
        altered["values"][-3] = 0.
        with self.assertRaises(ValueError):
            industry_validation.validate_bridge_witness(altered, exposure, predicted)

    def test_learned_signed_beta_keeps_unknown_actual_holdings(self):
        day = "2028-02-01"
        industry = {"input_schema_id": asset_domains.INPUT_SCHEMA_ID, "forecasts": [{"decision_date": day,
            "sector_id": "FX", "horizon_days": 3, "input_schema_id": asset_domains.INPUT_SCHEMA_ID,
            "predicted_coordinates": [.02, .03], "source_hash": "synthetic-fx"}]}
        exposure = {"synthetic-fund": [{"sector_id": "FX", "role": "fx", "coefficient": -2., "basis": "model_estimate",
            "as_of_date": day, "known_at": day+"T09:00:00+08:00", "evidence_refs": [{"synthetic_source": "regression"}]}]}
        sample = {"code": "synthetic-fund", "decision_date": day, "horizon_days": 3, "x": [0.]*6, "targets": {"hold": 1.02}}
        row = allocation_market.attach_industry_features([sample], industry, exposure, "12:00:00")[0]
        self.assertTrue(row["industry_ready"])
        self.assertEqual(row["industry_source"]["role_values"]["fx"], [-.04, -.06, 1.])
        self.assertEqual(row["industry_source"]["actual_current_unknown_weight"], 1.)
        self.assertEqual(row["x"][-1], 1.)
        self.assertEqual(row["targets"], sample["targets"])
        self.assertEqual(numeric_validation.validate_industry_bridge([row], {
            "samples": [sample], "industry": industry, "exposures": exposure,
            "clock": {"order_time_local": "12:00:00"}})["status"], "passed")

    def test_old_serialized_asset_model_requires_retraining(self):
        with self.assertRaisesRegex(ValueError, "retraining required"):
            industry_model.prediction({"input_schema_id": "source-news-industry-en-v1"}, {})

    def test_path_EN_model_names_match_full_native_and_registered_subset_inputs(self):
        full_names = allocation_market.FEATURE_NAMES+industry_model.FUND_FEATURE_NAMES
        for names in (full_names, allocation_market.FEATURE_NAMES, [full_names[0], full_names[6]]):
            rows = [{"x": [0.]*len(names), "fund_group_id": "synthetic-legal-fund", "decision_date": "2028-01-01",
                     "wealth_ratios": [1., 1.], "cash": [], "base_nav": 1.}]
            model = portfolio_paths._fit_model(rows, [], ["synthetic-legal-fund"], .01, .5, feature_names=names)
            self.assertEqual(model["feature_names"], names+["source_legal_fund_group:synthetic-legal-fund"])
            self.assertEqual(len(model["scaler_mean"]), len(model["feature_names"]))
            self.assertEqual(model["input_schema_id"], asset_domains.INPUT_SCHEMA_ID)
            self.assertEqual(portfolio_paths._predict(model, rows[0]), [0., 0.])
        with self.assertRaisesRegex(ValueError, "obsolete"):
            portfolio_paths._fit_model([{**rows[0], "x": [0.]*9}], [], ["synthetic-legal-fund"], .01, .5)

    def test_unknown_future_source_concept_requires_new_training_history(self):
        data = archive()
        samples, _ = industry_model.build_samples(data, [origins(data)[20]], 3, "12:00:00")
        encoder = industry_model.fit_feature_encoder(samples)
        unseen = copy.deepcopy(samples[0])
        unseen["feature_source"]["economic_features"]["observations"][0]["source_concept"]["canonical_label"] = "new unseen actual source concept"
        with self.assertRaises(industry_model.UnknownEconomicMetric):
            industry_model.encode_features_for_model(encoder, unseen)

    def test_style_estimate_rebuilds_original_sources_and_independent_EN_math(self):
        data = archive()
        sector = data["sectors"][0]
        sector["sector_relation"] = {"status": "verified", "known_at": "2027-12-31T09:00:00+08:00"}
        nav = []
        for index, quote in enumerate(sector["prices"]):
            value = (quote["close"]/100.)**2
            row = {"code": "synthetic-fund", "date": quote["date"], "nav": value, "cumulative_nav": value,
                   "distribution_per_share": 0.}
            known = quote["date"]+"T10:00:00+08:00"
            digest = fingerprint(row)
            version = {**row, "available_at": known, "observed_at": known, "version_id": digest,
                "raw_ref": {"source_id": "synthetic_engineering_original", "sha256": digest},
                "availability_evidence": {"kind": "archived_original_capture", "source_id": "synthetic_engineering_original",
                    "raw_sha256": digest, "captured_at": known}}
            nav.append({**row, "source_versions": [version]})
        policy = {"train_window_days": 200, "min_train_dates": 20, "cv_initial_train_fraction": .6,
                  "alpha_grid": [.000001], "l1_ratio_grid": [.5]}
        estimates = asset_exposure.estimate("synthetic-fund", nav, [origins(data)[60]], [sector], policy, "12:00:00")
        self.assertEqual(len(estimates), 1)
        self.assertIsNone(estimates[0]["actual_weight"])
        result = asset_exposure.validate_estimates({"synthetic-fund": estimates}, {"synthetic-fund": nav}, [sector])
        self.assertEqual(result["checked_model_count"], 1)
        altered = copy.deepcopy(estimates)
        altered[0]["coefficient"] += 1.
        with self.assertRaises(ValueError):
            asset_exposure.validate_estimates({"synthetic-fund": altered}, {"synthetic-fund": nav}, [sector])
        late = copy.deepcopy(sector)
        for quote in late["prices"]:
            quote["available_at"] = "2028-04-01T12:00:00+08:00"
        self.assertEqual(asset_exposure.estimate("synthetic-fund", nav, [origins(data)[60]], [late], policy, "12:00:00"), [])

    def test_original_series_percent_parser_keeps_real_capture_availability(self):
        contract = {"series_id": "YIELD", "quote_source_id": "synthetic-registered-transport", "quote_url": "https://example.invalid/series",
            "series_columns": {"date": "DATE", "value": "YIELD"}, "timezone": "America/New_York",
            "start": "2020-01-01", "end": "2020-01-04", "return_definition": "level_change",
            "asset_domain": {"return_target": {"source_unit": "percent", "canonical_unit": "decimal_rate", "transform": "level_change"}}}
        capture = {"registry_source_id": contract["quote_source_id"], "requested_url": contract["quote_url"],
            "retrieved_at": "2026-10-07T12:00:00Z", "text": "DATE,YIELD\n2020-01-01,-0.1\n2020-01-02,.\n2020-01-03,0\n"}
        with patch.object(asset_domains.source_fetch, "validate_capture_provenance"):
            rows = asset_domains.parse_series_capture(capture, contract)
        self.assertEqual([row["close"] for row in rows], [-.001, 0.])
        self.assertEqual({row["available_at"] for row in rows}, {capture["retrieved_at"]})
        self.assertTrue(all(row["market_public_available_at"] is None for row in rows))
