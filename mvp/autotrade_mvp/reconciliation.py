"""Evidence-bound provider/account reconciliation foundation.

This module is intentionally provider-neutral and network-free. It can only
interpret supplied evidence. It never treats an empty result page as proof of
absence unless all required surfaces have complete coverage and their provider
consistency horizons have elapsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Iterable, Mapping


_REQUIRED_ABSENCE_SURFACES = frozenset(
    {"OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"}
)


class ReconciliationError(ValueError):
    """Raised when reconciliation evidence is malformed or contradictory."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationError(f"{field} is required")
    return value.strip()


def _sequence(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReconciliationError(f"{field} must be a non-negative integer")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReconciliationError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: Decimal | str | int, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ReconciliationError(f"{field} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ReconciliationError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise ReconciliationError(f"{field} must be a finite decimal")
    return result


def _decimal_map(
    value: Mapping[str, Decimal | str | int] | None,
    field: str,
) -> Mapping[str, Decimal]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ReconciliationError(f"{field} must be a mapping")
    normalized: dict[str, Decimal] = {}
    for raw_key, raw_value in value.items():
        key = _text(raw_key, f"{field} key")
        if key in normalized:
            raise ReconciliationError(f"{field} contains duplicate key {key}")
        normalized[key] = _decimal(raw_value, f"{field}[{key}]")
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class CoverageWindow:
    surface: str
    started_at: datetime
    ended_at: datetime
    complete: bool
    cursor_exhausted: bool
    consistency_horizon_satisfied: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface", _text(self.surface, "surface").upper())
        start = _utc(self.started_at, "started_at")
        end = _utc(self.ended_at, "ended_at")
        if end < start:
            raise ReconciliationError("coverage window ended before it started")
        object.__setattr__(self, "started_at", start)
        object.__setattr__(self, "ended_at", end)
        for field in ("complete", "cursor_exhausted", "consistency_horizon_satisfied"):
            if type(getattr(self, field)) is not bool:
                raise ReconciliationError(f"{field} must be boolean")

    @property
    def proves_complete_surface(self) -> bool:
        return (
            self.complete
            and self.cursor_exhausted
            and self.consistency_horizon_satisfied
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "started_at": _utc_text(self.started_at),
            "ended_at": _utc_text(self.ended_at),
            "complete": self.complete,
            "cursor_exhausted": self.cursor_exhausted,
            "consistency_horizon_satisfied": self.consistency_horizon_satisfied,
        }


@dataclass(frozen=True)
class ProviderOrder:
    provider_order_id: str
    client_order_id: str | None
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_order_id",
            _text(self.provider_order_id, "provider_order_id"),
        )
        if self.client_order_id is not None:
            object.__setattr__(
                self,
                "client_order_id",
                _text(self.client_order_id, "client_order_id"),
            )
        object.__setattr__(self, "status", _text(self.status, "status").upper())


@dataclass(frozen=True)
class ProviderExecution:
    provider_execution_id: str
    provider_order_id: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_execution_id",
            _text(self.provider_execution_id, "provider_execution_id"),
        )
        if self.provider_order_id is not None:
            object.__setattr__(
                self,
                "provider_order_id",
                _text(self.provider_order_id, "provider_order_id"),
            )
        if self.client_order_id is not None:
            object.__setattr__(
                self,
                "client_order_id",
                _text(self.client_order_id, "client_order_id"),
            )


@dataclass(frozen=True)
class UnknownSubmission:
    attempt_id: str
    client_order_id: str
    provider_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", _text(self.attempt_id, "attempt_id"))
        object.__setattr__(
            self,
            "client_order_id",
            _text(self.client_order_id, "client_order_id"),
        )
        if self.provider_order_id is not None:
            object.__setattr__(
                self,
                "provider_order_id",
                _text(self.provider_order_id, "provider_order_id"),
            )


