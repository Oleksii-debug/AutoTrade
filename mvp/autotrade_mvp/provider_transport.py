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

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256, sha512
import base64
import binascii
import hmac
import json
import os
import re
from threading import Lock
from types import MappingProxyType
from typing import Any, Callable, ContextManager, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from weakref import ref as weakref_ref
from urllib.request import (
    AbstractHTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    Request,
    ProxyHandler,
    build_opener,
)

from .capabilities import CapabilityRegistry, CapabilitySnapshot
from .bybit_v5 import _AMBIGUOUS_RESPONSE_CODES as _BYBIT_AMBIGUOUS_RESPONSE_CODES
from .dispatch import ExactJsonTransportResponse
from .exact_decimal import ExactDecimalError, parse_canonical_decimal_text
from .persistence import JournalStore, payload_digest
from .kraken_futures import validate_futures_client_order_id
from .kraken_spot import (
    spot_submission_requires_reconciliation,
    validate_spot_client_order_id,
)
from .whitebit import (
    classify_whitebit_http_retry,
    sign_private_request,
    validate_client_order_id,
)
from .provider_core import (
    AuthenticatedReadQueryBinding,
    _require_authenticated_read_query_binding_authority,
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
)
from .windows_secrets import PersistentCredentialHandle
from .provider_response_limits import (
    DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    HARD_MAX_PROVIDER_RESPONSE_BYTES,
    require_provider_response_bytes,
)


class ProviderTransportError(RuntimeError):
    """Base error for the shared provider I/O seam."""


class ProviderTransportScopeError(ValueError):
    """Raised before I/O when provider/account/environment scope is invalid."""


class ProviderSecretResolver(Protocol):
    def lease_for_execution(
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
        provider_environment: str | None = None,
    ) -> ContextManager[str]: ...


class ProviderWireClient(Protocol):
    def send(
        self,
        request: "SignedHttpRequest | AuthenticatedReadHttpRequest",
    ) -> "bytes | TradingWireResponse | AuthenticatedReadWireResponse": ...


QuotaGate = Callable[[str, str, str, str], None]
ClockMillis = Callable[[], int]
ClockUtc = Callable[[], datetime]
_UINT64_MAX = (1 << 64) - 1
_NONCE_SEND_LOCKS_GUARD = Lock()
_NONCE_SEND_LOCKS: dict[str, object] = {}


def _serialized_nonce_send_lock(aggregate_id: str):
    with _NONCE_SEND_LOCKS_GUARD:
        lock = _NONCE_SEND_LOCKS.get(aggregate_id)
        if lock is None:
            lock = Lock()
            _NONCE_SEND_LOCKS[aggregate_id] = lock
        return lock


@contextmanager
def _exclusive_nonce_send_lock(thread_lock, lock_path):
    """Fence one credential's nonce allocation through wire send across processes."""

    with thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


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


KRAKEN_FUTURES_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "LIVE": ProviderEndpointPolicy(
                provider_id="KRAKEN",
                environment="LIVE",
                base_url="https://futures.kraken.com",
                allowed_hosts=frozenset({"futures.kraken.com"}),
            ),
            "DEMO": ProviderEndpointPolicy(
                provider_id="KRAKEN",
                environment="PAPER",
                base_url="https://demo-futures.kraken.com",
                allowed_hosts=frozenset({"demo-futures.kraken.com"}),
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


# OAuth2 direct Web API policy only. The local Client Portal Gateway has a
# different host/TLS deployment model and requires separate qualification.
IBKR_WEB_ENDPOINT_POLICIES: Mapping[str, ProviderEndpointPolicy] = (
    MappingProxyType(
        {
            "PAPER": ProviderEndpointPolicy(
                provider_id="IBKR",
                environment="PAPER",
                base_url="https://api.ibkr.com",
                allowed_hosts=frozenset({"api.ibkr.com"}),
            ),
            "LIVE": ProviderEndpointPolicy(
                provider_id="IBKR",
                environment="LIVE",
                base_url="https://api.ibkr.com",
                allowed_hosts=frozenset({"api.ibkr.com"}),
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


KRAKEN_SPOT_AUTHENTICATED_READ_ENDPOINTS: Mapping[
    str, AuthenticatedReadEndpointRule
] = MappingProxyType(
    {
        "/0/private/OpenOrders": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="ORDER.READ",
            data_entitlement="ORDERS",
            success_statuses=frozenset({200}),
        ),
        "/0/private/ClosedOrders": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="ORDER.READ",
            data_entitlement="ORDERS",
            success_statuses=frozenset({200}),
        ),
        "/0/private/QueryOrders": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="ORDER.READ",
            data_entitlement="ORDERS",
            success_statuses=frozenset({200}),
        ),
        "/0/private/TradesHistory": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="TRADE.READ",
            data_entitlement="TRADES",
            success_statuses=frozenset({200}),
        ),
        "/0/private/Ledgers": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="ACCOUNT.READ",
            data_entitlement="ACTIVITIES",
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
        "/v5/asset/delivery-record": AuthenticatedReadEndpointRule(
            surface=Surface.ACTIVITIES,
            permission_scope="ACCOUNT.READ",
            data_entitlement="ACTIVITIES",
            success_statuses=frozenset({200}),
        ),
    }
)


IBKR_WEB_AUTHENTICATED_READ_ENDPOINTS: Mapping[
    str, AuthenticatedReadEndpointRule
] = MappingProxyType(
    {
        "/iserver/auth/status": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="SESSION",
            success_statuses=frozenset({200}),
        ),
        "/iserver/accounts": AuthenticatedReadEndpointRule(
            surface=Surface.AUTHENTICATED_READ,
            permission_scope="ORDER.READ",
            data_entitlement="ACCOUNT",
            success_statuses=frozenset({200}),
        ),
    }
)


IBKR_WEB_AUTHENTICATED_READ_METHODS: Mapping[str, str] = MappingProxyType(
    {
        "/iserver/auth/status": "POST",
        "/iserver/accounts": "GET",
    }
)


_BYBIT_OPTION_DELIVERY_ENDPOINT = "/v5/asset/delivery-record"
_BYBIT_OPTION_DELIVERY_QUERY_FIELDS = frozenset(
    {"category", "symbol", "startTime", "endTime", "expDate", "limit", "cursor"}
)
_BYBIT_OPTION_DELIVERY_CURSOR_RE = re.compile(
    r"^(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})+$"
)
_BYBIT_OPTION_DELIVERY_MONTH_NUMBER = MappingProxyType(
    {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }
)
_BYBIT_OPTION_DELIVERY_MAX_RANGE_MS = 30 * 24 * 60 * 60 * 1000


