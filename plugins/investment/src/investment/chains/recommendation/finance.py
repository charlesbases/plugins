"""原合同精度、费用和有来源假设的独立有理数核算。"""
from __future__ import annotations

from datetime import date
from fractions import Fraction
from zoneinfo import ZoneInfo

from investment.contracts import instant, is_known
from .contracts import ensure, value, unique
from .deterministic import number, exact_instruction


def round_fraction(amount, places, rule):
    """仅按明确来源的最终精度舍入；无浮点与全局 Decimal 精度。"""
    ensure(type(places) is int and places >= 0 and amount >= 0, 'Invalid source rounding inputs')
    scale = 10 ** places
    scaled = amount * scale
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    ensure(rule in ('floor', 'ceiling', 'half_up', 'half_even'), 'Source rounding algorithm is not executable', missing=True)
    if rule == 'ceiling' and remainder:
        whole += 1
    elif rule in ('half_up', 'half_even'):
        twice = remainder * 2
        if twice > scaled.denominator or twice == scaled.denominator and (rule == 'half_up' or whole % 2):
            whole += 1
    return Fraction(whole, scale)


def subscription(payment, nav, rate):
    payment, nav, rate = map(number, (payment, nav, rate))
    ensure(payment > 0 and nav > 0 and rate >= 0, 'Invalid explicit subscription inputs')
    net = payment / (1 + rate)
    return net, payment - net, net / nav


def redemption(shares, nav, rate):
    shares, nav, rate = map(number, (shares, nav, rate))
    ensure(shares > 0 and nav > 0 and 0 <= rate <= 1, 'Invalid explicit redemption inputs')
    gross = shares * nav
    fee = gross * rate
    return gross, fee, gross - fee


def contract_for(s, action):
    cid = value(action['contract_version_id'], 'Original action contract version')
    rows = [r for r in s.contracts if r['contract_version_id'] == cid]
    ensure(len(rows) == 1, 'Exact source contract version is unavailable or duplicated', missing=True)
    contract = rows[0]
    ensure(contract['entity_id'] == value(action['entity_id'], 'Original entity')
           and contract['platform'] == s.account['platform'], 'Contract entity/platform differs')
    ensure(contract['raw_refs'], 'Source contract raw text missing', missing=True)
    for ref in contract['raw_refs']:
        s.ctx.resolver.resolve(ref)
    return contract


def grid_for(s, action):
    rows = [r for r in s.space['grid_rules'] if r['entity_id'] == value(action['entity_id'], 'Original entity')
            and r['contract_version_id'] == value(action['contract_version_id'], 'Original contract version')]
    ensure(len(rows) == 1, 'Exact frozen source grid is not unique', missing=True)
    return rows[0]


def terms(contract):
    return unique([row for row in contract['terms'] if row['term'] != 'other_documented'], 'term')


def source_term(items, name, text=False):
    ensure(name in items, 'Source contract term missing: ' + name, missing=True)
    return value(items[name]['text_value' if text else 'value'], 'Source term ' + name)


def precision(s):
    for action in s.actions:
        if action['side'] == 'hold':
            continue
        contract, grid = contract_for(s, action), grid_for(s, action)
        ensure(grid['status'] == 'ready', 'Original contract grid is not ready', missing=True)
        items = terms(contract)
        names = [('minimum_amount', 'subscription_minimum'), ('amount_step', 'subscription_step')] if action['side'] == 'buy' else [
            ('minimum_shares', 'redemption_minimum_shares'), ('share_step', 'redemption_step_shares'),
            ('minimum_remaining_shares', 'minimum_remaining_shares')]
        for field, term in names:
            ensure(number(value(grid[field], 'Frozen ' + field)) == number(source_term(items, term)),
                   'Frozen grid differs from actual contract: ' + field)
        for field, term in [('money_decimals', 'money_precision'), ('share_decimals', 'share_precision')]:
            source = number(source_term(items, term))
            ensure(source.denominator == 1 and value(grid[field], 'Frozen ' + field) == source, 'Source decimal precision differs')
        ensure(value(grid['rounding_rule'], 'Frozen rounding rule') == source_term(items, 'rounding_rule', text=True),
               'Source rounding rule differs')
        quantity = number(exact_instruction(action))
        minimum, step = ('minimum_amount', 'amount_step') if action['side'] == 'buy' else ('minimum_shares', 'share_step')
        step_value = number(value(grid[step], 'Source instruction step'))
        ensure(step_value > 0 and quantity >= number(value(grid[minimum], 'Source minimum'))
               and (quantity / step_value).denominator == 1, 'Original instruction violates source minimum/step')
        places = value(grid['money_decimals' if action['side'] == 'buy' else 'share_decimals'], 'Source instruction precision')
        ensure((quantity * 10 ** places).denominator == 1, 'Original instruction exceeds source precision')
        if action['side'] == 'sell':
            positions = unique(s.positions, 'instrument_id')
            shares = number(value(positions[value(action['entity_id'], 'Entity')]['confirmed_shares'], 'Registered holding'))
            remainder = shares - quantity
            ensure(remainder >= 0 and (remainder == 0 or remainder >= number(value(grid['minimum_remaining_shares'], 'Minimum remaining shares'))),
                   'Original redemption violates minimum remaining shares')
    for order in s.tables['advisory_orders']:
        ensure(order['precision_contract_ref'] == s.bundle['contract_vintages_ref'], 'Order precision source reference differs')


