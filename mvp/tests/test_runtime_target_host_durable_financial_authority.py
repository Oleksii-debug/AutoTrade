from __future__ import annotations

from inspect import getclosurevars
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import (
    runtime_target_host_durable_financial as durable,
)
from mvp.autotrade_mvp import (
    runtime_target_host_measurement as measurement_module,
)
from mvp.autotrade_mvp import (
    runtime_target_host_measurement_authority as measurement_authority,
)
from mvp.autotrade_mvp.runtime_target_host_durable_financial_authority import (
    _build_release_bound_durable_financial_authority,
    bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement,
)


RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "e" * 64


class RuntimeTargetHostDurableFinancialAuthorityTests(unittest.TestCase):
    def test_factory_uses_only_injected_direct_dependencies(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        current = Measurement()
        snapshotter = Mock(return_value=current)
        parent = Mock()
        expected = object()
        mechanics = Mock(return_value=expected)

        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=snapshotter,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
        )

        result = binder(
            store=object(),
            spec=object(),
            campaign_plan=object(),
            campaign_cut=object(),
            declared_plan_id="plan-1",
            measurement=current,
            expected_release_artifact_id=RELEASE_ID,
            expected_release_artifact_sha256=RELEASE_SHA,
        )

        self.assertIs(result, expected)
        snapshotter.assert_called_once_with(current)
        parent.assert_called_once()
        mechanics.assert_called_once()

    def test_invalid_release_identity_fails_before_parent_or_mechanics(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        parent = Mock()
        mechanics = Mock()
        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
        )

        with self.assertRaisesRegex(DurableError, "canonical UUID"):
            binder(
                store=object(),
                spec=object(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=Measurement(),
                expected_release_artifact_id="not-a-uuid",
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        parent.assert_not_called()
        mechanics.assert_not_called()

    def test_invalid_release_digest_fails_before_parent_or_mechanics(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        parent = Mock()
        mechanics = Mock()
        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
        )

        with self.assertRaisesRegex(DurableError, "canonical sha256"):
            binder(
                store=object(),
                spec=object(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=Measurement(),
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256="sha256:" + "G" * 64,
            )

        parent.assert_not_called()
        mechanics.assert_not_called()

    def test_production_authority_retains_original_direct_dependencies_after_rebind(self) -> None:
        before = getclosurevars(
            bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
        ).nonlocals
        original_snapshotter = measurement_module.snapshot_target_host_measurement
        original_parent = (
            measurement_authority.collect_release_bound_target_host_evidence
        )
        original_mechanics = (
            durable.bind_durable_financial_latency_to_target_host_measurement
        )
        self.assertIs(before["measurement_snapshotter"], original_snapshotter)
        self.assertIs(before["parent_collector"], original_parent)
        self.assertIs(before["durable_binder"], original_mechanics)

        forged_snapshotter = Mock()
        forged_parent = Mock()
        forged_mechanics = Mock()
        with (
            patch.object(
                measurement_module,
                "snapshot_target_host_measurement",
                forged_snapshotter,
            ),
            patch.object(
                measurement_authority,
                "collect_release_bound_target_host_evidence",
                forged_parent,
            ),
            patch.object(
                durable,
                "bind_durable_financial_latency_to_target_host_measurement",
                forged_mechanics,
            ),
        ):
            after = getclosurevars(
                bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
            ).nonlocals
            self.assertIs(after["measurement_snapshotter"], original_snapshotter)
            self.assertIs(after["parent_collector"], original_parent)
            self.assertIs(after["durable_binder"], original_mechanics)

        forged_snapshotter.assert_not_called()
        forged_parent.assert_not_called()
        forged_mechanics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
