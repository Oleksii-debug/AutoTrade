"""One local command for the currently implemented bootstrap checks."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def repository_python_test_dirs(root: Path = ROOT) -> tuple[str, ...]:
    tests_root = root / "tests"
    if not tests_root.is_dir():
        return ()
    return tuple(
        sorted(
            {
                path.parent.relative_to(root).as_posix()
                for path in tests_root.rglob("test_*.py")
                if path.is_file()
            }
        )
    )


PYTHON_TEST_DIRS = (
    *repository_python_test_dirs(),
    "research/tests",
    "mvp/tests",
)


def verification_commands() -> tuple[tuple[str, ...], ...]:
    return (
        (sys.executable, "tools/baseline.py", "check"),
        (sys.executable, "tools/build_provenance_manifest.py", "--check"),
        (sys.executable, "tools/check_nvda_qualification.py", "--check-status"),
        *tuple(
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                folder,
                "-v",
            )
            for folder in PYTHON_TEST_DIRS
        ),
        (
            "dotnet",
            "run",
            "--project",
            "tests/Contracts.DotNet/Contracts.DotNet.csproj",
            "--configuration",
            "Release",
            "--",
            "contracts/fixtures/common-scalars.corpus.json",
        ),
        (
            "dotnet",
            "run",
            "--project",
            "tests/Desktop.Client/Desktop.Client.csproj",
            "--configuration",
            "Release",
        ),
        (
            "dotnet",
            "run",
            "--project",
            "tests/Integration/LeanAdoption/LeanAdoptionProbe.csproj",
            "--configuration",
            "Release",
        ),
    )


def main() -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "research") + os.pathsep + str(ROOT)
    for command in verification_commands():
        result = subprocess.run(command, cwd=ROOT, env=env)
        if result.returncode:
            return result.returncode
    print(
        "Repository Python, .NET contracts, desktop client, LEAN probe, simulated MVP, "
        "observability and baseline checks passed. Provider and real NVDA qualification "
        "remain separate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
