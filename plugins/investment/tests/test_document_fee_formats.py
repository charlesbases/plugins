"""Original issuer/TT material and independently calculated fee boundaries."""
import datetime as dt
import hashlib
import json
import sys
import tempfile
import unittest
import re
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import fee_contract
import fee_sources
import source_documents
import source_fetch
from artifacts import Artifacts
from contracts import fingerprint, instant


class OriginalDocumentFeeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = Path(__file__).parent / "fixtures/native_dealing_sources"
        cls.registry = json.loads((cls.fixture / "registry.json").read_text(encoding="utf-8"))
        cls.index = json.loads((cls.fixture / "index.json").read_text(encoding="utf-8"))
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.artifacts = Artifacts(Path(temp.name))
        cls.references, cls.documents = {}, {}
        for name, item in cls.index.items():
            raw = (cls.fixture / item["raw_file"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["raw_sha256"]:
                raise AssertionError("Original source fixture changed")
            capture = item["capture"]
            registry = json.loads((cls.fixture / item.get("registry_file", "registry.json")).read_text(encoding="utf-8"))
            source_fetch.validate_capture_provenance(capture, registry)
            extracted = source_documents.extract_document(raw, capture["content_type"], capture["encoding"])
            document = {"schema_version": 4, "source_id": item["source_id"], "url": capture["final_url"],
                "capture": capture, "raw_ref": cls.artifacts.put_bytes(raw), "raw_sha256": item["raw_sha256"],
                "media_type": capture["content_type"], "retrieved_at": capture["retrieved_at"], **extracted,
                "status": "extracted", "required_actions": [], "text_sha256": fingerprint(extracted["blocks"]),
                "registry_ref": cls.artifacts.put_json(registry)}
            document["document_id"] = fingerprint({key: value for key, value in document.items() if key not in ("raw_ref", "registry_ref")})
            cls.references[name] = cls.artifacts.put_json(document)
            cls.documents[name] = document
        cls.as_of = (max(instant(item["capture"]["retrieved_at"]) for item in cls.index.values()) + dt.timedelta(seconds=1)).isoformat()
        cls.calendars = []
        for market, prefix in (("SSE", "sse"), ("SZSE", "szse")):
            rules = [block for block in cls.documents[prefix + "_rule"]["blocks"]
                if block["kind"] not in ("table_cell", "pdf_line")
                and "交易日为每周一至周五" in re.sub(r"\s+", "", block["text"]) and "休市" in block["text"]]
            if not rules or len({re.sub(r"\s+", "", row["text"]) for row in rules}) != 1:
                raise AssertionError("Original exchange weekday clause is ambiguous")
            notices = [block for block in cls.documents[prefix + "_annual"]["blocks"]
                if block["kind"] not in ("table_cell", "pdf_line")
                and ("休市" in block["text"] or "2026年" in re.sub(r"\s+", "", block["text"]))]
            cls.calendars.append({"id": market, "binding": {"kind": "exchange_annual_notice", "year": 2026,
                "weekday_rule": {"document_ref": cls.references[prefix + "_rule"], "locator": rules[0]["locator"]},
                "notice_spans": [{"document_ref": cls.references[prefix + "_annual"], "locator": row["locator"]} for row in notices]}})
        cls.results = {issuer: cls.inspect(issuer) for issuer in ("ef", "cmf")}

    @classmethod
    def request(cls, issuer):
        return {"schema_version": 4, "adapter": "document_formats_v2", "code": "110020" if issuer == "ef" else "217018",
            "sources": {"product": cls.references["ef_product" if issuer == "ef" else "cmf_product_pdf"],
                "prospectus": cls.references[issuer + "_prospectus"], "platform": cls.references[issuer + "_platform"],
                "cutoff": cls.references["cutoff"], "calendars": []}}

    @classmethod
    def inspect(cls, issuer):
        return fee_contract.inspect_contract(cls.request(issuer), cls.artifacts, as_of=cls.as_of)

    def test_two_issuers_native_materials_use_canonical_source_scope(self):
        for issuer, code, rate in (("ef", "110020", "0.0012"), ("cmf", "217018", "0.0008")):
            with self.subTest(issuer=issuer):
                result = self.results[issuer]
                self.assertEqual(result["normalized"]["subject"], {"code": code, "share_class": "A", "currency": "CNY", "channel": "TT", "investor_type": "retail"})
                self.assertEqual(result["normalized"]["subscription"]["bands"][0]["fee"]["rate"], rate)
                self.assertEqual(result["normalized"]["subscription"]["bands"][-1]["fee"], {"kind": "fixed", "amount": "1000"})

    def test_unknown_tt_thresholds_are_preserved_and_only_block_partial_sales(self):
        result = self.results["ef"]
        terms = result["normalized"]
        for field in ("minimum_redemption_shares", "minimum_remaining_shares"):
            self.assertIsNone(terms[field])
            self.assertEqual(result["field_statuses"][field]["literal"], "---份")
            self.assertNotIn(field, terms["action_evidence"]["buy"]["missing_fields"])
            self.assertIn(field, terms["action_evidence"]["sell_partial"]["missing_fields"])
        self.assertTrue(terms["full_redemption_allowed"])
        fee_contract.validate_sale("10", "10", terms, acquired_at="2026-09-01T00:00:00+08:00", execution_at=self.as_of)
        with self.assertRaisesRegex(ValueError, "thresholds"):
            fee_contract.validate_sale("9", "10", terms, acquired_at="2026-09-01T00:00:00+08:00", execution_at=self.as_of)

    def test_actual_truncation_modes_are_not_relabelled_as_fee_rounding(self):
        precision = self.results["cmf"]["normalized"]["trade_precision"]
        self.assertEqual(precision["share_rounding"], "down_fund")
        self.assertEqual(precision["redemption_net_rounding"], "down_fund")
        self.assertEqual(precision["fee_rounding"], "unrounded")

    def test_absent_calendar_is_not_filled_from_cny_or_normal_confirmation_lag(self):
        result = self.results["ef"]
        self.assertEqual(result["status"], "needs_research")
        self.assertIsNone(result["normalized"]["execution_calendar"])
        self.assertEqual(result["normalized"]["confirmation"]["lag_days"], 1)
        self.assertEqual(result["normalized"]["settlement"]["lag_days"], 7)
        with self.assertRaises(fee_contract.EvidenceGap):
            fee_contract.normalize_to_terms(result["contract"], self.artifacts, as_of=self.as_of,
                                            contract_ref=self.artifacts.put_json(result["contract"]))

    def test_actual_redemption_days_before_on_after_boundary(self):
        rule = self.results["ef"]["normalized"]["redemption"]
        self.assertEqual([fee_contract.charge("10000", rule, age=Decimal(age)) for age in (6, 7, 8)],
                         [Decimal("150.00"), Decimal("50.00"), Decimal("50.00")])
        self.assertTrue(rule["bands"][0]["maximum_inclusive"])
        self.assertEqual(rule["bands"][1]["minimum"], "7")

    def test_actual_subscription_money_before_on_after_boundary(self):
        terms = self.results["ef"]["normalized"]
        self.assertEqual([fee_contract.quote_entry(cash, terms, price="1", share_step="0.01")["fee"]
                          for cash in ("999999.99", "1000000", "1000000.01")],
                         [Decimal("1198.56"), Decimal("799.36"), Decimal("799.36")])

    def test_obsolete_public_product_adapter_has_no_current_alias(self):
        request = self.request("ef")
        request["adapter"] = "efunds_tt_v1"
        with self.assertRaisesRegex(ValueError, "Unsupported fee inspection adapter"):
            fee_contract.inspect_contract(request, self.artifacts, as_of=self.as_of)

    def test_actual_normalized_buy_term_preserves_unknown_partial_thresholds(self):
        request = self.request("ef")
        request["sources"]["calendars"] = self.calendars
        result = fee_contract.inspect_contract(request, self.artifacts, as_of=self.as_of)
        term = fee_contract.normalize_to_terms(result["contract"], self.artifacts, as_of=self.as_of,
                                               contract_ref=self.artifacts.put_json(result["contract"]))
        self.assertEqual(term["fee_contract"]["subject"]["investor_type"], "retail")
        self.assertEqual(term["investor_scope"], "retail")
        self.assertEqual(term["fee_contract"]["action_evidence"]["buy"]["status"], "source_supported")
        self.assertEqual(term["fee_contract"]["action_evidence"]["sell_full"]["status"], "source_supported")
        self.assertEqual(term["fee_contract"]["action_evidence"]["sell_partial"]["status"], "needs_research")
        self.assertIsNone(term["fee_contract"]["minimum_redemption_shares"])
        self.assertNotIn("2026-10-07", term["fee_contract"]["execution_calendar"]["open_dates"])
        self.assertIn("2026-10-08", term["fee_contract"]["execution_calendar"]["open_dates"])

    def test_calendar_role_cannot_omit_a_named_exchange(self):
        request = self.request("ef")
        request["sources"]["calendars"] = self.calendars
        page = next(block for block in self.documents["ef_prospectus"]["blocks"] if block["kind"] == "pdf_page"
            and all(word in re.sub(r"\s+", "", block["text"]) for word in ("上海证券交易所", "深圳证券交易所", "开放日")))
        request["sources"]["calendar_roles"] = {role: {"ids": ["SSE"] if role == "pricing" else ["SSE", "SZSE"],
            "locator": page["locator"]} for role in ("pricing", "confirmation", "settlement")}
        result = fee_contract.inspect_contract(request, self.artifacts, as_of=self.as_of)
        self.assertIsNone(result["normalized"]["execution_calendar"])
        self.assertIn("omits", result["field_statuses"]["calendar_roles"]["reason"])


class ExactSourceModeTests(unittest.TestCase):
    def test_money_endpoint_inclusions_are_not_shifted_by_an_epsilon(self):
        left = fee_sources.parse_interval("M≤100万元", holding=False)
        right = fee_sources.parse_interval("M>100万元", holding=False)
        rule = {"kind": "amount_tiers", "bands": [{**left, "fee": {"kind": "fixed", "amount": "1"}},
                                                   {**right, "fee": {"kind": "fixed", "amount": "2"}}]}
        self.assertEqual([fee_contract.charge(value, rule) for value in ("999999.99", "1000000", "1000000.01")],
                         [Decimal("1.00"), Decimal("1.00"), Decimal("2.00")])

    def test_down_fund_share_surplus_is_separate_from_fee_and_refund(self):
        terms = {"subscription": {"kind": "percentage", "rate": "0"},
                 "trade_precision": {"share_rounding": "down_fund", "fee_rounding": "unrounded"}}
        quote = fee_contract.quote_entry("100", terms, price="3", share_step="0.01")
        self.assertEqual(quote["shares"], Decimal("33.33"))
        self.assertEqual(quote["fee"], 0)
        self.assertEqual(quote["share_surplus"], Decimal("0.01"))
        self.assertEqual((quote["debit"], quote["remainder"]), (Decimal("100"), Decimal("0")))

    def test_net_truncation_keeps_contract_fee_and_rounding_loss_distinct(self):
        terms = {"redemption": {"kind": "percentage", "rate": "0.015"},
            "trade_precision": {"money_step": "0.01", "fee_rounding": "unrounded", "redemption_net_rounding": "down_fund"},
            "holding": {"minimum_days": 0, "day_basis": "calendar_days", "start_inclusive": True,
                        "end_inclusive": False, "end_event": "execution_date"}}
        details = fee_contract.quote_exit_details("1.005", terms, acquired_at="2026-01-01T00:00:00Z", execution_at="2026-02-01T00:00:00Z")
        self.assertEqual(details, {"gross": Decimal("1.005"), "contractual_fee": Decimal("0.015075"),
            "rounding_loss": Decimal("0.009925"), "net": Decimal("0.98"), "effective_cost": Decimal("0.025")})
        self.assertEqual(fee_contract.quote_exit("1.005", terms, acquired_at="2026-01-01T00:00:00Z", execution_at="2026-02-01T00:00:00Z"), Decimal("0.025"))

    def test_unproved_ordinary_account_or_institution_pension_tag_is_unknown(self):
        self.assertEqual(fee_sources.canonical_investor_scope("ordinary_account"), "unknown")
        self.assertEqual(fee_sources.canonical_investor_scope("养老金客户"), "unknown")
        self.assertEqual(fee_sources.canonical_investor_scope("个人养老金账户"), "personal_pension")

    def test_net_truncation_bound_includes_fractional_gross_rounding_loss(self):
        terms = {"redemption": {"kind": "percentage", "rate": "0.015"},
            "trade_precision": {"money_step": "0.01", "fee_rounding": "unrounded", "redemption_net_rounding": "down_fund"}}
        self.assertEqual(fee_contract.fee_bounds("1.005", terms, [31]), (Decimal("0"), Decimal("0.025075")))


if __name__ == "__main__":
    unittest.main()
