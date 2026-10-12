"""生产端共同残差向量和固定PCG64抽块；累计R不串乘。"""
from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np

from investment.contracts import ContractError, instant, is_known, known, unknown, require
from .production_inputs import gap, identifier
from .production_numerics import encoded


def exact_decimal(value):
    with localcontext() as context:
        context.prec = max(80, (len(str(abs(value.numerator))) + len(str(value.denominator))) * 4 + 16)
        result = Decimal(value.numerator) / value.denominator
    require(Fraction(result) == value, 'F13', '/cumulative_return', 'Finite decimal arithmetic did not preserve the original exact return')
    return encoded(result)


def contiguous(origins, complete):
    require(all(instant(a) < instant(b) for a, b in zip(origins, origins[1:])),
            'F11', '/origin_sequence', 'Original origins are reversed or repeated')
    segments, active = [], []
    for origin in origins:
        if origin in complete:
            active.append(origin)
        elif active:
            segments.append(active)
            active = []
    if active:
        segments.append(active)
    return segments


def windows(segments, length):
    return [{'segment_index': i, 'start_index': j} for i, segment in enumerate(segments)
            for j in range(max(len(segment) - length + 1, 0))]


def residual_rows(inputs, fitted):
    selections = {(row['entity_id'], row['horizon_id'], row['prediction_origin_at']): row for row in fitted['selections']}
    cutoff = instant(inputs.protocol['split_rule']['selection_information_cutoff_at'])
    result = []
    with localcontext() as context:
        context.prec = 80
        for prediction in fitted['historical']:
            selection = selections[(prediction['entity_id'], prediction['horizon_id'], prediction['origin_at'])]
            if selection['status'] != 'selected' or selection['selected_candidate_id'] != known(prediction['candidate_id']):
                continue
            label = inputs.labels[prediction['label_id']['value']]
            if label['label_status'] != 'mature' or not is_known(label['label_available_at']) or instant(label['label_available_at']['value']) > cutoff:
                continue
            require(is_known(label['outcome']), 'F11', '/outcome', 'Mature OOS outcome is unavailable', 'missing_evidence')
            observed, predicted = label['outcome']['value'], prediction['prediction']
            # 有限十进制差用Fraction构造，避免80位上下文截断破坏零容差外键算术。
            difference = Fraction(observed) - Fraction(predicted)
            residual = exact_decimal(difference)
            result.append({key: prediction[key] for key in ('origin_at', 'entity_id', 'horizon_id', 'prediction_id')}
                          | {'label_id': label['label_id'], 'observed_return': observed, 'predicted_return': predicted,
                             'residual': residual, 'label_available_at': label['label_available_at']['value'], 'unit': 'ratio'})
    return result


