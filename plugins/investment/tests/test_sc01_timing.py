"""整笔时序：所有落账场景从正式CLI与真实既有持仓开始。"""
import copy
import json
from datetime import datetime, timedelta, timezone

from investment.contracts import known
from tests import RealFlowCase, harness


class TimingTests(RealFlowCase):
    def trade(self, tid):
        return next(t for t in self.ledger()["trades"]["rows"] if t["trade_id"] == tid)

    def test_buy_and_sell_expected_times(self):
        self.first()
        self.execute([harness.mutate(harness.make_trade("buy", "1000", "2000"))], "holding")
        buy = harness.make_trade("buy", None, "100", True); buy["expected_at"] = harness.raw(harness.offset(86400))
        sale = harness.make_trade("sell", "100", "190"); sale["expected_at"] = harness.raw(harness.offset(172800))
        self.execute([harness.mutate(buy), harness.mutate(sale)], "two-independent-trades")
        before = self.ledger()
        prepared = self.execute([], "questions", prepare=True)["result"]
        self.assertEqual({q["trade_id"] for q in prepared["questions"]}, {buy["trade_id"], sale["trade_id"]})
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.trade(buy["trade_id"])["confirmation"]["state"], "pending")
        self.assertEqual(self.trade(sale["trade_id"])["settlement"]["state"], "pending")

    def test_reply_priority_and_postponement(self):
        self.first()
        buy = harness.make_trade("buy", None, "100", True); buy["expected_at"] = harness.raw(harness.offset(5))
        self.execute([harness.mutate(buy)], "submitted-awaiting")
        harness.wait_until(buy["expected_at"]["value"]["value"])
        self.execute([], "past-but-denied", replies=[harness.reply(buy, "not_completed")])
        old = self.trade(buy["trade_id"])
        self.assertEqual(old["confirmation"]["state"], "pending")
        self.assertEqual(old["expected_at"]["status"], "unknown")
        self.execute([], "no-reply", replies=[harness.reply(buy, "no_reply")])
        self.assertEqual(self.trade(buy["trade_id"]), old)
        future = harness.raw(harness.offset(86400))
        self.execute([], "postpone", replies=[harness.reply(buy, "not_completed", future)])
        self.assertEqual(self.trade(buy["trade_id"])["expected_at"], future)
        result = self.execute([], "invalid-postpone", False, replies=[harness.reply(buy, "not_completed", harness.raw(harness.offset(-1)))])
        for failure_path in result["folder"].glob("attempt-*/failure.json"):
            failure = json.loads(failure_path.read_bytes())
            self.assertEqual(failure["rule_id"], "A13")
            self.assertEqual(failure["object_ref"]["value"]["schema_id"], "investment.source_record")
            self.assertTrue(failure["json_pointer"]["value"].startswith("/arrival_replies/rows/0"))

    def test_due_posting_and_unknowns(self):
        self.first()
        self.execute([harness.mutate(harness.make_trade("buy", "1000", "2000"))], "holding")
        sale = harness.make_trade("sell", "100", None, True)
        day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        sale["expected_at"] = known({"kind": "date", "value": day, "timezone": "Asia/Shanghai"})
        self.execute([harness.mutate(sale)], "whole-date-due")
        actual = self.trade(sale["trade_id"])
        self.assertEqual(actual["confirmation"]["state"], "confirmed")
        self.assertEqual(actual["settlement"]["state"], "settled")
        for field in ("amount", "confirmed_shares"):
            self.assertEqual(actual[field]["status"], "unknown")
        self.assertEqual(actual["requested_shares"], known("100"))
        self.assertEqual(actual["confirmation"]["basis"], "expected_due")
        self.assertEqual(actual["settlement"]["actual_at"]["status"], "unknown")
        self.execute([], "deny-provisional", replies=[harness.reply(sale, "not_completed")])
        restored = self.trade(sale["trade_id"])
        self.assertEqual(restored["confirmation"]["state"], "pending")
        self.assertEqual(restored["settlement"]["state"], "pending")
        self.assertEqual(restored["requested_shares"], known("100"))

    def test_completion_dedup_and_reconciliation(self):
        self.first()
        self.execute([harness.mutate(harness.make_trade("buy", "1000", "2000"))], "holding")
        sale = harness.make_trade("sell", "200", "385"); sale["expected_at"] = harness.raw(harness.offset(5))
        self.execute([harness.mutate(sale)], "sale-awaiting")
        harness.wait_until(sale["expected_at"]["value"]["value"])
        self.execute([], "sale-due")
        old = self.trade(sale["trade_id"]); revised = copy.deepcopy(old); revised["fee_amount"] = known("1")
        self.execute([harness.mutate(revised, old, "update")], "fee-only")
        actual = self.trade(sale["trade_id"])
        self.assertEqual(actual["settlement"]["basis"], "expected_due")
        record = self.execute([], "denial", replies=[harness.reply(sale, "not_completed")])
        actual = self.trade(sale["trade_id"])
        self.assertEqual(actual["confirmation"]["basis"], "user_reported")
        self.assertEqual(actual["confirmed_shares"], known("200"))
        self.assertEqual(actual["settlement"]["state"], "pending")
        self.assertEqual(record["state"]["business_objects"]["positions"]["rows"][0]["confirmed_shares"], known("800"))
        completed_reply = harness.reply(sale, "completed")
        record = self.execute([], "actual-arrival", replies=[completed_reply])
        generation = self.ledger()["meta"]["generation"]
        self.execute([], "repeat-same-notice", replies=[completed_reply])
        self.assertEqual(self.ledger()["meta"]["generation"], generation)
        self.assertEqual(record["state"]["business_objects"]["positions"]["rows"][0]["confirmed_shares"], known("800"))

    def test_cutoff_is_frozen(self):
        self.first()
        buy = harness.make_trade("buy", None, "100", True); buy["expected_at"] = harness.raw(harness.offset(300))
        record = self.execute([harness.mutate(buy)], "future-decision")
        context = record["state"]["context"]
        self.assertEqual(self.trade(buy["trade_id"])["confirmation"]["state"], "pending")
        self.assertLess(context["execution_cutoff_at"], record["input"]["decision_at"])
        generation, fact_time = context["generation"], context["known_as_of"]
        refreshed = self.execute([], "automatic-refresh")
        self.assertEqual(refreshed["state"]["context"]["known_as_of"], fact_time)
        self.assertEqual(refreshed["state"]["context"]["generation"], generation)
        path, manifest = self.manifest()
        inv = next(i for i in manifest["invocations"] if i["invocation_id"] == record["input"]["invocation_id"])
        self.assertEqual(inv["created_at"], context["execution_cutoff_at"])

    def test_validator_rejects_wrong_due_state(self):
        self.first()
        buy = harness.make_trade("buy", None, "100", True); buy["expected_at"] = harness.raw(harness.offset(5))
        self.execute([harness.mutate(buy)], "awaiting")
        harness.wait_until(buy["expected_at"]["value"]["value"])
        record = self.execute([], "due")
        trades = copy.deepcopy(record["state"]["business_objects"]["trades"])
        trades["rows"][0]["confirmed_shares"] = known("100")
        wrong = self.candidate_counterexample(record, "unsupported-due-share", tables={"trades": trades})
        process = self.validate_output(record, wrong)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stderr)["rule_id"], "A14")
