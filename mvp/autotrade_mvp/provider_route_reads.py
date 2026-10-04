"""Provider-origin read provenance bound to one exact current route C/Q pair.

Provider-core owns neutral authenticated query/response byte handling. This
module adds the product route authority needed by provider-origin reads: before
I/O it re-resolves the selected route's exact capability snapshot and exact Q
at one global journal cut, then seals Q and rule identity into the read binding.
The response keeps that same historical Q even if a later Q supersedes it after
wire I/O; a new read must resolve current authority again.
"""
from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Mapping

from .capabilities import CapabilityError
from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from .provider_qualification_authority import ProviderQualificationError
from .provider_qualification_current_scope import ProviderQualificationCurrentScope
from .provider_selection import SelectedProviderRoute
from .provider_transport import (
    AuthenticatedReadEndpointRule,
    BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS,
    BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS,
    KRAKEN_SPOT_AUTHENTICATED_READ_ENDPOINTS,
)


_QUERY_TOKEN = object()
_RESPONSE_TOKEN = object()
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProviderRouteReadError(ValueError):
    pass


def _point(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderRouteReadError("read time must be exact timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _journal_cut(store: object) -> int:
    cut = store.whole_store_state_cut()
    if type(cut) is not dict:
        raise ProviderRouteReadError("whole-store read cut is non-canonical")
    sequence = cut.get("journal_sequence")
    if type(sequence) is not int or sequence < 0:
        raise ProviderRouteReadError("whole-store read cut lacks canonical sequence")
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


_READ_ENDPOINTS = {
    "BINANCE": BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS,
    "BYBIT": BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS,
    "KRAKEN": KRAKEN_SPOT_AUTHENTICATED_READ_ENDPOINTS,
}


def _qualified_read_endpoint_rule(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
) -> tuple[str, str, AuthenticatedReadEndpointRule]:
    if type(provider_id) is not str or not provider_id or provider_id != provider_id.strip():
        raise ProviderRouteReadError("provider_id must be canonical text")
    provider = provider_id.upper()
    if type(endpoint) is not str or not endpoint or endpoint != endpoint.strip():
        raise ProviderRouteReadError("endpoint must be canonical text")
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")
    if (
        type(permission_scope) is not str
        or not permission_scope
        or permission_scope != permission_scope.strip()
    ):
        raise ProviderRouteReadError("permission_scope must be canonical text")
    registry = _READ_ENDPOINTS.get(provider)
    if registry is None:
        raise ProviderRouteReadError(
            "provider has no canonical authenticated-read endpoint registry"
        )
    rule = registry.get(endpoint)
    if type(rule) is not AuthenticatedReadEndpointRule:
        raise ProviderRouteReadError(
            "authenticated-read endpoint is not present in canonical provider policy"
        )
    if rule.surface != surface:
        raise ProviderRouteReadError(
            "authenticated-read surface differs from canonical provider policy"
        )
    if rule.permission_scope != permission_scope:
        raise ProviderRouteReadError(
            "authenticated-read permission differs from canonical provider policy"
        )
    locator = {
        "provider_id": provider,
        "endpoint": endpoint,
    }
    claim_key = "READ_RULE:" + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    policy = {
        "provider_id": provider,
        "endpoint": endpoint,
        "surface": rule.surface.value,
        "permission_scope": rule.permission_scope,
        "data_entitlement": rule.data_entitlement,
        "success_statuses": sorted(rule.success_statuses),
    }
    policy_digest = "sha256:" + sha256(
        canonical_json(policy).encode("utf-8")
    ).hexdigest()
    return claim_key, policy_digest, rule


def qualified_read_route_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
) -> tuple[str, str]:
    """Return the Q semantic claim required for one canonical read endpoint."""

    key, digest, _rule = _qualified_read_endpoint_rule(
        provider_id=provider_id,
        endpoint=endpoint,
        surface=surface,
        permission_scope=permission_scope,
    )
    return key, digest


def _route_semantics(qualification: object) -> tuple[dict[str, str], str]:
    raw = getattr(qualification, "route_semantics_json", None)
    if type(raw) is not str or not raw:
        raise ProviderRouteReadError("provider qualification route semantics are unavailable")
    try:
        semantics = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ProviderRouteReadError(
            "provider qualification route semantics are malformed"
        ) from error
    if (
        type(semantics) is not dict
        or not semantics
        or any(type(key) is not str or type(value) is not str for key, value in semantics.items())
        or canonical_json(semantics) != raw
    ):
        raise ProviderRouteReadError(
            "provider qualification route semantics are non-canonical"
        )
    digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
    identity = getattr(qualification, "identity", None)
    if (
        identity is None
        or getattr(identity, "route_semantics_digest", None) != digest
        or getattr(identity, "content_digest", None)
        != getattr(qualification, "qualification_id", None)
    ):
        raise ProviderRouteReadError(
            "provider qualification route semantics do not match Q identity"
        )
    return semantics, digest


def _qualified_read_rule(
    *,
    qualification: object,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
) -> tuple[str, str, str, str, tuple[int, ...], str]:
    semantics, semantics_digest = _route_semantics(qualification)
    claim_key, endpoint_rule_digest, rule = _qualified_read_endpoint_rule(
        provider_id=provider_id,
        endpoint=endpoint,
        surface=surface,
        permission_scope=permission_scope,
    )
    if semantics.get(claim_key) != endpoint_rule_digest:
        raise ProviderRouteReadError(
            "provider qualification does not cover exact authenticated-read endpoint rule"
        )
    parser_identity = semantics.get("PARSER_IDENTITY")
    if (
        type(parser_identity) is not str
        or not parser_identity
        or parser_identity != parser_identity.strip()
    ):
        raise ProviderRouteReadError(
            "provider qualification lacks canonical parser identity for provider read"
        )
    qualified_rule_digest = "sha256:" + sha256(
        canonical_json(
            {
                "endpoint_rule_digest": endpoint_rule_digest,
                "qualification_route_semantics_digest": semantics_digest,
                "parser_identity": parser_identity,
            }
        ).encode("utf-8")
    ).hexdigest()
    return (
        semantics_digest,
        endpoint_rule_digest,
        qualified_rule_digest,
        rule.data_entitlement,
        tuple(sorted(rule.success_statuses)),
        parser_identity,
    )


@dataclass(frozen=True, slots=True, init=False)
class QualifiedProviderReadQueryBinding:
    query_binding: AuthenticatedReadQueryBinding
    qualification_id: str
    route_semantics_digest: str
    endpoint_rule_digest: str
    qualified_route_rule_digest: str
    data_entitlement: str
    accepted_success_statuses: tuple[int, ...]
    parser_identity: str
    authority_journal_sequence_cut: int
    provider_environment: str
    adapter_code_sha: str
    packaged_artifact_digest: str
    _factory_token: InitVar[object | None]

    def __init__(
        self,
        *,
        query_binding: AuthenticatedReadQueryBinding,
        qualification_id: str,
        route_semantics_digest: str,
        endpoint_rule_digest: str,
        qualified_route_rule_digest: str,
        data_entitlement: str,
        accepted_success_statuses: tuple[int, ...],
        parser_identity: str,
        authority_journal_sequence_cut: int,
        provider_environment: str,
        adapter_code_sha: str,
        packaged_artifact_digest: str,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _QUERY_TOKEN:
            raise ProviderRouteReadError(
                "qualified provider-read bindings must come from canonical route authority"
            )
        if type(query_binding) is not AuthenticatedReadQueryBinding:
            raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
        if type(qualification_id) is not str or _QID_RE.fullmatch(qualification_id) is None:
            raise ProviderRouteReadError("qualification_id is not canonical")
        if type(route_semantics_digest) is not str or _SHA_RE.fullmatch(route_semantics_digest) is None:
            raise ProviderRouteReadError("route_semantics_digest is not canonical")
        if (
            type(endpoint_rule_digest) is not str
            or _SHA_RE.fullmatch(endpoint_rule_digest) is None
        ):
            raise ProviderRouteReadError("endpoint_rule_digest is not canonical")
        if (
            type(qualified_route_rule_digest) is not str
            or _SHA_RE.fullmatch(qualified_route_rule_digest) is None
        ):
            raise ProviderRouteReadError("qualified_route_rule_digest is not canonical")
        if (
            type(data_entitlement) is not str
            or not data_entitlement
            or data_entitlement != data_entitlement.strip()
        ):
            raise ProviderRouteReadError("data_entitlement must be canonical text")
        if (
            type(accepted_success_statuses) is not tuple
            or not accepted_success_statuses
            or tuple(sorted(set(accepted_success_statuses))) != accepted_success_statuses
            or any(type(status) is not int or status < 200 or status > 299 for status in accepted_success_statuses)
        ):
            raise ProviderRouteReadError(
                "accepted_success_statuses must be sorted unique exact 2xx integers"
            )
        if (
            type(parser_identity) is not str
            or not parser_identity
            or parser_identity != parser_identity.strip()
        ):
            raise ProviderRouteReadError("parser_identity must be canonical text")
        if type(authority_journal_sequence_cut) is not int or authority_journal_sequence_cut < 0:
            raise ProviderRouteReadError("authority journal cut must be a non-negative exact integer")
        if type(provider_environment) is not str or not provider_environment:
            raise ProviderRouteReadError("provider_environment is required")
        if type(adapter_code_sha) is not str or re.fullmatch(r"[0-9a-f]{40}", adapter_code_sha) is None:
            raise ProviderRouteReadError("adapter_code_sha is not canonical")
        if type(packaged_artifact_digest) is not str or _SHA_RE.fullmatch(packaged_artifact_digest) is None:
            raise ProviderRouteReadError("packaged_artifact_digest is not canonical")
        object.__setattr__(self, "query_binding", query_binding)
        object.__setattr__(self, "qualification_id", qualification_id)
        object.__setattr__(self, "route_semantics_digest", route_semantics_digest)
        object.__setattr__(self, "endpoint_rule_digest", endpoint_rule_digest)
        object.__setattr__(self, "qualified_route_rule_digest", qualified_route_rule_digest)
        object.__setattr__(self, "data_entitlement", data_entitlement)
        object.__setattr__(self, "accepted_success_statuses", accepted_success_statuses)
        object.__setattr__(self, "parser_identity", parser_identity)
        object.__setattr__(self, "authority_journal_sequence_cut", authority_journal_sequence_cut)
        object.__setattr__(self, "provider_environment", provider_environment)
        object.__setattr__(self, "adapter_code_sha", adapter_code_sha)
        object.__setattr__(self, "packaged_artifact_digest", packaged_artifact_digest)

    @property
    def query_digest(self) -> str:
        material = {
            "base_query_digest": self.query_binding.query_digest,
            "qualification_id": self.qualification_id,
            "route_semantics_digest": self.route_semantics_digest,
            "endpoint_rule_digest": self.endpoint_rule_digest,
            "qualified_route_rule_digest": self.qualified_route_rule_digest,
            "data_entitlement": self.data_entitlement,
            "accepted_success_statuses": list(self.accepted_success_statuses),
            "parser_identity": self.parser_identity,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
            "provider_environment": self.provider_environment,
            "adapter_code_sha": self.adapter_code_sha,
            "packaged_artifact_digest": self.packaged_artifact_digest,
        }
        return "sha256:" + sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class QualifiedProviderResponseObservation:
    observation: ProviderResponseObservation
    query_binding: QualifiedProviderReadQueryBinding
    _factory_token: InitVar[object | None]

    def __init__(
        self,
        *,
        observation: ProviderResponseObservation,
        query_binding: QualifiedProviderReadQueryBinding,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _RESPONSE_TOKEN:
            raise ProviderRouteReadError(
                "qualified provider responses must come from exact observed bytes"
            )
        if type(observation) is not ProviderResponseObservation:
            raise TypeError("observation must be exact ProviderResponseObservation")
        if type(query_binding) is not QualifiedProviderReadQueryBinding:
            raise TypeError("query_binding must be exact QualifiedProviderReadQueryBinding")
        if observation.query_binding != query_binding.query_binding:
            raise ProviderRouteReadError("response does not belong to qualified read query")
        object.__setattr__(self, "observation", observation)
        object.__setattr__(self, "query_binding", query_binding)

    @property
    def evidence_ref(self) -> str:
        material = {
            "neutral_evidence_ref": self.observation.evidence_ref,
            "qualified_query_digest": self.query_binding.query_digest,
            "qualification_id": self.query_binding.qualification_id,
            "qualified_route_rule_digest": self.query_binding.qualified_route_rule_digest,
            "data_entitlement": self.query_binding.data_entitlement,
            "parser_identity": self.query_binding.parser_identity,
            "response_sha256": self.observation.response_sha256,
            "observed_at": self.observation.observed_at,
        }
        return "qualified-provider-read:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()

    @property
    def qualification_id(self) -> str:
        return self.query_binding.qualification_id

    @property
    def route_semantics_digest(self) -> str:
        return self.query_binding.route_semantics_digest

    @property
    def endpoint_rule_digest(self) -> str:
        return self.query_binding.endpoint_rule_digest

    @property
    def qualified_route_rule_digest(self) -> str:
        return self.query_binding.qualified_route_rule_digest

    @property
    def data_entitlement(self) -> str:
        return self.query_binding.data_entitlement

    @property
    def parser_identity(self) -> str:
        return self.query_binding.parser_identity

    @property
    def provider_id(self) -> str:
        return self.observation.provider_id

    @property
    def account_id(self) -> str:
        return self.observation.account_id

    @property
    def environment(self) -> str:
        return self.observation.environment


def prepare_qualified_provider_read(
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
    *,
    surface: Surface,
    endpoint: str,
    query: Mapping[str, str] | None,
    at: datetime,
    permission_scope: str = "ORDER.READ",
) -> QualifiedProviderReadQueryBinding:
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if type(capability_registry) is not DurableCapabilityRegistry:
        raise TypeError("capability_registry must be exact DurableCapabilityRegistry")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if capability_registry.store is not qualification_registry.store:
        raise ProviderRouteReadError(
            "capability and qualification read authorities must share one JournalStore instance"
        )
    point = _point(at)
    cut = _journal_cut(capability_registry.store)
    candidate = route.candidate

    try:
        capability = capability_registry.require_verified(
            provider_id=candidate.provider_id,
            account_id=candidate.account_id,
            entity_id=candidate.entity_id,
            environment=route.capability.environment,
            provider_environment=candidate.provider_environment,
            instrument_version=route.capability.instrument_version,
            at=point,
            journal_sequence_cut=cut,
        )
    except CapabilityError as error:
        raise ProviderRouteReadError(
            "selected route capability is not current for provider read"
        ) from error
    if capability.snapshot_id != route.capability_snapshot_id:
        raise ProviderRouteReadError(
            "selected route capability was superseded before provider read"
        )

    try:
        current_q = qualification_registry.require_exact_current(
            scope=_q_scope(route),
            at=point,
            expected_qualification_id=route.qualification_id,
            journal_sequence_cut=cut,
        )
    except ProviderQualificationError as error:
        raise ProviderRouteReadError(
            "selected route qualification is not exact current for provider read"
        ) from error
    if current_q.journal_sequence_cut != cut:
        raise ProviderRouteReadError("qualification authority did not honor provider-read cut")
    if _journal_cut(capability_registry.store) != cut:
        raise ProviderRouteReadError("provider-read authority changed during preparation")

    base = prepare_authenticated_read_query(
        capability=capability,
        surface=surface,
        endpoint=endpoint,
        query=query,
        at=point,
        permission_scope=permission_scope,
    )
    (
        route_semantics_digest,
        endpoint_rule_digest,
        qualified_route_rule_digest,
        data_entitlement,
        accepted_success_statuses,
        parser_identity,
    ) = _qualified_read_rule(
        qualification=current_q.qualification,
        provider_id=candidate.provider_id,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
    )
    return QualifiedProviderReadQueryBinding(
        query_binding=base,
        qualification_id=route.qualification_id,
        route_semantics_digest=route_semantics_digest,
        endpoint_rule_digest=endpoint_rule_digest,
        qualified_route_rule_digest=qualified_route_rule_digest,
        data_entitlement=data_entitlement,
        accepted_success_statuses=accepted_success_statuses,
        parser_identity=parser_identity,
        authority_journal_sequence_cut=cut,
        provider_environment=candidate.provider_environment,
        adapter_code_sha=candidate.adapter_code_sha,
        packaged_artifact_digest=candidate.packaged_artifact_digest,
        _factory_token=_QUERY_TOKEN,
    )


def observe_qualified_provider_json_response(
    *,
    query_binding: QualifiedProviderReadQueryBinding,
    http_status: int,
    response_bytes: bytes,
    observed_at: datetime,
) -> QualifiedProviderResponseObservation:
    if type(query_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("query_binding must be exact QualifiedProviderReadQueryBinding")
    if type(http_status) is not int or http_status not in query_binding.accepted_success_statuses:
        raise ProviderRouteReadError(
            "provider response status is outside qualified endpoint contract"
        )
    observation = observe_authenticated_json_response(
        query_binding=query_binding.query_binding,
        http_status=http_status,
        response_bytes=response_bytes,
        observed_at=observed_at,
    )
    return QualifiedProviderResponseObservation(
        observation=observation,
        query_binding=query_binding,
        _factory_token=_RESPONSE_TOKEN,
    )
