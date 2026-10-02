"""Shared decision, account and execution regressions.

Storage partition roundtrips belong to Artifacts; this pure runner instead
proves canonical result roundtrips and calls the same ledger/compiler as live.
"""
import copy
import datetime as dt
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]/"skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
import allocation_runner as runner
import allocation_market as market_adapter
import single_step_wealth
import execution
import ledger
import strategy
from contracts import canonical_bytes, strict_json_loads
from test_ledger import opening, event, order, T0, T1, T2, T3
from test_strategy_execution import spec, market, calculation, build_context as financial_context, risk
from test_numerical_schema4 import terms as fee_terms
from contracts import fingerprint


def data(source_archives=False, source_objects=None):
    nav, features = {}, {}
    end = dt.date(2030, 1, 2)
    count = 360 if source_archives else 320
    for code, slope in (("000001", .001), ("000002", .0003)):
        nav[code], features[code] = [], []
        for n in range(count):
            day = (end-dt.timedelta(days=count-1-n)).isoformat()
            nav[code].append({"code": code, "date": day, "nav": 10+(n-count+1)*slope, "distribution_per_share": 0})
            features[code].append({"code": code, "feature_cutoff_nav_date": day, "known_max_nav_date": day,
                                   "values": dict.fromkeys(market_adapter.NAV_FEATURE_NAMES, 0)})
    values = {"nav": nav, "features": features, "code_info": {code: {"fund_group_id": code} for code in nav}}
    first = next(iter(nav.values()))[0]["date"]
    at = first+"T09:00:00+08:00"
    event = {"event_id": "synthetic-observed-fact", "revision_id": "synthetic-source-revision",
             "known_at": at, "first_seen_at": at, "published_at": at, "available_at": at,
             "event_type": "source_fact_presence", "evidence_refs": [{"synthetic_quote": "archived-fact"}]}
    values["industry"] = {"schema_version": 4, "news_ref": fingerprint({"synthetic_review": "archived"}),
        "news_state": {"event_frontier_hash": fingerprint({"synthetic_event": event})},
        "sectors": [{"sector_id": "synthetic-sector", "benchmark_id": "synthetic-price-index",
            "return_definition": "price_return", "currency": "CNY", "calendar_id": "synthetic-daily", "timezone": "Asia/Shanghai",
            "prices": [{"date": row["date"], "close": 100., "available_at": row["date"]+"T09:00:00+08:00",
                        "availability_basis": "explicit_synthetic_archive", "raw_ref": {"synthetic_observation": row["date"]}}
                       for row in next(iter(nav.values()))], "features": [event], "coverage": {"absence_proven": False}}]}
    values["industry_exposures"] = {code: [{"sector_id": "synthetic-sector", "weight": ".8", "as_of_date": first,
        "known_at": at, "basis": "explicit_synthetic_archived_disclosure", "evidence_refs": [{"synthetic_disclosure": code}]}] for code in nav}
    if source_archives:
        # Explicit original synthetic observations, never market/account proof.
        from test_industry_model import archive
        from research_data import bind_source_versions
        if source_objects is None:
            raise ValueError("Independent original engineering byte references required")
        template = archive(constant=True)
        sector = copy.deepcopy(template["sectors"][0])
        sector.update(sector_id="synthetic-sector", prices=values["industry"]["sectors"][0]["prices"], features=[], economic_observations=[])
        for index, price in enumerate(sector["prices"]):
            if index % 7:
                continue
            stamp = price["available_at"]
            source_event = copy.deepcopy(template["sectors"][0]["features"][0])
            source_event.update(event_id="event-"+price["date"], revision_id="revision-"+price["date"], source_revision_id="source-"+price["date"])
            source_event.update(dict.fromkeys(("known_at","first_seen_at","published_at","available_at","effective_at"),stamp))
            observation = copy.deepcopy(template["sectors"][0]["economic_observations"][0])
            observation.update(observation_id="metric-"+price["date"], observation_key="metric-key-"+price["date"], sector_ids=["synthetic-sector"],
                event_id=source_event["event_id"], revision_id=source_event["revision_id"], source_revision_id=source_event["source_revision_id"],
                known_at=stamp, assessed_at=stamp, event_at=stamp)
            sector["features"].append(source_event); sector["economic_observations"].append(observation)
        values["industry"] = {**template,"news_ref":values["industry"]["news_ref"], "sectors":[sector],
            "news_state":{"event_frontier_hash":fingerprint(sector["features"])}}
        for code, rows in nav.items():
            for index,row in enumerate(rows):
                primitive = {**row,"cumulative_nav":row["nav"]}
                raw_ref = source_objects.put_bytes(canonical_bytes(primitive))
                original = strict_json_loads(source_objects.read(raw_ref).decode("utf8"))
                stamp = row["date"]+"T09:00:00+08:00";source_id="synthetic-original-"+code+"-"+row["date"]
                metadata = {"source_id":source_id,"sha256":raw_ref["sha256"],"retrieved_at":stamp,"available_at":stamp,
                    "availability_evidence":{"kind":"archived_original_capture","source_id":source_id,"raw_sha256":raw_ref["sha256"],
                        "captured_at":stamp,"raw_artifact_ref":raw_ref}}
                rows[index] = bind_source_versions([original],metadata)[0]
        values["industry_exposures"] = {code:[{**rows[0],"basis":"historical_disclosure","role":"single_sector","source_label":"synthetic-sector",
            "actual_exposure_status":"historical_disclosed_not_current","actual_weight":".8","stale":True}] for code,rows in values["industry_exposures"].items()}
    return values


