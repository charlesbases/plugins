"""由 SEC 当前披露页、Atom 和索引发现实际 XML 披露，不以 API 文档充数。"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from investment.chains.news.parsing import SourceCollector, normalize_url
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult
from investment.chains.news.transport import ResourceLimit
from investment.contracts import is_known


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)

    async def fetch(url: str, purpose: str):
        # 每个实际 hop 串行间隔至少 0.1 秒，重定向也服从访问频率约束。
        parent, response = None, None
        while True:
            await asyncio.sleep(0.1)
            try:
                response = await context.fetch(url, purpose=purpose, parent_capture_id=parent)
            except ResourceLimit:
                state.budget_stopped = True
                return response
            await state.observe(response)
            capture = response.envelope
            if capture['capture_state'] != 'complete' or not is_known(capture['redirect_target']) or is_known(capture['error']):
                return response
            parent, url = capture['capture_id'], capture['redirect_target']['value']

    for entrypoint in request['entrypoints']:
        if await fetch(entrypoint, 'entry') is None:
            return state.finish()

    documents_stopped = False
    while True:
        visited = {normalize_url(response.envelope['requested_url']) for response in state.responses.values()}
        if not documents_stopped:
            for opportunity in state.discovery().opportunities.values():
                url = opportunity['url']
                if url in visited or urlsplit(url).hostname not in state.manifest['allowed_hosts']:
                    continue
                if await fetch(url, 'document') is None:
                    documents_stopped = True
                    break
                visited.update(normalize_url(item.envelope['requested_url']) for item in state.responses.values())

        # 只跟随冻结规则从本轮文档、feed 或索引实际抽取的链接。
        continuation = next(((url, purpose) for url, purpose in state.discovery().followups.items()
                             if url not in visited and urlsplit(url).hostname in state.manifest['allowed_hosts']), None)
        if continuation is None or await fetch(continuation[0], continuation[1]) is None:
            break
    return state.finish()
