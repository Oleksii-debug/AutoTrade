"""Canonical provider-qualification issuance boundary.

A legacy ``QualificationEvidence`` value or a caller-constructed
``AcceptedQualificationAttestation`` is never authority here.  The only public
issuance path first resolves a source-owned protocol, then executes the canonical
signed-attestation verifier against one exact ArtifactStore generation, then
parses the authenticated provider campaign payload and derives a content identity
from all accepted material.

No provider-qualification protocol is source-enabled yet because the canonical
qualification trust policy is not present on this lineage.  Public issuance is
therefore intentionally unavailable rather than silently trusting test material.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .persistence import canonical_json
from .provider_core import REQUIRED_QUALIFICATION_CASES
from .provider_domain import ProviderFinancialScope
from .provider_qualification_identity import ProviderQualificationIdentity
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    QualificationTrustError,
    SignedQualificationAttestation,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)


_SCHEMA_VERSION = "1.0.0"
_MAX_CAMPAIGN_BYTES = 1_048_576
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_ACCEPTED_TOKEN = object()


class ProviderQualificationError(ValueError):
    """Provider qualification could not be established or replayed safely."""


class ProviderQualificationUnavailable(ProviderQualificationError):
    """The source-controlled authority needed to issue provider Q is absent."""


def _text(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderQualificationError(f"{name} must be canonical non-empty text")
    result = value.upper() if upper else value
    if len(result) > 512:
        raise ProviderQualificationError(f"{name} exceeds the canonical text bound")
    return result


def _token(value: object, *, name: str, upper: bool = False) -> str:
    result = _text(value, name=name, upper=upper)
    if _TOKEN_RE.fullmatch(result) is None:
        raise ProviderQualificationError(f"{name} is not canonical")
    return result


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ProviderQualificationError(f"{name} must be canonical sha256:<64-hex>")
    return value


def _git_sha(value: object, *, name: str) -> str:
    if type(value) is not str or _GIT_SHA_RE.fullmatch(value) is None:
        raise ProviderQualificationError(
            f"{name} must be canonical lowercase 40-hex Git SHA"
        )
    return value


def _qid(value: object, *, name: str) -> str:
    if type(value) is not str or _QID_RE.fullmatch(value) is None:
        raise ProviderQualificationError(
            f"{name} must be a canonical provider qualification id"
        )
    return value


def _uuid(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ProviderQualificationError(f"{name} must be a canonical UUID") from error
    if canonical != value:
        raise ProviderQualificationError(f"{name} must be a canonical UUID")
    return value


def _optional_uuid(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _uuid(value, name=name)


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderQualificationError(f"{name} must be a positive exact integer")
    return value


def _instant(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ProviderQualificationError(
            f"{name} must use canonical UTC second precision"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ProviderQualificationError(
            f"{name} must use canonical UTC second precision"
        )
    return value


def _instant_value(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _strict_object(
    value: object,
    *,
    name: str,
    keys: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise ProviderQualificationError(f"{name} must be an exact JSON object")
    actual = frozenset(value)
    if actual != keys:
        raise ProviderQualificationError(
            f"{name} fields mismatch: missing={sorted(keys-actual)} extra={sorted(actual-keys)}"
        )
    return value


def _unique_text_list(value: object, *, name: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise ProviderQualificationError(f"{name} must be an exact JSON array")
    items = tuple(_text(item, name=f"{name} item") for item in value)
    if len(set(items)) != len(items):
        raise ProviderQualificationError(f"{name} must contain unique values")
    canonical = tuple(sorted(items))
    if list(canonical) != value:
        raise ProviderQualificationError(f"{name} must be sorted canonically")
    return canonical


def _canonical_digest(value: object) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _decode_campaign_json(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise ProviderQualificationError("provider qualification campaign bytes are required")
    if len(raw) > _MAX_CAMPAIGN_BYTES:
        raise ProviderQualificationError("provider qualification campaign exceeds size bound")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ProviderQualificationError(
            "provider qualification campaign must be exact UTF-8 JSON"
        ) from error

    def no_duplicates(pairs):
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ProviderQualificationError(
                    "provider qualification campaign contains duplicate JSON keys"
                )
            result[key] = item
        return result

    def bounded_int(token: str) -> int:
        if len(token) > 18:
            raise ProviderQualificationError(
                "provider qualification campaign integer exceeds bound"
            )
        return int(token)

    try:
        value = json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_int=bounded_int,
            parse_float=lambda _value: (_ for _ in ()).throw(
                ProviderQualificationError(
                    "provider qualification campaign forbids JSON floats"
                )
            ),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ProviderQualificationError(
                    "provider qualification campaign forbids non-finite JSON"
                )
            ),
        )
    except ProviderQualificationError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ProviderQualificationError(
            "provider qualification campaign is malformed JSON"
        ) from error
    if type(value) is not dict:
        raise ProviderQualificationError(
            "provider qualification campaign must be a JSON object"
        )
    if canonical_json(value).encode("utf-8") != raw:
        raise ProviderQualificationError(
            "provider qualification campaign bytes are not canonical JSON"
        )
    return value


def _parse_evidence_ref(value: object, *, name: str) -> EvidenceArtifactRef:
    payload = _strict_object(
        value,
        name=name,
        keys=frozenset(
            {"artifact_id", "sha256", "media_type", "evidence_kind", "source_sha"}
        ),
    )
    try:
        ref = EvidenceArtifactRef(
            artifact_id=payload["artifact_id"],
            sha256=payload["sha256"],
            media_type=payload["media_type"],
            evidence_kind=payload["evidence_kind"],
            source_sha=payload["source_sha"],
        )
    except (QualificationTrustError, TypeError, ValueError) as error:
        raise ProviderQualificationError(f"{name} is not canonical") from error
    if ref.canonical() != payload:
        raise ProviderQualificationError(f"{name} is not canonical")
    return ref


@dataclass(frozen=True, slots=True)
class ProviderQualificationProtocol:
    key: str
    domain: str
    gate: str
    package_id: str
    protocol_id: str
    protocol_version: str
    requirement_id: str
    campaign_evidence_kind: str = "PROVIDER_QUALIFICATION_CAMPAIGN"

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _token(self.key, name="protocol key"))
        for name in ("domain", "gate", "package_id", "campaign_evidence_kind"):
            object.__setattr__(
                self,
                name,
                _token(getattr(self, name), name=name, upper=True),
            )
        for name in ("protocol_id", "protocol_version", "requirement_id"):
            object.__setattr__(
                self,
                name,
                _token(getattr(self, name), name=name),
            )


# Runtime registration would reintroduce caller-selected trust semantics.  A real
# provider protocol must be added here together with a reviewed canonical trust
# policy.  The empty mapping keeps today's lineage fail-closed.
_SOURCE_PROTOCOLS: Mapping[str, ProviderQualificationProtocol] = MappingProxyType({})


def source_provider_qualification_protocol(
    key: str,
) -> ProviderQualificationProtocol:
    key = _token(key, name="protocol key")
    try:
        protocol = _SOURCE_PROTOCOLS[key]
    except KeyError as error:
        raise ProviderQualificationUnavailable(
            "no source-controlled provider qualification protocol is configured"
        ) from error
    if type(protocol) is not ProviderQualificationProtocol:
        raise ProviderQualificationUnavailable(
            "provider qualification protocol authority is invalid"
        )
    return protocol


@dataclass(frozen=True, slots=True)
class ProviderQualificationScope:
    provider_scope: ProviderFinancialScope
    product_family: str
    adapter_source_git_sha: str
    packaged_artifact_digest: str
    campaign_id: str
    campaign_version: int
    protocol_id: str
    protocol_version: str

    def __post_init__(self) -> None:
        if type(self.provider_scope) is not ProviderFinancialScope:
            raise ProviderQualificationError(
                "provider_scope must be exact ProviderFinancialScope"
            )
        object.__setattr__(
            self,
            "product_family",
            _token(self.product_family, name="product_family", upper=True),
        )
        object.__setattr__(
            self,
            "adapter_source_git_sha",
            _git_sha(self.adapter_source_git_sha, name="adapter_source_git_sha"),
        )
        object.__setattr__(
            self,
            "packaged_artifact_digest",
            _digest(self.packaged_artifact_digest, name="packaged_artifact_digest"),
        )
        object.__setattr__(
            self,
            "campaign_id",
            _token(self.campaign_id, name="campaign_id"),
        )
        object.__setattr__(
            self,
            "campaign_version",
            _positive_int(self.campaign_version, name="campaign_version"),
        )
        object.__setattr__(
            self,
            "protocol_id",
            _token(self.protocol_id, name="protocol_id"),
        )
        object.__setattr__(
            self,
            "protocol_version",
            _token(self.protocol_version, name="protocol_version"),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope": self.provider_scope.payload(),
            "product_family": self.product_family,
            "adapter_source_git_sha": self.adapter_source_git_sha,
            "packaged_artifact_digest": self.packaged_artifact_digest,
            "campaign_id": self.campaign_id,
            "campaign_version": self.campaign_version,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
        }

    @property
    def content_digest(self) -> str:
        return "provider-qualification-scope:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderQualificationCampaign:
    scope: ProviderQualificationScope
    required_cases: tuple[str, ...]
    passed_cases: tuple[str, ...]
    failed_cases: tuple[str, ...]
    unsupported_features: tuple[str, ...]
    route_semantics_json: str
    documentation_revisions: tuple[str, ...]
    completed_at: str
    valid_until: str
    raw_evidence_refs: tuple[EvidenceArtifactRef, ...]
    supersedes_qualification_id: str | None


def parse_provider_qualification_campaign(
    raw: bytes,
) -> ProviderQualificationCampaign:
    value = _strict_object(
        _decode_campaign_json(raw),
        name="provider qualification campaign",
        keys=frozenset(
            {
                "schema_version",
                "provider_id",
                "product_family",
                "runtime_environment",
                "provider_environment",
                "entity_policy_id",
                "adapter_source_git_sha",
                "packaged_artifact_digest",
                "campaign_id",
                "campaign_version",
                "protocol_id",
                "protocol_version",
                "required_cases",
                "passed_cases",
                "failed_cases",
                "unsupported_features",
                "route_semantics",
                "documentation_revisions",
                "completed_at",
                "valid_until",
                "raw_evidence_refs",
                "supersedes_qualification_id",
            }
        ),
    )
    if value["schema_version"] != _SCHEMA_VERSION:
        raise ProviderQualificationError(
            "unsupported provider qualification campaign schema"
        )
    try:
        provider_scope = ProviderFinancialScope(
            provider_id=value["provider_id"],
            runtime_environment=value["runtime_environment"],
            provider_environment=value["provider_environment"],
            entity_policy_id=value["entity_policy_id"],
        )
    except (TypeError, ValueError) as error:
        raise ProviderQualificationError(
            "provider qualification financial scope is invalid"
        ) from error
    scope = ProviderQualificationScope(
        provider_scope=provider_scope,
        product_family=value["product_family"],
        adapter_source_git_sha=value["adapter_source_git_sha"],
        packaged_artifact_digest=value["packaged_artifact_digest"],
        campaign_id=value["campaign_id"],
        campaign_version=value["campaign_version"],
        protocol_id=value["protocol_id"],
        protocol_version=value["protocol_version"],
    )

    required_cases = _unique_text_list(
        value["required_cases"], name="required_cases"
    )
    source_required = tuple(sorted(REQUIRED_QUALIFICATION_CASES))
    if required_cases != source_required:
        raise ProviderQualificationError(
            "required_cases do not equal the source-owned provider case universe"
        )
    passed_cases = _unique_text_list(value["passed_cases"], name="passed_cases")
    failed_cases = _unique_text_list(value["failed_cases"], name="failed_cases")
    if set(passed_cases) & set(failed_cases):
        raise ProviderQualificationError(
            "passed_cases and failed_cases overlap"
        )
    if set(passed_cases) | set(failed_cases) != set(required_cases):
        raise ProviderQualificationError(
            "passed_cases and failed_cases must partition required_cases"
        )
    if failed_cases or passed_cases != required_cases:
        raise ProviderQualificationError(
            "accepted provider qualification campaign must pass every required case"
        )
    unsupported = _unique_text_list(
        value["unsupported_features"], name="unsupported_features"
    )

    semantics = value["route_semantics"]
    if type(semantics) is not dict or not semantics:
        raise ProviderQualificationError(
            "route_semantics must be a non-empty exact JSON object"
        )
    normalized_semantics: dict[str, str] = {}
    for raw_key, raw_value in semantics.items():
        key = _token(raw_key, name="route semantic key")
        item = _text(raw_value, name=f"route semantic {key}")
        if key in normalized_semantics:
            raise ProviderQualificationError(
                "route_semantics keys are not unique"
            )
        normalized_semantics[key] = item
    if list(semantics) != sorted(semantics):
        raise ProviderQualificationError(
            "route_semantics keys must be sorted canonically"
        )
    route_semantics_json = canonical_json(normalized_semantics)
    if route_semantics_json != canonical_json(semantics):
        raise ProviderQualificationError("route_semantics are not canonical")

    docs = _unique_text_list(
        value["documentation_revisions"], name="documentation_revisions"
    )
    if not docs:
        raise ProviderQualificationError(
            "documentation_revisions must not be empty"
        )
    completed_at = _instant(value["completed_at"], name="completed_at")
    valid_until = _instant(value["valid_until"], name="valid_until")
    if _instant_value(valid_until) <= _instant_value(completed_at):
        raise ProviderQualificationError(
            "valid_until must be after completed_at"
        )

    raw_ref_values = value["raw_evidence_refs"]
    if type(raw_ref_values) is not list:
        raise ProviderQualificationError(
            "raw_evidence_refs must be an exact JSON array"
        )
    raw_refs = tuple(
        _parse_evidence_ref(item, name=f"raw_evidence_refs[{index}]")
        for index, item in enumerate(raw_ref_values)
    )
    canonical_refs = tuple(sorted(raw_refs, key=lambda item: item.artifact_id))
    if raw_refs != canonical_refs or len(
        {item.artifact_id for item in raw_refs}
    ) != len(raw_refs):
        raise ProviderQualificationError(
            "raw_evidence_refs must be unique and sorted by artifact_id"
        )

    supersedes = value["supersedes_qualification_id"]
    if supersedes is not None:
        supersedes = _qid(
            supersedes,
            name="supersedes_qualification_id",
        )
    return ProviderQualificationCampaign(
        scope=scope,
        required_cases=required_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        unsupported_features=unsupported,
        route_semantics_json=route_semantics_json,
        documentation_revisions=docs,
        completed_at=completed_at,
        valid_until=valid_until,
        raw_evidence_refs=raw_refs,
        supersedes_qualification_id=supersedes,
    )


@dataclass(frozen=True, slots=True, init=False)
class AcceptedProviderQualification:
    """Sealed Q value.  Normal construction is rejected."""

    identity: ProviderQualificationIdentity
    scope: ProviderQualificationScope
    qualification_id: str
    required_cases: tuple[str, ...]
    unsupported_features: tuple[str, ...]
    route_semantics_json: str
    documentation_revisions: tuple[str, ...]
    completed_at: str
    valid_until: str
    campaign_artifact_ref: EvidenceArtifactRef
    raw_evidence_refs: tuple[EvidenceArtifactRef, ...]
    supersedes_qualification_id: str | None
    attestation_id: str
    attestation_digest: str
    policy_id: str
    policy_version: str
    trust_root_id: str
    producer_id: str
    verifier_id: str
    signed_at: str
    release_artifact_id: str | None

    def __init__(
        self,
        *,
        identity: ProviderQualificationIdentity,
        scope: ProviderQualificationScope,
        required_cases: tuple[str, ...],
        unsupported_features: tuple[str, ...],
        route_semantics_json: str,
        documentation_revisions: tuple[str, ...],
        completed_at: str,
        valid_until: str,
        campaign_artifact_ref: EvidenceArtifactRef,
        raw_evidence_refs: tuple[EvidenceArtifactRef, ...],
        supersedes_qualification_id: str | None,
        attestation_id: str,
        attestation_digest: str,
        policy_id: str,
        policy_version: str,
        trust_root_id: str,
        producer_id: str,
        verifier_id: str,
        signed_at: str,
        release_artifact_id: str | None,
        _issuance_token: object | None = None,
    ) -> None:
        if _issuance_token is not _ACCEPTED_TOKEN:
            raise ProviderQualificationError(
                "AcceptedProviderQualification must come from canonical provider qualification authority"
            )
        if type(identity) is not ProviderQualificationIdentity:
            raise ProviderQualificationError(
                "accepted provider qualification identity is invalid"
            )
        if type(scope) is not ProviderQualificationScope:
            raise ProviderQualificationError(
                "accepted provider qualification scope is invalid"
            )
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "qualification_id", identity.content_digest)
        object.__setattr__(self, "required_cases", tuple(required_cases))
        object.__setattr__(self, "unsupported_features", tuple(unsupported_features))
        object.__setattr__(self, "route_semantics_json", route_semantics_json)
        object.__setattr__(
            self, "documentation_revisions", tuple(documentation_revisions)
        )
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(self, "campaign_artifact_ref", campaign_artifact_ref)
        object.__setattr__(self, "raw_evidence_refs", tuple(raw_evidence_refs))
        object.__setattr__(
            self, "supersedes_qualification_id", supersedes_qualification_id
        )
        object.__setattr__(self, "attestation_id", attestation_id)
        object.__setattr__(self, "attestation_digest", attestation_digest)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(self, "trust_root_id", trust_root_id)
        object.__setattr__(self, "producer_id", producer_id)
        object.__setattr__(self, "verifier_id", verifier_id)
        object.__setattr__(self, "signed_at", signed_at)
        object.__setattr__(self, "release_artifact_id", release_artifact_id)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "qualification_id": self.qualification_id,
            "identity": self.identity.payload(),
            "scope": self.scope.payload(),
            "required_cases": list(self.required_cases),
            "unsupported_features": list(self.unsupported_features),
            "route_semantics": json.loads(self.route_semantics_json),
            "documentation_revisions": list(self.documentation_revisions),
            "completed_at": self.completed_at,
            "valid_until": self.valid_until,
            "campaign_artifact_ref": self.campaign_artifact_ref.canonical(),
            "raw_evidence_refs": [
                item.canonical() for item in self.raw_evidence_refs
            ],
            "supersedes_qualification_id": self.supersedes_qualification_id,
            "attestation_id": self.attestation_id,
            "attestation_digest": self.attestation_digest,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "trust_root_id": self.trust_root_id,
            "producer_id": self.producer_id,
            "verifier_id": self.verifier_id,
            "signed_at": self.signed_at,
            "release_artifact_id": self.release_artifact_id,
        }


def _identity_from_material(
    *,
    campaign: ProviderQualificationCampaign,
    campaign_artifact_ref: EvidenceArtifactRef,
    attestation_id: str,
    attestation_digest: str,
    policy_id: str,
    policy_version: str,
    trust_root_id: str,
    producer_id: str,
    verifier_id: str,
    signed_at: str,
    release_artifact_id: str | None,
) -> ProviderQualificationIdentity:
    all_refs = tuple(
        sorted(
            (campaign_artifact_ref, *campaign.raw_evidence_refs),
            key=lambda item: item.artifact_id,
        )
    )
    acceptance_metadata = {
        "attestation_id": attestation_id,
        "attestation_digest": attestation_digest,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "trust_root_id": trust_root_id,
        "producer_id": producer_id,
        "verifier_id": verifier_id,
        "signed_at": signed_at,
        "release_artifact_id": release_artifact_id,
    }
    return ProviderQualificationIdentity(
        provider_scope=campaign.scope.provider_scope,
        product_family=campaign.scope.product_family,
        adapter_source_git_sha=campaign.scope.adapter_source_git_sha,
        packaged_artifact_id=release_artifact_id,
        packaged_artifact_digest=campaign.scope.packaged_artifact_digest,
        campaign_id=campaign.scope.campaign_id,
        campaign_version=campaign.scope.campaign_version,
        protocol_id=campaign.scope.protocol_id,
        protocol_version=campaign.scope.protocol_version,
        required_case_policy_digest=_canonical_digest(
            list(campaign.required_cases)
        ),
        result_set_digest=_canonical_digest(
            {
                "passed_cases": list(campaign.passed_cases),
                "failed_cases": list(campaign.failed_cases),
                "unsupported_features": list(campaign.unsupported_features),
            }
        ),
        route_semantics_digest=_canonical_digest(
            json.loads(campaign.route_semantics_json)
        ),
        documentation_revision_digest=_canonical_digest(
            list(campaign.documentation_revisions)
        ),
        evidence_set_digest=_canonical_digest(
            [item.canonical() for item in all_refs]
        ),
        chronology_digest=_canonical_digest(
            {
                "completed_at": campaign.completed_at,
                "valid_until": campaign.valid_until,
            }
        ),
        lineage_digest=_canonical_digest(
            {
                "supersedes_qualification_id": campaign.supersedes_qualification_id
            }
        ),
        acceptance_metadata_digest=_canonical_digest(acceptance_metadata),
        attestation_digest=attestation_digest,
        trust_policy_digest=policy_id,
        issuer_identity_digest=_canonical_digest(
            {"producer_id": producer_id, "trust_root_id": trust_root_id}
        ),
        verifier_identity_digest=_canonical_digest(
            {"verifier_id": verifier_id}
        ),
    )


def _derive_accepted_provider_qualification(
    *,
    protocol: ProviderQualificationProtocol,
    campaign: ProviderQualificationCampaign,
    campaign_artifact_ref: EvidenceArtifactRef,
    accepted_attestation: AcceptedQualificationAttestation,
    receipt: SignedQualificationAttestation,
) -> AcceptedProviderQualification:
    if type(protocol) is not ProviderQualificationProtocol:
        raise ProviderQualificationError(
            "provider qualification protocol must be source-owned"
        )
    if type(campaign) is not ProviderQualificationCampaign:
        raise ProviderQualificationError(
            "campaign must be canonical ProviderQualificationCampaign"
        )
    if type(campaign_artifact_ref) is not EvidenceArtifactRef:
        raise ProviderQualificationError(
            "campaign artifact ref must be exact EvidenceArtifactRef"
        )
    if type(accepted_attestation) is not AcceptedQualificationAttestation:
        raise ProviderQualificationError(
            "accepted attestation must be canonical verifier output"
        )
    if type(receipt) is not SignedQualificationAttestation or type(
        receipt.attestation
    ) is not QualificationAttestation:
        raise ProviderQualificationError(
            "receipt must be exact canonical qualification receipt"
        )
    attestation = receipt.attestation
    if accepted_attestation.result != "PASS" or attestation.result != "PASS":
        raise ProviderQualificationError(
            "provider qualification requires canonical PASS attestation"
        )
    if (
        accepted_attestation.domain != protocol.domain
        or accepted_attestation.gate != protocol.gate
        or accepted_attestation.package_id != protocol.package_id
        or accepted_attestation.protocol_id != protocol.protocol_id
        or accepted_attestation.protocol_version != protocol.protocol_version
        or accepted_attestation.requirement_id != protocol.requirement_id
    ):
        raise ProviderQualificationError(
            "accepted attestation does not match provider qualification protocol"
        )
    if (
        campaign.scope.protocol_id != protocol.protocol_id
        or campaign.scope.protocol_version != protocol.protocol_version
    ):
        raise ProviderQualificationError(
            "campaign protocol does not match accepted attestation protocol"
        )
    if campaign.scope.adapter_source_git_sha != accepted_attestation.source_sha:
        raise ProviderQualificationError(
            "campaign adapter source does not match accepted source SHA"
        )
    if campaign.completed_at != attestation.completed_at:
        raise ProviderQualificationError(
            "campaign completion does not match attested completion"
        )
    if accepted_attestation.attestation_id != attestation.attestation_id:
        raise ProviderQualificationError(
            "canonical verifier returned different attestation identity"
        )
    if accepted_attestation.attestation_digest != attestation.content_digest:
        raise ProviderQualificationError(
            "canonical verifier returned different attestation digest"
        )
    if accepted_attestation.release_artifact_sha256 is not None and (
        campaign.scope.packaged_artifact_digest
        != accepted_attestation.release_artifact_sha256
    ):
        raise ProviderQualificationError(
            "campaign packaged artifact does not match accepted release artifact"
        )

    campaign_refs = [
        item
        for item in attestation.evidence_refs
        if item.evidence_kind == protocol.campaign_evidence_kind
    ]
    if len(campaign_refs) != 1 or campaign_refs[0] != campaign_artifact_ref:
        raise ProviderQualificationError(
            "attestation must bind exactly one canonical campaign artifact"
        )
    remaining = tuple(
        sorted(
            (
                item
                for item in attestation.evidence_refs
                if item != campaign_artifact_ref
            ),
            key=lambda item: item.artifact_id,
        )
    )
    if remaining != campaign.raw_evidence_refs:
        raise ProviderQualificationError(
            "campaign raw evidence refs do not exactly match accepted attestation evidence"
        )

    identity = _identity_from_material(
        campaign=campaign,
        campaign_artifact_ref=campaign_artifact_ref,
        attestation_id=accepted_attestation.attestation_id,
        attestation_digest=accepted_attestation.attestation_digest,
        policy_id=accepted_attestation.policy_id,
        policy_version=accepted_attestation.policy_version,
        trust_root_id=accepted_attestation.trust_root_id,
        producer_id=attestation.producer_id,
        verifier_id=attestation.verifier_id,
        signed_at=attestation.signed_at,
        release_artifact_id=accepted_attestation.release_artifact_id,
    )
    return AcceptedProviderQualification(
        identity=identity,
        scope=campaign.scope,
        required_cases=campaign.required_cases,
        unsupported_features=campaign.unsupported_features,
        route_semantics_json=campaign.route_semantics_json,
        documentation_revisions=campaign.documentation_revisions,
        completed_at=campaign.completed_at,
        valid_until=campaign.valid_until,
        campaign_artifact_ref=campaign_artifact_ref,
        raw_evidence_refs=campaign.raw_evidence_refs,
        supersedes_qualification_id=campaign.supersedes_qualification_id,
        attestation_id=accepted_attestation.attestation_id,
        attestation_digest=accepted_attestation.attestation_digest,
        policy_id=accepted_attestation.policy_id,
        policy_version=accepted_attestation.policy_version,
        trust_root_id=accepted_attestation.trust_root_id,
        producer_id=attestation.producer_id,
        verifier_id=attestation.verifier_id,
        signed_at=attestation.signed_at,
        release_artifact_id=accepted_attestation.release_artifact_id,
        _issuance_token=_ACCEPTED_TOKEN,
    )


def _detach_receipt(
    receipt: SignedQualificationAttestation,
) -> SignedQualificationAttestation:
    if type(receipt) is not SignedQualificationAttestation or type(
        receipt.attestation
    ) is not QualificationAttestation:
        raise TypeError(
            "receipt must be exact SignedQualificationAttestation"
        )
    if type(receipt.signature_b64) is not str:
        raise TypeError("qualification receipt signature must be exact text")
    detached = parse_signed_qualification_attestation(
        {
            "attestation": receipt.attestation.canonical_payload(),
            "signature_b64": receipt.signature_b64,
        }
    )
    if type(detached) is not SignedQualificationAttestation or type(
        detached.attestation
    ) is not QualificationAttestation:
        raise ProviderQualificationError(
            "qualification receipt parser returned non-canonical type"
        )
    return detached


def verify_provider_qualification_campaign(
    *,
    protocol_key: str,
    receipt: SignedQualificationAttestation,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
    expected_scope: ProviderQualificationScope,
    expected_release_artifact_id: str | None = None,
) -> AcceptedProviderQualification:
    """Issue Q only from source-owned protocol + canonical signed campaign proof."""

    protocol = source_provider_qualification_protocol(protocol_key)
    if type(expected_scope) is not ProviderQualificationScope:
        raise TypeError(
            "expected_scope must be exact ProviderQualificationScope"
        )
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be the canonical ArtifactStore")
    if expected_release_artifact_id is not None:
        expected_release_artifact_id = _uuid(
            expected_release_artifact_id,
            name="expected_release_artifact_id",
        )
    detached = _detach_receipt(receipt)
    campaign_refs = [
        item
        for item in detached.attestation.evidence_refs
        if item.evidence_kind == protocol.campaign_evidence_kind
    ]
    if len(campaign_refs) != 1:
        raise ProviderQualificationError(
            "attestation must contain exactly one provider campaign artifact"
        )
    campaign_ref = campaign_refs[0]
    if campaign_ref.media_type != "application/json":
        raise ProviderQualificationError(
            "provider qualification campaign must use application/json"
        )
    if campaign_ref.source_sha != expected_scope.adapter_source_git_sha:
        raise ProviderQualificationError(
            "campaign evidence source SHA does not match expected scope"
        )

    try:
        accepted = verify_canonical_qualification_attestation(
            detached,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=expected_scope.adapter_source_git_sha,
            expected_domain=protocol.domain,
            expected_gate=protocol.gate,
            expected_package_id=protocol.package_id,
            expected_protocol_id=protocol.protocol_id,
            expected_protocol_version=protocol.protocol_version,
            expected_requirement_id=protocol.requirement_id,
            expected_release_artifact_id=expected_release_artifact_id,
            expected_release_artifact_sha256=(
                expected_scope.packaged_artifact_digest
                if expected_release_artifact_id is not None
                else None
            ),
        )
        reader = trusted_authenticated_reader(
            evidence_root,
            publication_store=evidence_store,
        )
        manifest, raw = reader(campaign_ref.artifact_id)
    except (
        QualificationTrustError,
        ArtifactIntegrityError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        raise ProviderQualificationError(
            "provider qualification evidence could not cross canonical trust boundary"
        ) from error
    if (
        manifest.get("sha256") != campaign_ref.sha256
        or manifest.get("media_type") != campaign_ref.media_type
        or "sha256:" + sha256(raw).hexdigest() != campaign_ref.sha256
    ):
        raise ProviderQualificationError(
            "provider qualification campaign artifact changed or mismatched"
        )
    metadata = manifest.get("metadata")
    source_refs = manifest.get("source_refs")
    if (
        type(metadata) is not dict
        or metadata.get("evidence_kind") != campaign_ref.evidence_kind
        or type(source_refs) is not list
        or f"git:{campaign_ref.source_sha}" not in source_refs
    ):
        raise ProviderQualificationError(
            "provider qualification campaign manifest provenance mismatched"
        )

    campaign = parse_provider_qualification_campaign(raw)
    if campaign.scope != expected_scope:
        raise ProviderQualificationError(
            "provider qualification campaign scope does not match requested scope"
        )
    return _derive_accepted_provider_qualification(
        protocol=protocol,
        campaign=campaign,
        campaign_artifact_ref=campaign_ref,
        accepted_attestation=accepted,
        receipt=detached,
    )


def _scope_from_payload(value: object) -> ProviderQualificationScope:
    payload = _strict_object(
        value,
        name="accepted provider qualification scope",
        keys=frozenset(
            {
                "schema_version",
                "provider_scope",
                "product_family",
                "adapter_source_git_sha",
                "packaged_artifact_digest",
                "campaign_id",
                "campaign_version",
                "protocol_id",
                "protocol_version",
            }
        ),
    )
    provider_payload = _strict_object(
        payload["provider_scope"],
        name="accepted provider financial scope",
        keys=frozenset(
            {
                "schema_version",
                "provider_id",
                "runtime_environment",
                "provider_environment",
                "entity_policy_id",
            }
        ),
    )
    if (
        payload["schema_version"] != _SCHEMA_VERSION
        or provider_payload["schema_version"] != _SCHEMA_VERSION
    ):
        raise ProviderQualificationError(
            "accepted provider qualification scope schema mismatch"
        )
    scope = ProviderQualificationScope(
        provider_scope=ProviderFinancialScope(
            provider_id=provider_payload["provider_id"],
            runtime_environment=provider_payload["runtime_environment"],
            provider_environment=provider_payload["provider_environment"],
            entity_policy_id=provider_payload["entity_policy_id"],
        ),
        product_family=payload["product_family"],
        adapter_source_git_sha=payload["adapter_source_git_sha"],
        packaged_artifact_digest=payload["packaged_artifact_digest"],
        campaign_id=payload["campaign_id"],
        campaign_version=payload["campaign_version"],
        protocol_id=payload["protocol_id"],
        protocol_version=payload["protocol_version"],
    )
    if scope.payload() != payload:
        raise ProviderQualificationError(
            "accepted provider qualification scope is not canonical"
        )
    return scope


def _rehydrate_accepted_provider_qualification(
    payload: object,
) -> AcceptedProviderQualification:
    """Replay a durable Q and recompute its full content identity."""

    record = _strict_object(
        payload,
        name="accepted provider qualification",
        keys=frozenset(
            {
                "schema_version",
                "qualification_id",
                "identity",
                "scope",
                "required_cases",
                "unsupported_features",
                "route_semantics",
                "documentation_revisions",
                "completed_at",
                "valid_until",
                "campaign_artifact_ref",
                "raw_evidence_refs",
                "supersedes_qualification_id",
                "attestation_id",
                "attestation_digest",
                "policy_id",
                "policy_version",
                "trust_root_id",
                "producer_id",
                "verifier_id",
                "signed_at",
                "release_artifact_id",
            }
        ),
    )
    if record["schema_version"] != _SCHEMA_VERSION:
        raise ProviderQualificationError(
            "unsupported accepted provider qualification schema"
        )
    scope = _scope_from_payload(record["scope"])
    required = _unique_text_list(record["required_cases"], name="required_cases")
    if required != tuple(sorted(REQUIRED_QUALIFICATION_CASES)):
        raise ProviderQualificationError(
            "durable required_cases no longer match source-owned case universe"
        )
    unsupported = _unique_text_list(
        record["unsupported_features"], name="unsupported_features"
    )
    semantics = record["route_semantics"]
    if type(semantics) is not dict or not semantics:
        raise ProviderQualificationError(
            "durable route_semantics are invalid"
        )
    semantics_json = canonical_json(semantics)
    docs = _unique_text_list(
        record["documentation_revisions"], name="documentation_revisions"
    )
    completed_at = _instant(record["completed_at"], name="completed_at")
    valid_until = _instant(record["valid_until"], name="valid_until")
    if _instant_value(valid_until) <= _instant_value(completed_at):
        raise ProviderQualificationError(
            "durable provider qualification validity interval is invalid"
        )
    campaign_ref = _parse_evidence_ref(
        record["campaign_artifact_ref"], name="campaign_artifact_ref"
    )
    raw_ref_values = record["raw_evidence_refs"]
    if type(raw_ref_values) is not list:
        raise ProviderQualificationError(
            "raw_evidence_refs must be an exact JSON array"
        )
    raw_refs = tuple(
        _parse_evidence_ref(item, name=f"raw_evidence_refs[{index}]")
        for index, item in enumerate(raw_ref_values)
    )
    if raw_refs != tuple(sorted(raw_refs, key=lambda item: item.artifact_id)):
        raise ProviderQualificationError(
            "raw_evidence_refs are not canonical"
        )
    supersedes = record["supersedes_qualification_id"]
    if supersedes is not None:
        supersedes = _qid(
            supersedes,
            name="supersedes_qualification_id",
        )
    attestation_id = _uuid(record["attestation_id"], name="attestation_id")
    attestation_digest = _digest(
        record["attestation_digest"], name="attestation_digest"
    )
    policy_id = _digest(record["policy_id"], name="policy_id")
    policy_version = _token(record["policy_version"], name="policy_version")
    trust_root_id = _digest(record["trust_root_id"], name="trust_root_id")
    producer_id = _token(record["producer_id"], name="producer_id")
    verifier_id = _token(record["verifier_id"], name="verifier_id")
    signed_at = _instant(record["signed_at"], name="signed_at")
    release_artifact_id = _optional_uuid(
        record["release_artifact_id"], name="release_artifact_id"
    )

    campaign = ProviderQualificationCampaign(
        scope=scope,
        required_cases=required,
        passed_cases=required,
        failed_cases=(),
        unsupported_features=unsupported,
        route_semantics_json=semantics_json,
        documentation_revisions=docs,
        completed_at=completed_at,
        valid_until=valid_until,
        raw_evidence_refs=raw_refs,
        supersedes_qualification_id=supersedes,
    )
    expected_identity = _identity_from_material(
        campaign=campaign,
        campaign_artifact_ref=campaign_ref,
        attestation_id=attestation_id,
        attestation_digest=attestation_digest,
        policy_id=policy_id,
        policy_version=policy_version,
        trust_root_id=trust_root_id,
        producer_id=producer_id,
        verifier_id=verifier_id,
        signed_at=signed_at,
        release_artifact_id=release_artifact_id,
    )
    identity_payload = record["identity"]
    if type(identity_payload) is not dict or expected_identity.payload() != identity_payload:
        raise ProviderQualificationError(
            "accepted provider qualification identity content mismatch"
        )
    if expected_identity.content_digest != _qid(
        record["qualification_id"], name="qualification_id"
    ):
        raise ProviderQualificationError(
            "accepted provider qualification id mismatch"
        )
    accepted = AcceptedProviderQualification(
        identity=expected_identity,
        scope=scope,
        required_cases=required,
        unsupported_features=unsupported,
        route_semantics_json=semantics_json,
        documentation_revisions=docs,
        completed_at=completed_at,
        valid_until=valid_until,
        campaign_artifact_ref=campaign_ref,
        raw_evidence_refs=raw_refs,
        supersedes_qualification_id=supersedes,
        attestation_id=attestation_id,
        attestation_digest=attestation_digest,
        policy_id=policy_id,
        policy_version=policy_version,
        trust_root_id=trust_root_id,
        producer_id=producer_id,
        verifier_id=verifier_id,
        signed_at=signed_at,
        release_artifact_id=release_artifact_id,
        _issuance_token=_ACCEPTED_TOKEN,
    )
    if accepted.payload() != record:
        raise ProviderQualificationError(
            "accepted provider qualification durable payload is not canonical"
        )
    return accepted