def industry_ref(values):
    return {"bundle_hash": fingerprint(values["industry"]), "news_ref": fingerprint(values["industry"]["news_ref"]),
            "event_frontier_hash": values["industry"]["news_state"]["event_frontier_hash"]}


def build_context(selected, state, at, reference, **kwargs):
    import industry_model
    selected = copy.deepcopy(selected)
    selected["industry"]["training"] = {**selected["training"], "feature_names": industry_model.FEATURE_NAMES}
    kwargs.setdefault("industry_data_ref", industry_ref(data()))
    return financial_context(selected, state, at, reference, **kwargs)


def clock_for_tests():
    state, _ = opening()
    return single_step_wealth.build_clock_context(build_context(spec(), state, T1, market()))


def fitted():
    return {"status": "research_ready", "forecasts": [{"code": code, "feature_cutoff_date": "2030-01-02"} for code in ("000001", "000002")], "joint_scenarios": {"codes": ["000001", "000002"],
             "returns": [[.1, .02], [.08, .03]], "probabilities": [.5, .5], "dates": ["2028-01-01", "2028-03-01"], "targets": {"pricing": [[1,1],[1,1]], "terminal_nav": [[1.1,1.02],[1.08,1.03]], "hold": [[1.1,1.02],[1.08,1.03]], "buy": [[1.1,1.02],[1.08,1.03]], "sell": [[1,1],[1,1]]}, "clock_context": clock_for_tests(), "current_point_targets": {"pricing": [[1,1]], "terminal_nav": [[1.09,1.025]], "hold": [[1.09,1.025]], "buy": [[1.09,1.025]], "sell": [[1,1]]}, "source_oos": []}}


