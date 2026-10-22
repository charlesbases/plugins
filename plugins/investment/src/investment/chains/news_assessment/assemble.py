"""SC03业务表装配与准确引用；不授予用途，不修改模型给出的原始事实。"""
from __future__ import annotations

from investment.contracts import canonical, digest, now, require
from .contracts import TABLES


def assemble_candidate(ctx, draft):
    ctx.schemas.validate('investment.sc03.assessment_draft', draft)
    roles = {row['role']: row['ref'] for row in ctx.input['input_refs']}
    protocol = ctx.read_role('assessment_protocol_ref')
    identity = ctx.read_role('analyst_identity_ref')
    gaps = list(draft['unresolved_questions'])
    for role in ctx.definition['input_roles']:
        if role['role'] in roles and role['source']['kind'] == 'upstream_output':
            _, receipt = ctx.upstream_receipt(role['role'])
            for gap in receipt['blockers']:
                if gap not in gaps:
                    gaps.append(gap)
    unknown_identity = any(identity[field]['status'] == 'unknown'
                           for field in ('provider', 'model_name', 'model_version', 'training_cutoff'))
    if unknown_identity or protocol['interpretation_mode'] == 'historical_exploration':
        restriction = {'rule_id': 'A13', 'field_or_object': '/analyst_identity',
            'reason': 'Verified model metadata or training cutoff is unavailable, or interpretation is historical; training contamination has not been excluded.',
            'owner': 'producer', 'required_evidence': ['source_bytes'], 'affected_uses': ['pit_training']}
        if restriction not in gaps:
            gaps.append(restriction)
    refs = {}
    for name, (field, primary_key, schema_id) in TABLES.items():
        source = 'economic_observations' if name == 'observations' else name
        rows = draft[source]['rows']
        require(len({row[primary_key] for row in rows}) == len(rows), 'A02', '/' + source, 'Duplicate business primary key')
        value = {'rows': sorted(rows, key=lambda row: row[primary_key])}
        reference = ctx.put_json('assessment-' + name + '.json', value, schema_id)
        refs[field] = ctx.dataset_ref(value, schema_id, reference['uri'])
    payload = dict(refs, unresolved_questions=gaps, analyst_identity_ref=roles['analyst_identity_ref'],
                   assessment_protocol_ref=roles['assessment_protocol_ref'])
    payload_ref = ctx.put_json('assessment.json', payload, 'investment.sc03.research_assessment')
    return {'contract_id': 'investment.candidate_output', 'contract_version': 1, 'run_id': ctx.run_id,
            'stage_id': 'SC03', 'output_id': 'assessment-' + digest(canonical([ctx.invocation_id, ctx.attempt_no]))[:40],
            'created_at': now(), 'processing_state': 'completed', 'input_refs': ctx.input['input_refs'],
            'scope_ref': ctx.input['scope_ref'], 'method_ref': ctx.method_ref, 'payload_ref': payload_ref,
            'datasets': list(refs.values()), 'evidence_refs': [], 'gaps': gaps}
