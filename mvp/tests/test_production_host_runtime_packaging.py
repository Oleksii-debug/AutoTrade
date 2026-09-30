from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime import resource_lock as runtime_resource_lock
from autotrade_runtime import strict_json as runtime_strict_json
from research.autotrade_research.artifacts import resource_lock as research_resource_lock
from research.autotrade_research.io import strict_json as research_strict_json


ROOT = Path(__file__).resolve().parents[2]


class ProductionHostRuntimePackagingTests(unittest.TestCase):
    def test_research_compatibility_names_resolve_to_production_authorities(self):
        self.assertIs(research_resource_lock, runtime_resource_lock)
        self.assertIs(research_strict_json, runtime_strict_json)

    def test_host_config_and_instance_fence_run_without_research_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "installed"
            (staging / "mvp").mkdir(parents=True)
            shutil.copy2(ROOT / "mvp" / "__init__.py", staging / "mvp" / "__init__.py")
            shutil.copytree(
                ROOT / "mvp" / "autotrade_mvp",
                staging / "mvp" / "autotrade_mvp",
            )
            shutil.copytree(ROOT / "autotrade_runtime", staging / "autotrade_runtime")
            self.assertFalse((staging / "research").exists())

            journal = root / "journal.sqlite3"
            config_payload = json.dumps(
                {
                    "journal_path": str(journal.resolve()),
                    "account_id": "paper-account",
                    "environment": "PAPER",
                    "host_id": "host-a",
                    "bind_host": "127.0.0.1",
                    "bind_port": 8765,
                    "public_origin": "http://127.0.0.1:8765",
                },
                separators=(",", ":"),
            )
            script = """
import os
from pathlib import Path
from mvp.autotrade_mvp.production_host import (
    _InstanceFence,
    parse_production_host_config,
)

payload = os.environ['AUTOTRADE_HOST_CONFIG'].encode('utf-8')
config = parse_production_host_config(payload)
fence = _InstanceFence.acquire(config.journal_path)
try:
    assert fence.path == Path(str(config.journal_path) + '.host.lock')
finally:
    fence.release()
print('STAGED_HOST_OK')
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(staging)
            environment["AUTOTRADE_HOST_CONFIG"] = config_payload
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=staging,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=(
                    "isolated production host staging failed:\n"
                    + completed.stdout
                    + completed.stderr
                ),
            )
            self.assertEqual(completed.stdout.strip(), "STAGED_HOST_OK")


if __name__ == "__main__":
    unittest.main()
