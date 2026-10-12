"""F14 三类诊断与独立宿主审核；货币损失依赖不冒充本链实测。"""
from decimal import Decimal, localcontext

from investment.contracts import ContractError, canonical, instant, is_known, require
from .contracts import host_work, index, known, number
from .scenarios import block_starts, segments
from .numerics import close


def numerical_inventory(s):
    diagnostics = s.artifacts['diagnostics']
    requirements = s.protocol['statistical_assumptions']['requirements']
    requested = {row['assumption_id'] for row in requirements if row['diagnostic_method']['method_id'] == 'ordered_origin_block_inventory'
                 and row['diagnostic_method']['input_kind'] == 'synchronized_oos_residual_vectors'}
    paired_requests = {row['assumption_id'] for row in requirements if row['diagnostic_method']['method_id'] == 'ordered_origin_block_inventory'
                       and row['diagnostic_method']['input_kind'] == 'paired_origin_wealth_loss_vectors'}
    records = diagnostics['ordered_origin_block_inventory']
    index(records, ('inventory_id',), 'F14')
    models = s.artifacts['fitted_model']['models']
    expected = {row['fit_id']: [s.labels[key]['origin_at'] for key in row['training_label_ids']] for row in models}
    expected['joint'] = s.protocol['origin_rule']['origins']
    covered = set()
    covered_paired = set()
    for record in records:
        if record['input_kind'] == 'paired_origin_wealth_loss_vectors':
            require(record['assumption_ids'] and set(record['assumption_ids']) <= paired_requests,
                    'F14', '/paired_request', 'A deferred paired inventory changed the original request input kind')
            covered_paired |= set(record['assumption_ids'])
            paired = s.protocol['statistical_assumptions']['paired_evaluation']
            require(record['block_length'] == paired['block_length'] and record['block_length_parameter_id'] == paired['block_length_parameter_id']
                    and not record['origin_sequence'] and not record['fit_ids'] and not record['input_refs']
                    and record['assessment'] == 'insufficient_evidence', 'F14', '/paired_dependency', 'Residual/fit origins or a different block parameter were substituted for actual paired CNY wealth')
            for key in ('origin_count', 'complete_common_origin_count', 'overlapping_block_count', 'first_origin', 'last_origin'):
                require(record[key]['status'] == 'unknown', 'F14', '/paired_' + key, 'No actual paired CNY wealth observations exist in SC06')
            kind = 'policy_CNY_wealth_loss_vectors' if record['required_stage'] == 'SC07' else 'independent_paired_origin_CNY_wealth_loss_vectors'
            require(record['required_object_kind'] == kind and record['missing_object_ids'],
                    'F14', '/paired_missing_objects', 'Deferred paired inventory lost the exact missing object or stage')
            gaps = diagnostics['missing_evidence'] + s.payload['missing_evidence']
            for identity in record['assumption_ids']:
                position = next(i for i, row in enumerate(requirements) if row['assumption_id'] == identity)
                require(any(gap['field_or_object'] == '/statistical_assumptions/requirements/' + str(position) for gap in gaps),
                        'F14', '/paired_gap', 'Deferred original paired request has no accurately located public Gap')
            continue
        require(set(record['assumption_ids']) == requested, 'F14', '/inventory_assumptions', 'Origin inventory omitted or changed exact diagnostic requests')
        keys = record['fit_ids'] or ['joint']
        require(len(keys) == len(set(keys)) and all(key in expected and key not in covered for key in keys)
                and all(expected[key] == expected[keys[0]] for key in keys),
                'F14', '/inventory_inputs', 'Each actual fit and joint dataset requires a distinct origin inventory')
        key = keys[0]
        covered.update(keys)
        origins = expected[key]
        require(record['origin_sequence'] == origins and record['origin_count'] == len(origins)
                and record['block_length_parameter_id'] == s.protocol['scenario_method']['block_length_parameter_id']
                and record['block_length'] == s.protocol['scenario_method']['block_length'],
                'F14', '/origin_inventory', 'Inventory does not preserve original input origins or registered block parameter')
        if key == 'joint':
            complete = {row['origin_at'] for row in s.artifacts['current_joint_scenarios']['vector_inventory'] if row['status'] == 'complete'}
        else:
            complete = set(origins)
        parts = segments(origins, complete)
        count = len(block_starts(parts, record['block_length']))
        require(record['complete_common_origin_count'] == len(complete) and record['overlapping_block_count'] == count,
                'F14', '/origin_counts', 'Raw complete-origin count or nonbridging block count differs')
        for field, source in (('first_origin', origins[:1]), ('last_origin', origins[-1:])):
            require(record[field]['status'] == ('known' if source else 'unknown')
                    and (not source or record[field]['value'] == source[0]), 'F14', '/' + field, 'Origin endpoint inventory differs')
        insufficient = len(complete) != len(origins) or not count or bool(record['missing_object_ids'])
        require(record['assessment'] == ('insufficient_evidence' if insufficient else 'descriptive_only'), 'F14', '/assessment', 'Descriptive inventory was falsely promoted to assumption support')
        for ref in record['input_refs']:
            s.ctx.resolver.resolve(ref)
        require(not insufficient, 'F14', '/origin_inventory', 'Required current fit or common residual inventory is incomplete', 'missing_evidence')
    require(covered == set(expected), 'F14', '/ordered_origin_block_inventory', 'An actual fit or joint residual dataset has no complete inventory', 'missing_evidence')
    require(covered_paired == paired_requests, 'F14', '/paired_requests', 'An original paired wealth inventory request was removed')
    tails = diagnostics['empirical_loss_tail_inventory']
    tail_requests = {row['assumption_id'] for row in requirements if row['diagnostic_method']['method_id'] == 'empirical_loss_tail_inventory'}
    covered_tails = set()
    beta_id = s.protocol['wealth_definition']['risk_definition']['cvar_beta_parameter_id']
    parameters = {row['parameter_id']: row for row in s.protocol['parameter_specs']}
    for row in tails:
        require(row['assumption_ids'] and set(row['assumption_ids']) <= tail_requests and row['loss_definition_ref'] == s.protocol_ref
                and row['beta_parameter_id'] == beta_id and row['horizon_id'] in {h['horizon_id'] for h in s.protocol['horizons']},
                'F14', '/tail_request', 'Deferred loss inventory changed the original CNY request')
        covered_tails |= set(row['assumption_ids'])
        expected_kind = 'policy_CNY_wealth_loss_vectors' if row['required_stage'] == 'SC07' else 'independent_paired_origin_CNY_wealth_loss_vectors'
        require(row['required_object_kind'] == expected_kind and row['assessment'] == 'insufficient_evidence'
                and not row['input_refs'], 'F14', '/loss_dependency', 'SC06 falsely asserted actual CNY loss evidence or mismatched downstream dependency')
        for key in ('origin_count', 'empirical_var', 'strict_exceedance_count', 'var_atom_count', 'empirical_cvar'):
            require(row[key]['status'] == 'unknown', 'F14', '/' + key, 'No CNY loss input exists; a residual ratio cannot substitute for money')
        beta = parameters[beta_id]['value']
        if is_known(beta):
            require(number(known(row['beta'], 'F14', '/beta')) == number(beta['value'])
                    and number(known(row['tail_probability_mass'], 'F14', '/tail_mass')) == 1 - number(beta['value']),
                    'F14', '/tail_beta', 'Known beta or rational tail mass differs from protocol')
        else:
            require(row['beta']['status'] == row['tail_probability_mass']['status'] == 'unknown', 'F14', '/beta', 'Unknown risk parameter was filled with a default')
    require(covered_tails == tail_requests, 'F14', '/tail_requests', 'An original empirical CNY tail request was discarded')
    descriptive(s)


