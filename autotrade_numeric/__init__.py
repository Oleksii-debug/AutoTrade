"""Shared bounded exact numeric authority for AutoTrade."""

from .exact_decimal import (
    ExactDecimalError,
    MAX_INTEGER_DIGITS,
    MAX_RATIONAL_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    RoundingMode,
    as_fraction,
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
