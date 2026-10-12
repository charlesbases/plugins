"""SC03真实流程场景：合法上游、正式Controller与隔离独立预期。"""
from __future__ import annotations

import argparse
from datetime import timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.request import urlopen

sys.dont_write_bytecode = True
PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'src'))

from investment.contracts import canonical, digest, instant, is_known, known, now, require, unknown
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.research.controller import _upstream_binding
from investment.research.references import ResearchResolver
from investment.runtime.runs import atomic
from verify_sc02 import Scenario

CASES = ('supported_fact', 'source_republication', 'conflicting_metric', 'unsupported_mechanism', 'empty')


def case_material(selector):
    policy = ('On September 18, 2024, the Committee decided to lower the target range for the federal funds rate '
              'by 1/2 percentage point to 4-3/4 to 5 percent. The Committee will carefully assess incoming data, '
              'the evolving outlook, and the balance of risks.')
    initial = ('Agency Alpha reported that real GDP increased at an annual rate of 2.8 percent in the second '
               'quarter of 2024. This is the advance estimate released on July 25, 2024.')
    if selector in ('supported_fact', 'unsupported_mechanism'):
        rows = [('policy_origin', 'Policy committee statement', policy)]
        question = '原文报告了什么政策调整，其适用范围和未知是什么？'
        if selector == 'unsupported_mechanism':
            question = '审查断言：这份政策声明能够证明所有科技基金下一交易日必然上涨5%；保留源事实并列出缺失依据。'
        expected = {'require_source_fact': True, 'forbid_cross_checked': True}
        if selector == 'supported_fact':
            # 事前独立算术：1/2=5/10，4+3/4=475/100；单位不随编码改变。
            expected['exact_numeric_observations'] = [
                {'value': '0.5', 'literal': '1/2', 'source_unit': 'percentage point',
                 'unit_labels': ['percentage_point', 'percentage point', 'percentage points'], 'source_id': 'policy_origin'},
                {'value': '4.75', 'literal': '4-3/4', 'source_unit': 'percent',
                 'unit_labels': ['percent', '%'], 'source_id': 'policy_origin'},
                {'value': '5', 'literal': '5 percent', 'source_unit': 'percent',
                 'unit_labels': ['percent', '%'], 'source_id': 'policy_origin'}]
        if selector == 'unsupported_mechanism':
            # answered可明确回答“不能证明保证”；仍须独立coverage核其真实内容。
            expected['permitted_dispositions'] = ['rejected_statement', 'partial', 'unresolved', 'answered']
    elif selector == 'source_republication':
        rows = [('agency_alpha', 'Agency Alpha advance GDP release', initial),
                ('wire_copy', 'Republication of Agency Alpha release',
                 'This article republishes the Agency Alpha release, without independent measurement. ' + initial)]
        question = '原文GDP初值是什么，原始报告和明示转载能否算独立交叉核实？'
        expected = {'require_source_fact': True, 'forbid_cross_checked': True, 'source_values': ['2.8']}
    elif selector == 'conflicting_metric':
        rows = [('wire_a', 'Report of Agency Alpha advance estimate', initial),
                ('wire_b', 'Report of the same Agency Alpha advance estimate', initial.replace('2.8', '3.0'))]
        question = '同一机构、季度、年化口径和初次发布日期的两份GDP报道是否一致，如何保留分歧？'
        expected = {'source_values': ['2.8', '3.0'], 'require_dispute': True, 'forbid_cross_checked': True}
    else:
        rows, question = [], '这个完整已核配置窗口有哪些相关经济事件，能支持什么机制判断？'
        expected = {'empty_corpus': True, 'permitted_dispositions': ['no_relevant_material', 'unresolved']}
    return rows, question, dict(expected, source_origin='synthetic_fixture')


def _empty_window(scenario, entry):
    folder = scenario.plugins / entry['source_id']
    profile = json.loads((folder / 'extraction.json').read_bytes())
    profile['whole_window_proof'] = known({'url_pattern': profile['listing_rules'][0]['url_pattern'], 'format': 'html',
        'start_locator': '//*[@id="window-start"]', 'end_locator': '//*[@id="window-end"]',
        'complete_locator': '//*[@id="window-complete"]'})
    start = (instant(scenario.clock) - timedelta(days=7)).isoformat().replace('+00:00', 'Z')
    scenario.publish('/' + entry['source_id'] + '/list', '<html><body><ul></ul><span id="window-start">' + start
        + '</span><span id="window-end">' + scenario.clock + '</span><span id="window-complete">true</span></body></html>')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['profile_sha256'] = digest(canonical(profile))
    with urlopen(entry['entrypoints'][0], timeout=5) as response:
        raw, status = response.read(), response.status
    (scenario.root / 'configuration-empty-window-proof.html').write_bytes(raw)
    manifest['evidence'][0].update(captured_at=now(), http_status=known(status), response_sha256=digest(raw),
        response_byte_size=len(raw), observation='Actual finite synthetic window; not external publisher evidence')
    scenario.schemas.validate('investment.news_profile', profile)
    scenario.schemas.validate('investment.news_manifest', manifest)
    (folder / 'extraction.json').write_bytes(canonical(profile))
    (folder / 'manifest.json').write_bytes(canonical(manifest))


