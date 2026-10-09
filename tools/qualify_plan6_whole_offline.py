"""Plan 6 Section 6: offline-only whole-provider engineering qualification.

Executes existing Section 1-5 suites; does not create a qualification issuer,
provider account, signed trust root, trading sender, or financial truth.
A receipt is emitted ONLY after every child suite really exited successfully.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.plan6_offline_sections import SECTION_MODULES
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SECTIONS = (1, 2, 3, 4, 5)
_EXTERNAL_PENDING = "EXTERNAL_ACTIVATION_PENDING"


class Plan6WholeQualificationError(ValueError):
    """Fail closed without emitting an OFFLINE_SUPPORTED qualification."""


def verify_source_sha(*, expected: str, actual: str) -> str:
    if type(expected) is not str or _SHA.fullmatch(expected) is None:
        raise Plan6WholeQualificationError("missing/invalid exact source SHA")
    if type(actual) is not str or actual != expected:
        raise Plan6WholeQualificationError("checkout/source SHA mismatch")
    return expected


def read_section_receipt(section: int, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Return a non-secret result only on actual exit-zero, exact schema/coverage."""
    if type(section) is not int or section not in _SECTIONS:
        raise Plan6WholeQualificationError("unsupported Section")
    if result.returncode != 0:
        raise Plan6WholeQualificationError(f"Section {section} executable test suite FAILED")
    # The runner writes precisely one machine-readable final stdout line after
    # unittest finishes. A test's own intermediate output is not authority.
    lines = result.stdout.splitlines()
    if not lines:
        raise Plan6WholeQualificationError("Section missing executable receipt")
    try:
        receipt = json.loads(lines[-1])
    except (ValueError, TypeError) as error:
        raise Plan6WholeQualificationError("Section missing valid final receipt") from error
    if type(receipt) is not dict:
        raise Plan6WholeQualificationError("Section receipt must be an object")
    modules = list(dict.fromkeys(SECTION_MODULES[section]))
    count = receipt.get("test_count")
    conditions = (
        receipt.get("schema_version") == "plan6-offline-suite.v1",
        type(receipt.get("section")) is int and receipt["section"] == section,
        receipt.get("status") == "PASS",
        type(count) is int and count >= len(modules),
        receipt.get("test_modules") == modules,
        receipt.get("network") == "DENIED",
        receipt.get("provider_credentials") == "ABSENT",
        receipt.get("provider_account_activation") == "NOT_CLAIMED",
        receipt.get("paper_live") == "NOT_CLAIMED",
    )
    if not all(conditions):
        raise Plan6WholeQualificationError(f"Section {section} receipt scope/guard mismatch")
    return {"section": section, "test_count": count, "status": "PASS"}


def capability_matrix() -> dict[str, str]:
    """Informational source/fixture gates ONLY: never runtime account authority."""
    return {
        "provider_domain_account_capability_contracts": "OFFLINE_SUPPORTED",
        "signed_transport_reconciliation_and_recovery": "OFFLINE_SUPPORTED",
        "provider_family_fixtures": "OFFLINE_SUPPORTED",
        "financial_domain_adapter_fixtures": "OFFLINE_SUPPORTED",
        "offline_qualification_harness": "OFFLINE_SUPPORTED",
        "authenticated_real_provider_account": _EXTERNAL_PENDING,
        "provider_issued_account_entitlements": _EXTERNAL_PENDING,
        "real_provider_network_qualification": _EXTERNAL_PENDING,
        "paper_or_live_trading_campaign": _EXTERNAL_PENDING,
        "provider_backed_financial_truth": _EXTERNAL_PENDING,
        "signed_release_and_physical_nvda": "NOT_CLAIMED",
    }


def qualify_sections(source_sha: str) -> dict[str, object]:
    git = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    verify_source_sha(expected=source_sha, actual=git.stdout.strip())
    receipts = []
    for section in _SECTIONS:
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "plan6_offline_sections.py"),
             "--section", str(section)],
            cwd=ROOT, capture_output=True, text=True,
            check=False,
        )
        receipts.append(read_section_receipt(section, result))
    return {
        "schema_version": "plan6-whole-offline.v1",
        "source_sha": source_sha,
        "evidence_class": "SOURCE_FIXTURE_TEST",
        "whole_offline_status": "PASS",
        "section_results": receipts,
        "capability_matrix": capability_matrix(),
        "provider_account_activation": _EXTERNAL_PENDING,
        "paper_live": "NOT_CLAIMED",
        "real_money": "NOT_CLAIMED",
        "final_release": "NOT_CLAIMED",
    }


def publish_receipt(path: Path, receipt: dict[str, object]) -> None:
    """Atomic receipt publication on the same filesystem, after full PASS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            prefix=".plan6-whole-", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(receipt, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = os.environ.get("AUTOTRADE_SOURCE_SHA", "")
    try:
        args.output.unlink(missing_ok=True)  # stale artifacts never claim PASS
        receipt = qualify_sections(expected)
        publish_receipt(args.output, receipt)
    except (Plan6WholeQualificationError, subprocess.CalledProcessError, OSError) as error:
        # Never print subprocess stdout/stderr: secret-shaped data in a broken
        # test must never be promoted into a public CI log/artifact.
        print(f"PLAN6_WHOLE_OFFLINE_FAIL_CLOSED: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
