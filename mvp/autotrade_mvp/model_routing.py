"""Deterministic, privacy-aware model routing with explicit cost budgets.

This module is intentionally provider-neutral. It never expands authority, never
routes restricted data to an ineligible remote model, and always supports a
zero-model result.
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
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.locality not in {"local", "remote"}:
            raise ValueError("locality must be local or remote")
        if self.input_cost_per_million < 0 or self.output_cost_per_million < 0:
            raise ValueError("model costs cannot be negative")
        if not self.route_id:
            raise ValueError("route_id is required")


@dataclass(frozen=True)
class RoutingPolicy:
    mode: str
    max_request_cost: Decimal = Decimal("0")
    allow_remote: bool = False
    fixed_route_id: str | None = None
    allowed_route_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(f"unsupported routing mode: {self.mode}")
        if self.max_request_cost < 0:
            raise ValueError("max_request_cost cannot be negative")
        if self.mode == "fixed" and not self.fixed_route_id:
            raise ValueError("fixed mode requires fixed_route_id")


@dataclass(frozen=True)
class RouteRequest:
    data_class: str
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if not self.data_class:
            raise ValueError("data_class is required")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("token estimates cannot be negative")


@dataclass(frozen=True)
class RoutingDecision:
    route_id: str | None
    estimated_cost: Decimal
    execute: bool
    reason: str


def _estimated_cost(route: ModelRoute, request: RouteRequest) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(request.input_tokens) * route.input_cost_per_million / million
        + Decimal(request.output_tokens) * route.output_cost_per_million / million
    )


def _eligible(
    route: ModelRoute,
    request: RouteRequest,
    policy: RoutingPolicy,
) -> bool:
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
    return True


def select_route(
    routes: Iterable[ModelRoute],
    policy: RoutingPolicy,
    request: RouteRequest,
) -> RoutingDecision:
    """Select one eligible route or return an explicit zero-model decision."""

    if policy.mode == "zero":
        return RoutingDecision(None, Decimal("0"), False, "zero-model policy")

    candidates: list[tuple[Decimal, str, ModelRoute]] = []
    for route in routes:
        if not _eligible(route, request, policy):
            continue
        cost = _estimated_cost(route, request)
        if cost > policy.max_request_cost:
            continue
        candidates.append((cost, route.route_id, route))

    if not candidates:
        return RoutingDecision(None, Decimal("0"), False, "no eligible route within policy and budget")

    if policy.mode == "fixed":
        cost, _, route = candidates[0]
        return RoutingDecision(route.route_id, cost, True, "fixed eligible route")

    cost, _, route = min(candidates, key=lambda item: (item[0], item[1]))
    return RoutingDecision(route.route_id, cost, True, "lowest-cost eligible route")
