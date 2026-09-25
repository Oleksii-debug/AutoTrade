from __future__ import annotations

import unittest

from control.tools.reconvergence_integrity import (
    Change,
    PROTECTED_SENTINELS,
    assess_reconvergence,
    parse_name_status,
)


class ReconvergenceIntegrityTests(unittest.TestCase):
    def test_mass_base_tree_deletion_fails_closed(self):
        base = [f"path-{index}.txt" for index in range(100)]
        changes = [Change(status="D", path=path) for path in base[:60]]

        result = assess_reconvergence(
            base_paths=base,
            changes=changes,
            max_deletions=50,
            max_deleted_fraction=0.35,
            protected_sentinels=frozenset(),
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.deletion_count, 60)
        self.assertEqual(result.deletion_fraction, 0.6)
        self.assertIn("mass base-tree deletion", result.reasons[0])

    def test_small_scoped_deletion_is_not_misclassified_as_tree_destruction(self):
        base = [f"path-{index}.txt" for index in range(100)]
        result = assess_reconvergence(
            base_paths=base,
            changes=[Change(status="D", path="path-1.txt")],
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 1)

    def test_protected_sentinel_deletion_fails_even_when_single_path(self):
        sentinel = "control/INDEX.json"
        self.assertIn(sentinel, PROTECTED_SENTINELS)

        result = assess_reconvergence(
            base_paths=[sentinel, "README.md", "mvp/runtime.py"],
            changes=[Change(status="D", path=sentinel)],
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.protected_deletions, (sentinel,))
        self.assertIn("protected canonical sentinel deletion", result.reasons[0])

    def test_rename_is_not_counted_as_deletion(self):
        changes = parse_name_status(["R100\told.py\tnew.py"])

        result = assess_reconvergence(
            base_paths=["old.py", "other.py"],
            changes=changes,
            max_deletions=1,
            max_deleted_fraction=0.1,
            protected_sentinels=frozenset(),
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.deletion_count, 0)

    def test_parser_rejects_malformed_records(self):
        with self.assertRaises(ValueError):
            parse_name_status(["R100\tonly-old-path"])


if __name__ == "__main__":
    unittest.main()
