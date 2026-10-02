"""Synthetic mathematical source-timing cases; never real market evidence."""
import copy
import datetime as dt
import math
import unittest

import allocation_market as market
import news_wealth
import portfolio_paths
from contracts import fingerprint
from test_allocation import source_context


def archive(value, at, identity):
    digest = fingerprint(value)
    return {"available_at":at,"observed_at":at,"raw_ref":{"source_id":identity,"sha256":digest},
        "availability_evidence":{"kind":"archived_original_capture","source_id":identity,"raw_sha256":digest,"captured_at":at}}


def fixture(feedback=False, closed_origin=False):
    context = source_context(cash="999")
    context["decision_at"] = "2030-02-01T20:00:00+08:00"
    context["fee_contracts"]["C"]["trade_precision"]["money_step"] = "0.01"
    if not feedback:
        context["fee_contracts"]["C"]["subscription"] = {"kind":"percentage","rate":".25"}
    else:
        context["fee_contracts"]["C"]["confirmation"]["lag_days"] = 3
    if closed_origin:
        context["fee_contracts"]["C"]["execution_calendar"]["open_dates"].remove("2030-01-01")
    start,end = dt.date(2029,8,1),dt.date(2030,1,9)
    prices = {}
    day = start
    while day <= end:
        prices[str(day)] = 1.
        day += dt.timedelta(days=1)
    pattern = [2.,2.,2.,.5,.5,.5,.5,1.]
    if feedback:
        for origin in (dt.date(2029,12,10),dt.date(2029,12,20),dt.date(2030,1,1)):
            for offset,value in enumerate(pattern,1): prices[str(origin+dt.timedelta(days=offset))] = value
    else:
        prices["2030-01-09"] = 2.
    if closed_origin:
        del prices["2030-01-01"]
    raw = []
    for day,nav in prices.items():
        value = {"code":"C","date":day,"nav":nav,"distribution_per_share":0.,"corporate_actions":[]}
        version = {**value,**archive(value,day+"T21:00:00+08:00","synthetic-NAV-"+day)}
        version["version_id"] = fingerprint(version)
        raw.append({**value,"source_versions":[version]})
    def source_row(origin):
        cutoff = str(dt.date.fromisoformat(origin)-dt.timedelta(days=1))
        at = origin+"T20:00:00+08:00"
        vector,feature = market._feature_at({"feature_cutoff_nav_date":cutoff},raw,at,1)
        vector += [1]
        final = str(dt.date.fromisoformat(origin)+dt.timedelta(days=8))
        priced = str(dt.date.fromisoformat(origin)+dt.timedelta(days=1))
        mature = final+"T21:00:00+08:00"
        quote = lambda date, when: news_wealth._quote(raw,date,when)
        source = {"code":"C","base_date":cutoff,"base_nav":1.,"pricing_date":priced,"pricing_nav":quote(priced,mature)["nav"],
            "ownership_date":priced,"end_date":final,"terminal_nav":quote(final,mature)["nav"],"dividends":[],
            "label_available_at":mature,"label_available":final,"feature_source":feature,
            "source_quotes":[quote(row["date"],mature) for row in raw if cutoff < row["date"] <= final]}
        source["targets"] = market.recompute_source_targets(source)
        source["latent_targets"] = market.recompute_source_latents(source)
        source["source_hash"] = fingerprint(source)
        industry = {"code":"C","decision_date":origin,"origin_at":at,"values":[],"role":"synthetic_covariate_timing_witness"}
        industry["source_hash"] = fingerprint(industry)
        sample = {"code":"C","decision_date":origin,"feature_cutoff_date":cutoff,"feature_source":feature,"x":vector,
            "industry_source":industry,"label_source":source,"fund_group_id":"fund-C","horizon_days":8}
        return sample
    sample = source_row("2030-01-01")
    record = {"date":"2030-01-01","origin_at":"2030-01-01T20:00:00+08:00","codes":["C"],"label_available":"2030-01-09",
        "source_labels":{"C":sample["label_source"]},"source_hashes":{"C":sample["label_source"]["source_hash"]},"model":{"horizon_days":8}}
    protocol = {"max_current_actions":64,"max_policy_count":64,"future_cash_fractions":[1.],"primary_goal":"hold",
                "loss_tolerance":.5,"tail_probability":.5}
    frontier = archive(sample["industry_source"],"2030-01-01T19:00:00+08:00","synthetic-covariate-frontier")
    frontier["value_hash"] = sample["industry_source"]["source_hash"]
    frame = {"account":{"kind":"declared_all_cash_research","currency":"CNY","cash_cny":"8","external_cashflows":[]},
        "fee_scope":"current_terms_repriced","fee_contracts":copy.deepcopy(context["fee_contracts"]),
        "study_mode":"retrospective_source_oos","policy_scope":"full_source_mpc" if feedback else "current_action_then_hold",
        "protocol":protocol,"protocol_hash":fingerprint(protocol),"stage_dates":["2030-01-01","2030-01-05","2030-01-09"],
        "source_nav":{"C":raw},"source_frontier":[frontier]}
    if feedback:
        research = news_wealth._base_context(context,record,frame,[sample])
        contract = portfolio_paths.stage_contract(research)
        training = []
        for origin in ("2029-12-10","2029-12-20"):
            row = source_row(origin)
            source = row["label_source"]
            dates = [str(dt.date.fromisoformat(origin)+dt.timedelta(days=i)) for i in contract["offsets"]]
            observed = [news_wealth._quote(raw,date,source["label_available_at"]) for date in dates]
            window = [news_wealth._quote(raw,row["date"],source["label_available_at"])
                      for row in raw if source["base_date"] < row["date"] <= dates[-1]]
            witness = {"code":"C","origin":origin,"base_nav":1.,"base_date":source["base_date"],"observation_dates":dates,
                "nav_quotes":observed,"window_quotes":window,"window_end_date":dates[-1],
                "distributions":[],"future_distributions":[],"prior_entitlements":[],"original_label_source":source}
            training.append({"code":"C","decision_date":origin,"fund_group_id":"fund-C","x":row["x"],
                "wealth_ratios":[float(quote["nav"]) for quote in observed],"base_nav":1.,"cash":[],"label_source":witness,"source_hash":fingerprint(witness),
                "label_available_at":source["label_available_at"],"input_source":archive(row["x"],origin+"T19:00:00+08:00","synthetic-input-"+origin)})
        model = portfolio_paths._fit_model(training,[],["fund-C"],.001,.5,feature_names=market.FEATURE_NAMES)
        forecast = {"method":"source_multi_date_elastic_net","input_columns":list(range(6)),"training_rows":training,
            "cash_keys":[],"fund_groups":["fund-C"],"alpha":.001,"l1_ratio":.5,"model_hash":fingerprint(model),
            "contract":contract}
        frame["forecast_full"] = copy.deepcopy(forecast)
        frame["forecast_native"] = copy.deepcopy(forecast)
    context["paired_currency_validation"] = {"schema_version":1,"frames":{"2030-01-01":frame}}
    def prediction(terminal):
        return {"C":{name:(math.log(terminal) if name=="log_terminal_nav" else 0.) for name in market.LATENT_NAMES}}
    full,native = prediction(.5 if feedback else 1.5),prediction(.5 if feedback else 1.01)
    return context,record,[sample],full,native


