"""Compatibility re-exports for the single neutral strict-JSON authority."""

from autotrade_runtime.strict_json import (
    DuplicateJsonKeyError,
    InvalidJsonDomainError,
    NonStandardJsonConstantError,
    jsonl_bytes_are_blank,
    strict_json_loads,
)

__all__ = [
    "DuplicateJsonKeyError",
    "InvalidJsonDomainError",
    "NonStandardJsonConstantError",
    "jsonl_bytes_are_blank",
    "strict_json_loads",
]
