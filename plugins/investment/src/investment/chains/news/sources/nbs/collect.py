"""统计局发布目录采集；重复展示链接由统一机会清单保留各自定位。"""
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
        page = next(((url, purpose) for url, purpose in discovery.followups.items()
                     if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if page is not None:
            await state.fetch(page[0], purpose=page[1])
            continue
        release = next((row['url'] for row in discovery.opportunities.values()
                        if row['url'] not in visited
                        and urlsplit(row['url']).hostname in state.manifest['allowed_hosts']), None)
        if release is None:
            break
        await state.fetch(release, purpose='document')
    return state.finish()
