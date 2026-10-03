from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
    verify_composed_runtime_target_host_qualification,
)


MEASUREMENT = "sha256:" + "1" * 64
SPEC = "sha256:" + "2" * 64
WORKLOAD = "sha256:" + "3" * 64
OTHER_WORKLOAD = "sha256:" + "4" * 64
SOURCE = "a" * 40


class RuntimeTargetHostComposedDurablePlanIdentityTests(unittest.TestCase):
    def test_durable_plan_digest_mismatch_fails_before_signed_dispatch(self) -> None:
        frozen_measurement = SimpleNamespace(
            digest=MEASUREMENT,
            source_sha=SOURCE,
            spec_digest=SPEC,
            workload_profile_hash=WORKLOAD,
        )
        frozen_spec = object()
        frozen_plan = object()
        durable = SimpleNamespace(
            target_host_measurement_digest=MEASUREMENT,
            source_sha=SOURCE,
            spec_digest=SPEC,
            declared_plan_digest=OTHER_WORKLOAD,
        )

        with patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_snapshot_measurement",
            return_value=frozen_measurement,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_snapshot_spec",
            return_value=frozen_spec,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "_snapshot_campaign_plan",
            return_value=frozen_plan,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "bind_release_bound_durable_financial_latency_to_target_host_measurement",
            return_value=durable,
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_composed_qualification."
            "verify_runtime_target_host_qualification",
        ) as signed, self.assertRaisesRegex(
            RuntimeTargetHostCompositionError,
            "durable pre-run plan identity does not match canonical target-host workload identity",
        ):
            verify_composed_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                spec=object(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=object(),
                expected_release_artifact_id="70000000-0000-4000-8000-000000000001",
                expected_release_artifact_sha256="sha256:" + "f" * 64,
            )

        signed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
