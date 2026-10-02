"""Declared synthetic arithmetic archives exercise the real fitting code.

These are engineering regressions, not historical or investment-effectiveness
evidence; no validator or fitted result is mocked as successful.
"""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import candidate_readiness
import industry_model
import allocation_market as market
import single_step_wealth
from contracts import fingerprint
from test_industry_model import archive, policy, origins
from test_single_step import make_terms


def inputs():
    sector = archive(constant=True)
    days = [row["date"] for row in sector["sectors"][0]["prices"]]
    day = days[64]
    data = {"nav": {}, "features": {}, "code_info": {}, "industry": sector,
            "industry_exposures": {}, "industry_data_ref": {"synthetic": "explicit_math_fixture"}}
    terms = {}
    for code, slope in (("A", .001), ("B", .002), ("C", .003)):
        data["nav"][code] = [{"code": code, "date": date, "nav": 1+index*slope,
                              "distribution_per_share": 0} for index, date in enumerate(days)]
        data["features"][code] = [{"code": code, "feature_cutoff_nav_date": date,
            "known_max_nav_date": date, "values": dict.fromkeys(market.NAV_FEATURE_NAMES, 0.)} for date in days]
        data["code_info"][code] = {"fund_group_id": "synthetic-"+code}
        data["industry_exposures"][code] = [{"sector_id": "industry-a", "weight": ".8", "basis":"historical_disclosure", "role":"single_sector", "source_label":"industry-a",
            "actual_exposure_status":"historical_disclosed_not_current", "actual_weight":".8", "stale":True,
            "as_of_date": days[0], "known_at": days[0]+"T09:00:00+08:00",
            "evidence_refs": [{"synthetic_disclosure": code}]}]
        terms[code] = make_terms(code)
        terms[code]["execution_calendar"].update(coverage_start=days[0], coverage_end=days[-1], open_dates=days)
    spec = {"training": policy(), "industry": {"training": policy()},
            "planning": {"primary_horizon_days": 3, "primary_goal": "hold"},
            "availability": {"nav_lag_calendar_days": 0, "max_feature_age_days": 2},
            "decision": {"max_qualification_families": 64}}
    at = day+"T12:00:00+08:00"
    clock = single_step_wealth.build_clock_context({"spec": spec, "decision_at": at,
        "allocation_codes": list(terms), "fee_contracts": terms})
    state = {"positions": [{"code": "A"}, {"code": "B"}], "open_orders": []}
    return state, ["A", "B", "C"], data, spec, at, clock, terms


