"""SC04 验证用精确集合、准入、风险及可得时点规则。"""
from __future__ import annotations

from decimal import Decimal

from investment.contracts import canonical, instant, is_known, require

RULES = tuple(f'U{number:02}' for number in range(1, 22))


def predicate_verdicts(identity, rule):
    """从已取原文身份字段独立重算谓词，不读取生产者谓词结果。"""
    results = {}
    for predicate in rule['predicates']:
        identifier = predicate['predicate_id']
        require(identifier not in results, 'U09', '/predicates', 'Duplicate predicate identity')
        accepted = predicate['accepted_values']
        require(len(accepted) == len(set(accepted)), 'U09', '/accepted_values', 'Duplicate accepted value')
        require(predicate['operator'] != 'equals' or len(accepted) == 1,
                'U09', '/accepted_values', 'Equality requires exactly one expected value')
        observed = identity[predicate['field']] if identity is not None else None
        results[identifier] = ('true' if observed['value'] in accepted else 'false') if is_known(observed) else 'unknown'
    return results


def recompute_partitions(member_ids, group_ids, provenance, identities, predicate_results):
    """独立计算组内分区，再按存在合格、仍有可能、全部排除聚合全局。"""
    members, groups = set(member_ids), set(group_ids)
    require(len(members) == len(member_ids) and len(groups) == len(group_ids),
            'U07', '/rows', 'Duplicate member or group identity')
    relations = {}
    for row in provenance:
        member, group = row['member_id'], row['peer_group_id']
        require(member in members and group in groups, 'U08', '/membership_provenance', 'Foreign member or peer group')
        relations.setdefault((member, group), set()).add(row['relation'])
    require({member for member, _ in relations} == members, 'U06', '/membership_provenance', 'Original member has no group relation')
    local = {group: {'candidate': set(), 'excluded': set(), 'unknown': set()} for group in groups}
    for (member, group), states in relations.items():
        identity = identities.get(member)
        verdicts = set(predicate_results[member].values())
        if identity is None or not all(is_known(identity[field]) for field in ('entity_id', 'legal_fund_id', 'share_class_id')):
            disposition = 'unknown'
        elif 'false' in verdicts or states == {'proven_not_peer'}:
            disposition = 'excluded'
        elif states == {'confirmed'} and verdicts == {'true'}:
            disposition = 'candidate'
        else:
            disposition = 'unknown'
        local[group][disposition].add(member)
    global_sets = {'candidate': set(), 'excluded': set(), 'unknown': set()}
    for member in members:
        states = {status for parts in local.values() for status, values in parts.items() if member in values}
        status = 'candidate' if 'candidate' in states else 'unknown' if 'unknown' in states else 'excluded'
        global_sets[status].add(member)
    return local, global_sets


def verify_partition(member_ids, partitions):
    """分区守恒只证明对已调查U的处置，不证明原目录闭合。"""
    sets = {}
    for status in ('candidate', 'excluded', 'unknown'):
        rows = partitions[status]
        identifiers = [row['member_id'] for row in rows]
        require(len(identifiers) == len(set(identifiers)), 'U07', '/' + status, 'Duplicate disposition')
        require(all(row['status'] == status for row in rows), 'U07', '/' + status, 'Disposition table disagrees with status')
        sets[status] = set(identifiers)
    require(set(member_ids) == set.union(*sets.values()), 'U07', '/members', 'Disposition union differs from original U')
    require(not (sets['candidate'] & sets['excluded'] or sets['candidate'] & sets['unknown'] or sets['excluded'] & sets['unknown']),
            'U07', '/members', 'Disposition sets overlap')
    return sets


