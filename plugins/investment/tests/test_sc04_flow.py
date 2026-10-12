"""SC04 方法/真实原文基础及真实Controller场景；不构造上游Seal或宿主响应。"""
import base64
import hashlib
import json
import os
import re
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from investment.contracts import artifact, canonical, is_known, known, na, require, unknown
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.research.controller import ResearchController, _manifest
from investment.research.references import ResearchResolver
from investment.runtime.paths import validate_input_path
from investment.runtime.runs import atomic
from investment.chains.fund_universe.producer_rules import available_at, partition, reconcile_entries
from investment.chains.fund_universe.source_production import parse_directory, retrieve, source_entry

SELECTORS = ('paginated_discovery', 'unknown_identity', 'mandatory_risks', 'historical_scope_gap')
ACTUAL_RUNS = []
FLOW_START_STATES = {}


def _invocation_start_state(root, case):
    root = guard_namespace(Path(root), 'test')
    key = (str(root), case['run_id'], case['invocation_id'])
    if key not in FLOW_START_STATES:
        resolver = ResearchResolver(root, ResearchSchemas(('SC03', 'SC04')), mode='test')
        incoming = validate_input_path(root, root / case['input'])
        reference = resolver.path_ref(incoming, 'investment.stage_input')
        stage, parts = resolver.run_parts(reference['uri'])
        value = resolver.resolve(reference, 'investment.stage_input')
        require(stage == 'sc04' and len(parts) == 3 and parts[1] == case['invocation_id']
                and value['stage_id'] == 'SC04' and value['run_id'] == case['run_id']
                and value['invocation_id'] == case['invocation_id'],
                'RS03', '/input', 'Flow admission differs from its exact case identity', 'identity_mismatch')
        manifest = _manifest(resolver, 'SC04', parts[0])
        existing = next((row for row in manifest['invocations'] if row['invocation_id'] == case['invocation_id']), None)
        require(existing is None or existing['run_id'] == case['run_id'],
                'RS03', '/run_id', 'Existing flow belongs to a different run', 'identity_mismatch')
        FLOW_START_STATES[key] = existing['state'] if existing is not None else 'not_started'
    return FLOW_START_STATES[key]


def prepare_flow_admission(root, cases_path, selectors):
    FLOW_START_STATES.clear()
    root = guard_namespace(Path(root), 'test')
    path = validate_input_path(root, cases_path)
    if not path.exists():
        return
    document = json.loads(path.read_text(encoding='utf-8'))
    for selector in selectors:
        if selector in document['cases']:
            _invocation_start_state(root, document['cases'][selector])


class OriginalList(HTMLParser):
    """独立标准库扫描器，只从原code_content范围计算产品条目及URL代码。"""
    def __init__(self, source_url):
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.depth, self.current, self.entries = 0, None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and attrs.get('id') == 'code_content':
            self.depth = 1
        elif tag == 'div' and self.depth:
            self.depth += 1
        if not self.depth:
            return
        if tag == 'li':
            self.current = {'codes': set(), 'nonempty': False}
        if self.current is not None:
            self.current['nonempty'] |= bool(attrs.get('title') or attrs.get('href'))
            if tag == 'a':
                match = re.fullmatch(r'https?://(?:fund|fundf10)\.eastmoney\.com/([0-9]{6})\.html', urljoin(self.source_url, attrs.get('href', '')))
                if match:
                    self.current['codes'].add(match.group(1))

    def handle_data(self, text):
        if self.depth and self.current is not None and text.strip():
            self.current['nonempty'] = True

    def handle_endtag(self, tag):
        if tag == 'li' and self.current is not None:
            if self.current['nonempty']:
                self.entries.append(self.current)
            self.current = None
        if tag == 'div' and self.depth:
            self.depth -= 1


def original_scan(body, source_url):
    # 标签、属性、代码是ASCII；latin1逐字节映射避免共用生产者的HTML编码/建树实现。
    parser = OriginalList(source_url)
    parser.feed(body.decode('latin1'))
    parser.close()
    return parser.entries


