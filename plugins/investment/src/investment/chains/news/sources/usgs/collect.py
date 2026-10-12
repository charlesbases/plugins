"""沿 USGS 页面中的实际日度 feed 和 detail 链接获取原生事件记录。"""
from __future__ import annotations

from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    for entrypoint in request['entrypoints']:
        if await state.fetch(entrypoint, purpose='entry') is None:
            return state.finish()

    while True:
        visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()}
        discovery = state.discovery()
        feed = next(((url, purpose) for url, purpose in discovery.followups.items()
                     if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if feed is not None:
            if await state.fetch(feed[0], purpose=feed[1]) is None:
                break
            continue
        event = next((row['url'] for row in discovery.opportunities.values()
                      if row['url'] not in visited
                      and urlsplit(row['url']).hostname in state.manifest['allowed_hosts']), None)
        if event is None or await state.fetch(event, purpose='data') is None:
            break
    return state.finish()
