"""独立语义工作分派与响应核验；模型结论不替代原文和关系检查。"""
from __future__ import annotations

from collections import defaultdict
from investment.contracts import canonical, digest, require, unknown, now

from .contracts import KINDS, ACCEPTED, pointer, target_key
from .deterministic import acyclic
from . import reporting


def targets(snapshot):
    result = {}
    def add(kind, identity, path, dimension):
        key = target_key(kind, identity, path, dimension)
        result[key] = (kind, identity, path, dimension)
    for fact in snapshot.tables['facts']:
        for dimension in ('atomicity', 'entailment', 'contradiction'):
            add('fact', fact['fact_id'], '/assertion', dimension)
        for field, dimension in (('support_refs', 'entailment'), ('conflict_refs', 'contradiction')):
            for index in range(len(fact[field])):
                add('fact', fact['fact_id'], '/' + field + '/' + str(index), dimension)
    for row in snapshot.tables['counterevidence']:
        add('counterevidence', row['counterevidence_id'], '/relation', 'contradiction')
    for row in snapshot.tables['events']:
        add('event', row['event_id'], '/identity_status', 'event_identity')
    for row in snapshot.tables['observations']:
        add('observation', row['observation_id'], '/raw_value', 'measurement_scope')
    for row in snapshot.tables['hypotheses']:
        for field, dimension in (('mechanism', 'mechanism_step'), ('affected_exposures', 'exposure'),
                                 ('alternative_explanations', 'alternative'), ('falsification_conditions', 'falsifiability')):
            for index in range(len(row[field])):
                add('hypothesis', row['hypothesis_id'], '/' + field + '/' + str(index), dimension)
    for row in snapshot.tables['dispositions']:
        add('disposition', row['disposition_id'], '/status', 'coverage')
    result.update(reporting.gap_keys(snapshot))
    return result


def work_key(scope):
    return digest(canonical(scope))


def work_item(step, spans, questions, keys, dependencies=()):
    scope = {'step_id': step, 'document_spans': sorted(spans, key=lambda row: (row['document_version_id'], row['text_field'], row['start'])),
             'question_ids': sorted(set(questions)), 'target_keys': sorted(set(keys)),
             'related_work_keys': sorted(set(dependencies))}
    return dict(scope, work_key=work_key(scope))


def pack_work(items, budget):
    """只合并能容纳的完整工作项，不删除窗口或目标。"""
    packed, group = [], []
    def merged(rows):
        spans = {canonical(span): span for row in rows for span in row['document_spans']}
        return work_item(rows[0]['step_id'], list(spans.values()),
                         [key for row in rows for key in row['question_ids']],
                         [key for row in rows for key in row['target_keys']],
                         [key for row in rows for key in row['related_work_keys']])
    for item in items:
        trial = merged(group + [item])
        cost = sum(span['end'] - span['start'] for span in trial['document_spans']) + sum(len(key) for key in trial['target_keys'])
        if group and cost > budget:
            packed.append(merged(group))
            group = []
        group.append(item)
    if group:
        packed.append(merged(group))
    return packed


