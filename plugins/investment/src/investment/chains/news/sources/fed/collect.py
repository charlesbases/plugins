"""美联储 RSS 只用于发现及同链接发布时间依据，正文来自实际原文请求。"""
from __future__ import annotations

from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    for feed in request['entrypoints']:
        await state.fetch(feed, purpose='entry')
        if state.budget_stopped:
            return state.finish()

    visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()
               if response.envelope['parent_capture_id']['status'] != 'known'}
    for opportunity in state.discovery().opportunities.values():
        url = opportunity['url']
        if url in visited or urlsplit(url).hostname not in state.manifest['allowed_hosts']:
            continue
        await state.fetch(url, purpose='document')
        visited.add(url)
        if state.budget_stopped:
            break
    return state.finish()
