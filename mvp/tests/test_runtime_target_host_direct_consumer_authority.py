from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp import runtime_target_host_qualification as qualification
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


class RuntimeTargetHostDirectConsumerAuthorityTests(unittest.TestCase):
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

    def test_canonical_verifier_cannot_retarget_evidence_snapshot_helper(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        original_code = qualification._snapshot_ref.__code__

        def forged(_value):
            raise AssertionError("forged evidence snapshot helper executed")

        def canonical_verifier(*_args, **_kwargs):
            qualification._snapshot_ref.__code__ = forged.__code__
            return retained

        def reader(artifact_id):
            return {}, raw_by_id[artifact_id]

        try:
            with TemporaryDirectory() as directory:
                store = ArtifactStore(f"{directory}/store")
                with patch.object(
                    qualification,
                    "verify_canonical_qualification_attestation",
                    side_effect=canonical_verifier,
                ), patch.object(
                    qualification,
                    "trusted_authenticated_reader",
                    return_value=reader,
                ):
                    with self.assertRaisesRegex(
                        qualification.RuntimeTargetHostQualificationError,
                        "target-host evidence-ref consumer sealed executable changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._snapshot_ref.__code__ = original_code

    def test_binding_read_cannot_retarget_identity_consumer(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        binding_id = self._ref(
            evidence_refs, qualification.BINDING_EVIDENCE_KIND
        ).artifact_id
        original_code = qualification._identity_tuple.__code__

        def forged(**_kwargs):
            raise AssertionError("forged identity consumer executed")

        def reader(artifact_id):
            if artifact_id == binding_id:
                qualification._identity_tuple.__code__ = forged.__code__
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
                        "target-host identity consumer sealed executable changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._identity_tuple.__code__ = original_code

    def test_provenance_read_cannot_retarget_identity_projection(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        campaign_ref = self._ref(
            evidence_refs, qualification.CAMPAIGN_EVIDENCE_KIND
        )
        original_code = qualification._provenance_identity.__code__

        def forged(_value):
            raise AssertionError("forged provenance identity consumer executed")

        def reader(artifact_id):
            if artifact_id == campaign_ref.artifact_id:
                qualification._provenance_identity.__code__ = forged.__code__
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
                        "target-host provenance-identity consumer sealed executable changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._provenance_identity.__code__ = original_code

    def test_campaign_payload_read_cannot_retarget_semantic_verifier(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        campaign_ref = self._ref(
            evidence_refs, qualification.CAMPAIGN_EVIDENCE_KIND
        )
        campaign_provenance = qualification.RuntimeTargetHostProvenance.parse(
            raw_by_id[campaign_ref.artifact_id]
        )
        payload_id = campaign_provenance.payload_artifact_id
        original_code = qualification._verify_campaign_payload.__code__

        def forged(*_args, **_kwargs):
            raise AssertionError("forged campaign semantic verifier executed")

        def reader(artifact_id):
            if artifact_id == payload_id:
                qualification._verify_campaign_payload.__code__ = forged.__code__
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
                        "target-host campaign semantic verifier sealed executable changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._verify_campaign_payload.__code__ = original_code

    def test_canonical_verifier_cannot_retarget_terminal_result_class(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        probe_name = "__direct_consumer_authority_probe__"

        def canonical_verifier(*_args, **_kwargs):
            setattr(
                qualification.AcceptedRuntimeTargetHostQualification,
                probe_name,
                object(),
            )
            return retained

        def reader(artifact_id):
            return {}, raw_by_id[artifact_id]

        try:
            with TemporaryDirectory() as directory:
                store = ArtifactStore(f"{directory}/store")
                with patch.object(
                    qualification,
                    "verify_canonical_qualification_attestation",
                    side_effect=canonical_verifier,
                ), patch.object(
                    qualification,
                    "trusted_authenticated_reader",
                    return_value=reader,
                ):
                    with self.assertRaisesRegex(
                        qualification.RuntimeTargetHostQualificationError,
                        "accepted target-host result sealed class namespace key set changed",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            if hasattr(
                qualification.AcceptedRuntimeTargetHostQualification,
                probe_name,
            ):
                delattr(
                    qualification.AcceptedRuntimeTargetHostQualification,
                    probe_name,
                )


if __name__ == "__main__":
    unittest.main()
