"""独立赎回确认/到账算术；合同、身份、时点、到账事实由上层核实。

仅支持gross与fee采用同一明确来源精度/舍入规则的末端舍入。
不解析估值来源、任意分笔合同、申购、分红或利息事件，不据此授予用途。
"""
from copy import deepcopy
from fractions import Fraction
import re

from .validation_numbers import decimal_text, finite_fraction, rounded_decimal

_COMPONENTS = ('budget_cash', 'reserved_cash', 'pending_asset', 'receivable',
               'fee_payable', 'position_value', 'wealth')
_LOT_FIELDS = ('confirmed_shares', 'locked_shares', 'market_value')
_DECIMAL = re.compile(r'-?(0|[1-9][0-9]{0,29})(\.[0-9]{1,18})?')


def _value(optional):
    if (not isinstance(optional, dict) or set(optional) != {'status', 'value', 'reason'}
            or optional['status'] != 'known' or optional['reason'] is not None):
        raise ValueError('Financial arithmetic requires an explicit known value')
    text = optional['value']
    if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
        raise ValueError('Financial value must preserve DecimalString format')
    return finite_fraction(text)


def _known(value):
    optional = {'status': 'known', 'value': decimal_text(value), 'reason': None}
    _value(optional)
    return optional


def _wealth(values):
    return (values['budget_cash'] + values['reserved_cash'] + values['pending_asset']
            + values['receivable'] + values['position_value'] - values['fee_payable'])


def _state_values(state):
    values = {field: _value(state[field]) for field in _COMPONENTS}
    if any(values[field] < 0 for field in _COMPONENTS if field != 'wealth'):
        raise ValueError('Financial components cannot be negative')
    if values['wealth'] != _wealth(values):
        raise ValueError('Input wealth does not match the financial identity')
    return values


def redemption_confirmation_expected(state, *, lot_id, quantity, nav, fee_rate,
                                     removed_market_value, decimal_places, rounding):
    """从原FinancialState/LotState推导；removed_market_value须有原估值依据。

    精确raw_gross=q*NAV、raw_fee=raw_gross*r，各自末端来源舍入，
    net=gross-fee。适用同一来源舍入条件；不推断未提供的合同规则。
    """
    values = _state_values(state)
    matching = [index for index, lot in enumerate(state['lots']) if lot['lot_id'] == lot_id]
    if len(matching) != 1:
        raise ValueError('Exactly one original lot must match')
    index = matching[0]
    lot_values = {field: _value(state['lots'][index][field]) for field in _LOT_FIELDS}
    quantity, nav, rate, removed = map(finite_fraction,
                                      (quantity, nav, fee_rate, removed_market_value))
    shares, locked, market = (lot_values[field] for field in _LOT_FIELDS)
    if not 0 < quantity <= locked <= shares or nav <= 0 or not 0 <= rate <= 1:
        raise ValueError('Expected legal positive shares, locked lot, NAV and fee rate')
    if not 0 <= removed <= market or removed > values['position_value']:
        raise ValueError('Removed book value must come from the original valuation')
    if type(decimal_places) is not int or not 0 <= decimal_places <= 18:
        raise ValueError('Explicit financial precision must fit DecimalString')
    raw_gross = quantity * nav
    raw_fee = raw_gross * rate
    gross = Fraction(rounded_decimal(raw_gross, places=decimal_places, rounding=rounding))
    fee = Fraction(rounded_decimal(raw_fee, places=decimal_places, rounding=rounding))
    net = gross - fee
    remaining = {'confirmed_shares': shares - quantity, 'locked_shares': locked - quantity,
                 'market_value': market - removed}
    if remaining['confirmed_shares'] == 0 and any(remaining.values()):
        raise ValueError('Fully redeemed original lot must have no remaining lock or value')
    after = deepcopy(state)
    for field, value in remaining.items():
        after['lots'][index][field] = _known(value)
    values['receivable'] += net
    values['position_value'] -= removed
    values['wealth'] = _wealth(values)
    for field, value in values.items():
        after[field] = _known(value)
    amounts = {'gross_amount': gross, 'fee_amount': fee, 'net_amount': net,
               'quantity': quantity, 'nav': nav}
    return after, amounts


def settlement_expected(state, *, confirmed_net):
    """仅转移已证属于本笔的净应收；不核实到账、不二扣份额或费。"""
    values = _state_values(state)
    net = finite_fraction(confirmed_net)
    if not 0 < net <= values['receivable']:
        raise ValueError('Confirmed settlement must be within existing receivables')
    after = deepcopy(state)
    values['receivable'] -= net
    values['budget_cash'] += net
    values['wealth'] = _wealth(values)
    for field, value in values.items():
        after[field] = _known(value)
    return after


def reconciliation_differences(expected, actual):
    """内部路径诊断，非对外LedgerDifference序列化；差额为actual-expected。

    包括lot原市值；完整validator仍需核身份/来源并映射既有强格式。
    """
    expected_values = _state_values(expected)
    actual_values = {field: _value(actual[field]) for field in _COMPONENTS}
    pairs = {field: (expected_values[field], actual_values[field], 'CNY')
             for field in _COMPONENTS}
    expected_lots = {lot['lot_id']: lot for lot in expected['lots']}
    actual_lots = {lot['lot_id']: lot for lot in actual['lots']}
    if (len(expected_lots) != len(expected['lots']) or len(actual_lots) != len(actual['lots'])
            or expected_lots.keys() != actual_lots.keys()):
        raise ValueError('Reconciliation requires exactly matching original lot identities')
    for lot_id, lot in expected_lots.items():
        for field in _LOT_FIELDS:
            unit = 'CNY' if field == 'market_value' else 'share'
            pairs[f'lots/{lot_id}/{field}'] = (_value(lot[field]),
                                               _value(actual_lots[lot_id][field]), unit)
    return {path: {'expected': before, 'actual': after, 'difference': after - before,
                   'unit': unit, 'tolerance': Fraction(0)}
            for path, (before, after, unit) in pairs.items()}
