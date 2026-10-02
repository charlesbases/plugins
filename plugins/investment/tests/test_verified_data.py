"""Final data binding uses raw research audits, not self-reported success flags."""
import copy
from pathlib import Path
import sys
import tempfile
import hashlib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
from artifacts import Artifacts
from contracts import fingerprint, utc_now
from state_store import Store
import fund_universe
import pipeline
import research
import source_fetch
import source_documents
from test_pipeline_schema4 import fixture_contract_text
import verify
from test_research import CODE, START, END, synthetic_capture


class VerifiedDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'investment', 'binding')
        self.store.begin('fixture', {'kind': 'synthetic-source-binding'})
        self.enterContext(self.store.lease('fixture'))
        self.artifacts = Artifacts(self.store.base)
        with patch.object(source_fetch, 'fetch', side_effect=synthetic_capture):
            catalog = fund_universe.collect_catalog(self.store, self.artifacts, 'catalog')
            identity = fund_universe.resolve_identity([CODE], catalog, self.artifacts, store=self.store)
            prepared = research.prepare(self.store.root, self.store.plan, [CODE], START, END, [1], 120,
                                        operation_id='raw-research', identity_snapshot_ref=identity, store=self.store)
            contract, raw = fixture_contract_text()
            url = "https://www.cmfchina.com/web/fundDetail/"+CODE+"/index.html"
            capture = synthetic_capture(url, "issuer_cmfchina")
            binary = raw.encode("utf8")
            capture.update(text=raw, raw_bytes=binary, bytes=len(binary), raw_sha256=hashlib.sha256(binary).hexdigest(),
                           text_sha256=hashlib.sha256(binary).hexdigest())
            with patch.object(source_fetch, "fetch", return_value=capture):
                document_ref = source_documents.capture_document(url, "issuer_cmfchina", self.artifacts, store=self.store)
            contract["quote_observed_at"] = self.artifacts.read_json(document_ref)["retrieved_at"]
            locators = dict(zip(("dealing", "entry", "exit", "calendar"),
                                ("html/block/0", "html/block/1", "html/block/2", "html/block/3")))
            for value in contract["bindings"].values():
                for binding in value if isinstance(value, list) else [value]:
                    binding.update(document_ref=document_ref, locator=locators[binding["locator"]])
            contract["normalized"]["execution_calendar"]["evidence_ref"] = {
                "document_ref":document_ref, "locator":locators["calendar"]}
            contract["contract_hash"] = fingerprint({key: value for key, value in contract.items() if key != "contract_hash"})
            market = pipeline._market(prepared, {"currency": "CNY", "contracts": [contract]},
                                      self.store, self.artifacts, 'market')
        decision_at = utc_now()
        self.bundle = {"decision_at": decision_at, "context": {"decision_at": decision_at}, "market_id": market['market_id'],
                       "market_record_ref": self.artifacts.put_json(market['record']), "data_ref": market['data_ref'],
                       "discovery_ref": self.artifacts.put_json({"codes": [CODE], "identity_snapshot_ref": identity})}

    def test_verified_raw_research_rebinds_data_identity_and_mark_date(self):
        result = verify.verify_research_binding(self.bundle, self.artifacts)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['price_dates'], {CODE: END.isoformat()})

    def test_resealed_data_and_matching_calculation_hash_cannot_replace_source_facts(self):
        original = self.artifacts.read_json(self.bundle['data_ref'])
        for field in ('nav', 'features', 'code_info'):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                if field == 'nav':
                    changed['nav'][CODE][0]['nav'] = 99.0
                elif field == 'features':
                    changed['features'][CODE][0]['values']['momentum_20_nav_observations'] = 12.0
                else:
                    changed['code_info'][CODE]['fund_group_id'] = 'invented-singleton-group'
                bundle = copy.deepcopy(self.bundle)
                bundle['data_ref'] = self.artifacts.put_json(changed)
                bundle['calculation_ref'] = self.artifacts.put_json({'input_hash': fingerprint(changed)})
                bundle['bundle_hash'] = fingerprint(bundle)
                with self.assertRaisesRegex(ValueError, 'independently audited raw research'):
                    verify.verify_research_binding(bundle, self.artifacts)

    def test_resealed_market_price_and_coverage_cannot_override_real_nav(self):
        original = self.artifacts.read_json(self.bundle['market_record_ref'])
        for field in ('price', 'coverage'):
            with self.subTest(field=field):
                market = copy.deepcopy(original)
                if field == 'price':
                    market['market_ref']['prices'][CODE] = '99.0'
                else:
                    market['provenance']['coverage_by_code'][CODE]['start'] = '1900-01-01'
                bundle = copy.deepcopy(self.bundle)
                bundle.update(market_record_ref=self.artifacts.put_json(market), market_id=fingerprint(market))
                bundle['context']['market_ref_hash'] = fingerprint(market['market_ref'])
                with self.assertRaisesRegex(ValueError, 'Market marks, currencies or per-asset coverage'):
                    verify.verify_research_binding(bundle, self.artifacts)


if __name__ == '__main__':
    unittest.main()