def descriptive(s):
    residuals = s.rows('residuals')
    for row in s.artifacts['diagnostics']['diagnostics']:
        if row['quantity'] in ('effective_sample_size', 'unseen_tail_support'):
            require(row['value']['status'] == 'unknown', 'F14', '/unsupported_metric', 'Historical draws or counts were substituted for unsupported ESS/tail evidence')
            continue
        origins = row['input_origin_ids']
        require(len(origins) == len(set(origins)), 'F14', '/diagnostic_origins', 'Diagnostic duplicates historical origins')
        candidates = [value for value in residuals if value['origin_at'] in origins
                      and (not is_known(row['entity_id']) or value['entity_id'] == row['entity_id']['value'])
                      and (not is_known(row['horizon_id']) or value['horizon_id'] == row['horizon_id']['value'])]
        joint = s.artifacts['current_joint_scenarios']
        counts = {'historical_origin_count': len(s.protocol['origin_rule']['origins']),
                  'complete_vector_count': sum(item['status'] == 'complete' for item in joint['vector_inventory']),
                  'distinct_selected_origin_count': len({item['selected_origin'] for item in joint['scenarios']}),
                  'economic_support_violation_count': sum(item['economic_status'] != 'valid' for item in joint['scenarios'])}
        if row['quantity'] in counts:
            require(number(known(row['value'], 'F14', '/count')) == counts[row['quantity']] and row['unit'] == 'count',
                    'F14', '/descriptive_count', 'Descriptive count differs from the actual original inventory')
            continue
        require(candidates and len(candidates) == len(origins), 'F14', '/diagnostic_input', 'Descriptive residual sample is incomplete or ambiguous')
        with localcontext() as ctx:
            ctx.prec = 80
            by_origin = {value['origin_at']: value for value in candidates}
            require(set(by_origin) == set(origins), 'F14', '/diagnostic_origins', 'Descriptive sample does not preserve its exact declared origin identities')
            values = [number(by_origin[origin]['residual']) for origin in origins]
            mean = sum(values) / len(values)
            if row['quantity'] in ('residual_mean', 'residual_variance'):
                terms = values if row['quantity'] == 'residual_mean' else [(item - mean) ** 2 for item in values]
                value = sum(terms) / len(values)
                close(known(row['value'], 'F14', '/diagnostic_value'), value, terms, 4 * len(values) + 2, 'F14')
                require(row['unit'] == ('ratio' if row['quantity'] == 'residual_mean' else 'ratio_squared'), 'F14', '/unit', 'Residual diagnostic unit differs')
                continue
            other_entity = known(row['other_entity_id'], 'F14', '/other_entity')
            other_horizon = known(row['other_horizon_id'], 'F14', '/other_horizon')
            lookup = {(item['origin_at'], item['entity_id'], item['horizon_id']): number(item['residual']) for item in residuals}
            if row['quantity'] == 'cross_coordinate_covariance':
                require(all((origin, other_entity, other_horizon) in lookup for origin in origins),
                        'F14', '/covariance_pairs', 'A synchronized covariance coordinate is missing', 'missing_evidence')
                others = [lookup[(origin, other_entity, other_horizon)] for origin in origins]
                other_mean = sum(others) / len(others)
                terms = [(left - mean) * (right - other_mean) for left, right in zip(values, others)]
                value = sum(terms) / len(terms)
                close(known(row['value'], 'F14', '/covariance'), value, terms, 6 * len(values) + 3, 'F14')
                require(row['unit'] == 'ratio_squared', 'F14', '/unit', 'Covariance is not ratio squared')
            elif row['quantity'] == 'lag_correlation':
                lag = known(row['lag'], 'F14', '/lag')
                require(lag > 0, 'F14', '/lag', 'Lag correlation requires a positive registered lag')
                pairs = []
                for part in joint['contiguous_origin_segments']:
                    for position in range(lag, len(part)):
                        if part[position] in origins and part[position - lag] in origins:
                            pairs.append((lookup[(part[position], row['entity_id']['value'], row['horizon_id']['value'])],
                                          lookup[(part[position - lag], other_entity, other_horizon)]))
                if not pairs:
                    require(row['value']['status'] == 'unknown', 'F14', '/correlation', 'No within-segment lag pairs exist')
                    continue
                left_mean = sum(pair[0] for pair in pairs) / len(pairs)
                right_mean = sum(pair[1] for pair in pairs) / len(pairs)
                covariance = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
                variance_left = sum((left - left_mean) ** 2 for left, _ in pairs)
                variance_right = sum((right - right_mean) ** 2 for _, right in pairs)
                if not variance_left or not variance_right:
                    require(row['value']['status'] == 'unknown', 'F14', '/correlation', 'Zero Pearson denominator must remain unknown')
                    continue
                value = covariance / (variance_left * variance_right).sqrt()
                close(known(row['value'], 'F14', '/correlation'), value, [value], 12 * len(pairs) + 6, 'F14')
                require(row['unit'] == 'unitless', 'F14', '/unit', 'Correlation must be dimensionless')


