"""Deterministic, policy-bounded model routing for the simulated AutoTrade runtime.

This foundation deliberately performs no network calls. It decides whether a model
call is admissible and reserves a hard compute-cost ceiling before any provider
adapter is invoked by future integration work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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
        if not self.model_id or not self.provider_id:
            raise ValueError("model and provider identifiers are required")
        if self.estimated_cost < 0:
            raise ValueError("estimated cost cannot be negative")
        if self.latency_ms < 0:
            raise ValueError("latency cannot be negative")
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
        if self.maximum_cost < 0:
            raise ValueError("maximum cost cannot be negative")
        if self.maximum_latency_ms is not None and self.maximum_latency_ms < 0:
            raise ValueError("maximum latency cannot be negative")
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
        if not self.request_id:
            raise ValueError("request_id is required")
        if self.budget_remaining < 0:
            raise ValueError("budget remaining cannot be negative")
        if self.deadline_utc.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")


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
