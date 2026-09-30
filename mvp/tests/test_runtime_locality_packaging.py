from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.local_filesystem import LocalFilesystemQualificationError
from research.autotrade_research.artifacts import resource_lock


ROOT = Path(__file__).resolve().parents[2]


class ProductionRuntimeFilesystemPackagingTests(unittest.TestCase):
    def test_journal_store_runs_from_production_staging_without_research_tree(self):
        with TemporaryDirectory() as directory:
            staging = Path(directory) / "installed"
            (staging / "mvp").mkdir(parents=True)
            shutil.copy2(ROOT / "mvp" / "__init__.py", staging / "mvp" / "__init__.py")
            shutil.copytree(
                ROOT / "mvp" / "autotrade_mvp",
                staging / "mvp" / "autotrade_mvp",
            )
            shutil.copytree(
                ROOT / "autotrade_runtime",
                staging / "autotrade_runtime",
            )
            self.assertFalse((staging / "research").exists())
            self.assertFalse((staging / "autotrade_local_filesystem.py").exists())

            script = """
from pathlib import Path
from tempfile import TemporaryDirectory
from mvp.autotrade_mvp.persistence import JournalStore

with TemporaryDirectory() as directory:
    path = Path(directory) / 'journal.sqlite3'
    store = JournalStore(path)
    assert path.is_file()
    assert store.store_identity.canonical_path == str(path.resolve())
print('STAGED_JOURNAL_OK')
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(staging)
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
                    "isolated production staging failed:\n"
                    + completed.stdout
                    + completed.stderr
                ),
            )
            self.assertEqual(completed.stdout.strip(), "STAGED_JOURNAL_OK")

    def test_resource_lock_delegates_locality_to_production_runtime_authority(self):
        failure = LocalFilesystemQualificationError("remote path")
        with patch.object(
            resource_lock,
            "require_qualified_local_filesystem_path",
            side_effect=failure,
        ) as qualify:
            with self.assertRaisesRegex(
                resource_lock.ResourceLockError,
                "qualified local filesystem",
            ) as raised:
                resource_lock._reject_known_remote_lock_path(Path("lock.file"))
        qualify.assert_called_once_with(Path("lock.file"))
        self.assertIs(raised.exception.__cause__, failure)


if __name__ == "__main__":
    unittest.main()
