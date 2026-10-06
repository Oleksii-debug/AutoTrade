"""Provider-origin read provenance bound to one exact current route C/Q pair.

Provider-core owns neutral authenticated query/response byte handling. This
module adds the product route authority needed by provider-origin reads: before
I/O it re-resolves the selected route's exact capability snapshot and exact Q
at one global journal cut, then seals Q and rule identity into the read binding.
The response keeps that same historical Q even if a later Q supersedes it after
wire I/O; a new read must resolve current authority again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
import weakref
from typing import Callable, Mapping

from .bybit_v5 import (
    BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
    BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
)
from .ibkr_web import (
    IBKR_BROKERAGE_ACCOUNTS_PARSER_CONTRACT_DIGEST,
    IBKR_BROKERAGE_ACCOUNTS_PARSER_IDENTITY,
    IBKR_BROKERAGE_STATUS_PARSER_CONTRACT_DIGEST,
    IBKR_BROKERAGE_STATUS_PARSER_IDENTITY,
)
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
    _require_authenticated_read_query_binding_authority,
    _require_provider_response_observation_authority,
)
from .provider_qualification_authority import ProviderQualificationError
from .provider_qualification_current_scope import ProviderQualificationCurrentScope
from .provider_selection import SelectedProviderRoute
from .provider_transport import (
    AuthenticatedReadEndpointRule,
    BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS,
    BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS,
    KRAKEN_SPOT_AUTHENTICATED_READ_ENDPOINTS,
    IBKR_WEB_AUTHENTICATED_READ_ENDPOINTS,
)


_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProviderRouteReadError(ValueError):
    pass


def _point(value: datetime) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderRouteReadError("read time must be an exact stdlib timezone datetime")
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


_READ_ENDPOINTS = MappingProxyType(
    {
        "BINANCE": BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS,
        "BYBIT": BYBIT_V5_AUTHENTICATED_READ_ENDPOINTS,
        "KRAKEN": KRAKEN_SPOT_AUTHENTICATED_READ_ENDPOINTS,
        "IBKR": IBKR_WEB_AUTHENTICATED_READ_ENDPOINTS,
    }
)


_READ_ENDPOINT_PARSER_CONTRACTS = MappingProxyType(
    {
        ("BYBIT", "/v5/asset/delivery-record"): (
            BYBIT_OPTION_DELIVERY_PARSER_IDENTITY,
            BYBIT_OPTION_DELIVERY_PARSER_CONTRACT_DIGEST,
        ),
        ("IBKR", "/iserver/auth/status"): (
            IBKR_BROKERAGE_STATUS_PARSER_IDENTITY,
            IBKR_BROKERAGE_STATUS_PARSER_CONTRACT_DIGEST,
        ),
        ("IBKR", "/iserver/accounts"): (
            IBKR_BROKERAGE_ACCOUNTS_PARSER_IDENTITY,
            IBKR_BROKERAGE_ACCOUNTS_PARSER_CONTRACT_DIGEST,
        ),
    }
)


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


def qualified_read_parser_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
) -> tuple[str, str]:
    """Return one source-owned endpoint parser claim required from provider Q."""
    _claim_key, _rule_digest, _rule = _qualified_read_endpoint_rule(
        provider_id=provider_id,
        endpoint=endpoint,
        surface=surface,
        permission_scope=permission_scope,
    )
    provider = provider_id.upper()
    parser_contract = _READ_ENDPOINT_PARSER_CONTRACTS.get((provider, endpoint))
    if parser_contract is None:
        raise ProviderRouteReadError(
            "authenticated-read endpoint has no source-owned endpoint parser contract"
        )
    _parser_identity, parser_contract_digest = parser_contract
    locator = {"provider_id": provider, "endpoint": endpoint}
    parser_claim_key = "READ_PARSER:" + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    return parser_claim_key, parser_contract_digest


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
    source_parser_contract = _READ_ENDPOINT_PARSER_CONTRACTS.get(
        (provider_id.upper(), endpoint)
    )
    parser_contract_digest = None
    if source_parser_contract is None:
        parser_identity = semantics.get("PARSER_IDENTITY")
        if (
            type(parser_identity) is not str
            or not parser_identity
            or parser_identity != parser_identity.strip()
        ):
            raise ProviderRouteReadError(
                "provider qualification lacks canonical parser identity for provider read"
            )
    else:
        parser_identity, expected_parser_contract_digest = source_parser_contract
        parser_claim_key, parser_contract_digest = qualified_read_parser_semantic_claim(
            provider_id=provider_id,
            endpoint=endpoint,
            surface=surface,
            permission_scope=permission_scope,
        )
        if parser_contract_digest != expected_parser_contract_digest:
            raise ProviderRouteReadError(
                "source-owned authenticated-read parser contract is inconsistent"
            )
        if semantics.get(parser_claim_key) != parser_contract_digest:
            raise ProviderRouteReadError(
                "provider qualification does not cover exact authenticated-read parser contract"
            )
    qualified_rule_digest = "sha256:" + sha256(
        canonical_json(
            {
                "endpoint_rule_digest": endpoint_rule_digest,
                "qualification_route_semantics_digest": semantics_digest,
                "parser_identity": parser_identity,
                "parser_contract_digest": parser_contract_digest,
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


def _validate_qualified_provider_read_material(
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
) -> None:
    if type(query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError("query_binding must be exact AuthenticatedReadQueryBinding")
    _require_authenticated_read_query_binding_authority(query_binding)
    if type(qualification_id) is not str or _QID_RE.fullmatch(qualification_id) is None:
        raise ProviderRouteReadError("qualification_id is not canonical")
    if type(route_semantics_digest) is not str or _SHA_RE.fullmatch(route_semantics_digest) is None:
        raise ProviderRouteReadError("route_semantics_digest is not canonical")
    if type(endpoint_rule_digest) is not str or _SHA_RE.fullmatch(endpoint_rule_digest) is None:
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
        or any(
            type(status) is not int or status < 200 or status > 299
            for status in accepted_success_statuses
        )
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
    if (
        type(authority_journal_sequence_cut) is not int
        or authority_journal_sequence_cut < 0
    ):
        raise ProviderRouteReadError(
            "authority journal cut must be a non-negative exact integer"
        )
    if type(provider_environment) is not str or not provider_environment:
        raise ProviderRouteReadError("provider_environment is required")
    if (
        type(adapter_code_sha) is not str
        or re.fullmatch(r"[0-9a-f]{40}", adapter_code_sha) is None
    ):
        raise ProviderRouteReadError("adapter_code_sha is not canonical")
    if (
        type(packaged_artifact_digest) is not str
        or _SHA_RE.fullmatch(packaged_artifact_digest) is None
    ):
        raise ProviderRouteReadError("packaged_artifact_digest is not canonical")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
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

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderRouteReadError(
            "qualified provider-read bindings must come from canonical route authority"
        )

    @property
    def query_digest(self) -> str:
        _require_qualified_provider_read_binding_authority(self)
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


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedProviderResponseObservation:
    observation: ProviderResponseObservation
    query_binding: QualifiedProviderReadQueryBinding

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderRouteReadError(
            "qualified provider responses must come from exact observed bytes"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_provider_response_authority(self)
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
        _require_qualified_provider_response_authority(self)
        return self.query_binding.qualification_id

    @property
    def route_semantics_digest(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.query_binding.route_semantics_digest

    @property
    def endpoint_rule_digest(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.query_binding.endpoint_rule_digest

    @property
    def qualified_route_rule_digest(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.query_binding.qualified_route_rule_digest

    @property
    def data_entitlement(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.query_binding.data_entitlement

    @property
    def parser_identity(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.query_binding.parser_identity

    @property
    def provider_environment(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.query_binding.provider_environment

    @property
    def provider_id(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.observation.provider_id

    @property
    def account_id(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.observation.account_id

    @property
    def environment(self) -> str:
        _require_qualified_provider_response_authority(self)
        return self.observation.environment


def _install_qualified_provider_read_authority():
    """Keep Q/rule provenance outside caller-writable frozen dataclass state."""

    query_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    response_states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}

    def prune(states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]]) -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register_query(value: object) -> None:
        if type(value) is not QualifiedProviderReadQueryBinding:
            raise ProviderRouteReadError(
                "qualified read construction authority requires exact binding"
            )
        _require_authenticated_read_query_binding_authority(value.query_binding)
        prune(query_states)
        object_id = id(value)
        current = query_states.get(object_id)
        if current is not None and current[0]() is not None:
            raise ProviderRouteReadError(
                "qualified read construction authority identity collision"
            )
        query_states[object_id] = (
            weakref.ref(value),
            (
                value.query_binding, value.qualification_id, value.route_semantics_digest,
                value.endpoint_rule_digest, value.qualified_route_rule_digest,
                value.data_entitlement, value.accepted_success_statuses,
                value.parser_identity, value.authority_journal_sequence_cut,
                value.provider_environment, value.adapter_code_sha,
                value.packaged_artifact_digest,
            ),
        )

    def require_query(value: object) -> None:
        if type(value) is not QualifiedProviderReadQueryBinding:
            raise ProviderRouteReadError(
                "qualified read construction authority requires exact binding"
            )
        prune(query_states)
        state = query_states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderRouteReadError(
                "qualified read construction authority is unavailable"
            )
        (
            query_binding, qualification_id, route_semantics_digest,
            endpoint_rule_digest, qualified_route_rule_digest, data_entitlement,
            accepted_success_statuses, parser_identity, authority_journal_sequence_cut,
            provider_environment, adapter_code_sha, packaged_artifact_digest,
        ) = state[1]
        if value.query_binding is not query_binding:
            raise ProviderRouteReadError(
                "qualified read binding changed after route authority preparation"
            )
        _require_authenticated_read_query_binding_authority(value.query_binding)
        string_pairs = (
            (value.qualification_id, qualification_id),
            (value.route_semantics_digest, route_semantics_digest),
            (value.endpoint_rule_digest, endpoint_rule_digest),
            (value.qualified_route_rule_digest, qualified_route_rule_digest),
            (value.data_entitlement, data_entitlement),
            (value.parser_identity, parser_identity),
            (value.provider_environment, provider_environment),
            (value.adapter_code_sha, adapter_code_sha),
            (value.packaged_artifact_digest, packaged_artifact_digest),
        )
        if any(type(current) is not str or current != expected for current, expected in string_pairs):
            raise ProviderRouteReadError(
                "qualified read binding changed after route authority preparation"
            )
        if value.accepted_success_statuses is not accepted_success_statuses:
            raise ProviderRouteReadError(
                "qualified read binding changed after route authority preparation"
            )
        if (
            type(value.authority_journal_sequence_cut) is not int
            or value.authority_journal_sequence_cut != authority_journal_sequence_cut
        ):
            raise ProviderRouteReadError(
                "qualified read binding changed after route authority preparation"
            )

    def register_response(value: object) -> None:
        if type(value) is not QualifiedProviderResponseObservation:
            raise ProviderRouteReadError(
                "qualified response construction authority requires exact observation"
            )
        require_query(value.query_binding)
        _require_provider_response_observation_authority(value.observation)
        prune(response_states)
        object_id = id(value)
        current = response_states.get(object_id)
        if current is not None and current[0]() is not None:
            raise ProviderRouteReadError(
                "qualified response construction authority identity collision"
            )
        response_states[object_id] = (
            weakref.ref(value),
            (value.observation, value.query_binding),
        )

    def require_response(value: object) -> None:
        if type(value) is not QualifiedProviderResponseObservation:
            raise ProviderRouteReadError(
                "qualified response construction authority requires exact observation"
            )
        prune(response_states)
        state = response_states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderRouteReadError(
                "qualified response construction authority is unavailable"
            )
        observation, query_binding = state[1]
        if value.observation is not observation or value.query_binding is not query_binding:
            raise ProviderRouteReadError(
                "qualified response changed after exact-byte observation"
            )
        require_query(value.query_binding)
        _require_provider_response_observation_authority(value.observation)

    return register_query, require_query, register_response, require_response


(
    _register_qualified_provider_read_binding_authority,
    _require_qualified_provider_read_binding_authority,
    _register_qualified_provider_response_authority,
    _require_qualified_provider_response_authority,
) = _install_qualified_provider_read_authority()
del _install_qualified_provider_read_authority


def _prepare_qualified_provider_read_impl(
    route: SelectedProviderRoute,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
    *,
    surface: Surface,
    endpoint: str,
    query: Mapping[str, str] | None,
    at: datetime,
    permission_scope: str = "ORDER.READ",
    _register_authority,
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
    material = {
        "query_binding": base,
        "qualification_id": route.qualification_id,
        "route_semantics_digest": route_semantics_digest,
        "endpoint_rule_digest": endpoint_rule_digest,
        "qualified_route_rule_digest": qualified_route_rule_digest,
        "data_entitlement": data_entitlement,
        "accepted_success_statuses": accepted_success_statuses,
        "parser_identity": parser_identity,
        "authority_journal_sequence_cut": cut,
        "provider_environment": candidate.provider_environment,
        "adapter_code_sha": candidate.adapter_code_sha,
        "packaged_artifact_digest": candidate.packaged_artifact_digest,
    }
    _validate_qualified_provider_read_material(**material)
    binding = object.__new__(QualifiedProviderReadQueryBinding)
    for field_name, field_value in material.items():
        object.__setattr__(binding, field_name, field_value)
    _register_authority(binding)
    return binding


def _observe_qualified_provider_json_response_impl(
    *,
    query_binding: QualifiedProviderReadQueryBinding,
    http_status: int,
    response_bytes: bytes,
    observed_at: datetime,
    _register_authority,
) -> QualifiedProviderResponseObservation:
    if type(query_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError("query_binding must be exact QualifiedProviderReadQueryBinding")
    _require_qualified_provider_read_binding_authority(query_binding)
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
    qualified = object.__new__(QualifiedProviderResponseObservation)
    object.__setattr__(qualified, "observation", observation)
    object.__setattr__(qualified, "query_binding", query_binding)
    _register_authority(qualified)
    return qualified

def qualify_provider_response_observation(
    *,
    query_binding: QualifiedProviderReadQueryBinding,
    observation: ProviderResponseObservation,
) -> QualifiedProviderResponseObservation:
    """Attach exact current Q/rule provenance to one canonical wire observation.

    The neutral provider transport owns credential use and exact response-byte
    observation. This function proves that it executed the exact base binding
    sealed inside the qualified route binding before adding the historical Q
    identity. It does not reparse or copy provider bytes.
    """

    if type(query_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError(
            "query_binding must be exact QualifiedProviderReadQueryBinding"
        )
    _require_qualified_provider_read_binding_authority(query_binding)
    if type(observation) is not ProviderResponseObservation:
        raise TypeError("observation must be exact ProviderResponseObservation")
    _require_provider_response_observation_authority(observation)
    if observation.query_binding is not query_binding.query_binding:
        raise ProviderRouteReadError(
            "provider response was not produced from exact qualified read binding"
        )
    if (
        type(observation.http_status) is not int
        or observation.http_status not in query_binding.accepted_success_statuses
    ):
        raise ProviderRouteReadError(
            "provider response status is outside qualified endpoint contract"
        )
    qualified = object.__new__(QualifiedProviderResponseObservation)
    object.__setattr__(qualified, "observation", observation)
    object.__setattr__(qualified, "query_binding", query_binding)
    _register_qualified_provider_response_authority(qualified)
    return qualified


def execute_qualified_provider_read(
    *,
    query_binding: QualifiedProviderReadQueryBinding,
    transport: Callable[[AuthenticatedReadQueryBinding], ProviderResponseObservation],
) -> QualifiedProviderResponseObservation:
    """Execute one sealed qualified read through a neutral provider transport."""

    if type(query_binding) is not QualifiedProviderReadQueryBinding:
        raise TypeError(
            "query_binding must be exact QualifiedProviderReadQueryBinding"
        )
    _require_qualified_provider_read_binding_authority(query_binding)
    if not callable(transport):
        raise TypeError("transport must be callable")
    observation = transport(query_binding.query_binding)
    return qualify_provider_response_observation(
        query_binding=query_binding,
        observation=observation,
    )


def _bind_qualified_provider_read_minting(
    prepare_impl,
    observe_impl,
    register_query,
    register_response,
):
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
        return prepare_impl(
            route,
            capability_registry,
            qualification_registry,
            surface=surface,
            endpoint=endpoint,
            query=query,
            at=at,
            permission_scope=permission_scope,
            _register_authority=register_query,
        )

    def observe_qualified_provider_json_response(
        *,
        query_binding: QualifiedProviderReadQueryBinding,
        http_status: int,
        response_bytes: bytes,
        observed_at: datetime,
    ) -> QualifiedProviderResponseObservation:
        return observe_impl(
            query_binding=query_binding,
            http_status=http_status,
            response_bytes=response_bytes,
            observed_at=observed_at,
            _register_authority=register_response,
        )

    return prepare_qualified_provider_read, observe_qualified_provider_json_response


(
    prepare_qualified_provider_read,
    observe_qualified_provider_json_response,
) = _bind_qualified_provider_read_minting(
    _prepare_qualified_provider_read_impl,
    _observe_qualified_provider_json_response_impl,
    _register_qualified_provider_read_binding_authority,
    _register_qualified_provider_response_authority,
)
del _bind_qualified_provider_read_minting
del _prepare_qualified_provider_read_impl
del _observe_qualified_provider_json_response_impl
del _register_qualified_provider_read_binding_authority
del _register_qualified_provider_response_authority

