"""对本批真实上游执行原四selector；缺输入时如实记录未执行。"""

import argparse
from pathlib import Path
from uuid import uuid4

from investment.contracts import canonical, is_known, require
from investment.research.contracts import guard_namespace
from investment.research.controller import ResearchController
from investment.runtime.paths import validate_input_path


SELECTORS = ('exact_orders', 'no_action', 'observation_only', 'publication_change')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--selector', choices=SELECTORS, required=True)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--run-id')
    parser.add_argument('--invocation-id')
    args = parser.parse_args(argv)
    root = guard_namespace(args.root, 'test')
    report = {'selector': args.selector, 'execution_state': 'not_executed',
              'basis': 'Original exact actions, complete scope, received publication material, and independent arithmetic.',
              'reason': '', 'run_result': None, 'assertions': []}
    if args.input is None:
        report['reason'] = ('No current-batch SC09 StageInput was supplied by the upstream input builder. '
                            'Real sealed SC01/SC05/SC07/SC08 and the received PublicationContext are required.')
        print(canonical(report).decode('utf-8'))
        return 3
    require(args.run_id and args.invocation_id, 'RS03', '/invocation_id', 'A real run and invocation identity are required')
    submitted = validate_input_path(root, args.input)
    controller = ResearchController(root, mode='test', stage_id='SC09')
    # 使用公共Schema/引用基础，不调用流程或validator计算独立预期。
    from investment.research.contracts import ResearchSchemas, STAGES
    from investment.research.references import ResearchResolver
    schemas = ResearchSchemas(STAGES[:7])
    resolver = ResearchResolver(root, schemas, mode='test')
    submitted_ref = resolver.path_ref(submitted, 'investment.stage_input')
    incoming = resolver.resolve(submitted_ref, 'investment.stage_input')
    refs = {row['role']: row['ref'] for row in incoming['input_refs']}
    account = resolver.resolve(refs['transaction_context_ref'])
    policy = resolver.resolve(refs['frozen_policy_ref'])
    qualification = resolver.resolve(refs['qualification_ref'])
    publication = resolver.resolve(refs['publication_context_ref'])
    bundle = resolver.resolve(refs['dataset_bundle_ref'])
    market = resolver.resolve(bundle['current_market_snapshot_ref'])['rows']
    contracts = resolver.resolve(bundle['contract_vintages_ref'])['rows']
    actions = resolver.resolve(policy['current_actions_ref'])['rows']
    trades = resolver.resolve(account['trades_ref'])['rows']
    positions = resolver.resolve(account['positions_ref'])['rows']
    universe = resolver.resolve(policy['universe_ref'])
    members = resolver.resolve(universe['members_ref'])['rows']
    active = [row for row in actions if row['side'] != 'hold']
    if args.selector == 'exact_orders':
        require(active, 'R05', '/current_actions_ref', 'exact_orders requires actual non-hold first-step actions')
        for action in active:
            if action['side'] == 'sell':
                require(any(row['side'] == 'buy' and row['confirmation']['state'] == 'confirmed'
                            and is_known(action['entity_id']) and row['instrument_id'] == action['entity_id']['value']
                            for row in trades), 'R07', '/trades', 'A sell selector requires an actual confirmed prior purchase')
    if args.selector == 'no_action':
        require(not active and qualification['verdict'] == 'qualified'
                and qualification['permission_scope'] == 'complete_declared_scope',
                'R18', '/current_actions_ref', 'no_action requires complete qualified comparison and actual all-hold or lawful empty actions')
    changed = (publication['account_review']['status'] == 'changed' or publication['funding_review_status'] == 'changed'
               or any(row['status'] == 'changed' for row in publication['parameter_reviews'])
               or any(row['subscription_status'] == 'closed' or row['redemption_status'] == 'closed'
                      for row in publication['market_observations']))
    for observation in publication['market_observations']:
        original = [row for row in market if row['entity_id'] == observation['entity_id']]
        if len(original) == 1 and is_known(original[0]['nav_version_id']) and is_known(observation['reference_nav_version_id']):
            changed = changed or original[0]['nav_version_id'] != observation['reference_nav_version_id']
        if is_known(observation['observed_contract']):
            received = observation['observed_contract']['value']
            baseline = observation['baseline_contract_version_id']
            if is_known(baseline):
                original_contracts = [row for row in contracts if row['contract_version_id'] == baseline['value']]
                changed = changed or received['contract_version_id'] != baseline['value']
                if len(original_contracts) == 1:
                    changed = changed or any(received[key] != original_contracts[0][key]
                                             for key in ('platform', 'fees', 'terms'))
    if args.selector == 'publication_change':
        require(changed, 'R14', '/publication_context_ref', 'publication_change requires actually received changed material')
    if args.selector == 'observation_only':
        active_entities = {row['entity_id']['value'] for row in active if is_known(row['entity_id'])}
        active_observations = [row for row in publication['market_observations'] if row['entity_id'] in active_entities]
        require(qualification['verdict'] != 'qualified' or changed
                or publication['account_review']['status'] == 'unknown'
                or publication['funding_review_status'] == 'unknown' or publication['gaps']
                or any(row['status'] == 'unknown' for row in publication['parameter_reviews'])
                or any(row['gaps'] or any(not is_known(row[field]) for field in
                                          ('window_start', 'window_end', 'window_end_inclusive'))
                       for row in active_observations)
                or any(not row['citations'] or row['subscription_status'] == 'unknown'
                       or row['redemption_status'] == 'unknown' for row in publication['market_observations']),
                'R18', '/qualification_ref', 'observation_only requires a real insufficient or changed input')
    _, parts = resolver.run_parts(submitted_ref['uri'])
    manifest_path = root / 'runs' / 'sc09' / parts[0] / 'manifest.json'
    before = schemas.document('investment.research_daily_manifest', manifest_path.read_bytes()) if manifest_path.exists() else {'invocations': []}
    previous = next((item for item in before['invocations'] if item['invocation_id'] == args.invocation_id), None)
    if previous and previous['state'] in ('completed', 'failed'):
        report['reason'] = 'Invocation was already terminal before this command; no new selector execution is claimed.'
        print(canonical(report).decode('utf-8'))
        return 3
    result = controller.run(submitted, run_id=args.run_id, invocation_id=args.invocation_id)
    report['run_result'] = result
    if result['status'] == 'host_work_required':
        report.update(execution_state='pending', reason='Actual HostRequest requires the main Codex host to execute the frozen prompt in a fresh context.')
        code = 3
    elif result['status'] == 'failed':
        report.update(execution_state='failed', reason='Original controller exhausted its actual attempt budget; inspect failure_refs.')
        code = 1
    else:
        seal = controller.resolver.resolve(result['sealed_output_ref']['value'])
        candidate = controller.resolver.resolve(seal['candidate_output_ref'])
        payload = controller.resolver.resolve(candidate['payload_ref'])
        receipt = controller.resolver.resolve(seal['validation_receipt_ref'])
        orders = controller.resolver.resolve(payload['advisory_orders_ref'])['rows']
        mappings = controller.resolver.resolve(payload['action_mappings_ref'])['rows']
        dispositions = controller.resolver.resolve(payload['position_dispositions_ref'])['rows']
        expected_dispositions = [('declared_member', row['member_id'], canonical(universe['members_ref'])) for row in members]
        expected_dispositions.extend(('registered_position', row['instrument_id'], canonical(account['positions_ref'])) for row in positions)
        actual_dispositions = [(row['object_kind'], row['scope_object_id'], canonical(row['source_object_ref'])) for row in dispositions]
        assertions = [
            ('complete_original_actions', sorted(row['source_action_id'] for row in mappings) == sorted(row['action_id'] for row in actions)),
            ('complete_declared_members_and_positions', sorted(actual_dispositions) == sorted(expected_dispositions))]
        if args.selector == 'exact_orders':
            expected = {(row['action_id'], row['side'], row['amount']['value'] if row['side'] == 'buy' else row['shares']['value'])
                        for row in active}
            actual = {(row['source_action_id'], row['side'],
                       row['subscription_cash_amount']['value'] if row['side'] == 'buy' else row['redemption_shares']['value'])
                      for row in orders}
            assertions.extend([('exact_original_instruction_text', actual == expected),
                               ('decision_advice_independently_permitted', 'decision_advice' in receipt['allowed_uses'])])
        elif args.selector == 'no_action':
            assertions.append(('qualified_no_action_without_zero_orders', payload['decision_kind'] == 'no_action' and not orders
                               and 'decision_advice' in receipt['allowed_uses']))
        else:
            assertions.append(('observation_preserves_scope_without_orders',
                               payload['decision_kind'] == 'observation_only' and not orders and bool(payload['gaps'])))
            if args.selector == 'publication_change':
                assertions.append(('actual_change_blocks_advice', payload['publication_status'] == 'changed'
                                   and 'decision_advice' not in receipt['allowed_uses']))
        report['assertions'] = [{'name': name, 'passed': passed} for name, passed in assertions]
        success = all(passed for _, passed in assertions)
        report.update(execution_state='passed' if success else 'failed',
                      reason='Newly completed real process and independent validator outputs were checked against original inputs.')
        code = 0 if success else 1
    directory = root / 'verification'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ('sc09-' + args.selector + '-' + uuid4().hex + '.json')).write_bytes(canonical(report))
    print(canonical(report).decode('utf-8'))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
