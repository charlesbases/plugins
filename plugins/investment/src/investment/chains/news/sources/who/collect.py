"""从 WHO 实际页面配置发现 OData 目录，再取得暴露的疫情通报原文。"""
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
        discovery = state.discovery()
        continuation = next(((url, purpose) for url, purpose in discovery.followups.items()
                             if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if continuation is not None:
            if await state.fetch(continuation[0], purpose=continuation[1]) is None:
                break
            continue
        if documents_stopped:
            break
        for opportunity in discovery.opportunities.values():
            url = opportunity['url']
            if url in visited or urlsplit(url).hostname not in state.manifest['allowed_hosts']:
                continue
            if await state.fetch(url, purpose='document') is None:
                documents_stopped = True
                break
            visited.update(normalize_url(item.envelope['requested_url']) for item in state.responses.values())
        else:
            break
    return state.finish()
