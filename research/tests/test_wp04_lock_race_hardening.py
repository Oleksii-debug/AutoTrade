import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock
from unittest.mock import patch

import autotrade_research.artifacts.resource_lock as resource_lock_module
from autotrade_research.artifacts.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
    ResourceLockError,
)


class ResourceLockRaceHardeningTests(unittest.TestCase):
    def test_dangling_symlink_is_rejected_without_creating_external_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            external_target = root / "outside-target.lock"
            try:
                os.symlink(external_target, lock_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable on this platform: {exc}")

            with self.assertRaises(ResourceLockError):
                ResourceLock(lock_path).acquire()

            self.assertFalse(
                external_target.exists(),
                "unsafe create-through alias created the external target",
            )
            self.assertTrue(lock_path.is_symlink())

    @unittest.skipIf(
        os.name == "nt",
        "Windows normally forbids replacing an open lock pathname",
    )
    def test_path_swap_after_open_is_rejected_before_acquisition_succeeds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            lock_path.write_bytes(b"\0")
            lock = ResourceLock(lock_path)
            original_open = Path.open

            def open_then_replace(path_obj, *args, **kwargs):
                handle = original_open(path_obj, *args, **kwargs)
                mode = args[0] if args else kwargs.get("mode")
                if Path(path_obj) == lock_path and mode == "r+b":
                    os.unlink(lock_path)
                    lock_path.write_bytes(b"replacement")
                return handle

            with patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=open_then_replace,
            ):
                with self.assertRaisesRegex(
                    ResourceLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertEqual(lock_path.read_bytes(), b"replacement")
            self.assertIsNone(lock._handle)

    @unittest.skipIf(
        os.name == "nt",
        "Windows normally forbids replacing an open lock pathname",
    )
    def test_path_swap_after_os_lock_is_rejected_and_old_lock_is_released(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            lock = ResourceLock(lock_path)
            original_lock = ResourceLock._lock_handle

            def lock_then_replace(handle):
                original_lock(handle)
                os.unlink(lock_path)
                lock_path.write_bytes(b"replacement-after-lock")

            with patch.object(
                ResourceLock,
                "_lock_handle",
                side_effect=lock_then_replace,
            ):
                with self.assertRaisesRegex(
                    ResourceLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertEqual(lock_path.read_bytes(), b"replacement-after-lock")
            self.assertIsNone(lock._handle)
            with ResourceLock(lock_path):
                pass

    def test_acquisition_cleanup_close_failure_poisoning_preserves_primary(self):
        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            handle = mock.MagicMock()
            handle.tell.return_value = 1
            handle.close.side_effect = OSError("simulated close failure")
            primary = ResourceLockBusyError("another process owns the resource lock")

            try:
                with (
                    patch.object(lock, "_open_lock_handle", return_value=handle),
                    patch.object(lock, "_validate_handle_identity"),
                    patch.object(ResourceLock, "_lock_handle", side_effect=primary),
                ):
                    with self.assertRaises(ResourceLockBusyError) as caught:
                        lock.acquire()

                self.assertIs(caught.exception, primary)
                notes = getattr(caught.exception, "__notes__", ())
                self.assertTrue(
                    any(
                        "acquisition cleanup" in note
                        and "simulated close failure" in note
                        for note in notes
                    ),
                    f"cleanup evidence missing from primary exception notes: {notes!r}",
                )
                self.assertIs(lock._handle, handle)
                with self.assertRaisesRegex(ResourceLockError, "already held"):
                    lock.acquire()
            finally:
                # The production contract deliberately keeps the real Windows
                # parent namespace retained while handle lifetime is uncertain.
                # Once the poison assertions above are complete, remove only
                # the synthetic close failure and bypass the synthetic OS-lock
                # operation so the fixture can release that real guard.
                handle.close.side_effect = None
                if lock._handle is not None or lock._windows_parent_guard is not None:
                    with patch.object(lock, "_unlock_handle"):
                        lock.release()

    def test_dual_unlock_and_close_failure_keeps_lock_fail_closed(self):
        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            handle = mock.MagicMock()
            handle.close.side_effect = OSError("simulated close failure")
            lock._handle = handle

            with patch.object(
                ResourceLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated unlock failure") as caught:
                    lock.release()

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "close also failed after unlock failure" in note
                    and "simulated close failure" in note
                    for note in notes
                ),
                f"secondary close evidence missing from exception notes: {notes!r}",
            )
            self.assertIs(lock._handle, handle)
            with self.assertRaisesRegex(ResourceLockError, "already held"):
                lock.acquire()

    def test_context_manager_preserves_primary_body_failure_on_release_failure(self):
        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")

            with patch.object(
                ResourceLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(ValueError, "primary failure") as caught:
                    with lock:
                        raise ValueError("primary failure")

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "ResourceLock release also failed" in note
                    and "simulated unlock failure" in note
                    for note in notes
                ),
                f"release evidence missing from primary exception notes: {notes!r}",
            )
            self.assertIsNone(lock._handle)

    @unittest.skipUnless(os.name == "nt", "Windows-specific remote-share gate")
    def test_windows_unc_share_is_rejected_before_filesystem_touch(self):
        lock = ResourceLock(r"\\\\invalid-autotrade-host\\share\\resource.lock")
        with patch.object(
            Path,
            "mkdir",
            side_effect=AssertionError("UNC path reached filesystem mutation"),
        ) as mkdir:
            with self.assertRaisesRegex(
                ResourceLockError,
                "qualified local filesystem",
            ):
                lock.acquire()
        mkdir.assert_not_called()

    def test_windows_parent_guard_admission_failure_uses_resource_lock_error(self):
        class FailingParentGuard:
            def __enter__(self):
                raise RuntimeError("simulated retained-parent admission failure")

            def __exit__(self, exc_type, exc_value, traceback):
                raise AssertionError("failed parent admission must not call __exit__")

        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            windows_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
            )
            with (
                patch.object(resource_lock_module, "os", windows_os),
                patch.object(
                    resource_lock_module,
                    "require_qualified_local_filesystem_path",
                ),
                patch.object(
                    resource_lock_module,
                    "retain_windows_parent_namespace",
                    return_value=FailingParentGuard(),
                ),
            ):
                with self.assertRaisesRegex(
                    ResourceLockError,
                    "ordinary retained local Windows namespace",
                ) as caught:
                    lock.acquire()

            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertIsNone(lock._handle)
            self.assertIsNone(lock._windows_parent_guard)

    def test_windows_parent_guard_spans_complete_held_lock_lifetime(self):
        events = []

        class ParentGuard:
            def __enter__(self):
                events.append("parent-enter")
                return object()

            def __exit__(self, exc_type, exc_value, traceback):
                events.append("parent-exit")
                return False

        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            handle = mock.MagicMock()
            handle.close.side_effect = lambda: events.append("handle-close")
            guard = ParentGuard()
            windows_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
            )

            def acquired():
                events.append("lock-acquired")
                lock._handle = handle

            def unlocked(_handle):
                events.append("lock-unlocked")

            with (
                patch.object(resource_lock_module, "os", windows_os),
                patch.object(
                    resource_lock_module,
                    "require_qualified_local_filesystem_path",
                ),
                patch.object(
                    resource_lock_module,
                    "retain_windows_parent_namespace",
                    return_value=guard,
                ),
                patch.object(
                    lock,
                    "_acquire_after_parent_ready",
                    side_effect=acquired,
                ),
                patch.object(lock, "_unlock_handle", side_effect=unlocked),
            ):
                lock.acquire()
                self.assertEqual(events, ["parent-enter", "lock-acquired"])
                self.assertIs(lock._windows_parent_guard, guard)
                lock.release()

            self.assertEqual(
                events,
                [
                    "parent-enter",
                    "lock-acquired",
                    "lock-unlocked",
                    "handle-close",
                    "parent-exit",
                ],
            )
            self.assertIsNone(lock._handle)
            self.assertIsNone(lock._windows_parent_guard)

    def test_windows_parent_guard_is_not_released_when_handle_close_is_uncertain(self):
        events = []

        class ParentGuard:
            def __enter__(self):
                events.append("parent-enter")
                return object()

            def __exit__(self, exc_type, exc_value, traceback):
                events.append("parent-exit")
                return False

        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            handle = mock.MagicMock()
            handle.close.side_effect = OSError("simulated close failure")
            guard = ParentGuard()
            windows_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
            )

            def acquired():
                lock._handle = handle

            with (
                patch.object(resource_lock_module, "os", windows_os),
                patch.object(
                    resource_lock_module,
                    "require_qualified_local_filesystem_path",
                ),
                patch.object(
                    resource_lock_module,
                    "retain_windows_parent_namespace",
                    return_value=guard,
                ),
                patch.object(
                    lock,
                    "_acquire_after_parent_ready",
                    side_effect=acquired,
                ),
                patch.object(lock, "_unlock_handle"),
            ):
                lock.acquire()
                with self.assertRaisesRegex(OSError, "simulated close failure"):
                    lock.release()

            self.assertEqual(events, ["parent-enter"])
            self.assertIs(lock._handle, handle)
            self.assertIs(lock._windows_parent_guard, guard)

    def test_windows_parent_cleanup_failure_poisons_reacquire(self):
        events = []

        class FailingParentGuard:
            def __enter__(self):
                events.append("parent-enter")
                return object()

            def __exit__(self, exc_type, exc_value, traceback):
                events.append("parent-exit")
                raise OSError("simulated parent release failure")

        with TemporaryDirectory() as directory:
            lock = ResourceLock(Path(directory) / "resource.lock")
            guard = FailingParentGuard()
            windows_os = SimpleNamespace(
                name="nt",
                path=os.path,
                fspath=os.fspath,
            )
            primary = ResourceLockBusyError("simulated acquire failure")

            with (
                patch.object(resource_lock_module, "os", windows_os),
                patch.object(
                    resource_lock_module,
                    "require_qualified_local_filesystem_path",
                ),
                patch.object(
                    resource_lock_module,
                    "retain_windows_parent_namespace",
                    return_value=guard,
                ),
                patch.object(
                    lock,
                    "_acquire_after_parent_ready",
                    side_effect=primary,
                ),
            ):
                with self.assertRaises(ResourceLockBusyError) as caught:
                    lock.acquire()
                self.assertIs(caught.exception, primary)
                notes = getattr(caught.exception, "__notes__", ())
                self.assertTrue(
                    any(
                        "parent namespace release also failed" in note
                        and "simulated parent release failure" in note
                        for note in notes
                    ),
                    f"parent cleanup evidence missing from notes: {notes!r}",
                )
                self.assertIs(lock._windows_parent_guard, guard)
                with self.assertRaisesRegex(
                    ResourceLockError,
                    "unreleased Windows namespace authority",
                ):
                    lock.acquire()

        self.assertEqual(events, ["parent-enter", "parent-exit"])

    @unittest.skipUnless(
        os.name == "nt",
        "native Windows retained-parent lifetime regression",
    )
    def test_windows_held_lock_prevents_parent_generation_rename(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "held-parent"
            moved = root / "moved-parent"
            parent.mkdir()
            lock = ResourceLock(parent / "resource.lock")
            lock.acquire()
            try:
                with self.assertRaises(OSError):
                    parent.rename(moved)
                self.assertTrue(parent.exists())
                self.assertFalse(moved.exists())
            finally:
                lock.release()

            parent.rename(moved)
            self.assertFalse(parent.exists())
            self.assertTrue(moved.exists())

    @unittest.skipUnless(
        os.name == "nt",
        "native Windows lock-file generation regression",
    )
    def test_windows_held_lock_prevents_lock_file_generation_rename(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "resource.lock"
            moved_path = root / "moved-resource.lock"
            lock = ResourceLock(lock_path)
            lock.acquire()
            try:
                with self.assertRaises(OSError):
                    lock_path.rename(moved_path)
                self.assertTrue(lock_path.exists())
                self.assertFalse(moved_path.exists())
            finally:
                lock.release()

            lock_path.rename(moved_path)
            self.assertFalse(lock_path.exists())
            self.assertTrue(moved_path.exists())

    def test_blocking_mode_requires_exact_bool(self):
        with self.assertRaisesRegex(TypeError, "blocking must be bool"):
            ResourceLock("resource.lock", blocking=1)


if __name__ == "__main__":
    unittest.main()