def _bybit_delivery_query_integer(
    value: object,
    *,
    name: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        type(value) is not str
        or not value
        or len(value) > 20
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ProviderTransportScopeError(
            f"Bybit option delivery {name} must be canonical integer text"
        )
    parsed = int(value, 10)
    if str(parsed) != value or parsed < minimum:
        raise ProviderTransportScopeError(
            f"Bybit option delivery {name} must be canonical integer text"
        )
    if maximum is not None and parsed > maximum:
        raise ProviderTransportScopeError(
            f"Bybit option delivery {name} is outside the documented range"
        )
    return parsed


def _validate_bybit_option_delivery_query(
    binding: AuthenticatedReadQueryBinding,
) -> None:
    if binding.endpoint != _BYBIT_OPTION_DELIVERY_ENDPOINT:
        return
    query = binding.query
    unsupported = set(query) - _BYBIT_OPTION_DELIVERY_QUERY_FIELDS
    if unsupported:
        raise ProviderTransportScopeError(
            "Bybit option delivery query contains unsupported fields: "
            + ",".join(sorted(unsupported))
        )
    category = query.get("category")
    if type(category) is not str or category != "option":
        raise ProviderTransportScopeError(
            "Bybit option delivery query requires category=option"
        )

    symbol = query.get("symbol")
    if symbol is not None:
        if (
            type(symbol) is not str
            or not symbol
            or len(symbol) > 160
            or re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", symbol) is None
        ):
            raise ProviderTransportScopeError(
                "Bybit option delivery symbol must be uppercase canonical provider text"
            )

    start_ms = None
    end_ms = None
    if "startTime" in query:
        start_ms = _bybit_delivery_query_integer(
            query["startTime"],
            name="startTime",
        )
    if "endTime" in query:
        end_ms = _bybit_delivery_query_integer(
            query["endTime"],
            name="endTime",
        )
    if start_ms is not None and end_ms is not None:
        if end_ms < start_ms:
            raise ProviderTransportScopeError(
                "Bybit option delivery endTime cannot precede startTime"
            )
        if end_ms - start_ms > _BYBIT_OPTION_DELIVERY_MAX_RANGE_MS:
            raise ProviderTransportScopeError(
                "Bybit option delivery time range exceeds 30 days"
            )

    exp_date = query.get("expDate")
    if exp_date is not None:
        if type(exp_date) is not str or re.fullmatch(r"[0-3][0-9][A-Z]{3}[0-9]{2}", exp_date) is None:
            raise ProviderTransportScopeError(
                "Bybit option delivery expDate must use DDMMMYY"
            )
        day = int(exp_date[:2], 10)
        month = exp_date[2:5]
        year = 2000 + int(exp_date[5:7], 10)
        month_number = _BYBIT_OPTION_DELIVERY_MONTH_NUMBER.get(month)
        if day < 1 or month_number is None:
            raise ProviderTransportScopeError(
                "Bybit option delivery expDate must use DDMMMYY"
            )
        try:
            datetime(year, month_number, day, tzinfo=timezone.utc)
        except ValueError as exc:
            raise ProviderTransportScopeError(
                "Bybit option delivery expDate must use DDMMMYY"
            ) from exc

    if "limit" in query:
        _bybit_delivery_query_integer(
            query["limit"],
            name="limit",
            minimum=1,
            maximum=50,
        )

    cursor = query.get("cursor")
    if cursor is not None:
        if (
            type(cursor) is not str
            or _BYBIT_OPTION_DELIVERY_CURSOR_RE.fullmatch(cursor) is None
        ):
            raise ProviderTransportScopeError(
                "Bybit option delivery cursor must be canonical opaque percent-encoded text"
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
    _validate_bybit_option_delivery_query(binding)
    return rule


def _ibkr_authenticated_read_rule(
    binding: AuthenticatedReadQueryBinding,
) -> AuthenticatedReadEndpointRule:
    if type(binding) is not AuthenticatedReadQueryBinding:
        raise TypeError(
            "IBKR authenticated-read binding must be exact AuthenticatedReadQueryBinding"
        )
    _require_authenticated_read_query_binding_authority(binding)
    if binding.provider_id != "IBKR":
        raise ProviderTransportScopeError(
            "IBKR authenticated-read binding provider mismatch"
        )
    rule = IBKR_WEB_AUTHENTICATED_READ_ENDPOINTS.get(binding.endpoint)
    if rule is None:
        raise ProviderTransportScopeError(
            "IBKR authenticated-read endpoint is not explicitly allowed"
        )
    if binding.surface != rule.surface:
        raise ProviderTransportScopeError(
            "authenticated-read endpoint surface does not match IBKR policy"
        )
    if binding.permission_scope != rule.permission_scope:
        raise ProviderTransportScopeError(
            "authenticated-read permission scope does not match IBKR endpoint policy"
        )
    if binding.query:
        raise ProviderTransportScopeError(
            "IBKR status/accounts authenticated reads require an empty query"
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


_KRAKEN_SPOT_AUTHENTICATED_READ_QUERY_FIELDS: Mapping[str, frozenset[str]] = (
    MappingProxyType(
        {
            "/0/private/OpenOrders": frozenset(
                {"trades", "userref", "cl_ord_id", "rebase_multiplier"}
            ),
            "/0/private/ClosedOrders": frozenset(
                {
                    "trades",
                    "userref",
                    "cl_ord_id",
                    "start",
                    "end",
                    "ofs",
                    "closetime",
                    "consolidate_taker",
                    "without_count",
                    "rebase_multiplier",
                }
            ),
            "/0/private/QueryOrders": frozenset(
                {
                    "txid",
                    "trades",
                    "userref",
                    "consolidate_taker",
                    "rebase_multiplier",
                }
            ),
            "/0/private/TradesHistory": frozenset(
                {
                    "type",
                    "trades",
                    "start",
                    "end",
                    "ofs",
                    "without_count",
                    "consolidate_taker",
                    "ledgers",
                    "rebase_multiplier",
                    "aclass",
                    "pair",
                    "limit",
                }
            ),
            "/0/private/Ledgers": frozenset(
                {
                    "asset",
                    "aclass",
                    "type",
                    "start",
                    "end",
                    "ofs",
                    "without_count",
                    "rebase_multiplier",
                }
            ),
        }
    )
)

_KRAKEN_SPOT_BOOLEAN_QUERY_FIELDS = frozenset(
    {"trades", "consolidate_taker", "without_count", "ledgers"}
)


def _kraken_spot_canonical_integer(
    value: str,
    *,
    name: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as error:
        raise ProviderTransportScopeError(
            f"Kraken Spot {name} must be canonical integer text"
        ) from error
    if str(parsed) != value or parsed < minimum:
        raise ProviderTransportScopeError(
            f"Kraken Spot {name} must be canonical integer text"
        )
    if maximum is not None and parsed > maximum:
        raise ProviderTransportScopeError(
            f"Kraken Spot {name} is outside the documented range"
        )
    return parsed


def _kraken_spot_history_boundary(value: str, *, name: str) -> str:
    """Validate Kraken history start/end as Unix time or opaque tx/ledger id."""

    if value.isascii() and value.isdigit():
        _kraken_spot_canonical_integer(
            value,
            name=name,
            minimum=0,
        )
        return value
    parts = value.split("-")
    if (
        len(parts) < 2
        or any(
            not part
            or not part.isascii()
            or not part.isalnum()
            for part in parts
        )
    ):
        raise ProviderTransportScopeError(
            f"Kraken Spot {name} must be canonical Unix time or provider id"
        )
    return value


def _validate_kraken_spot_authenticated_read_query(
    binding: AuthenticatedReadQueryBinding,
) -> None:
    allowed = _KRAKEN_SPOT_AUTHENTICATED_READ_QUERY_FIELDS[binding.endpoint]
    unsupported = set(binding.query) - allowed
    if unsupported:
        raise ProviderTransportScopeError(
            "Kraken Spot authenticated-read query contains unsupported fields: "
            + ",".join(sorted(unsupported))
        )

    for field in _KRAKEN_SPOT_BOOLEAN_QUERY_FIELDS & set(binding.query):
        if binding.query[field] not in {"true", "false"}:
            raise ProviderTransportScopeError(
                f"Kraken Spot {field} must be canonical boolean text"
            )

    if "ofs" in binding.query:
        _kraken_spot_canonical_integer(
            binding.query["ofs"],
            name="ofs",
            minimum=0,
        )
    if "limit" in binding.query:
        _kraken_spot_canonical_integer(
            binding.query["limit"],
            name="limit",
            minimum=1,
            maximum=100,
        )
    if "userref" in binding.query:
        _kraken_spot_canonical_integer(
            binding.query["userref"],
            name="userref",
            minimum=-(1 << 31),
            maximum=(1 << 31) - 1,
        )
    for field in ("start", "end"):
        if field in binding.query:
            _kraken_spot_history_boundary(
                binding.query[field],
                name=field,
            )
    if "cl_ord_id" in binding.query:
        try:
            validate_spot_client_order_id(binding.query["cl_ord_id"])
        except ValueError as error:
            raise ProviderTransportScopeError(
                "Kraken Spot cl_ord_id filter is invalid"
            ) from error
    if binding.endpoint == "/0/private/QueryOrders":
        raw_txids = binding.query.get("txid")
        if raw_txids is None:
            raise ProviderTransportScopeError(
                "Kraken Spot QueryOrders requires txid"
            )
        txids = tuple(part.strip() for part in raw_txids.split(","))
        if (
            raw_txids != ",".join(txids)
            or not txids
            or len(txids) > 50
            or any(not value for value in txids)
            or len(set(txids)) != len(txids)
        ):
            raise ProviderTransportScopeError(
                "Kraken Spot QueryOrders txid must contain 1..50 unique order ids"
            )
        for value in txids:
            canonical = _canonical_text(
                value,
                name="Kraken Spot QueryOrders txid",
            )
            if any(character.isspace() for character in canonical):
                raise ProviderTransportScopeError(
                    "Kraken Spot QueryOrders txid contains whitespace"
                )

    enum_fields = {
        "rebase_multiplier": frozenset({"rebased", "base"}),
    }
    if binding.endpoint == "/0/private/ClosedOrders":
        enum_fields["closetime"] = frozenset({"open", "close", "both"})
    elif binding.endpoint == "/0/private/TradesHistory":
        enum_fields["type"] = frozenset(
            {
                "all",
                "any position",
                "closed position",
                "closing position",
                "no position",
            }
        )
        enum_fields["aclass"] = frozenset(
            {
                "forex",
                "equity_pair",
                "futures_contract",
                "synthetic_pair",
                "external_pair",
            }
        )
    elif binding.endpoint == "/0/private/Ledgers":
        enum_fields["aclass"] = frozenset({"currency"})
        enum_fields["type"] = frozenset(
            {
                "all",
                "trade",
                "deposit",
                "withdrawal",
                "transfer",
                "margin",
                "adjustment",
                "rollover",
                "credit",
                "settled",
                "staking",
                "dividend",
                "sale",
                "nft_rebate",
            }
        )
    for field, values in enum_fields.items():
        if field in binding.query and binding.query[field] not in values:
            raise ProviderTransportScopeError(
                f"Kraken Spot {field} is outside the documented enum"
            )


def _kraken_spot_authenticated_read_rule(
    binding: AuthenticatedReadQueryBinding,
) -> AuthenticatedReadEndpointRule:
    rule = KRAKEN_SPOT_AUTHENTICATED_READ_ENDPOINTS.get(binding.endpoint)
    if rule is None:
        raise ProviderTransportScopeError(
            "Kraken Spot authenticated-read endpoint is not explicitly allowed"
        )
    if binding.surface != rule.surface:
        raise ProviderTransportScopeError(
            "authenticated-read endpoint surface does not match Kraken Spot policy"
        )
    if binding.permission_scope != rule.permission_scope:
        raise ProviderTransportScopeError(
            "authenticated-read permission scope does not match Kraken Spot endpoint policy"
        )
    _validate_kraken_spot_authenticated_read_query(binding)
    return rule


@dataclass(frozen=True)
class SignedHttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout_seconds: int

    def __post_init__(self) -> None:
        raw_method = self.method
        if (
            type(raw_method) is not str
            or not raw_method
            or raw_method != raw_method.strip()
        ):
            raise ProviderTransportScopeError(
                "signed request method must be canonical text"
            )
        method = raw_method.upper()
        if method != "POST":
            raise ProviderTransportScopeError(
                "trade transport currently permits only POST"
            )

        raw_url = self.url
        if (
            type(raw_url) is not str
            or not raw_url
            or raw_url != raw_url.strip()
        ):
            raise ProviderTransportScopeError("signed request URL is invalid")
        if (
            not raw_url.isascii()
            or any(
                character <= " " or character == "\x7f"
                for character in raw_url
            )
            or "\\" in raw_url
        ):
            raise ProviderTransportScopeError("signed request URL is invalid")
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ProviderTransportScopeError("signed request URL is invalid")
        if type(self.body) is not bytes:
            raise ProviderTransportScopeError(
                "signed request body must be exact bytes"
            )
        has_query = bool(parsed.query)
        has_body = bool(self.body)
        if has_query == has_body:
            raise ProviderTransportScopeError(
                "signed POST requires exactly one payload channel: URL query or body"
            )

        if type(self.headers) not in {dict, MappingProxyType}:
            raise ProviderTransportScopeError(
                "signed request headers must be an exact inert mapping"
            )
        normalized_headers: dict[str, str] = {}
        normalized_header_names: set[str] = set()
        for raw_key, raw_value in self.headers.items():
            if (
                type(raw_key) is not str
                or not raw_key
                or raw_key != raw_key.strip()
            ):
                raise ProviderTransportScopeError(
                    "signed request header names must be canonical text"
                )
            if (
                type(raw_value) is not str
                or not raw_value
                or raw_value != raw_value.strip()
            ):
                raise ProviderTransportScopeError(
                    "signed request header values must be canonical text"
                )
            if (
                "\r" in raw_key
                or "\n" in raw_key
                or "\r" in raw_value
                or "\n" in raw_value
            ):
                raise ProviderTransportScopeError(
                    "header values must not contain line breaks"
                )
            if (
                not raw_key.isascii()
                or any(
                    not (
                        character.isalnum()
                        or character in "!#$%&'*+-.^_|~"
                        or character == "\x60"
                    )
                    for character in raw_key
                )
            ):
                raise ProviderTransportScopeError(
                    "signed request header names must be canonical text"
                )
            if (
                not raw_value.isascii()
                or any(
                    character < " " or character == "\x7f"
                    for character in raw_value
                )
            ):
                raise ProviderTransportScopeError(
                    "signed request header values must be canonical text"
                )
            canonical_name = raw_key.lower()
            if canonical_name in normalized_header_names:
                raise ProviderTransportScopeError(
                    "signed request header names must be unique case-insensitively"
                )
            normalized_header_names.add(canonical_name)
            normalized_headers[raw_key] = raw_value

        if (
            type(self.timeout_seconds) is not int
            or self.timeout_seconds < 1
            or self.timeout_seconds > 120
        ):
            raise ProviderTransportScopeError("invalid request timeout")
        object.__setattr__(self, "url", raw_url)
        object.__setattr__(self, "method", method)
        object.__setattr__(
            self, "headers", MappingProxyType(dict(normalized_headers))
        )
        _register_signed_http_request(self)


def _install_signed_http_request_integrity():
    """Seal one exact write envelope against post-construction mutation."""

    request_type = SignedHttpRequest
    object_getattribute = object.__getattribute__
    canonical_type = type
    canonical_id = id
    mapping_proxy_type = canonical_type(MappingProxyType({}))
    weakref = weakref_ref
    states: dict[int, tuple[object, tuple[object, ...]]] = {}

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(value: SignedHttpRequest) -> None:
        if canonical_type(value) is not request_type:
            return
        method = object_getattribute(value, "method")
        url = object_getattribute(value, "url")
        headers = object_getattribute(value, "headers")
        body = object_getattribute(value, "body")
        timeout_seconds = object_getattribute(value, "timeout_seconds")
        if (
            canonical_type(method) is not str
            or canonical_type(url) is not str
            or canonical_type(headers) is not mapping_proxy_type
            or canonical_type(body) is not bytes
            or canonical_type(timeout_seconds) is not int
        ):
            raise ProviderTransportScopeError(
                "signed request must contain exact canonical fields"
            )
        if any(
            canonical_type(key) is not str or canonical_type(item) is not str
            for key, item in headers.items()
        ):
            raise ProviderTransportScopeError(
                "signed request headers must contain exact text"
            )
        prune()
        object_id = canonical_id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            raise ProviderTransportScopeError(
                "signed request identity collision"
            )
        states[object_id] = (
            weakref(value),
            (method, url, headers, body, timeout_seconds),
        )

    def require(
        value: SignedHttpRequest,
    ) -> tuple[str, str, Mapping[str, str], bytes, int]:
        if canonical_type(value) is not request_type:
            raise TypeError("request must be exact SignedHttpRequest")
        prune()
        state = states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            raise ProviderTransportScopeError(
                "signed request lacks construction authority"
            )
        method, url, headers, body, timeout_seconds = state[1]
        current_method = object_getattribute(value, "method")
        current_url = object_getattribute(value, "url")
        current_headers = object_getattribute(value, "headers")
        current_body = object_getattribute(value, "body")
        current_timeout = object_getattribute(value, "timeout_seconds")
        if (
            canonical_type(current_method) is not str
            or canonical_type(current_url) is not str
            or canonical_type(current_body) is not bytes
            or canonical_type(current_timeout) is not int
            or current_method != method
            or current_url != url
            or current_headers is not headers
            or current_body != body
            or current_timeout != timeout_seconds
        ):
            raise ProviderTransportScopeError(
                "signed request changed after construction"
            )
        return method, url, headers, body, timeout_seconds

    return register, require


(
    _register_signed_http_request,
    _require_signed_http_request,
) = _install_signed_http_request_integrity()
del _install_signed_http_request_integrity


@dataclass(frozen=True)
class AuthenticatedReadHttpRequest:
    """One immutable authenticated provider read request.

    GET supports both exact signed-query reads and session-authenticated
    queryless reads. POST supports both signed form bodies and exact empty-body
    session reads. Provider route/capability authority remains outside this
    envelope, which stays separate from SignedHttpRequest so read responses keep
    their typed observation lifecycle and never acquire write authority.
    """

    url: str
    headers: Mapping[str, str]
    timeout_seconds: int
    method: str = "GET"
    body: bytes = b""

    def __post_init__(self) -> None:
        raw_method = self.method
        if (
            type(raw_method) is not str
            or not raw_method
            or raw_method != raw_method.strip()
        ):
            raise ProviderTransportScopeError(
                "authenticated-read method must be canonical text"
            )
        method = raw_method.upper()
        if method not in {"GET", "POST"}:
            raise ProviderTransportScopeError(
                "authenticated-read method must be GET or POST"
            )

        raw_url = self.url
        if (
            type(raw_url) is not str
            or not raw_url
            or raw_url != raw_url.strip()
        ):
            raise ProviderTransportScopeError(
                "authenticated-read URL must be canonical HTTPS"
            )
        if (
            not raw_url.isascii()
            or any(
                character <= " " or character == "\x7f"
                for character in raw_url
            )
            or "\\" in raw_url
        ):
            raise ProviderTransportScopeError(
                "authenticated-read URL must be canonical HTTPS"
            )
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ProviderTransportScopeError(
                "authenticated-read URL must be canonical HTTPS"
            )
        if type(self.body) is not bytes:
            raise ProviderTransportScopeError(
                "authenticated-read body must be exact bytes"
            )
        if method == "GET":
            if self.body:
                raise ProviderTransportScopeError(
                    "authenticated GET requires no body"
                )
        elif parsed.query:
            raise ProviderTransportScopeError(
                "authenticated POST requires no URL query"
            )

        if type(self.headers) not in {dict, MappingProxyType}:
            raise ProviderTransportScopeError(
                "authenticated-read headers must be an exact inert mapping"
            )
        normalized_headers: dict[str, str] = {}
        normalized_header_names: set[str] = set()
        for raw_key, raw_value in self.headers.items():
            if (
                type(raw_key) is not str
                or not raw_key
                or raw_key != raw_key.strip()
            ):
                raise ProviderTransportScopeError(
                    "authenticated-read header names must be canonical text"
                )
            if (
                type(raw_value) is not str
                or not raw_value
                or raw_value != raw_value.strip()
            ):
                raise ProviderTransportScopeError(
                    "authenticated-read header values must be canonical text"
                )
            if (
                "\r" in raw_key
                or "\n" in raw_key
                or "\r" in raw_value
                or "\n" in raw_value
            ):
                raise ProviderTransportScopeError(
                    "header values must not contain line breaks"
                )
            if (
                not raw_key.isascii()
                or any(
                    not (
                        character.isalnum()
                        or character in "!#$%&'*+-.^_|~"
                        or character == "\x60"
                    )
                    for character in raw_key
                )
            ):
                raise ProviderTransportScopeError(
                    "authenticated-read header names must be canonical text"
                )
            if (
                not raw_value.isascii()
                or any(
                    character < " " or character == "\x7f"
                    for character in raw_value
                )
            ):
                raise ProviderTransportScopeError(
                    "authenticated-read header values must be canonical text"
                )
            canonical_name = raw_key.lower()
            if canonical_name in normalized_header_names:
                raise ProviderTransportScopeError(
                    "authenticated-read header names must be unique case-insensitively"
                )
            normalized_header_names.add(canonical_name)
            normalized_headers[raw_key] = raw_value

        if (
            type(self.timeout_seconds) is not int
            or self.timeout_seconds < 1
            or self.timeout_seconds > 120
        ):
            raise ProviderTransportScopeError("invalid request timeout")
        object.__setattr__(self, "url", raw_url)
        object.__setattr__(self, "method", method)
        object.__setattr__(
            self,
            "headers",
            MappingProxyType(dict(normalized_headers)),
        )
        _register_authenticated_read_http_request(self)


def _install_authenticated_read_http_request_integrity():
    """Seal one exact read envelope against post-construction mutation.

    Frozen dataclasses can still be changed through object.__setattr__.  The
    wire boundary therefore keeps a closure-private construction snapshot and
    requires the exact request object to still match that snapshot before any
    outbound field is consumed.  Header identity is retained deliberately: the
    constructor replaces caller mappings with a fresh mappingproxy, so identity
    proves the later mapping is still that detached canonical copy without
    invoking an attacker-supplied mapping implementation.
    """

    request_type = AuthenticatedReadHttpRequest
    object_getattribute = object.__getattribute__
    canonical_type = type
    canonical_id = id
    mapping_proxy_type = canonical_type(MappingProxyType({}))
    weakref = weakref_ref
    states: dict[int, tuple[object, tuple[object, ...]]] = {}

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(value: AuthenticatedReadHttpRequest) -> None:
        if canonical_type(value) is not request_type:
            return
        method = object_getattribute(value, "method")
        url = object_getattribute(value, "url")
        headers = object_getattribute(value, "headers")
        body = object_getattribute(value, "body")
        timeout_seconds = object_getattribute(value, "timeout_seconds")
        if (
            canonical_type(method) is not str
            or canonical_type(url) is not str
            or canonical_type(headers) is not mapping_proxy_type
            or canonical_type(body) is not bytes
            or canonical_type(timeout_seconds) is not int
        ):
            raise ProviderTransportScopeError(
                "authenticated-read request must contain exact canonical fields"
            )
        if any(
            canonical_type(key) is not str or canonical_type(item) is not str
            for key, item in headers.items()
        ):
            raise ProviderTransportScopeError(
                "authenticated-read headers must contain exact text"
            )
        prune()
        object_id = canonical_id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            raise ProviderTransportScopeError(
                "authenticated-read request identity collision"
            )
        states[object_id] = (
            weakref(value),
            (method, url, headers, body, timeout_seconds),
        )

    def require(
        value: AuthenticatedReadHttpRequest,
    ) -> tuple[str, str, Mapping[str, str], bytes, int]:
        if canonical_type(value) is not request_type:
            raise TypeError(
                "request must be exact AuthenticatedReadHttpRequest"
            )
        prune()
        state = states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            raise ProviderTransportScopeError(
                "authenticated-read request lacks construction authority"
            )
        method, url, headers, body, timeout_seconds = state[1]
        current_method = object_getattribute(value, "method")
        current_url = object_getattribute(value, "url")
        current_headers = object_getattribute(value, "headers")
        current_body = object_getattribute(value, "body")
        current_timeout = object_getattribute(value, "timeout_seconds")
        if (
            canonical_type(current_method) is not str
            or canonical_type(current_url) is not str
            or canonical_type(current_body) is not bytes
            or canonical_type(current_timeout) is not int
            or current_method != method
            or current_url != url
            or current_headers is not headers
            or current_body != body
            or current_timeout != timeout_seconds
        ):
            raise ProviderTransportScopeError(
                "authenticated-read request changed after construction"
            )
        return method, url, headers, body, timeout_seconds

    return register, require


(
    _register_authenticated_read_http_request,
    _require_authenticated_read_http_request,
) = _install_authenticated_read_http_request_integrity()
del _install_authenticated_read_http_request_integrity


@dataclass(frozen=True)
class TradingWireResponse:
    """Definitive HTTP response observed after one guarded write send."""

    http_status: int
    body: bytes

    def __post_init__(self) -> None:
        if (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ProviderTransportScopeError("HTTP status must be an integer 100..599")
        try:
            require_provider_response_bytes(
                self.body,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=True,
            )
        except (TypeError, ValueError) as error:
            raise ProviderTransportError("invalid or oversized trading response") from error


@dataclass(frozen=True)
class AuthenticatedReadWireResponse:
    http_status: int
    body: bytes

    def __post_init__(self) -> None:
        if (
            type(self.http_status) is not int
            or self.http_status < 100
            or self.http_status > 599
        ):
            raise ProviderTransportScopeError("HTTP status must be an integer 100..599")
        try:
            require_provider_response_bytes(self.body, max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES)
        except (TypeError, ValueError) as error:
            raise ProviderTransportError("invalid or oversized authenticated-read response") from error


_DIRECT_TRADING_WRITE_TRANSPORT_IDENTITY = (
    "autotrade.provider_transport.UrllibJsonWireClient:direct-trading-write:v1"
)
_DIRECT_TRADING_WRITE_NETWORK_POLICY_IDENTITY = "sha256:" + sha256(
    json.dumps(
        {
            "automatic_retries": False,
            "https_only": True,
            "proxy_mode": "DIRECT_ONLY",
            "redirects": False,
            "response_body": "BOUNDED_EXACT_BYTES",
            "transport": "urllib",
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class DirectTradingWriteExecutionReceipt:
    """Closure-authorized proof of one exact direct trading HTTP response.

    This proves direct network execution only.  It is not provider lifecycle,
    qualification, financial-admission or fill authority by itself.
    """

    transport_identity: str
    network_policy_identity: str
    request_sha256: str
    http_status: int
    response_sha256: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderTransportError(
            "direct trading-write receipt is minted only by canonical wire execution"
        )


def direct_trading_write_transport_identity() -> str:
    return _DIRECT_TRADING_WRITE_TRANSPORT_IDENTITY


def direct_trading_write_network_policy_identity() -> str:
    return _DIRECT_TRADING_WRITE_NETWORK_POLICY_IDENTITY


def _direct_trading_write_request_digest(request: SignedHttpRequest) -> str:
    method, url, headers, body, timeout_seconds = _require_signed_http_request(request)
    material = {
        "method": method,
        "url_sha256": "sha256:" + sha256(url.encode("utf-8")).hexdigest(),
        "headers_sha256": "sha256:"
        + sha256(
            json.dumps(
                dict(sorted(dict(headers).items())),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "body_sha256": "sha256:" + sha256(body).hexdigest(),
        "timeout_seconds": timeout_seconds,
    }
    return "sha256:" + sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibJsonWireClient:
    """One-shot TLS client with redirects and automatic retries disabled."""

    def __init__(self, *, max_response_bytes: int = DEFAULT_MAX_PROVIDER_RESPONSE_BYTES) -> None:
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= HARD_MAX_PROVIDER_RESPONSE_BYTES:
            raise ProviderTransportScopeError("provider response byte budget is invalid")
        self.max_response_bytes = max_response_bytes
        # urllib otherwise discovers process/OS proxies implicitly. The
        # production shared client is direct-only; proxies require separate
        # explicit network-policy authority, not ambient environment variables.
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    def _response_budget(self) -> int:
        budget = self.max_response_bytes
        if (
            type(budget) is not int
            or not 1 <= budget <= HARD_MAX_PROVIDER_RESPONSE_BYTES
        ):
            raise ProviderTransportScopeError(
                "provider response byte budget is invalid"
            )
        return budget

    def _bounded_body(self, raw: bytes, *, max_bytes: int) -> bytes:
        try:
            return require_provider_response_bytes(
                raw,
                max_bytes=max_bytes,
                allow_empty=True,
            )
        except (TypeError, ValueError) as error:
            raise ProviderTransportError("invalid or oversized provider HTTP response") from error

    def _install_send_authority():
        canonical_type = type
        canonical_int = int
        canonical_bytes = bytes
        signed_request_type = SignedHttpRequest
        authenticated_read_request_type = AuthenticatedReadHttpRequest
        require_signed_request = _require_signed_http_request
        require_authenticated_read_request = _require_authenticated_read_http_request
        request_constructor = Request
        mapping_copy = dict
        type_error = TypeError
        base_exception = Exception
        provider_transport_error = ProviderTransportError
        http_error_type = HTTPError
        url_error_type = URLError
        authenticated_response_type = AuthenticatedReadWireResponse
        trading_response_type = TradingWireResponse

        def send(
            self,
            request: SignedHttpRequest | AuthenticatedReadHttpRequest,
        ) -> bytes | TradingWireResponse | AuthenticatedReadWireResponse:
            request_type = canonical_type(request)
            if request_type is authenticated_read_request_type:
                method, url, headers, body, timeout_seconds = (
                    require_authenticated_read_request(request)
                )
                is_authenticated_read = True
            elif request_type is signed_request_type:
                method, url, headers, body, timeout_seconds = require_signed_request(
                    request
                )
                is_authenticated_read = False
            else:
                raise type_error(
                    "request must be exact SignedHttpRequest or exact AuthenticatedReadHttpRequest"
                )
            data = body or None
            outbound = request_constructor(
                url,
                data=data,
                headers=mapping_copy(headers),
                method=method,
            )
            # Capture one exact validated budget before any response-body read.
            # Mutating the client during I/O cannot widen this send's read envelope.
            response_budget = self._response_budget()
            http_status: int | None = None
            http_error_status: int | None = None
            http_error_invalid_status = False
            http_error_read_failed = False
            transport_unavailable = False
            try:
                with self._opener.open(
                    outbound,
                    timeout=timeout_seconds,
                ) as response:
                    http_status = canonical_int(response.status)
                    raw = self._bounded_body(
                        response.read(response_budget + 1),
                        max_bytes=response_budget,
                    )
            except http_error_type as error:
                # An HTTPError retains its request URL and sometimes provider
                # headers, including signed read-query/credential material.
                # Read at most one bounded body here, but NEVER raise or construct
                # typed responses while the secret-bearing exception is active:
                # implicit __context__/explicit __cause__ would expose it later.
                try:
                    observed_status = error.code
                    if (
                        canonical_type(observed_status) is canonical_int
                        and 100 <= observed_status <= 599
                    ):
                        http_error_status = observed_status
                    else:
                        http_error_invalid_status = True
                except base_exception:
                    http_error_invalid_status = True
                if (
                    http_error_status is not None
                    and not 300 <= http_error_status < 400
                ):
                    try:
                        raw = error.read(response_budget + 1)
                    except base_exception:
                        http_error_read_failed = True
            except url_error_type:
                # urllib's transport exception can retain request metadata too.
                # The guarded caller already handles uncertainty after SEND.
                transport_unavailable = True

            # Only primitive, detached status/bytes/flags cross the exception
            # boundary. New failures are generated OUTSIDE urllib exception scope,
            # so their public context chain cannot contain the signed HTTPError.
            if transport_unavailable:
                raise provider_transport_error(
                    "provider HTTP transport response unavailable"
                )
            if http_error_invalid_status:
                raise provider_transport_error("provider HTTP error status invalid")
            if http_error_status is not None:
                if 300 <= http_error_status < 400:
                    raise provider_transport_error("provider redirect is prohibited")
                if http_error_read_failed:
                    raise provider_transport_error(
                        "provider HTTP error body unavailable"
                    )
                raw = self._bounded_body(raw, max_bytes=response_budget)
                if is_authenticated_read:
                    return authenticated_response_type(
                        http_status=http_error_status,
                        body=raw,
                    )
                return trading_response_type(
                    http_status=http_error_status,
                    body=raw,
                )
            if canonical_type(raw) is not canonical_bytes:
                raise provider_transport_error(
                    "provider returned a non-byte response"
                )
            if is_authenticated_read and not raw:
                raise provider_transport_error(
                    "authenticated-read provider returned an empty response"
                )
            if is_authenticated_read:
                if http_status is None:
                    raise provider_transport_error(
                        "authenticated-read HTTP status is unavailable"
                    )
                return authenticated_response_type(
                    http_status=http_status,
                    body=raw,
                )
            if http_status is None:
                raise provider_transport_error(
                    "trading HTTP status is unavailable"
                )
            return trading_response_type(
                http_status=http_status,
                body=raw,
            )

        return send

    send = _install_send_authority()
    del _install_send_authority


def _install_direct_trading_write_execution_authority():
    """Bind exact direct urllib write I/O to one non-self-mintable receipt."""

    clients: dict[int, tuple[object, tuple[object, ...]]] = {}
    receipts: dict[int, tuple[object, object, tuple[str, str, str, int, str]]] = {}

    canonical_type = type
    canonical_id = id
    canonical_tuple = tuple
    canonical_list = list
    canonical_dict = dict
    canonical_set = set
    canonical_bool = bool
    canonical_int = int
    canonical_str = str
    canonical_bytes = bytes
    canonical_repr = repr
    canonical_sorted = sorted
    canonical_getattr = getattr
    canonical_isinstance = isinstance
    canonical_zip = zip
    canonical_vars = vars
    canonical_len = len
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    weakref = weakref_ref

    client_type = UrllibJsonWireClient
    request_type = SignedHttpRequest
    response_type = TradingWireResponse
    receipt_type = DirectTradingWriteExecutionReceipt
    transport_error = ProviderTransportError
    opener_type = OpenerDirector
    proxy_type = ProxyHandler
    redirect_base_type = HTTPRedirectHandler
    redirect_type = _NoRedirectHandler
    abstract_http_type = AbstractHTTPHandler
    https_handler_type = HTTPSHandler

    canonical_opener_open = OpenerDirector.open
    canonical_opener_dispatch = OpenerDirector._open
    canonical_opener_call_chain = OpenerDirector._call_chain
    canonical_http_do_open = AbstractHTTPHandler.do_open
    canonical_https_open = HTTPSHandler.https_open
    canonical_proxy_open = ProxyHandler.proxy_open
    canonical_redirect_request = _NoRedirectHandler.redirect_request
    canonical_request_digest = _direct_trading_write_request_digest
    request_digest_code = canonical_request_digest.__code__
    canonical_require_signed_request = _require_signed_http_request
    require_signed_request_code = canonical_require_signed_request.__code__
    canonical_sha256 = sha256
    canonical_json_module = json
    canonical_json_dumps = json.dumps
    transport_identity = _DIRECT_TRADING_WRITE_TRANSPORT_IDENTITY
    network_policy_identity = _DIRECT_TRADING_WRITE_NETWORK_POLICY_IDENTITY

    def freeze_authority_state(value: object) -> object:
        value_type = canonical_type(value)
        if value is None or value_type in {canonical_bool, canonical_int, canonical_str, canonical_bytes}:
            return value
        if value_type is canonical_tuple:
            return ("tuple", canonical_tuple(freeze_authority_state(item) for item in value))
        if value_type is canonical_list:
            return ("list", canonical_tuple(freeze_authority_state(item) for item in value))
        if value_type is canonical_dict:
            frozen_items = canonical_tuple(
                canonical_sorted(
                    (
                        (
                            freeze_authority_state(key),
                            freeze_authority_state(item),
                        )
                        for key, item in value.items()
                    ),
                    key=canonical_repr,
                )
            )
            return ("dict", frozen_items)
        if value_type is canonical_set:
            return (
                "set",
                canonical_tuple(
                    canonical_sorted(
                        (freeze_authority_state(item) for item in value),
                        key=canonical_repr,
                    )
                ),
            )
        return ("identity", value_type, canonical_id(value))

    def implementation_changed() -> bool:
        return (
            UrllibJsonWireClient is not client_type
            or SignedHttpRequest is not request_type
            or TradingWireResponse is not response_type
            or DirectTradingWriteExecutionReceipt is not receipt_type
            or OpenerDirector is not opener_type
            or ProxyHandler is not proxy_type
            or HTTPRedirectHandler is not redirect_base_type
            or _NoRedirectHandler is not redirect_type
            or AbstractHTTPHandler is not abstract_http_type
            or HTTPSHandler is not https_handler_type
            or type is not canonical_type
            or id is not canonical_id
            or tuple is not canonical_tuple
            or list is not canonical_list
            or dict is not canonical_dict
            or set is not canonical_set
            or bool is not canonical_bool
            or int is not canonical_int
            or str is not canonical_str
            or bytes is not canonical_bytes
            or repr is not canonical_repr
            or sorted is not canonical_sorted
            or getattr is not canonical_getattr
            or isinstance is not canonical_isinstance
            or zip is not canonical_zip
            or vars is not canonical_vars
            or len is not canonical_len
            or object is not canonical_object
            or weakref_ref is not weakref
            or sha256 is not canonical_sha256
            or json is not canonical_json_module
            or canonical_json_module.dumps is not canonical_json_dumps
            or _direct_trading_write_request_digest is not canonical_request_digest
            or canonical_request_digest.__code__ is not request_digest_code
            or _require_signed_http_request is not canonical_require_signed_request
            or canonical_require_signed_request.__code__ is not require_signed_request_code
            or _DIRECT_TRADING_WRITE_TRANSPORT_IDENTITY != transport_identity
            or _DIRECT_TRADING_WRITE_NETWORK_POLICY_IDENTITY != network_policy_identity
        )

    def client_network_authority(client: UrllibJsonWireClient) -> tuple[object, ...]:
        if implementation_changed():
            raise transport_error(
                "direct trading-write wire implementation authority changed"
            )
        opener = object_getattribute(client, "__dict__").get("_opener")
        if canonical_type(opener) is not opener_type:
            raise transport_error(
                "direct trading-write wire client opener is not canonical"
            )
        opener_state = canonical_vars(opener)
        if "open" in opener_state:
            raise transport_error(
                "direct trading-write wire client opener method is shadowed"
            )
        if (
            opener_type.open is not canonical_opener_open
            or opener_type._open is not canonical_opener_dispatch
            or opener_type._call_chain is not canonical_opener_call_chain
            or abstract_http_type.do_open is not canonical_http_do_open
            or https_handler_type.https_open is not canonical_https_open
            or proxy_type.proxy_open is not canonical_proxy_open
            or redirect_type.redirect_request is not canonical_redirect_request
        ):
            raise transport_error(
                "direct trading-write wire client network implementation changed"
            )
        handlers = canonical_tuple(canonical_getattr(opener, "handlers", ()))
        proxies = canonical_tuple(
            handler for handler in handlers if canonical_type(handler) is proxy_type
        )
        redirects = canonical_tuple(
            handler
            for handler in handlers
            if canonical_isinstance(handler, redirect_base_type)
        )
        if (
            canonical_len(proxies) != 1
            or canonical_getattr(proxies[0], "proxies", None) != {}
            or canonical_len(redirects) != 1
            or canonical_type(redirects[0]) is not redirect_type
        ):
            raise transport_error(
                "direct trading-write wire client direct-only policy changed"
            )
        handler_state = canonical_tuple(
            (handler, freeze_authority_state(canonical_vars(handler)))
            for handler in handlers
        )
        return (
            opener,
            freeze_authority_state(opener_state),
            handler_state,
        )

    def prune() -> None:
        for states in (clients, receipts):
            for object_id, state in canonical_tuple(states.items()):
                if state[0]() is None:
                    states.pop(object_id, None)

    def register_client(client: object) -> None:
        if canonical_type(client) is not client_type:
            return
        prune()
        try:
            authority = client_network_authority(client)
        except transport_error:
            # Neutral/test clients remain usable, but cannot gain direct-wire proof.
            return
        clients[canonical_id(client)] = (weakref(client), authority)

    def require_client(client: object) -> UrllibJsonWireClient:
        if canonical_type(client) is not client_type:
            raise transport_error(
                "canonical direct trading-write wire client is required"
            )
        prune()
        client_state = clients.get(canonical_id(client))
        try:
            current_authority = client_network_authority(client)
        except transport_error as error:
            raise transport_error(
                "direct trading-write wire client network authority changed"
            ) from error
        if (
            client_state is None
            or client_state[0]() is not client
            or client_state[1] != current_authority
        ):
            raise transport_error(
                "direct trading-write wire client network authority changed"
            )
        return client

    def eligible_client(client: object) -> bool:
        try:
            require_client(client)
        except transport_error:
            return False
        return True

    def mint(
        client: object,
        request: object,
        response: object,
    ) -> DirectTradingWriteExecutionReceipt | None:
        if (
            canonical_type(client) is not client_type
            or canonical_type(request) is not request_type
            or canonical_type(response) is not response_type
        ):
            return None
        if not eligible_client(client):
            return None
        request_sha256 = canonical_request_digest(request)
        response_body = object_getattribute(response, "body")
        http_status = object_getattribute(response, "http_status")
        if (
            canonical_type(response_body) is not canonical_bytes
            or canonical_type(http_status) is not canonical_int
            or not 100 <= http_status <= 599
        ):
            raise transport_error(
                "direct trading-write response is not canonical"
            )
        response_sha256 = "sha256:" + canonical_sha256(response_body).hexdigest()
        receipt = canonical_object.__new__(receipt_type)
        values = (
            transport_identity,
            network_policy_identity,
            request_sha256,
            http_status,
            response_sha256,
        )
        for field_name, field_value in canonical_zip(
            (
                "transport_identity",
                "network_policy_identity",
                "request_sha256",
                "http_status",
                "response_sha256",
            ),
            values,
        ):
            canonical_object.__setattr__(receipt, field_name, field_value)
        receipts[canonical_id(receipt)] = (
            weakref(receipt),
            weakref(response),
            values,
        )
        canonical_object.__setattr__(
            response,
            "_direct_trading_write_execution_receipt",
            receipt,
        )
        return receipt

    def snapshot(
        receipt: object,
    ) -> tuple[str, str, str, int, str, object | None]:
        if implementation_changed() or canonical_type(receipt) is not receipt_type:
            raise transport_error(
                "canonical direct trading-write execution receipt is required"
            )
        prune()
        state = receipts.get(canonical_id(receipt))
        if state is None or state[0]() is not receipt:
            raise transport_error(
                "direct trading-write receipt construction authority is unavailable"
            )
        values = state[2]
        current = canonical_tuple(
            object_getattribute(receipt, name)
            for name in (
                "transport_identity",
                "network_policy_identity",
                "request_sha256",
                "http_status",
                "response_sha256",
            )
        )
        if current != values:
            raise transport_error(
                "direct trading-write receipt changed after wire execution"
            )
        return (*values, state[1]())

    return register_client, require_client, eligible_client, mint, snapshot


(
    _register_direct_trading_write_client,
    require_direct_trading_write_client,
    _direct_trading_write_client_is_eligible,
    _mint_direct_trading_write_execution_receipt,
    _direct_trading_write_execution_receipt_state,
) = _install_direct_trading_write_execution_authority()
del _install_direct_trading_write_execution_authority


def _bind_direct_trading_write_client_init(init_impl, register_client):
    def __init__(self, *args, **kwargs):
        init_impl(self, *args, **kwargs)
        register_client(self)

    return __init__


def _bind_direct_trading_write_send(send_impl, eligible_client, mint_receipt):
    request_type = SignedHttpRequest
    canonical_type = type

    def send(self, request):
        eligible_before_send = (
            canonical_type(request) is request_type
            and eligible_client(self)
        )
        response = send_impl(self, request)
        if eligible_before_send:
            mint_receipt(self, request, response)
        return response

    return send


UrllibJsonWireClient.__init__ = _bind_direct_trading_write_client_init(
    UrllibJsonWireClient.__init__,
    _register_direct_trading_write_client,
)
UrllibJsonWireClient.send = _bind_direct_trading_write_send(
    UrllibJsonWireClient.send,
    _direct_trading_write_client_is_eligible,
    _mint_direct_trading_write_execution_receipt,
)
del _bind_direct_trading_write_client_init
del _bind_direct_trading_write_send
del _register_direct_trading_write_client
del _direct_trading_write_client_is_eligible
del _mint_direct_trading_write_execution_receipt


def _bind_direct_trading_write_receipt_access(snapshot_impl):
    response_type = TradingWireResponse
    receipt_type = DirectTradingWriteExecutionReceipt
    canonical_type = type
    canonical_getattr = getattr
    canonical_sha256 = sha256
    mapping_proxy = MappingProxyType
    transport_error = ProviderTransportError
    object_getattribute = object.__getattribute__

    def direct_trading_write_execution_receipt(
        response: TradingWireResponse,
    ) -> DirectTradingWriteExecutionReceipt:
        if canonical_type(response) is not response_type:
            raise transport_error(
                "exact trading wire response is required"
            )
        receipt = canonical_getattr(
            response,
            "_direct_trading_write_execution_receipt",
            None,
        )
        values = snapshot_impl(receipt)
        if values[5] is not response:
            raise transport_error(
                "direct trading-write receipt is not bound to exact response"
            )
        status = object_getattribute(response, "http_status")
        raw = object_getattribute(response, "body")
        if (
            values[3] != status
            or values[4] != "sha256:" + canonical_sha256(raw).hexdigest()
        ):
            raise transport_error(
                "direct trading-write receipt does not match exact response"
            )
        return receipt

    def direct_trading_write_execution_receipt_snapshot(
        receipt: DirectTradingWriteExecutionReceipt,
    ) -> Mapping[str, object]:
        if canonical_type(receipt) is not receipt_type:
            raise transport_error(
                "canonical direct trading-write execution receipt is required"
            )
        values = snapshot_impl(receipt)
        return mapping_proxy(
            {
                "transport_identity": values[0],
                "network_policy_identity": values[1],
                "request_sha256": values[2],
                "http_status": values[3],
                "response_sha256": values[4],
            }
        )

    return (
        direct_trading_write_execution_receipt,
        direct_trading_write_execution_receipt_snapshot,
    )


(
    direct_trading_write_execution_receipt,
    direct_trading_write_execution_receipt_snapshot,
) = _bind_direct_trading_write_receipt_access(
    _direct_trading_write_execution_receipt_state
)
del _bind_direct_trading_write_receipt_access
del _direct_trading_write_execution_receipt_state


def _install_direct_trading_exact_response_authority(
    receipt_reader,
    receipt_snapshot,
):
    """Transfer live direct-wire proof without adding forgeable DTO fields."""

    states: dict[int, tuple[object, object, tuple[object, ...]]] = {}
    canonical_type = type
    canonical_id = id
    canonical_tuple = tuple
    canonical_dict = dict
    canonical_set = set
    canonical_frozenset = frozenset
    canonical_int = int
    canonical_bytes = bytes
    canonical_str = str
    canonical_bool = bool
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    weakref = weakref_ref
    exact_type = ExactJsonTransportResponse
    wire_type = TradingWireResponse
    transport_error = ProviderTransportError
    canonical_sha256 = sha256
    receipt_reader_code = receipt_reader.__code__
    receipt_snapshot_code = receipt_snapshot.__code__
    exact_field_names = canonical_frozenset(
        {
            "response_bytes",
            "http_status",
            "requires_reconciliation",
            "ambiguity_reason",
        }
    )

    def implementation_changed() -> bool:
        return (
            ExactJsonTransportResponse is not exact_type
            or TradingWireResponse is not wire_type
            or type is not canonical_type
            or id is not canonical_id
            or tuple is not canonical_tuple
            or dict is not canonical_dict
            or set is not canonical_set
            or frozenset is not canonical_frozenset
            or int is not canonical_int
            or bytes is not canonical_bytes
            or str is not canonical_str
            or bool is not canonical_bool
            or object is not canonical_object
            or weakref_ref is not weakref
            or sha256 is not canonical_sha256
            or receipt_reader.__code__ is not receipt_reader_code
            or receipt_snapshot.__code__ is not receipt_snapshot_code
        )

    def exact_snapshot(value: ExactJsonTransportResponse) -> tuple[object, ...]:
        if implementation_changed() or canonical_type(value) is not exact_type:
            raise transport_error(
                "exact trading response authority is unavailable"
            )
        state = object_getattribute(value, "__dict__")
        if canonical_type(state) is not canonical_dict:
            raise transport_error(
                "exact trading response state is not canonical"
            )
        if canonical_frozenset(state) != exact_field_names:
            raise transport_error(
                "exact trading response shape changed"
            )
        raw = state["response_bytes"]
        status = state["http_status"]
        reconciliation = state["requires_reconciliation"]
        reason = state["ambiguity_reason"]
        if (
            canonical_type(raw) is not canonical_bytes
            or (status is not None and canonical_type(status) is not canonical_int)
            or canonical_type(reconciliation) is not canonical_bool
            or (reason is not None and canonical_type(reason) is not canonical_str)
        ):
            raise transport_error(
                "exact trading response scalar authority changed"
            )
        return (raw, status, reconciliation, reason)

    def prune() -> None:
        for object_id, state in canonical_tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def inherit(
        source: object,
        exact: ExactJsonTransportResponse,
    ) -> ExactJsonTransportResponse:
        if canonical_type(source) is not wire_type:
            return exact
        try:
            receipt = receipt_reader(source)
        except transport_error:
            return exact
        receipt_values = receipt_snapshot(receipt)
        exact_values = exact_snapshot(exact)
        raw = exact_values[0]
        status = exact_values[1]
        if (
            status != receipt_values["http_status"]
            or "sha256:" + canonical_sha256(raw).hexdigest()
            != receipt_values["response_sha256"]
        ):
            raise transport_error(
                "direct trading-write receipt differs from exact response"
            )
        prune()
        states[canonical_id(exact)] = (
            weakref(exact),
            receipt,
            exact_values,
        )
        return exact

    def require(
        exact: ExactJsonTransportResponse,
    ) -> DirectTradingWriteExecutionReceipt:
        if implementation_changed() or canonical_type(exact) is not exact_type:
            raise transport_error(
                "exact direct trading-write response is required"
            )
        prune()
        state = states.get(canonical_id(exact))
        if state is None or state[0]() is not exact:
            raise transport_error(
                "exact response lacks direct trading-write execution authority"
            )
        current = exact_snapshot(exact)
        if current != state[2]:
            raise transport_error(
                "exact direct trading-write response changed after wire execution"
            )
        receipt = state[1]
        receipt_values = receipt_snapshot(receipt)
        if (
            current[1] != receipt_values["http_status"]
            or "sha256:" + canonical_sha256(current[0]).hexdigest()
            != receipt_values["response_sha256"]
        ):
            raise transport_error(
                "direct trading-write receipt no longer matches exact response"
            )
        return receipt

    return inherit, require


(
    _inherit_direct_trading_write_receipt,
    direct_trading_write_exact_response_receipt,
) = _install_direct_trading_exact_response_authority(
    direct_trading_write_execution_receipt,
    direct_trading_write_execution_receipt_snapshot,
)
del _install_direct_trading_exact_response_authority


def _direct_trading_exact_response(
    source: object,
    response_bytes: bytes,
    *,
    http_status: int | None = None,
    requires_reconciliation: bool = False,
    ambiguity_reason: str | None = None,
) -> ExactJsonTransportResponse:
    exact = ExactJsonTransportResponse(
        response_bytes,
        http_status=http_status,
        requires_reconciliation=requires_reconciliation,
        ambiguity_reason=ambiguity_reason,
    )
    return _inherit_direct_trading_write_receipt(source, exact)


def _trading_response_evidence(
    value: object,
) -> tuple[bytes, int | None]:
    """Validate exact post-SEND bytes/status without assuming a JSON body.

    Provider-specific classifiers must be able to preserve an already observed
    ambiguous HTTP result even when a gateway or upstream proxy returned HTML
    or arbitrary opaque bytes. Definitive responses still pass through the
    strict ExactJsonTransportResponse JSON contract below.
    """

    if type(value) is TradingWireResponse:
        # Frozen dataclasses can still be built without __init__ or modified
        # through object.__setattr__. Revalidate the nested HTTP status at
        # the actual post-SEND authority boundary, before virtual comparisons.
        status = value.http_status
        if type(status) is not int or not 100 <= status <= 599:
            raise ProviderTransportError("invalid trading HTTP response status")
        try:
            raw = require_provider_response_bytes(
                value.body,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=True,
            )
        except (TypeError, ValueError) as error:
            raise ProviderTransportError(
                "invalid or oversized trading response"
            ) from error
        return raw, status
    if type(value) is bytes:
        try:
            raw = require_provider_response_bytes(
                value,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (TypeError, ValueError) as error:
            raise ProviderTransportError(
                "invalid or oversized trading response"
            ) from error
        return raw, None
    raise ProviderTransportError(
        "trading wire client returned an unsupported response contract"
    )


def _exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Preserve HTTP status for a definitive exact JSON provider response.

    Raw bytes remain accepted for injected legacy/test wire clients. Production
    UrllibJsonWireClient always returns TradingWireResponse for guarded writes.
    """

    raw, status = _trading_response_evidence(value)
    return _direct_trading_exact_response(
        value,
        raw,
        http_status=status,
    )


def _bybit_exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Preserve Bybit post-send uncertainty for reconciliation.

    Durable dispatch must agree with the canonical Bybit response parser:
    every HTTP non-2xx write result is reconciliation-first, and documented
    ambiguous business codes remain UNKNOWN even when the HTTP layer is 2xx.
    """

    raw, status = _trading_response_evidence(value)
    if status is None:
        return _direct_trading_exact_response(
            value,
            raw,
            requires_reconciliation=True,
            ambiguity_reason="bybit_http_status_unavailable_execution_unknown",
        )
    if 500 <= status <= 599:
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="bybit_http_5xx_execution_unknown",
        )
    if status < 200 or status > 299:
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="bybit_http_non_2xx_execution_unknown",
        )
    exact = _direct_trading_exact_response(
        value,
        raw,
        http_status=status,
    )
    parsed = exact.payload
    if (
        type(parsed) is dict
        and type(parsed.get("retCode")) is int
        and parsed["retCode"] in _BYBIT_AMBIGUOUS_RESPONSE_CODES
    ):
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="bybit_ambiguous_ret_code_execution_unknown",
        )
    return exact


def _kraken_spot_exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Keep post-send Kraken Spot transport ambiguity reconciliation-first."""

    raw, status = _trading_response_evidence(value)
    if status is None:
        return _direct_trading_exact_response(
            value,
            raw,
            requires_reconciliation=True,
            ambiguity_reason="kraken_spot_http_status_unavailable_execution_unknown",
        )
    if 500 <= status <= 599:
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="kraken_spot_http_5xx_execution_unknown",
        )
    exact = _direct_trading_exact_response(
        value,
        raw,
        http_status=status,
    )
    if spot_submission_requires_reconciliation(exact.payload):
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="kraken_spot_deadline_elapsed",
        )
    return exact


def _alpaca_exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Keep post-send Alpaca transport ambiguity reconciliation-first."""

    raw, status = _trading_response_evidence(value)
    if status is None:
        return _direct_trading_exact_response(
            value,
            raw,
            requires_reconciliation=True,
            ambiguity_reason="alpaca_http_status_unavailable_execution_unknown",
        )
    if 500 <= status <= 599:
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="alpaca_http_5xx_execution_unknown",
        )
    return _direct_trading_exact_response(
        value,
        raw,
        http_status=status,
    )


def _binance_exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Conservatively classify Binance Spot order-send execution uncertainty.

    Binance documents that 5xx does NOT mean the matching engine rejected the
    order. It also identifies -1007 as execution-status-unknown. Preserve the
    exact status and response bytes for reconciliation; NEVER blindly retry
    after GuardedDispatcher's irreversible send barrier. Validated ordinary
    4xx denials and successful responses retain their existing semantics.
    This classification is no substitute for qualified provider-origin truth.
    """

    raw, status = _trading_response_evidence(value)
    if status is None:
        return _direct_trading_exact_response(
            value,
            raw,
            requires_reconciliation=True,
            ambiguity_reason="binance_spot_http_status_unavailable_execution_unknown",
        )
    if 500 <= status <= 599:
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="binance_spot_http_5xx_execution_unknown",
        )
    exact = _direct_trading_exact_response(
        value,
        raw,
        http_status=status,
    )
    parsed = exact.payload
    if (
        type(parsed) is dict
        and type(parsed.get("code")) is int
        and parsed["code"] == -1007
    ):
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="binance_spot_backend_timeout_execution_unknown",
        )
    return exact


def _whitebit_exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse:
    """Bind WhiteBIT financial-write HTTP ambiguity to durable dispatch state.

    WhiteBIT 429 and 5xx responses after the send barrier do not prove that the
    financial write was not accepted. They therefore remain UNKNOWN until
    reconciliation, rather than becoming a retry-safe SubmissionSent terminal.
    """

    raw, status = _trading_response_evidence(value)
    if status is None:
        return _direct_trading_exact_response(
            value,
            raw,
            requires_reconciliation=True,
            ambiguity_reason="whitebit_http_status_unavailable_execution_unknown",
        )
    decision = classify_whitebit_http_retry(
        status_code=status,
        attempt=1,
        request_class="WRITE",
    )
    if decision.requires_reconciliation:
        return _direct_trading_exact_response(
            value,
            raw,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="whitebit_" + decision.classification.lower(),
        )
    return _direct_trading_exact_response(
        value,
        raw,
        http_status=status,
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
        scope_fields: Mapping[str, object] | None = None,
        max_nonce: int | None = None,
        nonce_domain_name: str = "positive integer",
        aggregate_identity_material: str | None = None,
        initial_nonce_floor: int = 0,
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
        if max_nonce is not None and (
            isinstance(max_nonce, bool)
            or not isinstance(max_nonce, int)
            or max_nonce < 1
        ):
            raise ProviderTransportScopeError(
                "max_nonce must be a positive integer or None"
            )
        if (
            isinstance(initial_nonce_floor, bool)
            or not isinstance(initial_nonce_floor, int)
            or initial_nonce_floor < 0
            or (
                max_nonce is not None
                and initial_nonce_floor > max_nonce
            )
        ):
            raise ProviderTransportScopeError(
                "initial_nonce_floor must be a non-negative integer within the nonce domain"
            )
        domain_name = _canonical_text(
            nonce_domain_name,
            name="nonce_domain_name",
        )
        scope: dict[str, str | int] = {}
        if scope_fields is not None:
            if not isinstance(scope_fields, Mapping):
                raise TypeError("scope_fields must be a mapping or None")
            for raw_key, raw_value in scope_fields.items():
                key = _canonical_text(raw_key, name="nonce scope field")
                if isinstance(raw_value, bool):
                    raise ProviderTransportScopeError(
                        f"nonce scope field {key} must be canonical text or a positive integer"
                    )
                if isinstance(raw_value, int):
                    if raw_value < 1:
                        raise ProviderTransportScopeError(
                            f"nonce scope field {key} must be positive"
                        )
                    value: str | int = raw_value
                else:
                    value = _canonical_text(
                        raw_value,
                        name=f"nonce scope field {key}",
                    )
                scope[key] = value

        self.provider_id = provider
        self.display_name = label
        self.journal = journal
        self.account_id = account
        self.environment = env
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc or (lambda: datetime.now(timezone.utc))
        self.max_contention_retries = max_contention_retries
        self.max_nonce = max_nonce
        self.nonce_domain_name = domain_name
        self.initial_nonce_floor = initial_nonce_floor
        self.scope_fields = MappingProxyType(scope)
        if aggregate_identity_material is None:
            aggregate_material = f"{self.account_id}|{self.environment}"
            if scope:
                aggregate_material += "|" + json.dumps(
                    scope,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
        else:
            aggregate_material = _canonical_text(
                aggregate_identity_material,
                name="aggregate_identity_material",
            )
        self.aggregate_id = (
            self.provider_id
            + ":"
            + sha256(aggregate_material.encode("utf-8")).hexdigest()
        )
        self._send_thread_lock = _serialized_nonce_send_lock(self.aggregate_id)
        self._send_lock_path = self.journal.path.with_name(
            self.journal.path.name
            + ".nonce-send-"
            + sha256(self.aggregate_id.encode("utf-8")).hexdigest()[:24]
            + ".lock"
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
                or any(
                    payload.get(key) != value
                    for key, value in self.scope_fields.items()
                )
            ):
                raise ProviderTransportError(
                    f"{self.display_name} nonce journal scope does not match allocator"
                )
            nonce = payload.get("nonce")
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or nonce <= previous_nonce
                or (
                    self.max_nonce is not None
                    and nonce > self.max_nonce
                )
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
        return max(previous_nonce, self.initial_nonce_floor), previous_version

    def allocate(self) -> int:
        for _ in range(self.max_contention_retries):
            previous_nonce, previous_version = self._history()
            candidate = self.clock_millis()
            if (
                isinstance(candidate, bool)
                or not isinstance(candidate, int)
                or candidate <= 0
                or (
                    self.max_nonce is not None
                    and candidate > self.max_nonce
                )
            ):
                raise ProviderTransportScopeError(
                    f"{self.display_name} nonce clock must return a {self.nonce_domain_name} value"
                )
            if (
                self.max_nonce is not None
                and previous_nonce >= self.max_nonce
            ):
                raise ProviderTransportScopeError(
                    f"{self.display_name} nonce authority exhausted {self.nonce_domain_name} domain"
                )
            nonce = max(candidate, previous_nonce + 1)
            if self.max_nonce is not None and nonce > self.max_nonce:
                raise ProviderTransportScopeError(
                    f"{self.display_name} nonce authority exhausted {self.nonce_domain_name} domain"
                )
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
                **self.scope_fields,
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

    def serialized_send(self):
        return _exclusive_nonce_send_lock(
            self._send_thread_lock,
            self._send_lock_path,
        )

    def __call__(self) -> int:
        return self.allocate()


class WhiteBitDurableNonceAllocator:
    """Journal-backed WhiteBIT nonce authority keyed by provider API-key identity.

    WhiteBIT authenticates the nonce together with X-TXC-APIKEY.  Local account
    labels and credential-handle generations are therefore admission metadata,
    not independent provider nonce domains.  Only a SHA-256 API-key fingerprint
    is persisted.
    """

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
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        account = _canonical_text(account_id, name="account_id")
        env = _canonical_environment(environment)
        if env != "LIVE":
            raise ProviderTransportScopeError(
                "WhiteBIT durable nonce allocation is qualified only for LIVE"
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

        self.journal = journal
        self.account_id = account
        self.environment = env
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.max_contention_retries = max_contention_retries
        self.legacy_nonce_floor = self._load_legacy_nonce_floor()

    def _load_legacy_nonce_floor(self) -> int:
        """Carry integrity-valid pre-API-key WhiteBIT nonce history forward."""

        highest = 0
        previous_version = 0
        previous_nonce = 0
        expected_aggregate_id = (
            "WHITEBIT:"
            + sha256(
                f"{self.account_id}|{self.environment}".encode("utf-8")
            ).hexdigest()
        )
        for event in self.journal.load_events_by_aggregate_type(
            _DurableProviderNonceAllocator.AGGREGATE_TYPE
        ):
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ProviderTransportError(
                    "provider nonce journal payload is invalid"
                )
            if payload.get("provider_id") != "WHITEBIT":
                continue
            if (
                payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
            ):
                continue
            scope_keys = set(payload) - {
                "provider_id",
                "account_id",
                "environment",
                "nonce",
            }
            if scope_keys == {"provider_api_key_fingerprint"}:
                continue
            if scope_keys:
                raise ProviderTransportError(
                    "WhiteBIT nonce journal contains an unknown legacy scope"
                )
            if (
                event.get("event_type")
                != _DurableProviderNonceAllocator.EVENT_TYPE
            ):
                raise ProviderTransportError(
                    "WhiteBIT legacy nonce journal contains an unexpected event type"
                )
            if event.get("aggregate_id") != expected_aggregate_id:
                raise ProviderTransportError(
                    "WhiteBIT legacy nonce aggregate identity is invalid"
                )
            nonce = payload.get("nonce")
            version = event.get("aggregate_version")
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or nonce <= 0
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
            ):
                raise ProviderTransportError(
                    "WhiteBIT legacy nonce journal is invalid"
                )
            if version != previous_version + 1:
                raise ProviderTransportError(
                    "WhiteBIT legacy nonce aggregate sequence is invalid"
                )
            if nonce <= previous_nonce:
                raise ProviderTransportError(
                    "WhiteBIT legacy nonce journal is not strictly monotonic"
                )
            previous_version = version
            previous_nonce = nonce
            highest = nonce
        return highest

    @staticmethod
    def provider_api_key_fingerprint(provider_api_key: object) -> str:
        api_key = _canonical_text(
            provider_api_key,
            name="WhiteBIT provider API key",
        )
        return "sha256:" + sha256(api_key.encode("utf-8")).hexdigest()

    def for_provider_api_key(
        self,
        provider_api_key: object,
    ) -> _DurableProviderNonceAllocator:
        fingerprint = self.provider_api_key_fingerprint(provider_api_key)
        return _DurableProviderNonceAllocator(
            provider_id="WHITEBIT",
            display_name="WhiteBIT",
            journal=self.journal,
            account_id=self.account_id,
            environment=self.environment,
            clock_millis=self.clock_millis,
            clock_utc=self.clock_utc,
            max_contention_retries=self.max_contention_retries,
            scope_fields={
                "provider_api_key_fingerprint": fingerprint,
            },
            aggregate_identity_material=(
                f"WHITEBIT|{self.environment}|provider-api-key|{fingerprint}"
            ),
            initial_nonce_floor=self.legacy_nonce_floor,
        )

    def aggregate_id_for_provider_api_key(self, provider_api_key: object) -> str:
        return self.for_provider_api_key(provider_api_key).aggregate_id

    def send_lock_path_for_provider_api_key(self, provider_api_key: object):
        return self.for_provider_api_key(provider_api_key)._send_lock_path


class KrakenSpotDurableNonceAllocator:
    """Journal-backed Kraken Spot nonce authority keyed by provider API-key identity.

    Local READ/TRADE handles and credential generations are admission metadata,
    not Kraken nonce domains. The provider API key is fingerprinted in-memory and
    only that non-secret fingerprint is persisted as nonce scope evidence.
    """

    def __init__(
        self,
        *,
        journal: JournalStore,
        account_id: str,
        environment: str,
        credential_handle: PersistentCredentialHandle,
        clock_millis: ClockMillis,
        clock_utc: ClockUtc | None = None,
        max_contention_retries: int = 32,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        account = _canonical_text(account_id, name="account_id")
        env = _canonical_environment(environment)
        if env != "LIVE":
            raise ProviderTransportScopeError(
                "Kraken Spot durable nonce allocation is qualified only for LIVE"
            )
        if (
            credential_handle.provider != "KRAKEN"
            or credential_handle.environment != env
            or credential_handle.purpose not in {"TRADE", "READ"}
            or credential_handle.account_id != account
        ):
            raise ProviderTransportScopeError(
                "Kraken Spot nonce credential scope mismatch"
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

        self.journal = journal
        self.account_id = account
        self.environment = env
        self.credential_handle_id = credential_handle.handle_id
        self.credential_generation = credential_handle.generation
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.max_contention_retries = max_contention_retries
        self.legacy_nonce_floor = self._load_legacy_nonce_floor()

    def _load_legacy_nonce_floor(self) -> int:
        """Carry only integrity-valid pre-provider-key Kraken history forward."""

        highest = 0
        legacy_state: dict[str, tuple[tuple[str, int], int, int]] = {}
        for event in self.journal.load_events_by_aggregate_type(
            _DurableProviderNonceAllocator.AGGREGATE_TYPE
        ):
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise ProviderTransportError(
                    "provider nonce journal payload is invalid"
                )
            if payload.get("provider_id") != "KRAKEN":
                continue
            if (
                payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
            ):
                continue
            scope_keys = set(payload) - {
                "provider_id",
                "account_id",
                "environment",
                "nonce",
            }
            if scope_keys == {"provider_api_key_fingerprint"}:
                continue
            if scope_keys != {
                "credential_handle_id",
                "credential_generation",
            }:
                raise ProviderTransportError(
                    "Kraken Spot nonce journal contains an unknown legacy scope"
                )
            if (
                event.get("event_type")
                != _DurableProviderNonceAllocator.EVENT_TYPE
            ):
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce journal contains an unexpected event type"
                )

            handle_id = payload.get("credential_handle_id")
            generation = payload.get("credential_generation")
            if (
                not isinstance(handle_id, str)
                or not handle_id
                or handle_id != handle_id.strip()
                or any(ord(character) < 0x20 for character in handle_id)
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
            ):
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce scope is invalid"
                )
            scope = (handle_id, generation)
            legacy_scope = {
                "credential_handle_id": handle_id,
                "credential_generation": generation,
            }
            aggregate_material = (
                f"{self.account_id}|{self.environment}|"
                + json.dumps(
                    legacy_scope,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
            )
            expected_aggregate_id = (
                "KRAKEN:"
                + sha256(aggregate_material.encode("utf-8")).hexdigest()
            )
            aggregate_id = event.get("aggregate_id")
            if aggregate_id != expected_aggregate_id:
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce aggregate identity is invalid"
                )

            nonce = payload.get("nonce")
            version = event.get("aggregate_version")
            if (
                isinstance(nonce, bool)
                or not isinstance(nonce, int)
                or nonce <= 0
                or nonce > _UINT64_MAX
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
            ):
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce journal is invalid"
                )

            previous = legacy_state.get(aggregate_id)
            if previous is None:
                previous_scope = scope
                previous_version = 0
                previous_nonce = 0
            else:
                previous_scope, previous_version, previous_nonce = previous
            if scope != previous_scope:
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce scope changed within one aggregate"
                )
            if version != previous_version + 1:
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce aggregate sequence is invalid"
                )
            if nonce <= previous_nonce:
                raise ProviderTransportError(
                    "Kraken Spot legacy nonce journal is not strictly monotonic"
                )
            legacy_state[aggregate_id] = (scope, version, nonce)
            highest = max(highest, nonce)
        return highest

    @staticmethod
    def provider_api_key_fingerprint(provider_api_key: object) -> str:
        api_key = _canonical_text(
            provider_api_key,
            name="Kraken Spot provider API key",
        )
        return "sha256:" + sha256(api_key.encode("utf-8")).hexdigest()

    def for_provider_api_key(
        self,
        provider_api_key: object,
    ) -> _DurableProviderNonceAllocator:
        fingerprint = self.provider_api_key_fingerprint(provider_api_key)
        return _DurableProviderNonceAllocator(
            provider_id="KRAKEN",
            display_name="Kraken Spot",
            journal=self.journal,
            account_id=self.account_id,
            environment=self.environment,
            clock_millis=self.clock_millis,
            clock_utc=self.clock_utc,
            max_contention_retries=self.max_contention_retries,
            scope_fields={
                "provider_api_key_fingerprint": fingerprint,
            },
            max_nonce=_UINT64_MAX,
            nonce_domain_name="unsigned 64-bit",
            aggregate_identity_material=(
                f"KRAKEN|{self.environment}|provider-api-key|{fingerprint}"
            ),
            initial_nonce_floor=self.legacy_nonce_floor,
        )

    def aggregate_id_for_provider_api_key(self, provider_api_key: object) -> str:
        return self.for_provider_api_key(provider_api_key).aggregate_id

    def send_lock_path_for_provider_api_key(self, provider_api_key: object):
        return self.for_provider_api_key(provider_api_key)._send_lock_path

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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
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
        if not callable(quota_gate):
            raise TypeError(
                "quota_gate must be callable for WhiteBIT LIVE transport"
            )
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

        self.quota_gate(
            "WHITEBIT",
            self.account_id,
            "LIVE",
            "ORDER_WRITE",
        )

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="WHITEBIT",
            environment="LIVE",
            purpose="TRADE",
        ) as credential_plaintext:
            try:
                credential = WhiteBitCredential.parse(credential_plaintext)
            finally:
                credential_plaintext = None

            provider_nonce = self.nonce_allocator.for_provider_api_key(
                credential.api_key
            )
            with provider_nonce.serialized_send():
                nonce = provider_nonce.allocate()
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

                final_guard()
                wire_response = self.wire_client.send(signed)
                return _whitebit_exact_trading_response(wire_response)


@dataclass(frozen=True)
class KrakenFuturesCredential:
    """Exact private credential shape used only at the signing boundary."""

    api_key: str
    api_secret: str

    @classmethod
    def parse(cls, plaintext: object) -> "KrakenFuturesCredential":
        if type(plaintext) is not str or not plaintext:
            raise ProviderTransportScopeError(
                "Kraken Futures credential material is unavailable"
            )
        try:
            value = json.loads(plaintext)
        except json.JSONDecodeError as error:
            raise ProviderTransportScopeError(
                "Kraken Futures credential material has invalid format"
            ) from error
        if not isinstance(value, dict) or set(value) != {"api_key", "api_secret"}:
            raise ProviderTransportScopeError(
                "Kraken Futures credential material must contain exact api_key/api_secret fields"
            )
        api_key = _canonical_text(value["api_key"], name="api_key")
        api_secret = _canonical_text(value["api_secret"], name="api_secret")
        try:
            decoded = base64.b64decode(api_secret, validate=True)
        except (ValueError, binascii.Error, UnicodeEncodeError) as error:
            raise ProviderTransportScopeError(
                "Kraken Futures api_secret must be canonical base64"
            ) from error
        if not decoded or base64.b64encode(decoded).decode("ascii") != api_secret:
            raise ProviderTransportScopeError(
                "Kraken Futures api_secret must be canonical base64 of non-empty bytes"
            )
        return cls(api_key=api_key, api_secret=api_secret)


def _kraken_futures_decimal_text(value: object, *, name: str) -> str:
    try:
        number = parse_canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ProviderTransportScopeError(
            f"Kraken Futures {name} must be canonical bounded decimal text"
        ) from error
    if number <= 0:
        raise ProviderTransportScopeError(
            f"Kraken Futures {name} must be positive and finite"
        )
    return value


def _kraken_futures_prepared_body(body: object) -> Mapping[str, str]:
    if type(body) not in (dict, MappingProxyType):
        raise ProviderTransportScopeError(
            "prepared Kraken Futures request body must be a mapping"
        )
    normalized = dict(body)
    if any(type(key) is not str for key in normalized):
        raise ProviderTransportScopeError(
            "prepared Kraken Futures order keys must be exact text"
        )
    required = {"orderType", "symbol", "side", "size", "cliOrdId"}
    optional = {"limitPrice", "reduceOnly"}
    if not required <= set(normalized) or set(normalized) - required - optional:
        raise ProviderTransportScopeError(
            "prepared Kraken Futures order body is not canonical"
        )
    if any(type(value) is not str for value in normalized.values()):
        raise ProviderTransportScopeError(
            "prepared Kraken Futures order values must be exact text"
        )
    order_type = _canonical_text(normalized["orderType"], name="orderType")
    if order_type not in {"mkt", "lmt"}:
        raise ProviderTransportScopeError(
            "prepared Kraken Futures orderType must be mkt or lmt"
        )
    symbol = _canonical_text(normalized["symbol"], name="symbol")
    side = _canonical_text(normalized["side"], name="side")
    if side not in {"buy", "sell"}:
        raise ProviderTransportScopeError(
            "prepared Kraken Futures side must be buy or sell"
        )
    size = _kraken_futures_decimal_text(normalized["size"], name="size")
    try:
        client_id = validate_futures_client_order_id(normalized["cliOrdId"])
    except Exception as error:
        raise ProviderTransportScopeError(
            "prepared Kraken Futures client order id is invalid"
        ) from error
    if client_id != normalized["cliOrdId"]:
        raise ProviderTransportScopeError(
            "prepared Kraken Futures client order id must be canonical text"
        )

    price = normalized.get("limitPrice")
    if order_type == "mkt":
        if price is not None:
            raise ProviderTransportScopeError(
                "prepared Kraken Futures market order must omit limitPrice"
            )
    elif price is None:
        raise ProviderTransportScopeError(
            "prepared Kraken Futures limit order requires limitPrice"
        )
    else:
        normalized["limitPrice"] = _kraken_futures_decimal_text(
            price,
            name="limitPrice",
        )
    if "reduceOnly" in normalized and normalized["reduceOnly"] != "true":
        raise ProviderTransportScopeError(
            "prepared Kraken Futures reduceOnly must be literal true when present"
        )
    normalized["orderType"] = order_type
    normalized["symbol"] = symbol
    normalized["side"] = side
    normalized["size"] = size
    normalized["cliOrdId"] = client_id
    return MappingProxyType(normalized)


class KrakenFuturesSigner:
    """Pure Derivatives v3 signer; this class owns no send authority."""

    PLACE_ORDER_ENDPOINT = "/derivatives/api/v3/sendorder"
    SIGNING_PATH = "/api/v3/sendorder"

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        provider_environment: object,
        endpoint: object,
        body: object,
        credential_plaintext: object,
        nonce: object,
    ) -> SignedHttpRequest:
        if type(provider_environment) is not str or type(policy) is not ProviderEndpointPolicy:
            raise ProviderTransportScopeError(
                "Kraken Futures policy does not match exact provider environment"
            )
        provider_env = provider_environment
        canonical_policy = KRAKEN_FUTURES_ENDPOINT_POLICIES.get(provider_env)
        exact_policy_values = (
            type(policy.provider_id) is str
            and type(policy.environment) is str
            and type(policy.base_url) is str
            and type(policy.allowed_hosts) is frozenset
            and all(type(host) is str for host in policy.allowed_hosts)
            and type(policy.timeout_seconds) is int
        )
        if canonical_policy is None or not exact_policy_values or policy != canonical_policy:
            raise ProviderTransportScopeError(
                "Kraken Futures policy does not match exact provider environment"
            )
        path = endpoint
        if type(path) is not str or path != KrakenFuturesSigner.PLACE_ORDER_ENDPOINT:
            raise ProviderTransportScopeError(
                "Kraken Futures signer permits only the canonical sendorder path"
            )
        parameters = _kraken_futures_prepared_body(body)
        if (
            type(nonce) is not int
            or nonce <= 0
            or nonce > _UINT64_MAX
        ):
            raise ProviderTransportScopeError(
                "Kraken Futures nonce must be an unsigned 64-bit positive integer"
            )
        credential = KrakenFuturesCredential.parse(credential_plaintext)
        exact_query = urlencode(sorted(parameters.items()))
        exact_query_bytes = exact_query.encode("ascii")
        digest = sha256(
            exact_query_bytes
            + str(nonce).encode("ascii")
            + KrakenFuturesSigner.SIGNING_PATH.encode("ascii")
        ).digest()
        secret = base64.b64decode(credential.api_secret, validate=True)
        signature = base64.b64encode(
            hmac.new(secret, digest, sha512).digest()
        ).decode("ascii")
        return SignedHttpRequest(
            method="POST",
            url=ProviderEndpointPolicy.absolute_url(canonical_policy, path) + "?" + exact_query,
            headers=MappingProxyType(
                {
                    "APIKey": credential.api_key,
                    "Nonce": str(nonce),
                    "Authent": signature,
                }
            ),
            body=b"",
            timeout_seconds=canonical_policy.timeout_seconds,
        )


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


def _kraken_spot_signed_form_parts(
    *,
    policy: ProviderEndpointPolicy,
    endpoint: object,
    parameters: Mapping[str, object],
    credential_plaintext: object,
    nonce: object,
) -> tuple[str, bytes, Mapping[str, str]]:
    """Build exact Kraken private REST form bytes and HMAC headers.

    Endpoint admission remains with the caller-specific write/read policy. This
    helper owns only the shared Kraken authentication math so writes and
    authenticated reads cannot drift into competing signer implementations.
    """

    if not isinstance(policy, ProviderEndpointPolicy):
        raise TypeError("policy must be ProviderEndpointPolicy")
    if policy.provider_id != "KRAKEN" or policy.environment != "LIVE":
        raise ProviderTransportScopeError(
            "Kraken Spot signing requires KRAKEN LIVE policy"
        )
    path = _canonical_text(endpoint, name="endpoint")
    if not path.startswith("/0/private/"):
        raise ProviderTransportScopeError(
            "Kraken Spot private signer requires /0/private/ endpoint"
        )
    policy.absolute_url(path)
    if not isinstance(parameters, Mapping):
        raise ProviderTransportScopeError(
            "Kraken Spot signed parameters must be a mapping"
        )
    if (
        isinstance(nonce, bool)
        or not isinstance(nonce, int)
        or nonce <= 0
        or nonce > _UINT64_MAX
    ):
        raise ProviderTransportScopeError(
            "Kraken Spot nonce must be an unsigned 64-bit positive integer"
        )

    canonical: dict[str, str] = {}
    for key, value in parameters.items():
        canonical_key = _canonical_text(key, name="Kraken Spot parameter")
        if canonical_key in {"nonce", "otp"}:
            raise ProviderTransportScopeError(
                "prepared Kraken Spot parameters contain transport-owned authentication fields"
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
    return (
        path,
        exact_body,
        MappingProxyType(
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "API-Key": credential.api_key,
                "API-Sign": signature,
            }
        ),
    )


class KrakenSpotSigner:
    """Pure Kraken Spot AddOrder HMAC-SHA512 signer."""

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        endpoint: str,
        body: Mapping[str, object],
        credential_plaintext: str,
        nonce: int,
    ) -> SignedHttpRequest:
        path = _canonical_text(endpoint, name="endpoint")
        if path != "/0/private/AddOrder":
            raise ProviderTransportScopeError(
                "Kraken Spot signer permits only the canonical AddOrder path"
            )
        path, exact_body, headers = _kraken_spot_signed_form_parts(
            policy=policy,
            endpoint=path,
            parameters=body,
            credential_plaintext=credential_plaintext,
            nonce=nonce,
        )
        return SignedHttpRequest(
            method="POST",
            url=policy.absolute_url(path),
            headers=headers,
            body=exact_body,
            timeout_seconds=policy.timeout_seconds,
        )


class KrakenSpotAuthenticatedReadSigner:
    """Pure signer for explicitly admitted Kraken Spot private account reads."""

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        query_binding: AuthenticatedReadQueryBinding,
        credential_plaintext: object,
        nonce: object,
    ) -> AuthenticatedReadHttpRequest:
        if not isinstance(query_binding, AuthenticatedReadQueryBinding):
            raise TypeError(
                "query_binding must be AuthenticatedReadQueryBinding"
            )
        _require_authenticated_read_query_binding_authority(query_binding)
        if policy.provider_id != "KRAKEN" or policy.environment != "LIVE":
            raise ProviderTransportScopeError(
                "Kraken Spot authenticated-read signer requires KRAKEN LIVE policy"
            )
        if (
            query_binding.provider_id != policy.provider_id
            or query_binding.environment != policy.environment
        ):
            raise ProviderTransportScopeError(
                "authenticated-read binding provider/environment mismatch"
            )
        _kraken_spot_authenticated_read_rule(query_binding)
        path, exact_body, headers = _kraken_spot_signed_form_parts(
            policy=policy,
            endpoint=query_binding.endpoint,
            parameters=query_binding.query,
            credential_plaintext=credential_plaintext,
            nonce=nonce,
        )
        return AuthenticatedReadHttpRequest(
            url=policy.absolute_url(path),
            headers=headers,
            timeout_seconds=policy.timeout_seconds,
            method="POST",
            body=exact_body,
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
    if tif not in {"GTC", "IOC"}:
        raise ProviderTransportScopeError(
            "prepared Kraken Spot timeinforce must be GTC or IOC"
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
        if order_type != "limit" or tif == "IOC":
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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
            )
        if not isinstance(nonce_allocator, KrakenSpotDurableNonceAllocator):
            raise TypeError(
                "nonce_allocator must be KrakenSpotDurableNonceAllocator"
            )
        if (
            nonce_allocator.account_id != account
            or nonce_allocator.environment != "LIVE"
            or nonce_allocator.credential_handle_id != credential_handle.handle_id
            or nonce_allocator.credential_generation != credential_handle.generation
        ):
            raise ProviderTransportScopeError(
                "nonce allocator credential/account/environment scope mismatch"
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

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="KRAKEN",
            environment="LIVE",
            purpose="TRADE",
        ) as credential_plaintext:
            provider_api_key = None
            try:
                provider_api_key = KrakenSpotCredential.parse(
                    credential_plaintext
                ).api_key
                nonce_domain = self.nonce_allocator.for_provider_api_key(
                    provider_api_key
                )
                provider_api_key = None
                with nonce_domain.serialized_send():
                    nonce = nonce_domain.allocate()
                    signed = KrakenSpotSigner.sign(
                        policy=self.policy,
                        endpoint=endpoint,
                        body=body,
                        credential_plaintext=credential_plaintext,
                        nonce=nonce,
                    )

                    final_guard()
                    wire_response = self.wire_client.send(signed)
                    return _kraken_spot_exact_trading_response(wire_response)
            finally:
                provider_api_key = None
                credential_plaintext = None



class KrakenSpotAuthenticatedReadTransport:
    """One-shot credential-scoped Kraken Spot private REST read.

    Reuses provider-core query/response identity, the canonical capability
    registry, WP-46 READ credential resolution and the existing Kraken durable
    nonce authority. It owns no retry, cache, reconciliation or financial
    authority.
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
        nonce_allocator: KrakenSpotDurableNonceAllocator,
        clock_utc: ClockUtc,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
    ) -> None:
        if not isinstance(policy, ProviderEndpointPolicy):
            raise TypeError("policy must be ProviderEndpointPolicy")
        if policy.provider_id != "KRAKEN" or policy.environment != "LIVE":
            raise ProviderTransportScopeError(
                "Kraken Spot authenticated-read transport requires KRAKEN LIVE policy"
            )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "KRAKEN"
            or credential_handle.environment != "LIVE"
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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
            )
        if not isinstance(nonce_allocator, KrakenSpotDurableNonceAllocator):
            raise TypeError(
                "nonce_allocator must be KrakenSpotDurableNonceAllocator"
            )
        if (
            nonce_allocator.account_id != account
            or nonce_allocator.environment != "LIVE"
            or nonce_allocator.credential_handle_id != credential_handle.handle_id
            or nonce_allocator.credential_generation != credential_handle.generation
        ):
            raise ProviderTransportScopeError(
                "nonce allocator credential/account/environment scope mismatch"
            )
        if not callable(clock_utc):
            raise TypeError("clock_utc must be callable")
        if quota_gate is not None and not callable(quota_gate):
            raise TypeError("quota_gate must be callable or None")
        if wire_client is not None and not hasattr(wire_client, "send"):
            raise TypeError("wire_client must implement send")

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
        self.nonce_allocator = nonce_allocator
        self.clock_utc = clock_utc
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()

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
                provider_id="KRAKEN",
                account_id=self.account_id,
                entity_id=query_binding.entity_id,
                environment="LIVE",
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
            or current.provider_id != "KRAKEN"
            or current.account_id != self.account_id
            or current.entity_id != query_binding.entity_id
            or current.environment != "LIVE"
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
            query_binding.provider_id != "KRAKEN"
            or query_binding.account_id != self.account_id
            or query_binding.environment != "LIVE"
            or query_binding.capability_snapshot_id != self.capability_snapshot_id
        ):
            raise ProviderTransportScopeError(
                "authenticated-read query scope mismatch"
            )
        rule = _kraken_spot_authenticated_read_rule(query_binding)

        if self.quota_gate is not None:
            self.quota_gate(
                "KRAKEN",
                self.account_id,
                "LIVE",
                "AUTHENTICATED_READ",
            )

        # Revalidate after quota delay and before READ credential access.
        self._require_current_capability(query_binding, rule)

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="KRAKEN",
            environment="LIVE",
            purpose="READ",
        ) as credential_plaintext:
            provider_api_key = None
            try:
                provider_api_key = KrakenSpotCredential.parse(
                    credential_plaintext
                ).api_key
                nonce_domain = self.nonce_allocator.for_provider_api_key(
                    provider_api_key
                )
                provider_api_key = None
                with nonce_domain.serialized_send():
                    nonce = nonce_domain.allocate()
                    signed = KrakenSpotAuthenticatedReadSigner.sign(
                        policy=self.policy,
                        query_binding=query_binding,
                        credential_plaintext=credential_plaintext,
                        nonce=nonce,
                    )

                    # Resolve authority again immediately before the irreversible read.
                    self._require_current_capability(query_binding, rule)
                    wire_response = self.wire_client.send(signed)
            finally:
                provider_api_key = None
                credential_plaintext = None

            if not isinstance(wire_response, AuthenticatedReadWireResponse):
                raise ProviderTransportError(
                    "authenticated-read wire client must preserve HTTP status"
                )
            if wire_response.http_status not in rule.success_statuses:
                raise ProviderTransportError(
                    "authenticated provider read returned unexpected HTTP status "
                    + str(wire_response.http_status)
                    + "; allowed="
                    + ",".join(
                        str(status) for status in sorted(rule.success_statuses)
                    )
                )
            observed_at = self.clock_utc()
            return observe_authenticated_json_response(
                query_binding=query_binding,
                http_status=wire_response.http_status,
                response_bytes=wire_response.body,
                observed_at=observed_at,
            )


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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
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

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            purpose="TRADE",
        ) as credential_plaintext:
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
            return _alpaca_exact_trading_response(wire_response)


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
            or credential_handle.provider_environment != provider_env
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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
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
                provider_environment=self.provider_environment,
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

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="BYBIT",
            environment=self.policy.environment,
            purpose="TRADE",
            provider_environment=self.provider_environment,
        ) as credential_plaintext:
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
            # Shared production urllib returns typed status+body, while legacy
            # injected diagnostic wire clients may return exact raw bytes.
            wire_response = self.wire_client.send(signed)
            return _bybit_exact_trading_response(wire_response)


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
        _require_authenticated_read_query_binding_authority(query_binding)
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
        if query_binding.endpoint == _BYBIT_OPTION_DELIVERY_ENDPOINT:
            # Bybit returns nextPageCursor as an already percent-encoded opaque
            # token and instructs callers to feed that exact token back. Encoding
            # '%' again would turn %3A/%2C into %253A/%252C and change both the
            # signed bytes and pagination meaning. Other fields retain the
            # existing urlencode contract; the cursor validator above limits the
            # raw token to RFC3986 unreserved bytes plus canonical %XX escapes.
            exact_parts = []
            for key, value in sorted(query.items()):
                if key == "cursor":
                    exact_parts.append("cursor=" + value)
                else:
                    exact_parts.append(urlencode(((key, value),)))
            exact_query = "&".join(exact_parts)
        else:
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
            or credential_handle.provider_environment != provider_env
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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
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
                provider_environment=self.provider_environment,
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

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="BYBIT",
            environment=self.policy.environment,
            purpose="READ",
            provider_environment=self.provider_environment,
        ) as credential_plaintext:
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
class IbkrWebBearerCredential:
    """Exact OAuth2 SSO bearer token used only inside one credential lease."""

    bearer_token: str

    @classmethod
    def parse(cls, plaintext: object) -> "IbkrWebBearerCredential":
        if type(plaintext) is not str or not plaintext:
            raise ProviderTransportScopeError(
                "IBKR OAuth2 bearer credential material is unavailable"
            )
        if (
            plaintext != plaintext.strip()
            or len(plaintext) > 16384
            or plaintext.lower().startswith("bearer ")
            or re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,}", plaintext) is None
        ):
            raise ProviderTransportScopeError(
                "IBKR OAuth2 bearer credential is not canonical token text"
            )
        return cls(bearer_token=plaintext)


