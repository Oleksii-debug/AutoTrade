"""Exact-release supply-chain qualification without granting release authority.

Consumes already-produced SBOM/provenance/rights/advisory evidence. It does not invent
licenses, suppress vulnerabilities, or treat an architecture-time review as approval of
another release commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json


_PASS = "PASS"
_FAIL = "FAIL"
_INCONCLUSIVE = "INCONCLUSIVE"


def _sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 hex>")
    digest = value[7:]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError(f"{name} must use sha256:<64 hex>")
    return value.lower()


def _git_sha(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value.lower()):
        raise ValueError(f"{name} must be a 40-hex commit SHA")
    return value.lower()


@dataclass(frozen=True)
class ComponentEvidence:
    component_id: str
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

    def __post_init__(self) -> None:
        if not self.component_id.strip() or not self.version.strip() or not self.source_revision.strip():
            raise ValueError("component identity/version/source revision are required")
        _sha256(self.declared_artifact_hash, "declared_artifact_hash")
        _sha256(self.observed_artifact_hash, "observed_artifact_hash")
        if self.license_status not in {"APPROVED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("license_status must be explicit")
        if self.distribution_rights not in {"APPROVED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("distribution_rights must be explicit")
        if self.advisory_status not in {"CLEAR", "ALLOWLISTED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("advisory_status must be explicit")
        _git_sha(self.reviewed_for_release_sha, "reviewed_for_release_sha")


@dataclass(frozen=True)
class ModelDataRightsEvidence:
    artifact_id: str
    artifact_hash: str
    use_scope: str
    rights_status: str
    reviewed_for_release_sha: str

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or not self.use_scope.strip():
            raise ValueError("artifact identity and use scope are required")
        _sha256(self.artifact_hash, "artifact_hash")
        if self.rights_status not in {"APPROVED", "BLOCKED", "UNKNOWN"}:
            raise ValueError("rights_status must be explicit")
        _git_sha(self.reviewed_for_release_sha, "reviewed_for_release_sha")


@dataclass(frozen=True)
class SupplyChainEvidence:
    release_commit_sha: str
    built_from_commit_sha: str
    sbom_hash: str
    provenance_hash: str
    dependency_lock_hash: str
    distributed_component_ids: tuple[str, ...]
    components: tuple[ComponentEvidence, ...]
    model_data_rights: tuple[ModelDataRightsEvidence, ...]

    def __post_init__(self) -> None:
        _git_sha(self.release_commit_sha, "release_commit_sha")
        _git_sha(self.built_from_commit_sha, "built_from_commit_sha")
        _sha256(self.sbom_hash, "sbom_hash")
        _sha256(self.provenance_hash, "provenance_hash")
        _sha256(self.dependency_lock_hash, "dependency_lock_hash")
        if len(self.distributed_component_ids) != len(set(self.distributed_component_ids)):
            raise ValueError("distributed component inventory contains duplicates")
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


def qualify_supply_chain(evidence: SupplyChainEvidence) -> SupplyChainQualification:
    checks: list[tuple[str, str]] = []
    reasons: list[str] = []

    def record(name: str, status: str, reason: str | None = None) -> None:
        checks.append((name, status))
        if reason and status != _PASS:
            reasons.append(reason)

    exact_head = evidence.release_commit_sha == evidence.built_from_commit_sha
    record("exact_release_head", _PASS if exact_head else _FAIL, "SUPPLY_CHAIN.BUILD_SHA_MISMATCH")

    by_id = {item.component_id: item for item in evidence.components}
    inventory = set(evidence.distributed_component_ids)
    missing = sorted(inventory - set(by_id))
    extras = sorted(set(by_id) - inventory)
    record(
        "sbom_inventory",
        _PASS if not missing and not extras else _FAIL,
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
    for item in evidence.model_data_rights:
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
            "sbom": evidence.sbom_hash,
            "provenance": evidence.provenance_hash,
            "lock": evidence.dependency_lock_hash,
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
