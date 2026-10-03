from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp import runtime_target_host_composed_authority as composed_authority
from mvp.autotrade_mvp import runtime_target_host_qualification as qualification
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)
from mvp.tests.test_runtime_target_host_qualification import (
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


class RuntimeTargetHostReadIntegrityAuthorityTests(unittest.TestCase):
    @staticmethod
    def _verify(*, store, evidence_root, signed):
        return qualification.verify_runtime_target_host_qualification(
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

    @staticmethod
    def _fixture():
        raw_by_id, evidence_refs, _binding = material()
        return raw_by_id, evidence_refs, receipt(evidence_refs), accepted(evidence_refs)

    @staticmethod
    def _ref(evidence_refs, kind):
        return next(ref for ref in evidence_refs if ref.evidence_kind == kind)

    def test_composed_guard_rejects_pre_call_sha256_poisoning(self) -> None:
        original = qualification.sha256
        forged_calls = 0

        def forged(raw):
            nonlocal forged_calls
            forged_calls += 1
            return original(raw)

        qualification.sha256 = forged
        try:
            with self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                r"signed target-host verifier sealed dependency changed: sha256",
            ):
                composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD()
        finally:
            qualification.sha256 = original

        self.assertEqual(forged_calls, 0)
        composed_authority._PRODUCTION_SIGNED_VERIFIER_GUARD()

    def test_top_level_read_uses_retained_hash_not_self_restoring_global(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        binding_ref = self._ref(
            evidence_refs, qualification.BINDING_EVIDENCE_KIND
        )
        original_sha256 = qualification.sha256
        forged_calls = 0

        class ForgedDigest:
            def hexdigest(self):
                return binding_ref.sha256.removeprefix("sha256:")

        def forged(_raw):
            nonlocal forged_calls
            forged_calls += 1
            qualification.sha256 = original_sha256
            return ForgedDigest()

        def reader(artifact_id):
            if artifact_id == binding_ref.artifact_id:
                qualification.sha256 = forged
                return {}, b"{}"
            return {}, raw_by_id[artifact_id]

        try:
            with TemporaryDirectory() as directory:
                store = ArtifactStore(f"{directory}/store")
                with patch.object(
                    qualification,
                    "verify_canonical_qualification_attestation",
                    return_value=retained,
                ), patch.object(
                    qualification,
                    "trusted_authenticated_reader",
                    return_value=reader,
                ):
                    with self.assertRaisesRegex(
                        qualification.RuntimeTargetHostQualificationError,
                        "target-host artifact bytes do not match accepted evidence digest",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification.sha256 = original_sha256

        self.assertEqual(forged_calls, 0)

    def test_payload_read_uses_retained_hash_not_self_restoring_global(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        campaign_ref = self._ref(
            evidence_refs, qualification.CAMPAIGN_EVIDENCE_KIND
        )
        campaign_provenance = qualification.RuntimeTargetHostProvenance.parse(
            raw_by_id[campaign_ref.artifact_id]
        )
        payload_id = campaign_provenance.payload_artifact_id
        original_sha256 = qualification.sha256
        forged_calls = 0

        class ForgedDigest:
            def hexdigest(self):
                return campaign_provenance.payload_sha256.removeprefix("sha256:")

        def forged(_raw):
            nonlocal forged_calls
            forged_calls += 1
            qualification.sha256 = original_sha256
            return ForgedDigest()

        def reader(artifact_id):
            if artifact_id == payload_id:
                qualification.sha256 = forged
                return {}, b"{}"
            return {}, raw_by_id[artifact_id]

        try:
            with TemporaryDirectory() as directory:
                store = ArtifactStore(f"{directory}/store")
                with patch.object(
                    qualification,
                    "verify_canonical_qualification_attestation",
                    return_value=retained,
                ), patch.object(
                    qualification,
                    "trusted_authenticated_reader",
                    return_value=reader,
                ):
                    with self.assertRaisesRegex(
                        qualification.RuntimeTargetHostQualificationError,
                        "retained raw payload digest mismatch",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification.sha256 = original_sha256

        self.assertEqual(forged_calls, 0)

    def test_current_read_helper_code_mutation_is_rejected_inside_active_read(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        binding_id = self._ref(
            evidence_refs, qualification.BINDING_EVIDENCE_KIND
        ).artifact_id
        original_code = qualification._read_artifact_bytes.__code__

        def forged(
            reader,
            ref,
            *,
            digest_factory,
            post_read_authority_guard,
        ):
            raise AssertionError("forged read helper executed")

        def reader(artifact_id):
            if artifact_id == binding_id:
                qualification._read_artifact_bytes.__code__ = forged.__code__
            return {}, raw_by_id[artifact_id]

        try:
            with TemporaryDirectory() as directory:
                store = ArtifactStore(f"{directory}/store")
                with patch.object(
                    qualification,
                    "verify_canonical_qualification_attestation",
                    return_value=retained,
                ), patch.object(
                    qualification,
                    "trusted_authenticated_reader",
                    return_value=reader,
                ):
                    with self.assertRaisesRegex(
                        qualification.RuntimeTargetHostQualificationError,
                        "target-host evidence read helper sealed executable changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._read_artifact_bytes.__code__ = original_code

    def test_reader_cannot_install_forged_helper_for_subsequent_payload_read(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        binding_id = self._ref(
            evidence_refs, qualification.BINDING_EVIDENCE_KIND
        ).artifact_id
        original_code = qualification._read_bound_payload.__code__

        def forged(
            reader,
            provenance,
            *,
            forbidden_artifact_ids,
            forbidden_sha256,
            digest_factory,
            post_read_authority_guard,
        ):
            raise AssertionError("forged retained-payload helper executed")

        def reader(artifact_id):
            if artifact_id == binding_id:
                qualification._read_bound_payload.__code__ = forged.__code__
            return {}, raw_by_id[artifact_id]

        try:
            with TemporaryDirectory() as directory:
                store = ArtifactStore(f"{directory}/store")
                with patch.object(
                    qualification,
                    "verify_canonical_qualification_attestation",
                    return_value=retained,
                ), patch.object(
                    qualification,
                    "trusted_authenticated_reader",
                    return_value=reader,
                ):
                    with self.assertRaisesRegex(
                        qualification.RuntimeTargetHostQualificationError,
                        "target-host retained-payload read helper sealed executable changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._read_bound_payload.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
