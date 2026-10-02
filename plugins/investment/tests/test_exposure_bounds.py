"""Exact source/domain counterexamples, not economic effectiveness claims."""
import copy
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
from artifacts import Artifacts
from contracts import fingerprint, utc_now
from state_store import Store
import exposure_bounds
import source_documents
import source_fetch
from test_industry_data import transport


class ExposureBoundsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name), "synthetic-exposure")
        self.store.begin("source", {"synthetic": True})
        self.enterContext(self.store.lease("source"))
        self.artifacts = Artifacts(self.store.base)
        self.identities = {"123456": {"code": "123456", "legal_name": "Synthetic Fund", "company_name": "Synthetic Company"}}

    def original(self, text, subject="基金代码:123456;基金名称:Synthetic Fund"):
        html = "<p>"+subject+"</p><p>"+text+"</p>"
        url = "https://www.cmfchina.com/fundarticle/synthetic-source"
        with patch.object(source_fetch, "fetch", return_value=transport(html.encode(), url, "issuer_cmfchina", "text/html")):
            reference = source_documents.capture_document(url, "issuer_cmfchina", self.artifacts, store=self.store)
        return {"code": "123456", "document_ref": reference, "subject_locators": ["html/block/0"], "constraint_locators": ["html/block/1"]}

    def run_bounds(self, contracts):
        at = utc_now()
        value = exposure_bounds.assemble(contracts, self.identities, self.artifacts, at)
        self.assertEqual(exposure_bounds.validate(value, contracts, self.identities, self.artifacts, at)["status"], "passed")
        return value

    def test_missing_current_source_is_unbounded_not_one(self):
        bound = self.run_bounds([])["products"]["123456"]["default_bound"]
        self.assertEqual(bound["lower"], "0")
        self.assertIsNone(bound["upper"])

    def test_actual_contract_clause_grammar_preserves_gross_leverage_and_quote(self):
        contract = self.original("(15)本基金资产总值不超过基金资产净值的 140%;")
        bound = self.run_bounds([contract])["products"]["123456"]["default_bound"]
        self.assertEqual(Decimal(bound["upper"]), Decimal("1.4"))
        self.assertEqual(bound["evidence"][0]["quote"], "(15)本基金资产总值不超过基金资产净值的 140%;")

    def test_historical_weight_or_non_NAV_or_derivative_is_not_asset_cap(self):
        for text in ("报告期末本基金总资产占净资产140%。", "本基金总资产不超过基金资产的140%。",
                     "本基金股指期货合约名义价值不超过基金净资产的140%。"):
            with self.subTest(text=text):
                bound = self.run_bounds([self.original(text)])["products"]["123456"]["default_bound"]
                self.assertIsNone(bound["upper"])

    def test_another_product_cannot_supply_current_fund_bound(self):
        contract = self.original("本基金资产总值不超过基金净资产的140%。", "基金代码:654321;基金名称:Different Fund")
        value = self.run_bounds([contract])
        self.assertIsNone(value["products"]["123456"]["default_bound"]["upper"])
        self.assertIn("same_primary", str(value["products"]["123456"]["required_actions"]))

    def test_exact_legal_title_company_prefix_without_share_suffix_removal(self):
        contract = self.original("本基金资产总值不超过基金净资产的140%。", "Synthetic Company Synthetic Fund 基金合同")
        # HTML paragraph title is a supported original structural block.
        value = self.run_bounds([contract])
        self.assertEqual(value["products"]["123456"]["default_bound"]["upper"], "1.4")
        self.identities["123456"]["legal_name"] = "Synthetic Fund C"
        self.assertIsNone(self.run_bounds([contract])["products"]["123456"]["default_bound"]["upper"])

    def test_source_Nav_sector_interval_can_exceed_one_but_non_cash_basis_cannot(self):
        self.assertEqual(exposure_bounds._sector_clause("投资于医药行业的股票占基金资产净值的比例不低于80%且不超过130%。", "医药"),
                         (Decimal(".8"), Decimal("1.3")))
        self.assertIsNone(exposure_bounds._sector_clause("投资于医药行业的股票占非现金基金资产的比例不低于80%。", "医药"))
        self.assertIsNone(exposure_bounds._sector_clause("可能投资于医药行业的股票占基金资产净值的比例不超过80%。", "医药"))

    def test_changed_generated_bound_and_resealed_source_are_rejected(self):
        contract = self.original("本基金资产总值不超过基金净资产的140%。")
        at = utc_now()
        value = exposure_bounds.assemble([contract], self.identities, self.artifacts, at)
        altered = copy.deepcopy(value)
        altered["products"]["123456"]["default_bound"]["upper"] = "1"
        with self.assertRaisesRegex(ValueError, "original-source reconstruction"):
            exposure_bounds.validate(altered, [contract], self.identities, self.artifacts, at)
        document = self.artifacts.read_json(contract["document_ref"])
        document["blocks"][1]["text"] = "本基金资产总值不超过基金净资产的100%。"
        document["text_sha256"] = fingerprint(document["blocks"])
        # Physical refs are excluded from semantic identity; their bytes are still verified.
        document["document_id"] = fingerprint({key: value for key, value in document.items() if key not in ("document_id", "raw_ref", "registry_ref")})
        contract["document_ref"] = self.artifacts.put_json(document)
        with self.assertRaisesRegex(ValueError, "extraction differs"):
            self.run_bounds([contract])


if __name__ == "__main__":
    unittest.main()
