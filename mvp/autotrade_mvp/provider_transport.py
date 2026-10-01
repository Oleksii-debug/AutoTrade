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
import weakref
from threading import Lock, local
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    ProxyHandler,
    build_opener,
)
from uuid import UUID

from .capabilities import CapabilityError, CapabilityRegistry, CapabilitySnapshot
from .dispatch import ExactJsonTransportResponse, ExactOpaqueTransportResponse
from .exact_decimal import ExactDecimalError, parse_canonical_decimal_text
from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
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
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
)
from .windows_secrets import PersistentCredentialHandle
from .security import SecurityBoundary
from .provider_qualification_authority import ProviderQualificationCurrentReader
from .provider_selection import (
    ProviderSelectionError,
    SelectedProviderAuthority,
    revalidate_selected_provider_authority,
)
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
        provider_environment: str | None,
        purpose: str,
    ) -> str: ...


class ProviderWireClient(Protocol):
    def send(
        self,
        request: "SignedHttpRequest | AuthenticatedReadHttpRequest",
    ) -> "bytes | TradingWireResponse | AuthenticatedReadWireResponse": ...


@dataclass(frozen=True)
class _ProductCredentialWireComposition:
    """Module-owned product resolver/wire authority for one exact transport object."""

    instance_ref: object
    transport_type: type
    security_boundary: SecurityBoundary
    wire_client: ProviderWireClient
    value_scope: tuple[tuple[str, object], ...]
    identity_scope: tuple[tuple[str, object], ...]
    policy_state: tuple[str, str, str, frozenset[str], int]
    credential_handle_state: tuple[str, str, str, str, str, int, str]
    selected_provider_authority: SelectedProviderAuthority | None
    qualification_reader: ProviderQualificationCurrentReader | None


_PRODUCT_CREDENTIAL_WIRE_GUARD = Lock()
_PRODUCT_CREDENTIAL_WIRE: dict[int, _ProductCredentialWireComposition] = {}
_PRODUCT_FACTORY_CONTEXT = local()
_PRODUCT_AUTHENTICATED_READ_CAPTURE_CONTEXT = local()


def _product_factory_construction_active() -> bool:
    return getattr(_PRODUCT_FACTORY_CONTEXT, "depth", 0) > 0


@contextmanager
def _product_factory_construction():
    """Permit canonical default-wire creation only inside the product factory."""

    depth = getattr(_PRODUCT_FACTORY_CONTEXT, "depth", 0)
    _PRODUCT_FACTORY_CONTEXT.depth = depth + 1
    try:
        yield
    finally:
        if depth:
            _PRODUCT_FACTORY_CONTEXT.depth = depth
        else:
            try:
                delattr(_PRODUCT_FACTORY_CONTEXT, "depth")
            except AttributeError:
                pass

_PRODUCT_VALUE_SCOPE_FIELDS = (
    "account_id",
    "capability_snapshot_id",
    "provider_environment",
    "session_token",
    "origin",
    "execution_identity",
    "recv_window_ms",
)
_PRODUCT_IDENTITY_SCOPE_FIELDS = (
    "policy",
    "credential_handle",
    "capability_registry",
    "nonce_allocator",
    "clock_millis",
    "clock_utc",
    "quota_gate",
)


class _ProductConstructionResolver:
    """Non-authoritative constructor placeholder used only by the product factory."""

    def resolve_for_execution(self, *_args, **_kwargs):
        raise PermissionError(
            "product credential resolution requires issued transport composition"
        )


def _product_transport_types() -> tuple[type, ...]:
    # Names resolve only when a caller invokes the factory after module import.
    return (
        WhiteBitHttpTransport,
        KrakenSpotHttpTransport,
        KrakenSpotAuthenticatedReadTransport,
        AlpacaTradingHttpTransport,
        AlpacaAuthenticatedReadTransport,
        BybitV5HttpTransport,
        BybitV5AuthenticatedReadTransport,
        BinanceSpotHttpTransport,
        BinanceSpotAuthenticatedReadTransport,
    )


def _capture_product_scope(
    transport: object,
) -> tuple[tuple[tuple[str, object], ...], tuple[tuple[str, object], ...]]:
    values: list[tuple[str, object]] = []
    identities: list[tuple[str, object]] = []
    for name in _PRODUCT_VALUE_SCOPE_FIELDS:
        try:
            value = object.__getattribute__(transport, name)
        except AttributeError:
            continue
        values.append((name, value))
    for name in _PRODUCT_IDENTITY_SCOPE_FIELDS:
        try:
            value = object.__getattribute__(transport, name)
        except AttributeError:
            continue
        identities.append((name, value))
    return tuple(values), tuple(identities)


def _product_credential_wire_composition(
    transport: object,
) -> _ProductCredentialWireComposition | None:
    object_id = id(transport)
    with _PRODUCT_CREDENTIAL_WIRE_GUARD:
        composition = _PRODUCT_CREDENTIAL_WIRE.get(object_id)
        if composition is not None and composition.instance_ref() is not transport:
            _PRODUCT_CREDENTIAL_WIRE.pop(object_id, None)
            composition = None
    return composition


def _register_product_credential_wire(
    transport: object,
    *,
    security_boundary: SecurityBoundary,
    wire_client: ProviderWireClient,
    selected_provider_authority: SelectedProviderAuthority | None,
    qualification_reader: ProviderQualificationCurrentReader | None,
) -> None:
    if type(security_boundary) is not SecurityBoundary:
        raise ProviderTransportScopeError(
            "product credential authority requires exact SecurityBoundary"
        )
    if type(transport) not in _product_transport_types():
        raise ProviderTransportScopeError(
            "product credential authority requires exact transport type"
        )
    if type(wire_client) is not UrllibJsonWireClient:
        raise ProviderTransportScopeError(
            "product credential authority requires canonical urllib wire"
        )
    product_read = type(transport) in _product_authenticated_read_transport_types()
    if product_read:
        if type(selected_provider_authority) is not SelectedProviderAuthority:
            raise ProviderTransportScopeError(
                "product authenticated read requires exact selected provider authority"
            )
        if type(qualification_reader) is not ProviderQualificationCurrentReader:
            raise ProviderTransportScopeError(
                "product authenticated read requires exact qualification current reader"
            )
    elif selected_provider_authority is not None or qualification_reader is not None:
        raise ProviderTransportScopeError(
            "provider read authority may only be attached to authenticated-read transports"
        )

    value_scope, identity_scope = _capture_product_scope(transport)
    policy = object.__getattribute__(transport, "policy")
    credential_handle = object.__getattribute__(transport, "credential_handle")
    policy_state = _require_canonical_product_policy_state(policy)
    credential_handle_state = _credential_handle_state(credential_handle)
    required_values = {
        "account_id",
        "capability_snapshot_id",
        "session_token",
        "origin",
        "execution_identity",
    }
    if not required_values.issubset({name for name, _value in value_scope}):
        raise ProviderTransportScopeError(
            "product credential authority scope is incomplete"
        )
    required_identities = {"policy", "credential_handle"}
    if not required_identities.issubset(
        {name for name, _value in identity_scope}
    ):
        raise ProviderTransportScopeError(
            "product credential authority identity scope is incomplete"
        )

    object_id = id(transport)
    with _PRODUCT_CREDENTIAL_WIRE_GUARD:
        current = _PRODUCT_CREDENTIAL_WIRE.get(object_id)
        if current is not None and current.instance_ref() is transport:
            raise ProviderTransportScopeError(
                "product credential/wire composition is already issued"
            )

        def _discard(dead_ref, *, expected_id=object_id):
            with _PRODUCT_CREDENTIAL_WIRE_GUARD:
                existing = _PRODUCT_CREDENTIAL_WIRE.get(expected_id)
                if existing is not None and existing.instance_ref is dead_ref:
                    _PRODUCT_CREDENTIAL_WIRE.pop(expected_id, None)

        ref = weakref.ref(transport, _discard)
        _PRODUCT_CREDENTIAL_WIRE[object_id] = _ProductCredentialWireComposition(
            instance_ref=ref,
            transport_type=type(transport),
            security_boundary=security_boundary,
            wire_client=wire_client,
            value_scope=value_scope,
            identity_scope=identity_scope,
            policy_state=policy_state,
            credential_handle_state=credential_handle_state,
            selected_provider_authority=selected_provider_authority,
            qualification_reader=qualification_reader,
        )


