"""Real EN/cash algebra on explicit synthetic archives; not historical ROI proof."""
import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
import portfolio_paths as paths
import candidate_readiness as gate
import industry_model
import allocation_market as market
from test_economic_industry_readiness import inputs


def prepared(data_override=None):
    _, _, data, spec, at, supplied, terms = inputs()
    if data_override:
        data_override(data)
    import datetime as dt
    from contracts import fingerprint
    codes = ["A", "B"]
    marks = {code:str(data["nav"][code][64]["nav"]) for code in codes}
    for code in codes:
        first = dt.date.fromisoformat(data["nav"][code][0]["date"])
        warmup = [{"code":code,"date":str(first-dt.timedelta(days=120-i)),"nav":data["nav"][code][0]["nav"],"distribution_per_share":0.} for i in range(120)]
        data["nav"][code] = warmup+data["nav"][code]
        for row in data["nav"][code]:
            for event in row.get("corporate_actions", []):
                event.setdefault("known_at", row["date"]+"T21:00:00+08:00")
            value={key:item for key,item in row.items() if key!="source_versions"}
            stamp=row["date"]+"T21:00:00+08:00";digest=fingerprint(value)
            version={**value,"available_at":stamp,"observed_at":stamp,"raw_ref":{"source_id":"synthetic-original-NAV-"+code,"sha256":digest},
                "availability_evidence":{"kind":"archived_original_capture","source_id":"synthetic-original-NAV-"+code,"raw_sha256":digest,"captured_at":stamp}}
            version["version_id"]=fingerprint(version);row["source_versions"]=[version]
    clock = gate._clock(codes, supplied, spec, at, terms)
    raw = gate._samples(codes, data, spec, at[:10], clock)
    industry = industry_model.build_forward(data["industry"], sorted({row["decision_date"] for row in raw}),
        spec["industry"]["training"], 3, clock["order_time_local"])
    samples = market.attach_industry_features(raw, industry, data["industry_exposures"], clock["order_time_local"])
    fit = gate._family_fit(codes, data, spec, at, clock, industry)
    known = {}
    from contracts import ContractError
    for code in codes:
        eligible = []
        for row in data["nav"][code]:
            try:eligible.append(market.known_mark(row,at))
            except ContractError:continue
        known[code] = max(eligible,key=lambda mark:mark["nav_date"])
    marks = {code:mark["value"] for code,mark in known.items()}
    context = {"as_of": at[:10], "decision_at": at, "allocation_codes": codes, "fee_contracts": terms,
        "known_marks":known,"price_schema":"source_role_price_paths_v3",
        "spec": spec, "market_ref": {"prices": marks,"price_dates":{code:mark["nav_date"] for code,mark in known.items()},"known_marks":known},
        "model_request": {"training_policy": spec["training"]}, "known_future_actions": []}
    # Paired feature selection now values the same canonical policy family.
    # Declare a separate engineering all-cash account, not user history.
    import ledger
    state = ledger.apply_event(ledger.initial_state("CNY"),{"id":"source-window-research-capital","type":"opening","sequence":1,
        "effective_at":at,"known_at":at,"recorded_at":at,"data":{"cash":"100","lots":[],"prices":{},"price_dates":{}}})
    context["snapshot"] = ledger.snapshot(state,at,marks,context["market_ref"]["price_dates"])
    context["account_scope"] = "declared_synthetic_all_cash_research_account"
    context["purchase_eligible_codes"] = codes
    context["identities"] = {code:{"fund_group_id":"synthetic-"+code,"share_class":"C"} for code in codes}
    context["risk_state"] = {"net_principal":"100","loss_tolerance":".25","principal_floor":"75","remaining_loss_budget":"25"}
    context["model_request"].update(assets=[{"code":code,"buyable":True,"sellable":True,"buy_allowed":True,"min_buy":1,"max_weight":1} for code in codes],
        allocation_policy={"funding_levels":[0]})
    spec["decision"].update(max_current_actions=256,max_policy_count=2048,max_path_stages=8,future_cash_fractions=[1.])
    spec["allocation"] = {"tail_probability":.5}
    spec["constraints"] = {"sector_limits":{},"fund_group_limits":{}}
    context["context_hash"] = fingerprint(context)
    return context, data, samples, fit


