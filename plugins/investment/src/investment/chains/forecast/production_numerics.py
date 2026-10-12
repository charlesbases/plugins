"""生产端目标、凸对偶、转换及公开参考运算审计；与验证器分别实现。"""
import math
from decimal import Decimal, localcontext

from investment.contracts import require, known, na
from .production_inputs import numeric_known

ZERO, ONE = Decimal(0), Decimal(1)


def encoded(value):
    result = Decimal(str(value))
    require(result.is_finite() and math.isfinite(float(result)), 'F08', '/number', 'Nonfinite model calculation', 'missing_evidence')
    return '0' if result == 0 else str(result)


def binary(value):
    result = float(value)
    require(math.isfinite(result) and (result != 0 or value == 0), 'F08', '/conversion', 'Overflow or underflow prevents model calculation', 'missing_evidence')
    return Decimal.from_float(result)


def standardize(matrix):
    n = len(matrix)
    means = [sum(column, ZERO) / n for column in zip(*matrix)]
    variances = [sum(((row[j] - means[j]) ** 2 for row in matrix), ZERO) / n for j in range(len(means))]
    scales = [variance.sqrt() if variance else ONE for variance in variances]
    z = [[(value - means[j]) / scales[j] for j, value in enumerate(row)] for row in matrix]
    return means, variances, scales, z


class Scalar:
    """运算账本只描述登记参考表达式，不冒称BLAS或solver的物理操作。"""

    def __init__(self, value, ledger):
        self.value, self.ledger = Decimal(value), ledger

    def unwrap(self, other):
        return other.value if isinstance(other, Scalar) else Decimal(other)

    def apply(self, other, action):
        self.ledger[0] += 1
        return Scalar(action(self.value, self.unwrap(other)), self.ledger)

    def __add__(self, other): return self.apply(other, lambda a, b: a + b)
    __radd__ = __add__
    def __sub__(self, other): return self.apply(other, lambda a, b: a - b)
    def __rsub__(self, other): return self.apply(other, lambda a, b: b - a)
    def __mul__(self, other): return self.apply(other, lambda a, b: a * b)
    __rmul__ = __mul__
    def __truediv__(self, other): return self.apply(other, lambda a, b: a / b)
    def __rtruediv__(self, other): return self.apply(other, lambda a, b: b / a)
    def __pow__(self, power):
        require(power == 2, 'F08', '/reference', 'Reference power must be a single square')
        return self * self
    def __abs__(self): return Scalar(abs(self.value), self.ledger)
    def __neg__(self): return Scalar(-self.value, self.ledger)
    def __bool__(self): return bool(self.value)
    def __eq__(self, other): return self.value == self.unwrap(other)
    def __lt__(self, other): return self.value < self.unwrap(other)
    def __le__(self, other): return self.value <= self.unwrap(other)
    def __gt__(self, other): return self.value > self.unwrap(other)
    def __ge__(self, other): return self.value >= self.unwrap(other)


