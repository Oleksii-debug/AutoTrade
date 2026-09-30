from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_foundation.local_filesystem import LocalFilesystemQualificationError
from research.autotrade_research.artifacts import resource_lock
from tools.build_windows_bundle import build_bundle
from tools.build_windows_install_manifest import build_installer_input_manifest
from tools.stage_windows_foundation import stage_windows_foundation


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHA = "a" * 40


def _isolated_python(*, staging: Path, script: str, extra_env: dict[str, str] | None = None):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["AUTOTRADE_STAGING"] = str(staging)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=staging,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _component(path: Path, staging: Path, *, kind: str) -> dict[str, str]:
    relative = path.relative_to(staging).as_posix()
    return {
        "component_id": relative.replace("/", "-"),
        "kind": kind,
        "path": relative,
        "version": "1.0.0",
        "sha256": "sha256:" + sha256(path.read_bytes()).hexdigest(),
    }


class ProductionFoundationPackagingTests(unittest.TestCase):
    def test_journal_store_runs_from_hermetic_staging_without_research(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "installed"
            (staging / "mvp").mkdir(parents=True)
            shutil.copy2(ROOT / "mvp" / "__init__.py", staging / "mvp" / "__init__.py")
            shutil.copytree(
                ROOT / "mvp" / "autotrade_mvp",
                staging / "mvp" / "autotrade_mvp",
            )
            shutil.copytree(
                ROOT / "autotrade_foundation",
                staging / "autotrade_foundation",
            )
            self.assertFalse((staging / "research").exists())
            self.assertFalse((staging / "autotrade_local_filesystem.py").exists())

            database = root / "journal.sqlite3"
            script = """
import os
from pathlib import Path
import sys
staging = os.environ['AUTOTRADE_STAGING']
sys.path.insert(0, staging)
from autotrade_foundation.local_filesystem import require_qualified_local_filesystem_path
from mvp.autotrade_mvp.persistence import JournalStore
path = Path(os.environ['AUTOTRADE_TEST_DB'])
require_qualified_local_filesystem_path(path)
assert not path.exists()
store = JournalStore(path)
assert path.is_file()
assert store.store_identity.canonical_path == str(path.resolve())
for name in sys.modules:
    assert name != 'research' and not name.startswith('research.')
    assert name != 'autotrade_research' and not name.startswith('autotrade_research.')
print('STAGED_JOURNAL_OK')
"""
            completed = _isolated_python(
                staging=staging,
                script=script,
                extra_env={"AUTOTRADE_TEST_DB": str(database)},
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stdout + completed.stderr,
            )
            self.assertEqual(completed.stdout.strip(), "STAGED_JOURNAL_OK")
            self.assertTrue(database.is_file())

    def test_missing_foundation_fails_before_journal_creation_without_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "installed-without-foundation"
            (staging / "mvp").mkdir(parents=True)
            shutil.copy2(ROOT / "mvp" / "__init__.py", staging / "mvp" / "__init__.py")
            shutil.copytree(
                ROOT / "mvp" / "autotrade_mvp",
                staging / "mvp" / "autotrade_mvp",
            )
            database = root / "must-not-exist.sqlite3"
            script = """
import os
import sys
sys.path.insert(0, os.environ['AUTOTRADE_STAGING'])
from mvp.autotrade_mvp.persistence import JournalStore
JournalStore(os.environ['AUTOTRADE_TEST_DB'])
"""
            completed = _isolated_python(
                staging=staging,
                script=script,
                extra_env={"AUTOTRADE_TEST_DB": str(database)},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("autotrade_foundation", completed.stderr)
            self.assertFalse(database.exists())

    def test_resource_lock_delegates_to_foundation_before_lock_file_creation(self):
        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "lock.file"
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
                    resource_lock.ResourceLock(lock_path).acquire()
            qualify.assert_called_once_with(lock_path)
            self.assertIs(raised.exception.__cause__, failure)
            self.assertFalse(lock_path.exists())

    def test_canonical_foundation_assembler_binds_bundle_and_installer_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            executable = staging / "AutoTrade.Desktop.exe"
            dependency_lock = staging / "dependency-lock.json"
            sbom = staging / "sbom.spdx.json"
            executable.write_bytes(b"desktop")
            dependency_lock.write_text(
                '{"dependencies":{"runtime":"1.0.0"}}\n', encoding="utf-8"
            )
            sbom.write_text(
                '{"SPDXID":"SPDXRef-DOCUMENT","spdxVersion":"SPDX-2.3"}\n',
                encoding="utf-8",
            )

            composition = root / "composition.json"
            composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": SOURCE_SHA,
                        "dependency_lock_sha256": "sha256:"
                        + sha256(dependency_lock.read_bytes()).hexdigest(),
                        "sbom_sha256": "sha256:" + sha256(sbom.read_bytes()).hexdigest(),
                        "schema_compatibility": {
                            "minimum": "1.0.0",
                            "maximum": "1.0.x",
                        },
                        "runtime": {
                            "architecture": "x64",
                            "runtime_identifier": "win-x64",
                            "minimum_windows_version": "10.0.22621",
                        },
                        "components": [
                            _component(executable, staging, kind="runtime"),
                            _component(dependency_lock, staging, kind="dependency-lock"),
                            _component(sbom, staging, kind="sbom"),
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            staged = stage_windows_foundation(
                staging=staging,
                composition_path=composition,
            )
            self.assertEqual(
                {item["path"] for item in staged},
                {
                    "autotrade_foundation/__init__.py",
                    "autotrade_foundation/local_filesystem.py",
                },
            )

            provenance = root / "provenance.json"
            provenance.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "source_sha": SOURCE_SHA,
                        "release_eligible": True,
                        "blocking_issues": [],
                    }
                ),
                encoding="utf-8",
            )
            bundle = root / "release.zip"
            build_bundle(
                staging=staging,
                output=bundle,
                version="1.0.0",
                source_sha=SOURCE_SHA,
                mode="release",
                provenance_path=provenance,
                composition_path=composition,
            )
            installed = build_installer_input_manifest(
                bundle=bundle,
                output=root / "installer-input.json",
                target_framework="net10.0-windows",
                runtime_mode="SELF_CONTAINED",
            )["manifest"]

            runtime_path = "autotrade_foundation/local_filesystem.py"
            source_digest = "sha256:" + sha256((ROOT / runtime_path).read_bytes()).hexdigest()
            files = {item["target_relative_path"]: item for item in installed["files"]}
            components = {item["path"]: item for item in installed["components"]}
            self.assertEqual(files[runtime_path]["sha256"], source_digest)
            self.assertEqual(components[runtime_path]["sha256"], source_digest)


if __name__ == "__main__":
    unittest.main()