def build_product_credential_transport(
    transport_type: type,
    *,
    security_boundary: SecurityBoundary,
    selected_provider_authority: SelectedProviderAuthority | None = None,
    qualification_reader: ProviderQualificationCurrentReader | None = None,
    **transport_kwargs: object,
):
    """Issue one production credential/wire composition through the module TCB.

    Public provider transport constructors remain injection/test surfaces. They
    cannot acquire product credential authority merely because a caller happens
    to possess a SecurityBoundary object.
    """

    if type(security_boundary) is not SecurityBoundary:
        raise ProviderTransportScopeError(
            "product transport factory requires exact SecurityBoundary"
        )
    if transport_type not in _product_transport_types():
        raise ProviderTransportScopeError(
            "product transport factory requires an exact supported transport type"
        )
    if "secret_resolver" in transport_kwargs or "wire_client" in transport_kwargs:
        raise ProviderTransportScopeError(
            "product transport factory owns secret_resolver and wire_client"
        )

    product_read = transport_type in _product_authenticated_read_transport_types()
    if product_read:
        if type(selected_provider_authority) is not SelectedProviderAuthority:
            raise ProviderTransportScopeError(
                "product authenticated read requires exact selected provider authority"
            )
        if type(qualification_reader) is not ProviderQualificationCurrentReader:
            raise ProviderTransportScopeError(
                "product authenticated read requires exact qualification current reader"
            )
    elif selected_provider_authority is not None or qualification_reader is not None:
        raise ProviderTransportScopeError(
            "provider read authority may only be attached to authenticated-read transports"
        )

    kwargs = dict(transport_kwargs)
    if product_read:
        # Direct constructors remain TEST/INJECTED seams. Product-issued reads
        # consume process-owned authority time and cannot be backdated by callers.
        kwargs["clock_utc"] = _current_authority_utc
    if "policy" not in kwargs:
        raise ProviderTransportScopeError(
            "product transport factory requires canonical endpoint policy"
        )
    policy_state = _require_canonical_product_policy_state(kwargs["policy"])
    if product_read:
        selected = selected_provider_authority
        assert selected is not None
        if (
            selected.provider_id != policy_state[0]
            or selected.environment != policy_state[1]
            or kwargs.get("account_id") != selected.account_id
            or kwargs.get("capability_snapshot_id") != selected.capability_snapshot_id
            or (
                "provider_environment" in kwargs
                and kwargs.get("provider_environment") != selected.provider_environment
            )
        ):
            raise ProviderTransportScopeError(
                "selected provider authority does not match product transport scope"
            )
    kwargs["secret_resolver"] = _ProductConstructionResolver()
    kwargs["wire_client"] = None
    with _product_factory_construction():
        transport = transport_type(**kwargs)
    selected_wire = object.__getattribute__(transport, "_wire_client")
    if type(selected_wire) is not UrllibJsonWireClient:
        raise ProviderTransportScopeError(
            "product transport factory did not construct canonical wire"
        )
    _register_product_credential_wire(
        transport,
        security_boundary=security_boundary,
        wire_client=selected_wire,
        selected_provider_authority=selected_provider_authority,
        qualification_reader=qualification_reader,
    )
    # Keep the authority-bearing wire reachable only from the module registry.
    # The product instance retains no reference that ordinary caller code can
    # use to mutate the canonical opener or response budget in place.
    object.__setattr__(transport, "_wire_client", None)
    return transport


def _require_product_authenticated_read_authority(
    transport: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> None:
    """Revalidate exact selected Q1+C1 and canonical route at the wire cut."""

    composition = _product_credential_wire_composition(transport)
    if composition is None:
        return
    if type(transport) not in _product_authenticated_read_transport_types():
        return
    selected = composition.selected_provider_authority
    reader = composition.qualification_reader
    if type(selected) is not SelectedProviderAuthority or type(
        reader
    ) is not ProviderQualificationCurrentReader:
        raise ProviderTransportScopeError(
            "product authenticated-read Q+C authority is unavailable"
        )
    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
    if (
        query_binding.provider_id != selected.provider_id
        or query_binding.account_id != selected.account_id
        or query_binding.entity_id != selected.entity_id
        or query_binding.environment != selected.environment
        or query_binding.provider_environment != selected.provider_environment
        or query_binding.instrument_version != selected.instrument_version
        or query_binding.capability_snapshot_id != selected.capability_snapshot_id
    ):
        raise ProviderTransportScopeError(
            "authenticated-read query is outside selected provider authority"
        )

    registry = object.__getattribute__(transport, "capability_registry")
    if type(registry) is not CapabilityRegistry:
        raise ProviderTransportScopeError(
            "product authenticated-read capability registry changed"
        )
    try:
        first_route = canonical_authenticated_read_route(query_binding)
    except (ProviderTransportScopeError, TypeError, ValueError) as error:
        raise ProviderTransportScopeError(
            "authenticated-read route authority is unavailable before wire"
        ) from error

    point = _product_authority_utc()
    try:
        revalidate_selected_provider_authority(
            selected,
            qualification_reader=reader,
            capability_registry=registry,
            at=point,
        )
    except (ProviderSelectionError, TypeError, ValueError) as error:
        raise ProviderTransportScopeError(
            "selected provider Q+C authority changed before wire"
        ) from error

    try:
        final_route = canonical_authenticated_read_route(query_binding)
    except (ProviderTransportScopeError, TypeError, ValueError) as error:
        raise ProviderTransportScopeError(
            "authenticated-read route authority changed before wire"
        ) from error
    if final_route != first_route:
        raise ProviderTransportScopeError(
            "authenticated-read route authority changed during final Q+C cut"
        )


def _credential_wire_authority(
    transport: object,
) -> tuple[ProviderSecretResolver, ProviderWireClient]:
    """Resolve the exact factory-issued product pair or explicit injected test pair."""

    composition = _product_credential_wire_composition(transport)

    if composition is not None:
        if type(transport) is not composition.transport_type:
            raise ProviderTransportScopeError(
                "product credential/wire composition type changed"
            )
        for name, expected in composition.value_scope:
            if object.__getattribute__(transport, name) != expected:
                raise ProviderTransportScopeError(
                    "product credential/wire composition scope changed: " + name
                )
        for name, expected in composition.identity_scope:
            current = object.__getattribute__(transport, name)
            if current is not expected:
                raise ProviderTransportScopeError(
                    "product credential/wire composition authority changed: " + name
                )
        raw_policy = object.__getattribute__(transport, "policy")
        if _provider_endpoint_policy_state(raw_policy) != composition.policy_state:
            raise ProviderTransportScopeError(
                "product credential/wire composition policy state changed"
            )
        raw_handle = object.__getattribute__(transport, "credential_handle")
        if _credential_handle_state(raw_handle) != composition.credential_handle_state:
            raise ProviderTransportScopeError(
                "product credential/wire composition credential state changed"
            )
        selected_wire: ProviderWireClient = composition.wire_client
        capture = getattr(
            _PRODUCT_AUTHENTICATED_READ_CAPTURE_CONTEXT,
            "state",
            None,
        )
        if capture is not None:
            if capture.get("transport") is not transport:
                raise ProviderTransportScopeError(
                    "authenticated-read capture transport identity changed"
                )
            selected_wire = _AuthenticatedReadCaptureWire(
                composition.wire_client,
                capture,
            )
        return composition.security_boundary, selected_wire

    resolver = object.__getattribute__(transport, "secret_resolver")
    wire = object.__getattribute__(transport, "_wire_client")
    if type(resolver) is SecurityBoundary:
        raise ProviderTransportScopeError(
            "direct SecurityBoundary transport construction is not product authority; "
            "use build_product_credential_transport"
        )
    return resolver, wire


class _CredentialWireBoundTransport:
    """Separate factory-issued product authority from injected test pairs."""

    def __getattribute__(self, name: str):
        if name not in {"__dict__", "__class__"}:
            composition = _product_credential_wire_composition(self)
            if composition is not None:
                if name == "policy":
                    return _materialize_provider_endpoint_policy(
                        composition.policy_state
                    )
                if name == "credential_handle":
                    return _materialize_credential_handle(
                        composition.credential_handle_state
                    )
                for field, value in composition.value_scope:
                    if name == field:
                        return value
                for field, value in composition.identity_scope:
                    if name == field:
                        return value
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"secret_resolver", "_wire_client"}:
            try:
                object.__getattribute__(self, name)
            except AttributeError:
                pass
            else:
                raise ProviderTransportScopeError(
                    "credential/wire authority is immutable after construction"
                )
        object.__setattr__(self, name, value)

    @property
    def wire_client(self) -> ProviderWireClient:
        # Product callers must never receive the registry-owned canonical wire
        # object: mutating its opener/budget would mutate network authority
        # without replacing the transport slot. Injected/test transports retain
        # the observable wire seam for deterministic tests.
        composition = _product_credential_wire_composition(self)
        if composition is not None:
            raise ProviderTransportScopeError(
                "product credential/wire composition does not expose canonical wire"
            )
        return object.__getattribute__(self, "_wire_client")

    def _bind_wire_client(
        self,
        *,
        secret_resolver: ProviderSecretResolver,
        wire_client: ProviderWireClient | None,
    ) -> None:
        # Direct public constructors are TEST/INJECTED surfaces. Product/default
        # network creation is permitted only while the module-owned factory is
        # synchronously constructing one exact supported transport. Do not try
        # to detect every possible resolver proxy; prevent ordinary construction
        # from silently acquiring the canonical real-network wire.
        if type(secret_resolver) is SecurityBoundary:
            raise ProviderTransportScopeError(
                "direct SecurityBoundary transport construction is forbidden; "
                "use build_product_credential_transport"
            )
        factory_construction = (
            _product_factory_construction_active()
            and type(secret_resolver) is _ProductConstructionResolver
        )
        if wire_client is None:
            if not factory_construction:
                raise ProviderTransportScopeError(
                    "TEST/INJECTED transport construction requires an explicit wire_client"
                )
            selected = UrllibJsonWireClient()
        else:
            if factory_construction:
                raise ProviderTransportScopeError(
                    "product transport factory owns the canonical wire_client"
                )
            selected = wire_client
        object.__setattr__(self, "_wire_client", selected)