def holding_days(trade, decision_at, tier):
    ensure(tier['day_count'] == 'calendar_days', 'Fee day-count needs an explicitly executable source calendar', missing=True)
    start = value(trade['holding_start'], 'Actual source-lot holding start')
    if start['kind'] == 'date':
        return (instant(decision_at).astimezone(ZoneInfo(start['timezone'])).date() - date.fromisoformat(start['value'])).days
    return (instant(decision_at).astimezone(ZoneInfo('Asia/Shanghai')).date()
            - instant(start['value']).astimezone(ZoneInfo('Asia/Shanghai')).date()).days


def applicable(tier, amount, days):
    if amount < number(tier['amount_lower']) or days < tier['holding_days_lower']:
        return False
    for field, actual in [('amount_upper', amount), ('holding_days_upper', days)]:
        upper = tier[field]
        ensure(upper['status'] != 'unknown', 'Fee-tier upper bound is unknown', missing=True)
        if is_known(upper) and actual >= (number(upper['value']) if field == 'amount_upper' else upper['value']):
            return False
    return True


def _estimate(s, estimate, expected, action, contract, grid, *, shares=False):
    ensure(estimate['value_basis'] == 'estimated', 'Estimate claims confirmed value')
    ensure(instant(estimate['as_of']) <= instant(s.payload['publication_checked_at']), 'Estimate as_of is after publication check')
    assumptions = s.ctx.resolver.resolve(estimate['assumptions_ref'], 'investment.sc09.estimate_assumptions')
    ensure(assumptions['action_id'] == action['action_id']
           and value(assumptions['contract_version_id'], 'Estimate contract') == contract['contract_version_id']
           and assumptions['fee_tier_ids'] == action['fee_tier_ids'], 'Estimate assumptions do not bind original action/contract/fee tiers')
    ensure(assumptions['rounding_stage'] and assumptions['evidence_refs'] and estimate['evidence_refs'],
           'Estimate source/rounding assumptions missing', missing=True)
    ensure(not assumptions['unknown_inputs'], 'Estimate has unresolved source inputs', missing=True)
    source_calculations = {tier['calculation'] for tier in contract['fees'] if tier['tier_id'] in action['fee_tier_ids']
                           and tier['charge'] == ('subscription' if action['side'] == 'buy' else 'redemption')}
    ensure(source_calculations == {assumptions['formula_kind']} and assumptions['formula_kind'] in ('external_subscription_ratio', 'redemption_gross_ratio', 'fixed_per_trade'),
           'Documented estimate formula has no registered executable calculator', missing=True)
    actual = number(value(estimate['value'], 'Applicable estimate'))
    places = value(grid['share_decimals' if shares else 'money_decimals'], 'Source estimate precision')
    expected = round_fraction(expected, places, value(grid['rounding_rule'], 'Source rounding'))
    ensure(actual == expected, 'Independently recomputed source estimate differs')
    return assumptions


