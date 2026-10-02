"""Independent source arithmetic and user-facing financial report contracts."""
import re
from decimal import Decimal

import cash_reference as reference
from contracts import require


def _same_money(actual, expected, label):
    require(Decimal(str(actual)) == Decimal(str(expected)), "Report " + label + " differs from independent source arithmetic")


def reference_valuation_invariants(context, orders, value):
    """Verify source-normal estimates independently of the MPC producer."""
    require(type(value) is dict and value.get("schema_id") == "source_reference_cash_valuation_v1"
            and value.get("basis") == "current_known_NAV_reference_not_future_dealing_NAV",
            "Explicit source reference valuation required")
    require(value["submitted_at"] == context["decision_at"], "Report submission clock changed")
    expected = {(o["side"], o["code"], o.get("lot_id")): o for o in orders}
    legs = {(r["side"], r["code"], r.get("lot_id")): r for r in value["legs"]}
    require(len(expected) == len(orders) and len(legs) == len(value["legs"])
            and set(expected) == set(legs), "Report valuation legs differ from compiled current orders")
    lots = {r["lot_id"]: r for r in context["snapshot"].get("positions", [])}
    products = {}
    cash_reserved = Decimal(0)
    for key, order in expected.items():
        leg = legs[key]
        code = order["code"]
        terms = context["fee_contracts"][code]
        nav = Decimal(str(context["market_ref"]["prices"][code]))
        _same_money(leg["reference_nav"], nav, "reference NAV")
        require(leg["reference_nav_date"] == context["market_ref"]["price_dates"][code], "Report NAV date changed")
        priced = reference.pricing_day(terms, context["decision_at"])
        confirmed = reference.confirmation_day(terms, priced)
        require(leg["pricing_date"] == str(priced)
                and leg["normal_confirmation_date"] == str(confirmed)
                and leg["confirmation_basis"] == "source_normal_estimate_not_actual",
                "Report normal pricing/confirmation clock or scope differs")
        if order["side"] == "buy":
            request = Decimal(order["cash_limit"])
            cash_reserved += request
            quote = reference.entry_quote(request, terms, nav)
            _same_money(leg["cash_debit"], request, "subscription reservation")
            _same_money(leg["shares"], quote["shares"], "reference subscription shares")
            for field, quoted in (("gross", "gross"), ("contractual_fee", "fee")):
                _same_money(leg[field], quote[quoted], field)
            _same_money(leg.get("share_surplus", "0"), quote.get("share_surplus", "0"), "fund share surplus")
        else:
            require(order["lot_id"] in lots, "Report redemption lot lacks the confirmed snapshot")
            quantity = Decimal(order["share_limit"])
            lot = lots[order["lot_id"]]
            _same_money(leg["shares"], quantity, "redemption shares")
            quote = reference.exit_details(quantity * nav, terms, acquired_at=lot["acquired_at"],
                                           submitted_at=context["decision_at"], confirmed=confirmed)
            require(leg["holding_end_event"] == terms["holding"]["end_event"], "Report holding-age meaning changed")
            require(leg["normal_cash_date"] == str(reference.payment_day(terms, confirmed)), "Report cash availability clock changed")
            for field in ("gross", "contractual_fee", "rounding_loss", "net"):
                _same_money(leg[field], quote[field], field)
            item = products.setdefault(code, {"shares": Decimal(0), "lot_allocations": [],
                "gross": Decimal(0), "contractual_fee": Decimal(0), "rounding_loss": Decimal(0), "net": Decimal(0)})
            item["shares"] += quantity
            item["lot_allocations"].append({"lot_id": order["lot_id"], "shares": order["share_limit"]})
            for field in ("gross", "contractual_fee", "rounding_loss", "net"):
                item[field] += quote[field]
    require(cash_reserved <= Decimal(str(context["snapshot"]["available_cash"])), "Report buys spend unreceived or hypothetical cash")
    totals = {r["code"]: r for r in value["total_products"]}
    require(len(totals) == len(value["total_products"]) and set(totals) == set(products), "Report product totals differ")
    for code, expected_total in products.items():
        total = totals[code]
        terms = context["fee_contracts"][code]
        physical = sum((Decimal(str(row["shares"])) for row in lots.values() if row["code"] == code), Decimal(0))
        quantity = expected_total["shares"]
        require(0 < quantity <= physical, "Report redemption exceeds confirmed product shares")
        if not (quantity == physical and terms.get("full_redemption_allowed") is True):
            minimum, remaining = terms.get("minimum_redemption_shares"), terms.get("minimum_remaining_shares")
            require(minimum is not None and remaining is not None, "Report partial redemption source thresholds are unknown")
            require(quantity >= Decimal(str(minimum)) and (quantity == physical or physical-quantity >= Decimal(str(remaining))),
                    "Report product redemption violates source aggregate minimum or residual")
        _same_money(total["redemption_shares"], expected_total["shares"], "product redemption shares")
        require(sorted(total["lot_allocations"], key=lambda r: r["lot_id"])
                == sorted(expected_total["lot_allocations"], key=lambda r: r["lot_id"]), "Report product lot allocations changed")
        for field in ("gross", "contractual_fee", "rounding_loss", "net"):
            _same_money(total[field], expected_total[field], "aggregate " + field)
    return {"status": "passed", "scope": "independent_reference_NAV_source_normal_clock_fee_and_request_arithmetic"}


