"""SC06流程准入及独立基础预期；基础校准不冒充完整四selector。"""
import json
import hashlib
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import warnings
from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import scipy
import sklearn
from sklearn.linear_model import ElasticNet

from investment.contracts import ContractError, known, na, now, unknown
from investment.chains.forecast.production_inputs import ProductionInputs, numeric_known
from investment.chains.forecast.production_fits import _predict, _selection
from investment.chains.forecast.production_numerics import encoded, model_audit, standardize
from investment.chains.forecast.numerics import model_check
from investment.chains.forecast.production_scenarios import contiguous, windows, exact_decimal, joint_scenarios
from investment.research.contracts import guard_namespace
from investment.research.controller import ResearchController
from investment.runtime.paths import validate_input_path


SELECTOR_ADMISSION = {
    'forward_fit': {'entry': 'run_sc06.py -> ResearchController -> process_sc06.py -> produce -> validate_sc06.py',
                    'legal_preconditions': 'Same-batch real SC04/SC05 Seal, frozen protocol, physical training/selection/current views and actual two-role Host work',
                    'trigger': 'Full declared candidate grid and a registered training-mean comparator at chronological origins',
                    'source': 'Explicit synthetic source material through the actual upstream chains; never a fabricated Seal',
                    'independent_expectation': 'Hand-derived training means/population variances, exact forward SSE/n, actual saved coefficients and every original fit task'},
    'immature_labels': {'entry': 'Same actual SC06 process and independent validation path',
                        'legal_preconditions': 'Real SC05 permitted label rows retain separate maturity/availability per horizon',
                        'trigger': 'One horizon ends or becomes available after the task cutoff while another has matured',
                        'source': 'Actual upstream synthetic financial observations with declared economic periods',
                        'independent_expectation': 'Only the independently enumerated mature authorized labels enter each training fold'},
    'joint_scenarios': {'entry': 'Same actual SC06 process and independent validation path',
                        'legal_preconditions': 'Real upstream permissions, prior-selected OOS predictions, full U/R and complete consecutive common vectors',
                        'trigger': 'All requested entity/horizon residual coordinates share a historical origin',
                        'source': 'Actual upstream synthetic multi-asset histories and source-supported current economic endpoints',
                        'independent_expectation': 'Unchanged incomplete origins split blocks; one fixed PCG64 draw yields shared selected origins, exact mu+e, C anchor and 1/draws rational probabilities'},
    'member_gap': {'entry': 'Same actual SC06 process and independent validation path',
                   'legal_preconditions': 'Real SC04/SC05 reporting seals retain an unresolved original member or actual risk',
                   'trigger': 'The original U contains a member whose identity/features cannot be established',
                   'source': 'Actual upstream retained source Gap, not a narrowed successful subset',
                   'independent_expectation': 'Original member/risk IDs and Gap impacts remain; full joint scope cannot be silently reduced'}
}

SELECTORS = tuple(SELECTOR_ADMISSION)
ACTUAL_RUNS = []