class IbkrWebAuthenticatedReadSigner:
    """Pure OAuth2 direct-Web-API request builder for qualified read endpoints."""

    _API_PREFIX = "/v1/api"

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        query_binding: AuthenticatedReadQueryBinding,
        credential_plaintext: object,
    ) -> AuthenticatedReadHttpRequest:
        if type(policy) is not ProviderEndpointPolicy:
            raise TypeError("policy must be exact ProviderEndpointPolicy")
        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError(
                "query_binding must be exact AuthenticatedReadQueryBinding"
            )
        _require_authenticated_read_query_binding_authority(query_binding)
        if query_binding.provider_id != "IBKR":
            raise ProviderTransportScopeError(
                "IBKR authenticated-read signer requires IBKR binding"
            )
        canonical_policy = IBKR_WEB_ENDPOINT_POLICIES.get(
            query_binding.environment
        )
        if canonical_policy is None or policy != canonical_policy:
            raise ProviderTransportScopeError(
                "IBKR authenticated-read policy does not match exact environment"
            )
        _ibkr_authenticated_read_rule(query_binding)
        method = IBKR_WEB_AUTHENTICATED_READ_METHODS.get(
            query_binding.endpoint
        )
        if method is None:
            raise ProviderTransportScopeError(
                "IBKR authenticated-read method is not defined"
            )
        credential = IbkrWebBearerCredential.parse(credential_plaintext)
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + credential.bearer_token,
        }
        if method == "POST":
            # IBKR requires Content-Length on POST. Status carries no body.
            headers["Content-Length"] = "0"
            headers["Content-Type"] = "application/json"
        return AuthenticatedReadHttpRequest(
            url=policy.absolute_url(
                IbkrWebAuthenticatedReadSigner._API_PREFIX
                + query_binding.endpoint
            ),
            headers=MappingProxyType(headers),
            timeout_seconds=policy.timeout_seconds,
            method=method,
            body=b"",
        )