def joint_scenarios(inputs, fitted, residuals, endpoints, endpoint_gaps):
    p, method, policy = inputs.protocol, inputs.protocol['scenario_method'], inputs.protocol['randomness_policy']
    entities = {row['entity_id']['value'] for row in inputs.members if is_known(row['entity_id'])}
    entities.update(row['instrument_id']['value'] for row in inputs.risks if is_known(row['instrument_id']))
    coordinates = [{'entity_id': entity, 'horizon_id': horizon['horizon_id']} for entity in sorted(entities) for horizon in p['horizons']]
    lookup = {(row['origin_at'], row['entity_id'], row['horizon_id']): row for row in residuals}
    inventory, complete = [], set()
    for origin in p['origin_rule']['origins']:
        present = [row for row in coordinates if (origin, row['entity_id'], row['horizon_id']) in lookup]
        missing = [row for row in coordinates if row not in present]
        full = bool(coordinates) and not missing
        inventory.append({'origin_at': origin, 'status': 'complete' if full else 'incomplete', 'residual_keys': present,
                          'missing_coordinates': missing, 'reason': 'Complete mature prior-selected OOS vector' if full else 'Original required OOS coordinates are missing'})
        if full:
            complete.add(origin)
    parts = contiguous(p['origin_rule']['origins'], complete)
    starts = windows(parts, method['block_length'])
    grid = [{'grid_id': 'grid-anchor', 'at': p['information_cutoff_at'], 'horizon_ids': [], 'kind': 'anchor'}]
    grouped = {}
    for horizon in p['horizons']:
        if horizon['horizon_id'] in endpoints:
            grouped.setdefault(endpoints[horizon['horizon_id']], []).append(horizon['horizon_id'])
    for at in sorted(grouped, key=instant):
        grid.append({'grid_id': identifier('grid', at), 'at': at, 'horizon_ids': grouped[at], 'kind': 'horizon_endpoint'})
    gaps = list(endpoint_gaps)
    unresolved = [row['member_id'] for row in inputs.members if not is_known(row['entity_id'])]
    unresolved += [row['risk_id'] for row in inputs.risks if not is_known(row['instrument_id'])]
    if unresolved:
        gaps.append(gap('F16', '/required_member_ids', 'Original member/risk identities remain unresolved: ' + ', '.join(unresolved)))
    if not starts:
        gaps.append(gap('F11', '/valid_block_starts', 'No complete consecutive common residual block is available'))
    means = {(row['entity_id'], row['horizon_id']): Decimal(row['prediction']) for row in fitted['current']}
    required = {(row['entity_id'], row['horizon_id']) for row in coordinates}
    if not required or not required <= set(means):
        gaps.append(gap('F13', '/current_predictions', 'The full original current mean coordinate vector is incomplete'))
    if set(endpoints) != {h['horizon_id'] for h in p['horizons']}:
        gaps.append(gap('F13', '/time_grid', 'Original requested horizon endpoints are incomplete'))
    supported = (policy['generator'] == 'numpy_pcg64' and policy['algorithm_version'] == 'seedsequence4_int64_halfopen_vector_v1_numpy_2.5.4'
                 and np.__version__ == '2.5.4' and type(policy['seed']) is int and policy['seed'] >= 0)
    if not supported:
        gaps.append(gap('F12', '/randomness_policy', 'Actual generator/library or registered algorithm is unsupported'))
    indices, scenarios = [], []
    invalid_mean = False
    if required <= set(means) and set(endpoints) == {h['horizon_id'] for h in p['horizons']}:
        for entity in entities:
            dead = False
            for point in grid[1:]:
                values = [means[(entity, horizon)] for horizon in point['horizon_ids']]
                if len(set(values)) != 1 or any(value < -1 or dead and value != -1 for value in values):
                    invalid_mean = True
                dead = dead or any(value == -1 for value in values)
        if invalid_mean:
            gaps.append(gap('F13', '/current_mean_path', 'Current means violate endpoint identity, R>=-1 or zero-wealth absorption'))
    construction_gaps = list(gaps)
    if invalid_mean:
        construction_gaps = [row for row in construction_gaps if row['field_or_object'] != '/current_mean_path']
    if not construction_gaps:
        count = (method['draws'] + method['block_length'] - 1) // method['block_length']
        require(len(starts) <= np.iinfo(np.int64).max, 'F12', '/valid_block_starts', 'Original block population exceeds int64 high', 'resource_limit')
        # 唯一随机调用；坏经济路径不触发删样、重抽或重新加权。
        seed = np.random.SeedSequence(entropy=policy['seed'], spawn_key=(), pool_size=4, n_children_spawned=0)
        rng = np.random.Generator(np.random.PCG64(seed))
        indices = rng.integers(0, len(starts), size=count, dtype=np.int64, endpoint=False).tolist()
        for i in range(method['draws']):
            block, offset = divmod(i, method['block_length'])
            start = starts[indices[block]]
            origin = parts[start['segment_index']][start['start_index'] + offset]
            values, status = [], 'valid'
            with localcontext() as context:
                context.prec = 160
                for entity in sorted(entities):
                    values.append({'entity_id': entity, 'grid_id': grid[0]['grid_id'], 'cumulative_return': '0', 'gross_return': '1'})
                    dead = False
                    for point in grid[1:]:
                        returns = [Fraction(means[(entity, horizon)]) + Fraction(lookup[(origin, entity, horizon)]['residual']) for horizon in point['horizon_ids']]
                        if len(set(returns)) != 1:
                            # ScenarioValue不能无损表达同entity/grid双值；保留原预测/残差后走明确Failure。
                            raise ContractError('F13', '/scenarios/same_endpoint',
                                                'Incompatible cumulative horizon values: draw=' + str(i) + '; origin=' + origin
                                                + '; entity=' + entity + '; horizons=' + ','.join(point['horizon_ids'])
                                                + '; returns=' + ','.join(map(str, returns)), 'unsupported_output')
                        for value in dict.fromkeys(returns):
                            if value < -1:
                                status = 'below_minus_one'
                            elif dead and value != -1 and status == 'valid':
                                status = 'zero_revival'
                            values.append({'entity_id': entity, 'grid_id': point['grid_id'], 'cumulative_return': exact_decimal(value), 'gross_return': exact_decimal(1 + value)})
                        dead = dead or any(value == -1 for value in returns)
            scenarios.append({'scenario_id': identifier('scenario', [inputs.ctx.run_id, inputs.ctx.invocation_id, inputs.ctx.attempt_no, i]),
                              'selected_origin': origin, 'draw_index': i, 'block_draw_index': block, 'offset_in_block': offset,
                              'probability': encoded(float(Fraction(1, method['draws']))), 'values': values, 'economic_status': status,
                              'reason': 'Original jointly sampled cumulative-return vector; economic support retained'})
    invalid = invalid_mean or any(row['economic_status'] != 'valid' for row in scenarios)
    if invalid:
        gaps.append(gap('F13', '/scenarios', 'Original mean or sampled economic paths violate wealth support'))
    return {'protocol_ref': inputs.protocol_ref, 'information_cutoff_at': p['information_cutoff_at'], 'required_coordinates': coordinates,
            'required_member_ids': [row['member_id'] for row in inputs.members], 'mandatory_asset_ids': [row['risk_id'] for row in inputs.risks],
            'vector_inventory': inventory, 'contiguous_origin_segments': parts, 'valid_block_starts': starts, 'sampled_block_indices': indices,
            'seed': policy['seed'], 'random_algorithm': policy['generator'],
            'random_library_version': {'status': 'known', 'value': np.__version__, 'reason': None},
            'block_length': method['block_length'], 'draws': method['draws'], 'time_grid': grid, 'scenarios': scenarios,
            'probability_rule': 'equal_weight_empirical_draws', 'probability_numerator': 1, 'probability_denominator': method['draws'],
            'historical_complete_origin_count': len(complete), 'effective_sample_size': unknown('No supported effective-sample estimator is registered'),
            'status': 'invalid_economic_support' if invalid else 'constructed' if scenarios else 'insufficient_evidence', 'missing_evidence': gaps}
