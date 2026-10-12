"""独立共同向量、原序列分段、RNG重放和累计端点核验。"""
from decimal import Decimal, localcontext
from fractions import Fraction
import math

import numpy as np

from investment.contracts import instant, is_known, require
from .contracts import index, known, number
from .numerics import close
from .temporal import future_endpoints

GENERATOR = 'numpy_pcg64'
ALGORITHM_VERSION = 'seedsequence4_int64_halfopen_vector_v1_numpy_2.5.4'


def required_coordinates(s):
    dispositions = {row['member_id']: row for row in s.rows('member_forecast_dispositions')}
    entities = {row['entity_id']['value'] for row in s.members if is_known(row['entity_id'])
                and dispositions[row['member_id']]['forecast_status'] != 'proven_not_applicable'}
    entities |= {row['instrument_id']['value'] for row in s.risks if is_known(row['instrument_id'])}
    # 已退出当前U的合法历史任务仍核训练/预测，但不因此强造它的current行。
    return {(entity, horizon['horizon_id']) for entity in entities for horizon in s.protocol['horizons']}


def segments(origins, complete):
    require(all(instant(a) < instant(b) for a, b in zip(origins, origins[1:])), 'F11', '/origins', 'Original origin sequence is duplicate or reversed')
    result, current = [], []
    for origin in origins:
        if origin in complete:
            current.append(origin)
        elif current:
            result.append(current)
            current = []
    if current:
        result.append(current)
    return result


def block_starts(parts, length):
    return [{'segment_index': i, 'start_index': j}
            for i, part in enumerate(parts) for j in range(max(len(part) - length + 1, 0))]


def draw_indices(policy, count, windows):
    require(policy['generator'] == GENERATOR and policy['algorithm_version'] == ALGORITHM_VERSION
            and np.__version__ == '2.5.4', 'F12', '/randomness_policy', 'Registered RNG or actual library version is not supported', 'missing_evidence')
    require(type(policy['seed']) is int and policy['seed'] >= 0 and type(count) is int and count > 0
            and 0 < windows <= np.iinfo(np.int64).max, 'F12', '/random_parameters', 'No valid int64 half-open block population or draw count', 'missing_evidence')
    seed_sequence = np.random.SeedSequence(entropy=policy['seed'], spawn_key=(), pool_size=4, n_children_spawned=0)
    rng = np.random.Generator(np.random.PCG64(seed_sequence))
    return rng.integers(0, windows, size=count, dtype=np.int64, endpoint=False).tolist()


def residuals(s):
    source = {row['prediction_id']: row for row in s.rows('historical_predictions')}
    selections = {(row['entity_id'], row['horizon_id'], row['prediction_origin_at']): row
                  for row in s.artifacts['tuning_audit']['selections']}
    expected = {}
    cutoff = instant(s.protocol['split_rule']['selection_information_cutoff_at'])
    for prediction in source.values():
        key = (prediction['entity_id'], prediction['horizon_id'], prediction['origin_at'])
        selection = selections.get(key)
        if selection is None or selection['status'] != 'selected' or not is_known(prediction['label_id']):
            continue
        if prediction['candidate_id'] != known(selection['selected_candidate_id'], 'F11', '/selection'):
            continue
        label = s.labels[prediction['label_id']['value']]
        if label['label_status'] != 'mature' or not is_known(label['label_available_at']) or instant(label['label_available_at']['value']) > cutoff:
            continue
        expected[(prediction['origin_at'], prediction['entity_id'], prediction['horizon_id'])] = (prediction, label)
    rows = index(s.rows('residuals'), ('origin_at', 'entity_id', 'horizon_id'), 'F11')
    require(set(rows) == set(expected), 'F11', '/residuals', 'Residuals omit eligible common OOS components or use retrospective/unselected predictions')
    for key, row in rows.items():
        prediction, label = expected[key]
        require(row['prediction_id'] == prediction['prediction_id'] and row['label_id'] == label['label_id']
                and row['label_available_at'] == label['label_available_at']['value']
                and number(row['predicted_return']) == number(prediction['prediction'])
                and number(row['observed_return']) == number(known(label['outcome'], 'F11', '/outcome'))
                and Fraction(row['residual']) == Fraction(row['observed_return']) - Fraction(row['predicted_return']),
                'F11', '/residual', 'Residual differs from the exact original mature label and prior OOS prediction')
    joint = s.artifacts['current_joint_scenarios']
    required = required_coordinates(s)
    declared = {(row['entity_id'], row['horizon_id']) for row in joint['required_coordinates']}
    require(len(declared) == len(joint['required_coordinates']) and declared == required, 'F11', '/required_coordinates', 'Joint residual population shrank original member/risk coordinates')
    origins = s.protocol['origin_rule']['origins']
    require([row['origin_at'] for row in joint['vector_inventory']] == origins, 'F11', '/vector_inventory', 'Inventory changed the original historical origin sequence')
    complete = set()
    for row in joint['vector_inventory']:
        present = {(entity, horizon) for entity, horizon in required if (row['origin_at'], entity, horizon) in rows}
        actual = {(item['entity_id'], item['horizon_id']) for item in row['residual_keys']}
        missing = {(item['entity_id'], item['horizon_id']) for item in row['missing_coordinates']}
        require(actual == present and missing == required - present and row['status'] == ('complete' if present == required and required else 'incomplete'),
                'F11', '/vector_inventory', 'Common vector completeness or retained missing coordinates differ')
        if row['status'] == 'complete':
            complete.add(row['origin_at'])
    parts = segments(origins, complete)
    require(joint['contiguous_origin_segments'] == parts and joint['historical_complete_origin_count'] == len(complete),
            'F11', '/segments', 'Incomplete historical origins were silently removed or bridged')
    require(joint['block_length'] == s.protocol['scenario_method']['block_length']
            and joint['valid_block_starts'] == block_starts(parts, joint['block_length']), 'F11', '/blocks', 'Overlapping block inventory differs from original complete segments')
    require(joint['valid_block_starts'], 'F11', '/blocks', 'There is no complete legal common residual block', 'missing_evidence')


