"""Compatibility facade for the shared AutoTrade exact numeric authority.

The implementation lives in :mod:`autotrade_numeric.exact_decimal` so the MVP
runtime and the separately installed research package consume one source of
truth for Decimal/rational resource bounds and exact arithmetic.
"""

from autotrade_numeric.exact_decimal import (
    ExactDecimalError,
    MAX_INTEGER_DIGITS,
    MAX_RATIONAL_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    RoundingMode,
    as_fraction,
    bounded_fraction,
    canonical_decimal_text,
    exact_abs,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
    round_fraction_to_quantum,
    terminating_decimal,
    validate_fraction,
)

__all__ = [
    "ExactDecimalError",
    "MAX_INTEGER_DIGITS",
    "MAX_RATIONAL_DIGITS",
    "MAX_SCALE",
    "MAX_SIGNIFICANT_DIGITS",
    "RoundingMode",
    "as_fraction",
    "bounded_fraction",
    "canonical_decimal_text",
    "exact_abs",
    "exact_add",
    "exact_multiply",
    "exact_subtract",
    "exact_sum",
    "round_fraction_to_quantum",
    "terminating_decimal",
    "validate_fraction",
]
