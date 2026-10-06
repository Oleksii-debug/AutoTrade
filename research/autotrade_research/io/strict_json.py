from __future__ import annotations

import json
import math
from typing import Any

_JSON_WHITESPACE_BYTES = b" \t\r\n"
_JSON_INTEGER_MAX_DIGITS = 640
_JSON_FLOAT_MAX_SIGNIFICAND_DIGITS = 640
_JSON_FLOAT_MAX_EXPONENT_DIGITS = 6
_JSON_MAX_NESTING_DEPTH = 128
_JSON_MAX_DOCUMENT_CHARS = 1_000_000
_JSON_MAX_DECODED_NODES = 100_000


class DuplicateJsonKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"duplicate JSON object key: {key}")


class NonStandardJsonConstantError(ValueError):
    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(f"non-finite JSON constant: {value}")


class InvalidJsonDomainError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateJsonKeyError(key)
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise NonStandardJsonConstantError(value)


def _parse_bounded_json_integer(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > _JSON_INTEGER_MAX_DIGITS:
        raise InvalidJsonDomainError(
            f"JSON integer exceeds {_JSON_INTEGER_MAX_DIGITS} digits"
        )
    parsed = 0
    for character in digits:
        parsed = (parsed * 10) + (ord(character) - ord("0"))
    return -parsed if negative else parsed


def _parse_bounded_json_float(value: str) -> float:
    """Parse a JSON float without silently collapsing nonzero input to zero."""

    mantissa, separator, exponent = value.lower().partition("e")
    significand_digits = [character for character in mantissa if character.isdigit()]
    if len(significand_digits) > _JSON_FLOAT_MAX_SIGNIFICAND_DIGITS:
        raise InvalidJsonDomainError(
            "JSON floating-point significand exceeds "
            f"{_JSON_FLOAT_MAX_SIGNIFICAND_DIGITS} digits"
        )

    if separator:
        exponent_digits = exponent.lstrip("+-")
        if len(exponent_digits) > _JSON_FLOAT_MAX_EXPONENT_DIGITS:
            raise InvalidJsonDomainError(
                "JSON floating-point exponent exceeds "
                f"{_JSON_FLOAT_MAX_EXPONENT_DIGITS} digits"
            )

    parsed = float(value)
    if not math.isfinite(parsed):
        raise InvalidJsonDomainError("non-finite JSON number")

    if parsed == 0.0 and any(character != "0" for character in significand_digits):
        raise InvalidJsonDomainError(
            "nonzero JSON number underflows the supported floating-point domain"
        )
    return parsed


def _validate_json_nesting_before_decode(text: str) -> None:
    """Bound container depth before the recursive stdlib decoder sees the input.

    This scan intentionally does not try to validate JSON syntax. It tracks only
    structural delimiters that are outside strings, while respecting backslash
    escaping inside strings. ``json.loads`` remains the syntax authority and the
    post-decode validator remains defense in depth.
    """

    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _JSON_MAX_NESTING_DEPTH:
                raise InvalidJsonDomainError(
                    f"JSON nesting exceeds {_JSON_MAX_NESTING_DEPTH} containers"
                )
        elif character in "]}" and depth > 0:
            # Syntax matching is deliberately left to json.loads. Refusing to let
            # malformed leading closers drive depth below zero ensures they cannot
            # mask a later deeply nested segment from this resource fence.
            depth -= 1


def _validate_strict_json_value(root: object) -> None:
    stack = [(root, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > _JSON_MAX_DECODED_NODES:
            raise InvalidJsonDomainError(
                f"JSON decoded domain exceeds {_JSON_MAX_DECODED_NODES} nodes"
            )
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise InvalidJsonDomainError(
                    "JSON string contains invalid Unicode scalar"
                ) from exc
        elif value is None or isinstance(value, (bool, int)):
            continue
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidJsonDomainError("non-finite JSON number")
        elif isinstance(value, list):
            child_depth = depth + 1
            if child_depth > _JSON_MAX_NESTING_DEPTH:
                raise InvalidJsonDomainError(
                    f"JSON nesting exceeds {_JSON_MAX_NESTING_DEPTH} containers"
                )
            stack.extend((item, child_depth) for item in value)
        elif isinstance(value, dict):
            child_depth = depth + 1
            if child_depth > _JSON_MAX_NESTING_DEPTH:
                raise InvalidJsonDomainError(
                    f"JSON nesting exceeds {_JSON_MAX_NESTING_DEPTH} containers"
                )
            for key, item in value.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise InvalidJsonDomainError(
                        "JSON object key contains invalid Unicode scalar"
                    ) from exc
                stack.append((item, child_depth))
        else:
            raise InvalidJsonDomainError(
                f"unsupported decoded JSON type: {type(value).__name__}"
            )


def strict_json_loads(text: str) -> Any:
    """Decode one JSON value and reject ambiguous/non-canonical decoded domains."""
    if type(text) is not str:
        raise TypeError("text must be exact str; decode bytes explicitly at the boundary")
    if len(text) > _JSON_MAX_DOCUMENT_CHARS:
        raise InvalidJsonDomainError(
            f"JSON document exceeds {_JSON_MAX_DOCUMENT_CHARS} characters"
        )
    _validate_json_nesting_before_decode(text)
    raw = json.loads(
        text,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonstandard_json_constant,
        parse_int=_parse_bounded_json_integer,
        parse_float=_parse_bounded_json_float,
    )
    _validate_strict_json_value(raw)
    return raw


def jsonl_bytes_are_blank(payload: bytes) -> bool:
    """Return true only for whitespace bytes permitted by the JSON grammar."""
    if type(payload) is not bytes:
        raise TypeError("payload must be exact bytes")
    return not payload.strip(_JSON_WHITESPACE_BYTES)
