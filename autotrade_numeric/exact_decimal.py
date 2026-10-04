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
import re
from typing import Iterable, Literal

from ._generated_common_scalars import is_valid_common_scalar
from ._generated_decimal_limits import (
    MAX_DECIMAL_TEXT_LENGTH,
    MAX_INTEGER_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
)


class ExactDecimalError(ValueError):
    """Raised when exact Decimal arithmetic cannot satisfy its contract."""


RoundingMode = Literal["FLOOR", "CEILING", "HALF_EVEN"]

# Wire Decimal limits are generated into this package from the canonical schema.
# Keep only operation-specific intermediate budgets local to exact arithmetic.
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
    # Decimal is subclassable and methods such as is_finite(), as_tuple(), and
    # __format__ are virtual on subclasses. Financial authority therefore
    # accepts only the exact built-in scalar before consulting any Decimal
    # semantics; otherwise a caller-controlled subclass could choose the
    # coefficient/exponent or canonical identity consumed below.
    if type(value) is not Decimal:
        raise ExactDecimalError("value must be a finite Decimal")
    if not Decimal.is_finite(value):
        raise ExactDecimalError("value must be a finite Decimal")
    sign, digits, exponent = Decimal.as_tuple(value)
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


def parse_canonical_decimal_text(value: object) -> Decimal:
    """Parse canonical bounded wire Decimal text after cheap lexical admission.

    The package-local common-scalar binding owns the wire grammar and resource
    geometry. Only after that text-only validation succeeds do we construct
    Decimal, then re-apply the exact-financial envelope as a defensive invariant.
    """

    if type(value) is not str or not is_valid_common_scalar("Decimal", value):
        raise ExactDecimalError("value must be canonical bounded Decimal text")
    result = Decimal(value)
    _validate_decimal_envelope(result)
    return result


# Provider/domain presentation admission is intentionally distinct from the
# stricter canonical Decimal wire grammar. No Decimal construction, fixed
# expansion, hashing or financial mutation precedes this bounded preflight.
_BOUNDED_PRESENTATION = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z",
    re.ASCII,
)
_JSON_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z",
    re.ASCII,
)
_JSON_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)\Z", re.ASCII)


