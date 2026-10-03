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


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="latency-transitive-authority",
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
        event_id="financial-1",
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id="latency-transitive-authority",
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
            "committed_at": "2026-10-04T00:45:00Z",
        }
    )


class RuntimeLoadMeasurementTransitiveAuthorityTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "latency-transitive-authority.sqlite")

    def _declare(
        self,
        store: JournalStore,
        expected: ExpectedJournalEvent,
    ) -> str:
        return declare_runtime_event_plan(
            store,
            plan_id="latency-transitive-authority-plan",
            spec=_spec(),
            expected_events=(expected,),
        ).plan_id

    def _measure(self, store: JournalStore, expected: ExpectedJournalEvent, plan_id: str, operation) -> None:
        with patch(
            "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
            side_effect=(100, 200),
        ):
            measure_declared_financial_operation(
                store,
                _spec(),
                plan_id=plan_id,
                event_id=expected.event_id,
                operation=operation,
            )

    def _measurement_id(self, plan_id: str, event_id: str) -> str:
        return measurement_module._measurement_event_id(plan_id, event_id)

    def test_operation_cannot_rebind_measurement_id_sha256_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected()
            plan_id = self._declare(store, expected)
            original = measurement_module.sha256

            def attack() -> None:
                _append(store, expected)
                measurement_module.sha256 = lambda _value=b"": object()

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "transitive global dependency changed.*sha256",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                measurement_module.sha256 = original

            self.assertIsNone(store.get_event(self._measurement_id(plan_id, expected.event_id)))

    def test_operation_cannot_rebind_payload_digest_canonical_json_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected()
            plan_id = self._declare(store, expected)
            namespace = measurement_module.payload_digest.__globals__
            original = namespace["canonical_json"]

            def attack() -> None:
                _append(store, expected)
                namespace["canonical_json"] = lambda _value: "{}"

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "transitive global dependency changed.*canonical_json",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                namespace["canonical_json"] = original

            self.assertIsNone(store.get_event(self._measurement_id(plan_id, expected.event_id)))

    def test_operation_cannot_retarget_json_dumps_below_payload_digest(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected()
            plan_id = self._declare(store, expected)
            json_module = measurement_module.payload_digest.__globals__["json"]
            original = json_module.dumps

            def attack() -> None:
                _append(store, expected)
                json_module.dumps = lambda *_args, **_kwargs: "{}"

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    r"transitive module dependency changed.*json\.dumps",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                json_module.dumps = original

            self.assertIsNone(store.get_event(self._measurement_id(plan_id, expected.event_id)))

    def test_operation_cannot_override_journal_decode_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected()
            plan_id = self._declare(store, expected)
            had_own_member = "_decode_event_row" in JournalStore.__dict__
            original_member = JournalStore.__dict__.get("_decode_event_row")

            def forged_decode(_row):
                return {"event_id": expected.event_id, "journal_sequence": 1}

            def attack() -> None:
                _append(store, expected)
                JournalStore._decode_event_row = staticmethod(forged_decode)

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "JournalStore dependency changed.*_decode_event_row",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                if had_own_member:
                    JournalStore._decode_event_row = original_member
                else:
                    del JournalStore._decode_event_row

            self.assertIsNone(store.get_event(self._measurement_id(plan_id, expected.event_id)))

    def test_operation_cannot_mutate_journal_decoder_code_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected()
            plan_id = self._declare(store, expected)
            decoder = JournalStore._decode_event_row
            original_code = decoder.__code__

            def forged_decode(_row):
                return {"event_id": "forged"}

            def attack() -> None:
                _append(store, expected)
                decoder.__code__ = forged_decode.__code__

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "transitive executable authority changed.*_decode_event_row",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                decoder.__code__ = original_code

            self.assertIsNone(store.get_event(self._measurement_id(plan_id, expected.event_id)))

    def test_operation_cannot_rebind_plan_digest_payload_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected()
            plan_id = self._declare(store, expected)
            getter = DeclaredRuntimeEventPlan.digest.fget
            namespace = getter.__globals__
            original = namespace["payload_digest"]

            def attack() -> None:
                _append(store, expected)
                namespace["payload_digest"] = lambda _value: "sha256:" + ("0" * 64)

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    "transitive global dependency changed.*payload_digest",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                namespace["payload_digest"] = original

            self.assertIsNone(store.get_event(self._measurement_id(plan_id, expected.event_id)))


if __name__ == "__main__":
    unittest.main()
