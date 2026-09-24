from __future__ import annotations

import json
import math
from typing import Any

_JSON_WHITESPACE_BYTES = b" \t\r\n"
_JSON_INTEGER_MAX_DIGITS = 640
_JSON_MAX_NESTING_DEPTH = 128


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


def _validate_strict_json_value(root: object) -> None:
    stack = [(root, 0)]
    while stack:
        value, depth = stack.pop()
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
    if not isinstance(text, str):
        raise TypeError("text must be str; decode bytes explicitly at the boundary")
    raw = json.loads(
        text,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonstandard_json_constant,
        parse_int=_parse_bounded_json_integer,
    )
    _validate_strict_json_value(raw)
    return raw


def jsonl_bytes_are_blank(payload: bytes) -> bool:
    """Return true only for whitespace bytes permitted by the JSON grammar."""
    return not payload.strip(_JSON_WHITESPACE_BYTES)
