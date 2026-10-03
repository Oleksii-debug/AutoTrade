from __future__ import annotations

from inspect import getclosurevars
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import persistence as persistence_module
from mvp.autotrade_mvp import runtime_load_measurement as load_measurement_module
from mvp.autotrade_mvp import runtime_load_plan as load_plan_module
from mvp.autotrade_mvp.runtime_target_host_durable_financial import (
    RuntimeTargetHostDurableFinancialError,
)
from mvp.autotrade_mvp.runtime_target_host_durable_financial_authority import (
    _build_module_authority_guard,
    _build_release_bound_durable_financial_authority,
    bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement,
)


RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "e" * 64


class RuntimeTargetHostDurableFinancialExternalAuthorityTests(unittest.TestCase):
    def test_module_guard_rejects_closure_cell_retargeting(self) -> None:
        class DurableError(Exception):
            pass

        def original() -> int:
            return 1

        def forged() -> int:
            return 2

        dependency = original

        def root() -> int:
            return dependency()

        guard = _build_module_authority_guard(
            root=root,
            error_type=DurableError,
            label="closure-root",
        )
        self.assertEqual(root(), 1)
        cell = next(
            cell for cell in root.__closure__ or () if cell.cell_contents is original
        )
        try:
            cell.cell_contents = forged
            self.assertEqual(root(), 2)
            with self.assertRaisesRegex(DurableError, "sealed closure changed"):
                guard()
        finally:
            cell.cell_contents = original

        guard()
        self.assertEqual(root(), 1)

    def test_factory_requires_immutable_external_guard_tuple(self) -> None:
        class DurableError(Exception):
            pass

        with self.assertRaisesRegex(
            TypeError,
            "durable_external_dependency_guards must be an exact tuple",
        ):
            _build_release_bound_durable_financial_authority(
                measurement_type=object,
                measurement_snapshotter=lambda value: value,
                uuid_type=__import__("uuid").UUID,
                durable_error_type=DurableError,
                parent_collector=Mock(),
                parent_error_types=(ValueError,),
                durable_binder=Mock(),
                durable_external_dependency_guards=[Mock()],
            )

    def test_durable_side_effect_cannot_retarget_external_graph_and_return(self) -> None:
        class Measurement:
            release_artifact_id = RELEASE_ID
            release_artifact_sha256 = RELEASE_SHA

        class DurableError(Exception):
            pass

        current = Measurement()
        state = {"changed": False}
        guard_calls: list[bool] = []

        def require_external_graph() -> None:
            guard_calls.append(state["changed"])
            if state["changed"]:
                raise DurableError("durable external graph changed during mechanics")

        expected = object()

        def mechanics(*_args, **_kwargs):
            state["changed"] = True
            return expected

        binder = _build_release_bound_durable_financial_authority(
            measurement_type=Measurement,
            measurement_snapshotter=lambda value: value,
            uuid_type=__import__("uuid").UUID,
            durable_error_type=DurableError,
            parent_collector=Mock(),
            parent_error_types=(ValueError,),
            durable_binder=mechanics,
            durable_external_dependency_guards=(require_external_graph,),
        )

        with self.assertRaisesRegex(
            DurableError,
            "durable external graph changed during mechanics",
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

        self.assertEqual(guard_calls, [False, True])

    def test_production_guards_cover_external_loader_and_store_graphs(self) -> None:
        closure = getclosurevars(
            bind_sealed_release_bound_durable_financial_latency_to_target_host_measurement
        ).nonlocals
        guards = closure["durable_external_dependency_guards"]
        self.assertIs(type(guards), tuple)
        self.assertEqual(len(guards), 5)
        by_label = {
            getclosurevars(guard).nonlocals["label"]: guard for guard in guards
        }

        expected_labels = {
            "durable runtime-plan loader",
            "durable latency-sample loader",
            "JournalStore authority validator",
            "JournalStore authority scope wrapper",
            "JournalStore authority scope body",
        }
        self.assertEqual(set(by_label), expected_labels)

        with patch.object(load_plan_module, "_text", Mock()):
            with self.assertRaisesRegex(
                RuntimeTargetHostDurableFinancialError,
                "durable runtime-plan loader sealed dependency changed",
            ):
                by_label["durable runtime-plan loader"]()

        with patch.object(load_measurement_module, "_measurement_event_id", Mock()):
            with self.assertRaisesRegex(
                RuntimeTargetHostDurableFinancialError,
                "durable latency-sample loader sealed dependency changed",
            ):
                by_label["durable latency-sample loader"]()

        with patch.object(persistence_module, "_require_exact_journal_store_state", Mock()):
            with self.assertRaisesRegex(
                RuntimeTargetHostDurableFinancialError,
                "JournalStore authority validator sealed dependency changed",
            ):
                by_label["JournalStore authority validator"]()

        with patch.object(persistence_module, "require_exact_journal_store_identity", Mock()):
            with self.assertRaisesRegex(
                RuntimeTargetHostDurableFinancialError,
                "JournalStore authority scope body sealed dependency changed",
            ):
                by_label["JournalStore authority scope body"]()

        wrapper = persistence_module.journal_store_authority_scope
        wrapped = wrapper.__wrapped__
        wrapper_cell = next(
            cell for cell in wrapper.__closure__ or () if cell.cell_contents is wrapped
        )
        try:
            wrapper_cell.cell_contents = lambda *_args, **_kwargs: None
            with self.assertRaisesRegex(
                RuntimeTargetHostDurableFinancialError,
                "JournalStore authority scope wrapper sealed closure changed",
            ):
                by_label["JournalStore authority scope wrapper"]()
        finally:
            wrapper_cell.cell_contents = wrapped

        for guard in guards:
            guard()


if __name__ == "__main__":
    unittest.main()
