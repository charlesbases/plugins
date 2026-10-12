"""SC05 私有主动取材；只保存真实响应，金融解释经正式宿主执行。"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from investment.contracts import artifact, canonical, is_known, known, na, now, require, unknown

PREFIX = 'investment.sc05.'
PROVIDER_ID = 'evidenced_http_get'
PARSER_ID = 'financial_source_analysis'
PARSER_VERSION = 'v1'


def identifier(prefix, value):
    return prefix + '-' + hashlib.sha256(canonical(value)).hexdigest()


def unique_refs(references):
    return list({canonical(reference): reference for reference in references}.values())


def private_known(value):
    """SC05私有Optional的known.reason为null；不更改common.Optional的字符串约定。"""
    return {'status': 'known', 'value': value, 'reason': None}


def own_artifact(ctx, filename, schema_id):
    """恢复本attempt已有原字节，继续不得重新抓取并改写它。"""
    path = ctx.attempt_dir / filename
    if not path.exists():
        return None
    raw = path.read_bytes()
    uri = 'investment-run://sc05/' + path.relative_to(ctx.root / 'runs' / 'sc05').as_posix()
    reference = artifact(raw, schema_id, uri)
    reference['media_type'] = ctx.schemas.targets[schema_id]['media_type']
    ctx.resolver.allow(reference)
    return reference, ctx.resolver.resolve(reference, schema_id)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 每个重定向仍是实际HTTP请求，独立计预算并保存Location和响应。
        return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in ('a', 'link'):
            return
        self.links.extend(value for name, value in attrs if name.lower() == 'href' and value)


@dataclass
class Retrieval:
    requested_at: str
    finished_at: str
    result: str
    status: dict
    error: dict
    capture: dict | None
    redirects: list[str] = field(default_factory=list)


def allowed_url(url, rule):
    parts = urlsplit(url)
    return parts.scheme in ('http', 'https') and parts.hostname in rule['allowed_domains'] and not parts.username and not parts.password


def retrieve(rule, url, spec, *, task_id, source_type):
    """严格GET；拒绝越域跳转、超预算响应及未实现解析器身份。"""
    started = now()
    if (rule['provider_id'], rule['parser_id'], rule['parser_version']) != (PROVIDER_ID, PARSER_ID, PARSER_VERSION):
        return Retrieval(started, now(), 'unsupported_source', unknown('No HTTP request was made'),
                         private_known('Unsupported configured provider/parser implementation'), None)
    require(allowed_url(url, rule), 'D04', '/url', 'Request is outside the frozen source rule')
    opener = build_opener(_NoRedirect())
    response = None
    try:
        request = Request(url, method='GET', headers={'User-Agent': 'investment-sc05/1.0'})
        try:
            response = opener.open(request, timeout=spec['request_timeout_seconds'])
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            content_type = response.headers.get('Content-Type', 'application/octet-stream')
            body = response.read(spec['max_response_bytes'] + 1)
            location = response.headers.get('Location')
            final_url = response.geturl()
        if len(body) > spec['max_response_bytes']:
            return Retrieval(started, now(), 'parse_error', private_known(status),
                             private_known('Response exceeded max_response_bytes; partial bytes are not represented as a complete Capture'), None)
        require(allowed_url(final_url, rule), 'D04', '/final_url', 'Transport escaped the frozen source rule')
        captured_at = now()
        capture = {
            'capture_id': identifier('capture', [task_id, url, captured_at, hashlib.sha256(body).hexdigest()]),
            'task_id': task_id, 'requested_url': url, 'final_url': final_url, 'retrieved_at': captured_at,
            'http_status': status, 'content_type': content_type,
            'body_base64': base64.b64encode(body).decode('ascii'), 'body_sha256': hashlib.sha256(body).hexdigest(),
            'body_byte_size': len(body), 'source_type': source_type,
            'parser_id': rule['parser_id'], 'parser_version': rule['parser_version'],
        }
        success = 200 <= status < 300
        redirects = [urljoin(final_url, location)] if 300 <= status < 400 and location else []
        error = na('HTTP request succeeded') if success else private_known(
            'HTTP ' + str(status) + ('; Location: ' + location if location else ''))
        return Retrieval(started, now(), 'success' if success else 'http_error', private_known(status), error, capture, redirects)
    except (URLError, OSError, TimeoutError, ValueError) as error:
        return Retrieval(started, now(), 'network_error', unknown('No complete HTTP response'),
                         private_known(type(error).__name__ + ': ' + str(error)), None)


def discovered_links(capture, rule):
    """只沿已冻结来源域的原始链接；不把分页前若干条冒充完整发现。"""
    body = base64.b64decode(capture['body_base64'], validate=True)
    links = []
    media = capture['content_type'].split(';', 1)[0].strip().lower()
    if media in ('text/html', 'application/xhtml+xml'):
        charset = 'utf-8'
        for item in capture['content_type'].split(';')[1:]:
            if item.strip().lower().startswith('charset='):
                charset = item.split('=', 1)[1].strip().strip('"')
        parser = _Links()
        parser.feed(body.decode(charset, errors='strict'))
        links = parser.links
    elif media == 'application/json':
        value = json.loads(body)
        # 只识别原文显式链接关系；普通金融字符串不作URL猜测。
        if isinstance(value, dict):
            relation = value.get('links', [])
            if isinstance(relation, dict):
                relation = list(relation.values())
            if isinstance(relation, list):
                links = [item['href'] if isinstance(item, dict) else item
                         for item in relation if isinstance(item, str) or isinstance(item, dict) and isinstance(item.get('href'), str)]
    result = []
    for link in links:
        url = urldefrag(urljoin(capture['final_url'], link))[0]
        if allowed_url(url, rule) and url not in result:
            result.append(url)
    return result


@dataclass
class Acquisition:
    tasks: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    raw_refs: list = field(default_factory=list)
    groups: list = field(default_factory=list)


def collect(ctx, spec, objects):
    """完整原对象网格先登记；预算仅限制工作，不改变对象/能力全集。"""
    result = Acquisition()
    queue = []
    seen = set()
    rule_refs = {
        rule['source_rule_id']: ctx.put_json('source-rule-' + identifier('rule', rule) + '.json', rule, PREFIX + 'source_rule')
        for rule in spec['source_rules']
    }

    def enqueue(group, rule, url):
        url = urldefrag(url)[0]
        if not allowed_url(url, rule):
            return
        task_id = identifier('task', [group['object_ref'], group['object_id'], group['capability'], rule_refs[rule['source_rule_id']], url])
        if task_id not in seen:
            seen.add(task_id)
            queue.append((group, rule, url, task_id))

    for obj in objects:
        for capability in obj['capabilities']:
            rules = [rule for rule in spec['source_rules'] if rule['capability'] == capability]
            require(rules, 'D03', '/source_rules', 'Required capability has no frozen source rule: ' + capability, 'missing_evidence')
            group = {**obj, 'capability': capability, 'task_ids': [], 'task_refs': [], 'raw_refs': [],
                     'rule_refs': [rule_refs[rule['source_rule_id']] for rule in rules], 'pending_urls': [], 'discovery_errors': []}
            result.groups.append(group)
            for rule in rules:
                require(allowed_url(rule['entry_url'], rule), 'D04', '/entry_url', 'Source entry is outside its allowed domains')
                enqueue(group, rule, rule['entry_url'])
                for url in obj['source_links']:
                    enqueue(group, rule, url)

    position = 0
    while position < len(queue):
        group, rule, url, task_id = queue[position]
        position += 1
        attempts, attempt_refs = [], []
        redirected = False
        for sequence in range(1, spec['per_request_attempts'] + 1):
            filename = task_id + '-attempt-' + str(sequence) + '.json'
            existing = own_artifact(ctx, filename, PREFIX + 'attempt_row')
            if existing:
                attempt_ref, attempt = existing
                capture_ref = attempt['raw_ref']['value'] if is_known(attempt['raw_ref']) else None
                capture = ctx.resolver.resolve(capture_ref, PREFIX + 'capture') if capture_ref else None
                redirects = []
                if attempt['result'] == 'http_error' and is_known(attempt['error']) and '; Location: ' in attempt['error']['value']:
                    redirects = [urljoin(url, attempt['error']['value'].split('; Location: ', 1)[1])]
            else:
                if len(result.attempts) + len(attempts) >= spec['max_requests']:
                    break
                synthetic = urlsplit(url).hostname in ('127.0.0.1', 'localhost', '::1')
                require(not synthetic or ctx.mode == 'test', 'D04', '/source_type', 'Local fixture sources require isolated test mode')
                source_type = 'synthetic_fixture' if synthetic else 'live_public_source'
                retrieval = retrieve(rule, url, spec, task_id=task_id, source_type=source_type)
                capture = retrieval.capture
                capture_ref = ctx.put_json(task_id + '-capture-' + str(sequence) + '.json', capture, PREFIX + 'capture') if capture else None
                attempt = {'attempt_id': identifier('attempt', [task_id, sequence]), 'task_id': task_id, 'sequence': sequence,
                           'requested_at': retrieval.requested_at, 'finished_at': retrieval.finished_at, 'url': url,
                           'result': retrieval.result, 'http_status': retrieval.status,
                           'raw_ref': known(capture_ref) if capture_ref else unknown('No complete response bytes'),
                           'error': retrieval.error, 'source_type': source_type}
                attempt_ref = ctx.put_json(filename, attempt, PREFIX + 'attempt_row')
                redirects = retrieval.redirects
            attempts.append(attempt)
            attempt_refs.append(attempt_ref)
            if capture_ref:
                result.raw_refs.append(capture_ref)
                group['raw_refs'].append(capture_ref)
            for target in redirects:
                if allowed_url(target, rule):
                    enqueue(group, rule, target)
                else:
                    group['discovery_errors'].append('Redirect outside frozen domains: ' + target)
            redirected = bool(redirects)
            if attempt['result'] == 'success':
                try:
                    for target in discovered_links(capture, rule):
                        enqueue(group, rule, target)
                except (UnicodeError, LookupError, ValueError, TypeError) as error:
                    group['discovery_errors'].append(url + ': ' + type(error).__name__ + ': ' + str(error))
                break
            if redirected or attempt['result'] == 'unsupported_source':
                break
        result.attempts.extend(attempts)
        state = 'completed' if attempts and (attempts[-1]['result'] == 'success' or redirected) else 'failed' if attempts else 'pending'
        task = {'task_id': task_id, 'object_ref': group['object_ref'], 'object_id': group['object_id'],
                'object_kind': group['object_kind'], 'capability': group['capability'],
                'requested_time_scope_ref': spec['requested_time_scope_ref'], 'source_rule_ref': rule_refs[rule['source_rule_id']],
                'task_state': state, 'attempt_refs': attempt_refs}
        task_ref = ctx.put_json(task_id + '.json', task, PREFIX + 'acquisition_task_row')
        result.tasks.append(task)
        group['task_ids'].append(task_id)
        group['task_refs'].append(task_ref)
        if state != 'completed':
            group['pending_urls'].append(url)
    result.raw_refs = unique_refs(result.raw_refs)
    for group in result.groups:
        group['raw_refs'] = unique_refs(group['raw_refs'])
    return result


def analyse(ctx, acquisition):
    """按完整对象/能力工作切片调用正式分析；不得生成或预填宿主响应。"""
    material = {name: [] for name in ('observations', 'equity_events', 'contracts', 'calendar', 'dispositions', 'gaps')}
    for group in acquisition.groups:
        if not group['raw_refs']:
            reason = 'No complete response bytes were acquired for the requested object/capability; actual failed and unattempted URLs remain in the acquisition audit'
            material['dispositions'].append({
                'disposition_id': identifier('disposition', [group['object_ref'], group['object_id'], group['capability']]),
                'object_ref': group['object_ref'], 'object_id': group['object_id'], 'capability': group['capability'],
                'task_ids': group['task_ids'], 'disposition': 'unknown', 'rule_ref': group['rule_refs'][0],
                'evidence_refs': [], 'reason': reason,
                'affected_uses': ['current_account_review', 'pit_training', 'selection_comparison', 'independent_evaluation'],
                'missing_fields': sorted({field['field_name'] for reference in group['rule_refs']
                                          for field in ctx.resolver.resolve(reference, PREFIX + 'source_rule')['field_rules']}),
                'citations': [],
            })
            material['gaps'].append({
                'rule_id': 'D05', 'field_or_object': '/source_dispositions/' + group['object_id'] + '/' + group['capability'],
                'reason': reason, 'owner': 'source', 'required_evidence': ['source_bytes'],
                'affected_uses': ['current_account_review', 'pit_training', 'selection_comparison', 'independent_evaluation'],
            })
            continue
        key = identifier('analysis', [group['object_ref'], group['object_id'], group['capability'],
                                      group['task_ids'], group['raw_refs']])[:64]
        navigation = {'work_id': key, 'task_ids': group['task_ids'], 'object_id': group['object_id'],
                      'object_ref': group['object_ref'], 'capability': group['capability'],
                      'source_refs': group['raw_refs'], 'task_refs': group['task_refs'],
                      'source_rule_refs': group['rule_refs'], 'pending_urls': group['pending_urls'],
                      'discovery_errors': group['discovery_errors']}
        context_ref = ctx.put_text(key + '-context.txt', json.dumps(navigation, ensure_ascii=False, sort_keys=True),
                                   'investment.research_text')
        authority_refs = [citation['raw_ref'] for reference in group['rule_refs']
                          for citation in ctx.resolver.resolve(reference, PREFIX + 'source_rule')['authority_basis']]
        evidence = unique_refs([context_ref, *group['task_refs'], *group['rule_refs'], *group['raw_refs'],
                                *group['analysis_refs'], *authority_refs,
                                *[ctx.resolver.resolve(reference, PREFIX + 'acquisition_task_row')['requested_time_scope_ref']
                                  for reference in group['task_refs']]])
        request = ctx.request_host('financial_source_analysis', evidence, work_key=key)
        response = ctx.host_response('financial_source_analysis', work_key=key)
        if response is None:
            return request, None
        require(response['work_id'] == key and response['task_ids'] == group['task_ids']
                and response['source_refs'] == group['raw_refs'],
                'D05', '/work_id', 'Financial analysis differs from the exact source work', 'identity_mismatch')
        dispositions = response['dispositions']
        require(len(dispositions) == 1 and all(dispositions[0][field] == group[field]
                for field in ('object_id', 'object_ref', 'capability', 'task_ids'))
                and dispositions[0]['rule_ref'] in group['rule_refs'],
                'D05', '/dispositions', 'Financial analysis changed its original object/capability task', 'identity_mismatch')
        permitted = {canonical(reference) for reference in [*group['raw_refs'], *authority_refs, *group['rule_refs']]}

        def check_references(value):
            if isinstance(value, dict):
                if {'uri', 'sha256', 'schema_id'} <= value.keys():
                    require(canonical(value) in permitted, 'D05', '/source_refs',
                            'Financial analysis introduced an unprovided source reference', 'identity_mismatch')
                else:
                    for child in value.values():
                        check_references(child)
            elif isinstance(value, list):
                for child in value:
                    check_references(child)

        for name in ('observations', 'equity_events', 'contracts', 'calendar'):
            for row in response[name]:
                check_references(row)
                if group['entity_id'] is not None:
                    require(row['entity_id'] == group['entity_id'], 'D05', '/entity_id',
                            'Financial source analysis changed the frozen object entity', 'identity_mismatch')
                else:
                    require(not response[name], 'D05', '/entity_id',
                            'An unidentified original object cannot acquire a guessed entity', 'missing_evidence')
        for name in material:
            material[name].extend(response[name])
    return None, material
