import unittest

from mvp.autotrade_mvp.release_candidate import (
    ReleaseArtifactEvidence,
    ReleaseCandidateError,
    ReleaseCandidateInput,
    freeze_release_candidate,
)


SOURCE = "3cae63fac37820611cddd38128a91185cb271fff"
OTHER_SOURCE = "1" * 40
BASELINE = "sha256:" + "a" * 64
CONTRACTS = "sha256:" + "b" * 64

REQUIRED_ROLES = (
    "HOST",
    "WEB",
    "DESKTOP",
    "WINDOWS_PACKAGE",
    "SBOM",
    "DEPENDENCY_RIGHTS",
    "ACCESSIBILITY",
    "API_COMPATIBILITY",
    "CLEAN_INSTALL",
    "LICENSE_NOTICES",
)


def artifact(
    role,
    *,
    source_sha=SOURCE,
    signature_status=None,
    evidence_status="PASS",
    digest_char="c",
):
    if signature_status is None:
        signature_status = (
            "VERIFIED"
            if role in {"HOST", "WEB", "DESKTOP"}
            else "NOT_APPLICABLE"
        )
    return ReleaseArtifactEvidence.create(
        role=role,
        artifact_sha256="sha256:" + digest_char * 64,
        source_sha=source_sha,
        signature_status=signature_status,
        evidence_status=evidence_status,
    )


class ReleaseCandidateFreezeTests(unittest.TestCase):
    def candidate(self, **overrides):
        values = dict(
            release_id="autotrade-rc-20260924-1",
            source_sha=SOURCE,
            baseline_hash=BASELINE,
            schema_contract_hash=CONTRACTS,
            artifacts=tuple(
                artifact(role, digest_char=hex(index + 3)[2:])
                for index, role in enumerate(REQUIRED_ROLES)
            ),
            unresolved_blockers=(),
        )
        values.update(overrides)
        return ReleaseCandidateInput.create(**values)

    def test_complete_exact_evidence_freezes_deterministic_manifest(self):
        first = freeze_release_candidate(self.candidate())
        reordered = list(self.candidate().artifacts)
        reordered.reverse()
        second = freeze_release_candidate(
            self.candidate(artifacts=tuple(reordered))
        )
        self.assertEqual(first.status, "FROZEN")
        self.assertEqual(first.reasons, ())
        self.assertIsNotNone(first.manifest_json)
        self.assertEqual(first.manifest_sha256, second.manifest_sha256)
        self.assertEqual(first.manifest_json, second.manifest_json)

    def test_missing_required_artifact_blocks_freeze(self):
        artifacts = tuple(
            item for item in self.candidate().artifacts if item.role != "SBOM"
        )
        decision = freeze_release_candidate(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("missing_required_artifact:SBOM", decision.reasons)
        self.assertIsNone(decision.manifest_json)

    def test_artifact_from_another_source_sha_blocks_freeze(self):
        artifacts = list(self.candidate().artifacts)
        artifacts[0] = artifact("HOST", source_sha=OTHER_SOURCE)
        decision = freeze_release_candidate(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("source_sha_mismatch:HOST", decision.reasons)

    def test_unsigned_host_web_or_desktop_blocks_freeze(self):
        for role in ("HOST", "WEB", "DESKTOP"):
            with self.subTest(role=role):
                artifacts = [
                    (
                        artifact(role, signature_status="MISSING")
                        if item.role == role
                        else item
                    )
                    for item in self.candidate().artifacts
                ]
                decision = freeze_release_candidate(
                    self.candidate(artifacts=artifacts)
                )
                self.assertEqual(decision.status, "BLOCKED")
                self.assertIn(
                    f"signature_not_verified:{role}",
                    decision.reasons,
                )

    def test_failed_accessibility_is_a_blocker(self):
        artifacts = [
            (
                artifact("ACCESSIBILITY", evidence_status="FAIL")
                if item.role == "ACCESSIBILITY"
                else item
            )
            for item in self.candidate().artifacts
        ]
        decision = freeze_release_candidate(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn("evidence_failed:ACCESSIBILITY", decision.reasons)

    def test_inconclusive_dependency_rights_is_not_treated_as_pass(self):
        artifacts = [
            (
                artifact("DEPENDENCY_RIGHTS", evidence_status="INCONCLUSIVE")
                if item.role == "DEPENDENCY_RIGHTS"
                else item
            )
            for item in self.candidate().artifacts
        ]
        decision = freeze_release_candidate(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "evidence_inconclusive:DEPENDENCY_RIGHTS",
            decision.reasons,
        )

    def test_unresolved_blocker_prevents_manifest_publication(self):
        decision = freeze_release_candidate(
            self.candidate(
                unresolved_blockers=("provider-paper-qualification",)
            )
        )
        self.assertEqual(decision.status, "BLOCKED")
        self.assertIn(
            "unresolved_blocker:provider-paper-qualification",
            decision.reasons,
        )
        self.assertIsNone(decision.manifest_sha256)

    def test_duplicate_role_is_malformed_input_not_silently_overwritten(self):
        artifacts = self.candidate().artifacts
        with self.assertRaisesRegex(ReleaseCandidateError, "duplicate"):
            self.candidate(artifacts=artifacts + (artifacts[0],))

    def test_binary_signature_cannot_be_not_applicable(self):
        with self.assertRaisesRegex(ReleaseCandidateError, "NOT_APPLICABLE"):
            artifact("HOST", signature_status="NOT_APPLICABLE")

    def test_manifest_hash_changes_when_evidence_hash_changes(self):
        original = freeze_release_candidate(self.candidate())
        artifacts = list(self.candidate().artifacts)
        index = next(i for i, item in enumerate(artifacts) if item.role == "SBOM")
        artifacts[index] = artifact("SBOM", digest_char="f")
        changed = freeze_release_candidate(
            self.candidate(artifacts=artifacts)
        )
        self.assertEqual(changed.status, "FROZEN")
        self.assertNotEqual(
            original.manifest_sha256,
            changed.manifest_sha256,
        )


if __name__ == "__main__":
    unittest.main()
