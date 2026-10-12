"""取得 FAO newsroom 目录实际暴露的原文，不推测客户端分页。"""
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
        release = next((row['url'] for row in state.discovery().opportunities.values()
                        if row['url'] not in visited
                        and urlsplit(row['url']).hostname in state.manifest['allowed_hosts']), None)
        if release is None or await state.fetch(release, purpose='document') is None:
            break
    return state.finish()