@dataclass(frozen=True)
class ReconciliationDifference:
    kind: str
    local_ref: str | None
    provider_ref: str | None
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _text(self.kind, "kind"))
        if self.local_ref is not None:
            object.__setattr__(self, "local_ref", _text(self.local_ref, "local_ref"))
        if self.provider_ref is not None:
            object.__setattr__(
                self, "provider_ref", _text(self.provider_ref, "provider_ref")
            )
        if self.status not in {
            "MATCHED",
            "LOCAL_ONLY",
            "PROVIDER_ONLY",
            "VALUE_MISMATCH",
            "INCONCLUSIVE",
        }:
            raise ReconciliationError("difference status is unsupported")

    def to_contract_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "local_ref": self.local_ref,
            "provider_ref": self.provider_ref,
            "status": self.status,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    run_id: str
    scope: Mapping[str, Any]
    opening_local_version: int
    provider_watermarks: Mapping[str, Any]
    inspected_windows: tuple[CoverageWindow, ...]
    matched_items: tuple[str, ...]
    unmatched_items: tuple[str, ...]
    differences: tuple[ReconciliationDifference, ...]
    actions: tuple[str, ...]
    closing_version: int
    verdict: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _text(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "opening_local_version",
            _sequence(self.opening_local_version, "opening_local_version"),
        )
        object.__setattr__(
            self,
            "closing_version",
            _sequence(self.closing_version, "closing_version"),
        )
        if self.closing_version < self.opening_local_version:
            raise ReconciliationError("closing_version cannot move backwards")
        if self.verdict not in {"CONSISTENT", "CORRECTED", "INCONCLUSIVE", "BLOCKED"}:
            raise ReconciliationError("verdict is unsupported")
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))
        object.__setattr__(
            self,
            "provider_watermarks",
            MappingProxyType(dict(self.provider_watermarks)),
        )
        object.__setattr__(self, "inspected_windows", tuple(self.inspected_windows))
        object.__setattr__(self, "matched_items", tuple(self.matched_items))
        object.__setattr__(self, "unmatched_items", tuple(self.unmatched_items))
        object.__setattr__(self, "differences", tuple(self.differences))
        object.__setattr__(self, "actions", tuple(self.actions))

    @property
    def blocks_new_risk(self) -> bool:
        return self.verdict in {"INCONCLUSIVE", "BLOCKED"}

    def to_contract_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scope": dict(self.scope),
            "opening_local_version": str(self.opening_local_version),
            "provider_watermarks": dict(self.provider_watermarks),
            "inspected_windows": [window.to_dict() for window in self.inspected_windows],
            "matched_items": list(self.matched_items),
            "unmatched_items": list(self.unmatched_items),
            "differences": [
                difference.to_contract_dict() for difference in self.differences
            ],
            "actions": list(self.actions),
            "closing_version": str(self.closing_version),
            "verdict": self.verdict,
        }


def _coverage_index(
    windows: Iterable[CoverageWindow],
) -> tuple[tuple[CoverageWindow, ...], dict[str, CoverageWindow]]:
    normalized = tuple(windows)
    by_surface: dict[str, CoverageWindow] = {}
    for window in normalized:
        existing = by_surface.get(window.surface)
        if existing is not None:
            raise ReconciliationError(
                f"duplicate coverage window for {window.surface}"
            )
        by_surface[window.surface] = window
    return normalized, by_surface


def _absence_is_proven(by_surface: Mapping[str, CoverageWindow]) -> bool:
    return all(
        surface in by_surface and by_surface[surface].proves_complete_surface
        for surface in _REQUIRED_ABSENCE_SURFACES
    )


def _unknown_match(
    unknown: UnknownSubmission,
    orders: Iterable[ProviderOrder],
    executions: Iterable[ProviderExecution],
) -> str | None:
    for order in orders:
        if (
            unknown.provider_order_id is not None
            and order.provider_order_id == unknown.provider_order_id
        ) or order.client_order_id == unknown.client_order_id:
            return f"order:{order.provider_order_id}"
    for execution in executions:
        if (
            unknown.provider_order_id is not None
            and execution.provider_order_id == unknown.provider_order_id
        ) or execution.client_order_id == unknown.client_order_id:
            return f"execution:{execution.provider_execution_id}"
    return None


