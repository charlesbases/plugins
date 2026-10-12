"""真实 publication_review 工作计划、精确引用与原文核对。"""
from __future__ import annotations

import re
from hashlib import sha256

from investment.contracts import canonical, instant, is_known
from .contracts import Issue, ensure, value
from .temporal import capture_body


RULES = {
    'global': ('R01', 'R02', 'R15', 'R17', 'R18'),
    'action': ('R05', 'R06', 'R07', 'R08', 'R09', 'R11', 'R14'),
    'disposition': ('R10',),
    'publication': ('R03', 'R04', 'R12', 'R13', 'R14'),
}
PREFIX = {'global': 'g-', 'action': 'a-', 'disposition': 'd-', 'publication': 'p-'}


def target_id(kind, identity):
    return PREFIX[kind] + sha256(canonical([kind, *identity])).hexdigest()


def plan(s):
    params = s.parameters
    ensure(type(params['host_batch_size']) is int and params['host_batch_size'] > 0
           and type(params['host_work_budget']) is int and params['host_work_budget'] >= 2
           and params['host_parameter_source_kind'] == 'engineering_budget', 'Frozen host count/budget is invalid')
    ensure(params['host_batch_size_basis_refs'] and params['host_work_budget_basis_refs']
           and instant(params['host_parameters_determined_at']) <= instant(s.ctx.input['decision_at']),
           'Frozen host engineering budget lacks its actual basis or determination time', missing=True)
    targets = {kind: [] for kind in RULES}
    targets['global'] = [target_id('global', [s.ctx.input_ref])]
    targets['action'] = [target_id('action', [s.policy['current_actions_ref'], a['action_id']]) for a in s.actions]
    targets['disposition'] = [target_id('disposition', ['declared_member', m['member_id'], s.universe['members_ref']]) for m in s.members]
    targets['disposition'] += [target_id('disposition', ['registered_position', p['instrument_id'], s.account['positions_ref']]) for p in s.positions]
    targets['publication'] = [target_id('publication', [s.refs['publication_context_ref'], row['check_id']]) for row in s.tables['publication_checks']]
    result = []
    for kind, ids in targets.items():
        ensure(len(ids) == len(set(ids)), 'Duplicate target identity in original work scope')
        ids.sort()
        if kind == 'global':
            result.append(('global', ids, RULES[kind]))
            continue
        size = params['host_batch_size']
        for offset in range(0, len(ids), size):
            result.append((f'{kind}-batch-{offset // size + 1:04}', ids[offset:offset + size], RULES[kind]))
    ensure(2 * len(result) <= params['host_work_budget'], 'Frozen work budget cannot cover both roles and the original complete target set', missing=True)
    return result


def sources(s):
    refs = [s.ctx.input_ref, s.ctx.input['parameters_ref'], s.ctx.input['scope_ref'], s.ctx.method_ref,
            s.ctx.candidate_ref, s.ctx.candidate['payload_ref'], *s.refs.values(), *s.ctx.candidate['datasets'],
            *s.ctx.candidate['evidence_refs'], *s.evidence()]
    refs += [s.account[k] for k in ('trades_ref', 'positions_ref', 'pending_transactions_ref')]
    refs += [s.policy[k] for k in ('current_actions_ref', 'cash_schedule_ref', 'objective_ref', 'loss_definition_ref',
                                  'constraints_ref', 'action_space_ref', 'universe_ref', 'comparison_inventory_ref')]
    if is_known(s.policy['selected_rule_ref']):
        refs.append(s.policy['selected_rule_ref']['value'])
    refs += [s.universe['members_ref'], s.inventory['member_dispositions_ref'], s.bundle['current_market_snapshot_ref'],
             s.bundle['contract_vintages_ref'], s.funding['source_ref']]
    refs += s.parameters['host_batch_size_basis_refs'] + s.parameters['host_work_budget_basis_refs']
    refs += s.publication['account_review']['source_refs'] + s.publication['funding_confirmation_refs']
    refs += [ref for row in s.publication['parameter_reviews'] for ref in row['confirmation_refs']]
    refs += [citation['source_ref'] for row in s.publication['market_observations'] for citation in row['citations']]
    refs += [row['capture_ref']['value'] for row in s.publication['fetch_attempts'] if is_known(row['capture_ref'])]
    refs += [ref for row in s.contracts for ref in row['raw_refs']]
    if is_known(s.funding['future_contributions']):
        refs += [row['source_ref'] for row in s.funding['future_contributions']['value']]
    if s.recovery is not None:
        refs += [s.parameters['risk_recovery_review_ref']['value'], s.recovery['original_permission_ref']]
        refs += s.recovery['authorization_refs'] + s.recovery['constraint_evidence_refs']
    # 逐项 resolve 仍受当前 role grant 强制约束；不遍历 opaque receipt 的 evidence_refs。
    unique_refs = {}
    for ref in refs:
        ensure(ref['uri'] not in unique_refs or unique_refs[ref['uri']] == ref, 'Source locator has conflicting exact references')
        s.ctx.resolver.resolve(ref)
        unique_refs[ref['uri']] = ref
    return [unique_refs[key] for key in sorted(unique_refs)]


