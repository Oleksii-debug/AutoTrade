import unittest

from mvp.autotrade_mvp.lean_adoption import (
    LEAN_PIN,
    LeanAdoptionEvidence,
    qualify_lean_adoption,
)


class LeanAdoptionTests(unittest.TestCase):
    def evidence(self, **changes):
        values = dict(
            source_commit=LEAN_PIN,
            source_archive_sha256="sha256:source",
            windows_clean_build=True,
            linux_clean_build=True,
            canonical_decimal_vectors=True,
            callback_order_vectors=True,
            shutdown_restart_equivalent=True,
            simulator_bridge_verified=True,
            second_oms_absent=True,
            exact_source_license_recorded=True,
            evidence_ids=("win-build", "linux-build", "bridge", "restart"),
        )
        values.update(changes)
        return LeanAdoptionEvidence(**values)

    def test_complete_exact_pin_evidence_passes(self):
        result = qualify_lean_adoption(self.evidence())
        self.assertTrue(result.passed)
        self.assertEqual(result.reasons, ())

    def test_newer_or_unknown_commit_cannot_substitute_for_pin(self):
        result = qualify_lean_adoption(self.evidence(source_commit="deadbeef"))
        self.assertFalse(result.passed)
        self.assertIn("source_commit_not_pinned_baseline", result.reasons)

    def test_one_platform_build_is_not_cross_platform_qualification(self):
        result = qualify_lean_adoption(self.evidence(windows_clean_build=False))
        self.assertFalse(result.passed)
        self.assertIn("missing_windows_clean_build", result.reasons)

    def test_second_oms_is_a_hard_block(self):
        result = qualify_lean_adoption(self.evidence(second_oms_absent=False))
        self.assertFalse(result.passed)
        self.assertIn("missing_second_oms_absent", result.reasons)

    def test_restart_and_callback_evidence_are_independent(self):
        result = qualify_lean_adoption(
            self.evidence(shutdown_restart_equivalent=False, callback_order_vectors=False)
        )
        self.assertFalse(result.passed)
        self.assertIn("missing_shutdown_restart_equivalent", result.reasons)
        self.assertIn("missing_callback_order_vectors", result.reasons)


if __name__ == "__main__":
    unittest.main()
