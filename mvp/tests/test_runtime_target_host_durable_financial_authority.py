from __future__ import annotations

from inspect import getclosurevars
import unittest
from unittest.mock import Mock, call, patch

from mvp.autotrade_mvp import persistence as persistence_module
from mvp.autotrade_mvp import runtime_load_measurement as load_measurement_module
from mvp.autotrade_mvp import runtime_load_plan as plan_module
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
        external_guard_a = Mock()
        external_guard_b = Mock()

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
            durable_external_dependency_guards=(external_guard_a, external_guard_b),
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
        self.assertEqual(durable_guard.call_args_list, [call(), call()])
        self.assertEqual(external_guard_a.call_args_list, [call(), call()])
        self.assertEqual(external_guard_b.call_args_list, [call(), call()])
        mechanics.assert_called_once()

    def test_parent_side_effect_cannot_retarget_nested_measurement_snapshot(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        current = Measurement()
        snapshotter = Mock(return_value=current)
        state = {"measurement_graph_changed": False}

        def require_measurement_graph() -> None:
            if state["measurement_graph_changed"]:
                raise DurableError("measurement graph changed after parent evidence")

        def mutate_measurement_graph(**_kwargs) -> None:
            state["measurement_graph_changed"] = True

        measurement_guard = Mock(side_effect=require_measurement_graph)
        parent = Mock(side_effect=mutate_measurement_graph)
        durable_guard = Mock()
        external_guard = Mock()
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
            durable_external_dependency_guards=(external_guard,),
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
        external_guard.assert_not_called()
        mechanics.assert_not_called()

    def test_durable_projection_rechecks_graphs_after_mechanics(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        current = Measurement()
        expected = object()
        state = {"external_graph_changed": False}

        def require_external_graph() -> None:
            if state["external_graph_changed"]:
                raise DurableError("external durable graph changed during projection")

        def mutate_graph(*_args, **_kwargs):
            state["external_graph_changed"] = True
            return expected

        durable_guard = Mock()
        external_guard = Mock(side_effect=require_external_graph)
        mechanics = Mock(side_effect=mutate_graph)
        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=Mock(),
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
            durable_dependency_guard=durable_guard,
            durable_external_dependency_guards=(external_guard,),
        )

        with self.assertRaisesRegex(
            DurableError,
            "external durable graph changed during projection",
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

        mechanics.assert_called_once()
        self.assertEqual(durable_guard.call_args_list, [call(), call()])
        self.assertEqual(external_guard.call_args_list, [call(), call()])

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
        self.assertEqual(len(before["durable_external_dependency_guards"]), 4)

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
            self.assertIs(
                after["durable_external_dependency_guards"],
                before["durable_external_dependency_guards"],
            )

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
        external_guards = closure["durable_external_dependency_guards"]

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

        with patch.object(persistence_module, "_require_exact_journal_store_state", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "JournalStore authority sealed dependency changed",
            ):
                external_guards[0]()

        with patch.object(persistence_module, "require_exact_journal_store_authority", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "JournalStore scope sealed dependency changed",
            ):
                external_guards[1]()

        with patch.object(plan_module, "_read_plan", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "declared-plan loader sealed dependency changed",
            ):
                external_guards[2]()

        with patch.object(load_measurement_module, "_decode_measurement", Mock()):
            with self.assertRaisesRegex(
                durable.RuntimeTargetHostDurableFinancialError,
                "sample loader sealed dependency changed",
            ):
                external_guards[3]()

        measurement_guard()
        parent_guard()
        durable_guard()
        for guard in external_guards:
            guard()


if __name__ == "__main__":
    unittest.main()
