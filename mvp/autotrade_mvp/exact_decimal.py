"""Context-independent exact arithmetic for financial Decimal authority.

Python's :mod:`decimal` arithmetic applies the mutable ambient Context. That is
useful for presentation math, but it must not decide durable financial identity
or hard admission. These helpers convert finite Decimals to exact rational
integers, perform arithmetic there, and only round when a caller supplies an
explicit decimal quantum and rounding rule.

All public helpers enforce one explicit resource envelope before constructing
large powers or textual projections. The envelope is intentionally much wider
than supported provider precision while remaining finite and independent of the
mutable Decimal context.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Iterable, Literal


class ExactDecimalError(ValueError):
    """Raised when exact Decimal arithmetic cannot satisfy its contract."""


RoundingMode = Literal["FLOOR", "CEILING", "HALF_EVEN"]

# Canonical runtime resource envelope for exact financial decimal authority.
# Contract-layer lexical validation must remain compatible with these bounds.
MAX_SIGNIFICANT_DIGITS = 256
MAX_SCALE = 256
MAX_INTEGER_DIGITS = 256
MAX_RATIONAL_DIGITS = 1024


def _integer_digit_count(value: int, *, limit: int) -> int:
    """Count base-10 digits without converting an integer to text.

    A bit-length guard rejects obviously oversized values before bounded
    repeated division. This avoids dependence on CPython's mutable
    ``sys.int_max_str_digits`` setting.
    """

    number = abs(value)
    if number == 0:
        return 1
    if number.bit_length() > limit * 4 + 1:
        return limit + 1
    count = 0
    while number:
        number //= 10
        count += 1
        if count > limit:
            return count
    return count


def _validate_decimal_envelope(value: Decimal) -> tuple[int, tuple[int, ...], int]:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExactDecimalError("value must be a finite Decimal")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ExactDecimalError("value must be a finite Decimal")
    if not any(digits):
        return sign, (0,), 0

    digit_count = len(digits)
    if digit_count > MAX_SIGNIFICANT_DIGITS:
        raise ExactDecimalError("decimal exceeds maximum significant digits")
    if exponent < -MAX_SCALE:
        raise ExactDecimalError("decimal exceeds maximum scale")
    integer_digits = max(digit_count + exponent, 0)
    if integer_digits > MAX_INTEGER_DIGITS:
        raise ExactDecimalError("decimal exceeds maximum integer digits")
    return sign, digits, exponent


def _validate_fraction_intermediate(
    value: Fraction,
    *,
    max_digits: int = MAX_RATIONAL_DIGITS,
) -> None:
    if not isinstance(value, Fraction):
        raise TypeError("value must be Fraction")
    if _integer_digit_count(value.numerator, limit=max_digits) > max_digits:
        raise ExactDecimalError("rational numerator exceeds resource envelope")
    if _integer_digit_count(value.denominator, limit=max_digits) > max_digits:
        raise ExactDecimalError("rational denominator exceeds resource envelope")


def bounded_fraction(value: Fraction) -> Fraction:
    """Return an exact rational only if it satisfies the shared resource envelope."""

    _validate_fraction_intermediate(value)
    return value


def as_fraction(value: Decimal) -> Fraction:
    """Return the exact rational value of one bounded finite Decimal."""

    sign, digits, exponent = _validate_decimal_envelope(value)
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if sign:
        coefficient = -coefficient
    if coefficient == 0:
        return Fraction(0, 1)
    if exponent >= 0:
        result = Fraction(coefficient * (10**exponent), 1)
    else:
        result = Fraction(coefficient, 10 ** (-exponent))
    _validate_fraction_intermediate(result)
    return result


def _decimal_from_scaled_integer(coefficient: int, scale: int) -> Decimal:
    if scale < 0:
        raise ExactDecimalError("scale must be non-negative")
    if scale > MAX_SCALE:
        raise ExactDecimalError("decimal exceeds maximum scale")
    if coefficient == 0:
        return Decimal("0")

    absolute = abs(coefficient)
    digit_count = _integer_digit_count(
        absolute,
        limit=MAX_SIGNIFICANT_DIGITS,
    )
    if digit_count > MAX_SIGNIFICANT_DIGITS:
        raise ExactDecimalError("decimal exceeds maximum significant digits")
    if max(digit_count - scale, 0) > MAX_INTEGER_DIGITS:
        raise ExactDecimalError("decimal exceeds maximum integer digits")

    reversed_digits: list[int] = []
    while absolute:
        absolute, digit = divmod(absolute, 10)
        reversed_digits.append(digit)
    digits = tuple(reversed(reversed_digits))
    sign = 1 if coefficient < 0 else 0
    return Decimal((sign, digits, -scale))


def terminating_decimal(value: Fraction) -> Decimal:
    """Convert an exact rational to Decimal iff its bounded expansion terminates."""

    _validate_fraction_intermediate(value)
    numerator = value.numerator
    denominator = value.denominator
    powers_of_two = 0
    powers_of_five = 0
    while denominator % 2 == 0:
        denominator //= 2
        powers_of_two += 1
        if powers_of_two > MAX_SCALE:
            raise ExactDecimalError("decimal exceeds maximum scale")
    while denominator % 5 == 0:
        denominator //= 5
        powers_of_five += 1
        if powers_of_five > MAX_SCALE:
            raise ExactDecimalError("decimal exceeds maximum scale")
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
    result = as_fraction(left) + as_fraction(right)
    _validate_fraction_intermediate(result)
    return terminating_decimal(result)


def exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    result = as_fraction(left) - as_fraction(right)
    _validate_fraction_intermediate(result)
    return terminating_decimal(result)


def exact_multiply(*values: Decimal) -> Decimal:
    result = Fraction(1, 1)
    for value in values:
        result *= as_fraction(value)
        _validate_fraction_intermediate(result)
    return terminating_decimal(result)


def exact_sum(values: Iterable[Decimal], *, start: Decimal = Decimal("0")) -> Decimal:
    result = as_fraction(start)
    for value in values:
        result += as_fraction(value)
        _validate_fraction_intermediate(result)
    return terminating_decimal(result)


def round_fraction_to_quantum(
    value: Fraction,
    quantum: Decimal,
    *,
    mode: RoundingMode,
) -> Decimal:
    """Round an exact rational to an explicit bounded finite decimal quantum.

    The operation uses integer arithmetic only; the process Decimal context is
    never consulted. FLOOR and CEILING are mathematical directions, not
    sign-relative shortcuts. HALF_EVEN resolves exact ties toward an even
    quantum multiple.
    """

    _validate_fraction_intermediate(value)
    quantum_fraction = as_fraction(quantum)
    if quantum_fraction <= 0:
        raise ExactDecimalError("quantum must be positive")
    units = value / quantum_fraction
    _validate_fraction_intermediate(units)
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

    rounded = quantum_fraction * rounded_units
    _validate_fraction_intermediate(rounded)
    return terminating_decimal(rounded)


def canonical_decimal_text(value: Decimal) -> str:
    """Render a bounded exact finite Decimal without ambient-context normalization."""

    _validate_decimal_envelope(value)
    if value == 0:
        return "0"
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return fixed
