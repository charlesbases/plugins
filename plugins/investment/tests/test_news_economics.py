"""Controlled external transport/clock regressions, not investment-effect evidence."""
import copy
import datetime as dt
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))
from artifacts import Artifacts
from contracts import fingerprint, instant
from state_store import Store
import state_store
import pipeline
import news
import news_economics
import source_fetch

URL = "https://www.gov.cn/yaowen/liebiao/202001/content_123456.htm"


def open_analysis_at(store, artifacts, run_id, policy, at):
    """Produce a real authority under the isolated engineering operation clock."""
    authority = Store(store.root, store.plan)
    payload = {"news_policy": copy.deepcopy(policy)}
    operation = "analysis-operation-" + run_id
    with patch.object(state_store, "utc_now", return_value=at):
        authority.begin(operation, payload)
        with authority.lease(operation):
            return pipeline._open_analysis(payload, authority, artifacts, run_id)


class EconomicSourceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name), "engineering")
        self.store.begin("fixture", {"kind": "engineering"})
        self.enterContext(self.store.lease("fixture"))
        self.artifacts = Artifacts(self.store.base)
        self.clock = "2020-02-20T12:00:00Z"
        self.enterContext(patch.object(news, "utc_now", side_effect=lambda: self.clock))
        self.sources = {}
        self.policy = {"mode": "sealed_inputs", "sources": ["cn_state_council"], "max_age_seconds": 3600,
                       "required_source_groups": {"policy": ["cn_state_council"]}}

    def registry(self, manifest):
        return news.collection_policy(manifest, self.store, self.artifacts)[1]

    def collect(self, key, entries):
        self.news_run = open_analysis_at(self.store, self.artifacts, "analysis-"+key, self.policy, self.clock)
        def fetch(url, source_id, **kw):
            publication, quote = self.sources[url]
            text = '<meta name="PubDate" content="'+publication+'"><title>controlled fixture</title><div id="UCAP-CONTENT">'+quote+'<p>'+"明确标注的工程测试原文，不能当成真实市场训练样本。"*4+'</p></div>'
            raw = text.encode()
            return {"registry_source_id": source_id, "registry_hash": fingerprint(kw["registry"]),
                    "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
                    "content_type": "text/html", "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation",
                    "retrieved_at": self.clock, "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw), "raw_bytes": raw, "text": text}
        payload = {"run_id": self.news_run["run_id"], "cutoff_at": self.news_run["publish_cutoff"], "window_start": "2019-01-01T00:00:00Z",
                   "sources": [{"source_id": "cn_state_council", "urls": entries}], "max_urls_per_source": 10,
                   "required_source_groups": copy.deepcopy(self.news_run["news_policy"]["required_source_groups"])}
        with patch.object(source_fetch, "fetch", side_effect=fetch):
            return news.collect(payload, self.store, self.artifacts, key)

    def setup_release(self, *, archive_expectation=True, prior_value="2.0", expectation=True, actual_quote=None):
        actual_url, prior_url, forecast_url = URL, URL.replace("123456", "123457"), URL.replace("123456", "123458")
        actual = actual_quote or "2020年3月政策利率为3.0%。"
        prior = "2020年1月政策利率为"+prior_value+"%。"
        forecast = "预计2020年3月政策利率为2.5%。"
        self.sources = {actual_url: ("2020-04-01", actual), prior_url: ("2020-02-01", prior), forecast_url: ("2020-02-19", forecast)}
        if archive_expectation and expectation:
            self.collect("before-release", [forecast_url])
        self.clock = "2020-04-02T12:00:00Z"
        captured = self.collect("release", [actual_url, prior_url, forecast_url] if expectation else [actual_url, prior_url])
        def point(url, quote, token, period):
            row = next(item for item in captured["retrievals"] if item["requested_url"] == url)
            return {"version_id": row["version_id"], "quote": quote, "value_text": token, "unit": "percent", "period_label": period}
        current = point(actual_url, actual, "3.0", "2020年3月")
        event_id = next(row["document_id"] for row in captured["retrievals"] if row["requested_url"] == actual_url)
        claim = {"id": "c0", "kind": "fact", "text": actual, "version_id": current["version_id"], "quote": actual,
                 "categories": ["科技"], "direction": "watch", "counterevidence": [{"missing_reason": "controlled fixture"}],
                 "reviewer": "engineering-reviewer", "review_method": "verbatim_source"}
        event = {"event_key": None, "version_ids": [current["version_id"]], "claim_ids": ["c0"], "event_at": None,
                 "effective_from": None, "effective_until": None, "review_by": "2020-04-03T12:00:00Z", "supersedes": [], "retracts": [], "temporal_evidence": []}
        thesis = {"thesis_id": "temporary-argument", "sector_id": "CN_TECH", "kind": "industry", "label": "科技", "direction": "watch",
                  "horizon_days": 30, "search_terms": ["科技"], "claim_ids": ["c0"], "event_ids": [event_id],
                  "required_source_groups": ["policy"], "valid_until": "2020-04-03T12:00:00Z", "event_basis": "announcement_information"}
        observation = {"event_id": event_id, "sector_ids": ["CN_TECH"], "category": "monetary_policy", "metric_id": "policy_rate",
                       "metric_label": "政策利率", "current": current, "prior": point(prior_url, prior, prior_value, "2020年1月"),
                       "expectation": point(forecast_url, forecast, "2.5", "2020年3月") if expectation else None, "action": None}
        payload = {"collection_id": "release", "claims": [claim], "industry_theses": [thesis], "events": [event], "economic_observations": [observation]}
        spec = {"kind": "numeric_policy", "news": copy.deepcopy(self.news_run["news_policy"])}
        return payload, spec

    def transition_release(self, quote="2020年3月政策利率由2.0%调整为3.0%。", *, before="2.0", after="3.0", period="2020年3月"):
        payload, spec = self.setup_release(expectation=False, actual_quote=quote)
        observation = payload["economic_observations"][0]
        observation["current"].update(value_text=after, period_label=period, value_role="after")
        observation["prior"] = {**observation["current"], "value_text": before, "value_role": "before"}
        return payload, spec

    def test_public_scalar_rejects_coarsened_calendar_prefixes(self):
        cases = (("2020年3月15日", "2020年3月"), ("2020年3月15日", "2020年"),
                 ("2020年3月15日", "2020"), ("2020-03-15", "2020-03"),
                 ("2020-03-15", "2020"), ("2020年3月31日", "2020年3月"),
                 ("2020年3月", "2020年"), ("2020年第一季度", "2020年"))
        for source_period, label in cases:
            with self.subTest(source_period=source_period, label=label):
                fixture = EconomicSourceTests()
                fixture.setUp()
                try:
                    payload, _ = fixture.setup_release(expectation=False, actual_quote=source_period+"政策利率为3.0%。")
                    observation = payload["economic_observations"][0]
                    observation["current"]["period_label"], observation["prior"] = label, None
                    with self.assertRaisesRegex(ValueError, "complete source period granularity or date"):
                        news.assess(payload, fixture.store, fixture.artifacts, "coarsened-period")
                    self.assertIsNone(fixture.store.get("news-review", "coarsened-period"))
                    self.assertEqual(list(fixture.store.scan("news-economic-observation")), [])
                    self.assertEqual(list(fixture.store.scan("news-economic-first-known")), [])
                finally:
                    fixture.doCleanups()

    def test_public_scalar_rejects_multiple_complete_source_periods(self):
        cases = (("2020年3月15日至2020年3月16日政策利率为3.0%。", "2020年3月"),
                 ("2020年3月15日至2020年3月16日政策利率为3.0%。", "2020年3月15日"),
                 ("2020年3月15日政策利率为3.0%，相关公告发布于2020年3月16日。", "2020年3月15日"),
                 ("2020年3月政策利率为3.0%，依据2019年公告。", "2020年3月"))
        for quote, label in cases:
            with self.subTest(quote=quote, label=label):
                fixture = EconomicSourceTests()
                fixture.setUp()
                try:
                    payload, _ = fixture.setup_release(expectation=False, actual_quote=quote)
                    observation = payload["economic_observations"][0]
                    observation["current"]["period_label"], observation["prior"] = label, None
                    with self.assertRaisesRegex(ValueError, "one unambiguous complete source calendar period"):
                        news.assess(payload, fixture.store, fixture.artifacts, "ambiguous-period")
                    self.assertIsNone(fixture.store.get("news-review", "ambiguous-period"))
                    self.assertEqual(list(fixture.store.scan("news-economic-observation")), [])
                finally:
                    fixture.doCleanups()

    def test_public_registered_calendar_controls_keep_source_concept_and_replay_identity(self):
        cases = (("2020-03-15", "2020-03-15", "2020-03-15", "date"),
                 ("2020年3/15日", "2020年3/15日", "2020-03-15", "date"),
                 ("2020年3月", "2020年3月", "2020-03-31", "month"),
                 ("2020-03", "2020-03", "2020-03-31", "month"),
                 ("2020年第一季度", "2020年第一季度", "2020-03-31", "quarter"),
                 ("2020第一季度", "2020第一季度", "2020-03-31", "quarter"),
                 ("2020年", "2020年", "2020-12-31", "year"),
                 ("2020年", "2020", "2020-12-31", "year"),
                 ("２０２０-０３", "２０２０-０３", "2020-03-31", "month"),
                 ("２０２０年３月１５日", "２０２０年３月１５日", "2020-03-15", "date"))
        for source_period, label, period_end, period_type in cases:
            with self.subTest(source_period=source_period, label=label):
                fixture = EconomicSourceTests()
                fixture.setUp()
                try:
                    payload, spec = fixture.setup_release(expectation=False, actual_quote=source_period+"政策利率为3.0%。")
                    observation = payload["economic_observations"][0]
                    observation["current"]["period_label"], observation["prior"] = label, None
                    review = news.assess(payload, fixture.store, fixture.artifacts, "calendar-control")
                    actual = review["economic_observations"][0]
                    self.assertEqual(actual["current"]["period_label"], label)
                    self.assertEqual(actual["current"]["period_end"], period_end)
                    self.assertEqual(actual["source_concept"]["period_type"], period_type)
                    self.assertEqual(actual["source_concept"]["canonical_label"], "政策利率")
                    self.assertEqual(news.validate_review(review, spec, fixture.clock, fixture.artifacts,
                                                          store=fixture.store)["status"], "passed")
                    fixture.clock = "2020-04-02T12:05:00Z"
                    repeated = news.assess(payload, fixture.store, fixture.artifacts, "calendar-control-repeated")["economic_observations"][0]
                    self.assertEqual(repeated["observation_key"], actual["observation_key"])
                    self.assertEqual(repeated["known_at"], actual["known_at"])
                finally:
                    fixture.doCleanups()

    def test_public_scalar_rejects_impossible_source_day_even_with_month_label(self):
        payload, _ = self.setup_release(expectation=False, actual_quote="2020年2月30日政策利率为3.0%。")
        observation = payload["economic_observations"][0]
        observation["current"]["period_label"], observation["prior"] = "2020年2月", None
        with self.assertRaisesRegex(ValueError, "day is out of range"):
            news.assess(payload, self.store, self.artifacts, "impossible-source-day")
        self.assertIsNone(self.store.get("news-review", "impossible-source-day"))

    def test_public_scalar_requires_original_fullwidth_calendar_literal(self):
        payload, _ = self.setup_release(expectation=False, actual_quote="２０２０年３月１５日政策利率为3.0%。")
        observation = payload["economic_observations"][0]
        observation["current"]["period_label"], observation["prior"] = "2020年3月15日", None
        with self.assertRaisesRegex(ValueError, "not quoted from source"):
            news.assess(payload, self.store, self.artifacts, "normalized-label")
        self.assertIsNone(self.store.get("news-review", "normalized-label"))

    def test_public_scalar_does_not_extract_calendar_year_from_money(self):
        payload, spec = self.setup_release(expectation=False, actual_quote="2020年政策利率为3.0%，名义金额为2020元。")
        observation = payload["economic_observations"][0]
        observation["current"]["period_label"], observation["prior"] = "2020", None
        review = news.assess(payload, self.store, self.artifacts, "year-with-money")
        self.assertEqual(review["economic_observations"][0]["source_concept"]["period_type"], "year")
        self.assertEqual(news.validate_review(review, spec, self.clock, self.artifacts, store=self.store)["status"], "passed")
        fixture = EconomicSourceTests()
        fixture.setUp()
        try:
            payload, _ = fixture.setup_release(expectation=False, actual_quote="名义金额为2020元，政策利率为3.0%。")
            observation = payload["economic_observations"][0]
            observation["current"]["period_label"], observation["prior"] = "2020", None
            with self.assertRaisesRegex(ValueError, "one unambiguous complete source calendar period"):
                news.assess(payload, fixture.store, fixture.artifacts, "money-only")
            self.assertIsNone(fixture.store.get("news-review", "money-only"))
        finally:
            fixture.doCleanups()

    def test_public_chinese_daily_calendar_retains_day_granularity(self):
        payload, spec = self.setup_release(expectation=False, actual_quote="2020年3月15日政策利率为3.0%。")
        observation = payload["economic_observations"][0]
        observation["current"]["period_label"] = "2020年3月15日"
        observation["prior"] = None
        review = news.assess(payload, self.store, self.artifacts, "daily-source")
        actual = review["economic_observations"][0]
        self.assertEqual(actual["current"]["period_end"], "2020-03-15")
        self.assertEqual(actual["source_concept"]["period_type"], "date")
        self.assertEqual(news.validate_review(review, spec, self.clock, self.artifacts, store=self.store)["status"], "passed")

    def test_public_chinese_daily_calendar_rejects_impossible_date(self):
        payload, _ = self.setup_release(expectation=False, actual_quote="2020年2月30日政策利率为3.0%。")
        payload["economic_observations"][0]["current"]["period_label"] = "2020年2月30日"
        payload["economic_observations"][0]["prior"] = None
        with self.assertRaises(ValueError):
            news.assess(payload, self.store, self.artifacts, "impossible-day")
        self.assertIsNone(self.store.get("news-review", "impossible-day"))

    def test_public_typed_transition_roundtrips_source_roles_and_comparison(self):
        payload, spec = self.transition_release("自2020年3月15日起，政策利率由此前的2.0%上调至3.0%。", period="2020年3月15日")
        review = news.assess(payload, self.store, self.artifacts, "typed-transition")
        observation = review["economic_observations"][0]
        self.assertEqual((observation["prior"]["value"], observation["current"]["value"]), (2., 3.))
        self.assertEqual(observation["comparison_basis"], "source_stated_transition")
        self.assertEqual(observation["source_concept"]["period_type"], "date")
        self.assertEqual(observation["prior"]["source_transition"], observation["current"]["source_transition"])
        self.assertAlmostEqual(observation["relative_delta"], .5)
        self.assertIsNone(observation["relative_surprise"])
        self.assertEqual(news.validate_review(review, spec, self.clock, self.artifacts, store=self.store)["status"], "passed")
        self.clock = "2020-04-02T12:05:00Z"
        repeated = news.assess(payload, self.store, self.artifacts, "typed-transition-repeated")["economic_observations"][0]
        self.assertEqual(repeated["known_at"], observation["known_at"])
        self.assertEqual(repeated["observation_key"], observation["observation_key"])
        for field, value in (("value_role", "before"), ("source_transition", {})):
            changed = copy.deepcopy(review)
            changed.pop("manifest_ref")
            changed["economic_observations"][0]["current"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "recomputed original measurements"):
                news.validate_review(changed, spec, self.clock, self.artifacts, store=self.store)

    def test_public_transition_rejects_role_token_unit_and_period_tampering(self):
        payload, _ = self.transition_release()
        changes = (("value_role", "current"), ("value_role", "before"), ("value_text", "2.0"),
                   ("unit", "percentage_point"), ("period_label", "2020年2月"), ("measurement_type", "reported_change"))
        for index, (field, value) in enumerate(changes):
            changed = copy.deepcopy(payload)
            changed["economic_observations"][0]["current"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                news.assess(changed, self.store, self.artifacts, "transition-tamper-"+str(index))
        untyped = copy.deepcopy(payload)
        for point in (untyped["economic_observations"][0]["current"], untyped["economic_observations"][0]["prior"]):
            point.pop("value_role")
        with self.assertRaisesRegex(ValueError, "Ambiguous economic quote"):
            news.assess(untyped, self.store, self.artifacts, "untyped-transition")
        wrong_prior = copy.deepcopy(payload)
        wrong_prior["economic_observations"][0]["prior"] = {**wrong_prior["economic_observations"][0]["current"]}
        with self.assertRaises(ValueError):
            news.assess(wrong_prior, self.store, self.artifacts, "unpaired-transition")

    def test_public_transition_rejects_nonunique_or_contradictory_source_clause(self):
        clauses = ("2020年3月政策利率与准备金率由2.0%调整为3.0%。",
                   "2020年3月政策利率由2.0%调整为3.0%，同时提高0.1个百分点。",
                   "2020年3月政策利率未由2.0%调整为3.0%。",
                   "预计2020年3月政策利率由2.0%调整为3.0%。",
                   "2020年3月政策利率由2.0%下调为3.0%。",
                   "2020年3月政策利率由2.0%调整为3.0个百分点。",
                   "2020年3月政策利率由2.0%调整为银行贷款利率3.0%。",
                   "2020年3月政策利率由2.0%调整为3.0%。2020年3月准备金率为3.0%。",
                   "政策利率由2.0%调整为3.0%。")
        for index, quote in enumerate(clauses):
            with self.subTest(quote=quote):
                fixture = EconomicSourceTests()
                fixture.setUp()
                try:
                    payload, _ = fixture.transition_release(quote)
                    operation = "ambiguous-transition-"+str(index)
                    with self.assertRaises(ValueError):
                        news.assess(payload, fixture.store, fixture.artifacts, operation)
                    self.assertIsNone(fixture.store.get("news-review", operation))
                finally:
                    fixture.doCleanups()

    def test_public_transition_equal_numbers_still_require_roles(self):
        payload, spec = self.transition_release("2020年3月政策利率由2.0%调整为2.0%。", after="2.0")
        review = news.assess(payload, self.store, self.artifacts, "equal-typed")
        self.assertEqual(review["economic_observations"][0]["relative_delta"], 0.)
        self.assertEqual(news.validate_review(review, spec, self.clock, self.artifacts, store=self.store)["status"], "passed")
        for point in (payload["economic_observations"][0]["current"], payload["economic_observations"][0]["prior"]):
            point.pop("value_role")
        with self.assertRaisesRegex(ValueError, "Ambiguous economic quote"):
            news.assess(payload, self.store, self.artifacts, "equal-untyped")

    def test_public_transition_rejects_actual_clause_as_expectation(self):
        payload, _ = self.transition_release()
        payload["economic_observations"][0]["expectation"] = copy.deepcopy(payload["economic_observations"][0]["current"])
        with self.assertRaisesRegex(ValueError, "exactly two actual source values"):
            news.assess(payload, self.store, self.artifacts, "late-transition-expectation")

    def test_public_plain_prior_keeps_strict_earlier_period_requirement(self):
        payload, _ = self.setup_release(expectation=False)
        payload["economic_observations"][0]["prior"] = copy.deepcopy(payload["economic_observations"][0]["current"])
        with self.assertRaisesRegex(ValueError, "earlier source calendar period"):
            news.assess(payload, self.store, self.artifacts, "same-period-untyped-prior")

    def test_real_availability_prior_and_forecast_are_recomputed_and_replayed(self):
        payload, spec = self.setup_release()
        review = news.assess(payload, self.store, self.artifacts, "r1")
        row = review["economic_observations"][0]
        self.assertAlmostEqual(row["relative_delta"], .5)
        self.assertAlmostEqual(row["relative_surprise"], .2)
        self.assertEqual(row["expectation"]["known_at"], "2020-02-20T12:00:00Z")
        self.assertNotIn("comparison_basis", row)
        self.assertNotIn("value_role", row["current"])
        self.assertNotIn("source_transition", row["current"])
        self.assertEqual(news.validate_review(review, spec, self.clock, self.artifacts, store=self.store)["status"], "passed")
        self.assertEqual(news.assess(payload, self.store, self.artifacts, "r1"), review)
        renamed = copy.deepcopy(payload)
        renamed["industry_theses"][0]["thesis_id"] = "a-different-daily-argument"
        other = news.assess(renamed, self.store, self.artifacts, "r2")
        self.assertEqual(other["economic_observations"], review["economic_observations"])
        feature = news_economics.factual_event_features([row], "CN_TECH", self.clock)
        self.assertEqual(len(feature["feature_values"]), 84)
        self.assertEqual(feature["feature_values"][:5], [1, .5, 0, .2, 0])

    def test_stable_concept_alias_requires_the_original_relationship_quote(self):
        payload, _ = self.setup_release(actual_quote="2020年3月政策利率（又称基准利率）为3.0%。")
        observation = payload["economic_observations"][0]
        observation["concept"] = {"canonical_label": "基准利率", "alias_evidence": [
            {"version_id": observation["current"]["version_id"], "quote": observation["current"]["quote"]}]}
        review = news.assess(payload, self.store, self.artifacts, "source-alias")
        concept = review["economic_observations"][0]["source_concept"]
        self.assertEqual(concept["canonical_label"], "基准利率")
        self.assertEqual(concept["period_type"], "month")
        self.assertEqual(concept["unit"], "percent")
        invalid = copy.deepcopy(payload)
        invalid["economic_observations"][0]["concept"]["alias_evidence"] = []
        with self.assertRaisesRegex(ValueError, "cannot silently rename"):
            news.assess(invalid, self.store, self.artifacts, "unsupported-alias")

    def test_forecast_first_captured_after_release_is_rejected(self):
        payload, _ = self.setup_release(archive_expectation=False)
        with self.assertRaisesRegex(ValueError, "genuinely captured before actual release"):
            news.assess(payload, self.store, self.artifacts, "late")

    def test_polluted_first_observation_cannot_backdate_a_late_forecast(self):
        payload, _ = self.setup_release(archive_expectation=False)
        version = payload["economic_observations"][0]["expectation"]["version_id"]
        original_get = self.store.get
        def polluted_get(kind, key, **kwargs):
            row = original_get(kind, key, **kwargs)
            if kind == "news-version-first-observed" and key == version:
                return {**row, "observed_at": "2020-02-20T12:00:00Z"}
            return row
        with patch.object(self.store, "get", side_effect=polluted_get), self.assertRaisesRegex(
                ValueError, "original capture journal"):
            news.assess(payload, self.store, self.artifacts, "polluted-clock")

    def test_missing_and_zero_denominator_are_not_measured_zero_surprises(self):
        payload, _ = self.setup_release(prior_value="0", expectation=False)
        review = news.assess(payload, self.store, self.artifacts, "missing")
        row = review["economic_observations"][0]
        self.assertIsNone(row["relative_delta"])
        self.assertIsNone(row["relative_surprise"])
        feature = news_economics.factual_event_features([row], "CN_TECH", self.clock)
        self.assertEqual(feature["feature_values"][:5], [1, 0, 1, 0, 1])

    def test_altered_value_unit_and_caller_verified_flag_do_not_qualify(self):
        payload, _ = self.setup_release(expectation=False)
        for field, value in (("value_text", "9"), ("unit", "USD"), ("verified", True)):
            modified = copy.deepcopy(payload)
            modified["economic_observations"][0]["current"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                news.assess(modified, self.store, self.artifacts, "bad-"+field)

    def test_changed_persisted_economic_value_fails_source_reconstruction(self):
        payload, spec = self.setup_release(expectation=False)
        review = news.assess(payload, self.store, self.artifacts, "r1")
        changed = copy.deepcopy(review)
        changed.pop("manifest_ref")
        changed["economic_observations"][0]["current"]["value"] = 99
        with self.assertRaisesRegex(ValueError, "recomputed original measurements"):
            news.validate_review(changed, spec, self.clock, self.artifacts, store=self.store)

    def test_future_observations_and_unrelated_sector_do_not_enter_features(self):
        payload, _ = self.setup_release(expectation=False)
        row = news.assess(payload, self.store, self.artifacts, "r1")["economic_observations"][0]
        for sector, origin in (("CN_OTHER", self.clock), ("CN_TECH", "2020-04-01T12:00:00Z")):
            feature = news_economics.factual_event_features([row], sector, origin)
            self.assertEqual(feature["observations"], [])
            self.assertEqual(feature["feature_values"][:5], [0, 0, 1, 0, 1])

    def test_decline_word_derives_negative_change_and_level_change_mix_is_rejected(self):
        payload, _ = self.setup_release(expectation=False)
        manifest = self.artifacts.read_json(self.store.get("news-operation", fingerprint({"operation_id": "release"}))["manifest_ref"])
        row = payload["economic_observations"][0]["current"]
        quote = "2020年3月政策利率下降2.5%。"
        self.sources[URL] = ("2020-04-01", quote)
        other = self.collect("decline", [URL])
        point = {**row, "version_id": other["versions"][0]["version_id"], "quote": quote, "value_text": "2.5"}
        measured = news_economics._measurement(point, "policy_rate", "政策利率", other, self.artifacts, self.store, self.registry(other))
        self.assertEqual(measured["value"], -2.5)
        self.assertEqual(measured["measurement_type"], "reported_change")
        point["measurement_type"] = "level"
        with self.assertRaisesRegex(ValueError, "measurement type differs"):
            news_economics._measurement(point, "policy_rate", "政策利率", other, self.artifacts, self.store, self.registry(other))

    def test_negative_numeric_sign_cannot_be_removed_or_ambiguous_period_selected(self):
        payload, _ = self.setup_release(expectation=False)
        for name, quote, token in (("sign", "2020年3月政策利率为-2.5%。", "2.5"),
                                    ("period", "2020年1月与2020年3月政策利率为3.0%。", "3.0")):
            self.sources[URL] = ("2020-04-01", quote)
            collected = self.collect(name, [URL])
            point = {"version_id": collected["versions"][0]["version_id"], "quote": quote,
                     "value_text": token, "unit": "percent", "period_label": "2020年3月"}
            with self.subTest(name=name), self.assertRaises(ValueError):
                news_economics._measurement(point, "policy_rate", "政策利率", collected, self.artifacts, self.store, self.registry(collected))

    def test_quoted_forecast_cannot_be_submitted_as_current_actual(self):
        payload, _ = self.setup_release(expectation=False)
        quote = "预计2020年3月政策利率为3.0%。"
        self.sources[URL] = ("2020-04-01", quote)
        captured = self.collect("forecast-actual", [URL])
        point = {"version_id": captured["versions"][0]["version_id"], "quote": quote,
                 "value_text": "3.0", "unit": "percent", "period_label": "2020年3月"}
        with self.assertRaisesRegex(ValueError, "forecast cannot be presented as an actual"):
            news_economics._measurement(point, "policy_rate", "政策利率", captured, self.artifacts, self.store, self.registry(captured))

    def test_analysis_deadline_and_local_claim_id_do_not_refresh_source_information(self):
        payload, spec = self.setup_release(expectation=False)
        first = news.assess(payload, self.store, self.artifacts, "initial")
        previous_facts = news.factual_event_features(first, spec, self.clock, self.artifacts, store=self.store)
        changed = copy.deepcopy(payload)
        changed["events"][0]["review_by"] = "2020-04-04T12:00:00Z"
        changed["claims"][0]["id"] = "different-local-label"
        changed["events"][0]["claim_ids"] = ["different-local-label"]
        changed["industry_theses"][0]["claim_ids"] = ["different-local-label"]
        changed["industry_theses"][0]["thesis_id"] = "next-daily-report"
        self.clock = "2020-04-02T12:05:00Z"
        later = news.assess(changed, self.store, self.artifacts, "later")
        self.assertNotEqual(first["events"][0]["revision_id"], later["events"][0]["revision_id"])
        self.assertEqual(first["events"][0]["source_revision_id"], later["events"][0]["source_revision_id"])
        self.assertEqual(first["events"][0]["source_known_at"], later["events"][0]["source_known_at"])
        a, b = first["economic_observations"][0], later["economic_observations"][0]
        self.assertEqual(a["observation_key"], b["observation_key"])
        self.assertEqual(a["known_at"], b["known_at"])
        self.assertEqual(a["assessed_at"], "2020-04-02T12:00:00Z")
        self.assertEqual(b["assessed_at"], self.clock)
        self.assertEqual(news_economics.factual_event_features([b], "CN_TECH", "2020-04-02T12:01:00Z")["observations"], [])
        self.assertEqual(news_economics.factual_event_features([a, b], "CN_TECH", self.clock)["feature_values"][0], 1)
        facts = news.factual_event_features(later, spec, self.clock, self.artifacts, store=self.store)
        self.assertEqual(previous_facts["features"][0]["known_at"], facts["features"][0]["known_at"])
        self.assertEqual(previous_facts["features"][0]["revision_known_at"], facts["features"][0]["revision_known_at"])

    def test_frozen_registry_covers_measured_values_forecast_alias_and_replay(self):
        payload, spec = self.setup_release(actual_quote="2020年3月政策利率（又称基准利率）为3.0%。")
        observation = payload["economic_observations"][0]
        observation["concept"] = {"canonical_label": "基准利率", "alias_evidence": [
            {"version_id": observation["current"]["version_id"], "quote": observation["current"]["quote"]}]}
        revised = copy.deepcopy(source_fetch.load_registry())
        revised["sources"]["eu_ecb"]["priority"] += 1
        with patch.object(source_fetch, "load_registry", return_value=revised):
            review = news.assess(payload, self.store, self.artifacts, "frozen-registry")
            self.assertEqual(news.validate_review(review, spec, self.clock, self.artifacts, store=self.store)["status"], "passed")
        measured = review["economic_observations"][0]
        self.assertAlmostEqual(measured["relative_delta"], .5)
        self.assertAlmostEqual(measured["relative_surprise"], .2)
        self.assertEqual(measured["source_concept"]["canonical_label"], "基准利率")
        manifest = self.artifacts.read_json(review["collection_manifest_ref"])
        with self.assertRaisesRegex(ValueError, "not the authoritative run snapshot"):
            news_economics.assemble(payload["economic_observations"], manifest, review["claims"], review["events"],
                self.artifacts, self.clock, store=self.store, registry=revised)

    def test_all_supported_policy_actions_have_distinct_source_encodings(self):
        vectors = []
        for name, word in (("introduced", "印发"), ("removed", "废止"), ("unchanged", "维持")):
            payload, _ = self.setup_release(expectation=False)
            quote = "2020年3月医药产业政策现予"+word+"。"
            self.sources[URL] = ("2020-04-01", quote)
            captured = self.collect("action-"+name, [URL])
            event_id = captured["versions"][0]["document_id"]
            version = captured["versions"][0]["version_id"]
            payload["collection_id"] = "action-"+name
            payload["claims"][0].update(version_id=version, quote=quote, text=quote)
            payload["events"][0]["version_ids"] = [version]
            payload["industry_theses"][0]["event_ids"] = [event_id]
            payload["economic_observations"] = [{"event_id": event_id, "sector_ids": ["CN_TECH"], "category": "industry_policy",
                "metric_id": "policy_action", "metric_label": "医药产业政策", "current": None, "prior": None, "expectation": None,
                "action": {"version_id": version, "quote": quote, "value": name}}]
            revised = copy.deepcopy(source_fetch.load_registry())
            revised["sources"]["eu_ecb"]["priority"] += 1
            with patch.object(source_fetch, "load_registry", return_value=revised):
                review = news.assess(payload, self.store, self.artifacts, "review-"+name)
                self.assertEqual(news.validate_review(review, {"kind": "numeric_policy", "news": self.news_run["news_policy"]},
                    self.clock, self.artifacts, store=self.store)["status"], "passed")
            row = review["economic_observations"][0]
            vector = news_economics.factual_event_features([row], "CN_TECH", self.clock)["feature_values"]
            vectors.append(vector)
            index = news_economics.FEATURE_NAMES.index("economic_industry_policy_action_"+name+"_count")
            self.assertEqual(vector[index], 1)
        self.assertEqual(len({tuple(vector) for vector in vectors}), 3)


class OfficialAdapterTests(unittest.TestCase):
    def html(self, source, html, url):
        return news.parse_capture({"text": html, "content_type": "text/html", "final_url": url}, source_fetch.source_rule(source))

    def test_fed_article_uses_visible_publisher_date_not_fetch_time(self):
        body = "The Committee decided to maintain the target range. "*4
        parsed = self.html("us_fed", '<title>FOMC</title><div id="article"><p class="article__time">September 16, 2026</p>'+body+'</div>',
                           "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm")
        self.assertEqual(parsed["state"], "body")
        self.assertEqual(parsed["published_at"]["value"], "2026-09-16")
        self.assertEqual(parsed["published_at"]["precision"], "date")

    def test_ecb_navigation_section_is_not_an_article_body(self):
        body = "The Governing Council decided to change its interest rate. "*4
        parsed = self.html("eu_ecb", '<meta property="article:published_time" content="2026-09-10"><div class="section">NAVIGATION '*10+'</div><main><div class="section">'+body+'</div></main>',
                           "https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.example.en.html")
        self.assertEqual(parsed["body"], body.strip())
        self.assertNotIn("NAVIGATION", parsed["body"])

    def test_ecb_official_rss_double_slash_is_explicitly_normalized(self):
        rule = source_fetch.source_rule("eu_ecb")
        self.assertEqual(source_fetch.checked_url("https://www.ecb.europa.eu//press/key/date/2026/html/ecb.x.en.html", rule),
                         "https://www.ecb.europa.eu/press/key/date/2026/html/ecb.x.en.html")
        with self.assertRaises(ValueError):
            source_fetch.checked_url("https://www.ecb.europa.eu//other/data.html", rule)

    def test_rss_entities_and_unsupported_media_do_not_become_facts(self):
        rule = source_fetch.source_rule("eu_ecb")
        with self.assertRaisesRegex(ValueError, "Unsupported RSS"):
            news.parse_capture({"text": '<!DOCTYPE rss [<!ENTITY x "data">]><rss/>', "content_type": "application/rss+xml",
                                "final_url": rule["default_urls"][0]}, rule)

    def test_csrc_api_only_discovers_current_original_article_urls(self):
        import json
        data = {"data": {"page": 1, "rows": 18, "total": 36, "results": [
                {"url": "//www.csrc.gov.cn/csrc/c100028/c7661513/content.shtml", "title": "original", "contentHtml": "not a qualified body"}]}}
        rule = source_fetch.source_rule("cn_csrc")
        parsed = news.parse_capture({"text": json.dumps(data), "content_type": "application/json",
                                    "final_url": "https://www.csrc.gov.cn/searchList/"+"a"*32+"?page=1"}, rule)
        self.assertEqual(parsed["state"], "lead")
        self.assertIsNone(parsed["body"])
        self.assertIn("page=2", parsed["listing_links"][0])

    def test_ndrc_pagination_is_literal_and_keeps_three_distinct_source_channels(self):
        parsed = self.html("cn_ndrc_news", '<title>list</title><script>createPageHTML(10, 0, "index", "html")</script>',
                           "https://www.ndrc.gov.cn/xwdt/xwfb/")
        self.assertEqual(parsed["listing_links"], ["https://www.ndrc.gov.cn/xwdt/xwfb/index_1.html"])
        self.assertEqual(parsed["state"], "lead")
        for source in ("cn_ndrc_news", "cn_ndrc_policy", "cn_ndrc_interpretation"):
            self.assertEqual(source_fetch.source_rule(source)["purpose"], "news")


class CollectionContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name), "engineering")
        self.store.begin("fixture", {"kind": "engineering"})
        self.enterContext(self.store.lease("fixture"))
        self.artifacts = Artifacts(self.store.base)

    def capture(self, url, source, text, registry):
        from contracts import utc_now
        raw = text.encode()
        return {"registry_source_id": source, "registry_hash": fingerprint(registry), "requested_url": url,
                "final_url": url, "redirect_chain": [], "http_status": 200, "content_type": "text/html", "encoding": "utf-8",
                "transport": "HTTPS_default_certificate_validation", "retrieved_at": utc_now(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "raw_bytes": raw, "text": text}

    def test_queued_listing_pages_obey_limit_without_losing_pending_frontier(self):
        from contracts import utc_now
        registry = source_fetch.load_registry()
        rule = registry["sources"]["cn_ndrc_news"]
        registry["sources"] = {"cn_ndrc_news": rule}
        base = rule["default_urls"][0]
        requested = []
        def fetch(url, source, **kw):
            requested.append(url)
            text = '<title>list</title><a href="index_1.html">one</a><a href="index_2.html">two</a>'
            return self.capture(url, source, text, registry)
        payload = {"cutoff_at": utc_now(), "window_start": "2020-01-01T00:00:00Z", "max_urls_per_source": 10,
                   "max_pages_per_source": 2, "required_source_groups": {"industry": ["cn_ndrc_news"]}}
        with patch.object(source_fetch, "load_registry", return_value=registry), patch.object(source_fetch, "fetch", side_effect=fetch):
            policy = {"mode":"sealed_inputs","sources":["cn_ndrc_news"],"max_age_seconds":3600,
                      "required_source_groups":copy.deepcopy(payload["required_source_groups"])}
            run = open_analysis_at(self.store,self.artifacts,"listing-run",policy,utc_now())
            payload.update(run_id=run["run_id"],cutoff_at=run["publish_cutoff"])
            result = news.collect(payload, self.store, self.artifacts, "pages")
        self.assertEqual(requested, [base, base+"index_1.html"])
        self.assertEqual(result["source_results"][0]["listing_pages_visited"], 2)
        self.assertIn(base+"index_2.html", result["source_results"][0]["pending_urls"])
        self.assertFalse(result["coverage_complete_for_source_time_window"])

    def test_refresh_detects_source_revision_without_rewriting_first_seen(self):
        from contracts import utc_now
        import json
        registry = source_fetch.load_registry()
        rule = registry["sources"]["cn_state_council"]
        registry["sources"] = {"cn_state_council": rule}
        revision = ["original"]
        feed = rule["default_urls"][0]
        def fetch(url, source, **kw):
            if url == feed:
                row = self.capture(url, source, json.dumps([{"URL": URL, "TITLE": "fixture"}]), registry)
                row["content_type"] = "application/json"
                return row
            return self.capture(url, source, '<meta name="PubDate" content="2020-01-01"><div id="UCAP-CONTENT">'+
                                revision[0]+"工程合成正文用于检验原文修订保留，不是市场有效性数据。"*4+'</div>', registry)
        payload = {"cutoff_at": utc_now(), "window_start": "2020-01-01T00:00:00Z", "max_urls_per_source": 10,
                   "refresh_tracked_articles": 1, "required_source_groups": {"policy": ["cn_state_council"]}}
        with patch.object(source_fetch, "load_registry", return_value=registry), patch.object(source_fetch, "fetch", side_effect=fetch):
            policy = {"mode":"sealed_inputs","sources":["cn_state_council"],"max_age_seconds":3600,
                      "required_source_groups":copy.deepcopy(payload["required_source_groups"])}
            run = open_analysis_at(self.store,self.artifacts,"refresh-first-run",policy,utc_now())
            payload.update(run_id=run["run_id"],cutoff_at=run["publish_cutoff"])
            first = news.collect(payload, self.store, self.artifacts, "refresh-one")
            original = self.store.get("news-document", first["versions"][0]["document_id"])
            revision[0] = "revised"
            run = open_analysis_at(self.store,self.artifacts,"refresh-second-run",policy,utc_now())
            second = news.collect({**payload, "run_id":run["run_id"],"cutoff_at":run["publish_cutoff"]}, self.store, self.artifacts, "refresh-two")
        self.assertNotEqual(first["versions"][0]["version_id"], second["versions"][0]["version_id"])
        current = self.store.get("news-document", second["versions"][0]["document_id"])
        self.assertEqual(current["first_seen_at"], original["first_seen_at"])
        self.assertEqual(current["previous_version_id"], first["versions"][0]["version_id"])


if __name__ == "__main__":
    unittest.main()
