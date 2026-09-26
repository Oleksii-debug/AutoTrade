"""Canonical fail-closed host action authorization policy.

Only actions with implemented host semantics belong here. Adding a future action
requires an explicit role decision plus schema and parity tests before either host
store may durably accept it.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping


HOST_ACTION_POLICY_VERSION: Final[str] = "1.0.0"

HOST_ACTION_REQUIRED_ROLES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "BLOCK_NEW_EXPOSURE": frozenset({"OWNER", "OPERATOR"}),
        "REVOKE_AUTHORITY": frozenset({"OWNER"}),
        "SET_AUTHORITY": frozenset({"OWNER"}),
    }
)

SUPPORTED_HOST_ACTIONS: Final[tuple[str, ...]] = tuple(HOST_ACTION_REQUIRED_ROLES)


def canonical_host_action(value: object) -> str:
    """Return an exact supported action or fail before authorization/mutation."""

    if not isinstance(value, str) or not value:
        raise ValueError("host action must be a non-empty string")
    if value != value.strip() or value not in HOST_ACTION_REQUIRED_ROLES:
        raise ValueError("unsupported or non-canonical host action")
    return value


def required_roles_for_host_action(value: object) -> frozenset[str]:
    action = canonical_host_action(value)
    return HOST_ACTION_REQUIRED_ROLES[action]
