"""Defect regressions on declared synthetic sources; no return-performance claim."""
import copy
import datetime as dt
import math
import unittest

import asset_exposure
import portfolio_paths
from contracts import fingerprint
from test_industry_model import archive, origins


def style_sources():
    data = archive()
    sector = data["sectors"][0]
    sector["sector_relation"] = {"status": "verified", "known_at": "2027-12-31T09:00:00+08:00"}
    nav = []
    for index, quote in enumerate(sector["prices"]):
        amount = .2 if index == 21 else 0.
        value = {20: 1., 21: .8, 22: .96}.get(index, (quote["close"]/100.)**2)
        day = quote["date"]
        known = (sector["prices"][23]["date"] if index == 21 else day)+"T10:00:00+08:00"
        events = [{"kind": "cash_distribution", "code": "synthetic-fund", "per_share": amount,
            "record_date": sector["prices"][20]["date"], "ex_date": day, "pay_date": sector["prices"][22]["date"],
            "known_at": known, "currency": "CNY", "distribution_mode": "cash",
            "entitlement_rule": {"subscribe_on_record_date": "included", "redeem_on_record_date": "included"},
            "evidence_ref": {"synthetic_original_cash": day}}] if amount else []
        row = {"code": "synthetic-fund", "date": day, "nav": value, "cumulative_nav": value,
            "distribution_per_share": amount, "corporate_actions": events}
        digest = fingerprint(row)
        version = {**copy.deepcopy(row), "available_at": known, "observed_at": known,
            "raw_ref": {"source_id": "synthetic-original", "sha256": digest},
            "availability_evidence": {"kind": "archived_original_capture", "source_id": "synthetic-original",
                "raw_sha256": digest, "captured_at": known}}
        version["version_id"] = fingerprint(version)
        nav.append({**row, "source_versions": [version]})
    origin = origins(data)[62]
    sector["prices"] = sector["prices"][::2]
    policy = {"train_window_days": 200, "min_train_dates": 20, "cv_initial_train_fraction": .6,
        "alpha_grid": [.000001], "l1_ratio_grid": [.5]}
    return nav, sector, origin, policy


def style_estimate(nav, sector, origin, policy):
    return asset_exposure.estimate("synthetic-fund", nav, [origin], [sector], policy, "12:00:00")


class StyleSourceWindowRegressionTests(unittest.TestCase):
    def test_noncommon_cash_is_held_without_reinvestment(self):
        nav, sector, origin, policy = style_sources()
        result = style_estimate(nav, sector, origin, policy)
        self.assertEqual(len(result), 1)
        interval = next(row for row in result[0]["audit"]["panel"] if row["date"] == nav[22]["date"])
        self.assertAlmostEqual(interval["y"], math.log(1.16))
        self.assertNotAlmostEqual(interval["y"], math.log(.96))
        self.assertNotAlmostEqual(interval["y"], math.log(1.2))
        self.assertEqual(interval["known_at"], nav[21]["source_versions"][0]["available_at"])
        self.assertEqual([row["date"] for row in interval["source"]["window_NAV"]], [row["date"] for row in nav[20:23]])
        self.assertEqual(asset_exposure.validate_estimates({"synthetic-fund": result}, {"synthetic-fund": nav}, [sector])["status"], "passed")

    def test_missing_or_late_middle_source_and_cash_rights_do_not_form_label(self):
        for missing in ("NAV", "late_NAV", "cash", "code", "known_at", "entitlement_rule", "distribution_per_share"):
            with self.subTest(missing=missing):
                nav, sector, origin, policy = style_sources()
                version = nav[21]["source_versions"][0]
                if missing == "NAV":
                    nav[21]["source_versions"] = []
                elif missing == "late_NAV":
                    stamp = "2028-04-01T10:00:00+08:00"
                    version.update(available_at=stamp, observed_at=stamp)
                    version["availability_evidence"]["captured_at"] = stamp
                elif missing == "cash":
                    version["corporate_actions"] = []
                elif missing == "distribution_per_share":
                    version.pop(missing)
                else:
                    version["corporate_actions"][0].pop(missing)
                result = style_estimate(nav, sector, origin, policy)
                self.assertEqual(len(result), 1)
                self.assertNotIn(nav[22]["date"], {row["date"] for row in result[0]["audit"]["panel"]})

    def test_independent_validator_rejects_omitted_middle_source_evidence(self):
        nav, sector, origin, policy = style_sources()
        result = style_estimate(nav, sector, origin, policy)
        altered = copy.deepcopy(result)
        audit = altered[0]["audit"]
        interval = next(row for row in audit["panel"] if row["date"] == nav[22]["date"])
        interval["source"]["window_NAV"] = interval["source"].get("window_NAV", interval["source"]["NAV"])[::2]
        interval["source"]["events"] = []
        audit["training_data_hash"] = fingerprint(audit["panel"])
        altered[0]["evidence_refs"] = [quote["raw_ref"] for row in audit["panel"] for quote in row["source"]["window_NAV"]]
        altered[0]["evidence_refs"] += [event["evidence_ref"] for row in audit["panel"] for event in row["source"]["events"]]
        altered[0]["model_hash"] = fingerprint(audit)
        with self.assertRaisesRegex(ValueError, "complete style source NAV versions"):
            asset_exposure.validate_estimates({"synthetic-fund": altered}, {"synthetic-fund": nav}, [sector])


