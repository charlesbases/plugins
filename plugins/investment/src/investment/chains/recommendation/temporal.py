"""发布原材料、必要条件全集及来源时间交集的独立重建。"""
from __future__ import annotations

import base64
import binascii
from hashlib import sha256
import re

from investment.contracts import instant, is_known
from .contracts import ensure, value, unique, parameter_ids
from .finance import contract_for


def intersect(rows, at):
    """已核有限条件的真实交集；未知条件须由调用者先阻断。"""
    ensure(rows, 'No source-supported finite validity boundary exists', missing=True)
    latest_start = max(instant(row[1]) for row in rows)
    earliest = min(instant(row[2]) for row in rows)
    tied = [row for row in rows if instant(row[2]) == earliest]
    endpoint = 'exclusive' if any(row[3] == 'exclusive' for row in tied) else 'inclusive'
    ensure(latest_start < earliest or latest_start == earliest and endpoint == 'inclusive',
           'Necessary publication intervals have empty intersection')
    ensure(instant(at) >= latest_start and (instant(at) < earliest or instant(at) == earliest and endpoint == 'inclusive'),
           'Actual validation is outside the source-supported publication interval')
    return tied[0][2], endpoint, sorted(row[0] for row in tied)


def capture_body(capture):
    try:
        raw = base64.b64decode(capture['body_base64'], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Capture body is not valid Base64') from exc
    ensure(len(raw) == capture['body_byte_size'] and sha256(raw).hexdigest() == capture['body_sha256'],
           'Captured source body hash or length differs')
    ensure(200 <= capture['http_status'] < 300 and capture['content_type'], 'Capture is not a successful actual HTTP response')
    if 'requested_at' in capture:
        ensure(instant(capture['requested_at']) <= instant(capture['retrieved_at']), 'Capture retrieval precedes actual request')
    charset = re.search(r'charset\s*=\s*[\"\x27]?([A-Za-z0-9_-]+)', capture['content_type'], re.IGNORECASE)
    encoding = charset.group(1) if charset else 'utf-8'
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeError):
        ensure(False, 'Actual source text encoding is not established; original bytes are preserved', missing=True)


def markets(s):
    observations = unique(s.publication['market_observations'], 'entity_id')
    attempts = unique(s.publication['fetch_attempts'], 'attempt_id')
    entities = {value(a['entity_id'], 'Original action entity') for a in s.actions if is_known(a['entity_id'])}
    entities |= {row['instrument_id'] for row in s.positions}
    ensure(entities <= set(observations), 'A current action or registered position lacks its original market observation', missing=True)
    for entity in sorted(entities):
        obs = observations[entity]
        ensure(obs['platform'] == s.account['platform'], 'Market observation platform differs')
        ensure(obs['attempt_ids'], 'No actual market source fetch attempt is recorded', missing=True)
        for aid in obs['attempt_ids']:
            ensure(aid in attempts, 'Market observation references an absent fetch attempt')
            attempt = attempts[aid]
            ensure(instant(attempt['requested_at']) <= instant(attempt['completed_at']) <= instant(obs['checked_at']),
                   'Actual fetch/review times are out of order')
            if attempt['status'] == 'captured':
                ref = value(attempt['capture_ref'], 'Captured actual response')
                capture = s.ctx.resolver.resolve(ref, 'investment.sc09.capture')
                capture_body(capture)
                ensure(capture['url'] == attempt['url'] and instant(capture['retrieved_at']) <= instant(attempt['completed_at']),
                       'Capture URL or actual retrieval time differs from fetch attempt')
            else:
                ensure(attempt['capture_ref']['status'] != 'known', 'Failed fetch asserts a successful source capture')
        ensure(obs['citations'], 'Source status/contract/window claims lack original citations', missing=True)
        for action in [a for a in s.actions if is_known(a['entity_id']) and a['entity_id']['value'] == entity and a['side'] != 'hold']:
            contract = contract_for(s, action)
            ensure(value(obs['baseline_contract_version_id'], 'Market baseline contract') == contract['contract_version_id'],
                   'Market observation baseline differs from frozen contract')
            observed = value(obs['observed_contract'], 'Actually observed contract')
            ensure(observed == contract, 'Actually received contract change invalidates the frozen instruction')
            status = obs['subscription_status' if action['side'] == 'buy' else 'redemption_status']
            ensure(status != 'closed', 'Actually received trading closure invalidates the frozen instruction')
            ensure(status == 'open', 'Actual trading status is not established', missing=True)
            start = value(obs['window_start'], 'Actual submission window start')
            end = value(obs['window_end'], 'Actual submission window end')
            inclusive = value(obs['window_end_inclusive'], 'Original submission upper endpoint')
            checked = instant(s.payload['publication_checked_at'])
            ensure(instant(start) <= checked and (checked < instant(end) or inclusive and checked == instant(end)),
                   'Publication check is outside the actual submission window')


