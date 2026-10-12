"""SC03独立验证入口：确定性复核、真实宿主语义核对、逐用途许可。"""
from __future__ import annotations

from investment.contracts import ContractError, canonical
from .contracts import Checks, Snapshot
from . import deterministic, semantics, reporting


def _eligibility(snapshot, checks, reporting_reviews=()):
    inherited = {canonical(gap) for gap in snapshot.inherited_gaps}
    local = [gap for gap in snapshot.payload['unresolved_questions'] if canonical(gap) not in inherited]
    critical = all(checks.verdict(rule) == 'passed' for rule in snapshot.ctx.definition['critical_rules'])
    source_receipt = snapshot.upstream_receipts['evidence_corpus_ref'][1]

    def affected(use):
        return any(use in gap['affected_uses'] for gap in local)

    evidence_ok = critical and all(checks.verdict(rule) in ('passed', 'not_applicable')
                                   for rule in ('A05', 'A06', 'A07', 'A08'))
    evidence_ok = evidence_ok and not affected('evidence_review') and 'evidence_review' in source_receipt['allowed_uses']
    checks.set('A15', 'passed' if evidence_ok else 'insufficient_evidence',
               'Current source review eligibility; exact inherited blockers retain original restrictions without duplicate rejection')
    source_fact = any(row['verification_status'] in ('source_reported', 'cross_checked') for row in snapshot.tables['facts'])
    hypothesis_ok = evidence_ok and checks.verdict('A09') == 'passed' and source_fact and bool(snapshot.tables['hypotheses'])
    hypothesis_ok = hypothesis_ok and not affected('hypothesis_research') and 'hypothesis_research' in source_receipt['allowed_uses']
    checks.set('A16', 'passed' if hypothesis_ok else 'insufficient_evidence',
               'Actual supported facts, independently reviewed conditional mechanisms and question-specific evidence determine research eligibility')
    reporting_ok = critical and all(checks.verdict(rule) != 'failed' for rule in ('A05', 'A06', 'A07', 'A08', 'A09'))
    expected = set(reporting.local_gaps(snapshot))
    reviewed = {row['gap_sha256'] for row in reporting_reviews}
    faithful = len(reporting_reviews) == len(reviewed) and reviewed == expected and all(
        row['decision'] == 'faithful_limited_report' for row in reporting_reviews)
    reporting_ok = reporting_ok and faithful and 'reporting' in source_receipt['allowed_uses']
    checks.set('A17', 'passed' if reporting_ok else 'insufficient_evidence',
               'Hard checks, upstream permission and complete independent reporting-gap decisions govern faithful limited reporting')


def validate(ctx):
    """读取当前候选及原始依据，未收到实际宿主响应则返回HostRequest。"""
    snapshot = Snapshot.load(ctx)
    checks = Checks(ctx)
    reporting_reviews = []
    actions = [
        ('A01', deterministic.inputs), ('A02', deterministic.references),
        ('A03', deterministic.quotes_and_times), ('A04', deterministic.support_graph),
        ('A07', deterministic.events), ('A08', deterministic.observations),
        ('A09', deterministic.mechanisms), ('A10', deterministic.coverage),
        ('A13', deterministic.identity_limits), ('A14', deterministic.invocation),
    ]
    for rule, action in actions:
        checks.run(rule, lambda action=action: action(snapshot))
    evidence_refs = [ctx.input_ref, ctx.candidate_ref, ctx.method_ref]
    evidence_refs += [reference for reference, _ in snapshot.upstream_receipts.values()]
    semantic_rules = ('A05', 'A06', 'A07', 'A08', 'A09', 'A10', 'A11', 'A12')
    if any(check['verdict'] == 'failed' for check in checks.results.values()):
        for rule in semantic_rules:
            checks.set(rule, 'insufficient_evidence', 'Semantic qualification cannot proceed after a concrete deterministic boundary failure')
    else:
        try:
            request, result = semantics.request_or_reviews(snapshot, checks)
            if request is not None:
                return request
            review, references = result
            reporting_reviews = review['reporting_gap_reviews']
            evidence_refs += references
            semantics.semantic_rules(snapshot, review, checks)
            for rule, name in (('A05', 'facts'), ('A06', 'facts'), ('A07', 'events'),
                               ('A08', 'observations'), ('A09', 'hypotheses')):
                if not snapshot.tables[name] and (rule != 'A06' or not snapshot.tables['counterevidence']):
                    checks.set(rule, 'not_applicable', 'The exact object set is empty and full source/question coverage has been independently checked')
        except (ContractError, KeyError, ValueError, TypeError) as error:
            rule = getattr(error, 'rule', 'A12')
            rule = rule if rule in ctx.definition['required_rules'] else 'A11'
            checks.fail(rule, getattr(error, 'pointer', '/semantic_review'), str(error))
            for dependent in semantic_rules:
                checks.set(dependent, 'insufficient_evidence', 'Actual semantic evidence is incomplete or failed its binding/coverage checks')
    _eligibility(snapshot, checks, reporting_reviews)
    receipt = checks.receipt(snapshot.payload, evidence_refs, inherited=bool(snapshot.inherited_gaps))
    ctx.schemas.validate('investment.validation_receipt', receipt)
    return receipt
