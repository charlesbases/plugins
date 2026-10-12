"""人民银行静态目录与原文采集；分页只跟随冻结规则实际暴露的链接。"""
from __future__ import annotations

from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    for entrypoint in request['entrypoints']:
        await state.fetch(entrypoint, purpose='entry')
        if state.budget_stopped:
            return state.finish()

    while not state.budget_stopped:
        discovery = state.discovery()
        visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()
                   if response.envelope['parent_capture_id']['status'] != 'known'}
        followup = next(((url, purpose) for url, purpose in discovery.followups.items()
                         if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if followup is not None:
            await state.fetch(followup[0], purpose=followup[1])
            continue
        article = next((row['url'] for row in discovery.opportunities.values()
                        if row['url'] not in visited
                        and urlsplit(row['url']).hostname in state.manifest['allowed_hosts']), None)
        if article is None:
            break
        await state.fetch(article, purpose='document')
    return state.finish()
