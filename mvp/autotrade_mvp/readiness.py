"""Truthful runtime readiness projection for AutoTrade.

This module consumes safety/recovery facts from their canonical authorities. It
does not send orders, reconcile accounts, acquire ownership, or perform
failover. Its only job is to prevent a false READY state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class ReadinessError(ValueError):
    pass


class RuntimeMode(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    PROTECTION_ONLY = "PROTECTION_ONLY"
    NOT_READY = "NOT_READY"


def _bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ReadinessError(f"{name} must be boolean")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ReadinessError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ReadinessError(f"{name} must be an exact finite decimal") from error
    if not result.is_finite() or result < 0:
        raise ReadinessError(f"{name} must be a non-negative finite decimal")
    return result


def _count(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReadinessError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeSafetySignals:
    journal_writable: bool
    emergency_disk_reserve_available: bool
    schema_compatible: bool
    provider_authenticated: bool
    market_data_fresh: bool
    sender_ownership_proven: bool
    old_sender_fenced: bool
    provider_native_protection_present: bool
    emergency_execution_path_qualified: bool
    unknown_send_count: int
    reconciliation_lag_seconds: Decimal
    maximum_reconciliation_lag_seconds: Decimal
    clock_skew_seconds: Decimal
    maximum_clock_skew_seconds: Decimal
    unresolved_external_uncertainty: bool
    recovery_in_progress: bool

    def __post_init__(self) -> None:
        for field in (
            "journal_writable",
            "emergency_disk_reserve_available",
            "schema_compatible",
            "provider_authenticated",
            "market_data_fresh",
            "sender_ownership_proven",
            "old_sender_fenced",
            "provider_native_protection_present",
            "emergency_execution_path_qualified",
            "unresolved_external_uncertainty",
            "recovery_in_progress",
        ):
            object.__setattr__(
                self,
                field,
                _bool(getattr(self, field), name=field),
            )
        object.__setattr__(
            self,
            "unknown_send_count",
            _count(self.unknown_send_count, name="unknown_send_count"),
        )
        for field in (
            "reconciliation_lag_seconds",
            "maximum_reconciliation_lag_seconds",
            "clock_skew_seconds",
            "maximum_clock_skew_seconds",
        ):
            object.__setattr__(
                self,
                field,
                _decimal(getattr(self, field), name=field),
            )


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    live: bool
    mode: RuntimeMode
    ready_for_read: bool
    ready_for_new_exposure: bool
    protection_only_available: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.mode is RuntimeMode.READY


def evaluate_readiness(signals: RuntimeSafetySignals) -> RuntimeReadiness:
    if not isinstance(signals, RuntimeSafetySignals):
        raise TypeError("signals must be RuntimeSafetySignals")

    blockers: list[str] = []
    warnings: list[str] = []

    if not signals.schema_compatible:
        blockers.append("schema_incompatible")
    if not signals.sender_ownership_proven:
        blockers.append("sender_ownership_unproven")
    if not signals.old_sender_fenced:
        blockers.append("old_sender_not_fenced")
    if not signals.journal_writable:
        blockers.append("journal_not_writable")
    if not signals.emergency_disk_reserve_available:
        blockers.append("emergency_disk_reserve_unavailable")
    if not signals.provider_authenticated:
        blockers.append("provider_not_authenticated")
    if not signals.market_data_fresh:
        blockers.append("market_data_stale")
    if signals.unknown_send_count:
        blockers.append("unknown_sends_present")
    if signals.unresolved_external_uncertainty:
        blockers.append("external_uncertainty_unresolved")
    if (
        signals.reconciliation_lag_seconds
        > signals.maximum_reconciliation_lag_seconds
    ):
        blockers.append("reconciliation_lag_exceeded")
    if signals.clock_skew_seconds > signals.maximum_clock_skew_seconds:
        blockers.append("clock_skew_exceeded")
    if signals.recovery_in_progress:
        blockers.append("recovery_in_progress")

    protection_only = (
        signals.provider_authenticated
        and signals.sender_ownership_proven
        and signals.old_sender_fenced
        and (
            signals.provider_native_protection_present
            or (
                signals.emergency_execution_path_qualified
                and signals.emergency_disk_reserve_available
            )
        )
    )

    ready_for_read = signals.schema_compatible
    ready_for_new_exposure = not blockers

    if ready_for_new_exposure:
        mode = RuntimeMode.READY
    elif protection_only:
        mode = RuntimeMode.PROTECTION_ONLY
    elif ready_for_read:
        mode = RuntimeMode.DEGRADED
    else:
        mode = RuntimeMode.NOT_READY

    if not signals.provider_native_protection_present:
        warnings.append("provider_native_protection_absent")
    if not signals.emergency_execution_path_qualified:
        warnings.append("emergency_execution_path_unqualified")

    return RuntimeReadiness(
        live=True,
        mode=mode,
        ready_for_read=ready_for_read,
        ready_for_new_exposure=ready_for_new_exposure,
        protection_only_available=protection_only,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )
