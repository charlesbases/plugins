"""独立核原动作、范围、批次及诚实表达。"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from investment.contracts import canonical, instant, is_known
from zoneinfo import ZoneInfo
from .contracts import ensure, value, unique, parameter_ids, gaps


def number(text):
    return text if isinstance(text, Fraction) else Fraction(Decimal(text))


def exact_instruction(action):
    field = 'amount' if action['side'] == 'buy' else 'shares'
    text = value(action[field], 'Original ' + field)
    ensure(number(text) > 0, 'Original execution instruction must be strictly positive')
    return text


def identities(s):
    ctx, p, pub = s.ctx, s.payload, s.publication
    ensure([row['role'] for row in ctx.input['input_refs']] == [row['role'] for row in ctx.definition['input_roles']], 'Input role order differs')
    ensure(all(s.parameters[role] == ref for role, ref in s.refs.items()), 'Parameters differ from exact input role references')
    ensure(p['decision_at'] == ctx.input['decision_at'], 'Decision time differs from frozen StageInput')
    checked = p['publication_checked_at']
    ensure(checked == s.parameters['publication_checked_at'] == pub['reviewed_at'], 'Publication review clocks differ')
    ensure(instant(checked) <= instant(ctx.input['decision_at']) <= instant(ctx.candidate['created_at']), 'Publication/candidate time order differs')
    times = [pub['account_review']['checked_at'], s.funding['recorded_at']]
    times += [r['checked_at'] for r in pub['parameter_reviews'] + pub['market_observations']]
    times += [r['completed_at'] for r in pub['fetch_attempts']]
    ensure(all(instant(t) <= instant(checked) for t in times), 'A review or retrieval completed after publication_checked_at')


def bindings(s):
    p, q, policy = s.payload, s.qualification, s.policy
    for field, role in [('account_snapshot_ref', 'transaction_context_ref'), ('policy_ref', 'frozen_policy_ref'),
                        ('qualification_ref', 'qualification_ref'), ('publication_context_ref', 'publication_context_ref')]:
        ensure(p[field] == s.refs[role], 'Recommendation exact reference differs: ' + field)
    ensure(p['market_snapshot_ref'] == s.bundle['current_market_snapshot_ref'], 'Market snapshot differs')
    ensure(q['verified_policy_ref'] == s.refs['frozen_policy_ref'] and q['verified_actions_ref'] == policy['current_actions_ref']
           and q['verified_selected_rule_ref'] == policy['selected_rule_ref'], 'Qualification policy/actions/selected rule binding differs')
    ensure(policy['account_snapshot_ref'] == s.refs['transaction_context_ref'] == s.space['account_snapshot_ref']
           == s.objective['account_snapshot_ref'], 'Account version differs')
    ensure(s.ctx.input['scope_ref'] == s.parameters['experiment_protocol_ref'] == policy['experiment_protocol_ref']
           == s.bundle['experiment_protocol_ref'] == q['protocol_ref'] == s.constraints['protocol_ref'], 'Protocol version differs')
    ensure(s.space['funding_plan_ref'] == s.refs['funding_plan_ref'] == s.objective['funding_plan_ref'], 'FundingPlan differs')
    ensure(all('reporting' in receipt['allowed_uses'] for _, receipt in s.receipts.values()), 'Bound upstream is not readable for reporting')


def account_review(s):
    review = s.publication['account_review']
    ensure(review['account_snapshot_ref'] == s.refs['transaction_context_ref'], 'Account review uses another account version')
    ensure(review['source_kind'] == s.account['source_authority'] or review['source_kind'] == 'unknown', 'Account source authority changed')
    ensure(review['source_kind'] != 'synthetic_fixture' or s.ctx.mode == 'test', 'Synthetic account review outside test namespace')
    ensure(review['status'] != 'changed', 'Actually received account change invalidates old advice')
    ensure(review['status'] == 'consistent_registered_facts' and review['source_refs'], 'Registered account review source is unavailable', missing=True)
    for ref in review['source_refs']:
        s.ctx.resolver.resolve(ref)


def parameters(s):
    pub = s.publication
    ensure(pub['funding_plan_ref'] == s.refs['funding_plan_ref'], 'Publication funding baseline differs')
    ensure(pub['funding_review_status'] != 'changed', 'Actually received funding change invalidates old advice')
    ensure(pub['funding_review_status'] == 'consistent_registered_facts' and pub['funding_confirmation_refs'], 'Funding confirmation source missing', missing=True)
    value(s.funding['available_amount'], 'Independently declared available planning funds')
    ids = parameter_ids(s.objective) | parameter_ids(s.loss) | parameter_ids(s.constraints)
    specs = unique(s.protocol['parameter_specs'], 'parameter_id')
    reviews = unique(pub['parameter_reviews'], 'parameter_id')
    ensure(ids <= set(specs), 'A dependent parameter is absent from frozen protocol')
    for pid in sorted(ids):
        value(specs[pid]['value'], 'Frozen parameter ' + pid)
        if specs[pid]['source_kind'] != 'user_confirmation':
            continue
        ensure(pid in reviews, 'Missing publication review for user parameter ' + pid, missing=True)
        row = reviews[pid]
        ensure(row['baseline_protocol_ref'] == s.parameters['experiment_protocol_ref'], 'Parameter baseline protocol differs')
        ensure(row['status'] != 'changed', 'Actually received parameter change: ' + pid)
        ensure(row['status'] == 'consistent_registered_facts' and row['confirmation_refs'], 'Parameter confirmation source missing: ' + pid, missing=True)


def mappings(s):
    actions = unique(s.actions, 'action_id')
    mappings = unique(s.tables['action_mappings'], 'source_action_id')
    orders = unique(s.tables['advisory_orders'], 'source_action_id')
    ensure(set(mappings) == set(actions), 'Full current_actions must each have exactly one mapping')
    ensure(set(orders) <= set(actions), 'Order refers to an absent action')
    executable = s.payload['decision_kind'] in ('rebalance', 'risk_recovery')
    expected = {aid for aid, action in actions.items() if action['side'] != 'hold'} if executable else set()
    ensure(set(orders) == expected, 'Partial policy publication or an order in a non-executing decision')
    unique(s.tables['advisory_orders'], 'advisory_order_id')
    for aid, action in actions.items():
        row = mappings[aid]
        ensure(row['source_actions_ref'] == s.policy['current_actions_ref'], 'Mapping source actions differ')
        if aid in orders:
            order = orders[aid]
            ensure(row['mapping_status'] == 'mapped' and value(row['advisory_order_id'], 'Mapped order id') == order['advisory_order_id'], 'Mapping does not identify exact order')
            ensure(order['side'] == action['side'] and order['instrument_id'] == value(action['entity_id'], 'Original action entity')
                   and order['platform'] == s.account['platform'] and order['currency'] == s.account['currency'], 'Order identity/side differs')
            ensure(order['contract_version_id'] == value(action['contract_version_id'], 'Original contract')
                   and order['fee_tier_ids'] == action['fee_tier_ids'], 'Order contract/fee tiers differ')
        else:
            ensure(row['advisory_order_id']['status'] == 'not_applicable', 'Nonexecuted mapping claims an order')
            ensure(row['mapping_status'] == ('hold' if action['side'] == 'hold' else 'observation')
                   or s.payload['decision_kind'] == 'observation_only' and row['mapping_status'] == 'blocked', 'Mapping disposition differs from actual action')


def quantities(s):
    actions = unique(s.actions, 'action_id')
    for order in s.tables['advisory_orders']:
        action = actions[order['source_action_id']]
        original = exact_instruction(action)
        field, other = ('subscription_cash_amount', 'redemption_shares') if action['side'] == 'buy' else ('redemption_shares', 'subscription_cash_amount')
        ensure(value(order[field], 'Order quantity') == original, 'Instruction value or original decimal precision changed')
        ensure(order[other]['status'] == 'not_applicable', 'Opposite instruction direction must be not_applicable')
    if s.payload['decision_kind'] == 'no_action':
        ensure(all(action['side'] == 'hold' for action in s.actions), 'Unknown or missing action is not no_action')


def scenario_lots(rows, action_id):
    scenarios = {row['scenario_id'] for row in rows}
    amounts = {sid: defaultdict(Fraction) for sid in scenarios}
    for row in rows:
        if is_known(row['action_id']) and row['action_id']['value'] == action_id and row['event'] == 'redemption_confirmation':
            trade = value(row['trade_id'], 'Frozen redemption source lot')
            delta = number(value(row['share_delta'], 'Frozen redemption share delta'))
            ensure(delta < 0, 'Redemption confirmation must reduce source-lot shares')
            amounts[row['scenario_id']][trade] -= delta
    ensure(scenarios and all(amounts.values()), 'Current redemption has no complete per-scenario lot allocation', missing=True)
    unique_values = {tuple(sorted(x.items())) for x in amounts.values()}
    ensure(len(unique_values) == 1, 'Current first-step lot allocation differs across scenarios')
    return dict(next(iter(unique_values)))


def lots(s):
    trades = unique(s.trades, 'trade_id')
    positions = unique(s.positions, 'instrument_id')
    actions = unique(s.actions, 'action_id')
    for order in s.tables['advisory_orders']:
        if order['side'] != 'sell':
            ensure(order['lot_allocations']['status'] == 'not_applicable', 'Subscription cannot assert redemption lot allocation')
            continue
        action = actions[order['source_action_id']]
        expected = scenario_lots(s.states, action['action_id'])
        allocated = unique(value(order['lot_allocations'], 'Order lot allocations'), 'trade_id')
        ensure(set(allocated) == set(expected) == set(action['lot_trade_ids']), 'Order source-lot set differs from frozen first step')
        ensure(sum(expected.values()) == number(exact_instruction(action)), 'Frozen lot shares do not conserve original redemption')
        ensure(s.selected is not None, 'Selected allocation rule unavailable', missing=True)
        dimensions = [r for r in s.selected['allocation_dimensions'] if r['side'] == 'sell' and is_known(r['entity_id']) and r['entity_id']['value'] == order['instrument_id']]
        ensure(len(dimensions) == 1, 'Source allocation dimension is not uniquely established', missing=True)
        dim = dimensions[0]
        if dim['lot_rule'] == 'specific_confirmed_trades':
            ensure(set(expected) <= set(dim['lot_trade_ids']), 'Allocation uses an unauthorized source lot')
        grid = next((r for r in s.space['grid_rules'] if r['entity_id'] == order['instrument_id'] and r['contract_version_id'] == order['contract_version_id']), None)
        ensure(grid is not None, 'Original exact grid contract unavailable', missing=True)
        ensure(not set(expected) & set(grid['locked_trade_ids']), 'Frozen allocation includes a locked lot')
        available = number(value(positions[order['instrument_id']]['confirmed_shares'], 'Registered confirmed position'))
        pending = sum((number(value(t['requested_shares'], 'Pending redemption requested shares')) for t in s.trades
                       if t['instrument_id'] == order['instrument_id'] and t['side'] == 'sell' and t['confirmation']['state'] == 'pending'), Fraction())
        ensure(sum(expected.values()) + pending <= available, 'Redemption exceeds registered unlocked aggregate shares')
        baseline = {}
        prior = [t for t in s.trades if t['instrument_id'] == order['instrument_id'] and t['side'] == 'sell' and t['confirmation']['state'] == 'confirmed']
        eligible = [t for t in s.trades if t['instrument_id'] == order['instrument_id'] and t['side'] == 'buy'
                    and t['confirmation']['state'] == 'confirmed' and t['trade_id'] not in grid['locked_trade_ids']]
        for trade in eligible:
            tid = trade['trade_id']
            available_lot = number(value(trade['confirmed_shares'], 'Confirmed actual purchase shares'))
            if prior:
                initial = [r for r in s.states if r['event'] == 'initial' and is_known(r['trade_id']) and r['trade_id']['value'] == tid]
                balances = {number(value(r['share_delta'], 'Frozen initial source-lot balance')) for r in initial}
                ensure({r['scenario_id'] for r in initial} == {r['scenario_id'] for r in s.states}
                       and len(balances) == 1, 'Historical source-lot remainder is not uniquely established in every scenario', missing=True)
                available_lot = next(iter(balances))
                ensure(0 <= available_lot <= number(value(trade['confirmed_shares'], 'Original purchase shares')),
                       'Frozen initial source-lot remainder exceeds original purchase')
            baseline[tid] = available_lot
        if dim['lot_rule'] == 'fifo':
            groups = defaultdict(list)
            dated_starts = {}
            for trade in eligible:
                raw_start = value(trade['holding_start'], 'FIFO source-lot holding start')
                day = (instant(raw_start['value']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                       if raw_start['kind'] == 'instant' else raw_start['value'])
                dated_starts.setdefault(day, []).append(raw_start)
            for trade in eligible:
                start = value(trade['holding_start'], 'FIFO source-lot holding start')
                if start['kind'] == 'instant':
                    point = instant(start['value']).astimezone(ZoneInfo('Asia/Shanghai'))
                    day = point.date().isoformat()
                    key = (day, point.isoformat() if all(start['kind'] == 'instant' for start in dated_starts[day]) else '')
                else:
                    key = (start['value'], '')
                groups[key].append(trade['trade_id'])
            # 同一来源日期无法区分的批次由准确冻结 StateRow 决定；可证更早的日期/时刻不得跳过。
            keys = sorted(groups)
            for index, key in enumerate(keys):
                if any(expected.get(tid, 0) for later in keys[index + 1:] for tid in groups[later]):
                    ensure(sum((expected.get(tid, 0) for tid in groups[key]), Fraction())
                           == sum((baseline[tid] for tid in groups[key]), Fraction()), 'Frozen lot allocation contradicts original FIFO holding order')
        for tid, amount in expected.items():
            ensure(tid in baseline and amount <= baseline[tid], 'Current redemption exceeds its proven source-lot remainder')
            ensure(tid in trades and trades[tid]['side'] == 'buy' and trades[tid]['confirmation']['state'] == 'confirmed', 'Source lot is not an actual confirmed purchase')
            trade, allocation = trades[tid], allocated[tid]
            ensure(amount <= number(value(trade['confirmed_shares'], 'Confirmed source-lot shares')), 'Source lot exceeds actual confirmed purchase shares')
            ensure(number(value(allocation['shares'], 'Allocated shares')) == amount, 'Allocated lot quantity differs')
            ensure(allocation['allocation_rule_ref'] == s.policy['selected_rule_ref']['value'], 'Lot allocation rule reference differs')


def dispositions(s):
    rows = s.tables['position_dispositions']
    unique(rows, 'disposition_id')
    expected = {('declared_member', r['member_id']): (s.universe['members_ref'], r['member_id'], r['entity_id']) for r in s.members}
    expected.update({('registered_position', r['instrument_id']): (s.account['positions_ref'], r['instrument_id'],
                     {'status': 'known', 'value': r['instrument_id'], 'reason': None}) for r in s.positions})
    actual = {(r['object_kind'], r['scope_object_id']): r for r in rows}
    ensure(len(actual) == len(rows) and set(actual) == set(expected), 'Disposition scope differs from all original members and registered positions')
    action_ids = {r['action_id'] for r in s.actions}
    upstream = {r['member_id']: r for r in s.member_dispositions}
    for key, (ref, row_id, entity) in expected.items():
        row = actual[key]
        ensure(row['source_object_ref'] == ref and row['source_row_id'] == row_id and row['instrument_id'] == entity, 'Disposition source row/entity differs')
        ensure(set(row['action_ids']) <= action_ids, 'Disposition references an absent current action')
        if entity['status'] == 'unknown':
            ensure(row['disposition'] in ('unknown', 'observation'), 'Unresolved original member was silently classified')
        if key[0] == 'declared_member' and key[1] in upstream and upstream[key[1]]['comparison_status'] == 'excluded':
            ensure(row['disposition'] == 'excluded', 'Original excluded member disposition changed')


def honest(s):
    current = {canonical(gap) for gap in gaps(s.payload) + [g for rows in s.tables.values() for g in gaps(rows)]}
    ensure(set(s.inherited) <= current, 'Inherited blockers or input gaps were hidden')
    ensure(s.payload['risk_recovery_review_ref'] == s.parameters['risk_recovery_review_ref'], 'Recovery review binding changed')
    if s.payload['decision_kind'] == 'observation_only':
        ensure(not s.tables['advisory_orders'] and s.payload['permission_request'] == 'observation_only', 'Observation output asserts executable advice')
    ensure(s.funding['source_type'] != 'synthetic_fixture' or s.ctx.mode == 'test', 'Synthetic funding outside isolated test run')


def exports(s):
    refs = [s.payload[name + '_ref'] for name in s.tables]
    ensure(len(s.ctx.candidate['datasets']) == len(refs) and {canonical(r) for r in s.ctx.candidate['datasets']} == {canonical(r) for r in refs}, 'Candidate does not export exactly the five payload table references')
    for name, rows in s.tables.items():
        # Resolver 已逐项核 Schema、主键、units、null_policy 和 hash，不构造简化表引用。
        s.ctx.resolver.resolve(s.payload[name + '_ref'], 'investment.sc09.' + name)
    ensure(s.payload['published_at']['status'] == 'unknown', 'Candidate claims actual publication before SC10 delivery')
