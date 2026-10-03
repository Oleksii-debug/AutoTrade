from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


class RuntimeTargetHostChronologyBinderSealedTests(unittest.TestCase):
    def test_plan_bound_import_installs_sealed_terminal_dispatcher_before_return(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification as plan_bound

verifier = plan_bound.verify_declared_plan_runtime_target_host_qualification
if not verifier.__module__.endswith("runtime_target_host_chronology_bound_qualification"):
    raise AssertionError(
        "plan-bound import returned before the sealed chronology dispatcher was installed: "
        + verifier.__module__
    )
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=(
                "fresh-process terminal dispatcher sealing regression failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
