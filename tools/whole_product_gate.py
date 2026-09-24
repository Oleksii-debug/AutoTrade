"""Fail-closed whole-product completion gate for WP-60."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable


class WholeProductGateError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceRecord:
    kind: str
    requirement_id: str
    source_sha: str
    status: str
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.kind not in {"PRODUCT_SECTION", "WORK_PACKAGE"}:
            raise WholeProductGateError("unknown evidence kind")
        if self.status not in {"PASS", "BLOCKED", "INCONCLUSIVE"}:
            raise WholeProductGateError("unknown evidence status")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise WholeProductGateError("source_sha must be exact lowercase 40-char Git SHA")
        if not isinstance(self.requirement_id, str) or not self.requirement_id.strip():
            raise WholeProductGateError("requirement_id is required")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.strip():
            raise WholeProductGateError("evidence_ref is required")


@dataclass(frozen=True)
class WholeProductVerdict:
    status: str
    missing_sections: tuple[str, ...]
    missing_packages: tuple[str, ...]
    nonpassing: tuple[str, ...]


def product_section_ids(spec_text: str) -> tuple[str, ...]:
    found = []
    seen = set()
    for line in spec_text.splitlines():
        match = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", line)
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= 40 and number not in seen:
            seen.add(number)
            found.append(f"SECTION-{number:02d}")
    if found != [f"SECTION-{i:02d}" for i in range(1, 41)]:
        raise WholeProductGateError("canonical product spec must expose exactly sections 1..40")
    return tuple(found)


def work_package_ids(bank_text: str) -> tuple[str, ...]:
    try:
        bank = json.loads(bank_text)
    except json.JSONDecodeError as error:
        raise WholeProductGateError("invalid work-package bank") from error
    packages = bank.get("work_packages")
    if not isinstance(packages, list):
        raise WholeProductGateError("work_packages list missing")
    ids = tuple(item.get("id") for item in packages if isinstance(item, dict))
    expected = tuple(f"WP-{i:02d}" for i in range(1, 66))
    if ids != expected:
        raise WholeProductGateError("canonical bank must contain ordered WP-01..WP-65")
    return ids


def evaluate_whole_product(
    *,
    spec_text: str,
    bank_text: str,
    evidence: Iterable[EvidenceRecord],
    exact_source_sha: str,
) -> WholeProductVerdict:
    if not re.fullmatch(r"[0-9a-f]{40}", exact_source_sha):
        raise WholeProductGateError("exact_source_sha must be exact lowercase Git SHA")
    sections = product_section_ids(spec_text)
    packages = work_package_ids(bank_text)
    required = {("PRODUCT_SECTION", x) for x in sections} | {
        ("WORK_PACKAGE", x) for x in packages
    }
    by_key = {}
    for item in evidence:
        if not isinstance(item, EvidenceRecord):
            raise WholeProductGateError("evidence entries must be EvidenceRecord")
        key = (item.kind, item.requirement_id)
        if key not in required:
            raise WholeProductGateError("evidence references unknown requirement")
        if key in by_key:
            raise WholeProductGateError("duplicate requirement evidence")
        by_key[key] = item

    missing_sections = tuple(
        x for x in sections if ("PRODUCT_SECTION", x) not in by_key
    )
    missing_packages = tuple(x for x in packages if ("WORK_PACKAGE", x) not in by_key)
    nonpassing = tuple(
        f"{kind}:{rid}"
        for (kind, rid), item in sorted(by_key.items())
        if item.source_sha != exact_source_sha or item.status != "PASS"
    )

    status = "COMPLETE"
    if missing_sections or missing_packages or nonpassing:
        status = "INCOMPLETE"

    return WholeProductVerdict(
        status=status,
        missing_sections=missing_sections,
        missing_packages=missing_packages,
        nonpassing=nonpassing,
    )


def load_canonical(root: Path) -> tuple[str, str]:
    return (
        (root / "docs/product/PRODUCT_SPEC_CANONICAL.txt").read_text(encoding="utf-8"),
        (root / "control/work-packages/bank.json").read_text(encoding="utf-8"),
    )
