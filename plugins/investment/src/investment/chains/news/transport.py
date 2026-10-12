"""受信 HTTP 捕获上下文：资源边界、原始实体和本轮不可覆盖的证据文件。"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import truststore

from investment.contracts import artifact, canonical, digest, known, na, now, require, unknown
from investment.chains.news.contracts import NewsResolver
from investment.chains.news.protocol import BodyArtifact, CapturedResponse, CollectorRequest, FrozenSourceConfig


class ResourceLimit(Exception):
    """尚未发请求时达到有限工程预算，不制造一次网络捕获。"""


class CaptureSession:
    """同一尝试共享客户端、全局并发、请求总预算及连续捕获序号。"""

    def __init__(self, resolver: NewsResolver, base: Path, prefix: str, *, run_id: str,
                 invocation_id: str, attempt_no: int, limits: dict, user_agent: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.resolver, self.schemas = resolver, resolver.schemas
        self.base, self.prefix = Path(base).resolve(), prefix
        expected = resolver.root / 'runs' / 'sc02'
        require(self.base.is_relative_to(expected), 'N01', '/root', 'Capture directory crosses namespace', 'use_not_allowed')
        parts = resolver.run_parts(prefix + 'capture-log.json')
        require(parts[1:3] == [invocation_id, f'attempt-{attempt_no:02}']
                and self.base == (expected.joinpath(*parts[:-1])).resolve(),
                'N03', '/attempt_no', 'Capture directory attempt mismatch', 'identity_mismatch')
        self.run_id, self.invocation_id, self.attempt_no = run_id, invocation_id, attempt_no
        self.limits, self.user_agent = dict(limits), user_agent
        self.global_slots = asyncio.Semaphore(limits['global_concurrency'])
        self.reservation = asyncio.Lock()
        self.next_sequence = 0
        self.responses: dict[int, CapturedResponse] = {}
        self.contexts: dict[str, SourceContext] = {}
        self.log_ref = None
        self.closed = False
        self.client = httpx.AsyncClient(
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            timeout=httpx.Timeout(limits['timeout_ms'] / 1000), follow_redirects=False,
            headers={'User-Agent': user_agent, 'Accept-Encoding': 'gzip, deflate'},
            limits=httpx.Limits(max_connections=limits['global_concurrency']), transport=transport)

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.client.__aexit__(exc_type, exc, traceback)

    def write(self, name: str, value: dict, schema_id: str):
        """仅写当前尝试的新文件，重复调用不能覆盖已留存原始依据。"""
        self.schemas.validate(schema_id, value)
        raw = canonical(value)
        path = self.base / name
        require(path.resolve().parent == self.base, 'N02', '/uri', 'Artifact filename escapes attempt', 'invalid_reference')
        with path.open('xb') as output:
            output.write(raw)
        return artifact(raw, schema_id, self.prefix + name)

    def context(self, request: CollectorRequest, frozen: FrozenSourceConfig):
        require(not self.closed and request['source_id'] == frozen.source_id
                and request['source_id'] not in self.contexts,
                'N03', '/source_id', 'Context identity is duplicate or mismatched', 'identity_mismatch')
        context = SourceContext(self, request, frozen)
        self.contexts[request['source_id']] = context
        return context

    def freeze_log(self):
        """所有来源结束后冻结全量账本，顺序不依赖响应完成先后。"""
        if self.log_ref is not None:
            return self.log_ref
        require(len(self.responses) == self.next_sequence, 'N04', '/captures', 'An actual request has no terminal capture')
        self.closed = True
        batch = 'batch-' + digest(canonical([self.run_id, self.invocation_id, self.attempt_no]))[:40]
        value = {'request_id': batch, 'run_id': self.run_id, 'invocation_id': self.invocation_id,
                 'attempt_no': self.attempt_no, 'state': 'frozen', 'closed_at': now(),
                 'captures': [self.responses[n].capture_ref for n in sorted(self.responses)]}
        self.log_ref = self.write('capture-log.json', value, 'investment.news_capture_log')
        return self.log_ref


class SourceContext:
    """固定来源配置、每源并发/页/文档预算与已捕获正文的写入权限。"""

    def __init__(self, session: CaptureSession, request: CollectorRequest, frozen: FrozenSourceConfig):
        self.session, self.request, self._frozen = session, json.loads(canonical(request)), frozen
        self.manifest = json.loads(frozen.manifest_json)
        self.profile = json.loads(frozen.profile_json)
        self.slots = asyncio.Semaphore(request['limits']['per_source_concurrency'])
        self.page_count = self.document_count = 0
        self.responses: dict[str, CapturedResponse] = {}
        self.bodies: dict[str, tuple[dict, dict]] = {}

    @property
    def frozen_source(self) -> FrozenSourceConfig:
        return self._frozen

    async def fetch(self, url: str, *, purpose: str, parent_capture_id: str | None = None) -> CapturedResponse:
        from investment.chains.news.parsing import normalize_url
        url = normalize_url(url)
        self.session.schemas.validate('investment.collector_request', self.request)
        require(purpose in ('entry', 'listing', 'document', 'data'), 'N04', '/purpose', 'Unknown fetch purpose')
        host = urlsplit(url).hostname
        require(host in self.manifest['allowed_hosts'], 'N08', '/requested_url', 'URL host is outside frozen source', 'use_not_allowed')
        parent, depth = None, 0
        if parent_capture_id is not None:
            parent = self.responses.get(parent_capture_id)
            require(parent is not None and parent.envelope['redirect_target'] == known(url),
                    'N04', '/parent_capture_id', 'Redirect parent/target mismatch', 'identity_mismatch')
            hop = parent
            while hop is not None:
                depth += 1
                previous = hop.envelope['parent_capture_id']
                hop = self.responses[previous['value']] if previous['status'] == 'known' else None
        if depth > self.request['limits']['redirect_budget']:
            raise ResourceLimit('redirect_budget')
        document = purpose == 'document' or purpose == 'data' and any(
            re.fullmatch(rule['url_pattern'], url) for rule in self.profile['document_rules'])
        request = self.session.client.build_request('GET', url)
        async with self.slots, self.session.global_slots:
            async with self.session.reservation:
                limits = self.request['limits']
                if self.session.closed or self.session.next_sequence >= limits['request_budget']:
                    raise ResourceLimit('request_budget')
                if (self.document_count if document else self.page_count) >= limits['document_budget' if document else 'page_budget']:
                    raise ResourceLimit('document_budget' if document else 'page_budget')
                self.session.next_sequence += 1
                sequence = self.session.next_sequence
                if document:
                    self.document_count += 1
                else:
                    self.page_count += 1
            capture_id = f'cap-{sequence:08d}'
            envelope = {'capture_id': capture_id, 'request_id': self.request['request_id'],
                        'source_id': self.request['source_id'], 'sequence_no': sequence,
                        'parent_capture_id': known(parent_capture_id) if parent else na('not_a_redirect'),
                        'purpose': purpose, 'method': 'GET', 'requested_url': url, 'final_url': url,
                        'request_headers': [], 'response_headers': [], 'http_status': unknown('no_response'),
                        'redirect_target': na('not_a_redirect'), 'request_started_at': now(),
                        'response_finished_at': '', 'capture_state': 'failed',
                        'origin': 'synthetic' if self.session.resolver.network_origin == 'simulated' else 'live',
                        'response_bytes_base64': '', 'response_sha256': '', 'response_byte_size': 0,
                        'error': unknown('response_not_finished')}
            content, cancelled = bytearray(), None
            envelope['request_headers'] = self._headers(request.headers.raw)
            response = None
            try:
                async with asyncio.timeout(limits['timeout_ms'] / 1000):
                    response = await self.session.client.send(request, stream=True)
                    envelope['http_status'] = known(response.status_code)
                    envelope['response_headers'] = self._headers(response.headers.raw)
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('location'):
                        envelope['redirect_target'] = known(normalize_url(urljoin(url, response.headers['location'])))
                    async for chunk in response.aiter_raw():
                        remaining = limits['response_byte_limit'] - len(content)
                        content.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            envelope['capture_state'] = 'partial'
                            envelope['error'] = known({'code': 'response_byte_limit', 'message': 'response_byte_limit'})
                            break
                    else:
                        envelope['capture_state'] = 'complete'
                        envelope['error'] = na('no_error')
                    if envelope['capture_state'] == 'complete':
                        if response.status_code >= 400:
                            envelope['error'] = known({'code': 'http_status', 'message': f'HTTP_{response.status_code}'})
                        elif envelope['redirect_target']['status'] == 'known' and depth >= limits['redirect_budget']:
                            envelope['error'] = known({'code': 'redirect_limit', 'message': 'redirect_budget'})
            except asyncio.CancelledError as error:
                cancelled = error
                envelope['error'] = known({'code': 'cancelled', 'message': 'request_cancelled'})
            except (TimeoutError, httpx.TimeoutException):
                envelope['error'] = known({'code': 'timeout', 'message': 'request_timeout'})
            except httpx.HTTPError as error:
                envelope['error'] = known({'code': 'network', 'message': str(error)[:1000] or type(error).__name__})
            finally:
                # 取消同样保留已经发出的请求，先固定终止时刻和实体，再释放连接。
                envelope['response_finished_at'] = now()
                if envelope['capture_state'] != 'complete' and content:
                    envelope['capture_state'] = 'partial'
                raw = bytes(content)
                envelope.update(response_bytes_base64=base64.b64encode(raw).decode('ascii'),
                                response_sha256=digest(raw), response_byte_size=len(raw))
                reference = self.session.write('capture-' + capture_id + '.json', envelope, 'investment.news_capture')
                captured = CapturedResponse(reference, envelope, raw)
                self.responses[capture_id] = self.session.responses[sequence] = captured
                if response is not None:
                    await response.aclose()
            if cancelled is not None:
                raise cancelled
            return captured

    @staticmethod
    def _headers(headers):
        return [{'name': name.decode('latin-1'), 'value': value.decode('latin-1')} for name, value in headers]

    async def emit_body(self, body: BodyArtifact):
        capture = next((r for r in self.responses.values() if r.capture_ref == body['capture_ref']), None)
        require(capture is not None and body['source_id'] == self.request['source_id']
                and body['profile_ref'] == self._frozen.profile_ref,
                'N06', '/body_ref', 'Body does not belong to frozen source/capture', 'identity_mismatch')
        capture_id = capture.envelope['capture_id']
        if capture_id in self.bodies:
            previous, reference = self.bodies[capture_id]
            require(previous == body, 'N06', '/body_ref', 'A captured body cannot be replaced', 'identity_mismatch')
            return dict(reference)
        reference = self.session.write('body-' + capture_id + '.json', body, 'investment.news_body')
        self.bodies[capture_id] = json.loads(canonical(body)), reference
        return dict(reference)
