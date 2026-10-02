"""Public source workflow with synthetic transport; no economic success claim."""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
from contracts import instant, utc_now
from file_io import read_object
import investment
import source_fetch
import news_economics
import test_pipeline_schema4 as source_fixtures


class PublicIndustrySourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root = self.directory / "synthetic-source-cache"
        self.policy = {"mode": "sealed_inputs", "sources": ["cn_state_council"],
                       "required_source_groups": {"synthetic-policy": ["cn_state_council"]}, "max_age_seconds": 3600}
        self.transport = self.enterContext(patch.object(source_fetch, "fetch", side_effect=source_fixtures.source_capture))

    def call(self, identity, operation, payload, expected_code=0):
        path = self.directory / (identity+".json")
        path.write_text(json.dumps({"schema_version": 4, "request_id": "source:"+identity,
                                   "operation": operation, "payload": payload}), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = investment.main(["run", "--root", str(self.root), "--plan", "synthetic-only", "--request", str(path)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, expected_code, {"status": result.get("status"), "reason": result.get("reason"),
            "cause": result.get("validation_failure", {}).get("cause")})
        self.assertNotEqual(result["status"], "internal_error", result)
        return result

    def open_and_collect(self, suffix):
        opened = self.call("open-"+suffix, "analysis_start", {"news_policy": self.policy})
        run = opened["run"]
        payload = {"run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": "2020-01-01T00:00:00Z",
                   "sources": [{"source_id": "cn_state_council", "urls": [source_fixtures.NEWS_URL]}],
                   "required_source_groups": self.policy["required_source_groups"]}
        collected = self.call("news-"+suffix, "news_collect", payload)
        return run, payload, collected

    def test_public_analysis_news_sector_chain_preserves_missing_source_labels(self):
        run, _, collected = self.open_and_collect("one")
        version = collected["versions"][0]
        deadline = (instant(utc_now())+dt.timedelta(hours=1)).isoformat()
        claim = {"id": "source-fact", "kind": "fact", "text": source_fixtures.NEWS_BODY[:30],
                 "version_id": version["version_id"], "quote": source_fixtures.NEWS_BODY[:30], "categories": ["synthetic-observed"],
                 "direction": "neutral", "counterevidence": [{"missing_reason": "Synthetic transport-only source contract test"}],
                 "reviewer": "synthetic-source-fixture", "review_method": "literal_source_quote"}
        event = {"event_key": None, "version_ids": [version["version_id"]], "claim_ids": [claim["id"]], "event_at": None,
                 "effective_from": None, "effective_until": None, "review_by": deadline, "supersedes": [], "retracts": [], "temporal_evidence": []}
        thesis = {"thesis_id": "synthetic-label", "kind": "broad_market", "label": "Synthetic source observation only", "direction": "watch",
                  "horizon_days": 60, "search_terms": ["synthetic-observed"], "claim_ids": [claim["id"]], "event_ids": [version["document_id"]],
                  "required_source_groups": ["synthetic-policy"], "valid_until": deadline, "event_basis": "announcement_information"}
        assessed = self.call("assess-one", "news_assess", {"run_id": run["run_id"], "collection_id": collected["collection_id"],
            "claims": [claim], "events": [event], "industry_theses": [thesis]})
        self.assertEqual(assessed["run_id"], run["run_id"])
        spec = read_object(SCRIPTS.parent / "references/strategy.example.json")
        spec["news"] = self.policy
        prepared = self.call("sector-one", "industry_prepare", {"analysis_run_id": run["run_id"], "news_review_id": "source:assess-one",
            "spec": spec, "benchmark_contracts": []})
        self.assertEqual(prepared["status"], "partial")
        self.assertTrue(prepared["required_actions"])
        self.assertFalse(prepared["trade_ready"])
        self.assertEqual(prepared["sector_ids"], [])

    def test_same_public_run_retry_reuses_capture_and_new_run_fetches(self):
        run, payload, collected = self.open_and_collect("one")
        calls = self.transport.call_count
        repeated = self.call("news-one", "news_collect", payload)
        self.assertEqual(self.transport.call_count, calls)
        self.assertEqual(repeated["manifest_ref"], collected["manifest_ref"])
        later, _, fresh = self.open_and_collect("two")
        self.assertGreater(self.transport.call_count, calls)
        self.assertNotEqual(later["run_id"], run["run_id"])
        self.assertEqual(fresh["run_id"], later["run_id"])
        foreign = self.call("foreign-cutoff", "news_collect", {**payload, "run_id": later["run_id"]}, expected_code=1)
        self.assertIn(foreign["status"], {"invalid_input", "invalid_evidence", "input_conflict"})



    def test_new_economic_public_entry_retains_raw_semantics_and_cold_replay(self):
        import source_archival_fixture
        run, _, collected = self.open_and_collect("economic")
        version = collected["versions"][0]
        deadline = (instant(utc_now())+dt.timedelta(hours=1)).isoformat()
        quote = source_archival_fixture.ECONOMIC_QUOTE
        claim = {"id": "economic-source", "kind": "fact", "text": quote, "version_id": version["version_id"], "quote": quote,
                 "categories": ["黄金"], "direction": "neutral", "counterevidence": [{"missing_reason": "engineering source only"}],
                 "reviewer": "engineering", "review_method": "literal_source"}
        event = {"event_key": None, "version_ids": [version["version_id"]], "claim_ids": [claim["id"]], "event_at": None,
                 "effective_from": None, "effective_until": None, "review_by": deadline, "supersedes": [], "retracts": [], "temporal_evidence": []}
        thesis = {"thesis_id": "economic-argument", "sector_id": "黄金", "kind": "industry", "label": "黄金", "direction": "watch",
                  "horizon_days": 30, "search_terms": ["黄金"], "claim_ids": [claim["id"]], "event_ids": [version["document_id"]],
                  "required_source_groups": ["synthetic-policy"], "valid_until": deadline, "event_basis": "standing_policy"}
        review = self.call("economic-assess", "news_assess", {"run_id": run["run_id"], "collection_id": collected["collection_id"],
            "claims": [claim], "events": [event], "industry_theses": [thesis], "economic_observations": [{
                "event_id": version["document_id"], "sector_ids": ["黄金"], "category": "industry_policy", "metric_id": "policy_action",
                "metric_label": "黄金行业政策", "current": None, "prior": None, "expectation": None,
                "action": {"version_id": version["version_id"], "quote": quote, "value": "introduced"}}]})
        observation = review["economic_observations"][0]
        self.assertEqual(observation["source_action"]["quote"], quote)
        from artifacts import Artifacts
        artifacts = Artifacts(self.root/"plans"/"synthetic-only")
        collection = artifacts.read_json(review["collection_manifest_ref"])
        encoding_events = [{**event, "evidence_refs": [capture["raw_ref"] for capture in collection["retrievals"]
            if capture.get("version_id") in event["version_ids"] and capture.get("state") == "body"]}
            for event in review["events"]]
        encoded = news_economics.factual_event_features([observation], "黄金", utc_now(), events=encoding_events)
        self.assertEqual(len(encoded["feature_values"]), 84)
        named = dict(zip(encoded["feature_names"], encoded["feature_values"]))
        self.assertEqual(named["economic_event_qualitative_encoded_count"], 1)
        self.assertEqual([named["economic_event_"+state+"_count"] for state in ("measured", "unsupported", "source_gap")], [0, 0, 0])
        self.assertEqual(encoded["event_entity_states"][0]["status"], "qualitative_encoded")
        cold = self.call("cold-first", "daily_review", {})
        repeated = self.call("cold-first", "daily_review", {})
        self.assertEqual(cold, repeated)
        self.assertEqual(cold["status"], "awaiting_input")
        self.assertIsNone(cold.get("context"))


if __name__ == "__main__":
    unittest.main()
