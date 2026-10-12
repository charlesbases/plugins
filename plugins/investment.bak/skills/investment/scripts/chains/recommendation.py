"""Current-portfolio advice from this chain's actual accepted decision bundle."""
import copy
from decimal import Decimal, localcontext

import ledger
from contracts import require
from chains.common import check_control, context, emit
from chains.fund_universe import _refs
from chains.model import bound_bundle


def _observation(code, reason, evidence):
    return {"code": code, "action": "hold", "reason": reason,
        "amount_CNY": None, "shares": None, "fees_CNY": None,
        "evidence_refs": copy.deepcopy(evidence), "qualification": "unknown"}


def actions_from_bundle(model, bundle, evidence):
    """Keep actual reference-price legs; do not turn missing proof into zero."""
    actual = bundle["action_analysis"]["actions"]
    qualified = (model["execution_status"] == "qualified"
                 and model["model_fit_executed"] and model["cash_compared"])
    rows = {}
    for item in actual:
        rows.setdefault(item["code"], []).append(item)
    codes = set(model["holding_codes"]) | (set(rows) if qualified else set())
    result = []
    for code in sorted(codes):
        if not qualified or code not in model["compared_codes"]:
            result.append(_observation(code,
                "保持当前已确认持仓并观察；本次未取得该基金完整、独立核验的模型、费用与风险依据，调整金额和风险仍未知。", evidence))
            continue
        native = rows.get(code, [])
        if not native:
            result.append({"code": code, "action": "hold",
                "reason": "本次已验收的实际组合方案未对该持仓提交增减动作；依据保留在当前组合与费用、风险证据中。",
                "amount_CNY": "0", "shares": "0", "fees_CNY": "0",
                "evidence_refs": copy.deepcopy(evidence), "qualification": "qualified"})
            continue
        sides = {row["side"] for row in native}
        if len(sides) != 1:
            result.append(_observation(code,
                "实际组合包含同基金的双向动作；单一增减金额尚未完成核对，保留原始动作证据及当前持仓。", evidence))
            continue
        side = next(iter(sides))
        require(side in {"buy", "sell"}, "Actual public action has an unknown side")
        with localcontext() as decimal_context:
            decimal_context.prec = 80
            shares = sum((Decimal(row["reference_valuation"]["shares"]) for row in native), Decimal(0))
            fees = sum((Decimal(row["estimated_fee"]) for row in native), Decimal(0))
            amount = sum((Decimal(row["reference_valuation"]["cash_debit" if side == "buy" else "gross"])
                          for row in native), Decimal(0))
        result.append({"code": code, "action": "increase" if side == "buy" else "reduce",
            "reason": "；".join(row["reason"] for row in native),
            "amount_CNY": ledger.decimal(amount), "shares": ledger.decimal(shares), "fees_CNY": ledger.decimal(fees),
            "amount_basis": "actual_subscription_request_CNY" if side == "buy" else "actual_reference_redemption_gross_CNY",
            "evidence_refs": copy.deepcopy(evidence), "qualification": "qualified",
            "actual_action_legs": copy.deepcopy(native)})
    return result


def recommend(root, plan, previous, *, control_context):
    require(previous["stage"] == "model", "Recommendations must consume this chain's exact model output")
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    result, bundle, _ = bound_bundle(root, plan, previous)
    model = copy.deepcopy(previous["payload"]["model_result"])
    # Only the independently authenticated scheduler output can carry the
    # scientific status into actual portfolio advice.
    if not (previous["qualifications"]["predictive_validation"]
            and previous["qualifications"]["comparison_scope"]):
        model["execution_status"] = "insufficient_data" if not model["model_fit_executed"] else "unqualified"
    evidence = _refs(result["bundle_ref"], model["evidence_refs"])
    actions = actions_from_bundle(model, bundle, evidence)
    has_new_buy = any(row["action"] == "increase" and row["qualification"] == "qualified" for row in actions)
    data = {"holding_codes": copy.deepcopy(model["holding_codes"]),
        "risk_codes": copy.deepcopy(model.get("risk_codes", [])),
        "candidate_codes": copy.deepcopy(model["candidate_codes"]),
        "model_hash": previous["output_hash"], "actions": actions,
        "scope": "actual_current_H_and_independently_compared_Q_only; unknown_risk_is_observation_not_optimal_hold",
        "current_cash_CNY": previous["payload"]["account_context"]["cash"],
        "cash_conclusion": ("新增投入采用本次已独立验收的实际组合动作；可用现金与费用以当前资金证据为准。"
                            if has_new_buy else "保留已确认现金；未通过独立科学及金融验收的新增买入不生成金额。"),
        "limitations": copy.deepcopy(model["comparison_gaps"]),
        "bundle_ref": result["bundle_ref"],
    }
    return emit(previous, "recommendation", data, control_context=control_context,
                engine_context=context(previous), status="needs_evidence", source_refs=evidence)