def verify_mandatory_risks(positions, pending, rows, positions_ref, pending_ref):
    """逐原始行保留登记持仓和待确认/到账交易，不按基金代码合并风险。"""
    expected = {}
    for index, position in enumerate(positions):
        key = ('registered_position', position['instrument_id'], None)
        expected[key] = (positions_ref, f'/rows/{index}', position)
    for index, trade in enumerate(pending):
        key = ('pending_' + trade['side'], trade['instrument_id'], trade['trade_id'])
        expected[key] = (pending_ref, f'/rows/{index}', trade)
    actual = {}
    for row in rows:
        if row['risk_kind'] == 'cash_comparator':
            continue
        require(is_known(row['instrument_id']), 'U12', '/mandatory_assets', 'Known upstream instrument was changed to unknown')
        trade_id = row['trade_id']['value'] if is_known(row['trade_id']) else None
        key = (row['risk_kind'], row['instrument_id']['value'], trade_id)
        require(key not in actual and key in expected, 'U12', '/mandatory_assets', 'Duplicated or invented account risk')
        reference, locator, original = expected[key]
        require(row['origin_ref'] == reference and row['origin_locator'] == locator,
                'U12', '/mandatory_assets', 'Risk does not bind the exact upstream row')
        require(row['coverage'] == 'recorded_transactions_since_start' and row['retention_status'] == 'retained',
                'U12', '/mandatory_assets', 'Risk coverage or retention was changed')
        if row['risk_kind'] == 'registered_position':
            require(row['confirmed_shares'] == original['confirmed_shares'], 'U12', '/confirmed_shares', 'Registered shares were changed')
        else:
            require(row['confirmed_shares']['status'] == 'unknown', 'U12', '/confirmed_shares', 'Pending source row does not disclose confirmed shares')
            require(is_known(row['pending_phase']) and row['pending_phase']['value'] == original['pending_phase'],
                    'U12', '/pending_phase', 'Pending phase was changed')
        require(row['amount']['status'] == 'unknown', 'U12', '/amount', 'Position/pending source row does not disclose a valuation amount')
        actual[key] = row
    require(actual.keys() == expected.keys(), 'U12', '/mandatory_assets', 'A registered holding or pending transaction is missing')


def verify_weight_bounds(row):
    """只核精确数值关系，不把基准数值或区间当实际持仓。"""
    values = {field: Decimal(row[field]['value']) for field in ('weight', 'lower_weight', 'upper_weight') if is_known(row[field])}
    if row['exposure_kind'] == 'qualitative_mechanism':
        require(all(row[field]['status'] == 'not_applicable' for field in ('weight', 'lower_weight', 'upper_weight')),
                'U11', '/weight', 'Qualitative mechanism cannot carry an invented numeric weight')
    if values:
        require(is_known(row['denominator']), 'U11', '/denominator', 'Numeric exposure requires its disclosed denominator')
    if 'lower_weight' in values and 'upper_weight' in values:
        require(values['lower_weight'] <= values['upper_weight'], 'U11', '/lower_weight', 'Weight interval is reversed')
    if 'weight' in values:
        require('lower_weight' not in values or values['lower_weight'] <= values['weight'], 'U11', '/weight', 'Weight below disclosed bound')
        require('upper_weight' not in values or values['weight'] <= values['upper_weight'], 'U11', '/weight', 'Weight above disclosed bound')
    return values


def availability_at(time_evidence, bound, origin):
    """未知返回False；已知保守上界不能早于原可得时间区间上界。"""
    if not is_known(time_evidence) or not is_known(bound):
        return False
    value = time_evidence['value']
    lower, upper, selected = instant(value['lower_bound']), instant(value['upper_bound']), instant(bound['value'])
    require(lower <= upper <= selected, 'U14', '/availability_bound_at', 'Availability bound contradicts source time interval')
    return selected <= instant(origin)


def inherited_gaps(receipts, account, assessment):
    """忠实保留上游Gap；affected_uses标签本身不重新计算上游许可。"""
    result, seen = [], set()
    for gap in [*account['missing_facts'], *assessment['unresolved_questions'],
                *(gap for receipt in receipts for gap in receipt['blockers'])]:
        key = canonical(gap)
        if key not in seen:
            seen.add(key)
            result.append(gap)
    return result


def current_scope_gaps(tasks, rows, current_at, historical_origins):
    """历史任务缺口保留在审计中，不能仅凭标签误撤当前范围许可。"""
    current, origins = instant(current_at), {instant(value) for value in historical_origins}
    for task in tasks.values():
        require(instant(task['requested_origin']) in origins | {current}, 'U05', '/requested_origin', 'Task uses an unrequested origin')
    selected = []
    for row in rows:
        if is_known(row['task_id']):
            key = row['task_id']['value']
            require(key in tasks and tasks[key]['source_id'] == row['source_id'], 'U05', '/task_id', 'Scope gap has no matching original task')
            is_current = instant(tasks[key]['requested_origin']) == current
            require(row['scope_kind'] != 'historical_vintage' or not is_current,
                    'U05', '/scope_kind', 'Current task gap was relabeled as historical')
        else:
            is_current = row['scope_kind'] != 'historical_vintage'
            require(is_current or origins, 'U05', '/scope_kind', 'Historical gap has no requested historical origin')
        if is_current:
            selected.append(row)
    return selected
