import copy
import json
import hashlib
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]/"skills/investment/scripts"
sys.path.insert(0, str(SCRIPTS))
import allocation
import execution
import ledger
import strategy
from contracts import fingerprint
from test_ledger import opening, event, order, T0, T1, T2, T3, T4
from test_numerical_schema4 import terms
import single_step_wealth
import newtrade_guard
import allocation_market


def spec():
    value = json.loads((SCRIPTS.parent/"references/strategy.example.json").read_text())
    value["benchmark"]["weights"] = {"000001": "0.5", "000002": "0.5"}
    # Explicit engineering registration; example preferences are never silently
    # promoted by the production parser. These fixtures span their test clocks.
    value["trade_policy"]["registration"] = "declared"
    value["trade_policy"]["family"].update(start_at="2020-01-01T00:00:00Z", end_at="2032-01-01T00:00:00Z")
    return value


def market():
    value = json.loads((SCRIPTS.parent/"references/market.example.json").read_text())
    value["prices"] = {code: "10" for code in value["prices"]}
    value["price_dates"] = {code: T1[:10] for code in value["prices"]}
    value["terms_basis"] = "source_verified"
    from verify import code_identity
    value["source_hashes"] = code_identity()
    for row in value["terms"]:
        row["fee_contract"] = terms(row["code"], step="0.00000001")
        row["fee_contract"].update(order_cutoff_local="15:00:00", confirmation={"lag_days":1,"day_basis":"trading_days"})
        row["fee_contract_hash"] = fingerprint(row["fee_contract"])
        row["confirmed_execution_max_calendar_days"] = 1
        row["execution_bound_source"] = {"fixture": "explicit_one_day_confirmation_terms"}
    return capture_engineering_marks(value)


def capture_engineering_marks(reference):
    """Actual known_mark producer over explicit in-memory engineering captures.

    These bytes describe this fixture, with no historical source/PIT claim.
    Changing a synthetic quote requires a new capture identity.
    """
    reference = copy.deepcopy(reference)
    captures = {}
    for code,price in reference["prices"].items():
        original = {"code":code,"date":reference["price_dates"][code],"nav":float(price),
            "observed_at":reference["observed_at"],"scope":"synthetic_engineering_quote"}
        raw = json.dumps(original,sort_keys=True,separators=(",",":")).encode("utf-8")
        version = {**original,"raw_ref":{"source_id":"synthetic-engineering-NAV-"+code,
            "sha256":hashlib.sha256(raw).hexdigest()}}
        version["version_id"] = fingerprint(version)
        row = {"code":code,"date":original["date"],"source_versions":[version]}
        captures[code] = allocation_market.known_mark(row,reference["observed_at"])
    reference["known_marks"] = captures
    return reference


def risk(state, at=T1, reference=None, principal=None, tolerance="0.1"):
    reference = reference or market()
    value = Decimal(ledger.snapshot(state, at, reference["prices"], reference["price_dates"])["equity"] or "0")
    principal = Decimal(principal) if principal is not None else (value if value > 0 else Decimal("1000"))
    floor = principal*(1-Decimal(tolerance))
    return {"profile_hash": "c"*64, "capital_baseline": {"amount": str(principal), "fixture": True},
            "net_principal": str(principal), "loss_tolerance": tolerance, "principal_floor": str(floor),
            "remaining_loss_budget": str(value-floor), "currency": "CNY", "as_of": at, "valid_until": None}


def build_context(selected, state, at, reference, **kwargs):
    kwargs.setdefault("trade_state", newtrade_guard.derive_state(state, [], at, selected["trade_policy"]["economic"]["fee_window_days"], account_id=kwargs.get("account_id","main")))
    kwargs.setdefault("trade_family_review_index",1)
    return strategy.build_context(selected, state, at, reference,
                                  purchase_eligible_codes=kwargs.pop("purchase_eligible_codes", list(reference["prices"])),
                                  risk_state=kwargs.pop("risk_state", risk(state, at, reference)), **kwargs)




