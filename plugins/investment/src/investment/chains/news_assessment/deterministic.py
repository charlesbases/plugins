"""SC03确定性引用、图、时间、原值与全集核验。"""
from __future__ import annotations

from collections import deque
from investment.contracts import canonical, instant, require, now

from .contracts import ACCEPTED, KINDS, TABLES, number_literals, time_bounds


def acyclic(edges, rule, path):
    degree = {key: 0 for key in edges}
    for children in edges.values():
        for child in children:
            require(child in edges, rule, path, 'Graph points outside the current set')
            degree[child] += 1
    queue = deque(key for key, value in degree.items() if value == 0)
    count = 0
    while queue:
        node = queue.popleft()
        count += 1
        for child in edges[node]:
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    require(count == len(edges), rule, path, 'Evidence graph contains a cycle')


def inputs(snapshot):
    ctx, payload = snapshot.ctx, snapshot.payload
    candidate = ctx.candidate
    require(candidate['input_refs'] == ctx.input['input_refs'] and candidate['scope_ref'] == ctx.input['scope_ref']
            and candidate['method_ref'] == ctx.method_ref and candidate['run_id'] == ctx.run_id
            and candidate['stage_id'] == 'SC03', 'A01', '/candidate', 'Candidate differs from frozen input')
    roles = {item['role']: item['ref'] for item in ctx.input['input_refs']}
    for key in ('assessment_protocol_ref', 'analyst_identity_ref'):
        require(payload[key] == roles[key] == snapshot.parameters[key], 'A01', '/' + key, 'Protocol or identity binding differs')
    questions = snapshot.protocol['questions']
    require(len({row['question_id'] for row in questions}) == len(questions), 'A01', '/questions', 'Duplicate research question')
    allowed_basis = [ctx.input['scope_ref']]
    if 'account_risk_objects_ref' in roles:
        allowed_basis.append(roles['account_risk_objects_ref'])
    for question in questions:
        require(all(ref in allowed_basis for ref in question['basis_refs']), 'A01', '/questions', 'Question basis is outside task scope/account')
        require(question['basis'] != 'account_risk' or 'account_risk_objects_ref' in roles,
                'A01', '/questions', 'Account-risk question lacks an account input')
    require(snapshot.identity['role'] == 'producer', 'A01', '/analyst_identity_ref', 'Input analyst is not producer')


def references(snapshot):
    expected = [snapshot.payload[spec[0]] for spec in TABLES.values()]
    actual = snapshot.ctx.candidate['datasets']
    require(sorted(map(canonical, actual)) == sorted(map(canonical, expected)),
            'A02', '/datasets', 'Candidate datasets differ from the six exact business tables')
    for name, rows in snapshot.tables.items():
        for row in rows:
            for field in ('fact_ids', 'supporting_fact_ids', 'related_fact_ids'):
                if field in row:
                    snapshot.facts_exist(row[field], 'A02', '/' + name + '/' + field)
    for row in snapshot.tables['counterevidence']:
        collection = KINDS[row['target_kind']]
        require(row['target_id'] in snapshot.indexes[collection], 'A02', '/counterevidence/target_id', 'Counterevidence target does not exist')
    for row in snapshot.tables['dispositions']:
        require(set(row['hypothesis_ids']) <= set(snapshot.indexes['hypotheses']),
                'A02', '/dispositions/hypothesis_ids', 'Unknown hypothesis')
        require(set(row['document_version_ids']) <= set(snapshot.documents),
                'A02', '/dispositions/document_version_ids', 'Document outside corpus')


def quotes_and_times(snapshot):
    for row in snapshot.tables['facts']:
        for quote in row['quote_locators']:
            snapshot.quote(quote)
        require(set(row['document_version_ids']) == {q['document_version_id'] for q in row['quote_locators']},
                'A03', '/facts/document_version_ids', 'Fact versions differ from its exact quotes')
        time_bounds(row['event_time'])
        known = time_bounds(row['known_time'])
        available = [snapshot.documents[key]['available_time'] for key in row['document_version_ids']
                     if snapshot.documents[key]['available_time']['status'] == 'known']
        if available:
            first = min(available, key=lambda item: time_bounds(item)[0])
            require(known == time_bounds(first), 'A03', '/facts/known_time', 'Fact does not retain first proven corpus availability')
            require(row['known_time']['value']['basis_ref'] == first['value']['basis_ref'],
                    'A03', '/facts/known_time', 'Availability evidence identity changed')
        else:
            require(row['known_time']['status'] == 'unknown', 'A03', '/facts/known_time', 'Availability was invented')
    for row in snapshot.tables['counterevidence']:
        for quote in row['quote_locators']:
            snapshot.quote(quote)
    for row in snapshot.tables['observations']:
        snapshot.quote(row['quote_locator'])
        require(row['document_version_id'] == row['quote_locator']['document_version_id'],
                'A03', '/observations/document_version_id', 'Observation quote version differs')
        require(time_bounds(row['available_time']) == time_bounds(snapshot.documents[row['document_version_id']]['available_time']),
                'A03', '/observations/available_time', 'Observation availability differs from upstream source')
        time_bounds(row['economic_period'])


