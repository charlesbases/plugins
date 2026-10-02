"""Chronological, paired after-fee selection of an EN quality covariate block.

This consumes selection records only. It creates no orders, calibration claims,
or ledger facts. A one-point prediction screens source policies; its risk is
explicitly not a residual CVaR qualification. Final production calibration and
joint-path risk checks remain mandatory after this feature choice is frozen.
"""
import copy
import math

from contracts import fingerprint, instant, require
import allocation
import portfolio_mpc as mpc
import single_step_wealth
import verify


def _finish(value):
    value["frozen_hash"] = fingerprint(value)
    return value


def _path_contract(context, contract, path):
    require(contract["price_schema"] == path.get("price_schema") == "source_role_price_paths_v3",
            "Quality selection requires role-separated source prices")
    require(path["known_marks"] == contract["known_marks"], "Quality selection changed known source marks")
    require(set(context["allocation_codes"]) == set(contract["codes"]), "Quality selection changed the fund family")


def _screen(context, contract, predicted, policies):
    """Read predictions only; realized selection outcomes are inaccessible here."""
    distribution = mpc.as_current_order_distribution(context, {
        "status": "research_ready", "path_hash": fingerprint(predicted),
        "point_paths": [predicted], "selection_paths": [{**predicted, "probability": 1.}]})
    prices = {code: predicted["nav"][clock["pricing_date"]][code]
              for code, clock in distribution["clock_context"]["assets"].items()}
    capital = float(context["snapshot"]["equity"])
    budget = float(context["risk_state"]["remaining_loss_budget"])
    candidates, rejected = [], []
    for policy in policies:
        try:
            value = mpc.simulate(context, predicted, policy["current_action"], policy["future_rule"], policy["local_decision_dates"])
            mpc.verify_cash_flow(value)
            cash_ok = verify.cash_deadline_invariants(context, policy, predicted, value,
                                                      primary_end=policy["local_decision_dates"][-1])
            projection = single_step_wealth.project(context, policy["current_action"], distribution, pricing_nav=prices)
            if cash_ok and projection["concentration_feasible"] and capital-value["terminal_wealth"] <= budget:
                candidates.append((policy, value))
            else:
                rejected.append({"policy_id": policy["id"], "reason": "source_cash_exposure_or_point_principal_budget"})
        except mpc.PolicyInfeasible as exc:
            rejected.append({"policy_id": policy["id"], "reason": str(exc)})
    if not candidates:
        return None, rejected
    return min(candidates, key=lambda item: (-item[1]["terminal_wealth"], item[1]["fees"],
                                             item[1]["policy_turnover"], item[0]["id"])), rejected


