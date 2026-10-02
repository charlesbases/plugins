"""Controlled source/cash truths; these are not market or profit evidence."""
import copy
import datetime as dt
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
from contracts import fingerprint
import allocation_market as market
import allocation_statistics as statistics


def version(value, at, qualified=True):
    digest = fingerprint(value)
    ident = "engineering-original-NAV-A"
    result = {**copy.deepcopy(value), "observed_at": at, "available_at": at if qualified else None,
              "raw_ref": {"source_id": ident, "sha256": digest},
              "availability_evidence": {"kind": "archived_original_capture", "source_id": ident,
                  "raw_sha256": digest, "captured_at": at} if qualified else None}
    result["version_id"] = fingerprint(result)
    return result


def raw_series(first="2026-01-06", count=3, qualified=True):
    result = {}
    for index in range(count):
        day = dt.date.fromisoformat(first)+dt.timedelta(days=index)
        value = {"code": "A", "date": str(day), "nav": 1., "cumulative_nav": 1.,
                 "distribution_per_share": 0., "corporate_actions": []}
        result[day] = {**value, "source_versions": [version(value, str(day)+"T21:00:00+08:00", qualified)]}
    return result


def append_capture(raw, at, change=None):
    for day, row in raw.items():
        value = {key: copy.deepcopy(item) for key, item in row.items() if key != "source_versions"}
        if change is not None:
            change(day, value)
        row["source_versions"].append(version(value, at))
        row.update(value)


def source_case(origin="2026-01-06", horizon=2):
    day = dt.date.fromisoformat(origin)
    source = {"code": "A", "base_date": str(day-dt.timedelta(days=1)), "base_nav": 1.,
              "pricing_date": origin, "ownership_date": origin,
              "end_date": str(day+dt.timedelta(days=horizon)), "dividends": []}
    sample = {"code": "A", "fund_group_id": "A", "decision_date": origin, "horizon_days": horizon,
              "feature_cutoff_date": source["base_date"], "x": [0.]*len(market.FEATURE_NAMES),
              "industry_ready": True, "label_available_date": str(day+dt.timedelta(days=horizon+1)),
              "feature_source": {"base_nav": 1., "strict_PIT_verified": True, "availability_basis": "source_vintages"}}
    return sample, source


def cash_event(pay="2026-01-10", included=True):
    return {"kind": "cash_distribution", "code": "A", "id": "engineering-original-income",
            "record_date": "2026-01-06", "ex_date": "2026-01-06", "pay_date": pay,
            "per_share": .1, "currency": "CNY", "distribution_mode": "cash",
            "entitlement_rule": {"subscribe_on_record_date": "included" if included else "excluded",
                                 "redeem_on_record_date": "included"},
            "known_at": "2026-01-05T20:00:00+08:00", "evidence_ref": {"engineering_original_income": True}}


def income_series(event):
    raw = raw_series()
    for day, row in raw.items():
        row.update(nav=.9, cumulative_nav=1., distribution_per_share=.1 if str(day)=="2026-01-06" else 0.,
                   corporate_actions=[copy.deepcopy(event)] if str(day)=="2026-01-06" else [])
        value = {key: item for key, item in row.items() if key != "source_versions"}
        row["source_versions"] = [version(value, str(day)+"T21:00:00+08:00")]
    return raw


