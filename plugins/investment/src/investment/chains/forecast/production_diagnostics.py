"""生产端实际origin清单与残差描述；CNY政策损失保留准确下游依赖。"""
from decimal import Decimal, localcontext

from investment.contracts import instant, is_known, known, na, unknown
from .production_inputs import gap, identifier, numeric_known
from .production_numerics import encoded
from .production_scenarios import contiguous, windows


def _dependency(stage):
    return {'required_stage': stage,
            'required_object_kind': 'policy_CNY_wealth_loss_vectors' if stage == 'SC07' else 'independent_paired_origin_CNY_wealth_loss_vectors',
            'object_scope': 'same_protocol_horizon_reference_wealth_and_origin_basis', 'forecast_input_use': 'selection_comparison',
            'forecast_input_effect': 'does_not_by_itself_revoke_valid_forecast_input',
            'downstream_requirement': 'actual_CNY_wealth_loss_required_before_policy_risk_or_independent_qualification'}


def diagnostics(inputs, fitted, residuals, joint, model_ref, residual_ref, joint_ref):
    p, requirements = inputs.protocol, inputs.protocol['statistical_assumptions']['requirements']
    requested = [row['assumption_id'] for row in requirements if row['diagnostic_method']['method_id'] == 'ordered_origin_block_inventory'
                 and row['diagnostic_method']['input_kind'] == 'synchronized_oos_residual_vectors']
    inventories, tails, gaps, descriptions = [], [], [], []
    length, parameter = p['scenario_method']['block_length'], p['scenario_method']['block_length_parameter_id']
    datasets = [(model['fit_id'], [inputs.labels[key]['origin_at'] for key in model['training_label_ids']], [model_ref, inputs.view_refs['training']])
                for model in fitted['models']]
    datasets.append(('joint', p['origin_rule']['origins'], [residual_ref, joint_ref]))
    for identity, origins, refs in datasets:
        complete = set(origins) if identity != 'joint' else {row['origin_at'] for row in joint['vector_inventory'] if row['status'] == 'complete'}
        ordered = all(instant(a) < instant(b) for a, b in zip(origins, origins[1:]))
        parts = contiguous(origins, complete) if ordered else []
        count = len(windows(parts, length))
        missing = [identifier('origin', origin) for origin in origins if origin not in complete]
        insufficient = not ordered or len(complete) != len(origins) or not count or bool(missing)
        record = {'inventory_id': identifier('inventory', identity), 'assumption_ids': requested, 'input_refs': refs,
                  'fit_ids': [] if identity == 'joint' else [identity], 'origin_sequence': origins,
                  'method_id': 'ordered_origin_block_inventory', 'origin_count': len(origins), 'complete_common_origin_count': len(complete),
                  'block_length': length, 'block_length_parameter_id': parameter, 'overlapping_block_count': count,
                  'first_origin': known(origins[0]) if origins else unknown('No actual origin'),
                  'last_origin': known(origins[-1]) if origins else unknown('No actual origin'), 'missing_object_ids': missing,
                  'assessment': 'insufficient_evidence' if insufficient else 'descriptive_only',
                  'reason': 'Original ordering, complete origins and actual overlapping windows; counts do not establish weak dependence',
                  'input_kind': 'synchronized_oos_residual_vectors'}
        inventories.append(record)
        if insufficient:
            gaps.append(gap('F14', '/ordered_origin_block_inventory/' + str(len(inventories) - 1),
                            'Actual fitting/common residual origins are incomplete, unordered or have no legal registered block'))
    for position, requirement in enumerate(requirements):
        method = requirement['diagnostic_method']
        if method['input_kind'] != 'paired_origin_wealth_loss_vectors':
            continue
        reason = 'SC06 has no actual CNY policy/independent paired wealth-loss vectors; ratio residuals cannot supply them'
        gaps.append(gap('F14', '/statistical_assumptions/requirements/' + str(position), reason, ['independent_evaluation', 'decision_advice']))
        if method['method_id'] == 'ordered_origin_block_inventory':
            paired = p['statistical_assumptions']['paired_evaluation']
            inventories.append({'inventory_id': identifier('paired-inventory', requirement['assumption_id']),
                                'assumption_ids': [requirement['assumption_id']], 'input_refs': [], 'fit_ids': [], 'origin_sequence': [],
                                'method_id': method['method_id'], 'origin_count': unknown(reason), 'complete_common_origin_count': unknown(reason),
                                'block_length': paired['block_length'], 'block_length_parameter_id': paired['block_length_parameter_id'],
                                'overlapping_block_count': unknown(reason), 'first_origin': unknown(reason), 'last_origin': unknown(reason),
                                'missing_object_ids': [identifier('missing-paired-wealth', requirement['assumption_id'])],
                                'assessment': 'insufficient_evidence', 'reason': reason, 'input_kind': method['input_kind'], **_dependency('SC08')})
        elif method['method_id'] == 'empirical_loss_tail_inventory':
            beta_id = p['wealth_definition']['risk_definition']['cvar_beta_parameter_id']
            parameter_row = next(row for row in p['parameter_specs'] if row['parameter_id'] == beta_id)
            beta = parameter_row['value']
            for horizon in p['horizons']:
                with localcontext() as context:
                    context.prec = 80
                    mass = numeric_known(encoded(1 - Decimal(beta['value']))) if is_known(beta) else unknown('Registered beta is unknown')
                tails.append({'inventory_id': identifier('loss-tail', [requirement['assumption_id'], horizon['horizon_id']]),
                              'assumption_ids': [requirement['assumption_id']], 'method_id': method['method_id'], 'input_refs': [],
                              'loss_definition_ref': inputs.protocol_ref, 'horizon_id': horizon['horizon_id'], 'beta_parameter_id': beta_id,
                              'origin_count': unknown(reason), 'beta': numeric_known(beta['value']) if is_known(beta) else unknown('Registered beta is unknown'),
                              'empirical_var': unknown(reason), 'strict_exceedance_count': unknown(reason), 'var_atom_count': unknown(reason),
                              'tail_probability_mass': mass, 'empirical_cvar': unknown(reason), 'unit': 'CNY',
                              'assessment': 'insufficient_evidence', 'reason': reason, **_dependency('SC07')})
    counts = {'historical_origin_count': len(p['origin_rule']['origins']),
              'complete_vector_count': joint['historical_complete_origin_count'],
              'distinct_selected_origin_count': len({row['selected_origin'] for row in joint['scenarios']}),
              'economic_support_violation_count': sum(row['economic_status'] != 'valid' for row in joint['scenarios'])}
    def description(quantity, value, unit, origins, entity=None, horizon=None, other=None, lag=None):
        return {'diagnostic_id': identifier('diagnostic', [quantity, entity, horizon, other, lag]), 'quantity': quantity,
                'entity_id': known(entity) if entity is not None else na('Aggregate inventory'),
                'horizon_id': known(horizon) if horizon is not None else na('Aggregate inventory'),
                'other_entity_id': known(other[0]) if other else na('Single coordinate'),
                'other_horizon_id': known(other[1]) if other else na('Single coordinate'),
                'lag': numeric_known(lag) if lag is not None else na('Not a lag statistic'), 'value': value,
                'unit': unit, 'input_origin_ids': origins, 'reason': 'Actual empirical inventory or residual description; no assumption proof'}
    for quantity, count in counts.items():
        descriptions.append(description(quantity, numeric_known(str(count)), 'count', []))
    for quantity in ('effective_sample_size', 'unseen_tail_support'):
        descriptions.append(description(quantity, unknown('No supported estimator or unseen-tail evidence is registered'), 'count', []))
    coordinates = sorted({(row['entity_id'], row['horizon_id']) for row in residuals})
    lookup = {(row['origin_at'], row['entity_id'], row['horizon_id']): Decimal(row['residual']) for row in residuals}
    with localcontext() as context:
        context.prec = 80
        for entity, horizon in coordinates:
            origins = [origin for origin in p['origin_rule']['origins'] if (origin, entity, horizon) in lookup]
            values = [lookup[(origin, entity, horizon)] for origin in origins]
            mean = sum(values, Decimal(0)) / len(values)
            variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / len(values)
            descriptions.append(description('residual_mean', numeric_known(encoded(mean)), 'ratio', origins, entity, horizon))
            descriptions.append(description('residual_variance', numeric_known(encoded(variance)), 'ratio_squared', origins, entity, horizon))
        for i, (entity, horizon) in enumerate(coordinates):
            for other in coordinates[i + 1:]:
                origins = [origin for origin in p['origin_rule']['origins'] if (origin, entity, horizon) in lookup and (origin, *other) in lookup]
                if not origins:
                    continue
                left, right = [lookup[(origin, entity, horizon)] for origin in origins], [lookup[(origin, *other)] for origin in origins]
                lm, rm = sum(left) / len(left), sum(right) / len(right)
                covariance = sum((a - lm) * (b - rm) for a, b in zip(left, right)) / len(left)
                descriptions.append(description('cross_coordinate_covariance', numeric_known(encoded(covariance)), 'ratio_squared', origins, entity, horizon, other))
    return {'protocol_ref': inputs.protocol_ref, 'diagnostics': descriptions, 'missing_evidence': gaps,
            'ordered_origin_block_inventory': inventories, 'empirical_loss_tail_inventory': tails}
