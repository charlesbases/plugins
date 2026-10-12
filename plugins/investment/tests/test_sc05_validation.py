"""SC05已批准selector的分阶段独立校准；不创建StageInput/Seal或模型响应。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import threading
import unittest
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import urlopen
from types import SimpleNamespace
from jsonschema import Draft202012Validator

from investment.contracts import ContractError, artifact, canonical, instant, now
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.research.references import ResearchResolver
from investment.chains.research_dataset.contracts import (gap_index, mature_for_fit, quantized, verify_conversion,
                                                           verify_features, reinvested_return, period_key, label_dependencies,
                                                           label_availability_upper, view_observation_ids, verify_endpoint_period,
                                                           verify_inherited_availability, verify_scheduled_period)
from investment.chains.research_dataset.validation import Review
from investment.chains.research_dataset.source_validation import capture_bytes, located_text, semantic_paths, source_references


ADMISSION = {
    'partial_contract_source': {'entry': 'SC05真实取材与独立验收；本次只校准HTTP原文边界',
        'legal_precondition': '完整流程需本轮SC01/03/04真实seal；未取得，故本次不运行流程',
        'trigger': 'NAV源成功，独立费率源HTTP503', 'source': '实际本机HTTP模拟发布者，synthetic_fixture',
        'expected': 'NAV原值可定位；费率请求确实失败，不推零费率；原U/风险守恒完整流程待上游'},
    'pit_revision': {'entry': 'SC05历史特征独立方法verify_features',
        'legal_precondition': '冻结同指标旧版与后修订、固定源实体与单位；完整流程需SC04历史seal',
        'trigger': 'origin在两个真实源声明的发布界之间', 'source': '同一模拟发布者两个有明确发布时间的版本',
        'expected': '旧版可用；后修订在该origin拒绝，源字节不能因旧经济日期回填'},
    'label_maturity': {'entry': 'SC05标签成熟谓词mature_for_fit',
        'legal_precondition': '端点源公布时间晚于fit；当前C可更晚，完整流程需真实上游',
        'trigger': '目标结束早于fit但端点发布晚于fit', 'source': '模拟发布者明确端点公布时间',
        'expected': '结束日不授训练资格；公布前False，公布时True'},
    'validation_access': {'entry': '正式ResearchResolver.read/resolve',
        'legal_precondition': '本批实际合法训练/独立表及原始Capture，准确引用，只授过滤表',
        'trigger': '生产角色请求holdout或从训练行引用raw', 'source': '本批真实落盘的模拟发布者数据',
        'expected': 'training可读；holdout与raw的read/resolve均RS06拒绝；另授SC08读取holdout'},
}

SCHEDULE_ADMISSION = {
    'entry': 'D10日历预计期间→D11交易日端点→D18正式字段与原文引用边界',
    'legal_precondition': '完整业务仍需本轮真实SC03/04封存；本次只运行已批准真实模拟发布者源级/数学/引用接口',
    'trigger': '未来计划在origin前公布，非交易日仍估值；来源不全或计划与后来观察口径不同',
    'source': '明确synthetic_fixture的实际HTTP发布者；官方合同只支持非开放日可能估值这一可达形态，不证明模拟日期/时刻',
    'expected': '完整源时区日期上界只索引经济期间，未来期间不当未来NAV；迟发安排不回填origin，unknown保留Gap，实测端点原规则保留',
}

REVISION_ADMISSION = {
    'inherited_pit_evidence': {'entry': 'D07/D18继承Availability确定性回链',
        'legal_precondition': '正式业务需本轮真实SC04封存；基础检查只用有类型原始时间证据，不造Seal或receipt',
        'trigger': 'History/Identity/Exposure来自已验上游，未经过SC05 acquisition task',
        'source': '实际模拟发布者原文及准确原对象引用',
        'expected': '原值/保守界/用途一致可继承，无须伪造SC05 task；缺PIT用途只保留Gap与非PIT用途'},
    'late_reinvestment_nav': {'entry': 'D11实际依赖重建→D12成熟界→D14物理版本集合',
        'legal_precondition': '合法现金分红、合同及单位NAV，完整业务需真实上游；本次只校准正式方法',
        'trigger': '再投已经发生，但该价格的公布界晚于端点价格及fit cutoff',
        'source': '模拟发布者明确再投价格经济时刻与稍后发布时间',
        'expected': '实际闭包强制包含再投NAV；漏声明拒绝，成熟界取较晚公布，物理视图不得漏行'},
    'wrong_reinvestment_basis': {'entry': 'D11现金除真实再投单位NAV及D18关系语境',
        'legal_precondition': '同基金同时公布单位NAV和累计NAV，实际合同指定单位NAV',
        'trigger': '再投ID误指同基金累计NAV、错指标/单位币种或别日NAV',
        'source': '有明确measurement_basis和经济期间的模拟发布者',
        'expected': '仅单位NAV/正确metric/量纲币种/完整再投经济期间及合同依据匹配才算份额'},
    'missing_requested_endpoint': {'entry': 'D11起止经济期间与已冻结exact horizon',
        'legal_precondition': '请求期限终点行情，但发布者仅返回较旧NAV；不造非法已验上游',
        'trigger': '候选仍填正确outcome_end_at而错误复用旧观察作为end_version',
        'source': '模拟发布者明确旧日期与实际源精度',
        'expected': '所有horizon分支核完整期间/精度边界；旧NAV不能冒充未来终值，保留缺证'},
}


def known(value, local=False):
    return {'status': 'known', 'value': value, 'reason': None if local else ''}


def unavailable(reason='Not supplied by the publisher'):
    return {'status': 'unknown', 'value': None, 'reason': reason}


def na(reason='Not applicable'):
    return {'status': 'not_applicable', 'value': None, 'reason': reason}


class Publisher(BaseHTTPRequestHandler):
    records = {
        '/schedule': {'calendar_id': 'calendar-A', 'entity_id': 'fund-A', 'date': '2020-01-06',
                      'kind': 'estimated_valuation_economic_period', 'published_at': '2020-01-01T00:00:00Z',
                      'period': {'lower_bound': '2020-01-06T00:00:00Z', 'upper_bound': '2020-01-06T00:00:00Z',
                                 'precision': 'instant', 'timezone': 'UTC'},
                      'days': [{'date': '2020-01-02', 'is_trading_day': True}, {'date': '2020-01-03', 'is_trading_day': True},
                               {'date': '2020-01-04', 'is_trading_day': False}, {'date': '2020-01-05', 'is_trading_day': False},
                               {'date': '2020-01-06', 'is_trading_day': True}]},
        '/end-monday': {'entity': 'fund-A', 'metric': 'nav', 'value': '0.6', 'unit': 'CNY/share',
                        'economic_at': '2020-01-06T00:00:00Z', 'published_at': '2020-01-07T00:00:00Z'},
        '/schedule-day': {'calendar_id': 'calendar-A', 'entity_id': 'fund-A', 'date': '2020-01-04',
                          'kind': 'estimated_valuation_economic_period', 'published_at': '2020-01-01T00:00:00Z',
                          'period': {'lower_bound': '2020-01-03T16:00:00Z', 'upper_bound': '2020-01-04T15:59:59.999999Z',
                                     'precision': 'date', 'timezone': 'Asia/Shanghai'}},
        '/old': {'entity': 'fund-A', 'metric': 'nav', 'value': '1.08', 'unit': 'CNY/share',
                 'economic_date': '2020-01-01', 'published_at': '2020-01-02T00:00:00Z'},
        '/revision': {'entity': 'fund-A', 'metric': 'nav', 'value': '1.09', 'unit': 'CNY/share',
                      'economic_date': '2020-01-01', 'published_at': '2020-01-05T00:00:00Z'},
        '/start': {'entity': 'fund-A', 'metric': 'nav', 'value': '1', 'unit': 'CNY/share',
                   'economic_at': '2020-01-01T00:00:00Z', 'published_at': '2020-01-01T00:00:00Z'},
        '/end': {'entity': 'fund-A', 'metric': 'nav', 'value': '0.6', 'unit': 'CNY/share',
                 'economic_at': '2020-01-05T00:00:00Z', 'published_at': '2020-01-06T00:00:00Z'},
        '/reinvest': {'entity': 'fund-A', 'metric': 'nav', 'value': '0.5', 'unit': 'CNY/share',
                      'economic_at': '2020-01-04T00:00:00Z', 'published_at': '2020-01-07T00:00:00Z'},
        '/cumulative': {'entity': 'fund-A', 'metric': 'nav', 'value': '1.3', 'unit': 'CNY/share',
                        'measurement_basis': 'cumulative_nav', 'economic_at': '2020-01-04T00:00:00Z', 'published_at': '2020-01-07T00:00:00Z'},
        '/contract': {'entity': 'fund-A', 'dividend_election': 'Reinvest cash dividends at the unit NAV for the recorded reinvestment date'},
        '/dividend': {'entity': 'fund-A', 'cash_per_share': '0.1', 'reinvestment_at': '2020-01-04T00:00:00Z', 'nav_version': 'reinvest'},
        '/split': {'entity': 'fund-A', 'share_multiplier': '2', 'ex_at': '2020-01-02T00:00:00Z'},
    }

    def do_GET(self):
        status = 200 if self.path in self.records else 503
        body = json.dumps(self.records.get(self.path, {'error': 'Fee publication temporarily unavailable'})).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json;charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class SC05Calibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        requested_root = Path(os.environ.get('SC05_TEST_ROOT', str(Path.home() / '.investment/test/sc05-validator-20261020')))
        cls.root = guard_namespace(requested_root, 'test')
        cls.directory = cls.root / 'runs/sc05/20261020/source-calibration/attempt-01'
        cls.directory.mkdir(parents=True, exist_ok=True)
        (cls.root / 'admission.json').write_bytes(canonical({'selectors': ADMISSION, 'revision_checks': REVISION_ADMISSION, 'scheduled_period_checks': SCHEDULE_ADMISSION,
                                                          'complete_selectors_executed': False}))
        cls.schemas = ResearchSchemas(('SC03', 'SC04', 'SC05'))
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Publisher)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.captures = {}
        for endpoint in [*[path[1:] for path in Publisher.records], 'fees']:
            try:
                response = urlopen(cls.base + '/' + endpoint, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read()
                value = {'capture_id': endpoint, 'task_id': 'task-' + endpoint, 'requested_url': cls.base + '/' + endpoint,
                         'final_url': response.url, 'retrieved_at': now(), 'http_status': response.status,
                         'content_type': response.headers['Content-Type'], 'body_base64': base64.b64encode(raw).decode(),
                         'body_sha256': hashlib.sha256(raw).hexdigest(), 'body_byte_size': len(raw),
                         'source_type': 'synthetic_fixture', 'parser_id': 'json-pointer', 'parser_version': 'v1'}
                cls.schemas.validate('investment.sc05.capture', value)
                path = cls.directory / (endpoint + '-capture.json')
                path.write_bytes(canonical(value))
                ref = artifact(canonical(value), 'investment.sc05.capture', 'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
                cls.captures[endpoint] = value, ref

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def evidence(self, at, ref):
        return known({'lower_bound': at, 'upper_bound': at, 'precision': 'instant', 'timezone': 'UTC', 'basis_ref': ref})

    def observation(self, endpoint):
        capture, ref = self.captures[endpoint]
        source = Publisher.records['/' + endpoint]
        observation = {'version_id': endpoint, 'entity_id': 'fund-A', 'metric_id': 'nav',
                       'observation_period': self.evidence(source.get('economic_at', '2020-01-01T00:00:00Z'), ref),
                       'value': known(source['value']), 'unit': known(source['unit'], True), 'currency': known('CNY', True),
                       'raw_value': known(source['value']), 'raw_unit': known(source['unit'], True), 'raw_currency': known('CNY', True),
                       'conversion': na(), 'published_time': self.evidence(source['published_at'], ref),
                       'available_time': self.evidence(source['published_at'], ref), 'availability_basis': 'publisher_timestamp',
                       'availability_bound_at': known(source['published_at']), 'retrieved_at': capture['retrieved_at'],
                       'raw_ref': ref, 'supersedes_version_id': known('old') if endpoint == 'revision' else na(),
                       'measurement_basis': source.get('measurement_basis', 'unit_nav'),
                       'citations': [{'raw_ref': ref, 'locator': 'json:/value', 'quote': source['value'], 'claim': 'Published NAV for fund-A'}]}
        self.schemas.validate('investment.sc05.observation_row', observation)
        return observation

    def financial_example(self):
        observations = {key: self.observation(key) for key in ('start', 'end', 'reinvest')}
        contract_ref, dividend_ref, split_ref = (self.captures[key][1] for key in ('contract', 'dividend', 'split'))
        term_quote = {'raw_ref': contract_ref, 'locator': 'json:/dividend_election',
                      'quote': Publisher.records['/contract']['dividend_election'], 'claim': 'Actual reinvestment election and unit NAV basis'}
        contract = {'contract_version_id': 'contract', 'entity_id': 'fund-A', 'share_class_id': 'A', 'platform': 'publisher',
                    'effective_from': self.evidence('2019-12-31T00:00:00Z', contract_ref), 'effective_to': na(),
                    'published_time': self.evidence('2019-12-31T00:00:00Z', contract_ref),
                    'available_time': self.evidence('2019-12-31T00:00:00Z', contract_ref), 'fees': [],
                    'terms': [{'term_id': 'dividend-election', 'term': 'dividend_election', 'value': na(),
                               'text_value': known(Publisher.records['/contract']['dividend_election'], True), 'unit': na(),
                               'currency': na(), 'day_count': 'not_applicable', 'citations': [term_quote]}],
                    'raw_refs': [contract_ref], 'completeness': 'unknown', 'missing_fields': ['execution_terms']}
        split = {'event_id': 'split', 'entity_id': 'fund-A', 'event_type': 'split',
                 'record_at': self.evidence('2020-01-01T00:00:00Z', split_ref), 'ex_at': self.evidence('2020-01-02T00:00:00Z', split_ref),
                 'payable_at': na(), 'available_time': self.evidence('2020-01-01T00:00:00Z', split_ref), 'cash_per_share': na(),
                 'currency': na(), 'share_multiplier': known('2'), 'reinvestment_nav_version_id': na(), 'reinvestment_at': na(),
                 'contract_version_id': 'contract', 'cash_unit': 'currency_per_share',
                 'citations': [{'raw_ref': split_ref, 'locator': 'json:/share_multiplier', 'quote': '2', 'claim': 'Two shares replace each old share'}]}
        dividend = {'event_id': 'dividend', 'entity_id': 'fund-A', 'event_type': 'cash_dividend',
                    'record_at': self.evidence('2020-01-02T00:00:00Z', dividend_ref), 'ex_at': self.evidence('2020-01-03T00:00:00Z', dividend_ref),
                    'payable_at': self.evidence('2020-01-04T00:00:00Z', dividend_ref),
                    'available_time': self.evidence('2020-01-03T00:00:00Z', dividend_ref), 'cash_per_share': known('0.1'),
                    'currency': known('CNY', True), 'share_multiplier': na(), 'contract_version_id': 'contract',
                    'reinvestment_at': self.evidence('2020-01-04T00:00:00Z', dividend_ref), 'reinvestment_nav_version_id': known('reinvest'),
                    'cash_unit': 'currency_per_share',
                    'citations': [{'raw_ref': dividend_ref, 'locator': 'json:/cash_per_share', 'quote': '0.1', 'claim': 'Cash dividend per entitled share'}]}
        label = {'label_id': 'label', 'sample_id': 'sample', 'entity_id': 'fund-A', 'target_id': 'total-return', 'horizon_id': 'four-days',
                 'origin_at': '2020-01-01T00:00:00Z', 'outcome_end_at': '2020-01-05T00:00:00Z',
                 'label_available_at': known('2020-01-07T00:00:00Z'), 'label_status': 'mature', 'outcome': known('0.44'),
                 'unit': 'ratio', 'currency': known('CNY', True), 'start_version_id': known('start'), 'end_version_id': known('end'),
                 'equity_event_ids': ['split', 'dividend'], 'contract_version_ids': ['contract'], 'source_version_ids': ['start', 'end', 'reinvest'],
                 'calculation_basis': 'unit_nav_with_documented_equity_events', 'gap_ids': []}
        self.schemas.validate('investment.sc05.contract_vintage_row', contract)
        self.schemas.validate('investment.sc05.label_row', label)
        for event in (split, dividend):
            self.schemas.validate('investment.sc05.equity_event_row', event)
        return label, observations, [split, dividend], {'contract': contract}

    def test_partial_contract_source_source_level(self):
        nav, _ = self.captures['old']
        fee, _ = self.captures['fees']
        self.assertEqual(nav['http_status'], 200)
        self.assertEqual(fee['http_status'], 503)
        observation = self.observation('old')
        self.assertEqual(located_text(observation['citations'][0], nav, self.directory), '1.08')
        self.assertEqual(json.loads(capture_bytes(fee)), {'error': 'Fee publication temporarily unavailable'})
        with self.assertRaises(ContractError):
            located_text({'locator': 'json:/rate', 'quote': '0'}, fee, self.directory)

    def test_pit_revision_math_source_level(self):
        old, revision = self.observation('old'), self.observation('revision')
        self.assertEqual(period_key(old['observation_period']), period_key(revision['observation_period']))
        self.assertNotEqual(old['observation_period']['value']['basis_ref'], revision['observation_period']['value']['basis_ref'])
        reference = self.captures['old'][1]
        field = {'field_name': 'nav', 'source_locator': 'json:/value', 'meaning': 'Unit NAV in CNY per share',
                 'expected_unit': known('CNY/share', True), 'currency': known('CNY', True), 'time_role': 'not_temporal', 'required_for': ['pit_training']}
        rule = {'source_rule_id': 'nav-source', 'capability': 'nav', 'provider_id': 'publisher', 'source_kind': 'fund_manager',
                'entry_url': self.base + '/old', 'allowed_domains': ['127.0.0.1'], 'request_method': 'GET',
                'parser_id': 'json-pointer', 'parser_version': 'v1', 'field_rules': [field], 'identity_fields': ['entity'],
                'authority_basis': [{'raw_ref': reference, 'locator': 'json:/entity', 'quote': 'fund-A', 'claim': 'Publisher declares its instrument'}],
                'historical_evidence_policy': 'actual_vintage_or_unknown', 'no_data_policy': 'retain_unknown'}
        self.schemas.validate('investment.sc05.source_rule', rule)
        ref = artifact(canonical(rule), 'investment.sc05.source_rule', 'investment-run://sc05/20261020/source-calibration/attempt-01/rule.json')
        definition = {'feature_id': 'nav', 'field_name': 'nav', 'source_rule_ref': ref, 'transformation': 'identity',
                      'expected_unit': known('CNY/share', True), 'currency': known('CNY', True),
                      'applicability': {'kind': 'all_origin_forecast_entities'}, 'source_entity_binding': {'kind': 'panel_entity'}}
        feature = {'feature_id': 'nav', 'value': old['value'], 'unit': old['unit'], 'currency': old['currency'],
                   'source_version_ids': ['old'], 'availability_bound_at': old['availability_bound_at'], 'transformation': 'identity',
                   'transformation_spec_ref': ref, 'gap_ids': []}
        panel = {'entity_id': 'fund-A', 'origin_at': '2020-01-03T00:00:00Z', 'feature_values': [feature], 'eligible_for_training': True}
        protocol = {'model_search_space': {'predictors': [definition]}}
        verify_features(panel, protocol, {'old': old, 'revision': revision}, {ref['sha256']: rule})
        feature.update(value=revision['value'], source_version_ids=['revision'], availability_bound_at=revision['availability_bound_at'])
        with self.assertRaises(ContractError) as failure:
            verify_features(panel, protocol, {'old': old, 'revision': revision}, {ref['sha256']: rule})
        self.assertEqual(failure.exception.rule, 'D09')

    def test_label_maturity_math_level(self):
        label = {'label_status': 'mature', 'outcome': known('0.08'), 'outcome_end_at': '2020-01-01T00:00:00Z',
                 'label_available_at': known('2020-01-05T00:00:00Z')}
        self.assertFalse(mature_for_fit(label, '2020-01-03T00:00:00Z'))
        self.assertTrue(mature_for_fit(label, '2020-01-05T00:00:00Z'))
        self.assertEqual(quantized('0.125', 2), quantized('0.12', 2))
        self.assertEqual(quantized('0.135', 2), quantized('0.14', 2))

    def test_validation_access_interface_level(self):
        unscoped = ResearchResolver(self.root, self.schemas, mode='test')
        refs = {}
        for partition, endpoint in (('training', 'old'), ('independent', 'revision')):
            value = {'schema_version': 1, 'rows': [self.observation(endpoint)]}
            path = self.directory / (partition + '.json')
            path.write_bytes(canonical(value))
            refs[partition] = unscoped.dataset_ref(value, 'investment.sc05.observation_vintages',
                'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
        for stage in ('SC06', 'SC07'):
            resolver = ResearchResolver(self.root, self.schemas, mode='test', allowed_refs=[refs['training']])
            self.assertEqual(resolver.resolve(refs['training'])['rows'][0]['version_id'], 'old', stage)
            for prohibited in (refs['independent'], self.captures['old'][1]):
                for operation in (lambda: resolver.read(prohibited['uri']), lambda: resolver.resolve(prohibited)):
                    with self.assertRaises(ContractError) as failure:
                        operation()
                    self.assertEqual(failure.exception.code, 'use_not_allowed')
        independent = ResearchResolver(self.root, self.schemas, mode='test', allowed_refs=[refs['independent']])
        self.assertEqual(independent.resolve(refs['independent'])['rows'][0]['version_id'], 'revision')

    def test_complete_public_gap_identity(self):
        gap = {'rule_id': 'D10', 'field_or_object': 'fund-A/fee', 'reason': 'HTTP503', 'owner': 'source',
               'required_evidence': ['published_fee_schedule'], 'affected_uses': ['selection_comparison']}
        index = gap_index([gap, dict(gap)])
        self.assertEqual(list(index), ['g-' + hashlib.sha256(canonical(gap)).hexdigest()])
        changed = dict(gap, reason='Fee publication pending')
        self.assertEqual(len(gap_index([gap, changed])), 2)
        with self.assertRaises(ContractError):
            gap_index([dict(gap, gap_id='invented')])

    def test_identity_unit_cannot_be_relabelled(self):
        observation = self.observation('old')
        verify_conversion(observation)
        observation['unit'] = known('ratio', True)
        with self.assertRaises(ContractError):
            verify_conversion(observation)

    def test_split_dividend_reinvestment_independent_wealth(self):
        label, observations, events, contracts = self.financial_example()
        # 一份额先拆为2，分红0.2再投成0.4份，终值2.4*0.6=1.44。
        result = reinvested_return(label, observations, events, contracts)
        self.assertEqual(result, Fraction(11, 25))

    def test_late_reinvestment_dependency_maturity_and_view(self):
        label, observations, events, contracts = self.financial_example()
        label['source_version_ids'] = ['start', 'end']
        self.assertEqual(label_dependencies(label, observations, events, contracts)['observations'], {'start', 'end', 'reinvest'})
        with self.assertRaises(ContractError) as failure:
            label_dependencies(label, observations, events, contracts, require_declared=True)
        self.assertEqual(failure.exception.rule, 'D11')
        self.assertEqual(label_availability_upper(label, observations, events, contracts), instant('2020-01-07T00:00:00Z'))
        self.assertFalse(mature_for_fit(label, '2020-01-06T00:00:00Z'))
        self.assertTrue(mature_for_fit(label, '2020-01-07T00:00:00Z'))
        self.assertEqual(view_observation_ids([], [label], observations, events, contracts), {'start', 'end', 'reinvest'})

    def calendar_row(self, endpoint='schedule-day'):
        capture, ref = self.captures[endpoint]
        source = json.loads(capture_bytes(capture))
        return {'calendar_id': source['calendar_id'], 'entity_id': source['entity_id'], 'date': source['date'],
                'calendar_version_id': endpoint, 'is_trading_day': known(False), 'order_cutoff_at': unavailable(),
                'confirmation_day': unavailable(), 'settlement_day': unavailable(),
                'scheduled_valuation_period': known({**source['period'], 'basis_ref': ref}),
                'available_time': self.evidence(source['published_at'], ref),
                'citations': [{'raw_ref': ref, 'locator': 'json:/kind', 'quote': source['kind'],
                               'claim': 'Publisher explicitly identifies an estimated valuation economic period'}]}

    def test_scheduled_period_source_contract_and_timezone(self):
        row = self.calendar_row()
        self.schemas.validate('investment.sc05.calendar_row', row)
        lower, upper, precision, timezone = verify_scheduled_period(row)
        self.assertEqual((lower, upper), (instant('2020-01-03T16:00:00Z'), instant('2020-01-04T15:59:59.999999Z')))
        self.assertEqual((precision, timezone), ('date', 'Asia/Shanghai'))
        self.assertGreater(lower, instant('2020-01-02T00:00:00Z'))
        self.assertIn('/value/scheduled_valuation_period', semantic_paths(row))
        self.assertIn(self.captures['schedule-day'][1], source_references(row))
        resolver = ResearchResolver(self.root, self.schemas, mode='test')
        table = {'schema_version': 1, 'rows': [row]}
        path = self.directory / 'calendar-check.json'
        path.write_bytes(canonical(table))
        reference = resolver.dataset_ref(table, 'investment.sc05.calendar',
            'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
        method = json.loads((Path(__file__).parents[1] / 'docs/spec/v1/method-sc05.json').read_text(encoding='utf-8'))
        scoped = ResearchResolver(self.root, self.schemas, mode='test', allowed_refs=[reference],
                                  reference_grants=method['runtime']['reference_grants'])
        scoped.resolve(reference)
        self.assertEqual(scoped.resolve(row['scheduled_valuation_period']['value']['basis_ref']), self.captures['schedule-day'][0])
        with self.assertRaises(ContractError) as denied:
            scoped.resolve(self.captures['end'][1])
        self.assertEqual(denied.exception.rule, 'RS06')
        review = Review(SimpleNamespace(resolver=scoped, input={'information_cutoff_at': '2020-01-02T00:00:00Z'}, attempt_dir=self.directory))
        review.rows, review.gaps = {'calendar': [row]}, []
        review.calendar_schedules()
        row['available_time'] = self.evidence('2020-01-03T00:00:00Z', self.captures['schedule-day'][1])
        with self.assertRaises(ContractError) as failure:
            review.calendar_schedules()
        self.assertEqual(failure.exception.code, 'missing_evidence')
        row['available_time'] = self.evidence('2020-01-01T00:00:00Z', self.captures['schedule-day'][1])
        row['scheduled_valuation_period']['value']['basis_ref'] = self.captures['end'][1]
        with self.assertRaises(ContractError) as unsupported:
            review.calendar_schedules()
        self.assertEqual(unsupported.exception.pointer, '/scheduled_valuation_period')
        for patch in ({'timezone': 'UTC'}, {'lower_bound': '2020-01-03T17:00:00Z'}, {'precision': 'month'}):
            bad = self.calendar_row()
            bad['scheduled_valuation_period']['value'].update(patch)
            with self.assertRaises(ContractError):
                verify_scheduled_period(bad)
        row['scheduled_valuation_period'] = unavailable('Schedule interpretation remains unverified')
        review.gaps = [{'field_or_object': 'schedule-day/scheduled_valuation_period'}]
        review.calendar_schedules()
        self.assertIsNone(verify_scheduled_period(row))

    def test_origin_schedule_matches_observed_endpoint_period(self):
        label, observations, events, contracts = self.financial_example()
        observations['end-monday'] = self.observation('end-monday')
        label.update(outcome_end_at='2020-01-06T00:00:00Z', end_version_id=known('end-monday'),
                     source_version_ids=['start', 'end-monday', 'reinvest'])
        self.schemas.validate('investment.sc05.label_row', label)
        review = Review(None)
        review.protocol = {'target_definition': {'target_id': 'total-return', 'numerical_precision': {'decimal_places': 2}},
                          'horizons': [{'horizon_id': 'four-days', 'unit': 'calendar_day', 'length': 4,
                                        'end_rule': 'next_trading_boundary', 'calendar_id': 'calendar-A'}]}
        calendar = []
        source = json.loads(capture_bytes(self.captures['schedule'][0]))
        for day in source['days']:
            row = self.calendar_row('schedule')
            row.update(date=day['date'], is_trading_day=known(day['is_trading_day']))
            if day['date'] != source['date']:
                row['scheduled_valuation_period'] = unavailable()
            self.schemas.validate('investment.sc05.calendar_row', row)
            calendar.append(row)
        review.rows = {'labels': [label], 'events': events, 'calendar': calendar}
        review.indexes = {'observations': observations, 'contracts': contracts,
                          'panels': {'sample': {'entity_id': 'fund-A', 'origin_at': label['origin_at']}}}
        review.label_values()
        calendar[-1]['scheduled_valuation_period']['value']['lower_bound'] = '2020-01-06T01:00:00Z'
        calendar[-1]['scheduled_valuation_period']['value']['upper_bound'] = '2020-01-06T01:00:00Z'
        with self.assertRaises(ContractError) as failure:
            review.label_values()
        self.assertEqual(failure.exception.code, 'missing_evidence')
        calendar[-1]['scheduled_valuation_period'] = unavailable()
        review.label_values()

    def test_old_endpoint_cannot_value_exact_horizon(self):
        label, observations, events, contracts = self.financial_example()
        review = Review(None)
        review.protocol = {'target_definition': {'target_id': 'total-return', 'numerical_precision': {'decimal_places': 2}},
                           'horizons': [{'horizon_id': 'four-days', 'unit': 'calendar_day', 'length': 4, 'end_rule': 'exact_calendar_boundary'}]}
        review.rows = {'labels': [label], 'events': events}
        review.indexes = {'observations': observations, 'contracts': contracts,
                          'panels': {'sample': {'entity_id': 'fund-A', 'origin_at': label['origin_at']}}}
        review.label_values()
        label['end_version_id'] = known('start')
        with self.assertRaises(ContractError) as failure:
            review.label_values()
        self.assertEqual(failure.exception.code, 'missing_evidence')
        day = copy.deepcopy(observations['end'])
        period = day['observation_period']['value']
        period.update(lower_bound='2020-01-05T00:00:00Z', upper_bound='2020-01-05T23:59:59.999999Z', precision='date')
        verify_endpoint_period(day, '2020-01-05T23:59:59.999999Z')
        with self.assertRaises(ContractError):
            verify_endpoint_period(day, '2020-01-05T12:00:00Z')

    def test_reinvestment_price_basis_units_and_economic_time(self):
        label, observations, events, contracts = self.financial_example()
        wrong = self.observation('cumulative')
        observations['cumulative'] = wrong
        events[1]['reinvestment_nav_version_id'] = known('cumulative')
        with self.assertRaises(ContractError):
            reinvested_return(label, observations, events, contracts)
        events[1]['reinvestment_nav_version_id'] = known('reinvest')
        for field, value in [('metric_id', 'another-nav'), ('unit', known('ratio', True)), ('currency', known('USD', True)),
                             ('observation_period', observations['start']['observation_period'])]:
            changed = copy.deepcopy(observations)
            changed['reinvest'][field] = value
            with self.subTest(field=field), self.assertRaises(ContractError):
                reinvested_return(label, changed, events, contracts)

    def test_event_review_exact_nav_contract_context(self):
        label, observations, events, contracts = self.financial_example()
        resolver = ResearchResolver(self.root, self.schemas, mode='test')
        review = Review(SimpleNamespace(resolver=resolver))
        review.rows = {'observations': list(observations.values()), 'contracts': list(contracts.values())}
        review.bundle = {}
        for name, schema, field in [('observations', 'observation_vintages', 'observation_vintages_ref'),
                                    ('contracts', 'contract_vintages', 'contract_vintages_ref')]:
            value = {'schema_version': 1, 'rows': review.rows[name]}
            path = self.directory / ('event-context-' + name + '.json')
            path.write_bytes(canonical(value))
            review.bundle[field] = resolver.dataset_ref(value, 'investment.sc05.' + schema,
                'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
        related, sources = review.event_dependencies(events[1])
        self.assertEqual([row['value'] for row in related], [contracts['contract'], observations['reinvest']])
        self.assertIn(self.captures['reinvest'][1], sources)
        self.assertIn(self.captures['contract'][1], sources)

    def test_inherited_pit_availability_copy_and_use_gate(self):
        original = self.observation('old')
        source = {'available_time': original['available_time']}
        row = {'available_time': original['available_time'], 'availability_bound_at': original['availability_bound_at'],
               'origin_at': '2020-01-03T00:00:00Z', 'eligible': True, 'gap_ids': []}
        verify_inherited_availability(row, source, original['availability_bound_at'], True)
        row.update(eligible=False, gap_ids=['g-' + '1' * 64])
        verify_inherited_availability(row, source, original['availability_bound_at'], False)
        row['eligible'] = True
        with self.assertRaises(ContractError):
            verify_inherited_availability(row, source, original['availability_bound_at'], False)
        row['availability_bound_at'] = known('2020-01-01T00:00:00Z')
        with self.assertRaises(ContractError):
            verify_inherited_availability(row, source, original['availability_bound_at'], True)


class InheritedHistoryCalibration(unittest.TestCase):
    """仅核继承字段的格式投影，不创建上游执行或封存。"""

    def test_preserves_sc04_history_wrapper(self):
        requested = Path(os.environ.get('SC05_TEST_ROOT', str(Path.home() / '.investment/test/sc05-producer-20261020')))
        root = guard_namespace(requested / 'history-wrapper-format' / ('run-' + uuid4().hex), 'test')
        directory = root / 'runs/sc04/20261020/history-format/attempt-01'
        directory.mkdir(parents=True, exist_ok=True)
        schemas = ResearchSchemas(('SC03', 'SC04', 'SC05'))
        resolver = ResearchResolver(root, schemas, mode='test')
        value = {'rows': []}
        path = directory / 'history.json'
        path.write_bytes(canonical(value))
        reference = resolver.dataset_ref(value, 'investment.sc04.history',
                                         'investment-run://sc04/20261020/history-format/attempt-01/history.json')
        resolver.allow(reference)
        self.assertEqual(resolver.resolve(reference, 'investment.sc04.history'), value)
        wrapper = known(reference)
        original = canonical(wrapper)
        source_type = 'urn:investment:spec:v1:sc04#/$defs/OptionalDataset'
        field = schemas.documents['urn:investment:spec:v1:sc05']['$defs']['DatasetBundle']['properties']['universe_history_ref']
        self.assertEqual(field, {'$ref': source_type})
        upstream = Draft202012Validator({'$ref': source_type}, registry=schemas.resources, format_checker=schemas.format_checker)
        projection = Draft202012Validator(field, registry=schemas.resources, format_checker=schemas.format_checker)
        self.assertTrue(upstream.is_valid(wrapper))
        self.assertTrue(projection.is_valid(wrapper))
        self.assertEqual(canonical(wrapper), original)
        self.assertEqual(wrapper['reason'], '')
        changed = dict(wrapper, reason=None)
        self.assertFalse(upstream.is_valid(changed))
        self.assertFalse(projection.is_valid(changed))
        local = schemas.documents['urn:investment:spec:v1:sc05']['$defs']['OptionalDataset']
        self.assertEqual(local['oneOf'][0]['properties']['reason'], {'type': 'null'})
        (root / 'original-wrapper.json').write_bytes(original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
