"""Explicit analytical source frames; no historical-market skill qualification."""
import copy
import datetime as dt
import math
import unittest

import allocation_market as market
import industry_model
import portfolio_paths as paths
import quality_selection
import cash_reference
from contracts import fingerprint
from fund_quality import QUALITY_FEATURE_NAMES
from test_allocation import source_context
from test_single_step import make_terms


class QualityPriceRoleTests(unittest.TestCase):
    @staticmethod
    def marks(codes):
        return {code: {"nav_date": "2029-12-31", "value": "1", "known_at": "2029-12-31T16:00:00+08:00",
                       "source_ref": {"explicit_analytical_original": code}} for code in codes}

    def test_quote_accounting_grid_does_not_spend_local_decision_budget(self):
        context = source_context()
        codes = ["F"+str(i) for i in range(10)]
        context["allocation_codes"] = context["universe"] = codes
        context["fee_contracts"] = {code: make_terms(code) for code in codes}
        context["model_request"]["assets"] = [{**copy.deepcopy(context["model_request"]["assets"][0]), "code": code} for code in codes]
        context["snapshot"]["prices"] = context["market_ref"]["prices"] = {code: "1" for code in codes}
        context["known_marks"] = self.marks(codes)
        for i, code in enumerate(codes):
            context["fee_contracts"][code]["confirmation"]["lag_days"] = i+1
        contract = paths.stage_contract(context, max_stages=8)
        self.assertGreater(len(contract["stage_dates"]), 8)
        self.assertEqual(contract["nav_dates"], [(dt.date(2030, 1, 1)+dt.timedelta(days=i)).isoformat() for i in range(31)])
        for phase in contract["boundaries"]:
            for key in ("pricing_date", "confirmation_date", "settlement_date"):
                self.assertIn(phase[key], contract["stage_dates"])

    def test_watch_peer_mark_remains_original_but_is_not_an_execution_coordinate(self):
        context = source_context()
        context["known_marks"] = self.marks(["C", "unheld-stale-peer"])
        context["price_schema"] = "source_role_price_paths_v3"
        original = copy.deepcopy(context["known_marks"])
        contract = paths.stage_contract(context)
        self.assertEqual(set(contract["known_marks"]), {"C"})
        self.assertEqual(context["known_marks"], original)
        policy = {"proposed_contribution": "0", "current_action": {"buys": [], "sells": []}}
        leaf = {"price_schema": contract["price_schema"], "known_marks": contract["known_marks"], "nav": {}, "distributions": []}
        balances = {"cash": "8", "reserved_cash": "0", "receivables": "0"}
        cash_reference.reconcile(context, policy, leaf, [], "2030-01-31", balances)
        leaf["known_marks"]["C"]["value"] = "2"
        with self.assertRaisesRegex(ValueError, "known valuation binding changed"):
            cash_reference.reconcile(context, policy, leaf, [], "2030-01-31", balances)

    def test_real_nested_en_quality_beta_and_paired_source_policy_selection(self):
        base = dt.date(2029, 10, 1)
        frames, panel = [], []
        off_names = market.FEATURE_NAMES + industry_model.FUND_FEATURE_NAMES
        on_names = off_names + QUALITY_FEATURE_NAMES
        for i in range(70):
            day = (base+dt.timedelta(days=i)).isoformat()
            signal = 1. if i in (60, 65) else math.sin(i*.8)
            row = {"code": "C", "decision_date": day, "fund_group_id": "fund-C", "x": [0.]*len(off_names),
                   "wealth_ratios": [1.]*8+[math.exp(-.1+.25*signal)], "cash": [], "base_nav": 1.,
                   "label_available_date": (base+dt.timedelta(days=i+1)).isoformat(),
                   "label_available_at": (base+dt.timedelta(days=i+1)).isoformat()+"T16:00:00+08:00",
                   "source_hash": fingerprint({"explicit_analytical_original": i})}
            row["frame_versions"] = [copy.deepcopy(row)]
            frames.append(row)
            feature = {"code": "C", "decision_at": day+"T00:00:00+08:00", "source_known_at": "2029-09-30T16:00:00+08:00",
                       "values": [signal]+[0.]*(len(QUALITY_FEATURE_NAMES)-1), "team_regime": ["analytical-team"]}
            feature["feature_hash"] = fingerprint(feature)
            panel.append(feature)
        bundle = {"feature_names": QUALITY_FEATURE_NAMES, "feature_panel": panel}
        bundle["quality_bundle_hash"] = fingerprint(bundle)
        on_frames = [paths._quality_features(row, bundle, row["decision_date"]+"T12:00:00+08:00") for row in frames]
        policy = {"train_window_days": 200, "min_train_dates": 20, "cv_folds": 3, "cv_initial_train_fraction": .6,
                  "alpha_grid": [.0001, .001], "l1_ratio_grid": [.5, 1.]}
        context = source_context(cash="8")
        context["spec"]["planning"]["primary_horizon_days"] = 8
        marks = {"C": {"nav_date": "2029-12-31", "value": "1", "known_at": "2029-12-31T16:00:00+08:00",
                        "source_ref": {"explicit_analytical_original": True}}}
        contract = {"price_schema": "source_role_price_paths_v3", "codes": ["C"], "known_marks": marks,
                    "origin_date": "2030-01-01", "decision_at": context["decision_at"],
                    "nav_dates": [(dt.date(2030, 1, 1)+dt.timedelta(days=i)).isoformat() for i in range(9)]}
        arms = {"off": [], "on": []}
        for i in (60, 65):
            origin = frames[i]["decision_date"]
            for arm, source, names in (("off", frames, off_names), ("on", on_frames, on_names)):
                model, audit = paths._fit_at(source, ["C"], origin, policy, [], ["fund-C"], feature_names=names)
                selected = source[i]
                if arm == "on":
                    self.assertTrue(all(len(row["x"]) == len(on_names) for row in paths._eligible(source, ["C"], origin, policy)))
                    self.assertGreater(abs(model["coefficients"][-1][len(off_names)]), .05)
                actual = paths._targets(selected, [])
                predicted = paths._predict(model, selected)
                def decoded(values, identity):
                    return paths._decode({"C": {"values": values, "cash_keys": []}}, {"C": 1.}, contract,
                                         origin+"T12:00:00+08:00", [], identity, 1., selected["label_available_at"])
                arms[arm].append({"origin_at": origin+"T12:00:00+08:00", "label_available_at": selected["label_available_at"],
                    "source_hashes": [frames[i]["source_hash"]], "model_training_audit": audit,
                    "predicted": decoded(predicted, "predicted-"+origin), "realized": decoded(actual, "realized-"+origin)})
        value = quality_selection.select_quality_arm(context, contract, arms, source_hash=fingerprint(frames),
                            feature_names_by_arm={"off": off_names, "on": on_names})
        self.assertEqual(value["selected_arm"], "on")
        self.assertGreater(value["paired_mean_wealth_increment"], 0.)
        self.assertFalse(value["calibration_outcomes_consumed"])
        self.assertFalse(value["trade_ready"])

    def test_previous_origin_team_features_cannot_fill_current_team_source_gap(self):
        feature = {"code": "C", "decision_at": "2030-01-01T00:00:00+08:00", "source_known_at": "2029-12-31T16:00:00+08:00",
                   "values": [1.]*len(QUALITY_FEATURE_NAMES), "team_regime": ["prior-team"]}
        feature["feature_hash"] = fingerprint(feature)
        bundle = {"feature_names": QUALITY_FEATURE_NAMES, "feature_panel": [feature]}
        bundle["quality_bundle_hash"] = fingerprint(bundle)
        with self.assertRaisesRegex(ValueError, "current team"):
            paths._quality_features({"code": "C", "x": [0.]*45}, bundle, "2030-01-02T08:00:00+08:00")

    def test_conditional_sale_independent_cash_uses_observed_trigger_and_source_fee_age(self):
        terms = make_terms()
        terms["redemption"] = {"kind": "holding_tiers", "bands": [
            {"minimum": "0", "fee": {"kind": "percentage", "rate": "0.015"}},
            {"minimum": "7", "fee": {"kind": "percentage", "rate": "0.005"}}]}
        terms["confirmation"]["lag_days"] = 0
        terms["settlement"]["lag_days"] = 1
        context = {"as_of": "2030-01-01", "decision_at": "2030-01-01T08:00:00+08:00", "fee_contracts": {"C": terms},
            "snapshot": {"available_cash": "0", "reserved_cash": "0", "unsettled_cash": "0", "positions": [
                {"lot_id": "old", "code": "C", "shares": "1000", "reserved_shares": "0", "acquired_at": "2029-12-26T00:00:00+08:00"}]}}
        context["allocation_codes"] = ["C"]
        context["snapshot"]["prices"] = {"C": "1"}
        context["spec"] = {"planning": {"cash_deadline_days": None}}
        context["model_request"] = {"assets": [{"code": "C", "sellable": True}]}
        condition = {"initial_action": {"buys": [], "sells": []}, "current_buy": None,
            "source_action": {"buys": [], "sells": [{"lot_id": "old", "shares": "1000"}]},
            "reference_value": "1000", "comparison": "le", "decision_dates": ["2030-01-02"], "submission_time": "08:00:00"}
        policy = {"proposed_contribution": "0", "current_action": {"buys": [], "sells": []},
                  "local_decision_dates": ["2030-01-01", "2030-01-02", "2030-01-09"], "future_rule": {"conditional_sells": [condition]}}
        path = {"nav": {"2030-01-02": {"C": "1"}}, "distributions": []}
        rows = [{"kind": "price_sell", "date": "2030-01-02", "code": "C", "lot_id": "old", "shares": "1000", "nav": "1",
                 "submitted_at": "2030-01-02T08:00:00+08:00", "gross": "1000", "fee": "5", "receivable": "995", "pay_date": "2030-01-03",
                 "cash_available_now": False},
                {"kind": "credit_sale", "date": "2030-01-03", "code": "C", "amount": "995", "available_before": "0",
                 "available_delta": "995", "reserved_delta": "0", "available_after": "995", "reserved_after": "0"}]
        balances = {"cash": "995", "reserved_cash": "0", "receivables": "0"}
        self.assertEqual(cash_reference.reconcile(context, policy, path, rows, "2030-01-03", balances)["fees"], 5)
        forged = copy.deepcopy(rows)
        forged[0]["submitted_at"] = context["decision_at"]
        with self.assertRaisesRegex(ValueError, "sale pricing clock"):
            cash_reference.reconcile(context, policy, path, forged, "2030-01-03", balances)
        forged_policy = copy.deepcopy(policy)
        forged_policy["future_rule"]["conditional_sells"][0]["source_action"]["sells"][0]["shares"] = "500"
        with self.assertRaisesRegex(ValueError, "reference value"):
            cash_reference.reconcile(context, forged_policy, path, rows, "2030-01-03", balances)


if __name__ == "__main__":
    unittest.main()
