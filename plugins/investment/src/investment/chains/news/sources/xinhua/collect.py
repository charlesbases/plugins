"""新华网财经与国际入口共同采集，在有限文档预算内交替访问两栏原文。"""
from __future__ import annotations

from collections import deque
from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    entry_responses = []
    for entrypoint in request['entrypoints']:
        response = await state.fetch(entrypoint, purpose='entry')
        if response is not None:
            entry_responses.append(response)
        if state.budget_stopped:
            return state.finish()

    opportunities = state.discovery().opportunities.values()
    queues = [deque(row['url'] for row in opportunities if any(
        found['capture_ref'] == response.capture_ref for found in row['discovered_in']))
        for response in entry_responses]
    visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()
               if response.envelope['parent_capture_id']['status'] != 'known'}
    while any(queues) and not state.budget_stopped:
        for queue in queues:
            while queue and (queue[0] in visited or urlsplit(queue[0]).hostname not in state.manifest['allowed_hosts']):
                queue.popleft()
            if not queue:
                continue
            url = queue.popleft()
            await state.fetch(url, purpose='document')
            visited.add(url)
            if state.budget_stopped:
                break
    return state.finish()