class ProducerAlgebra(unittest.TestCase):
    """只断言内部集合与时间代数前提，不是业务Artifact或基金身份事实。"""
    def test_unknown_identity_is_not_excluded_and_one_group_does_not_override_another(self):
        stamp = '2026-10-19T00:00:00Z'
        # 符号化代数前提；不送入StageContext，不自称真实身份证据。
        identity = {'entity_id': known('entity'), 'legal_fund_id': known('legal'), 'share_class_id': known('share'),
                    'currency': known('CNY'), 'identity_citations': [],
                    'available_time': known({'lower_bound': stamp, 'upper_bound': stamp}), 'availability_bound_at': known(stamp)}
        eligibility = {'predicates': [{'predicate_id': 'currency', 'field': 'currency', 'operator': 'equals', 'accepted_values': ['CNY']}]}
        provenance = [{'member_id': 'known', 'peer_group_id': 'a', 'relation': 'proven_not_peer'},
                      {'member_id': 'known', 'peer_group_id': 'b', 'relation': 'confirmed'},
                      {'member_id': 'unknown', 'peer_group_id': 'a', 'relation': 'possible'},
                      {'member_id': 'unknown', 'peer_group_id': 'b', 'relation': 'possible'}]
        _, groups, global_status = partition(['known', 'unknown'], {'a': {}, 'b': {}}, {'known': identity, 'unknown': None}, provenance, eligibility, '2026-10-20T00:00:00Z')
        self.assertEqual(global_status, {'known': 'candidate', 'unknown': 'unknown'})
        self.assertEqual(groups['a']['excluded'], ['known'])
        self.assertEqual(groups['b']['candidate'], ['known'])

    def test_unknown_or_later_availability_cannot_be_backfilled(self):
        self.assertFalse(available_at({'available_time': unknown('unproven'), 'availability_bound_at': known('2026-10-20T00:00:00Z')}, '2026-10-19T00:00:00Z'))
        self.assertFalse(available_at({'available_time': known({'lower_bound': '2026-10-20T00:00:00Z', 'upper_bound': '2026-10-20T00:00:00Z'}),
                                       'availability_bound_at': known('2026-10-20T00:00:00Z')}, '2026-10-19T00:00:00Z'))


class ActualProducerSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        selected = os.environ.get('SC04_PRODUCER_TEST_ROOT')
        if not selected:
            raise unittest.SkipTest('No isolated source-calibration root supplied')
        cls.root = guard_namespace(Path(selected), 'test')
        cls.directory = cls.root / 'sources' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        cls.directory.mkdir(parents=True, exist_ok=True)
        cls.schemas = ResearchSchemas(('SC03', 'SC04'))
        cls.samples = []
        for index in (1, 2):
            retrieval = retrieve('https://fund.eastmoney.com/allfund.html', max_bytes=16 * 1024 * 1024, timeout=45)
            metadata = {'started_at': retrieval.started_at, 'finished_at': retrieval.finished_at, 'outcome': retrieval.outcome,
                        'reason': retrieval.reason, 'complete_body': retrieval.complete_body}
            atomic(cls.directory / ('retrieval-' + str(index) + '.json'), canonical(metadata), immutable=True)
            if retrieval.outcome != 'retrieved':
                raise unittest.SkipTest('Actual public source is unavailable: ' + retrieval.outcome + '; actual retrieval record retained')
            capture = retrieval.capture
            cls.schemas.validate('investment.sc04.capture', capture)
            path = cls.directory / ('allfund-' + str(index) + '.capture.json')
            atomic(path, canonical(capture), immutable=True)
            cls.samples.append((capture, path))

    def test_full_original_scope_matches_independent_byte_scan(self):
        for capture, _ in self.samples:
            body = base64.b64decode(capture['body_base64'], validate=True)
            self.assertEqual(hashlib.sha256(body).hexdigest(), capture['body_sha256'])
            self.assertEqual(len(body), capture['body_byte_size'])
            expected = original_scan(body, capture['response_url'])
            page = parse_directory(capture)
            expected_codes = set().union(*(row['codes'] for row in expected))
            self.assertEqual({row.code for row in page.entries if row.code is not None}, expected_codes)
            self.assertEqual(len(page.entries), len(expected))
            self.assertGreater(len(expected), 1)

    def test_same_observed_code_without_legal_share_evidence_is_not_merged(self):
        parsed = [parse_directory(capture) for capture, _ in self.samples]
        common = {entry.code for entry in parsed[0].entries if entry.code} & {entry.code for entry in parsed[1].entries if entry.code}
        self.assertTrue(common)
        code = min(common)
        entries, identities = [], {}
        for index, ((capture, path), page) in enumerate(zip(self.samples, parsed)):
            observed = next(entry for entry in page.entries if entry.code == code)
            raw_ref = artifact(canonical(capture), 'investment.sc04.capture', path.as_uri())
            row = source_entry(observed, source_id='eastmoney-calibration', task_id='calibration-' + str(index), raw_ref=raw_ref)
            entry_path = self.directory / ('entry-' + str(index) + '.json')
            atomic(entry_path, canonical(row), immutable=True)
            reference = artifact(canonical(row), 'investment.sc04.source_entry', entry_path.as_uri())
            member = row['entry_id'].replace('entry-', 'member-', 1)
            entries.append((member, row, reference))
            identities[member] = None
        sources, _, _, _, _ = reconcile_entries(entries, identities, [], [], '2026-10-20T00:00:00Z', ['reporting'])
        self.assertEqual(len(sources), 2)
        self.assertEqual({item[2]['uri'] for group in sources.values() for item in group}, {item[2]['uri'] for item in entries})


