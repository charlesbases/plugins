"""Synthetic boundary tests; live fund inputs are never encoded in production."""
import copy
import datetime as dt
import hashlib
import json
import re
import urllib.parse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/investment/scripts"))
import fund_universe as funds
import source_fetch
import source_documents
import fund_screen
from artifacts import Artifacts
from contracts import fingerprint, utc_now
from state_store import Store

ROWS = [["123451", "GOLD", "样例黄金A", "指数型-其他", "GOLD"],
        ["123452", "GOLD", "样例黄金C", "指数型-其他", "GOLD"],
        ["123453", "OTHER", "另一黄金观察A", "指数型-股票", "OTHER"],
        ["654321", "BOND", "样例债券A", "债券型-长债", "BOND"]]


def profile(code, legal_name=None, category=None):
    row = next(row for row in ROWS if row[0] == code)
    legal = legal_name or ("样例黄金基金" if code in ("123451", "123452") else "另一" + row[2] + "基金")
    target = "黄金9999" if code in ("123451", "123452") else "中证股票指数"
    return (f'<table><tr><th>基金全称</th><td>{legal}</td><th>基金简称</th><td>{row[2]}</td></tr>'
            f'<tr><th>基金代码</th><td>{code}</td><th>基金类型</th><td>{category or row[3]}</td></tr>'
            '<tr><th>基金管理人</th><td><a href="//fund.eastmoney.com/company/8888.html">样例管理人</a></td>'
            '<th>基金经理人</th><td><a href="//fund.eastmoney.com/manager/9999.html">样例经理</a></td></tr>'
            f'<tr><th>交易币种</th><td>人民币</td><th>交易场所</th><td>场外</td></tr>'
            f'<tr><th>份额类别</th><td>{row[2][-1]}</td></tr>'
            f'<tr><th>跟踪标的</th><td>{target}</td><th>业绩比较基准</th><td>{target}</td></tr></table>'
            '<h4><label>投资范围</label></h4><div></div><p>本基金主要投资债券。</p><h4>投资策略</h4>')


