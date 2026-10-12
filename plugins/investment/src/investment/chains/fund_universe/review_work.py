"""SC04 私有语义工作项：原对象切片、依赖闭包和准确摘要绑定。"""
from __future__ import annotations

import hashlib

from investment.contracts import canonical, is_known, require
from .source_validation import json_pointer

PREFIX = 'investment.sc04.'
SEMANTIC_FIELDS = {
    'identity': ['entity_id', 'legal_fund_id', 'share_class_id', 'fund_code', 'issuer_id', 'currency',
                 'asset_class', 'benchmark_id', 'benchmark_terms', 'strategy_id', 'legal_structure',
                 'share_class_terms', 'channel_eligibility', 'effective_at', 'available_time', 'availability_bound_at'],
    'coverage': ['source_coverage', 'subject_dispositions', 'current_status', 'historical_status', 'historical_origin_status'],
    'catalog_vintage': ['source_domains', 'available_time', 'availability_bound_at', 'list_semantics_citations'],
    'discovery_audit': ['closure_checks', 'completeness_status'],
    'exposure_row': ['member_id', 'share_class_id', 'hypothesis_id', 'exposure_kind', 'exposure_subject',
                     'weight', 'lower_weight', 'upper_weight', 'denominator', 'economic_at',
                     'published_at', 'available_time', 'availability_bound_at'],
}


def citations(value):
    """提取原定位，不用引文的存在代替语义核验。"""
    if isinstance(value, list):
        return [citation for child in value for citation in citations(child)]
    if not isinstance(value, dict):
        return []
    if {'citation_id', 'raw_ref', 'locator', 'quoted_text'} <= value.keys():
        return [value]
    return [citation for child in value.values() for citation in citations(child)]


def field_batches(value, fields):
    """数组逐元素，固定标量字段合为一批，空数组保留其字段目标。"""
    scalars = []
    for field in fields:
        child = value[field]
        if isinstance(child, list) and child:
            for index in range(len(child)):
                yield [f'/{field}/{index}']
        else:
            scalars.append('/' + field)
    if scalars:
        yield scalars


