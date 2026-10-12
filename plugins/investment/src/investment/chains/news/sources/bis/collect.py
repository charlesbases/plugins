"""实际访问 BIS 目录和所列新闻，未校准正文仅保留原始响应及待解析项。"""
from __future__ import annotations

from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    for entrypoint in request['entrypoints']:
        if await state.fetch(entrypoint, purpose='entry') is None:
            return state.finish()

    visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()}
    for opportunity in state.discovery().opportunities.values():
        url = opportunity['url']
        if url in visited or urlsplit(url).hostname not in state.manifest['allowed_hosts']:
            continue
        if await state.fetch(url, purpose='document') is None:
            break
        visited.update(normalize_url(item.envelope['requested_url']) for item in state.responses.values())
    return state.finish()