def estimates(s):
    actions, trades = unique(s.actions, 'action_id'), unique(s.trades, 'trade_id')
    observations = unique(s.publication['market_observations'], 'entity_id')
    for order in s.tables['advisory_orders']:
        action = actions[order['source_action_id']]
        contract, grid = contract_for(s, action), grid_for(s, action)
        ensure(order['instrument_id'] in observations, 'Reference NAV observation missing', missing=True)
        obs = observations[order['instrument_id']]
        ensure(order['reference_nav'] == obs['reference_nav'] and order['reference_nav_date'] == obs['reference_nav_date'], 'Reference NAV or date differs from original observed source')
        nav = number(value(order['reference_nav'], 'Source reference NAV'))
        ensure(nav > 0, 'Reference NAV must be positive')
        value(order['reference_nav_date'], 'Source NAV date')
        nav_version = value(obs['reference_nav_version_id'], 'Source NAV version')
        tiers = unique(contract['fees'], 'tier_id')
        ensure(set(action['fee_tier_ids']) <= set(tiers), 'Fee tier absent from source contract')
        result = order['estimated_result']
        applicable_tiers = [tiers[x] for x in action['fee_tier_ids'] if tiers[x]['charge'] == ('subscription' if order['side'] == 'buy' else 'redemption')]
        assumptions = []
        if order['side'] == 'buy':
            ensure(value(order['subscription_payment_includes_fee'], 'Subscription gross payment contract') is True,
                   'Source does not establish gross payment including the subscription fee', missing=True)
            payment = exact_instruction(action)
            matched = [t for t in applicable_tiers if applicable(t, number(payment), 0)]
            ensure(len(matched) == 1, 'Applicable subscription fee tier is not unique', missing=True)
            tier = matched[0]
            if tier['calculation'] == 'external_subscription_ratio':
                net, fee, units = subscription(payment, nav, value(tier['rate'], 'Source subscription rate'))
            else:
                ensure(tier['calculation'] == 'fixed_per_trade', 'Source subscription fee formula is not executable', missing=True)
                ensure(value(tier['currency'], 'Source fixed-fee currency') == 'CNY', 'Fixed subscription fee currency differs')
                fee = number(value(tier['fixed_amount'], 'Source fixed subscription fee'))
                net, units = number(payment) - fee, (number(payment) - fee) / nav
                ensure(net > 0, 'Subscription fee exhausts payment')
            for estimate, amount, share in [(order['fee_estimate'], fee, False),
                                             (result['reference_net_subscription_amount'], net, False),
                                             (result['reference_subscription_shares'], units, True)]:
                assumptions.append(_estimate(s, estimate, amount, action, contract, grid, shares=share))
            ensure(result['reference_redemption_gross']['value']['status'] == 'not_applicable'
                   and result['reference_net_receipt_amount']['value']['status'] == 'not_applicable', 'Subscription asserts a redemption result')
        else:
            ensure(order['subscription_payment_includes_fee']['status'] == 'not_applicable', 'Redemption asserts a subscription fee basis')
            gross, fee = Fraction(), Fraction()
            for lot in value(order['lot_allocations'], 'Actual lot allocations'):
                ensure(lot['trade_id'] in trades, 'Lot trade is absent')
                shares = number(value(lot['shares'], 'Source lot shares'))
                tid = value(lot['fee_tier_id'], 'Source lot fee tier')
                ensure(tid in tiers and tiers[tid]['charge'] == 'redemption', 'Lot fee tier absent or wrong charge')
                tier = tiers[tid]
                source_start = value(trades[lot['trade_id']]['holding_start'], 'Actual source holding start')
                stated_start = value(lot['holding_start'], 'Stated lot holding start')
                if source_start['kind'] == 'instant':
                    ensure(stated_start['precision'] == 'instant' and instant(stated_start['lower_bound']) == instant(stated_start['upper_bound']) == instant(source_start['value']),
                           'Lot holding instant differs from actual original source')
                else:
                    zone = ZoneInfo(source_start['timezone'])
                    ensure(stated_start['precision'] == 'date' and stated_start['timezone'] == source_start['timezone']
                           and instant(stated_start['lower_bound']).astimezone(zone).date().isoformat() == source_start['value']
                           and instant(stated_start['upper_bound']).astimezone(zone).date().isoformat() == source_start['value'],
                           'Lot holding source date or timezone differs')
                days = holding_days(trades[lot['trade_id']], action['decision_at'], tier)
                ensure(value(lot['holding_days'], 'Lot holding days') == days and days >= 0, 'Lot holding days differ from actual source start')
                matched = [t for t in applicable_tiers if applicable(t, number(exact_instruction(action)) * nav, days)]
                ensure(len(matched) == 1 and matched[0]['tier_id'] == tid, 'Source redemption fee-tier selection differs')
                lot_gross = shares * nav
                if tier['calculation'] == 'redemption_gross_ratio':
                    rate = value(tier['rate'], 'Actual redemption fee rate')
                    ensure(lot['fee_rate'] == tier['rate'], 'Lot fee rate differs')
                    _, lot_fee, _ = redemption(shares, nav, rate)
                else:
                    ensure(tier['calculation'] == 'fixed_per_trade' and len(order['lot_allocations']['value']) == 1,
                           'Fixed redemption fee needs a source-supported per-order allocation', missing=True)
                    ensure(value(tier['currency'], 'Source fixed-fee currency') == 'CNY', 'Fixed redemption fee currency differs')
                    lot_fee = number(value(tier['fixed_amount'], 'Source fixed redemption fee'))
                    ensure(lot['fee_rate']['status'] == 'not_applicable', 'Fixed fee claims an ad valorem rate')
                gross += lot_gross
                fee += lot_fee
                assumptions.append(_estimate(s, lot['fee_estimate'], lot_fee, action, contract, grid))
            for estimate, amount in [(order['fee_estimate'], fee), (result['reference_redemption_gross'], gross),
                                     (result['reference_net_receipt_amount'], gross - fee)]:
                assumptions.append(_estimate(s, estimate, amount, action, contract, grid))
            ensure(result['reference_subscription_shares']['value']['status'] == 'not_applicable'
                   and result['reference_net_subscription_amount']['value']['status'] == 'not_applicable', 'Redemption asserts subscription result')
        for assumption in assumptions:
            ensure(value(assumption['reference_nav_version_id'], 'Estimate NAV version') == nav_version
                   and order['reference_nav_date']['value'] in assumption['source_dates'], 'Estimate NAV/date assumptions differ')
        arrival = result['cash_arrival']
        ensure(arrival['value_basis'] == 'estimated' and instant(arrival['as_of']) <= instant(s.payload['publication_checked_at']), 'Cash arrival misrepresents actual settlement')
        if is_known(arrival['value']):
            ensure(arrival['evidence_refs'], 'Estimated arrival source missing', missing=True)
            origin = action['earliest_cash_arrival_at']
            ensure(is_known(origin), 'Original action has no source-supported arrival bound', missing=True)
            body = arrival['value']['value']
            ensure(body['precision'] == 'instant' and body['lower_bound'] == body['upper_bound'] == origin['value'],
                   'Estimated arrival differs from original supported earliest arrival')
            assumption = s.ctx.resolver.resolve(arrival['assumptions_ref'], 'investment.sc09.estimate_assumptions')
            ensure(assumption['action_id'] == action['action_id'], 'Arrival assumptions bind another action')


