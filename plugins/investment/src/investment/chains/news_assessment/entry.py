"""SC03宿主入口：由任务问题建立协议，核真实上游交接后保存本轮输入。"""
from __future__ import annotations

from pathlib import Path

from investment.contracts import canonical, digest, instant, now, require, unknown
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.research.controller import _input, _upstream_binding
from investment.research.references import ResearchResolver
from investment.runtime.runs import atomic


def prepare_input(root, *, mode, run_id, invocation_id, evidence_corpus_ref, scope_ref,
                  task_questions, text_window_codepoints, decision_at, information_cutoff_at,
                  interpretation_mode='current', account_risk_objects_ref=None, account_questions=()):
    """接收宿主从任务生成的问题和上下文预算，不要求用户填写模型技术字段。"""
    selected = guard_namespace(root, mode)
    schemas = ResearchSchemas(('SC03',))
    schemas.validate_type('PathID', run_id)
    schemas.validate_type('PathID', invocation_id)
    require(instant(information_cutoff_at) <= instant(decision_at), 'A01', '/decision_at', 'Cutoff exceeds decision time')
    resolver = ResearchResolver(selected, schemas, mode=mode)
    resolver.resolve(scope_ref)
    require(not account_questions or account_risk_objects_ref is not None, 'A01', '/questions', 'Account questions require the actual account input')
    questions = []
    for basis, texts, reference in (('research_scope', task_questions, scope_ref),
                                    ('account_risk', account_questions, account_risk_objects_ref)):
        for text in texts:
            key = digest(canonical([basis, text]))[:40]
            questions.append({'question_id': 'question-' + key, 'question': text, 'basis': basis, 'basis_refs': [reference]})
    protocol = {'protocol_id': 'protocol-' + digest(canonical(questions))[:40], 'protocol_version': 'v1',
                'questions': questions, 'method_id': 'sc03-source-grounded-assessment', 'method_version': 'v1',
                'interpretation_mode': interpretation_mode, 'cross_check_policy': 'independent_origins_and_no_unresolved_conflict',
                'semantic_policy': 'independent_atomic_source_review_v1'}
    schemas.validate('investment.sc03.assessment_protocol', protocol)
    require(len({row['question_id'] for row in questions}) == len(questions), 'A01', '/questions', 'Duplicate task question')
    require(type(text_window_codepoints) is int and text_window_codepoints > 0,
            'A01', '/text_window_codepoints', 'Host must select a positive integer context budget')
    roles = {'evidence_corpus_ref': evidence_corpus_ref}
    if account_risk_objects_ref is not None:
        roles['account_risk_objects_ref'] = account_risk_objects_ref
    clock = {'run_id': run_id, 'information_cutoff_at': information_cutoff_at}
    for role in schemas.definition('SC03')['input_roles']:
        if role['role'] in roles:
            resolver.resolve(roles[role['role']], role['schema_id'])
            _upstream_binding(resolver, roles[role['role']], role['source'], clock)
    from zoneinfo import ZoneInfo
    day = instant(now()).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')
    # 相同调用不得通过跨日搬迁或重新建立身份来重置冻结事实。
    matches = list((selected / 'runs/sc03').glob('*/' + invocation_id + '/submitted-input.json'))
    require(len(matches) <= 1, 'A01', '/invocation_id', 'Invocation has multiple input locations')
    folder = matches[0].parent if matches else selected / 'runs/sc03' / day / invocation_id
    require(folder.resolve().is_relative_to(selected / 'runs/sc03'), 'RS01', '/input', 'Input directory escapes selected stage')
    submitted = folder / 'submitted-input.json'
    if submitted.exists():
        incoming = resolver.resolve(resolver.path_ref(submitted, 'investment.stage_input'))
        actual = {row['role']: row['ref'] for row in incoming['input_refs']}
        old_protocol = resolver.resolve(actual['assessment_protocol_ref'])
        parameters = resolver.resolve(incoming['parameters_ref'])
        require(all(incoming[key] == value for key, value in {'run_id': run_id, 'invocation_id': invocation_id,
                'scope_ref': scope_ref, 'decision_at': decision_at, 'information_cutoff_at': information_cutoff_at}.items())
                and old_protocol == protocol and parameters['text_window_codepoints'] == text_window_codepoints
                and all(actual.get(role) == ref for role, ref in roles.items())
                and ('account_risk_objects_ref' in actual) == (account_risk_objects_ref is not None),
                'A01', '/input', 'Existing invocation inputs cannot change')
        _input(resolver, resolver.path_ref(submitted, 'investment.stage_input'), 'SC03', run_id, invocation_id)
        return submitted

    def save(name, value, schema_id):
        schemas.validate(schema_id, value)
        atomic(folder / name, canonical(value), immutable=True)
        return resolver.path_ref(folder / name, schema_id)

    identity = {'identity_id': 'analyst-' + digest(canonical(invocation_id))[:40], 'role': 'producer',
                **{field: unknown('verified_host_metadata_not_available_before_actual_model_work')
                   for field in ('provider', 'model_name', 'model_version', 'training_cutoff')},
                'identity_evidence_refs': [], 'generated_at': now(), 'host_invocation_id': invocation_id}
    protocol_ref = save('protocol.json', protocol, 'investment.sc03.assessment_protocol')
    identity_ref = save('identity.json', identity, 'investment.sc03.analyst_identity')
    parameters_ref = save('parameters.json', {'assessment_protocol_ref': protocol_ref, 'analyst_identity_ref': identity_ref,
                          'text_window_codepoints': text_window_codepoints}, 'investment.sc03.assessment_parameters')
    roles.update(assessment_protocol_ref=protocol_ref, analyst_identity_ref=identity_ref)
    incoming = {'contract_id': 'investment.stage_input', 'contract_version': 1, 'stage_id': 'SC03', 'run_id': run_id,
                'invocation_id': invocation_id, 'decision_at': decision_at, 'information_cutoff_at': information_cutoff_at,
                'scope_ref': scope_ref, 'input_refs': [{'role': row['role'], 'ref': roles[row['role']]}
                   for row in schemas.definition('SC03')['input_roles'] if row['role'] in roles], 'parameters_ref': parameters_ref}
    reference = save('submitted-input.json', incoming, 'investment.stage_input')
    _input(resolver, reference, 'SC03', run_id, invocation_id)
    return submitted