def _pointer(obj, path):
    if not path:
        return obj
    ensure(path.startswith('/'), 'Citation JSON locator is not a pointer')
    for token in path[1:].split('/'):
        ensure(re.search('~(?![01])', token) is None, 'Citation JSON pointer escape is invalid')
        key = token.replace('~1', '/').replace('~0', '~')
        if isinstance(obj, list):
            ensure(re.fullmatch('0|[1-9][0-9]*', key) is not None, 'Citation array locator is not a nonnegative RFC6901 index')
        try:
            obj = obj[int(key)] if isinstance(obj, list) else obj[key]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise Issue('Citation JSON locator does not exist in the actual original source') from error
    return obj


def citation(s, row, allowed):
    ref = row['source_ref']
    ensure(ref['uri'] in allowed and allowed[ref['uri']] == ref, 'Citation is outside the exact authorized source set')
    obj = s.ctx.resolver.resolve(ref)
    ensure(instant(row['observed_at']) <= instant(s.payload['publication_checked_at']), 'Citation observation is after the publication review')
    if ref['schema_id'] in ('investment.sc09.capture', 'investment.sc05.capture'):
        obj = capture_body(obj)
    locator = row['locator']
    if locator.startswith('json:'):
        located = _pointer(obj, locator[5:])
        text = located if isinstance(located, str) else canonical(located).decode('utf-8')
    else:
        match = re.fullmatch(r'lines:(\d+)-(\d+)', locator)
        ensure(match is not None and isinstance(obj, str), 'Citation requires an exact json: pointer or source lines:start-end locator', missing=True)
        first, last = map(int, match.groups())
        lines = obj.splitlines()
        ensure(1 <= first <= last <= len(lines), 'Citation line range is outside the actual original')
        text = '\n'.join(lines[first - 1:last])
    ensure(row['quote'] in text and row['quote'].strip(), 'Citation quote is not present at the actual source locator')
    return ref


def validate_batch(s, review, work, refs):
    key, targets, rules = work
    ensure(review['work_key'] == key and review['target_ids'] == targets, 'Review work key or exact target batch differs')
    wanted = {(target, rule) for target in targets for rule in rules}
    actual = {(row['target_id'], row['rule_id']) for row in review['findings']}
    ensure(len(actual) == len(review['findings']) and actual == wanted, 'Review omitted, duplicated or added target/rule findings')
    allowed = {ref['uri']: ref for ref in refs}
    candidate_uris = {ref['uri'] for ref in [s.ctx.candidate_ref, s.ctx.candidate['payload_ref'],
                                           *s.ctx.candidate['datasets'], *s.ctx.candidate['evidence_refs']]}
    for row in review['findings']:
        cited = [citation(s, item, allowed) for item in row['citations']]
        if row['verdict'] == 'supported':
            ensure(cited and any(ref['uri'] not in candidate_uris for ref in cited),
                   'A supported assertion lacks direct original evidence and only cites the producer')
        else:
            ensure(row['reason'] and row['gaps'] or row['verdict'] == 'contradicted' and cited,
                   'An insufficient or contradicted finding lacks its actual limitation or contrary evidence')


def request_or_reviews(s):
    work = plan(s)
    refs = sources(s)
    findings = []
    summary = s.ctx.host_work_summary()
    expected = {(role, step, key) for key, _, _ in work
                for role, step in [('producer', 'publication_assessment'), ('validator', 'publication_review')]}
    ensure(summary.registered_count() <= s.parameters['host_work_budget'], 'Actual registered host work exceeds the frozen budget')
    for key, targets, rules in work:
        review = s.ctx.host_response('publication_review', work_key=key)
        if review is None:
            summary = s.ctx.host_work_summary()
            ensure(summary.contains('validator', 'publication_review', key)
                   or summary.registered_count() < s.parameters['host_work_budget'], 'Actual pending/failed registrations exhaust the frozen host budget', missing=True)
            return s.ctx.request_host('publication_review', refs, work_key=key), None
        validate_batch(s, review, (key, targets, rules), refs)
        findings.extend(review['findings'])
    summary = s.ctx.host_work_summary()
    actual = {(item.role, item.step_id, item.work_key) for item in summary.items}
    ensure(actual == expected and all(item.state == 'completed' for item in summary.items),
           'Real same-attempt producer/validator work does not cover the full frozen plan', missing=True)
    ensure(s.payload['actual_host_work_item_count'] == summary.completed_count('producer'),
           'Candidate host count differs from actual completed producer work')
    return None, findings
