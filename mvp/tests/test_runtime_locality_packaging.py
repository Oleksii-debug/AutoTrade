from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from contextlib import contextmanager
import unittest
from unittest.mock import patch

import autotrade_foundation.local_filesystem as local_filesystem_module
from autotrade_foundation.local_filesystem import LocalFilesystemQualificationError
import autotrade_foundation.windows_namespace as windows_namespace_module
from autotrade_foundation.windows_namespace import (
    retain_windows_directory_namespace,
    retain_windows_parent_namespace,
    serialize_windows_directory_publication,
)
from research.autotrade_research.artifacts import resource_lock
from tools.build_windows_bundle import build_bundle
from tools.build_windows_install_manifest import build_installer_input_manifest
from mvp.autotrade_mvp.persistence import JournalStore
import tools.stage_windows_foundation as staging_module
from tools.stage_windows_foundation import FoundationStagingError, stage_windows_foundation


ROOT = Path(__file__).resolve().parents[2]
SOURCE_SHA = subprocess.check_output(("git", "rev-parse", "--verify", "HEAD"), cwd=ROOT, text=True).strip()
if len(SOURCE_SHA) != 40 or any(ch not in "0123456789abcdef" for ch in SOURCE_SHA):
    raise RuntimeError("integration oracle requires an exact checked-out Git commit")


def _isolated_python(*, staging: Path, script: str, extra_env: dict[str, str] | None = None):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["AUTOTRADE_STAGING"] = str(staging)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
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
            shutil.copytree(
                ROOT / "autotrade_numeric",
                staging / "autotrade_numeric",
            )
            self.assertFalse((staging / "research").exists())
            self.assertFalse((staging / "autotrade_local_filesystem.py").exists())

            database = root / "journal.sqlite3"
            script = """
import os
from pathlib import Path
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
staging = Path(os.environ['AUTOTRADE_STAGING']).resolve(strict=True)
sys.path.insert(0, str(staging))
from autotrade_foundation.local_filesystem import require_qualified_local_filesystem_path
from autotrade_numeric.exact_decimal import parse_bounded_exact_decimal
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.store_identity import (
    observe_database_identity,
    same_journal_backing_object,
)
assert str(parse_bounded_exact_decimal('1.25')) == '1.25'
path = Path(os.environ['AUTOTRADE_TEST_DB'])
require_qualified_local_filesystem_path(path)
assert not path.exists()
for name, module in tuple(sys.modules.items()):
    if name in (
        'mvp', 'mvp.autotrade_mvp', 'autotrade_foundation', 'autotrade_numeric'
    ) or name.startswith(
        ('mvp.autotrade_mvp.', 'autotrade_foundation.', 'autotrade_numeric.')
    ):
        module_file = Path(module.__file__).resolve(strict=True)
        assert module_file.is_relative_to(staging), (name, module_file)
    assert name != 'research' and not name.startswith('research.')
    assert name != 'autotrade_research' and not name.startswith('autotrade_research.')
store = JournalStore(path)
assert path.is_file()
identity = store.store_identity
if os.name == 'nt':
    assert identity.identity_source == 'windows_by_handle'
    assert same_journal_backing_object(
        identity,
        observe_database_identity(path),
    )
else:
    assert identity.canonical_path == str(path.resolve())
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
            shutil.copytree(
                ROOT / "autotrade_numeric",
                staging / "autotrade_numeric",
            )
            database = root / "must-not-exist.sqlite3"
            script = """
import os
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
sys.path.insert(0, os.environ['AUTOTRADE_STAGING'])
try:
    from mvp.autotrade_mvp.persistence import JournalStore
except ModuleNotFoundError as error:
    assert error.name == 'autotrade_foundation', error.name
    print('MISSING_FOUNDATION_DENIED')
else:
    raise AssertionError('installed product imported without mandatory foundation')
"""
            completed = _isolated_python(
                staging=staging,
                script=script,
                extra_env={"AUTOTRADE_TEST_DB": str(database)},
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(completed.stdout.strip(), "MISSING_FOUNDATION_DENIED")
            self.assertFalse(database.exists())

    def test_missing_numeric_fails_before_product_numeric_import_without_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "installed-without-numeric"
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
            database = root / "must-not-exist.sqlite3"
            script = """
import os
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
sys.path.insert(0, os.environ['AUTOTRADE_STAGING'])
try:
    from mvp.autotrade_mvp.exact_decimal import parse_bounded_exact_decimal
