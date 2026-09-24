"""One local command for the currently implemented bootstrap checks."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON_TEST_DIRS = (
    "tests/Contracts",
    "tests/Control",
    "tests/Observability",
    "tests/Provenance",
    "research/tests",
    "mvp/tests",
)

env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "research") + os.pathsep + str(ROOT)
commands = [
    [sys.executable, "tools/baseline.py", "check"],
    [sys.executable, "tools/check_nvda_qualification.py", "--check-status"],
    *[
        [sys.executable, "-m", "unittest", "discover", "-s", folder, "-v"]
        for folder in PYTHON_TEST_DIRS
    ],
]
for command in commands:
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)
print(
    "Repository Python, simulated MVP, observability and baseline checks passed. "
    "Windows/.NET/provider/NVDA qualification is separate."
)