def select_quality_arm(context, source_contract, records_by_arm, *, source_hash,
                       feature_names_by_arm, on_source_gaps=()):
    """Choose a covariate block by paired source-policy wealth under declared risk.

Both arms use the same frozen policy class, capital, origins and outcome bytes.
Each origin's policy is chosen from its prediction before its realized path is
read. This is model selection, not historical user performance or causal skill.
"""
    require(set(records_by_arm) == {"off", "on"} and set(feature_names_by_arm) == {"off", "on"},
            "Predeclared quality-on/off arms required")
    off, on = records_by_arm["off"], records_by_arm["on"]
    require(off and source_hash, "Source-qualified off-arm selection records required")
    result = {"schema_id": "source_quality_block_selection_v1", "selected_arm": "off",
        "source_hash": source_hash, "source_contract_hash": fingerprint(source_contract),
        "feature_names_by_arm": copy.deepcopy(feature_names_by_arm), "source_gaps": list(on_source_gaps),
        "selection_evidence": {}, "trade_ready": False, "quality_effectiveness_proven": False,
        "criterion": "maximum_paired_mean_afterfee_wealth_subject_to_empirical_principal_CVaR; ties_off",
        "risk_scope": "point_policy_screen_and_selection_empirical_tail_not_final_joint_residual_qualification",
        "historical_scope": "current_contract_current_capital_source_path_mapping_not_historical_actual_fills",
        "calibration_outcomes_consumed": False}
    require(len(off) >= 2 and len({row["origin_at"] for row in off}) == len(off), "Distinct forward selection origins required")
    for records in records_by_arm.values():
        for record in records:
            require(instant(record["origin_at"]) < instant(record["label_available_at"]) <= instant(context["decision_at"]),
                    "Quality selection outcome is future or immature")
            require(record["source_hashes"] and record["model_training_audit"], "Original selection lineage required")
            for key in ("predicted", "realized"):
                _path_contract(context, source_contract, record[key])
    if on:
        require(len(on) == len(off), "Quality arms must retain identical origin inventories")
        for left, right in zip(off, on):
            require(all(left[key] == right[key] for key in ("origin_at", "label_available_at", "source_hashes"))
                    and fingerprint(left["realized"]) == fingerprint(right["realized"]),
                    "Quality arms changed original realized source paths")
            for key in ("training_rows", "training_origin_dates"):
                require(key in left["model_training_audit"] and left["model_training_audit"][key] == right["model_training_audit"].get(key),
                        "Quality feature comparison requires identical mature training cohorts")
    decision = context["spec"]["decision"]
    family = mpc.freeze_policies(context, allocation.build_source_actions(context),
        max_current_actions=decision["max_current_actions"], cash_fractions=decision["future_cash_fractions"])
    result["policy_family_hash"] = family["family_hash"]
    zero = next(group for group in family["groups"] if group["proposed_contribution"] == 0)
    if zero["build_status"] != "complete":
        result["status"] = "selection_scope_incomplete"
        result["source_gaps"] += zero["required_actions"]
        return _finish(result)
    capital = float(context["snapshot"]["equity"])
    for arm, records in records_by_arm.items():
        if not records:
            result["selection_evidence"][arm] = {"status": "source_gap", "origins": []}
            continue
        rows = []
        for record in records:
            screened, rejected = _screen(context, source_contract, record["predicted"], zero["policies"])
            if screened is None:
                result["source_gaps"].append({"action": "complete_risk_feasible_quality_selection_policy",
                                             "arm": arm, "origin_at": record["origin_at"]})
                break
            policy, predicted = screened
            realized = mpc.simulate(context, record["realized"], policy["current_action"], policy["future_rule"], policy["local_decision_dates"])
            mpc.verify_cash_flow(realized)
            cash_ok = verify.cash_deadline_invariants(context, policy, record["realized"], realized,
                                                      primary_end=policy["local_decision_dates"][-1])
            rows.append({"origin_at": record["origin_at"], "outcome_available_at": record["label_available_at"],
                "policy_id": policy["id"], "policy_hash": fingerprint(policy), "source_hashes": record["source_hashes"],
                "training_audit_hash": fingerprint(record["model_training_audit"]),
                "predicted_wealth": predicted["terminal_wealth"], "realized_wealth": realized["terminal_wealth"],
                "fees": realized["fees"], "turnover": realized["policy_turnover"], "cash_requirement_passes": cash_ok,
                "predicted_valuation_hash": predicted["valuation_hash"], "realized_valuation_hash": realized["valuation_hash"],
                "rejected_predicted_policy_count": len(rejected)})
        complete = len(rows) == len(records)
        evidence = {"status": "complete" if complete else "incomplete", "origins": rows}
        if complete:
            probabilities = [1./len(rows)]*len(rows)
            cvar = mpc.tail_mean([capital-row["realized_wealth"] for row in rows], probabilities,
                                 context["spec"]["allocation"]["tail_probability"])
            evidence.update(mean_afterfee_wealth=math.fsum(row["realized_wealth"] for row in rows)/len(rows),
                empirical_principal_cvar=cvar,
                risk_feasible=cvar <= float(context["risk_state"]["remaining_loss_budget"])
                              and all(row["cash_requirement_passes"] for row in rows))
        result["selection_evidence"][arm] = evidence
    eligible = [(arm, row) for arm, row in result["selection_evidence"].items()
                if row["status"] == "complete" and row["risk_feasible"]]
    result["status"] = "selected" if eligible else "selection_risk_unqualified"
    if eligible:
        result["selected_arm"] = min(eligible, key=lambda pair: (-pair[1]["mean_afterfee_wealth"], pair[0] != "off"))[0]
    if on and all(row["status"] == "complete" for row in result["selection_evidence"].values()):
        result["paired_mean_wealth_increment"] = math.fsum(b["realized_wealth"]-a["realized_wealth"]
            for a, b in zip(result["selection_evidence"]["off"]["origins"], result["selection_evidence"]["on"]["origins"]))/len(off)
    return _finish(result)
