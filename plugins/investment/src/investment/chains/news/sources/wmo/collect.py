"""实际访问 WMO 入口，依冻结规则调度已暴露材料并保留访问或布局缺口。"""
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
        continuation = next(((url, purpose) for url, purpose in discovery.followups.items()
                             if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if continuation is not None:
            if await state.fetch(continuation[0], purpose=continuation[1]) is None:
                break
            continue
        article = next((row['url'] for row in discovery.opportunities.values()
                        if row['url'] not in visited
                        and urlsplit(row['url']).hostname in state.manifest['allowed_hosts']), None)
        if article is None or await state.fetch(article, purpose='document') is None:
            break
    return state.finish()