class NativeVintageMaturityTests(unittest.TestCase):
    def test_repeated_capture_correction_and_reversion_preserve_actual_maturity(self):
        raw = raw_series()
        sample, source = source_case()
        original = market._label_versions(sample, source, raw)
        append_capture(raw, "2026-02-03T21:00:00+08:00")
        self.assertEqual(market._label_versions(sample, source, raw), original)
        def revise(day, value):
            if str(day)==source["end_date"]:
                value.update(nav=1.2, cumulative_nav=1.2)
        def revert(day, value):
            if str(day)==source["end_date"]:
                value.update(nav=1., cumulative_nav=1.)
        append_capture(raw, "2026-02-04T21:00:00+08:00", revise)
        append_capture(raw, "2026-02-05T21:00:00+08:00", revert)
        labels = market._label_versions(sample, source, raw)
        self.assertEqual([row["label_available_at"] for row in labels],
                         ["2026-01-08T21:00:00+08:00", "2026-02-04T21:00:00+08:00", "2026-02-05T21:00:00+08:00"])
        self.assertEqual(labels[0]["targets"], labels[2]["targets"])
        self.assertNotEqual(labels[0]["targets"], labels[1]["targets"])
        self.assertEqual(market.mature_label_at({"label_versions": labels}, "2026-02-06")["label_available_at"],
                         "2026-02-05T21:00:00+08:00")

    def test_partial_source_proof_upgrade_is_not_coalesced_to_proxy_time(self):
        raw = raw_series(qualified=False)
        sample, source = source_case()
        append_capture(raw, "2026-02-03T21:00:00+08:00")
        labels = market._label_versions(sample, source, raw)
        self.assertEqual(len(labels), 2)
        self.assertEqual(labels[0]["targets"], labels[1]["targets"])
        self.assertFalse(labels[0]["label_source"]["strict_PIT_verified"])
        self.assertTrue(labels[1]["label_source"]["strict_PIT_verified"])
        self.assertEqual(labels[1]["label_available_at"], "2026-02-03T21:00:00+08:00")

    def test_incomplete_income_vintage_stays_unlabelled_until_original_rights_complete(self):
        raw = income_series(cash_event(pay=None))
        sample, source = source_case()
        with self.assertRaisesRegex(ValueError, "source is incomplete"):
            market._label_versions(sample, source, raw)
        def complete(day, value):
            if str(day)=="2026-01-06":
                value["corporate_actions"] = [cash_event(pay="2026-02-05")]
                value["corporate_actions"][0]["known_at"] = "2026-02-03T21:00:00+08:00"
        append_capture(raw, "2026-02-03T21:00:00+08:00", complete)
        labels = market._label_versions(sample, source, raw)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["label_available_at"], "2026-02-03T21:00:00+08:00")
        self.assertIsNone(market.mature_label_at({"label_versions": labels}, "2026-02-02"))
        self.assertAlmostEqual(labels[0]["label_source"]["dividend_values"]["hold"], .1)
        self.assertAlmostEqual(labels[0]["targets"]["hold"], 1.)

    def test_real_income_rights_revision_is_a_new_state_without_NAV_change(self):
        raw = income_series(cash_event())
        sample, source = source_case()
        original = market._label_versions(sample, source, raw)
        def revise(day, value):
            if str(day)=="2026-01-06":
                value["corporate_actions"] = [cash_event(included=False)]
        append_capture(raw, "2026-02-04T21:00:00+08:00", revise)
        labels = market._label_versions(sample, source, raw)
        self.assertEqual(len(labels), 2)
        self.assertEqual(labels[1]["label_available_at"], "2026-02-04T21:00:00+08:00")
        self.assertEqual(labels[0]["targets"]["terminal_nav"], labels[1]["targets"]["terminal_nav"])
        self.assertNotEqual(labels[0]["targets"]["buy"], labels[1]["targets"]["buy"])
        self.assertEqual(labels[0], original[0])

    def test_same_capture_cannot_erase_native_forward_OOS_calibration(self):
        first = dt.date(2026,1,1)
        raw = raw_series(str(first), 82)
        cases = [source_case(str(first+dt.timedelta(days=index)), 1) for index in range(80)]
        def samples():
            rows = []
            for sample, source in cases:
                labels = market._label_versions(sample, source, raw)
                rows.append({**copy.deepcopy(sample), **labels[-1], "label_versions": labels})
            return rows
        prediction = [{**cases[-1][0], "decision_date": "2026-04-01"}]
        # Exact retained public3 native policy, including all original thresholds.
        policy = {"alpha_grid": [.01], "cv_folds": 2, "cv_initial_train_fraction": .5,
                  "feature_names": list(market.FEATURE_NAMES), "l1_ratio_grid": [.5],
                  "min_joint_dates": 8, "min_train_dates": 16, "train_window_days": 180}
        before = statistics.fit_joint_targets(samples(), prediction, policy, {"order_time_local": "08:00:00"})
        append_capture(raw, "2026-03-30T21:00:00+08:00")
        after = statistics.fit_joint_targets(samples(), prediction, policy, {"order_time_local": "08:00:00"})
        self.assertEqual(before["status"], "research_ready", before.get("reason"))
        self.assertEqual(after, before)
        self.assertGreater(len(after["joint_scenarios"]["source_selection_oos"]), 0)
        self.assertGreater(len(after["joint_scenarios"]["source_calibration_oos"]), 0)


if __name__ == "__main__":
    unittest.main()
