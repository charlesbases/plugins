"""按 ENISA 新闻目录取得网络安全原文，保留重复展示定位及未处理机会。"""
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

        continuation = next(((url, purpose) for url, purpose in state.discovery().followups.items()
                             if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if continuation is None or await state.fetch(continuation[0], purpose=continuation[1]) is None:
            break
    return state.finish()
