"""SC06 当前候选、受限输入快照及逐规则结果。"""
from dataclasses import dataclass, field
from decimal import Decimal

from investment.contracts import ContractError, canonical, digest, instant, now, require


def index(rows, fields, rule='F04'):
    keys = [tuple(row[key] for key in fields) for row in rows]
    require(len(keys) == len(set(keys)), rule, '/primary_key', 'Duplicate business key')
    return dict(zip(keys, rows))


def known(value, rule, path):
    require(value['status'] == 'known', rule, path, 'Required value is unknown', 'missing_evidence')
    return value['value']


def number(value, rule='F08', path='/number'):
    result = Decimal(value)
    require(result.is_finite(), rule, path, 'Nonfinite numerical value')
    return result


def host_work(snapshot, rule):
    """分别核当前producer成功数和两角色保留预算，不读取跨角色内容。"""
    summary = snapshot.ctx.host_work_summary()
    require(snapshot.artifacts['tuning_audit']['actual_host_work_item_count'] == summary.completed_count('producer'),
            rule, '/actual_host_work_item_count', 'Candidate host work count differs from completed producer work in this attempt')
    require(summary.registered_count() <= snapshot.protocol['search_budget']['max_host_work_items'],
            rule, '/max_host_work_items', 'Registered two-role host work exceeds this attempt budget')
    return summary


@dataclass
class Snapshot:
    ctx: object
    bundle: dict
    universe: dict
    protocol: dict
    payload: dict
    views: dict
    view_refs: dict
    upstream: dict
    artifacts: dict
    panels: dict
    labels: dict
    observations: dict
    calendars: dict
    members: list
    risks: list
    gaps: dict
    evidence: list = field(default_factory=list)

    @classmethod
    def load(cls, ctx):
        require(ctx.role == 'validator' and ctx.candidate is not None and ctx.candidate_ref is not None,
                'F01', '/candidate', 'Independent validator requires a completed candidate')
        ctx.schemas.validate('investment.stage_input', ctx.input)
        ctx.schemas.validate('investment.candidate_output', ctx.candidate)
        require(ctx.resolver.resolve(ctx.candidate_ref, 'investment.candidate_output') == ctx.candidate,
                'F01', '/candidate', 'Candidate differs from immutable current artifact')
        bundle = ctx.read_role('dataset_bundle_ref', 'investment.sc05.dataset_bundle')
        universe = ctx.read_role('instrument_universe_ref', 'investment.sc04.instrument_universe')
        protocol = ctx.resolver.resolve(bundle['experiment_protocol_ref'], 'investment.sc05.experiment_protocol')
        payload = ctx.resolver.resolve(ctx.candidate['payload_ref'], 'investment.sc06.forecast_bundle')
        upstream = {row['role']: ctx.upstream_receipt(row['role']) for row in ctx.definition['input_roles']}
        views, view_refs, panels, labels, observations, calendars = {}, {}, {}, {}, {}, {}
        for partition in ('training', 'selection', 'current'):
            ref = bundle[partition + '_view_ref']
            view = ctx.resolver.resolve(ref, 'investment.sc05.data_view')
            require(view['partition'] == partition and view['protocol_ref'] == bundle['experiment_protocol_ref']
                    and 'SC06' in view['permitted_stages'] and view['raw_access'] is False,
                    'F02', '/views/' + partition, 'View is not the exact authorized physical partition')
            views[partition], view_refs[partition] = view, ref
            calendars[partition] = ctx.resolver.resolve(view['calendar_ref'], 'investment.sc05.calendar')['rows']
            for field_name, destination, fields in (
                ('asof_panel_ref', panels, ('sample_id',)),
                ('label_events_ref', labels, ('label_id',)),
                ('observation_vintages_ref', observations, ('version_id',)),
            ):
                rows = ctx.resolver.resolve(view[field_name])['rows']
                index(rows, fields)
                for row in rows:
                    key = row[fields[0]]
                    require(key not in destination or destination[key] == row, 'F04', '/views', 'Conflicting version across authorized views')
                    destination[key] = row
        artifacts = {}
        for key in ('fitted_model_ref', 'model_rule_ref', 'preprocessing_ref', 'training_audit_ref',
                    'tuning_audit_ref', 'member_forecast_dispositions_ref', 'historical_predictions_ref',
                    'current_predictions_ref', 'residuals_ref', 'current_joint_scenarios_ref',
                    'scenario_method_ref', 'assumptions_ref', 'diagnostics_ref'):
            artifacts[key.removesuffix('_ref')] = ctx.resolver.resolve(payload[key])
        members = ctx.resolver.resolve(universe['members_ref'])['rows']
        risks = ctx.resolver.resolve(universe['mandatory_assets_ref'])['rows']
        data_gaps = ctx.resolver.resolve(bundle['data_gaps_ref'])
        inherited = [gap for _, receipt in upstream.values() for gap in receipt['blockers']]
        gaps = {'g-' + digest(canonical(gap)): gap for gap in data_gaps['gaps'] + inherited}
        evidence = [ctx.input_ref, ctx.candidate_ref, ctx.method_ref, ctx.candidate['payload_ref']]
        evidence += [ref for ref, _ in upstream.values()] + list(view_refs.values())
        evidence += [payload[key] for key in payload if key.endswith('_ref')]
        evidence += [bundle['data_gaps_ref'], universe['members_ref'], universe['mandatory_assets_ref']]
        evidence += [view[key] for view in views.values() for key in ('asof_panel_ref', 'label_events_ref', 'observation_vintages_ref', 'calendar_ref')]
        return cls(ctx, bundle, universe, protocol, payload, views, view_refs, upstream, artifacts,
                   panels, labels, observations, calendars, members, risks, gaps, evidence)

    @property
    def protocol_ref(self):
        return self.bundle['experiment_protocol_ref']

    def predictors(self, entity):
        return [row for row in self.protocol['model_search_space']['predictors']
                if row['applicability']['kind'] == 'all_origin_forecast_entities'
                or entity in row['applicability']['entity_ids']]

    def rows(self, name):
        return self.artifacts[name]['rows']


