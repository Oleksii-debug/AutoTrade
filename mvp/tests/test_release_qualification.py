import unittest

from mvp.autotrade_mvp.release_qualification import (
    REQUIRED_RELEASE_CHECKS,
    ReleaseCandidate,
    ReleaseCheck,
    ReleaseQualificationError,
    evaluate_release,
)


SOURCE = "a" * 40
OTHER_SOURCE = "b" * 40
DIGEST = "sha256:" + "c" * 64


def candidate(*, signatures=True):
    return ReleaseCandidate.create(
        version="0.1.0",
        source_sha=SOURCE,
        installer_sha256=DIGEST,
        diagnostics_sha256=DIGEST,
        sbom_sha256=DIGEST,
        compatibility_manifest_sha256=DIGEST,
        signatures_verified=signatures,
    )


def all_checks(*, source=SOURCE, override=None):
    override = override or {}
    return tuple(
        ReleaseCheck.create(
            name=name,
            status=override.get(name, "PASS"),
            source_sha=source,
            evidence_ref=f"evidence:{name.lower()}",
        )
        for name in REQUIRED_RELEASE_CHECKS
    )


class ReleaseQualificationTests(unittest.TestCase):
    def test_complete_exact_head_evidence_can_pass_without_granting_live_authority(self):
        decision = evaluate_release(candidate(), all_checks())
        self.assertEqual(decision.status, "PASS")
        self.assertFalse(decision.live_authority_granted)
        self.assertEqual(decision.source_sha, SOURCE)

    def test_missing_nvda_evidence_is_inconclusive(self):
        checks = tuple(
            row for row in all_checks()
            if row.name != "NVDA_KEYBOARD"
        )
        decision = evaluate_release(candidate(), checks)
        self.assertEqual(decision.status, "INCONCLUSIVE")
        self.assertEqual(decision.checks["NVDA_KEYBOARD"], "INCONCLUSIVE")
        self.assertTrue(any("NVDA_KEYBOARD" in reason for reason in decision.reasons))

    def test_stale_green_evidence_from_other_head_fails(self):
        checks = list(all_checks())
        index = REQUIRED_RELEASE_CHECKS.index("WINDOWS_VERIFICATION")
        checks[index] = ReleaseCheck.create(
            name="WINDOWS_VERIFICATION",
            status="PASS",
            source_sha=OTHER_SOURCE,
            evidence_ref="evidence:old-windows-green",
        )
        decision = evaluate_release(candidate(), checks)
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["WINDOWS_VERIFICATION"], "FAIL")
        self.assertTrue(any("different source SHA" in reason for reason in decision.reasons))

    def test_failed_provider_qualification_blocks_release(self):
        decision = evaluate_release(
            candidate(),
            all_checks(override={"PROVIDER_QUALIFICATION": "FAIL"}),
        )
        self.assertEqual(decision.status, "FAIL")
        self.assertEqual(decision.checks["PROVIDER_QUALIFICATION"], "FAIL")

    def test_unsigned_artifacts_cannot_pass_even_with_green_checks(self):
        decision = evaluate_release(candidate(signatures=False), all_checks())
        self.assertEqual(decision.status, "FAIL")
        self.assertTrue(any("signatures" in reason for reason in decision.reasons))

    def test_direct_candidate_cannot_use_truthy_string_as_verified_signature(self):
        with self.assertRaisesRegex(ReleaseQualificationError, "must be boolean"):
            ReleaseCandidate(
                version="0.1.0",
                source_sha=SOURCE,
                installer_sha256=DIGEST,
                diagnostics_sha256=DIGEST,
                sbom_sha256=DIGEST,
                compatibility_manifest_sha256=DIGEST,
                signatures_verified="false",
            )

    def test_direct_release_check_requires_real_evidence_reference(self):
        with self.assertRaisesRegex(ReleaseQualificationError, "evidence_ref is required"):
            ReleaseCheck(
                name="PR_CI",
                status="PASS",
                source_sha=SOURCE,
                evidence_ref="   ",
            )

    def test_malformed_hashes_and_unknown_checks_fail_closed(self):
        with self.assertRaisesRegex(ReleaseQualificationError, "canonical SHA-256"):
            ReleaseCandidate.create(
                version="0.1.0",
                source_sha=SOURCE,
                installer_sha256="bad",
                diagnostics_sha256=DIGEST,
                sbom_sha256=DIGEST,
                compatibility_manifest_sha256=DIGEST,
                signatures_verified=True,
            )
        with self.assertRaisesRegex(ReleaseQualificationError, "schema review"):
            evaluate_release(
                candidate(),
                all_checks()
                + (
                    ReleaseCheck.create(
                        name="UNREGISTERED_GATE",
                        status="PASS",
                        source_sha=SOURCE,
                        evidence_ref="evidence:unknown",
                    ),
                ),
            )

    def test_duplicate_gate_cannot_double_count_evidence(self):
        checks = all_checks()
        with self.assertRaisesRegex(ReleaseQualificationError, "duplicate release check"):
            evaluate_release(candidate(), checks + (checks[0],))


if __name__ == "__main__":
    unittest.main()
