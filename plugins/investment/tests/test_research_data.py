"""Synthetic contract fixtures; no real portfolio, downloads or model fitting."""

import copy
import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills/investment/scripts/research_data.py"
spec = importlib.util.spec_from_file_location("investment_research_data", SCRIPT)
research = importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)
CODE = "123456"
START = dt.date(2020, 1, 1)
OBSERVED = "2021-01-01T20:00:00+08:00"


def timestamp(day):
    return int(dt.datetime.combine(day, dt.time(), research.CN).timestamp() * 1000)


def nav_text(observations):
    nav, cumulative = [], []
    for day, value, accumulated, note in observations:
        nav.append({"x": timestamp(day), "y": value, "unitMoney": note})
        cumulative.append([timestamp(day), accumulated])
    return ("var fS_name = " + json.dumps("Synthetic fund") + ";\n"
            "var fS_code = " + json.dumps(CODE) + ";\n"
            "var Data_netWorthTrend = " + json.dumps(nav, ensure_ascii=False) + ";\n"
            "var Data_ACWorthTrend = " + json.dumps(cumulative) + ";\n")


def normalized(count=123):
    return [{"code": CODE, "date": (START + dt.timedelta(days=i)).isoformat(),
             "nav": 2.0, "cumulative_nav": 3.0, "distribution_per_share": 0.0,
             "distribution_text": "", "provider_daily_return": None,
             "historical_published_at": None} for i in range(count)]


def issuer_correction():
    return {"distribution_per_share": .2, "source_url": "https://issuer.example/report.pdf",
            "location": "A class, ex-date, cash per share",
            "source_sha256": "a" * 64}


