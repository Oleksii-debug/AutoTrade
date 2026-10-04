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


def _rule_digest(route: SelectedProviderRoute) -> str:
    # route_semantics_json is already canonical at accepted-Q issuance. Hash it
    # again as an explicit consumer-facing rule identity rather than requiring
    # downstream consumers to parse mutable-looking text.
    return "sha256:" + sha256(route.qualification.route_semantics_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class QualifiedProviderReadQueryBinding:
    query_binding: AuthenticatedReadQueryBinding
    qualification_id: str
    route_semantics_digest: str
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
    def qualification_id(self) -> str:
        return self.query_binding.qualification_id

    @property
    def route_semantics_digest(self) -> str:
        return self.query_binding.route_semantics_digest

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
    return QualifiedProviderReadQueryBinding(
        query_binding=base,
        qualification_id=route.qualification_id,
        route_semantics_digest=_rule_digest(route),
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
