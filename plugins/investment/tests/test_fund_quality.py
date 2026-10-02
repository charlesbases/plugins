"""Synthetic raw transport tests mathematical/source contracts, not live alpha."""
import copy
import datetime as dt
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import fund_quality
import fund_screen
import fund_universe
import research_data
import source_documents
import source_fetch
from artifacts import Artifacts
from contracts import fingerprint, instant, utc_now
from state_store import Store
from test_industry_data import transport


class FundQualityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name), "synthetic-quality")
        self.store.begin("quality", {"synthetic": True})
        self.enterContext(self.store.lease("quality"))
        self.artifacts = Artifacts(self.store.base)
        self.days = [(dt.date(2024, 1, 1)+dt.timedelta(days=i)).isoformat() for i in range(161)]
        self.market = [.001+.005*math.sin(i*.37) for i in range(160)]
        self.rf = [.0001 for _ in self.market]
        self.policy = {**fund_quality.DEFAULT_POLICY, "min_observations": 30, "lookback_sessions": 160,
                       "bootstrap_resamples": 199, "block_length": 5}

    def doc(self, html, suffix):
        url = "https://www.cmfchina.com/fundarticle/"+suffix
        capture = transport(html.encode(), url, "issuer_cmfchina", "text/html")
        with patch.object(source_fetch, "fetch", return_value=capture):
            reference = source_documents.capture_document(url, "issuer_cmfchina", self.artifacts, store=self.store)
        return reference, source_documents.read_extracted_document(reference, self.artifacts)

    def series(self, rates, name, definition="total_return", role="equity_market", scope="equity", missing_vintage=False):
        metadata = (f"序列标识:{name};币种:CNY;对冲口径:unhedged;收益口径:{definition};"
                    f"频率:daily;资产类型:{scope};因子作用:{role}")
        head = ["日期", "上一观察日", "收益率"] + ([] if missing_vintage else ["可得时间"])
        rows = []
        for index, rate in enumerate(rates):
            cells = [self.days[index+1], self.days[index], str(rate)]
            if not missing_vintage:
                cells.append(self.days[index+1]+"T16:00:00+08:00")
            rows.append("<tr>"+"".join("<td>"+value+"</td>" for value in cells)+"</tr>")
        html = "<p>"+metadata+"</p><table><tr>"+"".join("<th>"+value+"</th>" for value in head)+"</tr>"+"".join(rows)+"</table>"
        ref, _ = self.doc(html, name)
        return {"document_ref": ref, "metadata_locator": "html/block/0", "table_locator": "html/table/0"}

    def source_data(self, mode="active", rates=None, appointments=None, disclosure_as_of=None, disclosed_date_present=True):
        rates = rates if rates is not None else [rf+.0003+1.2*(market-rf) for market, rf in zip(self.market, self.rf)]
        name = "甲经理"
        appointments = appointments or [(name, self.days[0], "至今")]
        names = "、".join(row[0] for row in appointments if row[2] == "至今")
        scope = "基金代码:123456;资产类型:equity;管理方式:"+mode+";币种:CNY;对冲口径:unhedged;跟踪标的:synthetic-market"
        if disclosed_date_present:
            # This synthetic original is captured now; default current-team
            # tests require an explicit disclosure covering their decision day.
            scope += ";任期披露截至:"+(disclosure_as_of or instant(utc_now()).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
        # Controlled engineering source declares completeness and endpoints.
        scope += ";经理覆盖范围:完整期间;经理覆盖起始:"+self.days[0]+";经理日期语义:任职日生效、离任日含当日"
        html = ("<p>"+scope+"</p><table><tr><th>基金经理</th><th>任职日期</th><th>离任日期</th></tr>"+
                "".join("<tr>"+"".join("<td>"+cell+"</td>" for cell in row)+"</tr>" for row in appointments)+"</table>")
        ref, document = self.doc(html, "scope-"+mode)
        identity = {"code": "123456", "currency": "CNY", "asset_class": "股票型" if mode == "active" else "股票型指数",
                    "manager_names": names, "manager_tenures": None, "company_restrictions": None}
        mapping = {"code": "123456", "source_id": "issuer_cmfchina", "url": document["url"], "role": "manager",
                   "locators": [row["locator"] for row in document["blocks"]]}
        identity = fund_universe._apply_issuer(ref, mapping, identity, self.artifacts)
        values, nav = [1.], []
        for rate in rates:
            values.append(values[-1]*(1+rate))
        for day, value in zip(self.days, values):
            stamp = int(dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), ZoneInfo("Asia/Shanghai")).timestamp()*1000)
            nav.append({"x": stamp, "y": value, "unitMoney": ""})
        text = ("var fS_code=\"123456\";var fS_name=\"Synthetic\";var Data_netWorthTrend="+json.dumps(nav)+
                ";var Data_ACWorthTrend="+json.dumps([[row["x"], row["y"]] for row in nav])+";")
        url = "https://fund.eastmoney.com/pingzhongdata/123456.js"
        with patch.object(source_fetch, "fetch", return_value=transport(text.encode(), url, "eastmoney_nav", "application/javascript")):
            captured = fund_universe._capture(url, "eastmoney_nav", self.artifacts, 1, store=self.store)
        rebuilt, _ = research_data.parse_nav(text, "123456", dt.date.fromisoformat(self.days[0]), dt.date.fromisoformat(self.days[-1]), {})
        data = {"nav": {"123456": rebuilt}, "features": [], "code_info": {"123456": identity}}
        contract = {"code": "123456", "mode": mode, "asset_scope": "equity", "hedge_policy": "unhedged",
                    "nav_source_ref": self.artifacts.put_json(captured),
                    "fund_scope_source": {"document_ref": ref, "locator": "html/block/0"}}
        if mode == "active":
            contract.update(model="equity_market", factor_series=[{"role": "equity_market", "source": self.series(self.market, "synthetic-market")}],
                            risk_free_source=self.series(self.rf, "synthetic-rf", "risk_free_return", "risk_free", "risk_free"))
        else:
            contract.update(benchmark_source=self.series(self.market, "synthetic-market"),
                            tracking_source={"document_ref": ref, "locator": "html/block/0"})
        return data, self.artifacts.put_json(data), contract

    def evaluate(self, data_ref, contracts):
        return fund_quality.evaluate(data_ref, contracts, self.artifacts, utc_now(), self.policy)

    def test_active_source_rebuilt_capm_coefficients_not_future_skill(self):
        _, reference, contract = self.source_data()
        output = self.evaluate(reference, [contract])
        product = output["products"]["123456"]
        stats = product["statistics"]["manager_regimes"][0]
        self.assertEqual(product["readiness"], "descriptive_estimate")
        self.assertAlmostEqual(stats["alpha_daily"], .0003, places=12)
        self.assertAlmostEqual(stats["betas"]["equity_market"], 1.2, places=11)
        self.assertLess(stats["residual_daily_std"], 1e-14)
        self.assertFalse(product["manager_skill_proven"])
        self.assertFalse(product["operating_fee_subtracted_again"])
        self.assertEqual(output["selection_role"], "evaluation_only_no_EN_return_addition")

    def test_actual_joint_manager_regimes_are_not_individual_attribution(self):
        appointments = [("甲经理", self.days[0], "至今"), ("乙经理", self.days[80], "至今")]
        _, reference, contract = self.source_data(appointments=appointments)
        output = self.evaluate(reference, [contract])
        stats = output["products"]["123456"]["statistics"]["manager_regimes"]
        self.assertEqual([row["managers"] for row in stats], [["甲经理"], ["乙经理", "甲经理"]])
        self.assertEqual([row["observations"] for row in stats], [78, 79])
        self.assertEqual(output["alpha_family_size"], 2)

    def test_reappearing_team_does_not_merge_across_management_change(self):
        appointments = [("甲经理", self.days[0], "至今"), ("乙经理", self.days[50], self.days[109])]
        _, reference, contract = self.source_data(appointments=appointments)
        output = self.evaluate(reference, [contract])
        stats = output["products"]["123456"]["statistics"]["manager_regimes"]
        self.assertEqual([row["observations"] for row in stats], [48, 57, 50])
        self.assertEqual(stats[0]["managers"], stats[-1]["managers"])
        self.assertLess(stats[0]["until_date"], stats[-1]["from_date"])

    def test_missing_manager_or_factor_vintage_is_unknown_not_bad_fund(self):
        data, reference, contract = self.source_data()
        changed = copy.deepcopy(data)
        changed["code_info"]["123456"]["manager_tenures"] = None
        product = self.evaluate(self.artifacts.put_json(changed), [contract])["products"]["123456"]
        self.assertEqual(product["readiness"], "unknown")
        self.assertIn("official_manager_tenure_missing", str(product["required_actions"]))
        contract["factor_series"][0]["source"] = self.series(self.market, "no-vintage", missing_vintage=True)
        product = self.evaluate(reference, [contract])["products"]["123456"]
        self.assertEqual(product["readiness"], "unknown")
        self.assertIn("可得时间", str(product["required_actions"]))

    def test_forged_nav_and_resealed_source_text_cannot_pass(self):
        data, reference, contract = self.source_data()
        changed = copy.deepcopy(data)
        changed["nav"]["123456"][10]["nav"] *= 1.01
        with self.assertRaisesRegex(ValueError, "NAV or distributions differ"):
            self.evaluate(self.artifacts.put_json(changed), [contract])
        source = contract["fund_scope_source"]
        document = self.artifacts.read_json(source["document_ref"])
        document["blocks"][0]["text"] = document["blocks"][0]["text"].replace("active", "passive")
        document["text_sha256"] = fingerprint(document["blocks"])
        # Reseal the current semantic identity so rejection must re-extract the raw source.
        document["document_id"] = fingerprint({key: value for key, value in document.items() if key not in ("document_id", "raw_ref", "registry_ref")})
        source["document_ref"] = self.artifacts.put_json(document)
        with self.assertRaisesRegex(ValueError, "extraction differs"):
            self.evaluate(reference, [contract])

    def test_passive_identical_total_returns_have_zero_td_and_te(self):
        _, reference, contract = self.source_data("passive", self.market)
        product = self.evaluate(reference, [contract])["products"]["123456"]
        stats = product["statistics"]["tracking_statistics"]
        self.assertAlmostEqual(stats["tracking_difference_period"], 0, places=12)
        self.assertAlmostEqual(stats["tracking_error_annual_long_run"], 0, places=12)
        self.assertIn("Bartlett_HAC", product["statistics"]["annualization_assumption"])

    def test_passive_price_return_and_wrong_hedge_are_unknown(self):
        _, reference, contract = self.source_data("passive", self.market)
        contract["benchmark_source"] = self.series(self.market, "synthetic-market", "price_return")
        self.assertEqual(self.evaluate(reference, [contract])["products"]["123456"]["readiness"], "unknown")
        contract["hedge_policy"] = "hedged"
        product = self.evaluate(reference, [contract])["products"]["123456"]
        self.assertEqual(product["readiness"], "unknown")
        self.assertIn("scope", str(product["required_actions"]))

    def test_reproduction_is_deterministic_and_changed_statistics_fail(self):
        _, reference, contract = self.source_data()
        at = utc_now()
        first = fund_quality.evaluate(reference, [contract], self.artifacts, at, self.policy)
        second = fund_quality.evaluate(reference, [contract], self.artifacts, at, self.policy)
        self.assertEqual(first, second)
        self.assertEqual(fund_quality.validate(first, reference, [contract], self.artifacts, at, self.policy)["status"], "passed")
        changed = copy.deepcopy(first)
        changed["products"]["123456"]["statistics"]["manager_regimes"][0]["alpha_daily"] += .001
        with self.assertRaisesRegex(ValueError, "independently rebuilt"):
            fund_quality.validate(changed, reference, [contract], self.artifacts, at, self.policy)

    def test_holm_step_down_has_independent_known_adjusted_values(self):
        products = {str(i): {"statistics": {"manager_regimes": [{"status": "estimated", "alpha_bootstrap_p_approximate": p}]}}
                    for i, p in enumerate([.01, .03, .04])}
        self.assertEqual(fund_quality._holm(products, .95), 3)
        rows = [product["statistics"]["manager_regimes"][0] for product in products.values()]
        self.assertEqual([round(row["alpha_holm_p_approximate"], 6) for row in rows], [.03, .06, .06])
        self.assertEqual([row["alpha_zero_rejected_in_declared_family"] for row in rows], [True, False, False])

    def test_no_contract_never_invents_quality_or_rejects_missing_manager(self):
        _, reference, _ = self.source_data()
        product = self.evaluate(reference, [])["products"]["123456"]
        self.assertEqual(product["readiness"], "unknown")
        self.assertEqual(product["statistics"], {})
        self.assertFalse(product["manager_skill_proven"])

    def test_screen_annotation_preserves_purchase_eligibility_and_amounts(self):
        _, reference, _ = self.source_data()
        prepared = fund_quality.prepare_source_quality(reference, [], self.artifacts, utc_now(), self.policy)
        quality, admission = prepared["current_evaluation"], prepared["admission"]
        selection = {"schema_version": 4, "eligible_buy_codes": ["123456"], "amounts": {"123456": "1000.00"},
                     "per_code": {"123456": {"eligible": True, "due_diligence": {"manager_skill_estimated": False}}}}
        selection["selection_hash"] = fingerprint(selection)
        original = copy.deepcopy(selection)
        enriched = fund_screen.attach_quality(selection, quality, admission)
        self.assertEqual(enriched["eligible_buy_codes"], ["123456"])
        self.assertEqual(enriched["amounts"], {"123456": "1000.00"})
        self.assertEqual(selection, original)
        diligence = enriched["per_code"]["123456"]["due_diligence"]
        self.assertEqual(diligence["quality_readiness"], "unknown")
        self.assertFalse(diligence["manager_skill_proven"])
        changed = copy.deepcopy(quality)
        changed["products"]["123456"]["readiness"] = "descriptive_estimate"
        with self.assertRaisesRegex(ValueError, "annotation input changed"):
            fund_screen.attach_quality(selection, changed, admission)

    def test_resealed_quality_description_is_rejected_by_source_and_annotation_validators(self):
        import stage_validation, verify
        _, reference, _ = self.source_data()
        prepared = fund_quality.prepare_source_quality(reference, [], self.artifacts, utc_now(), self.policy)
        quality, admission = prepared["current_evaluation"], prepared["admission"]
        inputs = {"data_ref":reference,"contracts":[],"decision_at":quality["decision_at"],"policy":self.policy}
        inputs.update(source_bundle_ref=self.artifacts.put_json(prepared), model_data_ref=reference)
        bundle = {"quality_ref":self.artifacts.put_json(quality),
                  "quality_inputs_ref":self.artifacts.put_json(inputs)}
        verify._verify_source_families(bundle,self.artifacts)
        selection = {"schema_version":4,"eligible_buy_codes":["123456"],
                     "per_code":{"123456":{"eligible":True,"due_diligence":{}}}}
        selection["selection_hash"] = fingerprint(selection)
        annotation_inputs = {"selection": selection, "quality": quality, "admission": admission, "quality_inputs": inputs}
        annotated = fund_screen.attach_quality(selection,quality,admission)
        checked = stage_validation._quality_annotation(annotated,annotation_inputs,
                                                        self.store,self.artifacts)
        self.assertEqual(checked["status"],"passed")
        for field,value in (("manager_skill_proven",True),("quality_hash","0"*64),
                            ("quality_required_actions",[])):
            changed = copy.deepcopy(annotated)
            changed["per_code"]["123456"]["due_diligence"][field] = value
            changed["selection_hash"] = fingerprint({key:value for key,value in changed.items() if key!="selection_hash"})
            with self.assertRaisesRegex(stage_validation.ValidationError,"Verified source quality admission and annotation"):
                stage_validation._quality_annotation(changed,annotation_inputs,
                                                     self.store,self.artifacts)
        changed_quality = copy.deepcopy(quality)
        changed_quality["products"]["123456"]["manager_skill_proven"] = True
        changed_quality["quality_hash"] = fingerprint({key:value for key,value in changed_quality.items() if key!="quality_hash"})
        changed_bundle = {**bundle,"quality_ref":self.artifacts.put_json(changed_quality)}
        with self.assertRaisesRegex(ValueError,"Displayed quality differs from original source-vintage bundle"):
            verify._verify_source_families(changed_bundle,self.artifacts)
        forged = copy.deepcopy(prepared)
        forged["current_evaluation"] = changed_quality
        forged["quality_bundle_hash"] = fingerprint({key:value for key,value in forged.items() if key!="quality_bundle_hash"})
        forged_inputs = {**inputs, "source_bundle_ref": self.artifacts.put_json(forged)}
        with self.assertRaisesRegex(ValueError, "source-vintage panel differs from original evidence"):
            verify._verify_source_families({**changed_bundle, "quality_inputs_ref": self.artifacts.put_json(forged_inputs)}, self.artifacts)

    def test_source_vintages_bind_latest_identity_without_backfilling_old_origins(self):
        old_data, _, old = self.source_data()
        old["identity_ref"] = self.artifacts.put_json(old_data["code_info"]["123456"])
        data, reference, new = self.source_data(appointments=[("乙经理", self.days[0], "至今")])
        new["identity_ref"] = self.artifacts.put_json(data["code_info"]["123456"])
        # Genuine original captures are current; neither is historical evidence for 2024.
        contracts = [{"source_vintages": [new, old]}]
        at = utc_now()
        prepared = fund_quality.prepare_source_quality(reference, contracts, self.artifacts, at, self.policy)
        current = prepared["current_evaluation"]["products"]["123456"]
        self.assertEqual(current["statistics"]["manager_regimes"][-1]["managers"], ["乙经理"])
        self.assertEqual(len(prepared["feature_panel"]), 1)
        self.assertEqual(prepared["feature_panel"][0]["decision_at"], at)
        self.assertEqual(prepared["feature_panel"][0]["team_regime"], ["乙经理"])
        self.assertTrue(prepared["source_gaps"])
        self.assertEqual(fund_quality.validate_source_quality(prepared, reference, contracts, self.artifacts, at, self.policy)["status"], "passed")
        augmented = fund_quality.bind_quality_features(data, prepared)
        self.assertEqual(set(augmented)-set(data), {"quality_source_bundle"})

    def test_original_company_operating_restriction_is_hard_admission_with_unknown_quality(self):
        data, _, _ = self.source_data()
        ref, document = self.doc("<p>基金代码:123456</p><p>申购限制:禁止新增申购</p>", "operating-restriction")
        mapping = {"code": "123456", "source_id": "issuer_cmfchina", "url": document["url"],
                   "role": "company_restriction", "locators": [row["locator"] for row in document["blocks"]]}
        identity = fund_universe._apply_issuer(ref, mapping, data["code_info"]["123456"], self.artifacts)
        data["code_info"]["123456"] = identity
        reference = self.artifacts.put_json(data)
        prepared = fund_quality.prepare_source_quality(reference, [], self.artifacts, utc_now(), self.policy)
        self.assertEqual(prepared["current_evaluation"]["products"]["123456"]["readiness"], "unknown")
        self.assertEqual(prepared["admission"]["excluded_new_codes"], ["123456"])
        selection = {"schema_version": 4, "eligible_buy_codes": ["123456"],
                     "per_code": {"123456": {"eligible": True, "reasons": [], "due_diligence": {}}}}
        selection["selection_hash"] = fingerprint(selection)
        annotated = fund_screen.attach_quality(selection, prepared["current_evaluation"], prepared["admission"])
        self.assertEqual(annotated["eligible_buy_codes"], [])
        self.assertFalse(annotated["per_code"]["123456"]["eligible"])
        self.assertTrue(identity["company_restrictions"][0]["evidence_refs"])

    def test_new_joint_team_without_observations_cannot_inherit_predecessor_alpha(self):
        _, reference, contract = self.source_data(appointments=[("甲经理", self.days[0], "至今"),
                                                               ("乙经理", self.days[-5], "至今")])
        prepared = fund_quality.prepare_source_quality(reference, [contract], self.artifacts, utc_now(), self.policy)
        regimes = prepared["current_evaluation"]["products"]["123456"]["statistics"]["manager_regimes"]
        self.assertEqual(regimes[0]["status"], "estimated")
        self.assertNotEqual(regimes[-1]["status"], "estimated")
        self.assertEqual(prepared["feature_panel"], [])
        self.assertTrue(prepared["source_gaps"])
        self.assertEqual(prepared["admission"]["excluded_new_codes"], [])

    def test_open_manager_tenure_stops_at_original_report_as_of_date(self):
        data, reference, contract = self.source_data(disclosure_as_of=self.days[30])
        facts = fund_quality._manager_facts(data["code_info"]["123456"], self.artifacts, utc_now())
        self.assertEqual(facts["appointments"][0]["disclosed_through_date"], self.days[30])
        product = self.evaluate(reference, [contract])["products"]["123456"]
        self.assertEqual(product["readiness"], "unknown")
        self.assertIn("no_qualified_manager_regime_statistics", str(product["required_actions"]))

    def test_manager_without_report_as_of_date_remains_unknown(self):
        _, reference, contract = self.source_data(disclosed_date_present=False)
        product = self.evaluate(reference, [contract])["products"]["123456"]
        self.assertEqual(product["readiness"], "unknown")
        self.assertIn("任期披露截至", str(product["required_actions"]))


if __name__ == "__main__":
    unittest.main()
