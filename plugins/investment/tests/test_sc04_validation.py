"""SC04 基础规则/本批新取原文校准；不创建StageContext、候选、凭证或封存。"""
import base64
import copy
import hashlib
import io
import json
import os
import re
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from investment.contracts import ContractError, canonical, known, na, unknown
from investment.chains.fund_universe.contracts import (
    availability_at, current_scope_gaps, inherited_gaps, predicate_verdicts, recompute_partitions,
    verify_mandatory_risks, verify_partition, verify_weight_bounds, verify_historical_exposure,
)
from investment.chains.fund_universe.source_validation import (
    capture_bytes, citation_text, numeric_literals, original_directory, pdf_page_text,
)
from investment.chains.fund_universe.review_work import ReviewWorkPlanner, field_batches
from investment.research.contracts import ResearchSchemas


def identity(value='CNY'):
    # 基础计算的已知前提，不充当实际基金档案或上游业务对象。
    return {'entity_id': known('entity'), 'legal_fund_id': known('legal'),
            'share_class_id': known('share-class'), 'currency': known(value)}


class ExactRuleCalibration(unittest.TestCase):
    def test_unknown_identity_and_cross_group_partition(self):
        rule = {'predicates': [{'predicate_id': 'currency', 'field': 'currency', 'operator': 'equals', 'accepted_values': ['CNY']}]}
        identities = {'a': identity(), 'b': None, 'c': identity('USD')}
        predicates = {key: predicate_verdicts(value, rule) for key, value in identities.items()}
        provenance = [
            {'member_id': 'a', 'peer_group_id': 'first', 'relation': 'proven_not_peer'},
            {'member_id': 'a', 'peer_group_id': 'second', 'relation': 'confirmed'},
            {'member_id': 'b', 'peer_group_id': 'first', 'relation': 'possible'},
            {'member_id': 'c', 'peer_group_id': 'first', 'relation': 'confirmed'},
        ]
        local, global_sets = recompute_partitions(['a', 'b', 'c'], ['first', 'second'], provenance, identities, predicates)
        self.assertEqual(local['first'], {'candidate': set(), 'excluded': {'a', 'c'}, 'unknown': {'b'}})
        self.assertEqual(local['second']['candidate'], {'a'})
        self.assertEqual(global_sets, {'candidate': {'a'}, 'excluded': {'c'}, 'unknown': {'b'}})

    def test_missing_share_class_stays_unknown(self):
        partial = identity()
        partial['share_class_id'] = unknown('Only the directory code was obtained')
        _, result = recompute_partitions(['a'], ['group'], [{'member_id': 'a', 'peer_group_id': 'group', 'relation': 'confirmed'}],
                                         {'a': partial}, {'a': {'currency': 'true'}})
        self.assertEqual(result['unknown'], {'a'})

    def test_partition_rejects_a_dropped_original_member(self):
        with self.assertRaises(ContractError) as caught:
            verify_partition(['a', 'b'], {'candidate': [{'member_id': 'a', 'status': 'candidate'}], 'excluded': [], 'unknown': []})
        self.assertEqual(caught.exception.rule, 'U07')

    def test_all_registered_and_pending_risks_are_preserved(self):
        # 只校准行级复制和键集合；这些值不是伪造的TransactionContext/Seal。
        positions = [{'instrument_id': 'fund', 'confirmed_shares': unknown('User has not supplied confirmed shares')}]
        pending = [{'instrument_id': 'fund', 'trade_id': 'buy', 'side': 'buy', 'pending_phase': 'confirmation'},
                   {'instrument_id': 'fund', 'trade_id': 'sell', 'side': 'sell', 'pending_phase': 'settlement'}]
        position_origin, pending_origin = {'source': 'position-method-argument'}, {'source': 'pending-method-argument'}
        rows = [{'risk_kind': 'registered_position', 'instrument_id': known('fund'), 'trade_id': na(), 'origin_ref': position_origin,
                 'origin_locator': '/rows/0', 'confirmed_shares': positions[0]['confirmed_shares'], 'amount': unknown(),
                 'coverage': 'recorded_transactions_since_start', 'retention_status': 'retained'}]
        for index, trade in enumerate(pending):
            rows.append({'risk_kind': 'pending_' + trade['side'], 'instrument_id': known('fund'), 'trade_id': known(trade['trade_id']),
                         'origin_ref': pending_origin, 'origin_locator': f'/rows/{index}', 'confirmed_shares': unknown(),
                         'amount': unknown(), 'pending_phase': known(trade['pending_phase']),
                         'coverage': 'recorded_transactions_since_start', 'retention_status': 'retained'})
        verify_mandatory_risks(positions, pending, rows, position_origin, pending_origin)
        with self.assertRaises(ContractError) as caught:
            verify_mandatory_risks(positions, pending, rows[:-1], position_origin, pending_origin)
        self.assertEqual(caught.exception.rule, 'U12')

    def test_disclosed_weights_use_exact_decimal_and_real_denominator(self):
        row = {'exposure_kind': 'contract_range', 'weight': known('0.8'), 'lower_weight': known('0.8'),
               'upper_weight': unknown(), 'denominator': known('stock assets')}
        self.assertEqual(verify_weight_bounds(row)['weight'], Decimal('0.8'))
        self.assertIn(Decimal('0.8'), numeric_literals('股票资产的80%'))
        self.assertNotIn(Decimal('80'), numeric_literals('股票资产的80%'))
        row['denominator'] = unknown('No denominator was disclosed')
        with self.assertRaises(ContractError):
            verify_weight_bounds(row)

    def test_historical_availability_gap_is_not_backfilled(self):
        evidence = known({'lower_bound': '2026-07-27T00:00:00+08:00', 'upper_bound': '2026-07-27T23:59:59+08:00'})
        self.assertFalse(availability_at(evidence, unknown('No historical availability proof'), '2026-07-28T00:00:00+08:00'))
        self.assertFalse(availability_at(evidence, known('2026-07-27T23:59:59+08:00'), '2026-07-26T00:00:00+08:00'))
        self.assertTrue(availability_at(evidence, known('2026-07-27T23:59:59+08:00'), '2026-07-28T00:00:00+08:00'))
        with self.assertRaises(ContractError):
            availability_at(evidence, known('2026-07-26T00:00:00+08:00'), '2026-07-28T00:00:00+08:00')

    def test_original_gap_and_noncritical_account_gate(self):
        gap = {'rule_id': 'upstream-rule', 'field_or_object': '/source', 'reason': 'Actual upstream limitation',
               'owner': 'source', 'required_evidence': ['source_bytes'], 'affected_uses': ['reporting']}
        # 只测试Gap值的忠实传递，不伪造ValidationReceipt或调用StageContext。
        assessment_gap = {**gap, 'field_or_object': '/hypotheses', 'reason': 'Unresolved research question'}
        self.assertEqual(inherited_gaps([{'blockers': [gap]}], {'missing_facts': [gap]},
                                       {'unresolved_questions': [assessment_gap]}), [gap, assessment_gap])
        runtime = ResearchSchemas(('SC03', 'SC04')).definition('SC04')
        self.assertIn('U21', runtime['required_rules'])
        self.assertNotIn('U21', runtime['critical_rules'])
        gates = {item['use']: item['required_pass_rules'] for item in runtime['use_rules']}
        self.assertIn('U21', gates['current_account_review'])
        self.assertNotIn('U21', gates['reporting'])

    def test_historical_scope_gap_does_not_replace_current_scope(self):
        current, historical = '2026-10-19T00:00:00Z', '2025-10-19T00:00:00Z'
        tasks = {'today': {'requested_origin': current, 'source_id': 'catalog'},
                 'past': {'requested_origin': historical, 'source_id': 'catalog'}}
        old_page = {'task_id': known('past'), 'source_id': 'catalog', 'scope_kind': 'page'}
        current_page = {**old_page, 'task_id': known('today')}
        self.assertEqual(current_scope_gaps(tasks, [old_page, current_page], current, [historical]), [current_page])
        with self.assertRaises(ContractError):
            current_scope_gaps(tasks, [{**current_page, 'scope_kind': 'historical_vintage'}], current, [historical])

    def test_private_field_batches_cover_array_elements_and_empty_value(self):
        value = {'rows': [{'member_id': 'first'}, {'member_id': 'second'}], 'empty': [], 'status': 'unknown'}
        batches = list(field_batches(value, ['rows', 'empty', 'status']))
        self.assertEqual(batches, [['/rows/0'], ['/rows/1'], ['/empty', '/status']])
        self.assertEqual(sum(len(paths) for paths in batches), 4)

    def test_history_work_item_keeps_original_versions_and_excludes_unrelated_member(self):
        # 只校准私有引用图，不构造上游Seal或完整阶段对象。
        values, refs = {}, {}
        def reference(name, schema, value):
            raw = canonical(value)
            ref = {'artifact_id': name, 'schema_id': schema, 'schema_version': 1,
                   'media_type': 'application/json', 'uri': 'investment-run://sc04/20261020/calibration/attempt-01/' + name + '.json',
                   'sha256': hashlib.sha256(raw).hexdigest(), 'byte_size': len(raw)}
            values[ref['uri']], refs[ref['uri']] = value, ref
            return ref
        raw_ref = reference('original', 'investment.sc04.capture', {'original_text': 'Original source context'})
        unrelated = reference('unrelated', 'investment.sc04.capture', {'original_text': 'Another member'})
        mapping_ref = reference('mapping', 'investment.sc04.exposure_mapping_rule', {'permitted_kinds': ['contract_range'], 'basis_refs': [raw_ref]})
        historical_identity = reference('old-identity', 'investment.sc04.identity', {'entity_id': known('old-entity'), 'share_class_id': known('old-share')})
        vintage_ref = reference('old-catalog', 'investment.sc04.catalog_vintage', {'original_list_refs': [raw_ref]})
        eligibility_ref = reference('old-rule', 'investment.sc04.eligibility_rule', {'predicates': []})
        exposure = {'exposure_id': 'exposure', 'member_id': 'past-member', 'hypothesis_id': unknown(),
                    'mapping_rule_ref': mapping_ref, 'citations': []}
        exposures_ref = reference('old-exposures', 'investment.sc04.exposures',
                                  {'rows': [exposure, {**exposure, 'exposure_id': 'foreign', 'member_id': 'other-member', 'citations': [unrelated]}]})
        history_ref = reference('history', 'investment.sc04.history', {'rows': [{
            'origin_at': '2026-07-28T00:00:00Z', 'member_id': 'past-member',
            'identity_version_ref': known(historical_identity), 'source_vintage_ref': vintage_ref,
            'eligibility_rule_ref': eligibility_ref, 'exposure_version_refs': [exposures_ref]}]})
        current_ref = reference('current-members', 'investment.sc04.members', {'rows': []})
        review = SimpleNamespace(refs=refs, values=values, rows={'members_ref': [], 'peer_groups_ref': []},
                                 history=values[history_ref['uri']]['rows'], funding=None, funding_ref=None,
                                 payload={'universe_history_ref': known(history_ref), 'members_ref': current_ref}, assessment={})
        planner = ReviewWorkPlanner(review)
        work = planner.build(exposures_ref, ['/rows/0'])
        locations = {(row['object_ref']['uri'], row['json_pointer']) for row in work['evidence_locations']}
        self.assertIn((history_ref['uri'], '/rows/0'), locations)
        self.assertIn((historical_identity['uri'], ''), locations)
        self.assertNotIn((exposures_ref['uri'], '/rows/1'), locations)
        selected_uris = {uri for uri, _ in locations}
        self.assertNotIn(unrelated['uri'], selected_uris)
        self.assertNotIn(current_ref['uri'], selected_uris)
        self.assertEqual(work['field_paths'], ['/rows/0'])
        expected_key = hashlib.sha256(canonical({key: value for key, value in work.items() if key != 'work_key'})).hexdigest()
        self.assertEqual(work['work_key'], expected_key)
        schemas = ResearchSchemas(('SC03', 'SC04'))
        schemas.validate('investment.sc04.review_work_item', work)
        with self.assertRaises(ContractError):
            schemas.validate('investment.sc04.review_work_item', {**work, 'producer_summary': 'Not permitted'})
        subject_ref = reference('subjects', 'investment.sc03.hypotheses', {'rows': [
            {'hypothesis_id': 'selected', 'original_ref': raw_ref},
            {'hypothesis_id': 'unrelated', 'original_ref': unrelated}]})
        planner.locations = {}
        planner.follow({'source_ref': subject_ref, 'source_locator': '/rows/0'}, {'subject_id': 'subject'})
        subject_locations = {(row['object_ref']['uri'], row['json_pointer']) for row in planner.locations.values()}
        self.assertIn((subject_ref['uri'], '/rows/0'), subject_locations)
        self.assertNotIn((subject_ref['uri'], '/rows/1'), subject_locations)
        self.assertNotIn(unrelated['uri'], {uri for uri, _ in subject_locations})
        # 目标全集由原始行数独立给出，不将一份响应冒充全表覆盖。
        isolated = SimpleNamespace(refs={exposures_ref['uri']: exposures_ref}, values={exposures_ref['uri']: values[exposures_ref['uri']]},
                                   rows={'members_ref': [], 'peer_groups_ref': []}, history=[], funding=None)
        self.assertEqual([paths for _, paths in ReviewWorkPlanner(isolated).targets()], [['/rows/0'], ['/rows/1']])

    def test_history_exposure_checks_original_number_identity_and_origin(self):
        history = {'member_id': 'past-member', 'entity_id': known('old-entity'),
                   'origin_at': '2026-07-28T00:00:00Z', 'membership_status': 'candidate'}
        old_identity = {'entity_id': known('old-entity'), 'share_class_id': known('old-share')}
        row = {'member_id': 'past-member', 'share_class_id': known('old-share'),
               'exposure_kind': 'contract_range', 'weight': known('0.8'), 'lower_weight': unknown(),
               'upper_weight': unknown(), 'denominator': known('stock assets'),
               'available_time': known({'lower_bound': '2026-07-27T00:00:00Z', 'upper_bound': '2026-07-27T23:59:59Z'}),
               'availability_bound_at': known('2026-07-27T23:59:59Z')}
        mapping = {'permitted_kinds': ['contract_range']}
        self.assertTrue(verify_historical_exposure(row, history, old_identity, mapping, ['股票资产的80%']))
        for changed in ({**row, 'weight': known('0.7')}, {**row, 'share_class_id': known('current-share')},
                        {**row, 'availability_bound_at': known('2026-07-29T00:00:00Z')},
                        {**row, 'member_id': 'current-member'}):
            with self.subTest(change=changed):
                with self.assertRaises(ContractError) as caught:
                    verify_historical_exposure(changed, history, old_identity, mapping, ['股票资产的80%'])
                self.assertEqual(caught.exception.rule, 'U15')
        self.assertFalse(verify_historical_exposure({**row, 'availability_bound_at': unknown()}, {**history, 'membership_status': 'unknown'},
                                                   old_identity, mapping, ['股票资产的80%']))


