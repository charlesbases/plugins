"""SC08 候选的对象关系与完整规则用途门；尚不提供完整验证入口。"""
from __future__ import annotations

from investment.contracts import canonical, digest, is_known, require


def _unique_rows(rows, fields, pointer):
    keys = [canonical([row[field] for field in fields]) for row in rows]
    require(len(keys) == len(set(keys)), 'Q17', pointer, 'Duplicate declared logical key', 'identity_mismatch')


def _exported_evidence(ctx, reference):
    require(reference in ctx.candidate['evidence_refs'], 'Q17', '/evidence_refs',
            'Business analysis reference is not exported by the candidate', 'invalid_reference')
    require(not reference['uri'].rsplit('/', 1)[-1].startswith('host-'), 'Q17', '/evidence_refs',
            'Private host records cannot be business evidence', 'use_not_allowed')


def load_candidate_inventory(ctx, bound):
    """读取候选声明的真实对象及表，保留原行和缺口，不授予任何 Q 通过。"""
    require(ctx.stage_id == 'SC08' and ctx.role == 'validator' and ctx.candidate is not None,
            'Q01', '/context', 'SC08 validator candidate context is required', 'use_not_allowed')
    payload = ctx.resolver.resolve(ctx.candidate['payload_ref'], 'investment.sc08.qualification_verdict')
    refs, roles, parameters = bound['input_refs'], bound['roles'], bound['parameters']
    policy, forecast = roles['frozen_policy_ref'], roles['forecast_ref']
    protocol_ref = parameters['experiment_protocol_ref']
    view_ref = parameters['independent_validation_view_ref']
    model_ref = forecast['model_rule_ref']
    for field, expected in (
        ('verified_policy_ref', refs['frozen_policy_ref']),
        ('verified_actions_ref', policy['current_actions_ref']),
        ('verified_model_rule_ref', model_ref),
        ('verified_selected_rule_ref', policy['selected_rule_ref']),
        ('protocol_ref', protocol_ref),
    ):
        require(payload[field] == expected, 'Q01', '/payload/' + field,
                'Verified object differs from the frozen input', 'identity_mismatch')

    objects = {}
    for name in ('evaluation_period', 'test_access_audit', 'repeated_test_accounting',
                 'assumption_report', 'comparison_scope_validation'):
        objects[name] = ctx.resolver.resolve(payload[name + '_ref'], 'investment.sc08.' + name)
    period = objects['evaluation_period']
    require(period['protocol_ref'] == protocol_ref and period['independent_view_ref'] == view_ref
            and period['evaluation_cutoff_at'] == parameters['evaluation_cutoff_at']
            and period['evidence_kind'] == payload['evidence_kind']
            and period['account_scope'] == payload['account_scope'],
            'Q01', '/evaluation_period_ref', 'Evaluation period binding differs', 'identity_mismatch')
    for name, view_field in (('test_access_audit', 'independent_view_ref'),
                             ('repeated_test_accounting', 'view_ref')):
        value = objects[name]
        require(value['protocol_ref'] == protocol_ref and value['model_rule_ref'] == model_ref
                and value['policy_ref'] == refs['frozen_policy_ref'] and value[view_field] == view_ref,
                'Q01', '/' + name + '_ref', 'Evaluation governance binding differs', 'identity_mismatch')
    scope = objects['comparison_scope_validation']
    require(scope['universe_ref'] == refs['instrument_universe_ref']
            and scope['discovery_audit_ref'] == roles['instrument_universe_ref']['discovery_audit_ref']
            and scope['comparison_inventory_ref'] == policy['comparison_inventory_ref'],
            'Q01', '/comparison_scope_validation_ref', 'Comparison scope binding differs', 'identity_mismatch')

    evidence = {}

    def read_analysis(reference, schema_id):
        _exported_evidence(ctx, reference)
        key = canonical(reference)
        if key not in evidence:
            evidence[key] = {'ref': reference, 'value': ctx.resolver.resolve(reference, schema_id)}
        require(reference['schema_id'] == schema_id, 'Q17', '/evidence_refs',
                'Business analysis schema differs', 'identity_mismatch')
        return evidence[key]['value']

    assessment = read_analysis(objects['assumption_report']['assessment_ref'], 'investment.sc08.scientific_assessment')
    require(assessment['protocol_ref'] == protocol_ref, 'Q01', '/assumption_report_ref/assessment_ref',
            'Scientific assessment protocol differs', 'identity_mismatch')

    tables, table_refs = {}, {}
    for table in ctx.schemas.stage_registries['SC08']['tables']:
        name = table['schema_id'].rsplit('.', 1)[-1]
        reference = scope['scope_checks_ref'] if name == 'scope_checks' else payload[name + '_ref']
        require(sum(export == reference for export in ctx.candidate['datasets']) == 1,
                'Q17', '/datasets/' + name, 'Required table must have one exact candidate export', 'identity_mismatch')
        tables[name] = ctx.resolver.resolve(reference, table['schema_id'])
        table_refs[name] = reference

    origins = tables['origin_replays']['rows']
    actions = {row['action_id']: row for row in tables['replay_actions']['rows']}
    ledger = {row['event_id']: row for row in tables['independent_ledger']['rows']}
    pairs = {row['pair_id']: row for row in tables['baseline_comparisons']['rows']}
    _unique_rows(origins, ('origin_at', 'horizon_id'), '/origin_replays')
    _unique_rows(list(pairs.values()), ('origin_at', 'horizon_id', 'baseline_id'), '/baseline_comparisons')
    _unique_rows(tables['scope_checks']['rows'], ('object_ref', 'object_id', 'peer_group_id'), '/scope_checks')
    origin_keys = {(row['origin_at'], row['horizon_id']) for row in origins}
    for origin in origins:
        require(origin['model_rule_ref'] == model_ref and origin['policy_rule_ref'] == policy['policy_rule_ref'],
                'Q17', '/origin_replays', 'Origin rule binding differs', 'identity_mismatch')
        for action_id in origin['action_ids']:
            require(action_id in actions, 'Q17', '/origin_replays/action_ids',
                    'Origin action is absent from replay actions', 'invalid_reference')
            action = actions[action_id]
            require(action['origin_at'] == origin['origin_at'], 'Q17', '/origin_replays/action_ids',
                    'Replay action origin differs', 'identity_mismatch')
            if is_known(origin['selected_rule_ref']):
                require(action['selected_rule_ref'] == origin['selected_rule_ref']['value'],
                        'Q17', '/origin_replays/action_ids', 'Replay action selected rule differs', 'identity_mismatch')
        for reference in origin['model_execution_refs']:
            execution = read_analysis(reference, 'investment.sc08.replay_computation')
            require(execution['origin_at'] == origin['origin_at'] and origin['horizon_id'] in execution['horizon_ids']
                    and execution['protocol_ref'] == protocol_ref and execution['model_rule_ref'] == model_ref
                    and execution['policy_space_ref'] == policy['policy_rule_ref'],
                    'Q17', '/origin_replays/model_execution_refs', 'Replay computation binding differs', 'identity_mismatch')
    for reconciliation in tables['ledger_reconciliation']['rows']:
        require(reconciliation['event_id'] in ledger, 'Q17', '/ledger_reconciliation/event_id',
                'Reconciliation event is absent from the independent ledger', 'invalid_reference')
    for pair in pairs.values():
        require((pair['origin_at'], pair['horizon_id']) in origin_keys,
                'Q17', '/baseline_comparisons', 'Paired outcome has no matching origin and horizon', 'invalid_reference')
        for event_id in pair['ledger_event_ids']:
            require(event_id in ledger, 'Q17', '/baseline_comparisons/ledger_event_ids',
                    'Paired ledger event is absent', 'invalid_reference')
            event = ledger[event_id]
            require(is_known(event['origin_at']) and event['origin_at']['value'] == pair['origin_at'],
                    'Q17', '/baseline_comparisons/ledger_event_ids', 'Paired ledger origin differs', 'identity_mismatch')
            policy_path = event['path_kind'] == 'policy' and event['path_id'] == pair['policy_path_id']
            baseline_path = (event['path_kind'] == 'baseline' and event['path_id'] == pair['baseline_path_id']
                             and is_known(event['baseline_id']) and event['baseline_id']['value'] == pair['baseline_id'])
            require(policy_path or baseline_path, 'Q17', '/baseline_comparisons/ledger_event_ids',
                    'Paired ledger path or baseline differs', 'identity_mismatch')

    registered_claims = bound['protocol']['repeated_test_policy']['claims']
    for name in ('net_advantage_intervals', 'risk_estimates'):
        for claim in tables[name]['rows']:
            matches = [item for item in registered_claims if item['claim_id'] == claim['claim_id']]
            require(len(matches) == 1, 'Q17', '/' + name + '/claim_id',
                    'Claim is absent or ambiguous in the frozen protocol', 'identity_mismatch')
            require(all(claim[field] == matches[0][field]
                        for field in ('metric_id', 'horizon_id', 'baseline_id', 'acceptance_rule_ids')),
                    'Q17', '/' + name, 'Claim fields differ from the frozen protocol', 'identity_mismatch')
            require(claim['assumption_report_ref'] == payload['assumption_report_ref'],
                    'Q17', '/' + name + '/assumption_report_ref', 'Claim assumption report differs', 'identity_mismatch')
            for pair_id in claim['eligible_pair_ids']:
                require(pair_id in pairs, 'Q17', '/' + name + '/eligible_pair_ids',
                        'Claim pair is absent from baseline comparisons', 'invalid_reference')
                pair = pairs[pair_id]
                require(pair['horizon_id'] == claim['horizon_id'] and
                        (not is_known(claim['baseline_id']) or pair['baseline_id'] == claim['baseline_id']['value']),
                        'Q17', '/' + name + '/eligible_pair_ids', 'Claim pair horizon or baseline differs', 'identity_mismatch')
            if is_known(claim['resample_plan_ref']):
                plan = read_analysis(claim['resample_plan_ref']['value'], 'investment.sc08.resample_plan')
                require(plan['horizon_id'] == claim['horizon_id'], 'Q17', '/' + name + '/resample_plan_ref',
                        'Resample plan horizon differs', 'identity_mismatch')
                for pair_id in plan['paired_ids']:
                    require(pair_id in pairs and pairs[pair_id]['horizon_id'] == plan['horizon_id'],
                            'Q17', '/' + name + '/resample_plan_ref', 'Resample pair binding differs', 'identity_mismatch')
    for row in tables['scope_checks']['rows']:
        target_id = 's-' + digest(canonical(['scope', row['object_ref'], row['object_id'], row['peer_group_id']]))
        require(row['scope_check_id'] == target_id, 'Q17', '/scope_checks/scope_check_id',
                'Scope target identity differs', 'identity_mismatch')
        for reference in row['execution_refs']:
            _exported_evidence(ctx, reference)

    return {'payload': payload, 'objects': objects, 'tables': tables, 'table_refs': table_refs,
            'evidence': list(evidence.values())}


def rule_use_gate(ctx, actual_checks):
    """只接收完整真实检查，按冻结顺序返回用途；不构造检查或凭证。"""
    require(ctx.stage_id == 'SC08' and ctx.role == 'validator' and ctx.candidate is not None,
            'Q17', '/context', 'SC08 validator candidate context is required', 'use_not_allowed')
    checks = list(actual_checks)
    for check in checks:
        ctx.schemas.validate_type('Check', check)
    rule_ids = [check['rule_id'] for check in checks]
    require(rule_ids == ctx.definition['required_rules'] and len(rule_ids) == len(set(rule_ids)),
            'Q17', '/checks', 'Complete ordered unique SC08 rules are required', 'identity_mismatch')
    verdicts = {check['rule_id']: check['verdict'] for check in checks}
    require('not_evaluated' not in verdicts.values(), 'Q17', '/checks',
            'Required SC08 rule was not evaluated', 'missing_evidence')
    critical = all(verdicts[rule] == 'passed' for rule in ctx.definition['critical_rules'])
    return [item['use'] for item in ctx.definition['use_rules'] if critical
            and all(verdicts[rule] == 'passed' for rule in item['required_pass_rules'])]
