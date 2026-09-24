"""One local command for the currently implemented bootstrap checks."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "research") + os.pathsep + str(ROOT)
commands = [
    [sys.executable, "tools/baseline.py", "check"],
    *[[sys.executable, "-m", "unittest", "discover", "-s", folder, "-v"] for folder in ("tests/Contracts", "tests/Control", "research/tests", "mvp/tests")],
]
for command in commands:
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode:
        raise SystemExit(result.returncode)
print("Repository and simulated AutoTrade MVP checks passed. Windows/.NET/provider/NVDA qualification is separate.")
