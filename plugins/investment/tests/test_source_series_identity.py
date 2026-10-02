"""Real source reader/producer with synthetic raw transport, never live prices."""
import copy
import unittest
from unittest.mock import patch

import asset_domains
import industry_data
import source_documents
import source_fetch
from contracts import utc_now
import test_industry_data as industry_fixtures
from test_industry_data import transport


class SourceSeriesIdentityTests(unittest.TestCase):
    setUp = industry_fixtures.IndustryDataTests.setUp
    collect = industry_fixtures.IndustryDataTests.collect
    make_review = industry_fixtures.IndustryDataTests.make_review
    open_policy = industry_fixtures.IndustryDataTests.open_policy
    fact_review = industry_fixtures.IndustryDataTests.fact_review

    def original_contract(self, series, name, unit, currency, role, transform, quote):
        url = "https://fred.stlouisfed.org/series/"+series
        html = "<p>"+series+" "+name+"</p><p>"+quote+"</p>"
        with patch.object(source_fetch, "fetch", return_value=transport(html.encode(), url, "fred_series_identity", "text/html")):
            reference = source_documents.capture_document(url, "fred_series_identity", self.artifacts, store=self.store)
        return {"sector_id": "合成产业", "thesis_ids": ["equity"], "benchmark_id": "engineering-"+series,
            "series_id": series, "series_name": name, "return_definition": transform, "currency": currency,
            "calendar_id": "source_US_observation_sessions", "timezone": "America/New_York",
            "identity_quote": series+" "+name, "identity_document_ref": reference,
            "start": "2025-01-02", "end": "2025-01-03", "benchmark_role": role,
            "adapter_id": asset_domains.SERIES_ADAPTER_ID, "quote_source_id": "fred_series_csv",
            "quote_url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id="+series,
            "series_columns": {"date": "observation_date", "value": series},
            "asset_domain": {"kind": "bond" if role == "rate" else "commodity" if role == "commodity" else "foreign",
                "role": role, "return_target": {"transform": transform, "economic_meaning": quote,
                    "source_unit": unit, "canonical_unit": "decimal_rate" if unit == "percent" else unit,
                    "locator": "html/block/1", "quote": quote}}}

    def prepare(self, contract, values, operation):
        review, spec = self.fact_review()
        raw = ("observation_date,"+contract["series_id"]+"\n2025-01-02,"+values[0]+"\n2025-01-03,"+values[1]+"\n").encode()
        capture = transport(raw, contract["quote_url"], "fred_series_csv", "text/csv")
        with patch.object(source_fetch, "fetch", return_value=capture):
            value = industry_data.prepare_sector_data(review, spec, self.store, self.artifacts, operation, [contract])
        return industry_data.validate_sector_data(value, spec, utc_now(), self.artifacts, store=self.store), value, spec

    def test_percent_rate_source_without_currency_passes_real_source_pipeline(self):
        contract = self.original_contract("DGS10", "Treasury Yield", "percent", "NONE", "rate", "level_change", "Yield Units: Percent")
        bundle, _, _ = self.prepare(contract, ["4.0", "4.5"], "rate-source")
        sector = bundle["sectors"][0]
        self.assertEqual(sector["currency"], "NONE")
        self.assertAlmostEqual(sector["prices"][0]["close"], .04)
        self.assertAlmostEqual(sector["returns"][0]["return"], .005)
        self.assertEqual(sector["sector_relation"]["status"], "verified")
        self.assertFalse(bundle["trade_ready"])

    def test_dollar_commodity_keeps_original_unit_without_unproved_FX_conversion(self):
        contract = self.original_contract("DCOILWTICO", "Crude Oil Prices", "USD_per_barrel", "USD", "commodity",
                                          "price_log_return", "Oil Units: U.S. Dollars per Barrel")
        bundle, _, _ = self.prepare(contract, ["80", "88"], "oil-source")
        sector = bundle["sectors"][0]
        self.assertEqual(sector["currency"], "USD")
        self.assertEqual(sector["prices"][0]["close"], 80.)
        self.assertAlmostEqual(sector["returns"][0]["return"], .1)
        self.assertEqual(sector["sector_relation"]["status"], "verified")
        wrong = copy.deepcopy(contract); wrong["currency"] = "CNY"
        with self.assertRaisesRegex(ValueError, "currency conflicts"):
            industry_data._contract(wrong)
        wrong = copy.deepcopy(contract); wrong["asset_domain"]["return_target"]["canonical_unit"] = "CNY_per_gram"
        with self.assertRaisesRegex(ValueError, "Unregistered asset unit conversion"):
            industry_data._contract(wrong)

    def test_reversed_FX_direction_and_wrong_unit_fail_original_identity(self):
        contract = self.original_contract("DEXCHUS", "Yuan Exchange Rate", "CNY_per_USD", "CNY", "fx",
            "price_log_return", "Exchange rate: yuan renminbi to one U.S. dollar")
        industry_data._identity(contract, self.artifacts, utc_now())
        reversed_contract = copy.deepcopy(contract)
        reversed_contract["currency"] = "USD"
        target = reversed_contract["asset_domain"]["return_target"]
        target["source_unit"] = target["canonical_unit"] = "USD_per_CNY"
        with self.assertRaisesRegex(ValueError, "quotation direction"):
            industry_data._identity(reversed_contract, self.artifacts, utc_now())
        wrong = copy.deepcopy(contract)
        wrong["currency"] = "NONE"
        target = wrong["asset_domain"]["return_target"]
        target.update(source_unit="percent", canonical_unit="decimal_rate", transform="level_change")
        wrong["return_definition"] = "level_change"
        with self.assertRaisesRegex(ValueError, "source unit"):
            industry_data._identity(wrong, self.artifacts, utc_now())

    def test_mainland_index_adapter_still_requires_CNY(self):
        contract = industry_fixtures.IndustryDataTests.contract(self)
        contract["currency"] = "USD"
        with self.assertRaisesRegex(ValueError, "mainland CNY clock"):
            industry_data._contract(contract)

    def test_official_EIA_dollars_unit_needs_commodity_publisher_context(self):
        quote = "Oil Units: Dollars per Barrel; Source: U.S. Energy Information Administration"
        contract = self.original_contract("DCOILWTICO", "Crude Oil Prices", "USD_per_barrel", "USD", "commodity",
                                          "price_log_return", quote)
        bundle, _, _ = self.prepare(contract, ["80", "88"], "eia-native-unit")
        self.assertEqual(bundle["sectors"][0]["currency"], "USD")

    def test_generic_foreign_dollars_are_not_assumed_USD_from_FRED_hostname(self):
        contract = self.original_contract("CADPRICE", "Crude Oil Prices", "USD_per_barrel", "USD", "commodity",
                                          "price_log_return", "Oil Units: Canadian Dollars per Barrel")
        with self.assertRaisesRegex(ValueError, "denomination lacks original"):
            industry_data._identity(contract, self.artifacts, utc_now())


if __name__ == "__main__":
    unittest.main()
