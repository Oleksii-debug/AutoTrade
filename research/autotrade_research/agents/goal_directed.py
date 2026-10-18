"""Goal-directed attention scheduling for one AutoTrade research organism.

The profiles in this module change research objectives and attention cadence.
They never grant order authority, enlarge hard risk, or turn a target return
into evidence that the target is achievable.  All workstreams share the
canonical market, portfolio, risk, accounting, critic and execution authorities
while retaining isolated memory/model namespaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import re
from typing import Iterable


SCHEMA = "autotrade-goal-directed-organism-v1"
PROFILE_IDS = (
    "BASELINE",
    "TARGET_25",
    "TARGET_50",
    "TARGET_100",
    "EVENT_DRIVEN_POSITION",
    "OPPORTUNITY_RESEARCH",
)
SHARED_AUTHORITIES = (
    "market-data",
    "portfolio-accounting",
    "risk",
    "order-management",
    "independent-critic",
)
_SESSION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use exact decimal text")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _canonical(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class ObjectiveProfile:
    profile_id: str
    target_return_fraction: Decimal | None
    target_horizon_seconds: int | None
    monitor_interval_seconds: int
    strategy_families: tuple[str, ...]
    passive_attention: bool
    opportunity_research_only: bool

    def __post_init__(self) -> None:
        if self.profile_id not in PROFILE_IDS:
            raise ValueError("unknown objective profile")
        target = self.target_return_fraction
        if target is not None:
            target = _decimal(target, name="target_return_fraction")
            if target not in {Decimal("0.25"), Decimal("0.50"), Decimal("1.00")}:
                raise ValueError("target return must be an approved experiment target")
            if type(self.target_horizon_seconds) is not int:
                raise TypeError("target horizon must be exact seconds")
            if not 60 <= self.target_horizon_seconds <= 86400:
                raise ValueError("active target horizon must be within one day")
        elif self.target_horizon_seconds is not None:
            raise ValueError("target horizon requires a target return")
        if (type(self.monitor_interval_seconds) is not int
                or not 1 <= self.monitor_interval_seconds <= 604800):
            raise ValueError("monitor interval is outside 1 second..7 days")
        if (type(self.strategy_families) is not tuple
                or not self.strategy_families
                or any(type(item) is not str or not item for item in self.strategy_families)
                or len(set(self.strategy_families)) != len(self.strategy_families)):
            raise ValueError("strategy families must be unique exact text")
        if type(self.passive_attention) is not bool:
            raise TypeError("passive_attention must be an exact bool")
        if type(self.opportunity_research_only) is not bool:
            raise TypeError("opportunity_research_only must be an exact bool")

    def document(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "target_return_fraction": (
                None if self.target_return_fraction is None
                else format(self.target_return_fraction, "f")
            ),
            "target_horizon_seconds": self.target_horizon_seconds,
            "monitor_interval_seconds": self.monitor_interval_seconds,
            "strategy_families": list(self.strategy_families),
            "passive_attention": self.passive_attention,
            "opportunity_research_only": self.opportunity_research_only,
        }


def profile_for(profile_id: str) -> ObjectiveProfile:
    """Return one immutable built-in profile; no profile changes risk authority."""
    profiles = {
        "BASELINE": ObjectiveProfile(
            "BASELINE", None, None, 60,
            ("NO_TRADE_CONTROL", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"),
            False, False,
        ),
        "TARGET_25": ObjectiveProfile(
            "TARGET_25", Decimal("0.25"), 86400, 15,
            ("ORDER_BOOK_IMBALANCE", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"),
            False, False,
        ),
        "TARGET_50": ObjectiveProfile(
            "TARGET_50", Decimal("0.50"), 86400, 10,
            ("ORDER_BOOK_IMBALANCE", "VOLATILITY_BREAKOUT", "MOMENTUM", "BREAKOUT"),
            False, False,
        ),
        "TARGET_100": ObjectiveProfile(
            "TARGET_100", Decimal("1.00"), 86400, 5,
            ("ORDER_BOOK_IMBALANCE", "VOLATILITY_BREAKOUT", "MOMENTUM", "BREAKOUT"),
            False, False,
        ),
        "EVENT_DRIVEN_POSITION": ObjectiveProfile(
            "EVENT_DRIVEN_POSITION", None, None, 21600,
            ("TREND", "MEAN_REVERSION", "BREAKOUT", "FIBONACCI_FEATURE"),
            True, False,
        ),
        "OPPORTUNITY_RESEARCH": ObjectiveProfile(
            "OPPORTUNITY_RESEARCH", None, None, 43200,
            ("NEW_LISTING_RESEARCH", "LAUNCHPAD_RESEARCH", "REWARD_RESEARCH"),
            True, True,
        ),
    }
    if type(profile_id) is not str or profile_id not in profiles:
        raise ValueError("unknown objective profile")
    return profiles[profile_id]


@dataclass(frozen=True, slots=True)
class WorkstreamSpec:
    session_id: str
    profile: ObjectiveProfile
    symbols: tuple[str, ...]
    created_at: datetime
    custom_thesis: str | None
    max_session_loss_fraction: Decimal
    max_position_fraction: Decimal
    paid_model_budget_usd: Decimal
    memory_namespace: str
    network_orders: bool = False
    trading_authority: bool = False

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or _SESSION_ID.fullmatch(self.session_id) is None:
            raise ValueError("session_id must be lowercase slug text")
        if type(self.profile) is not ObjectiveProfile:
            raise TypeError("profile must be an exact ObjectiveProfile")
        if (type(self.symbols) is not tuple or not self.symbols
                or any(type(symbol) is not str or not symbol for symbol in self.symbols)
                or len(set(self.symbols)) != len(self.symbols)):
            raise ValueError("symbols must be unique exact text")
        _instant(self.created_at, name="created_at")
        if self.custom_thesis is not None:
            if type(self.custom_thesis) is not str or not self.custom_thesis.strip():
                raise ValueError("custom thesis must be non-empty exact text")
            if len(self.custom_thesis) > 4000:
                raise ValueError("custom thesis is unbounded")
        loss = _decimal(self.max_session_loss_fraction, name="max_session_loss_fraction")
        position = _decimal(self.max_position_fraction, name="max_position_fraction")
        budget = _decimal(self.paid_model_budget_usd, name="paid_model_budget_usd")
        if not Decimal("0") < loss <= Decimal("0.10"):
            raise ValueError("session loss cap must remain within 10%")
        if not Decimal("0") < position <= Decimal("0.50"):
            raise ValueError("position cap must remain within 50%")
        if not Decimal("0") <= budget <= Decimal("100"):
            raise ValueError("paid model budget is outside the bounded research range")
        if (type(self.memory_namespace) is not str
                or not self.memory_namespace.startswith("workstreams/")
                or ".." in self.memory_namespace):
            raise ValueError("memory namespace is not canonical")
        if self.network_orders is not False or self.trading_authority is not False:
            raise ValueError("goal-directed research cannot grant order authority")
        if self.profile.opportunity_research_only and self.max_position_fraction != Decimal("0.01"):
            raise ValueError("opportunity research uses the inert 1% placeholder cap")

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "session_id": self.session_id,
            "profile": self.profile.document(),
            "symbols": list(self.symbols),
            "created_at": _instant(self.created_at, name="created_at").isoformat(),
            "custom_thesis": self.custom_thesis,
            "max_session_loss_fraction": format(self.max_session_loss_fraction, "f"),
            "max_position_fraction": format(self.max_position_fraction, "f"),
            "paid_model_budget_usd": format(self.paid_model_budget_usd, "f"),
            "memory_namespace": self.memory_namespace,
            "shared_authorities": list(SHARED_AUTHORITIES),
            "environment": "SIMULATION",
            "network_orders": False,
            "trading_authority": False,
        }


def create_workstream(
    *,
    session_id: str,
    profile_id: str,
    symbols: tuple[str, ...],
    created_at: datetime,
    custom_thesis: str | None = None,
    max_session_loss_fraction: object = "0.05",
    max_position_fraction: object = "0.25",
    paid_model_budget_usd: object = "0",
) -> WorkstreamSpec:
    profile = profile_for(profile_id)
    loss = _decimal(max_session_loss_fraction, name="max_session_loss_fraction")
    position = _decimal(max_position_fraction, name="max_position_fraction")
    budget = _decimal(paid_model_budget_usd, name="paid_model_budget_usd")
    if profile.opportunity_research_only:
        loss = Decimal("0.01")
        position = Decimal("0.01")
        budget = Decimal("0")
    identity = {
        "session_id": session_id,
        "profile_id": profile_id,
        "symbols": list(symbols),
        "created_at": _instant(created_at, name="created_at").isoformat(),
        "custom_thesis": custom_thesis,
    }
    suffix = sha256(_canonical(identity)).hexdigest()[:16]
    return WorkstreamSpec(
        session_id=session_id,
        profile=profile,
        symbols=symbols,
        created_at=created_at,
        custom_thesis=custom_thesis,
        max_session_loss_fraction=loss,
        max_position_fraction=position,
        paid_model_budget_usd=budget,
        memory_namespace=f"workstreams/{profile_id.lower()}/{session_id}-{suffix}",
    )


def workstream_from_document(value: object) -> WorkstreamSpec:
    """Re-admit a persisted workstream without trusting mutable profile fields."""
    expected = {
        "schema", "session_id", "profile", "symbols", "created_at",
        "custom_thesis", "max_session_loss_fraction", "max_position_fraction",
        "paid_model_budget_usd", "memory_namespace", "shared_authorities",
        "environment", "network_orders", "trading_authority",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("persisted workstream schema differs")
    if value["schema"] != SCHEMA or value["environment"] != "SIMULATION":
        raise ValueError("persisted workstream environment differs")
    profile_document = value["profile"]
    if type(profile_document) is not dict or type(profile_document.get("profile_id")) is not str:
        raise ValueError("persisted workstream profile is malformed")
    profile = profile_for(profile_document["profile_id"])
    if profile_document != profile.document():
        raise ValueError("persisted built-in profile was modified")
    if value["shared_authorities"] != list(SHARED_AUTHORITIES):
        raise ValueError("persisted shared authorities differ")
    if type(value["symbols"]) is not list:
        raise ValueError("persisted symbols must be a list")
    try:
        created_at = datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError("persisted created_at is malformed") from exc
    return WorkstreamSpec(
        session_id=value["session_id"],
        profile=profile,
        symbols=tuple(value["symbols"]),
        created_at=created_at,
        custom_thesis=value["custom_thesis"],
        max_session_loss_fraction=_decimal(
            value["max_session_loss_fraction"], name="max_session_loss_fraction"
        ),
        max_position_fraction=_decimal(
            value["max_position_fraction"], name="max_position_fraction"
        ),
        paid_model_budget_usd=_decimal(
            value["paid_model_budget_usd"], name="paid_model_budget_usd"
        ),
        memory_namespace=value["memory_namespace"],
        network_orders=value["network_orders"],
        trading_authority=value["trading_authority"],
    )


@dataclass(frozen=True, slots=True)
class AttentionObservation:
    available_at: datetime
    reference_price: Decimal
    current_price: Decimal
    next_scheduled_review: datetime
    take_profit_fraction: Decimal
    stop_loss_fraction: Decimal
    manual_wake: bool = False

    def __post_init__(self) -> None:
        _instant(self.available_at, name="available_at")
        _instant(self.next_scheduled_review, name="next_scheduled_review")
        reference = _decimal(self.reference_price, name="reference_price")
        current = _decimal(self.current_price, name="current_price")
        take = _decimal(self.take_profit_fraction, name="take_profit_fraction")
        stop = _decimal(self.stop_loss_fraction, name="stop_loss_fraction")
        if reference <= 0 or current <= 0:
            raise ValueError("attention prices must be positive")
        if not Decimal("0") < take <= Decimal("10"):
            raise ValueError("take-profit trigger is outside range")
        if not Decimal("0") < stop <= Decimal("0.10"):
            raise ValueError("stop-loss trigger must remain within 10%")
        if type(self.manual_wake) is not bool:
            raise TypeError("manual_wake must be exact bool")


@dataclass(frozen=True, slots=True)
class AttentionDecision:
    session_id: str
    phase: str
    reason: str
    priority: int
    deep_analysis_requested: bool
    paid_model_eligible: bool
    next_check_at: datetime


def decide_attention(
    spec: WorkstreamSpec,
    observation: AttentionObservation,
    *,
    now: datetime,
    max_market_age_seconds: int = 120,
) -> AttentionDecision:
    """Use free deterministic monitoring and wake analysis only on a real trigger."""
    if type(spec) is not WorkstreamSpec or type(observation) is not AttentionObservation:
        raise TypeError("attention inputs must be canonical")
    current = _instant(now, name="now")
    available = _instant(observation.available_at, name="available_at")
    scheduled = _instant(observation.next_scheduled_review, name="next_scheduled_review")
    if available > current:
        raise ValueError("future market observation cannot wake a workstream")
    if type(max_market_age_seconds) is not int or max_market_age_seconds < 1:
        raise ValueError("max market age must be positive exact seconds")
    next_check = current + timedelta(seconds=spec.profile.monitor_interval_seconds)
    if (current - available).total_seconds() > max_market_age_seconds:
        return AttentionDecision(
            spec.session_id, "RECOVERY", "market data is stale", 100, True, False,
            next_check,
        )
    with localcontext() as context:
        context.prec = 42
        change = (observation.current_price - observation.reference_price) / observation.reference_price
    trigger = None
    priority = 0
    if observation.manual_wake:
        trigger, priority = "manual wake", 95
    elif change <= -observation.stop_loss_fraction:
        trigger, priority = "stop-loss boundary reached", 90
    elif change >= observation.take_profit_fraction:
        trigger, priority = "take-profit boundary reached", 80
    elif current >= scheduled:
        trigger, priority = "scheduled review due", 50
    if trigger is not None:
        paid = (
            not spec.profile.opportunity_research_only
            and spec.paid_model_budget_usd > 0
            and priority >= 80
        )
        return AttentionDecision(
            spec.session_id, "WAKE_ANALYSIS", trigger, priority, True, paid,
            next_check,
        )
    return AttentionDecision(
        spec.session_id,
        "PASSIVE_MONITORING" if spec.profile.passive_attention else "ACTIVE_MONITORING",
        "no analysis trigger; deterministic monitor only",
        10 if spec.profile.passive_attention else 20,
        False,
        False,
        next_check,
    )


@dataclass(frozen=True, slots=True)
class AttentionGrant:
    decision: AttentionDecision
    deep_analysis_granted: bool
    paid_model_granted: bool


def allocate_attention(
    decisions: Iterable[AttentionDecision],
    *,
    max_deep_analyses: int = 1,
    max_paid_model_calls: int = 1,
) -> tuple[AttentionGrant, ...]:
    """Allocate one organism-wide attention budget without duplicate paid thought."""
    materialized = tuple(decisions)
    if any(type(item) is not AttentionDecision for item in materialized):
        raise TypeError("decisions must be canonical AttentionDecision values")
    if len({item.session_id for item in materialized}) != len(materialized):
        raise ValueError("one cycle cannot contain duplicate session decisions")
    if type(max_deep_analyses) is not int or max_deep_analyses < 0:
        raise ValueError("max_deep_analyses must be a non-negative integer")
    if type(max_paid_model_calls) is not int or max_paid_model_calls < 0:
        raise ValueError("max_paid_model_calls must be a non-negative integer")
    deep_left, paid_left = max_deep_analyses, max_paid_model_calls
    granted: dict[str, AttentionGrant] = {}
    for decision in sorted(materialized, key=lambda item: (-item.priority, item.session_id)):
        deep = decision.deep_analysis_requested and deep_left > 0
        if deep:
            deep_left -= 1
        paid = deep and decision.paid_model_eligible and paid_left > 0
        if paid:
            paid_left -= 1
        granted[decision.session_id] = AttentionGrant(decision, deep, paid)
    return tuple(granted[item.session_id] for item in materialized)


def evaluate_objective(
    spec: WorkstreamSpec,
    *,
    initial_equity: object,
    final_equity_after_costs: object,
    max_drawdown_fraction: object,
    horizon_complete: bool,
) -> dict[str, object]:
    """Evaluate a target honestly; target pressure cannot rewrite the outcome."""
    initial = _decimal(initial_equity, name="initial_equity")
    final = _decimal(final_equity_after_costs, name="final_equity_after_costs")
    drawdown = _decimal(max_drawdown_fraction, name="max_drawdown_fraction")
    if initial <= 0 or final < 0 or not Decimal("0") <= drawdown <= Decimal("1"):
        raise ValueError("objective outcome is outside financial bounds")
    if type(horizon_complete) is not bool:
        raise TypeError("horizon_complete must be exact bool")
    with localcontext() as context:
        context.prec = 42
        net_return = (final - initial) / initial
    target = spec.profile.target_return_fraction
    if drawdown > spec.max_session_loss_fraction:
        status = "RISK_LIMIT_BREACHED"
    elif not horizon_complete:
        status = "INCONCLUSIVE"
    elif target is None:
        status = "MEASURED_NO_TARGET"
    elif net_return >= target:
        status = "TARGET_MET"
    else:
        status = "TARGET_MISSED"
    return {
        "schema": SCHEMA,
        "session_id": spec.session_id,
        "profile_id": spec.profile.profile_id,
        "status": status,
        "target_return_fraction": None if target is None else format(target, "f"),
        "net_return_after_costs_fraction": format(net_return, "f"),
        "max_drawdown_fraction": format(drawdown, "f"),
        "horizon_complete": horizon_complete,
        "profitability_proven": False,
        "automatic_champion_promotion": False,
        "network_orders": False,
        "trading_authority": False,
    }


def organism_manifest(workstreams: Iterable[WorkstreamSpec]) -> dict[str, object]:
    materialized = tuple(workstreams)
    if not materialized or any(type(item) is not WorkstreamSpec for item in materialized):
        raise ValueError("organism requires canonical workstreams")
    if len({item.session_id for item in materialized}) != len(materialized):
        raise ValueError("workstream session ids must be unique")
    if len({item.memory_namespace for item in materialized}) != len(materialized):
        raise ValueError("workstream memory namespaces must be isolated")
    body = {
        "schema": SCHEMA,
        "architecture": "ONE_ORGANISM_SHARED_AUTHORITIES_ISOLATED_WORKSTREAM_MEMORY",
        "shared_authorities": list(SHARED_AUTHORITIES),
        "attention_policy": {
            "market_monitor": "CONTINUOUS_ZERO_MODEL",
            "analysis": "EVENT_DRIVEN_LOCAL_FIRST",
            "paid_models": "BOUNDED_ESCALATION_ONLY",
        },
        "workstreams": [item.document() for item in materialized],
        "environment": "SIMULATION",
        "network_orders": False,
        "trading_authority": False,
    }
    return {**body, "manifest_sha256": "sha256:" + sha256(_canonical(body)).hexdigest()}
