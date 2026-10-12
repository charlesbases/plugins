"""整笔预计完成策略；预计不会生成实际份额、金额或实际时间。"""
from copy import deepcopy

from investment.contracts import bounds, instant, is_known, known, na, unknown


def target_phase(trade):
    if trade["confirmation"]["state"] in ("cancelled", "failed"):
        return None
    return "confirmation" if trade["side"] == "buy" else "settlement"


def prepare_questions(bundle, prepared_at):
    explicit = {r["trade_id"] for r in bundle["source"]["arrival_replies"]["rows"] if r["result"] != "no_reply"}
    questions = []
    for trade in bundle["baseline"]["trades"]["rows"]:
        phase = target_phase(trade)
        expected = bounds(trade["expected_at"])
        if phase and trade[phase]["state"] == "pending" and expected and expected[0] > instant(prepared_at) and trade["trade_id"] not in explicit:
            questions.append({"trade_id": trade["trade_id"], "instrument_id": trade["instrument_id"], "kind": "buy_confirmation" if phase == "confirmation" else "sell_settlement", "expected_at": trade["expected_at"]})
    return {"schema_version": 1, "stage_id": "SC01", "account_id": bundle["source"]["account_id"], "base_generation": bundle["source"]["base_generation"], "prepared_at": prepared_at, "questions": questions}


def apply_due_policy(trades, replies, cutoff):
    result = deepcopy(trades)
    by_id = {reply["trade_id"]: reply for reply in replies}
    for trade in result:
        phase = target_phase(trade)
        if phase is None:
            continue
        reply = by_id.get(trade["trade_id"])
        explicit = reply is not None and reply["result"] != "no_reply"
        if explicit:
            trade["last_arrival_reply"] = known({"result": reply["result"], "reported_at": reply["reported_at"]["value"]})
        if explicit and reply["result"] == "not_completed":
            for name in ("confirmation", "settlement"):
                if trade[name]["basis"] == "expected_due":
                    trade[name] = {"state": "pending", "actual_at": unknown(), "basis": "not_applicable"}
            forecast = reply["new_expected_at"]
            if forecast["status"] != "not_applicable":
                trade["expected_at"] = deepcopy(forecast)
            else:
                previous = bounds(trade["expected_at"])
                if previous and previous[0] <= instant(cutoff):
                    trade["expected_at"] = unknown("withdrawn_forecast")
            continue
        completed = explicit and reply["result"] == "completed"
        forecast = bounds(trade["expected_at"])
        due = forecast is not None and forecast[0] <= instant(cutoff)
        if completed or due:
            basis = "user_reported" if completed else "expected_due"
            if trade["confirmation"]["state"] == "pending":
                trade["confirmation"] = {"state": "confirmed", "actual_at": unknown(), "basis": basis}
            elif completed and trade["confirmation"]["basis"] == "expected_due":
                trade["confirmation"]["basis"] = "user_reported"
            if trade["side"] == "sell":
                if trade["settlement"]["state"] == "pending":
                    trade["settlement"] = {"state": "settled", "actual_at": unknown(), "basis": basis}
                elif completed and trade["settlement"]["basis"] == "expected_due":
                    trade["settlement"]["basis"] = "user_reported"
    return result
