"""SC03单链执行结果核对；业务合理性和事实依据由正式验证脚本判断。"""
from __future__ import annotations

from investment.research.contracts import ResearchSchemas
from investment.research.references import ResearchResolver


def verify_flow(root, result):
    schemas = ResearchSchemas(('SC03',))
    schemas.validate('investment.research_run_result', result)
    resolver = ResearchResolver(root, schemas, mode='test')
    assert result['status'] == 'completed' and result['downstream_allowed'], result
    incoming = resolver.resolve(result['input_ref'], 'investment.stage_input')
    seal_ref = result['sealed_output_ref']['value']
    seal = resolver.resolve(seal_ref, 'investment.sealed_output')
    candidate = resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
    receipt = resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
    assert incoming['invocation_id'] == result['invocation_id']
    assert all(row['stage_id'] == 'SC03' and row['run_id'] == result['run_id']
               for row in (incoming, seal, candidate, receipt))
    assert candidate['input_refs'] == receipt['input_refs'] == incoming['input_refs']
    assert receipt['candidate_output_ref'] == seal['candidate_output_ref']
    assert receipt['overall_verdict'] in ('passed', 'partial') and receipt['allowed_uses']
    assert receipt['business_result'] in ('usable', 'observation_only')
    resolver.resolve(candidate['payload_ref'], 'investment.sc03.research_assessment')

    stage, parts = resolver.run_parts(seal_ref['uri'])
    assert stage == 'sc03' and parts[1] == result['invocation_id']
    directory = root / 'runs' / stage / parts[0] / parts[1] / parts[2]
    manifest = schemas.document('investment.research_daily_manifest',
                                (root / 'runs/sc03' / parts[0] / 'manifest.json').read_bytes())
    invocation = next(row for row in manifest['invocations'] if row['invocation_id'] == result['invocation_id'])
    assert invocation['state'] == 'completed' and invocation['sealed_output_ref'] == result['sealed_output_ref']
    assert len(invocation['attempts']) == result['attempt_count']
    attempt = invocation['attempts'][-1]
    for role in ('producer', 'validator'):
        exits = list(directory.glob(role + '-*.exit.txt'))
        assert len(exits) == 1 and exits[0].read_text().strip() == '0', role
        assert any(resolver.resolve(ref)['role'] == role for ref in attempt['host_request_refs']), role
    assert len(attempt['host_request_refs']) == len(attempt['host_response_refs'])
    assert len(list(root.glob('flow-*.stdout.txt'))) == 1
    return [result['input_ref'], seal_ref, seal['candidate_output_ref'], seal['validation_receipt_ref'], candidate['payload_ref']]
