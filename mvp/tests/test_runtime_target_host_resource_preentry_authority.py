from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    TargetHostFinancialSample,
    TargetHostResearchSample,
)
from mvp.autotrade_mvp.runtime_target_host_resource_evidence import (
    RuntimeTargetHostResourceEvidence,
    RuntimeTargetHostResourceEvidenceError,
    run_declared_target_host_campaign_with_resources,
)
from mvp.autotrade_mvp.runtime_target_host_runner import RuntimeTargetHostRunResult


INVENTORY_ARTIFACT_ID = "00000000-0000-4000-8000-000000000072"
MEASUREMENT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000073"
RUN_RECEIPT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000074"
RESOURCE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000075"


def _invoke(store: ArtifactStore):
    return run_declared_target_host_campaign_with_resources(
        journal=object(),
        evidence_store=store,
        spec=object(),
        authority_id="resource-authority",
        research_plan_id="resource-research-plan",
        financial_operations={},
        research_operations={},
        inventory_artifact_id=INVENTORY_ARTIFACT_ID,
        measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
        run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
        resource_artifact_id=RESOURCE_ARTIFACT_ID,
    )


class RuntimeTargetHostResourcePreEntryAuthorityTests(unittest.TestCase):
    def test_preinstalled_resource_field_descriptor_is_rejected_before_first_cut(self):
        original = RuntimeTargetHostResourceEvidence.authority_id

        class ForgedAuthorityDescriptor:
            def __get__(self, instance, owner=None):
                if instance is None:
                    return self
                return "forged-pre-entry-authority"

            def __set__(self, instance, value):
                return None

        RuntimeTargetHostResourceEvidence.authority_id = ForgedAuthorityDescriptor()
        try:
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot"
                    ) as capture,
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign"
                    ) as runner,
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "class descriptor changed before target-host run.*authority_id",
                    ):
                        _invoke(store)
                capture.assert_not_called()
                runner.assert_not_called()
        finally:
            RuntimeTargetHostResourceEvidence.authority_id = original

    def test_preinstalled_parent_result_descriptor_is_rejected_before_first_cut(self):
        original = RuntimeTargetHostRunResult.measurement

        class ForgedMeasurementDescriptor:
            def __get__(self, instance, owner=None):
                if instance is None:
                    return self
                return object()

            def __set__(self, instance, value):
                return None

        RuntimeTargetHostRunResult.measurement = ForgedMeasurementDescriptor()
        try:
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot"
                    ) as capture,
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign"
                    ) as runner,
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "class descriptor changed before target-host run.*measurement",
                    ):
                        _invoke(store)
                capture.assert_not_called()
                runner.assert_not_called()
        finally:
            RuntimeTargetHostRunResult.measurement = original

    def test_callback_replacement_of_derived_property_fails_before_second_cut(self):
        original = RuntimeTargetHostResourceEvidence.elapsed_monotonic_ns

        def malicious_runner(**_kwargs):
            RuntimeTargetHostResourceEvidence.elapsed_monotonic_ns = property(
                lambda self: 0
            )
            return object()

        try:
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                        return_value=object(),
                    ) as capture,
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                        side_effect=malicious_runner,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "class descriptor changed during target-host run.*elapsed_monotonic_ns",
                    ):
                        _invoke(store)
                self.assertEqual(capture.call_count, 1)
        finally:
            RuntimeTargetHostResourceEvidence.elapsed_monotonic_ns = original

    def test_callback_replacement_of_nested_sample_payload_fails_before_second_cut(self):
        original = TargetHostResearchSample.payload

        def malicious_runner(**_kwargs):
            TargetHostResearchSample.payload = property(
                lambda self: {"interference_us": 0}
            )
            return object()

        try:
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                        return_value=object(),
                    ) as capture,
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                        side_effect=malicious_runner,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "class descriptor changed during target-host run.*TargetHostResearchSample.payload",
                    ):
                        _invoke(store)
                self.assertEqual(capture.call_count, 1)
        finally:
            TargetHostResearchSample.payload = original

    def test_callback_in_place_mutation_of_nested_classmethod_code_fails_closed(self):
        descriptor = TargetHostFinancialSample.__dict__["from_durable"]
        original_code = descriptor.__func__.__code__

        def forged_from_durable(cls, value):
            return object()

        def malicious_runner(**_kwargs):
            descriptor.__func__.__code__ = forged_from_durable.__code__
            return object()

        try:
            with tempfile.TemporaryDirectory() as root:
                store = ArtifactStore(Path(root) / "evidence")
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                        return_value=object(),
                    ) as capture,
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                        side_effect=malicious_runner,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "class executable changed during target-host run.*TargetHostFinancialSample.from_durable",
                    ):
                        _invoke(store)
                self.assertEqual(capture.call_count, 1)
        finally:
            descriptor.__func__.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