class AllocationRunnerTests(unittest.TestCase):
    def test_mpc_cash_gap_actions_survive_current_order_blocking(self):
        import industry_binding
        import portfolio_mpc
        values = data()
        state, _ = opening()
        ctx = build_context(spec(), state, T1, market(), industry_data_ref=industry_ref(values))
        action = {"action": "revise_cash_requirement_or_complete_source_cash_path", "required_amount": "2000"}
        partial = {"status": "partial", "context_hash": ctx["context_hash"], "funding_options": [],
                   "reason": "cash_deadline_requirement_not_established", "required_actions": [action]}
        sample = {"decision_date": ctx["as_of"], "industry_ready": True}
        stage_results = {
            "numeric_samples": [sample],
            "numeric_industry": {"ready_sector_ids": ["synthetic-sector"]},
            "numeric_industry_bridge": [sample],
            "numeric_fit": {"status": "research_ready"},
            "numeric_paths": {"status": "research_ready"},
            "numeric_comparison": {"funding_options": []},
            "numeric_mpc": partial}
        def isolated_stage(name, inputs, producer, validator):
            return copy.deepcopy(stage_results[name]) if name in stage_results else producer()
        scope = {"industry": values["industry"], "missing_exposure_codes": [],
                 "required_current_sector_ids": ["synthetic-sector"]}
        with patch.object(industry_binding, "family_industry_scope", return_value=scope), \
             patch.object(portfolio_mpc, "as_current_order_distribution", return_value={}):
            result = runner.predict(ctx, values, stage_runner=isolated_stage)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["orders"]["orders"], [])
        self.assertFalse(result["trade_ready"])
        self.assertEqual(result["reason"], partial["reason"])
        self.assertEqual(result["required_actions"], [action])

    def test_current_material_industry_gap_prevents_model_and_trade(self):
        values = data()
        values["industry"]["required_actions"] = [{"action": "supply_official_benchmark_identity", "reason": "unverified_current_source"}]
        state, _ = opening()
        ctx = build_context(spec(), state, T1, market(), industry_data_ref=industry_ref(values))
        with patch.object(runner, "fit_at", side_effect=AssertionError("No model may override a source gap")) as fitted_model:
            result = runner.predict(ctx, values)
        fitted_model.assert_not_called()
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["trade_ready"])
        self.assertEqual(result["orders"]["orders"], [])
        self.assertEqual(result["required_actions"], values["industry"]["required_actions"])

    def test_calendar_preparation_supplies_actual_holiday_origin_without_relabeling(self):
        values = data()
        samples = runner.prepare_samples(values["nav"], values["features"], values["code_info"], 3, 1, "2030-01-06", clock_for_tests())
        rows = runner.prediction_rows(samples, ["000001", "000002"], "2030-01-06", 7)
        self.assertTrue(all(row["decision_date"] == "2030-01-06" and row["x"][-1] == 4 for row in rows))
        current = [row for row in samples if row["decision_date"] == "2030-01-06"]
        self.assertTrue(all(row["target_not_before"] == "2030-01-09" for row in current))
        self.assertTrue(all(row["return"] is None for row in current))

    def test_calendar_gap_cannot_relabel_an_old_forecast_as_a_new_full_horizon(self):
        values = data()
        samples = runner.prepare_samples(values["nav"], values["features"], values["code_info"], 3, 1, "2030-01-03", clock_for_tests())
        # Jan 6 is within the allowed feature age but the last decision origin
        # is Jan 3. Its fixed Jan 6 endpoint is not a new Jan 9 endpoint.
        with self.assertRaisesRegex(ValueError, "origin does not match"):
            runner.prediction_rows(samples, ["000001"], "2030-01-06", 10)

    def test_rebase_uses_current_nav_and_removes_already_owned_cash_distributions(self):
        fitted_value = {"forecasts": [{"code": "A", "feature_cutoff_date": "2030-01-01"}],
                        "joint_scenarios": {"codes": ["A"], "returns": [[.5], [0]], "probabilities": [.5, .5], "targets": {"pricing": [[1],[1]], "terminal_nav": [[1.4],[.9]], "hold": [[1.5],[1]], "buy": [[1.4],[.9]], "sell": [[1.1],[1.1]]}}}
        context_value = {"as_of": "2030-01-03", "market_ref": {"price_dates": {"A": "2030-01-02"}, "prices": {"A": "2.4"}}}
        values = {"nav": {"A": [{"date": "2030-01-01", "nav": 2, "distribution_per_share": 0},
                                   {"date": "2030-01-02", "nav": 2.4, "distribution_per_share": .2}]}}
        result = runner._rebase_targets(copy.deepcopy(fitted_value), context_value, values)
        self.assertAlmostEqual(result["joint_scenarios"]["returns"][0][0], 1/6)
        self.assertAlmostEqual(result["joint_scenarios"]["returns"][1][0], -.25)
        self.assertAlmostEqual(result["forecasts"][0]["expected_gross_return"], -1/24)
        context_value["market_ref"]["prices"]["A"] = "2.5"
        with self.assertRaisesRegex(ValueError, "valuation differs"):
            runner._rebase_targets(copy.deepcopy(fitted_value), context_value, values)

    def test_execution_roundoff_cannot_overspend_or_oversell(self):
        state, _ = opening("9991.006508946753")
        ctx = build_context(spec(), state, T1, market())
        with self.assertRaisesRegex(ValueError,"source precision"):
            execution._round_trades({"buys": [{"code": "000001", "cash_debit": "9991.00650903169"}], "sells": []}, ctx, [])
        orders, _, _ = execution._round_trades({"buys": [{"code": "000001", "cash_debit": "9991.00"}], "sells": []}, ctx, [])
        self.assertEqual(orders[0]["cash_limit"], "9991")
        self.assertLessEqual(Decimal(orders[0]["cash_limit"]), Decimal(state["cash"]))
        state, _ = opening("0", "100")
        with self.assertRaisesRegex(ValueError, "free shares"):
            ledger.apply_event(state, event(state, "order_reserved", {"order": order("sell", "100.00000001")}))

    def test_initial_snapshot_cannot_use_unpublished_start_nav(self):
        state, _ = opening("0", "100")
        future = event(state, "valuation", {"prices": {"000001": "20"}}, T1, T2)
        next_state = ledger.apply_event(state, future)
        with self.assertRaisesRegex(ValueError, "Historical snapshot"):
            ledger.snapshot(next_state, T1)
        prior = ledger.rebuild(state, [future], T1)
        self.assertEqual(ledger.snapshot(prior, T1)["equity"], "1000")
        self.assertEqual(prior["lots"]["old"]["shares"], "100")

    def test_reserved_projection_does_not_collide_with_user_ids(self):
        state, _ = opening("0", "100")
        state["lots"]["old:reserved"] = {**state["lots"]["old"], "lot_id": "old:reserved", "shares": "10"}
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order("sell", "10")}))
        ctx = build_context(spec(), state, T1, market())
        lots = ctx["model_request"]["account"]["positions"]
        self.assertEqual(len(lots), len({row["lot_id"] for row in lots}))
        self.assertEqual(sum(row["value"] for row in lots), 1100)

    def test_invalid_inputs_fail_before_fitting(self):
        state, _ = opening()
        selected = spec()
        with patch.object(runner, "fit_at") as fit:
            for levels in (["-100", "0"], ["1000"]):
                invalid = copy.deepcopy(selected); invalid["allocation"]["funding_levels"] = levels
                with self.assertRaises(ValueError):
                    build_context(invalid, state, T1, market())
            negative = {**state, "cash": "-100"}
            negative_context = build_context(selected, negative, T1, market())
            self.assertTrue(negative_context["blocked"])
            self.assertEqual(runner.predict(negative_context, data())["status"], "blocked")
            context = build_context(selected, state, T1, market())
            context["model_request"]["account"]["cash"] = 100000
            with self.assertRaises(ValueError):
                runner.predict(context, data())
            fit.assert_not_called()

    def test_opening_lot_cannot_override_projection_fields(self):
        for key in ("reserved", "confirmed", "known_invested_value"):
            state = ledger.initial_state("CNY")
            lot = {"lot_id": "old", "code": "000001", "shares": "1", "acquired_at": T0, key: "1"}
            with self.subTest(key=key), self.assertRaises(ValueError):
                ledger.apply_event(state, event(state, "opening", {"cash": "0", "lots": [lot], "prices": {"000001": "10"}}, T0))

    def test_lost_principal_is_not_reset_by_a_hypothetical_contribution(self):
        from allocation import build_source_actions, funded_context
        from test_allocation import source_context
        ctx=source_context(cash="0",funding=(0,1000))
        ctx["risk_state"].update(net_principal="1000",loss_tolerance=".25",principal_floor="750",remaining_loss_budget="-750")
        comparison=build_source_actions(ctx)
        self.assertEqual([group["capital_before_trade"] for group in comparison["funding_options"]],["0","1000"])
        funded=funded_context(ctx,1000)
        self.assertEqual(funded["risk_state"]["net_principal"],"2000")
        self.assertEqual(Decimal(funded["risk_state"]["principal_floor"]),Decimal("1500"))
        self.assertEqual(Decimal(funded["risk_state"]["remaining_loss_budget"]),Decimal("-500"))
        self.assertEqual(ctx["snapshot"]["available_cash"],"0")
        self.assertFalse(any(action["buys"] for action in comparison["funding_options"][0]["actions"]))

    def test_confirmed_buy_cannot_overwrite_existing_lot(self):
        state, _ = opening("1000", "100")
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order(value="100")}))
        with self.assertRaisesRegex(ValueError, "fresh lot ID"):
            ledger.apply_event(state, event(state, "buy_fill", {"order_id": "order-one", "fill_id": "f",
                "lot_id": "old", "shares": "10", "price": "10", "fee": "0", "final": True}, T2))
        self.assertEqual(state["lots"]["old"]["shares"], "100")

    def test_adapter_supplies_ordered_features_and_fixed_lag(self):
        state, _ = opening()
        ctx = build_context(spec(), state, T1, market())
        values = data()
        rows = runner.prepare_samples(values["nav"], values["features"], values["code_info"], 3, 2, "2030-01-02", clock_for_tests())
        with patch("allocation_statistics.fit_joint_targets", return_value={"status": "insufficient_evidence"}) as fit:
            runner.fit_at(rows, ctx["allocation_codes"], ctx, clock_for_tests())
        self.assertEqual(fit.call_args.args[2]["feature_names"], market_adapter.FEATURE_NAMES)
        selected = spec(); selected["training"]["feature_names"] = ["unrelated"]
        with self.assertRaisesRegex(ValueError, "features"):
            strategy.validate_spec(selected)

    def test_proposed_sale_cannot_fund_buy_before_actual_settlement(self):
        state, _ = opening("0", "100")
        reserve = {**order("sell", "100"), "fee_rate": "0.01", "settlement_days": 3}
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": reserve}))
        state = ledger.apply_event(state, event(state, "sell_fill", {"order_id": "order-one", "fill_id": "sale",
            "shares": "100", "price": "9", "fee": "9", "settlement_at": "2030-01-06T00:00:00Z", "final": True}, T2))
        self.assertEqual((state["cash"], ledger.snapshot(state, T2)["unsettled_cash"]), ("0", "891"))
        with self.assertRaisesRegex(ValueError, "settled free cash"):
            ledger.apply_event(state, event(state, "order_reserved", {"order": order(value="891")}, T2))
        paid_at = "2030-01-06T00:00:00Z"
        state = ledger.apply_event(state, event(state, "settlement", {"receivable_id": "sale"}, paid_at))
        self.assertEqual(state["cash"], "891")

    def test_unconfirmed_units_block_rather_than_reveal_unknown_execution_nav(self):
        state, _ = opening()
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order(value="900")}))
        state = ledger.apply_event(state, event(state, "subscription_pending", {"order_id": "order-one", "payment_id": "p", "amount": "900"}, T2))
        snap = ledger.snapshot(state, T2, {"000001": "9"})
        self.assertTrue(snap["blocked"]); self.assertIsNone(snap["unit_nav"])
        self.assertEqual(snap["positions"], [])
        self.assertEqual(state["cash"], "100")

    def test_additional_principal_is_not_profit(self):
        state, _ = opening()
        before = copy.deepcopy(state)
        state = ledger.apply_event(state, event(state, "cashflow", {"amount": "2000"}))
        self.assertEqual((state["cash"], ledger.observe(state, T1, {}, "1")["net_return"]), ("3000", "0"))
        self.assertEqual(before["cash"], "1000")

    def test_fee_changes_by_age_and_lot_and_invalid_rates_are_rejected(self):
        terms = {"redemption_schedule": [{"minimum_days": 0, "rate": "0.015"}, {"minimum_days": 7, "rate": "0"}]}
        self.assertEqual(ledger.redemption_rate(terms, T0, "2030-01-07T00:00:00Z"), "0.015")
        self.assertEqual(ledger.redemption_rate(terms, T0, "2030-01-08T00:00:00Z"), "0")
        with self.assertRaises(ValueError):
            ledger.redemption_rate({"redemption_fee": "1"}, T0, T1)
        state = ledger.initial_state("CNY")
        lots = [{"lot_id": identity, "code": "000001", "shares": "100", "acquired_at": acquired, "ownership_at": acquired, "price_date": T0[:10]}
                for identity, acquired in (("old", "2029-01-01T00:00:00Z"), ("young", T0))]
        state = ledger.apply_event(state, event(state, "opening", {"cash": "0", "lots": lots, "prices": {"000001": "10"}}, T0))
        ref = market(); ref["terms"][0]["redemption_schedule"] = terms["redemption_schedule"]
        ctx = build_context(spec(), state, T1, ref)
        rates = {row["lot_id"]: row["redemption_fee"] for row in ctx["model_request"]["account"]["positions"]}
        self.assertEqual(rates, {"old": 0.0, "young": .015})
        state, _ = opening("0", "10")
        contract = fee_terms("000001"); contract["redemption"] = {"kind": "holding_tiers", "bands": [{"minimum": "0", "fee": {"kind": "percentage", "rate": ".015"}}, {"minimum": "7", "fee": {"kind": "percentage", "rate": "0"}}]}
        quoted = {**order("sell", "10"), "fee_rate": "0.015", "redemption_schedule": terms["redemption_schedule"], "fee_contract": contract}
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": quoted}, "2030-01-07T00:00:00Z"))
        actual = execution.simulate_events(state, [quoted], {"000001": "10"}, "2030-01-08T00:00:00Z", price_dates={"000001": "2030-01-08"})
        self.assertEqual(actual[0]["data"]["fee"], "0")
        state = ledger.apply_event(state, actual[0])
        self.assertEqual(ledger.snapshot(state, "2030-01-08T00:00:00Z")["equity"], "100")

    def test_samples_exclude_account_fees_and_features_are_fresh_at_actual_decision(self):
        values = data()
        rows = runner.prepare_samples(values["nav"], values["features"], values["code_info"], 3, 2, "2030-01-02", clock_for_tests())
        selected = runner.prediction_rows(rows, ["000001", "000002"], "2030-01-02", 10)
        self.assertTrue(all(row["feature_cutoff_date"] <= "2029-12-31" for row in selected))
        self.assertTrue(all("fee" not in row for row in rows))
        with self.assertRaisesRegex(ValueError, "stale"):
            runner.prediction_rows(rows, ["000001"], "2031-01-01", 10)

    def test_daily_replay_calls_same_predict_and_preserves_contributions_once(self):
        state, _ = opening()
        flow = event(state, "cashflow", {"amount": "500"}, T1)
        ref2 = market(); ref2["observed_at"] = T2
        steps = [{"decision_at": T1, "market_ref": market(), "data": data(), "events": [flow], "risk_state": risk(ledger.apply_event(state, flow), T1), "purchase_eligible_codes": list(market()["prices"])},
                 {"decision_at": T2, "market_ref": ref2, "data": data(), "events": [], "risk_state": risk(ledger.apply_event(state, flow), T2, ref2), "purchase_eligible_codes": list(market()["prices"])}]
        with patch.object(runner, "fit_at", return_value={"status": "insufficient_evidence"}), patch.object(runner, "predict", wraps=runner.predict) as same:
            result = runner.replay(spec(), state, steps)
        self.assertEqual(same.call_count, 2)
        self.assertEqual(result["state"]["cash"], "1500")
        self.assertEqual(sum(row["type"] == "cashflow" for row in result["events"]), 1)
        self.assertTrue(all(row["net_return"] == "0" for row in result["observations"]))
        self.assertEqual(strict_json_loads(canonical_bytes(result)), result)

    def test_live_and_replay_compile_identical_orders(self):
        state, _ = opening(); ctx = build_context(spec(), state, T1, market())
        with patch.object(runner, "fit_at", return_value=fitted()):
            live = runner.predict(ctx, data())
            replayed = runner.replay(spec(), state, [{"decision_at": T1, "market_ref": market(), "data": data(), "events": [],
                                                     "risk_state": risk(state, T1), "purchase_eligible_codes": list(market()["prices"])}])
        self.assertEqual(replayed["decisions"][0]["calculation"], live)
        self.assertEqual(replayed["state"]["cash"], state["cash"])
        self.assertFalse(replayed["state"]["orders"])  # No invented calibration evidence in this direct fit fixture.

    def test_real_fit_uses_the_shared_compiler(self):
        selected = spec()
        selected["planning"]["primary_horizon_days"] = 3
        selected["training"].update(alpha_grid=[.001], l1_ratio_grid=[.5], cv_folds=2)
        state, _ = opening()
        import tempfile
        from artifacts import Artifacts
        original_sources = tempfile.TemporaryDirectory()
        self.addCleanup(original_sources.cleanup)
        values = data(source_archives=True,source_objects=Artifacts(original_sources.name))
        ctx = build_context(selected, state, T1, market(), industry_data_ref=industry_ref(values))
        result = runner.predict(ctx, values)
        self.assertEqual(result["status"], "research_ready", result.get("reason"))
        self.assertEqual(result["orders"]["status"], "no_action")
        self.assertEqual(result["orders"]["orders"], [])
        self.assertTrue(all(row["expected_gross_return"] < selected["allocation"]["minimum_advantage"]
                            for row in result["fitting"]["forecasts"]))
        self.assertEqual(result["return_basis"], "source_multi_date_nonanticipative_rolling_policy_current_action_only")


if __name__ == "__main__":
    unittest.main()
