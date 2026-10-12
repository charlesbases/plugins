"""SC04 实际发现任务、采集预算及同attempt续行。"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from investment.contracts import ContractError, canonical, instant, is_known, known, na, now, require, unknown
from .source_production import attempt_row, evidence_links, parse_directory, retrieve, source_entry

PREFIX = 'investment.sc04.'


def identity_key(prefix, *values):
    return prefix + '-' + hashlib.sha256(canonical(values)).hexdigest()[:32]


def restore(ctx, filename, schema):
    path = ctx.attempt_dir / filename
    if not path.exists():
        return None
    reference = ctx.resolver.path_ref(path, PREFIX + schema)
    return reference, ctx.resolver.resolve(reference, PREFIX + schema)


@dataclass
class Discovery:
    tasks: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    original_lists: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    unprocessed: list = field(default_factory=list)
    captures: dict = field(default_factory=dict)
    member_evidence: dict = field(default_factory=dict)


def collect(ctx, spec, clock, group_ids, requested_uses):
    """真实目录先行；预算结束后仍登记所有已知未抓链接及来源域。"""
    result, queue = Discovery(), deque()
    owners, captured_tasks = defaultdict(set), {}
    used_scopes = set()
    first_started = None

    def unfinished(task, kind, reason, description, refs=()):
        key = identity_key('scope', task['task_id'], kind, reason, description)
        if key in used_scopes:
            return
        used_scopes.add(key)
        result.unprocessed.append({
            'scope_gap_id': key, 'source_id': task['source_id'], 'task_id': known(task['task_id']),
            'peer_group_ids': list(task['peer_group_ids']), 'scope_kind': kind,
            'scope_description': description, 'basis_refs': list(refs), 'reason': reason,
            'affected_uses': list(requested_uses),
        })

    def enqueue(source, url, capability, origin, parent=None, raw_ref=None, members=()):
        identity = (source['source_id'], url, capability, origin)
        key = identity_key('task', *identity)
        if key in result.tasks:
            owners[key].update(members)
            previous = result.tasks[key]
            if parent and capability == 'catalog' and previous['parent_task_id'] != known(parent['task_id']):
                # 分页回环也作为明确未处理机会保留，避免删除可见next或构造父子环。
                alias = identity_key('task', *identity, parent['task_id'])
                if alias not in result.tasks:
                    task = dict(previous, task_id=alias, parent_task_id=known(parent['task_id']),
                                discovery_parent_ref=known(raw_ref), task_state='pending',
                                attempt_ids=[], continuation_task_ids=[], terminal_citations=[])
                    result.tasks[alias] = task
                    parent['continuation_task_ids'].append(alias)
                    unfinished(task, 'page', 'conflict', 'Repeated discovery URL requires independent pagination reconciliation', (raw_ref,))
                return alias
            return key
        task = {
            'task_id': key, 'parent_task_id': known(parent['task_id']) if parent else na('root_discovery_task'),
            'source_id': source['source_id'], 'subject_ids': [s['subject_id'] for s in spec['required_subjects']],
            'peer_group_ids': list(group_ids), 'capability': capability, 'request_url': url,
            'query_text': na('original_source_listing_or_observed_link'),
            'page_or_cursor': known(url) if parent and capability in ('catalog', 'peer_list') else na('not_a_pagination_cursor'),
            'discovery_parent_ref': known(raw_ref) if raw_ref else na('root_task'),
            'task_state': 'pending', 'attempt_ids': [], 'continuation_task_ids': [],
            'terminal_citations': [], 'requested_origin': origin,
        }
        result.tasks[key] = task
        owners[key].update(members)
        if parent:
            parent['continuation_task_ids'].append(key)
        if parent and capability == 'catalog':
            queue.appendleft((source, task))
        else:
            queue.append((source, task))
        return key

    for source in spec['source_domains']:
        enqueue(source, source['entry_url'], 'catalog', clock['current_at'])
        for origin in clock['historical_origins']:
            # 当前URL不包含当时版本信息；仅保存待找真实档案的任务，禁止请求今天页面冒充历史。
            key = enqueue(source, source['entry_url'], 'history', origin)
            unfinished(result.tasks[key], 'historical_vintage', 'missing_history',
                       'No original archive URL or dated source vintage has been identified for ' + origin)

    while queue:
        source, task = queue.popleft()
        key = task['task_id']
        if task['capability'] == 'history':
            continue
        saved = restore(ctx, 'source-attempt-' + key + '.json', 'attempt_row')
        raw_ref, capture = None, None
        if saved is not None:
            _, attempt = saved
            require(attempt['task_id'] == key and attempt['requested_url'] == task['request_url'],
                    'U05', '/attempts', 'Persisted retrieval differs from the frozen discovery task')
            if is_known(attempt['raw_ref']):
                raw_ref = attempt['raw_ref']['value']
                capture = ctx.resolver.resolve(raw_ref, PREFIX + 'capture')
        else:
            if first_started is not None:
                remaining = spec['max_wall_seconds'] - (instant(now()) - instant(first_started)).total_seconds()
            else:
                remaining = spec['max_wall_seconds']
            if len(result.attempts) >= spec['max_requests'] or remaining <= 0:
                unfinished(task, 'page' if task['capability'] == 'catalog' else 'identity_evidence',
                           'budget_exhausted', 'Registered retrieval budget ended before requesting ' + task['request_url'])
                continue
            require(not (ctx.attempt_dir / ('source-capture-' + key + '.json')).exists(),
                    'U05', '/attempts', 'Interrupted capture has no complete attempt record; preserve it and retry as a new controller attempt')
            retrieval = retrieve(task['request_url'], max_bytes=spec['max_response_bytes'], timeout=remaining)
            if retrieval.capture is not None:
                capture = retrieval.capture
                raw_ref = ctx.put_json('source-capture-' + key + '.json', capture, PREFIX + 'capture')
            attempt = attempt_row(retrieval, task_id=key, source_id=source['source_id'],
                                  requested_url=task['request_url'], raw_ref=raw_ref)
            ctx.put_json('source-attempt-' + key + '.json', attempt, PREFIX + 'attempt_row')
        first_started = min(first_started, attempt['started_at']) if first_started else attempt['started_at']
        result.attempts.append(attempt)
        task['attempt_ids'].append(attempt['attempt_id'])
        if capture is not None:
            result.captures[raw_ref['uri']] = (raw_ref, capture)
        if attempt['outcome'] != 'retrieved':
            task['task_state'] = 'failed'
            unfinished(task, 'page' if task['capability'] == 'catalog' else 'identity_evidence',
                       'budget_exhausted' if attempt['outcome'] == 'budget_exhausted' else 'failed',
                       attempt['failure_reason']['value'], (raw_ref,) if raw_ref else ())
            continue
        task['task_state'] = 'completed'
        captured_tasks[key] = raw_ref
        try:
            if task['capability'] == 'catalog':
                page = parse_directory(capture)
                result.original_lists.append(raw_ref)
                location = urlsplit(capture['response_url'])
                source_supported = location.hostname == 'fund.eastmoney.com' and location.path == '/allfund.html'
                layout_supported = bool(page.document.xpath('//*[@id="code_content"]//ul[contains(concat(" ",normalize-space(@class)," ")," num_right ")]'))
                if not source_supported or not layout_supported:
                    unfinished(task, 'unparsed_entries', 'unsupported_layout',
                               'Original nodes are retained, but this source/layout has no established complete product-list interpretation', (raw_ref,))
                for entry in page.entries:
                    row = source_entry(entry, source_id=source['source_id'], task_id=key, raw_ref=raw_ref)
                    reference = ctx.put_json('source-entry-' + row['entry_id'] + '.json', row, PREFIX + 'source_entry')
                    member_id = row['entry_id'].replace('entry-', 'member-', 1)
                    result.entries.append((member_id, row, reference))
                    for link in entry.links:
                        if urlsplit(link).hostname == 'fundf10.eastmoney.com':
                            enqueue(source, link, 'identity', clock['current_at'], task, raw_ref, (member_id,))
                for link in page.following:
                    enqueue(source, link, 'catalog', clock['current_at'], task, raw_ref)
                if source['pagination_mode'] in ('unknown', 'cursor'):
                    unfinished(task, 'source_domain', 'unsupported_layout',
                               'Declared pagination protocol is not proven by the parsed linked-page adapter', (raw_ref,))
            elif not (capture['response_content_type'].get('value') or '').lower().startswith('application/pdf'):
                for link in evidence_links(capture):
                    enqueue(source, link, 'disclosure', clock['current_at'], task, raw_ref, owners[key])
        except (ContractError, UnicodeError, LookupError, ValueError) as error:
            task['task_state'] = 'failed'
            unfinished(task, 'unparsed_entries' if task['capability'] == 'catalog' else 'identity_evidence',
                       'unsupported_layout', str(error), (raw_ref,))

    for task in result.tasks.values():
        for child in task['continuation_task_ids']:
            owners[child].update(owners[task['task_id']])
    for key, reference in captured_tasks.items():
        for member_id in owners[key]:
            result.member_evidence.setdefault(member_id, {})[reference['uri']] = reference
    return result