def funding(s):
    rows = s.tables['funding_conditions']
    unique(rows, 'condition_id')
    actions = {r['action_id'] for r in s.actions}
    trades = unique(s.trades, 'trade_id')
    for row in rows:
        ensure(set(row['action_ids']) <= actions, 'Funding condition references absent actions')
        if row['source_kind'] == 'current_plan_confirmation':
            ensure(row['status'] == 'fulfilled' and row['effect'] == 'confirmed_current_plan'
                   and value(row['verified_funding_plan_ref'], 'Current funding plan') == s.refs['funding_plan_ref'],
                   'Current funding confirmation is not the exact independent plan')
            continue
        ensure(row['effect'] == 'wait_update_facts_and_replan', 'Future funds were treated as current planning cash')
        if row['source_kind'] == 'expected_settlement':
            tid = value(row['source_trade_id'], 'Settlement source trade')
            ensure(tid in trades and trades[tid]['side'] == 'sell', 'Settlement condition lacks an original redemption')
            trade = trades[tid]
            if row['status'] == 'fulfilled':
                ensure(trade['settlement']['state'] == 'settled' and trade['settlement']['basis'] == 'user_reported'
                       and is_known(trade['settlement']['actual_at']), 'Expected due date was promoted to actual arrival')
            else:
                ensure(row['verified_funding_plan_ref']['status'] != 'known', 'Unarrived cash asserts an updated available plan')
    for pending in s.pending:
        if pending['side'] == 'sell' and pending['pending_phase'] == 'settlement':
            matches = [r for r in rows if r['source_kind'] == 'expected_settlement' and is_known(r['source_trade_id'])
                       and r['source_trade_id']['value'] == pending['trade_id']]
            ensure(len(matches) == 1, 'Pending receivable condition was omitted or duplicated')
    contributions = value(s.funding['future_contributions'], 'Future contribution declaration')
    for contribution in contributions:
        matches = [r for r in rows if r['source_kind'] == 'future_contribution'
                   and contribution['source_ref'] in r['evidence_refs']
                   and is_known(r['required_amount']) and r['required_amount']['value'] == contribution['amount']]
        ensure(len(matches) == 1, 'Original future contribution condition omitted or duplicated')
        row = matches[0]
        ensure(row['status'] != 'fulfilled' and row['verified_funding_plan_ref']['status'] != 'known',
               'Future planned contribution was promoted to actual available funds')
        if is_known(row['availability']):
            ensure(row['availability']['value']['precision'] == 'date'
                   and row['availability']['value']['lower_bound'][:10] == contribution['contribution_date'],
                   'Future contribution date changed')
    total = sum((number(exact_instruction(r)) for r in s.actions if r['side'] == 'buy'), Fraction())
    if s.tables['advisory_orders']:
        ensure(total <= number(value(s.funding['available_amount'], 'Independent available planning budget')),
               'Advice spends future/unarrived cash or exceeds the exact declared budget')
        ensure(not any(r['action_ids'] and r['effect'] == 'wait_update_facts_and_replan' for r in rows),
               'Advice executes before required future funding facts and replanning')
