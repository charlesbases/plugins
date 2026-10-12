"""准确候选、原输入及受限引用的只读快照。"""
from __future__ import annotations

from investment.contracts import canonical, is_known


class Issue(Exception):
    """独立规则的已证错误或明确证据缺口。"""

    def __init__(self, reason, verdict='failed', path='/recommendation'):
        super().__init__(reason)
        self.verdict, self.path = verdict, path


def ensure(condition, reason, *, missing=False, path='/recommendation'):
    if not condition:
        raise Issue(reason, 'insufficient_evidence' if missing else 'failed', path)


def value(optional, description):
    ensure(is_known(optional), description + ' is not known', missing=True)
    return optional['value']


def unique(rows, key):
    result = {row[key]: row for row in rows}
    ensure(len(result) == len(rows), 'Duplicate ' + key)
    return result


def parameter_ids(obj):
    result = set()
    if isinstance(obj, dict):
        for key, item in obj.items():
            if key.endswith('parameter_id'):
                if isinstance(item, str):
                    result.add(item)
                elif is_known(item):
                    result.add(item['value'])
            else:
                result.update(parameter_ids(item))
    elif isinstance(obj, list):
        for item in obj:
            result.update(parameter_ids(item))
    return result


def gaps(obj):
    result = []
    if isinstance(obj, dict):
        if set(obj) == {'rule_id', 'field_or_object', 'reason', 'owner', 'required_evidence', 'affected_uses'}:
            result.append(obj)
        else:
            for item in obj.values():
                result.extend(gaps(item))
    elif isinstance(obj, list):
        for item in obj:
            result.extend(gaps(item))
    return result


class Snapshot:
    """不读取 latest、DataView 或 producer 私有材料。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.refs = {row['role']: row['ref'] for row in ctx.input['input_refs']}
        self.parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], 'investment.sc09.parameters')
        self.roles = {row['role']: ctx.read_role(row['role'], row['schema_id'])
                      for row in ctx.definition['input_roles']}
        self.payload = ctx.resolver.resolve(ctx.candidate['payload_ref'], 'investment.sc09.recommendation_set')
        self.tables = {name: ctx.resolver.resolve(self.payload[name + '_ref'], 'investment.sc09.' + name)['rows']
                       for name in ('advisory_orders', 'action_mappings', 'position_dispositions',
                                    'funding_conditions', 'publication_checks')}
        self.account = self.roles['transaction_context_ref']
        self.funding = self.roles['funding_plan_ref']
        self.bundle = self.roles['dataset_bundle_ref']
        self.policy = self.roles['frozen_policy_ref']
        self.qualification = self.roles['qualification_ref']
        self.publication = self.roles['publication_context_ref']
        self.receipts = {row['role']: ctx.upstream_receipt(row['role']) for row in ctx.definition['input_roles']
                         if row['source']['kind'] == 'upstream_output'}
        self.trades = self.rows(self.account['trades_ref'])
        self.positions = self.rows(self.account['positions_ref'])
        self.pending = self.rows(self.account['pending_transactions_ref'])
        self.actions = self.rows(self.policy['current_actions_ref'])
        self.states = self.rows(self.policy['cash_schedule_ref'])
        self.protocol = ctx.resolver.resolve(self.parameters['experiment_protocol_ref'])
        self.objective = ctx.resolver.resolve(self.policy['objective_ref'])
        self.loss = ctx.resolver.resolve(self.policy['loss_definition_ref'])
        self.constraints = ctx.resolver.resolve(self.policy['constraints_ref'])
        self.space = ctx.resolver.resolve(self.policy['action_space_ref'])
        self.selected = ctx.resolver.resolve(self.policy['selected_rule_ref']['value']) if is_known(self.policy['selected_rule_ref']) else None
        self.universe = ctx.resolver.resolve(self.policy['universe_ref'])
        self.members = self.rows(self.universe['members_ref'])
        self.inventory = ctx.resolver.resolve(self.policy['comparison_inventory_ref'])
        self.member_dispositions = self.rows(self.inventory['member_dispositions_ref'])
        self.market = self.rows(self.bundle['current_market_snapshot_ref'])
        needed_contracts = {a['contract_version_id']['value'] for a in self.actions if is_known(a['contract_version_id'])}
        self.contracts = [r for r in self.rows(self.bundle['contract_vintages_ref']) if r['contract_version_id'] in needed_contracts]
        self.recovery = ctx.resolver.resolve(self.parameters['risk_recovery_review_ref']['value']) if is_known(self.parameters['risk_recovery_review_ref']) else None
        inherited = [gap for role in self.roles.values() for gap in gaps(role)]
        inherited += [gap for _, receipt in self.receipts.values() for gap in receipt['blockers']]
        self.inherited = {canonical(gap): gap for gap in inherited}

    def rows(self, reference):
        return self.ctx.resolver.resolve(reference)['rows']

    def evidence(self):
        # Check 的公共结构只接受 ArtifactRef；表引用保留在原 Candidate 与五表中。
        return [self.ctx.input_ref, self.ctx.candidate_ref, self.ctx.method_ref,
                *[ref for ref, _ in self.receipts.values()]]
