"""Source-owned, exact-current authority for provider absence evidence.

This module is deliberately narrower than reconciliation.  It can qualify:

* one direct Bybit negative surface (open orders, order history, executions),
* one complete paginated transaction-log no-match surface (activities), and
* one four-surface absence bundle.

Every positive result is bound to exact observed provider bytes, an exact
qualified read binding, the exact current source-owned provider qualification
Q, signed endpoint/parser negative-result semantics, and a signed provider
consistency horizon.  Caller booleans cannot mint these values.

The bundle is intentionally *not* accepted by the durable reconciliation
journal yet.  Until exact response evidence and the whole bundle have a
durable replay/re-authentication path, real-provider PROVEN_ABSENT remains
fail-closed in reconciliation_journal.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_core import Surface
from .provider_qualification_authority import ProviderQualificationError
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    QualifiedProviderResponseObservation,
    _journal_cut,
    _point,
    _q_scope,
    _require_qualified_provider_read_binding_authority,
    _require_qualified_provider_response_authority,
    _route_semantics,
    qualified_read_route_semantic_claim,
)
from .provider_selection import SelectedProviderRoute


_DIRECT_ENDPOINT_SURFACES = {
    "/v5/order/realtime": "OPEN_ORDERS",
    "/v5/order/history": "ORDER_HISTORY",
    "/v5/execution/list": "EXECUTIONS",
}
_REQUIRED_DIRECT_SURFACES = frozenset(_DIRECT_ENDPOINT_SURFACES.values())
_ACTIVITY_ENDPOINT = "/v5/account/transaction-log"
_BYBIT_CATEGORIES = frozenset({"spot", "linear", "inverse", "option"})
_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_MAX_HORIZON_MS = 30 * 24 * 60 * 60 * 1000
_DIRECT_NEGATIVE_PREFIX = "NEGATIVE_RESULT_RULE:"
_ACTIVITY_NEGATIVE_PREFIX = "PAGINATED_ACTIVITY_NEGATIVE_RULE:"
_HORIZON_PREFIX = "NEGATIVE_RESULT_HORIZON_MS:"
_DIRECT_NEGATIVE_VERSION = "EMPTY_SUCCESSFUL_RESULT_MEANS_NO_MATCH_FOR_EXACT_QUERY_V1"
_ACTIVITY_NEGATIVE_VERSION = "COMPLETE_WINDOW_ALL_CURSOR_PAGES_ORDER_LINK_ID_NO_MATCH_V1"
_HORIZON_VERSION = "PROVIDER_NEGATIVE_RESULT_CONSISTENCY_HORIZON_V1"


class ProviderAbsenceAuthorityError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAbsenceAuthorityError(f"{name} must be canonical non-empty text")
    return value


def _client_order_id(value: object) -> str:
    text = _text(value, "client_order_id")
    if len(text) > 36 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in text
    ):
        raise ProviderAbsenceAuthorityError(
            "Bybit client_order_id must be 1-36 ASCII letters, numbers, dashes or underscores"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProviderAbsenceAuthorityError(f"{name} must include timezone")
        return value.astimezone(timezone.utc)
    if type(value) is str and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProviderAbsenceAuthorityError(f"{name} must be exact ISO timestamp") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProviderAbsenceAuthorityError(f"{name} must include timezone")
        return parsed.astimezone(timezone.utc)
    raise ProviderAbsenceAuthorityError(
        f"{name} must be timezone-aware datetime or canonical ISO text"
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _millis(value: object, name: str) -> int:
    text = _text(value, name)
    if not text.isascii() or not text.isdigit():
        raise ProviderAbsenceAuthorityError(
            f"{name} must be canonical non-negative millisecond integer text"
        )
    parsed = int(text, 10)
    if str(parsed) != text:
        raise ProviderAbsenceAuthorityError(
            f"{name} must be canonical non-negative millisecond integer text"
        )
    return parsed


def _millis_utc_text(value: int) -> str:
    seconds, remainder = divmod(value, 1000)
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .replace(microsecond=remainder * 1000)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _read_rule_digest(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
) -> str:
    try:
        _claim_key, digest = qualified_read_route_semantic_claim(
            provider_id=provider_id,
            endpoint=endpoint,
            surface=surface,
            permission_scope=permission_scope,
        )
    except ProviderRouteReadError as error:
        raise ProviderAbsenceAuthorityError(
            "absence authority requires a canonical qualified provider-read endpoint"
        ) from error
    return digest


def qualified_direct_negative_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
    parser_identity: str,
) -> tuple[str, str]:
    """Return the exact signed Q claim for direct negative-result semantics."""

    provider = _text(provider_id, "provider_id").upper()
    path = _text(endpoint, "endpoint")
    permission = _text(permission_scope, "permission_scope")
    parser = _text(parser_identity, "parser_identity")
    if provider != "BYBIT" or path not in _DIRECT_ENDPOINT_SURFACES:
        raise ProviderAbsenceAuthorityError(
            "direct negative semantics are currently defined only for canonical Bybit order reads"
        )
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    read_digest = _read_rule_digest(
        provider_id=provider,
        endpoint=path,
        surface=surface,
        permission_scope=permission,
    )
    locator = {"provider_id": provider, "endpoint": path}
    claim_key = _DIRECT_NEGATIVE_PREFIX + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    policy = {
        "provider_id": provider,
        "endpoint": path,
        "surface": surface.value,
        "permission_scope": permission,
        "read_rule_digest": read_digest,
        "parser_identity": parser,
        "semantics": _DIRECT_NEGATIVE_VERSION,
    }
    claim_digest = "sha256:" + sha256(
        canonical_json(policy).encode("utf-8")
    ).hexdigest()
    return claim_key, claim_digest


def qualified_activity_negative_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
    parser_identity: str,
) -> tuple[str, str]:
    """Return signed Q claim for a complete paginated activity-window scan."""

    provider = _text(provider_id, "provider_id").upper()
    path = _text(endpoint, "endpoint")
    permission = _text(permission_scope, "permission_scope")
    parser = _text(parser_identity, "parser_identity")
    if provider != "BYBIT" or path != _ACTIVITY_ENDPOINT:
        raise ProviderAbsenceAuthorityError(
            "activity negative semantics are currently defined only for Bybit transaction log"
        )
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    read_digest = _read_rule_digest(
        provider_id=provider,
        endpoint=path,
        surface=surface,
        permission_scope=permission,
    )
    locator = {"provider_id": provider, "endpoint": path}
    claim_key = _ACTIVITY_NEGATIVE_PREFIX + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    policy = {
        "provider_id": provider,
        "endpoint": path,
        "surface": surface.value,
        "permission_scope": permission,
        "read_rule_digest": read_digest,
        "parser_identity": parser,
        "semantics": _ACTIVITY_NEGATIVE_VERSION,
        "result_list_field": "list",
        "request_cursor_field": "cursor",
        "response_cursor_field": "nextPageCursor",
        "target_identity_field": "orderLinkId",
        "event_time_field": "transactionTime",
    }
    claim_digest = "sha256:" + sha256(
        canonical_json(policy).encode("utf-8")
    ).hexdigest()
    return claim_key, claim_digest


def qualified_negative_horizon_claim_key(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
    parser_identity: str,
) -> str:
    """Return the Q key whose signed value is canonical minimum horizon ms."""

    provider = _text(provider_id, "provider_id").upper()
    path = _text(endpoint, "endpoint")
    permission = _text(permission_scope, "permission_scope")
    parser = _text(parser_identity, "parser_identity")
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    read_digest = _read_rule_digest(
        provider_id=provider,
        endpoint=path,
        surface=surface,
        permission_scope=permission,
    )
    locator = {
        "provider_id": provider,
        "endpoint": path,
        "surface": surface.value,
        "permission_scope": permission,
        "read_rule_digest": read_digest,
        "parser_identity": parser,
        "policy_version": _HORIZON_VERSION,
    }
    return _HORIZON_PREFIX + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()


def _signed_horizon_ms(semantics: dict[str, str], claim_key: str) -> int:
    raw = semantics.get(claim_key)
    if type(raw) is not str or not raw or not raw.isascii() or not raw.isdigit():
        raise ProviderAbsenceAuthorityError(
            "provider qualification lacks canonical signed negative-result horizon"
        )
    parsed = int(raw, 10)
    if str(parsed) != raw or parsed > _MAX_HORIZON_MS:
        raise ProviderAbsenceAuthorityError(
            "signed negative-result horizon is non-canonical or outside bounded policy range"
        )
    return parsed


def _require_route_binding(
    route: SelectedProviderRoute,
    binding: QualifiedProviderReadQueryBinding,
) -> None:
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("binding must be exact QualifiedProviderReadQueryBinding")
    _require_qualified_provider_read_binding_authority(binding)
    base = binding.query_binding
    candidate = route.candidate
    if (
        binding.qualification_id != route.qualification_id
        or base.provider_id != candidate.provider_id
        or base.account_id != candidate.account_id
        or base.environment != route.capability.environment
        or binding.provider_environment != candidate.provider_environment
        or binding.adapter_code_sha != candidate.adapter_code_sha
        or binding.packaged_artifact_digest != candidate.packaged_artifact_digest
    ):
        raise ProviderAbsenceAuthorityError(
            "qualified provider read does not match exact selected route identity"
        )


def _current_semantics(
    *,
    route: SelectedProviderRoute,
    binding: QualifiedProviderReadQueryBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> tuple[dict[str, str], str, int]:
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    _require_route_binding(route, binding)
    point = _point(at)
    cut = _journal_cut(qualification_registry.store)
    try:
        current_q = qualification_registry.require_exact_current(
            scope=_q_scope(route),
            at=point,
            expected_qualification_id=route.qualification_id,
            journal_sequence_cut=cut,
        )
    except ProviderQualificationError as error:
        raise ProviderAbsenceAuthorityError(
            "selected route qualification is not exact current for provider absence authority"
        ) from error
    if current_q.journal_sequence_cut != cut:
        raise ProviderAbsenceAuthorityError(
            "qualification authority did not honor provider absence journal cut"
        )
    try:
        semantics, semantics_digest = _route_semantics(current_q.qualification)
    except ProviderRouteReadError as error:
        raise ProviderAbsenceAuthorityError(
            "current provider qualification route semantics are invalid"
        ) from error
    if (
        semantics_digest != binding.route_semantics_digest
        or semantics.get("PARSER_IDENTITY") != binding.parser_identity
    ):
        raise ProviderAbsenceAuthorityError(
            "qualified provider read is not bound to exact current Q semantics/parser"
        )
    if _journal_cut(qualification_registry.store) != cut:
        raise ProviderAbsenceAuthorityError(
            "provider absence qualification authority changed during authorization"
        )
    return semantics, semantics_digest, cut


def _response_envelope(response: QualifiedProviderResponseObservation) -> tuple[list[object], str]:
    if type(response) is not QualifiedProviderResponseObservation:
        raise TypeError("response must be exact QualifiedProviderResponseObservation")
    _require_qualified_provider_response_authority(response)
    observation = response.observation
    if observation.http_status != 200:
        raise ProviderAbsenceAuthorityError(
            "Bybit negative observation requires exact HTTP 200"
        )
    payload = observation.payload
    if type(payload) is not dict:
        raise ProviderAbsenceAuthorityError(
            "Bybit negative observation response must be an object"
        )
    if type(payload.get("retCode")) is not int or payload.get("retCode") != 0:
        raise ProviderAbsenceAuthorityError(
            "Bybit negative observation requires exact successful retCode"
        )
    result = payload.get("result")
    if type(result) is not dict:
        raise ProviderAbsenceAuthorityError(
            "Bybit negative observation requires result object"
        )
    rows = result.get("list")
    if type(rows) is not list:
        raise ProviderAbsenceAuthorityError(
            "Bybit negative observation requires result.list array"
        )
    cursor = result.get("nextPageCursor", "")
    if type(cursor) is not str:
        raise ProviderAbsenceAuthorityError(
            "Bybit negative observation requires string nextPageCursor"
        )
    return rows, cursor


def _require_elapsed_horizon(
    *,
    submission_at: datetime,
    observed_at: datetime,
    minimum_horizon_ms: int,
) -> None:
    if observed_at < submission_at:
        raise ProviderAbsenceAuthorityError(
            "provider negative observation predates ambiguous submission"
        )
    if observed_at < submission_at + timedelta(milliseconds=minimum_horizon_ms):
        raise ProviderAbsenceAuthorityError(
            "provider negative observation has not satisfied signed consistency horizon"
        )


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedDirectNegativeSurface:
    provider_id: str
    account_id: str
    environment: str
    surface: str
    endpoint: str
    client_order_id: str
    qualification_id: str
    parser_identity: str
    route_semantics_digest: str
    qualified_query_digest: str
    provider_response_ref: str
    provider_response_sha256: str
    minimum_horizon_ms: int
    submission_at: str
    observed_at: str
    coverage_start: str
    coverage_end: str
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAbsenceAuthorityError(
            "direct negative surface must come from sealed provider absence authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_direct_negative_surface_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "client_order_id": self.client_order_id,
            "qualification_id": self.qualification_id,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "qualified_query_digest": self.qualified_query_digest,
            "provider_response_ref": self.provider_response_ref,
            "provider_response_sha256": self.provider_response_sha256,
            "minimum_horizon_ms": self.minimum_horizon_ms,
            "submission_at": self.submission_at,
            "observed_at": self.observed_at,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-direct-negative-surface:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedActivityNegativeSurface:
    provider_id: str
    account_id: str
    environment: str
    surface: str
    endpoint: str
    client_order_id: str
    qualification_id: str
    parser_identity: str
    route_semantics_digest: str
    minimum_horizon_ms: int
    submission_at: str
    observed_at: str
    coverage_start: str
    coverage_end: str
    page_count: int
    qualified_query_digests: tuple[str, ...]
    provider_response_refs: tuple[str, ...]
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAbsenceAuthorityError(
            "activity negative surface must come from sealed provider absence authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_activity_negative_surface_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "client_order_id": self.client_order_id,
            "qualification_id": self.qualification_id,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "minimum_horizon_ms": self.minimum_horizon_ms,
            "submission_at": self.submission_at,
            "observed_at": self.observed_at,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "page_count": self.page_count,
            "qualified_query_digests": list(self.qualified_query_digests),
            "provider_response_refs": list(self.provider_response_refs),
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-activity-negative-surface:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedProviderAbsenceBundle:
    provider_id: str
    account_id: str
    environment: str
    client_order_id: str
    qualification_id: str
    parser_identity: str
    route_semantics_digest: str
    submission_at: str
    surface_evidence: tuple[tuple[str, str], ...]
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAbsenceAuthorityError(
            "provider absence bundle must come from sealed four-surface authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_provider_absence_bundle_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "client_order_id": self.client_order_id,
            "qualification_id": self.qualification_id,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "submission_at": self.submission_at,
            "surface_evidence": [list(item) for item in self.surface_evidence],
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-provider-absence-bundle:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_provider_absence_authority():
    direct_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    activity_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    bundle_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}

    direct_fields = tuple(QualifiedDirectNegativeSurface.__dataclass_fields__)
    activity_fields = tuple(QualifiedActivityNegativeSurface.__dataclass_fields__)
    bundle_fields = tuple(QualifiedProviderAbsenceBundle.__dataclass_fields__)

    def prune(states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]]) -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(
        value: object,
        expected_type: type,
        fields: tuple[str, ...],
        states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]],
        label: str,
    ) -> None:
        if type(value) is not expected_type:
            raise ProviderAbsenceAuthorityError(
                f"{label} construction authority requires exact sealed type"
            )
        prune(states)
        object_id = id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            raise ProviderAbsenceAuthorityError(
                f"{label} construction authority identity collision"
            )
        states[object_id] = (
            weakref.ref(value),
            tuple(getattr(value, field_name) for field_name in fields),
        )

    def require(
        value: object,
        expected_type: type,
        fields: tuple[str, ...],
        states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]],
        label: str,
    ) -> None:
        if type(value) is not expected_type:
            raise ProviderAbsenceAuthorityError(
                f"{label} authority requires exact sealed type"
            )
        prune(states)
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAbsenceAuthorityError(
                f"{label} construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1]:
            raise ProviderAbsenceAuthorityError(
                f"{label} changed after authority authorization"
            )

    def register_direct(value: object) -> None:
        register(
            value,
            QualifiedDirectNegativeSurface,
            direct_fields,
            direct_states,
            "direct negative surface",
        )

    def require_direct(value: object) -> None:
        require(
            value,
            QualifiedDirectNegativeSurface,
            direct_fields,
            direct_states,
            "direct negative surface",
        )

    def register_activity(value: object) -> None:
        register(
            value,
            QualifiedActivityNegativeSurface,
            activity_fields,
            activity_states,
            "activity negative surface",
        )

    def require_activity(value: object) -> None:
        require(
            value,
            QualifiedActivityNegativeSurface,
            activity_fields,
            activity_states,
            "activity negative surface",
        )

    def register_bundle(value: object) -> None:
        register(
            value,
            QualifiedProviderAbsenceBundle,
            bundle_fields,
            bundle_states,
            "provider absence bundle",
        )

    def require_bundle(value: object) -> None:
        require(
            value,
            QualifiedProviderAbsenceBundle,
            bundle_fields,
            bundle_states,
            "provider absence bundle",
        )

    return (
        register_direct,
        require_direct,
        register_activity,
        require_activity,
        register_bundle,
        require_bundle,
    )


(
    _register_direct_negative_surface_authority,
    _require_direct_negative_surface_authority,
    _register_activity_negative_surface_authority,
    _require_activity_negative_surface_authority,
    _register_provider_absence_bundle_authority,
    _require_provider_absence_bundle_authority,
) = _install_provider_absence_authority()
del _install_provider_absence_authority


def _qualify_direct_negative_surface_impl(
    *,
    route: SelectedProviderRoute,
    response: QualifiedProviderResponseObservation,
    client_order_id: str,
    submission_at: datetime,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
    _register_authority,
) -> QualifiedDirectNegativeSurface:
    if type(response) is not QualifiedProviderResponseObservation:
        raise TypeError("response must be exact QualifiedProviderResponseObservation")
    _require_qualified_provider_response_authority(response)
    binding = response.query_binding
    _require_route_binding(route, binding)
    base = binding.query_binding
    target_id = _client_order_id(client_order_id)
    submitted = _instant(submission_at, "submission_at")

    if (
        base.provider_id != "BYBIT"
        or base.endpoint not in _DIRECT_ENDPOINT_SURFACES
        or base.surface is not Surface.AUTHENTICATED_READ
        or base.permission_scope != "ORDER.READ"
    ):
        raise ProviderAbsenceAuthorityError(
            "direct absence proof requires canonical Bybit authenticated ORDER.READ endpoint"
        )

    query = dict(base.query)
    category = query.get("category")
    if category not in _BYBIT_CATEGORIES:
        raise ProviderAbsenceAuthorityError(
            "Bybit direct negative query requires exact lowercase category"
        )
    if query.get("orderLinkId") != target_id:
        raise ProviderAbsenceAuthorityError(
            "Bybit direct negative query is not bound to target client order identity"
        )

    coverage_start = submitted
    coverage_end: datetime | None = None
    if base.endpoint in {"/v5/order/realtime", "/v5/execution/list"}:
        if set(query) != {"category", "orderLinkId"}:
            raise ProviderAbsenceAuthorityError(
                "Bybit direct identity query contains filters or pagination outside negative-proof scope"
            )
    else:
        if set(query) != {"category", "orderLinkId", "startTime", "endTime"}:
            raise ProviderAbsenceAuthorityError(
                "Bybit order-history negative proof requires exact identity plus bounded time window"
            )
        start_ms = _millis(query.get("startTime"), "startTime")
        end_ms = _millis(query.get("endTime"), "endTime")
        if end_ms < start_ms:
            raise ProviderAbsenceAuthorityError(
                "Bybit order-history endTime must not precede startTime"
            )
        if end_ms - start_ms > _MAX_WINDOW_MS:
            raise ProviderAbsenceAuthorityError(
                "Bybit order-history negative-proof window must not exceed seven days"
            )
        submitted_ms = int(submitted.timestamp() * 1000)
        if not start_ms <= submitted_ms <= end_ms:
            raise ProviderAbsenceAuthorityError(
                "Bybit order-history negative-proof window does not cover submission instant"
            )
        coverage_start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        coverage_end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)

    semantics, semantics_digest, cut = _current_semantics(
        route=route,
        binding=binding,
        qualification_registry=qualification_registry,
        at=at,
    )
    negative_key, negative_digest = qualified_direct_negative_semantic_claim(
        provider_id=base.provider_id,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=binding.parser_identity,
    )
    if semantics.get(negative_key) != negative_digest:
        raise ProviderAbsenceAuthorityError(
            "provider qualification does not sign direct negative-result semantics for exact read"
        )
    horizon_key = qualified_negative_horizon_claim_key(
        provider_id=base.provider_id,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=binding.parser_identity,
    )
    horizon_ms = _signed_horizon_ms(semantics, horizon_key)

    rows, cursor = _response_envelope(response)
    if rows:
        raise ProviderAbsenceAuthorityError(
            "Bybit direct provider result contains matching records"
        )
    if cursor != "":
        raise ProviderAbsenceAuthorityError(
            "Bybit direct empty result is paginated or has non-canonical cursor"
        )
    observed = _instant(response.observation.observed_at, "response.observed_at")
    _require_elapsed_horizon(
        submission_at=submitted,
        observed_at=observed,
        minimum_horizon_ms=horizon_ms,
    )
    if coverage_end is None:
        coverage_end = observed
    elif coverage_end > observed:
        raise ProviderAbsenceAuthorityError(
            "Bybit order-history negative window ends after provider observation"
        )

    material = {
        "provider_id": base.provider_id,
        "account_id": base.account_id,
        "environment": base.environment,
        "surface": _DIRECT_ENDPOINT_SURFACES[base.endpoint],
        "endpoint": base.endpoint,
        "client_order_id": target_id,
        "qualification_id": binding.qualification_id,
        "parser_identity": binding.parser_identity,
        "route_semantics_digest": semantics_digest,
        "qualified_query_digest": binding.query_digest,
        "provider_response_ref": response.evidence_ref,
        "provider_response_sha256": response.observation.response_sha256,
        "minimum_horizon_ms": horizon_ms,
        "submission_at": _utc_text(submitted),
        "observed_at": _utc_text(observed),
        "coverage_start": _utc_text(coverage_start),
        "coverage_end": _utc_text(coverage_end),
        "authority_journal_sequence_cut": cut,
    }
    value = object.__new__(QualifiedDirectNegativeSurface)
    for field_name, field_value in material.items():
        object.__setattr__(value, field_name, field_value)
    _register_authority(value)
    _require_direct_negative_surface_authority(value)
    return value


def _qualify_activity_negative_surface_impl(
    *,
    route: SelectedProviderRoute,
    pages: tuple[QualifiedProviderResponseObservation, ...],
    client_order_id: str,
    submission_at: datetime,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
    _register_authority,
) -> QualifiedActivityNegativeSurface:
    if type(pages) is not tuple or not pages:
        raise ProviderAbsenceAuthorityError(
            "activity negative proof requires a non-empty exact tuple of provider pages"
        )
    if any(type(page) is not QualifiedProviderResponseObservation for page in pages):
        raise TypeError(
            "activity pages must be exact QualifiedProviderResponseObservation values"
        )
    for page in pages:
        _require_qualified_provider_response_authority(page)

    first_binding = pages[0].query_binding
    _require_route_binding(route, first_binding)
    first_base = first_binding.query_binding
    target_id = _client_order_id(client_order_id)
    submitted = _instant(submission_at, "submission_at")
    if (
        first_base.provider_id != "BYBIT"
        or first_base.endpoint != _ACTIVITY_ENDPOINT
        or first_base.surface is not Surface.AUTHENTICATED_READ
        or first_base.permission_scope != "ACCOUNT.READ"
    ):
        raise ProviderAbsenceAuthorityError(
            "activity absence proof requires canonical Bybit transaction-log ACCOUNT.READ"
        )

    first_query = dict(first_base.query)
    if set(first_query) != {"accountType", "category", "startTime", "endTime", "limit"}:
        raise ProviderAbsenceAuthorityError(
            "first Bybit activity page requires exact unfiltered bounded-window query"
        )
    if first_query.get("accountType") != "UNIFIED":
        raise ProviderAbsenceAuthorityError(
            "Bybit activity negative proof requires UNIFIED accountType"
        )
    category = first_query.get("category")
    if category not in _BYBIT_CATEGORIES:
        raise ProviderAbsenceAuthorityError(
            "Bybit activity negative proof requires exact lowercase category"
        )
    if first_query.get("limit") != "50":
        raise ProviderAbsenceAuthorityError(
            "Bybit activity negative proof requires canonical maximum page limit 50"
        )
    start_ms = _millis(first_query.get("startTime"), "startTime")
    end_ms = _millis(first_query.get("endTime"), "endTime")
    if end_ms < start_ms:
        raise ProviderAbsenceAuthorityError(
            "Bybit activity endTime must not precede startTime"
        )
    if end_ms - start_ms > _MAX_WINDOW_MS:
        raise ProviderAbsenceAuthorityError(
            "Bybit activity negative-proof window must not exceed seven days"
        )
    submitted_ms = int(submitted.timestamp() * 1000)
    if not start_ms <= submitted_ms <= end_ms:
        raise ProviderAbsenceAuthorityError(
            "Bybit activity negative-proof window does not cover submission instant"
        )

    semantics, semantics_digest, cut = _current_semantics(
        route=route,
        binding=first_binding,
        qualification_registry=qualification_registry,
        at=at,
    )
    activity_key, activity_digest = qualified_activity_negative_semantic_claim(
        provider_id=first_base.provider_id,
        endpoint=first_base.endpoint,
        surface=first_base.surface,
        permission_scope=first_base.permission_scope,
        parser_identity=first_binding.parser_identity,
    )
    if semantics.get(activity_key) != activity_digest:
        raise ProviderAbsenceAuthorityError(
            "provider qualification does not sign complete paginated activity semantics"
        )
    horizon_key = qualified_negative_horizon_claim_key(
        provider_id=first_base.provider_id,
        endpoint=first_base.endpoint,
        surface=first_base.surface,
        permission_scope=first_base.permission_scope,
        parser_identity=first_binding.parser_identity,
    )
    horizon_ms = _signed_horizon_ms(semantics, horizon_key)

    base_query = dict(first_query)
    expected_cursor: str | None = None
    seen_cursors: set[str] = set()
    query_digests: list[str] = []
    response_refs: list[str] = []
    final_observed: datetime | None = None

    for index, page in enumerate(pages):
        binding = page.query_binding
        _require_route_binding(route, binding)
        base = binding.query_binding
        if (
            binding.qualification_id != first_binding.qualification_id
            or binding.route_semantics_digest != semantics_digest
            or binding.endpoint_rule_digest != first_binding.endpoint_rule_digest
            or binding.qualified_route_rule_digest != first_binding.qualified_route_rule_digest
            or binding.parser_identity != first_binding.parser_identity
            or base.provider_id != first_base.provider_id
            or base.account_id != first_base.account_id
            or base.environment != first_base.environment
            or base.endpoint != _ACTIVITY_ENDPOINT
            or base.surface is not Surface.AUTHENTICATED_READ
            or base.permission_scope != "ACCOUNT.READ"
        ):
            raise ProviderAbsenceAuthorityError(
                "activity page authority differs from exact signed transaction-log scope"
            )
        expected_query = dict(base_query)
        if index > 0:
            if expected_cursor is None or expected_cursor == "":
                raise ProviderAbsenceAuthorityError(
                    "activity page exists after terminal pagination cursor"
                )
            expected_query["cursor"] = expected_cursor
        if dict(base.query) != expected_query:
            raise ProviderAbsenceAuthorityError(
                "activity page query drifted from exact cursor chain"
            )

        rows, next_cursor = _response_envelope(page)
        for row in rows:
            if type(row) is not dict:
                raise ProviderAbsenceAuthorityError(
                    "Bybit activity page contains non-object row"
                )
            order_link_id = row.get("orderLinkId")
            if type(order_link_id) is not str:
                raise ProviderAbsenceAuthorityError(
                    "Bybit activity row lacks string orderLinkId"
                )
            if order_link_id == target_id:
                raise ProviderAbsenceAuthorityError(
                    "Bybit activity window contains target client order identity"
                )
            if row.get("category") != category:
                raise ProviderAbsenceAuthorityError(
                    "Bybit activity row category differs from qualified window"
                )
            event_ms = _millis(row.get("transactionTime"), "transactionTime")
            if not start_ms <= event_ms <= end_ms:
                raise ProviderAbsenceAuthorityError(
                    "Bybit activity row lies outside qualified window"
                )

        if next_cursor:
            if next_cursor in seen_cursors:
                raise ProviderAbsenceAuthorityError(
                    "Bybit activity pagination cursor cycle detected"
                )
            seen_cursors.add(next_cursor)
        if index < len(pages) - 1 and not next_cursor:
            raise ProviderAbsenceAuthorityError(
                "Bybit activity page chain terminates before supplied next page"
            )
        if index == len(pages) - 1 and next_cursor:
            raise ProviderAbsenceAuthorityError(
                "Bybit activity page chain is incomplete"
            )
        expected_cursor = next_cursor
        query_digests.append(binding.query_digest)
        response_refs.append(page.evidence_ref)
        final_observed = _instant(page.observation.observed_at, "page.observed_at")

    if final_observed is None:
        raise ProviderAbsenceAuthorityError("activity page chain has no final observation")
    coverage_start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    coverage_end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    if coverage_end > final_observed:
        raise ProviderAbsenceAuthorityError(
            "Bybit activity negative window ends after final provider observation"
        )
    _require_elapsed_horizon(
        submission_at=submitted,
        observed_at=final_observed,
        minimum_horizon_ms=horizon_ms,
    )

    material = {
        "provider_id": first_base.provider_id,
        "account_id": first_base.account_id,
        "environment": first_base.environment,
        "surface": "ACTIVITIES",
        "endpoint": _ACTIVITY_ENDPOINT,
        "client_order_id": target_id,
        "qualification_id": first_binding.qualification_id,
        "parser_identity": first_binding.parser_identity,
        "route_semantics_digest": semantics_digest,
        "minimum_horizon_ms": horizon_ms,
        "submission_at": _utc_text(submitted),
        "observed_at": _utc_text(final_observed),
        "coverage_start": _utc_text(coverage_start),
        "coverage_end": _utc_text(coverage_end),
        "page_count": len(pages),
        "qualified_query_digests": tuple(query_digests),
        "provider_response_refs": tuple(response_refs),
        "authority_journal_sequence_cut": cut,
    }
    value = object.__new__(QualifiedActivityNegativeSurface)
    for field_name, field_value in material.items():
        object.__setattr__(value, field_name, field_value)
    _register_authority(value)
    _require_activity_negative_surface_authority(value)
    return value


def _qualify_provider_absence_bundle_impl(
    *,
    route: SelectedProviderRoute,
    direct_surfaces: tuple[QualifiedDirectNegativeSurface, ...],
    activity_surface: QualifiedActivityNegativeSurface,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
    _register_authority,
) -> QualifiedProviderAbsenceBundle:
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(direct_surfaces) is not tuple or len(direct_surfaces) != 3:
        raise ProviderAbsenceAuthorityError(
            "provider absence bundle requires exactly three direct negative surfaces"
        )
    if any(type(value) is not QualifiedDirectNegativeSurface for value in direct_surfaces):
        raise TypeError("direct_surfaces must contain exact QualifiedDirectNegativeSurface values")
    if type(activity_surface) is not QualifiedActivityNegativeSurface:
        raise TypeError("activity_surface must be exact QualifiedActivityNegativeSurface")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    for value in direct_surfaces:
        _require_direct_negative_surface_authority(value)
    _require_activity_negative_surface_authority(activity_surface)

    by_surface = {value.surface: value for value in direct_surfaces}
    if set(by_surface) != _REQUIRED_DIRECT_SURFACES or len(by_surface) != 3:
        raise ProviderAbsenceAuthorityError(
            "provider absence bundle requires OPEN_ORDERS, ORDER_HISTORY and EXECUTIONS exactly once"
        )
    expected_endpoints = {surface: endpoint for endpoint, surface in _DIRECT_ENDPOINT_SURFACES.items()}
    for surface, value in by_surface.items():
        if value.endpoint != expected_endpoints[surface]:
            raise ProviderAbsenceAuthorityError(
                "provider absence direct surface is bound to wrong canonical endpoint"
            )
    if activity_surface.surface != "ACTIVITIES" or activity_surface.endpoint != _ACTIVITY_ENDPOINT:
        raise ProviderAbsenceAuthorityError(
            "provider absence bundle requires canonical ACTIVITIES transaction-log evidence"
        )

    ordered_values: tuple[object, ...] = (
        by_surface["OPEN_ORDERS"],
        by_surface["ORDER_HISTORY"],
        by_surface["EXECUTIONS"],
        activity_surface,
    )
    first = ordered_values[0]
    identity = (
        first.provider_id,
        first.account_id,
        first.environment,
        first.client_order_id,
        first.qualification_id,
        first.parser_identity,
        first.route_semantics_digest,
        first.submission_at,
    )
    for value in ordered_values[1:]:
        current = (
            value.provider_id,
            value.account_id,
            value.environment,
            value.client_order_id,
            value.qualification_id,
            value.parser_identity,
            value.route_semantics_digest,
            value.submission_at,
        )
        if current != identity:
            raise ProviderAbsenceAuthorityError(
                "provider absence surfaces do not share one exact route/Q/order/submission identity"
            )
    if first.provider_id != route.candidate.provider_id or first.qualification_id != route.qualification_id:
        raise ProviderAbsenceAuthorityError(
            "provider absence surfaces do not match selected route identity"
        )

    point = _point(at)
    cut = _journal_cut(qualification_registry.store)
    try:
        current_q = qualification_registry.require_exact_current(
            scope=_q_scope(route),
            at=point,
            expected_qualification_id=route.qualification_id,
            journal_sequence_cut=cut,
        )
    except ProviderQualificationError as error:
        raise ProviderAbsenceAuthorityError(
            "selected route qualification is not exact current for absence bundle"
        ) from error
    if current_q.journal_sequence_cut != cut:
        raise ProviderAbsenceAuthorityError(
            "qualification authority did not honor absence-bundle journal cut"
        )
    try:
        semantics, semantics_digest = _route_semantics(current_q.qualification)
    except ProviderRouteReadError as error:
        raise ProviderAbsenceAuthorityError(
            "current provider qualification route semantics are invalid"
        ) from error
    if semantics_digest != first.route_semantics_digest or semantics.get("PARSER_IDENTITY") != first.parser_identity:
        raise ProviderAbsenceAuthorityError(
            "absence surfaces are not bound to exact current Q semantics/parser"
        )
    if _journal_cut(qualification_registry.store) != cut:
        raise ProviderAbsenceAuthorityError(
            "provider absence bundle authority changed during authorization"
        )

    surface_evidence = tuple(
        (surface, value.evidence_ref)
        for surface, value in (
            ("OPEN_ORDERS", by_surface["OPEN_ORDERS"]),
            ("ORDER_HISTORY", by_surface["ORDER_HISTORY"]),
            ("EXECUTIONS", by_surface["EXECUTIONS"]),
            ("ACTIVITIES", activity_surface),
        )
    )
    material = {
        "provider_id": first.provider_id,
        "account_id": first.account_id,
        "environment": first.environment,
        "client_order_id": first.client_order_id,
        "qualification_id": first.qualification_id,
        "parser_identity": first.parser_identity,
        "route_semantics_digest": first.route_semantics_digest,
        "submission_at": first.submission_at,
        "surface_evidence": surface_evidence,
        "authority_journal_sequence_cut": cut,
    }
    value = object.__new__(QualifiedProviderAbsenceBundle)
    for field_name, field_value in material.items():
        object.__setattr__(value, field_name, field_value)
    _register_authority(value)
    _require_provider_absence_bundle_authority(value)
    return value


def _bind_provider_absence_minting(
    direct_impl,
    activity_impl,
    bundle_impl,
    register_direct,
    register_activity,
    register_bundle,
):
    def qualify_direct_negative_surface(
        *,
        route: SelectedProviderRoute,
        response: QualifiedProviderResponseObservation,
        client_order_id: str,
        submission_at: datetime,
        qualification_registry: DurableProviderQualificationRegistry,
        at: datetime,
    ) -> QualifiedDirectNegativeSurface:
        return direct_impl(
            route=route,
            response=response,
            client_order_id=client_order_id,
            submission_at=submission_at,
            qualification_registry=qualification_registry,
            at=at,
            _register_authority=register_direct,
        )

    def qualify_activity_negative_surface(
        *,
        route: SelectedProviderRoute,
        pages: tuple[QualifiedProviderResponseObservation, ...],
        client_order_id: str,
        submission_at: datetime,
        qualification_registry: DurableProviderQualificationRegistry,
        at: datetime,
    ) -> QualifiedActivityNegativeSurface:
        return activity_impl(
            route=route,
            pages=pages,
            client_order_id=client_order_id,
            submission_at=submission_at,
            qualification_registry=qualification_registry,
            at=at,
            _register_authority=register_activity,
        )

    def qualify_provider_absence_bundle(
        *,
        route: SelectedProviderRoute,
        direct_surfaces: tuple[QualifiedDirectNegativeSurface, ...],
        activity_surface: QualifiedActivityNegativeSurface,
        qualification_registry: DurableProviderQualificationRegistry,
        at: datetime,
    ) -> QualifiedProviderAbsenceBundle:
        return bundle_impl(
            route=route,
            direct_surfaces=direct_surfaces,
            activity_surface=activity_surface,
            qualification_registry=qualification_registry,
            at=at,
            _register_authority=register_bundle,
        )

    return (
        qualify_direct_negative_surface,
        qualify_activity_negative_surface,
        qualify_provider_absence_bundle,
    )


(
    qualify_direct_negative_surface,
    qualify_activity_negative_surface,
    qualify_provider_absence_bundle,
) = _bind_provider_absence_minting(
    _qualify_direct_negative_surface_impl,
    _qualify_activity_negative_surface_impl,
    _qualify_provider_absence_bundle_impl,
    _register_direct_negative_surface_authority,
    _register_activity_negative_surface_authority,
    _register_provider_absence_bundle_authority,
)
del _bind_provider_absence_minting
del _qualify_direct_negative_surface_impl
del _qualify_activity_negative_surface_impl
del _qualify_provider_absence_bundle_impl
del _register_direct_negative_surface_authority
del _register_activity_negative_surface_authority
del _register_provider_absence_bundle_authority
