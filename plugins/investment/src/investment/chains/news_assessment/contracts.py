"""SC03独立验证的输入快照、定位和规则结果，不调用生产者算法。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
import re

from investment.contracts import ContractError, canonical, digest, instant, now, require

TABLES = {
    'facts': ('facts_ref', 'fact_id', 'investment.sc03.facts'),
    'events': ('events_ref', 'event_id', 'investment.sc03.events'),
    'hypotheses': ('hypotheses_ref', 'hypothesis_id', 'investment.sc03.hypotheses'),
    'observations': ('economic_observations_ref', 'observation_id', 'investment.sc03.economic_observations'),
    'counterevidence': ('counterevidence_ref', 'counterevidence_id', 'investment.sc03.counterevidence'),
    'dispositions': ('dispositions_ref', 'disposition_id', 'investment.sc03.dispositions'),
}
KINDS = {'fact': 'facts', 'event': 'events', 'hypothesis': 'hypotheses',
         'observation': 'observations', 'disposition': 'dispositions', 'counterevidence': 'counterevidence'}
ACCEPTED = {'source_reported', 'cross_checked'}


def pointer(value, path):
    """只解析当前对象内准确JSON Pointer，不解析外部引用。"""
    for token in path.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def target_key(kind, identity, path, dimension):
    return canonical([kind, identity, path, dimension]).decode('utf-8')


def time_bounds(value):
    if value['status'] != 'known':
        return None
    body = value['value']
    lower, upper = instant(body['lower_bound']), instant(body['upper_bound'])
    require(lower <= upper, 'A03', '/time', 'Time bounds are reversed')
    require(body['precision'] != 'instant' or lower == upper, 'A03', '/time', 'Instant has unequal bounds')
    return lower, upper


def number_literals(text):
    """独立读取十进制与明确分数原值，不转换单位、不猜经济含义。"""
    values = set()
    pattern = r'(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?:-\d+/\d+|/\d+)?(?![\w.])'
    for match in re.finditer(pattern, text):
        token = match.group().replace(',', '')
        if '/' not in token:
            values.add(token)
            continue
        whole, fraction = ('0', token)
        mixed = re.fullmatch(r'(\d+)-(\d+/\d+)', token)
        if mixed:
            whole, fraction = mixed.groups()
        numerator, denominator = fraction.split('/')
        if Decimal(denominator) == 0:
            continue
        with localcontext() as context:
            context.prec = 80
            value = Decimal(whole) + Decimal(numerator) / Decimal(denominator)
            encoded = format(value, 'f')
            if len(encoded.partition('.')[2]) <= 18:
                values.add(encoded.rstrip('0').rstrip('.') if '.' in encoded else encoded)
    return values


@dataclass
class Snapshot:
    ctx: object
    corpus: dict
    protocol: dict
    identity: dict
    parameters: dict
    payload: dict
    documents: dict
    bodies: dict
    tables: dict
    indexes: dict
    pending: list
    coverage: list
    upstream_receipts: dict
    quotes: dict = field(default_factory=dict)

    @property
    def inherited_gaps(self):
        unique = {canonical(gap): gap for _, receipt in self.upstream_receipts.values() for gap in receipt['blockers']}
        return list(unique.values())

    @classmethod
    def load(cls, ctx):
        require(ctx.role == 'validator' and ctx.candidate is not None and ctx.candidate_ref is not None,
                'A01', '/candidate', 'Independent validator requires a completed candidate')
        ctx.schemas.validate('investment.stage_input', ctx.input)
        ctx.schemas.validate('investment.candidate_output', ctx.candidate)
        require(ctx.resolver.resolve(ctx.candidate_ref, 'investment.candidate_output') == ctx.candidate,
                'A01', '/candidate', 'Candidate object differs from its current immutable artifact')
        corpus = ctx.read_role('evidence_corpus_ref', 'investment.evidence_corpus')
        protocol = ctx.read_role('assessment_protocol_ref', 'investment.sc03.assessment_protocol')
        identity = ctx.read_role('analyst_identity_ref', 'investment.sc03.analyst_identity')
        parameters = ctx.resolver.resolve(ctx.input['parameters_ref'], 'investment.sc03.assessment_parameters')
        payload = ctx.resolver.resolve(ctx.candidate['payload_ref'], 'investment.sc03.research_assessment')
        documents = ctx.resolver.resolve(corpus['documents_ref'], 'investment.news_documents')['rows']
        require(len({row['version_id'] for row in documents}) == len(documents),
                'A02', '/documents', 'Duplicate document version')
        documents = {row['version_id']: row for row in documents}
        bodies = {key: ctx.resolver.resolve(row['body_ref'], 'investment.news_body')
                  for key, row in documents.items()}
        tables, indexes = {}, {}
        for name, (field_name, primary_key, schema_id) in TABLES.items():
            rows = ctx.resolver.resolve(payload[field_name], schema_id)['rows']
            require(len({row[primary_key] for row in rows}) == len(rows),
                    'A02', '/' + field_name, 'Duplicate business primary key')
            tables[name] = rows
            indexes[name] = {row[primary_key]: row for row in rows}
        pending = ctx.resolver.resolve(corpus['pending_items_ref'], 'investment.news_pending_items')['rows']
        coverage = ctx.resolver.resolve(corpus['coverage_ref'], 'investment.news_coverage')['rows']
        present_roles = {item['role'] for item in ctx.input['input_refs']}
        upstream = {item['role']: ctx.upstream_receipt(item['role'])
                    for item in ctx.definition['input_roles']
                    if item['role'] in present_roles and item['source']['kind'] == 'upstream_output'}
        return cls(ctx, corpus, protocol, identity, parameters, payload, documents, bodies,
                   tables, indexes, pending, coverage, upstream)

    def quote(self, quote):
        version = quote['document_version_id']
        require(version in self.documents, 'A03', '/document_version_id', 'Quote is outside the frozen corpus')
        require(quote['body_ref'] == self.documents[version]['body_ref'],
                'A03', '/body_ref', 'Quote body is not the exact upstream version')
        source = self.bodies[version][quote['text_field']]
        require(0 <= quote['start'] < quote['end'] <= len(source)
                and source[quote['start']:quote['end']] == quote['quote'],
                'A03', '/quote', 'Quote does not equal the exact Unicode source slice')
        previous = self.quotes.get(quote['quote_id'])
        require(previous is None or previous == quote, 'A03', '/quote_id', 'Quote identity has conflicting content')
        self.quotes[quote['quote_id']] = quote

    def facts_exist(self, identities, rule, path):
        require(set(identities) <= set(self.indexes['facts']), rule, path, 'Unknown supporting fact')


class Checks:
    """每条规则必须真实执行；失败与证据不足不被默认通过覆盖。"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.results = {}
        self.blockers = []

    def set(self, rule, verdict='passed', reason='Checked against current frozen input'):
        current = self.results.get(rule)
        rank = {'not_applicable': 0, 'passed': 1, 'insufficient_evidence': 2, 'failed': 3}
        if current and current['verdict'] in ('failed', 'insufficient_evidence') and rank[current['verdict']] > rank[verdict]:
            return
        self.results[rule] = {'rule_id': rule, 'verdict': verdict, 'reason': reason[:2048], 'evidence_refs': []}

    def fail(self, rule, path, reason, *, insufficient=False, uses=None):
        self.set(rule, 'insufficient_evidence' if insufficient else 'failed', path + ': ' + reason)
        gap = {'rule_id': rule, 'field_or_object': path if path != '/' else '/candidate',
               'reason': reason[:2048], 'owner': 'validator', 'required_evidence': ['source_bytes'],
               'affected_uses': uses or ['evidence_review', 'hypothesis_research', 'reporting']}
        if gap not in self.blockers:
            self.blockers.append(gap)

    def run(self, rule, action):
        try:
            action()
        except ContractError as error:
            self.fail(rule, error.pointer, str(error))
        else:
            self.set(rule)

    def verdict(self, rule):
        return self.results.get(rule, {}).get('verdict')

    def receipt(self, payload, evidence_refs, *, inherited=False):
        require(set(self.results) == set(self.ctx.definition['required_rules']),
                'A14', '/checks', 'Not all independent rules were executed')
        critical = all(self.verdict(rule) == 'passed' for rule in self.ctx.definition['critical_rules'])
        allowed = [item['use'] for item in self.ctx.definition['use_rules'] if critical and
                   all(self.verdict(rule) == 'passed' for rule in item['required_pass_rules'])]
        for item in self.results.values():
            item['evidence_refs'] = evidence_refs
        usable = 'hypothesis_research' in allowed and not inherited
        failed = any(item['verdict'] == 'failed' for item in self.results.values())
        blockers = list(payload['unresolved_questions'])
        blockers.extend(gap for gap in self.blockers if gap not in blockers)
        return {'validator_id': 'sc03-validator-v1', 'validator_version': 'v1',
                'run_id': self.ctx.run_id, 'stage_id': 'SC03', 'input_refs': self.ctx.input['input_refs'],
                'candidate_output_ref': self.ctx.candidate_ref, 'validated_at': now(),
                'checks': [self.results[rule] for rule in self.ctx.definition['required_rules']],
                'overall_verdict': 'failed' if failed else 'passed' if usable and not payload['unresolved_questions'] else 'partial',
                'business_result': 'rejected' if failed else 'usable' if usable else 'observation_only' if allowed else 'blocked',
                'allowed_uses': allowed, 'blockers': blockers}
