"""Deliver the actual canonical report with this chain's advice and limitations."""
import copy

from contracts import require
from chains.common import check_control, context, emit
from chains.contracts import validate_chain
from chains.fund_universe import _refs
from chains.model import bound_bundle


def report(root, plan, previous, *, control_context):
    require(previous["stage"] == "recommendation", "Report must consume this chain's exact recommendations")
    check_control(root, plan, previous["chain_id"], control_context, previous=previous)
    result, bundle, artifacts = bound_bundle(root, plan, previous)
    ancestors = [artifacts.read_json(reference) for reference in previous["lineage_refs"]]
    validate_chain(ancestors + [previous], allow_partial=control_context.get("delivery_mode") == "observation")
    by_stage = {value["stage"]: value for value in ancestors}
    model = by_stage["model"]
    news = by_stage["news_collection"]
    universe = by_stage["fund_universe"]
    recommendation = previous["payload"]["recommendations"]
    markdown = artifacts.read(result["report_ref"]).decode("utf-8")
    require(markdown == result["report_markdown"], "Actual canonical report differs from its immutable public reference")
    import verify
    verify.verify_report(bundle, markdown)
    limitations = copy.deepcopy(bundle.get("missing", []))
    limitations.extend(copy.deepcopy(recommendation.get("limitations", [])))
    for value in ancestors + [previous]:
        if not value["stage_complete"]:
            limitations.append({"stage": value["stage"], "reason": "This stage's independent business postcondition has not passed"})
    if model["payload"]["model_result"]["execution_status"] != "qualified":
        conclusion = "本次科学比较与当前组合建议尚未完成独立验收，投资目标未达成。"
    else:
        conclusion = "已保留本次实际模型和逐持仓建议；完整研究范围及最终投资目标仍须独立验收。"
    if not recommendation["holding_codes"]:
        conclusion += ("当前没有已有基金持仓；新增买入建议及金额只来自本次实际已验收的组合动作。"
                       if any(row["action"] == "increase" and row["qualification"] == "qualified"
                              for row in recommendation["actions"])
                       else "当前没有已确认基金持仓，保留已确认现金并观察。")
    else:
        conclusion += " " + "；".join(row["code"] + "：" + ({"increase": "增持", "reduce": "减持"}.get(row["action"])
                                      or ("保持" if row["qualification"] == "qualified" else "保持观察"))
                                  for row in recommendation["actions"] if row["code"] in recommendation["holding_codes"]) + "。"
    coverage = news["payload"]["news_material"].get("coverage", {})
    peer_scope = universe["payload"]["frozen_universe"]
    source_complete = (coverage.get("source_scope_complete") is True and news["qualifications"]["source_integrity"]
                       and not peer_scope.get("unavailable")
                       and all(row["membership_complete"] for row in peer_scope["peer_groups"]))
    refs = _refs(result["bundle_ref"], result["report_ref"], previous["source_refs"], previous["lineage_refs"])
    data = {"conclusion": conclusion, "recommendations_hash": previous["output_hash"],
        "model_hash": recommendation["model_hash"], "goal_achieved": False,
        "coverage": {"source_scope_complete": bool(source_complete),
                     "comparison_scope_complete": model["qualifications"]["comparison_scope"], "source_refs": refs},
        "report_markdown": markdown, "limitations": limitations,
        "public_report_ref": result["report_ref"], "public_bundle_ref": result["bundle_ref"],
    }
    return emit(previous, "report", data, control_context=control_context,
                engine_context=context(previous), status="needs_evidence", source_refs=refs)
