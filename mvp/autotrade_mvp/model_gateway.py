"""Deterministic, policy-bounded model routing for the simulated AutoTrade runtime.

This foundation deliberately performs no network calls. It decides whether a model
call is admissible and reserves a hard compute-cost ceiling before any provider
adapter is invoked by future integration work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable


class RoutingMode(StrEnum):
    ZERO = "ZERO"
    LOCAL_ONLY = "LOCAL_ONLY"
    FIXED = "FIXED"
    ALLOWLIST = "ALLOWLIST"
    DYNAMIC = "DYNAMIC"


class RouteStatus(StrEnum):
    ADMITTED = "ADMITTED"
    NO_MODEL = "NO_MODEL"
    REJECTED = "REJECTED"


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _identifier_tuple(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{field} must be a tuple")
    normalized = tuple(_identifier(value, field) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def _exact_decimal(value: Decimal | str | int, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return result


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    model_id: str
    provider_id: str
    revision: str | None
    remote: bool
    estimated_cost: Decimal
    latency_ms: int
    quality_score: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider_id"))
        if self.revision is not None:
            object.__setattr__(self, "revision", _identifier(self.revision, "revision"))
        if type(self.remote) is not bool:
            raise TypeError("remote must be boolean")
        if (
            not isinstance(self.latency_ms, int)
            or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise ValueError("latency must be a non-negative integer")
        object.__setattr__(
            self,
            "estimated_cost",
            _exact_decimal(self.estimated_cost, "estimated cost"),
        )
        object.__setattr__(
            self,
            "quality_score",
            _exact_decimal(self.quality_score, "quality score"),
        )
        if self.estimated_cost < 0:
            raise ValueError("estimated cost cannot be negative")
        if not (Decimal("0") <= self.quality_score <= Decimal("1")):
            raise ValueError("quality score must be within [0, 1]")


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    mode: RoutingMode
    allowed_model_ids: tuple[str, ...] = ()
    fixed_model_id: str | None = None
    allow_remote: bool = False
    maximum_cost: Decimal = Decimal("0")
    maximum_latency_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RoutingMode):
            raise TypeError("mode must be a RoutingMode")
        object.__setattr__(
            self,
            "allowed_model_ids",
            _identifier_tuple(self.allowed_model_ids, "allowed_model_ids"),
        )
        if self.fixed_model_id is not None:
            object.__setattr__(
                self,
                "fixed_model_id",
                _identifier(self.fixed_model_id, "fixed_model_id"),
            )
        if type(self.allow_remote) is not bool:
            raise TypeError("allow_remote must be boolean")
        object.__setattr__(
            self,
            "maximum_cost",
            _exact_decimal(self.maximum_cost, "maximum cost"),
        )
        if self.maximum_cost < 0:
            raise ValueError("maximum cost cannot be negative")
        if self.maximum_latency_ms is not None and (
            not isinstance(self.maximum_latency_ms, int)
            or isinstance(self.maximum_latency_ms, bool)
            or self.maximum_latency_ms < 0
        ):
            raise ValueError("maximum latency must be a non-negative integer")
        if self.mode is RoutingMode.FIXED and not self.fixed_model_id:
            raise ValueError("fixed mode requires fixed_model_id")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    request_id: str
    allowed_model_ids: tuple[str, ...]
    privacy_remote_allowed: bool
    budget_remaining: Decimal
    deadline_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(
            self,
            "allowed_model_ids",
            _identifier_tuple(self.allowed_model_ids, "allowed_model_ids"),
        )
        if type(self.privacy_remote_allowed) is not bool:
            raise TypeError("privacy_remote_allowed must be boolean")
        if not isinstance(self.deadline_utc, datetime) or self.deadline_utc.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        object.__setattr__(
            self,
            "budget_remaining",
            _exact_decimal(self.budget_remaining, "budget remaining"),
        )
        if self.budget_remaining < 0:
            raise ValueError("budget remaining cannot be negative")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    status: RouteStatus
    model_id: str | None
    provider_id: str | None
    revision: str | None
    reserved_cost: Decimal
    reason: str


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_descriptors(descriptors: Iterable[ModelDescriptor]) -> dict[str, ModelDescriptor]:
    result: dict[str, ModelDescriptor] = {}
    for descriptor in descriptors:
        if descriptor.model_id in result:
            raise ValueError(f"duplicate model descriptor: {descriptor.model_id}")
        result[descriptor.model_id] = descriptor
    return result


def route_model(
    policy: RoutingPolicy,
    request: ModelRequest,
    descriptors: Iterable[ModelDescriptor],
    *,
    now_utc: datetime | None = None,
) -> RouteDecision:
    """Return one deterministic admission decision without performing inference."""

    now = now_utc or _now_utc()
    if now.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    if now >= request.deadline_utc:
        return RouteDecision(RouteStatus.REJECTED, None, None, None, Decimal("0"), "deadline_expired")
    if policy.mode is RoutingMode.ZERO:
        return RouteDecision(RouteStatus.NO_MODEL, None, None, None, Decimal("0"), "zero_model_policy")

    by_id = _dedupe_descriptors(descriptors)
    request_allowed = set(request.allowed_model_ids)
    policy_allowed = set(policy.allowed_model_ids)

    if policy.mode is RoutingMode.FIXED:
        candidate_ids = [policy.fixed_model_id] if policy.fixed_model_id else []
    elif policy.mode in (RoutingMode.ALLOWLIST, RoutingMode.DYNAMIC, RoutingMode.LOCAL_ONLY):
        candidate_ids = sorted(policy_allowed)
    else:
        candidate_ids = []

    candidates: list[ModelDescriptor] = []
    for model_id in candidate_ids:
        if model_id not in request_allowed:
            continue
        descriptor = by_id.get(model_id)
        if descriptor is None:
            continue
        if descriptor.remote:
            if policy.mode is RoutingMode.LOCAL_ONLY:
                continue
            if not policy.allow_remote or not request.privacy_remote_allowed:
                continue
        if policy.maximum_latency_ms is not None and descriptor.latency_ms > policy.maximum_latency_ms:
            continue
        hard_budget = min(policy.maximum_cost, request.budget_remaining)
        if descriptor.estimated_cost > hard_budget:
            continue
        candidates.append(descriptor)

    if not candidates:
        return RouteDecision(RouteStatus.NO_MODEL, None, None, None, Decimal("0"), "no_admissible_model")

    if policy.mode in (RoutingMode.FIXED, RoutingMode.ALLOWLIST, RoutingMode.LOCAL_ONLY):
        chosen = candidates[0]
    else:
        # Higher measured quality first, then lower end-to-end estimated cost,
        # lower latency, and finally stable model identity for deterministic ties.
        chosen = sorted(
            candidates,
            key=lambda item: (
                -item.quality_score,
                item.estimated_cost,
                item.latency_ms,
                item.model_id,
            ),
        )[0]

    return RouteDecision(
        RouteStatus.ADMITTED,
        chosen.model_id,
        chosen.provider_id,
        chosen.revision,
        chosen.estimated_cost,
        "admitted",
    )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    ceiling: Decimal
    reserved: Decimal
    incurred: Decimal
    estimated_unbilled: Decimal

    @property
    def available(self) -> Decimal:
        used = self.reserved + self.incurred + self.estimated_unbilled
        remaining = self.ceiling - used
        return remaining if remaining > 0 else Decimal("0")


class BudgetLedger:
    """In-memory accounting primitive for model-call cost ceilings.

    Persistence is intentionally deferred to the journal integration work.
    """

    def __init__(self, ceiling: Decimal) -> None:
        ceiling = _exact_decimal(ceiling, "budget ceiling")
        if ceiling < 0:
            raise ValueError("budget ceiling cannot be negative")
        self._ceiling = ceiling
        self._reserved: dict[str, Decimal] = {}
        self._incurred = Decimal("0")
        self._estimated_unbilled_by_request: dict[str, Decimal] = {}
        self._settled_requests: set[str] = set()
        self._reconciled_bills: dict[str, tuple[str, Decimal]] = {}

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            ceiling=self._ceiling,
            reserved=sum(self._reserved.values(), Decimal("0")),
            incurred=self._incurred,
            estimated_unbilled=sum(
                self._estimated_unbilled_by_request.values(),
                Decimal("0"),
            ),
        )

    def reserve(self, request_id: str, amount: Decimal) -> None:
        request_id = _identifier(request_id, "request_id")
        amount = _exact_decimal(amount, "reservation")
        if amount < 0:
            raise ValueError("reservation cannot be negative")
        prior = self._reserved.get(request_id)
        if prior is not None:
            if prior != amount:
                raise ValueError("reservation conflict")
            return
        if amount > self.snapshot().available:
            raise ValueError("budget exhausted")
        self._reserved[request_id] = amount

    def release(self, request_id: str) -> Decimal:
        request_id = _identifier(request_id, "request_id")
        if request_id in self._settled_requests:
            raise ValueError("cannot release a request after the call boundary")
        return self._reserved.pop(request_id, Decimal("0"))

    def settle(
        self,
        request_id: str,
        *,
        incurred: Decimal,
        estimated_unbilled: Decimal = Decimal("0"),
    ) -> None:
        request_id = _identifier(request_id, "request_id")
        incurred = _exact_decimal(incurred, "incurred cost")
        estimated_unbilled = _exact_decimal(
            estimated_unbilled,
            "estimated unbilled cost",
        )
        if incurred < 0 or estimated_unbilled < 0:
            raise ValueError("costs cannot be negative")
        reserved = self._reserved.pop(request_id, None)
        if reserved is None:
            raise ValueError("unknown reservation")
        if incurred + estimated_unbilled > reserved:
            self._reserved[request_id] = reserved
            raise ValueError("settlement exceeds reserved ceiling")
        self._incurred += incurred
        self._estimated_unbilled_by_request[request_id] = estimated_unbilled
        self._settled_requests.add(request_id)

    def reconcile_unbilled(
        self,
        *,
        billing_id: str,
        request_id: str,
        billed: Decimal,
    ) -> None:
        identifier = _identifier(billing_id, "billing_id")
        request = _identifier(request_id, "request_id")
        billed = _exact_decimal(billed, "billed cost")
        if billed < 0:
            raise ValueError("billed cost cannot be negative")
        prior = self._reconciled_bills.get(identifier)
        if prior is not None:
            if prior != (request, billed):
                raise ValueError("billing reconciliation conflict")
            return
        if request not in self._settled_requests:
            raise ValueError("billing has no matching settled request")

        # Provider billing is observed economic truth. It must not be discarded
        # merely because the actual charge exceeded our prior estimate. Reduce
        # only this request's remaining estimate, then record the full bill.
        estimate = self._estimated_unbilled_by_request.get(request, Decimal("0"))
        remaining_estimate = estimate - min(estimate, billed)
        self._estimated_unbilled_by_request[request] = remaining_estimate
        self._incurred += billed
        self._reconciled_bills[identifier] = (request, billed)
