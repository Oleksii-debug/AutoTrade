"""Validate exact-build real NVDA qualification evidence for AutoTrade."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = ROOT / "qualification" / "nvda" / "requirements.json"
DEFAULT_STATUS = ROOT / "qualification" / "nvda" / "status.json"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def validate_evidence(
    evidence: dict[str, object],
    requirements: dict[str, object],
) -> dict[str, object]:
    source_sha = _required_text(evidence.get("source_sha"), name="source_sha").lower()
    artifact_sha = _required_text(
        evidence.get("artifact_sha256"),
        name="artifact_sha256",
    ).lower()
    if GIT_SHA.fullmatch(source_sha) is None:
        raise NvdaQualificationError("source_sha must be an exact 40-character Git SHA")
    if SHA256.fullmatch(artifact_sha) is None:
        raise NvdaQualificationError("artifact_sha256 must be sha256:<64 lowercase hex>")
    if evidence.get("method") != requirements.get("evidence_method"):
        raise NvdaQualificationError("evidence method is not real NVDA keyboard qualification")

    environment = evidence.get("environment")
    if not isinstance(environment, dict):
        raise NvdaQualificationError("environment must be an object")
    for field in ("windows_version", "nvda_version", "input_mode"):
        _required_text(environment.get(field), name=f"environment.{field}")
    if environment.get("input_mode") != "keyboard-only":
        raise NvdaQualificationError("qualification must be keyboard-only")
    if not str(environment.get("windows_version")).startswith("Windows 11"):
        raise NvdaQualificationError("qualification must run on Windows 11")
    if evidence.get("release_artifact") is not True:
        raise NvdaQualificationError("qualification must run against the delivered release artifact")

    observations = evidence.get("workflows")
    if not isinstance(observations, list):
        raise NvdaQualificationError("workflows must be an array")
    by_id: dict[str, dict[str, object]] = {}
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
        _required_text(item.get("evidence_ref"), name=f"{workflow_id}.evidence_ref")
        by_id[workflow_id] = item

    required = requirements.get("workflows")
    if not isinstance(required, list) or not required:
        raise NvdaQualificationError("requirements contain no workflows")
    required_ids = {
        _required_text(item.get("id"), name="requirements.workflow.id")
        for item in required
        if isinstance(item, dict)
    }
    if set(by_id) != required_ids:
        missing = sorted(required_ids - set(by_id))
        extra = sorted(set(by_id) - required_ids)
        raise NvdaQualificationError(
            f"workflow evidence mismatch; missing={missing}; extra={extra}"
        )

    reviewer = _required_text(evidence.get("reviewer"), name="reviewer")
    observed_at = _required_text(evidence.get("observed_at"), name="observed_at")
    return {
        "schema_version": "1.0.0",
        "qualified": True,
        "source_sha": source_sha,
        "artifact_sha256": artifact_sha,
        "windows_version": environment["windows_version"],
        "nvda_version": environment["nvda_version"],
        "workflow_count": len(required_ids),
        "reviewer": reviewer,
        "observed_at": observed_at,
    }


def evidence_digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--evidence", type=Path)
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
                evidence_path = ROOT / evidence_file
                evidence = _load(evidence_path, name="evidence")
                result = validate_evidence(evidence, requirements)
                if result["source_sha"] != status.get("source_sha"):
                    raise NvdaQualificationError("status source SHA does not match evidence")
                if result["artifact_sha256"] != status.get("artifact_sha256"):
                    raise NvdaQualificationError("status artifact SHA does not match evidence")
                if status.get("evidence_sha256") != evidence_digest(evidence_path):
                    raise NvdaQualificationError("status evidence digest is stale")
            else:
                if status.get("reason") != "NO_REAL_NVDA_RELEASE_EVIDENCE":
                    raise NvdaQualificationError(
                        "unqualified status must state NO_REAL_NVDA_RELEASE_EVIDENCE"
                    )
            print(json.dumps(status, sort_keys=True))
            return 0

        if args.evidence is None:
            raise NvdaQualificationError("--evidence is required unless --check-status is used")
        evidence = _load(args.evidence, name="evidence")
        result = validate_evidence(evidence, requirements)
        result["evidence_sha256"] = evidence_digest(args.evidence)
        print(json.dumps(result, sort_keys=True))
        return 0
    except NvdaQualificationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
