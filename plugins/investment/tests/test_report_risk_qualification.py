"""Source-optimised engineering decisions; these are not market profit evidence."""
import copy
import tempfile
import unittest
from pathlib import Path

import execution
import report
import report_validation
import verify
from artifacts import Artifacts
from test_decision_certificate import risk_case
from test_report_semantics import bundle_case


def source_bundle(objects, calibrated):
    context, paths, mpc = risk_case(calibration=calibrated)
    calculation = {"status": mpc["status"], "mpc": mpc, "paths": paths}
    calculation["orders"] = execution.compile_mpc_orders(calculation, context, {})
    orders = calculation["orders"]
    bundle = bundle_case(objects, context, orders["orders"], orders["reference_valuation"])
    bundle["orders"] = orders
    bundle["calculation_ref"] = objects.put_json(calculation)
    bundle["comparison_summary"] = report.summarize(calculation, context)
    bundle["action_analysis"] = report.explain_actions(bundle, objects)
    return bundle


class RiskQualificationReportTests(unittest.TestCase):
    def test_source_recovery_is_visible_and_cannot_claim_passed_budget(self):
        with tempfile.TemporaryDirectory(prefix="investment-risk-report-") as directory:
            bundle = source_bundle(Artifacts(Path(directory)), False)
            text = report.render(bundle)
            self.assertFalse(bundle["comparison_summary"]["selected"]["eligible"])
            self.assertEqual(bundle["orders"]["selected_plan_kind"], "risk_recovery")
            self.assertIn("风险预算尚未恢复（risk_not_restored）", text)
            self.assertEqual(verify.verify_report(bundle, text)["status"], "passed")
            for changed in (text.replace("普通资格 eligible=false", ""),
                            text + "\nwithin_verified_budget\n"):
                with self.assertRaisesRegex(ValueError, "risk recovery"):
                    report_validation.report_semantics(bundle, changed)
            corrupt = copy.deepcopy(bundle)
            corrupt["comparison_summary"]["selected"]["eligible"] = True
            with self.assertRaisesRegex(ValueError, "ordinary financial qualification"):
                report_validation.report_semantics(corrupt, text)

    def test_calibrated_source_ordinary_has_distinct_verified_qualification(self):
        with tempfile.TemporaryDirectory(prefix="investment-risk-report-") as directory:
            bundle = source_bundle(Artifacts(Path(directory)), True)
            text = report.render(bundle)
            self.assertEqual(bundle["comparison_summary"]["decision_status"], "feasible_selected")
            self.assertIn("在已验证范围内满足风险预算（within_verified_budget）", text)
            self.assertNotIn("风险预算尚未恢复（risk_not_restored）", text)
            self.assertEqual(verify.verify_report(bundle, text)["status"], "passed")
            with self.assertRaisesRegex(ValueError, "verified risk scope|verified risk|verified risk budget"):
                report_validation.report_semantics(bundle, text.replace(
                    "在已验证范围内满足风险预算（within_verified_budget）", "普通可行"))


if __name__ == "__main__":
    unittest.main()
