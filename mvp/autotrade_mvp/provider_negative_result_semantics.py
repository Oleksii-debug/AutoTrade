"""Signed negative-result semantics for exact qualified provider reads.

This module deliberately stops short of classifying any response as absent.
It proves only that the exact current provider qualification Q contains a
source-owned signed claim saying the parser for one canonical authenticated
read endpoint is allowed to interpret an empty successful result as no match
for that exact query.  A later response classifier must still prove that the
observed bytes are a canonical empty result under that parser before any
reconciliation layer can mint PROVEN_ABSENT.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re
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


_NEGATIVE_RESULT_RULE_PREFIX = "NEGATIVE_RESULT_RULE:"
_NEGATIVE_RESULT_SEMANTICS_V1 = "EMPTY_SUCCESSFUL_RESULT_MEANS_NO_MATCH_FOR_EXACT_QUERY_V1"
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProviderNegativeResultSemanticsError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderNegativeResultSemanticsError(
            f"{name} must be canonical non-empty text"
        )
    return value


def qualified_negative_result_route_semantic_claim(
    *,
    provider_id: str,
    endpoint: str,
    surface: Surface,
    permission_scope: str,
    parser_identity: str,
) -> tuple[str, str]:
    """Return the exact signed Q claim required for negative-result semantics.

    The locator is endpoint-specific.  The signed value additionally binds the
    canonical read-rule digest, surface/permission policy, parser identity, and
    semantic version so a parser or endpoint-policy change cannot inherit an
    earlier negative-result qualification accidentally.
    """

    parser = _text(parser_identity, "parser_identity")
    try:
        _read_key, read_rule_digest = qualified_read_route_semantic_claim(
            provider_id=provider_id,
            endpoint=endpoint,
            surface=surface,
            permission_scope=permission_scope,
        )
    except ProviderRouteReadError as error:
        raise ProviderNegativeResultSemanticsError(
            "negative-result semantics require a canonical qualified read endpoint"
        ) from error

    provider = _text(provider_id, "provider_id").upper()
    path = _text(endpoint, "endpoint")
    permission = _text(permission_scope, "permission_scope")
    if type(surface) is not Surface:
        raise TypeError("surface must be exact Surface")

    locator = {
        "provider_id": provider,
        "endpoint": path,
    }
    claim_key = _NEGATIVE_RESULT_RULE_PREFIX + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    policy = {
        "provider_id": provider,
        "endpoint": path,
        "surface": surface.value,
        "permission_scope": permission,
        "read_rule_digest": read_rule_digest,
        "parser_identity": parser,
        "semantics": _NEGATIVE_RESULT_SEMANTICS_V1,
    }
    claim_digest = "sha256:" + sha256(
        canonical_json(policy).encode("utf-8")
    ).hexdigest()
    return claim_key, claim_digest


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class QualifiedNegativeResultSemantics:
    """Sealed source-owned negative-result semantics for one prepared read."""

    qualification_id: str
    qualified_query_digest: str
    provider_id: str
    account_id: str
    environment: str
    endpoint: str
    surface: Surface
    permission_scope: str
    parser_identity: str
    route_semantics_digest: str
    negative_result_rule_key: str
    negative_result_rule_digest: str
    semantics_version: str
    authority_journal_sequence_cut: int

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderNegativeResultSemanticsError(
            "negative-result semantics must come from exact current qualification authority"
        )

    @property
    def evidence_ref(self) -> str:
        _require_qualified_negative_result_semantics_authority(self)
        material = {
            "qualification_id": self.qualification_id,
            "qualified_query_digest": self.qualified_query_digest,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "endpoint": self.endpoint,
            "surface": self.surface.value,
            "permission_scope": self.permission_scope,
            "parser_identity": self.parser_identity,
            "route_semantics_digest": self.route_semantics_digest,
            "negative_result_rule_key": self.negative_result_rule_key,
            "negative_result_rule_digest": self.negative_result_rule_digest,
            "semantics_version": self.semantics_version,
            "authority_journal_sequence_cut": self.authority_journal_sequence_cut,
        }
        return "qualified-negative-result-semantics:sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_negative_result_semantics_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def issue(**material: object) -> QualifiedNegativeResultSemantics:
        prune()
        value = object.__new__(QualifiedNegativeResultSemantics)
        for field_name, field_value in material.items():
            object.__setattr__(value, field_name, field_value)
        snapshot = tuple(material[field_name] for field_name in (
            "qualification_id",
            "qualified_query_digest",
            "provider_id",
            "account_id",
            "environment",
            "endpoint",
            "surface",
            "permission_scope",
            "parser_identity",
            "route_semantics_digest",
            "negative_result_rule_key",
            "negative_result_rule_digest",
            "semantics_version",
            "authority_journal_sequence_cut",
        ))
        states[id(value)] = (weakref.ref(value), snapshot)
        return value

    def require(value: object) -> None:
        if type(value) is not QualifiedNegativeResultSemantics:
            raise ProviderNegativeResultSemanticsError(
                "negative-result semantics authority requires exact sealed value"
            )
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderNegativeResultSemanticsError(
                "negative-result semantics construction authority is unavailable"
            )
        current = (
            value.qualification_id,
            value.qualified_query_digest,
            value.provider_id,
            value.account_id,
            value.environment,
            value.endpoint,
            value.surface,
            value.permission_scope,
            value.parser_identity,
            value.route_semantics_digest,
            value.negative_result_rule_key,
            value.negative_result_rule_digest,
            value.semantics_version,
            value.authority_journal_sequence_cut,
        )
        if current != state[1]:
            raise ProviderNegativeResultSemanticsError(
                "negative-result semantics changed after qualification authorization"
            )
        if type(value.surface) is not Surface:
            raise ProviderNegativeResultSemanticsError(
                "negative-result semantics surface changed after authorization"
            )
        if (
            _SHA_RE.fullmatch(value.route_semantics_digest) is None
            or _SHA_RE.fullmatch(value.negative_result_rule_digest) is None
        ):
            raise ProviderNegativeResultSemanticsError(
                "negative-result semantics digest changed after authorization"
            )

    return issue, require


(
    _issue_qualified_negative_result_semantics,
    _require_qualified_negative_result_semantics_authority,
) = _install_negative_result_semantics_authority()
del _install_negative_result_semantics_authority


def require_qualified_negative_result_semantics(
    *,
    route: SelectedProviderRoute,
    read_binding: QualifiedProviderReadQueryBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> QualifiedNegativeResultSemantics:
    """Revalidate exact-current Q and require its signed negative-result claim.

    This function never inspects a provider response and therefore cannot prove
    absence.  Its result is only one prerequisite for a future response-level
    negative proof.
    """

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
        raise ProviderNegativeResultSemanticsError(
            "qualified read authority is unavailable for negative-result semantics"
        ) from error

    if read_binding.qualification_id != route.qualification_id:
        raise ProviderNegativeResultSemanticsError(
            "qualified read and selected route do not bind the same qualification"
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
        raise ProviderNegativeResultSemanticsError(
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
        raise ProviderNegativeResultSemanticsError(
            "selected route qualification is not exact current for negative-result semantics"
        ) from error
    if current_q.journal_sequence_cut != cut:
        raise ProviderNegativeResultSemanticsError(
            "qualification authority did not honor negative-result journal cut"
        )
    try:
        if _journal_cut(qualification_registry.store) != cut:
            raise ProviderNegativeResultSemanticsError(
                "qualification authority changed during negative-result authorization"
            )
        semantics, semantics_digest = _route_semantics(current_q.qualification)
    except ProviderRouteReadError as error:
        raise ProviderNegativeResultSemanticsError(
            "current qualification route semantics are invalid"
        ) from error

    if semantics_digest != read_binding.route_semantics_digest:
        raise ProviderNegativeResultSemanticsError(
            "qualified read route semantics are not the exact current Q semantics"
        )
    parser_identity = semantics.get("PARSER_IDENTITY")
    if parser_identity != read_binding.parser_identity:
        raise ProviderNegativeResultSemanticsError(
            "qualified read parser identity is not exact current Q parser identity"
        )

    claim_key, claim_digest = qualified_negative_result_route_semantic_claim(
        provider_id=base.provider_id,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=read_binding.parser_identity,
    )
    if semantics.get(claim_key) != claim_digest:
        raise ProviderNegativeResultSemanticsError(
            "provider qualification does not authorize negative-result semantics for exact read"
        )

    value = _issue_qualified_negative_result_semantics(
        qualification_id=read_binding.qualification_id,
        qualified_query_digest=read_binding.query_digest,
        provider_id=base.provider_id,
        account_id=base.account_id,
        environment=base.environment,
        endpoint=base.endpoint,
        surface=base.surface,
        permission_scope=base.permission_scope,
        parser_identity=read_binding.parser_identity,
        route_semantics_digest=read_binding.route_semantics_digest,
        negative_result_rule_key=claim_key,
        negative_result_rule_digest=claim_digest,
        semantics_version=_NEGATIVE_RESULT_SEMANTICS_V1,
        authority_journal_sequence_cut=cut,
    )
    _require_qualified_negative_result_semantics_authority(value)
    return value
