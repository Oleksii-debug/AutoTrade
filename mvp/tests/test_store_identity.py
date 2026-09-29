from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autotrade_mvp.store_identity import (
    connection_main_identity,
    establish_database_anchor,
    freeze_database_path,
    observe_database_identity,
    require_database_identity,
)


class StoreIdentityTests(unittest.TestCase):
    def test_freeze_database_path_binds_relative_text_to_construction_cwd(self) -> None:
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                os.chdir(first)
                frozen = freeze_database_path("state/journal.sqlite")
                os.chdir(second)
                self.assertEqual(frozen, first / "state" / "journal.sqlite")
                self.assertTrue(frozen.is_absolute())
        finally:
            os.chdir(original_cwd)

    def test_symlink_aliases_canonicalize_to_one_store_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "journal.sqlite"
            sqlite3.connect(target).close()
            alias = root / "alias.sqlite"
            try:
                alias.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("filesystem does not permit symlink creation")

            self.assertEqual(freeze_database_path(alias), target.resolve())
            self.assertEqual(
                observe_database_identity(alias),
                observe_database_identity(target),
            )

    def test_initial_anchor_atomically_creates_and_binds_new_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            self.assertFalse(path.exists())

            anchor = establish_database_anchor(path)

            self.assertTrue(path.exists())
            self.assertEqual(anchor, observe_database_identity(path))
            self.assertEqual(anchor.canonical_path, str(path.resolve()))
            self.assertEqual(path.stat().st_nlink, 1)

            # Re-observing an already established path must return the same authority.
            self.assertEqual(establish_database_anchor(path), anchor)

    def test_hard_link_aliases_fail_closed_until_ambiguity_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "journal.sqlite"
            sqlite3.connect(original).close()
            alias = root / "hardlink.sqlite"
            try:
                os.link(original, alias)
            except OSError:
                self.skipTest("filesystem does not permit hard-link creation")

            with self.assertRaisesRegex(RuntimeError, "exactly one hard-link"):
                observe_database_identity(original)
            with self.assertRaisesRegex(RuntimeError, "exactly one hard-link"):
                observe_database_identity(alias)
            with self.assertRaisesRegex(RuntimeError, "exactly one hard-link"):
                establish_database_anchor(original)

            alias.unlink()
            identity = observe_database_identity(original)
            self.assertEqual(identity.canonical_path, str(original.resolve()))

    def test_replacement_at_same_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "journal.sqlite"
            sqlite3.connect(path).close()
            identity = observe_database_identity(path)

            replacement = root / "replacement.sqlite"
            sqlite3.connect(replacement).close()
            path.unlink()
            replacement.replace(path)

            with self.assertRaises(RuntimeError):
                require_database_identity(path, identity)

    def test_deleted_backing_path_fails_before_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            sqlite3.connect(path).close()
            identity = observe_database_identity(path)
            path.unlink()

            with self.assertRaises(RuntimeError):
                require_database_identity(path, identity)
            self.assertFalse(path.exists())

    def test_sqlite_database_list_observes_exact_opened_main_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection_main_identity(connection),
                    observe_database_identity(path),
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
