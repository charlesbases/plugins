"""SC05 D01—D20独立验收、受限物理视图及真实宿主原文审核。"""
from __future__ import annotations

import hashlib
import itertools
import re
from collections import defaultdict
from datetime import timedelta
from fractions import Fraction
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from investment.contracts import ContractError, canonical, instant, is_known, now, require
from investment.research.references import ARTIFACT_FIELDS, ResearchResolver

from .contracts import (RULES, available_at, gap_index, label_availability_upper, label_dependencies, mature_for_fit, overlaps, period_key, quantized,
                        reinvested_return, selected_predictors, time_upper, value_equal, verify_conversion,
                        verify_endpoint_period, verify_features, verify_inherited_availability, verify_scheduled_period, view_observation_ids)
from .source_validation import capture_bytes, located_text, pointer, semantic_paths, source_references

PREFIX = 'investment.sc05.'
TABLES = {
    'current_market_snapshot_ref': 'market', 'observation_vintages_ref': 'observations',
    'asof_panel_ref': 'panels', 'label_events_ref': 'labels', 'equity_events_ref': 'events',
    'contract_vintages_ref': 'contracts', 'calendar_ref': 'calendar', 'availability_audit_ref': 'availability',
}
AUDIT_TABLES = {'acquisition_tasks_ref': 'tasks', 'attempts_ref': 'attempts',
                'source_dispositions_ref': 'dispositions', 'pending_tasks_ref': 'pending'}
VIEW_TABLES = ('asof_panel_ref', 'label_events_ref', 'observation_vintages_ref',
               'contract_vintages_ref', 'calendar_ref', 'equity_events_ref')


