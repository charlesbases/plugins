"""Financial component regressions for unresolved valuation after fact persistence.

Real Store, feedback, reconciliation, risk, strategy, blocked-runner and report
rendering are exercised directly. News text is a rendering input, and the one
market-conversion case supplies synthetic loader data. These are not source
acceptance or public controller tests; test_pipeline_schema4 covers that chain.
"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_financial_v4 import FinancialV4Tests, AT
from artifacts import Artifacts
from contracts import EvidenceError, fingerprint
import account_reconciliation
import allocation_runner
import allocation_runtime
import allocation_market
import research_data
import ledger
import newtrade_guard
import pipeline
import report
import risk_profile
import strategy
import verify


class AccountReviewV4Tests(unittest.TestCase):
    def setUp(self):
        self.f = FinancialV4Tests("test_pre_ex_mark_cannot_double_count_income_and_unowned_candidate_needs_no_entitlement")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        self.objects = Artifacts(f.store.base)
        proof = self.objects.put_bytes(b"Synthetic confirmed receipt; no investment evidence")
        self.enterContext(patch.object(pipeline, "utc_now", side_effect=lambda: f.now))
        self.enterContext(patch.object(account_reconciliation, "utc_now", side_effect=lambda: f.now))
        buy = {"id": "B-buy", "type": "external_fill_confirmed", "effective_at": AT, "known_at": AT, "recorded_at": AT,
               "data": {"side": "buy", "code": "B", "fill_id": "B-fill", "lot_id": "B", "shares": "100", "price": "10",
                        "price_date": AT[:10], "gross_amount": "1000", "fee": "0", "cash_amount": "1000", "settlement_at": None,
                        "holding_started_at": AT, "ownership_at": AT, "evidence_ref": proof}}
        feedback = lambda payload, store, operation: pipeline.feedback(payload, store)
        f.call("buy", feedback, {"currency": "CNY", "events": [buy]})
        f.now = "2030-01-04T08:00:00Z"
        dividend = {"id": "B-income", "type": "dividend_declared", "effective_at": "2030-01-02T16:00:00Z",
                    "known_at": f.now, "recorded_at": f.now, "data": {"distribution_id": "B-income", "code": "B",
                        "per_share": "1", "pay_at": "2030-01-05T08:00:00Z", "record_at": "2030-01-02T15:59:59Z",
                        "entitled_shares": "100"}}
        f.call("income", feedback, {"currency": "CNY", "events": [dividend]})
        self.spec = json.loads((Path(pipeline.__file__).parent.parent / "references/strategy.example.json").read_text(encoding="utf-8"))
        self.spec["benchmark"] = {"rule": "fixed_weights", "weights": {"B": "1"}, "cash_weight": "0", "rebalance_dates": []}
        f.call("plan", risk_profile.update_constraints, {"constraints": {"platform": "TT", "currency": "CNY",
            "goal": "Synthetic accounting test", "excluded_categories": [],
            "position_limits": self.spec["constraints"]}, "current_constraints_hash": None,
            "user_source": {"message": "Synthetic explicit plan", "confirmed_at": f.now}})
        self.identities = {"B": {"fund_group_id": "B", "sector_exposures": None}}
        self.news_text = {"claims": [{"kind": "inference", "text": "Synthetic news continues during reconciliation"}]}
        self.market = {"market_ref": {"schema_version": 4, "currency": "CNY", "observed_at": f.now,
            "prices": {"B": "10"}, "price_dates": {"B": "2030-01-02"}, "currencies": {"B": "CNY"},
            "terms": [], "terms_basis": "source_verified", "source_hashes": {"fixture": proof["sha256"]}},
            "provenance": {"product_names": {"B": "Synthetic B"}, "price_dates": {"B": "2030-01-02"}, "terms_basis": "source_verified",
                "term_gaps": [{"code": "B", "kind": "contract_not_provided", "contract_ref": None,
                               "required_actions": [{"code": "B", "action": "capture_and_inspect_current_product_dealing_terms"}]}],
                "corporate_actions": [{"code": "B", "ex_date": "2030-01-03", "record_date": "2030-01-02", "per_share": "1"}],
                "action_inventory": {"by_code": {}}}, "evidence_refs": []}
        self.data = {"nav": {}, "features": {}, "code_info": self.identities}
        self.market["market_ref"]["known_marks"] = {"B":allocation_market.known_mark(
            self.captured_nav("2030-01-02",10.)[0],self.f.now)}

    def captured_nav(self,date,value,folder=None):
        """Capture engineering bytes now; never assert historical publication."""
        rows = [{"code":"B","date":date,"nav":value,"cumulative_nav":value,
                 "distribution_per_share":0.}]
        raw = json.dumps(rows,sort_keys=True,separators=(",",":")).encode("utf-8")
        reference = self.objects.put_bytes(raw)
        if folder is not None:
            (folder/"nav.json").write_bytes(raw)
        metadata = {"source_id":"synthetic-current-NAV-B","sha256":reference["sha256"],"retrieved_at":self.f.now}
        return research_data.bind_source_versions(rows,metadata)

    def component_review(self, identity):
        def evaluate(payload, store, operation):
            market = copy.deepcopy(self.market)
            reconciliation = account_reconciliation.reconcile_market("main", market, store, self.objects, operation)
            state = store.get("account", "main")
            account_review = account_reconciliation.review_account(state, market["market_ref"], self.f.now)
            context, calculation = None, None
            if account_review is None:
                risk = risk_profile.resolve(store, state, self.f.now, market["market_ref"]["prices"],
                                            price_dates=market["market_ref"]["price_dates"])
                trade = newtrade_guard.read_inputs(store, self.objects, "main", self.spec, self.f.now, decision_id=operation)
                context = strategy.build_context(self.spec, state, self.f.now, market["market_ref"],
                    risk_state=risk, purchase_eligible_codes=[], dynamic_universe=["B"], candidate_identities=self.identities,
                    known_future_actions=pipeline.future_actions(market, self.f.now),
                    trade_state=trade["trade_state"], trade_family_review_index=trade["trade_family_review_index"])
                calculation = allocation_runner.predict(context, self.data)
            orders = calculation["orders"] if calculation else {"status": "blocked", "orders": [], "waiting": [], "funding_options": []}
            model = {"decision_id": operation, "decision_at": self.f.now, "account_id": "main",
                "account_recorded_at": state["recorded_at"], "market_price_dates": market["provenance"]["price_dates"],
                "account_review": account_review, "context": context,
                "calculation_ref": self.objects.put_json(calculation) if calculation else None,
                "market_record_ref": self.objects.put_json(market), "product_names": market["provenance"]["product_names"],
                "term_gaps": market["provenance"]["term_gaps"], "news": self.news_text,
                "status": calculation["status"] if calculation else "awaiting_account", "orders": orders,
                "action_analysis": {"actions": []}, "missing": [],
                "reason": calculation["reason"] if calculation else "Confirmed account facts require valuation reconciliation.",
                "qualification": {"reason": "Synthetic financial component and rendering fixture only; no source acceptance or investment qualification."}}
            model["bundle_hash"] = fingerprint(model)
            markdown = report.render(model)
            return {"status": model["status"], "report_model_ref": self.objects.put_json(model),
                    "report_markdown": markdown, "reconciliation": reconciliation,
                    "rendering_check": verify.verify_report(model, markdown)}
        return self.f.call(identity, evaluate, {"scope": "financial_and_rendering_components", "market_hash": fingerprint(self.market)})

    def test_pre_ex_valuation_keeps_facts_and_publishes_news_with_required_actions(self):
        result = self.component_review("stale")
        self.assertEqual(result["status"], "awaiting_account")
        bundle = self.objects.read_json(result["report_model_ref"])
        self.assertIsNone(bundle["context"]); self.assertIsNone(bundle["calculation_ref"])
        self.assertEqual(bundle["orders"]["orders"], [])
        self.assertIn("obtain_post_ex_date_NAV", [row["action"] for row in bundle["account_review"]["required_actions"]])
        self.assertIn("Synthetic news continues", result["report_markdown"])
        self.assertIn("账户补核", result["report_markdown"])
        self.assertNotIn("剩余金额风险预算", result["report_markdown"])
        state = self.f.store.get("account", "main")
        self.assertEqual(state["receivables"]["B-income"]["amount"], "100")
        self.assertIsNone(ledger.snapshot(state, self.f.now)["equity"])
        self.assertIsNotNone(self.f.store.get("market_account_reconciliation", "stale:main"))
        self.assertEqual(self.component_review("stale"), result)
        self.assertEqual(bundle["account_review"], account_reconciliation.review_account(state, self.market["market_ref"], self.f.now))
        self.assertEqual(result["rendering_check"]["status"], "passed")

    def test_later_post_ex_nav_restores_risk_without_inventing_missing_product_terms(self):
        self.component_review("stale")
        self.market["market_ref"]["prices"]["B"] = "9"
        self.market["market_ref"]["price_dates"]["B"] = "2030-01-03"
        self.market["provenance"]["price_dates"]["B"] = "2030-01-03"
        self.market["market_ref"]["known_marks"] = {"B":allocation_market.known_mark(
            self.captured_nav("2030-01-03",9.)[0],self.f.now)}
        result = self.component_review("fresh")
        bundle = self.objects.read_json(result["report_model_ref"])
        self.assertIsNone(bundle["account_review"])
        self.assertEqual(bundle["context"]["risk_state"]["equity"], "1000")
        self.assertEqual(bundle["context"]["risk_state"]["remaining_loss_budget"], "250")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("required_dealing_contract_missing:B", bundle["context"]["reasons"])
        self.assertEqual(bundle["orders"]["orders"], [])

    def test_waiting_report_rejects_removed_reconciliation_or_injected_order_text(self):
        result = self.component_review("stale")
        original = self.objects.read_json(result["report_model_ref"])
        changed = copy.deepcopy(original)
        changed["account_review"]["required_actions"] = []
        for markdown in (report.render(changed), result["report_markdown"] + "\n买入 Synthetic B（B）：100 CNY。\n"):
            with self.assertRaises(EvidenceError):
                verify.verify_report(original, markdown)

    def test_real_market_inventory_blocks_double_count_before_ex_date_nav_arrives(self):
        folder = self.f.store.base / "synthetic-research-input"
        folder.mkdir()
        raw = b"Synthetic source discloses the Jan 3 distribution before post-ex NAV arrives"
        proof = self.objects.put_bytes(raw)
        (folder / "actions.txt").write_bytes(raw)
        nav_rows = self.captured_nav("2030-01-02",10.,folder)
        nav_ref = nav_rows[0]["source_versions"][0]["raw_ref"]
        (folder / "source-manifest.json").write_bytes(json.dumps([{
            "path": "actions.txt", "source_id": "actions", "final_url": "https://example.invalid/actions",
            "bytes":len(raw), "sha256":proof["sha256"], "raw_sha256":proof["sha256"],
            "retrieved_at": self.f.now},{"path":"nav.json","source_id":nav_ref["source_id"],
            "final_url":"https://example.invalid/synthetic-current-nav-B",
            "bytes":(folder/"nav.json").stat().st_size,"sha256":nav_ref["sha256"],"raw_sha256":nav_ref["sha256"],
            "retrieved_at":self.f.now}]).encode())
        dividend = {"date": "2030-01-03", "record_date": "2030-01-02", "cash_payment_date": "2030-01-05",
                    "distribution_per_share": 1, "currency": "CNY", "rights_rule": "confirmed_ownership_at_record_date"}
        inventory = {"by_code": {"B": {"known_at": self.f.now, "source_id": "actions",
                                       "source_sha256": proof["sha256"], "dividends": [dividend]}}}
        nav = {"B":nav_rows}
        manifest = {"dataset_hash": "synthetic-loader-boundary", "action_inventory": inventory,
                    "raw_summaries": [{"code": "B", "name_observed_today": "Synthetic B"}]}
        inputs = (folder, {"status": "not_evaluated", "scope": "synthetic-loader-boundary"}, manifest,
                  {"code_info": {"B": {"currency": "CNY"}}}, nav, {})
        # Loader data is supplied without a positive source-audit verdict.
        # Production market conversion, byte copying, coverage and account run.
        with patch.object(allocation_runtime, "load_data", return_value=inputs):
            converted = self.f.call("market-conversion", lambda payload, store, operation:
                pipeline._market({"run_path": "synthetic_source_boundary"}, payload, store, self.objects, operation),
                {"currency": "CNY", "contracts": []})
        self.market = converted["record"]
        result = self.component_review("source-inventory")
        self.assertEqual(result["status"], "awaiting_account")
        bundle = self.objects.read_json(result["report_model_ref"])
        market = self.objects.read_json(bundle["market_record_ref"])
        self.assertEqual(market["provenance"]["corporate_actions"], [])
        self.assertEqual(market["provenance"]["action_inventory"], inventory)
        self.assertIn("obtain_post_ex_date_NAV", [row["action"] for row in bundle["account_review"]["required_actions"]])
        state = self.f.store.get("account", "main")
        self.assertEqual(state["receivables"]["B-income"]["amount"], "100")
        self.assertIsNone(ledger.snapshot(state, self.f.now)["equity"])
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            risk_profile.resolve(self.f.store, state, self.f.now, market["market_ref"]["prices"],
                                 price_dates=market["market_ref"]["price_dates"])
        changed = copy.deepcopy(market)
        changed["provenance"]["action_inventory"]["by_code"]["B"]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source differs"):
            account_reconciliation.source_actions(changed, self.f.now)


if __name__ == "__main__":
    unittest.main()