def primal_dual(z, targets, weights, intercept, alpha, ratio, fit_intercept):
    # 逐项求值次序属于reference_certificate_v1；必须同时保留Lasso可行缩放。
    ledger, n, p = [0], len(targets), len(weights)
    y = [Scalar(value, ledger) for value in targets]
    z = [[Scalar(value, ledger) for value in row] for row in z]
    w = [Scalar(value, ledger) for value in weights]
    b, alpha, ratio = [Scalar(value, ledger) for value in (intercept, alpha, ratio)]
    l1, l2 = alpha * ratio, alpha * (1 - ratio)
    residual = [value - b - sum((a * weight for a, weight in zip(row, w)), ZERO) for row, value in zip(z, y)]
    terms = [value * value / (2 * n) for value in residual]
    terms += [l1 * abs(value) + l2 * value * value / 2 for value in w]
    primal = sum(terms, ZERO)
    theta = [value / n for value in residual]
    if fit_intercept:
        mean_theta = sum(theta, ZERO) / n
        theta = [value - mean_theta for value in theta]
    q = [sum((row[j] * value for row, value in zip(z, theta)), ZERO) for j in range(p)]
    if l2 == 0:
        maximum = max(map(abs, q), default=ZERO)
        factor = min(ONE, l1 / maximum) if maximum else ONE
        theta, q = [value * factor for value in theta], [value * factor for value in q]
    linear = [a * value for a, value in zip(y, theta)]
    quadratic = [-n * value * value / 2 for value in theta]
    dual_terms = [a + value for a, value in zip(linear, quadratic)]
    conjugates = [-max(abs(value) - l1, ZERO) ** 2 / (2 * l2) for value in q] if l2 else []
    dual_terms += conjugates
    dual = sum(dual_terms, ZERO)
    gradients = [-sum((row[j] * value for row, value in zip(z, residual)), ZERO) / n + l2 * w[j] for j in range(p)]
    violations, improvements = [], []
    for j, (weight, gradient) in enumerate(zip(w, gradients)):
        violations.append(abs(gradient + (l1 if weight > 0 else -l1)) if weight else max(abs(gradient) - l1, ZERO))
        curvature = sum((row[j] ** 2 for row in z), ZERO) / n + l2
        correlation = sum((row[j] * (residual[i] + row[j] * weight) for i, row in enumerate(z)), ZERO) / n
        optimum = (ONE if correlation >= 0 else -ONE) * max(abs(correlation) - l1, ZERO) / curvature if curvature else ZERO
        delta = optimum - weight
        improvements.append(max(ZERO, -gradient * delta - curvature * delta * delta / 2 - l1 * (abs(optimum) - abs(weight))))
    intercept_gradient = sum(residual, ZERO) / n
    if fit_intercept:
        violations.append(abs(intercept_gradient))
        mean_target = sum(y, ZERO) / n
        solver_target = [value - mean_target for value in y]
    else:
        solver_target = y
    kkt_terms = [abs(row[j] * value) / n for j in range(p) for row, value in zip(z, residual)]
    kkt_terms += [abs(l2 * weight) + l1 for weight in w]
    if fit_intercept:
        kkt_terms += [abs(value) / n for value in residual]
    result = {'objective': primal, 'dual_bound': dual, 'duality_gap': primal - dual,
              'maximum_kkt': max(violations, default=ZERO), 'coordinate_descent_improvement': improvements,
              'intercept_descent_improvement': intercept_gradient ** 2 / 2 if fit_intercept else ZERO,
              'target_second_moment': sum((value ** 2 for value in solver_target), ZERO) / n,
              'terms': terms + linear + quadratic + conjugates, 'kkt_terms': kkt_terms}
    def plain(value):
        if isinstance(value, Scalar): return value.value
        if isinstance(value, list): return [plain(item) for item in value]
        return value
    return {key: plain(value) for key, value in result.items()} | {'operations': ledger[0]}


def envelope(terms, operations, conversion):
    scale = sum(map(abs, terms), ZERO)
    conversion += sum((abs(binary(value) - value) for value in terms), ZERO)
    product = operations * Decimal(2) ** -52
    require(product < 1, 'F08', '/numerical_checks', 'Reference operation envelope is undefined', 'missing_evidence')
    rounding = 16 * product / (1 - product) * scale
    require(scale == 0 or conversion + rounding <= Decimal('1e-8') * scale,
            'F08', '/numerical_checks', 'Registered relative envelope cannot bound this calculation', 'missing_evidence')
    return scale, conversion, rounding


def audit(quantity, coordinate, expected, reported, terms, operations, converted, unit):
    scale, conversion, rounding = envelope(terms, operations, abs(converted - expected))
    require(abs(reported - expected) <= conversion + rounding, 'F08', '/numerical_checks/' + quantity,
            'Actual production value exceeds the frozen numerical policy', 'missing_evidence')
    return {'quantity': quantity, 'coordinate_id': known(coordinate) if coordinate is not None else na('scalar_quantity'),
            'operation_count': operations, 'absolute_term_scale': encoded(scale), 'conversion_error_bound': encoded(conversion),
            'roundoff_bound': encoded(rounding), 'independent_value': encoded(expected), 'reported_value': encoded(reported),
            'unit': unit, 'status': 'within_bound', 'reason': 'Production reference expression within frozen envelope'}


