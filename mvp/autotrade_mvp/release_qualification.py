"""Fail-closed exact-head release qualification for AutoTrade.

This module records whether a built candidate has the evidence required by the
canonical release architecture. It does not build, sign, install or authorize
trading. A PASS is possible only when every required gate is PASS on the exact
candidate source revision.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping


class ReleaseQualificationError(ValueError):
    pass


REQUIRED_RELEASE_CHECKS = (
    "PR_CI",
    "LINUX_VERIFICATION",
    "WINDOWS_VERIFICATION",
    "WINDOWS_INSTALL",
    "UPDATE_ROLLBACK",
    "NVDA_KEYBOARD",
    "BACKUP_RESTORE_CLEAN_MACHINE",
    "POST_RESTORE_RECONCILIATION",
    "EVIDENCE_EXPORT",
    "DEPENDENCY_RIGHTS",
    "SECURITY_SECRET_SCAN",
    "PROVIDER_QUALIFICATION",
)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseQualificationError(f"{name} is required")
    return value.strip()


def _git_sha(value: str, name: str = "source_sha") -> str:
    normalized = _text(value, name)
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized) is None:
        raise ReleaseQualificationError(f"{name} must be a canonical Git object id")
    return normalized


def _sha256(value: str, name: str) -> str:
    normalized = _text(value, name)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) is None:
        raise ReleaseQualificationError(f"{name} must be a canonical SHA-256 digest")
    return normalized


@dataclass(frozen=True)
class ReleaseCheck:
    name: str
    status: str
    source_sha: str
    evidence_ref: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        status: str,
        source_sha: str,
        evidence_ref: str,
    ) -> "ReleaseCheck":
        normalized_name = _text(name, "name").upper()
        normalized_status = _text(status, "status").upper()
        if normalized_status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ReleaseQualificationError("status must be PASS, FAIL or INCONCLUSIVE")
        return cls(
            name=normalized_name,
            status=normalized_status,
            source_sha=_git_sha(source_sha),
            evidence_ref=_text(evidence_ref, "evidence_ref"),
        )


@dataclass(frozen=True)
class ReleaseCandidate:
    version: str
    source_sha: str
    installer_sha256: str
    diagnostics_sha256: str
    sbom_sha256: str
    compatibility_manifest_sha256: str
    signatures_verified: bool

    @classmethod
    def create(
        cls,
        *,
        version: str,
        source_sha: str,
        installer_sha256: str,
        diagnostics_sha256: str,
        sbom_sha256: str,
        compatibility_manifest_sha256: str,
        signatures_verified: bool,
    ) -> "ReleaseCandidate":
        if not isinstance(signatures_verified, bool):
            raise ReleaseQualificationError("signatures_verified must be boolean")
        return cls(
            version=_text(version, "version"),
            source_sha=_git_sha(source_sha),
            installer_sha256=_sha256(installer_sha256, "installer_sha256"),
            diagnostics_sha256=_sha256(diagnostics_sha256, "diagnostics_sha256"),
            sbom_sha256=_sha256(sbom_sha256, "sbom_sha256"),
            compatibility_manifest_sha256=_sha256(
                compatibility_manifest_sha256,
                "compatibility_manifest_sha256",
            ),
            signatures_verified=signatures_verified,
        )


@dataclass(frozen=True)
class ReleaseDecision:
    status: str
    reasons: tuple[str, ...]
    checks: Mapping[str, str]
    source_sha: str
    live_authority_granted: bool = False


def evaluate_release(
    candidate: ReleaseCandidate,
    checks: Iterable[ReleaseCheck],
) -> ReleaseDecision:
    """Evaluate one immutable release candidate without inheriting stale evidence."""

    rows = tuple(checks)
    by_name: dict[str, ReleaseCheck] = {}
    for row in rows:
        if not isinstance(row, ReleaseCheck):
            raise ReleaseQualificationError("checks must contain ReleaseCheck values")
        if row.name in by_name:
            raise ReleaseQualificationError(f"duplicate release check: {row.name}")
        by_name[row.name] = row

    required = set(REQUIRED_RELEASE_CHECKS)
    unknown = sorted(set(by_name) - required)
    if unknown:
        raise ReleaseQualificationError(
            "unknown release checks require schema review: " + ", ".join(unknown)
        )

    reasons: list[str] = []
    check_statuses: dict[str, str] = {}
    missing = sorted(required - set(by_name))
    for name in missing:
        check_statuses[name] = "INCONCLUSIVE"
        reasons.append(f"{name}: evidence missing")

    for name in REQUIRED_RELEASE_CHECKS:
        row = by_name.get(name)
        if row is None:
            continue
        if row.source_sha != candidate.source_sha:
            check_statuses[name] = "FAIL"
            reasons.append(f"{name}: evidence belongs to a different source SHA")
            continue
        check_statuses[name] = row.status
        if row.status == "FAIL":
            reasons.append(f"{name}: gate failed")
        elif row.status == "INCONCLUSIVE":
            reasons.append(f"{name}: gate is inconclusive")

    if not candidate.signatures_verified:
        reasons.append("release artifact signatures are not verified")

    has_failure = any(value == "FAIL" for value in check_statuses.values())
    has_unknown = any(value == "INCONCLUSIVE" for value in check_statuses.values())
    if not candidate.signatures_verified:
        has_failure = True

    if has_failure:
        status = "FAIL"
    elif has_unknown:
        status = "INCONCLUSIVE"
    else:
        status = "PASS"
        reasons = ["all exact-head release gates passed"]

    return ReleaseDecision(
        status=status,
        reasons=tuple(reasons),
        checks=check_statuses,
        source_sha=candidate.source_sha,
        live_authority_granted=False,
    )
