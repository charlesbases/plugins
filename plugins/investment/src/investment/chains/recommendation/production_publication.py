"""从已接收发布材料独立构建必要条件与真实时间交集。"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from investment.contracts import instant, is_known, known, na, unknown
from .production_inputs import gap, parameter_ids, present, stable_id


def raw_time_evidence(raw_time, basis_ref):
    """保留原时区及日期精度；日期上下界描述整日而不是物理到账瞬间。"""
    if not is_known(raw_time):
        return dict(raw_time)
    raw = raw_time['value']
    if raw['kind'] == 'instant':
        stamp = instant(raw['value']).isoformat().replace('+00:00', 'Z')
        return known({'lower_bound': stamp, 'upper_bound': stamp, 'precision': 'instant',
                      'timezone': 'UTC', 'basis_ref': basis_ref})
    zone = ZoneInfo(raw['timezone'])
    lower = datetime.combine(date.fromisoformat(raw['value']), time.min, zone)
    upper = lower + timedelta(days=1) - timedelta(microseconds=1)
    return known({'lower_bound': lower.isoformat(), 'upper_bound': upper.isoformat(),
                  'precision': 'date', 'timezone': raw['timezone'], 'basis_ref': basis_ref})


def build_funding_conditions(inputs):
    actions = [row['action_id'] for row in inputs.actions]
    current = inputs.publication['funding_review_status'] == 'consistent_registered_facts'
    available = inputs.funding['available_amount']
    when = inputs.funding['declared_at']
    availability = raw_time_evidence(known({'kind': 'instant', 'value': when['value']}),
                                     inputs.funding['source_ref']) if is_known(when) else unknown('declared_availability_time_unknown')
    rows = [{
        'condition_id': stable_id('funding-', ['current_plan', inputs.refs['funding_plan_ref']]),
        'source_kind': 'current_plan_confirmation', 'source_trade_id': na('not_a_trade'),
        'action_ids': actions, 'required_amount': present(available['value']) if is_known(available) else dict(available),
        'currency': 'CNY', 'availability': availability,
        'status': 'fulfilled' if current and is_known(available) else 'unknown',
        'verified_funding_plan_ref': present(inputs.refs['funding_plan_ref']) if current else unknown('plan_not_confirmed'),
        'effect': 'confirmed_current_plan' if current else 'wait_update_facts_and_replan',
        'evidence_refs': [inputs.refs['funding_plan_ref'], *inputs.publication['funding_confirmation_refs']],
        'reason': 'Independent declared funding plan; no account balance or sale proceeds are inferred.'}]
    for pending in inputs.pending:
        if pending['side'] != 'sell' or pending['pending_phase'] != 'settlement':
            continue
        rows.append({
            'condition_id': stable_id('funding-', ['expected_settlement', pending['trade_id']]),
            'source_kind': 'expected_settlement', 'source_trade_id': present(pending['trade_id']),
            'action_ids': [], 'required_amount': unknown('receivable_is_not_current_funding'),
            'currency': 'CNY', 'availability': raw_time_evidence(pending['expected_at'], inputs.account['pending_transactions_ref']),
            'status': 'pending', 'verified_funding_plan_ref': unknown('requires_actual_facts_and_new_independent_plan'),
            'effect': 'wait_update_facts_and_replan', 'evidence_refs': [inputs.account['pending_transactions_ref']],
            'reason': 'Expected settlement remains conditional; register actual arrival and update the independent plan.'})
    contributions = inputs.funding['future_contributions']
    if is_known(contributions):
        for contribution in contributions['value']:
            rows.append({
                'condition_id': stable_id('funding-', ['future_contribution', contribution['contribution_id']]),
                'source_kind': 'future_contribution', 'source_trade_id': na('not_a_trade'), 'action_ids': [],
                'required_amount': present(contribution['amount']), 'currency': 'CNY',
                'availability': unknown('contribution_date_has_no_declared_timezone'),
                'status': 'pending', 'verified_funding_plan_ref': unknown('future_contribution_is_not_current_funding'),
                'effect': 'wait_update_facts_and_replan', 'evidence_refs': [contribution['source_ref']],
                'reason': 'Declared future contribution on ' + contribution['contribution_date']
                          + '; its source date has no timezone and does not increase the current planning budget.'})
    return rows


def build_checks(inputs):
    publication = inputs.publication
    checked = inputs.parameters['publication_checked_at']
    rows = []

    def add(domain, baseline, outcome, reason, *, action=None, scope=None, observed=(),
            boundary='event_contingent', start=None, end=None, endpoint='not_applicable', owner='SC09'):
        identifier = stable_id('check-', [domain, action, scope, baseline])
        issues = [] if outcome in ('passed', 'not_applicable') and boundary != 'unknown' else [
            gap('R14', '/publication_checks/' + identifier, reason)]
        rows.append({'check_id': identifier, 'domain': domain, 'scope_object_id': present(scope) if scope else na('global'),
                     'action_id': present(action) if action else na('global'), 'required': outcome != 'not_applicable',
                     'baseline_ref': baseline, 'observed_refs': list(observed), 'checked_at': checked,
                     'outcome': outcome, 'boundary_kind': boundary,
                     'valid_from': present(start) if start else unknown('no_exact_lower_bound'),
                     'valid_until': present(end) if end else (na('event_condition') if boundary == 'event_contingent'
                                                              else unknown('no_exact_upper_bound')),
                     'upper_endpoint': endpoint, 'responsible_stage': owner, 'reason': reason, 'gaps': issues})

    status = {'consistent_registered_facts': 'passed', 'changed': 'changed', 'unknown': 'unknown'}
    account = publication['account_review']
    add('account', inputs.refs['transaction_context_ref'], status[account['status']], account['reason'],
        observed=account['source_refs'], owner='SC01',
        boundary='event_contingent' if account['status'] == 'consistent_registered_facts' else 'unknown')
    funding_status = publication['funding_review_status']
    add('funding', inputs.refs['funding_plan_ref'], status[funding_status],
        'Review covers the received funding declaration; a new funding report invalidates this baseline.',
        observed=publication['funding_confirmation_refs'], owner='task_input',
        boundary='event_contingent' if funding_status == 'consistent_registered_facts' else 'unknown')
    parameters = parameter_ids([inputs.protocol, inputs.objective, inputs.loss, inputs.constraints])
    for identifier in sorted(parameters):
        matching = [row for row in publication['parameter_reviews'] if row['parameter_id'] == identifier]
        review = matching[0] if len(matching) == 1 else None
        outcome = status[review['status']] if review else 'unknown'
        add('user_parameter', inputs.parameters['experiment_protocol_ref'], outcome,
            review['reason'] if review else 'No unique received review for the original parameter.',
            scope=identifier, observed=review['confirmation_refs'] if review else (), owner='task_input',
            boundary='event_contingent' if outcome == 'passed' else 'unknown')
    for action in inputs.actions:
        if action['side'] == 'hold':
            continue
        entity = action['entity_id']['value'] if is_known(action['entity_id']) else None
        observations = [row for row in publication['market_observations'] if row['entity_id'] == entity]
        observation = observations[0] if len(observations) == 1 else None
        evidence = [item['source_ref'] for item in observation['citations']] if observation else []
        contracts = [row for row in inputs.contracts if is_known(action['contract_version_id'])
                     and row['contract_version_id'] == action['contract_version_id']['value']]
        contract = contracts[0] if len(contracts) == 1 else None
        market = [row for row in inputs.market if row['entity_id'] == entity]
        market_ok = (observation is not None and len(market) == 1 and evidence
                     and is_known(observation['reference_nav_version_id']) and is_known(market[0]['nav_version_id']))
        market_outcome = ('passed' if observation['reference_nav_version_id'] == market[0]['nav_version_id'] else 'changed') if market_ok else 'unknown'
        add('market', inputs.bundle['current_market_snapshot_ref'], market_outcome,
            'Reference market vintage must remain the frozen vintage; new market material is not a replacement.',
            action=action['action_id'], scope=entity, observed=evidence, owner='SC05',
            boundary='event_contingent' if market_outcome == 'passed' else 'unknown')
        same = bool(contract and observation and is_known(observation['observed_contract']) and evidence)
        if same:
            received = observation['observed_contract']['value']
            same = all(received[key] == contract[key] for key in ('contract_version_id', 'entity_id', 'platform', 'fees', 'terms'))
            contract_outcome = 'passed' if same else 'changed'
        else:
            contract_outcome = 'unknown'
        start, end, endpoint, boundary = None, None, 'not_applicable', 'event_contingent'
        if contract:
            beginning, ending = contract['effective_from'], contract['effective_to']
            if is_known(beginning) and beginning['value']['precision'] == 'instant':
                start = beginning['value']['lower_bound']
            if is_known(ending):
                # TimeEvidence只提供精度区间，没有合同上端点的包含语义。
                boundary, endpoint = 'unknown', 'unknown'
            elif ending['status'] != 'not_applicable':
                boundary, endpoint = 'unknown', 'unknown'
        else:
            boundary, endpoint = 'unknown', 'unknown'
        add('contract', inputs.bundle['contract_vintages_ref'], contract_outcome,
            'Exact original contract and terms; any finite end requires an explicitly supported endpoint.',
            action=action['action_id'], scope=entity, observed=evidence, start=start, end=end,
            endpoint=endpoint, boundary=boundary, owner='SC05')
        domain = 'subscription_status' if action['side'] == 'buy' else 'redemption_status'
        state = observation[domain] if observation else 'unknown'
        add(domain, inputs.bundle['current_market_snapshot_ref'],
            'passed' if state == 'open' and evidence else ('changed' if state == 'closed' else 'unknown'),
            'Received official status must permit the exact original action; a closure invalidates it.',
            action=action['action_id'], scope=entity, observed=evidence, owner='SC05',
            boundary='event_contingent' if state == 'open' and evidence else 'unknown')
        finite = bool(observation and evidence and all(is_known(observation[key])
                      for key in ('window_start', 'window_end', 'window_end_inclusive')))
        add('submission_window', inputs.bundle['current_market_snapshot_ref'], 'passed' if finite else 'unknown',
            'Submit only inside the source-supported window and its actual upper endpoint.',
            action=action['action_id'], scope=entity, observed=evidence, owner='SC05',
            boundary='finite' if finite else 'unknown',
            start=observation['window_start']['value'] if finite else None,
            end=observation['window_end']['value'] if finite else None,
            endpoint=('inclusive' if observation['window_end_inclusive']['value'] else 'exclusive') if finite else 'unknown')
    qualified = inputs.qualification
    qualification_receipt = inputs.receipts['qualification_ref'][1]
    finite = is_known(qualified['valid_until'])
    add('qualification', inputs.refs['qualification_ref'], 'passed' if finite else 'unknown',
        'SC08 frozen qualification ends exclusively; its receipt does not extend the original bound.',
        observed=[inputs.receipts['qualification_ref'][0]], owner='SC08', boundary='finite' if finite else 'unknown',
        start=qualification_receipt['validated_at'], end=qualified['valid_until']['value'] if finite else None,
        endpoint='exclusive' if finite else 'unknown')
    if inputs.policy['permission_request'] == 'risk_restoration':
        review = inputs.recovery
        good = bool(review and review['verdict'] == 'supported')
        finite = bool(review and review['boundary_kind'] == 'finite'
                      and is_known(review['valid_from']) and is_known(review['valid_until'])
                      and is_known(review['valid_until_inclusive']))
        add('risk_recovery', inputs.policy['constraints_ref'], 'passed' if good else 'unknown',
            review['reason'] if review else 'No original independently authorized risk recovery extraction.',
            observed=review['authorization_refs'] if review else (), owner='SC08',
            boundary='finite' if finite else ('event_contingent' if good and review['boundary_kind'] == 'event_contingent' else 'unknown'),
            start=review['valid_from']['value'] if finite else None,
            end=review['valid_until']['value'] if finite else None,
            endpoint=('inclusive' if review['valid_until_inclusive']['value'] else 'exclusive') if finite else 'unknown')
    return rows


def intersect_bounds(checks, checked_at):
    required = [row for row in checks if row['required']]
    events = sorted(row['check_id'] for row in required if row['boundary_kind'] == 'event_contingent')
    if any(row['outcome'] == 'changed' for row in required):
        return unknown('received_material_changed'), 'unknown', [], events, 'changed'
    if any(row['outcome'] == 'unknown' or row['boundary_kind'] == 'unknown' for row in required):
        return unknown('necessary_publication_condition_unknown'), 'unknown', [], events, 'insufficient'
    finite = [row for row in required if row['boundary_kind'] == 'finite']
    if not finite:
        return unknown('no_source_supported_finite_bound'), 'unknown', [], events, 'insufficient'
    bound = min(instant(row['valid_until']['value']) for row in finite)
    controlling = [row for row in finite if instant(row['valid_until']['value']) == bound]
    endpoint = 'exclusive' if any(row['upper_endpoint'] == 'exclusive' for row in controlling) else 'inclusive'
    latest_start = max(instant(row['valid_from']['value']) for row in finite)
    stamp = next(row['valid_until']['value'] for row in controlling)
    empty = latest_start > bound or (latest_start == bound and endpoint == 'exclusive')
    expired = instant(checked_at) > bound or (instant(checked_at) == bound and endpoint == 'exclusive')
    premature = instant(checked_at) < latest_start
    status = 'historical_only' if empty or expired else ('insufficient' if premature else 'eligible')
    return present(stamp), endpoint, sorted(row['check_id'] for row in controlling), events, status
