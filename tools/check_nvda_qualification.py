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

from research.autotrade_research.artifacts.store import ArtifactStore
from mvp.autotrade_mvp.qualification_attestation import (
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    parse_qualification_trust_policy,
    parse_signed_qualification_attestation,
    verify_qualification_attestation,
)

DEFAULT_REQUIREMENTS = ROOT / "qualification" / "nvda" / "requirements.json"
DEFAULT_STATUS = ROOT / "qualification" / "nvda" / "status.json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NVDA_DOMAIN = "ACCESSIBILITY"
_NVDA_GATE = "NVDA_KEYBOARD"
_NVDA_PACKAGE = "WP-53"
_NVDA_PROTOCOL = "nvda-keyboard-release-v1"
_NVDA_PROTOCOL_VERSION = "1.0.0"
_NVDA_RECORD_KIND = "NVDA_QUALIFICATION_RECORD"
_NVDA_WORKFLOW_KIND = "NVDA_WORKFLOW_EVIDENCE"


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
        "qualified": False,
        "evidence_complete": True,
        "reason": "INDEPENDENT_QUALIFICATION_ATTESTATION_REQUIRED",
        "source_sha": source_sha,
        "release_artifact_id": release_artifact_id,
        "artifact_sha256": artifact_sha,
        "windows_version": environment["windows_version"],
        "nvda_version": environment["nvda_version"],
        "workflow_count": len(required_ids),
        "workflow_ids": sorted(required_ids),
        "workflow_evidence_refs": sorted(evidence_refs),
        "reviewer": reviewer,
        "observed_at": observed_at,
    }


def validate_trusted_evidence(
    evidence: dict[str, object],
    requirements: dict[str, object],
    *,
    receipt: SignedQualificationAttestation,
    policy: QualificationTrustPolicy,
    evidence_store: ArtifactStore,
    expected_policy_id: str,
    expected_policy_version: str,
    evidence_sha256: str,
) -> dict[str, object]:
    """Promote complete NVDA evidence only through the canonical signed trust boundary."""
    result = validate_evidence(evidence, requirements)
    if SHA256.fullmatch(evidence_sha256) is None:
        raise NvdaQualificationError(
            "evidence_sha256 must be an immutable sha256 digest"
        )

    required_ids = frozenset(result["workflow_ids"])
    attestation = receipt.attestation
    if frozenset(attestation.requirement_ids) != required_ids:
        raise NvdaQualificationError(
            "signed attestation does not cover the exact NVDA requirement set"
        )

    workflow_refs = frozenset(result["workflow_evidence_refs"])
    receipt_refs = tuple(attestation.evidence_refs)
    receipt_digests = frozenset(item.sha256 for item in receipt_refs)
    expected_digests = workflow_refs | {evidence_sha256}
    if receipt_digests != expected_digests:
        raise NvdaQualificationError(
            "signed attestation does not bind the exact NVDA evidence population"
        )
    aggregate_refs = [
        item
        for item in receipt_refs
        if item.sha256 == evidence_sha256
        and item.evidence_kind == _NVDA_RECORD_KIND
    ]
    if len(aggregate_refs) != 1:
        raise NvdaQualificationError(
            "signed attestation must bind the aggregate NVDA qualification record"
        )
    workflow_receipts = [
        item
        for item in receipt_refs
        if item.sha256 in workflow_refs
        and item.evidence_kind == _NVDA_WORKFLOW_KIND
    ]
    if len(workflow_receipts) != len(workflow_refs):
        raise NvdaQualificationError(
            "signed attestation must bind every NVDA workflow evidence artifact"
        )

    try:
        accepted = verify_qualification_attestation(
            receipt,
            policy=policy,
            evidence_store=evidence_store,
            expected_policy_id=expected_policy_id,
            expected_policy_version=expected_policy_version,
            expected_source_sha=result["source_sha"],
            expected_domain=_NVDA_DOMAIN,
            expected_gate=_NVDA_GATE,
            expected_package_id=_NVDA_PACKAGE,
            expected_protocol_id=_NVDA_PROTOCOL,
            expected_protocol_version=_NVDA_PROTOCOL_VERSION,
            expected_requirement_id=sorted(required_ids)[0],
            expected_release_artifact_id=result["release_artifact_id"],
            expected_release_artifact_sha256=result["artifact_sha256"],
        )
    except QualificationTrustError as error:
        raise NvdaQualificationError(
            f"signed NVDA qualification trust rejected: {error}"
        ) from error
    if accepted.result != "PASS":
        raise NvdaQualificationError(
            f"signed NVDA qualification result is not PASS: {accepted.result}"
        )

    return {
        **result,
        "qualified": True,
        "reason": None,
        "evidence_sha256": evidence_sha256,
        "qualification_attestation_id": accepted.attestation_id,
        "qualification_attestation_digest": accepted.attestation_digest,
        "qualification_policy_id": accepted.policy_id,
        "qualification_trust_root_id": accepted.trust_root_id,
    }


