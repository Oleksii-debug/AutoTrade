"""Validate exact-build real NVDA qualification evidence for AutoTrade."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
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
    parse_qualification_trust_policy,
    parse_signed_qualification_attestation,
    verify_qualification_attestation,
)
from research.autotrade_research.artifacts.store import ArtifactStore

DEFAULT_REQUIREMENTS = ROOT / "qualification" / "nvda" / "requirements.json"
DEFAULT_STATUS = ROOT / "qualification" / "nvda" / "status.json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
NVDA_DOMAIN = "ACCESSIBILITY"
NVDA_GATE = "NVDA_RELEASE"
NVDA_PACKAGE_ID = "WP-53"
NVDA_PROTOCOL_ID = "real-nvda-keyboard-v1"
NVDA_PROTOCOL_VERSION = "1.0.0"
NVDA_EVIDENCE_KIND = "NVDA_REAL_RUN"
SIGNED_ATTESTATION_REQUIRED = "SIGNED_QUALIFICATION_ATTESTATION_REQUIRED"


class NvdaQualificationError(ValueError):
    pass


def _load(path: Path, *, name: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NvdaQualificationError(f"{name} is missing or invalid") from error
    if not isinstance(value, dict):
        raise NvdaQualificationError(f"{name} must be an object")
    return value


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
    required_os = _required_text(required_environment.get("os_family"), name="requirements.os_family")
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
        raise NvdaQualificationError("qualification must run against the delivered release artifact")

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
        evidence_ref = _required_text(item.get("evidence_ref"), name=f"{workflow_id}.evidence_ref")
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
    observed_at = observed_instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.0.0",
        "evidence_complete": True,
        "qualified": False,
        "reason": SIGNED_ATTESTATION_REQUIRED,
        "source_sha": source_sha,
        "artifact_sha256": artifact_sha,
        "windows_version": environment["windows_version"],
        "nvda_version": environment["nvda_version"],
        "workflow_count": len(required_ids),
        "reviewer": reviewer,
        "observed_at": observed_at,
    }


def evidence_digest(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise NvdaQualificationError("evidence file cannot be read") from error
    return "sha256:" + sha256(payload).hexdigest()


def release_artifact_digest(path: Path) -> str:
    try:
        if not path.is_file():
            raise NvdaQualificationError("release artifact must be a readable file")
        payload = path.read_bytes()
    except OSError as error:
        raise NvdaQualificationError("release artifact cannot be read") from error
    return "sha256:" + sha256(payload).hexdigest()


def _release_bundle_source_sha(release_artifact: Path) -> str:
    try:
        with zipfile.ZipFile(release_artifact, "r") as archive:
            manifest_names = [
                name for name in archive.namelist()
                if name == "bundle-manifest.json"
            ]
            if len(manifest_names) != 1:
                raise NvdaQualificationError(
                    "release artifact must contain exactly one bundle-manifest.json"
                )
            info = archive.getinfo("bundle-manifest.json")
            if info.file_size > 1024 * 1024:
                raise NvdaQualificationError("release bundle manifest is unreasonably large")
            try:
                manifest = json.loads(
                    archive.read(info).decode("utf-8")
                )
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
        raise NvdaQualificationError("NVDA qualification requires a release-mode bundle")
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
    actual = release_artifact_digest(release_artifact)
    if declared != actual:
        raise NvdaQualificationError(
            "release artifact SHA-256 does not match NVDA evidence"
        )
    artifact_source_sha = _release_bundle_source_sha(release_artifact)
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
    policy: QualificationTrustPolicy,
    evidence_store: ArtifactStore,
    expected_policy_id: str,
    expected_policy_version: str,
) -> dict[str, object]:
    result = validate_evidence(evidence, requirements)
    if SHA256.fullmatch(evidence_sha256) is None:
        raise NvdaQualificationError("evidence_sha256 must be canonical")
    if SHA256.fullmatch(release_artifact_sha256) is None:
        raise NvdaQualificationError("release artifact digest must be canonical")
    required = requirements.get("workflows")
    if not isinstance(required, list) or not required:
        raise NvdaQualificationError("requirements contain no workflows")
    if any(not isinstance(item, dict) for item in required):
        raise NvdaQualificationError("requirements.workflow must be an object")
    requirement_ids = tuple(
        sorted(
            _required_text(item.get("id"), name="requirements.workflow.id")
            for item in required
        )
    )
    if tuple(receipt.attestation.requirement_ids) != requirement_ids:
        raise NvdaQualificationError(
            "signed NVDA attestation requirements do not match the canonical workflow set"
        )
    release_artifact_id = receipt.attestation.release_artifact_id
    if release_artifact_id is None:
        raise NvdaQualificationError(
            "signed NVDA attestation must bind the delivered release artifact"
        )
    if receipt.attestation.release_artifact_sha256 != release_artifact_sha256:
        raise NvdaQualificationError(
            "signed NVDA attestation release digest does not match the delivered artifact"
        )
    exact_refs = [
        ref
        for ref in receipt.attestation.evidence_refs
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

    accepted = None
    for requirement_id in requirement_ids:
        try:
            current = verify_qualification_attestation(
                receipt,
                policy=policy,
                evidence_store=evidence_store,
                expected_policy_id=expected_policy_id,
                expected_policy_version=expected_policy_version,
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
    }


def _load_trust_inputs(args):
    values = (
        args.attestation,
        args.qualification_policy,
        args.evidence_store,
        args.expected_policy_id,
        args.expected_policy_version,
    )
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise NvdaQualificationError(
            "signed NVDA trust inputs must be supplied together"
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
        policy = parse_qualification_trust_policy(
            _load(args.qualification_policy, name="qualification trust policy")
        )
    except (QualificationTrustError, TypeError, ValueError) as error:
        raise NvdaQualificationError(
            "signed NVDA trust inputs are invalid"
        ) from error
    return (
        receipt,
        policy,
        ArtifactStore(args.evidence_store),
        args.expected_policy_id,
        args.expected_policy_version,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--release-artifact", type=Path)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--check-status", action="store_true")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--qualification-policy", type=Path)
    parser.add_argument("--evidence-store", type=Path)
    parser.add_argument("--expected-policy-id")
    parser.add_argument("--expected-policy-version")
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
                evidence = _load(evidence_path, name="evidence")
                actual_artifact_sha = validate_release_artifact_binding(
                    evidence,
                    args.release_artifact,
                )
                receipt, policy, evidence_store, policy_id, policy_version = trust
                result = validate_trusted_nvda_qualification(
                    evidence,
                    requirements,
                    evidence_sha256=evidence_digest(evidence_path),
                    release_artifact_sha256=actual_artifact_sha,
                    receipt=receipt,
                    policy=policy,
                    evidence_store=evidence_store,
                    expected_policy_id=policy_id,
                    expected_policy_version=policy_version,
                )
                for field in (
                    "source_sha",
                    "artifact_sha256",
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
            raise NvdaQualificationError("--evidence is required unless --check-status is used")
        if args.release_artifact is None:
            raise NvdaQualificationError(
                "--release-artifact is required for real NVDA qualification"
            )
        evidence = _load(args.evidence, name="evidence")
        result = validate_evidence(evidence, requirements)
        actual_artifact_sha = validate_release_artifact_binding(
            evidence,
            args.release_artifact,
        )
        raw_evidence_sha = evidence_digest(args.evidence)
        result["artifact_sha256"] = actual_artifact_sha
        result["evidence_sha256"] = raw_evidence_sha
        if trust is None:
            print(json.dumps(result, sort_keys=True))
            return 3

        receipt, policy, evidence_store, policy_id, policy_version = trust
        result = validate_trusted_nvda_qualification(
            evidence,
            requirements,
            evidence_sha256=raw_evidence_sha,
            release_artifact_sha256=actual_artifact_sha,
            receipt=receipt,
            policy=policy,
            evidence_store=evidence_store,
            expected_policy_id=policy_id,
            expected_policy_version=policy_version,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except NvdaQualificationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
