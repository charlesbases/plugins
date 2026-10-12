"""共享协议校准：真实可达的入口隔离、原文引用和宿主返回格式边界。"""
from __future__ import annotations

import copy
import os
import unittest
import uuid
from pathlib import Path

from investment.contracts import ContractError, canonical, known, now, unknown
from investment.research.contracts import ResearchSchemas, StageContext, guard_namespace
from investment.research.controller import ResearchController, _upstream_binding
from investment.chains.news.contracts import parse_news
from investment.research.host import _strict_model_json, validate_host_response
from investment.research.references import ResearchResolver
from investment.runtime.runs import atomic


class ResearchContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = guard_namespace(Path.home() / '.investment' / 'test' / ('research-contracts-' + uuid.uuid4().hex), 'test')
        cls.schemas = ResearchSchemas()
        cls.resolver = ResearchResolver(cls.root, cls.schemas, mode='test')
        cls.base = cls.root / 'runs' / 'sc03' / '20261019' / 'transport-calibration'

    def test_namespace(self):
        """用户可传错mode/root；拒绝在任何创建或读取之前发生。"""
        production = Path.home() / '.investment'
        for root, mode in ((production, 'test'), (production / 'test', 'test'),
                           (self.root, 'formal'), (self.root, 'invalid')):
            with self.subTest(root=root, mode=mode), self.assertRaises(ContractError):
                guard_namespace(root, mode)
            with self.subTest(controller_root=root, mode=mode), self.assertRaises(ContractError):
                ResearchController(root, mode=mode, stage_id='SC03')
        self.assertEqual(guard_namespace(self.root, 'test'), self.root)

    def test_reference_binding(self):
        """宿主真实文本可作为原文，但不能冒称JSON、错摘要或越根。"""
        path = self.base / 'attempt-01' / 'source.txt'
        atomic(path, '实际原文：模拟来源仅用于协议校准。\n'.encode('utf-8'), immutable=True)
        ref = self.resolver.path_ref(path, 'investment.research_text')
        self.assertEqual(self.resolver.resolve(ref), '实际原文：模拟来源仅用于协议校准。\n')
        for field, value in (('media_type', 'application/json'), ('sha256', '0' * 64),
                             ('byte_size', ref['byte_size'] + 1), ('uri', ref['uri'].replace('/source.txt', '/../source.txt'))):
            altered = dict(ref, **{field: value})
            with self.subTest(field=field), self.assertRaises(ContractError):
                self.resolver.resolve(altered)
        restricted = ResearchResolver(self.root, self.schemas, mode='test', allowed_refs=())
        with self.assertRaises(ContractError):
            restricted.resolve(ref)
        path.write_bytes(b'changed actual bytes')
        with self.assertRaises(ContractError):
            self.resolver.resolve(ref)

    def test_host_response_binding(self):
        """模型可返回重复键/浮点/非JSON；不得经宽松解析变为合法结果。"""
        for raw in ('{"x":1,"x":2}', '{"estimate":0.1}', '{"estimate":NaN}', 'tool unavailable', '{"count":9007199254740992}'):
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                _strict_model_json(raw)
        self.assertEqual(_strict_model_json(' { "estimate": "0.1" } '), {'estimate': '0.1'})

    def test_reference_grants(self):
        """冻结Schema包确有子引用；未授权时不可凭嵌套引用读取字节。"""
        resources = []
        for rid, document in sorted(self.schemas.documents.items()):
            path = self.base / ('schema-' + rid.rsplit(':', 1)[-1] + '.json')
            atomic(path, canonical(document), immutable=True)
            resources.append({'resource_id': rid, 'installed_filename': self.schemas.installed_files[rid],
                              'installed_sha256': self.schemas.installed_hashes[rid],
                              'document_ref': self.resolver.path_ref(path, 'investment.research_schema_document')})
        bundle = {'schema_version': 1, 'root_schema_id': 'investment.host_request',
                  'root_target': self.schemas.targets['investment.host_request']['target'], 'resources': resources}
        path = self.base / 'schema-bundle.json'
        atomic(path, canonical(bundle), immutable=True)
        reference = self.resolver.path_ref(path, 'investment.research_schema_bundle')
        restricted = ResearchResolver(self.root, self.schemas, mode='test', allowed_refs=(reference,))
        restricted.resolve(reference)
        with self.assertRaises(ContractError):
            restricted.resolve(resources[0]['document_ref'])
        granted = ResearchResolver(self.root, self.schemas, mode='test', allowed_refs=(reference,),
                                   reference_grants=({'schema_id': 'investment.research_schema_bundle', 'reference_paths': ['/resources/*/document_ref']},))
        granted.resolve(reference)
        self.assertEqual(granted.resolve(resources[0]['document_ref'])['$id'], resources[0]['resource_id'])

    def test_funding_plan(self):
        """用户明确可投入零与未知不同，未来零流水和重复ID必须拒绝。"""
        path = self.base / 'funding-source.txt'
        atomic(path, '模拟用户输入：本轮可投入0元，不追加投入。'.encode('utf-8'), immutable=True)
        source_ref = self.resolver.path_ref(path, 'investment.research_text')
        stamp = now()
        plan = {'schema_version': 1, 'plan_id': 'funding:zero', 'currency': 'CNY', 'source_type': 'synthetic_fixture',
                'available_amount': known('0'), 'declared_at': known(stamp), 'recorded_at': stamp,
                'source_ref': source_ref, 'future_contributions': known([])}
        plan_path = self.base / 'funding.json'
        atomic(plan_path, canonical(plan), immutable=True)
        self.assertEqual(self.resolver.resolve(self.resolver.path_ref(plan_path, 'investment.funding_plan'))['available_amount'], known('0'))
        unspecified = dict(plan, available_amount=unknown('User has not stated an amount'),
                           declared_at=unknown('Declaration time unavailable'), future_contributions=unknown('User has not stated a future plan'))
        self.schemas.validate('investment.funding_plan', unspecified)
        contribution = {'contribution_id': 'funding:next', 'contribution_date': '2026-11-01', 'amount': '0', 'source_ref': source_ref}
        with self.assertRaises(ContractError):
            self.schemas.validate('investment.funding_plan', dict(plan, future_contributions=known([contribution])))
        contribution['amount'] = '100'
        with self.assertRaisesRegex(ContractError, 'Duplicate declared funding contribution'):
            self.schemas.validate('investment.funding_plan', dict(plan, future_contributions=known([contribution, contribution])))

    def test_actual_pending_response_replay(self):
        """有真实待办请求时，复用同一响应但改nonce必须在任何业务验收前拒绝。"""
        request_path = os.environ.get('INVESTMENT_RESEARCH_HOST_REQUEST')
        response_path = os.environ.get('INVESTMENT_RESEARCH_HOST_RESPONSE')
        if not request_path or not response_path:
            self.skipTest('Requires current actual stage HostRequest and HostResponse; no fabricated stage success')
        schemas = ResearchSchemas(tuple(f'SC{n:02}' for n in range(3, int(os.environ['INVESTMENT_RESEARCH_STAGE'][2:]) + 1)))
        root = guard_namespace(Path(os.environ['INVESTMENT_RESEARCH_ROOT']), 'test')
        resolver = ResearchResolver(root, schemas, mode='test')
        request = resolver.resolve(resolver.path_ref(Path(request_path), 'investment.host_request'))
        response = resolver.resolve(resolver.path_ref(Path(response_path), 'investment.host_response'))
        response = copy.deepcopy(response)
        response['nonce'] = ('0' if request['nonce'][0] != '0' else '1') + request['nonce'][1:]
        with self.assertRaisesRegex(ContractError, 'does not bind current request'):
            validate_host_response(request, response, resolver)

    def test_upstream_receipt(self):
        """真实SC02验收可读取和引用，凭证中的候选/证据不能扩权。"""
        selected = os.environ.get('INVESTMENT_RESEARCH_UPSTREAM_ROOT')
        if not selected:
            self.skipTest('Requires actual SC01/SC02 handoff calibration artifacts, not a fabricated seal')
        root = guard_namespace(Path(selected), 'test')
        report = parse_news((root / 'research-handoff-calibration.json').read_bytes())
        schemas = ResearchSchemas()
        resolver = ResearchResolver(root, schemas, mode='test')
        seal = resolver.resolve(report['news_seal_ref'], 'investment.sealed_output')
        candidate = resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
        parts = resolver.legacy.run_parts(report['news_seal_ref']['uri'])
        input_path = root / 'runs' / 'sc02' / parts[0] / parts[1] / 'input.json'
        input_ref = resolver.path_ref(input_path, 'investment.stage_input')
        original = resolver.resolve(input_ref, 'investment.stage_input')
        source = {'kind': 'upstream_output', 'stage_id': 'SC02', 'required_uses': ['evidence_review']}
        binding = _upstream_binding(resolver, candidate['payload_ref'], source, original)
        role = 'evidence_corpus_ref'
        scoped = ResearchResolver(root, schemas, mode='test', allowed_refs=(candidate['payload_ref'],),
                                  reference_grants=({'schema_id': 'investment.validation_receipt',
                                                    'reference_paths': ['/candidate_output_ref', '/checks/*/evidence_refs/*']},))
        ctx = StageContext(root=root, mode='test', stage_id='SC03', run_id=original['run_id'],
                           invocation_id='receipt-read-calibration', attempt_no=1, role='validator', schemas=schemas,
                           resolver=scoped, input=original, input_ref=input_ref, method=schemas.research_method,
                           method_ref=candidate['method_ref'], definition={'input_roles': [{'role': role, 'source': source}]},
                           attempt_dir=root / 'runs' / 'sc03' / parts[0] / 'receipt-read-calibration' / 'attempt-01',
                           upstream_receipts={role: binding})
        allowed = copy.deepcopy(scoped.allowed)
        reference, receipt = ctx.upstream_receipt(role)
        self.assertEqual(reference, binding[0])
        self.assertEqual(receipt, binding[1])
        self.assertEqual(scoped.resolve(reference), binding[1])
        self.assertEqual(scoped.allowed, allowed)
        receipt['allowed_uses'].clear()
        self.assertEqual(ctx.upstream_receipt(role)[1]['allowed_uses'], binding[1]['allowed_uses'])
        with self.assertRaises(ContractError):
            scoped.resolve(binding[1]['candidate_output_ref'])
        for check in binding[1]['checks']:
            for evidence in check['evidence_refs']:
                if evidence['uri'] not in allowed:
                    with self.assertRaises(ContractError):
                        scoped.resolve(evidence)
        with self.assertRaises(ContractError):
            ctx.upstream_receipt('unregistered_role')


if __name__ == '__main__':
    unittest.main()
