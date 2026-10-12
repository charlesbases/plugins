"""SC04 实际宿主批次与生产者私有结果装配边界。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from investment.contracts import canonical, is_known, require
from investment.research.references import selected_references

PREFIX = 'investment.sc04.'


@dataclass
class Analysis:
    identities: dict = field(default_factory=dict)
    provenance: list = field(default_factory=list)
    exposures: list = field(default_factory=list)
    source_coverage: list = field(default_factory=list)
    gaps: list = field(default_factory=list)


def _unique_refs(references):
    result = {}
    for reference in references:
        previous = result.get(reference['uri'])
        require(previous is None or previous == reference, 'U02', '/allowed_evidence_refs', 'Conflicting analysis reference')
        result[reference['uri']] = reference
    return list(result.values())


def _capture_scope(ctx, references):
    queue, seen, captures = list(references), set(), {}
    grants = {row['schema_id']: row['reference_paths'] for row in ctx.definition['reference_grants']}
    while queue:
        reference = queue.pop()
        if reference['uri'] in seen:
            continue
        seen.add(reference['uri'])
        value = ctx.resolver.resolve(reference)
        if reference['schema_id'] == PREFIX + 'capture':
            captures[reference['uri']] = reference
        for pointer in grants.get(reference['schema_id'], ()):
            queue.extend(selected_references(value, pointer))
    return captures


def _check_citations(ctx, value, captures):
    if isinstance(value, list):
        for child in value:
            _check_citations(ctx, child, captures)
    elif isinstance(value, dict):
        if 'citation_id' in value and 'raw_ref' in value:
            reference = value['raw_ref']
            require(captures.get(reference['uri']) == reference, 'U08', '/raw_ref', 'Analysis citation is outside the exact batch source scope')
            ctx.resolver.resolve(reference, PREFIX + 'capture')
        for child in value.values():
            _check_citations(ctx, child, captures)


def _host(ctx, references):
    references = _unique_refs(references)
    key = hashlib.sha256(canonical([ctx.input_ref, ctx.method_ref, references])).hexdigest()
    response = ctx.host_response('source_analysis', work_key=key)
    if response is None:
        return ctx.request_host('source_analysis', references, work_key=key), None
    ctx.schemas.validate(PREFIX + 'analysis_batch', response)
    require(response['work_key'] == key, 'U01', '/work_key', 'Analysis response differs from its exact evidence batch')
    require(not response['peer_groups'] and not response['member_dispositions'],
            'U09', '/peer_groups', 'Model must return source relationships rather than duplicate derived partitions')
    _check_citations(ctx, response, _capture_scope(ctx, references))
    return None, response


def analyse(ctx, discovery, spec_ref, spec, common_refs, groups, group_refs, *, reuse_refs=None, hypothesis_ids=()):
    """只分析实际已取资料；未取材条目仍由主流程完整保留。"""
    result = Analysis()
    feedback_refs = [reference for reference in common_refs if reference['schema_id'] == 'investment.research_failure']
    request, response = _host(ctx, [spec_ref, *discovery.original_lists, *feedback_refs])
    if request is not None:
        return request, None
    require(not any(response[key] for key in ('subject_ids', 'identities', 'membership_provenance', 'exposure_links')),
            'U04', '/source_coverage', 'Source-domain batch returned unrelated member objects')
    source_ids = [row['source_id'] for row in response['source_coverage']]
    require(len(source_ids) == len(set(source_ids)) and set(source_ids) == {row['source_id'] for row in spec['source_domains']},
            'U04', '/source_coverage', 'Analysis dropped or invented a declared source domain')
    result.source_coverage = response['source_coverage']
    result.gaps.extend(response['gaps'])
    reuse_refs = reuse_refs or {}
    targets = list({entry[2]['uri']: entry for entry in discovery.entries if discovery.member_evidence.get(entry[0]) or reuse_refs.get(entry[0])}.values())
    subjects = {subject for group in groups.values() for subject in group['subject_ids']}
    # 批次仅约束一次上下文；所有实际有证据的目标都会遍历，不构成top N对象范围。
    for start in range(0, len(targets), 8):
        batch = targets[start:start + 8]
        references = [entry[2] for entry in batch] + [spec_ref] + list(group_refs.values()) + list(common_refs)
        for member, entry, _ in batch:
            references.extend(discovery.member_evidence.get(member, {}).values())
            references.extend(reuse_refs.get(member, ()))
        request, response = _host(ctx, references)
        if request is not None:
            return request, None
        require(set(response['subject_ids']) == subjects and not response['source_coverage'],
                'U03', '/subject_ids', 'Member analysis changed the exact batch subjects')
        members = {entry[0]: entry for entry in batch}
        identity_ids = [row['identity_id'] for row in response['identities']]
        require(len(identity_ids) == len(set(identity_ids)) and set(identity_ids) <= set(members),
                'U06', '/identities', 'Model returned a foreign or duplicated member identity')
        for identity in response['identities']:
            original = members[identity['identity_id']][1]['original_code']
            require(not (is_known(original) and is_known(identity['fund_code'])) or original['value'] == identity['fund_code']['value'],
                    'U06', '/fund_code', 'Identity does not describe its exact original source entry')
            result.identities[identity['identity_id']] = identity
        expected = {(member, reference['uri'], group_id) for member, _, reference in batch for group_id in groups}
        actual = set()
        for row in response['membership_provenance']:
            pair = (row['member_id'], row['source_entry_ref']['uri'], row['peer_group_id'])
            require(pair in expected and pair not in actual, 'U08', '/membership_provenance', 'Foreign or duplicate original relationship')
            member = members[row['member_id']]
            require(row['source_entry_ref'] == member[2] and row['rule_ref'] == groups[row['peer_group_id']]['peer_rule_ref'],
                    'U08', '/rule_ref', 'Relationship changed the exact source entry or peer rule')
            require(row['relation'] == 'possible' or row['classification_citations'],
                    'U08', '/classification_citations', 'Known peer relation requires actual classification evidence')
            actual.add(pair)
        require(actual == expected, 'U08', '/membership_provenance', 'Analysis omitted an original member/group relationship')
        require(not any(row['relation'] == 'possible' for row in response['membership_provenance']) or response['gaps'],
                'U08', '/gaps', 'Unknown relationship was returned without its actual evidence gap')
        for row in response['exposure_links']:
            require(row['member_id'] in members and (not is_known(row['hypothesis_id']) or row['hypothesis_id']['value'] in hypothesis_ids),
                    'U10', '/exposure_links', 'Exposure references a foreign member or hypothesis')
            mapping_ref = next(ref for ref in common_refs if ref['schema_id'] == PREFIX + 'exposure_mapping_rule')
            mapping = ctx.resolver.resolve(mapping_ref, PREFIX + 'exposure_mapping_rule')
            require(row['mapping_rule_ref'] == mapping_ref and row['exposure_kind'] in mapping['permitted_kinds'],
                    'U10', '/mapping_rule_ref', 'Exposure changed the frozen mapping rule or kind')
        result.provenance.extend(response['membership_provenance'])
        result.exposures.extend(response['exposure_links'])
        result.gaps.extend(response['gaps'])
    return None, result
