"""独立 Decimal 推导：折统计、凸对偶及逐坐标下降；不调用 fit。"""
import math
from decimal import Decimal, localcontext

from investment.contracts import require
from .contracts import number

ZERO = Decimal(0)
ONE = Decimal(1)
EPS = Decimal(2) ** -52


class _Counted:
    """只计本次独立表达式实际加减乘除；比较和精确改符号不算舍入运算。"""

    def __init__(self, value, meter):
        self.value, self.meter = value, meter

    def _value(self, other):
        return other.value if isinstance(other, _Counted) else Decimal(other)

    def __add__(self, other):
        self.meter[0] += 1
        return _Counted(self.value + self._value(other), self.meter)

    __radd__ = __add__

    def __sub__(self, other):
        self.meter[0] += 1
        return _Counted(self.value - self._value(other), self.meter)

    def __rsub__(self, other):
        self.meter[0] += 1
        return _Counted(self._value(other) - self.value, self.meter)

    def __mul__(self, other):
        self.meter[0] += 1
        return _Counted(self.value * self._value(other), self.meter)

    __rmul__ = __mul__

    def __truediv__(self, other):
        self.meter[0] += 1
        return _Counted(self.value / self._value(other), self.meter)

    def __rtruediv__(self, other):
        self.meter[0] += 1
        return _Counted(self._value(other) / self.value, self.meter)

    def __pow__(self, exponent):
        if exponent != 2:
            raise ValueError('Independent arithmetic supports only the explicitly used square')
        return self * self

    def __abs__(self):
        return _Counted(abs(self.value), self.meter)

    def __neg__(self):
        return _Counted(-self.value, self.meter)

    def __bool__(self):
        return bool(self.value)

    def __eq__(self, other):
        return self.value == self._value(other)

    def __lt__(self, other):
        return self.value < self._value(other)

    def __le__(self, other):
        return self.value <= self._value(other)

    def __gt__(self, other):
        return self.value > self._value(other)

    def __ge__(self, other):
        return self.value >= self._value(other)


def _plain(value):
    if isinstance(value, _Counted):
        return value.value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def envelope(terms, operations, conversion_error=ZERO):
    """按本表达式绝对项及实际求值运算数核工程界，不用单位常数替代尺度。"""
    with localcontext() as ctx:
        ctx.prec = 80
        values = [number(value) for value in terms]
        scale = sum(map(abs, values), ZERO)
        conversion = conversion_error
        for value in values:
            converted = float(value)
            require(math.isfinite(converted) and (converted != 0 or value == 0),
                    'F08', '/numerics', 'Overflow or underflow prevents a finite engineering bound', 'missing_evidence')
            conversion += abs(Decimal.from_float(converted) - value)
        product = operations * EPS
        require(product < 1, 'F08', '/numerics', 'Operation envelope is undefined', 'missing_evidence')
        rounding = 16 * product / (1 - product) * scale
        require(scale == 0 or rounding + conversion <= Decimal('1e-8') * scale,
                'F08', '/numerics', 'Engineering envelope exceeds registered relative cap', 'missing_evidence')
        return scale, conversion, rounding


def close(actual, expected, terms, operations, rule='F08', path='/numerics', conversion_error=ZERO):
    scale, conversion, rounding = envelope(terms, operations, conversion_error)
    require(abs(number(actual) - expected) <= conversion + rounding,
            rule, path, 'Value differs from independent calculation beyond the registered envelope')
    return {'value': expected, 'terms': terms, 'operations': operations, 'scale': scale,
            'conversion': conversion, 'rounding': rounding}


def statistics(matrix):
    with localcontext() as ctx:
        ctx.prec = 80
        require(bool(matrix), 'F07', '/training', 'Empty training sample', 'missing_evidence')
        n, p = len(matrix), len(matrix[0])
        require(p > 0 and all(len(row) == p for row in matrix), 'F07', '/features', 'Empty or inconsistent predictor matrix')
        means, variances, scales = [], [], []
        for column in zip(*matrix):
            mean = sum(column, ZERO) / n
            variance = sum(((value - mean) ** 2 for value in column), ZERO) / n
            means.append(mean)
            variances.append(variance)
            scales.append(variance.sqrt() if variance else ONE)
        z = [[(value - means[j]) / scales[j] for j, value in enumerate(row)] for row in matrix]
        return means, variances, scales, z


def binary_value(value):
    converted = float(value)
    require(math.isfinite(converted) and (converted != 0 or value == 0),
            'F08', '/conversion', 'A finite nonunderflowing binary64 reference input is required', 'missing_evidence')
    return Decimal.from_float(converted)


