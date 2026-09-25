"""AUTO-GENERATED from contracts/jsonschema/common.schema.json. DO NOT EDIT.

Run python tools/generate_common_scalar_bindings.py to regenerate.
"""

from __future__ import annotations

import re

CONTRACT_VERSION = "3.0.0"

_PATTERNS = {
    "Decimal": re.compile('^(?:0|[1-9][0-9]*(?:\\.[0-9]*[1-9])?|0\\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\\.[0-9]*[1-9])?|0\\.[0-9]*[1-9]))$'),
    "Sequence": re.compile('^(0|[1-9][0-9]*)$'),
    "Digest": re.compile('^sha256:[0-9a-f]{64}$'),
    "CurrencyId": re.compile('^[A-Za-z0-9._:-]+$'),
    "UnitId": re.compile('^[A-Za-z0-9._:/-]+$'),
}
_LENGTHS = {
    "CurrencyId": (1, 32),
    "UnitId": (1, 64),
}
_ENUMS = {
    "Environment": frozenset(('REPLAY', 'SIMULATION', 'PAPER', 'LIVE',)),
}


def is_valid_common_scalar(kind: str, value: object) -> bool:
    if not isinstance(value, str):
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
    return pattern.fullmatch(value) is not None
