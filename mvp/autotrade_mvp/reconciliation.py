"""Evidence-bound provider/account reconciliation for the AutoTrade foundation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping, Sequence

from .securities_borrow import BorrowAvailabilityEvidence


_REQUIRED_ABSENCE_SURFACES = frozenset(
    {"OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"}
)
_ACTIVITY_ORIGINS = frozenset({"AUTOTRADE", "MANUAL", "EXTERNAL", "UNKNOWN"})
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


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


def _environment(value: str) -> str:
    normalized = _text(value, name="environment").upper()
    if normalized not in _ENVIRONMENTS:
        raise ValueError(
            "environment must be one of LIVE, PAPER, REPLAY, SIMULATION"
        )
    return normalized


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
    provider_id: str
    account_id: str
    environment: str
    surface: str
    coverage_start: str
    coverage_end: str
    pagination_complete: bool
    consistency_horizon_satisfied: bool
    provider_semantics_exclude_execution: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "environment", _environment(self.environment)
        )
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

    def proves_complete_window(self, start: datetime, end: datetime) -> bool:
        coverage_start = _instant(self.coverage_start, name="coverage_start")
        coverage_end = _instant(self.coverage_end, name="coverage_end")
        return (
            self.pagination_complete
            and self.consistency_horizon_satisfied
            and coverage_start <= start <= end <= coverage_end
        )


@dataclass(frozen=True)
class SnapshotConsistencyEvidence:
    """Evidence that a multi-surface provider snapshot has one coherent cut."""

    provider_id: str
    account_id: str
    environment: str
    mode: str
    query_started_at: str
    query_completed_at: str
    buffered_stream_events: bool = False
    replay_complete: bool = False
    sequence_gap_detected: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "environment", _environment(self.environment)
        )
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
            return not self.sequence_gap_detected
        return (
            self.buffered_stream_events
            and self.replay_complete
            and not self.sequence_gap_detected
        )


@dataclass(frozen=True)
class ResourceAvailabilityEvidence:
    """Exact provider availability bound to one coherent account snapshot cut.

    This is reservation-capacity evidence, not a derived equity estimate.  It
    preserves provider/account/environment identity and freshness so the
    financial writer can fail closed instead of trusting caller-supplied
    availability.
    """

    provider_id: str
    account_id: str
    environment: str
    snapshot_id: str
    query_started_at: str
    query_completed_at: str
    valid_until: str
    available_resources: Mapping[str, Decimal]
    provider_as_of: str | None = None
    evidence_refs: tuple[str, ...] = ()
    resource_details: Mapping[str, Mapping[str, str]] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(self, "environment", _environment(self.environment))
        object.__setattr__(
            self, "snapshot_id", _text(self.snapshot_id, name="snapshot_id")
        )
        started = _instant(self.query_started_at, name="query_started_at")
        completed = _instant(self.query_completed_at, name="query_completed_at")
        valid = _instant(self.valid_until, name="valid_until")
        if completed < started:
            raise ValueError("query_completed_at must not precede query_started_at")
        if valid <= completed:
            raise ValueError("valid_until must be after query_completed_at")
        if self.provider_as_of is not None:
            _instant(self.provider_as_of, name="provider_as_of")
        if not isinstance(self.available_resources, Mapping):
            raise TypeError("available_resources must be a mapping")
        if not self.available_resources:
            raise ValueError("available_resources must not be empty")
        normalized: dict[str, Decimal] = {}
        for resource, raw in self.available_resources.items():
            if not isinstance(resource, str):
                raise TypeError("available_resources keys must be strings")
            key = _text(resource, name="available_resources key")
            if key in normalized:
                raise ValueError(
                    "available_resources keys must be unique after normalization"
                )
            amount = _decimal(raw, name=f"available_resources[{key}]")
            if amount < 0:
                raise ValueError("available resource amounts must be non-negative")
            normalized[key] = amount
        object.__setattr__(
            self,
            "available_resources",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

        raw_details = {} if self.resource_details is None else self.resource_details
        if not isinstance(raw_details, Mapping):
            raise TypeError("resource_details must be a mapping")
        normalized_details: dict[str, Mapping[str, str]] = {}
        for raw_resource, raw_detail in raw_details.items():
            resource = _text(raw_resource, name="resource_details key")
            if resource not in normalized:
                raise ValueError(
                    "resource_details may only describe available_resources"
                )
            if not isinstance(raw_detail, Mapping):
                raise TypeError("resource detail must be a mapping")
            detail: dict[str, str] = {}
            for raw_key, raw_value in raw_detail.items():
                key = _text(raw_key, name="resource detail key")
                if not isinstance(raw_value, str):
                    raise TypeError("resource detail values must be strings")
                if key in detail:
                    raise ValueError(
                        "resource detail keys must be unique after normalization"
                    )
                detail[key] = raw_value

            if resource.startswith("BORROW:"):
                borrow = BorrowAvailabilityEvidence.from_resource_detail(detail)
                if borrow.resource_key != resource:
                    raise ValueError(
                        "borrow resource identity does not match evidence scope"
                    )
                if (
                    borrow.provider_id != self.provider_id
                    or borrow.account_id != self.account_id
                    or borrow.environment != self.environment
                ):
                    raise ValueError("borrow availability scope mismatch")
                if borrow.capacity_quantity != normalized[resource]:
                    raise ValueError(
                        "borrow capacity differs from available resource amount"
                    )
                observed = _instant(
                    borrow.observed_at,
                    name="borrow_availability.observed_at",
                )
                expires = _instant(
                    borrow.expires_at,
                    name="borrow_availability.expires_at",
                )
                if observed < started or observed > completed:
                    raise ValueError(
                        "borrow availability observation is outside snapshot cut"
                    )
                if valid > expires:
                    raise ValueError(
                        "resource availability outlives borrow evidence"
                    )
            normalized_details[resource] = MappingProxyType(
                dict(sorted(detail.items()))
            )

        missing_borrow_details = [
            resource
            for resource in normalized
            if resource.startswith("BORROW:")
            and resource not in normalized_details
        ]
        if missing_borrow_details:
            raise ValueError(
                "BORROW resources require typed securities-borrow evidence"
            )
        object.__setattr__(
            self,
            "resource_details",
            MappingProxyType(dict(sorted(normalized_details.items()))),
        )

        if not isinstance(self.evidence_refs, tuple):
            raise TypeError("evidence_refs must be a tuple of strings")
        if not self.evidence_refs:
            raise ValueError(
                "resource availability requires at least one evidence_ref"
            )
        refs: list[str] = []
        for reference in self.evidence_refs:
            ref = _text(reference, name="evidence_ref")
            if ref in refs:
                raise ValueError("evidence_refs must be unique")
            refs.append(ref)
        object.__setattr__(self, "evidence_refs", tuple(refs))



@dataclass(frozen=True)
class ProviderWorkingOrderEvidence:
    provider_id: str
    account_id: str
    environment: str
    provider_order_id: str
    client_order_id: str | None
    instrument: str
    remaining_quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "environment", _environment(self.environment)
        )
        remaining = _decimal(self.remaining_quantity, name="remaining_quantity")
        if remaining <= 0:
            raise ValueError("remaining_quantity must be positive")
        object.__setattr__(
            self,
            "provider_order_id",
            _text(self.provider_order_id, name="provider_order_id"),
        )
        object.__setattr__(
            self,
            "client_order_id",
            (
                _text(self.client_order_id, name="client_order_id")
                if self.client_order_id is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "instrument",
            _text(self.instrument, name="instrument"),
        )
        object.__setattr__(self, "remaining_quantity", remaining)

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        provider_order_id: str,
        client_order_id: str | None,
        instrument: str,
        remaining_quantity,
    ) -> "ProviderWorkingOrderEvidence":
        remaining = _decimal(remaining_quantity, name="remaining_quantity")
        if remaining <= 0:
            raise ValueError("remaining_quantity must be positive")
        return cls(
            provider_id=_text(provider_id, name="provider_id").upper(),
            account_id=_text(account_id, name="account_id"),
            environment=_environment(environment),
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
    provider_id: str
    account_id: str
    environment: str
    provider_execution_id: str
    client_order_id: str | None
    instrument: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_currency: str
    trade_time: str
    evidence_refs: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "environment", _environment(self.environment)
        )
        quantity = _decimal(self.quantity, name="quantity")
        price = _decimal(self.price, name="price")
        fee_amount = _decimal(self.fee_amount, name="fee_amount")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")
        _instant(self.trade_time, name="trade_time")
        object.__setattr__(
            self,
            "provider_execution_id",
            _text(self.provider_execution_id, name="provider_execution_id"),
        )
        object.__setattr__(
            self,
            "client_order_id",
            (
                _text(self.client_order_id, name="client_order_id")
                if self.client_order_id is not None
                else None
            ),
        )
        object.__setattr__(
            self,
            "instrument",
            _text(self.instrument, name="instrument"),
        )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "fee_amount", fee_amount)
        object.__setattr__(
            self,
            "fee_currency",
            _text(self.fee_currency, name="fee_currency").upper(),
        )
        if not isinstance(self.evidence_refs, tuple):
            raise TypeError("evidence_refs must be a tuple of strings")
        refs: list[str] = []
        for reference in self.evidence_refs:
            ref = _text(reference, name="evidence_ref")
            if ref in refs:
                raise ValueError("evidence_refs must be unique")
            refs.append(ref)
        object.__setattr__(self, "evidence_refs", tuple(refs))

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        provider_execution_id: str,
        client_order_id: str | None,
        instrument: str,
        quantity,
        price,
        fee_amount=0,
        fee_currency: str,
        trade_time: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> "ProviderFillEvidence":
        qty = _decimal(quantity, name="quantity")
        px = _decimal(price, name="price")
        fee = _decimal(fee_amount, name="fee_amount")
        if qty <= 0 or px <= 0:
            raise ValueError("quantity and price must be positive")
        _instant(trade_time, name="trade_time")
        return cls(
            provider_id=_text(provider_id, name="provider_id").upper(),
            account_id=_text(account_id, name="account_id"),
            environment=_environment(environment),
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
            evidence_refs=evidence_refs,
        )


@dataclass(frozen=True)
class ProviderActivityEvidence:
    provider_id: str
    account_id: str
    environment: str
    activity_id: str
    activity_type: str
    origin: str
    occurred_at: str
    instrument: str | None = None
    currency: str | None = None
    client_order_id: str | None = None
    provider_order_id: str | None = None
    provider_execution_id: str | None = None
    signed_amount: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, name="provider_id").upper(),
        )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(
            self,
            "environment",
            _environment(self.environment),
        )
        object.__setattr__(
            self,
            "activity_id",
            _text(self.activity_id, name="activity_id"),
        )
        object.__setattr__(
            self,
            "activity_type",
            _text(self.activity_type, name="activity_type").upper(),
        )
        origin = _text(self.origin, name="origin").upper()
        if origin not in _ACTIVITY_ORIGINS:
            raise ValueError(f"unsupported provider activity origin: {origin}")
        object.__setattr__(self, "origin", origin)
        instant = _instant(self.occurred_at, name="occurred_at")
        object.__setattr__(
            self,
            "occurred_at",
            instant.isoformat().replace("+00:00", "Z"),
        )
        for field in (
            "instrument",
            "client_order_id",
            "provider_order_id",
            "provider_execution_id",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _text(value, name=field))
        if self.currency is not None:
            object.__setattr__(
                self,
                "currency",
                _text(self.currency, name="currency").upper(),
            )
        if self.signed_amount is not None:
            object.__setattr__(
                self,
                "signed_amount",
                _decimal(self.signed_amount, name="signed_amount"),
            )

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        activity_id: str,
        activity_type: str,
        origin: str,
        occurred_at: str,
        instrument: str | None = None,
        currency: str | None = None,
        client_order_id: str | None = None,
        provider_order_id: str | None = None,
        provider_execution_id: str | None = None,
        signed_amount=None,
    ) -> "ProviderActivityEvidence":
        return cls(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            activity_id=activity_id,
            activity_type=activity_type,
            origin=origin,
            occurred_at=occurred_at,
            instrument=instrument,
            currency=currency,
            client_order_id=client_order_id,
            provider_order_id=provider_order_id,
            provider_execution_id=provider_execution_id,
            signed_amount=(
                None
                if signed_amount is None
                else _decimal(signed_amount, name="signed_amount")
            ),
        )


@dataclass(frozen=True)
class UnknownSubmission:
    attempt_id: str
    intent_id: str
    client_order_id: str
    provider_id: str
    account_id: str
    environment: str
    started_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attempt_id",
            _text(self.attempt_id, name="attempt_id"),
        )
        object.__setattr__(
            self,
            "intent_id",
            _text(self.intent_id, name="intent_id"),
        )
        object.__setattr__(
            self,
            "client_order_id",
            _text(self.client_order_id, name="client_order_id"),
        )
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id").upper()
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "environment", _environment(self.environment)
        )
        _instant(self.started_at, name="started_at")

    @classmethod
    def create(
        cls,
        *,
        attempt_id: str,
        intent_id: str,
        client_order_id: str,
        provider_id: str,
        account_id: str,
        environment: str,
        started_at: str,
    ) -> "UnknownSubmission":
        _instant(started_at, name="started_at")
        return cls(
            attempt_id=_text(attempt_id, name="attempt_id"),
            intent_id=_text(intent_id, name="intent_id"),
            client_order_id=_text(client_order_id, name="client_order_id"),
            provider_id=_text(provider_id, name="provider_id").upper(),
            account_id=_text(account_id, name="account_id"),
            environment=_environment(environment),
            started_at=started_at,
        )


@dataclass(frozen=True)
class SubmissionResolution:
    attempt_id: str
    intent_id: str
    client_order_id: str
    outcome: str
    evidence_reason: str
    provider_order_ids: tuple[str, ...] = ()
    provider_execution_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationResult:
    provider_id: str
    account_id: str
    environment: str
    complete: bool
    matched_execution_ids: tuple[str, ...]
    unexpected_execution_ids: tuple[str, ...]
    missing_local_execution_ids: tuple[str, ...]
    matched_working_client_order_ids: tuple[str, ...]
    unexpected_working_provider_order_ids: tuple[str, ...]
    missing_local_working_client_order_ids: tuple[str, ...]
    snapshot_consistent: bool
    provider_cash: Mapping[str, Decimal]
    provider_positions: Mapping[str, Decimal]
    snapshot_mode: str | None
    snapshot_query_started_at: str | None
    snapshot_query_completed_at: str | None
    cash_differences: Mapping[str, Decimal]
    position_differences: Mapping[str, Decimal]
    submission_resolutions: tuple[SubmissionResolution, ...]
    blocking_resources: tuple[str, ...]
    reasons: tuple[str, ...]
    matched_provider_activity_ids: tuple[str, ...] = ()
    unexpected_provider_activity_ids: tuple[str, ...] = ()
    missing_local_provider_activity_ids: tuple[str, ...] = ()
    manual_or_external_activity_ids: tuple[str, ...] = ()
    activity_coverage_complete: bool = True
    resource_availability: ResourceAvailabilityEvidence | None = None
    borrow_differences: Mapping[str, Decimal] | None = None
    settlement_differences: Mapping[str, Decimal] | None = None
    settlement_activity_complete: bool = True

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
        if not isinstance(key, str):
            raise TypeError(f"{name} keys must be strings")
        normalized_key = _text(key, name=f"{name} key")
        if normalized_key in result:
            raise ValueError(f"{name} keys must be unique after normalization")
        result[normalized_key] = _decimal(
            value, name=f"{name}[{normalized_key}]"
        )
    return result


def _absence_coverage_index(
    evidence: Sequence[CoverageSurfaceEvidence],
    *,
    provider_id: str,
    account_id: str,
    environment: str,
) -> dict[str, CoverageSurfaceEvidence]:
    result: dict[str, CoverageSurfaceEvidence] = {}
    for item in evidence:
        if not isinstance(item, CoverageSurfaceEvidence):
            raise TypeError(
                "absence_coverage must contain CoverageSurfaceEvidence"
            )
        if (
            item.provider_id != provider_id
            or item.account_id != account_id
            or item.environment != environment
        ):
            raise ValueError("absence coverage scope mismatch")
        if item.surface in result:
            raise ValueError(
                f"duplicate absence coverage surface: {item.surface}"
            )
        result[item.surface] = item
    return result


def _absence_is_proven(
    evidence: Mapping[str, CoverageSurfaceEvidence],
    submission_time: datetime,
    reconciliation_end: datetime,
) -> bool:
    return all(
        surface in evidence
        and evidence[surface].provider_semantics_exclude_execution
        and evidence[surface].proves_complete_window(
            submission_time,
            reconciliation_end,
        )
        for surface in _REQUIRED_ABSENCE_SURFACES
    )


def reconcile_account(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    local_cash: Mapping[str, object],
    provider_cash: Mapping[str, object],
    local_positions: Mapping[str, object],
    provider_positions: Mapping[str, object],
    local_execution_ids: Sequence[str],
    provider_fills: Sequence[ProviderFillEvidence],
    local_working_client_order_ids: Sequence[str] = (),
    provider_working_orders: Sequence[ProviderWorkingOrderEvidence] = (),
    snapshot_consistency: SnapshotConsistencyEvidence | None = None,
    unknown_submissions: Sequence[UnknownSubmission] = (),
    searched_client_order_ids: Sequence[str] = (),
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    absence_coverage: Sequence[CoverageSurfaceEvidence] = (),
    local_provider_activity_ids: Sequence[str] = (),
    provider_activities: Sequence[ProviderActivityEvidence] = (),
    provider_activity_provider_id: str | None = None,
    provider_activity_account_id: str | None = None,
    activity_coverage: CoverageSurfaceEvidence | None = None,
    require_activity_reconciliation: bool = False,
    cash_tolerance: Mapping[str, object] | None = None,
    position_tolerance: Mapping[str, object] | None = None,
    resource_availability: ResourceAvailabilityEvidence | None = None,
    local_borrowed_resources: Mapping[str, object] | None = None,
    provider_borrowed_resources: Mapping[str, object] | None = None,
    active_borrow_recall_resources: Sequence[str] = (),
    local_settled_cash: Mapping[str, object] | None = None,
    provider_settled_cash: Mapping[str, object] | None = None,
    local_unsettled_receivable: Mapping[str, object] | None = None,
    provider_unsettled_receivable: Mapping[str, object] | None = None,
    local_unsettled_payable: Mapping[str, object] | None = None,
    provider_unsettled_payable: Mapping[str, object] | None = None,
    settlement_activity_complete: bool | None = None,
) -> ReconciliationResult:
    """Compare local and provider truth without inventing absence evidence.

    A previously UNKNOWN send becomes PROVEN_ABSENT only when the provider
    lookup explicitly searched its client order id and open-order, order-history,
    execution and activity surfaces each provide complete coverage across the
    submission instant, their consistency horizons have elapsed, and provider
    semantics explicitly exclude execution. Otherwise it remains UNKNOWN and
    blocks affected new risk.
    """

    provider_scope = _text(provider_id, name="provider_id").upper()
    account_scope = _text(account_id, name="account_id")
    environment_scope = _environment(environment)
    if not isinstance(pagination_complete, bool):
        raise TypeError("pagination_complete must be boolean")
    if not isinstance(require_activity_reconciliation, bool):
        raise TypeError("require_activity_reconciliation must be boolean")
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
    provider_client_fill_times: dict[str, list[datetime]] = {}
    provider_client_fills: dict[str, list[ProviderFillEvidence]] = {}
    for fill in provider_fills:
        if not isinstance(fill, ProviderFillEvidence):
            raise TypeError("provider_fills must contain ProviderFillEvidence")
        if fill.provider_id != provider_scope:
            raise ValueError("provider fill evidence provider_id mismatch")
        if fill.account_id != account_scope:
            raise ValueError("provider fill evidence account_id mismatch")
        if fill.environment != environment_scope:
            raise ValueError("provider fill evidence environment mismatch")
        if fill.provider_execution_id in provider_by_id:
            if provider_by_id[fill.provider_execution_id] != fill:
                raise ValueError("provider execution id has conflicting observations")
            continue
        provider_by_id[fill.provider_execution_id] = fill
        if fill.client_order_id is not None:
            provider_client_ids.add(fill.client_order_id)
            provider_client_fill_times.setdefault(fill.client_order_id, []).append(
                _instant(fill.trade_time, name="provider_fill.trade_time")
            )
            provider_client_fills.setdefault(fill.client_order_id, []).append(fill)

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

    provider_working_by_id: dict[str, ProviderWorkingOrderEvidence] = {}
    provider_working_by_client_id: dict[str, ProviderWorkingOrderEvidence] = {}
    for order in provider_working_orders:
        if not isinstance(order, ProviderWorkingOrderEvidence):
            raise TypeError(
                "provider_working_orders must contain ProviderWorkingOrderEvidence"
            )
        if order.provider_id != provider_scope:
            raise ValueError("provider working-order evidence provider_id mismatch")
        if order.account_id != account_scope:
            raise ValueError("provider working-order evidence account_id mismatch")
        if order.environment != environment_scope:
            raise ValueError("provider working-order evidence environment mismatch")
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

    local_working_set = set(local_working_ids)
    provider_working_client_ids = set(provider_working_by_client_id)
    matched_working = tuple(
        sorted(local_working_set & provider_working_client_ids)
    )
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
    snapshot_window_covered = False
    snapshot_started: datetime | None = None
    snapshot_completed: datetime | None = None
    if snapshot_consistency is not None:
        if (
            snapshot_consistency.provider_id != provider_scope
            or snapshot_consistency.account_id != account_scope
            or snapshot_consistency.environment != environment_scope
        ):
            raise ValueError("provider snapshot consistency scope mismatch")
        snapshot_started = _instant(
            snapshot_consistency.query_started_at,
            name="snapshot_consistency.query_started_at",
        )
        snapshot_completed = _instant(
            snapshot_consistency.query_completed_at,
            name="snapshot_consistency.query_completed_at",
        )
        snapshot_window_covered = (
            start <= snapshot_started <= snapshot_completed <= end
        )
    snapshot_is_consistent = bool(
        snapshot_consistency is not None
        and snapshot_consistency.consistent
        and snapshot_window_covered
    )

    if resource_availability is not None:
        if not isinstance(resource_availability, ResourceAvailabilityEvidence):
            raise TypeError(
                "resource_availability must be ResourceAvailabilityEvidence"
            )
        if (
            resource_availability.provider_id != provider_scope
            or resource_availability.account_id != account_scope
            or resource_availability.environment != environment_scope
        ):
            raise ValueError("resource availability scope mismatch")
        if snapshot_consistency is None:
            raise ValueError(
                "resource availability requires snapshot consistency evidence"
            )
        resource_started = _instant(
            resource_availability.query_started_at,
            name="resource_availability.query_started_at",
        )
        resource_completed = _instant(
            resource_availability.query_completed_at,
            name="resource_availability.query_completed_at",
        )
        snapshot_started = _instant(
            snapshot_consistency.query_started_at,
            name="snapshot_consistency.query_started_at",
        )
        snapshot_completed = _instant(
            snapshot_consistency.query_completed_at,
            name="snapshot_consistency.query_completed_at",
        )
        if (
            resource_started != snapshot_started
            or resource_completed != snapshot_completed
        ):
            raise ValueError(
                "resource availability snapshot cut differs from reconciliation"
            )

    local_borrowed = _amount_map(
        local_borrowed_resources or {},
        name="local_borrowed_resources",
    )
    provider_borrowed = _amount_map(
        provider_borrowed_resources or {},
        name="provider_borrowed_resources",
    )
    active_recalls = tuple(
        _text(value, name="active_borrow_recall_resource")
        for value in active_borrow_recall_resources
    )
    if len(active_recalls) != len(set(active_recalls)):
        raise ValueError("active_borrow_recall_resources must be unique")

    borrow_resources = (
        set(local_borrowed)
        | set(provider_borrowed)
        | set(active_recalls)
    )
    borrow_details = (
        {}
        if resource_availability is None
        else resource_availability.resource_details
    )
    for resource in sorted(borrow_resources):
        if not resource.startswith("BORROW:"):
            raise ValueError(
                "borrow reconciliation resources must use canonical BORROW identity"
            )
        detail = borrow_details.get(resource)
        if (
            not isinstance(detail, Mapping)
            or detail.get("resource_type") != "SECURITIES_BORROW"
        ):
            raise ValueError(
                "borrow reconciliation requires typed availability evidence"
            )

    borrow_differences: dict[str, Decimal] = {}
    for resource in sorted(set(local_borrowed) | set(provider_borrowed)):
        difference = provider_borrowed.get(
            resource,
            Decimal("0"),
        ) - local_borrowed.get(resource, Decimal("0"))
        if difference != 0:
            borrow_differences[resource] = difference

    settlement_inputs = (
        local_settled_cash,
        provider_settled_cash,
        local_unsettled_receivable,
        provider_unsettled_receivable,
        local_unsettled_payable,
        provider_unsettled_payable,
    )
    settlement_requested = any(value is not None for value in settlement_inputs)
    if settlement_requested and any(value is None for value in settlement_inputs):
        raise ValueError(
            "settlement reconciliation requires all local/provider settlement surfaces"
        )
    if settlement_requested and settlement_activity_complete is None:
        raise ValueError(
            "settlement reconciliation requires explicit activity completeness"
        )
    if settlement_activity_complete is not None and not isinstance(
        settlement_activity_complete, bool
    ):
        raise TypeError("settlement_activity_complete must be boolean or None")

    local_settled = _amount_map(
        local_settled_cash or {}, name="local_settled_cash"
    )
    provider_settled = _amount_map(
        provider_settled_cash or {}, name="provider_settled_cash"
    )
    local_receivable = _amount_map(
        local_unsettled_receivable or {},
        name="local_unsettled_receivable",
    )
    provider_receivable = _amount_map(
        provider_unsettled_receivable or {},
        name="provider_unsettled_receivable",
    )
    local_payable = _amount_map(
        local_unsettled_payable or {},
        name="local_unsettled_payable",
    )
    provider_payable = _amount_map(
        provider_unsettled_payable or {},
        name="provider_unsettled_payable",
    )
    if any(
        value < 0
        for mapping in (
            local_receivable,
            provider_receivable,
            local_payable,
            provider_payable,
        )
        for value in mapping.values()
    ):
        raise ValueError("unsettled settlement surfaces must be non-negative")

    settlement_differences: dict[str, Decimal] = {}
    if settlement_requested and settlement_activity_complete:
        for prefix, local_map, provider_map in (
            ("SETTLED", local_settled, provider_settled),
            ("RECEIVABLE", local_receivable, provider_receivable),
            ("PAYABLE", local_payable, provider_payable),
        ):
            for currency in sorted(set(local_map) | set(provider_map)):
                difference = provider_map.get(
                    currency, Decimal("0")
                ) - local_map.get(currency, Decimal("0"))
                if difference != 0:
                    settlement_differences[f"{prefix}:{currency}"] = difference

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

    local_activity_ids = tuple(
        _text(value, name="local_provider_activity_id")
        for value in local_provider_activity_ids
    )
    if len(local_activity_ids) != len(set(local_activity_ids)):
        raise ValueError("local_provider_activity_ids must be unique")

    provider_activity_by_id: dict[str, ProviderActivityEvidence] = {}
    activity_provider = (
        None
        if provider_activity_provider_id is None
        else _text(provider_activity_provider_id, name="provider_activity_provider_id").upper()
    )
    activity_account = (
        None
        if provider_activity_account_id is None
        else _text(provider_activity_account_id, name="provider_activity_account_id")
    )
    if (activity_provider is None) != (activity_account is None):
        raise ValueError(
            "provider activity scope must provide provider/account together"
        )
    if activity_provider is not None and activity_provider != provider_scope:
        raise ValueError("provider activity scope differs from reconciliation provider_id")
    if activity_account is not None and activity_account != account_scope:
        raise ValueError("provider activity scope differs from reconciliation account_id")
    for activity in provider_activities:
        if not isinstance(activity, ProviderActivityEvidence):
            raise TypeError(
                "provider_activities must contain ProviderActivityEvidence"
            )
        if activity.provider_id != provider_scope:
            raise ValueError("provider activity evidence provider_id mismatch")
        if activity.account_id != account_scope:
            raise ValueError("provider activity evidence account_id mismatch")
        if activity.environment != environment_scope:
            raise ValueError("provider activity evidence environment mismatch")
        existing = provider_activity_by_id.get(activity.activity_id)
        if existing is not None:
            if existing != activity:
                raise ValueError("provider activity id has conflicting observations")
            continue
        provider_activity_by_id[activity.activity_id] = activity

    provider_activity_ids = set(provider_activity_by_id)
    local_activity_id_set = set(local_activity_ids)
    matched_activities = tuple(
        sorted(local_activity_id_set & provider_activity_ids)
    )
    unexpected_activities = tuple(
        sorted(provider_activity_ids - local_activity_id_set)
    )
    missing_local_activities = tuple(
        sorted(local_activity_id_set - provider_activity_ids)
    )
    manual_or_external_activities = tuple(
        sorted(
            activity_id
            for activity_id in unexpected_activities
            if provider_activity_by_id[activity_id].origin
            in {"MANUAL", "EXTERNAL", "UNKNOWN"}
        )
    )

    if activity_coverage is not None and not isinstance(
        activity_coverage, CoverageSurfaceEvidence
    ):
        raise TypeError("activity_coverage must be CoverageSurfaceEvidence")
    if activity_coverage is not None and (
        activity_coverage.provider_id != provider_scope
        or activity_coverage.account_id != account_scope
        or activity_coverage.environment != environment_scope
    ):
        raise ValueError("provider activity coverage scope mismatch")
    activity_coverage_complete = True
    if require_activity_reconciliation:
        activity_coverage_complete = bool(
            activity_coverage is not None
            and activity_coverage.surface == "ACTIVITIES"
            and activity_coverage.proves_complete_window(start, end)
        )

    searched = {
        _text(value, name="searched_client_order_id")
        for value in searched_client_order_ids
    }
    absence_evidence = _absence_coverage_index(
        absence_coverage,
        provider_id=provider_scope,
        account_id=account_scope,
        environment=environment_scope,
    )
    # UNKNOWN submission identity is financial truth: one durable attempt may
    # appear at most once, and one provider-scoped client order id may belong
    # to only one attempt.  Resolve malformed/corrupted local history before
    # consulting provider observations so one provider fact can never resolve
    # multiple incompatible local submissions.
    normalized_unknown_submissions: list[UnknownSubmission] = []
    unknown_by_attempt: dict[str, UnknownSubmission] = {}
    unknown_by_client_order_id: dict[str, UnknownSubmission] = {}
    for submission in unknown_submissions:
        if not isinstance(submission, UnknownSubmission):
            raise TypeError("unknown_submissions must contain UnknownSubmission")
        if (
            submission.provider_id != provider_scope
            or submission.account_id != account_scope
            or submission.environment != environment_scope
        ):
            raise ValueError("unknown submission scope mismatch")

        existing_attempt = unknown_by_attempt.get(submission.attempt_id)
        if existing_attempt is not None:
            if existing_attempt != submission:
                raise ValueError(
                    "unknown submission attempt_id has conflicting observations"
                )
            # Exact replay of the same immutable attempt is idempotent.
            continue

        existing_client = unknown_by_client_order_id.get(
            submission.client_order_id
        )
        if existing_client is not None:
            raise ValueError(
                "unknown submission client_order_id is reused across attempts"
            )

        unknown_by_attempt[submission.attempt_id] = submission
        unknown_by_client_order_id[submission.client_order_id] = submission
        normalized_unknown_submissions.append(submission)

    resolutions: list[SubmissionResolution] = []
    for submission in normalized_unknown_submissions:
        submission_time = _instant(
            submission.started_at, name="unknown_submission.started_at"
        )
        provider_working = provider_working_by_client_id.get(
            submission.client_order_id
        )
        provider_order_ids: tuple[str, ...] = ()
        provider_execution_ids: tuple[str, ...] = ()
        matching_fill_times = provider_client_fill_times.get(
            submission.client_order_id, ()
        )
        matching_fills = provider_client_fills.get(
            submission.client_order_id, ()
        )
        provider_execution_ids = tuple(
            sorted(
                {
                    fill.provider_execution_id
                    for fill in matching_fills
                    if submission_time
                    <= _instant(fill.trade_time, name="provider_fill.trade_time")
                    <= end
                }
            )
        )
        causal_execution_observed = bool(provider_execution_ids)
        working_snapshot_is_causal = bool(
            provider_working is not None
            and snapshot_is_consistent
            and snapshot_started is not None
            and snapshot_started >= submission_time
        )
        if causal_execution_observed:
            outcome = "OBSERVED_EXECUTION"
            reason = "provider_execution_observed_after_submission"
        elif working_snapshot_is_causal:
            outcome = "OBSERVED_WORKING_ORDER"
            reason = "provider_working_order_observed_in_post_submission_snapshot"
            provider_order_ids = (provider_working.provider_order_id,)
        elif provider_working is not None:
            outcome = "UNKNOWN"
            reason = "provider_working_order_snapshot_not_causal_for_submission"
        elif submission.client_order_id in provider_client_ids:
            outcome = "UNKNOWN"
            reason = "matching_provider_execution_outside_submission_window"
        elif (
            snapshot_is_consistent
            and pagination_complete
            and submission.client_order_id in searched
            and start <= submission_time <= end
            and _absence_is_proven(
                absence_evidence,
                submission_time,
                end,
            )
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
                intent_id=submission.intent_id,
                client_order_id=submission.client_order_id,
                outcome=outcome,
                evidence_reason=reason,
                provider_order_ids=provider_order_ids,
                provider_execution_ids=provider_execution_ids,
            )
        )

    blocking: set[str] = set()
    reasons: list[str] = []
    for resource in borrow_differences:
        blocking.add(resource)
    if borrow_differences:
        reasons.append(
            "provider/local securities-borrow obligation differs"
        )
    for resource in active_recalls:
        blocking.add(resource)
    if active_recalls:
        reasons.append(
            "active provider securities-borrow recall blocks increased short risk"
        )
    if not snapshot_is_consistent:
        blocking.add("ACCOUNT")
        if snapshot_consistency is None:
            reasons.append("provider snapshot consistency is not evidenced")
        elif not snapshot_window_covered:
            reasons.append(
                "provider snapshot query window is outside reconciliation coverage"
            )
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
    if require_activity_reconciliation and not activity_coverage_complete:
        blocking.add("ACCOUNT")
        reasons.append(
            "provider activity coverage is incomplete for the reconciliation window"
        )
    if unexpected_activities:
        for activity_id in unexpected_activities:
            activity = provider_activity_by_id[activity_id]
            scoped = False
            if activity.instrument is not None:
                blocking.add(f"INSTRUMENT:{activity.instrument}")
                scoped = True
            if activity.currency is not None:
                blocking.add(f"CASH:{activity.currency}")
                scoped = True
            if not scoped:
                blocking.add("ACCOUNT")
        reasons.append(
            "provider contains account activity absent from local truth"
        )
    if manual_or_external_activities:
        reasons.append(
            "manual, external or unknown-origin provider activity requires import or explicit reconciliation"
        )
    if missing_local_activities:
        blocking.add("ACCOUNT")
        reasons.append(
            "local provider activity identities are absent from provider activity evidence"
        )
    if settlement_requested and not settlement_activity_complete:
        blocking.add("ACCOUNT")
        reasons.append(
            "provider settlement/activity coverage is incomplete"
        )
    if settlement_differences:
        for settlement_resource in settlement_differences:
            _, currency = settlement_resource.split(":", 1)
            blocking.add(f"CASH:{currency}")
        reasons.append(
            "provider/local settled or pending cash differs"
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
        and activity_coverage_complete
        and not unexpected_activities
        and not missing_local_activities
        and not cash_differences
        and not position_differences
        and (
            not settlement_requested
            or (
                settlement_activity_complete is True
                and not settlement_differences
            )
        )
        and all(item.outcome != "UNKNOWN" for item in resolutions)
    )
    return ReconciliationResult(
        provider_id=provider_scope,
        account_id=account_scope,
        environment=environment_scope,
        complete=complete,
        matched_execution_ids=matched,
        unexpected_execution_ids=unexpected,
        missing_local_execution_ids=missing,
        matched_working_client_order_ids=matched_working,
        unexpected_working_provider_order_ids=unexpected_working,
        missing_local_working_client_order_ids=missing_local_working,
        snapshot_consistent=snapshot_is_consistent,
        provider_cash=MappingProxyType(provider_cash_map),
        provider_positions=MappingProxyType(provider_position_map),
        snapshot_mode=(
            snapshot_consistency.mode
            if snapshot_consistency is not None
            else None
        ),
        snapshot_query_started_at=(
            snapshot_consistency.query_started_at
            if snapshot_consistency is not None
            else None
        ),
        snapshot_query_completed_at=(
            snapshot_consistency.query_completed_at
            if snapshot_consistency is not None
            else None
        ),
        cash_differences=MappingProxyType(cash_differences),
        position_differences=MappingProxyType(position_differences),
        submission_resolutions=tuple(resolutions),
        blocking_resources=tuple(sorted(blocking)),
        reasons=tuple(reasons),
        matched_provider_activity_ids=matched_activities,
        unexpected_provider_activity_ids=unexpected_activities,
        missing_local_provider_activity_ids=missing_local_activities,
        manual_or_external_activity_ids=manual_or_external_activities,
        activity_coverage_complete=activity_coverage_complete,
        resource_availability=resource_availability,
        borrow_differences=MappingProxyType(borrow_differences),
        settlement_differences=MappingProxyType(settlement_differences),
        settlement_activity_complete=(
            True
            if not settlement_requested
            else bool(settlement_activity_complete)
        ),
    )
