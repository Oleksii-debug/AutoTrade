import ast
import json
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from autotrade_research.artifacts import durable_publish
from autotrade_research.artifacts.durable_publish import (
    DurablePublishLockError,
    atomic_write_json,
)
from autotrade_research.artifacts.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
    ResourceLockError,
)
from autotrade_research.io.strict_json import (
    DuplicateJsonKeyError,
    InvalidJsonDomainError,
    NonStandardJsonConstantError,
    strict_json_loads,
)


def _hold_resource_lock(lock_path, ready, release) -> None:
    with ResourceLock(lock_path):
        ready.set()
        release.wait(10)


def _acquire_then_exit(lock_path, marker_path) -> None:
    lock = ResourceLock(lock_path)
    lock.acquire()
    Path(marker_path).write_text("locked", encoding="utf-8")
    os._exit(0)


class NeutralReuseCharacterizationTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_nested_keys_and_nonstandard_constants(self):
        with self.assertRaises(DuplicateJsonKeyError):
            strict_json_loads('{"outer":{"x":1,"x":2}}')
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(NonStandardJsonConstantError):
                    strict_json_loads('{"value":' + value + "}")

    def test_strict_json_rejects_overflow_huge_integer_lone_surrogate_and_excess_depth(self):
        with self.assertRaises(InvalidJsonDomainError):
            strict_json_loads('{"value":1e309}')
        with self.assertRaisesRegex(InvalidJsonDomainError, "640 digits"):
            strict_json_loads('{"value":' + ("9" * 641) + "}")
        with self.assertRaisesRegex(InvalidJsonDomainError, "invalid Unicode"):
            strict_json_loads('{"value":"\\ud800"}')

        too_deep = "[" * 129 + "0" + "]" * 129
        with self.assertRaisesRegex(InvalidJsonDomainError, "nesting exceeds"):
            strict_json_loads(too_deep)

    def test_strict_json_accepts_valid_unicode_and_requires_explicit_text_decode(self):
        payload = '{"message":"Привіт €","amount":"0.1000"}'
        self.assertEqual(
            strict_json_loads(payload),
            {"message": "Привіт €", "amount": "0.1000"},
        )
        with self.assertRaisesRegex(TypeError, "decode bytes explicitly"):
            strict_json_loads(payload.encode("utf-8"))

    def test_atomic_publish_never_exposes_partial_destination_before_replace(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            destination.write_text('{"version":"old"}\n', encoding="utf-8")

            with patch.object(
                durable_publish.os,
                "replace",
                side_effect=OSError("simulated crash before replace"),
            ):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    atomic_write_json(destination, {"version": "new"})

            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"version": "old"},
            )
            self.assertFalse(
                any(
                    path.name.startswith(".artifact.json.")
                    and path.name.endswith(".tmp")
                    for path in Path(directory).iterdir()
                )
            )

    def test_failure_after_replace_exposes_only_complete_new_json(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            destination.write_text('{"version":"old"}\n', encoding="utf-8")

            with patch.object(
                durable_publish,
                "_sync_parent_directory",
                side_effect=OSError("simulated directory sync failure"),
            ):
                with self.assertRaisesRegex(OSError, "directory sync failure"):
                    atomic_write_json(destination, {"version": "new", "items": [1, 2, 3]})

            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"version": "new", "items": [1, 2, 3]},
            )
            self.assertFalse(
                any(
                    path.name.startswith(".artifact.json.")
                    and path.name.endswith(".tmp")
                    for path in Path(directory).iterdir()
                )
            )


    def test_atomic_publish_rejects_hardlinked_lock_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "artifact.json"
            destination.write_text('{"version":"old"}\n', encoding="utf-8")
            unrelated = root / "unrelated.lock"
            unrelated.write_bytes(b"sentinel")
            lock_path = root / ".artifact.json.lock"
            try:
                os.link(unrelated, lock_path)
            except (OSError, NotImplementedError):
                self.skipTest("hard links unavailable on this platform")

            with self.assertRaisesRegex(
                DurablePublishLockError, "hard-link aliases"
            ):
                atomic_write_json(destination, {"version": "new"})

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                '{"version":"old"}\n',
            )
            self.assertEqual(unrelated.read_bytes(), b"sentinel")

    def test_atomic_publish_rejects_symlink_lock_path_when_supported(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "artifact.json"
            destination.write_text('{"version":"old"}\n', encoding="utf-8")
            unrelated = root / "unrelated.lock"
            unrelated.write_bytes(b"sentinel")
            lock_path = root / ".artifact.json.lock"
            try:
                os.symlink(unrelated, lock_path)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable on this platform")

            with self.assertRaisesRegex(
                DurablePublishLockError, "regular non-symlink"
            ):
                atomic_write_json(destination, {"version": "new"})

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                '{"version":"old"}\n',
            )
            self.assertEqual(unrelated.read_bytes(), b"sentinel")

    def test_resource_lock_excludes_second_process(self):
        with TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / "resource.lock")
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            child = context.Process(
                target=_hold_resource_lock,
                args=(lock_path, ready, release),
            )
            child.start()
            try:
                self.assertTrue(ready.wait(10), "child never acquired resource lock")
                with self.assertRaises(ResourceLockBusyError):
                    ResourceLock(lock_path).acquire()
            finally:
                release.set()
                child.join(10)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            self.assertEqual(child.exitcode, 0)

    def test_process_death_releases_local_os_lock(self):
        with TemporaryDirectory() as directory:
            lock_path = str(Path(directory) / "resource.lock")
            marker_path = str(Path(directory) / "locked.marker")
            context = multiprocessing.get_context("spawn")
            child = context.Process(
                target=_acquire_then_exit,
                args=(lock_path, marker_path),
            )
            child.start()
            deadline = time.monotonic() + 10
            while not Path(marker_path).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            child.join(10)
            self.assertTrue(Path(marker_path).exists(), "child never proved lock ownership")
            self.assertEqual(child.exitcode, 0)

            with ResourceLock(lock_path):
                self.assertTrue(Path(lock_path).is_file())

    def test_lock_path_aliases_are_rejected_when_platform_supports_them(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.lock"
            original.write_bytes(b"\0")

            hardlink = root / "hardlink.lock"
            try:
                os.link(original, hardlink)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaisesRegex(ResourceLockError, "hard-link aliases"):
                    ResourceLock(original).acquire()

            if hasattr(os, "symlink"):
                symlink = root / "symlink.lock"
                try:
                    os.symlink(original, symlink)
                except (OSError, NotImplementedError):
                    pass
                else:
                    with self.assertRaisesRegex(ResourceLockError, "regular non-symlink"):
                        ResourceLock(symlink).acquire()

    def test_provenance_is_exact_and_keeps_release_rights_unresolved(self):
        repo_root = Path(__file__).resolve().parents[2]
        record = json.loads(
            (
                repo_root
                / "provenance"
                / "reuse"
                / "autosport-neutral-primitives.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(record["work_package"], "WP-04")
        self.assertEqual(
            record["source"]["revision"],
            "cb102d85f0c820c7097875191deca73e53ec94f5",
        )
        self.assertEqual(
            record["autotrade_base_sha"],
            "b8a63b477a2328007ddfc4a55b7aa02941d9fa3a",
        )
        self.assertEqual(
            record["source"]["release_distribution_rights"],
            "UNRESOLVED",
        )
        self.assertEqual(record["qualification_status"], "IN_PROGRESS")
        self.assertEqual(
            record["characterization"]["exact_head_ci_evidence"],
            "PENDING",
        )
        self.assertFalse(
            record["authority_boundary"]["financial_ledger_write_allowed"]
        )
        self.assertFalse(
            record["authority_boundary"]["remote_execution_authority_allowed"]
        )

    def test_neutral_reuse_modules_have_no_autosport_runtime_import(self):
        root = Path(__file__).resolve().parents[1] / "autotrade_research"
        files = (
            root / "io" / "strict_json.py",
            root / "artifacts" / "durable_publish.py",
            root / "artifacts" / "resource_lock.py",
            root / "artifacts" / "content_store.py",
        )
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            self.assertFalse(
                any(name == "autosport" or name.startswith("autosport.") for name in imported),
                f"{path} retains Autosport runtime coupling",
            )


if __name__ == "__main__":
    unittest.main()
