"""Signed target-host runtime qualification profile for WP-65.

This module does not create performance evidence, sign attestations, or grant
trading authority.  It specializes the repository's canonical qualification
trust boundary for the terminal target-host portion of WP-65.  A trusted result
is possible only when an independently signed qualification attestation and all
of its immutable artifacts agree on the exact source, budget, configuration,
host, workload, and measurement provenance.

The canonical qualification trust policy remains separately controlled.  If no
policy/root is authorized for PERFORMANCE/RUNTIME_TARGET_HOST, canonical
verification fails closed; this module never widens signer authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Callable, Mapping

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)

from .qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationTrustError,
    SignedQualificationAttestation,
    verify_canonical_qualification_attestation,
)


class RuntimeTargetHostQualificationError(ValueError):
    """Raised when terminal target-host runtime evidence is incomplete or stale."""


DOMAIN = "PERFORMANCE"
GATE = "RUNTIME_TARGET_HOST"
PACKAGE_ID = "WP-65"
PROTOCOL_ID = "runtime-target-host-v1"
PROTOCOL_VERSION = "1.0.0"
REQUIREMENT_ID = "target-host-pressure-budget"
BINDING_SCHEMA_VERSION = "1.0.0"
BINDING_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_BINDING"
CAMPAIGN_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_CAMPAIGN"
STALENESS_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_STALENESS"
INTERFERENCE_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_INTERFERENCE"
RESOURCE_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_RESOURCES"
HOST_INVENTORY_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_INVENTORY"
JSON_MEDIA_TYPE = "application/json"

_REQUIRED_KINDS = frozenset(
    {
        BINDING_EVIDENCE_KIND,
        CAMPAIGN_EVIDENCE_KIND,
        STALENESS_EVIDENCE_KIND,
        INTERFERENCE_EVIDENCE_KIND,
        RESOURCE_EVIDENCE_KIND,
        HOST_INVENTORY_EVIDENCE_KIND,
    }
)
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostQualificationError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _git_sha(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _GIT_SHA.fullmatch(text) is None:
        raise RuntimeTargetHostQualificationError(
            f"{name} must be a lowercase 40-character Git SHA"
        )
    return text


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise RuntimeTargetHostQualificationError(
            f"{name} must be canonical sha256:<64 hex>"
        )
    return text


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeTargetHostQualificationError(
                "target-host binding contains duplicate JSON object key"
            )
        result[key] = value
    return result


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostBinding:
    """Canonical identity bridge across all terminal WP-65 evidence families."""

    source_sha: str
    scenario_id: str
    spec_digest: str
    configuration_hash: str
    host_fingerprint: str
    workload_profile_hash: str
    journal_store_identity_digest: str
    campaign_evidence_sha256: str
    staleness_evidence_sha256: str
    interference_evidence_sha256: str
    resource_evidence_sha256: str
    host_inventory_evidence_sha256: str
    schema_version: str = BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != BINDING_SCHEMA_VERSION:
            raise RuntimeTargetHostQualificationError(
                "unsupported target-host binding schema_version"
            )
        object.__setattr__(
            self, "source_sha", _git_sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self, "scenario_id", _text(self.scenario_id, name="scenario_id")
        )
        for field in (
            "spec_digest",
            "configuration_hash",
            "host_fingerprint",
            "workload_profile_hash",
            "journal_store_identity_digest",
            "campaign_evidence_sha256",
            "staleness_evidence_sha256",
            "interference_evidence_sha256",
            "resource_evidence_sha256",
            "host_inventory_evidence_sha256",
        ):
            object.__setattr__(
                self,
                field,
                _digest(getattr(self, field), name=field),
            )

    def canonical_payload(self) -> dict[str, str]:
        return {
            "campaign_evidence_sha256": self.campaign_evidence_sha256,
            "configuration_hash": self.configuration_hash,
            "host_fingerprint": self.host_fingerprint,
            "host_inventory_evidence_sha256": self.host_inventory_evidence_sha256,
            "interference_evidence_sha256": self.interference_evidence_sha256,
            "journal_store_identity_digest": self.journal_store_identity_digest,
            "resource_evidence_sha256": self.resource_evidence_sha256,
            "scenario_id": self.scenario_id,
            "schema_version": self.schema_version,
            "source_sha": self.source_sha,
            "spec_digest": self.spec_digest,
            "staleness_evidence_sha256": self.staleness_evidence_sha256,
            "workload_profile_hash": self.workload_profile_hash,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def parse(cls, raw: bytes) -> "RuntimeTargetHostBinding":
        if type(raw) is not bytes or not raw:
            raise RuntimeTargetHostQualificationError(
                "target-host binding must be non-empty bytes"
            )
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    RuntimeTargetHostQualificationError(
                        f"target-host binding contains invalid JSON constant {value}"
                    )
                ),
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeTargetHostQualificationError(
                "target-host binding is not valid UTF-8 JSON"
            ) from error
        if type(value) is not dict:
            raise RuntimeTargetHostQualificationError(
                "target-host binding must be a JSON object"
            )
        expected = {
            "campaign_evidence_sha256",
            "configuration_hash",
            "host_fingerprint",
            "host_inventory_evidence_sha256",
            "interference_evidence_sha256",
            "journal_store_identity_digest",
            "resource_evidence_sha256",
            "scenario_id",
            "schema_version",
            "source_sha",
            "spec_digest",
            "staleness_evidence_sha256",
            "workload_profile_hash",
        }
        if set(value) != expected:
            raise RuntimeTargetHostQualificationError(
                "target-host binding fields are non-canonical"
            )
        binding = cls(**value)
        if binding.canonical_bytes() != raw:
            raise RuntimeTargetHostQualificationError(
                "target-host binding bytes are not canonical JSON"
            )
        return binding


@dataclass(frozen=True, slots=True)
class AcceptedRuntimeTargetHostQualification:
    attestation_id: str
    attestation_digest: str
    source_sha: str
    scenario_id: str
    spec_digest: str
    configuration_hash: str
    host_fingerprint: str
    workload_profile_hash: str
    journal_store_identity_digest: str
    binding_artifact_id: str
    binding_sha256: str
    evidence_sha256_by_kind: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_sha256_by_kind",
            MappingProxyType(dict(self.evidence_sha256_by_kind)),
        )


def _snapshot_ref(value: EvidenceArtifactRef) -> EvidenceArtifactRef:
    if type(value) is not EvidenceArtifactRef:
        raise TypeError("target-host evidence ref must be exact EvidenceArtifactRef")
    return EvidenceArtifactRef(
        artifact_id=value.artifact_id,
        sha256=value.sha256,
        media_type=value.media_type,
        evidence_kind=value.evidence_kind,
        source_sha=value.source_sha,
    )


def _required_refs(
    accepted: AcceptedQualificationAttestation,
    *,
    expected_source_sha: str,
) -> dict[str, EvidenceArtifactRef]:
    if type(accepted) is not AcceptedQualificationAttestation:
        raise TypeError(
            "accepted attestation must be exact AcceptedQualificationAttestation"
        )
    refs = tuple(_snapshot_ref(value) for value in accepted.evidence_refs)
    by_kind: dict[str, EvidenceArtifactRef] = {}
    for ref in refs:
        if ref.source_sha != expected_source_sha:
            raise RuntimeTargetHostQualificationError(
                "target-host evidence ref belongs to another source SHA"
            )
        if ref.evidence_kind in by_kind:
            raise RuntimeTargetHostQualificationError(
                f"duplicate target-host evidence kind: {ref.evidence_kind}"
            )
        by_kind[ref.evidence_kind] = ref
    if set(by_kind) != _REQUIRED_KINDS:
        missing = sorted(_REQUIRED_KINDS - set(by_kind))
        extra = sorted(set(by_kind) - _REQUIRED_KINDS)
        raise RuntimeTargetHostQualificationError(
            f"target-host evidence set mismatch: missing={missing} extra={extra}"
        )
    if any(ref.media_type != JSON_MEDIA_TYPE for ref in by_kind.values()):
        raise RuntimeTargetHostQualificationError(
            "target-host evidence artifacts must use application/json"
        )
    return by_kind


def _read_artifact_bytes(
    reader: Callable[[str], tuple[dict[str, object], bytes]],
    ref: EvidenceArtifactRef,
) -> bytes:
    try:
        _manifest, raw = reader(ref.artifact_id)
    except (ArtifactIntegrityError, FileNotFoundError, OSError) as error:
        raise RuntimeTargetHostQualificationError(
            "target-host binding artifact cannot be read with integrity"
        ) from error
    if type(raw) is not bytes:
        raise RuntimeTargetHostQualificationError(
            "target-host binding artifact reader returned non-bytes"
        )
    observed = "sha256:" + sha256(raw).hexdigest()
    if observed != ref.sha256:
        raise RuntimeTargetHostQualificationError(
            "target-host binding bytes do not match accepted evidence digest"
        )
    return raw


def verify_runtime_target_host_qualification(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
    expected_source_sha: str,
    expected_scenario_id: str,
    expected_spec_digest: str,
    expected_configuration_hash: str,
    expected_host_fingerprint: str,
    expected_workload_profile_hash: str,
    expected_journal_store_identity_digest: str,
) -> AcceptedRuntimeTargetHostQualification:
    """Accept one terminal WP-65 target-host receipt through canonical trust only.

    This function cannot authorize a new signer.  It first delegates signature,
    scope, exact-source and immutable-artifact verification to the canonical
    qualification verifier.  It then validates the WP-65 evidence profile and
    cross-binds every provenance family through one canonical binding artifact.
    """

    if type(receipt) is not SignedQualificationAttestation:
        raise TypeError("receipt must be exact SignedQualificationAttestation")
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    source_sha = _git_sha(expected_source_sha, name="expected_source_sha")
    scenario_id = _text(expected_scenario_id, name="expected_scenario_id")
    spec_digest = _digest(expected_spec_digest, name="expected_spec_digest")
    configuration_hash = _digest(
        expected_configuration_hash, name="expected_configuration_hash"
    )
    host_fingerprint = _digest(
        expected_host_fingerprint, name="expected_host_fingerprint"
    )
    workload_profile_hash = _digest(
        expected_workload_profile_hash, name="expected_workload_profile_hash"
    )
    journal_identity = _digest(
        expected_journal_store_identity_digest,
        name="expected_journal_store_identity_digest",
    )

    try:
        accepted = verify_canonical_qualification_attestation(
            receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
            expected_source_sha=source_sha,
            expected_domain=DOMAIN,
            expected_gate=GATE,
            expected_package_id=PACKAGE_ID,
            expected_protocol_id=PROTOCOL_ID,
            expected_protocol_version=PROTOCOL_VERSION,
            expected_requirement_id=REQUIREMENT_ID,
        )
    except QualificationTrustError:
        raise
    if type(accepted) is not AcceptedQualificationAttestation:
        raise RuntimeTargetHostQualificationError(
            "canonical verifier returned non-canonical accepted attestation"
        )
    if accepted.result != "PASS":
        raise RuntimeTargetHostQualificationError(
            "terminal target-host qualification requires signed PASS"
        )
    if accepted.unresolved_limits:
        raise RuntimeTargetHostQualificationError(
            "terminal target-host qualification cannot retain unresolved limits"
        )

    refs = _required_refs(accepted, expected_source_sha=source_sha)
    try:
        reader = trusted_authenticated_reader(
            evidence_root,
            publication_store=evidence_store,
        )
    except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
        raise RuntimeTargetHostQualificationError(
            "target-host evidence authority cannot be bound"
        ) from error
    binding_ref = refs[BINDING_EVIDENCE_KIND]
    binding = RuntimeTargetHostBinding.parse(
        _read_artifact_bytes(reader, binding_ref)
    )
    expected_identity = (
        source_sha,
        scenario_id,
        spec_digest,
        configuration_hash,
        host_fingerprint,
        workload_profile_hash,
        journal_identity,
    )
    observed_identity = (
        binding.source_sha,
        binding.scenario_id,
        binding.spec_digest,
        binding.configuration_hash,
        binding.host_fingerprint,
        binding.workload_profile_hash,
        binding.journal_store_identity_digest,
    )
    if observed_identity != expected_identity:
        raise RuntimeTargetHostQualificationError(
            "target-host binding identity does not match requested qualification"
        )

    digest_bindings = {
        CAMPAIGN_EVIDENCE_KIND: binding.campaign_evidence_sha256,
        STALENESS_EVIDENCE_KIND: binding.staleness_evidence_sha256,
        INTERFERENCE_EVIDENCE_KIND: binding.interference_evidence_sha256,
        RESOURCE_EVIDENCE_KIND: binding.resource_evidence_sha256,
        HOST_INVENTORY_EVIDENCE_KIND: binding.host_inventory_evidence_sha256,
    }
    for kind, expected_digest in digest_bindings.items():
        if refs[kind].sha256 != expected_digest:
            raise RuntimeTargetHostQualificationError(
                f"target-host binding does not match {kind} artifact"
            )

    return AcceptedRuntimeTargetHostQualification(
        attestation_id=accepted.attestation_id,
        attestation_digest=accepted.attestation_digest,
        source_sha=source_sha,
        scenario_id=scenario_id,
        spec_digest=spec_digest,
        configuration_hash=configuration_hash,
        host_fingerprint=host_fingerprint,
        workload_profile_hash=workload_profile_hash,
        journal_store_identity_digest=journal_identity,
        binding_artifact_id=binding_ref.artifact_id,
        binding_sha256=binding_ref.sha256,
        evidence_sha256_by_kind={
            kind: ref.sha256 for kind, ref in sorted(refs.items())
        },
    )
