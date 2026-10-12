"""批准的SC02场景与全部真实来源验收；实际调用主控及独立验证进程。"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from investment.contracts import SPEC, artifact, canonical, dataset, digest, instant, is_known, known, na, now, unknown
from investment.chains.news.contracts import ROLES, REVIEW_USES, SOURCES, NewsResolver, NewsSchemas
from investment.runtime.paths import production_root, test_parent, validate_root

PLUGIN = Path(__file__).resolve().parents[1]
CASES = ('multi_source', 'wait_all_partial', 'pagination_duplicates', 'unknown_time_or_layout',
         'plugin_add_remove', 'all_failed_three_attempts', 'fresh_invocation', 'isolation')
LIVE_LIMITS = {'global_concurrency': 8, 'per_source_concurrency': 2, 'timeout_ms': 10000,
               'response_byte_limit': 8388608, 'decoded_byte_limit': 16777216, 'request_budget': 256,
               'page_budget': 8, 'document_budget': 4, 'redirect_budget': 3}
LOCAL_LIMITS = dict(LIVE_LIMITS, timeout_ms=3000)


def identity(prefix, *values):
    return prefix + '-' + hashlib.sha256(canonical(list(values))).hexdigest()[:40]


class Scenario:
    """每个场景拥有隔离根、真实发布端、事前预期与独立报告。"""

    def __init__(self, case_id, *, live=False):
        self.case_id, self.live, self.started = case_id, live, now()
        self.root = validate_root(test_parent() / ('sc02-' + case_id + '-' + uuid.uuid4().hex), fixture_mode=True)
        self.root.mkdir()
        self.schemas = NewsSchemas()
        self.plugins = SOURCES if live else self.root / 'plugins'
        self.origin = 'live' if live else 'simulated'
        self.resolver = NewsResolver(self.root, self.schemas, mode='test', network_origin=self.origin, plugins_dir=self.plugins)
        self.responses, self.requests, self.cases, self.outcomes = {}, [], [], []
        self.phase, self.server = 'configuration', None
        self.request_lock = threading.Lock()
        if not live:
            self._start_publisher()
        print('SC02 artifacts: ' + self.root.as_posix(), file=sys.stderr, flush=True)

    def _start_publisher(self):
        owner = self

        class Publisher(BaseHTTPRequestHandler):
            def do_GET(self):
                with owner.request_lock:
                    event = {'path': self.path, 'phase': owner.phase, 'started_at': now(), 'finished_at': None}
                    owner.requests.append(event)
                status, body, delay = owner.responses.get(self.path, (404, b'not found', 0))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    event.update(finished_at=now(), status=status, byte_size=len(body), sha256=digest(body))

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Publisher)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        (self.root / 'publisher-requests.json').write_bytes(canonical(self.requests))

    def _account(self):
        # 同一真实t先于构造和执行；空持仓来源保留synthetic_fixture。
        self.clock, self.run_id = self.started, 'run-' + uuid.uuid4().hex
        invocation = 'account-' + uuid.uuid4().hex
        self.day = instant(self.clock).astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d')
        folder = self.root / 'runs' / 'sc01' / self.day / invocation
        folder.mkdir(parents=True)
        prefix = f'investment-run://sc01/{self.day}/{invocation}/'

        def write(name, value, sid):
            self.schemas.validate(sid, value)
            raw = canonical(value)
            (folder / name).write_bytes(raw)
            return artifact(raw, sid, prefix + name)

        source = {'record_id': 'source-' + uuid.uuid4().hex, 'source_type': 'synthetic_fixture', 'operation': 'initialize',
                  'account_id': 'account-' + uuid.uuid4().hex, 'base_generation': 0, 'confirmed_at': self.clock,
                  'transaction_as_of': self.clock, 'platform': 'user-confirmed-records', 'currency': 'CNY',
                  'opening': {'start_at': self.clock, 'positions': 'confirmed_empty', 'coverage': 'recorded_transactions_since_start'},
                  'changes': {'rows': []}, 'arrival_replies': {'rows': []}}
        source_ref = write('source-record.json', source, 'investment.source_record')
        facts_ref = write('transaction-facts.json', {'source_record_ref': source_ref, 'base_generation': 0}, 'investment.transaction_facts')
        write('changes.json', {'rows': []}, 'investment.trade_changes')
        changes_ref = dataset({'rows': []}, 'investment.trade_changes', prefix + 'changes.json', self.schemas)
        scope = {'scope_id': 'account-scope', 'account_id': source['account_id'], 'operation': 'initialize',
                 'transaction_as_of_target': self.clock, 'account_information_cutoff_at': self.clock,
                 'purpose': 'fixture_validation', 'requested_uses': ['current_account_review', 'reporting']}
        scope_ref = write('scope.json', scope, 'investment.task_scope')
        method_ref = artifact((SPEC / 'method.json').read_bytes(), 'investment.method', 'investment-spec://v1/method.json')
        parameters = {'schema_version': 1, 'stage_id': 'SC01', 'parameters': [
            {'name': name, 'value': value, 'unit': unit, 'source_kind': kind, 'basis_ref': method_ref,
             'determination_rule': 'constant_v1', 'determined_at': self.clock}
            for name, value, unit, kind in (('max_attempts', 3, 'count', 'engineering_budget'),
                ('max_json_bytes', 10485760, 'count', 'engineering_budget'), ('decimal_precision', 80, 'count', 'engineering_budget'),
                ('execution_timezone', 'Asia/Shanghai', 'code', 'registered_business_rule'))]}
        params_ref = write('parameters.json', parameters, 'investment.parameter_set')
        incoming = {'contract_id': 'investment.stage_input', 'contract_version': 1, 'run_id': self.run_id, 'stage_id': 'SC01',
                    'invocation_id': invocation, 'decision_at': self.clock, 'information_cutoff_at': self.clock, 'scope_ref': scope_ref,
                    'input_refs': [{'role': name, 'ref': reference} for name, reference in
                        (('transaction_facts_ref', facts_ref), ('changes_ref', changes_ref), ('task_scope_ref', scope_ref))], 'parameters_ref': params_ref}
        write('submitted-input.json', incoming, 'investment.stage_input')
        command = [sys.executable, '-B', str(PLUGIN / 'scripts/run_sc01.py'), '--root', str(self.root), '--input', str(folder / 'submitted-input.json'),
                   '--run-id', self.run_id, '--invocation-id', invocation, '--fixture-mode']
        process = self.command(command, self.root / 'upstream')
        assert process.returncode == 0, process.stderr.decode('utf-8', errors='replace')
        result = json.loads(process.stdout)
        seal = self.resolver.resolve(result['sealed_output_ref'], 'investment.sealed_output')
        receipt = self.resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
        candidate = self.resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
        self.account_ref = candidate['payload_ref']
        account = self.resolver.resolve(self.account_ref, 'investment.transaction_context')
        assert receipt['business_result'] in ('usable', 'observation_only') and 'reporting' in receipt['allowed_uses']
        assert all(not self.resolver.resolve(account[key])['rows'] for key in ('trades_ref', 'positions_ref', 'pending_transactions_ref'))
        self.upstream_seal = result['sealed_output_ref']

    def command(self, command, prefix):
        process = subprocess.run(command, cwd=PLUGIN, capture_output=True,
            env=dict(os.environ, PYTHONPATH=str(PLUGIN / 'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))
        prefix.with_suffix('.command.json').write_bytes(canonical(command))
        prefix.with_suffix('.stdout.log').write_bytes(process.stdout)
        prefix.with_suffix('.stderr.log').write_bytes(process.stderr)
        return process

    def publish(self, path, body, *, status=200, delay=0):
        self.responses[path] = (status, body.encode('utf-8') if isinstance(body, str) else body, delay)

    def article(self, source_id, slug='doc', *, title=None, text=None, time_text=None, changed=False):
        title, text = title or source_id + ' bulletin', text or source_id + ' independently stated source material.'
        stamp = time_text if time_text is not None else (instant(self.clock) - timedelta(hours=1)).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        markup = '<main>' + text + '</main>' if changed else '<article><p>' + text + '</p></article>'
        raw = ('<html><body><h1>' + title + '</h1>' + markup + ('<time>' + stamp + '</time>' if stamp else '') + '</body></html>').encode('utf-8')
        path = '/' + source_id + '/' + slug
        self.publish(path, raw)
        return {'source_id': source_id, 'url': self.base_url + path, 'path': path, 'title': title, 'text': text, 'stamp': stamp, 'raw': raw}

    def listing(self, source_id, documents, *, page='list', next_page=None, delay=0, status=200):
        links = ''.join('<li><a href="' + item['url'] + '">' + item['title'] + '</a></li>' for item in documents)
        next_link = '<a rel="next" href="/' + source_id + '/' + next_page + '">next</a>' if next_page else ''
        self.publish('/' + source_id + '/' + page, '<html><body><ul>' + links + '</ul>' + next_link + '</body></html>', status=status, delay=delay)

    def install(self, source_id, *, material_kind='official_release'):
        folder = self.plugins / source_id
        shutil.copytree(SOURCES / 'ofac', folder)
        profile = json.loads((folder / 'extraction.json').read_bytes())
        pattern = re.escape(self.base_url + '/' + source_id + '/')
        profile.update(source_id=source_id, limitations=['Local synthetic publisher; whole window unknown'])
        profile['document_rules'] = [{'url_pattern': pattern + r'(?!list(?:\?|$)).+', 'format': 'html', 'title_locator': '//h1',
            'body_locator': '//article', 'time_locator': '//time', 'time_format': 'rfc3339', 'time_role': 'published',
            'timezone': unknown('timezone_in_actual_source_literal'), 'material_kind': material_kind}]
        profile['listing_rules'] = [{'url_pattern': pattern + r'list(?:\?page=\d+)?', 'format': 'html', 'items_locator': '//li/a',
            'url_locator': '@href', 'title_locator': '.', 'time_locator': '', 'next_locator': '//a[@rel="next"]/@href',
            'link_prefix': '', 'material_kind': material_kind, 'time_role': 'published', 'exhaustion_policy': 'paginated_unknown'}]
        profile['entry_follow_rules'] = []
        entry = self.base_url + '/' + source_id + '/list'
        # 安装证据来自本批实际HTTP探测，原始响应和所有请求另存于测试根。
        try:
            with urlopen(entry, timeout=5) as response:
                raw, status = response.read(), response.status
        except HTTPError as error:
            raw, status = error.read(), error.code
        (self.root / ('configuration-' + source_id + '.html')).write_bytes(raw)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest.update(source_id=source_id, publisher_id=source_id, publisher_name='Local synthetic ' + source_id,
            entrypoints=[entry], allowed_hosts=['127.0.0.1'], material_kinds=[material_kind], profile_sha256=digest(canonical(profile)),
            limitations=profile['limitations'], evidence=[{'role': 'entry', 'url': entry, 'captured_at': now(),
                'capture_state': 'complete', 'http_status': known(status), 'response_sha256': digest(raw), 'response_byte_size': len(raw),
                'observation': 'Actual HTTP response from explicitly synthetic local publisher; not real-site integration evidence'}])
        self.schemas.validate('investment.news_manifest', manifest)
        self.schemas.validate('investment.news_profile', profile)
        (folder / 'manifest.json').write_bytes(canonical(manifest))
        (folder / 'extraction.json').write_bytes(canonical(profile))
        assert (folder / 'collect.py').read_bytes() == (SOURCES / 'ofac' / 'collect.py').read_bytes()
        return {'source_id': source_id, 'entrypoints': [entry], 'material_kinds': [material_kind]}

    def input(self, entries):
        invocation = 'news-' + uuid.uuid4().hex
        folder = self.root / 'runs' / 'sc02' / self.day / invocation
        folder.mkdir(parents=True)
        prefix = f'investment-run://sc02/{self.day}/{invocation}/'

        def write(name, value, sid):
            self.schemas.validate(sid, value)
            (folder / name).write_bytes(canonical(value))
            return artifact(canonical(value), sid, prefix + name)

        values = ({'clock_id': 'decision-clock', 'decision_at': self.clock, 'information_cutoff_at': self.clock},
                  {'window_id': 'seven-day-window', 'start_at': (instant(self.clock) - timedelta(days=7)).isoformat(timespec='microseconds').replace('+00:00', 'Z'), 'end_at': self.clock},
                  {'scope_id': 'configured-publishers', 'entries': sorted(copy.deepcopy(entries), key=lambda item: item['source_id'])},
                  {'protocol_id': 'configured-discovery', 'discovery_policy': 'configured_entries', 'limits': LIVE_LIMITS if self.live else LOCAL_LIMITS})
        refs = [write(name, value, sid) for name, value, (_, sid) in zip(('clock.json', 'window.json', 'scope.json', 'query.json'), values, ROLES)]
        refs.append(self.account_ref)
        parameters = {'parameter_set_id': 'sc02-acceptance', 'mode': 'test', 'network_origin': self.origin,
                      'plugins_dir': self.plugins.as_posix(), 'user_agent': 'Investment-SC02/0.0.1 ' + ('live-validation' if self.live else 'synthetic-validation')}
        parameters_ref = write('parameters.json', parameters, 'investment.sc02_parameters')
        incoming = {'contract_id': 'investment.stage_input', 'contract_version': 1, 'run_id': self.run_id, 'stage_id': 'SC02',
                    'invocation_id': invocation, 'decision_at': self.clock, 'information_cutoff_at': self.clock, 'scope_ref': refs[2],
                    'input_refs': [{'role': role, 'ref': reference} for (role, _), reference in zip(ROLES, refs)], 'parameters_ref': parameters_ref}
        reference = write('submitted-input.json', incoming, 'investment.stage_input')
        return folder, incoming, reference

    def execute(self, entries, label, trigger, expected):
        folder, incoming, reference = self.input(entries)
        command = [sys.executable, '-B', str(PLUGIN / 'scripts/run_sc02.py'), '--root', str(self.root), '--mode', 'test',
                   '--network-origin', self.origin, '--plugins-dir', str(self.plugins), '--input', str(folder / 'submitted-input.json'),
                   '--run-id', self.run_id, '--invocation-id', incoming['invocation_id']]
        case = {'case_id': label, 'entry': json.dumps(command) + '; verified zero-holding SC01=' + self.upstream_seal['uri'],
                'trigger': trigger, 'expected': expected, 'actual': 'not yet executed', 'passed': False,
                'input_ref': known(reference), 'candidate_ref': unknown('execution_pending'), 'receipt_ref': unknown('execution_pending')}
        self.write_report(self.root / ('admission-' + label + '.json'), [case], [])
        self.cases.append(case)
        self.phase = label
        process = self.command(command, self.root / label)
        outcome = {'code': process.returncode, 'business_succeeded': False, 'output_valid': False, 'goal_satisfied': False, 'folder': folder}
        self.outcomes.append(outcome)
        case['actual'] = 'exit=' + str(process.returncode) + '; stderr=' + process.stderr.decode('utf-8', errors='replace')[:1800]
        if (folder / 'input.json').exists():
            case['input_ref'] = known(self.resolver.path_ref(folder / 'input.json', 'investment.stage_input'))
        if not process.stdout:
            case.update(candidate_ref=na('candidate_not_produced'), receipt_ref=na('validation_not_run'))
            return outcome
        result = self.schemas.document('investment.sc02_run_result', process.stdout)
        manifest = self.schemas.document('investment.sc02_daily_manifest', (folder.parent / 'manifest.json').read_bytes())
        invocation = next(item for item in manifest['invocations'] if item['invocation_id'] == incoming['invocation_id'])
        outcome.update(result=result, invocation=invocation)
        last = invocation['attempts'][-1]
        case['candidate_ref'] = last['candidate_ref'] if is_known(last['candidate_ref']) else na('candidate_not_produced')
        case['receipt_ref'] = last['receipt_ref'] if is_known(last['receipt_ref']) else na('validation_not_run')
        for key, sid in (('input_ref', 'investment.stage_input'), ('candidate_ref', 'investment.candidate_output'), ('receipt_ref', 'investment.validation_receipt')):
            if is_known(case[key]):
                self.resolver.resolve(case[key]['value'], sid)
        if result['status'] == 'sealed':
            seal = self.resolver.resolve(result['sealed_output_ref']['value'], 'investment.sealed_output')
            candidate = self.resolver.resolve(seal['candidate_output_ref'], 'investment.candidate_output')
            receipt = self.resolver.resolve(seal['validation_receipt_ref'], 'investment.validation_receipt')
            frozen = self.resolver.resolve(case['input_ref']['value'], 'investment.stage_input')
            assert seal['candidate_output_ref'] == case['candidate_ref']['value'] and seal['validation_receipt_ref'] == case['receipt_ref']['value']
            assert receipt['candidate_output_ref'] == seal['candidate_output_ref'] and receipt['input_refs'] == frozen['input_refs']
            assert receipt['run_id'] == candidate['run_id'] == self.run_id and receipt['stage_id'] == candidate['stage_id'] == 'SC02'
            assert [item['rule_id'] for item in receipt['checks']] == [f'N{number:02}' for number in range(1, 13)]
            assert all(item['verdict'] == 'passed' or item['rule_id'] == 'N10' and item['verdict'] == 'insufficient_evidence' for item in receipt['checks'])
            assert receipt['business_result'] in ('usable', 'observation_only') and receipt['allowed_uses'] == REVIEW_USES
            corpus = self.resolver.resolve(candidate['payload_ref'], 'investment.evidence_corpus')
            tables = {name: self.resolver.resolve(corpus[name + '_ref'])['rows'] for name in
                      ('documents', 'retrieval_attempts', 'opportunity_inventory', 'pending_items', 'extraction_spec', 'coverage')}
            captures = [self.resolver.resolve(ref, 'investment.news_capture') for ref in candidate['evidence_refs'][1:]]
            assert [item['sequence_no'] for item in captures] == list(range(1, len(captures) + 1))
            assert {item['capture_id'] for item in captures} == {item['capture_id'] for item in tables['retrieval_attempts']}
            assert all(item['origin'] == ('live' if self.live else 'synthetic') for item in captures)
            assert {item['source_id'] for item in tables['coverage']} == {item['source_id'] for item in entries}
            assert result['allowed_uses'] == REVIEW_USES and result['goal_status'] == 'unmet'
            outcome.update(candidate=candidate, receipt=receipt, corpus=corpus, tables=tables, captures=captures,
                           business_succeeded=process.returncode == 0, output_valid=True, goal_satisfied=False)
        capture_count = sum(len(self.resolver.resolve(item['capture_log_ref']['value'], 'investment.news_capture_log')['captures'])
                            for item in invocation['attempts'] if is_known(item['capture_log_ref']))
        failures = [self.resolver.resolve(item['failure_ref']['value'], 'investment.sc02_failure')
                    for item in invocation['attempts'] if is_known(item['failure_ref'])]
        case['actual'] = json.dumps({'exit': process.returncode, 'status': result['status'], 'attempts': result['attempt_count'],
            'goal': result['goal_status'], 'documents': len(outcome.get('tables', {}).get('documents', [])),
            'captures': capture_count, 'failure_causes': [{key: item[key] for key in ('attempt_no', 'phase', 'rule_id', 'code', 'pointer', 'reason')}
                for item in failures], 'path': folder.as_posix()})
        return outcome

    def assert_materials(self, outcome, expected):
        assert outcome['code'] == 0, self.cases[-1]['actual']
        documents = outcome['tables']['documents']
        assert {item['requested_url'] for item in documents} == {item['url'] for item in expected}
        by_url = {item['url']: item for item in expected}
        for document in documents:
            fixture = by_url[document['requested_url']]
            body, capture = self.resolver.resolve(document['body_ref']), self.resolver.resolve(document['raw_ref'])
            assert base64.b64decode(capture['response_bytes_base64']) == fixture['raw']
            assert document['version_id'] == 'ver-' + hashlib.sha256(fixture['raw']).hexdigest()
            assert document['document_id'] == identity('doc', fixture['source_id'], fixture['url'])
            assert body['title'] == fixture['title'] and body['text'] == fixture['text']
            assert document['available_time']['status'] == 'unknown' and document['availability_basis'] == 'unknown_before_cutoff'
            if fixture['stamp']:
                assert body['source_time'] == known(fixture['stamp']) and document['window_membership'] == 'within'
                assert document['published_time']['value']['lower_bound'] == fixture['stamp']
            else:
                assert body['source_time']['status'] == document['published_time']['status'] == 'unknown'
                assert document['window_membership'] == 'uncertain'

    def write_report(self, path, cases, outcomes):
        report = {'report_id': 'sc02-' + self.case_id, 'origin': 'synthetic', 'root': self.root.as_posix(),
                  'started_at': self.started, 'finished_at': now(), 'cases': copy.deepcopy(cases),
                  'execution_succeeded': bool(outcomes) and all(item['business_succeeded'] for item in outcomes),
                  'output_valid': bool(outcomes) and all(item['output_valid'] for item in outcomes),
                  'goal_satisfied': bool(outcomes) and all(item['goal_satisfied'] for item in outcomes)}
        self.schemas.validate('investment.sc02_unit_report', report)
        path.write_bytes(canonical(report))
        return report

    def finish(self, error=None):
        if error:
            (self.root / 'scenario-error.log').write_text(error, encoding='utf-8')
            if not self.cases:
                self.cases.append({'case_id': self.case_id, 'entry': 'verify_sc02.py ' + self.case_id,
                    'trigger': 'Construct current legal SC01 zero-holding prestate before the approved SC02 scenario.',
                    'expected': 'Actual SC01 seal and legal current SC02 input precede collection; never fabricate upstream artifacts.',
                    'actual': error.splitlines()[-1][:4096], 'passed': False, 'input_ref': na('input_not_created'),
                    'candidate_ref': na('candidate_not_produced'), 'receipt_ref': na('validation_not_run')})
            if self.outcomes:
                self.outcomes[-1]['output_valid'] = False
            if self.cases:
                self.cases[-1]['actual'] = (self.cases[-1]['actual'] + '; ' + error.splitlines()[-1])[:4096]
        else:
            for case in self.cases:
                case['passed'] = True
        return self.write_report(self.root / 'verification-report.json', self.cases, self.outcomes)


def multi_source(s):
    materials, entries = [], []
    for source_id, title, text, kind in (
        ('macro', 'Policy rate statement', 'Policy committee publishes its rate decision.', 'official_release'),
        ('health', 'Regional health bulletin', 'Regional disease surveillance records 12 cases.', 'article'),
        ('science', 'Research launch', 'An ocean research vessel begins a mapping campaign.', 'article')):
        material = s.article(source_id, title=title, text=text)
        s.listing(source_id, [material])
        entries.append(s.install(source_id, material_kind=kind))
        materials.append(material)
    outcome = s.execute(entries, 'multi_source', 'Verified zero holdings; finance, health and science HTTP publishers.',
        'Exactly three stated source documents/versions and six execution requests; no exposure inference; all coverage unknown; observation_only; goal unmet.')
    s.assert_materials(outcome, materials)
    assert len(outcome['captures']) == 6 and len(outcome['tables']['opportunity_inventory']) == 3
    assert not outcome['tables']['pending_items']


def wait_all_partial(s):
    materials, entries = [], []
    for source_id in ('fast', 'slow', 'outage'):
        material = s.article(source_id)
        s.listing(source_id, [material], delay=0.8 if source_id == 'slow' else 0, status=503 if source_id == 'outage' else 200)
        entries.append(s.install(source_id))
        if source_id != 'outage':
            materials.append(material)
    outcome = s.execute(entries, 'wait_all_partial', 'Slow publisher overlaps other source work; outage returns actual 503.',
        'Wait all three terminal sources; exact fast/slow materials, outage failed; five captures; partial research permission only.')
    s.assert_materials(outcome, materials)
    assert len(outcome['captures']) == 5
    assert {item['source_id']: item['status'] for item in outcome['tables']['coverage']} == {'fast': 'partial', 'slow': 'partial', 'outage': 'failed'}
    events = [item for item in s.requests if item['phase'] == 'wait_all_partial']
    slow = next(item for item in events if item['path'] == '/slow/list')
    assert any(instant(item['started_at']) < instant(slow['finished_at']) and instant(item['finished_at']) > instant(slow['started_at']) for item in events if item is not slow)
    seal = s.resolver.resolve(outcome['result']['sealed_output_ref']['value'])
    assert all(instant(item['finished_at']) <= instant(seal['sealed_at']) for item in events)


def pagination_duplicates(s):
    first, second = s.article('pages', 'alpha'), s.article('pages', 'beta')
    s.listing('pages', [first, first], next_page='list?page=2')
    s.listing('pages', [first, second], page='list?page=2')
    outcome = s.execute([s.install('pages')], 'pagination_duplicates', 'Two exposed HTTP pages repeat alpha URL three times.',
        'Exactly alpha/beta documents and two opportunities; alpha three discovery locators; four actual captures including both pages; no pending.')
    s.assert_materials(outcome, [first, second])
    assert len(outcome['captures']) == 4 and not outcome['tables']['pending_items']
    opportunities = {item['url']: item for item in outcome['tables']['opportunity_inventory']}
    assert set(opportunities) == {first['url'], second['url']} and len(opportunities[first['url']]['discovered_in']) == 3
    assert all(item['disposition'] == 'captured' for item in opportunities.values())


def unknown_time_or_layout(s):
    untimed = s.article('unknown', 'untimed', title='Untimed bulletin', time_text='')
    changed = s.article('unknown', 'changed', title='Changed layout', changed=True)
    s.listing('unknown', [untimed, changed])
    outcome = s.execute([s.install('unknown')], 'unknown_time_or_layout', 'Publisher omits time and changes another document body locator.',
        'Only exact untimed material adopted with unknown publication/window; changed raw retained, pending_parse and no invented body; three captures.')
    s.assert_materials(outcome, [untimed])
    assert len(outcome['captures']) == 3 and len(outcome['tables']['pending_items']) == 1
    assert outcome['tables']['pending_items'][0]['reason_code'] == 'pending_parse'
    assert {item['url']: item['disposition'] for item in outcome['tables']['opportunity_inventory']} == {untimed['url']: 'captured', changed['url']: 'pending_parse'}
    assert any(item['field_or_object'].endswith('/window_time') for item in outcome['candidate']['gaps'])


def plugin_add_remove(s):
    alpha, beta = s.article('alpha'), s.article('beta')
    s.listing('alpha', [alpha])
    s.listing('beta', [beta])
    entries = [s.install('alpha'), {'source_id': 'beta', 'entrypoints': [s.base_url + '/beta/list'], 'material_kinds': ['official_release']}]
    before = s.execute(entries, 'plugin_add_remove-before', 'Beta is requested but not installed.',
        'Alpha material and two captures; beta remains unsupported in full source scope.')
    s.assert_materials(before, [alpha])
    s.phase = 'configuration'
    s.install('beta')
    added = s.execute(entries, 'plugin_add_remove-added', 'Add ordinary beta plugin directory without changing core.',
        'Both exact materials and four captures; same two-source scope.')
    s.assert_materials(added, [alpha, beta])
    target = (s.plugins / 'beta').resolve()
    assert target.is_relative_to(s.root) and target.name == 'beta'
    shutil.rmtree(target)
    removed = s.execute(entries, 'plugin_add_remove-removed', 'Remove beta directory completely, while beta remains requested.',
        'Fresh alpha material/two captures; beta scope/extraction/coverage retained unsupported and frozen refs unknown.')
    s.assert_materials(removed, [alpha])
    for outcome in (before, removed):
        assert len(outcome['captures']) == 2
        assert {item['source_id']: item['status'] for item in outcome['tables']['coverage']} == {'alpha': 'partial', 'beta': 'unsupported'}
        binding = next(item for item in outcome['invocation']['profile_bindings'] if item['source_id'] == 'beta')
        assert binding['manifest_ref']['status'] == binding['profile_ref']['status'] == 'unknown'
    assert len(added['captures']) == 4


def all_failed_three_attempts(s):
    entries = []
    for source_id in ('outage_a', 'outage_b'):
        s.listing(source_id, [], status=503)
        entries.append(s.install(source_id))
    outcome = s.execute(entries, 'all_failed_three_attempts', 'Both actual publishers return 503 for every request.',
        'Exactly three failed process attempts/six captured503s; no candidate/receipt/Seal; actual reasons/logs retained; downstream denied and goal unmet.')
    assert outcome['code'] == 1, s.cases[-1]['actual']
    result, invocation = outcome['result'], outcome['invocation']
    assert result['status'] == 'failed' and result['attempt_count'] == 3 and result['allowed_uses'] == []
    assert result['sealed_output_ref']['status'] == 'not_applicable' and invocation['processing_state'] == 'failed'
    summary = s.resolver.resolve(result['failure_summary_ref']['value'], 'investment.sc02_failure_summary')
    assert summary['attempt_count'] == len(summary['attempt_failure_refs']) == 3 and summary['downstream_allowed'] is False
    for number, attempt in enumerate(invocation['attempts'], 1):
        assert attempt['attempt_no'] == number and attempt['processing_state'] == 'failed'
        assert attempt['candidate_ref']['status'] == attempt['receipt_ref']['status'] == 'not_applicable'
        failure = s.resolver.resolve(attempt['failure_ref']['value'], 'investment.sc02_failure')
        assert failure['phase'] == 'collection' and failure['pointer'] == '/documents'
        assert failure['reason'] == 'No supported material and no proven empty window'
        log = s.resolver.resolve(attempt['capture_log_ref']['value'], 'investment.news_capture_log')
        captures = [s.resolver.resolve(ref, 'investment.news_capture') for ref in log['captures']]
        assert len(captures) == 2 and all(item['http_status'] == known(503) for item in captures)
        directory = outcome['folder'] / f'attempt-{number:02}'
        assert not any((directory / filename).exists() for filename in ('candidate.json', 'receipt.json', 'sealed.json', 'validation.stdout.log'))
    assert len([item for item in s.requests if item['phase'] == 'all_failed_three_attempts']) == 6


def fresh_invocation(s):
    original = s.article('fresh', title='Publisher version', text='First actual publisher version.')
    s.listing('fresh', [original])
    entries = [s.install('fresh')]
    first = s.execute(entries, 'fresh_invocation-first', 'First current invocation against HTTP publisher.',
        'First exact bytes/version retained with two actual requests; completed same invocation returns exact result without refetch.')
    s.assert_materials(first, [original])
    command = json.loads((s.root / 'fresh_invocation-first.command.json').read_bytes())
    request_count = len(s.requests)
    repeated = s.command(command, s.root / 'same-invocation')
    assert repeated.returncode == 0 and repeated.stdout == (s.root / 'fresh_invocation-first.stdout.log').read_bytes()
    assert len(s.requests) == request_count
    changed = s.article('fresh', title='Publisher version', text='Second changed actual publisher version.')
    second = s.execute(entries, 'fresh_invocation-second', 'Publisher changes bytes at same URL; new invocation is started.',
        'Refetch exact second bytes; same document identity/new version; both old/new raws retained; two fresh requests.')
    s.assert_materials(second, [changed])
    left, right = first['tables']['documents'][0], second['tables']['documents'][0]
    assert left['document_id'] == right['document_id'] and left['version_id'] != right['version_id']
    assert len(first['captures']) == len(second['captures']) == 2
    assert base64.b64decode(s.resolver.resolve(left['raw_ref'])['response_bytes_base64']) == original['raw']


def isolation(s):
    material = s.article('isolated')
    s.listing('isolated', [material])
    entries = [s.install('isolated')]
    folder, incoming, reference = s.input(entries)
    command = [sys.executable, '-B', str(PLUGIN / 'scripts/run_sc02.py'), '--root', str(s.root), '--network-origin', 'live',
        '--input', str(folder / 'submitted-input.json'), '--run-id', s.run_id, '--invocation-id', incoming['invocation_id']]
    formal = production_root()

    def snapshot():
        return [(str(path), path.stat().st_size, path.stat().st_mtime_ns) for directory in (formal / 'runs/sc02', formal / 'data')
                if directory.exists() for path in sorted(directory.rglob('*'))]

    prior = snapshot()
    rejected = (('omitted-mode', command), ('wrong-mode', command + ['--mode', 'formal']),
        ('formal-test-input', [value if value != str(s.root) else str(formal) for value in command] + ['--mode', 'formal']))
    for label, attempt_command in rejected:
        case = {'case_id': 'isolation-' + label, 'entry': json.dumps(attempt_command), 'trigger': 'Omitted/wrong mode or test input passed to formal mode.',
            'expected': 'Startup rejection before input/lock/directory/DB/network effects; no candidate or validator; formal state unchanged.',
            'actual': 'not yet executed', 'passed': False, 'input_ref': known(reference),
            'candidate_ref': unknown('execution_pending'), 'receipt_ref': unknown('execution_pending')}
        s.write_report(s.root / ('admission-isolation-' + label + '.json'), [case], [])
        s.cases.append(case)
        process = s.command(attempt_command, s.root / ('isolation-' + label))
        s.outcomes.append({'business_succeeded': False, 'output_valid': False, 'goal_satisfied': False})
        case.update(actual='exit=' + str(process.returncode) + '; ' + process.stderr.decode('utf-8', errors='replace')[-2000:],
                    candidate_ref=na('candidate_not_produced'), receipt_ref=na('validation_not_run'))
        assert process.returncode == 2 and not process.stdout
        assert not (folder / 'input.json').exists() and not (folder.parent / 'manifest.json').exists()
        assert not (s.root / 'runs/sc02/.controller.lock').exists() and snapshot() == prior
        assert all(item['phase'] == 'configuration' for item in s.requests)
    outcome = s.execute(entries, 'isolation-valid', 'Valid explicitly isolated test-mode invocation follows rejections.',
        'Exact isolated material/two captures; formal state unchanged; correctly rejected business runs remain false in report flags.')
    s.assert_materials(outcome, [material])
    assert len(outcome['captures']) == 2 and snapshot() == prior


def live_all(s):
    entries = []
    for directory in sorted(SOURCES.iterdir()):
        if directory.is_dir():
            manifest = s.schemas.document('investment.news_manifest', (directory / 'manifest.json').read_bytes())
            entries.append({key: manifest[key] for key in ('source_id', 'entrypoints', 'material_kinds')})
    expected_ids = {'ap', 'bis', 'enisa', 'eu_trade', 'fao', 'fed', 'iea', 'imo', 'nbs', 'ofac', 'pbc', 'sec_edgar', 'usda', 'usgs', 'who', 'wmo', 'xinhua'}
    assert {item['source_id'] for item in entries} == expected_ids
    outcome = s.execute(entries, 'live-all', 'Fresh actual HTTP for all17/all declared entries/kinds; common current SC01/SC02 t and seven-day window.',
        'Retain all actual hops/source scope/opportunities/pending/raw/access-layout errors; independently validated partial may Seal for research; no whole-window/investment readiness claim.')
    assert outcome['code'] == 0, s.cases[-1]['actual']
    assert {item['source_id'] for item in outcome['tables']['extraction_spec']} == expected_ids
    assert all(item['manifest_ref']['status'] == item['profile_ref']['status'] == 'known' for item in outcome['invocation']['profile_bindings'])
    assert all(item['origin'] == 'live' for item in outcome['captures'])
    assert all(item['available_time']['status'] == 'unknown' for item in outcome['tables']['documents'])
    print('Live source results: ' + json.dumps([{'source_id': row['source_id'], 'status': row['status'], 'captures': len(row['capture_ids']),
        'documents': len(row['document_ids']), 'pending': len(row['pending_ids']), 'window_coverage': row['window_coverage'],
        'errors': [item['code'] for item in row['errors']]} for row in outcome['tables']['coverage']]), file=sys.stderr)


def run_case(case_id, *, live=False):
    scenario = Scenario(case_id, live=live)
    error = None
    try:
        scenario._account()
        (live_all if live else globals()[case_id])(scenario)
    except Exception:
        error = traceback.format_exc()
    finally:
        scenario.close()
    return scenario.finish(error), error


def main():
    parser = argparse.ArgumentParser()
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument('--case', choices=CASES)
    select.add_argument('--live-sources', choices=('all',))
    args = parser.parse_args()
    report, error = run_case(args.case or 'live-all', live=args.live_sources == 'all')
    sys.stdout.buffer.write(canonical(report))
    if error:
        print(error, file=sys.stderr)
    return 1 if error else 0


if __name__ == '__main__':
    raise SystemExit(main())
