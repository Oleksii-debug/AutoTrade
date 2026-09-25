"""Shared real-provider HTTP transport seam for guarded execution.

This module does not own financial authority, durable attempt state, retries, or
provider qualification. GuardedDispatcher remains the only send-state owner.
The transport resolves a scoped TRADE credential only at the final signing
boundary, calls the dispatcher's final guard exactly once, and performs exactly
one outbound HTTP request. Any exception after the guard is deliberately left
for GuardedDispatcher to classify as UNKNOWN.

Only Binance Spot PAPER/LIVE signing is wired here as the first concrete signer.
Other providers must reuse this network lifecycle and supply their own pure
signer/nonce rules rather than introduce another dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
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
    ) -> "bytes | AuthenticatedReadWireResponse": ...


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
    ) -> bytes:
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
        return raw


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
        raw = self.wire_client.send(signed)
        return ExactJsonTransportResponse(raw)


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