def model_audit(model, matrix, targets, predictions):
    with localcontext() as context:
        context.prec = 80
        x, y = [[Decimal(v) for v in row] for row in matrix], [Decimal(v) for v in targets]
        means, variances, scales, z = standardize(x)
        bx = [[binary(v) for v in row] for row in x]
        bm, bv, bs, bz = standardize(bx)
        weights = [Decimal(row['coefficient']) for row in model['feature_statistics']]
        intercept = Decimal(model['intercept'])
        records, n = [], len(y)
        model['solver']['numerical_checks'] = records
        for j, statistic in enumerate(model['feature_statistics']):
            column = [row[j] for row in x]
            for quantity, field, expected, terms, operations, converted, unit in (
                ('feature_mean', 'mean', means[j], column, n + 1, bm[j], statistic['unit']),
                ('feature_variance', 'population_variance', variances[j], [(v - means[j]) ** 2 for v in column], 3 * n + 1, bv[j], statistic['unit'] + '_squared'),
                ('feature_scale', 'scale', scales[j], [scales[j]], 1, bs[j], statistic['unit'])):
                records.append(audit(quantity, statistic['feature_id'], expected, Decimal(statistic[field]), terms, operations, converted, unit))
        for prediction_id, values, reported in predictions:
            values = list(map(Decimal, values))
            terms = [(value - mean) / scale * weight for value, mean, scale, weight in zip(values, means, scales, weights)]
            expected = sum(terms, intercept)
            converted = sum(((binary(value) - mean) / scale * binary(weight) for value, mean, scale, weight in zip(values, bm, bs, weights)), binary(intercept))
            records.append(audit('prediction', prediction_id, expected, Decimal(reported), terms + [intercept], 4 * len(weights), converted, 'ratio'))
        solver = model['solver']
        if model['family'] == 'training_mean':
            mean, binary_targets = sum(y, ZERO) / n, [binary(v) for v in y]
            binary_mean = sum(binary_targets, ZERO) / n
            terms = [(value - mean) ** 2 / (2 * n) for value in y]
            objective = sum(terms, ZERO)
            converted = sum(((v - binary_mean) ** 2 / (2 * n) for v in binary_targets), ZERO)
            solver['objective_value'] = numeric_known(encoded(objective))
            records.append(audit('objective', None, objective, objective, terms, 5 * n + 1, converted, 'ratio_squared'))
        else:
            alpha, ratio = Decimal(model['alpha']['value']), Decimal(model['l1_ratio']['value'])
            result = primal_dual(z, y, weights, intercept, alpha, ratio, model['fit_intercept'])
            converted = primal_dual([[binary(v) for v in row] for row in bz], [binary(v) for v in y],
                                    [binary(v) for v in weights], binary(intercept), binary(alpha), binary(ratio), model['fit_intercept'])
            _, conversion, rounding = envelope(result['terms'], result['operations'], abs(converted['duality_gap'] - result['duality_gap']))
            bound = conversion + rounding
            limit = Decimal(solver['convergence_tolerance']['value']) * result['target_second_moment']
            require(-bound <= Decimal(solver['reported_dual_gap']['value']) <= limit + bound
                    and -bound <= result['duality_gap'] <= limit + bound
                    and all(value <= limit + bound for value in result['coordinate_descent_improvement'])
                    and result['intercept_descent_improvement'] <= limit + bound,
                    'F08', '/solver', 'Actual fitted result fails the frozen optimality policy', 'missing_evidence')
            for quantity, field in (('objective', 'objective_value'), ('dual_bound', 'independent_dual_bound'), ('duality_gap', 'independent_gap')):
                value = result[quantity]
                solver[field] = numeric_known(encoded(value))
                records.append(audit(quantity, None, value, value, result['terms'], result['operations'], converted[quantity], 'ratio_squared'))
            solver['maximum_kkt_violation'] = numeric_known(encoded(result['maximum_kkt']))
            for j, statistic in enumerate(model['feature_statistics']):
                value = result['coordinate_descent_improvement'][j]
                records.append(audit('coordinate_descent_improvement', statistic['feature_id'], value, value, result['terms'], result['operations'], converted['coordinate_descent_improvement'][j], 'ratio_squared'))
            value = result['intercept_descent_improvement']
            records.append(audit('intercept_descent_improvement', None, value, value, result['terms'], result['operations'], converted['intercept_descent_improvement'], 'ratio_squared'))
        solver['numerical_checks'] = records
        return records