def support_graph(snapshot):
    facts = snapshot.indexes['facts']
    edges = {key: [] for key in facts}
    for key, row in facts.items():
        support = {canonical(link) for link in row['support_refs']}
        conflict = {canonical(link) for link in row['conflict_refs']}
        require(not support & conflict, 'A04', '/facts/' + key, 'The same edge is support and conflict')
        for field in ('support_refs', 'conflict_refs'):
            for link in row[field]:
                known = snapshot.quotes if link['kind'] == 'quote' else facts
                require(link['target_id'] in known, 'A04', '/facts/' + key + '/' + field, 'Dangling evidence edge')
                if link['kind'] == 'fact':
                    require(link['target_id'] != key, 'A04', '/facts/' + key, 'Self-reference cannot support or contradict itself')
                    if field == 'support_refs':
                        edges[key].append(link['target_id'])
        if row['verification_status'] in ACCEPTED:
            require(row['support_refs'], 'A04', '/facts/' + key, 'Accepted fact has no support path')
    acyclic(edges, 'A04', '/facts/support_refs')
    for root, row in facts.items():
        seen, pending, leaves = set(), [root], set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            for link in facts[key]['support_refs']:
                if link['kind'] == 'fact':
                    require(facts[key]['verification_status'] not in ACCEPTED or facts[link['target_id']]['verification_status'] in ACCEPTED,
                            'A04', '/facts/' + key, 'Accepted fact relies on unknown/disputed supporting fact')
                    pending.append(link['target_id'])
                else:
                    leaves.add(link['target_id'])
        if row['support_refs']:
            require(leaves, 'A04', '/facts/' + root, 'Support graph has no original quote leaf')
        require({snapshot.quotes[key]['document_version_id'] for key in leaves} <= set(row['document_version_ids']),
                'A04', '/facts/' + root, 'Fact omits support source version')


def events(snapshot):
    seen = set()
    for row in snapshot.tables['events']:
        path = '/events/' + row['event_id']
        require(row['event_id'] not in row['possible_same_event_ids']
                and set(row['possible_same_event_ids']) <= set(snapshot.indexes['events']),
                'A07', path, 'Invalid possible-event relation')
        time_bounds(row['event_time'])
        identity = canonical(sorted(row['fact_ids']))
        require(identity not in seen or row['identity_status'] == 'unresolved',
                'A07', path, 'Resolved duplicate event reuses the same fact set')
        seen.add(identity)


def observations(snapshot):
    for row in snapshot.tables['observations']:
        path = '/observations/' + row['observation_id']
        quote = row['quote_locator']['quote']
        if row['raw_value']['status'] == 'known':
            require(row['raw_value']['value'] in number_literals(quote),
                    'A08', path + '/raw_value', 'Exact source number/precision was not found in the independent quote')
        for field in ('unit', 'measurement_basis', 'release_vintage'):
            require(row[field]['status'] != 'not_applicable', 'A08', path + '/' + field, 'Missing measurement metadata is unknown, not inapplicable')
        if row['unit']['status'] == 'known':
            value = row['unit']['value'].casefold()
            aliases = {'percent': ('percent', '%'), 'percentage_point': ('percentage point', '百分点'),
                       'percentage points': ('percentage point', '百分点')}
            require(any(token in quote.casefold() for token in aliases.get(value, (value,))),
                    'A08', path + '/unit', 'Unit is not stated in the observation quote')
        if row['currency']['status'] == 'known':
            value = row['currency']['value']
            aliases = {'USD': ('USD', 'U.S. dollar', 'US dollar'), 'CNY': ('CNY', '人民币', 'RMB')}
            require(any(token.casefold() in quote.casefold() for token in aliases.get(value, (value,))),
                    'A08', path + '/currency', 'Currency is not explicit in the observation quote')