def plan(snapshot):
    budget = snapshot.parameters['text_window_codepoints']
    questions = [row['question_id'] for row in snapshot.protocol['questions']]
    expected = targets(snapshot)
    remaining = set(expected)
    source_items = []
    for version, body in sorted(snapshot.bodies.items()):
        windows = []
        for field in ('text', 'title'):
            windows.extend({'document_version_id': version, 'text_field': field, 'start': start,
                            'end': min(start + budget, len(body[field]))}
                           for start in range(0, len(body[field]), budget))
        for span in windows:
            keys = []
            for fact in snapshot.tables['facts']:
                if fact['document_version_ids'] != [version]:
                    continue
                if any(q['text_field'] == span['text_field'] and span['start'] <= q['start'] and q['end'] <= span['end']
                       for q in fact['quote_locators']):
                    for dimension in ('atomicity', 'entailment'):
                        key = target_key('fact', fact['fact_id'], '/assertion', dimension)
                        if key in remaining:
                            keys.append(key)
                            remaining.remove(key)
            source_items.append(work_item('review_sources', [span], questions, keys))
    if not source_items:
        empty_keys = [key for key, value in expected.items() if value[0] == 'disposition']
        remaining -= set(empty_keys)
        source_items.append(work_item('review_sources', [], questions, empty_keys))
    source_items = pack_work(source_items, budget)
    relation_items = []
    for key in sorted(remaining):
        kind, identity, _, _ = expected[key]
        if kind == 'reporting_gap':
            continue
        row = snapshot.indexes[KINDS[kind]][identity]
        fact_ids = row.get('supporting_fact_ids', row.get('fact_ids', row.get('related_fact_ids', [])))
        if kind == 'fact':
            fact_ids = [identity]
        quotes = [quote for fact_id in fact_ids for quote in snapshot.indexes['facts'][fact_id]['quote_locators']]
        if kind == 'counterevidence':
            quotes += row['quote_locators']
            target = snapshot.indexes[KINDS[row['target_kind']]][row['target_id']]
            quotes += target.get('quote_locators', [])
            if row['target_kind'] == 'observation':
                quotes.append(target['quote_locator'])
            for fact_id in target.get('supporting_fact_ids', []):
                quotes += snapshot.indexes['facts'][fact_id]['quote_locators']
        if kind == 'observation':
            # 同口径冲突可能藏在其他观测，保留全部观测源定位供独立比较。
            quotes += [item['quote_locator'] for item in snapshot.tables['observations']]
        quotes += [quote for item in snapshot.tables['counterevidence'] if item['target_id'] == identity
                   for quote in item['quote_locators']]
        spans = {canonical({k: q[k] for k in ('document_version_id', 'text_field', 'start', 'end')}):
                 {k: q[k] for k in ('document_version_id', 'text_field', 'start', 'end')} for q in quotes}
        dependencies = [item['work_key'] for item in source_items]
        relation_items.append(work_item('review_relations', list(spans.values()), questions, [key], dependencies))
    for identity in sorted(reporting.local_gaps(snapshot)):
        keys = [key for key in sorted(remaining) if expected[key][0] == 'reporting_gap' and expected[key][1] == identity]
        relation_items.append(work_item('review_relations', [], questions, keys,
                                       [item['work_key'] for item in source_items]))
    items = source_items + pack_work(relation_items, budget)
    require(len({item['work_key'] for item in items}) == len(items), 'A12', '/work_plan', 'Duplicate work scope')
    allocated = [key for item in items for key in item['target_keys']]
    require(len(allocated) == len(expected) and set(allocated) == set(expected),
            'A12', '/work_plan', 'Work plan omits or repeats a required target')
    return {'plan_id': 'review-' + digest(canonical(items))[:40], 'input_ref': snapshot.ctx.input_ref,
            'text_window_codepoints': budget, 'items': items}


def reviewer_identity(snapshot):
    ctx = snapshot.ctx
    path = ctx.attempt_dir / 'validation-reviewer.json'
    if path.exists():
        reference = ctx.resolver.path_ref(path, 'investment.sc03.analyst_identity')
        value = ctx.resolver.resolve(reference, 'investment.sc03.analyst_identity')
        require(value['role'] == 'semantic_validator' and value['host_invocation_id'] == ctx.invocation_id,
                'A11', '/reviewer_identity', 'Persisted reviewer identity differs from this invocation')
        return reference
    value = {'identity_id': 'semantic-reviewer-' + ctx.invocation_id[:80], 'role': 'semantic_validator',
             'provider': unknown('host_identity_not_available_before_actual_invocation'),
             'model_name': unknown('host_identity_not_available_before_actual_invocation'),
             'model_version': unknown('host_identity_not_available_before_actual_invocation'),
             'training_cutoff': unknown('verified_training_cutoff_not_available'),
             'identity_evidence_refs': [], 'generated_at': now(),
             'host_invocation_id': ctx.invocation_id}
    return ctx.put_json('validation-reviewer.json', value, 'investment.sc03.analyst_identity')