def request_or_review(s):
    summary = host_work(s, 'F17')
    review = s.ctx.host_response('forecast_review')
    if review is None:
        # 控制器在本进程返回后才登记请求；已有相同键不重复预占。
        additional = 0 if summary.contains('validator', 'forecast_review', 'main') else 1
        require(summary.registered_count() + additional <= s.protocol['search_budget']['max_host_work_items'],
                'F17', '/max_host_work_items', 'No reserved host budget remains for independent forecast review')
        return s.ctx.request_host('forecast_review', list({canonical(ref): ref for ref in s.evidence}.values())), None
    s.ctx.schemas.validate('investment.sc06.validation_review', review)
    require(review['protocol_ref'] == s.protocol_ref, 'F17', '/protocol_ref', 'Independent host reviewed another protocol')
    require(all(ref in s.evidence or ref in s.ctx.resolver.host_output_refs for ref in review['reviewed_refs']),
            'F17', '/reviewed_refs', 'Host claims an object outside the dispatched actual evidence')
    for reference in review['reviewed_refs']:
        s.ctx.resolver.resolve(reference)
    return None, review


def assumption_review(s, review):
    required = {row['assumption_id']: row for row in s.protocol['statistical_assumptions']['requirements']
                if row['diagnostic_method']['method_id'] == 'documented_assumption_review'}
    producer = index(s.artifacts['assumptions']['findings'], ('assumption_id',), 'F14')
    validator = index(review['assumption_findings'], ('assumption_id',), 'F14')
    require({key[0] for key in producer} == set(required) == {key[0] for key in validator},
            'F14', '/assumptions', 'Actual assumption requests were omitted or relabeled')
    for (identity,), finding in validator.items():
        for reference in required[identity]['basis_refs'] + finding['evidence_refs']:
            try:
                s.ctx.resolver.resolve(reference)
            except ContractError:
                require(False, 'F14', '/assumption_basis', 'Necessary assumption basis is unreadable within authorized scope', 'missing_evidence')
        require(finding['source_scope_match'] == 'supported' and finding['assessment'] == 'conditional_support'
                and not finding['observed_contradictions'] and not finding['unresolved_assumptions'],
                'F14', '/assumption/' + identity, 'Current fitting/OOS/joint assumptions lack conditional support or have actual contradictions', 'missing_evidence')


