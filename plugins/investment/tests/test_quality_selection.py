"""Paired source-policy cash truths; synthetic data never certify market skill."""
import copy
import unittest

from contracts import fingerprint
import quality_selection
from test_allocation import source_context
from test_portfolio_mpc import path


def case(off_terminal=1., on_terminal=1.2, actual_terminal=1.2, entry_rate="0"):
    context = source_context(cash="8")
    context["spec"]["planning"]["primary_horizon_days"] = 8
    context["fee_contracts"]["C"]["subscription"] = {"kind": "percentage", "rate": entry_rate}
    context["fee_contracts"]["C"]["trade_precision"]["money_step"] = "0.01"
    context["context_hash"] = fingerprint({k:v for k,v in context.items() if k != "context_hash"})
    marks = {"C": {"nav_date": "2029-12-31", "value": "1", "known_at": "2029-12-31T16:00:00+08:00",
                    "source_ref": {"synthetic_mathematical_mark": True}}}
    contract = {"price_schema": "source_role_price_paths_v3", "known_marks": marks, "codes": ["C"]}
    def leaf(terminal):
        value = path(["C"],lambda _, day: terminal if day == "2030-01-09" else 1.)
        value.update(price_schema=contract["price_schema"], known_marks=marks)
        return value
    arms = {"off": [], "on": []}
    for day in ("2029-11-01", "2029-12-01"):
        common = {"origin_at": day+"T12:00:00+08:00", "label_available_at": day+"T23:00:00+08:00",
            "source_hashes": [fingerprint({"synthetic_observed_outcome": day})], "realized": leaf(actual_terminal),
            "model_training_audit": {"training_rows": 20, "training_origin_dates": ["2029-10-01"]}}
        for arm, terminal in (("off", off_terminal), ("on", on_terminal)):
            arms[arm].append({**copy.deepcopy(common), "predicted": leaf(terminal)})
    return context, contract, arms


def select(context, contract, arms, gaps=()):
    return quality_selection.select_quality_arm(context, contract, arms, source_hash=fingerprint(arms),
        feature_names_by_arm={"off": ["native_NAV"], "on": ["native_NAV", "quality_source_metric"]}, on_source_gaps=gaps)


class QualityBlockPolicySelectionTests(unittest.TestCase):
    def test_same_funds_cash_source_policy_wealth_selects_on_without_trade_permission(self):
        context, contract, arms = case()
        value = select(context, contract, arms)
        self.assertEqual(value["selected_arm"], "on")
        self.assertAlmostEqual(value["paired_mean_wealth_increment"], 1.6)
        self.assertFalse(value["trade_ready"])
        self.assertFalse(value["calibration_outcomes_consumed"])
        self.assertEqual(value, select(context, contract, copy.deepcopy(arms)))

    def test_entry_fee_erases_raw_gain_and_ties_use_off(self):
        context, contract, arms = case(on_terminal=1.005, actual_terminal=1.005, entry_rate="0.01")
        value = select(context, contract, arms)
        self.assertEqual(value["selected_arm"], "off")
        self.assertEqual(value["paired_mean_wealth_increment"], 0.)

    def test_original_outcomes_and_mature_training_cohort_cannot_be_changed(self):
        context, contract, arms = case()
        for field in ("source", "training"):
            corrupt = copy.deepcopy(arms)
            if field == "source":
                corrupt["on"][0]["realized"]["nav"]["2030-01-09"]["C"] = 99.
            else:
                corrupt["on"][0]["model_training_audit"]["training_origin_dates"] = ["2029-10-02"]
            with self.subTest(field=field), self.assertRaises(ValueError):
                select(context, contract, corrupt)

    def test_absent_quality_history_has_explicit_gap_and_real_off_policy_evaluation(self):
        context, contract, arms = case()
        arms["on"] = []
        gap = {"action": "supply_original_quality_vintages_before_training_origins", "code": "C"}
        value = select(context, contract, arms, [gap])
        self.assertEqual(value["selected_arm"], "off")
        self.assertEqual(value["selection_evidence"]["off"]["status"], "complete")
        self.assertEqual(value["selection_evidence"]["on"]["status"], "source_gap")
        self.assertIn(gap, value["source_gaps"])


if __name__ == "__main__":
    unittest.main()
