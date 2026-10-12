"""真实阶段联调选择器；缺实际输入/宿主响应时明确跳过，不制造成功stage。"""
from __future__ import annotations

import json
import os
import unittest
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path

from investment.contracts import ContractError, is_known, na
from investment.research.contracts import HostWorkItem, HostWorkSummary, ResearchSchemas, StageContext, guard_namespace
from investment.research.controller import ResearchController, _manifest, _receipt_failure, load_context
from investment.research.host import _project_host_work_summary
from investment.research.references import ResearchResolver


class ReceiptFailureTests(unittest.TestCase):
    def test_current_failure_precedes_inherited_gaps(self):
        """只读实际早退收据的字段投影，校准错误选择，不代替阶段执行。"""
        source = os.environ.get('INVESTMENT_RESEARCH_RECEIPT_FAILURE_SOURCE')
        if not source:
            self.skipTest('Requires actual rejected validator stdout for receipt failure calibration')
        path = Path(source).resolve()
        self.assertTrue(path.is_relative_to(Path.home() / '.investment' / 'test'))
        receipt = json.loads(path.read_bytes())
        projection = {
            'checks': [{key: check[key] for key in ('rule_id', 'verdict', 'reason')} for check in receipt['checks']],
            'blockers': [{key: blocker[key] for key in ('rule_id', 'field_or_object', 'reason')} for blocker in receipt['blockers']],
        }
        self.assertEqual(projection['blockers'][0]['rule_id'], 'A11')
        self.assertEqual(projection['checks'][3]['verdict'], 'insufficient_evidence')
        self.assertEqual(projection['checks'][4]['rule_id'], 'U05')
        self.assertEqual(projection['checks'][4]['verdict'], 'failed')
        error = _receipt_failure(projection)
        self.assertEqual(error.rule, 'U05')
        self.assertEqual(error.pointer, '/checks/U05')
        self.assertEqual(str(error), 'Unknown layout was silently treated as fully parsed')
        self.assertEqual(error.code, 'unsupported_output')


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