class Checks:
    """规则不默认通过，真实不足与断言错误分别生成凭证。"""

    def __init__(self, ctx):
        self.ctx, self.results, self.blockers = ctx, {}, []

    def run(self, rule, action):
        try:
            result = action()
        except ContractError as exc:
            self.record(rule, 'insufficient_evidence' if exc.code == 'missing_evidence' else 'failed', str(exc), exc.pointer)
        else:
            self.record(rule, 'passed', 'Independently checked against this attempt and authorized input')
            return result

    def record(self, rule, verdict, reason, path='/candidate'):
        previous = self.results.get(rule)
        rank = {'passed': 0, 'insufficient_evidence': 1, 'failed': 2}
        if previous and rank[previous['verdict']] > rank[verdict]:
            return
        self.results[rule] = {'rule_id': rule, 'verdict': verdict, 'reason': reason[:2048], 'evidence_refs': []}
        if verdict != 'passed':
            uses = [row['use'] for row in self.ctx.definition['use_rules'] if rule in row['required_pass_rules']]
            self.blockers.append({'rule_id': rule, 'field_or_object': path if path != '/' else '/candidate',
                                  'reason': reason[:2048], 'owner': 'validator', 'required_evidence': ['source_bytes'],
                                  'affected_uses': uses})

    def receipt(self, snapshot):
        require(set(self.results) == set(self.ctx.definition['required_rules']), 'F18', '/checks', 'Incomplete rule dispatch')
        critical = all(self.results[key]['verdict'] == 'passed' for key in self.ctx.definition['critical_rules'])
        allowed = [row['use'] for row in self.ctx.definition['use_rules'] if critical
                   and all(self.results[key]['verdict'] == 'passed' for key in row['required_pass_rules'])]
        failed = any(row['verdict'] == 'failed' for row in self.results.values())
        blockers = [gap for _, receipt in snapshot.upstream.values() for gap in receipt['blockers']]
        blockers += self.ctx.candidate['gaps'] + snapshot.payload['missing_evidence'] + self.blockers
        blockers = list({canonical(gap): gap for gap in blockers}.values())
        for row in self.results.values():
            row['evidence_refs'] = list({canonical(ref): ref for ref in snapshot.evidence}.values())
        advanced = 'selection_comparison' in allowed
        return {'validator_id': 'sc06-validator-v1', 'validator_version': 'v1', 'run_id': self.ctx.run_id,
                'stage_id': 'SC06', 'input_refs': self.ctx.input['input_refs'], 'candidate_output_ref': self.ctx.candidate_ref,
                'validated_at': now(), 'checks': [self.results[key] for key in self.ctx.definition['required_rules']],
                'overall_verdict': 'failed' if failed else 'passed' if advanced and not blockers else 'partial',
                'business_result': 'rejected' if failed else 'usable' if advanced else 'observation_only' if allowed else 'blocked',
                'allowed_uses': allowed, 'blockers': blockers}