def request_or_reviews(snapshot, checks):
    ctx = snapshot.ctx
    work = plan(snapshot)
    plan_ref = ctx.put_json('validation-work-plan.json', work, 'investment.sc03.assessment_work_plan')
    identity_ref = reviewer_identity(snapshot)
    role_refs = [item['ref'] for item in ctx.input['input_refs']]
    base = [plan_ref, identity_ref, ctx.candidate_ref, ctx.candidate['payload_ref'], ctx.input['parameters_ref']]
    base += [reference for reference, _ in snapshot.upstream_receipts.values()]
    base += role_refs + ctx.candidate['datasets'] + [snapshot.corpus['documents_ref'], snapshot.corpus['coverage_ref'], snapshot.corpus['pending_items_ref']]
    reviews, output_refs = [], []
    for item in work['items']:
        references = base + [snapshot.documents[span['document_version_id']]['body_ref'] for span in item['document_spans']]
        if item['step_id'] == 'review_relations':
            references += output_refs + [row['body_ref'] for row in snapshot.documents.values()]
        references = list({ref['uri']: ref for ref in references}.values())
        response = ctx.host_response(item['step_id'], work_key=item['work_key'])
        if response is None:
            return ctx.request_host(item['step_id'], references, work_key=item['work_key']), None
        validate_batch(snapshot, response, item, identity_ref)
        reviews.append((item, response))
        matches = [ref for ref in ctx.resolver.host_output_refs if
                   ref['uri'].endswith('host-' + item['step_id'] + '-' + item['work_key'] + '-output.json')]
        require(len(matches) == 1, 'A11', '/host_response', 'Semantic response lacks actual bound output identity')
        output_refs.append(matches[0])
    combined = combine(snapshot, reviews, identity_ref)
    review_ref = ctx.put_json('validation-semantic-review.json', combined, 'investment.sc03.semantic_review')
    checks.set('A11', reason='Current input/candidate, exact work keys and actual host response bindings checked; actual calls require host audit evidence')
    checks.set('A12', reason='All independently enumerated targets and all source windows have exact bound findings')
    return None, (combined, [plan_ref, identity_ref, review_ref] + output_refs)


def validate_batch(snapshot, review, item, identity_ref):
    ctx = snapshot.ctx
    require(review['input_ref'] == ctx.input_ref and review['candidate_ref'] == ctx.candidate_ref
            and review['reviewer_identity_ref'] == identity_ref,
            'A11', '/semantic_review', 'Semantic output does not bind current input/candidate/reviewer')
    gap_targets = reporting.gap_keys(snapshot)
    assigned = set(item['target_keys']) & set(gap_targets)
    wanted = {gap_targets[key][1] for key in assigned}
    require(item['step_id'] == 'review_relations' or not wanted,
            'A12', '/reporting_gap_reviews', 'Source batches cannot decide reporting gaps')
    require(all(key in assigned for key, target in gap_targets.items() if target[1] in wanted),
            'A12', '/work_plan', 'Duplicate gap indices were split across batches')
    reporting.validate_reviews(snapshot, review['reporting_gap_reviews'], wanted)
    expected = set(item['target_keys']) - assigned
    actual = [target_key(row['target_kind'], row['target_id'], row['target_pointer'], row['dimension']) for row in review['findings']]
    require(len(actual) == len(expected) and set(actual) == expected,
            'A12', '/findings', 'Batch findings omit, duplicate or invent assigned targets')
    versions = {span['document_version_id'] for span in item['document_spans']}
    for field in ('source_lineage', 'document_reviews'):
        rows = review[field]
        wanted = versions if item['step_id'] == 'review_sources' else set()
        require(len(rows) == len(wanted) and {row['document_version_id'] for row in rows} == wanted,
                'A12', '/' + field, 'Source batch does not cover its exact document set')
    if item['step_id'] == 'review_sources':
        used_quotes = [q for finding in review['findings'] for q in finding['source_quotes']]
        used_quotes += [q for field in ('source_lineage', 'document_reviews') for row in review[field] for q in row['basis_quotes']]
        for quote in used_quotes:
            require(any(quote['document_version_id'] == span['document_version_id'] and quote['text_field'] == span['text_field']
                        and span['start'] <= quote['start'] < quote['end'] <= span['end'] for span in item['document_spans']),
                    'A12', '/source_quotes', 'Source review cites material outside its assigned original window')
    for finding in review['findings']:
        collection = KINDS[finding['target_kind']]
        row = snapshot.indexes[collection][finding['target_id']]
        pointer(row, finding['target_pointer'])
        for quote in finding['source_quotes']:
            snapshot.quote(quote)
        snapshot.facts_exist(finding['supporting_fact_ids'], 'A12', '/findings/supporting_fact_ids')
        judgment = finding['judgment']
        if judgment in ('supported', 'contradicted'):
            require(finding['source_quotes'] or finding['supporting_fact_ids'],
                    'A12', '/findings', 'Substantive semantic judgment has no actual evidence')
        if judgment == 'insufficient_evidence':
            require(finding['missing_evidence'], 'A12', '/findings/missing_evidence', 'Insufficient judgment hides missing evidence')
        if finding['target_kind'] == 'fact' and finding['dimension'] == 'contradiction':
            require(judgment != 'supported', 'A12', '/findings/judgment', 'Fact contradiction check must distinguish contrary evidence from no contrary evidence')
        if judgment == 'not_applicable':
            require(finding['target_kind'] == 'fact' and finding['dimension'] == 'contradiction' and finding['target_pointer'] == '/assertion', 'A12', '/findings/judgment', 'This target cannot skip semantic review')
    for lineage in review['source_lineage']:
        for quote in lineage['basis_quotes']:
            snapshot.quote(quote)
        require(set(lineage['derived_from_version_ids']) <= set(snapshot.documents),
                'A05', '/source_lineage', 'Source derivation refers outside current corpus')
        if lineage['origin_id']['status'] == 'known':
            require(lineage['basis_quotes'] and lineage['relationship'] != 'unknown',
                    'A05', '/source_lineage', 'Known original source has no source-grounded lineage evidence')
    for document in review['document_reviews']:
        require(set(document['question_ids']) <= {q['question_id'] for q in snapshot.protocol['questions']},
                'A10', '/document_reviews', 'Review invents a research question')
        snapshot.facts_exist(document['fact_ids'], 'A10', '/document_reviews')
        for quote in document['basis_quotes']:
            snapshot.quote(quote)
        require(document['assessment'] not in ('missing_claims', 'unreadable'),
                'A10', '/document_reviews', 'Independent source review found missing claims or unreadable material')


