from __future__ import annotations

from inspect import getclosurevars
import unittest
from unittest.mock import Mock, call, patch

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
    _build_module_authority_guard,
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
        measurement_guard = Mock()
        parent_guard = Mock()
        durable_guard = Mock()

        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=snapshotter,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
            measurement_dependency_guard=measurement_guard,
            parent_dependency_guard=parent_guard,
            durable_dependency_guard=durable_guard,
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
        self.assertEqual(measurement_guard.call_args_list, [call(), call()])
        snapshotter.assert_called_once_with(current)
        parent_guard.assert_called_once_with()
        parent.assert_called_once()
        durable_guard.assert_called_once_with()
        mechanics.assert_called_once()

    def test_parent_side_effect_cannot_retarget_nested_measurement_snapshot(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        current = Measurement()
        snapshotter = Mock(return_value=current)
        graph_changed = DurableError("measurement graph changed after parent evidence")
        measurement_guard = Mock(side_effect=(None, graph_changed))
        parent = Mock()
        durable_guard = Mock()
        mechanics = Mock()

        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=snapshotter,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=parent,
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
            measurement_dependency_guard=measurement_guard,
            parent_dependency_guard=Mock(),
            durable_dependency_guard=durable_guard,
        )

        with self.assertRaisesRegex(
            DurableError,
            "measurement graph changed after parent evidence",
        ):
            binder(
                store=object(),
                spec=object(),
                campaign_plan=object(),
                campaign_cut=object(),
                declared_plan_id="plan-1",
                measurement=current,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        self.assertEqual(measurement_guard.call_args_list, [call(), call()])
        snapshotter.assert_called_once_with(current)
        parent.assert_called_once()
        durable_guard.assert_not_called()
        mechanics.assert_not_called()

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

    def test_module_guard_rejects_root_code_replacement(self) -> None:
        class DurableError(Exception):
            pass

        def root():
            return 1

        def forged():
            return 2

        guard = _build_module_authority_guard(
            root=root,
            error_type=DurableError,
            label="root",
        )
        original_code = root.__code__
        try:
            root.__code__ = forged.__code__
            with self.assertRaisesRegex(
                DurableError,
                "root sealed executable changed",
            ):
                guard()
        finally:
            root.__code__ = original_code

        guard()

    def test_production_guards_reject_transitive_module_global_rebinds(self) -> None:
        closure = getclosurevars(
            bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
        ).nonlocals
        measurement_guard = closure["measurement_dependency_guard"]
        parent_guard = closure["parent_dependency_guard"]
        durable_guard = closure["durable_dependency_guard"]

        with patch.object(measurement_module, "_text", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "target-host measurement snapshotter sealed dependency changed",
            ):
                measurement_guard()

        with patch.object(measurement_authority, "_canonical_uuid", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "release-bound target-host evidence collector sealed dependency changed",
            ):
                parent_guard()

        with patch.object(durable, "_text", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "durable-financial binder sealed dependency changed",
            ):
                durable_guard()

        measurement_guard()
        parent_guard()
        durable_guard()


if __name__ == "__main__":
    unittest.main()
