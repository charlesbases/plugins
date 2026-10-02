"""Independent qualification counterexamples, not expected-return score tests."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import fund_screen
from contracts import fingerprint, utc_now
import test_fund_universe as universe_fixture


class FundScreenTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.plan = universe_fixture.FundUniverseTests.policy(), universe_fixture.FundUniverseTests.plan()
        self.ids = {}
        for code, share in [("123451", "A"), ("123452", "C"), ("123453", "Y")]:
            self.ids[code] = {"code": code, "currency": "CNY", "dealing_currency": "CNY",
                "execution_venue": "off_exchange_nav", "instrument_type": "off_exchange_nav",
                "share_class": share, "type": "synthetic-equity", "asset_class": "synthetic-equity",
                "fund_group_id": "source-legal-pool", "benchmark_id": None,
                "identity_scope": "synthetic-source", "manager_tenures": None, "company_restrictions": None,
                "sector_exposures": None, "unknown_fields": ["manager_tenures", "sector_exposures"]}
        group = {"group_id": "g", "kind": "same_legal_fund", "members": sorted(self.ids),
                 "coverage_status": "complete_declared_scope", "coverage_scope": "explicit_fixture_member_list"}
        self.discovery = {"schema_version": 4, "universe_policy": self.policy, "groups": [group],
            "codes": sorted(self.ids), "held_codes": [], "monitoring_codes": [],
            "completed_group_candidate_codes": sorted(self.ids),
            "exposures": {code: [{"thesis_id": "source-thesis"}] for code in self.ids}}
        self.discovery["comparison_identities"] = self.ids
        self.discovery["plan_constraints_hash"] = fingerprint(self.plan)
        self.discovery["category_labels"] = ["synthetic-equity", "synthetic-bond"]
        self.discovery["prequalification"] = {code: {"eligible": row["share_class"] != "Y",
            "reasons": ["pension_share_investor_eligibility_not_established"] if row["share_class"] == "Y" else []}
            for code, row in self.ids.items()}
        self.terms = [{"code": code, "fee_contract_ref": {"fixture": "source-bound-unit-test"},
            "observed_at": utc_now(),
            "fee_contract": {"subject": {"code": code, "share_class": row["share_class"], "currency": "CNY",
                                         "channel": "TT", "investor_type": "retail"},
                             "buyable": True, "holding": {"minimum_days": 0},
                             "redemption": {"kind": "percentage", "rate": "0"}}} for code, row in self.ids.items()]

    def select(self):
        # These pure screen fixtures represent newly discovered facts under the
        # specified plan. Stale-discovery rejection is tested separately.
        discovery = {**self.discovery, "plan_constraints_hash": fingerprint(self.plan)}
        return fund_screen.finalize_selection(discovery, self.ids, self.terms, self.plan, utc_now(), max_terms_age_seconds=3600)

    def test_complete_group_does_not_bypass_pension_investor_qualification(self):
        selected = self.select()
        self.assertEqual(selected["eligible_buy_codes"], ["123451", "123452"])
        self.assertIn("pension_share_investor_eligibility_not_established", selected["per_code"]["123453"]["reasons"])
        self.assertFalse(selected["per_code"]["123451"]["due_diligence"]["manager_skill_estimated"])
        self.assertEqual(selected["per_code"]["123451"]["cost_scenarios"]["0"]["status"], "portfolio_model_required")

    def test_unknown_and_unsupported_units_never_default_to_cny(self):
        self.ids["123451"]["currency"] = None
        self.ids["123452"]["currency"] = "USD"
        selected = self.select()
        self.assertEqual(selected["eligible_buy_codes"], [])
        self.assertIn("unknown_currency", selected["per_code"]["123451"]["reasons"])
        self.assertIn("unsupported_currency", selected["per_code"]["123452"]["reasons"])

    def test_fee_sensitivity_window_is_not_a_lock_eligibility_deadline(self):
        self.terms[0]["fee_contract"]["holding"]["minimum_days"] = 31
        self.terms[1]["fee_contract"]["subject"]["share_class"] = "A"
        selected = self.select()
        self.assertTrue(selected["per_code"]["123451"]["eligible"])
        self.assertIn("dealing_share_class_differs", selected["per_code"]["123452"]["reasons"])

    def test_cost_diagnostics_follow_each_source_and_missing_terms_have_no_default_ages(self):
        self.terms[0]["fee_contract"]["holding"]["minimum_days"] = 400
        self.terms[1]["fee_contract"]["redemption"] = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "365", "fee": {"kind": "percentage", "rate": "0"}}]}
        self.terms.pop()
        selected = self.select()
        self.assertEqual(set(selected["per_code"]["123451"]["cost_scenarios"]), {"400"})
        self.assertEqual(set(selected["per_code"]["123452"]["cost_scenarios"]), {"0", "1", "364", "365", "366"})
        self.assertEqual(selected["per_code"]["123453"]["cost_scenarios"], {})
        self.assertTrue(selected["per_code"]["123451"]["eligible"])

    def test_unknown_residual_exposure_cannot_bypass_explicit_plan_exclusion(self):
        self.plan["constraints"]["excluded_categories"] = ["coal"]
        self.plan["revision_id"] = fingerprint({k: v for k, v in self.plan.items() if k != "revision_id"})
        self.ids["123451"]["sector_exposures"] = {"status": "measured_disclosure", "weights": {"technology": "0.1"},
                                                 "unmapped_weight": "0.9"}
        selected = self.select()
        self.assertIn("cannot_verify_all_plan_category_exclusions", selected["per_code"]["123451"]["reasons"])

    def test_source_roster_semantics_can_share_different_valid_capture_refs(self):
        entries = {code: {"type": "synthetic-equity"} for code in self.ids}
        for code, identity in self.ids.items():
            identity["group_membership"] = {"kind": "same_legal_fund", "codes": sorted(self.ids),
                                            "scope": "source-roster", "evidence_refs": [{"capture": code}]}
        groups = fund_screen.build_groups(entries, self.ids, self.policy)
        self.assertEqual(groups[0]["coverage_status"], "complete_declared_scope")
        self.assertEqual(len(groups[0]["membership_evidence"]), 3)

    def test_unknown_hedge_policy_keeps_benchmark_comparison_pending(self):
        entries = {code: {"type": "synthetic-equity"} for code in self.ids}
        for identity in self.ids.values():
            identity["benchmark_id"] = "same-index"
        groups = fund_screen.build_groups(entries, self.ids, self.policy)
        benchmark = next(row for row in groups if row["kind"] == "same_benchmark")
        self.assertEqual(benchmark["coverage_status"], "pending")

    def test_issuer_classification_cannot_shrink_catalog_comparison_scope(self):
        entries = {code: {"type": "synthetic-equity"} for code in self.ids}
        ids = {"123451": {**self.ids["123451"], "type": "issuer-different-taxonomy"}}
        groups = fund_screen.build_groups(entries, ids, self.policy)
        self.assertEqual(groups[0]["coverage_status"], "pending")
        self.assertEqual(groups[0]["pending_codes"], ["123452", "123453"])



    def test_stale_unheld_quote_is_excluded_without_removing_a_fresh_peer(self):
        self.terms[0]["observed_at"] = "2000-01-01T00:00:00Z"
        selected = self.select()
        self.assertEqual(selected["eligible_buy_codes"], ["123452"])
        self.assertIn("stale_dealing_quote", selected["per_code"]["123451"]["reasons"])
        self.assertEqual(selected["terms_policy"], {"rule": "source_quote_observed_before_cutoff", "max_terms_age_seconds": 3600})
        self.assertEqual(selected["terms_policy_hash"], fingerprint(selected["terms_policy"]))

    def test_quote_age_boundary_and_future_time_are_explicit(self):
        for term in self.terms:
            term["observed_at"] = "2030-01-01T00:00:00Z"
        self.terms[0]["observed_at"] = "2029-12-31T23:00:00Z"
        self.terms[1]["observed_at"] = "2029-12-31T22:59:59.999999Z"
        selected = fund_screen.finalize_selection(self.discovery, self.ids, self.terms, self.plan,
                                                  "2030-01-01T00:00:00Z", max_terms_age_seconds=3600)
        self.assertEqual(selected["eligible_buy_codes"], ["123451"])
        self.assertIn("stale_dealing_quote", selected["per_code"]["123452"]["reasons"])
        self.terms[0]["observed_at"] = "2030-01-01T00:00:01Z"
        selected = fund_screen.finalize_selection(self.discovery, self.ids, self.terms, self.plan,
                                                  "2030-01-01T00:00:00Z", max_terms_age_seconds=3600)
        self.assertIn("future_dealing_quote", selected["per_code"]["123451"]["reasons"])


    def test_complete_historical_table_does_not_prove_zero_current_or_subindustry_exposure(self):
        self.plan["constraints"]["excluded_categories"] = ["coal"]
        self.plan["revision_id"] = fingerprint({key: value for key, value in self.plan.items() if key != "revision_id"})
        for weights in ({"manufacturing": "1"}, {"coal": "0", "technology": "1"}):
            self.ids["123451"]["sector_exposures"] = {"status": "measured_disclosure", "weights": weights,
                "unmapped_weight": "0", "disclosed_as_of": "2020-01-01", "evidence_refs": [{"synthetic": True}]}
            selected = self.select()
            self.assertIn("cannot_verify_all_plan_category_exclusions", selected["per_code"]["123451"]["reasons"])

    def test_explicit_product_category_exclusion_does_not_require_industry_inference(self):
        self.plan["constraints"]["excluded_categories"] = ["synthetic-bond"]
        self.plan["revision_id"] = fingerprint({key: value for key, value in self.plan.items() if key != "revision_id"})
        self.assertEqual(self.select()["eligible_buy_codes"], ["123451", "123452"])
        self.plan["constraints"]["excluded_categories"] = ["synthetic-equity"]
        self.plan["revision_id"] = fingerprint({key: value for key, value in self.plan.items() if key != "revision_id"})
        self.assertEqual(self.select()["eligible_buy_codes"], [])

if __name__ == "__main__":
    unittest.main()
