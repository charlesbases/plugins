"""真实阶段联调选择器；缺实际输入/宿主响应时明确跳过，不制造成功stage。"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from investment.contracts import is_known
from investment.research.contracts import guard_namespace
from investment.research.controller import ResearchController, _manifest


class ResearchRuntimeTests(unittest.TestCase):
    def context(self, selector):
        prefix = 'INVESTMENT_RESEARCH_' + selector.upper() + '_'
        keys = ('ROOT', 'STAGE', 'INPUT', 'RUN_ID', 'INVOCATION_ID')
        if not all(os.environ.get(prefix + key) for key in keys):
            self.skipTest('Requires actual current stage input and persisted host work for ' + selector)
        selected = {key: os.environ[prefix + key] for key in keys}
        root = guard_namespace(Path(selected['ROOT']), 'test')
        input_path = Path(selected['INPUT']).resolve()
        self.assertTrue(input_path.is_relative_to(root))
        controller = ResearchController(root, mode='test', stage_id=selected['STAGE'])
        return controller, input_path, selected

    def test_three_attempts(self):
        """真实链已发生连续失败后重入，不得恢复成新预算。"""
        controller, path, selected = self.context('three_attempts')
        result = controller.run(path, run_id=selected['RUN_ID'], invocation_id=selected['INVOCATION_ID'])
        if result['status'] == 'host_work_required':
            self.skipTest('Actual host work is still pending; preserve same attempt and dispatch through Codex')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['attempt_count'], 3)
        self.assertFalse(result['downstream_allowed'])
        self.assertEqual(len(result['failure_refs']), 3)
        failures = [controller.resolver.resolve(ref, 'investment.research_failure') for ref in result['failure_refs']]
        self.assertEqual([item['attempt_no'] for item in failures], [1, 2, 3])
        self.assertTrue(all(item['reason'] for item in failures))
        again = controller.run(path, run_id=selected['RUN_ID'], invocation_id=selected['INVOCATION_ID'])
        self.assertEqual(again, result)

    def test_process_then_validation(self):
        """实际本轮运行完成后核对不同进程日志、当前候选/凭证与封存绑定。"""
        controller, path, selected = self.context('process_then_validation')
        manifest_path = path.parent.parent / 'manifest.json'
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_bytes())
            matches = [item for item in previous['invocations'] if item['invocation_id'] == selected['INVOCATION_ID']]
            self.assertTrue(not matches or matches[0]['state'] not in ('completed', 'failed'),
                            'Select an actual unfinished invocation; archived completion is not current execution')
        result = controller.run(path, run_id=selected['RUN_ID'], invocation_id=selected['INVOCATION_ID'])
        if result['status'] == 'host_work_required':
            self.skipTest('Actual host work is still pending; this is not a stage success')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['downstream_allowed'])
        seal = controller.resolver.resolve(result['sealed_output_ref']['value'], 'investment.sealed_output')
        candidate = controller.resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
        receipt = controller.resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
        self.assertEqual(receipt['candidate_output_ref'], seal['candidate_output_ref'])
        self.assertEqual(candidate['run_id'], selected['RUN_ID'])
        self.assertEqual(receipt['validator_id'], selected['STAGE'].lower() + '-validator-v1')
        _, parts = controller.resolver.run_parts(result['input_ref']['uri'])
        manifest = _manifest(controller.resolver, selected['STAGE'], parts[0])
        invocation = next(item for item in manifest['invocations'] if item['invocation_id'] == selected['INVOCATION_ID'])
        attempt = invocation['attempts'][-1]
        self.assertEqual(attempt['state'], 'completed')
        self.assertTrue(is_known(attempt['candidate_ref']) and is_known(attempt['receipt_ref']))
        directory = path.parent / f"attempt-{attempt['attempt_no']:02}"
        produced = sorted(directory.glob('producer-*.exit.txt'))
        validated = sorted(directory.glob('validator-*.exit.txt'))
        self.assertTrue(produced and validated)
        self.assertEqual(produced[-1].read_text(), '0')
        self.assertEqual(validated[-1].read_text(), '0')
        self.assertLessEqual((directory / 'candidate.json').stat().st_mtime_ns, validated[-1].stat().st_mtime_ns)


if __name__ == '__main__':
    unittest.main()
