"""冻结规则的来源格式解析、时间证据与标准结果组装；不执行网页脚本。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import zlib

from lxml import etree, html

from investment.contracts import ContractError, canonical, digest, instant, is_known, known, require, unknown
from investment.chains.news.contracts import PENDING_DISPOSITIONS, REVIEW_USES
from investment.chains.news.protocol import CapturedResponse, CollectorContext, CollectorRequest


class LayoutUnknown(ValueError):
    """原始响应无法依固定规则形成明确材料或目录。"""


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    require(parts.scheme.lower() in ('http', 'https') and parts.hostname is not None
            and parts.username is None and parts.password is None,
            'N08', '/url', 'Source URL must be absolute HTTP(S)', 'invalid_reference')
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def identifier(prefix: str, *parts) -> str:
    return prefix + '-' + digest(canonical(list(parts)))[:40]


def decoded_entity(response: CapturedResponse, limit: int) -> bytes:
    """有界解码完整HTTP实体，拒绝未知编码、截断与串接压缩成员。"""
    encodings = [h['value'] for h in response.envelope['response_headers'] if h['name'].lower() == 'content-encoding']
    encoding = ','.join(encodings).strip().lower() or 'identity'
    raw = response.encoded_bytes
    if response.envelope['capture_state'] != 'complete':
        raise LayoutUnknown('capture_incomplete')
    if encoding == 'identity':
        if len(raw) > limit:
            raise LayoutUnknown('decoded_byte_limit')
        return raw
    if encoding not in ('gzip', 'deflate'):
        raise LayoutUnknown('content_encoding_unsupported')
    try:
        decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
        content = decoder.decompress(raw, limit + 1)
        if len(content) > limit or decoder.unconsumed_tail:
            raise LayoutUnknown('decoded_byte_limit')
        if not decoder.eof or decoder.unused_data:
            raise LayoutUnknown('compressed_entity_incomplete_or_concatenated')
        return content
    except zlib.error as error:
        raise LayoutUnknown('content_encoding_invalid') from error


def source_text(value) -> str:
    if isinstance(value, etree._Element):
        value = ' '.join(value.itertext())
    return ' '.join(str(value).split())


def json_pointer(value, locator: str):
    if locator == '':
        return value
    if not locator.startswith('/'):
        raise LayoutUnknown('json_pointer_invalid')
    for token in locator[1:].split('/'):
        key = token.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def source_tree(raw: bytes, profile: dict, fmt: str):
    text = raw.decode(profile['charset'], errors='strict')
    if fmt in ('json', 'odata', 'geojson'):
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise LayoutUnknown('duplicate_source_key')
                value[key] = item
            return value
        def invalid_constant(value):
            raise LayoutUnknown('nonfinite_source_number')
        return json.loads(text, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=invalid_constant)
    if fmt in ('xml', 'rss', 'atom'):
        return etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    tree = html.fromstring(text)
    for node in tree.xpath('//script|//style|//noscript'):
        node.drop_tree()
    return tree


def locate(tree, locator: str, json_mode=False):
    if not locator:
        return None
    try:
        if json_mode:
            return json_pointer(tree, locator)
        values = tree.xpath(locator)
        return values[0] if isinstance(values, list) and len(values) == 1 else values if not isinstance(values, list) else None
    except (KeyError, IndexError, ValueError, TypeError, etree.XPathError):
        return None


def extract_body(response: CapturedResponse, frozen, limit: int):
    profile = json.loads(frozen.profile_json)
    capture = response.envelope
    rule = next((r for r in profile['document_rules'] if re.fullmatch(r['url_pattern'], capture['final_url'])), None)
    if rule is None or rule['format'] == 'unresolved':
        raise LayoutUnknown('document_layout_unreviewed')
    try:
        raw = decoded_entity(response, limit)
        fmt = rule['format']
        tree = source_tree(raw, profile, fmt)
        title = locate(tree, rule['title_locator'], fmt == 'json')
        node = json_pointer(tree, rule['body_locator']) if fmt == 'json' else locate(tree, rule['body_locator'])
        time = locate(tree, rule['time_locator'], fmt == 'json')
        if title is None or node is None:
            raise LayoutUnknown('source_locator_missing_or_ambiguous')
        if fmt == 'json':
            if rule['body_locator'] != '':
                raise LayoutUnknown('native_record_must_be_complete')
            text = raw.decode(profile['charset'], errors='strict')
        elif fmt == 'xml':
            text = etree.tostring(node, method='c14n', with_comments=True).decode('utf-8')
        else:
            text = source_text(node)
        if not source_text(title) or not text.strip():
            raise LayoutUnknown('source_title_or_body_empty')
        body = {'source_id': capture['source_id'], 'capture_ref': response.capture_ref,
                'profile_ref': frozen.profile_ref, 'format': {'html':'html_text','xml':'xml_text','json':'json_record'}[fmt],
                'title': source_text(title), 'text': text,
                'source_time': known(source_text(time)) if time is not None and source_text(time) else unknown('source_time_missing'),
                'title_locator': rule['title_locator'], 'body_locator': rule['body_locator'], 'time_locator': rule['time_locator'],
                'native_media_type': 'application/geo+json' if rule['material_kind'] == 'event_record' else {'html':'text/html','xml':'application/xml','json':'application/json'}[fmt]}
        return body, rule, raw
    except (UnicodeError, ValueError, KeyError, TypeError, etree.Error) as error:
        raise LayoutUnknown(str(error) or 'source_layout_unknown') from error


def time_evidence(source_time: dict, rule: dict, basis_ref: dict):
    if not is_known(source_time):
        return unknown('source_time_missing')
    literal = source_time['value']
    fmt = rule['time_format']
    try:
        if fmt == 'rfc3339':
            if re.search(r'\.\d{7,}', literal):
                return unknown('source_time_precision_unsupported')
            point, precision, zone = instant(literal), 'instant', 'UTC'
        elif fmt == 'epoch_ms':
            if re.fullmatch(r'-?\d+', literal) is None:
                return unknown('source_time_format_unknown')
            point = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(literal))
            precision, zone = 'instant', 'UTC'
        else:
            if not is_known(rule['timezone']):
                return unknown('source_timezone_missing')
            formats = {'date_ymd': ('%Y-%m-%d',), 'date_dmy': ('%d/%m/%Y',), 'date_mdy': ('%m/%d/%Y',),
                       'english_date': ('%B %d, %Y', '%d %B %Y', '%d %b %Y')}
            point = None
            for date_format in formats.get(fmt, ()):
                try:
                    point = datetime.strptime(literal, date_format)
                    break
                except ValueError:
                    pass
            if point is None:
                return unknown('source_time_format_unknown')
            zone = rule['timezone']['value']
            point, precision = point.replace(tzinfo=ZoneInfo(zone)), 'date'
        upper = point + timedelta(days=1) if precision == 'date' else point
        return known({'lower_bound': utc_text(point), 'upper_bound': utc_text(upper),
                      'precision': precision, 'timezone': zone, 'basis_ref': basis_ref})
    except (ValueError, OverflowError, ContractError):
        return unknown('source_time_format_unknown')


def utc_text(point: datetime) -> str:
    return point.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def window_membership(evidence: dict, window: dict) -> str:
    if not is_known(evidence):
        return 'uncertain'
    lower, upper = (instant(evidence['value'][key]) for key in ('lower_bound', 'upper_bound'))
    start, end = instant(window['start_at']), instant(window['end_at'])
    if lower == upper:
        return 'within' if start <= lower < end else 'outside'
    if upper <= start or lower >= end:
        return 'outside'
    return 'within' if start <= lower and upper <= end else 'uncertain'


def listing_publication(literal: str, fmt: str, reference: dict):
    try:
        if fmt == 'rss' and re.search(r'(?:[+-]\d{4}|GMT|UT|UTC)$', literal):
            point = parsedate_to_datetime(literal)
        elif fmt == 'odata' and re.search(r'(?:Z|[+-]\d{2}:\d{2})$', literal) and not re.search(r'\.\d{7,}', literal):
            point = instant(literal)
        else:
            return unknown('publication_time_offset_or_format_missing')
        if point.tzinfo is None:
            return unknown('source_timezone_missing')
        return known({'lower_bound': utc_text(point), 'upper_bound': utc_text(point),
                      'precision': 'instant', 'timezone': 'UTC', 'basis_ref': reference})
    except (ValueError, TypeError, OverflowError, ContractError):
        return unknown('source_time_format_unknown')


def choose_publication(body_time: dict, listing_times: list[dict]):
    supported = ([body_time] if is_known(body_time) else []) + [time for time in listing_times if is_known(time)]
    if not supported:
        return body_time
    intervals = [(instant(t['value']['lower_bound']), instant(t['value']['upper_bound']), t) for t in supported]
    for lower, upper, evidence in sorted(intervals, key=lambda value: value[1] - value[0]):
        if all((left == lower == upper == right) if left == right else
               (left <= lower < right if lower == upper else left <= lower and upper <= right)
               for left, right, _ in intervals):
            return evidence
    return unknown('conflicting_publication_evidence')


@dataclass
class Discovery:
    """本轮实际响应暴露的机会、后续URL及目录识别状态。"""
    opportunities: dict[str, dict] = field(default_factory=dict)
    followups: dict[str, str] = field(default_factory=dict)
    recognized: set[str] = field(default_factory=set)
    layout_unknown: set[str] = field(default_factory=set)
    publication_times: dict[str, list[dict]] = field(default_factory=dict)


def discover_captures(responses: list[CapturedResponse], profile: dict, limit: int) -> Discovery:
    result = Discovery()
    for response in sorted(responses, key=lambda value: value.envelope['sequence_no']):
        capture = response.envelope
        if capture['purpose'] not in ('entry', 'listing', 'data') or not successful(response):
            continue
        try:
            raw = decoded_entity(response, limit)
        except LayoutUnknown:
            result.layout_unknown.add(capture['capture_id'])
            continue
        for follow in profile['entry_follow_rules']:
            if not re.fullmatch(follow['url_pattern'], capture['final_url']):
                continue
            try:
                if follow['locator_kind'] == 'regex':
                    values = re.findall(follow['locator'], raw.decode(profile['charset'], errors='strict'))
                else:
                    tree = source_tree(raw, profile, 'rss' if raw.lstrip().startswith(b'<?xml') else 'html')
                    values = tree.xpath(follow['locator'])
                for value in values:
                    url = normalize_url(urljoin(capture['final_url'], follow['link_prefix'] + source_text(value)))
                    result.followups.setdefault(url, follow['purpose'])
            except (ValueError, UnicodeError, etree.Error, ContractError):
                result.layout_unknown.add(capture['capture_id'])
        for rule in profile['listing_rules']:
            if re.fullmatch(rule['url_pattern'], capture['final_url']) and rule['format'] != 'unresolved':
                _listing_rows(result, response, raw, profile, rule)
    for row in result.opportunities.values():
        row['discovered_in'].sort(key=lambda value: (value['capture_ref']['uri'], value['locator']))
    return result


def successful(response: CapturedResponse) -> bool:
    capture = response.envelope
    return capture['capture_state'] == 'complete' and is_known(capture['http_status']) and 200 <= capture['http_status']['value'] < 300


def _listing_rows(result: Discovery, response: CapturedResponse, raw: bytes, profile: dict, rule: dict):
    capture = response.envelope
    json_mode = rule['format'] in ('odata', 'geojson')
    try:
        tree = source_tree(raw, profile, rule['format'])
        items = json_pointer(tree, rule['items_locator']) if json_mode else tree.xpath(rule['items_locator'])
        if not isinstance(items, list):
            raise LayoutUnknown('listing_container_changed')
        result.recognized.add(capture['capture_id'])
        if rule['next_locator']:
            next_values = [locate(tree, rule['next_locator'], True)] if json_mode else tree.xpath(rule['next_locator'])
            for value in next_values:
                if value:
                    result.followups.setdefault(normalize_url(urljoin(capture['final_url'], source_text(value))), 'listing')
        for index, item in enumerate(items):
            value = locate(item, rule['url_locator'], json_mode)
            if value is None or not source_text(value):
                result.layout_unknown.add(capture['capture_id'])
                continue
            url = normalize_url(urljoin(capture['final_url'], rule['link_prefix'] + source_text(value)))
            title = locate(item, rule['title_locator'], json_mode)
            time = locate(item, rule['time_locator'], json_mode)
            opportunity_id = identifier('opp', capture['source_id'], url)
            row = result.opportunities.setdefault(opportunity_id, {
                'opportunity_id': opportunity_id, 'source_id': capture['source_id'], 'url': url,
                'material_kind': rule['material_kind'], 'discovered_in': [],
                'listed_title': known(source_text(title)) if title is not None and source_text(title) else unknown('listing_title_missing'),
                'listed_time': known(source_text(time)) if time is not None and source_text(time) else unknown('listing_time_missing')})
            require(row['material_kind'] == rule['material_kind'], 'N08', '/opportunity_inventory', 'Conflicting source material kind')
            locator = rule['items_locator'] + '/' + str(index) if json_mode else item.getroottree().getpath(item)
            found = {'capture_ref': response.capture_ref, 'locator': locator}
            if found not in row['discovered_in']:
                row['discovered_in'].append(found)
            if rule['time_role'] == 'published' and time is not None and source_text(time):
                evidence = listing_publication(source_text(time), rule['format'], response.capture_ref)
                values = result.publication_times.setdefault(url, [])
                if is_known(evidence) and evidence not in values:
                    values.append(evidence)
    except (UnicodeError, ValueError, KeyError, IndexError, TypeError, etree.Error):
        result.layout_unknown.add(capture['capture_id'])


class SourceCollector:
    """来源策略可复用的证据累积器；调用者决定入口、发现和分页访问顺序。"""

    def __init__(self, request: CollectorRequest, context: CollectorContext, *, material_kinds=None):
        self.request, self.context = request, context
        self.frozen = context.frozen_source
        self.manifest, self.profile = json.loads(self.frozen.manifest_json), json.loads(self.frozen.profile_json)
        self.material_kinds = list(material_kinds) if material_kinds is not None else self.manifest['material_kinds']
        self.responses: dict[str, CapturedResponse] = {}
        self.bodies: dict[str, tuple[dict, dict, dict, bytes]] = {}
        self.unparsed: set[str] = set()
        self.budget_stopped = False

    async def observe(self, response: CapturedResponse):
        """接收本来源实际单hop，未知布局只保留raw，不填充正文。"""
        capture = response.envelope
        require(capture['request_id'] == self.request['request_id'] and capture['source_id'] == self.request['source_id'],
                'N04', '/capture_ref', 'Capture belongs to another source request', 'identity_mismatch')
        capture_id = capture['capture_id']
        if capture_id in self.responses:
            return
        self.responses[capture_id] = response
        if capture['purpose'] not in ('document', 'data') or not successful(response):
            return
        try:
            body, rule, raw = extract_body(response, self.frozen, self.request['limits']['decoded_byte_limit'])
        except LayoutUnknown:
            self.unparsed.add(capture_id)
            return
        body_ref = await self.context.emit_body(body)
        self.bodies[capture_id] = body, body_ref, rule, raw

    async def fetch(self, url: str, *, purpose: str):
        """逐hop取材；预算未发请求返回None，已发失败返回真实捕获。"""
        from investment.chains.news.transport import ResourceLimit
        parent, response = None, None
        while True:
            try:
                response = await self.context.fetch(url, purpose=purpose, parent_capture_id=parent)
            except ResourceLimit:
                self.budget_stopped = True
                return response
            await self.observe(response)
            capture = response.envelope
            if capture['capture_state'] != 'complete' or not is_known(capture['redirect_target']) or is_known(capture['error']):
                return response
            parent, url = capture['capture_id'], capture['redirect_target']['value']

    def discovery(self) -> Discovery:
        return discover_captures(list(self.responses.values()), self.profile, self.request['limits']['decoded_byte_limit'])

    def finish(self, *, global_capture_count: int | None = None):
        """全局汇总时再次传入最终请求总数，避免来源先结束而漏报全局预算缺口。"""
        return assemble_result(self, len(self.responses) if global_capture_count is None else global_capture_count)


def _root_response(response: CapturedResponse, by_id: dict[str, CapturedResponse]):
    while is_known(response.envelope['parent_capture_id']):
        response = by_id[response.envelope['parent_capture_id']['value']]
    return response


def assemble_result(state: SourceCollector, global_capture_count: int):
    request, profile = state.request, state.profile
    responses = sorted(state.responses.values(), key=lambda value: value.envelope['sequence_no'])
    discovery = state.discovery()
    supported, primary, documents = {}, {}, {}
    for response in responses:
        capture, capture_id = response.envelope, response.envelope['capture_id']
        if capture_id not in state.bodies:
            continue
        body, body_ref, rule, raw = state.bodies[capture_id]
        evidence = time_evidence(body['source_time'], rule, response.capture_ref)
        root = _root_response(response, state.responses)
        if rule['time_role'] == 'published':
            urls, hop = set(), response
            while True:
                urls.update((normalize_url(hop.envelope['requested_url']), normalize_url(hop.envelope['final_url'])))
                parent = hop.envelope['parent_capture_id']
                if not is_known(parent):
                    break
                hop = state.responses[parent['value']]
            listing_times = [time for url in sorted(urls) for time in discovery.publication_times.get(url, [])]
            capture_order = {r.capture_ref['uri']: r.envelope['sequence_no'] for r in responses}
            listing_times.sort(key=lambda time: capture_order[time['value']['basis_ref']['uri']])
            evidence = choose_publication(evidence, listing_times)
        document_id = identifier('doc', state.manifest['publisher_id'], normalize_url(capture['final_url']))
        version_id = 'ver-' + digest(raw)
        membership = window_membership(evidence, request['window'])
        item = {'response': response, 'requested_url': root.envelope['requested_url'],
                'document_id': document_id, 'version_id': version_id, 'membership': membership}
        supported[capture_id] = item
        key = document_id, version_id
        if key in primary:
            continue
        primary[key] = item
        if membership == 'outside' or rule['material_kind'] not in state.material_kinds:
            continue
        if not any(o['url'] == normalize_url(item['requested_url']) for o in discovery.opportunities.values()):
            continue
        available = instant(capture['response_finished_at']) <= instant(request['information_cutoff_at'])
        documents[key] = {'document_id': document_id, 'version_id': version_id,
            'publisher_id': state.manifest['publisher_id'], 'source_id': request['source_id'],
            'title': body['title'], 'material_kind': rule['material_kind'], 'requested_url': item['requested_url'],
            'final_url': capture['final_url'], 'raw_ref': response.capture_ref, 'body_ref': body_ref,
            'published_time': evidence if rule['time_role'] == 'published' else unknown('not_publication_time'),
            'window_time': evidence, 'window_time_role': rule['time_role'], 'window_membership': membership,
            'available_time': known({'lower_bound': capture['response_finished_at'], 'upper_bound': capture['response_finished_at'],
                'precision': 'instant', 'timezone': 'UTC', 'basis_ref': response.capture_ref}) if available else unknown('no_availability_by_cutoff'),
            'retrieved_at': capture['response_finished_at'], 'availability_basis': 'observed_response' if available else 'unknown_before_cutoff',
            'capture_status': 'complete'}
    document_rows = [documents[key] for key in sorted(documents)]
    opportunities = []
    for discovered in sorted(discovery.opportunities.values(), key=lambda value: value['opportunity_id']):
        row, ids = dict(discovered), []
        matches = [item for item in supported.values() if normalize_url(item['requested_url']) == row['url']]
        if row['material_kind'] not in state.material_kinds:
            disposition = 'excluded_kind'
        elif matches and all(item['membership'] == 'outside' for item in matches):
            disposition = 'excluded_outside_window'
        elif any(item['membership'] != 'outside' for item in matches):
            keys = {(item['document_id'], item['version_id']) for item in matches if item['membership'] != 'outside'}
            adopted = [doc for key, doc in documents.items() if key in keys]
            ids = sorted({doc['document_id'] for doc in adopted})
            disposition = 'captured' if any(normalize_url(doc['requested_url']) == row['url'] for doc in adopted) else 'duplicate_version'
        else:
            attempts = [r for r in responses if r.envelope['purpose'] in ('document', 'data')
                        and normalize_url(_root_response(r, state.responses).envelope['requested_url']) == row['url']]
            disposition = 'pending_fetch' if not attempts else 'pending_parse' if any(r.envelope['capture_id'] in state.unparsed for r in attempts) else 'failed_fetch'
        row.update(disposition=disposition, document_ids=ids, reason=disposition)
        opportunities.append(row)
    pending = [{'pending_id': identifier('pending', row['opportunity_id']), 'source_id': request['source_id'],
                'opportunity_id': row['opportunity_id'], 'reason_code': row['disposition'],
                'required_evidence': 'supported_layout' if row['disposition'] == 'pending_parse' else 'source_response',
                'affected_uses': list(REVIEW_USES)} for row in opportunities if row['disposition'] in PENDING_DISPOSITIONS]
    pending.sort(key=lambda value: value['pending_id'])
    coverage = source_coverage(request, profile, responses, discovery, state.unparsed,
                               document_rows, opportunities, pending, global_capture_count)
    return {'request_id': request['request_id'], 'source_id': request['source_id'], 'status': coverage['status'],
            'documents': document_rows, 'opportunities': opportunities, 'pending_items': pending,
            'coverage': coverage, 'errors': coverage['errors']}


def source_coverage(request, profile, responses, discovery, unparsed, documents, opportunities, pending, global_count):
    source_id, limits = request['source_id'], request['limits']
    captures = [response.envelope for response in responses]
    errors = []
    def error(code, message, capture_id=None):
        errors.append({'source_id': source_id, 'code': code,
                       'capture_id': known(capture_id) if capture_id else unknown('source_scope_gap'), 'message': message})
    if profile['calibration_state'] == 'unresolved':
        error('unresolved_profile', 'profile_calibration_unresolved')
    for capture in captures:
        status = capture['http_status']['value'] if is_known(capture['http_status']) else None
        if capture['capture_state'] != 'complete' or status is None or status >= 400:
            error('request_failed', capture['error']['value']['message'] if is_known(capture['error']) else 'HTTP_' + str(status), capture['capture_id'])
        if capture['capture_id'] in unparsed:
            error('layout_unknown', 'layout_unknown', capture['capture_id'])
        if capture['capture_id'] in discovery.layout_unknown:
            error('layout_unknown', 'listing_layout_unknown', capture['capture_id'])
    requested = {normalize_url(capture['requested_url']) for capture in captures}
    visited = sorted({url for url in request['entrypoints'] if normalize_url(url) in requested})
    outstanding = len(visited) < len(request['entrypoints']) or bool(set(discovery.followups) - requested) or bool(pending)
    document_count = sum(capture['purpose'] == 'document' or capture['purpose'] == 'data' and any(
        re.fullmatch(rule['url_pattern'], capture['final_url']) for rule in profile['document_rules']) for capture in captures)
    page_count = len(captures) - document_count
    stopped = outstanding and (global_count >= limits['request_budget'] or page_count >= limits['page_budget'] or document_count >= limits['document_budget'])
    if stopped:
        error('budget_stop', 'finite_resource_budget_stopped_scope')
    listing_failed = any(capture['purpose'] in ('entry', 'listing', 'data') and
        (capture['capture_state'] != 'complete' or not is_known(capture['http_status']) or capture['http_status']['value'] >= 400) for capture in captures)
    exposed = bool(discovery.recognized) and not outstanding
    policies = [rule['exhaustion_policy'] for rule in profile['listing_rules']]
    listing_state = 'error' if listing_failed else 'budget_stop' if stopped else 'exhausted_exposed' if exposed and policies and all(
        policy in ('finite_exposed', 'paginated_unknown') for policy in policies) else 'unknown'
    bounded = bounded_window(profile, responses, request['window'], limits['decoded_byte_limit'])
    complete = bounded and exposed and not errors and all(doc['window_membership'] == 'within' for doc in documents)
    if bounded and not listing_failed:
        listing_state = 'verified_window_boundary'
    if not complete:
        error('coverage_unknown', 'whole_window_not_proven')
    status = 'completed' if complete else 'partial' if documents else 'failed'
    return {'source_id': source_id, 'status': status, 'listing_state': listing_state,
            'window_coverage': 'complete' if complete else 'unknown', 'configured_entrypoints': request['entrypoints'],
            'visited_entrypoints': visited, 'capture_ids': sorted(c['capture_id'] for c in captures),
            'opportunity_ids': sorted(row['opportunity_id'] for row in opportunities),
            'document_ids': sorted({row['document_id'] for row in documents}),
            'pending_ids': sorted(row['pending_id'] for row in pending), 'errors': errors,
            'reason': 'whole_window_proven' if complete else 'whole_window_not_proven'}


def bounded_window(profile, responses, window, limit):
    proof = profile['whole_window_proof']
    if not is_known(proof):
        return False
    rule = proof['value']
    for response in responses:
        if not successful(response) or not re.fullmatch(rule['url_pattern'], response.envelope['final_url']):
            continue
        try:
            tree = source_tree(decoded_entity(response, limit), profile, rule['format'])
            json_mode = rule['format'] == 'json'
            start, end, complete = (locate(tree, rule[key], json_mode) for key in ('start_locator', 'end_locator', 'complete_locator'))
            if (complete is True or source_text(complete) == 'true') and start is not None and end is not None and (
                instant(source_text(start)) <= instant(window['start_at']) < instant(window['end_at']) <= instant(source_text(end))):
                return True
        except (ValueError, UnicodeError, KeyError, TypeError, etree.Error, ContractError):
            continue
    return False