QuotaGate = Callable[[str, str, str, str], None]
ClockMillis = Callable[[], int]
ClockUtc = Callable[[], datetime]


def _current_authority_utc() -> datetime:
    """Return product-owned UTC authority time for irreversible LIVE admission."""

    return datetime.now(timezone.utc)


def _product_authority_utc() -> datetime:
    """Validate the product-owned clock before financial/provider authority use."""

    point = _current_authority_utc()
    if (
        type(point) is not datetime
        or point.tzinfo is None
        or point.utcoffset() is None
    ):
        raise ProviderTransportScopeError(
            "product authority UTC clock must return exact aware datetime"
        )
    return point.astimezone(timezone.utc)


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


def _select_canonical_kraken_spot_live_policy(
    policy: object,
    *,
    subject: str,
) -> ProviderEndpointPolicy:
    """Select immutable AutoTrade-owned Kraken Spot LIVE destination authority."""

    if type(policy) is not ProviderEndpointPolicy:
        raise TypeError(f"{subject} policy must be exact ProviderEndpointPolicy")
    registered = KRAKEN_SPOT_ENDPOINT_POLICIES.get("LIVE")
    if type(registered) is not ProviderEndpointPolicy:
        raise ProviderTransportScopeError(
            "canonical Kraken Spot LIVE endpoint policy state changed"
        )
    state = _require_canonical_product_policy_state(registered)
    expected = (
        "KRAKEN",
        "LIVE",
        "https://api.kraken.com",
        frozenset({"api.kraken.com"}),
        15,
    )
    if state != expected:
        raise ProviderTransportScopeError(
            "canonical Kraken Spot LIVE endpoint policy state changed"
        )
    if policy is not registered:
        raise ProviderTransportScopeError(
            f"{subject} requires the AutoTrade-owned canonical KRAKEN LIVE policy"
        )
    return _materialize_provider_endpoint_policy(state)


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


def _provider_endpoint_policy_state(
    policy: object,
) -> tuple[str, str, str, frozenset[str], int]:
    if type(policy) is not ProviderEndpointPolicy:
        raise ProviderTransportScopeError(
            "product transport requires exact ProviderEndpointPolicy"
        )
    state = (
        object.__getattribute__(policy, "provider_id"),
        object.__getattribute__(policy, "environment"),
        object.__getattribute__(policy, "base_url"),
        object.__getattribute__(policy, "allowed_hosts"),
        object.__getattribute__(policy, "timeout_seconds"),
    )
    if (
        type(state[0]) is not str
        or type(state[1]) is not str
        or type(state[2]) is not str
        or type(state[3]) is not frozenset
        or any(type(host) is not str for host in state[3])
        or type(state[4]) is not int
    ):
        raise ProviderTransportScopeError(
            "product endpoint policy state is malformed"
        )
    return state