def _exact_time(optional, description):
    body = value(optional, description)
    ensure(body['precision'] == 'instant' and body['lower_bound'] == body['upper_bound'],
           description + ' is not an exact source instant; no date is converted to midnight', missing=True)
    return body['lower_bound']


def requirements(s):
    """由原依赖构建键；producer 的 required 标志不定义分母。"""
    required = {
        ('account', None, None): s.refs['transaction_context_ref'],
        ('funding', None, None): s.refs['funding_plan_ref'],
        ('qualification', None, None): s.refs['qualification_ref'],
    }
    specs = unique(s.protocol['parameter_specs'], 'parameter_id')
    ids = parameter_ids(s.objective) | parameter_ids(s.loss) | parameter_ids(s.constraints)
    for pid in ids:
        ensure(pid in specs, 'Publication parameter dependency is absent from the frozen protocol')
        if specs[pid]['source_kind'] == 'user_confirmation':
            required[('user_parameter', None, pid)] = s.parameters['experiment_protocol_ref']
    for action in s.actions:
        if not is_known(action['entity_id']):
            ensure(action['side'] == 'hold', 'An executable original action has an unknown entity', missing=True)
            continue
        aid, entity = action['action_id'], action['entity_id']['value']
        required[('market', aid, entity)] = s.bundle['current_market_snapshot_ref']
        if action['side'] != 'hold':
            for domain in ('contract', 'submission_window', 'subscription_status' if action['side'] == 'buy' else 'redemption_status'):
                required[(domain, aid, entity)] = s.bundle['contract_vintages_ref'] if domain == 'contract' else s.bundle['current_market_snapshot_ref']
    covered = {a['entity_id']['value'] for a in s.actions if is_known(a['entity_id'])}
    for position in s.positions:
        if position['instrument_id'] not in covered:
            required[('market', None, position['instrument_id'])] = s.bundle['current_market_snapshot_ref']
    if s.payload['decision_kind'] == 'risk_recovery':
        required[('risk_recovery', None, None)] = value(s.parameters['risk_recovery_review_ref'], 'Original recovery review')
    return required


def row_key(row):
    return (row['domain'], row['action_id']['value'] if is_known(row['action_id']) else None,
            row['scope_object_id']['value'] if is_known(row['scope_object_id']) else None)


