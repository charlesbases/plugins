"""Independent numerical examples for direct current-order economic targets."""
import copy
import datetime as dt
import math
import sys
import unittest
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import allocation_market as market
import allocation_statistics as stats
import single_step_wealth as wealth
from contracts import fingerprint


def make_terms(code="C", exit_rate="0"):
    days = [(dt.date(2030, 1, 1)+dt.timedelta(days=index)).isoformat() for index in range(120)]
    calendar = {"id":"fixture", "timezone":"Asia/Shanghai", "coverage_start":days[0], "coverage_end":days[-1],
                "open_dates":days, "evidence_ref":{"fixture":"analytical_calendar"}}
    return {"subject":{"code":code,"currency":"CNY","channel":"TT","share_class":"C","investor_type":"retail"},
        "subscription":{"kind":"percentage","rate":"0"}, "redemption":{"kind":"percentage","rate":exit_rate},
        "holding":{"minimum_days":0,"day_basis":"calendar_days","start_inclusive":True,"end_inclusive":False,"end_event":"execution_date"},
        "trade_precision":{"money_step":"0.01","share_step":"0.01","fee_rounding":"half_up","share_rounding":"down_refund"},
        "settlement":{"lag_days":2,"day_basis":"calendar_days"},
        "execution_calendar":calendar,"order_cutoff_local":"15:00:00","confirmation":{"lag_days":1,"day_basis":"trading_days"},
        "acquisition_rule":{"holding_start":"execution_date","ownership_start":"execution_date"},
        "minimum_redemption_shares":"0.01","minimum_remaining_shares":"0","min_buy":"1","max_buy":None,
        "buyable":True,"sellable":True}


def make_context(cash="100", shares="0", goal="hold", exit_rate="0"):
    positions = [] if shares=="0" else [{"lot_id":"old","code":"C","shares":shares,"reserved_shares":"0",
        "acquired_at":"2029-01-01T00:00:00+08:00","ownership_at":"2029-01-01T00:00:00+08:00"}]
    capital = str(Decimal(cash)+Decimal(shares))
    return {"schema_version":4,"decision_at":"2030-01-01T20:00:00+08:00","as_of":"2030-01-01",
        "universe":["C"],"allocation_codes":["C"],"purchase_eligible_codes":["C"],"fee_contracts":{"C":make_terms(exit_rate=exit_rate)},
        "identities":{"C":{"fund_group_id":"fund-C","share_class":"C"}},"known_future_actions":[],
        "market_ref":{"prices":{"C":"1"},"price_dates":{"C":"2030-01-01"}},
        "snapshot":{"cash":cash,"available_cash":cash,"reserved_cash":"0","unsettled_cash":"0","positions":positions,"prices":{"C":"1"},"equity":capital},
        "risk_state":{"remaining_loss_budget":"1000"},
        "model_request":{"assets":[{"code":"C","sellable":True,"buyable":True,"buy_allowed":True,"min_buy":1,"max_weight":1}]},
        "spec":{"allocation":{"tail_probability":.5},"constraints":{"fund_group_limits":{},"sector_limits":{}},
                "planning":{"primary_horizon_days":30,"primary_goal":goal}}}


def scenario(context, pricing=1, terminal_nav=1, hold=1, buy=1, sell=1):
    return {"codes":["C"],"dates":["2029-06-01"],"probabilities":[1.],"returns":[[hold-1]],
        "targets":{name:[[value]] for name,value in {"pricing":pricing,"terminal_nav":terminal_nav,"hold":hold,"buy":buy,"sell":sell}.items()},
        "clock_context":wealth.build_clock_context(context)}


