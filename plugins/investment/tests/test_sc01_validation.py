"""封闭格式及真实业务规则的独立反例。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from investment.chains.account.validation import expected_derived, verify_merge, verify_trades
from investment.contracts import ContractError, Schemas, canonical, known, parse
from investment.references import Resolver, safe_path
from tests import RealFlowCase, harness


class ValidationTests(RealFlowCase):
    def test_strict_contracts(self):
        schemas = Schemas()
        self.assertEqual(parse(b'{"a":"1.00"}'), {"a": "1.00"})
        for bad in (b'{"a":1,"a":2}', b'{"a":1.0}', b'{"a":NaN}', b'{"a":-0}', b'{"a":9007199254740992}', b'{ "a":1}', b'{"a":1}\n', b'\xef\xbb\xbf{}', b'{"a":"\\ud800"}'):
            with self.subTest(raw=bad), self.assertRaises(ContractError):
                parse(bad)
        self.assertEqual(canonical({"b": "\u2028", "a": "中"}), '{"a":"中","b":"\u2028"}'.encode())
        for value in ({"kind": "date", "value": "2026-02-30", "timezone": "Asia/Shanghai"}, {"kind": "instant", "value": "2026-02-30T00:00:00Z"}):
            with self.assertRaises(ContractError):
                schemas.validate_type("RawTime", value)
        with self.assertRaises(ContractError):
            schemas.validate("investment.trade", {"trade_id": "incomplete"})
        trade = harness.make_trade("buy", "100", "200")
        for key, value in (("settlements", []), ("confirmation", []), ("amount", known(200))):
            bad = dict(trade, **{key: value})
            with self.subTest(field=key), self.assertRaises(ContractError):
                schemas.validate("investment.trade", bad)
        bad = copy.deepcopy(trade); bad["holding_start"] = harness.n()
        with self.assertRaises(ContractError):
            schemas.validate("investment.trade", bad)
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            resolver = Resolver(Path(directory), schemas)
            for uri in ("https://example.com/input.json", "investment-run://sc01/20261015/inv/../input.json", "fixture://first/input.json", "investment-data://sc01/account/old/context"):
                with self.subTest(uri=uri), self.assertRaises(ContractError):
                    resolver.read(uri)
            with self.assertRaises(ContractError):
                safe_path(Path(directory), "..", "outside.json")
        record = self.first()
        process = self.validate_output(record)
        self.assertEqual(process.returncode, 0, process.stderr)
        wrong = self.candidate_counterexample(record, "wrong-format", candidate_changes={"unexpected": True})
        process = self.validate_output(record, wrong)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stderr)["rule_id"], "J02")

    def test_business_rules(self):
        buy, sale = harness.make_trade("buy", "1000", "2000"), harness.make_trade("sell", "200", "385")
        moment = harness.clock()
        source = {"confirmed_at": moment, "transaction_as_of": moment, "opening": {"start_at": "2026-01-01T00:00:00Z"}}
        trades = sorted([buy, sale], key=lambda t: t["trade_id"])
        verify_trades(trades, source)
        objects, _ = expected_derived(trades)
        self.assertEqual(objects["positions"]["rows"][0]["confirmed_shares"], known("800"))
        self.assertEqual(objects["pending_transactions"]["rows"][0]["pending_phase"], "settlement")
        paid = copy.deepcopy(sale); paid["settlement"] = {"state": "settled", "basis": "user_reported", "actual_at": harness.u()}
        verify_trades([buy, paid], source)
        self.assertEqual(expected_derived([buy, paid])[0]["positions"]["rows"][0]["confirmed_shares"], known("800"))
        self.assertEqual(expected_derived([buy, paid])[0]["pending_transactions"]["rows"], [])
        wrong = copy.deepcopy(buy); wrong["amount"] = known("1999")
        baseline = {"meta": None, "trades": {"rows": [buy]}}
        with self.assertRaises(ContractError) as error:
            verify_merge({"changes": {"rows": [harness.mutate(wrong, buy, "update")]}}, baseline, harness.clock())
        self.assertEqual(error.exception.rule, "I06")
        corrected = verify_merge({"changes": {"rows": [harness.mutate(wrong, buy, "correct")]}}, baseline, harness.clock())
        self.assertEqual(corrected[0]["amount"], known("1999"))
        unknown_buy = copy.deepcopy(buy); unknown_buy["confirmed_shares"] = harness.u()
        self.assertEqual(expected_derived([unknown_buy])[0]["positions"]["rows"][0]["confirmed_shares"]["status"], "unknown")
        bad = copy.deepcopy(buy); bad["confirmed_shares"] = known("0")
        with self.assertRaises(ContractError):
            verify_trades([bad], source)
        bad = copy.deepcopy(buy); bad["confirmed_shares"] = harness.n()
        with self.assertRaises(ContractError) as failure:
            verify_trades([bad], source)
        self.assertEqual(failure.exception.rule, "A05")
        cancelled = harness.make_trade("buy", None, "50", True)
        cancelled["confirmation"].update(state="cancelled", basis="user_reported")
        moment = harness.clock()
        source.update(confirmed_at=moment, transaction_as_of=moment)
        verify_trades([cancelled], source)
        self.assertEqual(expected_derived([cancelled])[0]["positions"]["rows"], [])
        for invalid_time in ("2025-12-31T00:00:00Z", "2099-01-01T00:00:00Z"):
            invalid = copy.deepcopy(buy); invalid["trade_at"] = harness.raw(invalid_time)
            with self.assertRaises(ContractError) as failure:
                verify_trades([invalid], source)
            self.assertEqual(failure.exception.rule, "A08")
        bad = copy.deepcopy(buy); bad["expected_at"] = harness.raw("2026-01-01T00:00:00Z")
        with self.assertRaises(ContractError) as failure:
            verify_trades([bad], source)
        self.assertEqual(failure.exception.rule, "A08")
        self.first()
        self.execute([harness.mutate(harness.make_trade("buy", "500", "1000"))], "confirmed-buy")
        sale = harness.make_trade("sell", "200", "398"); sale["expected_at"] = harness.raw(harness.offset(86400))
        record = self.execute([harness.mutate(sale)], "confirmed-sale-awaiting-cash")
        self.assertEqual(record["state"]["business_objects"]["positions"]["rows"][0]["confirmed_shares"], known("300"))
        process = self.validate_output(record)
        self.assertEqual(process.returncode, 0, process.stderr)
        checks = json.loads(process.stdout)["checks"]
        self.assertEqual(len(checks), 28)
        self.assertTrue({f"I{n:02}" for n in range(1, 8)} <= {c["rule_id"] for c in checks})
        positions = copy.deepcopy(record["state"]["business_objects"]["positions"])
        positions["rows"][0]["confirmed_shares"] = known("301")
        wrong = self.candidate_counterexample(record, "wrong-position", tables={"positions": positions})
        process = self.validate_output(record, wrong)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stderr)["rule_id"], "A10")
        trades = copy.deepcopy(record["state"]["business_objects"]["trades"])
        next(t for t in trades["rows"] if t["side"] == "buy")["confirmed_shares"] = known("501")
        wrong = self.candidate_counterexample(record, "unsupported-share", tables={"trades": trades})
        process = self.validate_output(record, wrong)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stderr)["rule_id"], "A14")
