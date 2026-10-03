from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from autotrade_runtime import strict_json as strict_json_module
from mvp.autotrade_mvp import runtime_target_host_qualification as qualification
from mvp.autotrade_mvp.runtime_target_host_campaign import ParsedRuntimeTargetHostCampaign
from mvp.autotrade_mvp.runtime_target_host_inventory import RuntimeTargetHostInventory
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


class RuntimeTargetHostPostReaderParserAuthorityTests(unittest.TestCase):
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

    def test_binding_read_cannot_install_self_restoring_local_json_parser(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        binding_id = self._ref(
            evidence_refs, qualification.BINDING_EVIDENCE_KIND
        ).artifact_id
        original = qualification._strict_json
        forged_calls = 0

        def forged(raw, *, name):
            nonlocal forged_calls
            forged_calls += 1
            qualification._strict_json = original
            return original(raw, name=name)

        def reader(artifact_id):
            if artifact_id == binding_id:
                qualification._strict_json = forged
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
                        r"target-host binding parser.*_strict_json",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._strict_json = original

        self.assertEqual(forged_calls, 0)

    def test_provenance_read_cannot_install_self_restoring_local_json_parser(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        campaign_ref = self._ref(
            evidence_refs, qualification.CAMPAIGN_EVIDENCE_KIND
        )
        original = qualification._strict_json
        forged_calls = 0

        def forged(raw, *, name):
            nonlocal forged_calls
            forged_calls += 1
            qualification._strict_json = original
            return original(raw, name=name)

        def reader(artifact_id):
            if artifact_id == campaign_ref.artifact_id:
                qualification._strict_json = forged
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
                        r"target-host provenance parser.*_strict_json",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            qualification._strict_json = original

        self.assertEqual(forged_calls, 0)

    def test_binding_read_cannot_retarget_strict_json_transitive_helper(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        binding_id = self._ref(
            evidence_refs, qualification.BINDING_EVIDENCE_KIND
        ).artifact_id
        original = strict_json_module._validate_json_nesting_before_decode
        forged_calls = 0

        def forged(text):
            nonlocal forged_calls
            forged_calls += 1
            strict_json_module._validate_json_nesting_before_decode = original
            return original(text)

        def reader(artifact_id):
            if artifact_id == binding_id:
                strict_json_module._validate_json_nesting_before_decode = forged
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
                        r"target-host strict JSON parser.*_validate_json_nesting_before_decode",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            strict_json_module._validate_json_nesting_before_decode = original

        self.assertEqual(forged_calls, 0)

    def test_campaign_payload_read_cannot_install_self_restoring_parser(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        campaign_ref = self._ref(
            evidence_refs, qualification.CAMPAIGN_EVIDENCE_KIND
        )
        campaign_provenance = qualification.RuntimeTargetHostProvenance.parse(
            raw_by_id[campaign_ref.artifact_id]
        )
        payload_id = campaign_provenance.payload_artifact_id
        original_descriptor = ParsedRuntimeTargetHostCampaign.__dict__["parse"]
        forged_calls = 0

        def forged(cls, raw):
            nonlocal forged_calls
            forged_calls += 1
            setattr(cls, "parse", original_descriptor)
            return original_descriptor.__func__(cls, raw)

        def reader(artifact_id):
            if artifact_id == payload_id:
                setattr(
                    ParsedRuntimeTargetHostCampaign,
                    "parse",
                    classmethod(forged),
                )
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
                        r"target-host campaign parser sealed class member changed: parse",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            setattr(ParsedRuntimeTargetHostCampaign, "parse", original_descriptor)

        self.assertEqual(forged_calls, 0)

    def test_inventory_payload_read_cannot_install_self_restoring_parser(self) -> None:
        raw_by_id, evidence_refs, signed, retained = self._fixture()
        inventory_ref = self._ref(
            evidence_refs, qualification.HOST_INVENTORY_EVIDENCE_KIND
        )
        inventory_provenance = qualification.RuntimeTargetHostProvenance.parse(
            raw_by_id[inventory_ref.artifact_id]
        )
        payload_id = inventory_provenance.payload_artifact_id
        original_descriptor = RuntimeTargetHostInventory.__dict__["parse"]
        forged_calls = 0

        def forged(cls, raw):
            nonlocal forged_calls
            forged_calls += 1
            setattr(cls, "parse", original_descriptor)
            return original_descriptor.__func__(cls, raw)

        def reader(artifact_id):
            if artifact_id == payload_id:
                setattr(RuntimeTargetHostInventory, "parse", classmethod(forged))
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
                        r"target-host inventory parser sealed class member changed: parse",
                    ):
                        self._verify(
                            store=store,
                            evidence_root=directory,
                            signed=signed,
                        )
        finally:
            setattr(RuntimeTargetHostInventory, "parse", original_descriptor)

        self.assertEqual(forged_calls, 0)


if __name__ == "__main__":
    unittest.main()
