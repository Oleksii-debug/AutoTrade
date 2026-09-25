"""Exact-evidence freeze gate for a release-candidate manifest.

This module is not a packager, signer, SBOM generator, accessibility authority
or compatibility test runner. It consumes the exact outputs of those canonical
authorities and freezes a deterministic release-candidate manifest only when all
required evidence belongs to the same source SHA and is explicitly passing.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Callable, Sequence


class ReleaseCandidateError(ValueError):
    """Raised when release-candidate evidence is malformed."""


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
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


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCandidateError(f"{name} is required")
    return value.strip()


def _git_sha(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if text != text.lower() or _GIT_SHA.fullmatch(text) is None:
        raise ReleaseCandidateError(
            f"{name} must be a 40-character lowercase git SHA"
        )
    return text


def _sha256(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    if text != text.lower() or _SHA256.fullmatch(text) is None:
        raise ReleaseCandidateError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return text


@dataclass(frozen=True)
class ReleaseArtifactEvidence:
    role: str
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


@dataclass(frozen=True)
class ReleaseCandidateDecision:
    status: str
    reasons: tuple[str, ...]
    manifest_json: str | None
    manifest_sha256: str | None

    def __post_init__(self) -> None:
        if self.status not in {"FROZEN", "BLOCKED"}:
            raise ReleaseCandidateError("unsupported release-candidate status")
        if self.status == "FROZEN":
            if self.reasons:
                raise ReleaseCandidateError(
                    "frozen release candidate cannot contain blocking reasons"
                )
            if self.manifest_json is None or self.manifest_sha256 is None:
                raise ReleaseCandidateError(
                    "frozen release candidate requires canonical manifest"
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


def _canonical_manifest(candidate: ReleaseCandidateInput) -> str:
    body = {
        "release_id": candidate.release_id,
        "source_sha": candidate.source_sha,
        "baseline_hash": candidate.baseline_hash,
        "schema_contract_hash": candidate.schema_contract_hash,
        "artifacts": [
            {
                "role": artifact.role,
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


def freeze_release_candidate(
    candidate: ReleaseCandidateInput,
    *,
    verify_evidence: Callable[[ReleaseArtifactEvidence], bool] | None = None,
) -> ReleaseCandidateDecision:
    """Freeze exact accepted evidence or fail closed without an RC manifest.

    PASS and VERIFIED fields are evidence claims, not proof by themselves.
    A release candidate can freeze only when an independent verifier resolves
    every exact artifact record successfully.
    """

    if not isinstance(candidate, ReleaseCandidateInput):
        raise TypeError("candidate must be ReleaseCandidateInput")
    if verify_evidence is not None and not callable(verify_evidence):
        raise TypeError("verify_evidence must be callable")

    reasons: list[str] = []
    if verify_evidence is None:
        reasons.append("independent_evidence_verifier_missing")
    by_role = {artifact.role: artifact for artifact in candidate.artifacts}

    for role in sorted(_REQUIRED_ROLES - set(by_role)):
        reasons.append(f"missing_required_artifact:{role}")

    for artifact in candidate.artifacts:
        if artifact.source_sha != candidate.source_sha:
            reasons.append(f"source_sha_mismatch:{artifact.role}")
        if artifact.evidence_status == "FAIL":
            reasons.append(f"evidence_failed:{artifact.role}")
        elif artifact.evidence_status == "INCONCLUSIVE":
            reasons.append(f"evidence_inconclusive:{artifact.role}")

        if verify_evidence is not None:
            try:
                independently_verified = verify_evidence(artifact)
            except Exception:
                independently_verified = False
            if independently_verified is not True:
                reasons.append(
                    f"evidence_not_independently_verified:{artifact.role}"
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

    manifest = _canonical_manifest(candidate)
    digest = "sha256:" + sha256(manifest.encode("utf-8")).hexdigest()
    return ReleaseCandidateDecision(
        status="FROZEN",
        reasons=(),
        manifest_json=manifest,
        manifest_sha256=digest,
    )
