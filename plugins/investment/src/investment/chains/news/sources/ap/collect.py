"""实际访问 AP 声明入口；未校准布局保留原始捕获和覆盖缺口。"""
from __future__ import annotations

from investment.chains.news.parsing import SourceCollector
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    for entrypoint in request['entrypoints']:
        await state.fetch(entrypoint, purpose='entry')
        if state.budget_stopped:
            break
    return state.finish()
