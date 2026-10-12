"""SC05 私有精确转换、历史版本选择和权益财富计算；不调用验证器算法。"""
from __future__ import annotations

from fractions import Fraction

from investment.contracts import canonical, instant, is_known, require

from .contracts import value_equal


def evidence_upper(evidence):
    if not is_known(evidence):
        return None
    value = evidence['value']
    lower, upper = instant(value['lower_bound']), instant(value['upper_bound'])
    require(lower <= upper, 'D07', '/available_time', 'Source time interval is reversed')
    return upper


def quantized_decimal(value, places):
    """先作整数有理数half-even，再输出有限十进制；不受Decimal全局精度影响。"""
    value = Fraction(value)
    scaled = value * 10 ** places
    sign = -1 if scaled < 0 else 1
    quotient, remainder = divmod(abs(scaled.numerator), scaled.denominator)
    if 2 * remainder > scaled.denominator or 2 * remainder == scaled.denominator and quotient % 2:
        quotient += 1
    integer = quotient * sign
    if not places:
        return str(integer)
    digits = str(abs(integer)).zfill(places + 1)
    result = ('-' if integer < 0 else '') + digits[:-places] + '.' + digits[-places:]
    return result.rstrip('0').rstrip('.') if '.' in result else result


def converted_value(row):
    """原单位、币种、方向和明确精度保持闭合，未定义转换返回未知。"""
    if not is_known(row['raw_value']) or not is_known(row['raw_unit']):
        return None
    if row['conversion']['status'] == 'not_applicable':
        require(value_equal(row['unit'], row['raw_unit']) and value_equal(row['currency'], row['raw_currency']),
                'D06', '/unit', 'Identity conversion changed source unit or currency')
        return row['raw_value']['value']
    if not is_known(row['conversion']):
        return None
    conversion = row['conversion']['value']
    require(conversion['source_unit'] == row['raw_unit']['value']
            and value_equal(conversion['source_currency'], row['raw_currency'])
            and is_known(row['unit']) and conversion['target_unit'] == row['unit']['value']
            and value_equal(conversion['target_currency'], row['currency']),
            'D06', '/conversion', 'Conversion dimensions differ from the actual source and target')
    source, factor = Fraction(row['raw_value']['value']), Fraction(conversion['factor'])
    require(factor > 0, 'D06', '/factor', 'Conversion factor is not positive')
    operation = conversion['operation']
    if operation == 'identity':
        require(factor == 1 and value_equal(row['unit'], row['raw_unit']) and value_equal(row['currency'], row['raw_currency']),
                'D06', '/operation', 'Identity conversion changed financial meaning')
        value = source
    elif operation == 'scale':
        require(value_equal(row['currency'], row['raw_currency']) and conversion['quote_direction'] == 'not_currency',
                'D06', '/operation', 'Unit scaling cannot silently convert currency')
        value = source * factor
    else:
        direction = 'target_per_source' if operation == 'fx_multiply' else 'source_per_target'
        require(conversion['quote_direction'] == direction, 'D06', '/quote_direction', 'FX conversion has the wrong quote direction')
        value = source * factor if operation == 'fx_multiply' else source / factor
    places = conversion['output_decimal_places']
    rendered = quantized_decimal(value, places)
    require(conversion['rounding'] == 'half_even' or Fraction(rendered) == value,
            'D06', '/rounding', 'Non-terminating conversion needs its registered representation precision')
    return rendered


def observation_bound(row):
    upper = evidence_upper(row['available_time'])
    conversion = row['conversion']
    if is_known(conversion) and conversion['value']['operation'] != 'identity':
        factor_upper = evidence_upper(conversion['value']['factor_available_time'])
        if factor_upper is None:
            return None
        upper = max(upper, factor_upper) if upper is not None else None
    return upper


