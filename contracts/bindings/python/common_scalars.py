"""AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.

Run python tools/generate_common_scalar_bindings.py to regenerate.
"""

from __future__ import annotations

import re

CONTRACT_VERSION = "7.0.0"

_PATTERNS = {
    "Decimal": re.compile('^(?:0|[1-9][0-9]*(?:\\.[0-9]*[1-9])?|0\\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\\.[0-9]*[1-9])?|0\\.[0-9]*[1-9]))$(?![\\s\\S])'),
    "Sequence": re.compile('^(0|[1-9][0-9]*)$(?![\\s\\S])'),
    "Digest": re.compile('^sha256:[0-9a-f]{64}$(?![\\s\\S])'),
    "CurrencyId": re.compile('^[A-Za-z0-9._:-]+$(?![\\s\\S])'),
    "UnitId": re.compile('^[A-Za-z0-9._:/-]+$(?![\\s\\S])'),
}
_LENGTHS = {
    "Decimal": (None, 259),
    "CurrencyId": (1, 32),
    "UnitId": (1, 64),
}
_DECIMAL_ENVELOPES = {
    "Decimal": (256, 256, 256),
}
_ENUMS = {
    "Environment": frozenset(('REPLAY', 'SIMULATION', 'PAPER', 'LIVE',)),
}


def _within_decimal_envelope(
    value: str, limits: tuple[int, int, int]
) -> bool:
    max_significant_digits, max_scale, max_integer_digits = limits
    unsigned = value[1:] if value.startswith("-") else value
    integer_part, dot, fractional_part = unsigned.partition(".")
    integer_magnitude = 0 if integer_part == "0" else len(integer_part)
    scale = len(fractional_part) if dot else 0
    coefficient = integer_part + fractional_part
    significant_digits = len(coefficient.lstrip("0")) or 1
    return (
        significant_digits <= max_significant_digits
        and scale <= max_scale
        and integer_magnitude <= max_integer_digits
    )


def is_valid_common_scalar(kind: str, value: object) -> bool:
    if type(value) is not str:
        return False
    enum = _ENUMS.get(kind)
    if enum is not None:
        return value in enum
    pattern = _PATTERNS.get(kind)
    if pattern is None:
        raise ValueError(f"unsupported common scalar kind: {kind}")
    limits = _LENGTHS.get(kind)
    if limits is not None:
        minimum, maximum = limits
        if minimum is not None and len(value) < minimum:
            return False
        if maximum is not None and len(value) > maximum:
            return False
    if pattern.fullmatch(value) is None:
        return False
    decimal_limits = _DECIMAL_ENVELOPES.get(kind)
    if decimal_limits is not None and not _within_decimal_envelope(
        value, decimal_limits
    ):
        return False
    return True
