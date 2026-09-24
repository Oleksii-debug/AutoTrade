"""Generated-shape common scalar admission helpers.

This module mirrors the canonical common.schema.json scalar subset used by the
shared conformance corpus. It has no financial authority and performs no
numeric conversion for Decimal or Sequence values.
"""

from __future__ import annotations

import re

_PATTERNS = {
    "Decimal": re.compile(r"^(?:0|[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]|-(?:[1-9][0-9]*(?:\.[0-9]*[1-9])?|0\.[0-9]*[1-9]))$"),
    "Sequence": re.compile(r"^(0|[1-9][0-9]*)$"),
    "Digest": re.compile(r"^sha256:[0-9a-f]{64}$"),
    "CurrencyId": re.compile(r"^[A-Za-z0-9._:-]{1,32}$"),
    "UnitId": re.compile(r"^[A-Za-z0-9._:/-]{1,64}$"),
}
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def is_valid_common_scalar(kind: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if kind == "Environment":
        return value in _ENVIRONMENTS
    pattern = _PATTERNS.get(kind)
    if pattern is None:
        raise ValueError(f"unsupported common scalar kind: {kind}")
    return pattern.fullmatch(value) is not None
