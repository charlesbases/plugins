"""SC04 独立验收：原始目录、分区、来源、风险、时点及真实上游许可。"""
from __future__ import annotations

import re
from collections import defaultdict

from investment.contracts import ContractError, canonical, instant, is_known, now, require
from investment.research.references import selected_references

from .contracts import (RULES, availability_at, current_scope_gaps, inherited_gaps, predicate_verdicts,
                        recompute_partitions, verify_mandatory_risks, verify_partition, verify_weight_bounds, verify_historical_exposure)
from .review_work import ReviewWorkPlanner
from .source_validation import capture_bytes, citation_text, json_pointer, numeric_literals, original_directory

PREFIX = 'investment.sc04.'
PAYLOAD_TABLES = {
    'members_ref': 'members', 'candidate_members_ref': 'dispositions', 'excluded_members_ref': 'dispositions',
    'unknown_members_ref': 'dispositions', 'peer_groups_ref': 'peer_groups', 'exposure_links_ref': 'exposures',
    'mandatory_assets_ref': 'mandatory_assets',
}
AUDIT_TABLES = {
    'query_tasks_ref': 'query_tasks', 'attempts_ref': 'attempts', 'membership_provenance_ref': 'membership_provenance',
    'possible_members_ref': 'possible_members', 'unprocessed_scope_ref': 'unprocessed_scope',
}