class PortfolioSourcePathTests(unittest.TestCase):
    def test_source_nonzero_cash_runs_real_en_and_original_joint_calibration(self):
        def recurring_cash(data):
            cumulative = 0.
            for index, row in enumerate(data["nav"]["A"]):
                if index in (18, 35, 55):
                    cumulative += .03
                    row["distribution_per_share"] = .03
                    row["corporate_actions"] = [{"kind": "cash_distribution", "code": "A", "per_share": .03,
                        "record_date": data["nav"]["A"][index-1]["date"], "ex_date": row["date"],
                        "pay_date": data["nav"]["A"][index+1]["date"], "currency": "CNY", "distribution_mode": "cash",
                        "entitlement_rule": {"subscribe_on_record_date": "included", "redeem_on_record_date": "included"},
                        "evidence_ref": {"synthetic_original_cash_document": index}}]
                row["nav"] -= cumulative
        context, data, samples, fit = prepared(recurring_cash)
        result = paths.build_paths(context, data, samples, fit)
        self.assertEqual(result["status"], "research_ready", result.get("reason"))
        self.assertGreater(len(result["cash_dictionary"]), 0)
        observed = [pair for pair in result["calibration_paths"] if pair["realized"]["distributions"]]
        self.assertTrue(observed)
        for pair in observed:
            leaf = pair["realized"]
            witness = next(row for row in leaf["source_witnesses"] if row["code"] == "A")
            mark = float(context["market_ref"]["prices"]["A"])
            for date, quote in zip(result["nav_dates"], witness["nav_quotes"]):
                cash = sum(event["per_share"] for event in leaf["distributions"]
                           if event["code"] == "A" and event["ex_date"] <= date)
                actual_cash = sum(event["per_share"] for event in witness["distributions"] if event["ex_date"] <= quote["date"])
                self.assertAlmostEqual(leaf["nav"][date]["A"]+cash,
                                       mark*(quote["nav"]+actual_cash)/witness["base_nav"])

    def test_full_joint_paths_and_original_independent_calibration_pairs(self):
        context, data, samples, fit = prepared()
        result = paths.build_paths(context, data, samples, fit)
        self.assertEqual(result["status"], "research_ready", result.get("reason"))
        self.assertAlmostEqual(sum(row["probability"] for row in result["selection_paths"]), 1.)
        self.assertEqual(paths.validate_paths(result, {"context": context, "data": data,
            "samples": samples, "fitting": fit})["status"], "passed")
        for groups in result["prefix_groups"].values():
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0]), len(result["selection_paths"]))
        for pair in result["calibration_paths"]:
            self.assertLess(pair["model_training_audit"]["maximum_label_available"], pair["origin_at"][:10])
            realized = pair["realized"]
            witness = next(row for row in realized["source_witnesses"] if row["code"] == "A")
            mark = float(context["market_ref"]["prices"]["A"])
            for date, source_quote in zip(result["nav_dates"], witness["nav_quotes"]):
                self.assertAlmostEqual(realized["nav"][date]["A"], mark*source_quote["nav"]/witness["base_nav"])
        self.assertLess(result["model_training_audit"]["maximum_label_available"],
                        result["model_training_audit"]["training_ceiling_at"][:10])

    def test_exact_utc_maturity_survives_producer_eligibility_and_validator(self):
        import datetime as dt
        from contracts import fingerprint
        context, data, samples, fit = prepared()
        original = copy.deepcopy(samples)
        origin = fit["joint_scenarios"]["source_calibration_oos"][-1]["date"]
        observed = str(dt.date.fromisoformat(origin)+dt.timedelta(days=3))
        stamp = observed+"T23:30:00+00:00"
        china_day = str(dt.date.fromisoformat(observed)+dt.timedelta(days=1))
        for code in context["allocation_codes"]:
            row = next(row for row in data["nav"][code] if row["date"] == observed)
            version = row["source_versions"][0]
            version.update(available_at=stamp, observed_at=stamp)
            version["availability_evidence"]["captured_at"] = stamp
            version["version_id"] = fingerprint({key:value for key,value in version.items() if key != "version_id"})
        frames, gaps = paths._frames(context, data, samples, paths.stage_contract(context))
        row = next(row for row in frames if row["code"] == "A" and row["decision_date"] == origin)
        self.assertEqual(row["label_available_at"], stamp)
        self.assertEqual(row["label_available_date"], china_day)
        policy = context["model_request"]["training_policy"]
        eligible = paths._eligible(frames, ["A"], china_day, policy)
        self.assertNotIn(origin, {row["decision_date"] for row in eligible})
        later = str(dt.date.fromisoformat(china_day)+dt.timedelta(days=2))
        eligible = paths._eligible(frames, ["A"], later, policy, ceiling=stamp)
        self.assertNotIn(origin, {row["decision_date"] for row in eligible})
        result = paths.build_paths(context, data, samples, fit)
        self.assertEqual(result["status"], "research_ready", result.get("reason"))
        record = next(row for row in result["path_source_records"] if row["date"] == origin)
        self.assertEqual(record["label_available_at"], stamp)
        self.assertEqual(paths.validate_paths(result, {"context":context,"data":data,"samples":samples,"fitting":fit})["status"], "passed")
        self.assertEqual(samples, original)

    def test_source_middle_ordinate_is_not_endpoint_interpolation(self):
        def change(data):
            data["nav"]["A"][40]["nav"] = 2.0
        context, data, samples, _ = prepared(change)
        contract = paths.stage_contract(context)
        frames, _ = paths._frames(context, data, samples, contract)
        origin = data["features"]["A"][39]["feature_cutoff_nav_date"]
        frame = next(row for row in frames if row["code"] == "A" and row["decision_date"] == origin)
        raw = {row["date"]:row for row in data["nav"]["A"]}
        base = raw[frame["label_source"]["base_date"]]["nav"]
        end = raw[frame["label_source"]["window_end_date"]]["nav"]
        middle = next(i for i,quote in enumerate(frame["label_source"]["nav_quotes"]) if quote["nav"]==2.0)
        self.assertEqual(frame["label_source"]["nav_quotes"][middle]["nav"], 2.0)
        self.assertAlmostEqual(frame["wealth_ratios"][middle], 2.0/base)
        interpolated = (2*base+end)/3
        self.assertNotAlmostEqual(frame["label_source"]["nav_quotes"][middle]["nav"], interpolated)

    def test_nonzero_cash_rights_and_wealth_conservation(self):
        contract = {"codes": ["A"], "nav_dates": ["2030-01-01", "2030-01-02", "2030-01-03", "2030-01-04"]}
        from test_strategy_execution import capture_engineering_marks
        contract.update(price_schema="source_role_price_paths_v3",origin_date="2030-01-01",decision_at="2030-01-01T20:00:00+08:00",
            known_marks=capture_engineering_marks({"prices":{"A":"100"},"price_dates":{"A":"2030-01-01"},"observed_at":"2030-01-01T20:00:00+08:00"})["known_marks"])
        rule = {"subscribe_on_record_date": "included", "redeem_on_record_date": "included"}
        event = {"code": "A", "record_date": "2030-01-02", "ex_date": "2030-01-03", "pay_date": "2030-01-04",
                 "currency": "CNY", "distribution_mode": "cash", "entitlement_rule": rule}
        key = paths._cash_key(event, "2030-01-01")
        latent = {"A": {"values": [0.]+[math.log(1.01)]*3+[math.sqrt(.1)], "cash_keys": [key]}}
        result = paths._decode(latent, {"A": 100.}, contract, "2030-01-01T20:00:00+08:00",
                               [{"synthetic_source": "cash_lifecycle"}], "cash-path", 1., None)
        self.assertEqual(result["distributions"][0]["entitlement_rule"], rule)
        self.assertAlmostEqual(result["distributions"][0]["per_share"], 10.)
        self.assertAlmostEqual(result["nav"]["2030-01-02"]["A"], 101.)
        self.assertAlmostEqual(result["nav"]["2030-01-03"]["A"], 91.)
        self.assertAlmostEqual(result["nav"]["2030-01-03"]["A"]+result["distributions"][0]["per_share"], 101.)
        known = {**event, "kind": "cash_distribution", "per_share": 20., "evidence_ref": {"synthetic_issuer_doc": "known"}}
        constrained = paths._decode(latent, {"A": 100.}, contract, "2030-01-01T20:00:00+08:00", [], "known", 1., None, [known])
        self.assertEqual(len(constrained["distributions"]), 1)
        self.assertEqual(constrained["distributions"][0]["per_share"], 20.)
        self.assertAlmostEqual(constrained["nav"]["2030-01-03"]["A"], 81.)

    def test_unseen_known_distribution_cannot_be_zeroed_to_qualify(self):
        context, data, samples, fit = prepared()
        context["known_future_actions"] = [{"code": "A", "kind": "cash_distribution", "per_share": .1,
            "record_date": "2028-03-06", "ex_date": "2028-03-07", "pay_date": "2028-03-08",
            "currency": "CNY", "distribution_mode": "cash",
            "entitlement_rule": {"subscribe_on_record_date": "included", "redeem_on_record_date": "included"},
            "evidence_ref": {"synthetic_issuer": "new_timing_pattern"}}]
        result = paths.build_paths(context, data, samples, fit)
        self.assertEqual(result["status"], "partial")
        self.assertIn("outside trained path timing support", result["reason"])
        self.assertFalse(result["trade_ready"])
        self.assertEqual(result["selection_paths"], [])

    def test_missing_full_rights_source_and_unfrozen_inputs_fail_closed(self):
        context, data, samples, fit = prepared()
        no_frozen = copy.deepcopy(fit)
        no_frozen.pop("industry_forecast")
        self.assertEqual(paths.build_paths(context, data, samples, no_frozen)["status"], "partial")
        changed = copy.deepcopy(data)
        original = copy.deepcopy(samples)
        for code in context["allocation_codes"]:
            rows = changed["nav"][code]
            for row in rows:
                row["distribution_per_share"] = .1
                for version in row["source_versions"]:
                    version["distribution_per_share"] = .1
                    version["corporate_actions"] = [{"kind":"cash_distribution", "per_share":.1}]
        result = paths.build_paths(context, changed, samples, fit)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["trade_ready"])
        self.assertEqual(samples, original)