class ResearchDataTests(unittest.TestCase):
    def parse(self, text, corrections=None):
        return research.parse_nav(text, CODE, START, START + dt.timedelta(days=366), corrections or {})

    def test_missing_arrays_are_rejected(self):
        text = nav_text([(START, 2, 3, "")])
        for name in ("fS_name", "Data_netWorthTrend", "Data_ACWorthTrend"):
            with self.subTest(name=name), self.assertRaises(research.ResearchError):
                self.parse(text.replace("var " + name, "var missing_" + name))

    def test_wrong_fund_or_missing_provider_identity_is_rejected(self):
        text = nav_text([(START, 2, 3, "")])
        for invalid in (text.replace('var fS_code = "123456"', 'var fS_code = "654321"'),
                        text.replace("var fS_code", "var missing_fS_code"),
                        text.replace('var fS_code = "123456"', 'var fS_code = 123456')):
            with self.subTest(invalid=invalid), self.assertRaises(research.ResearchError):
                self.parse(invalid)

    def test_non_json_and_nonfinite_arrays_are_rejected(self):
        text = nav_text([(START, 2, 3, "")])
        for malformed in (text.replace('"y": 2', '"y": NaN'),
                          text.replace('"y": 2', '"y": 1e999'),
                          text.replace('"y": 2', '"y": 1e-999'),
                          text.replace('"y": 2', '"y": 2, "y": 3'),
                          text.replace('"y": 2', '"y": () => 2'),
                          text + "var Data_netWorthTrend = [];",
                          text.replace(";\nvar Data_ACWorthTrend", ".map(x => x);\nvar Data_ACWorthTrend")):
            with self.subTest(malformed=malformed), self.assertRaises(research.ResearchError):
                self.parse(malformed)

    def test_unknown_dividend_or_split_is_rejected(self):
        for note in ("拆分：每份折算2份", "未知分红", "每0份派现金1元"):
            with self.subTest(note=note), self.assertRaises(research.ResearchError):
                self.parse(nav_text([(START, 2, 3, note)]))

    def test_missing_provider_distribution_uses_verified_evidence(self):
        day = START + dt.timedelta(days=1)
        text = nav_text([(START, 2, 3, ""), (day, 1.9, 3.1, "")])
        with self.assertRaises(research.ResearchError):
            self.parse(text)
        rows, summary = self.parse(text, {day.isoformat(): issuer_correction()})
        self.assertEqual(rows[1]["distribution_text"], "")
        self.assertEqual(rows[1]["distribution_per_share"], .2)
        self.assertEqual(rows[1]["distribution_evidence"]["mode"], "evidence补录")
        self.assertEqual(summary["dividend_count"], 1)
        unverified = issuer_correction()
        del unverified["source_sha256"]
        with self.assertRaises(research.ResearchError):
            self.parse(text, {day.isoformat(): unverified})

    def test_provider_fixed_distribution_is_corroborated_without_overwrite(self):
        day = START + dt.timedelta(days=1)
        note = "分红：每10份派现金2元"
        rows, _ = self.parse(nav_text([(START, 2, 3, ""), (day, 1.9, 3.1, note)]),
                             {day.isoformat(): issuer_correction()})
        self.assertEqual(rows[1]["distribution_text"], note)
        self.assertEqual(rows[1]["distribution_per_share"], .2)
        self.assertEqual(rows[1]["distribution_evidence"]["mode"], "provider_already_matches")

    def test_conflicting_issuer_distribution_is_rejected(self):
        day = START + dt.timedelta(days=1)
        with self.assertRaisesRegex(research.ResearchError, "conflicts"):
            self.parse(nav_text([(START, 2, 3, ""), (day, 1.9, 3.0, "每份派现金0.1元")]),
                       {day.isoformat(): issuer_correction()})

    def test_duplicate_dates_and_timestamp_identity_are_rejected(self):
        day = START + dt.timedelta(days=1)
        for observations in ([(START, 2, 3, ""), (START, 2, 3, "")],
                             [(day, 2, 3, ""), (START, 2, 3, "")]):
            with self.subTest(observations=observations), self.assertRaises(research.ResearchError):
                self.parse(nav_text(observations))
        text = nav_text([(START, 2, 3, "")])
        mismatched = text.replace("var Data_ACWorthTrend = [[" + str(timestamp(START)),
                                  "var Data_ACWorthTrend = [[" + str(timestamp(day)))
        with self.assertRaises(research.ResearchError):
            self.parse(mismatched)

    def test_cash_distribution_return_is_not_cumulative_nav_ratio(self):
        rows = normalized()
        rows[121].update(nav=1.9, cumulative_nav=3.1, distribution_per_share=.2)
        rows[122].update(nav=2.0, cumulative_nav=3.2)
        _, labels, _ = research.build_samples(rows, {"code": CODE}, [1, 2], 120, OBSERVED)
        first = next(y for y in labels if y["start_nav_date"] == rows[120]["date"] and y["horizon_calendar_days"] == 1)
        # One unit of capital buys half a share: .5 * (1.9 + .2) = 1.05.
        self.assertAlmostEqual(first["market_return"], .05, places=12)
        self.assertNotAlmostEqual(first["market_return"], 3.1 / 3 - 1, places=6)
        following = next(y for y in labels if y["start_nav_date"] == rows[121]["date"] and y["horizon_calendar_days"] == 1)
        self.assertAlmostEqual(following["market_return"], 2 / 1.9 - 1, places=12)
        self.assertIsNone(first["actual_investor_net_return"])
        self.assertFalse(first["strict_investor_eligibility"])

    def test_principal_path_and_peak_losses_include_initial_capital(self):
        rows = normalized()
        rows[121].update(nav=1.4, cumulative_nav=2.6, distribution_per_share=.2)
        rows[122].update(nav=2.0, cumulative_nav=3.2)
        _, labels, _ = research.build_samples(rows, {}, [2], 120, OBSERVED)
        first = labels[0]
        self.assertAlmostEqual(first["market_return"], .1, places=12)
        self.assertEqual(first["terminal_loss"], 0)
        self.assertAlmostEqual(first["principal_path_loss"], .2, places=12)
        self.assertAlmostEqual(first["peak_drawdown"], .2, places=12)

    def test_past_features_ignore_future_changes_and_current_metadata(self):
        rows = normalized()
        xs, _, summary = research.build_samples(rows, {"fund_group_id": "shared-portfolio", "current_fees": 999, "manager": "today"}, [1], 120, OBSERVED)
        changed = copy.deepcopy(rows)
        changed[121].update(nav=100, cumulative_nav=101)
        changed_xs, _, _ = research.build_samples(changed, {}, [1], 120, OBSERVED)
        self.assertEqual(xs[0]["values"], changed_xs[0]["values"])
        self.assertEqual(xs[0]["values"]["momentum_120_nav_observations"], 0)
        self.assertEqual(xs[0]["values"]["volatility_60_nav_observations_annualized_252"], 0)
        self.assertFalse(xs[0]["strict_PIT_verified"])
        self.assertEqual(summary["fund_group_id"], "shared-portfolio")
        self.assertNotIn("current_fees", xs[0]["values"])
        with self.assertRaises(research.ResearchError):
            research.build_samples(rows, {}, [1], 119, OBSERVED)

    def test_calendar_alignment_pending_and_insufficient_history(self):
        rows = normalized(122)
        rows[121]["date"] = (START + dt.timedelta(days=123)).isoformat()
        xs, labels, _ = research.build_samples(rows, {}, [1, 30], 120, OBSERVED)
        self.assertEqual(xs[0]["feature_window_start_date"], START.isoformat())
        self.assertEqual(labels[0]["end_nav_date"], rows[121]["date"])
        self.assertEqual(labels[0]["alignment_delay_calendar_days"], 2)
        pending = labels[1]
        self.assertEqual(pending["status"], "pending_future_NAV")
        for field in ("end_nav_date", "alignment_delay_calendar_days", "market_return", "terminal_loss", "principal_path_loss", "peak_drawdown"):
            self.assertIsNone(pending[field])
        xs, labels, summary = research.build_samples(normalized(120), {}, [30], 120, OBSERVED)
        self.assertEqual((xs, labels), ([], []))
        self.assertEqual(summary["status"], "insufficient_history")

    def test_shard_rotation_and_exclusive_ownership(self):
        records = normalized(3)
        line_sizes = [len((json.dumps(r, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()) for r in records]
        limit = max(line_sizes)
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            paths = research.write_tables(run, "normalized", CODE, records, limit)
            self.assertEqual(len(paths), 3)
            self.assertEqual(paths[0], "normalized/123456/2020-01-part-000001.jsonl")
            self.assertTrue(all((run / path).stat().st_size <= limit for path in paths))
            self.assertEqual(research.read_table(run, "normalized", CODE), records)
            with self.assertRaises(research.ResearchError):
                research.write_tables(run, "normalized", CODE, records, limit)
            before = set(run.rglob("*"))
            with self.assertRaises(research.ResearchError):
                research.write_tables(run, "features", CODE, [{"code": CODE, "feature_cutoff_nav_date": "2020-01-01", "huge": "x" * 1000}], 20)
            self.assertEqual(before, set(run.rglob("*")))

    def test_strict_table_reader_rejects_corruption_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            paths = research.write_tables(run, "normalized", CODE, normalized(1))
            path = run / paths[0]
            for corrupt in ("{broken\n", '{"code":"123456","date":"2020-01-01","nav":NaN}\n',
                            '{"code":"123456","date":"2020-01-01","nav":1e999}\n', "\n",
                            '{"code":"123456","date":"2020-02-01"}\n'):
                path.write_text(corrupt, encoding="utf-8")
                with self.subTest(corrupt=corrupt), self.assertRaises(research.ResearchError):
                    research.read_table(run, "normalized", CODE)
            for kind, code in (("../outside", CODE), ("normalized", "../../outside")):
                with self.subTest(kind=kind, code=code), self.assertRaises(research.ResearchError):
                    research.write_tables(run, kind, code, [])


if __name__ == "__main__":
    unittest.main()