def prediction(row, means, scales, coefficients, intercept):
    with localcontext() as ctx:
        ctx.prec = 80
        terms = [(value - mean) / scale * weight for value, mean, scale, weight in zip(row, means, scales, coefficients)]
        return sum(terms, intercept), terms + [intercept], 4 * len(terms)


def prediction_reference(row, matrix, coefficients, intercept):
    """F08/F10共同按原输入转换政策重建训练统计及预测参考差C。"""
    with localcontext() as ctx:
        ctx.prec = 80
        means, _, scales, _ = statistics(matrix)
        binary_means, _, binary_scales, _ = statistics([[binary_value(value) for value in values] for values in matrix])
        expected, terms, operations = prediction(row, means, scales, coefficients, intercept)
        converted, _, _ = prediction([binary_value(value) for value in row], binary_means, binary_scales,
                                     [binary_value(value) for value in coefficients], binary_value(intercept))
        return expected, terms, operations, abs(converted - expected)


def certificate(z, y, coefficients, intercept, alpha, ratio, fit_intercept):
    """按凸共轭构造可行对偶，Lasso边界先缩放，不依赖库停止标志。"""
    with localcontext() as ctx:
        ctx.prec = 80
        n, p = len(y), len(coefficients)
        require(n > 0 and len(z) == n, 'F08', '/training', 'No independent training target', 'missing_evidence')
        meter = [0]
        y = [_Counted(value, meter) for value in y]
        z = [[_Counted(value, meter) for value in row] for row in z]
        coefficients = [_Counted(value, meter) for value in coefficients]
        intercept, alpha, ratio = (_Counted(value, meter) for value in (intercept, alpha, ratio))
        l1, l2 = alpha * ratio, alpha * (1 - ratio)
        residual = [value - intercept - sum((a * b for a, b in zip(row, coefficients)), ZERO) for row, value in zip(z, y)]
        terms = [value * value / (2 * n) for value in residual]
        terms += [l1 * abs(value) + l2 * value * value / 2 for value in coefficients]
        primal = sum(terms, ZERO)
        theta = [value / n for value in residual]
        if fit_intercept:
            mean_theta = sum(theta, ZERO) / n
            theta = [value - mean_theta for value in theta]
        q = [sum((row[j] * t for row, t in zip(z, theta)), ZERO) for j in range(p)]
        if l2 == 0:
            maximum = max(map(abs, q), default=ZERO)
            factor = min(ONE, l1 / maximum) if maximum else ONE
            theta = [value * factor for value in theta]
            q = [value * factor for value in q]
        linear_dual_terms = [a * b for a, b in zip(y, theta)]
        quadratic_dual_terms = [-n * b * b / 2 for b in theta]
        dual_terms = [a + b for a, b in zip(linear_dual_terms, quadratic_dual_terms)]
        conjugate_terms = []
        if l2:
            conjugate_terms = [-max(abs(value) - l1, ZERO) ** 2 / (2 * l2) for value in q]
            dual_terms += conjugate_terms
        dual = sum(dual_terms, ZERO)
        gradients = [-sum((row[j] * value for row, value in zip(z, residual)), ZERO) / n + l2 * coefficients[j]
                     for j in range(p)]
        violations, improvements = [], []
        for j, (weight, gradient) in enumerate(zip(coefficients, gradients)):
            violations.append(abs(gradient + (l1 if weight > 0 else -l1)) if weight else max(abs(gradient) - l1, ZERO))
            curvature = sum((row[j] ** 2 for row in z), ZERO) / n + l2
            correlation = sum((row[j] * (residual[i] + row[j] * weight) for i, row in enumerate(z)), ZERO) / n
            optimum = (ONE if correlation >= 0 else -ONE) * max(abs(correlation) - l1, ZERO) / curvature if curvature else ZERO
            delta = optimum - weight
            improvement = -gradient * delta - curvature * delta * delta / 2 - l1 * (abs(optimum) - abs(weight))
            improvements.append(max(ZERO, improvement))
        intercept_gradient = sum(residual, ZERO) / n
        if fit_intercept:
            violations.append(abs(intercept_gradient))
        if fit_intercept:
            target_mean = sum(y, ZERO) / n
            solver_y = [value - target_mean for value in y]
        else:
            solver_y = y
        kkt_terms = [abs(row[j] * value) / n for j in range(p) for row, value in zip(z, residual)]
        kkt_terms += [abs(l2 * weight) + l1 for weight in coefficients]
        if fit_intercept:
            kkt_terms += [abs(value) / n for value in residual]
        result = {'objective': primal, 'dual_bound': dual, 'duality_gap': primal - dual,
                'maximum_kkt': max(violations, default=ZERO), 'coordinate_descent_improvement': improvements,
                'intercept_descent_improvement': intercept_gradient ** 2 / 2 if fit_intercept else ZERO,
                'target_second_moment': sum((value ** 2 for value in solver_y), ZERO) / n,
                'terms': terms + linear_dual_terms + quadratic_dual_terms + conjugate_terms, 'kkt_terms': kkt_terms}
        result['operations'] = meter[0]
        return _plain(result)


