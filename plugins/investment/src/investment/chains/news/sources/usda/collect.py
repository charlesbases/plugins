"""逐页采集 USDA 新闻稿，分页仅使用当前响应暴露的下一页链接。"""
from __future__ import annotations

from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    for entrypoint in request['entrypoints']:
        if await state.fetch(entrypoint, purpose='entry') is None:
            return state.finish()

    documents_stopped = False
    while True:
        visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()}
        if not documents_stopped:
            for opportunity in state.discovery().opportunities.values():
                url = opportunity['url']
                if url in visited or urlsplit(url).hostname not in state.manifest['allowed_hosts']:
                    continue
                if await state.fetch(url, purpose='document') is None:
                    documents_stopped = True
                    break
                visited.update(normalize_url(item.envelope['requested_url']) for item in state.responses.values())

        # 文档预算结束后仍保留已访问后续目录中的全部机会。
        continuation = next(((url, purpose) for url, purpose in state.discovery().followups.items()
                             if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if continuation is None or await state.fetch(continuation[0], purpose=continuation[1]) is None:
            break
    return state.finish()