class SC06ProductionFoundation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ.get('INVESTMENT_SC06_FLOW_ROOT', str(Path.home() / '.investment/test/sc06-producer-foundation-20261020-a'))).resolve()
        boundary = (Path.home() / '.investment/test').resolve()
        if cls.root == boundary or not cls.root.is_relative_to(boundary):
            raise RuntimeError('SC06 foundation artifacts must stay under a strict test-batch root')
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.records = []
        (cls.root / 'selector-admission.json').write_text(json.dumps({'source_type': 'synthetic_mathematical_fixture',
            'complete_selectors_executed': [], 'admissions': SELECTOR_ADMISSION,
            'boundary': 'The following unittest methods are projections/mathematical foundations, not upstream or Controller/Host execution'}, ensure_ascii=False, indent=2), encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        (cls.root / 'foundation-results.json').write_text(json.dumps({'source_type': 'synthetic_mathematical_fixture',
            'records': cls.records, 'complete_selectors_executed': [], 'actual_elasticnet_fit_count': sum(row.get('actual_fit_count', 0) for row in cls.records),
            'versions': {'numpy': np.__version__, 'scipy': scipy.__version__, 'scikit-learn': sklearn.__version__}}, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_real_fit_closed_form_and_independent_f08_foundation(self):
        # 事前闭式预期：x中心化为[-1,1]，第二列恒7；w=(.1-.01)/1.01=9/101，b=1/10。
        x, y = [['9999', '7'], ['10001', '7']], ['0', '.2']
        with localcontext() as context:
            context.prec = 80
            means, variances, scales, z = standardize([[Decimal(v) for v in row] for row in x])
        self.assertEqual(means, [Decimal('10000'), Decimal('7')])
        self.assertEqual(variances, [Decimal('1'), Decimal('0')])
        self.assertEqual(scales, [Decimal('1'), Decimal('1')])
        started = now()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            fitted = ElasticNet(alpha=.02, l1_ratio=.5, fit_intercept=True, precompute=False,
                                max_iter=10000, tol=1e-12, copy_X=True, warm_start=False, positive=False, selection='cyclic')
            record = {'method': self._testMethodName, 'actual_fit_count': 1, 'started_at': started,
                      'finished_at': None, 'status': 'actual_fit_started', 'matrix': x, 'target': y}
            self.records.append(record)
            fitted.fit(np.asarray(z, dtype=np.float64, order='F'), np.asarray(y, dtype=np.float64))
        finished = now()
        record.update(finished_at=finished, status='actual_fit_returned')
        self.assertLessEqual(abs(Fraction(str(fitted.coef_[0])) - Fraction(9, 101)), Fraction.from_float(math.ulp(float(fitted.coef_[0]))) * 4)
        self.assertEqual(float(fitted.coef_[1]), 0)
        self.assertLessEqual(abs(Fraction(str(fitted.intercept_)) - Fraction(1, 10)), Fraction.from_float(math.ulp(float(fitted.intercept_))) * 4)
        # 仅数值模型投影：不构造protocol、上游凭证、StageContext或宿主响应。
        model = {'family': 'elastic_net', 'fit_intercept': True, 'alpha': numeric_known('.02'), 'l1_ratio': numeric_known('.5'),
                 'intercept': encoded(fitted.intercept_), 'feature_statistics': [
                     {'feature_id': 'x' + str(j), 'unit': 'ratio', 'currency': na(), 'mean': encoded(means[j]),
                      'population_variance': encoded(variances[j]), 'scale': encoded(scales[j]), 'zero_variance': variances[j] == 0,
                      'coefficient': encoded(fitted.coef_[j])} for j in range(2)],
                 'solver': {'library': 'scikit-learn', 'version': sklearn.__version__, 'algorithm': 'cyclic_coordinate_descent',
                            'max_iterations': 10000, 'iterations': int(fitted.n_iter_), 'convergence_tolerance': numeric_known('1e-12'),
                            'reported_dual_gap': numeric_known(encoded(fitted.dual_gap_)), 'objective_value': unknown(),
                            'independent_dual_bound': unknown(), 'independent_gap': unknown(), 'maximum_kkt_violation': unknown(),
                            'warnings': [str(row.message) for row in caught], 'numerical_checks': [], 'termination': 'converged'}}
        values = ['10001.1', '9']
        reported = _predict(model, values)
        expected = Fraction(1, 10) + Fraction(9, 101) * Fraction(11, 10)
        self.assertLessEqual(abs(Fraction(reported) - expected), Fraction.from_float(math.ulp(float(reported))) * 4)
        model_audit(model, x, y, [('prediction-foundation', values, reported)])
        independent = model_check(model, [[Decimal(v) for v in row] for row in x], list(map(Decimal, y)),
                                  {'prediction-foundation': (list(map(Decimal, values)), reported)})
        self.assertGreaterEqual(independent['objective'], 0)
        record.update(status='foundation_validated', independent_expected_weight='9/101', independent_expected_intercept='1/10',
                      prediction=reported, model_projection=model, warnings=[str(row.message) for row in caught])

    def test_label_maturity_and_original_task_scope_foundation(self):
        # 方法投影只使用决策器实际访问的字段；未知成员没有伪造entity或Seal。
        inputs = object.__new__(ProductionInputs)
        inputs.ctx = SimpleNamespace(run_id='foundation', invocation_id='math', attempt_no=1,
                                     input_ref={'fixture': 'original-input'}, method_ref={'fixture': 'frozen-method'})
        inputs.bundle = {'experiment_protocol_ref': {'fixture': 'protocol'}}
        inputs.view_refs = {'training': {'fixture': 'training'}, 'selection': {'fixture': 'selection'}, 'current': {'fixture': 'current'}}
        inputs.protocol = {'origin_rule': {'origins': ['2020-01-03T00:00:00Z'], 'current_origin_at': '2020-01-04T00:00:00Z',
                                          'update_rule': 'expanding_window', 'lookback_observations': na()},
                           'horizons': [{'horizon_id': 'short'}, {'horizon_id': 'long'}],
                           'split_rule': {'folds': [], 'selection_information_cutoff_at': '2020-01-04T00:00:00Z',
                                          'independent_origin_start': '2020-02-01T00:00:00Z'},
                           'purge_embargo_rule': {'dependency_window_days': 0},
                           'model_search_space': {'candidate_order': [{'candidate_id': 'grid', 'alpha': '.02', 'l1_ratio': '.5'}],
                                                  'predictors': [{'feature_id': 'x', 'applicability': {'kind': 'all_origin_forecast_entities'}}]},
                           'baseline_definitions': [{'baseline_id': 'mean', 'family': 'training_mean'}]}
        panel = {'sample_id': 'sample', 'entity_id': 'historical-exit', 'origin_at': '2020-01-01T00:00:00Z',
                 'eligible_for_training': True, 'availability_bound_at': known('2020-01-01T00:00:00Z'),
                 'feature_values': [{'value': known('1'), 'availability_bound_at': known('2020-01-01T00:00:00Z')}]}
        inputs.panels = {'sample': panel}
        inputs.members = [{'member_id': 'member-current', 'entity_id': known('current')}, {'member_id': 'member-unknown', 'entity_id': unknown('Original source identity is missing')}]
        inputs.risks = [{'risk_id': 'held-risk', 'instrument_id': known('held-risk-entity')}]
        base = {'sample_id': 'sample', 'entity_id': 'historical-exit', 'origin_at': panel['origin_at'], 'label_status': 'mature',
                'label_available_at': known('2020-01-02T00:00:00Z'), 'outcome': known('.1')}
        inputs.labels = {'short-label': base | {'label_id': 'short-label', 'horizon_id': 'short', 'outcome_end_at': '2020-01-02T00:00:00Z'},
                         'long-label': base | {'label_id': 'long-label', 'horizon_id': 'long', 'outcome_end_at': '2020-01-05T00:00:00Z', 'label_status': 'immature'}}
        inputs.views = {'training': {'label_ids': ['short-label', 'long-label']}}
        tasks = inputs.tasks()
        self.assertEqual(len(tasks), 3 * 2 * 2 * 2)
        self.assertEqual({row['entity_id'] for row in tasks}, {'historical-exit', 'current', 'held-risk-entity'})
        chosen = {(row['horizon_id'], row['candidate_id']): row for row in tasks if row['entity_id'] == 'historical-exit' and row['origin_at'] == '2020-01-03T00:00:00Z'}
        self.assertEqual(inputs.decisions(chosen[('short', 'grid')])[0]['decision'], 'included')
        self.assertEqual(inputs.decisions(chosen[('long', 'grid')])[0]['decision'], 'immature')
        self.records.append({'method': self._testMethodName, 'task_count': len(tasks), 'original_unresolved_member': 'member-unknown',
                             'independent_short_decision': 'included', 'independent_long_decision': 'immature', 'boundary': 'Method projection, no upstream permission or complete-selector execution'})

    def test_selection_uses_only_then_mature_forward_pairs_foundation(self):
        # 事前预期：Jan3只有l1，A的SSE=0优于B的.04；Jan5两标签，B的SSE=.04优于A的1。
        candidates = [{'candidate_id': 'A', 'family': 'elastic_net'}, {'candidate_id': 'B', 'family': 'elastic_net'},
                      {'candidate_id': 'mean', 'family': 'training_mean'}]
        inputs = SimpleNamespace(protocol={'split_rule': {'selection_information_cutoff_at': '2020-01-05T00:00:00Z'},
                                            'model_search_space': {'candidate_order': [{'candidate_id': 'A'}, {'candidate_id': 'B'}]}},
                                 views={'selection': {'label_ids': ['l1', 'l2']}}, candidates=lambda: candidates,
                                 labels={'l1': {'label_id': 'l1', 'entity_id': 'asset', 'horizon_id': 'h',
                                                'origin_at': '2020-01-01T00:00:00Z', 'label_available_at': known('2020-01-02T00:00:00Z'), 'outcome': known('.2')},
                                         'l2': {'label_id': 'l2', 'entity_id': 'asset', 'horizon_id': 'h',
                                                'origin_at': '2020-01-02T00:00:00Z', 'label_available_at': known('2020-01-04T00:00:00Z'), 'outcome': known('1')}})
        predictions, runs = [], []
        for candidate, values in (('A', ['.2', '0']), ('B', ['.4', '1']), ('mean', ['.3', '.3'])):
            for i, value in enumerate(values):
                identity = candidate + str(i)
                predictions.append({'prediction_id': identity, 'fit_id': identity, 'entity_id': 'asset', 'horizon_id': 'h',
                                    'candidate_id': candidate, 'label_id': known('l' + str(i + 1)), 'prediction': value})
                runs.append({'fit_id': identity, 'fold_id': known('fold' + str(i))})
        early = _selection(inputs, 'asset', 'h', '2020-01-03T00:00:00Z', predictions, runs)
        later = _selection(inputs, 'asset', 'h', '2020-01-05T00:00:00Z', predictions, runs)
        self.assertEqual(early['selected_candidate_id'], known('A'))
        self.assertEqual(later['selected_candidate_id'], known('B'))
        self.assertEqual([row['observation_count'] for row in early['candidate_scores']], [1, 1, 1])
        self.assertEqual([row['observation_count'] for row in later['candidate_scores']], [2, 2, 2])
        self.assertEqual(Fraction(later['candidate_scores'][1]['squared_error_sum']['value']), Fraction(1, 25))
        missing = _selection(inputs, 'asset', 'h', '2020-01-05T00:00:00Z',
                             [row for row in predictions if row['prediction_id'] != 'B1'], runs)
        self.assertEqual(missing['status'], 'insufficient_evidence')
        self.assertEqual(missing['candidate_scores'][1]['selection_label_ids'], ['l1', 'l2'])
        self.records.append({'method': self._testMethodName, 'independent_early_winner': 'A', 'independent_late_winner': 'B',
                             'independent_B_late_SSE': '1/25', 'boundary': 'Method projection only, not full forward_fit selector'})

    def test_same_endpoint_residual_conflict_fails_without_rewriting_inputs_foundation(self):
        # 同一未来端点mu均为0、残差为.1/.2；ScenarioValue无horizon列，必须明确失败。
        origin, cutoff, endpoint = '2020-01-01T00:00:00Z', '2020-01-04T00:00:00Z', '2020-01-05T00:00:00Z'
        protocol = {'information_cutoff_at': cutoff, 'origin_rule': {'origins': [origin]},
                    'horizons': [{'horizon_id': 'h1'}, {'horizon_id': 'h2'}],
                    'scenario_method': {'block_length': 1, 'draws': 1},
                    'randomness_policy': {'seed': 17, 'generator': 'numpy_pcg64',
                                          'algorithm_version': 'seedsequence4_int64_halfopen_vector_v1_numpy_2.5.4'}}
        inputs = SimpleNamespace(protocol=protocol, protocol_ref={'mathematical_projection': 'same_endpoint'},
                                 members=[{'member_id': 'member-a', 'entity_id': known('a')}], risks=[],
                                 ctx=SimpleNamespace(run_id='mathematical-projection', invocation_id='same-endpoint', attempt_no=1))
        fitted = {'current': [{'entity_id': 'a', 'horizon_id': horizon, 'prediction': '0'} for horizon in ('h1', 'h2')]}
        residuals = [{'origin_at': origin, 'entity_id': 'a', 'horizon_id': horizon, 'residual': value}
                     for horizon, value in (('h1', '.1'), ('h2', '.2'))]
        before = json.dumps([fitted, residuals], sort_keys=True)
        with self.assertRaises(ContractError) as failure:
            joint_scenarios(inputs, fitted, residuals, {'h1': endpoint, 'h2': endpoint}, [])
        self.assertEqual((failure.exception.rule, failure.exception.pointer, failure.exception.code),
                         ('F13', '/scenarios/same_endpoint', 'unsupported_output'))
        self.assertIn('draw=0; origin=' + origin, str(failure.exception))
        self.assertIn('horizons=h1,h2; returns=1/10,1/5', str(failure.exception))
        self.assertEqual(json.dumps([fitted, residuals], sort_keys=True), before)
        self.records.append({'method': self._testMethodName, 'actual_fit_count': 0,
                             'source_type': 'synthetic_mathematical_fixture', 'independent_failure': 'F13/unsupported_output',
                             'retained_input_returns': ['1/10', '1/5'], 'boundary': 'Mathematical method projection; no legal upstream Context/Seal/Host or business Failure artifact'})

    def test_nonbridging_blocks_and_exact_cumulative_arithmetic_foundation(self):
        origins = ['2020-01-0' + str(i) + 'T00:00:00Z' for i in range(1, 6)]
        parts = contiguous(origins, {origins[0], origins[1], origins[3], origins[4]})
        self.assertEqual(parts, [origins[:2], origins[3:]])
        self.assertEqual(windows(parts, 2), [{'segment_index': 0, 'start_index': 0}, {'segment_index': 1, 'start_index': 0}])
        self.assertEqual(Fraction(exact_decimal(Fraction('.2') + Fraction('-.3'))), Fraction(-1, 10))
        self.assertEqual(Fraction(exact_decimal(Fraction('1e200') + Fraction('.1'))), Fraction('1e200') + Fraction('.1'))
        self.records.append({'method': self._testMethodName, 'independent_segments': parts, 'independent_block_count': 2,
                             'boundary': 'No RNG or complete upstream/controller/Host scenario execution'})


class ActualSC06Flow(unittest.TestCase):
    def _run(self, selector):
        selected = os.environ.get('INVESTMENT_SC06_FLOW_ROOT')
        if not selected:
            self.skipTest('No isolated batch with actual upstream SC04/SC05 seals was supplied')
        root = guard_namespace(Path(selected), 'test')
        path = validate_input_path(root, Path(os.environ.get('SC06_FLOW_CASES', str(root / 'sc06-flow-cases.json'))))
        if not path.exists():
            self.skipTest('No independent legal case file; no synthetic Seal, Context or Host response is created')
        cases = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(cases['schema_version'], 1)
        self.assertIn(cases['source_kind'], ('synthetic_fixture', 'actual_source_snapshot'))
        if selector not in cases['cases']:
            self.skipTest('Actual same-batch legal input is not supplied for ' + selector)
        case = cases['cases'][selector]
        basis_path = validate_input_path(root, root / case['independent_expectation_path'])
        basis_raw = basis_path.read_bytes()
        expectation = json.loads(basis_raw.decode('utf-8'))
        self.assertTrue(expectation['derivation'], 'Independent expected values must be derived before process execution')
        self.assertTrue(expectation['observed_sources'])
        for source in expectation['observed_sources']:
            source_path = validate_input_path(root, root / source['path'])
            self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source['sha256'])
        incoming = validate_input_path(root, root / case['input'])
        controller = ResearchController(root, mode='test', stage_id='SC06')
        result = controller.run(incoming, run_id=case['run_id'], invocation_id=case['invocation_id'])
        ACTUAL_RUNS.append({'selector': selector, 'source_kind': cases['source_kind'],
                            'independent_expectation_sha256': hashlib.sha256(basis_raw).hexdigest(), 'result': result})
        if result['status'] == 'host_work_required':
            self.skipTest('Actual fresh Host work is pending; resume this exact frozen invocation')
        self.assertEqual(result['status'], 'completed', 'Inspect retained actual attempt failures; do not reset the three-attempt budget')
        resolver = controller.resolver
        sealed = resolver.resolve(result['sealed_output_ref']['value'], 'investment.sealed_output')
        candidate = resolver.resolve(sealed['candidate_output_ref'], 'investment.candidate_output')
        receipt = resolver.resolve(sealed['validation_receipt_ref'], 'investment.validation_receipt')
        payload = resolver.resolve(candidate['payload_ref'], 'investment.sc06.forecast_bundle')
        frozen = resolver.resolve(result['input_ref'], 'investment.stage_input')
        self.assertEqual(candidate['input_refs'], frozen['input_refs'])
        self.assertEqual(payload['information_cutoff_at'], frozen['information_cutoff_at'])
        upstream = {row['role']: row['ref'] for row in frozen['input_refs']}
        self.assertEqual(payload['dataset_bundle_ref'], upstream['dataset_bundle_ref'])
        self.assertEqual(payload['instrument_universe_ref'], upstream['instrument_universe_ref'])
        artifacts = {field.removesuffix('_ref'): resolver.resolve(payload[field]) for field in (
            'fitted_model_ref', 'training_audit_ref', 'tuning_audit_ref', 'member_forecast_dispositions_ref',
            'historical_predictions_ref', 'current_predictions_ref', 'residuals_ref', 'current_joint_scenarios_ref')}
        return expectation, payload, artifacts, receipt

    def _training(self, expected, artifacts):
        runs = artifacts['training_audit']['runs']
        actual = {(row['entity_id'], row['horizon_id'], row['origin_at'], row['candidate_id']): row for row in runs}
        expected_keys = {tuple(row[key] for key in ('entity_id', 'horizon_id', 'origin_at', 'candidate_id')) for row in expected['fit_tasks']}
        self.assertEqual(set(actual), expected_keys)
        for row in expected['fit_tasks']:
            saved = actual[tuple(row[key] for key in ('entity_id', 'horizon_id', 'origin_at', 'candidate_id'))]
            self.assertEqual(saved['training_label_ids'], row['training_label_ids'])
            self.assertEqual(saved['rejected_label_ids'], row['rejected_label_ids'])
            self.assertEqual(saved['status'], row['status'])
        self.assertEqual(artifacts['tuning_audit']['actual_model_fit_count'], expected['actual_model_fit_count'])
        self.assertEqual(sum(row['actual_fit_started'] for row in runs), expected['actual_model_fit_count'])

    def test_forward_fit(self):
        expected, payload, artifacts, receipt = self._run('forward_fit')
        self._training(expected, artifacts)
        self.assertIn('hypothesis_research', receipt['allowed_uses'])
        for wanted in expected['model_values']:
            models = [row for row in artifacts['fitted_model']['models'] if all(row[key] == wanted[key]
                      for key in ('entity_id', 'horizon_id', 'origin_at', 'candidate_id'))]
            self.assertEqual(len(models), 1)
            self.assertEqual([Fraction(row['mean']) for row in models[0]['feature_statistics']], list(map(Fraction, wanted['feature_means'])))
            self.assertEqual([Fraction(row['population_variance']) for row in models[0]['feature_statistics']], list(map(Fraction, wanted['feature_variances'])))
            for actual, exact in zip([row['coefficient'] for row in models[0]['feature_statistics']], wanted['coefficients']):
                self.assertLessEqual(abs(Fraction(actual) - Fraction(exact)), Fraction(wanted['coefficient_absolute_bound']))
        winners = [{key: row[key] for key in ('entity_id', 'horizon_id', 'prediction_origin_at', 'selected_candidate_id')}
                   for row in artifacts['tuning_audit']['selections']]
        self.assertEqual(winners, expected['selections'])

    def test_immature_labels(self):
        expected, payload, artifacts, receipt = self._run('immature_labels')
        self._training(expected, artifacts)
        for wanted in expected['label_decisions']:
            matches = [run for run in artifacts['training_audit']['runs'] if all(run[key] == wanted[key]
                       for key in ('entity_id', 'horizon_id', 'origin_at', 'candidate_id'))]
            self.assertEqual(len(matches), 1)
            actual = {row['label_id']: row['decision'] for row in matches[0]['label_decisions']}
            self.assertEqual(actual, wanted['decisions'])

    def test_joint_scenarios(self):
        expected, payload, artifacts, receipt = self._run('joint_scenarios')
        joint = artifacts['current_joint_scenarios']
        self.assertIn('selection_comparison', receipt['allowed_uses'])
        self.assertEqual(joint['required_coordinates'], expected['required_coordinates'])
        self.assertEqual([{key: row[key] for key in ('origin_at', 'status', 'residual_keys', 'missing_coordinates')} for row in joint['vector_inventory']], expected['vector_inventory'])
        self.assertEqual(joint['contiguous_origin_segments'], expected['contiguous_origin_segments'])
        self.assertEqual(joint['sampled_block_indices'], expected['sampled_block_indices'])
        self.assertEqual([row['selected_origin'] for row in joint['scenarios']], expected['selected_origins'])
        self.assertEqual([{key: row[key] for key in ('at', 'horizon_ids', 'kind')} for row in joint['time_grid']], expected['time_grid'])
        endpoints = {row['grid_id']: row['at'] for row in joint['time_grid']}
        actual_values = [[{'entity_id': value['entity_id'], 'at': endpoints[value['grid_id']],
                           'cumulative_return': value['cumulative_return'], 'gross_return': value['gross_return']} for value in row['values']] for row in joint['scenarios']]
        self.assertEqual(actual_values, expected['scenario_values'])
        self.assertEqual((joint['probability_numerator'], joint['probability_denominator']), (1, len(joint['scenarios'])))

    def test_member_gap(self):
        expected, payload, artifacts, receipt = self._run('member_gap')
        dispositions = artifacts['member_forecast_dispositions']['rows']
        joint = artifacts['current_joint_scenarios']
        self.assertEqual([row['member_id'] for row in dispositions], expected['member_ids'])
        self.assertEqual(joint['required_member_ids'], expected['member_ids'])
        self.assertEqual(joint['mandatory_asset_ids'], expected['mandatory_asset_ids'])
        for identity in expected['unknown_member_ids']:
            saved = next(row for row in dispositions if row['member_id'] == identity)
            self.assertEqual(saved['forecast_status'], 'unknown')
            self.assertTrue(saved['affected_uses'])
        for original in expected['retained_gaps']:
            self.assertIn(original, payload['missing_evidence'])
        self.assertNotIn('selection_comparison', receipt['allowed_uses'])


if __name__ == '__main__':
    unittest.main()
