"""Durable provider-qualification authority.

This module is the provider-neutral historical Q owner.  It consumes a
verifier-produced AcceptedQualificationAttestation plus one strict campaign
payload, derives a content-addressed AcceptedProviderQualification, and stores
immutable ACCEPT/SUPERSEDE/REVOKE transitions in the canonical JournalStore.

Historical durability is intentionally separate from terminal route currentness.
Until product-owned revalidation is composed, require_current() fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from threading import Lock
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID
import weakref

from autotrade_runtime.artifacts import trusted_authenticated_reader
from autotrade_runtime.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_runtime.strict_json import strict_json_loads

from .persistence import JournalStore, canonical_json, payload_digest
from .provider_domain import (
    ProviderDomainError,
    ProviderFinancialScope,
    provider_financial_scope,
)
from .provider_core import REQUIRED_QUALIFICATION_CASES
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationTrustError,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)


_AGGREGATE_TYPE = "provider_qualification"
_EVENT_ACCEPT = "ProviderQualificationAccepted.v1"
_EVENT_SUPERSEDE = "ProviderQualificationSuperseded.v1"
_EVENT_REVOKE = "ProviderQualificationRevoked.v1"
_CAMPAIGN_SCHEMA = "1.0.0"
_ACCEPTED_SCHEMA = "1.0.0"
_RECONCILIATION_SCHEMA = "1.0.0"
_RECONCILIATION_STREAM_SCHEMA = "2.0.0"
_STREAM_SEQUENCE_POLICIES = frozenset(
    {
        "ARITHMETIC_SEQUENCE",
        "MONOTONIC_NONCONTIGUOUS",
        "NO_PROVIDER_SEQUENCE",
    }
)
_REQUIRED_CASE_SET_VERSION = "provider-qualification:v1"
_PROVIDER_DOMAIN = "PROVIDER"
_PROVIDER_GATE = "PROVIDER_QUALIFICATION"
_PROVIDER_PACKAGE = "AUTOTRADE_PROVIDER_QUALIFICATION"
_CAMPAIGN_EVIDENCE_KIND = "PROVIDER_QUALIFICATION_CAMPAIGN"
_CASE_RESULTS = frozenset({"PASS", "FAIL", "UNSUPPORTED"})
_GENERATION_SCHEMES = frozenset(
    {"PROVIDER_NATIVE_GENERATION", "SERIALIZED_ACQUISITION_GENERATION"}
)
_TOKEN = re.compile(r"^[A-Z0-9][A-Z0-9._:-]{0,127}$")
_POLICY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACCEPTED_TOKEN = object()
_ISSUED_QUALIFICATION_LOCK = Lock()
_ISSUED_QUALIFICATIONS: dict[
    int,
    tuple[weakref.ReferenceType["AcceptedProviderQualification"], str],
] = {}



class ProviderQualificationAuthorityError(ValueError):
    """Raised when provider qualification cannot become durable authority."""


class ProviderQualificationCurrentnessUnavailable(ProviderQualificationAuthorityError):
    """Raised while terminal current-Q revalidation is not yet composed."""


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderQualificationAuthorityError(
            f"{name} must be exact non-empty text"
        )
    return value


def _token(value: object, *, name: str, upper: bool = True) -> str:
    text = _exact_text(value, name=name)
    normalized = text.upper() if upper else text
    pattern = _TOKEN if upper else _POLICY_TOKEN
    if pattern.fullmatch(normalized) is None:
        raise ProviderQualificationAuthorityError(f"{name} is not canonical")
    return normalized


def _git_sha(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    if _SHA.fullmatch(text) is None:
        raise ProviderQualificationAuthorityError(f"{name} must be a Git object id")
    return text


def _digest(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    if _DIGEST.fullmatch(text) is None:
        raise ProviderQualificationAuthorityError(
            f"{name} must be a canonical SHA-256 digest"
        )
    return text


def _uuid(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise ProviderQualificationAuthorityError(
            f"{name} must be a canonical UUID"
        ) from error
    canonical = str(parsed)
    if canonical != text:
        raise ProviderQualificationAuthorityError(
            f"{name} must be a canonical UUID"
        )
    return canonical


def _utc_text(value: object, *, name: str) -> str:
    text = _exact_text(value, name=name)
    if not text.endswith("Z"):
        raise ProviderQualificationAuthorityError(f"{name} must be canonical UTC")
    try:
        point = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ProviderQualificationAuthorityError(
            f"{name} must be canonical UTC"
        ) from error
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ProviderQualificationAuthorityError(f"{name} must be canonical UTC")
    return text


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _utc_now() -> datetime:
    """Process-TCB current-time source; terminal callers cannot supply decision time."""

    return datetime.now(timezone.utc)


def _strict_keys(
    value: object, *, name: str, keys: frozenset[str]
) -> dict[str, object]:
    if type(value) is not dict:
        raise ProviderQualificationAuthorityError(f"{name} must be an exact object")
    if frozenset(value) != keys:
        missing = sorted(keys - frozenset(value))
        extra = sorted(frozenset(value) - keys)
        raise ProviderQualificationAuthorityError(
            f"{name} has non-canonical fields; missing={missing}; extra={extra}"
        )
    return value


def _string_list(
    value: object, *, name: str, upper: bool = False, allow_empty: bool = True
) -> tuple[str, ...]:
    if type(value) is not list:
        raise ProviderQualificationAuthorityError(f"{name} must be an exact list")
    items = tuple(
        _token(item, name=f"{name} item", upper=upper) for item in value
    )
    if not allow_empty and not items:
        raise ProviderQualificationAuthorityError(f"{name} must not be empty")
    if len(set(items)) != len(items):
        raise ProviderQualificationAuthorityError(f"{name} must not contain duplicates")
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True, order=True)
class ProviderQualificationCaseResult:
    case_id: str
    result: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "case_id", _token(self.case_id, name="case_id", upper=False)
        )
        result = _token(self.result, name="case result")
        if result not in _CASE_RESULTS:
            raise ProviderQualificationAuthorityError(
                "case result must be PASS, FAIL, or UNSUPPORTED"
            )
        object.__setattr__(self, "result", result)

    def canonical(self) -> dict[str, str]:
        return {"case_id": self.case_id, "result": self.result}


@dataclass(frozen=True, slots=True, order=True)
class ReconciliationSurfaceSemantics:
    surface_id: str
    endpoint: str
    category: str
    exclusion_authority: bool
    pagination_rule_id: str
    consistency_horizon_ms: int
    cache_policy_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "surface_id", _token(self.surface_id, name="surface_id")
        )
        endpoint = _exact_text(self.endpoint, name="endpoint")
        if not endpoint.startswith("/") or "://" in endpoint:
            raise ProviderQualificationAuthorityError(
                "reconciliation endpoint must be a provider-relative path"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(
            self, "category", _token(self.category, name="category")
        )
        if type(self.exclusion_authority) is not bool:
            raise ProviderQualificationAuthorityError(
                "exclusion_authority must be exact bool"
            )
        object.__setattr__(
            self,
            "pagination_rule_id",
            _token(self.pagination_rule_id, name="pagination_rule_id", upper=False),
        )
        if (
            type(self.consistency_horizon_ms) is not int
            or isinstance(self.consistency_horizon_ms, bool)
            or self.consistency_horizon_ms < 0
            or self.consistency_horizon_ms > 604_800_000
        ):
            raise ProviderQualificationAuthorityError(
                "consistency_horizon_ms must be an integer from 0 through 604800000"
            )
        object.__setattr__(
            self,
            "cache_policy_id",
            _token(self.cache_policy_id, name="cache_policy_id", upper=False),
        )

    def canonical(self) -> dict[str, object]:
        return {
            "cache_policy_id": self.cache_policy_id,
            "category": self.category,
            "consistency_horizon_ms": self.consistency_horizon_ms,
            "endpoint": self.endpoint,
            "exclusion_authority": self.exclusion_authority,
            "pagination_rule_id": self.pagination_rule_id,
            "surface_id": self.surface_id,
        }


@dataclass(frozen=True, slots=True, order=True)
class PrivateStreamSemantics:
    """Qualified parser/continuity contract for one private-stream topic."""

    topic_id: str
    parser_id: str
    parser_version: str
    sequence_policy: str
    sequence_scope: str
    recovery_method_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "topic_id", _token(self.topic_id, name="topic_id", upper=False)
        )
        object.__setattr__(
            self, "parser_id", _token(self.parser_id, name="parser_id", upper=False)
        )
        object.__setattr__(
            self,
            "parser_version",
            _token(self.parser_version, name="parser_version", upper=False),
        )
        policy = _token(self.sequence_policy, name="sequence_policy")
        if policy not in _STREAM_SEQUENCE_POLICIES:
            raise ProviderQualificationAuthorityError(
                "unsupported private-stream sequence policy"
            )
        object.__setattr__(self, "sequence_policy", policy)
        scope = _token(
            self.sequence_scope,
            name="sequence_scope",
            upper=False,
        )
        if policy == "NO_PROVIDER_SEQUENCE":
            if scope != "none":
                raise ProviderQualificationAuthorityError(
                    "NO_PROVIDER_SEQUENCE requires sequence_scope=none"
                )
        elif scope == "none":
            raise ProviderQualificationAuthorityError(
                "qualified provider sequence requires a non-none sequence scope"
            )
        object.__setattr__(self, "sequence_scope", scope)
        object.__setattr__(
            self,
            "recovery_method_id",
            _token(
                self.recovery_method_id,
                name="recovery_method_id",
                upper=False,
            ),
        )

    def canonical(self) -> dict[str, str]:
        return {
            "parser_id": self.parser_id,
            "parser_version": self.parser_version,
            "recovery_method_id": self.recovery_method_id,
            "sequence_policy": self.sequence_policy,
            "sequence_scope": self.sequence_scope,
            "topic_id": self.topic_id,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationSemantics:
    generation_scheme: str
    surfaces: tuple[ReconciliationSurfaceSemantics, ...]
    stream_topics: tuple[PrivateStreamSemantics, ...] = ()

    def __post_init__(self) -> None:
        scheme = _token(self.generation_scheme, name="generation_scheme")
        if scheme not in _GENERATION_SCHEMES:
            raise ProviderQualificationAuthorityError(
                "unsupported reconciliation generation scheme"
            )
        object.__setattr__(self, "generation_scheme", scheme)
        surfaces = tuple(self.surfaces)
        if not surfaces or not all(
            type(item) is ReconciliationSurfaceSemantics for item in surfaces
        ):
            raise ProviderQualificationAuthorityError(
                "reconciliation semantics require exact surfaces"
            )
        if len({item.surface_id for item in surfaces}) != len(surfaces):
            raise ProviderQualificationAuthorityError(
                "reconciliation surface identities must be unique"
            )
        object.__setattr__(
            self, "surfaces", tuple(sorted(surfaces, key=lambda item: item.surface_id))
        )
        stream_topics = tuple(self.stream_topics)
        if not all(type(item) is PrivateStreamSemantics for item in stream_topics):
            raise ProviderQualificationAuthorityError(
                "private-stream semantics require exact topic values"
            )
        if len({item.topic_id for item in stream_topics}) != len(stream_topics):
            raise ProviderQualificationAuthorityError(
                "private-stream topic identities must be unique"
            )
        object.__setattr__(
            self,
            "stream_topics",
            tuple(sorted(stream_topics, key=lambda item: item.topic_id)),
        )

    def canonical(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "generation_scheme": self.generation_scheme,
            "schema_version": _RECONCILIATION_SCHEMA,
            "surfaces": [item.canonical() for item in self.surfaces],
        }
        if self.stream_topics:
            payload["schema_version"] = _RECONCILIATION_STREAM_SCHEMA
            payload["stream_topics"] = [
                item.canonical() for item in self.stream_topics
            ]
        return payload

    @property
    def content_sha256(self) -> str:
        return "sha256:" + sha256(
            canonical_json(self.canonical()).encode("utf-8")
        ).hexdigest()


def _parse_reconciliation(value: object) -> ReconciliationSemantics | None:
    if value is None:
        return None
    if type(value) is not dict:
        raise ProviderQualificationAuthorityError(
            "reconciliation_semantics must be an exact object"
        )
    schema = value.get("schema_version")
    if schema == _RECONCILIATION_SCHEMA:
        raw = _strict_keys(
            value,
            name="reconciliation_semantics",
            keys=frozenset({"schema_version", "generation_scheme", "surfaces"}),
        )
        raw_stream_topics: object = []
    elif schema == _RECONCILIATION_STREAM_SCHEMA:
        raw = _strict_keys(
            value,
            name="reconciliation_semantics",
            keys=frozenset(
                {
                    "schema_version",
                    "generation_scheme",
                    "surfaces",
                    "stream_topics",
                }
            ),
        )
        raw_stream_topics = raw["stream_topics"]
        if type(raw_stream_topics) is not list or not raw_stream_topics:
            raise ProviderQualificationAuthorityError(
                "stream-aware reconciliation semantics require a non-empty exact stream_topics list"
            )
    else:
        raise ProviderQualificationAuthorityError(
            "unsupported reconciliation semantics schema"
        )

    if type(raw["surfaces"]) is not list:
        raise ProviderQualificationAuthorityError(
            "reconciliation surfaces must be an exact list"
        )
    surfaces: list[ReconciliationSurfaceSemantics] = []
    expected_surface_keys = frozenset(
        {
            "surface_id",
            "endpoint",
            "category",
            "exclusion_authority",
            "pagination_rule_id",
            "consistency_horizon_ms",
            "cache_policy_id",
        }
    )
    for index, item in enumerate(raw["surfaces"]):
        surface = _strict_keys(
            item,
            name=f"reconciliation surface {index}",
            keys=expected_surface_keys,
        )
        surfaces.append(
            ReconciliationSurfaceSemantics(
                surface_id=surface["surface_id"],
                endpoint=surface["endpoint"],
                category=surface["category"],
                exclusion_authority=surface["exclusion_authority"],
                pagination_rule_id=surface["pagination_rule_id"],
                consistency_horizon_ms=surface["consistency_horizon_ms"],
                cache_policy_id=surface["cache_policy_id"],
            )
        )

    stream_topics: list[PrivateStreamSemantics] = []
    expected_stream_keys = frozenset(
        {
            "topic_id",
            "parser_id",
            "parser_version",
            "sequence_policy",
            "sequence_scope",
            "recovery_method_id",
        }
    )
    if type(raw_stream_topics) is list:
        for index, item in enumerate(raw_stream_topics):
            topic = _strict_keys(
                item,
                name=f"private-stream topic {index}",
                keys=expected_stream_keys,
            )
            stream_topics.append(
                PrivateStreamSemantics(
                    topic_id=topic["topic_id"],
                    parser_id=topic["parser_id"],
                    parser_version=topic["parser_version"],
                    sequence_policy=topic["sequence_policy"],
                    sequence_scope=topic["sequence_scope"],
                    recovery_method_id=topic["recovery_method_id"],
                )
            )
    return ReconciliationSemantics(
        generation_scheme=raw["generation_scheme"],
        surfaces=tuple(surfaces),
        stream_topics=tuple(stream_topics),
    )


_CAMPAIGN_KEYS = frozenset(
    {
        "schema_version",
        "provider_id",
        "product_family",
        "environment",
        "provider_environment",
        "route_policy_id",
        "entity_policy_id",
        "network_policy_id",
        "account_class",
        "adapter_source_sha",
        "campaign_protocol_id",
        "campaign_protocol_version",
        "required_case_set_version",
        "case_results",
        "unsupported_features",
        "documentation_revisions",
        "campaign_artifact_id",
        "started_at",
        "completed_at",
        "valid_until",
        "release_artifact_id",
        "release_artifact_sha256",
        "live_authorized",
        "reconciliation_semantics",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderQualificationCampaign:
    provider_scope: ProviderFinancialScope
    product_family: str
    entity_policy_id: str
    network_policy_id: str
    account_class: str
    adapter_source_sha: str
    campaign_protocol_id: str
    campaign_protocol_version: str
    required_case_set_version: str
    case_results: tuple[ProviderQualificationCaseResult, ...]
    unsupported_features: tuple[str, ...]
    documentation_revisions: tuple[str, ...]
    campaign_artifact_id: str
    campaign_sha256: str
    started_at: str
    completed_at: str
    valid_until: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None
    live_authorized: bool
    reconciliation_semantics: ReconciliationSemantics | None

    def __post_init__(self) -> None:
        if type(self.provider_scope) is not ProviderFinancialScope:
            raise ProviderQualificationAuthorityError(
                "provider_scope must be exact ProviderFinancialScope"
            )
        object.__setattr__(
            self, "product_family", _token(self.product_family, name="product_family")
        )
        object.__setattr__(
            self,
            "entity_policy_id",
            _token(self.entity_policy_id, name="entity_policy_id", upper=False),
        )
        object.__setattr__(
            self,
            "network_policy_id",
            _token(self.network_policy_id, name="network_policy_id", upper=False),
        )
        object.__setattr__(
            self, "account_class", _token(self.account_class, name="account_class")
        )
        object.__setattr__(
            self,
            "adapter_source_sha",
            _git_sha(self.adapter_source_sha, name="adapter_source_sha"),
        )
        object.__setattr__(
            self,
            "campaign_protocol_id",
            _token(
                self.campaign_protocol_id,
                name="campaign_protocol_id",
                upper=False,
            ),
        )
        object.__setattr__(
            self,
            "campaign_protocol_version",
            _token(
                self.campaign_protocol_version,
                name="campaign_protocol_version",
                upper=False,
            ),
        )
        object.__setattr__(
            self,
            "required_case_set_version",
            _token(
                self.required_case_set_version,
                name="required_case_set_version",
                upper=False,
            ),
        )
        cases = tuple(self.case_results)
        if not cases or not all(type(item) is ProviderQualificationCaseResult for item in cases):
            raise ProviderQualificationAuthorityError(
                "case_results must contain exact case result values"
            )
        if len({item.case_id for item in cases}) != len(cases):
            raise ProviderQualificationAuthorityError("case_ids must be unique")
        if self.required_case_set_version != _REQUIRED_CASE_SET_VERSION:
            raise ProviderQualificationAuthorityError(
                "unsupported provider qualification required-case set version"
            )
        actual_case_ids = frozenset(item.case_id for item in cases)
        if actual_case_ids != REQUIRED_QUALIFICATION_CASES:
            missing = sorted(REQUIRED_QUALIFICATION_CASES - actual_case_ids)
            extra = sorted(actual_case_ids - REQUIRED_QUALIFICATION_CASES)
            raise ProviderQualificationAuthorityError(
                "provider qualification case results do not match the exact "
                f"required case set; missing={missing}; extra={extra}"
            )
        object.__setattr__(
            self, "case_results", tuple(sorted(cases, key=lambda item: item.case_id))
        )
        unsupported = tuple(self.unsupported_features)
        if len(set(unsupported)) != len(unsupported):
            raise ProviderQualificationAuthorityError(
                "unsupported_features must be unique"
            )
        for value in unsupported:
            _token(value, name="unsupported feature", upper=False)
        object.__setattr__(self, "unsupported_features", tuple(sorted(unsupported)))
        docs = tuple(self.documentation_revisions)
        if not docs or len(set(docs)) != len(docs):
            raise ProviderQualificationAuthorityError(
                "documentation_revisions must be non-empty and unique"
            )
        for value in docs:
            _token(value, name="documentation revision", upper=False)
        object.__setattr__(self, "documentation_revisions", tuple(sorted(docs)))
        object.__setattr__(
            self,
            "campaign_artifact_id",
            _uuid(self.campaign_artifact_id, name="campaign_artifact_id"),
        )
        object.__setattr__(
            self,
            "campaign_sha256",
            _digest(self.campaign_sha256, name="campaign_sha256"),
        )
        for name in ("started_at", "completed_at", "valid_until"):
            object.__setattr__(self, name, _utc_text(getattr(self, name), name=name))
        if not (
            _instant(self.started_at)
            <= _instant(self.completed_at)
            < _instant(self.valid_until)
        ):
            raise ProviderQualificationAuthorityError(
                "campaign chronology or freshness bound is invalid"
            )
        if (self.release_artifact_id is None) != (
            self.release_artifact_sha256 is None
        ):
            raise ProviderQualificationAuthorityError(
                "release artifact identity and digest must be supplied together"
            )
        if self.release_artifact_id is not None:
            object.__setattr__(
                self,
                "release_artifact_id",
                _uuid(self.release_artifact_id, name="release_artifact_id"),
            )
            object.__setattr__(
                self,
                "release_artifact_sha256",
                _digest(
                    self.release_artifact_sha256,
                    name="release_artifact_sha256",
                ),
            )
        if type(self.live_authorized) is not bool:
            raise ProviderQualificationAuthorityError(
                "live_authorized must be exact bool"
            )
        if self.provider_scope.environment == "LIVE" or self.live_authorized:
            raise ProviderQualificationAuthorityError(
                "LIVE provider qualification is not authorized by this protocol"
            )
        if self.reconciliation_semantics is not None and type(
            self.reconciliation_semantics
        ) is not ReconciliationSemantics:
            raise ProviderQualificationAuthorityError(
                "reconciliation_semantics must be exact or None"
            )

    @classmethod
    def from_mapping(
        cls,
        value: object,
        *,
        campaign_sha256: object,
    ) -> "ProviderQualificationCampaign":
        raw = _strict_keys(
            value,
            name="provider qualification campaign",
            keys=_CAMPAIGN_KEYS,
        )
        if raw["schema_version"] != _CAMPAIGN_SCHEMA:
            raise ProviderQualificationAuthorityError(
                "unsupported provider qualification campaign schema"
            )
        if type(raw["case_results"]) is not list:
            raise ProviderQualificationAuthorityError(
                "case_results must be an exact list"
            )
        cases: list[ProviderQualificationCaseResult] = []
        for index, item in enumerate(raw["case_results"]):
            case = _strict_keys(
                item,
                name=f"case_results[{index}]",
                keys=frozenset({"case_id", "result"}),
            )
            cases.append(
                ProviderQualificationCaseResult(
                    case_id=case["case_id"], result=case["result"]
                )
            )
        provider_scope = provider_financial_scope(
            provider_id=raw["provider_id"],
            environment=raw["environment"],
            provider_environment=raw["provider_environment"],
            route_policy_id=raw["route_policy_id"],
        )
        live_authorized = raw["live_authorized"]
        if type(live_authorized) is not bool:
            raise ProviderQualificationAuthorityError(
                "live_authorized must be exact bool"
            )
        return cls(
            provider_scope=provider_scope,
            product_family=raw["product_family"],
            entity_policy_id=raw["entity_policy_id"],
            network_policy_id=raw["network_policy_id"],
            account_class=raw["account_class"],
            adapter_source_sha=raw["adapter_source_sha"],
            campaign_protocol_id=raw["campaign_protocol_id"],
            campaign_protocol_version=raw["campaign_protocol_version"],
            required_case_set_version=raw["required_case_set_version"],
            case_results=tuple(cases),
            unsupported_features=_string_list(
                raw["unsupported_features"],
                name="unsupported_features",
                upper=False,
            ),
            documentation_revisions=_string_list(
                raw["documentation_revisions"],
                name="documentation_revisions",
                upper=False,
                allow_empty=False,
            ),
            campaign_artifact_id=raw["campaign_artifact_id"],
            campaign_sha256=campaign_sha256,
            started_at=raw["started_at"],
            completed_at=raw["completed_at"],
            valid_until=raw["valid_until"],
            release_artifact_id=raw["release_artifact_id"],
            release_artifact_sha256=raw["release_artifact_sha256"],
            live_authorized=live_authorized,
            reconciliation_semantics=_parse_reconciliation(
                raw["reconciliation_semantics"]
            ),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "account_class": self.account_class,
            "adapter_source_sha": self.adapter_source_sha,
            "campaign_artifact_id": self.campaign_artifact_id,
            "campaign_protocol_id": self.campaign_protocol_id,
            "campaign_protocol_version": self.campaign_protocol_version,
            "case_results": [item.canonical() for item in self.case_results],
            "completed_at": self.completed_at,
            "documentation_revisions": list(self.documentation_revisions),
            "entity_policy_id": self.entity_policy_id,
            "environment": self.provider_scope.environment,
            "live_authorized": self.live_authorized,
            "network_policy_id": self.network_policy_id,
            "product_family": self.product_family,
            "provider_environment": self.provider_scope.provider_environment,
            "provider_id": self.provider_scope.provider_id,
            "reconciliation_semantics": (
                self.reconciliation_semantics.canonical()
                if self.reconciliation_semantics is not None
                else None
            ),
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "required_case_set_version": self.required_case_set_version,
            "route_policy_id": self.provider_scope.route_policy_id,
            "schema_version": _CAMPAIGN_SCHEMA,
            "started_at": self.started_at,
            "unsupported_features": list(self.unsupported_features),
            "valid_until": self.valid_until,
        }

    @property
    def reconciliation_semantics_id(self) -> str | None:
        if self.reconciliation_semantics is None:
            return None
        return self.reconciliation_semantics.content_sha256


def _ref_payload(ref: EvidenceArtifactRef) -> dict[str, str]:
    return {
        "artifact_id": ref.artifact_id,
        "evidence_kind": ref.evidence_kind,
        "media_type": ref.media_type,
        "sha256": ref.sha256,
        "source_sha": ref.source_sha,
    }


def _accepted_attestation_payload(
    accepted: AcceptedQualificationAttestation,
) -> dict[str, object]:
    return {
        "attestation_digest": accepted.attestation_digest,
        "attestation_id": accepted.attestation_id,
        "attestation_json": accepted.attestation_json,
        "completed_at": accepted.completed_at,
        "domain": accepted.domain,
        "evidence_refs": [_ref_payload(ref) for ref in accepted.evidence_refs],
        "gate": accepted.gate,
        "harness_version": accepted.harness_version,
        "package_id": accepted.package_id,
        "policy_id": accepted.policy_id,
        "policy_version": accepted.policy_version,
        "producer_id": accepted.producer_id,
        "protocol_id": accepted.protocol_id,
        "protocol_version": accepted.protocol_version,
        "release_artifact_id": accepted.release_artifact_id,
        "release_artifact_sha256": accepted.release_artifact_sha256,
        "requirement_id": accepted.requirement_id,
        "requirement_ids": list(accepted.requirement_ids),
        "result": accepted.result,
        "runner_id": accepted.runner_id,
        "schema_version": accepted.schema_version,
        "signature_b64": accepted.signature_b64,
        "signed_at": accepted.signed_at,
        "source_sha": accepted.source_sha,
        "started_at": accepted.started_at,
        "trust_root_id": accepted.trust_root_id,
        "unresolved_limits": list(accepted.unresolved_limits),
        "verification_method": accepted.verification_method,
        "verifier_id": accepted.verifier_id,
    }


@dataclass(frozen=True, slots=True, weakref_slot=True)
class AcceptedProviderQualification:
    qualification_id: str
    campaign: ProviderQualificationCampaign
    attestation: Mapping[str, object]
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _ACCEPTED_TOKEN:
            raise ProviderQualificationAuthorityError(
                "accepted provider qualification must come from the issuer"
            )
        if type(self.campaign) is not ProviderQualificationCampaign:
            raise ProviderQualificationAuthorityError(
                "campaign must be exact ProviderQualificationCampaign"
            )
        if type(self.attestation) is not MappingProxyType:
            raise ProviderQualificationAuthorityError(
                "accepted attestation authority must be sealed"
            )
        payload = self.authority_payload()
        expected = "sha256:" + sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        if self.qualification_id != expected:
            raise ProviderQualificationAuthorityError(
                "qualification_id does not match accepted provider content"
            )

    @property
    def provider_scope(self) -> ProviderFinancialScope:
        return self.campaign.provider_scope

    @property
    def reconciliation_semantics_id(self) -> str | None:
        semantics_content = self.campaign.reconciliation_semantics_id
        if semantics_content is None:
            return None
        material = {
            "accepted_provider_qualification_id": self.qualification_id,
            "reconciliation_semantics_content_sha256": semantics_content,
            "schema_version": _RECONCILIATION_SCHEMA,
        }
        return "sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()

    @property
    def accepted_at(self) -> str:
        return _utc_text(
            self.attestation.get("signed_at"),
            name="accepted attestation signed_at",
        )

    def scope_payload(self) -> dict[str, object]:
        return {
            "account_class": self.campaign.account_class,
            "adapter_source_sha": self.campaign.adapter_source_sha,
            "entity_policy_id": self.campaign.entity_policy_id,
            "network_policy_id": self.campaign.network_policy_id,
            "product_family": self.campaign.product_family,
            "provider_scope": dict(self.campaign.provider_scope.payload),
            "release_artifact_id": self.campaign.release_artifact_id,
            "release_artifact_sha256": self.campaign.release_artifact_sha256,
        }

    @property
    def aggregate_id(self) -> str:
        return "provider-qualification:sha256:" + sha256(
            canonical_json(self.scope_payload()).encode("utf-8")
        ).hexdigest()

    def authority_payload(self) -> dict[str, object]:
        return {
            "accepted_attestation": dict(self.attestation),
            "campaign": self.campaign.canonical_payload(),
            "campaign_sha256": self.campaign.campaign_sha256,
            "schema_version": _ACCEPTED_SCHEMA,
        }

    def record_payload(self) -> dict[str, object]:
        return {
            **self.authority_payload(),
            "qualification_id": self.qualification_id,
        }


def _issued_qualification_digest(value: AcceptedProviderQualification) -> str:
    if type(value) is not AcceptedProviderQualification:
        raise TypeError(
            "qualification must be exact AcceptedProviderQualification"
        )
    return payload_digest(value.record_payload())


def _seal_issued_provider_qualification(
    value: AcceptedProviderQualification,
) -> AcceptedProviderQualification:
    key = id(value)
    digest = _issued_qualification_digest(value)

    def cleanup(
        ref: weakref.ReferenceType[AcceptedProviderQualification],
    ) -> None:
        with _ISSUED_QUALIFICATION_LOCK:
            current = _ISSUED_QUALIFICATIONS.get(key)
            if current is not None and current[0] is ref:
                _ISSUED_QUALIFICATIONS.pop(key, None)

    ref = weakref.ref(value, cleanup)
    with _ISSUED_QUALIFICATION_LOCK:
        _ISSUED_QUALIFICATIONS[key] = (ref, digest)
    return value


def _require_unmodified_issued_provider_qualification(
    value: object,
) -> AcceptedProviderQualification:
    if type(value) is not AcceptedProviderQualification:
        raise TypeError(
            "qualification must be exact AcceptedProviderQualification"
        )
    digest = _issued_qualification_digest(value)
    with _ISSUED_QUALIFICATION_LOCK:
        issued = _ISSUED_QUALIFICATIONS.get(id(value))
    if issued is None or issued[0]() is not value or issued[1] != digest:
        raise ProviderQualificationAuthorityError(
            "provider qualification changed after issuance or was not issued "
            "by this process"
        )
    return value


def issue_accepted_provider_qualification(
    *,
    accepted_attestation: AcceptedQualificationAttestation,
    campaign_payload: object,
) -> AcceptedProviderQualification:
    """Derive one historical accepted-Q value from already verified trust input.

    This first durable slice deliberately does not claim current route authority.
    require_current() remains fail-closed until product-owned revalidation is
    composed with the canonical signed-attestation verifier and retained bytes.
    """

    if type(accepted_attestation) is not AcceptedQualificationAttestation:
        raise TypeError(
            "accepted_attestation must be exact AcceptedQualificationAttestation"
        )
    raw_campaign = _strict_keys(
        campaign_payload,
        name="provider qualification campaign",
        keys=_CAMPAIGN_KEYS,
    )
    campaign_artifact_id = _uuid(
        raw_campaign["campaign_artifact_id"],
        name="campaign_artifact_id",
    )
    matching = [
        ref
        for ref in accepted_attestation.evidence_refs
        if ref.artifact_id == campaign_artifact_id
    ]
    if len(matching) != 1:
        raise ProviderQualificationAuthorityError(
            "accepted attestation must cover exactly one campaign artifact"
        )
    ref = matching[0]
    campaign_bytes = canonical_json(raw_campaign).encode("utf-8")
    observed_campaign_sha256 = "sha256:" + sha256(campaign_bytes).hexdigest()
    if observed_campaign_sha256 != ref.sha256:
        raise ProviderQualificationAuthorityError(
            "canonical campaign payload does not match accepted artifact digest"
        )
    campaign = ProviderQualificationCampaign.from_mapping(
        raw_campaign,
        campaign_sha256=ref.sha256,
    )
    if accepted_attestation.result != "PASS":
        raise ProviderQualificationAuthorityError(
            "provider qualification requires accepted PASS attestation"
        )
    if accepted_attestation.unresolved_limits:
        raise ProviderQualificationAuthorityError(
            "provider qualification attestation has unresolved limits"
        )
    if (
        accepted_attestation.domain != _PROVIDER_DOMAIN
        or accepted_attestation.gate != _PROVIDER_GATE
        or accepted_attestation.package_id != _PROVIDER_PACKAGE
    ):
        raise ProviderQualificationAuthorityError(
            "accepted attestation is not provider-qualification authority"
        )
    if accepted_attestation.source_sha != campaign.adapter_source_sha:
        raise ProviderQualificationAuthorityError(
            "campaign adapter source does not match accepted source"
        )
    if (
        accepted_attestation.protocol_id != campaign.campaign_protocol_id
        or accepted_attestation.protocol_version
        != campaign.campaign_protocol_version
    ):
        raise ProviderQualificationAuthorityError(
            "campaign protocol does not match accepted attestation"
        )
    if (
        campaign.required_case_set_version
        not in accepted_attestation.requirement_ids
    ):
        raise ProviderQualificationAuthorityError(
            "accepted attestation does not cover the campaign case-set version"
        )
    if any(item.result != "PASS" for item in campaign.case_results):
        raise ProviderQualificationAuthorityError(
            "all required provider qualification cases must PASS"
        )
    if (
        accepted_attestation.release_artifact_id
        != campaign.release_artifact_id
        or accepted_attestation.release_artifact_sha256
        != campaign.release_artifact_sha256
    ):
        raise ProviderQualificationAuthorityError(
            "campaign release identity does not match accepted attestation"
        )
    if not (
        _instant(accepted_attestation.started_at)
        <= _instant(campaign.started_at)
        <= _instant(campaign.completed_at)
        <= _instant(accepted_attestation.completed_at)
        <= _instant(accepted_attestation.signed_at)
    ):
        raise ProviderQualificationAuthorityError(
            "campaign chronology is outside accepted attestation chronology"
        )
    if (
        ref.evidence_kind != _CAMPAIGN_EVIDENCE_KIND
        or ref.source_sha != campaign.adapter_source_sha
    ):
        raise ProviderQualificationAuthorityError(
            "accepted campaign artifact binding is inconsistent"
        )
    sealed = MappingProxyType(_accepted_attestation_payload(accepted_attestation))
    authority = {
        "accepted_attestation": dict(sealed),
        "campaign": campaign.canonical_payload(),
        "campaign_sha256": campaign.campaign_sha256,
        "schema_version": _ACCEPTED_SCHEMA,
    }
    qualification_id = "sha256:" + sha256(
        canonical_json(authority).encode("utf-8")
    ).hexdigest()
    issued = AcceptedProviderQualification(
        qualification_id=qualification_id,
        campaign=campaign,
        attestation=sealed,
        _token=_ACCEPTED_TOKEN,
    )
    return _seal_issued_provider_qualification(issued)


def _campaign_from_record(
    value: object,
    *,
    campaign_sha256: object,
) -> ProviderQualificationCampaign:
    return ProviderQualificationCampaign.from_mapping(
        value,
        campaign_sha256=campaign_sha256,
    )


def _accepted_from_record(value: object) -> AcceptedProviderQualification:
    raw = _strict_keys(
        value,
        name="accepted provider qualification",
        keys=frozenset(
            {
                "schema_version",
                "qualification_id",
                "campaign",
                "campaign_sha256",
                "accepted_attestation",
            }
        ),
    )
    if raw["schema_version"] != _ACCEPTED_SCHEMA:
        raise ProviderQualificationAuthorityError(
            "unsupported accepted provider qualification schema"
        )
    campaign = _campaign_from_record(
        raw["campaign"],
        campaign_sha256=raw["campaign_sha256"],
    )
    attestation = raw["accepted_attestation"]
    if type(attestation) is not dict:
        raise ProviderQualificationAuthorityError(
            "accepted attestation record must be an exact object"
        )
    sealed = MappingProxyType(dict(attestation))
    qualification_id = _digest(
        raw["qualification_id"], name="qualification_id"
    )
    return AcceptedProviderQualification(
        qualification_id=qualification_id,
        campaign=campaign,
        attestation=sealed,
        _token=_ACCEPTED_TOKEN,
    )


@dataclass
class _ScopeState:
    current: AcceptedProviderQualification | None = None
    history: dict[str, AcceptedProviderQualification] = field(default_factory=dict)
    superseded_by: dict[str, str] = field(default_factory=dict)
    revoked: dict[str, tuple[str, str]] = field(default_factory=dict)


class DurableProviderQualificationRegistry:
    """Journal-backed immutable provider-Q history.

    Historical replay is available after restart.  Terminal current-Q authority
    remains deliberately unavailable until a product-owned revalidation service
    is composed; require_current() therefore always fails closed in this slice.
    """

    def __init__(self, store: JournalStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        self.store = store

    def _validated_history_cut(
        self,
    ) -> tuple[dict[str, _ScopeState], dict[str, int]]:
        states: dict[str, _ScopeState] = {}
        versions: dict[str, int] = {}
        events = self.store.load_events_by_aggregate_type(_AGGREGATE_TYPE)
        for event in events:
            if event["aggregate_type"] != _AGGREGATE_TYPE:
                raise ProviderQualificationAuthorityError(
                    "provider qualification event uses wrong aggregate type"
                )
            event_type = event["event_type"]
            if event_type not in {
                _EVENT_ACCEPT,
                _EVENT_SUPERSEDE,
                _EVENT_REVOKE,
            }:
                raise ProviderQualificationAuthorityError(
                    "unsupported provider qualification event type"
                )
            aggregate_id = event["aggregate_id"]
            expected_version = versions.get(aggregate_id, 0) + 1
            if event["aggregate_version"] != expected_version:
                raise ProviderQualificationAuthorityError(
                    "provider qualification aggregate version gap"
                )
            versions[aggregate_id] = expected_version
            if payload_digest(event["payload"]) != event["payload_hash"]:
                raise ProviderQualificationAuthorityError(
                    "provider qualification payload integrity failure"
                )
            payload = event["payload"]
            state = states.setdefault(aggregate_id, _ScopeState())
            if event_type == _EVENT_ACCEPT:
                raw = _strict_keys(
                    payload,
                    name="accept transition",
                    keys=frozenset({"transition", "qualification"}),
                )
                if raw["transition"] != "ACCEPT":
                    raise ProviderQualificationAuthorityError(
                        "accept transition payload is inconsistent"
                    )
                qualification = _accepted_from_record(raw["qualification"])
                if qualification.aggregate_id != aggregate_id:
                    raise ProviderQualificationAuthorityError(
                        "accepted qualification aggregate identity mismatch"
                    )
                expected_event_id = self._event_id(
                    "ACCEPT\n" + qualification.qualification_id
                )
                if event["event_id"] != expected_event_id:
                    raise ProviderQualificationAuthorityError(
                        "accepted qualification event identity is not canonical"
                    )
                if event["committed_at"] != qualification.accepted_at:
                    raise ProviderQualificationAuthorityError(
                        "accepted qualification event chronology is inconsistent"
                    )
                if state.current is not None or state.history:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification ACCEPT is not an initial transition"
                    )
                state.current = qualification
                state.history[qualification.qualification_id] = qualification
                continue
            if event_type == _EVENT_SUPERSEDE:
                raw = _strict_keys(
                    payload,
                    name="supersede transition",
                    keys=frozenset(
                        {
                            "transition",
                            "previous_qualification_id",
                            "qualification",
                        }
                    ),
                )
                if raw["transition"] != "SUPERSEDE":
                    raise ProviderQualificationAuthorityError(
                        "supersede transition payload is inconsistent"
                    )
                previous = _digest(
                    raw["previous_qualification_id"],
                    name="previous_qualification_id",
                )
                replacement = _accepted_from_record(raw["qualification"])
                if replacement.aggregate_id != aggregate_id:
                    raise ProviderQualificationAuthorityError(
                        "replacement qualification aggregate identity mismatch"
                    )
                expected_event_id = self._event_id(
                    "SUPERSEDE\n"
                    + previous
                    + "\n"
                    + replacement.qualification_id
                )
                if event["event_id"] != expected_event_id:
                    raise ProviderQualificationAuthorityError(
                        "replacement qualification event identity is not canonical"
                    )
                if event["committed_at"] != replacement.accepted_at:
                    raise ProviderQualificationAuthorityError(
                        "replacement qualification event chronology is inconsistent"
                    )
                if (
                    state.current is None
                    or state.current.qualification_id != previous
                ):
                    raise ProviderQualificationAuthorityError(
                        "provider qualification supersession does not match current Q"
                    )
                if replacement.qualification_id in state.history:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification replacement reuses historical Q"
                    )
                if _instant(replacement.accepted_at) < _instant(
                    state.current.accepted_at
                ):
                    raise ProviderQualificationAuthorityError(
                        "provider qualification supersession backdates accepted authority"
                    )
                state.history[replacement.qualification_id] = replacement
                state.superseded_by[previous] = replacement.qualification_id
                state.current = replacement
                continue
            raw = _strict_keys(
                payload,
                name="revoke transition",
                keys=frozenset(
                    {
                        "transition",
                        "qualification_id",
                        "reason",
                        "revoked_at",
                    }
                ),
            )
            if raw["transition"] != "REVOKE":
                raise ProviderQualificationAuthorityError(
                    "revoke transition payload is inconsistent"
                )
            qualification_id = _digest(
                raw["qualification_id"], name="qualification_id"
            )
            reason = _token(raw["reason"], name="revocation reason", upper=False)
            revoked_at = _utc_text(raw["revoked_at"], name="revoked_at")
            expected_event_id = self._event_id(
                "REVOKE\n"
                + qualification_id
                + "\n"
                + revoked_at
                + "\n"
                + reason
            )
            if event["event_id"] != expected_event_id:
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation event identity is not canonical"
                )
            if (
                state.current is None
                or state.current.qualification_id != qualification_id
            ):
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation does not match current Q"
                )
            if event["committed_at"] != revoked_at:
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation chronology is inconsistent"
                )
            if _instant(revoked_at) < _instant(state.current.accepted_at):
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation backdates accepted authority"
                )
            state.revoked[qualification_id] = (revoked_at, reason)
            state.current = None
        return states, versions

    @staticmethod
    def _event_id(material: str) -> str:
        return "provider-qualification:" + sha256(
            material.encode("utf-8")
        ).hexdigest()

    def _append(
        self,
        *,
        event_type: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: dict[str, object],
        committed_at: str,
        event_material: str,
    ) -> bool:
        envelope = {
            "event_id": self._event_id(event_material),
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        try:
            return self.store.append_event(envelope).inserted
        except ValueError as error:
            raise ProviderQualificationAuthorityError(
                "provider qualification history changed concurrently; refresh required"
            ) from error

    def register(self, qualification: AcceptedProviderQualification) -> bool:
        qualification = _require_unmodified_issued_provider_qualification(
            qualification
        )
        states, versions = self._validated_history_cut()
        aggregate_id = qualification.aggregate_id
        state = states.get(aggregate_id)
        if state is not None:
            if (
                state.current is not None
                and state.current.qualification_id == qualification.qualification_id
            ):
                return False
            if qualification.qualification_id in state.history:
                raise ProviderQualificationAuthorityError(
                    "historical provider qualification cannot be re-accepted"
                )
            if state.current is not None:
                raise ProviderQualificationAuthorityError(
                    "current provider qualification exists; explicit supersession required"
                )
            raise ProviderQualificationAuthorityError(
                "revoked provider qualification scope requires a new explicit authority generation"
            )
        payload = {
            "transition": "ACCEPT",
            "qualification": qualification.record_payload(),
        }
        return self._append(
            event_type=_EVENT_ACCEPT,
            aggregate_id=aggregate_id,
            aggregate_version=versions.get(aggregate_id, 0) + 1,
            payload=payload,
            committed_at=qualification.accepted_at,
            event_material="ACCEPT\n" + qualification.qualification_id,
        )

    def supersede(
        self,
        previous_qualification_id: str,
        replacement: AcceptedProviderQualification,
    ) -> bool:
        previous = _digest(
            previous_qualification_id, name="previous_qualification_id"
        )
        replacement = _require_unmodified_issued_provider_qualification(
            replacement
        )
        states, versions = self._validated_history_cut()
        aggregate_id = replacement.aggregate_id
        state = states.get(aggregate_id)
        if state is None:
            raise ProviderQualificationAuthorityError(
                "provider qualification scope has no accepted Q to supersede"
            )
        if (
            state.current is not None
            and state.current.qualification_id == replacement.qualification_id
            and state.superseded_by.get(previous) == replacement.qualification_id
        ):
            return False
        if state.current is None or state.current.qualification_id != previous:
            raise ProviderQualificationAuthorityError(
                "supersession does not target current provider qualification"
            )
        if replacement.qualification_id in state.history:
            raise ProviderQualificationAuthorityError(
                "replacement provider qualification already exists in history"
            )
        if _instant(replacement.accepted_at) < _instant(state.current.accepted_at):
            raise ProviderQualificationAuthorityError(
                "provider qualification supersession backdates accepted authority"
            )
        payload = {
            "transition": "SUPERSEDE",
            "previous_qualification_id": previous,
            "qualification": replacement.record_payload(),
        }
        return self._append(
            event_type=_EVENT_SUPERSEDE,
            aggregate_id=aggregate_id,
            aggregate_version=versions.get(aggregate_id, 0) + 1,
            payload=payload,
            committed_at=replacement.accepted_at,
            event_material=(
                "SUPERSEDE\n"
                + previous
                + "\n"
                + replacement.qualification_id
            ),
        )

    def revoke(
        self,
        qualification_id: str,
        *,
        revoked_at: str,
        reason: str,
    ) -> bool:
        qualification_id = _digest(
            qualification_id, name="qualification_id"
        )
        revoked_at = _utc_text(revoked_at, name="revoked_at")
        reason = _token(reason, name="revocation reason", upper=False)
        states, versions = self._validated_history_cut()
        matching: list[tuple[str, _ScopeState]] = []
        historical_revocation: tuple[str, str] | None = None
        for aggregate_id, state in states.items():
            if (
                state.current is not None
                and state.current.qualification_id == qualification_id
            ):
                matching.append((aggregate_id, state))
            if qualification_id in state.revoked:
                historical_revocation = state.revoked[qualification_id]
        if not matching:
            if historical_revocation == (revoked_at, reason):
                return False
            raise ProviderQualificationAuthorityError(
                "qualification_id is not the current provider qualification"
            )
        if len(matching) != 1:
            raise ProviderQualificationAuthorityError(
                "qualification_id is ambiguous across provider qualification scopes"
            )
        aggregate_id, state = matching[0]
        if state.current is None:
            raise ProviderQualificationAuthorityError(
                "provider qualification revocation lost current authority"
            )
        if _instant(revoked_at) < _instant(state.current.accepted_at):
            raise ProviderQualificationAuthorityError(
                "provider qualification revocation backdates accepted authority"
            )
        payload = {
            "transition": "REVOKE",
            "qualification_id": qualification_id,
            "reason": reason,
            "revoked_at": revoked_at,
        }
        return self._append(
            event_type=_EVENT_REVOKE,
            aggregate_id=aggregate_id,
            aggregate_version=versions.get(aggregate_id, 0) + 1,
            payload=payload,
            committed_at=revoked_at,
            event_material=(
                "REVOKE\n"
                + qualification_id
                + "\n"
                + revoked_at
                + "\n"
                + reason
            ),
        )

    def historical(self, qualification_id: str) -> AcceptedProviderQualification:
        qualification_id = _digest(
            qualification_id, name="qualification_id"
        )
        states, _versions = self._validated_history_cut()
        found: list[AcceptedProviderQualification] = []
        for state in states.values():
            value = state.history.get(qualification_id)
            if value is not None:
                found.append(value)
        if len(found) != 1:
            raise ProviderQualificationAuthorityError(
                "historical provider qualification is missing or ambiguous"
            )
        return found[0]

    def historical_current_for(
        self, qualification: AcceptedProviderQualification
    ) -> AcceptedProviderQualification | None:
        if type(qualification) is not AcceptedProviderQualification:
            raise TypeError(
                "qualification must be exact AcceptedProviderQualification"
            )
        states, _versions = self._validated_history_cut()
        state = states.get(qualification.aggregate_id)
        return None if state is None else state.current

    def historical_current_for_route(
        self,
        *,
        provider_id: str,
        product_family: str,
        environment: str,
        provider_environment: str,
        route_policy_id: str,
        entity_policy_id: str,
        network_policy_id: str,
        account_class: str,
        adapter_source_sha: str,
        release_artifact_id: str | None = None,
        release_artifact_sha256: str | None = None,
    ) -> AcceptedProviderQualification:
        """Resolve one historical current-Q by the complete route/build scope.

        This method is historical lookup only.  Terminal currentness still
        requires ProviderQualificationCurrentReader revalidation.
        """

        provider = _token(provider_id, name="provider_id")
        family = _token(product_family, name="product_family")
        runtime_environment = _token(environment, name="environment")
        provider_domain = _token(
            provider_environment,
            name="provider_environment",
        )
        route_policy = _token(
            route_policy_id,
            name="route_policy_id",
            upper=False,
        )
        entity_policy = _token(
            entity_policy_id,
            name="entity_policy_id",
            upper=False,
        )
        network_policy = _token(
            network_policy_id,
            name="network_policy_id",
            upper=False,
        )
        account = _token(account_class, name="account_class")
        adapter = _git_sha(adapter_source_sha, name="adapter_source_sha")
        if (release_artifact_id is None) != (
            release_artifact_sha256 is None
        ):
            raise ProviderQualificationAuthorityError(
                "release artifact identity and digest must be supplied together"
            )
        release_id = (
            None
            if release_artifact_id is None
            else _uuid(release_artifact_id, name="release_artifact_id")
        )
        release_sha = (
            None
            if release_artifact_sha256 is None
            else _digest(
                release_artifact_sha256,
                name="release_artifact_sha256",
            )
        )

        states, _versions = self._validated_history_cut()
        matches: list[AcceptedProviderQualification] = []
        for state in states.values():
            current = state.current
            if current is None:
                continue
            campaign = current.campaign
            scope = current.provider_scope
            if (
                scope.provider_id == provider
                and campaign.product_family == family
                and scope.environment == runtime_environment
                and scope.provider_environment == provider_domain
                and scope.route_policy_id == route_policy
                and campaign.entity_policy_id == entity_policy
                and campaign.network_policy_id == network_policy
                and campaign.account_class == account
                and campaign.adapter_source_sha == adapter
                and campaign.release_artifact_id == release_id
                and campaign.release_artifact_sha256 == release_sha
            ):
                matches.append(current)
        if len(matches) != 1:
            raise ProviderQualificationCurrentnessUnavailable(
                "exact current provider qualification route scope is missing or ambiguous"
            )
        return matches[0]

    def require_current(
        self, qualification_id: str
    ) -> AcceptedProviderQualification:
        # Validate that the caller is referring to a real historical Q, but never
        # turn durable history into terminal currentness merely after restart.
        self.historical(qualification_id)
        raise ProviderQualificationCurrentnessUnavailable(
            "current provider qualification requires product-owned revalidation"
        )


class ProviderQualificationCurrentReader:
    """Revalidate durable Q against canonical signer trust and retained campaign bytes.

    The object captures one sealed authenticated artifact reader at construction.
    Callers cannot supply a verifier, reader callback, trust policy, or decision
    timestamp to require_current().  Historical JournalStore state alone remains
    insufficient: every current read re-verifies the signed attestation and exact
    retained campaign artifact, then rechecks the durable current-Q cut.
    """

    def __init__(
        self,
        registry: DurableProviderQualificationRegistry,
        *,
        evidence_store: ArtifactStore,
        evidence_root: str | Path,
    ) -> None:
        if type(registry) is not DurableProviderQualificationRegistry:
            raise TypeError(
                "registry must be exact DurableProviderQualificationRegistry"
            )
        if type(evidence_store) is not ArtifactStore:
            raise TypeError("evidence_store must be the canonical ArtifactStore")
        try:
            reader = trusted_authenticated_reader(
                evidence_root,
                publication_store=evidence_store,
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderQualificationAuthorityError(
                "provider qualification evidence authority cannot be bound"
            ) from error
        self._registry = registry
        self._evidence_store = evidence_store
        self._evidence_root = Path(evidence_root)
        self._read_snapshot = reader

    @staticmethod
    def _campaign_ref(
        qualification: AcceptedProviderQualification,
    ) -> dict[str, object]:
        refs = qualification.attestation.get("evidence_refs")
        if type(refs) is not list:
            raise ProviderQualificationAuthorityError(
                "accepted qualification evidence refs are not canonical"
            )
        matches = [
            item
            for item in refs
            if type(item) is dict
            and item.get("artifact_id")
            == qualification.campaign.campaign_artifact_id
        ]
        if len(matches) != 1:
            raise ProviderQualificationAuthorityError(
                "accepted qualification must bind exactly one campaign artifact"
            )
        return matches[0]

    @staticmethod
    def _current_point() -> datetime:
        point = _utc_now()
        if (
            type(point) is not datetime
            or point.tzinfo is None
            or point.utcoffset() is None
        ):
            raise ProviderQualificationAuthorityError(
                "provider qualification current-time authority is invalid"
            )
        return point.astimezone(timezone.utc)

    @staticmethod
    def _require_fresh(
        qualification: AcceptedProviderQualification,
        point: datetime,
    ) -> None:
        if (
            point < _instant(qualification.accepted_at)
            or point >= _instant(qualification.campaign.valid_until)
        ):
            raise ProviderQualificationCurrentnessUnavailable(
                "provider qualification is not current at the product time cut"
            )

    def _reverify_attestation(
        self,
        qualification: AcceptedProviderQualification,
    ) -> None:
        raw_json = qualification.attestation.get("attestation_json")
        signature_b64 = qualification.attestation.get("signature_b64")
        if type(raw_json) is not str or type(signature_b64) is not str:
            raise ProviderQualificationAuthorityError(
                "durable accepted attestation material is incomplete"
            )
        try:
            attestation_payload = strict_json_loads(raw_json)
            receipt = parse_signed_qualification_attestation(
                {
                    "attestation": attestation_payload,
                    "signature_b64": signature_b64,
                }
            )
            accepted = verify_canonical_qualification_attestation(
                receipt,
                evidence_store=self._evidence_store,
                evidence_root=self._evidence_root,
                expected_source_sha=qualification.campaign.adapter_source_sha,
                expected_domain=_PROVIDER_DOMAIN,
                expected_gate=_PROVIDER_GATE,
                expected_package_id=_PROVIDER_PACKAGE,
                expected_protocol_id=qualification.campaign.campaign_protocol_id,
                expected_protocol_version=(
                    qualification.campaign.campaign_protocol_version
                ),
                expected_requirement_id=(
                    qualification.campaign.required_case_set_version
                ),
                expected_release_artifact_id=(
                    qualification.campaign.release_artifact_id
                ),
                expected_release_artifact_sha256=(
                    qualification.campaign.release_artifact_sha256
                ),
            )
        except (
            ArtifactIntegrityError,
            QualificationTrustError,
            TypeError,
            ValueError,
        ) as error:
            raise ProviderQualificationAuthorityError(
                "provider qualification signed authority cannot be reverified"
            ) from error
        if _accepted_attestation_payload(accepted) != dict(
            qualification.attestation
        ):
            raise ProviderQualificationAuthorityError(
                "reverified qualification attestation differs from durable Q"
            )

    def _reverify_campaign(
        self,
        qualification: AcceptedProviderQualification,
    ) -> None:
        ref = self._campaign_ref(qualification)
        try:
            manifest, data = self._read_snapshot(
                qualification.campaign.campaign_artifact_id
            )
        except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
            raise ProviderQualificationAuthorityError(
                "provider qualification campaign evidence cannot be read"
            ) from error
        if type(manifest) is not dict or type(data) is not bytes:
            raise ProviderQualificationAuthorityError(
                "provider qualification campaign snapshot is not canonical"
            )
        metadata = manifest.get("metadata")
        source_refs = manifest.get("source_refs")
        if (
            manifest.get("artifact_id")
            != qualification.campaign.campaign_artifact_id
            or manifest.get("sha256") != qualification.campaign.campaign_sha256
            or manifest.get("media_type") != ref.get("media_type")
            or type(metadata) is not dict
            or metadata.get("evidence_kind") != _CAMPAIGN_EVIDENCE_KIND
            or type(source_refs) is not list
            or f"git:{qualification.campaign.adapter_source_sha}"
            not in source_refs
        ):
            raise ProviderQualificationAuthorityError(
                "provider qualification campaign manifest conflicts with durable Q"
            )
        observed_digest = "sha256:" + sha256(data).hexdigest()
        if observed_digest != qualification.campaign.campaign_sha256:
            raise ProviderQualificationAuthorityError(
                "provider qualification campaign bytes changed"
            )
        try:
            text = data.decode("utf-8")
            payload = strict_json_loads(text)
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise ProviderQualificationAuthorityError(
                "provider qualification campaign bytes are not strict UTF-8 JSON"
            ) from error
        if canonical_json(payload) != text:
            raise ProviderQualificationAuthorityError(
                "provider qualification campaign JSON is not canonical"
            )
        reconstructed = ProviderQualificationCampaign.from_mapping(
            payload,
            campaign_sha256=observed_digest,
        )
        if reconstructed != qualification.campaign:
            raise ProviderQualificationAuthorityError(
                "retained provider qualification campaign differs from durable Q"
            )

    def require_current_for_route(
        self,
        *,
        provider_id: str,
        product_family: str,
        environment: str,
        provider_environment: str,
        route_policy_id: str,
        entity_policy_id: str,
        network_policy_id: str,
        account_class: str,
        adapter_source_sha: str,
        release_artifact_id: str | None = None,
        release_artifact_sha256: str | None = None,
        expected_qualification_id: str | None = None,
    ) -> AcceptedProviderQualification:
        """Resolve and revalidate the exact current Q for one route/build scope."""

        historical = self._registry.historical_current_for_route(
            provider_id=provider_id,
            product_family=product_family,
            environment=environment,
            provider_environment=provider_environment,
            route_policy_id=route_policy_id,
            entity_policy_id=entity_policy_id,
            network_policy_id=network_policy_id,
            account_class=account_class,
            adapter_source_sha=adapter_source_sha,
            release_artifact_id=release_artifact_id,
            release_artifact_sha256=release_artifact_sha256,
        )
        if expected_qualification_id is not None:
            expected = _digest(
                expected_qualification_id,
                name="expected_qualification_id",
            )
            if historical.qualification_id != expected:
                raise ProviderQualificationCurrentnessUnavailable(
                    "expected provider qualification is not current for route scope"
                )
        current = self.require_current(historical.qualification_id)
        if current != historical:
            raise ProviderQualificationCurrentnessUnavailable(
                "provider qualification route scope changed during revalidation"
            )
        return current

    def require_current_scope(
        self,
        *,
        provider_id: str,
        environment: str,
        provider_environment: str,
        route_policy_id: str,
    ) -> AcceptedProviderQualification:
        """Resolve exactly one current accepted Q for one financial route scope.

        The route policy id is decision-relevant authority.  Provider-origin
        callers pass the current canonical route identity here so a campaign
        qualified against an older endpoint/status/entitlement contract cannot
        silently authorize a newer route.
        """

        try:
            expected_scope = provider_financial_scope(
                provider_id=provider_id,
                environment=environment,
                provider_environment=provider_environment,
                route_policy_id=route_policy_id,
            )
        except ProviderDomainError as error:
            raise ProviderQualificationAuthorityError(
                "provider qualification scope is not canonical"
            ) from error

        def current_ids() -> tuple[str, ...]:
            states, _versions = self._registry._validated_history_cut()
            return tuple(
                sorted(
                    state.current.qualification_id
                    for state in states.values()
                    if state.current is not None
                    and state.current.provider_scope == expected_scope
                )
            )

        selected = current_ids()
        if len(selected) != 1:
            raise ProviderQualificationCurrentnessUnavailable(
                "current provider qualification scope is missing or ambiguous"
            )
        qualification = self.require_current(selected[0])
        if qualification.provider_scope != expected_scope:
            raise ProviderQualificationCurrentnessUnavailable(
                "current provider qualification scope changed during revalidation"
            )
        if current_ids() != (qualification.qualification_id,):
            raise ProviderQualificationCurrentnessUnavailable(
                "current provider qualification scope changed during revalidation"
            )
        return qualification

    def require_current(
        self,
        qualification_id: str,
    ) -> AcceptedProviderQualification:
        qualification = self._registry.historical(qualification_id)
        if (
            self._registry.historical_current_for(qualification)
            != qualification
        ):
            raise ProviderQualificationCurrentnessUnavailable(
                "provider qualification is superseded or revoked"
            )
        first_point = self._current_point()
        self._require_fresh(qualification, first_point)
        self._reverify_attestation(qualification)
        self._reverify_campaign(qualification)
        if (
            self._registry.historical_current_for(qualification)
            != qualification
        ):
            raise ProviderQualificationCurrentnessUnavailable(
                "provider qualification changed during revalidation"
            )
        self._require_fresh(qualification, self._current_point())
        return qualification