def disposition_review(s, review):
    expected = {('member', row['member_id']): (s.payload['member_forecast_dispositions_ref'], row['forecast_status'], row['affected_uses'])
                for row in s.rows('member_forecast_dispositions')}
    expected.update({('mandatory_risk', row['risk_id']): (s.universe['mandatory_assets_ref'], 'retained', []) for row in s.risks})
    findings = index(review['disposition_findings'], ('target_kind', 'target_id'), 'F15')
    require(set(findings) == set(expected), 'F15', '/disposition_findings', 'Review does not cover every original U member and mandatory risk once')
    for key, row in findings.items():
        reference, status, uses = expected[key]
        require(row['target_ref'] == reference and row['observed_status'] == status and set(uses) <= set(row['affected_uses']),
                'F15', '/disposition_target', 'Host reviewed the wrong original object, status or use impact')
        for evidence in row['evidence_refs']:
            s.ctx.resolver.resolve(evidence)
        require(row['verdict'] != 'contradicted', 'F15', '/disposition_evidence', 'Independent source review contradicts this model disposition')
        if row['verdict'] == 'insufficient_evidence':
            require(status == 'unknown' and row['gaps'] and row['affected_uses'], 'F15', '/disposition_support', 'Insufficient evidence cannot support claimed ready or not-applicable status')
        if status == 'unknown':
            require(row['gaps'] and row['affected_uses'], 'F15', '/unknown_impact', 'Faithful unknown disposition lost original gaps or material use impact')


def host_binding(s, review):
    require(review is not None and s.ctx.resolver.host_output_refs, 'F17', '/host', 'No actual independent host result was bound', 'missing_evidence')
    s.evidence += s.ctx.resolver.host_output_refs
    summary = host_work(s, 'F17')
    require(any(item.role == 'validator' and item.step_id == 'forecast_review' and item.work_key == 'main'
                and item.state == 'completed' for item in summary.items),
            'F17', '/host_work_summary', 'Independent forecast review has no completed current-attempt registration', 'missing_evidence')
