"""Canonical source-model diagnostics; synthetic observations are explicit."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))

import industry_model
import report
import report_validation
from contracts import canonical_bytes
from test_industry_model import archive, policy, origins


def rendered(calculation, news=None):
    summary = report.summarize(calculation, {"spec": {"planning": {"primary_horizon_days": 3}}})
    bundle = {"decision_at": "2028-03-05T12:00:00+08:00", "account_id": "main",
        "account_recorded_at": None, "market_price_dates": {}, "status": "conditional_research",
        "news": news or {}, "orders": {"orders": [], "waiting": [], "funding_options": []},
        "action_analysis": {"actions": []}, "comparison_summary": summary,
        "qualification": {"reason": "synthetic source diagnostics only"},
        "decision_id": "fixture", "bundle_hash": "a"*64}
    canonical_bytes(bundle)
    return summary, report.render(bundle)


class IndustryReportTests(unittest.TestCase):
    def test_source_instants_are_beijing_and_date_only_precision_is_preserved(self):
        news = {"claims": [{"text": "synthetic clock fixture", "kind": "fact", "url": "https://example.test/original",
            "published_at": {"precision": "timestamp", "value": "2026-10-08T23:30:00-04:00", "timezone": "America/New_York"},
            "event_at": "2026-10-08"},
            {"text": "synthetic date-only fixture", "kind": "fact", "url": "https://example.test/date-only",
             "published_at": {"precision": "date", "value": "2026-10-08", "timezone": "America/New_York"},
             "event_at": None}]}
        original = copy.deepcopy(news)
        _, text = rendered({}, news)
        self.assertIn("2026-10-09T11:30:00+08:00（北京时间）", text)
        self.assertIn("2026-10-08（原文仅日期，来源时区：America/New_York）", text)
        self.assertNotIn("2026-10-08T00:00:00", text)
        self.assertEqual(news, original)

    def test_summer_and_winter_offsets_are_not_a_fixed_hour_difference(self):
        summer = {"precision": "timestamp", "value": "2026-07-01T09:00:00-04:00", "timezone": "America/New_York"}
        winter = {"precision": "timestamp", "value": "2026-12-01T09:00:00-05:00", "timezone": "America/New_York"}
        self.assertEqual(report._beijing_time(summer), "2026-07-01T21:00:00+08:00（北京时间）")
        self.assertEqual(report._beijing_time(winter), "2026-12-01T22:00:00+08:00（北京时间）")

    def test_selected_policy_lower_bound_is_rendered_without_a_guard_copy(self):
        for lower_bound in (123.456, 0.0):
            with self.subTest(lower_bound=lower_bound):
                policy = {"id": "synthetic-policy", "expected_net_return": 0.01,
                    "cvar_loss_fraction": 0.02, "eligible": True,
                    "trade_guard": {"status": "passed", "reasons": []},
                    "point": {"fees": 2.0}, "expected_profit": 125.456,
                    "conditional_lower_bound": lower_bound, "current_projection": {}}
                summary, text = rendered({"mpc": {"selected_policy": policy}})
                self.assertEqual(summary["selected"]["conditional_lower_bound"], lower_bound)
                self.assertNotIn("conditional_lower_bound", summary["current_action_guard"])
                self.assertIn(f"后为 {lower_bound:.2f} 元", text)
                self.assertIn("不是未来盈利置信区间", text)

    def test_missing_selected_lower_bound_is_not_invented_from_guard(self):
        policy = {"id": "synthetic-policy", "expected_net_return": 0.01,
            "cvar_loss_fraction": 0.02, "eligible": True,
            "trade_guard": {"status": "needs_calibration", "conditional_lower_bound": 999.0},
            "point": {"fees": 2.0}, "expected_profit": 125.0,
            "conditional_lower_bound": None, "current_projection": {}}
        _, text = rendered({"mpc": {"selected_policy": policy}})
        self.assertNotIn("后为", text)
        self.assertNotIn("999.00 元", text)

    def test_source_proxy_identity_is_shown_without_an_industry_direction_or_EN(self):
        class Sources:
            def read_json(self, reference):
                return {"record": {"data_ref": "data"}, "data": {"sectors": [{
                    "sector_id": "医药研究标记", "benchmark_id": "declared-proxy", "index_code": "000300",
                    "index_name": "沪深300", "benchmark_role": "analyst_proxy",
                    "sector_relation_scope": "analyst_proxy_only_no_sector_admission",
                    "sector_relation": {"status": "partial"}}]}}[reference]
        actions = report.explain_actions({"context": None, "industry_record_ref": "record"}, Sources())
        bundle = {"decision_at": "2028-03-05T12:00:00+08:00", "account_id": "main",
            "account_recorded_at": None, "market_price_dates": {}, "status": "partial", "news": {},
            "orders": {"orders": [], "waiting": [], "funding_options": []}, "action_analysis": actions,
            "qualification": {"reason": "synthetic report binding only"}, "decision_id": "fixture", "bundle_hash": "a"*64}
        text = report.render(bundle)
        self.assertIn("沪深300（000300）", text)
        self.assertIn("analyst_proxy_only_no_sector_admission", text)
        self.assertIn("该代理仅保存原始行情", text)
        self.assertNotIn("两项对数价格预测", text)

    def test_report_uses_latest_actual_source_model_and_zero_constant_target_coefficients(self):
        data = archive(constant=True)
        industry = industry_model.build_forward(data, origins(data), policy(), 3, "12:00:00")
        before = copy.deepcopy(industry)
        summary, text = rendered({"industry_forecast": industry, "status": "research_ready"})
        current = summary["industry_diagnostics"]["current_forecasts"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["decision_date"], "2028-03-05")
        self.assertTrue(all(value == 0 for row in current[0]["model"]["coefficients"] for value in row))
        self.assertIn("alpha=0.01", text)
        self.assertIn("price-2028-03-05", text)
        self.assertIn("因果与未来收益均未验证", text)
        self.assertEqual(current[0]["asset_domain"], industry["forecasts"][-1]["asset_domain"])
        self.assertIn("两项对数价格比预测（无量纲）", text)
        report_validation.report_semantics({"orders": {"orders": []}, "comparison_summary": summary}, text)
        self.assertNotIn("行业评分", text)
        self.assertEqual(industry, before)

    def test_level_change_report_retains_source_units_and_rejects_log_price_label(self):
        for role in ("rate", "credit"):
            with self.subTest(role=role):
                data = archive(constant=True)
                sector = data["sectors"][0]
                sector["asset_domain"].update(kind="bond", role=role)
                target = sector["asset_domain"]["return_target"]
                target.update(transform="level_change", source_unit="percent", canonical_unit="decimal_rate",
                              economic_meaning="synthetic source rate/spread level change",
                              quote="synthetic percent level; engineering report case only")
                sector["return_definition"] = "level_change"
                samples, _ = industry_model.build_samples(data, origins(data), 3, "12:00:00")
                fitted = industry_model.fit_at(samples, samples[-1], policy(), "12:00:00")
                self.assertEqual(fitted["status"], "fitted")
                industry = {"status": "research_ready", "decision_date": fitted["decision_date"],
                            "horizon_days": 3, "forecasts": [fitted], "unavailable": []}
                summary, text = rendered({"industry_forecast": industry})
                current = summary["industry_diagnostics"]["current_forecasts"][0]
                self.assertEqual(current["asset_domain"], fitted["asset_domain"])
                self.assertIn("两项水平变化预测（decimal_rate）", text)
                self.assertIn("原始观测单位 percent", text)
                self.assertIn("规范观测单位 decimal_rate", text)
                self.assertNotIn("对数价格比预测", text)
                bundle = {"orders": {"orders": []}, "comparison_summary": summary}
                report_validation.report_semantics(bundle, text)
                for changed in (text.replace("水平变化预测（decimal_rate）", "对数价格比预测（无量纲）"),
                                text.replace("规范观测单位 decimal_rate", "规范观测单位 percent")):
                    with self.assertRaisesRegex(ValueError, "transform/unit"):
                        report_validation.report_semantics(bundle, changed)

    def test_missing_target_metadata_is_explicitly_unverified_without_log_default(self):
        data = archive(constant=True)
        industry = industry_model.build_forward(data, origins(data), policy(), 3, "12:00:00")
        for forecast in industry["forecasts"]:
            forecast.pop("asset_domain")
        summary, text = rendered({"industry_forecast": industry})
        self.assertIn("预测目标变换或单位待核", text)
        self.assertNotIn("对数价格比预测", text)
        self.assertNotIn("水平变化预测", text)
        bundle = {"orders": {"orders": []}, "comparison_summary": summary}
        report_validation.report_semantics(bundle, text)
        with self.assertRaisesRegex(ValueError, "explicitly unverified"):
            report_validation.report_semantics(bundle, text.replace("预测目标变换或单位待核", "两项对数价格比预测（无量纲）"))

    def test_missing_archived_news_prints_partial_actions_without_made_up_prediction(self):
        data = archive()
        data["sectors"][0]["features"] = []
        industry = industry_model.build_forward(data, origins(data), policy(), 3, "12:00:00")
        summary, text = rendered({"industry_forecast": industry, "status": "partial",
            "required_actions": [{"action": "supply_archived_news", "reason": "actual_first_seen_missing"}]})
        self.assertEqual(summary["industry_diagnostics"]["current_forecasts"], [])
        self.assertIn("insufficient_evidence", text)
        self.assertIn("supply_archived_PIT_economic_news_and_benchmark_observations", text)
        self.assertIn("actual_first_seen_missing", text)
        self.assertNotIn("两项对数价格预测", text)


if __name__ == "__main__":
    unittest.main()
