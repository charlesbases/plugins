"""真实ElasticNet/训练均值、全任务审计及当时可得前向选择。"""
import decimal
import warnings
from decimal import Decimal, localcontext
from fractions import Fraction

import numpy as np
import scipy
import sklearn
from sklearn.linear_model import ElasticNet

from investment.contracts import ContractError, instant, is_known, known, na, now, require, unknown
from .production_inputs import gap, identifier, numeric_known
from .production_numerics import encoded, model_audit, standardize


def _predict(model, values):
    weights = np.asarray([float(row['coefficient']) for row in model['feature_statistics']], dtype=np.float64)
    with localcontext() as context:
        context.prec = 80
        z = [(Decimal(value) - Decimal(row['mean'])) / Decimal(row['scale']) for value, row in zip(values, model['feature_statistics'])]
    result = float(np.dot(np.asarray(z, dtype=np.float64), weights) + float(model['intercept']))
    return encoded(result)


def _selection(inputs, entity, horizon, origin, predictions, runs):
    cutoff = min((origin, inputs.protocol['split_rule']['selection_information_cutoff_at']), key=instant)
    labels = [inputs.labels[key] for key in inputs.views['selection']['label_ids']
              if inputs.labels[key]['entity_id'] == entity and inputs.labels[key]['horizon_id'] == horizon
              and is_known(inputs.labels[key]['label_available_at'])
              and instant(inputs.labels[key]['label_available_at']['value']) <= instant(cutoff)
              and instant(inputs.labels[key]['origin_at']) < instant(origin)]
    expected = {row['label_id'] for row in labels}
    by_fit = {row['fit_id']: row for row in runs}
    scores, exact = [], {}
    for candidate in inputs.candidates():
        rows = [row for row in predictions if row['entity_id'] == entity and row['horizon_id'] == horizon
                and row['candidate_id'] == candidate['candidate_id'] and is_known(row['label_id']) and row['label_id']['value'] in expected]
        complete = bool(expected) and len(rows) == len(expected) and {row['label_id']['value'] for row in rows} == expected
        available = complete and all(is_known(inputs.labels[row['label_id']['value']]['outcome']) for row in rows)
        if available:
            total = sum(((Fraction(row['prediction']) - Fraction(inputs.labels[row['label_id']['value']]['outcome']['value'])) ** 2 for row in rows), Fraction(0))
            with localcontext() as context:
                context.prec = 80
                sse, mse = Decimal(total.numerator) / total.denominator, Decimal(total.numerator) / (total.denominator * len(expected))
            exact[candidate['candidate_id']] = total / len(expected)
        scores.append({'candidate_id': candidate['candidate_id'], 'family': candidate['family'], 'entity_id': entity,
                       'horizon_id': horizon, 'selection_at': cutoff, 'selection_label_ids': [row['label_id'] for row in labels],
                       'fold_ids': list(dict.fromkeys(by_fit[row['fit_id']]['fold_id']['value'] for row in rows if is_known(by_fit[row['fit_id']]['fold_id']))),
                       'prediction_ids': [row['prediction_id'] for row in rows],
                       'squared_error_sum': numeric_known(encoded(sse)) if available else unknown('Required forward predictions or mature outcomes are missing'),
                       'observation_count': len(rows), 'mse': numeric_known(encoded(mse)) if available else unknown('Common scoring denominator is incomplete'),
                       'status': 'scored' if available else 'insufficient_evidence',
                       'reason': 'Exact SSE over the unchanged common labels' if available else 'Required forward prediction set is incomplete'})
    grid = [row['candidate_id'] for row in inputs.protocol['model_search_space']['candidate_order']]
    selected = len(exact) == len(inputs.candidates()) and bool(expected)
    winner = min(grid, key=lambda key: (exact[key], grid.index(key))) if selected else None
    return {'entity_id': entity, 'horizon_id': horizon, 'prediction_origin_at': origin, 'information_cutoff_at': cutoff,
            'candidate_scores': scores, 'selected_candidate_id': known(winner) if selected else unknown('Complete prior forward selection is unavailable'),
            'tie_rule': 'exact_mse_then_declared_order', 'status': 'selected' if selected else 'insufficient_evidence',
            'reason': 'Complete prior mature forward SSE/n with exact declared tie order' if selected else 'Full prior forward evidence is incomplete'}


