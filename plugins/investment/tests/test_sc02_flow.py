"""批准的多源、等待、分页与未知事实场景。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_sc02 import run_case


class NewsFlowTests(unittest.TestCase):
    def test_multi_source(self):
        report, error = run_case('multi_source')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))

    def test_wait_all_partial(self):
        report, error = run_case('wait_all_partial')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))

    def test_pagination_duplicates(self):
        report, error = run_case('pagination_duplicates')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))

    def test_unknown_time_or_layout(self):
        report, error = run_case('unknown_time_or_layout')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))
