"""完整显式配对向量的独立算术；不证明样本或区间科学资格。"""
from fractions import Fraction

from .validation_numbers import finite_fraction


def _rational(value):
    return value if isinstance(value, Fraction) else finite_fraction(value)


def paired_deltas(policy_wealth, baseline_wealth):
    if not policy_wealth or len(policy_wealth) != len(baseline_wealth):
        raise ValueError('Complete nonempty paired vectors must have equal length')
    return tuple(_rational(policy) - _rational(baseline)
                 for policy, baseline in zip(policy_wealth, baseline_wealth))


def paired_mean(policy_wealth, baseline_wealth):
    deltas = paired_deltas(policy_wealth, baseline_wealth)
    return sum(deltas, Fraction(0)) / len(deltas)


def losses(initial_wealth, terminal_wealth):
    if not terminal_wealth:
        raise ValueError('Complete terminal wealth vector is required')
    initial = _rational(initial_wealth)
    return tuple(initial - _rational(wealth) for wealth in terminal_wealth)


def empirical_cvar(loss_vector, *, beta, weights):
    """最坏尾部质量积分；阈值原子仅取剩余质量，保留负损失。"""
    if not loss_vector or len(loss_vector) != len(weights):
        raise ValueError('Complete loss vector and explicit weights must align')
    quantile = _rational(beta)
    probabilities = tuple(_rational(weight) for weight in weights)
    if not 0 < quantile < 1 or any(weight < 0 for weight in probabilities):
        raise ValueError('Invalid beta or probability weight')
    if sum(probabilities, Fraction(0)) != 1:
        raise ValueError('Explicit probability weights must sum exactly to one')
    tail = remaining = 1 - quantile
    integral = Fraction(0)
    atoms = sorted(zip(map(_rational, loss_vector), probabilities), reverse=True)
    for loss, probability in atoms:
        mass = min(probability, remaining)
        integral += loss * mass
        remaining -= mass
        if remaining == 0:
            break
    return integral / tail


def synchronized_mbb_vectors(vectors, *, block_length, block_starts, origin_indices):
    """核已给定的重叠块拼接/截n及同步索引；不生成或认证seed/RNG。"""
    if not vectors or not vectors[0]:
        raise ValueError('Complete synchronized vectors are required')
    count = len(vectors[0])
    if any(len(vector) != count for vector in vectors):
        raise ValueError('All synchronized vectors must have equal length')
    if type(block_length) is not int or not 1 <= block_length <= count:
        raise ValueError('Invalid explicit overlapping block length')
    if len(block_starts) != (count + block_length - 1) // block_length:
        raise ValueError('Explicit block count must produce exactly one truncated draw')
    indices = []
    for start in block_starts:
        if type(start) is not int or not 0 <= start <= count - block_length:
            raise ValueError('Overlapping block start is out of range')
        indices.extend(range(start, start + block_length))
    indices = tuple(indices[:count])
    if any(type(index) is not int for index in origin_indices) or tuple(origin_indices) != indices:
        raise ValueError('Recorded origin indices do not match the supplied blocks')
    return tuple(tuple(_rational(vector[index]) for index in indices) for vector in vectors)
