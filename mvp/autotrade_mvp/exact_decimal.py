"""Context-independent exact arithmetic for financial Decimal authority.

Python's :mod:`decimal` arithmetic applies the mutable ambient Context. That is
useful for presentation math, but it must not decide durable financial identity
or hard admission. These helpers convert finite Decimals to exact rational
integers, perform arithmetic there, and only round when a caller supplies an
explicit decimal quantum and rounding rule.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Iterable, Literal


class ExactDecimalError(ValueError):
    """Raised when exact Decimal arithmetic cannot satisfy its contract."""


RoundingMode = Literal["FLOOR", "CEILING", "HALF_EVEN"]


def as_fraction(value: Decimal) -> Fraction:
    """Return the exact rational value of one finite Decimal."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExactDecimalError("value must be a finite Decimal")
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if sign:
        coefficient = -coefficient
    if exponent >= 0:
        return Fraction(coefficient * (10**exponent), 1)
    return Fraction(coefficient, 10 ** (-exponent))


def _decimal_from_scaled_integer(coefficient: int, scale: int) -> Decimal:
    if scale < 0:
        raise ExactDecimalError("scale must be non-negative")
    if coefficient == 0:
        return Decimal("0")
    sign = 1 if coefficient < 0 else 0
    digits = tuple(int(char) for char in str(abs(coefficient)))
    return Decimal((sign, digits, -scale))


def terminating_decimal(value: Fraction) -> Decimal:
    """Convert an exact rational to Decimal iff its base-10 expansion terminates."""

    if not isinstance(value, Fraction):
        raise TypeError("value must be Fraction")
    numerator = value.numerator
    denominator = value.denominator
    powers_of_two = 0
    powers_of_five = 0
    while denominator % 2 == 0:
        denominator //= 2
        powers_of_two += 1
    while denominator % 5 == 0:
        denominator //= 5
        powers_of_five += 1
    if denominator != 1:
        raise ExactDecimalError("rational value has a non-terminating decimal expansion")
    scale = max(powers_of_two, powers_of_five)
    coefficient = numerator
    coefficient *= 2 ** (scale - powers_of_two)
    coefficient *= 5 ** (scale - powers_of_five)
    return _decimal_from_scaled_integer(coefficient, scale)


def exact_abs(value: Decimal) -> Decimal:
    """Return absolute value without Decimal.__abs__ applying ambient context."""

    return terminating_decimal(abs(as_fraction(value)))


def exact_add(left: Decimal, right: Decimal) -> Decimal:
    return terminating_decimal(as_fraction(left) + as_fraction(right))


def exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    return terminating_decimal(as_fraction(left) - as_fraction(right))


def exact_multiply(*values: Decimal) -> Decimal:
    result = Fraction(1, 1)
    for value in values:
        result *= as_fraction(value)
    return terminating_decimal(result)


def exact_sum(values: Iterable[Decimal], *, start: Decimal = Decimal("0")) -> Decimal:
    result = as_fraction(start)
    for value in values:
        result += as_fraction(value)
    return terminating_decimal(result)


def exact_is_multiple(value: Decimal, quantum: Decimal) -> bool:
    """Return whether value is an exact integer multiple of a non-zero quantum."""

    quantum_fraction = as_fraction(quantum)
    if quantum_fraction == 0:
        raise ExactDecimalError("quantum must be non-zero")
    ratio = as_fraction(value) / quantum_fraction
    return ratio.denominator == 1


def round_fraction_to_quantum(
    value: Fraction,
    quantum: Decimal,
    *,
    mode: RoundingMode,
) -> Decimal:
    """Round an exact rational to an explicit finite decimal quantum.

    The operation uses integer arithmetic only; the process Decimal context is
    never consulted. FLOOR and CEILING are mathematical directions, not
    sign-relative shortcuts. HALF_EVEN resolves exact ties toward an even
    quantum multiple.
    """

    if not isinstance(value, Fraction):
        raise TypeError("value must be Fraction")
    quantum_fraction = as_fraction(quantum)
    if quantum_fraction <= 0:
        raise ExactDecimalError("quantum must be positive")
    units = value / quantum_fraction
    floor_units = units.numerator // units.denominator

    if mode == "FLOOR":
        rounded_units = floor_units
    elif mode == "CEILING":
        rounded_units = -((-units.numerator) // units.denominator)
    elif mode == "HALF_EVEN":
        remainder = units.numerator - floor_units * units.denominator
        doubled = remainder * 2
        if doubled < units.denominator:
            rounded_units = floor_units
        elif doubled > units.denominator:
            rounded_units = floor_units + 1
        else:
            rounded_units = floor_units if floor_units % 2 == 0 else floor_units + 1
    else:
        raise ExactDecimalError("unsupported rounding mode")

    return terminating_decimal(quantum_fraction * rounded_units)


def canonical_decimal_text(value: Decimal) -> str:
    """Render an exact finite Decimal without ambient-context normalization."""

    as_fraction(value)  # validates finite Decimal
    if value == 0:
        return "0"
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return fixed
