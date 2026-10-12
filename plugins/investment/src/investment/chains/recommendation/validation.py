"""SC09 独立 R01—R18 分发；控制器独占用途封存与失败预算。"""
from __future__ import annotations

from investment.contracts import ContractError, canonical, instant, is_known, now
from .contracts import Issue, Snapshot, ensure, value, parameter_ids
from . import deterministic, finance, temporal, semantic_review


def recovery(s):
    if s.payload['decision_kind'] == 'no_action':
        ensure(all(a['side'] == 'hold' for a in s.actions), 'No-action must preserve original complete hold actions')
    if s.payload['decision_kind'] != 'risk_recovery':
        return
    ensure(s.policy['permission_request'] == 'risk_restoration'
           and s.constraints['ordinary_or_restoration'] == 'risk_restoration', 'Ordinary policy permission cannot authorize risk recovery')
    original = value(s.constraints['restoration_permission_ref'], 'Original independent restoration authorization')
    review = s.recovery
    ensure(review is not None, 'Actual restoration extraction review is missing', missing=True)
    ensure(review['original_permission_ref'] == original
           and review['policy_ref'] == s.refs['frozen_policy_ref'] and review['actions_ref'] == s.policy['current_actions_ref']
           and review['account_snapshot_ref'] == s.refs['transaction_context_ref']
           and review['qualification_ref'] == s.refs['qualification_ref'], 'Restoration authorization subject differs')
    s.ctx.resolver.resolve(original)
    for ref in review['authorization_refs'] + review['constraint_evidence_refs']:
        s.ctx.resolver.resolve(ref)
    ensure(review['verdict'] != 'contradicted', 'Actual restoration source contradicts this permission')
    ensure(review['verdict'] == 'supported', 'Original restoration permission is not established', missing=True)
    ensure(set(value(review['permitted_action_ids'], 'Original permitted actions')) == {a['action_id'] for a in s.actions},
           'Restoration extraction changed the exact permitted action set')
    ids = parameter_ids(s.constraints)
    ensure(ids <= set(value(review['constraint_parameter_ids'], 'Original authorized constraints')), 'Restoration extraction omits dependent constraints')
    ensure(instant(value(review['authorized_at'], 'Actual authorization time')) <= instant(s.payload['publication_checked_at']),
           'Restoration authorization did not yet exist')
    receipt = s.receipts['qualification_ref'][1]
    ensure('current_account_review' in receipt['allowed_uses'], 'Independent current-account recovery use is not granted', missing=True)
    checks = {row['rule_id']: row['verdict'] for row in receipt['checks']}
    ensure(all(checks.get(rule) == 'passed' for rule in ('Q02', 'Q06', 'Q13', 'Q18')),
           'Actual SC08 action/lot/constraint/validity recovery checks are insufficient', missing=True)


def advice(s, checks):
    kind = s.payload['decision_kind']
    if kind == 'observation_only':
        ensure(not s.tables['advisory_orders'] and s.payload['permission_request'] == 'observation_only',
               'Observation candidate asserts executable orders')
        raise Issue('Exact observation preserves original scope and limitations without decision-advice permission', 'insufficient_evidence')
    ensure(s.payload['permission_request'] == 'decision_advice' and s.payload['publication_status'] == 'eligible',
           'Candidate decision advice is not currently eligible')
    if kind in ('rebalance', 'no_action'):
        for role, use in [('transaction_context_ref', 'current_account_review'), ('dataset_bundle_ref', 'selection_comparison'),
                          ('frozen_policy_ref', 'selection_comparison'), ('qualification_ref', 'independent_evaluation')]:
            ensure(use in s.receipts[role][1]['allowed_uses'], 'Actual upstream permission missing: ' + role + '/' + use, missing=True)
        ensure(s.qualification['verdict'] == 'qualified' and s.qualification['permission_scope'] == 'complete_declared_scope'
               and s.qualification['account_scope'] == 'planning_budget_plus_registered_assets'
               and s.inventory['completeness_status'] == 'complete' and s.policy['selection_scope'] == 'complete_declared_space',
               'Ordinary advice lacks complete comparison/account/scientific qualification', missing=True)
    else:
        recovery(s)
    ensure(all(checks[rule]['verdict'] == 'passed' for rule in [f'R{i:02}' for i in range(3, 15)]),
           'A necessary independent publication/financial rule is not passed', missing=True)


def _gap(rule, reason, path='/recommendation'):
    return {'rule_id': rule, 'field_or_object': path, 'reason': reason[:2048], 'owner': 'validator',
            'required_evidence': ['source_bytes'], 'affected_uses': ['decision_advice']}


def _run(checks, blockers, rule, action, evidence):
    try:
        action()
        checks[rule] = {'rule_id': rule, 'verdict': 'passed', 'reason': 'Independent original-input and source check passed', 'evidence_refs': evidence}
    except Issue as error:
        checks[rule] = {'rule_id': rule, 'verdict': error.verdict, 'reason': str(error)[:2048], 'evidence_refs': evidence}
        blockers.append(_gap(rule, str(error), error.path))
    except ContractError as error:
        verdict = 'insufficient_evidence' if error.code in ('missing_evidence', 'use_not_allowed') else 'failed'
        checks[rule] = {'rule_id': rule, 'verdict': verdict, 'reason': str(error)[:2048], 'evidence_refs': evidence}
        blockers.append(_gap(rule, str(error)))