def sampling(s):
    joint = s.artifacts['current_joint_scenarios']
    method, policy = s.protocol['scenario_method'], s.protocol['randomness_policy']
    require(s.artifacts['scenario_method']['method'] == method and s.artifacts['scenario_method']['randomness_policy'] == policy,
            'F12', '/scenario_method', 'Scenario algorithm or randomness policy was replaced')
    require(joint['draws'] == method['draws'] and joint['seed'] == policy['seed']
            and joint['random_algorithm'] == policy['generator']
            and known(joint['random_library_version'], 'F12', '/rng_version') == np.__version__,
            'F12', '/random_identity', 'Actual draw algorithm, seed, shape or library identity differs')
    length, draws = joint['block_length'], joint['draws']
    count = (draws + length - 1) // length
    require(len(joint['sampled_block_indices']) == count and len(joint['scenarios']) == draws,
            'F12', '/draw_count', 'Draws or complete block indices were omitted', 'missing_evidence')
    indices = draw_indices(policy, count, len(joint['valid_block_starts']))
    require(joint['sampled_block_indices'] == indices, 'F12', '/sampled_block_indices', 'Saved block choices do not replay the original fixed RNG call')
    require(joint['probability_numerator'] == 1 and joint['probability_denominator'] == draws, 'F12', '/weights', 'Scenario rational probabilities were reweighted')
    seen = set()
    for i, row in enumerate(joint['scenarios']):
        block_index, offset = divmod(i, length)
        start = joint['valid_block_starts'][indices[block_index]]
        origin = joint['contiguous_origin_segments'][start['segment_index']][start['start_index'] + offset]
        require(row['scenario_id'] not in seen and row['draw_index'] == i and row['block_draw_index'] == block_index
                and row['offset_in_block'] == offset and row['selected_origin'] == origin,
                'F12', '/scenario_index', 'Scenario selected origin or common block mapping differs')
        seen.add(row['scenario_id'])
        probability = number(row['probability'])
        require(float(probability) == float(Fraction(1, draws)) and abs(probability - Decimal.from_float(float(Fraction(1, draws)))) <= Decimal.from_float(math.ulp(float(probability))) / 2,
                'F12', '/probability', 'Probability is not the round-trip representation of exact rational 1/draws')


