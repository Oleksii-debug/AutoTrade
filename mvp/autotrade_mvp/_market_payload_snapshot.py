"""Deep immutable snapshot authority for admitted raw market payloads."""

from __future__ import annotations

from collections import UserDict
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


class PayloadSnapshotError(ValueError):
    """Raised when raw payload state cannot be safely detached from its caller."""


_MAX_PAYLOAD_CONTAINER_ITEMS = 20_000
_MAX_PAYLOAD_NODES = 100_000
_MAX_PAYLOAD_DEPTH = 64


class FrozenMarketPayload(Mapping[str, Any]):
    """Module-owned immutable mapping used after raw market ingress.

    The tuple representation is intentionally revalidated on every admission.
    Even if a caller obtains an instance and mutates its private slot via
    object.__setattr__, re-admission never executes caller-defined container
    callbacks and will fail closed on malformed state.
    """

    __slots__ = ("_items",)

    def __init__(self, items: tuple[tuple[str, Any], ...]) -> None:
        if type(items) is not tuple:
            raise TypeError("FrozenMarketPayload items must be an exact tuple")
        object.__setattr__(self, "_items", items)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenMarketPayload is immutable")

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        for key, _ in self._items:
            yield key

    def __getitem__(self, key: str) -> Any:
        for current_key, value in self._items:
            if current_key == key:
                return value
        raise KeyError(key)


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


def _mapping_items_without_callbacks(
    value: object,
    *,
    path: str,
) -> tuple[tuple[str, Any], ...]:
    """Read only exact built-in or module-owned mapping state."""

    if type(value) is dict:
        if len(value) > _MAX_PAYLOAD_CONTAINER_ITEMS:
            raise PayloadSnapshotError(
                f"{path} exceeds the market payload container resource envelope"
            )
        return tuple(value.items())

    if type(value) is UserDict:
        data = value.data
        if type(data) is not dict:
            raise PayloadSnapshotError(f"{path} UserDict state is invalid")
        if len(data) > _MAX_PAYLOAD_CONTAINER_ITEMS:
            raise PayloadSnapshotError(
                f"{path} exceeds the market payload container resource envelope"
            )
        return tuple(data.items())

    if type(value) is FrozenMarketPayload:
        items = value._items
        if type(items) is not tuple:
            raise PayloadSnapshotError(f"{path} frozen mapping state is invalid")
        if len(items) > _MAX_PAYLOAD_CONTAINER_ITEMS:
            raise PayloadSnapshotError(
                f"{path} exceeds the market payload container resource envelope"
            )
        validated: list[tuple[str, Any]] = []
        for entry in items:
            if type(entry) is not tuple or len(entry) != 2:
                raise PayloadSnapshotError(f"{path} frozen mapping state is invalid")
            key, item = entry
            if type(key) is not str:
                raise PayloadSnapshotError(
                    f"{path} mapping keys must be exact strings"
                )
            validated.append((key, item))
        return tuple(validated)

    raise PayloadSnapshotError(
        f"{path} must use an exact dict/UserDict or admitted frozen market payload"
    )


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

    if type(value) in {dict, UserDict, FrozenMarketPayload}:
        identity = id(value)
        if identity in active:
            raise PayloadSnapshotError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            frozen_items: list[tuple[str, Any]] = []
            seen_keys: set[str] = set()
            for key, item in _mapping_items_without_callbacks(value, path=path):
                if type(key) is not str:
                    raise PayloadSnapshotError(
                        f"{path} mapping keys must be exact strings"
                    )
                if key in seen_keys:
                    raise PayloadSnapshotError(
                        f"{path} contains duplicate mapping keys"
                    )
                seen_keys.add(key)
                frozen_items.append(
                    (
                        key,
                        _snapshot(
                            item,
                            path=f"{path}.{key}",
                            active=active,
                            budget=budget,
                            depth=depth + 1,
                        ),
                    )
                )
            return FrozenMarketPayload(tuple(frozen_items))
        finally:
            active.remove(identity)

    if type(value) is list or type(value) is tuple:
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


def snapshot_market_payload(value: object) -> FrozenMarketPayload:
    """Return a recursively detached immutable snapshot of one market payload."""

    if type(value) not in {dict, UserDict, FrozenMarketPayload}:
        raise PayloadSnapshotError(
            "payload must use an exact dict/UserDict or admitted frozen market payload"
        )
    frozen = _snapshot(
        value,
        path="payload",
        active=set(),
        budget=_SnapshotBudget(),
        depth=0,
    )
    if type(frozen) is not FrozenMarketPayload:
        raise PayloadSnapshotError("payload did not normalize to a frozen object")
    return frozen


__all__ = [
    "FrozenMarketPayload",
    "PayloadSnapshotError",
    "snapshot_market_payload",
]
