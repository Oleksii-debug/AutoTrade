from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mvp.autotrade_mvp.runtime_load_measurement as measurement_module
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import (
    RuntimeLoadMeasurementError,
    measure_declared_financial_operation,
)
from mvp.autotrade_mvp.runtime_load_plan import (
    DeclaredRuntimeEventPlan,
    declare_runtime_event_plan,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="measurement-callback-authority",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def expected_event() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-1",
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id="measurement-callback-authority",
        aggregate_version=1,
    )


def append_expected(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"kind": "risk_decision", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-04T00:30:00Z",
        }
    )


class RuntimeLoadMeasurementCallbackAuthorityTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "callback-authority.sqlite")

    def _declare(
        self,
        store: JournalStore,
        expected: ExpectedJournalEvent,
    ) -> DeclaredRuntimeEventPlan:
        return declare_runtime_event_plan(
            store,
            plan_id="measurement-callback-authority-plan",
            spec=spec(),
            expected_events=(expected,),
        )

    def _measurement_event_id(self, plan_id: str, event_id: str) -> str:
        return measurement_module._measurement_event_id(plan_id, event_id)

    def test_operation_cannot_mutate_event_verifier_code_before_readback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = expected_event()
            plan = self._declare(store, expected)
            original_code = measurement_module._require_expected_event.__code__

            def forged_verifier(event, expected, *, after_sequence):
                return event

            def attack() -> None:
                append_expected(store, expected)
                measurement_module._require_expected_event.__code__ = (
                    forged_verifier.__code__
                )

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "financial event binding verifier executable authority changed",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=attack,
                    )
            finally:
                measurement_module._require_expected_event.__code__ = original_code

            self.assertIsNotNone(store.get_event(expected.event_id))
            self.assertIsNone(
                store.get_event(self._measurement_event_id(plan.plan_id, expected.event_id))
            )

    def test_operation_cannot_rebind_journal_reader_before_readback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = expected_event()
            plan = self._declare(store, expected)
            had_own_member = "get_event" in JournalStore.__dict__
            original_member = JournalStore.__dict__.get("get_event")

            def attack() -> None:
                append_expected(store, expected)
                JournalStore.get_event = lambda self, event_id: None

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "JournalStore event reader callable authority changed",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=attack,
                    )
            finally:
                if had_own_member:
                    JournalStore.get_event = original_member
                else:
                    del JournalStore.get_event

            self.assertIsNotNone(store.get_event(expected.event_id))
            self.assertIsNone(
                store.get_event(self._measurement_event_id(plan.plan_id, expected.event_id))
            )

    def test_operation_cannot_mutate_plan_digest_property_before_payload_issue(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = expected_event()
            plan = self._declare(store, expected)
            digest_property = DeclaredRuntimeEventPlan.__dict__["digest"]
            original_code = digest_property.fget.__code__

            def forged_digest(self) -> str:
                return "sha256:" + ("0" * 64)

            def attack() -> None:
                append_expected(store, expected)
                digest_property.fget.__code__ = forged_digest.__code__

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "plan digest property executable authority changed",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=attack,
                    )
            finally:
                digest_property.fget.__code__ = original_code

            self.assertIsNone(
                store.get_event(self._measurement_event_id(plan.plan_id, expected.event_id))
            )

    def test_operation_cannot_rebind_decoder_before_measurement_commit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = expected_event()
            plan = self._declare(store, expected)
            original_decoder = measurement_module._decode_measurement

            def attack() -> None:
                append_expected(store, expected)
                measurement_module._decode_measurement = lambda **kwargs: None

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        "latency measurement decoder callable authority changed",
                    ),
                ):
                    measure_declared_financial_operation(
                        store,
                        spec(),
                        plan_id=plan.plan_id,
                        event_id=expected.event_id,
                        operation=attack,
                    )
            finally:
                measurement_module._decode_measurement = original_decoder

            self.assertIsNone(
                store.get_event(self._measurement_event_id(plan.plan_id, expected.event_id))
            )


if __name__ == "__main__":
    unittest.main()