def model_check(model, matrix, targets, predicted=None):
    """独立重建统计与最优性，返回可审计的数值证据，不拟合模型。"""
    with localcontext() as ctx:
        ctx.prec = 80
        stats = model['feature_statistics']
        means, variances, scales, z = statistics(matrix)
        binary_matrix = [[binary_value(value) for value in row] for row in matrix]
        binary_means, binary_variances, binary_scales, binary_z = statistics(binary_matrix)
        references = {}
        n, p = len(matrix), len(stats)
        require(p == len(means), 'F07', '/features', 'Coefficient dimension differs from frozen predictors')
        for j, row in enumerate(stats):
            column = [value[j] for value in matrix]
            for quantity, field, expected, terms, operations, converted, unit in (
                ('feature_mean', 'mean', means[j], column, n + 1, binary_means[j], row['unit']),
                ('feature_variance', 'population_variance', variances[j], [(value - means[j]) ** 2 for value in column], 3 * n + 1, binary_variances[j], row['unit'] + '_squared'),
                ('feature_scale', 'scale', scales[j], [scales[j]], 1, binary_scales[j], row['unit']),
            ):
                check = close(row[field], expected, terms, operations, 'F07', '/' + field, abs(converted - expected))
                references[(quantity, row['feature_id'])] = (check, number(row[field]), unit)
            require(row['zero_variance'] == (variances[j] == 0), 'F07', '/zero_variance', 'Zero variance is not an exact comparison')
        weights = [number(row['coefficient']) for row in stats]
        intercept = number(model['intercept'])
        solver = model['solver']
        for identity, (values, reported) in (predicted or {}).items():
            expected, terms, operations, conversion = prediction_reference(values, matrix, weights, intercept)
            check = close(reported, expected, terms, operations, 'F08', '/prediction', conversion)
            references[('prediction', identity)] = (check, number(reported), 'ratio')
        if model['family'] == 'training_mean':
            mean = sum(targets, ZERO) / len(targets)
            close(intercept, mean, targets, len(targets) + 1)
            require(all(value == 0 for value in weights) and solver['algorithm'] == 'arithmetic_training_mean'
                    and solver['termination'] == 'analytic_baseline', 'F08', '/baseline', 'Baseline is not the same training-label mean')
            require(model['alpha']['status'] == model['l1_ratio']['status'] == 'not_applicable'
                    and solver['iterations'] == solver['max_iterations'] == 0, 'F08', '/baseline_parameters', 'Analytic baseline borrowed ElasticNet parameters or iterations')
            terms = [(value - mean) ** 2 / (2 * len(targets)) for value in targets]
            objective = sum(terms, ZERO)
            binary_targets = [binary_value(value) for value in targets]
            binary_mean = sum(binary_targets, ZERO) / len(binary_targets)
            binary_objective = sum(((value - binary_mean) ** 2 / (2 * len(targets)) for value in binary_targets), ZERO)
            check = close(known_number(solver['objective_value']), objective, terms, 5 * len(targets) + 1,
                          conversion_error=abs(binary_objective - objective))
            references[('objective', None)] = (check, number(solver['objective_value']['value']), 'ratio_squared')
            audit_records(solver['numerical_checks'], references)
            return {'baseline_mean': mean}
        alpha = number(known_number(model['alpha']))
        ratio = number(known_number(model['l1_ratio']))
        require(alpha > 0 and 0 < ratio <= 1 and (model['fit_intercept'] or intercept == 0), 'F08', '/parameters', 'Invalid ElasticNet optimization parameters')
        result = certificate(z, targets, weights, intercept, alpha, ratio, model['fit_intercept'])
        binary_result = certificate([[binary_value(value) for value in row] for row in binary_z],
                                    [binary_value(value) for value in targets], [binary_value(value) for value in weights],
                                    binary_value(intercept), binary_value(alpha), binary_value(ratio), model['fit_intercept'])
        tolerance = number(known_number(solver['convergence_tolerance']))
        require(tolerance > 0 and solver['algorithm'] == 'cyclic_coordinate_descent'
                and 0 <= solver['iterations'] <= solver['max_iterations'] and solver['termination'] == 'converged',
                'F08', '/solver', 'Solver execution or tolerance is inconsistent')
        _, conversion, rounding = envelope(result['terms'], result['operations'], abs(binary_result['duality_gap'] - result['duality_gap']))
        bound = conversion + rounding
        limit = tolerance * result['target_second_moment']
        reported_gap = number(known_number(solver['reported_dual_gap']))
        require(-bound <= reported_gap <= limit + bound, 'F08', '/reported_dual_gap', 'Saved library gap contradicts its claimed successful termination')
        require(-bound <= result['duality_gap'] <= limit + bound, 'F08', '/duality_gap', 'Independent primal-dual certificate does not meet registered tolerance')
        require(all(value <= limit + bound for value in result['coordinate_descent_improvement'])
                and result['intercept_descent_improvement'] <= limit + bound,
                'F08', '/coordinate_optimality', 'Independent one-coordinate descent exceeds the allowed objective improvement')
        for key, independent in (('objective_value', result['objective']), ('independent_dual_bound', result['dual_bound']),
                                 ('independent_gap', result['duality_gap']), ('maximum_kkt_violation', result['maximum_kkt'])):
            close(known_number(solver[key]), independent, result['terms'] if key != 'maximum_kkt_violation' else result['kkt_terms'], result['operations'])
        for quantity, key in (('objective', 'objective_value'), ('dual_bound', 'independent_dual_bound'), ('duality_gap', 'independent_gap')):
            expected = result[quantity]
            check = close(known_number(solver[key]), expected, result['terms'], result['operations'],
                          conversion_error=abs(binary_result[quantity] - expected))
            references[(quantity, None)] = (check, number(solver[key]['value']), 'ratio_squared')
        for j, row in enumerate(stats):
            expected = result['coordinate_descent_improvement'][j]
            scale, conversion, rounding = envelope(result['terms'], result['operations'], abs(binary_result['coordinate_descent_improvement'][j] - expected))
            references[('coordinate_descent_improvement', row['feature_id'])] = (
                {'value': expected, 'operations': result['operations'], 'scale': scale, 'conversion': conversion, 'rounding': rounding}, None, 'ratio_squared')
        expected = result['intercept_descent_improvement']
        scale, conversion, rounding = envelope(result['terms'], result['operations'], abs(binary_result['intercept_descent_improvement'] - expected))
        references[('intercept_descent_improvement', None)] = (
            {'value': expected, 'operations': result['operations'], 'scale': scale, 'conversion': conversion, 'rounding': rounding}, None, 'ratio_squared')
        audit_records(solver['numerical_checks'], references)
        require(bool(solver['library']) and bool(solver['version']), 'F08', '/solver', 'Actual numerical library identity is absent')
        return result


