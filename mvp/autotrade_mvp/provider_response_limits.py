"""Pure shared upper bound for provider HTTP response bytes.

This is a resource policy, not a proof of provider origin or response meaning.
Adapters may impose a *smaller* route-qualified limit; no caller may raise the
hard ceiling without a reviewed policy/version change.
"""
from __future__ import annotations

DEFAULT_MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
HARD_MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024


def require_provider_response_bytes(raw: bytes, *, max_bytes: int = DEFAULT_MAX_PROVIDER_RESPONSE_BYTES) -> bytes:
    """Validate exact raw bytes before JSON parsing, journal, or provider projection."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= HARD_MAX_PROVIDER_RESPONSE_BYTES:
        raise ValueError("provider response byte budget is invalid")
    if type(raw) is not bytes or not raw:
        raise ValueError("provider response must be nonempty exact bytes")
    if len(raw) > max_bytes:
        raise ValueError("provider response exceeds byte budget")
    return raw


MAX_PROVIDER_JSON_DEPTH = 64


def require_provider_json_depth(raw: bytes, *, max_depth: int = MAX_PROVIDER_JSON_DEPTH) -> bytes:
    """Bound JSON structure BEFORE json.loads creates an attacker-sized graph.

    One pass over exact already-bounded raw bytes; quoted brackets/braces and
    backslash-escaped quotes are not counted. JSON grammar and exact numeric
    decoding remain with the single existing consumer decoder, not here.
    A scalar child of a 64-deep object/array has canonical freeze depth 64.
    """
    require_provider_response_bytes(raw, max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES)
    if type(max_depth) is not int or not 1 <= max_depth <= MAX_PROVIDER_JSON_DEPTH:
        raise ValueError("provider JSON structural depth budget is invalid")
    # provider_core._freeze_json starts the root at logical depth zero,
    # recursively checking every child at depth + 1. Thus an empty container
    # at depth 64 (65 nested containers including root) is valid, while any
    # child inside that deepest container is not. This scanner preserves that
    # installed boundary without recursively parsing or loosening it.
    containers: list[int] = []
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:  # backslash
                escaped = True
            elif byte == 34:  # quote
                quoted = False
            continue
        if byte == 34:  # opening quote
            if len(containers) - 1 >= max_depth:
                raise ValueError("provider JSON exceeds structural depth budget")
            quoted = True
        elif byte in (91, 123):  # [ {
            if len(containers) > max_depth:
                raise ValueError("provider JSON exceeds structural depth budget")
            containers.append(byte)
        elif byte in (93, 125):  # ] }
            if not containers or (containers[-1] == 91 and byte != 93) or (
                containers[-1] == 123 and byte != 125
            ):
                raise ValueError("provider JSON has invalid structural nesting")
            containers.pop()
        elif len(containers) - 1 >= max_depth and byte not in (9, 10, 13, 32):
            # A leaf at depth 64 may be an *empty* container only; any
            # literal, object key, delimiter or nested child would cause
            # _freeze_json(child, depth=65) to reject after allocation.
            raise ValueError("provider JSON exceeds structural depth budget")
    return raw