class IndependentSourceWindowTests(unittest.TestCase):
    def recapture(self, row, at, *, nav=None, events=None):
        from contracts import fingerprint
        value = {key:copy.deepcopy(row[key]) for key in ("code","date","nav","distribution_per_share","corporate_actions")}
        if nav is not None:
            value["nav"] = nav
        if events is not None:
            value["corporate_actions"] = events
            value["distribution_per_share"] = sum(event["per_share"] for event in events)
        digest = fingerprint(value)
        version = {**value,"available_at":at,"observed_at":at,"raw_ref":{"source_id":"synthetic-recapture","sha256":digest},
            "availability_evidence":{"kind":"archived_original_capture","source_id":"synthetic-recapture","raw_sha256":digest,"captured_at":at}}
        version["version_id"] = fingerprint(version)
        row["source_versions"].append(version)

    def test_unchanged_original_recapture_preserves_first_maturity(self):
        context,data,samples,contract = self.inputs()
        before,_ = paths._frames(context,data,samples,contract)
        for row in data["nav"]["A"]:
            if row["date"] <= "2026-02-03":
                self.recapture(row,"2026-02-03T21:00:00+08:00")
        after,gaps = paths._frames(context,data,samples,contract)
        self.assertEqual(after,before,gaps)
        self.assertTrue(all(len(row["frame_versions"]) == 1 for row in after))

    def test_true_correction_and_reversion_preserve_new_mature_versions(self):
        context,data,samples,contract = self.inputs()
        row = next(row for row in data["nav"]["A"] if row["date"] == "2026-01-14")
        original = row["nav"]
        self.recapture(row,"2026-02-03T21:00:00+08:00",nav=original+.1)
        self.recapture(row,"2026-02-04T21:00:00+08:00",nav=original)
        frames,gaps = paths._frames(context,data,samples,contract)
        frame = next(row for row in frames if row["decision_date"] == "2026-01-09")
        self.assertEqual([row["label_available_date"] for row in frame["frame_versions"]],["2026-01-14","2026-02-03","2026-02-04"])
        self.assertNotEqual(frame["frame_versions"][1]["wealth_ratios"],frame["frame_versions"][0]["wealth_ratios"])
        self.assertEqual(frame["frame_versions"][2]["wealth_ratios"],frame["frame_versions"][0]["wealth_ratios"])
        chosen = paths._eligible(frames,["A"],"2026-02-04",{"train_window_days":90})
        self.assertEqual(next(row for row in chosen if row["decision_date"] == "2026-01-09")["label_available_date"],"2026-02-03")

    def test_source_cash_boundary_preserves_paid_same_day_and_future_ex_rights(self):
        from contracts import fingerprint
        from test_single_step import make_context
        import portfolio_mpc as mpc
        for ex_date in ("2029-12-31","2030-01-01","2030-01-02"):
            with self.subTest(ex_date=ex_date):
                event = {"kind":"cash_distribution","code":"C","id":"original-cash","record_date":"2029-12-31","ex_date":ex_date,
                    "pay_date":ex_date,"per_share":.1,"currency":"CNY","distribution_mode":"cash",
                    "entitlement_rule":{"subscribe_on_record_date":"excluded","redeem_on_record_date":"included"},
                    "known_at":"2029-12-30T20:00:00+08:00","evidence_ref":{"explicit_synthetic_original":"cash"}}
                raw = []
                for day in ("2029-12-31","2030-01-01","2030-01-02","2030-01-03","2030-01-04"):
                    value = {"code":"C","date":day,"nav":.9 if day>=ex_date else 1.,"distribution_per_share":.1 if day==ex_date else 0.,"corporate_actions":[event] if day==ex_date else []}
                    row = {**value,"source_versions":[]}
                    self.recapture(row,day+"T21:00:00+08:00")
                    raw.append(row)
                old = {"base_date":"2029-12-30","end_date":"2030-01-04","dividends":[event],"source_hash":"explicit-synthetic-old-label"}
                sample = {"code":"C","decision_date":"2030-01-01","industry_ready":True,"fund_group_id":"C","x":[0.]*45,"feature_source":{"base_nav":1.},"label_source":old}
                frames,gaps = paths._frames({"allocation_codes":["C"]},{"nav":{"C":raw}},[sample],{"codes":["C"],"offsets":[1,2,3]})
                self.assertEqual(len(frames),1,gaps)
                frame = frames[0];keys = paths._dictionary([frame]);future = ex_date>"2030-01-01"
                self.assertEqual(frame["label_source"]["distributions"],[event])
                self.assertEqual(len(keys),int(future))
                from test_strategy_execution import capture_engineering_marks
                model_contract = {"codes":["C"],"nav_dates":["2030-01-02","2030-01-03","2030-01-04"],
                    "origin_date":"2030-01-01","decision_at":"2030-01-01T20:00:00+08:00","price_schema":"source_role_price_paths_v3",
                    "known_marks":capture_engineering_marks({"prices":{"C":"1"},"price_dates":{"C":"2029-12-30"},"observed_at":"2030-01-01T20:00:00+08:00"})["known_marks"]}
                leaf = paths._decode({"C":{"values":paths._targets(frame,keys),"cash_keys":keys}}, {"C":1.}, model_contract,
                    "2030-01-01T20:00:00+08:00",[frame["label_source"]],"cash-boundary",1.,frame["label_available_at"])
                context = make_context(cash="0" if future else "1",shares="10")
                context["spec"]["planning"]["primary_horizon_days"] = 3
                result = mpc.simulate(context,leaf,{"buys":[],"sells":[]},{"kind":"hold","weights":{}},["2030-01-01","2030-01-04"])
                self.assertAlmostEqual(result["terminal_wealth"],10.)
                self.assertEqual(len(leaf["distributions"]),int(future))
                self.assertEqual(sample["label_source"],old)

    def inputs(self, holiday=False):
        import datetime as dt
        from contracts import fingerprint
        first,last=dt.date(2026,1,1),dt.date(2026,2,10)
        dates=[str(first+dt.timedelta(days=i)) for i in range((last-first).days+1) if (first+dt.timedelta(days=i)).weekday()<5]
        if holiday:dates.remove("2026-01-12")
        raw=[]
        for i,date in enumerate(dates):
            events=[]
            if date=="2026-01-13":
                events=[{"kind":"cash_distribution","id":"synthetic-after-oldH3","record_date":date,"ex_date":date,"pay_date":"2026-01-15",
                    "per_share":.05,"currency":"CNY","distribution_mode":"cash","entitlement_rule":{"buy":"owned_on_record_date","sell":"owned_on_record_date"},
                    "evidence_ref":{"synthetic_original_rights":date},"known_at":date+"T21:00:00+08:00"}]
            value={"code":"A","date":date,"nav":1+i*.01,"distribution_per_share":.05 if events else 0.,"corporate_actions":events}
            at=date+"T21:00:00+08:00";digest=fingerprint(value)
            version={**value,"available_at":at,"observed_at":at,"raw_ref":{"source_id":"synthetic-"+date,"sha256":digest},
                "availability_evidence":{"kind":"archived_original_capture","source_id":"synthetic-"+date,"raw_sha256":digest,"captured_at":at}}
            version["version_id"]=fingerprint(version);raw.append({**value,"source_versions":[version]})
        samples=[]
        for origin in ("2026-01-05","2026-01-06","2026-01-08","2026-01-09"):
            base=max(date for date in dates if date<origin)
            oldend=next(date for date in dates if date>=str(dt.date.fromisoformat(origin)+dt.timedelta(days=3)))
            nb=next(row["nav"] for row in raw if row["date"]==base)
            source={"code":"A","base_date":base,"base_nav":nb,"end_date":oldend,"dividends":[],"label_available_at":oldend+"T21:00:00+08:00"}
            source["source_hash"]=fingerprint(source)
            samples.append({"code":"A","decision_date":origin,"fund_group_id":"A","industry_ready":True,"x":[0.]*45,"feature_source":{"base_nav":nb},"label_source":source})
        return {"allocation_codes":["A"]},{"nav":{"A":raw}},samples,{"offsets":[1,2,5],"codes":["A"]}

    def test_weekdays_and_holiday_use_new_source_window_without_altering_h3(self):
        from contracts import fingerprint
        for holiday in (False,True):
            with self.subTest(holiday=holiday):
                context,data,samples,contract=self.inputs(holiday)
                original=copy.deepcopy(samples)
                frames,gaps=paths._frames(context,data,samples,contract)
                self.assertEqual({row["decision_date"] for row in frames},{"2026-01-05","2026-01-06","2026-01-08","2026-01-09"},gaps)
                friday=next(row for row in frames if row["decision_date"]=="2026-01-09")
                source=friday["label_source"]
                self.assertEqual(source["requested_phase_dates"],["2026-01-10","2026-01-11","2026-01-14"])
                self.assertEqual(source["actual_quote_dates"],["2026-01-13","2026-01-13","2026-01-14"] if holiday else ["2026-01-12","2026-01-12","2026-01-14"])
                self.assertEqual([quote["date"] for quote in source["nav_quotes"]],source["actual_quote_dates"])
                self.assertEqual([event["id"] for event in source["distributions"]],["synthetic-after-oldH3"])
                self.assertEqual(friday["label_available_at"],"2026-01-14T21:00:00+08:00")
                self.assertEqual(friday["source_hash"],fingerprint(source))
                self.assertEqual(samples,original)
                self.assertGreater(source["window_end_date"],samples[-1]["label_source"]["end_date"])

    def test_missing_after_h3_vintage_and_late_maturity_remain_gaps(self):
        from contracts import fingerprint
        context,data,samples,contract=self.inputs()
        row=next(row for row in data["nav"]["A"] if row["date"]=="2026-01-14")
        row["source_versions"]=[]
        frames,gaps=paths._frames(context,data,samples,contract)
        self.assertNotIn("2026-01-09",{row["decision_date"] for row in frames})
        self.assertTrue(any(row["decision_date"]=="2026-01-09" for row in gaps))
        context,data,samples,contract=self.inputs()
        row=next(row for row in data["nav"]["A"] if row["date"]=="2026-01-14")
        version=row["source_versions"][0]
        version.update(available_at="2026-01-20T21:00:00+08:00",observed_at="2026-01-20T21:00:00+08:00")
        version["availability_evidence"]["captured_at"]=version["available_at"]
        version["version_id"]=fingerprint({key:value for key,value in version.items() if key!="version_id"})
        frames,gaps=paths._frames(context,data,samples,contract)
        eligible=paths._eligible(frames,["A"],"2026-01-19",{"train_window_days":90})
        self.assertNotIn("2026-01-09",{row["decision_date"] for row in eligible})


if __name__ == "__main__":
    unittest.main()