def combine(snapshot, reviews, identity_ref):
    lineages, documents, findings = defaultdict(list), defaultdict(list), []
    reporting_reviews = []
    for _, review in reviews:
        findings.extend(review['findings'])
        reporting_reviews.extend(review['reporting_gap_reviews'])
        for row in review['source_lineage']:
            lineages[row['document_version_id']].append(row)
        for row in review['document_reviews']:
            documents[row['document_version_id']].append(row)
    require(set(lineages) == set(snapshot.documents) == set(documents),
            'A10', '/document_reviews', 'Whole corpus was not independently reviewed')
    merged_lineages, merged_documents = [], []
    for version in sorted(snapshot.documents):
        options = lineages[version]
        known = [row for row in options if row['origin_id']['status'] == 'known']
        identities = {canonical([row['origin_id'], row['relationship'], sorted(row['derived_from_version_ids'])]) for row in known}
        require(len(identities) <= 1, 'A05', '/source_lineage/' + version, 'Independent windows disagree on source origin')
        selected = dict(known[0] if known else options[0])
        selected['basis_quotes'] = list({q['quote_id']: q for row in options for q in row['basis_quotes']}.values())
        merged_lineages.append(selected)
        rows = documents[version]
        merged = {'document_version_id': version, 'question_ids': sorted({key for row in rows for key in row['question_ids']}),
                  'fact_ids': sorted({key for row in rows for key in row['fact_ids']}),
                  'assessment': 'covered' if any(row['assessment'] == 'covered' for row in rows) else 'irrelevant',
                  'basis_quotes': list({q['quote_id']: q for row in rows for q in row['basis_quotes']}.values()),
                  'reason': 'All exact source windows were independently reviewed; their source quotations are retained.'}
        expected = {row['fact_id'] for row in snapshot.tables['facts'] if version in row['document_version_ids']}
        require(expected <= set(merged['fact_ids']), 'A10', '/document_reviews/' + version, 'Document review omitted candidate facts from that version')
        require(not expected or merged['assessment'] == 'covered', 'A10', '/document_reviews/' + version, 'A source with accepted candidate facts was marked irrelevant')
        merged_documents.append(merged)
    reporting.validate_reviews(snapshot, reporting_reviews, reporting.local_gaps(snapshot))
    return {'review_id': 'review-' + digest(canonical([findings, reporting_reviews]))[:40], 'input_ref': snapshot.ctx.input_ref,
            'candidate_ref': snapshot.ctx.candidate_ref, 'reviewer_identity_ref': identity_ref,
            'source_lineage': merged_lineages, 'document_reviews': merged_documents, 'findings': findings,
            'reporting_gap_reviews': reporting_reviews}


def source_groups(snapshot, review):
    rows = {row['document_version_id']: row for row in review['source_lineage']}
    acyclic({key: row['derived_from_version_ids'] for key, row in rows.items()}, 'A05', '/source_lineage')
    parents = {key: key for key in rows}
    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key
    def union(left, right):
        parents[root(left)] = root(right)
    for key, row in rows.items():
        for parent in row['derived_from_version_ids']:
            union(key, parent)
    by_origin, by_publisher = {}, {}
    for key, row in rows.items():
        publisher = snapshot.documents[key]['publisher_id']
        if publisher in by_publisher:
            union(key, by_publisher[publisher])
        by_publisher[publisher] = key
        if row['origin_id']['status'] == 'known':
            origin = row['origin_id']['value']
            if origin in by_origin:
                union(key, by_origin[origin])
            by_origin[origin] = key
    return {key: root(key) for key, row in rows.items() if row['origin_id']['status'] == 'known'}