class ActualSC04Flow(unittest.TestCase):
    def _run(self, selector):
        selected = os.environ.get('SC04_PRODUCER_TEST_ROOT')
        if not selected:
            self.skipTest('Actual legal SC01/SC02/SC03 run and isolated SC04 cases are not supplied')
        root = guard_namespace(Path(selected), 'test')
        path = validate_input_path(root, Path(os.environ.get('SC04_FLOW_CASES', str(root / 'sc04-flow-cases.json'))))
        if not path.exists():
            self.skipTest('No independently prepared cases with actual upstream seals; no synthetic Seal or Host response is created')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['schema_version'], 1)
        self.assertIn(manifest['source_kind'], ('synthetic_fixture', 'actual_source_snapshot'))
        if selector not in manifest['cases']:
            self.skipTest('Independent legal case is not supplied for ' + selector)
        case = manifest['cases'][selector]
        self.assertTrue(case['expectation']['observed_sources'], 'Independent original-source baselines must be prepared before running this case')
        incoming = validate_input_path(root, root / case['input'])
        if _invocation_start_state(root, case) == 'completed':
            self.skipTest('SC04 flow was already completed before this verification run; prior seal is not fresh execution')
        controller = ResearchController(root, mode='test', stage_id='SC04')
        result = controller.run(incoming, run_id=case['run_id'], invocation_id=case['invocation_id'])
        ACTUAL_RUNS.append({'selector': selector, 'source_kind': manifest['source_kind'], 'result': result})
        if result['status'] == 'host_work_required':
            self.skipTest('Actual fresh host work is pending; resume the same frozen invocation')
        self.assertEqual(result['status'], 'completed', 'Actual controller failed; inspect its retained failure_refs')
        resolver = controller.resolver
        sealed = resolver.resolve(result['sealed_output_ref']['value'], 'investment.sealed_output')
        candidate = resolver.resolve(sealed['candidate_output_ref'], 'investment.candidate_output')
        receipt = resolver.resolve(sealed['validation_receipt_ref'], 'investment.validation_receipt')
        payload = resolver.resolve(candidate['payload_ref'], 'investment.sc04.instrument_universe')
        audit = resolver.resolve(payload['discovery_audit_ref'], 'investment.sc04.discovery_audit')
        coverage = resolver.resolve(payload['coverage_ref'], 'investment.sc04.coverage')
        frozen = resolver.resolve(result['input_ref'], 'investment.stage_input')
        self.assertEqual(candidate['input_refs'], frozen['input_refs'])
        members = resolver.resolve(payload['members_ref'], 'investment.sc04.members')['rows']
        partitions = {status: resolver.resolve(payload[field], 'investment.sc04.dispositions')['rows'] for status, field in (
            ('candidate', 'candidate_members_ref'), ('excluded', 'excluded_members_ref'), ('unknown', 'unknown_members_ref'))}
        sets = {status: {row['member_id'] for row in rows} for status, rows in partitions.items()}
        self.assertEqual(set.union(*sets.values()), {row['member_id'] for row in members})
        self.assertFalse(sets['candidate'] & sets['excluded'] or sets['candidate'] & sets['unknown'] or sets['excluded'] & sets['unknown'])
        index = {}
        for member in members:
            for reference in member['source_entry_refs']:
                entry = resolver.resolve(reference, 'investment.sc04.source_entry')
                capture = resolver.resolve(entry['raw_ref'], 'investment.sc04.capture')
                index[(capture['request_url'], entry['locator'])] = member['member_id']
        for observed in case['expectation'].get('observed_sources', []):
            source_path = validate_input_path(root, root / observed['body_path'])
            expected = original_scan(source_path.read_bytes(), observed['request_url'])
            captures = [resolver.resolve(ref, 'investment.sc04.capture') for ref in audit['original_list_refs']]
            actual = next(row for row in captures if row['request_url'] == observed['request_url'])
            self.assertEqual(base64.b64decode(actual['body_base64'], validate=True), source_path.read_bytes())
            self.assertEqual(len([key for key in index if key[0] == observed['request_url']]), len(expected))
        return case['expectation'], resolver, frozen, payload, audit, coverage, receipt, members, sets, index

    def test_paginated_discovery(self):
        expected, resolver, _, _, audit, coverage, _, _, _, _ = self._run('paginated_discovery')
        self.assertTrue(expected['visible_next_urls'], 'A real paginated source is required; a single-document catalog is not this selector')
        tasks = resolver.resolve(audit['query_tasks_ref'], 'investment.sc04.query_tasks')['rows']
        scopes = resolver.resolve(audit['unprocessed_scope_ref'], 'investment.sc04.unprocessed_scope')['rows']
        self.assertTrue(set(expected['visible_next_urls']) <= {row['request_url'] for row in tasks})
        unresolved = {row['task_id']['value'] for row in scopes if is_known(row['task_id'])}
        self.assertTrue(set(expected['unprocessed_urls']) <= {row['request_url'] for row in tasks if row['task_id'] in unresolved})
        if expected['unprocessed_urls']:
            self.assertEqual(audit['completeness_status'], 'unknown')
            self.assertEqual(coverage['current_status'], 'unknown')

    def test_unknown_identity(self):
        expected, _, _, _, _, coverage, receipt, _, sets, index = self._run('unknown_identity')
        self.assertTrue(expected['unknown_entries'])
        for entry in expected['unknown_entries']:
            member = index[(entry['request_url'], entry['locator'])]
            self.assertIn(member, sets['unknown'])
            self.assertNotIn(member, sets['excluded'])
        self.assertEqual(coverage['current_status'], 'unknown')
        self.assertNotIn('selection_comparison', receipt['allowed_uses'])

    def test_mandatory_risks(self):
        _, resolver, frozen, payload, _, _, _, _, _, _ = self._run('mandatory_risks')
        reference = next(row['ref'] for row in frozen['input_refs'] if row['role'] == 'transaction_context_ref')
        account = resolver.resolve(reference, 'investment.transaction_context')
        positions = resolver.resolve(account['positions_ref'], 'investment.positions')['rows']
        pending = resolver.resolve(account['pending_transactions_ref'], 'investment.pending_transactions')['rows']
        self.assertTrue(positions)
        self.assertTrue({'buy', 'sell'} <= {row['side'] for row in pending})
        expected = {('registered_position', row['instrument_id'], None) for row in positions}
        expected.update(('pending_' + row['side'], row['instrument_id'], row['trade_id']) for row in pending)
        rows = resolver.resolve(payload['mandatory_assets_ref'], 'investment.sc04.mandatory_assets')['rows']
        actual = {(row['risk_kind'], row['instrument_id']['value'], row['trade_id']['value'] if is_known(row['trade_id']) else None) for row in rows if row['risk_kind'] != 'cash_comparator'}
        self.assertEqual(actual, expected)
        self.assertEqual(len([row for row in rows if row['risk_kind'] != 'cash_comparator']), len(expected))
        self.assertTrue(all(row['retention_status'] == 'retained' and row['amount']['status'] == 'unknown' for row in rows if row['risk_kind'] != 'cash_comparator'))

    def test_historical_scope_gap(self):
        expected, resolver, frozen, payload, _, coverage, receipt, _, _, _ = self._run('historical_scope_gap')
        reference = next(row['ref'] for row in frozen['input_refs'] if row['role'] == 'scope_time_request_ref')
        request = resolver.resolve(reference, 'investment.sc04.scope_time_request')
        self.assertTrue(request['historical_origins'])
        statuses = {row['origin_at']: row['status'] for row in coverage['historical_origin_status']}
        self.assertEqual(set(statuses), set(request['historical_origins']))
        self.assertTrue(expected['unknown_origins'])
        self.assertTrue(all(statuses[origin] == 'unknown' for origin in expected['unknown_origins']))
        self.assertEqual(coverage['historical_status'], 'unknown')
        self.assertNotIn('pit_training', receipt['allowed_uses'])
        history = resolver.resolve(payload['universe_history_ref']['value'], 'investment.sc04.history')['rows'] if is_known(payload['universe_history_ref']) else []
        self.assertTrue(all(row['membership_status'] == 'unknown' for row in history if row['origin_at'] in expected['unknown_origins']))
