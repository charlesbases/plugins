"""Closed EN/source arithmetic regressions, never historical investment evidence."""
import copy
import datetime as dt
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import allocation_market as market
import allocation_statistics as statistics
import news_validation
from industry_model import FUND_FEATURE_NAMES
import verify
from contracts import fingerprint


def sample(index):
    day = dt.date(2020, 1, 1)+dt.timedelta(days=index)
    value = .08*math.sin(index/3)
    latents = dict.fromkeys(market.LATENT_NAMES, 0.)
    latents.update(log_pricing=value/2, log_terminal_nav=value)
    available = (day+dt.timedelta(days=2)).isoformat()
    source = {"code": "C", "end_date": (day+dt.timedelta(days=1)).isoformat(),
              "strict_PIT_verified": True, "label_available_at": available+"T09:00:00+08:00",
              "synthetic_engineering_source": True}
    source["source_hash"] = fingerprint(source)
    return {"code": "C", "fund_group_id": "C", "decision_date": day.isoformat(), "horizon_days": 1,
            "feature_cutoff_date": (day-dt.timedelta(days=1)).isoformat(),
            "x": [0.]*len(market.FEATURE_NAMES)+[value/2, value]+[0.]*(len(FUND_FEATURE_NAMES)-2), "industry_ready": True,
            "industry_source": {"synthetic_nonzero_covariate": value},
            "feature_source": {"strict_PIT_verified": True}, "label_source": source,
            "label_available_date": available, "label_available_at": source["label_available_at"],
            "latent_targets": latents, "targets": market.decode_latents(latents)}


def fixture():
    from industry_model import FUND_FEATURE_NAMES
    samples = [sample(index) for index in range(42)]
    decision = (dt.date(2020, 1, 1)+dt.timedelta(days=42)).isoformat()
    policy = {"train_window_days": 300, "min_train_dates": 4, "cv_folds": 2, "min_joint_dates": 8,
              "cv_initial_train_fraction": .6, "alpha_grid": [.001], "l1_ratio_grid": [.5],
              "feature_names": market.FEATURE_NAMES+FUND_FEATURE_NAMES}
    current = sample(42)
    rows = [{"code": "C", "decision_date": decision, "horizon_days": 1, "x": current["x"]}]
    fitting = statistics.fit_joint_targets(samples, rows, policy, {"order_time_local": "20:00:00"})
    context = {"allocation_codes": ["C"], "as_of": decision, "decision_at": decision+"T20:00:00+08:00", "fee_contracts": {}}
    return context, samples, fitting, policy


class PredictiveIncrementContracts(unittest.TestCase):
    def test_missing_original_panel_reports_insufficient_without_shadow_orders(self):
        value = news_validation.predictive_increment({}, [], {"status": "partial"}, {})
        self.assertEqual(value["status"], "insufficient_evidence")
        self.assertFalse(value["shadow_orders_created"])
        self.assertFalse(value["used_for_order_selection"])
        self.assertEqual(value["paired_rows"], [])
        self.assertTrue(value["required_actions"])

    def test_same_EN_mature_panel_has_independent_native_mean_loss_truth(self):
        context, samples, fitting, policy = fixture()
        self.assertEqual(fitting["status"], "research_ready")
        value = news_validation.predictive_increment(context, samples, fitting, policy)
        self.assertEqual(value["status"], "descriptive_ready", value.get("reason"))
        self.assertEqual(value["calibration_dates"], [row["date"] for row in fitting["joint_scenarios"]["source_calibration_oos"]])
        for paired in value["paired_rows"]:
            train = [row for row in samples if row["decision_date"] < paired["date"] and row["label_available_date"] < paired["date"]]
            mean = [math.fsum(row["latent_targets"][name] for row in train)/len(train) for name in market.LATENT_NAMES]
            actual = next(row for row in samples if row["decision_date"] == paired["date"])
            expected = math.fsum((actual["latent_targets"][name]-centre)**2 for name, centre in zip(market.LATENT_NAMES, mean))/len(mean)
            self.assertAlmostEqual(paired["native_loss"], expected, places=12)
            self.assertAlmostEqual(paired["loss_difference_native_minus_full"], paired["native_loss"]-paired["full_loss"], places=12)
        self.assertFalse(value["shadow_orders_created"])
        self.assertFalse(value["fees_used_in_forecast_loss"])
        self.assertEqual(value["currency_increment"]["status"], "insufficient_evidence")
        self.assertFalse(value["currency_increment"]["profitability_claim"])

    def test_unknown_source_history_and_future_rows_cannot_manufacture_increment(self):
        context, samples, fitting, policy = fixture()
        original = news_validation.predictive_increment(context, samples, fitting, policy)
        future = news_validation.predictive_increment(context, samples+[sample(60)], fitting, policy)
        self.assertEqual(future, original)
        unknown = copy.deepcopy(samples)
        unknown[0]["feature_source"]["strict_PIT_verified"] = False
        result = news_validation.predictive_increment(context, unknown, fitting, policy)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["paired_rows"], [])

    def test_validator_rejects_changed_paired_delta(self):
        context, samples, fitting, policy = fixture()
        inputs = {"context": context, "samples": samples, "fitting": fitting, "policy": policy}
        actual = news_validation.predictive_increment(**inputs)
        self.assertEqual(news_validation.validate_predictive_increment(actual, inputs)["status"], "partial")
        changed = copy.deepcopy(actual)
        changed["paired_rows"][0]["loss_difference_native_minus_full"] += 1
        with self.assertRaisesRegex(ValueError, "sealed original source comparison"):
            news_validation.validate_predictive_increment(changed, inputs)


class OriginalVintageVerification(unittest.TestCase):
    def test_original_quote_does_not_inherit_later_text_or_unknown_observation(self):
        def version(nav, text, at, identity, qualified=True):
            raw = {"source_id": "synthetic", "sha256": identity*64}
            return {"code": "C", "date": "2020-01-01", "nav": nav, "cumulative_nav": nav,
                    "distribution_per_share": 0., "distribution_text": text, "provider_daily_return": .01,
                    "version_id": identity*64, "observed_at": at, "available_at": at if qualified else None,
                    "raw_ref": raw, "availability_evidence": {"kind": "archived_original_capture", "source_id": "synthetic",
                        "raw_sha256": raw["sha256"], "captured_at": at} if qualified else None}
        old = version(10., "original", "2020-01-02T09:00:00+08:00", "a")
        late = version(99., "later", "2020-01-05T09:00:00+08:00", "b")
        row = {"code": "C", "date": "2020-01-01", "nav": 99., "distribution_per_share": 0.,
               "distribution_text": "later", "source_versions": [old, late]}
        quote = verify._source_nav_quote(row, "2020-01-03T20:00:00+08:00")
        self.assertEqual(quote["nav"], 10.)
        self.assertEqual(quote["distribution_text"], "original")
        row["source_versions"] = [version(99., "unknown", "2020-01-05T09:00:00+08:00", "b", False)]
        self.assertIsNone(verify._source_nav_quote(row, "2020-01-03T20:00:00+08:00", require_known=True))


if __name__ == "__main__":
    unittest.main()
