"""SC04 实际取材、证据约束分析和完整范围候选；许可归独立验证者。"""
from __future__ import annotations

from datetime import timezone

from investment.contracts import canonical, instant, is_known, known, na, now, require, unknown
from .producer_analysis import analyse
from .producer_history import evidence_gap, reconstruct
from .producer_rules import available_at, mandatory_assets, partition, reconcile_entries
from .source_discovery import PREFIX, collect, identity_key
from .source_production import capture_body, html_document


def _table(ctx, name, rows, filename=None):
    schema = PREFIX + name
    table = next(row for row in ctx.schemas.stage_registries['SC04']['tables'] if row['schema_id'] == schema)
    rows = sorted(rows, key=lambda row: tuple(row[key].encode('utf-8') for key in table['primary_key']))
    value = {'rows': rows}
    reference = ctx.put_json((filename or name) + '.json', value, schema)
    return ctx.dataset_ref(value, schema, reference['uri'])


def _deduplicate(values):
    result, seen = [], set()
    for value in values:
        key = canonical(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _groups(ctx, spec, original_lists=None):
    groups = {}
    for rule in spec['peer_rules']:
        rule_ref = ctx.put_json('peer-rule-' + identity_key('rule', rule) + '.json', rule, PREFIX + 'peer_rule')
        subjects = [row['subject_id'] for row in spec['required_subjects'] if row['peer_rule_id'] == rule['peer_rule_id']]
        for subject in subjects or [None]:
            key = identity_key('group', rule['peer_rule_id'], subject)
            groups[key] = {'peer_group_id': key, 'peer_rule_ref': rule_ref, 'subject_ids': [subject] if subject else [],
                           'original_list_refs': list(original_lists or []), 'member_ids': [], 'possible_member_ids': [],
                           'excluded_member_ids': [], 'comparison_citations': [], 'completeness_status': 'unknown', 'closure_check_ids': []}
    return groups


def _subjects(spec, account, assessment, positions, pending, hypotheses):
    expected = {('hypothesis', canonical(assessment['hypotheses_ref']), '/rows/' + str(i)) for i, _ in enumerate(hypotheses)}
    expected.update(('exposure', canonical(assessment['hypotheses_ref']), f'/rows/{i}/affected_exposures/{j}')
                    for i, row in enumerate(hypotheses) for j, _ in enumerate(row['affected_exposures']))
    for kind, rows, reference in [('held_instrument', positions, account['positions_ref']), ('pending_instrument', pending, account['pending_transactions_ref'])]:
        expected.update((kind, canonical(reference), '/rows/' + str(i)) for i, _ in enumerate(rows))
    actual = [(row['kind'], canonical(row['source_ref']), row['source_locator']) for row in spec['required_subjects']]
    require(len(actual) == len(set(actual)) and set(actual) == expected, 'U03', '/required_subjects', 'Frozen discovery specification differs from all actual hypotheses, exposures and account risks')
    rules = {row['peer_rule_id'] for row in spec['peer_rules']}
    require(all(row['peer_rule_id'] in rules for row in spec['required_subjects']), 'U03', '/peer_rule_id', 'Search subject has no registered peer rule')


def _material(ctx, reference):
    material = {}
    if reference:
        evidence = ctx.resolver.resolve(reference, PREFIX + 'identity_evidence')
        for identity in evidence['identities']:
            if is_known(identity['fund_code']):
                copied = ctx.put_json('reused-identity-' + identity_key('identity', identity) + '.json', identity, PREFIX + 'identity')
                material.setdefault(identity['fund_code']['value'], []).append(copied)
    return material


def _catalog(ctx, discovery, spec, source_coverage, reusable_ref):
    citations = []
    for reference in discovery.original_lists:
        capture = discovery.captures[reference['uri']][1]
        document = html_document(capture)
        titles = document.xpath('//title')
        node = titles[0] if titles else document.xpath('//*[@id="code_content"]//li')[0]
        text = node.text_content().strip()
        citations.append({'citation_id': identity_key('citation', reference, 'list-semantics'), 'raw_ref': reference,
                          'locator_kind': 'html_element', 'locator': document.getroottree().getpath(node), 'quoted_text': text,
                          'available_time': unknown('Publication or prior source availability is not established by a retrieval timestamp'),
                          'availability_bound_at': known(capture['captured_at'])})
    latest = max((discovery.captures[ref['uri']][1] for ref in discovery.original_lists), key=lambda row: instant(row['captured_at']))
    # 捕获只有真实观察上界，不提供原始发布区间；更早的区间必须另有原版本依据。
    availability = unknown('No original publication/availability interval has been established for the complete listing')
    bound = known(latest['captured_at'])
    version_citations = []
    for reference in discovery.original_lists:
        proofs = [citation for row in source_coverage for citation in row['representativeness_citations']
                  if citation['raw_ref'] == reference and is_known(citation['available_time']) and is_known(citation['availability_bound_at'])]
        if not proofs:
            break
        proof = max(proofs, key=lambda row: instant(row['availability_bound_at']['value']))
        available_at(proof, proof['availability_bound_at']['value'])
        version_citations.append(proof)
    if len(version_citations) == len(discovery.original_lists):
        lower = max((instant(row['available_time']['value']['lower_bound']) for row in version_citations))
        upper = max((instant(row['available_time']['value']['upper_bound']) for row in version_citations))
        last_proof = max(version_citations, key=lambda row: instant(row['availability_bound_at']['value']))
        availability = known({'lower_bound': lower.astimezone(timezone.utc).isoformat(), 'upper_bound': upper.astimezone(timezone.utc).isoformat(),
                              'precision': 'instant' if lower == upper else 'interval', 'timezone': 'UTC', 'basis_ref': last_proof['raw_ref']})
        bound = last_proof['availability_bound_at']
        citations.extend(version_citations)
    elif reusable_ref:
        previous = ctx.resolver.resolve(reusable_ref, PREFIX + 'catalog_vintage')
        previous_captures = [ctx.resolver.resolve(reference, PREFIX + 'capture') for reference in previous['original_list_refs']]
        same_scope = [(row['source_id'], row['entry_url'], row['channel'], row['product_domain']) for row in previous['source_domains']] == [(row['source_id'], row['entry_url'], row['channel'], row['product_domain']) for row in spec['source_domains']]
        same_version = same_scope and len(previous_captures) == len(discovery.original_lists)
        unmatched = list(previous_captures)
        for reference in discovery.original_lists:
            actual = discovery.captures[reference['uri']][1]
            match = next((index for index, old in enumerate(unmatched)
                          if old['request_url'] == actual['request_url'] and old['response_url'] == actual['response_url']
                          and capture_body(old) == capture_body(actual)), None)
            if match is None:
                same_version = False
                break
            unmatched.pop(match)
        same_version &= not unmatched
        if same_version and is_known(previous['available_time']) and is_known(previous['availability_bound_at']):
            available_at(previous, previous['availability_bound_at']['value'])
            availability, bound = previous['available_time'], previous['availability_bound_at']
            citations.extend(previous['list_semantics_citations'])
    value = {'catalog_id': identity_key('catalog', discovery.original_lists), 'captured_at': latest['captured_at'],
             'source_domains': spec['source_domains'], 'original_list_refs': discovery.original_lists,
             'available_time': availability, 'availability_bound_at': bound, 'list_semantics_citations': _deduplicate(citations)}
    return ctx.put_json('catalog-vintage.json', value, PREFIX + 'catalog_vintage'), value


def produce(ctx):
    require(ctx.stage_id == 'SC04' and ctx.role == 'producer', 'U01', '/stage_id', 'SC04 producer context is required')
    refs = {row['role']: row['ref'] for row in ctx.input['input_refs']}
    account = ctx.read_role('transaction_context_ref', 'investment.transaction_context')
    assessment = ctx.read_role('research_assessment_ref', 'investment.sc03.research_assessment')
    spec = ctx.read_role('universe_discovery_spec_ref', PREFIX + 'discovery_spec')
    eligibility = ctx.read_role('eligibility_rule_ref', PREFIX + 'eligibility_rule')
    ctx.read_role('exposure_mapping_rule_ref', PREFIX + 'exposure_mapping_rule')
    clock = ctx.read_role('scope_time_request_ref', PREFIX + 'scope_time_request')
    parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], PREFIX + 'parameters')
    ctx.resolver.resolve(ctx.input['scope_ref'])
    require(instant(clock['current_at']) <= instant(ctx.input['information_cutoff_at']), 'U14', '/current_at', 'Universe origin is later than the frozen information cutoff')
    positions = ctx.resolver.resolve(account['positions_ref'], 'investment.positions')['rows']
    pending = ctx.resolver.resolve(account['pending_transactions_ref'], 'investment.pending_transactions')['rows']
    hypotheses = ctx.resolver.resolve(assessment['hypotheses_ref'], 'investment.sc03.hypotheses')['rows']
    _subjects(spec, account, assessment, positions, pending, hypotheses)
    inherited = [*account['missing_facts'], *assessment['unresolved_questions']]
    for role in ('transaction_context_ref', 'research_assessment_ref'):
        _, receipt = ctx.upstream_receipt(role)
        inherited.extend(receipt['blockers'])
    planned_groups = _groups(ctx, spec)
    discovery = collect(ctx, spec, clock, list(planned_groups), parameters['requested_uses'])
    task_ref = _table(ctx, 'query_tasks', list(discovery.tasks.values()), 'acquisition-tasks')
    attempt_ref = _table(ctx, 'attempts', discovery.attempts, 'acquisition-attempts')
    require(discovery.original_lists, 'U16', '/original_list_refs', 'No parsable original list was acquired; actual source-attempt records remain in this attempt')
    groups = {key: dict(group, original_list_refs=list(discovery.original_lists)) for key, group in planned_groups.items()}
    group_refs = {key: ctx.put_json('analysis-peer-' + key + '.json', group, PREFIX + 'peer_group') for key, group in groups.items()}
    material = _material(ctx, refs.get('identity_evidence_ref'))
    reuse = {member: material.get(row['original_code']['value'], ()) if is_known(row['original_code']) else () for member, row, _ in discovery.entries}
    common = _deduplicate([refs['eligibility_rule_ref'], refs['exposure_mapping_rule_ref'], refs['scope_time_request_ref'],
                           *[row['source_ref'] for row in spec['required_subjects']]])
    for failure in ctx.feedback:
        number = failure['attempt_no']
        require(1 <= number < ctx.attempt_no, 'U01', '/feedback/attempt_no', 'Feedback is not from a previous attempt')
        reference = ctx.resolver.path_ref(ctx.attempt_dir.parent / f'attempt-{number:02}' / 'failure.json', 'investment.research_failure')
        original = ctx.resolver.resolve(reference, 'investment.research_failure')
        require(canonical(original) == canonical(failure) and all(original[key] == value for key, value in
                    {'run_id': ctx.run_id, 'stage_id': ctx.stage_id, 'invocation_id': ctx.invocation_id,
                     'input_ref': ctx.input_ref, 'attempt_no': number}.items()),
                'U01', '/feedback', 'Feedback differs from its exact invocation failure')
        common.append(reference)
    request, analysis = analyse(ctx, discovery, refs['universe_discovery_spec_ref'], spec, common, groups, group_refs,
                                reuse_refs=reuse, hypothesis_ids={row['hypothesis_id'] for row in hypotheses})
    if request is not None:
        return request
    request, history = reconstruct(ctx, clock, refs.get('catalog_vintage_ref'), refs['universe_discovery_spec_ref'], spec,
                                   groups, common, refs['eligibility_rule_ref'], eligibility, material,
                                   {row['hypothesis_id'] for row in hypotheses})
    if request is not None:
        return request
    gaps = [*inherited, *analysis.gaps, *history.gaps]
    provenance = list(analysis.provenance)
    present = {(row['member_id'], row['peer_group_id']) for row in provenance}
    identities = {member: analysis.identities.get(member) for member, _, _ in discovery.entries}
    for member, entry, entry_ref in discovery.entries:
        for group_id, group in groups.items():
            if (member, group_id) not in present:
                provenance.append({'provenance_id': identity_key('provenance', member, entry_ref, group_id), 'member_id': member,
                                   'source_entry_ref': entry_ref, 'peer_group_id': group_id, 'relation': 'possible',
                                   'rule_ref': group['peer_rule_ref'], 'classification_citations': [], 'identity_merge_citations': []})
    sources, identities, provenance, exposures, merge_gaps = reconcile_entries(discovery.entries, identities, provenance, analysis.exposures, clock['current_at'], parameters['requested_uses'])
    gaps.extend(merge_gaps)
    for member, identity in identities.items():
        if identity is None or not available_at(identity, clock['current_at']):
            gaps.append(evidence_gap('U08', '/members/' + member + '/identity', 'Original entry is retained; legal/share identity available at the requested origin is not established', parameters['requested_uses']))
    identity_refs = {member: ctx.put_json('identity-' + member + '.json', identity, PREFIX + 'identity') for member, identity in identities.items() if identity}
    predicates, local, statuses = partition(list(sources), groups, identities, provenance, eligibility, clock['current_at'])
    for scope in discovery.unprocessed:
        task = discovery.tasks[scope['task_id']['value']]
        origin = task['requested_origin']
        if task['capability'] == 'history' and origin in history.complete_origins:
            continue
        if task['capability'] == 'history' and origin in history.catalog_origins:
            scope['scope_description'] = 'Historical catalog identified; historical identity/classification coverage remains incomplete at ' + origin
            scope['basis_refs'] = [refs['catalog_vintage_ref']]
        gaps.append(evidence_gap('U15' if task['capability'] == 'history' else 'U05', '/unprocessed_scope/' + scope['scope_gap_id'], scope['scope_description'], ['pit_training'] if task['capability'] == 'history' else scope['affected_uses']))
    scopes = [row for row in discovery.unprocessed if not (discovery.tasks[row['task_id']['value']]['capability'] == 'history' and discovery.tasks[row['task_id']['value']]['requested_origin'] in history.complete_origins)]
    current_scopes = [row for row in scopes if discovery.tasks[row['task_id']['value']]['requested_origin'] == clock['current_at']]
    source_coverage = []
    for source in spec['source_domains']:
        value = dict(next(row for row in analysis.source_coverage if row['source_id'] == source['source_id']))
        value['requested_domain'] = source['product_domain']
        value['task_ids'] = [row['task_id'] for row in discovery.tasks.values() if row['source_id'] == source['source_id'] and row['requested_origin'] == clock['current_at']]
        value['unprocessed_scope_ids'] = [row['scope_gap_id'] for row in current_scopes if row['source_id'] == source['source_id']]
        if value['unprocessed_scope_ids']:
            value['status'] = 'unknown'
        source_coverage.append(value)
    source_ready = all(row['status'] == 'complete' and is_known(row['verified_domain']) for row in source_coverage)
    source_ready &= any(row['issuer_scope'] == 'cross_issuer' for row in source_coverage)
    tasks_ready = not current_scopes and all(row['task_state'] == 'completed' for row in discovery.tasks.values() if row['requested_origin'] == clock['current_at'])
    catalog_ref, catalog = _catalog(ctx, discovery, spec, source_coverage, refs.get('catalog_vintage_ref'))
    catalog_ready = available_at(catalog, clock['current_at'])
    if not catalog_ready:
        gaps.append(evidence_gap('U14', '/catalog_vintage_ref', 'The exact listing version has no supported availability interval and bound at or before the frozen origin', ['selection_comparison', 'pit_training']))
    checks, final_group_refs = [], {}
    for group_id, group in groups.items():
        relation_rows = [row for row in provenance if row['peer_group_id'] == group_id]
        peers_ready = not local[group_id]['unknown'] and all(row['relation'] != 'possible' for row in relation_rows)
        group.update(member_ids=sorted(local[group_id]['candidate']), possible_member_ids=sorted(local[group_id]['unknown']),
                     excluded_member_ids=sorted(local[group_id]['excluded']), comparison_citations=_deduplicate([citation for row in relation_rows for citation in row['classification_citations']]))
        for rule, ready in [('U04', source_ready), ('U05', tasks_ready), ('U06', True), ('U08', peers_ready)]:
            check_id = identity_key('closure', group_id, rule)
            checks.append({'rule_id': rule, 'peer_group_id': group_id, 'check_id': check_id, 'verdict': 'passed' if ready else 'unknown',
                           'reason': 'Actual registered source, task and original-member conditions are satisfied' if ready else 'Original source, continuation or identity/classification evidence remains incomplete',
                           'evidence_refs': list(discovery.original_lists)})
            group['closure_check_ids'].append(check_id)
        group['completeness_status'] = 'complete' if source_ready and tasks_ready and peers_ready and catalog_ready and not merge_gaps else 'unknown'
        final_group_refs[group_id] = ctx.put_json('peer-' + group_id + '.json', group, PREFIX + 'peer_group')
    members, dispositions, possible = [], [], []
    for member, originals in sources.items():
        _, entry, entry_ref = originals[0]
        entry_refs = _deduplicate([original[2] for original in originals])
        code = identities[member]['fund_code'] if identities[member] and is_known(identities[member]['fund_code']) else entry['original_code']
        members.append({'member_id': member, 'entity_id': identities[member]['entity_id'] if identities[member] else unknown('Legal entity is unverified'),
                        'fund_code': code, 'source_entry_refs': entry_refs, 'peer_group_refs': list(final_group_refs.values()),
                        'identity_ref': known(identity_refs[member]) if member in identity_refs else unknown('No supported legal/share identity'),
                        'first_observed_at': min((discovery.captures[original[1]['raw_ref']['uri']][1]['captured_at'] for original in originals), key=instant)})
        dispositions.append({'member_id': member, 'status': statuses[member], 'eligibility_rule_ref': refs['eligibility_rule_ref'],
                             'predicate_results': predicates[member], 'evidence_refs': [identity_refs[member]] if member in identity_refs else [entry_ref],
                             'reason': 'Registered three-valued eligibility and original peer relationships'})
        for group_id, group in groups.items():
            if member in local[group_id]['unknown']:
                rule = ctx.resolver.resolve(group['peer_rule_ref'], PREFIX + 'peer_rule')
                possible.append({'member_id': member, 'peer_group_id': group_id, 'unresolved_dimensions': rule['dimensions'],
                                 'source_entry_refs': entry_refs, 'reason': 'Available identity, peer relationship or eligibility remains unresolved'})
    funding = ctx.resolver.resolve(refs['funding_plan_ref'], 'investment.funding_plan') if 'funding_plan_ref' in refs else None
    risks = mandatory_assets(account, positions, pending, funding=funding, funding_ref=refs.get('funding_plan_ref'),
                             parameters_ref=ctx.input['parameters_ref'], requested_uses=parameters['requested_uses'], channel=spec['channel'],
                             identities={member: identity if identity is not None and available_at(identity, clock['current_at']) else None for member, identity in identities.items()})
    tables = {'members_ref': _table(ctx, 'members', members), 'peer_groups_ref': _table(ctx, 'peer_groups', list(groups.values())),
              'exposure_links_ref': _table(ctx, 'exposures', exposures), 'mandatory_assets_ref': _table(ctx, 'mandatory_assets', risks)}
    for status, field in [('candidate', 'candidate_members_ref'), ('excluded', 'excluded_members_ref'), ('unknown', 'unknown_members_ref')]:
        tables[field] = _table(ctx, 'dispositions', [row for row in dispositions if row['status'] == status], status + '-members')
    history_ref = known(_table(ctx, 'history', history.rows)) if clock['historical_origins'] else na('Historical origins were not requested')
    universe_id = identity_key('universe', ctx.input_ref, ctx.attempt_no)
    snapshot = ctx.put_json('universe-snapshot.json', {'universe_id': universe_id, 'universe_at': clock['current_at'],
                            'discovery_spec_ref': refs['universe_discovery_spec_ref'], 'members_ref': tables['members_ref'], 'catalog_vintage_ref': known(catalog_ref)}, PREFIX + 'universe_snapshot')
    audit_tables = {'query_tasks_ref': task_ref, 'attempts_ref': attempt_ref,
                    'membership_provenance_ref': _table(ctx, 'membership_provenance', provenance),
                    'possible_members_ref': _table(ctx, 'possible_members', possible), 'unprocessed_scope_ref': _table(ctx, 'unprocessed_scope', scopes)}
    gaps = _deduplicate(gaps)
    complete = catalog_ready and source_ready and tasks_ready and all(group['completeness_status'] == 'complete' for group in groups.values())
    audit = {'audit_id': identity_key('audit', universe_id), 'run_id': ctx.run_id, 'universe_ref': snapshot,
             'scope_ref': ctx.input['scope_ref'], 'discovery_spec_ref': refs['universe_discovery_spec_ref'], 'original_list_refs': discovery.original_lists,
             **audit_tables, 'closure_checks': checks, 'completeness_status': 'complete' if complete else 'unknown', 'gaps': gaps}
    audit_ref = ctx.put_json('discovery-audit.json', audit, PREFIX + 'discovery_audit')
    subject_dispositions = [{'subject_id': row['subject_id'], 'disposition': 'covered' if source_ready and tasks_ready and all(group['completeness_status'] == 'complete' for group in groups.values() if row['subject_id'] in group['subject_ids']) else 'unknown',
                             'task_ids': [task['task_id'] for task in discovery.tasks.values() if row['subject_id'] in task['subject_ids'] and task['requested_origin'] == clock['current_at']],
                             'rule_ref': next(group['peer_rule_ref'] for group in groups.values() if row['subject_id'] in group['subject_ids']),
                             'citations': _deduplicate([citation for group in groups.values() if row['subject_id'] in group['subject_ids'] for citation in group['comparison_citations']]),
                             'reason': 'Actual original-source and peer classification coverage at the frozen origin'} for row in spec['required_subjects']]
    coverage = {'coverage_id': identity_key('coverage', universe_id), 'source_coverage': source_coverage,
                'required_subject_ids': [row['subject_id'] for row in spec['required_subjects']],
                'covered_subject_ids': [row['subject_id'] for row in subject_dispositions if row['disposition'] == 'covered'],
                'unresolved_subject_ids': [row['subject_id'] for row in subject_dispositions if row['disposition'] == 'unknown'],
                'subject_dispositions': subject_dispositions, 'current_status': 'complete' if complete else 'unknown',
                'historical_status': ('complete' if len(history.complete_origins) == len(clock['historical_origins']) else 'unknown') if clock['historical_origins'] else 'not_applicable',
                'historical_origin_status': history.statuses, 'limitations': ['Captured listings do not alone establish prior availability or complete cross-issuer peer comparison'], 'gaps': gaps}
    coverage_ref = ctx.put_json('coverage.json', coverage, PREFIX + 'coverage')
    payload = {'universe_id': universe_id, 'universe_at': clock['current_at'], 'universe_rule_ref': refs['universe_discovery_spec_ref'], **tables,
               'catalog_vintage_ref': known(catalog_ref), 'discovery_audit_ref': audit_ref, 'universe_history_ref': history_ref, 'coverage_ref': coverage_ref}
    payload_ref = ctx.put_json('instrument-universe.json', payload, PREFIX + 'instrument_universe')
    return {'contract_id': 'investment.candidate_output', 'contract_version': 1, 'run_id': ctx.run_id, 'stage_id': 'SC04',
            'output_id': identity_key('output', universe_id), 'created_at': now(), 'processing_state': 'completed',
            'input_refs': ctx.input['input_refs'], 'scope_ref': ctx.input['scope_ref'], 'method_ref': ctx.method_ref,
            'payload_ref': payload_ref, 'datasets': [*tables.values(), *audit_tables.values(), *([history_ref['value']] if is_known(history_ref) else [])],
            'evidence_refs': [snapshot, catalog_ref, audit_ref, coverage_ref], 'gaps': gaps}
