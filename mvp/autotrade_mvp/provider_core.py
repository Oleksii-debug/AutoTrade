"""Provider-neutral safety contract for real adapter implementations.

The registry names architectural targets only. A provider/product/environment
combination remains unqualified until exact evidence proves the required
surfaces on the exact adapter build. Nothing in this module grants live trading
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable, Literal, Mapping
import re


class ProviderCoreError(ValueError):
    pass


_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _code_sha(value: str, name: str = "adapter_code_sha") -> str:
    sha = _text(value, name)
    if _GIT_OBJECT_ID.fullmatch(sha) is None:
        raise ProviderCoreError(
            f"{name} must be a canonical 40- or 64-character lowercase Git object id"
        )
    return sha


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderCoreError(f"{name} is required")
    return value.strip()


def _decimal(value, name: str, *, non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProviderCoreError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderCoreError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ProviderCoreError(f"{name} must be a finite decimal")
    if non_negative and result < 0:
        raise ProviderCoreError(f"{name} cannot be negative")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProviderCoreError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class Surface(StrEnum):
    PUBLIC_DATA = "PUBLIC_DATA"
    AUTHENTICATED_READ = "AUTHENTICATED_READ"
    TRADING = "TRADING"
    ACTIVITIES = "ACTIVITIES"
    STREAM = "STREAM"


@dataclass(frozen=True)
class ProviderDefinition:
    provider_id: str
    product_families: tuple[str, ...]
    surfaces: tuple[Surface, ...]
    test_environment_note: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        products = tuple(_text(x, "product family") for x in self.product_families)
        if not products or len(set(products)) != len(products):
            raise ProviderCoreError("provider product families must be non-empty and unique")
        object.__setattr__(self, "product_families", products)
        surfaces = tuple(self.surfaces)
        if not surfaces or len(set(surfaces)) != len(surfaces):
            raise ProviderCoreError("provider surfaces must be non-empty and unique")
        object.__setattr__(self, "surfaces", surfaces)
        object.__setattr__(
            self, "test_environment_note", _text(self.test_environment_note, "test_environment_note")
        )


PROVIDERS: Mapping[str, ProviderDefinition] = {
    "BYBIT": ProviderDefinition(
        "BYBIT",
        ("SPOT", "MARGIN", "LINEAR_DERIVATIVES", "INVERSE_DERIVATIVES", "OPTIONS"),
        tuple(Surface),
        "Separate documented test/demo/live surfaces must be qualified per product.",
    ),
    "KRAKEN": ProviderDefinition(
        "KRAKEN",
        ("SPOT", "MARGIN", "DERIVATIVES"),
        tuple(Surface),
        "Spot and derivatives are separate API families; derivatives demo does not prove spot sandbox parity.",
    ),
    "WHITEBIT": ProviderDefinition(
        "WHITEBIT",
        ("SPOT", "COLLATERAL", "FUTURES"),
        tuple(Surface),
        "Recorded fixtures and any official test facility precede separately authorized live probes.",
    ),
    "BINANCE": ProviderDefinition(
        "BINANCE",
        ("SPOT", "MARGIN", "USD_M", "COIN_M", "OPTIONS"),
        tuple(Surface),
        "Product families use separate test facilities and must not share assumed filters or order semantics.",
    ),
    "IBKR": ProviderDefinition(
        "IBKR",
        ("EQUITIES", "FUTURES", "OPTIONS", "FX", "OTHER_ENTITLED"),
        tuple(Surface),
        "Paper account, data entitlements and session lifecycle must be qualified on the selected API.",
    ),
    "ALPACA": ProviderDefinition(
        "ALPACA",
        ("EQUITIES", "CRYPTO", "OPTIONS"),
        tuple(Surface),
        "Paper credentials are distinct; paper execution does not establish live execution realism.",
    ),
}


REQUIRED_QUALIFICATION_CASES = frozenset(
    {
        "metadata",
        "authentication",
        "clock",
        "quota",
        "stream_gap_reconnect",
        "market_order",
        "limit_order",
        "partial_fill",
        "cancel_fill_race",
        "rejection",
        "timeout_after_send",
        "duplicate_event",
        "lost_event",
        "terminal_correction",
        "account_mode_change",
        "manual_activity",
        "snapshot_reconciliation",
        "secret_redaction",
    }
)


@dataclass(frozen=True)
class QualificationEvidence:
    provider_id: str
    product_family: str
    environment: str
    adapter_code_sha: str
    documentation_ref: str
    observed_at: datetime
    expires_at: datetime
    passed_cases: frozenset[str]
    unsupported_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        if provider not in PROVIDERS:
            raise ProviderCoreError("unknown provider")
        object.__setattr__(self, "provider_id", provider)
        family = _text(self.product_family, "product_family")
        if family not in PROVIDERS[provider].product_families:
            raise ProviderCoreError("product family is not declared for provider")
        object.__setattr__(self, "product_family", family)
        object.__setattr__(
            self,
            "environment",
            _text(self.environment, "environment").upper(),
        )
        object.__setattr__(self, "adapter_code_sha", _code_sha(self.adapter_code_sha))
        object.__setattr__(self, "documentation_ref", _text(self.documentation_ref, "documentation_ref"))
        observed = _utc(self.observed_at, "observed_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= observed:
            raise ProviderCoreError("qualification expiry must be after observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        cases = frozenset(_text(x, "passed case") for x in self.passed_cases)
        object.__setattr__(self, "passed_cases", cases)
        object.__setattr__(
            self,
            "unsupported_features",
            tuple(sorted({_text(x, "unsupported feature") for x in self.unsupported_features})),
        )

    def status(self, *, now: datetime, exact_code_sha: str) -> str:
        point = _utc(now, "now")
        if _code_sha(exact_code_sha, "exact_code_sha") != self.adapter_code_sha:
            return "CODE_MISMATCH"
        if point < self.observed_at:
            return "FUTURE_EVIDENCE"
        if point >= self.expires_at:
            return "EXPIRED"
        missing = REQUIRED_QUALIFICATION_CASES - self.passed_cases
        if missing:
            return "INCOMPLETE"
        if self.environment == "LIVE":
            return "LIVE_REQUIRES_BOUNDED_REAL"
        return "QUALIFIED_FOR_NONLIVE"


@dataclass
class QuotaBucket:
    capacity: Decimal
    recovery_reserve: Decimal
    used: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.capacity = _decimal(self.capacity, "capacity", non_negative=True)
        self.recovery_reserve = _decimal(
            self.recovery_reserve, "recovery_reserve", non_negative=True
        )
        self.used = _decimal(self.used, "used", non_negative=True)
        if self.recovery_reserve > self.capacity:
            raise ProviderCoreError("recovery reserve cannot exceed capacity")
        if self.used > self.capacity:
            raise ProviderCoreError("used quota cannot exceed capacity")

    def available(self) -> Decimal:
        return self.capacity - self.used

    def acquire(self, cost, *, purpose: Literal["RECOVERY", "TRADING", "RESEARCH"]) -> None:
        amount = _decimal(cost, "quota cost", non_negative=True)
        if purpose not in {"RECOVERY", "TRADING", "RESEARCH"}:
            raise ProviderCoreError("unknown quota purpose")
        if amount == 0:
            return
        remaining = self.available()
        if amount > remaining:
            raise ProviderCoreError("provider quota exhausted")
        if purpose != "RECOVERY" and remaining - amount < self.recovery_reserve:
            raise ProviderCoreError("recovery quota reserve is protected")
        self.used += amount

    def release(self, cost) -> None:
        amount = _decimal(cost, "quota cost", non_negative=True)
        if amount > self.used:
            raise ProviderCoreError("cannot release more quota than was acquired")
        self.used -= amount

    def reset(self) -> None:
        self.used = Decimal("0")


@dataclass(frozen=True)
class ClockGuard:
    maximum_absolute_skew: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.maximum_absolute_skew, timedelta) or self.maximum_absolute_skew <= timedelta(0):
            raise ProviderCoreError("maximum_absolute_skew must be positive")

    def require_safe(self, *, host_time: datetime, provider_time: datetime) -> None:
        host = _utc(host_time, "host_time")
        provider = _utc(provider_time, "provider_time")
        if abs(host - provider) > self.maximum_absolute_skew:
            raise ProviderCoreError("provider authentication clock skew exceeds safe bound")


@dataclass(frozen=True)
class WriteOutcome:
    status: Literal["NOT_SENT", "ACKNOWLEDGED", "REJECTED", "UNKNOWN"]
    retry_same_economic_action: bool
    reconciliation_required: bool


def classify_write_outcome(
    *,
    transport_started: bool,
    provider_acknowledged: bool,
    provider_rejected: bool,
) -> WriteOutcome:
    """Classify write ambiguity without inventing a retry after possible send."""

    if provider_acknowledged and provider_rejected:
        raise ProviderCoreError("write cannot be both acknowledged and rejected")
    if not transport_started:
        if provider_acknowledged or provider_rejected:
            raise ProviderCoreError("provider result cannot predate transport")
        return WriteOutcome("NOT_SENT", True, False)
    if provider_acknowledged:
        return WriteOutcome("ACKNOWLEDGED", False, False)
    if provider_rejected:
        return WriteOutcome("REJECTED", False, False)
    return WriteOutcome("UNKNOWN", False, True)


def provider_definition(provider_id: str) -> ProviderDefinition:
    key = _text(provider_id, "provider_id").upper()
    try:
        return PROVIDERS[key]
    except KeyError as error:
        raise ProviderCoreError("unknown provider") from error