def fit_tasks(inputs, endpoints):
    """endpoints由获准当前源日历层提供；不在拟合器中猜测或补造。"""
    p, ctx = inputs.protocol, inputs.ctx
    candidates = {row['candidate_id']: row for row in inputs.candidates()}
    search = {row['name']: row['values'] for row in p['model_search_space']['numeric_search']}
    require(all(key in search and len(search[key]) == 1 for key in ('max_iterations', 'convergence_tolerance')),
            'F08', '/numeric_search', 'Optimizer budget and tolerance must each be uniquely registered', 'missing_evidence')
    models, runs, history, current_options, gaps = [], [], [], [], []
    matrices = {}
    allowed = {'pit_training', 'selection_comparison'} <= set(inputs.upstream['dataset_bundle_ref'][1]['allowed_uses'])
    started_count = 0
    for task in inputs.tasks():
        decisions = inputs.decisions(task)
        included = [row['label_id'] for row in decisions if row['decision'] == 'included']
        run = task | {'label_decisions': decisions, 'feature_ids': [row['feature_id'] for row in inputs.predictors(task['entity_id'])],
                      'training_label_ids': included, 'rejected_label_ids': [row['label_id'] for row in decisions if row['decision'] != 'included'],
                      'status': 'insufficient_evidence', 'reason': 'No actual fit has started', 'actual_fit_started': False,
                      'started_at': unknown('Fit has not started'), 'finished_at': unknown('Fit has not started'),
                      'failure_code': unknown('Fit has not started'), 'warnings': [], 'numerical_checks': []}
        runs.append(run)
        model = None
        caught = []
        try:
            require(allowed, 'F05', '/upstream_receipt', 'Upstream has not granted training and selection uses', 'use_not_allowed')
            require(included and run['feature_ids'], 'F06', '/training/' + task['fit_id'], 'No complete mature authorized training set', 'missing_evidence')
            require(started_count < p['search_budget']['max_model_fits'], 'F09', '/search_budget', 'Registered model-fit budget exhausted with original tasks retained', 'resource_limit')
            matrix = [inputs.panel_values(inputs.panels[inputs.labels[key]['sample_id']]) for key in included]
            targets = [inputs.labels[key]['outcome']['value'] for key in included]
            with localcontext() as context:
                context.prec = 80
                means, variances, scales, z = standardize([[Decimal(value) for value in row] for row in matrix])
            candidate = candidates[task['candidate_id']]
            baseline = candidate['family'] == 'training_mean'
            # 预处理在训练折内完成；真实启动前保留预算，不因失败退还。
            run['actual_fit_started'], run['started_at'] = True, known(now())
            started_count += 1
            caught = []
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                if baseline:
                    with localcontext() as context:
                        context.prec = 80
                        intercept = encoded(sum(map(Decimal, targets), Decimal(0)) / len(targets))
                    weights = ['0'] * len(run['feature_ids'])
                    iterations, reported_gap = 0, na('Analytic training mean has no iterative dual-gap report')
                else:
                    estimator = ElasticNet(alpha=float(candidate['alpha']), l1_ratio=float(candidate['l1_ratio']),
                                           fit_intercept=p['model_search_space']['intercept'], precompute=False,
                                           max_iter=int(Decimal(search['max_iterations'][0])), tol=float(search['convergence_tolerance'][0]),
                                           copy_X=True, warm_start=False, positive=False, selection='cyclic')
                    estimator.fit(np.asarray(z, dtype=np.float64, order='F'), np.asarray(targets, dtype=np.float64))
                    weights, intercept = [encoded(value) for value in estimator.coef_], encoded(estimator.intercept_)
                    iterations, reported_gap = int(estimator.n_iter_), numeric_known(encoded(estimator.dual_gap_))
            run['warnings'] = [type(item.message).__name__ + ': ' + str(item.message) for item in caught]
            run['finished_at'] = known(now())
            statistics = [{'feature_id': definition['feature_id'], 'unit': definition['expected_unit']['value'], 'currency': definition['currency'],
                           'mean': encoded(means[j]), 'population_variance': encoded(variances[j]), 'scale': encoded(scales[j]),
                           'zero_variance': variances[j] == 0, 'coefficient': weights[j]}
                          for j, definition in enumerate(inputs.predictors(task['entity_id']))]
            solver = {'library': 'python-decimal' if baseline else 'scikit-learn', 'version': decimal.__version__ if baseline else sklearn.__version__,
                      'algorithm': 'arithmetic_training_mean' if baseline else 'cyclic_coordinate_descent',
                      'max_iterations': 0 if baseline else int(Decimal(search['max_iterations'][0])), 'iterations': iterations,
                      'convergence_tolerance': na('Analytic baseline') if baseline else numeric_known(search['convergence_tolerance'][0]),
                      'reported_dual_gap': reported_gap, 'objective_value': unknown('Calculation pending'), 'independent_dual_bound': na('Analytic baseline'),
                      'independent_gap': na('Analytic baseline'), 'maximum_kkt_violation': na('Analytic baseline'), 'warnings': run['warnings'],
                      'numerical_checks': [], 'termination': 'analytic_baseline' if baseline else 'converged'}
            model = {key: task[key] for key in ('fit_id', 'candidate_id', 'entity_id', 'horizon_id', 'origin_at', 'fit_cutoff_at')}
            model.update(purpose='current' if task['origin_at'] == p['origin_rule']['current_origin_at'] else 'forward_oos',
                         family=candidate['family'], alpha=na('Analytic baseline') if baseline else numeric_known(candidate['alpha']),
                         l1_ratio=na('Analytic baseline') if baseline else numeric_known(candidate['l1_ratio']),
                         fit_intercept=True if baseline else p['model_search_space']['intercept'], intercept=intercept,
                         feature_statistics=statistics, training_label_ids=included,
                         training_sample_ids=[inputs.labels[key]['sample_id'] for key in included],
                         input_view_refs=[inputs.view_refs['training']], target_transform='identity', solver=solver)
            model_audit(model, matrix, targets, [])
            models.append(model)
            matrices[task['fit_id']] = (matrix, targets)
            run.update(status='fitted', reason='Actual fold fit and production optimality audit completed', failure_code=na('Fit succeeded'))
            run['numerical_checks'] = solver['numerical_checks']
        except (ContractError, ValueError, ArithmeticError, RuntimeError) as error:
            if run['actual_fit_started']:
                run['warnings'] = [type(item.message).__name__ + ': ' + str(item.message) for item in caught]
            if run['actual_fit_started'] and not is_known(run['finished_at']):
                run['finished_at'] = known(now())
            if run['actual_fit_started']:
                run['status'] = 'numerical_failure'
            elif not allowed:
                run['status'] = 'upstream_use_denied'
            run['reason'] = str(error)[:2048]
            run['failure_code'] = known(getattr(error, 'code', 'numerical_failure'))
            if model is not None:
                run['numerical_checks'] = model['solver']['numerical_checks']
            gaps.append(gap('F08' if run['actual_fit_started'] else 'F06', '/training_audit/runs/' + str(len(runs) - 1), run['reason']))
            continue
        panels = [row for row in inputs.panels.values() if row['entity_id'] == task['entity_id'] and row['origin_at'] == task['origin_at']]
        require(len(panels) <= 1, 'F10', '/samples', 'Origin/entity has ambiguous authorized prediction samples')
        if not panels:
            gaps.append(gap('F10', '/predictions/' + task['fit_id'], 'Original entity/origin has no permitted prediction sample'))
            continue
        panel = panels[0]
        try:
            values = inputs.panel_values(panel)
            require(is_known(panel['availability_bound_at']) and instant(panel['availability_bound_at']['value']) <= instant(task['origin_at'])
                    and all(is_known(f['availability_bound_at']) and instant(f['availability_bound_at']['value']) <= instant(task['origin_at']) for f in panel['feature_values']),
                    'F10', '/prediction_features', 'Prediction feature availability is unknown or later than its origin', 'missing_evidence')
            labels = [row for row in inputs.labels.values() if row['sample_id'] == panel['sample_id'] and row['horizon_id'] == task['horizon_id']]
            require(len(labels) <= 1, 'F10', '/label_id', 'Prediction target has ambiguous authorized labels')
            current = model['purpose'] == 'current'
            endpoint = endpoints.get(task['horizon_id']) if current else labels[0]['outcome_end_at'] if labels else None
            require(endpoint is not None, 'F10', '/outcome_end_at', 'Target endpoint is unavailable from authorized evidence', 'missing_evidence')
            prediction = {key: task[key] for key in ('entity_id', 'horizon_id', 'origin_at', 'fit_id', 'candidate_id', 'selection_information_cutoff_at')}
            prediction.update(prediction_id=identifier('prediction', [task['fit_id'], panel['sample_id']]), sample_id=panel['sample_id'],
                              label_id=na('Current target is not observed') if current else known(labels[0]['label_id']), outcome_end_at=endpoint,
                              purpose=model['purpose'], prediction=_predict(model, values), unit='ratio', input_version_ids=panel['source_version_ids'])
            prediction['economic_support'] = 'valid' if Decimal(prediction['prediction']) >= -1 else 'below_minus_one'
            (current_options if current else history).append(prediction)
        except (ContractError, ArithmeticError, ValueError) as error:
            gaps.append(gap('F10', '/predictions/' + task['fit_id'], str(error)))
    selections = [_selection(inputs, entity, horizon['horizon_id'], origin, history, runs)
                  for entity in inputs.entities() for horizon in p['horizons']
                  for origin in p['origin_rule']['origins'] + [p['origin_rule']['current_origin_at']]]
    for position, selection in enumerate(selections):
        if selection['status'] != 'selected':
            gaps.append(gap('F09', '/tuning_audit/selections/' + str(position), selection['reason']))
    by_selection = {(row['entity_id'], row['horizon_id'], row['prediction_origin_at']): row for row in selections}
    current = [row for row in current_options if by_selection[(row['entity_id'], row['horizon_id'], row['origin_at'])]['selected_candidate_id'] == known(row['candidate_id'])]
    for model in models:
        prediction_audit = [(row['prediction_id'], inputs.panel_values(inputs.panels[row['sample_id']]), row['prediction']) for row in history + current if row['fit_id'] == model['fit_id']]
        matrix, targets = matrices[model['fit_id']]
        model_audit(model, matrix, targets, prediction_audit)
        next(row for row in runs if row['fit_id'] == model['fit_id'])['numerical_checks'] = model['solver']['numerical_checks']
    return {'models': models, 'runs': runs, 'historical': history, 'current': current, 'selections': selections, 'gaps': gaps,
            'versions': {'numpy': np.__version__, 'scipy': scipy.__version__, 'scikit-learn': sklearn.__version__}}