def _write(resolver, path, value, schema_id):
    resolver.schemas.validate(schema_id, value)
    atomic(path, canonical(value), immutable=True)
    return resolver.path_ref(path, schema_id)


def prepare_corpus(root, result_path, question, expected, *, selector='public_source', budget=8192):
    root = guard_namespace(root, 'test')
    result_path = Path(result_path).resolve()
    require(result_path.is_relative_to(root), 'RS01', '/sc02_result', 'Upstream result is outside this test batch')
    schemas = ResearchSchemas(('SC03',))
    resolver = ResearchResolver(root, schemas, mode='test')
    result = schemas.document('investment.sc02_run_result', result_path.read_bytes())
    require(result['status'] == 'sealed' and is_known(result['sealed_output_ref']),
            'RS03', '/upstream', 'A real completed SC02 result is required')
    seal = resolver.resolve(result['sealed_output_ref']['value'], 'investment.sealed_output')
    candidate = resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
    stamp, invocation = now(), 'flow-' + uuid.uuid4().hex
    source = next(row['source'] for row in schemas.definition('SC03')['input_roles'] if row['role'] == 'evidence_corpus_ref')
    _upstream_binding(resolver, candidate['payload_ref'], source,
                      {'run_id': candidate['run_id'], 'information_cutoff_at': stamp})
    day = instant(stamp).astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d')
    folder = root / 'runs/sc03' / day / invocation
    folder.mkdir(parents=True)
    protocol = {'protocol_id': 'source-assessment', 'protocol_version': 'v1',
        'questions': [{'question_id': 'research-question', 'question': question, 'basis': 'research_scope',
                       'basis_refs': [candidate['scope_ref']]}],
        'method_id': 'sc03-source-grounded-assessment', 'method_version': 'v1',
        'interpretation_mode': 'historical_exploration',
        'cross_check_policy': 'independent_origins_and_no_unresolved_conflict',
        'semantic_policy': 'independent_atomic_source_review_v1'}
    protocol_ref = _write(resolver, folder / 'protocol.json', protocol, 'investment.sc03.assessment_protocol')
    identity = {'identity_id': 'host-producer', 'role': 'producer',
        'provider': unknown('verified_host_identity_not_available'), 'model_name': unknown('verified_host_identity_not_available'),
        'model_version': unknown('verified_host_identity_not_available'), 'training_cutoff': unknown('verified_training_cutoff_not_available'),
        'identity_evidence_refs': [], 'generated_at': stamp, 'host_invocation_id': invocation}
    identity_ref = _write(resolver, folder / 'identity.json', identity, 'investment.sc03.analyst_identity')
    params_ref = _write(resolver, folder / 'parameters.json', {'assessment_protocol_ref': protocol_ref,
        'analyst_identity_ref': identity_ref, 'text_window_codepoints': budget}, 'investment.sc03.assessment_parameters')
    incoming = {'contract_id': 'investment.stage_input', 'contract_version': 1, 'run_id': candidate['run_id'],
        'stage_id': 'SC03', 'invocation_id': invocation, 'decision_at': stamp, 'information_cutoff_at': stamp,
        'scope_ref': candidate['scope_ref'], 'input_refs': [{'role': role, 'ref': reference} for role, reference in
        [('evidence_corpus_ref', candidate['payload_ref']), ('assessment_protocol_ref', protocol_ref),
         ('analyst_identity_ref', identity_ref)]], 'parameters_ref': params_ref}
    submitted = folder / 'submitted-input.json'
    _write(resolver, submitted, incoming, 'investment.stage_input')
    state = {'selector': selector, 'root': str(root), 'folder': str(folder), 'input': str(submitted),
             'run_id': candidate['run_id'], 'invocation_id': invocation, 'started_at': stamp,
             'expected': expected, 'upstream_seal_ref': result['sealed_output_ref']['value'], 'calls': []}
    path = root / ('sc03-flow-' + invocation + '.json')
    atomic(path, canonical(state), immutable=True)
    atomic(root / ('sc03-expectation-' + invocation + '.json'), canonical({'selector': selector,
        'question': question, 'expected': expected, 'registered_before_SC03_execution': True}), immutable=True)
    return advance(path)


