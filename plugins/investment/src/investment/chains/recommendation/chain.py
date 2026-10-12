"""SC09生产入口：原动作映射、发布条件、正式评审和候选交接。"""

from investment.contracts import canonical, instant, is_known, na, now, require, unknown
from .production_inputs import gap, load_inputs, parameter_ids, present, stable_id
from .production_finance import build_orders
from .production_publication import build_checks, build_funding_conditions, intersect_bounds
from .production_sources import assess_sources


TABLES = ('advisory_orders', 'action_mappings', 'position_dispositions', 'funding_conditions', 'publication_checks')


def _dispositions(inputs, advice):
    rows = []
    existing = {row['member_id']: row for row in inputs.member_dispositions}
    objects = [('declared_member', row['member_id'], row['entity_id'], inputs.universe['members_ref'], row['member_id'])
               for row in inputs.members]
    objects.extend(('registered_position', row['instrument_id'], present(row['instrument_id']),
                    inputs.account['positions_ref'], row['instrument_id']) for row in inputs.positions)
    for kind, identifier, instrument, reference, source_id in objects:
        actions = [row for row in inputs.actions if is_known(instrument) and row['entity_id'] == instrument]
        upstream = existing.get(identifier) if kind == 'declared_member' else None
        excluded = bool(upstream and upstream['scope_status'] == 'excluded' and upstream['evidence_refs'])
        reason = upstream['reason'] if upstream else 'Retained original registered position; no external account claim.'
        issues = []
        if excluded:
            disposition = 'excluded'
        elif not is_known(instrument):
            disposition = 'unknown'
            issues.append(gap('R10', '/position_dispositions/' + identifier, 'Original member identity remains unknown.'))
        elif not advice:
            disposition = 'observation'
        elif any(row['side'] == 'buy' for row in actions):
            disposition = 'increase'
        elif any(row['side'] == 'sell' for row in actions):
            disposition = 'decrease'
        else:
            disposition = 'hold'
        rows.append({'disposition_id': stable_id('disposition-', [kind, identifier, reference]),
                     'scope_object_id': identifier, 'object_kind': kind, 'source_object_ref': reference,
                     'source_row_id': source_id, 'instrument_id': instrument,
                     'action_ids': [row['action_id'] for row in actions], 'disposition': disposition,
                     'qualification_scope': 'complete_declared_scope' if advice else 'limited_observation',
                     'reason': reason, 'evidence_refs': [reference, *(upstream['evidence_refs'] if upstream else [])],
                     'gaps': issues})
    return rows


def _ordinary_eligible(inputs):
    uses = {'transaction_context_ref': 'current_account_review', 'dataset_bundle_ref': 'selection_comparison',
            'frozen_policy_ref': 'selection_comparison', 'qualification_ref': 'independent_evaluation'}
    return (all(use in inputs.receipts[role][1]['allowed_uses'] for role, use in uses.items())
            and inputs.policy['permission_request'] == 'selection_comparison'
            and inputs.policy['selection_scope'] == 'complete_declared_space'
            and inputs.qualification['verdict'] == 'qualified'
            and inputs.qualification['permission_scope'] == 'complete_declared_scope'
            and inputs.qualification['account_scope'] == 'planning_budget_plus_registered_assets'
            and inputs.inventory['completeness_status'] == 'complete' and inputs.selected is not None
            and len(inputs.member_dispositions) == len(inputs.members)
            and {row['member_id'] for row in inputs.member_dispositions} == {row['member_id'] for row in inputs.members}
            and all((row['actually_compared'] and row['comparison_status'] == 'compared')
                    or (row['scope_status'] == 'excluded' and row['comparison_status'] == 'excluded' and row['evidence_refs'])
                    for row in inputs.member_dispositions))


def _recovery_eligible(inputs):
    review = inputs.recovery
    if inputs.policy['permission_request'] != 'risk_restoration' or review is None:
        return False
    if not all(is_known(review[field]) for field in ('permitted_action_ids', 'constraint_parameter_ids', 'authorized_at')):
        return False
    authorization = inputs.constraints['restoration_permission_ref']
    if not (inputs.constraints['ordinary_or_restoration'] == 'risk_restoration' and is_known(authorization)
            and authorization['value'] == review['original_permission_ref']
            and review['verdict'] == 'supported' and review['authorization_refs']
            and review['constraint_evidence_refs'] and review['permitted_use'] == 'risk_recovery_only'
            and review['policy_ref'] == inputs.refs['frozen_policy_ref']
            and review['qualification_ref'] == inputs.refs['qualification_ref']
            and review['account_snapshot_ref'] == inputs.refs['transaction_context_ref']
            and review['actions_ref'] == inputs.policy['current_actions_ref']
            and set(review['permitted_action_ids']['value']) == {row['action_id'] for row in inputs.actions}
            and parameter_ids(inputs.constraints) <= set(review['constraint_parameter_ids']['value'])
            and instant(review['authorized_at']['value']) <= instant(inputs.parameters['publication_checked_at'])):
        return False
    if not all('reporting' in receipt['allowed_uses'] for _, receipt in inputs.receipts.values()):
        return False
    if 'current_account_review' not in inputs.receipts['qualification_ref'][1]['allowed_uses']:
        return False
    checks = {row['rule_id']: row['verdict'] for row in inputs.receipts['qualification_ref'][1]['checks']}
    return all(checks.get(rule) == 'passed' for rule in ('Q02', 'Q06', 'Q13', 'Q18'))


