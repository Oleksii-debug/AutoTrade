"""Exact-evidence freeze gate for a release-candidate manifest.

This module is not a packager, signer, SBOM generator, accessibility authority
or compatibility test runner. It consumes the exact outputs of those canonical
authorities and freezes a deterministic release-candidate manifest only when all
required evidence belongs to the same source SHA and is explicitly passing.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Sequence
from uuid import UUID

from research.autotrade_research.artifacts import trusted_authenticated_reader
from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)

from .qualification_attestation import (
    AcceptedQualificationAttestation,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)

from .supply_chain_qualification import (
    SupplyChainEvidence,
    SupplyChainQualification,
    qualify_supply_chain,
)


class ReleaseCandidateError(ValueError):
    """Raised when release-candidate evidence is malformed."""


_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_ROLES = frozenset(
    {
        "HOST",
        "WEB",
        "DESKTOP",
        "WINDOWS_PACKAGE",
        "SBOM",
        "DEPENDENCY_RIGHTS",
        "ACCESSIBILITY",
        "API_COMPATIBILITY",
        "CLEAN_INSTALL",
        "LICENSE_NOTICES",
        "RELEASE_QUALIFICATION",
    }
)
_SIGNED_BINARY_ROLES = frozenset({"HOST", "WEB", "DESKTOP", "WINDOWS_PACKAGE"})
_SIGNATURE_STATUSES = frozenset(
    {"VERIFIED", "NOT_APPLICABLE", "MISSING", "INVALID"}
)
_EVIDENCE_STATUSES = frozenset({"PASS", "FAIL", "INCONCLUSIVE"})
_RELEASE_ARTIFACT_MEDIA_TYPE = "application/vnd.autotrade.release-artifact"
_RELEASE_EVIDENCE_KIND = "AUTOTRADE_RELEASE_EVIDENCE_V1"
_QUALIFICATION_DOMAIN = "RELEASE"
_QUALIFICATION_GATE = "FREEZE"
_QUALIFICATION_PACKAGE = "WP-54"
_QUALIFICATION_PROTOCOL = "release-freeze-v1"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_QUALIFICATION_REQUIREMENT = "release-candidate-freeze"
_QUALIFICATION_SUBJECT_REQUIREMENT_PREFIX = "release-candidate-subject-sha256:"


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCandidateError(f"{name} is required")
    return value.strip()


def _git_sha(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or value != value.lower()
        or _GIT_SHA.fullmatch(value) is None
    ):
        raise ReleaseCandidateError(
            f"{name} must be a canonical lowercase 40- or 64-character Git object id"
        )
    return value


def _artifact_id(value: str, *, name: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ReleaseCandidateError(f"{name} must be a canonical UUID")
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ReleaseCandidateError(f"{name} must be a canonical UUID") from error
    if canonical != value:
        raise ReleaseCandidateError(f"{name} must be a canonical UUID")
    return value


def _sha256(value: str, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _SHA256.fullmatch(value) is None
    ):
        raise ReleaseCandidateError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


@dataclass(frozen=True)
class ReleaseArtifactEvidence:
    role: str
    artifact_id: str
    artifact_sha256: str
    source_sha: str
    signature_status: str
    evidence_status: str

    def __post_init__(self) -> None:
        role = _text(self.role, name="role").upper()
        signature = _text(
            self.signature_status,
            name="signature_status",
        ).upper()
        evidence = _text(
            self.evidence_status,
            name="evidence_status",
        ).upper()
        if signature not in _SIGNATURE_STATUSES:
            raise ReleaseCandidateError("unsupported signature_status")
        if evidence not in _EVIDENCE_STATUSES:
            raise ReleaseCandidateError("unsupported evidence_status")
        if role in _SIGNED_BINARY_ROLES and signature == "NOT_APPLICABLE":
            raise ReleaseCandidateError(
                f"{role} signature cannot be NOT_APPLICABLE"
            )
        object.__setattr__(self, "role", role)
        object.__setattr__(
            self,
            "artifact_id",
            _artifact_id(self.artifact_id, name="artifact_id"),
        )
        object.__setattr__(
            self,
            "artifact_sha256",
            _sha256(self.artifact_sha256, name="artifact_sha256"),
        )
        object.__setattr__(
            self,
            "source_sha",
            _git_sha(self.source_sha, name="source_sha"),
        )
        object.__setattr__(self, "signature_status", signature)
        object.__setattr__(self, "evidence_status", evidence)

    @classmethod
    def create(
        cls,
        *,
        role: str,
        artifact_id: str,
        artifact_sha256: str,
        source_sha: str,
        signature_status: str,
        evidence_status: str,
    ) -> "ReleaseArtifactEvidence":
        normalized_role = _text(role, name="role").upper()
        signature = _text(
            signature_status,
            name="signature_status",
        ).upper()
        evidence = _text(
            evidence_status,
            name="evidence_status",
        ).upper()
        if signature not in _SIGNATURE_STATUSES:
            raise ReleaseCandidateError("unsupported signature_status")
        if evidence not in _EVIDENCE_STATUSES:
            raise ReleaseCandidateError("unsupported evidence_status")
        if normalized_role in _SIGNED_BINARY_ROLES and signature == "NOT_APPLICABLE":
            raise ReleaseCandidateError(
                f"{normalized_role} signature cannot be NOT_APPLICABLE"
            )
        return cls(
            role=normalized_role,
            artifact_id=_artifact_id(artifact_id, name="artifact_id"),
            artifact_sha256=_sha256(
                artifact_sha256,
                name="artifact_sha256",
            ),
            source_sha=_git_sha(source_sha, name="source_sha"),
            signature_status=signature,
            evidence_status=evidence,
        )


@dataclass(frozen=True)
class ReleaseCandidateInput:
    release_id: str
    source_sha: str
    baseline_hash: str
    schema_contract_hash: str
    artifacts: tuple[ReleaseArtifactEvidence, ...]
    unresolved_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.artifacts, (str, bytes)) or not isinstance(
            self.artifacts, Sequence
        ):
            raise ReleaseCandidateError("artifacts must be a sequence")
        artifacts = tuple(self.artifacts)
        roles: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, ReleaseArtifactEvidence):
                raise ReleaseCandidateError(
                    "every artifact must be ReleaseArtifactEvidence"
                )
            if artifact.role in roles:
                raise ReleaseCandidateError(
                    f"duplicate release artifact role: {artifact.role}"
                )
            roles.add(artifact.role)
        unsupported_roles = sorted(roles - _REQUIRED_ROLES)
        if unsupported_roles:
            raise ReleaseCandidateError(
                "unsupported release artifact role: "
                + ", ".join(unsupported_roles)
            )

        if isinstance(self.unresolved_blockers, (str, bytes)) or not isinstance(
            self.unresolved_blockers,
            Sequence,
        ):
            raise ReleaseCandidateError(
                "unresolved_blockers must be a sequence"
            )
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in self.unresolved_blockers
        )
        if len(set(blockers)) != len(blockers):
            raise ReleaseCandidateError(
                "unresolved_blockers contains duplicates"
            )

        object.__setattr__(
            self,
            "release_id",
            _text(self.release_id, name="release_id"),
        )
        object.__setattr__(
            self,
            "source_sha",
            _git_sha(self.source_sha, name="source_sha"),
        )
        object.__setattr__(
            self,
            "baseline_hash",
            _sha256(self.baseline_hash, name="baseline_hash"),
        )
        object.__setattr__(
            self,
            "schema_contract_hash",
            _sha256(
                self.schema_contract_hash,
                name="schema_contract_hash",
            ),
        )
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "unresolved_blockers", blockers)

    @classmethod
    def create(
        cls,
        *,
        release_id: str,
        source_sha: str,
        baseline_hash: str,
        schema_contract_hash: str,
        artifacts: Sequence[ReleaseArtifactEvidence],
        unresolved_blockers: Sequence[str] = (),
    ) -> "ReleaseCandidateInput":
        if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
            raise ReleaseCandidateError("artifacts must be a sequence")
        normalized_artifacts = tuple(artifacts)
        roles: set[str] = set()
        for artifact in normalized_artifacts:
            if not isinstance(artifact, ReleaseArtifactEvidence):
                raise ReleaseCandidateError(
                    "every artifact must be ReleaseArtifactEvidence"
                )
            if artifact.role in roles:
                raise ReleaseCandidateError(
                    f"duplicate release artifact role: {artifact.role}"
                )
            roles.add(artifact.role)
        unsupported_roles = sorted(roles - _REQUIRED_ROLES)
        if unsupported_roles:
            raise ReleaseCandidateError(
                "unsupported release artifact role: "
                + ", ".join(unsupported_roles)
            )
        if isinstance(unresolved_blockers, (str, bytes)) or not isinstance(
            unresolved_blockers,
            Sequence,
        ):
            raise ReleaseCandidateError(
                "unresolved_blockers must be a sequence"
            )
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in unresolved_blockers
        )
        if len(set(blockers)) != len(blockers):
            raise ReleaseCandidateError("unresolved_blockers contains duplicates")
        return cls(
            release_id=_text(release_id, name="release_id"),
            source_sha=_git_sha(source_sha, name="source_sha"),
            baseline_hash=_sha256(baseline_hash, name="baseline_hash"),
            schema_contract_hash=_sha256(
                schema_contract_hash,
                name="schema_contract_hash",
            ),
            artifacts=normalized_artifacts,
            unresolved_blockers=blockers,
        )



def _exact_snapshot_text(value: object, *, name: str) -> str:
    """Require an exact built-in string at a terminal snapshot boundary."""

    if type(value) is not str:
        raise ReleaseCandidateError(f"{name} must be an exact built-in string")
    return value


def _snapshot_release_artifact(
    artifact: ReleaseArtifactEvidence,
) -> ReleaseArtifactEvidence:
    """Detach one caller artifact into an exact base, scalar-only snapshot."""

    if not isinstance(artifact, ReleaseArtifactEvidence):
        raise ReleaseCandidateError(
            "every artifact must be ReleaseArtifactEvidence"
        )
    role = artifact.role
    artifact_id = artifact.artifact_id
    artifact_sha256 = artifact.artifact_sha256
    source_sha = artifact.source_sha
    signature_status = artifact.signature_status
    evidence_status = artifact.evidence_status
    values = {
        "role": _exact_snapshot_text(role, name="artifact.role"),
        "artifact_id": _exact_snapshot_text(
            artifact_id,
            name="artifact.artifact_id",
        ),
        "artifact_sha256": _exact_snapshot_text(
            artifact_sha256,
            name="artifact.artifact_sha256",
        ),
        "source_sha": _exact_snapshot_text(
            source_sha,
            name="artifact.source_sha",
        ),
        "signature_status": _exact_snapshot_text(
            signature_status,
            name="artifact.signature_status",
        ),
        "evidence_status": _exact_snapshot_text(
            evidence_status,
            name="artifact.evidence_status",
        ),
    }
    return ReleaseArtifactEvidence.create(**values)


def _snapshot_release_candidate(
    candidate: ReleaseCandidateInput,
) -> ReleaseCandidateInput:
    """Detach the complete caller graph before any authority callback executes."""

    if not isinstance(candidate, ReleaseCandidateInput):
        raise TypeError("candidate must be ReleaseCandidateInput")

    release_id = candidate.release_id
    source_sha = candidate.source_sha
    baseline_hash = candidate.baseline_hash
    schema_contract_hash = candidate.schema_contract_hash
    artifacts = candidate.artifacts
    unresolved_blockers = candidate.unresolved_blockers

    if type(artifacts) is not tuple:
        raise ReleaseCandidateError(
            "candidate.artifacts must be an exact tuple at the terminal boundary"
        )
    if type(unresolved_blockers) is not tuple:
        raise ReleaseCandidateError(
            "candidate.unresolved_blockers must be an exact tuple at the terminal boundary"
        )
    exact_blockers = tuple(
        _exact_snapshot_text(value, name="candidate.unresolved_blocker")
        for value in unresolved_blockers
    )
    exact_artifacts = tuple(
        _snapshot_release_artifact(artifact)
        for artifact in artifacts
    )
    return ReleaseCandidateInput.create(
        release_id=_exact_snapshot_text(
            release_id,
            name="candidate.release_id",
        ),
        source_sha=_exact_snapshot_text(
            source_sha,
            name="candidate.source_sha",
        ),
        baseline_hash=_exact_snapshot_text(
            baseline_hash,
            name="candidate.baseline_hash",
        ),
        schema_contract_hash=_exact_snapshot_text(
            schema_contract_hash,
            name="candidate.schema_contract_hash",
        ),
        artifacts=exact_artifacts,
        unresolved_blockers=exact_blockers,
    )

def release_candidate_subject_requirement(
    candidate: ReleaseCandidateInput,
) -> str:
    """Return the signed requirement binding every release-authority claim."""

    candidate = _snapshot_release_candidate(candidate)
    subject = {
        "release_id": candidate.release_id,
        "source_sha": candidate.source_sha,
        "baseline_hash": candidate.baseline_hash,
        "schema_contract_hash": candidate.schema_contract_hash,
        "artifacts": [
            {
                "role": artifact.role,
                "artifact_id": artifact.artifact_id,
                "artifact_sha256": artifact.artifact_sha256,
                "source_sha": artifact.source_sha,
                "signature_status": artifact.signature_status,
                "evidence_status": artifact.evidence_status,
            }
            for artifact in sorted(
                candidate.artifacts,
                key=lambda item: item.role,
            )
        ],
        "unresolved_blockers": sorted(candidate.unresolved_blockers),
    }
    canonical = json.dumps(
        subject,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return (
        _QUALIFICATION_SUBJECT_REQUIREMENT_PREFIX
        + sha256(canonical).hexdigest()
    )


@dataclass(frozen=True)
class ReleaseCandidateDecision:
    status: str
    reasons: tuple[str, ...]
    manifest_json: str | None
    manifest_sha256: str | None
    qualification_attestation_id: str | None = None
    qualification_attestation_digest: str | None = None
    qualification_policy_id: str | None = None
    qualification_trust_root_id: str | None = None
    _verification_store: InitVar[ArtifactStore | None] = None
    _verification_root: InitVar[str | Path | None] = None
    _supply_chain_evidence: InitVar[SupplyChainEvidence | None] = None
    _supply_chain_receipt: InitVar[SignedQualificationAttestation | None] = None

    def __post_init__(
        self,
        _verification_store: ArtifactStore | None,
        _verification_root: str | Path | None,
        _supply_chain_evidence: SupplyChainEvidence | None,
        _supply_chain_receipt: SignedQualificationAttestation | None,
    ) -> None:
        if self.status not in {"FROZEN", "BLOCKED"}:
            raise ReleaseCandidateError("unsupported release-candidate status")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in self.reasons
        ):
            raise ReleaseCandidateError(
                "release-candidate reasons must be a tuple of non-empty strings"
            )
        if len(set(self.reasons)) != len(self.reasons):
            raise ReleaseCandidateError(
                "release-candidate reasons must be unique"
            )
        if self.status == "FROZEN":
            if self.reasons:
                raise ReleaseCandidateError(
                    "frozen release candidate cannot contain blocking reasons"
                )
            if self.manifest_json is None or self.manifest_sha256 is None:
                raise ReleaseCandidateError(
                    "frozen release candidate requires canonical manifest"
                )
            digest = _sha256(
                self.manifest_sha256,
                name="manifest_sha256",
            )
            try:
                manifest = json.loads(self.manifest_json)
            except (json.JSONDecodeError, TypeError) as error:
                raise ReleaseCandidateError(
                    "frozen release candidate manifest must be valid JSON"
                ) from error
            if type(manifest) is not dict:
                raise ReleaseCandidateError(
                    "frozen release candidate manifest must be a JSON object"
                )
            canonical = json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            if canonical != self.manifest_json:
                raise ReleaseCandidateError(
                    "frozen release candidate manifest must use canonical JSON"
                )
            actual_digest = (
                "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()
            )
            if digest != actual_digest:
                raise ReleaseCandidateError(
                    "frozen release candidate manifest digest does not match manifest"
                )
            if set(manifest) != {
                "release_id",
                "source_sha",
                "baseline_hash",
                "schema_contract_hash",
                "artifacts",
                "qualification",
                "supply_chain",
            }:
                raise ReleaseCandidateError(
                    "frozen release candidate manifest has unsupported structure"
                )
            manifest_release_id = _text(
                manifest.get("release_id"),
                name="manifest.release_id",
            )
            manifest_source_sha = _git_sha(
                manifest.get("source_sha"),
                name="manifest.source_sha",
            )
            manifest_baseline_hash = _sha256(
                manifest.get("baseline_hash"),
                name="manifest.baseline_hash",
            )
            manifest_schema_contract_hash = _sha256(
                manifest.get("schema_contract_hash"),
                name="manifest.schema_contract_hash",
            )
            artifacts_raw = manifest.get("artifacts")
            if not isinstance(artifacts_raw, list):
                raise ReleaseCandidateError(
                    "frozen release candidate artifacts must be a list"
                )
            artifact_fields = {
                "role",
                "artifact_id",
                "artifact_sha256",
                "source_sha",
                "signature_status",
                "evidence_status",
            }
            parsed_artifacts: list[ReleaseArtifactEvidence] = []
            for raw in artifacts_raw:
                if type(raw) is not dict or set(raw) != artifact_fields:
                    raise ReleaseCandidateError(
                        "frozen release candidate artifact structure is not canonical"
                    )
                try:
                    artifact = ReleaseArtifactEvidence.create(**raw)
                except (ReleaseCandidateError, TypeError) as error:
                    raise ReleaseCandidateError(
                        "frozen release candidate artifact is malformed"
                    ) from error
                canonical_artifact = {
                    "role": artifact.role,
                    "artifact_id": artifact.artifact_id,
                    "artifact_sha256": artifact.artifact_sha256,
                    "source_sha": artifact.source_sha,
                    "signature_status": artifact.signature_status,
                    "evidence_status": artifact.evidence_status,
                }
                if canonical_artifact != raw:
                    raise ReleaseCandidateError(
                        "frozen release candidate artifact values are not canonical"
                    )
                if artifact.source_sha != manifest_source_sha:
                    raise ReleaseCandidateError(
                        f"frozen release candidate artifact source mismatch: {artifact.role}"
                    )
                if artifact.evidence_status != "PASS":
                    raise ReleaseCandidateError(
                        f"frozen release candidate artifact is not PASS: {artifact.role}"
                    )
                if (
                    artifact.role in _SIGNED_BINARY_ROLES
                    and artifact.signature_status != "VERIFIED"
                ):
                    raise ReleaseCandidateError(
                        f"frozen release candidate signature is not verified: {artifact.role}"
                    )
                if (
                    artifact.role not in _SIGNED_BINARY_ROLES
                    and artifact.signature_status in {"MISSING", "INVALID"}
                ):
                    raise ReleaseCandidateError(
                        f"frozen release candidate signature state is unresolved: {artifact.role}"
                    )
                parsed_artifacts.append(artifact)

            artifact_roles = [item.role for item in parsed_artifacts]
            if len(set(artifact_roles)) != len(artifact_roles):
                raise ReleaseCandidateError(
                    "frozen release candidate contains duplicate artifact roles"
                )
            if set(artifact_roles) != _REQUIRED_ROLES:
                raise ReleaseCandidateError(
                    "frozen release candidate artifact role set is not canonical"
                )
            if artifact_roles != sorted(artifact_roles):
                raise ReleaseCandidateError(
                    "frozen release candidate artifacts are not in canonical role order"
                )

            windows_package = next(
                item for item in parsed_artifacts
                if item.role == "WINDOWS_PACKAGE"
            )
            dependency_rights = next(
                item for item in parsed_artifacts
                if item.role == "DEPENDENCY_RIGHTS"
            )
            supply_chain = manifest.get("supply_chain")
            supply_fields = {
                "qualification_id",
                "attestation_id",
                "attestation_digest",
                "policy_id",
                "trust_root_id",
                "subject_requirement",
                "release_artifact_id",
                "release_artifact_sha256",
                "dependency_rights_artifact_id",
                "dependency_rights_artifact_sha256",
            }
            if type(supply_chain) is not dict or set(supply_chain) != supply_fields:
                raise ReleaseCandidateError(
                    "frozen release candidate requires canonical WP-64 proof identity"
                )
            supply_observed = {
                "qualification_id": _text(
                    supply_chain.get("qualification_id"),
                    name="manifest.supply_chain.qualification_id",
                ),
                "attestation_id": _text(
                    supply_chain.get("attestation_id"),
                    name="manifest.supply_chain.attestation_id",
                ),
                "attestation_digest": _sha256(
                    supply_chain.get("attestation_digest"),
                    name="manifest.supply_chain.attestation_digest",
                ),
                "policy_id": _sha256(
                    supply_chain.get("policy_id"),
                    name="manifest.supply_chain.policy_id",
                ),
                "trust_root_id": _sha256(
                    supply_chain.get("trust_root_id"),
                    name="manifest.supply_chain.trust_root_id",
                ),
                "subject_requirement": _text(
                    supply_chain.get("subject_requirement"),
                    name="manifest.supply_chain.subject_requirement",
                ),
                "release_artifact_id": _artifact_id(
                    supply_chain.get("release_artifact_id"),
                    name="manifest.supply_chain.release_artifact_id",
                ),
                "release_artifact_sha256": _sha256(
                    supply_chain.get("release_artifact_sha256"),
                    name="manifest.supply_chain.release_artifact_sha256",
                ),
                "dependency_rights_artifact_id": _artifact_id(
                    supply_chain.get("dependency_rights_artifact_id"),
                    name="manifest.supply_chain.dependency_rights_artifact_id",
                ),
                "dependency_rights_artifact_sha256": _sha256(
                    supply_chain.get("dependency_rights_artifact_sha256"),
                    name="manifest.supply_chain.dependency_rights_artifact_sha256",
                ),
            }
            if (
                supply_observed["release_artifact_id"] != windows_package.artifact_id
                or supply_observed["release_artifact_sha256"]
                != windows_package.artifact_sha256
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate WP-64 proof is bound to a different Windows package"
                )
            if (
                supply_observed["dependency_rights_artifact_id"]
                != dependency_rights.artifact_id
                or supply_observed["dependency_rights_artifact_sha256"]
                != dependency_rights.artifact_sha256
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate dependency-rights artifact does not reference WP-64 proof"
                )
            qualification = manifest.get("qualification")
            if type(qualification) is not dict or set(qualification) != {
                "attestation_id",
                "attestation_digest",
                "policy_id",
                "trust_root_id",
                "receipt",
            }:
                raise ReleaseCandidateError(
                    "frozen release candidate requires independently verified attestation"
                )
            expected = {
                "attestation_id": self.qualification_attestation_id,
                "attestation_digest": self.qualification_attestation_digest,
                "policy_id": self.qualification_policy_id,
                "trust_root_id": self.qualification_trust_root_id,
            }
            observed = {
                key: qualification.get(key)
                for key in expected
            }
            if observed != expected or any(value is None for value in expected.values()):
                raise ReleaseCandidateError(
                    "frozen release candidate attestation identity mismatch"
                )
            try:
                receipt = parse_signed_qualification_attestation(
                    qualification.get("receipt")
                )
            except QualificationTrustError as error:
                raise ReleaseCandidateError(
                    "frozen release candidate qualification receipt is malformed"
                ) from error
            if (
                receipt.attestation.attestation_id
                != self.qualification_attestation_id
                or receipt.attestation.content_digest
                != self.qualification_attestation_digest
                or receipt.attestation.trust_root_id
                != self.qualification_trust_root_id
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate qualification receipt identity mismatch"
                )
            reconstructed_candidate = ReleaseCandidateInput.create(
                release_id=manifest_release_id,
                source_sha=manifest_source_sha,
                baseline_hash=manifest_baseline_hash,
                schema_contract_hash=manifest_schema_contract_hash,
                artifacts=tuple(parsed_artifacts),
                unresolved_blockers=(),
            )
            if not _frozen_receipt_structurally_covers_exact_candidate(
                receipt,
                reconstructed_candidate,
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate qualification receipt "
                    "does not cover exact artifact set"
                )
            if (
                type(_verification_store) is not ArtifactStore
                or _verification_root is None
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate requires canonical qualification "
                    "verification context"
                )
            windows_package = next(
                item
                for item in reconstructed_candidate.artifacts
                if item.role == "WINDOWS_PACKAGE"
            )
            try:
                accepted = verify_canonical_qualification_attestation(
                    receipt,
                    evidence_store=_verification_store,
                    evidence_root=_verification_root,
                    expected_source_sha=manifest_source_sha,
                    expected_domain=_QUALIFICATION_DOMAIN,
                    expected_gate=_QUALIFICATION_GATE,
                    expected_package_id=_QUALIFICATION_PACKAGE,
                    expected_protocol_id=_QUALIFICATION_PROTOCOL,
                    expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
                    expected_requirement_id=_QUALIFICATION_REQUIREMENT,
                    expected_release_artifact_id=windows_package.artifact_id,
                    expected_release_artifact_sha256=(
                        windows_package.artifact_sha256
                    ),
                )
            except (QualificationTrustError, TypeError, ValueError) as error:
                raise ReleaseCandidateError(
                    "frozen release candidate qualification receipt is not "
                    "canonically verified"
                ) from error
            if accepted.result != "PASS":
                raise ReleaseCandidateError(
                    "frozen release candidate qualification result is not PASS"
                )
            if not _qualification_covers_exact_candidate(
                accepted,
                reconstructed_candidate,
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate canonically verified qualification "
                    "does not cover exact artifact set"
                )
            if (
                accepted.attestation_id != self.qualification_attestation_id
                or accepted.attestation_digest
                != self.qualification_attestation_digest
                or accepted.policy_id != self.qualification_policy_id
                or accepted.trust_root_id != self.qualification_trust_root_id
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate canonical qualification identity mismatch"
                )
            if (
                type(_verification_store) is not ArtifactStore
                or _verification_root is None
                or type(_supply_chain_evidence) is not SupplyChainEvidence
                or type(_supply_chain_receipt) is not SignedQualificationAttestation
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate requires canonical WP-64 replay context"
                )
            try:
                supply_result = qualify_supply_chain(
                    _supply_chain_evidence,
                    evidence_store=_verification_store,
                    evidence_root=_verification_root,
                    trust_receipt=_supply_chain_receipt,
                )
            except (TypeError, ValueError) as error:
                raise ReleaseCandidateError(
                    "frozen release candidate WP-64 proof cannot be canonically replayed"
                ) from error
            supply_expected = {
                "qualification_id": supply_result.qualification_id,
                "attestation_id": supply_result.accepted_attestation_id,
                "attestation_digest": supply_result.accepted_attestation_digest,
                "policy_id": supply_result.accepted_policy_id,
                "trust_root_id": supply_result.accepted_trust_root_id,
                "subject_requirement": supply_result.subject_requirement,
                "release_artifact_id": supply_result.release_artifact_id,
                "release_artifact_sha256": supply_result.release_artifact_sha256,
                "dependency_rights_artifact_id": dependency_rights.artifact_id,
                "dependency_rights_artifact_sha256": dependency_rights.artifact_sha256,
            }
            if (
                supply_result.status != "PASS"
                or supply_observed != supply_expected
                or any(value is None for value in supply_expected.values())
            ):
                raise ReleaseCandidateError(
                    "frozen release candidate WP-64 proof identity is not canonical"
                )

        else:
            if not self.reasons:
                raise ReleaseCandidateError(
                    "blocked release candidate requires at least one reason"
                )
            if self.manifest_json is not None or self.manifest_sha256 is not None:
                raise ReleaseCandidateError(
                    "blocked release candidate cannot publish a frozen manifest"
                )
            if any(
                value is not None
                for value in (
                    self.qualification_attestation_id,
                    self.qualification_attestation_digest,
                    self.qualification_policy_id,
                    self.qualification_trust_root_id,
                )
            ):
                raise ReleaseCandidateError(
                    "blocked release candidate cannot retain accepted qualification"
                )


def _canonical_manifest(
    candidate: ReleaseCandidateInput,
    accepted: AcceptedQualificationAttestation,
    supply_chain: SupplyChainQualification,
) -> str:
    body = {
        "release_id": candidate.release_id,
        "source_sha": candidate.source_sha,
        "baseline_hash": candidate.baseline_hash,
        "schema_contract_hash": candidate.schema_contract_hash,
        "supply_chain": {
            "qualification_id": supply_chain.qualification_id,
            "attestation_id": supply_chain.accepted_attestation_id,
            "attestation_digest": supply_chain.accepted_attestation_digest,
            "policy_id": supply_chain.accepted_policy_id,
            "trust_root_id": supply_chain.accepted_trust_root_id,
            "subject_requirement": supply_chain.subject_requirement,
            "release_artifact_id": supply_chain.release_artifact_id,
            "release_artifact_sha256": supply_chain.release_artifact_sha256,
            "dependency_rights_artifact_id": next(
                artifact.artifact_id
                for artifact in candidate.artifacts
                if artifact.role == "DEPENDENCY_RIGHTS"
            ),
            "dependency_rights_artifact_sha256": next(
                artifact.artifact_sha256
                for artifact in candidate.artifacts
                if artifact.role == "DEPENDENCY_RIGHTS"
            ),
        },
        "qualification": {
            "attestation_id": accepted.attestation_id,
            "attestation_digest": accepted.attestation_digest,
            "policy_id": accepted.policy_id,
            "trust_root_id": accepted.trust_root_id,
            "receipt": {
                "attestation": json.loads(accepted.attestation_json),
                "signature_b64": accepted.signature_b64,
            },
        },
        "artifacts": [
            {
                "role": artifact.role,
                "artifact_id": artifact.artifact_id,
                "artifact_sha256": artifact.artifact_sha256,
                "source_sha": artifact.source_sha,
                "signature_status": artifact.signature_status,
                "evidence_status": artifact.evidence_status,
            }
            for artifact in sorted(
                candidate.artifacts,
                key=lambda item: item.role,
            )
        ],
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _stored_evidence_is_verified(
    read_snapshot,
    artifact: ReleaseArtifactEvidence,
) -> bool:
    """Verify exact stored bytes and declared release-evidence bindings.

    ArtifactStore is a content-integrity boundary. It does not authenticate who
    produced the evidence or who asserted PASS/VERIFIED metadata.
    """

    try:
        manifest, _raw = read_snapshot(artifact.artifact_id)
        if not isinstance(manifest.get("manifest_hash"), str):
            return False
        if manifest.get("sha256") != artifact.artifact_sha256:
            return False
        if manifest.get("media_type") != _RELEASE_ARTIFACT_MEDIA_TYPE:
            return False
        if manifest.get("source_refs") != [f"git:{artifact.source_sha}"]:
            return False
        if manifest.get("metadata") != {
            "evidence_kind": _RELEASE_EVIDENCE_KIND,
            "role": artifact.role,
            "source_sha": artifact.source_sha,
            "signature_status": artifact.signature_status,
            "evidence_status": artifact.evidence_status,
        }:
            return False
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _qualification_claims_cover_exact_candidate(
    evidence_refs,
    requirement_ids: tuple[str, ...],
    candidate: ReleaseCandidateInput,
) -> bool:
    """Compare canonical qualification claims to one reconstructed candidate.

    This helper is structural only. Authority-bearing live freeze decisions must
    pass claims from AcceptedQualificationAttestation, never directly from an
    unverified signed receipt.
    """

    expected = {
        (
            artifact.artifact_id,
            artifact.artifact_sha256,
            artifact.source_sha,
            _RELEASE_ARTIFACT_MEDIA_TYPE,
            _RELEASE_EVIDENCE_KIND,
        )
        for artifact in candidate.artifacts
    }
    observed = {
        (
            ref.artifact_id,
            ref.sha256,
            ref.source_sha,
            ref.media_type,
            ref.evidence_kind,
        )
        for ref in evidence_refs
    }
    if observed != expected:
        return False
    return release_candidate_subject_requirement(candidate) in requirement_ids


def _qualification_covers_exact_candidate(
    accepted: AcceptedQualificationAttestation,
    candidate: ReleaseCandidateInput,
) -> bool:
    """Authority-bearing coverage check for a freshly accepted attestation."""

    return _qualification_claims_cover_exact_candidate(
        accepted.evidence_refs,
        accepted.requirement_ids,
        candidate,
    )


def _frozen_receipt_structurally_covers_exact_candidate(
    receipt: SignedQualificationAttestation,
    candidate: ReleaseCandidateInput,
) -> bool:
    """Validate frozen-manifest self-consistency before trust verification.

    This check is deliberately structural. ReleaseCandidateDecision separately
    re-runs the canonical exact-source signature/evidence verifier before a
    FROZEN value can exist.
    """

    return _qualification_claims_cover_exact_candidate(
        receipt.attestation.evidence_refs,
        tuple(receipt.attestation.requirement_ids),
        candidate,
    )


def freeze_release_candidate(
    candidate: ReleaseCandidateInput,
    *,
    evidence_store: ArtifactStore | None = None,
    evidence_root: str | Path | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
    supply_chain_evidence: SupplyChainEvidence | None = None,
    supply_chain_receipt: SignedQualificationAttestation | None = None,
    qualification_policy: QualificationTrustPolicy | None = None,
    expected_policy_id: str | None = None,
    expected_policy_version: str | None = None,
) -> ReleaseCandidateDecision:
    """Freeze exact accepted evidence or fail closed without an RC manifest.

    PASS and VERIFIED fields are claims, not proof. ArtifactStore proves exact
    bytes/manifest bindings only. A FROZEN decision additionally requires the
    canonical signed qualification authority, pinned policy identity, exact
    WP-54 scope, exact delivered Windows package binding, and exact coverage of
    every candidate artifact.
    """

    candidate = _snapshot_release_candidate(candidate)
    if evidence_store is not None and type(evidence_store) is not ArtifactStore:
        raise TypeError(
            "evidence_store must be ArtifactStore (canonical exact type required)"
        )
    if qualification_receipt is not None and not isinstance(
        qualification_receipt, SignedQualificationAttestation
    ):
        raise TypeError(
            "qualification_receipt must be SignedQualificationAttestation"
        )
    if qualification_policy is not None and not isinstance(
        qualification_policy, QualificationTrustPolicy
    ):
        raise TypeError(
            "qualification_policy must be QualificationTrustPolicy"
        )

    reasons: list[str] = []
    accepted: AcceptedQualificationAttestation | None = None
    supply_chain_qualification: SupplyChainQualification | None = None
    trusted_read = None
    if evidence_store is not None and evidence_root is not None:
        try:
            trusted_read = trusted_authenticated_reader(
                evidence_root,
                publication_store=evidence_store,
            )
        except (
            ArtifactIntegrityError,
            FileNotFoundError,
            OSError,
            TypeError,
            ValueError,
        ):
            trusted_read = None
    if evidence_store is None:
        reasons.append("evidence_store_missing")
    by_role = {artifact.role: artifact for artifact in candidate.artifacts}

    for role in sorted(_REQUIRED_ROLES - set(by_role)):
        reasons.append(f"missing_required_artifact:{role}")

    # Legacy policy/pin arguments remain compatibility-only inputs. They never
    # select terminal release trust; exact-source canonical policy does.
    trust_inputs_present = all(
        value is not None
        for value in (
            evidence_store,
            evidence_root,
            qualification_receipt,
        )
    )
    if not trust_inputs_present:
        reasons.append("independent_evidence_trust_unavailable")
    else:
        windows_package = by_role.get("WINDOWS_PACKAGE")
        if windows_package is None:
            reasons.append("independent_evidence_trust_invalid")
        else:
            try:
                accepted = verify_canonical_qualification_attestation(
                    qualification_receipt,
                    evidence_store=evidence_store,
                    evidence_root=evidence_root,
                    expected_source_sha=candidate.source_sha,
                    expected_domain=_QUALIFICATION_DOMAIN,
                    expected_gate=_QUALIFICATION_GATE,
                    expected_package_id=_QUALIFICATION_PACKAGE,
                    expected_protocol_id=_QUALIFICATION_PROTOCOL,
                    expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
                    expected_requirement_id=_QUALIFICATION_REQUIREMENT,
                    expected_release_artifact_id=windows_package.artifact_id,
                    expected_release_artifact_sha256=windows_package.artifact_sha256,
                )
            except (QualificationTrustError, TypeError, ValueError):
                reasons.append("independent_evidence_trust_invalid")
            else:
                if accepted.result != "PASS":
                    reasons.append(
                        f"qualification_result_not_pass:{accepted.result}"
                    )
                elif not _qualification_covers_exact_candidate(
                    accepted, candidate
                ):
                    reasons.append("qualification_evidence_set_mismatch")

    windows_package = by_role.get("WINDOWS_PACKAGE")
    dependency_rights = by_role.get("DEPENDENCY_RIGHTS")
    if (
        windows_package is None
        or dependency_rights is None
        or supply_chain_evidence is None
        or supply_chain_receipt is None
        or evidence_store is None
        or evidence_root is None
    ):
        reasons.append("supply_chain_qualification_unavailable")
    else:
        try:
            supply_chain_qualification = qualify_supply_chain(
                supply_chain_evidence,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
                trust_receipt=supply_chain_receipt,
            )
        except (TypeError, ValueError):
            reasons.append("supply_chain_qualification_invalid")
        else:
            supply_identity = (
                supply_chain_qualification.accepted_attestation_id,
                supply_chain_qualification.accepted_attestation_digest,
                supply_chain_qualification.accepted_policy_id,
                supply_chain_qualification.accepted_trust_root_id,
                supply_chain_qualification.subject_requirement,
            )
            if supply_chain_qualification.status != "PASS":
                reasons.append(
                    "supply_chain_qualification_not_pass:"
                    + supply_chain_qualification.status
                )
            elif any(value is None for value in supply_identity):
                reasons.append("supply_chain_qualification_identity_missing")
            elif (
                supply_chain_qualification.release_artifact_id
                != windows_package.artifact_id
                or supply_chain_qualification.release_artifact_sha256
                != windows_package.artifact_sha256
            ):
                reasons.append("supply_chain_release_artifact_mismatch")

    for artifact in candidate.artifacts:
        if artifact.source_sha != candidate.source_sha:
            reasons.append(f"source_sha_mismatch:{artifact.role}")
        if artifact.evidence_status == "FAIL":
            reasons.append(f"evidence_failed:{artifact.role}")
        elif artifact.evidence_status == "INCONCLUSIVE":
            reasons.append(f"evidence_inconclusive:{artifact.role}")

        if (
            trusted_read is None
            or not _stored_evidence_is_verified(trusted_read, artifact)
        ):
            reasons.append(
                f"evidence_integrity_unverified:{artifact.role}"
            )
        if (
            artifact.role not in _SIGNED_BINARY_ROLES
            and artifact.signature_status in {"MISSING", "INVALID"}
        ):
            reasons.append(f"signature_status_unresolved:{artifact.role}")

    for role in sorted(_SIGNED_BINARY_ROLES):
        artifact = by_role.get(role)
        if artifact is not None and artifact.signature_status != "VERIFIED":
            reasons.append(f"signature_not_verified:{role}")

    for blocker in candidate.unresolved_blockers:
        reasons.append(f"unresolved_blocker:{blocker}")

    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return ReleaseCandidateDecision(
            status="BLOCKED",
            reasons=unique_reasons,
            manifest_json=None,
            manifest_sha256=None,
        )

    if (
        accepted is None
        or qualification_receipt is None
        or supply_chain_qualification is None
        or supply_chain_evidence is None
        or supply_chain_receipt is None
    ):
        raise ReleaseCandidateError(
            "release freeze reached terminal path without accepted qualification"
        )
    manifest = _canonical_manifest(
        candidate,
        accepted,
        supply_chain_qualification,
    )
    digest = "sha256:" + sha256(manifest.encode("utf-8")).hexdigest()
    return ReleaseCandidateDecision(
        status="FROZEN",
        reasons=(),
        manifest_json=manifest,
        manifest_sha256=digest,
        qualification_attestation_id=accepted.attestation_id,
        qualification_attestation_digest=accepted.attestation_digest,
        qualification_policy_id=accepted.policy_id,
        qualification_trust_root_id=accepted.trust_root_id,
        _verification_store=evidence_store,
        _verification_root=evidence_root,
        _supply_chain_evidence=supply_chain_evidence,
        _supply_chain_receipt=supply_chain_receipt,
    )
