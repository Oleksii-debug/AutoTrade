"""Terminal qualified Bybit read wire receipt.

The shared provider transport already performs its final legacy capability check
immediately before ``wire_client.send``.  This module supplies that wire client:
it re-resolves the selected route's durable C and Q under one JournalStore cut
at the actual irreversible read boundary, delegates exactly one send, and mints
a sealed receipt over the exact returned status/bytes.

The receipt is the only product authority suitable for durable provider-origin
recording.  Callers cannot construct or relabel one themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import weakref
from urllib.parse import urlencode

from .capabilities import CapabilityError, CapabilitySnapshot
from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_qualification_authority import ProviderQualificationError
from .provider_qualification_current_scope import ProviderQualificationCurrentScope
from .provider_core import ProviderResponseObservation
from .provider_route_financial_binding import (
    build_selected_provider_route_transport_capability_registry,
)
from .provider_route_reads import (
    QualifiedProviderReadQueryBinding,
    _require_qualified_provider_read_binding_authority,
)
from .provider_selection import SelectedProviderRoute
from .provider_transport import (
    AuthenticatedReadHttpRequest,
    AuthenticatedReadWireResponse,
    BYBIT_V5_ENDPOINT_POLICIES,
    BybitV5AuthenticatedReadTransport,
    DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
    UrllibJsonWireClient,
)


class QualifiedProviderReadWireError(PermissionError):
    """Terminal durable C/Q authority or exact wire-receipt validation failed."""


def _point(value: datetime, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise QualifiedProviderReadWireError(
            f"{name} must be an exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _journal_cut(store: object) -> int:
    cut = store.whole_store_state_cut()
    if type(cut) is not dict:
        raise QualifiedProviderReadWireError(
            "whole-store terminal wire cut is non-canonical"
        )
    sequence = cut.get("journal_sequence")
    if type(sequence) is not int or sequence < 0:
        raise QualifiedProviderReadWireError(
            "whole-store terminal wire cut lacks canonical sequence"
        )
    return sequence


def _q_scope(route: SelectedProviderRoute) -> ProviderQualificationCurrentScope:
    q = route.qualification
    return ProviderQualificationCurrentScope(
        provider_scope=q.scope.provider_scope,
        product_family=q.scope.product_family,
        adapter_source_git_sha=q.scope.adapter_source_git_sha,
        packaged_artifact_digest=q.scope.packaged_artifact_digest,
        protocol_id=q.scope.protocol_id,
        protocol_version=q.scope.protocol_version,
    )


def _request_digest(request: AuthenticatedReadHttpRequest) -> str:
    if type(request) is not AuthenticatedReadHttpRequest:
        raise QualifiedProviderReadWireError(
            "terminal qualified read requires exact AuthenticatedReadHttpRequest"
        )
    material = {
        "method": request.method,
        "url": request.url,
        "body_sha256": "sha256:" + sha256(request.body).hexdigest(),
        "timeout_seconds": request.timeout_seconds,
    }
    return "sha256:" + sha256(canonical_json(material).encode("utf-8")).hexdigest()


def _require_request_matches_binding(
    request: AuthenticatedReadHttpRequest,
    *,
    query_binding: QualifiedProviderReadQueryBinding,
) -> None:
    _require_qualified_provider_read_binding_authority(query_binding)
    base = query_binding.query_binding
    policy = BYBIT_V5_ENDPOINT_POLICIES.get(query_binding.provider_environment)
    if policy is None:
        raise QualifiedProviderReadWireError(
            "qualified Bybit read has unsupported provider environment"
        )
    expected_url = (
        policy.absolute_url(base.endpoint)
        + "?"
        + urlencode(sorted(base.query.items()))
    )
    if (
        request.method != "GET"
        or request.body != b""
        or request.url != expected_url
        or request.timeout_seconds != policy.timeout_seconds
    ):
        raise QualifiedProviderReadWireError(
            "terminal signed request differs from exact qualified Bybit query"
        )
    expected_headers = {
        "Accept",
        "X-BAPI-API-KEY",
        "X-BAPI-TIMESTAMP",
        "X-BAPI-RECV-WINDOW",
        "X-BAPI-SIGN",
    }
    headers = request.headers
    if set(headers) != expected_headers or headers.get("Accept") != "application/json":
        raise QualifiedProviderReadWireError(
            "terminal signed request headers are not canonical Bybit V5 auth"
        )
    api_key = headers.get("X-BAPI-API-KEY")
    timestamp = headers.get("X-BAPI-TIMESTAMP")
    recv_window = headers.get("X-BAPI-RECV-WINDOW")
    signature = headers.get("X-BAPI-SIGN")
    if (
        type(api_key) is not str
        or not api_key
        or type(timestamp) is not str
        or not timestamp.isascii()
        or not timestamp.isdigit()
        or type(recv_window) is not str
        or not recv_window.isascii()
        or not recv_window.isdigit()
        or not 1 <= int(recv_window) <= 60000
        or type(signature) is not str
        or len(signature) != 64
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        raise QualifiedProviderReadWireError(
            "terminal signed request auth material is malformed"
        )


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedProviderReadWireReceipt:
    """Sealed proof that one exact qualified binding crossed the wire boundary."""

    query_binding: QualifiedProviderReadQueryBinding
    http_status: int
    response_bytes: bytes
    response_sha256: str
    request_sha256: str
    authority_journal_sequence_cut: int
    authorized_at: datetime
    observed_at: datetime

    def __init__(self, *_args, **_kwargs) -> None:
        raise QualifiedProviderReadWireError(
            "qualified provider-read wire receipts must come from terminal transport"
        )

    @property
    def receipt_id(self) -> str:
        _require_wire_receipt_authority(self)
        material = {
            "qualified_query_digest": self.query_binding.query_digest,
            "qualification_id": self.query_binding.qualification_id,
            "capability_snapshot_id": (
                self.query_binding.query_binding.capability_snapshot_id
            ),
            "request_sha256": self.request_sha256,
            "http_status": self.http_status,
            "response_sha256": self.response_sha256,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
            "authorized_at": self.authorized_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
        }
        return "qualified-provider-wire:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_wire_receipt_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(value: object) -> None:
        if type(value) is not QualifiedProviderReadWireReceipt:
            raise QualifiedProviderReadWireError(
                "wire receipt construction authority requires exact receipt"
            )
        _require_qualified_provider_read_binding_authority(value.query_binding)
        prune()
        object_id = id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            raise QualifiedProviderReadWireError(
                "wire receipt construction authority identity collision"
            )
        states[object_id] = (
            weakref.ref(value),
            (
                value.query_binding,
                value.http_status,
                value.response_bytes,
                value.response_sha256,
                value.request_sha256,
                value.authority_journal_sequence_cut,
                value.authorized_at,
                value.observed_at,
            ),
        )

    def require(value: object) -> None:
        if type(value) is not QualifiedProviderReadWireReceipt:
            raise QualifiedProviderReadWireError(
                "wire receipt authority requires exact receipt"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire receipt authority is unavailable"
            )
        (
            binding,
            status,
            response_bytes,
            response_sha256,
            request_sha256,
            cut,
            authorized_at,
            observed_at,
        ) = state[1]
        if value.query_binding is not binding:
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire receipt changed after send"
            )
        _require_qualified_provider_read_binding_authority(value.query_binding)
        if (
            type(value.http_status) is not int
            or value.http_status != status
            or type(value.response_bytes) is not bytes
            or value.response_bytes != response_bytes
            or type(value.response_sha256) is not str
            or value.response_sha256 != response_sha256
            or type(value.request_sha256) is not str
            or value.request_sha256 != request_sha256
            or type(value.authority_journal_sequence_cut) is not int
            or value.authority_journal_sequence_cut != cut
            or value.authorized_at != authorized_at
            or value.observed_at != observed_at
        ):
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire receipt changed after send"
            )
        if "sha256:" + sha256(value.response_bytes).hexdigest() != value.response_sha256:
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire receipt response digest mismatch"
            )

    return register, require


_register_wire_receipt_authority, _require_wire_receipt_authority = (
    _install_wire_receipt_authority()
)
del _install_wire_receipt_authority


def require_qualified_provider_read_wire_receipt(
    value: QualifiedProviderReadWireReceipt,
) -> QualifiedProviderReadWireReceipt:
    """Public fail-closed verifier for downstream provider-origin code."""

    _require_wire_receipt_authority(value)
    return value


class QualifiedBybitReadWireClient:
    """ProviderWireClient that places durable exact C/Q at terminal read SEND."""

    def __init__(
        self,
        *,
        route: SelectedProviderRoute,
        query_binding: QualifiedProviderReadQueryBinding,
        capability_registry: DurableCapabilityRegistry,
        qualification_registry: DurableProviderQualificationRegistry,
        delegate: UrllibJsonWireClient,
        clock_utc,
    ) -> None:
        if type(route) is not SelectedProviderRoute:
            raise TypeError("route must be exact SelectedProviderRoute")
        if type(query_binding) is not QualifiedProviderReadQueryBinding:
            raise TypeError(
                "query_binding must be exact QualifiedProviderReadQueryBinding"
            )
        _require_qualified_provider_read_binding_authority(query_binding)
        if type(capability_registry) is not DurableCapabilityRegistry:
            raise TypeError(
                "capability_registry must be exact DurableCapabilityRegistry"
            )
        if type(qualification_registry) is not DurableProviderQualificationRegistry:
            raise TypeError(
                "qualification_registry must be exact DurableProviderQualificationRegistry"
            )
        if capability_registry.store is not qualification_registry.store:
            raise QualifiedProviderReadWireError(
                "terminal C/Q authorities must share one exact JournalStore"
            )
        if type(delegate) is not UrllibJsonWireClient:
            raise TypeError(
                "delegate must be exact canonical UrllibJsonWireClient"
            )
        if not callable(clock_utc):
            raise TypeError("clock_utc must be callable")

        candidate = route.candidate
        base = query_binding.query_binding
        exact_route_identity = (
            candidate.provider_id,
            candidate.account_id,
            route.capability.environment,
            candidate.provider_environment,
            candidate.entity_id,
            route.capability.instrument_version,
            route.capability_snapshot_id,
            route.qualification_id,
            candidate.adapter_code_sha,
            candidate.packaged_artifact_digest,
        )
        exact_binding_identity = (
            base.provider_id,
            base.account_id,
            base.environment,
            query_binding.provider_environment,
            base.entity_id,
            base.instrument_version,
            base.capability_snapshot_id,
            query_binding.qualification_id,
            query_binding.adapter_code_sha,
            query_binding.packaged_artifact_digest,
        )
        if exact_route_identity != exact_binding_identity:
            raise QualifiedProviderReadWireError(
                "qualified read binding differs from selected provider route"
            )
        if candidate.provider_id != "BYBIT":
            raise QualifiedProviderReadWireError(
                "QualifiedBybitReadWireClient requires selected BYBIT route"
            )

        self.route = route
        self.query_binding = query_binding
        self.capability_registry = capability_registry
        self.qualification_registry = qualification_registry
        self.delegate = delegate
        self.clock_utc = clock_utc
        self._receipt: QualifiedProviderReadWireReceipt | None = None
        self._used = False

    @property
    def receipt(self) -> QualifiedProviderReadWireReceipt | None:
        if self._receipt is not None:
            _require_wire_receipt_authority(self._receipt)
        return self._receipt

    def _terminal_authority(self) -> tuple[int, datetime]:
        if self._used:
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire client is one-shot"
            )
        point = _point(self.clock_utc(), name="clock_utc")
        store = self.capability_registry.store
        cut = _journal_cut(store)
        candidate = self.route.candidate
        base = self.query_binding.query_binding
        try:
            capability = self.capability_registry.require_verified(
                provider_id=candidate.provider_id,
                account_id=candidate.account_id,
                entity_id=candidate.entity_id,
                environment=self.route.capability.environment,
                provider_environment=candidate.provider_environment,
                instrument_version=self.route.capability.instrument_version,
                at=point,
                journal_sequence_cut=cut,
            )
        except CapabilityError as error:
            raise QualifiedProviderReadWireError(
                "selected route capability is not exact current at terminal wire boundary"
            ) from error
        if (
            type(capability) is not CapabilitySnapshot
            or capability.snapshot_id != self.route.capability_snapshot_id
            or base.permission_scope not in capability.permission_scopes
            or self.query_binding.data_entitlement not in capability.data_entitlements
        ):
            raise QualifiedProviderReadWireError(
                "selected route capability does not authorize exact qualified read at terminal wire boundary"
            )
        try:
            current_q = self.qualification_registry.require_exact_current(
                scope=_q_scope(self.route),
                at=point,
                expected_qualification_id=self.route.qualification_id,
                journal_sequence_cut=cut,
            )
        except ProviderQualificationError as error:
            raise QualifiedProviderReadWireError(
                "selected route qualification is not exact current at terminal wire boundary"
            ) from error
        if current_q.journal_sequence_cut != cut:
            raise QualifiedProviderReadWireError(
                "qualification authority did not honor terminal wire cut"
            )
        if _journal_cut(store) != cut:
            raise QualifiedProviderReadWireError(
                "provider route authority changed during terminal wire guard"
            )
        return cut, point

    def send(
        self,
        request: AuthenticatedReadHttpRequest,
    ) -> AuthenticatedReadWireResponse:
        if type(request) is not AuthenticatedReadHttpRequest:
            raise QualifiedProviderReadWireError(
                "qualified provider read requires exact AuthenticatedReadHttpRequest"
            )
        if self._used:
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire client is one-shot"
            )
        _require_request_matches_binding(
            request,
            query_binding=self.query_binding,
        )
        cut, authorized_at = self._terminal_authority()
        self._used = True
        # Call the canonical class implementation directly. Instance-level
        # replacement of ``send`` cannot become provider-origin authority.
        response = UrllibJsonWireClient.send(self.delegate, request)
        if type(response) is not AuthenticatedReadWireResponse:
            raise QualifiedProviderReadWireError(
                "qualified provider-read wire delegate must preserve exact HTTP status"
            )
        if response.http_status not in self.query_binding.accepted_success_statuses:
            raise QualifiedProviderReadWireError(
                "terminal provider response status is outside qualified endpoint contract"
            )
        observed_at = _point(self.clock_utc(), name="clock_utc")
        if observed_at < authorized_at:
            raise QualifiedProviderReadWireError(
                "terminal provider response clock moved backwards"
            )
        receipt = object.__new__(QualifiedProviderReadWireReceipt)
        values = {
            "query_binding": self.query_binding,
            "http_status": response.http_status,
            "response_bytes": response.body,
            "response_sha256": "sha256:" + sha256(response.body).hexdigest(),
            "request_sha256": _request_digest(request),
            "authority_journal_sequence_cut": cut,
            "authorized_at": authorized_at,
            "observed_at": observed_at,
        }
        for name, value in values.items():
            object.__setattr__(receipt, name, value)
        _register_wire_receipt_authority(receipt)
        self._receipt = receipt
        return response


@dataclass(frozen=True, slots=True)
class QualifiedBybitReadExecution:
    observation: ProviderResponseObservation
    receipt: QualifiedProviderReadWireReceipt

    def __post_init__(self) -> None:
        if type(self.observation) is not ProviderResponseObservation:
            raise TypeError("observation must be exact ProviderResponseObservation")
        _require_wire_receipt_authority(self.receipt)
        if self.observation.query_binding is not self.receipt.query_binding.query_binding:
            raise QualifiedProviderReadWireError(
                "transport observation differs from wire receipt query"
            )
        if (
            self.observation.http_status != self.receipt.http_status
            or self.observation.response_sha256 != self.receipt.response_sha256
        ):
            raise QualifiedProviderReadWireError(
                "transport observation differs from exact wire receipt response"
            )


def execute_qualified_bybit_read(
    *,
    route: SelectedProviderRoute,
    query_binding: QualifiedProviderReadQueryBinding,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
    secret_resolver,
    credential_handle,
    session_token: str,
    origin: str,
    execution_identity: str,
    clock_millis,
    clock_utc,
    quota_gate=None,
    recv_window_ms: int = 5000,
    max_response_bytes: int = DEFAULT_MAX_PROVIDER_RESPONSE_BYTES,
) -> QualifiedBybitReadExecution:
    """Execute one product Bybit read and return its non-self-mintable receipt.

    No caller-selected network client is accepted. The shared Bybit transport
    owns signing/credential handling; this module owns exact durable C/Q at the
    final wire boundary and the canonical urllib client owns the network call.
    """

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(query_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError(
            "query_binding must be exact QualifiedProviderReadQueryBinding"
        )
    candidate = route.candidate
    if candidate.provider_id != "BYBIT":
        raise QualifiedProviderReadWireError(
            "execute_qualified_bybit_read requires selected BYBIT route"
        )
    policy = BYBIT_V5_ENDPOINT_POLICIES.get(candidate.provider_environment)
    if policy is None:
        raise QualifiedProviderReadWireError(
            "selected BYBIT route has unsupported provider environment"
        )
    delegate = UrllibJsonWireClient(max_response_bytes=max_response_bytes)
    terminal = QualifiedBybitReadWireClient(
        route=route,
        query_binding=query_binding,
        capability_registry=capability_registry,
        qualification_registry=qualification_registry,
        delegate=delegate,
        clock_utc=clock_utc,
    )
    transport = BybitV5AuthenticatedReadTransport(
        policy=policy,
        provider_environment=candidate.provider_environment,
        account_id=candidate.account_id,
        capability_snapshot_id=route.capability_snapshot_id,
        capability_registry=build_selected_provider_route_transport_capability_registry(
            route
        ),
        secret_resolver=secret_resolver,
        credential_handle=credential_handle,
        session_token=session_token,
        origin=origin,
        execution_identity=execution_identity,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=terminal,
        recv_window_ms=recv_window_ms,
    )
    observation = transport(query_binding.query_binding)
    receipt = terminal.receipt
    if receipt is None:
        raise QualifiedProviderReadWireError(
            "canonical Bybit transport returned without terminal wire receipt"
        )
    return QualifiedBybitReadExecution(
        observation=observation,
        receipt=receipt,
    )
