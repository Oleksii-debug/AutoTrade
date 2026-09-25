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

from .dispatch import ExactJsonTransportResponse
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
    def send(self, request: "SignedHttpRequest") -> bytes: ...


QuotaGate = Callable[[str, str, str, str], None]
ClockMillis = Callable[[], int]


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderTransportScopeError(f"{name} is required")
    return value.strip()


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
        path = _text(endpoint, name="endpoint")
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


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibJsonWireClient:
    """One-shot TLS client with redirects and automatic retries disabled."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def send(self, request: SignedHttpRequest) -> bytes:
        if not isinstance(request, SignedHttpRequest):
            raise TypeError("request must be SignedHttpRequest")
        outbound = Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with self._opener.open(
                outbound,
                timeout=request.timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as error:
            # Redirects are prohibited. Once the final barrier has passed even a
            # redirect response is an ambiguous write outcome, so propagate it
            # and let GuardedDispatcher preserve UNKNOWN.
            if 300 <= int(error.code) < 400:
                raise ProviderTransportError(
                    "provider redirect is prohibited"
                ) from error
            raw = error.read()
            if type(raw) is not bytes or not raw:
                raise ProviderTransportError(
                    "provider returned an empty HTTP error response"
                ) from error
        if type(raw) is not bytes or not raw:
            raise ProviderTransportError(
                "provider returned an empty or non-byte response"
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
            key = _text(raw_key, name="order parameter")
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
        self.capability_snapshot_id = capability
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
        endpoint = _text(request["endpoint"], name="endpoint")
        body = request["body"]
        capability = _text(
            request["capability_snapshot_id"],
            name="capability_snapshot_id",
        )
        if not isinstance(body, Mapping):
            raise ProviderTransportScopeError(
                "prepared Binance request body must be a mapping"
            )
        normalized: dict[str, str] = {}
        for raw_key, raw_value in body.items():
            key = _text(raw_key, name="order parameter")
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
        client_id = _text(client_order_id, name="client_order_id")
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