def mechanisms(snapshot):
    for row in snapshot.tables['hypotheses']:
        path = '/hypotheses/' + row['hypothesis_id']
        require(len({step['step_id'] for step in row['mechanism']}) == len(row['mechanism']),
                'A09', path, 'Duplicate mechanism step')
        require(len({item['exposure_id'] for item in row['affected_exposures']}) == len(row['affected_exposures']),
                'A09', path, 'Duplicate exposure')
        for step in row['mechanism']:
            snapshot.facts_exist(step['supporting_fact_ids'], 'A09', path)
            require(set(step['supporting_fact_ids']) <= set(row['supporting_fact_ids']),
                    'A09', path, 'Step support is absent from hypothesis support')
            require(step['status'] != 'source_supported' or step['supporting_fact_ids'],
                    'A09', path, 'Source-supported step has no fact support')
            require(step['status'] != 'conditional' or step['assumptions'],
                    'A09', path, 'Conditional mechanism has no explicit assumptions')
            require(step['status'] != 'unsupported' or row['research_status'] == 'exploratory',
                    'A09', path, 'Unsupported mechanism was presented as testable')
        for exposure in row['affected_exposures']:
            snapshot.facts_exist(exposure['supporting_fact_ids'], 'A09', path)
        horizon = row['horizon']
        if horizon['status'] == 'known':
            require(horizon['min_days'] <= horizon['max_days'], 'A09', path + '/horizon', 'Reversed horizon')
            snapshot.facts_exist(horizon['basis_fact_ids'], 'A09', path)
        require(row['expected_direction']['direction'] == 'unknown' or row['expected_direction']['conditions'],
                'A09', path + '/expected_direction', 'Directional hypothesis has no stated conditions')


def coverage(snapshot):
    rows = snapshot.tables['dispositions']
    questions = {row['question_id'] for row in snapshot.protocol['questions']}
    require(len(rows) == len(questions) and {row['question_id'] for row in rows} == questions,
            'A10', '/dispositions', 'Every frozen question must have exactly one disposition')
    global_gaps = snapshot.payload['unresolved_questions']
    require(snapshot.ctx.candidate['gaps'] == global_gaps, 'A10', '/gaps',
            'Candidate gaps must exactly preserve assessment unresolved questions')
    for row in rows:
        require(all(gap in global_gaps for gap in row['gaps']), 'A10', '/unresolved_questions', 'Question gap omitted from assessment')
        require(row['status'] not in ('partial', 'unresolved', 'no_relevant_material') or row['gaps'],
                'A10', '/dispositions', 'Unresolved question lacks an explicit gap')
        if not snapshot.documents:
            require(row['status'] in ('no_relevant_material', 'unresolved') and row['gaps'],
                    'A10', '/dispositions', 'Empty corpus cannot answer a substantive question')
    require(all(gap in global_gaps for gap in snapshot.inherited_gaps),
            'A10', '/unresolved_questions', 'Verified upstream receipt blockers must be preserved unchanged')
    if not snapshot.documents:
        require(not snapshot.pending and snapshot.coverage
                and all(row['window_coverage'] == 'complete' and row['status'] == 'completed' for row in snapshot.coverage),
                'A10', '/upstream/coverage', 'Empty corpus has no independently verified complete empty window')


def identity_limits(snapshot):
    identity = snapshot.identity
    gaps = snapshot.payload['unresolved_questions']
    for field in ('provider', 'model_name', 'model_version', 'training_cutoff'):
        require(identity[field]['status'] != 'not_applicable', 'A13', '/analyst_identity/' + field, 'Host model identity cannot be inapplicable')
    restricted_paths = {gap['field_or_object'] for gap in gaps if 'pit_training' in gap['affected_uses']}
    unknown_metadata = {'/analyst_identity/' + field for field in ('provider', 'model_name', 'model_version')
                        if identity[field]['status'] == 'unknown'}
    if unknown_metadata:
        require(bool(restricted_paths & (unknown_metadata | {'/analyst_identity'})),
                'A13', '/analyst_identity', 'Unknown model identity restriction was omitted')
    if identity['training_cutoff']['status'] == 'unknown':
        require(bool(restricted_paths & {'/analyst_identity', '/analyst_identity/training_cutoff'}),
                'A13', '/analyst_identity/training_cutoff', 'Unknown training time restriction was omitted')
    require(instant(identity['generated_at']) <= instant(now()), 'A13', '/analyst_identity/generated_at', 'Identity was generated in the future')
    if snapshot.protocol['interpretation_mode'] == 'historical_exploration':
        require(any('pit_training' in gap['affected_uses'] for gap in gaps),
                'A13', '/unresolved_questions', 'Historical model contamination limitation was omitted')


def invocation(snapshot):
    ctx = snapshot.ctx
    require(1 <= ctx.attempt_no <= 3 and ctx.input_ref['schema_id'] == 'investment.stage_input',
            'A14', '/attempt_no', 'Current bounded invocation is required')
    require(instant(ctx.input['decision_at']) <= instant(ctx.candidate['created_at']) <= instant(now()),
            'A14', '/candidate/created_at', 'Candidate time is outside current invocation')
