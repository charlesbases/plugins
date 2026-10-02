"""Synthetic source grammar counterexamples, with independently stated values."""
import copy
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import issuer_nav


class IssuerNavTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"code": "123456", "legal_name": "示例证券投资基金", "share_class": "C", "currency": "CNY"}
        self.lines = ["下属分级基金的基金简称 示例 A\n示例 C", "下属分级基金的交易代码 123455 123456",
                      "会计主体:示例证券投资基金", "报告截止日:2026年6月30日", "单位:人民币元",
                      "披露日期2026年8月31日，去年同期2025年6月30日",
                      "注:报告截止日2026年6月30日,A类基金份额净值2.01320元,C类基金份额净值1.9845元;基金份额总额100份"]

    def parse(self, lines=None, identity=None):
        return issuer_nav.parse_report({"blocks": [{"kind": "pdf_line", "locator": "line/"+str(i), "text": text}
                                                   for i, text in enumerate(lines or self.lines)]}, identity or self.identity)

    def test_explicit_share_and_report_date_preserve_literal_precision(self):
        row = self.parse()[0]
        self.assertEqual((row["code"], row["date"], row["nav"], row["decimal_places"]),
                         ("123456", "2026-06-30", "1.9845", 4))
        a = self.parse(identity={**self.identity, "code": "123455", "share_class": "A"})[0]
        self.assertEqual(issuer_nav.rounding_tolerance(a), Decimal("0.000005"))
        self.assertEqual(issuer_nav.rounding_tolerance(row), Decimal("0.00005"))
        self.assertTrue(row["date_locators"] and row["identity_locators"] and row["subject_locators"])

    def test_cumulative_and_return_labels_never_become_unit_nav(self):
        for replacement in ("基金份额累计净值", "基金份额净值增长率", "累计单位净值"):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                self.parse([v.replace("基金份额净值", replacement) for v in self.lines])

    def test_unlabelled_publication_date_is_not_nav_date(self):
        with self.assertRaises(ValueError):
            self.parse([v.replace("注:报告截止日", "注:披露日期") for v in self.lines])
        with self.assertRaises(ValueError):
            self.parse([v.replace("2026年6月30日", "2026年2月30日") for v in self.lines])

    def test_wrong_share_subject_or_currency_is_rejected(self):
        for changed in ({"share_class": "A"}, {"code": "999999"}, {"legal_name": "另一基金"}, {"currency": "USD"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.parse(identity={**self.identity, **changed})
        with self.assertRaises(ValueError):
            self.parse(self.lines+["会计主体:另一基金"])

    def test_duplicate_share_currencies_and_partial_note_are_rejected(self):
        for changed in (
                [v.replace("示例 A\n示例 C", "示例 A（人民币）\n示例 A（美元）") for v in self.lines],
                [v.replace("示例 C", "示例 C （澳元）") for v in self.lines],
                [v.replace("示例 C", "示例 C AUD") for v in self.lines],
                [v.replace("C类基金份额净值1.9845元", "C类基金份额净值1.9845美元") for v in self.lines],
                [v.replace("单位:人民币元", "单位:美元") for v in self.lines]):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.parse(changed)

    def test_conflicting_notes_and_caller_precision_cannot_override_source(self):
        with self.assertRaises(ValueError):
            self.parse(self.lines+[self.lines[-1].replace("1.9845", "2.0000")])
        row = copy.deepcopy(self.parse()[0]); row["decimal_places"] = 1
        with self.assertRaises(ValueError):
            issuer_nav.rounding_tolerance(row)


if __name__ == "__main__":
    unittest.main()
