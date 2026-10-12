"""SC03生产流程：全量原文规划、真实宿主分析与六表候选组装。"""
from __future__ import annotations

from investment.contracts import canonical, digest, now, require
from .contracts import TABLES


def _work(step, spans, questions, related=()):
    value = {'step_id': step, 'document_spans': spans, 'question_ids': questions,
             'target_keys': [], 'related_work_keys': list(related)}
    return dict(value, work_key='work-' + digest(canonical(value))[:40])


def plan(input_ref, documents, bodies, questions, budget):
    """连续覆盖全部正文和标题；预算只分片，不改变原始对象范围。"""
    groups, current, size = [], [], 0
    for document in sorted(documents, key=lambda row: row['version_id']):
        version = document['version_id']
        for field in ('text', 'title'):
            for start in range(0, len(bodies[version][field]), budget):
                end = min(start + budget, len(bodies[version][field]))
                if current and size + end - start > budget:
                    groups.append(current)
                    current, size = [], 0
                current.append({'document_version_id': version, 'text_field': field,
                                'start': start, 'end': end})
                size += end - start
    if current:
        groups.append(current)
    items = [_work('assess_documents', spans, questions) for spans in groups or [[]]]
    if len(items) > 1:
        # 整合保留所有源窗口与真实草稿依赖；不会把首片当成全篇。
        spans = [span for group in groups for span in group]
        items.append(_work('reconcile_evidence', spans, questions,
                           [item['work_key'] for item in items]))
    return {'plan_id': 'assessment-' + digest(canonical(items))[:40], 'input_ref': input_ref,
            'text_window_codepoints': budget, 'items': items}


def _identity_gaps(identity, protocol):
    unknown = [field for field in ('provider', 'model_name', 'model_version', 'training_cutoff')
               if identity[field]['status'] == 'unknown']
    reasons = []
    if unknown:
        reasons.append('Unknown analyst identity fields: ' + ', '.join(unknown))
    if protocol['interpretation_mode'] == 'historical_exploration':
        reasons.append('Historical exploration cannot exclude model training contamination')
    if not reasons:
        return []
    return [{'rule_id': 'A13', 'field_or_object': '/analyst_identity',
             'reason': '; '.join(reasons), 'owner': 'producer',
             'required_evidence': ['protocol_definition'], 'affected_uses': ['pit_training']}]


def _materialize(ctx, draft, roles, inherited, identity, protocol):
    # 原样保留模型和上游限制，并补全冻结身份及历史解释模式要求的限制。
    identity_gaps = _identity_gaps(identity, protocol)
    for gap in identity_gaps:
        ctx.schemas.validate_type('Gap', gap)
    gaps = {canonical(gap): gap for gap in draft['unresolved_questions'] + inherited + identity_gaps}
    references = {}
    for name, (field, _, schema_id) in TABLES.items():
        value = draft['economic_observations' if name == 'observations' else name]
        reference = ctx.put_json('assessment-' + name + '.json', value, schema_id)
        references[field] = ctx.dataset_ref(value, schema_id, reference['uri'])
    payload = dict(references, unresolved_questions=list(gaps.values()),
                   analyst_identity_ref=roles['analyst_identity_ref'],
                   assessment_protocol_ref=roles['assessment_protocol_ref'])
    payload_ref = ctx.put_json('assessment.json', payload, 'investment.sc03.research_assessment')
    candidate = {'contract_id': 'investment.candidate_output', 'contract_version': 1,
                 'run_id': ctx.run_id, 'stage_id': 'SC03',
                 'output_id': 'assessment-' + digest(canonical([ctx.input_ref, ctx.attempt_no]))[:40],
                 'created_at': now(), 'processing_state': 'completed',
                 'input_refs': ctx.input['input_refs'], 'scope_ref': ctx.input['scope_ref'],
                 'method_ref': ctx.method_ref, 'payload_ref': payload_ref,
                 'datasets': list(references.values()), 'evidence_refs': [],
                 'gaps': payload['unresolved_questions']}
    ctx.schemas.validate('investment.candidate_output', candidate)
    return candidate