class HostWorkSummaryTests(unittest.TestCase):
    """仅重放真实宿主记录的投影校准；内存索引不是Controller manifest。"""

    @classmethod
    def setUpClass(cls):
        selected = os.environ.get('INVESTMENT_RESEARCH_HOST_SUMMARY_CASES')
        if not selected:
            raise unittest.SkipTest('Requires an explicit test-batch cases file for actual host records')
        path = Path(selected).resolve()
        guard_namespace(path.parent, 'test')
        cls.cases = json.loads(path.read_bytes())

    def projection(self, case):
        root = guard_namespace(Path(case['root']), 'test')
        resolver = ResearchResolver(root, ResearchSchemas(('SC03',)), mode='test')
        paths = sorted((root / 'runs' / 'sc03').glob(f"*/*/attempt-{case['attempt_no']:02}/host-*-request.json"))
        self.assertTrue(paths, 'Select actual request artifacts, not a synthetic successful stage')
        request = resolver.resolve(resolver.path_ref(paths[0], 'investment.host_request'))
        base = paths[0].parent.parent
        steps = []
        for step in resolver.schemas.stage_methods['SC03']['runtime']['host_steps']:
            steps.append({key: step[key] for key in ('step_id', 'role', 'output_schema_id', 'required')})
            steps[-1].update(prompt_ref=resolver.path_ref(base / ('prompt-' + step['step_id'] + '.md'), 'investment.research_markdown'),
                             output_schema_ref=resolver.path_ref(base / ('schema-bundle-' + step['step_id'] + '.json'), 'investment.research_schema_bundle'))
        attempts = []
        for number in range(1, case['attempt_no'] + 1):
            requests, responses, candidate = [], [], na('candidate_not_produced')
            for path in sorted((base / f'attempt-{number:02}').glob('host-*-request.json')):
                reference = resolver.path_ref(path, 'investment.host_request')
                actual = resolver.resolve(reference)
                requests.append(reference)
                if actual['role'] == 'validator':
                    candidate = actual['candidate_ref']
                response_path = path.with_name(path.name.removesuffix('-request.json') + '-response.json')
                if response_path.exists():
                    responses.append(resolver.path_ref(response_path, 'investment.host_response'))
            self.assertTrue(requests, 'Every indexed attempt must have actual request artifacts')
            attempts.append({'attempt_no': number, 'candidate_ref': candidate,
                             'host_request_refs': requests, 'host_response_refs': responses})
        # 只为投影器整理实际记录；不写manifest、完成状态、Seal或HostEvent。
        index = {'run_id': request['run_id'], 'invocation_id': request['invocation_id'],
                 'frozen_input_ref': request['input_ref'], 'method_ref': request['method_ref'],
                 'host_steps': steps, 'attempts': attempts}
        return _project_host_work_summary(resolver, 'SC03', index, attempts[-1]), resolver, request, index, base

    def test_actual_projection_counts_and_immutable_headers(self):
        for case in self.cases:
            with self.subTest(root=case['root'], attempt=case['attempt_no']):
                summary, _, _, _, _ = self.projection(case)
                self.assertEqual(summary.registered_count(), case['registered'])
                for role in ('producer', 'validator'):
                    self.assertEqual(summary.completed_count(role), case['completed'][role])
                    self.assertEqual(summary.pending_count(role), case['pending'][role])
                    self.assertEqual(summary.failed_count(role), 0)
                self.assertEqual({field.name for field in fields(summary)},
                                 {'run_id', 'stage_id', 'invocation_id', 'attempt_no', 'input_sha256',
                                  'method_sha256', 'source_fingerprint', 'items'})
                for item in summary.items:
                    self.assertTrue(summary.contains(item.role, item.step_id, item.work_key))
                    self.assertEqual(set(asdict(item)), {'role', 'step_id', 'work_key', 'request_id',
                                                        'request_sha256', 'state', 'response_sha256'})
                    with self.assertRaises(FrozenInstanceError):
                        item.state = 'failed'
                with self.assertRaises(FrozenInstanceError):
                    summary.attempt_no = 3

    def test_runtime_constructor_contract(self):
        summary, _, _, _, _ = self.projection(self.cases[0])
        item = summary.items[0]
        for values in ({'role': None}, {'role': 'operator'}, {'step_id': '../private'}, {'work_key': 1},
                       {'request_id': ''}, {'request_sha256': 'A' * 64}, {'state': 'unknown'},
                       {'state': 'completed', 'response_sha256': None}, {'state': 'failed', 'response_sha256': None},
                       {'state': 'requested', 'response_sha256': 'a' * 64}):
            with self.subTest(item=values), self.assertRaises(ContractError):
                replace(item, **values)
        for values in ({'attempt_no': True}, {'attempt_no': 0}, {'attempt_no': 4}, {'stage_id': 'SC02'},
                       {'run_id': '../run'}, {'invocation_id': ''}, {'input_sha256': None},
                       {'method_sha256': 'bad'}, {'source_fingerprint': ''}, {'items': list(summary.items)},
                       {'items': (asdict(item),)}, {'items': (item, item)}):
            with self.subTest(summary=values), self.assertRaises(ContractError):
                replace(summary, **values)
        with self.assertRaises(TypeError):
            HostWorkItem(**dict(asdict(item), private_output={}))
        for method in (summary.registered_count, summary.completed_count, summary.failed_count, summary.pending_count):
            with self.assertRaises(ContractError):
                method('operator')
        with self.assertRaises(ContractError):
            summary.contains(None, item.step_id, item.work_key)

    def test_context_requires_binding_and_preserves_role_permissions(self):
        summary, internal, request, index, base = self.projection(self.cases[0])
        scoped = ResearchResolver(internal.root, internal.schemas, mode='test',
                                  allowed_refs=(request['input_ref'], request['method_ref']), reference_grants=())
        ctx = StageContext(root=internal.root, mode='test', stage_id='SC03', run_id=summary.run_id,
                           invocation_id=summary.invocation_id, attempt_no=summary.attempt_no, role='validator',
                           schemas=internal.schemas, resolver=scoped, input=internal.resolve(request['input_ref']),
                           input_ref=request['input_ref'], method=internal.schemas.stage_methods['SC03'],
                           method_ref=request['method_ref'], definition=internal.schemas.definition('SC03'),
                           attempt_dir=base / f'attempt-{summary.attempt_no:02}')
        with self.assertRaises(ContractError) as missing:
            ctx.host_work_summary()
        self.assertEqual(missing.exception.code, 'missing_evidence')
        allowed_before = dict(scoped.allowed)
        # 这里只校准Context能力方法；provider输入仍是已有真实记录，不声称load_context运行。
        ctx._host_work_provider = lambda: _project_host_work_summary(internal, 'SC03', index, index['attempts'][-1])
        self.assertEqual(ctx.host_work_summary(), summary)
        self.assertEqual(scoped.allowed, allowed_before)
        self.assertEqual(scoped.reference_grants, {})
        with self.assertRaises(ContractError):
            scoped.resolve(index['attempts'][-1]['host_request_refs'][0])
        ctx.attempt_no = 3 if summary.attempt_no != 3 else 2
        with self.assertRaises(ContractError) as mismatch:
            ctx.host_work_summary()
        self.assertEqual(mismatch.exception.code, 'identity_mismatch')


class HostWorkFactoryTests(unittest.TestCase):
    def test_current_controller_context(self):
        prefix = 'INVESTMENT_RESEARCH_HOST_SUMMARY_FACTORY_'
        names = ('ROOT', 'STAGE', 'INPUT', 'RUN_ID', 'INVOCATION_ID', 'ATTEMPT', 'ROLE')
        if not all(os.environ.get(prefix + name) for name in names):
            self.skipTest('Requires a real active controller manifest; projection calibration cannot substitute')
        values = {name: os.environ[prefix + name] for name in names}
        candidate = os.environ.get(prefix + 'CANDIDATE')
        ctx = load_context(values['ROOT'], mode='test', stage_id=values['STAGE'], input_path=values['INPUT'],
                           run_id=values['RUN_ID'], invocation_id=values['INVOCATION_ID'], attempt_no=int(values['ATTEMPT']),
                           role=values['ROLE'], candidate_path=candidate)
        allowed_before = dict(ctx.resolver.allowed)
        summary = ctx.host_work_summary()
        self.assertIs(type(summary), HostWorkSummary)
        self.assertEqual((summary.run_id, summary.invocation_id, summary.attempt_no),
                         (ctx.run_id, ctx.invocation_id, ctx.attempt_no))
        self.assertEqual(ctx.resolver.allowed, allowed_before)


if __name__ == '__main__':
    unittest.main()
