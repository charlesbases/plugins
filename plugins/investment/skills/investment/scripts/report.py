"""Canonical report; financial actions only come from the sealed decision."""
from decimal import Decimal, ROUND_HALF_UP
import json
import copy
from zoneinfo import ZoneInfo
from contracts import instant


def _text(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def _beijing_time(value):
    """Convert precise instants for display without inventing a date-only hour."""
    if isinstance(value, dict):
        if value.get("precision") == "timestamp":
            return instant(value["value"]).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()+"（北京时间）"
        if value.get("precision") == "date":
            return _text(value["value"])+"（原文仅日期，来源时区："+_text(value["timezone"])+"）"
    if isinstance(value, str) and "T" in value:
        try:
            return instant(value).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()+"（北京时间）"
        except ValueError:
            pass
    return _text(value)


def _industry_summary(calculation):
    industry = calculation.get("industry_forecast", {})
    if not industry:
        return {}
    day = industry.get("decision_date")
    latest = []
    for forecast in industry.get("forecasts", []):
        if forecast["decision_date"] != day:
            continue
        witness, audit, model = forecast["feature_source"], forecast["training_audit"], forecast["model"]
        latest.append({"sector_id": forecast["sector_id"], "decision_date": day,
            "predicted_coordinates": forecast["predicted_coordinates"],
            "pricing_phase_definition": forecast["pricing_phase_definition"],
            "asset_domain": copy.deepcopy(forecast.get("asset_domain")),
            "model": model, "training_rows": audit["training_rows"],
            "max_label_available_at": audit["max_label_available_at"],
            "training_ceiling_at": audit.get("training_ceiling_at"),
            "training_data_hash": audit["training_data_hash"],
            "feature_names": witness["feature_names"], "feature_values": witness["x"],
            "benchmark_base": witness["benchmark_base"], "coverage": witness["coverage"],
            "event_evidence_refs": [reference for event in witness["events"] for reference in event["evidence_refs"]],
            "source_hash": forecast["source_hash"]})
    keys = ("status", "decision_date", "horizon_days", "dataset_hash", "news_ref", "event_frontier_hash",
            "forecast_hash", "training_ceiling_at", "return_role", "reason", "required_actions")
    return {**{key: industry[key] for key in keys if key in industry}, "current_forecasts": latest,
            "current_unavailable": [row for row in industry.get("unavailable", []) if row["decision_date"] == day],
            "scope": "source_factual_news_predictive_benchmark_covariates_not_causal_or_future_profit_evidence"}


def summarize(calculation, context):
    mpc = calculation.get("mpc", {})
    policy = mpc.get("selected_policy")
    selected = None
    if policy is not None:
        selected = {key: policy[key] for key in ("id", "expected_net_return", "cvar_loss_fraction", "eligible",
            "recovery_eligible", "risk_violation_amount", "trade_guard") if key in policy}
        selected.update(fees=policy["point"]["fees"], expected_profit=policy["expected_profit"],
                        path_execution_cost=policy["point"]["path_execution_cost"],
                        terminal_exit_assumption_cost=policy["point"]["terminal_exit_assumption_cost"],
                        expected_policy_fees=policy["expected_policy_fees"],
                        worst_plan_fee_reserve=max(row["fees"] for row in policy["selection"]),
                        conditional_lower_bound=policy.get("conditional_lower_bound"),
                        current_projection=policy["current_projection"],
                        point_valuation=policy["point"])
    return {"horizon_days": context["spec"]["planning"]["primary_horizon_days"],
        "selected": selected, "selection_pending_reason": None if policy else mpc.get("reason", calculation.get("reason")),
        "selection_scope": mpc.get("selection_scope", mpc.get("scope")),
        "selected_candidate_id": calculation.get("orders", {}).get("selected_candidate_id"),
        "selected_plan_kind": calculation.get("orders", {}).get("selected_plan_kind"),
        "decision_status": mpc.get("decision_status"),
        "current_action_guard": policy.get("trade_guard", {}) if policy else {},
        "product_cost_comparison": calculation.get("product_cost_comparison", {}),
        "trade_calibration": copy.deepcopy(mpc.get("calibration", {})),
        "source_action_diagnostics": {key: copy.deepcopy(calculation.get("comparison", {}).get(key))
            for key in ("status", "scope", "menu_rule", "action_family_hash", "funding_options", "required_actions")},
        "frozen_policy_count": len(mpc.get("frozen_policy_family", {}).get("policies", [])),
        "industry_diagnostics": _industry_summary(calculation),
        "predictive_increment": copy.deepcopy(calculation.get("predictive_increment", {})),
        "model_required_actions": calculation.get("required_actions", []),
        "model_status": calculation.get("status"), "model_reason": calculation.get("reason"),
        "forecasts": calculation.get("fitting", {}).get("forecasts", []),
        "future_valuation_rule": policy.get("future_rule") if policy else None,
        "future_valuations_are_orders": False,
        "risk_scope": mpc.get("risk_scope"),
        "scope": "qualified_path_cash_fee_risk_estimates_not_promised_returns"}


def summarize_discovery(discovery, artifacts):
    if not discovery:
        return {}
    identity = artifacts.read_json(discovery["identity_snapshot_ref"])
    keys = ("name", "company_name", "manager_names", "manager_tenures", "type", "identity_scope",
            "currency", "fund_group_id", "sector_exposures", "benchmark")
    coverage = {key + "_count" if isinstance(value, list) else key: len(value) if isinstance(value, list) else value
                for key, value in discovery["coverage"].items()}
    return {"observed_at": discovery["observed_at"], "coverage": coverage,
            "required_actions": discovery["required_actions"],
            "candidates": [{"code": code, **{key: identity["identities"][code].get(key) for key in keys}}
                           for code in discovery["codes"]]}


def explain_actions(bundle, artifacts):
    """Derive explanations from sealed inputs; never invent a causal news link."""
    benchmarks = []
    if bundle.get("industry_record_ref"):
        sector_record = artifacts.read_json(bundle["industry_record_ref"])
        sector_data = artifacts.read_json(sector_record["data_ref"])
        keys = ("sector_id", "benchmark_id", "index_code", "index_name", "benchmark_role",
                "sector_relation_scope", "sector_relation")
        benchmarks = [{key: row.get(key) for key in keys} for row in sector_data["sectors"]]
    annotations = {}
    for name in ("quality", "exposure", "readiness"):
        ref = bundle.get(name+"_ref")
        if ref:
            annotations[name] = artifacts.read_json(ref)
    if bundle.get("family_results_ref"):
        annotations["families"] = artifacts.read_json(bundle["family_results_ref"])
    context = bundle.get("context")
    if not context:
        return {"actions": [], "peer_comparisons": [], "industry_benchmarks": benchmarks, "source_analyses": annotations,
                "scope": "awaiting_verified_calculation"}
    state = artifacts.read_json(bundle["account_state_ref"])
    calculation = artifacts.read_json(bundle["calculation_ref"])
    selection = artifacts.read_json(bundle["selection_ref"]) if bundle.get("selection_ref") else {}
    selection_id = bundle["orders"].get("selected_candidate_id")
    selection_kind = bundle["orders"].get("selected_plan_kind")
    chosen = calculation.get("mpc", {}).get("selected_policy") or {}
    guard = chosen.get("trade_guard", {})
    forecasts = {row["code"]: row for row in calculation.get("fitting", {}).get("forecasts", [])}
    positions = {}
    for lot in state["lots"].values():
        positions[lot["code"]] = positions.get(lot["code"], Decimal(0)) + Decimal(lot["shares"])
    cash = Decimal(context["snapshot"]["available_cash"])
    equity = Decimal(context["snapshot"]["equity"])
    valuation = bundle["orders"].get("reference_valuation")
    if bundle["orders"]["orders"]:
        from report_validation import reference_valuation_invariants, public_request_invariants
        reference_valuation_invariants(context, bundle["orders"]["orders"], valuation)
        public_request_invariants(bundle["orders"])
    legs = {(row["side"], row["code"], row.get("lot_id")): row for row in (valuation or {}).get("legs", [])}
    actions = []
    for order in bundle["orders"]["orders"]:
        code, side = order["code"], order["side"]
        price = Decimal(context["market_ref"]["prices"][code])
        reference = legs[(side, code, order.get("lot_id"))]
        before = positions.get(code, Decimal(0))
        if side == "buy":
            debit, fee = Decimal(order["cash_limit"]), Decimal(reference["contractual_fee"])
            after = before + Decimal(reference["shares"])
            cash -= debit
            funding = {"settled_cash": str(debit), "unreceived_sale_proceeds": "0", "unconfirmed_contribution": "0"}
        else:
            quantity = Decimal(order["share_limit"])
            after, debit = before - quantity, Decimal(0)
            fee = Decimal(reference["contractual_fee"])
            funding = {"source_lot": order["lot_id"], "estimated_receivable": reference["net"],
                       "estimated_rounding_loss": reference["rounding_loss"],
                       "estimated_execution_cost": str(Decimal(reference["contractual_fee"])+Decimal(reference["rounding_loss"])),
                       "available_after_confirmed_settlement_only": True}
        positions[code] = after
        selected_evidence = selection.get("per_code", {}).get(code, {})
        reason = {"risk_recovery": "风险恢复：没有普通合格方案，按已登记的经验风险超额优先规则选择纯减仓；风险预算尚未恢复（risk_not_restored），这不是通过普通风险资格的盈利方案。",
                  "assisted_override": "本次采用已封存的人工调整，金额仍通过同一费用、资金及风险约束。"}.get(
                      selection_kind, "在已声明候选方案中比较扣费后的财富与风险约束，金额取自实际选中的组合方案。")
        if chosen:
            reason += (f" 该组合情景均值净利润 {float(chosen['expected_profit']):.2f} 元；"
                       f"情景CVaR金额 {float(chosen['absolute_cvar']):.2f} 元。")
            if guard.get("net_advantage") is not None:
                reason += f" 相对同资金维持方案的点预测净优势 {float(guard['net_advantage']):.2f} 元。"
            if chosen.get("conditional_lower_bound") is not None:
                reason += f" 扣原样本外乐观误差缓冲后下界 {float(chosen['conditional_lower_bound']):.2f} 元。"
            reason += " 这是整体方案比较，单笔金额按来源精度、现金及组合约束选择，不把整体收益分摊成单笔因果收益。"
        actions.append({"order_id": order["order_id"], "code": code, "side": side,
            "before_shares": str(before), "after_shares_at_reference_nav": str(after),
            "before_weight_on_starting_equity": str(before * price / equity) if equity else None,
            "after_weight_on_starting_equity": str(after * price / equity) if equity else None,
            "reference_nav": str(price), "nav_date": context["market_ref"]["price_dates"][code],
            "estimated_fee": str(fee), "funding": funding, "remaining_settled_cash": str(cash),
            "selection_evidence": selected_evidence, "forecast": forecasts.get(code),
            "selected_plan_id": selection_id, "selected_plan_kind": selection_kind, "reason": reason,
            "reference_valuation": reference, "scope": "reference_NAV_projection_source_normal_clock_not_a_confirmed_fill"})
    return {"actions": actions, "peer_comparisons": selection.get("peer_comparisons", []),
            "industry_benchmarks": benchmarks,
            "per_code": selection.get("per_code", {}),
            "source_analyses": annotations,
            "reference_valuation": valuation,
            "selection_scope": calculation.get("mpc", {}).get("selection_scope"),
            "scope": "source_linked_selection_and_exact_reference_price_cash_arithmetic"}


def render(bundle):
    lines = ["新闻要点", "", f"分析时间：{_beijing_time(bundle['decision_at'])}。",
             f"账户记录时间：{_beijing_time(bundle['account_recorded_at']) if bundle['account_recorded_at'] else '尚无确认记录'}；各基金净值归属日：{_text(bundle['market_price_dates'] or '尚未取得')}。"]
    news = bundle.get("news", {})
    if news.get("information_cutoff_at"):
        lines.append(f"检索发布边界：{_beijing_time(news['cutoff_at'])}；实际信息封存时间：{_beijing_time(news['information_cutoff_at'])}。")
    claims = news.get("claims", [])
    if claims:
        for item in claims:
            lines.append(f"- {_text(item.get('kind', 'inference'))}：{_text(item['text'])}")
            if item.get("url"):
                lines.append(f"  原文：{item['url']}")
            lines.append(f"  原文发布时间：{_beijing_time(item.get('published_at', '未核实'))}；事件发生时间：{_beijing_time(item.get('event_at') or '未单独核实')}。")
            lines.append(f"  品类：{_text(', '.join(item.get('categories', [])))}；方向：{_text(item.get('direction', 'watch'))}。")
            for counter in item.get("counterevidence", []):
                if counter.get("missing_reason"):
                    lines.append("  反证待核：" + _text(counter["missing_reason"]))
                else:
                    lines.append("  反证：" + _text(counter["text"]))
                    source = counter.get("support", {}).get("source_url")
                    if source:
                        lines.append("  反证原文：" + _text(source))
    else:
        lines.append("本次需要基于已取得原文完成引用核验与影响分析。")
    coverage = news.get("source_results", [])
    for source in coverage:
        lines.append(f"- 来源 {_text(source['source_id'])}：{_text(source['state'])}；范围 {_text(source['coverage_claim'])}。")
        if source.get("discovery_pending_urls"):
            lines.append(f"  已发现、尚待采集链接 {len(source['discovery_pending_urls'])} 个；当前正文核验不代表该来源全天完整。")
    for benchmark in bundle["action_analysis"].get("industry_benchmarks", []):
        lines.append(f"- 行业研究标记 {_text(benchmark['sector_id'])} 的声明基准 {_text(benchmark['index_name'])}"
                     f"（{_text(benchmark['index_code'])}）；角色 {_text(benchmark['benchmark_role'])}；"
                     f"原文行业关系范围 {_text(benchmark['sector_relation_scope'])}。")
        if benchmark["benchmark_role"] == "analyst_proxy":
            lines.append("  该代理仅保存原始行情，未进入实际行业输入，也不支持该行业涨跌结论。")
        relation = benchmark.get("sector_relation") or {}
        if relation.get("document_ref"):
            lines.append(f"  行业定义原文 {_text(relation['document_ref'])}；结构位置 {_text(relation.get('locator'))}；"
                         f"证据状态 {_text(relation.get('status'))}。")
    summary = bundle.get("comparison_summary", {})
    increment = summary.get("predictive_increment", {})
    if increment:
        lines.append("行业/新闻协变量块的配对预测误差诊断：" + _text(increment["status"]) +
                     "；对照为相同原点与成熟标签的原生净值基准，投资净收益另以真实账本评价。")
        if increment.get("reason"):
            lines.append("  诊断资料范围：" + _text(increment["reason"]) + "。")
    industry = summary.get("industry_diagnostics", {})
    if industry:
        lines.append(f"行业模型来源状态：{_text(industry['status'])}；评价窗口 {_text(industry.get('horizon_days'))} 日；"
                     f"新闻绑定 {_text(industry.get('news_ref'))}；行业数据摘要 {_text(industry.get('dataset_hash'))}。")
        lines.append("行业 EN 输出只作基金 EN 的协变量；行业基准回报不另加进基金现金流。系数表达预测关联，因果与未来收益均未验证。")
        for forecast in industry["current_forecasts"]:
            model, base = forecast["model"], forecast["benchmark_base"]
            domain = forecast.get("asset_domain") or {}
            target = domain.get("return_target") or {}
            transform = target.get("transform")
            complete = all(type(target.get(key)) is str and target[key].strip()
                           for key in ("source_unit", "canonical_unit", "economic_meaning"))
            coordinates = _text(forecast["predicted_coordinates"])
            if complete and transform in ("price_log_return", "level_change"):
                label = ("对数价格比预测（无量纲）" if transform == "price_log_return" else
                         f"水平变化预测（{_text(target['canonical_unit'])}）")
                target_text = (f"目标变换 {_text(transform)}；两项{label} {coordinates}；"
                               f"原始观测单位 {_text(target['source_unit'])}；"
                               f"规范观测单位 {_text(target['canonical_unit'])}；"
                               f"经济含义 {_text(target['economic_meaning'])}")
            else:
                target_text = f"预测目标变换或单位待核；两项坐标 {coordinates}（经济含义尚未核实）"
            lines.append(f"- 行业 {_text(forecast['sector_id'])}：定价阶段定义 {_text(forecast['pricing_phase_definition'])}；"
                         f"{target_text}；基准参考日 {_text(base['date'])}，"
                         f"可得时间 {_text(base['available_at'])}，原始引用 {_text(base['raw_ref'])}。")
            lines.append(f"  已核事实特征 {_text(dict(zip(forecast['feature_names'], forecast['feature_values'])))}；"
                         f"观察覆盖 {_text(forecast['coverage'])}；引用 {_text(forecast['event_evidence_refs'])}。")
            lines.append(f"  EN 参数 alpha={_text(model['alpha'])}、l1_ratio={_text(model['l1_ratio'])}；"
                         f"标准化特征系数 {_text(model['coefficients'])}；成熟训练行 {_text(forecast['training_rows'])}，"
                         f"最晚标签可得时间 {_text(forecast['max_label_available_at'])}，校准前训练边界 {_text(forecast['training_ceiling_at'])}。")
        for gap in industry.get("current_unavailable", []):
            lines.append("- 行业来源缺口：" + _text(gap))
        for action in industry.get("required_actions", []):
            lines.append("- 行业补核：" + _text(action))
    for action in summary.get("model_required_actions", []):
        lines.append("- 模型补核：" + _text(action))
    lines += ["", "持仓变化", "", f"账户：{bundle['account_id']}；状态：{bundle['status']}。"]
    discovery = bundle.get("selection_summary", {})
    if discovery:
        lines.append(f"基金目录及档案取得时间：{_text(discovery['observed_at'])}；发现覆盖：{_text(discovery['coverage'])}。")
        for candidate in discovery["candidates"]:
            lines.append(f"- 比较候选 {_text(candidate['name'])}（{candidate['code']}）：{_text(candidate['type'])}；"
                         f"管理公司 {_text(candidate['company_name'])}；当前经理 {_text(candidate['manager_names'])}；"
                         f"任期资料 {_text(candidate['manager_tenures'] or '需结合任职公告核对')}。")
        for action in discovery["required_actions"]:
            lines.append("- 产品补核：" + _text(action))
    risk = (bundle.get("context") or {}).get("risk_state")
    if risk:
        lines.append(f"净投入本金 {risk['net_principal']} CNY；本金亏损容忍率 {float(risk['loss_tolerance']):.2%}；"
                     f"本金下限目标 {risk['principal_floor']} CNY；剩余金额风险预算 {risk['remaining_loss_budget']} CNY。")
        lines.append(f"风险设置下次时间边界：{risk['valid_until'] or '无已登记变更时间'}；金额预算为情景约束。")
    if bundle.get("missing"):
        lines.append("需要补充：" + "、".join(_text(item) for item in bundle["missing"]) + "。")
    for action in (bundle.get("account_review") or {}).get("required_actions", []):
        lines.append("- 账户补核：" + _text(action))
    for gap in bundle.get("term_gaps", []):
        lines.append(f"- {gap['code']}交易条款待核：{_text(gap['required_actions'])}；未把缺失数值填成零，不能据此认定其他份额成本更优。")
    orders = bundle["orders"]
    action_rows = {row["order_id"]: row for row in bundle["action_analysis"]["actions"]}
    if orders.get("orders"):
        lines.append("以下金额属于条件研究方案；投资有效性资格不由计算通过自动取得。")
        lines.append("正常处理估算采用当前已知净值及来源定价、确认、到账规则；真实成交净值、费用和到账以回执为准。")
        valuation = orders["reference_valuation"]
        totals = {row["code"]: row for row in valuation["total_products"]}
        for request in orders["redemption_requests"]:
            code = request["code"]
            name = bundle.get("product_names", {}).get(code, "名称待核实")
            total = totals[code]
            lines.append(f"- {_text(name)}（{code}）：卖出 {request['shares']} 份。")
            lines.append("  一笔基金赎回请求；以下批次为来源核算分配，不是需要分别提交的卖单。")
            lines.append(f"  正常处理参考费用 {total['contractual_fee']} 元；取整损耗 {total['rounding_loss']} 元；参考应收款 {total['net']} 元，确认到账后方可再次分配。")
            for allocation in request["lot_allocations"]:
                detail = next(row for row in bundle["action_analysis"]["actions"]
                              if row["code"] == code and row["funding"].get("source_lot") == allocation["lot_id"])
                ref = detail["reference_valuation"]
                lines.append(f"  批次 {_text(allocation['lot_id'])}：分配 {allocation['shares']} 份；定价日 {ref['pricing_date']}，正常确认日 {ref['normal_confirmation_date']}，正常到账日 {ref['normal_cash_date']}；参考费用 {ref['contractual_fee']} 元。")
                lines.append("  依据：" + _text(detail["reason"]))
        for order in (row for row in orders["orders"] if row["side"] == "buy"):
            amount = order["cash_limit"] if order["side"] == "buy" else order["share_limit"]
            unit = order["currency"] if order["side"] == "buy" else "份"
            side = {"buy": "买入", "sell": "卖出"}[order["side"]]
            name = bundle.get("product_names", {}).get(order["code"], "名称待核实")
            lines.append(f"- {_text(name)}（{order['code']}）：{side} {amount} {unit}。")
            detail = action_rows[order["order_id"]]
            before = detail["before_weight_on_starting_equity"]
            after = detail["after_weight_on_starting_equity"]
            lines.append(f"  持有份额 {_text(detail['before_shares'])} → {_text(detail['after_shares_at_reference_nav'])}；"
                         f"相对调整前权益的权重 {float(before):.2%} → {float(after):.2%}；参考费用 {_text(detail['estimated_fee'])} 元。")
            money = ("使用已到账现金 " + detail["funding"]["settled_cash"] + " 元" if order["side"] == "buy" else
                     "参考应收款 " + detail["funding"]["estimated_receivable"] + " 元，确认到账后方可再次分配")
            lines.append(f"  资金：{money}；剩余已到账现金 {_text(detail['remaining_settled_cash'])} 元。")
            lines.append(f"  依据：{_text(detail['reason'])}")
    else:
        lines.append("本次没有可执行的新增交易指令；已确认持仓保持台账记录，未确认订单继续占用相应资金或份额。")
    for waiting in orders.get("waiting", []):
        if waiting.get("valuation_only_future_rule", {}).get("conditional_sells"):
            lines.append("- 未来赎回与再投资仅用于情景估值；后续取得实际确认及当日新信息后重新分析，没有预定卖出日或未来订单。")
        else:
            lines.append("- 待满足条件：" + _text(waiting))
    for option in orders.get("funding_options", []):
        if option["proposed_contribution"] <= 0:
            continue
        best = option.get("best")
        if best and best.get("eligible") is True and best.get("point") is not None:
            detail = (f"模型预期净收益率 {best['expected_net_return']:.2%}，情景CVaR {best['cvar_loss_fraction']:.2%}，"
                      f"点估计总模型费用 {best['point']['fees']:.2f}，路径成交成本 {best['point']['path_execution_cost']:.2f}，"
                      f"终点退出假设成本 {best['point']['terminal_exit_assumption_cost']:.2f}，"
                      f"情景期望总模型费用 {best['expected_policy_fees']:.2f}，"
                      f"最坏情景完整计划费用预留 {max(row['fees'] for row in best['selection']):.2f}，当前动作 {_text(best['current_action'])}")
        elif best and best.get("recovery_eligible") is True:
            detail = ("风险恢复备选：风险预算尚未恢复（risk_not_restored）；经验风险超额 "
                      f"{float(best['risk_violation_amount']):.2f} 元，当前动作 {_text(best['current_action'])}")
        elif best:
            detail = "候选尚未取得可比较的路径策略资格，金额待核：" + _text(best.get("trade_guard"))
        else:
            detail = "尚无已验证可选方案；可能需要补齐来源或风险证据"
        lines.append(f"- 追加资金备选：{option['proposed_contribution']:.2f}；{detail}；尚未计入可用余额，到账后重新计算。")
    summary = bundle.get("comparison_summary", {})
    for code, evaluation in bundle["action_analysis"].get("per_code", {}).items():
        result = "符合本次候选资格" if evaluation["eligible"] else "本次不增加"
        lines.append(f"- 产品比较 {code}：{result}；理由 {_text(evaluation['reasons'] or '已核身份、比较组、币种、渠道及合同')}；"
                     f"关联行业判断 {_text(evaluation['thesis_ids'])}；待核资料 {_text(evaluation.get('unknown_fields', []))}。")
    for comparison in bundle["action_analysis"].get("peer_comparisons", []):
        lines.append(f"- 同类比较 {_text(comparison['group_id'])}：已核成员 {_text(comparison['members'])}，"
                     f"可进入资金比较的成员 {_text(comparison['qualified_members'])}；范围 {_text(comparison['coverage_scope'])}。")
    costs = summary.get("product_cost_comparison", {})
    if costs.get("products"):
        lines += ["", f"同额申赎成本参考：每支按 {costs['reference_amount']} 元、当前净值比较；这些金额不是订单，也不是各期限收益预测。", "",
                  "| 产品 | 计费持有日 | 申购费 | 赎回费 | 参考净财富 |", "|---|---:|---:|---:|---:|"]
        for product in costs["products"]:
            for horizon in product.get("horizons", []):
                if "reference_net_wealth_lower" in horizon:
                    lines.append(f"| {product['code']} | {horizon['holding_days']} | {float(horizon['entry_fee']):.2f} | {float(horizon['exit_cost']):.2f} | {float(horizon['reference_net_wealth_lower']):.2f} |")
                else:
                    lines.append(f"| {product['code']} | {horizon['holding_days']} | — | — | {_text(horizon['reason'])} |")
            if product.get("reason"):
                lines.append(f"- {product['code']}：该参考金额不适用，{_text(product['reason'])}。")
        lines.append("计费持有年龄由各产品源合同的费档及最低持有期边界自动生成，按源起止规则计费；这些年龄不设定卖出日，费用参考不承诺到账时间。")
    if summary.get("selection_pending_reason"):
        lines.append("候选金额待核：" + _text(summary["selection_pending_reason"]) + "；尚未完成逐份额估值。")
    selected = summary.get("selected")
    if selected:
        lines.append(f"在 {summary['horizon_days']} 天主期限的共同终点，当前候选方案情景均值净收益率 {selected['expected_net_return']:.2%}，"
                     f"情景CVaR {selected['cvar_loss_fraction']:.2%}，点估计总模型费用 {selected['fees']:.2f}。")
        lines.append(f"点估计费用组成：路径成交成本 {selected['path_execution_cost']:.2f} 元；"
                     f"终点退出假设成本 {selected['terminal_exit_assumption_cost']:.2f} 元。"
                     f"情景期望总模型费用 {selected['expected_policy_fees']:.2f} 元；"
                     f"最坏情景完整计划费用预留 {selected['worst_plan_fee_reserve']:.2f} 元。")
        lines.append("终点退出是估值假设，不是订单或台账费用；净财富已计入该退出净额，不再次扣减。"
                     "费用门槛使用实际近期窗口费用加最坏情景完整计划费用预留。")
        guard = summary.get("current_action_guard", selected.get("trade_guard", {}))
        if guard:
            lines.append("当前交易门槛：" + _text(guard.get("status")) + "；" + _text(guard.get("reasons", [])) + "。")
        if summary.get("decision_status") == "risk_recovery":
            lines.append("风险恢复：风险预算尚未恢复（risk_not_restored）；普通资格 eligible=false。"
                         f"经验情景风险超额 {float(selected['risk_violation_amount']):.2f} 元；"
                         "本次按已登记政策优先减少经验风险超额，不构成未来风险概率保证。")
        elif summary.get("decision_status") == "feasible_selected":
            lines.append("普通可行方案：在已验证范围内满足风险预算（within_verified_budget）；证据依据 "
                         + _text(guard.get("risk_evidence_basis")) + "。")
        if selected.get("conditional_lower_bound") is not None:
            lines.append(f"相对同资金持有的点预测净优势扣除原样本外乐观误差缓冲后为 {float(selected['conditional_lower_bound']):.2f} 元；"
                         "这是条件模型误差下界，与排序所用情景均值分列，不是未来盈利置信区间。")
    calibration = summary.get("trade_calibration", {})
    if calibration.get("status"):
        lines.append("原样本外误差校准：" + _text(calibration["status"]) + "；" + _text(calibration.get("reason")) + "。")
        if calibration.get("panel_split"):
            split = calibration["panel_split"]
            lines.append(f"候选选择与校准使用标签隔离的来源面板：选择 {_text(len(split.get('selection_origin_dates', [])))} 个原点，"
                         f"校准 {_text(len(split.get('calibration_origin_dates', [])))} 个原点；冻结候选摘要 {_text(calibration.get('frozen_family_hash'))}。")
        lines.append("时序块风险校准限于历史来源映射的当前条款损失；与当前联合情景 CVaR 分列，不是未来本金损失的有限样本概率保证。")
        for action in calibration.get("required_actions") or []:
            lines.append("- 校准补核：" + _text(action))
    source_actions = summary.get("source_action_diagnostics", {})
    if source_actions.get("status"):
        lines.append("来源动作菜单："+_text(source_actions["status"])+"；范围 "+_text(source_actions.get("scope"))+
                     "；完整资金路径政策数 "+_text(summary.get("frozen_policy_count"))+"。")
        for audit in source_actions.get("funding_options") or []:
            lines.append(f"- 追加档位 {_text(audit['proposed_contribution'])} 元：菜单组合 {_text(audit.get('combination_count'))}；"
                         f"声明当前动作集合已完成 {_text(audit.get('current_action_scope_complete'))}；"
                         f"覆盖全部来源精度格点 {_text(audit.get('source_precision_domain_complete'))}。"
                         "菜单内比较不构成所有金额或全市场最优证明。")
    stress = summary.get("source_principal_stress")
    if stress:
        lines.append(f"来源支持压力下财富下界 {_text(stress['wealth_lower'])} 元；原本金下限 {_text(stress['principal_floor'])} 元；"
                     f"是否仍满足本金下限 {_text(stress['principal_floor_survives'])}。该压力不赋予事件发生概率。")
    for forecast in summary.get("forecasts", []):
        if "expected_gross_return" in forecast:
            lines.append(f"- {forecast['code']}：同期限模型预测基金收益率 {forecast['expected_gross_return']:.2%}（未扣账户申赎费用）。")
    analyses = bundle["action_analysis"].get("source_analyses", {})
    quality = analyses.get("quality", {})
    for code, product in quality.get("products", {}).items():
        lines.append(f"- {code}基金质量证据：{_text(product['readiness'])}；"
                     f"原文任期/统计 {_text(product.get('statistics'))}；待核 {_text(product['required_actions'])}。")
    if quality:
        lines.append("经理因子统计为模型依赖的回顾估计；块区间及Holm检验为声明家庭下近似推断，不作为经理技能证明或额外预测收益。")
    for code, product in analyses.get("exposure", {}).get("products", {}).items():
        lines.append(f"- {code}行业现货资产/基金净资产约束：通用 {_text(product['default_bound'])}；"
                     f"行业 {_text(product['sectors'])}；待核 {_text(product['required_actions'])}。未知上界未填1，衍生名义/Delta及缺来源的穿透不在该界范围。")
    readiness = analyses.get("readiness", {})
    if readiness:
        lines.append("逐产品及联合资金集合资格：" + _text(readiness.get("readiness_by_code")) + "；覆盖：" + _text(readiness.get("coverage")) + "。")
    families = analyses.get("families", {})
    if families:
        lines.append("同本金合格资金集合比较：" + _text(families["selection"]) + "。")
        for item in families.get("results", []):
            lines.append("- 已评价资金集合 " + _text(item["family"]["family_id"]) + "；计算原始引用 " + _text(item["calculation_ref"]) + "。")
    if summary.get("future_valuation_rule"):
        lines.append("后续路径规则仅用于财富与到账情景估值：" + _text(summary["future_valuation_rule"]) + "；实际成交或到账后按新信息重新分析，未生成后续日期订单。")
    lines += ["", "判断依据：" + _text(bundle.get("reason", "依据已绑定账户、费用和风险约束比较方案。")),
              "资格范围：" + _text(bundle["qualification"]["reason"]),
              f"决策标识：{bundle['decision_id']}；摘要：{bundle['bundle_hash']}。"]
    return "\n".join(lines) + "\n"