def endpoints(s):
    joint = s.artifacts['current_joint_scenarios']
    cutoff = instant(s.payload['information_cutoff_at'])
    grid = index(joint['time_grid'], ('grid_id',), 'F13')
    anchors = [row for row in joint['time_grid'] if row['kind'] == 'anchor']
    require(len(anchors) == 1 and instant(anchors[0]['at']) == cutoff and not anchors[0]['horizon_ids'],
            'F13', '/anchor', 'Time grid must have exactly one C zero-return anchor')
    require(all(instant(a['at']) < instant(b['at']) for a, b in zip(joint['time_grid'], joint['time_grid'][1:])),
            'F13', '/time_grid', 'Time grid is duplicate or reversed')
    entities = {key[0] for key in required_coordinates(s)}
    expected_dates = future_endpoints(s.calendars['current'], s.protocol['horizons'], entities, cutoff)
    actual = {key: instant(row['at']) for row in joint['time_grid'] for key in row['horizon_ids']}
    require(actual == expected_dates and sum(len(row['horizon_ids']) for row in joint['time_grid']) == len(expected_dates)
            and len(joint['time_grid']) == 1 + len(set(expected_dates.values())),
            'F13', '/horizon_endpoints', 'Time grid added intermediate prices or changed true horizon endpoints')
    means = {(row['entity_id'], row['horizon_id']): row for row in s.rows('current_predictions')}
    residual = {(row['origin_at'], row['entity_id'], row['horizon_id']): Fraction(row['residual']) for row in s.rows('residuals')}
    for row in means.values():
        require(instant(row['origin_at']) == cutoff and instant(row['outcome_end_at']) == expected_dates[row['horizon_id']]
                and row['economic_support'] == ('valid' if number(row['prediction']) >= -1 else 'below_minus_one'),
                'F13', '/current_endpoint', 'Current prediction changed C, endpoint or economic diagnostic')
        require(number(row['prediction']) >= -1, 'F13', '/current_mean', 'Current mean lies below economic support', 'missing_evidence')
    require(required_coordinates(s) <= set(means), 'F13', '/current_mean_coordinates', 'Current mean omits a required entity/horizon coordinate', 'missing_evidence')
    # 先核均值自身；残差抵消不能恢复已归零财富或掩盖同端点冲突。
    for entity in entities:
        dead = False
        for point in joint['time_grid']:
            if point['kind'] == 'anchor':
                continue
            values = [Fraction(means[(entity, horizon)]['prediction']) for horizon in point['horizon_ids']]
            require(len(set(values)) == 1, 'F13', '/current_mean_same_endpoint', 'Current mean has conflicting cumulative values at the same endpoint')
            value = values[0]
            require(not dead or value == -1, 'F13', '/current_mean_path', 'Current mean revives positive wealth after zero', 'missing_evidence')
            dead = dead or value == -1
    violations = False
    for scenario in joint['scenarios']:
        rows = index(scenario['values'], ('entity_id', 'grid_id'), 'F13')
        require(set(rows) == {(entity, key[0]) for entity in entities for key in grid}, 'F13', '/scenario_coordinates', 'Scenario omitted a member/grid coordinate or invented an asset')
        status = 'valid'
        for entity in entities:
            dead = False
            for point in joint['time_grid']:
                row = rows[(entity, point['grid_id'])]
                expected = Fraction(0)
                if point['kind'] != 'anchor':
                    results = []
                    for horizon in point['horizon_ids']:
                        require((entity, horizon) in means and (scenario['selected_origin'], entity, horizon) in residual,
                                'F13', '/scenario_component', 'A common scenario component is missing', 'missing_evidence')
                        results.append(Fraction(means[(entity, horizon)]['prediction']) + residual[(scenario['selected_origin'], entity, horizon)])
                    require(len(set(results)) == 1, 'F13', '/same_endpoint', 'Same entity and endpoint have conflicting cumulative horizon values')
                    expected = results[0]
                require(Fraction(row['cumulative_return']) == expected and Fraction(row['gross_return']) == 1 + expected,
                        'F13', '/cumulative_path', 'Scenario does not equal mu plus one common-origin residual, relative to C')
                gross = number(row['gross_return'])
                if expected < -1:
                    status = 'below_minus_one'
                elif dead and gross > 0 and status == 'valid':
                    status = 'zero_revival'
                dead = dead or gross == 0
        require(scenario['economic_status'] == status, 'F13', '/economic_status', 'Invalid paths were relabeled instead of retained')
        violations |= status != 'valid'
    require(joint['status'] == ('invalid_economic_support' if violations else 'constructed'), 'F13', '/joint_status', 'Joint status contradicts retained economic paths')
    require(not violations, 'F13', '/economic_support', 'Original economic-invalid scenarios remain unusable without clipping or redraw', 'missing_evidence')


def scope(s):
    joint = s.artifacts['current_joint_scenarios']
    required = required_coordinates(s)
    current = {(row['entity_id'], row['horizon_id']) for row in s.rows('current_predictions')}
    require(required <= current and joint['status'] == 'constructed' and joint['scenarios'], 'F16', '/scope', 'Full required member/risk/horizon common scenarios are unavailable', 'missing_evidence')
    dispositions = {row['member_id']: row for row in s.rows('member_forecast_dispositions')}
    require(all(is_known(row['entity_id']) or dispositions[row['member_id']]['forecast_status'] == 'proven_not_applicable' for row in s.members)
            and all(is_known(row['instrument_id']) for row in s.risks), 'F16', '/identity', 'Unknown original member or risk identities still affect common-scenario scope', 'missing_evidence')
    require(all(row['forecast_status'] != 'unknown' for row in dispositions.values()),
            'F16', '/forecast_dispositions', 'An original member retains an unknown forecasting disposition', 'missing_evidence')