def _trusted_qualification_from_files(
    *,
    evidence_path: Path,
    requirements: dict[str, object],
    release_artifact: Path,
    attestation_path: Path,
    trust_policy_path: Path,
    evidence_store_path: Path,
    expected_policy_id: str,
    expected_policy_version: str,
) -> dict[str, object]:
    evidence = _load(evidence_path, name="evidence")
    validate_evidence(evidence, requirements)
    validate_release_artifact_binding(evidence, release_artifact)
    if not evidence_store_path.is_dir():
        raise NvdaQualificationError(
            "qualification evidence store must already exist"
        )
    try:
        policy = parse_qualification_trust_policy(
            _load(trust_policy_path, name="qualification trust policy")
        )
        receipt = parse_signed_qualification_attestation(
            _load(attestation_path, name="signed qualification attestation")
        )
    except QualificationTrustError as error:
        raise NvdaQualificationError(
            f"signed NVDA qualification input is invalid: {error}"
        ) from error
    store = ArtifactStore(evidence_store_path)
    return validate_trusted_evidence(
        evidence,
        requirements,
        receipt=receipt,
        policy=policy,
        evidence_store=store,
        expected_policy_id=expected_policy_id,
        expected_policy_version=expected_policy_version,
        evidence_sha256=evidence_digest(evidence_path),
    )


def _require_trust_arguments(args: argparse.Namespace) -> None:
    missing = [
        flag
        for flag, value in (
            ("--qualification-attestation", args.qualification_attestation),
            ("--qualification-trust-policy", args.qualification_trust_policy),
            ("--qualification-evidence-store", args.qualification_evidence_store),
            ("--expected-policy-id", args.expected_policy_id),
            ("--expected-policy-version", args.expected_policy_version),
        )
        if value is None
    ]
    if missing:
        raise NvdaQualificationError(
            "terminal NVDA qualification requires signed qualification trust: "
            + ", ".join(missing)
        )


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--release-artifact", type=Path)
    parser.add_argument("--qualification-attestation", type=Path)
    parser.add_argument("--qualification-trust-policy", type=Path)
    parser.add_argument("--qualification-evidence-store", type=Path)
    parser.add_argument("--expected-policy-id")
    parser.add_argument("--expected-policy-version")
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--check-status", action="store_true")
    args = parser.parse_args()
    try:
        requirements = _load(args.requirements, name="requirements")
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
                        "qualified status requires --release-artifact or a future "
                        "independently trusted artifact-binding attestation"
                    )
                _require_trust_arguments(args)
                result = _trusted_qualification_from_files(
                    evidence_path=evidence_path,
                    requirements=requirements,
                    release_artifact=args.release_artifact,
                    attestation_path=args.qualification_attestation,
                    trust_policy_path=args.qualification_trust_policy,
                    evidence_store_path=args.qualification_evidence_store,
                    expected_policy_id=args.expected_policy_id,
                    expected_policy_version=args.expected_policy_version,
                )
                actual_artifact_sha = result["artifact_sha256"]
                if result["source_sha"] != status.get("source_sha"):
                    raise NvdaQualificationError("status source SHA does not match evidence")
                if result["artifact_sha256"] != status.get("artifact_sha256"):
                    raise NvdaQualificationError("status artifact SHA does not match evidence")
                if actual_artifact_sha != status.get("artifact_sha256"):
                    raise NvdaQualificationError(
                        "status artifact SHA does not match observed release artifact"
                    )
                if status.get("evidence_sha256") != evidence_digest(evidence_path):
                    raise NvdaQualificationError("status evidence digest is stale")
                for field in (
                    "release_artifact_id",
                    "qualification_attestation_id",
                    "qualification_attestation_digest",
                    "qualification_policy_id",
                    "qualification_trust_root_id",
                ):
                    if status.get(field) != result.get(field):
                        raise NvdaQualificationError(
                            f"status {field} does not match accepted qualification trust"
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
        _require_trust_arguments(args)
        result = _trusted_qualification_from_files(
            evidence_path=args.evidence,
            requirements=requirements,
            release_artifact=args.release_artifact,
            attestation_path=args.qualification_attestation,
            trust_policy_path=args.qualification_trust_policy,
            evidence_store_path=args.qualification_evidence_store,
            expected_policy_id=args.expected_policy_id,
            expected_policy_version=args.expected_policy_version,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except NvdaQualificationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
