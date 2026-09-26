"""Fail-closed whole-product completion audit for canonical AutoTrade evidence.

This tool grants no financial or release authority.  Completion requires the
whole approved product surface, all 65 work packages, all canonical
qualification gates, exact-source evidence, and real NVDA qualification on the
same source revision.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mvp.autotrade_mvp.qualification_attestation import (
    QualificationTrustError,
    QualificationTrustPolicy,
    parse_qualification_trust_policy,
    parse_signed_qualification_attestation,
    verify_qualification_attestation,
)
from research.autotrade_research.artifacts.store import ArtifactStore

DEFAULT_SPEC = ROOT / "docs" / "product" / "PRODUCT_SPEC_CANONICAL.txt"
DEFAULT_BANK = ROOT / "control" / "work-packages" / "bank.json"
DEFAULT_QUALIFICATION = ROOT / "control" / "qualification.json"
DEFAULT_NVDA_STATUS = ROOT / "qualification" / "nvda" / "status.json"

EXPECTED_SECTION_IDS = tuple(f"SECTION-{index:02d}" for index in range(1, 41))
EXPECTED_PACKAGE_IDS = tuple(f"WP-{index:02d}" for index in range(1, 66))
EXPECTED_GATE_NAMES = frozenset(
    {
        "engineering_baseline",
        "repository_bootstrap",
        "reusable_code_migration",
        "financial_runtime",
        "providers",
        "economic_edge",
        "learning",
        "accessibility",
        "release",
        "quality_pipeline",
        "canonical_contracts",
        "dependency_provenance",
        "control_plane",
        "selected_first_party_reuse",
        "restart_recovery",
        "command_surface",
        "persistence",
        "artifact_store",
        "reservations",
    }
)
TERMINAL_PACKAGE_STATUS = "DONE"
TERMINAL_OVERALL_STATUS = "FULL_PRODUCT_QUALIFIED"
TERMINAL_GATE_STATUS = "QUALIFIED"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256_TEXT = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID_TEXT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NVDA_TERMINAL_REASON = "QUALIFIED_SIGNED_REAL_NVDA_RELEASE"
_WHOLE_PRODUCT_DOMAIN = "WHOLE_PRODUCT"
_WHOLE_PRODUCT_GATE = "COMPLETION"
_WHOLE_PRODUCT_PACKAGE = "WP-60"
_WHOLE_PRODUCT_PROTOCOL = "whole-product-completion-v1"
_WHOLE_PRODUCT_PROTOCOL_VERSION = "1.0.0"


@dataclass(frozen=True)
class WholeProductEvidenceContext:
    evidence_store: ArtifactStore
    policy: QualificationTrustPolicy
    expected_policy_id: str
    expected_policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_store, ArtifactStore):
            raise TypeError("evidence_store must be ArtifactStore")
        if not isinstance(self.policy, QualificationTrustPolicy):
            raise TypeError("policy must be QualificationTrustPolicy")
        if not isinstance(self.expected_policy_id, str) or not self.expected_policy_id:
            raise ValueError("expected_policy_id is required")
        if (
            not isinstance(self.expected_policy_version, str)
            or not self.expected_policy_version
        ):
            raise ValueError("expected_policy_version is required")


class ProductCompletionError(ValueError):
    pass


def _load(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductCompletionError(f"{name} is missing or invalid") from error
    if type(value) is not dict:
        raise ProductCompletionError(f"{name} must be an object")
    return value


def _load_text(path: Path, *, name: str) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProductCompletionError(f"{name} is missing") from error
    if not value.strip():
        raise ProductCompletionError(f"{name} is empty")
    return value


def _section_ids(spec_text: str) -> tuple[str, ...]:
    if not isinstance(spec_text, str) or not spec_text.strip():
        raise ProductCompletionError("canonical product spec must be non-empty text")
    found: list[str] = []
    seen: set[int] = set()
    for line in spec_text.splitlines():
        match = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", line)
        if match is None:
            continue
        number = int(match.group(1))
        if 1 <= number <= 40 and number not in seen:
            seen.add(number)
            found.append(f"SECTION-{number:02d}")
    if tuple(found) != EXPECTED_SECTION_IDS:
        raise ProductCompletionError(
            "canonical product spec must expose ordered sections 1..40"
        )
    return tuple(found)


def _exact_source(value: object) -> str | None:
    if isinstance(value, str) and _GIT_SHA.fullmatch(value):
        return value
    return None


def _terminal_nvda_status(
    nvda_status: dict[str, Any],
    *,
    exact_source_sha: str | None,
) -> bool:
    if nvda_status.get("qualified") is not True:
        return False
    if nvda_status.get("reason") != _NVDA_TERMINAL_REASON:
        return False
    if exact_source_sha is None or nvda_status.get("source_sha") != exact_source_sha:
        return False
    for field in ("release_artifact_id", "attestation_id"):
        value = nvda_status.get(field)
        if not isinstance(value, str) or _UUID_TEXT.fullmatch(value) is None:
            return False
    for field in (
        "artifact_sha256",
        "evidence_sha256",
        "attestation_digest",
        "policy_id",
        "trust_root_id",
    ):
        value = nvda_status.get(field)
        if not isinstance(value, str) or _SHA256_TEXT.fullmatch(value) is None:
            return False
    return True


def _independently_verified_evidence(
    item: dict[str, Any],
    *,
    requirement_id: str,
    exact_source_sha: str | None,
    evidence_context: WholeProductEvidenceContext | None,
) -> bool:
    if exact_source_sha is None or evidence_context is None:
        return False
    receipt_payload = item.get("receipt")
    evidence_ref = item.get("evidence_ref")
    try:
        receipt = parse_signed_qualification_attestation(receipt_payload)
        if evidence_ref != receipt.attestation.attestation_id:
            return False
        accepted = verify_qualification_attestation(
            receipt,
            policy=evidence_context.policy,
            evidence_store=evidence_context.evidence_store,
            expected_policy_id=evidence_context.expected_policy_id,
            expected_policy_version=evidence_context.expected_policy_version,
            expected_source_sha=exact_source_sha,
            expected_domain=_WHOLE_PRODUCT_DOMAIN,
            expected_gate=_WHOLE_PRODUCT_GATE,
            expected_package_id=_WHOLE_PRODUCT_PACKAGE,
            expected_protocol_id=_WHOLE_PRODUCT_PROTOCOL,
            expected_protocol_version=_WHOLE_PRODUCT_PROTOCOL_VERSION,
            expected_requirement_id=requirement_id,
        )
    except (QualificationTrustError, TypeError, ValueError):
        return False
    return (
        accepted.result == "PASS"
        and accepted.source_sha == exact_source_sha
        and accepted.requirement_id == requirement_id
        and accepted.attestation_id == evidence_ref
    )


def _evidence_matrix(
    qualification: dict[str, Any],
    *,
    exact_source_sha: str | None,
    evidence_context: WholeProductEvidenceContext | None,
) -> tuple[list[str], list[str], list[str]]:
    required = {
        ("PRODUCT_SECTION", section) for section in EXPECTED_SECTION_IDS
    } | {
        ("WORK_PACKAGE", package) for package in EXPECTED_PACKAGE_IDS
    }
    raw = qualification.get("whole_product_evidence")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ProductCompletionError("whole_product_evidence must be an array")

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    evidence_refs: set[str] = set()
    for item in raw:
        if type(item) is not dict:
            raise ProductCompletionError("whole_product_evidence entries must be objects")
        kind = item.get("kind")
        requirement_id = item.get("requirement_id")
        key = (kind, requirement_id)
        if key not in required:
            raise ProductCompletionError(
                f"whole_product_evidence references unknown requirement: {key}"
            )
        if key in by_key:
            raise ProductCompletionError(
                f"duplicate whole-product evidence: {kind}:{requirement_id}"
            )
        evidence_ref = item.get("evidence_ref")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ProductCompletionError(
                f"evidence_ref is required for {kind}:{requirement_id}"
            )
        if evidence_ref in evidence_refs:
            raise ProductCompletionError(
                f"whole-product evidence_ref must be unique: {evidence_ref}"
            )
        evidence_refs.add(evidence_ref)
        by_key[key] = item

    missing_sections = [
        section
        for section in EXPECTED_SECTION_IDS
        if ("PRODUCT_SECTION", section) not in by_key
    ]
    missing_packages = [
        package
        for package in EXPECTED_PACKAGE_IDS
        if ("WORK_PACKAGE", package) not in by_key
    ]
    nonpassing: list[str] = []
    for (kind, requirement_id), item in sorted(by_key.items()):
        if item.get("status") != "PASS":
            nonpassing.append(f"{kind}:{requirement_id}:status")
        if exact_source_sha is None or item.get("source_sha") != exact_source_sha:
            nonpassing.append(f"{kind}:{requirement_id}:source_sha")
        if not _independently_verified_evidence(
            item,
            requirement_id=requirement_id,
            exact_source_sha=exact_source_sha,
            evidence_context=evidence_context,
        ):
            nonpassing.append(
                f"{kind}:{requirement_id}:independent_verification"
            )
    return missing_sections, missing_packages, nonpassing


def evaluate_completion(
    bank: dict[str, Any],
    qualification: dict[str, Any],
    nvda_status: dict[str, Any],
    *,
    spec_text: str,
    exact_source_sha: str | None,
    evidence_context: WholeProductEvidenceContext | None = None,
) -> dict[str, Any]:
    sections = _section_ids(spec_text)
    packages = bank.get("packages")
    if not isinstance(packages, list):
        raise ProductCompletionError("work-package bank packages must be an array")

    by_id: dict[str, dict[str, Any]] = {}
    for item in packages:
        if type(item) is not dict:
            raise ProductCompletionError("every work package must be an object")
        package_id = item.get("id")
        if not isinstance(package_id, str) or not package_id:
            raise ProductCompletionError("every work package requires an id")
        if package_id in by_id:
            raise ProductCompletionError(f"duplicate work package id: {package_id}")
        by_id[package_id] = item

    expected = set(EXPECTED_PACKAGE_IDS)
    actual = set(by_id)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProductCompletionError(
            f"work-package bank must contain exactly WP-01..WP-65; "
            f"missing={missing}; extra={extra}"
        )

    incomplete_packages = tuple(
        package_id
        for package_id in EXPECTED_PACKAGE_IDS
        if by_id[package_id].get("proposed_status") != TERMINAL_PACKAGE_STATUS
    )

    gates = qualification.get("gates")
    if type(gates) is not dict or not gates:
        raise ProductCompletionError("qualification gates must be a non-empty object")
    missing_gates = sorted(EXPECTED_GATE_NAMES - set(gates))
    unknown_gates = [name for name in gates if name not in EXPECTED_GATE_NAMES]
    if unknown_gates:
        raise ProductCompletionError(
            "qualification gates contain unknown entries: "
            + ", ".join(sorted(repr(name) for name in unknown_gates))
        )
    nonterminal_gates = {
        str(name): value
        for name, value in sorted(gates.items())
        if value != TERMINAL_GATE_STATUS
    }

    source_sha = _exact_source(exact_source_sha)
    qualification_source_sha = _exact_source(qualification.get("source_sha"))
    qualification_source_matches = (
        source_sha is not None and qualification_source_sha == source_sha
    )
    missing_sections, missing_evidence_packages, nonpassing_evidence = _evidence_matrix(
        qualification,
        exact_source_sha=source_sha,
        evidence_context=evidence_context,
    )
    overall_status = qualification.get("overall_status")
    nvda_source_matches = (
        source_sha is not None and nvda_status.get("source_sha") == source_sha
    )
    nvda_qualified = _terminal_nvda_status(
        nvda_status,
        exact_source_sha=source_sha,
    )

    blockers: list[str] = []
    if source_sha is None:
        blockers.append("exact source SHA is missing or non-canonical")
    if not qualification_source_matches:
        blockers.append(
            "qualification source SHA is missing, non-canonical, or not the exact source SHA"
        )
    if len(sections) != 40:
        blockers.append("canonical product section count is not 40")
    if incomplete_packages:
        blockers.append(
            f"{len(incomplete_packages)} work packages are not {TERMINAL_PACKAGE_STATUS}"
        )
    if missing_gates:
        blockers.append(
            f"{len(missing_gates)} required qualification gates are missing"
        )
    if overall_status != TERMINAL_OVERALL_STATUS:
        blockers.append("overall qualification is not FULL_PRODUCT_QUALIFIED")
    if nonterminal_gates:
        blockers.append(
            f"{len(nonterminal_gates)} qualification gates are not QUALIFIED"
        )
    if missing_sections:
        blockers.append(
            f"{len(missing_sections)} product sections lack exact-source PASS evidence"
        )
    if missing_evidence_packages:
        blockers.append(
            f"{len(missing_evidence_packages)} work packages lack exact-source PASS evidence"
        )
    if nonpassing_evidence:
        blockers.append(
            f"{len(nonpassing_evidence)} whole-product evidence checks are nonpassing or stale"
        )
    if not nvda_qualified:
        blockers.append("real NVDA release qualification is not complete")
    if not nvda_source_matches:
        blockers.append("NVDA qualification is not bound to the exact source SHA")

    return {
        "schema_version": "2.0.0",
        "complete": not blockers,
        "exact_source_sha": source_sha,
        "qualification_source_sha": qualification_source_sha,
        "qualification_source_matches": qualification_source_matches,
        "product_section_count": len(sections),
        "package_count": len(by_id),
        "terminal_package_status": TERMINAL_PACKAGE_STATUS,
        "incomplete_packages": list(incomplete_packages),
        "missing_required_gates": missing_gates,
        "overall_status": overall_status,
        "required_overall_status": TERMINAL_OVERALL_STATUS,
        "nonterminal_gates": nonterminal_gates,
        "missing_section_evidence": missing_sections,
        "missing_package_evidence": missing_evidence_packages,
        "nonpassing_evidence": nonpassing_evidence,
        "nvda_qualified": nvda_qualified,
        "nvda_source_matches": nvda_source_matches,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    parser.add_argument("--qualification", type=Path, default=DEFAULT_QUALIFICATION)
    parser.add_argument("--nvda-status", type=Path, default=DEFAULT_NVDA_STATUS)
    parser.add_argument("--source-sha")
    parser.add_argument("--evidence-store", type=Path)
    parser.add_argument("--qualification-policy", type=Path)
    parser.add_argument("--expected-policy-id")
    parser.add_argument("--expected-policy-version")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()

    try:
        qualification = _load(args.qualification, name="qualification")
        trust_values = (
            args.evidence_store,
            args.qualification_policy,
            args.expected_policy_id,
            args.expected_policy_version,
        )
        if any(value is not None for value in trust_values) and not all(
            value is not None for value in trust_values
        ):
            raise ProductCompletionError(
                "whole-product evidence trust inputs must be supplied together"
            )
        evidence_context = None
        if all(value is not None for value in trust_values):
            if not args.evidence_store.is_dir():
                raise ProductCompletionError(
                    "whole-product evidence store must be an existing directory"
                )
            try:
                policy = parse_qualification_trust_policy(
                    _load(args.qualification_policy, name="qualification trust policy")
                )
                evidence_context = WholeProductEvidenceContext(
                    evidence_store=ArtifactStore(args.evidence_store),
                    policy=policy,
                    expected_policy_id=args.expected_policy_id,
                    expected_policy_version=args.expected_policy_version,
                )
            except (QualificationTrustError, TypeError, ValueError) as error:
                raise ProductCompletionError(
                    "whole-product evidence trust inputs are invalid"
                ) from error
        report = evaluate_completion(
            _load(args.bank, name="work-package bank"),
            qualification,
            _load(args.nvda_status, name="NVDA status"),
            spec_text=_load_text(args.spec, name="canonical product spec"),
            # Exact source must come from the invoking checkout/workflow boundary.
            # qualification.json cannot self-assert the revision it qualifies.
            exact_source_sha=args.source_sha,
            evidence_context=evidence_context,
        )
    except ProductCompletionError as error:
        print(str(error), file=sys.stderr)
        return 2

    print(json.dumps(report, sort_keys=True))
    if args.require_complete and not report["complete"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
