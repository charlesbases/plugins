"""Finite decimal model estimates and their validated probability measure.

No eligibility, ranking or risk algorithm lives here. Serialized model wealth
is a finite decimal estimate, not an assertion of infinite ledger precision.
"""
import math
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from contracts import require


def number(value):
    if isinstance(value, Fraction):
        return value
    require(not isinstance(value, bool), "Boolean is not a model number")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Finite decimal model number required") from error
    require(decimal.is_finite(), "Finite decimal model number required")
    return Fraction(decimal)


def measure(probabilities):
    """Validate the original PMF, then define one exact normalized measure."""
    weights = [number(value) for value in probabilities]
    require(weights and all(value >= 0 for value in weights)
        and all(math.isfinite(float(value)) for value in weights)
        and abs(math.fsum(float(value) for value in weights)-1) <= 1e-8,
        "Original probability mass required")
    total = sum(weights)
    require(total > 0, "Positive original probability mass required")
    return [value/total for value in weights]
