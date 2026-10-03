from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


class RuntimeTargetHostChronologyBinderSealedTests(unittest.TestCase):
    def test_plan_bound_import_seals_terminal_binder_before_external_prebind(self):
        repo_root = Path(__file__).resolve().parents[2]
        script = """
import mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification as plan_bound


def forged_terminal_verifier(*args, **kwargs):
    return object()


try:
    plan_bound._bind_terminal_chronology_verifier(forged_terminal_verifier)
except RuntimeError as error:
    if "already bound" not in str(error):
        raise
else:
    raise AssertionError(
        "terminal chronology binder remained externally pre-bindable after module import"
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
                "fresh-process terminal binder sealing regression failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
