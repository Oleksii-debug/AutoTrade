"""Deep immutable snapshot authority for admitted raw market payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping


class PayloadSnapshotError(ValueError):
    """Raised when raw payload state cannot be safely detached from its caller."""


_MAX_PAYLOAD_CONTAINER_ITEMS = 20_000
_MAX_PAYLOAD_NODES = 100_000
_MAX_PAYLOAD_DEPTH = 64


class _SnapshotBudget:
    """Bound detached authority growth independently of caller graph size."""

    __slots__ = ("remaining_nodes",)

    def __init__(self) -> None:
        self.remaining_nodes = _MAX_PAYLOAD_NODES

    def consume(self, *, path: str) -> None:
        if self.remaining_nodes <= 0:
            raise PayloadSnapshotError(
                f"{path} exceeds the market payload node resource envelope"
            )
        self.remaining_nodes -= 1


def _snapshot(
    value: Any,
    *,
    path: str,
    active: set[int],
    budget: _SnapshotBudget,
    depth: int,
) -> Any:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise PayloadSnapshotError(
            f"{path} exceeds the market payload nesting resource envelope"
        )
    budget.consume(path=path)
    if isinstance(value, Mapping):
        if len(value) > _MAX_PAYLOAD_CONTAINER_ITEMS:
            raise PayloadSnapshotError(
                f"{path} exceeds the market payload container resource envelope"
            )
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
                    budget=budget,
                    depth=depth + 1,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)

    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_PAYLOAD_CONTAINER_ITEMS:
            raise PayloadSnapshotError(
                f"{path} exceeds the market payload container resource envelope"
            )
        identity = id(value)
        if identity in active:
            raise PayloadSnapshotError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            return tuple(
                _snapshot(
                    item,
                    path=f"{path}[{index}]",
                    active=active,
                    budget=budget,
                    depth=depth + 1,
                )
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
        if type(value.tzinfo) is not timezone:
            raise PayloadSnapshotError(
                f"{path} datetime must use an exact built-in timezone"
            )
        if datetime.utcoffset(value) is None:
            raise PayloadSnapshotError(f"{path} datetime must be timezone-aware")
        try:
            normalized = datetime.astimezone(value, timezone.utc)
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
    frozen = _snapshot(
        value,
        path="payload",
        active=set(),
        budget=_SnapshotBudget(),
        depth=0,
    )
    assert isinstance(frozen, Mapping)
    return frozen


__all__ = ["PayloadSnapshotError", "snapshot_market_payload"]
