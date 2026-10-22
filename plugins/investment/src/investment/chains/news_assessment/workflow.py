"""SC03生产工作计划：完整原文分窗、实际宿主分析及跨材料整合。"""
from __future__ import annotations

from investment.contracts import canonical, digest, require
from .contracts import TABLES
from .draft_codec import compile_draft
from .session_executor import execute_sessions
from .source_layouts import SOURCE_LAYOUT_PROVIDERS, source_layouts


def work_item(step, spans, questions, targets=(), dependencies=()):
    scope = {'step_id': step,
             'document_spans': sorted(spans, key=lambda row: (row['document_version_id'], row['text_field'], row['start'])),
             'question_ids': sorted(questions), 'target_keys': sorted(set(targets)),
             'related_work_keys': sorted(set(dependencies))}
    return dict(scope, work_key=digest(canonical(scope)))


def source_plan(input_ref, documents, bodies, questions, budget, layouts):
    """按真实来源分配独立session；窗口仅定位原文字段，不拆外部执行。"""
    by_source = {}
    for version, body in sorted(bodies.items()):
        source_id = documents[version]['source_id']
        partition = version if source_id in SOURCE_LAYOUT_PROVIDERS else ''
        spans = by_source.setdefault((source_id, partition), [])
        for field in ('text', 'title'):
            for start in range(0, len(body[field]), budget):
                end = min(start + budget, len(body[field]))
                spans.append({'document_version_id': version, 'text_field': field, 'start': start, 'end': end})
    groups = [work_item('assess_documents', spans, questions) for _, spans in sorted(by_source.items())]
    if not groups:
        groups = [work_item('assess_documents', [], questions)]
    return {'plan_id': 'assessment-source-' + digest(canonical(groups))[:40], 'input_ref': input_ref,
            'text_window_codepoints': budget, 'items': groups, 'source_layouts': layouts}


def empty_draft():
    return {**{('economic_observations' if name == 'observations' else name): {'rows': []} for name in TABLES},
            'unresolved_questions': []}


def merge_drafts(drafts):
    """只合并同主键同内容；语义去重与问题最终处置交给实际整合。"""
    merged = empty_draft()
    for name, (_, primary, _) in TABLES.items():
        field = 'economic_observations' if name == 'observations' else name
        rows = {}
        for draft in drafts:
            for row in draft[field]['rows']:
                previous = rows.get(row[primary])
                require(previous is None or previous == row, 'A02', '/' + field,
                        'Source drafts reused an identity for different content')
                rows[row[primary]] = row
        merged[field]['rows'] = [rows[key] for key in sorted(rows)]
    gaps = {canonical(gap): gap for draft in drafts for gap in draft['unresolved_questions']}
    merged['unresolved_questions'] = [gaps[key] for key in sorted(gaps)]
    return merged


def quotes(value):
    if isinstance(value, dict):
        if {'quote_id', 'body_ref', 'document_version_id', 'text_field', 'start', 'end', 'quote'} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from quotes(child)
    elif isinstance(value, list):
        for child in value:
            yield from quotes(child)


def check_quotes(draft, documents, bodies, *, source_item=None):
    for quote in quotes(draft):
        version = quote['document_version_id']
        require(version in documents and quote['body_ref'] == documents[version]['body_ref'],
                'A03', '/quote', 'Draft quote is not from the current exact source version')
        text = bodies[version][quote['text_field']]
        require(0 <= quote['start'] < quote['end'] <= len(text)
                and text[quote['start']:quote['end']] == quote['quote'],
                'A03', '/quote', 'Draft quote differs from the exact Unicode source slice')
        if source_item is not None:
            require(any(span['document_version_id'] == version and span['text_field'] == quote['text_field']
                        and span['start'] <= quote['start'] < quote['end'] <= span['end']
                        for span in source_item['document_spans']),
                    'A03', '/quote', 'Source response cites outside its assigned window')


def needs_reconciliation(source, draft, questions):
    if len(source['items']) > 1 or any(draft[name]['rows'] for name in ('events', 'hypotheses', 'counterevidence')):
        return True
    if len(draft['economic_observations']['rows']) > 1:
        return True
    if any(len(row['document_version_ids']) > 1 or row['conflict_refs']
           or any(link['kind'] == 'fact' for link in row['support_refs']) for row in draft['facts']['rows']):
        return True
    actual = [row['question_id'] for row in draft['dispositions']['rows']]
    return sorted(actual) != sorted(questions)


