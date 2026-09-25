"""Fail-closed whole-product completion audit for canonical AutoTrade evidence.

This tool grants no financial or release authority.  Completion requires the
whole approved product surface, all 65 work packages, all canonical
qualification gates, exact-source evidence, and real NVDA qualification on the
same source revision.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
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


def _evidence_matrix(
    qualification: dict[str, Any],
    *,
    exact_source_sha: str | None,
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
    return missing_sections, missing_packages, nonpassing


def evaluate_completion(
    bank: dict[str, Any],
    qualification: dict[str, Any],
    nvda_status: dict[str, Any],
    *,
    spec_text: str,
    exact_source_sha: str | None,
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
    nonterminal_gates = {
        str(name): value
        for name, value in sorted(gates.items())
        if value != TERMINAL_GATE_STATUS
    }

    source_sha = _exact_source(exact_source_sha)
    missing_sections, missing_evidence_packages, nonpassing_evidence = _evidence_matrix(
        qualification,
        exact_source_sha=source_sha,
    )
    overall_status = qualification.get("overall_status")
    nvda_qualified = nvda_status.get("qualified") is True
    nvda_source_matches = (
        source_sha is not None and nvda_status.get("source_sha") == source_sha
    )

    blockers: list[str] = []
    if source_sha is None:
        blockers.append("exact source SHA is missing or non-canonical")
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
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()

    try:
        qualification = _load(args.qualification, name="qualification")
        report = evaluate_completion(
            _load(args.bank, name="work-package bank"),
            qualification,
            _load(args.nvda_status, name="NVDA status"),
            spec_text=_load_text(args.spec, name="canonical product spec"),
            exact_source_sha=args.source_sha or qualification.get("source_sha"),
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
