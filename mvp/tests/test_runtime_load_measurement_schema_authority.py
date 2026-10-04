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
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="latency-schema-authority",
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


def _expected() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-schema-1",
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id="latency-schema-authority",
        aggregate_version=1,
    )


def _append(store: JournalStore, expected: ExpectedJournalEvent) -> None:
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
            "committed_at": "2026-10-04T02:30:00Z",
        }
    )


class RuntimeLoadMeasurementSchemaAuthorityTests(unittest.TestCase):
    def test_operation_cannot_execute_hostile_schema_version_comparison(self) -> None:
        class HostileSchemaVersion:
            def __init__(self) -> None:
                self.comparisons = 0

            def __eq__(self, _other: object) -> bool:
                self.comparisons += 1
                raise AssertionError("hostile schema equality executed after callback")

            def __ne__(self, _other: object) -> bool:
                self.comparisons += 1
                raise AssertionError("hostile schema inequality executed after callback")

        with tempfile.TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "latency-schema-authority.sqlite")
            expected = _expected()
            plan_id = declare_runtime_event_plan(
                store,
                plan_id="latency-schema-authority-plan",
                spec=_spec(),
                expected_events=(expected,),
            ).plan_id
            hostile = HostileSchemaVersion()
            original = JournalStore.SCHEMA_VERSION

            def attack() -> None:
                _append(store, expected)
                JournalStore.SCHEMA_VERSION = hostile

            try:
                with patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(100, 200),
                ):
                    with self.assertRaisesRegex(
                        RuntimeLoadMeasurementError,
                        r"JournalStore schema authority changed",
                    ):
                        measure_declared_financial_operation(
                            store,
                            _spec(),
                            plan_id=plan_id,
                            event_id=expected.event_id,
                            operation=attack,
                        )
            finally:
                JournalStore.SCHEMA_VERSION = original

            self.assertEqual(hostile.comparisons, 0)
            measurement_id = measurement_module._measurement_event_id(
                plan_id,
                expected.event_id,
            )
            self.assertIsNone(store.get_event(measurement_id))


if __name__ == "__main__":
    unittest.main()
