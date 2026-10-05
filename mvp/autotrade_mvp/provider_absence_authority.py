"""Canonical provider-Q authority for provider absence semantics.

This module authorizes only the semantic statement that one exact qualified
provider endpoint, when completely and correctly observed, excludes an
unobserved execution for one reconciliation surface.

It deliberately does NOT authorize:
- pagination completeness;
- the temporal coverage window;
- consistency-horizon expiry;
- a PROVEN_ABSENT reconciliation verdict.

Those facts require their own sealed observation/coverage authorities.  Keeping
them separate prevents caller-authored booleans or timestamps from becoming
financial truth for recovery of an UNKNOWN outbound submission.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from types import MappingProxyType
import weakref
from typing import Mapping

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_qualification_authority import ProviderQualificationError
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderResponseObservation,
    _require_qualified_provider_response_authority,
)


_SCHEMA_VERSION = "1.0.0"
_SEMANTIC_MEANING = "COMPLETE_SURFACE_EXCLUDES_UNOBSERVED_EXECUTION"


class ProviderAbsenceAuthorityError(ValueError):
    """Canonical provider absence-semantics authority could not be established."""


@dataclass(frozen=True, slots=True)
class ProviderAbsenceEndpointPolicy:
    provider_id: str
    endpoint: str
    reconciliation_surface: str
    data_entitlement: str

    def __post_init__(self) -> None:
        provider = _token(self.provider_id, name="provider_id", upper=True)
        endpoint = _endpoint(self.endpoint)
        surface = _token(
            self.reconciliation_surface,
            name="reconciliation_surface",
            upper=True,
        )
        entitlement = _token(
            self.data_entitlement,
            name="data_entitlement",
            upper=True,
        )
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "reconciliation_surface", surface)
        object.__setattr__(self, "data_entitlement", entitlement)

    def payload(self) -> dict[str, str]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "reconciliation_surface": self.reconciliation_surface,
            "data_entitlement": self.data_entitlement,
            "semantic_meaning": _SEMANTIC_MEANING,
        }

    @property
    def claim_key(self) -> str:
        locator = {
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "reconciliation_surface": self.reconciliation_surface,
        }
        return "ABSENCE_RULE:" + sha256(
            canonical_json(locator).encode("utf-8")
        ).hexdigest()

    @property
    def claim_digest(self) -> str:
        return "sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedProviderAbsenceSemantics:
    """Sealed provider-Q semantic capability; never complete-window evidence."""

    provider_id: str
    account_id: str
    environment: str
    endpoint: str
    reconciliation_surface: str
    data_entitlement: str
    qualification_id: str
    route_semantics_digest: str
    provider_response_ref: str
    authority_journal_sequence_cut: int
    claim_key: str
    claim_digest: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAbsenceAuthorityError(
            "qualified absence semantics must come from canonical provider-Q authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_absence_semantics_authority(self)
        material = {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "endpoint": self.endpoint,
            "reconciliation_surface": self.reconciliation_surface,
            "data_entitlement": self.data_entitlement,
            "qualification_id": self.qualification_id,
            "route_semantics_digest": self.route_semantics_digest,
            "provider_response_ref": self.provider_response_ref,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
            "claim_key": self.claim_key,
            "claim_digest": self.claim_digest,
        }
        return "qualified-provider-absence-semantics:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_absence_semantics_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "provider_id",
        "account_id",
        "environment",
        "endpoint",
        "reconciliation_surface",
        "data_entitlement",
        "qualification_id",
        "route_semantics_digest",
        "provider_response_ref",
        "authority_journal_sequence_cut",
        "claim_key",
        "claim_digest",
    )

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> QualifiedProviderAbsenceSemantics:
        prune()
        value = object.__new__(QualifiedProviderAbsenceSemantics)
        for field_name in fields:
            object.__setattr__(value, field_name, material[field_name])
        states[id(value)] = (
            weakref.ref(value),
            tuple(material[field_name] for field_name in fields),
        )
        return value

    def require(value: object) -> None:
        if type(value) is not QualifiedProviderAbsenceSemantics:
            raise ProviderAbsenceAuthorityError(
                "absence semantics authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAbsenceAuthorityError(
                "absence semantics construction authority is unavailable"
            )
        current = tuple(getattr(value, field_name) for field_name in fields)
        if current != state[1]:
            raise ProviderAbsenceAuthorityError(
                "absence semantics authority changed after issuance"
            )

    return issue, require


(
    _issue_qualified_absence_semantics,
    _require_qualified_absence_semantics_authority,
) = _install_absence_semantics_authority()
del _install_absence_semantics_authority


def _token(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAbsenceAuthorityError(
            f"{name} must be canonical non-empty text"
        )
    result = value.upper() if upper else value
    if len(result) > 128 or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/+-"
        for character in result
    ):
        raise ProviderAbsenceAuthorityError(f"{name} is not canonical")
    return result


def _endpoint(value: object) -> str:
    endpoint = _token(value, name="endpoint")
    if (
        not endpoint.startswith("/")
        or endpoint.startswith("//")
        or "://" in endpoint
    ):
        raise ProviderAbsenceAuthorityError(
            "endpoint must be a canonical provider-relative path"
        )
    return endpoint


def _environment(value: object) -> str:
    environment = _token(value, name="environment", upper=True)
    if environment not in {"PAPER", "LIVE"}:
        raise ProviderAbsenceAuthorityError(
            "provider absence authority permits only PAPER or LIVE runtime environments"
        )
    return environment


_POLICIES: Mapping[
    tuple[str, str], ProviderAbsenceEndpointPolicy
] = MappingProxyType(
    {
        ("BYBIT", "/v5/order/realtime"): ProviderAbsenceEndpointPolicy(
            provider_id="BYBIT",
            endpoint="/v5/order/realtime",
            reconciliation_surface="OPEN_ORDERS",
            data_entitlement="ORDERS",
        ),
        ("BYBIT", "/v5/order/history"): ProviderAbsenceEndpointPolicy(
            provider_id="BYBIT",
            endpoint="/v5/order/history",
            reconciliation_surface="ORDER_HISTORY",
            data_entitlement="ORDERS",
        ),
        ("BYBIT", "/v5/execution/list"): ProviderAbsenceEndpointPolicy(
            provider_id="BYBIT",
            endpoint="/v5/execution/list",
            reconciliation_surface="EXECUTIONS",
            data_entitlement="EXECUTIONS",
        ),
        ("BYBIT", "/v5/account/transaction-log"): ProviderAbsenceEndpointPolicy(
            provider_id="BYBIT",
            endpoint="/v5/account/transaction-log",
            reconciliation_surface="ACTIVITIES",
            data_entitlement="ACTIVITIES",
        ),
    }
)


def qualified_absence_route_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    reconciliation_surface: str,
) -> tuple[str, str]:
    """Return the source-owned Q claim required for one absence surface."""

    provider = _token(provider_id, name="provider_id", upper=True)
    path = _endpoint(endpoint)
    surface = _token(
        reconciliation_surface,
        name="reconciliation_surface",
        upper=True,
    )
    policy = _POLICIES.get((provider, path))
    if policy is None or policy.reconciliation_surface != surface:
        raise ProviderAbsenceAuthorityError(
            "provider endpoint has no canonical absence-semantics policy for surface"
        )
    return policy.claim_key, policy.claim_digest


def _route_semantics(record: object) -> dict[str, str]:
    raw = getattr(record, "route_semantics_json", None)
    if type(raw) is not str or not raw:
        raise ProviderAbsenceAuthorityError(
            "provider qualification route semantics are unavailable"
        )
    try:
        semantics = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProviderAbsenceAuthorityError(
            "provider qualification route semantics are malformed"
        ) from error
    if (
        type(semantics) is not dict
        or not semantics
        or any(
            type(key) is not str or type(value) is not str
            for key, value in semantics.items()
        )
        or canonical_json(semantics) != raw
    ):
        raise ProviderAbsenceAuthorityError(
            "provider qualification route semantics are non-canonical"
        )
    digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
    identity = getattr(record, "identity", None)
    if (
        identity is None
        or getattr(identity, "route_semantics_digest", None) != digest
        or getattr(identity, "content_digest", None)
        != getattr(record, "qualification_id", None)
    ):
        raise ProviderAbsenceAuthorityError(
            "provider qualification route semantics do not match Q identity"
        )
    return semantics


def require_qualified_absence_semantics(
    observation: QualifiedProviderResponseObservation,
    qualification_registry: DurableProviderQualificationRegistry,
    *,
    reconciliation_surface: str,
) -> QualifiedProviderAbsenceSemantics:
    """Issue only the semantic capability attached to one exact qualified read.

    This function intentionally accepts no pagination, coverage-window, or
    consistency-horizon inputs.  Those facts are not properties of one
    qualified response and must be established by separate sealed authorities
    before reconciliation may construct durable negative coverage.
    """

    if type(observation) is not QualifiedProviderResponseObservation:
        raise TypeError(
            "observation must be exact QualifiedProviderResponseObservation"
        )
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    try:
        _require_qualified_provider_response_authority(observation)
        provider_response_ref = observation.evidence_ref
    except ProviderRouteReadError as error:
        raise ProviderAbsenceAuthorityError(
            "qualified provider response construction authority is unavailable"
        ) from error

    expected_provider = _token(
        observation.provider_id,
        name="provider_id",
        upper=True,
    )
    expected_account = _token(observation.account_id, name="account_id")
    expected_environment = _environment(observation.environment)
    expected_surface = _token(
        reconciliation_surface,
        name="reconciliation_surface",
        upper=True,
    )

    binding = observation.query_binding
    base = binding.query_binding
    policy = _POLICIES.get((expected_provider, base.endpoint))
    if policy is None or policy.reconciliation_surface != expected_surface:
        raise ProviderAbsenceAuthorityError(
            "qualified provider endpoint does not match requested reconciliation surface"
        )
    if binding.data_entitlement != policy.data_entitlement:
        raise ProviderAbsenceAuthorityError(
            "qualified provider read entitlement does not match absence policy"
        )

    try:
        record = qualification_registry.qualification(
            observation.qualification_id,
            journal_sequence_cut=binding.authority_journal_sequence_cut,
        )
    except ProviderQualificationError as error:
        raise ProviderAbsenceAuthorityError(
            "exact historical provider qualification is unavailable for response"
        ) from error

    scope = record.scope
    provider_scope = scope.provider_scope
    if (
        record.qualification_id != observation.qualification_id
        or provider_scope.provider_id != expected_provider
        or provider_scope.runtime_environment != expected_environment
        or provider_scope.provider_environment != binding.provider_environment
        or scope.adapter_source_git_sha != binding.adapter_code_sha
        or scope.packaged_artifact_digest != binding.packaged_artifact_digest
    ):
        raise ProviderAbsenceAuthorityError(
            "provider qualification scope does not match qualified response authority"
        )

    semantics = _route_semantics(record)
    semantics_digest = "sha256:" + sha256(
        record.route_semantics_json.encode("utf-8")
    ).hexdigest()
    if semantics_digest != observation.route_semantics_digest:
        raise ProviderAbsenceAuthorityError(
            "provider response route semantics differ from historical qualification"
        )
    claim_key, claim_digest = qualified_absence_route_semantic_claim(
        provider_id=expected_provider,
        endpoint=base.endpoint,
        reconciliation_surface=expected_surface,
    )
    if semantics.get(claim_key) != claim_digest:
        raise ProviderAbsenceAuthorityError(
            "provider qualification does not cover exact absence-semantics rule"
        )

    return _issue_qualified_absence_semantics(
        provider_id=expected_provider,
        account_id=expected_account,
        environment=expected_environment,
        endpoint=base.endpoint,
        reconciliation_surface=expected_surface,
        data_entitlement=policy.data_entitlement,
        qualification_id=observation.qualification_id,
        route_semantics_digest=semantics_digest,
        provider_response_ref=provider_response_ref,
        authority_journal_sequence_cut=binding.authority_journal_sequence_cut,
        claim_key=claim_key,
        claim_digest=claim_digest,
    )
