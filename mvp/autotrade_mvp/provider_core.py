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
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Literal, Mapping


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


def _canonical_provider_json(value: Any, *, name: str) -> str:
    def reject_binary_float(item: Any, path: str = "$") -> None:
        if isinstance(item, float):
            raise ProviderCoreError(
                f"{name} must not contain binary float at {path}"
            )
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ProviderCoreError(
                        f"{name} object keys must be text at {path}"
                    )
                reject_binary_float(child, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                reject_binary_float(child, f"{path}[{index}]")

    reject_binary_float(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ProviderCoreError(f"{name} must be canonical JSON data") from error


def _sha256_text(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedReconciliationRead:
    """Immutable authenticated-read scope fixed before provider access.

    This object is intentionally provider-neutral. Adapter code prepares it from
    the exact account/environment/endpoint and request payload before the read is
    performed. Response parsers then consume only BoundReconciliationResponse,
    never free caller scope labels.
    """

    provider_id: str
    account_id: str
    environment: str
    surface: str
    endpoint: str
    request_json: str
    request_sha256: str
    provenance_id: str

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        surface: str,
        endpoint: str,
        request: Mapping[str, Any],
    ) -> "PreparedReconciliationRead":
        if not isinstance(request, Mapping):
            raise ProviderCoreError("reconciliation read request must be a mapping")
        provider = _text(provider_id, "provider_id").upper()
        if provider not in PROVIDERS:
            raise ProviderCoreError("unknown provider")
        account = _text(account_id, "account_id")
        scope = _text(environment, "environment").upper()
        normalized_surface = _text(surface, "surface").upper()
        normalized_endpoint = _text(endpoint, "endpoint")
        request_json = _canonical_provider_json(request, name="reconciliation read request")
        request_sha = _sha256_text(request_json)
        identity = _canonical_provider_json(
            {
                "provider_id": provider,
                "account_id": account,
                "environment": scope,
                "surface": normalized_surface,
                "endpoint": normalized_endpoint,
                "request_sha256": request_sha,
            },
            name="reconciliation read provenance",
        )
        return cls(
            provider_id=provider,
            account_id=account,
            environment=scope,
            surface=normalized_surface,
            endpoint=normalized_endpoint,
            request_json=request_json,
            request_sha256=request_sha,
            provenance_id=_sha256_text(identity),
        )

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        if provider not in PROVIDERS or provider != self.provider_id:
            raise ProviderCoreError("reconciliation read provider_id is not canonical")
        if _text(self.account_id, "account_id") != self.account_id:
            raise ProviderCoreError("reconciliation read account_id is not canonical")
        if _text(self.environment, "environment").upper() != self.environment:
            raise ProviderCoreError("reconciliation read environment is not canonical")
        if _text(self.surface, "surface").upper() != self.surface:
            raise ProviderCoreError("reconciliation read surface is not canonical")
        if _text(self.endpoint, "endpoint") != self.endpoint:
            raise ProviderCoreError("reconciliation read endpoint is not canonical")
        try:
            request = json.loads(self.request_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ProviderCoreError("reconciliation read request_json is invalid") from error
        canonical_request = _canonical_provider_json(
            request, name="reconciliation read request"
        )
        if canonical_request != self.request_json:
            raise ProviderCoreError("reconciliation read request_json is not canonical")
        request_sha = _sha256_text(canonical_request)
        if request_sha != self.request_sha256:
            raise ProviderCoreError("reconciliation read request digest mismatch")
        identity = _canonical_provider_json(
            {
                "provider_id": self.provider_id,
                "account_id": self.account_id,
                "environment": self.environment,
                "surface": self.surface,
                "endpoint": self.endpoint,
                "request_sha256": request_sha,
            },
            name="reconciliation read provenance",
        )
        if _sha256_text(identity) != self.provenance_id:
            raise ProviderCoreError("reconciliation read provenance identity mismatch")

    def request_payload(self) -> dict[str, Any]:
        value = json.loads(self.request_json)
        if not isinstance(value, dict):
            raise ProviderCoreError("reconciliation read request must decode to an object")
        return value


@dataclass(frozen=True)
class BoundReconciliationResponse:
    """Exact provider response cryptographically bound to one prepared read."""

    read: PreparedReconciliationRead
    response_json: str
    response_sha256: str
    evidence_id: str

    @classmethod
    def bind(
        cls,
        read: PreparedReconciliationRead,
        response: Mapping[str, Any] | list[Any],
    ) -> "BoundReconciliationResponse":
        if not isinstance(read, PreparedReconciliationRead):
            raise TypeError("read must be PreparedReconciliationRead")
        response_json = _canonical_provider_json(
            response, name="reconciliation provider response"
        )
        response_sha = _sha256_text(response_json)
        identity = _canonical_provider_json(
            {
                "read_provenance_id": read.provenance_id,
                "response_sha256": response_sha,
            },
            name="reconciliation response evidence",
        )
        return cls(
            read=read,
            response_json=response_json,
            response_sha256=response_sha,
            evidence_id=_sha256_text(identity),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.read, PreparedReconciliationRead):
            raise TypeError("read must be PreparedReconciliationRead")
        # Re-run prepared-read integrity in case an unsafe construction path was used.
        self.read.__post_init__()
        try:
            response = json.loads(self.response_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise ProviderCoreError("reconciliation response_json is invalid") from error
        canonical_response = _canonical_provider_json(
            response, name="reconciliation provider response"
        )
        if canonical_response != self.response_json:
            raise ProviderCoreError("reconciliation response_json is not canonical")
        response_sha = _sha256_text(canonical_response)
        if response_sha != self.response_sha256:
            raise ProviderCoreError("reconciliation response digest mismatch")
        identity = _canonical_provider_json(
            {
                "read_provenance_id": self.read.provenance_id,
                "response_sha256": response_sha,
            },
            name="reconciliation response evidence",
        )
        if _sha256_text(identity) != self.evidence_id:
            raise ProviderCoreError("reconciliation response evidence identity mismatch")

    def payload(self) -> Any:
        self.__post_init__()
        return json.loads(self.response_json)


def require_reconciliation_response(
    evidence: BoundReconciliationResponse,
    *,
    provider_id: str,
    surface: str,
    endpoint: str,
) -> tuple[Any, str, str]:
    """Resolve payload and exact financial scope from immutable read provenance."""

    if not isinstance(evidence, BoundReconciliationResponse):
        raise TypeError("evidence must be BoundReconciliationResponse")
    read = evidence.read
    expected_provider = _text(provider_id, "provider_id").upper()
    expected_surface = _text(surface, "surface").upper()
    expected_endpoint = _text(endpoint, "endpoint")
    if (
        read.provider_id != expected_provider
        or read.surface != expected_surface
        or read.endpoint != expected_endpoint
    ):
        raise ProviderCoreError("reconciliation response provenance scope mismatch")
    return evidence.payload(), read.account_id, read.environment


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
