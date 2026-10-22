"""SC03来源布局插件：只读保存原文，不采集、不授予事实资格。"""
import base64
import gzip

from lxml import html

from investment.contracts import require
from investment.chains.news.parsing import source_text


def _images(node):
    return [{key: image.attrib[key] for key in ('src', 'alt', 'title') if key in image.attrib}
            for image in node.xpath('.//img')]


class HtmlLayout:
    """保留登记来源原始段落、表格列与图片引用，不访问网络。"""

    def __init__(self, provider_id):
        self.provider_id = provider_id

    def build(self, document, body, capture):
        raw = base64.b64decode(capture['response_bytes_base64'])
        encodings = [h['value'].lower() for h in capture['response_headers']
                     if h['name'].lower() == 'content-encoding']
        require(encodings in ([], ['identity'], ['gzip']), 'A03', '/source_layout',
                'Source layout requires the observed identity or gzip HTML encoding')
        if encodings == ['gzip']:
            raw = gzip.decompress(raw)
        tree = html.fromstring(raw.decode('utf-8'))
        for node in tree.xpath('//script|//style|//noscript'):
            node.drop_tree()
        nodes = tree.xpath(body['body_locator'])
        require(len(nodes) == 1 and source_text(nodes[0]) == body['text'],
                'A03', '/source_layout', 'HTML layout must match the exact saved article body')
        blocks = []
        for node in nodes[0].iterdescendants():
            if node.tag not in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table'):
                continue
            if any(parent.tag == 'table' for parent in node.iterancestors()):
                continue
            if node.tag != 'table':
                text, images = source_text(node), _images(node)
                if text or images:
                    blocks.append({'kind': 'paragraph', 'text': text, 'images': images})
                continue
            rows = []
            for row in node.xpath('.//tr'):
                if row.xpath('ancestor::table[1]')[0] is not node:
                    continue
                cells = []
                for cell in row.xpath('./td|./th'):
                    value = {'text': source_text(cell),
                             'text_runs': [text for text in cell.itertext() if text.strip()],
                             'images': _images(cell)}
                    value.update({key: cell.attrib[key] for key in ('rowspan', 'colspan') if key in cell.attrib})
                    cells.append(value)
                if cells:
                    rows.append(cells)
            if rows:
                blocks.append({'kind': 'table', 'rows': rows})
        return {'provider_id': self.provider_id, 'source_id': document['source_id'],
                'document_version_id': document['version_id'], 'body_ref': document['body_ref'],
                'raw_ref': document['raw_ref'], 'blocks': blocks}


SOURCE_LAYOUT_PROVIDERS = {source: HtmlLayout(source + '-html-layout-v1')
                           for source in ('nbs', 'pbc', 'who', 'fao')}


def source_layouts(ctx, documents, bodies):
    """按来源登记读真实capture；布局只辅助解释原始正文。"""
    layouts = []
    for version, document in sorted(documents.items()):
        provider = SOURCE_LAYOUT_PROVIDERS.get(document['source_id'])
        if provider is not None:
            capture = ctx.resolver.resolve(document['raw_ref'], 'investment.news_capture')
            layouts.append(provider.build(document, bodies[version], capture))
    return layouts
