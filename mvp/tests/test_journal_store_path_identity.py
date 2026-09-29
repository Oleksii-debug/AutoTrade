from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from autotrade_mvp.persistence import JournalStore


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


if __name__ == "__main__":
    unittest.main()