def public_request_invariants(orders):
    """Fund requests are distinct from internal per-lot accounting legs."""
    totals = {}
    for order in orders["orders"]:
        if order["side"] != "sell":
            continue
        row = totals.setdefault(order["code"], {"shares": Decimal(0), "lot_allocations": [], "ledger_order_ids": []})
        row["shares"] += Decimal(order["share_limit"])
        row["lot_allocations"].append({"lot_id": order["lot_id"], "shares": order["share_limit"]})
        row["ledger_order_ids"].append(order["order_id"])
    requests = {r["code"]: r for r in orders.get("redemption_requests", [])}
    require(len(requests) == len(orders.get("redemption_requests", [])) and set(requests) == set(totals),
            "Public redemption requests must cover each sold fund exactly once")
    for code, expected in totals.items():
        row = requests[code]
        _same_money(row["shares"], expected["shares"], "public redemption total")
        require(sorted(row["lot_allocations"], key=lambda r: r["lot_id"])
                == sorted(expected["lot_allocations"], key=lambda r: r["lot_id"])
                and sorted(row["ledger_order_ids"]) == sorted(expected["ledger_order_ids"]),
                "Public redemption request does not map its internal accounting legs")
    return requests


def _industry_target_semantics(bundle, text):
    """Check prediction meaning from the sealed source-domain metadata."""
    forecasts = bundle.get("comparison_summary", {}).get("industry_diagnostics", {}).get("current_forecasts", [])
    def readable(value):
        return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")
    for forecast in forecasts:
        prefix = "- 行业 " + readable(forecast["sector_id"]) + "："
        lines = [line for line in text.splitlines() if line.startswith(prefix)]
        require(len(lines) == 1, "Rendered source prediction target is missing or duplicated")
        line = lines[0]
        domain = forecast.get("asset_domain") or {}
        target = domain.get("return_target") or {}
        transform = target.get("transform")
        complete = all(type(target.get(key)) is str and target[key].strip()
                       for key in ("source_unit", "canonical_unit", "economic_meaning"))
        if not complete or transform not in ("price_log_return", "level_change"):
            require("预测目标变换或单位待核" in line and "经济含义尚未核实" in line
                    and "对数价格比预测" not in line and "水平变化预测" not in line,
                    "Rendered unknown prediction transform/unit must remain explicitly unverified")
            continue
        expected_label = ("两项对数价格比预测（无量纲）" if transform == "price_log_return" else
                          "两项水平变化预测（" + readable(target["canonical_unit"]) + "）")
        required = ["目标变换 " + transform, expected_label,
                    "原始观测单位 " + readable(target["source_unit"]),
                    "规范观测单位 " + readable(target["canonical_unit"]),
                    "经济含义 " + readable(target["economic_meaning"])]
        require(all(value in line for value in required), "Rendered prediction transform/unit differs from source target")


