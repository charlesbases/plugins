"""Controlled original-source manager completeness regression oracles."""
import unittest

import fund_quality
import fund_universe
from contracts import fingerprint, utc_now
from test_fund_universe import FundUniverseTests
import portfolio_paths
from test_fund_quality import FundQualityTests


class ManagerEvidenceTests(unittest.TestCase):
    setUp = FundQualityTests.setUp
    doc = FundQualityTests.doc
    series = FundQualityTests.series
    source_data = FundQualityTests.source_data

    def issuer(self, body, locators=None):
        reference, document = self.doc(body, "manager-completeness")
        identity = {"code": "123456", "currency": "CNY", "manager_names": "甲经理、乙经理",
                    "manager_tenures": None}
        mapping = {"code": "123456", "source_id": document["source_id"], "url": document["url"],
                   "role": "manager", "locators": locators or [block["locator"] for block in document["blocks"]]}
        return fund_universe._apply_issuer(reference, mapping, identity, self.artifacts)

    @staticmethod
    def table(rows):
        return '<table><tr><th>基金经理</th><th>任职日期</th><th>离任日期</th></tr>'+''.join(
            '<tr>'+''.join('<td>'+cell+'</td>' for cell in row)+'</tr>' for row in rows)+'</table>'

    def complete(self, rows):
        return ('<p>基金代码:123456;任期披露截至:2026-10-08;经理覆盖范围:完整期间;'
                '经理覆盖起始:2024-01-01;经理日期语义:任职日生效、离任日含当日</p>'+self.table(rows))

    def test_locator_hint_cannot_omit_joint_manager_in_same_table(self):
        identity = self.issuer(self.complete([('甲经理', '2024-01-01', '至今'), ('乙经理', '2024-01-01', '至今')]),
                               ['html/table/0/row/1'])
        self.assertEqual({row['name'] for row in identity['manager_tenures']}, {'甲经理', '乙经理'})

    def test_legacy_source_locators_without_completeness_proof_are_unknown(self):
        data, _, contract = self.source_data()
        identity = data['code_info']['123456']
        identity.pop('manager_evidence_proofs', None)
        output = fund_quality.evaluate(self.artifacts.put_json(data), [contract], self.artifacts, utc_now(), self.policy)
        self.assertEqual(output['products']['123456']['readiness'], 'unknown')

    def test_resealed_omission_of_joint_manager_proof_is_rejected(self):
        data, _, contract = self.source_data(appointments=[('甲经理', self.days[0], '至今'), ('乙经理', self.days[0], '至今')])
        identity = data['code_info']['123456']
        proofs = identity.setdefault('manager_evidence_proofs', [{'appointments': [{'name': '甲经理'}, {'name': '乙经理'}]}])
        proofs[0]['appointments'] = [row for row in proofs[0]['appointments'] if row['name'] != '乙经理']
        with self.assertRaisesRegex(ValueError, 'proof|Proof'):
            fund_quality.evaluate(self.artifacts.put_json(data), [contract], self.artifacts, utc_now(), self.policy)

    def test_two_fund_original_keeps_primary_subject_teams_separate(self):
        body = self.complete([('甲经理', '2024-01-01', '至今')])+self.complete(
            [('乙经理', '2024-01-01', '至今')]).replace('123456', '654321')
        identity = self.issuer(body, ['html/block/0', 'html/table/0/row/1'])
        self.assertEqual([row['name'] for row in identity['manager_tenures']], ['甲经理'])
        self.assertEqual(identity['manager_evidence_proofs'][0]['subject']['primary_code_inventory'], ['123456', '654321'])

    def test_unparsed_continuation_row_prevents_complete_coverage(self):
        body = self.complete([('甲经理', '2024-01-01', '至今')])+'<p>经理表续表</p><table><tr><td>乙经理</td><td>日期待核</td></tr></table>'
        identity = self.issuer(body, ['html/block/0', 'html/table/0/row/1'])
        proof = identity['manager_evidence_proofs'][0]
        self.assertEqual(proof['coverage']['status'], 'unknown')
        self.assertTrue(any(row['status'] == 'unparsed' for row in proof['row_inventory']))

    def test_supported_continuation_rows_are_inventoried(self):
        body = self.complete([('甲经理', '2024-01-01', '至今')])+'<p>经理表续表</p><table><tr><td>乙经理</td><td>2024-01-01</td><td>至今</td></tr><tr><td>丙经理</td><td>2024-02-01</td><td>至今</td></tr></table>'
        identity = self.issuer(body, ['html/block/0', 'html/table/0/row/1'])
        self.assertEqual({row['name'] for row in identity['manager_tenures']}, {'甲经理', '乙经理', '丙经理'})
        self.assertEqual(identity['manager_evidence_proofs'][0]['coverage']['status'], 'complete')

    def test_current_snapshot_never_establishes_historical_team(self):
        identity = self.issuer(self.complete([('甲经理', '2024-01-01', '至今')]).replace('完整期间', '当前快照'))
        facts = fund_quality._manager_facts(identity, self.artifacts, utc_now())
        self.assertEqual(fund_quality._team_regime(facts, '2024-02-01')['status'], 'unknown')

    def test_explicit_and_automatic_manager_paths_inventory_same_full_team(self):
        body = self.complete([('甲经理', '2024-01-01', '至今'), ('乙经理', '2024-01-01', '至今')])
        explicit = self.issuer(body, ['html/block/0', 'html/table/0/row/1'])
        ref = explicit['manager_evidence_proofs'][0]['document_ref']
        document = self.artifacts.read_json(ref)
        current = {key: value for key, value in explicit.items() if key not in ('manager_evidence_proofs', 'manager_tenures')}
        interpretation = fund_universe._automatic_issuer_mappings(document, '123456', current)
        manager_mapping = next(row for row in interpretation['mappings'] if row['role'] == 'manager')
        automatic = fund_universe._apply_issuer(ref, manager_mapping, current, self.artifacts)
        self.assertEqual(automatic['manager_evidence_proofs'], explicit['manager_evidence_proofs'])

    def test_coverage_gap_disables_quality_features_without_purchase_veto(self):
        data, _, contract = self.source_data()
        body = self.complete([('甲经理', self.days[0], '至今')]).replace('经理覆盖范围:完整期间;', '')
        data['code_info']['123456'] = self.issuer(body)
        prepared = fund_quality.prepare_source_quality(self.artifacts.put_json(data), [contract], self.artifacts, utc_now(), self.policy)
        self.assertEqual(prepared['admission']['excluded_new_codes'], [])
        self.assertEqual(prepared['feature_panel'], [])
        self.assertNotIn('source_evidence_gaps', data['code_info']['123456'])
        with self.assertRaisesRegex(ValueError, 'No source-quality feature vintage'):
            portfolio_paths._quality_features({'code': '123456', 'x': []}, prepared, utc_now())

    def test_later_snapshot_does_not_overwrite_prior_source_appointments(self):
        identity = self.issuer(self.complete([('甲经理', '2024-01-01', '2024-06-01')]))
        ref, document = self.doc(self.complete([('乙经理', '2024-06-02', '至今')]).replace('完整期间', '当前快照'), 'later-manager')
        mapping = {'code': '123456', 'source_id': document['source_id'], 'url': document['url'], 'role': 'manager',
                   'locators': [block['locator'] for block in document['blocks']]}
        result = fund_universe._apply_issuer(ref, mapping, identity, self.artifacts)
        self.assertEqual({row['name'] for row in result['manager_tenures']}, {'甲经理', '乙经理'})
        self.assertEqual(len(result['manager_evidence_proofs']), 2)

    def test_disconnected_current_snapshot_cannot_inherit_historical_alpha(self):
        data, _, contract = self.source_data(disclosure_as_of=self.days[-1])
        identity = data['code_info']['123456']
        ref, document = self.doc(self.complete([('甲经理', self.days[0], '至今')]).replace('完整期间', '当前快照'), 'gap-current-snapshot')
        mapping = {'code': '123456', 'source_id': document['source_id'], 'url': document['url'], 'role': 'manager',
                   'locators': [block['locator'] for block in document['blocks']]}
        data['code_info']['123456'] = fund_universe._apply_issuer(ref, mapping, identity, self.artifacts)
        prepared = fund_quality.prepare_source_quality(self.artifacts.put_json(data), [contract], self.artifacts, utc_now(), self.policy)
        statistics = prepared['current_evaluation']['products']['123456']['statistics']
        self.assertEqual(statistics['manager_regimes'][0]['status'], 'estimated')
        self.assertEqual(statistics['current_manager_regime']['observations'], 0)
        self.assertEqual(prepared['feature_panel'], [])

    def test_updated_distributor_roster_does_not_erase_historical_estimate(self):
        data, _, contract = self.source_data()
        data['code_info']['123456']['manager_names'] = '乙经理'
        prepared = fund_quality.prepare_source_quality(self.artifacts.put_json(data), [contract], self.artifacts, utc_now(), self.policy)
        statistics = prepared['current_evaluation']['products']['123456']['statistics']
        self.assertEqual(statistics['manager_regimes'][0]['status'], 'estimated')
        self.assertEqual(statistics['current_manager_regime']['reason'], 'current_distributor_and_official_team_conflict')
        self.assertEqual(prepared['feature_panel'], [])

    def test_departure_endpoint_is_read_from_original_semantics(self):
        body = self.complete([('甲经理', '2024-01-01', '2024-06-01'), ('乙经理', '2024-06-01', '至今')]).replace('离任日含当日', '离任日不含当日')
        identity = self.issuer(body)
        facts = fund_quality._manager_facts(identity, self.artifacts, utc_now())
        self.assertEqual(fund_quality._team_regime(facts, '2024-06-01')['managers'], ['乙经理'])