class NewsWealthTests(unittest.TestCase):
    def evaluate(self, args):
        context,record,rows,full,native = args
        return news_wealth.evaluate_pair(context=context,record=record,source_rows=rows,full_prediction=full,native_prediction=native)

    def test_fee_net_currency_difference_has_independent_cash_truth(self):
        args = fixture()
        original = copy.deepcopy(args[0])
        result = self.evaluate(args)
        self.assertEqual(result["status"],"conditional_currency_replay_ready",result.get("reason"))
        # Request 8 buys six whole shares at 1 plus fee 1.50 and refund .50.
        # Source terminal NAV 2 gives 12.50; native arm retains research cash 8.
        self.assertEqual((result["full"]["after_fee_terminal_wealth_cny"],result["native"]["after_fee_terminal_wealth_cny"]),(12.5,8.))
        self.assertEqual((result["full"]["turnover_cny"],result["native"]["turnover_cny"]),(8.,0.))
        self.assertEqual(result["full"]["frame_hash"],result["native"]["frame_hash"])
        self.assertEqual(result["initial_wealth_cny"],8.)
        self.assertEqual(result["full"]["cvar_loss_cny"],-1.5)
        self.assertEqual(result["native"]["cvar_loss_cny"],0.)
        self.assertTrue(result["source_evidence"]["no_residual_risk_evaluation"])
        self.assertFalse(result["actual_orders_created"])
        self.assertFalse(result["source_evidence"]["frame_source_PIT_verified"])
        self.assertFalse(result["source_evidence"]["strict_PIT_verified"])
        self.assertEqual(result["source_evidence"]["provenance_status"],"metadata_consistent_original_archive_authenticity_unverified")
        self.assertEqual(args[0],original)

    def test_complete_feedback_en_refit_changes_registered_policy_scope_result(self):
        args = fixture(feedback=True)
        full = self.evaluate(args)
        self.assertEqual(full["status"],"conditional_currency_replay_ready",full.get("reason"))
        # Initial 2 buys one share at source pricing NAV 2; remaining 6 buys
        # twelve shares at .5 only after the source Jan5 confirmation boundary.
        self.assertEqual(full["full"]["after_fee_terminal_wealth_cny"],13.)
        self.assertEqual(full["full"]["selected_policy"]["current_action"],{"buys":[{"code":"C","cash_debit":"2"}],"sells":[]})
        self.assertEqual(full["full"]["selected_policy"]["future_rule"]["weights"],{"C":"1.0"})
        args[0]["paired_currency_validation"]["frames"]["2030-01-01"]["policy_scope"] = "current_action_then_hold"
        subset = self.evaluate(args)
        self.assertEqual(subset["status"],"conditional_currency_replay_ready",subset.get("reason"))
        self.assertEqual(subset["full"]["after_fee_terminal_wealth_cny"],8.)
        self.assertNotEqual(full["policy_family_hash"],subset["policy_family_hash"])

    def test_v3_closed_origin_has_no_fabricated_origin_close(self):
        args = fixture(feedback=True,closed_origin=True)
        frame = args[0]["paired_currency_validation"]["frames"]["2030-01-01"]
        contract = frame["forecast_full"]["contract"]
        self.assertEqual(contract["origin_date"],"2030-01-01")
        self.assertEqual(contract["nav_dates"][0],"2030-01-02")
        self.assertEqual(contract["known_marks"]["C"]["nav_date"],"2029-12-31")
        result = self.evaluate(args)
        self.assertEqual(result["status"],"conditional_currency_replay_ready",result.get("reason"))
        self.assertEqual(result["full"]["after_fee_terminal_wealth_cny"],13.)
        self.assertFalse(result["source_evidence"]["strict_PIT_verified"])

    def test_endpoint_projection_uses_forecast_pricing_role_not_old_mark(self):
        args = fixture()
        args[3]["C"]["log_pricing"] = math.log(2.)
        result = self.evaluate(args)
        self.assertEqual(result["status"],"conditional_currency_replay_ready",result.get("reason"))
        self.assertEqual(result["full"]["selected_policy"]["current_action"],{"buys":[],"sells":[]})
        self.assertEqual(result["full"]["after_fee_terminal_wealth_cny"],8.)

    def test_missing_future_forecast_is_insufficient_without_interpolation(self):
        args = fixture(feedback=True)
        del args[0]["paired_currency_validation"]["frames"]["2030-01-01"]["forecast_native"]
        result = self.evaluate(args)
        self.assertEqual(result["status"],"insufficient_evidence")
        self.assertIn("multi-date source forecast is missing",result["reason"])

    def test_future_archive_and_external_verified_flag_cannot_supply_pit(self):
        args = fixture()
        ref = args[0]["paired_currency_validation"]["frames"]["2030-01-01"]["source_frontier"][0]
        ref.update(available_at="2030-02-01T00:00:00+08:00",observed_at="2030-02-01T00:00:00+08:00",strict_PIT_verified=True)
        ref["availability_evidence"]["captured_at"] = ref["available_at"]
        result = self.evaluate(args)
        self.assertEqual(result["status"],"insufficient_evidence")
        self.assertIn("information cutoff",result["reason"])

    def test_rehashed_outer_label_still_must_match_original_nav(self):
        args = fixture()
        source = args[2][0]["label_source"]
        source["terminal_nav"] = 3.
        source["source_hash"] = fingerprint({k:v for k,v in source.items() if k!="source_hash"})
        args[1]["source_hashes"]["C"] = source["source_hash"]
        result = self.evaluate(args)
        self.assertEqual(result["status"],"insufficient_evidence")
        self.assertIn("source terminal_nav",result["reason"])

    def test_declared_policy_budget_never_returns_currency_incumbent(self):
        args = fixture()
        frame = args[0]["paired_currency_validation"]["frames"]["2030-01-01"]
        frame["protocol"]["max_policy_count"] = 1
        frame["protocol_hash"] = fingerprint(frame["protocol"])
        result = self.evaluate(args)
        self.assertEqual(result["status"],"insufficient_evidence")
        self.assertIn("source policy family computation is incomplete",result["reason"])
        self.assertNotIn("full",result)
        self.assertNotIn("native",result)

    def test_rehashed_incomplete_training_cash_window_is_rejected(self):
        args = fixture(feedback=True)
        frame = args[0]["paired_currency_validation"]["frames"]["2030-01-01"]
        for arm in ("full","native"):
            row = frame["forecast_"+arm]["training_rows"][0]
            row["label_source"]["window_quotes"].pop(0)
            row["source_hash"] = fingerprint(row["label_source"])
        result = self.evaluate(args)
        self.assertEqual(result["status"],"insufficient_evidence")
        self.assertIn("full source cash/NAV window is incomplete",result["reason"])

    def test_research_origin_mark_does_not_inherit_today_context_mark(self):
        args = fixture(feedback=True)
        context,record,rows = args[:3]
        context["known_marks"] = {"C":{"nav_date":"2030-02-01","value":"999",
            "known_at":"2030-02-01T19:00:00+08:00","source_ref":{"synthetic":"today"}}}
        frame = context["paired_currency_validation"]["frames"]["2030-01-01"]
        research = news_wealth._base_context(context,record,frame,rows)
        self.assertEqual(research["known_marks"]["C"]["nav_date"],"2029-12-31")
        self.assertEqual(research["known_marks"]["C"]["value"],"1.0")
        result = self.evaluate(args)
        self.assertEqual(result["status"],"conditional_currency_replay_ready",result.get("reason"))
        self.assertFalse(result["source_evidence"]["strict_PIT_verified"])


    def test_current_registration_time_does_not_invent_historical_trial(self):
        args = fixture()
        frame = args[0]["paired_currency_validation"]["frames"]["2030-01-01"]
        frame["registration_source"] = archive(frame["protocol"],"2030-02-01T20:00:00+08:00","synthetic-study-created-today")
        retrospective = self.evaluate(args)
        self.assertEqual(retrospective["status"],"conditional_currency_replay_ready",retrospective.get("reason"))
        self.assertIn("retrospective_posthoc_strategy_simulation_not_prospective_trial",retrospective["limitations"])
        frame["study_mode"] = "prospective_registered_observation"
        prospective = self.evaluate(args)
        self.assertEqual(prospective["status"],"insufficient_evidence")
        self.assertIn("information cutoff",prospective["reason"])

    def test_research_account_does_not_inherit_actual_user_cash_deadline(self):
        args = fixture()
        args[0]["cash_requirement"] = {"date":"2030-02-09","required_amount":"999","scope":"actual_user_cash_need"}
        result = self.evaluate(args)
        self.assertEqual(result["status"],"conditional_currency_replay_ready",result.get("reason"))
        self.assertEqual(result["initial_wealth_cny"],8.)
        self.assertEqual(args[0]["cash_requirement"]["required_amount"],"999")


if __name__ == "__main__":
    unittest.main()