except ModuleNotFoundError as error:
    assert error.name == 'autotrade_numeric', error.name
    print('MISSING_NUMERIC_DENIED')
else:
    raise AssertionError('installed product imported without mandatory numeric authority')
"""
            completed = _isolated_python(
                staging=staging,
                script=script,
                extra_env={"AUTOTRADE_TEST_DB": str(database)},
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(completed.stdout.strip(), "MISSING_NUMERIC_DENIED")
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
            qualify.assert_called_once_with(os.fspath(lock_path))
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
                    "autotrade_foundation/windows_namespace.py",
                    "autotrade_numeric/__init__.py",
                    "autotrade_numeric/_generated_common_scalars.py",
                    "autotrade_numeric/_generated_decimal_limits.py",
                    "autotrade_numeric/exact_decimal.py",
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

            runtime_paths = (
                "autotrade_foundation/local_filesystem.py",
                "autotrade_foundation/windows_namespace.py",
                "autotrade_numeric/__init__.py",
                "autotrade_numeric/_generated_common_scalars.py",
                "autotrade_numeric/_generated_decimal_limits.py",
                "autotrade_numeric/exact_decimal.py",
            )
            files = {item["target_relative_path"]: item for item in installed["files"]}
            components = {item["path"]: item for item in installed["components"]}
            for runtime_path in runtime_paths:
                source_digest = "sha256:" + sha256(
                    (ROOT / runtime_path).read_bytes()
                ).hexdigest()
                self.assertEqual(files[runtime_path]["sha256"], source_digest)
                self.assertEqual(components[runtime_path]["sha256"], source_digest)


@unittest.skipUnless(sys.platform == "win32", "native Windows retained publication acceptance")
class WindowsFoundationRetainedPublicationTests(unittest.TestCase):
    def test_lexical_namespace_alias_is_rejected_before_absolute_path_expansion(self):
        with patch.object(
            local_filesystem_module.os.path,
            "abspath",
            side_effect=AssertionError("abspath must not run for unsafe lexical input"),
        ) as absolute:
            with self.assertRaisesRegex(
                LocalFilesystemQualificationError,
                "canonical Windows namespace",
            ):
                local_filesystem_module.freeze_local_filesystem_path(
                    r"C:\autotrade\locks.\resource.lock"
                )
        absolute.assert_not_called()

    def test_namespace_validator_rejects_reserved_parent_component(self):
        with self.assertRaisesRegex(RuntimeError, "non-canonical Win32 pathname"):
            windows_namespace_module.require_windows_namespace_path(
                r"C:\autotrade\CON\resource.lock",
                subject="test path",
            )

    def test_namespace_validator_preserves_ordinary_lexical_path(self):
        candidate = Path(r"C:\autotrade\locks\resource.lock")
        self.assertEqual(
            windows_namespace_module.require_windows_namespace_path(
                candidate,
                subject="test path",
            ),
            candidate,
        )

    def test_directory_publication_uses_exact_lock_and_unlock_abi(self):
        class ApiFunction:
            def __init__(self):
                self.argtypes = None
                self.restype = None
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                return 1

        class Kernel32:
            def __init__(self):
                self.LockFileEx = ApiFunction()
                self.UnlockFileEx = ApiFunction()

        kernel32 = Kernel32()
        authority = windows_namespace_module.RetainedWindowsDirectory(
            canonical_path="retained",
            volume_serial=1,
            file_index_high=2,
            file_index_low=3,
        )
        with (
            patch.object(
                windows_namespace_module,
                "_retained_authority_handle",
                return_value=101,
            ),
            patch.object(
                windows_namespace_module,
                "_nt_create_relative_file",
                return_value=202,
            ),
            patch.object(
                windows_namespace_module.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            patch.object(
                windows_namespace_module,
                "close_windows_handle",
            ) as close_handle,
        ):
            with serialize_windows_directory_publication(authority):
                pass

        self.assertEqual(len(kernel32.LockFileEx.argtypes), 6)
        self.assertEqual(len(kernel32.UnlockFileEx.argtypes), 5)
        self.assertEqual(len(kernel32.LockFileEx.calls), 1)
        self.assertEqual(len(kernel32.UnlockFileEx.calls), 1)
        close_handle.assert_called_once_with(202)

    def test_directory_publication_unlock_failure_still_closes_handle(self):
        class ApiFunction:
            def __init__(self, result):
                self.result = result
                self.argtypes = None
                self.restype = None
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                return self.result

        class Kernel32:
            def __init__(self):
                self.LockFileEx = ApiFunction(1)
                self.UnlockFileEx = ApiFunction(0)

        kernel32 = Kernel32()
        authority = windows_namespace_module.RetainedWindowsDirectory(
            canonical_path="retained",
            volume_serial=1,
            file_index_high=2,
            file_index_low=3,
        )
        with (
            patch.object(
                windows_namespace_module,
                "_retained_authority_handle",
                return_value=101,
            ),
            patch.object(
                windows_namespace_module,
                "_nt_create_relative_file",
                return_value=202,
            ),
            patch.object(
                windows_namespace_module.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            patch.object(
                windows_namespace_module.ctypes,
                "get_last_error",
                return_value=5,
                create=True,
            ),
            patch.object(
                windows_namespace_module,
                "close_windows_handle",
            ) as close_handle,
        ):
            with self.assertRaises(OSError):
                with serialize_windows_directory_publication(authority):
                    pass

        self.assertEqual(len(kernel32.UnlockFileEx.calls), 1)
        close_handle.assert_called_once_with(202)

    def test_directory_publication_lock_serializes_distinct_handles(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            started = Event()
            acquired = Event()
            failures: list[BaseException] = []

            def contender():
                try:
                    with retain_windows_directory_namespace(parent) as authority:
                        started.set()
                        with serialize_windows_directory_publication(authority):
                            acquired.set()
                except BaseException as error:
                    failures.append(error)
                    acquired.set()

            with retain_windows_directory_namespace(parent) as authority:
                with serialize_windows_directory_publication(authority):
                    thread = Thread(target=contender, daemon=True)
                    thread.start()
                    self.assertTrue(started.wait(5), "contender did not reach the lock")
                    self.assertFalse(
                        acquired.wait(0.25),
                        "second publisher entered while the retained transaction lock was held",
                    )
            thread.join(5)
            self.assertFalse(thread.is_alive(), "contender did not finish after lock release")
            self.assertEqual(failures, [])
            self.assertTrue(acquired.is_set())
            lock_path = parent / ".autotrade-composition.lock"
            self.assertTrue(lock_path.is_file())
            lock_path.unlink()
            self.assertFalse(lock_path.exists())

    def test_staging_does_not_use_visible_path_replace_on_windows(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            composition = root / "composition.json"
            composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": SOURCE_SHA,
                        "components": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "tools.stage_windows_foundation.os.replace",
                side_effect=AssertionError("visible-path os.replace is forbidden on Windows"),
            ):
                staged = stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                )
            self.assertEqual(len(staged), 7)


class FoundationExactGitProcessAuthorityTests(unittest.TestCase):
    def test_staging_uses_os_managed_git_with_minimal_environment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            staging.mkdir()
            composition = root / "composition.json"
            composition.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product": "AutoTrade",
                        "source_sha": SOURCE_SHA,
                        "components": [],
                    }
                ),
                encoding="utf-8",
            )
            original_run = subprocess.run
            observed_git_calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

            def guarded_run(command, *args, **kwargs):
                argv = tuple(str(item) for item in command)
                if argv and Path(argv[0]).name.lower() in {"git", "git.exe"}:
                    environment = kwargs.get("env")
                    self.assertIs(type(environment), dict)
                    self.assertTrue(Path(argv[0]).is_absolute())
                    self.assertNotIn("PATH", environment)
                    self.assertNotIn("HOME", environment)
                    self.assertNotIn("GIT_OBJECT_DIRECTORY", environment)
                    self.assertNotIn("GIT_ALTERNATE_OBJECT_DIRECTORIES", environment)
                    self.assertEqual(environment.get("GIT_CONFIG_NOSYSTEM"), "1")
                    self.assertEqual(environment.get("GIT_CONFIG_GLOBAL"), os.devnull)
                    self.assertEqual(environment.get("GIT_NO_REPLACE_OBJECTS"), "1")
                    observed_git_calls.append((argv, dict(environment)))
                return original_run(command, *args, **kwargs)

            hostile_environment = {
                "PATH": str(root / "caller-bin"),
                "HOME": str(root / "caller-home"),
                "GIT_CONFIG_GLOBAL": str(root / "attacker.gitconfig"),
                "GIT_OBJECT_DIRECTORY": str(root / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(root / "alternate-objects"),
                "GIT_NO_REPLACE_OBJECTS": "0",
            }
            with patch.dict(os.environ, hostile_environment, clear=False), patch.object(
                staging_module.subprocess,
                "run",
                side_effect=guarded_run,
            ):
                staged = stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                )

            self.assertEqual(len(staged), 7)
            self.assertGreaterEqual(len(observed_git_calls), 3)
            self.assertTrue(
                all(Path(argv[0]).is_absolute() for argv, _environment in observed_git_calls)
            )
            self.assertTrue(
                any(
                    argv[1:3] == ("cat-file", "blob")
                    for argv, _environment in observed_git_calls
                )
            )

    def test_staging_rejects_git_executable_selected_from_source_checkout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            fake_git = source / ("git.exe" if os.name == "nt" else "git")
            fake_git.write_bytes(b"not executable authority")
            with patch.object(
                staging_module,
                "_trusted_git_candidate_paths",
                return_value=(fake_git,),
            ):
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "must not originate from the source checkout",
                ):
                    staging_module._trusted_git_executable(source_root=source)


@unittest.skipIf(os.name == "nt", "POSIX retained publication only")
class PosixFoundationRetainedPublicationTests(unittest.TestCase):
    def _composition(self, root: Path) -> tuple[Path, Path]:
        staging = root / "staging"
        staging.mkdir()
        composition = root / "composition.json"
        composition.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "product": "AutoTrade",
                    "source_sha": SOURCE_SHA,
                    "components": [],
                }
            ),
            encoding="utf-8",
        )
        return staging, composition

    def test_posix_publication_uses_only_retained_dirfd_mutation(self):
        with TemporaryDirectory() as directory:
            staging, composition = self._composition(Path(directory))
            original_replace = os.replace
            relative_replacements = []

            def guarded_replace(src, dst, *args, **kwargs):
                self.assertIsNotNone(kwargs.get("src_dir_fd"))
                self.assertIsNotNone(kwargs.get("dst_dir_fd"))
                relative_replacements.append((src, dst))
                return original_replace(src, dst, *args, **kwargs)

            with patch.object(
                staging_module.tempfile,
                "mkstemp",
                side_effect=AssertionError(
                    "visible-path tempfile publication is forbidden on POSIX"
                ),
            ), patch.object(
                staging_module.os,
                "replace",
                side_effect=guarded_replace,
            ):
                staged = stage_windows_foundation(
                    staging=staging,
                    composition_path=composition,
                )

            self.assertEqual(len(staged), 7)
            self.assertEqual(len(relative_replacements), 1)
            self.assertTrue(
                (staging / "autotrade_foundation" / "local_filesystem.py").is_file()
            )

    def test_same_inode_manifest_tamper_is_not_silently_overwritten(self):
        with TemporaryDirectory() as directory:
            staging, composition = self._composition(Path(directory))
            original_manifest = composition.read_bytes()
            tampered_manifest = original_manifest.replace(b"AutoTrade", b"BadTrade!")
            self.assertEqual(len(tampered_manifest), len(original_manifest))
            self.assertNotEqual(tampered_manifest, original_manifest)
            original_publish = staging_module._atomic_publish_manifest
            tampered = False

            def tampering_publish(path, data, *, baseline, expected_original, posix_parent_authority=None):
                nonlocal tampered
                path.write_bytes(tampered_manifest)
                os.utime(
                    path,
                    ns=(baseline.st_atime_ns, baseline.st_mtime_ns),
                    follow_symlinks=False,
                )
                observed = path.stat(follow_symlinks=False)
                self.assertEqual(observed.st_dev, baseline.st_dev)
                self.assertEqual(observed.st_ino, baseline.st_ino)
                self.assertEqual(observed.st_size, baseline.st_size)
                self.assertEqual(observed.st_mtime_ns, baseline.st_mtime_ns)
                tampered = True
                return original_publish(
                    path,
                    data,
                    baseline=baseline,
                    expected_original=expected_original,
                    posix_parent_authority=posix_parent_authority,
                )

            with patch.object(
                staging_module,
                "_atomic_publish_manifest",
                side_effect=tampering_publish,
            ):
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "composition manifest changed during staging",
                ):
                    stage_windows_foundation(
                        staging=staging,
                        composition_path=composition,
                    )

            self.assertTrue(tampered)
            self.assertEqual(composition.read_bytes(), tampered_manifest)

    def test_parent_swap_after_retention_cannot_redirect_external_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging, composition = self._composition(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"stable")
            before_manifest = composition.read_bytes()
            original_retain = staging_module._retained_posix_relative_directory
            raced = False

            @contextmanager
            def swapping_retain(root_descriptor, parts, *, create):
                nonlocal raced
                with original_retain(
                    root_descriptor,
                    parts,
                    create=create,
                ) as descriptor:
                    if not raced and tuple(parts) == ("autotrade_numeric",):
                        parent = staging / "autotrade_numeric"
                        retained_generation = staging / "retained-numeric-generation"
                        parent.rename(retained_generation)
                        parent.symlink_to(outside, target_is_directory=True)
                        raced = True
                    yield descriptor

            with patch.object(
                staging_module,
                "_retained_posix_relative_directory",
                side_effect=swapping_retain,
            ):
                with self.assertRaisesRegex(
                    FoundationStagingError,
                    "path component must not be a symlink/reparse point|retained directory component cannot be opened|changed during retained publication",
                ):
                    stage_windows_foundation(
                        staging=staging,
                        composition_path=composition,
                    )

            self.assertTrue(raced)
            self.assertEqual(composition.read_bytes(), before_manifest)
            self.assertEqual(sorted(item.name for item in outside.iterdir()), ["sentinel"])
            self.assertEqual((outside / "sentinel").read_bytes(), b"stable")
            moved = staging / "retained-numeric-generation"
            self.assertTrue(moved.is_dir())
            self.assertEqual(list(moved.iterdir()), [])
            foundation = staging / "autotrade_foundation"
            self.assertTrue(foundation.is_dir())
            self.assertEqual(list(foundation.iterdir()), [])
            self.assertFalse((outside / "exact_decimal.py").exists())


@unittest.skipUnless(sys.platform == "win32", "native Windows junction and hardlink acceptance")
class WindowsFoundationNoReparseTests(unittest.TestCase):
    """Exact committed source positive and six native Windows no-alias failures."""

    def _fixture(self, root):
        repo = root / "tracked"
        package = repo / "autotrade_foundation"
        package.mkdir(parents=True)
        for filename in ("__init__.py", "local_filesystem.py", "windows_namespace.py"):
            shutil.copy2(ROOT / "autotrade_foundation" / filename, package / filename)
        numeric = repo / "autotrade_numeric"
        numeric.mkdir(parents=True)
        numeric_files = (
            "__init__.py",
            "_generated_common_scalars.py",
            "_generated_decimal_limits.py",
            "exact_decimal.py",
        )
        for filename in numeric_files:
            shutil.copy2(ROOT / "autotrade_numeric" / filename, numeric / filename)
        for command in (
            ("git", "-C", str(repo), "init", "-q"),
            ("git", "-C", str(repo), "add", "--",
             "autotrade_foundation/__init__.py",
             "autotrade_foundation/local_filesystem.py",
             "autotrade_foundation/windows_namespace.py",
             "autotrade_numeric/__init__.py",
             "autotrade_numeric/_generated_common_scalars.py",
             "autotrade_numeric/_generated_decimal_limits.py",
             "autotrade_numeric/exact_decimal.py"),
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
            ("cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(real)),
            text=True, capture_output=True, check=False)
        self.assertEqual(
            p.returncode,
            0,
            "junction fixture setup failed before the staging oracle: "
            + p.stdout
            + p.stderr,
        )
        self.assertTrue(alias.is_dir())
        self.assertTrue(
            os.path.isjunction(alias),
            "junction fixture must prove it created a native Windows junction",
        )

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
            self.assertEqual(len(result), 7)
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

    def test_retained_parent_creation_denies_namespace_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            guarded = root / "guarded"
            guarded.mkdir()
            target = guarded / "created" / "nested" / "leaf.bin"
            moved = root / "moved"
            with retain_windows_parent_namespace(target, create=True):
                self.assertTrue(target.parent.is_dir())
                with self.assertRaises(OSError):
                    guarded.rename(moved)
                self.assertTrue(guarded.is_dir())
                self.assertFalse(moved.exists())
            guarded.rename(moved)
            self.assertTrue((moved / "created" / "nested").is_dir())

    def test_journal_store_creates_missing_parent_only_under_retained_namespace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "new" / "nested" / "journal.sqlite3"
            store = JournalStore(database)
            self.assertTrue(database.is_file())
            self.assertEqual(store.store_identity.canonical_path, str(database))
            self.assertTrue(database.parent.is_dir())

    def test_journal_store_rejects_junction_parent_before_external_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "external-journal"
            outside.mkdir()
            alias = root / "journal-alias"
            self._junction(alias, outside)
            database = alias / "must-not-exist" / "journal.sqlite3"
            with self.assertRaises(RuntimeError):
                JournalStore(database)
            self.assertFalse((outside / "must-not-exist").exists())
            self.assertEqual(list(outside.iterdir()), [])

    def test_resource_lock_creates_missing_parent_only_under_retained_namespace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "new-lock" / "nested" / "resource.lock"
            lock = resource_lock.ResourceLock(lock_path)
            lock.acquire()
            try:
                self.assertTrue(lock_path.is_file())
                self.assertTrue(lock_path.parent.is_dir())
            finally:
                lock.release()

    def test_resource_lock_rejects_junction_parent_before_external_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "external-lock"
            outside.mkdir()
            alias = root / "lock-alias"
            self._junction(alias, outside)
            lock_path = alias / "must-not-exist" / "resource.lock"
            with self.assertRaises(resource_lock.ResourceLockError):
                resource_lock.ResourceLock(lock_path).acquire()
            self.assertFalse((outside / "must-not-exist").exists())
            self.assertEqual(list(outside.iterdir()), [])


    def test_missing_parent_junction_race_cannot_escape_staging_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            stage.mkdir()
            outside = root / "external-race"
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"stable")

            original_open = windows_namespace_module._open_relative_directory
            injected = []

            def race_parent(parent_handle, name, *, create=False):
                if (
                    create
                    and name == "autotrade_foundation"
                    and not injected
                ):
                    self._junction(stage / "autotrade_foundation", outside)
                    injected.append(True)
                return original_open(
                    parent_handle,
                    name,
                    create=create,
                )

            before = composition.read_bytes()
            with patch.object(
                windows_namespace_module,
                "_open_relative_directory",
                new=race_parent,
            ):
                with self.assertRaises(FoundationStagingError):
                    stage_windows_foundation(
                        staging=stage,
                        composition_path=composition,
                        source_root=source,
                    )

            self.assertEqual(injected, [True])
            self.assertEqual(composition.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["sentinel"])
            self.assertEqual((outside / "sentinel").read_bytes(), b"stable")

    def test_component_leaf_is_write_delete_pinned_until_manifest_commit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, stage, composition = self._fixture(root)
            stage.mkdir()
            target = stage / "autotrade_foundation" / "local_filesystem.py"
            expected = (source / "autotrade_foundation" / "local_filesystem.py").read_bytes()
            original_manifest = staging_module._atomic_publish_manifest
            guarded_calls = []

            def guarded_manifest(path, data, *, baseline):
                self.assertTrue(target.is_file())
                replacement = target.with_name("replacement.tmp")
                replacement.write_bytes(b"replacement")
                with self.assertRaises(OSError):
                    target.write_bytes(b"in-place tamper")
                with self.assertRaises(OSError):
                    os.replace(replacement, target)
                guarded_calls.append("blocked")
                return original_manifest(path, data, baseline=baseline)

            with patch.object(
                staging_module,
                "_atomic_publish_manifest",
                new=guarded_manifest,
            ):
                staged = stage_windows_foundation(
                    staging=stage,
                    composition_path=composition,
                    source_root=source,
                )
            self.assertEqual(len(staged), 7)
            self.assertEqual(guarded_calls, ["blocked"])
            self.assertEqual(target.read_bytes(), expected)

            # Exercise the pre-existing equal-byte path as well: remove one
            # composition record while retaining every staged leaf. The next
            # transaction must pin the existing leaf before manifest repair.
            record = json.loads(composition.read_text(encoding="utf-8"))
            record["components"] = [
                item
                for item in record["components"]
                if item["path"] != "autotrade_foundation/local_filesystem.py"
            ]
            composition.write_text(json.dumps(record), encoding="utf-8")
            guarded_calls.clear()
            with patch.object(
                staging_module,
                "_atomic_publish_manifest",
                new=guarded_manifest,
            ):
                stage_windows_foundation(
                    staging=stage,
                    composition_path=composition,
                    source_root=source,
                )
            self.assertEqual(guarded_calls, ["blocked"])
            self.assertEqual(target.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
