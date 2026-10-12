"""SC05 producer源级/数学独立校准；完整selectors需本轮真实上游和正式Host。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import unittest
from uuid import uuid4
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from investment.contracts import ContractError, artifact, canonical, known, na, unknown
from investment.research.contracts import ResearchSchemas, guard_namespace
from investment.chains.research_dataset.producer_sources import PARSER_ID, PARSER_VERSION, PROVIDER_ID, discovered_links, retrieve, private_known
from investment.chains.research_dataset.producer_math import normalize_observations, quantized_decimal, reinvested_wealth, select_version
from investment.chains.research_dataset.producer_samples import build_labels
from investment.chains.research_dataset.producer_views import assignments

ADMISSION = {
    'partial_contract_source': {
        'entry': 'run_sc05 with real same-batch SC01/SC03/SC04 sealed inputs',
        'precondition': 'Original U/risk/source rules and protocol are frozen; no forged upstream receipt',
        'trigger': 'NAV source succeeds while the separate fee source actually returns HTTP503',
        'source': 'synthetic_fixture HTTP publisher, marked in every Capture/Attempt',
        'expected': 'Original U/risk retained; observed NAV preserved; fee unknown with Gap and affected uses; full process and independent validator must run',
    },
    'pit_revision': {
        'entry': 'run_sc05 with actual historical origins and sealed universe history',
        'precondition': 'Original observation and its later real release/revision are both available with exact source evidence',
        'trigger': 'The later vintage is unavailable at the earlier origin',
        'source': 'synthetic_fixture explicitly released vintage records',
        'expected': 'Earlier origin uses the original value; later origin may use the evidenced revision; no backfill',
    },
    'label_maturity': {
        'entry': 'run_sc05 followed by validate_sc05 and ResearchController',
        'precondition': 'Frozen fold and target plus actual endpoint/equity/contract evidence',
        'trigger': 'Outcome period ends before fit cutoff but necessary publication is later',
        'source': 'synthetic_fixture with separate economic and release periods',
        'expected': 'Unpublished label has no training outcome and is excluded at the fit cutoff',
    },
    'validation_access': {
        'entry': 'SC06/SC07 scoped ResearchResolver over actual SC05 sealed DataViews',
        'precondition': 'Separate physical files and actual role grants; real same-batch seal',
        'trigger': 'Producer follows full-table/raw/quote/holdout ancestor reference',
        'source': 'actual materialized and independently validated SC05 dataset',
        'expected': 'Allowed training/selection/current reads succeed; every independent-validation route is denied',
    },
}


class _Publisher(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls.append(self.path)
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/nav')
            self.end_headers()
            self.wfile.write(b'original redirect body')
            return
        if self.path == '/macro':
            body = canonical({
                'source_type': 'synthetic_fixture', 'entity_id': 'fixture-economy',
                'metric_id': 'unemployment-rate', 'unit': 'percent',
                'observation_at': '2020-01-01T00:00:00Z', 'published_at': '2020-01-02T00:00:00Z',
                'raw_currency_basis': 'A labour-force percentage is not a money amount.',
                'observations': [
                    {'id': 'macro-a', 'value': '5.1', 'currency_basis': 'The reported rate has no monetary denomination.'},
                    {'id': 'macro-b', 'value': '5.1', 'currency_basis': 'This percentage is a non-currency economic quantity.'},
                ],
            })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/wealth':
            body = canonical({
                'source_type': 'synthetic_fixture', 'entity_id': 'fixture-A',
                'nav_unit': 'CNY/share', 'currency': 'CNY', 'measurement_basis': 'unit_nav',
                'nav': [
                    {'id': 'nav-start', 'value': '1', 'at': '2020-01-01T00:00:00Z', 'released_at': '2020-01-02T00:00:00Z'},
                    {'id': 'nav-reinvest', 'value': '0.5', 'at': '2020-01-04T00:00:00Z', 'released_at': '2020-01-05T00:00:00Z'},
                    {'id': 'nav-end', 'value': '0.6', 'at': '2020-01-06T00:00:00Z', 'released_at': '2020-01-07T00:00:00Z'},
                ],
                'split': {'at': '2020-01-02T00:00:00Z', 'multiplier': '2', 'released_at': '2020-01-03T00:00:00Z'},
                'dividend': {'at': '2020-01-03T00:00:00Z', 'cash_per_share': '0.1',
                             'payable_at': '2020-01-03T00:00:00Z', 'reinvested_at': '2020-01-04T00:00:00Z',
                             'released_at': '2020-01-04T00:00:00Z'},
                'contract': {'id': 'contract-A', 'share_class_id': 'fixture-share-A', 'platform': 'synthetic-publisher',
                             'effective_from': '2020-01-01T00:00:00Z', 'effective_to': 'ongoing',
                             'released_at': '2019-12-31T00:00:00Z', 'dividend_election': 'documented_reinvestment'},
            })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/vintages':
            body = canonical({'source_type': 'synthetic_fixture', 'entity_id': 'fixture-A',
                              'nav_unit': 'CNY/share', 'currency': 'CNY', 'measurement_basis': 'unit_nav',
                              'period_at': '2020-01-01T00:00:00Z',
                              'vintages': [
                                  {'id': 'original', 'value': '1.25', 'released_at': '2020-01-02T00:00:00Z'},
                                  {'id': 'revision', 'value': '1.3', 'released_at': '2020-02-02T00:00:00Z'},
                              ]})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"fund":"fixture-A","nav":"1.2500","unit":"CNY/share","links":[{"href":"/revision"}]}'
        self.send_response(503 if self.path == '/fees' else 200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProducerCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ.get('SC05_TEST_ROOT', str(Path.home() / '.investment/test/sc05-producer-20261020')))
        guard_namespace(cls.root, 'test')
        cls.root = cls.root / ('calibration-' + uuid4().hex)
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.directory = cls.root / 'runs/sc05/20261020/source-calibration/attempt-01'
        cls.directory.mkdir(parents=True, exist_ok=True)
        (cls.root / 'admission.json').write_text(json.dumps(ADMISSION, ensure_ascii=False, indent=2), encoding='utf-8')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Publisher)
        cls.server.calls = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.rule = {'provider_id': PROVIDER_ID, 'parser_id': PARSER_ID, 'parser_version': PARSER_VERSION, 'allowed_domains': ['127.0.0.1']}
        cls.spec = {'request_timeout_seconds': 5, 'max_response_bytes': 4096}
        cls.schemas = ResearchSchemas(('SC03', 'SC04', 'SC05'))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_actual_nav_and_fee_transport_are_separate(self):
        nav = retrieve(self.rule, self.base + '/nav', self.spec, task_id='fixture-nav', source_type='synthetic_fixture')
        fee = retrieve(self.rule, self.base + '/fees', self.spec, task_id='fixture-fees', source_type='synthetic_fixture')
        self.assertEqual((nav.result, nav.status['value']), ('success', 200))
        self.assertEqual((fee.result, fee.status['value']), ('http_error', 503))
        expected = b'{"fund":"fixture-A","nav":"1.2500","unit":"CNY/share","links":[{"href":"/revision"}]}'
        self.assertEqual(base64.b64decode(nav.capture['body_base64'], validate=True), expected)
        self.assertEqual(nav.capture['body_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(nav.capture['body_byte_size'], len(expected))
        self.assertEqual(discovered_links(nav.capture, self.rule), [self.base + '/revision'])
        for name, retrieval in [('nav', nav), ('fees', fee)]:
            self.schemas.validate('investment.sc05.capture', retrieval.capture)
            path = self.directory / (name + '-actual-capture.json')
            raw = canonical(retrieval.capture)
            path.write_bytes(raw)
            reference = artifact(raw, 'investment.sc05.capture',
                                 'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
            attempt = {'attempt_id': 'attempt-' + name, 'task_id': retrieval.capture['task_id'], 'sequence': 1,
                       'requested_at': retrieval.requested_at, 'finished_at': retrieval.finished_at, 'url': retrieval.capture['requested_url'],
                       'result': retrieval.result, 'http_status': retrieval.status, 'raw_ref': known(reference),
                       'error': retrieval.error, 'source_type': 'synthetic_fixture'}
            self.schemas.validate('investment.sc05.attempt_row', attempt)

    def test_redirect_is_a_separate_preserved_http_response(self):
        before = len(self.server.calls)
        first = retrieve(self.rule, self.base + '/redirect', self.spec, task_id='fixture-redirect', source_type='synthetic_fixture')
        self.assertEqual(len(self.server.calls), before + 1)
        self.assertEqual(first.status['value'], 302)
        self.assertEqual(first.capture['requested_url'], first.capture['final_url'])
        self.assertEqual(base64.b64decode(first.capture['body_base64']), b'original redirect body')
        self.assertEqual(first.redirects, [self.base + '/nav'])
        second = retrieve(self.rule, first.redirects[0], self.spec, task_id='fixture-final', source_type='synthetic_fixture')
        self.assertEqual(second.status['value'], 200)
        self.assertEqual(len(self.server.calls), before + 2)

    def test_response_budget_does_not_mislabel_partial_bytes(self):
        result = retrieve(self.rule, self.base + '/nav', {**self.spec, 'max_response_bytes': 8},
                          task_id='fixture-budget', source_type='synthetic_fixture')
        self.assertEqual(result.result, 'parse_error')
        self.assertIsNone(result.capture)
        self.assertIn('partial bytes', result.error['value'])

    def test_representation_rounding_has_independent_exact_expectations(self):
        self.assertEqual(quantized_decimal(Fraction(1, 8), 2), '0.12')
        self.assertEqual(quantized_decimal(Fraction(3, 8), 2), '0.38')
        self.assertEqual(quantized_decimal(Fraction(-1, 8), 2), '-0.12')
        self.assertEqual(quantized_decimal(Fraction(44, 100), 12), '0.44')

    def _wealth_material(self):
        retrieval = retrieve(self.rule, self.base + '/wealth', self.spec, task_id='fixture-wealth', source_type='synthetic_fixture')
        raw = canonical(retrieval.capture)
        path = self.directory / ('wealth-' + uuid4().hex + '-capture.json')
        path.write_bytes(raw)
        reference = artifact(raw, 'investment.sc05.capture',
                             'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
        source = json.loads(base64.b64decode(retrieval.capture['body_base64']))
        def time(at):
            return known({'lower_bound': at, 'upper_bound': at, 'precision': 'instant', 'timezone': 'UTC', 'basis_ref': reference})
        def citations(locator, quote):
            return [{'raw_ref': reference, 'locator': 'json:' + locator, 'quote': quote, 'claim': 'Declared synthetic financial source quantity'}]
        observations = []
        for index, record in enumerate(source['nav']):
            row = {'version_id': record['id'], 'entity_id': source['entity_id'], 'metric_id': 'unit-nav',
                   'observation_period': time(record['at']), 'value': known(record['value']), 'unit': private_known(source['nav_unit']),
                   'currency': private_known(source['currency']), 'raw_value': known(record['value']), 'raw_unit': private_known(source['nav_unit']),
                   'raw_currency': private_known(source['currency']), 'conversion': na('No quantity conversion'),
                   'published_time': time(record['released_at']), 'available_time': time(record['released_at']),
                   'availability_basis': 'archived_capture', 'availability_bound_at': known(record['released_at']),
                   'retrieved_at': retrieval.capture['retrieved_at'], 'raw_ref': reference, 'supersedes_version_id': na('Original release'),
                   'measurement_basis': source['measurement_basis'], 'citations': citations('/nav/' + str(index) + '/value', record['value'])}
            self.schemas.validate('investment.sc05.observation_row', row)
            observations.append(row)
        source_contract = source['contract']
        contract = {'contract_version_id': source_contract['id'], 'entity_id': source['entity_id'], 'share_class_id': source_contract['share_class_id'],
                    'platform': source_contract['platform'], 'effective_from': time(source_contract['effective_from']),
                    'effective_to': na('Documented ongoing election'), 'published_time': time(source_contract['released_at']),
                    'available_time': time(source_contract['released_at']), 'fees': [],
                    'terms': [{'term_id': 'fixture-election', 'term': 'dividend_election', 'value': na('Textual election'),
                               'text_value': private_known(source_contract['dividend_election']), 'unit': na('Textual election'),
                               'currency': na('Not a monetary quantity'), 'day_count': 'not_applicable',
                               'citations': citations('/contract/dividend_election', source_contract['dividend_election'])}],
                    'raw_refs': [reference], 'completeness': 'unknown', 'missing_fields': ['fee_schedule']}
        self.schemas.validate('investment.sc05.contract_vintage_row', contract)
        events = []
        for kind in ('split', 'dividend'):
            record = source[kind]
            row = {'event_id': 'event-' + kind, 'entity_id': source['entity_id'],
                   'event_type': 'cash_dividend' if kind == 'dividend' else 'split',
                   'record_at': time(record['at']), 'ex_at': time(record['at']),
                   'payable_at': time(record['payable_at']) if kind == 'dividend' else na('No cash payment'),
                   'available_time': time(record['released_at']), 'cash_per_share': known(record['cash_per_share']) if kind == 'dividend' else na('Share reorganization'),
                   'currency': private_known(source['currency']), 'share_multiplier': known(record['multiplier']) if kind == 'split' else na('Cash entitlement'),
                   'reinvestment_nav_version_id': known('nav-reinvest') if kind == 'dividend' else na('No reinvestment'),
                   'reinvestment_at': time(record['reinvested_at']) if kind == 'dividend' else na('No reinvestment'),
                   'contract_version_id': contract['contract_version_id'],
                   'citations': citations('/' + kind, record['cash_per_share'] if kind == 'dividend' else record['multiplier']),
                   'cash_unit': 'currency_per_share'}
            self.schemas.validate('investment.sc05.equity_event_row', row)
            events.append(row)
        return observations, events, [contract]

    def test_documented_wealth_uses_reinvestment_price_and_complete_dependencies(self):
        observations, events, contracts = self._wealth_material()
        result, versions, contract_ids, event_ids, bounds = reinvested_wealth(
            observations[0], observations[2], events, {row['version_id']: row for row in observations},
            {row['contract_version_id']: row for row in contracts}, equity_complete=True)
        # 独立预期：1份拆为2份；现金0.2按NAV0.5再投0.4份；2.4*0.6-1=0.44。
        self.assertEqual(result, Fraction(11, 25))
        self.assertEqual(versions, {'nav-start', 'nav-end', 'nav-reinvest'})
        self.assertEqual(contract_ids, {'contract-A'})
        self.assertEqual(event_ids, {'event-split', 'event-dividend'})
        self.assertEqual(max(bounds).isoformat(), '2020-01-07T00:00:00+00:00')

    def test_unpublished_label_stores_no_outcome_even_after_economic_end(self):
        observations, events, contracts = self._wealth_material()
        protocol = {'information_cutoff_at': '2020-01-06T12:00:00Z',
                    'target_definition': {'target_id': 'total-return', 'price_basis': 'unit_nav_with_documented_equity_events',
                                          'numerical_precision': {'decimal_places': 12}},
                    'horizons': [{'horizon_id': 'five-days', 'unit': 'calendar_day', 'length': 5,
                                  'calendar_id': 'fixture-calendar', 'end_rule': 'exact_calendar_boundary'}]}
        gaps = []
        labels = build_labels(protocol, [{'sample_id': 'fixture-sample', 'entity_id': 'fixture-A', 'origin_at': '2020-01-01T00:00:00Z'}],
                              observations, events, contracts, [], calendar_at=lambda *args: (), equity_complete=lambda *args: True, gaps=gaps)
        self.assertEqual(len(labels), 1)
        self.schemas.validate('investment.sc05.label_row', labels[0])
        self.assertEqual(labels[0]['label_status'], 'immature')
        self.assertEqual(labels[0]['label_available_at']['value'], '2020-01-07T00:00:00Z')
        self.assertEqual(labels[0]['outcome']['status'], 'unknown')
        self.assertIsNone(labels[0]['outcome']['value'])
        self.assertTrue(labels[0]['gap_ids'] and gaps)
        protocol['information_cutoff_at'] = '2020-01-10T00:00:00Z'
        protocol['split_rule'] = {
            'independent_origin_start': '2020-01-20T00:00:00Z', 'independent_origin_end': '2020-01-31T00:00:00Z',
            'selection_information_cutoff_at': '2020-01-19T00:00:00Z',
            'folds': [{'fold_id': 'fixture-fold', 'train_origin_start': '2020-01-01T00:00:00Z',
                       'train_origin_end': '2020-01-01T00:00:00Z', 'fit_cutoff_at': '2020-01-06T12:00:00Z',
                       'evaluate_origin_start': '2020-01-10T00:00:00Z', 'evaluate_origin_end': '2020-01-15T00:00:00Z',
                       'purpose': 'selection'}],
        }
        protocol['purge_embargo_rule'] = {'dependency_window_days': 0}
        panels = [{'sample_id': 'fixture-sample', 'entity_id': 'fixture-A', 'origin_at': '2020-01-01T00:00:00Z', 'eligible_for_training': True}]
        mature = build_labels(protocol, panels, observations, events, contracts, [],
                              calendar_at=lambda *args: (), equity_complete=lambda *args: True, gaps=[])
        self.assertEqual(mature[0]['outcome']['value'], '0.44')
        split = assignments(protocol, panels, mature)
        self.schemas.validate('investment.sc05.split_row', split[0])
        self.assertEqual(split[0]['partition'], 'excluded')
        self.assertEqual(split[0]['fit_cutoff_at'], '2020-01-06T12:00:00Z')

    def test_macro_nonmonetary_reasons_preserve_quantity_semantics(self):
        retrieval = retrieve(self.rule, self.base + '/macro', self.spec, task_id='fixture-macro', source_type='synthetic_fixture')
        self.assertEqual((retrieval.result, retrieval.status['value']), ('success', 200))
        self.schemas.validate('investment.sc05.capture', retrieval.capture)
        raw = canonical(retrieval.capture)
        path = self.directory / 'macro-actual-capture.json'
        path.write_bytes(raw)
        reference = artifact(raw, 'investment.sc05.capture',
                             'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
        source = json.loads(base64.b64decode(retrieval.capture['body_base64']))
        def evidence(at):
            return known({'lower_bound': at, 'upper_bound': at, 'precision': 'instant', 'timezone': 'UTC', 'basis_ref': reference})
        rows = []
        for index, record in enumerate(source['observations']):
            row = {'version_id': record['id'], 'entity_id': source['entity_id'], 'metric_id': source['metric_id'],
                   'observation_period': evidence(source['observation_at']), 'value': known(record['value']),
                   'unit': private_known(source['unit']), 'currency': na(record['currency_basis']),
                   'raw_value': known(record['value']), 'raw_unit': private_known(source['unit']),
                   'raw_currency': na(source['raw_currency_basis']), 'conversion': na('The reported percentage needs no conversion'),
                   'published_time': evidence(source['published_at']), 'available_time': evidence(retrieval.capture['retrieved_at']),
                   'availability_basis': 'current_capture_only', 'availability_bound_at': known(retrieval.capture['retrieved_at']),
                   'retrieved_at': retrieval.capture['retrieved_at'], 'raw_ref': reference,
                   'supersedes_version_id': na('Separate declared source observation'), 'measurement_basis': 'economic_metric',
                   'citations': [{'raw_ref': reference, 'locator': 'json:/observations/' + str(index) + '/value',
                                  'quote': record['value'], 'claim': 'Declared synthetic nonmonetary macro percentage'}]}
            self.schemas.validate('investment.sc05.observation_row', row)
            rows.append(row)
        before = canonical(rows)
        normalized = normalize_observations(rows, {reference['uri']: retrieval.capture})
        self.assertEqual(canonical(normalized), before)
        selected = select_version(normalized, source['entity_id'], source['metric_id'], retrieval.capture['retrieved_at'])
        self.assertIsNotNone(selected)
        self.assertEqual((selected['version_id'], selected['value']['value']), ('macro-a', '5.1'))
        for row, record in zip(rows, source['observations']):
            self.assertEqual(row['currency']['reason'], record['currency_basis'])
            self.assertEqual(row['raw_currency']['reason'], source['raw_currency_basis'])
        # 原因措辞可不同；真实状态不同或单位值不同仍不能当作同一量。
        for field, changed in (('currency', unknown('Currency applicability is unverified')),
                               ('unit', private_known('fraction'))):
            with self.subTest(field=field), self.assertRaises(ContractError):
                normalize_observations([{**rows[0], field: changed}], {reference['uri']: retrieval.capture})
            self.assertIsNone(select_version([rows[0], {**rows[1], field: changed}],
                                             source['entity_id'], source['metric_id'], retrieval.capture['retrieved_at']))
        self.assertEqual(canonical(rows), before)

    def test_revision_uses_only_actual_origin_information(self):
        retrieval = retrieve(self.rule, self.base + '/vintages', self.spec, task_id='fixture-vintages', source_type='synthetic_fixture')
        raw = canonical(retrieval.capture)
        path = self.directory / 'vintages-actual-capture.json'
        path.write_bytes(raw)
        reference = artifact(raw, 'investment.sc05.capture',
                             'investment-run://sc05/20261020/source-calibration/attempt-01/' + path.name)
        source = json.loads(base64.b64decode(retrieval.capture['body_base64']))
        def evidence(date):
            return known({'lower_bound': date, 'upper_bound': date, 'precision': 'instant', 'timezone': 'UTC', 'basis_ref': reference})
        def version(record, supersedes):
            value, released = record['value'], record['released_at']
            row = {'version_id': record['id'], 'entity_id': source['entity_id'], 'metric_id': 'unit-nav', 'value': known(value),
                    'unit': private_known(source['nav_unit']), 'currency': private_known(source['currency']), 'measurement_basis': source['measurement_basis'],
                    'available_time': evidence(released), 'conversion': na('Source quantity is unchanged'),
                    'observation_period': evidence(source['period_at']), 'supersedes_version_id': supersedes,
                    'raw_value': known(value), 'raw_unit': private_known(source['nav_unit']), 'raw_currency': private_known(source['currency']),
                    'published_time': evidence(released), 'availability_basis': 'archived_capture',
                    'availability_bound_at': known(released), 'retrieved_at': retrieval.capture['retrieved_at'], 'raw_ref': reference,
                    'citations': [{'raw_ref': reference, 'locator': 'json:/vintages', 'quote': value,
                                   'claim': 'Declared synthetic publisher vintage value'}]}
            self.schemas.validate('investment.sc05.observation_row', row)
            return row
        old = version(source['vintages'][0], na('Original publication'))
        new = version(source['vintages'][1], known('original'))
        normalize_observations([old, new], {reference['uri']: retrieval.capture})
        self.assertEqual(select_version([old, new], 'fixture-A', 'unit-nav', '2020-01-03T00:00:00Z')['value']['value'], '1.25')
        self.assertEqual(select_version([old, new], 'fixture-A', 'unit-nav', '2020-02-03T00:00:00Z')['value']['value'], '1.3')


if __name__ == '__main__':
    unittest.main()