def normalize_observations(rows, captures):
    """保留全部真实版本；不把当日抓取的旧经济日期当成历史发布时刻。"""
    result = {}
    for row in rows:
        key = row['version_id']
        require(key not in result or canonical(result[key]) == canonical(row),
                'D06', '/version_id', 'Different source values reused one immutable version identity', 'identity_mismatch')
        capture = captures.get(row['raw_ref']['uri'])
        if capture is not None:
            require(row['retrieved_at'] == capture['retrieved_at'], 'D07', '/retrieved_at',
                    'Observation changed the actual source capture time', 'identity_mismatch')
            if row['availability_basis'] == 'current_capture_only':
                upper = evidence_upper(row['available_time'])
                require(upper == instant(capture['retrieved_at']), 'D07', '/available_time',
                        'Current capture was backdated into historical availability', 'identity_mismatch')
        value = converted_value(row)
        require(not is_known(row['value']) or value is not None and Fraction(row['value']['value']) == Fraction(value),
                'D06', '/value', 'Normalized observation differs from the independently applied conversion')
        upper = observation_bound(row)
        require((upper is None and not is_known(row['availability_bound_at']))
                or upper is not None and is_known(row['availability_bound_at'])
                and upper == instant(row['availability_bound_at']['value']),
                'D07', '/availability_bound_at', 'Observation bound differs from its actual complete evidence')
        result[key] = row
    for row in result.values():
        predecessor = row['supersedes_version_id']
        if not is_known(predecessor):
            continue
        seen = {row['version_id']}
        current = row
        while is_known(current['supersedes_version_id']):
            prior_id = current['supersedes_version_id']['value']
            require(prior_id in result and prior_id not in seen, 'D09', '/supersedes_version_id',
                    'Revision lineage is missing or cyclic')
            prior = result[prior_id]
            require(all(prior[field] == row[field] for field in ('entity_id', 'metric_id', 'measurement_basis'))
                    and is_known(prior['observation_period']) and is_known(row['observation_period'])
                    and all(prior['observation_period']['value'][field] == row['observation_period']['value'][field]
                            for field in ('lower_bound', 'upper_bound', 'precision', 'timezone')),
                    'D09', '/supersedes_version_id', 'Revision changed its economic measurement')
            seen.add(prior_id)
            current = prior
    return list(result.values())


def _quantity_meaning(row, fields):
    """只投影业务比较的状态和值，完整原版本及其原因文本保持不变。"""
    return canonical({field: {'status': row[field]['status'], 'value': row[field]['value']}
                      if field in ('value', 'unit', 'currency') else row[field] for field in fields})


def select_version(rows, entity_id, metric_id, origin):
    """只在当时可得版本内移除已明确替代者；未裁决冲突不按下载顺序挑值。"""
    origin = instant(origin)
    eligible = [row for row in rows if row['entity_id'] == entity_id and row['metric_id'] == metric_id
                and is_known(row['value']) and observation_bound(row) is not None
                and observation_bound(row) <= origin and evidence_upper(row['observation_period']) is not None
                and evidence_upper(row['observation_period']) <= origin
                and (not is_known(row['conversion']) or row['conversion']['value']['operation'] == 'identity'
                     or evidence_upper(row['conversion']['value']['factor_observation_at']) is not None
                     and evidence_upper(row['conversion']['value']['factor_observation_at']) <= origin)]
    if not eligible:
        return None
    latest_period = max(evidence_upper(row['observation_period']) for row in eligible)
    period = [row for row in eligible if evidence_upper(row['observation_period']) == latest_period]
    if len({canonical({key: row['observation_period']['value'][key]
                       for key in ('lower_bound', 'upper_bound', 'precision', 'timezone')}) for row in period}) != 1:
        return None
    indexes = {row['version_id']: row for row in rows}
    superseded = set()
    for row in period:
        predecessor = row['supersedes_version_id']
        visited = set()
        while is_known(predecessor):
            prior_id = predecessor['value']
            require(prior_id in indexes and prior_id not in visited, 'D09', '/supersedes_version_id',
                    'Revision lineage is missing or cyclic')
            superseded.add(prior_id)
            visited.add(prior_id)
            predecessor = indexes[prior_id]['supersedes_version_id']
    active = [row for row in period if row['version_id'] not in superseded]
    if not active:
        return None
    meanings = {_quantity_meaning(row, ('value', 'unit', 'currency', 'measurement_basis')) for row in active}
    if len(meanings) != 1:
        return None
    latest_available = max(observation_bound(row) for row in active)
    return min((row for row in active if observation_bound(row) == latest_available),
               key=lambda row: row['version_id'].encode('utf-8'))


