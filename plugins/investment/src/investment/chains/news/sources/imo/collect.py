"""按 IMO 简报目录取材，保留路径大小写、真实重定向和分页查询。"""
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
                response = await state.fetch(url, purpose='document')
                if response is None:
                    documents_stopped = True
                    break
                visited.update(normalize_url(item.envelope['requested_url']) for item in state.responses.values())

        # 文档预算耗尽仍可读取已暴露的目录，保留其全部机会；不猜页码。
        continuation = next(((url, purpose) for url, purpose in state.discovery().followups.items()
                             if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if continuation is None:
            break
        url, purpose = continuation
        if await state.fetch(url, purpose=purpose) is None:
            break
    return state.finish()