def _bounded_exponent(
    text: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """Parse only enough exponent digits to decide bounded admission.

    'text' has already passed the ASCII presentation grammar. Saturation
    prevents constructing an oversized integer even if wire limits grow.
    """
    if not text:
        return 0
    negative = text[0] == "-"
    digits = text[1:] if text[0] in "+-" else text
    saturation = max(abs(minimum), abs(maximum)) + 1
    magnitude = 0
    for digit in digits:
        if magnitude <= saturation:
            magnitude = min(saturation + 1, magnitude * 10 + ord(digit) - 48)
    value = -magnitude if negative else magnitude
    if value < minimum or value > maximum:
        raise ExactDecimalError("decimal exponent exceeds resource envelope")
    return value


def _preflight_bounded_presentation(value: str, *, allow_exponent: bool) -> bool:
    """Return true for a validated zero token; otherwise validate geometry.

    This is a text-only check over exact builtin strings. The coefficient's
    *leading* zeros are insignificant; its written trailing zeros must remain
    significant, matching Decimal.as_tuple() and provider presentation.
    """
    if (
        len(value) > MAX_DECIMAL_TEXT_LENGTH
        or _BOUNDED_PRESENTATION.fullmatch(value) is None
    ):
        raise ExactDecimalError("invalid or oversized decimal presentation")
    unsigned = value[1:] if value.startswith(("-", "+")) else value
    e_position = unsigned.find("e")
    if e_position < 0:
        e_position = unsigned.find("E")
    if e_position >= 0 and not allow_exponent:
        raise ExactDecimalError("decimal exponent is not permitted")
    mantissa = unsigned if e_position < 0 else unsigned[:e_position]
    exponent_text = "" if e_position < 0 else unsigned[e_position + 1:]
    integer_part, dot, fractional_part = mantissa.partition(".")
    coefficient = (integer_part + fractional_part).lstrip("0")
    if not coefficient:
        # Canonicalize any grammar/length-safe zero immediately; never expand
        # an extreme zero exponent later with Decimal or format(..., 'f').
        return True
    significant_digits = len(coefficient)
    if significant_digits > MAX_SIGNIFICANT_DIGITS:
        raise ExactDecimalError("decimal exceeds maximum significant digits")
    fractional_length = len(fractional_part) if dot else 0
    minimum_exp = fractional_length - MAX_SCALE
    maximum_exp = fractional_length + MAX_INTEGER_DIGITS - significant_digits
    exponent = _bounded_exponent(
        exponent_text,
        minimum=minimum_exp,
        maximum=maximum_exp,
    )
    tuple_exponent = exponent - fractional_length
    if (
        max(-tuple_exponent, 0) > MAX_SCALE
        or max(significant_digits + tuple_exponent, 0) > MAX_INTEGER_DIGITS
    ):
        raise ExactDecimalError("decimal exceeds resource envelope")
    return False


def parse_bounded_exact_decimal(
    value: object,
    *,
    allow_exponent: bool = True,
) -> Decimal:
    """Admit exact provider/domain Decimal presentation before construction.

    Only exact built-in Decimal, int and str may supply financial quantity.
    Wire canonical admission remains parse_canonical_decimal_text(); this
    permissive boundary does not authorize arbitrary provider evidence.
    """
    if type(allow_exponent) is not bool:
        raise TypeError("allow_exponent must be bool")
    if type(value) is Decimal:
        _validate_decimal_envelope(value)
        if not any(Decimal.as_tuple(value).digits):
            return Decimal("0")
        return value
    if type(value) is int:
        if value == 0:
            return Decimal("0")
        if _integer_digit_count(
            value,
            limit=min(MAX_SIGNIFICANT_DIGITS, MAX_INTEGER_DIGITS),
        ) > min(MAX_SIGNIFICANT_DIGITS, MAX_INTEGER_DIGITS):
            raise ExactDecimalError("integer exceeds decimal resource envelope")
        result = Decimal(value)
        _validate_decimal_envelope(result)
        return result
    if type(value) is not str:
        raise ExactDecimalError("value must be exact str, int or finite Decimal")
    if _preflight_bounded_presentation(value, allow_exponent=allow_exponent):
        return Decimal("0")
    result = Decimal(value)
    _validate_decimal_envelope(result)
    return result


def parse_bounded_json_number_token(text: str) -> Decimal:
    """Strict JSON numeric token callback for json.loads(parse_float=...)."""
    if (
        type(text) is not str
        or len(text) > MAX_DECIMAL_TEXT_LENGTH
        or _JSON_NUMBER.fullmatch(text) is None
    ):
        raise ExactDecimalError("invalid or oversized JSON numeric token")
    return parse_bounded_exact_decimal(text)


def parse_bounded_json_integer_token(text: str) -> int:
    """Strict JSON integer callback that preserves integer/Decimal identity."""
    if (
        type(text) is not str
        or len(text) > MAX_DECIMAL_TEXT_LENGTH
        or _JSON_INTEGER.fullmatch(text) is None
    ):
        raise ExactDecimalError("invalid or oversized JSON integer token")
    if text in ("0", "-0"):
        return 0
    digits = len(text) - (1 if text.startswith("-") else 0)
    if digits > min(MAX_SIGNIFICANT_DIGITS, MAX_INTEGER_DIGITS):
        raise ExactDecimalError("JSON integer exceeds decimal resource envelope")
    return int(text)


def _validate_fraction_intermediate(
    value: Fraction,
    *,
    max_digits: int = MAX_RATIONAL_DIGITS,
) -> None:
    # Fraction is subclassable and numerator/denominator can be overridden.
    # Exact-rational authority must reject polymorphic values before reading
    # either property; all internally constructed Fraction values are exact
    # built-ins already.
    if type(value) is not Fraction:
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


def is_exact_decimal_multiple(value: Decimal, quantum: Decimal) -> bool:
    """Return whether bounded value is an exact integer multiple of quantum.

    The test is pure rational/integer arithmetic. It never consults Decimal
    precision or rounding, so grid admission is stable across process contexts.
    """

    value_fraction = as_fraction(value)
    quantum_fraction = as_fraction(quantum)
    if quantum_fraction <= 0:
        raise ExactDecimalError("quantum must be positive")
    units = value_fraction / quantum_fraction
    _validate_fraction_intermediate(units)
    return units.denominator == 1


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

    # ``str`` is subclassable and equality can be overridden. Reject a
    # polymorphic mode before any branch comparison so caller code cannot
    # select financial rounding semantics through virtual ``__eq__``.
    if type(mode) is not str:
        raise ExactDecimalError("unsupported rounding mode")
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
