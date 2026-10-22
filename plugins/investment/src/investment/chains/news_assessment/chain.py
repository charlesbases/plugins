"""SC03唯一生产业务入口：本轮冻结材料到候选，不自行签发验证或封存。"""
from investment.contracts import require
from .workflow import produce_draft
from .assemble import assemble_candidate


def produce(ctx):
    require(ctx.stage_id == 'SC03' and ctx.role == 'producer' and ctx.candidate is None,
            'A01', '/role', 'SC03 producer context required')
    result = produce_draft(ctx)
    return assemble_candidate(ctx, result)
