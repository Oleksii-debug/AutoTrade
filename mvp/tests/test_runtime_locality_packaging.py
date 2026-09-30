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
from tools.stage_windows_foundation import FoundationStagingError, stage_windows_foundation


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


@unittest.skipUnless(sys.platform == "win32", "native Windows junction and hardlink acceptance")
class WindowsFoundationNoReparseTests(unittest.TestCase):
    """Exact committed source positive and six native Windows no-alias failures."""

    def _fixture(self, root):
        repo = root / "tracked"
        package = repo / "autotrade_foundation"
        package.mkdir(parents=True)
        for filename in ("__init__.py", "local_filesystem.py"):
            shutil.copy2(ROOT / "autotrade_foundation" / filename, package / filename)
        for command in (
            ("git", "-C", str(repo), "init", "-q"),
            ("git", "-C", str(repo), "add", "--",
             "autotrade_foundation/__init__.py", "autotrade_foundation/local_filesystem.py"),
            ("git", "-C", str(repo), "-c", "user.name=Fixture",
             "-c", "user.email=fixture@example.invalid", "commit", "-qm", "committed bytes"),
        ):
            p = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        sha = subprocess.check_output(("git", "-C", str(repo), "rev-parse", "HEAD"), text=True).strip()
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        staging = root / "stage"
        composition = root / "composition.json"
        composition.write_text(json.dumps({
            "schema_version": "1.0.0", "product": "AutoTrade",
            "source_sha": sha, "components": []}), encoding="utf-8")
        return repo, staging, composition

    def _junction(self, alias, real):
        p = subprocess.run(
            ("cmd.exe", "/d", "/c", f'mklink /J "{alias}" "{real}"'),
            text=True, capture_output=True, check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(alias.is_dir())

    def _deny(self, source, stage, composition):
        before = composition.read_bytes()
        with self.assertRaises(FoundationStagingError):
            stage_windows_foundation(staging=stage, composition_path=composition, source_root=source)
        self.assertEqual(composition.read_bytes(), before)

    def test_exact_committed_source_and_idempotent_restage(self):
        with TemporaryDirectory() as directory:
            source, stage, composition = self._fixture(Path(directory))
            stage.mkdir()
            result = stage_windows_foundation(staging=stage, composition_path=composition, source_root=source)
            self.assertEqual(len(result), 2)
            first = composition.read_bytes()
            self.assertEqual(stage_windows_foundation(staging=stage, composition_path=composition, source_root=source), result)
            self.assertEqual(first, composition.read_bytes())
            self.assertEqual((stage / "autotrade_foundation" / "local_filesystem.py").read_bytes(),
                             (source / "autotrade_foundation" / "local_filesystem.py").read_bytes())

    def test_source_junction_cannot_counterfeit_committed_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            stage.mkdir()
            outside = root / "external-source"
            shutil.copytree(source / "autotrade_foundation", outside)
            shutil.rmtree(source / "autotrade_foundation")
            self._junction(source / "autotrade_foundation", outside)
            self._deny(source, stage, composition)
            self.assertFalse((stage / "autotrade_foundation").exists())

    def test_destination_junction_cannot_redirect_external_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            stage.mkdir()
            outside = root / "external-destination"
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"stable")
            self._junction(stage / "autotrade_foundation", outside)
            self._deny(source, stage, composition)
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel"])
            self.assertEqual((outside / "sentinel").read_bytes(), b"stable")

    def test_staging_root_junction_is_rejected_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            outside = root / "external-root"
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"stable")
            self._junction(stage, outside)
            self._deny(source, stage, composition)
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel"])

    def test_equal_byte_hardlink_leaf_is_not_an_authoritative_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            (stage / "autotrade_foundation").mkdir(parents=True)
            content = (source / "autotrade_foundation" / "local_filesystem.py").read_bytes()
            outside = root / "external-leaf.py"
            outside.write_bytes(content)
            os.link(outside, stage / "autotrade_foundation" / "local_filesystem.py")
            self._deny(source, stage, composition)
            self.assertEqual(outside.read_bytes(), content)
            self.assertFalse((stage / "autotrade_foundation" / "__init__.py").exists())

    def test_hardlinked_authority_manifest_is_rejected_before_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            stage.mkdir()
            external = root / "external-composition.json"
            composition.replace(external)
            os.link(external, composition)
            first = external.read_bytes()
            self._deny(source, stage, composition)
            self.assertEqual(external.read_bytes(), first)
            self.assertFalse((stage / "autotrade_foundation").exists())

    def test_second_component_conflict_preflights_before_first_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            stage.mkdir()
            components = [
                ("autotrade-foundation-package", "runtime-foundation", "__init__.py"),
                ("autotrade-foundation-local-filesystem", "wrong-kind", "local_filesystem.py"),
            ]
            record = json.loads(composition.read_text(encoding="utf-8"))
            record["components"] = [{
                "component_id": component_id, "kind": kind,
                "path": "autotrade_foundation/" + name,
                "version": "source-controlled",
                "sha256": "sha256:" + sha256(
                    (source / "autotrade_foundation" / name).read_bytes()).hexdigest()
            } for component_id, kind, name in components]
            composition.write_text(json.dumps(record), encoding="utf-8")
            self._deny(source, stage, composition)
            self.assertFalse((stage / "autotrade_foundation").exists())


if __name__ == "__main__":
    unittest.main()
