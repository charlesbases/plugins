"""本轮SC09生产输入与目标类型的精确投影。"""

from dataclasses import dataclass
from hashlib import sha256

from investment.contracts import canonical, is_known, require


def present(value):
    """SC09的known分支要求reason为null。"""
    return {'status': 'known', 'value': value, 'reason': None}


def gap(rule, path, reason, evidence='source_bytes'):
    return {'rule_id': rule, 'field_or_object': path, 'reason': reason, 'owner': 'producer',
            'required_evidence': [evidence], 'affected_uses': ['decision_advice']}


def stable_id(prefix, parts):
    return prefix + sha256(canonical(parts)).hexdigest()


def parameter_ids(value):
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get('parameter_id'), str):
            found.add(value['parameter_id'])
        elif is_known(value.get('parameter_id')):
            found.add(value['parameter_id']['value'])
        for child in value.values():
            found.update(parameter_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(parameter_ids(child))
    return found


def inherited_gaps(value):
    found = []
    if isinstance(value, dict):
        if isinstance(value.get('gaps'), list):
            found.extend(value['gaps'])
        for key, child in value.items():
            if key != 'gaps':
                found.extend(inherited_gaps(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(inherited_gaps(child))
    return found


@dataclass
class ProducerInputs:
    ctx: object
    refs: dict
    parameters: dict
    roles: dict
    receipts: dict
    account: dict
    funding: dict
    bundle: dict
    policy: dict
    qualification: dict
    publication: dict
    trades: list
    positions: list
    pending: list
    actions: list
    states: list
    protocol: dict
    objective: dict
    loss: dict
    constraints: dict
    space: dict
    selected: dict | None
    universe: dict
    members: list
    inventory: dict
    member_dispositions: list
    market: list
    contracts: list
    recovery: dict | None
    gaps: list

def load_inputs(ctx):
    require(ctx.role == 'producer', 'R02', '/role', 'SC09 production requires the producer role')
    refs = {row['role']: row['ref'] for row in ctx.input['input_refs']}
    expected = ctx.definition['input_roles']
    require([row['role'] for row in ctx.input['input_refs']] == [row['role'] for row in expected],
            'R02', '/input_refs', 'SC09 requires the original six ordered input roles')
    parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], 'investment.sc09.parameters')
    require(all(parameters[role] == reference for role, reference in refs.items()),
            'R02', '/parameters_ref', 'Parameters differ from the exact role references')
    roles = {row['role']: ctx.read_role(row['role'], row['schema_id']) for row in expected}
    account, funding, bundle, policy, qualification, publication = [roles[row['role']] for row in expected]
    receipts = {row['role']: ctx.upstream_receipt(row['role']) for row in expected
                if row['source']['kind'] == 'upstream_output'}
    require(policy['account_snapshot_ref'] == refs['transaction_context_ref']
            and qualification['verified_policy_ref'] == refs['frozen_policy_ref']
            and qualification['verified_actions_ref'] == policy['current_actions_ref']
            and qualification['verified_selected_rule_ref'] == policy['selected_rule_ref']
            and publication['account_review']['account_snapshot_ref'] == refs['transaction_context_ref']
            and publication['funding_plan_ref'] == refs['funding_plan_ref'],
            'R02', '/input_refs', 'Account, actions, selected rule, or publication baseline differ')
    protocol_ref = parameters['experiment_protocol_ref']
    require(all(obj[key] == protocol_ref for obj, key in
                ((bundle, 'experiment_protocol_ref'), (policy, 'experiment_protocol_ref'),
                 (qualification, 'protocol_ref'))),
            'R02', '/experiment_protocol_ref', 'Input protocols differ')
    require(parameters['publication_checked_at'] == publication['reviewed_at'],
            'R14', '/publication_checked_at', 'Publication review clock differs')
    resolve = ctx.resolver.resolve
    rows = lambda reference: resolve(reference)['rows']
    actions = rows(policy['current_actions_ref'])
    needed = {row['contract_version_id']['value'] for row in actions if is_known(row['contract_version_id'])}
    contracts = [row for row in rows(bundle['contract_vintages_ref']) if row['contract_version_id'] in needed]
    inventory = resolve(policy['comparison_inventory_ref'])
    universe = resolve(policy['universe_ref'])
    inherited = [item for role in roles.values() for item in inherited_gaps(role)]
    inherited.extend(item for _, receipt in receipts.values() for item in receipt['blockers'])
    return ProducerInputs(
        ctx, refs, parameters, roles, receipts, account, funding, bundle, policy, qualification, publication,
        rows(account['trades_ref']), rows(account['positions_ref']), rows(account['pending_transactions_ref']),
        actions, rows(policy['cash_schedule_ref']), resolve(protocol_ref), resolve(policy['objective_ref']),
        resolve(policy['loss_definition_ref']), resolve(policy['constraints_ref']), resolve(policy['action_space_ref']),
        resolve(policy['selected_rule_ref']['value']) if is_known(policy['selected_rule_ref']) else None,
        universe, rows(universe['members_ref']), inventory, rows(inventory['member_dispositions_ref']),
        rows(bundle['current_market_snapshot_ref']), contracts,
        resolve(parameters['risk_recovery_review_ref']['value']) if is_known(parameters['risk_recovery_review_ref']) else None,
        list({canonical(item): item for item in inherited}.values()))
