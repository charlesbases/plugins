"""Independent intraday accounting truths; no investment qualification."""
import unittest
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"skills/investment/scripts"))

import portfolio_mpc as mpc
from test_single_step import make_context
from test_portfolio_mpc import with_second, path, STAGES, EMPTY


class IntradayEntitlementTests(unittest.TestCase):
    def test_existing_cash_same_day_purchase_respects_record_date_choice(self):
        for subscription, dividend, wealth in (("included", "10", 100.), ("excluded", "0", 90.)):
            with self.subTest(subscription=subscription):
                context = make_context(cash="100")
                context["decision_at"] = "2030-01-01T08:00:00+08:00"
                event = {"code":"C", "id":"analytical-record", "record_date":"2030-01-02",
                    "ex_date":"2030-01-03", "pay_date":"2030-01-05", "per_share":.1,
                    "distribution_mode":"cash", "currency":"CNY",
                    "entitlement_rule":{"subscribe_on_record_date":subscription,
                                        "redeem_on_record_date":"included"}}
                scenario = path(["C"],lambda _,day: .9 if day>="2030-01-03" else 1.,[event])
                result = mpc.simulate(context,scenario,EMPTY,
                    {"kind":"allocate_settled_cash","weights":{"C":"1"}},STAGES)
                purchases = [row for row in result["cash_flow_log"] if row["kind"]=="price_buy"]
                self.assertEqual(len(purchases),1)
                self.assertEqual(purchases[0]["date"],"2030-01-02")
                self.assertEqual(Decimal(purchases[0]["nav"]),Decimal("1"))
                self.assertEqual(Decimal(purchases[0]["shares"]),Decimal("100"))
                rights = [row for row in result["cash_flow_log"] if row["kind"]=="distribution_receivable"]
                credits = [row for row in result["cash_flow_log"] if row["kind"]=="credit_distribution"]
                self.assertEqual(len(rights),1)
                self.assertEqual(len(credits),1)
                self.assertEqual(rights[0]["date"],"2030-01-03")
                self.assertEqual(credits[0]["date"],"2030-01-05")
                self.assertEqual(Decimal(rights[0]["amount"]),Decimal(dividend))
                self.assertEqual(Decimal(credits[0]["amount"]),Decimal(dividend))
                # 100 shares*.9 plus the source-defined record-date right.
                self.assertAlmostEqual(result["terminal_wealth"],wealth)
                self.assertFalse(result["future_rule_is_order"])
                mpc.verify_cash_flow(result)

    def test_unpublished_closing_NAV_cannot_change_earlier_purchase_amount(self):
        requests=[]
        for closing in (2.,5.):
            context=with_second(make_context(cash="100",shares="100"))
            context["decision_at"]="2030-01-01T08:00:00+08:00"
            context["spec"]["availability"]={"nav_lag_calendar_days":1}
            next(row for row in context["model_request"]["assets"] if row["code"]=="D")["max_weight"]=.25
            scenario=path(["C","D"],lambda code,day: (closing if day>="2030-01-02" else 1.) if code=="C" else 1.)
            result=mpc.simulate(context,scenario,EMPTY,
                {"kind":"allocate_settled_cash","weights":{"D":"1"}},STAGES)
            buys=[row for row in result["cash_flow_log"] if row["kind"]=="reserve_buy"]
            self.assertEqual(len(buys),1)
            self.assertEqual(buys[0]["date"],"2030-01-02")
            requests.append(Decimal(buys[0]["request"]))
        # At 08:00 the last known C NAV1 gives equity200 and a D25% cap50;
        # neither the unobserved C2 close(cap75) nor C5 close(cap100) is usable.
        self.assertEqual(requests,[Decimal("50"),Decimal("50")])

    def test_date_only_redemption_credit_cannot_fund_an_eight_am_purchase(self):
        context = with_second(make_context(cash="0",shares="100"))
        context["decision_at"] = "2030-01-01T08:00:00+08:00"
        context["snapshot"]["prices"]["D"] = context["market_ref"]["prices"]["D"] = "2"
        scenario = path(["C","D"],lambda code,day: 1. if code=="C" else (4. if day>="2030-01-05" else 2.))
        action = {"buys":[],"sells":[{"lot_id":"old","shares":"100"}]}
        stages = ["2030-01-01","2030-01-02","2030-01-04","2030-01-05","2030-01-09"]
        result = mpc.simulate(context,scenario,action,
            {"kind":"allocate_settled_cash","weights":{"D":"1"}},stages)
        # Source contract: Jan1 pricing, Jan2 confirmation, two calendar-day
        # lag gives Jan4 credit without a known hour. At 23:59:59 it can fund
        # a request priced on the next declared open day, Jan5.
        credits = [row for row in result["cash_flow_log"] if row["kind"]=="credit_sale"]
        reservations = [row for row in result["cash_flow_log"] if row["kind"]=="reserve_buy"]
        purchases = [row for row in result["cash_flow_log"] if row["kind"]=="price_buy"]
        self.assertEqual(len(credits),1)
        self.assertEqual(credits[0]["date"],"2030-01-04")
        self.assertEqual(Decimal(credits[0]["amount"]),Decimal("100"))
        self.assertEqual(len(reservations),1)
        self.assertEqual(reservations[0]["date"],"2030-01-04")
        self.assertEqual(len(purchases),1)
        self.assertEqual(purchases[0]["date"],"2030-01-05")
        self.assertEqual(Decimal(purchases[0]["nav"]),Decimal("4"))
        self.assertEqual(Decimal(purchases[0]["shares"]),Decimal("25"))
        self.assertAlmostEqual(result["terminal_wealth"],100.)
        self.assertFalse(result["future_rule_is_order"])
        mpc.verify_cash_flow(result)


if __name__ == "__main__":
    unittest.main()