def audit_records(records, references):
    """核公开参考表达式审计，候选自报k不能放宽任何接受界。"""
    keys = [(row['quantity'], row['coordinate_id']['value'] if row['coordinate_id']['status'] == 'known' else None) for row in records]
    require(len(keys) == len(set(keys)) and set(keys) == set(references), 'F08', '/numerical_checks', 'Numerical audit is incomplete, duplicated or has an unbound quantity/coordinate')
    for key, row in zip(keys, records):
        check, saved, unit = references[key]
        require(row['operation_count'] == check['operations'] and row['unit'] == unit
                and row['coordinate_id']['status'] == ('not_applicable' if key[1] is None else 'known'),
                'F08', '/numerical_checks', 'Audit changed the registered reference-operation count or quantity dimension')
        for field, expected in (('absolute_term_scale', check['scale']), ('conversion_error_bound', check['conversion']), ('roundoff_bound', check['rounding'])):
            actual = number(row[field])
            rounding = Decimal.from_float(math.ulp(float(expected))) if expected else ZERO
            require(abs(actual - expected) <= rounding, 'F08', '/numerical_checks/' + field, 'Audit scale/conversion/roundoff differs from independent reference by more than one representation ulp')
        limit = check['conversion'] + check['rounding']
        require(abs(number(row['independent_value']) - check['value']) <= limit
                and abs(number(row['reported_value']) - (check['value'] if saved is None else saved)) <= limit
                and abs(number(row['reported_value']) - number(row['independent_value'])) <= limit
                and row['status'] == 'within_bound', 'F08', '/numerical_checks/value', 'Audit did not preserve the actual reported value, reference value or bound result')


def known_number(value):
    require(value['status'] == 'known', 'F08', '/number', 'Numerical evidence is unknown', 'missing_evidence')
    return value['value']
