"""Original-source engineering regressions; no claim of live manager alpha."""
import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fund_quality
import fund_screen
import portfolio_paths
from contracts import fingerprint, instant, utc_now
import test_fund_quality as quality_fixtures


class CurrentManagerRegimeTests(unittest.TestCase):
    setUp = quality_fixtures.FundQualityTests.setUp
    doc = quality_fixtures.FundQualityTests.doc
    series = quality_fixtures.FundQualityTests.series
    source_data = quality_fixtures.FundQualityTests.source_data

    def prepare(self, reference, contracts, at=None):
        return fund_quality.prepare_source_quality(reference, contracts, self.artifacts, at or utc_now(), self.policy)

    def test_effective_new_manager_after_last_NAV_has_zero_observations(self):
        today = instant(utc_now()).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        _, reference, contract = self.source_data(appointments=[("甲经理", self.days[0], self.days[-1]),
                                                               ("乙经理", today, "至今")])
        result = self.prepare(reference, [contract])
        statistics = result["current_evaluation"]["products"]["123456"]["statistics"]
        self.assertEqual(statistics["manager_regimes"][0]["status"], "estimated")
        current = statistics["current_manager_regime"]
        self.assertEqual(current["managers"], ["乙经理"])
        self.assertEqual(current["observations"], 0)
        self.assertEqual(current["status"], "unknown")
        self.assertEqual(result["feature_panel"], [])
        self.assertEqual(result["admission"]["excluded_new_codes"], [])
        with self.assertRaisesRegex(ValueError, "No source-quality feature vintage"):
            portfolio_paths._quality_features({"code": "123456", "x": []}, result,
                                              result["input_binding"]["decision_at"])
        self.assertEqual(fund_quality.validate_source_quality(result, reference, [contract], self.artifacts,
            result["input_binding"]["decision_at"], self.policy)["status"], "passed")
        selection = {"eligible_buy_codes": ["123456"], "per_code": {"123456": {"eligible": True, "due_diligence": {}}}}
        selection["selection_hash"] = fingerprint(selection)
        annotated = fund_screen.attach_quality(selection, result["current_evaluation"], result["admission"])
        self.assertEqual(annotated["eligible_buy_codes"], ["123456"])

    def test_same_named_reappointment_cannot_borrow_earlier_tenure(self):
        today = instant(utc_now()).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        _, reference, contract = self.source_data(appointments=[("甲经理", self.days[0], self.days[-1]),
                                                               ("甲经理", today, "至今")])
        result = self.prepare(reference, [contract])
        statistics = result["current_evaluation"]["products"]["123456"]["statistics"]
        prior, current = statistics["manager_regimes"][0], statistics["current_manager_regime"]
        self.assertEqual(prior["managers"], current["managers"])
        self.assertNotEqual(prior["team_regime_id"], current["team_regime_id"])
        self.assertEqual(current["observations"], 0)
        self.assertEqual(result["feature_panel"], [])

    def test_old_disclosure_retains_historical_statistics_but_no_current_Q(self):
        _, reference, contract = self.source_data(disclosure_as_of=self.days[-1])
        result = self.prepare(reference, [contract])
        statistics = result["current_evaluation"]["products"]["123456"]["statistics"]
        self.assertEqual(statistics["manager_regimes"][0]["status"], "estimated")
        self.assertEqual(statistics["current_manager_regime"]["status"], "unknown")
        self.assertEqual(result["feature_panel"], [])
        self.assertTrue(any(row.get("reason") == "current_team_not_covered_by_original_disclosure"
                            for row in result["source_gaps"]))

    def test_future_observed_appointment_is_not_backfilled_into_decision(self):
        old_data, reference, old = self.source_data()
        old["identity_ref"] = self.artifacts.put_json(old_data["code_info"]["123456"])
        at = utc_now()
        future = (instant(at)+dt.timedelta(days=1)).isoformat()
        # Future source capture exists only in this controlled transport test;
        # known_at is derived from its original capture, never edited in output.
        with patch("test_industry_data.utc_now", return_value=future):
            new_data, _, new = self.source_data(appointments=[("乙经理", self.days[0], "至今")])
        new["identity_ref"] = self.artifacts.put_json(new_data["code_info"]["123456"])
        result = self.prepare(reference, [{"source_vintages": [old, new]}], at)
        self.assertEqual(result["feature_panel"][0]["team_regime"], ["甲经理"])
        self.assertEqual(result["current_evaluation"]["products"]["123456"]["statistics"]
                         ["current_manager_regime"]["managers"], ["甲经理"])


if __name__ == "__main__":
    unittest.main()
