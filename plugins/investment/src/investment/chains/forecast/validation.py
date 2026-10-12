"""SC06 完整独立 F01—F18 编排，不生产模型、不自签封存。"""
from .contracts import Checks, Snapshot
from . import deterministic as d
from . import scenarios, diagnostics


def validate(ctx):
    """核本attempt原输入，必要正式宿主等待完成后返回逐用途凭证。"""
    snapshot = Snapshot.load(ctx)
    checks = Checks(ctx)
    actions = [('F01', d.inputs), ('F02', d.references), ('F03', d.members), ('F04', d.relations),
               ('F05', d.permissions), ('F06', d.training), ('F07', d.features), ('F08', d.numerical_models),
               ('F09', d.tuning), ('F10', d.predictions), ('F11', scenarios.residuals),
               ('F12', scenarios.sampling), ('F13', scenarios.endpoints),
               ('F14', diagnostics.numerical_inventory), ('F15', d.dispositions),
               ('F16', scenarios.scope), ('F18', d.invocation)]
    for rule, action in actions:
        checks.run(rule, lambda action=action: action(snapshot))
    if any(row['verdict'] == 'failed' for row in checks.results.values()):
        checks.record('F17', 'insufficient_evidence', 'A deterministic boundary failure prevents independent semantic qualification')
    else:
        host_result = checks.run('F17', lambda: diagnostics.request_or_review(snapshot))
        if host_result is not None:
            request, review = host_result
            if request is not None:
                return request
            checks.run('F17', lambda: diagnostics.host_binding(snapshot, review))
            checks.run('F14', lambda: diagnostics.assumption_review(snapshot, review))
            checks.run('F15', lambda: diagnostics.disposition_review(snapshot, review))
    receipt = checks.receipt(snapshot)
    ctx.schemas.validate('investment.validation_receipt', receipt)
    return receipt
