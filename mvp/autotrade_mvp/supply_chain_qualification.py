"""Exact-release supply-chain qualification without granting release authority.

Consumes already-produced SBOM/provenance/rights/advisory evidence. It does not invent
licenses, suppress vulnerabilities, or treat an architecture-time review as approval of
another release commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from autotrade_runtime.strict_json import strict_json_loads

from mvp.autotrade_mvp.qualification_attestation import (
    QualificationTrustError,
    QualificationTrustUnavailable,
    SignedQualificationAttestation,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)


_PASS = "PASS"
_FAIL = "FAIL"
_INCONCLUSIVE = "INCONCLUSIVE"
_ALLOWED_QUALIFICATION_STATUSES = frozenset({_PASS, _FAIL, _INCONCLUSIVE})

_SBOM_MEDIA_TYPE = "application/vnd.autotrade.sbom"
_PROVENANCE_MEDIA_TYPE = "application/vnd.autotrade.provenance"
_DEPENDENCY_LOCK_MEDIA_TYPE = "application/vnd.autotrade.dependency-lock"
_COMPONENT_MEDIA_TYPE = "application/vnd.autotrade.distributed-component"
_RIGHTS_MEDIA_TYPE = "application/vnd.autotrade.rights-evidence"
_ADVISORY_EXCEPTION_MEDIA_TYPE = "application/vnd.autotrade.advisory-exception"
SUPPLY_CHAIN_PROOF_MEDIA_TYPE = "application/vnd.autotrade.supply-chain-proof+json"
SUPPLY_CHAIN_PROOF_EVIDENCE_KIND = "AUTOTRADE_WP64_SUPPLY_CHAIN_PROOF_V1"
_SUPPLY_CHAIN_PROOF_SCHEMA_VERSION = "1.0.0"
_QUALIFICATION_SUBJECT_REQUIREMENT_PREFIX = "supply-chain-subject-sha256:"


def _required_text(value: str, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _artifact_id(value: str, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a UUID")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError(f"{name} must be a UUID") from error


def _sha256(value: str, name: str) -> str:
    if type(value) is not str or not value.startswith("sha256:"):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    digest = value[7:]
    if len(digest) != 64 or any(
        c not in "0123456789abcdef" for c in digest
    ):
        raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
    return value


def _git_sha(value: str, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(
            f"{name} must be a 40-character lowercase hex commit SHA"
        )
    return value


def _choice(value: str, *, name: str, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError(f"{name} must be explicit")
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
            _required_text(value, name)
        if (
            type(self.notice_required) is not bool
            or type(self.notice_present) is not bool
        ):
            raise TypeError("notice flags must be boolean")
        _artifact_id(self.artifact_id, "artifact_id")
        _sha256(self.declared_artifact_hash, "declared_artifact_hash")
        _sha256(self.observed_artifact_hash, "observed_artifact_hash")
        _choice(
            self.license_status,
            name="license_status",
            allowed=frozenset({"APPROVED", "BLOCKED", "UNKNOWN"}),
        )
        _choice(
            self.distribution_rights,
            name="distribution_rights",
            allowed=frozenset({"APPROVED", "BLOCKED", "UNKNOWN"}),
        )
        _choice(
            self.advisory_status,
            name="advisory_status",
            allowed=frozenset({"CLEAR", "ALLOWLISTED", "BLOCKED", "UNKNOWN"}),
        )
        if self.advisory_status == "ALLOWLISTED":
            if self.advisory_exception_id is None:
                raise ValueError(
                    "ALLOWLISTED advisory status requires advisory_exception_id"
                )
            _artifact_id(
                self.advisory_exception_id,
                "advisory_exception_id",
            )
            if self.advisory_exception_hash is None:
                raise ValueError(
                    "ALLOWLISTED advisory status requires advisory_exception_hash"
                )
            _sha256(
                self.advisory_exception_hash,
                "advisory_exception_hash",
            )
        elif (
            self.advisory_exception_id is not None
            or self.advisory_exception_hash is not None
        ):
            raise ValueError(
                "advisory exception evidence is valid only for ALLOWLISTED status"
            )
        _git_sha(
            self.reviewed_for_release_sha,
            "reviewed_for_release_sha",
        )


@dataclass(frozen=True)
class ModelDataRightsEvidence:
    artifact_id: str
    artifact_hash: str
    use_scope: str
    rights_status: str
    reviewed_for_release_sha: str

    def __post_init__(self) -> None:
        _artifact_id(self.artifact_id, "artifact_id")
        _required_text(self.use_scope, "use_scope")
        _sha256(self.artifact_hash, "artifact_hash")
        _choice(
            self.rights_status,
            name="rights_status",
            allowed=frozenset({"APPROVED", "BLOCKED", "UNKNOWN"}),
        )
        _git_sha(
            self.reviewed_for_release_sha,
            "reviewed_for_release_sha",
        )


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
        _artifact_id(
            self.dependency_lock_artifact_id,
            "dependency_lock_artifact_id",
        )
        _sha256(self.sbom_hash, "sbom_hash")
        _sha256(self.provenance_hash, "provenance_hash")
        _sha256(self.dependency_lock_hash, "dependency_lock_hash")
        _git_sha(
            self.sbom_reviewed_for_release_sha,
            "sbom_reviewed_for_release_sha",
        )
        _git_sha(
            self.provenance_reviewed_for_release_sha,
            "provenance_reviewed_for_release_sha",
        )
        _git_sha(
            self.dependency_lock_reviewed_for_release_sha,
            "dependency_lock_reviewed_for_release_sha",
        )
        if type(self.distributed_component_ids) is not tuple:
            raise TypeError("distributed_component_ids must be a tuple")
        if type(self.sbom_component_ids) is not tuple:
            raise TypeError("sbom_component_ids must be a tuple")
        if type(self.components) is not tuple or any(
            type(item) is not ComponentEvidence for item in self.components
        ):
            raise TypeError(
                "components must be a tuple of ComponentEvidence"
            )
        if type(self.model_data_rights) is not tuple or any(
            type(item) is not ModelDataRightsEvidence
            for item in self.model_data_rights
        ):
            raise TypeError(
                "model_data_rights must be a tuple of ModelDataRightsEvidence"
            )
        if any(
            type(item) is not str or not item.strip()
            for item in self.distributed_component_ids
        ):
            raise ValueError(
                "distributed component inventory contains an invalid id"
            )
        if any(
            type(item) is not str or not item.strip()
            for item in self.sbom_component_ids
        ):
            raise ValueError("SBOM component inventory contains an invalid id")
        if len(self.distributed_component_ids) != len(
            set(self.distributed_component_ids)
        ):
            raise ValueError(
                "distributed component inventory contains duplicates"
            )
        if len(self.sbom_component_ids) != len(set(self.sbom_component_ids)):
            raise ValueError("SBOM component inventory contains duplicates")
        ids = [item.component_id for item in self.components]
        if len(ids) != len(set(ids)):
            raise ValueError("component evidence contains duplicate ids")
        rights_ids = [item.artifact_id for item in self.model_data_rights]
        if len(rights_ids) != len(set(rights_ids)):
            raise ValueError(
                "model/data rights evidence contains duplicate ids"
            )


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must use exact str")
    return value


def _snapshot_component_evidence(
    item: ComponentEvidence,
) -> ComponentEvidence:
    if type(item) is not ComponentEvidence:
        raise TypeError(
            "components must contain exact ComponentEvidence values"
        )
    values = {
        "component_id": object.__getattribute__(item, "component_id"),
        "artifact_id": object.__getattribute__(item, "artifact_id"),
        "version": object.__getattribute__(item, "version"),
        "declared_artifact_hash": object.__getattribute__(
            item, "declared_artifact_hash"
        ),
        "observed_artifact_hash": object.__getattribute__(
            item, "observed_artifact_hash"
        ),
        "source_revision": object.__getattribute__(item, "source_revision"),
        "license_status": object.__getattribute__(item, "license_status"),
        "distribution_rights": object.__getattribute__(
            item, "distribution_rights"
        ),
        "advisory_status": object.__getattribute__(item, "advisory_status"),
        "notice_required": object.__getattribute__(item, "notice_required"),
        "notice_present": object.__getattribute__(item, "notice_present"),
        "reviewed_for_release_sha": object.__getattribute__(
            item, "reviewed_for_release_sha"
        ),
        "advisory_exception_id": object.__getattribute__(
            item, "advisory_exception_id"
        ),
        "advisory_exception_hash": object.__getattribute__(
            item, "advisory_exception_hash"
        ),
    }
    for field in (
        "component_id",
        "artifact_id",
        "version",
        "declared_artifact_hash",
        "observed_artifact_hash",
        "source_revision",
        "license_status",
        "distribution_rights",
        "advisory_status",
        "reviewed_for_release_sha",
    ):
        values[field] = _exact_text(values[field], name=f"component.{field}")
    for field in ("notice_required", "notice_present"):
        if type(values[field]) is not bool:
            raise TypeError(f"component.{field} must use exact bool")
    for field in ("advisory_exception_id", "advisory_exception_hash"):
        if values[field] is not None and type(values[field]) is not str:
            raise TypeError(
                f"component.{field} must be None or exact str"
            )
    return ComponentEvidence(**values)


def _snapshot_model_data_rights_evidence(
    item: ModelDataRightsEvidence,
) -> ModelDataRightsEvidence:
    if type(item) is not ModelDataRightsEvidence:
        raise TypeError(
            "model_data_rights must contain exact ModelDataRightsEvidence values"
        )
    values = {
        "artifact_id": object.__getattribute__(item, "artifact_id"),
        "artifact_hash": object.__getattribute__(item, "artifact_hash"),
        "use_scope": object.__getattribute__(item, "use_scope"),
        "rights_status": object.__getattribute__(item, "rights_status"),
        "reviewed_for_release_sha": object.__getattribute__(
            item, "reviewed_for_release_sha"
        ),
    }
    for field in tuple(values):
        values[field] = _exact_text(
            values[field],
            name=f"model_data_rights.{field}",
        )
    return ModelDataRightsEvidence(**values)


def _snapshot_supply_chain_evidence(
    evidence: SupplyChainEvidence,
) -> SupplyChainEvidence:
    """Detach one complete release evidence graph before trust callbacks."""

    if type(evidence) is not SupplyChainEvidence:
        raise TypeError("evidence must be exact SupplyChainEvidence")
    scalar_fields = (
        "release_commit_sha",
        "built_from_commit_sha",
        "sbom_artifact_id",
        "sbom_hash",
        "provenance_artifact_id",
        "provenance_hash",
        "dependency_lock_artifact_id",
        "dependency_lock_hash",
        "sbom_reviewed_for_release_sha",
        "provenance_reviewed_for_release_sha",
        "dependency_lock_reviewed_for_release_sha",
    )
    values: dict[str, object] = {}
    for field in scalar_fields:
        values[field] = _exact_text(
            object.__getattribute__(evidence, field),
            name=f"supply_chain.{field}",
        )
    for field in ("release_artifact_id", "release_artifact_sha256"):
        current = object.__getattribute__(evidence, field)
        if current is not None and type(current) is not str:
            raise TypeError(
                f"supply_chain.{field} must be None or exact str"
            )
        values[field] = current

    distributed = object.__getattribute__(
        evidence, "distributed_component_ids"
    )
    sbom = object.__getattribute__(evidence, "sbom_component_ids")
    components = object.__getattribute__(evidence, "components")
    rights = object.__getattribute__(evidence, "model_data_rights")
    for current, name in (
        (distributed, "distributed_component_ids"),
        (sbom, "sbom_component_ids"),
    ):
        if type(current) is not tuple or any(
            type(value) is not str for value in current
        ):
            raise TypeError(
                f"supply_chain.{name} must use exact tuple[str]"
            )
    if type(components) is not tuple:
        raise TypeError(
            "supply_chain.components must use exact tuple"
        )
    if type(rights) is not tuple:
        raise TypeError(
            "supply_chain.model_data_rights must use exact tuple"
        )

    values["distributed_component_ids"] = tuple(distributed)
    values["sbom_component_ids"] = tuple(sbom)
    values["components"] = tuple(
        _snapshot_component_evidence(item)
        for item in components
    )
    values["model_data_rights"] = tuple(
        _snapshot_model_data_rights_evidence(item)
        for item in rights
    )
    return SupplyChainEvidence(**values)


def _supply_chain_evidence_payload(
    evidence: SupplyChainEvidence,
) -> dict[str, object]:
    evidence = _snapshot_supply_chain_evidence(evidence)
    return {
        "release_commit_sha": evidence.release_commit_sha,
        "built_from_commit_sha": evidence.built_from_commit_sha,
        "sbom_artifact_id": evidence.sbom_artifact_id,
        "sbom_hash": evidence.sbom_hash,
        "provenance_artifact_id": evidence.provenance_artifact_id,
        "provenance_hash": evidence.provenance_hash,
        "dependency_lock_artifact_id": evidence.dependency_lock_artifact_id,
        "dependency_lock_hash": evidence.dependency_lock_hash,
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
                "notice_required": item.notice_required,
                "notice_present": item.notice_present,
                "reviewed_for_release_sha": item.reviewed_for_release_sha,
                "advisory_exception_id": item.advisory_exception_id,
                "advisory_exception_hash": item.advisory_exception_hash,
            }
            for item in sorted(
                evidence.components,
                key=lambda value: value.component_id,
            )
        ],
        "model_data_rights": [
            {
                "artifact_id": item.artifact_id,
                "artifact_hash": item.artifact_hash,
                "use_scope": item.use_scope,
                "rights_status": item.rights_status,
                "reviewed_for_release_sha": item.reviewed_for_release_sha,
            }
            for item in sorted(
                evidence.model_data_rights,
                key=lambda value: value.artifact_id,
            )
        ],
        "release_artifact_id": evidence.release_artifact_id,
        "release_artifact_sha256": evidence.release_artifact_sha256,
    }


def _parse_supply_chain_evidence_payload(
    payload: object,
) -> SupplyChainEvidence:
    if type(payload) is not dict:
        raise ValueError("supply-chain proof evidence must be an object")
    expected = {
        "release_commit_sha",
        "built_from_commit_sha",
        "sbom_artifact_id",
        "sbom_hash",
        "provenance_artifact_id",
        "provenance_hash",
        "dependency_lock_artifact_id",
        "dependency_lock_hash",
        "sbom_reviewed_for_release_sha",
        "provenance_reviewed_for_release_sha",
        "dependency_lock_reviewed_for_release_sha",
        "distributed_component_ids",
        "sbom_component_ids",
        "components",
        "model_data_rights",
        "release_artifact_id",
        "release_artifact_sha256",
    }
    if set(payload) != expected:
        raise ValueError("supply-chain proof evidence has unsupported structure")
    distributed = payload["distributed_component_ids"]
    sbom_ids = payload["sbom_component_ids"]
    components_raw = payload["components"]
    rights_raw = payload["model_data_rights"]
    if type(distributed) is not list or type(sbom_ids) is not list:
        raise ValueError("supply-chain proof inventories must be lists")
    if type(components_raw) is not list or type(rights_raw) is not list:
        raise ValueError("supply-chain proof evidence collections must be lists")
    component_fields = {
        "component_id",
        "artifact_id",
        "version",
        "declared_artifact_hash",
        "observed_artifact_hash",
        "source_revision",
        "license_status",
        "distribution_rights",
        "advisory_status",
        "notice_required",
        "notice_present",
        "reviewed_for_release_sha",
        "advisory_exception_id",
        "advisory_exception_hash",
    }
    rights_fields = {
        "artifact_id",
        "artifact_hash",
        "use_scope",
        "rights_status",
        "reviewed_for_release_sha",
    }
    components = []
    for raw in components_raw:
        if type(raw) is not dict or set(raw) != component_fields:
            raise ValueError(
                "supply-chain proof component has unsupported structure"
            )
        components.append(ComponentEvidence(**raw))
    rights = []
    for raw in rights_raw:
        if type(raw) is not dict or set(raw) != rights_fields:
            raise ValueError(
                "supply-chain proof rights evidence has unsupported structure"
            )
        rights.append(ModelDataRightsEvidence(**raw))
    return SupplyChainEvidence(
        release_commit_sha=payload["release_commit_sha"],
        built_from_commit_sha=payload["built_from_commit_sha"],
        sbom_artifact_id=payload["sbom_artifact_id"],
        sbom_hash=payload["sbom_hash"],
        provenance_artifact_id=payload["provenance_artifact_id"],
        provenance_hash=payload["provenance_hash"],
        dependency_lock_artifact_id=payload["dependency_lock_artifact_id"],
        dependency_lock_hash=payload["dependency_lock_hash"],
        sbom_reviewed_for_release_sha=payload["sbom_reviewed_for_release_sha"],
        provenance_reviewed_for_release_sha=payload[
            "provenance_reviewed_for_release_sha"
        ],
        dependency_lock_reviewed_for_release_sha=payload[
            "dependency_lock_reviewed_for_release_sha"
        ],
        distributed_component_ids=tuple(distributed),
        sbom_component_ids=tuple(sbom_ids),
        components=tuple(components),
        model_data_rights=tuple(rights),
        release_artifact_id=payload["release_artifact_id"],
        release_artifact_sha256=payload["release_artifact_sha256"],
    )


def canonical_supply_chain_proof_bytes(
    evidence: SupplyChainEvidence,
    receipt: SignedQualificationAttestation,
) -> bytes:
    """Serialize exact WP-64 evidence + signed receipt as durable proof bytes."""

    evidence = _snapshot_supply_chain_evidence(evidence)
    if type(receipt) is not SignedQualificationAttestation:
        raise TypeError("receipt must be exact SignedQualificationAttestation")
    canonical_receipt = parse_signed_qualification_attestation(
        {
            "attestation": receipt.attestation.canonical_payload(),
            "signature_b64": receipt.signature_b64,
        }
    )
    payload = {
        "schema_version": _SUPPLY_CHAIN_PROOF_SCHEMA_VERSION,
        "evidence": _supply_chain_evidence_payload(evidence),
        "receipt": {
            "attestation": canonical_receipt.attestation.canonical_payload(),
            "signature_b64": canonical_receipt.signature_b64,
        },
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def parse_supply_chain_proof_bytes(
    raw: bytes,
) -> tuple[SupplyChainEvidence, SignedQualificationAttestation]:
    """Parse only canonical, resource-bounded durable WP-64 proof bytes."""

    if type(raw) is not bytes or not raw:
        raise ValueError("supply-chain proof bytes are required")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("supply-chain proof is not valid UTF-8") from error
    try:
        payload = strict_json_loads(text)
    except ValueError as error:
        raise ValueError(f"supply-chain proof JSON rejected: {error}") from error
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "evidence",
        "receipt",
    }:
        raise ValueError("supply-chain proof has unsupported structure")
    if payload["schema_version"] != _SUPPLY_CHAIN_PROOF_SCHEMA_VERSION:
        raise ValueError("supply-chain proof schema version is unsupported")
    evidence = _parse_supply_chain_evidence_payload(payload["evidence"])
    receipt = parse_signed_qualification_attestation(payload["receipt"])
    canonical = canonical_supply_chain_proof_bytes(evidence, receipt)
    if canonical != raw:
        raise ValueError("supply-chain proof is not canonical")
    return evidence, receipt


def supply_chain_subject_requirement(evidence: SupplyChainEvidence) -> str:
    """Bind signed WP-64 review authority to the exact detached semantic subject."""

    if type(evidence) is not SupplyChainEvidence:
        raise TypeError("evidence must be exact SupplyChainEvidence")
    subject = {
        "release_commit_sha": evidence.release_commit_sha,
        "built_from_commit_sha": evidence.built_from_commit_sha,
        "release_artifact": (
            None
            if evidence.release_artifact_id is None
            else {
                "artifact_id": evidence.release_artifact_id,
                "sha256": evidence.release_artifact_sha256,
            }
        ),
        "sbom": {
            "artifact_id": evidence.sbom_artifact_id,
            "sha256": evidence.sbom_hash,
            "reviewed_for_release_sha": evidence.sbom_reviewed_for_release_sha,
        },
        "provenance": {
            "artifact_id": evidence.provenance_artifact_id,
            "sha256": evidence.provenance_hash,
            "reviewed_for_release_sha": evidence.provenance_reviewed_for_release_sha,
        },
        "dependency_lock": {
            "artifact_id": evidence.dependency_lock_artifact_id,
            "sha256": evidence.dependency_lock_hash,
            "reviewed_for_release_sha": evidence.dependency_lock_reviewed_for_release_sha,
        },
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
            for item in sorted(
                evidence.components,
                key=lambda value: value.component_id,
            )
        ],
        "model_data_rights": [
            {
                "artifact_id": item.artifact_id,
                "artifact_hash": item.artifact_hash,
                "use_scope": item.use_scope,
                "rights_status": item.rights_status,
                "reviewed_for_release_sha": item.reviewed_for_release_sha,
            }
            for item in sorted(
                evidence.model_data_rights,
                key=lambda value: value.artifact_id,
            )
        ],
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
class SupplyChainQualification:
    qualification_id: str
    status: str
    checks: tuple[tuple[str, str], ...]
    reason_codes: tuple[str, ...]
    release_authority: bool = False
    accepted_attestation_id: str | None = None
    accepted_attestation_digest: str | None = None
    accepted_policy_id: str | None = None
    accepted_trust_root_id: str | None = None
    subject_requirement: str | None = None
    release_artifact_id: str | None = None
    release_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.qualification_id, "qualification_id")
        if type(self.status) is not str or self.status not in _ALLOWED_QUALIFICATION_STATUSES:
            raise ValueError("status must be PASS, FAIL, or INCONCLUSIVE")
        if type(self.checks) is not tuple:
            raise TypeError("checks must be a tuple")
        for item in self.checks:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError("checks must contain exact two-item tuples")
            name, status = item
            _required_text(name, "check name")
            if type(status) is not str or status not in _ALLOWED_QUALIFICATION_STATUSES:
                raise ValueError("check status must be PASS, FAIL, or INCONCLUSIVE")
        if type(self.reason_codes) is not tuple:
            raise TypeError("reason_codes must be a tuple")
        for reason in self.reason_codes:
            _required_text(reason, "reason_code")
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must be unique")
        if type(self.release_authority) is not bool:
            raise TypeError("release_authority must be boolean")
        if self.release_authority:
            raise ValueError(
                "supply-chain qualification never grants release authority"
            )
        for field in (
            "accepted_attestation_id",
            "accepted_attestation_digest",
            "accepted_policy_id",
            "accepted_trust_root_id",
            "subject_requirement",
            "release_artifact_id",
            "release_artifact_sha256",
        ):
            value = object.__getattribute__(self, field)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field} must be None or exact str")


def _store_artifact_matches(
    read_snapshot,
    *,
    artifact_id: str,
    artifact_hash: str,
    media_type: str,
    release_sha: str,
    metadata: dict[str, object],
) -> bool:
    """Verify exact immutable bytes and declared bindings through ArtifactStore.

    ArtifactStore is an integrity boundary, not an independent trust anchor: the
    caller that opens a store may also have populated it. Producer/authenticator
    trust is therefore evaluated separately and must remain fail-closed until a
    qualified attestation boundary exists.
    """

    try:
        manifest, raw = read_snapshot(artifact_id)
        if type(manifest) is not dict or type(raw) is not bytes:
            return False
        if type(manifest.get("manifest_hash")) is not str:
            return False
        if manifest.get("sha256") != artifact_hash:
            return False
        if manifest.get("media_type") != media_type:
            return False
        if manifest.get("source_refs") != [f"git:{release_sha}"]:
            return False
        if type(manifest.get("metadata")) is not dict:
            return False
        if manifest.get("metadata") != metadata:
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


def qualify_supply_chain(
    evidence: SupplyChainEvidence,
    *,
    evidence_store: ArtifactStore | None = None,
    evidence_root: str | Path | None = None,
    trust_receipt: SignedQualificationAttestation | None = None,
) -> SupplyChainQualification:
    evidence = _snapshot_supply_chain_evidence(evidence)
    subject_requirement = supply_chain_subject_requirement(evidence)
    if evidence_store is not None and type(evidence_store) is not ArtifactStore:
        raise TypeError(
            "evidence_store must be ArtifactStore (canonical exact type required)"
        )
    if (
        trust_receipt is not None
        and type(trust_receipt) is not SignedQualificationAttestation
    ):
        raise TypeError(
            "trust_receipt must be SignedQualificationAttestation"
        )
    checks: list[tuple[str, str]] = []
    reasons: list[str] = []
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

    def record(
        name: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        checks.append((name, status))
        if reason and status != _PASS:
            reasons.append(reason)

    immutable_checks: list[tuple[str, bool]] = []
    if trusted_read is not None:
        immutable_checks.extend(
            (
                (
                    "sbom",
                    _store_artifact_matches(
                        trusted_read,
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
                        trusted_read,
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
                        trusted_read,
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
        for item in sorted(
            evidence.components,
            key=lambda value: value.component_id,
        ):
            immutable_checks.append(
                (
                    "component:" + item.component_id,
                    _store_artifact_matches(
                        trusted_read,
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
                            trusted_read,
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
        for item in sorted(
            evidence.model_data_rights,
            key=lambda value: value.artifact_id,
        ):
            immutable_checks.append(
                (
                    "rights:" + item.artifact_id,
                    _store_artifact_matches(
                        trusted_read,
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
            (
                _PASS
                if immutable_checks
                and all(value for _, value in immutable_checks)
                else _INCONCLUSIVE
            ),
            "SUPPLY_CHAIN.EVIDENCE_UNVERIFIED",
        )

    # Content-addressed storage proves immutable bytes, not independent authorship.
    # Reuse the canonical signed qualification-attestation boundary for terminal
    # WP-64 trust rather than introducing a second signature or reviewer authority.
    accepted_trust = None
    if trust_receipt is None:
        record(
            "independent_evidence_trust",
            _INCONCLUSIVE,
            "SUPPLY_CHAIN.TRUST_ANCHOR_UNAVAILABLE",
        )
    elif evidence_store is None or evidence_root is None:
        record(
            "independent_evidence_trust",
            _INCONCLUSIVE,
            "SUPPLY_CHAIN.TRUST_EVIDENCE_ROOT_INCOMPLETE",
        )
    else:
        try:
            accepted_trust = verify_canonical_qualification_attestation(
                trust_receipt,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
                expected_source_sha=evidence.release_commit_sha,
                expected_domain="SUPPLY_CHAIN",
                expected_gate="RELEASE",
                expected_package_id="WP-64",
                expected_protocol_id="supply-chain-review-v1",
                expected_protocol_version="1.0.0",
                expected_requirement_id="independent-supply-chain-review",
                expected_release_artifact_id=evidence.release_artifact_id,
                expected_release_artifact_sha256=evidence.release_artifact_sha256,
            )
            review_identity = (
                accepted_trust.attestation_id,
                accepted_trust.attestation_digest,
                accepted_trust.policy_id,
                accepted_trust.trust_root_id,
            )
            subject_identity = None
            if subject_requirement in accepted_trust.requirement_ids:
                accepted_subject = verify_canonical_qualification_attestation(
                    trust_receipt,
                    evidence_store=evidence_store,
                    evidence_root=evidence_root,
                    expected_source_sha=evidence.release_commit_sha,
                    expected_domain="SUPPLY_CHAIN",
                    expected_gate="RELEASE",
                    expected_package_id="WP-64",
                    expected_protocol_id="supply-chain-review-v1",
                    expected_protocol_version="1.0.0",
                    expected_requirement_id=subject_requirement,
                    expected_release_artifact_id=evidence.release_artifact_id,
                    expected_release_artifact_sha256=evidence.release_artifact_sha256,
                )
                subject_identity = (
                    accepted_subject.attestation_id,
                    accepted_subject.attestation_digest,
                    accepted_subject.policy_id,
                    accepted_subject.trust_root_id,
                )

            expected_refs = {
                (
                    evidence.sbom_artifact_id,
                    evidence.sbom_hash,
                    _SBOM_MEDIA_TYPE,
                    "SBOM",
                ),
                (
                    evidence.provenance_artifact_id,
                    evidence.provenance_hash,
                    _PROVENANCE_MEDIA_TYPE,
                    "PROVENANCE",
                ),
                (
                    evidence.dependency_lock_artifact_id,
                    evidence.dependency_lock_hash,
                    _DEPENDENCY_LOCK_MEDIA_TYPE,
                    "DEPENDENCY_LOCK",
                ),
            }
            for item in evidence.components:
                expected_refs.add(
                    (
                        item.artifact_id,
                        item.observed_artifact_hash,
                        _COMPONENT_MEDIA_TYPE,
                        "DISTRIBUTED_COMPONENT",
                    )
                )
                if item.advisory_status == "ALLOWLISTED":
                    assert item.advisory_exception_id is not None
                    assert item.advisory_exception_hash is not None
                    expected_refs.add(
                        (
                            item.advisory_exception_id,
                            item.advisory_exception_hash,
                            _ADVISORY_EXCEPTION_MEDIA_TYPE,
                            "ADVISORY_EXCEPTION",
                        )
                    )
            for item in evidence.model_data_rights:
                expected_refs.add(
                    (
                        item.artifact_id,
                        item.artifact_hash,
                        _RIGHTS_MEDIA_TYPE,
                        "MODEL_DATA_RIGHTS",
                    )
                )

            attested_refs = {
                (
                    ref.artifact_id,
                    ref.sha256,
                    ref.media_type,
                    ref.evidence_kind,
                )
                for ref in accepted_trust.evidence_refs
            }
            if subject_identity is None or review_identity != subject_identity:
                record(
                    "independent_evidence_trust",
                    _FAIL,
                    "SUPPLY_CHAIN.TRUST_SUBJECT_MISMATCH",
                )
            elif attested_refs != expected_refs:
                record(
                    "independent_evidence_trust",
                    _FAIL,
                    "SUPPLY_CHAIN.TRUST_EVIDENCE_SET_MISMATCH",
                )
            elif accepted_trust.result == _PASS:
                record("independent_evidence_trust", _PASS)
            elif accepted_trust.result == _FAIL:
                record(
                    "independent_evidence_trust",
                    _FAIL,
                    "SUPPLY_CHAIN.INDEPENDENT_REVIEW_FAILED",
                )
            else:
                record(
                    "independent_evidence_trust",
                    _INCONCLUSIVE,
                    "SUPPLY_CHAIN.INDEPENDENT_REVIEW_INCONCLUSIVE",
                )
        except QualificationTrustUnavailable:
            record(
                "independent_evidence_trust",
                _INCONCLUSIVE,
                "SUPPLY_CHAIN.TRUST_ANCHOR_UNAVAILABLE",
            )
        except (QualificationTrustError, TypeError, ValueError):
            record(
                "independent_evidence_trust",
                _FAIL,
                "SUPPLY_CHAIN.TRUST_ATTESTATION_INVALID",
            )

    exact_head = evidence.release_commit_sha == evidence.built_from_commit_sha
    record(
        "exact_release_head",
        _PASS if exact_head else _FAIL,
        "SUPPLY_CHAIN.BUILD_SHA_MISMATCH",
    )

    for name, reviewed_sha in (
        ("sbom", evidence.sbom_reviewed_for_release_sha),
        ("provenance", evidence.provenance_reviewed_for_release_sha),
        (
            "dependency_lock",
            evidence.dependency_lock_reviewed_for_release_sha,
        ),
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
        (
            _PASS
            if sbom_inventory == inventory and component_inventory == inventory
            else _FAIL
        ),
        "SUPPLY_CHAIN.SBOM_INVENTORY_MISMATCH",
    )

    for component_id in sorted(inventory & set(by_id)):
        item = by_id[component_id]
        prefix = "component:" + component_id
        record(
            prefix + ":artifact_hash",
            (
                _PASS
                if item.declared_artifact_hash == item.observed_artifact_hash
                else _FAIL
            ),
            "SUPPLY_CHAIN.ARTIFACT_HASH_MISMATCH:" + component_id,
        )
        release_bound = (
            item.reviewed_for_release_sha == evidence.release_commit_sha
        )
        record(
            prefix + ":release_binding",
            _PASS if release_bound else _FAIL,
            "SUPPLY_CHAIN.STALE_REVIEW:" + component_id,
        )

        license_state = (
            _PASS
            if item.license_status == "APPROVED"
            else (_FAIL if item.license_status == "BLOCKED" else _INCONCLUSIVE)
        )
        record(
            prefix + ":license",
            license_state,
            "SUPPLY_CHAIN.LICENSE_"
            + item.license_status
            + ":"
            + component_id,
        )
        rights_state = (
            _PASS
            if item.distribution_rights == "APPROVED"
            else (
                _FAIL
                if item.distribution_rights == "BLOCKED"
                else _INCONCLUSIVE
            )
        )
        record(
            prefix + ":distribution_rights",
            rights_state,
            "SUPPLY_CHAIN.RIGHTS_"
            + item.distribution_rights
            + ":"
            + component_id,
        )
        advisory_state = (
            _PASS
            if item.advisory_status in {"CLEAR", "ALLOWLISTED"}
            else (
                _FAIL
                if item.advisory_status == "BLOCKED"
                else _INCONCLUSIVE
            )
        )
        record(
            prefix + ":advisory",
            advisory_state,
            "SUPPLY_CHAIN.ADVISORY_"
            + item.advisory_status
            + ":"
            + component_id,
        )
        notice_ok = (not item.notice_required) or item.notice_present
        record(
            prefix + ":notice",
            _PASS if notice_ok else _FAIL,
            "SUPPLY_CHAIN.MISSING_NOTICE:" + component_id,
        )

    if not evidence.model_data_rights:
        record(
            "model_data_rights",
            _INCONCLUSIVE,
            "SUPPLY_CHAIN.MODEL_DATA_RIGHTS_MISSING",
        )
    for item in sorted(
        evidence.model_data_rights,
        key=lambda value: value.artifact_id,
    ):
        prefix = "rights:" + item.artifact_id
        bound = item.reviewed_for_release_sha == evidence.release_commit_sha
        record(
            prefix + ":release_binding",
            _PASS if bound else _FAIL,
            "SUPPLY_CHAIN.STALE_RIGHTS_REVIEW:" + item.artifact_id,
        )
        state = (
            _PASS
            if item.rights_status == "APPROVED"
            else (_FAIL if item.rights_status == "BLOCKED" else _INCONCLUSIVE)
        )
        record(
            prefix + ":rights",
            state,
            "SUPPLY_CHAIN.MODEL_DATA_RIGHTS_"
            + item.rights_status
            + ":"
            + item.artifact_id,
        )

    has_fail = any(status == _FAIL for _, status in checks)
    has_inconclusive = any(
        status == _INCONCLUSIVE for _, status in checks
    )
    status = (
        _FAIL
        if has_fail
        else (_INCONCLUSIVE if has_inconclusive else _PASS)
    )
    canonical = json.dumps(
        {
            "release": evidence.release_commit_sha,
            "built_from": evidence.built_from_commit_sha,
            "release_artifact_id": evidence.release_artifact_id,
            "release_artifact_sha256": evidence.release_artifact_sha256,
            "sbom_artifact_id": evidence.sbom_artifact_id,
            "sbom": evidence.sbom_hash,
            "provenance_artifact_id": evidence.provenance_artifact_id,
            "provenance": evidence.provenance_hash,
            "dependency_lock_artifact_id": evidence.dependency_lock_artifact_id,
            "lock": evidence.dependency_lock_hash,
            "sbom_reviewed_for_release_sha": (
                evidence.sbom_reviewed_for_release_sha
            ),
            "provenance_reviewed_for_release_sha": (
                evidence.provenance_reviewed_for_release_sha
            ),
            "dependency_lock_reviewed_for_release_sha": (
                evidence.dependency_lock_reviewed_for_release_sha
            ),
            "distributed_component_ids": sorted(
                evidence.distributed_component_ids
            ),
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
                for item in sorted(
                    evidence.components,
                    key=lambda value: value.component_id,
                )
            ],
            "model_data_rights": [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_hash": item.artifact_hash,
                    "use_scope": item.use_scope,
                    "rights_status": item.rights_status,
                    "reviewed_for_release_sha": item.reviewed_for_release_sha,
                }
                for item in sorted(
                    evidence.model_data_rights,
                    key=lambda value: value.artifact_id,
                )
            ],
            "checks": checks,
            "reasons": sorted(set(reasons)),
            "trust": (
                None
                if trust_receipt is None
                else {
                    "attestation_digest": (
                        accepted_trust.attestation_digest
                        if accepted_trust is not None
                        else None
                    ),
                    "signature_sha256": (
                        "sha256:"
                        + sha256(
                            accepted_trust.signature_b64.encode("ascii")
                        ).hexdigest()
                        if accepted_trust is not None
                        else None
                    ),
                    "policy_id": (
                        accepted_trust.policy_id
                        if accepted_trust is not None
                        else None
                    ),
                    "policy_version": (
                        accepted_trust.policy_version
                        if accepted_trust is not None
                        else None
                    ),
                    "accepted_attestation_id": (
                        accepted_trust.attestation_id
                        if accepted_trust is not None
                        else None
                    ),
                }
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    accepted_identity = (
        None
        if accepted_trust is None
        else (
            accepted_trust.attestation_id,
            accepted_trust.attestation_digest,
            accepted_trust.policy_id,
            accepted_trust.trust_root_id,
        )
    )
    return SupplyChainQualification(
        "supply-" + sha256(canonical.encode("utf-8")).hexdigest()[:32],
        status,
        tuple(checks),
        tuple(dict.fromkeys(reasons)),
        False,
        None if accepted_identity is None else accepted_identity[0],
        None if accepted_identity is None else accepted_identity[1],
        None if accepted_identity is None else accepted_identity[2],
        None if accepted_identity is None else accepted_identity[3],
        subject_requirement if accepted_identity is not None else None,
        evidence.release_artifact_id,
        evidence.release_artifact_sha256,
    )