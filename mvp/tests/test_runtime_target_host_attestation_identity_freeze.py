from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    RuntimeTargetHostQualificationError,
    verify_runtime_target_host_qualification,
)
from mvp.tests.test_runtime_target_host_qualification import (
    ATTESTATION_DIGEST,
    CONFIG,
    HOST,
    JOURNAL,
    RELEASE_ID,
    RELEASE_SHA,
    SOURCE,
    SPEC,
    WORKLOAD,
    accepted,
    material,
    receipt,
)


_FORGED_ATTESTATION_ID = "10000000-0000-4000-8000-000000000099"
_FORGED_ATTESTATION_DIGEST = "sha256:" + "9" * 64


class RuntimeTargetHostAttestationIdentityFreezeTests(unittest.TestCase):
    @staticmethod
    def _verify(
        *,
        store,
        evidence_root,
        signed,
    ):
        return verify_runtime_target_host_qualification(
            signed,
            evidence_store=store,
            evidence_root=evidence_root,
            expected_source_sha=SOURCE,
            expected_scenario_id="target-host-primary",
            expected_spec_digest=SPEC,
            expected_configuration_hash=CONFIG,
            expected_host_fingerprint=HOST,
            expected_workload_profile_hash=WORKLOAD,
            expected_journal_store_identity_digest=JOURNAL,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )

    def test_artifact_callback_cannot_rebind_accepted_attestation_identity(self) -> None:
        raw_by_id, evidence_refs, _binding = material()
        signed = receipt(evidence_refs)
        retained = accepted(evidence_refs)
        original_id = retained.attestation_id
        original_digest = retained.attestation_digest
        mutated = False

        def reader(artifact_id):
            nonlocal mutated
            if not mutated:
                mutated = True
                object.__setattr__(
                    retained,
                    "attestation_id",
                    _FORGED_ATTESTATION_ID,
                )
                object.__setattr__(
                    retained,
                    "attestation_digest",
                    _FORGED_ATTESTATION_DIGEST,
                )
            if artifact_id not in raw_by_id:
                raise FileNotFoundError(artifact_id)
            return {}, raw_by_id[artifact_id]

        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                return_value=retained,
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
                return_value=reader,
            ):
                result = self._verify(
                    store=store,
                    evidence_root=directory,
                    signed=signed,
                )

        self.assertTrue(mutated)
        self.assertEqual(retained.attestation_id, _FORGED_ATTESTATION_ID)
        self.assertEqual(
            retained.attestation_digest,
            _FORGED_ATTESTATION_DIGEST,
        )
        self.assertEqual(result.attestation_id, original_id)
        self.assertEqual(result.attestation_digest, original_digest)
        self.assertEqual(result.attestation_digest, ATTESTATION_DIGEST)

    def test_noncanonical_accepted_attestation_identity_fails_before_profile_read(self) -> None:
        raw_by_id, evidence_refs, _binding = material()
        signed = receipt(evidence_refs)
        retained = accepted(evidence_refs)
        object.__setattr__(retained, "attestation_id", "not-a-uuid")
        reader_factory = Mock()

        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                return_value=retained,
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
                reader_factory,
            ):
                with self.assertRaisesRegex(
                    RuntimeTargetHostQualificationError,
                    "accepted attestation_id must be a canonical UUID",
                ):
                    self._verify(
                        store=store,
                        evidence_root=directory,
                        signed=signed,
                    )

        reader_factory.assert_not_called()

    def test_noncanonical_accepted_attestation_digest_fails_before_profile_read(self) -> None:
        raw_by_id, evidence_refs, _binding = material()
        signed = receipt(evidence_refs)
        retained = accepted(evidence_refs)
        object.__setattr__(retained, "attestation_digest", "not-a-digest")
        reader_factory = Mock()

        with TemporaryDirectory() as directory:
            store = ArtifactStore(f"{directory}/store")
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_canonical_qualification_attestation",
                return_value=retained,
            ), patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "trusted_authenticated_reader",
                reader_factory,
            ):
                with self.assertRaisesRegex(
                    RuntimeTargetHostQualificationError,
                    "accepted attestation_digest must be canonical sha256",
                ):
                    self._verify(
                        store=store,
                        evidence_root=directory,
                        signed=signed,
                    )

        reader_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