def vintage_panel():
    frames = []
    for index in range(24):
        day = str(dt.date(2026, 1, 1)+dt.timedelta(days=index))
        mature = str(dt.date(2026, 1, 2)+dt.timedelta(days=index))
        for code in ("A", "B"):
            versions = []
            stamps = [mature+"T21:00:00+08:00"]
            if index < 12:
                stamps += ["2026-02-01T21:00:00+08:00", "2026-05-01T21:00:00+08:00"]
            for revision, stamp in enumerate(stamps):
                source = {"code": code, "origin": day, "revision": revision}
                versions.append({"code": code, "decision_date": day, "fund_group_id": code,
                    "x": [index*.01, float(code == "B"), 0., .01, .02, 1.],
                    "wealth_ratios": [1.+index*.001+revision*.01+i*.002 for i in range(3)],
                    "base_nav": 1., "cash": [], "label_available_date": stamp[:10], "label_available_at": stamp,
                    "label_source": source, "source_hash": fingerprint(source), "label_reason": None})
            frame = copy.deepcopy(versions[-1])
            frame["frame_versions"] = versions
            frames.append(frame)
    return frames


def independent_selection(frames, day, policy):
    lower = str(dt.date.fromisoformat(day)-dt.timedelta(days=policy["train_window_days"]))
    result = []
    for frame in frames:
        if not lower <= frame["decision_date"] < day:
            continue
        candidates = [version for version in frame["frame_versions"] if version["label_available_date"] < day]
        if candidates:
            chosen = copy.deepcopy(sorted(candidates, key=lambda row: (row["label_available_at"], row["source_hash"]))[-1])
            chosen["frame_versions"] = copy.deepcopy(candidates)
            result.append(chosen)
    return result


class NestedVintageRegressionTests(unittest.TestCase):
    def test_outer_correction_inner_original_and_deepcopy_boundaries(self):
        frames = vintage_panel()
        original = copy.deepcopy(frames)
        policy = {"train_window_days": 90}
        outer = portfolio_paths._eligible(frames, ["A", "B"], "2026-02-20", policy)
        self.assertEqual(outer[0]["label_available_date"], "2026-02-01")
        inner = portfolio_paths._eligible(outer, ["A", "B"], "2026-01-15", policy)
        self.assertEqual(inner[0]["label_available_date"], "2026-01-02")
        self.assertEqual(inner, independent_selection(outer, "2026-01-15", policy))
        self.assertNotIn("2026-01-14", {row["decision_date"] for row in inner})
        self.assertTrue(all(version["label_available_date"] < "2026-02-20" for row in outer for version in row["frame_versions"]))
        outer[0]["label_source"]["revision"] = 99
        outer[0]["frame_versions"][0]["label_source"]["revision"] = 98
        self.assertEqual(frames, original)
        limited = portfolio_paths._eligible(frames, ["A", "B"], "2026-02-20", {"train_window_days": 40})
        recovered = portfolio_paths._eligible(limited, ["A", "B"], "2026-01-15", policy)
        self.assertNotIn("2026-01-01", {row["decision_date"] for row in recovered})

    def test_real_fit_inner_training_hashes_match_independent_vintages(self):
        frames = vintage_panel()
        policy = {"train_window_days": 90, "min_train_dates": 4, "cv_folds": 2,
            "cv_initial_train_fraction": .6, "alpha_grid": [.01], "l1_ratio_grid": [.5]}
        expected = independent_selection(frames, "2026-02-20", policy)
        model, audit = portfolio_paths._fit_at(frames, ["A", "B"], "2026-02-20", policy, [], ["A", "B"])
        self.assertEqual(audit["training_source_keys"], [[row["code"], row["decision_date"], row["source_hash"]] for row in expected])
        self.assertEqual(audit["training_hash"], fingerprint(expected))
        self.assertEqual(len(model["intercept"]), 3)
        for fold in audit["trials"][0]["folds"]:
            inner = independent_selection(expected, fold["origin"], policy)
            self.assertEqual(fold["training_hash"], fingerprint(inner))
            self.assertEqual(fold["maximum_label_available"], max(row["label_available_date"] for row in inner))
        self.assertIsNone(portfolio_paths._training_gap(frames, ["A", "B"], "2026-02-20", policy))
