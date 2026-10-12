"""独立核原期限的源日历计数及共同预计经济期间索引。"""
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from investment.contracts import ContractError, instant, is_known, require
from .contracts import index, known


def _available(row, cutoff):
    evidence = row['available_time']
    return is_known(evidence) and instant(evidence['value']['upper_bound']) <= cutoff


def _target_day(calendar, horizon, cutoff, timezone):
    origin = cutoff.astimezone(timezone).date()
    boundary = origin + timedelta(days=horizon['length'])
    day, count = origin + timedelta(days=1), 0
    last = date.fromisoformat(max(calendar))
    while day <= last:
        key = day.isoformat()
        require(key in calendar, 'F13', '/calendar_ref', 'Horizon has an undocumented calendar day', 'missing_evidence')
        row = calendar[key]
        require(_available(row, cutoff) and is_known(row['is_trading_day']),
                'F13', '/calendar_ref', 'Required calendar day was unknown or unavailable at C', 'missing_evidence')
        if row['is_trading_day']['value']:
            count += 1
            if (horizon['unit'] == 'trading_day' and count == horizon['length']
                    or horizon['unit'] == 'calendar_day' and day >= boundary):
                return key
        day += timedelta(days=1)
    raise ContractError('F13', '/calendar_ref', 'Calendar does not document the full requested horizon', 'missing_evidence')


def _calendar_period(rows, horizon, cutoff):
    calendar = {key[0]: row for key, row in index(rows, ('date',), 'F13').items()}
    # 不拿订单截止或旧NAV钟补时区；所有可得源时区都要导出同一目标。
    zones = {row['scheduled_valuation_period']['value']['timezone'] for row in rows
             if _available(row, cutoff) and is_known(row['scheduled_valuation_period'])}
    require(zones, 'F13', '/scheduled_valuation_period', 'No source timezone documents the requested economic schedule', 'missing_evidence')
    targets = set()
    for name in sorted(zones):
        try:
            timezone = ZoneInfo(name)
        except ZoneInfoNotFoundError as error:
            raise ContractError('F13', '/timezone', 'Source timezone database is unavailable', 'missing_evidence') from error
        target = _target_day(calendar, horizon, cutoff, timezone)
        value = known(calendar[target]['scheduled_valuation_period'], 'F13', '/scheduled_valuation_period')
        period = (instant(value['lower_bound']), instant(value['upper_bound']), value['precision'], value['timezone'])
        targets.add((target, period))
    require(len(targets) == 1, 'F13', '/scheduled_valuation_period', 'Source clocks imply different target dates or economic periods', 'missing_evidence')
    return targets.pop()


def future_endpoints(rows, horizons, entities, cutoff):
    """由已准日历重建全体required实体端点，不消费未来标签或原始源。"""
    result, periods = {}, {}
    for horizon in horizons:
        if horizon['unit'] == 'calendar_day' and horizon['end_rule'] == 'exact_calendar_boundary':
            # 沿上游原精确时长目标，不能改成另一个估值日或默认午夜。
            endpoint = cutoff + timedelta(days=horizon['length'])
        else:
            require(entities, 'F13', '/required_coordinates', 'Required forecast entities are unresolved', 'missing_evidence')
            targets = {_calendar_period([row for row in rows if row['entity_id'] == entity
                                         and row['calendar_id'] == horizon['calendar_id']], horizon, cutoff)
                       for entity in sorted(entities)}
            require(len(targets) == 1, 'F13', '/scheduled_valuation_period', 'Required entities do not share the full target date and economic period', 'missing_evidence')
            key = targets.pop()
            endpoint = key[1][1]
            # exact只约束原数字边界；同upper的真实期间身份由这些有据目标提供。
            require(endpoint not in periods or periods[endpoint] == key,
                    'F13', '/scheduled_valuation_period', 'Equal upper indices represent different economic periods', 'missing_evidence')
            periods[endpoint] = key
        require(endpoint > cutoff, 'F13', '/horizon_endpoint', 'Economic endpoint does not follow C', 'missing_evidence')
        result[horizon['horizon_id']] = endpoint
    return result
