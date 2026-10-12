"""按原指令、已确认份额和明确合同独立推导参考估计。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from fractions import Fraction

from investment.contracts import ContractError, instant, is_known, na, require, unknown
from .production_inputs import gap, present, stable_id
from .production_publication import raw_time_evidence


def number(optional, path):
    require(is_known(optional), 'R09', path, 'Required exact value is unknown', 'missing_evidence')
    return Fraction(optional['value'])


def rounded(value, decimals, rule):
    """对有理数仅在源合同指定的最后阶段舍入，不使用有限Decimal上下文。"""
    require(type(decimals) is int and decimals >= 0, 'R09', '/precision', 'Invalid source decimal precision')
    scaled = value * 10 ** decimals
    lower, remainder = divmod(scaled.numerator, scaled.denominator)
    if rule == 'floor':
        whole = lower
    elif rule == 'ceiling':
        whole = lower + bool(remainder)
    elif rule in ('half_up', 'half_even'):
        doubled = remainder * 2
        whole = lower + (doubled > scaled.denominator or
                         (doubled == scaled.denominator and (rule == 'half_up' or lower % 2 != 0)))
    else:
        raise ContractError('R09', '/rounding_rule', 'Source rounding rule has no executable registered formula', 'missing_evidence')
    sign = '-' if whole < 0 else ''
    text = str(abs(whole)).zfill(decimals + 1)
    return sign + (text[:-decimals] + '.' + text[-decimals:] if decimals else text)


def exact_decimal(value):
    denominator, decimals = value.denominator, 0
    while denominator > 1 and denominator % 2 == 0:
        denominator //= 2
        decimals += 1
    fives = 0
    while denominator > 1 and denominator % 5 == 0:
        denominator //= 5
        fives += 1
    require(denominator == 1, 'R07', '/lot_allocations', 'Original lot fraction has no exact decimal representation')
    return rounded(value, max(decimals, fives), 'floor')


def _term(contract, name):
    matches = [row for row in contract['terms'] if row['term'] == name]
    require(len(matches) == 1, 'R08', '/terms/' + name, 'No unique source contract term: ' + name, 'missing_evidence')
    return matches[0]


def _precision(contract, grid):
    money = number(_term(contract, 'money_precision')['value'], '/money_precision')
    shares = number(_term(contract, 'share_precision')['value'], '/share_precision')
    source_rule = _term(contract, 'rounding_rule')['text_value']
    require(money.denominator == shares.denominator == 1 and is_known(source_rule),
            'R08', '/precision', 'Source precision or rounding is not explicit', 'missing_evidence')
    require(is_known(grid['money_decimals']) and grid['money_decimals']['value'] == money
            and is_known(grid['share_decimals']) and grid['share_decimals']['value'] == shares
            and is_known(grid['rounding_rule']) and grid['rounding_rule']['value'] == source_rule['value'],
            'R08', '/grid_rules', 'Frozen grid differs from source contract precision')
    return int(money), int(shares), source_rule['value']


def resolve_lots(action, states, trades, positions, selected, grid):
    """只恢复各情景一致的原首步lot份额；历史卖出归属不明时拒绝猜测。"""
    require(selected is not None, 'R07', '/selected_rule_ref', 'Missing original allocation rule', 'missing_evidence')
    entity = action['entity_id']['value']
    dimensions = [row for row in selected['allocation_dimensions'] if row['side'] == 'sell'
                  and is_known(row['entity_id']) and row['entity_id']['value'] == entity]
    require(len(dimensions) == 1, 'R07', '/allocation_dimensions', 'No unique original sell allocation dimension', 'missing_evidence')
    dimension = dimensions[0]
    require(not set(action['lot_trade_ids']) & set(grid['locked_trade_ids']),
            'R07', '/lot_trade_ids', 'Original action includes locked confirmed shares')
    scenarios = sorted({row['scenario_id'] for row in states})
    allocations = []
    for scenario in scenarios:
        allocation = {}
        matching = [row for row in states if row['scenario_id'] == scenario and row['event'] == 'redemption_order'
                    and is_known(row['action_id']) and row['action_id']['value'] == action['action_id']]
        require(matching, 'R07', '/cash_schedule_ref', 'A scenario lacks the exact original first-step sell allocation', 'missing_evidence')
        for row in matching:
            require(row['at'] == action['decision_at'] and is_known(row['trade_id'])
                    and is_known(row['share_delta']), 'R07', '/cash_schedule_ref',
                    'Original first-step lot identity or quantity is unavailable', 'missing_evidence')
            trade_id = row['trade_id']['value']
            delta = number(row['share_delta'], '/share_delta')
            require(delta < 0, 'R07', '/share_delta', 'Redemption order must remove positive confirmed shares')
            allocation[trade_id] = allocation.get(trade_id, Fraction()) - delta
        allocations.append(allocation)
    require(allocations and all(item == allocations[0] for item in allocations),
            'R07', '/cash_schedule_ref', 'Original allocation differs across scenarios', 'missing_evidence')
    allocation = allocations[0]
    require(set(allocation) == set(action['lot_trade_ids'])
            and sum(allocation.values()) == number(action['shares'], '/shares'),
            'R07', '/lot_trade_ids', 'Original lot allocation does not conserve the exact requested shares')
    if dimension['lot_rule'] == 'specific_confirmed_trades':
        require(set(action['lot_trade_ids']) <= set(dimension['lot_trade_ids']),
                'R07', '/lot_trade_ids', 'Action exceeds the original specifically permitted lots')
    buys = [row for row in trades if row['instrument_id'] == entity and row['side'] == 'buy'
            and row['confirmation']['state'] == 'confirmed']
    sales = [row for row in trades if row['instrument_id'] == entity and row['side'] == 'sell'
             and row['confirmation']['state'] == 'confirmed']
    current = [row for row in positions if row['instrument_id'] == entity]
    require(len(current) == 1, 'R07', '/positions', 'No unique registered position', 'missing_evidence')
    total = number(current[0]['confirmed_shares'], '/confirmed_shares')
    if sales:
        require(len(buys) == 1, 'R07', '/trades', 'Earlier sales do not identify remaining shares per historical buy lot', 'missing_evidence')
        available = {buys[0]['trade_id']: total}
    else:
        available = {row['trade_id']: number(row['confirmed_shares'], '/confirmed_shares') for row in buys}
        require(sum(available.values()) == total, 'R07', '/positions', 'Confirmed buy lots differ from registered position')
    require(all(trade in available and amount <= available[trade] for trade, amount in allocation.items()),
            'R07', '/lot_allocations', 'Original allocation exceeds a confirmed current lot')
    if dimension['lot_rule'] == 'fifo':
        eligible = [row for row in buys if row['trade_id'] not in grid['locked_trade_ids']
                    and available.get(row['trade_id'], 0) > 0]
        def bounds(row):
            if not is_known(row['holding_start']):
                return None
            raw = row['holding_start']['value']
            if raw['kind'] == 'instant':
                point = instant(raw['value'])
                return point, point
            start = datetime.combine(date.fromisoformat(raw['value']), time.min, ZoneInfo(raw['timezone']))
            return start, start + timedelta(days=1) - timedelta(microseconds=1)
        # StateRow已唯一规定当前批次；只禁止越过有证据严格更早且尚未耗尽的lot。
        # 同日或精度区间重叠不发明平级顺序，也不重新选择原分配。
        for row in eligible:
            current_bounds = bounds(row)
            if row['trade_id'] not in allocation or current_bounds is None:
                continue
            for earlier in eligible:
                earlier_bounds = bounds(earlier)
                if earlier_bounds is not None and earlier_bounds[1] < current_bounds[0]:
                    require(allocation.get(earlier['trade_id'], Fraction()) == available[earlier['trade_id']],
                            'R07', '/lot_allocations', 'Original allocation bypasses a provably earlier available lot')
    return [(next(row for row in buys if row['trade_id'] == trade), allocation[trade])
            for trade in action['lot_trade_ids']]


def _tier(contract, action, amount, holding_days=None):
    charge = 'subscription' if action['side'] == 'buy' else 'redemption'
    matches = []
    for row in contract['fees']:
        if row['tier_id'] not in action['fee_tier_ids'] or row['charge'] != charge:
            continue
        upper = row['amount_upper']
        if amount < Fraction(row['amount_lower']) or (is_known(upper) and amount >= Fraction(upper['value'])):
            continue
        require(upper['status'] != 'unknown', 'R09', '/amount_upper', 'Fee tier upper boundary is unknown', 'missing_evidence')
        if holding_days is not None:
            end = row['holding_days_upper']
            if holding_days < row['holding_days_lower'] or (is_known(end) and holding_days >= end['value']):
                continue
            require(end['status'] != 'unknown', 'R09', '/holding_days_upper', 'Holding fee boundary is unknown', 'missing_evidence')
        matches.append(row)
    require(len(matches) == 1, 'R09', '/fee_tier_ids', 'No uniquely applicable original fee tier', 'missing_evidence')
    tier = matches[0]
    require(is_known(tier['currency']) and tier['currency']['value'] == 'CNY', 'R09', '/currency',
            'Source cash fee currency is not confirmed CNY', 'missing_evidence')
    return tier


def build_estimates(action, contract, grid, observation, lots=()):
    """返回原合同下的精确参考量；调用者负责持久化真实假设与Estimate载体。"""
    money_decimals, share_decimals, rule = _precision(contract, grid)
    nav = number(observation['reference_nav'], '/reference_nav')
    require(nav > 0 and is_known(observation['reference_nav_date'])
            and is_known(observation['reference_nav_version_id']),
            'R09', '/reference_nav', 'A positive dated reference NAV vintage is required', 'missing_evidence')
    quantities = {}
    fees, lot_results = [], []
    if action['side'] == 'buy':
        payment = number(action['amount'], '/amount')
        require(payment > 0, 'R05', '/amount', 'Original subscription payment must be positive')
        tier = _tier(contract, action, payment)
        if tier['calculation'] == 'external_subscription_ratio':
            rate = number(tier['rate'], '/rate')
            require(rate >= 0, 'R09', '/rate', 'Negative source fee rate')
            net = payment / (1 + rate)
        elif tier['calculation'] == 'fixed_per_trade':
            net = payment - number(tier['fixed_amount'], '/fixed_amount')
        else:
            raise ContractError('R09', '/calculation', 'Source subscription formula is not executable', 'missing_evidence')
        require(net > 0, 'R09', '/net_subscription', 'Source fee exhausts the original payment')
        fees.append(payment - net)
        quantities.update(reference_net_subscription_amount=net, reference_subscription_shares=net / nav)
        formula = tier['calculation']
        selected_tiers = [tier['tier_id']]
    else:
        quantity = number(action['shares'], '/shares')
        gross = quantity * nav
        for trade, shares in lots:
            holding = trade['holding_start']
            require(is_known(holding), 'R09', '/holding_start', 'Original holding start is unknown', 'missing_evidence')
            source = holding['value']
            if source['kind'] == 'date':
                zone = ZoneInfo(source['timezone'])
                origin_date = date.fromisoformat(source['value'])
            else:
                original = datetime.fromisoformat(source['value'].replace('Z', '+00:00'))
                zone, origin_date = original.tzinfo, original.date()
            days = (instant(action['decision_at']).astimezone(zone).date() - origin_date).days
            tier = _tier(contract, action, gross, days)
            require(tier['day_count'] == 'calendar_days', 'R09', '/day_count',
                    'Source holding day-count has no independent executable calendar basis', 'missing_evidence')
            if tier['calculation'] == 'redemption_gross_ratio':
                rate = number(tier['rate'], '/rate')
                require(rate >= 0, 'R09', '/rate', 'Negative source redemption rate')
                fee = shares * nav * rate
            elif tier['calculation'] == 'fixed_per_trade':
                require(len(lots) == 1, 'R09', '/fixed_amount', 'Source does not assign a per-trade fee among multiple lots', 'missing_evidence')
                rate, fee = None, number(tier['fixed_amount'], '/fixed_amount')
            else:
                raise ContractError('R09', '/calculation', 'Source redemption formula is not executable', 'missing_evidence')
            fees.append(fee)
            lot_results.append((trade, shares, days, tier, rate, rounded(fee, money_decimals, rule)))
        require(lots and sum(shares for _, shares in lots) == quantity, 'R07', '/lots', 'Exact sell lot shares are unavailable', 'missing_evidence')
        quantities.update(reference_redemption_gross=gross, reference_net_receipt_amount=gross - sum(fees))
        formula = 'redemption_gross_ratio' if all(item[3]['calculation'] == 'redemption_gross_ratio' for item in lot_results) else 'fixed_per_trade'
        selected_tiers = sorted({item[3]['tier_id'] for item in lot_results})
    quantities['fee_estimate'] = sum(fees)
    return {'values': {key: rounded(amount, share_decimals if key == 'reference_subscription_shares' else money_decimals, rule)
                       for key, amount in quantities.items()},
            'formula_kind': formula, 'tier_ids': selected_tiers, 'lots': lot_results}


def _validate_grid(action, contract, grid, positions):
    side = action['side']
    quantity = number(action['amount' if side == 'buy' else 'shares'], '/original_quantity')
    names = (('subscription_minimum', 'minimum_amount'), ('subscription_step', 'amount_step')) if side == 'buy' else (
        ('redemption_minimum_shares', 'minimum_shares'), ('redemption_step_shares', 'share_step'))
    for term_name, grid_name in names:
        source = _term(contract, term_name)['value']
        frozen = grid[grid_name]
        require(source['status'] == frozen['status']
                and (not is_known(source) or Fraction(source['value']) == Fraction(frozen['value'])),
                'R08', '/grid_rules/' + grid_name, 'Frozen grid differs from the original source trade term')
        require(source['status'] != 'unknown', 'R08', '/terms/' + term_name, 'Original trade term is unknown', 'missing_evidence')
        if is_known(source):
            limit = Fraction(source['value'])
            require((quantity >= limit) if 'minimum' in term_name else (limit > 0 and (quantity / limit).denominator == 1),
                    'R08', '/original_quantity', 'Original quantity violates the source minimum or step')
    if side == 'sell':
        source = _term(contract, 'minimum_remaining_shares')['value']
        frozen = grid['minimum_remaining_shares']
        require(source['status'] == frozen['status'] and (not is_known(source)
                or Fraction(source['value']) == Fraction(frozen['value'])), 'R08', '/minimum_remaining_shares',
                'Frozen remaining-share constraint differs from the source')
        require(source['status'] != 'unknown', 'R08', '/minimum_remaining_shares',
                'Source remaining-share constraint is unknown', 'missing_evidence')
        current = [row for row in positions if row['instrument_id'] == action['entity_id']['value']]
        require(len(current) == 1, 'R08', '/positions', 'No unique registered position', 'missing_evidence')
        remaining = number(current[0]['confirmed_shares'], '/confirmed_shares') - quantity
        require(remaining >= 0 and (not is_known(source) or remaining == 0 or remaining >= Fraction(source['value'])),
                'R08', '/minimum_remaining_shares', 'Original redemption violates the remaining-share constraint')


def build_orders(inputs):
    orders, assumptions, issues = [], [], []
    for action in inputs.actions:
        if action['side'] == 'hold':
            continue
        try:
            require(is_known(action['entity_id']) and is_known(action['contract_version_id']),
                    'R05', '/actions/' + action['action_id'], 'Original action identity or contract is unknown', 'missing_evidence')
            contracts = [row for row in inputs.contracts if row['contract_version_id'] == action['contract_version_id']['value']]
            grids = [row for row in inputs.space['grid_rules'] if row['entity_id'] == action['entity_id']['value']
                     and row['contract_version_id'] == action['contract_version_id']['value']]
            observations = [row for row in inputs.publication['market_observations']
                            if row['entity_id'] == action['entity_id']['value']]
            require(len(contracts) == len(grids) == len(observations) == 1, 'R09', '/actions/' + action['action_id'],
                    'Original contract, grid, or reference observation is not unique', 'missing_evidence')
            contract, grid, observation = contracts[0], grids[0], observations[0]
            _validate_grid(action, contract, grid, inputs.positions)
            lots = resolve_lots(action, inputs.states, inputs.trades, inputs.positions, inputs.selected, grid) if action['side'] == 'sell' else ()
            estimates = build_estimates(action, contract, grid, observation, lots)
            evidence = [inputs.policy['current_actions_ref'], inputs.bundle['contract_vintages_ref'],
                        inputs.refs['publication_context_ref'], *contract['raw_refs']]
            assumption = {
                'assumptions_id': stable_id('estimate-', [action['action_id']]), 'action_id': action['action_id'],
                'formula_kind': estimates['formula_kind'],
                'formula': 'Source formula; exact intermediate arithmetic and source precision at the final result.',
                'contract_version_id': present(contract['contract_version_id']), 'fee_tier_ids': estimates['tier_ids'],
                'reference_nav_version_id': observation['reference_nav_version_id'],
                'source_dates': [observation['reference_nav_date']['value']],
                'rounding_stage': 'Final source-defined money/share precision; original order text is never rounded.',
                'unknown_inputs': [], 'evidence_refs': evidence, 'gaps': []}
            reference = inputs.ctx.put_json(stable_id('estimate-', [action['action_id']]) + '.json', assumption, 'investment.sc09.estimate_assumptions')
            assumptions.append(reference)
            cash_arrival_ref = reference
            if action['side'] == 'sell':
                arrival_assumption = dict(
                    assumption, assumptions_id=stable_id('arrival-estimate-', [action['action_id']]),
                    unknown_inputs=['physical_cash_arrival'],
                    gaps=[gap('R09', '/actions/' + action['action_id'] + '/cash_arrival',
                              'SC07 conservative earliest index alone does not establish physical arrival precision.',
                              'settlement_confirmation')])
                cash_arrival_ref = inputs.ctx.put_json(
                    arrival_assumption['assumptions_id'] + '.json', arrival_assumption,
                    'investment.sc09.estimate_assumptions')
                assumptions.append(cash_arrival_ref)
                issues.extend(arrival_assumption['gaps'])
            def estimate(key):
                optional = present(estimates['values'][key]) if key in estimates['values'] else na('other_trade_direction')
                return {'value': optional, 'value_basis': 'estimated', 'assumptions_ref': reference,
                        'as_of': inputs.parameters['publication_checked_at'], 'evidence_refs': evidence}
            result = {key: estimate(key) for key in ('reference_subscription_shares', 'reference_net_subscription_amount',
                                                     'reference_redemption_gross', 'reference_net_receipt_amount')}
            result['cash_arrival'] = {
                'value': unknown('physical_arrival_requires_source_time_evidence') if action['side'] == 'sell' else na('subscription_not_cash_receipt'),
                'value_basis': 'estimated', 'assumptions_ref': cash_arrival_ref,
                'as_of': inputs.parameters['publication_checked_at'], 'evidence_refs': evidence}
            allocations = []
            for trade, shares, days, tier, rate, fee in estimates['lots']:
                allocations.append({'trade_id': trade['trade_id'], 'shares': present(exact_decimal(shares)),
                                    'fee_tier_id': present(tier['tier_id']), 'holding_start': raw_time_evidence(trade['holding_start'], inputs.account['trades_ref']),
                                    'holding_days': present(days), 'fee_rate': present(tier['rate']['value']) if rate is not None else na('fixed_fee'),
                                    'fee_estimate': dict(estimate('fee_estimate'), value=present(fee)),
                                    'allocation_rule_ref': inputs.policy['selected_rule_ref']['value'],
                                    'evidence_refs': [inputs.account['trades_ref'], inputs.policy['cash_schedule_ref'], *evidence]})
            orders.append({
                'advisory_order_id': stable_id('order-', [action['action_id']]), 'source_action_id': action['action_id'],
                'instrument_id': action['entity_id']['value'], 'platform': contract['platform'], 'currency': 'CNY',
                'side': action['side'], 'subscription_cash_amount': dict(action['amount']) if action['side'] == 'buy' else na('sell_uses_shares'),
                'redemption_shares': dict(action['shares']) if action['side'] == 'sell' else na('buy_uses_payment'),
                'subscription_payment_includes_fee': present(True) if action['side'] == 'buy' else na('sell_uses_shares'),
                'lot_allocations': present(allocations) if allocations else na('subscription_has_no_redemption_lots'),
                'fee_estimate': estimate('fee_estimate'), 'reference_nav': observation['reference_nav'],
                'nav_unit': 'CNY_per_share', 'reference_nav_date': observation['reference_nav_date'],
                'estimated_result': result, 'precision_contract_ref': inputs.bundle['contract_vintages_ref'],
                'contract_version_id': contract['contract_version_id'], 'fee_tier_ids': action['fee_tier_ids'],
                'execution_condition_ids': [], 'reason': 'Exact original first-step instruction; estimates are not execution facts.',
                'evidence_refs': evidence})
        except ContractError as error:
            issues.append(gap(error.rule, '/actions/' + action['action_id'], error.reason))
    buys = [row for row in inputs.actions if row['side'] == 'buy']
    if buys:
        if not is_known(inputs.funding['available_amount']) or any(not is_known(row['amount']) for row in buys):
            issues.append(gap('R11', '/funding_plan_ref', 'Original payments or independent current funding amount is unknown.', 'cash_confirmation'))
        elif sum(Fraction(row['amount']['value']) for row in buys) > Fraction(inputs.funding['available_amount']['value']):
            issues.append(gap('R11', '/funding_plan_ref', 'Original subscription payments exceed the independent current plan; future proceeds are excluded.', 'cash_confirmation'))
    return orders, assumptions, issues
