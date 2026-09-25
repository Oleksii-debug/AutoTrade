"""One local command for the currently implemented bootstrap checks."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON_TEST_SUITES = (
    "tests/Contracts",
    "tests/Control",
    "tests/Provenance",
    "tests/Observability",
    "research/tests",
    "mvp/tests",
)


def verification_commands() -> list[list[str]]:
    return [
        [sys.executable, "tools/baseline.py", "check"],
        [sys.executable, "tools/check_nvda_qualification.py", "--check-status"],
        *[
            [sys.executable, "-m", "unittest", "discover", "-s", folder, "-v"]
            for folder in PYTHON_TEST_SUITES
        ],
    ]


def main() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "research") + os.pathsep + str(ROOT)
    for command in verification_commands():
        result = subprocess.run(command, cwd=ROOT, env=env)
        if result.returncode:
            raise SystemExit(result.returncode)
    print(
        "Repository and simulated AutoTrade MVP checks passed. "
        "Windows/.NET/provider/NVDA qualification is separate."
    )


if __name__ == "__main__":
    main()