def relation_plan(source, draft, questions):
    spans = [span for item in source['items'] for span in item['document_spans']]
    targets = []
    for name, (_, primary, _) in TABLES.items():
        field = 'economic_observations' if name == 'observations' else name
        for row in draft[field]['rows']:
            targets.append(canonical([name, row[primary], '', 'integration']).decode('utf-8'))
    for question in questions:
        targets.append(canonical(['question', question, '', 'coverage']).decode('utf-8'))
    by_field = {}
    for span in spans:
        key = span['document_version_id'], span['text_field']
        previous = by_field.get(key)
        if previous is not None:
            targets.append(canonical(['boundary', ':'.join(key) + ':' + str(span['start']), '', 'cross_window']).decode('utf-8'))
        by_field[key] = span
    # 整合工作持有所有准确原文窗口和全部初稿目标；窗口仍逐个受冻结预算约束。
    # 不截断关系全集，无法在实际宿主上下文完成时按真实宿主失败处理。
    item = work_item('reconcile_evidence', spans, questions, targets, [row['work_key'] for row in source['items']])
    return {'plan_id': 'assessment-integration-' + digest(canonical(item))[:40], 'input_ref': source['input_ref'],
            'text_window_codepoints': source['text_window_codepoints'], 'items': source['items'] + [item],
            'source_layouts': source['source_layouts']}


def produce_draft(ctx):
    roles = {row['role']: row['ref'] for row in ctx.input['input_refs']}
    corpus = ctx.read_role('evidence_corpus_ref', 'investment.evidence_corpus')
    protocol = ctx.read_role('assessment_protocol_ref', 'investment.sc03.assessment_protocol')
    parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], 'investment.sc03.assessment_parameters')
    require(all(parameters[key] == roles[key] for key in ('assessment_protocol_ref', 'analyst_identity_ref')),
            'A01', '/parameters', 'Protocol or identity differs from frozen input roles')
    questions = [row['question_id'] for row in protocol['questions']]
    require(len(set(questions)) == len(questions), 'A01', '/questions', 'Duplicate research question')
    rows = ctx.resolver.resolve(corpus['documents_ref'], 'investment.news_documents')['rows']
    require(len({row['version_id'] for row in rows}) == len(rows), 'A02', '/documents', 'Duplicate document version')
    documents = {row['version_id']: row for row in rows}
    bodies = {key: ctx.resolver.resolve(row['body_ref'], 'investment.news_body') for key, row in documents.items()}
    layouts = source_layouts(ctx, documents, bodies)
    source = source_plan(ctx.input_ref, documents, bodies, questions, parameters['text_window_codepoints'], layouts)
    source_ref = ctx.put_json('assessment-source-work-plan.json', source, 'investment.sc03.assessment_work_plan')
    base = [ctx.input['scope_ref'], ctx.input['parameters_ref']] + [row['ref'] for row in ctx.input['input_refs']]
    for key in ('documents_ref', 'pending_items_ref', 'coverage_ref', 'opportunity_inventory_ref', 'source_scope_ref', 'window_ref'):
        if key in corpus:
            ctx.resolver.resolve(corpus[key])
            base.append(corpus[key])
    for role in ctx.definition['input_roles']:
        if role['role'] in roles and role['source']['kind'] == 'upstream_output':
            reference, _ = ctx.upstream_receipt(role['role'])
            base.append(reference)
    jobs, output_refs = [], []
    for item in source['items']:
        refs = base + [source_ref] + [documents[span['document_version_id']]['body_ref'] for span in item['document_spans']]
        versions = {span['document_version_id'] for span in item['document_spans']}
        refs += [documents[version]['raw_ref'] for version in sorted(versions)
                 if documents[version]['source_id'] in SOURCE_LAYOUT_PROVIDERS]
        refs = list({ref['uri']: ref for ref in refs}.values())
        jobs.append(('assess_documents', refs, item['work_key']))
    drafts = [compile_draft(result, ctx.schemas, layouts) for result in execute_sessions(ctx, jobs)]
    for item, response in zip(source['items'], drafts):
        check_quotes(response, documents, bodies, source_item=item)
        matches = [ref for ref in ctx.resolver.host_output_refs if
                   ref['uri'].endswith('host-assess_documents-' + item['work_key'] + '-output.json')]
        require(len(matches) == 1, 'A11', '/host_response', 'Source draft lacks exact bound output identity')
        output_refs.append(matches[0])
    merged = merge_drafts(drafts)
    if not needs_reconciliation(source, merged, questions):
        return merged
    plan = relation_plan(source, merged, questions)
    plan_ref = ctx.put_json('assessment-work-plan.json', plan, 'investment.sc03.assessment_work_plan')
    merged_ref = ctx.put_json('assessment-initial-draft.json', merged, 'investment.sc03.assessment_draft')
    item = plan['items'][-1]
    refs = base + [plan_ref, merged_ref] + output_refs + [row['body_ref'] for row in documents.values()]
    refs += [row['raw_ref'] for row in documents.values() if row['source_id'] in SOURCE_LAYOUT_PROVIDERS]
    refs = list({ref['uri']: ref for ref in refs}.values())
    generated = execute_sessions(ctx, [('reconcile_evidence', refs, item['work_key'])])[0]
    response = compile_draft(generated, ctx.schemas, layouts)
    check_quotes(response, documents, bodies)
    return response