class Review:
    """本attempt只读输入、候选及原始依据，不调用生产者算法或私有分析。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.refs, self.values, self.checks = {}, {}, {}
        self.rows, self.upstream, self.inherited, self.source_rules = {}, {}, [], {}

    def result(self, rule, verdict, reason=''):
        severity = {'passed': 0, 'insufficient_evidence': 1, 'failed': 2}
        if rule not in self.checks or severity[verdict] >= severity[self.checks[rule]['verdict']]:
            self.checks[rule] = {'rule_id': rule, 'verdict': verdict, 'reason': reason[:2048],
                                 'evidence_refs': [self.ctx.input_ref, self.ctx.candidate_ref]}

    def evaluate(self, rule, operation):
        try:
            operation()
        except ContractError as error:
            target = error.rule if error.rule in RULES else rule
            self.result(target, 'insufficient_evidence' if error.code == 'missing_evidence' else 'failed', error.reason)
            if target != rule:
                self.result(rule, 'insufficient_evidence', 'Required dependency failed: ' + error.reason)
            return False
        if rule not in self.checks:
            self.result(rule, 'passed')
        return True

    def read(self, reference, expected=None):
        require(not reference['uri'].rsplit('/', 1)[-1].startswith('host-'),
                'D01', '/references', 'Producer private host artifacts are not original business evidence')
        value = self.ctx.resolver.resolve(reference, expected)
        require(reference['uri'] not in self.refs or self.refs[reference['uri']] == reference,
                'D01', '/references', 'Conflicting reference identity')
        self.refs[reference['uri']], self.values[reference['uri']] = reference, value
        return value

    def role_ref(self, role):
        matches = [row['ref'] for row in self.ctx.input['input_refs'] if row['role'] == role]
        require(len(matches) == 1, 'D01', '/input_refs', 'Required role is absent or repeated')
        return matches[0]

    def load(self):
        require(self.ctx.role == 'validator' and self.ctx.candidate is not None and self.ctx.candidate_ref is not None,
                'D01', '/candidate', 'Independent validation requires a completed candidate')
        self.bundle = self.read(self.ctx.candidate['payload_ref'], PREFIX + 'dataset_bundle')
        for role in self.ctx.definition['input_roles']:
            if role['required']:
                self.read(self.role_ref(role['role']), role['schema_id'])
            if role['source']['kind'] == 'upstream_output':
                ref, receipt = self.ctx.upstream_receipt(role['role'])
                self.upstream[role['role']] = receipt
                self.inherited.extend(receipt['blockers'])
        self.account = self.values[self.role_ref('transaction_context_ref')['uri']]
        self.assessment = self.values[self.role_ref('research_assessment_ref')['uri']]
        self.universe = self.values[self.role_ref('instrument_universe_ref')['uri']]
        self.spec = self.values[self.role_ref('data_build_spec_ref')['uri']]
        self.protocol = self.values[self.role_ref('method_policy_ref')['uri']]
        self.funding = self.values[self.role_ref('funding_plan_ref')['uri']]
        self.inherited += self.account['missing_facts'] + self.assessment['unresolved_questions']
        self.parameters = self.read(self.ctx.input['parameters_ref'], PREFIX + 'parameters')
        self.requested = self.read(self.spec['requested_time_scope_ref'], PREFIX + 'requested_time_scope')
        self.audit = self.read(self.bundle['source_acquisition_audit_ref'], PREFIX + 'source_acquisition_audit')
        for field, name in TABLES.items():
            self.rows[name] = self.read(self.bundle[field])['rows']
        for field, name in AUDIT_TABLES.items():
            self.rows[name] = self.read(self.audit[field])['rows']
        self.gaps = self.read(self.bundle['data_gaps_ref'], PREFIX + 'data_gaps')['gaps']
        self.gap_map = gap_index([*self.gaps, *self.inherited])
        self.split = self.read(self.bundle['split_manifest_ref'], PREFIX + 'split_manifest')
        self.rows['assignments'] = self.read(self.split['assignments_ref'])['rows']
        self.members = self.read(self.universe['members_ref'])['rows']
        self.risks = self.read(self.universe['mandatory_assets_ref'])['rows']
        self.public_coverage = self.read(self.universe['coverage_ref'])
        self.economics = self.read(self.assessment['economic_observations_ref'])['rows']
        self.history = self.read(self.bundle['universe_history_ref']['value'])['rows'] if is_known(self.bundle['universe_history_ref']) else []
        self.views = {name: self.read(self.bundle[name + '_view_ref'], PREFIX + 'data_view')
                      for name in ('training', 'selection', 'independent_validation', 'current')}
        self.indexes = {}
        for name, key in (('observations', 'version_id'), ('panels', 'sample_id'), ('labels', 'label_id'),
                          ('events', 'event_id'), ('contracts', 'contract_version_id'), ('tasks', 'task_id'),
                          ('attempts', 'attempt_id')):
            index = {row[key]: row for row in self.rows[name]}
            require(len(index) == len(self.rows[name]), 'D01', '/' + name, 'Duplicate primary key')
            self.indexes[name] = index
        for definition in self.protocol['model_search_space']['predictors']:
            ref = definition['source_rule_ref']
            self.source_rules[ref['sha256']] = self.read(ref, PREFIX + 'source_rule')

    def bindings(self):
        incoming, candidate = self.ctx.input, self.ctx.candidate
        require(candidate['input_refs'] == incoming['input_refs'] and candidate['scope_ref'] == incoming['scope_ref']
                and candidate['method_ref'] == self.ctx.method_ref and candidate['run_id'] == self.ctx.run_id
                and candidate['stage_id'] == 'SC05', 'D01', '/candidate', 'Candidate changed frozen invocation identity')
        for field in ('data_build_spec_ref', 'method_policy_ref'):
            require(self.parameters[field] == self.role_ref(field), 'D01', '/' + field, 'Parameter and role reference differ')
        require(incoming['scope_ref'] == self.role_ref('method_policy_ref') == self.bundle['experiment_protocol_ref']
                and self.read(self.bundle['experiment_protocol_ref']) == self.protocol,
                'D01', '/scope_ref', 'Protocol differs from exact frozen scope')
        require(self.bundle['universe_history_ref'] == self.universe['universe_history_ref'],
                'D01', '/universe_history_ref', 'SC04 history was changed or replaced')
        require(self.audit['run_id'] == self.ctx.run_id and self.audit['data_build_spec_ref'] == self.role_ref('data_build_spec_ref')
                and self.audit['mandatory_assets_ref'] == self.universe['mandatory_assets_ref'],
                'D01', '/source_acquisition_audit_ref', 'Acquisition audit changed run, scope or risk inputs')
        discovery = self.read(self.universe['discovery_audit_ref'])
        require(self.audit['universe_ref'] == discovery['universe_ref'], 'D01', '/universe_ref', 'Audit does not bind original SC04 snapshot')
        cutoff = incoming['information_cutoff_at']
        require(instant(cutoff) <= instant(incoming['decision_at'])
                and self.protocol['information_cutoff_at'] == self.bundle['information_cutoff_at'] == cutoff
                and self.requested['current_cutoff_at'] == self.protocol['origin_rule']['current_origin_at'] == cutoff,
                'D01', '/information_cutoff_at', 'Current information clocks differ')
        for row in self.rows['labels']:
            require(row['sample_id'] in self.indexes['panels'], 'D01', '/sample_id', 'Label sample reference is unresolved')
            for field, index in (('source_version_ids', 'observations'), ('equity_event_ids', 'events'), ('contract_version_ids', 'contracts')):
                require(set(row[field]) <= self.indexes[index].keys(), 'D01', '/' + field, 'Label foreign key is unresolved')
            for field in ('start_version_id', 'end_version_id'):
                require(not is_known(row[field]) or row[field]['value'] in self.indexes['observations'], 'D01', '/' + field, 'Label endpoint is unresolved')
        for panel in self.rows['panels']:
            require(set(panel['source_version_ids']) <= self.indexes['observations'].keys()
                    and set(panel['contract_version_ids']) <= self.indexes['contracts'].keys(), 'D01', '/asof_panel', 'Panel foreign key is unresolved')
            for feature in panel['feature_values']:
                require(set(feature['source_version_ids']) <= self.indexes['observations'].keys(), 'D01', '/feature_values', 'Feature source version is unresolved')
        require(len({(row['entity_id'], row['origin_at'], row['universe_version_id']) for row in self.rows['panels']}) == len(self.rows['panels']),
                'D01', '/asof_panel', 'Entity/origin/universe semantic key is duplicated')
        require(len({(row['sample_id'], row['target_id'], row['horizon_id']) for row in self.rows['labels']}) == len(self.rows['labels']),
                'D01', '/label_events', 'Sample/target/horizon semantic key is duplicated')
        for event in self.rows['events']:
            require(event['contract_version_id'] in self.indexes['contracts'] and (not is_known(event['reinvestment_nav_version_id'])
                    or event['reinvestment_nav_version_id']['value'] in self.indexes['observations']),
                    'D01', '/equity_events', 'Equity contract or reinvestment NAV foreign key is unresolved')
        for row in self.rows['assignments']:
            require(row['sample_id'] in self.indexes['panels'] and row['label_id'] in self.indexes['labels'],
                    'D01', '/assignments', 'Partition label or sample is unresolved')

    def registration(self):
        registered = instant(self.protocol['registered_at'])
        starts = [instant(row['requested_at']) for row in self.rows['attempts']]
        require(registered <= instant(self.ctx.candidate['created_at'])
                and instant(self.spec['registered_at']) <= min(starts, default=instant(self.ctx.candidate['created_at']))
                and registered <= min(starts, default=instant(self.ctx.candidate['created_at'])),
                'D02', '/registered_at', 'Protocol or acquisition specification was registered after acquisition began')
        require(self.requested['origins'] == self.protocol['origin_rule']['origins']
                and instant(self.requested['registered_at']) <= min(starts, default=instant(self.ctx.candidate['created_at'])),
                'D02', '/requested_time_scope_ref', 'Requested historical origins or registration differ')
        public_origins = [row['origin_at'] for row in self.public_coverage['historical_origin_status']]
        require(self.universe['universe_at'] == self.ctx.input['information_cutoff_at']
                and len(public_origins) == len(set(public_origins)) and set(public_origins) == set(self.requested['origins'])
                and all(row['origin_at'] in public_origins for row in self.history),
                'D02', '/historical_origin_status', 'SC04 public time scope differs from this frozen protocol and build specification')
        for parameter in self.protocol['parameter_specs']:
            require(instant(parameter['determined_at']) <= registered, 'D02', '/determined_at', 'Parameter was determined after protocol registration')
        require(self.funding['source_type'] != 'synthetic_fixture' or self.ctx.mode == 'test',
                'D02', '/funding_plan_ref', 'Synthetic user funds cannot enter a formal invocation')

    def coverage(self):
        expected = {}
        def add(kind, identifier, reference, capabilities):
            expected[kind, identifier, canonical(reference)] = reference, capabilities
        for member in self.members:
            add('original_member', member['member_id'], self.universe['members_ref'], self.spec['required_capabilities'])
        current_members = {row['member_id'] for row in self.members}
        for historical in self.history:
            if historical['member_id'] not in current_members:
                add('original_member', historical['member_id'], self.bundle['universe_history_ref']['value'], self.spec['required_capabilities'])
        for risk in self.risks:
            add('mandatory_asset', risk['risk_id'], self.universe['mandatory_assets_ref'], self.spec['required_capabilities'])
        for economic in self.economics:
            add('economic_subject', economic['observation_id'], self.assessment['economic_observations_ref'], ['economic_observation'])
        identity_refs = [row['identity_ref']['value'] for row in self.members if is_known(row['identity_ref'])]
        identity_refs += [row['identity_version_ref']['value'] for row in self.history if is_known(row['identity_version_ref'])]
        for reference in identity_refs:
            identity = self.read(reference, 'investment.sc04.identity')
            if is_known(identity['benchmark_id']):
                add('benchmark', identity['benchmark_id']['value'], reference, ['benchmark'])
        tasks, pairs = self.rows['tasks'], set()
        require(tasks or not expected, 'D03', '/acquisition_tasks', 'Original objects have no acquisition tasks')
        for row in tasks:
            key = row['object_kind'], row['object_id'], canonical(row['object_ref'])
            require(key in expected, 'D03', '/object_ref', 'Task renamed or invented an original object')
            require(row['requested_time_scope_ref'] == self.spec['requested_time_scope_ref'], 'D03', '/requested_time_scope_ref', 'Task changed requested time scope')
            rule = self.read(row['source_rule_ref'], PREFIX + 'source_rule')
            require(rule in self.spec['source_rules'] and row['capability'] == rule['capability'],
                    'D03', '/source_rule_ref', 'Task source rule differs from frozen capability definition')
            self.source_rules[row['source_rule_ref']['sha256']] = rule
            pairs.add((*key, row['capability']))
        for key, (_, capabilities) in expected.items():
            require(all((*key, capability) in pairs for capability in capabilities),
                    'D03', '/acquisition_tasks', 'Original member, risk or economic capability is missing')
        actual = [(canonical(row['object_ref']), row['object_id'], row['capability']) for row in self.rows['dispositions']]
        require(len(actual) == len(set(actual)) and set(actual) == {(canonical(row['object_ref']), row['object_id'], row['capability']) for row in tasks},
                'D03', '/source_dispositions', 'Final disposition coverage differs from all original tasks')

    def acquisition(self):
        require(len(self.rows['attempts']) <= self.spec['max_requests'], 'D04', '/attempts', 'Actual requests exceed frozen budget')
        raw_refs = {row['uri']: row for row in self.audit['raw_refs']}
        used = set()
        grouped = defaultdict(list)
        for attempt in self.rows['attempts']:
            require(attempt['task_id'] in self.indexes['tasks'], 'D04', '/task_id', 'Attempt has no original task')
            task = self.indexes['tasks'][attempt['task_id']]
            rule = self.read(task['source_rule_ref'])
            require(instant(attempt['requested_at']) <= instant(attempt['finished_at']) <= instant(self.ctx.candidate['created_at']),
                    'D04', '/finished_at', 'Request times are reversed or follow candidate completion')
            require(urlsplit(attempt['url']).scheme in ('http', 'https') and urlsplit(attempt['url']).hostname in rule['allowed_domains'],
                    'D04', '/url', 'Actual request is outside frozen source domains')
            require(attempt['source_type'] != 'synthetic_fixture' or self.ctx.mode == 'test', 'D04', '/source_type', 'Synthetic capture entered formal root')
            grouped[attempt['task_id']].append(attempt)
            if is_known(attempt['raw_ref']):
                reference = attempt['raw_ref']['value']
                require(raw_refs.get(reference['uri']) == reference, 'D04', '/raw_refs', 'Attempt source bytes were omitted from audit')
                capture = self.read(reference, PREFIX + 'capture')
                capture_bytes(capture)
                require(capture['task_id'] == attempt['task_id'] and capture['requested_url'] == attempt['url']
                        and capture['source_type'] == attempt['source_type'] and capture['parser_id'] == rule['parser_id']
                        and capture['parser_version'] == rule['parser_version'] and capture['body_byte_size'] <= self.spec['max_response_bytes']
                        and urlsplit(capture['final_url']).hostname in rule['allowed_domains'],
                        'D04', '/raw_ref', 'Capture identity, parser, redirect or budget differs')
                require(instant(attempt['requested_at']) <= instant(capture['retrieved_at']) <= instant(attempt['finished_at']),
                        'D04', '/retrieved_at', 'Capture time is outside actual request interval')
                require(is_known(attempt['http_status']) and attempt['http_status']['value'] == capture['http_status'],
                        'D04', '/http_status', 'Attempt status differs from captured HTTP status')
                used.add(reference['uri'])
            require(attempt['result'] != 'success' or is_known(attempt['raw_ref']) and is_known(attempt['http_status'])
                    and 200 <= attempt['http_status']['value'] < 300 and attempt['error']['status'] == 'not_applicable',
                    'D04', '/result', 'Successful request lacks actual successful bytes/status')
            require(attempt['result'] == 'success' or is_known(attempt['error']), 'D04', '/error', 'Failed request lost its actual reason')
        require(used == raw_refs.keys(), 'D04', '/raw_refs', 'Audit contains unaccounted source captures')
        for task in self.rows['tasks']:
            attempts = sorted(grouped[task['task_id']], key=lambda item: item['sequence'])
            require([row['sequence'] for row in attempts] == list(range(1, len(attempts) + 1))
                    and len(attempts) <= self.spec['per_request_attempts'], 'D04', '/sequence', 'Retries were dropped or exceeded frozen budget')
            actual = [self.read(ref, PREFIX + 'attempt_row') for ref in task['attempt_refs']]
            require(actual == attempts, 'D04', '/attempt_refs', 'Task attempt list differs from actual preserved attempts')

    def dispositions(self):
        for row in self.rows['dispositions']:
            tasks = [self.indexes['tasks'][key] for key in row['task_ids'] if key in self.indexes['tasks']]
            require(tasks and len(tasks) == len(row['task_ids']) and all(task['object_id'] == row['object_id']
                    and task['capability'] == row['capability'] and task['object_ref'] == row['object_ref'] for task in tasks),
                    'D05', '/task_ids', 'Disposition changed original object or capability')
            require(row['rule_ref'] in [task['source_rule_ref'] for task in tasks], 'D05', '/rule_ref', 'Disposition uses an unregistered rule')
            if row['disposition'] in ('ready', 'proven_not_applicable'):
                require(row['citations'] and row['evidence_refs'], 'D05', '/disposition', 'Ready or N-A lacks located original evidence', 'missing_evidence')
            if row['disposition'] == 'ready':
                require(not row['missing_fields'], 'D05', '/missing_fields', 'Ready capability still contains missing fields')
                require(any(attempt['task_id'] in row['task_ids'] and attempt['result'] == 'success' for attempt in self.rows['attempts']),
                        'D05', '/disposition', 'Ready capability has no successful actual source request', 'missing_evidence')
            for citation in row['citations']:
                self.quote(citation)

    def numbers(self):
        for row in self.rows['observations']:
            verify_conversion(row)
        for contract in self.rows['contracts']:
            fees = contract['fees']
            for fee in fees:
                require(Fraction(fee['amount_lower']) >= 0 and (not is_known(fee['amount_upper'])
                        or Fraction(fee['amount_upper']['value']) > Fraction(fee['amount_lower'])), 'D06', '/fees', 'Fee amount interval is invalid')
                for field in ('rate', 'fixed_amount'):
                    require(not is_known(fee[field]) or Fraction(fee[field]['value']) >= 0, 'D06', '/fees', 'Fee value is negative')
                require(not is_known(fee['holding_days_upper']) or fee['holding_days_upper']['value'] > fee['holding_days_lower'],
                        'D06', '/fees', 'Fee holding-day interval is invalid')
                if fee['calculation'] in ('external_subscription_ratio', 'redemption_gross_ratio', 'included_in_nav'):
                    require(is_known(fee['rate']), 'D06', '/rate', 'Ratio fee requires a disclosed rate', 'missing_evidence')
                if fee['calculation'] == 'fixed_per_trade':
                    require(is_known(fee['fixed_amount']) and is_known(fee['currency']), 'D06', '/fixed_amount', 'Fixed fee amount/currency is unknown', 'missing_evidence')
                if fee['calculation'] == 'documented_formula':
                    require(contract['completeness'] != 'ready', 'D06', '/formula', 'Free documented formula has no registered executable calculator')
            for left, right in itertools.combinations(fees, 2):
                if (left['charge'], left['calculation'], left['day_count'], left['currency']) != (right['charge'], right['calculation'], right['day_count'], right['currency']):
                    continue
                if any(item[field]['status'] == 'unknown' for item in (left, right) for field in ('amount_upper', 'holding_days_upper')):
                    continue
                def interval(item, lower, upper):
                    return Fraction(str(item[lower])), Fraction(str(item[upper]['value'])) if is_known(item[upper]) else None
                a, b = interval(left, 'amount_lower', 'amount_upper'), interval(right, 'amount_lower', 'amount_upper')
                c, d = interval(left, 'holding_days_lower', 'holding_days_upper'), interval(right, 'holding_days_lower', 'holding_days_upper')
                amount_overlap = (a[1] is None or b[0] < a[1]) and (b[1] is None or a[0] < b[1])
                days_overlap = (c[1] is None or d[0] < c[1]) and (d[1] is None or c[0] < d[1])
                require(not (amount_overlap and days_overlap), 'D06', '/fees', 'Fee tiers overlap for the same charge')
            for term in contract['terms']:
                require(not (is_known(term['value']) and is_known(term['text_value'])), 'D06', '/terms', 'Term simultaneously claims numeric and textual values')
                require(not is_known(term['value']) or is_known(term['unit']), 'D06', '/terms', 'Numeric contract term lacks explicit units')

    def times(self):
        for row in self.rows['observations']:
            for field in ('published_time', 'available_time', 'observation_period'):
                evidence = row[field]
                time_upper(evidence)
                if is_known(evidence):
                    self.read(evidence['value']['basis_ref'])
            available_at(row['available_time'], row['availability_bound_at'], self.ctx.input['information_cutoff_at'])
            if row['availability_basis'] == 'current_capture_only':
                require(is_known(row['availability_bound_at']) and instant(row['availability_bound_at']['value']) == instant(row['retrieved_at']),
                        'D07', '/availability_bound_at', 'Current capture was backdated to an economic or NAV date')
            if not is_known(row['available_time']) or not is_known(row['availability_bound_at']):
                require(any(gap['field_or_object'] in (row['version_id'], '/observations/' + row['version_id'])
                            or row['version_id'] in gap['field_or_object'] for gap in self.gaps),
                        'D07', '/availability_bound_at', 'Unknown observation availability lacks a specific Gap')
            seen, current = set(), row
            while is_known(current['supersedes_version_id']):
                previous = current['supersedes_version_id']['value']
                require(previous in self.indexes['observations'] and previous not in seen, 'D07', '/supersedes_version_id', 'Revision chain is cyclic or unresolved')
                seen.add(previous)
                old = self.indexes['observations'][previous]
                require(all(value_equal(old[field], current[field]) for field in ('unit', 'currency'))
                        and period_key(old['observation_period']) == period_key(current['observation_period'])
                        and all(old[field] == current[field] for field in ('entity_id', 'metric_id', 'measurement_basis')),
                        'D07', '/supersedes_version_id', 'Revision changed measurement identity or semantic units')
                current = old
        for row in self.rows['availability']:
            if row['object_kind'] in ('universe', 'identity', 'exposure'):
                self.inherited_availability(row)
                continue
            require(row['eligible'] == available_at(row['available_time'], row['availability_bound_at'], row['origin_at']),
                    'D07', '/eligible', 'Availability audit eligibility differs from actual evidence')
            require(row['eligible'] or row['gap_ids'], 'D07', '/gap_ids', 'Unavailable object lacks a specific Gap')

    def current(self):
        cutoff = self.ctx.input['information_cutoff_at']
        entities = {row['entity_id']['value'] for row in self.members if is_known(row['entity_id'])}
        entity_by_member = {row['member_id']: row['entity_id']['value'] for row in self.members if is_known(row['entity_id'])}
        entity_by_member.update({row['member_id']: row['entity_id']['value'] for row in self.history if is_known(row['entity_id'])})
        unresolved_risk = False
        for risk in self.risks:
            if risk['risk_kind'] == 'cash_comparator':
                continue
            member = risk['member_id']['value'] if is_known(risk['member_id']) else None
            if member in entity_by_member:
                entities.add(entity_by_member[member])
            else:
                unresolved_risk = True
        represented = {row['entity_id'] for row in self.rows['market']}
        require(entities <= represented, 'D08', '/current_market_snapshot', 'A known original entity lacks current disposition', 'missing_evidence')
        ready = True
        for row in self.rows['market']:
            require(row['cutoff_at'] == cutoff, 'D08', '/cutoff_at', 'Current snapshot moved its information cutoff')
            if row['current_ready'] != 'ready':
                require(row['gap_ids'] or row['current_ready'] == 'proven_not_applicable', 'D08', '/gap_ids', 'Unknown current state lacks Gap')
                ready = False
                continue
            require(is_known(row['market_state']) and row['observation_version_ids'], 'D08', '/market_state', 'Current-ready state lacks actual market data', 'missing_evidence')
            for version in row['observation_version_ids']:
                require(version in self.indexes['observations'], 'D08', '/observation_version_ids', 'Current version is unresolved')
                observation = self.indexes['observations'][version]
                require(available_at(observation['available_time'], observation['availability_bound_at'], cutoff, 'D08'),
                        'D08', '/observation_version_ids', 'Current input was unavailable at C', 'missing_evidence')
        require(ready and self.rows['market'] and not unresolved_risk, 'D08', '/current_market_snapshot',
                'Current market capability or original mandatory risk identity is incomplete', 'missing_evidence')

    def historical(self):
        require(self.history and self.rows['panels'], 'D09', '/universe_history_ref', 'Historical membership or panels are unavailable', 'missing_evidence')
        origins = self.protocol['origin_rule']['origins']
        require(len(origins) == len(set(origins)) and sorted(origins, key=instant) == origins
                and all(instant(value) <= instant(self.ctx.input['information_cutoff_at']) for value in origins),
                'D09', '/origins', 'Historical origins are duplicated, unordered or after C')
        expected_panels = {(row['origin_at'], row['entity_id']['value']) for row in self.history
                           if row['membership_status'] == 'candidate' and is_known(row['entity_id'])}
        require(expected_panels <= {(row['origin_at'], row['entity_id']) for row in self.rows['panels']},
                'D09', '/asof_panel', 'A historical candidate entity/origin was silently omitted', 'missing_evidence')
        for panel in self.rows['panels']:
            if panel['origin_at'] == self.ctx.input['information_cutoff_at']:
                continue
            matching = [row for row in self.history if row['origin_at'] == panel['origin_at']
                        and row['universe_version_id'] == panel['universe_version_id'] and is_known(row['entity_id'])
                        and row['entity_id']['value'] == panel['entity_id']]
            require(matching and all(row['membership_status'] == 'candidate' and is_known(row['availability_bound_at'])
                    and instant(row['availability_bound_at']['value']) <= instant(panel['origin_at']) for row in matching),
                    'D09', '/universe_version_id', 'Historical panel is not supported by original PIT membership', 'missing_evidence')
            require(is_known(panel['identity_version_ref']) and all(row['identity_version_ref'] == panel['identity_version_ref']
                    and row['exposure_version_refs'] == panel['exposure_version_refs'] for row in matching),
                    'D09', '/identity_version_ref', 'Panel changed original historical identity or exposure evidence')
            audit_objects = [('universe', panel['universe_version_id'])]
            identity = self.read(panel['identity_version_ref']['value'], 'investment.sc04.identity')
            audit_objects.append(('identity', identity['identity_id']))
            for reference in panel['exposure_version_refs']:
                exposure = self.read(reference)
                require('exposure_id' in exposure, 'D09', '/exposure_version_refs', 'Exposure reference does not identify an exact source object')
                audit_objects.append(('exposure', exposure['exposure_id']))
            audit_objects += [('observation', key) for key in panel['source_version_ids']]
            audit_objects += [('contract', key) for key in panel['contract_version_ids']]
            for kind, identifier in audit_objects:
                proofs = [row for row in self.rows['availability'] if row['object_kind'] == kind and row['object_id'] == identifier
                          and row['origin_at'] == panel['origin_at']]
                require(len(proofs) == 1 and proofs[0]['eligible'], 'D09', '/availability_audit',
                        'Historical source, identity, exposure or universe lacks a unique eligible availability audit', 'missing_evidence')
            require(is_known(panel['availability_bound_at']) and instant(panel['availability_bound_at']['value']) <= instant(panel['origin_at']),
                    'D09', '/availability_bound_at', 'Historical panel availability is unknown or after origin', 'missing_evidence')
            for version in panel['source_version_ids']:
                selected = self.indexes['observations'][version]
                require(available_at(selected['available_time'], selected['availability_bound_at'], panel['origin_at'], 'D09'),
                        'D09', '/source_version_ids', 'Historical feature used an unavailable revision')
                for alternate in self.rows['observations']:
                    same = all(alternate[key] == selected[key] for key in ('entity_id', 'metric_id', 'measurement_basis'))
                    same = same and period_key(alternate['observation_period']) == period_key(selected['observation_period'])
                    if not same or alternate['version_id'] == version:
                        continue
                    if available_at(alternate['available_time'], alternate['availability_bound_at'], panel['origin_at'], 'D09'):
                        require(instant(alternate['availability_bound_at']['value']) <= instant(selected['availability_bound_at']['value']),
                                'D09', '/source_version_ids', 'Panel ignored a later genuinely available revision')

    def calendar_schedules(self):
        for row in self.rows['calendar']:
            period = verify_scheduled_period(row)
            evidence = row['scheduled_valuation_period']
            if evidence['status'] == 'unknown':
                require(any(row['calendar_version_id'] in gap['field_or_object'] for gap in self.gaps),
                        'D10', '/scheduled_valuation_period', 'Unknown valuation schedule lacks a calendar-specific Gap')
                continue
            upper = time_upper(row['available_time'], 'D10')
            require(upper is not None and upper <= instant(self.ctx.input['information_cutoff_at']),
                    'D10', '/available_time', 'Valuation arrangement was not available at C', 'missing_evidence')
            require(row['citations'], 'D10', '/scheduled_valuation_period', 'Valuation arrangement lacks original citations', 'missing_evidence')
            for citation in row['citations']:
                self.quote(citation)
            if period is not None:
                require(evidence['value']['basis_ref'] in [item['raw_ref'] for item in row['citations']],
                        'D10', '/scheduled_valuation_period', 'Scheduled period basis is not an exact cited calendar source')
                self.read(evidence['value']['basis_ref'])

    def financial_terms(self):
        self.calendar_schedules()
        required_terms = {'nav_precision', 'share_precision', 'money_precision', 'order_cutoff', 'subscription_confirmation_lag',
                          'redemption_confirmation_lag', 'settlement_lag', 'lock_period', 'subscription_status', 'redemption_status', 'rounding_rule'}
        require(self.rows['contracts'] and self.rows['calendar'], 'D10', '/contract_vintages_ref', 'Contract or trading calendar is unavailable', 'missing_evidence')
        for row in self.rows['dispositions']:
            if row['capability'] in ('fees', 'subscription_redemption', 'precision', 'lock_confirmation_settlement', 'calendar', 'equity_events'):
                require(row['disposition'] != 'unknown', 'D10', '/source_dispositions', 'Required financial capability remains unknown', 'missing_evidence')
        for contract in self.rows['contracts']:
            require(contract['completeness'] == 'ready' and not contract['missing_fields'], 'D10', '/completeness', 'Financial contract is incomplete', 'missing_evidence')
            require(required_terms <= {row['term'] for row in contract['terms']}, 'D10', '/terms', 'Required execution, precision or settlement term is absent', 'missing_evidence')
            require(is_known(contract['effective_from']) and time_upper(contract['available_time'], 'D10') is not None,
                    'D10', '/effective_from', 'Contract effective or availability time is unknown', 'missing_evidence')
        for panel in self.rows['panels']:
            for key in panel['contract_version_ids']:
                contract = self.indexes['contracts'][key]
                require(contract['entity_id'] == panel['entity_id'] and time_upper(contract['available_time'], 'D10') <= instant(panel['origin_at'])
                        and time_upper(contract['effective_from'], 'D10') <= instant(panel['origin_at'])
                        and (contract['effective_to']['status'] == 'not_applicable' or is_known(contract['effective_to'])
                             and instant(contract['effective_to']['value']['lower_bound']) > instant(panel['origin_at'])),
                        'D10', '/contract_version_ids', 'Contract is not historically available and effective')

    def label_values(self):
        require(self.rows['labels'], 'D11', '/label_events', 'No target labels are available', 'missing_evidence')
        horizons = {row['horizon_id']: row for row in self.protocol['horizons']}
        for label in self.rows['labels']:
            require(label['target_id'] == self.protocol['target_definition']['target_id'] and label['horizon_id'] in horizons,
                    'D11', '/target_id', 'Label changed target or horizon')
            panel = self.indexes['panels'][label['sample_id']]
            require(label['entity_id'] == panel['entity_id'] and label['origin_at'] == panel['origin_at']
                    and instant(label['outcome_end_at']) > instant(label['origin_at']), 'D11', '/origin_at', 'Label sample or horizon interval differs')
            horizon = horizons[label['horizon_id']]
            if horizon['unit'] == 'calendar_day' and horizon['end_rule'] == 'exact_calendar_boundary':
                require(instant(label['outcome_end_at']) == instant(label['origin_at']) + timedelta(days=horizon['length']),
                        'D11', '/outcome_end_at', 'Label end differs from registered calendar horizon')
            else:
                require(is_known(label['end_version_id']), 'D11', '/end_version_id', 'Trading horizon lacks an observed endpoint', 'missing_evidence')
                period = self.indexes['observations'][label['end_version_id']['value']]['observation_period']
                upper = time_upper(period, 'D11')
                require(upper is not None, 'D11', '/observation_period', 'Trading endpoint economic time is unknown', 'missing_evidence')
                try:
                    timezone = ZoneInfo(period['value']['timezone'])
                except ZoneInfoNotFoundError as error:
                    raise ContractError('D11', '/timezone', 'Source timezone database is unavailable', 'missing_evidence') from error
                origin_date = instant(label['origin_at']).astimezone(timezone).date()
                endpoint_date = instant(label['outcome_end_at']).astimezone(timezone).date()
                calendar = {row['date']: row for row in self.rows['calendar'] if row['entity_id'] == label['entity_id']
                            and row['calendar_id'] == horizon['calendar_id']}
                current_date, trading_days = origin_date + timedelta(days=1), []
                while current_date <= endpoint_date:
                    key = current_date.isoformat()
                    require(key in calendar and is_known(calendar[key]['is_trading_day']), 'D11', '/calendar_ref',
                            'Trading horizon has an undocumented calendar day', 'missing_evidence')
                    row = calendar[key]
                    available = time_upper(row['available_time'], 'D11')
                    require(available is not None and available <= instant(label['origin_at']), 'D11', '/calendar_ref',
                            'Trading schedule was not actually available at the historical origin', 'missing_evidence')
                    if row['is_trading_day']['value']:
                        trading_days.append(current_date)
                    current_date += timedelta(days=1)
                if horizon['unit'] == 'trading_day':
                    require(len(trading_days) == horizon['length'] and trading_days[-1] == endpoint_date,
                            'D11', '/outcome_end_at', 'Trading endpoint differs from its documented trading-day count')
                else:
                    boundary = origin_date + timedelta(days=horizon['length'])
                    eligible = [day for day in trading_days if day >= boundary]
                    require(eligible and eligible[0] == endpoint_date, 'D11', '/outcome_end_at',
                            'Calendar horizon did not use the next documented trading boundary')
                require(instant(period['value']['lower_bound']) <= instant(label['outcome_end_at']) <= upper,
                        'D11', '/outcome_end_at', 'Trading horizon endpoint is outside its source economic time interval')
                scheduled = verify_scheduled_period(calendar[endpoint_date.isoformat()], 'D11')
                if scheduled is not None:
                    require(scheduled == period_key(period), 'D11', '/scheduled_valuation_period',
                            'Origin-known valuation schedule and actual endpoint use different economic periods', 'missing_evidence')
            if not is_known(label['outcome']):
                require(label['label_status'] != 'mature' and label['gap_ids'], 'D11', '/outcome', 'Missing target was marked mature or lacks Gap')
                continue
            require(is_known(label['start_version_id']) and is_known(label['end_version_id'])
                    and Fraction(label['outcome']['value']) >= -1, 'D11', '/outcome', 'Known label lacks endpoints or violates wealth support')
            label_dependencies(label, self.indexes['observations'], self.rows['events'], self.indexes['contracts'], require_declared=True)
            for field, endpoint in (('start_version_id', label['origin_at']), ('end_version_id', label['outcome_end_at'])):
                source = self.indexes['observations'][label[field]['value']]
                verify_endpoint_period(source, endpoint)
            actual_events = [row for row in self.rows['events'] if row['entity_id'] == label['entity_id']]
            expected = reinvested_return(label, self.indexes['observations'], actual_events, self.indexes['contracts'])
            places = self.protocol['target_definition']['numerical_precision']['decimal_places']
            require(Fraction(label['outcome']['value']) == quantized(expected, places), 'D11', '/outcome', 'Target differs from independent reinvested wealth reconstruction')

    def maturity(self):
        for label in self.rows['labels']:
            if label['label_status'] != 'mature':
                require(not is_known(label['outcome']), 'D12', '/outcome', 'Immature target exposes a future numeric outcome')
                continue
            require(mature_for_fit(label, self.ctx.input['information_cutoff_at']), 'D12', '/label_available_at', 'Label is not mature at C')
            upper = label_availability_upper(label, self.indexes['observations'], self.rows['events'], self.indexes['contracts'])
            require(instant(label['label_available_at']['value']) == upper,
                    'D12', '/label_available_at', 'Label maturity is not the maximum required source upper bound')
        for assignment in self.rows['assignments']:
            require(assignment['label_id'] in self.indexes['labels'], 'D12', '/label_id', 'Partition label is unresolved')
            label = self.indexes['labels'][assignment['label_id']]
            require(all(assignment[key] == label[key] for key in ('sample_id', 'origin_at', 'outcome_end_at', 'label_available_at')),
                    'D12', '/assignments', 'Partition changed label identity or maturity')
            if assignment['partition'] == 'training':
                require(mature_for_fit(label, assignment['fit_cutoff_at']), 'D12', '/fit_cutoff_at', 'Training contains a label published after the fit cutoff')
                require(self.indexes['panels'][label['sample_id']]['eligible_for_training'],
                        'D12', '/sample_id', 'Training assignment includes a panel declared ineligible')

    def partitions(self):
        rule = self.protocol['split_rule']
        folds = {row['fold_id']: row for row in rule['folds']}
        require(len(folds) == len(rule['folds']), 'D13', '/folds', 'Duplicate fold ID')
        keys = [(row['label_id'], row['fold_id']) for row in self.rows['assignments']]
        require(len(keys) == len(set(keys)) and set(keys) == set(itertools.product(self.indexes['labels'], folds)),
                'D13', '/assignments', 'Per-horizon label/fold dispositions are missing or duplicated')
        require(instant(rule['selection_information_cutoff_at']) <= instant(rule['independent_origin_start']),
                'D13', '/selection_information_cutoff_at', 'Selection information was opened after independent evaluation began')
        for fold in folds.values():
            require(instant(fold['train_origin_start']) <= instant(fold['train_origin_end']) < instant(fold['evaluate_origin_start'])
                    <= instant(fold['evaluate_origin_end']) and instant(fold['fit_cutoff_at']) <= instant(fold['evaluate_origin_start']),
                    'D13', '/folds', 'Forward fold violates past-only temporal order')
        grouped = defaultdict(list)
        for row in self.rows['assignments']:
            require(row['fold_id'] in folds, 'D13', '/fold_id', 'Partition invented a fold')
            fold = folds[row['fold_id']]
            require(row['fit_cutoff_at'] == fold['fit_cutoff_at'], 'D13', '/fit_cutoff_at', 'Partition changed frozen fit cutoff')
            grouped[row['fold_id']].append(row)
            if row['partition'] == 'training':
                require(instant(fold['train_origin_start']) <= instant(row['origin_at']) <= instant(fold['train_origin_end']),
                        'D13', '/origin_at', 'Training sample is outside its frozen origin interval')
            elif row['partition'] in ('selection', 'independent_validation'):
                require(fold['purpose'] == row['partition'] and instant(fold['evaluate_origin_start']) <= instant(row['origin_at']) <= instant(fold['evaluate_origin_end']),
                        'D13', '/partition', 'Evaluation sample changed frozen fold purpose or interval')
                require(mature_for_fit(self.indexes['labels'][row['label_id']], self.ctx.input['information_cutoff_at']),
                        'D13', '/partition', 'Evaluation contains an immature label')
            if row['partition'] == 'independent_validation':
                require(instant(rule['independent_origin_start']) <= instant(row['origin_at']) <= instant(rule['independent_origin_end']),
                        'D13', '/independent_validation', 'Independent label lies outside the frozen interval')
            if row['partition'] == 'selection':
                require(is_known(row['label_available_at']) and instant(row['label_available_at']['value']) <= instant(rule['selection_information_cutoff_at'])
                        and not overlaps(row['origin_at'], row['outcome_end_at'], rule['independent_origin_start'], rule['independent_origin_end']),
                        'D13', '/selection', 'Selection label interval or publication crosses the independent information boundary')
        for items in grouped.values():
            evaluations = [row for row in items if row['partition'] in ('selection', 'independent_validation')]
            for training in (row for row in items if row['partition'] == 'training'):
                require(all(not overlaps(training['origin_at'], training['outcome_end_at'], row['origin_at'], row['outcome_end_at']) for row in evaluations),
                        'D13', '/training', 'Training label overlaps a closed evaluation label interval')
                if self.protocol['purge_embargo_rule']['embargo_rule'] == 'documented_dependency_window':
                    days = self.protocol['purge_embargo_rule']['dependency_window_days']
                    require(all(instant(training['outcome_end_at']) + timedelta(days=days) < instant(row['origin_at']) for row in evaluations),
                            'D13', '/dependency_window_days', 'Registered dependency window was not applied to actual label intervals')

    def views_check(self):
        views = self.views
        forbidden_labels = set(views['independent_validation']['label_ids'])
        require(not forbidden_labels.intersection(views['training']['label_ids'] + views['selection']['label_ids']),
                'D14', '/label_ids', 'Independent label is also exposed through a producer data view')
        for name, view in views.items():
            require(view['partition'] == name and view['protocol_ref'] == self.bundle['experiment_protocol_ref']
                    and self.split[name + '_view_ref'] == self.bundle[name + '_view_ref'], 'D14', '/data_view', 'View changed frozen protocol or partition')
            permitted = {'SC08'} if name == 'independent_validation' else {'SC06', 'SC07', 'SC08'}
            require(set(view['permitted_stages']) <= permitted and (name != 'independent_validation' or set(view['permitted_stages']) == {'SC08'}),
                    'D14', '/permitted_stages', 'View exposes independent data to a producer role')
            tables = {field: self.read(view[field])['rows'] for field in VIEW_TABLES}
            labels = tables['label_events_ref']
            expected = {row['label_id'] for row in self.rows['assignments'] if row['partition'] == name} if name != 'current' else set()
            require(set(view['label_ids']) == expected == {row['label_id'] for row in labels},
                    'D14', '/label_ids', 'Physical view labels differ from exact approved assignments')
            samples = {row['sample_id'] for row in labels} if name != 'current' else {row['sample_id'] for row in self.rows['panels'] if row['origin_at'] == self.ctx.input['information_cutoff_at']}
            require(set(view['sample_ids']) == samples == {row['sample_id'] for row in tables['asof_panel_ref']},
                    'D14', '/sample_ids', 'Physical panel differs from projection of approved label IDs')
            for field, original_name, key in (('label_events_ref', 'labels', 'label_id'), ('asof_panel_ref', 'panels', 'sample_id')):
                require(all(self.indexes[original_name][row[key]] == row for row in tables[field]), 'D14', '/data_view', 'Filtered rows changed original values')
            dependencies = [label_dependencies(row, self.indexes['observations'], self.rows['events'], self.indexes['contracts']) for row in labels]
            versions = view_observation_ids(tables['asof_panel_ref'], labels, self.indexes['observations'], self.rows['events'], self.indexes['contracts'])
            require(versions == {row['version_id'] for row in tables['observation_vintages_ref']},
                    'D14', '/observation_vintages_ref', 'Physical view exposes unused or future observation versions')
            for row in tables['observation_vintages_ref']:
                require(self.indexes['observations'][row['version_id']] == row, 'D14', '/observation_vintages_ref', 'View modified a source observation')
            event_ids = {key for row in labels for key in row['equity_event_ids']} | {key for dependency in dependencies for key in dependency['events']}
            contract_ids = {key for row in labels + tables['asof_panel_ref'] for key in row['contract_version_ids']}
            contract_ids |= {key for dependency in dependencies for key in dependency['contracts']}
            contract_ids |= {row['contract_version_id'] for row in tables['equity_events_ref']}
            require(event_ids == {row['event_id'] for row in tables['equity_events_ref']}
                    and contract_ids == {row['contract_version_id'] for row in tables['contract_vintages_ref']},
                    'D14', '/data_view', 'View exposes unused equity or contract rows')
            for field, original_name, key in (('equity_events_ref', 'events', 'event_id'), ('contract_vintages_ref', 'contracts', 'contract_version_id')):
                require(all(row[key] in self.indexes[original_name] and self.indexes[original_name][row[key]] == row for row in tables[field]),
                        'D14', '/data_view', 'View changed an equity or contract source row')
            if name in ('training', 'selection', 'current'):
                require(all(view[field]['uri'] != self.bundle[field]['uri'] and view[field]['uri'] != views['independent_validation'][field]['uri'] for field in VIEW_TABLES),
                        'D14', '/data_view', 'Data views are not physically separated')
        self.permission_check()

    def permission_check(self):
        permitted_grants = [{'schema_id': PREFIX + 'data_view', 'reference_paths': ['/' + name for name in VIEW_TABLES]},
                            {'schema_id': PREFIX + 'experiment_protocol', 'reference_paths': ['/model_search_space/predictors/*/source_rule_ref']}]
        for stage in ('SC06', 'SC07'):
            roots = [self.bundle[name + '_view_ref'] for name in ('training', 'selection', 'current')]
            resolver = ResearchResolver(self.ctx.root, self.ctx.schemas, mode=self.ctx.mode, allowed_refs=roots, reference_grants=permitted_grants)
            for name in ('training', 'selection', 'current'):
                view = resolver.resolve(self.bundle[name + '_view_ref'])
                for field in VIEW_TABLES:
                    resolver.resolve(view[field])
            prohibited = [self.bundle['independent_validation_view_ref'], self.bundle['source_acquisition_audit_ref']]
            prohibited += [self.bundle[field] for field in VIEW_TABLES]
            prohibited += self.audit['raw_refs']
            for ref in prohibited:
                try:
                    resolver.read(ref['uri'])
                except ContractError as error:
                    require(error.code == 'use_not_allowed', 'D14', '/read', 'Permission probe failed for a reason other than denied access')
                else:
                    raise ContractError('D14', '/read', stage + ' can read an independent or ancestor raw artifact')
                try:
                    resolver.resolve(ref)
                except ContractError as error:
                    require(error.code == 'use_not_allowed', 'D14', '/resolve', 'Permission probe failed without access denial')
                else:
                    raise ContractError('D14', '/resolve', stage + ' can resolve prohibited data')
        independent = ResearchResolver(self.ctx.root, self.ctx.schemas, mode=self.ctx.mode,
                                       allowed_refs=[self.bundle['independent_validation_view_ref']], reference_grants=permitted_grants)
        independent.resolve(self.bundle['independent_validation_view_ref'])

    def protocol_check(self):
        protocol = self.protocol
        predictors = protocol['model_search_space']['predictors']
        require(len({row['feature_id'] for row in predictors}) == len(predictors), 'D15', '/predictors', 'Predictor IDs are duplicated')
        for definition in predictors:
            rule = self.source_rules[definition['source_rule_ref']['sha256']]
            require(rule in self.spec['source_rules'], 'D15', '/source_rule_ref', 'Predictor SourceRule differs from frozen acquisition specification')
            fields = [row for row in rule['field_rules'] if row['field_name'] == definition['field_name']]
            require(len(fields) == 1 and fields[0]['time_role'] == 'not_temporal'
                    and definition['field_name'] not in rule['identity_fields'] and definition['feature_id'] == definition['field_name'],
                    'D15', '/predictors', 'Predictor is an identity/time field or lacks a unique frozen field')
        parameters = {row['parameter_id']: row for row in protocol['parameter_specs']}
        require(len(parameters) == len(protocol['parameter_specs']), 'D15', '/parameter_specs', 'Duplicate parameter ID')
        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key.endswith('_parameter_id') or key == 'parameter_id':
                        require(value in parameters, 'D15', '/' + key, 'Protocol parameter reference is unresolved')
                    elif key.endswith('parameter_ids'):
                        require(set(value) <= parameters.keys(), 'D15', '/' + key, 'Protocol parameter list is unresolved')
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(protocol)
        def bound_parameter(owner, id_field, value, unit=None):
            parameter = parameters[owner[id_field]]
            registered_value = parameter['value']['value']
            same = canonical(registered_value) == canonical(value)
            numeric = lambda item: isinstance(item, (str, int)) and not isinstance(item, bool)
            if numeric(registered_value) and numeric(value):
                same = Fraction(str(registered_value)) == Fraction(str(value))
            elif isinstance(registered_value, list) and isinstance(value, list):
                same = list(map(Fraction, registered_value)) == list(map(Fraction, value))
            require(is_known(parameter['value']) and same
                    and (unit is None or parameter['unit'] == unit), 'D15', '/' + id_field,
                    'Executed protocol value differs from its registered parameter or unit')
        for grid in protocol['model_search_space']['numeric_search']:
            bound_parameter(grid, 'parameter_id', grid['values'])
        for owner, field, value_field, unit in (
                (protocol['scenario_method'], 'block_length_parameter_id', 'block_length', 'count'),
                (protocol['scenario_method'], 'draws_parameter_id', 'draws', 'count'),
                (protocol['statistical_assumptions']['paired_evaluation'], 'block_length_parameter_id', 'block_length', 'count'),
                (protocol['statistical_assumptions']['paired_evaluation'], 'replications_parameter_id', 'replications', 'count'),
                (protocol['target_definition']['numerical_precision'], 'parameter_id', 'decimal_places', 'count'),
                (protocol['repeated_test_policy']['allocation_precision'], 'parameter_id', 'decimal_places', 'count'),
                (protocol['purge_embargo_rule'], 'parameter_id', 'dependency_window_days', 'day'),
                (protocol['randomness_policy'], 'seed_parameter_id', 'seed', None)):
            bound_parameter(owner, field, owner[value_field], unit)
        horizons = {row['horizon_id'] for row in protocol['horizons']}
        metrics = {row['metric_id']: row for row in protocol['metrics']}
        require(len(horizons) == len(protocol['horizons']) and len(metrics) == len(protocol['metrics']), 'D15', '/metrics', 'Metric or horizon IDs are duplicated')
        for metric in metrics.values():
            require(metric['horizon_id'] in horizons, 'D15', '/horizon_id', 'Metric horizon is unresolved')
        for acceptance in protocol['acceptance_rules']:
            require(acceptance['metric_id'] in metrics and metrics[acceptance['metric_id']]['quantity'] != 'effective_sample_size',
                    'D15', '/acceptance_rules', 'Acceptance references unknown metric or unsupported ESS')
            parameter = parameters[acceptance['threshold_parameter_id']]
            require(value_equal(acceptance['threshold'], parameter['value']) and parameter['unit'] == metrics[acceptance['metric_id']]['unit'],
                    'D15', '/threshold', 'Acceptance threshold changed its source value, unknown state or metric unit')
        repeated = protocol['repeated_test_policy']
        claims = repeated['claims']
        require(0 < Fraction(repeated['family_error_budget']) < 1 and len({row['claim_id'] for row in claims}) == len(claims),
                'D15', '/claims', 'Claim IDs or family error budget are invalid')
        allocation = repeated['allocation_rule']
        if allocation['algorithm_id'] == 'bonferroni_equal_registered_family':
            require(allocation['family_size'] == len(claims) and allocation['ordered_claim_ids'] == [row['claim_id'] for row in claims],
                    'D15', '/allocation_rule', 'Finite family does not bind the complete ordered claim list')
        acceptance_ids = {row['rule_id'] for row in protocol['acceptance_rules']}
        baselines = {row['baseline_id'] for row in protocol['baseline_definitions']}
        for claim in claims:
            require(claim['metric_id'] in metrics and claim['horizon_id'] in horizons and claim['baseline_id'] in baselines
                    and metrics[claim['metric_id']]['horizon_id'] == claim['horizon_id']
                    and set(claim['acceptance_rule_ids']) <= acceptance_ids, 'D15', '/claims', 'Claim metric, horizon, baseline or acceptance reference is unresolved')

    def features(self):
        for panel in self.rows['panels']:
            verify_features(panel, self.protocol, self.indexes['observations'], self.source_rules)
            if panel['eligible_for_training']:
                require(selected_predictors(self.protocol, panel['entity_id']) and all(is_known(row['value']) for row in panel['feature_values']),
                        'D16', '/eligible_for_training', 'Panel with missing or no applicable predictors claims readiness')
        searches = {row['name']: row for row in self.protocol['model_search_space']['numeric_search']}
        require({'alpha', 'l1_ratio', 'max_iterations', 'convergence_tolerance'} <= searches.keys(), 'D16', '/numeric_search', 'Required solver or model grid is absent')
        for name, row in searches.items():
            values = [Fraction(value) for value in row['values']]
            require(len(values) == len(set(values)) and all(value > 0 for value in values)
                    and (name != 'l1_ratio' or all(value <= 1 for value in values))
                    and (name != 'max_iterations' or all(value.denominator == 1 for value in values)),
                    'D16', '/numeric_search', 'Solver or model search values are invalid')
        declared = [(Fraction(row['alpha']), Fraction(row['l1_ratio'])) for row in self.protocol['model_search_space']['candidate_order']]
        expected = set(itertools.product(map(Fraction, searches['alpha']['values']), map(Fraction, searches['l1_ratio']['values'])))
        require(len(declared) == len(set(declared)) and set(declared) == expected, 'D16', '/candidate_order', 'Candidate order omits or repeats grid combinations')

    def gap_check(self):
        inherited = gap_index(self.inherited)
        require(inherited.keys() <= gap_index(self.gaps).keys(), 'D17', '/data_gaps_ref', 'Candidate dropped or rewrote an inherited Gap')
        require(gap_index(self.audit['gaps']).keys() <= self.gap_map.keys(), 'D17', '/source_acquisition_audit/gaps', 'Acquisition Gap was omitted from current DataGaps')
        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == 'gap_ids':
                        require(len(value) == len(set(value)) and all(re.fullmatch(r'g-[0-9a-f]{64}', item) and item in self.gap_map for item in value),
                                'D17', '/gap_ids', 'Gap foreign key is malformed, duplicated or unresolved')
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        for value in self.values.values():
            walk(value)
        pending = {row['task_id'] for row in self.rows['pending']}
        unknown = {key for row in self.rows['dispositions'] if row['disposition'] == 'unknown' for key in row['task_ids']}
        require(unknown <= pending and pending <= self.indexes['tasks'].keys(), 'D17', '/pending_tasks', 'Unresolved original tasks were dropped or invented')

    def quote(self, citation):
        source = self.read(citation['raw_ref'])
        return located_text(citation, source, self.ctx.attempt_dir)

    def inherited_availability(self, row):
        """只沿本轮公开SC04结果回链，不重做目录、身份或暴露业务验收。"""
        def identity(reference):
            return tuple(reference[key] for key in ARTIFACT_FIELDS)
        evidence = {identity(reference) for reference in row['evidence_refs']}
        candidates = []
        history_ref = self.bundle['universe_history_ref']['value'] if is_known(self.bundle['universe_history_ref']) else None
        histories = [item for item in self.history if item['origin_at'] == row['origin_at']]
        if row['object_kind'] == 'universe':
            matching = [item for item in histories if item['universe_version_id'] == row['object_id']]
            for item in matching:
                reference = item['source_vintage_ref']
                source = self.read(reference, 'investment.sc04.catalog_vintage')
                bounds = [source['availability_bound_at'], *(item['availability_bound_at'] for item in matching)]
                candidates.append((reference, source, bounds, [history_ref, reference]))
            if row['origin_at'] == self.ctx.input['information_cutoff_at']:
                discovery = self.read(self.universe['discovery_audit_ref'])
                snapshot_ref = discovery['universe_ref']
                snapshot = self.read(snapshot_ref, 'investment.sc04.universe_snapshot')
                if snapshot['universe_id'] == row['object_id']:
                    catalog = snapshot['catalog_vintage_ref']
                    if is_known(catalog):
                        reference = catalog['value']
                        source = self.read(reference, 'investment.sc04.catalog_vintage')
                        candidates.append((reference, source, [source['availability_bound_at']], [snapshot_ref, reference]))
                    else:
                        unknown = {'status': 'unknown', 'value': None, 'reason': catalog['reason']}
                        candidates.append((snapshot_ref, {'available_time': unknown}, [unknown], [snapshot_ref]))
        elif row['object_kind'] == 'identity':
            references = [item['identity_version_ref']['value'] for item in histories if is_known(item['identity_version_ref'])]
            if row['origin_at'] == self.ctx.input['information_cutoff_at']:
                references += [item['identity_ref']['value'] for item in self.members if is_known(item['identity_ref'])]
            for reference in references:
                source = self.read(reference, 'investment.sc04.identity')
                if source['identity_id'] == row['object_id']:
                    candidates.append((reference, source, [source['availability_bound_at']], [reference]))
        else:
            for item in histories:
                for reference in item['exposure_version_refs']:
                    source = self.read(reference)
                    if source.get('exposure_id') == row['object_id']:
                        candidates.append((reference, source, [source['availability_bound_at']], [reference]))
            if row['origin_at'] == self.ctx.input['information_cutoff_at']:
                reference = self.universe['exposure_links_ref']
                for source in self.read(reference)['rows']:
                    if source['exposure_id'] == row['object_id']:
                        candidates.append((reference, source, [source['availability_bound_at']], [reference]))
        candidates = [item for item in candidates if identity(item[0]) in evidence]
        require(candidates, 'D18', '/evidence_refs', 'Inherited object has no exact source reference in this verified upstream scope', 'missing_evidence')
        selected = {}
        for reference, source, bounds, required_refs in candidates:
            require(all(ref is not None and identity(ref) in evidence for ref in required_refs),
                    'D18', '/evidence_refs', 'Inherited availability omitted an actual source or history reference')
            bound = max(bounds, key=lambda value: instant(value['value'])) if all(is_known(value) for value in bounds) else {
                'status': 'unknown', 'value': None, 'reason': 'A required upstream availability bound is unknown'}
            key = canonical([source['available_time'], bound['status'], bound['value']])
            selected[key] = source, bound
        require(len(selected) == 1, 'D18', '/evidence_refs', 'Inherited source versions have conflicting availability evidence')
        source, bound = next(iter(selected.values()))
        use = 'current_account_review' if row['origin_at'] == self.ctx.input['information_cutoff_at'] else 'pit_training'
        upstream = self.upstream['instrument_universe_ref']
        verify_inherited_availability(row, source, bound, use in upstream['allowed_uses'])

    def event_dependencies(self, event):
        """正式事件审核获得实际NAV/合同原行与必要原文，不只审核裸ID。"""
        related, sources = [], []
        references = [('contract_vintages_ref', 'contracts', 'contract_version_id', event['contract_version_id'])]
        if is_known(event['reinvestment_nav_version_id']):
            references.append(('observation_vintages_ref', 'observations', 'version_id', event['reinvestment_nav_version_id']['value']))
        for field, name, key, identifier in references:
            matches = [(index, value) for index, value in enumerate(self.rows[name]) if value[key] == identifier]
            require(len(matches) == 1, 'D18', '/' + key, 'Event relation does not resolve to one exact original row')
            index, value = matches[0]
            reference, location = self.bundle[field], '/rows/' + str(index)
            require(pointer(self.read(reference), location) == value, 'D18', '/related_objects', 'Event relation slice changed source bytes')
            related.append({'original_ref': reference, 'original_pointer': location, 'value': value})
            sources.extend(source_references(value))
        return related, sources

    def task_context(self, identifiers):
        tasks = [self.indexes['tasks'][key] for key in identifiers]
        contexts, rules = {}, {}
        for task in tasks:
            reference = task['object_ref']
            original = self.read(reference)
            if 'rows' in original:
                identifier_field = {'original_member': 'member_id', 'mandatory_asset': 'risk_id',
                                    'economic_subject': 'observation_id'}[task['object_kind']]
                matches = [(index, row) for index, row in enumerate(original['rows']) if row[identifier_field] == task['object_id']]
                require(matches, 'D18', '/object_context', 'Task has no exact original object row')
                for index, row in matches:
                    location = '/rows/' + str(index)
                    contexts[reference['uri'], location] = {'original_ref': reference, 'original_pointer': location, 'value': row}
                    identity = row.get('identity_ref', row.get('identity_version_ref'))
                    if is_known(identity):
                        identity_ref = identity['value']
                        contexts[identity_ref['uri'], ''] = {'original_ref': identity_ref, 'original_pointer': '',
                                                             'value': self.read(identity_ref, 'investment.sc04.identity')}
                    if task['object_kind'] == 'mandatory_asset' and is_known(row['member_id']):
                        for member_index, member in enumerate(self.members):
                            if member['member_id'] == row['member_id']['value']:
                                member_ref = self.universe['members_ref']
                                member_pointer = '/rows/' + str(member_index)
                                contexts[member_ref['uri'], member_pointer] = {'original_ref': member_ref, 'original_pointer': member_pointer, 'value': member}
            else:
                require(task['object_kind'] == 'benchmark' and is_known(original['benchmark_id'])
                        and original['benchmark_id']['value'] == task['object_id'], 'D18', '/object_context', 'Benchmark task is not bound to its actual original Identity')
                contexts[reference['uri'], ''] = {'original_ref': reference, 'original_pointer': '', 'value': original}
            rule_ref = task['source_rule_ref']
            rules[rule_ref['sha256']] = self.read(rule_ref, PREFIX + 'source_rule')
        return tasks, [rules[key] for key in sorted(rules)], [contexts[key] for key in sorted(contexts)]

    def source_review(self):
        targets = []
        for field, name in TABLES.items():
            if name == 'panels' or name == 'labels':
                continue
            targets.extend((self.bundle[field], '/rows/' + str(index), row) for index, row in enumerate(self.rows[name]))
        targets.extend((self.audit['source_dispositions_ref'], '/rows/' + str(index), row) for index, row in enumerate(self.rows['dispositions']))
        targets.extend((self.role_ref('data_build_spec_ref'), '/source_rules/' + str(index), row) for index, row in enumerate(self.spec['source_rules']))
        require(targets, 'D18', '/review_targets', 'No actual source interpretation targets exist', 'missing_evidence')
        for original_ref, original_pointer, row in targets:
            require(pointer(self.read(original_ref), original_pointer) == row, 'D18', '/original_pointer', 'Review slice does not reconstruct exact original row')
            if row.get('object_kind') in ('universe', 'identity', 'exposure') and 'audit_id' in row:
                self.inherited_availability(row)
                continue
            sources = source_references(row)
            related_objects = []
            if 'event_id' in row:
                related_objects, relation_sources = self.event_dependencies(row)
                sources.extend(relation_sources)
                sources = list({ref['uri']: ref for ref in sources}.values())
            if 'snapshot_id' in row:
                versions = set(row['observation_version_ids'])
                if is_known(row['nav_version_id']):
                    versions.add(row['nav_version_id']['value'])
                for version in sorted(versions):
                    require(version in self.indexes['observations'], 'D18', '/nav_version_id', 'Current snapshot source version is unresolved')
                    sources.extend(source_references(self.indexes['observations'][version]))
                if is_known(row['contract_version_id']):
                    require(row['contract_version_id']['value'] in self.indexes['contracts'], 'D18', '/contract_version_id', 'Current snapshot contract is unresolved')
                    sources.extend(source_references(self.indexes['contracts'][row['contract_version_id']['value']]))
                sources = list({ref['uri']: ref for ref in sources}.values())
            tasks = [task['task_id'] for task in self.rows['tasks'] if task['object_id'] == row.get('object_id')
                     or task['task_id'] == row.get('task_id') or self.read(task['source_rule_ref']) == row]
            if not tasks:
                raw_uris = {ref['uri'] for ref in sources}
                tasks = sorted({attempt['task_id'] for attempt in self.rows['attempts'] if is_known(attempt['raw_ref']) and attempt['raw_ref']['value']['uri'] in raw_uris})
            if not tasks and 'entity_id' in row:
                objects = {member['member_id'] for member in self.members if is_known(member['entity_id']) and member['entity_id']['value'] == row['entity_id']}
                objects |= {risk['risk_id'] for risk in self.risks if is_known(risk['member_id']) and risk['member_id']['value'] in objects}
                tasks = sorted({task['task_id'] for task in self.rows['tasks'] if (task['object_id'] in objects
                                or task['object_kind'] == 'benchmark' and task['object_id'] == row['entity_id'])
                                and ('snapshot_id' not in row or task['capability'] == 'current_market')})
                sources = list({ref['uri']: ref for ref in [*sources, *(attempt['raw_ref']['value'] for attempt in self.rows['attempts']
                                if attempt['task_id'] in tasks and is_known(attempt['raw_ref']))]}.values())
            tasks = sorted(set(tasks))
            require(tasks, 'D18', '/task_ids', 'Source interpretation is not bound to any actual acquisition task')
            if 'source_rule_id' in row:
                tasks = tasks[:1]
            task_context, rules, object_context = self.task_context(tasks)
            key = 'review-' + hashlib.sha256(canonical([original_ref, original_pointer, row])).hexdigest()
            object_id = next((row[name] for name in ('version_id', 'event_id', 'contract_version_id', 'snapshot_id', 'disposition_id', 'audit_id', 'source_rule_id') if name in row), key)
            target = {'work_id': key, 'task_ids': tasks, 'object_id': object_id, 'original_ref': original_ref,
                      'original_pointer': original_pointer, 'field_paths': semantic_paths(row), 'source_refs': sources, 'value': row,
                      'task_context': task_context, 'source_rules': rules, 'object_context': object_context, 'related_objects': related_objects}
            reference = self.ctx.put_json('validation-' + key + '.json', target, PREFIX + 'review_target')
            evidence = [reference, *sources]
            response = self.ctx.host_response('financial_source_review', work_key=key)
            if response is None:
                return self.ctx.request_host('financial_source_review', evidence, work_key=key)
            require(response['work_id'] == key and response['task_ids'] == tasks and response['reviewed_refs'] == [reference],
                    'D18', '/reviewed_refs', 'Independent source review changed its exact batch')
            findings = response['findings']
            require([item['field_path'] for item in findings] == target['field_paths']
                    and all(item['object_id'] == object_id and item['rule_id'] == 'D18' for item in findings),
                    'D18', '/findings', 'Independent reviewer omitted, reordered or changed a required field')
            for finding in findings:
                value = pointer(target, finding['field_path'])
                for citation in finding['citations']:
                    require(citation['raw_ref'] in sources, 'D18', '/raw_ref', 'Independent reviewer cited outside the exact batch')
                    self.quote(citation)
                require(finding['verdict'] == 'passed', 'D18', finding['field_path'], finding['reason'],
                        'missing_evidence' if finding['verdict'] == 'insufficient_evidence' else 'business_invariant')
                require(all(item in self.gap_map for item in finding['gap_ids']), 'D18', '/gap_ids', 'Reviewer invented an unresolved Gap reference')
                if finding['field_path'] == '/value/scheduled_valuation_period' and value['status'] != 'unknown':
                    supporting = ([value['value']['basis_ref']] if is_known(value)
                                  else [item['raw_ref'] for item in row['citations']])
                    require(any(item['raw_ref'] in supporting for item in finding['citations']),
                            'D18', finding['field_path'], 'Valuation schedule lacks a finding citation to its exact calendar basis', 'missing_evidence')
                if isinstance(value, dict) and (is_known(value) or finding['field_path'] == '/value/scheduled_valuation_period'
                                               and value['status'] == 'not_applicable'):
                    require(finding['citations'], 'D18', finding['field_path'], 'Known source fact lacks located original evidence', 'missing_evidence')
        return None

    def economic_registration(self):
        protocol = self.protocol
        parameters = {row['parameter_id']: row for row in protocol['parameter_specs']}
        account = protocol['wealth_definition']['initial_account']
        capital = parameters[account['capital_parameter_id']]
        require(is_known(capital['value']) and Fraction(str(capital['value']['value'])) == Fraction(account['initial_capital'])
                and capital['unit'] == 'CNY' and capital['source_kind'] == 'registered_method'
                and instant(account['registered_at']) <= instant(protocol['registered_at']),
                'D19', '/initial_account', 'Standard experiment capital differs from its declared method assumption')
        risk = protocol['wealth_definition']['risk_definition']
        for key, unit in (('reference_wealth_parameter_id', 'CNY'), ('cvar_beta_parameter_id', 'ratio'),
                          ('risk_budget_parameter_id', 'CNY'), ('probability_budget_parameter_id', 'ratio')):
            parameter = parameters[risk[key]]
            require(parameter['unit'] == unit, 'D19', '/' + key, 'Risk parameter units differ')
            if key == 'cvar_beta_parameter_id' and is_known(parameter['value']):
                require(0 < Fraction(str(parameter['value']['value'])) < 1, 'D19', '/' + key, 'CVaR beta is outside (0,1)')
            if key in ('risk_budget_parameter_id', 'probability_budget_parameter_id'):
                require(parameter['source_kind'] == ('user_confirmation' if risk['risk_budget_kind'] == 'user_confirmation' else 'registered_method'),
                        'D19', '/' + key, 'Experimental risk assumption was relabeled as user preference')
        paired = protocol['statistical_assumptions']['paired_evaluation']
        confidence = parameters[paired['family_confidence_parameter_id']]
        require(confidence['unit'] == 'ratio' and (not is_known(confidence['value'])
                or Fraction(str(confidence['value']['value'])) == 1 - Fraction(protocol['repeated_test_policy']['family_error_budget'])),
                'D19', '/family_confidence_parameter_id', 'Statistical confidence was confused with CVaR beta or family alpha')
        methods = [row['diagnostic_method']['method_id'] for row in protocol['statistical_assumptions']['requirements']]
        require({'documented_assumption_review', 'ordered_origin_block_inventory', 'empirical_loss_tail_inventory'} <= set(methods),
                'D19', '/statistical_assumptions', 'Required conditional assumption and empirical inventories are not registered')

    def receipt(self):
        if any(row['verdict'] == 'failed' for row in self.checks.values()):
            self.result('D20', 'failed', 'Candidate contains a disproved assertion, changed identity or access violation')
        for rule in RULES:
            if rule not in self.checks:
                self.result(rule, 'insufficient_evidence', 'Required prerequisite was not established')
        checks = [self.checks[rule] for rule in RULES]
        critical = all(self.checks[rule]['verdict'] == 'passed' for rule in self.ctx.definition['critical_rules'])
        allowed = [row['use'] for row in self.ctx.definition['use_rules'] if critical
                   and all(self.checks[rule]['verdict'] == 'passed' for rule in row['required_pass_rules'])]
        gaps = list(getattr(self, 'gaps', self.inherited))
        for check in checks:
            if check['verdict'] != 'passed':
                affected = [row['use'] for row in self.ctx.definition['use_rules'] if check['rule_id'] in row['required_pass_rules']]
                gaps.append({'rule_id': check['rule_id'], 'field_or_object': '/checks/' + check['rule_id'], 'reason': check['reason'],
                             'owner': 'validator', 'required_evidence': ['source_bytes'], 'affected_uses': affected or ['reporting']})
        failed = any(row['verdict'] == 'failed' for row in checks)
        partial = any(row['verdict'] == 'insufficient_evidence' for row in checks)
        result = {'validator_id': 'sc05-validator-v1', 'validator_version': 'v1', 'run_id': self.ctx.run_id, 'stage_id': 'SC05',
                  'input_refs': self.ctx.input['input_refs'], 'candidate_output_ref': self.ctx.candidate_ref, 'validated_at': now(),
                  'checks': checks, 'overall_verdict': 'failed' if failed else 'partial' if partial else 'passed',
                  'business_result': 'rejected' if failed else 'blocked' if not allowed else 'observation_only' if partial else 'usable',
                  'allowed_uses': allowed, 'blockers': list(gap_index(gaps).values())}
        self.ctx.schemas.validate('investment.validation_receipt', result)
        return result

    def run(self):
        if not self.evaluate('D01', self.load):
            self.result('D20', 'passed', 'Receipt preserves all rules and blocks unavailable prerequisites')
            return self.receipt()
        for rule, operation in [('D01', self.bindings), ('D02', self.registration), ('D03', self.coverage),
                                ('D04', self.acquisition), ('D05', self.dispositions), ('D06', self.numbers),
                                ('D07', self.times), ('D08', self.current), ('D09', self.historical),
                                ('D10', self.financial_terms), ('D11', self.label_values), ('D12', self.maturity),
                                ('D13', self.partitions), ('D14', self.views_check), ('D15', self.protocol_check),
                                ('D16', self.features), ('D17', self.gap_check), ('D19', self.economic_registration)]:
            if self.checks.get('D01', {}).get('verdict') == 'failed':
                break
            prerequisite = 'D15' if rule in ('D16', 'D19') else None
            if prerequisite and self.checks.get(prerequisite, {}).get('verdict') != 'passed':
                self.result(rule, 'insufficient_evidence', 'Exact protocol parameter prerequisites were not established')
                continue
            self.evaluate(rule, operation)
        if not any(row['verdict'] == 'failed' for row in self.checks.values()):
            try:
                request = self.source_review()
            except ContractError as error:
                self.result('D18', 'insufficient_evidence' if error.code == 'missing_evidence' else 'failed', error.reason)
            else:
                if request is not None:
                    return request
                self.result('D18', 'passed')
        self.result('D20', 'passed', 'Exact ordered rule and frozen use gates applied independently')
        return self.receipt()


def validate(ctx):
    """返回完整独立凭证或待真实宿主执行的正式HostRequest。"""
    return Review(ctx).run()
