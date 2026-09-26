"""Shared real-provider HTTP transport seam for guarded execution.

This module does not own financial authority, durable attempt state, retries, or
provider qualification. GuardedDispatcher remains the only send-state owner.
The transport resolves a scoped TRADE credential only at the final signing
boundary, calls the dispatcher's final guard exactly once, and performs exactly
one outbound HTTP request. Any exception after the guard is deliberately left
for GuardedDispatcher to classify as UNKNOWN.

Binance Spot and WhiteBIT reuse this network lifecycle. Provider-specific
signing/nonce rules remain pure or journal-backed prerequisites to the same
final guard; future providers must extend this seam rather than introduce
another dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256, sha512
import base64
import binascii
import hmac
import json
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from .capabilities import CapabilityRegistry, CapabilitySnapshot
from .dispatch import ExactJsonTransportResponse
from .persistence import JournalStore, payload_digest
from .kraken_spot import validate_spot_client_order_id
from .whitebit import sign_private_request, validate_client_order_id
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
)
from .windows_secrets import PersistentCredentialHandle


class ProviderTransportError(RuntimeError):
    """Base error for the shared provider I/O seam."""


class ProviderTransportScopeError(ValueError):
    """Raised before I/O when provider/account/environment scope is invalid."""


class ProviderSecretResolver(Protocol):
    def resolve_for_execution(
        self,
        token: str,
        *,
        origin: str,
        handle: PersistentCredentialHandle,
        execution_identity: str,
        account_id: str,
        provider: str,
        environment: str,
        purpose: str,
    ) -> str: ...


class ProviderWireClient(Protocol):
    def send(
        self,
        request: "SignedHttpRequest | AuthenticatedReadHttpRequest",
    ) -> "bytes | TradingWireResponse | AuthenticatedReadWireResponse": ...


QuotaGate = Callable[[str, str, str, str], None]
ClockMillis = Callable[[], int]
ClockUtc = Callable[[], datetime]


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderTransportScopeError(f"{name} is required")
    return value.strip()


def _canonical_text(value: object, *, name: str) -> str:
    """Require exact non-empty text when bytes are bound to durable request identity."""
    text = _text(value, name=name)
    if value != text:
        raise ProviderTransportScopeError(f"{name} must be canonical text")
    return text


def _canonical_environment(value: object) -> str:
    environment = _text(value, name="environment").upper()
    if environment not in {"PAPER", "LIVE"}:
        raise ProviderTransportScopeError(
            "real-provider HTTP transport permits only PAPER or LIVE"
        )
    return environment


def _canonical_host(value: object) -> str:
    host = _text(value, name="allowed host").lower().rstrip(".")
    if "/" in host or ":" in host or "@" in host:
        raise ProviderTransportScopeError("allowed host must be a bare DNS name")
    return host


@dataclass(frozen=True)
class ProviderEndpointPolicy:
    """Exact provider/environment network destination.

    Redirects are never part of the policy. A request is emitted only to the
    configured HTTPS base host. Cross-environment retargeting therefore cannot
    happen implicitly through HTTP redirects.
    """

    provider_id: str
    environment: str
    base_url: str
    allowed_hosts: frozenset[str]
    timeout_seconds: int = 15

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, name="provider_id").upper()
        environment = _canonical_environment(self.environment)
        if not isinstance(self.allowed_hosts, frozenset) or not self.allowed_hosts:
            raise ProviderTransportScopeError(
                "allowed_hosts must be a non-empty frozenset"
            )
        hosts = frozenset(_canonical_host(host) for host in self.allowed_hosts)

        base = _text(self.base_url, name="base_url")
        parsed = urlsplit(base)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ProviderTransportScopeError(
                "base_url must be an origin-only HTTPS URL"
            )
        host = parsed.hostname.lower().rstrip(".")
        if host not in hosts:
            raise ProviderTransportScopeError(
                "base_url host is outside the explicit allowlist"
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
            or self.timeout_seconds > 120
        ):
            raise ProviderTransportScopeError(
                "timeout_seconds must be an integer from 1 through 120"
            )

        canonical_base = f"https://{host}"
        if parsed.port is not None:
            if parsed.port != 443:
                raise ProviderTransportScopeError(
                    "provider HTTPS origin must use the standard TLS port"
                )
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "base_url", canonical_base)
        object.__setattr__(self, "allowed_hosts", hosts)

    def absolute_url(self, endpoint: object) -> str:
        path = _canonical_text(endpoint, name="endpoint")
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "://" in path
            or "?" in path
            or "#" in path
        ):
            raise ProviderTransportScopeError(
                "endpoint must be a provider-relative path without query/fragment"
            )
        url = self.base_url + path
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise ProviderTransportScopeError(
                "provider request escaped the environment host allowlist"
            )
        return url


BINANCE_SPOT_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "PAPER": ProviderEndpointPolicy(
                provider_id="BINANCE",
                environment="PAPER",
                base_url="https://testnet.binance.vision",
                allowed_hosts=frozenset({"testnet.binance.vision"}),
            ),
            "LIVE": ProviderEndpointPolicy(
                provider_id="BINANCE",
                environment="LIVE",
                base_url="https://api.binance.com",
                allowed_hosts=frozenset({"api.binance.com"}),
            ),
        }
    )
)


WHITEBIT_ORDER_ENDPOINTS = frozenset(
    {
        "/api/v4/order/market",
        "/api/v4/order/new",
        "/api/v4/order/stop_market",
        "/api/v4/order/stop_limit",
        "/api/v4/order/stock_market",
        "/api/v4/order/collateral/market",
        "/api/v4/order/collateral/limit",
        "/api/v4/order/collateral/trigger-market",
        "/api/v4/order/collateral/stop-limit",
    }
)


WHITEBIT_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "LIVE": ProviderEndpointPolicy(
                provider_id="WHITEBIT",
                environment="LIVE",
                base_url="https://whitebit.com",
                allowed_hosts=frozenset({"whitebit.com"}),
            ),
        }
    )
)


KRAKEN_SPOT_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "LIVE": ProviderEndpointPolicy(
                provider_id="KRAKEN",
                environment="LIVE",
                base_url="https://api.kraken.com",
                allowed_hosts=frozenset({"api.kraken.com"}),
            ),
        }
    )
)


BYBIT_V5_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "MAINNET": ProviderEndpointPolicy(
                provider_id="BYBIT",
                environment="LIVE",
                base_url="https://api.bybit.com",
                allowed_hosts=frozenset({"api.bybit.com"}),
            ),
            "TESTNET": ProviderEndpointPolicy(
                provider_id="BYBIT",
                environment="PAPER",
                base_url="https://api-testnet.bybit.com",
                allowed_hosts=frozenset({"api-testnet.bybit.com"}),
            ),
            "DEMO": ProviderEndpointPolicy(
                provider_id="BYBIT",
                environment="PAPER",
                base_url="https://api-demo.bybit.com",
                allowed_hosts=frozenset({"api-demo.bybit.com"}),
            ),
        }
    )
)


ALPACA_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "PAPER": ProviderEndpointPolicy(
                provider_id="ALPACA",
                environment="PAPER",
                base_url="https://paper-api.alpaca.markets",
                allowed_hosts=frozenset({"paper-api.alpaca.markets"}),
            ),
            "LIVE": ProviderEndpointPolicy(
                provider_id="ALPACA",
                environment="LIVE",
                base_url="https://api.alpaca.markets",
                allowed_hosts=frozenset({"api.alpaca.markets"}),
            ),
        }
    )
)


@dataclass(frozen=True)
class AuthenticatedReadEndpointRule:
    surface: Surface
    permission_scope: str
    data_entitlement: str
    success_statuses: frozenset[int]

    def __post_init__(self) -> None:
        if self.surface not in {Surface.AUTHENTICATED_READ, Surface.ACTIVITIES}:
            raise ProviderTransportScopeError(
                "authenticated-read endpoint rule requires a read surface"
            )
        object.__setattr__(
            self,
            "permission_scope",
            _canonical_text(self.permission_scope, name="permission_scope"),
        )
        object.__setattr__(
            self,
            "data_entitlement",
            _canonical_text(self.data_entitlement, name="data_entitlement"),
        )
        if (
            not isinstance(self.success_statuses, frozenset)
            or not self.success_statuses
            or any(
                isinstance(status, bool)
                or not isinstance(status, int)
                or status < 200
                or status > 299
                for status in self.success_statuses
            )
        ):
            raise ProviderTransportScopeError(
                "authenticated-read success_statuses must be a non-empty frozenset of 2xx integers"
            )


BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS: Mapping[
    str, AuthenticatedReadEndpointRule
] = MappingProxyType(
    {
        "/api/v3/account": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="ACCOUNT",
            success_statuses=frozenset({200}),
        ),
        "/api/v3/openOrders": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="ORDER.READ",
            data_entitlement="ORDERS",
            success_statuses=frozenset({200}),
        ),
        "/api/v3/myTrades": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="TRADE.READ",
            data_entitlement="TRADES",
            success_statuses=frozenset({200}),
        ),
    }
)


BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS: Mapping[
    str, AuthenticatedReadEndpointRule
] = MappingProxyType(
    {
        "/v5/order/realtime": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="ORDERS",
            success_statuses=frozenset({200}),
        ),
        "/v5/order/history": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="ORDERS",
            success_statuses=frozenset({200}),
        ),
        "/v5/execution/list": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="EXECUTIONS",
            success_statuses=frozenset({200}),
        ),
        "/v5/position/list": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="POSITION.READ",
            data_entitlement="POSITIONS",
            success_statuses=frozenset({200}),
        ),
        "/v5/account/wallet-balance": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            data_entitlement="BALANCES",
            success_statuses=frozenset({200}),
        ),
        "/v5/account/transaction-log": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ACCOUNT.READ",
            data_entitlement="ACTIVITIES",
            success_statuses=frozenset({200}),
        ),
    }
)


def _bybit_authenticated_read_rule(
    binding: AuthenticatedReadQueryBinding,
) -> AuthenticatedReadEndpointRule:
    rule = BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS.get(binding.endpoint)
    if rule is None:
        raise ProviderTransportScopeError(
            "Bybit authenticated-read endpoint is not explicitly allowed"
        )
    if binding.surface != rule.surface:
        raise ProviderTransportScopeError(
            "authenticated-read endpoint surface does not match Bybit policy"
        )
    if binding.permission_scope != rule.permission_scope:
        raise ProviderTransportScopeError(
            "authenticated-read permission scope does not match Bybit endpoint policy"
        )
    return rule


def _binance_authenticated_read_rule(
    binding: AuthenticatedReadQueryBinding,
) -> AuthenticatedReadEndpointRule:
    rule = BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS.get(binding.endpoint)
    if rule is None:
        raise ProviderTransportScopeError(
            "Binance authenticated-read endpoint is not explicitly allowed"
        )
    if binding.surface != rule.surface:
        raise ProviderTransportScopeError(
            "authenticated-read endpoint surface does not match provider policy"
        )
    if binding.permission_scope != rule.permission_scope:
        raise ProviderTransportScopeError(
            "authenticated-read permission scope does not match provider endpoint policy"
        )
    return rule


@dataclass(frozen=True)
class SignedHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_seconds: int

    def __post_init__(self) -> None:
        method = _text(self.method, name="method").upper()
        if method != "POST":
            raise ProviderTransportScopeError(
                "trade transport currently permits only POST"
            )
        parsed = urlsplit(_text(self.url, name="url"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ProviderTransportScopeError("signed request URL is invalid")
        if type(self.body) is not bytes or not self.body:
            raise ProviderTransportScopeError(
                "signed request body must be non-empty exact bytes"
            )
        if not isinstance(self.headers, Mapping):
            raise ProviderTransportScopeError("headers must be a mapping")
        normalized_headers: dict[str, str] = {}
        for raw_key, raw_value in self.headers.items():
            key = _text(raw_key, name="header name")
            value = _text(raw_value, name=f"header {key}")
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ProviderTransportScopeError(
                    "header values must not contain line breaks"
                )
            normalized_headers[key] = value
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
            or self.timeout_seconds > 120
        ):
            raise ProviderTransportScopeError("invalid request timeout")
        object.__setattr__(self, "method", method)
        object.__setattr__(
            self, "headers", MappingProxyType(dict(normalized_headers))
        )


@dataclass(frozen=True)
class AuthenticatedReadHttpRequest:
    """One immutable authenticated provider GET request.

    This is intentionally separate from SignedHttpRequest so the write transport
    cannot accidentally broaden its POST-only contract or final-send semantics.
    """

    url: str
    headers: Mapping[str, str]
    timeout_seconds: int

    def __post_init__(self) -> None:
        parsed = urlsplit(_text(self.url, name="url"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.query
        ):
            raise ProviderTransportScopeError(
                "authenticated-read URL must be HTTPS with an exact signed query"
            )
        if not isinstance(self.headers, Mapping):
            raise ProviderTransportScopeError("headers must be a mapping")
        normalized_headers: dict[str, str] = {}
        for raw_key, raw_value in self.headers.items():
            key = _text(raw_key, name="header name")
            value = _text(raw_value, name=f"header {key}")
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ProviderTransportScopeError(
                    "header values must not contain line breaks"
                )
            normalized_headers[key] = value
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
            or self.timeout_seconds > 120
        ):
            raise ProviderTransportScopeError("invalid request timeout")
        object.__setattr__(
            self,
            "headers",
            MappingProxyType(dict(normalized_headers)),
        )


@dataclass(frozen=True)
class TradingWireResponse:
    """Definitive HTTP response observed after one guarded write send."""

    http_status: int
    body: bytes

    def __post_init__(self) -> None:
        if (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ProviderTransportScopeError("HTTP status must be an integer 100..599")
        if type(self.body) is not bytes or not self.body:
            raise ProviderTransportError(
                "provider returned an empty or non-byte trading response"
            )


@dataclass(frozen=True)
class AuthenticatedReadWireResponse:
    http_status: int
    body: bytes

    def __post_init__(self) -> None:
        if (
            isinstance(self.http_status, bool)
            or not isinstance(self.http_status, int)
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ProviderTransportScopeError("HTTP status must be an integer 100..599")
        if type(self.body) is not bytes or not self.body:
            raise ProviderTransportError(
                "provider returned an empty or non-byte authenticated-read response"
            )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibJsonWireClient:
    """One-shot TLS client with redirects and automatic retries disabled."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def send(
        self,
        request: SignedHttpRequest | AuthenticatedReadHttpRequest,
    ) -> bytes | TradingWireResponse | AuthenticatedReadWireResponse:
        if not isinstance(
            request,
            (SignedHttpRequest, AuthenticatedReadHttpRequest),
        ):
            raise TypeError(
                "request must be SignedHttpRequest or AuthenticatedReadHttpRequest"
            )
        if isinstance(request, SignedHttpRequest):
            data = request.body
            method = request.method
        else:
            data = None
            method = "GET"
        outbound = Request(
            request.url,
            data=data,
            headers=dict(request.headers),
            method=method,
        )
        is_authenticated_read = isinstance(
            request,
            AuthenticatedReadHttpRequest,
        )
        http_status: int | None = None
        try:
            with self._opener.open(
                outbound,
                timeout=request.timeout_seconds,
            ) as response:
                http_status = int(response.status)
                raw = response.read()
        except HTTPError as error:
            # Redirects are prohibited for both reads and writes. For reads,
            # preserve non-redirect HTTP status as a typed outcome so an error
            # body can never be promoted to successful provider state.
            if 300 <= int(error.code) < 400:
                raise ProviderTransportError(
                    "provider redirect is prohibited"
                ) from error
            raw = error.read()
            if type(raw) is not bytes or not raw:
                raise ProviderTransportError(
                    "provider returned an empty HTTP error response"
                ) from error
            if is_authenticated_read:
                return AuthenticatedReadWireResponse(
                    http_status=int(error.code),
                    body=raw,
                )
            return TradingWireResponse(
                http_status=int(error.code),
                body=raw,
            )
        if type(raw) is not bytes or not raw:
            raise ProviderTransportError(
                "provider returned an empty or non-byte response"
            )
        if is_authenticated_read:
            if http_status is None:
                raise ProviderTransportError(
                    "authenticated-read HTTP status is unavailable"
                )
            return AuthenticatedReadWireResponse(
                http_status=http_status,
                body=raw,
            )
        if http_status is None:
            raise ProviderTransportError(
                "trading HTTP status is unavailable"
            )
        return TradingWireResponse(
            http_status=http_status,
            body=raw,
        )