def _provider_network_policy_identity(policy: ProviderEndpointPolicy) -> str:
    """Content identity of the exact canonical HTTPS destination policy."""

    provider_id, environment, base_url, allowed_hosts, timeout_seconds = (
        _provider_endpoint_policy_state(policy)
    )
    material = {
        "schema_version": "provider-network-policy:v1",
        "provider_id": provider_id,
        "environment": environment,
        "base_url": base_url,
        "allowed_hosts": sorted(allowed_hosts),
        "timeout_seconds": timeout_seconds,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return "sha256:" + sha256(encoded).hexdigest()


_CANONICAL_PROVIDER_ENDPOINT_POLICY_STATES: Mapping[
    int,
    tuple[
        ProviderEndpointPolicy,
        tuple[str, str, str, frozenset[str], int],
    ],
] = MappingProxyType(
    {
        id(policy): (policy, _provider_endpoint_policy_state(policy))
        for policy_map in (
            BINANCE_SPOT_ENDPOINT_POLICIES,
            WHITEBIT_ENDPOINT_POLICIES,
            KRAKEN_FUTURES_ENDPOINT_POLICIES,
            KRAKEN_SPOT_ENDPOINT_POLICIES,
            BYBIT_V5_ENDPOINT_POLICIES,
            ALPACA_ENDPOINT_POLICIES,
        )
        for policy in policy_map.values()
    }
)


def _require_canonical_product_policy_state(
    policy: object,
) -> tuple[str, str, str, frozenset[str], int]:
    state = _provider_endpoint_policy_state(policy)
    sealed = _CANONICAL_PROVIDER_ENDPOINT_POLICY_STATES.get(id(policy))
    if sealed is None or sealed[0] is not policy or sealed[1] != state:
        raise ProviderTransportScopeError(
            "product endpoint policy must be the unchanged canonical registry policy"
        )
    return state


def _materialize_provider_endpoint_policy(
    state: tuple[str, str, str, frozenset[str], int],
) -> ProviderEndpointPolicy:
    return ProviderEndpointPolicy(
        provider_id=state[0],
        environment=state[1],
        base_url=state[2],
        allowed_hosts=state[3],
        timeout_seconds=state[4],
    )


def _credential_handle_state(
    handle: object,
) -> tuple[str, str, str, str, str, int, str]:
    if type(handle) is not PersistentCredentialHandle:
        raise ProviderTransportScopeError(
            "product transport requires exact PersistentCredentialHandle"
        )
    state = (
        object.__getattribute__(handle, "handle_id"),
        object.__getattribute__(handle, "account_id"),
        object.__getattribute__(handle, "provider"),
        object.__getattribute__(handle, "environment"),
        object.__getattribute__(handle, "purpose"),
        object.__getattribute__(handle, "generation"),
        object.__getattribute__(handle, "provider_environment"),
    )
    if (
        any(type(value) is not str for value in (*state[:5], state[6]))
        or type(state[5]) is not int
    ):
        raise ProviderTransportScopeError(
            "product credential handle state is malformed"
        )
    return state


def _materialize_credential_handle(
    state: tuple[str, str, str, str, str, int, str],
) -> PersistentCredentialHandle:
    return PersistentCredentialHandle(
        handle_id=state[0],
        account_id=state[1],
        provider=state[2],
        environment=state[3],
        purpose=state[4],
        generation=state[5],
        provider_environment=state[6],
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


ALPACA_AUTHENTICATED_READ_ENDPOINTS: Mapping[
    str, AuthenticatedReadEndpointRule
] = MappingProxyType(
    {
        "/v2/account/activities/FILL": AuthenticatedReadEndpointRule(
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


def _alpaca_query_instant(
    value: object,
    *,
    name: str,
    allow_date: bool,
) -> str:
    text = _canonical_text(value, name=name)
    if allow_date and len(text) == 10:
        try:
            parsed_date = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as error:
            raise ProviderTransportScopeError(
                f"Alpaca {name} must be canonical YYYY-MM-DD or UTC timestamp"
            ) from error
        if parsed_date.strftime("%Y-%m-%d") == text:
            return text
    if not text.endswith("Z"):
        raise ProviderTransportScopeError(
            f"Alpaca {name} must be a canonical UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ProviderTransportScopeError(
            f"Alpaca {name} must be a canonical UTC timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderTransportScopeError(
            f"Alpaca {name} must include UTC timezone"
        )
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ProviderTransportScopeError(
            f"Alpaca {name} must be canonical UTC text"
        )
    return text


def _validate_alpaca_authenticated_read_query(
    binding: AuthenticatedReadQueryBinding,
) -> None:
    allowed = frozenset(
        {
            "order_id",
            "date",
            "until",
            "after",
            "direction",
            "page_size",
            "page_token",
        }
    )
    unsupported = set(binding.query) - allowed
    if unsupported:
        raise ProviderTransportScopeError(
            "Alpaca authenticated-read query contains unsupported fields: "
            + ",".join(sorted(unsupported))
        )

    # Production reconciliation must choose an explicit finite page budget.
    raw_page_size = binding.query.get("page_size")
    if raw_page_size is None:
        raise ProviderTransportScopeError(
            "Alpaca authenticated-read query requires explicit page_size"
        )
    try:
        page_size = int(raw_page_size, 10)
    except ValueError as error:
        raise ProviderTransportScopeError(
            "Alpaca page_size must be canonical integer text"
        ) from error
    if str(page_size) != raw_page_size or not 1 <= page_size <= 100:
        raise ProviderTransportScopeError(
            "Alpaca page_size must be a canonical integer from 1 through 100"
        )

    direction = binding.query.get("direction")
    if direction is not None and direction not in {"asc", "desc"}:
        raise ProviderTransportScopeError(
            "Alpaca direction must be asc or desc"
        )

    order_id = binding.query.get("order_id")
    if order_id is not None:
        canonical_order_id = _canonical_text(order_id, name="order_id")
        try:
            parsed_order_id = UUID(canonical_order_id)
        except ValueError as error:
            raise ProviderTransportScopeError(
                "Alpaca order_id must be a canonical UUID"
            ) from error
        if str(parsed_order_id) != canonical_order_id:
            raise ProviderTransportScopeError(
                "Alpaca order_id must be a canonical lowercase UUID"
            )

    if "date" in binding.query:
        _alpaca_query_instant(
            binding.query["date"],
            name="date",
            allow_date=True,
        )
    for field in ("after", "until"):
        if field in binding.query:
            _alpaca_query_instant(
                binding.query[field],
                name=field,
                allow_date=False,
            )

    page_token = binding.query.get("page_token")
    if page_token is not None:
        token = _canonical_text(page_token, name="page_token")
        if (
            len(token) > 512
            or any(character.isspace() for character in token)
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
        ):
            raise ProviderTransportScopeError(
                "Alpaca page_token is outside the canonical resource envelope"
            )


def _alpaca_authenticated_read_rule(
    binding: AuthenticatedReadQueryBinding,
) -> AuthenticatedReadEndpointRule:
    rule = ALPACA_AUTHENTICATED_READ_ENDPOINTS.get(binding.endpoint)
    if rule is None:
        raise ProviderTransportScopeError(
            "Alpaca authenticated-read endpoint is not explicitly allowed"
        )
    if binding.surface != rule.surface:
        raise ProviderTransportScopeError(
            "authenticated-read endpoint surface does not match Alpaca policy"
        )
    if binding.permission_scope != rule.permission_scope:
        raise ProviderTransportScopeError(
            "authenticated-read permission scope does not match Alpaca endpoint policy"
        )
    _validate_alpaca_authenticated_read_query(binding)
    return rule


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
class AuthenticatedReadRouteAuthority:
    """Versioned exact semantic identity of one canonical authenticated-read route."""

    schema_version: str
    provider_id: str
    environment: str
    provider_environment: str
    endpoint: str
    surface: Surface
    permission_scope: str
    data_entitlement: str
    success_statuses: tuple[int, ...]
    network_policy_identity: str
    route_identity: str


def resolve_authenticated_read_route_authority(
    binding: AuthenticatedReadQueryBinding,
) -> AuthenticatedReadRouteAuthority:
    """Resolve exact current endpoint semantics from the production route registry."""

    if type(binding) is not AuthenticatedReadQueryBinding:
        raise TypeError("binding must be exact AuthenticatedReadQueryBinding")

    if binding.provider_id == "ALPACA":
        policy = ALPACA_ENDPOINT_POLICIES.get(binding.provider_environment)
        if policy is None or policy.environment != binding.environment:
            raise ProviderTransportScopeError(
                "Alpaca authenticated-read provider environment is not canonical"
            )
        rule = _alpaca_authenticated_read_rule(binding)
    elif binding.provider_id == "BYBIT":
        policy = BYBIT_V5_ENDPOINT_POLICIES.get(binding.provider_environment)
        if policy is None or policy.environment != binding.environment:
            raise ProviderTransportScopeError(
                "Bybit authenticated-read provider environment is not canonical"
            )
        rule = _bybit_authenticated_read_rule(binding)
    elif binding.provider_id == "BINANCE":
        policy = BINANCE_SPOT_ENDPOINT_POLICIES.get(binding.provider_environment)
        if policy is None or policy.environment != binding.environment:
            raise ProviderTransportScopeError(
                "Binance authenticated-read provider environment is not canonical"
            )
        rule = _binance_authenticated_read_rule(binding)
    elif binding.provider_id == "KRAKEN":
        policy = KRAKEN_SPOT_ENDPOINT_POLICIES.get(binding.provider_environment)
        if policy is None or policy.environment != binding.environment:
            raise ProviderTransportScopeError(
                "Kraken Spot authenticated-read provider environment is not canonical"
            )
        rule = _kraken_spot_authenticated_read_rule(binding)
    else:
        raise ProviderTransportScopeError(
            "provider has no canonical authenticated-read route registry"
        )

    statuses = tuple(sorted(rule.success_statuses))
    material = {
        "schema_version": "authenticated-read-route:v1",
        "provider_id": binding.provider_id,
        "environment": binding.environment,
        "provider_environment": binding.provider_environment,
        "endpoint": binding.endpoint,
        "surface": rule.surface.value,
        "permission_scope": rule.permission_scope,
        "data_entitlement": rule.data_entitlement,
        "success_statuses": list(statuses),
        "network_policy_identity": _provider_network_policy_identity(policy),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return AuthenticatedReadRouteAuthority(
        schema_version=material["schema_version"],
        provider_id=binding.provider_id,
        environment=binding.environment,
        provider_environment=binding.provider_environment,
        endpoint=binding.endpoint,
        surface=rule.surface,
        permission_scope=rule.permission_scope,
        data_entitlement=rule.data_entitlement,
        success_statuses=statuses,
        network_policy_identity=material["network_policy_identity"],
        route_identity="sha256:" + sha256(encoded).hexdigest(),
    )


def canonical_authenticated_read_route(
    binding: AuthenticatedReadQueryBinding,
) -> Mapping[str, object]:
    """Compatibility mapping over the single canonical route authority."""

    route = resolve_authenticated_read_route_authority(binding)
    return MappingProxyType(
        {
            "schema_version": route.schema_version,
            "provider_id": route.provider_id,
            "environment": route.environment,
            "provider_environment": route.provider_environment,
            "surface": route.surface.value,
            "endpoint": route.endpoint,
            "permission_scope": route.permission_scope,
            "data_entitlement": route.data_entitlement,
            "success_statuses": list(route.success_statuses),
            "network_policy_identity": route.network_policy_identity,
            "route_digest": route.route_identity,
        }
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
    """One immutable authenticated provider read request.

    GET keeps the exact signed-query contract used by Binance. POST supports
    providers such as Kraken whose private read APIs authenticate a form body.
    The envelope remains separate from SignedHttpRequest so read responses keep
    their typed observation lifecycle and never acquire write authority.
    """

    url: str
    headers: Mapping[str, str]
    timeout_seconds: int
    method: str = "GET"
    body: bytes = b""

    def __post_init__(self) -> None:
        method = _text(self.method, name="method").upper()
        if method not in {"GET", "POST"}:
            raise ProviderTransportScopeError(
                "authenticated-read method must be GET or POST"
            )
        parsed = urlsplit(_text(self.url, name="url"))
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
            if not parsed.query or self.body:
                raise ProviderTransportScopeError(
                    "authenticated GET requires an exact signed query and no body"
                )
        elif parsed.query or not self.body:
            raise ProviderTransportScopeError(
                "authenticated POST requires an exact body and no URL query"
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
            require_provider_response_bytes(
                self.body,
                max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
                allow_empty=True,
            )
        except (TypeError, ValueError) as error:
            raise ProviderTransportError("invalid or oversized authenticated-read response") from error


@dataclass(frozen=True, slots=True)
class AuthenticatedReadProductWireReceipt:
    """Exact raw response proven to come from one factory-issued product wire send."""

    receipt_id: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    capability_snapshot_id: str
    query_digest: str
    transport_identity: str
    observed_at: str
    http_status: int
    response_sha256: str
    response_bytes: bytes


class _AuthenticatedReadCaptureWire:
    """Tee the exact canonical product wire without accepting caller response bytes."""

    def __init__(self, wire: ProviderWireClient, state: dict[str, object]) -> None:
        self._wire = wire
        self._state = state

    def send(
        self,
        request: "SignedHttpRequest | AuthenticatedReadHttpRequest",
    ) -> "bytes | TradingWireResponse | AuthenticatedReadWireResponse":
        if type(request) is not AuthenticatedReadHttpRequest:
            raise ProviderTransportScopeError(
                "product authenticated-read capture requires exact read request"
            )
        if self._state.get("response") is not None:
            raise ProviderTransportScopeError(
                "product authenticated-read capture observed more than one send"
            )
        response = self._wire.send(request)
        if type(response) is not AuthenticatedReadWireResponse:
            raise ProviderTransportError(
                "product authenticated-read wire did not preserve exact HTTP response"
            )
        self._state["response"] = response
        return response


@contextmanager
def _capture_product_authenticated_read(transport: object):
    if getattr(
        _PRODUCT_AUTHENTICATED_READ_CAPTURE_CONTEXT,
        "state",
        None,
    ) is not None:
        raise ProviderTransportScopeError(
            "nested product authenticated-read capture is forbidden"
        )
    state: dict[str, object] = {"transport": transport, "response": None}
    _PRODUCT_AUTHENTICATED_READ_CAPTURE_CONTEXT.state = state
    try:
        yield state
    finally:
        try:
            delattr(_PRODUCT_AUTHENTICATED_READ_CAPTURE_CONTEXT, "state")
        except AttributeError:
            pass


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
            data = request.body or None
            method = request.method
        else:
            data = request.body or None
            method = request.method
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
                timeout=request.timeout_seconds,
            ) as response:
                http_status = int(response.status)
                raw = self._bounded_body(
                    response.read(response_budget + 1),
                    max_bytes=response_budget,
                )
        except HTTPError as error:
            # An HTTPError retains its request URL and sometimes provider
            # headers, including signed read-query/credential material.
            # Read at most one bounded body here, but NEVER raise or construct
            # typed responses while the secret-bearing exception is active:
            # implicit __context__/explicit __cause__ would expose it later.
            try:
                observed_status = error.code
                if type(observed_status) is int and 100 <= observed_status <= 599:
                    http_error_status = observed_status
                else:
                    http_error_invalid_status = True
            except Exception:
                http_error_invalid_status = True
            if http_error_status is not None and not 300 <= http_error_status < 400:
                try:
                    raw = error.read(response_budget + 1)
                except Exception:
                    http_error_read_failed = True
        except URLError:
            # urllib's transport exception can retain request metadata too.
            # The guarded caller already handles uncertainty after SEND.
            transport_unavailable = True

        # Only primitive, detached status/bytes/flags cross the exception
        # boundary. New failures are generated OUTSIDE urllib exception scope,
        # so their public context chain cannot contain the signed HTTPError.
        if transport_unavailable:
            raise ProviderTransportError("provider HTTP transport response unavailable")
        if http_error_invalid_status:
            raise ProviderTransportError("provider HTTP error status invalid")
        if http_error_status is not None:
            if 300 <= http_error_status < 400:
                raise ProviderTransportError("provider redirect is prohibited")
            if http_error_read_failed:
                raise ProviderTransportError("provider HTTP error body unavailable")
            raw = self._bounded_body(raw, max_bytes=response_budget)
            if is_authenticated_read:
                return AuthenticatedReadWireResponse(
                    http_status=http_error_status,
                    body=raw,
                )
            return TradingWireResponse(
                http_status=http_error_status,
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
    if type(value) is TradingWireResponse:
        # Frozen dataclasses can still be built without __init__ or modified
        # through object.__setattr__. Revalidate the nested HTTP status at
        # the actual post-SEND authority boundary, before virtual comparisons.
        status = value.http_status
        if type(status) is not int or not 100 <= status <= 599:
            raise ProviderTransportError("invalid trading HTTP response status")
        try:
            raw = require_provider_response_bytes(value.body, max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES)
        except (TypeError, ValueError) as error:
            raise ProviderTransportError("invalid or oversized trading response") from error
        return ExactJsonTransportResponse(
            raw,
            http_status=status,
        )
    if type(value) is bytes:
        try:
            raw = require_provider_response_bytes(value, max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES)
        except (TypeError, ValueError) as error:
            raise ProviderTransportError("invalid or oversized trading response") from error
        return ExactJsonTransportResponse(raw)
    raise ProviderTransportError(
        "trading wire client returned an unsupported response contract"
    )


def _binance_exact_trading_response(
    value: object,
) -> ExactJsonTransportResponse | ExactOpaqueTransportResponse:
    """Conservatively classify Binance Spot order-send execution uncertainty.

    Binance documents that 5xx does NOT mean the matching engine rejected the
    order. It also identifies -1007 as execution-status-unknown. Preserve the
    exact status and bounded response bytes for reconciliation; NEVER blindly
    retry after GuardedDispatcher's irreversible send barrier. A 5xx body need
    not be JSON and may be empty, so that transport fact uses the opaque exact
    evidence contract instead of fabricating JSON.
    """
    if type(value) is TradingWireResponse:
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
        if 500 <= status <= 599:
            if raw:
                try:
                    exact = ExactJsonTransportResponse(raw, http_status=status)
                except ValueError:
                    exact = None
                if exact is not None:
                    return ExactJsonTransportResponse(
                        exact.response_bytes,
                        http_status=status,
                        requires_reconciliation=True,
                        ambiguity_reason="binance_spot_http_5xx_execution_unknown",
                    )
            return ExactOpaqueTransportResponse(
                raw,
                http_status=status,
                ambiguity_reason="binance_spot_http_5xx_execution_unknown",
            )

    exact = _exact_trading_response(value)
    status = exact.http_status
    parsed = exact.payload
    if status is not None and 500 <= status <= 599:
        return ExactJsonTransportResponse(
            exact.response_bytes,
            http_status=status,
            requires_reconciliation=True,
            ambiguity_reason="binance_spot_http_5xx_execution_unknown",
        )
    if (
        type(parsed) is dict
        and type(parsed.get("code")) is int
        and parsed["code"] == -1007
    ):
        return ExactJsonTransportResponse(
            exact.response_bytes,
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

    exact = _exact_trading_response(value)
    if exact.http_status is None:
        return exact
    decision = classify_whitebit_http_retry(
        status_code=exact.http_status,
        attempt=1,
        request_class="WRITE",
    )
    if not decision.requires_reconciliation:
        return exact
    return ExactJsonTransportResponse(
        exact.response_bytes,
        http_status=exact.http_status,
        requires_reconciliation=True,
        ambiguity_reason="whitebit_" + decision.classification.lower(),
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
        journal_identity = require_exact_journal_store_authority(
            journal,
            subject="provider nonce journal",
        )
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
        self._journal_identity = journal_identity
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

    def _require_journal_authority(self) -> JournalStore:
        current = require_exact_journal_store_authority(
            self.journal,
            subject=f"{self.display_name} nonce journal",
        )
        if current != self._journal_identity:
            raise ProviderTransportScopeError(
                f"{self.display_name} nonce journal authority changed"
            )
        return self.journal

    def _history(self) -> tuple[int, int]:
        journal = self._require_journal_authority()
        events = journal.load_events(self.AGGREGATE_TYPE, self.aggregate_id)
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
            journal = self._require_journal_authority()
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
                self._require_journal_authority()
                result = journal.append_event(envelope)
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
        self._require_journal_authority()
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
        journal_identity = require_exact_journal_store_authority(
            journal,
            subject="WhiteBIT nonce journal",
        )
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
        self._journal_identity = journal_identity
        self.account_id = account
        self.environment = env
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.max_contention_retries = max_contention_retries
        self.legacy_nonce_floor = self._load_legacy_nonce_floor()

    def _require_journal_authority(self) -> JournalStore:
        current = require_exact_journal_store_authority(
            self.journal,
            subject="WhiteBIT nonce journal",
        )
        if current != self._journal_identity:
            raise ProviderTransportScopeError(
                "WhiteBIT nonce journal authority changed"
            )
        return self.journal

    def _load_legacy_nonce_floor(self) -> int:
        """Carry integrity-valid pre-API-key WhiteBIT nonce history forward."""

        journal = self._require_journal_authority()
        highest = 0
        previous_version = 0
        previous_nonce = 0
        expected_aggregate_id = (
            "WHITEBIT:"
            + sha256(
                f"{self.account_id}|{self.environment}".encode("utf-8")
            ).hexdigest()
        )
        for event in journal.load_events_by_aggregate_type(
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
        journal = self._require_journal_authority()
        return _DurableProviderNonceAllocator(
            provider_id="WHITEBIT",
            display_name="WhiteBIT",
            journal=journal,
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
        journal_identity = require_exact_journal_store_authority(
            journal,
            subject="Kraken Spot nonce journal",
        )
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
            or credential_handle.provider_environment != env
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
        self._journal_identity = journal_identity
        self.account_id = account
        self.environment = env
        self.credential_handle_id = credential_handle.handle_id
        self.credential_generation = credential_handle.generation
        self.clock_millis = clock_millis
        self.clock_utc = clock_utc
        self.max_contention_retries = max_contention_retries
        self.legacy_nonce_floor = self._load_legacy_nonce_floor()

    def _require_journal_authority(self) -> JournalStore:
        current = require_exact_journal_store_authority(
            self.journal,
            subject="Kraken Spot nonce journal",
        )
        if current != self._journal_identity:
            raise ProviderTransportScopeError(
                "Kraken Spot nonce journal authority changed"
            )
        return self.journal

    def _load_legacy_nonce_floor(self) -> int:
        """Carry only integrity-valid pre-provider-key Kraken history forward."""

        journal = self._require_journal_authority()
        highest = 0
        legacy_state: dict[str, tuple[tuple[str, int], int, int]] = {}
        for event in journal.load_events_by_aggregate_type(
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
        journal = self._require_journal_authority()
        return _DurableProviderNonceAllocator(
            provider_id="KRAKEN",
            display_name="Kraken Spot",
            journal=journal,
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

class WhiteBitHttpTransport(_CredentialWireBoundTransport):
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
            or credential_handle.provider_environment != "LIVE"
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)

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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="WHITEBIT",
            environment="LIVE",
            provider_environment="LIVE",
            purpose="TRADE",
        )
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
            wire_response = _credential_wire_authority(self)[1].send(signed)
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
        if canonical_policy is not None:
            try:
                _require_canonical_product_policy_state(canonical_policy)
            except ProviderTransportScopeError as error:
                raise ProviderTransportScopeError(
                    "Kraken Futures canonical endpoint policy changed"
                ) from error
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
        if policy.provider_id != "KRAKEN" or policy.environment != "LIVE":
            raise ProviderTransportScopeError(
                "Kraken Spot authenticated-read signer requires KRAKEN LIVE policy"
            )
        if (
            query_binding.provider_id != policy.provider_id
            or query_binding.environment != policy.environment
            or query_binding.provider_environment != policy.environment
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


class KrakenSpotHttpTransport(_CredentialWireBoundTransport):
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
        selected_policy = _select_canonical_kraken_spot_live_policy(
            policy,
            subject="Kraken Spot order transport",
        )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "KRAKEN"
            or credential_handle.environment != "LIVE"
            or credential_handle.provider_environment != "LIVE"
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

        self.policy = selected_policy
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)

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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="KRAKEN",
            environment="LIVE",
            provider_environment="LIVE",
            purpose="TRADE",
        )
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
                wire_response = _credential_wire_authority(self)[1].send(signed)
                exact = _exact_trading_response(wire_response)
                if spot_submission_requires_reconciliation(exact.payload):
                    return ExactJsonTransportResponse(
                        exact.response_bytes,
                        http_status=exact.http_status,
                        requires_reconciliation=True,
                        ambiguity_reason="kraken_spot_deadline_elapsed",
                    )
                return exact
        finally:
            provider_api_key = None
            credential_plaintext = None



class KrakenSpotAuthenticatedReadTransport(_CredentialWireBoundTransport):
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
        selected_policy = _select_canonical_kraken_spot_live_policy(
            policy,
            subject="Kraken Spot authenticated-read transport",
        )
        if not isinstance(credential_handle, PersistentCredentialHandle):
            raise TypeError(
                "credential_handle must be PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != "KRAKEN"
            or credential_handle.environment != "LIVE"
            or credential_handle.provider_environment != "LIVE"
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

        self.policy = selected_policy
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)

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
            or query_binding.provider_environment != "LIVE"
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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="KRAKEN",
            environment="LIVE",
            provider_environment="LIVE",
            purpose="READ",
        )
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
                _require_product_authenticated_read_authority(self, query_binding)
                wire_response = _credential_wire_authority(self)[1].send(signed)
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


class AlpacaAuthenticatedReadSigner:
    """Pure Alpaca Trading API authenticated-GET signer.

    Alpaca uses static key/secret request headers rather than an HMAC query
    signature. Query bytes remain canonical and are exactly the bytes sent.
    """

    @staticmethod
    def sign(
        *,
        policy: ProviderEndpointPolicy,
        query_binding: AuthenticatedReadQueryBinding,
        credential_plaintext: object,
    ) -> AuthenticatedReadHttpRequest:
        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError(
                "query_binding must be exact AuthenticatedReadQueryBinding"
            )
        if policy.provider_id != "ALPACA":
            raise ProviderTransportScopeError(
                "Alpaca authenticated-read signer requires ALPACA policy"
            )
        if (
            query_binding.provider_id != policy.provider_id
            or query_binding.environment != policy.environment
            or query_binding.provider_environment != policy.environment
        ):
            raise ProviderTransportScopeError(
                "authenticated-read binding provider/environment mismatch"
            )
        _alpaca_authenticated_read_rule(query_binding)
        credential = AlpacaTradingCredential.parse(credential_plaintext)
        exact_query = urlencode(sorted(query_binding.query.items()))
        return AuthenticatedReadHttpRequest(
            url=policy.absolute_url(query_binding.endpoint) + "?" + exact_query,
            headers=MappingProxyType(
                {
                    "Accept": "application/json",
                    "APCA-API-KEY-ID": credential.api_key,
                    "APCA-API-SECRET-KEY": credential.api_secret,
                }
            ),
            timeout_seconds=policy.timeout_seconds,
        )


class AlpacaAuthenticatedReadTransport(_CredentialWireBoundTransport):
    """One-shot Alpaca account-activity read on the shared provider I/O TCB.

    This returns the existing exact-byte ProviderResponseObservation. It does
    not itself mint durable PROVIDER_ORIGIN financial authority; #652's durable
    issuer composes above this exact network/query boundary.
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
        if policy.provider_id != "ALPACA":
            raise ProviderTransportScopeError(
                "Alpaca authenticated-read transport requires ALPACA policy"
            )
        if type(credential_handle) is not PersistentCredentialHandle:
            raise TypeError(
                "credential_handle must be exact PersistentCredentialHandle"
            )
        if (
            credential_handle.provider != policy.provider_id
            or credential_handle.environment != policy.environment
            or credential_handle.provider_environment != policy.environment
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
        self.clock_utc = clock_utc
        self.quota_gate = quota_gate
        self._bind_wire_client(
            secret_resolver=secret_resolver,
            wire_client=wire_client,
        )

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
                "clock_utc must return an exact timezone-aware datetime"
            )
        point = point.astimezone(timezone.utc)
        try:
            current = self.capability_registry.require_verified(
                provider_id=self.policy.provider_id,
                account_id=self.account_id,
                entity_id=query_binding.entity_id,
                environment=self.policy.environment,
                provider_environment=query_binding.provider_environment,
                instrument_version=query_binding.instrument_version,
                at=point,
            )
        except Exception as error:
            raise ProviderTransportScopeError(
                "authenticated-read current capability cannot be verified"
            ) from error
        if type(current) is not CapabilitySnapshot:
            raise ProviderTransportScopeError(
                "capability registry must return exact CapabilitySnapshot"
            )
        if (
            current.snapshot_id != self.capability_snapshot_id
            or current.provider_id != self.policy.provider_id
            or current.account_id != self.account_id
            or current.entity_id != query_binding.entity_id
            or current.environment != self.policy.environment
            or current.provider_environment != query_binding.provider_environment
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
        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError(
                "query_binding must be exact AuthenticatedReadQueryBinding"
            )
        if (
            query_binding.provider_id != self.policy.provider_id
            or query_binding.account_id != self.account_id
            or query_binding.environment != self.policy.environment
            or query_binding.provider_environment != self.policy.environment
            or query_binding.capability_snapshot_id != self.capability_snapshot_id
        ):
            raise ProviderTransportScopeError(
                "authenticated-read query scope mismatch"
            )
        rule = _alpaca_authenticated_read_rule(query_binding)

        if self.quota_gate is not None:
            self.quota_gate(
                self.policy.provider_id,
                self.account_id,
                self.policy.environment,
                "AUTHENTICATED_READ",
            )

        self._require_current_capability(query_binding, rule)
        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            provider_environment=self.policy.environment,
            purpose="READ",
        )
        try:
            request = AlpacaAuthenticatedReadSigner.sign(
                policy=self.policy,
                query_binding=query_binding,
                credential_plaintext=credential_plaintext,
            )
        finally:
            credential_plaintext = None

        # Re-resolve current C after credential access and immediately before
        # the only irreversible network read.
        self._require_current_capability(query_binding, rule)
        _require_product_authenticated_read_authority(self, query_binding)
        wire_response = _credential_wire_authority(self)[1].send(request)
        if type(wire_response) is not AuthenticatedReadWireResponse:
            raise ProviderTransportError(
                "authenticated-read wire client must preserve exact HTTP status"
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


class AlpacaTradingHttpTransport(_CredentialWireBoundTransport):
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
            or credential_handle.provider_environment != policy.environment
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)

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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            provider_environment=self.policy.environment,
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
        wire_response = _credential_wire_authority(self)[1].send(signed)
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


class BybitV5HttpTransport(_CredentialWireBoundTransport):
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)
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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="BYBIT",
            environment=self.policy.environment,
            provider_environment=self.provider_environment,
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
        # Shared production urllib returns typed status+body, while legacy
        # injected diagnostic wire clients may return exact raw bytes.
        wire_response = _credential_wire_authority(self)[1].send(signed)
        return _exact_trading_response(wire_response)


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
            or BYBIT_V5_ENDPOINT_POLICIES.get(
                query_binding.provider_environment
            ) != policy
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


class BybitV5AuthenticatedReadTransport(_CredentialWireBoundTransport):
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)
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
            or current.provider_environment != self.provider_environment
            or current.provider_environment != query_binding.provider_environment
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
            or query_binding.provider_environment != self.provider_environment
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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider="BYBIT",
            environment=self.policy.environment,
            provider_environment=self.provider_environment,
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
        _require_product_authenticated_read_authority(self, query_binding)
        wire_response = _credential_wire_authority(self)[1].send(signed)
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


class BinanceSpotHttpTransport(_CredentialWireBoundTransport):
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
            or credential_handle.provider_environment != policy.environment
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)
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
        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            provider_environment=self.policy.environment,
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
        wire_response = _credential_wire_authority(self)[1].send(signed)
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
        if policy.provider_id != "BINANCE":
            raise ProviderTransportScopeError(
                "Binance authenticated-read signer requires BINANCE policy"
            )
        if (
            query_binding.provider_id != policy.provider_id
            or query_binding.environment != policy.environment
            or query_binding.provider_environment != policy.environment
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


class BinanceSpotAuthenticatedReadTransport(_CredentialWireBoundTransport):
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
            or credential_handle.provider_environment != policy.environment
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
        self._bind_wire_client(secret_resolver=secret_resolver, wire_client=wire_client)
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
            or query_binding.provider_environment != self.policy.environment
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

        credential_authority, _ = _credential_wire_authority(self)
        credential_plaintext = credential_authority.resolve_for_execution(
            self.session_token,
            origin=self.origin,
            handle=self.credential_handle,
            execution_identity=self.execution_identity,
            account_id=self.account_id,
            provider=self.policy.provider_id,
            environment=self.policy.environment,
            provider_environment=self.policy.environment,
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
        _require_product_authenticated_read_authority(self, query_binding)
        wire_response = _credential_wire_authority(self)[1].send(signed)
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

def _product_authenticated_read_transport_types() -> tuple[type, ...]:
    return (
        KrakenSpotAuthenticatedReadTransport,
        AlpacaAuthenticatedReadTransport,
        BybitV5AuthenticatedReadTransport,
        BinanceSpotAuthenticatedReadTransport,
    )


def _product_authenticated_read_transport_identity(
    transport: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> str:
    composition = _product_credential_wire_composition(transport)
    if composition is None:
        raise ProviderTransportScopeError(
            "authenticated-read provider origin requires factory-issued product transport"
        )
    value_scope = dict(composition.value_scope)
    provider_environment = value_scope.get(
        "provider_environment",
        composition.policy_state[1],
    )
    if (
        composition.policy_state[0] != query_binding.provider_id
        or composition.policy_state[1] != query_binding.environment
        or value_scope.get("account_id") != query_binding.account_id
        or value_scope.get("capability_snapshot_id")
        != query_binding.capability_snapshot_id
        or provider_environment != query_binding.provider_environment
    ):
        raise ProviderTransportScopeError(
            "factory-issued transport does not match authenticated-read query scope"
        )
    material = {
        "schema_version": "product-authenticated-read-transport:v1",
        "transport_type": (
            composition.transport_type.__module__
            + "."
            + composition.transport_type.__qualname__
        ),
        "provider_id": query_binding.provider_id,
        "account_id": query_binding.account_id,
        "environment": query_binding.environment,
        "provider_environment": query_binding.provider_environment,
        "capability_snapshot_id": query_binding.capability_snapshot_id,
        "query_digest": query_binding.query_digest,
        "policy_state": [
            composition.policy_state[0],
            composition.policy_state[1],
            composition.policy_state[2],
            sorted(composition.policy_state[3]),
            composition.policy_state[4],
        ],
        "credential_handle_state": list(composition.credential_handle_state),
        "execution_identity": object.__getattribute__(
            transport,
            "execution_identity",
        ),
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return "sha256:" + sha256(encoded).hexdigest()


def product_authenticated_read_transport_identity(
    transport: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> str:
    """Resolve the exact factory-issued transport composition identity before I/O."""

    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError(
            "query_binding must be exact AuthenticatedReadQueryBinding"
        )
    if type(transport) not in _product_authenticated_read_transport_types():
        raise TypeError(
            "transport must be exact supported authenticated-read transport"
        )
    return _product_authenticated_read_transport_identity(
        transport,
        query_binding,
    )


def product_authenticated_read_prepared_authority(
    transport: object,
    query_binding: AuthenticatedReadQueryBinding,
) -> Mapping[str, object]:
    """Rederive exact current product read authority before durable Prepared.

    The query binding is treated as a structural request only.  Authority comes
    from the factory-issued transport composition, its canonical current
    CapabilityRegistry, the current endpoint-rule registry, the exact credential
    handle generation and the transport-owned UTC clock.
    """

    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError(
            "query_binding must be exact AuthenticatedReadQueryBinding"
        )
    if type(transport) not in _product_authenticated_read_transport_types():
        raise TypeError(
            "transport must be exact supported authenticated-read transport"
        )
    composition = _product_credential_wire_composition(transport)
    if composition is None:
        raise ProviderTransportScopeError(
            "authenticated-read prepared authority requires factory-issued product transport"
        )
    transport_identity = _product_authenticated_read_transport_identity(
        transport,
        query_binding,
    )
    route = resolve_authenticated_read_route_authority(query_binding)
    values = dict(composition.value_scope)
    provider_id = composition.policy_state[0]
    environment = composition.policy_state[1]
    account_id = values.get("account_id")
    capability_snapshot_id = values.get("capability_snapshot_id")
    provider_environment = values.get(
        "provider_environment",
        environment,
    )
    registry = object.__getattribute__(transport, "capability_registry")
    if type(registry) is not CapabilityRegistry:
        raise ProviderTransportScopeError(
            "product authenticated-read capability registry is not canonical"
        )
    clock = object.__getattribute__(transport, "clock_utc")
    if not callable(clock):
        raise ProviderTransportScopeError(
            "product authenticated-read UTC clock is unavailable"
        )
    point = clock()
    if (
        type(point) is not datetime
        or point.tzinfo is None
        or point.utcoffset() is None
    ):
        raise ProviderTransportScopeError(
            "product authenticated-read UTC clock must return exact aware datetime"
        )
    point = point.astimezone(timezone.utc)
    try:
        current = registry.require_verified(
            provider_id=provider_id,
            account_id=account_id,
            entity_id=query_binding.entity_id,
            environment=environment,
            provider_environment=provider_environment,
            instrument_version=query_binding.instrument_version,
            at=point,
        )
    except (CapabilityError, TypeError, ValueError) as error:
        raise ProviderTransportScopeError(
            "authenticated-read current capability cannot rederive requested scope"
        ) from error
    if (
        type(current) is not CapabilitySnapshot
        or current.snapshot_id != capability_snapshot_id
        or current.snapshot_id != query_binding.capability_snapshot_id
        or current.provider_id != provider_id
        or current.provider_id != query_binding.provider_id
        or current.account_id != account_id
        or current.account_id != query_binding.account_id
        or current.entity_id != query_binding.entity_id
        or current.environment != environment
        or current.environment != query_binding.environment
        or current.provider_environment != provider_environment
        or current.provider_environment != query_binding.provider_environment
        or current.instrument_version != query_binding.instrument_version
        or query_binding.permission_scope not in current.permission_scopes
        or route.data_entitlement not in current.data_entitlements
    ):
        raise ProviderTransportScopeError(
            "authenticated-read query does not match rederived current capability authority"
        )
    try:
        query_prepared_at = datetime.fromisoformat(
            query_binding.prepared_at.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise ProviderTransportScopeError(
            "authenticated-read prepared_at is not canonical UTC"
        ) from error
    if (
        query_prepared_at.tzinfo is None
        or not query_binding.prepared_at.endswith("Z")
        or query_prepared_at.astimezone(timezone.utc) > point
    ):
        raise ProviderTransportScopeError(
            "authenticated-read query preparation instant is not admissible at issuer cut"
        )
    credential_state = composition.credential_handle_state
    credential_identity_material = {
        "handle_id": credential_state[0],
        "account_id": credential_state[1],
        "provider": credential_state[2],
        "environment": credential_state[3],
        "purpose": credential_state[4],
    }
    credential_identity = "sha256:" + sha256(
        json.dumps(
            credential_identity_material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    return MappingProxyType(
        {
            "schema_version": "product-authenticated-read-prepared-authority:v1",
            "provider_id": current.provider_id,
            "account_id": current.account_id,
            "entity_id": current.entity_id,
            "environment": current.environment,
            "provider_environment": current.provider_environment,
            "capability_snapshot_id": current.snapshot_id,
            "instrument_version": current.instrument_version,
            "surface": route.surface.value,
            "endpoint": route.endpoint,
            "permission_scope": route.permission_scope,
            "data_entitlement": route.data_entitlement,
            "success_statuses": tuple(route.success_statuses),
            "route_identity": route.route_identity,
            "transport_identity": transport_identity,
            "network_policy_identity": route.network_policy_identity,
            "credential_handle_identity": credential_identity,
            "credential_generation": credential_state[5],
            "validated_at": point.isoformat().replace("+00:00", "Z"),
        }
    )


def _build_product_authenticated_read_receipt_api():
    """Create the product-wire receipt authority without module-exported mutable state."""

    guard = Lock()
    records: dict[str, tuple[int, tuple[object, ...]]] = {}

    def execute_product_authenticated_read(
        transport: object,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> AuthenticatedReadProductWireReceipt:
        """Execute one real factory-issued read and retain its exact raw wire result.

        No response bytes/status can be supplied by the caller. Once the canonical
        product wire has returned, parser/status errors no longer erase the
        definitive provider response; downstream financial promotion still applies
        route/Q/status/parser rules separately.
        """

        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError(
                "query_binding must be exact AuthenticatedReadQueryBinding"
            )
        if type(transport) not in _product_authenticated_read_transport_types():
            raise TypeError(
                "transport must be exact supported authenticated-read transport"
            )
        if _product_credential_wire_composition(transport) is None:
            raise ProviderTransportScopeError(
                "authenticated-read provider origin requires factory-issued product transport"
            )
        route = resolve_authenticated_read_route_authority(query_binding)
        transport_identity = _product_authenticated_read_transport_identity(
            transport,
            query_binding,
        )

        captured: dict[str, object]
        parse_error: Exception | None = None
        with _capture_product_authenticated_read(transport) as captured:
            try:
                transport(query_binding)
            except Exception as error:
                if captured.get("response") is None:
                    raise
                parse_error = error

        response = captured.get("response")
        if type(response) is not AuthenticatedReadWireResponse:
            raise ProviderTransportError(
                "factory-issued authenticated read produced no exact wire response"
            )
        clock = object.__getattribute__(transport, "clock_utc")
        observed = clock()
        if (
            type(observed) is not datetime
            or observed.tzinfo is None
            or observed.utcoffset() is None
        ):
            raise ProviderTransportScopeError(
                "product authenticated-read clock must return exact aware datetime"
            )
        observed_text = (
            observed.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        body = require_provider_response_bytes(
            response.body,
            max_bytes=HARD_MAX_PROVIDER_RESPONSE_BYTES,
            allow_empty=True,
        )
        response_sha256 = "sha256:" + sha256(body).hexdigest()
        material = {
            "schema_version": "product-authenticated-read-receipt:v1",
            "provider_id": query_binding.provider_id,
            "account_id": query_binding.account_id,
            "environment": query_binding.environment,
            "provider_environment": query_binding.provider_environment,
            "capability_snapshot_id": query_binding.capability_snapshot_id,
            "query_digest": query_binding.query_digest,
            "route_identity": route.route_identity,
            "transport_identity": transport_identity,
            "observed_at": observed_text,
            "http_status": response.http_status,
            "response_sha256": response_sha256,
        }
        receipt_id = "product-auth-read:sha256:" + sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        receipt = AuthenticatedReadProductWireReceipt(
            receipt_id=receipt_id,
            provider_id=query_binding.provider_id,
            account_id=query_binding.account_id,
            environment=query_binding.environment,
            provider_environment=query_binding.provider_environment,
            capability_snapshot_id=query_binding.capability_snapshot_id,
            query_digest=query_binding.query_digest,
            transport_identity=transport_identity,
            observed_at=observed_text,
            http_status=response.http_status,
            response_sha256=response_sha256,
            response_bytes=body,
        )
        record = (
            receipt.provider_id,
            receipt.account_id,
            receipt.environment,
            receipt.provider_environment,
            receipt.capability_snapshot_id,
            receipt.query_digest,
            route.route_identity,
            receipt.transport_identity,
            receipt.observed_at,
            receipt.http_status,
            receipt.response_sha256,
            receipt.response_bytes,
        )
        with guard:
            existing = records.get(receipt_id)
            if existing is not None and existing != (id(receipt), record):
                raise ProviderTransportError(
                    "product authenticated-read receipt identity conflict"
                )
            records[receipt_id] = (id(receipt), record)
        # parse_error is intentionally not re-raised: the exact wire fact exists.
        # Financial promotion later re-applies current route/Q/status/parser rules.
        _ = parse_error
        return receipt

    def validate_product_authenticated_read_receipt(
        receipt: AuthenticatedReadProductWireReceipt,
        query_binding: AuthenticatedReadQueryBinding,
    ) -> tuple[str, str, int, bytes, datetime]:
        """Return receipt facts only for the exact closure-issued object."""

        if type(receipt) is not AuthenticatedReadProductWireReceipt:
            raise ProviderTransportScopeError(
                "product wire receipt must be exact AuthenticatedReadProductWireReceipt"
            )
        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError(
                "query_binding must be exact AuthenticatedReadQueryBinding"
            )
        route = resolve_authenticated_read_route_authority(query_binding)
        material = {
            "schema_version": "product-authenticated-read-receipt:v1",
            "provider_id": receipt.provider_id,
            "account_id": receipt.account_id,
            "environment": receipt.environment,
            "provider_environment": receipt.provider_environment,
            "capability_snapshot_id": receipt.capability_snapshot_id,
            "query_digest": receipt.query_digest,
            "route_identity": route.route_identity,
            "transport_identity": receipt.transport_identity,
            "observed_at": receipt.observed_at,
            "http_status": receipt.http_status,
            "response_sha256": receipt.response_sha256,
        }
        expected_receipt_id = "product-auth-read:sha256:" + sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        if receipt.receipt_id != expected_receipt_id:
            raise ProviderTransportScopeError(
                "product authenticated-read receipt identity mismatch"
            )
        record = (
            receipt.provider_id,
            receipt.account_id,
            receipt.environment,
            receipt.provider_environment,
            receipt.capability_snapshot_id,
            receipt.query_digest,
            route.route_identity,
            receipt.transport_identity,
            receipt.observed_at,
            receipt.http_status,
            receipt.response_sha256,
            receipt.response_bytes,
        )
        with guard:
            registered = records.get(receipt.receipt_id)
        if registered != (id(receipt), record):
            raise ProviderTransportScopeError(
                "product authenticated-read receipt lacks canonical wire execution evidence"
            )
        if (
            receipt.provider_id != query_binding.provider_id
            or receipt.account_id != query_binding.account_id
            or receipt.environment != query_binding.environment
            or receipt.provider_environment != query_binding.provider_environment
            or receipt.capability_snapshot_id != query_binding.capability_snapshot_id
            or receipt.query_digest != query_binding.query_digest
        ):
            raise ProviderTransportScopeError(
                "product authenticated-read receipt query scope mismatch"
            )
        if (
            "sha256:" + sha256(receipt.response_bytes).hexdigest()
            != receipt.response_sha256
        ):
            raise ProviderTransportScopeError(
                "product authenticated-read receipt response digest mismatch"
            )
        try:
            point = datetime.fromisoformat(
                receipt.observed_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ProviderTransportScopeError(
                "product authenticated-read receipt timestamp is invalid"
            ) from error
        if (
            point.tzinfo is None
            or not receipt.observed_at.endswith("Z")
            or point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            != receipt.observed_at
        ):
            raise ProviderTransportScopeError(
                "product authenticated-read receipt timestamp is not canonical UTC"
            )
        return (
            receipt.transport_identity,
            route.network_policy_identity,
            receipt.http_status,
            receipt.response_bytes,
            point.astimezone(timezone.utc),
        )

    def retire_product_authenticated_read_receipt(
        receipt: AuthenticatedReadProductWireReceipt,
    ) -> None:
        """Forget one in-process raw receipt after durable origin publication."""

        if type(receipt) is not AuthenticatedReadProductWireReceipt:
            raise ProviderTransportScopeError(
                "product wire receipt must be exact AuthenticatedReadProductWireReceipt"
            )
        with guard:
            existing = records.get(receipt.receipt_id)
            if existing is None or existing[0] != id(receipt):
                raise ProviderTransportScopeError(
                    "product authenticated-read receipt is not current"
                )
            del records[receipt.receipt_id]

    return (
        execute_product_authenticated_read,
        validate_product_authenticated_read_receipt,
        retire_product_authenticated_read_receipt,
    )


(
    execute_product_authenticated_read,
    validate_product_authenticated_read_receipt,
    retire_product_authenticated_read_receipt,
) = _build_product_authenticated_read_receipt_api()

