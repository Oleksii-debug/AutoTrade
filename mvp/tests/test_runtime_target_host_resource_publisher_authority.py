from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.runtime_target_host_resource_evidence import (
    RuntimeTargetHostResourceEvidenceError,
    run_declared_target_host_campaign_with_resources,
)


INVENTORY_ARTIFACT_ID = "00000000-0000-4000-8000-000000000072"
MEASUREMENT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000073"
RUN_RECEIPT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000074"
RESOURCE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000075"


class RuntimeTargetHostResourcePublisherAuthorityTests(unittest.TestCase):
    def test_wrapper_rejects_callback_rebinding_of_artifact_store_publisher(self):
        original_publish_bytes = ArtifactStore.__dict__["publish_bytes"]
        forged_calls = 0

        def forged_publish_bytes(*_args, **_kwargs):
            nonlocal forged_calls
            forged_calls += 1
            raise AssertionError("forged ArtifactStore publisher executed")

        def malicious_runner(**_kwargs):
            ArtifactStore.publish_bytes = forged_publish_bytes
            return object()

        with tempfile.TemporaryDirectory() as root:
            store = ArtifactStore(Path(root) / "evidence")
            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.capture_runtime_target_host_resource_snapshot",
                        return_value=object(),
                    ),
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_resource_evidence.run_declared_target_host_campaign",
                        side_effect=malicious_runner,
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeTargetHostResourceEvidenceError,
                        "class descriptor changed.*publish_bytes",
                    ):
                        run_declared_target_host_campaign_with_resources(
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
            finally:
                ArtifactStore.publish_bytes = original_publish_bytes

        self.assertEqual(forged_calls, 0)


if __name__ == "__main__":
    unittest.main()