class ManagerIdentityProofTests(unittest.TestCase):
    setUp = FundUniverseTests.setUp
    doc = FundQualityTests.doc

    def test_verify_identity_rebuilds_resealed_deleted_manager(self):
        catalog = fund_universe.collect_catalog(self.store, self.artifacts, 'proof-catalog')
        body = ManagerEvidenceTests.complete(self, [('甲经理', '2024-01-01', '至今'), ('乙经理', '2024-01-01', '至今')]).replace('123456', '123451')
        ref, document = self.doc(body, 'identity-manager-proof')
        mapping = {'code': '123451', 'source_id': document['source_id'], 'url': document['url'], 'role': 'manager',
                   'locators': ['html/block/0', 'html/table/0/row/1']}
        snapshot_ref = fund_universe.resolve_identity(['123451'], catalog, self.artifacts, store=self.store,
            issuer_disclosures=[mapping], issuer_sources={fingerprint(mapping): ref})
        snapshot = self.artifacts.read_json(snapshot_ref)
        fund_universe.verify_identity(snapshot, self.artifacts)
        from artifacts import Artifacts
        destination = Artifacts(self.store.base / 'manager-proof-archive')
        archived = fund_universe.archive_identity(snapshot, self.artifacts, destination)
        fund_universe.verify_identity(archived, destination)
        proof = snapshot['identities']['123451']['manager_evidence_proofs'][0]
        proof['appointments'] = [row for row in proof['appointments'] if row['name'] != '乙经理']
        proof['proof_hash'] = fingerprint({key: value for key, value in proof.items() if key != 'proof_hash'})
        with self.assertRaisesRegex(ValueError, 'differ from original'):
            fund_universe.verify_identity(snapshot, self.artifacts)

    table = staticmethod(ManagerEvidenceTests.table)


if __name__ == '__main__':
    unittest.main()
