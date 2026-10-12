"""SC08独立验证的精确数值基础；返回数值不授予任何业务用途。"""
from decimal import Decimal
from fractions import Fraction

from investment.research.contracts import scientific_decimal


def finite_decimal(value: str) -> Decimal:
    """严格读取现有ScientificDecimal，不接受布尔、非有限或默认值。"""
    if not isinstance(value, str) or len(value) > 64 or not scientific_decimal(value):
        raise ValueError('Expected a finite ScientificDecimal string')
    return Decimal(value)


def finite_fraction(value: str) -> Fraction:
    return Fraction(finite_decimal(value))


def exact_decimal(value: Fraction) -> Decimal:
    """仅转换可终止的有理数，不依赖进程Decimal精度。"""
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError('Rational value has no terminating decimal representation')
    places = max(twos, fives)
    integer = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    return Decimal((int(integer < 0), tuple(map(int, str(abs(integer)))), -places))


def rounded_decimal(value: Fraction, *, places: int, rounding: str) -> Decimal:
    """按显式精度整数舍入；统计点/下界/上界分别用half_even/floor/ceiling。"""
    if type(places) is not int or places < 0:
        raise ValueError('Decimal places must be an explicit nonnegative integer')
    if rounding not in ('half_up', 'half_even', 'floor', 'ceiling'):
        raise ValueError('Unsupported explicit rounding rule')
    scaled = value * 10 ** places
    lower, remainder = divmod(scaled.numerator, scaled.denominator)
    if rounding == 'floor':
        integer = lower
    elif rounding == 'ceiling':
        integer = lower + (remainder != 0)
    else:
        twice = 2 * remainder
        integer = lower + (twice > scaled.denominator)
        if twice == scaled.denominator:
            integer = lower + (lower % 2 != 0 if rounding == 'half_even' else scaled >= 0)
    return Decimal((int(integer < 0), tuple(map(int, str(abs(integer)))), -places))


def decimal_text(value: Fraction) -> str:
    """原金融壳需要普通十进制文本，拒绝非终止值而非近似。"""
    text = format(exact_decimal(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text
