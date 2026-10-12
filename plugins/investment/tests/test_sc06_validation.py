"""SC06 数学基础校准；不是四个完整流程 selector，不伪造上游与宿主。"""
import json
import copy
import os
from pathlib import Path
import unittest
import warnings
from decimal import Decimal, localcontext
from dataclasses import FrozenInstanceError
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import scipy
import sklearn
from sklearn.linear_model import ElasticNet

from investment.contracts import ContractError, digest, instant, known, unknown
from investment.research.contracts import HostWorkItem, HostWorkSummary, ResearchSchemas
from investment.chains.forecast.numerics import certificate, envelope, model_check, statistics
from investment.chains.forecast.scenarios import ALGORITHM_VERSION, block_starts, draw_indices, segments
from investment.chains.forecast.temporal import future_endpoints
from investment.chains.forecast import deterministic as d
from investment.chains.forecast.scenarios import endpoints


class MathematicalCalibration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get('INVESTMENT_SC06_CALIBRATION_ROOT')
        if not value:
            raise RuntimeError('An explicit isolated calibration root is required')
        cls.root = Path(value).resolve()
        parent = (Path.home() / '.investment' / 'test').resolve()
        if cls.root == parent or not cls.root.is_relative_to(parent):
            raise RuntimeError('Calibration root must be a strict descendant of the test namespace')
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.records = []
        cls.schemas = ResearchSchemas(('SC03', 'SC04', 'SC05', 'SC06'))

    @classmethod
    def tearDownClass(cls):
        report = {'scope': 'independent_mathematical_foundation_only',
                  'selectors': {key: 'not_run_requires_real_upstream_and_producer' for key in ('forward_fit', 'immature_labels', 'joint_scenarios', 'member_gap')},
                  'versions': {'numpy': np.__version__, 'scipy': scipy.__version__, 'sklearn': sklearn.__version__}, 'records': cls.records}
        (cls.root / 'mathematical-calibration.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_analytic_primal_dual_kkt(self):
        # 对称两点下先求闭式最小点，再核独立凸共轭；不是库停止标志的复写。
        cases = [
            ('elastic', [['-1'], ['1']], ['0', '.2'], [Fraction(9, 101)], '.1', '.02', '.5', True, Fraction(1, 1010), Fraction(1, 100)),
            ('no_intercept_nonzero_mean', [['-1'], ['1']], ['1', '3'], [Fraction(9, 11)], '0', '.2', '.5', False, Fraction(469, 220), Fraction(5)),
            ('correlated', [['-1', '-1'], ['1', '1']], ['0', '2'], [Fraction(3, 7)] * 2, '1', '.2', '.5', True, Fraction(4, 35), Fraction(1)),
            ('lasso_boundary', [['-1'], ['1']], ['0', '.2'], [Fraction(0)], '.1', '.1', '1', True, Fraction(1, 200), Fraction(1, 100)),
            ('flat_zero', [['-1'], ['1']], ['0', '0'], [Fraction(0)], '0', '.1', '.5', True, Fraction(0), Fraction(0)),
        ]
        with localcontext() as ctx:
            ctx.prec = 80
            for name, x, y, w, b, alpha, ratio, fit_intercept, objective, moment in cases:
                with self.subTest(case=name):
                    cert = certificate([[Decimal(v) for v in row] for row in x], [Decimal(v) for v in y],
                                       [Decimal(v.numerator) / v.denominator for v in w], Decimal(b), Decimal(alpha), Decimal(ratio), fit_intercept)
                    expected = Decimal(objective.numerator) / objective.denominator
                    self.assertLess(abs(cert['objective'] - expected), Decimal('1e-65'))
                    self.assertLess(abs(cert['dual_bound'] - expected), Decimal('1e-65'))
                    self.assertLess(abs(cert['maximum_kkt']), Decimal('1e-65'))
                    self.assertLess(abs(cert['duality_gap']), Decimal('1e-65'))
                    self.assertEqual(cert['target_second_moment'], Decimal(moment.numerator) / moment.denominator)
                    self.records.append({'case': name, 'source': 'synthetic_mathematical_fixture', 'oracle': 'closed_form_convex_minimum',
                                         'expected_objective_fraction': str(objective), 'gap': str(cert['duality_gap']), 'operations': cert['operations']})

    def test_actual_fit_independent_certificate(self):
        # 库只产生待核系数；闭式预期与P/D判据先于实际fit确定。
        cases = [
            ('elastic', [[-1.], [1.]], [0., .2], .02, .5, True, [Fraction(9, 101)], Fraction(1, 10)),
            ('no_intercept', [[-1.], [1.]], [1., 3.], .2, .5, False, [Fraction(9, 11)], Fraction(0)),
            ('correlated', [[-1., -1.], [1., 1.]], [0., 2.], .2, .5, True, [Fraction(3, 7)] * 2, Fraction(1)),
            ('lasso', [[-1.], [1.]], [0., .2], .1, 1., True, [Fraction(0)], Fraction(1, 10)),
            ('zero', [[-1.], [1.]], [0., 0.], .1, .5, True, [Fraction(0)], Fraction(0)),
        ]
        for name, x, y, alpha, ratio, fit_intercept, expected_w, expected_b in cases:
            with self.subTest(case=name), warnings.catch_warnings(record=True) as observed, localcontext() as ctx:
                ctx.prec = 80
                fitted = ElasticNet(alpha=alpha, l1_ratio=ratio, fit_intercept=fit_intercept,
                                    max_iter=100000, tol=1e-12, selection='cyclic', warm_start=False, positive=False).fit(np.array(x), np.array(y))
                for actual, expected in zip(fitted.coef_, expected_w):
                    self.assertLess(abs(Fraction(float(actual)) - expected), Fraction(1, 10**9))
                self.assertLess(abs(Fraction(float(fitted.intercept_)) - expected_b), Fraction(1, 10**12))
                cert = certificate([[Decimal(str(v)) for v in row] for row in x], [Decimal(str(v)) for v in y],
                                   [Decimal(repr(float(v))) for v in fitted.coef_], Decimal(repr(float(fitted.intercept_))),
                                   Decimal(str(alpha)), Decimal(str(ratio)), fit_intercept)
                _, conversion, rounding = envelope(cert['terms'], cert['operations'])
                limit = Decimal('1e-12') * cert['target_second_moment'] + conversion + rounding
                self.assertGreaterEqual(cert['duality_gap'], -conversion - rounding)
                self.assertLessEqual(cert['duality_gap'], limit)
                self.assertTrue(all(v <= limit for v in cert['coordinate_descent_improvement']))
                self.records.append({'case': 'actual_' + name, 'source': 'actual_fit_for_validator_mathematical_calibration_only',
                                     'coefficients': [repr(float(v)) for v in fitted.coef_], 'intercept': repr(float(fitted.intercept_)),
                                     'iterations': int(fitted.n_iter_), 'warnings': [str(v.message) for v in observed],
                                     'gap': str(cert['duality_gap']), 'roundoff': str(rounding), 'conversion': str(conversion)})

    def test_scale_and_nonoptimal_point(self):
        for text in ('.000001', '1', '1000000'):
            with self.subTest(scale=text):
                k = Decimal(text)
                means, variance, scales, z = statistics([[Decimal(0)], [2 * k]])
                self.assertEqual(means, [k]); self.assertEqual(variance, [k * k]); self.assertEqual(scales, [k])
                self.assertEqual(z, [[Decimal(-1)], [Decimal(1)]])
        cert = certificate([[Decimal(-1)], [Decimal(1)]], [Decimal(0), Decimal('.2')], [Decimal(0)],
                           Decimal('.1'), Decimal('.02'), Decimal('.5'), True)
        self.assertGreater(cert['duality_gap'], Decimal('.001'))
        self.assertGreater(cert['coordinate_descent_improvement'][0], Decimal('.001'))
        with self.assertRaises(ContractError):
            envelope([Decimal('1e-400')], 1)

    def test_actual_fit_reference_audit_and_false_operation_count(self):
        # 一维对称样本用简化标量闭式构造审计，不调用validator的证书/包络生成预期。
        with warnings.catch_warnings(record=True) as observed:
            fitted = ElasticNet(alpha=.02, l1_ratio=.5, tol=1e-12, max_iter=100000, selection='cyclic').fit(np.array([[-1.], [1.]]), np.array([0., .2]))
        w = Fraction(repr(float(fitted.coef_[0])))
        b, alpha, ratio = Fraction(1, 10), Fraction(1, 50), Fraction(1, 2)

        def scalar(weight, center, a, mix):
            r, l1, l2 = center - weight, a * mix, a * (1 - mix)
            terms = [r*r/4, r*r/4, l1*abs(weight)+l2*weight*weight/2,
                     Fraction(0), center*r, -r*r/4, -r*r/4, -(r-l1)*(r-l1)/(2*l2)]
            primal = r*r/2+l1*abs(weight)+l2*weight*weight/2
            dual = center*r-r*r/2-(r-l1)*(r-l1)/(2*l2)
            optimum = (center-l1)/(1+l2)
            return {'objective': primal, 'dual_bound': dual, 'duality_gap': primal-dual,
                    'coordinate_descent_improvement': (1+l2)*(weight-optimum)**2/2,
                    'intercept_descent_improvement': Fraction(0)}, terms

        def text(value):
            with localcontext() as ctx:
                ctx.prec = 80
                return str(Decimal(value.numerator)/value.denominator)

        original, terms = scalar(w, b, alpha, ratio)
        converted, _ = scalar(Fraction(float(w)), Fraction(float(b)), Fraction(float(alpha)), Fraction(float(ratio)))
        records = []

        def audit(quantity, coordinate, value, values, operations, unit, converted_value=None):
            absolute = sum(map(abs, values), Fraction(0))
            conversion = sum((abs(Fraction(float(item))-item) for item in values), Fraction(0))
            if converted_value is not None:
                conversion += abs(converted_value-value)
            product = Fraction(operations, 2**52)
            rounding = 16*product/(1-product)*absolute
            records.append({'quantity': quantity, 'coordinate_id': {'status': 'known' if coordinate else 'not_applicable', 'value': coordinate, 'reason': None if coordinate else 'scalar_reference'},
                            'operation_count': operations, 'absolute_term_scale': text(absolute), 'conversion_error_bound': text(conversion),
                            'roundoff_bound': text(rounding), 'independent_value': text(value), 'reported_value': text(value),
                            'unit': unit, 'status': 'within_bound', 'reason': 'independent scalar closed-form calibration'})

        audit('feature_mean', 'x', Fraction(1), [Fraction(0), Fraction(2)], 3, 'ratio')
        audit('feature_variance', 'x', Fraction(1), [Fraction(1), Fraction(1)], 7, 'ratio_squared')
        audit('feature_scale', 'x', Fraction(1), [Fraction(1)], 1, 'ratio')
        # n=2,p=1,fit_intercept=true、L2正的公开整次参考包按逐项基本运算手数为110。
        for quantity, value in original.items():
            coordinate = 'x' if quantity == 'coordinate_descent_improvement' else None
            audit(quantity, coordinate, value, terms, 110, 'ratio_squared', converted[quantity])
        opt = lambda value: {'status': 'known', 'value': value, 'reason': None}
        model = {'family': 'elastic_net', 'alpha': opt('.02'), 'l1_ratio': opt('.5'), 'fit_intercept': True,
                 'intercept': '.1', 'feature_statistics': [{'feature_id': 'x', 'unit': 'ratio', 'currency': {'status': 'not_applicable', 'value': None, 'reason': 'dimensionless'},
                                                          'mean': '1', 'population_variance': '1', 'scale': '1', 'zero_variance': False, 'coefficient': repr(float(fitted.coef_[0]))}],
                 'solver': {'algorithm': 'cyclic_coordinate_descent', 'library': 'scikit-learn', 'version': sklearn.__version__,
                            'max_iterations': 100000, 'iterations': int(fitted.n_iter_), 'convergence_tolerance': opt('1e-12'),
                            'reported_dual_gap': opt(repr(float(fitted.dual_gap_))), 'objective_value': opt(text(original['objective'])),
                            'independent_dual_bound': opt(text(original['dual_bound'])), 'independent_gap': opt(text(original['duality_gap'])),
                            'maximum_kkt_violation': opt(text(abs(Fraction(101,100)*w-Fraction(9,100)))),
                            'termination': 'converged', 'numerical_checks': records, 'warnings': [str(value.message) for value in observed]}}
        model_check(model, [[Decimal(0)], [Decimal(2)]], [Decimal(0), Decimal('.2')])
        wrong = copy.deepcopy(model)
        wrong['solver']['numerical_checks'][0]['operation_count'] += 1
        with self.assertRaises(ContractError):
            model_check(wrong, [[Decimal(0)], [Decimal(2)]], [Decimal(0), Decimal('.2')])
        self.records.append({'case': 'actual_fit_reference_audit', 'source': 'actual_fit_with_independent_closed_scalar_audit',
                             'reference_operations': 110, 'wrong_operation_count': 'rejected', 'schema_status': 'mathematical_parameters_only_not_a_business_artifact',
                             'raw_matrix': [['0'], ['2']], 'fit_matrix': [['-1'], ['1']], 'targets': ['0', '.2'], 'mathematical_model_parameters': model})

    def test_common_blocks_rng_api(self):
        origins = ['2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z', '2020-01-03T00:00:00Z', '2020-01-04T00:00:00Z']
        parts = segments(origins, {origins[0], origins[2], origins[3]})
        self.assertEqual(parts, [[origins[0]], origins[2:]])
        self.assertEqual(block_starts(parts, 2), [{'segment_index': 1, 'start_index': 0}])
        policy = {'generator': 'numpy_pcg64', 'algorithm_version': ALGORITHM_VERSION, 'seed': 42}
        self.assertEqual(draw_indices(policy, 4, 7), [0, 5, 4, 3])
        self.assertEqual(draw_indices(dict(policy, seed=123), 3, 1), [0, 0, 0])
        self.assertEqual(draw_indices(policy, 4, 7), [0, 5, 4, 3])

    def test_registered_candidate_actual_family(self):
        # 合法冻结网格/基线及准确SSE的F09方法投影，不冒充完整上游或Host。
        origin, current = '2026-10-18T00:00:00Z', '2026-10-19T00:00:00Z'
        protocol = {'model_search_space': {'candidate_order': [{'candidate_id': 'grid', 'alpha': '.02', 'l1_ratio': '.5'}]},
                    'baseline_definitions': [{'baseline_id': 'mean', 'family': 'training_mean'}],
                    'search_budget': {'max_model_fits': 8, 'max_host_work_items': 1},
                    'origin_rule': {'origins': [origin], 'current_origin_at': current},
                    'horizons': [{'horizon_id': 'h'}],
                    'split_rule': {'selection_information_cutoff_at': origin}}
        runs = [{'fit_id': candidate + '-' + when, 'candidate_id': candidate, 'entity_id': 'asset-A',
                 'horizon_id': 'h', 'origin_at': when, 'actual_fit_started': when == origin}
                for when in (origin, current) for candidate in ('grid', 'mean')]
        models = [{'fit_id': candidate + '-' + origin, 'candidate_id': candidate, 'family': family}
                  for candidate, family in [('grid', 'elastic_net'), ('mean', 'training_mean')]]
        predictions = [{'prediction_id': candidate, 'fit_id': candidate + '-' + origin,
                        'candidate_id': candidate, 'label_id': known('selection-label'), 'prediction': value}
                       for candidate, value in [('grid', '0'), ('mean', '.1')]]
        scores = [{'candidate_id': candidate, 'family': family, 'entity_id': 'asset-A', 'horizon_id': 'h',
                   'selection_at': origin, 'status': 'scored', 'selection_label_ids': ['selection-label'],
                   'prediction_ids': [candidate], 'observation_count': 1,
                   'squared_error_sum': known(error), 'mse': known(error)}
                  for candidate, family, error in [('grid', 'elastic_net', '0'), ('mean', 'training_mean', '.01')]]
        selection = {'entity_id': 'asset-A', 'horizon_id': 'h', 'prediction_origin_at': current,
                     'information_cutoff_at': origin, 'candidate_scores': scores,
                     'status': 'selected', 'selected_candidate_id': known('grid')}
        artifacts = {'fitted_model': {'models': models}, 'training_audit': {'runs': runs},
                     'tuning_audit': {'actual_model_fit_count': 2, 'actual_candidate_count': 1,
                                      'actual_host_work_item_count': 0, 'selections': [selection]},
                     'historical_predictions': {'rows': predictions}}
        summary = HostWorkSummary('run', 'SC06', 'invocation', 1, '3' * 64, '4' * 64, '5' * 64, ())
        s = SimpleNamespace(protocol=protocol, artifacts=artifacts, ctx=SimpleNamespace(host_work_summary=lambda: summary),
                            labels={'selection-label': {'entity_id': 'asset-A', 'horizon_id': 'h',
                                    'label_available_at': known(origin), 'origin_at': '2026-10-17T00:00:00Z', 'outcome': known('0')}},
                            views={'selection': {'label_ids': ['selection-label']}},
                            panels={'sample': {'entity_id': 'asset-A'}}, members=[], risks=[])
        s.rows = lambda name: s.artifacts[name]['rows']
        d.tuning(s)
        (self.root / 'candidate-family-method-input.json').write_text(json.dumps(
            {'source_type': 'synthetic_mathematical_fixture', 'scope': 'F09_method_projection_only',
             'protocol': protocol, 'artifacts': artifacts}, indent=2), encoding='utf-8')
        models[0]['family'] = 'training_mean'
        with self.assertRaises(ContractError) as error:
            d.tuning(s)
        self.assertEqual(error.exception.pointer, '/score_fit_family')
        models[0]['family'] = 'elastic_net'
        scores[0]['family'] = 'training_mean'
        with self.assertRaises(ContractError):
            d.tuning(s)
        self.records.append({'case': 'registered_candidate_actual_family', 'source': 'F09_method_projection_only',
                             'prior_expected': 'honest mapping accepted; grid ID backed by actual training_mean fit rejected',
                             'fit_count': 0, 'host_execution': 'not_run'})

    def test_prediction_binary_conversion_policy(self):
        # 先给闭式9/101、1/10与原C定义；库只产生一个真实待核拟合。
        with warnings.catch_warnings(record=True) as observed:
            fitted = ElasticNet(alpha=.02, l1_ratio=.5, tol=1e-12, max_iter=100000,
                                selection='cyclic').fit(np.array([[-1.], [1.]]), np.array([0., .2]))
        weight, intercept = repr(float(fitted.coef_[0])), repr(float(fitted.intercept_))
        self.assertLess(abs(Fraction(weight) - Fraction(9, 101)), Fraction(1, 10**12))
        self.assertLess(abs(Fraction(intercept) - Fraction(1, 10)), Fraction(1, 10**12))
        value = repr(float(fitted.predict(np.array([[(10001.1 - 10000.) / 1.]]))[0]))
        exact = (Fraction('10001.1') - 10000) * Fraction(weight) + Fraction(intercept)
        converted = (Fraction(float('10001.1')) - 10000) * Fraction(float(weight)) + Fraction(float(intercept))
        terms = [(Fraction('10001.1') - 10000) * Fraction(weight), Fraction(intercept)]
        conversion = sum((abs(Fraction(float(term)) - term) for term in terms), Fraction(0))
        product = Fraction(4, 2**52)
        rounding = 16 * product / (1 - product) * sum(map(abs, terms), Fraction(0))
        self.assertGreater(abs(Fraction(value) - exact), conversion + rounding)
        self.assertLessEqual(abs(Fraction(value) - exact), conversion + abs(converted - exact) + rounding)
        self.assertLessEqual(conversion + abs(converted - exact) + rounding,
                             Fraction(1, 10**8) * sum(map(abs, terms), Fraction(0)))
        origin = '2026-10-19T00:00:00Z'
        model = {'fit_id': 'fit-grid', 'candidate_id': 'grid', 'family': 'elastic_net', 'entity_id': 'asset-A',
                 'horizon_id': 'h', 'origin_at': origin, 'purpose': 'current', 'training_label_ids': ['l1', 'l2'],
                 'training_sample_ids': ['s1', 's2'], 'feature_statistics': [{'coefficient': weight}], 'intercept': intercept}
        row = {'prediction_id': 'p', 'fit_id': 'fit-grid', 'candidate_id': 'grid', 'entity_id': 'asset-A',
               'horizon_id': 'h', 'origin_at': origin, 'purpose': 'current', 'sample_id': 'current',
               'prediction': value, 'input_version_ids': ['current-version'], 'selection_information_cutoff_at': origin,
               'label_id': unknown('Future outcome is unavailable')}
        panels = {key: {'entity_id': 'asset-A', 'origin_at': origin, 'feature_values': [{'value': known(x)}],
                        'source_version_ids': ['current-version']} for key, x in [('s1', '9999'), ('s2', '10001'), ('current', '10001.1')]}
        artifacts = {'fitted_model': {'models': [model]}, 'historical_predictions': {'rows': []},
                     'current_predictions': {'rows': [row]}, 'tuning_audit': {'selections': [
                         {'entity_id': 'asset-A', 'horizon_id': 'h', 'prediction_origin_at': origin, 'status': 'selected',
                          'selected_candidate_id': known('grid'), 'information_cutoff_at': origin}]}}
        protocol = {'model_search_space': {'candidate_order': [{'candidate_id': 'grid', 'alpha': '.02', 'l1_ratio': '.5'}]},
                    'baseline_definitions': [{'baseline_id': 'mean', 'family': 'training_mean'}]}
        s = SimpleNamespace(artifacts=artifacts, protocol=protocol, panels=panels,
                            labels={'l1': {'sample_id': 's1', 'outcome': known('0')}, 'l2': {'sample_id': 's2', 'outcome': known('.2')}})
        s.rows = lambda name: s.artifacts[name]['rows']
        (self.root / 'prediction-conversion-method-input.json').write_text(json.dumps(
            {'source_type': 'synthetic_mathematical_fixture', 'scope': 'F10_method_projection_only',
             'protocol': protocol, 'artifacts': artifacts, 'panels': panels, 'labels': s.labels}, indent=2), encoding='utf-8')
        d.predictions(s)
        row['prediction'] = str(Decimal(value) + Decimal('1e-8'))
        with self.assertRaises(ContractError) as error:
            d.predictions(s)
        self.assertEqual(error.exception.pointer, '/prediction_value')
        self.records.append({'case': 'prediction_binary_conversion_policy', 'source': 'one_actual_fit_F10_method_projection_only',
                             'raw_matrix': [['9999'], ['10001']], 'current': '10001.1', 'coefficient': weight,
                             'intercept': intercept, 'actual_prediction': value, 'exact_prediction_fraction': str(exact),
                             'reference_conversion_fraction': str(abs(converted - exact)), 'roundoff_fraction': str(rounding),
                             'warnings': [str(item.message) for item in observed], 'fit_count': 1})

    def test_current_mean_path_before_residuals(self):
        # 合法exact期限下的均值/残差候选反例；不造上游Seal、未来标签或新金融结果。
        cutoff = '2026-10-19T00:00:00Z'
        for name, means, errors, shared, rejected in [
                ('absorbed', ['-1', '-1'], ['1', '1'], False, False),
                ('revival-hidden', ['-1', '0'], ['1', '0'], False, True),
                ('same-endpoint', ['.1', '.1'], ['-.1', '-.1'], True, False),
                ('conflict-hidden', ['.1', '.2'], ['-.1', '-.2'], True, True)]:
            with self.subTest(case=name):
                horizons = [{'horizon_id': 'h' + str(i), 'length': 1 if shared else i + 1,
                             'unit': 'calendar_day', 'calendar_id': 'calendar', 'end_rule': 'exact_calendar_boundary'} for i in range(2)]
                predictions = [{'entity_id': 'asset-A', 'horizon_id': h['horizon_id'], 'origin_at': cutoff,
                                'outcome_end_at': '2026-10-' + ('20' if h['length'] == 1 else '21') + 'T00:00:00Z',
                                'prediction': mean, 'economic_support': 'valid'} for h, mean in zip(horizons, means)]
                grid = [{'grid_id': 'anchor', 'at': cutoff, 'kind': 'anchor', 'horizon_ids': []},
                        {'grid_id': 'g1', 'at': '2026-10-20T00:00:00Z', 'kind': 'horizon_endpoint',
                         'horizon_ids': ['h0', 'h1'] if shared else ['h0']}]
                if not shared:
                    grid.append({'grid_id': 'g2', 'at': '2026-10-21T00:00:00Z', 'kind': 'horizon_endpoint', 'horizon_ids': ['h1']})
                joint = {'time_grid': grid, 'status': 'constructed', 'scenarios': [
                    {'selected_origin': '2026-10-17T00:00:00Z', 'economic_status': 'valid', 'values': [
                        {'entity_id': 'asset-A', 'grid_id': point['grid_id'], 'cumulative_return': '0', 'gross_return': '1'} for point in grid]}]}
                artifacts = {'current_joint_scenarios': joint, 'current_predictions': {'rows': predictions},
                             'residuals': {'rows': [{'origin_at': '2026-10-17T00:00:00Z', 'entity_id': 'asset-A',
                                                    'horizon_id': h['horizon_id'], 'residual': error} for h, error in zip(horizons, errors)]},
                             'member_forecast_dispositions': {'rows': [{'member_id': 'm', 'forecast_status': 'ready'}]}}
                s = SimpleNamespace(artifacts=artifacts, protocol={'horizons': horizons}, payload={'information_cutoff_at': cutoff},
                                    calendars={'current': []}, members=[{'member_id': 'm', 'entity_id': known('asset-A')}], risks=[])
                s.rows = lambda key: s.artifacts[key]['rows']
                (self.root / ('mean-path-' + name + '.json')).write_text(json.dumps(
                    {'source_type': 'synthetic_mathematical_fixture', 'scope': 'F13_method_projection_only',
                     'horizons': horizons, 'artifacts': artifacts}, indent=2), encoding='utf-8')
                before = copy.deepcopy(artifacts)
                if rejected:
                    with self.assertRaises(ContractError) as error:
                        endpoints(s)
                    self.assertEqual(error.exception.pointer, '/current_mean_path' if not shared else '/current_mean_same_endpoint')
                else:
                    endpoints(s)
                self.assertEqual(artifacts, before)
        self.records.append({'case': 'current_mean_path_before_residuals', 'source': 'F13_method_projection_only',
                             'prior_expected': 'absorption and equal-endpoint controls accepted; residual-hidden mean revival/conflict rejected', 'fit_count': 0})

    def _calendar_rows(self, name, entity='asset-A', changes=None, omit=(), calendar='calendar'):
        # 真实落本批模拟源；仅核字段/时间数学，不冒充SC05封存或金融日历事实。
        source_rows = []
        changes = changes or {}
        for day, trading in [('2026-10-20', True), ('2026-10-21', True), ('2026-10-22', True),
                             ('2026-10-23', True), ('2026-10-24', False), ('2026-10-25', False), ('2026-10-26', True)]:
            if day in omit:
                continue
            change = changes.get(day, {})
            zone = change.get('timezone', 'Asia/Shanghai')
            offset = '+08:00' if zone == 'Asia/Shanghai' else '+00:00'
            source_rows.append({'date': day, 'trading': trading, 'trading_status': 'known', 'schedule_status': 'known',
                                'lower_bound': day + 'T00:00:00' + offset, 'upper_bound': day + 'T23:59:59.999999' + offset,
                                'timezone': zone, 'precision': 'date', **change})
        source = {'source_type': 'synthetic_fixture', 'entity': entity, 'calendar': calendar,
                  'available_at': '2026-10-19T00:00:00Z', 'rows': source_rows}
        raw = json.dumps(source, ensure_ascii=False, indent=2).encode('utf-8')
        (self.root / (name + '.json')).write_bytes(raw)
        reference = {'artifact_id': name, 'schema_id': 'investment.research_text', 'schema_version': 1,
                     'media_type': 'text/plain', 'uri': 'fixture://sc06-calendar/' + name,
                     'sha256': digest(raw), 'byte_size': len(raw)}
        result = []
        for value in source_rows:
            period = known({key: value[key] for key in ('lower_bound', 'upper_bound', 'precision', 'timezone')} | {'basis_ref': reference})
            if value['schedule_status'] != 'known':
                period = {'status': value['schedule_status'], 'value': None, 'reason': 'Synthetic source explicitly declares this schedule status'}
            row = {'calendar_id': calendar, 'date': value['date'], 'entity_id': entity,
                   'is_trading_day': known(value['trading']) if value['trading_status'] == 'known' else unknown('Synthetic source has no trading flag'),
                   'scheduled_valuation_period': period, 'order_cutoff_at': unknown('Not supplied'),
                   'confirmation_day': unknown('Not supplied'), 'settlement_day': unknown('Not supplied'),
                   'available_time': known({'lower_bound': source['available_at'], 'upper_bound': source['available_at'],
                                            'precision': 'instant', 'timezone': 'UTC', 'basis_ref': reference}),
                   'calendar_version_id': name + '-' + value['date'],
                   'citations': [{'raw_ref': reference, 'locator': 'json:/rows', 'quote': value['date'],
                                  'claim': 'Synthetic declared calendar values for time foundation calibration only'}]}
            self.schemas.validate('investment.sc05.calendar_row', row)
            result.append(row)
        return result

    def test_calendar_endpoint_foundation(self):
        # 预期来自事前列出的日历及UTC+8换算，未取自待核函数。
        rows = self._calendar_rows('calendar-known-A') + self._calendar_rows('calendar-known-B', 'asset-B')
        trade = {'horizon_id': 'trade', 'length': 2, 'unit': 'trading_day', 'calendar_id': 'calendar', 'end_rule': 'next_documented_trading_boundary'}
        next_day = dict(trade, horizon_id='next', unit='calendar_day')
        exact = dict(next_day, horizon_id='exact', end_rule='exact_calendar_boundary')
        expected = {'trade': instant('2026-10-21T15:59:59.999999Z'), 'next': instant('2026-10-21T15:59:59.999999Z'),
                    'exact': instant('2026-10-21T11:00:00Z')}
        self.assertEqual(future_endpoints(rows, [trade, next_day, exact], {'asset-A', 'asset-B'}, instant('2026-10-19T11:00:00Z')), expected)
        self.assertEqual(future_endpoints(rows, [dict(next_day, length=1)], {'asset-A'}, instant('2026-10-23T11:00:00Z')),
                         {'next': instant('2026-10-26T15:59:59.999999Z')})
        single = self._calendar_rows('calendar-instant', changes={'2026-10-21': {'precision': 'instant',
                                    'lower_bound': '2026-10-21T15:00:00+08:00', 'upper_bound': '2026-10-21T15:00:00+08:00'}})
        self.assertEqual(future_endpoints(single, [trade], {'asset-A'}, instant('2026-10-19T11:00:00Z')),
                         {'trade': instant('2026-10-21T07:00:00Z')})
        self.records.append({'case': 'calendar_endpoint_foundation', 'source': 'synthetic_calendar_time_mathematics_only',
                             'expected_trade_date_upper': '2026-10-21T15:59:59.999999Z', 'expected_exact': '2026-10-21T11:00:00Z',
                             'expected_next_after_synthetic_weekend': '2026-10-26T15:59:59.999999Z', 'expected_instant': '2026-10-21T07:00:00Z'})

    def test_calendar_gaps_preserve_horizon(self):
        horizon = {'horizon_id': 'trade', 'length': 2, 'unit': 'trading_day', 'calendar_id': 'calendar', 'end_rule': 'next_documented_trading_boundary'}
        cases = [('missing-day', {}, ('2026-10-20',)),
                 ('unknown-trading', {'2026-10-20': {'trading_status': 'unknown'}}, ()),
                 ('unknown-target', {'2026-10-21': {'schedule_status': 'unknown'}}, ()),
                 ('no-valuation-target', {'2026-10-21': {'schedule_status': 'not_applicable'}}, ())]
        for name, changes, omit in cases:
            with self.subTest(case=name):
                rows = self._calendar_rows('calendar-' + name, changes=changes, omit=omit)
                before = copy.deepcopy(rows)
                with self.assertRaises(ContractError) as error:
                    future_endpoints(rows, [horizon], {'asset-A'}, instant('2026-10-19T11:00:00Z'))
                self.assertEqual(error.exception.code, 'missing_evidence')
                self.assertEqual(rows, before)
        self.records.append({'case': 'calendar_gaps_preserve_horizon', 'source': 'synthetic_calendar_time_mathematics_only',
                             'prior_expected': 'all four retain missing_evidence; no row mutation or later-date substitution'})

    def test_calendar_common_period_scope(self):
        horizon = {'horizon_id': 'trade', 'length': 2, 'unit': 'trading_day', 'calendar_id': 'calendar', 'end_rule': 'next_documented_trading_boundary'}
        rows = self._calendar_rows('calendar-common-A')
        for name, change in [('different-lower', {'precision': 'interval', 'lower_bound': '2026-10-21T09:00:00+08:00'}),
                             ('different-precision', {'precision': 'interval'})]:
            with self.subTest(case=name):
                other = self._calendar_rows('calendar-' + name, 'asset-B', {'2026-10-21': change})
                with self.assertRaises(ContractError) as error:
                    future_endpoints(rows + other, [horizon], {'asset-A', 'asset-B'}, instant('2026-10-19T11:00:00Z'))
                self.assertEqual(error.exception.code, 'missing_evidence')
        with self.assertRaises(ContractError) as error:
            future_endpoints(rows, [horizon], {'asset-A', 'asset-B'}, instant('2026-10-19T11:00:00Z'))
        self.assertEqual(error.exception.code, 'missing_evidence')
        one = dict(horizon, length=1)
        exact = dict(one, horizon_id='exact', unit='calendar_day', end_rule='exact_calendar_boundary')
        cutoff = instant('2026-10-19T15:59:59.999999Z')
        expected = {'trade': instant('2026-10-20T15:59:59.999999Z'), 'exact': instant('2026-10-20T15:59:59.999999Z')}
        for order in ([one, exact], [exact, one]):
            self.assertEqual(future_endpoints(rows, order, {'asset-A'}, cutoff), expected)
        self.assertEqual(future_endpoints([], [exact], set(), cutoff), {'exact': expected['exact']})
        other = self._calendar_rows('calendar-other-period', changes={'2026-10-21': {'precision': 'interval'}}, calendar='other')
        with self.assertRaises(ContractError) as error:
            future_endpoints(rows + other, [horizon, dict(horizon, horizon_id='other', calendar_id='other')], {'asset-A'}, instant('2026-10-19T11:00:00Z'))
        self.assertEqual(error.exception.code, 'missing_evidence')
        self.records.append({'case': 'calendar_common_period_scope', 'source': 'synthetic_calendar_time_mathematics_only',
                             'prior_expected': 'same upper with different lower/precision, missing required entity, or conflicting horizon periods all insufficient'})

    def test_calendar_source_clock_consensus(self):
        horizon = {'horizon_id': 'trade', 'length': 1, 'unit': 'trading_day', 'calendar_id': 'calendar', 'end_rule': 'next_documented_trading_boundary'}
        changes = {'2026-10-20': {'timezone': 'UTC'}}
        rows = self._calendar_rows('calendar-two-clocks', changes=changes)
        self.assertEqual(future_endpoints(rows, [dict(horizon, length=2)], {'asset-A'}, instant('2026-10-19T11:00:00Z')),
                         {'trade': instant('2026-10-21T15:59:59.999999Z')})
        with self.assertRaises(ContractError) as error:
            future_endpoints(rows, [horizon], {'asset-A'}, instant('2026-10-19T22:00:00Z'))
        self.assertEqual(error.exception.code, 'missing_evidence')
        rows = self._calendar_rows('calendar-two-clocks-na', changes={**changes, '2026-10-21': {'schedule_status': 'not_applicable'}})
        with self.assertRaises(ContractError) as error:
            future_endpoints(rows, [horizon], {'asset-A'}, instant('2026-10-19T22:00:00Z'))
        self.assertEqual(error.exception.code, 'missing_evidence')
        self.records.append({'case': 'calendar_source_clock_consensus', 'source': 'synthetic_calendar_time_mathematics_only',
                             'prior_expected': 'both source clocks accepted when target agrees; differing local origin dates or one NA target remain insufficient'})

    def test_host_work_summary_interface(self):
        # 仅校准公共控制头值对象；不是合法业务上下文、模型响应或封存凭证。
        coordinates = [('producer', 'forecast_assumptions', 'a', 'completed'),
                       ('producer', 'forecast_assumptions', 'b', 'failed'),
                       ('producer', 'forecast_assumptions', 'c', 'requested'),
                       ('validator', 'forecast_review', 'main', 'completed'),
                       ('validator', 'forecast_review', 'extra', 'requested'),
                       ('producer', 'forecast_assumptions', 'd', 'completed')]
        items = tuple(HostWorkItem(role, step, key, 'request-' + str(index), '1' * 64, state,
                                   None if state == 'requested' else '2' * 64)
                      for index, (role, step, key, state) in enumerate(coordinates))
        summary = HostWorkSummary('run', 'SC06', 'invocation', 1, '3' * 64, '4' * 64, '5' * 64, items)
        self.assertEqual(summary.completed_count('producer'), 2)
        self.assertEqual(summary.registered_count(), 6)
        self.assertEqual(summary.registered_count('producer'), 4)
        self.assertEqual(summary.completed_count(), 3)
        self.assertEqual(summary.pending_count(), 2)
        self.assertEqual(summary.failed_count(), 1)
        self.assertTrue(summary.contains('validator', 'forecast_review', 'main'))
        self.assertFalse(summary.contains('validator', 'forecast_review', 'new'))
        with self.assertRaises(FrozenInstanceError):
            summary.attempt_no = 2
        with self.assertRaises(FrozenInstanceError):
            items[0].state = 'requested'
        self.records.append({'case': 'host_work_summary_interface', 'source': 'public_immutable_control_header_values_only',
                             'producer_completed': 2, 'both_roles_registered_including_pending_failed': 6,
                             'not_executed': 'controller_factory_or_business_host_request_or_response'})


if __name__ == '__main__':
    unittest.main()