def semantic_rules(snapshot, review, checks):
    judgments = {target_key(row['target_kind'], row['target_id'], row['target_pointer'], row['dimension']): row
                 for row in review['findings']}
    groups = source_groups(snapshot, review)
    checks.set('A05')
    checks.set('A06')
    for fact in snapshot.tables['facts']:
        key, state = fact['fact_id'], fact['verification_status']
        get = lambda dim: judgments[target_key('fact', key, '/assertion', dim)]['judgment']
        atom, entail, conflict = get('atomicity'), get('entailment'), get('contradiction')
        if atom == 'contradicted' or entail == 'contradicted':
            checks.fail('A06', '/facts/' + key, 'Independent original-source reading rejects the candidate assertion')
        elif atom == 'insufficient_evidence' or entail == 'insufficient_evidence' or conflict == 'insufficient_evidence':
            if state != 'unknown' or not any(key in gap['field_or_object'] for gap in snapshot.payload['unresolved_questions']):
                checks.fail('A06', '/facts/' + key, 'Unresolved source assertion was not retained as unknown with a specific gap')
        elif conflict == 'contradicted':
            if state != 'disputed' or not fact['conflict_refs']:
                checks.fail('A06', '/facts/' + key, 'Conflicting source evidence was not retained as disputed')
        elif state not in ACCEPTED:
            checks.fail('A06', '/facts/' + key, 'Clear source support was downgraded to an unsupported default status')
        for field, dimension, wanted in (('support_refs', 'entailment', 'supported'), ('conflict_refs', 'contradiction', 'contradicted')):
            for index in range(len(fact[field])):
                judged = judgments[target_key('fact', key, '/' + field + '/' + str(index), dimension)]
                if judged['judgment'] != wanted:
                    checks.fail('A06', '/facts/' + key + '/' + field + '/' + str(index), 'Declared evidence relationship is not independently supported')
        if state == 'cross_checked':
            entailed = judgments[target_key('fact', key, '/assertion', 'entailment')]['source_quotes']
            pending, seen, support_quotes = [key], set(), []
            while pending:
                fact_id = pending.pop()
                if fact_id in seen:
                    continue
                seen.add(fact_id)
                for link in snapshot.indexes['facts'][fact_id]['support_refs']:
                    if link['kind'] == 'fact':
                        pending.append(link['target_id'])
                    else:
                        support_quotes.append(snapshot.quotes[link['target_id']])
            supported_versions = {quote['document_version_id'] for quote in entailed if any(
                quote['document_version_id'] == source['document_version_id'] and quote['text_field'] == source['text_field']
                and source['start'] <= quote['start'] < quote['end'] <= source['end'] for source in support_quotes)}
            origins = {groups[version] for version in supported_versions if version in groups}
            if len(origins) < 2 or conflict != 'not_applicable':
                checks.fail('A05', '/facts/' + key, 'Independent original support groups do not justify cross-check status')
    for finding in review['findings']:
        kind, verdict, identity = finding['target_kind'], finding['judgment'], finding['target_id']
        if kind == 'counterevidence' and verdict != 'supported':
            checks.fail('A06', '/counterevidence/' + identity, 'Counterevidence relationship is not supported by the actual sources')
        if kind == 'event' and verdict != 'supported':
            row = snapshot.indexes['events'][identity]
            if row['identity_status'] != 'unresolved':
                checks.fail('A07', '/events/' + identity, 'Event identity is not supported by independent source review')
        if kind == 'observation' and verdict != 'supported':
            row = snapshot.indexes['observations'][identity]
            if row['raw_value']['status'] == 'known':
                checks.fail('A08', '/observations/' + identity, 'Observed value or its measurement scope is not source-supported')
        if kind == 'hypothesis' and verdict != 'supported':
            row = snapshot.indexes['hypotheses'][identity]
            if row['research_status'] != 'exploratory' or not any(identity in gap['field_or_object'] for gap in snapshot.payload['unresolved_questions']):
                checks.fail('A09', '/hypotheses/' + identity, 'Unsupported mechanism was not limited to exploration with a specific gap')
        if kind == 'disposition' and verdict != 'supported':
            row = snapshot.indexes['dispositions'][identity]
            permitted = verdict == 'insufficient_evidence' and row['status'] in ('partial', 'unresolved', 'no_relevant_material') and row['gaps']
            if not permitted:
                checks.fail('A10', '/dispositions/' + identity, 'Question disposition is inconsistent with independent coverage review')
