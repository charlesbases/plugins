"""批准的安装、失败预算、鲜取与命名空间场景。"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from verify_sc02 import run_case


class NewsPluginTests(unittest.TestCase):
    def test_plugin_add_remove(self):
        report, error = run_case('plugin_add_remove')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))

    def test_all_failed_three_attempts(self):
        report, error = run_case('all_failed_three_attempts')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))

    def test_fresh_invocation(self):
        report, error = run_case('fresh_invocation')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))

    def test_isolation(self):
        report, error = run_case('isolation')
        self.assertIsNone(error, error)
        self.assertTrue(all(case['passed'] for case in report['cases']))
