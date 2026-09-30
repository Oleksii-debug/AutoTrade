"""Shared installed v5 exact-number semantics for product and research."""

from .exact_decimal import (
    ExactDecimalError,
    RoundingMode,
    MAX_SIGNIFICANT_DIGITS,
    MAX_SCALE,
    MAX_INTEGER_DIGITS,
    MAX_RATIONAL_DIGITS,
    parse_canonical_decimal_text,
    bounded_fraction,
    as_fraction,
    is_exact_decimal_multiple,
    terminating_decimal,
    exact_abs,
    exact_add,
    exact_subtract,
    exact_multiply,
    exact_sum,
    round_fraction_to_quantum,
    canonical_decimal_text,
)

__all__ = [
    "ExactDecimalError",
    "RoundingMode",
    "MAX_SIGNIFICANT_DIGITS",
    "MAX_SCALE",
    "MAX_INTEGER_DIGITS",
    "MAX_RATIONAL_DIGITS",
    "parse_canonical_decimal_text",
    "bounded_fraction",
    "as_fraction",
    "is_exact_decimal_multiple",
    "terminating_decimal",
    "exact_abs",
    "exact_add",
    "exact_subtract",
    "exact_multiply",
    "exact_sum",
    "round_fraction_to_quantum",
    "canonical_decimal_text"
]