def exact_endpoint(rows, entity_id, endpoint, basis, cutoff, *, availability_metadata=False):
    """日期/区间只在完整经济期间保守上界充当端点，不能延用过期NAV。"""
    endpoint_time = instant(endpoint)
    if endpoint_time > instant(cutoff) and not availability_metadata:
        return None
    measurement = 'unit_nav' if basis == 'unit_nav_with_documented_equity_events' else 'documented_reinvested_return_index'
    matching = [row for row in rows if row['entity_id'] == entity_id and row['measurement_basis'] == measurement
                and is_known(row['value']) and evidence_upper(row['observation_period']) == endpoint_time
                and observation_bound(row) is not None]
    candidates = [row for row in matching if observation_bound(row) <= instant(cutoff)]
    if not candidates and availability_metadata and matching:
        first_release = min(observation_bound(row) for row in matching)
        candidates = [row for row in matching if observation_bound(row) == first_release]
    if not candidates:
        return None
    periods = {canonical({key: row['observation_period']['value'][key]
                          for key in ('lower_bound', 'upper_bound', 'precision', 'timezone')}) for row in candidates}
    if len(periods) != 1:
        return None
    indexes = {row['version_id']: row for row in rows}
    superseded = set()
    for row in candidates:
        predecessor = row['supersedes_version_id']
        visited = set()
        while is_known(predecessor):
            prior_id = predecessor['value']
            require(prior_id in indexes and prior_id not in visited, 'D09', '/supersedes_version_id',
                    'Revision lineage is missing or cyclic')
            superseded.add(prior_id)
            visited.add(prior_id)
            predecessor = indexes[prior_id]['supersedes_version_id']
    candidates = [row for row in candidates if row['version_id'] not in superseded]
    meanings = {_quantity_meaning(row, ('metric_id', 'value', 'unit', 'currency')) for row in candidates}
    return min(candidates, key=lambda row: row['version_id'].encode('utf-8')) if candidates and len(meanings) == 1 else None