def validate(ctx):
    """仅在实际 validator 上下文运行；未获真实宿主响应返回正式 HostRequest。"""
    s = Snapshot(ctx)
    checked_at = now()
    checks, blockers = {}, list(s.inherited.values())
    evidence = s.evidence()
    actions = [
        ('R01', lambda: deterministic.identities(s)), ('R02', lambda: deterministic.bindings(s)),
        ('R03', lambda: deterministic.account_review(s)), ('R04', lambda: deterministic.parameters(s)),
        ('R05', lambda: deterministic.mappings(s)), ('R06', lambda: deterministic.quantities(s)),
        ('R07', lambda: deterministic.lots(s)), ('R08', lambda: finance.precision(s)),
        ('R09', lambda: finance.estimates(s)), ('R10', lambda: deterministic.dispositions(s)),
        ('R11', lambda: finance.funding(s)), ('R12', lambda: temporal.markets(s)),
        ('R13', lambda: temporal.validity(s, checked_at)), ('R14', lambda: recovery(s)),
        ('R15', lambda: deterministic.honest(s)), ('R17', lambda: deterministic.exports(s)),
    ]
    for rule, action in actions:
        _run(checks, blockers, rule, action, evidence)
    findings = []
    if any(checks[rule]['verdict'] == 'failed' for rule in ('R01', 'R02', 'R15', 'R17')):
        _run(checks, blockers, 'R16', lambda: ensure(False, 'Real semantic review cannot proceed after a proven structural/identity/honesty failure', missing=True), evidence)
    else:
        try:
            request, findings = semantic_review.request_or_reviews(s)
            if request is not None:
                return request
            _run(checks, blockers, 'R16', lambda: None, evidence)
        except (Issue, ContractError) as error:
            _run(checks, blockers, 'R16', lambda: _raise(error), evidence)
            findings = []
    final_findings = [row for row in findings if row['rule_id'] == 'R18']
    for finding in [row for row in findings if row['rule_id'] != 'R18']:
        rule = finding['rule_id']
        if finding['verdict'] == 'supported':
            continue
        verdict = 'failed' if finding['verdict'] == 'contradicted' else 'insufficient_evidence'
        if rule not in checks or checks[rule]['verdict'] == 'passed' or verdict == 'failed':
            checks[rule] = {'rule_id': rule, 'verdict': verdict, 'reason': finding['reason'][:2048], 'evidence_refs': evidence}
        blockers.extend(finding['gaps'])
        blockers.append(_gap(rule, finding['reason']))
    # 最终 receipt 时钟直接参加交集；宿主续行不沿用旧检查时钟。
    checked_at = now()
    if checks['R13']['verdict'] == 'passed':
        _run(checks, blockers, 'R13', lambda: temporal.validity(s, checked_at), evidence)
    _run(checks, blockers, 'R18', lambda: advice(s, checks), evidence)
    for finding in final_findings:
        if finding['verdict'] == 'supported':
            continue
        verdict = 'failed' if finding['verdict'] == 'contradicted' else 'insufficient_evidence'
        if checks['R18']['verdict'] == 'passed' or verdict == 'failed':
            checks['R18'] = {'rule_id': 'R18', 'verdict': verdict, 'reason': finding['reason'][:2048], 'evidence_refs': evidence}
        blockers.extend(finding['gaps'])
        blockers.append(_gap('R18', finding['reason']))
    if not findings and checks['R16']['verdict'] != 'passed':
        for rule in ('R03', 'R04', 'R09', 'R12', 'R13', 'R14', 'R18'):
            if checks[rule]['verdict'] == 'passed':
                checks[rule].update(verdict='insufficient_evidence', reason='Actual independent source review is incomplete')
                blockers.append(_gap(rule, 'Actual independent source review is incomplete'))
    critical = all(checks[rule]['verdict'] == 'passed' for rule in ctx.definition['critical_rules'])
    allowed = [item['use'] for item in ctx.definition['use_rules']
               if critical and all(checks[rule]['verdict'] == 'passed' for rule in item['required_pass_rules'])]
    all_passed = all(checks[rule]['verdict'] == 'passed' for rule in ctx.definition['required_rules'])
    result = {
        'validator_id': 'sc09-validator-v1', 'validator_version': 'v1', 'run_id': ctx.run_id, 'stage_id': 'SC09',
        'input_refs': ctx.input['input_refs'], 'candidate_output_ref': ctx.candidate_ref, 'validated_at': checked_at,
        'checks': [checks[rule] for rule in ctx.definition['required_rules']],
        'overall_verdict': 'passed' if all_passed else ('partial' if critical and allowed else 'failed'),
        'business_result': 'usable' if 'decision_advice' in allowed else ('observation_only' if critical and allowed else ('rejected' if not critical else 'blocked')),
        'allowed_uses': allowed, 'blockers': list({canonical(row): row for row in blockers}.values()),
    }
    ctx.schemas.validate('investment.validation_receipt', result)
    return result


def _raise(error):
    raise error
