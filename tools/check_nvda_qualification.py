"""Validate exact-build real NVDA qualification evidence for AutoTrade."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvp.autotrade_mvp.qualification_attestation import (
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    load_canonical_nvda_requirements_bytes,
    parse_signed_qualification_attestation,
    verify_canonical_qualification_attestation,
)
from autotrade_runtime.artifacts import ArtifactStore

DEFAULT_REQUIREMENTS = ROOT / "qualification" / "nvda" / "requirements.json"
DEFAULT_STATUS = ROOT / "qualification" / "nvda" / "status.json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
NVDA_DOMAIN = "ACCESSIBILITY"
NVDA_GATE = "NVDA_RELEASE"
NVDA_PACKAGE_ID = "WP-53"
NVDA_PROTOCOL_ID = "real-nvda-keyboard-v1"
NVDA_PROTOCOL_VERSION = "1.0.0"
NVDA_EVIDENCE_KIND = "NVDA_REAL_RUN"
NVDA_REQUIREMENTS_REQUIREMENT_PREFIX = "nvda-requirements/sha256:"
SIGNED_ATTESTATION_REQUIRED = "SIGNED_QUALIFICATION_ATTESTATION_REQUIRED"
_MAX_RELEASE_BUNDLE_BYTES = 1024 * 1024 * 1024
_MAX_RELEASE_MANIFEST_BYTES = 1024 * 1024


class NvdaQualificationError(ValueError):
    pass


def _read_file_bytes(path: Path, *, name: str) -> bytes:
    """Read one immutable process-local snapshot of an authority input."""
    try:
        if not path.is_file():
            raise NvdaQualificationError(f"{name} must be a readable file")
        payload = path.read_bytes()
    except OSError as error:
        raise NvdaQualificationError(f"{name} cannot be read") from error
    return payload


def _parse_json_snapshot(payload: bytes, *, name: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NvdaQualificationError(f"{name} is missing or invalid") from error
    if not isinstance(value, dict):
        raise NvdaQualificationError(f"{name} must be an object")
    return value


def _load_json_snapshot(
    path: Path,
    *,
    name: str,
) -> tuple[dict[str, object], bytes, str]:
    payload = _read_file_bytes(path, name=name)
    value = _parse_json_snapshot(payload, name=name)
    return value, payload, "sha256:" + sha256(payload).hexdigest()


def _load(path: Path, *, name: str) -> dict[str, object]:
    value, _, _ = _load_json_snapshot(path, name=name)
    return value


def _canonical_nvda_requirements(
    *,
    source_sha: str,
    supplied_requirements: dict[str, object],
) -> tuple[dict[str, object], str, str]:
    """Resolve the full physical protocol from exact source, never caller bytes."""

    try:
        raw = load_canonical_nvda_requirements_bytes(
            expected_source_sha=source_sha,
        )
    except (QualificationTrustError, TypeError, ValueError) as error:
        raise NvdaQualificationError(
            "canonical NVDA requirements are unavailable for exact source"
        ) from error
    canonical = _parse_json_snapshot(raw, name="canonical NVDA requirements")
    if canonical != supplied_requirements:
        raise NvdaQualificationError(
            "supplied NVDA requirements do not match exact-source canonical requirements"
        )
    digest = "sha256:" + sha256(raw).hexdigest()
    return (
        canonical,
        digest,
        NVDA_REQUIREMENTS_REQUIREMENT_PREFIX + digest.removeprefix("sha256:"),
    )


def _signed_nvda_requirement_ids(
    canonical_requirements: dict[str, object],
    requirements_requirement_id: str,
) -> tuple[str, ...]:
    workflows = canonical_requirements.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        raise NvdaQualificationError("requirements contain no workflows")
    if any(type(item) is not dict for item in workflows):
        raise NvdaQualificationError("requirements.workflow must be an object")
    workflow_ids = tuple(
        sorted(
            _required_text(item.get("id"), name="requirements.workflow.id")
            for item in workflows
        )
    )
    if len(workflow_ids) != len(set(workflow_ids)):
        raise NvdaQualificationError("requirements.workflow ids must be unique")
    requirement_ids = tuple(
        sorted((*workflow_ids, requirements_requirement_id))
    )
    if len(requirement_ids) != len(set(requirement_ids)):
        raise NvdaQualificationError("NVDA signed requirement ids must be unique")
    return requirement_ids


def canonical_nvda_requirement_ids(
    *,
    source_sha: str,
    supplied_requirements: dict[str, object],
) -> tuple[str, ...]:
    """Return the one exact-source signed requirement set used by terminal consumers."""

    canonical, _digest, requirements_requirement_id = (
        _canonical_nvda_requirements(
            source_sha=source_sha,
            supplied_requirements=supplied_requirements,
        )
    )
    return _signed_nvda_requirement_ids(
        canonical,
        requirements_requirement_id,
    )


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NvdaQualificationError(f"{name} is required")
    return value.strip()


def _workflow_requirement_digest(workflow: dict[str, object]) -> str:
    workflow_id = _required_text(workflow.get("id"), name="requirements.workflow.id")
    description = _required_text(
        workflow.get("description"),
        name=f"requirements.workflow[{workflow_id}].description",
    )
    payload = json.dumps(
        {"description": description, "id": workflow_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def validate_evidence(
    evidence: dict[str, object],
    requirements: dict[str, object],
) -> dict[str, object]:
    if requirements.get("schema_version") != "1.0.0":
        raise NvdaQualificationError("unsupported NVDA requirements schema_version")
    if evidence.get("schema_version") != requirements["schema_version"]:
        raise NvdaQualificationError("evidence schema_version does not match requirements")
    source_sha = _required_text(evidence.get("source_sha"), name="source_sha")
    artifact_sha = _required_text(
        evidence.get("artifact_sha256"),
        name="artifact_sha256",
    )
    if GIT_SHA.fullmatch(source_sha) is None:
        raise NvdaQualificationError("source_sha must be an exact 40-character Git SHA")
    if SHA256.fullmatch(artifact_sha) is None:
        raise NvdaQualificationError("artifact_sha256 must be sha256:<64 lowercase hex>")
    release_artifact_id = _required_text(
        evidence.get("release_artifact_id"),
        name="release_artifact_id",
    )
    if UUID_TEXT.fullmatch(release_artifact_id) is None:
        raise NvdaQualificationError(
            "release_artifact_id must be a canonical lowercase UUID"
        )
    if evidence.get("method") != requirements.get("evidence_method"):
        raise NvdaQualificationError("evidence method is not real NVDA keyboard qualification")

    environment = evidence.get("environment")
    if not isinstance(environment, dict):
        raise NvdaQualificationError("environment must be an object")
    for field in ("windows_version", "assistive_technology", "nvda_version", "input_mode"):
        _required_text(environment.get(field), name=f"environment.{field}")
    required_environment = requirements.get("required_environment")
    if not isinstance(required_environment, dict):
        raise NvdaQualificationError("requirements.required_environment must be an object")
    required_os = _required_text(
        required_environment.get("os_family"), name="requirements.os_family"
    )
    required_at = _required_text(
        required_environment.get("assistive_technology"),
        name="requirements.assistive_technology",
    )
    required_input = _required_text(
        required_environment.get("input_mode"),
        name="requirements.input_mode",
    )
    if environment.get("input_mode") != required_input:
        raise NvdaQualificationError(f"qualification must be {required_input}")
    windows_version = _required_text(
        environment.get("windows_version"),
        name="environment.windows_version",
    )
    if windows_version != required_os and not windows_version.startswith(required_os + " "):
        raise NvdaQualificationError(f"qualification must run on {required_os}")
    if environment.get("assistive_technology") != required_at:
        raise NvdaQualificationError(
            f"qualification must use required assistive technology: {required_at}"
        )
    if evidence.get("release_artifact") is not True:
        raise NvdaQualificationError(
            "qualification must run against the delivered release artifact"
        )

    observations = evidence.get("workflows")
    if not isinstance(observations, list):
        raise NvdaQualificationError("workflows must be an array")
    by_id: dict[str, dict[str, object]] = {}
    evidence_refs: set[str] = set()
    for item in observations:
        if not isinstance(item, dict):
            raise NvdaQualificationError("workflow evidence must be an object")
        workflow_id = _required_text(item.get("id"), name="workflow.id")
        if workflow_id in by_id:
            raise NvdaQualificationError(f"duplicate workflow evidence: {workflow_id}")
        if item.get("passed") is not True:
            raise NvdaQualificationError(f"workflow did not pass: {workflow_id}")
        _required_text(item.get("keyboard_steps"), name=f"{workflow_id}.keyboard_steps")
        _required_text(item.get("nvda_observation"), name=f"{workflow_id}.nvda_observation")
        requirement_sha = _required_text(
            item.get("requirement_sha256"),
            name=f"{workflow_id}.requirement_sha256",
        )
        if SHA256.fullmatch(requirement_sha) is None:
            raise NvdaQualificationError(
                f"{workflow_id}.requirement_sha256 must be an immutable sha256 digest"
            )
        evidence_ref = _required_text(
            item.get("evidence_ref"), name=f"{workflow_id}.evidence_ref"
        )
        if SHA256.fullmatch(evidence_ref) is None:
            raise NvdaQualificationError(
                f"{workflow_id}.evidence_ref must be an immutable sha256 digest"
            )
        if evidence_ref in evidence_refs:
            raise NvdaQualificationError("workflow evidence references must be unique")
        evidence_refs.add(evidence_ref)
        by_id[workflow_id] = item

    required = requirements.get("workflows")
    if not isinstance(required, list) or not required:
        raise NvdaQualificationError("requirements contain no workflows")
    required_id_list: list[str] = []
    requirement_digests: dict[str, str] = {}
    for item in required:
        if not isinstance(item, dict):
            raise NvdaQualificationError("requirements.workflow must be an object")
        workflow_id = _required_text(item.get("id"), name="requirements.workflow.id")
        required_id_list.append(workflow_id)
        requirement_digests[workflow_id] = _workflow_requirement_digest(item)
    if len(required_id_list) != len(set(required_id_list)):
        raise NvdaQualificationError("requirements.workflow ids must be unique")
    required_ids = set(required_id_list)
    if set(by_id) != required_ids:
        missing = sorted(required_ids - set(by_id))
        extra = sorted(set(by_id) - required_ids)
        raise NvdaQualificationError(
            f"workflow evidence mismatch; missing={missing}; extra={extra}"
        )
    for workflow_id in required_id_list:
        if by_id[workflow_id].get("requirement_sha256") != requirement_digests[workflow_id]:
            raise NvdaQualificationError(
                f"workflow evidence is stale for requirement: {workflow_id}"
            )

    reviewer = _required_text(evidence.get("reviewer"), name="reviewer")
    observed_at = _required_text(evidence.get("observed_at"), name="observed_at")
    try:
        observed_instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise NvdaQualificationError("observed_at must be an ISO timestamp") from error
    if observed_instant.tzinfo is None:
        raise NvdaQualificationError("observed_at must include timezone")
    observed_at = (
        observed_instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return {
        "schema_version": "1.0.0",
        "evidence_complete": True,
        "qualified": False,
        "reason": SIGNED_ATTESTATION_REQUIRED,
        "source_sha": source_sha,
        "artifact_sha256": artifact_sha,
        "release_artifact_id": release_artifact_id,
        "windows_version": environment["windows_version"],
        "nvda_version": environment["nvda_version"],
        "workflow_count": len(required_ids),
        "reviewer": reviewer,
        "observed_at": observed_at,
    }


def evidence_digest(path: Path) -> str:
    payload = _read_file_bytes(path, name="evidence file")
    return "sha256:" + sha256(payload).hexdigest()


def release_artifact_digest(path: Path) -> str:
    payload = _read_file_bytes(path, name="release artifact")
    return "sha256:" + sha256(payload).hexdigest()


def _release_bundle_source_sha_from_bytes(payload: bytes) -> str:
    if len(payload) > _MAX_RELEASE_BUNDLE_BYTES:
        raise NvdaQualificationError("release artifact is unreasonably large")
    try:
        with zipfile.ZipFile(BytesIO(payload), "r") as archive:
            manifest_names = [
                name for name in archive.namelist() if name == "bundle-manifest.json"
            ]
            if len(manifest_names) != 1:
                raise NvdaQualificationError(
                    "release artifact must contain exactly one bundle-manifest.json"
                )
            info = archive.getinfo("bundle-manifest.json")
            if info.file_size > _MAX_RELEASE_MANIFEST_BYTES:
                raise NvdaQualificationError(
                    "release bundle manifest is unreasonably large"
                )
            try:
                manifest_payload = archive.read(info)
                manifest = json.loads(manifest_payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise NvdaQualificationError(
                    "release bundle manifest is invalid"
                ) from error
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise NvdaQualificationError(
            "release artifact must be a readable AutoTrade release bundle"
        ) from error

    if not isinstance(manifest, dict):
        raise NvdaQualificationError("release bundle manifest must be an object")
    if manifest.get("product") != "AutoTrade":
        raise NvdaQualificationError("release bundle product must be AutoTrade")
    if manifest.get("mode") != "release":
        raise NvdaQualificationError(
            "NVDA qualification requires a release-mode bundle"
        )
    if manifest.get("release_eligible") is not True:
        raise NvdaQualificationError(
            "NVDA qualification requires a release-eligible bundle"
        )
    source_sha = _required_text(
        manifest.get("source_sha"),
        name="bundle-manifest.source_sha",
    )
    if GIT_SHA.fullmatch(source_sha) is None:
        raise NvdaQualificationError(
            "bundle-manifest.source_sha must be an exact 40-character lowercase Git SHA"
        )
    return source_sha


def _release_bundle_source_sha(release_artifact: Path) -> str:
    payload = _read_file_bytes(release_artifact, name="release artifact")
    return _release_bundle_source_sha_from_bytes(payload)


def validate_release_artifact_binding(
    evidence: dict[str, object],
    release_artifact: Path,
) -> str:
    declared = _required_text(
        evidence.get("artifact_sha256"),
        name="artifact_sha256",
    )
    if SHA256.fullmatch(declared) is None:
        raise NvdaQualificationError(
            "artifact_sha256 must be canonical sha256:<64 lowercase hex>"
        )
    payload = _read_file_bytes(release_artifact, name="release artifact")
    actual = "sha256:" + sha256(payload).hexdigest()
    if declared != actual:
        raise NvdaQualificationError(
            "release artifact SHA-256 does not match NVDA evidence"
        )
    artifact_source_sha = _release_bundle_source_sha_from_bytes(payload)
    evidence_source_sha = _required_text(
        evidence.get("source_sha"),
        name="source_sha",
    )
    if GIT_SHA.fullmatch(evidence_source_sha) is None:
        raise NvdaQualificationError(
            "source_sha must be an exact 40-character lowercase Git SHA"
        )
    if artifact_source_sha != evidence_source_sha:
        raise NvdaQualificationError(
            "release bundle source SHA does not match NVDA evidence"
        )
    return actual


def validate_trusted_nvda_qualification(
    evidence: dict[str, object],
    requirements: dict[str, object],
    *,
    evidence_sha256: str,
    release_artifact_sha256: str,
    receipt: SignedQualificationAttestation,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
    policy: QualificationTrustPolicy | None = None,
    expected_policy_id: str | None = None,
    expected_policy_version: str | None = None,
) -> dict[str, object]:
    """Validate terminal NVDA evidence using exact-source canonical signer trust.

    The legacy policy/pin arguments remain source-compatible only. They never
    select the terminal signer authority; policy/root identity comes exclusively
    from the AcceptedQualificationAttestation returned by the canonical verifier.
    """
    source_sha = _required_text(evidence.get("source_sha"), name="source_sha")
    if GIT_SHA.fullmatch(source_sha) is None:
        raise NvdaQualificationError(
            "source_sha must be an exact 40-character Git SHA"
        )
    (
        canonical_requirements,
        requirements_sha256,
        requirements_requirement_id,
    ) = _canonical_nvda_requirements(
        source_sha=source_sha,
        supplied_requirements=requirements,
    )
    result = validate_evidence(evidence, canonical_requirements)
    if SHA256.fullmatch(evidence_sha256) is None:
        raise NvdaQualificationError("evidence_sha256 must be canonical")
    if SHA256.fullmatch(release_artifact_sha256) is None:
        raise NvdaQualificationError("release artifact digest must be canonical")
    requirement_ids = _signed_nvda_requirement_ids(
        canonical_requirements,
        requirements_requirement_id,
    )
    release_artifact_id = _required_text(
        result.get("release_artifact_id"),
        name="release_artifact_id",
    )

    accepted = None
    for requirement_id in requirement_ids:
        try:
            current = verify_canonical_qualification_attestation(
                receipt,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
                expected_source_sha=result["source_sha"],
                expected_domain=NVDA_DOMAIN,
                expected_gate=NVDA_GATE,
                expected_package_id=NVDA_PACKAGE_ID,
                expected_protocol_id=NVDA_PROTOCOL_ID,
                expected_protocol_version=NVDA_PROTOCOL_VERSION,
                expected_requirement_id=requirement_id,
                expected_release_artifact_id=release_artifact_id,
                expected_release_artifact_sha256=release_artifact_sha256,
            )
        except (QualificationTrustError, TypeError, ValueError) as error:
            raise NvdaQualificationError(
                "signed NVDA qualification verification failed"
            ) from error
        if current.result != "PASS":
            raise NvdaQualificationError(
                f"signed NVDA qualification is not PASS for {requirement_id}"
            )
        if accepted is None:
            accepted = current
        elif (
            current.attestation_id != accepted.attestation_id
            or current.attestation_digest != accepted.attestation_digest
            or current.policy_id != accepted.policy_id
            or current.trust_root_id != accepted.trust_root_id
        ):
            raise NvdaQualificationError(
                "NVDA workflow requirements resolved to inconsistent trust receipts"
            )
    if accepted is None:
        raise NvdaQualificationError("signed NVDA qualification has no requirements")
    if tuple(sorted(accepted.requirement_ids)) != requirement_ids:
        raise NvdaQualificationError(
            "signed NVDA attestation requirements do not match the canonical requirements set"
        )
    if (
        accepted.release_artifact_id != release_artifact_id
        or accepted.release_artifact_sha256 != release_artifact_sha256
    ):
        raise NvdaQualificationError(
            "accepted NVDA attestation release binding is inconsistent"
        )
    exact_refs = [
        ref
        for ref in accepted.evidence_refs
        if (
            ref.sha256 == evidence_sha256
            and ref.evidence_kind == NVDA_EVIDENCE_KIND
            and ref.source_sha == result["source_sha"]
        )
    ]
    if len(exact_refs) != 1:
        raise NvdaQualificationError(
            "signed NVDA attestation must resolve the exact raw NVDA evidence payload"
        )
    return {
        **result,
        "qualified": True,
        "reason": "QUALIFIED_SIGNED_REAL_NVDA_RELEASE",
        "attestation_id": accepted.attestation_id,
        "attestation_digest": accepted.attestation_digest,
        "policy_id": accepted.policy_id,
        "trust_root_id": accepted.trust_root_id,
        "release_artifact_id": release_artifact_id,
        "artifact_sha256": release_artifact_sha256,
        "evidence_sha256": evidence_sha256,
        "requirements_sha256": requirements_sha256,
        "requirements_requirement_id": requirements_requirement_id,
    }


def _load_trust_inputs(args):
    values = (args.attestation, args.evidence_store)
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise NvdaQualificationError(
            "signed NVDA attestation and evidence store must be supplied together"
        )
    if not all(value is not None for value in values):
        return None
    if not args.evidence_store.is_dir():
        raise NvdaQualificationError(
            "NVDA evidence store must be an existing directory"
        )
    try:
        receipt = parse_signed_qualification_attestation(
            _load(args.attestation, name="signed NVDA attestation")
        )
    except (QualificationTrustError, TypeError, ValueError) as error:
        raise NvdaQualificationError("signed NVDA trust inputs are invalid") from error
    store = ArtifactStore(args.evidence_store)
    return receipt, store, args.evidence_store


def _load_evidence_once(path: Path) -> tuple[dict[str, object], str]:
    evidence, _, digest = _load_json_snapshot(path, name="evidence")
    return evidence, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--release-artifact", type=Path)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--check-status", action="store_true")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--evidence-store", type=Path)
    args = parser.parse_args()
    try:
        requirements = _load(args.requirements, name="requirements")
        trust = _load_trust_inputs(args)
        if args.check_status:
            status = _load(args.status, name="status")
            if status.get("qualified") is True:
                evidence_file = _required_text(
                    status.get("evidence_file"),
                    name="status.evidence_file",
                )
                evidence_relative = Path(evidence_file)
                if evidence_relative.is_absolute() or ".." in evidence_relative.parts:
                    raise NvdaQualificationError(
                        "status evidence_file must stay inside qualification/nvda"
                    )
                evidence_path = (ROOT / evidence_relative).resolve()
                qualification_root = (ROOT / "qualification" / "nvda").resolve()
                try:
                    evidence_path.relative_to(qualification_root)
                except ValueError as error:
                    raise NvdaQualificationError(
                        "status evidence_file must stay inside qualification/nvda"
                    ) from error
                if args.release_artifact is None:
                    raise NvdaQualificationError(
                        "qualified status requires --release-artifact"
                    )
                if trust is None:
                    raise NvdaQualificationError(
                        "qualified status requires signed qualification trust inputs"
                    )
                evidence, raw_evidence_sha = _load_evidence_once(evidence_path)
                actual_artifact_sha = validate_release_artifact_binding(
                    evidence,
                    args.release_artifact,
                )
                receipt, evidence_store, evidence_root = trust
                result = validate_trusted_nvda_qualification(
                    evidence,
                    requirements,
                    evidence_sha256=raw_evidence_sha,
                    release_artifact_sha256=actual_artifact_sha,
                    receipt=receipt,
                    evidence_store=evidence_store,
                    evidence_root=evidence_root,
                )
                for field in (
                    "source_sha",
                    "artifact_sha256",
                    "release_artifact_id",
                    "evidence_sha256",
                    "attestation_id",
                    "attestation_digest",
                    "policy_id",
                    "trust_root_id",
                ):
                    if result.get(field) != status.get(field):
                        raise NvdaQualificationError(
                            f"status {field} does not match independently verified NVDA evidence"
                        )
            else:
                if status.get("reason") != "NO_REAL_NVDA_RELEASE_EVIDENCE":
                    raise NvdaQualificationError(
                        "unqualified status must state NO_REAL_NVDA_RELEASE_EVIDENCE"
                    )
            print(json.dumps(status, sort_keys=True))
            return 0

        if args.evidence is None:
            raise NvdaQualificationError(
                "--evidence is required unless --check-status is used"
            )
        if args.release_artifact is None:
            raise NvdaQualificationError(
                "--release-artifact is required for real NVDA qualification"
            )
        evidence, raw_evidence_sha = _load_evidence_once(args.evidence)
        result = validate_evidence(evidence, requirements)
        actual_artifact_sha = validate_release_artifact_binding(
            evidence,
            args.release_artifact,
        )
        result["artifact_sha256"] = actual_artifact_sha
        result["evidence_sha256"] = raw_evidence_sha
        if trust is None:
            print(json.dumps(result, sort_keys=True))
            return 3

        receipt, evidence_store, evidence_root = trust
        result = validate_trusted_nvda_qualification(
            evidence,
            requirements,
            evidence_sha256=raw_evidence_sha,
            release_artifact_sha256=actual_artifact_sha,
            receipt=receipt,
            evidence_store=evidence_store,
            evidence_root=evidence_root,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except NvdaQualificationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
