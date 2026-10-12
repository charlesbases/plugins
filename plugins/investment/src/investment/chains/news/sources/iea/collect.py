"""实际请求 IEA 配置入口，按冻结规则保留访问与未知覆盖结果。"""
from __future__ import annotations

from investment.chains.news.parsing import SourceCollector
from investment.chains.news.protocol import CollectorContext, CollectorRequest, CollectorResult


async def collect(request: CollectorRequest, context: CollectorContext) -> CollectorResult:
    state = SourceCollector(request, context)
    # 当前没有已校准的目录或正文规则，仍实际请求而不按旧状态跳过。
    for entrypoint in request['entrypoints']:
        if await state.fetch(entrypoint, purpose='entry') is None:
            break
    return state.finish()