def validity(s, at):
    required = requirements(s)
    rows = s.tables['publication_checks']
    unique(rows, 'check_id')
    by_key = {}
    for row in rows:
        key = row_key(row)
        by_key.setdefault(key, []).append(row)
    ensure(set(required) <= set(by_key), 'A necessary publication condition was omitted')
    ensure(all(key in required or not row['required'] for key, rows in by_key.items() for row in rows), 'Additional required condition lacks a source dependency')
    observations = unique(s.publication['market_observations'], 'entity_id')
    finite, events, unknown = [], [], []
    for key, baseline, row in [(key, baseline, row) for key, baseline in required.items() for row in by_key[key]]:
        domain, aid, entity = key
        ensure(row['required'] and row['baseline_ref'] == baseline, 'Necessary publication condition was disabled or rebound')
        ensure(instant(row['checked_at']) <= instant(s.payload['publication_checked_at']), 'Condition check is after actual publication review')
        ensure(row['outcome'] != 'changed', 'Actually received publication change: ' + row['check_id'])
        if row['outcome'] == 'unknown' or row['boundary_kind'] == 'unknown':
            unknown.append(row['check_id'])
            continue
        ensure(row['outcome'] == 'passed' and row['boundary_kind'] != 'not_applicable',
               'Necessary publication dependency was declared not applicable')
        ensure(row['observed_refs'], 'Necessary publication source is missing: ' + row['check_id'], missing=True)
        if domain == 'qualification':
            end = value(s.qualification['valid_until'], 'Original qualification validity boundary')
            start = s.receipts['qualification_ref'][1]['validated_at']
            ensure(row['boundary_kind'] == 'finite' and row['upper_endpoint'] == 'exclusive'
                   and value(row['valid_until'], 'Qualification publication upper bound') == end
                   and value(row['valid_from'], 'Qualification publication lower bound') == start,
                   'Qualification boundary or its strict original endpoint differs')
        elif domain == 'submission_window':
            obs = observations[entity]
            ensure(row['boundary_kind'] == 'finite'
                   and value(row['valid_from'], 'Submission lower bound') == value(obs['window_start'], 'Original submission start')
                   and value(row['valid_until'], 'Submission upper bound') == value(obs['window_end'], 'Original submission end')
                   and row['upper_endpoint'] == ('inclusive' if value(obs['window_end_inclusive'], 'Original window endpoint') else 'exclusive'),
                   'Publication window differs from actual source window')
        elif domain == 'contract':
            action = next(a for a in s.actions if a['action_id'] == aid)
            contract = contract_for(s, action)
            if contract['effective_to']['status'] == 'unknown':
                unknown.append(row['check_id'])
                continue
            if is_known(contract['effective_to']):
                ensure(row['boundary_kind'] == 'finite' and value(row['valid_until'], 'Contract upper bound') == _exact_time(contract['effective_to'], 'Original contract end')
                       and value(row['valid_from'], 'Contract lower bound') == _exact_time(contract['effective_from'], 'Original contract start'),
                       'Contract validity bounds differ from original source')
            else:
                ensure(row['boundary_kind'] == 'event_contingent', 'Documented open-ended contract lost its event restriction')
        elif domain == 'risk_recovery':
            review = s.recovery
            ensure(review is not None, 'Necessary recovery review missing', missing=True)
            ensure(row['boundary_kind'] == review['boundary_kind'] and row['valid_from'] == review['valid_from']
                   and row['valid_until'] == review['valid_until'], 'Recovery authorization bounds differ')
            if review['boundary_kind'] == 'finite':
                ensure(row['upper_endpoint'] == ('inclusive' if value(review['valid_until_inclusive'], 'Original recovery endpoint') else 'exclusive'),
                       'Recovery authorization endpoint differs')
        else:
            ensure(row['boundary_kind'] == 'event_contingent', 'Account/funding/parameter/status source was converted to an unsupported finite guarantee')
        if row['boundary_kind'] == 'finite':
            ensure(row['upper_endpoint'] in ('inclusive', 'exclusive'), 'Finite source upper endpoint is unknown', missing=True)
            finite.append((row['check_id'], value(row['valid_from'], 'Necessary lower bound'), value(row['valid_until'], 'Necessary upper bound'), row['upper_endpoint']))
        elif row['boundary_kind'] == 'event_contingent':
            ensure(row['reason'] and row['upper_endpoint'] == 'not_applicable' and row['valid_until']['status'] == 'not_applicable',
                   'Event restriction asserts an unsupported infinite or finite deadline')
            events.append(row['check_id'])
    if unknown or not finite:
        ensure(s.payload['valid_until']['status'] == 'unknown' and s.payload['valid_until_endpoint'] == 'unknown'
               and not s.payload['controlling_check_ids'], 'Unknown necessary source boundary was replaced by a fabricated finite validity')
        ensure(False, 'Necessary original validity boundary is unknown: ' + ', '.join(unknown), missing=True)
    end, endpoint, controls = intersect(finite, at)
    ensure(value(s.payload['valid_until'], 'Recommendation validity boundary') == end
           and s.payload['valid_until_endpoint'] == endpoint and sorted(s.payload['controlling_check_ids']) == controls,
           'Recommendation earliest boundary, endpoint or tied controlling IDs differ')
    ensure(sorted(s.payload['event_invalidation_check_ids']) == sorted(events), 'Original event invalidation conditions were omitted')
