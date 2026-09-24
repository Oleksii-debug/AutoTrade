"""Deterministic, privacy-aware model routing with explicit cost budgets.

This module is provider-neutral. It never expands trading authority, never
routes restricted data to an ineligible remote model, and always supports an
explicit no-model outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


_VALID_MODES = {"zero", "local", "fixed", "allowlist", "dynamic"}


@dataclass(frozen=True)
class ModelRoute:
    route_id: str
    provider: str
    model: str
    locality: str
    input_cost_per_million: Decimal = Decimal("0")
    output_cost_per_million: Decimal = Decimal("0")
    allowed_data_classes: frozenset[str] = frozenset({"public"})
    expected_latency_ms: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.locality not in {"local", "remote"}:
            raise ValueError("locality must be local or remote")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("model costs cannot be negative")
        if self.expected_latency_ms is not None and self.expected_latency_ms < 0:
            raise ValueError("expected_latency_ms cannot be negative")
        if not self.route_id or not self.provider or not self.model:
            raise ValueError("route_id, provider and model are required")


@dataclass(frozen=True)
class RoutingPolicy:
    mode: str
    max_request_cost: Decimal = Decimal("0")
    max_total_cost: Decimal | None = None
    allow_remote: bool = False
    fixed_route_id: str | None = None
    allowed_route_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(f"unsupported routing mode: {self.mode}")
        if self.max_request_cost < 0:
            raise ValueError("max_request_cost cannot be negative")
        if self.max_total_cost is not None and self.max_total_cost < 0:
            raise ValueError("max_total_cost cannot be negative")
        if self.mode == "fixed" and not self.fixed_route_id:
            raise ValueError("fixed mode requires fixed_route_id")


@dataclass(frozen=True)
class RouteRequest:
    data_class: str
    input_tokens: int
    output_tokens: int
    deadline_ms: int | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        if not self.data_class:
            raise ValueError("data_class is required")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token estimates cannot be negative")
        if self.deadline_ms is not None and self.deadline_ms < 0:
            raise ValueError("deadline_ms cannot be negative")
        if not isinstance(self.cancelled, bool):
            raise TypeError("cancelled must be a boolean")


@dataclass(frozen=True)
class RoutingDecision:
    route_id: str | None
    provider: str | None
    model: str | None
    locality: str | None
    estimated_cost: Decimal
    spent_before: Decimal
    projected_total_cost: Decimal
    deadline_ms: int | None
    execute: bool
    reason: str


def _spent(value: Decimal | str | int) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError("spent_cost must be a finite non-negative decimal")
    return result


def _no_route(request: RouteRequest, spent_cost: Decimal, reason: str) -> RoutingDecision:
    return RoutingDecision(
        route_id=None,
        provider=None,
        model=None,
        locality=None,
        estimated_cost=Decimal("0"),
        spent_before=spent_cost,
        projected_total_cost=spent_cost,
        deadline_ms=request.deadline_ms,
        execute=False,
        reason=reason,
    )


def _estimated_cost(route: ModelRoute, request: RouteRequest) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(request.input_tokens) * route.input_cost_per_million / million
        + Decimal(request.output_tokens) * route.output_cost_per_million / million
    )


def _eligible(route: ModelRoute, request: RouteRequest, policy: RoutingPolicy) -> bool:
    if not route.enabled:
        return False
    if request.data_class not in route.allowed_data_classes:
        return False
    if route.locality == "remote" and not policy.allow_remote:
        return False
    if policy.mode == "local" and route.locality != "local":
        return False
    if policy.mode == "fixed" and route.route_id != policy.fixed_route_id:
        return False
    if policy.mode == "allowlist" and route.route_id not in policy.allowed_route_ids:
        return False
    if (
        request.deadline_ms is not None
        and route.expected_latency_ms is not None
        and route.expected_latency_ms > request.deadline_ms
    ):
        return False
    return True


def select_route(
    routes: Iterable[ModelRoute],
    policy: RoutingPolicy,
    request: RouteRequest,
    *,
    spent_cost: Decimal | str | int = Decimal("0"),
) -> RoutingDecision:
    """Select one eligible route or return an explicit no-model decision."""

    spent = _spent(spent_cost)

    if request.cancelled:
        return _no_route(request, spent, "request cancelled")
    if policy.mode == "zero":
        return _no_route(request, spent, "zero-model policy")
    if policy.max_total_cost is not None and spent >= policy.max_total_cost:
        return _no_route(request, spent, "total model budget exhausted")

    candidates: list[tuple[Decimal, str, ModelRoute]] = []
    for route in routes:
        if not _eligible(route, request, policy):
            continue
        cost = _estimated_cost(route, request)
        if cost > policy.max_request_cost:
            continue
        if policy.max_total_cost is not None and spent + cost > policy.max_total_cost:
            continue
        candidates.append((cost, route.route_id, route))

    if not candidates:
        return _no_route(
            request,
            spent,
            "no eligible route within privacy, deadline and budget policy",
        )

    if policy.mode == "fixed":
        cost, _, route = candidates[0]
        reason = "fixed eligible route"
    else:
        cost, _, route = min(candidates, key=lambda item: (item[0], item[1]))
        reason = "lowest-cost eligible route"

    return RoutingDecision(
        route_id=route.route_id,
        provider=route.provider,
        model=route.model,
        locality=route.locality,
        estimated_cost=cost,
        spent_before=spent,
        projected_total_cost=spent + cost,
        deadline_ms=request.deadline_ms,
        execute=True,
        reason=reason,
    )
