"""Deep immutable snapshot authority for admitted raw market payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping


class PayloadSnapshotError(ValueError):
    """Raised when raw payload state cannot be safely detached from its caller."""


def _snapshot(value: Any, *, path: str, active: set[int]) -> Any:
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise PayloadSnapshotError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise PayloadSnapshotError(
                        f"{path} mapping keys must be exact strings"
                    )
                frozen[key] = _snapshot(
                    item,
                    path=f"{path}.{key}",
                    active=active,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)

    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise PayloadSnapshotError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            return tuple(
                _snapshot(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)

    # Never retain caller-defined scalar subclasses as admitted authority.
    # Containers are rebuilt above; exact immutable scalars are retained, while
    # datetimes are detached from any caller-owned tzinfo implementation by
    # normalizing once to an exact built-in UTC datetime.
    if value is None:
        return None
    if type(value) in (str, bool, int, Decimal):
        return value
    if type(value) is datetime:
        if value.tzinfo is None:
            raise PayloadSnapshotError(f"{path} datetime must be timezone-aware")
        try:
            normalized = value.astimezone(timezone.utc)
        except (OverflowError, ValueError, TypeError) as error:
            raise PayloadSnapshotError(
                f"{path} datetime must have a deterministic timezone"
            ) from error
        if type(normalized) is not datetime:
            raise PayloadSnapshotError(
                f"{path} datetime did not normalize to an exact datetime"
            )
        return normalized

    raise PayloadSnapshotError(
        f"{path} contains unsupported value type {type(value).__name__}"
    )


def snapshot_market_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a recursively detached immutable snapshot of one market payload."""

    if not isinstance(value, Mapping):
        raise PayloadSnapshotError("payload must be an object")
    frozen = _snapshot(value, path="payload", active=set())
    assert isinstance(frozen, Mapping)
    return frozen


__all__ = ["PayloadSnapshotError", "snapshot_market_payload"]
