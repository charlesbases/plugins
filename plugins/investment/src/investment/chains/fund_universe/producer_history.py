"""SC04 真实旧版本范围；缺档案不使用今天名单回填。"""
from __future__ import annotations

from dataclasses import dataclass, field

from investment.contracts import ContractError, instant, is_known, known, unknown
from .producer_analysis import analyse
from .producer_rules import available_at, partition, reconcile_entries
from .source_discovery import Discovery, PREFIX, identity_key
from .source_production import parse_directory, source_entry


def evidence_gap(rule, path, reason, uses):
    return {'rule_id': rule, 'field_or_object': path, 'reason': reason, 'owner': 'source',
            'required_evidence': ['source_bytes'], 'affected_uses': list(uses)}


@dataclass
class History:
    rows: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    catalog_origins: set = field(default_factory=set)
    complete_origins: set = field(default_factory=set)


def reconstruct(ctx, clock, catalog_ref, spec_ref, spec, groups, common_refs, eligibility_ref, eligibility, material, hypothesis_ids):
    result = History()
    catalog = ctx.resolver.resolve(catalog_ref, PREFIX + 'catalog_vintage') if catalog_ref else None
    for origin in clock['historical_origins']:
        path = '/history/' + identity_key('origin', origin)
        if catalog is None or not available_at(catalog, origin):
            reason = 'No supplied original catalog version is demonstrably available by ' + origin
            result.statuses.append({'origin_at': origin, 'status': 'unknown', 'universe_version_id': unknown(reason),
                                    'reason': reason, 'evidence_refs': [catalog_ref] if catalog_ref else []})
            result.gaps.append(evidence_gap('U15', path, reason, ['pit_training']))
            continue
        version = identity_key('history', catalog_ref, origin)
        discovery = Discovery(original_lists=list(catalog['original_list_refs']))
        following, urls, task_refs = set(), set(), []
        try:
            for raw_ref in catalog['original_list_refs']:
                capture = ctx.resolver.resolve(raw_ref, PREFIX + 'capture')
                page = parse_directory(capture)
                discovery.captures[raw_ref['uri']] = (raw_ref, capture)
                urls.update((capture['request_url'], capture['response_url']))
                following.update(page.following)
                matching = [item for item in catalog['source_domains'] if item['entry_url'] in (capture['request_url'], capture['response_url'])]
                if not matching and len(catalog['source_domains']) == 1:
                    matching = list(catalog['source_domains'])
                if len(matching) != 1:
                    raise ContractError('U15', '/source_domains', 'Historical page has no unique original source-domain assignment', 'missing_evidence')
                source = matching[0]
                task_id = identity_key('history-task', version, source['source_id'], raw_ref)
                task = {'task_id': task_id, 'parent_task_id': {'status': 'not_applicable', 'value': None, 'reason': 'supplied_original_version'},
                        'source_id': source['source_id'], 'subject_ids': [row['subject_id'] for row in spec['required_subjects']],
                        'peer_group_ids': list(groups), 'capability': 'history', 'request_url': capture['request_url'],
                        'query_text': {'status': 'not_applicable', 'value': None, 'reason': 'read_supplied_original_bytes_not_a_new_HTTP_request'},
                        'page_or_cursor': {'status': 'not_applicable', 'value': None, 'reason': 'source_vintage_lookup'},
                        'discovery_parent_ref': known(catalog_ref), 'task_state': 'pending', 'attempt_ids': [],
                        'continuation_task_ids': [], 'terminal_citations': [], 'requested_origin': origin}
                task_refs.append(ctx.put_json('history-task-' + task_id + '.json', task, PREFIX + 'query_task_row'))
                for entry in page.entries:
                    row = source_entry(entry, source_id=source['source_id'], task_id=task_id, raw_ref=raw_ref)
                    reference = ctx.put_json('history-entry-' + version + '-' + row['entry_id'] + '.json', row, PREFIX + 'source_entry')
                    discovery.entries.append((row['entry_id'].replace('entry-', 'member-', 1), row, reference))
        except (ContractError, UnicodeError, LookupError, ValueError) as error:
            reason = 'Original historical catalog cannot be fully enumerated: ' + str(error)
            result.statuses.append({'origin_at': origin, 'status': 'unknown', 'universe_version_id': known(version), 'reason': reason, 'evidence_refs': [catalog_ref]})
            result.gaps.append(evidence_gap('U15', path, reason, ['pit_training']))
            continue
        result.catalog_origins.add(origin)
        historical_groups, group_refs = {}, {}
        for key, group in groups.items():
            value = dict(group, original_list_refs=list(catalog['original_list_refs']))
            historical_groups[key] = value
            group_refs[key] = ctx.put_json('history-group-' + version + '-' + key + '.json', value, PREFIX + 'peer_group')
        lookup = {member: material.get(row['original_code']['value'], ()) if is_known(row['original_code']) else ()
                  for member, row, _ in discovery.entries}
        request, analysis = analyse(ctx, discovery, spec_ref, spec, [*common_refs, catalog_ref, *task_refs], historical_groups, group_refs,
                                    reuse_refs=lookup, hypothesis_ids=hypothesis_ids)
        if request is not None:
            return request, None
        identities = {member: analysis.identities.get(member) for member, _, _ in discovery.entries}
        sources, identities, provenance, exposure_rows, merge_gaps = reconcile_entries(discovery.entries, identities, analysis.provenance, analysis.exposures, origin, ['pit_training'])
        result.gaps.extend(merge_gaps)
        _, _, statuses = partition(list(sources), historical_groups, identities, provenance, eligibility, origin)
        ready = bool(discovery.entries) and not merge_gaps and following <= urls and all(row['status'] == 'complete' for row in analysis.source_coverage)
        for member, originals in sources.items():
            row = originals[0][1]
            identity = identities[member]
            identity_ref = ctx.put_json('history-identity-' + version + '-' + member + '.json', identity, PREFIX + 'identity') if identity else None
            usable = identity is not None and available_at(identity, origin)
            exposures = []
            for exposure in exposure_rows:
                if exposure['member_id'] == member and usable and available_at(exposure, origin):
                    exposures.append(ctx.put_json('history-exposure-' + version + '-' + identity_key('item', exposure) + '.json', exposure, PREFIX + 'exposure_row'))
            bound = max((catalog['availability_bound_at']['value'], identity['availability_bound_at']['value']), key=instant) if usable else None
            result.rows.append({'origin_at': origin, 'universe_version_id': version, 'member_id': member,
                                'entity_id': identity['entity_id'] if usable else unknown('Historical entity version is unavailable'),
                                'membership_status': statuses[member],
                                'identity_version_ref': known(identity_ref) if identity_ref else unknown('No historical legal/share identity evidence'),
                                'exposure_version_refs': exposures, 'eligibility_rule_ref': eligibility_ref,
                                'availability_bound_at': known(bound) if bound else unknown('No supported historical availability bound'),
                                'source_vintage_ref': catalog_ref})
            ready &= usable and statuses[member] != 'unknown' and is_known(row['original_code'])
        result.gaps.extend(analysis.gaps)
        reason = 'Original version, identities, relationships and continuation evidence support this origin' if ready else 'Original version is retained but historical identities, relationships or pagination remain unproven'
        result.statuses.append({'origin_at': origin, 'status': 'complete' if ready else 'unknown',
                                'universe_version_id': known(version), 'reason': reason, 'evidence_refs': [catalog_ref]})
        if ready:
            result.complete_origins.add(origin)
        else:
            result.gaps.append(evidence_gap('U15', path, reason, ['pit_training']))
    return None, result
