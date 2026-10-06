from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.provider_origin_journal_identity as identity_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin_journal_identity import (
    _journal_identity_material,
    canonical_provider_origin_journal_identity,
)
from mvp.autotrade_mvp.store_identity import JournalStoreIdentity


class ProviderOriginJournalIdentityTests(unittest.TestCase):
    def test_windows_path_spelling_is_not_backing_object_identity(self):
        left = JournalStoreIdentity(
            canonical_path=r"C:\\DATA\\journal.sqlite3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=17,
            windows_file_index_high=23,
            windows_file_index_low=42,
        )
        right = JournalStoreIdentity(
            canonical_path=r"c:\\data\\JOURNAL.sqlite3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=17,
            windows_file_index_high=23,
            windows_file_index_low=42,
        )
        self.assertEqual(
            _journal_identity_material(left),
            _journal_identity_material(right),
        )

    def test_posix_canonical_path_remains_part_of_identity(self):
        left = JournalStoreIdentity(
            canonical_path="/tmp/one/journal.sqlite3",
            filesystem_device=17,
            filesystem_inode=42,
        )
        right = JournalStoreIdentity(
            canonical_path="/tmp/two/journal.sqlite3",
            filesystem_device=17,
            filesystem_inode=42,
        )
        self.assertNotEqual(
            _journal_identity_material(left),
            _journal_identity_material(right),
        )

    def test_polymorphic_identity_is_rejected_before_field_trust(self):
        class ForgedIdentity(JournalStoreIdentity):
            pass

        forged = ForgedIdentity(
            canonical_path="/tmp/journal.sqlite3",
            filesystem_device=17,
            filesystem_inode=42,
        )
        with self.assertRaises(TypeError):
            _journal_identity_material(forged)

    def test_reopened_store_has_same_canonical_identity(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            first = JournalStore(path)
            first_identity = canonical_provider_origin_journal_identity(first)
            reopened = JournalStore(path)
            self.assertEqual(
                canonical_provider_origin_journal_identity(reopened),
                first_identity,
            )

    def test_different_backing_files_do_not_alias(self):
        with TemporaryDirectory() as directory:
            first = JournalStore(f"{directory}/first.sqlite3")
            second = JournalStore(f"{directory}/second.sqlite3")
            self.assertNotEqual(
                canonical_provider_origin_journal_identity(first),
                canonical_provider_origin_journal_identity(second),
            )

    def test_public_identity_function_retains_captured_helpers(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            expected = canonical_provider_origin_journal_identity(store)
            original_digest = identity_module.payload_digest
            original_material = identity_module._journal_identity_material
            identity_module.payload_digest = lambda _value: "sha256:" + "f" * 64
            identity_module._journal_identity_material = lambda _value: {
                "attacker": True
            }
            try:
                self.assertEqual(
                    canonical_provider_origin_journal_identity(store),
                    expected,
                )
            finally:
                identity_module.payload_digest = original_digest
                identity_module._journal_identity_material = original_material


if __name__ == "__main__":
    unittest.main()
