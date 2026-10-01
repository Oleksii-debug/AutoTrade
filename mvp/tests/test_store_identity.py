from __future__ import annotations

from contextlib import nullcontext
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade_foundation.windows_namespace import require_windows_namespace_component
from mvp.autotrade_mvp.store_identity import (
    JournalStoreIdentity,
    connection_main_identity,
    establish_database_anchor,
    freeze_database_path,
    guard_windows_database_authority,
    observe_database_identity,
    require_database_identity,
    require_exact_journal_store_identity,
    same_journal_backing_object,
)


class StoreIdentityTests(unittest.TestCase):
    def test_exact_identity_raw_state_key_fails_before_callback(self):
        touched = []

        class PoisonKey:
            def __hash__(self):
                touched.append("hash")
                return hash("canonical_path")

            def __eq__(self, other):
                touched.append("eq")
                return other == "canonical_path"

        identity = JournalStoreIdentity(
            canonical_path="/tmp/journal.sqlite",
            filesystem_device=1,
            filesystem_inode=2,
        )
        state = vars(identity)
        canonical_path = state.pop("canonical_path")
        key = PoisonKey()
        state[key] = "poison"
        state["canonical_path"] = canonical_path
        touched.clear()

        with self.assertRaisesRegex(TypeError, "state keys must be exact str"):
            require_exact_journal_store_identity(identity)
        self.assertEqual(touched, [])

    def test_exact_identity_with_hostile_field_fails_before_reflected_equality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            sqlite3.connect(path).close()
            genuine = observe_database_identity(path)
            touched = []

            class HostileField:
                def __eq__(self, _other):
                    touched.append("eq")
                    return True

                def __ne__(self, _other):
                    touched.append("ne")
                    return False

            poisoned = JournalStoreIdentity(
                canonical_path=HostileField(),
                filesystem_device=genuine.filesystem_device,
                filesystem_inode=genuine.filesystem_inode,
                identity_source=genuine.identity_source,
                windows_volume_serial=genuine.windows_volume_serial,
                windows_file_index_high=genuine.windows_file_index_high,
                windows_file_index_low=genuine.windows_file_index_low,
            )
            with self.assertRaisesRegex(
                TypeError,
                "canonical_path must be exact non-empty str",
            ):
                require_database_identity(path, poisoned)
            self.assertEqual(touched, [])

    def test_foreign_expected_identity_fails_before_reflected_equality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            sqlite3.connect(path).close()
            touched = []

            class HostileIdentity:
                canonical_path = str(path.resolve())

                def __eq__(self, _other):
                    touched.append("eq")
                    return True

                def __ne__(self, _other):
                    touched.append("ne")
                    return False

            hostile = HostileIdentity()
            with self.assertRaisesRegex(
                TypeError,
                "expected journal store identity must be exact JournalStoreIdentity",
            ):
                require_database_identity(path, hostile)
            self.assertEqual(touched, [])

            genuine = observe_database_identity(path)
            self.assertIs(type(genuine), JournalStoreIdentity)
            self.assertEqual(require_database_identity(path, genuine), genuine)

    def test_freeze_database_path_binds_relative_text_to_construction_cwd(self) -> None:
        original_cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
                first = Path(first_dir)
                second = Path(second_dir)
                os.chdir(first)
                frozen = freeze_database_path("state/journal.sqlite")
                os.chdir(second)
                expected = first / "state" / "journal.sqlite"
                if sys.platform == "win32":
                    expected = Path(os.path.abspath(os.fspath(expected)))
                else:
                    expected = expected.resolve(strict=False)
                self.assertEqual(frozen, expected)
                self.assertTrue(frozen.is_absolute())
                os.chdir(original_cwd)
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

            if sys.platform == "win32":
                # Windows authority rejects the alias itself rather than
                # traversing it during pre-authority canonicalization.
                self.assertEqual(
                    freeze_database_path(alias),
                    Path(os.path.abspath(os.fspath(alias))),
                )
                with self.assertRaises(RuntimeError):
                    observe_database_identity(alias)
                self.assertIs(
                    type(observe_database_identity(target)),
                    JournalStoreIdentity,
                )
            else:
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
            self.assertEqual(anchor.canonical_path, str(freeze_database_path(path)))
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
            self.assertEqual(
                identity.canonical_path,
                str(freeze_database_path(original)),
            )

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

    @unittest.skipUnless(sys.platform == "win32", "Windows-only handle cleanup")
    def test_windows_identity_validation_failure_releases_all_guard_handles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "journal.sqlite"
            sqlite3.connect(path).close()
            moved = root / "moved.sqlite"

            with patch(
                "mvp.autotrade_mvp.store_identity._windows_identity_from_handle",
                side_effect=RuntimeError("injected native identity failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected native identity failure",
                ):
                    with guard_windows_database_authority(
                        path,
                        create=False,
                    ):
                        self.fail("identity failure must occur before guard yield")

            # A leaked no-FILE_SHARE_DELETE file or namespace handle would make
            # one of these rename operations fail on Windows.
            path.replace(moved)
            moved.replace(path)
            self.assertTrue(path.exists())

    def test_windows_guard_preserves_primary_when_handle_close_also_fails(self) -> None:
        identity = JournalStoreIdentity(
            canonical_path=str(Path.cwd() / "journal.sqlite"),
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=7,
            windows_file_index_high=11,
            windows_file_index_low=13,
        )
        primary = RuntimeError("primary SQLite failure")
        with (
            patch(
                "mvp.autotrade_mvp.store_identity.sys.platform",
                "win32",
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.retain_windows_parent_namespace",
                return_value=nullcontext(),
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.open_windows_regular_file",
                return_value=123,
            ),
            patch(
                "mvp.autotrade_mvp.store_identity._windows_identity_from_handle",
                return_value=identity,
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.close_windows_handle",
                side_effect=OSError("secondary CloseHandle failure"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                with guard_windows_database_authority(
                    "journal.sqlite",
                    create=False,
                ):
                    raise primary

        self.assertIs(caught.exception, primary)
        notes = getattr(caught.exception, "__notes__", ())
        self.assertTrue(
            any(
                "journal backing HANDLE cleanup also failed" in note
                and "secondary CloseHandle failure" in note
                for note in notes
            ),
            f"cleanup evidence missing from primary error notes: {notes!r}",
        )

    def test_windows_guard_surfaces_handle_close_failure_after_successful_body(self) -> None:
        identity = JournalStoreIdentity(
            canonical_path=str(Path.cwd() / "journal.sqlite"),
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=7,
            windows_file_index_high=11,
            windows_file_index_low=13,
        )
        with (
            patch(
                "mvp.autotrade_mvp.store_identity.sys.platform",
                "win32",
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.retain_windows_parent_namespace",
                return_value=nullcontext(),
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.open_windows_regular_file",
                return_value=123,
            ),
            patch(
                "mvp.autotrade_mvp.store_identity._windows_identity_from_handle",
                return_value=identity,
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.close_windows_handle",
                side_effect=OSError("CloseHandle failed after success"),
            ),
        ):
            with self.assertRaisesRegex(
                OSError,
                "CloseHandle failed after success",
            ):
                with guard_windows_database_authority(
                    "journal.sqlite",
                    create=False,
                ) as guarded:
                    self.assertEqual(guarded, identity)

    def test_windows_guard_keeps_authority_verdict_over_close_failure(self) -> None:
        identity = JournalStoreIdentity(
            canonical_path=str(Path.cwd() / "journal.sqlite"),
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=7,
            windows_file_index_high=11,
            windows_file_index_low=13,
        )
        primary = RuntimeError("primary SQLite failure")
        authority_error = RuntimeError("authority changed during failure")
        with (
            patch(
                "mvp.autotrade_mvp.store_identity.sys.platform",
                "win32",
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.retain_windows_parent_namespace",
                return_value=nullcontext(),
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.open_windows_regular_file",
                return_value=123,
            ),
            patch(
                "mvp.autotrade_mvp.store_identity._windows_identity_from_handle",
                side_effect=(identity, authority_error),
            ),
            patch(
                "mvp.autotrade_mvp.store_identity.close_windows_handle",
                side_effect=OSError("secondary CloseHandle failure"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                with guard_windows_database_authority(
                    "journal.sqlite",
                    create=False,
                ):
                    raise primary

        self.assertIs(caught.exception, authority_error)
        self.assertIs(caught.exception.__cause__, primary)
        notes = getattr(caught.exception, "__notes__", ())
        self.assertTrue(
            any(
                "journal backing HANDLE cleanup also failed" in note
                and "secondary CloseHandle failure" in note
                for note in notes
            ),
            f"cleanup evidence missing from authority error notes: {notes!r}",
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows-only native identity")
    def test_windows_identity_is_native_by_handle_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            sqlite3.connect(path).close()

            identity = observe_database_identity(path)

            self.assertEqual(identity.identity_source, "windows_by_handle")
            self.assertIsNone(identity.filesystem_device)
            self.assertIsNone(identity.filesystem_inode)
            self.assertIsNotNone(identity.windows_volume_serial)
            self.assertNotEqual(identity.windows_volume_serial, 0)
            self.assertNotEqual(
                (
                    identity.windows_file_index_high,
                    identity.windows_file_index_low,
                ),
                (0, 0),
            )

    def test_sqlite_database_list_observes_exact_opened_main_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            connection = sqlite3.connect(path)
            try:
                self.assertTrue(
                    same_journal_backing_object(
                        connection_main_identity(connection),
                        observe_database_identity(path),
                    )
                )
            finally:
                connection.close()


    def test_same_backing_object_uses_native_windows_identity_not_path_spelling(self) -> None:
        left = JournalStoreIdentity(
            canonical_path=r"C:\\TEMP\\SHORT~1\\journal.sqlite3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=7,
            windows_file_index_high=11,
            windows_file_index_low=13,
        )
        right = JournalStoreIdentity(
            canonical_path=r"C:\\Temp\\Long Directory\\journal.sqlite3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=7,
            windows_file_index_high=11,
            windows_file_index_low=13,
        )
        other = JournalStoreIdentity(
            canonical_path=right.canonical_path,
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=7,
            windows_file_index_high=11,
            windows_file_index_low=17,
        )

        self.assertTrue(same_journal_backing_object(left, right))
        self.assertFalse(same_journal_backing_object(left, other))

    def test_validated_identity_is_detached_from_mutable_dataclass_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            sqlite3.connect(path).close()
            original = observe_database_identity(path)
            expected_canonical_path = original.canonical_path
            validated = require_exact_journal_store_identity(original)
            self.assertIsNot(validated, original)
            self.assertEqual(validated, original)
            vars(original)["canonical_path"] = str(path.with_name("other.sqlite"))
            self.assertEqual(validated.canonical_path, expected_canonical_path)



    def test_windows_namespace_component_rejects_win32_alias_forms(self) -> None:
        invalid = (
            "journal.sqlite3:shadow",
            "journal.sqlite3.",
            "journal.sqlite3 ",
            "CON.sqlite3",
            "nul",
            "COM1.log",
            "LPT9.data",
            "bad?.sqlite3",
            "bad|name.sqlite3",
            "bad" + chr(1) + "name.sqlite3",
        )
        for name in invalid:
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeError, "canonical Win32"):
                    require_windows_namespace_component(
                        name,
                        subject="journal backing file",
                    )
        self.assertEqual(
            require_windows_namespace_component(
                "journal.sqlite3",
                subject="journal backing file",
            ),
            "journal.sqlite3",
        )

    @unittest.skipUnless(sys.platform == "win32", "native Windows pathname semantics")
    def test_windows_journal_rejects_alternate_stream_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "journal.sqlite3"
            candidate = Path(str(ordinary) + ":shadow")
            with self.assertRaisesRegex(RuntimeError, "canonical Win32"):
                establish_database_anchor(candidate)
            self.assertFalse(ordinary.exists())

    @unittest.skipUnless(sys.platform == "win32", "native Windows pathname semantics")
    def test_windows_journal_rejects_normalized_parent_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ambiguous_parent = root / "state."
            candidate = ambiguous_parent / "journal.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "namespace component"):
                establish_database_anchor(candidate)
            self.assertFalse(ambiguous_parent.exists())

if __name__ == "__main__":
    unittest.main()
