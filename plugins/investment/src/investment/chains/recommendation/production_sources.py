"""生产角色的完整工作计划与逐源实际原文支持检查。"""

import base64
import hashlib
import re
from email.message import Message

from investment.contracts import canonical, instant, is_known, require
from .production_inputs import gap, stable_id


RULES = {
    'global': ('R01', 'R02', 'R15', 'R17', 'R18'),
    'action': ('R05', 'R06', 'R07', 'R08', 'R09', 'R11', 'R14'),
    'disposition': ('R10',),
    'publication': ('R03', 'R04', 'R12', 'R13', 'R14')}


def plan_work(inputs, dispositions, checks):
    groups = {
        'global': [stable_id('g-', ['global', inputs.ctx.input_ref])],
        'action': [stable_id('a-', ['action', inputs.policy['current_actions_ref'], row['action_id']])
                   for row in inputs.actions],
        'disposition': [stable_id('d-', ['disposition', row['object_kind'], row['scope_object_id'], row['source_object_ref']])
                        for row in dispositions],
        'publication': [stable_id('p-', ['publication', inputs.refs['publication_context_ref'], row['check_id']])
                        for row in checks]}
    size = inputs.parameters['host_batch_size']
    require(type(size) is int and size > 0, 'R16', '/host_batch_size', 'Host batch size must be an explicit positive count')
    batches = [('global', 'global', groups['global'])]
    for kind in ('action', 'disposition', 'publication'):
        targets = sorted(groups[kind])
        batches.extend((f'{kind}-batch-{index // size + 1:04}', kind, targets[index:index + size])
                       for index in range(0, len(targets), size))
    return batches


def _pointer(value, pointer):
    require(pointer == '' or pointer.startswith('/'), 'R17', '/locator', 'Invalid RFC6901 source locator')
    for token in pointer.split('/')[1:] if pointer else ():
        require(re.search(r'~(?:[^01]|$)', token) is None, 'R17', '/locator', 'Invalid RFC6901 escape')
        key = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            require(key in value, 'R17', '/locator', 'Source object member does not exist')
            value = value[key]
        elif isinstance(value, list):
            require(re.fullmatch(r'0|[1-9][0-9]*', key) is not None and int(key) < len(value),
                    'R17', '/locator', 'Source array index does not exist')
            value = value[int(key)]
        else:
            require(False, 'R17', '/locator', 'Source locator traverses a scalar')
    return value


def _capture_text(capture):
    raw = base64.b64decode(capture['body_base64'], validate=True)
    require(len(raw) == capture['body_byte_size'] and hashlib.sha256(raw).hexdigest() == capture['body_sha256'],
            'R17', '/capture', 'Source capture bytes differ from their recorded identity')
    require(200 <= capture['http_status'] < 300, 'R17', '/http_status', 'Source capture is not a successful response')
    header = Message()
    header['content-type'] = capture['content_type']
    charset = header.get_content_charset() or 'utf-8'
    return raw.decode(charset, errors='strict')


def _source_clocks(inputs, reference, source, locator):
    # 每种钟只说明该种源声明；治理/登记时点不证明更早市场可得或物理到账。
    recorded = {item['observed_at'] for observation in inputs.publication['market_observations']
                for item in observation['citations'] if item['source_ref'] == reference}
    native = {
        'investment.transaction_context': 'known_as_of',
        'investment.funding_plan': 'recorded_at',
        'investment.sc07.frozen_policy': 'frozen_at',
        'investment.validation_receipt': 'validated_at',
        'investment.sc09.publication_context': 'reviewed_at'}
    field = native.get(reference['schema_id'])
    if field is not None:
        recorded.add(source[field])
    account_refs = [inputs.account[key] for key in ('trades_ref', 'positions_ref', 'pending_transactions_ref')]
    if reference in account_refs:
        recorded.add(inputs.account['known_as_of'])
    funding_refs = [inputs.funding['source_ref']]
    if is_known(inputs.funding['future_contributions']):
        funding_refs.extend(item['source_ref'] for item in inputs.funding['future_contributions']['value'])
    if reference in funding_refs:
        recorded.add(inputs.funding['recorded_at'])
    policy_refs = [inputs.policy[key] for key in
                   ('current_actions_ref', 'cash_schedule_ref', 'objective_ref', 'loss_definition_ref',
                    'constraints_ref', 'action_space_ref', 'comparison_inventory_ref')]
    if is_known(inputs.policy['selected_rule_ref']):
        policy_refs.append(inputs.policy['selected_rule_ref']['value'])
    if reference in policy_refs:
        recorded.add(inputs.policy['frozen_at'])
    engineering_refs = inputs.parameters['host_batch_size_basis_refs'] + inputs.parameters['host_work_budget_basis_refs']
    host_fields = ('host_batch_size', 'host_work_budget', 'host_parameters_determined_at',
                   'host_parameter_source_kind', 'host_batch_size_basis_refs', 'host_work_budget_basis_refs')
    host_locator = any(locator == 'json:/' + field or locator.startswith('json:/' + field + '/') for field in host_fields)
    if reference in engineering_refs or (reference == inputs.ctx.input['parameters_ref'] and host_locator):
        recorded.add(inputs.parameters['host_parameters_determined_at'])
    return recorded


