"""SC04 验证器独立读取原始目录与引文，拒绝生产者自证。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from investment.contracts import ContractError, now, require


def capture_bytes(capture):
    try:
        raw = base64.b64decode(capture['body_base64'], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ContractError('U02', '/body_base64', 'Capture has invalid Base64', 'schema_violation') from error
    require(base64.b64encode(raw).decode('ascii') == capture['body_base64'], 'U02', '/body_base64', 'Capture Base64 is not canonical')
    require(len(raw) == capture['body_byte_size'] and hashlib.sha256(raw).hexdigest() == capture['body_sha256'],
            'U02', '/body_sha256', 'Decoded source bytes do not match their capture')
    return raw


def original_directory(capture, *, max_bytes):
    """只对实际已核的全目录布局独立枚举；未知布局不猜成空目录。"""
    require(capture['body_byte_size'] <= max_bytes, 'U06', '/body_byte_size', 'Original directory exceeds the frozen parsing byte budget', 'missing_evidence')
    raw = capture_bytes(capture)
    url = urlsplit(capture['response_url'])
    document = _html_document(raw, _text_encoding(capture, raw))
    next_links = {urljoin(capture['response_url'], node.get('href')) for node in document.xpath('//a[@href]')
                  if 'next' in (node.get('rel') or '').split() or re.sub(r'\s+', '', node.text_content()) in ('下一页', '下一頁')}
    if url.hostname != 'fund.eastmoney.com' or url.path != '/allfund.html':
        return None, next_links
    containers = document.xpath('//*[@id="code_content"]//ul[contains(concat(" ",normalize-space(@class)," ")," num_right ")]')
    require(containers, 'U06', '/original_list_refs', 'Known directory layout changed; empty is not proven', 'missing_evidence')
    entries = {}
    for container in containers:
        for element in container.xpath('./li'):
            # 已核目录用完全空的li补齐列；无任何产品内容，不捏造成员。
            if len(element) == 0 and not (element.text or '').strip() and set(element.attrib) <= {'class'}:
                continue
            locator = document.getroottree().getpath(element)
            anchors = element.xpath('.//a[@href]')
            products = [(anchor, re.fullmatch(r'/(\d{6})\.html', urlsplit(urljoin(capture['response_url'], anchor.get('href'))).path))
                        for anchor in anchors if urlsplit(urljoin(capture['response_url'], anchor.get('href'))).hostname == 'fund.eastmoney.com']
            products = [(anchor, match) for anchor, match in products if match is not None]
            label = products[0][0].text_content() if products else element.text_content()
            if not label.strip():
                titles = [value for value in element.xpath('.//@title') if value.strip()]
                label = titles[0] if titles else ''
            require(label.strip(), 'U06', '/original_list_refs', 'Unparsed blank product row must remain unresolved', 'missing_evidence')
            entries[locator] = {'label': label, 'code': products[0][1].group(1) if products else None,
                                'context': ''.join(element.itertext()) + ' ' + ' '.join(element.xpath('.//@title'))}
    return entries, next_links


def json_pointer(value, pointer):
    for token in pointer.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        try:
            value = value[int(token)] if isinstance(value, list) and token.isdigit() else value[token]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ContractError('U10', '/locator', 'Source locator is absent', 'missing_evidence') from error
    return value


def _text_encoding(capture, raw):
    content_type = capture['response_content_type']
    declared = content_type['value'] if content_type['status'] == 'known' else ''
    match = re.search(r'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', declared, re.I)
    if match is None:
        match = re.search(rb'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', raw[:4096], re.I)
        label = match.group(1).decode('ascii') if match else 'utf-8'
    else:
        label = match.group(1)
    # WHATWG将这些HTML标签映射GBK，其decoder为gb18030；不靠试错替换字符。
    if label.lower() in {'gb2312', 'gb_2312', 'gb_2312-80', 'csgb2312', 'csiso58gb231280', 'iso-ir-58', 'chinese', 'gbk', 'x-gbk'}:
        return 'gb18030'
    return label


def _html_document(raw, encoding):
    try:
        decoded = raw.decode(encoding)
        parser = html.HTMLParser(encoding='utf-8', no_network=True)
        document = html.fromstring(decoded.encode('utf-8'), parser=parser)
    except (LookupError, UnicodeError, etree.ParserError, ValueError) as error:
        raise ContractError('U10', '/raw_ref', 'Original HTML cannot be decoded and parsed completely', 'missing_evidence') from error
    require(not any(item.level_name == 'FATAL' for item in parser.error_log),
            'U10', '/raw_ref', 'Fatal HTML parsing cannot certify a complete source tree', 'missing_evidence')
    return document


def pdf_page_text(raw, page, attempt_dir):
    """本机Xpdf不支持PDF stdin；仅创建并清理本调用自己的准确文件。"""
    require(raw.startswith(b'%PDF-'), 'U10', '/raw_ref', 'Requested PDF is not a PDF response')
    executable = shutil.which('pdftotext')
    require(executable is not None, 'U10', '/raw_ref', 'pdftotext is unavailable', 'missing_evidence')
    directory = Path(attempt_dir).resolve()
    require(directory.is_dir(), 'U10', '/attempt_dir', 'Validated attempt directory is required')
    name = 'validation-pdf-' + hashlib.sha256(raw).hexdigest() + '.pdf'
    selected = directory / name
    require(selected.resolve().parent == directory, 'U10', '/raw_ref', 'PDF scratch path escapes the current attempt')
    created = False
    command = [executable, '-f', str(page), '-l', str(page), '-enc', 'UTF-8', '-layout', str(selected), '-']
    started = now()
    audit = {'event': 'sc04_pdf_source_check', 'source_sha256': hashlib.sha256(raw).hexdigest(), 'command': command, 'started_at': started}
    try:
        if selected.exists():
            require(selected.read_bytes() == raw, 'U10', '/raw_ref', 'Existing PDF scratch bytes differ')
        else:
            with selected.open('xb') as stream:
                stream.write(raw)
            created = True
        version = subprocess.run([executable, '-v'], capture_output=True, timeout=10)
        audit['tool_version'] = (version.stdout + version.stderr).decode('utf-8', 'replace').strip()
        result = subprocess.run(command, capture_output=True, timeout=30)
        audit.update(exit_code=result.returncode, stderr=result.stderr.decode('utf-8', 'replace'), output_sha256=hashlib.sha256(result.stdout).hexdigest())
        require(result.returncode == 0 and result.stdout.strip(), 'U10', '/locator', 'PDF extraction failed or produced no page text', 'missing_evidence')
        return result.stdout.decode('utf-8')
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        audit['error'] = str(error)
        raise ContractError('U10', '/locator', 'PDF text capability failed: ' + str(error), 'missing_evidence') from error
    finally:
        audit['finished_at'] = now()
        print(json.dumps(audit, ensure_ascii=True, separators=(',', ':')), file=sys.stderr)
        if created:
            selected.unlink()


def citation_text(citation, source, attempt_dir):
    """摘录在准确原文位置成立只是必要条件，事实含义仍须独立语义核验。"""
    if isinstance(source, str):
        raw = source.encode('utf-8')
        encoding = 'utf-8'
    else:
        raw = capture_bytes(source)
        encoding = _text_encoding(source, raw)
    kind, locator = citation['locator_kind'], citation['locator']
    if kind == 'pdf_page':
        match = re.fullmatch(r'([1-9][0-9]*)(?::(.+))?', locator)
        require(match is not None, 'U10', '/locator', 'PDF locator requires a positive page number')
        text = pdf_page_text(raw, int(match.group(1)), attempt_dir)
        if match.group(2):
            require(re.sub(r'\s+', '', match.group(2)) in re.sub(r'\s+', '', text),
                    'U10', '/locator', 'PDF paragraph anchor is absent')
    elif kind == 'html_element':
        try:
            nodes = _html_document(raw, encoding).xpath(locator)
        except (etree.ParserError, etree.XPathError, ValueError) as error:
            raise ContractError('U10', '/locator', 'Invalid HTML source locator') from error
        require(len(nodes) == 1 and isinstance(nodes[0], etree._Element), 'U10', '/locator', 'HTML source location must select one element')
        text = ''.join(nodes[0].itertext()) + ' ' + ' '.join(nodes[0].xpath('.//@title'))
    elif kind == 'byte_range':
        match = re.fullmatch(r'(0|[1-9][0-9]*):(0|[1-9][0-9]*)', locator)
        require(match is not None, 'U10', '/locator', 'Byte location must be start:end')
        start, end = map(int, match.groups())
        require(0 <= start < end <= len(raw), 'U10', '/locator', 'Byte range is outside the source')
        try:
            text = raw[start:end].decode(encoding)
        except (LookupError, UnicodeError) as error:
            raise ContractError('U10', '/locator', 'Source byte range is not decodable', 'missing_evidence') from error
    else:
        try:
            document = json.loads(raw.decode(encoding), parse_float=Decimal)
        except (ValueError, LookupError, UnicodeError) as error:
            raise ContractError('U10', '/locator', 'JSON source cannot be parsed', 'missing_evidence') from error
        value = json_pointer(document, locator)
        require(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool), 'U10', '/locator', 'JSON quote location must select a scalar text or number')
        text = str(value)
    require(re.sub(r'\s+', '', citation['quoted_text']) in re.sub(r'\s+', '', text),
            'U10', '/quoted_text', 'Quote is not present at the exact original source location')
    return text


def numeric_literals(text):
    """原文百分数精确除100；此函数不判断数值的主体或分母。"""
    result = set()
    for match in re.finditer(r'(?<![0-9.])([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*([%％]?)', text):
        value = Decimal(match.group(1).replace(',', ''))
        result.add(value / Decimal(100) if match.group(2) else value)
    return result