def reinvested_wealth(start, end, events, observations, contracts, *, equity_complete):
    """一份初始资产的份额、现金和应收独立记账；返回精确回报及完整真实依赖。"""
    require(start['entity_id'] == end['entity_id'] and start['metric_id'] == end['metric_id']
            and start['unit'] == end['unit'] and is_known(start['unit']) and start['currency'] == end['currency']
            and start['measurement_basis'] == end['measurement_basis'],
            'D11', '/end_version_id', 'Endpoints use incompatible economic quantities')
    initial, terminal = Fraction(start['value']['value']), Fraction(end['value']['value'])
    require(initial > 0 and terminal >= 0, 'D11', '/value', 'Initial wealth must be positive and terminal wealth nonnegative')
    origin, finish = evidence_upper(start['observation_period']), evidence_upper(end['observation_period'])
    require(origin is not None and finish is not None and origin < finish, 'D11', '/observation_period', 'Economic endpoints are not complete and ordered')
    dependency_ids = {start['version_id'], end['version_id']}
    contract_ids, event_ids = set(), set()
    bounds = [observation_bound(start), observation_bound(end)]
    if start['measurement_basis'] == 'documented_reinvested_return_index':
        return terminal / initial - 1, dependency_ids, contract_ids, event_ids, bounds
    require(start['measurement_basis'] == 'unit_nav' and equity_complete, 'D11', '/equity_events',
            'Unit NAV requires complete documented equity coverage for the requested period', 'missing_evidence')
    selected = []
    for event in events:
        if event['entity_id'] != start['entity_id']:
            continue
        ex = evidence_upper(event['ex_at'])
        require(ex is not None, 'D11', '/ex_at', 'An unresolved equity-event period cannot be silently discarded', 'missing_evidence')
        if origin < ex <= finish:
            selected.append((ex, event))
    # 同时发生的现金与拆并若未提供顺序，不能借ID排序补造金融含义。
    by_time = {}
    for ex, event in selected:
        by_time.setdefault(ex, set()).add(event['event_type'] == 'cash_dividend')
    require(all(len(kinds) == 1 for kinds in by_time.values()), 'D11', '/equity_events',
            'Simultaneous dividend and share-reorganization ordering is not evidenced', 'missing_evidence')
    actions, receivables = [], {}
    for ex, event in selected:
        contract_id = event['contract_version_id']
        require(contract_id in contracts, 'D11', '/contract_version_id', 'Equity event lacks its exact contract', 'missing_evidence')
        contract = contracts[contract_id]
        require(contract['entity_id'] == start['entity_id'], 'D11', '/contract_version_id', 'Equity event contract belongs to a different entity')
        lower = evidence_upper(contract['effective_from'])
        upper = evidence_upper(contract['effective_to'])
        require(lower is not None and lower <= ex and (contract['effective_to']['status'] == 'not_applicable' or upper is not None and ex <= upper),
                'D11', '/effective_from', 'Equity event has no applicable evidenced contract interval', 'missing_evidence')
        contract_ids.add(contract_id)
        event_ids.add(event['event_id'])
        bounds.extend([evidence_upper(event['available_time']), evidence_upper(contract['available_time'])])
        actions.append((ex, 'event', event))
        if event['event_type'] == 'cash_dividend' and is_known(event['reinvestment_at']):
            reinvestment = evidence_upper(event['reinvestment_at'])
            payable = evidence_upper(event['payable_at'])
            require(reinvestment is not None and payable is not None and ex <= payable <= reinvestment,
                    'D11', '/reinvestment_at', 'Reinvestment occurs before its evidenced entitlement/payment', 'missing_evidence')
            if reinvestment <= finish:
                actions.append((reinvestment, 'reinvest', event))
    shares = Fraction(1)
    for time, action, event in sorted(actions, key=lambda row: (row[0], row[1], row[2]['event_id'])):
        if action == 'event':
            if event['event_type'] != 'cash_dividend':
                require(is_known(event['share_multiplier']) and Fraction(event['share_multiplier']['value']) > 0,
                        'D11', '/share_multiplier', 'Share reorganization lacks its positive documented multiplier')
                shares *= Fraction(event['share_multiplier']['value'])
                continue
            require(is_known(event['cash_per_share']) and event['currency'] == start['currency']
                    and Fraction(event['cash_per_share']['value']) >= 0,
                    'D11', '/cash_per_share', 'Dividend lacks compatible nonnegative source cash')
            receivables[event['event_id']] = shares * Fraction(event['cash_per_share']['value'])
            continue
        contract = contracts[event['contract_version_id']]
        elections = [term for term in contract['terms'] if term['term'] == 'dividend_election'
                     and is_known(term['text_value']) and term['citations']]
        require(elections, 'D11', '/dividend_election', 'Reinvestment has no documented contract election', 'missing_evidence')
        require(is_known(event['reinvestment_nav_version_id']), 'D11', '/reinvestment_nav_version_id', 'Reinvestment NAV is unknown', 'missing_evidence')
        version_id = event['reinvestment_nav_version_id']['value']
        require(version_id in observations, 'D11', '/reinvestment_nav_version_id', 'Reinvestment NAV version was omitted', 'missing_evidence')
        nav = observations[version_id]
        require(nav['entity_id'] == start['entity_id'] and nav['metric_id'] == start['metric_id']
                and nav['measurement_basis'] == 'unit_nav' and nav['unit'] == start['unit']
                and nav['currency'] == start['currency'] and is_known(nav['value']) and Fraction(nav['value']['value']) > 0,
                'D11', '/reinvestment_nav_version_id', 'Reinvestment price is not the same positive unit NAV quantity')
        nav_period = nav['observation_period']
        require(is_known(nav_period) and instant(nav_period['value']['lower_bound']) <= instant(event['reinvestment_at']['value']['lower_bound'])
                and evidence_upper(nav_period) >= evidence_upper(event['reinvestment_at']),
                'D11', '/reinvestment_at', 'Reinvestment NAV does not cover its complete economic period')
        shares += receivables.pop(event['event_id']) / Fraction(nav['value']['value'])
        dependency_ids.add(version_id)
        bounds.append(observation_bound(nav))
    return (shares * terminal + sum(receivables.values(), Fraction(0))) / initial - 1, dependency_ids, contract_ids, event_ids, bounds