def _table(ctx, name, rows):
    value = {'schema_version': 1, 'rows': rows}
    schema = 'investment.sc09.' + name
    reference = ctx.put_json(name + '.json', value, schema)
    return ctx.dataset_ref(value, schema, reference['uri'])


def produce(ctx):
    inputs = load_inputs(ctx)
    require(instant(inputs.parameters['publication_checked_at']) <= instant(ctx.input['decision_at']),
            'R14', '/publication_checked_at', 'Publication review cannot postdate this frozen decision')
    checks = build_checks(inputs)
    funding = build_funding_conditions(inputs)
    # 发布目标在模型工作前冻结；模型只评审真实来源，不重写条件或事实。
    checks_ref = _table(ctx, 'publication_checks', checks)
    provisional = _dispositions(inputs, False)
    request, source_gaps = assess_sources(inputs, provisional, checks, checks_ref)
    if request is not None:
        return request
    orders, assumptions, finance_gaps = build_orders(inputs)
    bound, endpoint, controlling, events, status = intersect_bounds(checks, now())
    issues = [*inputs.gaps, *source_gaps, *finance_gaps,
              *(item for row in checks for item in row['gaps']),
              *(item for row in provisional for item in row['gaps'])]
    ordinary, recovery = _ordinary_eligible(inputs), _recovery_eligible(inputs)
    if not ordinary and not recovery:
        issues.append(gap('R18', '/permission_request',
                          'Original complete comparison or independently authorized recovery gates are not all established.',
                          'protocol_definition'))
    active = [row for row in inputs.actions if row['side'] != 'hold']
    complete_orders = {row['source_action_id'] for row in orders} == {row['action_id'] for row in active}
    if not complete_orders:
        issues.append(gap('R05', '/advisory_orders', 'All original non-hold actions must be executable together; no partial publication.'))
    blocking = [item for item in issues if 'decision_advice' in item['affected_uses']]
    advice = status == 'eligible' and not blocking and complete_orders and (ordinary or recovery)
    kind = ('risk_recovery' if recovery else ('rebalance' if active else 'no_action')) if advice else 'observation_only'
    if not advice:
        orders = []
    condition_ids = [row['condition_id'] for row in funding if row['source_kind'] == 'current_plan_confirmation']
    for order in orders:
        order['execution_condition_ids'] = condition_ids
    mappings = []
    index = {row['source_action_id']: row for row in orders}
    for action in inputs.actions:
        order = index.get(action['action_id'])
        mappings.append({'source_action_id': action['action_id'], 'source_actions_ref': inputs.policy['current_actions_ref'],
                         'advisory_order_id': present(order['advisory_order_id']) if order else na('no_executable_order'),
                         'mapping_status': 'mapped' if order else ('hold' if action['side'] == 'hold' else 'observation'),
                         'reason': 'Original action retained exactly; orders require the complete current publication gates.',
                         'gaps': [] if order or action['side'] == 'hold' else issues})
    dispositions = _dispositions(inputs, advice)
    issues = list({canonical(item): item for item in issues}.values())
    tables = {'advisory_orders': orders, 'action_mappings': mappings, 'position_dispositions': dispositions,
              'funding_conditions': funding}
    references = {name: _table(ctx, name, rows) for name, rows in tables.items()}
    references['publication_checks'] = checks_ref
    payload = {'advice_id': stable_id('advice-', [ctx.input_ref, ctx.attempt_no]),
               'decision_at': ctx.input['decision_at'], 'published_at': unknown('not_yet_published'),
               'publication_checked_at': inputs.parameters['publication_checked_at'],
               'decision_kind': kind, 'publication_status': status if status != 'eligible' or advice else 'insufficient',
               'permission_request': 'decision_advice' if advice else 'observation_only',
               'account_snapshot_ref': inputs.refs['transaction_context_ref'],
               'market_snapshot_ref': inputs.bundle['current_market_snapshot_ref'],
               'policy_ref': inputs.refs['frozen_policy_ref'], 'qualification_ref': inputs.refs['qualification_ref'],
               'publication_context_ref': inputs.refs['publication_context_ref'],
               'risk_recovery_review_ref': inputs.parameters['risk_recovery_review_ref'],
               **{name + '_ref': references[name] for name in TABLES},
               'valid_until': bound, 'valid_until_endpoint': endpoint, 'controlling_check_ids': controlling,
               'event_invalidation_check_ids': events, 'actual_host_work_item_count': ctx.host_work_summary().completed_count('producer'),
               'gaps': issues, 'limitations': ['Review covers received registered facts and the exact declared scope; estimates are not executions.']}
    payload_ref = ctx.put_json('recommendation.json', payload, 'investment.sc09.recommendation_set')
    return {'contract_id': 'investment.candidate_output', 'contract_version': 1, 'run_id': ctx.run_id, 'stage_id': 'SC09',
            'output_id': payload['advice_id'], 'created_at': now(), 'processing_state': 'completed',
            'input_refs': ctx.input['input_refs'], 'scope_ref': ctx.input['scope_ref'], 'method_ref': ctx.method_ref,
            'payload_ref': payload_ref, 'datasets': [references[name] for name in TABLES],
            'evidence_refs': assumptions, 'gaps': issues}