def calculation(context, distribution=None):
    """No source/PIT paths in this primitive fixture: no invented calibration."""
    reason = "independent_source_path_calibration_required"
    return {"status": "blocked" if context["blocked"] else "partial", "context_hash": context["context_hash"],
            "reason": reason, "comparison": {"funding_options": []}, "paths": {"status": "partial", "trade_ready": False},
            "mpc": {"status": "partial", "context_hash": context["context_hash"], "reason": reason,
                    "funding_options": [], "selected_policy": None, "current_action": {"buys": [], "sells": []}}}


class StrategyExecutionTests(unittest.TestCase):
    def test_confirmed_receivable_context_keeps_arrival_separate_from_current_cash(self):
        state, _ = opening("100", "10")
        declared = event(state, "dividend_declared", {"distribution_id": "div", "code": "000001",
            "per_share": "1", "pay_at": T3[:10], "record_at": T0, "entitled_shares": "10"})
        state = ledger.apply_event(state, declared)
        context = build_context(spec(), state, T1, market())
        self.assertFalse(context["blocked"])
        self.assertEqual(context["receivables"], context["snapshot"]["receivables"])
        self.assertEqual(context["receivables"][0]["due_at"], T3[:10])
        self.assertEqual(context["receivables"][0]["source_identity"], declared["data"]["distribution_id"])
        self.assertEqual(context["snapshot"]["available_cash"], "100")
        self.assertEqual(context["model_request"]["account"]["cash"], 100.)

    def test_compiler_preserves_exact_share_quantity_through_repricing(self):
        quantity = "123456.12345678"
        state, _ = opening("0", quantity)
        reference = market(); reference["prices"]["000001"] = "1.2345"
        reference = capture_engineering_marks(reference)
        ctx = build_context(spec(), state, T1, reference)
        raw = Decimal(quantity)*Decimal("1.2345")
        candidate = {"buys": [], "sells": [{"lot_id": "old", "shares": quantity}]}
        orders, sales, _ = execution._round_trades(candidate, ctx, [])
        self.assertEqual(orders[0]["share_limit"], quantity)
        self.assertEqual(Decimal(sales["old"]), raw)
        projected = single_step_wealth.project(ctx, {"buys": [], "sells": [{"lot_id": "old", "shares": orders[0]["share_limit"]}]},
                                           {"codes": ctx["allocation_codes"], "returns": [[0., 0.]], "probabilities": [1.]},
                                           pricing_nav={"000001":"2.3456","000002":"10"})
        self.assertEqual(Decimal(projected["sells"][0]["shares"]), Decimal(quantity))
        self.assertEqual(Decimal(str(projected["sells"][0]["gross_value"])),
                         (Decimal(quantity)*Decimal("2.3456")).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))

    def test_compiler_groups_fifo_lots_before_source_minimum(self):
        state,_=opening("0","3")
        state["lots"]["new"]={**state["lots"]["old"],"lot_id":"new","shares":"5","acquired_at":T1}
        reference=market()
        contract=reference["terms"][0]["fee_contract"]
        contract.update(redemption_allocation={"method":"fifo"},minimum_redemption_shares="4",minimum_remaining_shares="2",
            redemption_fee_application="per_lot",redemption_fee_application_source={"synthetic_original_fee_scope":"each_batch"},
            redemption_rounding_application="per_lot",redemption_rounding_application_source={"synthetic_original_rounding_scope":"each_batch"})
        reference["terms"][0]["fee_contract_hash"]=fingerprint(contract)
        ctx=build_context(spec(),state,T2,reference)
        action={"buys":[],"sells":[{"lot_id":"old","shares":"3"},{"lot_id":"new","shares":"1"}]}
        orders,_,_=execution._round_trades(action,ctx,[])
        requests=execution._redemption_requests(orders)
        self.assertEqual(requests[0]["shares"],"4")
        self.assertEqual(requests[0]["lot_allocations"],action["sells"])
        self.assertEqual(len(requests),1)
        contract["redemption"]={"kind":"fixed","amount":"1"}
        contract.pop("redemption_fee_application_source")
        reference["terms"][0]["fee_contract_hash"]=fingerprint(contract)
        ctx=build_context(spec(),state,T2,reference)
        with self.assertRaisesRegex(ValueError,"multi-lot redemption fee application"):
            execution._round_trades(action,ctx,[])

    def test_simulated_net_rounding_keeps_contractual_fee_and_exact_gross(self):
        state,_=opening("0","8")
        reference=market()
        contract=reference["terms"][0]["fee_contract"]
        contract["redemption"]={"kind":"percentage","rate":"0"}
        contract["trade_precision"].update(fee_rounding="unrounded",redemption_net_rounding="down_fund")
        reference["terms"][0]["fee_contract_hash"]=fingerprint(contract)
        ctx=build_context(spec(),state,T1,reference)
        orders,_,_=execution._round_trades({"buys":[],"sells":[{"lot_id":"old","shares":"8"}]},ctx,[])
        for row in execution.reserve_events(state,orders,T1):state=ledger.apply_event(state,row)
        fills=execution.simulate_events(state,orders,{"000001":"1.2345"},T2,price_dates={"000001":T2[:10]})
        self.assertEqual(fills[0]["data"]["gross_amount"],"9.876")
        self.assertEqual(fills[0]["data"]["fee"],"0")
        self.assertEqual(fills[0]["data"]["net_amount"],"9.87")
        state=ledger.apply_event(state,fills[0])
        self.assertEqual(next(iter(state["receivables"].values()))["amount"],"9.87")
        self.assertEqual(state["reconciliation"],[])

    def test_unheld_stale_contract_is_isolated_and_cannot_refresh_itself(self):
        state, _ = opening()
        reference = market()
        reference["terms"][0]["observed_at"] = "2029-01-01T00:00:00Z"
        ctx = build_context(spec(), state, T1, reference)
        self.assertFalse(ctx["blocked"])
        self.assertEqual(ctx["allocation_codes"], ["000002"])
        self.assertEqual(ctx["stale_term_codes"], ["000001"])
        self.assertEqual(ctx["term_gaps"]["000001"]["observed_at"], "2029-01-01T00:00:00Z")
        held, _ = opening("0", "10")
        self.assertTrue(build_context(spec(), held, T1, reference)["blocked"])

    def test_pending_sale_of_a_hedge_blocks_new_quantitative_orders(self):
        state, _ = opening("100", "10")
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order("sell", "5")}))
        ctx = build_context(spec(), state, T1, market())
        self.assertTrue(ctx["blocked"])
        self.assertIn("pending_order:order-one", ctx["reasons"])
        self.assertEqual(execution.compile_orders(calculation(ctx), ctx, {})["orders"], [])

    def test_assisted_override_cannot_replace_source_path_qualification(self):
        state,_ = opening()
        selected = spec(); selected["kind"] = "assisted_workflow"; selected["intervention"]["mode"] = "bounded_override"; selected["model"].update(provider_model="declared-unit-analysis@1", prompt_hash="a"*64)
        context = build_context(selected,state,T1,market(),purchase_eligible_codes=["000002"])
        result = calculation(context)
        plan = execution.compile_orders(result,context,{})
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["reason"], "independent_source_path_calibration_required")

    def test_assumed_terms_cannot_qualify_strategy_or_passive_orders(self):
        state, _ = opening()
        reference = market(); reference["terms_basis"] = "assumed"
        with self.assertRaisesRegex(ValueError, "Source-verified"):
            build_context(spec(), state, T1, reference)
        with self.assertRaisesRegex(ValueError, "source-verified"):
            execution.compile_benchmark(spec(), state, T1, reference, account_id="trial:benchmark", initial_allocation=True)

    def test_derived_floor_preserves_more_than_twelve_decimal_places(self):
        state, _ = opening("123.123456789012")
        resolved = risk(state, tolerance="0.123456789012")
        ctx = build_context(spec(), state, T1, market(), risk_state=resolved)
        expected = Decimal("123.123456789012")*(1-Decimal("0.123456789012"))
        self.assertEqual(Decimal(ctx["risk_state"]["principal_floor"]), expected)

    def test_passive_benchmark_buys_from_cash_and_obeys_its_frozen_calendar(self):
        state, _ = opening()
        plan = execution.compile_benchmark(spec(), state, T1, market(), account_id="trial:benchmark", initial_allocation=True)
        self.assertEqual([(row["code"], row["cash_limit"]) for row in plan["orders"]],
                         [("000001", "500"), ("000002", "500")])
        for event_row in execution.reserve_events(state, plan["orders"], T1):
            state = ledger.apply_event(state, event_row)
        for event_row in execution.simulate_events(state, plan["orders"], market()["prices"], T2, price_dates={code:T2[:10] for code in market()["prices"]}):
            state = ledger.apply_event(state, event_row)
        self.assertEqual(ledger.snapshot(state, T2)["equity"], "1000")
        next_market = market(); next_market["observed_at"] = T2
        later = execution.compile_benchmark(spec(), state, T2, next_market, account_id="trial:benchmark")
        self.assertEqual(later["orders"], [])

    def test_missing_model_error_evidence_cannot_publish_buy_orders(self):
        state,_=opening("10000")
        ctx=build_context(spec(),state,T1,market())
        result=calculation(ctx)
        plan=execution.compile_orders(result,ctx,{})
        self.assertEqual(plan["orders"],[])
        self.assertTrue(all(not candidate.get("trade_guard",{}).get("eligible",False)
                            for group in result["comparison"]["funding_options"]
                            for candidate in group["candidates"] if candidate["buys"]))


    def test_resolved_principal_budget_must_match_current_equity(self):
        state, _ = opening("8500")
        resolved = risk(state, principal="10000", tolerance="0.25")
        ctx = build_context(spec(), state, T1, market(), risk_state=resolved)
        self.assertEqual(ctx["risk_state"]["remaining_loss_budget"], "1000")
        resolved["remaining_loss_budget"] = "2500"
        with self.assertRaisesRegex(ValueError, "current marked equity"):
            build_context(spec(), state, T1, market(), risk_state=resolved)

    def test_context_derives_account_and_actual_date_and_rejects_future_or_foreign_currency(self):
        state, _ = opening()
        ctx = build_context(spec(), state, T1, market(), account_id="trial:one")
        self.assertEqual(ctx["account_id"], "trial:one")
        self.assertEqual(ctx["as_of"], "2030-01-02")
        self.assertEqual(ctx["model_request"]["account"]["cash"], 1000)
        self.assertEqual(ctx["account_hash"], fingerprint(state))
        with self.assertRaisesRegex(ValueError, "future or stale|already known"):
            build_context(spec(), state, T0, market())
        with self.assertRaises(ValueError):
            build_context(spec(), state, T1, {**market(), "currency": "USD"})
        with self.assertRaises(ValueError):
            strategy.validate_spec({**spec(), "as_of": "2020-01-01"})

    def test_context_reserved_lot_aliases_cannot_collide(self):
        state, _ = opening("0", "10")
        first = {**state["lots"]["old"], "lot_id": "old:reserved", "shares": "5"}
        state["lots"][first["lot_id"]] = first
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order("sell", "5")}))
        ctx = build_context(spec(), state, T1, market())
        identities = [row["lot_id"] for row in ctx["model_request"]["account"]["positions"]]
        self.assertEqual(len(identities), len(set(identities)))

    def test_rounding_funding_and_fill_use_one_ledger(self):
        state, _ = opening()
        ctx = build_context(spec(), state, T1, market())
        before = copy.deepcopy(state)
        plan = execution.compile_benchmark(spec(), state, T1, market(), account_id="unit:ledger", initial_allocation=True)
        self.assertEqual(plan["status"], "ready")
        self.assertLessEqual(sum(Decimal(row["cash_limit"]) for row in plan["orders"]), Decimal("1000"))
        for row in plan["orders"]:
            self.assertEqual(Decimal(row["cash_limit"]) % Decimal(".01"), 0)
        for row in execution.reserve_events(state, plan["orders"], T1):
            state = ledger.apply_event(state, row)
        for row in execution.simulate_events(state, plan["orders"], market()["prices"], T2, price_dates={code:T2[:10] for code in market()["prices"]}):
            state = ledger.apply_event(state, row)
        self.assertEqual(ledger.snapshot(state, T2)["equity"], "1000")
        self.assertEqual(before["cash"], "1000")

    def test_changed_context_and_unsettled_cash_cannot_generate_spend(self):
        state, _ = opening("0", "10")
        state = ledger.apply_event(state, event(state, "order_reserved", {"order": order("sell", "10")}))
        state = ledger.apply_event(state, event(state, "sell_fill", {"order_id": "order-one", "fill_id": "sale", "shares": "10", "price": "10",
            "fee": "0", "settlement_at": T3, "final": True}, T2))
        ref = market(); ref["observed_at"] = T2
        ctx = build_context(spec(), state, T2, ref)
        plan = execution.compile_orders(calculation(ctx), ctx, {})
        self.assertEqual(plan["orders"], [])
        bad = copy.deepcopy(ctx); bad["model_request"]["account"]["cash"] = 1000
        with self.assertRaisesRegex(ValueError, "context changed"):
            execution.compile_orders(calculation(ctx), bad, {})

    def test_assisted_input_is_bound_but_cannot_publish_uncalibrated_amounts(self):
        state,_=opening(); selected=spec(); selected["kind"]="assisted_workflow"; selected["intervention"]["mode"]="bounded_override"; selected["model"].update(provider_model="declared-unit-analysis@1", prompt_hash="a"*64)
        context=build_context(selected,state,T1,market())
        result=calculation(context)
        self.assertEqual(execution.compile_orders(result,context,{})["orders"],[])
        changed=copy.deepcopy(context); changed["model_request"]["account"]["cash"]+=1
        with self.assertRaisesRegex(ValueError,"context changed"):
            execution.compile_orders(result,changed,{})

    def test_historical_simulation_is_explicit_and_does_not_mutate_view(self):
        state, first = opening()
        reserve = event(state, "order_reserved", {"order": {**order(value="200"), "fee_contract": terms("000001",step="0.00000001")}})
        view = ledger.rebuild(ledger.initial_state("CNY"), [first, reserve], T4, T2)
        before = copy.deepcopy(view)
        with self.assertRaisesRegex(ValueError, "Economic view"):
            execution.simulate_events(view, [{**order(value="200"), "fee_contract": terms("000001",step="0.00000001")}], {"000001": "10"}, T2, T4, price_dates={"000001": T2[:10]})
        events = execution.simulate_events(view, [{**order(value="200"), "fee_contract": terms("000001",step="0.00000001")}], {"000001": "10"}, T2, T4, price_dates={"000001": T2[:10]}, allow_economic_view=True)
        self.assertEqual(view, before)
        rebuilt = ledger.rebuild(ledger.initial_state("CNY"), [first, reserve]+events, T4)
        self.assertEqual(ledger.snapshot(rebuilt, T4)["equity"], "1000")


class CashPlanningContractTests(unittest.TestCase):
    def test_internal_model_horizon_requires_no_exit_ages_or_holding_limit(self):
        selected = spec()
        selected["planning"]["primary_horizon_days"] = 730
        self.assertEqual(strategy.validate_spec(selected)["planning"]["primary_horizon_days"], 730)
        selected["planning"]["exit_sensitivity_days"] = [7, 30, 60, 90]
        with self.assertRaisesRegex(ValueError, "unknown"):
            strategy.validate_spec(selected)

    def test_context_seals_optional_deadline_and_detects_changed_amount(self):
        import portfolio_mpc
        selected = spec()
        selected["planning"].update(cash_deadline_days=30, cash_required_amount="250")
        state, _ = opening()
        context = build_context(selected, state, T1, market())
        self.assertEqual(context["cash_requirement"]["required_amount"], "250")
        self.assertEqual(portfolio_mpc.cash_requirement(context), context["cash_requirement"])
        changed = copy.deepcopy(context)
        changed["spec"]["planning"]["cash_required_amount"] = "251"
        with self.assertRaisesRegex(ValueError, "Cash requirement differs"):
            portfolio_mpc.cash_requirement(changed)


if __name__ == "__main__":
    unittest.main()
