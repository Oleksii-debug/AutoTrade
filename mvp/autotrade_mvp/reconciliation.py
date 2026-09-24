"""Evidence-bound provider/account reconciliation for the AutoTrade foundation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping, Sequence


_REQUIRED_ABSENCE_SURFACES = frozenset(
    {"OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"}
)
_RECONCILIATION_AUTHORITY = object()


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CoverageSurfaceEvidence:
    surface: str
    coverage_start: str
    coverage_end: str
    pagination_complete: bool
    consistency_horizon_satisfied: bool
    provider_semantics_exclude_execution: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "surface",
            _text(self.surface, name="surface").upper(),
        )
        start = _instant(self.coverage_start, name="coverage_start")
        end = _instant(self.coverage_end, name="coverage_end")
        if end < start:
            raise ValueError("coverage_end must not precede coverage_start")
        for field in (
            "pagination_complete",
            "consistency_horizon_satisfied",
            "provider_semantics_exclude_execution",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def proves_absence_for(self, instant: datetime) -> bool:
        start = _instant(self.coverage_start, name="coverage_start")
        end = _instant(self.coverage_end, name="coverage_end")
        return (
            self.pagination_complete
            and self.consistency_horizon_satisfied
            and self.provider_semantics_exclude_execution
            and start <= instant <= end
        )


@dataclass(frozen=True)
class SnapshotConsistencyEvidence:
    """Evidence that a multi-surface provider snapshot has one coherent cut."""

    mode: str
    query_started_at: str
    query_completed_at: str
    buffered_stream_events: bool = False
    replay_complete: bool = False
    sequence_gap_detected: bool = False

    def __post_init__(self) -> None:
        normalized = _text(self.mode, name="mode").upper()
        if normalized not in {"ATOMIC", "COMPOSED"}:
            raise ValueError("mode must be ATOMIC or COMPOSED")
        object.__setattr__(self, "mode", normalized)
        started = _instant(self.query_started_at, name="query_started_at")
        completed = _instant(self.query_completed_at, name="query_completed_at")
        if completed < started:
            raise ValueError("query_completed_at must not precede query_started_at")
        for field in (
            "buffered_stream_events",
            "replay_complete",
            "sequence_gap_detected",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    @property
    def consistent(self) -> bool:
        if self.mode == "ATOMIC":
            return True
        return (
            self.buffered_stream_events
            and self.replay_complete
            and not self.sequence_gap_detected
        )


@dataclass(frozen=True)
class LocalWorkingOrderEvidence:
    client_order_id: str
    instrument: str
    remaining_quantity: Decimal

    @classmethod
    def create(
        cls,
        *,
        client_order_id: str,
        instrument: str,
        remaining_quantity,
    ) -> "LocalWorkingOrderEvidence":
        remaining = _decimal(remaining_quantity, name="remaining_quantity")
        if remaining <= 0:
            raise ValueError("remaining_quantity must be positive")
        return cls(
            client_order_id=_text(client_order_id, name="client_order_id"),
            instrument=_text(instrument, name="instrument"),
            remaining_quantity=remaining,
        )


@dataclass(frozen=True)
class ProviderWorkingOrderEvidence:
    provider_order_id: str
    client_order_id: str | None
    instrument: str
    remaining_quantity: Decimal

    @classmethod
    def create(
        cls,
        *,
        provider_order_id: str,
        client_order_id: str | None,
        instrument: str,
        remaining_quantity,
    ) -> "ProviderWorkingOrderEvidence":
        remaining = _decimal(remaining_quantity, name="remaining_quantity")
        if remaining <= 0:
            raise ValueError("remaining_quantity must be positive")
        return cls(
            provider_order_id=_text(provider_order_id, name="provider_order_id"),
            client_order_id=(
                _text(client_order_id, name="client_order_id")
                if client_order_id is not None
                else None
            ),
            instrument=_text(instrument, name="instrument"),
            remaining_quantity=remaining,
        )


@dataclass(frozen=True)
class ProviderFillEvidence:
    provider_execution_id: str
    client_order_id: str | None
    instrument: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_currency: str
    trade_time: str

    @classmethod
    def create(
        cls,
        *,
        provider_execution_id: str,
        client_order_id: str | None,
        instrument: str,
        quantity,
        price,
        fee_amount=0,
        fee_currency: str,
        trade_time: str,
    ) -> "ProviderFillEvidence":
        qty = _decimal(quantity, name="quantity")
        px = _decimal(price, name="price")
        fee = _decimal(fee_amount, name="fee_amount")
        if qty <= 0 or px <= 0:
            raise ValueError("quantity and price must be positive")
        _instant(trade_time, name="trade_time")
        return cls(
            provider_execution_id=_text(
                provider_execution_id, name="provider_execution_id"
            ),
            client_order_id=(
                _text(client_order_id, name="client_order_id")
                if client_order_id is not None
                else None
            ),
            instrument=_text(instrument, name="instrument"),
            quantity=qty,
            price=px,
            fee_amount=fee,
            fee_currency=_text(fee_currency, name="fee_currency").upper(),
            trade_time=trade_time,
        )


@dataclass(frozen=True)
class UnknownSubmission:
    attempt_id: str
    client_order_id: str
    started_at: str

    @classmethod
    def create(
        cls, *, attempt_id: str, client_order_id: str, started_at: str
    ) -> "UnknownSubmission":
        _instant(started_at, name="started_at")
        return cls(
            attempt_id=_text(attempt_id, name="attempt_id"),
            client_order_id=_text(client_order_id, name="client_order_id"),
            started_at=started_at,
        )


@dataclass(frozen=True)
class SubmissionResolution:
    attempt_id: str
    client_order_id: str
    outcome: str
    evidence_reason: str
    provider_execution_ids: tuple[str, ...] = ()
    provider_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationResult:
    complete: bool
    matched_execution_ids: tuple[str, ...]
    unexpected_execution_ids: tuple[str, ...]
    missing_local_execution_ids: tuple[str, ...]
    matched_working_client_order_ids: tuple[str, ...]
    unexpected_working_provider_order_ids: tuple[str, ...]
    missing_local_working_client_order_ids: tuple[str, ...]
    mismatched_working_client_order_ids: tuple[str, ...]
    snapshot_consistent: bool
    cash_differences: Mapping[str, Decimal]
    position_differences: Mapping[str, Decimal]
    submission_resolutions: tuple[SubmissionResolution, ...]
    blocking_resources: tuple[str, ...]
    reasons: tuple[str, ...]
    _authority_marker: object = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._authority_marker is not _RECONCILIATION_AUTHORITY:
            raise TypeError(
                "ReconciliationResult can only be created by canonical reconcile_account"
            )

    @property
    def blocks_new_risk(self) -> bool:
        return bool(self.blocking_resources)


def _amount_map(
    values: Mapping[str, object], *, name: str
) -> dict[str, Decimal]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    result: dict[str, Decimal] = {}
    for key, value in values.items():
        result[_text(str(key), name=f"{name} key")] = _decimal(
            value, name=f"{name}[{key}]"
        )
    return result


def _absence_coverage_index(
    evidence: Sequence[CoverageSurfaceEvidence],
) -> dict[str, CoverageSurfaceEvidence]:
    result: dict[str, CoverageSurfaceEvidence] = {}
    for item in evidence:
        if not isinstance(item, CoverageSurfaceEvidence):
            raise TypeError(
                "absence_coverage must contain CoverageSurfaceEvidence"
            )
        if item.surface in result:
            raise ValueError(
                f"duplicate absence coverage surface: {item.surface}"
            )
        result[item.surface] = item
    return result


def _absence_is_proven(
    evidence: Mapping[str, CoverageSurfaceEvidence],
    submission_time: datetime,
) -> bool:
    return all(
        surface in evidence
        and evidence[surface].proves_absence_for(submission_time)
        for surface in _REQUIRED_ABSENCE_SURFACES
    )


def reconcile_account(
    *,
    local_cash: Mapping[str, object],
    provider_cash: Mapping[str, object],
    local_positions: Mapping[str, object],
    provider_positions: Mapping[str, object],
    local_execution_ids: Sequence[str],
    provider_fills: Sequence[ProviderFillEvidence],
    local_working_client_order_ids: Sequence[str] = (),
    local_working_orders: Sequence[LocalWorkingOrderEvidence] = (),
    provider_working_orders: Sequence[ProviderWorkingOrderEvidence] = (),
    snapshot_consistency: SnapshotConsistencyEvidence | None = None,
    unknown_submissions: Sequence[UnknownSubmission] = (),
    searched_client_order_ids: Sequence[str] = (),
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    absence_coverage: Sequence[CoverageSurfaceEvidence] = (),
    cash_tolerance: Mapping[str, object] | None = None,
    position_tolerance: Mapping[str, object] | None = None,
) -> ReconciliationResult:
    """Compare local and provider truth without inventing absence evidence.

    A previously UNKNOWN send becomes PROVEN_ABSENT only when the provider
    lookup explicitly searched its client order id and open-order, order-history,
    execution and activity surfaces each provide complete coverage across the
    submission instant, their consistency horizons have elapsed, and provider
    semantics explicitly exclude execution. Otherwise it remains UNKNOWN and
    blocks affected new risk.
    """

    if not isinstance(pagination_complete, bool):
        raise TypeError("pagination_complete must be boolean")
    start = _instant(coverage_start, name="coverage_start")
    end = _instant(coverage_end, name="coverage_end")
    if end < start:
        raise ValueError("coverage_end must not precede coverage_start")

    local_cash_map = _amount_map(local_cash, name="local_cash")
    provider_cash_map = _amount_map(provider_cash, name="provider_cash")
    local_position_map = _amount_map(local_positions, name="local_positions")
    provider_position_map = _amount_map(
        provider_positions, name="provider_positions"
    )
    cash_tol = _amount_map(cash_tolerance or {}, name="cash_tolerance")
    pos_tol = _amount_map(
        position_tolerance or {}, name="position_tolerance"
    )
    if any(value < 0 for value in cash_tol.values()) or any(
        value < 0 for value in pos_tol.values()
    ):
        raise ValueError("reconciliation tolerances must be non-negative")

    local_ids = tuple(_text(value, name="local_execution_id") for value in local_execution_ids)
    if len(local_ids) != len(set(local_ids)):
        raise ValueError("local_execution_ids must be unique")

    provider_by_id: dict[str, ProviderFillEvidence] = {}
    provider_client_ids: set[str] = set()
    provider_execution_ids_by_client: dict[str, set[str]] = {}
    for fill in provider_fills:
        if not isinstance(fill, ProviderFillEvidence):
            raise TypeError("provider_fills must contain ProviderFillEvidence")
        if fill.provider_execution_id in provider_by_id:
            if provider_by_id[fill.provider_execution_id] != fill:
                raise ValueError("provider execution id has conflicting observations")
            continue
        provider_by_id[fill.provider_execution_id] = fill
        if fill.client_order_id is not None:
            provider_client_ids.add(fill.client_order_id)
            provider_execution_ids_by_client.setdefault(fill.client_order_id, set()).add(
                fill.provider_execution_id
            )

    provider_ids = set(provider_by_id)
    local_id_set = set(local_ids)
    matched = tuple(sorted(local_id_set & provider_ids))
    unexpected = tuple(sorted(provider_ids - local_id_set))
    missing = tuple(sorted(local_id_set - provider_ids))

    local_working_ids = tuple(
        _text(value, name="local_working_client_order_id")
        for value in local_working_client_order_ids
    )
    if len(local_working_ids) != len(set(local_working_ids)):
        raise ValueError("local_working_client_order_ids must be unique")

    local_working_by_client_id: dict[str, LocalWorkingOrderEvidence] = {}
    for order in local_working_orders:
        if not isinstance(order, LocalWorkingOrderEvidence):
            raise TypeError(
                "local_working_orders must contain LocalWorkingOrderEvidence"
            )
        existing = local_working_by_client_id.get(order.client_order_id)
        if existing is not None and existing != order:
            raise ValueError(
                "local client order id has conflicting working-order observations"
            )
        local_working_by_client_id[order.client_order_id] = order

    provider_working_by_id: dict[str, ProviderWorkingOrderEvidence] = {}
    provider_working_by_client_id: dict[str, ProviderWorkingOrderEvidence] = {}
    for order in provider_working_orders:
        if not isinstance(order, ProviderWorkingOrderEvidence):
            raise TypeError(
                "provider_working_orders must contain ProviderWorkingOrderEvidence"
            )
        existing_provider = provider_working_by_id.get(order.provider_order_id)
        if existing_provider is not None:
            if existing_provider != order:
                raise ValueError("provider working order id has conflicting observations")
            continue
        provider_working_by_id[order.provider_order_id] = order
        if order.client_order_id is not None:
            existing_client = provider_working_by_client_id.get(order.client_order_id)
            if existing_client is not None and existing_client != order:
                raise ValueError(
                    "client order id maps to multiple provider working orders"
                )
            provider_working_by_client_id[order.client_order_id] = order

    local_working_set = set(local_working_ids) | set(local_working_by_client_id)
    provider_working_client_ids = set(provider_working_by_client_id)
    candidate_matched_working = local_working_set & provider_working_client_ids
    mismatched_working: list[str] = []
    matched_working_values: list[str] = []
    for client_order_id in sorted(candidate_matched_working):
        local_detail = local_working_by_client_id.get(client_order_id)
        provider_detail = provider_working_by_client_id[client_order_id]
        if local_detail is None:
            # Identity proves that an ambiguous send reached the provider, so it
            # prevents blind resend, but it is not enough to prove current open
            # exposure.  Exact instrument and remaining quantity are required
            # before the account can become READY.
            mismatched_working.append(client_order_id)
        elif (
            local_detail.instrument != provider_detail.instrument
            or local_detail.remaining_quantity != provider_detail.remaining_quantity
        ):
            mismatched_working.append(client_order_id)
        else:
            matched_working_values.append(client_order_id)
    matched_working = tuple(matched_working_values)
    mismatched_working_ids = tuple(mismatched_working)
    missing_local_working = tuple(
        sorted(local_working_set - provider_working_client_ids)
    )
    unexpected_working = tuple(
        sorted(
            order.provider_order_id
            for order in provider_working_by_id.values()
            if order.client_order_id is None
            or order.client_order_id not in local_working_set
        )
    )

    if snapshot_consistency is not None and not isinstance(
        snapshot_consistency, SnapshotConsistencyEvidence
    ):
        raise TypeError(
            "snapshot_consistency must be SnapshotConsistencyEvidence"
        )
    snapshot_is_consistent = bool(
        snapshot_consistency is not None and snapshot_consistency.consistent
    )

    cash_differences: dict[str, Decimal] = {}
    for currency in sorted(set(local_cash_map) | set(provider_cash_map)):
        difference = provider_cash_map.get(currency, Decimal("0")) - local_cash_map.get(
            currency, Decimal("0")
        )
        tolerance = cash_tol.get(currency, Decimal("0"))
        if abs(difference) > tolerance:
            cash_differences[currency] = difference

    position_differences: dict[str, Decimal] = {}
    for instrument in sorted(
        set(local_position_map) | set(provider_position_map)
    ):
        difference = provider_position_map.get(
            instrument, Decimal("0")
        ) - local_position_map.get(instrument, Decimal("0"))
        tolerance = pos_tol.get(instrument, Decimal("0"))
        if abs(difference) > tolerance:
            position_differences[instrument] = difference

    searched = {
        _text(value, name="searched_client_order_id")
        for value in searched_client_order_ids
    }
    absence_evidence = _absence_coverage_index(absence_coverage)
    resolutions: list[SubmissionResolution] = []
    for submission in unknown_submissions:
        if not isinstance(submission, UnknownSubmission):
            raise TypeError("unknown_submissions must contain UnknownSubmission")
        submission_time = _instant(
            submission.started_at, name="unknown_submission.started_at"
        )
        matched_provider_execution_ids = tuple(
            sorted(provider_execution_ids_by_client.get(submission.client_order_id, set()))
        )
        matched_provider_order = provider_working_by_client_id.get(
            submission.client_order_id
        )
        matched_provider_order_ids: tuple[str, ...] = ()
        if matched_provider_execution_ids:
            outcome = "OBSERVED_EXECUTION"
            reason = "provider_activity_contains_client_order_id"
        elif matched_provider_order is not None:
            outcome = "OBSERVED_WORKING_ORDER"
            reason = "provider_working_orders_contains_client_order_id"
            matched_provider_order_ids = (matched_provider_order.provider_order_id,)
        elif (
            snapshot_is_consistent
            and pagination_complete
            and submission.client_order_id in searched
            and start <= submission_time <= end
            and _absence_is_proven(absence_evidence, submission_time)
        ):
            outcome = "PROVEN_ABSENT"
            reason = (
                "complete_open_history_execution_activity_coverage_"
                "with_elapsed_consistency_horizons_and_provider_semantics"
            )
        else:
            outcome = "UNKNOWN"
            if not pagination_complete:
                reason = "provider_activity_pagination_incomplete"
            elif submission.client_order_id not in searched:
                reason = "client_order_id_not_explicitly_searched"
            elif not (start <= submission_time <= end):
                reason = "submission_time_outside_complete_coverage"
            elif not snapshot_is_consistent:
                reason = "provider_snapshot_consistency_not_evidenced"
            else:
                reason = "absence_surface_evidence_incomplete"
        resolutions.append(
            SubmissionResolution(
                attempt_id=submission.attempt_id,
                client_order_id=submission.client_order_id,
                outcome=outcome,
                evidence_reason=reason,
                provider_execution_ids=matched_provider_execution_ids,
                provider_order_ids=matched_provider_order_ids,
            )
        )

    blocking: set[str] = set()
    reasons: list[str] = []
    if not snapshot_is_consistent:
        blocking.add("ACCOUNT")
        if snapshot_consistency is None:
            reasons.append("provider snapshot consistency is not evidenced")
        elif snapshot_consistency.sequence_gap_detected:
            reasons.append("provider snapshot stream contains a sequence gap")
        else:
            reasons.append(
                "provider composed snapshot did not complete buffered stream replay"
            )
    if not pagination_complete:
        blocking.add("ACCOUNT")
        reasons.append("provider activity pagination is incomplete")
    if unexpected:
        for execution_id in unexpected:
            fill = provider_by_id[execution_id]
            blocking.add(f"INSTRUMENT:{fill.instrument}")
        reasons.append("provider contains fills absent from local economic truth")
    if missing:
        blocking.add("ACCOUNT")
        reasons.append("local fills are absent from provider evidence window")
    if unexpected_working:
        for provider_order_id in unexpected_working:
            order = provider_working_by_id[provider_order_id]
            blocking.add(f"INSTRUMENT:{order.instrument}")
        reasons.append(
            "provider contains working orders absent from local order truth"
        )
    if missing_local_working:
        blocking.add("ACCOUNT")
        reasons.append(
            "local working orders are absent from provider working-order snapshot"
        )
    if mismatched_working_ids:
        for client_order_id in mismatched_working_ids:
            local_order = local_working_by_client_id.get(client_order_id)
            provider_order = provider_working_by_client_id[client_order_id]
            if local_order is None:
                blocking.add("ACCOUNT")
            else:
                blocking.add(f"INSTRUMENT:{local_order.instrument}")
            blocking.add(f"INSTRUMENT:{provider_order.instrument}")
        reasons.append(
            "working-order instrument or remaining quantity is unverified or differs from local truth"
        )
    for currency in cash_differences:
        blocking.add(f"CASH:{currency}")
    if cash_differences:
        reasons.append("cash snapshot differs beyond declared tolerance")
    for instrument in position_differences:
        blocking.add(f"INSTRUMENT:{instrument}")
    if position_differences:
        reasons.append("position snapshot differs beyond declared tolerance")
    if any(item.outcome == "UNKNOWN" for item in resolutions):
        blocking.add("ACCOUNT")
        reasons.append("one or more submission attempts remain UNKNOWN")
    if any(item.outcome == "OBSERVED_EXECUTION" for item in resolutions):
        reasons.append("previously UNKNOWN submission has provider execution evidence")
    if any(item.outcome == "OBSERVED_WORKING_ORDER" for item in resolutions):
        reasons.append("previously UNKNOWN submission has provider working-order evidence")

    complete = (
        snapshot_is_consistent
        and pagination_complete
        and not unexpected
        and not missing
        and not unexpected_working
        and not missing_local_working
        and not mismatched_working_ids
        and not cash_differences
        and not position_differences
        and all(item.outcome != "UNKNOWN" for item in resolutions)
    )
    return ReconciliationResult(
        complete=complete,
        matched_execution_ids=matched,
        unexpected_execution_ids=unexpected,
        missing_local_execution_ids=missing,
        matched_working_client_order_ids=matched_working,
        unexpected_working_provider_order_ids=unexpected_working,
        missing_local_working_client_order_ids=missing_local_working,
        mismatched_working_client_order_ids=mismatched_working_ids,
        snapshot_consistent=snapshot_is_consistent,
        cash_differences=MappingProxyType(cash_differences),
        position_differences=MappingProxyType(position_differences),
        submission_resolutions=tuple(resolutions),
        blocking_resources=tuple(sorted(blocking)),
        reasons=tuple(reasons),
        _authority_marker=_RECONCILIATION_AUTHORITY,
    )