class EconomicIndustryBoundaries(unittest.TestCase):
    def test_opposite_objective_changes_have_distinct_economic_inputs_with_same_counts(self):
        down = archive()
        up = copy.deepcopy(down)
        for item in down["sectors"][0]["economic_observations"]:
            item["relative_delta"] = -.1
        for item in up["sectors"][0]["economic_observations"]:
            item["relative_delta"] = .1
        day = origins(down)[20]
        x_down, _ = industry_model.feature_at(down["sectors"][0], day, "12:00:00")
        x_up, _ = industry_model.feature_at(up["sectors"][0], day, "12:00:00")
        self.assertNotEqual(x_down[:-8], x_up[:-8])
        self.assertEqual(x_down[-8:], x_up[-8:])

    def test_missing_economic_content_cannot_run_count_only_forecast(self):
        bundle = archive()
        bundle["sectors"][0]["economic_observations"] = []
        result = industry_model.build_forward(bundle, origins(bundle), policy(), 3, "12:00:00")
        self.assertEqual(result["forecasts"], [])
        self.assertFalse(result["trade_ready"])
        self.assertTrue(any("source event lacks its own verified economic encoding" in row["reason"] for row in result["unavailable"]))

    def test_future_price_revision_does_not_replace_historical_base(self):
        sector = archive()["sectors"][0]
        old = copy.deepcopy(sector["prices"][10])
        revised = {**old, "close": 999999., "available_at": sector["prices"][50]["available_at"],
                   "raw_ref": {"synthetic_correction": "genuinely_later"}}
        sector["price_versions"] = sector["prices"] + [revised]
        sector["prices"][10] = revised
        _, witness = industry_model.feature_at(sector, old["date"], "12:00:00")
        self.assertEqual(witness["benchmark_base"], old)
        self.assertEqual(next(row for row in industry_model.prices_at(sector, revised["available_at"])
                              if row["date"] == old["date"])["close"], 999999.)

    def test_unready_other_sector_does_not_erase_ready_sector_forecast(self):
        bundle = archive(constant=True)
        other = copy.deepcopy(bundle["sectors"][0])
        other["sector_id"] = "unready-other"
        other["economic_observations"] = []
        bundle["sectors"].append(other)
        result = industry_model.build_forward(bundle, origins(bundle), policy(), 3, "12:00:00")
        self.assertIn("industry-a", result["ready_sector_ids"])
        self.assertEqual(result["sector_readiness"]["unready-other"]["status"], "insufficient_evidence")
        frozen = industry_model.freeze_current(result, origins(bundle)[50]+"T12:00:00+08:00", ["industry-a"])
        self.assertTrue(any(row["sector_id"] == "industry-a" and row["decision_date"] == origins(bundle)[-1]
                            for row in frozen["forecasts"]))

    def test_future_outcome_correction_keeps_prior_fit_and_applies_only_when_known(self):
        bundle = archive(constant=True)
        revised = copy.deepcopy(bundle)
        sector = revised["sectors"][0]
        original = copy.deepcopy(sector["prices"])
        correction = {**original[10], "close": 101., "available_at": original[50]["available_at"],
                      "raw_ref": {"synthetic_correction": "released_at_day_50"}}
        sector["price_versions"] = original + [correction]
        sector["prices"][10] = correction
        days = origins(bundle)
        before, _ = industry_model.build_samples(bundle, days, 3, "12:00:00")
        after, _ = industry_model.build_samples(revised, days, 3, "12:00:00")
        for samples in (before, after):
            row = next(item for item in samples if item["decision_date"] == days[30])
            fit = industry_model.fit_at(samples, row, policy(), "12:00:00")
            if samples is before:
                baseline = fit
            else:
                self.assertEqual(fit["training_audit"]["training_data_hash"], baseline["training_audit"]["training_data_hash"])
                self.assertEqual(fit["predicted_coordinates"], baseline["predicted_coordinates"])
        matured = industry_model.eligible_at(after, "industry-a", days[60], policy(), "12:00:00")
        day_nine = next(row for row in matured if row["decision_date"] == days[9])
        self.assertEqual(day_nine["label_source"]["phase"]["close"], 101.)
        self.assertEqual(day_nine["label_available_at"], correction["available_at"])


class CandidateQualificationBoundaries(unittest.TestCase):
    def test_unready_new_candidate_does_not_change_held_family_joint_proof(self):
        state, codes, data, spec, at, clock, terms = inputs()
        data["industry_exposures"]["C"][0]["known_at"] = "2035-01-01T00:00:00+08:00"
        combined = candidate_readiness.assess_readiness(state, codes, data, spec, at, clock, fee_contracts=terms)
        isolated = candidate_readiness.assess_readiness(state, ["A", "B"], data, spec, at, clock, fee_contracts=terms)
        ab = next(row for row in combined["candidate_families"] if row["allocation_codes"] == ["A", "B"])
        baseline = next(row for row in isolated["candidate_families"] if row["allocation_codes"] == ["A", "B"])
        self.assertEqual(ab["fit_hash"], baseline["fit_hash"])
        self.assertEqual(ab["samples_hash"], baseline["samples_hash"])
        self.assertIn("C", combined["excluded_new_codes"])
        self.assertEqual(combined["missing_held_codes"], [])
        self.assertGreater(len(ab["calibration_origins"]), 0)
        self.assertEqual(data["industry_exposures"]["C"][0]["known_at"], "2035-01-01T00:00:00+08:00")

    def test_unknown_held_candidate_remains_risk_and_prevents_qualified_family(self):
        state, codes, data, spec, at, clock, terms = inputs()
        state["positions"].append({"code": "C"})
        data["industry_exposures"]["C"] = []
        result = candidate_readiness.assess_readiness(state, codes, data, spec, at, clock, fee_contracts=terms)
        self.assertEqual(result["required_risk_codes"], ["A", "B", "C"])
        self.assertEqual(result["missing_held_codes"], ["C"])
        self.assertEqual(result["candidate_families"], [])
        self.assertFalse(result["trade_ready"])

    def test_live_order_is_kept_in_risk_universe(self):
        self.assertEqual(candidate_readiness.risk_codes({"positions": [{"code": "A"}],
                         "open_orders": [{"code": "C"}]}), ["A", "C"])


if __name__ == "__main__":
    unittest.main()