class ReviewWorkPlanner:
    """从目标实际关系取依赖，不将全已加载引用复制进每个请求。"""
    def __init__(self, review):
        self.review = review
        self.refs, self.values = review.refs, review.values
        self.indices = {}
        self.member_indices = {row['member_id']: index for index, row in enumerate(review.rows['members_ref'])}
        self.group_indices = {row['peer_group_id']: index for index, row in enumerate(review.rows['peer_groups_ref'])}
        self.historical_exposures = {}
        for index, row in enumerate(review.history):
            for reference in row['exposure_version_refs']:
                self.historical_exposures.setdefault((reference['uri'], row['member_id']), []).append(index)

    def targets(self):
        for uri in sorted(self.refs):
            reference, value = self.refs[uri], self.values[uri]
            name = reference['schema_id'].removeprefix(PREFIX)
            if name in SEMANTIC_FIELDS:
                for paths in field_batches(value, SEMANTIC_FIELDS[name]):
                    yield reference, paths
            elif name in ('membership_provenance', 'exposures', 'history'):
                for index in range(len(value['rows'])):
                    yield reference, [f'/rows/{index}']
        if self.review.funding is not None:
            yield self.review.funding_ref, ['/available_amount', '/declared_at']

    def build(self, reference, paths):
        self.locations = {}
        name = reference['schema_id'].removeprefix(PREFIX)
        selected = json_pointer(self.values[reference['uri']], paths[0]) if len(paths) == 1 else {}
        context = selected if isinstance(selected, dict) else {}
        for path in paths:
            self.add(reference, path, context)
        if name == 'identity':
            for field in ('identity_citations', 'identity_id', 'entity_id', 'share_class_id', 'available_time', 'availability_bound_at'):
                self.add(reference, '/' + field, context)
        elif name in ('exposures', 'exposure_row', 'membership_provenance'):
            row = selected if name != 'exposure_row' else self.values[reference['uri']]
            context = row
            historical = self.historical_exposures.get((reference['uri'], row['member_id']), []) if name in ('exposures', 'exposure_row') else []
            if historical:
                for index in historical:
                    self.add(self.review.payload['universe_history_ref']['value'], f'/rows/{index}', row)
            else:
                self.member(row['member_id'])
            if name == 'membership_provenance':
                self.group(row['peer_group_id'])
            if is_known(row.get('hypothesis_id', {})):
                self.table(self.review.assessment['hypotheses_ref'], {'hypothesis_id': row['hypothesis_id']['value']})
            if name == 'exposure_row':
                self.add(reference, '/citations', row)
                self.add(reference, '/mapping_rule_ref', row)
        elif name == 'catalog_vintage':
            for field in ('list_semantics_citations', 'available_time', 'availability_bound_at'):
                self.add(reference, '/' + field, context)
        elif name == 'discovery_audit':
            if '/completeness_status' in paths:
                self.add(reference, '/closure_checks', context)
            self.discovery(context)
            self.acquisition(context)
            self.table(self.review.audit['unprocessed_scope_ref'], context)
        elif name == 'coverage':
            self.coverage(paths, context)
        elif reference == self.review.funding_ref:
            self.add(self.review.funding['source_ref'], '', context)
        locations = [self.locations[key] for key in sorted(self.locations)]
        work = {'target_ref': reference, 'field_paths': paths, 'evidence_locations': locations}
        work['work_key'] = hashlib.sha256(canonical(work)).hexdigest()
        return work

    def add(self, reference, pointer='', context=None):
        context = context or {}
        key = (reference['uri'], pointer)
        if key in self.locations:
            return
        require(self.refs.get(reference['uri']) == reference, 'U10', '/evidence_locations',
                'Semantic dependency is not an exact loaded granted reference')
        value = json_pointer(self.values[reference['uri']], pointer)
        self.locations[key] = {'object_ref': reference, 'json_pointer': pointer, 'source_locators': list({canonical(citation): citation for citation in citations(value)}.values())}
        if reference['schema_id'] == PREFIX + 'capture' or isinstance(value, str):
            return
        self.follow(value, context)

    def follow(self, value, context):
        if isinstance(value, list):
            for child in value:
                self.follow(child, context)
        elif isinstance(value, dict):
            if {'uri', 'schema_id', 'sha256', 'artifact_id'} <= value.keys():
                uri = value['uri']
                if uri not in self.values:
                    return
                require(self.refs.get(uri) == value, 'U10', '/evidence_locations',
                        'Loaded reference metadata must match original reference')
                if value['schema_id'] == PREFIX + 'discovery_spec':
                    self.discovery(context)
                elif isinstance(self.values[value['uri']], dict) and 'rows' in self.values[value['uri']]:
                    self.table(value, context)
                else:
                    self.add(value, '', context)
            else:
                if 'source_ref' in value and 'source_locator' in value:
                    self.add(value['source_ref'], value['source_locator'], context)
                    children = [child for key, child in value.items() if key not in ('source_ref', 'source_locator')]
                else:
                    children = value.values()
                for child in children:
                    self.follow(child, context)

    def table(self, reference, context):
        rows = self.values[reference['uri']]['rows']
        matches = None
        fields = {field: [context[field]] for field in ('member_id', 'hypothesis_id', 'source_id', 'task_id', 'origin_at')
                  if isinstance(context.get(field), str)}
        if 'task_ids' in context:
            fields['task_id'] = context['task_ids']
        if 'peer_group_id' in context:
            fields['peer_group_ids'] = [context['peer_group_id']]
        for field, values in fields.items():
            key = (reference['uri'], field)
            if key not in self.indices:
                index = {}
                for position, row in enumerate(rows):
                    present = row.get(field, [])
                    for value in present if isinstance(present, list) else [present]:
                        if isinstance(value, str):
                            index.setdefault(value, set()).add(position)
                self.indices[key] = index
            index = self.indices[key]
            if not index and not any(field in row for row in rows):
                continue
            selected = set().union(*(index.get(value, set()) for value in values))
            matches = selected if matches is None else matches & selected
        matches = sorted(matches) if matches is not None else range(len(rows))
        if not rows:
            self.add(reference, '/rows', context)
        for index in matches:
            self.add(reference, f'/rows/{index}', context)
        return [rows[index] for index in matches]

    def acquisition(self, context):
        tasks = self.table(self.review.audit['query_tasks_ref'], context)
        self.table(self.review.audit['attempts_ref'], {**context, 'task_ids': [row['task_id'] for row in tasks]})

    def member(self, member_id):
        reference = self.review.payload['members_ref']
        if member_id in self.member_indices:
            index = self.member_indices[member_id]
            row = self.values[reference['uri']]['rows'][index]
            for field in ('member_id', 'entity_id', 'fund_code', 'identity_ref', 'source_entry_refs'):
                self.add(reference, f'/rows/{index}/{field}', row)

    def group(self, group_id):
        reference = self.review.payload['peer_groups_ref']
        if group_id in self.group_indices:
            index = self.group_indices[group_id]
            row = self.values[reference['uri']]['rows'][index]
            for field in ('peer_group_id', 'peer_rule_ref', 'comparison_citations', 'subject_ids'):
                self.add(reference, f'/rows/{index}/{field}', row)

    def discovery(self, context):
        reference = self.review.role_ref('universe_discovery_spec_ref')
        value = self.values[reference['uri']]
        rule_id = context.get('peer_rule_id')
        if 'peer_group_id' in context and context['peer_group_id'] in self.review.groups:
            peer_ref = self.review.groups[context['peer_group_id']]['peer_rule_ref']
            rule_id = self.values[peer_ref['uri']]['peer_rule_id']
        for field in ('product_scope_basis', 'source_domains', 'peer_rules', 'required_subjects'):
            for index, row in enumerate(value[field]):
                if field == 'required_subjects' and row['subject_id'] != context.get('subject_id'):
                    continue
                if field == 'peer_rules' and row['peer_rule_id'] != rule_id:
                    continue
                if field == 'source_domains' and 'source_id' in context and row['source_id'] != context['source_id']:
                    continue
                self.add(reference, f'/{field}/{index}', context)

    def coverage(self, paths, context):
        reference = self.review.payload['coverage_ref']
        if any(path.startswith('/subject_dispositions/') for path in paths):
            self.discovery(context)
            self.acquisition(context)
        elif any(path.startswith('/source_coverage/') for path in paths):
            self.discovery(context)
            self.acquisition(context)
            self.table(self.review.audit['unprocessed_scope_ref'], context)
        elif any(path.startswith('/historical_origin_status/') for path in paths):
            self.add(self.review.role_ref('scope_time_request_ref'), '/historical_origins', context)
        else:
            # 全局状态的语义依赖是逐项已核状态；集合和可得性仍由确定性U15/U19独立计算。
            for field in ('source_coverage', 'subject_dispositions', 'historical_origin_status'):
                self.add(reference, '/' + field, context)
            self.add(self.review.payload['discovery_audit_ref'], '/closure_checks', context)
