"""Hand-specified synthetic timing/revision contracts; not market evidence."""
import copy
import datetime as dt
import unittest

import allocation_market as market
import allocation_statistics as stats
import numeric_validation
import portfolio_paths
import research_data
import candidate_readiness
from contracts import fingerprint
from test_research_data import normalized
import test_research_audit as audit_fixture
from test_allocation_statistics import sample_rows, training_policy, enrich_row, day


def capture(at, identity="synthetic-NAV", archived=False):
    value = {"source_id": identity, "sha256": "a"*64, "retrieved_at": at}
    if archived:
        value.update(available_at=at, availability_evidence={"kind": "archived_original_capture",
            "source_id": identity, "raw_sha256": "a"*64, "captured_at": at})
    return value


class NavVintageContracts(unittest.TestCase):
    def test_today_capture_cannot_invent_historical_availability(self):
        rows = research_data.bind_source_versions(normalized(1), capture("2021-01-01T20:00:00+08:00"))
        version = rows[0]["source_versions"][0]
        self.assertEqual(version["observed_at"], "2021-01-01T20:00:00+08:00")
        self.assertIsNone(version["available_at"])
        bad = capture("2021-01-01T20:00:00+08:00")
        bad["available_at"] = "2020-01-01T20:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "original archived capture"):
            research_data.bind_source_versions(normalized(1), bad)

    def test_late_nav_revision_cannot_change_an_earlier_feature(self):
        original = normalized(125)
        old_at = (dt.date(2020,1,1)+dt.timedelta(days=124)).isoformat()+"T20:00:00+08:00"
        old = capture(old_at, archived=True)
        current = copy.deepcopy(original)
        current[120]["nav"] = 4.
        bound = research_data.bind_source_versions(current, capture("2021-01-01T20:00:00+08:00"),
            [{"rows": original, "capture_metadata": old}])
        features, _, _ = research_data.build_samples(bound, {"code":"123456","fund_group_id":"fund"}, [1],120,"2021-01-01T20:00:00+08:00")
        feature = features[-1]
        before = market._feature_at(feature, bound, "2020-05-06T08:00:00+08:00",1)
        after = market._feature_at(feature, bound, "2021-01-02T08:00:00+08:00",1)
        self.assertEqual(before[0], [0.,0.,0.,0.,0.])
        self.assertTrue(before[1]["strict_PIT_verified"])
        self.assertAlmostEqual(after[0][4], .5)
        self.assertFalse(after[1]["strict_PIT_verified"])

    def test_future_label_revision_preserves_prior_fit_and_training_hash(self):
        rows = sample_rows(90)
        for row in rows:
            version = {key: copy.deepcopy(row[key]) for key in
                ("label_available_date","label_source","targets","latent_targets","return")}
            version["label_available_at"] = row["label_available_date"]+"T20:00:00+08:00"
            row["label_versions"] = [version]
        prediction = [{"code":code,"decision_date":day(80),"horizon_days":3,"x":[.2,.1]} for code in ("A","B")]
        before = stats._fit_mean_at(rows,prediction,training_policy())
        changed = copy.deepcopy(rows)
        for row in changed:
            revised = enrich_row({**row,"return":.3,"label_available_date":day(100)})
            version = {key: copy.deepcopy(revised[key]) for key in
                ("label_available_date","label_source","targets","latent_targets","return")}
            version["label_available_at"] = day(100)+"T20:00:00+08:00"
            row.update(version)
            row["label_versions"].append(version)
        after = stats._fit_mean_at(changed,prediction,training_policy())
        self.assertEqual(before["status"],"fitted")
        self.assertEqual(before,after)

    def test_label_and_middle_path_wait_for_actual_vintage_maturity(self):
        rows = [{"code":"A","date":f"2020-01-0{i}","nav":1.,"cumulative_nav":1.,"distribution_per_share":0.} for i in range(1,5)]
        archives = [{"rows":[row],"capture_metadata":capture(f"2020-01-0{i+1}T20:00:00+08:00",f"archive-{i}",True)} for i,row in enumerate(rows,1)]
        revised = copy.deepcopy(rows)
        revised[2].update(nav=2., cumulative_nav=2.)
        archives.append({"rows": copy.deepcopy(revised), "capture_metadata": capture("2020-01-10T20:00:00+08:00", "revision-original", True)})
        revised = research_data.bind_source_versions(revised,capture("2020-01-12T20:00:00+08:00"),archives)
        source = {"code":"A","base_date":"2020-01-01","base_nav":1.,"pricing_date":"2020-01-02",
            "pricing_nav":1.,"ownership_date":"2020-01-02","confirmation_date":"2020-01-02",
            "end_date":"2020-01-04","terminal_nav":1.,"dividends":[],"clock":{},"feature_cutoff_date":"2020-01-01"}
        sample = {"code":"A","decision_date":"2020-01-02","fund_group_id":"fund",
            "x":[0.]*(len(market.FEATURE_NAMES)+len(portfolio_paths.industry_model.FUND_FEATURE_NAMES)),
            "industry_ready":True,"label_available_date":"2020-01-05",
            "feature_source":{"strict_PIT_verified":True, "base_nav": source["base_nav"]}}
        versions = market._label_versions(sample,source,{dt.date.fromisoformat(row["date"]):row for row in revised})
        sample.update(versions[-1],label_versions=versions)
        self.assertIsNone(market.mature_label_at(sample,"2020-01-05"))
        old = market.mature_label_at(sample,"2020-01-07")
        late = market.mature_label_at(sample,"2020-01-11")
        self.assertEqual(old["label_source"]["source_quotes"][1]["nav"],1.)
        self.assertEqual(late["label_source"]["source_quotes"][1]["nav"],2.)
        frames, missing = portfolio_paths._frames({"allocation_codes":["A"]}, {"nav":{"A":revised}}, [sample], {"offsets":[1,2]})
        self.assertEqual(missing,[])
        self.assertEqual(portfolio_paths._eligible(frames,["A"],"2020-01-07",{"train_window_days":30})[0]["wealth_ratios"],[1.,1.])
        self.assertEqual(portfolio_paths._eligible(frames,["A"],"2020-01-11",{"train_window_days":30})[0]["wealth_ratios"],[2.,1.])

    def test_raw_audit_rejects_resealed_vintage_value_or_time(self):
        audit = audit_fixture.audit
        fixture = audit_fixture.ResearchAuditTests("test_constant_market_fixture_passes_with_known_maturity_counts")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        metadata = fixture.sources[0]
        row = research_data.bind_source_versions(fixture.normalized,metadata)[0]
        sources = {metadata["source_id"]:metadata}
        audit.verify_nav_versions(row,sources,fixture.run)
        bad = copy.deepcopy(row)
        bad["source_versions"][0]["nav"] = 2.
        bad["source_versions"][0]["version_id"] = fingerprint({key:value for key,value in bad["source_versions"][0].items() if key != "version_id"})
        with self.assertRaisesRegex(ValueError,"version NAV"):
            audit.verify_nav_versions(bad,sources,fixture.run)
        bad = copy.deepcopy(row)
        bad["source_versions"][0]["available_at"] = "2019-01-01T20:00:00+08:00"
        bad["source_versions"][0]["version_id"] = fingerprint({key:value for key,value in bad["source_versions"][0].items() if key != "version_id"})
        with self.assertRaisesRegex(ValueError,"times differ"):
            audit.verify_nav_versions(bad,sources,fixture.run)

    def test_numeric_validator_rebuilds_actual_release_features_and_maturity(self):
        original = normalized(140)
        archives = []
        for index,row in enumerate(original):
            at = (dt.date(2020,1,1)+dt.timedelta(days=index+2)).isoformat()+"T20:00:00+08:00"
            archives.append({"rows":[row],"capture_metadata":capture(at,f"archive-{index}",True)})
        bound = [research_data.bind_source_versions([row],bundle["capture_metadata"])[0]
                 for row,bundle in zip(original,archives)]
        features, _, _ = research_data.build_samples(bound,{"code":"123456","fund_group_id":"fund"},[3],120,"2021-01-01T20:00:00+08:00")
        data = {"nav":{"123456":bound},"features":{"123456":features},"code_info":{"123456":{"fund_group_id":"fund"}}}
        clock = {"order_time_local":"08:00:00","assets":{"123456":{"after_cutoff":False,
            "confirmation_rule":{"lag_days":0,"day_basis":"trading_days"},"ownership_start":"execution_date","holding_start":"execution_date"}}}
        context = {"as_of":"2020-05-10","model_request":{"nav_availability_calendar_lag":1,"horizon_days":3}}
        samples = market.build_single_step_samples(data["nav"],data["features"],data["code_info"],3,1,context["as_of"],clock)
        self.assertTrue(samples)
        self.assertEqual(samples[0]["x"], [0.,0.,0.,0.,0.,3])
        self.assertEqual(samples[0]["label_available_date"], (dt.date.fromisoformat(samples[0]["exit_date"])+dt.timedelta(days=2)).isoformat())
        self.assertEqual(numeric_validation.validate_samples(samples,{"data":data,"context":context,"clock":clock})["status"],"passed")

    def test_partial_archive_does_not_fill_late_known_window_from_latest_nav(self):
        rows = normalized(125)
        bound = research_data.bind_source_versions(rows,capture("2021-01-01T20:00:00+08:00"),
            [{"rows":[rows[0]],"capture_metadata":capture("2020-01-02T20:00:00+08:00",archived=True)}])
        features,_,_ = research_data.build_samples(bound,{"code":"123456","fund_group_id":"fund"},[1],120,"2021-01-01T20:00:00+08:00")
        self.assertIsNone(market._feature_at(features[0],bound,"2020-05-06T08:00:00+08:00",1))
        self.assertIsNone(market.nav_snapshot(bound[1],"2020-05-06T08:00:00+08:00",require_known=True))

    def test_late_capture_metadata_does_not_rewrite_earlier_quote(self):
        original = normalized(1)
        original[0]["provider_daily_return"] = 0.
        metadata = capture("2020-01-02T20:00:00+08:00",archived=True)
        before = research_data.bind_source_versions(original,metadata)
        changed = copy.deepcopy(original)
        changed[0].update(nav=4.,provider_daily_return=.99,distribution_text="later source revision")
        after = research_data.bind_source_versions(changed,capture("2021-01-01T20:00:00+08:00"),
            [{"rows":original,"capture_metadata":metadata}])
        first = market.nav_snapshot(before[0],"2020-01-03T08:00:00+08:00")
        self.assertEqual(first,market.nav_snapshot(after[0],"2020-01-03T08:00:00+08:00"))
        self.assertEqual(first["provider_daily_return"],0.)
        self.assertEqual(first["distribution_text"],"")

    def test_candidate_dates_use_mature_original_label_version(self):
        row = sample_rows(1)[0]
        first = {key: copy.deepcopy(row[key]) for key in ("label_available_date","label_source","targets","latent_targets","return")}
        first["label_available_at"] = first["label_available_date"]+"T20:00:00+08:00"
        later = {**copy.deepcopy(first),"label_available_date":day(25),"label_available_at":day(25)+"T20:00:00+08:00"}
        row.update(later,label_versions=[first,later],industry_ready=True)
        self.assertEqual(candidate_readiness._mature_dates([row],"A",day(20),30),{day(0)})


if __name__ == "__main__":
    unittest.main()