def _citation(inputs, citation):
    reference = citation['source_ref']
    source = inputs.ctx.resolver.resolve(reference)
    require(instant(citation['observed_at']) <= instant(inputs.parameters['publication_checked_at']),
            'R17', '/observed_at', 'Model reading time cannot replace the original source observation time')
    if reference['schema_id'] in ('investment.sc09.capture', 'investment.sc05.capture'):
        observed = source['retrieved_at']
        material = _capture_text(source)
    else:
        material = source
        recorded = _source_clocks(inputs, reference, source, citation['locator'])
        require(any(instant(citation['observed_at']) == instant(stamp) for stamp in recorded), 'R17', '/observed_at',
                'Citation has no exact original observed-time binding', 'missing_evidence')
        observed = citation['observed_at']
    locator = citation['locator']
    if locator.startswith('json:'):
        require(isinstance(material, (dict, list)), 'R17', '/locator', 'JSON locator requires actual JSON source material')
        selected = _pointer(material, locator[5:])
        text = selected if isinstance(selected, str) else canonical(selected).decode('utf-8')
    else:
        match = re.fullmatch(r'lines:([1-9][0-9]*)-([1-9][0-9]*)', locator)
        require(isinstance(material, str) and match is not None, 'R17', '/locator',
                'Text locator must identify an actual one-based line interval')
        first, last = map(int, match.groups())
        lines = material.splitlines(keepends=True)
        require(first <= last <= len(lines), 'R17', '/locator', 'Source text line interval does not exist')
        text = ''.join(lines[first - 1:last])
    require(instant(citation['observed_at']) == instant(observed) and citation['quote'] and citation['quote'] in text,
            'R17', '/quote', 'Citation quote or source observation time is not supported by the original material')


def assess_sources(inputs, dispositions, checks, checks_ref):
    batches = plan_work(inputs, dispositions, checks)
    summary = inputs.ctx.host_work_summary()
    expected = {(role, step, key) for role, step in
                (('producer', 'publication_assessment'), ('validator', 'publication_review'))
                for key, _, _ in batches}
    registered = {(item.role, item.step_id, item.work_key) for item in summary.items}
    require(len(registered | expected) <= inputs.parameters['host_work_budget'], 'R16', '/host_work_budget',
            'Frozen host work budget cannot cover the complete original target scope', 'execution_failed')
    require(inputs.parameters['host_parameter_source_kind'] == 'engineering_budget'
            and inputs.parameters['host_batch_size_basis_refs'] and inputs.parameters['host_work_budget_basis_refs']
            and instant(inputs.parameters['host_parameters_determined_at']) <= instant(inputs.ctx.input['decision_at']),
            'R16', '/host_work_budget', 'Host engineering budget lacks its original basis or frozen determination time')
    allowed = [inputs.ctx.input_ref, inputs.ctx.input['parameters_ref'], inputs.ctx.input['scope_ref'],
               inputs.ctx.method_ref, *inputs.refs.values(), checks_ref]
    allowed.extend(reference for reference, _ in inputs.receipts.values())
    allowed.extend(inputs.parameters['host_batch_size_basis_refs'] + inputs.parameters['host_work_budget_basis_refs'])
    allowed.extend(reference for contract in inputs.contracts for reference in contract['raw_refs'])
    allowed.extend(item['source_ref'] for observation in inputs.publication['market_observations']
                   for item in observation['citations'])
    allowed.extend(inputs.publication['account_review']['source_refs'])
    allowed.extend(inputs.publication['funding_confirmation_refs'])
    allowed.extend(reference for review in inputs.publication['parameter_reviews'] for reference in review['confirmation_refs'])
    if inputs.recovery:
        allowed.extend(inputs.recovery['authorization_refs'] + inputs.recovery['constraint_evidence_refs'])
    allowed = list({canonical(reference): reference for reference in allowed}.values())
    issues = []
    for key, kind, targets in batches:
        response = inputs.ctx.host_response('publication_assessment', work_key=key)
        if response is None:
            return inputs.ctx.request_host('publication_assessment', allowed, work_key=key), issues
        require(response['work_key'] == key and response['target_ids'] == targets, 'R17', '/target_ids',
                'Publication assessment does not cover the exact current work batch')
        expected_findings = {(target, rule) for target in targets for rule in RULES[kind]}
        actual = [(item['target_id'], item['rule_id']) for item in response['findings']]
        require(len(actual) == len(set(actual)) and set(actual) == expected_findings,
                'R17', '/findings', 'Publication assessment does not exactly cover every target and applicable rule')
        issues.extend(response['gaps'])
        for finding in response['findings']:
            if finding['verdict'] == 'supported':
                require(finding['citations'], 'R17', '/citations', 'A supported factual assessment requires original source citations')
            for citation in finding['citations']:
                _citation(inputs, citation)
            issues.extend(finding['gaps'])
            if finding['verdict'] != 'supported':
                issues.append(gap(finding['rule_id'], '/assessment/' + finding['target_id'],
                                  finding['verdict'] + ': ' + finding['reason']))
    return None, issues
