"""Exact provider negative observations without reconciliation finality.

One empty provider response is not PROVEN_ABSENT.  This module can issue a
sealed observation only when all of these are true at classification time:
- the response came from the exact canonical authenticated query binding;
- the query is a sealed direct client-order-id negative-result scope;
- the exact current source-owned Q signs negative-result semantics for that
  endpoint/parser/read rule; and
- the observed Bybit success envelope is an empty, unpaginated result set.

Consistency horizons, the ACTIVITIES complete-window proof, and the required
multi-surface bundle remain separate prerequisites for final reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import weakref
from datetime import datetime

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_core import (
    ProviderResponseObservation,
    Surface,
    _require_provider_response_observation_authority,
)
from .provider_negative_result_query_scope import (
    CanonicalNegativeResultQueryScope,
    _require_negative_result_query_scope_authority,
)
from .provider_negative_result_semantics import (
    _require_qualified_negative_result_semantics_authority,
    require_qualified_negative_result_semantics,
)
from .provider_route_reads import (
    QualifiedProviderReadQueryBinding,
    _require_qualified_provider_read_binding_authority,
)
from .provider_selection import SelectedProviderRoute


_SURFACE_BY_ENDPOINT = {
    "/v5/order/realtime": "OPEN_ORDERS",
    "/v5/order/history": "ORDER_HISTORY",
    "/v5/execution/list": "EXECUTIONS",
}


class ProviderNegativeResultObservationError(ValueError):
    pass


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedEmptyIdentityObservation:
    provider_id: str
    account_id: str
    environment: str
    surface: str
    endpoint: str
    client_order_id: str
    qualification_id: str
    parser_identity: str
    qualified_query_digest: str
    route_semantics_digest: str
    negative_result_semantics_ref: str
    query_scope_ref: str
    provider_response_ref: str
    provider_response_sha256: str
    observed_at: str
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderNegativeResultObservationError(
            "qualified empty observations must come from exact provider evidence authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_empty_identity_observation_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "client_order_id": self.client_order_id,
            "qualification_id": self.qualification_id,
            "parser_identity": self.parser_identity,
            "qualified_query_digest": self.qualified_query_digest,
            "route_semantics_digest": self.route_semantics_digest,
            "negative_result_semantics_ref": self.negative_result_semantics_ref,
            "query_scope_ref": self.query_scope_ref,
            "provider_response_ref": self.provider_response_ref,
            "provider_response_sha256": self.provider_response_sha256,
            "observed_at": self.observed_at,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-empty-provider-identity:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_empty_observation_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "provider_id",
        "account_id",
        "environment",
        "surface",
        "endpoint",
        "client_order_id",
        "qualification_id",
        "parser_identity",
        "qualified_query_digest",
        "route_semantics_digest",
        "negative_result_semantics_ref",
        "query_scope_ref",
        "provider_response_ref",
        "provider_response_sha256",
        "observed_at",
        "authority_journal_sequence_cut",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> QualifiedEmptyIdentityObservation:
        prune()
        value = object.__new__(QualifiedEmptyIdentityObservation)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not QualifiedEmptyIdentityObservation:
            raise ProviderNegativeResultObservationError(
                "qualified empty observation authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderNegativeResultObservationError(
                "qualified empty observation construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1]:
            raise ProviderNegativeResultObservationError(
                "qualified empty observation changed after evidence authorization"
            )

    return issue, require


(
    _issue_qualified_empty_identity_observation,
    _require_qualified_empty_identity_observation_authority,
) = _install_empty_observation_authority()
del _install_empty_observation_authority


def _require_empty_bybit_success(observation: ProviderResponseObservation) -> None:
    if observation.http_status != 200:
        raise ProviderNegativeResultObservationError(
            "Bybit negative observation requires exact HTTP 200"
        )
    payload = observation.payload
    if type(payload) is not dict:
        raise ProviderNegativeResultObservationError(
            "Bybit negative observation response must be an object"
        )
    if type(payload.get("retCode")) is not int or payload.get("retCode") != 0:
        raise ProviderNegativeResultObservationError(
            "Bybit negative observation requires exact successful retCode"
        )
    result = payload.get("result")
    if type(result) is not dict:
        raise ProviderNegativeResultObservationError(
            "Bybit negative observation requires result object"
        )
    items = result.get("list")
    if type(items) is not list:
        raise ProviderNegativeResultObservationError(
            "Bybit negative observation requires result.list array"
        )
    if items:
        raise ProviderNegativeResultObservationError(
            "Bybit provider result contains matching records"
        )
    cursor = result.get("nextPageCursor", "")
    if type(cursor) is not str or cursor != "":
        raise ProviderNegativeResultObservationError(
            "Bybit empty result is paginated or has non-canonical cursor"
        )


def qualify_empty_bybit_identity_observation(
    *,
    route: SelectedProviderRoute,
    read_binding: QualifiedProviderReadQueryBinding,
    query_scope: CanonicalNegativeResultQueryScope,
    response: ProviderResponseObservation,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> QualifiedEmptyIdentityObservation:
    """Issue one exact-current, signed-Q empty identity observation."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(read_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("read_binding must be exact QualifiedProviderReadQueryBinding")
    if type(query_scope) is not CanonicalNegativeResultQueryScope:
        raise TypeError("query_scope must be exact CanonicalNegativeResultQueryScope")
    if type(response) is not ProviderResponseObservation:
        raise TypeError("response must be exact ProviderResponseObservation")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )

    try:
        _require_qualified_provider_read_binding_authority(read_binding)
        _require_negative_result_query_scope_authority(query_scope)
        _require_provider_response_observation_authority(response)
    except Exception as error:
        raise ProviderNegativeResultObservationError(
            "provider negative-observation prerequisite authority is unavailable"
        ) from error

    base = read_binding.query_binding
    if response.query_binding is not base:
        raise ProviderNegativeResultObservationError(
            "provider response does not belong to exact qualified read binding"
        )
    if (
        query_scope.query_digest != base.query_digest
        or query_scope.provider_id != base.provider_id
        or query_scope.account_id != base.account_id
        or query_scope.environment != base.environment
        or query_scope.endpoint != base.endpoint
        or query_scope.surface is not base.surface
        or query_scope.permission_scope != base.permission_scope
    ):
        raise ProviderNegativeResultObservationError(
            "negative-result query scope does not match exact qualified read"
        )
    expected_surface = _SURFACE_BY_ENDPOINT.get(query_scope.endpoint)
    if expected_surface is None:
        raise ProviderNegativeResultObservationError(
            "endpoint is not a direct Bybit negative-observation surface"
        )

    semantics = require_qualified_negative_result_semantics(
        route=route,
        read_binding=read_binding,
        qualification_registry=qualification_registry,
        at=at,
    )
    _require_qualified_negative_result_semantics_authority(semantics)
    if (
        semantics.qualified_query_digest != read_binding.query_digest
        or semantics.provider_id != query_scope.provider_id
        or semantics.account_id != query_scope.account_id
        or semantics.environment != query_scope.environment
        or semantics.endpoint != query_scope.endpoint
        or semantics.surface is not query_scope.surface
        or semantics.permission_scope != query_scope.permission_scope
        or semantics.parser_identity != read_binding.parser_identity
    ):
        raise ProviderNegativeResultObservationError(
            "signed negative-result semantics do not match exact query scope"
        )

    _require_empty_bybit_success(response)
    if type(response.observed_at) is not str or not response.observed_at:
        raise ProviderNegativeResultObservationError(
            "provider observation time is unavailable"
        )

    value = _issue_qualified_empty_identity_observation(
        provider_id=query_scope.provider_id,
        account_id=query_scope.account_id,
        environment=query_scope.environment,
        surface=expected_surface,
        endpoint=query_scope.endpoint,
        client_order_id=query_scope.client_order_id,
        qualification_id=semantics.qualification_id,
        parser_identity=semantics.parser_identity,
        qualified_query_digest=read_binding.query_digest,
        route_semantics_digest=semantics.route_semantics_digest,
        negative_result_semantics_ref=semantics.evidence_ref,
        query_scope_ref=query_scope.evidence_ref,
        provider_response_ref=response.evidence_ref,
        provider_response_sha256=response.response_sha256,
        observed_at=response.observed_at,
        authority_journal_sequence_cut=semantics.authority_journal_sequence_cut,
    )
    _require_qualified_empty_identity_observation_authority(value)
    return value