def prepare(selector):
    materials, question, expected = case_material(selector)
    scenario = Scenario('sc03-flow-' + selector)
    try:
        scenario._account()
        entries = []
        for source_id, title, text in materials:
            article = scenario.article(source_id, title=title, text=text)
            scenario.listing(source_id, [article])
            entries.append(scenario.install(source_id))
        if not materials:
            scenario.listing('empty_publisher', [])
            entry = scenario.install('empty_publisher')
            _empty_window(scenario, entry)
            entries.append(entry)
        outcome = scenario.execute(entries, 'upstream-news', 'Actual SC01 then SC02 on explicit synthetic publications',
            'Current legal SC02 seal with evidence_review; source origin and all limitations retained')
        require(outcome['code'] == 0 and outcome['result']['status'] == 'sealed', 'RS03', '/upstream', 'SC02 did not seal')
        scenario.finish()
        return prepare_corpus(scenario.root, scenario.root / 'upstream-news.stdout.log', question, expected, selector=selector)
    finally:
        scenario.close()


def evaluate(resolver, state, result):
    seal = resolver.resolve(result['sealed_output_ref']['value'], 'investment.sealed_output')
    candidate = resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
    receipt = resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
    payload = resolver.resolve(candidate['payload_ref'], 'investment.sc03.research_assessment')
    tables = {name: resolver.resolve(payload[field])['rows'] for name, field in
        [('facts', 'facts_ref'), ('events', 'events_ref'), ('hypotheses', 'hypotheses_ref'),
         ('observations', 'economic_observations_ref'), ('dispositions', 'dispositions_ref'),
         ('counterevidence', 'counterevidence_ref')]}
    corpus_ref = next(row['ref'] for row in candidate['input_refs'] if row['role'] == 'evidence_corpus_ref')
    corpus = resolver.resolve(corpus_ref, 'investment.evidence_corpus')
    documents = resolver.resolve(corpus['documents_ref'], 'investment.news_documents')['rows']
    bodies = {row['version_id']: resolver.resolve(row['body_ref'], 'investment.news_body') for row in documents}
    checks = {'one_original_question': len(tables['dispositions']) == 1
              and tables['dispositions'][0]['question_id'] == 'research-question'}
    expected = state['expected']
    if expected.get('require_source_fact'):
        checks['substantive_source_fact'] = any(row['verification_status'] in ('source_reported', 'cross_checked') for row in tables['facts'])
    if expected.get('forbid_cross_checked'):
        checks['no_false_independent_confirmation'] = all(row['verification_status'] != 'cross_checked' for row in tables['facts'])
    if expected.get('source_values'):
        actual = {row['raw_value']['value'] for row in tables['observations'] if row['raw_value']['status'] == 'known'}
        checks['all_original_values_preserved'] = set(expected['source_values']) <= actual
    for index, target in enumerate(expected.get('exact_numeric_observations', [])):
        matched = False
        for row in tables['observations']:
            if row['raw_value']['status'] != 'known' or row['raw_value']['value'] != target['value']:
                continue
            if row['unit']['status'] != 'known' or row['unit']['value'].casefold() not in target['unit_labels']:
                continue
            quote = row['quote_locator']
            document = next((item for item in documents if item['version_id'] == row['document_version_id']), None)
            if document is None or document['source_id'] != target['source_id']:
                continue
            source = bodies[document['version_id']][quote['text_field']]
            matched = (quote['document_version_id'] == document['version_id'] and quote['body_ref'] == document['body_ref']
                and 0 <= quote['start'] < quote['end'] <= len(source)
                and source[quote['start']:quote['end']] == quote['quote']
                and target['literal'] in quote['quote'] and target['source_unit'] in quote['quote'])
            if matched:
                break
        checks['exact_same_unit_source_number_' + str(index)] = matched
    if expected.get('require_dispute'):
        checks['same_vintage_conflict_preserved'] = bool(tables['counterevidence']) and any(
            row['verification_status'] == 'disputed' for row in tables['facts'])
    if expected.get('empty_corpus'):
        checks['empty_stays_empty'] = not documents and all(not tables[name] for name in ('facts', 'events', 'hypotheses', 'observations'))
    if expected.get('permitted_dispositions'):
        checks['faithful_question_result'] = all(row['status'] in expected['permitted_dispositions'] for row in tables['dispositions'])
    if expected.get('minimum_max_document_codepoints'):
        checks['actual_full_long_document'] = max((len(body['text']) for body in bodies.values()), default=0) >= expected['minimum_max_document_codepoints']
    for index, target in enumerate(expected.get('required_observations', [])):
        versions = {row['version_id'] for row in documents if row['source_id'] == target['source_id']}
        checks['source_observation_' + str(index)] = any(row['document_version_id'] in versions
            and row['raw_value']['status'] == 'known' and row['raw_value']['value'] == target['raw_value']
            and row['release_vintage']['status'] == 'known' and row['release_vintage']['value'] == target['release_vintage']
            for row in tables['observations'])
    captures = [resolver.resolve(body['capture_ref'], 'investment.news_capture') for body in bodies.values()]
    origin = 'synthetic' if expected.get('source_origin') == 'synthetic_fixture' else expected.get('source_origin')
    if origin:
        checks['faithful_source_origin'] = (bool(captures) or expected.get('empty_corpus', False)) and all(row['origin'] == origin for row in captures)
    return {'candidate_ref': seal['candidate_output_ref'], 'receipt_ref': seal['validation_receipt_ref'],
        'independent_checks': checks, 'acceptance_satisfied': bool(checks) and all(checks.values()),
        'output_valid': receipt['overall_verdict'] in ('passed', 'partial') and bool(receipt['allowed_uses']),
        'original_research_goal_satisfied': all(row['status'] == 'answered' for row in tables['dispositions'])
            and not payload['unresolved_questions'],
        'source_document_count': len(documents), 'source_codepoints': {key: len(body['text']) for key, body in bodies.items()}}


