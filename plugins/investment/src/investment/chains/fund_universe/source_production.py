"""SC04 实际取材与目录解析；不复用独立验证者的业务推导。"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from lxml import etree, html

from investment.contracts import known, na, now, require, unknown


@dataclass(frozen=True)
class Retrieval:
    started_at: str
    finished_at: str
    capture: dict | None
    outcome: str
    reason: str | None
    complete_body: bool


@dataclass(frozen=True)
class DirectoryEntry:
    locator: str
    label: str
    code: str | None
    links: tuple[str, ...]


@dataclass(frozen=True)
class DirectoryPage:
    entries: tuple[DirectoryEntry, ...]
    following: tuple[str, ...]
    document: object


def retrieve(url, *, max_bytes, timeout):
    """取得一次实际响应；超预算的截断字节也保留，但不能当完整目录解析。"""
    require(urlsplit(url).scheme in ('http', 'https'), 'U05', '/request_url', 'HTTP source URL is required')
    require(max_bytes > 0 and timeout > 0, 'U05', '/budget', 'Positive remaining retrieval budget is required')
    started = now()
    request = Request(url, headers={'User-Agent': 'Investment/0.0.1', 'Accept-Encoding': 'identity'})
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as error:
        response = error
    except (URLError, TimeoutError, OSError) as error:
        return Retrieval(started, now(), None, 'network_error', str(error), False)
    with response:
        try:
            body = response.read(max_bytes + 1)
        except (TimeoutError, OSError) as error:
            return Retrieval(started, now(), None, 'network_error', str(error), False)
        finished = now()
        content_type = response.headers.get('Content-Type')
        capture = {
            'capture_id': 'capture-' + hashlib.sha256((url + started).encode()).hexdigest(),
            'request_url': url, 'response_url': response.geturl(), 'captured_at': finished,
            'http_status': response.status if response.status is not None else response.code,
            'response_content_type': known(content_type) if content_type else unknown('HTTP Content-Type was absent'),
            'body_base64': base64.b64encode(body).decode('ascii'),
            'body_sha256': hashlib.sha256(body).hexdigest(), 'body_byte_size': len(body),
        }
    if len(body) > max_bytes:
        return Retrieval(started, finished, capture, 'budget_exhausted', 'Response exceeded remaining byte budget; body is incomplete', False)
    if not 200 <= capture['http_status'] < 300:
        return Retrieval(started, finished, capture, 'http_error', 'HTTP status ' + str(capture['http_status']), True)
    return Retrieval(started, finished, capture, 'retrieved', None, True)


def capture_body(capture):
    body = base64.b64decode(capture['body_base64'], validate=True)
    require(len(body) == capture['body_byte_size'] and hashlib.sha256(body).hexdigest() == capture['body_sha256'],
            'U05', '/body_base64', 'Stored response bytes differ from their capture')
    return body


def html_document(capture):
    """先完整严格解码再建树，拒绝lxml静默截断的恢复树。"""
    body = capture_body(capture)
    header = capture['response_content_type']
    label = header['value'] if header['status'] == 'known' else ''
    match = re.search(r'charset\s*=\s*["\']?([\w-]+)', label, re.I)
    if match is None:
        match = re.search(br'charset\s*=\s*["\']?([\w-]+)', body[:4096], re.I)
        encoding = match.group(1).decode('ascii') if match else 'utf-8'
    else:
        encoding = match.group(1)
    if encoding.lower().replace('_', '-') in ('gb2312', 'gbk', 'x-gbk', 'gb-2312', 'chinese'):
        encoding = 'gb18030'
    text = body.decode(encoding, errors='strict')
    parser = html.HTMLParser(encoding='utf-8', no_network=True)
    document = html.fromstring(text.encode('utf-8'), parser=parser, base_url=capture['response_url'])
    require(not any(item.level >= etree.ErrorLevels.FATAL for item in parser.error_log),
            'U06', '/raw_ref', 'HTML parser reported fatal recovery; original scope is not enumerable')
    return document


def _http_links(node, base):
    result = []
    for href in node.xpath('.//a/@href'):
        absolute = urljoin(base, href.strip())
        if urlsplit(absolute).scheme in ('http', 'https') and absolute not in result:
            result.append(absolute)
    return tuple(result)


def parse_directory(capture):
    """解析已核天天基金目录布局；未知布局报缺口，不制造空全集。"""
    document = html_document(capture)
    roots = document.xpath('//*[@id="code_content"]')
    require(len(roots) == 1, 'U06', '/raw_ref', 'Unsupported original directory layout')
    tree = document.getroottree()
    entries = []
    for node in roots[0].xpath('.//li'):
        text = ' '.join(node.text_content().split())
        titles = [value.strip() for value in node.xpath('.//@title') if value.strip()]
        links = _http_links(node, capture['response_url'])
        # 已核布局中的完全空填充li没有产品含义；有任何产品内容则保留。
        if not text and not titles and not links and len(node) == 0:
            continue
        label = text or ' '.join(titles)
        require(bool(label), 'U06', '/raw_label', 'Nonempty product container has no readable original label')
        codes = set()
        for link in links:
            parsed = urlsplit(link)
            match = re.fullmatch(r'/([0-9]{6})\.html', parsed.path)
            if parsed.hostname in ('fund.eastmoney.com', 'fundf10.eastmoney.com') and match:
                codes.add(match.group(1))
        # 多代码信号属于待核冲突，不能从中任选一个身份。
        code = next(iter(codes)) if len(codes) == 1 else None
        entries.append(DirectoryEntry(tree.getpath(node), label, code, links))
    require(bool(entries), 'U06', '/raw_ref', 'Original directory has no enumerable product entries')
    following = []
    for anchor in document.xpath('//a[@href]'):
        title = ' '.join(anchor.text_content().split()).lower()
        if 'next' in anchor.get('rel', '').lower().split() or title in ('下一页', '下页', 'next', 'next page', '>'):
            url = urljoin(capture['response_url'], anchor.get('href'))
            if urlsplit(url).scheme in ('http', 'https') and url != capture['response_url'] and url not in following:
                following.append(url)
    return DirectoryPage(tuple(entries), tuple(following), document)


def evidence_links(capture):
    """仅把实际档案中的基础/合同/披露链接变为取材线索，不据名称推断身份。"""
    document = html_document(capture)
    links = []
    for anchor in document.xpath('//a[@href]'):
        href = anchor.get('href')
        text = ' '.join(anchor.text_content().split())
        absolute = urljoin(capture['response_url'], href)
        path = urlsplit(absolute).path.lower()
        relevant = bool(re.search(r'/(?:jbgk|jjgg)_[0-9]{6}(?:_[0-9]+)?\.html$', path))
        relevant |= path.endswith('.pdf') or any(word in text for word in ('基金合同', '招募说明书', '定期报告', '年度报告', '中期报告'))
        if relevant and urlsplit(absolute).scheme in ('http', 'https') and absolute not in links:
            links.append(absolute)
    return tuple(links)


def source_entry(entry, *, source_id, task_id, raw_ref):
    key = hashlib.sha256((raw_ref['uri'] + '\n' + raw_ref['sha256'] + '\n' + entry.locator).encode()).hexdigest()
    return {'entry_id': 'entry-' + key, 'source_id': source_id, 'task_id': task_id,
            'raw_ref': raw_ref, 'locator': entry.locator, 'raw_label': entry.label,
            'original_code': known(entry.code) if entry.code is not None else unknown('Original entry has no unambiguous fund code'),
            'entry_kind': 'identified_product' if entry.code is not None else 'unparsed_product',
            'discovered_links': list(entry.links)}


def attempt_row(retrieval, *, task_id, source_id, requested_url, raw_ref):
    return {'attempt_id': 'attempt-' + hashlib.sha256((task_id + retrieval.started_at).encode()).hexdigest(),
            'task_id': task_id, 'source_id': source_id, 'requested_url': requested_url,
            'started_at': retrieval.started_at, 'finished_at': retrieval.finished_at, 'outcome': retrieval.outcome,
            'http_status': known(retrieval.capture['http_status']) if retrieval.capture else unknown('No HTTP response received'),
            'raw_ref': known(raw_ref) if raw_ref else unknown('No response body was captured'),
            'failure_reason': known(retrieval.reason) if retrieval.reason else na('retrieval_completed')}