class DirectWealthTests(unittest.TestCase):
    def test_current_fee_quote_and_future_exit_have_different_principal_risk(self):
        # Independent arithmetic: 100*.99*.99=98.01 vs immediate 100*.99=99.
        # This validates fee primitives, not a profitable trading recommendation.
        context = make_context(cash="0", shares="100", goal="redeem", exit_rate="0.01")
        distribution = scenario(context, terminal_nav=.99, hold=.99, buy=.99)
        held = wealth.project(context, {"buys": [], "sells": []}, distribution)
        sold = wealth.project(context, {"buys": [], "sells": [{"lot_id": "old", "shares": "100"}]}, distribution)
        hold_value = wealth.value(context, held, distribution)
        sale_value = wealth.value(context, sold, distribution)
        self.assertAlmostEqual(hold_value["absolute_cvar_upper"], 1.99)
        self.assertAlmostEqual(sale_value["absolute_cvar_upper"], 1.)
        self.assertGreater(hold_value["absolute_cvar_upper"], 1.5)
        self.assertLess(sale_value["absolute_cvar_upper"], 1.5)
        self.assertEqual(sold["buys"], [])
        self.assertEqual(len(sold["sells"]), 1)

    def test_adverse_extra_rows_and_wrong_clocks_cannot_be_ignored(self):
        context = make_context()
        distribution = scenario(context)
        projection = wealth.project(context, {"buys":[],"sells":[]}, distribution)
        distribution["targets"]["hold"].append([.01])
        with self.assertRaisesRegex(ValueError, "aligned probability mass"):
            wealth.value(context, projection, distribution)
        distribution = scenario(context)
        distribution["clock_context"]["assets"]["C"]["pricing_date"] = "2030-01-04"
        with self.assertRaisesRegex(ValueError, "source-derived current clocks"):
            wealth.value(context, projection, distribution)

    def test_new_subscription_does_not_collect_pre_pricing_gain(self):
        context = make_context()
        distribution = scenario(context, pricing=2, terminal_nav=2.2, hold=2.2, buy=1.1, sell=2)
        projection = wealth.project(context, {"buys":[{"code":"C","cash_debit":100}],"sells":[]}, distribution)
        result = wealth.value(context, projection, distribution)
        self.assertEqual(result["wealth_lower"], [110.])
        self.assertEqual(Decimal(result["scenario_rows"][0]["trades"][0]["shares"]), Decimal(50))
        self.assertEqual(result["cash_available_lower"], 0.)

    def test_sale_proceeds_are_economic_wealth_and_not_current_cash(self):
        context = make_context(cash="0", shares="100")
        distribution = scenario(context, pricing=.8, terminal_nav=.7, hold=.7, buy=.875, sell=.8)
        projection = wealth.project(context, {"buys":[],"sells":[{"lot_id":"old","shares":"100"}]}, distribution)
        result = wealth.value(context, projection, distribution)
        self.assertEqual(result["wealth_lower"], [80.])
        self.assertEqual(result["cash_available_lower"], 0.)
        self.assertFalse(result["scenario_rows"][0]["trades"][0]["spendable_now"])

    def test_terminal_redemption_fee_does_not_tax_cash_dividend(self):
        context = make_context(goal="redeem", exit_rate="0.1")
        distribution = scenario(context, terminal_nav=.9, hold=1.1, buy=1.1)
        projection = wealth.project(context, {"buys":[{"code":"C","cash_debit":100}],"sells":[]}, distribution)
        result = wealth.value(context, projection, distribution)
        self.assertEqual(result["wealth_lower"], [101.])  # NAV 90 - fee 9 + dividend 20.
        self.assertEqual(result["wealth_upper"], [101.])

    def test_inconsistent_targets_retain_probability_with_support_bound(self):
        context = make_context()
        distribution = scenario(context, pricing=2, terminal_nav=2.2, hold=2.2, buy=1.5, sell=2)
        projection = wealth.project(context, {"buys":[{"code":"C","cash_debit":100}],"sells":[]}, distribution)
        result = wealth.value(context, projection, distribution)
        self.assertEqual(result["probabilities"], [1.])
        self.assertEqual(result["wealth_lower"], [0.])
        self.assertEqual(result["wealth_upper"], [None])
        self.assertIn("unresolved_targets", result["qualification"])

    def test_known_future_dividend_cannot_be_silently_set_to_zero(self):
        context = make_context()
        context["known_future_actions"] = [{"code":"C","ex_date":"2030-01-04","record_date":"2030-01-03","pay_date":"2030-01-06",
            "per_share":".1","rights_rule":{"subscribe_on_record_date":"excluded","redeem_on_record_date":"included"}}]
        distribution = scenario(context)
        projection = wealth.project(context, {"buys":[{"code":"C","cash_debit":100}],"sells":[]}, distribution)
        result = wealth.value(context, projection, distribution)
        self.assertFalse(result["scenario_rows"][0]["targets_modeled"])
        self.assertIn("known cash distribution", result["scenario_rows"][0]["reasons"][0])

    def test_cost_table_uses_only_source_holding_age_without_future_calendar(self):
        context = make_context(exit_rate="0.01")
        context["fee_contracts"]["C"].pop("execution_calendar")
        result = wealth.product_cost_comparison(context)
        ages = [item["holding_days"] for item in result["products"][0]["horizons"]]
        self.assertEqual(ages, [0])
        self.assertTrue(all(item["exit_cost"]=="1.00" for item in result["products"][0]["horizons"]))

    def test_product_source_ages_ignore_model_H_and_preserve_account_facts(self):
        context = make_context(exit_rate="0.01")
        context["fee_contracts"]["C"]["holding"]["minimum_days"] = 400
        other = make_terms(code="D")
        other["holding"]["minimum_days"] = 31
        other["redemption"] = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7.5", "fee": {"kind": "percentage", "rate": "0.005"}},
            {"minimum": "365", "fee": {"kind": "percentage", "rate": "0"}}]}
        context["fee_contracts"]["D"] = other
        context["universe"].append("D")
        context["purchase_eligible_codes"].append("D")
        context["identities"]["D"] = {"fund_group_id": "fund-D", "share_class": "C"}
        context["snapshot"]["prices"]["D"] = "1"
        context["market_ref"]["price_dates"]["D"] = "2030-01-01"
        for terms in context["fee_contracts"].values():
            terms.pop("execution_calendar")
        context["spec"]["planning"]["primary_horizon_days"] = 3
        snapshot = copy.deepcopy(context["snapshot"])
        short = wealth.product_cost_comparison(context)
        context["spec"]["planning"]["primary_horizon_days"] = 730
        self.assertEqual(short, wealth.product_cost_comparison(context))
        self.assertEqual(context["snapshot"], snapshot)
        products = {row["code"]: row for row in short["products"]}
        self.assertEqual([row["holding_days"] for row in products["C"]["horizons"]], [400])
        rows = {row["holding_days"]: row for row in products["D"]["horizons"]}
        self.assertEqual(sorted(rows), [0, 1, 7, 8, 30, 31, 32, 364, 365, 366])
        self.assertEqual(rows[30]["reason"], "holding_age_precedes_source_lock")
        self.assertEqual(rows[31]["exit_cost"], "0.50")
        self.assertEqual(rows[365]["exit_cost"], "0.00")
        self.assertFalse(short["actual_order"])