def advance(path):
    path = Path(path).resolve()
    root = guard_namespace(path.parent, 'test')
    state = json.loads(path.read_bytes())
    require(Path(state['root']).resolve() == root and Path(state['input']).resolve().is_relative_to(root),
            'RS01', '/state', 'Flow state escapes this test batch')
    command = [sys.executable, '-B', str(PLUGIN / 'scripts/run_sc03.py'), '--root', str(root), '--mode', 'test',
        '--input', state['input'], '--run-id', state['run_id'], '--invocation-id', state['invocation_id']]
    schemas = ResearchSchemas(('SC03',))
    schemas.validate_type('PathID', state['invocation_id'])
    index = len(state['calls']) + 1
    prefix = root / ('sc03-controller-' + state['invocation_id'] + '-call-' + str(index))
    process = subprocess.run(command, cwd=PLUGIN, capture_output=True,
        env=dict(os.environ, PYTHONPATH=str(PLUGIN / 'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))
    atomic(prefix.with_suffix('.command.json'), canonical(command), immutable=True)
    atomic(prefix.with_suffix('.stdout.log'), process.stdout, immutable=True)
    atomic(prefix.with_suffix('.stderr.log'), process.stderr, immutable=True)
    state['calls'].append({'index': index, 'exit_code': process.returncode, 'stdout': str(prefix.with_suffix('.stdout.log'))})
    atomic(path, canonical(state))
    require(process.returncode in (0, 1, 3) and bool(process.stdout), 'RS04', '/controller',
            'Controller could not execute: ' + process.stderr.decode('utf-8', errors='replace')[:1200])
    result = schemas.document('investment.research_run_result', process.stdout)
    report = {'state_path': str(path), 'selector': state['selector'], 'status': result['status'],
        'started_at': state['started_at'], 'observed_at': now(), 'result': result,
        'execution_succeeded': result['status'] == 'completed', 'output_valid': False,
        'original_research_goal_satisfied': False, 'acceptance_satisfied': False}
    if result['status'] == 'completed':
        report.update(evaluate(ResearchResolver(root, schemas, mode='test'), state, result))
    atomic(root / ('sc03-flow-report-' + state['invocation_id'] + '.json'), canonical(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare', choices=CASES)
    action.add_argument('--advance', type=Path)
    action.add_argument('--from-sc02-result', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--question')
    parser.add_argument('--expectation', type=Path)
    parser.add_argument('--text-window-codepoints', type=int, default=8192)
    args = parser.parse_args(argv)
    if args.prepare:
        report = prepare(args.prepare)
    elif args.advance:
        report = advance(args.advance)
    else:
        require(args.root is not None and bool(args.question) and args.expectation is not None,
                'RS01', '/arguments', 'Existing SC02 result requires root, original question and independent expectation')
        root = guard_namespace(args.root, 'test')
        require(args.expectation.resolve().is_relative_to(root), 'RS01', '/expectation', 'Expectation is outside batch')
        expected = json.loads(args.expectation.read_bytes())
        report = prepare_corpus(root, args.from_sc02_result, args.question, expected, budget=args.text_window_codepoints)
    sys.stdout.buffer.write(canonical(report))
    return 3 if report['status'] == 'host_work_required' else 0 if report['acceptance_satisfied'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