def _exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Preserve HTTP status when the wire client can prove a definitive response.

    Raw bytes remain accepted for injected legacy/test wire clients. Production
    UrllibJsonWireClient always returns TradingWireResponse for guarded writes.
    """
    if isinstance(value, TradingWireResponse):
        return ExactJsonTransportResponse(
            value.body,
            http_status=value.http_status,
        )
    if type(value) is bytes:
        return ExactJsonTransportResponse(value)
    raise ProviderTransportError(
        "trading wire client returned an unsupported response contract"
    )


@dataclass(frozen=True)
class WhiteBitCredential:
    api_key: str
    api_secret: str

    @classmethod
    def parse(cls, plaintext: object) -> "WhiteBitCredential":
        if not isinstance(plaintext, str) or not plaintext:
            raise ProviderTransportScopeError(
                "WhiteBIT credential material is unavailable"
            )
        try:
            value = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ProviderTransportScopeError(
                "WhiteBIT credential material has invalid format"
            ) from error
        if not isinstance(value, dict) or set(value) != {
            "api_key",
            "api_secret",
        }:
            raise ProviderTransportScopeError(
                "WhiteBIT credential material must contain exact api_key/api_secret fields"
            )
        return cls(
            api_key=_canonical_text(value["api_key"], name="api_key"),
            api_secret=_canonical_text(value["api_secret"], name="api_secret"),
        )


class _DurableProviderNonceAllocator:
    """Single journal-backed monotonic nonce authority shared by provider transports."""

    AGGREGATE_TYPE = "provider_nonce"
    EVENT_TYPE = "ProviderNonceAllocated"

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        journal: JournalStore,
        account_id: str,
        environment: str,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc | None = None,
        max_contention_retries: int = 32,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        provider = _canonical_text(provider_id, name="provider_id").upper()
        label = _canonical_text(display_name, name="display_name")
        account = _canonical_text(account_id, name="account_id")
        env = _canonical_environment(environment)
        if env != "LIVE":
            raise ProviderTransportScopeError(
                f"{label} durable nonce allocation is qualified only for LIVE"
            )
        if not callable(clock_millis):
            raise TypeError("clock_millis must be callable")
        if clock_utc is not None and not callable(clock_utc):
            raise TypeError("clock_utc must be callable or None")
        if (
            isinstance(max_contention_retries, bool)
            or not isinstance(max_contention_retries, int)
            or max_contention_retries < 1
            or max_contention_retries > 1024
        ):
            raise ProviderTransportScopeError(
                "max_contention_retries must be an integer from 1 through 1024"
            )
        self.provider_id = provider
        self.display_name = label
        self.journal = journal
        self.account_id = account
        self.environment = env
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc or (lambda: datetime.now(timezone.utc))
        self.max_contention_retries = max_contention_retries
        self.aggregate_id = (
            self.provider_id
            + ":"
            + sha256(
                f"{self.account_id}|{self.environment}".encode("utf-8")
            ).hexdigest()
        )

    def _history(self) -> tuple[int, int]:
        events = self.journal.load_events(self.AGGREGATE_TYPE, self.aggregate_id)
        previous_nonce = 0
        previous_version = 0
        for event in events:
            if event.get("event_type") != self.EVENT_TYPE:
                raise ProviderTransportError(
                    f"{self.display_name} nonce journal contains an unexpected event type"
                )
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ProviderTransportError(
                    f"{self.display_name} nonce journal payload is invalid"
                )
            if (
                payload.get("provider_id") != self.provider_id
                or payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
            ):
                raise ProviderTransportError(
                    f"{self.display_name} nonce journal scope does not match allocator"
                )
            nonce = payload.get("nonce")
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or nonce <= previous_nonce
            ):
                raise ProviderTransportError(
                    f"{self.display_name} nonce journal is not strictly monotonic"
                )
            version = event.get("aggregate_version")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version != previous_version + 1
            ):
                raise ProviderTransportError(
                    f"{self.display_name} nonce journal aggregate sequence is invalid"
                )
            previous_nonce = nonce
            previous_version = version
        return previous_nonce, previous_version

    def allocate(self) -> int:
        for _ in range(self.max_contention_retries):
            previous_nonce, previous_version = self._history()
            candidate = self.clock_millis()
            if (
                isinstance(candidate, bool)
                or not isinstance(candidate, int)
                or candidate <= 0
            ):
                raise ProviderTransportScopeError(
                    f"{self.display_name} nonce clock must return a positive integer"
                )
            nonce = max(candidate, previous_nonce + 1)
            committed_at = self.clock_utc()
            if (
                not isinstance(committed_at, datetime)
                or committed_at.tzinfo is None
                or committed_at.utcoffset() is None
            ):
                raise ProviderTransportScopeError(
                    "clock_utc must return a timezone-aware datetime"
                )
            committed_at = committed_at.astimezone(timezone.utc)
            version = previous_version + 1
            payload = {
                "provider_id": self.provider_id,
                "account_id": self.account_id,
                "environment": self.environment,
                "nonce": nonce,
            }
            allocation_identity = (
                f"{self.aggregate_id}|{version}|{nonce}|"
                f"{committed_at.isoformat()}"
            )
            envelope = {
                "event_id": self.provider_id.lower()
                + "-nonce-"
                + sha256(allocation_identity.encode("utf-8")).hexdigest()[:40],
                "event_type": self.EVENT_TYPE,
                "aggregate_type": self.AGGREGATE_TYPE,
                "aggregate_id": self.aggregate_id,
                "aggregate_version": str(version),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": committed_at.isoformat().replace("+00:00", "Z"),
            }
            try:
                result = self.journal.append_event(envelope)
            except ValueError as error:
                if "aggregate_version must be" in str(error):
                    continue
                raise
            if not result.inserted:
                continue
            return nonce
        raise ProviderTransportError(
            f"{self.display_name} nonce allocation exceeded local contention budget"
        )

    def __call__(self) -> int:
        return self.allocate()


class WhiteBitDurableNonceAllocator(_DurableProviderNonceAllocator):
    """Journal-backed monotonic WhiteBIT nonce authority."""

    def __init__(
        self,
        *,
        journal: JournalStore,
        account_id: str,
        environment: str,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc | None = None,
        max_contention_retries: int = 32,
    ) -> None:
        super().__init__(
            provider_id="WHITEBIT",
            display_name="WhiteBIT",
            journal=journal,
            account_id=account_id,
            environment=environment,
            clock_millis=clock_millis,
            clock_utc=clock_utc,
            max_contention_retries=max_contention_retries,
        )


class KrakenSpotDurableNonceAllocator(_DurableProviderNonceAllocator):
    """Journal-backed monotonic Kraken Spot nonce authority."""

    def __init__(
        self,
        *,
        journal: JournalStore,
        account_id: str,
        environment: str,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc | None = None,
        max_contention_retries: int = 32,
    ) -> None:
        super().__init__(
            provider_id="KRAKEN",
            display_name="Kraken Spot",
            journal=journal,
            account_id=account_id,
            environment=environment,
            clock_millis=clock_millis,
            clock_utc=clock_utc,
            max_contention_retries=max_contention_retries,
        )


class WhiteBitHttpTransport:
    """GuardedDispatcher-compatible WhiteBIT LIVE order transport.

    Quota admission, durable nonce allocation, secret resolution and signing all
    complete before the dispatcher's final guard. After the guard, the only
    operation is one HTTP POST. The transport owns no retry or fill authority.
    """

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        account_id: str,
        capability_snapshot_id: str,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        nonce_allocator: WhiteBitDurableNonceAllocator,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "WHITEBIT" or policy.environment != "LIVE":
            raise ProviderTransportScopeError(
                "WhiteBIT order transport requires WHITEBIT LIVE policy"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "WHITEBIT"
            or credential_handle.environment != "LIVE"
            or credential_handle.purpose != "TRADE"
        ):
            raise ProviderTransportScopeError(
                "credential handle provider/environment/purpose mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if not isinstance(nonce_allocator, WhiteBitDurableNonceAllocator):
            raise TypeError(
                "nonce_allocator must be WhiteBitDurableNonceAllocator"
            )
        if (
            nonce_allocator.account_id != account
            or nonce_allocator.environment != "LIVE"
        ):
            raise ProviderTransportScopeError(
                "nonce allocator account/environment mismatch"
            )
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")

        self.policy = policy
        self.account_id = account
        self.capability_snapshot_id = _canonical_text(
            capability_snapshot_id,
            name="capability_snapshot_id",
        )
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _canonical_text(session_token, name="session_token")
        self.origin = _canonical_text(origin, name="origin")
        self.execution_identity = _canonical_text(
            execution_identity,
            name="execution_identity",
        )
        self.nonce_allocator = nonce_allocator
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()

    @staticmethod
    def _prepared_fields(
        request: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, object], str]:
        if not isinstance(request, Mapping):
            raise ProviderTransportScopeError(
                "prepared provider request must be a mapping"
            )
        if set(request) != {
            "endpoint",
            "body",
            "capability_snapshot_id",
        }:
            raise ProviderTransportScopeError(
                "prepared WhiteBIT request fields are not canonical"
            )
        endpoint = _canonical_text(request["endpoint"], name="endpoint")
        if endpoint not in WHITEBIT_ORDER_ENDPOINTS:
            raise ProviderTransportScopeError(
                "prepared WhiteBIT endpoint must be a canonical order path"
            )
        body = request["body"]
        if not isinstance(body, Mapping):
            raise ProviderTransportScopeError(
                "prepared WhiteBIT request body must be a mapping"
            )
        normalized = dict(body)
        if {"request", "nonce", "nonceWindow"} & set(normalized):
            raise ProviderTransportScopeError(
                "prepared WhiteBIT body contains transport-owned authentication fields"
            )
        capability = _canonical_text(
            request["capability_snapshot_id"],
            name="capability_snapshot_id",
        )
        return endpoint, MappingProxyType(normalized), capability

    def __call__(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard: Callable[[], None],
    ) -> ExactJsonTransportResponse:
        if not callable(final_guard):
            raise TypeError("final_guard must be callable")
        client_id = validate_client_order_id(client_order_id)
        endpoint, body, capability = self._prepared_fields(request)
        if capability != self.capability_snapshot_id:
            raise ProviderTransportScopeError(
                "prepared request capability snapshot mismatch"
            )
        if body.get("clientOrderId") != client_id:
            raise ProviderTransportScopeError(
                "prepared request client order identity mismatch"
            )

        if self.quota_gate is not None:
            self.quota_gate(
                "WHITEBIT",
                self.account_id,
                "LIVE",
                "ORDER_WRITE",
            )

        nonce = self.nonce_allocator.allocate()
        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="WHITEBIT",
            environment="LIVE",
            purpose="TRADE",
        )
        try:
            credential = WhiteBitCredential.parse(credential_plaintext)
            provider_signed = sign_private_request(
                endpoint=endpoint,
                parameters=body,
                nonce=nonce,
                api_key=credential.api_key,
                api_secret=credential.api_secret,
                nonce_window=False,
            )
            signed = SignedHttpRequest(
                method="POST",
                url=self.policy.absolute_url(provider_signed.endpoint),
                headers=provider_signed.headers,
                body=provider_signed.body,
                timeout_seconds=self.policy.timeout_seconds,
            )
        finally:
            credential_plaintext = None

        final_guard()
        wire_response = self.wire_client.send(signed)
        return _exact_trading_response(wire_response)


@dataclass(frozen=True)
class KrakenSpotCredential:
    api_key: str
    api_secret: str

    @classmethod
    def parse(cls, plaintext: object) -> "KrakenSpotCredential":
        if not isinstance(plaintext, str) or not plaintext:
            raise ProviderTransportScopeError(
                "Kraken Spot credential material is unavailable"
            )
        try:
            value = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ProviderTransportScopeError(
                "Kraken Spot credential material has invalid format"
            ) from error
        if not isinstance(value, dict) or set(value) != {
            "api_key",
            "api_secret",
        }:
            raise ProviderTransportScopeError(
                "Kraken Spot credential material must contain exact api_key/api_secret fields"
            )
        api_key = _canonical_text(value["api_key"], name="api_key")
        api_secret = _canonical_text(value["api_secret"], name="api_secret")
        try:
            decoded = base64.b64decode(api_secret, validate=True)
        except (ValueError, binascii.Error, UnicodeEncodeError) as error:
            raise ProviderTransportScopeError(
                "Kraken Spot api_secret must be canonical base64"
            ) from error
        if not decoded:
            raise ProviderTransportScopeError(
                "Kraken Spot api_secret must decode to non-empty bytes"
            )
        return cls(api_key=api_key, api_secret=api_secret)


class KrakenSpotSigner:
    """Pure Kraken Spot REST HMAC-SHA512 signer."""

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        endpoint: str,
        body: Mapping[str, object],
        credential_plaintext: str,
        nonce: int,
    ) -> SignedHttpRequest:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "KRAKEN" or policy.environment != "LIVE":
            raise ProviderTransportScopeError(
                "Kraken Spot signer requires KRAKEN LIVE policy"
            )
        path = _canonical_text(endpoint, name="endpoint")
        if path != "/0/private/AddOrder":
            raise ProviderTransportScopeError(
                "Kraken Spot signer permits only the canonical AddOrder path"
            )
        if not isinstance(body, Mapping):
            raise ProviderTransportScopeError(
                "Kraken Spot signer body must be a mapping"
            )
        if isinstance(nonce, bool) or not isinstance(nonce, int) or nonce <= 0:
            raise ProviderTransportScopeError(
                "Kraken Spot nonce must be a positive integer"
            )
        canonical: dict[str, str] = {}
        for key, value in body.items():
            canonical_key = _canonical_text(key, name="Kraken Spot parameter")
            if canonical_key in {"nonce", "otp"}:
                raise ProviderTransportScopeError(
                    "prepared Kraken Spot body contains transport-owned authentication fields"
                )
            canonical_value = _canonical_text(
                value,
                name=f"Kraken Spot parameter {canonical_key}",
            )
            canonical[canonical_key] = canonical_value
        canonical["nonce"] = str(nonce)
        exact_body = urlencode(sorted(canonical.items())).encode("ascii")
        credential = KrakenSpotCredential.parse(credential_plaintext)
        message_digest = sha256(
            str(nonce).encode("ascii") + exact_body
        ).digest()
        message = path.encode("ascii") + message_digest
        secret = base64.b64decode(credential.api_secret, validate=True)
        signature = base64.b64encode(
            hmac.new(secret, message, sha512).digest()
        ).decode("ascii")
        return SignedHttpRequest(
            method="POST",
            url=policy.absolute_url(path),
            headers=MappingProxyType(
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "API-Key": credential.api_key,
                    "API-Sign": signature,
                }
            ),
            body=exact_body,
            timeout_seconds=policy.timeout_seconds,
        )


def _kraken_spot_prepared_body(
    body: object,
) -> Mapping[str, object]:
    if not isinstance(body, Mapping):
        raise ProviderTransportScopeError(
            "prepared Kraken Spot request body must be a mapping"
        )
    normalized = dict(body)
    required = {
        "pair",
        "type",
        "ordertype",
        "volume",
        "cl_ord_id",
        "timeinforce",
    }
    optional = {"price", "oflags"}
    if not required <= set(normalized) or set(normalized) - required - optional:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot AddOrder body is not canonical"
        )
    if {"nonce", "otp"} & set(normalized):
        raise ProviderTransportScopeError(
            "prepared Kraken Spot body contains transport-owned authentication fields"
        )

    pair = _canonical_text(normalized["pair"], name="pair")
    if pair != pair.upper():
        raise ProviderTransportScopeError(
            "prepared Kraken Spot pair must be uppercase"
        )
    side = _canonical_text(normalized["type"], name="type")
    if side not in {"buy", "sell"}:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot type must be buy or sell"
        )
    order_type = _canonical_text(normalized["ordertype"], name="ordertype")
    if order_type not in {"market", "limit"}:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot ordertype must be market or limit"
        )
    tif = _canonical_text(normalized["timeinforce"], name="timeinforce")
    if tif not in {"gtc", "ioc"}:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot timeinforce must be gtc or ioc"
        )
    for field in ("volume", "price"):
        value = normalized.get(field)
        if value is None:
            if field == "price" and order_type == "market":
                continue
            if field == "price":
                raise ProviderTransportScopeError(
                    "prepared Kraken Spot limit order requires price"
                )
            raise ProviderTransportScopeError(
                "prepared Kraken Spot volume is required"
            )
        text = _canonical_text(value, name=field)
        try:
            decimal_value = Decimal(text)
        except (InvalidOperation, ValueError) as error:
            raise ProviderTransportScopeError(
                f"prepared Kraken Spot {field} must be an exact decimal"
            ) from error
        if not decimal_value.is_finite() or decimal_value <= 0:
            raise ProviderTransportScopeError(
                f"prepared Kraken Spot {field} must be positive and finite"
            )
        if format(decimal_value, "f") != text:
            raise ProviderTransportScopeError(
                f"prepared Kraken Spot {field} must be canonical decimal text"
            )
    if order_type == "market" and "price" in normalized:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot market order must omit price"
        )
    flags = normalized.get("oflags")
    if flags is not None:
        if _canonical_text(flags, name="oflags") != "post":
            raise ProviderTransportScopeError(
                "prepared Kraken Spot oflags is unsupported"
            )
        if order_type != "limit" or tif == "ioc":
            raise ProviderTransportScopeError(
                "prepared Kraken Spot post-only order shape is invalid"
            )
    try:
        client_id = validate_spot_client_order_id(normalized["cl_ord_id"])
    except ValueError as error:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot client order id is invalid"
        ) from error
    normalized["pair"] = pair
    normalized["type"] = side
    normalized["ordertype"] = order_type
    normalized["timeinforce"] = tif
    normalized["cl_ord_id"] = client_id
    return MappingProxyType(normalized)


class KrakenSpotHttpTransport:
    """GuardedDispatcher-compatible Kraken Spot LIVE AddOrder transport."""

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        account_id: str,
        capability_snapshot_id: str,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        nonce_allocator: KrakenSpotDurableNonceAllocator,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "KRAKEN" or policy.environment != "LIVE":
            raise ProviderTransportScopeError(
                "Kraken Spot order transport requires KRAKEN LIVE policy"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "KRAKEN"
            or credential_handle.environment != "LIVE"
            or credential_handle.purpose != "TRADE"
        ):
            raise ProviderTransportScopeError(
                "credential handle provider/environment/purpose mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if not isinstance(nonce_allocator, KrakenSpotDurableNonceAllocator):
            raise TypeError(
                "nonce_allocator must be KrakenSpotDurableNonceAllocator"
            )
        if (
            nonce_allocator.account_id != account
            or nonce_allocator.environment != "LIVE"
        ):
            raise ProviderTransportScopeError(
                "nonce allocator account/environment mismatch"
            )
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")

        self.policy = policy
        self.account_id = account
        self.capability_snapshot_id = _canonical_text(
            capability_snapshot_id,
            name="capability_snapshot_id",
        )
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _canonical_text(session_token, name="session_token")
        self.origin = _canonical_text(origin, name="origin")
        self.execution_identity = _canonical_text(
            execution_identity,
            name="execution_identity",
        )
        self.nonce_allocator = nonce_allocator
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()

    @staticmethod
    def _prepared_fields(
        request: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, object], str]:
        if not isinstance(request, Mapping):
            raise ProviderTransportScopeError(
                "prepared provider request must be a mapping"
            )
        if set(request) != {
            "endpoint",
            "body",
            "capability_snapshot_id",
        }:
            raise ProviderTransportScopeError(
                "prepared Kraken Spot request fields are not canonical"
            )
        endpoint = _canonical_text(request["endpoint"], name="endpoint")
        if endpoint != "/0/private/AddOrder":
            raise ProviderTransportScopeError(
                "prepared Kraken Spot endpoint must be the canonical AddOrder path"
            )
        body = _kraken_spot_prepared_body(request["body"])
        capability = _canonical_text(
            request["capability_snapshot_id"],
            name="capability_snapshot_id",
        )
        return endpoint, body, capability

    def __call__(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard: Callable[[], None],
    ) -> ExactJsonTransportResponse:
        if not callable(final_guard):
            raise TypeError("final_guard must be callable")
        try:
            client_id = validate_spot_client_order_id(client_order_id)
        except ValueError as error:
            raise ProviderTransportScopeError(
                "Kraken Spot client order id is invalid"
            ) from error
        endpoint, body, capability = self._prepared_fields(request)
        if capability != self.capability_snapshot_id:
            raise ProviderTransportScopeError(
                "prepared request capability snapshot mismatch"
            )
        if body.get("cl_ord_id") != client_id:
            raise ProviderTransportScopeError(
                "prepared request client order identity mismatch"
            )

        if self.quota_gate is not None:
            self.quota_gate(
                "KRAKEN",
                self.account_id,
                "LIVE",
                "ORDER_WRITE",
            )

        nonce = self.nonce_allocator.allocate()
        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="KRAKEN",
            environment="LIVE",
            purpose="TRADE",
        )
        try:
            signed = KrakenSpotSigner.sign(
                policy=self.policy,
                endpoint=endpoint,
                body=body,
                credential_plaintext=credential_plaintext,
                nonce=nonce,
            )
        finally:
            credential_plaintext = None

        final_guard()
        wire_response = self.wire_client.send(signed)
        return _exact_trading_response(wire_response)


@dataclass(frozen=True)
class AlpacaTradingCredential:
    api_key: str
    api_secret: str

    @classmethod
    def parse(cls, plaintext: object) -> "AlpacaTradingCredential":
        if not isinstance(plaintext, str) or not plaintext:
            raise ProviderTransportScopeError(
                "Alpaca credential material is unavailable"
            )
        try:
            value = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ProviderTransportScopeError(
                "Alpaca credential material has invalid format"
            ) from error
        if not isinstance(value, dict) or set(value) != {
            "api_key",
            "api_secret",
        }:
            raise ProviderTransportScopeError(
                "Alpaca credential material must contain exact api_key/api_secret fields"
            )
        return cls(
            api_key=_canonical_text(value["api_key"], name="api_key"),
            api_secret=_canonical_text(value["api_secret"], name="api_secret"),
        )


def _reject_binary_float(value: object, *, path: str = "body") -> None:
    if isinstance(value, float):
        raise ProviderTransportScopeError(
            f"{path} must not contain binary floating financial values"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_binary_float(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_binary_float(item, path=f"{path}[{index}]")


class AlpacaTradingHttpTransport:
    """GuardedDispatcher-compatible Alpaca Trading API order transport.

    Authentication, host selection and quota admission complete before the
    dispatcher's final guard. Exactly one POST occurs after that guard. Any
    post-guard transport ambiguity propagates to GuardedDispatcher as UNKNOWN.
    """

    ORDER_ENDPOINT = "/v2/orders"

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        account_id: str,
        capability_snapshot_id: str,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "ALPACA":
            raise ProviderTransportScopeError(
                "Alpaca Trading transport requires ALPACA policy"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != policy.provider_id
            or credential_handle.environment != policy.environment
            or credential_handle.purpose != "TRADE"
        ):
            raise ProviderTransportScopeError(
                "credential handle provider/environment/purpose mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")

        self.policy = policy
        self.account_id = account
        self.capability_snapshot_id = _canonical_text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _canonical_text(
            session_token, name="session_token"
        )
        self.origin = _canonical_text(origin, name="origin")
        self.execution_identity = _canonical_text(
            execution_identity, name="execution_identity"
        )
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()

    @staticmethod
    def _prepared_fields(
        request: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, object], str, str, str, str]:
        if not isinstance(request, Mapping):
            raise ProviderTransportScopeError(
                "prepared provider request must be a mapping"
            )
        expected = {
            "endpoint",
            "body",
            "account_id",
            "environment",
            "capability_snapshot_id",
            "capability_snapshot_ids",
            "instrument_versions",
            "body_sha256",
        }
        if set(request) != expected:
            raise ProviderTransportScopeError(
                "prepared Alpaca request fields are not canonical"
            )
        endpoint = _canonical_text(request["endpoint"], name="endpoint")
        if endpoint != AlpacaTradingHttpTransport.ORDER_ENDPOINT:
            raise ProviderTransportScopeError(
                "Alpaca transport received an unsupported endpoint"
            )
        body = request["body"]
        if not isinstance(body, Mapping) or not body:
            raise ProviderTransportScopeError(
                "prepared Alpaca request body must be a non-empty mapping"
            )
        _reject_binary_float(body)
        account = _canonical_text(request["account_id"], name="account_id")
        environment = _canonical_environment(request["environment"])
        capability = _canonical_text(
            request["capability_snapshot_id"],
            name="capability_snapshot_id",
        )
        raw_capabilities = request["capability_snapshot_ids"]
        raw_instruments = request["instrument_versions"]
        if (
            not isinstance(raw_capabilities, (list, tuple))
            or not raw_capabilities
            or not isinstance(raw_instruments, (list, tuple))
            or not raw_instruments
            or len(raw_capabilities) != len(raw_instruments)
        ):
            raise ProviderTransportScopeError(
                "Alpaca capability and instrument bindings must be aligned non-empty sequences"
            )
        capabilities = tuple(
            _canonical_text(value, name="capability_snapshot_id")
            for value in raw_capabilities
        )
        if capability not in capabilities:
            raise ProviderTransportScopeError(
                "primary Alpaca capability snapshot is not in the prepared binding"
            )
        for value in raw_instruments:
            _canonical_text(value, name="instrument_version")

        rendered = json.dumps(
            dict(body),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        digest = _canonical_text(request["body_sha256"], name="body_sha256")
        actual_digest = "sha256:" + sha256(rendered).hexdigest()
        if digest != actual_digest:
            raise ProviderTransportScopeError(
                "prepared Alpaca request body digest mismatch"
            )
        return (
            endpoint,
            MappingProxyType(dict(body)),
            account,
            environment,
            capability,
            actual_digest,
        )

    def __call__(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard: Callable[[], None],
    ) -> ExactJsonTransportResponse:
        if not callable(final_guard):
            raise TypeError("final_guard must be callable")
        client_id = _canonical_text(
            client_order_id, name="client_order_id"
        )
        endpoint, body, account, environment, capability, _digest = (
            self._prepared_fields(request)
        )
        if account != self.account_id:
            raise ProviderTransportScopeError(
                "prepared request account mismatch"
            )
        if environment != self.policy.environment:
            raise ProviderTransportScopeError(
                "prepared request environment mismatch"
            )
        if capability != self.capability_snapshot_id:
            raise ProviderTransportScopeError(
                "prepared request capability snapshot mismatch"
            )
        if body.get("client_order_id") != client_id:
            raise ProviderTransportScopeError(
                "prepared request client order identity mismatch"
            )

        if self.quota_gate is not None:
            self.quota_gate(
                self.policy.provider_id,
                self.account_id,
                self.policy.environment,
                "ORDER_WRITE",
            )

        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            purpose="TRADE",
        )
        try:
            credential = AlpacaTradingCredential.parse(
                credential_plaintext
            )
            exact_body = json.dumps(
                dict(body),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            signed = SignedHttpRequest(
                method="POST",
                url=self.policy.absolute_url(endpoint),
                headers=MappingProxyType(
                    {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "APCA-API-KEY-ID": credential.api_key,
                        "APCA-API-SECRET-KEY": credential.api_secret,
                    }
                ),
                body=exact_body,
                timeout_seconds=self.policy.timeout_seconds,
            )
        finally:
            credential_plaintext = None

        final_guard()
        wire_response = self.wire_client.send(signed)
        return _exact_trading_response(wire_response)


@dataclass(frozen=True)
class BybitV5Credential:
    api_key: str
    api_secret: str

    @classmethod
    def parse(cls, plaintext: object) -> "BybitV5Credential":
        if not isinstance(plaintext, str) or not plaintext:
            raise ProviderTransportScopeError(
                "Bybit credential material is unavailable"
            )
        try:
            value = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ProviderTransportScopeError(
                "Bybit credential material has invalid format"
            ) from error
        if not isinstance(value, dict) or set(value) != {
            "api_key",
            "api_secret",
        }:
            raise ProviderTransportScopeError(
                "Bybit credential material must contain exact api_key/api_secret fields"
            )
        return cls(
            api_key=_canonical_text(value["api_key"], name="api_key"),
            api_secret=_canonical_text(value["api_secret"], name="api_secret"),
        )


class BybitV5Signer:
    """Pure Bybit V5 HMAC signer over exact canonical order bytes."""

    PLACE_ORDER_ENDPOINT = "/v5/order/create"

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        endpoint: object,
        body: object,
        credential_plaintext: object,
        timestamp_ms: object,
        recv_window_ms: int = 5000,
    ) -> SignedHttpRequest:
        if policy.provider_id != "BYBIT":
            raise ProviderTransportScopeError(
                "Bybit signer requires a BYBIT endpoint policy"
            )
        path = _canonical_text(endpoint, name="endpoint")
        if path != BybitV5Signer.PLACE_ORDER_ENDPOINT:
            raise ProviderTransportScopeError(
                "Bybit V5 trade signer received an unsupported endpoint"
            )
        if not isinstance(body, Mapping) or not body:
            raise ProviderTransportScopeError(
                "Bybit order body must be a non-empty mapping"
            )
        _reject_binary_float(body)
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            raise ProviderTransportScopeError(
                "timestamp_ms must be a non-negative integer"
            )
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )
        credential = BybitV5Credential.parse(credential_plaintext)
        exact_body = json.dumps(
            dict(body),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        signing_material = (
            str(timestamp_ms)
            + credential.api_key
            + str(recv_window_ms)
        ).encode("utf-8") + exact_body
        signature = hmac.new(
            credential.api_secret.encode("utf-8"),
            signing_material,
            sha256,
        ).hexdigest()
        return SignedHttpRequest(
            method="POST",
            url=policy.absolute_url(path),
            headers=MappingProxyType(
                {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-BAPI-API-KEY": credential.api_key,
                    "X-BAPI-TIMESTAMP": str(timestamp_ms),
                    "X-BAPI-RECV-WINDOW": str(recv_window_ms),
                    "X-BAPI-SIGN": signature,
                }
            ),
            body=exact_body,
            timeout_seconds=policy.timeout_seconds,
        )


class BybitV5HttpTransport:
    """GuardedDispatcher-compatible Bybit V5 MAINNET/TESTNET/DEMO transport."""

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        provider_environment: str,
        account_id: str,
        capability_snapshot_id: str,
        capability_registry: CapabilityRegistry,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        provider_env = _canonical_text(
            provider_environment, name="provider_environment"
        ).upper()
        canonical_policy = BYBIT_V5_ENDPOINT_POLICIES.get(provider_env)
        if canonical_policy is None:
            raise ProviderTransportScopeError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if policy != canonical_policy:
            raise ProviderTransportScopeError(
                "Bybit policy does not match exact provider environment"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "BYBIT"
            or credential_handle.environment != policy.environment
            or credential_handle.purpose != "TRADE"
        ):
            raise ProviderTransportScopeError(
                "credential handle provider/environment/purpose mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        if not isinstance(capability_registry, CapabilityRegistry):
            raise TypeError("capability_registry must be CapabilityRegistry")
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if not callable(clock_millis) or not callable(clock_utc):
            raise TypeError("Bybit write clocks must be callable")
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )

        self.policy = policy
        self.provider_environment = provider_env
        self.account_id = account
        self.capability_snapshot_id = _canonical_text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        self.capability_registry = capability_registry
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _canonical_text(
            session_token, name="session_token"
        )
        self.origin = _canonical_text(origin, name="origin")
        self.execution_identity = _canonical_text(
            execution_identity, name="execution_identity"
        )
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()
        self.recv_window_ms = recv_window_ms

    @staticmethod
    def _prepared_fields(
        request: Mapping[str, Any],
    ) -> tuple[
        str,
        Mapping[str, object],
        str,
        str,
        str,
        str,
        str,
        str,
        str,
    ]:
        if not isinstance(request, Mapping):
            raise ProviderTransportScopeError(
                "prepared provider request must be a mapping"
            )
        expected = {
            "endpoint",
            "body",
            "account_id",
            "environment",
            "provider_environment",
            "capability_snapshot_id",
            "entity_id",
            "capability_snapshot_ids",
            "instrument_versions",
            "body_sha256",
        }
        if set(request) != expected:
            raise ProviderTransportScopeError(
                "prepared Bybit request fields are not canonical"
            )
        endpoint = _canonical_text(request["endpoint"], name="endpoint")
        if endpoint != BybitV5Signer.PLACE_ORDER_ENDPOINT:
            raise ProviderTransportScopeError(
                "Bybit transport received an unsupported endpoint"
            )
        body = request["body"]
        if not isinstance(body, Mapping) or not body:
            raise ProviderTransportScopeError(
                "prepared Bybit request body must be a non-empty mapping"
            )
        _reject_binary_float(body)
        account = _canonical_text(request["account_id"], name="account_id")
        environment = _canonical_environment(request["environment"])
        provider_environment = _canonical_text(
            request["provider_environment"], name="provider_environment"
        ).upper()
        capability = _canonical_text(
            request["capability_snapshot_id"],
            name="capability_snapshot_id",
        )
        entity_id = _canonical_text(request["entity_id"], name="entity_id")
        raw_capabilities = request["capability_snapshot_ids"]
        raw_instruments = request["instrument_versions"]
        if (
            not isinstance(raw_capabilities, (list, tuple))
            or len(raw_capabilities) != 1
            or not isinstance(raw_instruments, (list, tuple))
            or len(raw_instruments) != 1
        ):
            raise ProviderTransportScopeError(
                "Bybit capability and instrument bindings must contain exactly one identity"
            )
        if (
            _canonical_text(
                raw_capabilities[0], name="capability_snapshot_id"
            )
            != capability
        ):
            raise ProviderTransportScopeError(
                "Bybit capability snapshot binding is inconsistent"
            )
        instrument_version = _canonical_text(
            raw_instruments[0], name="instrument_version"
        )

        exact_body = json.dumps(
            dict(body),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        digest = _canonical_text(request["body_sha256"], name="body_sha256")
        actual_digest = "sha256:" + sha256(exact_body).hexdigest()
        if digest != actual_digest:
            raise ProviderTransportScopeError(
                "prepared Bybit request body digest mismatch"
            )
        return (
            endpoint,
            MappingProxyType(dict(body)),
            account,
            environment,
            provider_environment,
            capability,
            entity_id,
            instrument_version,
            actual_digest,
        )

    def _require_current_capability(
        self,
        *,
        entity_id: str,
        instrument_version: str,
    ) -> CapabilitySnapshot:
        point = self.clock_utc()
        if (
            not isinstance(point, datetime)
            or point.tzinfo is None
            or point.utcoffset() is None
        ):
            raise ProviderTransportScopeError(
                "clock_utc must return a timezone-aware datetime"
            )
        point = point.astimezone(timezone.utc)
        try:
            current = self.capability_registry.require_verified(
                provider_id="BYBIT",
                account_id=self.account_id,
                entity_id=entity_id,
                environment=self.policy.environment,
                instrument_version=instrument_version,
                at=point,
            )
        except Exception as error:
            raise ProviderTransportScopeError(
                "Bybit write current capability cannot be verified"
            ) from error
        if (
            not isinstance(current, CapabilitySnapshot)
            or current.snapshot_id != self.capability_snapshot_id
            or current.provider_id != "BYBIT"
            or current.account_id != self.account_id
            or current.entity_id != entity_id
            or current.environment != self.policy.environment
            or current.instrument_version != instrument_version
            or current.status != "VERIFIED"
            or not (current.observed_at <= point < current.expires_at)
            or "ORDER_WRITE" not in current.permission_scopes
        ):
            raise ProviderTransportScopeError(
                "Bybit write capability is no longer valid for exact prepared request"
            )
        return current

    def __call__(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard: Callable[[], None],
    ) -> ExactJsonTransportResponse:
        if not callable(final_guard):
            raise TypeError("final_guard must be callable")
        client_id = _canonical_text(
            client_order_id, name="client_order_id"
        )
        (
            endpoint,
            body,
            account,
            environment,
            provider_environment,
            capability,
            entity_id,
            instrument_version,
            _digest,
        ) = self._prepared_fields(request)
        if account != self.account_id:
            raise ProviderTransportScopeError(
                "prepared request account mismatch"
            )
        if environment != self.policy.environment:
            raise ProviderTransportScopeError(
                "prepared request environment mismatch"
            )
        if provider_environment != self.provider_environment:
            raise ProviderTransportScopeError(
                "prepared request provider environment mismatch"
            )
        if capability != self.capability_snapshot_id:
            raise ProviderTransportScopeError(
                "prepared request capability snapshot mismatch"
            )
        if body.get("orderLinkId") != client_id:
            raise ProviderTransportScopeError(
                "prepared request client order identity mismatch"
            )

        if self.quota_gate is not None:
            self.quota_gate(
                "BYBIT",
                self.account_id,
                self.policy.environment,
                "ORDER_WRITE",
            )

        self._require_current_capability(
            entity_id=entity_id,
            instrument_version=instrument_version,
        )

        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="BYBIT",
            environment=self.policy.environment,
            purpose="TRADE",
        )
        try:
            signed = BybitV5Signer.sign(
                policy=self.policy,
                endpoint=endpoint,
                body=body,
                credential_plaintext=credential_plaintext,
                timestamp_ms=self.clock_millis(),
                recv_window_ms=self.recv_window_ms,
            )
        finally:
            credential_plaintext = None

        self._require_current_capability(
            entity_id=entity_id,
            instrument_version=instrument_version,
        )
        final_guard()
        raw = self.wire_client.send(signed)
        if not isinstance(raw, bytes):
            raise ProviderTransportError(
                "Bybit order wire client must return exact response bytes"
            )
        return ExactJsonTransportResponse(raw)


class BybitV5AuthenticatedReadSigner:
    """Pure Bybit V5 authenticated-GET signer over one canonical query binding."""

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        query_binding: AuthenticatedReadQueryBinding,
        credential_plaintext: object,
        timestamp_ms: object,
        recv_window_ms: int = 5000,
    ) -> AuthenticatedReadHttpRequest:
        if not isinstance(query_binding, AuthenticatedReadQueryBinding):
            raise TypeError(
                "query_binding must be AuthenticatedReadQueryBinding"
            )
        if policy.provider_id != "BYBIT":
            raise ProviderTransportScopeError(
                "Bybit authenticated-read signer requires BYBIT policy"
            )
        if (
            query_binding.provider_id != "BYBIT"
            or query_binding.environment != policy.environment
        ):
            raise ProviderTransportScopeError(
                "authenticated-read binding provider/environment mismatch"
            )
        _bybit_authenticated_read_rule(query_binding)
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            raise ProviderTransportScopeError(
                "timestamp_ms must be a non-negative integer"
            )
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )

        query: dict[str, str] = {}
        for raw_key, raw_value in query_binding.query.items():
            key = _canonical_text(raw_key, name="query parameter")
            if not isinstance(raw_value, str) or raw_value != raw_value.strip():
                raise ProviderTransportScopeError(
                    "Bybit authenticated-read query values must be canonical strings"
                )
            if key in query:
                raise ProviderTransportScopeError(
                    "Bybit authenticated-read query keys must be unique"
                )
            query[key] = raw_value
        if not query:
            raise ProviderTransportScopeError(
                "Bybit authenticated-read query must not be empty"
            )

        credential = BybitV5Credential.parse(credential_plaintext)
        exact_query = urlencode(sorted(query.items()))
        signing_material = (
            str(timestamp_ms)
            + credential.api_key
            + str(recv_window_ms)
            + exact_query
        ).encode("utf-8")
        signature = hmac.new(
            credential.api_secret.encode("utf-8"),
            signing_material,
            sha256,
        ).hexdigest()
        return AuthenticatedReadHttpRequest(
            url=policy.absolute_url(query_binding.endpoint) + "?" + exact_query,
            headers=MappingProxyType(
                {
                    "Accept": "application/json",
                    "X-BAPI-API-KEY": credential.api_key,
                    "X-BAPI-TIMESTAMP": str(timestamp_ms),
                    "X-BAPI-RECV-WINDOW": str(recv_window_ms),
                    "X-BAPI-SIGN": signature,
                }
            ),
            timeout_seconds=policy.timeout_seconds,
        )


class BybitV5AuthenticatedReadTransport:
    """One-shot scoped Bybit authenticated read for reconciliation surfaces."""

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        provider_environment: str,
        account_id: str,
        capability_snapshot_id: str,
        capability_registry: CapabilityRegistry,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        provider_env = _canonical_text(
            provider_environment, name="provider_environment"
        ).upper()
        canonical_policy = BYBIT_V5_ENDPOINT_POLICIES.get(provider_env)
        if canonical_policy is None or policy != canonical_policy:
            raise ProviderTransportScopeError(
                "Bybit read policy does not match exact provider environment"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "BYBIT"
            or credential_handle.environment != policy.environment
            or credential_handle.purpose != "READ"
        ):
            raise ProviderTransportScopeError(
                "READ credential handle provider/environment/purpose mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        if not isinstance(capability_registry, CapabilityRegistry):
            raise TypeError("capability_registry must be CapabilityRegistry")
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if not callable(clock_millis) or not callable(clock_utc):
            raise TypeError("Bybit read clocks must be callable")
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )

        self.policy = policy
        self.provider_environment = provider_env
        self.account_id = account
        self.capability_snapshot_id = _canonical_text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        self.capability_registry = capability_registry
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _canonical_text(
            session_token, name="session_token"
        )
        self.origin = _canonical_text(origin, name="origin")
        self.execution_identity = _canonical_text(
            execution_identity, name="execution_identity"
        )
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()
        self.recv_window_ms = recv_window_ms

    def _require_current_capability(
        self,
        query_binding: AuthenticatedReadQueryBinding,
        rule: AuthenticatedReadEndpointRule,
    ) -> CapabilitySnapshot:
        point = self.clock_utc()
        if (
            not isinstance(point, datetime)
            or point.tzinfo is None
            or point.utcoffset() is None
        ):
            raise ProviderTransportScopeError(
                "clock_utc must return a timezone-aware datetime"
            )
        point = point.astimezone(timezone.utc)
        try:
            current = self.capability_registry.require_verified(
                provider_id="BYBIT",
                account_id=self.account_id,
                entity_id=query_binding.entity_id,
                environment=self.policy.environment,
                instrument_version=query_binding.instrument_version,
                at=point,
            )
        except Exception as error:
            raise ProviderTransportScopeError(
                "Bybit authenticated-read current capability cannot be verified"
            ) from error
        if (
            not isinstance(current, CapabilitySnapshot)
            or current.snapshot_id != self.capability_snapshot_id
            or current.provider_id != "BYBIT"
            or current.account_id != self.account_id
            or current.entity_id != query_binding.entity_id
            or current.environment != self.policy.environment
            or current.instrument_version != query_binding.instrument_version
            or current.status != "VERIFIED"
            or not (current.observed_at <= point < current.expires_at)
            or query_binding.permission_scope not in current.permission_scopes
            or rule.data_entitlement not in current.data_entitlements
        ):
            raise ProviderTransportScopeError(
                "Bybit authenticated-read capability is no longer valid for exact query binding"
            )
        return current

    def __call__(
        self,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> ProviderResponseObservation:
        if not isinstance(query_binding, AuthenticatedReadQueryBinding):
            raise TypeError(
                "query_binding must be AuthenticatedReadQueryBinding"
            )
        if (
            query_binding.provider_id != "BYBIT"
            or query_binding.account_id != self.account_id
            or query_binding.environment != self.policy.environment
            or query_binding.capability_snapshot_id != self.capability_snapshot_id
        ):
            raise ProviderTransportScopeError(
                "Bybit authenticated-read query scope mismatch"
            )
        rule = _bybit_authenticated_read_rule(query_binding)

        if self.quota_gate is not None:
            self.quota_gate(
                "BYBIT",
                self.account_id,
                self.policy.environment,
                "AUTHENTICATED_READ",
            )

        self._require_current_capability(query_binding, rule)

        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="BYBIT",
            environment=self.policy.environment,
            purpose="READ",
        )
        try:
            signed = BybitV5AuthenticatedReadSigner.sign(
                policy=self.policy,
                query_binding=query_binding,
                credential_plaintext=credential_plaintext,
                timestamp_ms=self.clock_millis(),
                recv_window_ms=self.recv_window_ms,
            )
        finally:
            credential_plaintext = None

        self._require_current_capability(query_binding, rule)
        wire_response = self.wire_client.send(signed)
        if not isinstance(wire_response, AuthenticatedReadWireResponse):
            raise ProviderTransportError(
                "Bybit authenticated-read wire client must preserve HTTP status"
            )
        if wire_response.http_status not in rule.success_statuses:
            raise ProviderTransportError(
                "Bybit authenticated read returned unexpected HTTP status "
                + str(wire_response.http_status)
            )
        return observe_authenticated_json_response(
            query_binding=query_binding,
            http_status=wire_response.http_status,
            response_bytes=wire_response.body,
            observed_at=self.clock_utc(),
        )


@dataclass(frozen=True)
class BinanceSpotCredential:
    api_key: str
    api_secret: str

    @classmethod
    def parse(cls, plaintext: object) -> "BinanceSpotCredential":
        if not isinstance(plaintext, str) or not plaintext:
            raise ProviderTransportScopeError(
                "Binance credential material is unavailable"
            )
        try:
            value = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ProviderTransportScopeError(
                "Binance credential material has invalid format"
            ) from error
        if not isinstance(value, dict) or set(value) != {
            "api_key",
            "api_secret",
        }:
            raise ProviderTransportScopeError(
                "Binance credential material must contain exact api_key/api_secret fields"
            )
        api_key = _text(value["api_key"], name="api_key")
        api_secret = _text(value["api_secret"], name="api_secret")
        return cls(api_key=api_key, api_secret=api_secret)


class BinanceSpotSigner:
    """Pure deterministic Binance Spot order signer."""

    PLACE_ORDER_ENDPOINT = "/api/v3/order"

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        endpoint: object,
        body: object,
        credential_plaintext: object,
        timestamp_ms: object,
        recv_window_ms: int = 5000,
    ) -> SignedHttpRequest:
        if policy.provider_id != "BINANCE":
            raise ProviderTransportScopeError(
                "Binance signer requires a BINANCE endpoint policy"
            )
        path = _text(endpoint, name="endpoint")
        if path != BinanceSpotSigner.PLACE_ORDER_ENDPOINT:
            raise ProviderTransportScopeError(
                "Binance Spot trade signer received an unsupported endpoint"
            )
        if not isinstance(body, Mapping) or not body:
            raise ProviderTransportScopeError(
                "Binance order body must be a non-empty mapping"
            )
        canonical: dict[str, str] = {}
        for raw_key, raw_value in body.items():
            key = _canonical_text(raw_key, name="order parameter")
            if not isinstance(raw_value, str) or raw_value != raw_value.strip():
                raise ProviderTransportScopeError(
                    "Binance order parameters must be canonical strings"
                )
            if key in {"timestamp", "recvWindow", "signature"}:
                raise ProviderTransportScopeError(
                    "adapter body must not pre-populate transport-owned signing fields"
                )
            canonical[key] = raw_value

        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            raise ProviderTransportScopeError(
                "timestamp_ms must be a non-negative integer"
            )
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )
        credential = BinanceSpotCredential.parse(credential_plaintext)

        canonical["recvWindow"] = str(recv_window_ms)
        canonical["timestamp"] = str(timestamp_ms)
        unsigned = urlencode(sorted(canonical.items())).encode("ascii")
        signature = hmac.new(
            credential.api_secret.encode("utf-8"),
            unsigned,
            sha256,
        ).hexdigest()
        exact_body = unsigned + b"&signature=" + signature.encode("ascii")
        return SignedHttpRequest(
            method="POST",
            url=policy.absolute_url(path),
            headers=MappingProxyType(
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-MBX-APIKEY": credential.api_key,
                }
            ),
            body=exact_body,
            timeout_seconds=policy.timeout_seconds,
        )


class BinanceSpotHttpTransport:
    """GuardedDispatcher-compatible Binance Spot PAPER/LIVE transport.

    The object is intentionally account/environment/capability scoped at
    construction. It performs no retry. Quota waiting, secret resolution and
    signing all happen before the final guard. The already-signed immutable bytes
    are then sent once immediately after the guard.
    """

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        account_id: str,
        capability_snapshot_id: str,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        clock_millis: ClockMillis,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "BINANCE":
            raise ProviderTransportScopeError(
                "Binance Spot transport requires BINANCE policy"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != policy.provider_id
            or credential_handle.environment != policy.environment
            or credential_handle.purpose != "TRADE"
        ):
            raise ProviderTransportScopeError(
                "credential handle provider/environment/purpose mismatch"
            )
        account = _text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        capability = _text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if not callable(clock_millis):
            raise TypeError("clock_millis must be callable")
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )

        self.policy = policy
        self.account_id = account
        self.capability_snapshot_id = _canonical_text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _text(session_token, name="session_token")
        self.origin = _text(origin, name="origin")
        self.execution_identity = _text(
            execution_identity, name="execution_identity"
        )
        self.clock_millis = clock_millis
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()
        self.recv_window_ms = recv_window_ms

    @staticmethod
    def _prepared_fields(
        request: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, str], str]:
        if not isinstance(request, Mapping):
            raise ProviderTransportScopeError(
                "prepared provider request must be a mapping"
            )
        if set(request) != {
            "endpoint",
            "body",
            "capability_snapshot_id",
        }:
            raise ProviderTransportScopeError(
                "prepared Binance request fields are not canonical"
            )
        endpoint = _canonical_text(request["endpoint"], name="endpoint")
        body = request["body"]
        capability = _canonical_text(
            request["capability_snapshot_id"],
            name="capability_snapshot_id",
        )
        if not isinstance(body, Mapping):
            raise ProviderTransportScopeError(
                "prepared Binance request body must be a mapping"
            )
        normalized: dict[str, str] = {}
        for raw_key, raw_value in body.items():
            key = _canonical_text(raw_key, name="order parameter")
            if not isinstance(raw_value, str) or raw_value != raw_value.strip():
                raise ProviderTransportScopeError(
                    "prepared Binance body values must be canonical strings"
                )
            normalized[key] = raw_value
        return endpoint, MappingProxyType(normalized), capability

    def __call__(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard: Callable[[], None],
    ) -> ExactJsonTransportResponse:
        if not callable(final_guard):
            raise TypeError("final_guard must be callable")
        client_id = _canonical_text(client_order_id, name="client_order_id")
        endpoint, body, capability = self._prepared_fields(request)
        if capability != self.capability_snapshot_id:
            raise ProviderTransportScopeError(
                "prepared request capability snapshot mismatch"
            )
        if body.get("newClientOrderId") != client_id:
            raise ProviderTransportScopeError(
                "prepared request client order identity mismatch"
            )

        # Quota/recovery-budget admission occurs before credential resolution and
        # before the irreversible send barrier. The callback owns no retries.
        if self.quota_gate is not None:
            self.quota_gate(
                self.policy.provider_id,
                self.account_id,
                self.policy.environment,
                "ORDER_WRITE",
            )

        # WP-46 owns secret storage and role/session authorization. Plaintext is
        # requested only now, used once for pure signing, and never attached to
        # the durable dispatch request or returned response.
        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            purpose="TRADE",
        )
        try:
            timestamp_ms = self.clock_millis()
            signed = BinanceSpotSigner.sign(
                policy=self.policy,
                endpoint=endpoint,
                body=body,
                credential_plaintext=credential_plaintext,
                timestamp_ms=timestamp_ms,
                recv_window_ms=self.recv_window_ms,
            )
        finally:
            # Python strings cannot be securely zeroized. Drop the only local
            # transport reference immediately; the canonical vault remains the
            # sole persistence authority.
            credential_plaintext = None

        # No waits, signing, host selection or mutation may occur after this
        # point. A wire exception after the guard is intentionally propagated so
        # GuardedDispatcher records UNKNOWN and requires reconciliation.
        final_guard()
        wire_response = self.wire_client.send(signed)
        return _exact_trading_response(wire_response)


class BinanceSpotAuthenticatedReadSigner:
    """Pure Binance authenticated-GET signer over a canonical read binding."""

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        query_binding: AuthenticatedReadQueryBinding,
        credential_plaintext: object,
        timestamp_ms: object,
        recv_window_ms: int = 5000,
    ) -> AuthenticatedReadHttpRequest:
        if not isinstance(query_binding, AuthenticatedReadQueryBinding):
            raise TypeError(
                "query_binding must be AuthenticatedReadQueryBinding"
            )
        if policy.provider_id != "BINANCE":
            raise ProviderTransportScopeError(
                "Binance authenticated-read signer requires BINANCE policy"
            )
        if (
            query_binding.provider_id != policy.provider_id
            or query_binding.environment != policy.environment
        ):
            raise ProviderTransportScopeError(
                "authenticated-read binding provider/environment mismatch"
            )
        _binance_authenticated_read_rule(query_binding)
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            raise ProviderTransportScopeError(
                "timestamp_ms must be a non-negative integer"
            )
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )
        canonical: dict[str, str] = {}
        for raw_key, raw_value in query_binding.query.items():
            key = _canonical_text(raw_key, name="query parameter")
            if not isinstance(raw_value, str) or raw_value != raw_value.strip():
                raise ProviderTransportScopeError(
                    "authenticated-read query values must be canonical strings"
                )
            if key in {"timestamp", "recvWindow", "signature"}:
                raise ProviderTransportScopeError(
                    "query binding must not pre-populate transport signing fields"
                )
            canonical[key] = raw_value

        credential = BinanceSpotCredential.parse(credential_plaintext)
        canonical["recvWindow"] = str(recv_window_ms)
        canonical["timestamp"] = str(timestamp_ms)
        unsigned = urlencode(sorted(canonical.items()))
        signature = hmac.new(
            credential.api_secret.encode("utf-8"),
            unsigned.encode("ascii"),
            sha256,
        ).hexdigest()
        exact_query = unsigned + "&signature=" + signature
        return AuthenticatedReadHttpRequest(
            url=policy.absolute_url(query_binding.endpoint) + "?" + exact_query,
            headers=MappingProxyType(
                {
                    "Accept": "application/json",
                    "X-MBX-APIKEY": credential.api_key,
                }
            ),
            timeout_seconds=policy.timeout_seconds,
        )


class BinanceSpotAuthenticatedReadTransport:
    """One-shot credential-scoped Binance authenticated read.

    The transport reuses the provider-core authenticated query/response
    identities. It owns no retry, cache, reconciliation, or financial authority.
    One call emits at most one GET and returns one exact-byte-bound observation.
    """

    def __init__(
        self,
        *,
        policy: ProviderEndpointPolicy,
        account_id: str,
        capability_snapshot_id: str,
        capability_registry: CapabilityRegistry,
        secret_resolver: ProviderSecretResolver,
        credential_handle: PersistentCredentialHandle,
        session_token: str,
        origin: str,
        execution_identity: str,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
        recv_window_ms: int = 5000,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "BINANCE":
            raise ProviderTransportScopeError(
                "Binance authenticated-read transport requires BINANCE policy"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != policy.provider_id
            or credential_handle.environment != policy.environment
            or credential_handle.purpose != "READ"
        ):
            raise ProviderTransportScopeError(
                "READ credential handle provider/environment/purpose mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "credential handle account mismatch"
            )
        capability = _canonical_text(
            capability_snapshot_id,
            name="capability_snapshot_id",
        )
        if not isinstance(capability_registry, CapabilityRegistry):
            raise TypeError("capability_registry must be CapabilityRegistry")
        if not hasattr(secret_resolver, "resolve_for_execution"):
            raise TypeError(
                "secret_resolver must implement resolve_for_execution"
            )
        if not callable(clock_millis):
            raise TypeError("clock_millis must be callable")
        if not callable(clock_utc):
            raise TypeError("clock_utc must be callable")
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")
        if (
            isinstance(recv_window_ms, bool)
            or not isinstance(recv_window_ms, int)
            or recv_window_ms < 1
            or recv_window_ms > 60000
        ):
            raise ProviderTransportScopeError(
                "recv_window_ms must be an integer from 1 through 60000"
            )

        self.policy = policy
        self.account_id = account
        self.capability_snapshot_id = capability
        self.capability_registry = capability_registry
        self.secret_resolver = secret_resolver
        self.credential_handle = credential_handle
        self.session_token = _canonical_text(
            session_token,
            name="session_token",
        )
        self.origin = _canonical_text(origin, name="origin")
        self.execution_identity = _canonical_text(
            execution_identity,
            name="execution_identity",
        )
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()
        self.recv_window_ms = recv_window_ms

    def _require_current_capability(
        self,
        query_binding: AuthenticatedReadQueryBinding,
        rule: AuthenticatedReadEndpointRule,
    ) -> CapabilitySnapshot:
        point = self.clock_utc()
        if (
            not isinstance(point, datetime)
            or point.tzinfo is None
            or point.utcoffset() is None
        ):
            raise ProviderTransportScopeError(
                "clock_utc must return a timezone-aware datetime"
            )
        point = point.astimezone(timezone.utc)
        try:
            current = self.capability_registry.require_verified(
                provider_id=self.policy.provider_id,
                account_id=self.account_id,
                entity_id=query_binding.entity_id,
                environment=self.policy.environment,
                instrument_version=query_binding.instrument_version,
                at=point,
            )
        except Exception as error:
            raise ProviderTransportScopeError(
                "authenticated-read current capability cannot be verified"
            ) from error
        if not isinstance(current, CapabilitySnapshot):
            raise ProviderTransportScopeError(
                "capability registry must return CapabilitySnapshot"
            )
        if (
            current.snapshot_id != self.capability_snapshot_id
            or current.provider_id != self.policy.provider_id
            or current.account_id != self.account_id
            or current.entity_id != query_binding.entity_id
            or current.environment != self.policy.environment
            or current.instrument_version != query_binding.instrument_version
            or current.status != "VERIFIED"
            or not (current.observed_at <= point < current.expires_at)
            or query_binding.permission_scope not in current.permission_scopes
            or rule.data_entitlement not in current.data_entitlements
        ):
            raise ProviderTransportScopeError(
                "authenticated-read capability is no longer valid for exact query binding"
            )
        return current

    def __call__(
        self,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> ProviderResponseObservation:
        if not isinstance(query_binding, AuthenticatedReadQueryBinding):
            raise TypeError(
                "query_binding must be AuthenticatedReadQueryBinding"
            )
        if (
            query_binding.provider_id != self.policy.provider_id
            or query_binding.account_id != self.account_id
            or query_binding.environment != self.policy.environment
            or query_binding.capability_snapshot_id != self.capability_snapshot_id
        ):
            raise ProviderTransportScopeError(
                "authenticated-read query scope mismatch"
            )
        rule = _binance_authenticated_read_rule(query_binding)

        if self.quota_gate is not None:
            self.quota_gate(
                self.policy.provider_id,
                self.account_id,
                self.policy.environment,
                "AUTHENTICATED_READ",
            )

        # Revalidate after any quota wait and before touching READ credentials.
        self._require_current_capability(query_binding, rule)

        credential_plaintext = self.secret_resolver.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            purpose="READ",
        )
        try:
            signed = BinanceSpotAuthenticatedReadSigner.sign(
                policy=self.policy,
                query_binding=query_binding,
                credential_plaintext=credential_plaintext,
                timestamp_ms=self.clock_millis(),
                recv_window_ms=self.recv_window_ms,
            )
        finally:
            credential_plaintext = None

        # Secret access/signing may take time. Re-resolve authority at the
        # irreversible boundary so revocation/expiry cannot race the wire send.
        self._require_current_capability(query_binding, rule)
        wire_response = self.wire_client.send(signed)
        if not isinstance(wire_response, AuthenticatedReadWireResponse):
            raise ProviderTransportError(
                "authenticated-read wire client must preserve HTTP status"
            )
        if wire_response.http_status not in rule.success_statuses:
            raise ProviderTransportError(
                "authenticated provider read returned unexpected HTTP status "
                + str(wire_response.http_status)
                + "; allowed="
                + ",".join(str(status) for status in sorted(rule.success_statuses))
            )
        observed_at = self.clock_utc()
        return observe_authenticated_json_response(
            query_binding=query_binding,
            http_status=wire_response.http_status,
            response_bytes=wire_response.body,
            observed_at=observed_at,
        )
