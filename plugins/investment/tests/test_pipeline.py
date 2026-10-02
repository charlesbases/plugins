"""Public orchestration and report gates; fixture research is labelled synthetic."""
import json
import hashlib
import io
from contextlib import redirect_stdout
import datetime as dt
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
import pipeline
from contracts import ConflictError, EvidenceError, strict_json_loads, utc_now
from state_store import Store
from artifacts import Artifacts
import verify

STAMP = "2025-01-01T12:00:00+08:00"


def opening():
    return {"id": "opening", "type": "opening", "effective_at": STAMP, "known_at": STAMP,
            "recorded_at": STAMP, "data": {"cash": "1000", "lots": [], "prices": {}, "price_dates": {}}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"

    def tearDown(self):
        self.temp.cleanup()

    def run_request(self, identity, operation, payload):
        return pipeline.run({"schema_version": 4, "request_id": identity, "operation": operation,
                             "payload": payload}, self.root, "case")

    def test_confirmed_exposure_limits_use_exact_numeric_equality(self):
        spec = json.loads((SCRIPTS.parent / "references/strategy.example.json").read_text(encoding="utf-8"))
        spec["constraints"] = {"fund_group_limits": {"group": .5}, "sector_limits": {"sector": .2}}
        plan = {"constraints": {"currency": spec["currency"], "platform": spec["universe_policy"]["platform"],
                               "position_limits": {"fund_group_limits": {"group": "0.50"}, "sector_limits": {"sector": "0.20"}}}}
        pipeline._check_plan(plan, spec)
        plan["constraints"]["position_limits"]["sector_limits"]["sector"] = "0.30"
        with self.assertRaisesRegex(ValueError, "Exposure limits"):
            pipeline._check_plan(plan, spec)

    def test_first_use_and_idempotent_feedback(self):
        self.assertEqual(self.run_request("status", "status", {})["mode"], "first_investment")
        payload = {"currency": "CNY", "events": [opening()]}
        first = self.run_request("feedback", "feedback", payload)
        self.assertEqual(first, self.run_request("feedback", "feedback", payload))
        self.assertEqual(self.run_request("repeat", "feedback", payload)["added"], [])
        account = Store(self.root, "case").get("account", "main")
        self.assertEqual((account["cash"], account["sequence"]), ("1000", 1))

    def test_conflicting_request_does_not_change_cash(self):
        payload = {"currency": "CNY", "events": [opening()]}
        self.run_request("feedback", "feedback", payload)
        payload["events"][0]["data"]["cash"] = "2000"
        with self.assertRaises(ConflictError):
            self.run_request("feedback", "feedback", payload)
        self.assertEqual(Store(self.root, "case").get("account", "main")["cash"], "1000")

    def test_invalid_batch_rolls_back_all_events(self):
        bad = {"id": "bad", "type": "valuation", "effective_at": STAMP, "known_at": STAMP,
               "recorded_at": STAMP, "data": {"prices": {"000001": "not-a-number"}, "price_dates": {"000001": "2025-01-01"}}}
        with self.assertRaises(ValueError):
            self.run_request("bad", "feedback", {"currency": "CNY", "events": [opening(), bad]})
        store = Store(self.root, "case")
        self.assertIsNone(store.get("account", "main"))
        self.assertIsNone(store.get("ledger_event", "opening"))

    def test_source_failure_preserves_confirmed_facts_and_retry(self):
        from test_pipeline_schema4 import source_capture, NEWS_URL
        from validation_runtime import StageValidationFailure
        run = self.run_request("news-authority","analysis_start",{"news_policy":{"mode":"sealed_inputs",
            "sources":["cn_state_council"],"max_age_seconds":3600,
            "required_source_groups":{"synthetic-policy":["cn_state_council"]}}})["run"]
        payload = {"analysis_run_id":run["run_id"],"news_policy":run["news_policy"],"feedback": {"currency": "CNY", "events": [opening()]},
                   "news": {"run_id":run["run_id"],"window_start": STAMP, "cutoff_at": run["publish_cutoff"],
                            "sources": [{"source_id": "cn_state_council", "urls": [NEWS_URL]}],
                            "required_source_groups": {"synthetic-policy": ["cn_state_council"]}}}
        with patch("news.collect", side_effect=OSError("fixture network unavailable")) as collect:
            with self.assertRaises(OSError):
                self.run_request("review", "daily_review", payload)
            self.assertEqual(collect.call_count, 3)
            with self.assertRaises(StageValidationFailure):
                self.run_request("review", "daily_review", payload)
            self.assertEqual(collect.call_count, 3)
        self.assertEqual(Store(self.root, "case").get("account", "main")["cash"], "1000")
        with patch("source_fetch.fetch", side_effect=source_capture):
            result = pipeline.run({"schema_version": 4, "request_id": "review-repaired", "revision_of": "review",
                                   "operation": "daily_review", "payload": payload}, self.root, "case")
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(Store(self.root, "case").get("account", "main")["sequence"], 1)

    def test_final_review_reexecutes_three_rounds_without_recrediting_facts(self):
        original = Store.complete
        calls = []
        def transient_final(store, *args, **kwargs):
            calls.append(args[0])
            if len(calls) < 3:
                raise EvidenceError("SYNTHETIC final publication fault")
            return original(store, *args, **kwargs)
        with patch.object(Store, "complete", transient_final):
            result = self.run_request("three-final-rounds", "feedback", {"currency":"CNY", "events":[opening()]})
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["automatic_validation"]["round_no"], 3)
        store = Store(self.root, "case")
        self.assertEqual((store.get("account", "main")["cash"],store.get("account", "main")["sequence"]),("1000",1))
        self.assertEqual([store.get("workflow_validation_end", "three-final-rounds:r"+str(n))["status"]
                          for n in (1,2,3)],["failed","failed","passed"])

    def test_final_review_three_failures_stop_and_resume_does_not_reset_budget(self):
        with patch.object(Store, "complete", side_effect=EvidenceError("SYNTHETIC persistent final fault")) as complete:
            payload = {"currency":"CNY", "events":[opening()]}
            with self.assertRaises(pipeline.FinalValidationFailure):
                self.run_request("exhausted-final-rounds", "feedback", payload)
            self.assertEqual(complete.call_count, 3)
            with self.assertRaises(pipeline.FinalValidationFailure):
                self.run_request("exhausted-final-rounds", "feedback", payload)
            self.assertEqual(complete.call_count, 3)
        account = Store(self.root, "case").get("account", "main")
        self.assertEqual((account["cash"],account["sequence"]),("1000",1))

    def test_report_and_bundle_tamper_rejected(self):
        with patch("news.collect", return_value={"claims": [], "source_results": [], "fixture": "synthetic"}):
            result = self.run_request("review", "daily_review", {})
        objects = Artifacts(Store(self.root, "case").base)
        bundle = objects.read_json(result["bundle_ref"])
        verify.verify_report(bundle, result["report_markdown"])
        with self.assertRaises(EvidenceError):
            verify.verify_report(bundle, result["report_markdown"] + "Buy 99999")
        bundle["orders"]["orders"].append({"code": "000001", "cash_limit": "99999"})
        with self.assertRaises(EvidenceError):
            verify.verify_bundle(bundle, objects, store=Store(self.root, "case"))

    def test_json_types_duplicates_and_nonfinite_rejected(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(ValueError):
                strict_json_loads(text)
        for version in (True, 1, 2, 3, 4.0):
            with self.assertRaises(ValueError):
                pipeline.run({"schema_version": version, "request_id": "x", "operation": "status", "payload": {}}, self.root, "case")
        self.assertFalse(self.root.exists())

    def test_non_numerical_decision_audit_does_not_install_model_packages(self):
        with patch("news.collect", return_value={"claims": [], "source_results": [], "fixture": "synthetic"}):
            self.run_request("needs-input", "daily_review", {})
        with patch("allocation_runtime.ensure_runtime", side_effect=AssertionError("Numerical runtime is unnecessary")):
            result = self.run_request("audit-input", "audit", {"decision_id": "needs-input"})
        self.assertEqual(result["decision"]["status"], "passed")

    def test_operation_type_is_classified_before_hash_lookup(self):
        for operation in ([], {}, None, 1):
            with self.assertRaisesRegex(ValueError, "Unknown operation"):
                self.run_request("invalid-type", operation, {})
        self.assertFalse(self.root.exists())

    def test_first_use_requires_explicit_principal_and_loss_preference(self):
        result = self.run_request("initial", "status", {})
        self.assertEqual(result["profile"]["status"], "confirmation_required")
        self.assertEqual({item["field"] for item in result["profile"]["required_inputs"]},
                         {"principal", "loss_tolerance", "account_confirmation"})
        self.assertIsNone(result["account"])

    def test_market_rejects_unverified_flags_and_changed_normalized_terms(self):
        objects = Artifacts(Store(self.root, "case").base)
        invalid = {"schema_version": 4, "terms_basis": "user_attested", "observed_at": STAMP,
                   "terms": [{"verified": True, "subscription_fee": "0"}]}
        with self.assertRaisesRegex(EvidenceError, "source-verified"):
            verify.execution_sources(invalid, objects)
        reference = objects.put_json({"fixture": "synthetic contract; tests the projection boundary only"})
        derived = {"code": "123451", "fee_contract_ref": reference, "subscription_fee": "0.015"}
        market = {"schema_version": 4, "terms_basis": "source_verified", "observed_at": STAMP,
                  "terms": [{**derived, "subscription_fee": "0"}]}
        with patch("fee_contract.normalize_to_terms", return_value=derived):
            with self.assertRaisesRegex(EvidenceError, "re-extracted"):
                verify.execution_sources(market, objects)

    def test_nested_event_type_is_structured_cli_error(self):
        path = Path(self.temp.name) / "malformed.json"
        event = {**opening(), "type": []}
        path.write_text(json.dumps({"schema_version": 4, "request_id": "bad-nested", "operation": "feedback",
                                   "payload": {"currency": "CNY", "events": [event]}}), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "investment.py"), "run", "--root", str(self.root),
                                 "--plan", "case", "--request", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid_input")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.root.exists())

    def test_cli_status_uses_current_entry(self):
        path = Path(self.temp.name) / "request.json"
        path.write_text(json.dumps({"schema_version": 4, "request_id": "cli", "operation": "status", "payload": {}}))
        proc = subprocess.run([sys.executable, "-B", str(SCRIPTS / "investment.py"), "run", "--root", str(self.root),
                               "--plan", "case", "--request", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["mode"], "first_investment")

    def test_cli_loads_timezone_before_financial_confirmation_validation(self):
        import investment
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        ready = []
        def load_runtime(root):
            ready.append(str(root))
        def timezone(key):
            if not ready:
                raise ZoneInfoNotFoundError(key)
            return ZoneInfo(key)
        request = {"schema_version": 4, "request_id": "timezone-confirmation", "operation": "feedback",
                   "payload": {"currency": "CNY", "events": [{"id": "fixture-redemption", "type": "sell_fill",
                       "effective_at": STAMP, "known_at": STAMP, "recorded_at": STAMP,
                       "data": {"price_date": "2025-01-01", "actual_redemption_confirmed_at": STAMP}}]}}
        path = Path(self.temp.name) / "confirmation.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        with patch("allocation_runtime.ensure_runtime", side_effect=load_runtime), \
             patch("zoneinfo.ZoneInfo", side_effect=timezone), \
             patch("pipeline.run", return_value={"status": "recorded"}) as dispatch, redirect_stdout(io.StringIO()):
            code = investment.main(["run", "--root", str(self.root), "--plan", "case", "--request", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(ready, [str(self.root)])
        self.assertEqual(dispatch.call_args.args[0], request)

    def test_cli_dependency_failure_stops_before_dispatch(self):
        import investment
        path = Path(self.temp.name) / "dependency-failure.json"
        path.write_text(json.dumps({"schema_version": 4, "request_id": "dependency-failure",
                                   "operation": "status", "payload": {}}), encoding="utf-8")
        output = io.StringIO()
        with patch("allocation_runtime.ensure_runtime", side_effect=ValueError("Pinned runtime is incomplete")), \
             patch("pipeline.run") as dispatch, redirect_stdout(output):
            code = investment.main(["run", "--root", str(self.root), "--plan", "case", "--request", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "invalid_input")
        dispatch.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_interrupted_daily_reuses_report_clock_and_preserves_attempt_budget(self):
        from contracts import instant
        first = []
        later = []
        def clock():
            if later:
                return later[0]
            stamp = utc_now()
            first.append(stamp)
            return stamp
        with patch("pipeline.utc_now", side_effect=clock), \
             patch.object(Store, "complete", side_effect=KeyboardInterrupt("SYNTHETIC interruption before publication")):
            with self.assertRaises(KeyboardInterrupt):
                self.run_request("interrupted-daily", "daily_review", {})
        store = Store(self.root, "case")
        sealed = store.get("decision_clock", "interrupted-daily")
        self.assertIsNotNone(sealed)
        later.append((instant(first[-1])+dt.timedelta(minutes=5)).isoformat())
        with patch("pipeline.utc_now", side_effect=clock):
            result = self.run_request("interrupted-daily", "daily_review", {})
        self.assertEqual(result["status"], "awaiting_input")
        self.assertEqual(result["automatic_validation"]["status"], "partial")
        self.assertEqual(store.get("decision_clock", "interrupted-daily"), sealed)
        report = Artifacts(store.base).read_json(result["bundle_ref"])
        self.assertTrue(report["missing"])
        self.assertEqual(report["decision_at"], sealed["decision_at"])
        starts = [row for _, row in store.scan("validation_attempt_start")
                  if row["operation_id"] == "interrupted-daily" and row["stage"] == "operation_daily_review"]
        self.assertEqual([row["number"] for row in starts], [1, 2])
        self.assertIsNone(store.get("account", "main"))

    def test_competing_worker_cannot_overwrite_completed_result(self):
        request = {"schema_version": 4, "request_id": "raced", "operation": "feedback",
                   "payload": {"currency": "CNY", "events": [opening()]}}
        begin, competing = Store.begin, []

        def interleave(store, identity, body):
            prior = begin(store, identity, body)
            if not competing:
                competing.append(None)
                competing[0] = pipeline.run(request, self.root, "case")
            return prior

        with patch.object(Store, "begin", interleave):
            outer = pipeline.run(request, self.root, "case")
        self.assertEqual(outer, competing[0])
        self.assertEqual(outer, pipeline.run(request, self.root, "case"))
        store = Store(self.root, "case")
        self.assertEqual(store.operation("raced")["status"], "completed")
        self.assertEqual(len(list(store.scan("ledger_event"))), 1)

    def test_unknown_requires_an_existing_verified_artifact(self):
        event = {"id": "unknown", "type": "unknown", "effective_at": STAMP, "known_at": STAMP,
                 "recorded_at": STAMP, "data": {"reason": "Pending orders need confirmation"}}
        self.run_request("unknown", "feedback", {"currency": "CNY", "events": [opening(), event]})
        resolution = {**event, "id": "resolve", "type": "resolve_unknown", "data": {
            "unknown_id": "unknown", "evidence_ref": "this-file-does-not-exist", "resolution": "No pending orders"}}
        with self.assertRaises(ValueError):
            self.run_request("bad-resolution", "feedback", {"currency": "CNY", "events": [resolution]})
        store = Store(self.root, "case")
        self.assertEqual(len(store.get("account", "main")["unknown"]), 1)
        resolution["data"]["evidence_ref"] = Artifacts(store.base).put_json({"user_attested": "No pending orders"})
        self.run_request("verified-resolution", "feedback", {"currency": "CNY", "events": [resolution]})
        self.assertEqual(store.get("account", "main")["unknown"], [])

    def test_report_summarizes_funding_without_dumping_scenarios(self):
        from report import render
        selected = {"expected_net_return": 0.02, "cvar_loss_fraction": 0.05, "fees": 2.0,
                    "scenario_profits": [10000.0]*1000}
        bundle = {"decision_at": STAMP, "account_id": "main", "account_recorded_at": STAMP, "market_price_dates": {"000001": "2025-01-01"}, "action_analysis": {"actions": []},
                  "status": "conditional_research", "news": {},
                  "decision_id": "fixture", "bundle_hash": "a"*64, "qualification": {"reason": "fixture"},
                  "orders": {"orders": [], "waiting": [], "funding_options": [
                      {"proposed_contribution": 2000.0, "best": selected, "candidates": [selected]*4}]}}
        rendered = render(bundle)
        self.assertLess(len(rendered), 1500)
        self.assertIn("2000.00", rendered)
        self.assertNotIn("scenario_profits", rendered)

    def test_selected_report_uses_final_action_metrics_and_never_another_candidate(self):
        from report import summarize
        # Report-unit fixture only: it does not qualify a model or trade.
        model = {"id":"model-buy", "expected_net_return":.1, "cvar_loss_fraction":-.1,
                 "point":{"fees":2.}}
        held = {"id":"current-hold", "expected_profit":0, "expected_net_return":0,
            "cvar_loss_fraction":0, "point":{"fees":0}, "current_projection":{},
            "eligible":True, "trade_guard":{"eligible":True}}
        calculation = {"mpc":{"funding_options":[{"candidates":[model,held]}],
            "selected_policy":held}, "orders":{"selected_candidate_id":"current-hold", "status":"no_action"}}
        context = {"spec":{"planning":{"primary_horizon_days":30}}}
        selected = summarize(calculation, context)["selected"]
        self.assertEqual(selected["id"], "current-hold")
        self.assertEqual((selected["expected_net_return"],selected["cvar_loss_fraction"],selected["fees"]),(0,0,0))
        calculation["mpc"]["selected_policy"] = None
        calculation["mpc"]["reason"] = "source_path_valuation_required"
        calculation["orders"] = {"status":"blocked"}
        summary = summarize(calculation, context)
        self.assertIsNone(summary["selected"])
        self.assertEqual(summary["selection_pending_reason"], "source_path_valuation_required")

    def test_numeric_family_stage_budgets_are_independent_and_persistent(self):
        # Runtime-only arithmetic contract; no model or financial qualification.
        from validation_runtime import StageRuntime, StageValidationFailure
        from contracts import ContractError
        store = Store(self.root, "case")
        store.begin("families", {"fixture":"frozen_runtime_input"})
        contracts = {"numeric_fit":{"name":"numeric.fit","version":"1"}}
        calls = []
        def rejected(value, inputs):
            return {"status":"failed","scope":"analytical_runtime_only",
                    "checks":{"independent_number":value["number"] == inputs["expected"]}}
        def accepted(value, inputs):
            return {"status":"passed" if value["number"] == inputs["expected"] else "failed",
                    "scope":"analytical_runtime_only","checks":{"independent_number":value["number"] == inputs["expected"]}}
        with store.lease("families"):
            runtime = StageRuntime(store, code_identity={"revision":"analytical"},
                validator_contracts=contracts, round_no=1)
            store._local.validation_runtime = runtime
            failed = pipeline._numeric_stage_runner(store, "a"*64)
            good = pipeline._numeric_stage_runner(store, "b"*64)
            def wrong():
                calls.append("a")
                return {"number":0}
            with self.assertRaises(StageValidationFailure):
                failed("numeric_fit", {"expected":1}, wrong, rejected)
            self.assertEqual(calls, ["a"]*3)
            self.assertEqual(len(runtime.evidence("numeric_fit@"+"a"*64)["attempts"]),3)
            self.assertEqual(good("numeric_fit", {"expected":1}, lambda:{"number":1}, accepted),{"number":1})
            self.assertEqual(len(runtime.evidence("numeric_fit@"+"b"*64)["attempts"]),1)
            # Recreating the callback/runtime for this same original operation
            # cannot replenish the exhausted first family's budget.
            resumed = StageRuntime(store, code_identity={"revision":"analytical"},
                validator_contracts=contracts, round_no=1)
            store._local.validation_runtime = resumed
            with self.assertRaises(StageValidationFailure):
                pipeline._numeric_stage_runner(store,"a"*64)("numeric_fit",{"expected":1},wrong,rejected)
            self.assertEqual(calls, ["a"]*3)
            with self.assertRaises(ContractError):
                pipeline._numeric_stage_runner(store,"invalid")("numeric_fit",{},wrong,rejected)
            self.assertEqual(calls, ["a"]*3)

    def test_settled_fill_cannot_be_credited_twice_across_archive(self):
        first = opening()
        first["data"] = {"cash": "0", "lots": [{"lot_id": "lot", "code": "000001", "shares": "100", "acquired_at": STAMP, "ownership_at": STAMP, "price_date": "2025-01-01"}],
                         "prices": {"000001": "10"}, "price_dates": {"000001": "2025-01-01"}}
        def event(identity, kind, data):
            return {"id": identity, "type": kind, "effective_at": STAMP, "known_at": STAMP, "recorded_at": STAMP, "data": data}
        order = {"order_id": "sell-order", "code": "000001", "side": "sell", "currency": "CNY", "lot_id": "lot",
                 "cash_limit": "0", "share_limit": "80", "fee_rate": "0", "settlement_days": 0,
                 "share_step": "0.0001", "context_hash": "a"*64}
        fill = event("fill-first", "sell_fill", {"order_id": "sell-order", "fill_id": "broker-fill-001",
             "shares": "20", "price": "10", "price_date": "2025-01-01", "fee": "0", "gross_amount": "200",
             "net_amount": "200", "settlement_at": STAMP, "final": False})
        settle = event("settle-first", "settlement", {"receivable_id": "broker-fill-001", "amount": "200"})
        self.run_request("first-trade", "feedback", {"currency": "CNY", "events": [
            first, event("reserve", "order_reserved", {"order": order}), fill, settle]})
        store = Store(self.root, "case")
        self.assertEqual(store.get("account", "main")["cash"], "200")
        with store.maintenance():
            store.archive()
        result = self.run_request("duplicate-trade", "feedback", {"currency": "CNY", "events": [
            {**fill, "id": "fill-again"}, {**settle, "id": "settle-again"}]})
        self.assertEqual(result["added"], [])
        self.assertEqual(store.get("account", "main")["cash"], "200")
        self.assertEqual(store.get("account", "main")["lots"]["lot"]["shares"], "80")
        self.assertEqual(len(list(store.scan("ledger_event"))), 4)
        changed = {**fill, "id": "conflicting-fill", "data": {**fill["data"], "shares": "30"}}
        with self.assertRaises(ConflictError):
            self.run_request("conflicting-trade", "feedback", {"currency": "CNY", "events": [changed]})

    def test_unimplemented_benchmark_is_rejected(self):
        from file_io import read_object
        from strategy import validate_spec
        spec = read_object(SCRIPTS.parent / "references/strategy.example.json")
        spec["benchmark"]["rule"] = "unimplemented_adaptive_rule"
        with self.assertRaisesRegex(ValueError, "fixed_weights"):
            validate_spec(spec)

    def test_unprojectable_receipt_is_preserved_and_blocks_account_once(self):
        self.run_request("open", "feedback", {"currency": "CNY", "events": [opening()]})
        receipt = {**opening(), "id": "incomplete-confirmation", "type": "buy_fill",
                   "data": {"fill_id": "provider-receipt-7", "shares": "10"}}
        payload = {"currency": "CNY", "events": [receipt]}
        for request_id in ("receipt-first", "receipt-second"):
            with self.assertRaises(pipeline.UnreconciledConfirmation):
                self.run_request(request_id, "feedback", payload)
        store = Store(self.root, "case")
        account = store.get("account", "main")
        self.assertEqual(account["cash"], "1000")
        self.assertEqual(account["lots"], {})
        self.assertEqual(len(account["unknown"]), 1)
        self.assertEqual(account["sequence"], 2)
        recorded = list(store.scan("unreconciled_fact"))
        self.assertEqual(len(recorded), 1)
        raw = Artifacts(store.base).read(recorded[0][1]["raw_ref"])
        self.assertEqual(strict_json_loads(raw.decode()), payload)

    def test_equivalent_timezone_does_not_repeat_a_split(self):
        first = opening()
        first["data"] = {"cash": "0", "lots": [{"lot_id": "lot", "code": "000001", "shares": "100", "acquired_at": STAMP, "ownership_at": STAMP, "price_date": "2025-01-01"}],
                         "prices": {"000001": "10"}, "price_dates": {"000001": "2025-01-01"}}
        split = {"id": "split", "type": "split", "effective_at": STAMP, "known_at": STAMP,
                 "recorded_at": STAMP, "data": {"code": "000001", "ratio": "2"}}
        self.run_request("split", "feedback", {"currency": "CNY", "events": [first, split]})
        again = {**split, "id": "split-UTC", "effective_at": "2025-01-01T04:00:00Z"}
        self.assertEqual(self.run_request("split-again", "feedback", {"currency": "CNY", "events": [again]})["added"], [])
        self.assertEqual(Store(self.root, "case").get("account", "main")["lots"]["lot"]["shares"], "200")


if __name__ == "__main__":
    unittest.main()