class IbkrWebAuthenticatedReadTransport:
    """One-shot OAuth2 direct IBKR read over existing capability authority.

    This transport owns no brokerage-session generation, retries, provider
    qualification, order submission, or reconciliation. It only carries the
    already-authorized status/accounts read to the pinned OAuth2 Web API host
    and mints the canonical exact-byte ProviderResponseObservation.
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
        clock_utc: ClockUtc,
        quota_gate: QuotaGate | None = None,
        wire_client: ProviderWireClient | None = None,
    ) -> None:
        if type(policy) is not ProviderEndpointPolicy:
            raise TypeError("policy must be exact ProviderEndpointPolicy")
        canonical_policy = IBKR_WEB_ENDPOINT_POLICIES.get(policy.environment)
        if (
            policy.provider_id != "IBKR"
            or canonical_policy is None
            or policy != canonical_policy
        ):
            raise ProviderTransportScopeError(
                "IBKR read policy must be exact OAuth2 direct Web API policy"
            )
        if type(credential_handle) is not PersistentCredentialHandle:
            raise TypeError(
                "credential_handle must be exact PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "IBKR"
            or credential_handle.environment != policy.environment
            or credential_handle.provider_environment != policy.environment
            or credential_handle.purpose != "READ"
        ):
            raise ProviderTransportScopeError(
                "IBKR READ credential handle scope mismatch"
            )
        account = _canonical_text(account_id, name="account_id")
        if credential_handle.account_id != account:
            raise ProviderTransportScopeError(
                "IBKR credential handle account mismatch"
            )
        if not isinstance(capability_registry, CapabilityRegistry):
            raise TypeError("capability_registry must be CapabilityRegistry")
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
            )
        if not callable(clock_utc):
            raise TypeError("clock_utc must be callable")
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
        self.clock_utc = clock_utc
        self.quota_gate = quota_gate
        self.wire_client = wire_client or UrllibJsonWireClient()

    def _require_current_capability(
        self,
        query_binding: AuthenticatedReadQueryBinding,
        rule: AuthenticatedReadEndpointRule,
    ) -> CapabilitySnapshot:
        point = self.clock_utc()
        if (
            type(point) is not datetime
            or point.tzinfo is None
            or point.utcoffset() is None
        ):
            raise ProviderTransportScopeError(
                "clock_utc must return a timezone-aware datetime"
            )
        point = point.astimezone(timezone.utc)
        try:
            current = self.capability_registry.require_verified(
                provider_id="IBKR",
                account_id=self.account_id,
                entity_id=query_binding.entity_id,
                environment=self.policy.environment,
                instrument_version=query_binding.instrument_version,
                at=point,
            )
        except Exception as error:
            raise ProviderTransportScopeError(
                "IBKR authenticated-read current capability cannot be verified"
            ) from error
        if (
            type(current) is not CapabilitySnapshot
            or current.snapshot_id != self.capability_snapshot_id
            or current.provider_id != "IBKR"
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
                "IBKR authenticated-read capability is no longer valid for exact binding"
            )
        return current

    def __call__(
        self,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> ProviderResponseObservation:
        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError(
                "query_binding must be exact AuthenticatedReadQueryBinding"
            )
        _require_authenticated_read_query_binding_authority(query_binding)
        if (
            query_binding.provider_id != "IBKR"
            or query_binding.account_id != self.account_id
            or query_binding.environment != self.policy.environment
            or query_binding.capability_snapshot_id
            != self.capability_snapshot_id
        ):
            raise ProviderTransportScopeError(
                "IBKR authenticated-read query scope mismatch"
            )
        rule = _ibkr_authenticated_read_rule(query_binding)

        if self.quota_gate is not None:
            self.quota_gate(
                "IBKR",
                self.account_id,
                self.policy.environment,
                "AUTHENTICATED_READ",
            )

        self._require_current_capability(query_binding, rule)
        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="IBKR",
            environment=self.policy.environment,
            purpose="READ",
            provider_environment=self.policy.environment,
        ) as credential_plaintext:
            try:
                request = IbkrWebAuthenticatedReadSigner.sign(
                    policy=self.policy,
                    query_binding=query_binding,
                    credential_plaintext=credential_plaintext,
                )
            finally:
                credential_plaintext = None

            # Credential access can race revocation/expiry; re-check immediately
            # before the one outbound read.
            self._require_current_capability(query_binding, rule)
            wire_response = self.wire_client.send(request)
            if type(wire_response) is not AuthenticatedReadWireResponse:
                raise ProviderTransportError(
                    "IBKR authenticated-read wire client must preserve HTTP status"
                )
            if wire_response.http_status not in rule.success_statuses:
                raise ProviderTransportError(
                    "IBKR authenticated read returned unexpected HTTP status "
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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
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
        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            purpose="TRADE",
        ) as credential_plaintext:
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
            return _binance_exact_trading_response(wire_response)


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
        _require_authenticated_read_query_binding_authority(query_binding)
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
        if not hasattr(secret_resolver, "lease_for_execution"):
            raise TypeError(
                "secret_resolver must implement lease_for_execution"
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

        with self.secret_resolver.lease_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            purpose="READ",
        ) as credential_plaintext:
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
