"""Exact-current source-Q consistency horizons for provider negative evidence.

Provider absence is unsafe until the provider's own read surfaces have had time
to converge after the ambiguous send.  The minimum delay is therefore not a
caller boolean or local constant: it is a signed route-semantics value in the
exact current provider qualification Q, bound to endpoint/read rule/parser.

This module issues only the signed horizon policy.  Observation-specific
settlement is implemented separately and must prove the actual provider
observation occurred after the signed delay.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_core import Surface
from .provider_qualification_authority import ProviderQualificationError
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderReadQueryBinding,
    _journal_cut,
    _point,
    _q_scope,
    _require_qualified_provider_read_binding_authority,
    _route_semantics,
    qualified_read_route_semantic_claim,
)
from .provider_selection import SelectedProviderRoute


_HORIZON_CLAIM_PREFIX = "NEGATIVE_RESULT_HORIZON_MS:"
_HORIZON_POLICY_VERSION = "PROVIDER_NEGATIVE_RESULT_CONSISTENCY_HORIZON_V1"
_MAX_HORIZON_MS = 30 * 24 * 60 * 60 * 1000


class ProviderNegativeResultHorizonError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderNegativeResultHorizonError(
            f"{name} must be canonical non-empty text"
        )
    return value


def qualified_negative_result_horizon_claim_key(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
    parser_identity: str,
) -> str:
    """Return the signed Q key whose value is canonical horizon milliseconds."""

    provider = _text(provider_id, "provider_id").upper()
    path = _text(endpoint, "endpoint")
    permission = _text(permission_scope, "permission_scope")
    parser = _text(parser_identity, "parser_identity")
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    try:
        _read_key, read_rule_digest = qualified_read_route_semantic_claim(
            provider_id=provider,
            endpoint=path,
            surface=surface,
            permission_scope=permission,
        )
    except ProviderRouteReadError as error:
        raise ProviderNegativeResultHorizonError(
            "negative-result horizon requires a canonical qualified read endpoint"
        ) from error
    locator = {
        "provider_id": provider,
        "endpoint": path,
        "surface": surface.value,
        "permission_scope": permission,
        "read_rule_digest": read_rule_digest,
        "parser_identity": parser,
        "policy_version": _HORIZON_POLICY_VERSION,
    }
    return _HORIZON_CLAIM_PREFIX + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()


def _horizon_ms(value: object) -> int:
    if type(value) is not str or not value or not value.isascii() or not value.isdigit():
        raise ProviderNegativeResultHorizonError(
            "signed negative-result horizon must be canonical millisecond integer text"
        )
    parsed = int(value, 10)
    if str(parsed) != value or parsed > _MAX_HORIZON_MS:
        raise ProviderNegativeResultHorizonError(
            "signed negative-result horizon is non-canonical or outside bounded policy range"
        )
    return parsed


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedNegativeResultHorizon:
    qualification_id: str
    provider_id: str
    account_id: str
    environment: str
    endpoint: str
    surface: Surface
    permission_scope: str
    parser_identity: str
    route_semantics_digest: str
    horizon_claim_key: str
    minimum_horizon_ms: int
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderNegativeResultHorizonError(
            "negative-result horizon must come from exact current qualification authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_negative_result_horizon_authority(self)
        material = {
            "qualification_id": self.qualification_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "endpoint": self.endpoint,
            "surface": self.surface.value,
            "permission_scope": self.permission_scope,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "horizon_claim_key": self.horizon_claim_key,
            "minimum_horizon_ms": self.minimum_horizon_ms,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-negative-result-horizon:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_horizon_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "qualification_id",
        "provider_id",
        "account_id",
        "environment",
        "endpoint",
        "surface",
        "permission_scope",
        "parser_identity",
        "route_semantics_digest",
        "horizon_claim_key",
        "minimum_horizon_ms",
        "authority_journal_sequence_cut",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> QualifiedNegativeResultHorizon:
        prune()
        value = object.__new__(QualifiedNegativeResultHorizon)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not QualifiedNegativeResultHorizon:
            raise ProviderNegativeResultHorizonError(
                "negative-result horizon authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderNegativeResultHorizonError(
                "negative-result horizon construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1] or type(value.surface) is not Surface:
            raise ProviderNegativeResultHorizonError(
                "negative-result horizon changed after qualification authorization"
            )

    return issue, require


(
    _issue_qualified_negative_result_horizon,
    _require_qualified_negative_result_horizon_authority,
) = _install_horizon_authority()
del _install_horizon_authority


def require_qualified_negative_result_horizon(
    *,
    route: SelectedProviderRoute,
    read_binding: QualifiedProviderReadQueryBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> QualifiedNegativeResultHorizon:
    """Return the exact-current signed minimum convergence delay for one read."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(read_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("read_binding must be exact QualifiedProviderReadQueryBinding")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    try:
        _require_qualified_provider_read_binding_authority(read_binding)
        point = _point(at)
        cut = _journal_cut(qualification_registry.store)
    except ProviderRouteReadError as error:
        raise ProviderNegativeResultHorizonError(
            "qualified read authority is unavailable for negative-result horizon"
        ) from error
    if read_binding.qualification_id != route.qualification_id:
        raise ProviderNegativeResultHorizonError(
            "qualified read and selected route bind different qualifications"
        )
    base = read_binding.query_binding
    candidate = route.candidate
    if (
        base.provider_id != candidate.provider_id
        or base.account_id != candidate.account_id
        or base.environment != route.capability.environment
        or read_binding.provider_environment != candidate.provider_environment
        or read_binding.adapter_code_sha != candidate.adapter_code_sha
        or read_binding.packaged_artifact_digest != candidate.packaged_artifact_digest
    ):
        raise ProviderNegativeResultHorizonError(
            "qualified read does not match selected route identity"
        )

    try:
        current_q = qualification_registry.require_exact_current(
            scope=_q_scope(route),
            at=point,
            expected_qualification_id=route.qualification_id,
            journal_sequence_cut=cut,
        )
    except ProviderQualificationError as error:
        raise ProviderNegativeResultHorizonError(
            "selected route qualification is not exact current for negative-result horizon"
        ) from error
    if current_q.journal_sequence_cut != cut:
        raise ProviderNegativeResultHorizonError(
            "qualification authority did not honor negative-result horizon journal cut"
        )
    try:
        if _journal_cut(qualification_registry.store) != cut:
            raise ProviderNegativeResultHorizonError(
                "qualification authority changed during negative-result horizon authorization"
            )
        semantics, semantics_digest = _route_semantics(current_q.qualification)
    except ProviderRouteReadError as error:
        raise ProviderNegativeResultHorizonError(
            "current qualification route semantics are invalid"
        ) from error
    if semantics_digest != read_binding.route_semantics_digest:
        raise ProviderNegativeResultHorizonError(
            "qualified read route semantics are not exact current Q semantics"
        )
    if semantics.get("PARSER_IDENTITY") != read_binding.parser_identity:
        raise ProviderNegativeResultHorizonError(
            "qualified read parser identity is not exact current Q parser identity"
        )

    claim_key = qualified_negative_result_horizon_claim_key(
        provider_id=base.provider_id,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=read_binding.parser_identity,
    )
    if claim_key not in semantics:
        raise ProviderNegativeResultHorizonError(
            "provider qualification does not sign a negative-result consistency horizon for exact read"
        )
    minimum_horizon_ms = _horizon_ms(semantics[claim_key])
    value = _issue_qualified_negative_result_horizon(
        qualification_id=read_binding.qualification_id,
        provider_id=base.provider_id,
        account_id=base.account_id,
        environment=base.environment,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=read_binding.parser_identity,
        route_semantics_digest=read_binding.route_semantics_digest,
        horizon_claim_key=claim_key,
        minimum_horizon_ms=minimum_horizon_ms,
        authority_journal_sequence_cut=cut,
    )
    _require_qualified_negative_result_horizon_authority(value)
    return value