@unittest.skipUnless(os.environ.get('SC04_CALIBRATION_ROOT'), 'Run with an explicitly prepared fresh source batch')
class ActualSourceCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ['SC04_CALIBRATION_ROOT']).resolve()
        test_root = (Path.home() / '.investment/test').resolve()
        if not cls.root.is_relative_to(test_root) or cls.root == test_root:
            raise AssertionError('Fresh isolated test batch required')
        cls.directory = json.loads((cls.root / 'allfund.capture.json').read_text(encoding='utf-8'))
        cls.pdf = json.loads((cls.root / 'issuer-prospectus.capture.json').read_text(encoding='utf-8'))
        cls.attempt = cls.root / 'source-calibration'
        cls.attempt.mkdir(exist_ok=True)

    def test_actual_directory_preserves_all_original_code_signals(self):
        raw = capture_bytes(self.directory)
        entries, following = original_directory(self.directory, max_bytes=16 * 1024 * 1024)
        # 与全响应中的独立URL词法集合核对；不是调用被测枚举器生成预期。
        observed_codes = {match.decode('ascii') for match in re.findall(rb'https?://fund\.eastmoney\.com/(\d{6})\.html', raw)}
        actual_codes = {entry['code'] for entry in entries.values() if entry['code'] is not None}
        self.assertTrue(actual_codes == observed_codes, f'Full original code set differs: actual={len(actual_codes)}, original={len(observed_codes)}')
        self.assertIn('000001', observed_codes)
        self.assertGreater(len(entries), 1)
        # 实际来源为单文档；不人为造页声称paginated_discovery已校准。
        self.assertEqual(following, set())

    def test_real_capture_digest_rejects_corruption(self):
        changed = copy.deepcopy(self.directory)
        changed['body_sha256'] = '0' * 64
        with self.assertRaises(ContractError) as caught:
            capture_bytes(changed)
        self.assertEqual(caught.exception.rule, 'U02')

    def test_complete_decoding_does_not_make_a_real_code_unknown(self):
        entries, _ = original_directory(self.directory, max_bytes=16 * 1024 * 1024)
        matching = [(path, row) for path, row in entries.items() if '东方永兴18个月定开债C' in row['label']]
        self.assertEqual(len(matching), 1)
        locator, entry = matching[0]
        self.assertEqual(entry['code'], '003325')
        text = citation_text({'locator_kind': 'html_element', 'locator': locator, 'quoted_text': entry['label']}, self.directory, self.attempt)
        self.assertIn(entry['label'], text)

    def test_actual_html_original_location_is_required(self):
        entries, _ = original_directory(self.directory, max_bytes=16 * 1024 * 1024)
        locator, entry = next((path, row) for path, row in entries.items() if row['code'] == '000001')
        text = citation_text({'locator_kind': 'html_element', 'locator': locator, 'quoted_text': entry['label']}, self.directory, self.attempt)
        self.assertIn('000001', text)
        with self.assertRaises(ContractError):
            citation_text({'locator_kind': 'html_element', 'locator': locator, 'quoted_text': 'unrelated fabricated statement'}, self.directory, self.attempt)

    def test_actual_pdf_page_denominator_and_owned_cleanup(self):
        raw = capture_bytes(self.pdf)
        scratch = self.attempt / ('validation-pdf-' + hashlib.sha256(raw).hexdigest() + '.pdf')
        audit = io.StringIO()
        with redirect_stderr(audit):
            text = pdf_page_text(raw, 105, self.attempt)
        self.assertIn('基金股票资产的80%', re.sub(r'\s+', '', text))
        self.assertFalse(scratch.exists())
        event = json.loads(audit.getvalue().strip())
        self.assertEqual(event['source_sha256'], self.pdf['body_sha256'])
        self.assertEqual(event['exit_code'], 0)
        self.assertIn('pdftotext', event['tool_version'].lower())
        (self.root / 'pdf-tool-audit.json').write_text(audit.getvalue(), encoding='utf-8')

    def test_existing_matching_pdf_is_not_deleted(self):
        raw = capture_bytes(self.pdf)
        scratch = self.attempt / ('validation-pdf-' + hashlib.sha256(raw).hexdigest() + '.pdf')
        self.assertFalse(scratch.exists())
        scratch.write_bytes(raw)
        try:
            with redirect_stderr(io.StringIO()):
                text = pdf_page_text(raw, 109, self.attempt)
            self.assertIn('并非本基金的跟踪标的', re.sub(r'\s+', '', text))
            self.assertEqual(scratch.read_bytes(), raw)
        finally:
            # 只清理本测试刚创建且父目录已经核定的准确文件。
            scratch.unlink()


if __name__ == '__main__':
    unittest.main()