def reconcile_account(
    *,
    run_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    opening_local_version: int,
    closing_version: int,
    local_provider_order_ids: Iterable[str] = (),
    local_execution_ids: Iterable[str] = (),
    unknown_submissions: Iterable[UnknownSubmission] = (),
    provider_orders: Iterable[ProviderOrder] = (),
    provider_executions: Iterable[ProviderExecution] = (),
    local_balances: Mapping[str, Decimal | str | int] | None = None,
    provider_balances: Mapping[str, Decimal | str | int] | None = None,
    local_positions: Mapping[str, Decimal | str | int] | None = None,
    provider_positions: Mapping[str, Decimal | str | int] | None = None,
    coverage_windows: Iterable[CoverageWindow] = (),
    provider_watermarks: Mapping[str, Any] | None = None,
    snapshot_atomic: bool = False,
    buffered_stream_gap_detected: bool = False,
) -> ReconciliationResult:
    """Compare local durable truth with supplied provider observations.

    A result never performs an economic correction. Definitive mismatches are
    BLOCKED; insufficient coverage is INCONCLUSIVE. Both block new risk.
    """

    provider = _text(provider_id, "provider_id")
    account = _text(account_id, "account_id")
    env = _text(environment, "environment").upper()
    open_version = _sequence(opening_local_version, "opening_local_version")
    close_version = _sequence(closing_version, "closing_version")
    if close_version < open_version:
        raise ReconciliationError("closing_version cannot move backwards")
    if type(snapshot_atomic) is not bool or type(buffered_stream_gap_detected) is not bool:
        raise ReconciliationError("snapshot flags must be boolean")

    local_orders = tuple(
        _text(item, "local_provider_order_id") for item in local_provider_order_ids
    )
    if len(local_orders) != len(set(local_orders)):
        raise ReconciliationError("local provider order IDs must be unique")
    local_execs = tuple(_text(item, "local_execution_id") for item in local_execution_ids)
    if len(local_execs) != len(set(local_execs)):
        raise ReconciliationError("local execution IDs must be unique")

    unknowns = tuple(unknown_submissions)
    if len({item.attempt_id for item in unknowns}) != len(unknowns):
        raise ReconciliationError("unknown submission attempt IDs must be unique")
    orders = tuple(provider_orders)
    if len({item.provider_order_id for item in orders}) != len(orders):
        raise ReconciliationError("provider order IDs must be unique")
    executions = tuple(provider_executions)
    if len({item.provider_execution_id for item in executions}) != len(executions):
        raise ReconciliationError("provider execution IDs must be unique")

    windows, coverage = _coverage_index(coverage_windows)
    absence_proven = _absence_is_proven(coverage)

    local_cash = _decimal_map(local_balances, "local_balances")
    provider_cash = _decimal_map(provider_balances, "provider_balances")
    local_pos = _decimal_map(local_positions, "local_positions")
    provider_pos = _decimal_map(provider_positions, "provider_positions")

    matched: list[str] = []
    unmatched: list[str] = []
    differences: list[ReconciliationDifference] = []
    actions: list[str] = []

    if not snapshot_atomic and buffered_stream_gap_detected:
        unmatched.append("snapshot-cut")
        differences.append(
            ReconciliationDifference(
                kind="SNAPSHOT_CUT",
                local_ref="local-account-state",
                provider_ref="provider-snapshot",
                status="INCONCLUSIVE",
            )
        )
        actions.append("REPEAT_SNAPSHOT_AFTER_STREAM_GAP")

    observed_order_ids = {item.provider_order_id for item in orders}
    observed_execution_ids = {item.provider_execution_id for item in executions}

    resolved_provider_refs: set[str] = set()
    resolved_execution_refs: set[str] = set()
    for unknown in unknowns:
        observed = _unknown_match(unknown, orders, executions)
        item_ref = f"submission:{unknown.attempt_id}"
        if observed is not None:
            matched.append(item_ref)
            actions.append(f"RESOLVE_UNKNOWN_PRESENT:{unknown.attempt_id}:{observed}")
            if observed.startswith("order:"):
                resolved_provider_refs.add(observed.removeprefix("order:"))
            else:
                resolved_execution_refs.add(observed.removeprefix("execution:"))
        elif absence_proven:
            matched.append(f"{item_ref}:PROVEN_ABSENT")
            actions.append(f"RESOLVE_UNKNOWN_PROVEN_ABSENT:{unknown.attempt_id}")
        else:
            unmatched.append(item_ref)
            differences.append(
                ReconciliationDifference(
                    kind="UNKNOWN_SUBMISSION",
                    local_ref=unknown.attempt_id,
                    provider_ref=unknown.provider_order_id,
                    status="INCONCLUSIVE",
                )
            )
            actions.append(f"KEEP_WORST_CASE_RISK:{unknown.attempt_id}")

    for order_id in local_orders:
        ref = f"order:{order_id}"
        if order_id in observed_order_ids:
            matched.append(ref)
            resolved_provider_refs.add(order_id)
        elif absence_proven:
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind="LOCAL_ORDER_ABSENT_AT_PROVIDER",
                    local_ref=order_id,
                    provider_ref=None,
                    status="LOCAL_ONLY",
                )
            )
            actions.append(f"CLOSE_LOCAL_ORDER_AFTER_PROVEN_ABSENCE:{order_id}")
        else:
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind="LOCAL_ORDER_COVERAGE_INCOMPLETE",
                    local_ref=order_id,
                    provider_ref=None,
                    status="INCONCLUSIVE",
                )
            )

    for execution_id in local_execs:
        ref = f"execution:{execution_id}"
        if execution_id in observed_execution_ids:
            matched.append(ref)
            resolved_execution_refs.add(execution_id)
        elif absence_proven:
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind="LOCAL_EXECUTION_ABSENT_AT_PROVIDER",
                    local_ref=execution_id,
                    provider_ref=None,
                    status="LOCAL_ONLY",
                )
            )
            actions.append(f"INVESTIGATE_LOCAL_EXECUTION:{execution_id}")
        else:
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind="LOCAL_EXECUTION_COVERAGE_INCOMPLETE",
                    local_ref=execution_id,
                    provider_ref=None,
                    status="INCONCLUSIVE",
                )
            )

    known_order_ids = set(local_orders) | resolved_provider_refs
    for order in orders:
        if order.provider_order_id not in known_order_ids:
            ref = f"order:{order.provider_order_id}"
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind="EXTERNAL_OR_MANUAL_ORDER",
                    local_ref=None,
                    provider_ref=order.provider_order_id,
                    status="PROVIDER_ONLY",
                )
            )
            actions.append(f"IMPORT_EXTERNAL_ORDER:{order.provider_order_id}")

    known_execution_ids = set(local_execs) | resolved_execution_refs
    for execution in executions:
        if execution.provider_execution_id not in known_execution_ids:
            ref = f"execution:{execution.provider_execution_id}"
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind="EXTERNAL_OR_MANUAL_EXECUTION",
                    local_ref=None,
                    provider_ref=execution.provider_execution_id,
                    status="PROVIDER_ONLY",
                )
            )
            actions.append(f"IMPORT_EXTERNAL_EXECUTION:{execution.provider_execution_id}")

    for prefix, local_values, provider_values in (
        ("BALANCE", local_cash, provider_cash),
        ("POSITION", local_pos, provider_pos),
    ):
        for key in sorted(set(local_values) | set(provider_values)):
            local_value = local_values.get(key, Decimal("0"))
            provider_value = provider_values.get(key, Decimal("0"))
            if local_value == provider_value:
                matched.append(f"{prefix.lower()}:{key}")
                continue
            ref = f"{prefix.lower()}:{key}"
            unmatched.append(ref)
            differences.append(
                ReconciliationDifference(
                    kind=f"{prefix}_VALUE_MISMATCH",
                    local_ref=f"{key}:{local_value}",
                    provider_ref=f"{key}:{provider_value}",
                    status="VALUE_MISMATCH",
                )
            )
            actions.append(f"INVESTIGATE_{prefix}_DIFFERENCE:{key}")

    has_definitive_mismatch = any(
        item.status in {"LOCAL_ONLY", "PROVIDER_ONLY", "VALUE_MISMATCH"}
        for item in differences
    )
    has_inconclusive = any(item.status == "INCONCLUSIVE" for item in differences)
    if has_definitive_mismatch:
        verdict = "BLOCKED"
    elif has_inconclusive:
        verdict = "INCONCLUSIVE"
    elif actions:
        verdict = "BLOCKED"
        actions.append("COMMIT_RECONCILIATION_RESOLUTIONS_BEFORE_READY")
    else:
        verdict = "CONSISTENT"

    watermarks = dict(provider_watermarks or {})
    watermarks.setdefault("snapshot_atomic", snapshot_atomic)
    watermarks.setdefault(
        "buffered_stream_gap_detected", buffered_stream_gap_detected
    )
    watermarks.setdefault("absence_coverage_complete", absence_proven)

    return ReconciliationResult(
        run_id=_text(run_id, "run_id"),
        scope={
            "provider_id": provider,
            "account_id": account,
            "environment": env,
        },
        opening_local_version=open_version,
        provider_watermarks=watermarks,
        inspected_windows=windows,
        matched_items=tuple(sorted(set(matched))),
        unmatched_items=tuple(sorted(set(unmatched))),
        differences=tuple(differences),
        actions=tuple(actions),
        closing_version=close_version,
        verdict=verdict,
    )
