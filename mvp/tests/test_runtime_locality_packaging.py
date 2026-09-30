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

from autotrade_runtime.local_filesystem import LocalFilesystemQualificationError
from research.autotrade_research.artifacts import resource_lock
from tools.build_windows_bundle import build_bundle
from tools.build_windows_install_manifest import build_installer_input_manifest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHA = "a" * 40


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

    def test_release_and_installer_inventory_bind_production_locality_module(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            (staging / "AutoTrade.Desktop.exe").write_bytes(b"desktop")
            (staging / "dependency-lock.json").write_text(
                '{"dependencies":{"runtime":"1.0.0"}}\n',
                encoding="utf-8",
            )
            (staging / "sbom.spdx.json").write_text(
                '{"SPDXID":"SPDXRef-DOCUMENT","spdxVersion":"SPDX-2.3"}\n',
                encoding="utf-8",
            )
            runtime_dir = staging / "autotrade_runtime"
            runtime_dir.mkdir()
            for name in ("__init__.py", "local_filesystem.py"):
                shutil.copy2(ROOT / "autotrade_runtime" / name, runtime_dir / name)

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

            components = []
            for path in sorted(staging.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(staging).as_posix()
                if relative == "dependency-lock.json":
                    kind = "dependency-lock"
                elif relative == "sbom.spdx.json":
                    kind = "sbom"
                elif relative.endswith(".exe"):
                    kind = "runtime"
                else:
                    kind = "asset"
                components.append(
                    {
                        "component_id": relative.replace("/", "-"),
                        "kind": kind,
                        "path": relative,
                        "version": "1.0.0",
                        "sha256": "sha256:" + sha256(path.read_bytes()).hexdigest(),
                    }
                )
            composition = root / "composition.json"
            dependency_lock = staging / "dependency-lock.json"
            sbom = staging / "sbom.spdx.json"
            composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": SOURCE_SHA,
                        "dependency_lock_sha256": "sha256:"
                        + sha256(dependency_lock.read_bytes()).hexdigest(),
                        "sbom_sha256": "sha256:"
                        + sha256(sbom.read_bytes()).hexdigest(),
                        "schema_compatibility": {
                            "minimum": "1.0.0",
                            "maximum": "1.0.x",
                        },
                        "runtime": {
                            "architecture": "x64",
                            "runtime_identifier": "win-x64",
                            "minimum_windows_version": "10.0.22621",
                        },
                        "components": components,
                    },
                    sort_keys=True,
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

            runtime_path = "autotrade_runtime/local_filesystem.py"
            source_digest = "sha256:" + sha256(
                (ROOT / runtime_path).read_bytes()
            ).hexdigest()
            files = {
                item["target_relative_path"]: item
                for item in installed["files"]
            }
            components_by_path = {
                item["path"]: item
                for item in installed["components"]
            }
            self.assertIn(runtime_path, files)
            self.assertEqual(files[runtime_path]["sha256"], source_digest)
            self.assertIn(runtime_path, components_by_path)
            self.assertEqual(
                components_by_path[runtime_path]["sha256"],
                source_digest,
            )


if __name__ == "__main__":
    unittest.main()