def produce(ctx):
    """在同attempt继续真实Host工作；候选不包含验证结论或私有工作材料。"""
    require(ctx.role == 'producer', 'A01', '/role', 'Assessment requires producer context')
    roles = {item['role']: item['ref'] for item in ctx.input['input_refs']}
    corpus = ctx.read_role('evidence_corpus_ref', 'investment.evidence_corpus')
    protocol = ctx.read_role('assessment_protocol_ref', 'investment.sc03.assessment_protocol')
    identity = ctx.read_role('analyst_identity_ref', 'investment.sc03.analyst_identity')
    parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], 'investment.sc03.assessment_parameters')
    require(identity['role'] == 'producer' and all(parameters[key] == roles[key]
            for key in ('assessment_protocol_ref', 'analyst_identity_ref')),
            'A01', '/parameters', 'Frozen analyst or protocol binding differs')
    documents = ctx.resolver.resolve(corpus['documents_ref'], 'investment.news_documents')['rows']
    require(len({row['version_id'] for row in documents}) == len(documents),
            'A02', '/documents', 'Duplicate document version')
    by_version = {row['version_id']: row for row in documents}
    bodies = {version: ctx.resolver.resolve(row['body_ref'], 'investment.news_body')
              for version, row in by_version.items()}
    questions = [row['question_id'] for row in protocol['questions']]
    work = plan(ctx.input_ref, documents, bodies, questions, parameters['text_window_codepoints'])
    plan_ref = ctx.put_json('assessment-work-plan.json', work, 'investment.sc03.assessment_work_plan')
    upstream = [ctx.upstream_receipt(item['role']) for item in ctx.definition['input_roles']
                if item['role'] in roles and item['source']['kind'] == 'upstream_output']
    inherited = [gap for _, receipt in upstream for gap in receipt['blockers']]
    feedback_refs = []
    for value in ctx.feedback:
        number = value['attempt_no']
        require(1 <= number < ctx.attempt_no, 'RS03', '/feedback',
                'Feedback is not from an earlier attempt', 'identity_mismatch')
        reference = ctx.resolver.path_ref(ctx.attempt_dir.parent / f'attempt-{number:02}' / 'failure.json',
                                          'investment.research_failure')
        failure = ctx.resolver.resolve(reference, 'investment.research_failure')
        require(canonical(failure) == canonical(value) and all(failure[key] == expected for key, expected in
                {'run_id': ctx.run_id, 'stage_id': ctx.stage_id, 'invocation_id': ctx.invocation_id,
                 'input_ref': ctx.input_ref, 'attempt_no': number}.items()),
                'RS03', '/feedback', 'Feedback differs from its exact frozen attempt binding', 'identity_mismatch')
        feedback_refs.append(reference)
    base = [plan_ref, ctx.input['scope_ref'], ctx.input['parameters_ref']] + list(roles.values())
    base += [corpus[key] for key in ('documents_ref', 'coverage_ref', 'pending_items_ref')]
    base += [reference for reference, _ in upstream] + feedback_refs
    outputs, final = {}, None
    for item in work['items']:
        references = base + [by_version[span['document_version_id']]['body_ref']
                             for span in item['document_spans']]
        references += [outputs[key] for key in item['related_work_keys']]
        references = list({reference['uri']: reference for reference in references}.values())
        response = ctx.host_response(item['step_id'], work_key=item['work_key'])
        if response is None:
            return ctx.request_host(item['step_id'], references, work_key=item['work_key'])
        ctx.schemas.validate('investment.sc03.assessment_draft', response)
        matches = [reference for reference in ctx.resolver.host_output_refs if reference['uri'].endswith(
            'host-' + item['step_id'] + '-' + item['work_key'] + '-output.json')]
        require(len(matches) == 1, 'A11', '/host_response', 'Draft lacks actual bound host output')
        outputs[item['work_key']], final = matches[0], response
    require(len(final['dispositions']['rows']) == len(questions)
            and {row['question_id'] for row in final['dispositions']['rows']} == set(questions),
            'A10', '/dispositions', 'Final assessment must retain every frozen research question exactly once')
    return _materialize(ctx, final, roles, inherited, identity, protocol)
