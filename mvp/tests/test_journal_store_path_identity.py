from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore


class JournalStorePathIdentityRegressionTests(unittest.TestCase):
    def test_relative_backing_path_is_frozen_at_construction(self) -> None:
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                (first / "state").mkdir()
                (second / "state").mkdir()

                os.chdir(first)
                store = JournalStore("state/journal.sqlite")
                expected = (first / "state" / "journal.sqlite").resolve()

                self.assertTrue(
                    Path(store.path).is_absolute(),
                    "JournalStore must freeze relative durable-state authority to an absolute path at construction",
                )
                self.assertEqual(Path(store.path), expected)

                os.chdir(second)
                self.assertEqual(
                    Path(store.path),
                    expected,
                    "changing process CWD must not retarget an already-constructed JournalStore",
                )
        finally:
            os.chdir(original_cwd)

    def test_construction_captures_cwd_before_parent_creation(self) -> None:
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                real_mkdir = Path.mkdir
                cwd_changed = False

                def mkdir_after_cwd_change(path, *args, **kwargs):
                    nonlocal cwd_changed
                    if not cwd_changed:
                        cwd_changed = True
                        os.chdir(second)
                    return real_mkdir(path, *args, **kwargs)

                os.chdir(first)
                with patch.object(Path, "mkdir", new=mkdir_after_cwd_change):
                    store = JournalStore("state/journal.sqlite")

                expected = (first / "state" / "journal.sqlite").resolve()
                unexpected = second / "state" / "journal.sqlite"
                self.assertTrue(cwd_changed)
                self.assertEqual(Path(store.path), expected)
                self.assertTrue(expected.exists())
                self.assertFalse(
                    unexpected.exists(),
                    "construction-time CWD changes must not select a second journal authority",
                )
        finally:
            os.chdir(original_cwd)

    def test_same_relative_text_in_different_cwds_is_not_same_store_identity(self) -> None:
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                (first / "state").mkdir()
                (second / "state").mkdir()

                os.chdir(first)
                first_store = JournalStore("state/journal.sqlite")

                os.chdir(second)
                second_store = JournalStore("state/journal.sqlite")

                self.assertNotEqual(
                    Path(first_store.path),
                    Path(second_store.path),
                    "equal caller path text in different CWDs must not alias two durable-state authorities",
                )
        finally:
            os.chdir(original_cwd)

    def test_store_identity_is_stable_across_cwd_change_and_restart(self) -> None:
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                (first / "state").mkdir()

                os.chdir(first)
                first_store = JournalStore("state/journal.sqlite")
                identity = first_store.store_identity

                os.chdir(second)
                self.assertEqual(first_store.store_identity, identity)

                restarted = JournalStore(first / "state" / "journal.sqlite")
                self.assertEqual(restarted.store_identity, identity)
        finally:
            os.chdir(original_cwd)

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows uses retained native namespace/file guards instead of post-open detection",
    )
    def test_first_open_replacement_between_anchor_and_sqlite_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "journal.sqlite"
            replacement = root / "replacement.sqlite"
            real_connect = sqlite3.connect
            raced = False

            def racing_connect(database, *args, **kwargs):
                nonlocal raced
                database_path = Path(database)
                if not raced and database_path == path:
                    raced = True
                    real_connect(replacement).close()
                    path.unlink()
                    replacement.replace(path)
                return real_connect(database, *args, **kwargs)

            with patch(
                "mvp.autotrade_mvp.persistence.sqlite3.connect",
                side_effect=racing_connect,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "first open does not match the anchored journal backing file",
                ):
                    JournalStore(path)

            self.assertTrue(raced)
            self.assertTrue(path.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows-only namespace guard")
    def test_windows_connect_guard_denies_file_and_parent_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "state"
            parent.mkdir()
            path = parent / "journal.sqlite"
            moved_file = parent / "moved.sqlite"
            moved_parent = root / "state-moved"
            real_connect = sqlite3.connect
            observed = {"file": False, "parent": False}

            def guarded_connect(database, *args, **kwargs):
                database_path = Path(database)
                if database_path == path:
                    with self.assertRaises(OSError):
                        path.replace(moved_file)
                    observed["file"] = True
                    with self.assertRaises(OSError):
                        parent.replace(moved_parent)
                    observed["parent"] = True
                return real_connect(database, *args, **kwargs)

            with patch(
                "mvp.autotrade_mvp.persistence.sqlite3.connect",
                side_effect=guarded_connect,
            ):
                store = JournalStore(path)

            self.assertEqual(observed, {"file": True, "parent": True})
            self.assertEqual(store.store_identity.identity_source, "windows_by_handle")
            self.assertTrue(path.exists())
            self.assertTrue(parent.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows-only native identity")
    def test_windows_replacement_after_guard_release_changes_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "journal.sqlite"
            store = JournalStore(path)
            original = store.store_identity
            detached = root / "detached.sqlite"

            path.replace(detached)
            sqlite3.connect(path).close()

            with self.assertRaises(RuntimeError):
                store.current_journal_sequence()
            replacement = JournalStore(path)
            self.assertNotEqual(replacement.store_identity, original)

    def test_hard_link_aliases_are_rejected_before_wal_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "journal.sqlite"
            original = JournalStore(path)
            original_identity = original.store_identity
            alias = root / "hardlink.sqlite"
            try:
                os.link(path, alias)
            except OSError:
                self.skipTest("filesystem does not permit hard-link creation")

            with self.assertRaisesRegex(RuntimeError, "exactly one hard-link"):
                original.current_journal_sequence()
            with self.assertRaisesRegex(RuntimeError, "exactly one hard-link"):
                JournalStore(alias)

            alias.unlink()
            self.assertEqual(original.current_journal_sequence(), 0)
            self.assertEqual(original.store_identity, original_identity)

    def test_replacing_backing_file_after_construction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = JournalStore(path)
            original_identity = store.store_identity

            replacement = Path(directory) / "replacement.sqlite"
            JournalStore(replacement)
            path.unlink()
            replacement.replace(path)

            with self.assertRaises((RuntimeError, ValueError, OSError)):
                store.current_journal_sequence()

            self.assertEqual(store.store_identity, original_identity)

    def test_deleted_backing_file_is_not_silently_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = JournalStore(path)
            identity = store.store_identity
            path.unlink()

            with self.assertRaises(RuntimeError):
                store.current_journal_sequence()

            self.assertFalse(path.exists())
            self.assertEqual(store.store_identity, identity)

    def test_replacement_while_connection_is_open_is_detected_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "journal.sqlite"
            store = JournalStore(path)
            original_identity = store.store_identity

            replacement = root / "replacement.sqlite"
            JournalStore(replacement)

            with self.assertRaises(RuntimeError):
                with store._connect():
                    try:
                        replacement.replace(path)
                    except OSError:
                        self.skipTest(
                            "platform forbids replacing an open SQLite database path"
                        )

            self.assertEqual(store.store_identity, original_identity)


if __name__ == "__main__":
    unittest.main()