def capture(url, source_id, **kwargs):
    text = 'var r = ' + json.dumps(ROWS, ensure_ascii=False) + ';' if source_id == "eastmoney_catalog" else profile(Path(url).stem.split('_')[1])
    raw = text.encode("utf-8")
    return {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
            "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
            "content_type": "application/javascript" if source_id == "eastmoney_catalog" else "text/html",
            "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation", "retrieved_at": utc_now(),
            "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "text_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": raw, "text": text}


def thesis():
    return {"industry_theses": [{"thesis_id": "gold", "kind": "asset_class", "label": "黄金",
                                "direction": "increase", "horizon_days": 90, "search_terms": ["黄金"],
                                "claim_ids": ["test-news-claim"], "evidence_refs": [{"test_only": True}]}]}


class FundUniverseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "investment", "fixture")
        self.store.begin("fixture", {"kind": "test"})
        self.enterContext(self.store.lease("fixture"))
        self.artifacts = Artifacts(self.store.base)
        self.patch = patch.object(source_fetch, "fetch", side_effect=capture)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    @staticmethod
    def plan():
        record = {"schema_version": 4, "revision_count": 1, "previous_hash": None, "effective_at": utc_now(),
                  "user_source": {"message": "Synthetic explicit constraints", "confirmed_at": utc_now()},
                  "constraints": {"platform": "TT", "currency": "CNY", "goal": "synthetic",
                      "excluded_categories": [],
                      "position_limits": {"fund_group_limits": {}, "sector_limits": {}}}}
        record["user_source"]["confirmed_at"] = record["effective_at"]
        return {**record, "revision_id": fingerprint(record)}

    @staticmethod
    def policy():
        return {"market": "CN_public_funds", "platform": "TT", "currency": "CNY",
                "supported_instruments": ["off_exchange_nav"], "comparison_groups": ["same_legal_fund", "same_benchmark"],
                "require_complete_group": True}

    @staticmethod
    def state(review):
        return {"active_thesis_ids": [row["thesis_id"] for row in review["industry_theses"]],
                "blocked_theses": {}, "event_frontier_hash": "synthetic"}

    def discover(self, review, held, store, artifacts, operation, policy, **kwargs):
        if not hasattr(self, "plan_record"):
            self.plan_record = self.plan()
        return funds.discover(review, held, store, artifacts, operation, policy, universe_policy=self.policy(),
                              news_state=self.state(review), plan_constraints=self.plan_record, **kwargs)

    def validate(self, result, review, held, artifacts, as_of, **kwargs):
        return funds.validate_discovery(result, review, held, artifacts, as_of, news_state=self.state(review),
                                        plan_constraints=self.plan_record, **kwargs)

    def identities(self, codes):
        catalog = funds.collect_catalog(self.store, self.artifacts, "catalog")
        reference = funds.resolve_identity(codes, catalog, self.artifacts, store=self.store)
        return self.artifacts.read_json(reference)

    def test_directory_drives_identity_and_ac_shares_have_one_legal_group(self):
        snapshot = self.identities(["123451", "123452", "654321"])
        verified = funds.verify_identity(snapshot, self.artifacts)
        self.assertEqual(verified["123451"]["fund_group_id"], verified["123452"]["fund_group_id"])
        self.assertNotEqual(verified["123451"]["fund_group_id"], verified["654321"]["fund_group_id"])
        self.assertEqual(verified["123452"]["share_class"], "C")
        self.assertIsNone(verified["123451"]["regulatory_parent_code"])
        self.assertIn("主要投资债券", verified["654321"]["investment_scope"])

    def test_missing_directory_category_is_resolved_from_the_profile(self):
        rows = copy.deepcopy(ROWS)
        rows[0][3] = ""
        entries = funds.parse_catalog('var r = ' + json.dumps(rows) + ';')
        identity = funds.parse_profile(profile("123451"), "123451", entries["123451"])
        self.assertEqual(identity["type"], "指数型-其他")

    def test_raw_profile_conflict_requires_research_and_cannot_be_self_attested(self):
        def conflicting(url, sid, **kwargs):
            value = capture(url, sid, **kwargs)
            if sid == "eastmoney_profile":
                text = value["text"].replace("指数型-其他", "债券型-长债")
                raw = text.encode("utf-8")
                value.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(),
                             text_sha256=hashlib.sha256(raw).hexdigest())
            return value
        with patch.object(source_fetch, "fetch", side_effect=conflicting):
            snapshot = self.identities(["123451"])
        self.assertEqual(snapshot["identities"], {})
        self.assertEqual(snapshot["required_actions"][0]["action"], "retrieve_original_issuer_disclosure")
        snapshot = self.identities(["123452"])
        snapshot["identities"]["123452"]["fund_group_id"] = "self-attested"
        with self.assertRaisesRegex(ValueError, "differ from original"):
            funds.verify_identity(snapshot, self.artifacts)

    def test_expired_identity_sources_require_new_capture(self):
        snapshot = self.identities(["123451"])
        later = (dt.datetime.fromisoformat(snapshot["observed_at"]) + dt.timedelta(days=2)).isoformat()
        with self.assertRaisesRegex(ValueError, "stale"):
            funds.verify_identity(snapshot, self.artifacts, as_of=later)

    def test_discovery_verifies_mandates_preserves_holdings_and_reports_budget(self):
        result = self.discover(thesis(), ["654321"], self.store, self.artifacts, "discovery", {"max_profile_requests": 3})
        self.assertEqual(result["codes"], ["123451", "123452", "654321"])
        self.assertEqual(result["monitor_codes"], ["654321"])
        self.assertEqual(result["coverage"]["unexamined_codes"], ["123453"])
        self.assertEqual(len(result["attempted_codes"]), 3)
        self.assertIsNone(result["exposures"]["123451"][0]["weight"])
        self.assertEqual(self.validate(result, thesis(), ["654321"], self.artifacts, utc_now())["status"], "ready_with_pending_groups")
        result["codes"].append("123453")
        with self.assertRaisesRegex(ValueError, "sealed artifact"):
            self.validate(result, thesis(), ["654321"], self.artifacts, utc_now())

    def test_name_only_match_remains_an_explicit_research_task(self):
        result = self.discover(thesis(), [], self.store, self.artifacts, "all", {"max_profile_requests": 4})
        self.assertNotIn("123453", result["codes"])
        self.assertTrue(any(row["code"] == "123453" for row in result["required_actions"]))

    def test_source_impacted_watch_and_conflict_candidates_reach_numerical_comparison(self):
        review = thesis()
        review["industry_theses"][0]["direction"] = "watch"
        result = self.discover(review, ["123451"], self.store, self.artifacts, "watch", {})
        self.assertEqual(result["codes"], ["123451", "123452"])
        self.assertEqual(result["completed_group_candidate_codes"], ["123451", "123452"])
        self.assertEqual(result["eligible_buy_codes"], [])  # Original fee contracts are still required.
        self.assertEqual(self.validate(result, review, ["123451"], self.artifacts, utc_now())["eligible_buy_codes"], [])
        review = thesis()
        negative = {**review["industry_theses"][0], "thesis_id": "gold-counterevidence", "direction": "decrease"}
        review["industry_theses"].append(negative)
        result = self.discover(review, [], self.store, self.artifacts, "conflict", {})
        self.assertEqual(result["codes"], ["123451", "123452"])
        self.assertEqual(result["completed_group_candidate_codes"], ["123451", "123452"])
        self.assertEqual(result["eligible_buy_codes"], [])
        self.assertEqual(result["status"], "ready_with_pending_groups")
        self.assertTrue(any(row["action"] == "resolve_industry_direction_conflict" for row in result["required_actions"]))

    def test_source_label_roles_cannot_be_reversed_by_a_caller(self):
        identity = self.identities(["123451", "123452"])["identities"]["123452"]
        raw = ("<p>基金代码:123452</p><p>基金主代码123451 A类123451 C类123452</p>").encode("utf8")
        document = {**source_documents.extract_document(raw, "text/html", "utf8"),
                    "document_id": "source-fixture", "raw_sha256": hashlib.sha256(raw).hexdigest()}
        mapping = {"code": "123452", "source_id": "issuer_cmfchina", "url": "fixture",
                   "role": "identity", "locators": [row["locator"] for row in document["blocks"]]}
        parsed = funds._identity_from_document(document, mapping, identity)
        self.assertEqual((parsed["share_class"], parsed["disclosed_parent_code"]), ("C", "123451"))
        with self.assertRaisesRegex(ValueError, "Current issuer disclosure"):
            funds._identity_from_document(document, {**mapping, "share_class": "A"}, identity)
        mapping["code"] = "123451"
        with self.assertRaisesRegex(ValueError, "primary product"):
            funds._identity_from_document(document, mapping, identity)

    def test_budget_cannot_promote_partial_group_and_continuation_completes_it(self):
        first = self.discover(thesis(), [], self.store, self.artifacts, "partial", {"max_profile_requests": 1})
        self.assertEqual(first["completed_group_candidate_codes"], [])
        self.assertEqual(first["status"], "awaiting_coverage")
        next_batch = self.discover(thesis(), [], self.store, self.artifacts, "continued", {"max_profile_requests": 1},
                                   continue_from=first["candidate_ref"])
        self.assertEqual(next_batch["completed_group_candidate_codes"], ["123451", "123452"])
        self.assertEqual(next_batch["eligible_buy_codes"], [])
        self.assertEqual(next_batch["coverage"]["reused_profiles"], 1)
        self.validate(next_batch, thesis(), [], self.artifacts, utc_now())

    def test_monitoring_role_does_not_create_buy_eligibility(self):
        result = self.discover(thesis(), [], self.store, self.artifacts, "monitor", {"max_profile_requests": 1},
                               monitoring_codes=["654321"])
        self.assertEqual(result["codes"], ["654321"])
        self.assertEqual(result["completed_group_candidate_codes"], [])
        self.validate(result, thesis(), [], self.artifacts, utc_now(), monitoring_codes=["654321"])

    def test_incomplete_pipeline_stage_reuses_committed_discovery_without_fetching(self):
        first = self.discover(thesis(), ['654321'], self.store, self.artifacts, 'recover', {'max_profile_requests': 3})
        with patch.object(source_fetch, 'fetch', side_effect=AssertionError('Committed candidate evidence must be reused')):
            recovered = self.discover(thesis(), ['654321'], self.store, self.artifacts, 'recover', {'max_profile_requests': 3})
        self.assertEqual(first, recovered)

    def test_resealed_watch_result_cannot_discard_source_mandate_research_tasks(self):
        review = thesis()
        review['industry_theses'][0]['direction'] = 'watch'
        result = self.discover(review, [], self.store, self.artifacts, 'watch-needs-source', {'max_profile_requests': 4})
        self.assertEqual(result['status'], 'ready_with_pending_groups')
        self.assertTrue(result['required_actions'])
        result['required_actions'] = []
        result['candidate_ref'] = self.artifacts.put_json({key: value for key, value in result.items() if key != 'candidate_ref'})
        with self.assertRaisesRegex(ValueError, 'required source research actions'):
            self.validate(result, review, [], self.artifacts, utc_now())

    def test_cash_units_and_split_details_are_derived_from_source(self):
        text = ('<script>var strbzdm="123451";</script><table><tr><th>年份</th><th>权益登记日</th><th>除息日</th><th>每10份分红</th><th>分红发放日</th></tr>'
                '<tr><td>2025</td><td>2025-01-02</td><td>2025-01-02</td><td>每10份派现金1.2000元</td><td>2025-01-06</td></tr></table>'
                '<table><tr><th>年份</th><th>拆分折算日</th><th>拆分类型</th><th>拆分折算比例</th></tr>'
                '<tr><td>2025</td><td>2025-02-03</td><td>份额折算</td><td>1:2</td></tr></table>')
        parsed = funds.parse_actions(text, "123451")
        self.assertEqual(parsed["dividends"][0]["distribution_per_share"], 0.12)
        self.assertEqual(parsed["dividends"][0]["cash_payment_date"], "2025-01-06")
        self.assertEqual(parsed["splits"][0]["ratio_text"], "1:2")
        with self.assertRaisesRegex(ValueError, "Cash units"):
            funds.parse_actions(text.replace("每10份派现金", "每1份派现金"), "123451")

    def test_resealed_coverage_cannot_hide_unexamined_catalog_members(self):
        result = self.discover(thesis(), [], self.store, self.artifacts, "partial-counts", {"max_profile_requests": 1})
        self.assertTrue(result["coverage"]["unexamined_codes"])
        result["coverage"]["unexamined_codes"] = []
        result["candidate_ref"] = self.artifacts.put_json({key: value for key, value in result.items() if key != "candidate_ref"})
        with self.assertRaisesRegex(ValueError, "coverage counts"):
            self.validate(result, thesis(), [], self.artifacts, utc_now())



    def test_real_front_back_code_pattern_is_not_misreported_as_source_conflict(self):
        # Exact role pattern observed on real jbgk_000154.html; the surrounding
        # profile remains synthetic and the deferred-fee model is unsupported.
        entry = {"code": "000154", "name": "样例黄金A", "type": "指数型-其他"}
        text = profile("123451").replace("<td>123451</td>", "<td>100038（前端）、000154（后端）</td>")
        with self.assertRaises(funds.DeferredFeeIdentity) as caught:
            funds.parse_profile(text, "000154", entry)
        self.assertEqual(caught.exception.identity_status, "unsupported_deferred_fee_identity")
        self.assertEqual(caught.exception.identity_details["labelled_code_roles"], {"100038": "前端", "000154": "后端"})
        self.assertNotIn("conflict", str(caught.exception))

    @staticmethod
    def rejected_candidate_capture(url, source_id, **kwargs):
        value = capture(url, source_id, **kwargs)
        if source_id == "eastmoney_profile" and "123451" in url:
            text = value["text"].replace("<td>123451</td>", "<td>100038（前端）、123451（后端）</td>")
            raw = text.encode("utf8")
            value.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(),
                         text_sha256=hashlib.sha256(raw).hexdigest())
        return value

    def test_rejected_candidate_preserves_original_source_and_does_not_block_unrelated_holding(self):
        with patch.object(source_fetch, "fetch", side_effect=self.rejected_candidate_capture):
            result = self.discover(thesis(), ["654321"], self.store, self.artifacts, "rejected-candidate", {"max_profile_requests": 3})
        self.assertEqual(result["status"], "ready_with_pending_groups")
        self.assertEqual(result["codes"], ["654321"])
        self.assertEqual(result["coverage"]["rejected_profile_codes"], ["123451"])
        snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
        rejected = snapshot["rejected_profiles"]["123451"]
        self.assertEqual(rejected["failure"]["kind"], "unsupported_deferred_fee_identity")
        self.assertEqual(hashlib.sha256(self.artifacts.read(rejected["source"]["raw_ref"])).hexdigest(),
                         rejected["source"]["capture"]["raw_sha256"])
        self.validate(result, thesis(), ["654321"], self.artifacts, utc_now())
        archived_artifacts = Artifacts(self.store.base / "negative-evidence-archive")
        archived = funds.archive_identity(snapshot, self.artifacts, archived_artifacts)
        self.assertEqual(set(funds.verify_identity(archived, archived_artifacts)), {"123452", "654321"})

    def test_rejected_held_code_still_blocks_unknown_account_valuation(self):
        with patch.object(source_fetch, "fetch", side_effect=self.rejected_candidate_capture):
            result = self.discover(thesis(), ["123451"], self.store, self.artifacts, "rejected-held", {"max_profile_requests": 2})
        self.assertEqual(result["status"], "needs_research")
        self.assertIn("123451", result["critical_identity_codes"])
        self.assertNotIn("123451", result["completed_group_candidate_codes"])

    def test_rejected_source_failure_must_reproduce_and_cannot_hide_valid_profile(self):
        with patch.object(source_fetch, "fetch", side_effect=self.rejected_candidate_capture):
            snapshot = self.identities(["123451"])
        snapshot["rejected_profiles"]["123451"]["failure"]["message"] = "caller-created rejection"
        with self.assertRaisesRegex(ValueError, "failure differs"):
            funds.verify_identity(snapshot, self.artifacts)
        good = self.identities(["123452"])
        record = good["profiles"].pop("123452")
        good["identities"].pop("123452")
        good["rejected_profiles"]["123452"] = {"source": record, "failure": {"kind": "invented"}}
        with self.assertRaisesRegex(ValueError, "successfully parsed"):
            funds.verify_identity(good, self.artifacts)


    def test_continuation_preserves_rejected_evidence_without_spending_budget_on_it_again(self):
        with patch.object(source_fetch, "fetch", side_effect=self.rejected_candidate_capture):
            first = self.discover(thesis(), [], self.store, self.artifacts, "reject-first", {"max_profile_requests": 1})
        self.assertEqual(first["coverage"]["rejected_profile_codes"], ["123451"])
        def next_capture(url, source_id, **kwargs):
            self.assertNotIn("jbgk_123451", url)
            return capture(url, source_id, **kwargs)
        with patch.object(source_fetch, "fetch", side_effect=next_capture):
            second = self.discover(thesis(), [], self.store, self.artifacts, "reject-next",
                                   {"max_profile_requests": 1}, continue_from=first["candidate_ref"])
        self.assertEqual(second["attempted_codes"], ["123452"])
        self.assertEqual(second["coverage"]["rejected_profile_codes"], ["123451"])
        self.assertEqual(second["completed_group_candidate_codes"], [])
        self.validate(second, thesis(), [], self.artifacts, utc_now())


    def test_required_issuer_is_captured_before_failed_exploration_exhausts_bytes(self):
        mapping = {"code": "654321", "source_id": "issuer_cmfchina",
            "url": "https://www.cmfchina.com/web/fundDetail/654321/index.html",
            "role": "identity", "locators": ["html/block/0", "html/block/1"]}
        calls = []
        def network(url, source_id, **kwargs):
            calls.append((source_id, url, kwargs.get("max_bytes")))
            if source_id == "issuer_cmfchina":
                value = capture("https://fundf10.eastmoney.com/jbgk_654321.html", "eastmoney_profile")
                text = "<p>基金代码:654321</p><p>交易币种:人民币</p>"
                raw = text.encode("utf8")
                value.update(registry_source_id=source_id, requested_url=url, final_url=url, text=text, raw_bytes=raw,
                             bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
                return value
            if "jbgk_123451" in url:
                raise OSError("Synthetic exploration download has unknown partial byte consumption")
            return capture(url, source_id, **kwargs)
        with patch.object(source_fetch, "fetch", side_effect=network):
            result = self.discover(thesis(), ["654321"], self.store, self.artifacts, "reserve-first",
                {"max_profile_requests": 4, "max_total_bytes": 6000}, issuer_disclosures=[mapping])
        self.assertEqual(result["status"], "ready_with_pending_groups")
        self.assertEqual(result["codes"], ["654321"])
        self.assertEqual([row["phase"] for row in result["coverage"]["io_records"]],
                         ["directory", "required_identity", "required_issuer", "remaining_exploration"])
        self.assertEqual(result["coverage"]["source_byte_budget_charged"], 6000)
        failed = result["coverage"]["io_records"][-1]
        self.assertEqual(failed["charged_bytes"], failed["reserved_bytes"])
        self.assertEqual(failed["outcome"], "failed")
        self.assertLess(calls[2][2], 6000)
        self.validate(result, thesis(), ["654321"], self.artifacts, utc_now())

    def test_three_continuations_accumulate_frozen_directory_scope_without_partial_group_admission(self):
        first = self.discover(thesis(), [], self.store, self.artifacts, "cover-one", {"max_profile_requests": 1})
        self.assertEqual(first["completed_group_candidate_codes"], [])
        second = self.discover(thesis(), [], self.store, self.artifacts, "cover-two",
                               {"max_profile_requests": 1}, continue_from=first["candidate_ref"])
        third = self.discover(thesis(), [], self.store, self.artifacts, "cover-three",
                              {"max_profile_requests": 1}, continue_from=second["candidate_ref"])
        self.assertEqual(first["coverage"]["directory_content_hash"], third["coverage"]["directory_content_hash"])
        self.assertEqual(third["coverage"]["attempted_history"], ["123451", "123452", "123453"])
        self.assertEqual(third["coverage"]["unexamined_codes"], [])
        self.assertEqual(second["completed_group_candidate_codes"], ["123451", "123452"])
        self.assertEqual(third["completed_group_candidate_codes"], ["123451", "123452"])
        self.assertFalse(third["coverage"]["ordering_is_rank"])
        self.validate(third, thesis(), [], self.artifacts, utc_now())

    def test_complete_group_above_io_batch_has_no_financial_admission_cap(self):
        codes = [str(index).zfill(6) for index in range(1, 52)]
        entries = {code: {"name": "Synthetic 黄金 "+code, "type": "synthetic-equity"} for code in codes}
        identities = {code: {"code": code, "fund_group_id": "pool-"+code, "share_class": "A",
            "currency": "CNY", "dealing_currency": "CNY", "execution_venue": "off_exchange_nav",
            "instrument_type": "off_exchange_nav", "asset_class": "synthetic-equity", "type": "synthetic-equity",
            "tracking_target": "黄金", "benchmark_id": "synthetic-common-benchmark", "hedge_policy": "unhedged"}
            for code in codes}
        policy = {**self.policy(), "comparison_groups": ["same_benchmark"]}
        result = funds._derive_selection(entries, identities, [], [], funds._theses(thesis()), policy, funds._policy({}), self.plan())
        self.assertEqual(result["codes"], codes)
        self.assertEqual(result["completed_group_candidate_codes"], codes)
        reordered = funds._derive_selection(dict(reversed(list(entries.items()))), dict(reversed(list(identities.items()))),
            [], [], funds._theses(thesis()), policy, funds._policy({}), self.plan())
        self.assertEqual(reordered, result)

    def test_source_io_continuation_preserves_whole_51_product_domain(self):
        rows = [[str(index).zfill(6), "SYNTHETIC", "Synthetic 黄金 "+str(index)+"A", "指数型-其他", "SYNTHETIC"]
                for index in range(1, 52)]
        original_profile = profile
        with patch.dict(globals(), {"ROWS": rows}), patch.dict(globals(), {
                "profile": lambda code: original_profile(code).replace("中证股票指数", "黄金9999")}):
            first = self.discover(thesis(), [], self.store, self.artifacts, "io-first", {})
            self.assertEqual(len(first["attempted_codes"]), 50)
            self.assertEqual(first["codes"], [])
            second = self.discover(thesis(), [], self.store, self.artifacts, "io-second", {}, continue_from=first["candidate_ref"])
            self.assertEqual(second["codes"], [row[0] for row in rows])
            self.assertEqual(second["coverage"]["unexamined_codes"], [])
            self.validate(second, thesis(), [], self.artifacts, utc_now())

    def automatic_capture(self, url, source_id, **kwargs):
        if source_id != "issuer_cmfchina":
            result = capture(url, source_id, **kwargs)
            if source_id == "eastmoney_profile" and any("jbgk_"+code in url for code in ("123451", "123452")):
                text = result["text"]
                text = re.sub(r'<tr><th>交易币种</th>.*?</tr>', '', text)
                text = re.sub(r'<tr><th>份额类别</th>.*?</tr>', '', text)
                text += '<a href="http://www.cmfchina.com/web/fundDetail/123451/identity.html">原始发行人资料</a>'
            else:
                return result
        else:
            text = ('<p>基金份额分为以下全部份额类别</p><table><tr><th>基金全称</th><td>样例黄金基金</td></tr>'
                '<tr><th>基金代码</th><th>份额类别</th></tr><tr><td>123451</td><td>A</td></tr>'
                '<tr><td>123452</td><td>C</td></tr><tr><th>交易币种</th><td>人民币</td></tr>'
                '<tr><th>交易场所</th><td>场外</td></tr></table>')
            raw = text.encode("utf-8")
            result = {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
                "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
                "content_type": "text/html", "encoding": "utf-8", "transport": "HTTPS_default_certificate_validation", "retrieved_at": utc_now(),
                "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_bytes": raw, "text": text}
            return result
        raw = text.encode("utf-8")
        return {**result, "raw_bytes": raw, "text": text, "bytes": len(raw),
                "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest()}

    def test_actual_profile_href_automatically_supplies_source_bound_identity(self):
        with patch.object(source_fetch, "fetch", side_effect=self.automatic_capture):
            result = self.discover(thesis(), [], self.store, self.artifacts, "auto-source", {})
        snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
        self.assertEqual(result["completed_group_candidate_codes"], ["123451", "123452"])
        self.assertEqual(snapshot["identities"]["123451"]["currency"], "CNY")
        self.assertEqual(snapshot["identities"]["123452"]["share_class"], "C")
        self.assertTrue(all(row["href"].startswith("http://") and row["url"].startswith("https://") for row in snapshot["automatic_issuer_discovery"]))
        funds.verify_identity(snapshot, self.artifacts)
        tampered = copy.deepcopy(snapshot)
        tampered["automatic_issuer_discovery"][0]["href"] += "?invented=yes"
        with self.assertRaisesRegex(ValueError, "original profile link"):
            funds.verify_identity(tampered, self.artifacts)

    def test_mandatory_issuer_link_runs_before_exploration_budget(self):
        with patch.object(source_fetch, "fetch", side_effect=self.automatic_capture):
            result = self.discover(thesis(), ["123451"], self.store, self.artifacts, "auto-priority", {"max_source_requests": 3})
        snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
        self.assertEqual(snapshot["identities"]["123451"]["currency"], "CNY")
        self.assertEqual([row["phase"] for row in result["coverage"]["io_records"]],
                         ["directory", "required_identity", "automatic_issuer"])
        self.validate(result, thesis(), ["123451"], self.artifacts, utc_now())

    def announcement_capture(self, url, source_id, **kwargs):
        from source_archival_fixture import unicodePDF
        if source_id == "eastmoney_profile":
            result = self.automatic_capture(url, source_id, **kwargs)
            text = result["text"] + '<a href="jjgg_'+Path(url).stem.split('_')[1]+'.html">原公告目录</a>'
        elif source_id == "eastmoney_announcements_page":
            code = Path(url).stem.split('_')[1]
            text = ('<script src="//j5.dfcfw.com/sc/js/web/f10_min_20250219.js"></script>'
                '<script id="jjggtmp" type="text/html">{{if value.ATTACHTYPE}}'
                '<a href="http://pdf.dfcfw.com/pdf/H2_{{value.ID}}_1.pdf"></a></script>'
                '<script>var strbzdm="'+code+'";var params = { code: strbzdm, pindex: 1, pernum: 20, type: 0 };</script>')
        elif source_id == "eastmoney_announcements_script":
            text = 'var apiHost="//api.fund.eastmoney.com";var request=apiHost+"/f10/JJGG?callback=?&fundcode="+strcode+"&pageIndex="+pageindex+"&pageSize="+pers+"&type="+stype;'
        elif source_id == "eastmoney_announcements_api":
            code = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["fundcode"][0]
            text = json.dumps({"ErrCode": 0, "PageIndex": 1, "PageSize": 20, "Data": [{"FUNDCODE": code,
                "TITLE": "Synthetic 基金产品资料概要", "ATTACHTYPE": "0", "ID": "AN202610061234567890"}]})
        elif source_id == "issuer_eastmoney_pdf":
            raw = unicodePDF(["基金全称:样例黄金基金", "基金代码:123451", "交易币种:人民币", "交易场所:场外"])
            text = None
        else:
            return self.automatic_capture(url, source_id, **kwargs)
        if text is not None:
            raw = text.encode("utf-8")
        media = "application/pdf" if source_id == "issuer_eastmoney_pdf" else "application/json" if source_id == "eastmoney_announcements_api" else "application/javascript" if source_id == "eastmoney_announcements_script" else "text/html"
        return {"registry_source_id": source_id, "registry_hash": fingerprint(source_fetch.load_registry()),
                "requested_url": url, "final_url": url, "redirect_chain": [], "http_status": 200,
                "content_type": media, "encoding": None if text is None else "utf-8", "transport": "HTTPS_default_certificate_validation", "retrieved_at": utc_now(),
                "bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(), "text_sha256": hashlib.sha256(raw).hexdigest() if text is not None else None,
                "raw_bytes": raw, "text": text, "request_headers": {"Referer": source_fetch.source_rule(source_id)["referer"]}
                if source_fetch.source_rule(source_id).get("referer") else {}}

    def test_original_announcement_recipe_pdf_and_archive_reconstruct_sources(self):
        with patch.object(source_fetch, "fetch", side_effect=self.announcement_capture):
            result = self.discover(thesis(), ["123451"], self.store, self.artifacts, "notice-chain", {})
        snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
        self.assertTrue(snapshot["announcement_discovery"])
        self.assertTrue(any(row["source_id"] == "issuer_eastmoney_pdf" and row["document_ref"] for row in snapshot["automatic_issuer_discovery"]))
        self.validate(result, thesis(), ["123451"], self.artifacts, utc_now())
        destination = Artifacts(self.store.base / "notice-archive")
        copied = funds.archive_identity(snapshot, self.artifacts, destination)
        funds.verify_identity(copied, destination)

    def test_rejected_announcement_api_and_malformed_menu_are_source_scoped(self):
        for failure in ("api", "doctype"):
            def rejected(url, source_id, **kwargs):
                result = self.announcement_capture(url, source_id, **kwargs)
                if source_id == ("eastmoney_announcements_api" if failure == "api" else "eastmoney_announcements_page"):
                    text = json.dumps({"ErrCode": -999, "Data": [], "PageIndex": 0, "PageSize": 0}) if failure == "api" else '<![DOCTYPE html]>'
                    raw = text.encode("utf-8")
                    result.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
                return result
            with self.subTest(failure=failure), patch.object(source_fetch, "fetch", side_effect=rejected):
                result = self.discover(thesis(), ["123451"], self.store, self.artifacts, "notice-rejected-"+failure, {})
                snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
                self.assertTrue(all(row["failure"] for row in snapshot["announcement_discovery"]))
                self.assertTrue(any(row["action"] == "complete_original_announcement_discovery" for row in snapshot["required_actions"]))
                self.assertIn("123451", result["codes"])
                self.validate(result, thesis(), ["123451"], self.artifacts, utc_now())

    def test_automatic_issuer_wrong_subject_keeps_unknowns_and_original_task(self):
        def wrong_subject(url, source_id, **kwargs):
            result = self.automatic_capture(url, source_id, **kwargs)
            if source_id == "issuer_cmfchina":
                text = result["text"].replace("123451", "999991").replace("123452", "999992")
                raw = text.encode("utf-8")
                result.update(text=text, raw_bytes=raw, bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
            return result
        with patch.object(source_fetch, "fetch", side_effect=wrong_subject):
            result = self.discover(thesis(), [], self.store, self.artifacts, "auto-wrong", {})
        snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
        self.assertEqual(result["completed_group_candidate_codes"], [])
        self.assertIsNone(snapshot["identities"]["123451"]["currency"])
        self.assertTrue(any(row["action"] == "complete_discovered_issuer_source" for row in snapshot["required_actions"]))
        funds.verify_identity(snapshot, self.artifacts)


    def test_preexcluded_y_peer_keeps_complete_group_evidence_without_requiring_nav_or_fees(self):
        codes = ["111111", "222222", "333333"]
        identities = {}
        for code, share in zip(codes, ("A", "C", "Y")):
            identities[code] = {"code": code, "fund_group_id": "synthetic-single-pool", "share_class": share,
                "currency": "CNY", "dealing_currency": "CNY", "execution_venue": "off_exchange_nav",
                "instrument_type": "off_exchange_nav", "asset_class": "synthetic-equity", "type": "synthetic-equity",
                "tracking_target": "黄金", "investment_scope": "", "benchmark_id": None, "sector_exposures": None,
                "company_restrictions": None, "identity_scope": "explicit_synthetic_roster",
                "group_membership": {"kind": "same_legal_fund", "codes": codes,
                                     "scope": "complete_synthetic_roster", "evidence_refs": [{"synthetic": True}]}}
        entries = {code: {"name": "Synthetic 黄金 "+code, "type": "synthetic-equity"} for code in codes}
        result = funds._derive_selection(entries, identities, [], [], funds._theses(thesis()), self.policy(),
                                        funds._policy({}), self.plan())
        self.assertEqual(result["codes"], ["111111", "222222"])
        self.assertEqual(result["completed_group_candidate_codes"], ["111111", "222222"])
        self.assertEqual(result["groups"][0]["members"], codes)
        self.assertIn("333333", result["comparison_identities"])
        self.assertEqual(result["prequalification"]["333333"]["reasons"],
                         ["pension_share_investor_eligibility_not_established"])

    def test_large_directory_pending_lists_are_shared_not_repeated_per_group(self):
        entries = {str(index).zfill(6): {"name": "Synthetic 黄金", "type": "synthetic"} for index in range(1, 3001)}
        identities = {code: {"code": code, "fund_group_id": "pool-"+code, "share_class": "A",
            "currency": "CNY", "dealing_currency": "CNY", "execution_venue": "off_exchange_nav",
            "instrument_type": "off_exchange_nav", "asset_class": "synthetic", "type": "synthetic",
            "tracking_target": "黄金", "benchmark_id": None} for code in list(entries)[:200]}
        result = funds._derive_selection(entries, identities, [], [], funds._theses(thesis()), self.policy(),
                                        funds._policy({}), self.plan())
        self.assertEqual(len(result["groups"]), 200)
        self.assertEqual(len(result["pending_scopes"]), 1)
        self.assertEqual(len(next(iter(result["pending_scopes"].values()))), 2800)
        self.assertEqual({row["pending_code_count"] for row in result["groups"]}, {2800})
        from contracts import canonical_bytes
        self.assertLess(len(canonical_bytes(result)), 1024*1024)


    def test_free_form_prohibited_or_optional_mandate_is_only_an_exposure_lead(self):
        for mandate in ("本基金不投资黄金。", "本基金可投资股票、债券、黄金及多种行业，但尚未披露实际持仓。"):
            identity = {"code": "123451", "tracking_target": None, "asset_class": "混合型",
                        "investment_scope": mandate, "sector_exposures": None}
            matches, actions = funds._qualified_exposures(identity, funds._theses(thesis()))
            self.assertEqual(matches, [])
            self.assertEqual(actions[0]["action"], "verify_actual_product_exposure")
            self.assertEqual(actions[0]["disclosed_mandate"], mandate)

    def test_explicit_tracking_target_establishes_relation_without_inventing_weight(self):
        identity = {"code": "123451", "tracking_target": "上海黄金交易所黄金价格", "asset_class": "商品型",
                    "investment_scope": "可投资其他金融工具。", "sector_exposures": None}
        matches, actions = funds._qualified_exposures(identity, funds._theses(thesis()))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["supporting_fields"][0]["scope"], "explicit_source_tracking_target")
        self.assertIsNone(matches[0]["weight"])
        self.assertEqual(actions, [])

    def test_reported_sector_weight_has_date_and_is_not_treated_as_live_holdings(self):
        identity = {"code": "123451", "tracking_target": None, "asset_class": "混合型",
                    "investment_scope": "", "sector_exposures": {"status": "measured_disclosure",
                        "weights": {"黄金行业": "0.25", "其他": "0.50"}, "disclosed_as_of": "2026-06-30",
                        "evidence_refs": [{"fixture": "dated source table"}]}}
        matches, actions = funds._qualified_exposures(identity, funds._theses(thesis()))
        self.assertEqual(matches[0]["weight"], "0.25")
        support = matches[0]["supporting_fields"][0]
        self.assertEqual(support["disclosed_as_of"], "2026-06-30")
        self.assertEqual(support["scope"], "last_disclosed_weight_not_live_holdings")
        self.assertEqual(actions, [])

    def test_asset_category_does_not_establish_an_unrelated_industry(self):
        identity = {"code": "123451", "tracking_target": None, "asset_class": "商品型-黄金",
                    "investment_scope": "", "sector_exposures": None}
        asset_matches, _ = funds._qualified_exposures(identity, funds._theses(thesis()))
        self.assertEqual(asset_matches[0]["supporting_fields"][0]["scope"], "source_disclosed_asset_category")
        rows = funds._theses(thesis())
        rows[0]["kind"] = "industry"
        industry_matches, _ = funds._qualified_exposures(identity, rows)
        self.assertEqual(industry_matches, [])


    @staticmethod
    def conflicting_issuer_capture(url, source_id, **kwargs):
        if source_id != "issuer_cmfchina":
            return capture(url, source_id, **kwargs)
        value = capture("https://fundf10.eastmoney.com/jbgk_123451.html", "eastmoney_profile")
        text = "<p>基金代码:123451</p><p>基金全称:另一法律主体基金</p>"
        raw = text.encode("utf8")
        value.update(registry_source_id=source_id, requested_url=url, final_url=url, text=text, raw_bytes=raw,
                     bytes=len(raw), raw_sha256=hashlib.sha256(raw).hexdigest(), text_sha256=hashlib.sha256(raw).hexdigest())
        return value

    @staticmethod
    def conflicting_issuer_mapping(role="identity"):
        return {"code": "123451", "source_id": "issuer_cmfchina", "url": "https://www.cmfchina.com/web/fundDetail/123451/index.html",
                "role": role, "locators": ["html/block/0", "html/block/1"]}

    def test_rejected_issuer_cannot_be_covered_by_base_profile_and_retains_raw_evidence(self):
        with patch.object(source_fetch, "fetch", side_effect=self.conflicting_issuer_capture):
            result = self.discover(thesis(), ["654321"], self.store, self.artifacts, "issuer-conflict",
                {"max_profile_requests": 4}, issuer_disclosures=[self.conflicting_issuer_mapping()])
        self.assertEqual(result["status"], "ready_with_pending_groups")
        self.assertEqual(result["codes"], ["654321"])
        self.assertNotIn("123451", result["completed_group_candidate_codes"])
        snapshot = self.artifacts.read_json(result["identity_snapshot_ref"])
        self.assertEqual(len(snapshot["rejected_issuer_documents"]), 1)
        rejected = snapshot["rejected_issuer_documents"][0]
        self.assertEqual(rejected["failure"]["exception"], "IssuerIdentityMismatch")
        self.assertTrue(snapshot["identities"]["123451"]["source_evidence_gaps"][0]["critical_identity"])
        self.assertTrue(self.artifacts.read_json(rejected["document_ref"])["raw_ref"])
        self.validate(result, thesis(), ["654321"], self.artifacts, utc_now())
        archived_artifacts = Artifacts(self.store.base / "issuer-negative-archive")
        archived = funds.archive_identity(snapshot, self.artifacts, archived_artifacts)
        funds.verify_identity(archived, archived_artifacts)

    def test_held_identity_conflict_is_critical_even_when_document_was_called_manager_evidence(self):
        with patch.object(source_fetch, "fetch", side_effect=self.conflicting_issuer_capture):
            result = self.discover(thesis(), ["123451"], self.store, self.artifacts, "issuer-held-conflict",
                {"max_profile_requests": 4}, issuer_disclosures=[self.conflicting_issuer_mapping("manager")])
        self.assertEqual(result["status"], "needs_research")
        self.assertIn("123451", result["critical_identity_codes"])

    def test_resealed_issuer_failure_cannot_disappear_or_change_reason(self):
        with patch.object(source_fetch, "fetch", side_effect=self.conflicting_issuer_capture):
            catalog = funds.collect_catalog(self.store, self.artifacts, "bad-issuer-catalog")
            ref = funds.resolve_identity(["123451"], catalog, self.artifacts, store=self.store,
                                         issuer_disclosures=[self.conflicting_issuer_mapping()])
        snapshot = self.artifacts.read_json(ref)
        original = copy.deepcopy(snapshot)
        snapshot["rejected_issuer_documents"] = []
        with self.assertRaisesRegex(ValueError, "Every issuer request"):
            funds.verify_identity(snapshot, self.artifacts)
        original["rejected_issuer_documents"][0]["failure"]["message"] = "fake source refusal"
        with self.assertRaisesRegex(ValueError, "preserved reason"):
            funds.verify_identity(original, self.artifacts)

if __name__ == "__main__":
    unittest.main()
