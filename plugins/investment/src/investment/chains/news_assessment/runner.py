"""SC03单次调用控制器：自动绑定子session证据，不向调用者暴露人工续行。"""
from __future__ import annotations

import sys

from investment.contracts import ContractError, canonical, require
from investment.research.controller import ResearchController, _parser, _save


def _primary_failure(failures, resolver):
    """保留全部失败，优先返回实际原因，连带取消不能遮盖触发它的错误。"""
    for error in failures:
        reference = getattr(error, 'host_response_ref', None)
        if reference is None:
            return error
        response = resolver.resolve(reference, 'investment.host_response')
        if response['status'] != 'failed' or response['error']['value']['code'] != 'cancelled':
            return error
    return failures[0]


class SC03Controller(ResearchController):
    """将流程进程自动完成的模型调用纳入同一次运行审计。"""

    def _invoke(self, base, invocation, attempt, role, method):
        process = super()._invoke(base, invocation, attempt, role, method)
        directory = base / f"attempt-{attempt['attempt_no']:02}"
        filenames = ('assessment-source-work-plan.json', 'assessment-work-plan.json') if role == 'producer' else ('validation-work-plan.json',)
        items = {}
        for filename in filenames:
            path = directory / filename
            if not path.exists():
                continue
            plan = self.schemas.document('investment.sc03.assessment_work_plan', path.read_bytes())
            require(plan['input_ref'] == invocation['frozen_input_ref'], 'RS03', '/work_plan', 'Work plan input differs')
            for item in plan['items']:
                items[(item['step_id'], item['work_key'])] = item
        manifest = self.resolver.schemas.document('investment.research_daily_manifest',
            (base.parent / 'manifest.json').read_bytes())
        # 使用run持有的同一invocation对象；保存时替换磁盘快照中的对应条目。
        manifest['invocations'] = [invocation if row['invocation_id'] == invocation['invocation_id'] else row
                                   for row in manifest['invocations']]
        failures = []
        for step, key in items:
            path = directory / ('host-' + step + '-' + key + '-request.json')
            if not path.exists():
                continue
            request = self.schemas.document('investment.host_request', path.read_bytes())
            reference = self._request(request, invocation, attempt, role, base)
            require(reference not in attempt['host_request_refs'], 'RS05', '/request', 'Model request was already bound')
            attempt['host_request_refs'].append(reference)
            _save(self.resolver, manifest)
            try:
                response_ref = self._response(reference, directory)
                require(response_ref is not None, 'RS05', '/response', 'Automatic model execution left no final response')
                attempt['host_response_refs'].append(response_ref)
            except ContractError as error:
                failures.append(error)
            _save(self.resolver, manifest)
        for error in failures:
            reference = getattr(error, 'host_response_ref', None)
            if reference is not None and reference not in attempt['host_response_refs']:
                attempt['host_response_refs'].append(reference)
        _save(self.resolver, manifest)
        if failures:
            raise _primary_failure(failures, self.resolver)
        require(process.returncode != 3, 'RS06', '/process', 'SC03 cannot return manual host work', 'execution_failed')
        return process

    def _result(self, invocation):
        require(invocation['state'] != 'host_work_required', 'RS06', '/state',
                'Manual SC03 continuation is not supported', 'execution_failed')
        return super()._result(invocation)


def run_cli(argv=None):
    """只返回本次完整运行的最终结果。"""
    args = _parser().parse_args(argv)
    try:
        result = SC03Controller(args.root, mode=args.mode, stage_id='SC03').run(
            args.input, run_id=args.run_id, invocation_id=args.invocation_id)
    except (ContractError, OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical(result))
    return 0 if result['status'] == 'completed' else 1
