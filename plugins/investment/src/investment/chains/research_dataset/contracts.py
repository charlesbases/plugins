"""SC05 验证器使用的精确关系与时点谓词。"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from investment.contracts import ContractError, canonical, instant, is_known, require

RULES = tuple(f'D{number:02}' for number in range(1, 21))
GAP_FIELDS = {'rule_id', 'field_or_object', 'reason', 'owner', 'required_evidence', 'affected_uses'}


def gap_index(gaps):
    """外键索引仅派生于完整公共值，不修改Gap或添加公共字段。"""
    result = {}
    for gap in gaps:
        require(set(gap) == GAP_FIELDS, 'D17', '/gaps', 'Gap must retain the exact public fields')
        key = 'g-' + hashlib.sha256(canonical(gap)).hexdigest()
        require(key not in result or canonical(result[key]) == canonical(gap),
                'D17', '/gaps', 'Distinct canonical Gap bytes have the same derived identity')
        result[key] = gap
    return result


def value_equal(left, right):
    """比较Optional语义值；未知原因文字不替代状态和值。"""
    return (left['status'], left['value']) == (right['status'], right['value'])


def period_key(evidence):
    """同经济期间可有不同原文版本；引用身份不等于期间语义。"""
    require(is_known(evidence), 'D07', '/observation_period', 'Revision economic period is unknown', 'missing_evidence')
    value = evidence['value']
    return instant(value['lower_bound']), instant(value['upper_bound']), value['precision'], value['timezone']


def quantized(value, places):
    """独立有理数half-even；不依赖Decimal上下文精度。"""
    scaled = Fraction(value) * 10 ** places
    lower, remainder = divmod(scaled.numerator, scaled.denominator)
    twice = 2 * remainder
    rounded = lower + (twice > scaled.denominator or twice == scaled.denominator and lower % 2 != 0)
    return Fraction(rounded, 10 ** places)


def time_upper(evidence, rule='D07'):
    if not is_known(evidence):
        return None
    item = evidence['value']
    lower, upper = instant(item['lower_bound']), instant(item['upper_bound'])
    require(lower <= upper, rule, '/available_time', 'Source time interval is reversed')
    if item['precision'] == 'date':
        require(upper - lower == timedelta(days=1) - timedelta(microseconds=1),
                rule, '/available_time', 'Date precision must retain the complete conservative local day')
    return upper


def verify_scheduled_period(row, rule='D10'):
    """预计经济期间只作带来源的索引，不能冒充已发生估值瞬间。"""
    evidence = row['scheduled_valuation_period']
    if not is_known(evidence):
        return None
    value = evidence['value']
    lower, upper = instant(value['lower_bound']), instant(value['upper_bound'])
    require(lower <= upper, rule, '/scheduled_valuation_period', 'Scheduled economic interval is reversed')
    try:
        timezone = ZoneInfo(value['timezone'])
    except ZoneInfoNotFoundError as error:
        raise ContractError(rule, '/scheduled_valuation_period', 'Source timezone database is unavailable', 'missing_evidence') from error
    day = date.fromisoformat(row['date'])
    require(value['precision'] in ('instant', 'date', 'interval'), rule, '/scheduled_valuation_period',
            'Coarse scheduled period cannot identify this calendar date', 'missing_evidence')
    require(upper.astimezone(timezone).date() == day, rule, '/scheduled_valuation_period',
            'Scheduled period does not end on its source calendar date')
    if value['precision'] == 'date':
        require(lower == datetime.combine(day, time.min, timezone)
                and upper == datetime.combine(day + timedelta(days=1), time.min, timezone) - timedelta(microseconds=1),
                rule, '/scheduled_valuation_period', 'Date precision must retain the complete source local day')
    elif value['precision'] == 'instant':
        require(lower == upper, rule, '/scheduled_valuation_period', 'Instant schedule contains a nonzero interval')
    else:
        require(lower < upper, rule, '/scheduled_valuation_period', 'Interval schedule has no economic span')
    return lower, upper, value['precision'], value['timezone']


def available_at(evidence, bound, origin, rule='D07'):
    upper = time_upper(evidence, rule)
    if upper is None or not is_known(bound):
        return False
    selected = instant(bound['value'])
    require(upper <= selected, rule, '/availability_bound_at', 'Availability bound precedes source upper bound')
    return selected <= instant(origin)


def verify_conversion(row):
    if not is_known(row['value']):
        require(not is_known(row['raw_value']) or row['conversion']['status'] == 'unknown',
                'D06', '/value', 'Known raw value was discarded without an unresolved conversion')
        return
    require(is_known(row['raw_value']) and is_known(row['unit']) and is_known(row['raw_unit']),
            'D06', '/value', 'Known observation lacks raw value or explicit units')
    raw, actual = Fraction(row['raw_value']['value']), Fraction(row['value']['value'])
    if not is_known(row['conversion']):
        require(row['conversion']['status'] == 'not_applicable' and actual == raw
                and value_equal(row['unit'], row['raw_unit']) and value_equal(row['currency'], row['raw_currency']),
                'D06', '/conversion', 'Unconverted observation changed source number or unit')
        return
    conversion = row['conversion']['value']
    require(conversion['source_unit'] == row['raw_unit']['value'] and conversion['target_unit'] == row['unit']['value']
            and value_equal(conversion['source_currency'], row['raw_currency'])
            and value_equal(conversion['target_currency'], row['currency']),
            'D06', '/conversion', 'Conversion units or currencies do not match source and target')
    factor = Fraction(conversion['factor'])
    require(factor > 0, 'D06', '/factor', 'Conversion factor must be positive')
    operation, direction = conversion['operation'], conversion['quote_direction']
    if operation == 'identity':
        require(factor == 1 and row['unit'] == row['raw_unit'] and value_equal(row['currency'], row['raw_currency']),
                'D06', '/conversion', 'Identity conversion changed units or currency')
        expected = raw
    elif operation == 'fx_divide':
        require(direction == 'source_per_target', 'D06', '/quote_direction', 'FX division quote direction differs')
        expected = raw / factor
    else:
        require(direction == ('target_per_source' if operation == 'fx_multiply' else 'not_currency'),
                'D06', '/quote_direction', 'Conversion quote direction differs')
        expected = raw * factor
    if conversion['rounding'] == 'none':
        require(actual == expected, 'D06', '/value', 'Exact converted value differs')
    else:
        places = conversion['output_decimal_places']
        require(actual == quantized(expected, places) and abs(actual - expected) <= Fraction(1, 2 * 10 ** places),
                'D06', '/value', 'Converted value differs from registered half-even quantization')


def selected_predictors(protocol, entity_id):
    return [item for item in protocol['model_search_space']['predictors']
            if item['applicability']['kind'] == 'all_origin_forecast_entities'
            or entity_id in item['applicability']['entity_ids']]


def verify_features(panel, protocol, observations, source_rules):
    predictors = selected_predictors(protocol, panel['entity_id'])
    require([row['feature_id'] for row in panel['feature_values']] == [row['feature_id'] for row in predictors],
            'D16', '/feature_values', 'Panel does not retain the full ordered P(entity)')
    for feature, definition in zip(panel['feature_values'], predictors):
        require(feature['transformation'] == definition['transformation']
                and feature['transformation_spec_ref'] == definition['source_rule_ref'],
                'D16', '/feature_values', 'Feature transformation or source definition changed')
        require(value_equal(feature['unit'], definition['expected_unit']) and value_equal(feature['currency'], definition['currency']),
                'D16', '/feature_values', 'Feature target unit or currency differs from frozen definition')
        if not is_known(feature['value']):
            require(feature['gap_ids'] and not panel['eligible_for_training'],
                    'D16', '/feature_values', 'Missing feature slot lacks Gap or still claims training readiness')
            continue
        require(is_known(feature['unit']) and feature['currency']['status'] in ('known', 'not_applicable'),
                'D16', '/feature_values', 'Numeric predictor target units or currency remain unknown', 'missing_evidence')
        require(len(feature['source_version_ids']) == 1, 'D16', '/source_version_ids', 'Identity/conversion requires one exact observation version')
        source = observations[feature['source_version_ids'][0]]
        binding = definition['source_entity_binding']
        entity = panel['entity_id'] if binding['kind'] == 'panel_entity' else binding['entity_id']
        require(source['entity_id'] == entity and source['metric_id'] == definition['feature_id'],
                'D16', '/source_version_ids', 'Predictor source entity or metric differs')
        rule = source_rules[definition['source_rule_ref']['sha256']]
        fields = [item for item in rule['field_rules'] if item['field_name'] == definition['field_name']]
        require(len(fields) == 1, 'D16', '/field_name', 'Predictor field is not unique')
        field = fields[0]
        require(value_equal(field['expected_unit'], source['raw_unit']) and value_equal(field['currency'], source['raw_currency']),
                'D16', '/source_version_ids', 'Raw source units differ from frozen field rule')
        if definition['transformation'] == 'identity':
            require(value_equal(source['raw_unit'], definition['expected_unit']) and value_equal(source['raw_currency'], definition['currency'])
                    and (not is_known(source['conversion']) or source['conversion']['value']['operation'] == 'identity'),
                    'D16', '/transformation', 'Identity predictor contains an undeclared conversion')
        else:
            require(is_known(source['conversion']), 'D16', '/conversion', 'Declared conversion lacks its actual conversion record')
            conversion = source['conversion']['value']
            require(time_upper(conversion['factor_available_time'], 'D16') is not None
                    and time_upper(conversion['factor_available_time'], 'D16') <= instant(panel['origin_at']),
                    'D16', '/factor_available_time', 'Conversion factor was not actually available at feature origin')
        require(value_equal(source['value'], feature['value']) and value_equal(source['unit'], feature['unit'])
                and value_equal(source['currency'], feature['currency']), 'D16', '/value', 'Panel feature differs from its observation')
        require(available_at(source['available_time'], source['availability_bound_at'], panel['origin_at'], 'D09')
                and feature['availability_bound_at'] == source['availability_bound_at'],
                'D09', '/feature_values', 'Feature is unavailable at its own historical origin')


def mature_for_fit(label, fit_cutoff):
    return (label['label_status'] == 'mature' and is_known(label['outcome'])
            and is_known(label['label_available_at'])
            and instant(label['outcome_end_at']) <= instant(label['label_available_at']['value']) <= instant(fit_cutoff))


def overlaps(left_start, left_end, right_start, right_end):
    return instant(left_start) <= instant(right_end) and instant(right_start) <= instant(left_end)


def verify_endpoint_period(observation, endpoint):
    """非单点经济期间只支持其保守上界，不把旧行情外推到请求终点。"""
    evidence = observation['observation_period']
    upper = time_upper(evidence, 'D11')
    require(upper is not None, 'D11', '/observation_period', 'Valuation economic period is unknown', 'missing_evidence')
    value = evidence['value']
    lower = instant(value['lower_bound'])
    require(value['precision'] != 'instant' or lower == upper, 'D11', '/observation_period', 'Instant precision contains a nonzero interval')
    require(lower <= instant(endpoint) <= upper and instant(endpoint) == upper,
            'D11', '/observation_period', 'Requested valuation endpoint is not supported by the complete source economic period', 'missing_evidence')


def reinvestment_nav(event, start, observations, contracts):
    require(is_known(event['reinvestment_nav_version_id']) and event['reinvestment_nav_version_id']['value'] in observations,
            'D11', '/reinvestment_nav_version_id', 'Dividend reinvestment lacks its actual observed NAV', 'missing_evidence')
    nav = observations[event['reinvestment_nav_version_id']['value']]
    require(nav['entity_id'] == start['entity_id'] == event['entity_id'] and nav['metric_id'] == start['metric_id']
            and nav['measurement_basis'] == 'unit_nav' and is_known(nav['value']) and Fraction(nav['value']['value']) > 0
            and value_equal(nav['unit'], start['unit']) and value_equal(nav['currency'], start['currency'])
            and value_equal(event['currency'], nav['currency']),
            'D11', '/reinvestment_nav_version_id', 'Reinvestment price has the wrong entity, metric, NAV basis, unit or currency')
    price_time, reinvestment = nav['observation_period'], event['reinvestment_at']
    require(time_upper(price_time, 'D11') is not None and time_upper(reinvestment, 'D11') is not None,
            'D11', '/reinvestment_at', 'Reinvestment economic time or price period is unknown', 'missing_evidence')
    require(instant(price_time['value']['lower_bound']) <= instant(reinvestment['value']['lower_bound'])
            and instant(reinvestment['value']['upper_bound']) <= instant(price_time['value']['upper_bound']),
            'D11', '/reinvestment_nav_version_id', 'NAV economic period does not cover the actual reinvestment time', 'missing_evidence')
    require(event['contract_version_id'] in contracts, 'D11', '/contract_version_id', 'Reinvestment contract is absent', 'missing_evidence')
    contract = contracts[event['contract_version_id']]
    require(contract['entity_id'] == event['entity_id'], 'D11', '/contract_version_id', 'Reinvestment contract belongs to another entity')
    terms = [row for row in contract['terms'] if row['term'] == 'dividend_election' and is_known(row['text_value']) and row['citations']]
    require(terms, 'D11', '/contract_version_id', 'Reinvestment lacks a documented dividend election contract term', 'missing_evidence')
    return nav


def label_dependencies(label, observations, events, contracts, *, require_declared=False):
    """从实际运算读取关系重建闭包，不依赖候选自行列出的source_version_ids。"""
    observation_ids = {label[key]['value'] for key in ('start_version_id', 'end_version_id') if is_known(label[key])}
    event_ids, contract_ids = set(), set(label['contract_version_ids'])
    require(observation_ids <= observations.keys(), 'D11', '/source_version_ids', 'Actual label endpoint is unresolved')
    for event in events if label['calculation_basis'] == 'unit_nav_with_documented_equity_events' else ():
        if event['entity_id'] != label['entity_id']:
            continue
        ex = time_upper(event['ex_at'], 'D11')
        require(ex is not None, 'D11', '/ex_at', 'Actual equity economic time is unknown', 'missing_evidence')
        if not (instant(label['origin_at']) < ex <= instant(label['outcome_end_at'])):
            continue
        event_ids.add(event['event_id'])
        contract_ids.add(event['contract_version_id'])
        if event['event_type'] == 'cash_dividend':
            at = time_upper(event['reinvestment_at'], 'D11')
            require(at is not None, 'D11', '/reinvestment_at', 'Actual dividend reinvestment time is unknown', 'missing_evidence')
            if at <= instant(label['outcome_end_at']):
                require(is_known(label['start_version_id']), 'D11', '/start_version_id', 'Reinvestment has no actual initial valuation', 'missing_evidence')
                nav = reinvestment_nav(event, observations[label['start_version_id']['value']], observations, contracts)
                observation_ids.add(nav['version_id'])
    if require_declared:
        require(observation_ids <= set(label['source_version_ids']) and event_ids <= set(label['equity_event_ids'])
                and contract_ids <= set(label['contract_version_ids']), 'D11', '/source_version_ids',
                'Label omitted an actual valuation, reinvestment, equity or contract dependency')
    return {'observations': observation_ids, 'events': event_ids, 'contracts': contract_ids}


def label_availability_upper(label, observations, events, contracts):
    dependencies = label_dependencies(label, observations, events, contracts)
    bounds = [instant(label['outcome_end_at'])]
    for key in set(label['source_version_ids']) | dependencies['observations']:
        source = observations[key]
        require(is_known(source['availability_bound_at']), 'D12', '/source_version_ids', 'Label source availability is unknown', 'missing_evidence')
        bounds.append(instant(source['availability_bound_at']['value']))
    for kind, index in (('events', {row['event_id']: row for row in events}), ('contracts', contracts)):
        for key in dependencies[kind] | (set(label['equity_event_ids']) if kind == 'events' else set()):
            upper = time_upper(index[key]['available_time'], 'D12')
            require(upper is not None, 'D12', '/' + kind, 'A required label dependency has unknown availability', 'missing_evidence')
            bounds.append(upper)
    return max(bounds)


def view_observation_ids(panels, labels, observations, events, contracts):
    versions = {key for row in panels for key in row['source_version_ids']}
    for label in labels:
        versions.update(label['source_version_ids'])
        versions.update(label_dependencies(label, observations, events, contracts)['observations'])
    return versions


def verify_inherited_availability(row, source, bound, pit_allowed):
    """仅比较本链转引/可得性派生；source内容由真实上游负责业务验收。"""
    require(row['available_time'] == source['available_time'] and value_equal(row['availability_bound_at'], bound),
            'D18', '/available_time', 'Inherited availability changed the exact source evidence or derived bound')
    eligible = available_at(source['available_time'], bound, row['origin_at'], 'D18') and pit_allowed
    require(row['eligible'] == eligible, 'D18', '/eligible', 'Inherited eligibility differs from actual time evidence or upstream permitted use')
    require(eligible or row['gap_ids'], 'D18', '/gap_ids', 'Unavailable inherited object lost its concrete Gap')


def reinvested_return(label, observations, events, contracts):
    """从原NAV与实际权益事件独立重建一初始份额的财富；不调用producer。"""
    start, end = (observations[label[name]['value']] for name in ('start_version_id', 'end_version_id'))
    require(start['entity_id'] == end['entity_id'] == label['entity_id'] and start['metric_id'] == end['metric_id'],
            'D11', '/source_version_ids', 'Label endpoints refer to different instruments or metrics')
    require(is_known(start['value']) and is_known(end['value']) and Fraction(start['value']['value']) > 0
            and Fraction(end['value']['value']) >= 0, 'D11', '/outcome', 'Label endpoints cannot form nonnegative wealth')
    require(value_equal(start['currency'], end['currency']) and value_equal(start['currency'], label['currency'])
            and value_equal(start['unit'], end['unit']), 'D11', '/currency', 'Label endpoint units or currencies differ')
    verify_endpoint_period(start, label['origin_at'])
    verify_endpoint_period(end, label['outcome_end_at'])
    initial = Fraction(start['value']['value'])
    if label['calculation_basis'] == 'documented_reinvestment_index':
        require(start['measurement_basis'] == end['measurement_basis'] == 'documented_reinvested_return_index'
                and not label['equity_event_ids'], 'D11', '/calculation_basis', 'Reinvestment index cannot double-count equity events')
        return Fraction(end['value']['value']) / initial - 1
    require(start['measurement_basis'] == end['measurement_basis'] == 'unit_nav',
            'D11', '/calculation_basis', 'Cumulative NAV cannot substitute for a reinvestment index')
    shares, cash = Fraction(1), Fraction(0)
    timeline = []
    for event in events:
        record = time_upper(event['record_at'], 'D11')
        ex = time_upper(event['ex_at'], 'D11')
        require(record is not None and ex is not None and record <= ex,
                'D11', '/ex_at', 'Equity record/ex time is unknown or reversed', 'missing_evidence')
        if not (instant(label['origin_at']) < ex <= instant(label['outcome_end_at'])):
            continue
        require(event['event_id'] in label['equity_event_ids'], 'D11', '/equity_event_ids', 'Label omitted an applicable equity event')
        require(event['contract_version_id'] in contracts and event['contract_version_id'] in label['contract_version_ids'],
                'D11', '/contract_version_id', 'Equity event lacks its applicable contract')
        timeline.append((ex, 0, event))
        if event['event_type'] == 'cash_dividend':
            reinvestment = time_upper(event['reinvestment_at'], 'D11')
            require(reinvestment is not None and reinvestment >= ex, 'D11', '/reinvestment_at', 'Reinvestment timing is unknown or reversed', 'missing_evidence')
            if reinvestment <= instant(label['outcome_end_at']):
                timeline.append((reinvestment, 1, event))
    entitlements = {}
    for _, action, event in sorted(timeline, key=lambda row: (row[0], row[1], row[2]['event_id'])):
        if event['event_type'] in ('split', 'merge'):
            require(is_known(event['share_multiplier']) and Fraction(event['share_multiplier']['value']) > 0,
                    'D11', '/share_multiplier', 'Share event requires a positive disclosed multiplier')
            shares *= Fraction(event['share_multiplier']['value'])
        elif action == 0:
            require(is_known(event['cash_per_share']) and Fraction(event['cash_per_share']['value']) >= 0
                    and value_equal(event['currency'], label['currency']), 'D11', '/cash_per_share', 'Dividend amount or currency is unresolved')
            entitlement = shares * Fraction(event['cash_per_share']['value'])
            cash += entitlement
            entitlements[event['event_id']] = entitlement
        else:
            require(is_known(event['reinvestment_nav_version_id']), 'D11', '/reinvestment_nav_version_id', 'Dividend reinvestment lacks a disclosed NAV', 'missing_evidence')
            nav = reinvestment_nav(event, start, observations, contracts)
            amount = entitlements[event['event_id']]
            shares += amount / Fraction(nav['value']['value'])
            cash -= amount
    return (shares * Fraction(end['value']['value']) + cash) / initial - 1
