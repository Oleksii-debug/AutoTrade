"""Signed target-host runtime qualification profile for WP-65.

This module creates no measurements, signer authority, release authority, or
trading authority. It specializes the repository's existing signed qualification
boundary for terminal target-host runtime evidence. Canonical verification fails
closed unless a separately controlled trust policy authorizes the exact
PERFORMANCE/RUNTIME_TARGET_HOST scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import UUID

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    SignedQualificationAttestation,
    verify_canonical_qualification_attestation,
)


class RuntimeTargetHostQualificationError(ValueError):
    """Raised when target-host runtime evidence is incomplete or inconsistent."""


DOMAIN = "PERFORMANCE"
GATE = "RUNTIME_TARGET_HOST"
PACKAGE_ID = "WP-65"
PROTOCOL_ID = "runtime-target-host-v1"
PROTOCOL_VERSION = "1.0.0"
REQUIREMENT_ID = "target-host-pressure-budget"
BINDING_SCHEMA_VERSION = "1.0.0"
PROVENANCE_SCHEMA_VERSION = "1.0.0"
BINDING_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_BINDING"
CAMPAIGN_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_CAMPAIGN"
STALENESS_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_STALENESS"
INTERFERENCE_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_INTERFERENCE"
RESOURCE_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_RESOURCES"
HOST_INVENTORY_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_INVENTORY"
JSON_MEDIA_TYPE = "application/json"

_PROVENANCE_KINDS = frozenset(
    {
        CAMPAIGN_EVIDENCE_KIND,
        STALENESS_EVIDENCE_KIND,
        INTERFERENCE_EVIDENCE_KIND,
        RESOURCE_EVIDENCE_KIND,
        HOST_INVENTORY_EVIDENCE_KIND,
    }
)
_REQUIRED_KINDS = _PROVENANCE_KINDS | {BINDING_EVIDENCE_KIND}
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


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        canonical = str(UUID(text))
    except (ValueError, AttributeError, TypeError) as error:
        raise RuntimeTargetHostQualificationError(
            f"{name} must be a canonical UUID"
        ) from error
    if canonical != text:
        raise RuntimeTargetHostQualificationError(
            f"{name} must be a canonical UUID"
        )
    return text


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeTargetHostQualificationError(
                "target-host evidence contains duplicate JSON object key"
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


def _strict_json(raw: bytes, *, name: str) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostQualificationError(f"{name} must be non-empty bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeTargetHostQualificationError(
                    f"{name} contains invalid JSON constant {token}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeTargetHostQualificationError(
            f"{name} is not valid UTF-8 JSON"
        ) from error
    if type(value) is not dict:
        raise RuntimeTargetHostQualificationError(f"{name} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostProvenance:
    """Canonical envelope retained by each target-host evidence family."""

    evidence_kind: str
    source_sha: str
    scenario_id: str
    spec_digest: str
    configuration_hash: str
    host_fingerprint: str
    workload_profile_hash: str
    journal_store_identity_digest: str
    release_artifact_id: str
    release_artifact_sha256: str
    collector_id: str
    collector_version: str
    payload_artifact_id: str
    payload_sha256: str
    schema_version: str = PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROVENANCE_SCHEMA_VERSION:
            raise RuntimeTargetHostQualificationError(
                "unsupported target-host provenance schema_version"
            )
        kind = _text(self.evidence_kind, name="evidence_kind")
        if kind not in _PROVENANCE_KINDS:
            raise RuntimeTargetHostQualificationError(
                "unsupported target-host provenance evidence_kind"
            )
        object.__setattr__(self, "evidence_kind", kind)
        object.__setattr__(
            self, "source_sha", _git_sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self,
            "release_artifact_id",
            _uuid(self.release_artifact_id, name="release_artifact_id"),
        )
        object.__setattr__(
            self,
            "payload_artifact_id",
            _uuid(self.payload_artifact_id, name="payload_artifact_id"),
        )
        for field in ("scenario_id", "collector_id", "collector_version"):
            object.__setattr__(
                self, field, _text(getattr(self, field), name=field)
            )
        for field in (
            "spec_digest",
            "configuration_hash",
            "host_fingerprint",
            "workload_profile_hash",
            "journal_store_identity_digest",
            "release_artifact_sha256",
            "payload_sha256",
        ):
            object.__setattr__(
                self, field, _digest(getattr(self, field), name=field)
            )

    def canonical_payload(self) -> dict[str, str]:
        return {
            "collector_id": self.collector_id,
            "collector_version": self.collector_version,
            "configuration_hash": self.configuration_hash,
            "evidence_kind": self.evidence_kind,
            "host_fingerprint": self.host_fingerprint,
            "journal_store_identity_digest": self.journal_store_identity_digest,
            "payload_artifact_id": self.payload_artifact_id,
            "payload_sha256": self.payload_sha256,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "scenario_id": self.scenario_id,
            "schema_version": self.schema_version,
            "source_sha": self.source_sha,
            "spec_digest": self.spec_digest,
            "workload_profile_hash": self.workload_profile_hash,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @classmethod
    def parse(cls, raw: bytes) -> "RuntimeTargetHostProvenance":
        value = _strict_json(raw, name="target-host provenance")
        expected = {
            "collector_id",
            "collector_version",
            "configuration_hash",
            "evidence_kind",
            "host_fingerprint",
            "journal_store_identity_digest",
            "payload_artifact_id",
            "payload_sha256",
            "release_artifact_id",
            "release_artifact_sha256",
            "scenario_id",
            "schema_version",
            "source_sha",
            "spec_digest",
            "workload_profile_hash",
        }
        if set(value) != expected:
            raise RuntimeTargetHostQualificationError(
                "target-host provenance fields are non-canonical"
            )
        envelope = cls(**value)
        if envelope.canonical_bytes() != raw:
            raise RuntimeTargetHostQualificationError(
                "target-host provenance bytes are not canonical JSON"
            )
        return envelope


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostBinding:
    """Canonical digest bridge across all terminal WP-65 evidence families."""

    source_sha: str
    scenario_id: str
    spec_digest: str
    configuration_hash: str
    host_fingerprint: str
    workload_profile_hash: str
    journal_store_identity_digest: str
    release_artifact_id: str
    release_artifact_sha256: str
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
        object.__setattr__(
            self,
            "release_artifact_id",
            _uuid(self.release_artifact_id, name="release_artifact_id"),
        )
        for field in (
            "spec_digest",
            "configuration_hash",
            "host_fingerprint",
            "workload_profile_hash",
            "journal_store_identity_digest",
            "release_artifact_sha256",
            "campaign_evidence_sha256",
            "staleness_evidence_sha256",
            "interference_evidence_sha256",
            "resource_evidence_sha256",
            "host_inventory_evidence_sha256",
        ):
            object.__setattr__(
                self, field, _digest(getattr(self, field), name=field)
            )

    def canonical_payload(self) -> dict[str, str]:
        return {
            "campaign_evidence_sha256": self.campaign_evidence_sha256,
            "configuration_hash": self.configuration_hash,
            "host_fingerprint": self.host_fingerprint,
            "host_inventory_evidence_sha256": self.host_inventory_evidence_sha256,
            "interference_evidence_sha256": self.interference_evidence_sha256,
            "journal_store_identity_digest": self.journal_store_identity_digest,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
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
        value = _strict_json(raw, name="target-host binding")
        expected = {
            "campaign_evidence_sha256",
            "configuration_hash",
            "host_fingerprint",
            "host_inventory_evidence_sha256",
            "interference_evidence_sha256",
            "journal_store_identity_digest",
            "release_artifact_id",
            "release_artifact_sha256",
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
    release_artifact_id: str
    release_artifact_sha256: str
    binding_artifact_id: str
    binding_sha256: str
    evidence_sha256_by_kind: Mapping[str, str]
    payload_artifact_id_by_kind: Mapping[str, str]
    payload_sha256_by_kind: Mapping[str, str]
    collector_by_kind: Mapping[str, str]

    def __post_init__(self) -> None:
        for field in (
            "evidence_sha256_by_kind",
            "payload_artifact_id_by_kind",
            "payload_sha256_by_kind",
            "collector_by_kind",
        ):
            object.__setattr__(
                self,
                field,
                MappingProxyType(dict(getattr(self, field))),
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
            "target-host artifact cannot be read with integrity"
        ) from error
    if type(raw) is not bytes:
        raise RuntimeTargetHostQualificationError(
            "target-host artifact reader returned non-bytes"
        )
    if "sha256:" + sha256(raw).hexdigest() != ref.sha256:
        raise RuntimeTargetHostQualificationError(
            "target-host artifact bytes do not match accepted evidence digest"
        )
    return raw


def _read_bound_payload(
    reader: Callable[[str], tuple[dict[str, object], bytes]],
    provenance: RuntimeTargetHostProvenance,
    *,
    forbidden_artifact_ids: set[str],
    forbidden_sha256: set[str],
) -> bytes:
    if provenance.payload_artifact_id in forbidden_artifact_ids:
        raise RuntimeTargetHostQualificationError(
            f"retained raw payload artifact is not independent for {provenance.evidence_kind}"
        )
    if provenance.payload_sha256 in forbidden_sha256:
        raise RuntimeTargetHostQualificationError(
            f"retained raw payload bytes are not independent for {provenance.evidence_kind}"
        )
    try:
        _manifest, raw = reader(provenance.payload_artifact_id)
    except (ArtifactIntegrityError, FileNotFoundError, OSError) as error:
        raise RuntimeTargetHostQualificationError(
            f"retained raw payload is unavailable for {provenance.evidence_kind}"
        ) from error
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostQualificationError(
            f"retained raw payload is empty or non-bytes for {provenance.evidence_kind}"
        )
    if "sha256:" + sha256(raw).hexdigest() != provenance.payload_sha256:
        raise RuntimeTargetHostQualificationError(
            f"retained raw payload digest mismatch for {provenance.evidence_kind}"
        )
    return raw


def _identity_tuple(
    *,
    source_sha: str,
    scenario_id: str,
    spec_digest: str,
    configuration_hash: str,
    host_fingerprint: str,
    workload_profile_hash: str,
    journal_store_identity_digest: str,
    release_artifact_id: str,
    release_artifact_sha256: str,
) -> tuple[str, ...]:
    return (
        source_sha,
        scenario_id,
        spec_digest,
        configuration_hash,
        host_fingerprint,
        workload_profile_hash,
        journal_store_identity_digest,
        release_artifact_id,
        release_artifact_sha256,
    )


def _provenance_identity(value: RuntimeTargetHostProvenance) -> tuple[str, ...]:
    return _identity_tuple(
        source_sha=value.source_sha,
        scenario_id=value.scenario_id,
        spec_digest=value.spec_digest,
        configuration_hash=value.configuration_hash,
        host_fingerprint=value.host_fingerprint,
        workload_profile_hash=value.workload_profile_hash,
        journal_store_identity_digest=value.journal_store_identity_digest,
        release_artifact_id=value.release_artifact_id,
        release_artifact_sha256=value.release_artifact_sha256,
    )


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
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> AcceptedRuntimeTargetHostQualification:
    """Accept one terminal WP-65 target-host receipt through canonical trust only."""

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
    release_artifact_id = _uuid(
        expected_release_artifact_id,
        name="expected_release_artifact_id",
    )
    release_artifact_sha256 = _digest(
        expected_release_artifact_sha256,
        name="expected_release_artifact_sha256",
    )
    expected_identity = _identity_tuple(
        source_sha=source_sha,
        scenario_id=scenario_id,
        spec_digest=spec_digest,
        configuration_hash=configuration_hash,
        host_fingerprint=host_fingerprint,
        workload_profile_hash=workload_profile_hash,
        journal_store_identity_digest=journal_identity,
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
    )

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
        expected_release_artifact_id=release_artifact_id,
        expected_release_artifact_sha256=release_artifact_sha256,
    )
    if type(accepted) is not AcceptedQualificationAttestation:
        raise RuntimeTargetHostQualificationError(
            "canonical verifier returned non-canonical accepted attestation"
        )
    if accepted.release_artifact_id != release_artifact_id or (
        accepted.release_artifact_sha256 != release_artifact_sha256
    ):
        raise RuntimeTargetHostQualificationError(
            "canonical verifier accepted a different release artifact"
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
    if any(ref.artifact_id == release_artifact_id for ref in refs.values()):
        raise RuntimeTargetHostQualificationError(
            "target-host evidence artifact cannot alias delivered release artifact"
        )
    if any(ref.sha256 == release_artifact_sha256 for ref in refs.values()):
        raise RuntimeTargetHostQualificationError(
            "target-host evidence bytes cannot alias delivered release bytes"
        )
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
    binding_identity = _identity_tuple(
        source_sha=binding.source_sha,
        scenario_id=binding.scenario_id,
        spec_digest=binding.spec_digest,
        configuration_hash=binding.configuration_hash,
        host_fingerprint=binding.host_fingerprint,
        workload_profile_hash=binding.workload_profile_hash,
        journal_store_identity_digest=binding.journal_store_identity_digest,
        release_artifact_id=binding.release_artifact_id,
        release_artifact_sha256=binding.release_artifact_sha256,
    )
    if binding_identity != expected_identity:
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
    top_level_artifact_ids = {ref.artifact_id for ref in refs.values()}
    top_level_sha256 = {ref.sha256 for ref in refs.values()}
    payload_artifact_ids: set[str] = set()
    payload_sha256: set[str] = set()
    payload_id_by_kind: dict[str, str] = {}
    payload_digest_by_kind: dict[str, str] = {}
    collectors: dict[str, str] = {}
    for kind in sorted(_PROVENANCE_KINDS):
        ref = refs[kind]
        if ref.sha256 != digest_bindings[kind]:
            raise RuntimeTargetHostQualificationError(
                f"target-host binding does not match {kind} artifact"
            )
        provenance = RuntimeTargetHostProvenance.parse(
            _read_artifact_bytes(reader, ref)
        )
        if provenance.evidence_kind != kind:
            raise RuntimeTargetHostQualificationError(
                f"target-host provenance kind conflicts for {kind}"
            )
        if _provenance_identity(provenance) != expected_identity:
            raise RuntimeTargetHostQualificationError(
                f"target-host provenance identity conflicts for {kind}"
            )
        _read_bound_payload(
            reader,
            provenance,
            forbidden_artifact_ids=(
                top_level_artifact_ids | payload_artifact_ids | {release_artifact_id}
            ),
            forbidden_sha256=(
                top_level_sha256 | payload_sha256 | {release_artifact_sha256}
            ),
        )
        payload_artifact_ids.add(provenance.payload_artifact_id)
        payload_sha256.add(provenance.payload_sha256)
        payload_id_by_kind[kind] = provenance.payload_artifact_id
        payload_digest_by_kind[kind] = provenance.payload_sha256
        collectors[kind] = f"{provenance.collector_id}@{provenance.collector_version}"

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
        release_artifact_id=release_artifact_id,
        release_artifact_sha256=release_artifact_sha256,
        binding_artifact_id=binding_ref.artifact_id,
        binding_sha256=binding_ref.sha256,
        evidence_sha256_by_kind={
            kind: ref.sha256 for kind, ref in sorted(refs.items())
        },
        payload_artifact_id_by_kind=payload_id_by_kind,
        payload_sha256_by_kind=payload_digest_by_kind,
        collector_by_kind=collectors,
    )
