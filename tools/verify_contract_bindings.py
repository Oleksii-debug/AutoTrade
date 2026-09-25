"""Cross-language executable conformance for the canonical common-scalar corpus.

This is a contract-compatibility gate only. It grants no financial authority.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.bindings.python.common_scalars import (  # noqa: E402
    CONTRACT_VERSION,
    is_valid_common_scalar,
)

CORPUS = ROOT / "contracts" / "fixtures" / "common-scalars.corpus.json"
MANIFEST = ROOT / "contracts" / "manifest.json"
NODE_RUNNER = ROOT / "tests" / "Contracts" / "common_scalar_conformance.js"
CSHARP_PROJECT = (
    ROOT
    / "tests"
    / "Contracts"
    / "AutoTrade.Contracts.Conformance"
    / "AutoTrade.Contracts.Conformance.csproj"
)


def _python_conformance() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = manifest["contract_version"]
    if CONTRACT_VERSION != version:
        raise RuntimeError(
            f"Python binding version {CONTRACT_VERSION} != manifest {version}"
        )
    if corpus["contract_version"] != version:
        raise RuntimeError(
            "common-scalar corpus contract_version does not match manifest"
        )

    names: set[str] = set()
    for case in corpus["cases"]:
        name = case["name"]
        if name in names:
            raise RuntimeError(f"duplicate corpus case: {name}")
        names.add(name)
        actual = is_valid_common_scalar(case["type"], case["value"])
        if actual is not case["expected"]:
            raise RuntimeError(
                f"Python verdict mismatch for {name}: "
                f"expected {case['expected']}, got {actual}"
            )
    print(f"Python common-scalar conformance passed ({len(names)} cases).")


def _run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    _python_conformance()
    _run(
        "node",
        str(NODE_RUNNER.relative_to(ROOT)),
        str(CORPUS.relative_to(ROOT)),
        str(MANIFEST.relative_to(ROOT)),
    )
    _run(
        "dotnet",
        "run",
        "--project",
        str(CSHARP_PROJECT.relative_to(ROOT)),
        "--configuration",
        "Release",
        "--",
        str(CORPUS.relative_to(ROOT)),
        str(MANIFEST.relative_to(ROOT)),
    )
    print(
        "Cross-language common-scalar conformance passed for "
        "Python, TypeScript, and C#."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
