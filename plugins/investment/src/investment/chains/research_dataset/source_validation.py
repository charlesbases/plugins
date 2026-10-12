"""SC05 独立回原字节定位；文本存在不替代正式宿主含义审核。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
import subprocess
from decimal import Decimal

from lxml import etree, html

from investment.contracts import ContractError, require


def pointer(value, path):
    require(path == '' or path.startswith('/'), 'D18', '/field_path', 'Invalid JSON pointer')
    for token in path.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        try:
            value = value[int(token)] if isinstance(value, list) and token.isdigit() else value[token]
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise ContractError('D18', '/field_path', 'Source or target pointer is absent', 'missing_evidence') from error
    return value


def capture_bytes(capture):
    try:
        raw = base64.b64decode(capture['body_base64'], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ContractError('D04', '/body_base64', 'Invalid source Base64') from error
    require(base64.b64encode(raw).decode('ascii') == capture['body_base64']
            and len(raw) == capture['body_byte_size'] and hashlib.sha256(raw).hexdigest() == capture['body_sha256'],
            'D04', '/body_sha256', 'Source byte digest or length differs')
    return raw


def located_text(citation, source, attempt_dir):
    raw = source.encode('utf8') if isinstance(source, str) else capture_bytes(source)
    content_type = 'text/plain;charset=utf-8' if isinstance(source, str) else source['content_type']
    match = re.search(r'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', content_type, re.I)
    if match is None:
        match = re.search(rb'charset\s*=\s*["\x27]?([A-Za-z0-9_-]+)', raw[:4096], re.I)
        encoding = match.group(1).decode('ascii') if match else 'utf-8'
    else:
        encoding = match.group(1)
    if encoding.lower() in ('gb2312', 'gbk', 'x-gbk'):
        encoding = 'gb18030'
    kind, separator, location = citation['locator'].partition(':')
    require(separator and kind in ('json', 'html', 'bytes', 'pdf'), 'D18', '/locator', 'Source locator requires registered kind')
    try:
        if kind == 'json':
            value = pointer(json.loads(raw.decode(encoding), parse_float=Decimal), location)
            require(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool),
                    'D18', '/locator', 'JSON location must select a scalar source value')
            text = str(value)
        elif kind == 'html':
            document = html.fromstring(raw.decode(encoding), parser=html.HTMLParser(no_network=True))
            nodes = document.xpath(location)
            require(len(nodes) == 1 and isinstance(nodes[0], etree._Element), 'D18', '/locator', 'HTML location must select one element')
            text = ''.join(nodes[0].itertext())
        elif kind == 'bytes':
            match = re.fullmatch(r'(0|[1-9][0-9]*):(0|[1-9][0-9]*)', location)
            require(match is not None, 'D18', '/locator', 'Invalid byte-range location')
            start, end = map(int, match.groups())
            require(0 <= start < end <= len(raw), 'D18', '/locator', 'Source byte range is outside original bytes')
            text = raw[start:end].decode(encoding)
        else:
            require(re.fullmatch(r'[1-9][0-9]*', location) is not None and raw.startswith(b'%PDF-'),
                    'D18', '/locator', 'PDF location requires actual PDF and positive page')
            executable = shutil.which('pdftotext')
            require(executable is not None, 'D18', '/locator', 'PDF extraction capability unavailable', 'missing_evidence')
            path = attempt_dir / ('validation-pdf-' + hashlib.sha256(raw).hexdigest() + '.pdf')
            created = not path.exists()
            require(created or path.read_bytes() == raw, 'D18', '/raw_ref', 'Existing PDF scratch bytes differ')
            try:
                if created:
                    with path.open('xb') as stream:
                        stream.write(raw)
                result = subprocess.run([executable, '-f', location, '-l', location, '-enc', 'UTF-8', '-layout', str(path), '-'],
                                        capture_output=True, timeout=30)
                require(result.returncode == 0 and result.stdout.strip(), 'D18', '/locator', 'PDF extraction failed or is empty', 'missing_evidence')
                text = result.stdout.decode('utf8')
            finally:
                if created and path.exists():
                    path.unlink()
    except (ValueError, UnicodeError, LookupError, etree.XPathError, etree.ParserError, OSError, subprocess.TimeoutExpired) as error:
        raise ContractError('D18', '/locator', 'Original source cannot be located: ' + str(error), 'missing_evidence') from error
    require(re.sub(r'\s+', '', citation['quote']) in re.sub(r'\s+', '', text),
            'D18', '/quote', 'Quote is absent at the exact original source location')
    return text


def semantic_paths(value, prefix='/value'):
    """引用、引文本身交由确定性核验；逐科学/金融字段交给独立宿主。"""
    if isinstance(value, dict):
        if 'schema_id' in value and 'uri' in value:
            return []
        if 'status' in value and 'value' in value:
            return [prefix]
        return [path for key, item in value.items() if key not in ('citations', 'authority_basis')
                for path in semantic_paths(item, prefix + '/' + key.replace('~', '~0').replace('/', '~1'))]
    if isinstance(value, list):
        return [path for index, item in enumerate(value) for path in semantic_paths(item, prefix + '/' + str(index))] or [prefix]
    return [prefix]


def source_references(value):
    """只收集对象明确原文/时间/转换依据，不开放原表或祖先包。"""
    found = {}
    def walk(node, key=''):
        if isinstance(node, dict):
            if 'schema_id' in node and 'uri' in node:
                if key in ('raw_ref', 'basis_ref', 'factor_source_ref', 'precision_basis_ref', 'evidence_refs', 'raw_refs'):
                    found[node['uri']] = node
                return
            for name, child in node.items():
                walk(child, name)
        elif isinstance(node, list):
            for child in node:
                walk(child, key)
    walk(value)
    return [found[key] for key in sorted(found)]