class DirectSourceLabels(unittest.TestCase):
    def test_zero_cash_decoder_preserves_exact_economic_identities(self):
        latent = dict.fromkeys(market.LATENT_NAMES, 0.)
        latent["log_pricing"], latent["log_terminal_nav"] = math.log(2), math.log(2.2)
        target = market.decode_latents(latent)
        self.assertEqual(target["hold"], target["terminal_nav"])
        self.assertAlmostEqual(target["pricing"]*target["buy"], target["terminal_nav"])
        self.assertEqual(target["sell"], target["pricing"])

    def test_rights_membership_decoder_preserves_both_entitlements(self):
        source = {"base_date":"2029-01-01","base_nav":1.,"pricing_date":"2029-01-02","pricing_nav":1.,
            "ownership_date":"2029-01-02","end_date":"2029-01-04","terminal_nav":.9,
            "dividends":[{"record_date":"2029-01-02","ex_date":"2029-01-03","pay_date":"2029-01-04",
                "per_share":.1,"distribution_mode":"cash","currency":"CNY","evidence_ref":{"fixture":"explicit_rule"},
                "entitlement_rule":{"subscribe_on_record_date":"included","redeem_on_record_date":"included"}}]}
        latent = market.recompute_source_latents(source)
        self.assertAlmostEqual(latent["sqrt_cash_both"]**2, .1)
        target = market.decode_latents(latent)
        for name,value in market.recompute_source_targets(source).items():
            self.assertAlmostEqual(target[name], value)

    def inputs(self):
        days = [(dt.date(2029,1,1)+dt.timedelta(days=i)).isoformat() for i in range(100)]
        nav = {"C":[{"date":day,"nav":1+.001*i,"distribution_per_share":0,"corporate_actions":[]} for i,day in enumerate(days)]}
        features = {"C":[{"code":"C","feature_cutoff_nav_date":day,"known_max_nav_date":day,
                          "values":dict(zip(market.NAV_FEATURE_NAMES,[.01,.02,.03,.04,.05]))} for day in days]}
        info = {"C":{"fund_group_id":"fund-C"}}
        clock = wealth.build_clock_context(make_context())
        return nav,features,info,clock,days

    def test_labels_use_pricing_denominator_and_can_be_rederived(self):
        nav,features,info,clock,days = self.inputs()
        labels = market.build_single_step_samples(nav,features,info,7,1,days[-1],clock)
        label = next(row for row in labels if row["targets"] is not None)
        source = label["label_source"]
        self.assertGreater(source["pricing_date"], label["decision_date"])
        self.assertEqual(source["targets"], market.recompute_source_targets(source))
        self.assertAlmostEqual(source["targets"]["buy"], source["terminal_nav"]/source["pricing_nav"])
        self.assertNotEqual(source["targets"]["buy"], source["targets"]["hold"])

    def test_nonzero_dividend_without_record_rights_blocks_that_label(self):
        nav,features,info,clock,days = self.inputs()
        nav["C"][5]["distribution_per_share"] = .1
        labels = market.build_single_step_samples(nav,features,info,7,1,days[-1],clock)
        affected = next(row for row in labels if row["decision_date"]==days[1])
        self.assertIsNone(affected["targets"])
        self.assertIn("entitlement", affected["label_reason"])

    def test_oos_errors_are_frozen_before_the_current_origin(self):
        nav,features,info,clock,days = self.inputs()
        samples = market.build_single_step_samples(nav,features,info,7,1,days[-1],clock)
        origin = days[-1]
        latest = next(row for row in samples if row["decision_date"]==origin)
        prediction = [{"code":"C","decision_date":origin,"horizon_days":7,"x":latest["x"],"feature_cutoff_date":latest["feature_cutoff_date"]}]
        policy = {"train_window_days":1000,"min_train_dates":5,"cv_folds":2,"min_joint_dates":5,
            "cv_initial_train_fraction":.5,"feature_names":market.FEATURE_NAMES,"alpha_grid":[.001],"l1_ratio_grid":[.5]}
        result = stats.fit_joint_targets(samples,prediction,policy,clock)
        self.assertEqual(result["status"], "research_ready")
        for record in result["joint_scenarios"]["source_oos"]:
            self.assertLess(record["label_available"], origin)
            self.assertLess(record["training_audit"]["max_label_available_date"], record["date"])
            self.assertEqual(record["realized_targets"]["buy"][0], record["source_labels"]["C"]["targets"]["buy"])
        changed = copy.deepcopy(samples)
        changed[-1]["x"]=[999]*len(market.FEATURE_NAMES)
        self.assertEqual(result, stats.fit_joint_targets(changed,prediction,policy,clock))


if __name__=="__main__":
    unittest.main()