def report_semantics(bundle, text):
    """Check independently required content before renderer equality checking."""
    orders = bundle["orders"]
    summary = bundle.get("comparison_summary", {})
    selected = summary.get("selected")
    if selected:
        point = selected["point_valuation"]
        required = {"fees", "path_execution_cost", "terminal_exit_assumption_cost",
                    "expected_policy_fees", "worst_plan_fee_reserve"}
        require(required <= selected.keys() and required-{"expected_policy_fees", "worst_plan_fee_reserve"} <= point.keys()
                and {"cash_flow_log", "terminal_exit_assumptions"} <= point.keys(),
                "Report model cost components are absent")
        for field in ("fees", "path_execution_cost", "terminal_exit_assumption_cost"):
            _same_money(selected[field], point[field], "point model cost "+field)
        path_cost = sum((Decimal(str(row["fee"])) for row in point["cash_flow_log"]
                         if row["kind"] in {"price_buy", "price_sell"}), Decimal(0))
        terminal_cost = sum((Decimal(row["contractual_fee"])+Decimal(row["rounding_loss"])
                             for row in point["terminal_exit_assumptions"]), Decimal(0))
        for field, amount in (("path_execution_cost", path_cost), ("terminal_exit_assumption_cost", terminal_cost),
                              ("fees", path_cost+terminal_cost)):
            _same_money(point[field], float(amount), "point model cost composition "+field)
        require(Decimal(str(selected["worst_plan_fee_reserve"])) >= Decimal(str(selected["expected_policy_fees"])) >= 0,
                "Report expected costs exceed worst plan reserve")
        for label, field in (("点估计总模型费用", "fees"), ("路径成交成本", "path_execution_cost"),
                             ("终点退出假设成本", "terminal_exit_assumption_cost"),
                             ("情景期望总模型费用", "expected_policy_fees"),
                             ("最坏情景完整计划费用预留", "worst_plan_fee_reserve")):
            require(f"{label} {selected[field]:.2f}" in text, "Rendered model cost meaning or amount is omitted: "+field)
        require("终点退出是估值假设，不是订单或台账费用" in text
                and "净财富已计入该退出净额，不再次扣减" in text
                and "实际近期窗口费用加最坏情景完整计划费用预留" in text,
                "Rendered terminal cost or fee reserve scope is omitted")
    if summary.get("decision_status") == "risk_recovery":
        selected = summary.get("selected") or {}
        require(selected.get("eligible") is False and selected.get("recovery_eligible") is True
                and orders.get("selected_plan_kind") == "risk_recovery"
                and orders.get("risk_status") == "risk_not_restored",
                "Report risk recovery must not claim ordinary financial qualification")
        require("风险预算尚未恢复（risk_not_restored）" in text
                and "普通资格 eligible=false" in text and "within_verified_budget" not in text,
                "Rendered risk recovery must expose un-restored risk without a passed budget claim")
    elif summary.get("decision_status") == "feasible_selected":
        require((summary.get("selected") or {}).get("eligible") is True
                and orders.get("risk_status") == "within_verified_budget",
                "Report ordinary financial qualification differs from sealed decision")
        require("在已验证范围内满足风险预算（within_verified_budget）" in text,
                "Rendered ordinary decision must state its verified risk scope")
    requests = public_request_invariants(orders)
    expected = []
    for code, row in requests.items():
        expected.append((code, "卖出", Decimal(row["shares"]), "份"))
    for order in orders["orders"]:
        if order["side"] == "buy":
            expected.append((order["code"], "买入", Decimal(order["cash_limit"]), order["currency"]))
    pattern = r"^- .*?（([^）]+)）：(买入|卖出) ([0-9]+(?:\.[0-9]+)?) ([^\s。]+)。$"
    actual = [(m[0], m[1], Decimal(m[2]), m[3]) for m in re.findall(pattern, text, flags=re.MULTILINE)]
    require(sorted(actual) == sorted(expected), "Rendered public fund requests differ from product-level current requests")
    _industry_target_semantics(bundle, text)
    for claim in bundle.get("news", {}).get("claims", []):
        for evidence in claim.get("counterevidence", []):
            required = evidence.get("missing_reason") or evidence.get("text")
            if required:
                require(" ".join(required.split()) in text, "Rendered news counterevidence or missing reason is omitted")
    if orders["orders"]:
        value = orders.get("reference_valuation")
        require(type(value) is dict, "Current report needs its source-normal valuation envelope")
        require("正常处理估算" in text and "真实成交" in text, "Report estimate/actual valuation scope is missing")
        for row in value["total_products"]:
            require("参考应收款 " + str(row["net"]) + " 元" in text, "Rendered redemption net differs from authoritative envelope")
    return {"status": "passed", "scope": "independent_public_request_counterevidence_and_valuation_scope_semantics"}
