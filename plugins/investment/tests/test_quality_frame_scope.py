"""Analytical source-frame scope and real EN beta; no market-profit evidence."""
import copy
import datetime as dt
import math
import unittest

import allocation_market as market
import industry_model
import portfolio_paths as paths
import quality_selection
from contracts import fingerprint
from fund_quality import QUALITY_FEATURE_NAMES
from test_allocation import source_context


def inputs():
    base = dt.date(2030, 1, 1)-dt.timedelta(days=133)
    off_names = market.FEATURE_NAMES+industry_model.FUND_FEATURE_NAMES
    frames, panel = [], []
    for i in range(130):
        day = str(base+dt.timedelta(days=i))
        signal = 1. if i in (100, 110) else math.sin(i*.8)
        row = {"code": "C", "decision_date": day, "fund_group_id": "fund-C", "x": [0.]*len(off_names),
               "wealth_ratios": [1.]*8+[math.exp(-.1+.25*signal)], "cash": [], "base_nav": 1.,
               "label_available_date": str(base+dt.timedelta(days=i+8)),
               "label_available_at": str(base+dt.timedelta(days=i+8))+"T16:00:00+08:00",
               "source_hash": fingerprint({"explicit_analytical_original": i})}
        row["frame_versions"] = [copy.deepcopy(row)]
        frames.append(row)
        if i >= 20:
            feature = {"code": "C", "decision_at": day+"T00:00:00+08:00",
                       "source_known_at": str(base-dt.timedelta(days=1))+"T16:00:00+08:00",
                       "values": [signal]+[0.]*(len(QUALITY_FEATURE_NAMES)-1), "team_regime": ["analytical-team"]}
            feature["feature_hash"] = fingerprint(feature)
            panel.append(feature)
    bundle = {"feature_names": QUALITY_FEATURE_NAMES, "feature_panel": panel}
    bundle["quality_bundle_hash"] = fingerprint(bundle)
    policy = {"train_window_days": 80, "min_train_dates": 20, "cv_folds": 3, "cv_initial_train_fraction": .7,
              "alpha_grid": [.0001, .001], "l1_ratio_grid": [.5, 1.]}
    selection = [frames[i]["decision_date"] for i in (100, 110)]
    calibration = [frames[i]["decision_date"] for i in (119, 124)]
    return frames, bundle, policy, selection, calibration


class QualityFrameScopeTests(unittest.TestCase):
    def test_unused_old_Q_gap_keeps_actual_on_beta_and_identical_used_cohorts(self):
        frames, bundle, policy, selection, calibration = inputs()
        original = copy.deepcopy(frames)
        ceiling = calibration[0]+"T12:00:00+08:00"
        keys = paths._quality_frame_keys(frames, ["C"], selection+calibration, "2030-01-01", policy, ceiling)
        independently_required = {(origin, "C") for origin in selection+calibration}
        for origin in selection+calibration:
            independently_required.update((row["decision_date"], row["code"])
                                          for row in paths._eligible(frames, ["C"], origin, policy))
        independently_required.update((row["decision_date"], row["code"])
                                      for row in paths._eligible(frames, ["C"], "2030-01-01", policy, ceiling))
        self.assertEqual(keys, independently_required)
        self.assertNotIn((frames[0]["decision_date"], "C"), keys)
        with self.assertRaisesRegex(ValueError, "current team"):
            paths._quality_features(frames[0], bundle, frames[0]["decision_date"]+"T12:00:00+08:00")
        on_frames = [paths._quality_features(row, bundle, row["decision_date"]+"T12:00:00+08:00")
                     for row in frames if (row["decision_date"], row["code"]) in keys]
        off_names = market.FEATURE_NAMES+industry_model.FUND_FEATURE_NAMES
        on_names = off_names+QUALITY_FEATURE_NAMES
        context = source_context(cash="8")
        context["spec"]["planning"]["primary_horizon_days"] = 8
        marks = {"C": {"nav_date": "2029-12-31", "value": "1", "known_at": "2029-12-31T16:00:00+08:00",
                       "source_ref": {"explicit_analytical_original": True}}}
        contract = {"price_schema": "source_role_price_paths_v3", "codes": ["C"], "known_marks": marks,
                    "origin_date": "2030-01-01", "decision_at": context["decision_at"],
                    "nav_dates": [str(dt.date(2030, 1, 1)+dt.timedelta(days=i)) for i in range(9)]}
        arms = {"off": [], "on": []}
        for origin in selection:
            audits = []
            for arm, source, names in (("off", frames, off_names), ("on", on_frames, on_names)):
                model, audit = paths._fit_at(source, ["C"], origin, policy, [], ["fund-C"], feature_names=names)
                audits.append(audit)
                row = next(row for row in source if row["decision_date"] == origin)
                if arm == "on":
                    self.assertGreater(abs(model["coefficients"][-1][len(off_names)]), .05)
                def decoded(values, identity):
                    return paths._decode({"C": {"values": values, "cash_keys": []}}, {"C": 1.}, contract,
                        origin+"T12:00:00+08:00", [], identity, 1., row["label_available_at"])
                arms[arm].append({"origin_at": origin+"T12:00:00+08:00", "label_available_at": row["label_available_at"],
                    "source_hashes": [row["source_hash"]], "model_training_audit": audit,
                    "predicted": decoded(paths._predict(model, row), "predicted-"+origin),
                    "realized": decoded(paths._targets(row, []), "realized-"+origin)})
            self.assertEqual(audits[0]["training_source_keys"], audits[1]["training_source_keys"])
        value = quality_selection.select_quality_arm(context, contract, arms, source_hash=fingerprint(frames),
                    feature_names_by_arm={"off": off_names, "on": on_names})
        self.assertEqual(value["selected_arm"], "on")
        self.assertGreater(value["paired_mean_wealth_increment"], 0.)
        self.assertFalse(value["trade_ready"])
        self.assertFalse(value["calibration_outcomes_consumed"])
        self.assertEqual(frames, original)

    def test_missing_actually_used_Q_still_refuses_on_without_mutating_off(self):
        frames, bundle, policy, selection, calibration = inputs()
        keys = paths._quality_frame_keys(frames, ["C"], selection+calibration, "2030-01-01", policy,
                                         calibration[0]+"T12:00:00+08:00")
        missing = min(keys)[0]
        bundle["feature_panel"] = [row for row in bundle["feature_panel"] if row["decision_at"][:10] != missing]
        bundle["quality_bundle_hash"] = fingerprint({key: value for key, value in bundle.items() if key != "quality_bundle_hash"})
        with self.assertRaisesRegex(ValueError, "current team"):
            for row in frames:
                if (row["decision_date"], row["code"]) in keys:
                    paths._quality_features(row, bundle, row["decision_date"]+"T12:00:00+08:00")
        model, audit = paths._fit_at(frames, ["C"], selection[0], policy, [], ["fund-C"],
                                    feature_names=market.FEATURE_NAMES+industry_model.FUND_FEATURE_NAMES)
        self.assertTrue(audit["training_rows"])
        self.assertEqual(len(model["feature_names"]), 46)


if __name__ == "__main__":
    unittest.main()
