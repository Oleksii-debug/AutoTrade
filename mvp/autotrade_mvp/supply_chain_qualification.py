"""Exact-release supply-chain qualification without granting release authority.

Consumes already-produced SBOM/provenance/rights/advisory evidence. It does not invent
licenses, suppress vulnerabilities, or treat an architecture-time review as approval of
another release commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from uuid import UUID

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)


_PASS = "PASS"
_FAIL = "FAIL"
_INCONCLUSIVE = "INCONCLUSIVE"

_SBOM_MEDIA_TYPE = "application/vnd.autotrade.sbom"
_PROVENANCE_MEDIA_TYPE = "application/vnd.autotrade.provenance"
_DEPENDENCY_LOCK_MEDIA_TYPE = "application/vnd.autotrade.dependency-lock"
_COMPONENT_MEDIA_TYPE = "application/vnd.autotrade.distributed-component"
_RIGHTS_MEDIA_TYPE = "application/vnd.autotrade.rights-evidence"
_ADVISORY_EXCEPTION_MEDIA_TYPE = "application/vnd.autotrade.advisory-exception"


def _artifact_id(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UUID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError(f"{name} must be a UUID") from error


def _sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    digest = value[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


def _git_sha(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} must be a 40-character lowercase hex commit SHA")
    return value


@dataclass(frozen=True)
class ComponentEvidence:
    component_id: str
    artifact_id: str
    version: str
    declared_artifact_hash: str
    observed_artifact_hash: str
    source_revision: str
    license_status: str
    distribution_rights: str
    advisory_status: str
    notice_required: bool
    notice_present: bool
    reviewed_for_release_sha: str
    advisory_exception_id: str | None = None
    advisory_exception_hash: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.component_id, "component_id"),
            (self.version, "version"),
            (self.source_revision, "source_revision"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if type(self.notice_required) is not bool or type(self.notice_present) is not bool:
            raise TypeError("notice flags must be boolean")
        _artifact_id(self.artifact_id, "artifact_id")
        _sha256(self.declared_artifact_hash, "declared_artifact_hash")
        _sha256(self.observed_artifact_hash, "observed_artifact_hash")
        if self.license_status not in {"APPROVED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("license_status must be explicit")
        if self.distribution_rights not in {"APPROVED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("distribution_rights must be explicit")
        if self.advisory_status not in {"CLEAR", "ALLOWLISTED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("advisory_status must be explicit")
        if self.advisory_status == "ALLOWLISTED":
            if self.advisory_exception_id is None:
                raise ValueError("ALLOWLISTED advisory status requires advisory_exception_id")
            _artifact_id(self.advisory_exception_id, "advisory_exception_id")
            if self.advisory_exception_hash is None:
                raise ValueError("ALLOWLISTED advisory status requires advisory_exception_hash")
            _sha256(self.advisory_exception_hash, "advisory_exception_hash")
        elif self.advisory_exception_id is not None or self.advisory_exception_hash is not None:
            raise ValueError("advisory exception evidence is valid only for ALLOWLISTED status")
        _git_sha(self.reviewed_for_release_sha, "reviewed_for_release_sha")


@dataclass(frozen=True)
class ModelDataRightsEvidence:
    artifact_id: str
    artifact_hash: str
    use_scope: str
    rights_status: str
    reviewed_for_release_sha: str

    def __post_init__(self) -> None:
        _artifact_id(self.artifact_id, "artifact_id")
        if not isinstance(self.use_scope, str) or not self.use_scope.strip():
            raise ValueError("use_scope is required")
        _sha256(self.artifact_hash, "artifact_hash")
        if self.rights_status not in {"APPROVED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("rights_status must be explicit")
        _git_sha(self.reviewed_for_release_sha, "reviewed_for_release_sha")


@dataclass(frozen=True)
class SupplyChainEvidence:
    release_commit_sha: str
    built_from_commit_sha: str
    sbom_artifact_id: str
    sbom_hash: str
    provenance_artifact_id: str
    provenance_hash: str
    dependency_lock_artifact_id: str
    dependency_lock_hash: str
    sbom_reviewed_for_release_sha: str
    provenance_reviewed_for_release_sha: str
    dependency_lock_reviewed_for_release_sha: str
    distributed_component_ids: tuple[str, ...]
    sbom_component_ids: tuple[str, ...]
    components: tuple[ComponentEvidence, ...]
    model_data_rights: tuple[ModelDataRightsEvidence, ...]

    def __post_init__(self) -> None:
        _git_sha(self.release_commit_sha, "release_commit_sha")
        _git_sha(self.built_from_commit_sha, "built_from_commit_sha")
        _artifact_id(self.sbom_artifact_id, "sbom_artifact_id")
        _artifact_id(self.provenance_artifact_id, "provenance_artifact_id")
        _artifact_id(self.dependency_lock_artifact_id, "dependency_lock_artifact_id")
        _sha256(self.sbom_hash, "sbom_hash")
        _sha256(self.provenance_hash, "provenance_hash")
        _sha256(self.dependency_lock_hash, "dependency_lock_hash")
        _git_sha(self.sbom_reviewed_for_release_sha, "sbom_reviewed_for_release_sha")
        _git_sha(self.provenance_reviewed_for_release_sha, "provenance_reviewed_for_release_sha")
        _git_sha(
            self.dependency_lock_reviewed_for_release_sha,
            "dependency_lock_reviewed_for_release_sha",
        )
        if not isinstance(self.distributed_component_ids, tuple):
            raise TypeError("distributed_component_ids must be a tuple")
        if not isinstance(self.sbom_component_ids, tuple):
            raise TypeError("sbom_component_ids must be a tuple")
        if not isinstance(self.components, tuple) or any(
            not isinstance(item, ComponentEvidence) for item in self.components
        ):
            raise TypeError("components must be a tuple of ComponentEvidence")
        if not isinstance(self.model_data_rights, tuple) or any(
            not isinstance(item, ModelDataRightsEvidence) for item in self.model_data_rights
        ):
            raise TypeError("model_data_rights must be a tuple of ModelDataRightsEvidence")
        if any(not isinstance(item, str) or not item.strip() for item in self.distributed_component_ids):
            raise ValueError("distributed component inventory contains an invalid id")
        if any(not isinstance(item, str) or not item.strip() for item in self.sbom_component_ids):
            raise ValueError("SBOM component inventory contains an invalid id")
        if len(self.distributed_component_ids) != len(set(self.distributed_component_ids)):
            raise ValueError("distributed component inventory contains duplicates")
        if len(self.sbom_component_ids) != len(set(self.sbom_component_ids)):
            raise ValueError("SBOM component inventory contains duplicates")
        ids = [item.component_id for item in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("component evidence contains duplicate ids")
        rights_ids = [item.artifact_id for item in self.model_data_rights]
        if len(rights_ids) != len(set(rights_ids)):
            raise ValueError("model/data rights evidence contains duplicate ids")


@dataclass(frozen=True)
class SupplyChainQualification:
    qualification_id: str
    status: str
    checks: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]
    release_authority: bool = False


def _store_artifact_matches(
    store: ArtifactStore,
    *,
    artifact_id: str,
    artifact_hash: str,
    media_type: str,
    release_sha: str,
    metadata: dict[str, object],
) -> bool:
    """Verify exact immutable bytes and declared bindings through ArtifactStore.\n\n    ArtifactStore is an integrity boundary, not an independent trust anchor: the\n    caller that opens a store may also have populated it. Producer/authenticator\n    trust is therefore evaluated separately and must remain fail-closed until a\n    qualified attestation boundary exists.\n    """

    try:
        manifest = store.load_manifest(artifact_id)
        if not isinstance(manifest.get("manifest_hash"), str):
            return False
        if manifest.get("sha256") != artifact_hash:
            return False
        if manifest.get("media_type") != media_type:
            return False
        if manifest.get("source_refs") != [f"git:{release_sha}"]:
            return False
        if manifest.get("metadata") != metadata:
            return False
        store.read_bytes(artifact_id)
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def qualify_supply_chain(
    evidence: SupplyChainEvidence,
    *,
    evidence_store: ArtifactStore | None = None,
) -> SupplyChainQualification:
    if not isinstance(evidence, SupplyChainEvidence):
        raise TypeError("evidence must be SupplyChainEvidence")
    if evidence_store is not None and not isinstance(evidence_store, ArtifactStore):
        raise TypeError("evidence_store must be ArtifactStore")
    checks: list[tuple[str, str]] = []
    reasons: list[str] = []

    def record(name: str, status: str, reason: str | None = None) -> None:
        checks.append((name, status))
        if reason and status != _PASS:
            reasons.append(reason)

    immutable_checks: list[tuple[str, bool]] = []
    if evidence_store is not None:
        immutable_checks.extend(
            (
                (
                    "sbom",
                    _store_artifact_matches(
                        evidence_store,
                        artifact_id=evidence.sbom_artifact_id,
                        artifact_hash=evidence.sbom_hash,
                        media_type=_SBOM_MEDIA_TYPE,
                        release_sha=evidence.release_commit_sha,
                        metadata={
                            "evidence_kind": "SBOM",
                            "release_sha": evidence.release_commit_sha,
                        },
                    ),
                ),
                (
                    "provenance",
                    _store_artifact_matches(
                        evidence_store,
                        artifact_id=evidence.provenance_artifact_id,
                        artifact_hash=evidence.provenance_hash,
                        media_type=_PROVENANCE_MEDIA_TYPE,
                        release_sha=evidence.release_commit_sha,
                        metadata={
                            "evidence_kind": "PROVENANCE",
                            "release_sha": evidence.release_commit_sha,
                        },
                    ),
                ),
                (
                    "dependency_lock",
                    _store_artifact_matches(
                        evidence_store,
                        artifact_id=evidence.dependency_lock_artifact_id,
                        artifact_hash=evidence.dependency_lock_hash,
                        media_type=_DEPENDENCY_LOCK_MEDIA_TYPE,
                        release_sha=evidence.release_commit_sha,
                        metadata={
                            "evidence_kind": "DEPENDENCY_LOCK",
                            "release_sha": evidence.release_commit_sha,
                        },
                    ),
                ),
            )
        )
        for item in evidence.components:
            immutable_checks.append(
                (
                    "component:" + item.component_id,
                    _store_artifact_matches(
                        evidence_store,
                        artifact_id=item.artifact_id,
                        artifact_hash=item.observed_artifact_hash,
                        media_type=_COMPONENT_MEDIA_TYPE,
                        release_sha=evidence.release_commit_sha,
                        metadata={
                            "evidence_kind": "DISTRIBUTED_COMPONENT",
                            "component_id": item.component_id,
                            "version": item.version,
                            "release_sha": evidence.release_commit_sha,
                        },
                    ),
                )
            )
            if item.advisory_status == "ALLOWLISTED":
                assert item.advisory_exception_id is not None
                assert item.advisory_exception_hash is not None
                immutable_checks.append(
                    (
                        "advisory_exception:" + item.component_id,
                        _store_artifact_matches(
                            evidence_store,
                            artifact_id=item.advisory_exception_id,
                            artifact_hash=item.advisory_exception_hash,
                            media_type=_ADVISORY_EXCEPTION_MEDIA_TYPE,
                            release_sha=evidence.release_commit_sha,
                            metadata={
                                "evidence_kind": "ADVISORY_EXCEPTION",
                                "component_id": item.component_id,
                                "release_sha": evidence.release_commit_sha,
                            },
                        ),
                    )
                )
        for item in evidence.model_data_rights:
            immutable_checks.append(
                (
                    "rights:" + item.artifact_id,
                    _store_artifact_matches(
                        evidence_store,
                        artifact_id=item.artifact_id,
                        artifact_hash=item.artifact_hash,
                        media_type=_RIGHTS_MEDIA_TYPE,
                        release_sha=evidence.release_commit_sha,
                        metadata={
                            "evidence_kind": "MODEL_DATA_RIGHTS",
                            "use_scope": item.use_scope,
                            "release_sha": evidence.release_commit_sha,
                        },
                    ),
                )
            )

    if evidence_store is None:
        record(
            "immutable_evidence_bundle",
            _INCONCLUSIVE,
            "SUPPLY_CHAIN.EVIDENCE_STORE_MISSING",
        )
    else:
        for label, verified in immutable_checks:
            record(
                "immutable:" + label,
                _PASS if verified else _INCONCLUSIVE,
                "SUPPLY_CHAIN.IMMUTABLE_ARTIFACT_UNVERIFIED:" + label,
            )
        record(
            "immutable_evidence_bundle",
            _PASS if immutable_checks and all(value for _, value in immutable_checks)
            else _INCONCLUSIVE,
            "SUPPLY_CHAIN.EVIDENCE_UNVERIFIED",
        )

    # Content-addressed storage can prove that exact bytes and metadata exist and
    # have not changed. It cannot prove who produced or independently verified
    # those assertions because the caller may populate an ArtifactStore itself.
    # Until WP-64 has a qualified authenticated/signed attestation boundary,
    # integrity evidence alone must never elevate supply-chain qualification to PASS.
    record(
        "independent_evidence_trust",
        _INCONCLUSIVE,
        "SUPPLY_CHAIN.TRUST_ANCHOR_UNAVAILABLE",
    )

    exact_head = evidence.release_commit_sha == evidence.built_from_commit_sha
    record("exact_release_head", _PASS if exact_head else _FAIL, "SUPPLY_CHAIN.BUILD_SHA_MISMATCH")

    for name, reviewed_sha in (
        ("sbom", evidence.sbom_reviewed_for_release_sha),
        ("provenance", evidence.provenance_reviewed_for_release_sha),
        ("dependency_lock", evidence.dependency_lock_reviewed_for_release_sha),
    ):
        bound = reviewed_sha == evidence.release_commit_sha
        record(
            name + ":release_binding",
            _PASS if bound else _FAIL,
            "SUPPLY_CHAIN.STALE_" + name.upper() + "_REVIEW",
        )

    by_id = {item.component_id: item for item in evidence.components}
    inventory = set(evidence.distributed_component_ids)
    sbom_inventory = set(evidence.sbom_component_ids)
    component_inventory = set(by_id)
    record(
        "sbom_inventory",
        _PASS
        if sbom_inventory == inventory and component_inventory == inventory
        else _FAIL,
        "SUPPLY_CHAIN.SBOM_INVENTORY_MISMATCH",
    )

    for component_id in sorted(inventory & set(by_id)):
        item = by_id[component_id]
        prefix = "component:" + component_id
        record(
            prefix + ":artifact_hash",
            _PASS if item.declared_artifact_hash == item.observed_artifact_hash else _FAIL,
            "SUPPLY_CHAIN.ARTIFACT_HASH_MISMATCH:" + component_id,
        )
        release_bound = item.reviewed_for_release_sha == evidence.release_commit_sha
        record(
            prefix + ":release_binding",
            _PASS if release_bound else _FAIL,
            "SUPPLY_CHAIN.STALE_REVIEW:" + component_id,
        )

        license_state = _PASS if item.license_status == "APPROVED" else (_FAIL if item.license_status == "BLOCKED" else _INCONCLUSIVE)
        record(prefix + ":license", license_state, "SUPPLY_CHAIN.LICENSE_" + item.license_status + ":" + component_id)
        rights_state = _PASS if item.distribution_rights == "APPROVED" else (_FAIL if item.distribution_rights == "BLOCKED" else _INCONCLUSIVE)
        record(prefix + ":distribution_rights", rights_state, "SUPPLY_CHAIN.RIGHTS_" + item.distribution_rights + ":" + component_id)
        advisory_state = _PASS if item.advisory_status in {"CLEAR", "ALLOWLISTED"} else (_FAIL if item.advisory_status == "BLOCKED" else _INCONCLUSIVE)
        record(prefix + ":advisory", advisory_state, "SUPPLY_CHAIN.ADVISORY_" + item.advisory_status + ":" + component_id)
        notice_ok = (not item.notice_required) or item.notice_present
        record(prefix + ":notice", _PASS if notice_ok else _FAIL, "SUPPLY_CHAIN.MISSING_NOTICE:" + component_id)

    if not evidence.model_data_rights:
        record("model_data_rights", _INCONCLUSIVE, "SUPPLY_CHAIN.MODEL_DATA_RIGHTS_MISSING")
    for item in sorted(evidence.model_data_rights, key=lambda value: value.artifact_id):
        prefix = "rights:" + item.artifact_id
        bound = item.reviewed_for_release_sha == evidence.release_commit_sha
        record(prefix + ":release_binding", _PASS if bound else _FAIL, "SUPPLY_CHAIN.STALE_RIGHTS_REVIEW:" + item.artifact_id)
        state = _PASS if item.rights_status == "APPROVED" else (_FAIL if item.rights_status == "BLOCKED" else _INCONCLUSIVE)
        record(prefix + ":rights", state, "SUPPLY_CHAIN.MODEL_DATA_RIGHTS_" + item.rights_status + ":" + item.artifact_id)

    has_fail = any(status == _FAIL for _, status in checks)
    has_inconclusive = any(status == _INCONCLUSIVE for _, status in checks)
    status = _FAIL if has_fail else (_INCONCLUSIVE if has_inconclusive else _PASS)
    canonical = json.dumps(
        {
            "release": evidence.release_commit_sha,
            "built_from": evidence.built_from_commit_sha,
            "sbom_artifact_id": evidence.sbom_artifact_id,
            "sbom": evidence.sbom_hash,
            "provenance_artifact_id": evidence.provenance_artifact_id,
            "provenance": evidence.provenance_hash,
            "dependency_lock_artifact_id": evidence.dependency_lock_artifact_id,
            "lock": evidence.dependency_lock_hash,
            "sbom_reviewed_for_release_sha": evidence.sbom_reviewed_for_release_sha,
            "provenance_reviewed_for_release_sha": evidence.provenance_reviewed_for_release_sha,
            "dependency_lock_reviewed_for_release_sha": evidence.dependency_lock_reviewed_for_release_sha,
            "distributed_component_ids": sorted(evidence.distributed_component_ids),
            "sbom_component_ids": sorted(evidence.sbom_component_ids),
            "components": [
                {
                    "component_id": item.component_id,
                    "artifact_id": item.artifact_id,
                    "version": item.version,
                    "declared_artifact_hash": item.declared_artifact_hash,
                    "observed_artifact_hash": item.observed_artifact_hash,
                    "source_revision": item.source_revision,
                    "license_status": item.license_status,
                    "distribution_rights": item.distribution_rights,
                    "advisory_status": item.advisory_status,
                    "advisory_exception_id": item.advisory_exception_id,
                    "advisory_exception_hash": item.advisory_exception_hash,
                    "notice_required": item.notice_required,
                    "notice_present": item.notice_present,
                    "reviewed_for_release_sha": item.reviewed_for_release_sha,
                }
                for item in sorted(evidence.components, key=lambda value: value.component_id)
            ],
            "model_data_rights": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_hash": item.artifact_hash,
                    "use_scope": item.use_scope,
                    "rights_status": item.rights_status,
                    "reviewed_for_release_sha": item.reviewed_for_release_sha,
                }
                for item in sorted(evidence.model_data_rights, key=lambda value: value.artifact_id)
            ],
            "checks": checks,
            "reasons": sorted(set(reasons)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return SupplyChainQualification(
        "supply-" + sha256(canonical.encode("utf-8")).hexdigest()[:32],
        status,
        tuple(checks),
        tuple(dict.fromkeys(reasons)),
    )
