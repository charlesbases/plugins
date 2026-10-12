"""SC08 验证者的本轮输入绑定与上游用途事实；不评定业务规则。"""
from __future__ import annotations

from investment.contracts import instant, now, require


_NUMERICAL_USES = {
    'SC01': 'current_account_review',
    'SC04': 'selection_comparison',
    'SC05': 'independent_evaluation',
    'SC06': 'selection_comparison',
    'SC07': 'selection_comparison',
}


def read_role_uses(ctx):
    """保留真实上游凭证及高用途缺口，不把报告权限提升为评价权限。"""
    require(ctx.stage_id == 'SC08' and ctx.role == 'validator' and ctx.candidate is not None,
            'Q01', '/context', 'SC08 validator candidate context is required', 'use_not_allowed')
    input_refs = {item['role']: item['ref'] for item in ctx.input['input_refs']}
    uses = {}
    for role in ctx.definition['input_roles']:
        source = role['source']
        if source['kind'] != 'upstream_output':
            continue
        receipt_ref, receipt = ctx.upstream_receipt(role['role'])
        numerical_use = _NUMERICAL_USES[source['stage_id']]
        uses[role['role']] = {
            'input_ref': input_refs[role['role']],
            'receipt_ref': receipt_ref,
            'receipt': receipt,
            'allowed_uses': receipt['allowed_uses'],
            'missing_uses': [numerical_use] if numerical_use not in receipt['allowed_uses'] else [],
        }
    return uses


def load_bound_inputs(ctx):
    """核真实输入身份；独立视图仅读控制对象，不读取其数据引用。"""
    require(ctx.stage_id == 'SC08' and ctx.role == 'validator' and ctx.candidate is not None,
            'Q01', '/context', 'SC08 validator candidate context is required', 'use_not_allowed')
    refs = {item['role']: item['ref'] for item in ctx.input['input_refs']}
    roles = {item['role']: ctx.read_role(item['role'], item['schema_id'])
             for item in ctx.definition['input_roles']}
    parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], ctx.definition['parameters_schema_id'])
    for role in ctx.definition['input_roles']:
        name = role['role']
        require(parameters[name] == refs[name], 'Q01', '/parameters/' + name,
                'Parameter reference differs from the actual input role', 'identity_mismatch')

    bundle = roles['dataset_bundle_ref']
    forecast = roles['forecast_ref']
    policy = roles['frozen_policy_ref']
    protocol_ref = parameters['experiment_protocol_ref']
    require(all(reference == protocol_ref for reference in (
        ctx.input['scope_ref'], bundle['experiment_protocol_ref'],
        forecast['experiment_protocol_ref'], policy['experiment_protocol_ref'])),
        'Q01', '/experiment_protocol_ref', 'Protocol bindings differ', 'identity_mismatch')
    for actual, expected, pointer in (
        (forecast['dataset_bundle_ref'], refs['dataset_bundle_ref'], '/forecast/dataset_bundle_ref'),
        (forecast['instrument_universe_ref'], refs['instrument_universe_ref'], '/forecast/instrument_universe_ref'),
        (policy['account_snapshot_ref'], refs['transaction_context_ref'], '/policy/account_snapshot_ref'),
        (policy['forecast_ref'], refs['forecast_ref'], '/policy/forecast_ref'),
        (policy['universe_ref'], refs['instrument_universe_ref'], '/policy/universe_ref'),
        (parameters['independent_validation_view_ref'], bundle['independent_validation_view_ref'],
         '/independent_validation_view_ref'),
    ):
        require(actual == expected, 'Q01', pointer, 'Frozen input binding differs', 'identity_mismatch')

    protocol = ctx.resolver.resolve(protocol_ref, 'investment.sc05.experiment_protocol')
    view = ctx.resolver.resolve(parameters['independent_validation_view_ref'], 'investment.sc05.data_view')
    require(view['protocol_ref'] == protocol_ref and view['partition'] == 'independent_validation'
            and 'SC08' in view['permitted_stages'] and view['raw_access'] is False,
            'Q01', '/independent_validation_view_ref', 'Independent view control binding differs', 'use_not_allowed')
    checked_at = now()
    require(instant(parameters['evaluation_cutoff_at']) <= instant(ctx.input['information_cutoff_at'])
            <= instant(ctx.input['decision_at']) <= instant(checked_at),
            'Q01', '/evaluation_cutoff_at', 'Evaluation clocks are out of order', 'identity_mismatch')
    return {'input_refs': refs, 'roles': roles, 'parameters': parameters, 'protocol': protocol,
            'independent_view': view, 'checked_at': checked_at, 'role_uses': read_role_uses(ctx)}
