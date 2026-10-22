"""SC03单链单测：复用保存的上游示例，实际执行本链流程和验证。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / 'src'), str(PLUGIN)]

from investment.contracts import ContractError, canonical, now, require
from investment.chains.news_assessment.entry import prepare_input
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.research.references import ResearchResolver
from investment.runtime.paths import validate_input_path
from investment.runtime.runs import atomic


def prepare(input_example):
    example = Path(input_example).resolve()
    source_root = guard_namespace(example.parents[2], 'test')
    require(example == source_root / 'examples/sc03/input.json',
            'RS01', '/input_example', 'Use the saved SC03 input example')
    schemas = ResearchSchemas(('SC03',))
    template = schemas.document('investment.stage_input', example.read_bytes())
    require(template['stage_id'] == 'SC03', 'RS03', '/stage_id', 'SC03 input example required')
    source = ResearchResolver(source_root, schemas, mode='test')
    roles = {row['role']: row['ref'] for row in template['input_refs']}
    protocol = source.resolve(roles['assessment_protocol_ref'], 'investment.sc03.assessment_protocol')
    parameters = source.resolve(template['parameters_ref'], 'investment.sc03.assessment_parameters')

    root = guard_namespace(Path.home() / '.investment/test' / ('sc03-' + uuid.uuid4().hex), 'test')
    root.mkdir()
    # 只恢复已完成上游的记录及引用材料，不复制SC03旧候选或触发上游脚本。
    for stage in ('sc01', 'sc02'):
        directory = source_root / 'runs' / stage
        require(directory.resolve().is_relative_to(source_root / 'runs'),
                'RS01', '/input_example', 'Example stage directory escapes source root')
        shutil.copytree(directory, root / 'runs' / stage)
    invocation_id = 'sc03-' + uuid.uuid4().hex
    submitted = prepare_input(root, mode='test', run_id=template['run_id'], invocation_id=invocation_id,
        evidence_corpus_ref=roles['evidence_corpus_ref'], scope_ref=template['scope_ref'],
        task_questions=[row['question'] for row in protocol['questions'] if row['basis'] == 'research_scope'],
        text_window_codepoints=parameters['text_window_codepoints'],
        decision_at=template['decision_at'], information_cutoff_at=template['information_cutoff_at'],
        interpretation_mode=protocol['interpretation_mode'],
        account_risk_objects_ref=roles.get('account_risk_objects_ref'),
        account_questions=[row['question'] for row in protocol['questions'] if row['basis'] == 'account_risk'])
    resolver = ResearchResolver(root, schemas, mode='test')
    input_ref = resolver.path_ref(submitted, 'investment.stage_input')
    case = {'case_id': 'SC03-single-chain', 'phase': 'flow',
        'entry': 'run_sc03 -> process_sc03 -> validate_sc03',
        'preconditions': 'Saved upstream records imported from ' + str(source_root) + '; SC01/SC02 are not executed in this test.',
        'trigger': 'Execute SC03 with saved input example ' + str(example),
        'expected': 'Current process and independent validation execute; completion follows the registered run and handoff result.',
        'actual': 'Current SC03 input prepared; execution pending.', 'result': 'pending', 'evidence_refs': [input_ref]}
    report = {'schema_version': 1, 'stage_id': 'SC03', 'test_run_id': root.name,
              'started_at': now(), 'updated_at': now(), 'cases': [case]}
    control = {'root': str(root), 'input_example': str(example),
        'run_id': template['run_id'], 'invocation_id': invocation_id,
        'submitted_path': str(submitted), 'report': report}
    path = root / 'sc03-verification.json'
    atomic(path, canonical(control), immutable=True)
    print("SC03 single-call test started: pid=" + str(os.getpid()) + " root=" + str(root), file=sys.stderr, flush=True)
    return execute(path)



def save_example(root, resolver, result, source_example):
    """只保存本次真实封存业务输出及其原始引用，不额外作业务验收。"""
    seal_ref = result['sealed_output_ref']['value']
    seal = resolver.resolve(seal_ref, 'investment.sealed_output')
    candidate = resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
    receipt = resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
    folder = root / 'examples/sc03'
    folder.mkdir(parents=True)
    atomic(folder / 'input.json', resolver.resolve(result['input_ref'], binding_only=True), immutable=True)
    atomic(folder / 'output.json', resolver.resolve(candidate['payload_ref'], binding_only=True), immutable=True)
    record = {'run_id': result['run_id'], 'invocation_id': result['invocation_id'],
        'source_input_example': source_example, 'input_ref': result['input_ref'],
        'output_ref': candidate['payload_ref'], 'candidate_ref': seal['candidate_output_ref'],
        'receipt_ref': seal['validation_receipt_ref'], 'sealed_output_ref': seal_ref,
        'allowed_uses': receipt['allowed_uses'], 'business_result': receipt['business_result']}
    atomic(folder / 'record.json', canonical(record), immutable=True)


def execute(state_path):
    path = Path(state_path).resolve()
    root = guard_namespace(path.parent, 'test')
    require(path.name == 'sc03-verification.json', 'RS01', '/state', 'SC03 test state required')
    control = json.loads(validate_input_path(root, path).read_bytes())
    require(Path(control['root']).resolve() == root, 'RS01', '/root', 'Test state belongs to another batch')
    schemas = ResearchSchemas(('SC03',))
    resolver = ResearchResolver(root, schemas, mode='test')
    case = control['report']['cases'][0]

    def persist():
        control['report']['updated_at'] = now()
        schemas.validate('investment.sc03.verification_report', control['report'])
        atomic(path, canonical(control))
        atomic(root / 'verification-report.json', canonical(control['report']))

    command = [sys.executable, '-B', str(PLUGIN / 'scripts/run_sc03.py'), '--root', str(root),
        '--mode', 'test', '--input', control['submitted_path'], '--run-id', control['run_id'],
        '--invocation-id', control['invocation_id']]
    try:
        index = len(list(root.glob('flow-*.stdout.txt'))) + 1
        process = subprocess.run(command, cwd=PLUGIN, capture_output=True,
            env=dict(os.environ, PYTHONPATH=str(PLUGIN / 'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))
        atomic(root / f'flow-{index:03}.stdout.txt', process.stdout, immutable=True)
        atomic(root / f'flow-{index:03}.stderr.txt', process.stderr, immutable=True)
        require(process.returncode in (0, 1), 'A14', '/process', process.stderr.decode('utf-8', errors='replace'))
        result = schemas.document('investment.research_run_result', process.stdout)
        require((result['run_id'], result['invocation_id']) == (control['run_id'], control['invocation_id']),
                'A14', '/invocation_id', 'Run result differs from current test invocation')
        case['evidence_refs'] = [result['input_ref']] + result['failure_refs']
        require(result['status'] != 'host_work_required', 'A14', '/status', 'Single-call SC03 cannot await manual host work')
        if result['status'] == 'failed':
            case.update(result='failed', actual=f"Actual SC03 failed after {result['attempt_count']} persisted attempts; no current sealed output.")
            persist()
            print(canonical({'report_path': str(root / 'verification-report.json'), 'status': 'failed'}).decode())
            return 1
        from tests.test_sc03_flow import verify_flow
        case.update(result='passed', actual='Current SC03 process and independent validation completed with matching current run artifacts; upstream examples were reused.',
                    evidence_refs=verify_flow(root, result))
        save_example(root, resolver, result, control['input_example'])
        persist()
        print(canonical({'report_path': str(root / 'verification-report.json'), 'results': ['passed']}).decode())
        return 0
    except (AssertionError, ContractError, ValueError, OSError) as error:
        case.update(result='failed', actual=str(error) or type(error).__name__)
        persist()
        print(canonical({'report_path': str(root / 'verification-report.json'), 'status': 'failed',
                         'reason': str(error) or type(error).__name__}).decode())
        return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input-example', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(prepare(args.input_example))
