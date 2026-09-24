"""Fail-closed whole-product completion audit for the canonical AutoTrade control files.

This tool grants no financial or release authority. It only refuses to call the
product complete unless the architecture bank, qualification gates, and real
NVDA status all carry explicit terminal evidence states.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BANK = ROOT / "control" / "work-packages" / "bank.json"
DEFAULT_QUALIFICATION = ROOT / "control" / "qualification.json"
DEFAULT_NVDA_STATUS = ROOT / "qualification" / "nvda" / "status.json"

EXPECTED_PACKAGE_IDS = tuple(f"WP-{index:02d}" for index in range(1, 66))
TERMINAL_PACKAGE_STATUS = "DONE"
TERMINAL_OVERALL_STATUS = "FULL_PRODUCT_QUALIFIED"
TERMINAL_GATE_STATUS = "QUALIFIED"


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


def evaluate_completion(
    bank: dict[str, Any],
    qualification: dict[str, Any],
    nvda_status: dict[str, Any],
) -> dict[str, Any]:
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
            f"work-package bank must contain exactly WP-01..WP-65; missing={missing}; extra={extra}"
        )

    incomplete_packages = tuple(
        package_id
        for package_id in EXPECTED_PACKAGE_IDS
        if by_id[package_id].get("proposed_status") != TERMINAL_PACKAGE_STATUS
    )

    gates = qualification.get("gates")
    if type(gates) is not dict or not gates:
        raise ProductCompletionError("qualification gates must be a non-empty object")
    nonterminal_gates = {
        str(name): value
        for name, value in sorted(gates.items())
        if value != TERMINAL_GATE_STATUS
    }

    overall_status = qualification.get("overall_status")
    nvda_qualified = nvda_status.get("qualified") is True

    blockers: list[str] = []
    if incomplete_packages:
        blockers.append(
            f"{len(incomplete_packages)} work packages are not {TERMINAL_PACKAGE_STATUS}"
        )
    if overall_status != TERMINAL_OVERALL_STATUS:
        blockers.append(
            "overall qualification is not FULL_PRODUCT_QUALIFIED"
        )
    if nonterminal_gates:
        blockers.append(
            f"{len(nonterminal_gates)} qualification gates are not QUALIFIED"
        )
    if not nvda_qualified:
        blockers.append("real NVDA release qualification is not complete")

    return {
        "schema_version": "1.0.0",
        "complete": not blockers,
        "package_count": len(by_id),
        "terminal_package_status": TERMINAL_PACKAGE_STATUS,
        "incomplete_packages": list(incomplete_packages),
        "overall_status": overall_status,
        "required_overall_status": TERMINAL_OVERALL_STATUS,
        "nonterminal_gates": nonterminal_gates,
        "nvda_qualified": nvda_qualified,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bank", type=Path, default=DEFAULT_BANK)
    parser.add_argument("--qualification", type=Path, default=DEFAULT_QUALIFICATION)
    parser.add_argument("--nvda-status", type=Path, default=DEFAULT_NVDA_STATUS)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()

    try:
        report = evaluate_completion(
            _load(args.bank, name="work-package bank"),
            _load(args.qualification, name="qualification"),
            _load(args.nvda_status, name="NVDA status"),
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
