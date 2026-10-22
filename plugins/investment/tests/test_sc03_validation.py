"""SC03真实上游和真实宿主校准入口；不伪造Seal或模型响应。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import uuid
from types import SimpleNamespace
from jsonschema import Draft202012Validator, ValidationError
from urllib.request import urlopen

sys.dont_write_bytecode = True
PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'src'))

from investment.contracts import ContractError, SPEC, artifact, canonical, digest, instant, known, now, require, unknown
from investment.runtime.runs import atomic
from investment.research.contracts import ResearchSchemas, StageContext, guard_namespace
from investment.research.references import ResearchResolver
from investment.research.controller import ResearchController, _input
from investment.chains.news_assessment.contracts import TABLES, number_literals, Checks
from investment.chains.news_assessment import reporting
from investment.chains.news_assessment.validation import validate, _eligibility
from investment.chains.news_assessment.semantics import work_item, plan

SOURCE = importlib.util.spec_from_file_location('investment_sc02_scenario', PLUGIN / 'scripts/verify_sc02.py')
SC02 = importlib.util.module_from_spec(SOURCE)
SOURCE.loader.exec_module(SC02)
CASES = ('supported_fact', 'source_republication', 'conflicting_metric', 'unsupported_mechanism', 'empty_corpus')


def case_material(selector):
    policy = 'On September 18, 2024, the Committee decided to lower the target range for the federal funds rate by 1/2 percentage point to 4-3/4 to 5 percent. The Committee will carefully assess incoming data, the evolving outlook, and the balance of risks.'
    initial = 'Agency Alpha reported that real GDP increased at an annual rate of 2.8 percent in the second quarter of 2024. This is the advance estimate released on July 25, 2024.'
    if selector in ('supported_fact', 'unsupported_mechanism'):
        documents = [('policy_origin', 'Policy committee statement', policy)]
        question = '原文报告了怎样的政策变化，能够提出哪些有条件、可证伪的经济传导假说？'
        if selector == 'unsupported_mechanism':
            question += '这些材料是否支持所有科技基金下一交易日必然上涨5%的断言？'
        expected = {'source_numbers': ['0.5', '4.75', '5'], 'cross_checked': False,
                    'return_probability_or_guarantee': False, 'source_status': 'source_reported'}
    elif selector == 'source_republication':
        documents = [('agency_alpha', 'Agency Alpha advance GDP release', initial),
                     ('wire_copy', 'Republication of Agency Alpha release', 'This article republishes the Agency Alpha release, without independent measurement. ' + initial)]
        question = '原文中的GDP初值是什么，原始报告和转载能否算独立交叉核实？'
        expected = {'source_numbers': ['2.8'], 'independent_origins': 1, 'cross_checked': False}
    elif selector == 'conflicting_metric':
        documents = [('wire_a', 'Report of Agency Alpha advance estimate', initial),
                     ('wire_b', 'Report of the same Agency Alpha advance estimate', initial.replace('2.8', '3.0'))]
        question = '同一机构、指标、季度、年化口径及初次发布日期的两份报道是否一致，如何保留分歧？'
        expected = {'source_numbers': ['2.8', '3.0'], 'relation': 'contradiction', 'preserve_both': True, 'average_or_vote': False}
    else:
        documents = []
        question = '这个完整已核配置窗口内有哪些相关经济事件，能支持什么机制判断？'
        expected = {'documents': 0, 'substantive_answer': False, 'business_result': 'observation_only',
                    'hypothesis_research': False, 'coverage_finding': 'insufficient_evidence'}
    return documents, question, expected


def write_artifact(resolver, path, value, schema_id):
    resolver.schemas.validate(schema_id, value)
    raw = canonical(value)
    if path.exists():
        require(path.read_bytes() == raw, 'A01', '/calibration', 'Calibration artifact is immutable')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return resolver.path_ref(path, schema_id)


def empty_window(sc, entry):
    folder = sc.plugins / entry['source_id']
    profile = json.loads((folder / 'extraction.json').read_bytes())
    profile['whole_window_proof'] = known({'url_pattern': profile['listing_rules'][0]['url_pattern'], 'format': 'html',
        'start_locator': '//*[@id="window-start"]', 'end_locator': '//*[@id="window-end"]', 'complete_locator': '//*[@id="window-complete"]'})
    start = (instant(sc.clock) - timedelta(days=7)).isoformat().replace('+00:00', 'Z')
    body = '<html><body><ul></ul><span id="window-start">' + start + '</span><span id="window-end">' + sc.clock + '</span><span id="window-complete">true</span></body></html>'
    sc.publish('/' + entry['source_id'] + '/list', body)
    # 明确有限模拟源完整窗口；按实际响应重新安装入口证据。
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['profile_sha256'] = digest(canonical(profile))
    with urlopen(entry['entrypoints'][0], timeout=5) as response:
        raw, status = response.read(), response.status
    (sc.root / 'configuration-empty-window-proof.html').write_bytes(raw)
    manifest['evidence'][0].update(captured_at=now(), http_status=known(status), response_sha256=digest(raw), response_byte_size=len(raw),
        observation='Actual synthetic finite-window response; not proof about any real external publisher')
    sc.schemas.validate('investment.news_profile', profile)
    sc.schemas.validate('investment.news_manifest', manifest)
    (folder / 'extraction.json').write_bytes(canonical(profile))
    (folder / 'manifest.json').write_bytes(canonical(manifest))


def prepare(selector):
    documents, question, expected = case_material(selector)
    sc = SC02.Scenario('sc03-' + selector)
    try:
        sc._account()
        entries = []
        for source_id, title, text in documents:
            article = sc.article(source_id, title=title, text=text)
            sc.listing(source_id, [article])
            entries.append(sc.install(source_id))
        if not documents:
            sc.listing('empty_publisher', [])
            entry = sc.install('empty_publisher')
            empty_window(sc, entry)
            entries.append(entry)
        outcome = sc.execute(entries, 'upstream-news', 'Explicit synthetic publications through actual SC01 and SC02',
                             'Real SC02 seal with evidence_review permission; source bytes and synthetic origin retained')
        require(outcome['code'] == 0, 'A01', '/upstream', 'Actual SC02 did not produce a legal sealed calibration input')
        seal = sc.resolver.resolve(outcome['result']['sealed_output_ref']['value'], 'investment.sealed_output')
        candidate = sc.resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
        receipt = sc.resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
        require('evidence_review' in receipt['allowed_uses'], 'A01', '/upstream', 'Actual upstream receipt does not permit evidence review')
        root = sc.root
        schemas = ResearchSchemas(('SC03',))
        resolver = ResearchResolver(root, schemas, mode='test')
        invocation_id = 'calibration-' + uuid.uuid4().hex
        stamp = now()
        day = instant(stamp).astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d')
        folder = root / 'runs/sc03' / day / invocation_id
        folder.mkdir(parents=True)
        scope_ref = candidate['scope_ref']
        protocol = {'protocol_id': 'source-assessment', 'protocol_version': 'v1',
            'questions': [{'question_id': 'research-question', 'question': question, 'basis': 'research_scope', 'basis_refs': [scope_ref]}],
            'method_id': 'sc03-source-grounded-assessment', 'method_version': 'v1', 'interpretation_mode': 'historical_exploration',
            'cross_check_policy': 'independent_origins_and_no_unresolved_conflict', 'semantic_policy': 'independent_atomic_source_review_v1'}
        protocol_ref = write_artifact(resolver, folder / 'protocol.json', protocol, 'investment.sc03.assessment_protocol')
        identity = {'identity_id': 'host-producer', 'role': 'producer', 'provider': unknown('actual_host_metadata_not_yet_available'),
            'model_name': unknown('actual_host_metadata_not_yet_available'), 'model_version': unknown('actual_host_metadata_not_yet_available'),
            'training_cutoff': unknown('verified_training_cutoff_not_available'), 'identity_evidence_refs': [],
            'generated_at': stamp, 'host_invocation_id': invocation_id}
        identity_ref = write_artifact(resolver, folder / 'identity.json', identity, 'investment.sc03.analyst_identity')
        parameters_ref = write_artifact(resolver, folder / 'parameters.json',
            {'assessment_protocol_ref': protocol_ref, 'analyst_identity_ref': identity_ref, 'text_window_codepoints': 8192},
            'investment.sc03.assessment_parameters')
        incoming = {'contract_id': 'investment.stage_input', 'contract_version': 1, 'run_id': sc.run_id, 'stage_id': 'SC03',
            'invocation_id': invocation_id, 'decision_at': stamp, 'information_cutoff_at': stamp, 'scope_ref': scope_ref,
            'input_refs': [{'role': role, 'ref': ref} for role, ref in
                [('evidence_corpus_ref', candidate['payload_ref']), ('assessment_protocol_ref', protocol_ref), ('analyst_identity_ref', identity_ref)]],
            'parameters_ref': parameters_ref}
        submitted_ref = write_artifact(resolver, folder / 'submitted-input.json', incoming, 'investment.stage_input')
        bindings = {}
        _input(resolver, submitted_ref, 'SC03', sc.run_id, invocation_id, upstream_receipts=bindings)
        controller = ResearchController(root, mode='test', stage_id='SC03')
        controller.schemas, controller.resolver = schemas, resolver
        frozen = controller._freeze(incoming, folder)
        (folder / 'attempt-01').mkdir()
        state = {'selector': selector, 'root': str(root), 'folder': str(folder), 'run_id': sc.run_id, 'invocation_id': invocation_id,
                 'frozen': frozen, 'input_ref': frozen['frozen_input_ref'], 'attempt_no': 1,
                 'candidate_ref': None, 'expected': expected, 'upstream_seal_ref': sc.upstream_seal,
                 'news_seal_ref': outcome['result']['sealed_output_ref'], 'phase': 'awaiting_actual_producer_model',
                 'scope': 'independent_validator_calibration_not_SC03_controller_execution'}
        state_path = root / 'sc03-calibration.json'
        state_path.write_bytes(canonical(state))
        (root / 'sc03-independent-expectation.json').write_bytes(canonical({'selector': selector, 'expected': expected,
            'source_origin': 'synthetic_fixture', 'source_documents': documents, 'authorization': 'validator_first_calibration'}))
        return advance(state_path)
    finally:
        sc.close()


def context(state, role):
    root = guard_namespace(Path(state['root']), 'test')
    schemas = ResearchSchemas(('SC03',))
    resolver = ResearchResolver(root, schemas, mode='test')
    incoming = resolver.resolve(state['input_ref'], 'investment.stage_input')
    bindings = {}
    _input(resolver, state['input_ref'], 'SC03', state['run_id'], state['invocation_id'], upstream_receipts=bindings)
    frozen = state['frozen']
    method = resolver.resolve(frozen['method_ref'], 'investment.sc03_method')
    allowed = [state['input_ref'], incoming['scope_ref'], incoming['parameters_ref'], frozen['method_ref']]
    allowed += [item['ref'] for item in incoming['input_refs']]
    steps = [item for item in frozen['host_steps'] if item['role'] == role]
    allowed += [step[key] for step in steps for key in ('prompt_ref', 'output_schema_ref')]
    candidate = resolver.resolve(state['candidate_ref'], 'investment.candidate_output') if state['candidate_ref'] else None
    if candidate:
        allowed += [state['candidate_ref'], candidate['payload_ref']] + candidate['datasets'] + candidate['evidence_refs']
    scoped = ResearchResolver(root, schemas, mode='test', allowed_refs=allowed, reference_grants=method['runtime']['reference_grants'])
    attempt_dir = Path(state['folder']) / f"attempt-{state['attempt_no']:02}"
    scoped.local_context = ('SC03', role, attempt_dir)
    scoped.host_output_refs = []
    return StageContext(root=root, mode='test', stage_id='SC03', run_id=state['run_id'], invocation_id=state['invocation_id'],
        attempt_no=state['attempt_no'], role=role, schemas=schemas, resolver=scoped, input=incoming, input_ref=state['input_ref'], method=method,
        method_ref=frozen['method_ref'], definition=method['runtime'], attempt_dir=attempt_dir,
        candidate=candidate if role == 'validator' else None, candidate_ref=state['candidate_ref'] if role == 'validator' else None,
        frozen_host_steps=steps, upstream_receipts=bindings)


def assemble_candidate(ctx, draft):
    refs = {}
    for name, (payload_field, _, schema_id) in TABLES.items():
        source_field = 'economic_observations' if name == 'observations' else name
        value = draft[source_field]
        reference = ctx.put_json('calibration-' + name + '.json', value, schema_id)
        refs[payload_field] = ctx.dataset_ref(value, schema_id, reference['uri'])
    roles = {item['role']: item['ref'] for item in ctx.input['input_refs']}
    payload = dict(refs, unresolved_questions=draft['unresolved_questions'],
                   analyst_identity_ref=roles['analyst_identity_ref'], assessment_protocol_ref=roles['assessment_protocol_ref'])
    payload_ref = ctx.put_json('calibration-assessment.json', payload, 'investment.sc03.research_assessment')
    candidate = {'contract_id': 'investment.candidate_output', 'contract_version': 1, 'run_id': ctx.run_id, 'stage_id': 'SC03',
        'output_id': 'calibration-output', 'created_at': now(), 'processing_state': 'completed',
        'input_refs': ctx.input['input_refs'], 'scope_ref': ctx.input['scope_ref'], 'method_ref': ctx.method_ref,
        'payload_ref': payload_ref, 'datasets': list(refs.values()), 'evidence_refs': [],
        'gaps': draft['unresolved_questions']}
    ctx.schemas.validate('investment.candidate_output', candidate)
    return candidate


def read_state(state_path):
    """校准控制文件读取前先约束到单批test根。"""
    state_path = Path(state_path).resolve()
    root = guard_namespace(state_path.parent, 'test')
    require(state_path.name == 'sc03-calibration.json', 'A01', '/calibration', 'Exact calibration state filename is required')
    state = json.loads(state_path.read_bytes())
    require(Path(state['root']).resolve() == root and Path(state['folder']).resolve().is_relative_to(root / 'runs/sc03'),
            'A01', '/calibration', 'Calibration state points outside its selected test batch')
    return state_path, state


def retry_failed_host(state_path):
    """只据实际绑定的producer失败消费一次校准预算，不改旧响应。"""
    state_path, state = read_state(state_path)
    require(state['candidate_ref'] is None, 'A14', '/candidate', 'This retry entry is limited to pre-candidate host failure')
    require(state['attempt_no'] < 3, 'A14', '/attempt_no', 'Three calibration attempts are exhausted')
    ctx = context(state, 'producer')
    failed = []
    for path in ctx.attempt_dir.glob('host-*-response.json'):
        response = json.loads(path.read_bytes())
        if response['status'] != 'failed':
            continue
        request_path = path.with_name(path.name.removesuffix('-response.json') + '-request.json')
        request = json.loads(request_path.read_bytes())
        require(request['role'] == 'producer', 'A14', '/host_request', 'Only an actual producer failure may enter this retry')
        try:
            ctx.host_response(request['step_id'], work_key=request['work_key'])
        except ContractError as error:
            require(error.code == 'execution_failed', 'A14', '/host_response', 'Failure binding did not validate')
            failed.append({'attempt_no': state['attempt_no'], 'recorded_at': now(),
                           'request_ref': ctx.resolver.path_ref(request_path, 'investment.host_request'),
                           'response_ref': ctx.resolver.path_ref(path, 'investment.host_response'),
                           'error': response['error'], 'reason': str(error)})
        else:
            raise AssertionError('A failed host response returned a business output')
    require(len(failed) == 1, 'A14', '/host_response', 'Exactly one genuine failed response is required for retry')
    state.setdefault('failures', []).extend(failed)
    state['attempt_no'] += 1
    (Path(state['folder']) / f"attempt-{state['attempt_no']:02}").mkdir()
    state['phase'] = 'awaiting_actual_producer_model'
    atomic(state_path, canonical(state))
    return advance(state_path)


def advance(state_path):
    state_path, state = read_state(state_path)
    if state['candidate_ref'] is None:
        ctx = context(state, 'producer')
        corpus = ctx.read_role('evidence_corpus_ref')
        documents = ctx.resolver.resolve(corpus['documents_ref'])['rows']
        upstream_ref, _ = ctx.upstream_receipt('evidence_corpus_ref')
        refs = [item['ref'] for item in ctx.input['input_refs']] + [ctx.input['parameters_ref'], upstream_ref,
               corpus['documents_ref'], corpus['coverage_ref'], corpus['pending_items_ref']]
        refs += [row['body_ref'] for row in documents]
        # 校准候选来自同一正式模型模板，测试仅保存其实际业务行并装配准确文件引用。
        protocol = ctx.read_role('assessment_protocol_ref')
        budget = ctx.resolver.resolve(ctx.input['parameters_ref'])['text_window_codepoints']
        spans = []
        for row in documents:
            body = ctx.resolver.resolve(row['body_ref'])
            for field in ('text', 'title'):
                spans += [{'document_version_id': row['version_id'], 'text_field': field, 'start': start, 'end': min(start + budget, len(body[field]))}
                          for start in range(0, len(body[field]), budget)]
        item = work_item('assess_documents', spans, [q['question_id'] for q in protocol['questions']], [])
        work = {'plan_id': 'calibration-source-plan', 'input_ref': ctx.input_ref, 'text_window_codepoints': budget, 'items': [item]}
        plan_ref = ctx.put_json('calibration-work-plan.json', work, 'investment.sc03.assessment_work_plan')
        refs.append(plan_ref)
        response = ctx.host_response('assess_documents', work_key=item['work_key'])
        if response is None:
            result = ctx.request_host('assess_documents', refs, work_key=item['work_key'])
            request_path = ctx.attempt_dir / ('host-assess_documents-' + item['work_key'] + '-request.json')
            print(canonical({'state': str(state_path), 'request_path': str(request_path), 'request': result}).decode())
            return result
        candidate = assemble_candidate(ctx, response)
        path = ctx.attempt_dir / 'candidate.json'
        if path.exists():
            previous_raw = path.read_bytes()
            previous = ctx.schemas.document('investment.candidate_output', previous_raw)
            candidate['created_at'] = previous['created_at']
            require(previous_raw == canonical(candidate), 'A01', '/candidate',
                    'Existing calibration candidate differs from the exact bound model output')
        raw = canonical(candidate)
        # 控制文件由校准装配边界持久化，不让producer角色扩大读取权限。
        atomic(path, raw, immutable=True)
        uri = 'investment-run://sc03/' + path.relative_to(ctx.root / 'runs/sc03').as_posix()
        state['candidate_ref'] = artifact(raw, 'investment.candidate_output', uri)
        state['phase'] = 'independent_validator'
        state_path.write_bytes(canonical(state))
    ctx = context(state, 'validator')
    result = validate(ctx)
    if 'request_id' in result:
        path = ctx.attempt_dir / ('host-' + result['step_id'] + '-' + result['work_key'] + '-request.json')
        print(canonical({'state': str(state_path), 'request_path': str(path), 'request': result}).decode())
        return result
    ctx.schemas.validate('investment.validation_receipt', result)
    (Path(state['root']) / 'sc03-validation-receipt.json').write_bytes(canonical(result))
    state['phase'] = 'validator_returned'
    state['actual_receipt'] = result
    state_path.write_bytes(canonical(state))
    print(canonical({'state': str(state_path), 'receipt': result, 'scope': state['scope']}).decode())
    return result


class ReportingGapTests(unittest.TestCase):
    """仅测试机器边界对象；不构造上游Seal、HostEvent或模型响应。"""

    def snapshot(self, gaps, *, empty=False):
        schemas = ResearchSchemas(('SC03',))
        values = {}
        def reference(schema_id, name, value):
            ref = artifact(canonical(value), schema_id, 'investment-run://sc03/20261019/unit/attempt-01/' + name + '.json')
            values[canonical(ref)] = value
            return ref
        tables = {name: [] for name in TABLES}
        tables['facts'] = [{'fact_id': 'f1', 'event_time': unknown('not_known'), 'assertion': 'reported',
                            'verification_status': 'source_reported', 'support_refs': [], 'conflict_refs': [],
                            'quote_locators': [], 'document_version_ids': []},
                           {'fact_id': 'f2', 'event_time': unknown('not_known'), 'assertion': 'reported',
                            'verification_status': 'source_reported', 'support_refs': [], 'conflict_refs': [],
                            'quote_locators': [], 'document_version_ids': []}] if not empty else []
        payload = {'unresolved_questions': gaps}
        for name, (field, _, schema_id) in TABLES.items():
            payload[field] = reference(schema_id, name, {'rows': tables[name]})
        identity = {'provider': unknown('not_known')}
        protocol = {'questions': [{'question_id': 'q'}]}
        payload['analyst_identity_ref'] = reference('investment.sc03.analyst_identity', 'identity', identity)
        payload['assessment_protocol_ref'] = reference('investment.sc03.assessment_protocol', 'protocol', protocol)
        corpus = {field: reference(schema_id, field, {'rows': []}) for field, schema_id in
                  [('documents_ref', 'investment.news_documents'), ('coverage_ref', 'investment.news_coverage'),
                   ('pending_items_ref', 'investment.news_pending_items')]}
        incoming = {'parameters_ref': reference('investment.sc03.assessment_parameters', 'params', {}),
                    'input_refs': [{'role': 'analyst_identity_ref', 'ref': payload['analyst_identity_ref']},
                                   {'role': 'assessment_protocol_ref', 'ref': payload['assessment_protocol_ref']}]}
        upstream = (reference('investment.validation_receipt', 'receipt', {}), {'blockers': [], 'allowed_uses':
                    ['evidence_review', 'hypothesis_research', 'reporting']})
        ctx = SimpleNamespace(input=incoming, input_ref=reference('investment.stage_input', 'input', incoming),
            candidate={'payload_ref': reference('investment.sc03.research_assessment', 'payload', payload)},
            resolver=SimpleNamespace(resolve=lambda ref: values[canonical(ref)]), schemas=schemas,
            definition={'critical_rules': ['A01']})
        return SimpleNamespace(ctx=ctx, payload=payload, identity=identity, protocol=protocol, corpus=corpus,
            tables=tables, indexes={name: {row[TABLES[name][1]]: row for row in rows} for name, rows in tables.items()},
            documents={}, bodies={}, inherited_gaps=[], upstream_receipts={'evidence_corpus_ref': upstream},
            parameters={'text_window_codepoints': 100})

    def gap(self, path='/facts/event_time'):
        return {'rule_id': 'A07', 'field_or_object': path, 'reason': 'Event time is not available',
                'owner': 'source', 'required_evidence': ['source_bytes'], 'affected_uses': ['reporting']}

    def review(self, snapshot, *, decision='faithful_limited_report', pointer='/rows'):
        key = next(iter(reporting.local_gaps(snapshot)))
        return {'gap_sha256': key, 'reviewed_objects': [{'object_ref': snapshot.payload['facts_ref'],
                'json_pointer': pointer}], 'decision': decision, 'reason': 'Both current event times remain unknown.',
                'missing_evidence': ['source event time'] if decision == 'insufficient_evidence' else []}

    def test_complete_gap_identity_and_duplicate_indices(self):
        gap = self.gap()
        snapshot = self.snapshot([gap, deepcopy(gap)])
        groups = reporting.local_gaps(snapshot)
        self.assertEqual(next(iter(groups.values()))['indices'], [0, 1])
        self.assertEqual(len(reporting.gap_keys(snapshot)), 2)
        items = plan(snapshot)['items']
        assigned = [item for item in items if any('"reporting_gap"' in key for key in item['target_keys'])]
        self.assertEqual(len(assigned), 1)
        self.assertEqual(len(assigned[0]['target_keys']), 2)

    def test_only_exact_verified_inheritance_is_excluded(self):
        original = self.gap()
        changed = dict(original, reason='A distinct local limitation')
        snapshot = self.snapshot([original, changed])
        snapshot.inherited_gaps = [original]
        self.assertEqual(set(reporting.local_gaps(snapshot)), {digest(canonical(changed))})

    def test_column_review_must_cover_every_current_row(self):
        snapshot = self.snapshot([self.gap()])
        reporting.validate_reviews(snapshot, [self.review(snapshot)], reporting.local_gaps(snapshot))
        with self.assertRaises(ContractError):
            reporting.validate_reviews(snapshot, [self.review(snapshot, pointer='/rows/0')], reporting.local_gaps(snapshot))

    def test_unmapped_gap_only_records_insufficient_location(self):
        snapshot = self.snapshot([self.gap('/unknown-object')])
        row = self.review(snapshot, decision='insufficient_evidence')
        row['reviewed_objects'] = [{'object_ref': snapshot.ctx.candidate['payload_ref'],
                                    'json_pointer': '/unresolved_questions/0'}]
        reporting.validate_reviews(snapshot, [row], reporting.local_gaps(snapshot))
        for decision in ('faithful_limited_report', 'blocks_report'):
            with self.subTest(decision=decision), self.assertRaises(ContractError):
                reporting.validate_reviews(snapshot, [dict(row, decision=decision)], reporting.local_gaps(snapshot))

    def test_wrong_reference_or_pointer_is_rejected(self):
        snapshot = self.snapshot([self.gap()])
        for pointer in ('/rows/-1', '/rows/01', '/rows/2', '/rows/0/not_present', '/rows/0/~2'):
            with self.subTest(pointer=pointer), self.assertRaises(ContractError):
                reporting.validate_reviews(snapshot, [self.review(snapshot, pointer=pointer)], reporting.local_gaps(snapshot))
        row = self.review(snapshot)
        row['reviewed_objects'][0]['object_ref'] = dict(row['reviewed_objects'][0]['object_ref'], sha256='0' * 64)
        with self.assertRaises(ContractError):
            reporting.validate_reviews(snapshot, [row], reporting.local_gaps(snapshot))

    def test_gap_set_is_exact_unique_and_source_batch_cannot_fill_it(self):
        snapshot = self.snapshot([self.gap()])
        row = self.review(snapshot)
        for rows, wanted in (([], reporting.local_gaps(snapshot)), ([row, row], reporting.local_gaps(snapshot)), ([row], set())):
            with self.subTest(count=len(rows)), self.assertRaises(ContractError):
                reporting.validate_reviews(snapshot, rows, wanted)

    def test_new_private_schema_requires_insufficiency_evidence(self):
        snapshot = self.snapshot([self.gap()])
        row = self.review(snapshot, decision='insufficient_evidence')
        validator = Draft202012Validator({'$ref': 'urn:investment:spec:v1:sc03#/$defs/ReportingGapReview'},
            registry=snapshot.ctx.schemas.resources, format_checker=snapshot.ctx.schemas.format_checker)
        validator.validate(row)
        with self.assertRaises(ValidationError):
            validator.validate(dict(row, missing_evidence=[]))

    def test_empty_column_is_structural_evidence_without_source_quotes(self):
        snapshot = self.snapshot([self.gap()], empty=True)
        reporting.validate_reviews(snapshot, [self.review(snapshot)], reporting.local_gaps(snapshot))
        self.assertEqual(reporting.restricted_objects(snapshot, self.gap('/facts/event_time/nonexistent')), [])
        self.assertEqual(reporting.restricted_objects(snapshot, self.gap('prefix/facts')), [])

    def test_report_gate_keeps_hard_checks_and_research_restriction(self):
        snapshot = self.snapshot([self.gap()])
        for decision in ('faithful_limited_report', 'blocks_report', 'insufficient_evidence'):
            checks = Checks(snapshot.ctx)
            for rule in ('A01', 'A05', 'A06', 'A07', 'A08', 'A09'):
                checks.set(rule)
            _eligibility(snapshot, checks, [self.review(snapshot, decision=decision)])
            self.assertEqual(checks.verdict('A17'), 'passed' if decision == 'faithful_limited_report' else 'insufficient_evidence')
            self.assertEqual(checks.verdict('A16'), 'insufficient_evidence')
        checks = Checks(snapshot.ctx)
        for rule in ('A01', 'A05', 'A06', 'A07', 'A08', 'A09'):
            checks.set(rule)
        checks.set('A06', 'failed')
        _eligibility(snapshot, checks, [self.review(snapshot)])
        self.assertEqual(checks.verdict('A17'), 'insufficient_evidence')
        checks = Checks(snapshot.ctx)
        for rule in ('A01', 'A05', 'A06', 'A07', 'A08', 'A09'):
            checks.set(rule)
        snapshot.upstream_receipts['evidence_corpus_ref'][1]['allowed_uses'].remove('reporting')
        _eligibility(snapshot, checks, [self.review(snapshot)])
        self.assertEqual(checks.verdict('A17'), 'insufficient_evidence')



class SourceNumberTests(unittest.TestCase):
    def test_fomc_fraction_source_numbers(self):
        self.assertEqual(number_literals('lowered by 1/2 percentage point to 4-3/4 to 5 percent'), {'0.5', '4.75', '5'})

    def test_decimal_precision_is_not_added_or_dropped(self):
        self.assertEqual(number_literals('real GDP was 2.80 percent'), {'2.80'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', choices=CASES)
    parser.add_argument('--advance', type=Path)
    parser.add_argument('--retry-failed-host', type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare)
    elif args.advance:
        advance(args.advance)
    elif args.retry_failed_host:
        retry_failed_host(args.retry_failed_host)
    else:
        unittest.main(argv=[sys.argv[0]])