class Review:
    """所有读取沿冻结引用权限；不创建宽松Resolver或修改候选。"""
    def __init__(self, ctx):
        self.ctx = ctx
        self.refs, self.values, self.checks = {}, {}, {}
        self.upstream, self.inherited, self.directories, self.review_gaps = {}, [], {}, []

    def result(self, rule, verdict, reason=''):
        severity = {'passed': 0, 'not_applicable': 1, 'insufficient_evidence': 2, 'failed': 3}
        previous = self.checks.get(rule)
        if previous is None or severity[verdict] >= severity[previous['verdict']]:
            self.checks[rule] = {'rule_id': rule, 'verdict': verdict, 'reason': reason[:2048],
                                 'evidence_refs': [self.ctx.input_ref, self.ctx.candidate_ref]}

    def run_check(self, rule, function):
        try:
            function()
        except ContractError as error:
            target = error.rule if error.rule in RULES else rule
            self.result(target, 'insufficient_evidence' if error.code == 'missing_evidence' else 'failed', error.reason)
            if target != rule:
                self.result(rule, 'insufficient_evidence', 'Dependent evidence failed: ' + error.reason)
            return False
        if rule not in self.checks:
            self.result(rule, 'passed')
        return True

    def read(self, reference, expected=None):
        require(reference is not None, 'U01', '/input_refs', 'Required business role is absent')
        uri = reference['uri']
        require(not uri.rsplit('/', 1)[-1].startswith('host-'), 'U02', '/references', 'Host private output is not original business evidence')
        if uri in self.refs:
            require(self.refs[uri] == reference and (expected is None or reference['schema_id'] == expected),
                    'U02', '/references', 'Reference metadata or business type differs')
            return self.values[uri]
        value = self.ctx.resolver.resolve(reference, expected)
        self.refs[uri], self.values[uri] = reference, value
        return value

    def role_ref(self, name):
        matches = [row['ref'] for row in self.ctx.input['input_refs'] if row['role'] == name]
        require(len(matches) <= 1, 'U01', '/input_refs', 'Duplicate input role')
        return matches[0] if matches else None

    def follow_grants(self):
        grants = {row['schema_id']: row['reference_paths'] for row in self.ctx.definition['reference_grants']}
        queue, visited = list(self.refs.values()), set()
        while queue:
            reference = queue.pop()
            value = self.read(reference)
            if reference['uri'] in visited or reference['schema_id'] == 'investment.validation_receipt':
                continue
            visited.add(reference['uri'])
            for path in grants.get(reference['schema_id'], ()):
                queue.extend(selected_references(value, path))

    def load(self):
        require(self.ctx.stage_id == 'SC04' and self.ctx.role == 'validator' and self.ctx.candidate is not None,
                'U01', '/candidate', 'Current completed SC04 candidate is required')
        self.account = self.read(self.role_ref('transaction_context_ref'), 'investment.transaction_context')
        self.assessment = self.read(self.role_ref('research_assessment_ref'), 'investment.sc03.research_assessment')
        for role in ('transaction_context_ref', 'research_assessment_ref'):
            reference, receipt = self.ctx.upstream_receipt(role)
            self.upstream[role] = receipt
            self.refs[reference['uri']], self.values[reference['uri']] = reference, receipt
        self.inherited = inherited_gaps(list(self.upstream.values()), self.account, self.assessment)
        self.discovery = self.read(self.role_ref('universe_discovery_spec_ref'), PREFIX + 'discovery_spec')
        self.eligibility = self.read(self.role_ref('eligibility_rule_ref'), PREFIX + 'eligibility_rule')
        self.mapping = self.read(self.role_ref('exposure_mapping_rule_ref'), PREFIX + 'exposure_mapping_rule')
        self.clock = self.read(self.role_ref('scope_time_request_ref'), PREFIX + 'scope_time_request')
        self.parameters = self.read(self.ctx.input['parameters_ref'], PREFIX + 'parameters')
        self.positions = self.read(self.account['positions_ref'], 'investment.positions')['rows']
        self.pending = self.read(self.account['pending_transactions_ref'], 'investment.pending_transactions')['rows']
        self.hypotheses = self.read(self.assessment['hypotheses_ref'], 'investment.sc03.hypotheses')['rows']
        self.payload = self.read(self.ctx.candidate['payload_ref'], PREFIX + 'instrument_universe')
        self.rows = {field: self.read(self.payload[field], PREFIX + name)['rows'] for field, name in PAYLOAD_TABLES.items()}
        self.audit = self.read(self.payload['discovery_audit_ref'], PREFIX + 'discovery_audit')
        self.rows.update({field: self.read(self.audit[field], PREFIX + name)['rows'] for field, name in AUDIT_TABLES.items()})
        self.coverage = self.read(self.payload['coverage_ref'], PREFIX + 'coverage')
        self.history = self.read(self.payload['universe_history_ref']['value'], PREFIX + 'history')['rows'] if is_known(self.payload['universe_history_ref']) else []
        self.funding_ref = self.role_ref('funding_plan_ref')
        self.funding = self.read(self.funding_ref, 'investment.funding_plan') if self.funding_ref else None
        self.follow_grants()
        self.members = {row['member_id']: row for row in self.rows['members_ref']}
        self.groups = {row['peer_group_id']: row for row in self.rows['peer_groups_ref']}
        self.identities = {key: self.read(row['identity_ref']['value'], PREFIX + 'identity') if is_known(row['identity_ref']) else None
                           for key, row in self.members.items()}
        self.current_identities = {key: value if value is not None and availability_at(value['available_time'], value['availability_bound_at'], self.clock['current_at']) else None
                                   for key, value in self.identities.items()}
        self.predicates = {key: predicate_verdicts(value, self.eligibility) for key, value in self.current_identities.items()}

    def binding(self):
        candidate = self.ctx.candidate
        require(candidate['input_refs'] == self.ctx.input['input_refs'] and candidate['scope_ref'] == self.ctx.input['scope_ref']
                and candidate['method_ref'] == self.ctx.method_ref, 'U01', '/input_refs', 'Frozen input, scope or method was changed')
        require(self.payload['universe_rule_ref'] == self.role_ref('universe_discovery_spec_ref')
                and self.audit['discovery_spec_ref'] == self.role_ref('universe_discovery_spec_ref')
                and self.audit['scope_ref'] == self.ctx.input['scope_ref'] and self.audit['run_id'] == self.ctx.run_id,
                'U01', '/discovery_audit_ref', 'Universe audit does not bind the current task')
        snapshot = self.read(self.audit['universe_ref'], PREFIX + 'universe_snapshot')
        require(all(snapshot[field] == self.payload[field] for field in ('universe_id', 'universe_at', 'members_ref', 'catalog_vintage_ref'))
                and snapshot['discovery_spec_ref'] == self.payload['universe_rule_ref'], 'U01', '/universe_ref', 'Snapshot differs from the frozen original universe')
        actual = {canonical(gap) for gap in candidate['gaps']}
        require(all(canonical(gap) in actual for gap in self.inherited), 'U01', '/gaps', 'Upstream gap was removed or rewritten')

    def subjects(self):
        expected = set()
        for index, row in enumerate(self.hypotheses):
            expected.add(('hypothesis', canonical(self.assessment['hypotheses_ref']), f'/rows/{index}'))
            expected.update(('exposure', canonical(self.assessment['hypotheses_ref']), f'/rows/{index}/affected_exposures/{j}') for j, _ in enumerate(row['affected_exposures']))
        for kind, rows, ref in [('held_instrument', self.positions, self.account['positions_ref']), ('pending_instrument', self.pending, self.account['pending_transactions_ref'])]:
            expected.update((kind, canonical(ref), f'/rows/{index}') for index, _ in enumerate(rows))
        actual = [(row['kind'], canonical(row['source_ref']), row['source_locator']) for row in self.discovery['required_subjects']]
        require(len(actual) == len(set(actual)) and set(actual) == expected, 'U03', '/required_subjects', 'Required search subjects differ from all upstream hypotheses/exposures/risks')
        ids = {row['subject_id'] for row in self.discovery['required_subjects']}
        dispositions = self.coverage['subject_dispositions']
        require(len(dispositions) == len({row['subject_id'] for row in dispositions}) and {row['subject_id'] for row in dispositions} == ids,
                'U03', '/subject_dispositions', 'A subject was dropped or duplicated')
        covered = {row['subject_id'] for row in dispositions if row['disposition'] != 'unknown'}
        require(set(self.coverage['required_subject_ids']) == ids and set(self.coverage['covered_subject_ids']) == covered
                and set(self.coverage['unresolved_subject_ids']) == ids - covered, 'U03', '/coverage_ref', 'Coverage changes the original subject set')
        tasks = {row['task_id']: row for row in self.rows['query_tasks_ref']}
        for row in dispositions:
            if row['disposition'] == 'proven_not_applicable':
                require(row['citations'], 'U03', '/citations', 'Not-applicable search has no original evidence')
            else:
                require(row['task_ids'] and all(key in tasks and row['subject_id'] in tasks[key]['subject_ids'] for key in row['task_ids']),
                        'U03', '/task_ids', 'Subject is not bound to actual search tasks')

    def domains(self):
        sources = {row['source_id']: row for row in self.discovery['source_domains']}
        rows = self.coverage['source_coverage']
        require(len(rows) == len({row['source_id'] for row in rows}) and {row['source_id'] for row in rows} == set(sources),
                'U04', '/source_coverage', 'Declared source domains disappeared')
        ready = any(row['issuer_scope'] == 'cross_issuer' for row in sources.values())
        ready &= all(row['status'] == 'complete' and row['representativeness_citations'] and is_known(row['verified_domain']) for row in rows)
        if not ready:
            self.result('U04', 'insufficient_evidence', 'Cross-issuer channel/product representativeness remains unproven')

    def requests(self):
        tasks = {row['task_id']: row for row in self.rows['query_tasks_ref']}
        self.current_gaps = current_scope_gaps(tasks, self.rows['unprocessed_scope_ref'], self.clock['current_at'], self.clock['historical_origins'])
        attempts = {row['attempt_id']: row for row in self.rows['attempts_ref']}
        sources = {row['source_id'] for row in self.discovery['source_domains']}
        for attempt in attempts.values():
            require(attempt['task_id'] in tasks, 'U05', '/attempts', 'Attempt has no actual task')
            task = tasks[attempt['task_id']]
            require(attempt['source_id'] == task['source_id'] and attempt['requested_url'] == task['request_url']
                    and instant(attempt['started_at']) <= instant(attempt['finished_at']), 'U05', '/attempts', 'Request identity or interval differs')
            if is_known(attempt['raw_ref']):
                capture = self.read(attempt['raw_ref']['value'], PREFIX + 'capture')
                capture_bytes(capture)
                require(capture['request_url'] == attempt['requested_url'] and instant(attempt['started_at']) <= instant(capture['captured_at']) <= instant(attempt['finished_at'])
                        and is_known(attempt['http_status']) and attempt['http_status']['value'] == capture['http_status'],
                        'U05', '/raw_ref', 'Capture does not match its actual request')
            if attempt['outcome'] == 'retrieved':
                require(is_known(attempt['raw_ref']) and is_known(attempt['http_status']) and 200 <= attempt['http_status']['value'] < 300,
                        'U05', '/outcome', 'Successful retrieval has no successful original response')
        incomplete = False
        for key, task in tasks.items():
            require(task['source_id'] in sources, 'U05', '/source_id', 'Unregistered source task')
            require(set(task['attempt_ids']) == {row['attempt_id'] for row in attempts.values() if row['task_id'] == key},
                    'U05', '/attempt_ids', 'Task hides an attempt or invents one')
            require(all(child in tasks and is_known(tasks[child]['parent_task_id']) and tasks[child]['parent_task_id']['value'] == key for child in task['continuation_task_ids']),
                    'U05', '/continuation_task_ids', 'Continuation edge is missing')
            trail, cursor = set(), key
            while cursor is not None:
                require(cursor in tasks and cursor not in trail, 'U05', '/parent_task_id', 'Pagination parent is missing or cyclic')
                trail.add(cursor)
                parent = tasks[cursor]['parent_task_id']
                if is_known(parent):
                    require(parent['value'] in tasks and cursor in tasks[parent['value']]['continuation_task_ids'], 'U05', '/parent_task_id', 'Parent-child pagination edge differs')
                cursor = parent['value'] if is_known(parent) else None
            if task['task_state'] == 'completed':
                require(any(attempts[item]['outcome'] == 'retrieved' for item in task['attempt_ids']), 'U05', '/task_state', 'Completed task lacks a retrieved response')
            elif instant(task['requested_origin']) == instant(self.clock['current_at']):
                incomplete = True
        for reference in self.audit['original_list_refs']:
            capture = self.read(reference, PREFIX + 'capture')
            entries, following = original_directory(capture, max_bytes=self.discovery['max_response_bytes'])
            self.directories[reference['uri']] = entries
            task_ids = {row['task_id'] for row in attempts.values() if is_known(row['raw_ref']) and row['raw_ref']['value'] == reference}
            require(task_ids, 'U05', '/original_list_refs', 'Original list has no retrieval attempt')
            linked = {tasks[child]['request_url'] for key in task_ids for child in tasks[key]['continuation_task_ids']}
            require(following <= linked, 'U05', '/continuation_task_ids', 'A visible next page was dropped')
            if entries is None:
                incomplete |= any(instant(tasks[key]['requested_origin']) == instant(self.clock['current_at']) for key in task_ids)
                require(any(row['scope_kind'] == 'unparsed_entries' and row['source_id'] in {tasks[key]['source_id'] for key in task_ids} for row in self.rows['unprocessed_scope_ref']),
                        'U05', '/unprocessed_scope_ref', 'Unknown layout was silently treated as fully parsed')
        if incomplete or self.current_gaps:
            self.result('U05', 'insufficient_evidence', 'Actual unfinished tasks/pages/source regions remain in the original scope')

    def original_members(self):
        mapped = defaultdict(set)
        owners = {}
        for member, row in self.members.items():
            require(row['source_entry_refs'], 'U06', '/source_entry_refs', 'Member lost its original entry')
            for reference in row['source_entry_refs']:
                entry = self.read(reference, PREFIX + 'source_entry')
                require(entry['raw_ref']['uri'] in self.directories, 'U06', '/source_entry_refs', 'Member is not from a declared original list')
                source_key = (entry['raw_ref']['uri'], entry['locator'])
                require(source_key not in owners or owners[source_key] == member,
                        'U06', '/source_entry_refs', 'One original entry was assigned to different members')
                owners[source_key] = member
                mapped[entry['raw_ref']['uri']].add(entry['locator'])
                observed = self.directories[entry['raw_ref']['uri']]
                if observed is not None:
                    require(entry['locator'] in observed,
                            'U06', '/locator', 'Entry is outside the independently parsed original directory')
                    source_row = observed[entry['locator']]
                    require(re.sub(r'\s+', '', entry['raw_label']) in re.sub(r'\s+', '', source_row['context']),
                            'U06', '/raw_label', 'Original label is not at its claimed source node')
                    require((entry['original_code']['status'] == 'unknown' if source_row['code'] is None else
                             is_known(entry['original_code']) and entry['original_code']['value'] == source_row['code']),
                            'U06', '/original_code', 'Original entry or code differs from independent parsing')
                else:
                    capture = self.read(entry['raw_ref'], PREFIX + 'capture')
                    citation_text({'locator_kind': 'html_element', 'locator': entry['locator'], 'quoted_text': entry['raw_label']}, capture, self.ctx.attempt_dir)
            identity = self.identities[member]
            require(identity is not None or row['entity_id']['status'] == 'unknown', 'U06', '/entity_id', 'Known entity has no identity evidence')
            if identity is not None:
                require((row['entity_id']['status'], row['entity_id']['value']) == (identity['entity_id']['status'], identity['entity_id']['value']),
                        'U06', '/entity_id', 'Member differs from its actual identity version')
            matching = [item for item in self.rows['membership_provenance_ref'] if item['member_id'] == member]
            require({item['source_entry_ref']['uri'] for item in matching} == {item['uri'] for item in row['source_entry_refs']},
                    'U06', '/membership_provenance_ref', 'Original entry-to-member mapping is incomplete')
            group_ids = {item['peer_group_id'] for item in matching}
            require({self.read(ref, PREFIX + 'peer_group')['peer_group_id'] for ref in row['peer_group_refs']} == group_ids,
                    'U06', '/peer_group_refs', 'Member group references differ from its original mappings')
        for uri, expected in self.directories.items():
            if expected is not None:
                require(mapped[uri] == set(expected), 'U06', '/members_ref', 'A discovered product entry was omitted or invented')

    def partition(self):
        self.actual_partition = verify_partition(list(self.members), {name: self.rows[field] for name, field in
            [('candidate', 'candidate_members_ref'), ('excluded', 'excluded_members_ref'), ('unknown', 'unknown_members_ref')]})

    def check_eligibility(self):
        for field in ('candidate_members_ref', 'excluded_members_ref', 'unknown_members_ref'):
            for row in self.rows[field]:
                results = row['predicate_results']
                require(row['eligibility_rule_ref'] == self.role_ref('eligibility_rule_ref') and len(results) == len({item['predicate_id'] for item in results})
                        and {item['predicate_id']: item['verdict'] for item in results} == self.predicates[row['member_id']],
                        'U09', '/predicate_results', 'Candidate predicates differ from independent available identity comparison')
        self.local_partition, expected = recompute_partitions(list(self.members), list(self.groups), self.rows['membership_provenance_ref'], self.current_identities, self.predicates)
        require(expected == self.actual_partition, 'U09', '/members_ref', 'Global C/K/E differs from independent cross-group aggregation')
        for group, parts in self.local_partition.items():
            actual = self.groups[group]
            require(set(actual['member_ids']) == parts['candidate'] and set(actual['excluded_member_ids']) == parts['excluded']
                    and set(actual['possible_member_ids']) == parts['unknown'], 'U09', '/peer_groups_ref', 'Group C/E/K differs from original relationships')

    def peers(self):
        rules = {row['peer_rule_id']: row for row in self.discovery['peer_rules']}
        subjects = {row['subject_id'] for row in self.discovery['required_subjects']}
        for group in self.groups.values():
            rule = self.read(group['peer_rule_ref'], PREFIX + 'peer_rule')
            require(rule['peer_rule_id'] in rules and rule == rules[rule['peer_rule_id']] and set(group['subject_ids']) <= subjects,
                    'U08', '/peer_groups_ref', 'Peer rule or group subject was changed')
        for row in self.rows['membership_provenance_ref']:
            require(row['peer_group_id'] in self.groups and row['rule_ref'] == self.groups[row['peer_group_id']]['peer_rule_ref'],
                    'U08', '/rule_ref', 'Classification does not bind its declared peer rule')
            require(row['relation'] == 'possible' or row['classification_citations'], 'U08', '/classification_citations', 'Known classification has no original source')
        possible = {(row['member_id'], row['peer_group_id']) for row in self.rows['possible_members_ref']}
        expected = {(member, group) for group, parts in self.local_partition.items() for member in parts['unknown']}
        require(possible == expected, 'U08', '/possible_members_ref', 'Unknown group relationships were hidden')
        if possible:
            self.result('U08', 'insufficient_evidence', 'Group classification or eligibility is still unknown')

    def quote(self, citation):
        source = self.read(citation['raw_ref'])
        require(citation['raw_ref']['schema_id'] == PREFIX + 'capture' or isinstance(source, str),
                'U10', '/raw_ref', 'Derived business JSON is not original source evidence')
        return citation_text(citation, source, self.ctx.attempt_dir)

    def quotes(self):
        citations = {}
        def inspect(value):
            if isinstance(value, list):
                for child in value:
                    inspect(child)
            elif isinstance(value, dict):
                if {'citation_id', 'raw_ref', 'quoted_text', 'locator_kind', 'locator'} <= value.keys():
                    key = value['citation_id']
                    require(key not in citations or citations[key] == value, 'U10', '/citation_id', 'Citation ID has conflicting original positions')
                    citations[key] = value
                for child in value.values():
                    inspect(child)
        for uri, value in list(self.values.items()):
            if self.refs[uri]['schema_id'].startswith(PREFIX) and self.refs[uri]['schema_id'] != PREFIX + 'capture':
                inspect(value)
        for citation in citations.values():
            self.quote(citation)

    def exposures(self):
        hypotheses = {row['hypothesis_id'] for row in self.hypotheses}
        for row in self.rows['exposure_links_ref']:
            require(row['member_id'] in self.members and row['mapping_rule_ref'] == self.role_ref('exposure_mapping_rule_ref')
                    and row['exposure_kind'] in self.mapping['permitted_kinds']
                    and (not is_known(row['hypothesis_id']) or row['hypothesis_id']['value'] in hypotheses),
                    'U11', '/exposure_links_ref', 'Exposure target or mapping differs')
            identity = self.identities[row['member_id']]
            if is_known(row['share_class_id']):
                require(identity is not None and row['share_class_id']['value'] == identity['share_class_id']['value'],
                        'U11', '/share_class_id', 'Exposure belongs to another share class')
            values = verify_weight_bounds(row)
            source_values = set().union(*(numeric_literals(item['quoted_text']) for item in row['citations']))
            require(all(value in source_values for value in values.values()), 'U11', '/weight', 'Value is not the exact disclosed number or percent conversion')

    def risks(self):
        verify_mandatory_risks(self.positions, self.pending, self.rows['mandatory_assets_ref'], self.account['positions_ref'], self.account['pending_transactions_ref'])

    def funds(self):
        rows = [row for row in self.rows['mandatory_assets_ref'] if row['risk_kind'] == 'cash_comparator']
        needed = self.funding is not None or 'selection_comparison' in self.parameters['requested_uses']
        require(len(rows) <= 1 and (not needed or len(rows) == 1), 'U13', '/mandatory_assets_ref', 'Cash comparator is missing or duplicated')
        if not rows:
            return
        if self.funding is None:
            require(rows[0]['amount']['status'] == 'unknown', 'U13', '/amount', 'Independent cash amount is absent, not zero')
        else:
            require(rows[0]['amount'] == self.funding['available_amount'] and rows[0]['origin_ref'] == self.funding_ref
                    and rows[0]['origin_locator'] == '/available_amount', 'U13', '/amount', 'Funding amount or origin was changed')
            require(self.ctx.mode == 'test' or self.funding['source_type'] == 'user_confirmation', 'U13', '/source_type', 'Synthetic funds entered formal output')

    def times(self):
        require(self.payload['universe_at'] == self.clock['current_at'] and instant(self.clock['current_at']) <= instant(self.ctx.input['information_cutoff_at']),
                'U14', '/universe_at', 'Universe origin differs from its frozen clock')
        self.current_ready = all(value is not None for value in self.current_identities.values())
        for row in self.rows['exposure_links_ref']:
            self.current_ready &= availability_at(row['available_time'], row['availability_bound_at'], self.clock['current_at'])
        if is_known(self.payload['catalog_vintage_ref']):
            vintage = self.read(self.payload['catalog_vintage_ref']['value'], PREFIX + 'catalog_vintage')
            self.current_ready &= availability_at(vintage['available_time'], vintage['availability_bound_at'], self.clock['current_at'])
        else:
            self.current_ready = False

    def historical(self):
        origins = set(self.clock['historical_origins'])
        statuses = self.coverage['historical_origin_status']
        require(len(statuses) == len({row['origin_at'] for row in statuses}) and {row['origin_at'] for row in statuses} == origins,
                'U15', '/historical_origin_status', 'A requested origin was dropped or duplicated')
        if not origins:
            require(self.payload['universe_history_ref']['status'] == 'not_applicable' and self.coverage['historical_status'] == 'not_applicable',
                    'U15', '/universe_history_ref', 'History not requested must be explicitly not applicable')
            self.result('U15', 'not_applicable', 'Historical scope was not requested')
            return
        batches = defaultdict(list)
        ready = bool(self.history)
        for row in self.history:
            require(row['origin_at'] in origins, 'U15', '/history', 'Foreign historical origin')
            usable = is_known(row['availability_bound_at']) and instant(row['availability_bound_at']['value']) <= instant(row['origin_at'])
            require(usable or row['membership_status'] == 'unknown', 'U15', '/availability_bound_at', 'Later evidence was used for historical membership')
            ready &= usable and row['membership_status'] != 'unknown'
            batches[(row['origin_at'], row['universe_version_id'])].append(row)
        for (origin, version), rows in batches.items():
            reference = rows[0]['source_vintage_ref']
            require(all(row['source_vintage_ref'] == reference for row in rows), 'U15', '/source_vintage_ref', 'One historical version has conflicting source vintages')
            vintage = self.read(reference, PREFIX + 'catalog_vintage')
            vintage_usable = availability_at(vintage['available_time'], vintage['availability_bound_at'], origin)
            require(vintage_usable or all(row['membership_status'] == 'unknown' for row in rows),
                    'U15', '/source_vintage_ref', 'Later catalog was asserted as known historical membership')
            ready &= vintage_usable
            source_codes, row_codes = set(), set()
            for raw_ref in vintage['original_list_refs']:
                entries, _ = original_directory(self.read(raw_ref, PREFIX + 'capture'), max_bytes=self.discovery['max_response_bytes'])
                if entries is None or any(row['code'] is None for row in entries.values()):
                    ready = False
                else:
                    source_codes.update(row['code'] for row in entries.values())
            for row in rows:
                identity = self.read(row['identity_version_ref']['value'], PREFIX + 'identity') if is_known(row['identity_version_ref']) else None
                identity_usable = identity is not None and availability_at(identity['available_time'], identity['availability_bound_at'], origin)
                require(identity_usable or row['membership_status'] == 'unknown', 'U15', '/identity_version_ref',
                        'Unknown or later identity was asserted as known historical membership')
                ready &= identity_usable
                if is_known(row['entity_id']):
                    require(identity is not None and is_known(identity['entity_id']) and row['entity_id'] == identity['entity_id'],
                            'U15', '/entity_id', 'Historical entity differs from its identity version')
                for exposure_ref in row['exposure_version_refs']:
                    require(exposure_ref['schema_id'] in (PREFIX + 'exposure_row', PREFIX + 'exposures'),
                            'U15', '/exposure_version_refs', 'Historical exposure reference has another business type')
                    version_value = self.read(exposure_ref)
                    exposure_rows = ([version_value] if exposure_ref['schema_id'] == PREFIX + 'exposure_row'
                                     else [item for item in version_value['rows'] if item['member_id'] == row['member_id']])
                    require(exposure_rows and len({item['exposure_id'] for item in exposure_rows}) == len(exposure_rows),
                            'U15', '/exposure_version_refs', 'Historical exposure version omits the member or duplicates an exposure')
                    for exposure in exposure_rows:
                        mapping = self.read(exposure['mapping_rule_ref'], PREFIX + 'exposure_mapping_rule')
                        texts = [self.quote(citation) for citation in exposure['citations']]
                        ready &= verify_historical_exposure(exposure, row, identity, mapping, texts)
                if identity is None:
                    continue
                if is_known(identity['fund_code']):
                    row_codes.add(identity['fund_code']['value'])
                else:
                    ready = False
                rule = self.read(row['eligibility_rule_ref'], PREFIX + 'eligibility_rule')
                verdicts = set(predicate_verdicts(identity, rule).values())
                require(row['membership_status'] != 'candidate' or verdicts == {'true'}, 'U15', '/membership_status', 'Historical candidate fails the disclosed eligibility rule')
            ready &= source_codes == row_codes
        ready &= {origin for origin, _ in batches} == origins and all(row['status'] == 'complete' for row in statuses) and self.coverage['historical_status'] == 'complete'
        if self.uses_hypotheses():
            ready &= 'hypothesis_research' in self.upstream['research_assessment_ref']['allowed_uses']
        if not ready:
            require(self.coverage['historical_status'] != 'complete', 'U15', '/historical_status', 'Claimed complete history omits original or unavailable members')
            self.result('U15', 'insufficient_evidence', 'Historical original scope or point-in-time facts remain unproven')

    def acquisition(self):
        require(self.audit['original_list_refs'], 'U16', '/original_list_refs', 'No original directory was obtained; a normal empty universe is forbidden')
        captured = {row['raw_ref']['value']['uri'] for row in self.rows['attempts_ref'] if is_known(row['raw_ref'])}
        require(all(ref['uri'] in captured for ref in self.audit['original_list_refs']), 'U16', '/attempts_ref', 'Original list lacks an actual retrieval attempt')
        if self.rows['unprocessed_scope_ref']:
            require(self.audit['gaps'] and self.coverage['gaps'], 'U16', '/gaps', 'Unfinished acquisition has no recorded evidence gaps')

    def labels(self):
        gaps = {canonical(gap) for gap in self.ctx.candidate['gaps']}
        require(all(canonical(gap) in gaps for gap in [*self.inherited, *self.audit['gaps'], *self.coverage['gaps']]),
                'U17', '/gaps', 'Original source or upstream gaps were hidden')
        closed = all(self.checks.get(rule, {}).get('verdict') == 'passed' for rule in ('U04', 'U05', 'U08'))
        if self.audit['completeness_status'] == 'complete' or self.coverage['current_status'] == 'complete':
            require(closed and self.current_ready and not self.rows['possible_members_ref'] and not self.current_gaps,
                    'U17', '/completeness_status', 'Declared complete scope contradicts actual unresolved work')

    def ordering(self):
        require(1 <= self.ctx.attempt_no <= 3 and self.ctx.candidate['run_id'] == self.ctx.run_id
                and self.ctx.candidate['stage_id'] == 'SC04' and self.ctx.candidate['processing_state'] == 'completed',
                'U18', '/candidate', 'Candidate identity or execution state differs')
        tables = {row['schema_id']: row for row in self.ctx.schemas.stage_registries['SC04']['tables']}
        for uri, reference in self.refs.items():
            if reference['schema_id'] in tables:
                key = tables[reference['schema_id']]['primary_key']
                rows = self.values[uri]['rows']
                require(rows == sorted(rows, key=lambda row: tuple(row[field].encode('utf-8') for field in key)),
                        'U18', '/rows', 'Semantic table row order differs from the frozen registry')

    def uses_hypotheses(self):
        return any(row['kind'] in ('hypothesis', 'exposure') for row in self.discovery['required_subjects'])

    def selection(self):
        ready = all(self.checks.get(rule, {}).get('verdict') == 'passed' for rule in ('U04', 'U05', 'U08'))
        ready &= self.current_ready and self.audit['completeness_status'] == 'complete' and self.coverage['current_status'] == 'complete'
        ready &= not self.rows['unknown_members_ref'] and not self.rows['possible_members_ref'] and not self.current_gaps and not self.coverage['unresolved_subject_ids']
        if self.uses_hypotheses():
            ready &= 'hypothesis_research' in self.upstream['research_assessment_ref']['allowed_uses']
        if not ready:
            self.result('U19', 'insufficient_evidence', 'Complete peer comparison has not met its actual scope and upstream-use gates')

    def hypotheses_gate(self):
        if not self.hypotheses:
            self.result('U20', 'not_applicable', 'No hypothesis was requested')
        elif 'hypothesis_research' not in self.upstream['research_assessment_ref']['allowed_uses'] or not any(
            is_known(row['hypothesis_id']) and row['member_id'] in self.actual_partition['candidate'] for row in self.rows['exposure_links_ref']):
            self.result('U20', 'insufficient_evidence', 'No supported eligible hypothesis mapping or upstream analysis permission')

    def account_gate(self):
        if 'current_account_review' not in self.upstream['transaction_context_ref']['allowed_uses']:
            self.result('U21', 'insufficient_evidence', 'Actual SC01 receipt restricts current account review; its reporting permission is preserved')

    def semantic_review(self):
        planner = ReviewWorkPlanner(self)
        targets = list(planner.targets())
        expected = [(reference['uri'], path) for reference, paths in targets for path in paths]
        require(expected and len(expected) == len(set(expected)), 'U10', '/semantic_review',
                'Semantic targets are absent or duplicated')
        completed = set()
        for reference, paths in targets:
            work = planner.build(reference, paths)
            work_key = work['work_key']
            work_ref = self.ctx.put_json('validation-review-' + work_key + '.json', work, PREFIX + 'review_work_item')
            by_uri = {location['object_ref']['uri']: location['object_ref'] for location in work['evidence_locations']}
            evidence = [work_ref, *(by_uri[uri] for uri in sorted(by_uri))]
            response = self.ctx.host_response('source_review', work_key=work_key)
            if response is None:
                return self.ctx.request_host('source_review', evidence, work_key=work_key)
            require(response['work_key'] == work_key and response['reviewed_object_refs'] == [reference], 'U10', '/reviewed_object_refs', 'Independent reviewer changed its object batch')
            self.review_gaps.extend(response['gaps'])
            findings = response['findings']
            require(len(findings) == len(paths) and {row['field_path'] for row in findings} == set(paths)
                    and all(row['object_ref'] == reference for row in findings), 'U10', '/findings', 'Independent reviewer omitted a required field')
            for finding in findings:
                value = json_pointer(self.values[reference['uri']], finding['field_path'])
                for citation in finding['citations']:
                    require(citation['raw_ref'] in evidence, 'U10', '/raw_ref', 'Reviewer cited outside current granted source evidence')
                    self.quote(citation)
                if finding['verdict'] != 'supported':
                    raise ContractError('U10', finding['field_path'], finding['reason'],
                                        'missing_evidence' if finding['verdict'] == 'unknown' else 'business_invariant')
                unknown = isinstance(value, dict) and value.get('status') in ('unknown', 'not_applicable')
                unknown |= isinstance(value, str) and value in ('unknown', 'not_applicable')
                require(unknown or value == [] or finding['citations'], 'U10', finding['field_path'], 'Known semantic claim lacks original located evidence')
            completed.update((reference['uri'], path) for path in paths)
        require(completed == set(expected), 'U10', '/semantic_review', 'Semantic response coverage is incomplete')
        return None

    def receipt(self):
        if any(row['verdict'] == 'failed' for row in self.checks.values()):
            self.result('U17', 'failed', 'Current candidate contains a disproved assertion or invariant')
        for rule in self.ctx.definition['required_rules']:
            if rule not in self.checks:
                self.result(rule, 'insufficient_evidence', 'Failed prerequisites prevented a supported evaluation')
        checks = [self.checks[rule] for rule in self.ctx.definition['required_rules']]
        critical = all(self.checks[rule]['verdict'] == 'passed' for rule in self.ctx.definition['critical_rules'])
        allowed = [row['use'] for row in self.ctx.definition['use_rules'] if critical and all(self.checks[rule]['verdict'] == 'passed' for rule in row['required_pass_rules'])]
        gaps, seen = [], set()
        for gap in [*self.inherited, *self.review_gaps]:
            if canonical(gap) not in seen:
                gaps.append(gap)
                seen.add(canonical(gap))
        for row in checks:
            if row['verdict'] not in ('failed', 'insufficient_evidence'):
                continue
            affected = [item['use'] for item in self.ctx.definition['use_rules'] if row['rule_id'] in item['required_pass_rules']] or ['reporting']
            gap = {'rule_id': row['rule_id'], 'field_or_object': '/checks/' + row['rule_id'], 'reason': row['reason'],
                   'owner': 'validator', 'required_evidence': ['source_bytes'], 'affected_uses': affected}
            if canonical(gap) not in seen:
                gaps.append(gap)
                seen.add(canonical(gap))
        failed = any(row['verdict'] == 'failed' for row in checks)
        partial = any(row['verdict'] == 'insufficient_evidence' for row in checks)
        result = {'validator_id': 'sc04-validator-v1', 'validator_version': 'v1', 'run_id': self.ctx.run_id, 'stage_id': 'SC04',
                  'input_refs': self.ctx.input['input_refs'], 'candidate_output_ref': self.ctx.candidate_ref, 'validated_at': now(),
                  'checks': checks, 'overall_verdict': 'failed' if failed else 'partial' if partial else 'passed',
                  'business_result': 'rejected' if failed else 'blocked' if not allowed else 'observation_only' if partial else 'usable',
                  'allowed_uses': allowed, 'blockers': gaps}
        self.ctx.schemas.validate('investment.validation_receipt', result)
        return result

    def run(self):
        if not self.run_check('U02', self.load):
            return self.receipt()
        operations = [
            ('U01', self.binding), ('U03', self.subjects), ('U04', self.domains), ('U05', self.requests),
            ('U06', self.original_members), ('U07', self.partition), ('U09', self.check_eligibility), ('U08', self.peers),
            ('U10', self.quotes), ('U11', self.exposures), ('U12', self.risks), ('U13', self.funds), ('U14', self.times),
            ('U15', self.historical), ('U16', self.acquisition), ('U17', self.labels), ('U18', self.ordering),
            ('U19', self.selection), ('U20', self.hypotheses_gate), ('U21', self.account_gate),
        ]
        for rule, operation in operations:
            if not self.run_check(rule, operation):
                return self.receipt()
        if not all(self.checks[rule]['verdict'] == 'passed' for rule in self.ctx.definition['critical_rules']):
            return self.receipt()
        try:
            request = self.semantic_review()
        except ContractError as error:
            self.result('U10', 'insufficient_evidence' if error.code == 'missing_evidence' else 'failed', error.reason)
        else:
            if request is not None:
                return request
        return self.receipt()


def validate(ctx):
    """返回独立凭证或真实待执行宿主请求，不自签业务输出。"""
    return Review(ctx).run()

